#!/usr/bin/env python3
"""Exercise existing direct/reuse contracts without changing their guards.

Every case owns a fresh disposable L1 pair. Direct negative tests use real
Firecracker pause/export calls, then check whether the source can still work.
An expected compatibility rejection is distinct from successful migration.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import time

HERE = Path(__file__).resolve().parent
CASES = ("direct-baseline", "direct-two-vcpu", "direct-block", "reuse-block", "reuse-two-vcpu")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--qemu-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    require(not args.out.exists(), "Choose a new output directory")
    out = args.out.resolve()
    out.mkdir(parents=True)
    os.environ["CXL_LAB_SESSION"] = "compat-" + secrets.token_hex(6)
    os.environ["CXL_LAB_ASSETS"] = str(args.assets.resolve())
    os.environ["CXL_LAB_QEMU_ROOT"] = str(args.qemu_root.resolve())
    # These modules resolve paths/session configuration when imported.
    import lab
    import nfs_bench as nfs

    direct = args.case.startswith("direct")
    block = args.case.endswith("block")
    report = {"schema_version": 1, "case": args.case, "result": "running",
              "compatibility": "not_tested", "source_usable_after_rejection": None,
              "arguments": {key: str(value) if isinstance(value, Path) else value
                            for key, value in vars(args).items()},
              "runtime_sources": {name: lab.sha256(HERE / name) for name in
                                  ("mode_compatibility.py", "nfs_bench.py", "lab.py")},
              "api": [], "cleanup_errors": [],
              "limitations": ["Nested KVM and coherent emulated shared CXL on one host",
                              "Expected rejection proves the existing contract, not device migration",
                              "A failed direct pause can partially quiesce the source; usability is tested separately"]}
    measured_sources = out / "measured-sources"
    measured_sources.mkdir()
    for name, expected in report["runtime_sources"].items():
        raw = (HERE / name).read_bytes()
        (measured_sources / name).write_bytes(raw)
        require(lab.sha256(measured_sources / name) == expected, f"Source changed during capture: {name}")
    config = None
    directory = "/run/compat-" + secrets.token_hex(8)
    started = False

    def save():
        lab.save_json(out / "report.json", report)

    def command(arguments, filename):
        with (out / filename).open("w") as stream:
            result = subprocess.run([str(item) for item in arguments], cwd=HERE,
                                    stdout=stream, stderr=subprocess.STDOUT)
        require(result.returncode == 0, f"Command failed ({result.returncode}); see {filename}")

    def api(vm, method, path, body, expected=204):
        text = nfs.shell(vm, "set +e\n/lab/shared-probe api " + directory + "/api.sock " +
                         shlex.join([method, path, "" if body is None else json.dumps(body), str(expected)]) +
                         "\nrc=$?\nprintf 'COMPAT_API_RC=%s\\n' \"$rc\"\n")
        (out / f"api-{len(report['api']):02d}.txt").write_text(text)
        lines = [line[len("LAB_API_JSON "):] for line in text.splitlines()
                 if line.startswith("LAB_API_JSON ")]
        require(len(lines) == 1, f"Missing API response: {text}")
        row = json.loads(lines[0])
        row.update(method=method, path=path, request=body)
        report["api"].append(row)
        save()
        require(row["status"] == expected, f"Unexpected API status: {row}")
        return row

    def pool(vm, name):
        raw = nfs.shell(vm, "cat /sys/module/kvm/parameters/dax_pool")
        (out / (name + ".txt")).write_text(raw)
        return {key: int(value) for key, value in re.findall(
            r"\b(state|owner|generation|vm_create_calls|vcpu_create_calls)=([0-9]+)", raw)}

    def bounded_probe(vm, label, offset, timeout=5):
        # Timeout is observed inside the L1 shell, so an unsupported I/O path
        # cannot strand the host controller on a blocking console command.
        cmd = f"read {label} {offset} 1048576 4096 seq 1"
        pattern = f'"label":"{label}".*"ok":(true|false)}}[[:space:]]*$'
        text = nfs.shell(vm, f"printf '%s\\n' {shlex.quote(cmd)} > {directory}/input\n"
            f"n=0\nwhile ! grep -Eq {shlex.quote(pattern)} {directory}/fc.log; do\n"
            f"n=$((n+1))\nif [ $n -ge {timeout * 50} ]; then echo COMPAT_PROBE_TIMEOUT; exit 0; fi\n"
            "sleep .02\ndone\n" + f"grep -E {shlex.quote(pattern)} {directory}/fc.log", timeout=timeout+10)
        (out / (label + ".txt")).write_text(text)
        if any(line.strip() == "COMPAT_PROBE_TIMEOUT" for line in text.splitlines()):
            return {"completed": False, "reason": "No complete guest read response within bounded wait"}
        rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"event":"read"')]
        require(len(rows) == 1, f"Invalid read output: {text}")
        value = rows[0]
        require(value["label"] == label and value["ok"] and not value["mismatched_words"], "Read data error")
        return {"completed": True, "read": value}

    def counter(vm):
        text = nfs.shell(vm, f"grep '^L2_COUNTER=' {directory}/fc.log | tail -n 1")
        values = re.findall(r"^L2_COUNTER=([0-9]+)\s*$", text, re.M)
        require(len(values) == 1, f"Missing diskless progress counter: {text}")
        return int(values[0])

    save()
    try:
        arguments = ["python3", HERE / "lab.py", "start", "--vcpus", "2", "--memory-mib", "2048",
                     "--cxl-mib", "4096", "--dax-guest", "--dax-4k", "--qemu", args.qemu,
                     "--memory-backing-dir", "/dev/shm"]
        if direct:
            arguments.append("--dax-handoff")
        if args.case != "direct-baseline":
            arguments += ["--migration-network", "--network-backend", "tap", "--storage-image", args.assets / "storage.raw"]
        command(arguments, "start.log")
        started = True
        run_path, config = lab.current_run()
        report["run"] = str(run_path)
        report["config"] = config
        command(["python3", HERE / "lab.py", "verify", "--timeout", "180"], "verify.log")
        if args.case == "direct-baseline":
            command(["python3", HERE / "lab.py", "direct-handoff-bench", "--guest", "linux",
                     "--iterations", str(args.iterations), "--warmup", "0", "--control", "local",
                     "--reuse-fds", "--kernel-mode", "3", "--guest-cpu", "0", "--executor-cpu", "1",
                     "--poll-us", "5", "--spin-us", "50", "--timer-slack-ns", "1"], "direct.log")
            candidates = sorted(run_path.glob("direct-linux-handoff-*/report.json"))
            require(len(candidates) == 1, "Ambiguous direct baseline report")
            baseline = json.loads(candidates[0].read_text())
            require(baseline["result"] == "passed", "Direct baseline failed")
            report.update(compatibility="supported_diskless_direct", baseline_report=str(candidates[0]),
                          baseline_validation=baseline["validation"], result="passed")
        else:
            a, b = config["vms"]["A"], config["vms"]["B"]
            report["vhost_affinity"] = nfs.pin_vms(config, argparse.Namespace(host_cpus_a="0-2", host_cpus_b="3-5"))
            for name, text in nfs.setup_storage(a, b, directory).items():
                (out / (name + ".txt")).write_text(text)
            (out / "B-nfs-mounts.txt").write_text(nfs.select_backend(b, "nfs"))
            # Verify the target NFS image is readable before testing mode gates.
            (out / "B-nfs-image-read.txt").write_text(nfs.shell(b,
                "dd if=/disk/sandbox.raw of=/dev/null bs=4096 count=1\ncat /proc/net/rpc/nfs"))
            if direct:
                for role, vm in (("A", a), ("B", b)):
                    mode, node = (1, 1) if role == "A" else (2, 2)
                    raw = nfs.shell(vm, "resource=$(cat /sys/bus/dax/devices/dax0.0/resource)\n"
                        "echo 3 > /sys/module/kvm/parameters/dax_handoff_mode\n" +
                        f"echo {node} > /sys/module/kvm/parameters/dax_node\necho {mode} > /sys/module/kvm/parameters/dax_shared\n" +
                        "printf '%x,%x\\n' \"$((resource + 2415919104))\" 134217728 > /sys/module/kvm/parameters/dax_pool\n"
                        "cat /sys/module/kvm/parameters/dax_pool\ncat /sys/module/kvm_intel/parameters/pml")
                    (out / (role + "-pool-setup.txt")).write_text(raw)
            env = {"FC_DAX_RAM": "/dev/dax0.0", "FC_DAX_OFFSET": "2097152", "FC_DAX_SNAPSHOT": "0" if direct else "1"}
            if direct:
                env.update(FC_DAX_DIRECT="1", FC_DAX_HEAP="/dev/dax0.0", FC_DAX_HEAP_OFFSET="2684354560", FC_DAX_HEAP_BYTES="67108864")
            else:
                env.update(FC_DAX_REUSE="1", FC_DAX_NO_NESTED_GUEST="1")
            report["firecracker_environment"] = env
            nfs.shell(a, f"mkfifo {directory}/input\n(exec 3<>{directory}/input\nexec env " +
                shlex.join([f"{key}={value}" for key, value in env.items()]) +
                f" /lab/firecracker --no-seccomp --api-sock {directory}/api.sock <&3 > {directory}/fc.log 2>&1) &\n" +
                nfs.record_pid(directory) + f"n=0\nwhile [ ! -S {directory}/api.sock ]; do\n"
                f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 500 ]\nsleep .02\ndone")
            vcpus = 1 if block else 2
            components = {"/machine-config": {"vcpu_count": vcpus, "mem_size_mib": 512},
                "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux",
                    "initrd_path": "/lab/nfs-l2-initramfs.cpio" if block else "/lab/l2-initramfs.cpio",
                    "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init nmi_watchdog=0"}}
            if block:
                components["/drives/data"] = {"drive_id": "data", "path_on_host": "/disk/sandbox.raw",
                    "is_root_device": False, "is_read_only": True, "cache_type": "Writeback", "io_engine": "Sync"}
            report["guest_configuration"] = components
            for path, body in components.items():
                api(a, "PUT", path, body)
            api(a, "PUT", "/actions", {"action_type": "InstanceStart"})
            ready = '"event":"ready"' if block else 'L2_COUNTER=2'
            nfs.shell(a, f"n=0\nwhile ! grep -Fq {shlex.quote(ready)} {directory}/fc.log; do\n"
                f"! grep -q 'Kernel panic' {directory}/fc.log\nread p s < {directory}/fc.pid\nkill -0 \"$p\"\n"
                "n=$((n+1))\n[ $n -lt 3000 ]\nsleep .02\ndone", timeout=90)
            before = bounded_probe(a, "before_guard", 0) if block else {"counter": counter(a)}
            if block:
                require(before["completed"], "File guest failed before compatibility check")
            report["source_before"] = before
            report["pool_before"] = pool(a, "pool-before")
            if direct:
                rejection = api(a, "PATCH", "/vm", {"state": "Paused"}, 400)
                require("Direct shared KVM object handoff failed" in rejection["response"]
                        and "os error 95" in rejection["response"], "Direct rejection was not EOPNOTSUPP")
                report["rejection_stage"] = "direct_pause_export"
            else:
                api(a, "PATCH", "/vm", {"state": "Paused"})
                rejection = api(a, "PUT", "/actions", {"action_type": "PrepareReuse"}, 400)
                expected = "diskless MMIO workers only" if block else "one-vCPU"
                require(expected in rejection["response"], f"Reuse rejected at a different guard than {expected}")
                report["rejection_stage"] = "prepare_reuse"
            report["pool_after"] = pool(a, "pool-after")
            if direct:
                for key in ("state", "owner", "generation"):
                    require(key in report["pool_before"] and report["pool_after"].get(key) == report["pool_before"][key],
                            f"Direct rejection changed shared ownership field {key}")
                report["ownership_unchanged"] = True
            report["source_api_after_rejection"] = api(a, "GET", "/", None, 200)
            api(a, "PATCH", "/vm", {"state": "Resumed"})
            if block:
                after = bounded_probe(a, "after_guard_cold", 128 * 1024 * 1024)
                usable = after["completed"]
                if usable:
                    require(after["read"]["open_cookie"] == before["read"]["open_cookie"], "Guest identity changed")
                    require(after["read"]["resident_before"] == 0 and after["read"]["block_delta"]["read_sectors"] > 0,
                            "Post-rejection read did not exercise cold backing I/O")
            else:
                time.sleep(2)
                after = {"counter": counter(a)}
                usable = after["counter"] > before["counter"]
            report.update(source_after=after, source_usable_after_rejection=usable,
                          compatibility="rejected_by_existing_guard", result="passed")
            if direct and not usable:
                report["source_recovery_finding"] = "Resume API succeeded but source guest I/O/progress did not recover; direct pause partially quiesces before rejecting."
            if not direct:
                require(usable, "Reuse rejection left the source unusable")
    except BaseException as exc:
        report.update(result="failed", error=repr(exc))
        raise
    finally:
        if config is not None and args.case != "direct-baseline":
            for role, vm in config["vms"].items():
                try:
                    nfs.stop_fc(vm, directory)
                    if role == "A":
                        raw, integrity = lab.guest_fetch_file(vm, directory + "/fc.log")
                        (out / "source-firecracker.log").write_bytes(raw)
                        report["source_log_integrity"] = integrity
                except Exception as exc:
                    report["cleanup_errors"].append(f"{role}: {exc}")
        if started:
            try:
                command(["python3", HERE / "lab.py", "stop"], "stop.log")
                report["cleanup"] = {"owned_qemu_alive": {role: lab.alive(vm) for role, vm in config["vms"].items()},
                                     "cxl_backing_exists": Path(config["cxl_backing"]).exists()}
                require(not any(report["cleanup"]["owned_qemu_alive"].values()) and not report["cleanup"]["cxl_backing_exists"],
                        "Owned VM/shared backing remained after cleanup")
            except Exception as exc:
                report["cleanup_errors"].append(str(exc))
        if report["cleanup_errors"]:
            report["result"] = "failed"
        save()
        print(f"COMPATIBILITY_REPORT={out / 'report.json'}", flush=True)
    require(report["result"] == "passed", "Compatibility checks failed; inspect report")


if __name__ == "__main__":
    main()
