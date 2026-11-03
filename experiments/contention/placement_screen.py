#!/usr/bin/env python3
"""Placement screen (docs/experiment-plan-tail-placement.md, section 8): compare sync-plan runs that replay the
same steps under different placements. Per run and step: step time, how much later the step's last rollouts end
than in the reference run (same rollouts), runqueue wait over run per node (cpu1ms.csv), per-node 1-s vCPU demand
p99 and max (vcpu.csv), moves, and image locality computed from where each sandbox ran: distinct (node, image)
pairs = pulls, eager bytes (rootfs size), lazy bytes (the most any sandbox of that image read from its base disk on
that node), and the per-node serialized pull time at 1 and 10 Gbit/s.
Async runs (per-group loop, no --sync-plan; section 8.4) are paired by (group slot, round): group and rollout
latency vs the reference, the reference's slowest 10% of groups, completed rollouts per hour, per-node wait over run,
rollout exec time vs the reference by co-runners on its node during its commands, and pulls with unlimited node cache.
Usage: placement_screen.py REFERENCE_RUN RUN... [--out FILE.json]"""
import argparse
import collections
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def listed(value):
    return value if isinstance(value, list) else json.loads(value.replace("'", '"').replace("None", "null"))


def load(run):
    config = json.loads((run / "run.json").read_text())
    candidates = {e["name"]: e for e in json.loads(Path(config["candidates_file"]).read_text())["traces"]}
    boxes = {}
    for line in (run / "sandboxes.jsonl").open():
        row = json.loads(line)
        boxes[row["vm"]] = row
    steps = [json.loads(line) for line in (run / "steps.jsonl").open()]
    return config, candidates, boxes, steps


def image_bytes(key, cache={}):
    if key not in cache:
        path = HERE / ".local/images" / key / "rootfs.ext4"
        cache[key] = path.stat().st_blocks * 512 if path.exists() else 0
    return cache[key]


