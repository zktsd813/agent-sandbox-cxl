#!/usr/bin/env python3
"""Image-affinity placement and host page cache memory (2026-11-18).

Input: the cold working sets of the 264 multilingual traces from `pagecache_tax.py discover`
(old checkout, harness/trace-replay/results/pagecache-discover-20261103): per trace `working-set.npz`
(resident lower-layer pages after a cold replay) and `lowers.json` (the image's layers and files).

Storage is assumed DSec-like: read-only image layers (EROFS) whose page cache every sandbox on a node shares
when it uses the same layer. A page is keyed by (layer diff_id, path, page index). Only page cache memory is
modelled; CPU is not (see README for the CPU side).

Placements of T tasks x G rollouts on N nodes, equal sandbox count per node:
  spread        image-blind (what power-of-k does to images): sandboxes in random order, round robin
  task-b        each task's G rollouts are cut into chunks of b; chunks go to the least-filled node
  lower-forced  sandboxes sorted by lower image, then task, and packed node by node
  repo-forced   the same with the repository instead of the lower image

The lower image of an image is the longest layer chain from the base that at least one other image also has.
"""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

PAGE = 4096
GIB = 2**30
MIX = np.uint64(0x9E3779B97F4A7C15)
CLASSES = ("cross-repo", "repo", "task")


def key64(*parts):
    return int.from_bytes(hashlib.blake2b("\0".join(parts).encode(), digest_size=8).digest(), "little")


def load(data):
    traces, chains, layer_ids = {}, {}, {}
    for line in (data / "discover.jsonl").open():
        r = json.loads(line)
        name = r["trace"]
        npz = data / name / "working-set.npz"
        if not npz.exists() or not r["after"]["pages"]:
            continue
        listing = json.loads((data / name / "lowers.json").read_text())
        diff = listing["diff_ids"]  # top layer first
        files = listing["files"]
        with np.load(npz) as z:
            fidx, page = z["fidx"], z["page"]
        used = np.unique(fidx)
        fkey = np.zeros(len(files), np.uint64)
        flayer = np.zeros(len(files), np.int32)
        for i in used:
            fkey[i] = key64(diff[files[i][0]], files[i][1])
            flayer[i] = layer_ids.setdefault(diff[files[i][0]], len(layer_ids))
        keys = fkey[fidx] ^ (page.astype(np.uint64) * MIX)
        keys, first = np.unique(keys, return_index=True)
        traces[name] = {"keys": keys, "layer": flayer[fidx[first]]}
        chains[name] = tuple(diff[::-1])  # base first
        for d in diff:
            layer_ids.setdefault(d, len(layer_ids))
    return traces, chains, layer_ids


def lower_images(chains):
    prefix = Counter(c[:p] for c in chains.values() for p in range(1, len(c) + 1))
    lower = {}
    for name, c in chains.items():
        p = max(p for p in range(1, len(c) + 1) if prefix[c[:p]] >= 2) if prefix[c[:1]] >= 2 else 0
        lower[name] = c[:p] if p else (name,)
    return lower


def layer_classes(chains, repo, layer_ids):
    repos, tasks = defaultdict(set), defaultdict(set)
    for name, c in chains.items():
        for d in c:
            repos[d].add(repo[name])
            tasks[d].add(name)
    cls = np.zeros(len(layer_ids), np.int8)
    for d, i in layer_ids.items():
        cls[i] = 0 if len(repos[d]) >= 2 else (1 if len(tasks[d]) >= 2 else 2)
    return cls


def node_cache(task_sets, traces, cls):
    """Page cache bytes per node (GiB) and per layer class, from the set of distinct tasks on each node."""
    total, by_class = [], np.zeros(3)
    for tasks in task_sets:
        if not tasks:
            total.append(0.0)
            continue
        keys = np.concatenate([traces[t]["keys"] for t in tasks])
        layers = np.concatenate([traces[t]["layer"] for t in tasks])
        keys, first = np.unique(keys, return_index=True)
        c = np.bincount(cls[layers[first]], minlength=3) * PAGE / GIB
        by_class += c
        total.append(float(c.sum()))
    return np.array(total), by_class


