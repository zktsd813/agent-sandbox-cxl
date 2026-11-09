#!/usr/bin/env python3
"""Replay the reward evaluation of public RL rollouts (sweagent/iter2-rl-rollouts) in microVMs.

The trajectories hold only the agent's commands. After the agent submits, the RL job ran a separate evaluation in the
sandbox: install the package, check out the test files, apply the hidden test patch, run the selected tests. Its xtrace
(`set -x`) is in the rewards archive and the test patch is in the raw record, so the script can be rebuilt. Each
evaluation becomes a three-command trace for `sandbox.py solo`, starting from the clean image:
  0  apply the agent's final patch (model_patch)
  1  setup and install (the xtrace lines before the test files are checked out)
  2  setup again (environment does not carry over between commands), test patch, tests, test files restored

  python3 reward_eval.py prepare --out .local/reward-eval
  python3 sandbox.py --candidates .local/reward-eval/candidates.json solo --cores 2-25 --vcpus 8 --node 0 \
      --sibling-offset 64 --harness-cpus 64-65
  python3 reward_eval.py report .local/runs/<solo run> --prep .local/reward-eval --agent .local/runs/<agent solo run>
"""
import argparse
import collections
import csv
import json
import re
import tarfile
from pathlib import Path

import numpy as np

import rl_rollouts as rr

HERE = Path(__file__).resolve().parent
AGENT_SOLO = HERE / ".local/runs/20261104T092752Z-solo-pdkup4sy"  # the same traces' agent commands, 8 vCPU
TIMEOUT_S = 1800
INSTALL = re.compile(r"pip3? install|-m pip install|setup\.py (develop|install)|\bconda install\b")
SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")


def eval_lines(output):
    """Top-level xtrace commands of the evaluation: everything before the test run, the test command, and what
    follows the end marker (test files restored)."""
    lines, state = [], "before"
    for line in output.splitlines():
        if not line.startswith("+ "):
            continue
        cmd = line[2:]
        if state == "before":
            lines.append(cmd)
            if cmd.startswith(": '>>>>> Start Test Output'"):
                state = "test"
        elif state == "test":
            lines.append(cmd)
            state = "running"
        elif state == "running" and cmd.startswith(": '>>>>> End Test Output'"):
            lines.append(cmd)
            state = "after"
        elif state == "after":
            lines.append(cmd)
    return lines if state == "after" else None


def heredoc(path, text):
    return f"cat > {path} <<'SWE_EVAL_EOF'\n{text.rstrip(chr(10))}\nSWE_EVAL_EOF"


def build_commands(record):
    lines = eval_lines(record["reward"]["output"])
    if not lines:
        raise ValueError("no complete evaluation xtrace")
    split = next(i for i, c in enumerate(lines) if c == "cat" or c.startswith("git checkout"))
    setup = [c for c in lines[:split] if not INSTALL.search(c)]
    test = []
    for i, c in enumerate(lines[split:], split):
        if c == "cat":  # the heredoc that wrote the test patch; its file is the next `git apply` argument
            target = next(re.search(r"(/\S+\.diff)", x).group(1) for x in lines[i:] if x.startswith("git apply"))
            test.append(heredoc(target, record["instance"]["test_patch"]))
        else:
            test.append(c)
    patch = record.get("model_patch") or ""
    apply = (heredoc("/tmp/model.patch", patch) + "\ngit apply -v /tmp/model.patch") if patch.strip() else "true"
    return [apply, "\n".join(lines[:split]), "\n".join(setup + test)], len(setup) < split


def summary_counts(text):
    """pytest's final summary line: {passed: n, failed: n, ...}."""
    found = None
    for line in text.splitlines():
        if re.match(r"^=+ .* in [\d.]+s", line.strip()) or re.match(r"^=+ .*(passed|failed|error).*=+$", line.strip()):
            found = line
    if not found:
        return None
    out = collections.Counter()
    for n, k in SUMMARY.findall(found):
        out["errors" if k.startswith("error") else k] += int(n)
    return dict(out)


