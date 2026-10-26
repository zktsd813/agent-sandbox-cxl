#!/usr/bin/env python3
"""Fleet-scale version of sim.py: the same trace-driven node model, compiled with numba for DSec-sized clusters.

Model (identical to sim.py where they overlap; `test` checks it):
- A sandbox replays a program from its solo run: per-command vCPU demand in 10 ms slots (cores, at most its vCPU
  count) and the trace's recorded LLM waits. The first CPU segment is the boot; later CPU segments are commands and
  are cut at 60 s like the guest agent does.
- A node shares C cores like CFS: a sandbox with demand d runs as ceil(d) equal threads; every runnable thread gets
  the same share up to its demand (water-filling). Unserved demand is runqueue wait and stretches the command.
- Workloads: `slots` (fixed sandboxes per node, as in the pilot), `fleet` (slots float; each new sandbox goes to the
  less loaded of k sampled nodes, DSec power-of-k).
- Moves (split nodes only): when a node's excess demand, averaged over the detection window, reaches the threshold,
  up to ceil(excess) of its busiest sandboxes move to nodes that have an idle core. Migration dumps guest memory with
  one source core (stop-and-copy pauses the sandbox until it arrives; pre-copy keeps it running), then sends it
  over the node's NIC one migration at a time. Hand-off moves a small state with a fixed latency and no copy.
"""

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor
import json
import math
from pathlib import Path
import random
import statistics
import time

import numpy as np
from numba import njit

HERE = Path(__file__).resolve().parent
DT = 0.010
SANDBOX_GAP_S = 0.5
TIMEOUT_S = 60

# Experiment 3 (nested lab, TAP ~37 Gb/s): stop-and-copy dump ~0.30 s/GiB of one core, transfer ~0.39 s/GiB.
MECHANISMS = {
    "none": dict(kind=0),
    "instant": dict(kind=1, latency_s=0.0, cpu_s=0.0),
    "hand-off 1ms": dict(kind=1, latency_s=0.001, cpu_s=0.001),
    "hand-off 10ms": dict(kind=1, latency_s=0.010, cpu_s=0.002),
    "migration stop-and-copy": dict(kind=2, pause=True, dump_s_per_gib=0.30, xfer_s_per_gib=0.39, nic_gbps=37.0),
    "migration pre-copy": dict(kind=2, pause=False, dump_s_per_gib=0.30, xfer_s_per_gib=0.39, nic_gbps=37.0),
}


# Kernel scheduling wait the fluid share leaves out (RESULTS-fleet.md 7절): wait/run = a(P) * exp(b * util) for a
# pool of P cores, fitted per mode to the 1 vCPU pilot runs at P = 5, 10, 20 that did not diverge from their solo
# runs, at matched utilization (pilot_audit.py, kernel_wait_fit.py). Beyond the measured sizes "slope" extends the
# log-log fit of a over P, "floor" keeps a at its 20-core value.
KERNEL_WAIT = {"capacity": {"b": 6.0, "a": {5: 5.129e-05, 10: 6.313e-05, 20: 8.215e-05}, "slope": 0.340},
               "latency": {"b": 6.0, "a": {5: 7.827e-05, 10: 8.620e-05, 20: 1.042e-04}, "slope": 0.207}}


def kernel_wait_a(config, pool_cores):
    """Per-pool coefficient a for config["kernel_wait"]: none, fit (config["kernel_wait_a"]: {P: a} or one a),
    slope or floor."""
    variant = config.get("kernel_wait", "none")
    out = np.zeros(len(pool_cores))
    if variant == "none":
        return out
    if variant == "fit":
        given = config["kernel_wait_a"]
        for i, size in enumerate(pool_cores):
            out[i] = given[str(int(size))] if isinstance(given, dict) else float(given)
        return out
    fit = KERNEL_WAIT[config.get("kernel_wait_mode", "capacity")]
    known = sorted(fit["a"])
    for i, size in enumerate(pool_cores):
        size = float(size)
        if size in known:
            out[i] = fit["a"][int(size)]
        elif size > known[-1]:
            top = fit["a"][known[-1]]
            out[i] = top if variant == "floor" else top * (size / known[-1]) ** fit["slope"]
        else:
            lo = max(k for k in known if k < size) if size > known[0] else known[0]
            hi = min(k for k in known if k > size) if size < known[-1] else known[-1]
            if lo == hi:
                out[i] = fit["a"][lo] * (size / lo) ** fit["slope"]
            else:
                f = math.log(size / lo) / math.log(hi / lo)
                out[i] = math.exp((1 - f) * math.log(fit["a"][lo]) + f * math.log(fit["a"][hi]))
    return out


def usable(summary, max_mismatch=0.10):
    """A solo replay the model may use: it finished and few commands diverged from the recorded trace."""
    checked = summary.get("returncode_checked") or 0
    return checked > 0 and summary.get("returncode_mismatches", 0) <= max_mismatch * checked


