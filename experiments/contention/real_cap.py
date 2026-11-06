#!/usr/bin/env python3
"""Measured counterpart of cap_sweep.py: concurrent Docker replays of the 264 multilingual traces on one 16-core pool.

Containers run the recorded commands with the recorded LLM gaps (`experiment.py --schedule gaps`), K at a time: when
one finishes, the next queued trace starts (`--setup on-arrival --starts together --max-active K`). Policies are no
cap, `--cpus 4` and `--cpus 1` (CFS quota, as in Docker-based RL harnesses). Unlike the simulator, every source of
contention is present: runqueue, quota throttling, LLC, memory bandwidth, frequency, I/O and the Docker daemon.
Each policy runs at low load (K=4, the reference for its commands) and high load (K=32, every trace four times; the
selected traces average 0.38 cores, so about 76% of the 16 cores before stretching).
With schedutil the clock follows load (K=4 ran about 25% fewer cycles per CPU-second than K=32), so the low-load
reference is slow; `run` refuses unless the workload CPUs and their SMT siblings use the performance governor (bud05
keeps it on every CPU since 2026-11-05; turbo stays on, about 4 GHz). `simulate` replays the same configuration in
fleet.py with the measured K=4 no-cap run as the demand profile, so measured and simulated slowdowns share one
reference and differ only in what the simulator leaves out. The simulator's lost CPU is the CPU-weighted mean stretch
minus one, so `analyze` reports that weighting too (`cpu_weighted`) next to the summed wall time (`wall_total`).

  python3 real_cap.py select     # 24 wide and 24 narrow traces from the 10-14 solo profile
  python3 real_cap.py run --runs runs-perf       # conditions in order; finished ones are skipped
  python3 real_cap.py analyze --runs runs-perf   # per command vs the policy's K=4 run, no cap K=4 and no cap same K
  python3 real_cap.py simulate --runs runs-perf  # fleet.py on 1 node x 16 cores, same K and policies
"""

import argparse
import bisect
import collections
import csv
import glob
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time

import numpy as np

import cap_sweep
import fleet

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCAL = HERE / ".local/real"
EXPERIMENT = REPO / "harness/trace-replay/experiment.py"
CONFIG = REPO / "harness/trace-replay/configs/contention.json"
WORKLOAD_CPUS = "2-17"
CORES = 16
MBM = Path("/sys/fs/resctrl/mon_data/mon_L3_00/mbm_total_bytes")  # socket 0 memory traffic, all tasks
TSC_GHZ = 2.5  # Xeon Gold 6548Y+ base clock; ref-cycles tick at this rate (turbo is on: busy cores run near 4 GHz)
RESCTRL = Path("/sys/fs/resctrl")
# socket 0's L3 has 15 ways (cbm 7fff); ways 13-14 are shared with I/O (shareable_bits 6000), so each pool gets 6 private
# ways and ways 12-14 stay with the default group. Socket 1 (domain 1) is never written.
L3_SPLIT = ("003f", "0fc0")
CONDITIONS = [(policy, cpus, k, repeats) for k, repeats in ((4, 1), (32, 4))
              for policy, cpus in (("none", None), ("cap4", 4), ("cap1", 1))]
SIM_POLICY = {"none": "cap16", "cap4": "cap4", "cap1": "cap1"}  # no quota still sees only the 16 cpuset CPUs


def cond_name(policy, k):
    return f"{policy}-k{k}"


def cpu_list(spec):
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return out


def governor_path(cpu):
    return Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")


