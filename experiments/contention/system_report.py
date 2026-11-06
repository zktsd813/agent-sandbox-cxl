"""System (host kernel and VMM) work of microVM sandboxes, from `sandbox.py pilot --sys-trace` runs.

For each run: per task (image), how much system work its sandboxes cause per second of life; per node over time, how
evenly that work and the vCPU load fall on the nodes; and per event, what the host charged (exit handling, VMM
syscall time, lock waits, journal commits, block latency). Several runs at different densities show contention as
per-event cost that grows with load.

    python3 system_report.py RUN [RUN ...] [--out report.json]
"""
import argparse
import collections
import csv
import json
import re
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CANDIDATES = HERE / "candidates-plan.json"
EXIT_NAMES = {1: "external_interrupt", 7: "interrupt_window", 10: "cpuid", 12: "hlt", 18: "vmcall", 28: "cr_access",
              30: "io", 31: "msr_read", 32: "msr_write", 40: "pause", 48: "ept_violation", 49: "ept_misconfig",
              52: "preemption_timer", 54: "wbinvd", 55: "xsetbv"}
SYSCALL_NAMES = {0: "read", 1: "write", 3: "close", 7: "poll", 9: "mmap", 11: "munmap", 16: "ioctl", 17: "pread64",
                 18: "pwrite64", 19: "readv", 20: "writev", 23: "select", 74: "fsync", 75: "fdatasync",
                 202: "futex", 232: "epoll_wait", 270: "pselect6", 271: "ppoll", 281: "epoll_pwait", 285: "fallocate",
                 295: "preadv", 296: "pwritev", 441: "epoll_pwait2"}
BLOCKING = {7, 23, 202, 232, 270, 271, 281, 441}  # the VMM waiting for work, not doing it
DISK_IO = {17, 18, 19, 20, 74, 75, 285, 295, 296}  # block device emulation on the host file
BIN_S = 2.0


def parse_system(path):
    """2 s blocks [(t_ns, {map: {key: value}})] and the contended lock sites [(frames, ns)] printed at exit."""
    blocks, sites, frames, current = [], [], None, None
    line_re = re.compile(r"@(\w+)(?:\[([\d, ]+)\])?: (\d+)$")
    for line in path.read_text().splitlines():
        if line.startswith("T "):
            current = {"t": int(line[2:]), "maps": collections.defaultdict(dict)}
            blocks.append(current)
        elif line.startswith("END"):
            current = None
        elif line.startswith("@lock_site["):
            frames = []
        elif frames is not None:
            if line.startswith("]: "):
                sites.append(([f.split("+")[0] for f in frames], int(line[3:])))
                frames = None
            elif line.strip():
                frames.append(line.strip())
        elif current is not None:
            m = line_re.match(line)
            if m:
                key = tuple(int(x) for x in m.group(2).split(", ")) if m.group(2) else ()
                current["maps"][m.group(1)][key] = int(m.group(3))
    return blocks, sites


