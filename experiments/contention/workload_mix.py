#!/usr/bin/env python3
"""How much do node-load results depend on the workload? Three checks (docs/workload-diversity-plan.md, 2 절).

  nodes   load shape of two nodes in measured runs: 11-05 Docker pools (cgroup use, 20 ms) and RL microVM runs
          (nodes.csv/vcpu.csv, 10 ms). Correlation between the two nodes at 1 s, its fast part (minus a centered 30 s
          mean), 60 s means, node-sum variance over the sum of per-sandbox variances, sandbox mean and peak cores.
  traces  per-sandbox load by trace set: multilingual SWE-bench (Gemini, 264, by language), the 48 of the pool runs,
          the public Python RL rollouts (Qwen3.5 GRPO, 8 vCPU solo run).
  sim     fleet.py, 2 nodes x 15 cores, uncapped profiles, steady state, per trace set and target use: static,
          10 ms hand-off, 5 s pre-copy migration and one shared pool. `report` prints the recovered share.

  python3 workload_mix.py nodes --docker runs-pools/static runs-extreme/k120-static \
      --microvm 20261030T061203Z-pilot-k96-none-g8-plan0-1-v1-splan-seg0-lprg1qib:15
  python3 workload_mix.py traces
  PYTHONHASHSEED=0 numactl --cpunodebind=0 --membind=0 nice -n 19 python3 workload_mix.py sim --out .local/mix/sim.jsonl
  python3 workload_mix.py report .local/mix/sim.jsonl

`sim` needs PYTHONHASHSEED: fleet.py draws placement from hash(("place", seed)), which Python salts per process. With
3 seeds the recovered shares moved by up to 15 points between processes; with 10 seeds, by up to 9.
"""

import argparse
import collections
import csv
import json
import multiprocessing
import os
from pathlib import Path

import numpy as np

import cap_sweep
import extreme
import fleet
import real_cap

HERE = Path(__file__).resolve().parent
PY_SOLO = HERE / ".local/runs/20261104T092752Z-solo-pdkup4sy"  # public RL rollouts, 8 vCPU, one per image
LANG = {
    "JS/TS": "preactjs babel axios immutable-js mrdoob facebook",
    "Java": "projectlombok apache google javaparser reactivex",
    "Ruby": "rubocop fluent fastlane jekyll faker-ruby jordansissel",
    "Go": "caddyserver gin-gonic prometheus gohugoio hashicorp",
    "PHP": "laravel briannesbitt phpoffice php-cs-fixer",
    "C/C++": "redis jqlang micropython fmtlib nlohmann",
    "Rust": "tokio-rs sharkdp burntsushi astral-sh",
}
OWNER = {o: lang for lang, owners in LANG.items() for o in owners.split()}
SIM_SETS = {"all264": None, "used48": "used", "heavy(Java,Rust,JS,Go)": ("Java", "Rust", "JS/TS", "Go"),
            "C/C++": ("C/C++",), "light(Ruby,PHP)": ("Ruby", "PHP")}
SIM_VARIANTS = [("none", "none", 50, 1.0), ("hand-off 10ms", "none", 50, 1.0), ("migration pre-copy", "none", 500, 5.0),
                ("none", "all", 50, 1.0)]


def coarse(x, n):
    m = len(x) // n * n
    return x[:m].reshape(-1, n).mean(axis=1)


