#!/usr/bin/env python3
"""Plain figures that tell the motivation story from the overnight pilot runs and the validated simulator.

1. lost-cpu:     measured CPU waiting, split vs pooled cores
2. model-check:  simulator (sim.py) against the measured totals
3. move-latency: lost CPU recovered by moving VMs, by move mechanism (simulator)

Run with a Python that has numpy and matplotlib (e.g. .local/microvm/analysis-env).
"""

import csv
import glob
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
RUNS = HERE / ".local/runs"
OUT = HERE / "figures"

# Reference palette (dataviz skill): categorical slots 1-2, blue ordinal steps, diverging red pole.
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, RED = "#2a78d6", "#eb6834", "#e34948"
BLUE_STEPS = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
BLUE_LIGHT = "#b7d3f6"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "axes.titlecolor": INK, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8,
    "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE, "legend.frameon": False})

DENSITIES = [10, 14, 18, 21, 24, 27]  # sandboxes per 5-core node


def runs(per_node, pool, vcpus=1, group=0, gap=1):
    found = []
    for path in sorted(RUNS.glob(f"*-pilot-k{per_node}-{pool}-v{vcpus}-s*")):
        config = json.loads((path / "run.json").read_text())
        if config["group_size"] == group and config["gap_scale"] == gap and str(config["seed"]) != "smoke":
            found.append(path)
    return found


def mean(values):
    return statistics.mean(values)


def note(fig, text):
    fig.text(0.01, 0.01, text, fontsize=8, color=INK2, ha="left", va="bottom")


def timeline():
    """One partitioned node at DSec-like density: runnable vCPUs vs its 5 cores over 3 seconds."""
    run = runs(14, "none")[0]
    data = np.loadtxt(run / "cpu1ms.csv", delimiter=",", skiprows=1, dtype=np.int64)
    config = json.loads((run / "run.json").read_text())
    start = data[0, 0] + int((config["ramp_s"] + 60) * 1e9)
    data = data[data[:, 0] >= start]
    demand = (data[:, 2] + data[:, 3]) / data[:, 1]  # node 0: running + waiting vCPUs
    span = 3000
    # Show the 3 s window with the most waiting time in the first 5 minutes, so the burst pattern is visible.
    waiting = np.convolve(np.maximum(0, demand - 5), np.ones(span), mode="valid")[:300_000]
    begin = int(np.argmax(waiting))
    t = np.arange(span) / 1000
    d = demand[begin:begin + span]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.fill_between(t, 0, np.minimum(d, 5), step="post", color=BLUE, alpha=.18, lw=0)
    ax.step(t, d, where="post", color=BLUE, lw=1.2, label="vCPUs that want to run")
    ax.fill_between(t, 5, np.maximum(d, 5), step="post", color=ORANGE, alpha=.55, lw=0, label="waiting for a core (lost CPU)")
    ax.axhline(5, color=INK, lw=1.5)
    ax.text(t[-1], 5.15, "node capacity: 5 cores", ha="right", va="bottom", fontsize=9, color=INK)
    ax.set(xlim=(0, t[-1]), ylim=(0, max(9, d.max() + 1)), xlabel="time (s)", ylabel="vCPUs")
    ax.set_title("1. Contention comes in millisecond bursts", pad=22)
    ax.text(0, 1.02, "one 5-core node, 14 sandboxes (2.8 per core, DSec-like density), 1 ms samples",
            transform=ax.transAxes, fontsize=9, color=INK2)
    ax.legend(loc="upper left", ncol=2, fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "1-timeline.png", dpi=160)


