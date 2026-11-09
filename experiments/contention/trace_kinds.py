#!/usr/bin/env python3
"""What sandboxes do, by tool-call kind, and the reward evaluation that follows each rollout
(docs/results/trace-character-20261109/README.md).

  kinds  tool calls by kind (test, install, build, run, edit, git, explore, submit): share of calls, tool wall time
         and CPU; duration; cores while running; where in a rollout the CPU goes; how concentrated it is. Three sets:
           rl-rec  public Python RL job (sweagent/iter2-rl-rollouts), converted traces, recorded durations
           rl-cpu  51 of them replayed alone in an 8 vCPU microVM (vCPU run seconds per command)
           ml-cpu  multilingual SWE-bench (Gemini), 264 traces replayed alone in Docker on DRAM (cgroup CPU)
  eval   reward evaluation of the same RL job (reward_eval_time in the rewards archive): share of rollout time, how
         much it lengthens steps and groups when appended after the last tool call, how many run at once, and its
         time against the agent's own vCPU time for the profiled traces.

  python3 trace_kinds.py kinds --out .local/trace-kinds.json
  python3 trace_kinds.py eval --out .local/trace-eval.json
"""
import argparse
import collections
import csv
import importlib.util
import json
import re
import tarfile
from pathlib import Path

import numpy as np

import rl_rollouts as rr

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RL_TRACES = rr.DATA / "traces"
RL_SOLO = HERE / ".local/runs/20261104T092752Z-solo-pdkup4sy"  # 8 vCPU, one microVM at a time
RL_CANDIDATES = HERE / "candidates-plan.json"
ML_DOCKER = REPO / "harness/trace-replay/results/numa-immediate-20261014/node0"
ML_CANDIDATES = HERE / "candidates-all.json"

