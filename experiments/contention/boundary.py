#!/usr/bin/env python3
"""Boundary placement versus reactive hand-off. All inputs are read-only; JSONL runs resume by config ID.

Run this entrypoint under numactl --cpunodebind=0 --membind=0 nice -n 19, with BLAS threads=1.
The 60 s agent timeout is recorded. Use --timeout-s 1000000 for uncensored SLO comparisons.
"""
import argparse
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import numpy as np

import cap_sweep as cap
import fleet

HERE = Path(__file__).resolve().parent
OLD = Path("/home/ijkim/agent-sandbox-cxl")
OUT = HERE / ".local/boundary"
SOLOS = {"rl1": "20261026T133107Z-solo-5i8p_9nm", "rl8": "20261104T092752Z-solo-pdkup4sy"}
STATE = {}


def read_module(name, path):
    # Prevent importlib from writing __pycache__ into the old checkout.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


def quantiles(values):
    x = np.asarray(values)
    return dict(zip(("p10", "p50", "p90", "p99"), np.percentile(x, [10, 50, 90, 99]).tolist())) if len(x) else {}


def predictions(profiles, programs, names, policy):
    """Leave-one-trace-out widths: held-out trace contributes neither repository nor fallback data."""
    classifier = read_module("boundary_cmdkind", OLD / "experiments/workload-char/cmdkind.py")
    entries = {t["name"]: t for t in json.loads(cap.CANDIDATES.read_text())["traces"]}
    repo = lambda n: re.sub(r"-\d+$", "", n)
    labels, samples = {}, {}
    for name in names:
        batches = cap.replay.load_trace(str(OLD / entries[name]["trace"]))["batches"]
        # Profiles were emitted in sorted batch order; use the events to align even if batch IDs have holes.
        directory = next(d for d in cap.DOCKER.iterdir() if d.is_dir() and d.name.split("-", 1)[-1] == name)
        events = [json.loads(line) for line in (directory / "events.jsonl").open()]
        ids = sorted({e["batch"] for e in events if e["event"] == "command_start"})
        labels[name] = [any(classifier.kind(a["command"]) in ("test", "build", "install")
                            for a in batches[b]["actions"]) for b in ids]
        samples[name] = [float(cap.stretch(d, cap.vcpus_for(policy, profiles[name])).mean())
                         for flag, (_, d) in zip(labels[name], profiles[name]["commands"]) if flag]
    predicted, errors, ratios, truth, flags, peak_ratios, cvs = {}, [], [], [], [], [], []
    fallback_count = 0
    cpu_work = []
    for name in names:
        peer = [w for n in names if n != name and repo(n) == repo(name) for w in samples[n]]
        if not peer:
            peer = [w for n in names if n != name for w in samples[n]]
            fallback_count += 1
        estimate = float(np.median(peer)) if peer else 1.0
        predicted[name] = []
        command_i = 0
        for kind, value in programs[name]["program"]:
            if kind == "gap":
                predicted[name].append((False, 0.0, 0.0))
                continue
            flag = labels[name][command_i]
            command_i += 1
            actual = value.sum() * fleet.DT >= 1.0
            predicted[name].append((flag, estimate, float(np.percentile(value, 90))))
            truth.append(actual)
            cpu_work.append(float(value.sum() * fleet.DT))
            flags.append(flag)
            if flag:
                errors.append(estimate - value.mean())
                if value.mean() > 0:
                    ratios.append(estimate / value.mean())
            if actual:
                peak_ratios.append(float(np.percentile(value, 90) / value.mean()))
                cvs.append(float(value.std() / value.mean()))
    truth, flags = np.array(truth), np.array(flags)
    cpu_work = np.array(cpu_work)
    report = {"policy": policy, "commands": len(truth), "actual_heavy": int(truth.sum()),
              "predicted_heavy": int(flags.sum()), "recall": float((truth & flags).sum() / truth.sum()),
              "precision": float((truth & flags).sum() / flags.sum()),
              "cpu_recall": float(cpu_work[truth & flags].sum() / cpu_work[truth].sum()),
              "heavy_cpu_share": float(cpu_work[truth].sum() / cpu_work.sum()),
              "width_error_cores": quantiles(errors), "width_predicted_over_actual": quantiles(ratios),
              "heavy_p90_over_mean": quantiles(peak_ratios), "heavy_cv": quantiles(cvs),
              "fallback_traces": fallback_count, "fit": "other traces in same repo; else other traces globally"}
    return predicted, report


