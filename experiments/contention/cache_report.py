#!/usr/bin/env python3
"""Placement policies vs host page cache (fleet.py --blocks output): table per condition and a trade-off figure.

Per (nodes, per_node, LLM scale, RL mode) and policy, averaged over seeds: utilization, runqueue wait / run,
step slowdown, moves per sandbox, host page cache per node (mean, peak), cluster cache, and 3FS fetch per hour for
each rho (share of an image a moved sandbox still reads on its new node). The shared (CXL) cache is the same for
every placement and is printed once per condition.
"""

import argparse
import collections
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="directory for summary.json and figures")
    parser.add_argument("--rho", default="0.25")
    args = parser.parse_args()
    data = json.loads(args.results.read_text())["results"]
    rho = args.rho
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in data:
        key = (r["nodes"], r["per_node"], r["gap_scale"], r["rl_mode"])
        groups[key][r["policy"]].append(r)
    mean = lambda rs, k: sum(r[k] for r in rs if r.get(k) is not None) / max(1, sum(r.get(k) is not None for r in rs))
    summary = []
    for key in sorted(groups):
        nodes, per_node, scale, mode = key
        print(f"\n== {nodes} nodes x 15 cores, {per_node}/node ({per_node / 15:.1f}/core), LLM x{scale}, {mode}")
        shared = None
        for policy, rs in sorted(groups[key].items()):
            row = {"nodes": nodes, "per_node": per_node, "gap_scale": scale, "rl_mode": mode, "policy": policy,
                   "seeds": len(rs), "utilization": mean(rs, "run_cores_per_node") / 15,
                   "wait_over_run": mean(rs, "wait_over_run"), "lost_over_run": mean(rs, "lost_over_run"),
                   "step_slowdown_mean": mean(rs, "step_slowdown_mean"),
                   "trace_slowdown_p90": mean(rs, "trace_slowdown_p90"),
                   "moves": mean(rs, "moves"), "traces_finished": mean(rs, "traces_finished"),
                   "cache_mean_gib_per_node": mean(rs, f"cache_mean_gib_per_node_rho{rho}"),
                   "cache_peak_gib_per_node": mean(rs, f"cache_peak_gib_per_node_rho{rho}"),
                   "cache_total_mean_gib": mean(rs, f"cache_total_mean_gib_rho{rho}"),
                   **{f"fetch_gib_per_hour_rho{x}": mean(rs, f"cache_fetch_gib_per_hour_rho{x}") for x in ("0", "0.25", "1")}}
            shared = (mean(rs, f"shared_cache_mean_gib_rho{rho}"), mean(rs, f"shared_fetch_gib_per_hour_rho{rho}"))
            summary.append(row)
            print(f"  {policy:26s} util {row['utilization']:.2f} wait/run {row['wait_over_run']:.4f} "
                  f"step {row['step_slowdown_mean'] or 0:.3f} moves {row['moves']:7.0f} | cache/node {row['cache_mean_gib_per_node']:.2f} "
                  f"(peak {row['cache_peak_gib_per_node']:.2f}) total {row['cache_total_mean_gib']:.2f} GiB | fetch GiB/h "
                  f"rho0 {row['fetch_gib_per_hour_rho0']:.1f} rho.25 {row['fetch_gib_per_hour_rho0.25']:.1f} "
                  f"rho1 {row['fetch_gib_per_hour_rho1']:.1f}")
        print(f"  {'shared CXL cache':26s} total {shared[0]:.2f} GiB, fetch {shared[1]:.1f} GiB/h (any placement)")
        summary.append({"nodes": nodes, "per_node": per_node, "gap_scale": scale, "rl_mode": mode,
                        "policy": "shared CXL cache", "cache_total_mean_gib": shared[0], "fetch_gib_per_hour": shared[1]})
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    keys = [k for k in sorted(groups) if k[1] == 96]
    cols = 3
    rows = -(-len(keys) // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 3.2 * rows), squeeze=False)
    colors = {"P1": "#2471a3", "P2": "#c0392b", "P3": "#e67e22", "P4": "#7d3c98", "P5": "#16a085", "POOL": "#7f8c8d"}
    for ax, key in zip(axes.flat, keys):
        for row in summary:
            if (row["nodes"], row["per_node"], row["gap_scale"], row["rl_mode"]) != key or "wait_over_run" not in row:
                continue
            tag = row["policy"].split()[0]
            hand = "hand-off" in row["policy"]
            ax.scatter(row["cache_mean_gib_per_node"], row["wait_over_run"] * 100, color=colors.get(tag, "black"),
                       marker="^" if hand else "o", s=36)
            ax.annotate(row["policy"].replace(" + hand-off", "+H").replace("least-loaded", "LL"),
                        (row["cache_mean_gib_per_node"], row["wait_over_run"] * 100), fontsize=5,
                        xytext=(2, 2), textcoords="offset points")
        ax.set_title(f"{key[0]} nodes, LLM x{key[2]}, {key[3]}", fontsize=8)
        ax.set_xlabel("host page cache per node (GiB)", fontsize=7)
        ax.set_ylabel("runqueue wait / run (%)", fontsize=7)
        ax.tick_params(labelsize=6)
    for ax in axes.flat[len(keys):]:
        ax.axis("off")
    fig.suptitle(f"Placement: CPU wait vs image page cache (triangles: with 10 ms hand-off; moved sandbox reads rho={rho})",
                 fontsize=9)
    fig.tight_layout()
    fig.savefig(args.out / "placement-cache-tradeoff.png", dpi=160)
    print(args.out)


if __name__ == "__main__":
    main()