def load_programs(solo_dir, candidates_path, max_mismatch=0.10):
    """Programs from a solo run: [("cpu", demand per 10ms in cores) | ("gap", s)], plus vCPUs and guest memory."""
    entries = {t["name"]: t for t in json.loads(Path(candidates_path).read_text())["traces"]}
    run_config = json.loads((Path(solo_dir) / "run.json").read_text())
    vcpus = int(run_config.get("vcpus", 1))
    programs = {}
    for name, entry in entries.items():
        directory = Path(solo_dir) / name
        if not (directory / "summary.json").exists() or not usable(json.loads((directory / "summary.json").read_text()),
                                                                     max_mismatch):
            continue
        samples = {}
        launch = None
        with (directory / "threads.csv").open() as stream:
            for row in csv.DictReader(stream):
                t = int(row["t_ns"])
                launch = t if launch is None else launch
                if row["comm"].startswith("fc_vcpu"):
                    samples[t] = samples.get(t, 0) + int(row["run_ns"])
        times = np.array(sorted(samples), dtype=float)
        runs = np.array([samples[t] for t in sorted(samples)], dtype=float)
        commands = list(csv.DictReader((directory / "commands.csv").open()))

        def slots(begin, end):
            edges = np.arange(begin, end + DT * 1e9, DT * 1e9)
            if len(edges) < 2:
                edges = np.array([begin, begin + DT * 1e9])
            cumulative = np.interp(edges, times, runs, left=0.0)
            return np.clip(np.diff(cumulative) / (DT * 1e9), 0.0, vcpus)

        starts = [int(c["host_start_ns"]) for c in commands]
        ends = [int(c["host_end_ns"]) for c in commands]
        program = [("cpu", slots(launch, starts[0]))]
        for i, command in enumerate(commands):
            if i and float(command["gap_s"]) > 0:
                program.append(("gap", float(command["gap_s"])))
            program.append(("cpu", slots(starts[i], starts[i + 1] if i + 1 < len(commands) else ends[i])))
        programs[name] = {"program": program, "vcpus": vcpus,
                          "memory_gib": entry.get("memory_mib", 2048) / 1024}
    return programs


def flatten(programs, names):
    """Arrays for the compiled loop; trace i is names[i]."""
    demand, kind, start, length, first, count, vcpus, memory, cpu_s, gap_s = [], [], [], [], [], [], [], [], [], []
    for name in names:
        info = programs[name]
        first.append(len(kind))
        segments = 0
        busy = idle = 0.0
        for k, value in info["program"]:
            if k == "cpu":
                if not len(value):
                    continue
                kind.append(0)
                start.append(len(demand))
                length.append(len(value))
                demand.extend(value.tolist())
                busy += len(value) * DT
            else:
                kind.append(1)
                start.append(0)
                length.append(max(1, math.ceil(value / DT - 1e-9)))
                idle += value
            segments += 1
        count.append(segments)
        vcpus.append(info["vcpus"])
        memory.append(info["memory_gib"])
        cpu_s.append(busy)
        gap_s.append(idle)
    return dict(slot_demand=np.array(demand, dtype=np.float64), seg_kind=np.array(kind, dtype=np.int8),
                seg_start=np.array(start, dtype=np.int64), seg_len=np.array(length, dtype=np.int64),
                trace_first=np.array(first, dtype=np.int64), trace_count=np.array(count, dtype=np.int64),
                trace_vcpus=np.array(vcpus, dtype=np.float64), trace_mem=np.array(memory, dtype=np.float64),
                trace_cpu_s=np.array(cpu_s, dtype=np.float64), trace_gap_s=np.array(gap_s, dtype=np.float64),
                trace_expected=np.array(cpu_s, dtype=np.float64) + np.array(gap_s, dtype=np.float64))


@njit(cache=True)
def _water_fill(units, threads, capacity):
    """Equal share per thread up to each thread's demand; returns per-entry served cores (entry = threads x unit)."""
    n = units.shape[0]
    served = np.zeros(n)
    total = 0.0
    for i in range(n):
        total += units[i] * threads[i]
    if total <= capacity:
        for i in range(n):
            served[i] = units[i] * threads[i]
        return served
    order = np.argsort(units)
    left = capacity
    remaining = 0.0
    for i in range(n):
        remaining += threads[i]
    for j in range(n):
        i = order[j]
        level = left / remaining
        if units[i] <= level:
            served[i] = units[i] * threads[i]
        else:
            served[i] = level * threads[i]
        left -= served[i]
        remaining -= threads[i]
    return served


@njit(cache=True)
def _expected_excess(probs, deterministic, capacity):
    """E[max(0, S - capacity)] for S = deterministic + sum of independent Bernoulli(probs[i]) running threads.
    Exact Poisson-binomial for small pools, normal approximation for large ones."""
    n = probs.shape[0]
    mean = deterministic
    var = 0.0
    for i in range(n):
        mean += probs[i]
        var += probs[i] * (1.0 - probs[i])
    if n <= 256:
        dist = np.zeros(n + 1)
        dist[0] = 1.0
        for i in range(n):
            q = probs[i]
            for k in range(i + 1, 0, -1):
                dist[k] = dist[k] * (1.0 - q) + dist[k - 1] * q
            dist[0] *= 1.0 - q
        excess = 0.0
        for k in range(n + 1):
            over = k + deterministic - capacity
            if over > 0.0:
                excess += dist[k] * over
        return excess
    if var <= 1e-12:
        return max(0.0, mean - capacity)
    sd = math.sqrt(var)
    z = (capacity + 0.5 - mean) / sd  # continuity correction for a count
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    tail = 0.5 * math.erfc(z / math.sqrt(2.0))
    return max(0.0, sd * pdf + (mean - capacity - 0.5) * tail)