def load(run):
    meta = json.loads((run / "run.json").read_text())
    boxes = [json.loads(line) for line in (run / "sandboxes.jsonl").open()]
    boxes = [b for b in boxes if b.get("pid") and b.get("start_ns")]
    t0 = min(b["start_ns"] for b in boxes)
    lo, hi = t0 + meta["ramp_s"] * 1e9, t0 + (meta["ramp_s"] + meta["duration_s"]) * 1e9
    vcpu = collections.defaultdict(lambda: np.zeros(2))
    node_cpu = collections.defaultdict(lambda: collections.defaultdict(float))
    with (run / "vcpu.csv").open() as stream:
        for r in csv.DictReader(stream):
            vcpu[int(r["vm"])] += (int(r["run_ns"]), int(r["wait_ns"]))
    with (run / "nodes.csv").open() as stream:
        for r in csv.DictReader(stream):
            t = int(r["t_ns"])
            if lo <= t < hi:
                node_cpu[int(r["node"])][int((t - lo) // (BIN_S * 1e9))] += int(r["run_ns"]) / (BIN_S * 1e9)
    blocks, sites = parse_system(run / "system.txt")
    return meta, boxes, (lo, hi), vcpu, node_cpu, blocks, sites


def per_vm(boxes, blocks, vcpu):
    """Totals per VM over its life: vCPU run and wait, exits, exit handling, VMM syscalls, bytes, lock waits, and the
    guest's own disk writes (SWE_MEM sectors of vda/vdb/vdc)."""
    by_pid = {b["pid"]: b for b in boxes}
    out = {b["vm"]: collections.Counter() for b in boxes}
    for block in blocks:
        maps = block["maps"]
        for (pid, reason), n in maps.get("exits", {}).items():
            if pid in by_pid:
                c = out[by_pid[pid]["vm"]]
                c["exits_hlt" if reason == 12 else "exits"] += n
                c[f"exit_{EXIT_NAMES.get(reason, reason)}"] += n
        for key, ns in maps.get("exit_ns", {}).items():  # (pid,) in the first runs, (pid, reason) since
            if key[0] in by_pid:
                c = out[by_pid[key[0]]["vm"]]
                c["exit_ns"] += ns
                if len(key) > 1:
                    c[f"exit_ns_{EXIT_NAMES.get(key[1], key[1])}"] += ns
        for (pid,), ns in maps.get("vmm_rq_ns", {}).items():
            if pid in by_pid:
                out[by_pid[pid]["vm"]]["vmm_rq_ns"] += ns
        for (pid,), n in maps.get("vmm_wakes", {}).items():
            if pid in by_pid:
                out[by_pid[pid]["vm"]]["vmm_wakes"] += n
        for (pid, sc), n in maps.get("sc_n", {}).items():
            if pid in by_pid and sc not in BLOCKING:
                c = out[by_pid[pid]["vm"]]
                c["syscalls"] += n
                c["disk_syscalls" if sc in DISK_IO else "other_syscalls"] += n
                c["fsyncs"] += n if sc in (74, 75) else 0
        for (pid, sc), ns in maps.get("sc_ns", {}).items():
            if pid in by_pid and sc not in BLOCKING:
                c = out[by_pid[pid]["vm"]]
                c["syscall_ns"] += ns
                c["disk_syscall_ns" if sc in DISK_IO else "other_syscall_ns"] += ns
                c["fsync_ns"] += ns if sc in (74, 75) else 0
        for (pid, sc), nbytes in maps.get("sc_bytes", {}).items():
            if pid in by_pid:
                out[by_pid[pid]["vm"]]["disk_bytes" if sc in DISK_IO else "other_bytes"] += nbytes
        for (pid,), ns in maps.get("lock_ns", {}).items():
            if pid in by_pid:
                out[by_pid[pid]["vm"]]["lock_ns"] += ns
    for b in boxes:
        c = out[b["vm"]]
        c["run_ns"], c["wait_ns"] = vcpu.get(b["vm"], (0, 0))
        c["life_s"] = (b["end_ns"] - b["start_ns"]) / 1e9
        mem = [m for m in b.get("mem") or [] if len(m) > 13]  # a sample cut short at shutdown has no disk fields
        if len(mem) >= 2:
            # host_ns, uptime, 6 memory fields, then read and written sectors of vda (base), vdb (job), vdc (upper)
            c["guest_write_bytes"] = sum(mem[-1][i] - mem[0][i] for i in (9, 11, 13)) * 512
    return out


def task_of():
    return {e["name"]: e.get("image_key", e["name"]) for e in json.loads(CANDIDATES.read_text())["traces"]}


METRICS = {  # name: (numerator, scale, unit), rates per second of sandbox life
    "vcpu_cores": ("run_ns", 1e-9, "cores"),
    "exits_per_s": ("exits", 1, "/s"),
    "exit_handling_ms_per_s": ("exit_ns", 1e-6, "ms/s"),
    "vmm_syscall_ms_per_s": ("syscall_ns", 1e-6, "ms/s"),
    "vmm_disk_mb_per_s": ("disk_bytes", 1e-6, "MB/s"),
    "fsyncs_per_s": ("fsyncs", 1, "/s"),
    "lock_wait_ms_per_s": ("lock_ns", 1e-6, "ms/s"),
    "guest_write_mb_per_s": ("guest_write_bytes", 1e-6, "MB/s"),
    "vmm_cpu_wait_ms_per_s": ("vmm_rq_ns", 1e-6, "ms/s"),
}


def task_table(vms, boxes):
    """Per task: rates per second of life, summed over its sandboxes; and how concentrated each rate is over tasks."""
    names = task_of()
    tasks = collections.defaultdict(collections.Counter)
    for b in boxes:
        tasks[names.get(b["trace"], b["trace"])].update(vms[b["vm"]])
    rows = {}
    for task, c in tasks.items():
        if c["life_s"] > 0:
            rows[task] = {m: c[num] * scale / c["life_s"] for m, (num, scale, _) in METRICS.items()}
            rows[task]["sandboxes"] = sum(1 for b in boxes if names.get(b["trace"], b["trace"]) == task)
    spread = {}
    for m in METRICS:
        v = np.array([r[m] for r in rows.values()])
        life = np.array([tasks[t]["life_s"] for t in rows])
        load = np.sort(v * life)[::-1]  # each task's total, for the share of the heaviest tasks
        spread[m] = {"p10": float(np.percentile(v, 10)), "p50": float(np.median(v)), "p90": float(np.percentile(v, 90)),
                     "max": float(v.max()), "p90_over_p50": float(np.percentile(v, 90) / max(np.median(v), 1e-12)),
                     "top10pct_task_share": float(load[:max(1, len(load) // 10)].sum() / max(load.sum(), 1e-12))}
    return rows, spread


def node_series(boxes, blocks, window, node_cpu, n_nodes):
    """Per node and 2 s bin: vCPU cores in use, and system ms per s (exit handling + VMM syscalls + lock waits)."""
    lo, hi = window
    nbins = int((hi - lo) // (BIN_S * 1e9))
    by_pid = {b["pid"]: b for b in boxes}
    system = np.zeros((n_nodes, nbins))
    for block in blocks:
        i = int((block["t"] - lo) // (BIN_S * 1e9)) - 1  # the block's counts belong to the 2 s before its stamp
        if not 0 <= i < nbins or block["t"] > hi:  # past hi a block holds the teardown of every VM at the end
            continue
        maps = block["maps"]
        for key, ns in [*maps.get("exit_ns", {}).items(), *maps.get("lock_ns", {}).items(),
                        *(((pid,), ns) for (pid, sc), ns in maps.get("sc_ns", {}).items() if sc not in BLOCKING)]:
            b = by_pid.get(key[0])
            if b:
                system[b["node"], i] += ns / 1e6 / BIN_S
    cpu = np.array([[node_cpu[n].get(i, 0.0) for i in range(nbins)] for n in range(n_nodes)])
    return cpu, system


def balance(series):
    """How the nodes' loads relate over time (nodes x 2 s bins): mean and CV per node; correlation averaged over node
    pairs (raw, by rank, and after removing a 30 s moving mean); the share of all load that sits above the per-bin mean
    of the nodes (what instant moves could shift to other nodes), and the same for 30 s means (what slow moves could
    shift); and the share of bins where the busiest node carries over twice the least busy one's load while the total
    is above its median."""
    x = np.asarray(series, dtype=float)
    k = int(30 / BIN_S)
    smooth = np.array([np.convolve(r, np.ones(k) / k, mode="same") for r in x])
    pairs = [(i, j) for i in range(len(x)) for j in range(i + 1, len(x))]
    corr = lambda y: float(np.mean([np.corrcoef(y[i], y[j])[0, 1] for i, j in pairs])) if pairs else float("nan")
    excess = lambda y: float(np.clip(y - y.mean(0), 0, None).sum() / max(y.sum(), 1e-12))
    total = x.sum(0)
    return {"mean": x.mean(1).tolist(), "cv": (x.std(1) / np.maximum(x.mean(1), 1e-12)).tolist(),
            "corr": corr(x), "corr_rank": corr(np.argsort(np.argsort(x, axis=1), axis=1).astype(float)),
            "corr_fast": corr(x - smooth), "excess_share": excess(x), "excess_share_30s": excess(smooth),
            "lopsided_share": float(((x.max(0) > 2 * x.min(0)) & (total > np.median(total))).mean())}


def per_event(blocks, window, vms):
    """Host-wide cost per event over the window: exit handling per non-HLT exit, VMM syscall time per call by kind,
    journal commit time, block request latency, and lock waits per VM-second."""
    lo, hi = window
    tot = collections.Counter()
    for block in blocks:
        if not lo + BIN_S * 1e9 <= block["t"] <= hi:
            continue
        maps = block["maps"]
        tot["exits"] += sum(n for (pid, r), n in maps.get("exits", {}).items() if r != 12)
        tot["exit_ns"] += sum(maps.get("exit_ns", {}).values())
        for (pid, r), n in maps.get("exits", {}).items():
            tot[f"xn_{EXIT_NAMES.get(r, r)}"] += n
        for key, ns in maps.get("exit_ns", {}).items():
            if len(key) > 1:
                tot[f"xt_{EXIT_NAMES.get(key[1], key[1])}"] += ns
        tot["vmm_rq_ns"] += sum(maps.get("vmm_rq_ns", {}).values())
        tot["vmm_wakes"] += sum(maps.get("vmm_wakes", {}).values())
        for (pid, sc), n in maps.get("sc_n", {}).items():
            if sc not in BLOCKING:
                tot[f"n_{SYSCALL_NAMES.get(sc, sc)}"] += n
        for (pid, sc), ns in maps.get("sc_ns", {}).items():
            if sc not in BLOCKING:
                tot[f"ns_{SYSCALL_NAMES.get(sc, sc)}"] += ns
        for name in ("jbd2_n", "jbd2_ns", "bio_n", "bio_ns", "lock_all"):
            tot[name] += sum(maps.get(name, {}).values())
        tot["lock_ns"] += sum(maps.get("lock_ns", {}).values())
    span = (hi - lo) / 1e9
    calls = {k[2:]: tot[k] for k in tot if k.startswith("n_")}
    top = sorted(calls, key=lambda s: -tot[f"ns_{s}"])[:8]
    reasons = sorted((k[3:] for k in tot if k.startswith("xt_")), key=lambda r: -tot[f"xt_{r}"])[:6]
    return {"exits_per_s": tot["exits"] / span, "exit_handling_us": tot["exit_ns"] / max(tot["exits"], 1) / 1e3,
            "exit_us_by_reason": {r: {"per_s": tot[f"xn_{r}"] / span, "us": tot[f"xt_{r}"] / max(tot[f"xn_{r}"], 1) / 1e3}
                                  for r in reasons},
            "vmm_wakeup_wait_us": tot["vmm_rq_ns"] / max(tot["vmm_wakes"], 1) / 1e3,
            "vmm_wakeups_per_s": tot["vmm_wakes"] / span,
            "syscall_us": {s: {"per_s": calls[s] / span, "us": tot[f"ns_{s}"] / max(calls[s], 1) / 1e3} for s in top},
            "jbd2_commits_per_s": tot["jbd2_n"] / span, "jbd2_commit_ms": tot["jbd2_ns"] / max(tot["jbd2_n"], 1) / 1e6,
            "block_req_per_s": tot["bio_n"] / span, "block_latency_us": tot["bio_ns"] / max(tot["bio_n"], 1) / 1e3,
            "lock_wait_ms_per_s_host": tot["lock_all"] / span / 1e6,
            "lock_wait_ms_per_s_firecracker": tot["lock_ns"] / span / 1e6}


def command_times(boxes):
    """Guest command wall time by (trace, command index), from the serial markers."""
    out = collections.defaultdict(list)
    for b in boxes:
        for c in b.get("commands", []):
            if c.get("host_end_ns") and c.get("rc") is not None:
                out[(b["trace"], c["index"])].append((c["host_end_ns"] - c["host_start_ns"]) / 1e9)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--overlay", action="store_true",
                        help="The runs are single-node runs on an otherwise idle host: treat them as the nodes of one "
                             "cluster whose kernels share nothing, aligned by time since the end of each ramp")
    args = parser.parse_args()
    report, times = {}, {}
    if args.overlay:
        cpus, systems = [], []
        for run in args.runs:
            meta, boxes, window, vcpu, node_cpu, blocks, _ = load(run)
            cpu, system = node_series(boxes, blocks, window, node_cpu, len(meta["nodes"]))
            cpus.append(cpu[0])
            systems.append(system[0])
        n = min(len(c) for c in cpus)
        cpus, systems = np.array([c[:n] for c in cpus]), np.array([s[:n] for s in systems])
        report["overlay"] = {"nodes": len(args.runs), "bins": n,
                             "cpu": balance(cpus), "system": balance(systems),
                             "pairs": {"cpu": [balance(cpus[[i, j]]) for i in range(len(cpus)) for j in range(i + 1, len(cpus))],
                                       "system": [balance(systems[[i, j]]) for i in range(len(cpus)) for j in range(i + 1, len(cpus))]}}
    for run in args.runs:
        meta, boxes, window, vcpu, node_cpu, blocks, sites = load(run)
        vms = per_vm(boxes, blocks, vcpu)
        tasks, spread = task_table(vms, boxes)
        cpu, system = node_series(boxes, blocks, window, node_cpu, len(meta["nodes"]))
        life = sum(v["life_s"] for v in vms.values())
        run_s = sum(v["run_ns"] for v in vms.values()) / 1e9
        report[run.name] = {
            "per_node": meta["per_node"], "sandboxes": len(boxes),
            "vcpu_wait_share": sum(v["wait_ns"] for v in vms.values()) / max(1, sum(v["run_ns"] + v["wait_ns"] for v in vms.values())),
            "system_ms_per_vcpu_s": sum(v["exit_ns"] + v["syscall_ns"] + v["lock_ns"] for v in vms.values()) / 1e6 / max(run_s, 1e-9),
            "per_event": per_event(blocks, window, vms),
            "task_spread": spread, "tasks": tasks,
            "nodes_cpu": balance(cpu), "nodes_system": balance(system),
            "lock_sites": [{"frames": f[2:7], "ms": ns / 1e6} for f, ns in sorted(sites, key=lambda s: -s[1])[:10]],
            "life_s": life}
        times[run.name] = command_times(boxes)
    if len(args.runs) >= 2:
        a, b = (times[r.name] for r in args.runs[:2])
        ratios = [np.median(b[k]) / np.median(a[k]) for k in a.keys() & b.keys() if np.median(a[k]) > 0.05]
        report["command_ratio_second_vs_first"] = {"commands": len(ratios), "p50": float(np.median(ratios)),
                                                   "p90": float(np.percentile(ratios, 90))}
    text = json.dumps(report, indent=1, default=float)
    if args.out:
        args.out.write_text(text)
    for name, r in report.items():
        if name == "overlay":
            for kind in ("cpu", "system"):
                print(f"overlay of {r['nodes']} nodes, {kind}:", json.dumps(r[kind], default=lambda x: round(x, 3)))
            continue
        if not isinstance(r, dict) or "per_node" not in r:
            print(name, json.dumps(r))
            continue
        print(f"== {name}: K={r['per_node']}/node, {r['sandboxes']} sandboxes, vCPU wait share {r['vcpu_wait_share']:.1%}, "
              f"system {r['system_ms_per_vcpu_s']:.1f} ms per vCPU-s")
        print("  per event:", json.dumps(r["per_event"], default=lambda x: round(x, 3)))
        print("  per task (rates per sandbox-second):")
        for m, s in r["task_spread"].items():
            print(f"    {m:24s} p10 {s['p10']:.4g} p50 {s['p50']:.4g} p90 {s['p90']:.4g} max {s['max']:.4g} "
                  f"| p90/p50 {s['p90_over_p50']:.1f}, top 10% tasks {s['top10pct_task_share']:.0%}")
        print("  nodes, vCPU cores:", json.dumps(r["nodes_cpu"], default=lambda x: round(x, 3)))
        print("  nodes, system ms/s:", json.dumps(r["nodes_system"], default=lambda x: round(x, 3)))
        for site in r["lock_sites"][:5]:
            print(f"  lock {site['ms']:.0f} ms: {' < '.join(site['frames'])}")


if __name__ == "__main__":
    main()
