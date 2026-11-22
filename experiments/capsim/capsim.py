#!/usr/bin/env python3
"""Memory-capacity fleet simulator: DRAM only vs per-node CXL expander vs shared CXL pool, with or without moving a
sandbox when a heavy command starts.

Inputs (prep.py): per command the 10 ms CPU demand of the 10-14 Docker DRAM replay (uncapped, up to 30 cores), the
recorded LLM gap before it, the memory at its start (state) and its peak; per trace the guest memory size (reservation,
1.5x Docker peak, at least 2 GiB).

Model
- N nodes, C cores, D GiB DRAM each. CXL: none, X GiB private per node (expander), or N*X GiB shared (pool).
- A fixed population of sandbox slots. A slot runs one trace (commands and recorded gaps); when it ends, a new random
  trace arrives 0.5 s later and must be admitted.
- Admission reserves the guest memory size: dram: node sum <= D; expander: node sum <= D + X; pool: each node uses its
  DRAM first and the excess over D comes from the pool, sum of excess <= N*X. Sample place_k nodes (default 2; 0 = all)
  and take the fitting one with the lowest key (place_metric 0 = CPU demand of the previous step, 1 = sandbox count,
  2 = reserved memory), else the lowest-key node that fits, else wait. Defaults reproduce the 11-16 runs.
- Balance probe (measurement phase): per step, demand above node cores, the part of it other nodes could have taken,
  and the node-to-node CV and max/mean of CPU demand, resident memory and reservations, also over 1 s windows.
- CPU: CFS-like water-filling per node, a command of demand d runs as ceil(d) equal threads.
- Memory placement on a node (expander and pool): new memory of running commands gets DRAM first; sandbox state
  (memory at command start) gets the DRAM left, the rest is in CXL. Tax stretches a command (more CPU time, same
  width): tax = cxl_state_fraction * tau_state + spilled_new_fraction * tau_new, with heavy and light values.
- GRPO groups (optional, group > 1): G slots share one trace and start together (plus a random jitter); the group
  takes a new trace only when all G are done. The G siblings are cut into chunks of `chunk`; a chunk's siblings go to
  the node of the first one placed (image co-location), others follow the normal placement.
- Image page cache (optional, read_gbps > 0): a command that starts on a node without its task's image pages waits for
  the task-only working set to be read (first reader reads, later ones wait for the rest). Pages stay while any
  sandbox of the task is on the node. A move to such a node pays the same read (cold_on_move).
- tax_split: all sandbox state in CXL and new memory of running commands in DRAM, on every node.
- Moves (optional) when a heavy command (>= 1 CPU-s, oracle) starts and its node lacks width + 1 free cores: go to the
  node with the most free cores (previous step demand) that admits its reservation and has DRAM for its new memory.
  The command pauses for the move: hand-off a fixed latency; copy also sends the state over a NIC.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from numba import njit

DT = 0.010
HERE = Path(__file__).resolve().parent
MODES = {"dram": 0, "expander": 1, "pool": 2}


@njit(cache=True)
def _admit_ok(mode, n, r, R_node, D, X, pool_used, pool_cap):
    if mode == 0:
        return R_node[n] + r <= D + 1e-9
    if mode == 1:
        return R_node[n] + r <= D + X + 1e-9
    old = max(0.0, R_node[n] - D)
    new = max(0.0, R_node[n] + r - D)
    return pool_used - old + new <= pool_cap + 1e-9


@njit(cache=True)
def _pool_delta(n, r, R_node, D):
    """Change of pool use when r is added to node n (r may be negative)."""
    return max(0.0, R_node[n] + r - D) - max(0.0, R_node[n] - D)


@njit(cache=True)
def _read_pause(ready, n, t, step, ws):
    """Steps a command waits for the task's image pages on node n: the first reader reads ws steps, later ones wait
    for the rest of that read."""
    if ws <= 0:
        return 0
    if ready[n, t] < 0:
        ready[n, t] = step + ws
        return ws
    rem = ready[n, t] - step
    return rem if rem > 0 else 0


@njit(cache=True)
def _pkey(metric, n, load, nsb, R_node):
    if metric == 0:
        return load[n]
    if metric == 1:
        return nsb[n] + 1e-6 * load[n]
    return R_node[n] + 1e-6 * load[n]


@njit(cache=True)
def simulate(tr_first, tr_count, tr_reserve, c_gap, c_off, c_len, c_state, c_new, c_cpu, c_heavy, c_width, demand,
             mode, n_nodes, cores, D, X, n_slots, seed, ramp_steps, end_steps, move_kind, move_lat_steps,
             nic_gib_s, copy_base_gib, tau_hs, tau_ls, tau_hn, tau_ln, margin, max_rec, hwm_model, place_k, place_metric,
             group_size, chunk, jitter_steps, ws_steps, cold_on_move, tax_split):
    np.random.seed(seed)
    n_tr = tr_first.shape[0]
    pool_cap = n_nodes * X if mode == 2 else 0.0
    node = np.full(n_slots, -1, np.int64)
    trace = np.zeros(n_slots, np.int64)
    cmd = np.zeros(n_slots, np.int64)
    phase = np.zeros(n_slots, np.int64)  # 0 wait admission, 1 gap, 2 run, 3 paused (moving)
    cnt = np.zeros(n_slots, np.int64)
    prog = np.zeros(n_slots)
    start = np.zeros(n_slots, np.int64)
    cur_d = np.zeros(n_slots)
    park = np.zeros(n_slots)  # resident memory between commands counted on the node
    G = group_size
    n_ch = max(1, G // chunk)
    n_groups = n_slots // G if G > 1 else 0
    chunk_node = np.full(max(1, n_groups * n_ch), -1, np.int64)
    gdone = np.zeros(max(1, n_groups), np.int64)
    pres = np.zeros((n_nodes, n_tr), np.int64)  # sandboxes of each task on each node
    ready = np.full((n_nodes, n_tr), -1, np.int64)  # step when the task's image pages are in the node's page cache
    needs_first = np.zeros(n_slots, np.bool_)
    if G > 1:
        for g in range(n_groups):
            t0 = np.random.randint(n_tr)
            c0 = np.random.randint(max(1, ramp_steps // 2))
            for i in range(G):
                trace[g * G + i] = t0
                cnt[g * G + i] = c0 + (np.random.randint(jitter_steps + 1) if jitter_steps > 0 else 0)
    else:
        for s in range(n_slots):
            trace[s] = np.random.randint(n_tr)
            cnt[s] = np.random.randint(max(1, ramp_steps // 2))
    R_node = np.zeros(n_nodes)
    P_node = np.zeros(n_nodes)
    W_node = np.zeros(n_nodes)
    load = np.zeros(n_nodes)
    nsb = np.zeros(n_nodes)
    pool_used = 0.0
    f_park = np.zeros(n_nodes)
    f_spill = np.zeros(n_nodes)
    # records
    rec_slow = np.zeros(max_rec)
    rec_cpu = np.zeros(max_rec)
    rec_heavy = np.zeros(max_rec, np.bool_)
    n_rec = 0
    acc = np.zeros(n_nodes)
    n_acc = 0
    stats = np.zeros(32)  # 10 resident GiB sum (P+W) per node-step  # 0 admitted-slot-steps, 1 waiting-slot-steps, 2 served core-steps, 3 moves, 4 move tries
    # 5 taxed extra cpu (core-steps), 6 spill node-steps, 7 pool used sum, 8 sandboxes started, 9 blocked moves
    counts = np.zeros(n_nodes, np.int64)
    order = np.zeros(n_slots, np.int64)
    first = np.zeros(n_nodes + 1, np.int64)
    u = np.zeros(n_slots)
    th = np.zeros(n_slots)
    served = np.zeros(n_slots)
    reserved_now = np.zeros(n_nodes)
    for step in range(end_steps):
        measure = step >= ramp_steps
        reserved_now[:] = load
        # 1. transitions
        for s in range(n_slots):
            if phase[s] == 0:
                cnt[s] -= 1
                if cnt[s] > 0:
                    continue
                r = tr_reserve[trace[s]]
                best = -1
                ch = -1
                if G > 1:
                    # siblings of one chunk follow the first one placed, if it still fits
                    ch = (s // G) * n_ch + (s % G) // chunk
                    if chunk_node[ch] >= 0 and _admit_ok(mode, chunk_node[ch], r, R_node, D, X, pool_used, pool_cap):
                        best = chunk_node[ch]
                if best < 0 and place_k > 0 and place_k < n_nodes:
                    for i in range(place_k):
                        cand = np.random.randint(n_nodes)
                        if _admit_ok(mode, cand, r, R_node, D, X, pool_used, pool_cap):
                            if best < 0 or _pkey(place_metric, cand, load, nsb, R_node) < _pkey(place_metric, best, load, nsb, R_node):
                                best = cand
                if best < 0:
                    # all-node mode breaks ties from a random start; the k-sample fallback keeps the 11-16 order
                    o = np.random.randint(n_nodes) if place_k <= 0 or place_k >= n_nodes else 0
                    for j in range(n_nodes):
                        cand = (o + j) % n_nodes
                        if _admit_ok(mode, cand, r, R_node, D, X, pool_used, pool_cap):
                            if best < 0 or _pkey(place_metric, cand, load, nsb, R_node) < _pkey(place_metric, best, load, nsb, R_node):
                                best = cand
                if best < 0:
                    cnt[s] = 10
                    continue
                if mode == 2:
                    pool_used += _pool_delta(best, r, R_node, D)
                R_node[best] += r
                node[s] = best
                nsb[best] += 1.0
                if ch >= 0 and chunk_node[ch] < 0:
                    chunk_node[ch] = best
                pres[best, trace[s]] += 1
                needs_first[s] = True
                cmd[s] = tr_first[trace[s]]
                park[s] = c_state[cmd[s]]
                P_node[best] += park[s]
                phase[s] = 1
                cnt[s] = c_gap[cmd[s]]
                if measure:
                    stats[8] += 1
            if phase[s] == 1:
                if cnt[s] > 0:
                    cnt[s] -= 1
                    continue
                c = cmd[s]
                n = node[s]
                start[s] = step
                prog[s] = 0.0
                phase[s] = 2
                if move_kind > 0 and c_heavy[c]:
                    w = c_width[c]
                    free_n = cores - reserved_now[n]
                    if free_n < w + margin:
                        stats[4] += 1
                        r = tr_reserve[trace[s]]
                        best = -1
                        bestfree = free_n + 1.0
                        for m in range(n_nodes):
                            if m == n:
                                continue
                            fm = cores - reserved_now[m]
                            if fm < bestfree:
                                continue
                            if mode != 0 and D - W_node[m] < c_new[c]:
                                continue
                            if mode == 2:
                                delta = _pool_delta(n, -r, R_node, D)
                                R_node[n] -= r
                                ok = _admit_ok(mode, m, r, R_node, D, X, pool_used + delta, pool_cap)
                                R_node[n] += r
                            else:
                                ok = _admit_ok(mode, m, r, R_node, D, X, pool_used, pool_cap)
                            if ok:
                                best = m
                                bestfree = fm
                        if best >= 0:
                            if mode == 2:
                                pool_used += _pool_delta(n, -r, R_node, D)
                            R_node[n] -= r
                            if mode == 2:
                                pool_used += _pool_delta(best, r, R_node, D)
                            R_node[best] += r
                            P_node[n] -= park[s]
                            P_node[best] += park[s]
                            reserved_now[best] += w
                            reserved_now[n] -= w
                            nsb[n] -= 1.0
                            nsb[best] += 1.0
                            tr_ = trace[s]
                            pres[n, tr_] -= 1
                            if pres[n, tr_] == 0:
                                ready[n, tr_] = -1
                            pres[best, tr_] += 1
                            node[s] = best
                            n = best
                            pause = move_lat_steps
                            if move_kind == 2:
                                pause += int(math.ceil((park[s] + copy_base_gib) / nic_gib_s / DT))
                            if cold_on_move:
                                rp = _read_pause(ready, best, tr_, step, ws_steps[tr_])
                                pause += rp
                                if measure:
                                    stats[29] += rp
                            needs_first[s] = False
                            phase[s] = 3
                            cnt[s] = pause
                            if measure:
                                stats[3] += 1
                        elif measure:
                            stats[9] += 1
                if needs_first[s]:
                    needs_first[s] = False
                    rp = _read_pause(ready, n, trace[s], step, ws_steps[trace[s]])
                    if rp > 0:
                        if phase[s] == 3:
                            cnt[s] += rp
                        else:
                            phase[s] = 3
                            cnt[s] = rp
                        if measure:
                            stats[29] += rp
                W_node[n] += c_new[c]
                continue
            if phase[s] == 3:
                cnt[s] -= 1
                if cnt[s] <= 0:
                    phase[s] = 2
        # 2. per-node memory placement
        for n in range(n_nodes):
            if tax_split:
                # all sandbox state in CXL, new memory of running commands in DRAM
                f_park[n] = 1.0
                f_spill[n] = 0.0
                continue
            if mode == 0:
                f_park[n] = 0.0
                f_spill[n] = 0.0
                continue
            f_spill[n] = (W_node[n] - D) / W_node[n] if W_node[n] > D else 0.0
            left = max(0.0, D - W_node[n])
            f_park[n] = (P_node[n] - left) / P_node[n] if P_node[n] > left else 0.0
            if measure and f_spill[n] > 0:
                stats[6] += 1
        if measure:
            stats[7] += pool_used
            for n in range(n_nodes):
                stats[10] += P_node[n] + W_node[n]
        # 3. CPU: bucket running slots by node
        counts[:] = 0
        for s in range(n_slots):
            if phase[s] == 2:
                cur_d[s] = demand[c_off[cmd[s]] + int(prog[s])]
                counts[node[s]] += 1
        first[0] = 0
        for n in range(n_nodes):
            first[n + 1] = first[n] + counts[n]
        counts[:] = 0
        for s in range(n_slots):
            if phase[s] == 2:
                n = node[s]
                order[first[n] + counts[n]] = s
                counts[n] += 1
        for n in range(n_nodes):
            total = 0.0
            for i in range(first[n], first[n + 1]):
                total += cur_d[order[i]]
            load[n] = total
            if total <= cores:
                for i in range(first[n], first[n + 1]):
                    served[order[i]] = cur_d[order[i]]
                continue
            k = first[n + 1] - first[n]
            idx = np.empty(k, np.int64)
            tt = 0.0
            for j in range(k):
                s = order[first[n] + j]
                th[s] = max(1.0, math.ceil(cur_d[s] - 1e-9))
                u[s] = cur_d[s] / th[s]
                idx[j] = s
                tt += th[s]
            keys = np.empty(k)
            for j in range(k):
                keys[j] = u[idx[j]]
            srt = idx[np.argsort(keys)]
            cap = float(cores)
            for j in range(k):
                s = srt[j]
                if u[s] * tt <= cap:
                    served[s] = cur_d[s]
                    cap -= cur_d[s]
                    tt -= th[s]
                else:
                    level = cap / tt
                    for jj in range(j, k):
                        s2 = srt[jj]
                        served[s2] = th[s2] * level
                    break
        if measure:
            tot = 0.0
            over = 0.0
            spare = 0.0
            mx = 0.0
            for n in range(n_nodes):
                tot += load[n]
                acc[n] += load[n]
                if load[n] > cores:
                    over += load[n] - cores
                else:
                    spare += cores - load[n]
                if load[n] > mx:
                    mx = load[n]
            stats[11] += over
            stats[12] += min(over, spare)
            if over > 0:
                stats[13] += 1
            stats[14] += tot
            mean = tot / n_nodes
            if mean > 0:
                v = 0.0
                for n in range(n_nodes):
                    v += (load[n] - mean) ** 2
                stats[15] += math.sqrt(v / n_nodes) / mean
                stats[16] += mx / mean
                stats[21] += 1
            mt = 0.0
            mmx = 0.0
            rt = 0.0
            rmx = 0.0
            for n in range(n_nodes):
                m = P_node[n] + W_node[n]
                mt += m
                if m > mmx:
                    mmx = m
                rt += R_node[n]
                if R_node[n] > rmx:
                    rmx = R_node[n]
            mmean = mt / n_nodes
            rmean = rt / n_nodes
            vm = 0.0
            vr = 0.0
            for n in range(n_nodes):
                vm += (P_node[n] + W_node[n] - mmean) ** 2
                vr += (R_node[n] - rmean) ** 2
            if mmean > 0:
                stats[17] += math.sqrt(vm / n_nodes) / mmean
                stats[18] += mmx / mmean
                stats[27] += 1
            if rmean > 0:
                stats[19] += math.sqrt(vr / n_nodes) / rmean
                stats[20] += rmx / rmean
                stats[28] += 1
            n_acc += 1
            if n_acc == 100:
                at = 0.0
                amx = 0.0
                aover = 0.0
                aspare = 0.0
                for n in range(n_nodes):
                    a = acc[n] / 100.0
                    at += a
                    if a > amx:
                        amx = a
                    if a > cores:
                        aover += a - cores
                    else:
                        aspare += cores - a
                am = at / n_nodes
                if am > 0:
                    va = 0.0
                    for n in range(n_nodes):
                        va += (acc[n] / 100.0 - am) ** 2
                    stats[22] += math.sqrt(va / n_nodes) / am
                    stats[23] += amx / am
                    stats[24] += 1
                stats[25] += aover * 100.0
                stats[26] += min(aover, aspare) * 100.0
                acc[:] = 0.0
                n_acc = 0
        # 4. progress and completion
        for s in range(n_slots):
            if phase[s] == 0:
                if measure:
                    stats[1] += 1
                continue
            if phase[s] == 4:
                continue
            if measure:
                stats[0] += 1
            if phase[s] != 2:
                continue
            c = cmd[s]
            n = node[s]
            if c_heavy[c]:
                tax = f_park[n] * tau_hs + f_spill[n] * tau_hn
            else:
                tax = f_park[n] * tau_ls + f_spill[n] * tau_ln
            if cur_d[s] > 0:
                prog[s] += served[s] / cur_d[s] / (1.0 + tax)
                if measure:
                    stats[2] += served[s]
                    stats[5] += served[s] * tax / (1.0 + tax)
            else:
                prog[s] += 1.0
            if prog[s] >= c_len[c] - 1e-9:
                if start[s] >= ramp_steps and n_rec < max_rec:
                    rec_slow[n_rec] = (step + 1 - start[s]) / c_len[c]
                    rec_cpu[n_rec] = c_cpu[c]
                    rec_heavy[n_rec] = c_heavy[c]
                    n_rec += 1
                W_node[n] -= c_new[c]
                last = tr_first[trace[s]] + tr_count[trace[s]] - 1
                if hwm_model:
                    newpark = max(park[s], c_state[c] + c_new[c])
                elif c < last:
                    newpark = c_state[c + 1]
                else:
                    newpark = park[s]
                P_node[n] += newpark - park[s]
                park[s] = newpark
                if c < last:
                    cmd[s] = c + 1
                    phase[s] = 1
                    cnt[s] = c_gap[c + 1]
                else:
                    P_node[n] -= park[s]
                    park[s] = 0.0
                    r = tr_reserve[trace[s]]
                    if mode == 2:
                        pool_used += _pool_delta(n, -r, R_node, D)
                    R_node[n] -= r
                    nsb[n] -= 1.0
                    pres[n, trace[s]] -= 1
                    if pres[n, trace[s]] == 0:
                        ready[n, trace[s]] = -1
                    node[s] = -1
                    if G > 1:
                        # a group starts its next task only when all siblings are done
                        g = s // G
                        phase[s] = 4
                        gdone[g] += 1
                        if gdone[g] == G:
                            gdone[g] = 0
                            t0 = np.random.randint(n_tr)
                            for i in range(G):
                                s2 = g * G + i
                                phase[s2] = 0
                                trace[s2] = t0
                                cnt[s2] = 50 + (np.random.randint(jitter_steps + 1) if jitter_steps > 0 else 0)
                            for i in range(n_ch):
                                chunk_node[g * n_ch + i] = -1
                    else:
                        phase[s] = 0
                        cnt[s] = 50
                        trace[s] = np.random.randint(n_tr)
    return rec_slow[:n_rec], rec_cpu[:n_rec], rec_heavy[:n_rec], stats


def load_inputs(path, ws_json=None):
    """ws_json: per-trace image working set from image_place.py --per-trace; adds task_mib (task-only layer MiB,
    mean of all traces where a trace is missing)."""
    z = np.load(path)
    ct = z["cmd_trace"]
    n_tr = len(z["names"])
    first = np.searchsorted(ct, np.arange(n_tr))
    count = np.bincount(ct, minlength=n_tr)
    offs = z["offs"]
    demand = z["demand"]
    c_len = np.diff(offs)
    c_cpu = np.add.reduceat(demand, offs[:-1]) * DT
    c_cpu[c_len == 0] = 0.0
    out = dict(tr_first=first.astype(np.int64), tr_count=count.astype(np.int64),
                tr_reserve=z["reserve_gib"].astype(float),
                c_gap=np.ceil(z["gap_s"] / DT - 1e-9).astype(np.int64), c_off=offs[:-1].astype(np.int64),
                c_len=c_len.astype(np.int64), c_state=z["state_gib"].astype(float),
                c_new=np.maximum(0.0, z["peak_gib"] - z["state_gib"]).astype(float), c_cpu=c_cpu,
                c_heavy=c_cpu >= 1.0, c_width=c_cpu / np.maximum(DT, c_len * DT), demand=demand.astype(float))
    if ws_json:
        per = json.loads(Path(ws_json).read_text())
        mean = float(np.mean([v["task_mib"] for v in per.values()]))
        out["task_mib"] = np.array([per.get(str(n), {}).get("task_mib", mean) for n in z["names"]])
    return out


def run(cfg, inputs):
    t0 = time.time()
    move = {"none": (0, 0), "handoff": (1, cfg.get("lat_ms", 10)), "copy": (2, cfg.get("lat_ms", 10))}[cfg["move"]]
    n_slots = int(round(cfg["density"] * cfg["cores"] * cfg["nodes"]))
    group = cfg.get("group", 1)
    if group > 1:
        n_slots = max(group, n_slots // group * group)
    base = {k: v for k, v in inputs.items() if k != "task_mib"}
    if cfg.get("gap_scale", 1) != 1:
        base["c_gap"] = np.ceil(base["c_gap"] * cfg["gap_scale"]).astype(np.int64)
    mib = inputs.get("task_mib")
    read_bps = cfg.get("read_gbps", 0) * 1e9
    ws_steps = (np.ceil(mib * 2**20 / read_bps / DT).astype(np.int64) if mib is not None and read_bps > 0
                else np.zeros(len(base["tr_first"]), np.int64))
    slow, cpu, heavy, st = simulate(
        **base, mode=MODES[cfg["mem"]], n_nodes=cfg["nodes"], cores=cfg["cores"],
        D=cfg["dram_per_core"] * cfg["cores"], X=cfg["cxl_per_core"] * cfg["cores"], n_slots=n_slots,
        seed=cfg["seed"], ramp_steps=int(cfg["ramp_s"] / DT), end_steps=int((cfg["ramp_s"] + cfg["dur_s"]) / DT),
        move_kind=move[0], move_lat_steps=max(1, int(round(move[1] / 1000 / DT))), nic_gib_s=cfg.get("nic_gbps", 100) / 8 * 1e9 / 2**30,
        copy_base_gib=cfg.get("copy_base_gib", 0.1), tau_hs=cfg["tau_hs"], tau_ls=cfg["tau_ls"], tau_hn=cfg["tau_hn"],
        tau_ln=cfg["tau_ln"], margin=1.0, max_rec=4_000_000, hwm_model=cfg.get("resident", "actual") == "hwm",
        place_k=cfg.get("place_k", 2), place_metric=cfg.get("place_metric", 0), group_size=group,
        chunk=cfg.get("chunk", 1), jitter_steps=int(round(cfg.get("jitter_s", 0) / DT)), ws_steps=ws_steps,
        cold_on_move=cfg.get("cold_on_move", True), tax_split=cfg.get("tax_split", False))
    steps = cfg["dur_s"] / DT
    nc = cfg["nodes"] * cfg["cores"]
    hs = slow[heavy]
    out = dict(cfg)
    out.update(
        commands=int(len(slow)),
        cpu_weighted=float((slow * cpu).sum() / max(cpu.sum(), 1e-9)),
        heavy_p50=float(np.quantile(hs, 0.5)) if len(hs) else None,
        heavy_p99=float(np.quantile(hs, 0.99)) if len(hs) else None,
        all_p99=float(np.quantile(slow, 0.99)) if len(slow) else None,
        running_per_core=float(st[0] / steps / nc),
        waiting_per_core=float(st[1] / steps / nc),
        util=float(st[2] / steps / nc),
        tax_cpu_frac=float(st[5] / max(st[2], 1e-9)),
        useful_util=float((st[2] - st[5]) / steps / nc),
        moves_per_s=float(st[3] / cfg["dur_s"]),
        blocked_moves_per_s=float(st[9] / cfg["dur_s"]),
        spill_node_frac=float(st[6] / steps / cfg["nodes"]),
        pool_used_frac=float(st[7] / steps / max(1e-9, cfg["nodes"] * cfg["cxl_per_core"] * cfg["cores"])) if cfg["mem"] == "pool" else None,
        sandboxes_per_s=float(st[8] / cfg["dur_s"]),
        resident_gib_per_core=float(st[10] / steps / nc),
        wall_s=time.time() - t0,
        cpu_over_frac=float(st[11] / max(st[14], 1e-9)),
        cpu_stranded_frac=float(st[12] / max(st[14], 1e-9)),
        over_step_frac=float(st[13] / steps),
        cpu_cv=float(st[15] / max(st[21], 1)),
        cpu_maxmean=float(st[16] / max(st[21], 1)),
        mem_cv=float(st[17] / max(st[27], 1)),
        mem_maxmean=float(st[18] / max(st[27], 1)),
        res_cv=float(st[19] / max(st[28], 1)),
        res_maxmean=float(st[20] / max(st[28], 1)),
        cpu1s_cv=float(st[22] / max(st[24], 1)),
        cpu1s_maxmean=float(st[23] / max(st[24], 1)),
        cpu1s_over_frac=float(st[25] / max(st[14], 1e-9)),
        cpu1s_stranded_frac=float(st[26] / max(st[14], 1e-9)),
        read_s_per_sandbox=float(st[29] * DT / max(st[8], 1)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=str(HERE / ".local/inputs.npz"))
    ap.add_argument("--config", required=True, help="JSON object")
    args = ap.parse_args()
    print(json.dumps(run(json.loads(args.config), load_inputs(args.inputs))))


if __name__ == "__main__":
    main()