@njit(cache=True)
def _vruntime_fill(v, caps, threads, budget):
    """CFS-like fluid allocation: raise the lowest vruntimes first to a common level L; entry i gets
    min(cap_i, threads_i * max(0, L - v_i)) CPU-seconds, totalling the budget (all caps when they fit)."""
    n = v.shape[0]
    got = np.zeros(n)
    total = 0.0
    for i in range(n):
        total += caps[i]
    if total <= budget:
        for i in range(n):
            got[i] = caps[i]
        return got
    lo = v.min()
    hi = lo
    for i in range(n):
        top = v[i] + caps[i] / threads[i]
        if top > hi:
            hi = top
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        used = 0.0
        for i in range(n):
            g = threads[i] * (mid - v[i])
            if g > caps[i]:
                g = caps[i]
            if g > 0.0:
                used += g
        if used > budget:
            hi = mid
        else:
            lo = mid
    used = 0.0
    for i in range(n):
        g = threads[i] * (lo - v[i])
        if g > caps[i]:
            g = caps[i]
        if g < 0.0:
            g = 0.0
        got[i] = g
        used += g
    # hand the bisection residue to entries still below their cap, in vruntime order
    rest = budget - used
    order = np.argsort(v)
    for j in range(n):
        i = order[j]
        if rest <= 0.0:
            break
        room = caps[i] - got[i]
        if room > 0.0:
            add = room if room < rest else rest
            got[i] += add
            rest -= add
    return got


@njit(cache=True)
def _enter(seg, seg_last, seg_kind, seg_len, step, gap_scale=1.0):
    """Skip empty CPU segments; returns (segment, position, gap steps left, segment start step)."""
    while seg <= seg_last:
        if seg_kind[seg] == 1:
            return seg, 0, max(1, int(round(seg_len[seg] * gap_scale))), step
        if seg_len[seg] > 0:
            return seg, 0, 0, step
        seg += 1
    return seg, 0, 0, step


