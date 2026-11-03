#!/usr/bin/env python3
"""Synchronous-step runs (sandbox.py pilot --sync-steps): per-step time vs solo, and where in a step the CPU goes.

Per run: step time / (slowest member's solo time + jitter), the tail (time while fewer than 10% of rollouts are
still running), the step-relative profile of run and runqueue-wait cores (all nodes, --bin-s), and the share of the
step's runqueue wait in its first 10 / 30 / 60 s (start burst: boot and first commands of every rollout together).
Plan runs (--sync-plan) add per-wave tails, the live-rollout curve over normalized step time, boot and first-command
latency and base-image reads; their expected solo end includes each member's wave offset and start delay.
Writes <run>/sync-steps.json; with --figure, wait cores vs time in step for all runs given.
"""

import argparse
import bisect
import csv
import json
from pathlib import Path


def expected_solo(solos, scale):
    out, timed_out = {}, set()
    for solo in solos:  # plan segments each have their own solo run
        for row in csv.DictReader((solo / "solo-summary.csv").open()):
            out[row["name"]] = float(row["replay_s"]) + float(row["recorded_gap_s"]) * scale
            if int(row["timeouts"]):
                timed_out.add(row["name"])
    return out, timed_out


def plan_step_details(s, members, start, duration, bins=20):
    """Plan steps: per-wave tails, live-rollout curve over normalized step time, boot and first-command latency,
    and base-image disk reads (guest vda read sectors, with --mem-sample-s and a virtio-blk base)."""
    waves = []
    for w, launch in enumerate(s["wave_launch_ns"]):
        rows = [r for r in members if s["member_wave"][s["member_vm"].index(r["vm"])] == w]
        if launch is None or not rows:
            continue
        ends = sorted((r["end_ns"] - launch) / 1e9 for r in rows)
        last = ends[-1]
        waves.append({"wave": w, "launch_s": round((launch - s["begin_ns"]) / 1e9, 1), "members": len(rows),
                      "end_s": round(last, 1), "p50_end_s": round(ends[len(ends) // 2], 1),
                      "p90_end_s": round(ends[int(len(ends) * .9)], 1),
                      "tail_fraction": round(1 - ends[int(len(ends) * .9)] / last, 3) if last else None})
    live = []
    for b in range(bins):
        t = s["begin_ns"] + (b + .5) / bins * duration * 1e9
        live.append(sum(1 for r in members if r.get("start_ns") and r["start_ns"] <= t < r["end_ns"]))
    boot = sorted((r["ready_ns"] - r["start_ns"]) / 1e9 for r in members if r.get("ready_ns") and r.get("start_ns"))
    first = sorted((r["commands"][0]["host_end_ns"] - r["commands"][0]["host_start_ns"]) / 1e9 for r in members
                   if r.get("commands") and r["commands"][0].get("host_end_ns"))
    base_reads = [r["mem"][-1][8] * 512 for r in members if r.get("mem") and len(r["mem"][-1]) > 8]
    pick = lambda v, q: round(v[min(len(v) - 1, int(len(v) * q))], 2) if v else None
    return {"waves": waves, "live_by_twentieth": live, "boot_s_p50_p90": [pick(boot, .5), pick(boot, .9)],
            "first_cmd_s_p50_p90": [pick(first, .5), pick(first, .9)],
            "base_read_gib": round(sum(base_reads) / 2**30, 3) if base_reads else None,
            "images": len(s.get("images") or [])}


def analyze(run, solo, bin_s):
    config = json.loads((run / "run.json").read_text())
    steps = [json.loads(line) for line in (run / "steps.jsonl").open()]
    expected, solo_timed_out = expected_solo(solo, config["gap_scale"])
    records = [json.loads(line) for line in (run / "sandboxes.jsonl").open()]
    nodes = [n["index"] for n in config["nodes"]]
    cores = sum(len(n["cores"]) for n in config["nodes"])
    times, run_ns, wait_ns = [], [], []
    with (run / "cpu1ms.csv").open() as stream:
        for row in csv.DictReader(stream):
            times.append(int(row["t_ns"]))
            run_ns.append(sum(int(row[f"run{i}"]) for i in nodes))
            wait_ns.append(sum(int(row[f"wait{i}"]) for i in nodes))
    profile = {}
    per_step = []
    by_vm = {r["vm"]: r for r in records}
    for s in steps:
        duration = (s["end_ns"] - s["begin_ns"]) / 1e9
        ends = sorted((e - s["begin_ns"]) / 1e9 for e in s["member_end_ns"])
        if "start_delay_s" in s:
            # Plan steps: a member can end no earlier than its wave offset + start delay + solo time.
            start = {vm: s["wave_offset_s"][w] + d for vm, w, d in zip(s["member_vm"], s["member_wave"], s["start_delay_s"])}
            members = [by_vm[vm] for vm in s["member_vm"] if vm in by_vm]
        else:
            start = None
            members = [r for r in records if s["begin_ns"] <= r.get("start_ns", 0) <= s["end_ns"]]
        lead = (lambda r: start.get(r["vm"], 0.0)) if start else (lambda r: config["group_jitter_s"])
        solo_max = max(expected[r["trace"]] + lead(r) for r in members) if members else None
        # clean: members with no 60 s command timeout in solo or here (a timeout is not contention)
        clean = [r for r in members if r["trace"] not in solo_timed_out and not r.get("timeouts")]
        clean_end = max((r["end_ns"] - s["begin_ns"]) / 1e9 for r in clean) if clean else None
        clean_solo = max(expected[r["trace"]] + lead(r) for r in clean) if clean else None
        extra = plan_step_details(s, members, start, duration) if start else {}
        lo, hi = bisect.bisect_left(times, s["begin_ns"]), bisect.bisect_right(times, s["end_ns"])
        wait_total = sum(wait_ns[lo:hi]) / 1e9
        run_total = sum(run_ns[lo:hi]) / 1e9
        early = {}
        for head in (10, 30, 60):
            cut = bisect.bisect_right(times, s["begin_ns"] + int(head * 1e9))
            early[f"{head}s"] = round(sum(wait_ns[lo:cut]) / 1e9 / wait_total, 4) if wait_total else None
        for i in range(lo, hi):
            b = int((times[i] - s["begin_ns"]) / 1e9 // bin_s)
            acc = profile.setdefault(b, [0, 0, 0])
            acc[0] += run_ns[i]
            acc[1] += wait_ns[i]
            acc[2] += 1
        per_step.append({"step": s["step"], "complete": s["completed"] == s["members"], "duration_s": round(duration, 1),
                         "solo_max_s": round(solo_max, 1), "slowdown": round(duration / solo_max, 3),
                         "clean_members": len(clean), "clean_end_s": round(clean_end, 1) if clean else None,
                         "clean_solo_max_s": round(clean_solo, 1) if clean else None,
                         "clean_slowdown": round(clean_end / clean_solo, 3) if clean else None,
                         "prepare_s": s.get("prepare_s"),
                         "p50_member_end_s": round(ends[len(ends) // 2], 1) if ends else None,
                         "p90_member_end_s": round(ends[int(len(ends) * .9)], 1) if ends else None,
                         "tail_fraction": round(1 - ends[int(len(ends) * .9)] / duration, 3) if ends else None,
                         "utilization": round(run_total / duration / cores, 4) if duration else None,
                         "wait_over_run": round(wait_total / run_total, 4) if run_total else None,
                         "wait_share_first": early, **extra})
    # 1 ms samples: run/wait ns per bin, divided by sample count x 1 ms gives mean cores
    dt = (times[1] - times[0]) if len(times) > 1 else 1_000_000
    prof = [{"t_s": b * bin_s, "run_cores": round(v[0] / (v[2] * dt), 3), "wait_cores": round(v[1] / (v[2] * dt), 3)}
            for b, v in sorted(profile.items())]
    complete = [p for p in per_step if p["complete"]]
    mean = lambda key: round(sum(p[key] for p in complete) / len(complete), 4) if complete else None
    result = {"run": run.name, "condition": (run / "condition.txt").read_text().split()[0] if (run / "condition.txt").exists() else None,
              "pool": config["pool"], "placement": config["placement"], "gap_scale": config["gap_scale"], "cores": cores,
              "steps": per_step, "mean_step_slowdown": mean("slowdown"), "mean_clean_step_slowdown": mean("clean_slowdown"),
              "mean_step_s": mean("duration_s"), "mean_utilization": mean("utilization"),
              "mean_wait_over_run": mean("wait_over_run"), "mean_tail_fraction": mean("tail_fraction"),
              "wait_share_first": {k: round(sum(p["wait_share_first"][k] or 0 for p in complete) / len(complete), 4)
                                   for k in ("10s", "30s", "60s")} if complete else None,
              "profile": prof}
    (run / "sync-steps.json").write_text(json.dumps(result, indent=1) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--solo", type=Path, required=True, action="append", help="Solo run directory (repeatable)")
    parser.add_argument("--bin-s", type=float, default=5)
    parser.add_argument("--figure", type=Path)
    args = parser.parse_args()
    results = [analyze(run, args.solo, args.bin_s) for run in args.runs]
    for r in results:
        print(json.dumps({k: v for k, v in r.items() if k not in ("profile", "steps")}))
        for s in r["steps"]:
            print("  ", json.dumps(s))
    if args.figure:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        scales = sorted({r["gap_scale"] for r in results})
        fig, axes = plt.subplots(len(scales), 1, figsize=(7, 2.6 * len(scales)), squeeze=False)
        for ax, scale in zip(axes[:, 0], scales):
            for r in results:
                if r["gap_scale"] != scale:
                    continue
                label = f"{'POOL' if r['pool'] == 'all' else 'ISO'} {r['placement']}"
                xs = [p["t_s"] for p in r["profile"]]
                ax.plot(xs, [p["wait_cores"] for p in r["profile"]], label=f"{label} wait")
                ax.plot(xs, [p["run_cores"] for p in r["profile"]], lw=.7, ls="--", label=f"{label} run")
            ax.set(title=f"LLM x{scale}: cores running / waiting vs time since step start (mean over steps)",
                   xlabel="s since step start", ylabel="cores (30 total)")
            ax.title.set_fontsize(8)
            ax.legend(fontsize=6)
        fig.tight_layout()
        args.figure.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(args.figure, dpi=160)


if __name__ == "__main__":
    main()
