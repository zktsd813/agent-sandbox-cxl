"""Run one R2E-Gym rollout under the trace tools (W1, W5).

R2E-Gym is used as installed; three DockerRuntime methods are wrapped at
import time instead of editing its source:

- container name is trace-<run>-<rollout> and the container carries the
  trace.run / trace.rollout labels (docker_events and cgroup_sampler key on
  them) plus an optional cpuset;
- reusing an existing container requires the exact name *and* our label.
  R2E-Gym's own lookup is a substring match over all containers on the
  host, which on a shared server could pick up someone else's;
- copy_to_container (put_archive, not visible in Docker exec events) is
  logged with the file content so the rollout can be replayed.

Limits follow DeepSWE's RL training script (plan §6.2): 50 steps, 36,864
token context, temperature 1.0, 20 min agent time, 5 min reward.

usage: r2e_rollout.py --job job.json
  job: {run, rollout, root, task_id, dataset_dir, llm_name, base_url,
        temperature, max_steps, max_tokens, max_total_time, reward_timeout,
        cpuset?}
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path

from docker_safety import LABEL_ROLLOUT, LABEL_RUN
from llm_proxy import rollout_dir


def load_row(dataset_dir, task_id):
    import glob

    import pyarrow.parquet as pq

    for f in sorted(glob.glob(f"{dataset_dir}/*.parquet")):
        hit = pq.read_table(f, filters=[("docker_image", "=", task_id)])
        if hit.num_rows:
            return hit.slice(0, 1).to_pylist()[0]
    raise KeyError(task_id)


def patch_runtime(job, out):
    from r2egym.agenthub.runtime.docker import DockerRuntime

    name = re.sub(r"[^A-Za-z0-9_.-]", "-", f"trace-{job['run']}-{job['rollout']}")
    labels = {LABEL_RUN: job["run"], LABEL_ROLLOUT: job["rollout"]}
    put_dir = out / "put_archive"

    DockerRuntime._get_container_name = staticmethod(lambda image_name: name)

    orig_start = DockerRuntime.start_container

    def start_container(self, docker_image, command, ctr_name, **kw):
        if self.backend != "docker":
            return orig_start(self, docker_image, command, ctr_name, **kw)
        kw = dict(kw)
        kw["labels"] = {**kw.get("labels", {}), **labels}
        if job.get("cpuset"):
            kw["cpuset_cpus"] = job["cpuset"]
        existing = [c for c in self.client.containers.list(all=True, filters={"name": f"^/{ctr_name}$"})
                    if c.labels.get(LABEL_RUN) == job["run"]]
        if existing:
            self.container = existing[0]
            if self.container.status != "running":
                self.container.start()
            return
        try:
            self.container = self.client.containers.run(
                docker_image, command, name=ctr_name, detach=True, tty=True, stdin_open=True, **kw)
        except Exception as e:
            print("Container start error:", repr(e))
            self.stop_container()

    orig_copy = DockerRuntime.copy_to_container

    def copy_to_container(self, src_path, dest_path):
        data = Path(src_path).read_bytes()
        put_dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256(data).hexdigest()
        (put_dir / f"{h[:16]}.gz").write_bytes(gzip.compress(data))
        t0 = time.time()
        r = orig_copy(self, src_path, dest_path)
        with (out / "put_archive.jsonl").open("a") as f:
            f.write(json.dumps({"t_start": t0, "t_end": time.time(), "dest": dest_path,
                                "bytes": len(data), "sha256": h}) + "\n")
        return r

    DockerRuntime.start_container = start_container
    DockerRuntime.copy_to_container = copy_to_container


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    job = json.loads(Path(ap.parse_args().job).read_text())
    out = rollout_dir(Path(job["root"]), job["rollout"])
    out.mkdir(parents=True, exist_ok=True)
    os.environ["LLM_BASE_URL"] = job["base_url"]

    import r2egym
    from r2egym.agenthub.agent.agent import Agent, AgentArgs
    from r2egym.agenthub.environment.env import EnvArgs, RepoEnv

    # The agent config names its tool files relative to the R2E-Gym checkout.
    os.chdir(Path(r2egym.__file__).resolve().parents[2])
    patch_runtime(job, out)
    row = load_row(job["dataset_dir"], job["task_id"])
    meta = {**{k: v for k, v in job.items() if k != "root"}, "t_start": time.time()}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))

    result = {"reward": None, "exit_reason": None, "error": None}
    env = None
    try:
        t0 = time.time()
        env = RepoEnv(EnvArgs(ds=row), backend="docker")
        result["t_env_ready"] = time.time()
        result["env_setup_s"] = result["t_env_ready"] - t0
        cfg = Path(r2egym.__file__).parent / "agenthub/config/r2egym/edit_non_fn_calling.yaml"
        args = AgentArgs.from_yaml(cfg)
        args.llm_name = job["llm_name"]
        agent = Agent(name="EditAgent", args=args)
        traj = agent.run(env, use_fn_calling=False, max_steps=job["max_steps"],
                         max_steps_absolute=job["max_steps"], max_token_limit=job["max_tokens"],
                         max_total_time=job["max_total_time"], temperature=job["temperature"],
                         scaffold="r2egym")
        result["t_agent_done"] = time.time()
        result["exit_reason"] = getattr(traj, "exit_reason", None)
        result["num_steps"] = len(traj.trajectory_steps)
        result["truncated"] = result["exit_reason"] in ("abs_step_limit", "max_step_limit", "token_limit",
                                                        "traj_time_limit", "agent_max_step_limit")
        result["patch"] = traj.output_patch if hasattr(traj, "output_patch") else None
        t1 = time.time()
        reward, test_output = env.runtime._calculate_reward(get_test_output=True, timeout=job["reward_timeout"])
        result["t_reward_start"], result["t_reward_end"] = t1, time.time()
        result["reward"] = reward
        (out / "scaffold").mkdir(exist_ok=True)
        (out / "scaffold" / "trajectory.json").write_text(traj.model_dump_json())
        (out / "scaffold" / "test_output.txt").write_text(str(test_output))
    except Exception as e:
        import traceback
        result["error"] = traceback.format_exc()
        print(result["error"])
    finally:
        if env is not None:
            env.close()
        result["t_end"] = time.time()
        (out / "result.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
