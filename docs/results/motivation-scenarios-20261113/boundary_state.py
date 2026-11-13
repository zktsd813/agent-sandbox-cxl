"""Sandbox state at the start of heavy commands (10-14 multilang Docker, DRAM node 0).

At each command_start take the last 100 ms cgroup sample before it: anon, file (page cache), and the
cumulative block write bytes so far (upper bound on writable-layer growth). Heavy = command CPU >= 1 s.
Report CPU-weighted quantiles and how long a stop-and-copy would take at a given network rate.

  python3 boundary_state.py OUT.json OUT-per-command.csv
"""
import csv, json, sys
from pathlib import Path
import numpy as np

ROOT = Path("/home/ijkim/agent-sandbox-cxl/harness/trace-replay/results/numa-immediate-20261014/node0")
rows = []
mid = []  # (cpu in sample, anon+file+writes) for samples inside heavy commands
percmd = []
for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
    if not (d / "metrics.csv").exists():
        continue
    t, anon, file, wr, cpu = [], [], [], [], []
    with (d / "metrics.csv").open() as f:
        for r in csv.DictReader(f):
            if r["phase"] != "running" or not r["anon_bytes"]:
                continue
            t.append(float(r["elapsed_s"])); anon.append(float(r["anon_bytes"])); file.append(float(r["file_bytes"]))
            wr.append(float(r["io_write_bytes"] or 0)); cpu.append(float(r["cpu_usec"]) / 1e6)
    if len(t) < 2:
        continue
    t, anon, file, wr, cpu = map(np.array, (t, anon, file, wr, cpu))
    ev = [json.loads(l) for l in (d / "events.jsonl").open()]
    start = {e["batch"]: e["elapsed_s"] for e in ev if e["event"] == "command_start"}
    end = {e["batch"]: e["elapsed_s"] for e in ev if e["event"] == "command_end"}
    for b in sorted(start)[1:]:
        if b not in end:
            continue
        i = max(0, np.searchsorted(t, start[b]) - 1)
        c = float(np.interp(end[b], t, cpu) - np.interp(start[b], t, cpu))
        rows.append((d.name, c, anon[i], file[i], wr[i], anon.max(), (anon + file).max()))
        percmd.append((d.name.split("-", 1)[1], b, c, anon[i], file[i], wr[i]))
        if c >= 1.0:
            m = (t >= start[b]) & (t <= end[b])
            idx = np.nonzero(m)[0]
            for j in idx[1:]:
                mid.append((cpu[j] - cpu[j - 1], anon[j], anon[j] + file[j] + wr[j] - wr[idx[0]] + wr[idx[0]]))
a = np.array([r[1:] for r in rows])
c, anon, file, wr, anon_peak, tot_peak = a.T
h = c >= 1.0


def wq(x, w, qs=(0.1, 0.5, 0.9, 0.99)):
    o = np.argsort(x)
    cw = np.cumsum(w[o]) / w.sum()
    return [float(x[o][min(len(x) - 1, np.searchsorted(cw, q))]) for q in qs]


MiB = 2 ** 20
out = {"commands": int(len(rows)), "heavy": int(h.sum()), "traces": len({r[0] for r in rows})}
for name, x in (("anon", anon), ("file_page_cache", file), ("cumulative_block_writes", wr), ("anon_plus_writes", anon + wr),
                ("anon_plus_file", anon + file)):
    out[name + "_MiB_heavy_cpu_weighted_p10_p50_p90_p99"] = [round(v / MiB, 1) for v in wq(x[h], c[h])]
# stop-and-copy time at 100 Gb/s (12.5 GB/s) and 25 Gb/s, CPU-weighted share of heavy CPU that fits in 100 ms / 20 ms
for gbps in (25, 100):
    rate = gbps / 8 * 1e9
    for name, x in (("anon", anon), ("anon_plus_writes", anon + wr), ("anon_plus_file", anon + file)):
        tcopy = x[h] / rate
        out[f"copy_{name}_{gbps}G_fits"] = {f"{int(L*1000)}ms": float(c[h][tcopy <= L].sum() / c[h].sum()) for L in (0.02, 0.1, 0.3, 1.0)}
mm = np.array(mid)
out["mid_heavy_anon_MiB_cpu_weighted_p10_p50_p90_p99"] = [round(v / MiB, 1) for v in wq(mm[:, 1], mm[:, 0])]
out["mid_heavy_anon_file_writes_MiB_cpu_weighted_p10_p50_p90_p99"] = [round(v / MiB, 1) for v in wq(mm[:, 2], mm[:, 0])]
for gbps in (25, 100):
    rate = gbps / 8 * 1e9
    out[f"mid_copy_all_{gbps}G_fits"] = {f"{int(L*1000)}ms": float(mm[mm[:, 2] / rate <= L, 0].sum() / mm[:, 0].sum()) for L in (0.02, 0.1, 0.3, 1.0)}
with open(sys.argv[2], "w") as f:
    w = csv.writer(f); w.writerow(["trace", "batch", "cpu_s", "anon_bytes", "file_bytes", "cum_write_bytes"])
    w.writerows(percmd)
out["trace_peak_anon_MiB_p50_p90"] = [round(v / MiB) for v in np.percentile(np.unique(anon_peak), [50, 90])]
print(json.dumps(out, indent=1))
json.dump(out, open(sys.argv[1], "w"), indent=1)
