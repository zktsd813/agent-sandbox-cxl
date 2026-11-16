#!/usr/bin/env python3
"""Independently audit exact SWE command results and VM-private RAM handoff.

No runner/controller imports. Receipts, serial frames, source traces and final
submission are checked independently, including process/FD continuity.
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
import tarfile

from experiment_sources import verify_sources

WARNING = b"/root/.bashrc: line 1: /opt/miniconda3/etc/profile.d/conda.sh: No such file or directory\n"
SUBMIT = b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
FATAL_KERNEL = re.compile(r"Kernel panic|Out of memory:|oom-kill:|Killed process [0-9]+|"
                          r"Buffer I/O error|EXT4-fs error|I/O error, dev vda|"
                          r"blk_update_request: I/O error|\bBUG:|\bkernel BUG at|\bOops:", re.I)


PAGE = 4096
MIB = 1024**2
PRIVATE_START, PRIVATE_END = 2*MIB, 3072*MIB
TRACE_NAMES = {"gin-gonic__gin-3741", "php-cs-fixer__php-cs-fixer-8331", "micropython__micropython-13039"}
MODES = {"baseline", "state-only", "direct"}

def need(ok, message):
    if not ok:
        raise ValueError(message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def fingerprint(data):
    return {"sha256": sha(data), "without_startup_warning_sha256": sha(data.replace(WARNING, b"")),
            "startup_warning_count": data.count(WARNING)}

def original_trace(path):
    """Parse source bytes directly instead of importing the replay implementation."""
    raw = path.read_bytes()
    source = json.loads(raw)
    config = source["info"]["config"]
    need(config["environment_type"].endswith(".DockerEnvironment"), "unsupported trace environment")
    need(not config["environment"].get("forward_env"), "host environment forwarding")
    need(source["info"]["exit_status"] == "Submitted", "source trace did not submit")
    batches = []
    for message in source["messages"]:
        extra = message.get("extra", {})
        if message["role"] == "assistant" and extra.get("actions"):
            batches.append([extra["actions"], []])
        elif batches and message["role"] in ("tool", "user") and "returncode" in extra:
            batches[-1][1].append(extra)
    need(bool(batches), "trace has no actions")
    commands = []
    for number, (actions, observations) in enumerate(batches):
        need(len(actions) == len(observations) or (number == len(batches) - 1 and not observations),
                f"incomplete recorded observations in batch {number}")
        for index, action in enumerate(actions):
            need(isinstance(action["command"], str), "non-text trace command")
            data = action["command"].encode()
            commands.append({"index": len(commands), "command": data,
                             "command_sha256": sha(data),
                             "expected_returncode": observations[index]["returncode"] if observations else None,
                             "source_output": observations[index]["raw_output"].encode() if observations else None})
    need(sum(c["expected_returncode"] is None for c in commands) == 1 and
            commands[-1]["expected_returncode"] is None, "only final submission may lack recorded return code")
    return {"sha256": sha(raw), "commands": commands, "environment": config["environment"],
            "submission": fingerprint(source["info"]["submission"].encode())}

def archive_files(path):
    # Never extract guest-controlled paths to the host filesystem.
    files = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            need(not name.is_absolute() and ".." not in name.parts, f"unsafe archive member {member.name}")
            if member.isdir():
                continue
            need(member.isfile(), f"unexpected archive member type: {member.name}")
            key = str(name)
            need(key not in files, f"duplicate archive member: {key}")
            files[key] = archive.extractfile(member).read()
    return files

def block_stats(data):
    values = [int(value) for value in data.split()]
    need(len(values) >= 11 and min(values) >= 0, "invalid guest block statistics")
    return dict(zip(("read_ios", "read_sectors", "write_ios", "write_sectors"),
                    (values[0], values[2], values[4], values[6])))

def test_summary(command, output):
    """Keep test outcomes/counts while dropping duration and scheduling noise."""
    command = command.decode().strip()
    output = output.decode(errors="replace")
    if command.startswith("go test "):
        packages = sorted(re.findall(r"^(ok|\?|FAIL)[ \t]+([^ \t\r\n]+)(?:[ \t].*)?$", output, re.M))
        need(bool(packages), "Go test command produced no package results")
        return {"kind": "go", "packages": packages}
    if command.startswith("vendor/bin/phpunit ") and "--version" not in shlex.split(command)[1:]:
        counts = re.findall(r"^Tests: ([0-9]+), Assertions: ([0-9]+)", output, re.M)
        if not counts:
            counts = re.findall(r"^OK \(([0-9]+) tests?, ([0-9]+) assertions?\)", output, re.M)
        need(len(counts) == 1, "PHPUnit command produced no unique test/assertion summary")
        return {"kind": "phpunit", "tests": int(counts[0][0]), "assertions": int(counts[0][1])}
    if command.startswith("python3 tests/run-tests.py "):
        performed = re.findall(r"^([0-9]+) tests performed \(([0-9]+) individual testcases\)$", output, re.M)
        passed = re.findall(r"^([0-9]+) tests passed$", output, re.M)
        need(len(performed) == len(passed) == 1, "MicroPython command produced no unique test summary")
        result = {"kind": "micropython", "tests": int(performed[0][0]),
                  "testcases": int(performed[0][1]), "passed": int(passed[0])}
        for status in ("skipped", "failed"):
            matches = re.findall(rf"^([0-9]+) tests {status}: ([^\r\n]+)$", output, re.M)
            need(len(matches) <= 1, f"duplicate MicroPython {status} summary")
            result[status] = {"count": int(matches[0][0]), "names": sorted(matches[0][1].split())} if matches else {"count": 0, "names": []}
            need(result[status]["count"] == len(result[status]["names"]), f"MicroPython {status} count/names disagree")
        need(result["passed"] + result["failed"]["count"] == result["tests"], "MicroPython test counts disagree")
        return result
    return None

def document_sha(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

def execution_identity(report):
    """Compare measured execution inputs while allowing audit-only revisions.

    Trial measured-sources contains execution code, not this offline verifier.
    Mode-specific handoff flags and each trace's immutable image are compared
    separately, so they do not hide a changed kernel, controller or VM size.
    """
    fixture, config, plan = report["fixture"], report["config"], report["trace"]
    common = {"source_hashes": report["source_hashes"],
        "runtime_inputs": {key: fixture["inputs"][key]["sha256"] for key in ("firecracker", "l1_kernel", "l2_kernel")},
        "runtime_artifacts": {key: fixture["artifacts"][key] for key in
                              ("firecracker", "l1-kernel", "l2-vmlinux", "pool_memory_module.ko")},
        "host_modules": fixture["inputs"]["kvm_modules"],
        "configuration": {key: config[key] for key in ("host_kernel", "qemu_sha256", "l1_vcpus", "l1_memory_mib",
                                                       "shared_mib", "cxl_mib", "dax_guest", "dax_4k")},
        "storage_template_sha256": config["home_storage_image"]["template_sha256"],
        "memory_mib": report["arguments"]["memory_mib"], "vcpus": report["arguments"].get("vcpus",1),
        "restore_timing": report["arguments"].get("restore_timing", False)}
    workload = {key: plan[key] for key in ("name", "trace_sha256", "image_sha256", "image_bytes", "command_count",
        "commands", "handoff_before", "environment", "effective_environment")}
    workload["initramfs_sha256"] = fixture["artifacts"][plan["name"] + "-initramfs.cpio"]
    return {"execution_inputs_sha256": document_sha(common), "workload_inputs_sha256": document_sha(workload)}

def local(root, name):
    need(isinstance(name, str) and not Path(name).is_absolute() and ".." not in Path(name).parts,
         "unsafe artifact path")
    return root / name

def guest(e, event, kind=None):
    expected = event["receipt"]
    rows = [json.loads(line) for line in e.text(event["file"]).splitlines()
            if line.startswith('{"event":')]
    matches = [r for r in rows if r.get("event") == expected["event"] and r.get("label") == expected.get("label")]
    need(matches == [expected] and (kind is None or expected["event"] == kind), "altered guest event")
    return expected

def identity_pair(e, before, after):
    a, b = guest(e, before, "trace_identity"), guest(e, after, "trace_identity")
    for key in ("boot_id", "pid", "next_index", "sentinel_sha256", "fd_inode", "fd_deleted"):
        need(a[key] == b[key], "handoff changed guest " + key)
    need(a["fd_deleted"] is True and b["fd_ordinal"] == a["fd_ordinal"] + 1
         and b["fd_position"] > a["fd_position"], "unlinked FD continuity missing")
    return a, b

def serial_proof(e, report):
    """Bind extracted receipts to complete logs from each fenced VMM."""
    logs = {}
    for cleanup in report["cleanup"]:
        following = [(e.order[name], name) for name, item in e.items.items()
                     if item["kind"] == "final-guest-log" and e.order[name] > e.order[cleanup["file"]]]
        need(following, "missing final guest serial log")
        _, name = min(following)
        need(name not in logs.values(), "reused final guest log")
        logs[cleanup["directory"]] = name
    text = "\n".join(e.text(logs[launch["directory"]]).replace("\r", "")
                     for launch in sorted(report["launches"], key=lambda row: row["epoch"]))
    need(not FATAL_KERNEL.search(text) and "TRACE_FATAL" not in text, "fatal guest error in full serial log")
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"event":')]
    if report["mode"] == "direct":
        # Each resident serial file contains disjoint visits. Validate ordering inside
        # every file, then merge using the controller's unique command sequence labels.
        def serial_order(row):
            if row["event"] == "trace_ready": return (-1, 0)
            match = re.fullmatch(r"v1e([0-9]+)c([0-9]+)", row.get("label", ""))
            need(match is not None, "resident serial command label missing")
            return (int(match[2]), 0 if row["event"] == "trace_start" else 1)
        for name in logs.values():
            local = [json.loads(line) for line in e.text(name).splitlines() if line.startswith('{"event":')]
            need([serial_order(r) for r in local] == sorted(serial_order(r) for r in local), "resident serial reordered locally")
        rows.sort(key=serial_order)
    need(not any(row["event"] in ("trace_error", "trace_protocol_error") for row in rows), "guest protocol error")
    ready = [row for row in rows if row["event"] == "trace_ready"]
    need(ready == [guest(e, report["boot"], "trace_ready")], "guest reboot/missing ready event")
    controls = [item["receipt"] for item in report["control_commands"]]
    need([row for row in rows if row["event"] not in ("trace_ready", "trace_start")] == controls,
         "full serial/control transcript differs")
    count = report["trace"]["command_count"]
    need([(row["event"], row["index"]) for row in rows if row["event"] in ("trace_start", "trace_end")] ==
         [(kind, i) for i in range(count) for kind in ("trace_start", "trace_end")], "raw command execution order")
    for end in [row for row in rows if row["event"] == "trace_end"]:
        starts = [row for row in rows if row["event"] == "trace_start" and row["label"] == end.get("run_label", end["label"])]
        need(len(starts) == 1 and all(starts[0][key] == end[key] for key in
             ("index", "command_sha256", "start_s")), "command start/end disagreement")
    export = guest(e, report["guest_export"], "trace_export")
    frames = re.findall(rf"^TRACE_EXPORT_BEGIN {re.escape(export['label'])} ([0-9]+) ([0-9a-f]{{64}})\n(.*?)^TRACE_EXPORT_END {re.escape(export['label'])}$",
                        text, re.M | re.S)
    need(len(frames) == 1, "guest archive serial frame absent/duplicate")
    size, checksum, encoded = frames[0]
    data = base64.b64decode("".join(encoded.split()), validate=True)
    need(data == e.read(report["guest_archive"]) and len(data) == int(size) == export["bytes"]
         and sha(data) == checksum == export["sha256"], "guest archive differs from full serial frame")
    return ready[0]

def nfs_reads(e, name):
    records = [line.split() for line in e.text(name).splitlines() if line.startswith("proc3 ")]
    need(len(records) == 1, "NFS counter missing")
    return int(records[0][8])

def verify_trace(e, report):
    plan, root = report["trace"], e.root
    source = report["source_trace"]
    if isinstance(source, dict):
        source = source["file"]
    trace = original_trace(local(root, source))
    need(trace["sha256"] == plan["trace_sha256"] and trace["environment"] == plan["environment"],
         "source trace/environment changed")
    if isinstance(report["source_trace"], dict):
        need(trace["sha256"] == report["source_trace"]["sha256"], "recorded source trace hash differs")
    archive = local(root, report["guest_archive"])
    need(sha(archive.read_bytes()) == report["guest_archive_sha256"], "guest archive hash")
    files = archive_files(archive)
    ready = serial_proof(e, report)
    commands = [x for x in report["commands"] if x.get("receipt", {}).get("event") == "trace_end"]
    need(len(commands) == plan["command_count"] == len(trace["commands"]), "trace command count")
    expected_files = {f"results/{i:03d}.{extension}" for i in range(len(commands))
                      for extension in ("command", "out", "json", "block-before", "block-after")}
    cpu_files = {key for key in files if re.fullmatch(r"results/[0-9]{3}\.cpu-(before|after|[a-zA-Z0-9_-]+)", key)}
    if cpu_files:
        required_cpu = {f"results/{i:03d}.cpu-{side}" for i in range(len(commands)) for side in ("before", "after")}
        required_cpu.update(f"results/{row['receipt']['index']:03d}.cpu-{row['receipt']['label']}"
                            for row in report['control_commands'] if row.get('operation') == 'running'
                            and row['receipt']['index'] < len(commands))
        need(cpu_files == required_cpu, "missing/extra per-CPU counter snapshots")
        for name in cpu_files:
            rows = [line.split() for line in files[name].decode().splitlines()]
            need([r[0] for r in rows] == [f"cpu{i}" for i in range(report['arguments'].get('vcpus', 1))]
                 and all(len(r) >= 9 and all(v.isdigit() for v in r[1:]) for r in rows), "invalid per-CPU counters")
    need({key for key in files if key.startswith("results/") and key.split("/")[-1][:3].isdigit()} ==
         expected_files | cpu_files, "missing/extra command artifacts")
    raw_ends, tests, read_ios, remote_reads, duration = [], [], 0, 0, 0.0
    for index, (command, event) in enumerate(zip(trace["commands"], commands)):
        end = guest(e, event, "trace_end")
        need(end["index"] == index and end["next_index"] == index + 1
             and end["command_sha256"] == command["command_sha256"], "command order/bytes changed")
        prefix = f"results/{index:03d}"
        need(files[prefix + ".command"] == command["command"] and json.loads(files[prefix + ".json"]) == end,
             "archive command/end differs from raw receipt")
        need(sha(files[prefix + ".out"]) == end["output_sha256"], "command stdout hash")
        expected = command["expected_returncode"]
        need(end["rc"] == (0 if expected is None else expected) and end["timed_out"] is False,
             "recorded command failed/diverged/timed out")
        if command["source_output"] is not None:
            previous = test_summary(command["command"], command["source_output"])
            if previous is not None:
                actual = test_summary(command["command"], files[prefix + ".out"])
                need(actual == previous, "test outcomes differ from original trace")
                tests.append({"index": index, **actual})
        need(end["end_s"] >= end["start_s"] and (not raw_ends or end["start_s"] >= raw_ends[-1]["end_s"]),
             "guest command clock/order")
        elapsed = end["end_s"] - end["start_s"]
        need(math.isfinite(elapsed) and math.isclose(elapsed, event["guest_duration_s"], abs_tol=1e-8), "command duration altered")
        duration += elapsed
        raw_ends.append(end)
        before, after = block_stats(files[prefix + ".block-before"]), block_stats(files[prefix + ".block-after"])
        need(after["write_ios"] == before["write_ios"] and after["write_sectors"] == before["write_sectors"],
             "readonly root image received writes")
        need(after["read_ios"] >= before["read_ios"], "block read counter reversed")
        read_ios += after["read_ios"] - before["read_ios"]
        expected_role = "B" if report["mode"] != "baseline" and plan["handoff_before"] <= index < len(commands) - 2 else "A"
        need(event["role"] == expected_role, "command executed on wrong node")
        need(event["vm"] == 1 and event["tier"] == ("cxl" if expected_role == "B" else "dram"), "command ran on wrong memory tier")
        if "network" in event:
            network = event["network"]
            delta = nfs_reads(e, network["after_file"]) - nfs_reads(e, network["before_file"])
            need(delta == network["nfs_read_delta"] >= 0, "NFS counter delta")
            if expected_role == "B":
                remote_reads += delta
    final = files[f"results/{len(commands) - 1:03d}.out"].splitlines(keepends=True)
    markers = [i for i, line in enumerate(final) if line.strip() == SUBMIT]
    need(len(markers) == 1, "submission marker absent/duplicate")
    submitted = fingerprint(b"".join(line for i, line in enumerate(final) if i != markers[0]))
    need(all(submitted[key] == trace["submission"][key] for key in
             ("without_startup_warning_sha256", "startup_warning_count")), "final submission differs from source trace")
    need(json.loads(files["results/complete.json"]) == {"completed_commands": len(commands)}, "trace incomplete")
    need(not FATAL_KERNEL.search(files["results/dmesg.txt"].decode(errors="replace")), "fatal guest kernel error")
    mounts = [line.split() for line in files["results/guest-mounts.txt"].decode().splitlines()]
    lower = [r for r in mounts if r[1] == "/lower"]
    upper = [r for r in mounts if r[1] == "/ram"]
    overlay = [r for r in mounts if r[1] == "/newroot"]
    need(len(lower) == 1 and lower[0][0] == "/dev/vda" and lower[0][2] == "ext4"
         and "ro" in lower[0][3].split(","), "lower is not readonly ext4")
    need(len(upper) == 1 and upper[0][2] == "tmpfs" and len(overlay) == 1
         and overlay[0][2] == "overlay" and "upperdir=/ram/upper" in overlay[0][3].split(","), "private tmpfs upper absent")
    need(not any(r[2] in ("nfs", "nfs4") for r in mounts), "guest unexpectedly uses NFS")
    identities = []
    for item in report["control_commands"]:
        if item.get("receipt", {}).get("event") == "trace_identity":
            identities.append(guest(e, item, "trace_identity"))
    need(identities, "no guest process/FD identity evidence")
    boot, pid, inode = (identities[0][k] for k in ("boot_id", "pid", "fd_inode"))
    need((boot, pid) == (ready["boot_id"], ready["pid"]) and ready["command_count"] == len(commands), "boot workload identity")
    need([row["fd_ordinal"] for row in identities] == list(range(len(identities))), "FD observation sequence reset/gap")
    for identity in identities:
        need((identity["boot_id"], identity["pid"], identity["fd_inode"]) == (boot, pid, inode)
             and identity["fd_deleted"] is True and inode > 0, "guest process or open FD replaced")
        ordinal = identity["fd_ordinal"]
        need(identity["fd_line"] == f"FD-{boot}-{ordinal}" and identity["fd_position"] ==
             sum(len(f"FD-{boot}-{i}\n".encode()) for i in range(ordinal + 1)), "FD contents/position changed")
        complete = identity["next_index"]
        need(0 <= complete <= len(commands), "identity progress range")
        state = f"{boot} {complete} {raw_ends[complete - 1]['output_sha256']}\n" if complete else f"{boot} 0\n"
        need(identity["sentinel_sha256"] == sha(state.encode()), "mutable trace state changed")
    need(files["results/state"] == f"{boot} {len(commands)} {raw_ends[-1]['output_sha256']}\n".encode(), "final guest state")
    need(math.isclose(duration, report["guest_duration_s"], abs_tol=1e-8), "aggregate command duration changed")
    return {"commands": len(commands), "returncodes": [r["rc"] for r in raw_ends], "submission": submitted,
            "tests": tests, "guest_command_seconds": duration, "block_read_ios": read_ios,
            "remote_nfs_reads": remote_reads, "boot_id": boot, "identity_observations": len(identities)}

def integer(value, name, limit=(1 << 64) - 1):
    need(type(value) is int and 0 <= value <= limit, "invalid integer " + name)
    return value

def ranges(items):
    out = set()
    for item in items:
        start, count = integer(item["offset"], "range offset"), integer(item["pages"], "range pages")
        need(start % PAGE == 0 and 0 < count <= 1 << 20, "bad allocation range")
        new = set(range(start, start + count * PAGE, PAGE))
        need(not out & new, "duplicate allocation ranges")
        out.update(new)
    return out

class Evidence:
    def __init__(self, report, root):
        self.report, self.root, self.items, self.order = report, Path(root), {}, {}
        for index, entry in enumerate(report["receipts"]):
            name = entry["file"]
            need(Path(name).name == name and name not in self.items, "duplicate/unsafe receipt")
            self.items[name], self.order[name] = entry, index
            self.read(name)

    def read(self, name):
        need(name in self.items, "unregistered evidence " + name)
        data = (self.root / name).read_bytes()
        need(sha(data) == self.items[name]["sha256"], "receipt hash mismatch " + name)
        return data

    def text(self, name):
        return self.read(name).decode()

    def json(self, name):
        return json.loads(self.read(name))

    def rows(self, name, prefix):
        return [json.loads(line.split(prefix, 1)[1]) for line in self.text(name).splitlines()
                if prefix in line and not line.lstrip().startswith(">")]

    def record(self, name, prefix):
        rows = self.rows(name, prefix)
        need(len(rows) == 1, "missing/ambiguous raw record " + name)
        return rows[0]


def handle(raw, memory):
    need(raw['version'] == 4 and not raw.get('mappings') and not raw.get('credits'), 'full mapping in normal export')
    h = raw['handle']
    need(h['abi'] == 4 and h['count']*PAGE == memory and h['state'] in (1, 2)
         and h['owner_epoch'] == int(raw['run_epoch'])
         and h['allocation_generation'] == raw['allocation_generation'] > 0
         and h['phys_base'] == raw['phys_base'] and h['device_bytes'] == raw['device_bytes']
         and h['exhausted'] == 0 and h['reserved'] == [0, 0, 0, 0], 'invalid ownership handle')
    return h


def placement(e, name, memory, requested):
    value = e.json(name)
    h = handle(value, memory)
    private = set()
    mappings = requested['mappings']
    need(len(mappings) == memory // PAGE and requested['allocation_generation'] == h['allocation_generation'],
         'promotion coverage/lease')
    for i, page in enumerate(mappings):
        offset = integer(page['offset'], 'mapping offset')
        need(set(page) == {'gpa', 'offset'} and page['gpa'] == i*PAGE
             and offset % PAGE == 0 and PRIVATE_START <= offset < PRIVATE_END
             and offset not in private, 'promotion alias/geometry')
        private.add(offset)
    return value, private


def kernel_intervals(text):
    """Reparse raw ftrace independently of the runtime probe collector."""
    need(not any(int(n) for n in re.findall(r'^overrun:\s*(\d+)', text, re.M)), 'Kernel trace overflow')
    stacks, rows, epoch = {}, [], None
    names = {'arch', 'ept', 'vmx', 'pit', 'lapic', 'nested_root', 'tlb_current', 'tlb_all', 'tlb_guest'}
    for line in text.splitlines():
        match = re.match(r'^.*?-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+)\.(\d+):\s+(\w+):\s*(.*)$', line.strip())
        if match is None:
            continue
        pid, cpu, seconds, fraction, event, fields = match.groups()
        stamp = int(seconds)*10**9 + int((fraction+'0'*9)[:9])
        marker = re.search(r'RESIDENT_KERNEL_EPOCH=(\d+)', fields)
        if marker:
            epoch = int(marker[1])
            continue
        name, _, action = event.rpartition('_')
        if name not in names or action not in ('enter', 'exit'):
            continue
        key = (name, pid)
        if action == 'enter':
            value = re.search(r'attach=(0x[0-9a-f]+|\d+)', fields)
            stacks.setdefault(key, []).append((stamp, epoch, bool(int(value[1], 0)) if value else None))
        else:
            need(stacks.get(key), 'Unmatched kernel trace return')
            start, generation, attach = stacks[key].pop()
            need(stamp >= start, 'Kernel clock reversed')
            rows.append({'epoch': generation, 'function': name, 'attach': attach, 'duration_ns': stamp-start})
    need(rows and not any(stacks.values()), 'Incomplete kernel trace')
    return rows


def verify(path):
    path = Path(path)
    r = json.loads(path.read_bytes())
    need(r['schema'] == 'swe-handoff-trace-v1' and r['result'] == 'passed', 'Incomplete runtime trial')
    e = Evidence(r, path.parent)
    memory = r['arguments']['memory_mib'] * MIB
    need(memory == 2048*MIB and r['mode'] in ('baseline', 'state-only', 'direct'), 'Unexpected runtime geometry')
    verify_sources(r, path.parent)
    workload = verify_trace(e, r)
    launches = {}
    for launch in r['launches']:
        pids = re.findall(r'^EXEC_PROCESS (\d+) (\d+)\s*$', e.text(launch['file']), re.M)
        need(len(pids) == 1 and launch['directory'] not in launches, 'Duplicate/missing runtime incarnation')
        launches[launch['directory']] = (pids[0], launch['role'])
    for row in r['cleanup']:
        need(row.get('fenced') and row['directory'] in launches, 'Unfenced runtime')
        pid, role = launches[row['directory']]
        need(row['role'] == role and re.findall(r'^FENCED (\d+) (\d+)\s*$', e.text(row['file']), re.M) == [pid],
             'Cleanup fenced a different process')
    need(len(r['cleanup']) == len(launches), 'Runtime cleanup missing')
    configs = [a['request'] for a in r['api_events'] if a['path'] == '/machine-config']
    vcpus = r['arguments'].get('vcpus',1)
    need(vcpus in (1,2,4,8) and configs == [{'vcpu_count': vcpus, 'mem_size_mib': 2048}], 'Wrong VM configuration')
    disks = [a['request'] for a in r['api_events'] if a['path'] == '/drives/data']
    need(len(disks) == 1 and disks[0]['is_read_only'] and disks[0]['path_on_host'] == '/disk/sandbox.raw',
         'Wrong immutable block device')
    for api in r['api_events']:
        need(e.record(api['file'], 'LAB_API_JSON ') == api['response'] and api['response']['status'] in (200,204),
             'Failed/altered API result')
        if api['path'] == '/snapshot/create':
            need(r['mode'] == 'state-only' and api['request']['mem_file_path'] == '/dev/dax0.0', 'Full-RAM snapshot')
        if api['path'] == '/snapshot/load':
            need(r['mode'] == 'state-only' and api['request']['mem_backend']['backend_path'] == '/dev/dax0.0',
                 'Wrong snapshot RAM backend')
    grants, initial = {}, {}
    for event in r['allocator_events']:
        owner, frames = event['owner'], ranges(event['ranges'])
        need(len(frames) == event['pages'] and all(PRIVATE_START <= x < PRIVATE_END for x in frames), 'Bad grant')
        if event['operation'] == 'allocate':
            need(not any(frames & old for old in grants.values()), 'Private allocation alias')
            grants.setdefault(owner,set()).update(frames)
            initial.setdefault(event['generation'],set()).update(frames)
        else:
            need(event['operation'] == 'release' and grants.get(owner) == frames and
                 event['fenced'] and event['detached'], 'Released live/foreign grant')
            del grants[owner]
    need(not grants, 'Leaked pool grant')
    counts = collections.Counter()
    handoffs = []
    for transition in r['transitions']:
        kind = transition['kind']; counts[kind] += 1
        need(transition['result'] == 'passed', 'Failed transition')
        if kind in ('promotion','demotion'):
            operation = transition['operation']; raw = e.json(operation['file'])
            need(raw == operation['report'] and raw['result'] == 'passed' and
                 raw['ram_snapshot_bytes'] == 0, 'Invalid tier-change operation')
            if kind == 'promotion':
                value, private = placement(e, transition['export']['file'], memory,
                                                    e.json(operation['plan_file']))
                need(private == initial[value['allocation_generation']], 'Promotion escaped grant')
                need(len(private)*PAGE == raw['copied_private_bytes'] == memory, 'Promotion accounting')
                identity_pair(e, transition['before'], transition['after'])
            else:
                need(raw['copied_private_bytes'] == memory, 'Demotion did not materialize RAM')
                h = handle(e.json(transition['previous']['file']), memory)
                returned = ranges(transition['returned_ranges'])
                need(returned == initial[h['allocation_generation']] and raw['module'] is None,
                     'Demotion released wrong grant or retained mapper')
                covered = 0
                for region in raw['dram_vmas']:
                    need(region['bytes'] > 0, 'Empty DRAM region')
                    for line in region['maps']:
                        fields = line.split()
                        need(len(fields) == 5 and fields[1] == 'rw-p' and fields[4] == '0', 'Nonprivate DRAM mapping')
                    covered += region['bytes']
                need(covered == memory, 'Demotion RAM coverage')
                identity_pair(e,transition['before'],transition['after'])
        elif kind == 'direct':
            need(transition.get('runtime_path') == 'resident-v2', 'Unexpected direct runtime')
            fast = e.record(transition['fast_file'],'RESIDENT_FAST_JSON ')
            from resident_proxy import verify_receipt
            verify_receipt(e.text(transition['fast_file']),fast)
            need(fast == transition['fast'] and fast['version'] == 2 and fast['ack'] is None and
                 set(fast['apis']) == {'send','receive'} and all(x['status'] == 204 for x in fast['apis'].values()),
                 'Runtime retained diagnostic API/guest probe or handoff failed')
            need(0 < fast['pause_to_resume_response_ns'] <= fast['handoff_ns'] <= fast['request_ns']
                 and fast['pause_to_resume_response_ns'] <= fast['request_to_resume_response_ns'] <= fast['request_ns']
                 and sum(api['duration_us'] for api in fast['apis'].values())*1000 <= fast['handoff_ns']+1000,
                 'Request timing excludes measured work')
            handoffs.append(transition)
        elif kind == 'state-only':
            state=e.read(transition['state_file']);manifest=e.json(transition['manifest_file'])
            h = handle(manifest, memory)
            need(h['state'] == 2 and h['owner_epoch'] == transition['source_epoch'], 'Unsealed/foreign source mapper')
            need(0 < len(state) < MIB and sha(state) == manifest['state_sha256'] and
                 transition['ram_transfer_bytes'] == transition['ram_snapshot_bytes'] == 0, 'Bad state-only transfer')
            if transition.get('runtime_path') == 'state-v2':
                need('stop_snapshot_process' in e.text(transition['source_stop_file']), 'Source stop absent')
                need(transition['load_api']['request']['resume_vm'] is True, 'Restore did not resume')
            else:
                identity_pair(e,transition['before'],transition['after'])
            handoffs.append(transition)
        else:raise ValueError('Unknown transition')
    if r['mode'] == 'direct':
        need(len(launches) == 2 and {role for pid, role in launches.values()} == {'A', 'B'}, 'Resident VMM relaunch')
        preps = r['resident_preparations']
        need(len(preps) == 1 and preps[0]['fresh'] is True and preps[0]['phase'] == 'post-promotion-prewarm',
             'Readiness repeated inside request')
        prep = preps[0]
        boot = e.record(prep['native_boot_file'], 'RESIDENT_BOOT_JSON ')
        need(boot == prep['native_boot'] and boot['native_ready_ns'] >= boot['fork_to_api_ready_ns']+boot['prepare_api_ns']
             and boot['fork_to_api_ready_ns'] >= boot['fork_ns'] > 0, 'Native readiness timing changed')
        need([str(boot['pid']), str(boot['starttime'])] == list(launches[prep['directory']][0]), 'Native prepared PID changed')
        need(prep['source_paused'] is False, 'Preparation was on paused path')
        need(set(prep['servers']) == {'A', 'B'} and prep['full_prewarm_controller_ns'] >=
             prep['controller_wall_ns']+prep['control_server_prepare_ns'], 'Prewarm interval excludes work')
        for server in prep['servers'].values():
            boot = e.record(server['file'], 'RESIDENT_SERVER_BOOT_JSON ')
            need(boot == server['native_boot'] and boot['native_ready_ns'] >= boot['fork_to_ready_ns'] > 0,
                 'Native server readiness changed')
            closed = [row for row in r['controller_cleanup'] if row['prepare_file'] == server['file']]
            need(len(closed) == 1 and closed[0]['fenced'] is True and
                 re.findall(r'^FENCED (\d+) (\d+)\s*$', e.text(closed[0]['file']), re.M) ==
                 [(str(boot['pid']), str(boot['starttime']))], 'Native control server identity changed')
        for hop in handoffs:
            ready = r['resident_readiness'][hop['destination']]
            prepared = hop['preparation']
            need(prepared.get('cached') is True and prepared['api'] is None and prepared['fresh'] is False
                 and prepared['ready_file'] == ready['ready_file'] and prepared['directory'] == ready['directory']
                 == hop['destination_directory'], 'Cached readiness changed')
            need(prepared['source_paused'] is False and e.order[prepared['ready_file']] < e.order[hop['fast_file']],
                 'Preparation was on paused path')
            need(prepared['control_server_prepare_ns'] == 0, 'Ready request created servers')
        home = [row for row in r['receipts'] if row['kind'] == 'home-storage']
        remote = [row for row in r['receipts'] if row['kind'] == 'remote-storage']
        need(len(home) == len(remote) == 1 and 'nocto' in e.text(remote[0]['file']), 'Immutable NFS cache missing')
        mounts = [line.split() for line in e.text(home[0]['file']).splitlines()]
        need(any(len(row) == 6 and row[1:3] == ['/storage', 'ext4'] and 'ro' in row[3].split(',')
                 for row in mounts), 'Image filesystem is writable')
    traces = r.get('kernel_trace', [])
    need(len(traces) == (len(handoffs) if r['arguments'].get('kernel_trace') else 0), 'Kernel trace coverage')
    for trace, hop in zip(traces, handoffs):
        need(trace['epoch'] == hop['destination_epoch'] and trace['destination'] == hop['destination'], 'Kernel trace epoch')
        need({node['role'] for node in trace['nodes']} == {'A', 'B'}, 'Kernel trace node coverage')
        for node in trace['nodes']:
            parsed = kernel_intervals(e.text(node['file']))
            need(parsed == node['intervals'] and all(row['epoch'] == trace['epoch'] for row in parsed), 'Kernel trace changed')
    for preparation in r.get('resident_preparations',[]):
        server=preparation.get('servers',{}).get('B',{})
        if 'peer_ready' in server:
            peer=e.record(server['file'],'RESIDENT_PEER_READY_JSON ')
            need(peer==server['peer_ready'] and peer['owner_changed'] is False and
                 0<peer['connection_prepare_ns']<=server['native_boot']['native_ready_ns'],
                 'Peer connection preparation changed')
            need(all(hop['fast']['connection_prepare_ns']==0 for hop in handoffs),
                 'Prepared peer reconnected during handoff')
    events = {}
    for name,item in e.items.items():
        if item['kind'] != 'final-vmm-log':continue
        for line in e.text(name).splitlines():
            if 'DIRECT_HANDOFF_JSON ' in line:
                event=json.loads(line.split('DIRECT_HANDOFF_JSON ',1)[1])
                key=(event['event'],event['token']['generation'])
                need(key not in events,'Repeated handoff event');events[key]=event
    if r['mode'] == 'direct' and r['arguments'].get('restore_timing'):
        need(len(events) == 2*len(handoffs),'Missing profiled handoff')
        retained=None
        for i,hop in enumerate(handoffs,1):
            exp,imp=events[('export',i)],events[('import',i)]
            keys=('vm_offset','vcpu_offset','vmcs_offset','run_offset','ram_hva','ram_bytes','vm_creates','vcpu_creates')
            if 'abi_version' in exp['token']:
                keys += ('abi_version','vcpu_count','vcpus')
                need(exp['token']['abi_version'] == 2 and exp['token']['vcpu_count'] == vcpus, 'Wrong SMP token ABI/topology')
                need(len(exp['token']['vcpus']) == 8 and
                     [c['id'] for c in exp['token']['vcpus'][:vcpus]] == list(range(vcpus)), 'Missing per-vCPU identities')
            graph={k:exp['token'][k] for k in keys}
            need(all(imp['token'][k] == v for k,v in graph.items()) and
                 exp['token']['flags'] == 2 and imp['token']['flags'] == 1,'Changed kernel graph/owner')
            need(graph['vm_creates'] == 1 and graph['vcpu_creates'] == vcpus and
                 exp['snapshot_bytes'] == exp['ram_transfer_bytes'] == imp['snapshot_bytes'] == imp['ram_transfer_bytes'] == 0,
                 'Snapshot or RAM copy on direct handoff')
            if retained is not None:need(graph == retained,'Recreated kernel graph')
            retained=graph
            need(imp['mapper']['handle']['state'] == 1 and int(imp['mapper']['run_epoch']) == hop['destination_epoch'],
                 'Wrong imported mapper epoch')
    if r['mode'] == 'baseline':
        need(not counts and not grants,'Baseline changed memory tier')
    else:
        extra=2 if r['arguments'].get('resident_repromote_handoff') else 0
        if r['arguments'].get('inflight_handoff_index') is not None: extra += 2
        if r['arguments'].get('parallel_build'): extra += 6
        nhops=2+2*r['arguments'].get('direct_profile_roundtrips',0)+extra
        need(counts == {'promotion':2,'demotion':2,r['mode']:nhops},'Missing lifecycle case')
        need([(x['source'],x['destination'],x['source_epoch'],x['destination_epoch']) for x in handoffs] ==
             [('A' if i%2==0 else 'B','B' if i%2==0 else 'A',i,i+1) for i in range(nhops)],'Wrong itinerary')
        need(r['allocator_final']['allocated_bytes'] == 0,'Allocator not empty')
    need(len(r['audits']) == (2 if r['arguments'].get('full_audit') and r['mode'] != 'baseline' else 0),
         'Explicit audit coverage')
    for audit in r['audits']:
        raw = e.json(audit['file'])
        need(raw['operation'] == 'audit' and len(raw['pages'])*PAGE == memory, 'Audit coverage')
        for index, page in enumerate(raw['pages']):
            need(page['gpa'] == index*PAGE and PRIVATE_START <= page['offset'] < PRIVATE_END, 'Audit GPA/frame')
            if page['present']:
                need(page['writable'] and page['actual_pfn']*PAGE == raw['phys_base']+page['offset'], 'Private PTE mismatch')
    inflight = [command for command in r['commands'] if 'inflight' in command]
    requested = r['arguments'].get('inflight_handoff_index')
    need(len(inflight) == (0 if requested is None else 1), 'Missing/extra in-flight command')
    for command in inflight:
        proof = command['inflight']; start = guest(e,proof['started'],'trace_async_started')
        end = guest(e,command,'trace_end')
        need(start['index'] == end['index'] == requested and end['run_label'] == start['label'],
             'In-flight command identity mismatch')
        observed = proof['observations']; files = proof['handoff_files']
        need(len(observed) == 3 and len(files) == 2, 'Incomplete in-flight itinerary')
        selected = [hop for hop in handoffs if hop['fast_file'] in files]
        need([hop['fast_file'] for hop in selected] == files and
             [(hop['source'],hop['destination']) for hop in selected] == [('B','A'),('A','B')],
             'Wrong in-flight handoffs')
        for i, observation in enumerate(observed):
            live = guest(e,observation,'trace_running')
            need(live['active'] is True and live['process_state'] not in ('Z','X','x','missing') and
                 live['start_ticks'] > 0 and all(live[key] == start[key] for key in
                    ('index','command_pid','start_ticks')) and observation['role'] == ('A' if i==1 else 'B'),
                 'Command not alive across handoff')
        order = [proof['started']['file'],observed[0]['file'],files[0],observed[1]['file'],
                 files[1],observed[2]['file'],command['file']]
        need([e.order[f] for f in order] == sorted(e.order[f] for f in order), 'In-flight evidence reordered')
    return {'result':'passed','mode':r['mode'],'trace':r['trace']['name'],
        'report_sha256':sha(path.read_bytes()),**execution_identity(r),**workload,
        'transitions':dict(counts),'inflight_commands':len(inflight),
        'runtime_image_rehash':False,'remote_nfs_reads':None,
        'per_handoff_guest_probes':any('before' in hop or 'after' in hop for hop in handoffs),
        'profiled_kernel_handoffs':len(events)//2,
        'limitations':['Coherent nested lab; immutable fixture identity comes from build records, not runtime rehash',
                       'No per-handoff source fault probes; owner rejection tests run separately',
                       'Per-command NFS counter polling removed; NFS read count was not measured']}

def verify_suite(path):
    path = Path(path)
    suite = json.loads(path.read_bytes())
    need(suite["schema"] == "swe-handoff-suite-v1" and suite["trials"], "unknown/empty suite")
    verify_sources(suite, path.parent)
    rows, groups = [], collections.defaultdict(dict)
    for trial in suite["trials"]:
        report = local(path.parent, trial["report_file"])
        need(sha(report.read_bytes()) == trial["report_sha256"], "suite report hash")
        result = verify(report)
        need(result["trace"] == trial["trace"] and result["mode"] == trial["mode"], "suite trial identity")
        need(set(trial["outer_cleanup"]) == {"A", "B"} and
             all(entry["fenced"] is True for entry in trial["outer_cleanup"].values())
             and trial["memory_backing_removed"] is True and all(trial["network_removed"].values()), "outer resources not cleaned")
        need(result["mode"] not in groups[result["trace"]], "duplicate trace/mode trial")
        groups[result["trace"]][result["mode"]] = result
        rows.append(result)
    need(len({row["execution_inputs_sha256"] for row in rows}) == 1, "suite mixes execution source/runtime/configuration")
    comparison = []
    for name, modes in groups.items():
        need(len({row["workload_inputs_sha256"] for row in modes.values()}) == 1,
             "trace modes use different immutable inputs or guest command environment")
        baseline = modes.get("baseline")
        for mode, result in modes.items():
            if mode == "baseline":
                continue
            if baseline:
                need(result["returncodes"] == baseline["returncodes"] and result["tests"] == baseline["tests"] and
                     result["submission"]["without_startup_warning_sha256"] == baseline["submission"]["without_startup_warning_sha256"],
                     "baseline and lifecycle workload results differ")
            comparison.append({"trace": name, "mode": mode, "baseline_present": baseline is not None,
                               "workload_results_match": True if baseline else None})
    return {"result": "passed", "trials": rows, "comparisons": comparison,
            "execution_inputs_sha256": rows[0]["execution_inputs_sha256"],
            "coverage": {name: sorted(modes) for name, modes in groups.items()},
            "complete_three_mode_matrix": all(set(modes) == MODES for modes in groups.values()),
            "complete_nine_trial_matrix": set(groups) == TRACE_NAMES and all(set(modes) == MODES for modes in groups.values())}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, nargs="?")
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.report is None) == (args.suite is None):
        parser.error("Choose a report or --suite")
    result = verify_suite(args.suite) if args.suite else verify(args.report)
    data = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(data)
    print(data, end="")


if __name__ == '__main__':
    main()
