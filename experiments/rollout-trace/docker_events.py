"""Record Docker container and exec events of our rollouts (M1, M4).

Follows `docker events` for containers that carry the trace.run label of
this run and appends every event, with its nanosecond time, to
<root>/_global/docker_events.jsonl. Other users' containers are not
recorded. `split()` later turns the exec events of one rollout into its
exec.jsonl: one line per command with start, end, exit code.

usage: docker_events.py --root traces --run RUN_ID
       docker_events.py --root traces --split     (after the run)
"""

import argparse
import json
from pathlib import Path

import docker

from docker_safety import LABEL_ROLLOUT, LABEL_RUN
from llm_proxy import rollout_dir


def follow(root, run_id):
    out = Path(root) / "_global"
    out.mkdir(parents=True, exist_ok=True)
    client = docker.from_env()
    events = client.events(decode=True, filters={"type": "container", "label": f"{LABEL_RUN}={run_id}"})
    with (out / "docker_events.jsonl").open("a") as f:
        for ev in events:
            attrs = ev.get("Actor", {}).get("Attributes", {})
            if attrs.get(LABEL_RUN) != run_id:
                continue
            f.write(json.dumps({
                "t_ns": ev.get("timeNano"),
                "action": ev.get("Action") or ev.get("status"),
                "container": ev.get("Actor", {}).get("ID"),
                "rollout": attrs.get(LABEL_ROLLOUT),
                "image": attrs.get("image"),
                "exec_id": attrs.get("execID"),
                "exit_code": attrs.get("exitCode"),
            }) + "\n")
            f.flush()


def split(root):
    """Pair exec_start/exec_die by exec id and write each rollout's exec.jsonl."""
    root = Path(root)
    execs, life = {}, {}
    with (root / "_global" / "docker_events.jsonl").open() as f:
        for line in f:
            e = json.loads(line)
            rid, action = e["rollout"], e["action"]
            if action.startswith("exec_create") or action.startswith("exec_start"):
                cmd = action.split(": ", 1)[1] if ": " in action else ""
                x = execs.setdefault((rid, e["exec_id"]), {"exec_id": e["exec_id"], "cmd": cmd})
                key = "t_create_ns" if action.startswith("exec_create") else "t_start_ns"
                x.setdefault(key, e["t_ns"])
            elif action == "exec_die":
                x = execs.setdefault((rid, e["exec_id"]), {"exec_id": e["exec_id"]})
                x["t_end_ns"] = e["t_ns"]
                x["exit_code"] = int(e["exit_code"]) if e["exit_code"] is not None else None
            else:
                life.setdefault(rid, []).append({"t_ns": e["t_ns"], "action": action,
                                                 "container": e["container"], "image": e["image"]})
    by_rollout = {}
    for (rid, _), x in execs.items():
        by_rollout.setdefault(rid, []).append(x)
    for rid in set(by_rollout) | set(life):
        d = rollout_dir(root, rid)
        d.mkdir(parents=True, exist_ok=True)
        rows = sorted(by_rollout.get(rid, []), key=lambda x: x.get("t_start_ns") or x.get("t_create_ns") or 0)
        with (d / "exec.jsonl").open("w") as f:
            for x in rows:
                f.write(json.dumps(x) + "\n")
        with (d / "container.jsonl").open("w") as f:
            for x in life.get(rid, []):
                f.write(json.dumps(x) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--run")
    ap.add_argument("--split", action="store_true")
    a = ap.parse_args()
    if a.split:
        split(a.root)
    else:
        follow(a.root, a.run)


if __name__ == "__main__":
    main()
