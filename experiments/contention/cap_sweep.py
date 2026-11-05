#!/usr/bin/env python3
"""Hard cap vs no cap vs per-image size, with and without moves: sandbox slowdown against node contention.

Programs come from the 10-14 Docker DRAM replay (harness/trace-replay/results/numa-immediate-20261014/node0: a cpuset
of 30 cores, no CPU quota, cgroup CPU every 20 ms) instead of a microVM solo run, so one profile gives every width.
Per command, a 10 ms slot of demand d is served at most V cores at a time and the part above V runs right after it
(a slot with d > V becomes d/V slots of V): the work is kept and the command stretches. Gaps are the recorded LLM
waits. Sizing policies:
  capV      every sandbox has V vCPUs; cap30 is the uncapped profile (all 30 cores the replay could use)
  imageQ    each trace gets the smallest power of two >= the width under which Q% of its CPU-seconds ran, at most 16
Slowdowns in the simulation are against the uncapped solo time of the same trace, so they include the cap's own
stretch as well as contention. Commands are not cut at 60 s (the agent's timeout would hide the cap's cost);
`programs` counts the commands a cap pushes past 60 s and 180 s instead.

  python3 cap_sweep.py test
  python3 cap_sweep.py programs
  python3 cap_sweep.py bursts
  python3 cap_sweep.py grid --out grids/cap.json
  numactl --cpunodebind=0 --membind=0 nice -n 19 python3 cap_sweep.py run --grid grids/cap.json --jobs 24 \
      --output .local/cap/cap.jsonl
  python3 cap_sweep.py report .local/cap/cap.jsonl
"""

import argparse
import csv
import importlib.util
import json
import math
import multiprocessing
from pathlib import Path
import time

import numpy as np

import fleet

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DOCKER = REPO / "harness/trace-replay/results/numa-immediate-20261014/node0"
CANDIDATES = HERE / "candidates-all.json"
REPLAY_CORES = 30
DT = fleet.DT
POLICIES = ["cap1", "cap2", "cap4", "cap8", "cap16", "cap30", "image50", "image90"]
VARIANTS = [("none", "none"), ("hand-off 10ms", "none"), ("migration pre-copy", "none"), ("none", "all")]
MOVE_WINDOWS = {"hand-off 10ms": (50, 1.0), "migration pre-copy": (500, 5.0)}  # detect_ms, hold_s

