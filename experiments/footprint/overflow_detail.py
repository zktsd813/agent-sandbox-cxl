#!/usr/bin/env python3
"""Detail of the 11-19 overflow (swap) runs and of page-cache refaults in the fill runs.

  overflow_detail.py [QUEUE_DIR]

- Per 2 min of each overflow run: live, anon, file, swap, memory PSI (full), refaults, major faults, OOM kills.
- Real overcommit: peak (anon + swap + kernel) / budget. The run names use 1-rl's whole use including file cache.
- Command slowdown by phase (0-4 min boot, 4-12 min swap filling, 12-22 min after): CPU-weighted ratio, extra time,
  share of commands over 2x, heavy (baseline >= 1 s) p99 and max; heavy p99 after settle with a bootstrap 95% interval.
  Baseline = median host time of the same (trace, command index) over all of 1-rl; network commands are left out.
- The slowest commands of the 1.30x run while swap filled.
- VM boot time per step of 1-ci (page cache of the shared image squeezed out at the last step).
"""
import collections
import csv
import json
import sys
from pathlib import Path

import numpy as np

FP = Path(__file__).resolve().parent
sys.path.insert(0, str(FP))
import to_sim  # noqa: E402

Q = Path(sys.argv[1]) if len(sys.argv) > 1 else FP / ".local/queue/20261118T2014"
RUNS = ("2-rl-100", "2-rl-80", "2-rl-65")
PHASES = ((0, 4, "boot"), (4, 12, "swap filling"), (12, 23, "after"))
GIB = 2 ** 30


def baseline():
    run, meta, steps, boxes = to_sim.load_run(Q / "1-rl")
    cmds = to_sim.commands(boxes, to_sim.command_texts(meta, boxes), True)
    return to_sim.first_step_baseline(run, meta, steps, cmds, to_sim.load_csv(run / "node.csv"), 600, True)


def series(name):
    node = to_sim.load_csv(Q / name / "node.csv")
    t = (node["t_ns"] - node["t_ns"][0]) / 1e9
    budget = json.loads((Q / name / "run.json").read_text())["budget_bytes"] / GIB
    demand = (node["cg_anon"] + node["swap_current"] + node["cg_kernel"]) / GIB
    k = int(np.argmax(demand))
    oom = [round(float(t[i]) / 60, 1) for i in range(1, len(t)) if node["oom_kill"][i] != node["oom_kill"][i - 1]]
    print(f"=== {name}: budget {budget:.1f} GiB, peak demand {demand[k]:.1f} GiB at {t[k]/60:.1f} min, "
          f"real overcommit {demand[k]/budget:.2f}, swap max {node['swap_current'].max()/GIB:.1f} GiB, OOM at min {oom}")
    print("  min  live  anonG  fileG  swapG  psi_full%  refF/s  refA/s  majf/s  oom")
    edges = np.arange(0, t[-1] + 120, 120)
    for a, b in zip(edges[:-1], edges[1:]):
        m = (t >= a) & (t < b)
        if m.sum() < 2:
            continue
        i0, i1 = np.where(m)[0][[0, -1]]
        dt = t[i1] - t[i0]
        d = lambda c: (node[c][i1] - node[c][i0]) / dt
        print(f"  {a/60:4.0f} {node['live'][m].mean():5.0f} {node['cg_anon'][i1]/GIB:6.1f} {node['cg_file'][i1]/GIB:6.1f} "
              f"{node['swap_current'][i1]/GIB:6.1f} {d('psi_mem_full_us')/1e4:9.2f} {d('refault_file'):7.0f} "
              f"{d('refault_anon'):7.0f} {d('pgmajfault'):7.0f} {int(node['oom_kill'][i1])}")


def command_rows(name, base):
    run, meta, steps, boxes = to_sim.load_run(Q / name)
    texts = to_sim.command_texts(meta, boxes)
    t0 = to_sim.load_csv(run / "node.csv")["t_ns"][0]
    rows = []
    for b in boxes:
        cs = [c for c in b.get("commands", []) if c.get("host_end_ns") and c.get("rc") is not None]
        for c in cs:
            k = (b["trace"], c["index"])
            if k not in base or base[k] < 0.05 or to_sim.NETWORK.search(texts.get(k, "")):
                continue
            rows.append(((c["host_start_ns"] - t0) / 6e10, (c["host_end_ns"] - c["host_start_ns"]) / 1e9, base[k],
                         texts.get(k, "")[:70].replace("\n", " ")))
    return rows


def phases(name, rows):
    a = np.array([r[:3] for r in rows])
    for lo, hi, lab in PHASES:
        m = (a[:, 0] >= lo) & (a[:, 0] < hi)
        d, bs = a[m, 1], a[m, 2]
        ex, r, h = (d - bs) * 1000, d / bs, bs >= 1
        print(f"  {lo:>2}-{hi:<2} min {lab:13s} n={m.sum():5d} cpu-wt {d.sum()/bs.sum():.3f} extra mean {ex.mean():4.0f} ms "
              f"p99 {np.percentile(ex, 99):5.0f} ms >2x {np.mean(r > 2):.3f} heavy n={h.sum()} p99 "
              f"{np.percentile(r[h], 99):.2f} max {r[h].max():.2f}")
    m = a[:, 0] >= 10  # the window of the summary tables (settle 600 s)
    r = a[m, 1][a[m, 2] >= 1] / a[m, 2][a[m, 2] >= 1]
    boot = np.random.default_rng(1).choice(r, (2000, len(r)))
    ci = np.percentile(np.percentile(boot, 99, axis=1), [2.5, 97.5])
    print(f"  after 10 min: heavy n={len(r)} p99 {np.percentile(r, 99):.2f} (95% interval {ci[0]:.2f}-{ci[1]:.2f})")


def slowest(rows, n=12):
    sel = [(d - b, d / b, b, txt) for m, d, b, txt in rows if 4 <= m < 12]
    sel.sort(reverse=True)
    for ex, r, b, txt in sel[:n]:
        print(f"  +{ex:5.2f} s x{r:4.1f} base {b:5.2f} s  {txt}")
    ex = np.array([s[0] for s in sel if s[0] > 0])
    print(f"  top 1% of commands carry {ex[: max(1, len(sel) // 100)].sum() / ex.sum():.2f} of the extra time")


def ci_boot():
    run, meta, steps, boxes = to_sim.load_run(Q / "1-ci")
    ramps = [json.loads(l) for l in open(Q / "1-ci/steps.jsonl") if '"kind": "ramp"' in l]
    edges = [r["t_ns"] for r in ramps] + [float("inf")]
    for k, r in enumerate(ramps):
        b = [(x["ready_ns"] - x["start_ns"]) / 1e6 for x in boxes
             if x.get("ready_ns") and edges[k] <= x["start_ns"] < edges[k + 1]]
        if b:
            print(f"  1-ci target {r['target']}: boots {len(b)}, ms p50 {np.median(b):.0f} p90 {np.percentile(b, 90):.0f} "
                  f"p99 {np.percentile(b, 99):.0f}")


def main():
    base = baseline()
    for name in RUNS:
        series(name)
        rows = command_rows(name, base)
        phases(name, rows)
        if name == "2-rl-65":
            print("  slowest commands while swap filled:")
            slowest(rows)
    print("=== VM boot time, 1-ci")
    ci_boot()


if __name__ == "__main__":
    main()
