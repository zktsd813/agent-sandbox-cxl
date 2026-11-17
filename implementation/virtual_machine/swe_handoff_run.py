#!/usr/bin/env python3
"""Replay a few recorded SWE traces in fresh DRAM/state-only/direct lab pairs.

Each trial gets a new pair, private home-storage copy and CXL backing. The
recorded guest commands are not rewritten here. This is a bounded workload
experiment, not a matched stock-Firecracker performance benchmark.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import random
import shutil
import subprocess
import sys
import time

import lab
from experiment_sources import record_sources

HERE = Path(__file__).resolve().parent
NAMES = ("gin-gonic__gin-3741", "php-cs-fixer__php-cs-fixer-8331",
         "micropython__micropython-13039")
MODES = ("baseline", "state-only", "direct")
SOURCES = ("experiment_sources.py", "swe_handoff_run.py", "handoff_bench.py", "swe_handoff_prepare.py",
    "verify_swe_handoff.py", "swe-handoff-l2-init.sh", "swe-handoff-guest-run.sh", "pool_memory_module.c",
    "pool_memory_uapi.h", "nfs_bench.py", "trace_prepare.py",
    "lab.py", "shared_probe.c", "resident_fast.py",
    "resident_control.c", "resident_bootstrap.c", "resident_prepare.py", "control_rpc.c", "control_rpc.py",
    "guest-init.sh", "resident_wire.h", "resident_proxy.py", "kernel_handoff_trace.py",
    "firecracker.patch", "kvm-dax-state.patch", "kvm-dax-source.json", "kvm-prefault.patch", "firecracker-prefault.patch", "prefault_probe.c",
    "multi_vcpu.py", "dirty_oracle.py", "page_hash.c", "io_mix.c")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--network-backend", choices=("socket", "tap"), default="socket")
    parser.add_argument("--qemu-root", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cpus", default="0-7")
    parser.add_argument("--names", choices=NAMES, nargs="+", default=list(NAMES))
    parser.add_argument("--modes", choices=MODES, nargs="+", default=list(MODES))
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--vcpus", type=int, choices=(1,2,4,8), default=1)
    # Default: 6144/8192 for trajectory runs, 8192/16384 otherwise.
    parser.add_argument("--l1-memory-mib", type=int, choices=(4096,6144,8192))
    parser.add_argument("--cxl-mib", type=int, choices=(4096,8192,16384))
    parser.add_argument("--parallel-build", action="store_true")
    parser.add_argument("--dirty-ledger", action="store_true")
    parser.add_argument("--dirty-oracle", action="store_true")
    parser.add_argument("--page-hash", type=Path)
    parser.add_argument("--pml", choices=("on", "off"), default="off")
    parser.add_argument("--restore-timing", action="store_true")
    parser.add_argument("--kernel-trace", action="store_true")
    parser.add_argument("--inflight-handoff-index", type=int)
    parser.add_argument("--direct-profile-roundtrips", type=int, default=0)
    parser.add_argument("--keep-storage", action="store_true",
                        help="Retain each recorded private outer disk after its pair stops")
    parser.add_argument("--defer-verification", action="store_true",
                        help="Preserve execution-only results; never label the suite passed")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--trajectory-policies', nargs='+', choices=('heavy', 'all'))
    parser.add_argument('--heavy-plan', type=Path)
    parser.add_argument('--count-faults', action='store_true')
    parser.add_argument('--prefault', choices=('all','mapped'))
    parser.add_argument('--prefault-variants', nargs='+', choices=('all','mapped'))
    parser.add_argument('--prefault-activate', action='store_true')
    parser.add_argument('--prefault-probe', type=Path)
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261116)
    parser.add_argument('--skip-trials', type=int, default=0, help='Resume a recorded shuffled plan in a fresh output/session; preserve the completed prefix separately')
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{1,29}", args.session):
        parser.error("Use a fresh lowercase session name of at most 29 characters")
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", args.cpus):
        parser.error("Use a taskset CPU list such as 0-7")
    if args.memory_mib != 2048:
        parser.error("This workload fixture uses 2048MiB per sandbox")
    if not 0 <= args.direct_profile_roundtrips <= 20 or (args.direct_profile_roundtrips and args.modes != ['direct']):
        parser.error("Profile repetitions require only direct mode and 0..20 roundtrips")
    if args.inflight_handoff_index is not None and ("direct" not in args.modes or len(args.names) != 1):
        parser.error("In-flight index requires one explicit trace including direct mode")
    if args.kernel_trace and args.modes != ['direct']:
        parser.error("Kernel tracing requires only direct mode")
    if len(set(args.names)) != len(args.names) or len(set(args.modes)) != len(args.modes):
        parser.error("Do not repeat trace names or modes")
    if args.trajectory_policies and (not args.heavy_plan or args.repetitions < 1 or args.kernel_trace):
        parser.error('Trajectory requires a heavy plan, positive repetitions and no per-hop kernel-trace option')
    if args.prefault and (args.modes != ['state-only'] or not args.trajectory_policies):
        parser.error('Prefault requires only state-only trajectory mode')
    if args.prefault_variants and (args.prefault or not args.trajectory_policies or len(set(args.prefault_variants)) != len(args.prefault_variants)):
        parser.error('Prefault variants require trajectory, unique variants, and no single prefault option')
    if args.prefault_activate and not (args.prefault or args.prefault_variants):
        parser.error('Activation requires prefault')
    if args.dirty_oracle and (not args.dirty_ledger or args.modes != ['direct'] or not args.page_hash):
        parser.error('Dirty oracle requires direct-only ledger and page-hash executable')
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
    for name in SOURCES:
        if not (HERE / name).is_file():
            raise FileNotFoundError(HERE / name)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_hashes, source_reference = record_sources(HERE, SOURCES)
    suite = {"schema": "swe-handoff-suite-v1", "result": "running",
             'controller_pid': os.getpid(),
             "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
             "fixture_manifest": manifest, "fixture_manifest_sha256": lab.sha256(manifest_path),
             "source_hashes": source_hashes, "source_reference": source_reference, "qemu_sha256": lab.sha256(qemu), "trials": [],
             "scope": "One fresh pair per actual trace/mode; same configured vCPU count and 2GiB sandbox configuration",
             "limitations": ["Coherent nested KVM on one physical host",
                             "One trial per trace/mode unless separately repeated",
                             "Baseline uses the same patched VMM in DRAM, not pristine Firecracker",
                             ("Includes two handoffs during a running command" if args.inflight_handoff_index is not None else "Transitions are between recorded commands")]}

    def save():
        lab.save_json(output / "report.json", suite)

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
        with storage.open('rb') as stream:
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        arm["owned_storage"] = {"path": str(storage), "initial_sha256":
            config["home_storage_image"].get("initial_sha256"), "retained": True,
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
        methods = [(mode,args.prefault) for mode in args.modes] + [('state-only',p) for p in (args.prefault_variants or [])]
        tasks = [(name, mode, policy, repeat, prefault) for name in args.names for mode,prefault in methods
                 for policy in (args.trajectory_policies or [None]) for repeat in range(args.repetitions)]
        if args.trajectory_policies:
            random.Random(args.seed).shuffle(tasks)
        if not 0 <= args.skip_trials < len(tasks):
            raise ValueError('skip-trials must select a nonempty suffix of the recorded plan')
        suite['planned_order'] = tasks
        suite['skipped_planned_order'] = tasks[:args.skip_trials]
        for index, (name, mode, policy, repeat, prefault) in enumerate(tasks[args.skip_trials:], start=args.skip_trials):
            session = f"{args.session}-{index:02d}"
            pointer = HERE / ".local" / f"current-{session}.json"
            if pointer.exists():
                raise RuntimeError("Session already exists; use a fresh name: " + session)
            method = "prefault-"+prefault if prefault else mode
            trial_id = f"{name}-{method}" + (f'-{policy}-r{repeat}' if policy else '')
            arm = {"id": trial_id, "trace": name, "mode": mode, "policy": policy, 'repeat': repeat,
                   "session": session, "result": "running", "method":method}
            suite["trials"].append(arm)
            env = dict(os.environ, CXL_LAB_ASSETS=str(assets), CXL_LAB_SESSION=session,
                       CXL_LAB_QEMU_ROOT=str(qemu_root))
            arm["environment"] = {key: env[key] for key in
                ("CXL_LAB_ASSETS", "CXL_LAB_SESSION", "CXL_LAB_QEMU_ROOT")}
            prefix = ['numactl', '--cpunodebind=0', '--membind=0', "taskset", "-c", args.cpus, sys.executable, "-u"]
            l1_mib = args.l1_memory_mib or (6144 if policy else 8192)
            cxl_mib = args.cxl_mib or (8192 if policy else 16384)
            start = prefix + [str(HERE / "lab.py"), "start", "--vcpus", str(max(4, args.vcpus+2)), "--memory-mib", str(l1_mib),
                "--shared-mib", "64", "--cxl-mib", str(cxl_mib), "--dax-guest", "--dax-4k",
                "--migration-network", "--network-backend", args.network_backend, "--memory-backing-dir", "/dev/shm",
                "--direct-cpu-model",
                "--home-storage-image", str(assets / "storage.raw"), "--qemu", str(qemu)]
            start.extend(["--pml", args.pml])
            if mode == "direct":
                start.append("--dax-handoff")
            bench = prefix + [str(HERE / "handoff_bench.py"), "--mode", mode, "--name", name,
                              "--memory-mib", str(args.memory_mib), "--vcpus", str(args.vcpus),
                              "--direct-profile-roundtrips", str(args.direct_profile_roundtrips)]
            if args.dirty_ledger:
                bench.append('--dirty-ledger')
            if args.dirty_oracle:
                bench.extend(['--dirty-oracle', '--page-hash', str(args.page_hash.resolve())])
            if policy:
                bench.extend(['--trajectory-policy', policy, '--heavy-plan', str(args.heavy_plan.resolve())])
                if args.count_faults: bench.append('--count-faults')
            if args.prefault_probe:
                bench.extend(['--prefault-probe',str(args.prefault_probe.resolve())])
            if prefault:
                bench.extend(['--prefault',prefault])
                if args.prefault_activate: bench.append('--prefault-activate')
            if args.parallel_build:
                bench.append("--parallel-build")
            if mode == "direct" and not policy:
                bench.append("--resident-repromote-handoff")
            if args.restore_timing:
                bench.append("--restore-timing")
            if args.kernel_trace:
                bench.append("--kernel-trace")
            if args.inflight_handoff_index is not None and mode == "direct":
                bench.extend(["--inflight-handoff-index", str(args.inflight_handoff_index)])
            save()
            failure = None
            lab_lock = (HERE.parents[1]/'.codex-runs/lab.lock').open('a')
            fcntl.flock(lab_lock, fcntl.LOCK_EX)
            try:
                # Hashing the sparse 13GiB template can occupy node-0 cache.
                # Release only clean pages of this read-only input, never use
                # the host's global drop_caches control.
                with (assets/'storage.raw').open('rb') as stream:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                hardware = subprocess.check_output(['numactl', '--hardware'], text=True)
                arm['numa_hardware'] = hardware
                free = int(re.search(r'node 0 free: (\d+) MB', hardware)[1])
                arm['budget_mib'] = 2*l1_mib+cxl_mib+64
                if free < arm['budget_mib']+4096:
                    raise RuntimeError(f'Node 0 free {free} MiB < lab budget + 4GiB')
                arm["start"] = command(start, env, output / (trial_id + "-start.log"))
                if arm["start"]["returncode"]:
                    raise RuntimeError("Lab start failed: " + trial_id)
                with (HERE.parents[1]/'.codex-runs/quiet.lock').open('a') as quiet:
                    fcntl.flock(quiet, fcntl.LOCK_EX)
                    for phase in ('before',):
                        (output/(trial_id+'-load-'+phase+'.txt')).write_text(
                            Path('/proc/loadavg').read_text()+subprocess.check_output(
                                ['ps','-eo','pid,user,psr,pcpu,comm'], text=True))
                    try:
                        arm["bench"] = command(bench, env, output / (trial_id + "-bench.log"))
                    finally:
                        (output/(trial_id+'-load-after.txt')).write_text(
                            Path('/proc/loadavg').read_text()+subprocess.check_output(
                                ['ps','-eo','pid,user,psr,pcpu,comm'], text=True))
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
                    reports = list(run.glob("swe-handoff-*/report.json"))
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
                lab_lock.close()
                save()
            if failure:
                raise failure
            if "report_file" not in arm:
                raise RuntimeError("Expected exactly one SWE handoff report for " + trial_id)
            if args.defer_verification:
                arm["result"] = "execution-passed-unverified"
            else:
                verification = output / (trial_id + "-verification.json")
                arm["verification"] = command([sys.executable, str(HERE / "verify_swe_handoff.py"),
                    str(output / arm["report_file"]), "--output", str(verification)], env,
                    output / (trial_id + "-verification.log"))
                if arm["verification"]["returncode"]:
                    raise RuntimeError("Independent workload verification failed: " + trial_id)
                arm["verification_result"] = {"file": verification.name, **record_file(verification)}
                arm["result"] = "passed"
            release_storage(arm)
            save()
            print("SWE_HANDOFF_TRIAL_DONE " + trial_id, flush=True)
        suite["result"] = "execution-passed-unverified" if args.defer_verification else "passed"
        save()
        if not args.defer_verification:
            verification = output / "suite-verification.json"
            suite["verification"] = command([sys.executable, str(HERE / "verify_swe_handoff.py"),
                "--suite", str(output / "report.json"), "--output", str(verification)], os.environ,
                output / "suite-verification.log")
            if suite["verification"]["returncode"]:
                raise RuntimeError("Independent suite comparison failed")
            suite["verification_result"] = {"file": verification.name, **record_file(verification)}
    except BaseException as exc:
        suite.update(result="failed", error=repr(exc))
        if suite["trials"] and suite["trials"][-1]["result"] == "running":
            suite["trials"][-1].update(result="failed", error=repr(exc))
        raise
    finally:
        save()
        print("SWE_HANDOFF_SUITE " + str(output / "report.json"), flush=True)


if __name__ == "__main__":
    main()