spec = importlib.util.spec_from_file_location("replay", REPO / "harness/trace-replay/experiment.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def load_profiles(root=DOCKER, candidates=CANDIDATES):
    """Per trace: [(recorded gap before the command, uncapped demand per 10 ms in cores)] and guest memory."""
    entries = {t["name"]: t for t in json.loads(Path(candidates).read_text())["traces"]}
    out = {}
    for d in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        name = d.name.split("-", 1)[1]
        if name not in entries or not (d / "metrics.csv").exists():
            continue
        t, cpu = [], []
        with (d / "metrics.csv").open() as stream:
            for row in csv.DictReader(stream):
                if row["phase"] == "running":
                    t.append(float(row["elapsed_s"]))
                    cpu.append(float(row["cpu_usec"]) / 1e6)
        events = [json.loads(line) for line in (d / "events.jsonl").open()]
        start = {e["batch"]: e["elapsed_s"] for e in events if e["event"] == "command_start"}
        end = {e["batch"]: e["elapsed_s"] for e in events if e["event"] == "command_end"}
        if len(t) < 2 or not start or set(start) != set(end):
            continue
        batches = replay.load_trace(str(REPO / entries[name]["trace"]))["batches"]
        commands = []
        for b in sorted(start):
            n = max(1, math.ceil((end[b] - start[b]) / DT - 1e-9))
            edges = np.minimum(start[b] + DT * np.arange(n + 1), end[b])
            demand = np.clip(np.diff(np.interp(edges, t, cpu)) / DT, 0.0, REPLAY_CORES)
            commands.append((batches[b]["gap"] if commands else 0.0, demand))
        out[name] = {"commands": commands, "memory_gib": entries[name].get("memory_mib", 2048) / 1024}
    return out


def stretch(demand, v):
    """Serve at most v cores: a slot of demand d > v lasts d/v slots at rate v, later slots shift right. The stretched
    rate curve is resampled onto whole slots (work is kept; the command's last slot rounds up to a whole slot)."""
    if not len(demand) or demand.max() <= v:
        return demand
    edges = np.concatenate([[0.0], np.cumsum(np.maximum(1.0, demand / v))])  # stretched slot boundaries
    work = np.concatenate([[0.0], np.cumsum(demand)])  # cumulative work, linear inside each stretched slot
    grid = np.minimum(np.arange(math.ceil(edges[-1] - 1e-9) + 1, dtype=float), edges[-1])
    return np.diff(np.interp(grid, edges, work))


def width(demands, q):
    """Width under which q% of the CPU-seconds ran (each slot weighted by its CPU)."""
    d = np.sort(np.concatenate(demands))
    d = d[d > 0]
    if not len(d):
        return 1.0
    cum = np.cumsum(d) / d.sum()
    return float(d[min(len(d) - 1, np.searchsorted(cum, q / 100))])


def vcpus_for(policy, profile):
    if policy.startswith("cap"):
        return int(policy[3:])
    w = width([d for _, d in profile["commands"]], int(policy[5:]))
    return min(16, 2 ** max(0, math.ceil(math.log2(max(w, 1.0)) - 1e-9)))


def programs_for(profiles, policy):
    """fleet.py programs under a policy, plus each trace's uncapped busy seconds (the slowdown reference)."""
    programs, base = {}, {}
    for name, p in profiles.items():
        v = vcpus_for(policy, p)
        program = []
        for gap, demand in p["commands"]:
            if program and gap > 0:
                program.append(("gap", gap))
            program.append(("cpu", stretch(demand, v)))
        programs[name] = {"program": program, "vcpus": v, "memory_gib": p["memory_gib"]}
        base[name] = sum(len(d) for _, d in p["commands"]) * DT
    return programs, base


def solo_summary(profiles, policy):
    """Solo (uncontended) cost of a policy and the mean demand of one sandbox slot."""
    per_trace, tool_base, tool_cap, over60, over180, cpu_s, time_s, sizes = [], 0.0, 0.0, 0, 0, 0.0, 0.0, []
    for p in profiles.values():
        v = vcpus_for(policy, p)
        sizes.append(v)
        base = cap = gaps = 0.0
        for gap, demand in p["commands"]:
            b, c = len(demand) * DT, len(stretch(demand, v)) * DT
            base, cap, gaps = base + b, cap + c, gaps + gap
            over60 += c >= 60 > b
            over180 += c >= 180 > b
            cpu_s += demand.sum() * DT
        tool_base, tool_cap = tool_base + base, tool_cap + cap
        per_trace.append((cap + gaps) / (base + gaps))
        time_s += cap + gaps + fleet.SANDBOX_GAP_S
    per_trace = np.array(per_trace)
    return {"policy": policy, "traces": len(per_trace), "vcpus": dict(sorted(
                {str(v): sizes.count(v) for v in set(sizes)}.items(), key=lambda kv: int(kv[0]))),
            "tool_time_ratio": tool_cap / tool_base,
            **{f"solo_slowdown_p{q}": float(np.percentile(per_trace, q)) for q in (50, 90, 99)},
            "solo_slowdown_mean": float(per_trace.mean()), "commands_newly_over_60s": int(over60),
            "commands_newly_over_180s": int(over180), "mean_demand_cores": cpu_s / time_s}


def cmd_test(_args):
    assert np.allclose(stretch(np.array([0.5, 2.5, 0.0]), 1), [0.5, 1, 1, 0.5, 0.0])  # 4.5 slots, last rounds up
    assert np.allclose(stretch(np.array([4.0]), 2), [2, 2])
    assert np.allclose(stretch(np.array([1.0, 3.0]), 4), [1.0, 3.0])
    assert np.allclose(stretch(np.full(10, 1.2), 1), np.ones(12))  # 12 slots, not 20
    x = np.random.default_rng(0).random(1000) * 30
    for v in (1, 2, 4, 8, 16, 30):
        y = stretch(x, v)
        assert abs(y.sum() - x.sum()) < 1e-6 and y.max() <= v + 1e-9, v
    d = np.array([1.0, 1.0, 8.0])  # CPU-seconds: 20% at width 1, 80% at width 8
    assert width([d], 15) == 1.0 and width([d], 50) == 8.0
    profile = lambda w: {"commands": [(0.0, np.array([w]))]}
    assert [vcpus_for("image50", profile(w)) for w in (0.5, 1.0, 3.0, 4.0, 20.0)] == [1, 1, 4, 4, 16]
    print("ok")


def cmd_programs(args):
    profiles = load_profiles()
    for policy in args.policies:
        print(json.dumps(solo_summary(profiles, policy)))


def cmd_bursts(args):
    """When a sandbox first runs wide (>= --cores over 100 ms) in its life, and how many separate wide bursts it has."""
    profiles, rows = load_profiles(), []
    for profile in profiles.values():
        t, times, demand = 0.0, [], []
        for gap, d in profile["commands"]:
            t += gap
            times.append(t + DT * np.arange(len(d)))
            demand.append(d)
            t += DT * len(d)
        times, demand = np.concatenate(times), np.concatenate(demand)
        k = max(1, int(round(0.1 / DT)))
        wide = np.convolve(demand, np.ones(k) / k, "same") >= args.cores
        if wide.any():
            starts = times[wide]
            rows.append((starts[0], starts[0] / t, 1 + int((np.diff(starts) > args.gap_s).sum())))
    first, share, bursts = (np.array(c) for c in zip(*rows))
    print(json.dumps({"traces": len(profiles), "wide": len(rows),
                      "first_s_p10_p50_p90": np.percentile(first, [10, 50, 90]).round(1).tolist(),
                      "first_share_of_life_p10_p50_p90": np.percentile(share, [10, 50, 90]).round(3).tolist(),
                      "first_after_25pct": round(float((share > 0.25).mean()), 3),
                      "first_after_50pct": round(float((share > 0.5).mean()), 3),
                      "first_within_60s": round(float((first < 60).mean()), 3),
                      "bursts_p50_p90": np.percentile(bursts, [50, 90]).tolist()}))


def cmd_grid(args):
    profiles = load_profiles()
    demand = {p: solo_summary(profiles, p)["mean_demand_cores"] for p in args.policies}
    unit = args.batch * args.group if args.rl else 1
    step = unit // math.gcd(unit, args.nodes)  # per_node x nodes must be a multiple of batch x group
    configs = []
    for policy in args.policies:
        for cores in args.cores:
            for util in args.util:
                per_node = max(step, round(util / 100 * cores / demand[policy] / step) * step)
                for mechanism, pool in VARIANTS:
                    detect_ms, hold_s = MOVE_WINDOWS.get(mechanism, (50, 1.0))
                    for seed in range(1, args.seeds + 1):
                        config = dict(stage="cap", policy=policy, nodes=args.nodes, cores=cores, per_node=per_node,
                                      floating=True, pool=pool, seed=str(seed), ramp_s=args.ramp_s,
                                      duration_s=args.duration_s, target_util=util, carry=True, max_batch=8,
                                      mechanism=mechanism, nic_gbps=100, detect_ms=detect_ms, hold_s=hold_s)
                        if args.rl:
                            config.update(rl=True, batch=args.batch, group=args.group, train_s=args.train_s)
                        configs.append(config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(configs, indent=1))
    print(f"{len(configs)} configs -> {args.out}; mean demand per sandbox: "
          + ", ".join(f"{p} {d:.3f}" for p, d in demand.items()))


_JOB = {}


def _simulate(config):
    fleet.TIMEOUT_S = _JOB["timeout_s"]
    arrays, names = _JOB["arrays"][config["policy"]], _JOB["names"]
    return fleet.simulate({"arrays": arrays, "names": names, "config": config, "groups": [], "images": names,
                           "blocks": None})


def cmd_run(args):
    configs = [fleet.DEFAULTS | c for c in json.loads(args.grid.read_text())]
    key = lambda c: json.dumps({k: c[k] for k in sorted(c) if k != "rho"}, sort_keys=True)
    done = set()
    if args.output.exists():
        done = {key(r) for r in map(json.loads, args.output.open())}
    todo = [c for c in configs if key(c) not in done]
    profiles = load_profiles()
    names = sorted(profiles)
    policies = sorted({c["policy"] for c in configs})
    _JOB.update(timeout_s=args.timeout_s, names=names, arrays={})
    for policy in policies:
        programs, base = programs_for(profiles, policy)
        arrays = fleet.flatten(programs, names)
        arrays["trace_cpu_s"] = np.array([base[n] for n in names])  # slowdowns against the uncapped solo run
        _JOB["arrays"][policy] = arrays
    args.output.parent.mkdir(parents=True, exist_ok=True)
    solo = args.output.with_suffix(".solo.json")
    solo.write_text(json.dumps([solo_summary(profiles, p) for p in policies], indent=1))
    print(f"{len(todo)} of {len(configs)} configs to run, {len(names)} traces, solo summary -> {solo}", flush=True)
    started = time.time()
    with multiprocessing.get_context("fork").Pool(args.jobs) as pool, args.output.open("a") as out:
        for i, result in enumerate(pool.imap_unordered(_simulate, todo), 1):
            result = {k: v for k, v in result.items() if k != "rho"}
            out.write(json.dumps(result) + "\n")
            out.flush()
            if i % 20 == 0 or i == len(todo):
                print(f"{i}/{len(todo)} done, {time.time() - started:.0f} s", flush=True)


def cmd_report(args):
    rows = [json.loads(line) for line in args.results.open()]
    solo = {s["policy"]: s for s in json.loads(args.results.with_suffix(".solo.json").read_text())}
    print("solo cost of each policy (no contention):")
    print("policy   vCPUs                        tool x  trace p50  p90   p99   cmds>60s  demand")
    for p, s in solo.items():
        print(f"{p:8s} {json.dumps(s['vcpus']):28s} {s['tool_time_ratio']:5.2f}  {s['solo_slowdown_p50']:5.2f}  "
              f"{s['solo_slowdown_p90']:5.2f} {s['solo_slowdown_p99']:5.2f}  {s['commands_newly_over_60s']:6d}  "
              f"{s['mean_demand_cores']:.3f}")
    groups = {}
    for r in rows:
        groups.setdefault((r["cores"], r["policy"], r["target_util"], r["mechanism"], r["pool"]), []).append(r)
    mean = lambda rs, k: float(np.mean([r[k] for r in rs if r.get(k) is not None])) if any(
        r.get(k) is not None for r in rs) else float("nan")
    order = {p: i for i, p in enumerate(POLICIES)}
    variants = [f"{m}/{p}" for m, p in VARIANTS]
    rl = any(r.get("rl") for r in rows)
    metric = "step_slowdown_p90" if rl else "trace_slowdown_p90"
    if args.csv:
        fields = ["util_pct", "lost_pct", "tool_slowdown", "trace_slowdown_p50", "trace_slowdown_p90",
                  "trace_slowdown_p99", "step_slowdown_p50", "step_slowdown_p90", "step_slowdown_p99", "moves_per_s"]
        with args.csv.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cores", "policy", "target_util", "mechanism", "pool", "seeds", *fields])
            for (cores, policy, util, m, p), rs in sorted(groups.items(), key=lambda kv: (
                    kv[0][0], order.get(kv[0][1], 99), kv[0][2], kv[0][3], kv[0][4])):
                lost = mean(rs, "lost_over_run")
                values = [mean(rs, "run_cores_per_node") / cores * 100, lost * 100,
                          solo[policy]["tool_time_ratio"] * (1 + lost),  # cap stretch x contention wait (fluid)
                          *(mean(rs, k) for k in fields[3:9]), mean(rs, "moves") / rs[0]["duration_s"]]
                writer.writerow([cores, policy, util, m, p, len(rs), *(f"{v:.4g}" for v in values)])
    for cores in sorted({k[0] for k in groups}):
        print(f"\n{cores} cores per node: actual util %, lost CPU %, {metric} (vs uncapped solo)")
        print("policy   target  " + "  ".join(f"{v:>28s}" for v in variants))
        for policy in sorted({k[1] for k in groups if k[0] == cores}, key=lambda p: order.get(p, 99)):
            for util in sorted({k[2] for k in groups if k[0] == cores and k[1] == policy}):
                cells = []
                for m, p in VARIANTS:
                    rs = groups.get((cores, policy, util, m, p), [])
                    cells.append(f"{mean(rs, 'run_cores_per_node') / cores * 100:5.1f} {mean(rs, 'lost_over_run') * 100:7.2f}"
                                 f" {mean(rs, metric):6.3f}" if rs else " " * 20)
                print(f"{policy:8s} {util:5d}  " + "  ".join(f"{c:>28s}" for c in cells))
        print(f"\nhighest actual utilization with {metric} <= SLO ({cores} cores; '-' = never)")
        print("SLO    policy   " + "  ".join(f"{v:>24s}" for v in variants))
        for slo in args.slo:
            for policy in sorted({k[1] for k in groups if k[0] == cores}, key=lambda p: order.get(p, 99)):
                cells = []
                for m, p in VARIANTS:
                    ok = [mean(rs, "run_cores_per_node") / cores * 100 for (c, po, u, mm, pp), rs in groups.items()
                          if (c, po, mm, pp) == (cores, policy, m, p) and mean(rs, metric) <= slo]
                    cells.append(f"{max(ok):.1f}" if ok else "-")
                print(f"{slo:5.2f}  {policy:8s} " + "  ".join(f"{c:>24s}" for c in cells))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("test").set_defaults(func=cmd_test)
    p = sub.add_parser("programs")
    p.add_argument("--policies", nargs="+", default=POLICIES)
    p.set_defaults(func=cmd_programs)
    p = sub.add_parser("bursts", help="when wide bursts happen in a sandbox's life")
    p.add_argument("--cores", type=float, default=8.0)
    p.add_argument("--gap-s", type=float, default=5.0, help="wide spans further apart are separate bursts")
    p.set_defaults(func=cmd_bursts)
    p = sub.add_parser("grid")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--policies", nargs="+", default=POLICIES)
    p.add_argument("--cores", type=int, nargs="+", default=[48, 96, 192])
    p.add_argument("--util", type=int, nargs="+", default=[60, 70, 80, 90])
    p.add_argument("--nodes", type=int, default=16)
    p.add_argument("--seeds", type=int, default=2)
    p.add_argument("--ramp-s", type=float, default=120)
    p.add_argument("--duration-s", type=float, default=600)
    p.add_argument("--rl", action="store_true", help="synchronous RL steps (batch x group rollouts, slowest wins)")
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--group", type=int, default=8)
    p.add_argument("--train-s", type=float, default=30.0)
    p.set_defaults(func=cmd_grid)
    p = sub.add_parser("run")
    p.add_argument("--grid", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True, help="JSON lines; configs already in it are skipped")
    p.add_argument("--jobs", type=int, default=24)
    p.add_argument("--timeout-s", type=float, default=1e6, help="command cut (fleet.TIMEOUT_S); default: none")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("results", type=Path)
    p.add_argument("--slo", type=float, nargs="+", default=[1.10, 1.25, 1.50])
    p.add_argument("--csv", type=Path, help="also write seed means per condition")
    p.set_defaults(func=cmd_report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
