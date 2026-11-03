#!/usr/bin/env python3
"""Guest memory composition and host memory of pilot runs with --mem-sample-s (block base disk vs --base-pmem DAX).

Per run (inside the ramp + duration window):
  guest   per-VM medians over time of Cached, AnonPages, MemFree (SWE_MEM lines) and base-image reads
  host    sum over live VMs of RssAnon (guest RAM the host backs privately) and RssFile (shared file mappings, i.e. the
          DAX base), the images' host page cache (image-cache.csv), and sandbox memory = RssAnon + image cache
  reads   base-image reads by VM age: share of a VM's base reads still to come after t seconds (moved sandbox re-reads)
  per_vm  host RssAnon of a VM against its guest's page cache, anon, shmem and kernel memory (guest RAM is 2 GiB)
"""

import argparse
import bisect
import collections
import csv
import json
from pathlib import Path

KIB = 1024


def window(run, config):
    times = []
    with (run / "memory.csv").open() as stream:
        for row in csv.DictReader(stream):
            times.append(int(row["t_ns"]))
    begin = min(times) + int(config["ramp_s"] * 1e9)
    return begin, begin + int(config["duration_s"] * 1e9)


def q(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))] if values else None


def composition(run, begin, end, guest_kib=2048 * KIB):
    """Per VM, medians over its samples (after 60 s uptime) of host RssAnon and of the guest's non-free memory split into
    page cache, anon, shmem and kernel/other; touched_free = host RssAnon - guest non-free (freed but not reported)."""
    anon = collections.defaultdict(list)
    with (run / "memory.csv").open() as stream:
        for row in csv.DictReader(stream):
            if row["vm"].endswith("_anon"):
                anon[int(row["vm"][:-5])].append((int(row["t_ns"]), int(row["rss_kib"])))
    keys = {vm: [t for t, _ in v] for vm, v in anon.items()}
    parts = collections.defaultdict(list)
    for line in (run / "sandboxes.jsonl").open():
        r = json.loads(line)
        rows = collections.defaultdict(list)
        for m in r.get("mem", []):
            if not begin <= m[0] <= end or m[1] < 60:
                continue
            ts = keys.get(r["vm"], [])
            i = bisect.bisect_left(ts, m[0])
            near = [j for j in (i - 1, i) if 0 <= j < len(ts) and abs(ts[j] - m[0]) < 3e9]
            if not near:
                continue
            host = anon[r["vm"]][min(near, key=lambda j: abs(ts[j] - m[0]))][1]
            used = guest_kib - m[2]
            for name, value in (("host_rss_anon", host), ("guest_used", used), ("page_cache", m[3] + m[4]),
                                ("anon", m[5]), ("shmem", m[6]), ("kernel_other", used - m[3] - m[4] - m[5] - m[6]),
                                ("touched_free", host - used)):
                rows[name].append(value)
        for name, values in rows.items():
            parts[name].append(q(values, 0.5))
    return {name: {"median_mib": round(q(v, 0.5) / KIB, 1), "mean_mib": round(sum(v) / len(v) / KIB, 1)}
            for name, v in parts.items()} | {"vms": len(parts["host_rss_anon"])}


