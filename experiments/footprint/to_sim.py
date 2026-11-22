#!/usr/bin/env python3
"""Summaries and simulator inputs from footprint runs (fill.py + memscan.py outputs).

  to_sim.py steps RUN [--baseline RUN1] [--json OUT]
                                         per ramp step: live sandboxes, CPU use, memory per sandbox, admission
                                         blocked (memory ceiling), thrashing signs, command slowdown vs the first step
  to_sim.py export RUN OUT.npz           per sandbox 1 s series (RssAnon, vCPU+VMM CPU) and command events, the
                                         input of the capacity simulator
  to_sim.py snapshot RUN [--json OUT]   per LLM wait of scanned sandboxes: wait length, pages present before the wait
                                         that the next command touches (re-read after a restore), pages written over
                                         wait + command (diff snapshot); totals for "snapshot waits longer than T"
  to_sim.py memory RUN [--json OUT]     memscan: hot/cold split, written rate, compression ratio, duplicate pages

Definitions
- Step window: from the step start + --settle-s to the next step start (the last step: to the run end).
- Slowdown of a command: its host time divided by the baseline of the same (trace, command index), the median over the
  first step (fewest sandboxes, CPU use below half). Commands without a first-step baseline are skipped.
  CPU-weighted slowdown = sum of times / sum of baselines over commands with baseline >= 50 ms; heavy p99 = 99th
  percentile of the slowdown over commands with baseline >= 1 s (the SLO of the 11-16/11-18 simulator).
- Memory ceiling: a step where admission was blocked (live below target with nothing pending) for more than half of
  the window; the live count there is the ceiling.
"""

import argparse
import collections
import csv
import json
from pathlib import Path
import re
import sys

import numpy as np

GIB = 1 << 30


def load_csv(path, types=float):
    with open(path) as stream:
        reader = csv.reader(stream)
        header = next(reader)
        rows = [r for r in reader if len(r) == len(header)]
    out = {}
    for i, name in enumerate(header):
        col = [r[i] for r in rows]
        try:
            out[name] = np.array([float(x) if x != "" else np.nan for x in col])
        except ValueError:
            out[name] = np.array(col)
    return out


def load_run(run):
    run = Path(run)
    meta = json.loads((run / "run.json").read_text())
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines() if l.strip()]
    boxes = [json.loads(l) for l in (run / "sandboxes.jsonl").read_text().splitlines() if l.strip()]
    return run, meta, steps, boxes


def ramp_windows(meta, steps, node, settle_s):
    ramps = [s for s in steps if s["kind"] == "ramp"]
    end = node["t_ns"][-1] if len(node["t_ns"]) else 0
    out = []
    for i, s in enumerate(ramps):
        stop = ramps[i + 1]["t_ns"] if i + 1 < len(ramps) else end
        out.append((s["target"], s["t_ns"] + settle_s * 1e9, stop))
    return out


NETWORK = re.compile(r"\b(pip3?|uv|poetry|conda|apt(-get)?|npm|yarn|pnpm|gem|bundle|composer|cargo\s+(fetch|install|update)"
                     r"|go\s+(get|mod\s+download|install)|curl|wget|git\s+(clone|fetch|pull)|mvn|gradle)\b")


def command_texts(meta, boxes):
    """(trace name, index) -> command text, from the traces in the run's candidates file."""
    try:
        entries = {e["name"]: e for e in json.loads(Path(meta["candidates"]).read_text())["traces"]}
    except (OSError, KeyError, ValueError):
        return {}
    roots = [Path("/home/ijkim/agent-sandbox-cxl-latest"), Path("/home/ijkim/agent-sandbox-cxl")]
    out = {}
    for name in {b["trace"] for b in boxes}:
        e = entries.get(name)
        if not e:
            continue
        path = Path(e["trace"])
        path = path if path.is_absolute() else next((r / path for r in roots if (r / path).exists()), path)
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        texts = [a["command"] for m in raw["messages"] if m["role"] == "assistant"
                 for a in (m.get("extra") or {}).get("actions", [])]
        for i, t in enumerate(texts):
            out[(name, i)] = t
    return out


