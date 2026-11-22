#!/usr/bin/env python3
"""Placement sweep with a wide spread of sandbox sizes (11-19 evening, CPU added 11-20). Uses imbalance.py's model.

  imbalance_wide.py widen IN.npz TRACES.csv OUT.npz [--seed 1] [--cpu-cap V]   SWE series rescaled to the 264 traces
  imbalance_wide.py run WIDE.npz OUT.jsonl PROCS [--set wide|widecpu]
  imbalance_wide.py report OUT.jsonl

Why: the microVM inputs hold only 12 SWE traces (mean 374 MiB, similar sizes), and live-count placement looked as good
as per-type expected memory there. The 264 multilingual SWE traces replayed in Docker on 10-14 (old checkout
docs/results/numa-immediate-20261014/paired-traces.csv, cgroup peak incl. file cache) peak at 0.63 GiB median, 4.7 p90,
11.7 p99 and 18 GiB max (CV 1.4).

Widening: each SWE sandbox draws one of the 264 traces. Memory: the part of its series above its boot level (minimum
of its first 10 s) is scaled so the series reaches that trace's peak, so a big sandbox still looks small when it is
placed and grows afterwards. The draws are the same as the 11-19 run (rng.choice == index draw, checked).

CPU (--cpu-cap V, 11-20): bigger traces also use more CPU (Spearman 0.82 between peak memory and mean cores in the
Docker replay; cores grow about as peak^0.5). The microVM runs had 1 vCPU, so heavy traces were capped there. Each SWE
series is scaled so its mean over active seconds (> 0.05 core) equals CAL x the drawn trace's Docker mean cores, where
CAL = median (microVM active-second mean / Docker mean cores) over the six traces that 1 vCPU did not cap (Docker mean
cores <= 1). A second's demand is then held to V cores; the excess is carried to the next second, and what is left at
the end is dropped (reported). Without --cpu-cap, CPU stays as measured. RL is unchanged.

Rules: present-state only (user, 11-19: agent sandboxes are unpredictable, so no rule may predict load) - random, CPU
(last 10 s), memory in use, live count, memory now + recent CPU - and per-type expected memory as a reference line.
"""
import argparse
import collections
import csv
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imbalance  # noqa: E402

RULES = ("random", "cpu-k2", "cpu-all", "mem-k2", "mem-all", "count-k2", "memexp-k2")
RULES_CPU = RULES + ("memcpu-k2", "memcpu-all")
ACTIVE = 0.05  # core; a second counts as active above this


def _active_mean(s):
    a = s[s > ACTIVE]
    return float(a.mean()) if len(a) else 0.0


def _cap(s, cap):
    out, carry = np.empty_like(s), 0.0
    for t, x in enumerate(s):
        want = x + carry
        out[t] = min(want, cap)
        carry = want - out[t]
    return out, carry