def prepare(workloads):
    state = {"implementation_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in (HERE / "fleet.py", HERE / "cap_sweep.py", Path(__file__))},
             "arrays": {}, "names": {}, "solo": {}, "predictors": [], "sources": {}}
    if any(w.startswith("cap") for w in workloads):
        cap.REPO = OLD
        cap.DOCKER = OLD / "harness/trace-replay/results/numa-immediate-20261014/node0"
        profiles = cap.load_profiles(root=cap.DOCKER)
        for policy in sorted(w for w in workloads if w.startswith("cap")):
            programs, _ = cap.programs_for(profiles, policy)
            names = sorted(programs)
            arrays = fleet.flatten(programs, names)
            pred, report = predictions(profiles, programs, names, policy)
            state["predictors"].append(report)
            flat = [p for n in names for p in pred[n]]
            arrays["class_heavy"] = np.array([p[0] for p in flat], np.bool_)
            arrays["class_width"] = np.array([p[1] for p in flat])
            arrays["peak_width"] = np.array([p[2] for p in flat])
            state["arrays"][policy], state["names"][policy] = arrays, names
            state["solo"][policy] = cap.solo_summary(profiles, policy)
        state["sources"]["multilingual"] = str(cap.DOCKER)
    for workload in sorted(w for w in workloads if w.startswith("rl")):
        solo = OLD / "experiments/contention/.local/runs" / SOLOS[workload]
        candidates = HERE / "candidates-rl.json"
        programs = fleet.load_programs(solo, candidates)
        if not programs:
            raise ValueError(f"no eligible traces in {solo}")
        names = sorted(programs)
        arrays = fleet.flatten(programs, names)
        total_work = float(arrays["seg_cpu_s"].sum())
        state["arrays"][workload], state["names"][workload] = arrays, names
        state["solo"][workload] = {"traces": len(names), "vcpus": programs[names[0]]["vcpus"],
                                   "cpu_s": total_work, "cpu_profile_s": float(arrays["trace_cpu_s"].sum()),
                                   "gap_s": float(arrays["trace_gap_s"].sum()), "tool_time_ratio": 1.0}
        state["sources"][workload] = {"solo": str(solo), "candidates": str(candidates),
                                    "run_vcpus": json.loads((solo / "run.json").read_text())["vcpus"]}
    return state


def variant(name):
    c = dict(variant=name, mechanism="none", pool="none", bnd_on=False, predictor="oracle",
             bnd_latency_s=0.0, bnd_cpu_s=0.0, bnd_k=0, bnd_view=0, bnd_margin=1.0, width_mode="mean")
    if name == "pool":
        c["pool"] = "all"
    elif name == "reactive":
        c["mechanism"] = "hand-off 10ms"
    elif name == "migration":
        c.update(mechanism="migration pre-copy", detect_ms=500, hold_s=5.0)
    elif name.startswith("bnd"):
        c.update(bnd_on=True, predictor="class" if "class" in name else "oracle")
        c["bnd_latency_s"] = 1.0 if "1s" in name else 0.1 if "100ms" in name else 0.01 if "10ms" in name else 0.001
        c["bnd_cpu_s"] = 0.002 if c["bnd_latency_s"] == 0.01 else 0.001
        if "+reactive" in name:
            c["mechanism"] = "hand-off 10ms"
        if "view1" in name:
            c["bnd_view"] = 1
        if "peak" in name:
            c["width_mode"] = "peak"
        if "k2" in name:
            c["bnd_k"] = 2
    return c