def shape(label, use, members, cores, demand=None):
    """use: {node: cores per 1 s bin}; members: {node: [per-sandbox cores per 1 s bin]}."""
    nodes = sorted(use)[:2]
    a, b = (use[n] for n in nodes)
    hot = [use[n] >= cores - 0.5 for n in nodes]
    one = hot[0] ^ hot[1]
    smooth = lambda x: x - np.convolve(x, np.ones(31) / 31, "same")  # noqa: E731
    r = {"util": np.mean([use[n].mean() for n in nodes]) / cores,
         "corr_1s": np.corrcoef(a, b)[0, 1], "corr_fast": np.corrcoef(smooth(a), smooth(b))[0, 1],
         "corr_60s": np.corrcoef(coarse(a, 60), coarse(b, 60))[0, 1],
         "diff_p50": np.median(np.abs(a - b)), "diff_p90": np.percentile(np.abs(a - b), 90),
         "full": np.mean([h.mean() for h in hot]), "both_full": np.mean(hot[0] & hot[1]),
         "both_full_if_independent": hot[0].mean() * hot[1].mean(), "one_full": one.mean(),
         "other_idle_p50": np.median(np.where(hot[0], cores - b, cores - a)[one]) if one.any() else float("nan")}
    ratios, means, peaks = [], [], []
    for n in nodes:
        s = np.array(members[n])
        ratios.append(s.sum(axis=0).var() / max(1e-12, s.var(axis=1).sum()))
        for x in s:
            on = np.nonzero(x > 0.005)[0]
            if len(on) >= 30:
                means.append(x[on[0]:on[-1] + 1].mean())  # over the sandbox's life, LLM waits included
                peaks.append(x.max())
    r.update(var_over_independent=np.mean(ratios), sandbox_mean_p50=np.median(means),
             sandbox_mean_p90=np.percentile(means, 90), sandbox_peak_p50=np.median(peaks),
             sandbox_peak_p90=np.percentile(peaks, 90))
    if demand is not None:
        over = [demand[n] > cores for n in nodes]
        r.update(over_capacity=np.mean([o.mean() for o in over]), both_over=np.mean(over[0] & over[1]))
    print(label)
    print("  " + ", ".join(f"{k} {v:.3f}" for k, v in r.items()), flush=True)
    return r


def docker_shape(spec):
    runs, name = spec.split("/")
    runs = HERE / ".local/real" / runs
    out = runs / name
    run = real_cap.load_run(out)
    t0 = extreme.start_unix(out)
    lo, hi = run["window"]
    log = list(csv.DictReader((runs / f"{name}-moves.csv").open()))
    _, use, _, per = extreme.pool_grid([(run, t0)], real_cap.POOLS, log, t0 + lo, t0 + hi)
    home = {}
    for m in log:
        if m["event"] == "seen":
            home.setdefault(m["container"], m["from_pool"])
    members = {p: [coarse(u, 50) for s, u in per.items() if home.get(s) == p] for p in real_cap.POOLS}
    return shape(f"docker {spec}", {p: coarse(use[p], 50) for p in real_cap.POOLS}, members, 15)


