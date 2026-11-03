#!/usr/bin/env python3
"""Aggregate the RL-rollout pilot runs (run_rl_matrix.sh) into one table and the virtual-node figures.

Per run (from `sandbox.py episodes`): utilization, runqueue wait / run, lost CPU, the share of lost CPU during which
another node had an idle core, contention-episode lengths, slowdown vs solo, and guest memory (memory.csv).
Pairs ISO/POOL with the same density and LLM speed give the virtual-node gap = 1 - lost_POOL / lost_ISO.
"""

import argparse
import csv
import json
from pathlib import Path


def load(run):
    summary_path = run / "contention-1ms.json"
    if not summary_path.exists() or not (run / "condition.txt").exists():
        return None
    s = json.loads(summary_path.read_text())
    config = json.loads((run / "run.json").read_text())
    name, condition = (run / "condition.txt").read_text().split()
    _, per_node, pool, placement, scale, move = condition.split(":")
    cores = sum(s["cores_per_unit"].values())
    run_cores = sum(s["mean_run_cores_per_unit"].values())
    rss = []
    free = []
    with (run / "memory.csv").open() as stream:
        for row in csv.DictReader(stream):
            if row["vm"].isdigit():
                rss.append(int(row["rss_kib"]))
            elif row["vm"].endswith("_reclaimable"):
                free.append(int(row["rss_kib"]))
    rss.sort()
    return {
        "name": name, "run": run.name, "pool": pool, "placement": placement, "llm_scale": float(scale), "move": move,
        "per_node": int(per_node), "sandboxes_per_core": s["sandboxes_per_core"], "cores": cores,
        "utilization": round(run_cores / cores, 4),
        "wait_over_run": s["total_wait_over_run"], "lost_over_run": s["lost_over_run"],
        "contended_time_fraction": round(sum(s["contended_time_fraction"].values()) / len(s["contended_time_fraction"]), 4),
        "lost_with_idle_core_elsewhere": s["lost_fraction_with_idle_core_elsewhere"],
        "episode_p50_s": s["episode_length_s"]["p50"], "episode_lostw_p50_s": s["episode_length_s_lost_weighted"]["p50"],
        "episode_lostw_p90_s": s["episode_length_s_lost_weighted"]["p90"],
        "recoverable_10ms": s["recoverable_upper_bound_by_move_latency"].get("10ms"),
        "recoverable": s["recoverable_upper_bound_by_move_latency"],
        "cmd_slowdown_p50": s["command_slowdown_vs_solo"].get("p50"), "cmd_slowdown_p99": s["command_slowdown_vs_solo"].get("p99"),
        "cmd_n": s["command_slowdown_vs_solo"]["n"],
        "rollout_slowdown_p50": s["trace_slowdown_vs_solo"].get("p50"), "rollout_slowdown_p90": s["trace_slowdown_vs_solo"].get("p90"),
        "step_slowdown_p50": s["step_slowdown_vs_solo"].get("p50"), "step_slowdown_p90": s["step_slowdown_vs_solo"].get("p90"),
        "step_n": s["step_slowdown_vs_solo"]["n"],
        "sandboxes": s["sandboxes"], "timeouts": s["command_timeouts"], "mismatches": s["command_mismatches"],
        "errors": s["errors"], "rss_p50_mib": round(rss[len(rss) // 2] / 1024) if rss else None,
        "rss_max_mib": round(rss[-1] / 1024) if rss else None,
        "numa0_reclaimable_min_gib": round(min(free) / 2**20, 1) if free else None,
        "seed": config["seed"], "duration_s": config["duration_s"], "ramp_s": config["ramp_s"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = [r for r in (load(run) for run in args.runs) if r]
    args.out.mkdir(parents=True, exist_ok=True)
    fields = [k for k in rows[0] if k != "recoverable"]
    with (args.out / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    gaps = []
    for iso in rows:
        if iso["pool"] != "none" or iso["move"] != "none":
            continue
        for pooled in rows:
            if (pooled["pool"] == "all" and pooled["per_node"] == iso["per_node"]
                    and pooled["llm_scale"] == iso["llm_scale"] and pooled["move"] == "none"):
                def rel(key):
                    a, b = iso[key], pooled[key]
                    return round(1 - b / a, 4) if a else None

                gaps.append({"iso": iso["name"], "pool": pooled["name"], "placement": iso["placement"],
                             "sandboxes_per_core": iso["sandboxes_per_core"], "llm_scale": iso["llm_scale"],
                             "utilization_iso": iso["utilization"], "lost_iso": iso["lost_over_run"],
                             "lost_pool": pooled["lost_over_run"], "gap_lost": rel("lost_over_run"),
                             "wait_iso": iso["wait_over_run"], "wait_pool": pooled["wait_over_run"],
                             "gap_wait": rel("wait_over_run"),
                             "cmd_p99_iso": iso["cmd_slowdown_p99"], "cmd_p99_pool": pooled["cmd_slowdown_p99"],
                             "step_p90_iso": iso["step_slowdown_p90"], "step_p90_pool": pooled["step_slowdown_p90"]})
    with (args.out / "gaps.csv").open("w", newline="") as stream:
        if gaps:
            writer = csv.DictWriter(stream, fieldnames=list(gaps[0]))
            writer.writeheader()
            writer.writerows(gaps)
    (args.out / "summary.json").write_text(json.dumps({"runs": rows, "gaps": gaps}, indent=1) + "\n")
    for r in rows:
        print(f"{r['name']:24s} util {r['utilization']:.2f} wait/run {r['wait_over_run']} lost/run {r['lost_over_run']} "
              f"idle-elsewhere {r['lost_with_idle_core_elsewhere']} cmd p99 {r['cmd_slowdown_p99']} "
              f"step p90 {r['step_slowdown_p90']} rss p50 {r['rss_p50_mib']}MiB")
    for g in gaps:
        print("GAP", g)


if __name__ == "__main__":
    main()
