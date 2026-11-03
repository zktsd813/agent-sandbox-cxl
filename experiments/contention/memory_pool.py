#!/usr/bin/env python3
"""Memory headroom per node vs one shared pool, from pilot runs' per-VM RSS (memory.csv, 2 s).

Per run, S_n(t) = sum of guest RSS of the VMs homed on node n (logical node; memory is on one NUMA node either way).
  headroom     H_n = q(S_n) - mean(S_n) for q in max / p99.9 / p99; pooled H = q(sum_n S_n) - mean(sum_n S_n)
  provisioned  ISO: sum_n q(S_n); pool: N x L + P, local DRAM L = a quantile of S_n, P = q(sum_n max(0, S_n - L))
Overflow handling with local DRAM L per node (no transfer vs transfer):
  fallback     a VM's new pages go to local DRAM while the node has room, else straight to CXL (no copy). Freed
               pages leave CXL first (LIFO) or DRAM first (FIFO). Pages stay where they were allocated.
               Reported: CXL-resident share of RSS (tax exposure), pool peak, and CXL pages that sit there while
               the node has free DRAM for them (would need promotion, i.e. a transfer, to fix).
  demotion     new pages always go to DRAM; when the node exceeds L, existing pages are copied to CXL and copied
               back when it drops (the transfer approach). Reported: GiB moved per hour and peak rate.
"""

import argparse
import collections
import csv
import json
from pathlib import Path

GIB = 2 ** 20  # KiB per GiB


def quantile(values, q):
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0


def load(run):
    config = json.loads((run / "run.json").read_text())
    home = {}
    for line in (run / "vms.jsonl").open():
        r = json.loads(line)
        home[r["vm"]] = r["node"]
    samples = collections.defaultdict(dict)  # t -> {vm: kib}
    with (run / "memory.csv").open() as stream:
        for row in csv.DictReader(stream):
            if row["vm"].isdigit():
                samples[int(row["t_ns"])][int(row["vm"])] = int(row["rss_kib"])
    times = sorted(samples)
    begin = times[0] + int(config["ramp_s"] * 1e9)
    end = begin + int(config["duration_s"] * 1e9)
    if config.get("sync_steps"):
        steps = [json.loads(line) for line in (run / "steps.jsonl").open()]
        if steps:
            begin, end = steps[0]["begin_ns"], steps[-1]["end_ns"]
    # VMs whose home node is unknown (still running at the end and never recorded) are dropped
    series = [(t, {vm: k for vm, k in samples[t].items() if vm in home}) for t in times if begin <= t <= end]
    nodes = sorted({n["index"] for n in config["nodes"]})
    return config, home, nodes, series


def headroom(per_node, total):
    out = {}
    for label, q in (("max", 1.0), ("p999", 0.999), ("p99", 0.99)):
        h_nodes = sum(quantile(s, q) - sum(s) / len(s) for s in per_node.values())
        h_pool = quantile(total, q) - sum(total) / len(total)
        out[label] = {"sum_node_headroom_gib": round(h_nodes / GIB, 2), "pool_headroom_gib": round(h_pool / GIB, 2),
                      "saved_fraction": round(1 - h_pool / h_nodes, 3) if h_nodes > 0 else None,
                      "iso_provisioned_gib": round(sum(quantile(s, q) for s in per_node.values()) / GIB, 2)}
    return out


def fallback(series, home, nodes, local_kib, lifo):
    dram, cxl = collections.defaultdict(int), collections.defaultdict(int)
    previous = {}
    exposure = stranded = rss_total = 0.0
    pool_peak = 0
    for t, vms in series:
        for vm in set(previous) - set(vms):  # VM ended: free everything
            dram.pop(vm, None)
            cxl.pop(vm, None)
        used = collections.defaultdict(int)
        for vm, d in dram.items():
            used[home[vm]] += d
        for vm, kib in vms.items():
            delta = kib - previous.get(vm, 0)
            n = home[vm]
            if delta > 0:
                room = max(0, local_kib - used[n])
                to_dram = min(delta, room)
                dram[vm] += to_dram
                used[n] += to_dram
                cxl[vm] += delta - to_dram
            elif delta < 0:
                free = -delta
                order = (cxl, dram) if lifo else (dram, cxl)
                for tier in order:
                    take = min(free, tier[vm])
                    tier[vm] -= take
                    free -= take
                    if tier is dram:
                        used[n] -= take
        previous = dict(vms)
        in_cxl = sum(cxl.values())
        pool_peak = max(pool_peak, in_cxl)
        exposure += in_cxl
        rss_total += sum(vms.values())
        for n in nodes:
            node_cxl = sum(v for vm, v in cxl.items() if home[vm] == n)
            stranded += min(node_cxl, max(0, local_kib - used[n]))
    return {"cxl_share_of_rss": round(exposure / rss_total, 4) if rss_total else None,
            "pool_peak_gib": round(pool_peak / GIB, 2),
            "promotable_share_of_cxl": round(stranded / exposure, 4) if exposure else None}


def demotion(per_node, local_kib, dt_s):
    moved = peak_rate = 0.0
    for s in per_node.values():
        previous = 0
        for v in s:
            over = max(0, v - local_kib)
            step = abs(over - previous)
            moved += step
            peak_rate = max(peak_rate, step / dt_s)
            previous = over
    return moved, peak_rate


