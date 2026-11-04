#!/usr/bin/env python3
"""Measured counterpart of cap_sweep.py: concurrent Docker replays of the 264 multilingual traces on one 16-core pool.

Containers run the recorded commands with the recorded LLM gaps (`experiment.py --schedule gaps`), K at a time: when
one finishes, the next queued trace starts (`--setup on-arrival --starts together --max-active K`). Policies are no
cap, `--cpus 4` and `--cpus 1` (CFS quota, as in Docker-based RL harnesses). Unlike the simulator, every source of
contention is present: runqueue, quota throttling, LLC, memory bandwidth, frequency, I/O and the Docker daemon.
Each policy runs at low load (K=4, the reference for its commands) and high load (K=32, every trace four times; the
selected traces average 0.38 cores, so about 76% of the 16 cores before stretching).

  python3 real_cap.py select     # 24 wide and 24 narrow traces from the 10-14 solo profile
  python3 real_cap.py run        # conditions in order; finished ones are skipped
  python3 real_cap.py analyze    # per-command comparison with the low-load run of each policy and with no cap
"""

import argparse
import bisect
import csv
import json
from pathlib import Path
import random
import subprocess
import sys
import threading
import time

import numpy as np

import cap_sweep

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCAL = HERE / ".local/real"
EXPERIMENT = REPO / "harness/trace-replay/experiment.py"
CONFIG = REPO / "harness/trace-replay/configs/contention.json"
WORKLOAD_CPUS = "2-17"
CORES = 16
MBM = Path("/sys/fs/resctrl/mon_data/mon_L3_00/mbm_total_bytes")  # socket 0 memory traffic, all tasks
CONDITIONS = [(policy, cpus, k, repeats) for k, repeats in ((4, 1), (32, 4))
              for policy, cpus in (("none", None), ("cap4", 4), ("cap1", 1))]


def cond_name(policy, k):
    return f"{policy}-k{k}"


def cmd_select(args):
    profiles = cap_sweep.load_profiles()
    entries = {t["name"]: t for t in json.loads(cap_sweep.CANDIDATES.read_text())["traces"]}
    timed_out = set()
    for d in cap_sweep.DOCKER.iterdir():
        if (d / "commands.csv").exists() and any(r["timed_out"] == "True" for r in csv.DictReader((d / "commands.csv").open())):
            timed_out.add(d.name.split("-", 1)[1])
    rows = []
    for name, p in profiles.items():
        e = entries[name]
        if not e.get("docker_eligible") or (e.get("docker_peak_memory_gib") or 99) > args.max_peak_gib or name in timed_out:
            continue
        demand = [d for _, d in p["commands"]]
        flat = np.concatenate(demand)
        peak = (np.convolve(flat, np.ones(10), "valid") / 10).max() if len(flat) >= 10 else flat.max()
        tool, gap = len(flat) * cap_sweep.DT, sum(g for g, _ in p["commands"])
        if tool + gap > args.max_window_s:
            continue
        rows.append({"name": name, "trace": e["trace"], "peak_cores_100ms": round(float(peak), 2), "tool_s": round(tool, 2),
                     "gap_s": round(gap, 2), "cpu_s": round(float(flat.sum() * cap_sweep.DT), 2),
                     "peak_gib": e["docker_peak_memory_gib"]})
    rng = random.Random(args.seed)
    wide = [r for r in rows if r["peak_cores_100ms"] >= 8]
    narrow = [r for r in rows if r["peak_cores_100ms"] < 4]
    chosen = rng.sample(wide, min(args.per_class, len(wide))) + rng.sample(narrow, min(args.per_class, len(narrow)))
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "traces.json").write_text(json.dumps(chosen, indent=1))
    cpu = sum(r["cpu_s"] for r in chosen)
    span = sum(r["tool_s"] + r["gap_s"] for r in chosen)
    print(f"eligible {len(rows)} (wide {len(wide)}, narrow {len(narrow)}) -> {len(chosen)} traces; "
          f"mean demand {cpu / span:.3f} cores, so K=16 on {CORES} cores is about {16 * cpu / span / CORES:.0%} busy "
          f"before stretching; mean window {span / len(chosen):.0f} s")


