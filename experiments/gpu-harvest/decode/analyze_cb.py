"""Summarize run_cb.sh: engine throughput/CPU with and without sandboxes, and sandbox command slowdown per engine.
usage: analyze_cb.py CB_DIR
Sandbox commands count only if they ended inside the engine's measurement window. Per command key (rollout, idx) the
median elapsed time is compared with the sandbox-alone median for the same key.
"""
import glob, json, os, re, sys
from collections import defaultdict
import numpy as np

d = sys.argv[1]
eng = defaultdict(list)    # (engine, sb) -> RESULT dicts
win = {}
for log in glob.glob(f"{d}/*-sb*-r*.log"):
    run = os.path.basename(log)[:-4]
    m = re.match(r"(.+)-sb(\d)-r(\d)", run)
    res = [json.loads(l[7:]) for l in open(log) if l.startswith("RESULT ")]
    if not res:
        print("no RESULT:", run); continue
    eng[(m[1], int(m[2]))].append(res[0]); win[run] = (res[0]["t_start"], res[0]["t_end"])
for mk in glob.glob(f"{d}/none-sb2-r*.mark"):
    run = os.path.basename(mk)[:-5]; t = float(open(mk).read()); win[run] = (t, t + 120)

print("== engine side (mean over reps)")
keys = ["useful_tok_s", "proc_cpu_cores", "boundary_cpu_ms_per_chunk", "boundary_cpu_ms_p99", "gpu_idle_frac", "wasted_frac"]
for e in ["vllm-sync", "vllm-async", "K8-sync", "K8-overlap"]:
    for sb in (0, 2):
        rs = eng.get((e, sb), [])
        if not rs: continue
        vals = {k: np.mean([r[k] for r in rs]) for k in keys if k in rs[0]}
        print(f"{e:11s} sb{sb} n={len(rs)} " + " ".join(f"{k}={v:.3g}" for k, v in vals.items())
              + "  tok/s per rep=" + str([round(r["useful_tok_s"]) for r in rs]))
    a, b = eng.get((e, 0)), eng.get((e, 2))
    if a and b:
        print(f"{'':11s} tok/s change with sandboxes: {np.mean([r['useful_tok_s'] for r in b]) / np.mean([r['useful_tok_s'] for r in a]) - 1:+.1%}")

print("== sandbox side")
cmds = defaultdict(lambda: defaultdict(list))   # cond -> key -> elapsed list
cpu = defaultdict(list)
nroll = defaultdict(int)
for f in glob.glob(f"{d}/*-sb2-r*.jsonl"):
    run = os.path.basename(f)[:-6]
    if run not in win: continue
    cond = run.rsplit("-r", 1)[0]
    t0, t1 = win[run]
    for l in open(f):
        r = json.loads(l)
        if "elapsed_s" in r and t0 <= r["t"] <= t1:
            cmds[cond][(r["rollout"], r["idx"])].append(r["elapsed_s"])
        elif "container_cpu_s" in r:
            cpu[cond].append(r["container_cpu_s"]); nroll[cond] += 1
base = {k: np.median(v) for k, v in cmds["none-sb2"].items()}
print(f"sandbox alone: {len(base)} command keys, container CPU per rollout median {np.median(cpu['none-sb2']):.2f}s")
for cond in sorted(cmds):
    med = {k: np.median(v) for k, v in cmds[cond].items()}
    common = [k for k in med if k in base]
    ratio = np.array([med[k] / base[k] for k in common])
    heavy = [k for k in common if base[k] >= 0.5]
    n_cmd = sum(len(v) for v in cmds[cond].values())
    print(f"{cond:16s} cmds={n_cmd:4d} keys={len(common):3d} rollouts~{nroll[cond]:3d} "
          f"sum ratio={sum(med[k] for k in common) / sum(base[k] for k in common):.3f} "
          f"median ratio={np.median(ratio):.3f} p90={np.percentile(ratio, 90):.3f} "
          f"heavy(>=0.5s,n={len(heavy)}) sum ratio={sum(med[k] for k in heavy) / max(1e-9, sum(base[k] for k in heavy)):.3f}")
