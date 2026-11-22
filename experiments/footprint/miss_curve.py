#!/usr/bin/env python3
"""Swap/zswap cost curve from memscan access logs: evict every page idle for A seconds or more (a global LRU by idle
age), then count the memory freed and the pages that come back.

  miss_curve.py RUN [RUN ...] [--thresholds 10 20 30 60 120 300 600] [--json OUT]

Per scanned sandbox, from the access log (present, accessed per ~5 s round):
- age of a page = time since its last access (or since it first appeared); a page that leaves (free page reporting)
  starts over when it comes back.
- freed share at A = page-seconds spent at age >= A / page-seconds present (swap frees all of it; zswap keeps the
  compressed copy, so it frees share x (1 - lz4 ratio)).
- faults at A = re-accesses of pages whose age had reached A (each is one swap-in), as MiB per sandbox-second and per
  command. A round counts toward a command when the command overlaps it; re-accesses outside commands (LLM waits) are
  counted apart. The age is known to the round (about 5 s), so ages near A are uncertain by one round.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

FP = Path(__file__).resolve().parent
sys.path.insert(0, str(FP))
import memscan  # noqa: E402
import to_sim  # noqa: E402

PAGE_MIB = 4096 / 2 ** 20
AGE_BINS = np.array([0, 5, 10, 20, 30, 60, 120, 300, 600, 1e9])


def one_vm(path, cmds, thresholds):
    recs = list(memscan.read_access_log(path))
    if len(recs) < 3:
        return None
    n = len(recs[0][1])
    last = np.full(n, -1.0)
    held = np.zeros(len(thresholds))      # page-seconds at age >= A
    total = 0.0                           # page-seconds present
    faults_cmd = np.zeros((len(cmds), len(thresholds)))
    faults_wait = np.zeros(len(thresholds))
    t_prev = recs[0][0] / 1e9
    pres0 = recs[0][1]
    last[pres0] = t_prev
    span = 0.0
    starts = np.array([c[0] for c in cmds]) if cmds else np.zeros(0)
    ends = np.array([c[1] for c in cmds]) if cmds else np.zeros(0)
    for t_ns, present, accessed, _ in recs[1:]:
        t = t_ns / 1e9
        dt = t - t_prev
        if dt <= 0:
            continue
        span += dt
        known = last >= 0
        age = np.where(known, t - last, 0.0)
        # memory held during (t_prev, t]: pages present and not accessed this round, at their age
        idle = present & ~accessed & known
        total += present.sum() * dt
        for j, A in enumerate(thresholds):
            held[j] += np.count_nonzero(idle & (age >= A)) * dt
        # re-accesses this round
        re = accessed & known
        if re.any():
            ages = age[re]
            ov = np.where((starts < t) & (ends > t_prev))[0]
            for j, A in enumerate(thresholds):
                k = np.count_nonzero(ages >= A)
                if not k:
                    continue
                if len(ov):
                    faults_cmd[ov[-1], j] += k
                else:
                    faults_wait[j] += k
        last[accessed] = t
        new = present & ~known
        last[new] = t
        last[~present] = -1.0
        t_prev = t
    return dict(span=span, total=total, held=held, faults_cmd=faults_cmd, faults_wait=faults_wait,
                cmd_s=np.array([c[1] - c[0] for c in cmds]), base=np.array([c[2] for c in cmds]))


def run_curve(run, thresholds):
    run, meta, steps, boxes = to_sim.load_run(run)
    out = []
    for b in boxes:
        path = run / "memscan/access" / f"{b['vm']}.bin"
        if not path.exists():
            continue
        cmds = [(c["host_start_ns"] / 1e9, c["host_end_ns"] / 1e9, 0.0) for c in b.get("commands", [])
                if c.get("host_end_ns")]
        r = one_vm(path, cmds, thresholds)
        if r:
            out.append(r)
    return meta, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--thresholds", type=float, nargs="+", default=[10, 20, 30, 60, 120, 300, 600])
    ap.add_argument("--json")
    a = ap.parse_args()
    th = a.thresholds
    res = {}
    for run in a.runs:
        meta, vms = run_curve(Path(run), th)
        if not vms:
            continue
        span = sum(v["span"] for v in vms)
        total = sum(v["total"] for v in vms)
        held = sum(v["held"] for v in vms)
        fc = np.concatenate([v["faults_cmd"] for v in vms if len(v["faults_cmd"])])
        fw = sum(v["faults_wait"] for v in vms)
        cmd_s = np.concatenate([v["cmd_s"] for v in vms if len(v["cmd_s"])])
        rows = []
        print(f"=== {run}: {meta['workload']}, {len(vms)} scanned sandboxes, {span/3600:.1f} sandbox-hours, "
              f"{len(cmd_s)} commands, mean present {total/span*PAGE_MIB:.0f} MiB")
        print("   A s  freed  swap-x  faults MiB/s/sbx  in cmds  per cmd MiB p50  p90   p99   cmds>=10MiB  >=50MiB")
        for j, A in enumerate(th):
            share = held[j] / total
            per_cmd = fc[:, j] * PAGE_MIB
            f_total = (fc[:, j].sum() + fw[j]) * PAGE_MIB
            row = dict(A_s=A, freed_share=round(float(share), 3), swap_capacity_x=round(1 / (1 - share), 2),
                       fault_mib_per_sandbox_s=round(float(f_total / span), 4),
                       fault_share_in_commands=round(float(fc[:, j].sum() * PAGE_MIB / f_total), 3) if f_total else None,
                       per_cmd_mib_p50=round(float(np.percentile(per_cmd, 50)), 2),
                       per_cmd_mib_p90=round(float(np.percentile(per_cmd, 90)), 2),
                       per_cmd_mib_p99=round(float(np.percentile(per_cmd, 99)), 2),
                       cmds_ge10mib=round(float(np.mean(per_cmd >= 10)), 3),
                       cmds_ge50mib=round(float(np.mean(per_cmd >= 50)), 3))
            rows.append(row)
            print(f"  {A:4.0f} {share:6.3f} {row['swap_capacity_x']:6.2f} {row['fault_mib_per_sandbox_s']:14.4f} "
                  f"{row['fault_share_in_commands'] or 0:8.2f} {row['per_cmd_mib_p50']:14.2f} {row['per_cmd_mib_p90']:5.2f} "
                  f"{row['per_cmd_mib_p99']:6.2f} {row['cmds_ge10mib']:10.3f} {row['cmds_ge50mib']:8.3f}")
        res[str(run)] = dict(workload=meta["workload"], sandboxes=len(vms), sandbox_hours=round(span / 3600, 2),
                             commands=int(len(cmd_s)), present_mib=round(total / span * PAGE_MIB, 1), curve=rows)
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1) + "\n")


if __name__ == "__main__":
    main()