def commands(boxes, texts=None, skip_network=False):
    rows = []
    for b in boxes:
        for c in b.get("commands", []):
            if c.get("host_end_ns") and c.get("rc") is not None:
                if skip_network and texts and NETWORK.search(texts.get((b["trace"], c["index"]), "")):
                    continue
                rows.append((b["trace"], c["index"], c["host_start_ns"], c["host_end_ns"], b["vm"]))
    return rows


def first_step_baseline(run, meta, steps, cmds, node, settle_s, all_steps=False):
    """Median host time of each (trace, command index) in the first ramp step (fewest sandboxes), or in every step
    when the baseline run never contended (all_steps)."""
    windows = ramp_windows(meta, steps, node, settle_s)
    first = windows[0] if windows else None
    base = collections.defaultdict(list)
    for trace, idx, t0, t1, _ in cmds:
        if all_steps or (first and first[1] - settle_s * 1e9 <= t0 < first[2]):
            base[(trace, idx)].append((t1 - t0) / 1e9)
    return {k: float(np.median(v)) for k, v in base.items()}


def steps_cmd(args):
    run, meta, steps, boxes = load_run(args.run)
    node = load_csv(run / "node.csv")
    windows = ramp_windows(meta, steps, node, args.settle_s)
    texts = command_texts(meta, boxes)
    cmds = commands(boxes, texts, skip_network=True)  # network commands vary by seconds between runs
    unit = meta.get("group_size") or 1
    ncpu = len(meta["cpus"])
    base = first_step_baseline(run, meta, steps, cmds, node, args.settle_s)
    if args.baseline:  # e.g. an overflow or 38-core run against experiment 1's first step
        brun, bmeta, bsteps, bboxes = load_run(args.baseline)
        base = first_step_baseline(brun, bmeta, bsteps, commands(bboxes, command_texts(bmeta, bboxes), True),
                                   load_csv(brun / "node.csv"), args.settle_s, args.baseline_all)
    out = []
    for target, t0, t1 in windows:
        m = (node["t_ns"] >= t0) & (node["t_ns"] < t1)
        if m.sum() < 3:
            continue
        idx = np.where(m)[0]
        a, b = idx[0], idx[-1]
        dt = (node["t_ns"][b] - node["t_ns"][a]) / 1e9
        util = (node["busy_jiffies"][b] - node["busy_jiffies"][a]) / max(1.0, node["total_jiffies"][b] - node["total_jiffies"][a])
        live = node["live"][m]
        if "admit_waiting" in node:  # sandboxes blocked for room (fill.py after 11-18 20:50)
            blocked = node["admit_waiting"][m] > 0
        else:  # older runs: live below target with nothing pending (overcounts GRPO slots waiting for siblings)
            blocked = (live < node["target"][m] - unit + 1) & (node["pending"][m] == 0)
        nonfile = (node["cg_current"][m] - node["cg_file"][m] + node["cg_shmem"][m])

        def rate(col):
            return (node[col][b] - node[col][a]) / dt if col in node else float("nan")

        times, bases = [], []
        for trace, i, s0, s1, _ in cmds:
            if t0 <= s0 < t1 and (trace, i) in base:
                times.append((s1 - s0) / 1e9)
                bases.append(base[(trace, i)])
        times, bases = np.array(times), np.array(bases)
        w = bases >= 0.05
        heavy = bases >= 1.0
        row = {"target": target, "seconds": round(dt), "live_mean": round(float(live.mean()), 1),
               "live_max": int(live.max()), "cpu_util": round(float(util), 3), "busy_cpus": round(float(util * ncpu), 1),
               "busy_cpus_per_sandbox": round(float(util * ncpu / max(1.0, live.mean())), 4),
               "anon_gib": round(float(node["cg_anon"][m].mean() / GIB), 2),
               "nonfile_gib": round(float(nonfile.mean() / GIB), 2),
               "file_gib": round(float(node["cg_file"][m].mean() / GIB), 2),
               "gib_per_sandbox": round(float(nonfile.mean() / GIB / max(1.0, live.mean())), 3),
               "room_min_gib": round(float(node["room"][m].min() / GIB), 1),
               "admission_blocked": round(float(blocked.mean()), 3),
               "psi_mem_some": round(rate("psi_mem_some_us") / 1e6, 4),
               "psi_mem_full": round(rate("psi_mem_full_us") / 1e6, 4),
               "refault_file_per_s": round(rate("refault_file"), 1), "refault_anon_per_s": round(rate("refault_anon"), 1),
               "majfault_per_s": round(rate("pgmajfault"), 1),
               "oom_kills": int(node["oom_kill"][b] - node["oom_kill"][a]) if "oom_kill" in node else None,
               "commands": int(len(times)),
               "cpu_weighted_slowdown": round(float(times[w].sum() / bases[w].sum()), 3) if w.any() else None,
               "heavy_p99": round(float(np.percentile(times[heavy] / bases[heavy], 99)), 3) if heavy.sum() >= 5 else None,
               "median_slowdown": round(float(np.median(times / bases)), 3) if len(times) else None}
        row["memory_ceiling"] = row["admission_blocked"] > 0.5
        out.append(row)
    gib_per_cpu = None
    if out:
        r = out[-1]
        if r["busy_cpus_per_sandbox"] > 0:
            gib_per_cpu = r["gib_per_sandbox"] / r["busy_cpus_per_sandbox"]
    summary = {"run": str(run), "workload": meta["workload"], "gap_scale": meta["gap_scale"],
               "cpus": ncpu, "physical_cores": meta.get("physical_cores"), "budget_gib": meta["budget_bytes"] / GIB,
               "baseline_commands": len(base), "steps": out,
               "memory_per_busy_cpu_gib_last_step": round(gib_per_cpu, 2) if gib_per_cpu else None}
    cols = ["target", "live_mean", "busy_cpus", "cpu_util", "gib_per_sandbox", "nonfile_gib", "file_gib",
            "room_min_gib", "admission_blocked", "psi_mem_some", "refault_file_per_s", "oom_kills",
            "cpu_weighted_slowdown", "heavy_p99", "commands"]
    print(f"{run.name}: {meta['workload']} x{meta['gap_scale']:g}, {ncpu} CPUs, budget {meta['budget_bytes'] / GIB:.0f} GiB, "
          f"{len(base)} baseline commands")
    print("  ".join(f"{c:>10.10s}" for c in cols))
    for r in out:
        print("  ".join(f"{str(r[c]):>10.10s}" for c in cols))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1) + "\n")
    return summary