def cmd_prepare(args):
    out = args.out.resolve()
    (out / "traces").mkdir(parents=True, exist_ok=True)
    entries = {t["name"]: t for t in json.loads((HERE / "candidates-plan.json").read_text())["traces"]}
    names = json.loads((args.agent / "run.json").read_text())["traces"]
    index = {f"{r['instance_id']}_{r['sample']}": r for r in rr.load_index()}
    want = {n: index[n] for n in names if n in index}
    by_shard = collections.defaultdict(dict)
    for n, r in want.items():
        by_shard[r["shard"]][r["member"]] = n
    records = {}
    for shard, members in by_shard.items():
        with tarfile.open(rr.DATA / "raw" / shard) as tar:
            for m in tar:
                if m.name in members:
                    d = json.load(tar.extractfile(m))
                    records[members[m.name]] = {"instance": d["instance"], "model_patch": d.get("model_patch")}
    keys = {(r["run"], n): n for n, r in want.items()}
    with tarfile.open(rr.DATA / "raw/rewards.tar.gz") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith("_rewards.json"):
                run, base = m.name.split("/", 1)
                n = keys.get((run, base[:-len("_rewards.json")]))
                if n in records:
                    records[n]["reward"] = json.load(tar.extractfile(m))
    candidates, skipped = [], {}
    for n, rec in sorted(records.items()):
        if "reward" not in rec:
            skipped[n] = "no reward record"
            continue
        try:
            commands, has_install = build_commands(rec)
        except (ValueError, StopIteration) as error:
            skipped[n] = str(error) or "no test patch target"
            continue
        source = json.loads(Path(entries[n]["trace"]).read_text())
        config = source["info"]["config"]
        config["environment"]["timeout"] = TIMEOUT_S
        messages = []
        for i, c in enumerate(commands):
            messages.append({"role": "assistant", "content": "", "extra": {"actions": [{"command": c}], "timestamp": 2.0 * i}})
            messages.append({"role": "tool", "content": "", "extra": {"returncode": 0, "timestamp": 2.0 * i + 1}})
        trace = out / "traces" / f"{n}-eval.traj.json"
        trace.write_text(json.dumps({"instance_id": f"{n}-eval", "trajectory_format": "reward-eval",
                                     "info": {"config": config, "exit_status": "Evaluated", "submission": ""},
                                     "messages": messages}))
        reward = rec["reward"]
        (out / "traces" / f"{n}-expected.json").write_text(json.dumps({
            **{k: v for k, v in reward.items() if k != "output"}, "has_install": has_install,
            "recorded_counts": summary_counts(reward["output"])}))
        candidates.append({**entries[n], "name": f"{n}-eval", "trace": str(trace), "commands": len(commands)})
    (out / "candidates.json").write_text(json.dumps({"criteria": {"source": "reward_eval.py prepare"},
                                                     "traces": candidates}, indent=1))
    print(f"{len(candidates)} evaluation traces in {out / 'candidates.json'}; skipped {len(skipped)}: {skipped}")


def vcpu_series(directory):
    """(t_ns array, cumulative vCPU run ns summed over the vCPU threads) from threads.csv."""
    acc = collections.defaultdict(int)
    for row in csv.DictReader((directory / "threads.csv").open()):
        if row["comm"].startswith("fc_vcpu"):
            acc[int(row["t_ns"])] += int(row["run_ns"])
    t = np.array(sorted(acc))
    return t, np.array([acc[x] for x in t])


def peak_cores(t, run, t0, t1, window_s=0.1):
    m = (t >= t0) & (t <= t1)
    if m.sum() < 3:
        return None
    tt, rr_ = t[m], run[m]
    best, j = 0.0, 0
    for i in range(len(tt)):
        while tt[i] - tt[j] > window_s * 1e9:
            j += 1
        if tt[i] > tt[j]:
            best = max(best, (rr_[i] - rr_[j]) / (tt[i] - tt[j]))
    return best


