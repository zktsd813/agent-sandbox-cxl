#!/usr/bin/env python3
"""G1 analysis: per-owner CPU demand, DDR, memory and GPU on one clock, phase table, gate 1 metric, figures 1-2.

Usage:
  analyze_g1.py RUN_DIR [--steps A-B]          -> RUN_DIR/analysis.json, RUN_DIR/fig1-timeline.png
  analyze_g1.py --cdf OUT.png RUN_DIR [...]    -> figure 2 (borrowable-core CDF) over several runs
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

INTERVAL_NS = 100_000_000
PHASES = ("gen", "collect_trajectory", "wait_prev_gen", "sync_rollout_weights", "old_log_prob", "ref", "values",
          "update_actor", "testing", "save_checkpoint")
ROLLOUT = ("gen", "collect_trajectory", "wait_prev_gen")  # phases where the trainer mostly waits for generation
OWNERS = ("trainer worker", "controller", "Ray system", "other")
# Reference categorical palette (dataviz skill references/palette.md), fixed order, light mode.
COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
PHASE_GRAY = {"gen": "#f3f2ee", "collect_trajectory": "#f3f2ee", "wait_prev_gen": "#f3f2ee",
              "sync_rollout_weights": "#dcdad3", "old_log_prob": "#dcdad3", "ref": "#dcdad3", "values": "#dcdad3",
              "update_actor": "#c4c1b8", "testing": "#ebe9e3", "save_checkpoint": "#ebe9e3"}


def cpuset_size(spec):
    total = 0
    for part in spec.split(","):
        a, _, b = part.partition("-")
        total += int(b or a) - int(a) + 1
    return total


def owner_of(names):
    """names: thread names seen in one process (the main thread carries the Ray actor title)."""
    if any(n == "ray::WorkerDict" for n in names):
        return "trainer worker"
    if any(n in ("ray::TaskRunner",) for n in names) or "python" in names and len(names) < 3:
        return "controller"
    if any(n.startswith(("raylet", "gcs_server", "ray-dashboard", "ray::IDLE", "ray::")) for n in names):
        return "Ray system"
    return "other"


def load(run):
    env = json.loads((run / "env.json").read_text())
    samples = run / "samples"
    names = defaultdict(set)
    rows = []
    with (samples / "threads.csv").open() as stream:
        for r in csv.DictReader(stream):
            pid = int(r["pid"])
            names[pid].add(r["comm"])
            rows.append((int(r["t_ns"]), pid, int(r["oncpu_ns"])))
    cpu = np.loadtxt(samples / "cpu.csv", delimiter=",", skiprows=1, dtype=np.int64)
    t = cpu[:, 0]
    t0 = env["start_monotonic_ns"]
    index = {v: i for i, v in enumerate(t)}
    owner_pid = {pid: owner_of(n) for pid, n in names.items()}
    demand = {o: np.zeros(len(t)) for o in OWNERS}
    for t_ns, pid, oncpu in rows:
        i = index.get(t_ns)
        if i is not None and i > 0:
            demand[owner_pid[pid]][i] += oncpu / (t[i] - t[i - 1])
    busy, total = cpu[:, 1::2], cpu[:, 2::2]
    d_busy, d_total = np.diff(busy, axis=0), np.diff(total, axis=0)
    util = np.where(d_total > 0, d_busy / np.maximum(d_total, 1), 0)
    node0 = [c for c in range(util.shape[1]) if c < 16 or 32 <= c < 48]
    node0_busy = np.concatenate([[0], util[:, node0].sum(axis=1)])

    imc = defaultdict(dict)
    with (samples / "imc.csv").open() as stream:
        start = int(stream.readline().split("=")[1])
        for line in stream:
            f = line.strip().split(",")
            if len(f) < 6 or f[4] != "MiB":
                continue
            ts = start + int(float(f[0]) * 1e9)
            kind = "read" if "read" in f[5] else "write"
            imc[(f[1], kind)][ts] = float(f[3]) * 2**20 / 1e9 / (INTERVAL_NS / 1e9)
    mem = np.loadtxt(samples / "mem.csv", delimiter=",", skiprows=1, dtype=np.int64)
    with (samples / "mem.csv").open() as stream:
        header = stream.readline().strip().split(",")
    gpu = np.loadtxt(samples / "gpu.csv", delimiter=",", skiprows=1)
    phases = []
    for line in (run / "phases.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["end_ns"] is not None:
            phases.append((e["name"], e["start_ns"], e["end_ns"]))
    return {"env": env, "t": t, "t0": t0, "demand": demand, "node0_busy": node0_busy, "imc": imc,
            "mem": mem, "mem_header": header, "gpu": gpu, "phases": phases,
            "budget": cpuset_size(env["train_cpus"])}


def steps_of(data):
    return [(s, e) for name, s, e in data["phases"] if name == "step"]


def analyze(data, skip=2):
    t, budget = data["t"], data["budget"]
    train = sum(data["demand"][o] for o in OWNERS)
    steps = steps_of(data)[skip:]
    per_step, phase_rows = [], defaultdict(list)
    for s, e in steps:
        mask = (t >= s) & (t < e)
        d = train[mask]
        per_step.append({"seconds": (e - s) / 1e9, "mean": float(d.mean()), "p99": float(np.percentile(d, 99)),
                         "max": float(d.max()), "swing": float(np.percentile(d, 99) - d.mean())})
    s_read = data["imc"].get(("S0", "read"), {})
    s_write = data["imc"].get(("S0", "write"), {})
    imc_t = np.array(sorted(s_read))
    imc_bw = np.array([s_read[k] + s_write.get(k, 0) for k in imc_t]) if len(imc_t) else np.array([])
    first, last = (steps[0][0], steps[-1][1]) if steps else (0, 0)
    for name, s, e in data["phases"]:
        if name not in PHASES or not (first <= s < last):
            continue
        d = train[(t >= s) & (t < e)]
        bw = imc_bw[(imc_t >= s) & (imc_t < e)] if len(imc_t) else np.array([])
        if len(d):
            phase_rows[name].append(((e - s) / 1e9, d.mean(), np.percentile(d, 95), d.max(),
                                     bw.mean() if len(bw) else np.nan, bw.max() if len(bw) else np.nan))
    table = {}
    for name, rows in phase_rows.items():
        a = np.array(rows)
        table[name] = {"count": len(rows), "seconds_mean": a[:, 0].mean(), "cores_mean": a[:, 1].mean(),
                       "cores_p95": a[:, 2].mean(), "cores_max": a[:, 3].max(),
                       "ddr_s0_gbps_mean": np.nanmean(a[:, 4]), "ddr_s0_gbps_max": np.nanmax(a[:, 5])}
    in_steps = np.zeros(len(t), bool)
    for s, e in steps:
        in_steps |= (t >= s) & (t < e)
    high = in_steps & (train > 0.5 * budget)
    covered = np.zeros(len(t), bool)
    for name, s, e in data["phases"]:
        if name in PHASES and name not in ROLLOUT:
            covered |= (t >= s) & (t < e)
    swing = float(np.median([p["swing"] for p in per_step])) if per_step else 0.0
    return {
        "config": data["env"]["config"], "budget_threads": budget, "steps_analyzed": len(per_step),
        "step_seconds_mean": float(np.mean([p["seconds"] for p in per_step])) if per_step else None,
        "demand_mean_cores": float(train[in_steps].mean()) if in_steps.any() else None,
        "demand_p99_cores": float(np.percentile(train[in_steps], 99)) if in_steps.any() else None,
        "gate1_swing_median_cores": swing, "gate1_threshold_cores": 0.1 * budget,
        "gate1_swing_pass": swing >= 0.1 * budget,
        "high_demand_seconds": float(high.sum() * INTERVAL_NS / 1e9),
        "high_demand_in_nongen_phase": float((high & covered).sum() / high.sum()) if high.any() else None,
        "per_step": per_step,
        "phases": {k: {kk: float(vv) for kk, vv in v.items()} for k, v in table.items()},
    }


def timeline(data, out, steps=(3, 4)):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    all_steps = steps_of(data)
    a, b = steps
    if len(all_steps) < b:
        a, b = 1, len(all_steps)
    lo, hi = all_steps[a - 1][0], all_steps[b - 1][1]
    t = data["t"]
    m = (t >= lo) & (t <= hi)
    x = (t[m] - lo) / 1e9
    fig, axes = plt.subplots(5, 1, figsize=(11, 8.2), sharex=True,
                             gridspec_kw={"height_ratios": [0.35, 2.2, 1.1, 1.1, 1.1]})
    strip, cores, ddr, mem, gpu = axes
    for name, s, e in data["phases"]:
        if name not in PHASES or e < lo or s > hi:
            continue
        xs, xe = (max(s, lo) - lo) / 1e9, (min(e, hi) - lo) / 1e9
        strip.axvspan(xs, xe, color=PHASE_GRAY[name], lw=0)
        if xe - xs > (hi - lo) / 1e9 * 0.035:
            strip.text((xs + xe) / 2, 0.5, name.replace("_", " "), ha="center", va="center", fontsize=7.5, color=INK)
        for ax in axes[1:]:
            if name not in ROLLOUT:
                ax.axvspan(xs, xe, color=PHASE_GRAY[name], lw=0, alpha=0.55, zorder=0)
    strip.set_yticks([])
    strip.set_title(f"{data['env']['config']}: training-side resources over steps {a}-{b} "
                    f"(cpuset {data['budget']} threads, 100 ms)", loc="left", fontsize=10, color=INK)
    stack = [data["demand"][o][m] for o in OWNERS]
    cores.stackplot(x, stack, colors=COLORS, labels=OWNERS, lw=0.4, edgecolor="#fcfcfb")
    cores.plot(x, data["node0_busy"][m], color=INK2, lw=0.8, label="node0 busy (all processes)")
    cores.axhline(data["budget"], color=INK2, ls="--", lw=1)
    cores.text(0, data["budget"], " cpuset budget", va="bottom", fontsize=7.5, color=INK2)
    cores.set_ylabel("busy cores")
    cores.set_ylim(0, data["budget"] * 1.1)
    cores.legend(loc="upper right", fontsize=7.5, ncol=5, frameon=False)
    for (sock, kind), series in sorted(data["imc"].items()):
        if sock != "S0":
            continue
        ts = np.array(sorted(series))
        mm = (ts >= lo) & (ts <= hi)
        ddr.plot((ts[mm] - lo) / 1e9, np.array([series[k] for k in ts])[mm],
                 color=COLORS[0] if kind == "read" else COLORS[1], lw=1, label=f"socket0 {kind}")
    ddr.set_ylabel("DDR GB/s")
    ddr.legend(loc="upper right", fontsize=7.5, frameon=False, ncol=2)
    mt = data["mem"][:, 0]
    mm = (mt >= lo) & (mt <= hi)
    col = data["mem_header"].index("node0_used_kB")
    mem.plot((mt[mm] - lo) / 1e9, data["mem"][mm, col] / 2**20, color=COLORS[0], lw=1.2, label="node0 used")
    mem.set_ylabel("host mem GiB")
    mem.legend(loc="upper right", fontsize=7.5, frameon=False)
    g = data["gpu"]
    for i in (0, 1):
        sel = (g[:, 1] == i) & (g[:, 0] >= lo) & (g[:, 0] <= hi)
        gpu.plot((g[sel, 0] - lo) / 1e9, g[sel, 2], color=COLORS[i], lw=1, label=f"GPU{i}")
    gpu.set_ylabel("GPU util %")
    gpu.set_ylim(0, 105)
    gpu.legend(loc="upper right", fontsize=7.5, frameon=False, ncol=2)
    gpu.set_xlabel("seconds from step start")
    for ax in axes[1:]:
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=8, colors=INK2)
    strip.spines[:].set_visible(False)
    fig.tight_layout(h_pad=0.4)
    fig.savefig(out, dpi=160, facecolor="#fcfcfb")
    plt.close(fig)


def cdf(runs, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for i, run in enumerate(runs):
        data = load(run)
        train = sum(data["demand"][o] for o in OWNERS)
        in_steps = np.zeros(len(data["t"]), bool)
        for s, e in steps_of(data)[2:]:
            in_steps |= (data["t"] >= s) & (data["t"] < e)
        borrow = np.sort(data["budget"] - train[in_steps] - 1)
        ax.plot(borrow, np.linspace(0, 1, len(borrow)), color=COLORS[i % 4], lw=2,
                label=f"{data['env']['config']} ({data['budget']} threads)")
    ax.set_xlabel("borrowable cores (budget - training demand - 1)")
    ax.set_ylabel("fraction of step time")
    ax.grid(color=GRID, lw=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=160, facecolor="#fcfcfb")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--cdf", type=Path)
    parser.add_argument("--steps", default="3-4")
    parser.add_argument("--skip", type=int, default=2)
    args = parser.parse_args()
    if args.cdf:
        cdf(args.runs, args.cdf)
        return
    for run in args.runs:
        data = load(run)
        result = analyze(data, skip=args.skip)
        (run / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
        a, b = (int(x) for x in args.steps.split("-"))
        timeline(data, run / "fig1-timeline.png", (a, b))
        print(json.dumps({k: v for k, v in result.items() if k not in ("per_step", "phases")}, indent=2))


if __name__ == "__main__":
    main()