def widen(src, traces_csv, out, seed, cpu_cap):
    z = dict(np.load(src))
    rows = [r for r in csv.DictReader(open(traces_csv)) if r["peak_memory_gib_node0"] not in ("", "nan")]
    peaks = np.array([float(r["peak_memory_gib_node0"]) for r in rows]) * 1024
    cores = np.array([float(r["mean_cpu_cores_node0"]) for r in rows])
    by_trace = {r["trace"]: i for i, r in enumerate(rows)}
    rng = np.random.default_rng(seed)
    rss, cpu, off, wl = z["rss"].astype(np.float32).copy(), z["cpu"].astype(np.float32).copy(), z["offsets"], z["wl"]
    swe = [i for i in range(len(off) - 1) if wl[i] == "swe"]

    if cpu_cap:
        # which trace each SWE sandbox replayed (prep keeps SWE units as '<run>:<vm>')
        vm2t = {}
        for run in imbalance.SOURCES["swe"]:
            for line in open(imbalance.Q / run / "sandboxes.jsonl"):
                b = json.loads(line)
                vm2t[(run, b["vm"])] = b["trace"]
        own = {}
        for i in swe:
            run, vm = str(z["names"][z["group"][i]]).split(":")
            own[i] = vm2t[(run, int(vm))]
        per = collections.defaultdict(list)
        for i in swe:
            per[own[i]].append(_active_mean(cpu[off[i]:off[i + 1]]))
        ratios = [np.median(v) / cores[by_trace[t]] for t, v in per.items() if cores[by_trace[t]] <= 1]
        cal = float(np.median(ratios))
        print(f"CAL {cal:.3f} from {len(ratios)} uncapped traces ({', '.join(f'{x:.2f}' for x in sorted(ratios))})")

    drawn_peak, drawn_cores, dropped, work0, work1 = [], [], 0.0, 0.0, 0.0
    for i in swe:
        a, b = off[i], off[i + 1]
        j = int(rng.integers(len(peaks)))
        s = rss[a:b]
        boot = float(s[: min(10, len(s))].min())
        top = float(s.max())
        target = max(float(peaks[j]), boot + 16)
        if top > boot:
            rss[a:b] = boot + (s - boot) * ((target - boot) / (top - boot))
        drawn_peak.append(target)
        if cpu_cap:
            c = cpu[a:b]
            am = _active_mean(c)
            work0 += float(c.sum())
            if am > 0:
                scaled, left = _cap(c * (cal * cores[j] / am), cpu_cap)
                cpu[a:b] = scaled
                dropped += left
            work1 += float(cpu[a:b].sum())
            drawn_cores.append(cores[j])
    z["rss"] = rss
    z["cpu"] = cpu
    np.savez_compressed(out, **z)
    series = [(off[i], off[i + 1]) for i in swe]
    means = np.array([rss[a:b].mean() for a, b in series])
    pk = np.array([rss[a:b].max() for a, b in series])
    print(f"SWE sandboxes {len(swe)}: peak MiB p50 {np.median(pk):.0f} p90 {np.percentile(pk, 90):.0f} "
          f"p99 {np.percentile(pk, 99):.0f} max {pk.max():.0f}; time-mean MiB mean {means.mean():.0f} "
          f"CV {means.std() / means.mean():.2f}")
    if cpu_cap:
        cm = np.array([cpu[a:b].mean() for a, b in series])
        cp = np.array([cpu[a:b].max() for a, b in series])
        print(f"SWE CPU: mean core per sandbox {cm.mean():.3f} (was {work0 / sum(b - a for a, b in series):.3f}); "
              f"per-sandbox mean p50 {np.median(cm):.3f} p90 {np.percentile(cm, 90):.3f} max {cm.max():.2f}; "
              f"1 s peak p50 {np.median(cp):.2f} p90 {np.percentile(cp, 90):.2f} max {cp.max():.2f}; "
              f"work x{work1 / work0:.2f}, dropped at the cap {dropped / (work1 + dropped):.4f}; "
              f"core per GiB (time-mean) {cm.sum() / (means.sum() / 1024):.3f}; "
              f"Spearman(mean memory, mean CPU) {_spearman(means, cm):.2f}")


def _spearman(x, y):
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1])


def vmonly(src, out, min_gib):
    """Keep SWE sandboxes whose peak is at least min_gib: a stand-in for VM-class work (11-22, small servers)."""
    z = dict(np.load(src))
    off, wl = z["offsets"], z["wl"]
    keep = [i for i in range(len(off) - 1) if wl[i] == "swe" and z["rss"][off[i]:off[i + 1]].max() >= min_gib * 1024]
    rss = [z["rss"][off[i]:off[i + 1]] for i in keep]
    cpu = [z["cpu"][off[i]:off[i + 1]] for i in keep]
    new_off = np.concatenate([[0], np.cumsum([len(s) for s in rss])])
    np.savez_compressed(out, rss=np.concatenate(rss), cpu=np.concatenate(cpu), offsets=new_off, wl=wl[keep],
                        group=z["group"][keep], names=z["names"])
    means = np.array([s.mean() for s in rss])
    print(f"kept {len(keep)} of {int((wl == 'swe').sum())} SWE sandboxes (peak >= {min_gib} GiB): time-mean memory "
          f"{means.mean():.0f} MiB (CV {means.std() / means.mean():.2f}), mean CPU {np.mean([c.mean() for c in cpu]):.3f} "
          f"core, GiB per core {means.sum() / 1024 / sum(c.mean() for c in cpu):.2f}")