@njit(cache=True)
def run_fleet(slot_demand, seg_kind, seg_start, seg_len, trace_first, trace_count, trace_vcpus, trace_mem,
              trace_expected, n_nodes, cores, pool_of_node, n_pools, slot_home, first_start, choices, place_rand,
              place_k, floating, steps, begin_step, gap_steps, timeout_steps,
              mech_kind, pause, latency_steps, handoff_cpu_s, dump_s_per_gib, xfer_s_per_gib,
              detect_steps, threshold, hold_steps, max_batch, job_size, train_steps, jitter, gap_scale,
              cfs, sleeper_credit_s, inflation, carry, bernoulli, kw_a, kw_b, kw_latency):
    n_slots = slot_home.shape[0]
    vrun = np.zeros(n_slots)      # CFS virtual runtime per sandbox (seconds of CPU per thread)
    was_runnable = np.zeros(n_slots, np.bool_)
    task_v = np.zeros(200000)
    min_v = np.zeros(n_pools)
    n_jobs = n_slots // job_size if job_size > 0 else 0
    job_done = np.zeros(max(1, n_jobs), np.int64)
    job_begin = np.zeros(max(1, n_jobs), np.int64)
    job_ideal = np.zeros(max(1, n_jobs))
    step_ratio = np.zeros(200000)
    step_secs = np.zeros(200000)
    n_steps_done = 0
    for j in range(n_jobs):
        job_begin[j] = first_start[j * job_size:(j + 1) * job_size].min()
    active = np.zeros(n_slots, np.bool_)
    tr = np.zeros(n_slots, np.int64)
    seg = np.zeros(n_slots, np.int64)
    seg_last = np.zeros(n_slots, np.int64)
    pos = np.zeros(n_slots, np.int64)
    prog = np.zeros(n_slots)
    gap_left = np.zeros(n_slots, np.int64)
    seg_t0 = np.zeros(n_slots, np.int64)
    t_start = np.zeros(n_slots, np.int64)
    node = slot_home.copy()
    paused_until = np.full(n_slots, -1, np.int64)
    locked_until = np.full(n_slots, -1, np.int64)
    recent = np.zeros(n_slots)
    next_start = first_start.copy()
    choice_i = np.zeros(n_slots, np.int64)
    move_eff = np.full(n_slots, -1, np.int64)
    move_dest = np.zeros(n_slots, np.int64)
    demand = np.zeros(n_slots)
    want = np.zeros(n_slots)
    served = np.zeros(n_slots)
    live = np.zeros(n_nodes, np.int64)
    # move CPU work queued per node (core-seconds), modeled as one extra thread per active move task
    max_tasks = 200000
    task_node = np.zeros(max_tasks, np.int64)
    task_left = np.zeros(max_tasks)
    n_tasks = 0
    nic_free = np.zeros(n_nodes, np.int64)
    history = np.zeros((n_pools, max(1, detect_steps)))
    hist_sum = np.zeros(n_pools)
    cooldown = np.full(n_pools, -1, np.int64)
    pool_cap = np.zeros(n_pools)
    for n in range(n_nodes):
        pool_cap[pool_of_node[n]] += cores
    wait = 0.0
    run = 0.0
    overhead = 0.0
    paused_want = 0.0
    moves = 0
    blocked = 0
    finished = np.zeros(1_000_000)
    n_finished = 0
    load = np.zeros(n_pools)
    for step in range(steps):
        measuring = step >= begin_step
        # 1. start sandboxes
        for s in range(n_slots):
            if not active[s] and next_start[s] >= 0 and step >= next_start[s]:
                t = choices[s, choice_i[s] % choices.shape[1]]
                if floating:
                    best = -1
                    for j in range(place_k):
                        cand = int(place_rand[s, choice_i[s] % place_rand.shape[1], j] * n_nodes)
                        if best < 0 or live[cand] < live[best]:
                            best = cand
                    node[s] = best
                else:
                    node[s] = slot_home[s]
                choice_i[s] += 1
                active[s] = True
                tr[s] = t
                seg_last[s] = trace_first[t] + trace_count[t] - 1
                seg[s], pos[s], gap_left[s], seg_t0[s] = _enter(trace_first[t], seg_last[s], seg_kind, seg_len, step, gap_scale)
                prog[s] = 0.0
                t_start[s] = step
                vrun[s] = min_v[pool_of_node[node[s]]]
                was_runnable[s] = False
                paused_until[s] = -1
                locked_until[s] = -1
                move_eff[s] = -1
                recent[s] = 0.0
                next_start[s] = -1
                live[node[s]] += 1
                if job_size > 0:
                    j = s // job_size
                    ideal = trace_expected[t] + (step - job_begin[j]) * 0.01
                    if ideal > job_ideal[j]:
                        job_ideal[j] = ideal
        # 2. moves that complete now
        for s in range(n_slots):
            if active[s] and move_eff[s] >= 0 and step >= move_eff[s]:
                live[node[s]] -= 1
                vrun[s] += min_v[pool_of_node[move_dest[s]]] - min_v[pool_of_node[node[s]]]
                node[s] = move_dest[s]
                live[node[s]] += 1
                move_eff[s] = -1
        # 3. demand
        for s in range(n_slots):
            w = 0.0
            if active[s] and seg[s] <= seg_last[s] and seg_kind[seg[s]] == 0:
                w = slot_demand[seg_start[seg[s]] + pos[s]]
            want[s] = w
            demand[s] = 0.0 if step < paused_until[s] else w
            if cfs:
                runnable_now = demand[s] > 0.0
                if runnable_now and not was_runnable[s]:  # wakeup: GENTLE_FAIR_SLEEPERS placement
                    floor = min_v[pool_of_node[node[s]]] - sleeper_credit_s
                    if vrun[s] < floor:
                        vrun[s] = floor
                was_runnable[s] = runnable_now
        # 4. share each pool's cores (runnable sandboxes bucketed by pool, plus move-CPU tasks)
        count = np.zeros(n_pools + 1, np.int64)
        for s in range(n_slots):
            if demand[s] > 0.0:
                count[pool_of_node[node[s]] + 1] += 1
        for i in range(n_tasks):
            count[pool_of_node[task_node[i]] + 1] += 1
        for p in range(n_pools):
            count[p + 1] += count[p]
        fill = count[:-1].copy()
        members = np.empty(count[n_pools], np.int64)
        for s in range(n_slots):
            if demand[s] > 0.0:
                p = pool_of_node[node[s]]
                members[fill[p]] = s
                fill[p] += 1
        for i in range(n_tasks):
            p = pool_of_node[task_node[i]]
            members[fill[p]] = -1 - i
            fill[p] += 1
        for p in range(n_pools):
            lo = count[p]
            m = count[p + 1] - lo
            units = np.empty(m)
            threads = np.empty(m)
            sandbox_load = 0.0
            for j in range(m):
                o = members[lo + j]
                if o >= 0:
                    th = math.ceil(demand[o] - 1e-9)
                    threads[j] = th
                    units[j] = demand[o] / th
                    sandbox_load += demand[o]
                else:
                    threads[j] = 1.0
                    units[j] = 1.0
            if cfs:
                vs = np.empty(m)
                caps = np.empty(m)
                for j in range(m):
                    o = members[lo + j]
                    vs[j] = vrun[o] if o >= 0 else task_v[-1 - o]
                    caps[j] = units[j] * threads[j] * DT
                got = _vruntime_fill(vs, caps, threads, pool_cap[p] * DT) / DT
                lowest = 1e300
                for j in range(m):
                    o = members[lo + j]
                    if o >= 0:
                        vrun[o] += got[j] * DT / threads[j]
                        if vrun[o] < lowest:
                            lowest = vrun[o]
                    else:
                        task_v[-1 - o] += got[j] * DT
                if m > 0 and lowest < 1e299 and lowest > min_v[p]:
                    min_v[p] = lowest
            elif bernoulli:
                # each vCPU thread is running with probability = its share of the slot; everyone gets the same
                # efficiency eta = 1 - E[excess]/E[running], which keeps sub-slot burstiness (no fluid smoothing)
                n_thr = 0
                det = 0.0
                for j in range(m):
                    if members[lo + j] >= 0:
                        n_thr += int(threads[j])
                    else:
                        det += 1.0
                probs = np.empty(n_thr)
                k = 0
                mean = det
                for j in range(m):
                    if members[lo + j] >= 0:
                        for _t in range(int(threads[j])):
                            probs[k] = units[j]
                            k += 1
                        mean += units[j] * threads[j]
                got = np.empty(m)
                eta = 1.0
                if mean > 0.0:
                    eta = 1.0 - _expected_excess(probs, det, pool_cap[p]) / mean
                for j in range(m):
                    got[j] = units[j] * threads[j] * eta
            else:
                got = _water_fill(units, threads, pool_cap[p])
            if kw_a[p] > 0.0 and m > 0:
                # measured kernel scheduling wait on top of the fluid share: every entry is served 1/(1+e) of its
                # fluid share, e = a(pool size) * exp(b * util). capacity mode: the freed core-time is lost.
                # latency mode (work-conserving): the freed core-time goes to entries the fluid share left short
                busy = 0.0
                for j in range(m):
                    busy += got[j]
                util = busy / pool_cap[p]
                if util > 1.0:
                    util = 1.0
                keep = 1.0 / (1.0 + kw_a[p] * math.exp(kw_b * util))
                short = np.empty(m)
                left = pool_cap[p]
                for j in range(m):
                    short[j] = (units[j] * threads[j] - got[j]) / threads[j]
                    got[j] *= keep
                    left -= got[j]
                if kw_latency and left > 1e-12:
                    more = _water_fill(short, threads, left)
                    for j in range(m):
                        got[j] += more[j]
            total = 0.0
            for j in range(m):
                total += units[j] * threads[j]
                o = members[lo + j]
                if o >= 0:
                    served[o] = got[j]
                    if measuring:
                        wait += (demand[o] - got[j]) * DT
                        run += got[j] * DT
                else:
                    task_left[-1 - o] -= got[j] * DT
                    if measuring:
                        overhead += got[j] * DT
            load[p] = sandbox_load  # destination choice looks at sandbox demand, as in sim.py
            excess = total - pool_cap[p]
            if excess < 0.0:
                excess = 0.0
            if detect_steps > 0:
                idx = step % detect_steps
                hist_sum[p] += excess - history[p, idx]
                history[p, idx] = excess
        # drop finished move tasks
        k = 0
        for i in range(n_tasks):
            if task_left[i] > 1e-12:
                task_node[k] = task_node[i]
                task_left[k] = task_left[i]
                task_v[k] = task_v[i]
                k += 1
        n_tasks = k
        # 5. advance sandboxes
        for s in range(n_slots):
            if not active[s]:
                continue
            recent[s] = 0.9 * recent[s] + 0.1 * demand[s]
            if step < paused_until[s]:
                if measuring:
                    paused_want += want[s] * DT
                continue
            g = seg[s]
            if g <= seg_last[s]:
                if seg_kind[g] == 1:
                    gap_left[s] -= 1
                    if gap_left[s] <= 0:
                        seg[s], pos[s], gap_left[s], seg_t0[s] = _enter(g + 1, seg_last[s], seg_kind, seg_len, step + 1, gap_scale)
                        prog[s] = 0.0
                elif g > trace_first[tr[s]] and step + 1 - seg_t0[s] >= timeout_steps:
                    seg[s], pos[s], gap_left[s], seg_t0[s] = _enter(g + 1, seg_last[s], seg_kind, seg_len, step + 1, gap_scale)
                    prog[s] = 0.0
                else:
                    w = want[s]
                    prog[s] += 1.0 if w <= 0.0 else served[s] / (w * inflation)
                    if prog[s] >= 1.0 - 1e-9:
                        pos[s] += 1
                        # carry=True: pure fluid progress (used for the fleet results). carry=False: a partly served
                        # slot rounds up to a whole extra step; it fits small nodes but overstates contention on large
                        # ones (RESULTS-fleet.md 1절). Scheduler wait is added separately (kernel_wait, 7절)
                        prog[s] = prog[s] - 1.0 if carry and prog[s] > 1.0 else 0.0
                        if pos[s] >= seg_len[g]:
                            prog[s] = 0.0
                            seg[s], pos[s], gap_left[s], seg_t0[s] = _enter(g + 1, seg_last[s], seg_kind, seg_len, step + 1, gap_scale)
            if seg[s] > seg_last[s]:
                if measuring and t_start[s] >= begin_step and n_finished < finished.shape[0]:
                    finished[n_finished] = (step + 1 - t_start[s]) * 0.01 / trace_expected[tr[s]]
                    n_finished += 1
                active[s] = False
                live[node[s]] -= 1
                if job_size == 0:
                    next_start[s] = step + 1 + gap_steps
                else:
                    # synchronous RL step: the job waits for its slowest rollout, trains, then starts the next step
                    j = s // job_size
                    job_done[j] += 1
                    if job_done[j] == job_size:
                        if measuring and job_begin[j] >= begin_step and n_steps_done < step_ratio.shape[0]:
                            step_secs[n_steps_done] = (step + 1 - job_begin[j]) * 0.01
                            step_ratio[n_steps_done] = step_secs[n_steps_done] / job_ideal[j]
                            n_steps_done += 1
                        nxt = step + 1 + train_steps
                        job_begin[j] = nxt
                        job_done[j] = 0
                        job_ideal[j] = 0.0
                        for q in range(j * job_size, (j + 1) * job_size):
                            next_start[q] = nxt + jitter[q, choice_i[q] % jitter.shape[1]]
        # 6. moves (split nodes: pool == node)
        if mech_kind > 0 and n_pools == n_nodes:
            ncount = np.zeros(n_nodes + 1, np.int64)
            for s in range(n_slots):
                if active[s]:
                    ncount[node[s] + 1] += 1
            for n in range(n_nodes):
                ncount[n + 1] += ncount[n]
            nfill = ncount[:-1].copy()
            on_node = np.empty(ncount[n_nodes], np.int64)
            for s in range(n_slots):
                if active[s]:
                    on_node[nfill[node[s]]] = s
                    nfill[node[s]] += 1
            for p in range(n_pools):
                if cooldown[p] > step or detect_steps == 0 or hist_sum[p] / detect_steps < threshold:
                    continue
                batch = min(max_batch, max(1, math.ceil(hist_sum[p] / detect_steps)))
                done = 0
                last_eff = step
                while done < batch:
                    dest = -1
                    for q in range(n_pools):
                        if q != p and load[q] <= pool_cap[q] - 1.0 and (dest < 0 or load[q] < load[dest]):
                            dest = q
                    pick = -1
                    for j in range(ncount[p], ncount[p + 1]):
                        s = on_node[j]
                        if active[s] and node[s] == p and locked_until[s] <= step and move_eff[s] < 0 \
                                and recent[s] > 0.05 and (pick < 0 or recent[s] > recent[pick]):
                            pick = s
                    if dest < 0 or pick < 0:
                        if done == 0:
                            blocked += 1
                        break
                    if mech_kind == 1:
                        eff = step + latency_steps
                        if latency_steps > 0:
                            paused_until[pick] = eff
                        if handoff_cpu_s > 0.0 and n_tasks < max_tasks:
                            task_node[n_tasks] = p
                            task_left[n_tasks] = handoff_cpu_s
                            task_v[n_tasks] = min_v[p]
                            n_tasks += 1
                    else:
                        mem = trace_mem[tr[pick]]
                        dump = int(math.ceil(dump_s_per_gib * mem / DT))
                        xfer = int(math.ceil(xfer_s_per_gib * mem / DT))
                        begin = max(step + dump, nic_free[p])
                        eff = begin + xfer
                        nic_free[p] = eff
                        if pause:
                            paused_until[pick] = eff
                        if n_tasks < max_tasks:
                            task_node[n_tasks] = p
                            task_left[n_tasks] = dump_s_per_gib * mem
                            task_v[n_tasks] = min_v[p]
                            n_tasks += 1
                    move_eff[pick] = eff
                    move_dest[pick] = dest
                    locked_until[pick] = eff + hold_steps
                    load[dest] += recent[pick]
                    load[p] -= recent[pick]
                    if eff > last_eff:
                        last_eff = eff
                    moves += 1
                    done += 1
                if done > 0:
                    cooldown[p] = last_eff + hold_steps
    return wait, run, overhead, paused_want, moves, blocked, finished[:n_finished], step_secs[:n_steps_done], \
        step_ratio[:n_steps_done]


