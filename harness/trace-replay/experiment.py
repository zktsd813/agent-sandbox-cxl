"""Replay native mini traces in NUMA-bound Docker containers, without an LLM."""

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed


PERF_EVENTS = "{instructions,cycles,cache-references,cache-misses,cpu/event=0xd1,umask=0x20,name=retired_l3_misses/}"
WRITE_PAGE_CAPACITY = 16 * 1024 * 1024


def build_numa_helper(root, disable_thp=False):
    """Bind the payload after runc setup; movable-only CXL cannot hold kernel buffers."""
    source = root / "numa-bind.c"
    source.write_text(r'''#include <linux/mempolicy.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/prctl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 3 || (strcmp(argv[1], "0") && strcmp(argv[1], "1") && strcmp(argv[1], "2"))) return 125;
    if (DISABLE_THP && prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)) return 125;
    unsigned long mask = 1UL << atoi(argv[1]), actual = 0;
    int policy = 0;
    if (syscall(SYS_set_mempolicy, MPOL_BIND, &mask, 8 * sizeof(mask)) ||
        syscall(SYS_get_mempolicy, &policy, &actual, 8 * sizeof(actual), 0, 0) ||
        policy != MPOL_BIND || actual != mask) {
        perror("mini-numa-bind: memory policy verification failed");
        return 125;
    }
    execvp(argv[2], &argv[2]);
    perror("mini-numa-bind: exec failed");
    return 127;
}
'''.replace("DISABLE_THP", "1" if disable_thp else "0"))
    binary = root / "numa-bind"
    subprocess.run(["gcc", "-O2", "-static", str(source), "-o", str(binary)], check=True)
    return str(binary)