def iso_capacity(series, home, nodes, per_node, steps=80):
    """Same total memory T with and without the pool. Without: N identical nodes of T/N, so T = N x max_n q(S_n)
    (or sum_n q(S_n) if each node were sized for its own load). With: N x L local + pool P, P = q(sum_n max(0, S_n - L)),
    the pool also full at its own q. The smallest T over L, and the CXL share of RSS that the split costs."""
    n = len(nodes)
    top = max(max(s) for s in per_node.values())
    out = {}
    for label, q in (("max", 1.0), ("p999", 0.999)):
        no_pool = n * max(quantile(s, q) for s in per_node.values())
        sized = sum(quantile(s, q) for s in per_node.values())
        best = None
        for i in range(steps + 1):
            local = top * i / steps
            over = [sum(max(0, per_node[m][k] - local) for m in nodes) for k in range(len(series))]
            total = n * local + quantile(over, q)
            if best is None or total < best[0]:
                best = (total, local, quantile(over, q))
        total, local, pool = best
        entry = {"no_pool_identical_nodes_gib": round(no_pool / GIB, 2), "no_pool_sized_per_node_gib": round(sized / GIB, 2),
                 "pool_total_gib": round(total / GIB, 2), "pool_local_per_node_gib": round(local / GIB, 2),
                 "pool_cxl_gib": round(pool / GIB, 2), "pool_cxl_ratio": round(pool / total, 3) if total else None,
                 "density_gain_vs_identical": round(no_pool / total - 1, 3) if total else None,
                 "density_gain_vs_sized": round(sized / total - 1, 3) if total else None,
                 "cxl_share_of_rss": fallback(series, home, nodes, local, True)["cxl_share_of_rss"]}
        # fixed CXL ratio r: the smallest T with local (1 - r) T / N per node and pool r T that never overflows (at q)
        for r in (0.1, 0.25, 0.5):
            lo, hi = 0.0, float(no_pool)
            for _ in range(30):
                mid = (lo + hi) / 2
                local_r = mid * (1 - r) / n
                over = [sum(max(0, per_node[m][k] - local_r) for m in nodes) for k in range(len(series))]
                lo, hi = (lo, mid) if quantile(over, q) <= r * mid else (mid, hi)
            entry[f"min_T_r{int(r * 100)}"] = {"total_gib": round(hi / GIB, 2),
                                               "density_gain_vs_identical": round(no_pool / hi - 1, 3),
                                               "density_gain_vs_sized": round(sized / hi - 1, 3),
                                               "cxl_share_of_rss": fallback(series, home, nodes, hi * (1 - r) / n, True)["cxl_share_of_rss"]}
        # the same T as the no-pool setup, split with a fixed CXL ratio r: does the pool hold, and what share sits on CXL
        for r in (0.25, 0.5):
            local_r = no_pool * (1 - r) / n
            over = [sum(max(0, per_node[m][k] - local_r) for m in nodes) for k in range(len(series))]
            entry[f"same_T_r{int(r * 100)}"] = {"pool_peak_use": round(max(over) / (no_pool * r), 3),
                                                "cxl_share_of_rss": fallback(series, home, nodes, local_r, True)["cxl_share_of_rss"]}
        out[label] = entry
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    results = []
    for run in args.runs:
        config, home, nodes, series = load(run)
        per_node = {n: [sum(k for vm, k in vms.items() if home[vm] == n) for _, vms in series] for n in nodes}
        total = [sum(v) for v in zip(*per_node.values())]
        hours = (series[-1][0] - series[0][0]) / 3.6e12
        dt_s = (series[-1][0] - series[0][0]) / 1e9 / max(1, len(series) - 1)
        result = {"run": run.name, "condition": (run / "condition.txt").read_text().split()[0],
                  "sync": bool(config.get("sync_steps")), "samples": len(series), "hours": round(hours, 2),
                  "mean_node_gib": {n: round(sum(s) / len(s) / GIB, 2) for n, s in per_node.items()},
                  "max_node_gib": {n: round(max(s) / GIB, 2) for n, s in per_node.items()},
                  "mean_vm_mib": round(sum(total) / sum(len(v) for _, v in series) / 1024, 1),
                  "mean_live_vms": round(sum(len(v) for _, v in series) / len(series), 1),
                  "headroom": headroom(per_node, total), "local_dram": {}}
        for label, q in (("p50", .5), ("p90", .9), ("p99", .99)):
            local = max(quantile(s, q) for s in per_node.values())  # same L on every node
            over = [sum(max(0, per_node[n][i] - local) for n in nodes) for i in range(len(total))]
            moved, rate = demotion(per_node, local, dt_s)
            result["local_dram"][label] = {
                "L_gib": round(local / GIB, 2),
                "pool_needed_max_gib": round(max(over) / GIB, 2),
                "provisioned_pool_gib": round((len(nodes) * local + max(over)) / GIB, 2),
                "provisioned_iso_gib": round(sum(max(s) for s in per_node.values()) / GIB, 2),
                "time_over_L": round(sum(o > 0 for o in over) / len(over), 4),
                "fallback_lifo": fallback(series, home, nodes, local, True),
                "fallback_fifo": fallback(series, home, nodes, local, False),
                "demotion_gib_per_hour": round(moved / GIB / hours, 2) if hours else None,
                "demotion_peak_gib_per_s": round(rate / GIB, 3)}
        result["iso_capacity"] = iso_capacity(series, home, nodes, per_node)
        results.append(result)
        print(json.dumps(result))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main()
