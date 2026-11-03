#!/usr/bin/env python3
"""Figures for docs/results/virtual-node-rl-20261026 from report_rl.py output (one or more runs.csv) and run dirs.

1-wait-vs-util.png   runqueue wait / run vs node utilization, ISO vs POOL (each server a marker)
2-episodes.png       lost-CPU-weighted CDF of contention-episode length (ISO runs), move latencies marked
3-recovery.png       share of ISO lost CPU removed: POOL (ideal), emulated hand-off / pre-copy, latency upper bounds
4-breakdown.png      where commands' extra time goes (runqueue wait / slower CPU / other)
"""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MOVES = {"1 ms": 0.001, "10 ms hand-off": 0.010, "100 ms": 0.1, "pre-copy 2 GiB (1.4 s)": 1.42}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, action="append", required=True, help="runs.csv[:label]")
    parser.add_argument("--runs-root", type=Path, action="append", required=True, help="dir containing the run dirs")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for spec, root in zip(args.table, args.runs_root):
        path, _, label = str(spec).partition(":")
        for r in csv.DictReader(open(path)):
            r["server"] = label or "bud05"
            r["dir"] = root / r["run"]
            rows.append(r)
    args.out.mkdir(parents=True, exist_ok=True)
    f = lambda r, k: float(r[k]) if r[k] not in ("", "None") else None

    # 1. wait vs utilization
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    styles = {("none", "none"): ("ISO (2 x 15 cores)", "o", "#c0392b"), ("all", "none"): ("POOL (30 cores)", "s", "#2471a3"),
              ("none", "hand-off"): ("ISO + 10 ms hand-off", "^", "#7d3c98"),
              ("none", "pre-copy"): ("ISO + pre-copy migration", "v", "#7f8c8d")}
    seen = set()
    for r in rows:
        key = (r["pool"], r["move"])
        if key not in styles:
            continue
        label, marker, color = styles[key]
        ax.scatter(f(r, "utilization") * 100, f(r, "wait_over_run") * 100, marker=marker, color=color,
                   facecolors="none" if r["server"] != "bud05" else color, s=46,
                   label=label if key not in seen else None)
        seen.add(key)
        ax.annotate(f"x{r['llm_scale']}/{float(r['sandboxes_per_core']):.1f}", (f(r, "utilization") * 100, f(r, "wait_over_run") * 100),
                    fontsize=6, xytext=(3, 3), textcoords="offset points")
    ax.set(xlabel="CPU utilization of the 30 cores (%)", ylabel="runqueue wait / run time (%)",
           title="RL rollouts, 1 vCPU microVMs (bud05; seed 1 and paired seed p1)\nlabel: LLM speed scale / sandboxes per core")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(args.out / "1-wait-vs-util.png", dpi=160)

    # 2. episode length CDF
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    for r in rows:
        if r["pool"] != "none" or r["move"] != "none" or r["server"] != "bud05":
            continue
        path = r["dir"] / "episodes-1ms.csv"
        if not path.exists():
            continue
        pairs = sorted((float(e["length_s"]), float(e["lost_s"])) for e in csv.DictReader(path.open()))
        total = sum(l for _, l in pairs)
        if not total:
            continue
        acc, xs, ys = 0.0, [], []
        for length, lost in pairs:
            acc += lost
            xs.append(length)
            ys.append(acc / total)
        ax.step(xs, ys, where="post", label=f"x{r['llm_scale']}, {float(r['sandboxes_per_core']):.1f}/core, util {f(r, 'utilization'):.0%}")
    for name, latency in MOVES.items():
        ax.axvline(latency, color="gray", lw=.8, ls="--")
        ax.text(latency, 0.02, name, rotation=90, fontsize=6, va="bottom")
    ax.set(xscale="log", xlabel="contention episode length (s), node wait >= 0.1 core per 1 ms",
           ylabel="CDF of lost CPU time", title="Lost-CPU-weighted episode length, ISO nodes (bud05)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(args.out / "2-episodes.png", dpi=160)

    # 3. recovery for the conditions that have a POOL pair
    groups = {}
    for r in rows:
        groups.setdefault((r["server"], r["llm_scale"], r["per_node"]), []).append(r)
    fig, ax = plt.subplots(figsize=(7, 4.2))
    bars, labels = [], []
    for (server, scale, per_node), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], float(kv[0][1]))):
        iso = next((r for r in rs if r["pool"] == "none" and r["move"] == "none" and r["placement"] == "pack"), None)
        if not iso or not f(iso, "wait_over_run"):
            continue
        base = f(iso, "wait_over_run")
        entry = {"POOL": next((1 - f(r, "wait_over_run") / base for r in rs if r["pool"] == "all"), None),
                 "hand-off (emulated)": next((1 - f(r, "wait_over_run") / base for r in rs if r["move"] == "hand-off"), None),
                 "pre-copy (emulated)": next((1 - f(r, "wait_over_run") / base for r in rs if r["move"] == "pre-copy"), None),
                 "spread placement": next((1 - f(r, "wait_over_run") / base for r in rs
                                           if r["placement"] == "spread" and r["pool"] == "none" and r["move"] == "none"), None)}
        summary = json.loads((iso["dir"] / "contention-1ms.json").read_text())
        bound = summary["recoverable_upper_bound_by_move_latency"]
        entry["bound 10 ms"] = bound.get("10ms")
        bars.append(entry)
        labels.append(f"{server}\nx{scale}, {float(iso['sandboxes_per_core']):.1f}/core\nutil {f(iso, 'utilization'):.0%}")
    keys = ["POOL", "bound 10 ms", "hand-off (emulated)", "pre-copy (emulated)", "spread placement"]
    width = 0.16
    for i, k in enumerate(keys):
        ax.bar([x + i * width for x in range(len(bars))], [b[k] if b[k] is not None else 0 for b in bars], width,
               label=k)
    ax.axhline(0, color="black", lw=.6)
    ax.set_xticks([x + 2 * width for x in range(len(bars))], labels, fontsize=7)
    ax.set(ylabel="share of ISO runqueue wait removed", title="What closes the gap (ISO pack = 0)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(args.out / "3-recovery.png", dpi=160)

    # 4. slowdown breakdown
    fig, ax = plt.subplots(figsize=(7, 4.2))
    names, parts = [], []
    for r in rows:
        path = r["dir"] / "slowdown-breakdown.json"
        if r["server"] != "bud05" or not path.exists():
            continue
        b = json.loads(path.read_text())["all"]
        names.append(f"{r['name']}\nslowdown {b['slowdown_mean']:.2f}")
        parts.append((b["wait_share"], b["cpu_share"], b["other_share"]))
    bottoms = [0] * len(parts)
    for i, (label, color) in enumerate((("runqueue wait", "#c0392b"), ("slower CPU (freq/cache/memory)", "#e67e22"),
                                        ("other (I/O, network)", "#95a5a6"))):
        values = [max(0, p[i]) for p in parts]
        ax.bar(range(len(parts)), values, bottom=bottoms, label=label, color=color)
        bottoms = [b + v for b, v in zip(bottoms, values)]
    ax.set_xticks(range(len(parts)), names, fontsize=6, rotation=30, ha="right")
    ax.set(ylabel="share of extra command time vs solo", title="Where commands' extra time goes (solo >= 0.5 s)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(args.out / "4-breakdown.png", dpi=160)
    print(args.out)


if __name__ == "__main__":
    main()