def simulate(job):
    arrays, names, config = job["arrays"], job["names"], job["config"]
    nodes, cores, pool = config["nodes"], config["cores"], config["pool"]
    if pool == "none":
        pool_of_node = np.arange(nodes)
    elif pool == "socket":
        pool_of_node = np.arange(nodes) // max(1, nodes // 2)
    else:
        pool_of_node = np.zeros(nodes, np.int64)
    n_pools = int(pool_of_node.max()) + 1
    per_node = config["per_node"]
    n_slots = per_node * nodes
    slot_home = np.repeat(np.arange(nodes), per_node).astype(np.int64)
    ramp_steps = int(round(config["ramp_s"] / DT))
    steps = int(round((config["ramp_s"] + config["duration_s"]) / DT))
    entries = list(range(len(names)))
    width = 256
    first_start = np.zeros(n_slots, np.int64)
    choices = np.zeros((n_slots, width), np.int64)
    jitter = np.zeros((n_slots, width), np.int64)
    job_size = config["batch"] * config["group"] if config["rl"] else 0
    if job_size:
        if n_slots % job_size:
            raise ValueError("per_node x nodes must be a multiple of batch x group")
        if not config["floating"]:
            slot_home = (np.arange(n_slots) % nodes).astype(np.int64)  # rollout i on node i mod N, as in the pilot
        for j in range(n_slots // job_size):
            rng = random.Random(f"{config['seed']}-group-{j}")  # same draw order as the pilot's group mode
            offset = rng.uniform(0, config["ramp_s"])
            for k in range(width):
                tasks = [rng.choice(entries) for _ in range(config["batch"])]
                for i in range(job_size):
                    s = j * job_size + i
                    choices[s, k] = tasks[i // config["group"]]
                    jitter[s, k] = int(round(rng.uniform(0, config["jitter_s"]) / DT))
            for i in range(job_size):
                s = j * job_size + i
                first_start[s] = int(math.ceil(offset / DT)) + jitter[s, 0]
    else:
        for s in range(n_slots):
            n, slot = divmod(s, per_node)
            rng = random.Random(f"{config['seed']}-{n}-{slot}")  # same draws as sim.py and the pilot
            first_start[s] = int(math.ceil(rng.uniform(0, config["ramp_s"]) / DT - 1e-9))
            choices[s] = [rng.choice(entries) for _ in range(width)]
    place = np.random.default_rng(abs(hash(("place", config["seed"]))) % 2**32).random((n_slots, width, max(1, config["place_k"])))
    mech = MECHANISMS[config["mechanism"]]
    scale = 37.0 / config.get("nic_gbps", mech.get("nic_gbps", 37.0)) if mech["kind"] == 2 else 1.0
    started = time.time()
    wait, run, overhead, paused_want, moves, blocked, finished, step_secs, step_ratio = run_fleet(
        arrays["slot_demand"], arrays["seg_kind"], arrays["seg_start"], arrays["seg_len"], arrays["trace_first"],
        arrays["trace_count"], arrays["trace_vcpus"], arrays["trace_mem"],
        arrays["trace_cpu_s"] + arrays["trace_gap_s"] * config["gap_scale"],
        nodes, float(cores), pool_of_node.astype(np.int64), n_pools, slot_home, first_start, choices, place,
        int(config["place_k"]), bool(config["floating"]), steps, ramp_steps, int(round(SANDBOX_GAP_S / DT)),
        int(round(TIMEOUT_S / DT)), int(mech["kind"]), bool(mech.get("pause", False)),
        int(round(mech.get("latency_s", 0.0) / DT)) if mech["kind"] == 1 else 0,
        float(mech.get("cpu_s", 0.0)), float(mech.get("dump_s_per_gib", 0.0)),
        float(mech.get("xfer_s_per_gib", 0.0)) * scale, int(round(config["detect_ms"] / 10)),
        float(config["threshold"]), int(round(config["hold_s"] / DT)), int(config["max_batch"]), int(job_size),
        int(round(config["train_s"] / DT)), jitter, float(config["gap_scale"]), config["sched"] == "cfs",
        float(config["sleeper_credit_ms"]) / 1000, float(config["cpu_inflation"]),
        bool(config["carry"] if config["sched"] != "bernoulli" else True), config["sched"] == "bernoulli",
        kernel_wait_a(config, cores * (np.bincount(pool_of_node) if config["kernel_wait_size"] == "pool"
                                       else np.ones(n_pools))),
        float(config["kernel_wait_b"] if config["kernel_wait_b"] is not None
              else KERNEL_WAIT[config["kernel_wait_mode"]]["b"]),
        config["kernel_wait_mode"] == "latency")
    q = lambda x: float(np.percentile(finished, x)) if len(finished) else None
    return {**{k: v for k, v in config.items()}, "wait_over_run": wait / run if run else None,
            "run_cores_per_node": run / config["duration_s"] / nodes, "move_cpu_over_run": overhead / run if run else None,
            "paused_work_over_run": paused_want / run if run else None,
            "lost_over_run": (wait + paused_want) / run if run else None, "moves": int(moves), "blocked": int(blocked),
            "traces_finished": int(len(finished)), "trace_slowdown_p50": q(50), "trace_slowdown_p90": q(90),
            "trace_slowdown_p99": q(99), "trace_slowdown_mean": float(finished.mean()) if len(finished) else None,
            "rl_steps": int(len(step_secs)),
            "step_s_p50": float(np.percentile(step_secs, 50)) if len(step_secs) else None,
            "step_s_mean": float(step_secs.mean()) if len(step_secs) else None,
            "step_slowdown_p50": float(np.percentile(step_ratio, 50)) if len(step_ratio) else None,
            "step_slowdown_mean": float(step_ratio.mean()) if len(step_ratio) else None,
            "wall_s": round(time.time() - started, 2)}


DEFAULTS = dict(nodes=4, cores=5, pool="none", per_node=14, seed="1", ramp_s=60, duration_s=600, mechanism="none",
                floating=False, place_k=2, detect_ms=50, threshold=0.1, hold_s=1.0, max_batch=1,
                rl=False, batch=8, group=8, train_s=30.0, jitter_s=2.0, gap_scale=1.0,
                sched="share", sleeper_credit_ms=12.0, cpu_inflation=1.0, carry=False,
                kernel_wait="none", kernel_wait_a=0.0, kernel_wait_b=None, kernel_wait_mode="capacity",
                kernel_wait_size="pool")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="Run the hand-computed checks and exit")
    parser.add_argument("--solo", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--grid", type=Path, help="JSON list of config overrides (see DEFAULTS)")
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.test:
        self_test()
        return
    programs = load_programs(args.solo, args.candidates)
    names = [t["name"] for t in json.loads(args.candidates.read_text())["traces"] if t["name"] in programs]
    arrays = flatten(programs, names)
    configs = [DEFAULTS | c for c in json.loads(args.grid.read_text())]
    jobs = [{"arrays": arrays, "names": names, "config": c} for c in configs]
    with ProcessPoolExecutor(args.jobs) as pool:
        results = list(pool.map(simulate, jobs))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"solo": str(args.solo), "candidates": str(args.candidates), "traces": len(names),
                                       "mechanisms": MECHANISMS, "results": results}, indent=1))
    print(f"{len(results)} configs, {len(names)} traces -> {args.output}")


