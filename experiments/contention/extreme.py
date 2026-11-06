"""Extreme-load motivation runs on bud05 NUMA 0: two 15-core pools (CPUs 2-16, 17-31) stand for two nodes, as in the
real_cap.py two-pool runs. Each sandbox is a Docker container replaying one trace (`experiment.py`, recorded LLM gaps).

- load: K sandboxes on the two pools, 5K traces in a shuffled order (the 48 of traces.json), K = 90, 120, 150. At the
  K=60 per-sandbox demand this is about 60, 80 and 100% of the 30 cores.
- tsunami: K=40 background sandboxes plus four waves. Each wave is G=8 copies of one wide trace started together
  (a GRPO group, the same trace replayed 8 times). The waves go into one pool (local), four per pool (spread), or
  everything shares one 30-core pool.
- mover (real_cap.Mover): rule "gain" moves a starved sandbox to the pool where it would get >= 1.5x the cores, busy or
  not; rule "free" only to a pool with >= 4 idle cores. Reaction 0.06 s (hand-off) or 5 s (migration). A moved sandbox
  stays put 5 s, or 30 s in the -hold30 conditions (the 0.06 s gain mover moved some sandboxes back and forth).

Each run waits until the host 1-min load is below 4 and refuses to start unless the governor is performance.
Raw results go to .local/real/runs-extreme/ (Git-ignored); `analyze` writes runs-extreme-analysis.json there too.
"""
import argparse
import collections
import csv
import json
import math
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_cap as rc  # noqa: E402

RUNS = rc.LOCAL / "runs-extreme"
CONFIG = rc.REPO / "harness/trace-replay/configs/contention-dense.json"  # contention.json + controller 0-1,64-65
CONTROLLER = "0-1,64-65"
UNION = ",".join(rc.POOLS)
WAVE_G = 8
WAVE_TRACES = ["gohugoio__hugo-12579", "fmtlib__fmt-1683", "preactjs__preact-2757", "caddyserver__caddy-6370"]
WAVE_AT_S = [60, 420, 780, 1140]  # after the background starts; each wave runs 180-210 s alone
TSUNAMI_K, TSUNAMI_TRACES = 40, 320  # keeps 40 background sandboxes running for about 30 min

# name, K, waves (None, "local", "spread"), shared pool, mover rule, mover delay (s)[, hold (s)]; in priority order
CONDITIONS = [
    ("tsunami-local-static", 40, "local", False, None, None),
    ("tsunami-local-gain", 40, "local", False, "gain", 0.06),
    ("tsunami-shared", 40, "local", True, None, None),
    ("tsunami-spread-static", 40, "spread", False, None, None),
    ("tsunami-local-gain-5s", 40, "local", False, "gain", 5.0),
    ("k120-static", 120, None, False, None, None),
    ("k120-gain", 120, None, False, "gain", 0.06),
    ("k120-shared", 120, None, True, None, None),
    ("k150-static", 150, None, False, None, None),
    ("k150-gain", 150, None, False, "gain", 0.06),
    ("k150-shared", 150, None, True, None, None),
    ("k120-gain-5s", 120, None, False, "gain", 5.0),
    ("k120-free", 120, None, False, "free", 0.06),
    ("k90-static", 90, None, False, None, None),
    ("k90-gain", 90, None, False, "gain", 0.06),
    ("k90-shared", 90, None, True, None, None),
    ("k120-static-r2", 120, None, False, None, None),
    ("k120-gain-r2", 120, None, False, "gain", 0.06),
    ("tsunami-local-free", 40, "local", False, "free", 0.06),
    ("tsunami-local-gain-hold30", 40, "local", False, "gain", 0.06, 30.0),
    ("k120-gain-hold30", 120, None, False, "gain", 0.06, 30.0),
    # 11-06: "gain" needs one core more, which at 95% use means 2x; "ratio" drops that, "balance" evens waiting counts
    ("k150-ratio", 150, None, False, "ratio", 0.06),
    ("k150-balance", 150, None, False, "balance", 0.06),
    ("k120-ratio", 120, None, False, "ratio", 0.06),
    ("k120-balance", 120, None, False, "balance", 0.06),
]
# 11-06: resources other than CPU time. The k120-static setup with 3K traces (about 12 min) and per-pool L3 occupancy,
# memory traffic and LLC misses. "mon" leaves socket 0's L3 shared by both pools; "split" gives each pool 6 private ways.
LLC_CONDITIONS = [("llc-k120-shared", 120, "mon"), ("llc-k120-split", 120, "split")]
LLC_TRACES = 360


