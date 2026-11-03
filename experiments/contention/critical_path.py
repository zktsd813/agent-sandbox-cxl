#!/usr/bin/env python3
"""Critical path of the original RL job (sweagent/iter2-rl-rollouts) from index.jsonl timing
(docs/experiment-plan-tail-placement.md, section 8.5).

Per rollout: launch = first_ts - env_creation_s; env = env_creation_s; llm = sum of tool->assistant gaps;
tool = (last_ts - first_ts) - llm, i.e. tool execution plus agent overhead plus the first LLM call.
Units are training steps (all waves), first waves, and complete GRPO groups of 8. The critical member is the one
that ends last; its components are reported as shares of the unit's span (first launch to last end), and the span
is recomputed with one component of every member scaled, optionally with the LLM time scaled first (faster
inference; launch delays of later waves are left as recorded, so steps are reported at LLM x1 only).
Usage: critical_path.py"""
import json

import numpy as np

import rl_rollouts as rr

COMPONENTS = ("env creation", "LLM", "tool+agent")


def parts(r, scale=(1.0, 1.0, 1.0)):
    env, llm = r["env_creation_s"] or 0.0, r["llm_gap_sum_s"]
    return r["first_ts"] - env, env * scale[0], llm * scale[1], (r["last_ts"] - r["first_ts"] - llm) * scale[2]


def span(members, scale=(1.0, 1.0, 1.0)):
    """(span from the first launch to the last end, critical member) with components scaled."""
    start = min(parts(r)[0] for r in members)
    return max(((lambda p: p[0] + sum(p[1:]) - start)(parts(r, scale)), r) for r in members), start


def report(name, units, llm_scale=1.0):
    shares, more = [], {}
    for members in units:
        base = (1.0, llm_scale, 1.0)
        (s, critical), start = span(members, base)
        launch, *c = parts(critical, base)
        shares.append([(launch - start) / s] + [x / s for x in c])
        for i, n in enumerate(COMPONENTS):
            for x in (1.2, 2.0):
                scaled = list(base)
                scaled[i] *= x
                more.setdefault((n, x), []).append(span(members, tuple(scaled))[0][0] / s - 1)
    a = np.array(shares)
    print(f"{name}, LLM x{llm_scale}: {len(units)} units; critical member share of span p50 (p90): " +
          ", ".join(f"{n} {np.median(a[:, i]):.1%} ({np.percentile(a[:, i], 90):.1%})"
                    for i, n in enumerate(("launch delay",) + COMPONENTS)))
    print("  span increase, every member's component x1.2 / x2, p50 (p90): " +
          "; ".join(f"{n} " + " / ".join(f"{np.median(more[(n, x)]):+.1%} ({np.percentile(more[(n, x)], 90):+.1%})"
                                         for x in (1.2, 2.0)) for n in COMPONENTS))


def main():
    rows = [r for r in rr.load_index() if r["first_ts"] is not None and r["last_ts"] is not None]
    p = np.array([parts(r)[1:] for r in rows])
    total = p.sum(1)
    print(f"{len(rows)} rollouts, time from launch p50 {np.median(total):.0f} s, p90 {np.percentile(total, 90):.0f} s")
    for i, n in enumerate(COMPONENTS):
        print(f"  {n}: {p[:, i].sum() / total.sum():.1%} of all rollout time; per rollout p50 "
              f"{np.median(p[:, i] / total):.1%}, p90 {np.percentile(p[:, i] / total, 90):.1%}; "
              f"seconds p50 {np.median(p[:, i]):.0f}, p90 {np.percentile(p[:, i], 90):.0f}")
    steps = rr.job_steps(rows)
    all_steps = [[r for w in st for r in w] for run in steps.values() for st in run]
    last_wave = sum(1 for run in steps.values() for st in run
                    if len(st) > 1 and max((r for w in st for r in w), key=lambda r: r["last_ts"]) in st[-1])
    print(f"{len(all_steps)} steps; {sum(len(st) > 1 for run in steps.values() for st in run)} with two or more waves; "
          f"critical rollout in the last wave in {last_wave}")
    by_key = {f"{r['instance_id']}_{r['sample']}@{r['shard']}": r for r in rows}
    groups = [[by_key[m] for m in g["members"]] for g in json.loads(rr.GROUPS.read_text())["groups"]
              if len(g["members"]) == 8 and all(m in by_key for m in g["members"])]
    report("steps", all_steps)
    for scale in (1.0, 0.25):
        report("first waves", [st[0] for run in steps.values() for st in run], scale)
        report("groups of 8", groups, scale)


if __name__ == "__main__":
    main()
