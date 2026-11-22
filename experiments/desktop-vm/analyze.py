#!/usr/bin/env python3
"""Footprint of desktop agent VMs from a desktop_vm.py fill run (11-22).

  analyze.py RUN [--json OUT]

Per VM (QEMU process): anonymous RSS at ready / after setup / at the end, peak, and growth after placement (end - ready);
vCPU cores per second (from schedstat deltas), the share of seconds above 1 core, the widest second.
Node: live VMs, summed anonymous RSS, VM CPU use over the VM CPU set, memory per busy core (GiB per core) over the
steady window (all slots started, before the deadline).
Per action kind: vCPU core-seconds in the action window (start to start + 3 s or the next action), for the
"can we tell a heavy action from its string" check.
memscan (RUN/memscan/idle.csv, if present): share of present guest pages not accessed for 30 s or more.
"""
import argparse
import collections
import csv
import json
from pathlib import Path

import numpy as np


def load_jsonl(path):
    return [json.loads(line) for line in open(path) if line.strip()] if path.exists() else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--json")
    ap.add_argument("--npz", help="also write per-VM series for the placement simulator")
    ap.add_argument("--from-ready", action="store_true", help="sim series start when the VM is ready (no boot)")
    a = ap.parse_args()
    run = Path(a.run)
    meta = json.load(open(run / "run.json"))
    vms = {r["vm"]: r for r in load_jsonl(run / "vms.jsonl")}
    steps = load_jsonl(run / "steps.jsonl")
    per = collections.defaultdict(list)
    with open(run / "proc.csv") as f:
        for r in csv.DictReader(f):
            per[r["vm"]].append((int(r["t_ns"]), int(r["rss_anon_kib"]) / 1024, int(r["vcpu_run_ns"]),
                                 int(r["proc_run_ns"]), int(r["rss_file_kib"]) / 1024))
    host = list(csv.DictReader(open(run / "host.csv")))
    out = {"run": str(run), "slots": meta["slots"], "mem_mib": meta["mem_mib"], "vcpus": meta["vcpus"],
           "scale": meta.get("scale", 1.0)}

    # ---------------- per VM
    rows = []
    cores_all = []
    for vid, s in per.items():
        s.sort()
        t = np.array([x[0] for x in s]) / 1e9
        rss = np.array([x[1] for x in s])
        vc = np.array([x[2] for x in s]) / 1e9
        if len(t) < 5:
            continue
        cores = np.diff(vc) / np.maximum(1e-3, np.diff(t))
        cores_all.append(cores)
        rec = vms.get(vid, {})
        def at(ns):
            if not ns:
                return None
            i = np.searchsorted(t, ns / 1e9)
            return float(rss[min(i, len(rss) - 1)])
        ready_ns = rec.get("start_ns", 0) + int(rec.get("boot_s", 0) * 1e9) if rec.get("boot_s") else None
        rows.append({"vm": vid, "task": rec.get("task"), "life_s": float(t[-1] - t[0]),
                     "rss_ready": at(ready_ns), "rss_setup": at(rec.get("episode_start_ns")),
                     "rss_end": float(rss[-1]), "rss_peak": float(rss.max()), "rss_mean": float(rss.mean()),
                     "cores_mean": float(cores.mean()), "cores_p99": float(np.percentile(cores, 99)),
                     "cores_max": float(cores.max()), "over1_share": float((cores > 1).mean()),
                     "file_mib_end": float(s[-1][4]), "boot_s": rec.get("boot_s"), "setup_s": rec.get("setup_s"),
                     "setup_errors": len(rec.get("setup_errors", [])), "error": rec.get("error")})
    done = [r for r in rows if r["rss_ready"] is not None and not r["error"]]
    def q(vals, p):
        vals = [v for v in vals if v is not None]
        return round(float(np.percentile(vals, p)), 1) if vals else None
    out["vms"] = len(rows)
    out["vms_complete"] = len(done)
    for key in ("rss_ready", "rss_setup", "rss_end", "rss_peak", "rss_mean", "boot_s", "setup_s"):
        out[key] = {p: q([r[key] for r in done], p) for p in (10, 50, 90, 100)}
    growth = [r["rss_end"] - r["rss_ready"] for r in done]
    out["growth_after_ready_mib"] = {p: q(growth, p) for p in (10, 50, 90, 100)}
    out["growth_cv"] = round(float(np.std(growth) / max(1e-9, np.mean(growth))), 2) if growth else None
    allc = np.concatenate(cores_all) if cores_all else np.array([0.0])
    out["vm_cores"] = {"mean": round(float(allc.mean()), 3), "p99": round(float(np.percentile(allc, 99)), 2),
                       "max": round(float(allc.max()), 2), "over1_share": round(float((allc > 1).mean()), 4),
                       "over2_share": round(float((allc > 2).mean()), 4)}
    out["gib_per_core"] = round(float(np.mean([r["rss_mean"] for r in done]) / 1024 /
                                      max(1e-9, np.mean([r["cores_mean"] for r in done]))), 2) if done else None

    # ---------------- node, steady window
    t0 = meta["start_ns"] / 1e9 + meta["slots"] * meta.get("stagger_s", 20) + 120
    t1 = meta["start_ns"] / 1e9 + meta["minutes"] * 60
    bins = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for vid, s in per.items():
        for (ta, _, va, _, _), (tb, rb, vb, _, _) in zip(s, s[1:]):
            sec = int(tb / 1e9)
            bins[sec][0] += rb
            bins[sec][1] += (vb - va) / max(1, tb - ta)
            bins[sec][2] += 1
    win = [v for sec, v in bins.items() if t0 <= sec <= t1]
    ncpu = 28  # VM CPU set 4-31,68-95 = 28 cores (56 hardware threads)
    if win:
        rss_sum = np.array([v[0] for v in win]) / 1024
        cpu_sum = np.array([v[1] for v in win])
        live = np.array([v[2] for v in win])
        out["steady"] = {"seconds": len(win), "live_vms_mean": round(float(live.mean()), 1),
                         "rss_sum_gib_mean": round(float(rss_sum.mean()), 1), "rss_sum_gib_max": round(float(rss_sum.max()), 1),
                         "vm_cores_mean": round(float(cpu_sum.mean()), 2), "vm_cores_p99": round(float(np.percentile(cpu_sum, 99)), 2),
                         "cpu_util_of_28_cores": round(float(cpu_sum.mean() / ncpu), 3),
                         "gib_per_busy_core": round(float(rss_sum.mean() / max(1e-9, cpu_sum.mean())), 1)}
    hm = [h for h in host if t0 <= int(h["t_ns"]) / 1e9 <= t1]
    if hm:
        out["steady"]["node0_free_gib_min"] = round(min(int(h["mem_free_kib"]) for h in hm) / 2**20, 1)
        out["steady"]["node0_file_gib_mean"] = round(float(np.mean([int(h["file_pages_kib"]) for h in hm])) / 2**20, 1)

    # ---------------- per action kind
    by_vm_steps = collections.defaultdict(list)
    for st in steps:
        by_vm_steps[st["vm"]].append(st)
    kind = collections.defaultdict(list)
    for vid, sts in by_vm_steps.items():
        s = per.get(vid)
        if not s:
            continue
        t = np.array([x[0] for x in s])
        vc = np.array([x[2] for x in s])
        sts.sort(key=lambda x: x["start_ns"])
        for i, st in enumerate(sts):
            end = min(st["start_ns"] + 3_000_000_000, sts[i + 1]["start_ns"] if i + 1 < len(sts) else 1 << 62)
            a_i, b_i = np.searchsorted(t, st["start_ns"]), np.searchsorted(t, end)
            if b_i >= len(t) or a_i == 0:
                continue
            kind[st["kind"]].append((vc[b_i] - vc[a_i - 1]) / 1e9)
    out["action_core_s"] = {k: {"n": len(v), "mean": round(float(np.mean(v)), 2), "p90": round(float(np.percentile(v, 90)), 2)}
                            for k, v in sorted(kind.items(), key=lambda kv: -len(kv[1]))}
    lat = [((st.get("exec_ns", st["start_ns"]) - st["start_ns"]) / 1e6, (st["end_ns"] - st.get("exec_ns", st["start_ns"])) / 1e6)
           for st in steps if "error" not in st]
    if lat:
        out["action_exec_ms_p50"] = round(float(np.median([x[0] for x in lat])), 0)
        out["screenshot_ms_p50"] = round(float(np.median([x[1] for x in lat])), 0)
        out["late_s_p90"] = round(float(np.percentile([st["late_s"] for st in steps], 90)), 2)

    # ---------------- memscan
    idle = run / "memscan" / "idle.csv"
    if idle.exists():
        rs = list(csv.DictReader(open(idle)))
        rs = [r for r in rs if t0 * 1e9 <= int(r["t_ns"]) <= t1 * 1e9]
        if rs:
            present = sum(int(r["present"]) for r in rs)
            cold = sum(int(r["age_30_120"]) + int(r["age_ge120"]) for r in rs)
            out["memscan_cold_ge30s_share"] = round(cold / max(1, present), 3)

    print(json.dumps(out, indent=1))
    if a.json:
        json.dump(out | {"per_vm": rows}, open(a.json, "w"), indent=1)
    if a.npz:
        to_sim(per, vms, a.npz, a.from_ready)