def wave_dirs(name):
    return [RUNS / f"{name}.wave{i}" for i in range(len(WAVE_AT_S))]


def order_for(k, waves):
    """The same shuffled arrival order for every condition with this K (and for the tsunami background)."""
    traces = [t["trace"] for t in json.loads((rc.LOCAL / "traces.json").read_text())]
    n = TSUNAMI_TRACES if waves else 5 * k
    order = traces * math.ceil(n / len(traces))
    random.Random("tsunami" if waves else f"k{k}").shuffle(order)
    return order[:n]


def harness(traces, out, k, placement):
    return [sys.executable, str(rc.EXPERIMENT), *[str(rc.REPO / t) for t in traces], "--config", str(CONFIG),
            "--output", str(out), "--schedule", "gaps", "--starts", "together", "--setup", "on-arrival",
            "--max-active", str(k), "--keep-going", "--command-timeout", "600", *placement]


def run_one(name, k, waves, shared, rule, delay, args, hold=rc.MOVE_HOLD_S, l3=None, n=None):
    out = RUNS / name
    if rc.finished(out) and all(rc.finished(d) for d in (wave_dirs(name) if waves else [])):
        print(f"{name}: done, skipped", flush=True)
        return
    stamp = int(time.time())
    for d in [out, *wave_dirs(name)]:
        if d.exists():
            d.rename(d.with_name(f"{d.name}-failed-{stamp}"))
    while float(Path("/proc/loadavg").read_text().split()[0]) > args.max_load:
        print(f"{name}: waiting for host load to drop", flush=True)
        time.sleep(60)
    cpus_g = rc.governor_cpus(UNION)
    governors = sorted({rc.governor_path(c).read_text().strip() for c in cpus_g})
    if governors != ["performance"]:
        raise SystemExit(f"{name}: governor on CPUs {cpus_g} is {governors}, not performance")
    pools = (UNION,) if shared else rc.POOLS
    spread = ["--workload-cpus", UNION] if shared else ["--pools", *rc.POOLS]
    order = order_for(k, waves)[:n]
    print(f"{name}: {len(order)} traces, K={k}, waves {waves}, shared {shared}, rule {rule}, delay {delay}, L3 {l3}",
          flush=True)
    stop = threading.Event()
    groups = rc.resctrl_pools(rc.POOLS, l3 == "split") if l3 else ()
    sampler = threading.Thread(target=rc.host_sampler, args=(RUNS / f"{name}-host.csv", stop, UNION, groups))
    mover = rc.Mover(pools, delay, RUNS / f"{name}-moves.csv", stop, rule=rule or "free", cpus=CONTROLLER, hold=hold)
    sampler.start()
    mover.start()
    started = time.time()
    background = subprocess.Popen(harness(order, out, k, spread), cwd=rc.REPO)
    launched = []
    try:
        for i, at in enumerate(WAVE_AT_S if waves else []):
            while time.time() < started + at and background.poll() is None:
                time.sleep(0.2)
            if background.poll() is not None:
                print(f"{name}: background ended before wave {i}", flush=True)
                break
            place = (["--workload-cpus", UNION] if shared else ["--workload-cpus", rc.POOLS[0]] if waves == "local"
                     else ["--pools", *rc.POOLS])
            trace = next(t["trace"] for t in json.loads((rc.LOCAL / "traces.json").read_text())
                         if t["name"] == WAVE_TRACES[i % len(WAVE_TRACES)])
            launched.append(subprocess.Popen(harness([trace] * WAVE_G, wave_dirs(name)[i], WAVE_G, place), cwd=rc.REPO,
                                             stdout=subprocess.DEVNULL))
            print(f"{name}: wave {i} ({WAVE_TRACES[i % len(WAVE_TRACES)]} x{WAVE_G}) at {time.time() - started:.0f} s",
                  flush=True)
        codes = [background.wait(), *[p.wait() for p in launched]]
    finally:
        for p in [background, *launched]:
            if p.poll() is None:
                p.terminate()
                p.wait()
        stop.set()
        sampler.join()
        mover.join()
    print(f"{name}: exit {codes}, {time.time() - started:.0f} s", flush=True)


def cmd_run(args):
    RUNS.mkdir(parents=True, exist_ok=True)
    for name, k, waves, shared, rule, delay, *hold in CONDITIONS:
        if not args.only or name in args.only:
            run_one(name, k, waves, shared, rule, delay, args, *hold)


