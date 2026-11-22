#!/usr/bin/env python3
"""Node imbalance under memory overcommit: which placement keeps both memory and CPU even (11-19).

  imbalance.py prep OUT.npz                 per-sandbox series from the 11-18 footprint runs (bud05)
  imbalance.py run IN.npz OUT.jsonl PROCS   the placement sweep (numpy only; run it off the measurement host)
  imbalance.py report OUT.jsonl             table

Question (user, 11-19): placing by CPU (least of k) should leave memory uneven, and placing by memory should leave CPU
uneven. The 11-18 sim (docs/results/capacity-balance-20261118) admitted by the full guest size, so memory that grows
after admission never showed up. Here nothing is reserved: a node takes a sandbox and its memory follows the measured
series, as under overcommit.

Inputs (prep): every sandbox that ran its whole trace in the 11-18 fill runs, as 1 s series of host memory (RssAnon of
its Firecracker process) and CPU (vCPU + VMM thread run time). RL comes as the real GRPO groups (8 rollouts of one
task, started together) of 1-rl and 3-rl-38; SWE as single sandboxes of 1-swe. Sandboxes stopped at a run's deadline
are left out, which drops the longest RL rollouts.

Model (run): N nodes, C CPUs and C x DRAM-per-CPU GiB each. Units (an RL group or one SWE sandbox) arrive as a Poisson
process at the rate that makes the mean memory in use rho x the cluster's. Each member is placed at arrival by the rule,
seeing the nodes as they are at that second (memory in use now, CPU over the last 10 s, live count, or the expected
memory of live sandboxes = per-workload mean). A new sandbox shows only its boot memory, so rules on memory in use
cannot see the growth to come. Nothing moves afterwards and slowdown does not feed back (open loop).
Measured after a 1 h warm-up, for 1 h:
- memory: node-seconds over the node's DRAM (would swap), excess GiB as a share of all memory, p99 of node memory /
  DRAM (the headroom each node would need), mean over time of the fullest node, CV across nodes, and the p99 and max
  over time of the summed excess as a share of all DRAM (the size of a shared pool that would absorb it).
- CPU: CV across nodes (1 s), demand beyond the node's CPUs as a share of all demand, CPU-weighted fair-share stretch
  (sum of demand x max(1, demand / CPUs) / sum of demand).
"""
import argparse
import collections
import json
from pathlib import Path
import sys

import numpy as np

FP = Path(__file__).resolve().parent
Q = FP / ".local/queue/20261118T2014"
SOURCES = {"rl": ("1-rl", "3-rl-38"), "swe": ("1-swe",)}


# ---------------------------------------------------------------- prep (bud05)
def load_int_csv(path):
    with open(path) as f:
        header = f.readline().strip().split(",")
    data = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.int64, ndmin=2)
    return {h: data[:, i] for i, h in enumerate(header)}


