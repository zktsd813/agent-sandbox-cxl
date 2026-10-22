#!/usr/bin/env python3
"""Aggregate analyzed pilot runs (contention-*.json from `sandbox.py episodes`) into tables and figures.

Needs matplotlib for figures; tables are written without it.
"""

import argparse
import csv
import json
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
RUNS = HERE / ".local/runs"
LATENCIES = ["instant", "1ms", "10ms", "100ms", "512MiB first ACK", "1GiB first ACK", "2GiB first ACK", "1GiB working set"]
LATENCY_S = {"instant": 0, "1ms": .001, "10ms": .01, "100ms": .1, "512MiB first ACK": .435, "1GiB first ACK": .770,
             "2GiB first ACK": 1.417, "1GiB working set": 4.894}


def load(suffix):
    runs = []
    for path in sorted(RUNS.glob(f"*-pilot-*/contention-{suffix}.json")):
        summary = json.loads(path.read_text())
        config = json.loads((path.parent / "run.json").read_text())
        if str(config["seed"]) == "smoke":
            continue
        summary.update(directory=path.parent, gap_scale=config["gap_scale"], duration_s=config["duration_s"])
        persist = path.parent / "persistence-1ms.json"
        if not persist.exists() and (path.parent / "cpu1ms.csv").exists():
            persistence(path.parent)
        summary["persistence"] = json.loads(persist.read_text()) if persist.exists() else {}
        runs.append(summary)
    return runs


def key(run):
    return (run["vcpus"], run["group_size"], run["gap_scale"], run["per_node"], run["pool"])


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def aggregate(runs):
    groups = {}
    for run in runs:
        groups.setdefault(key(run), []).append(run)
    rows = []
    for (vcpus, group, scale, per_node, pool), members in sorted(groups.items()):
        row = {"vcpus": vcpus, "group_size": group, "gap_scale": scale, "per_node": per_node, "pool": pool,
               "sandboxes_per_core": members[0]["sandboxes_per_core"], "seeds": len(members),
               "run_cores_per_5": mean(5 * mean(r["mean_run_cores_per_unit"].values()) / (sum(r["cores_per_unit"].values())
                                                                                           / len(r["cores_per_unit"]))
                                       for r in members),
               "demand_cores_per_5": mean(5 * mean(r["mean_demand_cores_per_unit"].values()) / (sum(r["cores_per_unit"].values())
                                                                                                 / len(r["cores_per_unit"]))
                                          for r in members),
               "wait_over_run": mean(r["total_wait_over_run"] for r in members),
               "contended_fraction": mean(mean(r["contended_time_fraction"].values()) for r in members),
               "episode_lostw_p50_s": mean(r["episode_length_s_lost_weighted"]["p50"] for r in members),
               "episode_lostw_p90_s": mean(r["episode_length_s_lost_weighted"]["p90"] for r in members),
               "idle_elsewhere": mean(r["lost_fraction_with_idle_core_elsewhere"] for r in members),
               "trace_slowdown_p50": mean(r["trace_slowdown_vs_solo"]["p50"] for r in members),
               "trace_slowdown_p90": mean(r["trace_slowdown_vs_solo"]["p90"] for r in members),
               "command_slowdown_p50": mean(r["command_slowdown_vs_solo"]["p50"] for r in members),
               "command_slowdown_p99": mean(r["command_slowdown_vs_solo"]["p99"] for r in members),
               "step_slowdown_p50": mean(r["step_slowdown_vs_solo"]["p50"] for r in members),
               "step_slowdown_p90": mean(r["step_slowdown_vs_solo"]["p90"] for r in members),
               "timeouts": mean(r["command_timeouts"] for r in members)}
        for name in LATENCIES:
            row[f"recover_{name}"] = mean(r["recoverable_upper_bound_by_move_latency"].get(name) for r in members)
        for tau in ("tau10ms", "tau100ms"):
            for name in ("10ms", "100ms", "0.435s", "1.417s"):
                row[f"reach_{tau}_{name}"] = mean(r["persistence"].get(tau, {}).get(name) for r in members)
        rows.append(row)
    return rows


def write_table(rows, path):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def episode_cdf(directory, suffix):
    rows = list(csv.DictReader((directory / f"episodes-{suffix}.csv").open()))
    pairs = sorted((float(r["length_s"]), float(r["lost_s"])) for r in rows)
    total, acc, xs, ys = sum(l for _, l in pairs) or 1, 0.0, [], []
    for length, lost in pairs:
        acc += lost
        xs.append(length)
        ys.append(acc / total)
    return xs, ys


