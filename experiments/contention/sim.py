#!/usr/bin/env python3
"""Trace-driven node simulator for the contention experiment (HANDOFF Phase C, first version).

Each sandbox replays a "program" built from its solo run: per-command vCPU demand in 10ms slots (the CPU time
the trace used when it had a core to itself), separated by the trace's recorded LLM waits. Nodes of C cores share
CPU like CFS: when the sandboxes on a node want more than C cores, each gets an equal share up to its demand
(water-filling). Demand not served in a slot is runqueue wait and stretches the command, which delays everything
after it. Optional moves take the busiest sandbox off an overloaded node after a latency L, with the move's own
CPU cost and (for stop-and-copy) the sandbox paused while it moves.

The run-time series is safe to use: unlike schedstat run_delay, a task's run time is accounted while it runs.
"""

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import random
import statistics

import numpy as np

HERE = Path(__file__).resolve().parent
DT = 0.010
SOLO = HERE / ".local/runs/20261020T092617Z-solo-8hjb2n4z"
SANDBOX_GAP_S = 0.5  # harness work between two sandboxes on a slot (disk setup, cleanup)
TIMEOUT_S = 60  # mini's per-command timeout, enforced with SIGKILL by guest-init.sh

# name: (latency_s, pause, source_cpu_s). Source CPU is charged as an extra 1-core task on the source node.
MECHANISMS = {
    "none": None,
    "instant": (0.0, False, 0.0),
    "hand-off 10ms": (0.010, True, 0.002),
    "migration 0.44s 512MiB stop-and-copy": (0.435, True, 0.166),
    "migration 1.42s 2GiB stop-and-copy": (1.417, True, 0.616),
    "migration 1.42s 2GiB pre-copy": (1.417, False, 0.616),
}


def build_programs(solo=SOLO):
    """{trace: [("cpu", demand per 10ms slot) | ("gap", seconds)]} from the solo run."""
    names = [t["name"] for t in json.loads((HERE / "candidates.json").read_text())["traces"]]
    programs = {}
    for name in names:
        directory = solo / name
        if not (directory / "commands.csv").exists():
            continue
        times, runs = [], []
        with (directory / "threads.csv").open() as stream:
            for row in csv.DictReader(stream):
                if row["comm"].startswith("fc_vcpu"):
                    times.append(int(row["t_ns"]))
                    runs.append(int(row["run_ns"]))
        times, runs = np.array(times, dtype=float), np.array(runs, dtype=float)
        with (directory / "threads.csv").open() as stream:
            launch = int(next(csv.DictReader(stream))["t_ns"])
        commands = list(csv.DictReader((directory / "commands.csv").open()))

        def slots(begin, end):
            edges = np.arange(begin, end + DT * 1e9, DT * 1e9)
            if len(edges) < 2:
                edges = np.array([begin, begin + DT * 1e9])
            cumulative = np.interp(edges, times, runs, left=0.0)
            return np.clip(np.diff(cumulative) / (DT * 1e9), 0.0, 1.0)

        starts = [int(c["host_start_ns"]) for c in commands]
        ends = [int(c["host_end_ns"]) for c in commands]
        program = [("cpu", slots(launch, starts[0]))]  # boot
        for i, command in enumerate(commands):
            if i and float(command["gap_s"]) > 0:
                program.append(("gap", float(command["gap_s"])))
            program.append(("cpu", slots(starts[i], starts[i + 1] if i + 1 < len(commands) else ends[i])))
        programs[name] = program
    return names, programs


def share(demands, capacity):
    """CFS-like equal share up to each demand (water-filling)."""
    total = sum(demands)
    if total <= capacity:
        return list(demands)
    order = sorted(range(len(demands)), key=lambda i: demands[i])
    served, left, remaining = [0.0] * len(demands), capacity, len(demands)
    for i in order:
        level = left / remaining
        served[i] = demands[i] if demands[i] <= level else level
        left -= served[i]
        remaining -= 1
    return served


