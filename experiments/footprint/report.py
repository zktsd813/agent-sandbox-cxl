#!/usr/bin/env python3
"""Result tables of the 11-18/19 footprint measurement from the per-run summaries (to_sim.py steps/memory/snapshot).

  report.py SUMMARY_DIR [--json OUT]

Per workload, at the step where memory bound (admission blocked) or the last step:
  m      memory per live sandbox (cgroup memory minus file cache, GiB)
  c      busy logical CPUs per live sandbox
  util   node CPU use at that step
Crossover: the DRAM per physical core above which CPU (72% use, the simulator's SLO point) binds before memory:
  2 x 0.72 x m / c   (two logical CPUs per core, SMT on)
After reductions (upper bound): m x (1 - merged-page saving) x (1 - cold30 share x (1 - lz4 ratio)).
"""
import argparse
import json
from pathlib import Path
import statistics as st

RUNS = {  # workload -> (60-core fill runs, 38-core run)
    "RL": (["1-rl", "1-rl-hi"], "3-rl-38"),
    "SWE": (["1-swe", "1-swe-hi"], "3-swe-38"),
    "CI": (["1-ci"], "3-ci-38"),
}


def load(d, name, kind):
    p = d / f"{name}-{kind}.json"
    return json.loads(p.read_text()) if p.exists() else None


def ceiling_step(steps):
    """The step where admission was blocked most (memory ceiling), else the last one."""
    # blocked admission with little room left; runs before admit_waiting was logged (1-rl) count GRPO slots waiting
    # for siblings as blocked, so the room check keeps them out
    bound = [s for s in steps["steps"] if s["admission_blocked"] > 0.5 and s["room_min_gib"] < 20]
    return (bound[0] if bound else steps["steps"][-1]), bool(bound)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("summary", type=Path)
    ap.add_argument("--json")
    a = ap.parse_args()
    out = {}
    print("workload  run       cores budget  live  bound  m GiB  c CPU   util  crossover GiB/core  slowdown p99")
    for w, (fills, run38) in RUNS.items():
        rows = []
        for name in fills + [run38]:
            s = load(a.summary, name, "steps")
            if not s:
                continue
            step, bound = ceiling_step(s)
            m, c = step["gib_per_sandbox"], step["busy_cpus_per_sandbox"]
            cross = 2 * 0.72 * m / c if c else None
            rows.append(dict(run=name, cores=s["physical_cores"], budget=round(s["budget_gib"]), live=step["live_mean"],
                             bound=bound, m=m, c=c, util=step["cpu_util"], crossover=cross,
                             slowdown=step["cpu_weighted_slowdown"], p99=step["heavy_p99"]))
            print(f"{w:8s}  {name:9s} {s['physical_cores']:5d} {s['budget_gib']:6.0f} {step['live_mean']:6.0f} "
                  f"{str(bound):6s} {m:6.3f} {c:6.4f} {step['cpu_util']:6.3f} {cross:10.1f} {'':8s}"
                  f"{step['cpu_weighted_slowdown']} {step['heavy_p99']}")
        mem = [load(a.summary, n, "memory") for n in fills + [run38]]
        mem = [x for x in mem if x and x.get("idle")]
        bins = [b for x in mem for b in x.get("dedup", []) if b["vms"] >= 16]
        red = None
        if mem and bins:
            cold = st.mean(x["idle"]["cold30_share"] for x in mem)
            lz4 = st.mean(x["compress"]["cold"]["lz4_ratio"] for x in mem if "cold" in x.get("compress", {}))
            sav = st.median(b["saving_if_merged"] for b in bins)
            red = dict(cold30=round(cold, 3), cold120=round(st.mean(x["idle"]["cold120_share"] for x in mem), 3),
                       lz4_cold=round(lz4, 3), merged_saving=round(sav, 3),
                       zero=round(st.median(b["zero_share"] for b in bins), 3),
                       dup_within_group=round(st.median(b["dup_within_group"] for b in bins), 3),
                       dup_across=round(st.median(b["dup_across_groups"] for b in bins), 3),
                       remaining_after_all=round((1 - sav) * (1 - cold * (1 - lz4)), 3),
                       remaining_compress_only=round(1 - cold * (1 - lz4), 3))
            print(f"          reductions: {red}")
        snaps = [load(a.summary, n, "snapshot") for n in fills]
        snaps = [x for x in snaps if x]
        snap = None
        if snaps:
            big = max(snaps, key=lambda x: x["waits"])
            snap = dict(run=Path(big["run"]).name, waits=big["waits"], wait_s_p50=big["wait_s_p50"],
                        reread_mib_p50=big["reread_mib_p50"], reread_mib_p90=big["reread_mib_p90"],
                        policies=[{k: p[k] for k in ("T_s", "waits_snapshotted", "memory_freed_share",
                                                      "delay_ms_per_command_mean", "delay_ms_per_snapshotted_command_p50",
                                                      "delay_ms_per_snapshotted_command_p90", "diff_snapshot_mib_per_s")}
                                  for p in big["policies"] if p["T_s"] in (0, 5, 10, 30)])
            for p in snap["policies"]:
                print(f"          snapshot T>={p['T_s']:>2}s: freed {p['memory_freed_share']}, delay mean "
                      f"{p['delay_ms_per_command_mean']} ms/cmd, snapshotted p50/p90 "
                      f"{p['delay_ms_per_snapshotted_command_p50']}/{p['delay_ms_per_snapshotted_command_p90']} ms, "
                      f"diff {p['diff_snapshot_mib_per_s']} MiB/s per sandbox")
        out[w] = dict(rows=rows, reductions=red, snapshot=snap)
    over = []
    for x in (100, 80, 65):
        s = load(a.summary, f"2-rl-{x}", "steps")
        if s:
            step = s["steps"][-1]
            over.append(dict(level=x, budget=s["budget_gib"], live=step["live_mean"], slowdown=step["cpu_weighted_slowdown"],
                             p99=step["heavy_p99"], oom=step["oom_kills"], psi_mem_full=step["psi_mem_full"],
                             refault_file_per_s=step["refault_file_per_s"], refault_anon_per_s=step["refault_anon_per_s"],
                             majfault_per_s=step["majfault_per_s"]))
            print(f"overflow {x}%: {over[-1]}")
    out["overflow"] = over
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
