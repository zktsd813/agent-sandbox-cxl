#!/usr/bin/env python3
"""Fit the kernel scheduling wait that the fluid model leaves out (RESULTS-fleet.md 7절).

fleet.py adds wait/run = a(P) * exp(b * util) on top of the fluid share for a pool of P cores. This script fits a(P)
for P = 5, 10, 20 and one b to the 1 vCPU pilot runs without moves (validation ladder V1 and V2): for each b on a grid,
a(P) is bisected so that the model's log error over the runs of that pool size averages zero, and the b with the
smallest RMS log error wins. The slope of log a over log P is the "slope" extrapolation to larger pools.
With AUDIT.json from pilot_audit.py, only runs whose diverged vCPU share is under 5% are used, and each run's CPU
demand is scaled so the model's utilization equals the measured one (the fit then sees wait at matched utilization).
Usage: kernel_wait_fit.py OUTPUT.json [capacity|latency] [AUDIT.json]  (run on socket 1)
"""

from concurrent.futures import ProcessPoolExecutor
import json
import math
from pathlib import Path
import sys

import fleet
import validate

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REPORTS = ("validate-overnight-carry.json", "validate-v-carry.json")
SOLO = {1: HERE / ".local/runs/20261021T070508Z-solo-7vxk272h", 4: HERE / ".local/runs/20261021T075141Z-solo-8exx1l39"}
B_GRID = (0.0, 2.0, 4.0, 6.0, 8.0, 9.0, 10.0, 11.0, 12.0, 14.0)
STEPS = 20
MODE = sys.argv[2] if len(sys.argv) > 2 else "capacity"
AUDIT = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 else None


def runs(vcpus):
    """(run dir, pool size, fleet config, measured wait/run) for pilot runs without moves, RL or LLM-wait scaling."""
    out = []
    for name in REPORTS:
        for entry in json.loads((HERE / ".local/report" / name).read_text()):
            if "checks" not in entry:
                continue
            run = Path(entry["run"])
            run = run if run.is_absolute() else ROOT / run
            config, sim = validate.fleet_config(run, carry=True)
            if config["vcpus"] != vcpus or sim["mechanism"] != "none" or sim["rl"] or sim["gap_scale"] != 1.0:
                continue
            pool = {"none": sim["cores"], "socket": sim["cores"] * sim["nodes"] // 2,
                    "all": sim["cores"] * sim["nodes"]}[sim["pool"]]
            measured = json.loads((run / "contention-1ms.json").read_text())["total_wait_over_run"]
            if AUDIT is not None:
                audit = AUDIT.get(str(run))
                if not audit or audit["diverged_cpu"] >= 0.05:
                    continue
                sim = sim | {"cpu_inflation": audit["demand_factor"]}
            candidates = Path(config.get("candidates", HERE / "candidates.json"))
            out.append((run, pool, sim, measured, candidates if candidates.is_absolute() else ROOT / candidates))
    return out


_CACHE = {}


def _simulate(job):
    vcpus, candidates, config = job
    key = (vcpus, str(candidates))
    if key not in _CACHE:
        programs = fleet.load_programs(SOLO[vcpus], candidates)
        names = [t["name"] for t in json.loads(candidates.read_text())["traces"] if t["name"] in programs]
        _CACHE[key] = (fleet.flatten(programs, names), names)
    arrays, names = _CACHE[key]
    return fleet.simulate({"arrays": arrays, "names": names, "config": config})["wait_over_run"]


def model(pool, items, a, b):
    jobs = [(1, cand, sim | {"kernel_wait": "fit", "kernel_wait_a": {str(k): v for k, v in a.items()},
                             "kernel_wait_b": b, "kernel_wait_mode": MODE}) for _, _, sim, _, cand in items]
    return list(pool.map(_simulate, jobs))


def main():
    items = runs(1)
    sizes = sorted({p for _, p, _, _, _ in items})
    print(f"{len(items)} runs, pool sizes {sizes}", flush=True)
    fits = []
    with ProcessPoolExecutor(12) as pool:
        for b in B_GRID:
            lo = {p: math.log(1e-10) for p in sizes}
            hi = {p: math.log(2.0) for p in sizes}
            for _ in range(STEPS):
                a = {p: math.exp(0.5 * (lo[p] + hi[p])) for p in sizes}
                got = model(pool, items, a, b)
                for p in sizes:
                    err = [math.log(max(g, 1e-5) / m) for g, (_, q, _, m, _) in zip(got, items) if q == p]
                    if sum(err) > 0:
                        hi[p] = math.log(a[p])
                    else:
                        lo[p] = math.log(a[p])
            a = {p: math.exp(0.5 * (lo[p] + hi[p])) for p in sizes}
            got = model(pool, items, a, b)
            rms = math.sqrt(sum(math.log(max(g, 1e-5) / m) ** 2 for g, (_, _, _, m, _) in zip(got, items)) / len(items))
            xs, ys = [math.log(p) for p in sizes], [math.log(a[p]) for p in sizes]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            fits.append({"b": b, "a": a, "slope": slope, "rms_log_error": rms,
                         "runs": [{"run": str(r), "pool": q, "measured": m, "model": g}
                                  for g, (r, q, _, m, _) in zip(got, items)]})
            print(f"b {b:.1f}: a {', '.join(f'{p}: {a[p]:.4f}' for p in sizes)}  slope {slope:+.2f}  rms log error {rms:.3f}",
                  flush=True)
    best = min(fits, key=lambda f: f["rms_log_error"])
    Path(sys.argv[1]).write_text(json.dumps({"best": {k: best[k] for k in ("b", "a", "slope", "rms_log_error")},
                                             "fits": fits}, indent=1))
    print(f"best b {best['b']} -> {sys.argv[1]}")


if __name__ == "__main__":
    main()
