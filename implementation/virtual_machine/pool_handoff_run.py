#!/usr/bin/env python3
"""Run guest-authorized pooling in fresh state-only/direct lab pairs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import lab

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--qemu", type=Path, required=True)
    p.add_argument("--qemu-root", type=Path, required=True)
    p.add_argument("--kvm-memory-smoke", type=Path, required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--cpus", default="0-7")
    p.add_argument("--cycles", type=int, default=2)
    p.add_argument("--modes", nargs="+", choices=("state-only", "direct"),
                   default=["state-only", "direct"])
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{1,30}", a.session) or a.cycles < 2:
        p.error("Use a fresh lowercase session (at most 30 characters) and at least two cycles")
    if len(set(a.modes)) != len(a.modes):
        p.error("Each mode may appear only once")
    assets = a.assets.resolve(strict=True)
    manifest = json.loads((assets / "manifest.json").read_text())
    for name, digest in manifest["artifacts"].items():
        if Path(name).name != name or lab.sha256(assets / name) != digest:
            raise RuntimeError("Fixture hash mismatch: " + name)
    qemu = a.qemu.resolve(strict=True)
    smoke = a.kvm_memory_smoke.resolve(strict=True)
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = ("pool_handoff_run.py", "pool_handoff_bench.py", "pool_handoff_control.py",
               "guest_pool_protocol.py", "verify_pool_handoff.py", "lab.py",
               "firecracker.patch", "kvm-dax-state.patch", "kvm-dax-source.json")
    (output / "measured-sources").mkdir()
    for name in sources:
        shutil.copy2(HERE / name, output / "measured-sources" / name)
    suite = {"schema": "guest-pool-handoff-suite-v1", "result": "running",
             "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items()},
             "fixture_manifest": manifest, "fixture_manifest_sha256": lab.sha256(assets / "manifest.json"),
             "qemu_sha256": lab.sha256(qemu), "kvm_memory_smoke_sha256": lab.sha256(smoke),
             "source_hashes": {name: lab.sha256(output / "measured-sources" / name) for name in sources},
             "arms": []}

    def save():
        lab.save_json(output / "report.json", suite)

    def command(argv, env, log):
        with log.open("w") as stream:
            subprocess.run(argv, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)

    save()
    try:
        for index, mode in enumerate(a.modes):
            session = f"{a.session}-{index}"
            pointer = HERE / ".local" / f"current-{session}.json"
            if pointer.exists():
                raise RuntimeError("Session already exists; use a fresh name: " + session)
            arm = {"mode": mode, "session": session, "result": "running"}
            suite["arms"].append(arm)
            save()
            env = dict(os.environ, CXL_LAB_ASSETS=str(assets), CXL_LAB_SESSION=session,
                       CXL_LAB_QEMU_ROOT=str(a.qemu_root.resolve(strict=True)))
            prefix = ["taskset", "-c", a.cpus, sys.executable, "-u"]
            # lab.start creates a per-run copy of the home storage template.
            # Each mode has a fresh pair, CXL backing and allocator lifetime.
            start = prefix + [str(HERE / "lab.py"), "start", "--vcpus", "4", "--memory-mib", "8192",
                "--shared-mib", "64", "--cxl-mib", "4096", "--dax-guest", "--dax-4k",
                "--migration-network", "--network-backend", "tap", "--memory-backing-dir", "/dev/shm",
                "--home-storage-image", str(assets / "storage.raw"), "--qemu", str(qemu)]
            if mode == "direct":
                start.append("--dax-handoff")
            bench = prefix + [str(HERE / "pool_handoff_bench.py"), "--mode", mode,
                "--sandboxes", "3", "--cycles", str(a.cycles), "--kvm-memory-smoke", str(smoke)]
            arm["commands"] = {"start": start, "bench": bench}
            try:
                command(start, env, output / (mode + "-start.log"))
                command(bench, env, output / (mode + "-bench.log"))
            finally:
                if pointer.exists():
                    run = Path(json.loads(pointer.read_text())["run"])
                    arm["original_run"] = str(run)
                    try:
                        command([sys.executable, str(HERE / "lab.py"), "stop"], env,
                                output / (mode + "-stop.log"))
                    finally:
                        config = json.loads((run / "run.json").read_text())
                        arm["outer_cleanup"] = {role: not lab.alive(vm) for role, vm in config["vms"].items()}
                        arm["memory_backing_removed"] = not Path(config["ephemeral_memory_dir"]).exists()
                        shutil.copy2(run / "run.json", output / (mode + "-config.json"))
                        reports = list(run.glob("pool-handoff-*/report.json"))
                        if len(reports) == 1:
                            shutil.copytree(reports[0].parent, output / mode)
                            arm.update(report_file=mode + "/report.json", report_sha256=lab.sha256(reports[0]))
                        save()
            if not all(arm["outer_cleanup"].values()) or not arm["memory_backing_removed"]:
                raise RuntimeError("Lab resources remain for " + mode)
            if "report_file" not in arm:
                raise RuntimeError("Expected exactly one handoff report for " + mode)
            command([sys.executable, str(HERE / "verify_pool_handoff.py"), str(output / arm["report_file"]),
                     "--output", str(output / (mode + "-verification.json"))], env,
                    output / (mode + "-verification.log"))
            arm["result"] = "passed"
            save()
            print("POOL_HANDOFF_ARM_DONE " + mode, flush=True)
        suite["result"] = "passed"
    except BaseException as exc:
        suite.update(result="failed", error=repr(exc))
        raise
    finally:
        save()
        print("POOL_HANDOFF_SUITE " + str(output / "report.json"), flush=True)


if __name__ == "__main__":
    main()