def place(policy, names, G, N, rng, group_of=None, b=1):
    cap = -(-len(names) * G // N)
    nodes = [[] for _ in range(N)]
    if policy == "spread":
        boxes = [t for t in names for _ in range(G)]
        rng.shuffle(boxes)
        for i, t in enumerate(boxes):
            nodes[i % N].append(t)
    elif policy == "task":
        chunks = [(t, b) for t in names for _ in range(G // b)]
        order = rng.permutation(len(chunks))
        fill = np.zeros(N)
        for i in order:
            t, size = chunks[i]
            low = np.flatnonzero(fill == fill.min())
            n = int(rng.choice(low))
            nodes[n].extend([t] * size)
            fill[n] += size
    else:  # forced: pack by group, then task
        groups = sorted(set(group_of.values()), key=str)
        rank = {groups[i]: r for r, i in enumerate(rng.permutation(len(groups)))}
        boxes = sorted((t for t in names for _ in range(G)), key=lambda t: (rank[group_of[t]], t))
        for i, t in enumerate(boxes):
            nodes[min(i // cap, N - 1)].append(t)
    return nodes


def describe(nodes, traces, cls, group_of):
    task_sets = [sorted(set(n)) for n in nodes]
    total, by_class = node_cache(task_sets, traces, cls)
    boxes = sum(len(n) for n in nodes)
    same_task = [max(Counter(n).values()) for n in nodes if n]
    top_lower = [max(Counter(group_of[t] for t in n).values()) / len(n) for n in nodes if n]
    return {"cache_gib": float(total.sum()), "node_max_gib": float(total.max()), "node_min_gib": float(total.min()),
            "per_sandbox_mib": float(total.sum() * 1024 / boxes),
            "by_class_gib": dict(zip(CLASSES, map(float, by_class))),
            "same_task_max_per_node": float(np.mean(same_task)),
            "tasks_per_node": float(np.mean([len(s) for s in task_sets])),
            "top_lower_share": float(np.mean(top_lower))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--rollouts", type=int, default=8)
    ap.add_argument("--nodes", type=int, default=16)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--per-trace", type=Path, help="also write per-trace working set and task-only MiB (JSON)")
    a = ap.parse_args()

    traces, chains, layer_ids = load(a.data)
    names = sorted(traces)
    repo = {n: n.rsplit("-", 1)[0] for n in names}
    lower = lower_images(chains)
    cls = layer_classes(chains, repo, layer_ids)

    ws = np.array([len(traces[n]["keys"]) for n in names]) * PAGE / 2**20
    allk = np.concatenate([traces[n]["keys"] for n in names])
    alll = np.concatenate([traces[n]["layer"] for n in names])
    uk, first = np.unique(allk, return_index=True)
    single = np.bincount(cls[alll[first]], minlength=3) * PAGE / GIB
    per_task_class = np.zeros(3)
    for n in names:
        per_task_class += np.bincount(cls[traces[n]["layer"]], minlength=3) * PAGE / GIB
    result = {
        "tasks": len(names), "repos": len(set(repo.values())), "lower_images": len(set(lower.values())),
        "rollouts": a.rollouts, "nodes": a.nodes, "seeds": a.seeds,
        "working_set_mib": {f"p{q}": float(np.percentile(ws, q)) for q in (10, 50, 90)} | {"mean": float(ws.mean())},
        "sum_over_tasks_gib": dict(zip(CLASSES, map(float, per_task_class))),
        "single_copy_gib": dict(zip(CLASSES, map(float, single))),
        "no_sharing_gib": float(ws.sum() * a.rollouts / 1024),
        "placements": {},
    }
    plans = [("spread", "spread", {}), ("task-2", "task", {"b": 2}), ("task-4", "task", {"b": 4}),
             ("task-8", "task", {"b": 8}), ("lower-forced", "forced", {"group_of": lower}),
             ("repo-forced", "forced", {"group_of": repo})]
    for label, policy, kw in plans:
        runs = [describe(place(policy, names, a.rollouts, a.nodes, np.random.default_rng(s), **kw), traces, cls, lower)
                for s in range(a.seeds)]
        result["placements"][label] = {k: (float(np.mean([r[k] for r in runs])) if not isinstance(runs[0][k], dict)
                                           else {c: float(np.mean([r[k][c] for r in runs])) for c in CLASSES})
                                       for k in runs[0]}
        p = result["placements"][label]
        print(f"{label:13s} cache {p['cache_gib']:7.1f} GiB  per sandbox {p['per_sandbox_mib']:6.1f} MiB  "
              f"node {p['node_min_gib']:.1f}-{p['node_max_gib']:.1f}  same-task max {p['same_task_max_per_node']:.1f}  "
              f"tasks/node {p['tasks_per_node']:.0f}  top lower share {p['top_lower_share']:.2f}  "
              + " ".join(f"{c} {p['by_class_gib'][c]:.1f}" for c in CLASSES), flush=True)
    if a.per_trace:
        per = {}
        for n in names:
            c = np.bincount(cls[traces[n]["layer"]], minlength=3) * PAGE / 2**20
            per[n] = {"ws_mib": float(c.sum()), "task_mib": float(c[2]), "repo": repo[n], "lower": "|".join(lower[n])[-16:]}
        a.per_trace.write_text(json.dumps(per) + "\n")
    a.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "placements"}, indent=2))


if __name__ == "__main__":
    main()