def node_cpu(run, nodes, t0, t1):
    """Per-node vCPU demand per second (cores) and runqueue wait / run over [t0, t1)."""
    seconds = int((t1 - t0) // 1_000_000_000) + 1
    demand = np.zeros((nodes, seconds))
    with (run / "vcpu.csv").open() as stream:
        next(stream)
        for line in stream:
            t, _, node, _, run_ns, _ = line.split(",")
            i = (int(t) - t0) // 1_000_000_000
            if 0 <= i < seconds:
                demand[int(node), i] += int(run_ns) / 1e9
    run_ns, wait_ns = np.zeros(nodes), np.zeros(nodes)
    with (run / "cpu1ms.csv").open() as stream:
        header = next(stream).strip().split(",")
        for line in stream:
            row = line.split(",")
            if t0 <= int(row[0]) < t1:
                for n in range(nodes):
                    run_ns[n] += int(row[header.index(f"run{n}")])
                    wait_ns[n] += int(row[header.index(f"wait{n}")])
    return demand, wait_ns / np.maximum(run_ns, 1)


def analyze(run, reference_ends):
    config, candidates, boxes, steps = load(run)
    nodes = len(config["nodes"])
    threads = [len(n["cores"]) for n in config["nodes"]]
    moves = sum(1 for _ in (run / "moves.jsonl").open()) if (run / "moves.jsonl").exists() else 0
    out = {"run": run.name, "condition": (run / "condition.txt").read_text().strip()
           if (run / "condition.txt").exists() else config["placement"],
           "placement": config["placement"], "move": config["move"], "moves": moves, "steps": []}
    by_trace = {b["trace"]: b for b in boxes.values()}
    for s in steps:
        traces, member_node = listed(s["traces"]), listed(s["member_node"])
        begin, end = s["begin_ns"], s["end_ns"]
        ends = {t: (by_trace[t]["end_ns"] - begin) / 1e9 for t in traces if t in by_trace and by_trace[t].get("end_ns")}
        demand, wait_over_run = node_cpu(run, nodes, begin, end)
        live = demand.sum(0) > 0
        pairs = collections.defaultdict(list)
        for t, n in zip(traces, member_node):
            pairs[(n, candidates[t]["image_key"])].append(t)
        eager = collections.Counter()
        lazy = collections.Counter()
        for (n, key), members in pairs.items():
            eager[n] += image_bytes(key)
            reads = [by_trace[t]["mem"][-1][8] * 512 for t in members
                     if t in by_trace and by_trace[t].get("mem") and len(by_trace[t]["mem"][-1]) > 8]
            lazy[n] += max(reads, default=0)
        row = {"step": s["step"], "orig_step": s.get("orig_step"), "duration_s": round((end - begin) / 1e9, 1),
               "members_per_node": dict(sorted(collections.Counter(member_node).items())),
               "wait_over_run_per_node": [round(float(x), 4) for x in wait_over_run],
               "demand_p99_per_node": [round(float(np.percentile(d[live], 99)), 2) for d in demand],
               "demand_max_per_node": [round(float(d.max()), 2) for d in demand], "threads_per_node": threads,
               "pulls": len(pairs), "images": len({k for _, k in pairs}),
               "eager_gib": round(sum(eager.values()) / 2**30, 1), "lazy_gib": round(sum(lazy.values()) / 2**30, 2),
               "eager_pull_s_max_node_1g": round(max(eager.values()) * 8 / 1e9, 0),
               "eager_pull_s_max_node_10g": round(max(eager.values()) * 8 / 1e10, 0),
               "lazy_pull_s_max_node_1g": round(max(lazy.values()) * 8 / 1e9, 1)}
        ref = reference_ends.get(s["step"])
        if ref:
            common = [t for t in ends if t in ref]
            ratio = sorted(ends[t] / ref[t] for t in common if ref[t] > 0)
            late = sorted(common, key=lambda t: ref[t])[-max(1, len(common) // 20):]  # last 5% in the reference
            row["end_ratio_p50_p90_max"] = [round(ratio[len(ratio) // 2], 3), round(ratio[int(len(ratio) * .9)], 3),
                                            round(ratio[-1], 3)]
            row["late5pct_end_ratio_mean"] = round(float(np.mean([ends[t] / ref[t] for t in late])), 3)
        out["steps"].append(row)
        out.setdefault("_ends", {})[s["step"]] = ends
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if json.loads((args.runs[0] / "run.json").read_text())["sync_plan"] is None:
        return async_main(args)
    reference = analyze(args.runs[0], {})
    results = [reference] + [analyze(run, reference["_ends"]) for run in args.runs[1:]]
    ref_steps = {r["step"]: r["duration_s"] for r in reference["steps"]}
    for result in results:
        for row in result["steps"]:
            row["duration_vs_reference"] = round(row["duration_s"] / ref_steps[row["step"]], 3)
        result.pop("_ends")
    if args.out:
        args.out.write_text(json.dumps(results, indent=1))
    for result in results:
        print(f"{result['condition']} (moves {result['moves']})")
        for row in result["steps"]:
            print(f"  step {row['step']}: {row['duration_s']} s (x{row['duration_vs_reference']}), "
                  f"late 5% end x{row.get('late5pct_end_ratio_mean', 1.0)}, "
                  f"wait/run per node {row['wait_over_run_per_node']}, demand p99 {row['demand_p99_per_node']} "
                  f"of {row['threads_per_node']} threads, members {row['members_per_node']}")
            print(f"    pulls {row['pulls']} for {row['images']} images, eager {row['eager_gib']} GiB "
                  f"(max node {row['eager_pull_s_max_node_1g']} s at 1 Gb/s, {row['eager_pull_s_max_node_10g']} s "
                  f"at 10 Gb/s), lazy {row['lazy_gib']} GiB (max node {row['lazy_pull_s_max_node_1g']} s at 1 Gb/s)")


def exec_s(box):
    return sum(c["host_end_ns"] - c["host_start_ns"] for c in box["commands"] if c.get("host_end_ns")) / 1e9


def residency(run, boxes):
    """Per sandbox, [(from_ns, node)] from its start through each move (sandbox records hold the final node)."""
    moves = collections.defaultdict(list)
    if (run / "moves.jsonl").exists():
        for line in (run / "moves.jsonl").open():
            move = json.loads(line)
            moves[move["vm"]].append(move)
    out = {}
    for vm, b in boxes.items():
        ms = sorted(moves[vm], key=lambda m: m["start_ns"])
        out[vm] = [(b["start_ns"], ms[0]["from"] if ms else b["node"])] + [(m["end_ns"], m["to"]) for m in ms]
    return out


def load_async(run):
    config, candidates, boxes, groups = load(run)
    window = (min(g["begin_ns"] for g in groups) + config["ramp_s"] * 1e9,
              min(g["begin_ns"] for g in groups) + (config["ramp_s"] + config["duration_s"]) * 1e9)
    rollouts = {}  # (slot, round, trace) -> sandbox record; members start within the jitter of the group's begin
    late = (config["group_jitter_s"] + 30) * 1e9
    for g in groups:
        for b in boxes.values():
            if b["trace"] in g["traces"] and g["begin_ns"] <= b["start_ns"] <= g["begin_ns"] + late:
                rollouts[(g["group"], g["step"], b["trace"])] = b
    return config, candidates, boxes, groups, window, rollouts


def analyze_async(run, reference):
    config, candidates, boxes, groups, window, rollouts = load_async(run)
    nodes = len(config["nodes"])
    moves = sum(1 for _ in (run / "moves.jsonl").open()) if (run / "moves.jsonl").exists() else 0
    done = {(g["group"], g["step"]): g for g in groups if g["completed"] == g["members"]}
    out = {"run": run.name, "condition": (run / "condition.txt").read_text().strip()
           if (run / "condition.txt").exists() else config["placement"], "moves": moves, "groups_completed": len(done)}
    finished = [b for b in boxes.values() if b.get("completed") and window[0] <= b["end_ns"] < window[1]]
    out["rollouts_per_hour"] = round(len(finished) / ((window[1] - window[0]) / 3.6e12), 1)
    demand, wait_over_run = node_cpu(run, nodes, int(window[0]), int(window[1]))
    live = demand.sum(0) > 0
    out["wait_over_run_per_node"] = [round(float(x), 4) for x in wait_over_run]
    out["demand_p99_per_node"] = [round(float(np.percentile(d[live], 99)), 2) for d in demand]
    stays = residency(run, boxes)
    segments = collections.defaultdict(list)  # node -> (from, to) of every sandbox's stays there
    for vm, b in boxes.items():
        for (t, node), (t_next, _) in zip(stays[vm], stays[vm][1:] + [(b["end_ns"], None)]):
            segments[node].append((t, t_next))
    segments = {n: np.array(s) for n, s in segments.items()}

    def node_at(b, t):
        return [node for start, node in stays[b["vm"]] if start <= t][-1]

    # Pulls at the start node: a hand-off leaves the image (rootfs) with the sandbox's CXL-resident state.
    seen, eager, lazy = {}, collections.Counter(), collections.Counter()
    for b in sorted(boxes.values(), key=lambda b: b["start_ns"]):
        node = stays[b["vm"]][0][1]
        key = (node, candidates[b["trace"]]["image_key"])
        read = b["mem"][-1][8] * 512 if b.get("mem") and len(b["mem"][-1]) > 8 else 0
        if key not in seen:
            seen[key] = read
            eager[node] += image_bytes(key[1])
            lazy[node] += read
        elif read > seen[key]:
            lazy[b["node"]] += read - seen[key]
            seen[key] = read
    out.update({"pulls": len(seen), "images": len({k for _, k in seen}), "eager_gib": round(sum(eager.values()) / 2**30, 1),
                "eager_pull_s_max_node_1g": round(max(eager.values()) * 8 / 1e9),
                "eager_pull_s_max_node_10g": round(max(eager.values()) * 8 / 1e10),
                "lazy_pull_s_max_node_1g": round(max(lazy.values()) * 8 / 1e9, 1)})
    if reference:
        ref_done, ref_rollouts = reference
        common = [k for k in done if k in ref_done and done[k]["traces"] == ref_done[k]["traces"]]
        lat = {k: (done[k]["end_ns"] - done[k]["begin_ns"]) / (ref_done[k]["end_ns"] - ref_done[k]["begin_ns"]) for k in common}
        ratio = sorted(lat.values())
        slow = sorted(common, key=lambda k: ref_done[k]["end_ns"] - ref_done[k]["begin_ns"])[-max(1, len(common) // 10):]
        out["paired_groups"] = len(common)
        out["group_latency_ratio_p50_p90_max"] = [round(ratio[len(ratio) // 2], 3), round(ratio[int(len(ratio) * .9)], 3),
                                                  round(ratio[-1], 3)]
        out["slowest10pct_group_latency_ratio_mean"] = round(float(np.mean([lat[k] for k in slow])), 3)
        rr, bins = [], collections.defaultdict(list)
        for k, b in rollouts.items():
            ref = ref_rollouts.get(k)
            if not (ref and b.get("completed") and ref.get("completed") and k[:2] in lat):
                continue
            rr.append((b["end_ns"] - done[k[:2]]["begin_ns"]) / max(ref["end_ns"] - ref_done[k[:2]]["begin_ns"], 1))
            if exec_s(ref) > 5:
                mids = [((c["host_start_ns"] + c["host_end_ns"]) / 2, c["host_end_ns"] - c["host_start_ns"])
                        for c in b["commands"] if c.get("host_end_ns")]
                counts = []  # other sandboxes on the node this one was on at each command's midpoint
                for m, _ in mids:
                    s = segments[node_at(b, m)]
                    counts.append(((s[:, 0] <= m) & (m < s[:, 1])).sum() - 1)
                co = np.average(counts, weights=[w for _, w in mids]) if mids and sum(w for _, w in mids) else 0
                bins[min(int(co // 15) * 15, 60)].append(exec_s(b) / exec_s(ref))
        rr.sort()
        out["rollout_latency_ratio_p50_p99_max"] = [round(rr[len(rr) // 2], 3), round(rr[int(len(rr) * .99)], 3),
                                                    round(rr[-1], 3)]
        out["exec_ratio_by_corunners"] = {f"{lo}-{lo + 14}" if lo < 60 else "60+":
                                          [len(v), round(float(np.median(v)), 3), round(float(np.percentile(v, 90)), 3)]
                                          for lo, v in sorted(bins.items())}
    return out, (done, rollouts)


def async_main(args):
    reference, ref_data = analyze_async(args.runs[0], None)
    results = [reference] + [analyze_async(run, ref_data)[0] for run in args.runs[1:]]
    if args.out:
        args.out.write_text(json.dumps(results, indent=1))
    for r in results:
        print(f"{r['condition']} (moves {r['moves']}): {r['groups_completed']} groups, {r['rollouts_per_hour']} rollouts/h, "
              f"wait/run per node {r['wait_over_run_per_node']}, demand p99 {r['demand_p99_per_node']}")
        if "paired_groups" in r:
            print(f"  {r['paired_groups']} paired groups: latency ratio p50/p90/max {r['group_latency_ratio_p50_p90_max']}, "
                  f"slowest 10% x{r['slowest10pct_group_latency_ratio_mean']}; rollouts p50/p99/max "
                  f"{r['rollout_latency_ratio_p50_p99_max']}; exec ratio by co-runners [n, p50, p90] {r['exec_ratio_by_corunners']}")
        print(f"  pulls {r['pulls']} for {r['images']} images, eager {r['eager_gib']} GiB (max node "
              f"{r['eager_pull_s_max_node_1g']} s at 1 Gb/s, {r['eager_pull_s_max_node_10g']} s at 10 Gb/s), "
              f"lazy max node {r['lazy_pull_s_max_node_1g']} s at 1 Gb/s")


if __name__ == "__main__":
    main()