def governor_cpus(spec=WORKLOAD_CPUS):
    """Workload CPUs and their SMT siblings (a core's clock follows the busier thread's request)."""
    cpus = set()
    for c in cpu_list(spec):
        cpus.update(cpu_list(Path(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list").read_text().strip()))
    return sorted(cpus)


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


def sudo_write(path, text):
    subprocess.run(["sudo", "-n", "tee", str(path)], input=text, text=True, stdout=subprocess.DEVNULL, check=True)


def resctrl_pools(pools, split):
    """One resctrl group per pool, chosen by CPU: a monitor group under the default group, or with split a control
    group that also gives the pool its own L3 ways (L3_SPLIT, domain 0 only). Returns each group's socket-0 mon_data
    directory; resctrl_clear() removes the groups."""
    dirs = []
    for i, pool in enumerate(pools):
        group = RESCTRL / f"rc-pool{i}" if split else RESCTRL / "mon_groups" / f"rc-pool{i}"
        subprocess.run(["sudo", "-n", "mkdir", str(group)], check=True)
        if split:
            sudo_write(group / "schemata", f"L3:0={L3_SPLIT[i]}\n")
        sudo_write(group / "cpus_list", pool)
        dirs.append(group / "mon_data/mon_L3_00")
    return dirs


def resctrl_clear(n):
    for i in range(n):
        for group in (RESCTRL / f"rc-pool{i}", RESCTRL / "mon_groups" / f"rc-pool{i}"):
            if group.exists():
                subprocess.run(["sudo", "-n", "rmdir", str(group)], check=False)


def host_sampler(path, stop, cpus=WORKLOAD_CPUS, groups=()):
    """Socket 0 memory bandwidth (resctrl MBM) every second, and workload-CPU IPC and clock from a system-wide perf.
    With groups (resctrl_pools), perf counts per CPU and adds LLC references and misses, and each group's L3
    occupancy and memory traffic is sampled too."""
    perf_csv = path.with_suffix(".perf.csv")
    events = "instructions,cycles,ref-cycles" + (",cache-references,cache-misses" if groups else "")
    perf = subprocess.Popen(["sudo", "-n", "/usr/lib/linux-tools-6.8.0-146/perf", "stat", "-a", "-C", cpus,
                             *(["-A"] if groups else []), "-e", events, "-I", "1000", "-x", ",", "-o", str(perf_csv)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if groups:
        path.with_suffix(".perf.json").write_text(json.dumps({"start_unix": time.time()}))
    files = ("llc_occupancy", "mbm_total_bytes", "mbm_local_bytes")
    with path.open("w") as out:
        out.write("t_s,mbm_total_bytes" + "".join(f",g{i}_{f}" for i in range(len(groups)) for f in files)
                  + (",t_unix\n" if groups else "\n"))
        while not stop.wait(1.0):
            try:
                row = [f"{time.monotonic():.3f}", MBM.read_text().strip()]
                row += [(g / f).read_text().strip() for g in groups for f in files]
                out.write(",".join(row) + (f",{time.time():.3f}\n" if groups else "\n"))
                out.flush()
            except OSError:
                pass
    subprocess.run(["sudo", "-n", "kill", "-INT", str(perf.pid)], check=False)
    try:
        perf.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    # sudo may not pass the signal on, which left perf running after earlier runs
    subprocess.run(["sudo", "-n", "pkill", "-INT", "-f", "--", f"[p]erf stat .*-o {perf_csv}"], check=False)


def finished(out):
    """A run counts as done when every queued trace ended; an interrupted run also writes budget_end."""
    events = out / "events.jsonl"
    if not events.exists():
        return False
    text = events.read_text()
    return '"budget_end"' in text and text.count('"session_queued"') == text.count('"session_end"') + text.count(
        '"session_failed"') and '"session_queued"' in text


def cmd_run(args):
    traces = json.loads((LOCAL / "traces.json").read_text())
    runs = LOCAL / args.runs
    runs.mkdir(parents=True, exist_ok=True)
    cpus_g = governor_cpus()
    for policy, cpus, k, repeats in CONDITIONS:
        name = cond_name(policy, k)
        if args.only and name not in args.only:
            continue
        out = runs / name
        if finished(out):
            print(f"{name}: done, skipped", flush=True)
            continue
        if out.exists():
            out.rename(runs / f"{name}-failed-{int(time.time())}")
        while float(Path("/proc/loadavg").read_text().split()[0]) > args.max_load:  # other jobs would skew the run
            print(f"{name}: waiting for host load to drop", flush=True)
            time.sleep(60)
        governors = sorted({governor_path(c).read_text().strip() for c in cpus_g})
        if args.governor and governors != [args.governor]:
            raise SystemExit(f"{name}: governor on CPUs {cpus_g} is {governors}, not {args.governor}")
        order = [t["trace"] for t in traces] * repeats
        random.Random(name).shuffle(order)
        command = [sys.executable, str(EXPERIMENT), *[str(REPO / t) for t in order], "--config", str(CONFIG),
                   "--output", str(out), "--schedule", "gaps", "--starts", "together", "--setup", "on-arrival",
                   "--max-active", str(k), "--keep-going", "--command-timeout", "600",
                   *(["--cpus", str(cpus)] if cpus else [])]
        print(f"{name}: {len(order)} traces, K={k}, cpus={cpus}, governor {'/'.join(governors)}", flush=True)
        (runs / f"{name}-host.json").write_text(json.dumps({"governor": governors, "governor_cpus": cpus_g}))
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
            "window": (min(full), max(full)), "changes": (times, [n for _, n in changes]), "series": arrays}


def saturation(run, lo, hi, bin_s=0.1):
    """Share of 100 ms bins where the containers together ran on >= CORES - 0.5 cores, and the share of all PSI
    cpu-some time that fell in those bins (near 1: tasks waited only when the pool was full, i.e. no idle core)."""
    grid = np.arange(lo, hi, bin_s)
    use, some = np.zeros(len(grid) - 1), np.zeros(len(grid) - 1)
    for a in run["series"].values():
        if len(a) < 2:
            continue
        inside = (grid >= a[0, 0]) & (grid <= a[-1, 0])
        inside = inside[:-1] & inside[1:]
        use += np.where(inside, np.diff(np.interp(grid, a[:, 0], a[:, 1])), 0.0) / 1e6 / bin_s
        some += np.where(inside, np.diff(np.interp(grid, a[:, 0], a[:, 3])), 0.0) / 1e6
    full = use >= CORES - 0.5
    return {"saturated_time": float(full.mean()), "some_when_saturated": float(some[full].sum() / some.sum())}


def versus_1014(run):
    """Tool and CPU seconds of each trace's first replay against the 10-14 solo profile in traces.json."""
    old = {t["name"]: t for t in json.loads((LOCAL / "traces.json").read_text())}
    tool, cpu = {}, {}
    for (name, _), items in run["commands"].items():
        tool[name] = tool.get(name, 0.0) + items[0]["wall"]
        cpu[name] = cpu.get(name, 0.0) + items[0]["cpu"]
    names = [n for n in tool if n in old]
    gap = sum(old[n]["gap_s"] for n in names)
    return {"tool_vs_1014": sum(tool[n] for n in names) / sum(old[n]["tool_s"] for n in names),
            "cpu_vs_1014": sum(cpu[n] for n in names) / sum(old[n]["cpu_s"] for n in names),
            "tool_share": sum(tool.values()) / (sum(tool.values()) + gap),
            "tool_share_1014": sum(old[n]["tool_s"] for n in names) / (sum(old[n]["tool_s"] for n in names) + gap)}


def host_stats(directory, name, window, clock0):
    """Mean socket-0 memory bandwidth (GB/s) and workload-CPU IPC over the run (host files use their own clocks)."""
    out = {}
    host = directory.parent / f"{name}-host.csv"
    if host.exists():
        rows = [(float(r["t_s"]), float(r["mbm_total_bytes"])) for r in csv.DictReader(host.open())]
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
        if totals.get("ref-cycles"):
            out["busy_ghz"] = totals["cycles"] / totals["ref-cycles"] * TSC_GHZ  # mean clock while not halted
    return out


def cmd_analyze(args):
    runs = LOCAL / args.runs
    data = {}
    dirs = {}
    for policy, cpus, k, repeats in CONDITIONS:
        d = runs / cond_name(policy, k)
        if not (d / "events.jsonl").exists() and k == 4 and args.reference:
            d = LOCAL / args.reference / cond_name(policy, k)  # K=4 runs alone at ~5% load; its order barely matters
        if (d / "events.jsonl").exists():
            data[cond_name(policy, k)], dirs[cond_name(policy, k)] = load_run(d), d
    summary = []
    for policy, cpus, k, repeats in CONDITIONS:
        name = cond_name(policy, k)
        if name not in data:
            continue
        run = data[name]
        refs = {"own": data.get(cond_name(policy, 4)), "none": data.get(cond_name("none", 4)),
                "none_same_k": data.get(cond_name("none", k))}
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
                  "timed_out": sum(bool(c["timed_out"]) for _, c in rows),
                  "some_over_wall": sum(c["some"] for _, c in rows) / sum(c["wall"] for _, c in rows),
                  "throttled_over_wall": sum(c["throttled"] for _, c in rows) / sum(c["wall"] for _, c in rows)}
        used = [run]
        for label, ref in refs.items():
            if ref is None or any(ref is u for u in used):
                continue
            used.append(ref)
            # Ratios leave out a command that hit the 600 s cut on either side (counted in timed_out) and one whose CPU
            # time is off by more than 2x from the reference (diverged: it did other work, e.g. a stalled download)
            pairs, diverged = [], 0
            for key, c in rows:
                base = ref["commands"].get(key)
                if not base or c["timed_out"] or any(b["timed_out"] for b in base):
                    continue
                b = {f: float(np.median([x[f] for x in base])) for f in ("wall", "cpu")}
                if b["cpu"] >= 0.1 and not 0.5 <= c["cpu"] / b["cpu"] <= 2.0:
                    diverged += 1
                    continue
                pairs.append((c, b))
            result[f"vs_{label}_diverged"] = diverged
            heavy = [(c, b) for c, b in pairs if b["cpu"] >= 1.0]
            if not heavy:
                continue
            ratios = np.array([c["wall"] / b["wall"] for c, b in heavy])
            weighted = [(b["cpu"], c["wall"] / b["wall"]) for c, b in pairs if b["wall"] > 0]
            result.update({f"vs_{label}_heavy_commands": len(heavy),
                           f"vs_{label}_wall_total": sum(c["wall"] for c, _ in pairs) / sum(b["wall"] for _, b in pairs),
                           f"vs_{label}_cpu_weighted": sum(w * r for w, r in weighted) / sum(w for w, _ in weighted),
                           f"vs_{label}_heavy_p50": float(np.median(ratios)), f"vs_{label}_heavy_p90": float(np.percentile(ratios, 90)),
                           f"vs_{label}_cpu_inflation": sum(c["cpu"] for c, _ in heavy) / sum(b["cpu"] for _, b in heavy)})
        result.update(saturation(run, *(run["window"] if k > 4 else (0.0, span))))
        if k == 4:
            result.update(versus_1014(run))
        result.update(host_stats(dirs[name], name, run["window"], 0.0))
        summary.append(result)
        print(json.dumps({k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in result.items()}))
    (LOCAL / f"{args.runs}-analysis.json").write_text(json.dumps(summary, indent=1))


def run_profiles(directory):
    """cap_sweep profiles (recorded gap, demand per 10 ms) from a run where every trace ran once, e.g. none-k4."""
    paths = {t["name"]: REPO / t["trace"] for t in json.loads((LOCAL / "traces.json").read_text())}
    entries = {t["name"]: t for t in json.loads(cap_sweep.CANDIDATES.read_text())["traces"]}
    events = [json.loads(line) for line in (directory / "events.jsonl").open()]
    trace_of = next(({s["session"]: s["trace"] for s in e["sessions"]} for e in events
                     if e["event"] == "replay_clock_start"), {})
    series = {}
    with (directory / "metrics.csv").open() as stream:
        for r in csv.DictReader(stream):
            if r["session"] and r["cpu_usec"]:
                series.setdefault(r["session"], []).append((float(r["elapsed_s"]), float(r["cpu_usec"]) / 1e6))
    commands = {}
    for e in events:
        if e["event"] == "command_end":
            commands.setdefault(e["session"], {})[e["batch"]] = (e["started_s"], e["started_s"] + e["duration_s"])
    out = {}
    for session, spans in commands.items():
        a = np.array(sorted(series.get(session, [])))
        if len(a) < 2:
            continue
        name = trace_of[session]
        batches = cap_sweep.replay.load_trace(str(paths[name]))["batches"]
        profile = []
        for b in sorted(spans):
            start, end = spans[b]
            n = max(1, math.ceil((end - start) / cap_sweep.DT - 1e-9))
            edges = np.minimum(start + cap_sweep.DT * np.arange(n + 1), end)
            demand = np.clip(np.diff(np.interp(edges, a[:, 0], a[:, 1])) / cap_sweep.DT, 0.0, CORES)
            profile.append((batches[b]["gap"] if profile else 0.0, demand))
        out[name] = {"commands": profile, "memory_gib": entries[name].get("memory_mib", 2048) / 1024}
    return out


def cmd_simulate(args):
    """fleet.py (runqueue sharing only) on the measured configuration, demand from the run's no-cap K=4 replay."""
    profiles = run_profiles(LOCAL / args.runs / cond_name("none", 4))
    names = sorted(profiles)
    fleet.TIMEOUT_S = 1e6
    rows = []
    for policy in SIM_POLICY:
        sim_policy = SIM_POLICY[policy]
        programs, base = cap_sweep.programs_for(profiles, sim_policy)
        arrays = fleet.flatten(programs, names)
        arrays["trace_cpu_s"] = np.array([base[n] for n in names])  # slowdowns against the uncapped K=4 profile
        tool = cap_sweep.solo_summary(profiles, sim_policy)["tool_time_ratio"]
        v = cap_sweep.vcpus_for(sim_policy, None)
        slots = [d for p in profiles.values() for _, d in p["commands"]]  # cap stretch per command, CPU-weighted
        cpu_weighted = sum(d.sum() * len(cap_sweep.stretch(d, v)) / len(d) for d in slots) / sum(d.sum() for d in slots)
        for k in sorted({k for _, _, k, _ in CONDITIONS}):
            results = []
            for seed in range(1, args.seeds + 1):
                config = fleet.DEFAULTS | dict(nodes=1, cores=CORES, per_node=k, floating=True, pool="none",
                                               mechanism="none", seed=str(seed), ramp_s=args.ramp_s,
                                               duration_s=args.duration_s, carry=True)
                results.append(fleet.simulate({"arrays": arrays, "names": names, "config": config, "groups": [],
                                               "images": names, "blocks": None}))
            mean = lambda key: float(np.mean([r[key] for r in results if r[key] is not None]))
            row = {"condition": cond_name(policy, k), "sim_policy": sim_policy, "seeds": len(results),
                   "util": mean("run_cores_per_node") / CORES, "lost_over_run": mean("lost_over_run"),
                   "solo_tool_ratio": tool, "solo_cpu_weighted": cpu_weighted,
                   "tool_vs_none_solo": tool * (1 + mean("lost_over_run")),
                   "trace_slowdown_p50": mean("trace_slowdown_p50"), "trace_slowdown_p90": mean("trace_slowdown_p90")}
            rows.append(row)
            print(json.dumps({k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in row.items()}), flush=True)
    (LOCAL / f"{args.runs}-sim.json").write_text(json.dumps(rows, indent=1))


def per_container(run, lo, hi, bin_s):
    """CPU cores used and PSI cpu-some seconds per container and bin (rows follow run["series"])."""
    grid = np.arange(lo, hi, bin_s)
    use, some = np.zeros((len(run["series"]), len(grid) - 1)), np.zeros((len(run["series"]), len(grid) - 1))
    for i, a in enumerate(run["series"].values()):
        if len(a) < 2:
            continue
        inside = (grid >= a[0, 0]) & (grid <= a[-1, 0])
        inside = inside[:-1] & inside[1:]
        use[i] = np.where(inside, np.diff(np.interp(grid, a[:, 0], a[:, 1])), 0.0) / 1e6 / bin_s
        some[i] = np.where(inside, np.diff(np.interp(grid, a[:, 0], a[:, 3])), 0.0) / 1e6
    return use, some


def episodes(hot, bin_s, merge_s):
    """[start, end) bin spans of runs of hot bins, gaps up to merge_s joined, and each bin's age in its span (-1 outside)."""
    edges = np.flatnonzero(np.diff(np.concatenate([[0], hot.astype(int), [0]])))
    spans = []
    for a, b in zip(edges[::2], edges[1::2]):
        if spans and (a - spans[-1][1]) * bin_s <= merge_s:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    age = np.full(len(hot), -1.0)
    for a, b in spans:
        age[a:b] = np.arange(b - a) * bin_s
    return spans, age


def cmd_handoff(args):
    """What a mover could get back, from the measured K=32 runs alone (no simulator).

    A contended bin has the pool full (>= CORES - 0.5 cores used) and on average at least half a container waiting
    (PSI cpu some). Episodes are runs of contended bins, gaps up to --merge-s joined; the loss is PSI-some time.
    - timing: a mover that reacts d seconds into an episode and ends it gets back at most the loss after age d. Per
      command, if the extra time accrues evenly over the loaded run and the command finishes at its K=4 pace once
      moved, a move after d seconds gives back wall32 - (d + (1 - d / wall32) * wall4).
    - how many moves: each running command wants its K=4 mean rate (CPU / wall); the moves needed in a bin are the
      fewest containers, largest first, whose removal brings the demand to the pool size. Mean rates hide bursts, so
      this is a lower bound.
    - who and at what cost: wide (100 ms peak >= 8 cores) vs narrow traces; CXL/DRAM trace latency of the same traces
      from the 10-14 replay (one run each, schedutil low clock, Docker immediate schedule)."""
    runs = LOCAL / args.runs
    traces = json.loads((LOCAL / "traces.json").read_text())
    wide = {t["name"] for t in traces if t["peak_cores_100ms"] >= 8}
    ref_dir = runs / cond_name("none", 4)
    if not (ref_dir / "events.jsonl").exists() and args.reference:
        ref_dir = LOCAL / args.reference / cond_name("none", 4)
    ref = load_run(ref_dir)
    rate = {k: v[0]["cpu"] / v[0]["wall"] for k, v in ref["commands"].items() if v[0]["wall"] > 0}
    rows = []
    for policy in ("none", "cap4"):
        name = cond_name(policy, 32)
        run = load_run(runs / name)
        lo, hi = run["window"]
        use, some = per_container(run, lo, hi, args.bin_s)
        node, waiting = use.sum(0), some.sum(0)
        spans, age = episodes((node >= CORES - 0.5) & (waiting > 0.5 * args.bin_s), args.bin_s, args.merge_s)
        hot = age >= 0
        lengths = np.array([(b - a) * args.bin_s for a, b in spans])
        loss = np.array([waiting[a:b].sum() for a, b in spans])
        order = np.argsort(lengths)
        cum = np.cumsum(loss[order]) / loss.sum()
        w = waiting[hot] / waiting[hot].sum()  # loss weight of each contended bin
        sessions = list(run["series"])
        is_wide = np.array([run["sessions"].get(s, ("",))[0] in wide for s in sessions])
        r = {"condition": name, "bin_s": args.bin_s, "contended_time": float(hot.mean()), "episodes": len(spans),
             "episodes_per_min": len(spans) / ((hi - lo) / 60), "loss_in_episodes": float(loss.sum() / waiting.sum()),
             **{f"loss_weighted_length_p{q}": float(lengths[order][min(len(cum) - 1, np.searchsorted(cum, q / 100))])
                for q in (10, 50, 90)},
             **{f"loss_after_{d:g}s": float(waiting[age >= d].sum() / waiting.sum()) for d in args.delays},
             "wide_share_of_loss": float(some[is_wide][:, hot].sum() / some[:, hot].sum()),
             "wide_cores_when_contended": float((w * use[is_wide][:, hot].sum(0)).sum()),
             "narrow_cores_when_contended": float((w * use[~is_wide][:, hot].sum(0)).sum()),
             **{f"free_ge{x}_cores_time": float((CORES - node >= x).mean()) for x in (4, 8, 12)}}
        if policy == "none":
            events = [json.loads(line) for line in (runs / name / "events.jsonl").open()]
            trace_of = next({s["session"]: s["trace"] for s in e["sessions"]} for e in events
                            if e["event"] == "replay_clock_start")
            row_of = {s: i for i, s in enumerate(sessions)}
            grid = np.arange(lo, hi, args.bin_s)[:use.shape[1]]
            demand = np.zeros_like(use)
            for e in events:
                key = (trace_of.get(e.get("session")), e.get("batch"))
                if e["event"] == "command_end" and e["session"] in row_of and key in rate:
                    a, b = np.searchsorted(grid, [e["started_s"], e["started_s"] + e["duration_s"]])
                    demand[row_of[e["session"]], a:b] = min(rate[key], CORES)
            want = -np.sort(-demand[:, hot], axis=0)  # per contended bin, largest demand first
            tail = want[::-1].cumsum(0)[::-1]  # demand left after removing the k largest = tail[k]
            moves = (tail > CORES).sum(0)
            for a0, a1 in ((0, 0.25), (0.25, 1), (1, 5), (5, 15), (15, None)):
                m = (age[hot] >= a0) & (age[hot] < (a1 or np.inf))
                mw = w[m] / w[m].sum()
                r[f"age_{a0:g}_{a1 or 'end'}s"] = {"loss": float(w[m].sum()),
                                                  "demand_over_pool": float((mw * want[:, m].sum(0) / CORES).sum()),
                                                  "moves_p50": float(np.percentile(moves[m], 50)),
                                                  "le2_moves": float(mw[moves[m] <= 2].sum())}
            pairs = {True: [], False: []}
            for key, items in run["commands"].items():
                base = ref["commands"].get(key)
                if not base or base[0]["timed_out"]:
                    continue
                for c in items:
                    if lo <= c["t"] <= hi and not c["timed_out"] and (
                            base[0]["cpu"] < 0.1 or 0.5 <= c["cpu"] / base[0]["cpu"] <= 2.0):
                        pairs[key[0] in wide].append((c["wall"], base[0]["wall"]))
            both = pairs[True] + pairs[False]
            extra = sum(max(0.0, x - b) for x, b in both)
            r.update({"commands": len(both), "tool_total": sum(x for x, _ in both) / sum(b for _, b in both),
                      "tool_total_wide": sum(x for x, _ in pairs[True]) / sum(b for _, b in pairs[True]),
                      "tool_total_narrow": sum(x for x, _ in pairs[False]) / sum(b for _, b in pairs[False]),
                      "wide_share_of_extra": sum(max(0.0, x - b) for x, b in pairs[True]) / extra,
                      **{f"extra_in_cmds_ge{x}s": sum(max(0.0, c - b) for c, b in both if c >= x) / extra
                         for x in (1, 5, 10, 30)}})
            for d in args.delays:
                back = sum(max(0.0, x - (d + (1 - d / x) * b)) for x, b in both if d < x)
                r[f"extra_back_after_{d:g}s"] = back / extra
                r[f"tool_total_after_{d:g}s"] = (sum(x for x, _ in both) - back) / sum(b for _, b in both)
        rows.append(r)
        print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}), flush=True)
    names = {t["name"] for t in traces}
    tax = {"wide": [], "narrow": []}
    with (REPO / "docs/results/numa-immediate-20261014/paired-traces.csv").open() as stream:
        for t in csv.DictReader(stream):
            if t["eligible"] == "True" and t["trace"] in names:
                tax["wide" if t["trace"] in wide else "narrow"].append(float(t["trace_latency_ratio_cxl_over_dram"]))
    cxl = {f"cxl_tax_{k}_{s}": float(f(v)) for k, v in tax.items() if v
           for s, f in (("n", len), ("p50", np.median), ("p90", lambda x: np.percentile(x, 90)))}
    rows.append(cxl)
    print(json.dumps({k: round(v, 4) for k, v in cxl.items()}))
    (LOCAL / f"{args.runs}-handoff.json").write_text(json.dumps(rows, indent=1))


