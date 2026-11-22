"""11-18 placement sweep: sample size and key of the admission choice (docs/results/capacity-balance-20261118)."""
import json, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
import capsim
BASE = dict(nodes=16, ramp_s=600, dur_s=2400, tau_hs=0.02, tau_ls=0.03, tau_hn=0.6, tau_ln=0.05, lat_ms=10,
            nic_gbps=100, copy_base_gib=0.1, resident="hwm", dram_per_core=2)
DENS = {48: 0.85, 96: 1.05, 192: 1.2}
SET = [("dram", 0, "none"), ("pool", 8, "none"), ("pool", 8, "handoff")]
# (k, metric): 0 CPU now, 1 sandbox count, 2 reserved memory; k=0 means all nodes
PLACE = [(1, 0), (2, 0), (4, 0), (0, 0), (2, 1), (0, 1), (0, 2)]
def cfgs():
    for cores, d in DENS.items():
        for mem, x, mv in SET:
            for k, m in PLACE:
                for seed in (1, 2):
                    yield dict(BASE, cores=cores, mem=mem, cxl_per_core=x, move=mv, density=d, seed=seed,
                               place_k=k, place_metric=m)
_in = None
def work(c):
    global _in
    if _in is None:
        _in = capsim.load_inputs(capsim.HERE / ".local/inputs.npz")
    return capsim.run(c, _in)
if __name__ == "__main__":
    todo = list(cfgs())
    work(dict(todo[0], dur_s=10, ramp_s=10))
    with ProcessPoolExecutor(int(sys.argv[2])) as ex, open(sys.argv[1], "w") as f:
        for fut in as_completed([ex.submit(work, c) for c in todo]):
            f.write(json.dumps(fut.result()) + "\n"); f.flush()
    print("done")
