#!/usr/bin/env python3
"""Max running sandboxes per core under the SLO, per hardware and setup (linear interpolation at the first failure).

  analyze.py RUNS.jsonl [--resident hwm|actual] [--csv OUT]
"""
import argparse, csv, json
from collections import defaultdict
import numpy as np

SLO = dict(cpu_weighted=1.10, heavy_p99=2.0)
M = ["running_per_core", "util", "useful_util", "cpu_weighted", "heavy_p99", "all_p99", "tax_cpu_frac",
     "waiting_per_core", "moves_per_s", "blocked_moves_per_s", "resident_gib_per_core", "spill_node_frac"]
ORDER = {("dram", "none"): 0, ("dram", "copy"): 1, ("expander", "none"): 2, ("expander", "copy"): 3,
         ("pool", "none"): 4, ("pool", "handoff"): 5}


def points(g):
    pts = []
    for d in sorted(g):
        rs = g[d]
        pts.append({m: float(np.mean([r[m] for r in rs])) for m in M} | {"density": d, "seeds": len(rs)})
    return pts


def best(pts):
    fail = next((i for i, p in enumerate(pts) if any(p[k] > v for k, v in SLO.items())), None)
    passing = pts if fail is None else pts[:fail]
    mem_cap = max(p["running_per_core"] for p in pts)
    if not passing:
        return None
    b = dict(max(passing, key=lambda p: p["running_per_core"]))
    b["limit"] = "grid-top" if fail is None else "slo"
    if fail is not None and fail > 0:
        lo, hi = pts[fail - 1], pts[fail]
        ts = [(v - lo[k]) / (hi[k] - lo[k]) for k, v in SLO.items() if hi[k] > v and hi[k] > lo[k]]
        t = max(0.0, min(1.0, min(ts))) if ts else 0.0
        cand = {m: lo[m] + t * (hi[m] - lo[m]) for m in M}
        if cand["running_per_core"] > b["running_per_core"]:
            b.update(cand)
    if b["running_per_core"] >= mem_cap - 0.02 and pts[-1]["waiting_per_core"] > 0.05:
        b["limit"] = "memory"
    b["mem_cap"] = mem_cap
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--resident", default="hwm")
    ap.add_argument("--csv")
    a = ap.parse_args()
    groups = defaultdict(lambda: defaultdict(list))
    for path in a.runs:
        for line in open(path):
            r = json.loads(line)
            if r["resident"] == a.resident:
                groups[(r["cores"], r["dram_per_core"], r["mem"], r["cxl_per_core"], r["move"])][r["density"]].append(r)
    out = []
    print(f"resident={a.resident}  SLO: CPU-weighted <= {SLO['cpu_weighted']}, heavy p99 <= {SLO['heavy_p99']}")
    for hw in sorted({k[:2] for k in groups}):
        keys = sorted([k for k in groups if k[:2] == hw], key=lambda k: (k[3], ORDER[(k[2], k[4])]))
        print(f"\n== {hw[0]} cores/node, DRAM {hw[1]} GiB/core")
        print(f"{'setup':20s} {'sbx/core':>8s} {'vs DRAM':>7s} {'util':>5s} {'cpuw':>5s} {'hp99':>5s} {'tax%':>5s} "
              f"{'GiB/c':>5s} {'mv/s':>5s} {'memcap':>6s} limit")
        base = None
        for k in keys:
            b = best(points(groups[k]))
            name = f"{k[2]}{'' if k[2] == 'dram' else '+' + str(k[3])} {k[4]}"
            if b is None:
                print(f"{name:20s} no point passes"); continue
            if k[2] == "dram" and k[4] == "none":
                base = b["running_per_core"]
            rel = b["running_per_core"] / base if base else float("nan")
            print(f"{name:20s} {b['running_per_core']:8.3f} {rel:7.2f} {b['util']:5.2f} {b['cpu_weighted']:5.3f} "
                  f"{b['heavy_p99']:5.2f} {100 * b['tax_cpu_frac']:5.2f} {b['resident_gib_per_core']:5.2f} "
                  f"{b['moves_per_s']:5.1f} {b['mem_cap']:6.3f} {b['limit']}")
            out.append(dict(resident=a.resident, cores=k[0], dram_per_core=k[1], mem=k[2], cxl_per_core=k[3], move=k[4],
                            vs_dram_none=rel, **{m: b[m] for m in M}, mem_cap=b["mem_cap"], limit=b["limit"]))
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0]))
            w.writeheader(); w.writerows(out)


if __name__ == "__main__":
    main()
