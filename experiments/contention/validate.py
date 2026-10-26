#!/usr/bin/env python3
"""Compare measured pilot runs with fleet.py run on the same configuration (PLAN-fleet.md, validation ladder).

For every pilot run: read run.json, simulate the same nodes, density, pool, vCPUs, seed, move mechanism and group
mode with the matching solo run as input, and check the measured totals against the model with the criteria fixed
in PLAN-fleet.md:
- runqueue wait / CPU run: within 20% (relative) when the measurement is >= 5%, else within 1 point
- trace slowdown vs solo, p50 and p90: within 0.03
- RL step slowdown p50 (group mode): within 0.05
"""

import argparse
import json
from pathlib import Path

import fleet

HERE = Path(__file__).resolve().parent
MOVE = {"none": "none", "instant": "instant", "hand-off": "hand-off 10ms", "stop-and-copy": "migration stop-and-copy",
        "pre-copy": "migration pre-copy"}


def fleet_config(run, sched="share", carry=False, kernel_wait="none"):
    config = json.loads((run / "run.json").read_text())
    nodes = config["nodes"]
    cores = {len(n["cores"]) for n in nodes}
    assert len(cores) == 1, "nodes of different sizes"
    group = config.get("group_size", 0)
    return config, fleet.DEFAULTS | {
        "nodes": len(nodes), "cores": cores.pop(), "per_node": config["per_node"], "pool": config["pool"],
        "seed": str(config["seed"]), "ramp_s": config["ramp_s"], "duration_s": config["duration_s"],
        "mechanism": MOVE[config.get("move", "none")], "detect_ms": config.get("move_detect_ms", 50),
        "threshold": config.get("move_threshold", 0.1), "hold_s": config.get("move_hold_s", 1.0),
        "rl": bool(group), "batch": 1, "group": group or 8, "train_s": 0.0, "jitter_s": config.get("group_jitter_s", 2.0),
        "gap_scale": config.get("gap_scale", 1.0), "sched": sched, "carry": carry, "kernel_wait": kernel_wait}


def check(measured, modeled):
    rows = []
    wait_m, wait_s = measured["total_wait_over_run"], modeled["wait_over_run"]
    ok = abs(wait_s - wait_m) <= (0.20 * wait_m if wait_m >= 0.05 else 0.01)
    rows.append(("wait/run", wait_m, wait_s, ok))
    for q in ("p50", "p90"):
        m, s = measured["trace_slowdown_vs_solo"][q], modeled[f"trace_slowdown_{q}"]
        rows.append((f"trace slowdown {q}", m, s, m is not None and s is not None and abs(s - m) <= 0.03))
    if measured["step_slowdown_vs_solo"]["n"]:
        m, s = measured["step_slowdown_vs_solo"]["p50"], modeled["step_slowdown_p50"]
        rows.append(("step slowdown p50", m, s, s is not None and abs(s - m) <= 0.05))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--solo", action="append", required=True, help="vcpus=PATH, e.g. 1=.local/runs/...-solo-x")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sched", choices=("share", "cfs", "bernoulli"), default="share", help="Model CPU allocation")
    parser.add_argument("--carry", action="store_true", help="Pure fluid slot progress (lower bound on contention)")
    parser.add_argument("--kernel-wait", choices=("none", "slope", "floor"), default="none",
                        help="Add the fitted kernel scheduling wait (fleet.KERNEL_WAIT)")
    args = parser.parse_args()
    solos = {int(k): Path(v) for k, v in (s.split("=", 1) for s in args.solo)}
    cache, report = {}, []
    for run in sorted(args.runs):
        config, sim_config = fleet_config(run, args.sched, args.carry, args.kernel_wait)
        vcpus, candidates = config["vcpus"], Path(config.get("candidates", HERE / "candidates.json"))
        key = (vcpus, str(candidates))
        if key not in cache:
            programs = fleet.load_programs(solos[vcpus], candidates)
            names = [t["name"] for t in json.loads(candidates.read_text())["traces"] if t["name"] in programs]
            cache[key] = (fleet.flatten(programs, names), names)
        arrays, names = cache[key]
        modeled = fleet.simulate({"arrays": arrays, "names": names, "config": sim_config})
        measured = json.loads((run / "contention-1ms.json").read_text())
        rows = check(measured, modeled)
        report.append({"run": str(run), "config": sim_config, "checks": [
            {"metric": m, "measured": a, "model": b, "pass": p} for m, a, b, p in rows]})
        label = f"k{sim_config['per_node']} {sim_config['pool']} v{vcpus} {sim_config['mechanism']}" + \
                (f" g{sim_config['group']}" if sim_config["rl"] else "") + f" s{sim_config['seed']}"
        print(label.ljust(46), "  ".join(f"{m} {a:.3f}/{b:.3f}{'' if p else ' FAIL'}" for m, a, b, p in rows
                                          if a is not None and b is not None), flush=True)
    # Moves (PLAN-fleet.md V4): reduction of wait/run vs the matching no-move run, same ranking and within 10 points
    base = {(r["config"]["per_node"], r["config"]["pool"], r["config"]["seed"], r["config"]["rl"]): r for r in report
            if r["config"]["mechanism"] == "none"}
    moves = []
    for r in report:
        c = r["config"]
        b = base.get((c["per_node"], c["pool"], c["seed"], c["rl"]))
        if c["mechanism"] == "none" or not b:
            continue
        get = lambda rep, side: next(x[side] for x in rep["checks"] if x["metric"] == "wait/run")
        measured = 1 - get(r, "measured") / get(b, "measured")
        modeled = 1 - get(r, "model") / get(b, "model")
        moves.append({"run": r["run"], "mechanism": c["mechanism"], "per_node": c["per_node"], "seed": c["seed"],
                      "measured_reduction": measured, "model_reduction": modeled, "pass": abs(measured - modeled) <= 0.10})
        print(f"move k{c['per_node']} s{c['seed']} {c['mechanism']:<26} reduction measured {measured:+.2f} model {modeled:+.2f}"
              f"{'' if abs(measured - modeled) <= 0.10 else ' FAIL'}")
    for key in {(m["per_node"], m["seed"]) for m in moves}:
        group = [m for m in moves if (m["per_node"], m["seed"]) == key]
        same = [m["mechanism"] for m in sorted(group, key=lambda m: -m["measured_reduction"])] == \
               [m["mechanism"] for m in sorted(group, key=lambda m: -m["model_reduction"])]
        print(f"move ranking k{key[0]} s{key[1]}: {'same' if same else 'DIFFERENT'}")
    report.append({"moves": moves})
    args.output.write_text(json.dumps(report, indent=1))
    total = [c["pass"] for r in report if "checks" in r for c in r["checks"]]
    print(f"{sum(total)}/{len(total)} checks pass -> {args.output}")


if __name__ == "__main__":
    main()
