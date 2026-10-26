"""Sample cgroup v2 counters of our rollout containers (M5).

Every 10 ms: cpu.stat usage/user/system/throttled. Every 100 ms also
cpu.pressure, memory.current, memory.peak and io.stat totals. One CSV per
container under <rollout dir>/cgroup/<container id[:12]>.csv. The host-wide
/proc/pressure/cpu and loadavg go to <root>/_global/host_pressure.csv so
that load from other users of a shared server can be told apart.

New containers are found every 100 ms from the cgroup tree and kept if
they carry this run's trace.run label.
Only reads files; run it pinned to a core of its own (taskset -c N).

usage: cgroup_sampler.py --root traces --run RUN_ID
"""

import argparse
import time
from pathlib import Path

import docker

from docker_safety import LABEL_ROLLOUT, LABEL_RUN
from llm_proxy import rollout_dir

CG = Path("/sys/fs/cgroup/system.slice")
FAST, SLOW, DISCOVER = 0.010, 0.100, 0.100


def kv(text):
    return dict(line.split()[:2] for line in text.splitlines() if line.strip())


def pressure(text):
    """'some avg10=.. total=N' -> (some_total_us, full_total_us)."""
    out = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        out[kind] = int(dict(f.split("=") for f in fields)["total"])
    return out.get("some", ""), out.get("full", "")


def io_totals(text):
    rb = wb = 0
    for line in text.splitlines():
        f = dict(x.split("=") for x in line.split()[1:] if "=" in x)
        rb += int(f.get("rbytes", 0))
        wb += int(f.get("wbytes", 0))
    return rb, wb


def read(p):
    try:
        return p.read_text()
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None


class Target:
    HEADER = "t,usage_us,user_us,system_us,throttled_us,psi_some_us,psi_full_us,mem_cur,mem_peak,io_rbytes,io_wbytes\n"

    def __init__(self, cid, out_csv):
        self.dir = CG / f"docker-{cid}.scope"
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        self.f = out_csv.open("a")
        if out_csv.stat().st_size == 0:
            self.f.write(self.HEADER)

    def sample(self, now, slow):
        s = read(self.dir / "cpu.stat")
        if s is None:
            return False
        c = kv(s)
        row = [f"{now:.4f}", c.get("usage_usec"), c.get("user_usec"), c.get("system_usec"),
               c.get("throttled_usec", "")]
        if slow:
            ps = read(self.dir / "cpu.pressure")
            some, full = pressure(ps) if ps else ("", "")
            io = read(self.dir / "io.stat")
            rb, wb = io_totals(io) if io is not None else ("", "")
            row += [some, full, (read(self.dir / "memory.current") or "").strip(),
                    (read(self.dir / "memory.peak") or "").strip(), rb, wb]
        else:
            row += [""] * 6
        self.f.write(",".join(str(x) for x in row) + "\n")
        return True

    def close(self):
        self.f.close()


def run(root, run_id):
    root = Path(root)
    client = docker.from_env()
    targets, seen = {}, set()
    (root / "_global").mkdir(parents=True, exist_ok=True)
    host_path = root / "_global" / "host_pressure.csv"
    new = not host_path.exists()
    host = host_path.open("a")
    if new:
        host.write("t,host_cpu_some_us,host_cpu_full_us,load1\n")
    next_fast = next_slow = next_disc = time.time()
    while True:
        now = time.time()
        if now >= next_disc:
            # Listing cgroup dirs is cheap; ask Docker only about new ones.
            next_disc = now + DISCOVER
            for p in CG.glob("docker-*.scope"):
                cid = p.name[len("docker-"):-len(".scope")]
                if cid in targets or cid in seen:
                    continue
                seen.add(cid)
                try:
                    labels = client.containers.get(cid).labels
                except docker.errors.NotFound:
                    continue
                if labels.get(LABEL_RUN) == run_id:
                    rid = labels.get(LABEL_ROLLOUT, "unknown")
                    targets[cid] = Target(cid, rollout_dir(root, rid) / "cgroup" / f"{cid[:12]}.csv")
        slow = now >= next_slow
        if slow:
            next_slow = now + SLOW
            some, full = pressure(read(Path("/proc/pressure/cpu")))
            load1 = read(Path("/proc/loadavg")).split()[0]
            host.write(f"{now:.3f},{some},{full},{load1}\n")
        for cid in list(targets):
            if not targets[cid].sample(now, slow):  # container gone
                targets.pop(cid).close()
        if slow:
            host.flush()
            for t in targets.values():
                t.f.flush()
        next_fast += FAST
        delay = next_fast - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            next_fast = time.time()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    run(a.root, a.run)


if __name__ == "__main__":
    main()
