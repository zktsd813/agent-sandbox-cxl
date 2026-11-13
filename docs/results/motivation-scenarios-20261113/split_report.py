#!/usr/bin/env python3
"""Summarize split_tax.py runs: per stage, median wall over repetitions for dram / cxl / split, and memory placement.

  python3 split_report.py split-runs.jsonl split-stages.csv split-summary.json
"""
import csv
import json
import statistics as st
import sys
from collections import defaultdict

MiB = 2 ** 20
runs = [json.loads(l) for l in open(sys.argv[1])]
by = defaultdict(list)
for r in runs:
    by[(r["trace"], r["stage"], r["cond"])].append(r)
stages = sorted({(t, s) for t, s, _ in by}, key=lambda x: (x[0], x[1]))
rows = []
for t, s in stages:
    cell = {c: by.get((t, s, c), []) for c in ("dram", "cxl", "split")}
    if not all(cell.values()):
        continue
    med = {c: st.median(r["wall_s"] for r in v) for c, v in cell.items()}
    cpu = {c: st.median((r["after"]["cpu_usec"] - r["before"]["cpu_usec"]) / 1e6 for r in v) for c, v in cell.items()}
    ok = all(r["returncode"] == r["expected_returncode"] and not r["timed_out"] and r["prefix_mismatch"] == 0
             for v in cell.values() for r in v)
    sp = cell["split"]
    dr = cell["dram"]
    cx = cell["cxl"]

    def m(rs, f):
        return st.median(f(r) for r in rs) / MiB

    rows.append(dict(
        trace=t, stage=s, command=sp[0]["command"][:60], reps=min(len(v) for v in cell.values()), all_ok=ok,
        dram_s=round(med["dram"], 3), cxl_s=round(med["cxl"], 3), split_s=round(med["split"], 3),
        cxl_tax=round(med["cxl"] / med["dram"] - 1, 4), split_tax=round(med["split"] / med["dram"] - 1, 4),
        dram_spread=round((max(r["wall_s"] for r in dr) - min(r["wall_s"] for r in dr)) / med["dram"], 4),
        split_spread=round((max(r["wall_s"] for r in sp) - min(r["wall_s"] for r in sp)) / med["split"], 4),
        dram_cpu_s=round(cpu["dram"], 2), split_cpu_s=round(cpu["split"], 2), cxl_cpu_s=round(cpu["cxl"], 2),
        cxl_cpu_tax=round(cpu["cxl"] / cpu["dram"] - 1, 4), split_cpu_tax=round(cpu["split"] / cpu["dram"] - 1, 4),
        # split: state left by earlier commands, in CXL when the command starts; new file pages the command leaves in DRAM
        split_prior_file_cxl_mib=round(m(sp, lambda r: r["before"].get("file_N2", 0)), 1),
        split_prior_anon_cxl_mib=round(m(sp, lambda r: r["before"].get("anon_N2", 0)), 1),
        split_new_file_dram_mib=round(m(sp, lambda r: r["after"].get("file_N0", 0) - r["before"].get("file_N0", 0)), 1),
        split_left_anon_dram_mib=round(m(sp, lambda r: r["after"].get("anon_N0", 0)), 1),
        split_file_cxl_growth_mib=round(m(sp, lambda r: r["after"].get("file_N2", 0) - r["before"].get("file_N2", 0)), 1),
        cxl_new_file_dram_mib=round(m(cx, lambda r: r["after"].get("file_N0", 0) - r["before"].get("file_N0", 0)), 1),
        dram_prior_file_cxl_mib=round(m(dr, lambda r: r["before"].get("file_N2", 0)), 1),
        loadavg_max=max(r["loadavg"][0] for v in cell.values() for r in v),
    ))
with open(sys.argv[2], "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
W = sum(r["dram_cpu_s"] for r in rows)
summary = dict(
    stages=len(rows), runs=len(runs), all_ok=all(r["all_ok"] for r in rows),
    cpu_weighted_cxl_tax=round(sum(r["cxl_tax"] * r["dram_cpu_s"] for r in rows) / W, 4),
    cpu_weighted_split_tax=round(sum(r["split_tax"] * r["dram_cpu_s"] for r in rows) / W, 4),
    cpu_weighted_cxl_cpu_tax=round(sum(r["cxl_cpu_tax"] * r["dram_cpu_s"] for r in rows) / W, 4),
    cpu_weighted_split_cpu_tax=round(sum(r["split_cpu_tax"] * r["dram_cpu_s"] for r in rows) / W, 4),
    max_split_cpu_tax=max(r["split_cpu_tax"] for r in rows),
    median_cxl_tax=round(st.median(r["cxl_tax"] for r in rows), 4),
    median_split_tax=round(st.median(r["split_tax"] for r in rows), 4),
    max_split_tax=max(r["split_tax"] for r in rows),
    median_dram_spread=round(st.median(r["dram_spread"] for r in rows), 4),
    split_prior_file_cxl_mib_median=st.median(r["split_prior_file_cxl_mib"] for r in rows),
    split_prior_file_cxl_mib_max=max(r["split_prior_file_cxl_mib"] for r in rows),
    split_new_file_dram_mib_median=st.median(r["split_new_file_dram_mib"] for r in rows),
    split_new_file_dram_mib_max=max(r["split_new_file_dram_mib"] for r in rows),
)
json.dump(dict(summary=summary, stages=rows), open(sys.argv[3], "w"), indent=1)
print(json.dumps(summary, indent=1))
for r in rows:
    print(f"{r['trace'][:28]:28} {r['stage']:3} dram {r['dram_s']:7.2f} cxl {r['cxl_s']:7.2f} ({r['cxl_tax']:+.1%}) "
          f"split {r['split_s']:7.2f} ({r['split_tax']:+.1%}) cpu-tax cxl {r['cxl_cpu_tax']:+.0%} split {r['split_cpu_tax']:+.1%} spread {r['dram_spread']:.1%} prior-CXL {r['split_prior_file_cxl_mib']:6.0f} MiB "
          f"new-DRAM {r['split_new_file_dram_mib']:6.0f} MiB ok {r['all_ok']}")
