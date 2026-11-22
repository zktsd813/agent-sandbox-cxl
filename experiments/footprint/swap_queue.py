#!/usr/bin/env python3
"""Swap under steady arrivals (11-19): a fixed number of sandboxes on NUMA node 0, each finished sandbox replaced at
once, memory budget = measured demand / overcommit, swap on /dev/sda3 through the run's own cgroup.

  swap_queue.py [--only swe,rl] [--levels 1.5,2,3] [--reuse QUEUE_DIR]

The 11-19 overflow runs were one wave (GRPO groups wait for their slowest member, so demand fell after 13 minutes) and
the 16 GiB swap partition stopped them at 1.30x. Here the live count stays fixed for the whole run (slot replacement,
no groups) and the sandbox count is small enough that x3 still fits in the swap partition.

Per workload: a control run with room to spare and no swap, then one run per overcommit level with the same count,
CPUs and seed. The control also brings the images into the page cache; those pages stay charged to the control's
cgroup, so the later runs' budgets hold the sandboxes' own memory (anon, kernel, writable-disk cache), as in the
overflow runs. Demand = mean (anon + kernel) of the control after its stagger window; budget = demand / level.
CPUs 4-31 and 68-95 (node 0 without the controller's); node 1 and its CPUs are not used; memscan is off (idle-page
tracking clears accessed bits and would change what reclaim evicts).
"""
import argparse
import contextlib
import datetime
import fcntl
import json
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_queue as rq  # noqa: E402
import to_sim  # noqa: E402

rq.SHAPE["n0"] = "4-31,68-95"
GIB = 2 ** 30
# count chosen for ~18 GiB of demand (RL ~200 MiB, SWE ~450 MiB per sandbox in the 11-18 fills); seconds per run;
# new slots start within the stagger window (RL lives ~18 min, SWE ~2-4 min)
PLAN = {"swe": dict(n=40, seconds=720, stagger=120), "rl": dict(n=88, seconds=1500, stagger=600)}
SWAP_ROOM_GIB = 14.0  # sda3 is 16 GiB and holds some host pages already
# Other sessions on this host (Codex lab work) take lab.lock -x while a lab is up, quiet.lock -x while timing and
# quiet.lock -s for builds. On 11-19 14:02-16:00 their QEMU runs wrote disk images on sda during these runs and slowed
# swap-ins; each run now holds both locks exclusively (taken in that order, released between runs).
LOCKS = [HERE.parents[1] / ".codex-runs/lab.lock", HERE.parents[1] / ".codex-runs/quiet.lock"]


@contextlib.contextmanager
def held(paths):
    files = []
    try:
        for path in paths:
            f = open(path, "a")
            files.append(f)
            rq.log(f"waiting for {path.name}")
            fcntl.flock(f, fcntl.LOCK_EX)
        rq.log("locks held")
        rq.precheck(strict=False)
        yield
    finally:
        for f in reversed(files):
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


def demand_gib(run, after_s):
    node = to_sim.load_csv(run / "node.csv")
    t = (node["t_ns"] - node["t_ns"][0]) / 1e9
    m = t >= after_s
    d = (node["cg_anon"][m] + node["cg_kernel"][m]) / GIB
    return float(d.mean()), float(d.max())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="swe,rl")
    ap.add_argument("--levels", default="2,3,1.5")
    ap.add_argument("--reuse", type=Path)
    ap.add_argument("--seed", default="7")
    ap.add_argument("--no-locks", action="store_true", help="Do not take the other sessions' locks")
    args = ap.parse_args()
    locks = [] if args.no_locks else LOCKS
    out_root = args.reuse or rq.LOG_DIR / (datetime.datetime.now(rq.KST).strftime("%Y%m%dT%H%M") + "-swap")
    out_root.mkdir(parents=True, exist_ok=True)
    for wl in args.only.split(","):
        p = PLAN[wl]
        common = (f"{wl} --no-admission --scan 0 --seed {args.seed} --stagger-s {p['stagger']} "
                  f"--ramp {p['n']}:{p['seconds']}")
        minutes = p["seconds"] / 60 + 3
        ctrl = f"4-{wl}-ctrl"
        if not (out_root / ctrl / "done.json").exists():
            with held(locks):
                rq.run_item(ctrl, minutes, rq.fill_plan("n0", 60, 0, common), out_root, None)
        mean, peak = demand_gib(out_root / ctrl, p["stagger"] + 60)
        rq.log(f"{ctrl}: demand after the stagger window mean {mean:.1f} GiB, peak {peak:.1f} GiB")
        for level in [float(x) for x in args.levels.split(",")]:
            name = f"4-{wl}-x{level:g}"
            if (out_root / name / "done.json").exists():
                continue
            budget = round(mean / level, 1)
            if peak - budget > SWAP_ROOM_GIB:
                rq.log(f"{name}: peak demand {peak:.1f} GiB minus budget {budget} GiB exceeds the swap room; "
                       f"OOM kills are likely")
            try:
                with held(locks):
                    rq.run_item(name, minutes, rq.fill_plan("n0", budget, 16, common), out_root, ctrl)
            except Exception as exc:  # keep going
                rq.log(f"item {name} failed: {exc!r}")
    rq.log("swap queue finished")


if __name__ == "__main__":
    main()