def analyze(run):
    config = json.loads((run / "run.json").read_text())
    pmem = bool(config.get("base_pmem"))
    begin, end = window(run, config)
    # host: per sample time, sums over VMs
    host = collections.defaultdict(lambda: [0, 0, 0, 0])  # t -> [vm count, rss, anon, file] (KiB)
    with (run / "memory.csv").open() as stream:
        for row in csv.DictReader(stream):
            t = int(row["t_ns"])
            if not begin <= t <= end:
                continue
            vm = row["vm"]
            v = int(row["rss_kib"])
            if vm.isdigit():
                host[t][0] += 1
                host[t][1] += v
            elif vm.endswith("_anon"):
                host[t][2] += v
            elif vm.endswith("_file"):
                host[t][3] += v
    cache = collections.defaultdict(int)
    cache_path = run / "image-cache.csv"
    if cache_path.exists():
        for line in cache_path.open():
            t, _, size = line.strip().split(",")
            if begin <= int(t) <= end:
                cache[int(t)] += int(size) // KIB
    cache_times = sorted(cache)
    samples = []
    for t in sorted(host):
        n, rss, anon, mapped = host[t]
        i = bisect.bisect_right(cache_times, t) - 1
        image = cache[cache_times[i]] if i >= 0 else None
        samples.append((n, rss, anon, mapped, image))
    gib = lambda kib: round(kib / 2 ** 20, 2) if kib is not None else None
    mean = lambda k: sum(s[k] for s in samples if s[k] is not None) / max(1, sum(s[k] is not None for s in samples))
    peak = lambda k: max((s[k] for s in samples if s[k] is not None), default=None)
    total = [s[2] + s[4] for s in samples if s[4] is not None]
    # guest: per VM, the sample medians, then medians over VMs; base reads by age
    guest = collections.defaultdict(list)
    curves = []
    base_field = None if pmem else 8  # vda read sectors in block mode (index in the SWE_MEM row)
    for line in (run / "sandboxes.jsonl").open():
        r = json.loads(line)
        mem = [m for m in r.get("mem", []) if begin <= m[0] <= end]
        if not mem:
            continue
        for name, idx in (("cached_mib", 3), ("anon_mib", 5), ("free_mib", 2), ("shmem_mib", 6)):
            guest[name].append(q([m[idx] / KIB for m in mem], 0.5))
        if base_field is not None and r.get("completed") and r.get("mem"):
            full = r["mem"]
            t0 = full[0][1]
            last = full[-1][base_field]
            if last > 0:
                curves.append([(m[1] - t0, m[base_field] / last) for m in full])
    remaining = {}
    for age in (30, 60, 120, 300, 600, 1200):
        vals = []
        for c in curves:
            ages = [a for a, _ in c]
            i = bisect.bisect_right(ages, age) - 1
            if ages[-1] >= age:
                vals.append(1 - c[i][1] if i >= 0 else 1.0)
        remaining[f"{age}s"] = {"n": len(vals), "median": round(q(vals, 0.5), 3) if vals else None,
                                "mean": round(sum(vals) / len(vals), 3) if vals else None}
    # a move at a uniformly random time of a VM's life: its expected share of base reads still to come (rho)
    rho = [sum(1 - f for _, f in c) / len(c) for c in curves]
    if rho:
        remaining["uniform_move"] = {"n": len(rho), "median": round(q(rho, 0.5), 3), "mean": round(sum(rho) / len(rho), 3)}
    contention = json.loads((run / "contention-1ms.json").read_text()) if (run / "contention-1ms.json").exists() else {}
    return {"run": run.name, "condition": (run / "condition.txt").read_text().split()[0] if (run / "condition.txt").exists() else None,
            "base_pmem": pmem, "mean_live_vms": round(mean(0), 1),
            "host_rss_gib_mean": gib(mean(1)), "host_rss_anon_gib_mean": gib(mean(2)), "host_rss_anon_gib_peak": gib(peak(2)),
            "host_rss_file_gib_mean": gib(mean(3)), "image_cache_gib_mean": gib(mean(4)), "image_cache_gib_peak": gib(peak(4)),
            "sandbox_memory_gib_mean": gib(sum(total) / len(total)) if total else None,
            "sandbox_memory_gib_peak": gib(max(total)) if total else None,
            "per_vm_rss_anon_mib": round(mean(2) / max(1, mean(0)) / KIB, 1),
            "guest_median_mib": {k: round(q(v, 0.5), 1) for k, v in guest.items()},
            "per_vm": composition(run, begin, end),
            "base_reads_remaining_after_age": remaining,
            "wait_over_run": contention.get("total_wait_over_run"),
            "utilization": round(sum(contention["mean_run_cores_per_unit"].values()) / sum(contention["cores_per_unit"].values()), 3)
            if contention else None,
            "cmd_slowdown": contention.get("command_slowdown_vs_solo"),
            "mismatches": contention.get("command_mismatches"), "timeouts": contention.get("command_timeouts"),
            "sandboxes": contention.get("sandboxes")}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    results = [analyze(run) for run in args.runs]
    for r in results:
        print(json.dumps(r))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main()