def make_grid(args, state):
    configs = []
    for priority in args.priorities:
        workloads = ["cap30"] if priority in (1, 4) else ["cap16"] if priority == 2 else ["rl1", "rl8"]
        for workload in workloads:
            gaps = [1.0, 0.25] if priority == 3 else [1.0]
            for gap in gaps:
                solo = state["solo"][workload]
                demand = (solo["cpu_s"] / (solo["cpu_profile_s"] + solo["gap_s"] * gap + solo["traces"] * fleet.SANDBOX_GAP_S)
                          if priority == 3 else solo["mean_demand_cores"])
                for cores in ([96, 192] if priority == 2 else [192] if priority == 4 else [48, 96, 192]):
                    for util in ([70, 80, 85, 90, 95] if priority == 2 else [50, 60, 70, 80, 90] if priority == 3
                                 else [200] if priority == 4 else [50, 60, 70, 80, 85, 90, 95]):
                        variants = ["none", "pool", "reactive", "bnd-oracle-1ms", "bnd-oracle-100ms"]
                        if priority in (1, 2, 4):
                            variants += ["bnd-oracle-1ms+reactive"]
                        if priority == 1:
                            variants += ["bnd-oracle-10ms", "bnd-oracle-1s", "bnd-class-1ms", "bnd-class-100ms",
                                         "bnd-class-1ms+reactive"]
                            if util in (80, 90):
                                variants += ["migration"]
                                if cores == 192:
                                    variants += ["bnd-oracle-1ms-view1", "bnd-oracle-1ms-peak", "bnd-oracle-1ms-k2"]
                        per_node = max(1, round(util / 100 * cores / demand))
                        if priority == 4:
                            # Exactly cap_sweep section 4: batch=16, group=8, 30 s training.
                            per_node = max(8, round(per_node / 8) * 8)
                        for name in variants:
                            for seed in (1, 2):
                                c = fleet.DEFAULTS | dict(priority=priority, workload=workload, nodes=16, cores=cores,
                                    target_util=util, per_node=per_node, floating=True, carry=True, seed=str(seed),
                                    stable_placement=True, max_batch=8, nic_gbps=100, gap_scale=gap,
                                    ramp_s=args.ramp_s, duration_s=args.duration_s, timeout_s=args.timeout_s,
                                    oracle_threshold=1.0) | variant(name)
                                if args.timeout_s <= 60:
                                    c["strict_timeout"] = True
                                if priority == 3:
                                    c["choice_width"] = 64
                                if priority == 4:
                                    c.update(rl=True, batch=16, group=8, train_s=30.0)
                                configs.append(c)
    return configs


def config_id(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]


def simulate(config):
    fleet.TIMEOUT_S = config["timeout_s"]
    workload = config["workload"]
    arrays = STATE["arrays"][workload].copy()
    actual = arrays["seg_cpu_s"] >= config["oracle_threshold"]
    arrays["seg_heavy"] = arrays["class_heavy"] if config["predictor"] == "class" else actual
    arrays["seg_width"] = (arrays["class_width"] if config["predictor"] == "class" else arrays["peak_width"]
                            if config["width_mode"] == "peak" else arrays["seg_cpu_s"] / np.maximum(fleet.DT, arrays["seg_len"] * fleet.DT))
    result = fleet.simulate(dict(arrays=arrays, names=STATE["names"][workload], config=config))
    result["config_id"] = config_id(config)
    result["sandbox_per_core"] = config["per_node"] / config["cores"]
    result["gib_per_core"] = result["sandbox_per_core"] * 228 / 1024 if workload.startswith("rl") else None
    result["tool_time_ratio"] = STATE["solo"][workload]["tool_time_ratio"]
    return result


