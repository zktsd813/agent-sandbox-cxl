#!/usr/bin/env python3
"""Does the number of distinct traces change node contention at the same utilization? (RESULTS-fleet.md, 6절)

Draws random subsets of 15/30/60/120 traces (3 each) plus the full set, sizes each run for the target utilization from
the subset's own mean demand, and simulates 16 x 192-core nodes split and fully shared, seeds 1-2.
Usage: trace_count.py OUTPUT.json
"""
import json
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fleet

HERE = Path(__file__).resolve().parent
SOLO = {1: HERE / ".local/runs/20261021T070508Z-solo-7vxk272h", 4: HERE / ".local/runs/20261021T075141Z-solo-8exx1l39"}
TARGET = {1: 0.92, 4: 0.85}


def demand(programs, names):
    cpu = wall = 0.0
    for n in names:
        for k, v in programs[n]["program"]:
            if k == "cpu":
                cpu += float(v.sum()) * fleet.DT; wall += len(v) * fleet.DT
            else:
                wall += v
        wall += fleet.SANDBOX_GAP_S
    return cpu / wall


def main():
    jobs, meta = [], []
    for v in (1, 4):
        programs = fleet.load_programs(SOLO[v], HERE / "candidates-all.json")
        allnames = [t["name"] for t in json.loads((HERE / "candidates-all.json").read_text())["traces"] if t["name"] in programs]
        subsets = [("all", 0, allnames)]
        for size in (15, 30, 60, 120):
            for rep in range(3):
                subsets.append((size, rep, sorted(random.Random(f"sub-{v}-{size}-{rep}").sample(allnames, size))))
        for size, rep, names in subsets:
            per_node = round(TARGET[v] * 192 / demand(programs, names))
            arrays = fleet.flatten(programs, names)
            for pool in ("none", "all"):
                for seed in ("1", "2"):
                    cfg = fleet.DEFAULTS | dict(nodes=16, cores=192, per_node=per_node, floating=True, pool=pool, seed=seed,
                                                ramp_s=120, duration_s=600, carry=True, max_batch=8)
                    jobs.append({"arrays": arrays, "names": names, "config": cfg})
                    meta.append(dict(vcpus=v, size=len(names) if size == "all" else size, rep=rep, pool=pool, seed=seed, per_node=per_node))
    with ProcessPoolExecutor(12) as ex:
        res = list(ex.map(fleet.simulate, jobs))
    out = [m | {k: r[k] for k in ("lost_over_run", "run_cores_per_node")} for m, r in zip(meta, res)]
    Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
    print(len(out), "runs")


if __name__ == "__main__":
    main()