def figures(runs, rows, out, suffix):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    base = [r for r in rows if r["vcpus"] == 1 and r["group_size"] == 0 and r["gap_scale"] == 1]
    fig, axes = plt.subplots(1, 3, figsize=(18, 4.8))
    ax = axes[0]
    seed1 = sorted((r for r in runs if (r["vcpus"], r["group_size"], r["gap_scale"], r["pool"], str(r["seed"])) == (1, 0, 1, "none", "1")),
                   key=lambda r: r["per_node"])
    for run in seed1:
        xs, ys = episode_cdf(run["directory"], suffix)
        ax.step(xs, ys, where="post", label=f"{run['sandboxes_per_core']:g}/core (wait {run['total_wait_over_run']:.0%} of run)")
    for name in LATENCIES[1:]:
        ax.axvline(LATENCY_S[name], color="gray", lw=.7, ls="--" if LATENCY_S[name] < .3 else ":")
    ax.set(xscale="log", xlabel="contention episode length (s)", ylabel="CDF of lost CPU time",
           title=f"Partitioned 4x5-core nodes, {suffix} samples\n(dashed: 1/10/100 ms; dotted: exp-3 migration)")
    ax.legend(fontsize=7)

    ax = axes[1]
    part = sorted((r for r in base if r["pool"] == "none"), key=lambda r: r["per_node"])
    x = [r["sandboxes_per_core"] for r in part]
    for name in LATENCIES:
        ax.plot(x, [r[f"recover_{name}"] for r in part], marker="o", ls="-" if LATENCY_S[name] < .3 else "--",
                label=f"{name}" + ("" if name in ("instant", "1ms", "10ms", "100ms") else f" ({LATENCY_S[name]}s)"))
    ax.set(ylim=(0, 1.02), xlabel="sandboxes per physical core", ylabel="recoverable fraction of lost CPU (upper bound)",
           title="Move at episode start to a node with an idle core\n(mean over seeds)")
    ax.legend(fontsize=7)

    ax = axes[2]
    for pool, style in (("none", "o-"), ("socket", "s--"), ("all", "^-")):
        series = sorted((r for r in base if r["pool"] == pool), key=lambda r: r["per_node"])
        if series:
            ax.plot([r["sandboxes_per_core"] for r in series], [r["wait_over_run"] for r in series], style,
                    label={"none": "partitioned 4x5", "socket": "pooled per socket 2x10", "all": "pooled 1x20"}[pool])
    ax.set(yscale="log", xlabel="sandboxes per physical core", ylabel="runqueue wait / CPU run time",
           title="Measured: same load, vCPUs free to use other nodes' cores")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / f"overview-{suffix}.png", dpi=150)


PERSIST_TAU_MS = (1, 10, 100)
PERSIST_L_S = {"1ms": .001, "10ms": .01, "100ms": .1, "0.435s": .435, "0.77s": .770, "1.417s": 1.417, "4.894s": 4.894}


