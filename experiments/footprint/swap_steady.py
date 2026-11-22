#!/usr/bin/env python3
"""Report of the steady-arrival swap runs (swap_queue.py).

  swap_steady.py QUEUE_DIR [--json OUT]

Per run, over the window after the stagger (all slots running, ages mixed):
- real overcommit = (anon + swap + kernel) / budget, mean and peak; swap in use; swap-in and swap-out rates (host
  pswpin/pswpout; these runs are the only swap users); cgroup memory PSI full; anon and file refaults; OOM kills;
  utilization and MiB/s of sda, which holds both the swap partition and the VMs' writable disks.
- command slowdown against the control run of the same workload: baseline = median host time of the same
  (trace, command index) over the whole control; network commands and commands whose baseline is under 50 ms are
  left out (the share of commands that found a baseline at all is printed). Pairs the control never ran take the
  median of the 11-18 fill runs (no swap, 60 or 38 cores); their share is printed too.
  CPU-weighted ratio (sum of times / sum of baselines), mean and p99 extra time, share over 2x, heavy (baseline >= 1 s)
  median and p99 ratio, and how many commands found a baseline.
- extra command time (all commands with a baseline, any length) per MiB swapped in over the window: an effective cost
  per MiB that also carries reclaim stalls, and leaves out swap-ins during waits only in the sense that they cost no
  command time.
- progress: commands finished per sandbox-minute (all commands), command timeouts (60 s, so the heavy p99 and max are
  capped there), sandboxes that ran their whole trace.
Per 2 minutes: live, anon, swap, overcommit, swap-in/out MiB/s, PSI full, CPU-weighted ratio.
"""
import argparse
import collections
import json
from pathlib import Path
import sys

import numpy as np

FP = Path(__file__).resolve().parent
sys.path.insert(0, str(FP))
import to_sim  # noqa: E402

GIB = 2 ** 30
STAGGER = {"swe": 120, "rl": 600}
PAGE_MIB = 4096 / 2 ** 20


def load(run):
    run, meta, steps, boxes = to_sim.load_run(run)
    node = to_sim.load_csv(run / "node.csv")
    host = to_sim.load_csv(run / "host.csv")
    return meta, boxes, node, host


def cmd_rows(meta, boxes, t0_ns):
    """(start min, host s, trace, index, network) of every finished command."""
    texts = to_sim.command_texts(meta, boxes)
    rows = []
    for b in boxes:
        for c in b.get("commands", []):
            if not c.get("host_end_ns") or c.get("rc") is None:
                continue
            k = (b["trace"], c["index"])
            rows.append(((c["host_start_ns"] - t0_ns) / 6e10, (c["host_end_ns"] - c["host_start_ns"]) / 1e9, k,
                         bool(to_sim.NETWORK.search(texts.get(k, "")))))
    return rows


FALLBACK = FP / ".local/queue/20261118T2014"  # 11-18 fill runs, no swap, for traces the control did not run
FALLBACK_RUNS = {"rl": ("1-rl", "3-rl-38"), "swe": ("1-swe", "1-swe-hi", "3-swe-38")}


def medians(run):
    meta, boxes, node, host = load(run)
    base = collections.defaultdict(list)
    for m, d, k, net in cmd_rows(meta, boxes, node["t_ns"][0]):
        if not net:
            base[k].append(d)
    return {k: float(np.median(v)) for k, v in base.items()}


def baseline(ctrl, wl):
    """Control medians; (trace, index) pairs the control never ran take the median of the 11-18 fill runs."""
    base = medians(ctrl)
    extra = collections.defaultdict(list)
    for name in FALLBACK_RUNS[wl]:
        if (FALLBACK / name / "node.csv").exists():
            for k, v in medians(FALLBACK / name).items():
                if k not in base:
                    extra[k].append(v)
    base.update({k: float(np.median(v)) for k, v in extra.items()})
    return base, set(extra)


def rate(host, col, m, t):
    i0, i1 = np.where(m)[0][[0, -1]]
    return (host[col][i1] - host[col][i0]) / (t[i1] - t[i0])


