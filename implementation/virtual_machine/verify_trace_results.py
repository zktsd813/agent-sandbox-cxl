#!/usr/bin/env python3
"""Audit actual-trace migration results using raw evidence, without starting VMs.

The input trace, guest command files and outputs, serial records, block/NFS
counters, snapshot state and measured source files are checked independently.
Large prepared disk images are optional; --artifacts checks them when available.
"""
import argparse
import base64
import collections
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import statistics
import tarfile

WARNING = b"/root/.bashrc: line 1: /opt/miniconda3/etc/profile.d/conda.sh: No such file or directory\n"
SUBMIT = b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
COUNTERS = ("rx_bytes", "tx_bytes", "nfs_read_calls", "mount_read_ops",
            "mount_read_sent_bytes", "mount_read_received_bytes")
FATAL_KERNEL = re.compile(r"Kernel panic|Out of memory:|oom-kill:|Killed process [0-9]+|"
                          r"Buffer I/O error|EXT4-fs error|I/O error, dev vda|"
                          r"blk_update_request: I/O error|\bBUG:|\bkernel BUG at|\bOops:", re.I)


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inside(directory, name):
    path = directory / name
    require(not Path(name).is_absolute() and path.resolve().is_relative_to(directory.resolve()),
            f"evidence path escapes directory: {name}")
    return path


def raw_receipt(directory, evidence, prefix, label):
    raw = inside(directory, evidence["file"]).read_text().replace("\r", "")
    records = [json.loads(line[len(prefix):]) for line in raw.splitlines() if line.startswith(prefix)]
    require(len(records) == 1, f"{label}: missing/duplicate native receipt")
    require(records[0] == evidence["receipt"], f"{label}: native receipt raw/report disagreement")
    return records[0]


def fnv1a64(data):
    value = 14695981039346656037
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return f"{value:016x}"


def audit_state_transport(handoff, trial, stage, directory, memory_bytes, transport):
    """Bind native receipts to this trial and to both independently fetched states."""
    label = f"{trial['id']}/handoff{stage}"
    require(handoff.get("state_transport", "serial") == transport, f"{label}: state transport disagreement")
    require(handoff.get("handoff_mode", "snapshot" if transport == "serial" else None) == "snapshot",
            f"{label}: trace evidence does not demonstrate direct/reuse handoff")
    if transport == "serial":
        require("mailbox" not in handoff, f"{label}: serial handoff contains mailbox claims")
        return {"transport": transport, "mailbox_phases": {}, "api_phases": {}}
    require(transport == "dax-mailbox", f"{label}: unknown state transport")
    nonce = trial["mailbox_nonce"]
    require(isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{16}", nonce) and int(nonce, 16) > 0,
            f"{label}: invalid mailbox session nonce")
    source = inside(directory, handoff["state_file"]).read_bytes()
    target_path = inside(directory, handoff["destination_state_file"])
    require(target_path.resolve() != inside(directory, handoff["state_file"]).resolve(),
            f"{label}: destination evidence reuses source state file")
    target = target_path.read_bytes()
    integrity = handoff["destination_state_integrity"]
    require(target == source, f"{label}: mailbox destination state differs from source")
    require(len(target) == integrity["bytes"] and sha(target) == integrity["sha256"] and
            integrity["source_unchanged_during_fetch"] is True, f"{label}: destination state fetch integrity")
    require(handoff["destination_prestarted"] is True, f"{label}: destination was not prestarted")
    require(set(handoff["mailbox"]) == {"publish", "receive"}, f"{label}: mailbox phases")
    durations, files = {}, []
    for phase in ("publish", "receive"):
        evidence = handoff["mailbox"][phase]
        files.append(evidence["file"])
        receipt = raw_receipt(directory, evidence, "STATE_MAILBOX_JSON ", label + "/" + phase)
        expected = {"phase": phase, "role": handoff["source"] if phase == "publish" else handoff["destination"],
                    "source_role": handoff["source"], "nonce": nonce, "round": stage + 1,
                    "schema": "534e415053484f02", "offset": 1048576, "capacity": 262144,
                    "memory_bytes": memory_bytes, "state_bytes": len(source), "state_hash": fnv1a64(source)}
        require(all(receipt.get(key) == value for key, value in expected.items()),
                f"{label}/{phase}: mailbox session/layout/payload metadata mismatch")
        require(0 < len(source) <= receipt["capacity"] and
                receipt["offset"] + receipt["capacity"] < 2097152,
                f"{label}/{phase}: mailbox overlaps guest RAM or exceeds capacity")
        duration = receipt["duration_ns"]
        require(type(duration) is int and 0 <= duration < 2 ** 64,
                f"{label}/{phase}: invalid native duration")
        durations[phase] = duration
    require(set(handoff["api_observations"]) == {"pause", "snapshot", "load", "resume"},
            f"{label}: native API phases")
    api_durations = {}
    for phase, evidence in handoff["api_observations"].items():
        files.append(evidence["file"])
        receipt = raw_receipt(directory, evidence, "LAB_API_JSON ", label + "/" + phase)
        require(receipt["status"] == receipt["expected_status"] == 204 and
                re.match(r"HTTP/1\.1 204(?: |\r?\n)", receipt["response"]),
                f"{label}/{phase}: unsuccessful native API response")
        duration = receipt["duration_us"]
        require(type(duration) in (int, float) and math.isfinite(duration) and duration >= 0,
                f"{label}/{phase}: invalid native API duration")
        api_durations[phase] = duration
    require(len(set(files)) == len(files), f"{label}: reused native receipt file")
    return {"transport": transport, "mailbox_phases": durations, "api_phases": api_durations}


def fingerprint(data):
    return {"sha256": sha(data), "without_startup_warning_sha256": sha(data.replace(WARNING, b"")),
            "startup_warning_count": data.count(WARNING)}