def persistence(directory, threshold=0.1):
    """Resolution-robust reachability: lost CPU at t counts as reachable by a mover with latency L when the node has been
    overloaded continuously for >= L at time scale tau (causal moving average of runqueue wait >= threshold cores) and
    another node had an idle core at t. Uses the 1ms per-node schedstat samples."""
    import numpy as np
    config = json.loads((directory / "run.json").read_text())
    data = np.loadtxt(directory / "cpu1ms.csv", delimiter=",", skiprows=1, dtype=np.int64)
    t, dt = data[:, 0], data[:, 1].astype(float)
    begin = t[0] + int(config["ramp_s"] * 1e9)
    keep = (t >= begin) & (t <= begin + int(config["duration_s"] * 1e9))
    t, dt = t[keep], dt[keep]
    nodes = config["nodes"]
    run = np.stack([data[keep, 2 + 2 * i] for i in range(len(nodes))]).astype(float)
    wait = np.stack([data[keep, 3 + 2 * i] for i in range(len(nodes))]).astype(float)
    cores = np.array([len(n["cores"]) for n in nodes], dtype=float)[:, None]
    idle = np.maximum(0, cores - run / dt)
    lost = wait.sum()
    result = {}
    for tau in PERSIST_TAU_MS:
        kernel = np.ones(tau) / tau
        smooth = np.stack([np.convolve(w / dt, kernel, mode="full")[:len(dt)] for w in wait])
        overloaded = smooth >= threshold
        age = np.zeros_like(smooth)
        for i in range(len(nodes)):
            run_length = 0.0
            for j in range(len(dt)):
                run_length = run_length + dt[j] / 1e9 if overloaded[i, j] else 0.0
                age[i, j] = run_length
        idle_elsewhere = idle.sum(axis=0)[None, :] - idle >= 1
        result[f"tau{tau}ms"] = {name: round(float((wait * ((age >= latency) & idle_elsewhere)).sum() / lost), 4) if lost else None
                                 for name, latency in PERSIST_L_S.items()}
        result[f"tau{tau}ms"]["overloaded_time_fraction"] = round(float(overloaded.mean()), 4)
    (directory / "persistence-1ms.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


MOVE_L_S = {"instant": 0.0, "10ms": .01, "100ms": .1, "0.435s": .435, "0.77s": .770, "1.417s": 1.417, "4.894s": 4.894}


def moves(directory, detect_ms=100, threshold=0.1, lookback_ms=100, hold_s=1.0):
    """Replay the measured per-VM 10ms vCPU demand (run + runqueue wait) under a reactive balancer.

    When a node's estimated wait, averaged over `detect_ms`, reaches `threshold` cores, the VM with the most demand in
    the last `lookback_ms` moves to the least-loaded node that has an idle core; its demand counts there from
    trigger + L on. A node starts no new move for L + hold_s. Node wait is estimated as max(0, demand - cores), so the
    move's effect includes new wait created at the destination. Demand time series are held fixed (first order): the
    moved VM is not sped up, its pause during the move and the source CPU spent on the move are not charged.
    """
    import numpy as np
    config = json.loads((directory / "run.json").read_text())
    nodes = config["nodes"]
    cores = np.array([len(n["cores"]) for n in nodes], dtype=float)
    ticks = np.unique(np.loadtxt(directory / "nodes.csv", delimiter=",", skiprows=1, usecols=0, dtype=np.int64))
    data = np.loadtxt(directory / "vcpu.csv", delimiter=",", skiprows=1, dtype=np.int64)
    index = np.searchsorted(ticks, data[:, 0])
    vm_ids, vm_index = np.unique(data[:, 3], return_inverse=True)
    demand = np.zeros((len(vm_ids), len(ticks)), dtype=np.float32)
    np.add.at(demand, (vm_index, index), ((data[:, 4] + data[:, 5]) / data[:, 1]).astype(np.float32))
    home = np.zeros(len(vm_ids), dtype=np.int64)
    home[vm_index] = data[:, 2]
    dt = np.diff(ticks, prepend=ticks[0] - 10_000_000) / 1e9
    begin = ticks[0] + int(config["ramp_s"] * 1e9)
    window = (ticks >= begin) & (ticks <= begin + int(config["duration_s"] * 1e9))
    measured_wait = None
    with (directory / "nodes.csv").open() as stream:
        measured_wait = sum(int(r["wait_ns"]) for r in csv.DictReader(stream) if begin <= int(r["t_ns"]) <= begin + int(config["duration_s"] * 1e9)) / 1e9

    def node_wait(node_demand):
        return (np.maximum(0, node_demand - cores[:, None]) * dt[None, :])[:, window].sum()

    base_demand = np.stack([demand[home == n].sum(axis=0) for n in range(len(nodes))])
    base = node_wait(base_demand)
    pooled = (np.maximum(0, base_demand.sum(axis=0) - cores.sum()) * dt)[window].sum()
    detect, lookback = max(1, round(detect_ms / 10)), max(1, round(lookback_ms / 10))
    result = {"estimated_wait_s": round(float(base), 3), "measured_wait_s": round(measured_wait, 3),
              "pooled_all_reduction": round(float(1 - pooled / base), 4) if base else None,
              "detect_ms": detect_ms, "threshold_cores": threshold, "hold_s": hold_s, "recovered": {}, "moves": {}}
    for name, latency in MOVE_L_S.items():
        where = home.copy()
        pending = []  # (effective tick, vm, destination)
        node_demand = base_demand.copy()
        cooldown = np.full(len(nodes), -1)
        moved_until = np.full(len(vm_ids), -1)
        count = 0
        delay = int(round(latency / 0.01))
        for t in range(len(ticks)):
            while pending and pending[0][0] <= t:
                _, vm, destination = pending.pop(0)
                node_demand[where[vm], t:] -= demand[vm, t:]
                node_demand[destination, t:] += demand[vm, t:]
                where[vm] = destination
            if t < detect:
                continue
            recent = np.maximum(0, node_demand[:, t - detect + 1:t + 1] - cores[:, None]).mean(axis=1)
            for n in np.nonzero((recent >= threshold) & (cooldown < t))[0]:
                current = node_demand[:, t]
                candidates = [m for m in range(len(nodes)) if m != n and current[m] <= cores[m] - 1]
                if not candidates:
                    continue
                destination = min(candidates, key=lambda m: current[m] / cores[m])
                members = np.nonzero((where == n) & (moved_until < t))[0]
                if not len(members):
                    continue
                vm = members[np.argmax(demand[members, t - lookback + 1:t + 1].sum(axis=1))]
                if demand[vm, t - lookback + 1:t + 1].sum() == 0:
                    continue
                pending.append((t + delay, vm, destination))
                pending.sort()
                cooldown[n] = t + delay + int(hold_s / 0.01)
                moved_until[vm] = t + delay + int(hold_s / 0.01)
                count += 1
        after = node_wait(node_demand)
        result["recovered"][name] = round(float(1 - after / base), 4) if base else None
        result["moves"][name] = count
    (directory / "moves.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="1ms", help="contention-<suffix>.json to read (1ms, 1ms-bin10ms, 10ms)")
    parser.add_argument("--output", type=Path, default=HERE / ".local/report")
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runs = load(args.suffix)
    rows = aggregate(runs)
    write_table(rows, args.output / f"summary-{args.suffix}.csv")
    for row in rows:
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()})
    if not args.no_figures:
        figures(runs, rows, args.output, args.suffix)
    print(f"{len(runs)} runs -> {args.output}")


if __name__ == "__main__":
    main()
