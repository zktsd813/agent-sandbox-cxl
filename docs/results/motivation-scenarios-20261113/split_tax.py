#!/usr/bin/env python3
"""CXL tax of a heavy command when the sandbox's earlier state stays in CXL and only the command runs from DRAM.

The "park in CXL, run the heavy command locally" story: between commands the sandbox lives in CXL; when a heavy
command arrives it is attached on a node and its new memory comes from that node's DRAM, with nothing copied first.
Container proxy on bud05 (NUMA 0 CPUs, node 0 DRAM, node 2 DCD):
  dram   commands 0..k-1 and command k bound to node 0
  cxl    commands 0..k-1 and command k bound to node 2
  split  commands 0..k-1 bound to node 2 (their page cache and files stay there), command k bound to node 0
The image's page cache is evicted first so file pages land on the bound node (as in the 11-12 pilot). Commands run
back to back (no LLM waits); a 2 s pause precedes command k. One fresh container per (stage, condition, repetition).

  sudo -n python3 split_tax.py all --out DIR [--reps 3]     # shuffled per repetition, finished runs skipped
"""
import argparse
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

OLD = Path("/home/ijkim/agent-sandbox-cxl")
TRACES = OLD / "harness/trace-replay/data/native"
spec = importlib.util.spec_from_file_location("replay", OLD / "harness/trace-replay/experiment.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

# heavy (test/build) stages: batch index in the trace; no timeout before them in the 10-14 replay
STAGES = {"projectlombok__lombok-3215": [22, 23, 36],      # ant compile, ant test, ant dist (11-12 pilot)
          "rubocop__rubocop-13424": [18, 26, 42],          # bundle exec rspec (11-12 pilot)
          "fmtlib__fmt-3729": [6, 16],                      # cmake && make -j, then rebuild after an edit
          "caddyserver__caddy-5761": [24],                  # go build
          "sharkdp__bat-2393": [10],                        # cargo test
          "preactjs__preact-3454": [6, 20],                 # npm run test:karma, first and repeated
          "google__gson-2134": [4, 13],                     # mvn test, then one test class
          "redis__redis-13338": [21]}                       # make -j && runtest
CPUS = "2-31"
CONTROL = {0, 1}


def evict_image(root):
    n = 0
    for top, dirs, names in os.walk(root):
        if top == root:
            dirs[:] = [d for d in dirs if d not in ("proc", "sys", "dev")]
        for name in names:
            try:
                fd = os.open(os.path.join(top, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                if (os.fstat(fd).st_mode & 0o170000) == 0o100000:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    n += 1
            except OSError:
                pass
            finally:
                os.close(fd)
    return n


def numa(cgroup):
    out = {}
    for line in (cgroup / "memory.numa_stat").read_text().splitlines():
        key, *nodes = line.split()
        if key in ("anon", "file"):
            for x in nodes:
                node, v = x.split("=")
                out[f"{key}_{node}"] = int(v)
    out["cpu_usec"] = int(dict(l.split() for l in (cgroup / "cpu.stat").read_text().splitlines())["usage_usec"])
    return out


def run_one(trace_name, stage, cond, rep, out, helper):
    trace = replay.load_trace(str(TRACES / f"{trace_name}.traj.json"))
    env = trace["environment"]
    pre_node = 0 if cond == "dram" else 2
    cmd_node = 2 if cond == "cxl" else 0
    name = f"st-{trace_name.split('__')[1]}-{stage}-{cond}"[:63]
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    replay.docker("run", "-d", "--name", name, "--cpuset-cpus", CPUS, "--cpuset-mems", "0,2",
                  "--memory", "32g", "--memory-swap", "32g", "--pids-limit", "16384", "-w", env["cwd"],
                  "--mount", f"type=bind,src={helper},dst=/opt/mini-numa-bind,readonly", "--cap-add", "SYS_NICE",
                  env["image"], "/opt/mini-numa-bind", str(pre_node), "sleep", "infinity")
    rec = dict(trace=trace_name, stage=stage, cond=cond, rep=rep, t0=time.time(), loadavg=os.getloadavg())
    try:
        cgroup = replay.container_group(name)
        pid = replay.docker("inspect", "--format", "{{.State.Pid}}", name)
        rec["evicted_files"] = evict_image(f"/proc/{pid}/root")
        base = ["docker", "exec", "-w", env["cwd"], *[x for k, v in env.get("env", {}).items() for x in ("-e", f"{k}={v}")], name,
                "/opt/mini-numa-bind"]
        logdir = out / "logs"
        logdir.mkdir(exist_ok=True)
        mism = 0
        t = time.monotonic()
        with open(logdir / f"{name}-r{rep}.log", "w") as log:
            for index, batch in enumerate(trace["batches"][:stage]):
                for ai, action in enumerate(batch["actions"]):
                    code, to = replay.run_action([*base, str(pre_node), *env["interpreter"], action["command"]], log,
                                                 env.get("timeout", 60), {"failure": None}, _Never())
                    exp = batch["observations"][ai]["returncode"] if batch["observations"] else None
                    mism += int(to or (exp is not None and code != exp))
            rec["prefix_s"] = time.monotonic() - t
            rec["prefix_mismatch"] = mism
            time.sleep(2.0)
            batch = trace["batches"][stage]
            assert len(batch["actions"]) == 1
            rec["before"] = numa(cgroup)
            log.write(f"\n=== measured batch {stage} on node {cmd_node} ===\n")
            log.flush()
            t = time.monotonic()
            code, to = replay.run_action([*base, str(cmd_node), *env["interpreter"], batch["actions"][0]["command"]], log,
                                         env.get("timeout", 60), {"failure": None}, _Never())
            rec["wall_s"] = time.monotonic() - t
            rec["after"] = numa(cgroup)
        rec["returncode"], rec["timed_out"] = code, to
        rec["expected_returncode"] = batch["observations"][0]["returncode"] if batch["observations"] else None
        rec["command"] = batch["actions"][0]["command"][:200]
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return rec


class _Never:
    def is_set(self):
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["all", "one"])
    p.add_argument("--out", required=True)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--trace")
    p.add_argument("--stage", type=int)
    p.add_argument("--cond")
    a = p.parse_args()
    os.sched_setaffinity(0, CONTROL)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    helper = replay.build_numa_helper(out)
    res = out / "runs.jsonl"
    done = set()
    if res.exists():
        done = {(r["trace"], r["stage"], r["cond"], r["rep"]) for r in map(json.loads, res.open())}
    if a.mode == "one":
        jobs = [(a.trace, a.stage, a.cond, 0)]
    else:
        jobs = []
        for rep in range(1, a.reps + 1):
            x = [(t, s, c, rep) for t, ss in STAGES.items() for s in ss for c in ("dram", "cxl", "split")]
            random.Random(rep).shuffle(x)
            jobs += x
    with res.open("a") as f:
        for job in jobs:
            if job in done:
                continue
            t = time.time()
            rec = run_one(*job, out, helper)
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{time.strftime('%T')} {job} wall {rec.get('wall_s', float('nan')):.2f}s rc {rec.get('returncode')}/"
                  f"{rec.get('expected_returncode')} prefix_mismatch {rec.get('prefix_mismatch')} ({time.time() - t:.0f}s)",
                  flush=True)
    owner = os.environ.get("SUDO_UID")
    if owner:
        for path in [out, *out.rglob("*")]:
            os.chown(path, int(owner), int(os.environ.get("SUDO_GID", owner)))


if __name__ == "__main__":
    main()