def cmd_report(args):
    agent = {r["name"]: r for r in csv.DictReader((args.agent / "solo-summary.csv").open())}
    rows = []
    for d in sorted(p for p in args.run.iterdir() if (p / "commands.csv").exists()):
        name = d.name[:-len("-eval")]
        expected = json.loads((args.prep / "traces" / f"{name}-expected.json").read_text())
        cmds = list(csv.DictReader((d / "commands.csv").open()))
        t, run = vcpu_series(d)
        phase = {}
        for c, label in zip(cmds, ("patch", "install", "test")):
            t0, t1 = int(c["host_start_ns"]), int(c["host_end_ns"])
            phase[label] = {"wall_s": (t1 - t0) / 1e9, "vcpu_s": float(c["vcpu_run_s"] or 0),
                            "rc": int(c["returncode"]), "peak_cores": peak_cores(t, run, t0, t1)}
        test_out = (d / "out" / "002.out").read_text(errors="replace")
        got = summary_counts(test_out)
        rec = expected.get("recorded_counts")
        a = agent.get(name, {})
        rows.append({"name": name, "family": name, "recorded_eval_s": expected["reward_eval_time"],
                     "recorded_tests_passed": expected["tests_passed"], "recorded_counts": rec, "replayed_counts": got,
                     "same_outcome": bool(got and rec and got.get("passed", 0) == rec.get("passed", 0)
                                          and got.get("failed", 0) == rec.get("failed", 0)),
                     "patch_applied": phase["patch"]["rc"] == 0, "has_install": expected["has_install"],
                     "agent_vcpu_s": float(a["vcpu_run_s"]) if a else None,
                     "agent_command_s": float(a["replay_s"]) if a else None, **{f"{k}_{x}": v[x] for k, v in phase.items()
                                                                                for x in ("wall_s", "vcpu_s", "peak_cores")}})
    for r in rows:
        r["eval_wall_s"] = r["install_wall_s"] + r["test_wall_s"]
        r["eval_vcpu_s"] = r["install_vcpu_s"] + r["test_vcpu_s"]
    ok = [r for r in rows if r["same_outcome"]]
    print(f"{len(rows)} evaluations replayed, {sum(r['patch_applied'] for r in rows)} patches applied, "
          f"{len(ok)} with the recorded pass/fail counts")
    print(f"{'trace':45s} {'rec s':>6s} {'wall':>6s} {'inst':>6s} {'test':>6s} {'vCPU s':>7s} {'peak':>5s} "
          f"{'agent vCPU':>10s} {'same':>5s}")
    for r in sorted(rows, key=lambda r: -r["eval_vcpu_s"]):
        print(f"{r['name'][:45]:45s} {r['recorded_eval_s']:6.1f} {r['eval_wall_s']:6.1f} {r['install_wall_s']:6.1f} "
              f"{r['test_wall_s']:6.1f} {r['eval_vcpu_s']:7.1f} {max(r['install_peak_cores'] or 0, r['test_peak_cores'] or 0):5.1f} "
              f"{(r['agent_vcpu_s'] or float('nan')):10.1f} {str(r['same_outcome']):>5s}")
    x = np.array([(r["recorded_eval_s"], r["eval_wall_s"], r["eval_vcpu_s"], r["install_vcpu_s"], r["test_vcpu_s"],
                   r["agent_vcpu_s"] or np.nan) for r in rows])
    summary = {
        "evaluations": len(rows), "same_outcome": len(ok),
        "wall_over_recorded_p50": float(np.median(x[:, 1] / x[:, 0])),
        "eval_vcpu_s_p10_p50_p90": np.percentile(x[:, 2], [10, 50, 90]).tolist(),
        "eval_cores_while_running_p50": float(np.median(x[:, 2] / x[:, 1])),
        "install_share_of_eval_vcpu": float(x[:, 3].sum() / x[:, 2].sum()),
        "eval_over_agent_vcpu_p50_p90": [float(np.nanmedian(x[:, 2] / x[:, 5])), float(np.nanpercentile(x[:, 2] / x[:, 5], 90))],
        "eval_share_of_agent_plus_eval_vcpu": float(np.nansum(x[:, 2]) / (np.nansum(x[:, 2]) + np.nansum(x[:, 5]))),
        "peak_cores_p50_p90": np.percentile([max(r["install_peak_cores"] or 0, r["test_peak_cores"] or 0) for r in rows],
                                            [50, 90]).tolist()}
    print(json.dumps(summary, indent=1))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--agent", type=Path, default=AGENT_SOLO, help="Agent solo run whose traces to evaluate")
    p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("report")
    p.add_argument("run", type=Path)
    p.add_argument("--prep", type=Path, required=True)
    p.add_argument("--agent", type=Path, default=AGENT_SOLO)
    p.add_argument("--out", type=Path)
    p.set_defaults(fn=cmd_report)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
