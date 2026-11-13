#!/usr/bin/env python3
"""Boundary placement versus reactive hand-off. All inputs are read-only; JSONL runs resume by config ID.

Run this entrypoint under numactl --cpunodebind=0 --membind=0 nice -n 19, with BLAS threads=1.
The 60 s agent timeout is recorded. Use --timeout-s 1000000 for uncensored SLO comparisons.
"""
import argparse
import csv
from collections import Counter
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


def prepare(workloads, advanced=False):
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
            if advanced:
                state["q3_inputs"] = q3_inputs(profiles, names, arrays)
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


def q3_inputs(profiles, names, arrays):
    """Global leave-one-trace-out history probabilities; copy state joined by event batch ID."""
    classifier = read_module("premove_cmdkind", OLD / "experiments/workload-char/cmdkind.py")
    entries = {t["name"]: t for t in json.loads(cap.CANDIDATES.read_text())["traces"]}
    directories = {d.name.split("-",1)[-1]:d for d in cap.DOCKER.iterdir() if d.is_dir()}
    sequences, per_trace, totals = {}, {}, Counter()
    for name in names:
        batches = cap.replay.load_trace(str(OLD / entries[name]["trace"]))["batches"]
        events = [json.loads(line) for line in (directories[name]/"events.jsonl").open()]
        ids = sorted({e["batch"] for e in events if e["event"]=="command_start"})
        previous, previous_heavy, ever_heavy = "start", False, False
        seq=[]
        counts=Counter()
        for b,(_,demand) in zip(ids, profiles[name]["commands"]):
            key=(previous,previous_heavy,ever_heavy)
            truth=float(demand.sum()*fleet.DT)>=1.0
            kinds={classifier.kind(action["command"]) for action in batches[b]["actions"]}
            # A batch takes the highest-priority kind, matching cmdkind's rule for a compound shell command.
            kind=next((k for k in classifier.KINDS if k in kinds),"other")
            seq.append((b,key,truth,float(demand.sum()*fleet.DT)))
            counts[(key,False)]+=1
            counts[(key,True)]+=int(truth)
            previous,previous_heavy,ever_heavy=kind,truth,ever_heavy or truth
        sequences[name],per_trace[name]=seq,counts
        totals.update(counts)
    state_path=OUT/"boundary-state.csv"
    state={(r["trace"],int(r["batch"])):r for r in csv.DictReader(state_path.open())}
    probability=np.zeros(len(arrays["seg_len"]))
    state_bytes=np.zeros(len(probability))
    copy_eligible=np.zeros(len(probability),np.bool_)
    truth_all=[];prob_all=[];work_all=[];gaps=[];heavy_gaps=[];heavy_gap_work=[]
    missing=[];state_weights=[];state_af=[];state_afw=[]
    for i,name in enumerate(names):
        command_i=0
        total_else=sum(totals[(key,False)]-per_trace[name][(key,False)] for key in {k for k,_ in totals})
        heavy_else=sum(totals[(key,True)]-per_trace[name][(key,True)] for key in {k for k,_ in totals})
        for g in range(arrays["trace_first"][i],arrays["trace_first"][i]+arrays["trace_count"][i]):
            if arrays["seg_kind"][g]!=0:continue
            b,key,truth,work=sequences[name][command_i]
            command_i+=1
            denominator=totals[(key,False)]-per_trace[name][(key,False)]
            prob=(totals[(key,True)]-per_trace[name][(key,True)])/denominator if denominator else heavy_else/total_else
            probability[g]=prob
            truth_all.append(truth);prob_all.append(prob);work_all.append(work)
            gap=(arrays["seg_len"][g-1]*fleet.DT if g>arrays["trace_first"][i] and arrays["seg_kind"][g-1]==1 else 0.0)
            gaps.append(gap)
            if truth:heavy_gaps.append(gap);heavy_gap_work.append(work)
            row=state.get((name,b))
            if row is None:
                missing.append(dict(trace=name,batch=b,heavy=truth,cpu_s=work))
                if command_i!=1 or b!=0:raise ValueError(f"missing non-boot boundary state: {name} batch {b}")
                continue
            copy_eligible[g]=True
            af=float(row["anon_bytes"])+float(row["file_bytes"])
            afw=af+float(row["cum_write_bytes"])
            state_bytes[g]=afw
            if truth:state_weights.append(work);state_af.append(af);state_afw.append(afw)
        assert command_i==len(sequences[name])
    arrays["hist_probability"]=probability
    arrays["boundary_state_bytes"]=state_bytes
    arrays["copy_eligible"]=copy_eligible
    truth_all,prob_all,work_all=map(np.asarray,(truth_all,prob_all,work_all))
    def report(threshold):
        flag=prob_all>=threshold
        return dict(threshold=threshold,recall=float((flag&truth_all).sum()/truth_all.sum()),
          precision=float((flag&truth_all).sum()/flag.sum()),cpu_recall=float(work_all[flag&truth_all].sum()/work_all[truth_all].sum()),
          predicted_fraction=float(flag.mean()))
    def weighted(values,weights):
        order=np.argsort(values);values=np.asarray(values)[order];cum=np.cumsum(np.asarray(weights)[order]);cum/=cum[-1]
        return {f"p{q}":float(values[min(len(values)-1,np.searchsorted(cum,q/100))]) for q in (10,50,90,99)}
    return dict(history=[report(t) for t in (.2,.3)],history_key="previous batch's highest-priority cmdkind, previous actual heavy, any strictly earlier actual heavy (including previous)",
        fit="all other traces, subtract held-out trace counts; unseen key falls back to other-trace heavy frequency",
        commands=len(truth_all),missing_state=missing,missing_boot_heavy_cpu_fraction=sum(r["cpu_s"] for r in missing if r["heavy"])/sum(work_all[truth_all]),state_file=str(state_path),state_sha256=hashlib.sha256(state_path.read_bytes()).hexdigest(),
        heavy_gap_s=quantiles(heavy_gaps),heavy_cpu_gap_at_least3_fraction=float(np.sum(np.asarray(heavy_gap_work)[np.asarray(heavy_gaps)>=3])/sum(heavy_gap_work)),
        heavy_cpu_weighted_anon_file_bytes=weighted(state_af,state_weights),heavy_cpu_weighted_copy_bytes=weighted(state_afw,state_weights))