def cmd_llc(args):
    RUNS.mkdir(parents=True, exist_ok=True)
    for name, k, l3 in LLC_CONDITIONS:
        if not args.only or name in args.only:
            try:
                run_one(name, k, None, False, None, None, args, l3=l3, n=LLC_TRACES)
            finally:
                rc.resctrl_clear(len(rc.POOLS))


def start_unix(directory):
    return next(json.loads(line)["wall_time_unix"] for line in (directory / "events.jsonl").open()
                if '"replay_clock_start"' in line)


def pool_grid(instances, pools, log, lo, hi, bin_s=0.02):
    """Per pool and bin (unix time): cores used, containers' PSI cpu-some seconds, and per container the cores used."""
    grid = np.arange(lo, hi, bin_s)
    where = collections.defaultdict(list)
    if len(pools) == 1:
        for run, t0 in instances:
            for s in run["series"]:
                where[s].append((-np.inf, pools[0]))
    else:
        for m in log:
            if m["event"] in ("seen", "move"):
                where[m["container"]].append((float(m["t_unix"]), m["to_pool"] or m["from_pool"]))
    use = {p: np.zeros(len(grid) - 1) for p in pools}
    some = {p: np.zeros(len(grid) - 1) for p in pools}
    per = {}
    for run, t0 in instances:
        for s, a in run["series"].items():
            if len(a) < 2:
                continue
            t = a[:, 0] + t0
            inside = (grid >= t[0]) & (grid <= t[-1])
            inside = inside[:-1] & inside[1:]
            u = np.where(inside, np.diff(np.interp(grid, t, a[:, 1])), 0.0) / 1e6 / bin_s
            w = np.where(inside, np.diff(np.interp(grid, t, a[:, 3])), 0.0) / 1e6
            per[s] = u
            spans = sorted(where.get(s, []))
            for j, (a0, p) in enumerate(spans):
                a1 = spans[j + 1][0] if j + 1 < len(spans) else np.inf
                m = (grid[:-1] >= a0) & (grid[:-1] < a1)
                use[p] += np.where(m, u, 0.0)
                some[p] += np.where(m, w, 0.0)
    return grid[:-1], use, some, per


def command_ratios(run, ref, wide, lo, hi):
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
    if not both:
        return {}
    heavy = np.array([c["wall"] / b["wall"] for c, b in both if b["cpu"] >= 1.0])
    total = lambda ps: sum(c["wall"] for c, _ in ps) / sum(b["wall"] for _, b in ps) if ps else None
    return {"commands": len(both), "tool_total": total(both), "tool_total_wide": total(pairs[True]),
            "tool_total_narrow": total(pairs[False]),
            "cpu_weighted": sum(b["cpu"] * c["wall"] / b["wall"] for c, b in both if b["wall"] > 0)
            / sum(b["cpu"] for c, b in both if b["wall"] > 0),
            "cpu_inflation": sum(c["cpu"] for c, _ in both) / sum(b["cpu"] for _, b in both),
            "heavy_p50": float(np.median(heavy)) if len(heavy) else None,
            "heavy_p90": float(np.percentile(heavy, 90)) if len(heavy) else None,
            "heavy_p99": float(np.percentile(heavy, 99)) if len(heavy) else None}


