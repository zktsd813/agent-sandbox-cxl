#!/usr/bin/env python3
"""Run the capacity grid in parallel; resumable jsonl output."""
import itertools, json, os, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import capsim

BASE = dict(nodes=16, ramp_s=600, dur_s=2400, tau_hs=0.02, tau_ls=0.03, tau_hn=0.6, tau_ln=0.05, lat_ms=10,
            nic_gbps=100, copy_base_gib=0.1)
SETUPS = [("dram", 0, "none"), ("dram", 0, "copy")]
for x in (2, 4, 8):
    SETUPS += [("expander", x, "none"), ("expander", x, "copy"), ("pool", x, "none"), ("pool", x, "handoff")]


FINE = [s for s in SETUPS if s[1] in (0, 2, 8)]
WHICH = sys.argv[3] if len(sys.argv) > 3 else "coarse"


def grid():
    if WHICH == "fine":
        space = itertools.product(("hwm", "actual"), (96, 48, 192), (1, 2, 4), FINE,
                                  [round(0.5 + 0.05 * i, 2) for i in range(21)], (1, 2, 3))
    else:
        space = itertools.product(("hwm", "actual"), (96, 48, 192), (2, 4), SETUPS,
                                  [round(0.3 + 0.1 * i, 1) for i in range(14)], (1, 2))
    for resident, cores, d, (mem, x, move), dens, seed in space:
        yield dict(BASE, resident=resident, cores=cores, dram_per_core=d, mem=mem, cxl_per_core=x, move=move,
                   density=dens, seed=seed)


def key(c):
    return json.dumps({k: c[k] for k in sorted(c)}, sort_keys=True)


_inputs = None


def work(c):
    global _inputs
    if _inputs is None:
        _inputs = capsim.load_inputs(capsim.HERE / ".local/inputs.npz")
    return capsim.run(c, _inputs)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    done = set()
    if out.exists():
        for line in out.open():
            r = json.loads(line)
            done.add(key({k: r[k] for k in r if k in next(grid())}))
    todo = [c for c in grid() if key(c) not in done]
    print(f"{len(todo)} to run", flush=True)
    work(dict(todo[0], dur_s=10, ramp_s=10)) if todo else None  # compile once
    with ProcessPoolExecutor(int(sys.argv[2])) as ex, out.open("a") as f:
        for i, fut in enumerate(as_completed([ex.submit(work, c) for c in todo])):
            f.write(json.dumps(fut.result()) + "\n"); f.flush()
            if i % 100 == 0:
                print(i, flush=True)
