#!/usr/bin/env python3
"""Fill one node with agent sandboxes (Firecracker microVMs) and record their memory and CPU (2026-11-18 plan,
docs/results/sandbox-footprint-20261118).

The node is a set of CPUs (default: every CPU but the controller's) and a memory budget (the cgroup this process runs
in; start it with `systemd-run --user --scope -p MemoryMax=...G -p MemorySwapMax=0`). Sandboxes are added along a ramp
(`--ramp N:SECONDS,...`, N = target live sandboxes) and each finished sandbox is replaced by a new trace at once.
A sandbox (or a whole GRPO group) is admitted only while the budget has room (`--reserve-gib` plus `--admit-gib` per
new sandbox, counting file cache as reclaimable), so when memory binds the live count stops below the target: that
count is the memory ceiling. vCPU and VMM threads float over all node CPUs, as on a normal host.

Outputs in RUN (= --out or .local/runs/<time>-<workload>):
  run.json           configuration, host, CPUs, budget
  sandboxes.jsonl    one line per sandbox: trace, start/end, command times and return codes, guest memory samples
                     (SWE_MEM: uptime, MemFree, Cached, Buffers, AnonPages, Shmem, Dirty KiB, disk sectors)
  vcpu.csv           every --sample-ms: t_ns, vm, run_ns and runqueue wait_ns of its vCPU threads (deltas, nonzero only)
  vm.csv             every second: t_ns, vm, VMM-thread run_ns delta, RssAnon and RssFile KiB of the VMM process
  node.csv           every second: live, target, cgroup memory (current, anon, file, shmem, kernel), estimated room,
                     host MemAvailable, node CPU busy (CPUs of the node), cgroup PSI memory some/full, OOM kills, and
                     cumulative refaults (file, anon), major faults, pages scanned and stolen, swap in use (thrashing signs),
                     sandboxes waiting for room (admit_waiting > 0 = memory ceiling)
  steps.jsonl        ramp steps and GRPO group steps
  host.csv           sandbox.host_sampler (PSI, vmstat, disk, network)
  live.json          live VMs for memscan.py (pid, guest MiB, sampled flag); memscan writes RUN/memscan/
"""

import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import random
import resource
import shutil
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
LATEST = HERE.parents[1]
OLD = Path("/home/ijkim/agent-sandbox-cxl")  # images, Firecracker and guest kernel of the earlier runs (read only)
sys.path.insert(0, str(LATEST / "experiments/contention"))
import sandbox as sb  # noqa: E402

sb.FIRECRACKER = OLD / "experiments/contention/.local/bin/firecracker"
sb.KERNEL = OLD / ".local/microvm/vmlinux-6.1.188"
IMAGE_ROOTS = [HERE / ".local/images", OLD / "experiments/contention/.local/images"]
WORKLOADS = {  # candidates file, guest MiB default, network
    "rl": (OLD / "experiments/contention/candidates-plan.json", None, True),
    "swe": (OLD / "experiments/contention/candidates-all.json", None, True),
    "ci": (HERE / "candidates-ci.json", None, False),
}
GIB = 1 << 30


def image_dir(entry):
    key = entry.get("image_key", entry["name"])
    for root in IMAGE_ROOTS:
        if (root / key / "image.json").exists():
            return root / key
    return IMAGE_ROOTS[-1] / key


sb.image_dir = image_dir
_run = sb.run


def run_no_journal(command, **kwargs):
    """Per-VM job and writable disks without an ext4 journal: they are thrown away with the VM, and the journals
    (32 + 64 MiB per VM) filled / at several hundred live sandboxes (11-18 22:45)."""
    if command and command[0] == "mkfs.ext4" and "-O" not in command:
        command = [command[0], "-O", "^has_journal", *command[1:]]
    return _run(command, **kwargs)


sb.run = run_no_journal