def taxed_arrays(arrays, tau):
    """Stretch time at unchanged width, conserve tau*CPU, and keep DRAM reference denominators."""
    out=arrays.copy()
    out["seg_reference_len"]=arrays["seg_len"].copy()
    out["seg_reference_cpu_s"]=arrays["seg_cpu_s"].copy()
    demands=[];starts=[];lengths=[];cpu=[];offset=0
    for g,(kind,start,length) in enumerate(zip(arrays["seg_kind"],arrays["seg_start"],arrays["seg_len"])):
        if kind==1:
            starts.append(0);lengths.append(length);cpu.append(0.0);continue
        scale=tau if arrays["seg_cpu_s"][g]>=1.0 else 1.02
        demand=arrays["slot_demand"][start:start+length]
        edges=np.minimum(np.arange(int(np.ceil(length*scale-1e-9))+1),length*scale)
        work=scale*np.interp(edges/scale,np.arange(length+1),np.r_[0.0,np.cumsum(demand)])
        stretched=np.diff(work)
        # Cumulative interpolation can leave ~1e-13 cores at an idle edge. Such numerical
        # ghosts round to zero threads in the allocator. Remove them and conserve segment work.
        stretched[np.abs(stretched)<1e-9]=0.0
        if len(stretched) and stretched.max()>0:
            stretched[np.argmax(stretched)]+=scale*demand.sum()-stretched.sum()
        starts.append(offset);offset+=len(stretched);lengths.append(len(stretched));cpu.append(float(stretched.sum()*fleet.DT))
        demands.append(stretched)
    out["slot_demand"]=np.concatenate(demands)
    out["seg_start"]=np.asarray(starts,np.int64);out["seg_len"]=np.asarray(lengths,np.int64);out["seg_cpu_s"]=np.asarray(cpu)
    out["trace_taxed_cpu_s"]=np.array([sum(out["seg_len"][a:a+n][out["seg_kind"][a:a+n]==0])*fleet.DT for a,n in zip(out["trace_first"],out["trace_count"])])
    return out


