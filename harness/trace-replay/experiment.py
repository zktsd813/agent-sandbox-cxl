"""Replay native mini traces in NUMA-bound Docker containers, without an LLM."""

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed


def download_traces(inventory_path, destination):
    inventory = json.loads(inventory_path.read_text())
    base_url = f"https://raw.githubusercontent.com/{inventory['repository']}/{inventory['commit']}/"
    destination.mkdir(parents=True, exist_ok=True)
    for index, entry in enumerate(inventory["traces"], 1):
        path = destination / Path(entry["path"]).name
        if path.exists():
            raw = path.read_bytes()
        else:
            with urllib.request.urlopen(base_url + entry["path"], timeout=60) as response:
                raw = response.read()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Trace size/hash mismatch: {path}")
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(raw)
            temporary.replace(path)
        print(f"[{index}/{len(inventory['traces'])}] verified {path.name}", flush=True)


def submission_fingerprint(text):
    warning = "/root/.bashrc: line 1: /opt/miniconda3/etc/profile.d/conda.sh: No such file or directory\n"
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(),
            "without_startup_warning_sha256": hashlib.sha256(text.replace(warning, "").encode()).hexdigest(),
            "startup_warning_count": text.count(warning)}


def load_trace(source):
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=30) as response:
            raw = response.read()
    else:
        raw = Path(source).read_bytes()
    trace = json.loads(raw)
    settings = trace["info"]["config"]
    if not settings["environment_type"].endswith(".DockerEnvironment"):
        raise ValueError(f"Only Docker traces are supported: {source}")
    environment = settings["environment"]
    if environment.get("forward_env"):
        raise ValueError("Host environment forwarding is unsupported")
    batches = []
    for message in trace["messages"]:
        extra = message.get("extra", {})
        if message["role"] == "assistant" and extra.get("actions"):
            batches.append({"at": extra.get("timestamp"), "actions": extra["actions"], "observations": []})
        elif batches and message["role"] in ("tool", "user") and "returncode" in extra:
            batches[-1]["observations"].append(extra)
    if not batches:
        raise ValueError(f"No recorded actions: {source}")
    previous_end = None
    for index, batch in enumerate(batches):
        times = [batch["at"]] + [observation.get("timestamp") for observation in batch["observations"]]
        if any(not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) for t in times):
            raise ValueError(f"Missing/invalid timestamp in batch {index}: {source}")
        if times != sorted(times) or previous_end is not None and batch["at"] < previous_end:
            raise ValueError(f"Out-of-order timestamps in batch {index}: {source}")
        observation_count = len(batch["observations"])
        if observation_count != len(batch["actions"]) and not (index == len(batches) - 1 and observation_count == 0):
            raise ValueError(f"Incomplete observations in batch {index}: {source}")
        if any(not isinstance(action.get("command"), str) for action in batch["actions"]):
            raise ValueError(f"Unsupported action in batch {index}: {source}")
        batch["gap"] = 0 if previous_end is None else batch["at"] - previous_end
        previous_end = times[-1]
    submission = trace["info"].get("submission", "")
    return {"source": source, "sha256": hashlib.sha256(raw).hexdigest(), "environment": environment,
            "source_exit_status": trace["info"].get("exit_status"),
            "source_submission_sha256": hashlib.sha256(submission.encode()).hexdigest()
                if trace["info"].get("exit_status") == "Submitted" else None,
            "source_submission_fingerprint": submission_fingerprint(submission),
            "command_count": sum(len(batch["actions"]) for batch in batches),
            "last_action_command": batches[-1]["actions"][-1]["command"],
            "name": trace.get("instance_id") or Path(source).name, "batches": batches,
            "first": batches[0]["at"], "last": previous_end}


def cpu_ids(value):
    result = set()
    for part in value.split(","):
        bounds = [int(number) for number in part.split("-")]
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def docker(*arguments, **kwargs):
    return subprocess.run(["docker", *arguments], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=kwargs.pop("timeout", 120), **kwargs).stdout.strip()


def task_image(trace, config):
    image = trace["environment"]["image"]
    return config["sample"]["currently_observed_image"] if image == config["sample"]["image_reference_from_trace"] else image


def systemd(*arguments):
    return subprocess.run(["sudo", "-n", "systemctl", *arguments], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120).stdout.strip()


