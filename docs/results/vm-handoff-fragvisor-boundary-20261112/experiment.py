#!/usr/bin/env python3
"""Run one node-1-only lab at a time; preserve raw evidence outside Git."""
import argparse
import collections
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[3]
VM = REPO / "implementation/virtual_machine"
LOGS = REPO / ".codex-runs"
os.environ["CXL_LAB_SESSION"] = "run09"
os.environ.setdefault("CXL_LAB_ASSETS", str(VM / ".local/assets09-final"))
spec = importlib.util.spec_from_file_location("lab09", VM / "lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def cli(name, *args):
    path = LOGS / f"09-{name}.log"
    with path.open("w") as stream:
        subprocess.run([sys.executable, str(VM / "lab.py"), *args], check=True,
                       stdout=stream, stderr=subprocess.STDOUT)
    return path


def host_snapshot():
    return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "loadavg": Path("/proc/loadavg").read_text().strip(),
            "ps": subprocess.check_output(["ps", "-eo", "pid,user,psr,pcpu,comm"], text=True)}


def placement(config, label):
    result = {}
    for role, vm in config["vms"].items():
        pid = vm["pid"]
        assert lab.alive(vm), (role, pid)
        threads = {}
        for task in Path(f"/proc/{pid}/task").iterdir():
            cpus = re.search(r"Cpus_allowed_list:\s*(.*)", (task / "status").read_text())[1]
            threads[task.name] = cpus
            assert all(32 <= cpu <= 63 for cpu in lab_cpu_list(cpus)), (pid, task.name, cpus)
        maps = Path(f"/proc/{pid}/numa_maps").read_text()
        (LOGS / f"09-{label}-{role}-numa_maps.log").write_text(maps)
        pages = collections.Counter()
        for node, count in re.findall(r"\bN(\d+)=(\d+)", maps):
            pages[node] += int(count)
        assert set(pages) == {"1"}, (role, dict(pages))
        guest = lab.guest_command(vm, """for p in /proc/[0-9]*; do
test -r "$p/comm" || continue
comm=$(cat "$p/comm")
case "$comm" in firecracker|shared-probe|fc_vcpu*)
for t in "$p"/task/*; do
printf '%s %s ' "$comm" "$t"
grep Cpus_allowed_list "$t/status"
done;; esac
done""", timeout=15)
        result[role] = {"host_pid": pid, "host_threads": threads, "numa_pages": dict(pages),
                        "shared_backing_maps": [s for s in maps.splitlines() if "cxl-lab-" in s],
                        "guest_threads": guest,
                        "guest_cpu_note": "L1 CPUs 0-1 execute through QEMU threads restricted to host 32-63"}
    return result


def lab_cpu_list(text):
    for part in text.split(","):
        limits = part.split("-")
        yield from range(int(limits[0]), int(limits[-1]) + 1)


def trace_setup(config, name):
    for role, vm in config["vms"].items():
        text = lab.guest_command(vm, """mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true
T=/sys/kernel/tracing
echo 0 > $T/tracing_on
echo nop > $T/current_tracer
echo mono > $T/trace_clock
echo 131072 > $T/buffer_size_kb
echo > $T/trace
cat $T/events/kvm/kvm_page_fault/format
echo 1 > $T/events/kvm/kvm_page_fault/enable
echo 1 > $T/tracing_on""", timeout=30)
        (LOGS / f"09-{name}-{role}-trace-setup.log").write_text(text)


def trace_collect(run, config, name):
    outputs = {}
    for index, (role, vm) in enumerate(config["vms"].items()):
        # /dev/mem is inside this disposable L1. It copies trace data into an
        # unused part of our emulated CXL file, avoiding a large serial transfer.
        offset = (1024 + index * 256) * 1024 * 1024
        text = lab.guest_command(vm, f"""T=/sys/kernel/tracing
echo 0 > $T/tracing_on
cat $T/trace > /run/ept-trace.txt
gzip -c /run/ept-trace.txt > /run/ept-trace.gz
printf 'TRACE_BYTES='; wc -c < /run/ept-trace.gz
for f in $T/per_cpu/cpu*/stats; do echo TRACE_STATS=$f; cat $f; done
resource=$(cat /sys/bus/dax/devices/dax0.0/resource)
dd if=/run/ept-trace.gz of=/dev/mem bs=4096 seek=$((resource / 4096 + {offset // 4096})) conv=notrunc
""", timeout=120)
        (LOGS / f"09-{name}-{role}-trace-collection.log").write_text(text)
        size = int(re.search(r"TRACE_BYTES=(\d+)", text)[1])
        assert size < 128 * 1024 * 1024
        with open(config["cxl_backing"], "rb") as backing:
            data = os.pread(backing.fileno(), size, offset)
        assert data[:2] == b"\x1f\x8b", "L1 /dev/mem trace copy did not reach the owned CXL backing"
        path = run / f"{name}-{role}-ept-trace.txt.gz"
        path.write_bytes(data)
        assert len(data) == size
        stats = [int(v) for v in re.findall(r"(?:^|\n)(?:overrun|commit overrun|dropped events):\s*(\d+)", text)]
        assert stats and not any(stats), text
        outputs[role] = {"path": str(path), "bytes": size, "stats": text}
    return outputs