def original_trace(path):
    """Parse source bytes directly instead of importing the replay implementation."""
    raw = path.read_bytes()
    source = json.loads(raw)
    config = source["info"]["config"]
    require(config["environment_type"].endswith(".DockerEnvironment"), "unsupported trace environment")
    require(not config["environment"].get("forward_env"), "host environment forwarding")
    require(source["info"]["exit_status"] == "Submitted", "source trace did not submit")
    batches = []
    for message in source["messages"]:
        extra = message.get("extra", {})
        if message["role"] == "assistant" and extra.get("actions"):
            batches.append([extra["actions"], []])
        elif batches and message["role"] in ("tool", "user") and "returncode" in extra:
            batches[-1][1].append(extra)
    require(bool(batches), "trace has no actions")
    commands = []
    for number, (actions, observations) in enumerate(batches):
        require(len(actions) == len(observations) or (number == len(batches) - 1 and not observations),
                f"incomplete recorded observations in batch {number}")
        for index, action in enumerate(actions):
            require(isinstance(action["command"], str), "non-text trace command")
            data = action["command"].encode()
            commands.append({"index": len(commands), "command": data,
                             "command_sha256": sha(data),
                             "expected_returncode": observations[index]["returncode"] if observations else None,
                             "source_output": observations[index]["raw_output"].encode() if observations else None})
    require(sum(c["expected_returncode"] is None for c in commands) == 1 and
            commands[-1]["expected_returncode"] is None, "only final submission may lack recorded return code")
    return {"sha256": sha(raw), "commands": commands, "environment": config["environment"],
            "submission": fingerprint(source["info"]["submission"].encode())}


def archive_files(path):
    # Never extract guest-controlled paths to the host filesystem.
    files = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts, f"unsafe archive member {member.name}")
            if member.isdir():
                continue
            require(member.isfile(), f"unexpected archive member type: {member.name}")
            key = str(name)
            require(key not in files, f"duplicate archive member: {key}")
            files[key] = archive.extractfile(member).read()
    return files


def newc_files(path, wanted=None):
    """Read newc CPIO directly; do not execute cpio or trust prepared wrappers."""
    data = path.read_bytes()
    offset, result, seen = 0, {}, set()
    while True:
        header = data[offset:offset + 110]
        require(len(header) == 110 and header[:6] in (b"070701", b"070702"), f"{path}: invalid CPIO header")
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        mode, size, namesize = fields[1], fields[6], fields[11]
        offset += 110
        raw_name = data[offset:offset + namesize]
        require(namesize > 0 and len(raw_name) == namesize and raw_name[-1:] == b"\0", f"{path}: invalid CPIO filename")
        name = str(PurePosixPath(raw_name[:-1].decode()))
        require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts,
                f"{path}: unsafe CPIO path")
        offset = (offset + namesize + 3) & ~3
        payload = data[offset:offset + size]
        require(len(payload) == size, f"{path}: truncated CPIO payload")
        offset = (offset + size + 3) & ~3
        if name == "TRAILER!!!":
            require(size == 0 and not any(data[offset:]), f"{path}: unexpected data after CPIO trailer")
            return result
        require(name not in seen, f"{path}: duplicate CPIO entry: {name}")
        seen.add(name)
        if header[:6] == b"070702":
            require(sum(payload) & 0xffffffff == fields[12], f"{path}: CPIO CRC")
        if wanted is None or name in wanted:
            result[name] = {"mode": mode, "data": payload}


def effective_environment(plan, trace):
    environment = dict(item.split("=", 1) for item in plan["image_metadata"]["env"])
    environment.update({"HOME": "/root", "HOSTNAME": "swe-microvm"})
    environment.update(trace["environment"].get("env", {}))
    require(environment == plan["effective_environment"], f"{plan['name']}: prepared environment differs from image+trace")
    return environment


def prepared_commands(asset_dir, plan, trace, sources, l1_files):
    name = plan["name"]
    initramfs = asset_dir / f"{name}-initramfs.cpio"
    require(l1_files[f"lab/{name}-initramfs.cpio"]["data"] == initramfs.read_bytes(),
            f"{name}: L1 embedded initramfs differs from audited standalone artifact")
    files = newc_files(initramfs)
    for destination, source in (("init", "trace-l2-init.sh"), ("lab/trace-guest-run.sh", "trace-guest-run.sh")):
        require(files[destination]["mode"] & 0o170000 == 0o100000 and
                files[destination]["data"] == (sources / source).read_bytes(), f"{name}: prepared controller {destination}")
    require(files["init"]["mode"] & 0o111, f"{name}: init is not executable")
    environment = effective_environment(plan, trace)
    for command in trace["commands"]:
        prefix = f"lab/trace/cmd/{command['index']:03d}"
        require(files[prefix + ".command"]["data"] == command["command"], f"{name}: CPIO command bytes")
        argv = ["/trace/busybox", "env", "-i", *[f"{key}={value}" for key, value in environment.items()],
                *trace["environment"]["interpreter"], command["command"].decode()]
        expected = ("#!/trace/busybox sh\ncd " + shlex.quote(trace["environment"]["cwd"]) +
                    " || exit 125\nexec " + shlex.join(argv) + "\n").encode()
        require(files[prefix + ".sh"]["mode"] & 0o170000 == 0o100000 and
                files[prefix + ".sh"]["mode"] & 0o111 and files[prefix + ".sh"]["data"] == expected,
                f"{name}/{command['index']}: actual executable wrapper differs from original trace")
    expected_meta = f"{len(trace['commands'])} {trace['environment'].get('timeout', 60)}\n".encode()
    require(files["lab/trace/meta"]["data"] == expected_meta, f"{name}: prepared command count/timeout")
    return len(trace["commands"])


def events(path):
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith('{"event":'):
            item = json.loads(line)
            if item["event"].startswith("trace_"):
                rows.append(item)
    return rows


def block_stats(data):
    values = [int(value) for value in data.split()]
    require(len(values) >= 11 and min(values) >= 0, "invalid guest block statistics")
    return dict(zip(("read_ios", "read_sectors", "write_ios", "write_sectors"),
                    (values[0], values[2], values[4], values[6])))


