#!/usr/bin/env python3
"""Replay recorded development traces across A->B->A shared-DAX handoffs.

The readonly lower filesystem is a Firecracker block image, opened locally on
A and either locally or through host NFS on B. Overlay writes remain in guest
tmpfs. No host mounts, services, or global cache drops are used.
"""
import argparse
import base64
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import time

import lab
import nfs_bench as nfs

HERE = Path(__file__).resolve().parent
SOURCES = ("trace_bench.py", "trace_prepare.py", "trace-l2-init.sh", "trace-guest-run.sh",
           "nfs_bench.py", "nfs_probe.c", "lab.py", "nfs_dependencies.sh", "shared_probe.c", "cache_probe.h", "mptcp_probe.h")


def serial(vm, directory, command, token, event, timeout=180):
    pattern = f'"event":"{event}","label":"{token}"'
    text = nfs.shell(vm, f"printf '%s\\n' {shlex.quote(command)} > {directory}/input\n"
        f"n=0\nwhile ! grep -Eq {shlex.quote(re.escape(pattern) + '.*}[[:space:]]*$')} {directory}/fc.log; do\n"
        f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\n"
        f"! grep -Eq 'TRACE_FATAL|Kernel panic|\"event\":\"trace_(error|protocol_error)\"' {directory}/fc.log\n"
        f"n=$((n+1))\n[ $n -lt {timeout*50} ]\nsleep .02\ndone\n"
        f"grep -F {shlex.quote(pattern)} {directory}/fc.log", timeout=timeout+15)
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"event":')]
    rows = [r for r in rows if r.get("event") == event and r.get("label") == token]
    if len(rows) != 1:
        raise RuntimeError(f"Missing/duplicate serial event {token}: {text}")
    return rows[0]


def observe(vm, out, relative):
    directory = out / relative
    directory.mkdir(parents=True)
    files = {"nfs.txt": "/proc/net/rpc/nfs", "mountstats.txt": "/proc/self/mountstats",
             "rx_bytes.txt": "/sys/class/net/eth0/statistics/rx_bytes",
             "tx_bytes.txt": "/sys/class/net/eth0/statistics/tx_bytes"}
    body = ""
    for name, path in files.items():
        body += f"printf 'OBS_BEGIN {name}\\n'\ncat {path}\nprintf 'OBS_END {name}\\n'\n"
    text = nfs.shell(vm, body)
    values = {}
    for name in files:
        match = re.search(rf"^OBS_BEGIN {re.escape(name)}\n(.*?)^OBS_END {re.escape(name)}$", text.replace("\r", ""), re.M | re.S)
        if not match:
            raise RuntimeError(f"Incomplete raw host counters: {text}")
        (directory / name).write_text(match[1])
        values[name] = match[1]
    proc = next(line.split() for line in values["nfs.txt"].splitlines() if line.startswith("proc3 "))
    counters = {"rx_bytes": int(values["rx_bytes.txt"]), "tx_bytes": int(values["tx_bytes.txt"]),
                "nfs_read_calls": int(proc[8]), "mount_read_ops": 0,
                "mount_read_sent_bytes": 0, "mount_read_received_bytes": 0}
    active = False
    for line in values["mountstats.txt"].splitlines():
        words = line.split()
        if words and words[0] == "device":
            active = " mounted on /remote " in line
        if active and words and words[0] == "READ:":
            counters.update(zip(("mount_read_ops", "mount_read_sent_bytes", "mount_read_received_bytes"),
                                (int(words[i]) for i in (1, 4, 5))))
    return counters


def save_log(vm, directory, out, trial, role, stage):
    raw, integrity = lab.guest_fetch_file(vm, directory + "/fc.log")
    name = f"{trial['id']}-stage{stage}-{role}.log"
    (out / name).write_bytes(raw)
    trial["logs"].append({"role": role, "stage": stage, "path": name, "integrity": integrity})
    return raw


