#!/usr/bin/env python3
"""Figures for RESULTS-fleet.md from the validation reports and the fleet.py sweeps (.local/report)."""

import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
REPORT = HERE / ".local/report"
OUT = HERE / "figures"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA, YELLOW, RED = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e34948"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "axes.titlecolor": INK, "axes.titlesize": 11.5, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8,
    "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE, "legend.frameon": False})


def results(name):
    return json.loads((REPORT / name).read_text())["results"]


def mean(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return statistics.mean(values) if values else float("nan")


def validation():
    """Left: wait removed by sharing cores, measured vs fluid model. Right: measured/model total wait by pool size."""
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.4))
    for name, color, label in (("validate-overnight-carry.json", BLUE, "short traces, 4x5 cores (V1)"),
                               ("validate-v-carry.json", ORANGE, "all traces, 2x5 cores (V2)")):
        report = [r for r in json.loads((REPORT / name).read_text()) if "checks" in r]
        index, sizes = {}, {}
        for r in report:
            c = r["config"]
            if c["mechanism"] != "none" or c["rl"] or c.get("gap_scale", 1) != 1:
                continue
            w = next(x for x in r["checks"] if x["metric"] == "wait/run")
            index[(c["per_node"], c["seed"], c["pool"])] = (w["measured"], w["model"])
            pool = {"none": c["cores"], "socket": c["cores"] * c["nodes"] // 2, "all": c["cores"] * c["nodes"]}[c["pool"]]
            if w["measured"] >= 0.02:
                sizes.setdefault(pool, []).append(w["measured"] / max(w["model"], 1e-4))
        xs, ys = [], []
        for (k, s, pool), (measured, model) in index.items():
            if pool != "none":
                continue
            for other in ("socket", "all"):
                if (k, s, other) in index:
                    xs.append((model - index[(k, s, other)][1]) * 100)
                    ys.append((measured - index[(k, s, other)][0]) * 100)
        left.plot(xs, ys, "o", color=color, ms=7, markeredgecolor=SURFACE, markeredgewidth=1.5, label=label)
        pools = sorted(sizes)
        right.plot(pools, [statistics.median(sizes[p]) for p in pools], "o-", color=color, lw=2, ms=7, label=label)
    top = max(left.get_xlim()[1], left.get_ylim()[1])
    left.plot([0, top], [0, top], color=INK2, lw=1, ls="--")
    left.text(top * .55, top * .5, "model = measured", color=INK2, fontsize=8, rotation=35)
    left.set(xlabel="fluid model (points of CPU)", ylabel="measured (points of CPU)",
             title="Waiting removed when nodes share cores")
    left.legend(fontsize=8.5, loc="upper left")
    right.axhline(1, color=INK2, lw=1, ls="--")
    right.set(xscale="log", xlabel="cores sharing one run queue set (pool size)", ylabel="measured / fluid model, total wait",
              title="Waiting the model leaves out grows with pool size\n(kernel scheduling; moving VMs cannot remove it)")
    right.set_xticks([5, 10, 20], ["5", "10", "20 (2 sockets)"])
    right.minorticks_off()
    right.legend(fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "fleet-1-validation.png", dpi=160)


def node_size():
    fig, ax = plt.subplots(figsize=(10, 4.4))
    for vcpus, color in ((1, BLUE), (4, ORANGE)):
        rows = [r for r in results(f"S-v{vcpus}.json") if r.get("stage") == "S1" and r["pool"] == "none"]
        for util, style in ((80, "-"), (90, "--")):
            cores = sorted({r["cores"] for r in rows})
            lost = [mean([r for r in rows if r["cores"] == c and r["target_util"] == util], "lost_over_run") * 100 for c in cores]
            ax.plot(cores, lost, style, marker="o", color=color, lw=2, ms=6,
                    label=f"{vcpus} vCPU sandboxes, {util}% target utilization")
    ax.axhline(1, color=INK2, lw=1, ls=":")
    ax.text(5.2, 1.15, "1% of CPU", color=INK2, fontsize=8)
    ax.axvline(192, color=GRID, lw=6, zorder=0)
    ax.text(150, 40, "DSec-size\nnode", color=INK2, fontsize=8, ha="right")
    ax.set(xscale="log", yscale="log", ylim=(0.01, 100), xlabel="cores per node (16 nodes, power-of-2 placement)",
           ylabel="lost CPU, split nodes (% of CPU used)", title="Node-level contention shrinks fast with node size")
    ax.set_xticks([5, 10, 20, 48, 96, 192], ["5", "10", "20", "48", "96", "192"])
    ax.legend(fontsize=8.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(OUT / "fleet-2-node-size.png", dpi=160)


def capacity():
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4), sharey=True)
    lines = [(("none", "none"), INK2, "no moves"), (("hand-off 10ms", "none"), BLUE, "hand-off 10 ms"),
             (("migration pre-copy", "none"), AQUA, "migration pre-copy, 100 Gb/s"),
             (("migration stop-and-copy", "none"), RED, "migration stop-and-copy, 100 Gb/s"),
             (("none", "all"), YELLOW, "ideal: all nodes share cores")]
    for ax, vcpus in zip(axes, (4, 1)):
        rows = results(f"S6-v{vcpus}.json")
        utils = sorted({r["target_util"] for r in rows})
        for (mechanism, pool), color, label in lines:
            pts = [(mean([r for r in rows if r["target_util"] == u and r["mechanism"] == "none" and r["pool"] == "none"],
                         "run_cores_per_node") / 192 * 100,
                    mean([r for r in rows if r["target_util"] == u and r["mechanism"] == mechanism and r["pool"] == pool],
                         "lost_over_run") * 100) for u in utils]
            ax.plot([p[0] for p in pts], [max(p[1], 1e-3) for p in pts], marker="o", color=color, lw=2, ms=5, label=label)
        ax.axhline(1, color=INK2, lw=1, ls=":")
        ax.text(74.3, 1.12, "1% of CPU", color=INK2, fontsize=8)
        ax.axvline(80, color=INK2, lw=1, ls="--")
        ax.text(80.3, 0.006, "DSec: cloud bursting above 80%", color=INK2, fontsize=8)
        ax.set(xlim=(74, 97), yscale="log", ylim=(0.005, 15), xlabel="node utilization (%)",
               title=f"{vcpus} vCPU sandboxes, 16 x 192-core nodes")
    axes[0].set_ylabel("lost CPU (% of CPU used)")
    axes[1].legend(fontsize=8, loc="center left", bbox_to_anchor=(0, .4))
    fig.suptitle("How hot nodes can run before 1% of CPU is lost to node-level contention", x=.01, ha="left",
                 fontsize=12, weight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, .94))
    fig.savefig(OUT / "fleet-3-capacity.png", dpi=160)


