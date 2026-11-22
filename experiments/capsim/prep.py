"""Per-trace inputs for the capacity simulator: 10 ms CPU demand per command (cap_sweep cap30 profile), recorded LLM
gaps, and per-command memory from the same 10-14 Docker DRAM replay: memory at command start (state) and peak during
the command. Writes one npz."""
import csv, importlib.util, json, math, sys
from pathlib import Path
import numpy as np

LATEST = Path("/home/ijkim/agent-sandbox-cxl-latest")
DOCKER = Path("/home/ijkim/agent-sandbox-cxl/harness/trace-replay/results/numa-immediate-20261014/node0")
sys.path.insert(0, str(LATEST / "experiments/contention"))
import cap_sweep  # noqa: E402

DT = 0.010
GIB = 1024 ** 3


def memory_rows(d):
    t, mem = [], []
    with (d / "metrics.csv").open() as s:
        for row in csv.DictReader(s):
            if row["phase"] == "running" and row["memory_bytes"]:
                t.append(float(row["elapsed_s"])); mem.append(int(row["memory_bytes"]))
    return np.array(t), np.array(mem, dtype=float)


def main(out):
    cap_sweep.REPO = Path("/home/ijkim/agent-sandbox-cxl")  # traces live in the old checkout (read only)
    profiles = cap_sweep.load_profiles(root=DOCKER)
    entries = {t["name"]: t for t in json.loads(cap_sweep.CANDIDATES.read_text())["traces"]}
    names, reserve, cmd_trace, gap, state, peak, offs, demand = [], [], [], [], [], [], [0], []
    for d in sorted(p for p in DOCKER.iterdir() if p.is_dir()):
        name = d.name.split("-", 1)[1]
        if name not in profiles:
            continue
        t, mem = memory_rows(d)
        events = [json.loads(l) for l in (d / "events.jsonl").open()]
        start = {e["batch"]: e["elapsed_s"] for e in events if e["event"] == "command_start"}
        end = {e["batch"]: e["elapsed_s"] for e in events if e["event"] == "command_end"}
        cmds = profiles[name]["commands"]
        assert len(cmds) == len(start)
        ti = len(names)
        names.append(name); reserve.append(entries[name]["memory_mib"] / 1024)
        for (g, dem), b in zip(cmds, sorted(start)):
            s0 = float(np.interp(start[b], t, mem))
            inside = mem[(t >= start[b]) & (t <= end[b])]
            pk = max(s0, float(np.interp(end[b], t, mem)), float(inside.max()) if len(inside) else 0.0)
            cmd_trace.append(ti); gap.append(g); state.append(s0 / GIB); peak.append(pk / GIB)
            demand.append(dem); offs.append(offs[-1] + len(dem))
    np.savez_compressed(out, names=np.array(names), reserve_gib=np.array(reserve), cmd_trace=np.array(cmd_trace),
                        gap_s=np.array(gap), state_gib=np.array(state), peak_gib=np.array(peak),
                        offs=np.array(offs), demand=np.concatenate(demand))
    st, pk, res = np.array(state), np.array(peak), np.array(reserve)
    cpu = np.array([x.sum() * DT for x in demand])
    heavy = cpu >= 1.0
    print(f"traces {len(names)} commands {len(cpu)} heavy {heavy.sum()} ({cpu[heavy].sum()/cpu.sum():.1%} CPU)")
    print(f"reserve GiB mean {res.mean():.2f} median {np.median(res):.2f}")
    print(f"state GiB median {np.median(st):.3f} p90 {np.quantile(st,.9):.3f} max {st.max():.2f}")
    w = pk - st
    print(f"heavy new GiB median {np.median(w[heavy]):.3f} p90 {np.quantile(w[heavy],.9):.2f} max {w[heavy].max():.2f}")
    print(f"trace peak/reserve median {np.median([pk[np.array(cmd_trace)==i].max()/res[i] for i in range(len(names))]):.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
