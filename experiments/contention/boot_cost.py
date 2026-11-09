#!/usr/bin/env python3
"""Time, CPU and memory of creating microVMs in a burst, with the cores idle or busy.

For each load level (busy-loop processes per core on the VM cores) --vms microVMs are created at once on those cores:
  prepare  job disk (mkfs.ext4 -d) and sparse 8 GiB writable disk (mkfs.ext4): the host side of a creation, done one
           VM after another on the VM cores; CPU from the children's rusage
  boot     all VMMs started together; Firecracker start -> SWE_READY on the serial console (guest kernel, overlay
           mount, agent start); CPU and runqueue wait from the VMM threads' schedstat at READY
  idle     the guest then runs one `sleep`; the VMM RSS after --idle-s is what a pre-created, unused microVM holds
Same disk layout and VM config as `sandbox.py solo` (shared read-only base, balloon), without network.

  python3 boot_cost.py --entry NAME [--candidates FILE] --cores 2-9 --vms 16 --loads 0 1 2 4 --rounds 3 --out .local/boot-cost.json
"""
import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

import sandbox


def trivial_trace(entry, path, idle_s):
    """The entry's trace config with a single `sleep` command, so the guest idles after boot."""
    config = json.loads((sandbox.REPO / entry["trace"]).read_text())["info"]["config"]
    path.write_text(json.dumps({"instance_id": path.stem, "trajectory_format": "boot-cost",
                                "info": {"config": config, "exit_status": "Idle", "submission": ""},
                                "messages": [{"role": "assistant", "content": "",
                                              "extra": {"actions": [{"command": f"sleep {idle_s * 3}"}], "timestamp": 0.0}},
                                             {"role": "tool", "content": "", "extra": {"returncode": 0, "timestamp": 1.0}}]}))


def vmm_stats(pid):
    """(run ns, runqueue wait ns) summed over the VMM's threads, and RSS KiB."""
    run_ns = wait_ns = 0
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            r, w, _ = (task / "schedstat").read_text().split()
            run_ns, wait_ns = run_ns + int(r), wait_ns + int(w)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            pass
    rss = next(int(line.split()[1]) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
               if line.startswith("VmRSS:"))
    return run_ns, wait_ns, rss


def prepare(entry, image, directory, memory_mib):
    """Job and writable disks plus the VMM config; (wall s, CPU s) of the host commands."""
    directory.mkdir(parents=True)
    t0, c0 = time.monotonic(), os.times()
    sandbox.make_job(entry, image, directory, 0.0)
    upper = directory / "upper.ext4"
    sandbox.run(["truncate", "-s", "8G", upper])
    sandbox.run(["mkfs.ext4", "-q", "-F", "-L", "swe-upper", upper])
    c1 = os.times()
    config = {"boot-source": {"kernel_image_path": str(sandbox.KERNEL), "initrd_path": str(sandbox.overlay_initrd()),
                              "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"},
              "drives": [{"drive_id": "base", "path_on_host": str(sandbox.image_dir(entry) / "rootfs.ext4"),
                          "is_root_device": False, "is_read_only": True},
                         {"drive_id": "job", "path_on_host": str(directory / "job.ext4"), "is_root_device": False,
                          "is_read_only": False},
                         {"drive_id": "upper", "path_on_host": str(upper), "is_root_device": False, "is_read_only": False}],
              "machine-config": {"vcpu_count": 1, "mem_size_mib": memory_mib}}
    if sandbox.BALLOON:
        config["balloon"] = sandbox.balloon_config()
    sandbox.save_json(directory / "config.json", config)
    return time.monotonic() - t0, (c1.children_user - c0.children_user) + (c1.children_system - c0.children_system)


