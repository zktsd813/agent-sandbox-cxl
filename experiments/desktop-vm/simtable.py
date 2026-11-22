#!/usr/bin/env python3
"""Compact table of an imbalance_wide.py "small"/"smalldesk" result: per server size, memory per core and load."""
import collections, json, sys
import numpy as np
a = collections.defaultdict(list)
for line in open(sys.argv[1]):
    r = json.loads(line)
    a[(r["cpus"], r["dram_per_cpu"], r["rho"], r["rule"])].append(r)
def m(k, x):
    return np.mean([r[x] for r in a[k]])
rules = sys.argv[2].split(",") if len(sys.argv) > 2 else ["random", "count-k2", "mem-k2", "cpu-k2"]
print("cpus GiB/core load | VMs/node | rule: memOver nodeP99 poolP99 | cpuUtil cpuOver beyond movable stretch")
for cpus, dram, rho in sorted({k[:3] for k in a}):
    for rule in rules:
        k = (cpus, dram, rho, rule)
        if k not in a:
            continue
        print(f"{cpus:3d} {dram} {rho} | {m(k, 'per_node'):5.1f} | {rule:9s} {m(k, 'mem_over_node_s'):.3f} "
              f"{m(k, 'mem_p99') - 1:+.3f} {m(k, 'pool_p99_share'):.3f} | {m(k, 'cpu_util'):.2f} {m(k, 'cpu_over_node_s'):.3f} "
              f"{m(k, 'cpu_beyond'):.4f} {m(k, 'cpu_movable'):.2f} {m(k, 'cpu_stretch'):.3f}")