def container_group(name):
    pid = docker("inspect", "--format", "{{.State.Pid}}", name)
    group = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if line.startswith("0::"))
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def create_budget(config, name, image_id):
    docker("run", "-d", "--name", name, "--cgroup-parent", config["budget_unit"],
           "--cpuset-cpus", config["controller_cpus"], "--cpuset-mems", "0", "--memory", "64m",
           "--memory-swap", "64m", "--pids-limit", "64", "--network", "none", image_id, "sleep", "infinity")
    systemd("set-property", "--runtime", config["budget_unit"],
            f"MemoryMax={config['aggregate_memory_limit_bytes']}", "MemorySwapMax=0",
            f"AllowedCPUs={config['node_cpu_pool']}", "AllowedMemoryNodes=0", "TasksMax=65536")
    group = container_group(name).parent
    if int((group / "memory.max").read_text()) != config["aggregate_memory_limit_bytes"]:
        raise RuntimeError("Shared memory budget was not applied")
    if (group / "memory.swap.max").read_text().strip() != "0" or (group / "cpuset.mems.effective").read_text().strip() != "0":
        raise RuntimeError("Shared NUMA/swap constraint was not applied")
    return group


def resources(cgroup, memory=True):
    cpu = dict(line.split() for line in (cgroup / "cpu.stat").read_text().splitlines())
    result = {"cpu_usec": int(cpu["usage_usec"]), "throttled_usec": int(cpu.get("throttled_usec", 0))}
    if not memory:
        return result
    memory_stat = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
    numa = {}
    for line in (cgroup / "memory.numa_stat").read_text().splitlines():
        kind, *nodes = line.split()
        if kind in ("anon", "file"):
            numa[kind] = dict(node.split("=") for node in nodes)
    events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
    pids = dict(line.split() for line in (cgroup / "pids.events").read_text().splitlines())
    peak = cgroup / "memory.peak"
    return {**result, "memory_bytes": int((cgroup / "memory.current").read_text()),
            "peak_bytes": int(peak.read_text()) if peak.exists() else None,
            "anon_bytes": int(memory_stat["anon"]), "file_bytes": int(memory_stat["file"]),
            "anon_node0_bytes": int(numa["anon"].get("N0", 0)),
            "anon_other_bytes": sum(int(size) for node, size in numa["anon"].items() if node != "N0"),
            "file_other_bytes": sum(int(size) for node, size in numa["file"].items() if node != "N0"),
            "swap_bytes": int((cgroup / "memory.swap.current").read_text()),
            "oom_kill": int(events.get("oom_kill", 0)),
            "pids_current": int((cgroup / "pids.current").read_text()), "pids_limit_events": int(pids.get("max", 0))}


def prepare(trace, config, name, pull, image_id=None):
    image = task_image(trace, config)
    if pull:
        docker("pull", image, timeout=1800)
    image_id = image_id or docker("image", "inspect", "--format", "{{.Id}}", image)
    docker("run", "-d", "--name", name, "--cgroup-parent", config["budget_unit"], "--cpuset-cpus", config["workload_cpus"],
           "--cpuset-mems", config["memory_nodes"], "--memory", str(config["memory_limit_bytes"]),
           "--memory-swap", str(config["memory_limit_bytes"]), "--pids-limit", str(config["pids_limit"]),
           "-w", trace["environment"]["cwd"], image_id, "sleep", "infinity")
    cgroup = container_group(name)
    cpus = (cgroup / "cpuset.cpus.effective").read_text().strip()
    mems = (cgroup / "cpuset.mems.effective").read_text().strip()
    if cpu_ids(cpus) != cpu_ids(config["workload_cpus"]) or mems != "0":
        raise RuntimeError(f"Unexpected effective cpuset for {name}: {cpus}, {mems}")
    if (cgroup / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError(f"Swap was not disabled for {name}")
    for filename, value in (("memory.max", config["memory_limit_bytes"]), ("pids.max", config["pids_limit"])):
        if (cgroup / filename).read_text().strip() != str(value):
            raise RuntimeError(f"Unexpected {filename} for {name}")
    return {"name": name, "trace": trace, "image_id": image_id, "cgroup": cgroup, "phase": "ready", "failure": None}


def sample(sessions, clock, intervals, stop, record, destination, keep_going=False):
    cached, next_memory = {}, 0
    with destination.open("w", newline="") as stream:
        writer = None
        while not stop.is_set():
            started = time.monotonic()
            read_memory = started >= next_memory
            for session in list(sessions):
                if session["phase"] == "done" or session["failure"]:
                    continue
                try:
                    values = resources(session["cgroup"], memory=read_memory or session["name"] not in cached)
                    cached.setdefault(session["name"], {}).update(values)
                    row = {"elapsed_s": time.monotonic() - clock, "sample_cycle_s": started - clock,
                           "session": session["name"],
                           "phase": session["phase"], **cached[session["name"]]}
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=row)
                        writer.writeheader()
                    writer.writerow(row)
                    if row["anon_other_bytes"] or row["swap_bytes"] or row["oom_kill"] or row["pids_limit_events"]:
                        record("error", session=session["name"], error="NUMA/swap/OOM/PID constraint failure", resources=values)
                        session["failure"] = "NUMA/swap/OOM/PID constraint failure"
                        if not keep_going:
                            stop.set()
                except OSError as error:
                    if session["phase"] != "done":
                        record("error", session=session["name"], error=str(error))
                        session["failure"] = str(error)
                        if not keep_going:
                            stop.set()
            if read_memory:
                next_memory = started + intervals["memory_interval_ms"] / 1000
            stop.wait(max(0, intervals["cpu_interval_ms"] / 1000 - (time.monotonic() - started)))


