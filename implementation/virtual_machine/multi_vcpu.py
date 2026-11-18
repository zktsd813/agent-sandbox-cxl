#!/usr/bin/env python3
"""Run one isolated, locked SMP handoff trial and retain its evidence."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
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
    p.add_argument("--iterations", type=int, default=400)
    p.add_argument("--warmup", type=int, default=10)
    p.add_argument("--measure", action="store_true", help="Hold quiet.lock exclusively")
    p.add_argument("--resident-bench", choices=("idle","busy","dirty","race"))
    p.add_argument("--mixed-io", action="store_true")
    p.add_argument("--io-fixture", type=Path)
    p.add_argument("--cxl-mib", type=int, choices=(2048,4096,8192))
    p.add_argument("--memory-mib", type=int, choices=(128,2048))
    p.add_argument("--dirty-lines",type=int,choices=(0,1,8,64),default=0)
    p.add_argument("--dirty-mode",choices=("change","same","read"),default="change")
    p.add_argument("--dirty-mib", type=int, choices=(0,16,64,256), default=0)
    p.add_argument("--assets", type=Path)
    p.add_argument("--dirty-ledger", action="store_true")
    p.add_argument("--noncoherent", action="store_true")
    p.add_argument("--reuse-test", action="store_true")
    p.add_argument("--legacy-observer", action="store_true")
    p.add_argument("--audit-parked", action="store_true")
    p.add_argument("--arena-limit-bytes", type=int,default=0)
    p.add_argument("--fail-maintenance", choices=("writeback","epoch","record"))
    p.add_argument("--manual-protect",action="store_true")
    p.add_argument("--clean-helper",action="store_true")
    p.add_argument("--guest-io-probe",action="store_true")
    p.add_argument("--service-probe",action="store_true")
    p.add_argument("--preclean", action="store_true")
    p.add_argument("--clean-paused", action="store_true", help="Test only: pause and hash each clean")
    p.add_argument("--clean-wait", choices=("0","1","recorded"), default="0")
    p.add_argument("--clean-repeats", type=int, default=1)
    p.add_argument("--cache-skip-line",type=int,default=-1)
    p.add_argument("--skip-invalidate-page",type=int,default=-1)
    p.add_argument("--drop-consumed-once", action="store_true")
    p.add_argument("--dirty-oracle", action="store_true")
    p.add_argument("--page-hash", type=Path)
    p.add_argument("--pml", choices=("on", "off"), default="off")
    p.add_argument("--tail-profile", action="store_true")
    p.add_argument("--profile", action="store_true")
    p.add_argument("--resume-profile", action="store_true")
    p.add_argument("--kernel-mode", type=int, choices=(2,3), default=3)
    p.add_argument("--test-running-vcpu", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--firecracker", type=Path, default=LOCAL / "fc/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker")
    p.add_argument("--qemu", type=Path, default=LOCAL / "qemu-runtime/bin/qemu-system-x86_64")
    args = p.parse_args()
    if args.manual_protect and not args.noncoherent:p.error("Manual protect requires noncoherent mode")
    if args.noncoherent: args.dirty_ledger=True
    fixture_trial = bool(args.resident_bench or args.mixed_io)
    memory_mib = args.memory_mib or (2048 if args.mixed_io or args.resident_bench=="dirty" else 128)
    cxl_mib = args.cxl_mib or (4096 if memory_mib==2048 else 2048)
    prefix = "io-mix-*" if args.mixed_io else ("resident-bench-*" if args.resident_bench else "direct-linux-handoff-*")
    if fixture_trial and (not args.assets or not args.measure):
        p.error("resident benchmark needs --assets and exclusive --measure")
    args.output = args.output.resolve()
    if not args.output.is_relative_to(HERE.parents[1]):
        p.error("output must be inside this worktree")
    args.output.mkdir(parents=True, exist_ok=False)
    session = "mvcpu-" + datetime.datetime.now(datetime.timezone.utc).strftime("%m%d%H%M%S")
    assets = args.assets.resolve() if args.assets else LOCAL / ("assets-" + session)
    current = LOCAL / ("current-" + session + ".json")
    env = dict(os.environ, CXL_LAB_SESSION=session, CXL_LAB_ASSETS=str(assets),
               CXL_LAB_KERNEL_SOURCE=str(LOCAL / "linux"),
               CXL_LAB_QEMU_ROOT=str(INPUTS / "qemu-cxl"))
    # The input wrapper has its own read-only library/firmware search paths.
    command = ["numactl", "--physcpubind=0-23", "--membind=0", sys.executable, str(HERE / "lab.py")]
    result = {"result": "running", "vcpus": args.vcpus, "iterations": args.iterations,
              "warmup": args.warmup, "kernel_mode": args.kernel_mode, "exclusive_quiet": args.measure,
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "memory_policy": "NUMA node 0; CPUs 0-23", "baseline_local_gib": 35,
              "session": session}

    def run(name, *arguments):
        print(name, flush=True)
        argv = [*command, *map(str, arguments)]
        if name == "resident":
            argv[argv.index(str(HERE / "lab.py"))] = str(HERE / "handoff_bench.py")
        if name == "prepare":
            argv = ["nice", "-n", "19", *argv]
        with (args.output / (name + ".log")).open("wb") as log:
            subprocess.run(argv, env=env, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=1800)

    print("Waiting for lab.lock and quiet.lock", flush=True)
    with (LOCKS / "lab.lock").open("rb") as lab_lock, (LOCKS / "quiet.lock").open("rb") as quiet_lock:
        fcntl.flock(lab_lock, fcntl.LOCK_EX)
        fcntl.flock(quiet_lock, fcntl.LOCK_EX if args.measure else fcntl.LOCK_SH)
        perf_handle = None
        try:
            if not fixture_trial:
                run("prepare", "prepare", "--firecracker", args.firecracker,
                    "--l1-kernel", LOCAL / "kernel-build/arch/x86/boot/bzImage",
                    "--module-dir", LOCAL / "kernel-modules/lib/modules/6.6.0-rc6-dcd-v3",
                    "--l2-kernel", INPUTS / "guest-swe-build/vmlinux")
            # 2 x 4 GiB L1 RAM + one shared 2 GiB CXL file + ivshmem + 4 GiB reserve.
            budget = (2 * 4096 + cxl_mib + (64 if fixture_trial else 4)) * 1024 * 1024
            if fixture_trial:
                with (assets/'storage.raw').open('rb') as stream:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            result['numa_hardware'] = subprocess.check_output(['numactl', '--hardware'], text=True)
            free = node0_free()
            result.update(node0_free_before_bytes=free, budget_bytes=budget,
                          reserve_bytes=4 * 1024**3)
            if free < budget + result["reserve_bytes"]:
                from tail_probe import release_project_cache
                result['project_cache_hint']=release_project_cache(LOCAL)
                result['numa_hardware_after_hint']=subprocess.check_output(['numactl','--hardware'],text=True)
                free=node0_free();result['node0_free_after_hint_bytes']=free
            if free < budget + result["reserve_bytes"]:
                raise RuntimeError("node 0 free memory below budget + 4 GiB")
            start = ["start", "--vcpus", max(4,args.vcpus+2), "--memory-mib",4096,
                "--shared-mib",64 if fixture_trial else 4,"--cxl-mib",cxl_mib,"--dax-handoff","--dax-guest",
                "--dax-4k","--network-backend","socket","--memory-backing-dir","/dev/shm","--qemu",args.qemu]
            start += ["--pml", args.pml]
            if fixture_trial:
                start += ["--migration-network","--home-storage-image",assets/"storage.raw"]
            run("start", *start)
            run("verify", "verify")
            config = json.loads((Path(json.loads(current.read_text())["run"]) / "run.json").read_text())
            resources = {}
            for role, vm in config["vms"].items():
                status = Path(f"/proc/{vm['pid']}/status").read_text()
                maps = Path(f"/proc/{vm['pid']}/numa_maps").read_text()
                (args.output / f"{role}-numa-maps.txt").write_text(maps)
                nodes = {}
                for line in maps.splitlines():
                    if not ((re.search(r"anon=[1-9]", line) and "file=" not in line) or "cxl-memory.bin" in line or "shared-memory.bin" in line):
                        continue
                    for node, pages in re.findall(r"\bN(\d+)=(\d+)\b", line):
                        nodes[node] = nodes.get(node, 0) + int(pages)
                resources[role] = {"owned_memory_pages_by_node": nodes,
                    "cpus_allowed": re.search(r"Cpus_allowed_list:\s*(.+)", status)[1]}
                if any(int(node) != 0 and pages for node, pages in nodes.items()):
                    raise RuntimeError("QEMU owned memory allocated outside node 0")
            result["resources"] = resources
            if args.tail_profile:
                from tail_probe import start_perf
                perf_handle = start_perf(config["vms"], args.output)
            bench_args = ["direct-handoff-bench", "--guest", "linux", "--vcpus", args.vcpus,
                          "--iterations", args.iterations, "--warmup", args.warmup, "--kernel-mode", args.kernel_mode,
                          "--executor-cpu", 1, "--guest-cpu", 0, "--control", "http",
                          "--poll-us", 50, "--dwell-ms", 20, "--reuse-fds"]
            if args.measure:
                bench_args += ["--smp-workload"]
            if args.profile:
                bench_args += ["--profile"]
            if args.resume_profile:
                bench_args += ["--resume-profile"]
            if args.test_running_vcpu:
                bench_args += ["--test-running-vcpu"]
            if fixture_trial:
                bench_args = ["--mode","direct","--name","gin-gonic__gin-3741","--memory-mib",memory_mib,
                    "--vcpus",args.vcpus,"--resident-bench",args.resident_bench,
                    "--iterations",args.iterations,"--warmup",args.warmup]
                if args.resident_bench=="dirty": bench_args += ["--dirty-mib",args.dirty_mib,"--dirty-lines",args.dirty_lines,"--dirty-mode",args.dirty_mode]
                if args.mixed_io:
                    if not args.io_fixture or not args.dirty_oracle: raise ValueError('Mixed I/O requires fixture and oracle')
                    bench_args = ['--mode','direct','--name','gin-gonic__gin-3741','--memory-mib',2048,
                        '--vcpus',args.vcpus,'--mixed-io','--io-fixture',args.io_fixture.resolve(),
                        '--iterations',args.iterations,'--warmup',0]
                if args.profile: bench_args += ["--profile","--restore-timing"]
                if args.resume_profile: bench_args += ["--resume-profile"]
                if args.noncoherent: bench_args += ['--noncoherent']
                if args.reuse_test: bench_args += ['--reuse-test']
                if args.legacy_observer: bench_args += ['--legacy-observer']
                if args.audit_parked: bench_args += ['--audit-parked']
                if args.arena_limit_bytes: bench_args += ['--arena-limit-bytes',args.arena_limit_bytes]
                if args.fail_maintenance: bench_args += ['--fail-maintenance',args.fail_maintenance]
                if args.tail_profile: bench_args += ["--tail-profile"]
                if args.manual_protect: bench_args += ["--manual-protect"]
                if args.clean_helper: bench_args += ["--clean-helper"]
                if args.guest_io_probe: bench_args += ["--guest-io-probe"]
                if args.service_probe: bench_args += ["--service-probe"]
                if args.preclean: bench_args += ['--preclean','--clean-wait',args.clean_wait,'--clean-repeats',args.clean_repeats]
                if args.clean_paused: bench_args += ['--clean-paused']
                if args.cache_skip_line>=0: bench_args += ["--cache-skip-line",args.cache_skip_line]
                if args.skip_invalidate_page>=0: bench_args += ["--skip-invalidate-page",args.skip_invalidate_page]
                if args.drop_consumed_once: bench_args += ['--drop-consumed-once']
                if args.dirty_ledger: bench_args += ['--dirty-ledger']
                if args.dirty_oracle:
                    if not args.dirty_ledger or not args.page_hash: raise ValueError('Oracle requires ledger and hash tool')
                    bench_args += ['--dirty-oracle', '--page-hash', args.page_hash.resolve()]
                run("resident",*bench_args)
            else:
                run("handoff", *bench_args)
            config = json.loads((Path(json.loads(current.read_text())["run"]) / "run.json").read_text())
            reports = sorted(Path(config["run"]).glob(prefix+"/report.json"))
            if len(reports) != 1:
                raise RuntimeError("Expected exactly one handoff report")
            shutil.copy2(reports[0], args.output / "handoff.json")
            if args.dirty_oracle:
                shutil.copytree(reports[0].parent/'dirty-oracle', args.output/'dirty-oracle')
            if json.loads(reports[0].read_text())["result"] != "passed":
                raise RuntimeError("Benchmark did not pass")
            result.update(result="passed", report=str(reports[0]), run=config["run"])
        except BaseException as error:
            result.update(result="failed", error=str(error))
            raise
        finally:
            if perf_handle:
                from tail_probe import stop_perf
                result["perf_returncode"] = stop_perf(perf_handle)
            if current.exists():
                config_path = Path(json.loads(current.read_text())["run"]) / "run.json"
                config = json.loads(config_path.read_text())
                result["run"] = config["run"]
                try:
                    run("stop", "stop")
                finally:
                    result["cleanup"] = {"qemu_pids": [v["pid"] for v in config["vms"].values()],
                        "backing_removed": not Path(config.get("ephemeral_memory_dir") or config["run"]).exists()}
                    for report in Path(config["run"]).glob(prefix+"/report.json"):
                        shutil.copy2(report, args.output / "handoff.json")
                        if args.reuse_test and (report.parent/'reuse-vm2').exists():
                            shutil.copytree(report.parent/'reuse-vm2',args.output/'reuse-vm2',dirs_exist_ok=True)
                    if fixture_trial and result["cleanup"]["backing_removed"]:
                        storage=Path(config["home_storage_image"]["path"])
                        # The fixture mounts this private template copy read-only.
                        # Keep receipts and hashes for failures, not another disk copy.
                        if storage==Path(config["run"])/"home-storage.raw" and not storage.is_symlink():
                            storage.unlink()
                            result['cleanup']['storage_copy_removed']=True
            result["worktree_local_du"] = subprocess.check_output(["du", "-sh", str(LOCAL)], text=True).strip()
            (args.output / "trial.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
