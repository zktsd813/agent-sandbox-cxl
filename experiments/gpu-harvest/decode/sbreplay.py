"""Replay the C4 sandbox commands (real agent tool calls, setup and grading) back to back in fresh containers.
usage: sbreplay.py OUT.jsonl STREAMS SECONDS CPUSET
Uses the rollouts whose images are present locally. Each stream loops over them (offset by stream index) until SECONDS
elapse; LLM waits are removed, so this is a dense sandbox load. Containers get the C4 limits (2 CPUs, 4 GiB, no
network) and are pinned to CPUSET. One JSON line per command: stream, pass, rollout, idx, phase, elapsed_s, rc;
one per rollout with its container CPU (cgroup usage_usec).
"""
import json, subprocess, sys, threading, time
from pathlib import Path

TRACE = Path(__file__).resolve().parents[1] / "results/g1-C4-20261103/train/attempts/0001/trace"
out_path, streams, seconds, cpuset = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
have = set(subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True,
                          text=True, check=True).stdout.split())
rollouts = {}  # rollout_id -> dict(image, env, steps[(kind, ...)])
for line in open(TRACE / "events.jsonl"):
    e = json.loads(line)
    r = e.get("rollout_id")
    if e["event"] == "sandbox_ready":
        cfg = json.load(open(TRACE / e["inspect"]["path"]))["Config"]
        rollouts[r] = dict(image=e["image"], env=cfg["Env"], workdir=cfg["WorkingDir"], cmd=cfg["Cmd"], steps=[])
    elif e["event"] == "tool_copy" and r in rollouts:
        rollouts[r]["steps"].append(("copy", str(TRACE / e["contents"]["path"]), e["destination"]))
    elif e["event"] == "command_start" and r in rollouts:
        rollouts[r]["steps"].append(("exec", e["execution_phase"], e["argv"], e["cwd"], e["env"]))
order = sorted(r for r, v in rollouts.items() if v["image"] in have)
lock = threading.Lock()
fout = open(out_path, "w")


def emit(d):
    with lock:
        fout.write(json.dumps(d) + "\n"); fout.flush()


def run_rollout(sid, p, rid):
    v = rollouts[rid]
    cmd = ["docker", "run", "-d", "-t", "--cpuset-cpus", cpuset, "--cpus", "2", "--memory", "4g", "--network", "none",
           "-w", v["workdir"]] + sum((["-e", x] for x in v["env"]), []) + [v["image"]] + v["cmd"]
    cid = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    try:
        idx = 0
        for st in v["steps"]:
            if st[0] == "copy":
                subprocess.run(["docker", "cp", st[1], f"{cid}:{st[2]}"], capture_output=True, check=True)
                continue
            _, phase, argv, cwd, env = st
            ex = ["docker", "exec", "-w", cwd] + sum((["-e", f"{k}={x}"] for k, x in env.items()), []) + [cid] + argv
            t0 = time.perf_counter()
            rc = subprocess.run(ex, capture_output=True).returncode
            emit(dict(stream=sid, pass_=p, rollout=rid[:8], idx=idx, phase=phase, elapsed_s=time.perf_counter() - t0,
                      rc=rc, t=time.time()))
            idx += 1
        stat = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat").read_text().split()
        emit(dict(stream=sid, pass_=p, rollout=rid[:8], container_cpu_s=int(stat[stat.index("usage_usec") + 1]) / 1e6))
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def stream(sid):
    t0, p = time.perf_counter(), 0
    while time.perf_counter() - t0 < seconds:
        for k in range(len(order)):
            if time.perf_counter() - t0 >= seconds:
                return
            run_rollout(sid, p, order[(k + sid * len(order) // max(streams, 1)) % len(order)])
        p += 1


ts = [threading.Thread(target=stream, args=(i,)) for i in range(streams)]
for t in ts: t.start()
for t in ts: t.join()