class FakeSandbox:
    """--dry-run stand-in: a `sleep` process per sandbox, so the ramp, admission and samplers run without VMs."""

    def __init__(self, entry, directory, node, slot, args):
        self.entry, self.directory, self.args = entry, directory, args
        self.markers, self.complete, self.process = [], threading.Event(), None

    def start(self):
        self.directory.mkdir(parents=True)
        self.process = subprocess.Popen(["sleep", str(random.uniform(5, 20))])
        self.started_ns = time.monotonic_ns()
        threading.Thread(target=lambda: (self.process.wait(), self.markers.append((time.monotonic_ns(), "SWE_COMPLETE")),
                                         self.complete.set()), daemon=True).start()

    def pin_vcpus(self):
        return {}

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait()

    def result(self):
        return {"trace": self.entry["name"], "completed": bool(self.markers), "commands": []}


def cpu_list(spec):
    return sorted(sb.replay.cpu_ids(spec))


def cgroup_dir():
    return Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().split("::", 1)[1].strip().lstrip("/")


def read_kv(path):
    try:
        return dict(line.split()[:2] for line in path.read_text().splitlines())
    except OSError:
        return {}


class Budget:
    """Memory room of the run's cgroup: budget minus what cannot be reclaimed (everything but file cache)."""

    def __init__(self, gib):
        self.cg = cgroup_dir()
        limit = (self.cg / "memory.max").read_text().strip()
        self.limit = int(limit) if limit != "max" else None
        self.budget = int(gib * GIB) if gib else self.limit
        if not self.budget:
            raise SystemExit("No memory budget: run under a scope with MemoryMax or pass --budget-gib")

    def state(self):
        stat = {k: int(v) for k, v in read_kv(self.cg / "memory.stat").items()}
        current = int((self.cg / "memory.current").read_text())
        file = stat.get("file", 0)
        kernel = stat.get("kernel", stat.get("slab", 0) + stat.get("kernel_stack", 0) + stat.get("pagetables", 0))
        return {"current": current, "anon": stat.get("anon", 0), "file": file, "shmem": stat.get("shmem", 0),
                "kernel": kernel, "room": self.budget - (current - file + stat.get("shmem", 0))}


MEM_NODES = []  # NUMA nodes with CPUs (node 2 on bud05 is CXL memory without CPUs; sandboxes never use it)


def host_available():
    """Free + file pages (minus shmem) + reclaimable slab of the CPU memory nodes, in bytes: what the node can still
    give."""
    total = 0
    for n in MEM_NODES:
        f = {l.split()[2].rstrip(":"): int(l.split()[3]) for l in open(f"/sys/devices/system/node/node{n}/meminfo")}
        total += f["MemFree"] + f["FilePages"] - f["Shmem"] + f.get("SReclaimable", 0)
    return total * 1024