def host_sampler(path, stop):
    """Socket 0 memory bandwidth (resctrl MBM) every second, and workload-CPU IPC from a system-wide perf."""
    perf = subprocess.Popen(["sudo", "-n", "/usr/lib/linux-tools-6.8.0-146/perf", "stat", "-a", "-C", WORKLOAD_CPUS,
                             "-e", "instructions,cycles", "-I", "1000", "-x", ",", "-o", str(path.with_suffix(".perf.csv"))],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with path.open("w") as out:
        out.write("t_s,mbm_total_bytes\n")
        while not stop.wait(1.0):
            try:
                out.write(f"{time.monotonic():.3f},{MBM.read_text().strip()}\n")
                out.flush()
            except OSError:
                pass
    subprocess.run(["sudo", "-n", "kill", "-INT", str(perf.pid)], check=False)
    perf.wait(timeout=30)


def cmd_run(args):
    traces = json.loads((LOCAL / "traces.json").read_text())
    runs = LOCAL / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for policy, cpus, k, repeats in CONDITIONS:
        name = cond_name(policy, k)
        if args.only and name not in args.only:
            continue
        out = runs / name
        if (out / "events.jsonl").exists() and '"budget_end"' in (out / "events.jsonl").read_text():
            print(f"{name}: done, skipped", flush=True)
            continue
        if out.exists():
            out.rename(runs / f"{name}-failed-{int(time.time())}")
        while float(Path("/proc/loadavg").read_text().split()[0]) > args.max_load:  # other jobs would skew the run
            print(f"{name}: waiting for host load to drop", flush=True)
            time.sleep(60)
        order = [t["trace"] for t in traces] * repeats
        random.Random(name).shuffle(order)
        command = [sys.executable, str(EXPERIMENT), *[str(REPO / t) for t in order], "--config", str(CONFIG),
                   "--output", str(out), "--schedule", "gaps", "--starts", "together", "--setup", "on-arrival",
                   "--max-active", str(k), "--keep-going", "--command-timeout", "600",
                   *(["--cpus", str(cpus)] if cpus else [])]
        print(f"{name}: {len(order)} traces, K={k}, cpus={cpus}", flush=True)
        stop = threading.Event()
        sampler = threading.Thread(target=host_sampler, args=(runs / f"{name}-host.csv", stop))
        sampler.start()
        started = time.time()
        try:
            result = subprocess.run(command, cwd=REPO)
        finally:
            stop.set()
            sampler.join()
        print(f"{name}: exit {result.returncode}, {time.time() - started:.0f} s", flush=True)


def load_run(directory):
    """Per command: (trace, batch) -> list of dicts with wall, cpu, throttled, some (PSI cpu some) seconds."""
    events = [json.loads(line) for line in (directory / "events.jsonl").open()]
    trace_of = {}
    for e in events:
        if e["event"] == "replay_clock_start":
            trace_of = {s["session"]: s["trace"] for s in e["sessions"]}
    active, changes = 0, []
    for e in events:
        if e["event"] == "session_start":
            active += 1
            changes.append((e["elapsed_s"], active))
        elif e["event"] in ("session_end", "session_failed"):
            active -= 1
            changes.append((e["elapsed_s"], active))
    series = {}
    with (directory / "metrics.csv").open() as stream:
        for r in csv.DictReader(stream):
            if r["session"] and r["cpu_usec"]:
                series.setdefault(r["session"], []).append((float(r["elapsed_s"]), float(r["cpu_usec"]),
                                                            float(r["throttled_usec"] or 0),
                                                            float(r["cpu_pressure_some_usec"] or 0)))
    arrays = {s: np.array(sorted(v)) for s, v in series.items()}

    def at(session, t):
        a = arrays.get(session)
        if a is None or len(a) < 2:
            return None
        return np.array([np.interp(t, a[:, 0], a[:, j]) for j in (1, 2, 3)]) / 1e6

    commands, sessions = {}, {}
    for e in events:
        if e["event"] == "command_end":
            s, t0, wall = e["session"], e["started_s"], e["duration_s"]
            a, b = at(s, t0), at(s, t0 + wall)
            if a is None:
                continue
            cpu, throttled, some = b - a
            commands.setdefault((trace_of[s], e["batch"]), []).append(
                {"t": t0, "wall": wall, "cpu": cpu, "throttled": throttled, "some": some, "timed_out": e["timed_out"]})
        elif e["event"] == "session_start":
            sessions.setdefault(e["session"], {})["start"] = e["elapsed_s"]
        elif e["event"] == "session_end":
            sessions.setdefault(e["session"], {})["end"] = e["elapsed_s"]
    times = [t for t, _ in changes]
    full = [t for t, n in changes if n == max(n for _, n in changes)] if changes else [0.0]
    return {"commands": commands, "sessions": {s: (trace_of[s], v["start"], v["end"]) for s, v in sessions.items()
                                               if "start" in v and "end" in v},
            "window": (min(full), max(full)), "changes": (times, [n for _, n in changes])}


def host_stats(directory, name, window, clock0):
    """Mean socket-0 memory bandwidth (GB/s) and workload-CPU IPC over the run (host files use their own clocks)."""
    out = {}
    host = directory.parent / f"{name}-host.csv"
    if host.exists():
        rows = [tuple(map(float, r.values())) for r in csv.DictReader(host.open())]
        if len(rows) > 2:
            (t0, b0), (t1, b1) = rows[0], rows[-1]
            out["mem_bw_gbs"] = (b1 - b0) / (t1 - t0) / 1e9
    perf = host.with_suffix(".perf.csv") if host.exists() else None
    if perf and perf.exists():
        totals = {}
        for line in perf.read_text().splitlines():
            parts = line.split(",")
            if len(parts) > 3 and parts[1].replace(".", "").isdigit():
                totals[parts[3]] = totals.get(parts[3], 0.0) + float(parts[1])
        if totals.get("cycles"):
            out["ipc"] = totals.get("instructions", 0.0) / totals["cycles"]
    return out


def cmd_analyze(args):
    runs = LOCAL / "runs"
    data = {}
    for policy, cpus, k, repeats in CONDITIONS:
        d = runs / cond_name(policy, k)
        if (d / "events.jsonl").exists():
            data[cond_name(policy, k)] = load_run(d)
    summary = []
    for policy, cpus, k, repeats in CONDITIONS:
        name = cond_name(policy, k)
        if name not in data:
            continue
        run, own, none = data[name], data.get(cond_name(policy, 4)), data.get(cond_name("none", 4))
        lo, hi = run["window"] if k > 4 else (0.0, float("inf"))
        rows = []
        for key, items in run["commands"].items():
            for c in items:
                if lo <= c["t"] <= hi:
                    rows.append((key, c))
        cpu = sum(c["cpu"] for _, c in rows)
        span = (hi - lo) if k > 4 else max(c["t"] + c["wall"] for _, c in rows) - min(c["t"] for _, c in rows)
        result = {"condition": name, "commands": len(rows), "window_s": round(span, 1),
                  "util": cpu / (CORES * span) if k > 4 else None,
                  "over_60s": sum(c["wall"] > 60 for _, c in rows),
                  "some_over_wall": sum(c["some"] for _, c in rows) / sum(c["wall"] for _, c in rows),
                  "throttled_over_wall": sum(c["throttled"] for _, c in rows) / sum(c["wall"] for _, c in rows)}
        for label, ref in (("own", own), ("none", none)):
            if ref is None or (label == "own" and ref is run):
                continue
            pairs = []
            for key, c in rows:
                base = ref["commands"].get(key)
                if base:
                    pairs.append((c, {f: float(np.median([b[f] for b in base])) for f in ("wall", "cpu")}))
            heavy = [(c, b) for c, b in pairs if b["cpu"] >= 1.0]
            if not heavy:
                continue
            ratios = np.array([c["wall"] / b["wall"] for c, b in heavy])
            result.update({f"vs_{label}_heavy_commands": len(heavy),
                           f"vs_{label}_wall_total": sum(c["wall"] for c, _ in pairs) / sum(b["wall"] for _, b in pairs),
                           f"vs_{label}_heavy_p50": float(np.median(ratios)), f"vs_{label}_heavy_p90": float(np.percentile(ratios, 90)),
                           f"vs_{label}_cpu_inflation": sum(c["cpu"] for c, _ in heavy) / sum(b["cpu"] for _, b in heavy)})
        result.update(host_stats(runs / name, name, run["window"], 0.0))
        summary.append(result)
        print(json.dumps({k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in result.items()}))
    (LOCAL / "analysis.json").write_text(json.dumps(summary, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("select")
    p.add_argument("--per-class", type=int, default=24)
    p.add_argument("--max-peak-gib", type=float, default=4.0)
    p.add_argument("--max-window-s", type=float, default=900)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(func=cmd_select)
    p = sub.add_parser("run")
    p.add_argument("--only", nargs="+", help="condition names, e.g. none-k4 cap1-k16")
    p.add_argument("--max-load", type=float, default=4.0, help="wait while the host 1-min load is above this")
    p.set_defaults(func=cmd_run)
    sub.add_parser("analyze").set_defaults(func=cmd_analyze)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