def host_counters(directory):
    nfs = (directory / "nfs.txt").read_text().splitlines()
    read = [int(line.split()[8]) for line in nfs if line.startswith("proc3 ")]
    require(len(read) == 1, "missing/duplicate NFS proc3 counters")
    mount = []
    active = False
    for line in (directory / "mountstats.txt").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == "device":
            active = " mounted on /remote " in line
        elif active and fields and fields[0] == "READ:":
            mount.append([int(fields[i]) for i in (1, 4, 5)])
    require(len(mount) == 1, "missing/duplicate /remote NFS READ counters")
    values = [int((directory / (name + ".txt")).read_text()) for name in COUNTERS[:2]]
    values += read + mount[0]
    require(min(values) >= 0, "negative absolute host counters")
    return dict(zip(COUNTERS, values))


def subtract(before, after):
    require(before.keys() == after.keys(), "counter fields changed")
    result = {key: after[key] - value for key, value in before.items()}
    require(min(result.values()) >= 0, "counter moved backwards")
    return result


def close(actual, expected, name):
    require(math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-6),
            f"{name}: {actual} != {expected}")


def test_summary(command, output):
    """Keep test outcomes/counts while dropping duration and scheduling noise."""
    command = command.decode().strip()
    output = output.decode(errors="replace")
    if command.startswith("go test "):
        packages = sorted(re.findall(r"^(ok|\?|FAIL)[ \t]+([^ \t\r\n]+)(?:[ \t].*)?$", output, re.M))
        require(bool(packages), "Go test command produced no package results")
        return {"kind": "go", "packages": packages}
    if command.startswith("vendor/bin/phpunit ") and "--version" not in shlex.split(command)[1:]:
        counts = re.findall(r"^Tests: ([0-9]+), Assertions: ([0-9]+)", output, re.M)
        if not counts:
            counts = re.findall(r"^OK \(([0-9]+) tests?, ([0-9]+) assertions?\)", output, re.M)
        require(len(counts) == 1, "PHPUnit command produced no unique test/assertion summary")
        return {"kind": "phpunit", "tests": int(counts[0][0]), "assertions": int(counts[0][1])}
    if command.startswith("python3 tests/run-tests.py "):
        performed = re.findall(r"^([0-9]+) tests performed \(([0-9]+) individual testcases\)$", output, re.M)
        passed = re.findall(r"^([0-9]+) tests passed$", output, re.M)
        require(len(performed) == len(passed) == 1, "MicroPython command produced no unique test summary")
        result = {"kind": "micropython", "tests": int(performed[0][0]),
                  "testcases": int(performed[0][1]), "passed": int(passed[0])}
        for status in ("skipped", "failed"):
            matches = re.findall(rf"^([0-9]+) tests {status}: ([^\r\n]+)$", output, re.M)
            require(len(matches) <= 1, f"duplicate MicroPython {status} summary")
            result[status] = {"count": int(matches[0][0]), "names": sorted(matches[0][1].split())} if matches else {"count": 0, "names": []}
            require(result[status]["count"] == len(result[status]["names"]), f"MicroPython {status} count/names disagree")
        require(result["passed"] + result["failed"]["count"] == result["tests"], "MicroPython test counts disagree")
        return result
    return None