def analyze_one(name, k, waves, shared, rule, delay, ref, wide, solo, hold=rc.MOVE_HOLD_S):
    out = RUNS / name
    run = rc.load_run(out)
    t0 = start_unix(out)
    lo, hi = run["window"]
    r = {"condition": name, "k": k, "waves": waves, "shared": shared, "rule": rule, "delay_s": delay, "hold_s": hold,
         "window_s": hi - lo, "failed_sessions": (out / "events.jsonl").read_text().count('"session_failed"')}
    r.update({f"bg_{key}": v for key, v in command_ratios(run, ref, wide, lo, hi).items()})
    # rollouts that started while K ran, against the same trace alone (K=4); failed sessions ended early
    failed = {json.loads(line)["session"] for line in (out / "events.jsonl").open() if '"session_failed"' in line}
    lives = np.array([(e - s) / solo[t] for name_, (t, s, e) in run["sessions"].items()
                      if name_ not in failed and t in solo and lo <= s <= hi])
    if len(lives):
        r.update({"bg_rollouts": len(lives), "bg_rollout_mean": float(lives.mean()),
                  **{f"bg_rollout_p{q}": float(np.percentile(lives, q)) for q in (50, 90, 99)}})
    instances = [(run, t0)]
    wave_rows = []
    for i, d in enumerate(wave_dirs(name) if waves else []):
        if not (d / "events.jsonl").exists():
            continue
        w = rc.load_run(d)
        instances.append((w, start_unix(d)))
        spans = [(s, e) for _, s, e in w["sessions"].values()]
        trace = WAVE_TRACES[i % len(WAVE_TRACES)]
        lives = [e - s for s, e in spans]
        row = {"wave": i, "trace": trace, "done": len(spans), "solo_s": solo.get(trace),
               "makespan_s": max(e for _, e in spans) - min(s for s, _ in spans) if spans else None,
               "rollout_mean_s": float(np.mean(lives)) if lives else None}
        if row["makespan_s"] and row["solo_s"]:
            row["makespan_ratio"] = row["makespan_s"] / row["solo_s"]
            row["rollout_mean_ratio"] = row["rollout_mean_s"] / row["solo_s"]
        row.update(command_ratios(w, ref, wide, -np.inf, np.inf))
        wave_rows.append(row)
    if wave_rows:
        r["waves_detail"] = wave_rows
        done = [w for w in wave_rows if "makespan_ratio" in w]
        r["wave_makespan_ratio_mean"] = float(np.mean([w["makespan_ratio"] for w in done])) if done else None
        r["wave_makespan_ratio_max"] = float(max(w["makespan_ratio"] for w in done)) if done else None
        r["wave_tool_total_mean"] = float(np.mean([w["tool_total"] for w in done if w.get("tool_total")])) if done else None
    log = list(csv.DictReader((RUNS / f"{name}-moves.csv").open()))
    pools = (UNION,) if shared else rc.POOLS
    grid, use, some, per = pool_grid(instances, pools, log, t0 + lo, t0 + hi)
    hot = {p: (use[p] >= len(rc.cpu_list(p)) - 0.5) & (some[p] > 0.01) for p in pools}
    span = hi - lo
    r.update({"util": sum(u.sum() for u in use.values()) * 0.02 / (len(rc.cpu_list(UNION)) * span),
              "contended_time_per_pool": float(np.mean([h.mean() for h in hot.values()])),
              "wait_s_per_s": float(sum(s.sum() for s in some.values()) / span)})
    if len(pools) == 2:
        a, b = (hot[p] for p in pools)
        r["both_hot"] = float((a & b).mean())
        # idle cores in a pool while the other pool is contended: capacity a mover could use
        idle = np.where(a & ~b, len(rc.cpu_list(pools[1])) - use[pools[1]], 0.0) + np.where(
            b & ~a, len(rc.cpu_list(pools[0])) - use[pools[0]], 0.0)
        r["stranded_cores_mean"] = float(np.clip(idle, 0, None).mean())
        r["wait_share_one_hot"] = float(sum(np.where(a ^ b, some[p], 0).sum() for p in pools)
                                        / max(1e-9, sum(some[p].sum() for p in pools)))
    made = [m for m in log if m["event"] == "move" and t0 + lo <= float(m["t_unix"]) <= t0 + hi]
    r.update({"moves": len(made), "moves_per_min": len(made) / (span / 60),
              "moves_failed": sum(m["event"] == "move_failed" for m in log),
              "move_latency_p50_s": float(np.median([float(m["latency_s"]) for m in made])) if made else None})
    if made:
        wave_names = {s for w, _ in instances[1:] for s in w["series"]}
        gains, dest_hot = [], 0
        for m in made:
            i = int((float(m["t_unix"]) - grid[0]) / 0.02)
            u = per.get(m["container"])
            if u is not None and 50 <= i < len(u) - 60:
                before, after = u[i - 50:i].mean(), u[i + 10:i + 60].mean()
                if before > 0.05:
                    gains.append(after / before)
            dest_hot += bool(hot[m["to_pool"]][i:i + 100].any()) if m["to_pool"] in hot else 0
        r.update({"moved_wave_share": sum(m["container"] in wave_names for m in made) / len(made),
                  "move_cpu_gain_p50": float(np.median(gains)) if gains else None,
                  "move_cpu_gain_p25": float(np.percentile(gains, 25)) if gains else None,
                  "move_dest_hot_within_2s": dest_hot / len(made)})
    r.update(rc.host_stats(out, name, run["window"], 0.0))
    return r