CONTROLLER_CPUS = "0-1"
POOLS = ("2-16", "17-31")  # two 15-core pools on NUMA 0 next to the controller CPUs
POOL_K = 60  # 2 sandboxes per core, as K=32 on 16 cores
POOL_REPEATS = 8  # 384 traces through 60 slots: about as long as the K=32 runs
POOL_RUNS = [("move-0.06s", 0.06), ("static", None), ("move-1s", 1.0), ("move-5s", 5.0), ("shared", None)]
MOVE_HOLD_S = 5.0  # a moved container stays put this long
MOVE_MIN_FREE = 4.0  # the destination needs this many idle cores (mean over the last 0.5 s)
MOVE_MIN_WAIT_S = 0.1  # a candidate waited at least this long in the last 0.5 s
MOVE_DEST_COOL_S = 0.5  # a pool takes no new container for this long after one arrived (its free cores catch up)
GAIN_RATIO = 1.5  # rules "gain" and "ratio": the candidate would get at least 1.5x its current cores in the destination
GAIN_MIN_CORES = 1.0  # "gain" also wants one core more: at 95% use every container gets about one core, so this means 2x
RATIO_MIN_CORES = 0.1  # "ratio" keeps only a noise floor
BALANCE_MIN_DIFF = 2.0  # rule "balance": the source has this many more waiting containers than the destination (0.5 s)


