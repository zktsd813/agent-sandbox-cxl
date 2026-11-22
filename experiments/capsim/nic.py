import itertools, json, sys
from concurrent.futures import ProcessPoolExecutor
import grid
def cfgs():
    for cores, d, dens, seed in itertools.product((48, 96, 192), (2, 4), [round(0.8 + 0.05 * i, 2) for i in range(15)], (1, 2, 3)):
        yield dict(grid.BASE, resident="hwm", cores=cores, dram_per_core=d, mem="expander", cxl_per_core=8, move="copy",
                   density=dens, seed=seed, nic_gbps=25)
if __name__ == "__main__":
    with ProcessPoolExecutor(26) as ex, open("nic25.jsonl", "w") as f:
        for r in ex.map(grid.work, list(cfgs())):
            f.write(json.dumps(r) + "\n")