def self_test():
    profile=np.tile(np.r_[np.full(7,2.3),np.zeros(13)],100)
    programs={"x":dict(program=[("cpu",profile)],vcpus=4,memory_gib=1.,boot=False)}
    arrays=fleet.flatten(programs,["x"])
    original=arrays["slot_demand"].copy()
    for tau in (1.,1.02,1.05,1.10,1.23):
        taxed=taxed_arrays(arrays,tau)
        demand=taxed["slot_demand"]
        assert np.all((demand==0)|(demand>1e-9))
        assert abs(demand.sum()-tau*original.sum())<1e-9
        assert np.array_equal(arrays["slot_demand"],original)
        assert np.array_equal(taxed["seg_reference_len"],arrays["seg_len"])
    # A taxed, uncontended command has slowdown tau against its original DRAM length.
    arrays=fleet.flatten({"x":dict(program=[("cpu",np.ones(100))],vcpus=1,memory_gib=1.,boot=False)},["x"])
    config=fleet.DEFAULTS|dict(nodes=1,cores=4,per_node=1,ramp_s=0,duration_s=1.5,mechanism="none",pool="none")
    result=fleet.simulate(dict(arrays=taxed_arrays(arrays,1.23),names=["x"],config=config))
    assert abs(result["cpu_weighted_slowdown"]-1.23)<1e-9,result
    assert abs(result["run_cores_per_node"]-1.23/1.5)<1e-9,result
    assert abs(result["dram_run_cores_per_node"]-1/1.5)<1e-9,result
    print("boundary self-test passed")


def q3_grid(args,state):
    configs=[]
    pre=[f"pre-{predictor}-{latency:g}s" for predictor in ("oracle","hist") for latency in (.1,.3,1.,3.)]
    combined=["pre-hist-1s+bnd-class-1ms","pre-hist-1s+bnd-class-100ms"]
    copies=["bnd-copy-25G","bnd-copy-100G"]
    def add(study,cores,target,names,gap=1.):
        arrays=state["arrays"]["cap30"]
        demand=state["solo"]["cap30"]["mean_demand_cores"] if gap==1 else arrays["seg_cpu_s"].sum()/(arrays["trace_cpu_s"].sum()+arrays["trace_gap_s"].sum()*gap+len(state["names"]["cap30"])*fleet.SANDBOX_GAP_S)
        for name in names:
            for seed in (1,2):
                c=fleet.DEFAULTS|dict(priority=20,study="12-"+study,workload="cap30",nodes=16,cores=cores,target_util=target,
                   per_node=max(1,round(target/100*cores/demand)),floating=True,carry=True,stable_placement=True,
                   seed=str(seed),max_batch=8,nic_gbps=100,gap_scale=gap,ramp_s=600,duration_s=5400.0,timeout_s=1e6,oracle_threshold=1.0)|variant(name)
                if name.startswith("pre-"):
                    c.update(pre_on=True,pre_predictor="oracle" if name.startswith("pre-oracle") else "hist",pre_threshold=.3 if "hist30" in name else .2,
                         pre_latency_s=float(re.search(r"-([0-9.]+)s",name)[1]),pre_cpu_s=.001)
                    if "+bnd-class" in name:
                        c.update(bnd_on=True,predictor="class",bnd_latency_s=.1 if "100ms" in name else .001,bnd_cpu_s=.001)
                elif name.startswith("bnd-copy"):
                    c.update(copy_gbps=float(re.search(r"-(\d+)G",name)[1]),bnd_on=True,predictor="oracle")
                elif name.startswith("bnd-") and name.endswith("s") and not name.endswith("ms"):
                    c.update(bnd_latency_s=float(name.rsplit("-",1)[1][:-1]),bnd_cpu_s=.001)
                elif name.startswith("cxl-"):
                    c.update(tax_heavy=float(name.split("-")[-1]),tax_light=1.02,mechanism="hand-off 10ms",strict_handoff=True)
                    if "hybrid" in name:c.update(bnd_on=True,bnd_latency_s=.001,bnd_cpu_s=.001)
                if c["mechanism"]=="hand-off 10ms":c["strict_handoff"]=True
                configs.append(c)
    for stage in args.q3:
        if stage=="A":
            for cores in (48,96,192):
                for target in (80,90):add(stage,cores,target,pre+combined+copies)
        elif stage=="SLO":
            for cores in (48,96):
                for target in (70,85,95):add(stage,cores,target,["pre-hist-1s",combined[0]]+copies)
        elif stage=="TAX":
            for cores in (48,96,192):
                for target in (80,90):add(stage,cores,target,[f"cxl-{policy}-{tau:g}" for policy in ("reactive","hybrid") for tau in (1.,1.02,1.05,1.10,1.23)])
        elif stage=="TAX-SLO":
            for cores in (48,96,192):
                for target in (70,85,95):add(stage,cores,target,[f"cxl-{policy}-{tau:g}" for policy in ("reactive","hybrid") for tau in ((1.,1.02,1.05,1.10,1.23) if target==70 else (1.,1.02,1.05))])
        elif stage=="MATCH":
            for cores in (48,96,192):
                for target in (80,90):add(stage,cores,target,[f"bnd-{pred}-{latency:g}s" for pred in ("oracle","class") for latency in (.1,.3,1.,3.) if latency!=.1 and not (pred=="oracle" and latency==1.)])
        elif stage=="HIST30":
            for target in (80,90):add(stage,96,target,["pre-hist30-1s"])
        elif stage=="GAP":
            for target in (80,90):add(stage,96,target,["none","pool","pre-oracle-1s","pre-oracle-3s","pre-hist-1s","pre-hist-3s","bnd-class-1ms","reactive"],4.)
    return configs


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
                                if c["mechanism"] == "hand-off 10ms":
                                    c["strict_handoff"] = True
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