def cmd_analyze(args):
    traces = json.loads((rc.LOCAL / "traces.json").read_text())
    wide = {t["name"] for t in traces if t["peak_cores_100ms"] >= 8}
    ref = rc.load_run(rc.LOCAL / args.reference / rc.cond_name("none", 4))
    solo = {t: e - s for t, s, e in ref["sessions"].values()}
    rows = []
    for spec in CONDITIONS:
        if (args.only and spec[0] not in args.only) or not rc.finished(RUNS / spec[0]):
            continue
        r = analyze_one(*spec[:6], ref, wide, solo, *spec[6:])
        rows.append(r)
        print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k != "waves_detail"}),
              flush=True)
        for w in r.get("waves_detail", []):
            print("   ", json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in w.items()
                                     if k in ("wave", "trace", "done", "solo_s", "makespan_s", "makespan_ratio",
                                              "rollout_mean_ratio", "tool_total")}), flush=True)
    path = rc.LOCAL / "runs-extreme-analysis.json"
    old = {r["condition"]: r for r in json.loads(path.read_text())} if path.exists() and args.only else {}
    old.update({r["condition"]: r for r in rows})
    path.write_text(json.dumps([old[s[0]] for s in CONDITIONS if s[0] in old], indent=1))


PERF_EVENTS = ("instructions", "cycles", "ref-cycles", "cache-references", "cache-misses")


def pool_seconds(name):
    """Per pool and second (unix time): perf counts summed over the pool's CPUs; the pool's resctrl L3 occupancy (MiB)
    and memory traffic (GB/s, total and local); and socket 0 traffic outside the pool groups (GB/s)."""
    start = json.loads((RUNS / f"{name}-host.perf.json").read_text())["start_unix"]
    pool_of = {c: i for i, p in enumerate(rc.POOLS) for c in rc.cpu_list(p)}
    counts = collections.defaultdict(lambda: collections.defaultdict(float))
    for line in (RUNS / f"{name}-host.perf.csv").read_text().splitlines():
        parts = line.split(",")
        if len(parts) > 4 and parts[1].startswith("CPU") and parts[2].isdigit() and int(parts[1][3:]) in pool_of:
            counts[start + float(parts[0])][(pool_of[int(parts[1][3:])], parts[4])] += float(parts[2])
    perf_t = np.array(sorted(counts))
    perf = {(i, e): np.array([counts[t][(i, e)] for t in perf_t]) for i in range(len(rc.POOLS)) for e in PERF_EVENTS}
    rows = list(csv.DictReader((RUNS / f"{name}-host.csv").open()))
    t = np.array([float(r["t_unix"]) for r in rows])
    res = {"rest": np.diff([float(r["mbm_total_bytes"]) for r in rows]) / np.diff(t) / 1e9}
    for i in range(len(rc.POOLS)):
        res[(i, "occupancy_mib")] = np.array([float(r[f"g{i}_llc_occupancy"]) for r in rows])[1:] / 2**20
        for f in ("mbm_total_bytes", "mbm_local_bytes"):
            res[(i, f)] = np.diff([float(r[f"g{i}_{f}"]) for r in rows]) / np.diff(t) / 1e9
    return perf_t, perf, t[1:], res


def psi_rates(directory, lo, hi):
    """Containers' PSI 'some' time per second of the window (sum over containers), for cpu, memory and io."""
    cols = {"cpu": "cpu_pressure_some_usec", "memory": "memory_pressure_some_usec", "io": "io_pressure_some_usec"}
    series = collections.defaultdict(list)
    with (directory / "metrics.csv").open() as stream:
        for r in csv.DictReader(stream):
            if r["session"] and r["cpu_usec"]:
                series[r["session"]].append([float(r["elapsed_s"])] + [float(r[c] or 0) for c in cols.values()])
    total = np.zeros(len(cols))
    for rows in series.values():
        a = np.array(sorted(rows))
        a0, a1 = max(lo, a[0, 0]), min(hi, a[-1, 0])
        if a1 > a0:
            total += [np.interp(a1, a[:, 0], a[:, j]) - np.interp(a0, a[:, 0], a[:, j]) for j in range(1, len(cols) + 1)]
    return {f"psi_{k}_s_per_s": float(v / 1e6 / (hi - lo)) for k, v in zip(cols, total)}


