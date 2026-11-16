#!/usr/bin/env python3
"""Run one isolated, locked SMP handoff trial and retain its evidence."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LOCAL = HERE / ".local"
BASE = Path("/home/ijkim/agent-sandbox-cxl-latest")
LOCKS = BASE / ".codex-runs"
INPUTS = BASE / "implementation/virtual_machine/.local"


def node0_free():
    for line in Path("/sys/devices/system/node/node0/meminfo").read_text().splitlines():
        if "MemFree:" in line:
            return int(line.split()[-2]) * 1024
    raise RuntimeError("node 0 MemFree missing")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vcpus", type=int, choices=[1, 2, 4, 8], required=True)
    p.add_argument("--iterations", type=int, default=210)
    p.add_argument("--warmup", type=int, default=10)
    p.add_argument("--measure", action="store_true", help="Hold quiet.lock exclusively")
    p.add_argument("--test-running-vcpu", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--firecracker", type=Path, default=LOCAL / "fc/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker")
    p.add_argument("--qemu", type=Path, default=INPUTS / "run09-qemu/bin/qemu-system-x86_64")
    args = p.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to(HERE.parents[1]):
        p.error("output must be inside this worktree")
    args.output.mkdir(parents=True, exist_ok=False)
    session = "mvcpu-" + datetime.datetime.now(datetime.timezone.utc).strftime("%m%d%H%M%S")
    assets = LOCAL / ("assets-" + session)
    current = LOCAL / ("current-" + session + ".json")
    env = dict(os.environ, CXL_LAB_SESSION=session, CXL_LAB_ASSETS=str(assets),
               CXL_LAB_KERNEL_SOURCE=str(LOCAL / "linux"),
               CXL_LAB_QEMU_ROOT=str(INPUTS / "qemu-cxl"))
    # The input wrapper has its own read-only library/firmware search paths.
    command = ["numactl", "--physcpubind=0-23", "--membind=0", sys.executable, str(HERE / "lab.py")]
    result = {"result": "running", "vcpus": args.vcpus, "iterations": args.iterations,
              "warmup": args.warmup, "exclusive_quiet": args.measure,
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "memory_policy": "NUMA node 0; CPUs 0-23", "baseline_local_gib": 35,
              "session": session}

    def run(name, *arguments):
        print(name, flush=True)
        with (args.output / (name + ".log")).open("wb") as log:
            subprocess.run([*command, *map(str, arguments)], env=env, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=1800)

    print("Waiting for lab.lock and quiet.lock", flush=True)
    with (LOCKS / "lab.lock").open("rb") as lab_lock, (LOCKS / "quiet.lock").open("rb") as quiet_lock:
        fcntl.flock(lab_lock, fcntl.LOCK_EX)
        fcntl.flock(quiet_lock, fcntl.LOCK_EX if args.measure else fcntl.LOCK_SH)
        try:
            run("prepare", "prepare", "--firecracker", args.firecracker,
                "--l1-kernel", LOCAL / "kernel-build/arch/x86/boot/bzImage",
                "--module-dir", LOCAL / "kernel-modules/lib/modules/6.6.0-rc6-dcd-v3",
                "--l2-kernel", INPUTS / "guest-swe-build/vmlinux")
            # 2 x 4 GiB L1 RAM + one shared 2 GiB CXL file + ivshmem + 4 GiB reserve.
            budget = (2 * 4096 + 2048 + 4) * 1024 * 1024
            free = node0_free()
            result.update(node0_free_before_bytes=free, budget_bytes=budget,
                          reserve_bytes=4 * 1024**3)
            if free < budget + result["reserve_bytes"]:
                raise RuntimeError("node 0 free memory below budget + 4 GiB")
            run("start", "start", "--vcpus", max(4, args.vcpus + 2), "--memory-mib", 4096,
                "--shared-mib", 4, "--cxl-mib", 2048, "--dax-handoff", "--dax-guest",
                "--dax-4k", "--network-backend", "socket", "--memory-backing-dir", "/dev/shm",
                "--qemu", args.qemu)
            run("verify", "verify")
            bench_args = ["direct-handoff-bench", "--guest", "linux", "--vcpus", args.vcpus,
                          "--iterations", args.iterations, "--warmup", args.warmup,
                          "--executor-cpu", 1, "--guest-cpu", 0, "--control", "http",
                          "--poll-us", 50, "--dwell-ms", 20, "--reuse-fds"]
            if args.test_running_vcpu:
                bench_args += ["--test-running-vcpu"]
            run("handoff", *bench_args)
            config = json.loads((Path(json.loads(current.read_text())["run"]) / "run.json").read_text())
            reports = sorted(Path(config["run"]).glob("direct-linux-handoff-*/report.json"))
            if len(reports) != 1:
                raise RuntimeError("Expected exactly one handoff report")
            shutil.copy2(reports[0], args.output / "handoff.json")
            result.update(result="passed", report=str(reports[0]), run=config["run"])
        except BaseException as error:
            result.update(result="failed", error=str(error))
            raise
        finally:
            if current.exists():
                config_path = Path(json.loads(current.read_text())["run"]) / "run.json"
                config = json.loads(config_path.read_text())
                result["run"] = config["run"]
                try:
                    run("stop", "stop")
                finally:
                    result["cleanup"] = {"qemu_pids": [v["pid"] for v in config["vms"].values()],
                        "backing_removed": not Path(config.get("ephemeral_memory_dir") or config["run"]).exists()}
                    for report in Path(config["run"]).glob("direct-linux-handoff-*/report.json"):
                        shutil.copy2(report, args.output / "handoff.json")
            result["worktree_local_du"] = subprocess.check_output(["du", "-sh", str(LOCAL)], text=True).strip()
            (args.output / "trial.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