def followup_grid(args, state):
    """Task 11. Each study keeps a paired density, time window, and seed across policies."""
    configs = []
    def add(study, workload, cores, target, names, **extra):
        solo = state["solo"][workload]
        gap = .25 if workload == "rl8" else 1.0
        demand = (solo["cpu_s"] / (solo["cpu_profile_s"] + solo["gap_s"]*gap + solo["traces"]*fleet.SANDBOX_GAP_S)
                  if workload == "rl8" else solo["mean_demand_cores"])
        for name in names:
            for seed in (1,2):
                c = fleet.DEFAULTS | dict(priority=10+"ABCD".index(study[0]), study=study, workload=workload,
                    nodes=16, cores=cores, target_util=target, per_node=max(1,round(target/100*cores/demand)),
                    seed=str(seed), stable_placement=True, floating=True, carry=True, nic_gbps=100,
                    gap_scale=gap, max_batch=8, ramp_s=600, duration_s=5400.0, timeout_s=1e6,
                    oracle_threshold=1.0, choice_width=64 if workload=="rl8" else 256) | variant(name)
                if workload == "rl8":
                    c.update(ramp_s=1800.0,duration_s=3600.0)
                if name.startswith("reactive-L") or "+reactive-L" in name:
                    latency = float(name.split("-L")[-1])
                    c.update(mechanism="hand-off 10ms",reactive_latency_s=latency,reactive_cpu_s=.002,
                             detect_ms=50,hold_s=max(1.0,2*latency))
                if c["mechanism"] == "hand-off 10ms":
                    c["strict_handoff"] = True
                c.update(extra)
                configs.append(c)
    ordinary = ["none","pool","reactive","bnd-oracle-1ms","bnd-oracle-100ms","bnd-oracle-1ms+reactive"]
    for study in args.followup:
        if study == "A":
            names = [f"reactive-L{x}" for x in (.001,.01,.1,.3,1.0,3.0)]
            names += [f"bnd-oracle-1ms+reactive-L{x}" for x in (.001,.1,1.0)] + ["migration"]
            for cores in (96,192):
                for target in (80,90):add("A","cap30",cores,target,names)
            add("A","rl8",48,90,names+["none","pool","bnd-oracle-1ms","bnd-oracle-10ms","bnd-oracle-100ms","bnd-oracle-1s"])
        elif study in ("B-pilot","B"):
            calibration = json.loads(args.density.read_text()) if study == "B" else {}
            for workload in ("cap30","rl8"):
                for target in (((80,100,110) if workload=="rl8" else (100,160,220)) if study=="B-pilot" else (70,80)):
                    for layout in (["spread"] if study=="B-pilot" else ["spread","pack"]):
                        solo=state["solo"][workload]
                        demand=(solo["cpu_s"]/(solo["cpu_profile_s"]+solo["gap_s"]*.25+solo["traces"]*fleet.SANDBOX_GAP_S)
                                if workload=="rl8" else solo["mean_demand_cores"])
                        per_node=(max(8,round(target/100*192/demand/8)*8) if study=="B-pilot"
                                  else calibration[f"{workload}-{target}"])
                        add(study,workload,192,target,["none"] if study=="B-pilot" else ordinary,
                            rl=True,batch=8,group=8,train_s=30.0,jitter_s=0.0,floating=False,placement=layout,
                            per_node=per_node,choice_width=64,ramp_s=1800.0,
                            duration_s=1800.0 if study=="B-pilot" else 3600.0 if workload=="rl8" else 5400.0,
                            group_mode="eight identical trace copies; no complete eligible real group")
        elif study in ("C","C-rl"):
            workload = "cap30" if study=="C" else "rl8"
            for target in ((70,80) if workload=="cap30" else (90,)):
                names=ordinary[:4]+["reactive-L1.0",ordinary[-1]]
                # No-event controls have the identical horizon and density as their event pairs.
                add(study,workload,192 if workload=="cap30" else 48,target,names,capacity_a=0.0)
                for amplitude in (.25,.5):
                    for mean_s in (1.0,10.0,60.0):
                        for duty in (.05,.2):
                            add(study,workload,192 if workload=="cap30" else 48,target,names,
                                capacity_a=amplitude,capacity_T_s=mean_s,capacity_duty=duty)
        elif study == "D":
            for target in (70,80,85,90,95):add("D","cap30",256,target,ordinary[:5])
    return configs