def slowdown(rows, base, lo, hi, fallback=frozenset()):
    sel = [(d, base[k]) for m, d, k, net in rows if lo <= m < hi and not net and k in base and base[k] >= 0.05]
    n_fb = sum(1 for m, d, k, net in rows if lo <= m < hi and not net and k in fallback and base[k] >= 0.05)
    window = [k for m, d, k, net in rows if lo <= m < hi and not net]
    if not sel:
        return None
    a = np.array(sel)
    d, b = a[:, 0], a[:, 1]
    r, ex, h = d / b, (d - b) * 1000, b >= 1
    pos = np.sort(ex[ex > 0])[::-1]
    return dict(n=len(a), from_fill_runs=round(n_fb / len(a), 3), with_baseline=round(sum(k in base for k in window) / max(1, len(window)), 3), cpu_weighted=round(float(d.sum() / b.sum()), 3),
                extra_ms_mean=round(float(ex.mean())), extra_ms_p99=round(float(np.percentile(ex, 99))),
                over_2x=round(float(np.mean(r > 2)), 4), ratio_p50=round(float(np.median(r)), 3),
                ratio_p90=round(float(np.percentile(r, 90)), 2),
                top10_share_of_extra=round(float(pos[:max(1, len(r) // 10)].sum() / pos.sum()), 2) if len(pos) else None,
                heavy_n=int(h.sum()),
                heavy_p50=round(float(np.median(r[h])), 2) if h.any() else None,
                heavy_p99=round(float(np.percentile(r[h], 99)), 2) if h.any() else None,
                heavy_max=round(float(r[h].max()), 2) if h.any() else None)


def report(run, base, wl, fallback):
    meta, boxes, node, host = load(run)
    t = (node["t_ns"] - node["t_ns"][0]) / 1e9
    th = (host["t_ns"] - node["t_ns"][0]) / 1e9
    end = t[-1]
    lo = STAGGER[wl] + 60
    budget = meta["budget_bytes"] / GIB
    dem = (node["cg_anon"] + node["swap_current"] + node["cg_kernel"]) / GIB
    m, mh = (t >= lo) & (t < end - 10), (th >= lo) & (th < end - 10)
    if m.sum() < 30 or mh.sum() < 30:
        return None
    rows = cmd_rows(meta, boxes, node["t_ns"][0])
    live_min = node["live"][m].mean() * (t[m][-1] - t[m][0]) / 60
    done = sum(1 for r in rows if lo / 60 <= r[0] < (end - 10) / 60)
    oom = int(node["oom_kill"][-1] - node["oom_kill"][0])
    ncmd = sum(len(b.get("commands", [])) for b in boxes)
    timeouts = sum(b.get("timeouts") or 0 for b in boxes)
    allc = [(d, base[k]) for mm_, d, k, net in rows if lo / 60 <= mm_ < (end - 10) / 60 and not net and k in base]
    extra_s = float(sum(d - b for d, b in allc))
    swapin_mib = float(rate(host, "pswpin", mh, th) * PAGE_MIB * (th[mh][-1] - th[mh][0]))
    out = dict(run=run.name, budget_gib=round(budget, 1), live_mean=round(float(node["live"][m].mean()), 1),
               demand_mean_gib=round(float(dem[m].mean()), 1), demand_peak_gib=round(float(dem[m].max()), 1),
               overcommit_mean=round(float(dem[m].mean() / budget), 2),
               overcommit_peak=round(float(dem[m].max() / budget), 2),
               swap_max_gib=round(float(node["swap_current"].max() / GIB), 1),
               swapin_mib_s=round(float(rate(host, "pswpin", mh, th) * PAGE_MIB), 1),
               swapout_mib_s=round(float(rate(host, "pswpout", mh, th) * PAGE_MIB), 1),
               psi_full_pct=round(float(rate(node, "psi_mem_full_us", m, t) / 1e4), 2),
               disk_util_pct=round(float(rate(host, "disk_io_ms", mh, th) / 10), 1),
               disk_read_mib_s=round(float(rate(host, "disk_rd_sectors", mh, th) * 512 / 2 ** 20), 1),
               disk_write_mib_s=round(float(rate(host, "disk_wr_sectors", mh, th) * 512 / 2 ** 20), 1),
               refault_anon_s=round(float(rate(node, "refault_anon", m, t))),
               refault_file_s=round(float(rate(node, "refault_file", m, t))),
               oom_kills=oom, cmds_per_sandbox_min=round(done / live_min, 2),
               timeouts=timeouts, timeouts_per_1000_cmds=round(1000 * timeouts / max(1, ncmd), 1),
               sandboxes_completed=sum(1 for b in boxes if b.get("completed")),
               window_min=[round(lo / 60, 1), round((end - 10) / 60, 1)],
               extra_cmd_s_total=round(extra_s), swapin_mib_total=round(swapin_mib),
               extra_ms_per_swapin_mib=round(1000 * extra_s / swapin_mib) if swapin_mib > 1 else None,
               commands=slowdown(rows, base, lo / 60, (end - 10) / 60, fallback))
    series = []
    edges = np.arange(0, end, 120)
    for a, b in zip(edges[:-1], edges[1:]):
        mm, mmh = (t >= a) & (t < b), (th >= a) & (th < b)
        if mm.sum() < 3 or mmh.sum() < 3:
            continue
        s = slowdown(rows, base, a / 60, b / 60)
        series.append(dict(min=int(a / 60), live=round(float(node["live"][mm].mean())),
                           anon=round(float(node["cg_anon"][mm][-1] / GIB), 1),
                           swap=round(float(node["swap_current"][mm][-1] / GIB), 1),
                           x=round(float(dem[mm].max() / budget), 2),
                           swapin=round(float(rate(host, "pswpin", mmh, th) * PAGE_MIB), 1),
                           swapout=round(float(rate(host, "pswpout", mmh, th) * PAGE_MIB), 1),
                           psi_full=round(float(rate(node, "psi_mem_full_us", mm, t) / 1e4), 2),
                           cpu_weighted=s["cpu_weighted"] if s else None))
    out["series"] = series
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queue", type=Path)
    ap.add_argument("--json")
    a = ap.parse_args()
    res = {}
    for wl in ("swe", "rl"):
        ctrl = a.queue / f"4-{wl}-ctrl"
        if not (ctrl / "node.csv").exists():
            continue
        base, fallback = baseline(ctrl, wl)
        runs = [ctrl] + sorted((p for p in a.queue.glob(f"4-{wl}-x*") if p.is_dir()), key=lambda p: float(p.name.split("-x")[1]))
        for run in runs:
            if not (run / "node.csv").exists() or not (run / "host.csv").exists():
                continue
            r = report(run, base, wl, fallback)
            if r is None:
                continue
            res[run.name] = r
            c = r["commands"] or {}
            print(f"=== {run.name}: budget {r['budget_gib']} GiB, live {r['live_mean']}, demand mean {r['demand_mean_gib']} "
                  f"peak {r['demand_peak_gib']} GiB, overcommit mean {r['overcommit_mean']} peak {r['overcommit_peak']}, "
                  f"swap max {r['swap_max_gib']} GiB, OOM {r['oom_kills']}, window min {r['window_min']}")
            print(f"  swap-in {r['swapin_mib_s']} MiB/s, swap-out {r['swapout_mib_s']} MiB/s, PSI full {r['psi_full_pct']}%, "
                  f"refault anon {r['refault_anon_s']}/s file {r['refault_file_s']}/s, disk (sda: swap + VM disks) util "
                  f"{r['disk_util_pct']}% read {r['disk_read_mib_s']} write {r['disk_write_mib_s']} MiB/s, "
                  f"commands/sandbox-min {r['cmds_per_sandbox_min']}, command timeouts (60 s) {r['timeouts']} "
                  f"({r['timeouts_per_1000_cmds']} per 1000), sandboxes completed {r['sandboxes_completed']}")
            print(f"  extra command time {r['extra_cmd_s_total']} s over {r['swapin_mib_total']} MiB swapped in: "
                  f"{r['extra_ms_per_swapin_mib']} ms per MiB (all commands with a baseline, any length)")
            print(f"  commands n={c.get('n')} (>= 50 ms; {c.get('with_baseline')} of commands have a baseline, "
                  f"{c.get('from_fill_runs')} of these from the 11-18 fill runs): cpu-wt {c.get('cpu_weighted')}, extra mean "
                  f"{c.get('extra_ms_mean')} ms p99 {c.get('extra_ms_p99')} ms, >2x {c.get('over_2x')}, ratio p50 {c.get('ratio_p50')} "
                  f"p90 {c.get('ratio_p90')}, top 10% carry {c.get('top10_share_of_extra')} of the extra, heavy n={c.get('heavy_n')} "
                  f"p50 {c.get('heavy_p50')} p99 {c.get('heavy_p99')} max {c.get('heavy_max')}")
            print("   min live anonG swapG    x  in/s  out/s  psi%  cpu-wt")
            for s in r["series"]:
                print(f"  {s['min']:4d} {s['live']:4d} {s['anon']:5.1f} {s['swap']:5.1f} {s['x']:4.2f} {s['swapin']:5.1f} "
                      f"{s['swapout']:6.1f} {s['psi_full']:5.2f}  {s['cpu_weighted']}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1) + "\n")


if __name__ == "__main__":
    main()