class Sandbox:
    __slots__ = ("program", "segment", "slot", "progress", "gap_left", "start", "expected", "name", "node",
                 "paused_until", "locked_until", "recent", "segment_start", "now")

    def __init__(self, name, program, node, now):
        self.name, self.program, self.node, self.start = name, program, node, now
        self.segment, self.slot, self.progress, self.gap_left = 0, 0, 0.0, 0.0
        self.expected = sum(len(s[1]) * DT if s[0] == "cpu" else s[1] for s in program)
        self.paused_until, self.locked_until, self.recent = -1.0, -1.0, 0.0
        self.now = now
        self._enter()

    def _enter(self):
        while self.segment < len(self.program):
            kind, value = self.program[self.segment]
            if kind == "gap":
                self.gap_left = value
                return
            if len(value):
                self.slot, self.progress, self.segment_start = 0, 0.0, self.now
                return
            self.segment += 1

    @property
    def done(self):
        return self.segment >= len(self.program)

    def want(self):
        """Demand ignoring a pause: what the sandbox would run now."""
        if self.done:
            return 0.0
        kind, value = self.program[self.segment]
        return 0.0 if kind == "gap" else float(value[self.slot])

    def demand(self, now):
        return 0.0 if now < self.paused_until else self.want()

    def advance(self, served, now):
        self.now = now + DT
        if self.done or now < self.paused_until:
            return
        kind, value = self.program[self.segment]
        if kind == "cpu" and self.segment and now + DT - self.segment_start >= TIMEOUT_S:
            self.segment += 1  # the guest agent kills the command
            self._enter()
            return
        if kind == "gap":
            self.gap_left -= DT
            if self.gap_left <= 1e-9:
                self.segment += 1
                self._enter()
            return
        want = value[self.slot]
        self.progress += 1.0 if want <= 0 else served / want
        if self.progress >= 1.0 - 1e-9:
            self.slot += 1
            self.progress = 0.0
            if self.slot >= len(value):
                self.segment += 1
                self._enter()


