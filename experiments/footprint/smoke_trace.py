#!/usr/bin/env python3
"""Synthetic trace for the smoke test of memscan inside a real microVM (known memory use, known waits).

Command 0 writes 200 MiB of anonymous memory once and then holds it idle for 25 s: idle tracking should see about
51,200 pages accessed and written in the first round, then the same pages ageing. After it exits, free page reporting
should return them (RssAnon drops). After a 20 s LLM wait, command 1 touches 100 MiB of new memory; command 2 is a
no-op after a 10 s wait. Writes .local/smoke/smoke.traj.json and .local/smoke/candidates-smoke.json (N copies,
image of one RL task).

  smoke_trace.py [--copies 2]
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RL_CANDIDATES = Path("/home/ijkim/agent-sandbox-cxl/experiments/contention/candidates-plan.json")
IMAGES = Path("/home/ijkim/agent-sandbox-cxl/experiments/contention/.local/images")
COMMANDS = [  # (LLM wait before, command, duration)
    (0.0, "python3 -c \"import time; b=bytearray(200<<20)\nfor i in range(0, len(b), 4096): b[i]=1\ntime.sleep(25)\"", 27.0),
    (20.0, "python3 -c \"b=bytearray(100<<20)\nfor i in range(0, len(b), 4096): b[i]=1\"", 1.0),
    (10.0, "echo done", 0.1),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--copies", type=int, default=2)
    args = ap.parse_args()
    base = next(e for e in json.loads(RL_CANDIDATES.read_text())["traces"]
                if (IMAGES / e["image_key"] / "image.json").exists())
    source = json.loads(Path(base["trace"]).read_text())
    out = HERE / ".local/smoke"
    out.mkdir(parents=True, exist_ok=True)
    t, messages = 1_800_000_000.0, []
    for gap, command, duration in COMMANDS:
        t += gap
        messages.append({"role": "assistant", "content": "", "extra": {"actions": [{"command": command}], "timestamp": t}})
        t += duration
        messages.append({"role": "tool", "content": "", "extra": {"returncode": 0, "timestamp": t}})
    trace = {"instance_id": "smoke", "trajectory_format": source.get("trajectory_format"),
             "info": {"config": source["info"]["config"], "exit_status": "smoke", "submission": "", "source": "smoke"},
             "messages": messages}
    path = out / "smoke.traj.json"
    path.write_text(json.dumps(trace, indent=1))
    entries = [{**{k: base[k] for k in ("image", "image_key", "cwd", "memory_mib")}, "name": f"smoke-{i}",
                "trace": str(path), "group": None} for i in range(args.copies)]
    (out / "candidates-smoke.json").write_text(json.dumps({"criteria": "smoke", "traces": entries}, indent=1))
    print(f"{path}\n{out / 'candidates-smoke.json'} ({args.copies} copies, image {base['image_key']})")


if __name__ == "__main__":
    main()