def calibrate_density(path):
    """Interpolate the pilot spread baseline; any extrapolation is recorded, never hidden."""
    rows = [json.loads(line) for line in path.open()]
    result, details = {}, []
    for workload in ("cap30","rl8"):
        points=[]
        for offered in ((80,100,110) if workload=="rl8" else (100,160,220)):
            rs=[r for r in rows if r["workload"]==workload and r["target_util"]==offered]
            if len(rs)!=2:raise ValueError("pilot requires both seeds at three densities")
            points.append((np.mean([r["run_cores_per_node"] for r in rs])/192,rs[0]["per_node"]))
        for target in (70,80):
            ordered=sorted(points,key=lambda p:p[1])  # Offered density stays ordered even when utilization saturates.
            lo,hi=next(((lo,hi) for lo,hi in zip(ordered,ordered[1:]) if lo[0]<=target/100<=hi[0]),
                       (ordered[0],ordered[1]) if target/100<ordered[0][0] else (ordered[-2],ordered[-1]))
            (u0,p0),(u1,p1)=lo,hi
            if u1-u0<.005 or p1<=p0:raise ValueError(f"pilot utilization is not increasing: {workload} {points}")
            per_node=max(8,round((p0+(target/100-u0)*(p1-p0)/(u1-u0))/8)*8)
            result[f"{workload}-{target}"]=per_node
            details.append(dict(workload=workload,target_util=target,per_node=per_node,pilot=points,
                                extrapolated=not u0<=target/100<=u1))
    path.with_name("density.json").write_text(json.dumps(result,indent=2))
    path.with_name("density-fit.json").write_text(json.dumps(details,indent=2))
    print(json.dumps(details,indent=2),flush=True)