def microvm_shape(spec):
    name, _, cores = spec.partition(":")
    run_dir = HERE / ".local/runs" / name
    rows = np.loadtxt(run_dir / "nodes.csv", delimiter=",", skiprows=1, usecols=(0, 2, 3, 4, 5))
    t0 = rows[:, 0].min()
    bins = ((rows[:, 0] - t0) // 1e9).astype(int)
    n_bins = bins.max() + 1
    use, wait, live = {}, {}, np.zeros(n_bins)
    for n in (0, 1):
        m = rows[:, 1] == n
        use[n], wait[n] = np.zeros(n_bins), np.zeros(n_bins)
        np.add.at(use[n], bins[m], rows[m, 3] / 1e9)
        np.add.at(wait[n], bins[m], rows[m, 4] / 1e9)
        np.maximum.at(live, bins[m], rows[m, 2])
    on = np.nonzero(live > 0)[0]
    lo, hi = on[0], on[-1] + 1
    vc = np.loadtxt(run_dir / "vcpu.csv", delimiter=",", skiprows=1, usecols=(0, 2, 3, 4))
    vms, index = np.unique(vc[:, 2].astype(int), return_inverse=True)
    series = np.zeros((len(vms), n_bins))
    np.add.at(series, (index, ((vc[:, 0] - t0) // 1e9).astype(int)), vc[:, 3] / 1e9)
    node_of = {}
    for i, n in zip(index, vc[:, 1].astype(int)):
        node_of.setdefault(i, n)
    members = {n: [series[i, lo:hi] for i in range(len(vms)) if node_of[i] == n] for n in (0, 1)}
    return shape(f"microvm {name}", {n: use[n][lo:hi] for n in (0, 1)}, members, int(cores or 15),
                 demand={n: use[n][lo:hi] + wait[n][lo:hi] for n in (0, 1)})


def cmd_nodes(args):
    for spec in args.docker:
        docker_shape(spec)
    for spec in args.microvm:
        microvm_shape(spec)


def used48():
    return {t["name"] for t in json.loads((HERE / ".local/real/traces.json").read_text())}


def cmd_traces(_args):
    profiles = cap_sweep.load_profiles()
    used = used48()
    rows = []
    for name, p in profiles.items():
        demand = np.concatenate([d for _, d in p["commands"]])
        n = len(demand) // 10 * 10
        cpu, tool = demand.sum() * cap_sweep.DT, len(demand) * cap_sweep.DT
        gap = sum(g for g, _ in p["commands"])
        rows.append({"lang": OWNER[name.split("__")[0]], "used": name in used, "mean": cpu / (tool + gap),
                     "share": tool / (tool + gap), "gap_turn": gap / max(1, len(p["commands"]) - 1),
                     "peak": demand[:n].reshape(-1, 10).mean(axis=1).max() if n else demand.max(),
                     "above4": demand[demand > 4].sum() * cap_sweep.DT / max(cpu, 1e-9), "cpu": cpu,
                     "time": tool + gap + fleet.SANDBOX_GAP_S})
    py = []
    for r in csv.DictReader((PY_SOLO / "solo-summary.csv").open()):
        tool, gap = float(r["replay_s"]), float(r["recorded_gap_s"])
        py.append({"mean": float(r["vcpu_run_s"]) / (tool + gap), "share": tool / (tool + gap),
                   "gap_turn": gap / max(1, int(r["commands"]) - 1), "cpu": float(r["vcpu_run_s"]),
                   "time": tool + gap + fleet.SANDBOX_GAP_S})

    def line(label, xs):
        q = lambda k, p: np.percentile([x[k] for x in xs], p)  # noqa: E731
        demand = sum(x["cpu"] for x in xs) / sum(x["time"] for x in xs)
        s = (f"{label:<24} n={len(xs):>3}  sandbox mean cores p50 {q('mean', 50):.3f} p90 {q('mean', 90):.3f} | "
             f"tool share p50 {q('share', 50):.0%} | LLM gap per turn p50 {q('gap_turn', 50):.1f} s | "
             f"aggregate demand {demand:.3f} -> 70% of 15 cores at {10.5 / demand:.0f} per node")
        if "peak" in xs[0]:
            s += f" | 100 ms peak p50 {q('peak', 50):.1f} | CPU above 4 cores {np.mean([x['above4'] for x in xs]):.0%}"
        print(s)

    line("multilingual all", rows)
    line("  used in pool runs", [r for r in rows if r["used"]])
    for lang in LANG:
        line(f"  {lang}", [r for r in rows if r["lang"] == lang])
    line("Python RL (Qwen GRPO)", py)


_JOB = {}


def _simulate(config):
    arrays, names = _JOB[config["set"]]
    return fleet.simulate({"arrays": arrays, "names": names, "config": config, "groups": [], "images": names,
                           "blocks": None})


def cmd_sim(args):
    if "PYTHONHASHSEED" not in os.environ:
        raise SystemExit("set PYTHONHASHSEED (fleet.py places sandboxes with hash(), salted per process)")
    profiles = cap_sweep.load_profiles()
    used = used48()
    configs = []
    for label, pick in SIM_SETS.items():
        names = sorted(n for n in profiles if pick is None or (n in used if pick == "used"
                                                               else OWNER[n.split("__")[0]] in pick))
        sub = {n: profiles[n] for n in names}
        programs, base = cap_sweep.programs_for(sub, "cap30")
        arrays = fleet.flatten(programs, names)
        arrays["trace_cpu_s"] = np.array([base[n] for n in names])
        _JOB[label] = (arrays, names)
        demand = cap_sweep.solo_summary(sub, "cap30")["mean_demand_cores"]
        for util in args.util:
            for mechanism, pool, detect_ms, hold_s in SIM_VARIANTS:
                for seed in range(1, args.seeds + 1):
                    configs.append(fleet.DEFAULTS | dict(
                        set=label, policy="cap30", nodes=2, cores=15, per_node=max(1, round(util / 100 * 15 / demand)),
                        floating=True, pool=pool, seed=str(seed), ramp_s=120, duration_s=1800, target_util=util,
                        carry=True, max_batch=8, mechanism=mechanism, nic_gbps=100, detect_ms=detect_ms,
                        hold_s=hold_s, demand_per_sandbox=demand))
    fleet.TIMEOUT_S = 10 ** 6  # commands are not cut at 60 s, as in cap_sweep.py
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with multiprocessing.get_context("fork").Pool(args.jobs) as pool, args.out.open("w") as out:
        for r in pool.imap_unordered(_simulate, configs):
            out.write(json.dumps({k: v for k, v in r.items() if k != "rho"}) + "\n")
    print(f"{len(configs)} runs -> {args.out}")


def cmd_report(args):
    groups = collections.defaultdict(list)
    for line in args.results.open():
        r = json.loads(line)
        groups[(r["set"], r["target_util"], r["mechanism"], r["pool"])].append(r)
    mean = lambda key, k: float(np.mean([r[k] for r in groups[key]]))  # noqa: E731
    print(f"{'set':<24}{'util':>5}{'per node':>9}{'busy':>6}{'lost static':>12}{'lost shared':>12}"
          f"{'hand-off':>9}{'5 s mig':>8}{'trace slowdown st / HO / sh':>30}")
    for s in SIM_SETS:
        for u in sorted({k[1] for k in groups if k[0] == s}):
            st, sh = (s, u, "none", "none"), (s, u, "none", "all")
            ho, mg = (s, u, "hand-off 10ms", "none"), (s, u, "migration pre-copy", "none")
            gap = mean(st, "lost_over_run") - mean(sh, "lost_over_run")
            rec = lambda key: (mean(st, "lost_over_run") - mean(key, "lost_over_run")) / gap  # noqa: E731
            print(f"{s:<24}{u:>5}{groups[st][0]['per_node']:>9}{mean(st, 'run_cores_per_node') / 15:>6.2f}"
                  f"{mean(st, 'lost_over_run'):>12.3f}{mean(sh, 'lost_over_run'):>12.3f}{rec(ho):>9.0%}{rec(mg):>8.0%}"
                  f"{mean(st, 'trace_slowdown_mean'):>14.3f} / {mean(ho, 'trace_slowdown_mean'):.3f} / "
                  f"{mean(sh, 'trace_slowdown_mean'):.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("nodes")
    p.add_argument("--docker", nargs="*", default=[], help="<runs dir under .local/real>/<condition>")
    p.add_argument("--microvm", nargs="*", default=[], help="<run dir under .local/runs>[:cores per node]")
    p.set_defaults(func=cmd_nodes)
    sub.add_parser("traces").set_defaults(func=cmd_traces)
    p = sub.add_parser("sim")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--util", type=int, nargs="+", default=[50, 70, 85])
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--jobs", type=int, default=24)
    p.set_defaults(func=cmd_sim)
    p = sub.add_parser("report")
    p.add_argument("results", type=Path)
    p.set_defaults(func=cmd_report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