def check_load(log):
    """Refuse a grid above four external cores on NUMA node 0; retry once after ten minutes."""
    cpus_text = subprocess.check_output(["numactl", "--hardware"], text=True)
    node0 = set(map(int, re.search(r"node 0 cpus: (.*)", cpus_text)[1].split()))
    for attempt in range(2):
        up = subprocess.check_output(["uptime"], text=True)
        ps = subprocess.check_output(["ps", "-eo", "user,pid,psr,pcpu,comm", "--sort=-pcpu"], text=True)
        busy = []
        for line in ps.splitlines()[1:]:
            user, pid, cpu, pct, comm = line.split(None, 4)
            if int(cpu) in node0 and int(pid) != os.getpid() and comm not in ("ps",) and float(pct) > 0:
                busy.append((int(pid), float(pct), comm))
        record = dict(time=time.time(), uptime=up.strip(), top20=ps.splitlines()[:21],
                      external_node0_cpu_pct=sum(pct for _, pct, _ in busy), attempt=attempt)
        with log.open("a") as out:
            out.write(json.dumps(record) + "\n")
        print(up.strip(), f"node0 external CPU={record["external_node0_cpu_pct"]:.1f}%", flush=True)
        if record["external_node0_cpu_pct"] <= 400:
            return True
        if attempt == 0:
            # Short sleeps allow interruption and a progress message each minute.
            for minute in range(10):
                print(f"node0 busy; waiting minute {minute+1}/10", flush=True)
                time.sleep(60)
    return False


def main():
    global STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["grid", "run", "bench"])
    parser.add_argument("--priorities", type=int, nargs="+", default=[1, 2, 3, 4])
    parser.add_argument("--ramp-s", type=float, default=600)
    parser.add_argument("--duration-s", type=float, default=5400)
    parser.add_argument("--timeout-s", type=float, default=60)
    parser.add_argument("--grid", type=Path, default=OUT / "grid.json")
    parser.add_argument("--output", type=Path, default=OUT / "results.jsonl")
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--index", type=int, default=0, help="bench config index")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 24:
        parser.error("jobs must be 1..24")
    OUT.mkdir(parents=True, exist_ok=True)
    configs = json.loads(args.grid.read_text()) if args.command != "grid" else []
    workloads = {c["workload"] for c in configs} if configs else {
        w for p in args.priorities for w in (["cap30"] if p in (1, 4) else ["cap16"] if p == 2 else ["rl1", "rl8"])}
    STATE = prepare(workloads)
    metadata = {k: v for k, v in STATE.items() if k not in ("arrays", "names")}
    args.output.with_suffix(".inputs.json").write_text(json.dumps(metadata, indent=2))
    if args.command == "grid":
        configs = make_grid(args, STATE)
        args.grid.write_text(json.dumps(configs, indent=1))
        print(f"{len(configs)} configs -> {args.grid}")
        return
    if args.command == "bench":
        config = configs[args.index]
        started = time.monotonic()
        print(json.dumps(simulate(config)), flush=True)
        print(f"wall including preparation excluded: {time.monotonic()-started:.2f} s", flush=True)
        return
    if not check_load(OUT / "load.jsonl"):
        print("node0 still busy after ten minutes; no grid started", flush=True)
        raise SystemExit(75)
    done = {json.loads(line)["config_id"] for line in args.output.open()} if args.output.exists() else set()
    todo = [c for c in configs if config_id(c) not in done]
    print(f"{len(todo)}/{len(configs)} configs, {args.jobs} workers", flush=True)
    started = time.monotonic()
    with multiprocessing.get_context("fork").Pool(args.jobs) as pool, args.output.open("a") as out:
        for i, result in enumerate(pool.imap_unordered(simulate, todo), 1):
            out.write(json.dumps(result) + "\n")
            out.flush()
            print(f"{i}/{len(todo)} {result["workload"]} {result["cores"]} {result["target_util"]} {result["variant"]} "
                  f"seed={result["seed"]} wall={result["wall_s"]:.1f}s elapsed={time.monotonic()-started:.0f}s", flush=True)
    if not check_load(OUT / "load.jsonl"):
        raise SystemExit(75)


if __name__ == "__main__":
    main()
