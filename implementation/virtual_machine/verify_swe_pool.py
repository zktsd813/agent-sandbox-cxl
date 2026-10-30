#!/usr/bin/env python3
"""Independently audit exact SWE commands and RAM-preserving pool transitions.

No benchmark, controller, or admission-parser imports. The older independent
auditors supply only raw-evidence, archive, trace and mapping readers.
"""
import argparse
import base64
import collections
import hashlib
import json
import math
from pathlib import Path
import re
import struct

from verify_pool_handoff import Evidence, dram_mapping, paused, ranges
from verify_trace_results import (FATAL_KERNEL, SUBMIT, archive_files, block_stats,
                                  fingerprint, original_trace, test_summary)

PAGE = 4096
MIB = 1024**2
PRIVATE_START, PRIVATE_END = 2 * MIB, 3072 * MIB
CANONICAL_START = 4096 * MIB
TRACE_NAMES = {"gin-gonic__gin-3741", "php-cs-fixer__php-cs-fixer-8331", "micropython__micropython-13039"}
MODES = {"baseline", "state-only", "direct"}


def need(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


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
                              ("firecracker", "l1-kernel", "l2-vmlinux", "guest-pool", "pool_memory_module.ko", "pool-dax-copy")},
        "host_modules": fixture["inputs"]["pool_kernel_modules"],
        "configuration": {key: config[key] for key in ("host_kernel", "qemu_sha256", "l1_vcpus", "l1_memory_mib",
                                                       "shared_mib", "cxl_mib", "dax_guest", "dax_4k")},
        "storage_template_sha256": config["home_storage_image"]["template_sha256"],
        "memory_mib": report["arguments"]["memory_mib"], "vcpus": 1,
        "credit_mib": report["arguments"]["credit_mib"]}
    workload = {key: plan[key] for key in ("name", "trace_sha256", "image_sha256", "image_bytes", "command_count",
        "commands", "handoff_before", "environment", "effective_environment", "pool_files", "pool_requested_pages")}
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
        starts = [row for row in rows if row["event"] == "trace_start" and row["label"] == end["label"]]
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


