#!/usr/bin/env python3
"""Audit pilot runs against the solo runs the simulator uses (RESULTS-fleet.md 7절).

For each pilot run without moves:
- divergence: per sandbox, the first command whose return code differs from the SOLO run of the same trace (not the
  recorded trace), whether it hit the 60 s timeout, and the share of the run's vCPU time spent from there on;
- same-path CPU: vCPU time of the commands before that point, over the solo run's vCPU time for the same commands;
- demand factor: the cpu_inflation that makes fleet.py's utilization equal the measured one (bisection), and the
  model's wait at that factor.
Usage: pilot_audit.py OUTPUT.json  (run on socket 1)
"""

import bisect
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
import json
from pathlib import Path
import sys

import fleet
import validate

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REPORTS = ("validate-overnight-carry.json", "validate-v-carry.json")
SOLO = {1: HERE / ".local/runs/20261021T070508Z-solo-7vxk272h", 4: HERE / ".local/runs/20261021T075141Z-solo-8exx1l39",
        "old": HERE / ".local/runs/20261020T092617Z-solo-8hjb2n4z"}
_CACHE = {}


def solo_key(config):
    """Short-trace pilots (V1) used the old 1 vCPU solo run; the rest use the solo run of their vCPU count."""
    return config.get("vcpus", 1) if "candidates-" in str(config.get("candidates", "")) else "old"


def divergence(run, config):
    key = solo_key(config)
    samples = defaultdict(lambda: ([], []))
    with (run / "vcpu.csv").open() as stream:
        for row in csv.DictReader(stream):
            t, r = samples[int(row["vm"])]
            t.append(int(row["t_ns"]))
            r.append(int(row["run_ns"]))
    total = after = same = same_solo = 0.0
    sandboxes = 0
    causes = Counter()
    for line in (run / "sandboxes.jsonl").read_text().splitlines():
        box = json.loads(line)
        path = SOLO[key] / box["trace"] / "commands.csv"
        if box.get("vm") not in samples or not path.exists():
            continue
        solo = list(csv.DictReader(path.open()))
        t, r = samples[box["vm"]]
        cumulative = [0]
        for x in r:
            cumulative.append(cumulative[-1] + x)
        upto = lambda ns: cumulative[bisect.bisect_right(t, ns)]
        total += cumulative[-1] / 1e9
        sandboxes += 1
        for i, command in enumerate(c for c in box.get("commands", []) if c.get("host_end_ns")):
            if i >= len(solo):
                break
            if str(command.get("rc")) != solo[i]["returncode"]:
                seconds = (command["host_end_ns"] - command["host_start_ns"]) / 1e9
                causes["timeout in pilot" if seconds > 59 else
                       "timeout in solo" if solo[i]["timed_out"] == "True" else "other"] += 1
                after += (cumulative[-1] - upto(command["host_start_ns"])) / 1e9
                break
            if solo[i].get("vcpu_run_s"):
                same += (upto(command["host_end_ns"]) - upto(command["host_start_ns"])) / 1e9
                same_solo += float(solo[i]["vcpu_run_s"])
    return {"sandboxes": sandboxes, "diverged": sum(causes.values()), "causes": dict(causes),
            "diverged_cpu": after / total if total else None, "same_path_cpu": same / same_solo if same_solo else None}


def matched(job):
    """Bisect cpu_inflation until fleet.py's utilization equals the measured one."""
    vcpus, candidates, sim, utilization = job
    key = (vcpus, str(candidates))
    if key not in _CACHE:
        programs = fleet.load_programs(SOLO[vcpus], candidates)
        names = [t["name"] for t in json.loads(candidates.read_text())["traces"] if t["name"] in programs]
        _CACHE[key] = (fleet.flatten(programs, names), names)
    arrays, names = _CACHE[key]
    lo, hi = 0.8, 1.6
    for _ in range(9):
        mid = 0.5 * (lo + hi)
        out = fleet.simulate({"arrays": arrays, "names": names, "config": sim | {"cpu_inflation": mid}})
        lo, hi = (mid, hi) if out["run_cores_per_node"] / sim["cores"] < utilization else (lo, mid)
    factor = 0.5 * (lo + hi)
    out = fleet.simulate({"arrays": arrays, "names": names, "config": sim | {"cpu_inflation": factor}})
    return factor, out["run_cores_per_node"] / sim["cores"], out["wait_over_run"]


def main():
    runs = []
    for name in REPORTS:
        for entry in json.loads((HERE / ".local/report" / name).read_text()):
            if "checks" in entry:
                run = Path(entry["run"])
                runs.append(run if run.is_absolute() else ROOT / run)
    audit, jobs = {}, []
    for run in sorted(set(runs)):
        config, sim = validate.fleet_config(run, carry=True)
        if sim["mechanism"] != "none" or sim["gap_scale"] != 1.0:
            continue
        measured = json.loads((run / "contention-1ms.json").read_text())
        candidates = Path(config.get("candidates", HERE / "candidates.json"))
        candidates = candidates if candidates.is_absolute() else ROOT / candidates
        pool = {"none": sim["cores"], "socket": sim["cores"] * sim["nodes"] // 2,
                "all": sim["cores"] * sim["nodes"]}[sim["pool"]]
        utilization = sum(measured["mean_run_cores_per_unit"].values()) / sum(measured["cores_per_unit"].values())
        audit[str(run)] = {"vcpus": config["vcpus"], "pool_cores": pool, "per_node": sim["per_node"], "seed": sim["seed"],
                           "rl": sim["rl"], "utilization": utilization, "wait_over_run": measured["total_wait_over_run"],
                           **divergence(run, config)}
        jobs.append((str(run), (config["vcpus"], candidates, sim, utilization)))
    with ProcessPoolExecutor(12) as pool:
        for (run, _), (factor, model_util, model_wait) in zip(jobs, pool.map(matched, [j for _, j in jobs])):
            audit[run].update({"demand_factor": factor, "model_utilization": model_util, "model_wait_over_run": model_wait})
    Path(sys.argv[1]).write_text(json.dumps(audit, indent=1))
    for run, a in sorted(audit.items(), key=lambda x: (x[1]["vcpus"], x[1]["pool_cores"], x[1]["utilization"])):
        print(f"v{a['vcpus']} P{a['pool_cores']:>2} k{a['per_node']:>2} s{a['seed']} util {a['utilization']*100:5.1f} "
              f"diverged {a['diverged_cpu']*100:5.1f}% same-path cpu {a['same_path_cpu'] or 0:.2f} demand x{a['demand_factor']:.2f} "
              f"wait {a['wait_over_run']*100:5.2f} model {a['model_wait_over_run']*100:5.2f}")


if __name__ == "__main__":
    main()
