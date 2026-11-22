#!/usr/bin/env python3
"""Sandboxes per core under the SLO and a memory ceiling, for image co-location chunk b (2026-11-18).

The CPU side comes from imgcap.py runs (memory not limited). The memory side is applied per density point:
  memory per sandbox m(b) = R - u + u / b_eff + S / n
    R      time-average resident memory per running sandbox of the run (hwm model, Docker; it holds that sandbox's own
           copy of its task-only image pages)
    u      mean task-only image working set, 0.1385 GiB (image_place.py, 264 traces)
    b_eff  siblings sharing one copy on a node: 8 / expected distinct nodes of 8/b chunks on 16 nodes
    S      single copy of the layers shared across tasks, 2.86 GiB per node (upper bound), n = sandboxes per node
With CXL, only the part of memory beyond DRAM sits in CXL. Its share f = max(0, 1 - DRAM / need) sets the tax: the
metrics are interpolated between the no-tax run and the run with all state in CXL (same density, same moves).
A point passes when CPU-weighted slowdown <= 1.10, heavy p99 <= 2.0 and memory need <= DRAM + CXL. The capacity is
the largest passing running_per_core, interpolated at the first failing point.

  imgcap_analyze.py RUNS.jsonl [--json OUT]
"""
import argparse, json
from collections import defaultdict
import numpy as np

U, S, G, N = 0.1385, 2.86, 8, 16
SLO = dict(cpu_weighted=1.10, heavy_p99=2.0)
MET = ("running_per_core", "cpu_weighted", "heavy_p99", "resident_gib_per_core", "util", "read_s_per_sandbox",
       "moves_per_s", "cpu_cv")
# label, run without tax, run with all state in CXL, DRAM GiB/core, CXL GiB/core
CONFIGS = [("DRAM 2", "dram", None, 2.0, 0.0), ("DRAM 4", "dram", None, 4.0, 0.0),
           ("DRAM 2 + CXL 1", "dram", "cxl", 2.0, 1.0), ("DRAM 2 + CXL 2", "dram", "cxl", 2.0, 2.0),
           ("DRAM 2 + CXL 1, hand-off", "handoff-notax", "cxl-handoff", 2.0, 1.0),
           ("DRAM 2 + CXL 2, hand-off", "handoff-notax", "cxl-handoff", 2.0, 2.0)]


def b_eff(b):
    k = G // b
    return G / (N * (1 - (1 - 1 / N) ** k))


def points(rs):
    by = defaultdict(list)
    for r in rs:
        by[r["density"]].append(r)
    return {d: {m: float(np.mean([r[m] for r in g])) for m in MET} for d, g in by.items()}


def need(p, b, cores):
    """Memory per core the running sandboxes need (GiB)."""
    run = p["running_per_core"]
    R = p["resident_gib_per_core"] / run
    return run * (R - U + U / b_eff(b) + S / (run * cores))


def series(base, taxed, b, cores, dram):
    out = []
    for d in sorted(base):
        p = dict(base[d], density=d)
        p["need"] = need(base[d], b, cores)
        f = max(0.0, 1.0 - dram / p["need"]) if taxed is not None else 0.0
        if taxed is not None and f > 0:
            if d not in taxed:
                continue
            for m in ("running_per_core", "cpu_weighted", "heavy_p99", "util"):
                p[m] = (1 - f) * base[d][m] + f * taxed[d][m]
        p["cxl_share"] = f
        out.append(p)
    return out


def capacity(pts, mem):
    def margins(p):
        m = {k: v - p[k] for k, v in SLO.items()}
        m["memory"] = mem - p["need"]
        return m
    last = None
    for p in pts:
        mg = margins(p)
        if min(mg.values()) >= 0:
            last = p
            continue
        if last is None:
            return None, "below-grid", p
        lm = margins(last)
        ts = {k: lm[k] / (lm[k] - mg[k]) for k in mg if mg[k] < 0 and lm[k] - mg[k] > 0}
        binding = min(ts, key=ts.get) if ts else min(mg, key=mg.get)
        t = max(0.0, min(1.0, ts.get(binding, 0.0)))
        run = last["running_per_core"] + t * (p["running_per_core"] - last["running_per_core"])
        return run, "memory" if binding == "memory" else "cpu", last
    return pts[-1]["running_per_core"], "grid-top", pts[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs")
    ap.add_argument("--json")
    a = ap.parse_args()
    groups = defaultdict(list)
    for line in open(a.runs):
        r = json.loads(line)
        groups[(r["cores"], r["gap_scale"], r["jitter_s"], r["setup"], r["chunk"])].append(r)
    out = []
    for cores, gap, jit in sorted({k[:3] for k in groups}):
        print(f"\n== {cores} cores, LLM wait x{gap}, sibling jitter {jit} s  (sandboxes/core under SLO and memory)")
        print(f"{'config':28s}" + "".join(f"{'b=' + str(b):>16s}" for b in (1, 2, 4, 8)) + "   best vs b=1")
        for label, base_s, tax_s, dram, cxl in CONFIGS:
            cells, caps = [], {}
            for b in (1, 2, 4, 8):
                base = points(groups[(cores, gap, jit, base_s, b)])
                taxed = points(groups[(cores, gap, jit, tax_s, b)]) if tax_s else None
                cap, lim, p = capacity(series(base, taxed, b, cores, dram), dram + cxl)
                caps[b] = cap
                cells.append(f"{cap:9.3f} {lim[:6]:>6s}" if cap is not None else f"{'<grid':>9s} {'':>6s}")
                out.append(dict(cores=cores, gap_scale=gap, jitter_s=jit, config=label, dram=dram, cxl=cxl, chunk=b,
                                capacity=cap, limit=lim, cxl_share=p.get("cxl_share") if p else None))
            ok = {b: c for b, c in caps.items() if c is not None}
            bb = max(ok, key=ok.get) if ok else None
            gain = f"b={bb} {ok[bb] / caps[1]:.2f}x" if bb and caps.get(1) else ""
            print(f"{label:28s}" + "".join(cells) + "   " + gain)
        print("  CPU-weighted slowdown / heavy p99 / first-read s per sandbox at one density, by b:")
        for setup in ("dram", "handoff-notax"):
            base = points(groups[(cores, gap, jit, setup, 1)])
            d_ref = sorted(base)[len(base) // 2]
            cells = []
            for b in (1, 2, 4, 8):
                p = points(groups[(cores, gap, jit, setup, b)])[d_ref]
                cells.append(f"b={b} {p['cpu_weighted']:.3f}/{p['heavy_p99']:.2f}/{p['read_s_per_sandbox']:.2f}")
            print(f"    {setup:13s} density {d_ref}: " + "  ".join(cells))
    if a.json:
        open(a.json, "w").write(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