def configs(which="wide"):
    if which in ("small", "smalldesk"):  # 11-22: VM-class work on smaller servers (a smaller server = the VM share)
        base = dict(nodes=16, warm_s=3600, measure_s=3600, set=which, mix="swe")
        for cpus in (32, 48, 96):
            for dram in ((2, 4) if which == "small" else (2, 4, 8)):  # desktop VMs are CPU-light: add 8 GiB/core
                for rho in (0.85, 0.95):
                    for rule in RULES_CPU:
                        for seed in (1, 2):
                            yield dict(base, cpus=cpus, dram_per_cpu=dram, rho=rho, rule=rule, seed=seed)
        return
    base = dict(nodes=16, cpus=96, warm_s=3600, measure_s=3600, set=which)
    for mix in ("swe", "mix"):
        for dram in (2, 4):
            for rho in (0.85, 0.95):
                for rule in (RULES_CPU if which == "widecpu" else RULES):
                    for seed in (1, 2):
                        yield dict(base, mix=mix, dram_per_cpu=dram, rho=rho, rule=rule, seed=seed)


def run(path, out, procs, which):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    with ProcessPoolExecutor(procs) as ex, open(out, "w") as f:
        for fut in as_completed([ex.submit(imbalance._work, (path, c)) for c in configs(which)]):
            f.write(json.dumps(fut.result()) + "\n")
            f.flush()


KEYS = ("mem_over_node_s", "mem_p99", "pool_p99_share", "cpu_util", "cpu_over_node_s", "cpu_beyond", "cpu_movable",
        "cpu_stretch", "cpu_p99_node", "cpu_p99_cluster")


def report(path):
    rows = [json.loads(line) for line in open(path)]
    if "cpu_movable" not in rows[0]:
        return imbalance.report(path)
    agg = collections.defaultdict(list)
    for r in rows:
        agg[(r["mix"] + (f"@{r['cpus']}cpu" if r.get("cpus", 96) != 96 or r.get("set") in ("small", "smalldesk") else ""),
             r["dram_per_cpu"], r["rho"], r["rule"])].append(r)
    last = None
    for key in sorted(agg):
        if key[:3] != last:
            last = key[:3]
            print(f"\n=== {key[0]}, DRAM {key[1]} GiB/CPU, memory load {key[2]}, VMs per node "
                  f"{np.mean([r['per_node'] for r in agg[key]]):.1f}")
            print(f"  {'rule':12s} " + " ".join(f"{k[:15]:>15s}" for k in KEYS))
        v = {k: np.mean([r[k] for r in agg[key]]) for k in KEYS}
        print(f"  {key[3]:12s} " + " ".join(f"{v[k]:15.4f}" for k in KEYS))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("widen")
    p.add_argument("inputs")
    p.add_argument("traces_csv")
    p.add_argument("out")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--cpu-cap", type=float, default=0, help="vCPU cap per sandbox; 0 keeps CPU as measured")
    p = sub.add_parser("run")
    p.add_argument("inputs")
    p.add_argument("out")
    p.add_argument("procs", type=int)
    p.add_argument("--set", default="wide", choices=("wide", "widecpu", "small", "smalldesk"))
    p = sub.add_parser("vmonly")
    p.add_argument("inputs")
    p.add_argument("out")
    p.add_argument("--min-gib", type=float, default=1.0)
    p = sub.add_parser("report")
    p.add_argument("results")
    a = ap.parse_args()
    if a.cmd == "vmonly":
        vmonly(a.inputs, a.out, a.min_gib)
    elif a.cmd == "widen":
        widen(a.inputs, a.traces_csv, a.out, a.seed, a.cpu_cap)
    elif a.cmd == "run":
        run(a.inputs, a.out, a.procs, a.set)
    else:
        report(a.results)


if __name__ == "__main__":
    main()
