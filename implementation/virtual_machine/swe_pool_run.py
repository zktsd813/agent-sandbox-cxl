#!/usr/bin/env python3
"""Replay a few recorded SWE traces in fresh DRAM/state-only/direct lab pairs.

Each trial gets a new pair, private home-storage copy and CXL backing. The
recorded guest commands are not rewritten here. This is a bounded workload
experiment, not a matched stock-Firecracker performance benchmark.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import lab

HERE = Path(__file__).resolve().parent
NAMES = ("gin-gonic__gin-3741", "php-cs-fixer__php-cs-fixer-8331",
         "micropython__micropython-13039")
MODES = ("baseline", "state-only", "direct")
SOURCES = ("swe_pool_run.py", "swe_pool_bench.py", "swe_pool_prepare.py",
           "swe_pool_protocol.py", "verify_swe_pool.py", "swe-pool-l2-init.sh",
           "swe-pool-guest-run.sh", "guest-pool-cli.c", "guest-pool-uapi.h",
           "guest-pool.c", "guest_pool_protocol.py", "pool_handoff_bench.py",
           "pool_handoff_control.py", "pool_memory_module.c", "pool_memory_uapi.h",
           "pool_bench.py", "pool_control.py", "home_storage_bench.py", "nfs_bench.py",
           "pool_compare_bench.py", "pool_discard_bench.py", "trace_bench.py",
           "trace_prepare.py", "trace-guest-run.sh", "verify_trace_results.py",
           "verify_pool_handoff.py", "verify_pool_discard_protocol.py", "lab.py",
           "shared_probe.c",
           "firecracker.patch", "kvm-dax-state.patch", "kvm-dax-source.json")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--qemu-root", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cpus", default="0-7")
    parser.add_argument("--names", choices=NAMES, nargs="+", default=list(NAMES))
    parser.add_argument("--modes", choices=MODES, nargs="+", default=list(MODES))
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--keep-storage", action="store_true",
                        help="Retain each recorded private outer disk after its pair stops")
    parser.add_argument("--defer-verification", action="store_true",
                        help="Preserve execution-only results; never label the suite passed")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{1,29}", args.session):
        parser.error("Use a fresh lowercase session name of at most 29 characters")
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", args.cpus):
        parser.error("Use a taskset CPU list such as 0-7")
    if args.memory_mib != 2048:
        parser.error("This workload fixture uses 2048MiB and one vCPU per sandbox")
    if len(set(args.names)) != len(args.names) or len(set(args.modes)) != len(args.modes):
        parser.error("Do not repeat trace names or modes")
    return args


def record_file(path):
    return {"bytes": path.stat().st_size, "sha256": lab.sha256(path)}


def main():
    args = arguments()
    assets = args.assets.resolve(strict=True)
    qemu = args.qemu.resolve(strict=True)
    qemu_root = args.qemu_root.resolve(strict=True)
    manifest_path = assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in args.names:
        if name not in manifest["traces"]:
            raise RuntimeError("Trace missing from fixture: " + name)
    for name, digest in manifest["artifacts"].items():
        if Path(name).name != name or lab.sha256(assets / name) != digest:
            raise RuntimeError("Fixture hash mismatch: " + name)
    for name in SOURCES:
        if not (HERE / name).is_file():
            raise FileNotFoundError(HERE / name)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "measured-sources").mkdir()
    for name in SOURCES:
        shutil.copy2(HERE / name, output / "measured-sources" / name)
    source_hashes = {name: lab.sha256(output / "measured-sources" / name) for name in SOURCES}
    suite = {"schema": "swe-pool-suite-v1", "result": "running",
             "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
             "fixture_manifest": manifest, "fixture_manifest_sha256": lab.sha256(manifest_path),
             "source_hashes": source_hashes, "qemu_sha256": lab.sha256(qemu), "trials": [],
             "scope": "One fresh pair per actual trace/mode; same one-vCPU 2GiB sandbox configuration",
             "limitations": ["Coherent nested KVM on one physical host",
                             "One trial per trace/mode unless separately repeated",
                             "Baseline uses the same patched VMM in DRAM, not pristine Firecracker",
                             "Transitions are between recorded commands"]}

    def save():
        lab.save_json(output / "report.json", suite)

    def require_frozen_sources():
        if any(lab.sha256(HERE / path) != digest for path, digest in source_hashes.items()):
            raise RuntimeError("Execution/verification source changed during the suite; use a new output")

    def command(argv, env, path):
        started = time.monotonic_ns()
        with path.open("w") as stream:
            result = subprocess.run(argv, env=env, stdout=stream, stderr=subprocess.STDOUT)
        return {"argv": argv, "returncode": result.returncode,
                "controller_wall_ns": time.monotonic_ns() - started,
                "log": path.name, **record_file(path)}

    def cleanup(run, config, arm):
        arm["outer_cleanup"] = {role: {"pid": vm["pid"], "starttime": vm["starttime"],
            "fenced": not lab.alive(vm)} for role, vm in config["vms"].items()}
        fenced = all(row["fenced"] for row in arm["outer_cleanup"].values())
        directory = config.get("ephemeral_memory_dir")
        arm["memory_backing_removed"] = bool(directory and not Path(directory).exists())
        objects = (config.get("migration_network") or {}).get("host_network", {}).get("objects", {})
        arm["network_removed"] = {key: not (Path("/sys/class/net") / value["name"]).exists()
                                  for key, value in objects.items()}
        if not fenced or not arm["memory_backing_removed"] or not all(arm["network_removed"].values()):
            raise RuntimeError("Recorded pair resources remain; preserving its private storage")
        storage = Path(config["home_storage_image"]["path"])
        if storage != run / "home-storage.raw" or storage.is_symlink() or not storage.is_file():
            raise RuntimeError("Unexpected owned storage path; refusing cleanup")
        arm["owned_storage"] = {"path": str(storage), "initial_sha256":
            config["home_storage_image"]["initial_sha256"], "retained": True,
            "bytes": storage.stat().st_size}

    def release_storage(arm):
        # A failed benchmark or verifier never reaches this function. Preserve
        # its exact private disk for diagnosis; remove only successful trials.
        if args.keep_storage or args.defer_verification:
            return
        storage = Path(arm["owned_storage"]["path"])
        run = Path(arm["original_run"])
        if storage != run / "home-storage.raw" or storage.is_symlink() or not storage.is_file():
            raise RuntimeError("Owned storage changed after fencing; refusing cleanup")
        if not all(row["fenced"] for row in arm["outer_cleanup"].values()):
            raise RuntimeError("Storage owner is not fenced")
        storage.unlink()
        arm["owned_storage"].update(retained=False, removed_after_qemu_fence=not storage.exists())

    save()
    try:
        for index, (name, mode) in enumerate((name, mode) for name in args.names for mode in args.modes):
            require_frozen_sources()
            session = f"{args.session}-{index:02d}"
            pointer = HERE / ".local" / f"current-{session}.json"
            if pointer.exists():
                raise RuntimeError("Session already exists; use a fresh name: " + session)
            trial_id = f"{name}-{mode}"
            arm = {"id": trial_id, "trace": name, "mode": mode, "session": session, "result": "running"}
            suite["trials"].append(arm)
            env = dict(os.environ, CXL_LAB_ASSETS=str(assets), CXL_LAB_SESSION=session,
                       CXL_LAB_QEMU_ROOT=str(qemu_root))
            arm["environment"] = {key: env[key] for key in
                ("CXL_LAB_ASSETS", "CXL_LAB_SESSION", "CXL_LAB_QEMU_ROOT")}
            prefix = ["taskset", "-c", args.cpus, sys.executable, "-u"]
            start = prefix + [str(HERE / "lab.py"), "start", "--vcpus", "4", "--memory-mib", "8192",
                "--shared-mib", "64", "--cxl-mib", "16384", "--dax-guest", "--dax-4k",
                "--migration-network", "--network-backend", "tap", "--memory-backing-dir", "/dev/shm",
                "--home-storage-image", str(assets / "storage.raw"), "--qemu", str(qemu)]
            if mode == "direct":
                start.append("--dax-handoff")
            bench = prefix + [str(HERE / "swe_pool_bench.py"), "--mode", mode, "--name", name,
                              "--memory-mib", str(args.memory_mib)]
            save()
            failure = None
            try:
                arm["start"] = command(start, env, output / (trial_id + "-start.log"))
                if arm["start"]["returncode"]:
                    raise RuntimeError("Lab start failed: " + trial_id)
                arm["bench"] = command(bench, env, output / (trial_id + "-bench.log"))
                if arm["bench"]["returncode"]:
                    raise RuntimeError("Workload trial failed: " + trial_id)
            except BaseException as exc:
                failure = exc
                arm.update(result="failed", error=repr(exc))
            finally:
                if pointer.exists():
                    run = Path(json.loads(pointer.read_text())["run"])
                    arm["original_run"] = str(run)
                    arm["stop"] = command([sys.executable, str(HERE / "lab.py"), "stop"], env,
                                          output / (trial_id + "-stop.log"))
                    config = json.loads((run / "run.json").read_text())
                    shutil.copy2(run / "run.json", output / (trial_id + "-config.json"))
                    arm["config"] = {"file": trial_id + "-config.json", **record_file(run / "run.json")}
                    for source in sorted(run.glob("vm-*.log")):
                        shutil.copy2(source, output / (trial_id + "-" + source.name))
                    reports = list(run.glob("swe-pool-*/report.json"))
                    if len(reports) == 1:
                        shutil.copytree(reports[0].parent, output / trial_id)
                        arm.update(report_file=trial_id + "/report.json", report_sha256=lab.sha256(reports[0]))
                    else:
                        arm["report_candidates"] = [str(path) for path in reports]
                    try:
                        cleanup(run, config, arm)
                        if arm["stop"]["returncode"]:
                            raise RuntimeError("Lab stop returned failure: " + trial_id)
                    except BaseException as exc:
                        arm["cleanup_error"] = repr(exc)
                        failure = failure or exc
                else:
                    arm["cleanup_error"] = "No recorded pair pointer; lab start failed before publication"
                save()
            if failure:
                raise failure
            require_frozen_sources()
            if "report_file" not in arm:
                raise RuntimeError("Expected exactly one SWE pooling report for " + trial_id)
            if args.defer_verification:
                arm["result"] = "execution-passed-unverified"
            else:
                verification = output / (trial_id + "-verification.json")
                arm["verification"] = command([sys.executable, str(HERE / "verify_swe_pool.py"),
                    str(output / arm["report_file"]), "--output", str(verification)], env,
                    output / (trial_id + "-verification.log"))
                if arm["verification"]["returncode"]:
                    raise RuntimeError("Independent workload verification failed: " + trial_id)
                require_frozen_sources()
                arm["verification_result"] = {"file": verification.name, **record_file(verification)}
                arm["result"] = "passed"
            release_storage(arm)
            save()
            print("SWE_POOL_TRIAL_DONE " + trial_id, flush=True)
        suite["result"] = "execution-passed-unverified" if args.defer_verification else "passed"
        save()
        if not args.defer_verification:
            require_frozen_sources()
            verification = output / "suite-verification.json"
            suite["verification"] = command([sys.executable, str(HERE / "verify_swe_pool.py"),
                "--suite", str(output / "report.json"), "--output", str(verification)], os.environ,
                output / "suite-verification.log")
            if suite["verification"]["returncode"]:
                raise RuntimeError("Independent suite comparison failed")
            require_frozen_sources()
            suite["verification_result"] = {"file": verification.name, **record_file(verification)}
    except BaseException as exc:
        suite.update(result="failed", error=repr(exc))
        if suite["trials"] and suite["trials"][-1]["result"] == "running":
            suite["trials"][-1].update(result="failed", error=repr(exc))
        raise
    finally:
        save()
        print("SWE_POOL_SUITE " + str(output / "report.json"), flush=True)


if __name__ == "__main__":
    main()