def write_tracker():
    """Privileged, cgroup-filtered observer; speak JSON over stdin/stdout."""
    from bcc import BPF
    import ctypes

    symbols = {}
    for line in Path("/proc/kallsyms").read_text().splitlines():
        address, _, name, *_ = line.split()
        if name in ("page_offset_base", "pgdir_shift", "vmemmap_base"):
            symbols[name] = int(address, 16)
    if len(symbols) != 3 or not all(symbols.values()):
        raise RuntimeError("Kernel address symbols are required for physical-page tracking")
    program = r'''
#include <uapi/linux/ptrace.h>
#include <linux/mm.h>
#include <linux/sched.h>
struct control { u64 cgroup; u64 pid; u64 start; u64 end; u64 epoch; };
struct pending { u64 address; u64 epoch; u64 mm; };
BPF_ARRAY(control, struct control, 1);
BPF_HASH(faults, u64, struct pending, 32768);
BPF_HASH(pages, u64, u64, WRITE_PAGE_CAPACITY);
BPF_ARRAY(errors, u64, 4);
BPF_ARRAY(counts, u64, 3);
static inline void error(int index) { u64 *v = errors.lookup(&index); if (v) __sync_fetch_and_add(v, 1); }
static inline int enabled(void) {
    int zero = 0;
    struct control *c = control.lookup(&zero);
    return c && c->cgroup && c->cgroup == bpf_get_current_cgroup_id()
        && (!c->pid || c->pid == (bpf_get_current_pid_tgid() >> 32));
}
static inline void save(u64 pfn, u64 kind) {
    int zero = 0;
    struct control *c = control.lookup(&zero);
    if (!c || !c->epoch) return;
    u64 empty = 0;
    u64 *old = pages.lookup_or_try_init(&pfn, &empty);
    if (!old) { error(0); return; }
    for (int retry = 0; retry < 16; retry++) {
        u64 previous = *old;
        u64 flags = (previous >> 2) == c->epoch ? previous & 3 : 0;
        u64 next = (c->epoch << 2) | flags | kind;
        if (__sync_val_compare_and_swap(old, previous, next) != previous) continue;
        if (!flags) { int index = 0; u64 *v = counts.lookup(&index); if (v) __sync_fetch_and_add(v, 1); }
        if (!(flags & kind)) { int index = kind == 1 ? 1 : 2; u64 *v = counts.lookup(&index); if (v) __sync_fetch_and_add(v, 1); }
        return;
    }
    error(0);
}
static inline u64 table_entry(u64 table, u64 address, int shift) {
    u64 entry = 0;
    bpf_probe_read_kernel(&entry, 8, (void *)(table + ((address >> shift) & 511) * 8));
    return entry;
}
int fault_start(struct pt_regs *ctx, struct vm_area_struct *vma, unsigned long address, unsigned int flags) {
    if (!enabled() || !(flags & FAULT_FLAG_WRITE)) return 0;
    int zero = 0;
    struct control *c = control.lookup(&zero);
    if (c && c->end && (address < c->start || address >= c->end)) return 0;
    u64 id = bpf_get_current_pid_tgid();
    struct pending value = {.address = address, .epoch = c ? c->epoch : 0};
    bpf_probe_read_kernel(&value.mm, 8, &vma->vm_mm);
    if (faults.update(&id, &value)) error(1);
    return 0;
}
int fault_end(struct pt_regs *ctx) {
    u64 id = bpf_get_current_pid_tgid();
    struct pending *saved = faults.lookup(&id);
    if (!saved) return 0;
    u64 address = saved->address, epoch = saved->epoch;
    struct mm_struct *mm = (void *)saved->mm;
    faults.delete(&id);
    if (!enabled() || (PT_REGS_RC(ctx) & (VM_FAULT_ERROR | VM_FAULT_RETRY))) return 0;
    int zero = 0;
    struct control *c = control.lookup(&zero);
    if (!c || c->epoch != epoch) return 0;
    u64 table = 0, base = 0;
    unsigned int shift = 0;
    if (!mm) { error(2); return 0; }
    bpf_probe_read_kernel(&table, 8, &mm->pgd);
    bpf_probe_read_kernel(&base, 8, (void *)PAGE_OFFSET_SYMBOL);
    bpf_probe_read_kernel(&shift, 4, (void *)PGDIR_SHIFT_SYMBOL);
    const u64 mask = 0x000ffffffffff000ULL;
    u64 entry;
    if (shift == 48) {
        entry = table_entry(table, address, 48);
        if (!(entry & 1)) { error(2); return 0; }
        table = (entry & mask) + base;
    }
    entry = table_entry(table, address, 39);
    if (!(entry & 1)) { error(2); return 0; }
    table = (entry & mask) + base;
    entry = table_entry(table, address, 30);
    if (!(entry & 1)) { error(2); return 0; }
    if (entry & 128) { error(3); return 0; }
    table = (entry & mask) + base;
    entry = table_entry(table, address, 21);
    if (!(entry & 1)) { error(2); return 0; }
    if (entry & 128) { error(3); return 0; }
    table = (entry & mask) + base;
    entry = table_entry(table, address, 12);
    if (!(entry & 1) || !(entry & 2)) { error(2); return 0; }
    save((entry & mask) >> 12, 1);
    return 0;
}
int copy_start(struct pt_regs *ctx, struct file *file, struct address_space *mapping, loff_t pos, unsigned len, unsigned copied) {
    if (!enabled()) return 0;
    if (!copied) return 0;
    struct page *page = (void *)ctx->r9;
    u64 offset = pos & 4095;
    u64 page_array_base = 0;
    bpf_probe_read_kernel(&page_array_base, 8, (void *)VMEMMAP_SYMBOL);
    u64 address = (((u64)page - page_array_base) / sizeof(struct page)) * 4096 + offset;
    u64 first = address >> 12, last = (address + copied - 1) >> 12;
    if (last - first >= 2) { error(3); return 0; }
    for (int i = 0; i < 2; i++) {
        if (first + i > last) break;
        save(first + i, 2);
    }
    return 0;
}
'''
    for placeholder, name in (("PAGE_OFFSET_SYMBOL", "page_offset_base"),
                              ("PGDIR_SHIFT_SYMBOL", "pgdir_shift"), ("VMEMMAP_SYMBOL", "vmemmap_base")):
        program = program.replace(placeholder, hex(symbols[name]) + "ULL")
    bpf = BPF(text=program.replace("WRITE_PAGE_CAPACITY", str(WRITE_PAGE_CAPACITY)))
    bpf.attach_kprobe(event="handle_mm_fault", fn_name="fault_start")
    bpf.attach_kretprobe(event="handle_mm_fault", fn_name="fault_end", maxactive=4096)
    for name in ("block_write_end", "generic_write_end", "ext4_write_end", "ext4_da_write_end", "ext4_journalled_write_end", "simple_write_end", "shmem_write_end"):
        bpf.attach_kprobe(event=name, fn_name="copy_start")
    control, pages, errors = bpf["control"], bpf["pages"], bpf["errors"]
    group, epoch = None, 0

    def missed_probes():
        rows = [line.split() for line in Path("/sys/kernel/tracing/kprobe_profile").read_text().splitlines()]
        own = [row for row in rows if row[0].endswith(f"_bcc_{os.getpid()}") and row[0].startswith("r_")]
        if len(own) != 1:
            raise RuntimeError("Could not verify the write-fault return-probe miss counter")
        return sum(int(row[-1]) for row in own)

    missed_at_start = missed_probes()
    print(json.dumps({"ready": True, "method": "write-fault PFNs plus supported buffered write_end destination PFNs"}), flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request["op"] == "close":
                break
            group = Path(request["cgroup"]).resolve()
            if not str(group).startswith("/sys/fs/cgroup/") or "minireplay" not in str(group):
                raise ValueError("Only this experiment's minireplay cgroups may be tracked")
            (group / "cgroup.freeze").write_text("1")
            deadline = time.monotonic() + 10
            while "frozen 1" not in (group / "cgroup.events").read_text():
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out freezing the workload for a measurement boundary")
                time.sleep(.001)
            control[ctypes.c_int(0)] = control.Leaf()
            if request["op"] == "begin":
                missed_at_start = missed_probes()
                epoch += 1
                for index in range(3):
                    bpf["counts"][ctypes.c_int(index)] = ctypes.c_ulonglong(0)
                bpf["faults"].clear()
                for index in range(4):
                    errors[ctypes.c_int(index)] = ctypes.c_ulonglong(0)
                for filename in [group / "cgroup.procs", *group.glob("**/cgroup.procs")]:
                    for pid in set(filename.read_text().split()):
                        try:
                            Path(f"/proc/{pid}/clear_refs").write_text("4")
                        except ProcessLookupError:
                            pass
                control[ctypes.c_int(0)] = control.Leaf(group.stat().st_ino, request.get("pid", 0),
                                                       request.get("start", 0), request.get("end", 0), epoch)
                result = {"begun": True}
            else:
                observed = [bpf["counts"][ctypes.c_int(index)].value for index in range(3)]
                counts = [errors[ctypes.c_int(index)].value for index in range(4)]
                missed = missed_probes() - missed_at_start
                result = {"write_fault_pages": observed[1],
                          "buffered_write_pages": observed[2],
                          "unique_observed_pages": observed[0], "unique_observed_bytes": observed[0] * 4096,
                          "map_update_failures": counts[0] + counts[1], "unresolved_faults": counts[2],
                          "unsupported_large_pages": counts[3], "missed_return_probes": missed,
                          "valid": not any(counts) and missed == 0}
            (group / "cgroup.freeze").write_text("0")
            print(json.dumps(result), flush=True)
    finally:
        if group is not None and group.exists():
            (group / "cgroup.freeze").write_text("0")
        bpf.cleanup()


def read_perf(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.startswith("{")]
    totals = {row["event"]: row for row in rows if "interval" not in row}
    required = ("instructions", "cycles", "cache-references", "cache-misses", "retired_l3_misses")
    valid = all(event in totals and totals[event]["counter-value"] not in ("<not counted>", "<not supported>")
                and float(totals[event]["pcnt-running"]) >= 99 for event in required)
    valid = valid and all(float(row["pcnt-running"]) >= 99 for row in rows if row["event"] in required and row["event-runtime"] > 0)
    counts = {key: int(float(row["counter-value"])) for key, row in totals.items()
              if row["counter-value"] not in ("<not counted>", "<not supported>")}
    instructions = counts.get("instructions", 0)
    valid = valid and instructions > 0
    return {"valid": valid, "counts": counts, "totals": totals,
            "llc_mpki": 1000 * counts["cache-misses"] / instructions if valid else None,
            "retired_load_l3_mpki": 1000 * counts["retired_l3_misses"] / instructions if valid else None,
            "ipc": instructions / counts["cycles"] if valid and counts["cycles"] else None}, rows


def perf_control(monitor, command):
    os.write(monitor["control_fd"], (command + "\n").encode())
    if not select.select([monitor["ack_fd"]], [], [], 15)[0]:
        raise RuntimeError(f"perf did not acknowledge {command}; inspect {monitor['path']}")
    if not os.read(monitor["ack_fd"], 1024).startswith(b"ack\n"):
        raise RuntimeError("Invalid perf acknowledgement")


def start_perf(session, arguments, clock, record, output):
    path = output / f"{session['name']}.perf.jsonl"
    monitor = {"path": path, "process": None, "origin_s": time.monotonic() - clock}
    session["perf"] = monitor  # Keep partial setup reachable by cleanup.
    for kind in ("control", "ack"):
        fifo = path.with_suffix(f".{kind}.fifo")
        os.mkfifo(fifo, 0o600)
        monitor[kind + "_path"] = fifo
        monitor[kind + "_fd"] = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    group = str(session["cgroup"].relative_to("/sys/fs/cgroup"))
    command = ["sudo", "-n", str(arguments.perf_bin), "stat", "-a", "-C", arguments.workload_cpus,
               "-e", PERF_EVENTS, "-G", group, "-j", "-I", str(arguments.perf_interval_ms),
               "--summary", "--delay=-1", f"--control=fifo:{monitor['control_path']},{monitor['ack_path']}"]
    with path.open("w") as stream:
        monitor["origin_s"] = time.monotonic() - clock
        monitor["process"] = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stream)
    perf_control(monitor, "enable")
    record("perf_start", session=session["name"], origin_s=monitor["origin_s"], command=command)