def water_level(uses, size):
    """Cores a newcomer would get in a pool that shares CPU equally per container (cgroup v2, equal cpu.weight), taking
    the others' current use as their demand: those under the level keep theirs, the rest drop to the level."""
    rest, n = size, len(uses) + 1
    for u in sorted(uses):
        if u >= rest / n:
            break
        rest, n = rest - u, n - 1
    return rest / n


CPUSET_HELPER = r"""
import re, sys
target = re.compile(r"/sys/fs/cgroup/minireplay[0-9a-f]+\.slice/docker-[0-9a-f]{64}\.scope/cpuset\.cpus")
for line in sys.stdin:
    path, cpus = line.split()
    try:
        if not target.fullmatch(path) or not re.fullmatch(r"[0-9,-]+", cpus):
            raise ValueError(path)
        with open(path, "w") as stream:
            stream.write(cpus)
        print("ok", flush=True)
    except Exception as error:
        print("err", error, flush=True)
"""


class Mover(threading.Thread):
    """Moves one container at a time out of a contended pool by writing its cgroup cpuset.cpus through a root helper
    (`docker update --cpuset-cpus` took 0.05-2 s while dockerd was busy). Only the CPU moves (memory stays on node 0),
    so this is an ideal hand-off; the write takes about a millisecond and the kernel moves the tasks at once.

    Every 20 ms it reads each container's cgroup CPU usage and PSI cpu-some total. A pool is contended in a tick when
    its containers use >= size - 0.5 cores and on average at least half a container waits, as in `handoff`; gaps up to
    0.1 s are joined. After `delay` seconds of contention, while the pool is still contended, the container that waited
    most over the last 0.5 s (at least MOVE_MIN_WAIT_S) moves to another pool that took no container in the last
    MOVE_DEST_COOL_S; the clock then restarts, so each further move needs another `delay`. A moved container stays put
    for `hold` seconds. delay None only records each container's pool. The destination rule:
    - "free": the pool with the most idle cores, if it is not contended and has >= MOVE_MIN_FREE idle cores.
    - "gain": the pool where the candidate would get the most cores (`water_level` over the 0.5 s mean use of the
      containers there), if that is >= GAIN_RATIO x and GAIN_MIN_CORES more than it gets now. The destination may be
      busy or even contended: a sandbox starved by a crowd of containers moves to a busy pool shared by fewer.
    - "ratio": as "gain" with RATIO_MIN_CORES instead of GAIN_MIN_CORES.
    - "balance": the pool with the fewest waiting containers (PSI cpu-some summed over its containers, 0.5 s mean), if
      the source has at least BALANCE_MIN_DIFF more. Like the kernel balancing one shared pool, but by containers."""

    def __init__(self, pools, delay, log_path, stop, rule="free", cpus=CONTROLLER_CPUS, hold=MOVE_HOLD_S):
        super().__init__(daemon=True)
        self.pools, self.delay, self.stop, self.rule, self.cpus, self.hold = pools, delay, stop, rule, cpus, hold
        self.size = {p: len(cpu_list(p)) for p in pools}
        self.log = log_path.open("w", newline="")
        self.csv = csv.writer(self.log)  # quotes a pool like "2-16,17-31"
        self.csv.writerow(["t_unix", "event", "container", "from_pool", "to_pool", "waited_s", "pool_use", "latency_s"])

    def write(self, event, name, a="", b="", waited=0.0, use=0.0, latency=0.0):
        self.csv.writerow([f"{time.time():.3f}", event, name, a, b, f"{waited:.3f}", f"{use:.2f}", f"{latency:.3f}"])
        self.log.flush()

    def run(self):
        os.sched_setaffinity(0, cpu_list(self.cpus))  # this thread only
        tick, window = 0.02, 25
        known = {}
        helper = None if self.delay is None else subprocess.Popen(
            ["sudo", "-n", sys.executable, "-c", CPUSET_HELPER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            bufsize=1)
        hot_since, last_hot = dict.fromkeys(self.pools), dict.fromkeys(self.pools, -1e9)
        arrived = dict.fromkeys(self.pools, -1e9)
        free = {p: collections.deque(maxlen=window) for p in self.pools}
        waiting = {p: collections.deque(maxlen=window) for p in self.pools}
        next_scan = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_scan:
                next_scan = now + 0.5
                listing = subprocess.run(["docker", "ps", "--no-trunc", "--filter", "name=mini-replay-", "--format",
                                          "{{.ID}} {{.Names}}"], capture_output=True, text=True).stdout
                live = dict(line.split(" ", 1) for line in listing.splitlines() if " " in line)
                for cid in [c for c in known if c not in live]:
                    del known[cid]
                for cid, name in live.items():
                    paths = [] if cid in known else glob.glob(f"/sys/fs/cgroup/minireplay*.slice/docker-{cid}.scope")
                    if not paths:
                        continue
                    try:
                        cpus = cpu_list(Path(paths[0], "cpuset.cpus").read_text().strip())
                    except OSError:
                        continue
                    pool = next((p for p in self.pools if cpu_list(p) == cpus), None)
                    if pool:  # the budget container sits on the controller CPUs
                        known[cid] = {"name": name, "path": Path(paths[0]), "pool": pool, "prev": None,
                                      "wait": collections.deque(maxlen=window), "use": collections.deque(maxlen=window),
                                      "moved_at": -1e9}
                        self.write("seen", name, pool)
            use, wait = dict.fromkeys(self.pools, 0.0), dict.fromkeys(self.pools, 0.0)
            for c in known.values():
                try:
                    usage = int((c["path"] / "cpu.stat").read_text().split("\n", 1)[0].split()[1])
                    some = int((c["path"] / "cpu.pressure").read_text().split("\n", 1)[0].rsplit("=", 1)[1])
                except (OSError, IndexError, ValueError):
                    continue
                if c["prev"]:
                    dt = now - c["prev"][0]
                    u = (usage - c["prev"][1]) / 1e6 / dt
                    use[c["pool"]] += u
                    w = (some - c["prev"][2]) / 1e6 / dt
                    wait[c["pool"]] += w
                    c["wait"].append(w)
                    c["use"].append(u)
                c["prev"] = (now, usage, some)
            for p in self.pools:
                free[p].append(self.size[p] - use[p])
                waiting[p].append(wait[p])
                if use[p] >= self.size[p] - 0.5 and wait[p] > 0.5:
                    if now - last_hot[p] > 0.1:
                        hot_since[p] = now
                    last_hot[p] = now
                elif now - last_hot[p] > 0.1:
                    hot_since[p] = None
            for p in (self.pools if self.delay is not None else ()):
                if hot_since[p] is None or now - hot_since[p] < self.delay or last_hot[p] != now:
                    continue
                movable = [(cid, c) for cid, c in known.items() if c["pool"] == p and now - c["moved_at"] >= self.hold
                           and sum(c["wait"]) * tick >= MOVE_MIN_WAIT_S]
                if not movable:
                    continue
                cid, c = max(movable, key=lambda item: sum(item[1]["wait"]))
                if self.rule == "free":
                    dests = [q for q in self.pools if q != p and hot_since[q] is None
                             and now - arrived[q] >= MOVE_DEST_COOL_S and np.mean(free[q]) >= MOVE_MIN_FREE]
                    dest = max(dests, key=lambda q: np.mean(free[q])) if dests else None
                elif self.rule == "balance":
                    pending = {q: np.mean(waiting[q]) for q in self.pools
                               if q != p and now - arrived[q] >= MOVE_DEST_COOL_S}
                    dest = min(pending, key=pending.get) if pending else None
                    if dest and np.mean(waiting[p]) - pending[dest] < BALANCE_MIN_DIFF:
                        dest = None
                else:
                    mine = np.mean(c["use"]) if c["use"] else 0.0
                    level = {q: water_level([np.mean(o["use"]) for o in known.values() if o["pool"] == q and o["use"]],
                                            self.size[q])
                             for q in self.pools if q != p and now - arrived[q] >= MOVE_DEST_COOL_S}
                    dest = max(level, key=level.get) if level else None
                    floor = GAIN_MIN_CORES if self.rule == "gain" else RATIO_MIN_CORES
                    if dest and not (level[dest] >= GAIN_RATIO * mine and level[dest] >= mine + floor):
                        dest = None
                if dest is None:
                    continue
                helper.stdin.write(f"{c['path'] / 'cpuset.cpus'} {dest}\n")
                reply = helper.stdout.readline().strip()
                self.write("move" if reply == "ok" else "move_failed", c["name"], p, dest, sum(c["wait"]) * tick, use[p],
                           time.monotonic() - now)
                if reply == "ok":
                    c["pool"], c["moved_at"], hot_since[p], arrived[dest] = dest, now, now, now
            time.sleep(max(0.0, tick - (time.monotonic() - now)))
        if helper:
            helper.stdin.close()
            helper.wait(timeout=10)
        self.log.close()


def cmd_pools_run(args):
    """Two 15-core pools with 60 sandboxes, static or with the mover at several delays, and one shared 30-core pool."""
    traces = json.loads((LOCAL / "traces.json").read_text())
    runs = LOCAL / args.runs
    runs.mkdir(parents=True, exist_ok=True)
    order = [t["trace"] for t in traces] * POOL_REPEATS
    random.Random("two-pool").shuffle(order)  # same arrival order in every condition
    union = ",".join(POOLS)
    cpus_g = governor_cpus(union)
    for name, delay in POOL_RUNS:
        if args.only and name not in args.only:
            continue
        out = runs / name
        if finished(out):
            print(f"{name}: done, skipped", flush=True)
            continue
        if out.exists():
            out.rename(runs / f"{name}-failed-{int(time.time())}")
        while float(Path("/proc/loadavg").read_text().split()[0]) > args.max_load:
            print(f"{name}: waiting for host load to drop", flush=True)
            time.sleep(60)
        governors = sorted({governor_path(c).read_text().strip() for c in cpus_g})
        if governors != ["performance"]:
            raise SystemExit(f"{name}: governor on CPUs {cpus_g} is {governors}, not performance")
        traces_now = order[:args.limit] if args.limit else order
        pools = POOLS if name != "shared" else (union,)
        command = [sys.executable, str(EXPERIMENT), *[str(REPO / t) for t in traces_now], "--config", str(CONFIG),
                   "--output", str(out), "--schedule", "gaps", "--starts", "together", "--setup", "on-arrival",
                   "--max-active", str(args.k), "--keep-going", "--command-timeout", "600",
                   *(["--pools", *POOLS] if len(pools) > 1 else ["--workload-cpus", union])]
        print(f"{name}: {len(traces_now)} traces, K={args.k}, pools {pools}, move delay {delay}", flush=True)
        stop = threading.Event()
        sampler = threading.Thread(target=host_sampler, args=(runs / f"{name}-host.csv", stop, union))
        mover = Mover(pools, delay, runs / f"{name}-moves.csv", stop)
        sampler.start()
        mover.start()
        started = time.time()
        try:
            result = subprocess.run(command, cwd=REPO)
        finally:
            stop.set()
            sampler.join()
            mover.join()
        print(f"{name}: exit {result.returncode}, {time.time() - started:.0f} s", flush=True)


def cmd_pools_analyze(args):
    """Per command against the 16-core no-cap K=4 run (runs-perf), contention per pool and the moves made."""
    wide = {t["name"] for t in json.loads((LOCAL / "traces.json").read_text()) if t["peak_cores_100ms"] >= 8}
    ref = load_run(LOCAL / args.reference / cond_name("none", 4))
    runs = LOCAL / args.runs
    rows = []
    for name, delay in POOL_RUNS:
        d = runs / name
        if not (d / "events.jsonl").exists():
            continue
        run = load_run(d)
        lo, hi = run["window"]
        if hi - lo < 60:  # a short smoke run never holds K for long: use the whole run
            lo, hi = min(s for _, s, _ in run["sessions"].values()), max(e for _, _, e in run["sessions"].values())
        pairs = {True: [], False: []}
        for key, items in run["commands"].items():
            base = ref["commands"].get(key)
            if not base or base[0]["timed_out"]:
                continue
            for c in items:
                if lo <= c["t"] <= hi and not c["timed_out"] and (
                        base[0]["cpu"] < 0.1 or 0.5 <= c["cpu"] / base[0]["cpu"] <= 2.0):
                    pairs[key[0] in wide].append((c, base[0]))
        both = pairs[True] + pairs[False]
        heavy = np.array([c["wall"] / b["wall"] for c, b in both if b["cpu"] >= 1.0])
        total = lambda ps: sum(c["wall"] for c, _ in ps) / sum(b["wall"] for _, b in ps)
        span = hi - lo
        r = {"condition": name, "delay_s": delay, "commands": len(both),
             "util": sum(c["cpu"] for c, _ in both) / (len(cpu_list(",".join(POOLS))) * span),
             "timed_out": sum(bool(c["timed_out"]) for items in run["commands"].values() for c in items
                              if lo <= c["t"] <= hi),
             "tool_total": total(both), "tool_total_wide": total(pairs[True]), "tool_total_narrow": total(pairs[False]),
             "cpu_weighted": sum(b["cpu"] * c["wall"] / b["wall"] for c, b in both if b["wall"] > 0)
             / sum(b["cpu"] for c, b in both if b["wall"] > 0),
             "heavy_p50": float(np.median(heavy)), "heavy_p90": float(np.percentile(heavy, 90))}
        moves = d.parent / f"{name}-moves.csv"
        if moves.exists():
            start = next(json.loads(line)["wall_time_unix"] for line in (d / "events.jsonl").open()
                         if '"replay_clock_start"' in line)
            log = list(csv.DictReader(moves.open()))
            made = [m for m in log if m["event"] == "move" and lo <= float(m["t_unix"]) - start <= hi]
            r.update({"moves": len(made), "moves_per_min": len(made) / (span / 60),
                      "moves_failed": sum(m["event"] == "move_failed" for m in log),
                      "move_latency_p50_s": float(np.median([float(m["latency_s"]) for m in made])) if made else None})
            # contended time per pool from each container's pool over time
            pool_at = collections.defaultdict(list)
            if name == "shared":  # one pool (early logs wrote "2-16,17-31" unquoted)
                for s in run["series"]:
                    pool_at[s].append((-np.inf, ",".join(POOLS)))
            for m in (log if name != "shared" else ()):
                if m["event"] in ("seen", "move"):
                    pool_at[m["container"]].append((float(m["t_unix"]) - start, m["to_pool"] or m["from_pool"]))
            use, some = per_container(run, lo, hi, 0.02)
            grid = np.arange(lo, hi, 0.02)[:use.shape[1]]
            pools = sorted({p for v in pool_at.values() for _, p in v})
            pool_use = {p: np.zeros(use.shape[1]) for p in pools}
            pool_wait = {p: np.zeros(use.shape[1]) for p in pools}
            for i, s in enumerate(run["series"]):
                for j, (t0, p) in enumerate(sorted(pool_at.get(s, []))):
                    t1 = sorted(pool_at[s])[j + 1][0] if j + 1 < len(pool_at[s]) else np.inf
                    m = (grid >= t0) & (grid < t1)
                    pool_use[p] += np.where(m, use[i], 0.0)
                    pool_wait[p] += np.where(m, some[i], 0.0)
            hot = [(pool_use[p] >= len(cpu_list(p)) - 0.5) & (pool_wait[p] > 0.01) for p in pools]
            r.update({"contended_time_per_pool": float(np.mean([h.mean() for h in hot])),
                      "wait_s_per_s": float(sum(pool_wait[p].sum() for p in pools) / span)})
        rows.append(r)
        print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}), flush=True)
    (LOCAL / f"{args.runs}-analysis.json").write_text(json.dumps(rows, indent=1))


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
    p.add_argument("--runs", default="runs", help="output directory under .local/real")
    p.add_argument("--only", nargs="+", help="condition names, e.g. none-k4 cap1-k16")
    p.add_argument("--max-load", type=float, default=4.0, help="wait while the host 1-min load is above this")
    p.add_argument("--governor", default="performance",
                   help="refuse to run unless the workload CPUs and siblings use this governor ('' skips the check)")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("analyze")
    p.add_argument("--runs", default="runs")
    p.add_argument("--reference", help="runs directory to take the K=4 conditions from when --runs has none")
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("simulate")
    p.add_argument("--runs", default="runs")
    p.add_argument("--seeds", type=int, default=4)
    p.add_argument("--ramp-s", type=float, default=120)
    p.add_argument("--duration-s", type=float, default=1200)
    p.set_defaults(func=cmd_simulate)
    p = sub.add_parser("handoff")
    p.add_argument("--runs", default="runs")
    p.add_argument("--bin-s", type=float, default=0.02, help="container CPU is sampled every 20 ms")
    p.add_argument("--merge-s", type=float, default=0.1, help="join episodes separated by a shorter gap")
    p.add_argument("--reference", help="runs directory to take none-k4 from when --runs has none")
    p.add_argument("--delays", type=float, nargs="+", default=[0.02, 0.06, 0.25, 1.0, 5.0, 30.0],
                   help="mover reaction + move time, s (0.06: 50 ms detection + 10 ms hand-off)")
    p.set_defaults(func=cmd_handoff)
    p = sub.add_parser("pools-run", help="two 15-core pools: static, mover at several delays, one shared pool")
    p.add_argument("--runs", default="runs-pools")
    p.add_argument("--only", nargs="+", help="conditions, e.g. static move-0.06s")
    p.add_argument("--k", type=int, default=POOL_K, help="sandboxes running at once across both pools")
    p.add_argument("--limit", type=int, help="replay only the first N traces of the order (smoke test)")
    p.add_argument("--max-load", type=float, default=4.0)
    p.set_defaults(func=cmd_pools_run)
    p = sub.add_parser("pools-analyze")
    p.add_argument("--runs", default="runs-pools")
    p.add_argument("--reference", default="runs-perf", help="runs directory whose none-k4 is the per-command reference")
    p.set_defaults(func=cmd_pools_analyze)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