def source_fence_script(directory):
    # A zombie may be reaped between existence testing and reading /proc.
    # Read first; a failed read is safe only if that process entry is gone.
    return (f"read p expected < {directory}/fc.pid\n"
        "case \"$p:$expected\" in *[!0-9:]*|:|*:) exit 1 ;; esac\n"
        "[ \"$p\" -gt 1 ]\n[ \"$expected\" -gt 0 ]\n"
        "observed=0\nreason=missing\nif record=$(cat /proc/$p/stat 2>/dev/null); then\n"
        "record=${record##*) }\nset -- $record\nstate=$1\nshift 19\nobserved=$1\n"
        "case \"$observed\" in ''|*[!0-9]*) exit 1 ;; esac\n[ \"$observed\" -gt 0 ]\n"
        "if [ \"$observed\" != \"$expected\" ]; then reason=reused\n"
        "else case \"$state\" in Z|X) reason=$state ;; *) exit 1 ;; esac\nfi\n"
        "else [ ! -e /proc/$p/stat ] || exit 1\nfi\n"
        "printf 'FENCED %s %s\\nFENCE_STATE %s %s\\n' \"$p\" \"$expected\" \"$reason\" \"$observed\"")


def handoff(vms, directory, out, trial, source, destination, stage, transport, memory_mib):
    source_vm, target_vm = vms[source], vms[destination]
    token = f"{trial['id']}_h{stage}_before"
    before = serial(source_vm, directory, f"identity {token}", token, "trace_identity")
    prefix = f"{trial['id']}-h{stage}"
    observations, mailbox = {}, {}

    def observed_api(vm, phase, path, method, body):
        raw = nfs.api(vm, directory, path, method, body)
        filename = f"{prefix}-{phase}-api.txt"
        (out / filename).write_text(raw)
        rows = [json.loads(line.removeprefix("LAB_API_JSON ")) for line in raw.splitlines()
                if line.startswith("LAB_API_JSON ")]
        if len(rows) != 1 or rows[0]["status"] != 204:
            raise RuntimeError(f"Invalid native API receipt: {phase}")
        observations[phase] = {"file": filename, "receipt": rows[0]}

    def transfer(vm, phase, role):
        raw = nfs.shell(vm, "/lab/shared-probe state-mailbox " + shlex.join([
            role, phase, "/dev/dax0.0", directory + "/state", trial["mailbox_nonce"],
            str(stage + 1), str(memory_mib)]))
        filename = f"{prefix}-mailbox-{phase}.txt"
        (out / filename).write_text(raw)
        rows = [json.loads(line.removeprefix("STATE_MAILBOX_JSON ")) for line in raw.splitlines()
                if line.startswith("STATE_MAILBOX_JSON ")]
        if len(rows) != 1:
            raise RuntimeError(f"Invalid native mailbox receipt: {phase}")
        mailbox[phase] = {"file": filename, "receipt": rows[0]}

    # Prestart only an empty API process. Restore creates its private KVM objects.
    # No guest RAM mapping or initialization happens during this preparation.
    if transport == "dax-mailbox":
        nfs.start_fc(target_vm, directory)
    t = time.monotonic_ns()
    observed_api(source_vm, "pause", "/vm", "PATCH", {"state": "Paused"})
    observed_api(source_vm, "snapshot", "/snapshot/create", "PUT", {"snapshot_type": "Full",
        "snapshot_path": directory + "/state", "mem_file_path": "/dev/dax0.0"})
    nfs.stop_fc(source_vm, directory)
    fenced = nfs.shell(source_vm, source_fence_script(directory))
    if transport == "dax-mailbox":
        transfer(source_vm, "publish", source)
        transfer(target_vm, "receive", destination)
    else:
        state, integrity = lab.guest_fetch_file(source_vm, directory + "/state")
        nfs.upload(target_vm, directory + "/state", state)
        nfs.start_fc(target_vm, directory)
    observed_api(target_vm, "load", "/snapshot/load", "PUT", {"snapshot_path": directory + "/state",
        "mem_backend": {"backend_type": "File", "backend_path": "/dev/dax0.0"}, "resume_vm": False})
    observed_api(target_vm, "resume", "/vm", "PATCH", {"state": "Resumed"})
    token = f"{trial['id']}_h{stage}_after"
    after = serial(target_vm, directory, f"identity {token}", token, "trace_identity")
    controller_handoff_ns = time.monotonic_ns() - t
    for key in ("boot_id", "pid", "next_index", "sentinel_sha256", "fd_inode"):
        if before[key] != after[key]:
            raise RuntimeError(f"Handoff lost guest state: {key}")
    if (not before["fd_deleted"] or not after["fd_deleted"] or
            after["fd_ordinal"] != before["fd_ordinal"] + 1 or
            after["fd_line"] != f"FD-{after['boot_id']}-{after['fd_ordinal']}" or
            after["fd_position"] != before["fd_position"] + len(after["fd_line"]) + 1):
        raise RuntimeError("Handoff lost unlinked open file or its read cursor")
    # Evidence collection is outside the pause-to-identity controller interval.
    # In mailbox mode these are audit copies, never the restore transport.
    if transport == "dax-mailbox":
        state, integrity = lab.guest_fetch_file(source_vm, directory + "/state")
    received, received_integrity = lab.guest_fetch_file(target_vm, directory + "/state")
    if state != received:
        raise RuntimeError("Destination checkpoint differs from source")
    name, received_name = prefix + "-state.bin", prefix + "-destination-state.bin"
    (out / name).write_bytes(state)
    (out / received_name).write_bytes(received)
    save_log(source_vm, directory, out, trial, source, stage)
    fence_file = prefix + "-fence.txt"
    (out / fence_file).write_text(fenced)
    record = {"source": source, "destination": destination, "before_identity": before,
              "after_identity": after, "state_file": name, "state_bytes": len(state),
              "state_integrity": integrity, "ram_transfer_bytes": 0, "source_fence": fenced,
              "source_fenced": True, "fence_file": fence_file,
              "state_transport": transport, "handoff_mode": "snapshot",
              "destination_prestarted": transport == "dax-mailbox",
              "destination_state_file": received_name, "destination_state_integrity": received_integrity,
              "api_observations": observations, "controller_handoff_ns": controller_handoff_ns}
    if mailbox:
        record["mailbox"] = mailbox
    trial["handoffs"].append(record)
    return record


