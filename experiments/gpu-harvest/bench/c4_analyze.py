"""C4 analysis: per-phase CPU demand of the training tree (by owner), Docker sandbox CPU, node0 borrowable cores,
GPU util and DDR bandwidth. phases.jsonl is empty for C4, so phase windows come from worker.log step timings.
usage: c4_analyze.py RUN_DIR -> prints tables, writes RUN_DIR/c4-analysis.json and c4-timeline.png
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

run = Path(sys.argv[1])
env = json.loads((run / "env.json").read_text())
S = run / "samples"
w2m = lambda wall: env["start_monotonic_ns"] + (wall - env["start_wall"]) * 1e9

# Phase windows from worker.log: the "Saved LoRA adapter" line is the end of save_checkpoint (= step end).
log = (run / "train/attempts/0001/worker.log").read_text()
ends = [datetime.strptime(m, "%Y-%m-%d %H:%M:%S,%f").timestamp()
        for m in re.findall(r"INFO:(\S+ \S+):\[Rank 0\] \[rank-0\]: Saved LoRA adapter", log)]
timing = [dict(re.findall(r"timing_s/(\w+):([\d.]+)", l)) for l in log.splitlines() if " step:" in l and "timing_s/step" in l]
phases = []  # (step, name, start_ns, end_ns)
for k, (end, t) in enumerate(zip(ends, timing), 1):
    t = {a: float(b) for a, b in t.items()}
    e = end
    for name in ("save_checkpoint", "update_actor", "old_log_prob"):
        phases.append((k, name, w2m(e - t[name]), w2m(e))); e -= t[name]
    e -= t["transform_trajectory"]
    phases.append((k, "collect_trajectory", w2m(e - t["collect_trajectory"]), w2m(e)))
first = min(p[2] for p in phases)
last_end = max(p[3] for p in phases)
done = json.loads((run / "done.json").read_text())
phases.append((0, "init", env["start_monotonic_ns"], first))
phases.append((len(ends) + 1, "collect_trajectory (cut)", last_end, w2m(done["end_wall"])))
phases.sort(key=lambda p: p[2])

# CPU per 100 ms sample.
cpu = pd.read_csv(S / "cpu.csv").to_numpy(np.int64)
t = cpu[:, 0]
busy, total = np.diff(cpu[:, 1::2], axis=0), np.diff(cpu[:, 2::2], axis=0)
util = np.where(total > 0, busy / np.maximum(total, 1), 0.0)
node0 = [c for c in range(util.shape[1]) if c < 16 or 32 <= c < 48]
node1 = [c for c in range(util.shape[1]) if c not in node0]
tt = t[1:]
dt = np.diff(t).astype(float)
frame = pd.DataFrame({"t": tt, "node0_busy": util[:, node0].sum(1), "node1_busy": util[:, node1].sum(1)})

procs = {}
for line in open(S / "procs.jsonl"):
    d = json.loads(line); procs[d["pid"]] = d
th = pd.read_csv(S / "threads.csv", usecols=["t_ns", "pid", "comm", "oncpu_ns"])
names = th.groupby("pid")["comm"].agg(lambda s: set(s.unique()))
def owner(pid):
    n = names[pid]; cmd = procs.get(pid, {}).get("cmdline", "")
    if "ray::WorkerDict" in n: return "FSDP worker"
    if n & {"ray::vLLMHttpSe", "VLLM::EngineCor"} or "spawn_main" in cmd: return "vLLM"
    if "cache_images" in cmd or "skopeo" in n: return "image load"
    if "ray::TaskRunner" in n: return "controller"
    return "Ray/other"
th["owner"] = th["pid"].map({p: owner(p) for p in names.index})
idx = pd.Series(np.arange(len(t)), index=t)
th["i"] = th["t_ns"].map(idx)
th = th[th["i"] > 0]
g = th.groupby(["i", "owner"])["oncpu_ns"].sum().unstack(fill_value=0)
OWN = ["FSDP worker", "vLLM", "controller", "image load", "Ray/other"]
for o in OWN:
    col = np.zeros(len(t))
    if o in g: col[g.index.to_numpy()] = g[o].to_numpy()
    frame[o] = col[1:] / dt
frame["train_total"] = frame[OWN].sum(1)

cg = pd.read_csv(S / "cgroups.csv")
cg = cg.sort_values(["cgroup", "t_ns"])
# usage_usec is already the per-sample delta (sampler.py); the first sample of each scope has no previous one.
cg["dt"] = cg.groupby("cgroup")["t_ns"].diff()
cg = cg.dropna()
cg["cores"] = cg["usage_usec"] * 1e3 / cg["dt"]
cgt = cg.groupby("t_ns").agg(docker=("cores", "sum"), containers=("cgroup", "nunique"),
                             active=("cores", lambda s: int((s > 0.05).sum())))
frame = frame.merge(cgt, left_on="t", right_index=True, how="left").fillna({"docker": 0, "containers": 0, "active": 0})

gpu = pd.read_csv(S / "gpu.csv")
gu = gpu.pivot_table(index="t_ns", columns="gpu", values="util")
frame = pd.merge_asof(frame.sort_values("t"), gu.rename(columns=lambda c: f"gpu{c}").reset_index().rename(columns={"t_ns": "t"}),
                      on="t", direction="nearest")

imc_rows = []
with open(S / "imc.csv") as f:
    start = int(f.readline().split("=")[1])
    for line in f:
        p = line.strip().split(",")
        if len(p) > 5 and p[4] == "MiB":
            imc_rows.append((start + int(float(p[0]) * 1e9), p[1], float(p[3])))
imc = pd.DataFrame(imc_rows, columns=["t", "sock", "mib"]).groupby(["t", "sock"])["mib"].sum().unstack(fill_value=0)
imc = (imc / 0.1 / 1024).rename(columns=lambda c: f"ddr_{c}_GBps").reset_index()  # MiB per 100 ms -> GiB/s
frame = pd.merge_asof(frame, imc, on="t", direction="nearest")

def window(a, b):
    return frame[(frame["t"] > a) & (frame["t"] <= b)]
rows = []
for k, name, a, b in phases:
    f = window(a, b)
    if f.empty: continue
    r = {"step": k, "phase": name, "dur_s": round((b - a) / 1e9, 1)}
    for c in OWN + ["train_total", "docker", "node0_busy", "node1_busy"]:
        r[c] = round(f[c].mean(), 2)
    r["train_p99"] = round(f["train_total"].quantile(0.99), 2)
    r["train_max"] = round(f["train_total"].max(), 2)
    r["borrow_p1"] = round(32 - f["train_total"].quantile(0.99), 1)
    r["docker_p99"] = round(f["docker"].quantile(0.99), 2)
    r["docker_max"] = round(f["docker"].max(), 2)
    r["active_mean"] = round(f["active"].mean(), 2)
    r["gpu0"] = round(f["gpu0"].mean()); r["gpu1"] = round(f["gpu1"].mean())
    r["ddr_S0"] = round(f.get("ddr_S0_GBps", pd.Series([np.nan])).mean(), 1)
    rows.append(r)
tab = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print(tab.to_string(index=False))

# Docker per-container burst view over the whole run: per container, fraction of 100 ms samples above 0.5 core,
# and run lengths of contiguous >0.5-core bursts.
runs = []
for name, c in cg.groupby("cgroup"):
    on = (c["cores"].to_numpy() > 0.5).astype(int)
    if not on.any(): continue
    edges = np.diff(np.concatenate([[0], on, [0]]))
    lens = (np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)) * 0.1
    runs.extend(lens)
runs = np.array(runs)
sec = {"n_containers": int(cg["cgroup"].nunique()),
       "burst_runs": int(len(runs)),
       "burst_len_s_p50_p90_p99_max": [round(float(np.percentile(runs, q)), 1) for q in (50, 90, 99)] + [round(float(runs.max()), 1)] if len(runs) else None,
       "docker_core_s_total": round(float((frame["docker"] * 0.1).sum()), 1),
       "train_core_s_total": round(float((frame["train_total"] * 0.1).sum()), 1),
       "wall_s": round(float((frame["t"].iloc[-1] - frame["t"].iloc[0]) / 1e9), 1)}
print(json.dumps(sec))
# Training-demand bursts on node0: contiguous 100 ms samples with train_total > 4 cores.
on = (frame["train_total"].to_numpy() > 4).astype(int)
edges = np.diff(np.concatenate([[0], on, [0]]))
st, en = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
tb = pd.DataFrame({"start_s": (frame["t"].to_numpy()[st] - env["start_monotonic_ns"]) / 1e9, "len_s": (en - st) * 0.1,
                   "mean_cores": [frame["train_total"].iloc[a:b].mean() for a, b in zip(st, en)],
                   "max_cores": [frame["train_total"].iloc[a:b].max() for a, b in zip(st, en)]})
print("training bursts >4 cores:", len(tb))
print(tb[tb["len_s"] >= 1].round(1).to_string(index=False))
json.dump({"phases": rows, "summary": sec, "train_bursts_over4": tb.round(2).to_dict("records")},
          open(run / "c4-analysis.json", "w"), indent=1)
frame.to_parquet(run / "c4-frame.parquet")
