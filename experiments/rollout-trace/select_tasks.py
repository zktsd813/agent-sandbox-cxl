"""Pick the W1 task list from R2E-Gym-Subset (plan §6.2).

512 tasks in proportion to each repo's share of the 4,578, and inside them
128 tasks (W1 Qwen3-32B and W2) with the same proportions. Largest
remainder rounding, fixed seed. Writes tasks/W1.jsonl with one line per
task: task id (= docker_image), repo, and whether it is in the 128 subset.

usage: select_tasks.py <R2E-Gym-Subset data dir>
"""

import glob
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

SEED = 20261025


def apportion(counts, total):
    n = sum(counts.values())
    exact = {k: total * v / n for k, v in counts.items()}
    out = {k: int(x) for k, x in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def main():
    files = sorted(glob.glob(f"{sys.argv[1]}/*.parquet"))
    rows = [r for f in files for r in pq.read_table(f, columns=["repo_name", "docker_image"]).to_pylist()]
    by_repo = defaultdict(list)
    for r in rows:
        by_repo[r["repo_name"]].append(r["docker_image"])
    rng = random.Random(SEED)
    counts = {k: len(v) for k, v in by_repo.items()}
    n512, n128 = apportion(counts, 512), apportion(counts, 128)
    out = []
    for repo in sorted(by_repo):
        picked = rng.sample(sorted(by_repo[repo]), n512[repo])
        out += [{"task_id": img, "repo": repo, "shared128": i < n128[repo]} for i, img in enumerate(picked)]
    dest = Path(__file__).resolve().parent / "tasks" / "W1.jsonl"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("".join(json.dumps(r) + "\n" for r in out))
    print(f"{len(rows)} tasks, {len(by_repo)} repos -> {len(out)} ({sum(r['shared128'] for r in out)} shared)")
    for repo in sorted(by_repo, key=lambda k: -counts[k]):
        print(f"  {repo:12s} {counts[repo]:5d} -> {n512[repo]:3d} / {n128[repo]:3d}")


if __name__ == "__main__":
    main()
