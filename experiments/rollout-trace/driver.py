"""Collect R2E-Gym rollouts image by image (plan §7, §8).

Starts llm_proxy, docker_events, cgroup_sampler and vllm_metrics, then for
each task: pull its image (DockerSafety), run its G rollouts with at most
--concurrency at a time, remove this run's containers and the image we
pulled. Rollout k of task t gets the id <W>~<model>~<task>~g<k> and a
4-CPU cpuset taken in turn from --sandbox-cpus.

Every removal goes through DockerSafety: images that were on the host
before the run, or that anyone's container uses, are never touched.

usage: driver.py --workload W1 --model-tag deepswe --llm hosted_vllm/deepswe \
         --tasks tasks/W1.jsonl --n-tasks 1 --g 1 --root traces --run s1
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import manifest
from docker_safety import DockerSafety
from docker_events import split

HERE = Path(__file__).resolve().parent
PY = sys.executable
# Where setup/fetch_hf.sh put datasets and setup/setup_env.sh the venvs.
DATA_ROOT = os.environ.get("DATA_ROOT", "/mnt/ssd1/ijkim")
ENV_ROOT = Path(os.environ.get("ENV_ROOT", Path.home() / "rollout-env"))


def cpu_list(spec):
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return out


def safe_name(task_id):
    return task_id.replace("/", "_").replace(":", "@")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", required=True)
    ap.add_argument("--model-tag", required=True)
    ap.add_argument("--llm", required=True, help="litellm model name, e.g. hosted_vllm/deepswe")
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--n-tasks", type=int, required=True)
    ap.add_argument("--only-shared", action="store_true", help="only the 128-task subset")
    ap.add_argument("--g", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--root", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--dataset-dir", default=f"{DATA_ROOT}/datasets/R2E-Gym--R2E-Gym-Subset/data")
    ap.add_argument("--vllm", default="http://127.0.0.1:18000")
    ap.add_argument("--proxy-port", type=int, default=19000)
    ap.add_argument("--sandbox-cpus", default="0-15,32-47")
    ap.add_argument("--tool-cpu", default="24", help="core for the cgroup sampler")
    ap.add_argument("--min-free-gb", type=float, default=150)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--max-tokens", type=int, default=36864)
    ap.add_argument("--max-total-time", type=int, default=1200)
    ap.add_argument("--reward-timeout", type=int, default=300)
    a = ap.parse_args()

    root = Path(a.root).resolve()
    state = root / "_global"
    tasks = [json.loads(l) for l in Path(a.tasks).read_text().splitlines()]
    if a.only_shared:
        tasks = [t for t in tasks if t.get("shared128")]
    tasks = tasks[: a.n_tasks]

    safety = DockerSafety(state, a.run, min_free_gb=a.min_free_gb)
    safety.snapshot()
    manifest.write(root, {"run": a.run, "args": vars(a),
                          "scaffold_versions": (ENV_ROOT / "VERSIONS").read_text()})

    procs = []

    def bg(args, cpu=None):
        cmd = ([f"taskset", "-c", cpu] if cpu else []) + [PY, *args]
        procs.append(subprocess.Popen(cmd, cwd=HERE))

    bg(["llm_proxy.py", "--root", str(root), "--upstream", a.vllm, "--port", str(a.proxy_port)])
    bg(["docker_events.py", "--root", str(root), "--run", a.run])
    bg(["cgroup_sampler.py", "--root", str(root), "--run", a.run], cpu=a.tool_cpu)
    bg(["vllm_metrics.py", "--root", str(root), "--url", f"{a.vllm}/metrics"])
    time.sleep(2)

    cpus = cpu_list(a.sandbox_cpus)
    slots = [",".join(map(str, cpus[i:i + 4])) for i in range(0, len(cpus) - 3, 4)]
    log = (state / "driver.jsonl").open("a")

    def note(**kw):
        log.write(json.dumps({"t": time.time(), **kw}) + "\n")
        log.flush()

    def run_one(job):
        jf = state / "jobs" / f"{job['rollout'].replace('~', '__')}.json"
        jf.parent.mkdir(parents=True, exist_ok=True)
        jf.write_text(json.dumps(job))
        logf = state / "logs" / f"{jf.stem}.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        with logf.open("w") as lf:
            rc = subprocess.run([PY, "r2e_rollout.py", "--job", str(jf)], cwd=HERE,
                                stdout=lf, stderr=subprocess.STDOUT).returncode
        note(event="rollout_done", rollout=job["rollout"], rc=rc)

    try:
        n = 0
        for t in tasks:
            img = t["task_id"]
            note(event="pull_start", image=img, free_gb=safety.free_gb())
            safety.pull(img)
            note(event="pull_done", image=img, free_gb=safety.free_gb())
            jobs = []
            for k in range(a.g):
                rid = f"{a.workload}~{a.model_tag}~{safe_name(img)}~g{k}"
                jobs.append({
                    "run": a.run, "rollout": rid, "root": str(root), "task_id": img,
                    "dataset_dir": a.dataset_dir, "llm_name": a.llm,
                    "base_url": f"http://127.0.0.1:{a.proxy_port}/r/{rid}/v1",
                    "temperature": a.temperature, "max_steps": a.max_steps,
                    "max_tokens": a.max_tokens, "max_total_time": a.max_total_time,
                    "reward_timeout": a.reward_timeout, "group": k,
                    "cpuset": slots[n % len(slots)],
                })
                n += 1
            with ThreadPoolExecutor(a.concurrency) as ex:
                list(ex.map(run_one, jobs))
            note(event="containers_removed", names=safety.remove_containers())
            note(event="image_removed", image=img, removed=safety.remove_image(img), free_gb=safety.free_gb())
    finally:
        note(event="cleanup", names=safety.remove_containers())
        time.sleep(1)
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()
        split(root)


if __name__ == "__main__":
    main()