def stop_perf(session, record):
    monitor = session.get("perf")
    if not monitor:
        return
    try:
        process = monitor["process"]
        if process and process.poll() is None:
            perf_control(monitor, "disable")
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)
        result, _ = read_perf(monitor["path"])
        record("perf_end", session=session["name"], **result)
        if not result["valid"]:
            raise RuntimeError("Invalid PMU measurement (missing counts or counter coverage below 99%)")
    finally:
        process = monitor["process"]
        if process and process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)
        for kind in ("control", "ack"):
            if kind + "_fd" in monitor:
                os.close(monitor[kind + "_fd"])
            if kind + "_path" in monitor:
                monitor[kind + "_path"].unlink(missing_ok=True)


def download_traces(inventory_path, destination):
    inventory = json.loads(inventory_path.read_text())
    base_url = f"https://raw.githubusercontent.com/{inventory['repository']}/{inventory['commit']}/"
    destination.mkdir(parents=True, exist_ok=True)
    for index, entry in enumerate(inventory["traces"], 1):
        path = destination / Path(entry["path"]).name
        if path.exists():
            raw = path.read_bytes()
        else:
            with urllib.request.urlopen(base_url + entry["path"], timeout=60) as response:
                raw = response.read()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Trace size/hash mismatch: {path}")
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(raw)
            temporary.replace(path)
        print(f"[{index}/{len(inventory['traces'])}] verified {path.name}", flush=True)


def submission_fingerprint(text):
    warning = "/root/.bashrc: line 1: /opt/miniconda3/etc/profile.d/conda.sh: No such file or directory\n"
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(),
            "without_startup_warning_sha256": hashlib.sha256(text.replace(warning, "").encode()).hexdigest(),
            "startup_warning_count": text.count(warning)}


def load_trace(source):
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=30) as response:
            raw = response.read()
    else:
        raw = Path(source).read_bytes()
    trace = json.loads(raw)
    settings = trace["info"]["config"]
    if not settings["environment_type"].endswith(".DockerEnvironment"):
        raise ValueError(f"Only Docker traces are supported: {source}")
    environment = settings["environment"]
    if environment.get("forward_env"):
        raise ValueError("Host environment forwarding is unsupported")
    batches = []
    for message in trace["messages"]:
        extra = message.get("extra", {})
        if message["role"] == "assistant" and extra.get("actions"):
            batches.append({"at": extra.get("timestamp"), "actions": extra["actions"], "observations": []})
        elif batches and message["role"] in ("tool", "user") and "returncode" in extra:
            batches[-1]["observations"].append(extra)
    if not batches:
        raise ValueError(f"No recorded actions: {source}")
    previous_end = None
    for index, batch in enumerate(batches):
        times = [batch["at"]] + [observation.get("timestamp") for observation in batch["observations"]]
        if any(not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) for t in times):
            raise ValueError(f"Missing/invalid timestamp in batch {index}: {source}")
        if times != sorted(times) or previous_end is not None and batch["at"] < previous_end:
            raise ValueError(f"Out-of-order timestamps in batch {index}: {source}")
        observation_count = len(batch["observations"])
        if observation_count != len(batch["actions"]) and not (index == len(batches) - 1 and observation_count == 0):
            raise ValueError(f"Incomplete observations in batch {index}: {source}")
        if any(not isinstance(action.get("command"), str) for action in batch["actions"]):
            raise ValueError(f"Unsupported action in batch {index}: {source}")
        batch["gap"] = 0 if previous_end is None else batch["at"] - previous_end
        previous_end = times[-1]
    submission = trace["info"].get("submission", "")
    return {"source": source, "sha256": hashlib.sha256(raw).hexdigest(), "environment": environment,
            "source_exit_status": trace["info"].get("exit_status"),
            "source_submission_sha256": hashlib.sha256(submission.encode()).hexdigest()
                if trace["info"].get("exit_status") == "Submitted" else None,
            "source_submission_fingerprint": submission_fingerprint(submission),
            "command_count": sum(len(batch["actions"]) for batch in batches),
            "last_action_command": batches[-1]["actions"][-1]["command"],
            "name": trace.get("instance_id") or Path(source).name, "batches": batches,
            "first": batches[0]["at"], "last": previous_end}


def cpu_ids(value):
    result = set()
    for part in value.split(","):
        bounds = [int(number) for number in part.split("-")]
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def docker(*arguments, **kwargs):
    return subprocess.run(["docker", *arguments], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=kwargs.pop("timeout", 120), **kwargs).stdout.strip()


def task_image(trace, config):
    image = trace["environment"]["image"]
    return config["sample"]["currently_observed_image"] if image == config["sample"]["image_reference_from_trace"] else image


def systemd(*arguments):
    return subprocess.run(["sudo", "-n", "systemctl", *arguments], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120).stdout.strip()


def container_group(name):
    pid = docker("inspect", "--format", "{{.State.Pid}}", name)
    group = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if line.startswith("0::"))
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def create_budget(config, name, image_id):
    allowed_mems = "0,2" if config.get("numa_bind_helper") and not config.get("write_tracking") else ",".join(str(n) for n in sorted({config["numa_node"], int(config["memory_nodes"])}))
    docker("run", "-d", "--name", name, "--cgroup-parent", config["budget_unit"],
           "--cpuset-cpus", config["controller_cpus"], "--cpuset-mems", str(config["numa_node"]), "--memory", "64m",
           "--memory-swap", "64m", "--pids-limit", "64", "--network", "none", image_id, "sleep", "infinity")
    systemd("set-property", "--runtime", config["budget_unit"],
            f"MemoryMax={config['aggregate_memory_limit_bytes']}", "MemorySwapMax=0",
            f"AllowedCPUs={config['node_cpu_pool']}", f"AllowedMemoryNodes={allowed_mems}", "TasksMax=65536")
    group = container_group(name).parent
    if int((group / "memory.max").read_text()) != config["aggregate_memory_limit_bytes"]:
        raise RuntimeError("Shared memory budget was not applied")
    if (group / "memory.swap.max").read_text().strip() != "0" or cpu_ids((group / "cpuset.mems.effective").read_text().strip()) != cpu_ids(allowed_mems):
        raise RuntimeError("Shared NUMA/swap constraint was not applied")
    return group