spec = importlib.util.spec_from_file_location("replay", REPO / "harness/trace-replay/experiment.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

KINDS = ["test", "install", "build", "run", "edit", "git", "explore", "submit", "other"]
# First match wins, so a command that edits and then runs tests counts as a test.
RULES = [(k, re.compile(p)) for k, p in [
    ("submit", r"complete_task|submit"),
    ("test", r"\b(py\.?test|tox|nosetests|unittest|manage\.py test|runtests|go test|cargo test|cargo nextest|"
             r"(npm|yarn|pnpm) (run )?[\w:-]*test|ant\b[^|;&]*\btest|jest|mocha|vitest|mvn\b[^|;&]*\btest|"
             r"gradlew?\b[^|;&]*\btest|rspec|rake (test|spec)|phpunit|pest\b|ctest|make (check|test)|"
             r"bundle exec (rake|rspec|ruby -itest))"),
    ("install", r"\b(pip3? install|-m pip install|uv (pip|sync|add)|conda install|(npm|pnpm) (install|ci|i)\b|"
                r"yarn( install)?\s*($|&&|;)|apt(-get)? install|gem install|bundle install|"
                r"composer (install|require|update)|go (get|mod)|cargo (add|fetch))"),
    ("build", r"\b(ant|make|cmake|ninja|meson|cargo (build|check|run|clippy)|go (build|run|vet)|mvn|gradlew?|"
              r"(npm|yarn|pnpm) (run )?(build|compile|lint)|tsc|gcc|g\+\+|clang\+?\+?|javac|rustc|"
              r"setup\.py (build|develop))\b"),
    ("run", r"\bpython[0-9.]*\s+(-[^m\s]\S*\s+)*(-c\b|-\s|[\w/.-]+\.py\b|<<)|\bpython[0-9.]*\s+-m\s+(?!pip|pytest)\w"),
    ("run", r"\b(node|ruby|php|java|go run|bash [\w/.-]+\.sh|sh [\w/.-]+\.sh|\./[\w/.-]+)\b"),
    ("edit", r"sed -i|perl -[a-z]*i|cat\s*>|cat\s*<<|\btee\b|apply_patch|git apply|\bpatch\b|str_replace|"
             r">\s*[\w/.-]+\.\w+\s*(<<|$)"),
    ("git", r"^\s*(cd [^&;]+(&&|;)\s*)?git\b"),
    ("explore", r"\b(cat|head|tail|grep|egrep|rg|find|ls|tree|wc|sed -n|awk|nl|less|more|file|stat|du|pwd|echo|"
                r"which|diff|cut|sort|uniq|xargs)\b"),
]]


def kind(command):
    s = command.strip().lower()
    for k, rx in RULES:
        if rx.search(s):
            # a python heredoc or -c that writes files is an edit, not a run
            if k == "run" and re.search(r"write_text|\.write\(|open\([^)]*['\"]w|\.replace\(", s) \
                    and not re.search(r"py\.?test|unittest|assert ", s):
                return "edit"
            return k
    return "other"


def recorded_rl():
    """[[(kind, recorded seconds from the assistant timestamp to the tool timestamp, None)]] per trace."""
    out = []
    for f in sorted(RL_TRACES.glob("*.traj.json")):
        calls, pending = [], None
        for m in json.loads(f.read_text())["messages"]:
            extra = m.get("extra") or {}
            if m["role"] == "assistant" and extra.get("actions"):
                pending = (extra["actions"][0].get("command", ""), extra.get("timestamp"))
            elif m["role"] == "tool" and pending and pending[1] and isinstance(extra.get("timestamp"), (int, float)):
                calls.append((kind(pending[0]), max(0.0, extra["timestamp"] - pending[1]), None))
                pending = None
        out.append(calls)
    return out


def replayed_rl():
    """{trace: [(kind, wall s, vCPU run s)]} from the 8 vCPU solo microVM run."""
    entries = {t["name"]: t for t in json.loads(RL_CANDIDATES.read_text())["traces"]}
    out = {}
    for d in sorted(p for p in RL_SOLO.iterdir() if (p / "commands.csv").exists()):
        batches = replay.load_trace(entries[d.name]["trace"])["batches"]
        out[d.name] = [(kind(batches[int(r["batch"])]["actions"][int(r["action"])].get("command", "")),
                        (int(r["host_end_ns"]) - int(r["host_start_ns"])) / 1e9, float(r["vcpu_run_s"]))
                       for r in csv.DictReader((d / "commands.csv").open())]
    return out


def replayed_ml():
    """{trace: [(kind, wall s, CPU s)]} from the multilingual Docker DRAM run (cgroup CPU, 20 ms samples)."""
    entries = {t["name"]: t for t in json.loads(ML_CANDIDATES.read_text())["traces"]}
    out = {}
    for d in sorted(p for p in ML_DOCKER.iterdir() if p.is_dir()):
        name = d.name.split("-", 1)[1]
        if name not in entries or not (d / "metrics.csv").exists():
            continue
        t, cpu = [], []
        for r in csv.DictReader((d / "metrics.csv").open()):
            if r["phase"] == "running":
                t.append(float(r["elapsed_s"]))
                cpu.append(float(r["cpu_usec"]) / 1e6)
        events = [json.loads(line) for line in (d / "events.jsonl").open()]
        start = {e["batch"]: e["elapsed_s"] for e in events if e["event"] == "command_start"}
        end = {e["batch"]: e["elapsed_s"] for e in events if e["event"] == "command_end"}
        if len(t) < 2 or not start or set(start) != set(end):
            continue
        batches = replay.load_trace(str(REPO / entries[name]["trace"]))["batches"]
        out[name] = [(kind(batches[b]["actions"][0].get("command", "")), end[b] - start[b],
                      max(0.0, float(np.interp(end[b], t, cpu) - np.interp(start[b], t, cpu)))) for b in sorted(start)]
    return out


def kind_table(name, traces):
    rows = [c for calls in traces for c in calls]
    cpu = rows[0][2] is not None
    wall = sum(w for _, w, _ in rows)
    total = sum(c for _, _, c in rows) if cpu else 0.0
    print(f"\n== {name}: {len(traces)} traces, {len(rows)} calls, tool wall {wall / 3600:.1f} h"
          + (f", CPU {total / 3600:.1f} core-h" if cpu else ""))
    print(f"{'kind':8s} {'calls':>6s} {'wall':>6s} " + ("   CPU " if cpu else "") + " dur p50     p90     p99"
          + ("  cores p50   p90" if cpu else ""))
    by = collections.defaultdict(list)
    for k, w, c in rows:
        by[k].append((w, c))
    out = {}
    for k in KINDS:
        if k not in by:
            continue
        w = np.array([x for x, _ in by[k]])
        q = np.percentile(w, [50, 90, 99])
        r = {"calls": len(w) / len(rows), "wall": w.sum() / wall, "dur_p50_p90_p99": q.tolist()}
        line = f"{k:8s} {r['calls']:6.1%} {r['wall']:6.1%} "
        if cpu:
            c = np.array([y for _, y in by[k]])
            cores = c[w > 0.05] / w[w > 0.05]
            r["cpu"] = c.sum() / total
            r["cores_p50_p90"] = [float(np.median(cores)), float(np.percentile(cores, 90))] if len(cores) else [0, 0]
            line += f"{r['cpu']:6.1%} "
        line += f"{q[0]:8.2f} {q[1]:7.2f} {q[2]:7.1f}"
        if cpu:
            line += f" {r['cores_p50_p90'][0]:9.2f} {r['cores_p50_p90'][1]:5.2f}"
        print(line)
        out[k] = r
    if cpu:
        decile = np.zeros(10)
        for calls in traces:
            for i, (_, _, c) in enumerate(calls):
                decile[min(9, 10 * i // len(calls))] += c
        share = lambda f: [f(sorted((c for _, _, c in calls), reverse=True)) / max(sum(c for *_, c in calls), 1e-9)  # noqa: E731
                           for calls in traces]
        top1, top3 = share(lambda s: s[0]), share(lambda s: sum(s[:3]))
        known = [sum(c for k, _, c in calls if k in ("test", "build", "install")) / max(sum(c for *_, c in calls), 1e-9)
                 for calls in traces]
        out["cpu_by_position_decile"] = (decile / decile.sum()).tolist()
        out["per_trace"] = {"top1_p50": float(np.median(top1)), "top3_p50": float(np.median(top3)),
                            "test_build_install_p50": float(np.median(known))}
        print("CPU by position in the rollout (deciles of command index): "
              + " ".join(f"{x:.0%}" for x in out["cpu_by_position_decile"]))
        print(f"per trace: heaviest command {np.median(top1):.0%} of its CPU (p50), top 3 {np.median(top3):.0%}, "
              f"test/build/install {np.median(known):.0%}")
    return out


def cmd_kinds(args):
    rl = replayed_rl()
    report = {"rl-rec": kind_table("Python RL (iter2), recorded durations", recorded_rl()),
              "rl-cpu": kind_table("Python RL, 8 vCPU microVM alone", list(rl.values())),
              "ml-cpu": kind_table("multilingual SWE-bench, Docker alone", list(replayed_ml().values()))}
    index = {f"{r['instance_id']}_{r['sample']}": r for r in rr.load_index()}
    pairs = np.array([(index[n]["last_ts"] - index[n]["first_ts"] - index[n]["llm_gap_sum_s"], sum(w for _, w, _ in c),
                       len(c)) for n, c in rl.items() if n in index])
    report["rl_recorded_vs_replayed"] = {
        "traces": len(pairs), "ratio_p50": float(np.median(pairs[:, 0] / pairs[:, 1])),
        "extra_per_command_s_p50": float(np.median((pairs[:, 0] - pairs[:, 1]) / pairs[:, 2])),
        "replayed_share_of_recorded": float(pairs[:, 1].sum() / pairs[:, 0].sum())}
    print(f"\nrecorded tool+agent vs replayed command time, {len(pairs)} RL traces: "
          + json.dumps(report["rl_recorded_vs_replayed"], default=lambda x: round(x, 3)))
    if args.out:
        args.out.write_text(json.dumps(report, indent=1))


def cmd_eval(args):
    rewards = {}
    with tarfile.open(rr.DATA / "raw/rewards.tar.gz") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith("_rewards.json"):
                d = json.load(tar.extractfile(m))
                run, base = m.name.split("/", 1)
                rewards[(run, base[:-len("_rewards.json")])] = d.get("reward_eval_time")
    rows = [r for r in rr.load_index() if r["first_ts"] is not None and r["last_ts"] is not None]
    for r in rows:
        r["eval"] = rewards.get((r["run"], f"{r['instance_id']}_{r['sample']}"))
    rows = [r for r in rows if isinstance(r["eval"], (int, float))]
    out = {"rollouts": len(rows), "reward_files": len(rewards)}
    print(f"{len(rows)} rollouts with reward_eval_time ({len(rewards)} reward files)")
    parts = {"env creation": np.array([r["env_creation_s"] or 0.0 for r in rows]),
             "LLM": np.array([r["llm_gap_sum_s"] for r in rows]),
             "tool+agent": np.array([r["last_ts"] - r["first_ts"] - r["llm_gap_sum_s"] for r in rows]),
             "reward eval": np.array([r["eval"] for r in rows])}
    total = sum(parts.values())
    for name, x in parts.items():
        q = np.percentile(x, [10, 50, 90, 99])
        out[name] = {"s_p10_p50_p90_p99": q.tolist(), "share": float(x.sum() / total.sum())}
        print(f"  {name:12s} s p10 {q[0]:6.1f} p50 {q[1]:6.1f} p90 {q[2]:6.1f} p99 {q[3]:6.1f} | {x.sum() / total.sum():5.1%}")
    launch = lambda r: r["first_ts"] - (r["env_creation_s"] or 0.0)  # noqa: E731
    for name, units in (("steps", [[r for w in st for r in w] for run in rr.job_steps(rows).values() for st in run]),
                        ("groups of 8", [[by[m] for m in g["members"]] for by in [{f"{r['instance_id']}_{r['sample']}@{r['shard']}": r for r in rows}]
                                         for g in json.loads(rr.GROUPS.read_text())["groups"]
                                         if len(g["members"]) == 8 and all(m in by for m in g["members"])])):
        grow = [(max(r["last_ts"] + r["eval"] for r in u) - min(map(launch, u))) /
                (max(r["last_ts"] for r in u) - min(map(launch, u))) - 1 for u in units]
        out[name] = {"units": len(units), "span_growth_p50_p90": [float(np.median(grow)), float(np.percentile(grow, 90))]}
        print(f"  {name}: {len(units)} units, span longer by the eval p50 {np.median(grow):+.1%}, p90 {np.percentile(grow, 90):+.1%}")
    peak = []
    for run in {r["run"] for r in rows}:
        members = [r for r in rows if r["run"] == run]
        t0 = min(map(launch, members))
        n = int(max(r["last_ts"] + r["eval"] for r in members) - t0) + 2
        alive, evals = np.zeros(n), np.zeros(n)
        for r in members:
            a, b, c = (int(x - t0) for x in (launch(r), r["last_ts"], r["last_ts"] + r["eval"]))
            alive[a:c + 1] += 1
            evals[b:c + 1] += 1
        on = alive > 0
        peak.append({"run": run, "alive_p50": float(np.median(alive[on])), "evals_p50": float(np.median(evals[on])),
                     "evals_p99": float(np.percentile(evals[on], 99)), "evals_max": float(evals.max())})
    out["concurrency"] = peak
    for p in peak:
        print("  " + json.dumps(p))
    solo = {r["name"]: r for r in csv.DictReader((RL_SOLO / "solo-summary.csv").open())}
    by_name = {f"{r['instance_id']}_{r['sample']}": r for r in rows}
    x = np.array([(float(solo[n]["vcpu_run_s"]), by_name[n]["eval"]) for n in solo if n in by_name])
    out["profiled"] = {"traces": len(x), "agent_vcpu_s_p50": float(np.median(x[:, 0])), "eval_s_p50": float(np.median(x[:, 1])),
                       "eval_over_agent_vcpu_p50_p90": [float(np.median(x[:, 1] / x[:, 0])),
                                                         float(np.percentile(x[:, 1] / x[:, 0], 90))]}
    print("  profiled: " + json.dumps(out["profiled"], default=lambda v: round(v, 2)))
    if args.out:
        args.out.write_text(json.dumps(out, indent=1))


def cmd_env(args):
    """What env creation time follows: creations in flight at launch, position in the wave, first use of the image in
    the run, image size, and the spread inside a GRPO group (same image, launched together)."""
    from scipy.stats import spearmanr
    rows = [r for r in rr.load_index() if r["first_ts"] is not None and r["env_creation_s"] is not None]
    sizes = json.loads((rr.DATA / "image-sizes.json").read_text())
    for r in rows:
        r["launch"] = r["first_ts"] - r["env_creation_s"]
    by_run = collections.defaultdict(list)
    for r in rows:
        by_run[r["run"]].append(r)
    for members in by_run.values():
        members.sort(key=lambda r: r["launch"])
        launches = np.array([r["launch"] for r in members])
        readies = np.sort([r["first_ts"] for r in members])
        seen = set()
        for r in members:
            # in flight at this launch: launched before (or at) it and not ready yet
            r["in_flight"] = int(np.searchsorted(launches, r["launch"], "right") - np.searchsorted(readies, r["launch"], "right")) - 1
            r["first_use"] = r["image"] not in seen
            seen.add(r["image"])
    for run, steps in rr.job_steps(rows).items():
        for st in steps:
            for w in st:
                t0 = w[0]["launch"]
                for i, r in enumerate(w):
                    r["wave_rank"], r["since_wave_s"], r["wave_size"] = i / max(1, len(w) - 1), r["launch"] - t0, len(w)
    env = np.array([r["env_creation_s"] for r in rows])
    out = {"rollouts": len(rows), "env_s_p10_p50_p90": np.percentile(env, [10, 50, 90]).tolist()}
    print(f"{len(rows)} rollouts, env creation p10 {np.percentile(env, 10):.0f} s, p50 {np.median(env):.0f}, "
          f"p90 {np.percentile(env, 90):.0f}")

    def binned(name, key, edges=None, q=5):
        sub = [r for r in rows if r.get(key) is not None]
        x = np.array([float(r[key]) for r in sub])
        y = np.array([r["env_creation_s"] for r in sub])
        edges = edges if edges is not None else np.unique(np.quantile(x, np.linspace(0, 1, q + 1)))
        table = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (x >= lo) & ((x < hi) | (hi == edges[-1]) & (x <= hi))
            if m.any():
                table.append({"from": float(lo), "to": float(hi), "n": int(m.sum()), "env_p50": float(np.median(y[m])),
                              "env_p90": float(np.percentile(y[m], 90))})
        rho = spearmanr(x, y)[0]
        out[name] = {"spearman": float(rho), "bins": table}
        print(f"{name}: Spearman {rho:+.2f}; " + "; ".join(f"[{b['from']:.3g}, {b['to']:.3g}] n {b['n']} "
                                                          f"p50 {b['env_p50']:.0f} p90 {b['env_p90']:.0f}" for b in table))

    binned("creations in flight at launch", "in_flight")
    binned("position in the wave (0 first, 1 last)", "wave_rank", np.linspace(0, 1, 6))
    binned("seconds since the wave started", "since_wave_s")
    for r in rows:
        r["size_gib"] = sizes.get(r["image"], {}).get("compressed_gib")
    binned("compressed image GiB (selected 47 images)", "size_gib", q=3)
    first = np.array([r["env_creation_s"] for r in rows if r["first_use"]])
    later = np.array([r["env_creation_s"] for r in rows if not r["first_use"]])
    out["image_first_use_in_run"] = {"n": len(first), "p50": float(np.median(first)), "later_n": len(later),
                                     "later_p50": float(np.median(later))}
    print(f"first use of the image in the run: n {len(first)}, p50 {np.median(first):.0f} s; later uses n {len(later)}, "
          f"p50 {np.median(later):.0f} s")
    by_key = {f"{r['instance_id']}_{r['sample']}@{r['shard']}": r for r in rows}
    spread = [max(by_key[m]["env_creation_s"] for m in g["members"]) - min(by_key[m]["env_creation_s"] for m in g["members"])
              for g in json.loads(rr.GROUPS.read_text())["groups"]
              if len(g["members"]) == 8 and all(m in by_key for m in g["members"])]
    out["group_spread_s_p50_p90"] = [float(np.median(spread)), float(np.percentile(spread, 90))]
    print(f"spread inside a group of 8 (same image, launched together): p50 {np.median(spread):.0f} s, "
          f"p90 {np.percentile(spread, 90):.0f} s")
    if args.out:
        args.out.write_text(json.dumps(out, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("kinds", cmd_kinds), ("eval", cmd_eval), ("env", cmd_env)):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path)
        p.set_defaults(fn=fn)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