def llc_one(name, l3, lo, hi):
    """Per pool over the window [lo, hi] (unix): clock, IPC, LLC misses, L3 occupancy and memory traffic. The pools run
    independent work (their loads are uncorrelated), so this pool's misses and IPC against how busy the other pool was
    in the same second show interference across pools; against its own load they mostly show which commands ran."""
    perf_t, perf, res_t, res = pool_seconds(name)
    pin, rin = (perf_t >= lo) & (perf_t <= hi), (res_t >= lo) & (res_t <= hi)
    # the default group's count includes its monitor groups ("mon") but not the pools' control groups ("split")
    socket = res["rest"] + (sum(res[(i, "mbm_total_bytes")] for i in range(len(rc.POOLS))) if l3 == "split" else 0)
    out = {"socket_gbs": float(socket[rin].mean())}
    for i in range(len(rc.POOLS)):
        ins, cyc, ref, refs, miss = (perf[(i, e)][pin] for e in PERF_EVENTS)
        other = perf[(1 - i, "ref-cycles")][pin] / (rc.TSC_GHZ * 1e9)  # cores the other pool kept busy
        q1, q3 = np.percentile(other, [25, 75])
        low, high = other <= q1, other >= q3
        occ, bw, loc = (res[(i, f)][rin] for f in ("occupancy_mib", "mbm_total_bytes", "mbm_local_bytes"))
        out[f"pool{i}"] = {
            "busy_cores": float((ref / (rc.TSC_GHZ * 1e9)).mean()), "ghz": cyc.sum() / ref.sum() * rc.TSC_GHZ,
            "ipc": ins.sum() / cyc.sum(), "llc_mpki": 1000 * miss.sum() / ins.sum(), "llc_miss_ratio": miss.sum() / refs.sum(),
            "occupancy_mib_p10_p50_p90": [float(np.percentile(occ, q)) for q in (10, 50, 90)],
            "mem_gbs_mean_p99_max": [float(bw.mean()), float(np.percentile(bw, 99)), float(bw.max())],
            "mem_local_share": float(loc.sum() / bw.sum()),
            "other_busy_q1_q3": [float(q1), float(q3)],
            "mpki_other_low_high": [1000 * miss[m].sum() / ins[m].sum() for m in (low, high)],
            "ipc_other_low_high": [ins[m].sum() / cyc[m].sum() for m in (low, high)],
            "ghz_other_low_high": [cyc[m].sum() / ref[m].sum() * rc.TSC_GHZ for m in (low, high)]}
    return out


def cmd_llc_analyze(args):
    traces = json.loads((rc.LOCAL / "traces.json").read_text())
    wide = {t["name"] for t in traces if t["peak_cores_100ms"] >= 8}
    ref_dir = rc.LOCAL / args.reference / rc.cond_name("none", 4)
    ref = rc.load_run(ref_dir)
    solo = {t: e - s for t, s, e in ref["sessions"].values()}
    rows = [{"condition": "reference-k4", **rc.host_stats(ref_dir, rc.cond_name("none", 4), None, 0.0)}]
    for name, k, l3 in LLC_CONDITIONS:
        out = RUNS / name
        if not rc.finished(out):
            continue
        r = analyze_one(name, k, None, False, None, None, ref, wide, solo)
        r = {key: r[key] for key in ("condition", "window_s", "util", "wait_s_per_s", "bg_cpu_weighted", "bg_cpu_inflation",
                                     "bg_tool_total", "bg_heavy_p50") if key in r}
        run = rc.load_run(out)
        lo, hi = run["window"]
        t0 = start_unix(out)
        r.update({"l3": l3, **psi_rates(out, lo, hi), **llc_one(name, l3, t0 + lo, t0 + hi)})
        rows.append(r)
    for r in rows:
        print(json.dumps(r, default=float), flush=True)
    (rc.LOCAL / "runs-llc-analysis.json").write_text(json.dumps(rows, indent=1, default=float))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--only", nargs="+", help="condition names")
    p.add_argument("--max-load", type=float, default=4.0)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("llc", help="run LLC_CONDITIONS")
    p.add_argument("--only", nargs="+", help="condition names")
    p.add_argument("--max-load", type=float, default=4.0)
    p.set_defaults(func=cmd_llc)
    p = sub.add_parser("llc-analyze")
    p.add_argument("--reference", default="runs-perf", help="runs directory whose none-k4 is the per-command reference")
    p.set_defaults(func=cmd_llc_analyze)
    p = sub.add_parser("analyze")
    p.add_argument("--only", nargs="+")
    p.add_argument("--reference", default="runs-perf", help="runs directory whose none-k4 is the per-command reference")
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("plan")
    p.set_defaults(func=lambda a: [print(s[0], len(order_for(s[1], s[2])), s[2:]) for s in CONDITIONS])
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