def resources(cgroup, memory=True, target_node=0):
    cpu = dict(line.split() for line in (cgroup / "cpu.stat").read_text().splitlines())
    result = {"cpu_usec": int(cpu["usage_usec"]), "throttled_usec": int(cpu.get("throttled_usec", 0))}
    if not memory:
        return result
    memory_stat = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
    numa = {}
    for line in (cgroup / "memory.numa_stat").read_text().splitlines():
        kind, *nodes = line.split()
        if kind in ("anon", "file"):
            numa[kind] = dict(node.split("=") for node in nodes)
    events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
    pids = dict(line.split() for line in (cgroup / "pids.events").read_text().splitlines())
    io = {}
    for line in (cgroup / "io.stat").read_text().splitlines():
        for item in line.split()[1:]:
            key, value = item.split("=")
            io[key] = io.get(key, 0) + int(value)
    pressure = {}
    for kind in ("cpu", "memory", "io"):
        for line in (cgroup / f"{kind}.pressure").read_text().splitlines():
            fields = dict(item.split("=") for item in line.split()[1:])
            pressure[f"{kind}_pressure_{line.split()[0]}_usec"] = int(fields["total"])
    peak = cgroup / "memory.peak"
    return {**result, **pressure, "io_read_bytes": io.get("rbytes", 0), "io_write_bytes": io.get("wbytes", 0),
            "minor_and_major_faults": int(memory_stat.get("pgfault", 0)),
            "major_faults": int(memory_stat.get("pgmajfault", 0)),
            "memory_bytes": int((cgroup / "memory.current").read_text()),
            "file_dirty_bytes": int(memory_stat.get("file_dirty", 0)),
            "file_writeback_bytes": int(memory_stat.get("file_writeback", 0)),
            "peak_bytes": int(peak.read_text()) if peak.exists() else None,
            "anon_bytes": int(memory_stat["anon"]), "file_bytes": int(memory_stat["file"]),
            "anon_node0_bytes": int(numa["anon"].get("N0", 0)),
            "anon_target_bytes": int(numa["anon"].get(f"N{target_node}", 0)),
            "file_target_bytes": int(numa["file"].get(f"N{target_node}", 0)),
            "anon_other_bytes": sum(int(size) for node, size in numa["anon"].items() if node != f"N{target_node}"),
            "file_other_bytes": sum(int(size) for node, size in numa["file"].items() if node != f"N{target_node}"),
            "swap_bytes": int((cgroup / "memory.swap.current").read_text()),
            "oom_kill": int(events.get("oom_kill", 0)),
            "pids_current": int((cgroup / "pids.current").read_text()), "pids_limit_events": int(pids.get("max", 0))}


def prepare(trace, config, name, pull, image_id=None):
    image = task_image(trace, config)
    if pull:
        docker("pull", image, timeout=1800)
    image_id = image_id or docker("image", "inspect", "--format", "{{.Id}}", image)
    memory_mask = "0,2" if config.get("numa_bind_helper") and not config.get("write_tracking") else config["memory_nodes"]
    extra, init = [], ["sleep", "infinity"]
    if config.get("numa_bind_helper"):
        extra = ["--mount", f"type=bind,src={config['numa_bind_helper']},dst=/opt/mini-numa-bind,readonly", "--cap-add", "SYS_NICE"]
        init = ["/opt/mini-numa-bind", config["memory_nodes"], *init]
    docker("run", "-d", "--name", name, "--cgroup-parent", config["budget_unit"], "--cpuset-cpus", config["workload_cpus"],
           "--cpuset-mems", memory_mask, "--memory", str(config["memory_limit_bytes"]),
           "--memory-swap", str(config["memory_limit_bytes"]), "--pids-limit", str(config["pids_limit"]),
           "-w", trace["environment"]["cwd"], *extra, image_id, *init)
    cgroup = container_group(name)
    cpus = (cgroup / "cpuset.cpus.effective").read_text().strip()
    mems = (cgroup / "cpuset.mems.effective").read_text().strip()
    if cpu_ids(cpus) != cpu_ids(config["workload_cpus"]) or cpu_ids(mems) != cpu_ids(memory_mask):
        raise RuntimeError(f"Unexpected effective cpuset for {name}: {cpus}, {mems}")
    if (cgroup / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError(f"Swap was not disabled for {name}")
    for filename, value in (("memory.max", config["memory_limit_bytes"]), ("pids.max", config["pids_limit"])):
        if (cgroup / filename).read_text().strip() != str(value):
            raise RuntimeError(f"Unexpected {filename} for {name}")
    return {"name": name, "trace": trace, "image_id": image_id, "cgroup": cgroup,
            "memory_node": int(config["memory_nodes"]), "numa_bind_helper": bool(config.get("numa_bind_helper")),
            "phase": "ready", "failure": None}


def sample(sessions, clock, intervals, stop, record, destination, keep_going=False):
    cached, next_memory = {}, 0
    with destination.open("w", newline="") as stream:
        writer = None
        while not stop.is_set():
            started = time.monotonic()
            read_memory = started >= next_memory
            for session in list(sessions):
                if session["phase"] == "done" or session["failure"]:
                    continue
                try:
                    values = resources(session["cgroup"], memory=read_memory or session["name"] not in cached,
                                       target_node=session["memory_node"])
                    cached.setdefault(session["name"], {}).update(values)
                    row = {"elapsed_s": time.monotonic() - clock, "sample_cycle_s": started - clock,
                           "session": session["name"],
                           "phase": session["phase"], **cached[session["name"]]}
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=row)
                        writer.writeheader()
                    writer.writerow(row)
                    wrong_node = row["anon_other_bytes"] and (not session["numa_bind_helper"] or row["phase"] in ("ready", "waiting"))
                    if wrong_node or row["swap_bytes"] or row["oom_kill"] or row["pids_limit_events"]:
                        record("error", session=session["name"], error="NUMA/swap/OOM/PID constraint failure", resources=values)
                        session["failure"] = "NUMA/swap/OOM/PID constraint failure"
                        if not keep_going:
                            stop.set()
                except OSError as error:
                    if session["phase"] != "done":
                        record("error", session=session["name"], error=str(error))
                        session["failure"] = str(error)
                        if not keep_going:
                            stop.set()
            if read_memory:
                next_memory = started + intervals["memory_interval_ms"] / 1000
            stop.wait(max(0, intervals["cpu_interval_ms"] / 1000 - (time.monotonic() - started)))


def remove_container(name, record):
    try:
        docker("rm", "-f", name)
        return True
    except subprocess.CalledProcessError as error:
        if "No such container" in error.stderr:
            return True
        record("error", session=name, error=f"Container cleanup failed: {error.stderr}")
    except (OSError, subprocess.TimeoutExpired) as error:
        record("error", session=name, error=f"Container cleanup failed: {error}")
    return False


