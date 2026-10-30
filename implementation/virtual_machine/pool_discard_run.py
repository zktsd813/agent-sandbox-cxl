#!/usr/bin/env python3
"""Run baseline/reporting arms in fresh, disposable lab pairs; preserve raw evidence."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

import lab

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--qemu", type=Path, required=True)
    p.add_argument("--qemu-root", type=Path, required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--anon-mib", type=int, default=400)
    p.add_argument("--modes", nargs="+", choices=("baseline", "reporting"), default=["baseline", "reporting"])
    p.add_argument("--lifecycle", action="store_true")
    p.add_argument("--profile", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    assets = a.assets.resolve()
    sources = ("pool_discard_run.py", "pool_discard_bench.py", "pool_discard_prepare.py", "pool_discard_probe.c",
               "pool-discard-l2-init.sh", "pool-discard-guest-trace.patch", "firecracker.patch", "pool_cow_latency.c", "pool_cow_trace.py")
    suite = {"schema": "pool-discard-suite-v1", "result": "running", "rounds": a.rounds, "anon_mib": a.anon_mib, "arms": [],
             "fixture_manifest": json.loads((assets / "manifest.json").read_text()),
             "source_hashes": {name: lab.sha256(HERE / name) for name in sources}}
    (a.output / "measured-sources").mkdir()
    for name in sources:
        shutil.copy2(HERE / name, a.output / "measured-sources" / name)
    def save():
        lab.save_json(a.output / "report.json", suite)
    save()
    try:
        for index, mode in enumerate(a.modes):
            session = f"{a.session}-{index}"
            disk = a.output / (mode + "-storage.raw")
            subprocess.run(["cp", "--sparse=always", "--reflink=auto", str(assets / "storage.raw"), str(disk)], check=True)
            env = dict(os.environ, CXL_LAB_ASSETS=str(assets), CXL_LAB_SESSION=session,
                       CXL_LAB_QEMU_ROOT=str(a.qemu_root.resolve()))
            prefix = ["taskset", "-c", "0-7", "python3", "-u"]
            subprocess.run(prefix + [str(HERE / "lab.py"), "start", "--vcpus", "4", "--memory-mib", "8192",
                "--shared-mib", "64", "--cxl-mib", "4096", "--dax-guest", "--dax-4k", "--pool-cow",
                "--migration-network", "--network-backend", "tap", "--memory-backing-dir", "/dev/shm",
                "--home-storage-image", str(disk.resolve()), "--qemu", str(a.qemu.resolve())], env=env, check=True)
            run = Path(json.loads((HERE / ".local" / f"current-{session}.json").read_text())["run"])
            try:
                command = prefix + [str(HERE / "pool_discard_bench.py"), "--mode", mode, "--rounds", str(a.rounds),
                                    "--anon-mib", str(a.anon_mib)]
                if a.lifecycle and mode == "reporting":
                    command.append("--lifecycle")
                if a.profile and mode == "baseline":
                    command.append("--profile")
                subprocess.run(command, env=env, check=True)
            finally:
                subprocess.run(["python3", str(HERE / "lab.py"), "stop"], env=env, check=True)
                reports = list(run.glob("discard-*/report.json"))
                if reports:
                    assert len(reports) == 1
                    shutil.copytree(reports[0].parent, a.output / mode)
                    suite["arms"].append({"mode": mode, "report_file": mode + "/report.json",
                                          "sha256": lab.sha256(reports[0]), "original_report": str(reports[0])})
                    save()
            print("DISCARD_ARM_DONE " + mode, flush=True)
        suite["result"] = "passed"
    except BaseException as exc:
        suite["result"] = "failed"
        suite["error"] = repr(exc)
        raise
    finally:
        save()
        print("POOL_DISCARD_SUITE " + str(a.output / "report.json"), flush=True)


if __name__ == "__main__":
    main()