def burst(entry, image, work, cores, node, n, memory_mib, idle_s, harness_cpus):
    rows = [{} for _ in range(n)]
    os.sched_setaffinity(0, cores)  # the host side of a creation runs where the VMs and the load are
    for i in range(n):
        rows[i]["prepare_wall_s"], rows[i]["prepare_cpu_s"] = prepare(entry, image, work / f"vm{i}", memory_mib)
    os.sched_setaffinity(0, harness_cpus)
    ready = threading.Barrier(n + 1)
    procs = []

    def watch(i, proc, started):
        try:
            for line in proc.stdout:
                if line.startswith("SWE_READY"):
                    now = time.monotonic_ns()
                    run_ns, wait_ns, rss = vmm_stats(proc.pid)
                    rows[i].update({"boot_s": (now - started) / 1e9, "boot_cpu_s": run_ns / 1e9,
                                    "boot_wait_s": wait_ns / 1e9, "rss_ready_mib": rss / 1024})
                    break
        finally:
            ready.wait()
            for _ in proc.stdout:  # keep draining so the guest never blocks on the console
                pass

    for i in range(n):
        p = subprocess.Popen(["numactl", f"--membind={node}", "taskset", "-c", ",".join(map(str, sorted(cores))),
                              str(sandbox.FIRECRACKER), "--no-api", "--config-file", str(work / f"vm{i}" / "config.json")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace")
        procs.append(p)
        threading.Thread(target=watch, args=(i, p, time.monotonic_ns()), daemon=True).start()
    ready.wait(timeout=300)
    time.sleep(idle_s)
    for i, p in enumerate(procs):
        try:
            rows[i]["rss_idle_mib"] = vmm_stats(p.pid)[2] / 1024
        except (FileNotFoundError, ProcessLookupError, StopIteration):
            pass
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    shutil.rmtree(work)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entry", required=True, help="Trace name in --candidates whose built image to boot")
    parser.add_argument("--candidates", type=Path, default=sandbox.HERE / "candidates-rl.json")
    parser.add_argument("--cores", default="2-9")
    parser.add_argument("--harness-cpus", default="0-1")
    parser.add_argument("--node", type=int, default=0)
    parser.add_argument("--vms", type=int, default=16)
    parser.add_argument("--loads", type=float, nargs="+", default=[0, 1, 2, 4], help="Busy loops per core")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--idle-s", type=float, default=10.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    cores, harness = (set(x for part in spec.split(",") for x in range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1))
                      for spec in (args.cores, args.harness_cpus))
    entry = next(e for e in json.loads(args.candidates.read_text())["traces"] if e["name"] == args.entry)
    image = json.loads((sandbox.image_dir(entry) / "image.json").read_text())
    work = sandbox.LOCAL / "boot-cost"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    trace = work.parent / "boot-cost-trace.traj.json"
    trivial_trace(entry, trace, args.idle_s)
    entry = {**entry, "trace": str(trace)}
    report = {"entry": args.entry, "cores": sorted(cores), "vms": args.vms, "memory_mib": args.memory_mib, "levels": []}
    os.sched_setaffinity(0, harness)
    for load in args.loads:
        hogs = [subprocess.Popen(["taskset", "-c", str(c), "sh", "-c", "while :; do :; done"])
                for c in sorted(cores) for _ in range(int(load))]
        try:
            time.sleep(1 if hogs else 0)
            rows = [r for _ in range(args.rounds)
                    for r in burst(entry, image, work / "b", cores, args.node, args.vms, args.memory_mib, args.idle_s, harness)]
        finally:
            for h in hogs:
                h.kill()
                h.wait()
        level = {"load_per_core": load, "rows": rows}
        for key in ("prepare_wall_s", "prepare_cpu_s", "boot_s", "boot_cpu_s", "boot_wait_s", "rss_ready_mib", "rss_idle_mib"):
            v = [r[key] for r in rows if key in r]
            level[key] = np.percentile(v, [50, 90, 100]).tolist() if v else None
        level["booted"] = sum("boot_s" in r for r in rows)
        report["levels"].append(level)
        print(json.dumps({k: v for k, v in level.items() if k != "rows"}, default=lambda x: round(x, 3)), flush=True)
        if args.out:
            args.out.write_text(json.dumps(report, indent=1))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