def run_action(command, log, timeout, session, stop):
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as process:
        try:
            pidfd = os.pidfd_open(process.pid)
        except ProcessLookupError:
            return process.wait(), False
        try:
            while process.poll() is None:
                if stop.is_set() or session["failure"]:
                    process.kill()
                    process.wait()
                    raise RuntimeError(session["failure"] or "Replay interrupted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    return -1, True  # mini kills the CLI; the container command may survive.
                select.select([pidfd], [], [], min(.2, remaining))
            return process.returncode, False
        finally:
            os.close(pidfd)


def replay(session, origin, clock, arguments, record, stop, output):
    trace = session["trace"]
    offset = trace["first"] - origin if arguments.starts == "recorded" else 0
    environment = trace["environment"]
    command = ["docker", "exec", "-w", environment["cwd"]]
    for key, value in environment.get("env", {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.append(session["name"])
    if session["numa_bind_helper"]:
        command.extend(["/opt/mini-numa-bind", str(session["memory_node"])])
    command.extend(environment["interpreter"])
    tracker = None

    def track(operation):
        tracker.stdin.write(json.dumps({"op": operation, "cgroup": str(session["cgroup"])}) + "\n")
        tracker.stdin.flush()
        if not select.select([tracker.stdout], [], [], 120)[0]:
            raise RuntimeError("Write tracker response timed out")
        line = tracker.stdout.readline()
        if not line:
            raise RuntimeError("Write tracker exited; inspect write-tracker.log")
        return json.loads(line)

    try:
        if arguments.write_pages:
            with (output / "write-tracker.log").open("w") as errors:
                tracker = subprocess.Popen(["sudo", "-n", sys.executable, str(Path(__file__).resolve()), "--write-tracker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
            if not select.select([tracker.stdout], [], [], 120)[0] or not tracker.stdout.readline():
                raise RuntimeError("Write tracker failed to initialize; inspect write-tracker.log")
        if arguments.perf:
            start_perf(session, arguments, clock, record, output)
        with (output / f"{session['name']}.log").open("w") as log:
            for index, batch in enumerate(trace["batches"]):
                if arguments.schedule == "immediate":
                    planned = time.monotonic()
                elif arguments.schedule == "timestamps" or index == 0:
                    planned = clock + (offset + batch["at"] - trace["first"]) * arguments.scale
                else:
                    planned = time.monotonic() + batch["gap"] * arguments.scale
                session["phase"] = "waiting"
                while planned > time.monotonic() and not stop.is_set() and not session["failure"]:
                    stop.wait(min(.2, planned - time.monotonic()))
                if session["failure"]:
                    raise RuntimeError(session["failure"])
                if stop.is_set():
                    return
                session["phase"] = "running"
                if index == 0:
                    record("session_start", session=session["name"], initial_resources=resources(session["cgroup"], target_node=session["memory_node"]))
                record("batch_start", session=session["name"], batch=index,
                       planned_s=planned - clock, lateness_s=max(0, time.monotonic() - planned))
                for action_index, action in enumerate(batch["actions"]):
                    if stop.is_set():
                        return
                    if tracker:
                        track("begin")
                    started = time.monotonic()
                    log.write(f"\n--- batch {index}, action {action_index} ---\n{action['command']}\n")
                    log.flush()
                    record("command_start", session=session["name"], batch=index, action=action_index, started_s=started - clock)
                    returncode, timed_out = run_action([*command, action["command"]], log,
                                                       environment.get("timeout", 60), session, stop)
                    if session["numa_bind_helper"] and returncode == 125:
                        raise RuntimeError("NUMA binding or Docker execution failed; inspect the command log")
                    if timed_out:
                        log.write("\n[Docker CLI timeout; preserving mini's command semantics]\n")
                    expected = batch["observations"][action_index]["returncode"] if batch["observations"] else None
                    duration = time.monotonic() - started
                    if tracker:
                        measured = time.monotonic()
                        record("write_pages", session=session["name"], batch=index, action=action_index,
                               **track("end"), collection_s=time.monotonic() - measured)
                    record("command_end", session=session["name"], batch=index, action=action_index,
                           started_s=started - clock, duration_s=duration,
                           returncode=returncode, expected_returncode=expected, timed_out=timed_out)
                record("batch_end", session=session["name"], batch=index)
            final = resources(session["cgroup"], target_node=session["memory_node"])
            if final["oom_kill"] or final["anon_other_bytes"] or final["swap_bytes"] or final["pids_limit_events"]:
                raise RuntimeError(f"OOM or NUMA/swap constraint failure: {final}")
            record("session_end", session=session["name"], final_resources=final)
    except Exception as error:
        record("error", session=session["name"], error=str(error))
        try:
            record("session_failed", session=session["name"], final_resources=resources(session["cgroup"], target_node=session["memory_node"]))
        except OSError:
            pass
        if not arguments.keep_going:
            stop.set()
            raise
    finally:
        try:
            if tracker:
                try:
                    tracker.stdin.write(json.dumps({"op": "close"}) + "\n")
                    tracker.stdin.flush()
                except BrokenPipeError:
                    pass
                tracker.stdin.close()
                tracker.wait(timeout=30)
        finally:
            session["phase"] = "done"
            try:
                stop_perf(session, record)
            finally:
                if not remove_container(session["name"], record):
                    stop.set()
                    raise RuntimeError(f"Could not clean up {session['name']}")


def replay_command(arguments, sources, output, memory_node, sequential=False):
    command = [sys.executable, str(Path(__file__).resolve()), *sources,
               "--config", str(arguments.config.resolve()), "--output", str(output),
               "--schedule", arguments.schedule, "--starts", "together", "--scale", str(arguments.scale),
               "--workload-cpus", arguments.workload_cpus, "--memory-node", str(memory_node), "--max-active", "1"]
    if arguments.perf:
        command += ["--perf", "--perf-bin", str(arguments.perf_bin.resolve()),
                    "--perf-interval-ms", str(arguments.perf_interval_ms)]
    if arguments.pull:
        command.append("--pull")
    if arguments.write_pages:
        command.append("--write-pages")
    if sequential:
        command.append("--sequential")
        if arguments.keep_going:
            command.append("--keep-going")
    return command


def compare_memory(arguments, traces, config):
    """Finish the complete DRAM suite before starting the complete CXL suite."""
    from analyze import compare_runs

    root = arguments.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config["numa_bind_helper"] = build_numa_helper(root)
    config["numa_bind_sha256"] = hashlib.sha256(Path(config["numa_bind_helper"]).read_bytes()).hexdigest()
    config["memory_policy"] = "Every payload uses verified MPOL_BIND to memory_nodes. cpuset allows 0,2 so non-movable kernel/runtime buffers can use node 0."
    references = sorted({task_image(trace, config) for trace in traces})
    image_ids = docker("image", "inspect", "--format", "{{.Id}}", *references).splitlines()
    if len(image_ids) != len(references):
        raise RuntimeError("Image inventory inspection returned incomplete results")
    config["image_ids"] = dict(zip(references, image_ids))
    arguments.config = root / "config.json"
    arguments.config.write_text(json.dumps(config, indent=2) + "\n")
    campaign = {"state": "running", "started_unix": time.time(), "memory_nodes": [0, 2],
                "schedule": "immediate", "traces": len(traces), "config": config,
                "code_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ("experiment.py", "analyze.py")}, "runs": []}
    path = root / "comparison.json"

    def save():
        temporary = path.with_suffix(".json.part")
        temporary.write_text(json.dumps(campaign, indent=2) + "\n")
        temporary.replace(path)

    save()
    try:
        for node in (0, 2):
            for name, digest in campaign["code_sha256"].items():
                if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest:
                    raise RuntimeError(f"Code changed during comparison: {name}")
            run = {"memory_node": node, "directory": f"node{node}", "state": "running", "started_unix": time.time()}
            campaign["runs"].append(run)
            save()
            print(f"START memory node {node}: {len(traces)} sequential traces without scheduling waits", flush=True)
            command = replay_command(arguments, [trace["source"] for trace in traces], root / run["directory"], node, sequential=True)
            with (root / "runner.log").open("a") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            run.update(state="complete" if result.returncode == 0 else "failed",
                       returncode=result.returncode, finished_unix=time.time())
            save()
            compare_runs(root)
            if result.returncode:
                raise RuntimeError(f"Memory node {node} suite failed; inspect {root / run['directory']}")
        campaign["state"] = "complete"
    except BaseException as error:
        if campaign["runs"] and campaign["runs"][-1]["state"] == "running":
            campaign["runs"][-1].update(state="interrupted", finished_unix=time.time())
        campaign.update(state="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        raise
    finally:
        campaign["updated_unix"] = time.time()
        save()
        compare_runs(root)


def sequential(arguments, traces, config):
    """Run each trace in a separate child; wait for its cleanup before the next."""
    from analyze import summarize, summarize_suite

    if len({trace["name"] for trace in traces}) != len(traces):
        raise ValueError("Sequential inputs must contain distinct traces")
    config["max_concurrent_sessions"] = 1
    root = arguments.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if arguments.write_pages:
        config["write_tracking"] = {
            "method": "cgroup-filtered successful write faults and supported buffered write_end PFNs",
            "page_size": 4096, "page_capacity": WRITE_PAGE_CAPACITY, "thp_disabled": True,
            "scope": "Observed write-triggered physical addresses; excludes unprobed kernel stores, DMA and unsupported I/O paths",
            "wait": "Boundary snapshot only; no LLM or model wait replay", "counts_freed_pages": True}
        config["numa_bind_helper"] = build_numa_helper(root, disable_thp=True)
        config["numa_bind_sha256"] = hashlib.sha256(Path(config["numa_bind_helper"]).read_bytes()).hexdigest()
        references = sorted({task_image(trace, config) for trace in traces})
        image_ids = docker("image", "inspect", "--format", "{{.Id}}", *references).splitlines()
        if len(image_ids) != len(references):
            raise RuntimeError("Incomplete local image inventory")
        config["image_ids"] = dict(zip(references, image_ids))
        arguments.config = root / "config.json"
        arguments.config.write_text(json.dumps(config, indent=2) + "\n")
    suite = {"state": "running", "started_unix": time.time(), "mode": "sequential",
             "schedule": arguments.schedule, "scale": arguments.scale, "perf": arguments.perf,
             "source_span_sum_s": sum(t["last"] - t["first"] for t in traces),
             "config": config, "attempts": [],
             "code_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ("experiment.py", "analyze.py")},
             "traces": [{"name": t["name"], "source": t["source"], "sha256": t["sha256"]} for t in traces]}

    def save():
        temporary = root / "suite.json.part"
        temporary.write_text(json.dumps(suite, indent=2) + "\n")
        temporary.replace(root / "suite.json")

    save()
    try:
        for index, trace in enumerate(traces, 1):
            for name, digest in suite["code_sha256"].items():
                if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest:
                    raise RuntimeError(f"Code changed during the experiment: {name}")
            destination = root / f"{index:03d}-{Path(trace['name']).name}"
            attempt = {"index": index, "trace": trace["name"], "directory": destination.name,
                       "state": "running", "started_unix": time.time()}
            suite["attempts"].append(attempt)
            save()
            summarize_suite(root)
            print(f"[{index}/{len(traces)}] START {trace['name']}", flush=True)
            command = replay_command(arguments, [trace["source"]], destination, int(config["memory_nodes"]))
            # The child fails on an invalid session; the suite records it and can continue.
            with (root / "runner.log").open("a") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            attempt.update(state="finished" if result.returncode == 0 else "failed",
                           returncode=result.returncode, finished_unix=time.time())
            save()
            if (destination / "manifest.json").exists():
                child_manifest = json.loads((destination / "manifest.json").read_text())
                run_id = child_manifest["config"]["budget_unit"].removeprefix("minireplay").removesuffix(".slice")
                remaining = docker("ps", "-a", "--filter", "name=mini-replay", "--format", "{{.Names}}")
                if any(run_id in name for name in remaining.splitlines()):
                    raise RuntimeError("Previous containers were not removed; refusing to start the next trace")
                summary = summarize(destination)
                attempt["infrastructure_passed"] = summary["infrastructure_passed"]
                attempt["mpki_valid"] = all(s.get("perf", {}).get("valid", False) for s in summary["sessions"].values())
            save()
            summarize_suite(root)
            if (root.parent / "comparison.json").exists():
                from analyze import compare_runs
                compare_runs(root.parent)
            print(f"[{index}/{len(traces)}] {attempt['state'].upper()} {trace['name']}", flush=True)
            if result.returncode and not arguments.keep_going:
                raise RuntimeError(f"Trace failed; inspect {destination}")
            if arguments.perf and not attempt.get("mpki_valid"):
                raise RuntimeError("Stopping suite because a required MPKI measurement failed")
        suite["state"] = "complete"
    except BaseException as error:
        if suite["attempts"] and suite["attempts"][-1]["state"] == "running":
            suite["attempts"][-1].update(state="interrupted", finished_unix=time.time())
        suite.update(state="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        raise
    finally:
        suite["updated_unix"] = time.time()
        save()
        summarize_suite(root)


def main():
    if sys.argv[1:] == ["--write-tracker"]:
        write_tracker()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="*", help="Native .traj.json paths or HTTPS URLs")
    parser.add_argument("--download-traces", action="store_true", help="Download and verify the pinned trace inventory, then exit")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "configs/node0.json")
    parser.add_argument("--output", type=Path, default=Path("harness/trace-replay/results"))
    parser.add_argument("--schedule", choices=("timestamps", "gaps", "immediate"), default="timestamps")
    parser.add_argument("--starts", choices=("recorded", "together"), default="recorded")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--setup", choices=("precreate", "on-arrival"), default="precreate")
    parser.add_argument("--max-active", type=int, help="Maximum resident containers; still subject to the memory budget")
    parser.add_argument("--workload-cpus", help="Shared workload CPU pool, restricted to the configured CPU NUMA node")
    parser.add_argument("--keep-going", action="store_true", help="Continue other sessions after an individual workload failure")
    parser.add_argument("--sequential", action="store_true", help="Execute each trace once, with one workload container at a time")
    parser.add_argument("--memory-node", type=int, choices=(0, 1, 2), help="Workload memory NUMA node; controller stays on the configured CPU node")
    parser.add_argument("--compare-memory", action="store_true", help="Run all traces on memory node 0, then node 2; requires immediate schedule")
    parser.add_argument("--perf", action="store_true", help="Measure cgroup LLC MPKI using Intel Sapphire/Emerald Rapids PMU events")
    parser.add_argument("--write-pages", action="store_true", help="Observe write-fault and buffered-write pages per tool call; DRAM only")
    parser.add_argument("--perf-bin", type=Path, default=Path("/usr/lib/linux-tools-6.8.0-146/perf"))
    parser.add_argument("--perf-interval-ms", type=int, default=1000)
    parser.add_argument("--pull", action="store_true", help="Download missing task images before measurement")
    parser.add_argument("--plan", action="store_true", help="Inspect timing; do not contact Docker or execute actions")
    arguments = parser.parse_args()
    if arguments.download_traces:
        if arguments.traces:
            parser.error("--download-traces does not accept replay inputs")
        data = Path(__file__).parent / "data"
        download_traces(data / "inventory.json", data / "native")
        return
    if not arguments.traces:
        parser.error("Provide trace inputs or use --download-traces")
    if not math.isfinite(arguments.scale) or arguments.scale <= 0:
        parser.error("--scale must be finite and positive")
    config = json.loads(arguments.config.read_text())
    if config.get("numa_bind_helper") and hashlib.sha256(Path(config["numa_bind_helper"]).read_bytes()).hexdigest() != config["numa_bind_sha256"]:
        parser.error("NUMA binding helper changed during the experiment")
    if arguments.memory_node is not None:
        config["memory_nodes"] = str(arguments.memory_node)
    if arguments.write_pages:
        if arguments.compare_memory or config["memory_nodes"] != str(config["numa_node"]) or config["numa_node"] not in (0, 1) or arguments.schedule != "immediate":
            parser.error("--write-pages requires local DRAM on CPU node 0 or 1 and --schedule immediate")
        if not arguments.sequential and not config.get("write_tracking"):
            parser.error("Start --write-pages with --sequential to prepare a pinned tracking configuration")
    if arguments.compare_memory:
        if config["numa_node"] != 0:
            parser.error("--compare-memory requires CPU node 0")
        if arguments.schedule != "immediate":
            parser.error("--compare-memory requires --schedule immediate")
        arguments.sequential = True
    if arguments.workload_cpus:
        config["workload_cpus"] = arguments.workload_cpus
    arguments.workload_cpus = config["workload_cpus"]
    if arguments.perf_interval_ms < 100:
        parser.error("Use a perf interval of at least 100 ms to limit measurement overhead")
    if arguments.perf:
        cpu = dict(line.split(":", 1) for line in Path("/proc/cpuinfo").read_text().split("\n\n")[0].splitlines() if ":" in line)
        cpu = {key.strip(): value.strip() for key, value in cpu.items()}
        if cpu.get("vendor_id") != "GenuineIntel" or int(cpu.get("model", 0)) not in (143, 207):
            parser.error("The raw retired-load event must be verified for this CPU model before using --perf")
    if arguments.max_active is not None:
        if arguments.max_active < 1:
            parser.error("--max-active must be positive")
        config["max_concurrent_sessions"] = arguments.max_active
    traces = [load_trace(source) for source in arguments.traces]
    origin = min(trace["first"] for trace in traces)
    windows = [(trace["first"] - origin if arguments.starts == "recorded" else 0,
                trace["last"] - trace["first"]) for trace in traces]
    events = sorted([(start, 1) for start, span in windows] + [(start + span, -1) for start, span in windows])
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    plan = {"schedule": arguments.schedule, "starts": arguments.starts, "scale": arguments.scale,
            "experiment_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "controller_pid": os.getpid(),
            "invocation": [sys.executable, *sys.argv],
            "source_origin_unix": origin, "observed_window_peak": peak,
            "observed_schedule_span_s": max(start + span for start, span in windows) * arguments.scale,
            "kernel": os.uname().release,
            "clock_assumption": "Recorded timestamps are comparable across the selected traces.",
            "container_setup": arguments.setup,
            "keep_going": arguments.keep_going,
            "setup_note": "On-arrival creates containers at the first recorded action; original creation times are unknown.",
            "timeout_semantics": "Like mini: kill the Docker CLI, record -1 and continue; its container command may survive.",
            "config": config, "traces": [{key: value for key, value in trace.items() if key != "batches"} for trace in traces]}
    if arguments.perf:
        plan["perf"] = {"events": PERF_EVENTS, "interval_ms": arguments.perf_interval_ms,
                        "scope": "Container cgroup on workload CPUs, user and kernel; excludes container setup/teardown",
                        "llc_mpki": "1000 * LONGEST_LAT_CACHE.MISS (event=0x2e,umask=0x41) / instructions",
                        "retired_load_l3_mpki": "1000 * MEM_LOAD_RETIRED.L3_MISS (event=0xd1,umask=0x20) / instructions",
                        "event_reference": "https://perfmon-events.intel.com/platforms/emeraldrapids/core-events/core/",
                        "minimum_counter_running_percent": 99,
                        "binary": str(arguments.perf_bin),
                        "version": subprocess.check_output([str(arguments.perf_bin), "--version"], text=True).strip()}
    if arguments.sequential:
        plan.update(mode="sequential", observed_window_peak=1,
                    observed_schedule_span_s=sum(t["last"] - t["first"] for t in traces) * arguments.scale,
                    clock_assumption="Reset each trace clock; no inter-trace wall-clock schedule is preserved.")
    if arguments.schedule == "immediate":
        plan["clock_assumption"] = "No timestamp/gap waits. Execute commands in recorded order as soon as the preceding command returns."
        plan["observed_schedule_span_s"] = None
    plan["latency_definition"] = "Host monotonic elapsed time from launching Docker exec to its completion; includes Docker control-plane overhead."
    if arguments.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    if not arguments.sequential and arguments.setup == "precreate" and len(traces) > config["max_concurrent_sessions"]:
        parser.error("Too many resident sessions for the configured budget")
    available = cpu_ids(Path(f"/sys/devices/system/node/node{config['numa_node']}/cpulist").read_text().strip())
    if config["numa_node"] not in (0, 1) or config["memory_nodes"] not in (str(config["numa_node"]), "2") or not cpu_ids(config["workload_cpus"]) <= available:
        parser.error("Workload CPUs must belong to CPU node 0 or 1; memory must be local DRAM or node 2")
    if not cpu_ids(config["node_cpu_pool"]) <= available:
        parser.error("The CPU pool must belong to the configured CPU node")
    if not cpu_ids(config["controller_cpus"]) <= available:
        parser.error("Controller CPUs must belong to the configured CPU node")
    if cpu_ids(config["controller_cpus"]) & cpu_ids(config["workload_cpus"]):
        parser.error("Controller and workload CPUs must be disjoint")
    meminfo = Path(f"/sys/devices/system/node/node{config['memory_nodes']}/meminfo").read_text().splitlines()
    node_memory = {line.split()[-3].rstrip(":"): int(line.split()[-2]) * 1024 for line in meminfo if line.endswith(" kB")}
    free_bytes = node_memory["MemFree"]
    # Reserve headroom; include clean inactive cache and half of active cache/slab.
    reclaimable = max(0, node_memory["Inactive(file)"] + node_memory["Active(file)"] // 2
                      - node_memory["Shmem"] - node_memory["Dirty"] - node_memory["Writeback"])
    safe_available = free_bytes + reclaimable + node_memory.get("SReclaimable", 0) // 2
    budget_sessions = min(len(traces), config["max_concurrent_sessions"])
    budget_fraction = config.get("memory_budget_fraction", .8)
    if not 0 < budget_fraction <= .9:
        parser.error("memory_budget_fraction must be positive and at most 0.9")
    config["aggregate_memory_limit_bytes"] = min(config["aggregate_memory_limit_bytes"], int(safe_available * budget_fraction) // 4096 * 4096)
    if config["memory_limit_bytes"] > config["aggregate_memory_limit_bytes"]:
        parser.error("Shared budget is smaller than one session's limit")
    plan["memory_node_free_bytes_at_check"] = free_bytes
    plan["memory_node_available_budget_estimate_bytes"] = safe_available
    plan["memory_budget_method"] = f"{budget_fraction:.0%} of (MemFree + inactive file + half active file - shmem/dirty/writeback + half SReclaimable); estimate, not kernel MemAvailable"
    plan["host_pressure_at_start"] = {kind: Path(f"/proc/pressure/{kind}").read_text() for kind in ("cpu", "memory", "io")}
    if not os.environ.get("MINI_REPLAY_BOUND"):
        os.execvpe("numactl", ["numactl", f"--physcpubind={config['controller_cpus']}", f"--membind={config['numa_node']}",
                              sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                   {**os.environ, "MINI_REPLAY_BOUND": "1"})
    if arguments.compare_memory:
        compare_memory(arguments, traces, config)
        return
    if arguments.sequential:
        sequential(arguments, traces, config)
        return
    if docker("info", "--format", "{{.CgroupVersion}}") != "2":
        raise RuntimeError("cgroup v2 is required")
    subprocess.run(["sudo", "-n", "true"], check=True)
    images = {}
    for trace in traces:
        image = task_image(trace, config)
        if image not in images:
            if arguments.pull:
                docker("pull", image, timeout=1800)
            images[image] = config.get("image_ids", {}).get(image) or docker("image", "inspect", "--format", "{{.Id}}", image)
    run_id = uuid.uuid4().hex
    config["budget_unit"] = f"minireplay{run_id}.slice"
    budget_name = f"mini-replay-budget-{run_id}"
    arguments.output.mkdir(parents=True, exist_ok=False)
    (arguments.output / "manifest.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    sessions, names = [], []
    stop, lock = threading.Event(), threading.Lock()
    executor = sampler = budget = None
    with (arguments.output / "events.jsonl").open("w") as event_log:
        clock = time.monotonic()

        def record(kind, **values):
            with lock:
                event_log.write(json.dumps({"event": kind, "elapsed_s": time.monotonic() - clock, **values}) + "\n")
                event_log.flush()

        try:
            budget = create_budget(config, budget_name, next(iter(images.values())))
            record("budget_ready", unit=config["budget_unit"], cgroup=str(budget), resources=resources(budget))
            names = [f"mini-replay-{run_id}-{index}" for index in range(len(traces))]
            if arguments.setup == "precreate":
                for trace, name in zip(traces, names):
                    session = prepare(trace, config, name, False, images[task_image(trace, config)])
                    sessions.append(session)
                    images[task_image(trace, config)] = session["image_id"]
            clock = time.monotonic()
            record("budget_start", resources=resources(budget))
            record("replay_clock_start", wall_time_unix=time.time(), sessions=[{"session": name, "trace": trace["name"],
                   "image_id": images[task_image(trace, config)]} for trace, name in zip(traces, names)])
            sampler = threading.Thread(target=sample, args=(sessions, clock, config["sampling"],
                                                            stop, record, arguments.output / "metrics.csv", arguments.keep_going))
            sampler.start()
            executor = ThreadPoolExecutor(max_workers=budget_sessions)
            if arguments.setup == "precreate":
                futures = [executor.submit(replay, s, origin, clock, arguments, record, stop, arguments.output) for s in sessions]
            else:
                def start_session(trace, name, planned):
                    if stop.is_set():
                        return
                    record("container_start", session=name, admission_delay_s=max(0, time.monotonic() - planned))
                    try:
                        session = prepare(trace, config, name, False, images[task_image(trace, config)])
                    except Exception as error:
                        record("error", session=name, error=str(error))
                        if not remove_container(name, record):
                            stop.set()
                        if not arguments.keep_going:
                            stop.set()
                            raise
                        return
                    sessions.append(session)
                    record("container_ready", session=name, image_id=session["image_id"])
                    replay(session, origin, clock, arguments, record, stop, arguments.output)

                futures = []
                for trace, name in sorted(zip(traces, names), key=lambda pair: pair[0]["first"]):
                    offset = trace["first"] - origin if arguments.starts == "recorded" else 0
                    planned = clock + offset * arguments.scale
                    if stop.wait(max(0, planned - time.monotonic())):
                        break
                    record("session_queued", session=name, planned_s=planned - clock)
                    futures.append(executor.submit(start_session, trace, name, planned))
            for future in as_completed(futures):
                future.result()
            if stop.is_set():
                raise RuntimeError("Measurement or replay was interrupted; inspect events.jsonl")
        except Exception as error:
            record("error", error=str(error), details=getattr(error, "stderr", None))
            stop.set()
            raise
        finally:
            stop.set()
            if sampler:
                sampler.join()
            cleanup_ok = True
            for name in names:
                if not remove_container(name, record):
                    cleanup_ok = False
            if executor:
                executor.shutdown(wait=True)
            if budget:
                record("budget_end", resources=resources(budget))
            if not remove_container(budget_name, record):
                cleanup_ok = False
            try:
                systemd("stop", config["budget_unit"])
                systemd("revert", config["budget_unit"])
            except subprocess.CalledProcessError as error:
                record("error", error=f"Budget cleanup failed: {error.stderr}")
                cleanup_ok = False
            if not cleanup_ok:
                raise RuntimeError("Container cleanup failed; inspect events.jsonl")
    print(f"Results: {arguments.output}")


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupt)
    main()