def self_test():
    """Hand-computed cases for the compiled model."""
    got = _water_fill(np.array([1.0, 1.0]), np.array([1.0, 1.0]), 1.0)
    assert np.allclose(got, [0.5, 0.5]), got
    got = _water_fill(np.array([1.0, 0.2]), np.array([1.0, 1.0]), 1.0)
    assert np.allclose(got, [0.8, 0.2]), got
    got = _water_fill(np.array([1.0, 1.0]), np.array([4.0, 1.0]), 2.0)  # 5 threads share 2 cores
    assert np.allclose(got, [1.6, 0.4]), got

    def program(*parts):
        return {"program": [("cpu", np.full(n, d)) if kind == "cpu" else ("gap", n) for kind, n, d in parts],
                "vcpus": 1, "memory_gib": 1.0}

    # two threads each running half the time on one core: P(both) = 1/4 -> E[excess] = 0.25
    assert abs(_expected_excess(np.array([0.5, 0.5]), 0.0, 1.0) - 0.25) < 1e-12
    big = np.full(1000, 0.5)  # normal branch vs exact mean-field: E[(S-500)+] for S~Bin(1000, .5) ~ 6.3
    assert abs(_expected_excess(big, 0.0, 500.0) - 6.3) < 0.4, _expected_excess(big, 0.0, 500.0)
    got = _vruntime_fill(np.array([0.0, 0.004]), np.array([0.01, 0.01]), np.array([1.0, 1.0]), 0.01)
    assert np.allclose(got, [0.007, 0.003]), got  # level L: (L - 0) + (L - 0.004) = 0.01

    def go(programs, nodes, cores, per_node, mechanism="none", steps=None, detect=1, hold=0, homes=None, job=0, train=0,
           cfs=False, choice=None, kw=(0.0, 0.0), latency=False):
        names = list(programs)
        arrays = flatten(programs, names)
        homes = np.repeat(np.arange(nodes), per_node) if homes is None else np.array(homes)
        n_slots = len(homes)
        mech = MECHANISMS[mechanism]
        return run_fleet(arrays["slot_demand"], arrays["seg_kind"], arrays["seg_start"], arrays["seg_len"],
                         arrays["trace_first"], arrays["trace_count"], arrays["trace_vcpus"], arrays["trace_mem"],
                         arrays["trace_expected"], nodes, float(cores), np.arange(nodes), nodes,
                         homes.astype(np.int64), np.zeros(n_slots, np.int64),
                         np.zeros((n_slots, 4), np.int64) if choice is None else np.array(choice, np.int64),
                         np.zeros((n_slots, 4, 1)), 1, False, steps, 0,
                         10**9, int(TIMEOUT_S / DT), mech["kind"], bool(mech.get("pause", False)),
                         int(round(mech.get("latency_s", 0) / DT)), float(mech.get("cpu_s", 0)),
                         float(mech.get("dump_s_per_gib", 0)), float(mech.get("xfer_s_per_gib", 0)), detect, 0.1, hold, 1,
                         job, train, np.zeros((n_slots, 4), np.int64), 1.0, cfs, 0.012, 1.0, True, False,
                         np.full(nodes, kw[0]), kw[1], latency)

    # two 1-core sandboxes of 1 s each on one core: both run at 0.5 for 2 s -> 2 s of wait in total
    wait, run, *_ = go({"a": program(("cpu", 100, 1.0))}, nodes=1, cores=1, per_node=2, steps=200)
    assert abs(wait - 2.0) < 0.02 and abs(run - 2.0) < 0.02, (wait, run)
    # kernel wait term: one 1 s sandbox alone on 2 cores (util 0.5), a = 0.05, b = 2 ln 2 -> wait/run = 0.05 * 2 = 0.1
    wait, run, *_ = go({"a": program(("cpu", 100, 1.0))}, nodes=1, cores=2, per_node=1, steps=300,
                       kw=(0.05, 2 * math.log(2.0)))
    assert abs(wait / run - 0.1) < 1e-6 and abs(run - 1.0) < 0.02, (wait, run)
    # latency mode, same case: nobody is short of cores, so nothing is handed back and the result is the same
    wait, run, *_ = go({"a": program(("cpu", 100, 1.0))}, nodes=1, cores=2, per_node=1, steps=300,
                       kw=(0.05, 2 * math.log(2.0)), latency=True)
    assert abs(wait / run - 0.1) < 1e-6 and abs(run - 1.0) < 0.02, (wait, run)
    # latency mode, three 1 s sandboxes on 2 cores (util 1, a = 0.05, b = ln 2 -> e = 0.1): the freed core-time goes
    # back to the short sandboxes, so the pool stays fully busy (capacity mode would keep only 2/1.1 cores busy)
    three = {"a": program(("cpu", 100, 1.0))}
    wait, run, *_ = go(three, nodes=1, cores=2, per_node=3, steps=10, kw=(0.05, math.log(2.0)), latency=True)
    assert abs(run - 0.2) < 1e-9, (wait, run)
    wait, run, *_ = go(three, nodes=1, cores=2, per_node=3, steps=10, kw=(0.05, math.log(2.0)))
    assert abs(run - 0.2 / 1.1) < 1e-9, (wait, run)
    # timeout: boot 10 ms, 1 s gap, then a 70 s command cut at 60 s
    finished = go({"a": program(("cpu", 1, 1.0), ("gap", 1.0, 0), ("cpu", 7000, 1.0))}, nodes=1, cores=1,
                  per_node=1, steps=7000)[6]
    assert abs(finished[0] * 71.01 - 61.01) < 0.03, finished
    # two 20 s sandboxes on node 0 of two 1-core nodes: 40 s of wait without moves, ~none with an instant move
    two = {"a": program(("cpu", 2000, 1.0))}
    wait, *_ = go(two, nodes=2, cores=1, per_node=0, steps=4000, homes=[0, 0])
    assert abs(wait - 40.0) < 0.1, wait  # each runs at 0.5 for 40 s
    wait, run, overhead, paused, moves, blocked = go(two, nodes=2, cores=1, per_node=0, mechanism="instant",
                                                     steps=4000, homes=[0, 0])[:6]
    assert wait < 0.05 and moves == 1, (wait, moves)
    # stop-and-copy of a 1 GiB sandbox: paused for dump 0.30 s + transfer 0.39 s, 0.30 core-s of source CPU
    wait, run, overhead, paused, moves, blocked = go(two, nodes=2, cores=1, per_node=0,
                                                     mechanism="migration stop-and-copy", steps=4000, homes=[0, 0])[:6]
    assert moves == 1 and abs(paused - 0.69) < 0.03 and abs(overhead - 0.30) < 0.02, (paused, overhead)
    # CFS mode: symmetric sharing is unchanged (two 20 s hogs on one core -> 40 s of wait)
    wait, *_ = go(two, nodes=2, cores=1, per_node=0, steps=4000, homes=[0, 0], cfs=True)
    assert abs(wait - 40.0) < 0.1, wait
    # CFS mode: a 30 ms burst waking next to a CPU hog finishes sooner than with equal shares (12 ms sleeper credit)
    mixed = {"hog": program(("cpu", 2000, 1.0)), "burst": program(("cpu", 1, 0.0), ("gap", 0.5, 0), ("cpu", 3, 1.0))}
    choice = [[0] * 4, [1] * 4]
    fair = go(mixed, nodes=1, cores=1, per_node=0, steps=1000, homes=[0, 0], choice=choice)[6]
    cfs = go(mixed, nodes=1, cores=1, per_node=0, steps=1000, homes=[0, 0], choice=choice, cfs=True)[6]
    assert cfs.min() < fair.min(), (cfs, fair)  # the burst's slowdown
    # RL job of 2 rollouts on one 1-core node: both 1 s of work -> each step takes 2 s, then 1 s of training
    out = go({"a": program(("cpu", 100, 1.0))}, nodes=1, cores=1, per_node=2, steps=1000, job=2, train=100)
    step_secs = out[7]
    assert len(step_secs) == 3 and np.allclose(step_secs, 2.0, atol=0.02), step_secs  # steps begin at 0, 3, 6 s
    print("self-test passed")


if __name__ == "__main__":
    main()