def mechanisms():
    options = [("instant", 37, "instant"), ("hand-off 1ms", 37, "hand-off\n1 ms"), ("hand-off 10ms", 37, "hand-off\n10 ms"),
               ("migration pre-copy", 100, "migration\npre-copy\n100 Gb/s"), ("migration pre-copy", 37, "migration\npre-copy\n37 Gb/s"),
               ("migration stop-and-copy", 100, "migration\nstop-and-copy\n100 Gb/s"),
               ("migration stop-and-copy", 37, "migration\nstop-and-copy\n37 Gb/s")]
    conditions = [(4, 192, 85, BLUE), (4, 192, 90, ORANGE), (1, 192, 95, AQUA)]
    fig, ax = plt.subplots(figsize=(12, 4.8))
    x = np.arange(len(options))
    w = .26
    for j, (vcpus, cores, util, color) in enumerate(conditions):
        rows = [r for r in results(f"S-v{vcpus}.json") if r.get("stage") == "S4" and r["cores"] == cores and r["target_util"] == util]
        base = mean([r for r in rows if r["mechanism"] == "none" and r["pool"] == "none"], "lost_over_run")
        actual = mean([r for r in rows if r["mechanism"] == "none" and r["pool"] == "none"], "run_cores_per_node") / cores * 100
        vals = []
        for mechanism, nic, _ in options:
            best = min(mean([r for r in rows if r["mechanism"] == mechanism and r.get("nic_gbps") == nic and r["detect_ms"] == d
                             and r["hold_s"] == h], "lost_over_run") for d, h in ((50, 1.0), (500, 1.0), (500, 5.0), (2000, 5.0)))
            vals.append((1 - best / base) * 100)
        offset = (j - 1) * (w + .02)
        ax.bar(x + offset, vals, w, color=color,
               label=f"{vcpus} vCPU, {cores} cores, {actual:.0f}% utilization (lost without moves: {base * 100:.1f}%)")
        for i, v in enumerate(vals):
            ax.text(x[i] + offset, v + (2 if v >= 0 else -2), f"{v:.0f}", ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=7.5, color=INK)
    ax.axhline(0, color=INK, lw=1)
    ax.set_xticks(x, [o[2] for o in options], fontsize=8.5)
    ax.set(ylim=(-95, 110), ylabel="lost CPU recovered vs. no moves (%)",
           title="Moving VMs: a 1-10 ms hand-off recovers most of the loss, full VM migration does not")
    ax.grid(axis="x", visible=False)
    ax.legend(fontsize=8, loc="lower left")
    fig.text(.01, .01, "fleet.py fluid model, 16 nodes, best of 4 detection/hold policies, 3 seeds. Lost CPU = runqueue wait "
             "+ work stalled while a VM is paused. Migration: 0.30 s/GiB dump on a source core + memory/NIC transfer.",
             fontsize=7.5, color=INK2)
    fig.tight_layout(rect=(0, .04, 1, 1))
    fig.savefig(OUT / "fleet-4-mechanisms.png", dpi=160)


def main():
    OUT.mkdir(exist_ok=True)
    validation()
    node_size()
    capacity()
    mechanisms()
    print(sorted(p.name for p in OUT.glob("fleet-*.png")))


if __name__ == "__main__":
    main()