def remove_container(name, record):
    try:
        docker("rm", "-f", name)
        return True
    except subprocess.CalledProcessError as error:
        if "No such container" in error.stderr:
            return True
        record("error", session=name, error=f"Container cleanup failed: {error.stderr}")
    except (OSError, subprocess.TimeoutExpired) as error:
        record("error", session=name, error=f"Container cleanup failed: {error}")
    return False


def run_action(command, log, timeout, session, stop):
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as process:
        while process.poll() is None:
            if stop.is_set() or session["failure"]:
                process.kill()
                process.wait()
                raise RuntimeError(session["failure"] or "Replay interrupted")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                return -1, True  # mini kills the CLI; the container command may survive.
            try:
                process.wait(timeout=min(.2, remaining))
            except subprocess.TimeoutExpired:
                pass
        return process.returncode, False


def replay(session, origin, clock, arguments, record, stop, output):
    trace = session["trace"]
    offset = trace["first"] - origin if arguments.starts == "recorded" else 0
    environment = trace["environment"]
    command = ["docker", "exec", "-w", environment["cwd"]]
    for key, value in environment.get("env", {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([session["name"], *environment["interpreter"]])
    try:
        with (output / f"{session['name']}.log").open("w") as log:
            for index, batch in enumerate(trace["batches"]):
                if arguments.schedule == "timestamps" or index == 0:
                    planned = clock + (offset + batch["at"] - trace["first"]) * arguments.scale
                else:
                    planned = time.monotonic() + batch["gap"] * arguments.scale
                session["phase"] = "waiting"
                while planned > time.monotonic() and not stop.is_set() and not session["failure"]:
                    stop.wait(min(.2, planned - time.monotonic()))
                if session["failure"]:
                    raise RuntimeError(session["failure"])
                if stop.is_set():
                    return
                session["phase"] = "running"
                if index == 0:
                    record("session_start", session=session["name"], initial_resources=resources(session["cgroup"]))
                record("batch_start", session=session["name"], batch=index,
                       planned_s=planned - clock, lateness_s=max(0, time.monotonic() - planned))
                for action_index, action in enumerate(batch["actions"]):
                    if stop.is_set():
                        return
                    started = time.monotonic()
                    log.write(f"\n--- batch {index}, action {action_index} ---\n{action['command']}\n")
                    log.flush()
                    record("command_start", session=session["name"], batch=index, action=action_index, started_s=started - clock)
                    returncode, timed_out = run_action([*command, action["command"]], log,
                                                       environment.get("timeout", 60), session, stop)
                    if timed_out:
                        log.write("\n[Docker CLI timeout; preserving mini's command semantics]\n")
                    expected = batch["observations"][action_index]["returncode"] if batch["observations"] else None
                    record("command_end", session=session["name"], batch=index, action=action_index,
                           started_s=started - clock, duration_s=time.monotonic() - started,
                           returncode=returncode, expected_returncode=expected, timed_out=timed_out)
                record("batch_end", session=session["name"], batch=index)
            final = resources(session["cgroup"])
            if final["oom_kill"] or final["anon_other_bytes"] or final["swap_bytes"] or final["pids_limit_events"]:
                raise RuntimeError(f"OOM or NUMA/swap constraint failure: {final}")
            record("session_end", session=session["name"], final_resources=final)
    except Exception as error:
        record("error", session=session["name"], error=str(error))
        try:
            record("session_failed", session=session["name"], final_resources=resources(session["cgroup"]))
        except OSError:
            pass
        if not arguments.keep_going:
            stop.set()
            raise
    finally:
        session["phase"] = "done"
        if not remove_container(session["name"], record):
            stop.set()
            raise RuntimeError(f"Could not clean up {session['name']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="*", help="Native .traj.json paths or HTTPS URLs")
    parser.add_argument("--download-traces", action="store_true", help="Download and verify the pinned trace inventory, then exit")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "configs/node0.json")
    parser.add_argument("--output", type=Path, default=Path("harness/trace-replay/results"))
    parser.add_argument("--schedule", choices=("timestamps", "gaps"), default="timestamps")
    parser.add_argument("--starts", choices=("recorded", "together"), default="recorded")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--setup", choices=("precreate", "on-arrival"), default="precreate")
    parser.add_argument("--max-active", type=int, help="Maximum resident containers; still subject to the memory budget")
    parser.add_argument("--workload-cpus", help="Shared workload CPU pool, restricted to node 0")
    parser.add_argument("--keep-going", action="store_true", help="Continue other sessions after an individual workload failure")
    parser.add_argument("--pull", action="store_true", help="Download missing task images before measurement")
    parser.add_argument("--plan", action="store_true", help="Inspect timing; do not contact Docker or execute actions")
    arguments = parser.parse_args()
    if arguments.download_traces:
        if arguments.traces:
            parser.error("--download-traces does not accept replay inputs")
        data = Path(__file__).parent / "data"
        download_traces(data / "inventory.json", data / "native")
        return
    if not arguments.traces:
        parser.error("Provide trace inputs or use --download-traces")
    if not math.isfinite(arguments.scale) or arguments.scale <= 0:
        parser.error("--scale must be finite and positive")
    config = json.loads(arguments.config.read_text())
    if arguments.workload_cpus:
        config["workload_cpus"] = arguments.workload_cpus
    if arguments.max_active is not None:
        if arguments.max_active < 1:
            parser.error("--max-active must be positive")
        config["max_concurrent_sessions"] = arguments.max_active
    traces = [load_trace(source) for source in arguments.traces]
    origin = min(trace["first"] for trace in traces)
    windows = [(trace["first"] - origin if arguments.starts == "recorded" else 0,
                trace["last"] - trace["first"]) for trace in traces]
    events = sorted([(start, 1) for start, span in windows] + [(start + span, -1) for start, span in windows])
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    plan = {"schedule": arguments.schedule, "starts": arguments.starts, "scale": arguments.scale,
            "experiment_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "controller_pid": os.getpid(),
            "invocation": [sys.executable, *sys.argv],
            "source_origin_unix": origin, "observed_window_peak": peak,
            "observed_schedule_span_s": max(start + span for start, span in windows) * arguments.scale,
            "kernel": os.uname().release,
            "clock_assumption": "Recorded timestamps are comparable across the selected traces.",
            "container_setup": arguments.setup,
            "keep_going": arguments.keep_going,
            "setup_note": "On-arrival creates containers at the first recorded action; original creation times are unknown.",
            "timeout_semantics": "Like mini: kill the Docker CLI, record -1 and continue; its container command may survive.",
            "config": config, "traces": [{key: value for key, value in trace.items() if key != "batches"} for trace in traces]}
    if arguments.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    if arguments.setup == "precreate" and len(traces) > config["max_concurrent_sessions"]:
        parser.error("Too many resident sessions for the configured budget")
    available = cpu_ids(Path(f"/sys/devices/system/node/node{config['numa_node']}/cpulist").read_text().strip())
    if config["numa_node"] != 0 or config["memory_nodes"] != "0" or not cpu_ids(config["workload_cpus"]) <= available:
        parser.error("This baseline requires node 0 CPUs and memory")
    if not cpu_ids(config["controller_cpus"]) <= available:
        parser.error("Controller CPUs must also belong to node 0")
    if cpu_ids(config["controller_cpus"]) & cpu_ids(config["workload_cpus"]):
        parser.error("Controller and workload CPUs must be disjoint")
    meminfo = Path("/sys/devices/system/node/node0/meminfo").read_text().splitlines()
    free_bytes = next(int(line.split()[-2]) * 1024 for line in meminfo if "MemFree:" in line)
    budget_sessions = min(len(traces), config["max_concurrent_sessions"])
    config["aggregate_memory_limit_bytes"] = min(config["aggregate_memory_limit_bytes"], int(free_bytes * .8) // 4096 * 4096)
    if config["memory_limit_bytes"] > config["aggregate_memory_limit_bytes"]:
        parser.error("Shared budget is smaller than one session's limit")
    plan["node0_free_bytes_at_check"] = free_bytes
    if not os.environ.get("MINI_REPLAY_BOUND"):
        os.execvpe("numactl", ["numactl", f"--physcpubind={config['controller_cpus']}", "--membind=0",
                              sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                   {**os.environ, "MINI_REPLAY_BOUND": "1"})
    if docker("info", "--format", "{{.CgroupVersion}}") != "2":
        raise RuntimeError("cgroup v2 is required")
    subprocess.run(["sudo", "-n", "true"], check=True)
    images = {}
    for trace in traces:
        image = task_image(trace, config)
        if image not in images:
            if arguments.pull:
                docker("pull", image, timeout=1800)
            images[image] = docker("image", "inspect", "--format", "{{.Id}}", image)
    run_id = uuid.uuid4().hex
    config["budget_unit"] = f"minireplay{run_id}.slice"
    budget_name = f"mini-replay-budget-{run_id}"
    arguments.output.mkdir(parents=True, exist_ok=False)
    (arguments.output / "manifest.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    sessions, names = [], []
    stop, lock = threading.Event(), threading.Lock()
    executor = sampler = budget = None
    with (arguments.output / "events.jsonl").open("w") as event_log:
        clock = time.monotonic()

        def record(kind, **values):
            with lock:
                event_log.write(json.dumps({"event": kind, "elapsed_s": time.monotonic() - clock, **values}) + "\n")
                event_log.flush()

        try:
            budget = create_budget(config, budget_name, next(iter(images.values())))
            record("budget_ready", unit=config["budget_unit"], cgroup=str(budget), resources=resources(budget))
            names = [f"mini-replay-{run_id}-{index}" for index in range(len(traces))]
            if arguments.setup == "precreate":
                for trace, name in zip(traces, names):
                    session = prepare(trace, config, name, False, images[task_image(trace, config)])
                    sessions.append(session)
                    images[task_image(trace, config)] = session["image_id"]
            clock = time.monotonic()
            record("budget_start", resources=resources(budget))
            record("replay_clock_start", wall_time_unix=time.time(), sessions=[{"session": name, "trace": trace["name"],
                   "image_id": images[task_image(trace, config)]} for trace, name in zip(traces, names)])
            sampler = threading.Thread(target=sample, args=(sessions, clock, config["sampling"],
                                                            stop, record, arguments.output / "metrics.csv", arguments.keep_going))
            sampler.start()
            executor = ThreadPoolExecutor(max_workers=budget_sessions)
            if arguments.setup == "precreate":
                futures = [executor.submit(replay, s, origin, clock, arguments, record, stop, arguments.output) for s in sessions]
            else:
                def start_session(trace, name, planned):
                    if stop.is_set():
                        return
                    record("container_start", session=name, admission_delay_s=max(0, time.monotonic() - planned))
                    try:
                        session = prepare(trace, config, name, False, images[task_image(trace, config)])
                    except Exception as error:
                        record("error", session=name, error=str(error))
                        if not remove_container(name, record):
                            stop.set()
                        if not arguments.keep_going:
                            stop.set()
                            raise
                        return
                    sessions.append(session)
                    record("container_ready", session=name, image_id=session["image_id"])
                    replay(session, origin, clock, arguments, record, stop, arguments.output)

                futures = []
                for trace, name in sorted(zip(traces, names), key=lambda pair: pair[0]["first"]):
                    offset = trace["first"] - origin if arguments.starts == "recorded" else 0
                    planned = clock + offset * arguments.scale
                    if stop.wait(max(0, planned - time.monotonic())):
                        break
                    record("session_queued", session=name, planned_s=planned - clock)
                    futures.append(executor.submit(start_session, trace, name, planned))
            for future in as_completed(futures):
                future.result()
            if stop.is_set():
                raise RuntimeError("Measurement or replay was interrupted; inspect events.jsonl")
        except Exception as error:
            record("error", error=str(error), details=getattr(error, "stderr", None))
            stop.set()
            raise
        finally:
            stop.set()
            if sampler:
                sampler.join()
            cleanup_ok = True
            for name in names:
                if not remove_container(name, record):
                    cleanup_ok = False
            if executor:
                executor.shutdown(wait=True)
            if budget:
                record("budget_end", resources=resources(budget))
            if not remove_container(budget_name, record):
                cleanup_ok = False
            try:
                systemd("stop", config["budget_unit"])
                systemd("revert", config["budget_unit"])
            except subprocess.CalledProcessError as error:
                record("error", error=f"Budget cleanup failed: {error.stderr}")
                cleanup_ok = False
            if not cleanup_ok:
                raise RuntimeError("Container cleanup failed; inspect events.jsonl")
    print(f"Results: {arguments.output}")


if __name__ == "__main__":
    main()