def run_condition(name, kind, poll, dense=False, trace=False, existing=False):
    assert set(os.sched_getaffinity(0)) <= set(range(32, 64))
    policy = subprocess.check_output(["numactl", "--show"], text=True)
    assert re.search(r"^membind: 1\s*$", policy, re.M), policy
    memory, cxl = 2048, (4096 if dense and kind == "direct" else 2048)
    budget = memory * 2 + cxl + 64
    assert budget <= 10 * 1024
    hardware = subprocess.check_output(["numactl", "--hardware"], text=True)
    (LOGS / f"09-{name}-hardware.log").write_text(hardware)
    assert int(re.search(r"node 1 free: (\d+) MB", hardware)[1]) >= budget + 2048, hardware
    evidence = {"name": name, "kind": kind, "requested_poll_us": poll,
                "poll_us": poll if kind in ("direct", "cpu") else 50, "dense": dense, "trace": trace,
                "lab_budget_mib": budget, "hardware_before": hardware, "numa_policy": policy,
                "commit": lab.output(["git", "rev-parse", "HEAD"], cwd=REPO),
                "host_before": host_snapshot()}
    history = []
    done = threading.Event()

    def monitor():
        while not done.is_set():
            history.append(host_snapshot())
            done.wait(1)

    worker = threading.Thread(target=monitor)
    worker.start()
    try:
        if not existing:
            args = ["start", "--vcpus", "2", "--memory-mib", str(memory), "--cxl-mib", str(cxl),
                    "--shared-mib", "64", "--dax-guest", "--dax-4k", "--network-backend", "socket",
                    "--memory-backing-dir", "/dev/shm", "--qemu",
                    str(VM / ".local/run09-qemu/bin/qemu-system-x86_64")]
            if kind in ("direct", "cpu"):
                args.append("--dax-handoff")
            cli(name + "-start", *args)
        run, config = lab.current_run()
        evidence["run"] = str(run)
        cli(name + "-verify", "verify")
        evidence["placement_before"] = placement(config, name + "-before")
        iterations, warmup = (4, 0) if trace else ((20, 4) if dense else (100, 10))
        if kind in ("direct", "cpu"):
            args = ["direct-handoff-bench", "--guest", "cpu" if kind == "cpu" else "linux",
                    "--iterations", str(iterations), "--warmup", str(warmup), "--poll-us", str(poll),
                    "--profile", "--resume-profile"]
            if dense:
                args.extend(["--memory-mib", "512", "--dense-working-set"])
        else:
            args = ["handoff-bench", "--preset", kind, "--iterations", str(iterations),
                    "--warmup", str(warmup), "--profile-restore"]
            if dense:
                args.extend(["--memory-mib", "512", "--dense-working-set"])
        if trace:
            trace_setup(config, name)
            if kind not in ("direct", "cpu"):
                args.append("--profile-ack")
        log = cli(name + "-bench", *args)
        evidence["report"] = json.loads(log.read_text())["report"]
        if trace:
            evidence["ept_traces"] = trace_collect(run, config, name)
        evidence["placement_after"] = placement(config, name + "-after")
        evidence["result"] = "passed"
        print(json.dumps({"name": name, "report": evidence["report"]}), flush=True)
    except BaseException as exc:
        evidence["result"], evidence["error"] = "failed", str(exc)
        raise
    finally:
        try:
            if "config" in locals():
                cli(name + "-stop", "stop")
                evidence["owned_qemu_remaining"] = [v["pid"] for v in config["vms"].values() if lab.alive(v)]
                evidence["backing_remaining"] = [p for p in (config["backing"], config["cxl_backing"])
                                                 if p and Path(p).exists()]
                assert not evidence["owned_qemu_remaining"] and not evidence["backing_remaining"]
        finally:
            done.set()
            worker.join()
            evidence["host_after"] = host_snapshot()
            evidence["host_samples"] = history
            save(LOGS / f"09-{name}-evidence.json", evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("kind", choices=["cpu", "direct", "reuse-prepared", "snapshot"])
    parser.add_argument("--poll-us", type=int, default=50,
                        help="Direct/CPU polling; native modes retain preset poll=50, spin=500")
    parser.add_argument("--dense", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--existing", action="store_true")
    args = parser.parse_args()
    with (LOGS / "09-lab.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_condition(args.name, args.kind, args.poll_us, args.dense, args.trace, args.existing)
