#!/usr/bin/env python3
"""CPU side of image co-location (2026-11-18): GRPO groups of 8 whose siblings are packed b per node, with the image
page cache read on first use. Memory is not limited here (DRAM 1000 GiB/core); the memory ceiling is applied
afterwards by imgcap_analyze.py from the resident memory these runs report and the page cache sharing of
image_place.py. Resumable jsonl output.

  imgcap.py OUT.jsonl WORKERS
"""
import itertools, json, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import capsim

BASE = dict(nodes=16, ramp_s=600, dur_s=2400, tau_hs=0.02, tau_ls=0.03, tau_hn=0.6, tau_ln=0.05, lat_ms=10,
            nic_gbps=100, copy_base_gib=0.1, resident="hwm", dram_per_core=1000, mem="dram", cxl_per_core=0,
            group=8, read_gbps=1.0)
# no tax and full tax (all sandbox state in CXL); imgcap_analyze.py interpolates by the CXL share actually needed
SETUPS = {"dram": dict(move="none", tax_split=False), "cxl": dict(move="none", tax_split=True),
          "handoff-notax": dict(move="handoff", tax_split=False), "cxl-handoff": dict(move="handoff", tax_split=True)}
GAPS = {1: np.arange(0.4, 2.01, 0.1), 2: np.arange(0.4, 4.01, 0.2), 4: np.arange(0.8, 8.01, 0.4)}


def grid():
    for cores, gap, chunk, jitter, setup, seed in itertools.product((96,), GAPS, (1, 2, 4, 8), (0, 60), SETUPS, (1, 2)):
        for d in GAPS[gap]:
            yield dict(BASE, **SETUPS[setup], setup=setup, cores=cores, gap_scale=gap, chunk=chunk, jitter_s=jitter,
                       density=round(float(d), 2), seed=seed)


def key(c):
    return json.dumps({k: c[k] for k in sorted(c)}, sort_keys=True)


_inputs = None


def work(c):
    global _inputs
    if _inputs is None:
        _inputs = capsim.load_inputs(capsim.HERE / ".local/inputs.npz", capsim.HERE / ".local/ws-per-trace.json")
    return capsim.run(c, _inputs)


if __name__ == "__main__":
    out = __import__("pathlib").Path(sys.argv[1])
    sample = next(grid())
    done = set()
    if out.exists():
        for line in out.open():
            r = json.loads(line)
            done.add(key({k: r[k] for k in sample}))
    todo = [c for c in grid() if key(c) not in done]
    print(f"{len(todo)} to run", flush=True)
    if todo:
        work(dict(todo[0], dur_s=10, ramp_s=10))  # compile once
    with ProcessPoolExecutor(int(sys.argv[2])) as ex, out.open("a") as f:
        for i, fut in enumerate(as_completed([ex.submit(work, c) for c in todo])):
            f.write(json.dumps(fut.result()) + "\n"); f.flush()
            if i % 200 == 0:
                print(i, flush=True)