def lost_cpu():
    """Runqueue wait as a share of CPU used: split nodes vs the same load with shared cores."""
    split, pooled = [], []
    for k in DENSITIES:
        split.append(mean(json.loads((r / "contention-1ms.json").read_text())["total_wait_over_run"] for r in runs(k, "none")))
        pooled.append(mean(json.loads((r / "contention-1ms.json").read_text())["total_wait_over_run"] for r in runs(k, "all")))
    x = np.arange(len(DENSITIES))
    w = .36
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.bar(x - w / 2 - .01, np.array(split) * 100, w, color=BLUE, label="split: each node uses only its own 5 cores")
    ax.bar(x + w / 2 + .01, np.array(pooled) * 100, w, color=ORANGE, label="pooled: idle cores of other nodes usable at once")
    for i, (s, p) in enumerate(zip(split, pooled)):
        ax.text(x[i] - w / 2, s * 100 + 1, f"{s:.1%}", ha="center", fontsize=8, color=INK2)
        ax.text(x[i] + w / 2, p * 100 + 1, f"{p:.1%}", ha="center", fontsize=8, color=INK2)
        ax.text(x[i], max(s, p) * 100 + 6, f"−{1 - p / s:.0%}", ha="center", fontsize=10, color=INK, weight="bold")
    marks = {14: "\nDSec microVM peak", 21: "\nDSec microVM max"}
    ax.set_xticks(x, [f"{k / 5:g}{marks.get(k, '')}" for k in DENSITIES])
    ax.set(xlabel="sandboxes per physical core", ylabel="CPU time spent waiting (% of CPU used)", ylim=(0, 68))
    ax.grid(axis="x", visible=False)
    ax.set_title("1. Most of the waiting disappears if other nodes' idle cores can be used immediately", pad=22)
    ax.text(0, 1.02, "4 nodes × 5 cores, 1 vCPU sandboxes, mean of 3 seeds; bold = reduction from pooling",
            transform=ax.transAxes, fontsize=9, color=INK2)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "1-lost-cpu.png", dpi=160)


def bursts():
    """Share of lost CPU by the length of the contention episode it belongs to (1ms samples)."""
    edges = [(0, .0015, "1 ms"), (.0015, .0105, "2–10 ms"), (.0105, .1005, "10–100 ms"), (.1005, 1e9, "> 100 ms")]
    shares = []
    for k in DENSITIES:
        per_seed = []
        for r in runs(k, "none"):
            rows = [(float(e["length_s"]), float(e["lost_s"])) for e in csv.DictReader((r / "episodes-1ms.csv").open())]
            total = sum(l for _, l in rows)
            per_seed.append([sum(l for n, l in rows if lo <= n < hi) / total for lo, hi, _ in edges])
        shares.append(np.mean(per_seed, axis=0))
    shares = np.array(shares) * 100
    fig, ax = plt.subplots(figsize=(10, 4.2))
    y = np.arange(len(DENSITIES))
    left = np.zeros(len(DENSITIES))
    for j, (_, _, label) in enumerate(edges):
        ax.barh(y, shares[:, j], .55, left=left, color=BLUE_STEPS[j], edgecolor=SURFACE, linewidth=2, label=label)
        for i in range(len(DENSITIES)):
            if shares[i, j] >= 8:
                ax.text(left[i] + shares[i, j] / 2, y[i], f"{shares[i, j]:.0f}%", ha="center", va="center", fontsize=8,
                        color="white" if j else INK)
        left += shares[:, j]
    ax.set_yticks(y, [f"{k / 5:g} per core" for k in DENSITIES])
    ax.invert_yaxis()
    ax.set(xlim=(0, 100), xlabel="share of lost CPU time (%)")
    ax.grid(axis="y", visible=False)
    ax.set_title("3. Lost CPU comes from bursts far shorter than a VM migration (0.4–1.4 s)", pad=22)
    ax.text(0, 1.02, "length of the contention episode each lost millisecond belongs to; split nodes, mean of 3 seeds",
            transform=ax.transAxes, fontsize=9, color=INK2)
    ax.legend(loc="lower center", bbox_to_anchor=(.5, 1.07), ncol=4, fontsize=9, title="episode length", title_fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "3-bursts.png", dpi=160)