def simulate(config):
    fleet.TIMEOUT_S = config["timeout_s"]
    workload = config["workload"]
    arrays = STATE["arrays"][workload].copy()
    if config.get("tax_heavy") is not None:
        arrays=taxed_arrays(arrays,config["tax_heavy"])
    actual = arrays.get("seg_reference_cpu_s",arrays["seg_cpu_s"]) >= config["oracle_threshold"]
    arrays["seg_heavy"] = arrays["class_heavy"] if config["predictor"] == "class" else actual
    arrays["seg_width"] = (arrays["class_width"] if config["predictor"] == "class" else arrays["peak_width"]
                            if config["width_mode"] == "peak" else arrays["seg_cpu_s"] / np.maximum(fleet.DT, arrays["seg_len"] * fleet.DT))
    if config.get("pre_on"):
        arrays["pre_heavy"]=actual if config["pre_predictor"]=="oracle" else arrays["hist_probability"]>=config["pre_threshold"]
        arrays["pre_width"]=arrays["seg_cpu_s"]/np.maximum(fleet.DT,arrays["seg_len"]*fleet.DT) if config["pre_predictor"]=="oracle" else arrays["class_width"]
    if config.get("copy_gbps"):
        arrays["seg_heavy"]=arrays["seg_heavy"] & arrays["copy_eligible"]
        copy_s=arrays["boundary_state_bytes"]/(config["copy_gbps"]*1e9/8)
        arrays["seg_bnd_latency_steps"]=(copy_s+.005)/fleet.DT
        arrays["seg_bnd_cpu_s"]=copy_s
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
    parser.add_argument("command", choices=["grid", "run", "bench", "calibrate", "test"])
    parser.add_argument("--q3",nargs="+",choices=["A","SLO","TAX","TAX-SLO","MATCH","HIST30","GAP"])
    parser.add_argument("--followup", nargs="+", choices=["A","B-pilot","B","C","C-rl","D"])
    parser.add_argument("--density", type=Path, default=HERE/".local/boundary11/density.json")
    parser.add_argument("--priorities", type=int, nargs="+", default=[1, 2, 3, 4])
    parser.add_argument("--ramp-s", type=float, default=600)
    parser.add_argument("--duration-s", type=float, default=5400)
    parser.add_argument("--timeout-s", type=float, default=60)
    parser.add_argument("--grid", type=Path, default=OUT / "grid.json")
    parser.add_argument("--output", type=Path, default=OUT / "results.jsonl")
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--index", type=int, default=0, help="bench config index")
    args = parser.parse_args()
    if args.command == "test":
        self_test()
        return
    if args.command == "calibrate":
        calibrate_density(args.output)
        return
    if not 1 <= args.jobs <= 24:
        parser.error("jobs must be 1..24")
    OUT.mkdir(parents=True, exist_ok=True)
    args.grid.parent.mkdir(parents=True,exist_ok=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    configs = json.loads(args.grid.read_text()) if args.command != "grid" else []
    if configs:
        workloads={c["workload"] for c in configs}
    elif args.q3:
        workloads={"cap30"}
    elif args.followup:
        workloads={"cap30","rl8"} if any(s in ("A","B-pilot","B","C-rl") for s in args.followup) else {"cap30"}
    else:
        workloads={w for p in args.priorities for w in (["cap30"] if p in (1,4) else ["cap16"] if p==2 else ["rl1","rl8"])}
    STATE = prepare(workloads,advanced=bool(args.q3 or any(str(c.get("study","")).startswith("12-") for c in configs)))
    metadata = {k: v for k, v in STATE.items() if k not in ("arrays", "names")}
    args.output.with_suffix(".inputs.json").write_text(json.dumps(metadata, indent=2))
    if args.command == "grid":
        configs = q3_grid(args, STATE) if args.q3 else followup_grid(args, STATE) if args.followup else make_grid(args, STATE)
        args.grid.write_text(json.dumps(configs, indent=1))
        print(f"{len(configs)} configs -> {args.grid}")
        return
    if args.command == "bench":
        config = configs[args.index]
        started = time.monotonic()
        print(json.dumps(simulate(config)), flush=True)
        print(f"wall including preparation excluded: {time.monotonic()-started:.2f} s", flush=True)
        return
    if not check_load(args.output.parent / "load.jsonl"):
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
    if not check_load(args.output.parent / "load.jsonl"):
        raise SystemExit(75)


if __name__ == "__main__":
    main()
