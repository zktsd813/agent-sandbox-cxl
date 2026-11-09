#!/usr/bin/env python3
"""100 ms node sampler for G1/G2 (docs/gpu-node-harvest-a6000-plan.md, section 3).

Writes into --out, all times CLOCK_MONOTONIC ns:
  cpu.csv      per-CPU cumulative busy and total jiffies (/proc/stat)
  threads.csv  per (pid, thread comm) on-CPU and run-queue wait ns deltas (/proc/<pid>/task/*/schedstat), nonzero rows only
  procs.jsonl  first sighting of each tracked pid with comm, cmdline, ppid
  mem.csv      /proc/meminfo and per-NUMA-node used memory (kB)
  gpu.csv      NVML utilisation, memory, PCIe throughput, SM clock, power
  imc.csv      per-socket DDR read/write MiB per interval (sudo perf, own monotonic start)
Tracked processes are the descendants of --root-pid (re-scanned every second); --track-uid adds every process of a uid.
Stops on SIGINT/SIGTERM or when --root-pid exits.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

MEMINFO = ("MemTotal", "MemFree", "Cached", "AnonPages", "Shmem", "Mlocked", "Unevictable", "Dirty")


def read(path):
    with open(path) as stream:
        return stream.read()


def cpu_row():
    row = []
    for line in read("/proc/stat").splitlines()[1:]:
        if not line.startswith("cpu"):
            break
        fields = [int(x) for x in line.split()[1:]]
        idle = fields[3] + fields[4]
        total = sum(fields[:8])
        row += [total - idle, total]
    return row


def proc_table():
    table = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = read(f"/proc/{name}/stat")
            uid = os.stat(f"/proc/{name}").st_uid
        except OSError:
            continue
        rest = stat[stat.rindex(")") + 2:].split()
        table[int(name)] = (int(rest[1]), uid)
    return table


def descendants(table, roots, uid):
    children = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack += children.get(pid, [])
    if uid is not None:
        found |= {pid for pid, (_, owner) in table.items() if owner == uid}
    return found


def read_raw(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        return os.read(descriptor, 256)
    finally:
        os.close(descriptor)


def thread_sample(pids, comms):
    """comms caches tid -> thread name; the caller clears it on each process re-scan."""
    sample = {}
    for pid in pids:
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                oncpu, wait, _ = read_raw(f"/proc/{pid}/task/{tid}/schedstat").split()
                comm = comms.get(tid)
                if comm is None:
                    comm = comms[tid] = read_raw(f"/proc/{pid}/task/{tid}/comm").decode(errors="replace").strip()
            except (OSError, ValueError):
                continue
            key = (pid, comm)
            prev = sample.get(key, (0, 0, 0))
            sample[key] = (prev[0] + int(oncpu), prev[1] + int(wait), prev[2] + 1)
    return sample


def meminfo_row():
    values = {}
    for line in read("/proc/meminfo").splitlines():
        key, rest = line.split(":", 1)
        values[key] = int(rest.split()[0])
    row = [values.get(key, 0) for key in MEMINFO]
    for node in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
        for line in read(node / "meminfo").splitlines():
            if "MemUsed:" in line:
                row.append(int(line.split()[3]))
    return row


class Gpus:
    def __init__(self):
        import pynvml
        self.nvml = pynvml
        pynvml.nvmlInit()
        self.handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(pynvml.nvmlDeviceGetCount())]

    def rows(self):
        nvml, rows = self.nvml, []
        for index, handle in enumerate(self.handles):
            util = nvml.nvmlDeviceGetUtilizationRates(handle)
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            rows.append([index, util.gpu, util.memory, memory.used // 2**20,
                         nvml.nvmlDeviceGetPcieThroughput(handle, nvml.NVML_PCIE_UTIL_TX_BYTES),
                         nvml.nvmlDeviceGetPcieThroughput(handle, nvml.NVML_PCIE_UTIL_RX_BYTES),
                         nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_SM),
                         nvml.nvmlDeviceGetPowerUsage(handle)])
        return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--root-pid", type=int, action="append", default=[])
    parser.add_argument("--track-uid", type=int)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--no-imc", action="store_true")
    parser.add_argument("--cgroup-glob", action="append", default=[],
                        help="cgroup v2 dirs to sample (cpu.stat usage delta, memory.current), e.g. docker scopes")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))

    imc = None
    if not args.no_imc:
        imc_out = (args.out / "imc.csv").open("w")
        imc_out.write(f"# start_ns={time.monotonic_ns()}\n")
        imc_out.flush()
        imc = subprocess.Popen(["sudo", "-n", "perf", "stat", "-a", "-I", str(int(args.interval * 1000)), "-x,",
                                "--per-socket", "-e", "uncore_imc/cas_count_read/,uncore_imc/cas_count_write/"],
                               stdout=subprocess.DEVNULL, stderr=imc_out, start_new_session=True)

    ncpu = os.cpu_count()
    files = {name: (args.out / f"{name}.csv").open("w", newline="")
             for name in ("cpu", "threads", "mem", "gpu", "cgroups")}
    writers = {name: csv.writer(stream) for name, stream in files.items()}
    writers["cgroups"].writerow(["t_ns", "cgroup", "usage_usec", "memory_bytes"])
    writers["cpu"].writerow(["t_ns"] + [f"{kind}{i}" for i in range(ncpu) for kind in ("busy", "total")])
    writers["threads"].writerow(["t_ns", "pid", "comm", "oncpu_ns", "wait_ns", "threads"])
    nodes = sorted(p.name for p in Path("/sys/devices/system/node").glob("node[0-9]*"))
    writers["mem"].writerow(["t_ns"] + [f"{k}_kB" for k in MEMINFO] + [f"{n}_used_kB" for n in nodes])
    writers["gpu"].writerow(["t_ns", "gpu", "util", "mem_util", "mem_used_mib", "pcie_tx_kBps", "pcie_rx_kBps",
                             "sm_mhz", "power_mw"])
    # nvmlDeviceGetPcieThroughput blocks ~20 ms per call, so GPUs are sampled on their own thread and cadence.
    gpus = Gpus()

    def gpu_loop():
        tick = time.monotonic()
        while not stop:
            tick += args.interval
            t_ns = time.monotonic_ns()
            for row in gpus.rows():
                writers["gpu"].writerow([t_ns] + row)
            time.sleep(max(0.0, tick - time.monotonic()))
    threading.Thread(target=gpu_loop, daemon=True).start()
    procs = (args.out / "procs.jsonl").open("w")
    seen, tracked, previous, comms = set(), set(), {}, {}
    cgroups, cgroup_usage = [], {}
    next_scan = next_tick = time.monotonic()
    overhead_ns = samples = 0
    while not stop:
        now = time.monotonic()
        if now < next_tick:
            time.sleep(next_tick - now)
        next_tick += args.interval
        begin = time.monotonic_ns()
        if begin / 1e9 >= next_scan:
            table = proc_table()
            if args.root_pid and not any(pid in table for pid in args.root_pid):
                break
            tracked = descendants(table, args.root_pid, args.track_uid)
            for pid in tracked - seen:
                try:
                    cmdline = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()[:400]
                    comm = read(f"/proc/{pid}/comm").strip()
                except OSError:
                    continue
                procs.write(json.dumps({"t_ns": begin, "pid": pid, "ppid": table[pid][0], "comm": comm,
                                        "cmdline": cmdline}) + "\n")
                seen.add(pid)
            procs.flush()
            next_scan = begin / 1e9 + 1.0
            comms.clear()
            cgroups = [p for pattern in args.cgroup_glob for p in Path("/").glob(pattern.lstrip("/"))]
        writers["cpu"].writerow([begin] + cpu_row())
        current = thread_sample(tracked, comms)
        for key, (oncpu, wait, count) in current.items():
            old = previous.get(key)
            if old is None:
                continue
            d_on, d_wait = oncpu - old[0], wait - old[1]
            if d_on > 0 or d_wait > 0:
                writers["threads"].writerow([begin, key[0], key[1], max(d_on, 0), max(d_wait, 0), count])
        previous = current
        for group in cgroups:
            try:
                usage = int(read(group / "cpu.stat").split()[1])
                memory = int(read(group / "memory.current"))
            except (OSError, ValueError, IndexError):
                continue
            old = cgroup_usage.get(group.name)
            cgroup_usage[group.name] = usage
            if old is not None:
                writers["cgroups"].writerow([begin, group.name, usage - old, memory])
        writers["mem"].writerow([begin] + meminfo_row())
        samples += 1
        overhead_ns += time.monotonic_ns() - begin
        if samples % 50 == 0:
            for stream in files.values():
                stream.flush()
    stop.append(1)
    time.sleep(3 * args.interval)
    for stream in files.values():
        stream.close()
    if imc:
        subprocess.run(["sudo", "-n", "kill", "-INT", str(imc.pid)], check=False)
        imc.wait(timeout=10)
    (args.out / "sampler.json").write_text(json.dumps({
        "samples": samples, "interval_s": args.interval,
        "mean_sample_cost_ms": overhead_ns / max(samples, 1) / 1e6, "tracked_pids_seen": len(seen)}, indent=2) + "\n")


if __name__ == "__main__":
    main()