def simulate(config):
    """One run: per_node sandboxes on each node, `pool` (none/socket/all) sets which cores each node may use."""
    names, programs = config["names"], config["programs"]
    nodes, cores, per_node = config["nodes"], config["cores"], config["per_node"]
    ramp, duration = config["ramp_s"], config["duration_s"]
    mechanism = MECHANISMS[config["mechanism"]]
    pools = {"none": [[n] for n in range(nodes)], "socket": [[0, 1], [2, 3]], "all": [list(range(nodes))]}[config["pool"]]
    pool_of = {n: p for p, members in enumerate(pools) for n in members}
    capacity = [cores * len(members) for members in pools]
    entries = [n for n in names if n in programs]
    rngs, next_start, boxes = {}, {}, {}
    for n in range(nodes):
        for s in range(per_node):
            rng = random.Random(f"{config['seed']}-{n}-{s}")
            rngs[(n, s)] = rng
            next_start[(n, s)] = rng.uniform(0, ramp)
    steps = int((ramp + duration) / DT)
    begin_step = int(ramp / DT)
    wait = run = overhead = paused = paused_want = 0.0
    moves = 0
    finished = []
    tasks = []  # move CPU: [node, remaining_cpu_s]
    pending = []  # [effective_time, slot key, destination, sandbox]
    history = {p: [0.0] * config["detect_steps"] for p in range(len(pools))}
    cooldown = {p: -1.0 for p in range(len(pools))}
    for step in range(steps):
        now = step * DT
        for key, start in list(next_start.items()):
            if start is not None and now >= start:
                name = rngs[key].choice(entries)
                boxes[key] = Sandbox(name, programs[name], key[0], now)
                next_start[key] = None
        for move in [m for m in pending if m[0] <= now]:
            pending.remove(move)
            if boxes.get(move[1]) is move[3]:  # the same sandbox, not a successor in its slot
                move[3].node = move[2]
        demand = {key: box.demand(now) for key, box in boxes.items()}
        by_pool = {p: [] for p in range(len(pools))}
        for key, box in boxes.items():
            by_pool[pool_of[box.node]].append(key)
        served = {}
        measuring = step >= begin_step
        for p, keys in by_pool.items():
            task_ids = [i for i, t in enumerate(tasks) if pool_of[t[0]] == p]
            wants = [demand[k] for k in keys] + [1.0] * len(task_ids)
            got = share(wants, capacity[p])
            for k, g in zip(keys, got):
                served[k] = g
                if measuring:
                    wait += (demand[k] - g) * DT
                    run += g * DT
            for i, g in zip(task_ids, got[len(keys):]):
                tasks[i][1] -= g * DT
                if measuring:
                    overhead += g * DT
            excess = max(0.0, sum(wants) - capacity[p])
            history[p] = history[p][1:] + [excess]
        tasks = [t for t in tasks if t[1] > 1e-9]
        for key, box in list(boxes.items()):
            if measuring and now < box.paused_until:
                paused += DT
                paused_want += box.want() * DT
            box.recent = 0.9 * box.recent + 0.1 * demand[key]
            box.advance(served[key], now)
            if box.done:
                if measuring and box.start >= ramp:
                    finished.append((now + DT - box.start) / box.expected)
                del boxes[key]
                next_start[key] = now + DT + SANDBOX_GAP_S
        if mechanism and config["pool"] == "none":
            latency, pause, source_cpu = mechanism
            load = {p: sum(demand[k] for k in by_pool[p]) for p in by_pool}
            for p in range(len(pools)):
                if cooldown[p] > now or sum(history[p]) / len(history[p]) < config["threshold"]:
                    continue
                spare = [q for q in range(len(pools)) if q != p and load[q] <= capacity[q] - 1]
                candidates = [k for k in by_pool[p] if k in boxes and boxes[k].locked_until <= now]
                if not spare or not candidates:
                    continue
                destination = min(spare, key=lambda q: load[q])
                key = max(candidates, key=lambda k: boxes[k].recent)
                if boxes[key].recent <= 0.05:
                    continue
                box = boxes[key]
                if pause:
                    box.paused_until = now + latency
                if source_cpu:
                    tasks.append([box.node, source_cpu])
                pending.append([now + latency, key, destination, box])
                load[destination] += box.recent
                cooldown[p] = now + latency + config["hold_s"]
                box.locked_until = now + latency + config["hold_s"]
                moves += 1
    q = lambda v, x: float(np.percentile(v, x)) if v else None
    return {k: config[k] for k in ("per_node", "pool", "mechanism", "seed")} | {
        "wait_over_run": wait / run if run else None, "run_cores_per_node": run / duration / nodes,
        "move_cpu_over_run": overhead / run if run else None, "paused_s": paused, "moves": moves,
        "paused_work_over_run": paused_want / run if run else None,
        "lost_over_run": (wait + paused_want) / run if run else None,
        "traces_finished": len(finished), "trace_slowdown_p50": q(finished, 50), "trace_slowdown_p90": q(finished, 90),
        "trace_slowdown_mean": statistics.mean(finished) if finished else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--densities", default="10,14,18,21,24,27")
    parser.add_argument("--seeds", default="1,2,3")
    parser.add_argument("--pools", default="none,socket,all")
    parser.add_argument("--mechanisms", default=",".join(k for k in MECHANISMS if k != "none"),
                        help="Move mechanisms to add on split nodes (comma-separated names from MECHANISMS)")
    parser.add_argument("--detect-ms", type=float, default=50)
    parser.add_argument("--threshold", type=float, default=0.1, help="Mean excess demand (cores) that triggers a move")
    parser.add_argument("--hold-s", type=float, default=1.0)
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--output", type=Path, default=HERE / ".local/report/sim.json")
    args = parser.parse_args()
    names, programs = build_programs()
    base = {"names": names, "programs": programs, "nodes": 4, "cores": 5, "ramp_s": 60, "duration_s": 600,
            "detect_steps": max(1, round(args.detect_ms / 10)), "threshold": args.threshold, "hold_s": args.hold_s}
    configs = []
    for k in map(int, args.densities.split(",")):
        for seed in args.seeds.split(","):
            for pool in args.pools.split(","):
                configs.append(base | {"per_node": k, "seed": seed, "pool": pool, "mechanism": "none"})
            for mechanism in filter(None, args.mechanisms.split(",")):
                configs.append(base | {"per_node": k, "seed": seed, "pool": "none", "mechanism": mechanism})
    with ProcessPoolExecutor(args.jobs) as pool:
        results = list(pool.map(simulate, configs))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"detect_ms": args.detect_ms, "threshold": args.threshold, "hold_s": args.hold_s,
                                       "mechanisms": {k: v for k, v in MECHANISMS.items()}, "results": results}, indent=1))
    for r in results:
        print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})


if __name__ == "__main__":
    main()