def audit_trial(trial, plan, trace, directory, memory_mib=None, state_transport=None):
    name = trial["id"]
    transport = state_transport or ("dax-mailbox" if "mailbox_nonce" in trial else "serial")
    require(trial["result"] == "passed", f"{name}: trial failed")
    require(trial["trace_sha256"] == trace["sha256"] == plan["trace_sha256"], f"{name}: trace hash")
    require(trial["image_sha256"] == plan["image_sha256"], f"{name}: image hash")
    if "backend_mount_file" in trial:
        mounts = [line.split() for line in inside(directory, trial["backend_mount_file"]).read_text().splitlines()]
        disk = [row for row in mounts if len(row) >= 4 and row[1] == "/disk"]
        require(len(disk) == 1 and "ro" in disk[0][3].split(","), f"{name}: B backing mount is not uniquely readonly")
        if trial["backend"] == "nfs":
            require(disk[0][0] == "172.30.0.1:/storage" and disk[0][2] == "nfs" and
                    "vers=3" in disk[0][3].split(","), f"{name}: B backing is not the home node's NFSv3 mount")
        else:
            require(disk[0][0] == "/dev/vda" and disk[0][2] == "ext4", f"{name}: B backing is not the local ext4 device")
    require(plan["environment"] == trace["environment"], f"{name}: trace environment changed")
    require(plan["source_submission_fingerprint"] == trace["submission"], f"{name}: source submission")
    commands = trace["commands"]
    require(plan["command_count"] == len(commands) == len(plan["commands"]) == len(trial["commands"]),
            f"{name}: command count")
    require([row["index"] for row in trial["commands"]] == list(range(len(commands))), f"{name}: command ordering")
    for source, prepared in zip(commands, plan["commands"]):
        for key in ("index", "command_sha256", "expected_returncode"):
            require(source[key] == prepared[key], f"{name}/{source['index']}: prepared {key}")
        require(prepared["command"].encode() == source["command"], f"{name}: prepared command bytes")
        require(prepared["source_output_sha256"] == (sha(source["source_output"]) if source["source_output"] is not None else None),
                f"{name}: prepared original output checksum")
    archive = inside(directory, trial["guest_archive"])
    require(digest(archive) == trial["guest_archive_sha256"], f"{name}: guest archive checksum")
    files = archive_files(archive)
    raw = []
    serial_text = ""
    dax_bytes = memory_mib * 1024 * 1024 if memory_mib is not None else None
    snapshot_paths = []
    for log in trial["logs"]:
        path = inside(directory, log["path"])
        require(digest(path) == log["integrity"]["sha256"] and path.stat().st_size == log["integrity"]["bytes"],
                f"{name}: serial log integrity")
        require(log["integrity"]["source_unchanged_during_fetch"], f"{name}: changing serial log during fetch")
        raw.extend(events(path))
        text = path.read_text(errors="replace")
        serial_text += text
        mappings = re.findall(r" DAX_RAM path=(\S+) offset=([0-9]+) gpa=([0-9]+) bytes=([0-9]+) initialize=(true|false) hva=0x[0-9a-f]+", text)
        require(len(mappings) == 1, f"{name}/stage{log['stage']}: missing/duplicate DAX mapping")
        backing, offset, gpa, size, initialize = mappings[0]
        if dax_bytes is None:
            dax_bytes = int(size)
        require(backing == "/dev/dax0.0" and int(offset) == 2097152 and int(gpa) == 0 and
                int(size) == dax_bytes > 0 and initialize == ("true" if log["stage"] == 0 else "false"),
                f"{name}/stage{log['stage']}: guest RAM not retained in the shared DAX mapping")
        requests = []
        for api_path, encoded in re.findall(r'The API server received a Put request on "([^"]+)" with body (".*")\.', text):
            if api_path in ("/snapshot/create", "/snapshot/load"):
                requests.append((api_path, json.loads(json.loads(encoded))))
        expected_requests = ["/snapshot/create"] if log["stage"] == 0 else (
            ["/snapshot/load", "/snapshot/create"] if log["stage"] == 1 else ["/snapshot/load"])
        require([item[0] for item in requests] == expected_requests, f"{name}: snapshot API sequence")
        for api_path, body in requests:
            snapshot_paths.append(body["snapshot_path"])
            if api_path == "/snapshot/create":
                require(body["snapshot_type"] == "Full" and body["mem_file_path"] == "/dev/dax0.0", f"{name}: snapshot copied RAM to another file")
            else:
                require(body["mem_backend"] == {"backend_type": "File", "backend_path": "/dev/dax0.0"} and
                        body["resume_vm"] is False, f"{name}: restore used a different memory backing")
    require(len(set(snapshot_paths)) == 1, f"{name}: snapshot paths differ between source and destination")
    require(not FATAL_KERNEL.search(serial_text), f"{name}: fatal kernel/VMM error in serial log")
    require(not FATAL_KERNEL.search(files["results/dmesg.txt"].decode(errors="replace")),
            f"{name}: fatal guest kernel error")
    export = [row for row in raw if row["event"] == "trace_export"]
    require(len(export) == 1 and export[0]["sha256"] == trial["guest_archive_sha256"] and
            export[0]["bytes"] == archive.stat().st_size, f"{name}: export receipt")
    require(export[0]["label"] == name + "_export", f"{name}: export evidence belongs to another trial")
    token = re.escape(export[0]["label"])
    framed = re.findall(rf"^TRACE_EXPORT_BEGIN {token} ([0-9]+) ([0-9a-f]{{64}})\n(.*?)^TRACE_EXPORT_END {token}$",
                        serial_text.replace("\r", ""), re.M | re.S)
    require(len(framed) == 1, f"{name}: missing/duplicate serial export")
    size, checksum, encoded = framed[0]
    exported_bytes = base64.b64decode("".join(encoded.split()), validate=True)
    require(exported_bytes == archive.read_bytes() and int(size) == len(exported_bytes) and sha(exported_bytes) == checksum,
            f"{name}: serial export differs from saved guest archive")
    require(not [row for row in raw if row["event"] in ("trace_error", "trace_protocol_error")],
            f"{name}: guest protocol error")
    ready = [row for row in raw if row["event"] == "trace_ready"]
    require(len(ready) == 1 and ready[0]["command_count"] == len(commands), f"{name}: guest reboot or missing ready")
    require(trial["logs"][0]["role"] == "A" and
            [item["role"] for item in trial["logs"]] == ["A", "B", "A"] and
            [item["stage"] for item in trial["logs"]] == [0, 1, 2], f"{name}: stage sequence")
    starts = [row for row in raw if row["event"] == "trace_start"]
    ends = [row for row in raw if row["event"] == "trace_end"]
    require([row["index"] for row in starts] == list(range(len(commands))), f"{name}: missing/duplicate/raw out-of-order start")
    require([row["index"] for row in ends] == list(range(len(commands))), f"{name}: missing/duplicate/raw out-of-order end")
    execution = [(row["event"], row["index"]) for row in raw if row["event"] in ("trace_start", "trace_end")]
    require(execution == [(event, i) for i in range(len(commands)) for event in ("trace_start", "trace_end")],
            f"{name}: overlapping or reordered command execution")
    require(len({row["label"] for row in starts}) == len(commands), f"{name}: duplicate command tokens")
    expected_files = {f"results/{i:03d}.{extension}" for i in range(len(commands))
                      for extension in ("command", "out", "json", "block-before", "block-after")}
    actual_files = {key for key in files if key.startswith("results/") and key.split("/")[-1][:3].isdigit()}
    require(actual_files == expected_files, f"{name}: missing/extra command artifacts")
    durations, total_io, counter_differences, test_outcomes = [], collections.Counter(), [], []
    for command, reported, start, end in zip(commands, trial["commands"], starts, ends):
        index = command["index"]
        prefix = f"results/{index:03d}"
        label = f"{name}/{index}"
        require(files[prefix + ".command"] == command["command"], f"{label}: executed command bytes differ")
        require(start["label"] == end["label"], f"{label}: command token changed")
        require(start["label"] == f"{name}_cmd{index}", f"{label}: command evidence belongs to another trial")
        require(start["start_s"] == end["start_s"], f"{label}: start/end timestamp disagreement")
        require(index == 0 or start["start_s"] >= ends[index - 1]["end_s"],
                f"{label}: guest monotonic clock moved backwards between commands or across handoff")
        require(start["command_sha256"] == end["command_sha256"] == reported["command_sha256"] == command["command_sha256"],
                f"{label}: command checksum disagreement")
        require(json.loads(files[prefix + ".json"]) == end, f"{label}: raw archive/serial end disagreement")
        require(all(reported[key] == value for key, value in end.items()), f"{label}: report/raw guest fields differ")
        require(sha(files[prefix + ".out"]) == end["output_sha256"], f"{label}: output checksum")
        if command["source_output"] is not None:
            expected_tests = test_summary(command["command"], command["source_output"])
            if expected_tests is not None:
                actual_tests = test_summary(command["command"], files[prefix + ".out"])
                require(actual_tests == expected_tests, f"{label}: stable test outcomes differ from original trace: {actual_tests} != {expected_tests}")
                test_outcomes.append({"index": index, **actual_tests})
        require(end["next_index"] == index + 1 and end["rc"] == reported["returncode"], f"{label}: completion state")
        require(end["timed_out"] is False, f"{label}: watchdog timeout")
        expected = command["expected_returncode"]
        require(end["rc"] == (0 if expected is None else expected), f"{label}: recorded exit code mismatch")
        duration = float(end["end_s"]) - float(end["start_s"])
        require(math.isfinite(duration) and duration >= 0, f"{label}: invalid command duration")
        close(reported["guest_duration_s"], duration, label + " duration")
        durations.append(duration)
        role = "B" if plan["handoff_before"] <= index < len(commands) - 2 else "A"
        require(reported["role"] == role, f"{label}: execution role differs from handoff schedule")
        io_delta = subtract(block_stats(files[prefix + ".block-before"]), block_stats(files[prefix + ".block-after"]))
        require(io_delta["write_ios"] == io_delta["write_sectors"] == 0, f"{label}: readonly backing received writes")
        if "block_delta" in reported:
            require(reported["block_delta"] == io_delta, f"{label}: block counter delta")
        net_delta = subtract(host_counters(inside(directory, reported["counter_before"])),
                             host_counters(inside(directory, reported["counter_after"])))
        require(net_delta == reported["network_delta"], f"{label}: NFS counter delta")
        if trial["backend"] == "local":
            require(net_delta["nfs_read_calls"] == net_delta["mount_read_ops"] == net_delta["mount_read_received_bytes"] == 0,
                    f"{label}: local backend used NFS READ")
        if net_delta["nfs_read_calls"] != net_delta["mount_read_ops"]:
            counter_differences.append({"trial": name, "command": index,
                                       "started": net_delta["nfs_read_calls"], "completed": net_delta["mount_read_ops"]})
        total_io.update(io_delta)
        total_io.update(net_delta)
    last = files[f"results/{len(commands)-1:03d}.out"].splitlines(keepends=True)
    markers = [index for index, line in enumerate(last) if line.strip() == SUBMIT]
    require(len(markers) == 1, f"{name}: submission marker missing or duplicated")
    submitted = fingerprint(b"".join(line for i, line in enumerate(last) if i != markers[0]))
    # Original Docker capture can place this stderr startup line after stdout;
    # a guest's merged output places it before the patch. Only this exact known
    # warning is removed, and its count must still agree. Preserve both raw
    # hashes so this transport-order difference remains visible in the audit.
    require(all(submitted[key] == trace["submission"][key] for key in
                ("without_startup_warning_sha256", "startup_warning_count")),
            f"{name}: final submission fingerprint mismatch")
    require("results/complete.json" in files, f"{name}: no complete record")
    require(json.loads(files["results/complete.json"]) == {"completed_commands": len(commands)},
            f"{name}: incomplete guest replay")
    require(files["results/state"] == f"{ready[0]['boot_id']} {len(commands)} {ends[-1]['output_sha256']}\n".encode(),
            f"{name}: final mutable sentinel")
    mounts = [line.split() for line in files["results/guest-mounts.txt"].decode().splitlines()]
    lower = [row for row in mounts if row[1] == "/lower"]
    ram = [row for row in mounts if row[1] == "/ram"]
    overlay = [row for row in mounts if row[1] == "/newroot"]
    require(len(lower) == 1 and lower[0][0] == "/dev/vda" and lower[0][2] == "ext4" and
            "ro" in lower[0][3].split(","), f"{name}: lower disk not readonly ext4")
    require(len(ram) == 1 and ram[0][2] == "tmpfs", f"{name}: writable layer not in guest RAM")
    require(len(overlay) == 1 and overlay[0][2] == "overlay" and
            "upperdir=/ram/upper" in overlay[0][3].split(","), f"{name}: overlay upper mismatch")
    require(not any(row[2] in ("nfs", "nfs4") for row in mounts), f"{name}: unexpected guest NFS mount")
    identities = [row for row in raw if row["event"] == "trace_identity"]
    require([row["fd_ordinal"] for row in identities] == list(range(len(identities))), f"{name}: FD sequence reset/gap")
    for identity in identities:
        require(identity["boot_id"] == ready[0]["boot_id"] and identity["pid"] == ready[0]["pid"],
                f"{name}: guest identity changed")
        ordinal = identity["fd_ordinal"]
        require(identity["fd_deleted"] is True and identity["fd_inode"] > 0, f"{name}: unlinked FD not retained")
        require(identity["fd_inode"] == identities[0]["fd_inode"], f"{name}: open-file inode changed during replay")
        require(identity["fd_line"] == f"FD-{identity['boot_id']}-{ordinal}", f"{name}: FD contents/offset changed")
        position = sum(len(f"FD-{identity['boot_id']}-{i}\n".encode()) for i in range(ordinal + 1))
        require(identity["fd_position"] == position, f"{name}: FD offset continuity")
        completed = identity["next_index"]
        require(0 <= completed <= len(commands), f"{name}: identity next command")
        state = (f"{identity['boot_id']} {completed} {ends[completed - 1]['output_sha256']}\n"
                 if completed else f"{identity['boot_id']} 0\n")
        require(identity["sentinel_sha256"] == sha(state.encode()), f"{name}: mutable file contents not retained")
    require(len(trial["handoffs"]) == 2, f"{name}: handoff count")
    boundaries = [plan["handoff_before"], len(commands) - 2]
    transport_audits = []
    for index, handoff in enumerate(trial["handoffs"]):
        label = f"{name}/handoff{index}"
        require((handoff["source"], handoff["destination"]) == (("A", "B") if index == 0 else ("B", "A")),
                f"{label}: direction")
        before, after = handoff["before_identity"], handoff["after_identity"]
        require(before["label"] == f"{name}_h{index}_before" and after["label"] == f"{name}_h{index}_after",
                f"{label}: handoff evidence belongs to another trial")
        require(identities.count(before) == identities.count(after) == 1, f"{label}: identity missing/duplicated in raw logs")
        require(before["next_index"] == after["next_index"] == boundaries[index], f"{label}: boundary moved")
        for field in ("boot_id", "pid", "sentinel_sha256", "fd_inode", "fd_deleted"):
            require(before[field] == after[field], f"{label}: {field} continuity")
        require(after["fd_ordinal"] == before["fd_ordinal"] + 1, f"{label}: FD did not continue")
        require(handoff["ram_transfer_bytes"] == 0, f"{label}: RAM copied")
        state = inside(directory, handoff["state_file"])
        require(0 < state.stat().st_size == handoff["state_bytes"] == handoff["state_integrity"]["bytes"] < 4 * 1024 * 1024,
                f"{label}: state length")
        require(digest(state) == handoff["state_integrity"]["sha256"], f"{label}: state checksum")
        require(handoff["state_integrity"]["source_unchanged_during_fetch"], f"{label}: unstable state fetch")
        transport_audits.append(audit_state_transport(handoff, trial, index, directory, dax_bytes, transport))
        require(handoff["source_fenced"] is True, f"{label}: source not fenced")
        fence = inside(directory, handoff["fence_file"]).read_bytes().decode()
        require(fence == handoff["source_fence"], f"{label}: source fence raw/report disagreement")
        fence = fence.replace("\r", "")
        receipt = re.findall(r"^FENCED ([0-9]+) ([0-9]+)$", fence, re.M)
        require(len(receipt) == 1 and int(receipt[0][0]) > 1 and int(receipt[0][1]) > 0,
                f"{label}: missing owned-process fence receipt")
        states = re.findall(r"^FENCE_STATE (missing|reused|Z|X) ([0-9]+)$", fence, re.M)
        require(len(states) == 1, f"{label}: missing incarnation-aware fence state")
        reason, observed = states[0][0], int(states[0][1])
        expected_start = int(receipt[0][1])
        require((reason == "missing" and observed == 0) or
                (reason in ("Z", "X") and observed == expected_start) or
                (reason == "reused" and observed > 0 and observed != expected_start),
                f"{label}: source process could still execute")
    evictions = [row for row in raw if row["event"] == "trace_evicted"]
    if trial["cache_mode"] == "evicted":
        require([row["next_index"] for row in evictions] == boundaries and evictions == trial["evictions"],
                f"{name}: missing/incorrect raw guest cache-drop records")
        require([row["label"] for row in evictions] == [f"{name}_evict1", f"{name}_evict2"], f"{name}: cache-drop trial tokens")
        require(all(row["drop_caches"] == 3 and row["cached_after_kib"] >= 0 and row["cached_before_kib"] >= 0
                    for row in evictions), f"{name}: guest cache-drop evidence")
    else:
        require(not evictions, f"{name}: retained trial evicted guest caches")
    for role in ("A", "B"):
        rows = []
        for line in (directory / f"{name}-{role}-cache.txt").read_text().splitlines():
            if line.startswith('{"event":"resident"'):
                rows.append(json.loads(line))
        require(len(rows) == 1 and rows[0]["offset"] == 0 and rows[0]["bytes"] == plan["image_bytes"] and
                rows[0]["resident_before"] == rows[0]["resident_after"] == trial["host_cache_before"][role] == 0,
                f"{name}: {role} initial host image cache was not empty")
    if "guest_duration_s" in trial:
        close(trial["guest_duration_s"], sum(durations), name + " total duration")
    destination_duration = sum(duration for index, duration in enumerate(durations)
                               if plan["handoff_before"] <= index < len(commands) - 2)
    close(trial["destination_duration_s"], destination_duration, name + " destination duration")
    require(trial["nfs_read_calls"] == total_io["nfs_read_calls"] and
            trial["nfs_received_bytes"] == total_io["mount_read_received_bytes"], f"{name}: total NFS accounting")
    trial_net, outside_net = None, None
    if "trial_counter_before" in trial or "trial_counter_after" in trial:
        trial_net = subtract(host_counters(inside(directory, trial["trial_counter_before"])),
                             host_counters(inside(directory, trial["trial_counter_after"])))
        require(trial_net == trial["trial_network_delta"], f"{name}: whole-trial NFS counters")
        outside_net = subtract({key: total_io[key] for key in COUNTERS}, trial_net)
        require(outside_net == trial["outside_command_network_delta"], f"{name}: traffic outside command windows")
        if trial["backend"] == "local":
            require(trial_net["nfs_read_calls"] == trial_net["mount_read_ops"] == trial_net["mount_read_received_bytes"] == 0,
                    f"{name}: local trial used NFS READ outside command windows")
    return {"trial": name, "boot_id": ready[0]["boot_id"], "commands": len(commands), "recorded_exits_checked": len(commands) - 1,
            "final_exit_checked": 1, "guest_duration_s": sum(durations), "io": dict(total_io),
            "destination_duration_s": destination_duration,
            "trial_network_delta": trial_net, "outside_command_network_delta": outside_net,
            "test_outcomes": test_outcomes,
            "submission": {"actual": submitted, "source": trace["submission"],
                           "raw_bytes_match": submitted["sha256"] == trace["submission"]["sha256"],
                           "patch_bytes_match": True},
            "handoffs": len(trial["handoffs"]), "identities": len(identities),
            "state_transport": transport, "native_handoff_phases": transport_audits,
            "rpc_counter_differences": counter_differences}


