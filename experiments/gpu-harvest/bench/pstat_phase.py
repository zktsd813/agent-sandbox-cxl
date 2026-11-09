"""Per-phase CPU counters of the GPU worker main threads from pstat-<label>.<pid>.csv (perf stat -I 500 -x,).
usage: pstat_phase.py LABEL RUN_DIR [SKIP_STEPS]; PSTAT_DIR holds the perf stat files (default .). Prints per phase: on-CPU share, effective GHz, IPC, Ginstructions per step.
"""
import glob, json, os, sys
from collections import defaultdict
S = os.environ.get("PSTAT_DIR", ".")
label, run = sys.argv[1], sys.argv[2]
skip = int(sys.argv[3]) if len(sys.argv) > 3 else 1
ph = [json.loads(l) for l in open(f"{run}/phases.jsonl")]
steps = sorted((e["start_ns"], e["end_ns"]) for e in ph if e["name"] == "step" and e["end_ns"])[skip:]
lo = steps[0][0]
wins = defaultdict(list)
for e in ph:
    if e["end_ns"] and e["start_ns"] >= lo and e["name"] in ("gen", "old_log_prob", "update_actor"):
        wins[e["name"]].append((e["start_ns"], e["end_ns"]))
for path in sorted(glob.glob(f"{S}/pstat-{label}.*.csv")):
    pid = path.split(".")[-2]
    t0 = int(open(f"{S}/pstat-{label}.{pid}.start").read())
    rows = defaultdict(dict)
    for line in open(path):
        f = line.strip().split(",")
        if len(f) < 4 or line.startswith("#"):
            continue
        try:
            rows[float(f[0])][f[3]] = float(f[1])
        except ValueError:
            continue
    out = []
    for name in ("gen", "old_log_prob", "update_actor"):
        acc = defaultdict(float); dur = 0.0
        for t, r in rows.items():
            end = t0 + t * 1e9; start = end - 0.5e9
            if any(a <= start and end <= b for a, b in wins[name]):
                for k in ("cycles", "instructions", "task-clock"):
                    acc[k] += r.get(k, 0.0)
                dur += 0.5
        if not dur:
            continue
        busy_s = acc["task-clock"] / 1e3
        n = len(wins[name])
        out.append(f"{name}: oncpu {busy_s / dur:4.0%} GHz {acc['cycles'] / busy_s / 1e9:4.2f} IPC {acc['instructions'] / acc['cycles']:4.2f}"
                   f" Ginstr/step {acc['instructions'] / 1e9 / n * (sum(b - a for a, b in wins[name]) / 1e9) / dur:6.1f}")
    print(f"{label} worker {pid}: " + " | ".join(out))