def moves():
    """Reactive balancer replay: wait removed, and what is left after paying for the moves."""
    # Per-move source CPU: ~2 ms for a state-only hand-off; 0.616 s to dump a 2 GiB guest (experiment 3).
    options = [("instant", "instant\n(ideal)", .002), ("10ms", "hand-off\n10 ms", .002), ("100ms", "100 ms", .002),
               ("0.435s", "migration\n0.44 s", .616), ("0.77s", "migration\n0.77 s", .616), ("1.417s", "migration\n1.42 s", .616)]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), sharey=True)
    for ax, k in zip(axes, (14, 21)):
        gross, net = [], []
        for key, _, cost in options:
            g, n = [], []
            for r in runs(k, "none"):
                m = json.loads((r / "moves-d10h0.1.json").read_text())
                g.append(m["recovered"][key])
                n.append((m["recovered"][key] * m["estimated_wait_s"] - m["moves"][key] * cost) / m["estimated_wait_s"])
            gross.append(mean(g) * 100)
            net.append(mean(n) * 100)
        x = np.arange(len(options))
        w = .36
        ax.bar(x - w / 2 - .01, gross, w, color=BLUE_LIGHT, label="wait removed (move treated as free)")
        ax.bar(x + w / 2 + .01, net, w, color=[BLUE if v >= 0 else RED for v in net],
               label="net, after the CPU spent on moving")
        for i, (g, n) in enumerate(zip(gross, net)):
            ax.text(x[i] - w / 2, g + 4, f"{g:.0f}", ha="center", fontsize=8, color=INK2)
            ax.text(x[i] + w / 2, n + (4 if n >= 0 else -4), f"{n:+.0f}", ha="center", va="bottom" if n >= 0 else "top",
                    fontsize=8, color=INK)
        ax.axhline(0, color=INK, lw=1)
        ax.axvspan(2.5, 5.6, color=GRID, alpha=.45, lw=0)
        ax.text(4, 108, "full VM migration (2 GiB guest)", ha="center", fontsize=9, color=INK2)
        ax.set_xticks(x, [label for _, label, _ in options], fontsize=8.5)
        ax.set(ylim=(-300, 120), title=f"{k / 5:g} sandboxes per core")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("% of the waiting time")
    axes[0].legend(loc="lower left", fontsize=9)
    fig.suptitle("4. Moving a VM pays off only when the move is fast and cheap", x=.01, ha="left", fontsize=12,
                 weight="bold", color=INK)
    note(fig, "Replay of measured per-VM demand with a reactive balancer (detect 10 ms, move the busiest VM to an "
              "idle node); mean of 3 seeds. Move cost: ~2 ms CPU per hand-off, 0.62 s CPU per 2 GiB snapshot dump; "
              "VM pause during migration not charged.")
    fig.tight_layout(rect=(0, .05, 1, .95))
    fig.savefig(OUT / "4-moves.png", dpi=160)


SIM = HERE / ".local/report"
POLICIES = ("sim-d50-h1.json", "sim-d500-h1.json", "sim-d500-h5.json", "sim-d2000-h5.json")
MOVE_ORDER = [("instant", "instant\n(ideal)"), ("hand-off 10ms", "hand-off\n10 ms"),
              ("migration 0.44s 512MiB stop-and-copy", "migration 0.44 s\n512 MiB\nstop-and-copy"),
              ("migration 1.42s 2GiB pre-copy", "migration 1.42 s\n2 GiB\npre-copy"),
              ("migration 1.42s 2GiB stop-and-copy", "migration 1.42 s\n2 GiB\nstop-and-copy")]


def sim_results(name):
    groups = {}
    for r in json.loads((SIM / name).read_text())["results"]:
        groups.setdefault((r["per_node"], r["pool"], r["mechanism"]), []).append(r)
    return groups


