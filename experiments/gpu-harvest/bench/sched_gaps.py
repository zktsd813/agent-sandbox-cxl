# On-CPU slices and sleep gaps of trainer worker threads from a perf sched trace (monotonic clock).
import json, re, sys
from collections import defaultdict
import numpy as np
S, run = sys.argv[1], sys.argv[2]
threads = {}
for line in open(f"{S}/sched-blk.threads"):
    pid, tid, comm = line.split(maxsplit=2)
    threads[int(tid)] = (int(pid), comm.strip())
sw = re.compile(r"\s*\d+ \[(\d+)\] (\d+\.\d+): sched:sched_switch: (.+):(\d+) \[\d+\] (\S+) ==> (.+):(\d+) \[\d+\]")
on_since, slices, gaps, off_since = {}, defaultdict(list), defaultdict(list), {}
t_first = t_last = None
for line in open(f"{S}/sched-blk.txt"):
    m = sw.match(line)
    if not m:
        continue
    t = float(m.group(2)); prev, nxt = int(m.group(4)), int(m.group(7))
    t_first = t if t_first is None else t_first; t_last = t
    if prev in threads and prev in on_since:
        slices[prev].append(t - on_since.pop(prev)); off_since[prev] = t
    if nxt in threads:
        on_since[nxt] = t
        if nxt in off_since:
            gaps[nxt].append(t - off_since.pop(nxt))
span = t_last - t_first
ph = [json.loads(l) for l in open(f"{run}/phases.jsonl")]
lo, hi = t_first * 1e9, t_last * 1e9
print(f"trace {span:.1f} s; phases overlapping:",
      sorted({(e['name'], round((min(e['end_ns'], hi) - max(e['start_ns'], lo)) / 1e9, 1)) for e in ph
              if e['end_ns'] and e['name'] not in ('step',) and e['start_ns'] < hi and e['end_ns'] > lo}))
def q(a):
    a = np.array(a) * 1e6
    return f"n={len(a):6d} p50 {np.percentile(a,50):8.0f} p90 {np.percentile(a,90):8.0f} p99 {np.percentile(a,99):9.0f} us"
for tid, (pid, comm) in sorted(threads.items()):
    if not slices.get(tid):
        continue
    busy = sum(slices[tid]) / span
    if busy < 0.02:
        continue
    print(f"{pid} {tid} {comm:16s} busy {busy:4.0%}  switches/s {len(slices[tid])/span:7.0f}")
    print(f"      on-CPU slice {q(slices[tid])}")
    if gaps.get(tid):
        g = np.array(gaps[tid])
        print(f"      off-CPU gap  {q(g)}  gap time >=1ms {g[g>=1e-3].sum()/g.sum():4.0%}, >=10ms {g[g>=1e-2].sum()/g.sum():4.0%}")
