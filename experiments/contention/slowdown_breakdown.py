#!/usr/bin/env python3
"""Where the extra time of a command goes under contention (pilot run vs its solo run).

For each command in the measurement window whose solo duration is at least --min-solo-s:
  extra    = pilot duration - solo duration
  wait     = vCPU runqueue wait inside the command (vcpu.csv, 10 ms samples)
  cpu      = vCPU run time inside the command - solo vCPU run time (slower execution: frequency, cache, memory)
  other    = extra - wait - cpu (time the vCPU was neither running nor runnable: I/O, network, host-side delays)
Totals and the same split for the slowest 1% of commands (by slowdown) are printed and saved.
"""

import argparse
import bisect
import collections
import csv
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path)
    parser.add_argument("--solo", type=Path, required=True)
    parser.add_argument("--min-solo-s", type=float, default=0.5)
    args = parser.parse_args()
    config = json.loads((args.run / "run.json").read_text())
    samples = collections.defaultdict(list)  # vm -> [(t_ns, dt_ns, run_ns, wait_ns)]
    with (args.run / "vcpu.csv").open() as stream:
        for row in csv.DictReader(stream):
            samples[int(row["vm"])].append((int(row["t_ns"]), int(row["dt_ns"]), int(row["run_ns"]), int(row["wait_ns"])))
    first = min(s[0][0] for s in samples.values() if s)
    begin = first + int(config["ramp_s"] * 1e9)
    end = begin + int(config["duration_s"] * 1e9)
    if config.get("sync_steps"):
        records = [json.loads(line) for line in (args.run / "steps.jsonl").open()]
        if records:
            begin, end = records[0]["begin_ns"], records[-1]["end_ns"]
    solo_cache = {}

    def solo(trace):
        if trace not in solo_cache:
            path = args.solo / trace / "commands.csv"
            solo_cache[trace] = {int(r["index"]): (float(r["guest_duration_s"]), float(r["vcpu_run_s"] or 0))
                                 for r in csv.DictReader(path.open())} if path.exists() else {}
        return solo_cache[trace]

    rows = []
    for line in (args.run / "sandboxes.jsonl").open():
        record = json.loads(line)
        series = samples.get(record.get("vm"), [])
        times = [s[0] for s in series]
        reference = solo(record["trace"])
        for c in record.get("commands", []):
            if not c["host_end_ns"] or c["host_start_ns"] < begin or c["host_end_ns"] > end:
                continue
            base = reference.get(c["index"])
            if not base or base[0] < args.min_solo_s:
                continue
            # 10 ms samples whose interval ends inside the command
            lo = bisect.bisect_left(times, c["host_start_ns"])
            hi = bisect.bisect_right(times, c["host_end_ns"])
            run = sum(s[2] for s in series[lo:hi]) / 1e9
            wait = sum(s[3] for s in series[lo:hi]) / 1e9
            duration = (c["host_end_ns"] - c["host_start_ns"]) / 1e9
            rows.append({"duration": duration, "solo": base[0], "extra": duration - base[0], "wait": wait,
                         "cpu": run - base[1], "slowdown": duration / base[0]})

    def split(subset):
        extra = sum(r["extra"] for r in subset)
        wait = sum(r["wait"] for r in subset)
        cpu = sum(r["cpu"] for r in subset)
        return {"commands": len(subset), "extra_s": round(extra, 1), "wait_share": round(wait / extra, 3) if extra else None,
                "cpu_share": round(cpu / extra, 3) if extra else None,
                "other_share": round((extra - wait - cpu) / extra, 3) if extra else None,
                "slowdown_mean": round(sum(r["slowdown"] for r in subset) / len(subset), 3) if subset else None}

    rows.sort(key=lambda r: r["slowdown"])
    tail = rows[int(len(rows) * 0.99):]
    result = {"run": args.run.name, "min_solo_s": args.min_solo_s, "all": split(rows), "slowest_1pct": split(tail),
              "slowest_10pct": split(rows[int(len(rows) * 0.9):])}
    (args.run / "slowdown-breakdown.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