def export_cmd(args):
    """Per sandbox: 1 s series of RssAnon (KiB) and CPU seconds (vCPU + VMM), plus command (start, end) in s from
    the sandbox start. Saved as one npz with object arrays keyed by sandbox."""
    run, meta, steps, boxes = load_run(args.run)
    vmc = load_csv(run / "vm.csv")
    vcpu = load_csv(run / "vcpu.csv")
    out = {}
    by_vm = {b["vm"]: b for b in boxes if b.get("start_ns")}
    for vm, b in by_vm.items():
        t0 = b["start_ns"]
        m = vmc["vm"] == vm
        sec = ((vmc["t_ns"][m] - t0) / 1e9).astype(int)
        n = int(sec.max()) + 1 if m.any() else 0
        rss = np.zeros(n, np.float32)
        cpu = np.zeros(n, np.float32)
        if n:
            rss[sec.clip(0)] = vmc["rss_anon_kib"][m]
            np.add.at(cpu, sec.clip(0), vmc["vmm_run_ns"][m] / 1e9)
            mv = vcpu["vm"] == vm
            s2 = ((vcpu["t_ns"][mv] - t0) / 1e9).astype(int).clip(0, n - 1)
            np.add.at(cpu, s2, vcpu["run_ns"][mv] / 1e9)
        ev = np.array([((c["host_start_ns"] - t0) / 1e9, (c["host_end_ns"] - t0) / 1e9)
                       for c in b.get("commands", []) if c.get("host_end_ns")], np.float32).reshape(-1, 2)
        out[f"{vm}"] = (b["trace"], b.get("group"), rss, cpu, ev, bool(b.get("completed")))
    np.savez_compressed(args.out, meta=json.dumps(meta), **{
        f"{k}_{f}": v for k, (trace, group, rss, cpu, ev, done) in out.items()
        for f, v in (("rss_kib", rss), ("cpu_s", cpu), ("cmd_s", ev))},
        index=json.dumps({k: {"trace": t, "group": g, "completed": d} for k, (t, g, _, _, _, d) in out.items()}))
    print(f"{len(out)} sandboxes -> {args.out}")


