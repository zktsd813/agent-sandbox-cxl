"""CPU-weighted command duration distribution and the share of CPU a dispatch cost C can reach (C <= x% of duration)."""
import sys, json, numpy as np
sys.path.insert(0, "/home/ijkim/agent-sandbox-cxl-latest/experiments/contention")
import fleet, cap_sweep
DT = fleet.DT
OLD = "/home/ijkim/agent-sandbox-cxl/experiments/contention/.local/runs/"
def from_programs(programs):
    d, c = [], []
    for p in programs.values():
        cpu = [x for k, x in p["program"] if k == "cpu"]
        for seg in cpu[1:]:  # skip boot
            d.append(len(seg) * DT); c.append(float(np.sum(seg)) * DT)
    return np.array(d), np.array(c)
work = {}
cap_sweep.REPO = __import__("pathlib").Path("/home/ijkim/agent-sandbox-cxl")
prof = cap_sweep.load_profiles(root="/home/ijkim/agent-sandbox-cxl/harness/trace-replay/results/numa-immediate-20261014/node0")
d, c = [], []
for p in prof.values():
    for _, dem in p["commands"]:
        d.append(len(dem) * DT); c.append(float(dem.sum()) * DT)
work["multilang (10-14 Docker, uncapped)"] = (np.array(d), np.array(c))
work["Python RL 1 vCPU (iter2, 440)"] = from_programs(fleet.load_programs(OLD + "20261026T133107Z-solo-5i8p_9nm", "/home/ijkim/agent-sandbox-cxl-latest/experiments/contention/candidates-rl.json"))
work["Python RL 8 vCPU (51)"] = from_programs(fleet.load_programs(OLD + "20261104T092752Z-solo-pdkup4sy", "/home/ijkim/agent-sandbox-cxl-latest/experiments/contention/candidates-plan.json"))
bins = [0, 0.1, 0.5, 1, 2, 5, 10, 30, 1e9]
costs = [0.001, 0.003, 0.01, 0.05, 0.1, 0.3, 1.0]
out = {}
for name, (d, c) in work.items():
    tot = c.sum()
    share = {f"{bins[i]}-{bins[i+1]}": float(c[(d >= bins[i]) & (d < bins[i+1])].sum() / tot) for i in range(len(bins) - 1)}
    reach5 = {C: float(c[d >= 20 * C].sum() / tot) for C in costs}
    reach2 = {C: float(c[d >= 50 * C].sum() / tot) for C in costs}
    m = c >= 0.1  # commands worth considering
    over = {C: float((c[m] * C / d[m]).sum() / c[m].sum()) for C in costs}
    heavy = c >= 1.0
    out[name] = dict(commands=int(len(d)), cpu_s=float(tot), mean_cores_while_running=float(tot / d.sum()),
                     heavy_cmd_share=float(heavy.mean()), heavy_cpu_share=float(c[heavy].sum() / tot),
                     heavy_dur_p10_p50_p90=np.percentile(d[heavy], [10, 50, 90]).round(2).tolist() if heavy.any() else None,
                     cpu_share_by_duration_s=share, cpu_reachable_at_5pct=reach5, cpu_reachable_at_2pct=reach2,
                     cpu_weighted_overhead_dispatch_all_ge_0p1cpu=over)
print(json.dumps(out, indent=1))
json.dump(out, open(sys.argv[1], "w"), indent=1)