def model_check():
    """Simulator vs measurement: CPU waiting with split and pooled cores (no moves)."""
    sim = sim_results("sim.json")
    fig, ax = plt.subplots(figsize=(10, 4.2))
    x = [k / 5 for k in DENSITIES]
    for pool, color, label in (("none", BLUE, "split 4×5 cores"), ("all", ORANGE, "pooled 1×20 cores")):
        measured = [mean(json.loads((r / "contention-1ms.json").read_text())["total_wait_over_run"] for r in runs(k, pool)) * 100
                    for k in DENSITIES]
        modeled = [mean(r["wait_over_run"] for r in sim[(k, pool, "none")]) * 100 for k in DENSITIES]
        ax.plot(x, modeled, color=color, lw=2, label=f"{label}: simulator")
        ax.plot(x, measured, "o", color=color, ms=8, markeredgecolor=SURFACE, markeredgewidth=2, label=f"{label}: measured")
    ax.set(xlabel="sandboxes per physical core", ylabel="CPU time spent waiting (% of CPU used)", ylim=(0, 60))
    ax.set_xticks(x, [f"{v:g}" for v in x])
    ax.set_title("2. The burst-replay simulator reproduces the measured waiting", pad=22)
    ax.text(0, 1.02, "simulator: solo-run vCPU demand per 10 ms + recorded LLM waits, equal CPU share per node; "
            "mean of 3 seeds each", transform=ax.transAxes, fontsize=9, color=INK2)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "2-model-check.png", dpi=160)


def move_latency():
    """Share of lost CPU recovered by moving the busiest VM off an overloaded node, best of four policies."""
    tables = [sim_results(name) for name in POLICIES]
    densities = (14, 18, 21)
    colors = (BLUE, ORANGE, "#1baf7a")
    fig, ax = plt.subplots(figsize=(11, 4.8))
    x = np.arange(len(MOVE_ORDER))
    w = .26
    for j, (k, color) in enumerate(zip(densities, colors)):
        base = mean(r["lost_over_run"] for r in tables[0][(k, "none", "none")])
        best = [min(mean(r["lost_over_run"] for r in t[(k, "none", m)]) for t in tables) for m, _ in MOVE_ORDER]
        recovered = [(1 - b / base) * 100 for b in best]
        offset = (j - 1) * (w + .02)
        ax.bar(x + offset, recovered, w, color=color, label=f"{k / 5:g} sandboxes per core (lost CPU without moves: {base * 100:.1f}% of CPU used)")
        for i, v in enumerate(recovered):
            ax.text(x[i] + offset, v + (2 if v >= 0 else -2), f"{v:.0f}", ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=8, color=INK)
    ax.axhline(0, color=INK, lw=1)
    ax.axvspan(1.5, 4.5, color=GRID, alpha=.45, lw=0, zorder=0)
    ax.text(3, -11, "full VM migration (latency and source CPU from experiment 3)", ha="center", fontsize=9, color=INK2)
    ax.set_xticks(x, [label for _, label in MOVE_ORDER], fontsize=9)
    ax.set(ylim=(-15, 105), ylabel="lost CPU recovered vs. no moves (%)")
    ax.grid(axis="x", visible=False)
    ax.set_title("3. Only a fast, cheap move recovers the lost CPU", pad=22)
    ax.text(0, 1.02, "simulator, split 4×5-core nodes; lost CPU = runqueue wait + work stalled while a VM is paused; "
            "best of 4 detection/hold policies; mean of 3 seeds", transform=ax.transAxes, fontsize=9, color=INK2)
    ax.legend(loc="upper right", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "3-move-latency.png", dpi=160)


def main():
    OUT.mkdir(exist_ok=True)
    # timeline(), bursts() and moves() need event-level runnable intervals: schedstat run_delay is credited when the
    # task finally runs, so per-tick wait lumps land late (see RESULTS-20261021.md, correction). Only totals are valid.
    lost_cpu()
    model_check()
    move_latency()
    print(sorted(p.name for p in OUT.glob("[1-9]-*.png")))


if __name__ == "__main__":
    main()