PAGE_MIB = 4096 / 2**20
RESTORE_MS_PER_MIB = 2.2   # re-reading pages a guest used, disk restore (11-18 restore measurement, old checkout)
RESTORE_CALL_MS = 18.0     # Firecracker restore call (same measurement)
SNAPSHOT_FULL_S = 1.25     # full snapshot, VM stopped (same measurement); diff snapshots are sized from soft-dirty


def memscan_reader():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import memscan
    return memscan.read_access_log


def memory_cmd(args):
    """memscan summary: present, cold share, written rate, compression, duplicate pages (KSM upper bound)."""
    run, meta, steps, boxes = load_run(args.run)
    ms = run / "memscan"
    idle = load_csv(ms / "idle.csv")
    out = {"run": str(run), "workload": meta["workload"]}
    if len(idle.get("t_ns", [])):
        present = idle["present"]
        cold = idle["age_30_120"] + idle["age_ge120"]
        ok = present > 0
        out["idle"] = {"rows": int(ok.sum()), "present_mib_mean": round(float(present[ok].mean() * PAGE_MIB), 1),
                       "cold30_share": round(float(cold[ok].sum() / present[ok].sum()), 3),
                       "cold120_share": round(float(idle["age_ge120"][ok].sum() / present[ok].sum()), 3),
                       "accessed_share_per_round": round(float(idle["accessed"][ok].sum() / present[ok].sum()), 3),
                       "written_mib_per_round": round(float(np.nanmean(idle["written"][ok]) * PAGE_MIB), 2)
                       if "written" in idle else None,
                       "scan_ms_p50": round(float(np.nanmedian(idle["scan_ms"])), 1)}
    if (ms / "compress.csv").exists():
        c = load_csv(ms / "compress.csv")
        comp = {}
        for kind in ("hot", "cold"):
            m = c["kind"] == kind
            if m.any():
                comp[kind] = {"pages": int(c["pages"][m].sum()),
                              "lz4_ratio": round(float(c["lz4_bytes"][m].sum() / c["raw_bytes"][m].sum()), 3),
                              "zlib1_ratio": round(float(c["zlib1_bytes"][m].sum() / c["raw_bytes"][m].sum()), 3)}
        out["compress"] = comp
    group_of = {b["vm"]: b.get("group") for b in boxes}
    # Hash files (per shard, periodic rounds and first-time rounds) binned by read time; per bin the latest read of
    # each VM is used, so a bin is one snapshot of the scanned VMs' contents.
    bins = collections.defaultdict(dict)
    for f in sorted((ms / "hash").glob("*.npz")):
        z = np.load(f, allow_pickle=True)
        off, vms, tns = z["off"], list(z["vms"]), z["t_ns"]
        for k, vm in enumerate(vms):
            b = int(tns[k] // (args.bin_s * 1e9))
            old = bins[b].get(int(vm))
            if old is None or old[0] < tns[k]:
                sl = slice(off[k], off[k + 1])
                bins[b][int(vm)] = (tns[k], z["h"][sl], z["zero"][sl], z["age"][sl])
    dedup = []
    first_bin = min(bins) if bins else 0
    for r, per_vm_data in sorted(bins.items()):
        if len(per_vm_data) < args.min_vms:
            continue
        hs, zs, ages, owners = [], [], [], []
        for vm, (_, h_, z_, a_) in per_vm_data.items():
            hs.append(h_)
            zs.append(z_)
            ages.append(a_)
            owners.append(np.full(len(h_), vm, np.int64))
        if not hs:
            continue
        h, zero, age, owner = map(np.concatenate, (hs, zs, ages, owners))
        nz = ~zero
        total = int(len(h))
        uniq_all = len(np.unique(h[nz]))
        # duplicates inside one VM, inside one GRPO group, and across the node
        per_vm = sum(len(np.unique(h[nz & (owner == v)])) for v in np.unique(owner))
        groups = collections.defaultdict(list)
        for v in np.unique(owner):
            groups[group_of.get(int(v)) or f"vm{v}"].append(v)
        per_group = sum(len(np.unique(h[nz & np.isin(owner, vs)])) for vs in groups.values())
        cold = nz & (age >= 30)
        dedup.append({"bin_start_s": int((r - first_bin) * args.bin_s), "vms": int(len(np.unique(owner))), "pages": total,
                      "zero_share": round(float(zero.mean()), 3),
                      "dup_within_vm": round(1 - per_vm / max(1, nz.sum()), 3),
                      "dup_within_group": round((per_vm - per_group) / max(1, nz.sum()), 3),
                      "dup_across_groups": round((per_group - uniq_all) / max(1, nz.sum()), 3),
                      "saving_if_merged": round(1 - (uniq_all + (zero.sum() > 0)) / max(1, total), 3),
                      "cold_saving_if_merged": round(1 - len(np.unique(h[cold])) / max(1, cold.sum()), 3)
                      if cold.any() else None})
    out["dedup"] = dedup
    print(json.dumps(out, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1) + "\n")


def snapshot_cmd(args):
    """Per LLM wait of a scanned sandbox: wait length, pages present at the wait start, pages of those the next command
    touched (re-read after a restore from disk), pages new to it (zero-filled), pages written since the previous wait
    (diff snapshot). Then, for "snapshot every wait longer than T": memory freed (time average), delay added to the
    next command, snapshot bytes written per second."""
    read_access_log = memscan_reader()
    run, meta, steps, boxes = load_run(args.run)
    interval_ns = args.interval_s * 1e9
    waits = []
    span_s = 0.0
    for b in boxes:
        path = run / "memscan/access" / f"{b['vm']}.bin"
        if not path.exists():
            continue
        recs = list(read_access_log(path))
        if len(recs) < 3:
            continue
        ts = np.array([r[0] for r in recs])
        span_s += (ts[-1] - ts[0]) / 1e9
        cmds = [c for c in b.get("commands", []) if c.get("host_end_ns")]
        for i in range(1, len(cmds)):
            e_prev, s_next, e_next = cmds[i - 1]["host_end_ns"], cmds[i]["host_start_ns"], cmds[i]["host_end_ns"]
            k0 = np.searchsorted(ts, e_prev, side="right") - 1  # last round before the wait
            k1 = np.searchsorted(ts, s_next, side="right")      # first round after the next command starts
            k2 = np.searchsorted(ts, e_next + interval_ns, side="right")
            if k0 < 1 or k2 <= k1 or k2 > len(recs):
                continue
            present0 = recs[k0][1]
            touched = np.zeros_like(present0)
            for k in range(k1, k2):
                touched |= recs[k][2]
            prev_end = cmds[i - 2]["host_end_ns"] if i >= 2 else ts[0]
            kp = np.searchsorted(ts, prev_end, side="right")
            written = np.zeros_like(present0)
            known = True
            for k in range(kp, k0 + 1):
                if recs[k][3] is None:
                    known = False
                else:
                    written |= recs[k][3]
            waits.append({"vm": b["vm"], "wait_s": (s_next - e_prev) / 1e9, "cmd_s": (e_next - s_next) / 1e9,
                          "present_mib": float(present0.sum() * PAGE_MIB),
                          "reread_mib": float((touched & present0).sum() * PAGE_MIB),
                          "new_mib": float((touched & ~present0).sum() * PAGE_MIB),
                          "diff_mib": float(written.sum() * PAGE_MIB) if known else None})
    if not waits:
        print("no scanned waits")
        return
    w = {k: np.array([x[k] if x[k] is not None else np.nan for x in waits], float) for k in waits[0] if k != "vm"}
    mem_time = float(np.sum(w["present_mib"] * (w["wait_s"] + w["cmd_s"])))  # MiB*s held across waits + commands
    policies = []
    for T in args.thresholds:
        m = w["wait_s"] >= T
        down = np.clip(w["wait_s"] - SNAPSHOT_FULL_S, 0, None) * m
        freed = float(np.sum(w["present_mib"] * down))
        delay_ms = (RESTORE_CALL_MS + w["reread_mib"] * RESTORE_MS_PER_MIB) * m
        policies.append({"T_s": T, "waits_snapshotted": round(float(m.mean()), 3),
                         "memory_freed_share": round(freed / mem_time, 3) if mem_time else None,
                         "delay_ms_per_command_mean": round(float(delay_ms.mean()), 1),
                         "delay_ms_per_snapshotted_command_p50": round(float(np.median(delay_ms[m])), 1) if m.any() else None,
                         "delay_ms_per_snapshotted_command_p90": round(float(np.percentile(delay_ms[m], 90)), 1) if m.any() else None,
                         "diff_snapshot_mib_per_s": round(float(np.nansum(w["diff_mib"] * m) / span_s), 3) if span_s else None,
                         "full_snapshot_mib_per_s": round(float(np.sum(w["present_mib"] * m) / span_s), 3) if span_s else None})
    out = {"run": str(run), "waits": len(waits), "sandboxes": len({x["vm"] for x in waits}),
           "wait_s_p50": round(float(np.median(w["wait_s"])), 2),
           "present_mib_p50": round(float(np.median(w["present_mib"])), 1),
           "reread_mib_p50": round(float(np.median(w["reread_mib"])), 2),
           "reread_mib_p90": round(float(np.percentile(w["reread_mib"], 90)), 2),
           "reread_share_of_present": round(float(w["reread_mib"].sum() / max(1e-9, w["present_mib"].sum())), 3),
           "policies": policies, "note": "per-wait times at memscan's round resolution; delay uses the 11-18 disk-restore "
                                         "cost; *_mib_per_s are per scanned sandbox"}
    print(json.dumps(out, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("steps")
    p.add_argument("run")
    p.add_argument("--settle-s", type=float, default=120)
    p.add_argument("--baseline", help="Take command baselines from this run's first step instead")
    p.add_argument("--baseline-all", action="store_true",
                   help="Use every step of the baseline run (when it never contended, e.g. 1-rl: CPU < 4%%)")
    p.add_argument("--json")
    p.set_defaults(func=steps_cmd)
    p = sub.add_parser("export")
    p.add_argument("run")
    p.add_argument("out")
    p.set_defaults(func=export_cmd)
    p = sub.add_parser("memory")
    p.add_argument("run")
    p.add_argument("--bin-s", type=float, default=300, help="Hash reads within one bin form one snapshot")
    p.add_argument("--min-vms", type=int, default=8, help="Skip bins with fewer scanned VMs")
    p.add_argument("--json")
    p.set_defaults(func=memory_cmd)
    p = sub.add_parser("snapshot")
    p.add_argument("run")
    p.add_argument("--interval-s", type=float, default=5.0, help="memscan idle interval")
    p.add_argument("--thresholds", type=float, nargs="+", default=[0, 2, 5, 10, 30, 60])
    p.add_argument("--json")
    p.set_defaults(func=snapshot_cmd)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