def summarize(trials):
    groups = collections.defaultdict(list)
    for trial in trials:
        if trial.get("result") == "passed":
            groups[(trial["trace"], trial["backend"], trial["cache_mode"])].append(trial)
    result = []
    for (trace, backend, cache), rows in sorted(groups.items()):
        item = {"trace": trace, "backend": backend, "cache_mode": cache, "n": len(rows)}
        for key in ("guest_duration_s", "destination_duration_s", "nfs_read_calls", "nfs_received_bytes"):
            values = [r[key] for r in rows]
            item[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
        result.append(item)
    return result


def run(args):
    run_path, config = lab.current_run()
    if not config.get("dax_guest") or config.get("dax_kvm_state") or config.get("dax_handoff"):
        raise RuntimeError("Requires ordinary shared-DAX lab")
    if (config.get("migration_network") or {}).get("backend") != "tap" or not config.get("storage_image"):
        raise RuntimeError("Requires isolated TAP network and prepared storage image")
    if args.memory_mib + 2 > config["cxl_mib"] or args.vcpus > config["l1_vcpus"]:
        raise ValueError("Guest RAM/vCPUs exceed lab capacity")
    if not 1 <= args.cycles <= 10 or not 0 <= args.eviction_cycles <= 10:
        raise ValueError("cycles must be 1..10; eviction-cycles 0..10")
    manifest = config["prepared"]
    names = args.names or list(manifest["traces"])
    if len(set(names)) != len(names) or not set(names) <= set(manifest["traces"]):
        raise ValueError("Invalid trace selection")
    out = run_path / ("trace-" + str(time.time_ns()))
    out.mkdir()
    (out / "measured-sources").mkdir()
    for name in SOURCES:
        shutil.copy2(HERE / name, out / "measured-sources" / name)
    (out / "traces").mkdir()
    for name in names:
        shutil.copy2(lab.ASSETS / "traces" / (name + ".traj.json"), out / "traces" / (name + ".traj.json"))
    lab.save_json(out / "manifest.json", manifest)
    report = {"schema_version": 1, "result": "running", "config": config,
        "arguments": {**vars(args), "names": names}, "trials": [], "cleanup_errors": [],
        "runtime_sources": {name: lab.sha256(HERE / name) for name in SOURCES},
        "limitations": ["One physical host, nested KVM, emulated coherent CXL",
            "Guest RAM begins in shared DAX; no initial DRAM-to-CXL promotion",
            "Readonly image lower plus guest tmpfs upper; no shared writable disk",
            "Serial control commands remain; state transport is selected separately",
            "Controller handoff includes shell/API waits and identity check; excludes prestart and audit collection",
            "Controller pauses are excluded from command-time totals",
            "Guest uptime resolution 10ms; no recorded think-time gaps or external network",
            "No physical NIC or network shaping; device/controller cache uncontrolled"]}
    lab.save_json(out / "report.json", report)
    vms = config["vms"]
    a, b = vms["A"], vms["B"]
    directory = "/run/trace-" + os.urandom(8).hex()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report["vhost_cpu_affinity"] = nfs.pin_vms(config, args)
        report["host_cpu_affinity"] = {role: {
            thread.name: sorted(os.sched_getaffinity(int(thread.name)))
            for thread in Path(f"/proc/{vm['pid']}/task").iterdir()}
            for role, vm in vms.items()}
        environment = nfs.setup_storage(a, b, directory, names[0] + ".raw")
        for key, value in environment.items():
            (out / (key + ".txt")).write_text(value)
        for mode, cycles in (("retained", args.cycles), ("evicted", args.eviction_cycles)):
            for cycle in range(cycles):
                for name in names:
                    entry = manifest["traces"][name]
                    backends = ["nfs", "local"] if cycle % 2 == 0 else ["local", "nfs"]
                    for backend in backends:
                        trial = {"id": f"{name}_{mode}_{cycle}_{backend}", "trace": name, "cycle": cycle,
                            "backend": backend, "cache_mode": mode, "commands": [], "handoffs": [], "logs": [],
                            "trace_sha256": entry["trace_sha256"], "image_sha256": entry["image_sha256"],
                            "result": "running"}
                        if args.state_transport == "dax-mailbox":
                            trial["mailbox_nonce"] = os.urandom(8).hex()
                        report["trials"].append(trial)
                        lab.save_json(out / "report.json", report)
                        trial["backend_mount_file"] = trial["id"] + "-B-mounts.txt"
                        (out / trial["backend_mount_file"]).write_text(nfs.select_backend(b, backend))
                        trial["host_cache_before"] = {}
                        for role, vm in vms.items():
                            nfs.shell(vm, "sync\necho 3 > /proc/sys/vm/drop_caches")
                            raw = nfs.shell(vm, f"/lab/nfs-probe resident /disk/{name}.raw host 0 {entry['image_bytes']}")
                            (out / f"{trial['id']}-{role}-cache.txt").write_text(raw)
                            pages = [json.loads(line)["resident_after"] for line in raw.splitlines() if line.startswith('{"event":"resident"')]
                            if pages != [0]:
                                raise RuntimeError(f"Initial host image cache is not empty: {role} {pages}")
                            trial["host_cache_before"][role] = pages[0]
                        trial["trial_counter_before"] = f"counters/{trial['id']}-trial-before"
                        trial_before = observe(b, out, trial["trial_counter_before"])
                        nfs.start_fc(a, directory)
                        components = {
                            "/machine-config": {"vcpu_count": args.vcpus, "mem_size_mib": args.memory_mib},
                            "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux",
                                "initrd_path": f"/lab/{name}-initramfs.cpio",
                                "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init"},
                            "/drives/data": {"drive_id": "data", "path_on_host": f"/disk/{name}.raw",
                                "is_root_device": False, "is_read_only": True, "cache_type": "Writeback", "io_engine": "Sync"}}
                        for path, body in components.items():
                            nfs.api(a, directory, path, "PUT", body)
                        nfs.api(a, directory, "/actions", "PUT", {"action_type": "InstanceStart"})
                        nfs.shell(a, f"n=0\nwhile ! grep -q '\"event\":\"trace_ready\"' {directory}/fc.log; do\n"
                            f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\n"
                            f"! grep -q 'Kernel panic' {directory}/fc.log\n"
                            "n=$((n+1))\n[ $n -lt 3000 ]\nsleep .02\ndone", timeout=90)
                        role, stage = "A", 0
                        boundaries = [entry["handoff_before"], entry["command_count"] - 2]
                        trial["handoff_before"] = boundaries
                        trial_start = time.monotonic_ns()
                        for command in entry["commands"]:
                            index = command["index"]
                            if index in boundaries:
                                target = "B" if role == "A" else "A"
                                handoff(vms, directory, out, trial, role, target, stage, args.state_transport, args.memory_mib)
                                role, stage = target, stage + 1
                                if mode == "evicted":
                                    token = f"{trial['id']}_evict{stage}"
                                    trial.setdefault("evictions", []).append(serial(vms[role], directory,
                                        f"evict {token}", token, "trace_evicted"))
                            token = f"{trial['id']}_cmd{index}"
                            before_path = f"counters/{token}-before"
                            after_path = f"counters/{token}-after"
                            before = observe(b, out, before_path)
                            row = serial(vms[role], directory, f"run {index} {token}", token, "trace_end",
                                         timeout=int(entry["environment"].get("timeout", 60)) + 30)
                            after = observe(b, out, after_path)
                            row.update(role=role, returncode=row["rc"], guest_duration_s=round(row["end_s"]-row["start_s"], 6),
                                       counter_before=before_path, counter_after=after_path, network_delta=nfs.delta(before, after))
                            if row["timed_out"]:
                                raise RuntimeError(f"Trace command timed out: {name} command {index}")
                            expected = command["expected_returncode"]
                            if row["command_sha256"] != command["command_sha256"] or row["rc"] != (expected if expected is not None else 0):
                                raise RuntimeError(f"Trace command diverged: {name} command {index}: {row}, expected {expected}")
                            trial["commands"].append(row)
                            lab.save_json(out / "report.json", report)
                        token = trial["id"] + "_export"
                        exported = serial(vms[role], directory, f"export {token}", token, "trace_export", timeout=180)
                        nfs.stop_fc(vms[role], directory)
                        raw = save_log(vms[role], directory, out, trial, role, stage)
                        # Archive framing is generated by the guest controller, never workload stdout.
                        match = re.search(rf"TRACE_EXPORT_BEGIN {re.escape(token)} [0-9]+ [0-9a-f]{{64}}\r?\n(.*?)TRACE_EXPORT_END {re.escape(token)}", raw.decode(), re.S)
                        if not match:
                            raise RuntimeError("Missing complete guest result archive")
                        archive = base64.b64decode("".join(match[1].split()), validate=True)
                        if hashlib.sha256(archive).hexdigest() != exported["sha256"]:
                            raise RuntimeError("Guest archive hash mismatch")
                        trial["guest_archive"] = trial["id"] + "-guest.tar.gz"
                        trial["guest_archive_sha256"] = exported["sha256"]
                        (out / trial["guest_archive"]).write_bytes(archive)
                        trial["trial_counter_after"] = f"counters/{trial['id']}-trial-after"
                        trial["trial_network_delta"] = nfs.delta(trial_before, observe(b, out, trial["trial_counter_after"]))
                        command_totals = {key: sum(c["network_delta"][key] for c in trial["commands"])
                                          for key in trial_before}
                        trial["outside_command_network_delta"] = nfs.delta(command_totals, trial["trial_network_delta"])
                        trial["guest_duration_s"] = round(sum(c["guest_duration_s"] for c in trial["commands"]), 6)
                        trial["destination_duration_s"] = round(sum(c["guest_duration_s"] for c in trial["commands"] if c["role"] == "B"), 6)
                        trial["nfs_read_calls"] = sum(c["network_delta"]["nfs_read_calls"] for c in trial["commands"])
                        trial["nfs_received_bytes"] = sum(c["network_delta"]["mount_read_received_bytes"] for c in trial["commands"])
                        trial["controller_replay_ns"] = time.monotonic_ns() - trial_start
                        trial["result"] = "passed"
                        report["summary"] = summarize(report["trials"])
                        lab.save_json(out / "report.json", report)
                        print(json.dumps({"trial": trial["id"], "seconds": trial["guest_duration_s"],
                                          "nfs_reads": trial["nfs_read_calls"]}), flush=True)
        report["result"] = "passed"
    except BaseException as exc:
        report.update(result="failed", error=repr(exc))
        raise
    finally:
        for role, vm in vms.items():
            try:
                nfs.stop_fc(vm, directory)
                raw, _ = lab.guest_fetch_file(vm, directory + "/fc.log")
                (out / f"last-{role}.log").write_bytes(raw)
            except Exception as exc:
                report["cleanup_errors"].append(f"{role}: {exc}")
        if report["cleanup_errors"]:
            report["result"] = "failed"
        report["summary"] = summarize(report["trials"])
        lab.save_json(out / "report.json", report)
        print(f"TRACE_REPORT={out / 'report.json'}", flush=True)
    if report["result"] != "passed":
        raise RuntimeError(report.get("error", "Cleanup failed"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-transport", choices=("serial", "dax-mailbox"), default="serial",
                        help="Snapshot state transport; DAX mode prestarts only the destination API process")
    parser.add_argument("--names", nargs="+")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--eviction-cycles", type=int, default=1)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--vcpus", type=int, default=2)
    parser.add_argument("--host-cpus-a", default="0-2")
    parser.add_argument("--host-cpus-b", default="3-5")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