def prep(out):
    rss_all, cpu_all, offsets, wl_of, group_of, names = [], [], [0], [], [], []
    for wl, runs in SOURCES.items():
        for run in runs:
            boxes = [json.loads(l) for l in open(Q / run / "sandboxes.jsonl")]
            vm = load_int_csv(Q / run / "vm.csv")
            vc = load_int_csv(Q / run / "vcpu.csv")
            by_vm = collections.defaultdict(list)
            for i, v in enumerate(vm["vm"]):
                by_vm[int(v)].append(i)
            vc_by = collections.defaultdict(list)
            for i, v in enumerate(vc["vm"]):
                vc_by[int(v)].append(i)
            groups = collections.defaultdict(list)
            for b in boxes:
                groups[(run, b.get("group")) if wl == "rl" else (run, b["vm"])].append(b)
            kept = 0
            for key, members in groups.items():
                if not all(m.get("completed") and m.get("end_ns") for m in members):
                    continue
                if wl == "rl" and len(members) != 8:
                    continue
                gid = len(names)
                names.append(f"{run}:{key[1]}")
                for b in members:
                    t0, n = b["start_ns"], int(np.ceil((b["end_ns"] - b["start_ns"]) / 1e9))
                    if n < 2:
                        continue
                    idx = np.array(by_vm[b["vm"]], dtype=np.int64)
                    rss = np.zeros(n, np.float32)
                    cpu = np.zeros(n, np.float32)
                    if len(idx):
                        s = ((vm["t_ns"][idx] - t0) // 1_000_000_000).clip(0, n - 1)
                        rss[s] = vm["rss_anon_kib"][idx] / 1024
                        np.add.at(cpu, s, vm["vmm_run_ns"][idx] / 1e9)
                    j = np.array(vc_by[b["vm"]], dtype=np.int64)
                    if len(j):
                        s = ((vc["t_ns"][j] - t0) // 1_000_000_000)
                        ok = (s >= 0) & (s < n)
                        np.add.at(cpu, s[ok], vc["run_ns"][j][ok] / 1e9)
                    # forward-fill memory between samples (a VM shows up in vm.csv once it has a pid)
                    have = rss > 0
                    if have.any():
                        last = np.maximum.accumulate(np.where(have, np.arange(n), 0))
                        rss = rss[last]
                    rss_all.append(rss)
                    cpu_all.append(cpu)
                    offsets.append(offsets[-1] + n)
                    wl_of.append(wl)
                    group_of.append(gid)
                kept += 1
            print(f"{run}: {kept} units", flush=True)
    np.savez_compressed(out, rss=np.concatenate(rss_all), cpu=np.concatenate(cpu_all), offsets=np.array(offsets),
                        wl=np.array(wl_of), group=np.array(group_of), names=np.array(names))
    wl_a, g_a, off = np.array(wl_of), np.array(group_of), np.array(offsets)
    for wl in SOURCES:
        m = wl_a == wl
        lens = np.diff(off)[m]
        rs = [rss_all[i] for i in np.where(m)[0]]
        cs = [cpu_all[i] for i in np.where(m)[0]]
        print(f"{wl}: {m.sum()} sandboxes in {len(set(g_a[m]))} units, life p50 {np.median(lens)/60:.1f} min, "
              f"mean memory {np.mean([r.mean() for r in rs]):.0f} MiB, mean CPU {np.mean([c.mean() for c in cs]):.3f}, "
              f"memory-s per sandbox {np.mean([r.sum() for r in rs])/1024:.0f} GiB-s")


# ---------------------------------------------------------------- simulation
class Inputs:
    def __init__(self, path):
        z = np.load(path)
        self.rss, self.cpu, off = z["rss"], z["cpu"], z["offsets"]
        self.wl, self.group = z["wl"], z["group"]
        self.series = [(int(off[i]), int(off[i + 1])) for i in range(len(off) - 1)]
        self.units = collections.defaultdict(list)  # wl -> list of member index lists
        by_g = collections.defaultdict(list)
        for i, g in enumerate(self.group):
            by_g[int(g)].append(i)
        for g, members in by_g.items():
            self.units[str(self.wl[members[0]])].append(members)
        self.mean_rss = {w: float(np.mean([self.rss[a:b].mean() for i, (a, b) in enumerate(self.series)
                                           if self.wl[i] == w])) for w in self.units}
        self.mean_cpu = {w: float(np.mean([self.cpu[a:b].mean() for i, (a, b) in enumerate(self.series)
                                           if self.wl[i] == w])) for w in self.units}
        self.unit_mem_s = {w: float(np.mean([sum(self.rss[self.series[i][0]:self.series[i][1]].sum() for i in u)
                                             for u in us])) for w, us in self.units.items()}


MIXES = {"rl": {"rl": 1.0}, "swe": {"swe": 1.0}, "mix": {"rl": 0.5, "swe": 0.5}}  # share of memory
# name: (k, metric, filter); k = 0 means every node; filter = place only where expected memory <= 0.9 x DRAM
RULES = {
    "random": (1, None, False),
    "cpu-k2": (2, "cpu", False),
    "cpu-all": (0, "cpu", False),
    "mem-k2": (2, "mem", False),
    "mem-all": (0, "mem", False),
    "count-k2": (2, "count", False),
    "memexp-k2": (2, "memexp", False),
    "memexp-all": (0, "memexp", False),
    "memexp-filter-cpu-k2": (2, "cpu", True),
    "sum2d-k2": (2, "sum2d", False),
    # 11-20: present-state two-resource score, memory in use now + CPU of the last 10 s (no prediction)
    "memcpu-k2": (2, "memcpu", False),
    "memcpu-all": (0, "memcpu", False),
    # floor set (11-19, second question): no numeric prediction at all, and a clairvoyant bound
    "typecount-all": (0, "typecount", False),   # fewest live sandboxes of the same workload type (needs the label only)
    "oracle-mem-all": (0, "oracle-mem", False),  # knows every sandbox's whole future memory: lowest future peak
    "oracle-2d-all": (0, "oracle-2d", False),    # same, lowest future peak of max(memory share, CPU share)
}


def simulate(cfg, inp):
    rng = np.random.default_rng(cfg["seed"])
    N, C = cfg["nodes"], cfg["cpus"]
    M = C * cfg["dram_per_cpu"] * 1024.0  # MiB
    warm, meas = cfg["warm_s"], cfg["measure_s"]
    T = warm + meas
    lmax = max(b - a for a, b in inp.series) + 5
    mem = np.zeros((N, T + lmax), np.float32)
    cpu = np.zeros((N, T + lmax), np.float32)
    memexp = np.zeros((N, T + lmax), np.float32)
    count = np.zeros((N, T + lmax), np.float32)
    tcount = {w: np.zeros((N, T + lmax), np.float32) for w in MIXES[cfg["mix"]]}
    events = []
    for w, share in MIXES[cfg["mix"]].items():
        rate = cfg["rho"] * N * M * share / inp.unit_mem_s[w]
        n = rng.poisson(rate * T)
        for t in rng.uniform(0, T, n):
            events.append((t, w))
    events.sort()
    k, metric, filt = RULES[cfg["rule"]]
    spread = cfg.get("spread", False)
    for t, w in events:
        unit = inp.units[w][rng.integers(len(inp.units[w]))]
        used = set()
        for i in unit:
            ts = int(t + (rng.uniform(0, 2) if len(unit) > 1 else 0))
            a, b = inp.series[i]
            L = b - a
            cand = np.arange(N)
            if spread and len(used) < N:
                cand = np.array([x for x in cand if x not in used])
            if filt:
                ok = cand[memexp[cand, ts] + inp.mean_rss[w] <= 0.9 * M]
                cand = ok if len(ok) else cand[[int(np.argmin(memexp[cand, ts]))]]
            if k:
                cand = rng.choice(cand, min(k, len(cand)), replace=False)
            if metric is None:
                node = int(cand[0])
            else:
                if metric == "cpu":
                    v = cpu[cand, max(0, ts - 10):ts].mean(1) if ts > 0 else np.zeros(len(cand))
                elif metric == "mem":
                    v = mem[cand, ts]
                elif metric == "memexp":
                    v = memexp[cand, ts]
                elif metric == "count":
                    v = count[cand, ts]
                elif metric == "memcpu":
                    v = mem[cand, ts] / M + (cpu[cand, max(0, ts - 10):ts].mean(1) if ts > 0 else 0) / C
                elif metric == "typecount":
                    v = tcount[w][cand, ts]
                elif metric == "oracle-mem":
                    v = (mem[cand, ts:ts + L] + inp.rss[a:b]).max(1)
                elif metric == "oracle-2d":
                    v = np.maximum((mem[cand, ts:ts + L] + inp.rss[a:b]).max(1) / M,
                                   (cpu[cand, ts:ts + L] + inp.cpu[a:b]).max(1) / C)
                else:  # sum2d: expected memory share + recent CPU share
                    v = memexp[cand, ts] / M + (cpu[cand, max(0, ts - 10):ts].mean(1) if ts > 0 else 0) / C
                best = np.flatnonzero(v == v.min())
                node = int(cand[best[rng.integers(len(best))]])
            used.add(node)
            mem[node, ts:ts + L] += inp.rss[a:b]
            cpu[node, ts:ts + L] += inp.cpu[a:b]
            memexp[node, ts:ts + L] += inp.mean_rss[w]
            count[node, ts:ts + L] += 1
            tcount[w][node, ts:ts + L] += 1
    m, c = mem[:, warm:T] / M, cpu[:, warm:T]
    tot_mem = mem[:, warm:T].sum()
    over = np.maximum(0, mem[:, warm:T] - M)
    cpu_tot = c.sum()
    return dict(cfg, mem_mean=round(float(m.mean()), 3),
                mem_over_node_s=round(float((m > 1).mean()), 4),
                mem_excess_share=round(float(over.sum() / tot_mem), 5),
                pool_p99_share=round(float(np.percentile(over.sum(0), 99) / (N * M)), 5),
                pool_max_share=round(float(over.sum(0).max() / (N * M)), 5),
                mem_p99=round(float(np.percentile(m, 99)), 3),
                mem_fullest_mean=round(float(m.max(0).mean()), 3),
                mem_cv=round(float((m.std(0) / m.mean(0)).mean()), 4),
                cpu_util=round(float(c.mean() / C), 3),
                cpu_cv=round(float((c.std(0) / np.maximum(1e-9, c.mean(0))).mean()), 4),
                cpu_beyond=round(float(np.maximum(0, c - C).sum() / cpu_tot), 5),
                cpu_stretch=round(float((c * np.maximum(1, c / C)).sum() / cpu_tot), 4),
                cpu_fullest_mean=round(float((c.max(0) / C).mean()), 3),
                # 11-20: CPU overflow and how much of it had idle CPU on another node in the same second
                cpu_over_node_s=round(float((c > C).mean()), 4),
                cpu_movable=round(float(np.minimum(np.maximum(0, c - C).sum(0), np.maximum(0, C - c).sum(0)).sum()
                                        / max(1e-9, float(np.maximum(0, c - C).sum()))), 4),
                cpu_p99_node=round(float(np.percentile(c / C, 99)), 3),
                cpu_p99_cluster=round(float(np.percentile(c.sum(0) / (N * C), 99)), 3),
                per_node=round(float(count[:, warm:T].mean()), 1),
                arrivals=len(events))


def configs(quick=False):
    base = dict(nodes=16, cpus=96, warm_s=3600, measure_s=3600)
    for mix in MIXES:
        for dram in (2, 4):
            for rho in (0.85, 0.95):
                for rule in RULES:
                    for seed in ((1,) if quick else (1, 2)):
                        yield dict(base, mix=mix, dram_per_cpu=dram, rho=rho, rule=rule, seed=seed)
                for seed in ((1,) if quick else (1, 2)):  # siblings of a GRPO group on different nodes
                    yield dict(base, mix=mix, dram_per_cpu=dram, rho=rho, rule="memexp-filter-cpu-k2", spread=True,
                               seed=seed)


def configs_floor():
    """Second question (11-19): is the imbalance left after placement a property of the rule or of the workload?"""
    base = dict(nodes=16, cpus=96, warm_s=3600, measure_s=3600, set="floor")
    rules = ("random", "cpu-k2", "mem-k2", "count-k2", "memexp-k2", "typecount-all", "oracle-mem-all", "oracle-2d-all")
    for mix in MIXES:
        for dram in (2, 4):
            for rho in (0.85, 0.95):
                for rule in rules:
                    for seed in (1, 2):
                        yield dict(base, mix=mix, dram_per_cpu=dram, rho=rho, rule=rule, seed=seed)
    for mix in ("swe", "mix"):  # node size: fewer or more sandboxes per node
        for cpus in (48, 192):
            for rule in ("cpu-k2", "typecount-all", "oracle-mem-all", "oracle-2d-all"):
                for seed in (1, 2):
                    yield dict(base, cpus=cpus, mix=mix, dram_per_cpu=2, rho=0.95, rule=rule, seed=seed)


def floor(path):
    """Per-sandbox variability (pooled over sandbox-seconds: between sandboxes and over time) and the node CV it
    implies with n independent sandboxes per node: (sigma / mu) / sqrt(n)."""
    inp = Inputs(path)
    out = {}
    for w in inp.units:
        idx = [i for i in range(len(inp.series)) if inp.wl[i] == w]
        r = np.concatenate([inp.rss[a:b] for a, b in (inp.series[i] for i in idx)])
        c = np.concatenate([inp.cpu[a:b] for a, b in (inp.series[i] for i in idx)])
        within_r = np.mean([inp.rss[a:b].std() ** 2 for a, b in (inp.series[i] for i in idx)]) ** 0.5
        within_c = np.mean([inp.cpu[a:b].std() ** 2 for a, b in (inp.series[i] for i in idx)]) ** 0.5
        out[w] = dict(mem_mean_mib=round(float(r.mean()), 1), mem_cv=round(float(r.std() / r.mean()), 3),
                      mem_cv_within=round(float(within_r / r.mean()), 3),
                      cpu_mean=round(float(c.mean()), 4), cpu_cv=round(float(c.std() / c.mean()), 3),
                      cpu_cv_within=round(float(within_c / c.mean()), 3))
        print(w, out[w])
    return out


_inp = None


def _work(args):
    global _inp
    path, cfg = args
    if _inp is None:
        _inp = Inputs(path)
    return simulate(cfg, _inp)


def run(path, out, procs, which="place"):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    todo = list(configs_floor() if which == "floor" else configs())
    if any(c["mix"] == "rl" for c in todo) and "rl" not in Inputs(path).units:
        sys.exit("no RL units in the inputs")
    with ProcessPoolExecutor(procs) as ex, open(out, "w") as f:
        for fut in as_completed([ex.submit(_work, (path, c)) for c in todo]):
            f.write(json.dumps(fut.result()) + "\n")
            f.flush()


def report(path):
    rows = [json.loads(l) for l in open(path)]
    agg = collections.defaultdict(list)
    for r in rows:
        agg[(r["mix"], r["dram_per_cpu"], r["rho"], r["rule"] + ("+spread" if r.get("spread") else "")
             + (f"@{r['cpus']}cpu" if r.get("cpus", 96) != 96 else ""))].append(r)
    keys = ("mem_over_node_s", "mem_excess_share", "pool_p99_share", "mem_p99", "mem_fullest_mean", "mem_cv", "cpu_util", "cpu_cv",
            "cpu_beyond", "cpu_stretch", "cpu_fullest_mean")
    last = None
    for key in sorted(agg):
        if key[:3] != last:
            last = key[:3]
            print(f"\n=== {key[0]}, DRAM {key[1]} GiB/CPU, memory load {key[2]}")
            print(f"  {'rule':26s} " + " ".join(f"{k[:16]:>16s}" for k in keys))
        v = {k: np.mean([r[k] for r in agg[key]]) for k in keys}
        print(f"  {key[3]:26s} " + " ".join(f"{v[k]:16.4f}" for k in keys))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("out")
    p = sub.add_parser("run")
    p.add_argument("inputs")
    p.add_argument("out")
    p.add_argument("procs", type=int)
    p.add_argument("--set", default="place", choices=("place", "floor"))
    p = sub.add_parser("floor")
    p.add_argument("inputs")
    p = sub.add_parser("report")
    p.add_argument("results")
    a = ap.parse_args()
    if a.cmd == "prep":
        prep(a.out)
    elif a.cmd == "run":
        run(a.inputs, a.out, a.procs, a.set)
    elif a.cmd == "floor":
        floor(a.inputs)
    else:
        report(a.results)


if __name__ == "__main__":
    main()
