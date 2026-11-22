#!/usr/bin/env python3
"""First-order sandboxes per node by memory-extension policy (11-19), from report.py's JSON.

N = min(memory ceiling, CPU ceiling); memory ceiling = (budget - reserve + extra) / (m x remaining share);
CPU ceiling = 0.72 x logical CPUs / c. Upper bounds: no compression/merge CPU cost, no restore storms, merge measured
among same-task replicas. Snapshot keeps (1 - freed share) of the memory-time on average and ignores the headroom a
restore needs.

  policy_calc.py REPORT_JSON [--budget 114 --reserve 12 --cpus 120 --cxl-gib 120]
"""
import argparse
import json

ap = argparse.ArgumentParser()
ap.add_argument("report")
ap.add_argument("--budget", type=float, default=114)
ap.add_argument("--reserve", type=float, default=12)
ap.add_argument("--cpus", type=int, default=120)
ap.add_argument("--cxl-gib", type=float, default=120, help="CXL added (2 GiB per physical core at 60 cores)")
a = ap.parse_args()
rep = json.load(open(a.report))
ROW = {"RL": "1-rl-hi", "SWE": "1-swe-hi", "CI": "1-ci"}
SNAP_T = {"RL": 10, "SWE": 10, "CI": 5}
print(f"{'':4s} {'base':>6s} " + " ".join(f"{h:>16s}" for h in
      ("zswap", "merge", "zswap+merge", "snapshot", "CXL", "CXL+zswap+merge")) + "   CPU cap")
for w, run in ROW.items():
    r = next(x for x in rep[w]["rows"] if x["run"] == run)
    red = rep[w]["reductions"]
    snap = next(p for p in rep[w]["snapshot"]["policies"] if p["T_s"] == SNAP_T[w])
    m, c = r["m"], r["c"]
    cpu_cap = 0.72 * a.cpus / c
    mem = a.budget - a.reserve

    def n(extra=0.0, keep=1.0):
        return min((mem + extra) / (m * keep), cpu_cap)

    base = n()
    cells = [n(keep=red["remaining_compress_only"]), n(keep=1 - red["merged_saving"]), n(keep=red["remaining_after_all"]),
             n(keep=1 - snap["memory_freed_share"]), n(extra=a.cxl_gib), n(extra=a.cxl_gib, keep=red["remaining_after_all"])]
    print(f"{w:4s} {base:6.0f} " + " ".join(f"{x:7.0f} (x{x / base:4.1f})" for x in cells) + f"   {cpu_cap:6.0f}")