def raw_authority(e, event, plan, memory):
    """Reconstruct the public UAPI bytes without the runtime parser."""
    prepared = guest(e, event["command"], "swe_pool_prepared")
    identity = guest(e, event["identity"], "trace_identity")
    data = e.read(event["file"])
    need(prepared["bytes"] == len(data) and prepared["sha256"] == sha(data), "authority transfer identity")
    need(prepared["boot_id"] == identity["boot_id"] and prepared["pid"] == identity["pid"], "authority guest identity")
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    head, status = rows[0], rows[-1]
    expected = plan["pool_files"]
    need(head == event["summary"] and head["kind"] == "guest_pool_registration" and head["version"] == 1,
         "authority summary changed")
    n = head["selected_pages"]
    need(type(n) is int and 0 <= n <= 65536 and head["nr_files"] == len(expected) <= 16
         and len(rows) == 2 + len(expected) + n, "authority coverage")
    need(head["image_id"] == plan["image_sha256"] and head["vm_epoch"] == event["epoch"] == prepared["epoch"]
         and head["plan_generation"] == event["token"] == prepared["plan_generation"] > 0
         and head["session_cookie"] > 0 and prepared["selected_pages"] == n, "authority version/session binding")
    need(head["status_gpa"] % PAGE == 0 and 0 <= head["status_gpa"] <= memory - PAGE, "status GPA")
    identities = rows[1:1 + len(expected)]
    records = rows[1 + len(expected):-1]
    digest = hashlib.sha256(b"GPOOL_RECORDS_V1")
    digest.update(struct.pack("<IIQQQQ32s", 1, len(identities), n, head["session_cookie"], head["vm_epoch"],
                              head["plan_generation"], bytes.fromhex(head["image_id"])))
    requested, inodes = 0, set()
    for slot, (actual, frozen) in enumerate(zip(identities, expected)):
        need(actual["kind"] == "guest_pool_file" and actual["file_slot"] == slot, "file order")
        for key in ("inode", "inode_generation", "file_bytes", "fs_uuid"):
            need(actual[key] == frozen[key], "authority file differs from frozen " + key)
        need(actual["inode"] not in inodes and actual["start_page"] == 0
             and actual["page_count"] == (actual["file_bytes"] + PAGE - 1) // PAGE
             and actual["dev_major"] == 253 and actual["dev_minor"] == 0, "authority inode/range/device")
        inodes.add(actual["inode"])
        requested += actual["page_count"]
        digest.update(struct.pack("<QQQQII16sII", actual["inode"], actual["file_bytes"], actual["start_page"],
            actual["page_count"], actual["inode_generation"], actual["file_slot"], bytes.fromhex(actual["fs_uuid"]),
            actual["dev_major"], actual["dev_minor"]))
    need(requested == head["requested_pages"] <= 65536, "requested cache scope differs")
    previous, by_gpa = None, {}
    for record in records:
        need(record["kind"] == "guest_pool_record" and record["flags"] == 63, "dirty/unlocked authority")
        slot, index, address, disk = (record[k] for k in ("file_slot", "file_page", "gpa", "disk_offset"))
        need(type(slot) is int and 0 <= slot < len(expected) and type(index) is int and index >= 0,
             "record file bounds")
        key = (slot, index)
        need(previous is None or previous < key, "unordered/duplicate authority")
        previous = key
        need(type(address) is int and address % PAGE == 0 and 0 <= address <= memory - PAGE
             and address not in by_gpa and address != head["status_gpa"], "authority GPA alias/bounds")
        need(index < identities[slot]["file_bytes"] // PAGE, "authority includes partial EOF")
        offsets = [physical + index * PAGE - logical for logical, physical, size in expected[slot]["extents"]
                   if logical <= index * PAGE and (index + 1) * PAGE <= logical + size]
        need(offsets == [disk] and disk % PAGE == 0 and 0 <= disk <= plan["image_bytes"] - PAGE,
             "authority bmap/frozen extent mismatch")
        by_gpa[address] = disk
        digest.update(struct.pack("<IIQQQ", slot, 63, index, address, disk))
    need(digest.hexdigest() == head["records_sha256"], "authority digest")
    need(status["kind"] == "guest_pool_status" and status["state"] == 2 and
         all(status[k] == head[k] for k in ("session_cookie", "vm_epoch", "plan_generation", "records_sha256"))
         and status["record_count"] == n, "authority inactive/mismatched trailing status")
    return {"summary": head, "by_gpa": by_gpa, "count": n, "event": event,
            "registration_metadata_read_ios": prepared["block_read_ios"]}


def status_proof(raw, authority):
    data = bytes.fromhex(raw["registration_status_hex"])
    need(len(data) == 208, "kernel status length")
    s = authority["summary"]
    values = struct.unpack_from("<QIIQQQQII", data)
    need(values == (0x47504f4f4c535431, 2, 1, s["session_cookie"], s["vm_epoch"],
                    s["plan_generation"], s["selected_pages"], s["nr_files"], 32), "stale live kernel authority")
    need(data[56:88].hex() == s["image_id"] and data[88:120].hex() == s["records_sha256"]
         and data[128:136] == bytes(8) and data[144:] == bytes(64), "live authority digest/reserved")


def placement(e, name, memory, plan):
    value = e.json(name)
    need(value["version"] == 1 and len(value["mappings"]) == memory // PAGE and value["exhausted"] == 0,
         "incomplete/exhausted placement")
    need(value["canonical_start"] == CANONICAL_START and value["canonical_bytes"] == plan["image_bytes"],
         "canonical image geometry")
    private, shared = set(), {}
    for index, row in enumerate(value["mappings"]):
        offset = row["offset"]
        need(row["gpa"] == index * PAGE and type(row["shared"]) is bool and type(offset) is int
             and offset % PAGE == 0 and 0 <= offset <= value["device_bytes"] - PAGE, "placement geometry")
        if row["shared"]:
            need(CANONICAL_START <= offset < CANONICAL_START + plan["image_bytes"], "shared outside canonical")
            shared[row["gpa"]] = offset
        else:
            need(PRIVATE_START <= offset < PRIVATE_END and offset not in private, "private alias/range")
            private.add(offset)
    credits = set(value["credits"])
    need(len(credits) == len(value["credits"]) and not (credits & private), "credit alias")
    need(all(type(p) is int and p % PAGE == 0 and PRIVATE_START <= p < PRIVATE_END for p in credits), "credit bounds")
    return value, private, shared, credits


def nfs_reads(e, name):
    records = [line.split() for line in e.text(name).splitlines() if line.startswith("proc3 ")]
    need(len(records) == 1, "NFS counter missing")
    return int(records[0][8])


def runtime_inputs(e, report):
    fixture, plan = report["fixture"], report["trace"]
    expected = {"/lab/firecracker": fixture["inputs"]["firecracker"]["sha256"],
                "/lab/l2-vmlinux": fixture["artifacts"]["l2-vmlinux"],
                "/lab/guest-pool": fixture["artifacts"]["guest-pool"],
                "/lab/pool_memory_module.ko": fixture["artifacts"]["pool_memory_module.ko"],
                "/lab/" + plan["name"] + "-initramfs.cpio": fixture["artifacts"][plan["name"] + "-initramfs.cpio"],
                **{"/modules/" + key: value for key, value in fixture["inputs"]["pool_kernel_modules"].items()}}
    hashes = [e.text(name) for name, item in e.items.items() if item["kind"] == "runtime-hashes"]
    need(len(hashes) == 2, "missing host runtime identity")
    for raw in hashes:
        measured = {name: digest for digest, name in re.findall(r"^([0-9a-f]{64})  (\S+)\s*$", raw, re.M)}
        need(measured == expected and "dax_scattered" in raw, "runtime kernel/helper/input differs")
    setup = e.record(report["pool_setup"]["file"], "POOL_DAX_COPY_JSON ")
    need(setup["result"] == "passed" and setup["operation"] == "import" and
         setup["offset"] == CANONICAL_START and setup["bytes"] == plan["image_bytes"], "canonical preload missing/wrong")
    final = report["canonical_final"]
    raw = e.record(final["file"], "POOL_DAX_COPY_JSON ")
    need(raw == final["receipt"] and raw["operation"] == "verify" and raw["result"] == "passed"
         and raw["offset"] == CANONICAL_START and raw["bytes"] == plan["image_bytes"]
         and raw["full_compare"] is True, "canonical full-image verification failed")
    image = final["image"]
    need(re.findall(r"^([0-9a-f]{64})  /disk/sandbox.raw\s*$", e.text(image["file"]), re.M) ==
         [image["sha256"]] == [plan["image_sha256"]], "immutable backing image changed")


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
    need({key for key in files if key.startswith("results/") and key.split("/")[-1][:3].isdigit()} ==
         expected_files, "missing/extra command artifacts")
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


def verify(path):
    path = Path(path)
    report = json.loads(path.read_bytes())
    need(report["schema"] == "swe-pool-trace-v1" and report["result"] == "passed", "trace run incomplete/failed")
    need(report["mode"] in ("baseline", "state-only", "direct"), "unknown mode")
    e = Evidence(report, path.parent)
    plan, memory = report["trace"], report["arguments"]["memory_mib"] * MIB
    need(memory == 2048 * MIB and report["arguments"]["credit_mib"] == 256 and report["config"]["cxl_mib"] == 16384,
         "unexpected workload geometry")
    need(report["config"]["dax_handoff"] == (report["mode"] == "direct") and
         report["config"]["dax_guest"] is True and report["config"]["dax_4k"] is True, "wrong outer memory mode")
    need(report["boundaries"] == [plan["handoff_before"], plan["command_count"] - 2], "unexpected transition boundaries")
    need(report["layout"] == {"private_start": PRIVATE_START, "private_end": PRIVATE_END, "bloom": 3072*MIB,
        "canonical_start": CANONICAL_START, "canonical_bytes": plan["image_bytes"], "kvm_arena": 12288*MIB}, "pool geometry changed")
    for name, expected in report["source_hashes"].items():
        need(Path(name).name == name and sha((path.parent / "measured-sources" / name).read_bytes()) == expected,
             "measured source differs")
    trace_result = verify_trace(e, report)
    runtime_inputs(e, report)
    incarnations = {}
    expected_vmm = report.get("runtime_overrides", {}).get("firecracker", {}).get("sha256")
    if expected_vmm is None:
        expected_vmm = report["fixture"]["inputs"]["firecracker"]["sha256"]
    for launch in report["launches"]:
        raw = e.text(launch["file"])
        hashes = re.findall(r"^([0-9a-f]{64})  /proc/([0-9]+)/exe\s*$", raw, re.M)
        pids = re.findall(r"^EXEC_PROCESS (\d+) (\d+)\s*$", raw, re.M)
        need(len(hashes) == len(pids) == 1 and hashes[0][0] == expected_vmm == launch["sha256"]
             and hashes[0][1] == pids[0][0], "runtime VMM identity")
        need(launch["directory"] not in incarnations, "duplicate VMM incarnation")
        incarnations[launch["directory"]] = (pids[0], launch["role"])
    for api in report["api_events"]:
        need(e.record(api["file"], "LAB_API_JSON ") == api["response"] and api["response"]["status"] in (200, 204),
             "API response altered/failed")
        echo = e.text(api["file"]).split("LAB_API_JSON ", 1)[0].replace("\r", "").replace("\n", "")
        need(api["path"] in echo and json.dumps(api["request"]) in echo, "API request altered")
        if api["path"] == "/snapshot/create":
            need(report["mode"] == "state-only" and api["request"]["mem_file_path"] == "/dev/dax0.0", "full-RAM/wrong-mode snapshot")
        if api["path"] == "/snapshot/load":
            need(report["mode"] == "state-only" and api["request"]["mem_backend"] ==
                 {"backend_type": "File", "backend_path": "/dev/dax0.0"} and api["request"]["resume_vm"] is False,
                 "restore copied/wrong RAM backing")
    configs = [api["request"] for api in report["api_events"] if api["path"] == "/machine-config"]
    need(configs == [{"vcpu_count": 1, "mem_size_mib": 2048}], "unexpected inner VM CPU/memory configuration")
    disks = [api["request"] for api in report["api_events"] if api["path"] == "/drives/data"]
    need(len(disks) == 1 and disks[0]["path_on_host"] == "/disk/sandbox.raw" and disks[0]["is_read_only"] is True,
         "root block device not immutable")
    authorities = {a["file"]: raw_authority(e, a, plan, memory) for a in report["authority_events"]}
    need(len(authorities) == len(report["authority_events"]), "reused authority event")
    grants, initial, lifetime, released = {}, {}, {}, {}
    for event in report["allocator_events"]:
        owner, frames = event["owner"], ranges(event["ranges"])
        need(len(frames) == event["pages"] and all(PRIVATE_START <= x < PRIVATE_END for x in frames), "allocator range")
        if event["operation"] == "allocate":
            need(not any(frames & others for others in grants.values()), "cross-owner private alias")
            if owner not in grants:
                need(event["generation"] not in initial, "allocation generation reused")
                initial[event["generation"]] = set(frames)
                grants[owner] = set()
            grants[owner].update(frames)
            lifetime.setdefault(event["generation"], set()).update(frames)
        elif event["operation"] == "release":
            need(owner in grants and grants[owner] == frames and event["fenced"] is True and event["detached"] is True,
                 "released live/wrong grant")
            released[event["generation"]] = frames
            del grants[owner]
        else:
            raise ValueError("unknown allocator operation")
    need(not grants, "private allocator leak")
    counts, shared_bytes, direct_previous = collections.Counter(), [], None
    for transition in report["transitions"]:
        kind = transition["kind"]
        counts[kind] += 1
        need(transition["result"] == "passed", "failed transition")
        if kind in ("promotion", "demotion"):
            operation = transition["operation"]
            raw = e.json(operation["file"])
            need(raw == operation["report"] and raw["result"] == "passed"
                 and raw["ram_snapshot_bytes"] == raw["file_page_compare_bytes"] == 0, "RAM copy/compare scope")
            need(raw["operation"] == operation["operation"] == ("promote" if kind == "promotion" else "demote")
                 and raw["vm_id"] == str(transition["vm"]) and raw["run_epoch"] == str(transition["epoch"]),
                 "memory operation identity changed")
            paused(e, transition["vm"], transition["epoch"], transition["source"], operation["file"])
            if kind == "promotion":
                authority = authorities[transition["authority"]["file"]]
                status_proof(raw, authority)
                value, private, shared, credits = placement(e, transition["export"]["file"], memory, plan)
                need(value["vm_id"] == str(transition["vm"]) and value["run_epoch"] == str(transition["epoch"]),
                     "promotion placement identity changed")
                need(private | credits == initial[value["allocation_generation"]], "promotion outside grant")
                need(len(private) * PAGE == raw["copied_private_bytes"] and len(shared) * PAGE == raw["shared_bytes"]
                     and raw["copied_private_bytes"] + raw["shared_bytes"] == memory, "promotion byte accounting")
                need(all(authority["by_gpa"].get(gpa, -1) + CANONICAL_START == offset for gpa, offset in shared.items()),
                     "promotion shares non-authoritative data")
                released_guest = guest(e, transition["release"], "swe_pool_released")
                need(released_guest["inactive"] is True and e.order[operation["file"]] < e.order[transition["release"]["file"]],
                     "authority released before map")
                need(released_guest["epoch"] == authority["summary"]["vm_epoch"] and
                     released_guest["plan_generation"] == authority["summary"]["plan_generation"], "wrong authority released")
                paused(e, transition["vm"], transition["epoch"], transition["source"], operation["file"],
                       authority["event"]["command"]["file"])
                identity_pair(e, transition["authority"]["identity"], transition["after"])
                shared_bytes.append(len(shared) * PAGE)
            else:
                need(raw["copied_private_bytes"] == memory and raw["shared_bytes"] == 0 and raw["module"] is None,
                     "demotion omitted logical RAM")
                dram_mapping(raw, memory, False)
                lines = e.text(transition["dram_maps_file"]).splitlines()
                need(all(line in lines for vma in raw["dram_vmas"] for line in vma["maps"]), "demotion raw proc maps mismatch")
                value, private, _, credits = placement(e, transition["previous"]["file"], memory, plan)
                need(ranges(transition["returned_ranges"]) == private | credits == released[value["allocation_generation"]],
                     "demotion returned wrong grant")
                identity_pair(e, transition["before"], transition["after"])
        elif kind in ("state-only", "direct"):
            need(kind == report["mode"] and transition["destination_epoch"] == transition["source_epoch"] + 1
                 and transition["ram_snapshot_bytes"] == transition["ram_transfer_bytes"] == 0, "handoff mode/epoch/RAM copy")
            identity_pair(e, transition["before"], transition["after"])
            sources = [x for x in report["launches"] if x["vm"] == transition["vm"] and x["epoch"] == transition["source_epoch"]]
            need(len(sources) == 1 and sources[0]["role"] == transition["source"], "source incarnation missing")
            fence = re.findall(r"^FENCED (\d+) (\d+)\s*$", e.text(transition["source_fence_file"]), re.M)
            need(fence == [incarnations[sources[0]["directory"]][0]], "wrong source fence")
            incoming = e.json(transition["manifest_file"])
            target, private, _, credits = placement(e, transition["destination_placement"]["file"], memory, plan)
            need(incoming["run_epoch"] == str(transition["source_epoch"]) and target["run_epoch"] == str(transition["destination_epoch"])
                 and incoming["vm_id"] == target["vm_id"] == str(transition["vm"])
                 and incoming["allocation_generation"] == target["allocation_generation"], "placement lease/epoch changed")
            need(incoming["mappings"] == target["mappings"] and set(incoming["credits"]) == set(target["credits"])
                 and private | credits == lifetime[target["allocation_generation"]], "handoff changed backing")
            need(e.order[transition["manifest_file"]] < e.order[transition["source_fence_file"]]
                 < e.order[transition["destination_placement"]["file"]] < e.order[transition["after"]["file"]], "handoff ownership order")
            if kind == "state-only":
                data = e.read(transition["state_file"])
                need(len(data) == transition["state_bytes"] < memory // 16
                     and sha(data) == transition["state_sha256"] == incoming["state_sha256"], "CPU state/placement binding")
            else:
                data = e.json(transition["device_state_file"])
                need(set(data) == {"schema", "token", "machine_config", "boot_source", "memory", "memory_manifest_sha256", "devices"}
                     and data["schema"] == "direct-device-handoff-v1" and transition["cpu_snapshot_bytes"] == 0,
                     "direct contains CPU snapshot")
                need(data["memory_manifest_sha256"] == sha(e.read(transition["manifest_file"])), "direct mapping digest")
                token = data["token"]
                need(token == transition["token"] and token["vm_creates"] == token["vcpu_creates"] == 1, "direct rebuilt KVM")
                imports = [(name, row) for name, item in e.items.items() if item.get("kind") == "direct-import-log"
                           for row in e.rows(name, "DIRECT_HANDOFF_JSON ") if row == transition["import_event"]]
                need(len(imports) == 1, "direct import missing")
                name, imported = imports[0]
                need(imported["snapshot_bytes"] == imported["ram_transfer_bytes"] == 0, "direct import copied RAM/CPU")
                need(imported["token"]["flags"] == 1 and imported["token"]["node"] ==
                     (1 if transition["destination"] == "A" else 2), "direct destination ownership")
                need(e.order[transition["source_fence_file"]] < e.order[name] < e.order[transition["after"]["file"]],
                     "direct import outside fenced ownership window")
                retained = ("vm_offset", "vcpu_offset", "vmcs_offset", "run_offset", "ept_root_offset", "ram_hva", "ram_bytes")
                need(all(imported["token"][k] == token[k] for k in (*retained, "generation", "vm_creates", "vcpu_creates")),
                     "direct target replaced kernel object")
                if direct_previous:
                    need(all(token[k] == direct_previous[k] for k in retained) and token["generation"] > direct_previous["generation"],
                         "direct cycle changed KVM graph")
                direct_previous = token
        else:
            raise ValueError("unsupported transition")
    audit_bytes = []
    for audit in report["audits"]:
        event = audit["authority"]
        authority = authorities[event["file"]]
        operation = audit["operation"]
        raw = e.json(operation["file"])
        need({k: v for k, v in raw.items() if k != "pages"} == operation["report"]
             and raw["result"] == "passed", "raw audit/report differs")
        status_proof(raw, authority)
        paused(e, event["vm"], event["epoch"], event["role"], operation["file"], event["command"]["file"])
        pages = raw["pages"]
        need(len(pages) == memory // PAGE and [page["gpa"] for page in pages] == list(range(0, memory, PAGE)),
             "PTE audit omits logical RAM")
        for pte in pages:
            if pte["shared"]:
                need(pte["writable"] is False and CANONICAL_START <= pte["offset"] < CANONICAL_START + plan["image_bytes"],
                     "canonical PTE writable/outside image")
            else:
                need(PRIVATE_START <= pte["offset"] < PRIVATE_END, "audit private frame outside allocator")
            if pte["present"]:
                need(pte["actual_pfn"] * PAGE == raw["phys_base"] + pte["offset"], "actual PTE disagrees with mapping table")
        actual = []
        for address, disk in authority["by_gpa"].items():
            pte = pages[address // PAGE]
            if pte["present"]:
                need(pte["actual_pfn"] * PAGE == raw["phys_base"] + pte["offset"], "actual PTE differs from declared frame")
            if pte["shared"]:
                need(pte["writable"] is False and pte["offset"] == CANONICAL_START + disk,
                     "current lower cache canonical mapping wrong/writable")
                if pte["present"]:
                    actual.append(address)
        need(len(actual) * PAGE == audit["present_shared_bytes"] and
             authority["count"] * PAGE == audit["registered_bytes"], "current eligible/present shared accounting")
        released_guest = guest(e, audit["release"], "swe_pool_released")
        need(released_guest["inactive"] is True and released_guest["epoch"] == event["epoch"]
             and released_guest["plan_generation"] == event["token"]
             and e.order[operation["file"]] < e.order[audit["release"]["file"]], "audit authority lifetime")
        audit_bytes.append(len(actual) * PAGE)
    # Follow all live mapping exports. Once a page is private, its frame must
    # remain fixed until demotion. Detaching a shared page consumes exactly one
    # credit; neither ordinary writes nor reporting may remap another GPA.
    previous_exports, detachments = {}, []
    for name, item in e.items.items():
        if item["kind"] != "memory-manifest":
            continue
        current, private, shared, credits = placement(e, name, memory, plan)
        generation = current["allocation_generation"]
        need(private | credits == lifetime[generation], "export lost/borrowed allocation credits")
        previous = previous_exports.get(generation)
        if previous:
            old, old_credits = previous
            changed = []
            for before, after in zip(old["mappings"], current["mappings"]):
                if before == after:
                    continue
                need(before["shared"] is True and after["shared"] is False and after["offset"] in old_credits,
                     "write/discard changed an unrelated private/canonical frame")
                changed.append(after["offset"])
            need(set(changed) == old_credits - credits and credits <= old_credits,
                 "shared detach did not consume exactly its own credits")
            if changed:
                detachments.append({"file": name, "detached_pages": len(changed),
                                    "cow_pages": current["cow_pages"], "discard_pages": current["discard_pages"],
                                    "epoch": current["run_epoch"]})
        previous_exports[generation] = current, credits
    for entry in report["cleanup"]:
        need(entry.get("fenced") is True and entry["directory"] in incarnations and entry["role"] == incarnations[entry["directory"]][1]
             and re.findall(r"^FENCED (\d+) (\d+)\s*$", e.text(entry["file"]), re.M) == [incarnations[entry["directory"]][0]],
             "final VMM incarnation not fenced")
    need(len(report["cleanup"]) == len(incarnations), "cleanup omitted VMM")
    if report["mode"] == "baseline":
        need(not counts and not authorities and not report["allocator_events"] and not report["audits"], "baseline changed tier")
    else:
        need(counts == {"promotion": 2, "demotion": 2, report["mode"]: 2}, "required lifecycle incomplete")
        need(shared_bytes and max(shared_bytes) > 0, "no naturally resident eligible lower data")
        need(len(report["audits"]) == 2, "missing current guest cache PTE audits")
        sequence = [row["kind"] for row in report["transitions"]]
        need(sequence == ["promotion", report["mode"], report["mode"], "demotion", "promotion", "demotion"],
             "unexpected lifecycle ordering")
        hops = [row for row in report["transitions"] if row["kind"] == report["mode"]]
        need([(row["source"], row["destination"], row["source_epoch"], row["destination_epoch"]) for row in hops] ==
             [("A", "B", 0, 1), ("B", "A", 1, 2)], "unexpected ownership itinerary")
        need([row["before"]["receipt"]["next_index"] for row in hops] == report["boundaries"], "handoff crossed command boundary")
        allocator = report["allocator_final"]
        need(allocator["allocated_bytes"] == 0 and allocator["owners"] == {} and
             allocator["capacity_bytes"] == allocator["free_bytes"] == PRIVATE_END - PRIVATE_START, "allocator not completely returned")
    return {"result": "passed", "mode": report["mode"], "trace": plan["name"], "report_sha256": sha(path.read_bytes()),
            **execution_identity(report),
            "receipts": len(e.items), "transitions": dict(counts), "registered_cache_bytes": [a["count"] * PAGE for a in authorities.values()],
            "shared_cache_bytes": shared_bytes, "present_current_shared_bytes": audit_bytes,
            "shared_detachments": detachments,
            "authority_metadata_read_ios": [a["registration_metadata_read_ios"] for a in authorities.values()], **trace_result,
            "limitations": ["Single coherent physical host; selected naturally resident lower files only",
                            "Transitions occur between recorded commands; not inside a running command"]}


def verify_suite(path):
    path = Path(path)
    suite = json.loads(path.read_bytes())
    need(suite["schema"] == "swe-pool-suite-v1" and suite["trials"], "unknown/empty suite")
    for name, expected in suite["source_hashes"].items():
        need(Path(name).name == name and sha((path.parent / "measured-sources" / name).read_bytes()) == expected,
             "suite source changed")
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


if __name__ == "__main__":
    main()