def parse_ramp(spec):
    steps = []
    for part in spec.split(","):
        n, _, s = part.partition(":")
        steps.append((int(n), float(s)))
    return steps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workload", choices=sorted(WORKLOADS))
    ap.add_argument("--candidates", type=Path, help="Override the workload's candidates file")
    ap.add_argument("--names", nargs="*", help="Only these traces")
    ap.add_argument("--ramp", required=True, help="TARGET:SECONDS,... live sandboxes per step, e.g. 96:600,192:600")
    ap.add_argument("--controller-cpus", default="0-3,64-67")
    ap.add_argument("--cpus", help="Node CPUs (default: all online CPUs but the controller's)")
    ap.add_argument("--budget-gib", type=float, help="Memory budget (default: the scope's MemoryMax)")
    ap.add_argument("--reserve-gib", type=float, default=12.0, help="Room kept free in the budget")
    ap.add_argument("--admit-gib", type=float, default=0.5, help="Room asked per sandbox being admitted")
    ap.add_argument("--no-admission", action="store_true",
                    help="Take every sandbox regardless of room (run past the budget; pair with a swap limit)")
    ap.add_argument("--mems", default="0",
                    help="NUMA nodes for guest memory. Node 1 is not used: another user's processes are confined to it "
                         "(cpuset mems=1), and filling it made the kernel OOM-kill sandboxes on 11-18 21:39")
    ap.add_argument("--protect-nodes", default="1", help="Stop the run if free + file memory of these nodes falls below "
                                                         "--protect-gib (other users live there)")
    ap.add_argument("--protect-gib", type=float, default=4.0)
    ap.add_argument("--disk-floor-gib", type=float, default=15.0,
                    help="Stop the run if the run directory's file system has less free space (shared with others)")
    ap.add_argument("--host-floor-gib", type=float, default=3.0,
                    help="Stop the run if free + file memory of the CPU memory nodes falls below")
    ap.add_argument("--group-size", type=int, default=0, help="GRPO: G rollouts of one task start together")
    ap.add_argument("--group-jitter-s", type=float, default=2.0)
    ap.add_argument("--gap-scale", type=float, default=1.0, help="Recorded LLM wait multiplier")
    ap.add_argument("--memory-mib", type=int, help="Guest RAM (default: per trace, else 2048)")
    ap.add_argument("--vcpus", type=int, default=1)
    ap.add_argument("--upper-gib", type=int, default=8)
    ap.add_argument("--base-pmem", action="store_true", help="Base image as DAX pmem (one host page cache copy)")
    ap.add_argument("--no-network", dest="network", action="store_false")
    ap.add_argument("--mem-sample-s", type=float, default=2.0, help="Guest /proc/meminfo sampling (SWE_MEM lines)")
    ap.add_argument("--sample-ms", type=float, default=100.0, help="vCPU run/wait sampling interval")
    ap.add_argument("--stagger-s", type=float, default=90.0, help="New slots of a step start within this window")
    ap.add_argument("--scan", type=int, default=32, help="Sandboxes scanned by memscan at any time (0 = no memscan)")
    ap.add_argument("--scan-all-below", type=int, default=48, help="Scan every sandbox while the target is <= this")
    ap.add_argument("--memscan-args", default="", help="Extra memscan.py arguments")
    ap.add_argument("--disk", default="sda", help="Block device of the run directory (host.csv disk columns)")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep-vm-dirs", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="sleep processes instead of VMs (logic test)")
    args = ap.parse_args()
    # Several files per sandbox (serial pipe and log, disks); a systemd service starts with a soft limit of 1024,
    # which stopped the CI run at ~400 live sandboxes (11-18 22:54).
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))

    candidates_path, default_mib, network = WORKLOADS[args.workload]
    candidates_path = args.candidates or candidates_path
    args.network = args.network and network
    args.ls_resctrl, args.memory_mib = None, args.memory_mib or default_mib
    entries = []
    for e in json.loads(candidates_path.read_text())["traces"]:
        trace = Path(e["trace"])
        if not trace.is_absolute():  # 264-trace originals live in the old checkout (git-ignored data)
            trace = next((r / trace for r in (LATEST, OLD) if (r / trace).exists()), LATEST / trace)
        if trace.exists() and (image_dir(e) / "image.json").exists() and (not args.names or e["name"] in args.names):
            entries.append({**e, "trace": str(trace)})
    if not entries:
        raise SystemExit(f"No traces with built images in {candidates_path}")
    groups = collections.defaultdict(list)
    for e in entries:
        groups[e.get("group")].append(e)
    real_groups = [sorted(v, key=lambda e: e["name"]) for g, v in sorted(groups.items(), key=lambda kv: str(kv[0]))
                   if g and len(v) == args.group_size]
    if args.group_size and not real_groups:
        print(f"No complete groups of {args.group_size}; a group replicates one trace", flush=True)

    controller = set(cpu_list(args.controller_cpus))
    online = set(cpu_list(Path("/sys/devices/system/cpu/online").read_text().strip()))
    cpus = set(cpu_list(args.cpus)) if args.cpus else online - controller
    if cpus & controller or not cpus <= online:
        raise SystemExit(f"Node CPUs must be online and outside the controller: {sorted(cpus - online)}")
    os.sched_setaffinity(0, controller)
    spec = ",".join(map(str, sorted(cpus)))
    node = {"index": 0, "memory_node": args.mems,
            "vmm_cpus": spec, "vcpu_cpus": cpus, "cores": cpus, "siblings": set()}
    MEM_NODES[:] = [int(n) for n in node["memory_node"].split(",")]
    budget = Budget(args.budget_gib)
    ramp = parse_ramp(args.ramp)
    tag = f"{args.workload}-x{args.gap_scale:g}" + (f"-g{args.group_size}" if args.group_size else "")
    directory = args.out or HERE / ".local/runs" / datetime.datetime.now(datetime.timezone.utc).strftime(
        f"%Y%m%dT%H%M%SZ-{tag}")
    directory = directory if directory.is_absolute() else Path.cwd() / directory
    (directory / "vm").mkdir(parents=True, exist_ok=False)
    if args.dry_run:
        sb.Sandbox, args.network = FakeSandbox, False
    if args.network:
        sb.network_up()
    if not args.dry_run:
        sb.overlay_initrd(args.base_pmem)
    sb.save_json(directory / "run.json", {
        "kind": "footprint-fill", "workload": args.workload, "candidates": str(candidates_path),
        "traces": len(entries), "groups": len(real_groups), "ramp": ramp, "group_size": args.group_size,
        "gap_scale": args.gap_scale, "cpus": sorted(cpus), "controller_cpus": sorted(controller),
        "physical_cores": len({Path(f"/sys/devices/system/cpu/cpu{c}/topology/core_id").read_text().strip()
                               + "/" + Path(f"/sys/devices/system/cpu/cpu{c}/topology/physical_package_id").read_text().strip()
                               for c in cpus}),
        "memory_nodes": node["memory_node"], "protect_nodes": args.protect_nodes, "budget_bytes": budget.budget, "cgroup_limit": budget.limit,
        "cgroup": str(budget.cg), "reserve_gib": args.reserve_gib, "admit_gib": args.admit_gib,
        "admission": not args.no_admission,
        "swap_max": (budget.cg / "memory.swap.max").read_text().strip() if (budget.cg / "memory.swap.max").exists() else None,
        "memory_mib": args.memory_mib, "vcpus": args.vcpus, "base_pmem": args.base_pmem, "network": args.network,
        "mem_sample_s": args.mem_sample_s, "sample_ms": args.sample_ms, "scan": args.scan, "seed": args.seed,
        "smt": Path("/sys/devices/system/cpu/smt/active").read_text().strip(),
        "governors": sorted({Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor").read_text().strip()
                             for c in cpus if Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq").exists()}),
        "vm_disks": "ext4 without journal (from 11-18 22:45)", "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
        "ksm_run": Path("/sys/kernel/mm/ksm/run").read_text().strip(),
        "thp": Path("/sys/kernel/mm/transparent_hugepage/enabled").read_text().strip(),
        "host_kernel": os.uname().release, "firecracker": str(sb.FIRECRACKER), "guest_kernel": str(sb.KERNEL),
        "host_mem_available": host_available()})

    lock, stop = threading.Lock(), threading.Event()
    live = {}  # vm -> Sandbox
    meta = {}  # vm -> dict for live.json
    target = [0]
    pending = [0]  # sandboxes admitted but not yet booted far enough to show their memory
    waiting = [0]  # slots blocked in admit() because the budget has no room (memory ceiling)
    counter = [0]
    free_net = list(range(1999, -1, -1))  # network identity of live sandboxes (TAP ctv<N>, 10.200.<102+N/250>.x)
    scanned = set()
    events = (directory / "sandboxes.jsonl").open("w")
    steps = (directory / "steps.jsonl").open("w")
    started = time.monotonic()
    deadline = started + sum(s for _, s in ramp)

    def log_step(record):
        with lock:
            steps.write(json.dumps(record) + "\n")
            steps.flush()

    def admit(count):
        """Block until the budget has room for `count` new sandboxes; False when the run ends first.
        With --no-admission every sandbox is taken at once (overcommit past the budget: thrashing runs)."""
        if args.no_admission:
            with lock:
                pending[0] += count
            return not stop.is_set() and time.monotonic() < deadline
        need = (args.reserve_gib + args.admit_gib * count) * GIB
        blocked = False
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                with lock:
                    room = budget.state()["room"] - pending[0] * args.admit_gib * GIB
                    if room >= need:
                        pending[0] += count
                        return True
                    if not blocked:
                        blocked = True
                        waiting[0] += count
                stop.wait(1.0)
            return False
        finally:
            if blocked:
                with lock:
                    waiting[0] -= count

    def run_sandbox(entry, delay, group=None):
        """One sandbox from boot to its last command; its admission share (`pending`) is released once it has
        booted (its memory then shows in the cgroup) or failed."""
        held = [True]

        def release():
            if held[0]:
                held[0] = False
                with lock:
                    pending[0] -= 1

        if delay and stop.wait(delay):
            release()
            return None
        with lock:
            vm = counter[0]
            counter[0] += 1
            net = free_net.pop()
        box = sb.Sandbox(entry, directory / "vm" / f"{vm:05d}", node, net, args)
        box.net_id = net
        error, sampled = None, False
        try:
            box.start()
            with lock:
                live[vm] = box
                cap = max(args.scan, target[0]) if target[0] <= args.scan_all_below else args.scan
                sampled = bool(args.scan) and len(scanned) < cap
                if sampled:
                    scanned.add(vm)
                meta[vm] = {"pid": box.process.pid, "mem_mib": args.memory_mib or entry.get("memory_mib", 2048),
                            "sampled": sampled, "trace": entry["name"], "group": group, "start_ns": box.started_ns}
            while not box.complete.wait(.5):
                if held[0] and (box.markers or time.monotonic_ns() - box.started_ns > 20e9):
                    release()
                if time.monotonic() >= deadline or stop.is_set():
                    break
        except Exception as exc:
            error = str(exc)
        finally:
            release()
            with lock:
                live.pop(vm, None)
                meta.pop(vm, None)
                scanned.discard(vm)
            box.stop()
            with lock:
                free_net.append(net)
        try:
            record = box.result() if box.process else {"trace": entry["name"]}
        except Exception as exc:
            record = {"trace": entry["name"], "result_error": str(exc)}
        record.update({"vm": vm, "group": group, "end_ns": time.monotonic_ns(), "error": error, "sampled": sampled,
                       "deadline_stop": time.monotonic() >= deadline or stop.is_set()})
        with lock:
            events.write(json.dumps(record) + "\n")
            events.flush()
        if not args.keep_vm_dirs:
            shutil.rmtree(box.directory, ignore_errors=True)
        return record

    failures = collections.deque(maxlen=50)

    def check_failure(record):
        """Back off after a failed sandbox; stop the run when most recent sandboxes failed to start."""
        failed = bool(record and record.get("error") and not record.get("commands"))
        with lock:
            failures.append(failed)
            bad = len(failures) >= 20 and sum(failures) > 0.8 * len(failures)
        if bad and not stop.is_set():
            print(f"stopping: {sum(failures)} of the last {len(failures)} sandboxes failed: {record.get('error')}",
                  flush=True)
            stop.set()
        if failed:
            stop.wait(2.0)

    def slot_loop(slot, first_delay):
        rng = random.Random(f"{args.seed}-{slot}")
        if stop.wait(first_delay):
            return
        while not stop.is_set() and time.monotonic() < deadline:
            if not admit(1):
                return
            check_failure(run_sandbox(rng.choice(entries), 0.0))

    def group_loop(g, first_delay):
        rng = random.Random(f"{args.seed}-group-{g}")
        if stop.wait(first_delay):
            return
        step = 0
        while not stop.is_set() and time.monotonic() < deadline:
            chosen = rng.choice(real_groups) if real_groups else [rng.choice(entries)] * args.group_size
            if not admit(len(chosen)):
                return
            delays = [rng.uniform(0, args.group_jitter_s) for _ in chosen]
            results = [None] * len(chosen)
            name = f"g{g}s{step}"

            def member(i):
                results[i] = run_sandbox(chosen[i], delays[i], name)

            begin = time.monotonic_ns()
            workers = [threading.Thread(target=member, args=(i,)) for i in range(len(chosen))]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
            for r in results:
                check_failure(r)
            log_step({"kind": "group", "slot": g, "step": step, "group": name, "rl_group": chosen[0].get("group"),
                      "traces": [e["name"] for e in chosen], "begin_ns": begin, "end_ns": time.monotonic_ns(),
                      "member_end_ns": [r["end_ns"] for r in results if r],
                      "completed": sum(bool(r and r.get("completed")) for r in results)})
            step += 1

    def write_live():
        with lock:
            data = {"t_ns": time.monotonic_ns(), "vms": {str(k): v for k, v in meta.items()}}
        tmp = directory / "live.json.tmp"
        tmp.write_text(json.dumps(data))
        os.replace(tmp, directory / "live.json")

    def sampler():
        """Never dies: an error in one round is logged and the next round runs (the safety checks live here)."""
        while not stop.is_set():
            try:
                sampler_rounds()
            except Exception as exc:  # noqa: BLE001
                print(f"sampler error, restarting it: {exc!r}", flush=True)
                stop.wait(1.0)

    def sampler_rounds():
        """vCPU run/wait every --sample-ms; per-VM VMM threads and RSS, node memory and CPU every second."""
        prev, prev_vmm = {}, {}
        interval = args.sample_ms / 1000
        every = max(1, round(1.0 / interval))
        tick = 0
        stat_cpus = {f"cpu{c}" for c in cpus}
        cg = budget.cg

        def node_busy():
            busy = total = 0
            for line in open("/proc/stat"):
                f = line.split()
                if f[0] in stat_cpus:
                    v = list(map(int, f[1:9]))
                    total += sum(v)
                    busy += sum(v) - v[3] - v[4]
            return busy, total

        last_busy = node_busy()
        with (directory / "vcpu.csv").open("a") as vs, (directory / "vm.csv").open("a") as ms, \
                (directory / "node.csv").open("a") as ns:  # append: the sampler restarts after an error
            if not vs.tell():
                vs.write("t_ns,vm,run_ns,wait_ns\n")
            if not ms.tell():
                ms.write("t_ns,vm,vmm_run_ns,rss_anon_kib,rss_file_kib\n")
            if not ns.tell():
                ns.write("t_ns,live,target,pending,cg_current,cg_anon,cg_file,cg_shmem,cg_kernel,room,host_available,"
                     "busy_jiffies,total_jiffies,psi_mem_some_us,psi_mem_full_us,oom_kill,refault_file,refault_anon,"
                     "pgmajfault,pgscan,pgsteal,swap_current,admit_waiting\n")
            target_t = time.monotonic()
            while not stop.is_set():
                target_t += interval
                delay = target_t - time.monotonic()
                if delay > 0:
                    stop.wait(delay)
                else:
                    target_t = time.monotonic()
                now = time.monotonic_ns()
                with lock:
                    boxes = list(live.items())
                for vm, box in boxes:
                    try:  # a sandbox can exit between listing and reading its /proc entries
                        vcpus = list(box.pin_vcpus().items())
                    except OSError:
                        continue
                    for tid, path in vcpus:
                        try:
                            r, w, _ = map(int, open(path).read().split())
                        except (OSError, ValueError):
                            continue
                        o = prev.get(tid, (r, w))
                        prev[tid] = (r, w)
                        if r - o[0] or w - o[1]:
                            vs.write(f"{now},{vm},{r - o[0]},{w - o[1]}\n")
                tick += 1
                if tick % every:
                    continue
                for vm, box in boxes:
                    pid = box.process.pid if box.process else None
                    if not pid:
                        continue
                    run_vmm = 0
                    try:
                        stats = sb.thread_stats(pid)
                    except OSError:
                        continue
                    for tid, comm, r, _ in stats:
                        if not comm.startswith("fc_vcpu"):
                            run_vmm += r
                    o = prev_vmm.get(vm, run_vmm)
                    prev_vmm[vm] = run_vmm
                    anon, mapped = sb.vm_rss_parts_kib(pid)
                    ms.write(f"{now},{vm},{run_vmm - o},{anon},{mapped}\n")
                st = budget.state()
                busy = node_busy()
                psi = {l.split()[0]: l for l in (cg / "memory.pressure").read_text().splitlines()} \
                    if (cg / "memory.pressure").exists() else {}
                total_us = [psi[k].rsplit("total=", 1)[1] if k in psi else "" for k in ("some", "full")]
                oom = read_kv(cg / "memory.events").get("oom_kill", "")
                cs = read_kv(cg / "memory.stat")  # cumulative: thrashing shows as refaults and major faults
                thrash = ",".join(cs.get(k, "") for k in ("workingset_refault_file", "workingset_refault_anon",
                                                          "pgmajfault", "pgscan", "pgsteal"))
                try:
                    thrash += "," + (cg / "memory.swap.current").read_text().strip()
                except OSError:
                    thrash += ","
                with lock:
                    n_live, tgt, pend = len(live), target[0], pending[0]
                ns.write(f"{now},{n_live},{tgt},{pend},{st['current']},{st['anon']},{st['file']},{st['shmem']},"
                         f"{st['kernel']},{st['room']},{host_available()},{busy[0]},{busy[1]},"
                         f"{total_us[0]},{total_us[1]},{oom},{thrash},{waiting[0]}\n")
                vs.flush()
                ms.flush()
                ns.flush()
                write_live()
                if host_available() < args.host_floor_gib * GIB:
                    print("host MemAvailable below the floor; stopping", flush=True)
                    stop.set()
                fs = os.statvfs(directory)
                if fs.f_bavail * fs.f_frsize < args.disk_floor_gib * GIB:
                    print(f"run file system below {args.disk_floor_gib} GiB free; stopping", flush=True)
                    stop.set()
                for n in (int(x) for x in args.protect_nodes.split(",") if x):
                    if sb.numa_reclaimable_kib(n)[0] * 1024 < args.protect_gib * GIB:
                        print(f"node {n} free + file below {args.protect_gib} GiB (other users); stopping", flush=True)
                        stop.set()

    sampling = threading.Thread(target=sampler, daemon=True)
    sampling.start()
    host_stop = threading.Event()
    host_log = threading.Thread(target=sb.host_sampler, args=(directory, host_stop, cpus, args.disk), daemon=True)
    host_log.start()
    memscan = None
    if args.scan:
        write_live()
        log = (directory / "memscan.log").open("w")
        memscan = subprocess.Popen(["sudo", "-n", sys.executable, str(HERE / "memscan.py"), "--run", str(directory),
                                    "--cpus", args.controller_cpus, *args.memscan_args.split()],
                                   stdout=log, stderr=subprocess.STDOUT)

    threads = []
    unit = args.group_size or 1
    rng = random.Random(f"{args.seed}-ramp")
    print(f"fill {tag}: {len(entries)} traces, {len(cpus)} CPUs, budget {budget.budget / GIB:.1f} GiB, ramp {ramp} "
          f"-> {directory}", flush=True)
    try:
        for n, seconds in ramp:
            step_end = time.monotonic() + seconds
            with lock:
                old = target[0]
                target[0] = max(old, n)
            log_step({"kind": "ramp", "target": target[0], "t_ns": time.monotonic_ns(), "seconds": seconds})
            for k in range(old // unit, target[0] // unit):
                fn = group_loop if args.group_size else slot_loop
                t = threading.Thread(target=fn, args=(k, rng.uniform(0, args.stagger_s)), daemon=True)
                t.start()
                threads.append(t)
            while not stop.is_set() and time.monotonic() < step_end:
                stop.wait(min(5.0, max(0.0, step_end - time.monotonic())))
            if stop.is_set():
                break
    except KeyboardInterrupt:
        print("interrupted", flush=True)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=60)
        if memscan:
            (directory / "memscan.stop").touch()
            try:
                memscan.wait(timeout=60)
            except subprocess.TimeoutExpired:
                subprocess.run(["sudo", "-n", "kill", "-TERM", str(memscan.pid)])
        host_stop.set()
        sampling.join(timeout=10)
        events.close()
        steps.close()
        sb.save_json(directory / "done.json", {"wall_s": round(time.monotonic() - started, 1),
                                               "sandboxes": counter[0], "left_running": len(live)})
    print(f"done: {counter[0]} sandboxes -> {directory}", flush=True)


if __name__ == "__main__":
    main()
