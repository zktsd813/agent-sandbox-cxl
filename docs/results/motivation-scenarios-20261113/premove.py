"""Can a slow migration hide in the LLM wait before a heavy command?

For each command: the recorded LLM wait before it, its CPU-seconds and solo duration, its kind.
1. CPU share of heavy commands (CPU >= 1 s) whose preceding wait is at least L.
2. How well the *previous* commands predict that the next one is heavy (known at wait start),
   leave-one-trace-out, versus knowing the command string itself (known at command start).
"""
import sys, json, re, importlib.util
from pathlib import Path
import numpy as np

LATEST = Path("/home/ijkim/agent-sandbox-cxl-latest")
OLD = Path("/home/ijkim/agent-sandbox-cxl")
sys.path.insert(0, str(LATEST / "experiments/contention"))
sys.dont_write_bytecode = True
import fleet, cap_sweep as cap

DT = fleet.DT


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


kindmod = load("cmdkind", OLD / "experiments/workload-char/cmdkind.py")
cap.REPO = OLD
cap.DOCKER = OLD / "harness/trace-replay/results/numa-immediate-20261014/node0"
profiles = cap.load_profiles(root=cap.DOCKER)
entries = {t["name"]: t for t in json.loads(cap.CANDIDATES.read_text())["traces"]}

rows = []  # trace, idx, gap, cpu, dur, kind, prev_kind, prev_heavy, any_heavy_before, since_last_heavy
for name, prof in sorted(profiles.items()):
    batches = cap.replay.load_trace(str(OLD / entries[name]["trace"]))["batches"]
    directory = next(d for d in cap.DOCKER.iterdir() if d.is_dir() and d.name.split("-", 1)[-1] == name)
    events = [json.loads(l) for l in (directory / "events.jsonl").open()]
    ids = sorted({e["batch"] for e in events if e["event"] == "command_start"})
    kinds = [kindmod.kind(" ; ".join(a["command"] for a in batches[b]["actions"])) for b in ids]
    prev_kind, prev_heavy, any_heavy = "start", False, False
    for i, ((gap, dem), k) in enumerate(zip(prof["commands"], kinds)):
        cpu = float(dem.sum()) * DT
        dur = len(dem) * DT
        heavy = cpu >= 1.0
        if i > 0:  # skip boot / first command (no LLM wait before it in the replay)
            rows.append((name, i, float(gap), cpu, dur, k, prev_kind, prev_heavy, any_heavy))
        prev_kind, prev_heavy, any_heavy = k, heavy, any_heavy or heavy

names = np.array([r[0] for r in rows])
gap = np.array([r[2] for r in rows])
cpu = np.array([r[3] for r in rows])
dur = np.array([r[4] for r in rows])
kind = np.array([r[5] for r in rows])
pkind = np.array([r[6] for r in rows])
pheavy = np.array([r[7] for r in rows])
anyh = np.array([r[8] for r in rows])
heavy = cpu >= 1.0
out = {"commands": int(len(rows)), "heavy": int(heavy.sum()), "heavy_cpu_share": float(cpu[heavy].sum() / cpu.sum())}

# 1. wait before heavy commands
w = gap[heavy]
c = cpu[heavy]
out["wait_before_heavy_s_p10_p50_p90"] = np.percentile(w, [10, 25, 50, 90]).round(2).tolist()
out["wait_before_heavy_cpu_weighted_p10_p50"] = [float(np.interp(q, np.cumsum(c[np.argsort(w)]) / c.sum(), np.sort(w))) for q in (0.1, 0.25, 0.5)]
out["heavy_cpu_share_with_wait_ge_L"] = {L: float(c[w >= L].sum() / c.sum()) for L in (0.1, 0.3, 1, 3, 10, 30)}
out["heavy_cmd_duration_s_p10_p50_p90"] = np.percentile(dur[heavy], [10, 50, 90]).round(2).tolist()

# 2. predict "next is heavy" at wait start (history only) vs at command start (string)
def loo_rate(keys):
    """P(heavy | key) estimated from the other traces; returns per-row probability."""
    keys = np.array(keys)
    p = np.zeros(len(keys))
    uniq_t = np.unique(names)
    for t in uniq_t:
        test = names == t
        train = ~test
        tab = {}
        for k in np.unique(keys[train]):
            m = train & (keys == k)
            tab[k] = (heavy[m].sum() + 1) / (m.sum() + 2)
        base = heavy[train].mean()
        p[test] = [tab.get(k, base) for k in keys[test]]
    return p


def score(p, thr):
    pred = p >= thr
    tp = pred & heavy
    return dict(threshold=thr, recall=float(tp.sum() / heavy.sum()), precision=float(tp.sum() / max(pred.sum(), 1)),
                cpu_recall=float(cpu[tp].sum() / cpu[heavy].sum()), predicted_share=float(pred.mean()))


hist_key = [f"{a}|{int(b)}|{int(c_)}" for a, b, c_ in zip(pkind, pheavy, anyh)]
p_hist = loo_rate(hist_key)
p_now = np.isin(kind, ("test", "build", "install")).astype(float)
out["predict_at_wait_start_history"] = [score(p_hist, t) for t in (0.2, 0.3, 0.5)]
out["predict_at_command_start_string"] = score(p_now, 0.5)
# transitions: what precedes heavy commands
tr = {}
for k in np.unique(pkind):
    m = pkind == k
    tr[str(k)] = dict(n=int(m.sum()), p_next_heavy=float(heavy[m].mean()), share_of_heavy_cpu=float(cpu[m & heavy].sum() / cpu[heavy].sum()))
out["prev_kind_to_next_heavy"] = dict(sorted(tr.items(), key=lambda x: -x[1]["share_of_heavy_cpu"]))
print(json.dumps(out, indent=1))
json.dump(out, open(sys.argv[1], "w"), indent=1)