def to_sim(per, vms, out, from_ready=False):
    """Per-VM 1 s series (QEMU anonymous RSS in MiB, whole-process CPU in cores) for ../footprint/imbalance_wide.py.
    Only VMs that finished their episode without error. Labelled "swe" so the "small" configs (mix=swe) use them.
    from_ready drops the boot (an RL environment is usually reset from a snapshot or warm pool, not booted)."""
    rss_all, cpu_all, offsets, names = [], [], [0], []
    for vid, s in sorted(per.items()):
        rec = vms.get(vid, {})
        if rec.get("error") or "episode_end_ns" not in rec or len(s) < 30:
            continue
        s = sorted(s)
        if from_ready:
            ready = rec["start_ns"] + int(rec["boot_s"] * 1e9)
            s = [x for x in s if x[0] >= ready]
            if len(s) < 30:
                continue
        t = np.array([x[0] for x in s]) / 1e9
        n = int(np.ceil(t[-1] - t[0])) + 1
        sec = np.clip((t - t[0]).astype(int), 0, n - 1)
        rss = np.zeros(n, np.float32)
        rss[sec] = [x[1] for x in s]
        have = rss > 0
        last = np.maximum.accumulate(np.where(have, np.arange(n), 0))
        rss = rss[last]
        proc = np.array([x[3] for x in s], dtype=np.float64)
        cpu = np.zeros(n, np.float32)
        np.add.at(cpu, sec[1:], np.diff(proc) / 1e9)
        rss_all.append(rss)
        cpu_all.append(cpu)
        offsets.append(offsets[-1] + n)
        names.append(vid)
    k = len(names)
    np.savez_compressed(out, rss=np.concatenate(rss_all), cpu=np.concatenate(cpu_all), offsets=np.array(offsets),
                        wl=np.array(["swe"] * k), group=np.arange(k), names=np.array(names))
    means = np.array([r.mean() for r in rss_all])
    print(f"sim inputs: {k} VMs -> {out}; time-mean memory {means.mean():.0f} MiB (CV {means.std() / means.mean():.2f}), "
          f"mean CPU {np.mean([c.mean() for c in cpu_all]):.3f} core, life p50 {np.median(np.diff(offsets)) / 60:.1f} min")


if __name__ == "__main__":
    main()