def audit(args):
    report = json.loads(args.report.read_text())
    directory = args.evidence_dir or args.report.parent
    manifest = json.loads((args.manifest or directory / "manifest.json").read_text())
    require(report["schema_version"] == 1, "unsupported report schema")
    require(report["result"] == "passed", f"experiment result: {report['result']}: {report.get('error')}")
    require(not report.get("cleanup_errors"), "experiment cleanup failed")
    require(report["config"]["prepared"] == manifest, "report/prepared manifest disagreement")
    trials = report["trials"]
    require(bool(trials) and len({row["id"] for row in trials}) == len(trials), "missing/duplicate trials")
    arguments = report["arguments"]
    state_transport = arguments.get("state_transport", "serial")
    require(state_transport in ("serial", "dax-mailbox"), "unsupported state transport")
    if state_transport == "dax-mailbox":
        require(len({row["mailbox_nonce"] for row in trials}) == len(trials), "mailbox session nonce reused across trials")
    else:
        require(all("mailbox_nonce" not in row for row in trials), "serial trial contains mailbox session")
    config = report["config"]
    require(config["dax_guest"] and not config["dax_kvm_state"] and not config["dax_handoff"],
            "experiment did not use ordinary shared-DAX RAM")
    require(arguments["memory_mib"] + 2 <= config["cxl_mib"], "guest RAM exceeds shared CXL backing")
    for role in ("A", "B"):
        argv = config["vms"][role]["command"]
        backings = [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == "-object" and
                    "id=cxl-shared," in argv[i + 1]]
        require(len(backings) == 1 and backings[0].startswith("memory-backend-file,"), f"{role}: missing CXL file backend")
        options = dict(item.split("=", 1) for item in backings[0].split(",")[1:])
        require(options["share"] == "on" and options["mem-path"] == config["cxl_backing"] and
                options["size"] == f"{config['cxl_mib']}M", f"{role}: QEMU did not map the same shared CXL file")
        devices = [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == "-device" and
                   argv[i + 1].startswith("cxl-type3,")]
        require(len(devices) == 1 and "volatile-memdev=cxl-shared" in devices[0].split(","),
                f"{role}: shared backing not connected to CXL device")
    names = arguments["names"]
    require(bool(names) and len(set(names)) == len(names), "missing/duplicate trace names")
    cycles = {"retained": arguments["cycles"], "evicted": arguments["eviction_cycles"]}
    require(all(isinstance(n, int) and n >= 0 for n in cycles.values()), "invalid cycle counts")
    if args.formal:
        require(cycles == {"retained": 3, "evicted": 1} and len(names) == 3, "formal matrix must have 3 traces,3 retained cycles,1 evicted cycle")
        require(all(all(key in trial for key in ("trial_counter_before", "trial_counter_after", "trial_network_delta",
                                                "outside_command_network_delta", "backend_mount_file")) for trial in trials),
                "formal run requires whole-trial traffic accounting and raw B backing mounts")
        cpu_sets = {}
        for role in ("A", "B"):
            specification = arguments["host_cpus_" + role.lower()]
            require(isinstance(specification, str) and bool(specification), f"{role}: missing formal host CPU affinity")
            cpus = set()
            for part in specification.split(","):
                ends = [int(value) for value in part.split("-")]
                require(len(ends) in (1, 2) and min(ends) >= 0 and ends[0] <= ends[-1], "invalid host CPU specification")
                cpus.update(range(ends[0], ends[-1] + 1))
            cpu_sets[role] = cpus
            actual = report["host_cpu_affinity"][role]
            require(bool(actual) and all(set(values) == cpus for values in actual.values()), f"{role}: QEMU thread affinity drift")
            require(set(report["vhost_cpu_affinity"][role]["cpus"]) == cpus, f"{role}: vhost affinity drift")
        require(not cpu_sets["A"] & cpu_sets["B"], "formal A/B CPU allocations overlap")
    expected = {(name, mode, cycle, backend) for name in names for mode, count in cycles.items()
                for cycle in range(count) for backend in ("local", "nfs")}
    keys = [(row["trace"], row["cache_mode"], row["cycle"], row["backend"]) for row in trials]
    require(len(set(keys)) == len(keys) and set(keys) == expected, "missing/duplicate/unexpected experimental cells")
    sources = args.source_dir or directory / "measured-sources"
    runtime = report["runtime_sources"]
    require(bool(runtime), "no runtime source provenance")
    for name, expected_hash in runtime.items():
        require(digest(inside(sources, name)) == expected_hash, f"runtime source drift: {name}")
    for name in ("trace-l2-init.sh", "trace-guest-run.sh"):
        require(manifest["source_hashes"][name] == runtime[name], f"guest compiled/prepared source differs at runtime: {name}")
    helper_sources = []
    if state_transport == "dax-mailbox":
        for key, name in (("probe_source", "shared_probe.c"), ("cache_probe_source", "cache_probe.h"),
                          ("mptcp_probe_source", "mptcp_probe.h")):
            require(manifest["inputs"][key]["sha256"] == runtime[name], f"native helper prepared/runtime source drift: {name}")
            helper_sources.append(name)
        require(manifest["source_hashes"]["shared_probe.c"] == runtime["shared_probe.c"], "native helper source provenance differs")
        require(manifest["inputs"]["shared_probe"]["sha256"] == manifest["artifacts"]["shared-probe"],
                "native helper input/artifact binary differs")
    patch_dir = args.patch_dir or directory / "measured-patches"
    patches_checked = []
    if args.patch_dir or patch_dir.is_dir():
        require(bool(config["source_patches"]), "missing DAX implementation patch provenance")
        for name, expected_hash in config["source_patches"].items():
            require(digest(inside(patch_dir, name)) == expected_hash, f"DAX implementation patch drift: {name}")
            patches_checked.append(name)
    parsed = {name: original_trace(directory / "traces" / (name + ".traj.json")) for name in names}
    for name in names:
        effective_environment(manifest["traces"][name], parsed[name])
    audits = [audit_trial(row, manifest["traces"][row["trace"]], parsed[row["trace"]], directory,
                          memory_mib=arguments["memory_mib"], state_transport=state_transport) for row in trials]
    require(len({row["boot_id"] for row in audits}) == len(trials), "independent trials reused a guest boot identity")
    for name in names:
        remote = [audit for row, audit in zip(trials, audits) if row["trace"] == name and
                  row["cache_mode"] == "evicted" and row["backend"] == "nfs"]
        if cycles["evicted"]:
            require(sum(audit["io"]["nfs_read_calls"] for audit in remote) > 0 and
                    sum(audit["io"]["mount_read_ops"] for audit in remote) > 0,
                    f"{name}: evicted NFS cells did not exercise remote reads")
    checked_artifacts = []
    wrappers_checked = 0
    if args.artifacts:
        asset_dir = args.assets_dir or Path(report["config"]["assets"])
        for name, expected_hash in manifest["artifacts"].items():
            require(digest(inside(asset_dir, name)) == expected_hash, f"prepared artifact hash: {name}")
            checked_artifacts.append(name)
        wanted = {f"lab/{name}-initramfs.cpio" for name in names}
        if state_transport == "dax-mailbox":
            wanted.add("lab/shared-probe")
        l1_files = newc_files(asset_dir / "l1-initramfs.cpio", wanted)
        if state_transport == "dax-mailbox":
            helper = l1_files["lab/shared-probe"]
            require(helper["mode"] & 0o170000 == 0o100000 and helper["mode"] & 0o111 and
                    helper["data"] == (asset_dir / "shared-probe").read_bytes() and
                    sha(helper["data"]) == manifest["artifacts"]["shared-probe"],
                    "L1 embedded native helper differs from audited executable artifact")
            checked_artifacts.append("l1-initramfs.cpio:lab/shared-probe")
        for name in names:
            plan = manifest["traces"][name]
            image = Path(plan["source_image"])
            require(image.stat().st_size == plan["image_bytes"] and digest(image) == plan["image_sha256"],
                    f"borrowed source image changed: {name}")
            checked_artifacts.append(plan["source_image"])
            wrappers_checked += prepared_commands(asset_dir, plan, parsed[name], sources, l1_files)
    groups = collections.defaultdict(list)
    for trial, checked in zip(trials, audits):
        groups[trial["trace"], trial["cache_mode"], trial["backend"]].append(checked["guest_duration_s"])
    summary = [{"trace": key[0], "cache_mode": key[1], "backend": key[2], "n": len(values),
                "guest_duration_s": {"median": statistics.median(values), "min": min(values), "max": max(values)}}
               for key, values in sorted(groups.items())]
    claimed = {(row["trace"], row["cache_mode"], row["backend"]): row for row in report["summary"]}
    require(len(claimed) == len(report["summary"]) and set(claimed) == set(groups), "summary missing/duplicate groups")
    for key, claim in claimed.items():
        own = [checked for trial, checked in zip(trials, audits)
               if (trial["trace"], trial["cache_mode"], trial["backend"]) == key]
        require(claim["n"] == len(own), f"{key}: summary count")
        for metric in ("guest_duration_s", "destination_duration_s", "nfs_read_calls", "nfs_received_bytes"):
            values = [row[metric] if metric.endswith("duration_s") else
                      row["io"]["nfs_read_calls" if metric == "nfs_read_calls" else "mount_read_received_bytes"] for row in own]
            for statistic, expected_value in (("median", statistics.median(values)), ("min", min(values)), ("max", max(values))):
                close(claim[metric][statistic], expected_value, f"{key}/{metric}/{statistic}")
    result = {"result": "passed", "schema_version": 1, "trials": len(trials),
              "commands": sum(row["commands"] for row in audits),
              "recorded_exits_checked": sum(row["recorded_exits_checked"] for row in audits),
              "submission_exits_checked": len(trials), "submissions_checked": len(trials),
              "handoffs": sum(row["handoffs"] for row in audits),
              "state_transport": state_transport,
              "mailbox_handoffs_checked": sum(phase["transport"] == "dax-mailbox" for row in audits for phase in row["native_handoff_phases"]),
              "native_mailbox_phases_checked": sum(len(phase["mailbox_phases"]) for row in audits for phase in row["native_handoff_phases"]),
              "native_api_phases_checked": sum(len(phase["api_phases"]) for row in audits for phase in row["native_handoff_phases"]),
              "native_timing_note": "Each native phase uses its own node's monotonic clock; phase sums are not end-to-end downtime.",
              "fresh_guest_boot_ids": len({row["boot_id"] for row in audits}),
              "stable_test_summaries_checked": sum(len(row["test_outcomes"]) for row in audits),
              "submission_normalization": {"removed_exact_line": WARNING.decode(), "require_same_warning_count": True,
                  "raw_hash_difference_trials": [row["trial"] for row in audits if not row["submission"]["raw_bytes_match"]]},
              "runtime_sources_checked": sorted(runtime), "artifact_files_checked": checked_artifacts,
              "native_helper_sources_checked": helper_sources,
              "source_patches_checked": sorted(patches_checked),
              "prepared_executable_wrappers_checked": wrappers_checked,
              "rpc_counter_differences": [item for row in audits for item in row["rpc_counter_differences"]],
              "rpc_counter_note": "proc3 counts started RPCs, mountstats counts completed RPCs; snapshots are sequential.",
              "summary": summary, "trial_audits": audits}
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(text)
    print(text, end="")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--patch-dir", type=Path, help="Exact DAX implementation patches; automatically checks evidence/measured-patches when present")
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--artifacts", action="store_true")
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--json-out", type=Path)
    return audit(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
