#!/usr/bin/env python3
"""Offline audit of the A-home writable-disk and DRAM/CXL lifecycle experiment.

Checks recorded bytes and native observations, not just the controller's verdict.
No VMs are started. --negative-tests mutates in-memory copies of evidence only.
Large RAM payloads are not archived: full RAM equality is attested by the native
helper's byte comparison, while serialized CPU/device states are re-read here.
"""
import argparse
import copy
import functools
import hashlib
import json
import math
from pathlib import Path
import re

MIB = 1024 * 1024
MASK = (1 << 64) - 1
FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
TAGS = {"mutable": 0x4D555441424C4500, "mapped": 0x4D415050494E4700,
        "payload": 0x5041594C4F414400, "cursor": 0x435552534F520000}
IDENTITY = ("boot_id", "pid", "open_cookie", "mutable_inode", "cursor_inode", "unlinked_inode")
FATAL = re.compile(r"Kernel panic|HOME_STORAGE_FATAL|home-storage-probe:|Out of memory:|"
                   r"oom-kill:|EXT4-fs error|Buffer I/O error|I/O error, dev vda|"
                   r"Failed to drain ops and flush|\bkernel BUG at|\bOops:", re.I)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fnv(data, initial=FNV_OFFSET):
    value = initial
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & MASK
    return f"{value:016x}"


@functools.lru_cache(maxsize=128)
def pattern_hash(tag_name, generation, byte_count, offset=0):
    """Independent Python oracle for the fixture's little-endian data words."""
    require(byte_count > 0 and byte_count % 8 == offset % 8 == 0, "oracle range")
    value = FNV_OFFSET
    tag = TAGS[tag_name] ^ ((generation * 0xD6E8FEB86659FD93) & MASK)
    for word in range(offset // 8, (offset + byte_count) // 8):
        x = ((tag ^ word) + 0x9E3779B97F4A7C15) & MASK
        x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & MASK
        x ^= x >> 31
        for byte in x.to_bytes(8, "little"):
            value = ((value ^ byte) * FNV_PRIME) & MASK
    return f"{value:016x}"


class Evidence:
    def __init__(self, directory, report, overrides=None):
        self.directory = directory.resolve()
        self.overrides = overrides or {}
        self.report = report
        self.registered = {}
        for index, row in enumerate(report["receipts"]):
            name = row["file"]
            require(name not in self.registered, "duplicate raw receipt filename")
            require(re.match(rf"^{index:04d}-", name), "raw receipt order/name mismatch")
            require(sha(self.read(name)) == row["sha256"], f"raw receipt SHA256: {name}")
            self.registered[name] = row

    def read(self, name):
        require(isinstance(name, str) and not Path(name).is_absolute(), "absolute evidence path")
        path = (self.directory / name).resolve()
        require(path.is_relative_to(self.directory), "evidence path escapes report directory")
        return self.overrides[name] if name in self.overrides else path.read_bytes()

    def text(self, name):
        return self.read(name).decode().replace("\r", "")

    def native(self, item, prefix, key="receipt"):
        require(item["file"] in self.registered, "unregistered native receipt")
        rows = [json.loads(line[len(prefix):]) for line in self.text(item["file"]).splitlines()
                if line.startswith(prefix)]
        require(len(rows) == 1 and rows[0] == item[key], "raw/native report disagreement")
        return rows[0]

    def integrity(self, name, expected):
        data = self.read(name)
        require(len(data) == expected["bytes"] and sha(data) == expected["sha256"] and
                expected["source_unchanged_during_fetch"] is True, "fetched evidence integrity")
        return data

    def in_order(self, *names):
        require(all(name in self.registered for name in names), "unregistered ordered receipt")
        positions = [int(name.split("-", 1)[0]) for name in names]
        require(all(a < b for a, b in zip(positions, positions[1:])), "unsafe native operation order")


def api(e, item):
    r = e.native(item, "LAB_API_JSON ", "response")
    require(r["status"] == r["expected_status"] == 204 and
            re.match(r"HTTP/1\.1 204(?: |\r?\n)", r["response"]), "native API did not succeed")
    require(type(r["duration_us"]) in (int, float) and math.isfinite(r["duration_us"]) and
            r["duration_us"] >= 0, "invalid native API duration")


def mapping(e, item, tier, memory_bytes, memory_path=None):
    rows = []
    for line in e.text(item["file"]).splitlines():
        match = re.match(r"^([0-9a-f]+)-([0-9a-f]+) ([-rwxps]{4}) ([0-9a-f]+) \S+ \d+\s*(.*)$", line)
        if match:
            start, end, permissions, offset, path = match.groups()
            rows.append({"length": int(end, 16) - int(start, 16), "permissions": permissions,
                         "offset": int(offset, 16), "path": path})
    require(rows == item["mappings"] and item["tier"] == tier and item["memory_path"] == memory_path,
            "RAM mapping raw/report mismatch")
    dax = [row for row in rows if row["path"] == "/dev/dax0.0"]
    if tier == "cxl":
        require(dax == [{"length": memory_bytes, "permissions": "rw-s", "offset": 2*MIB,
                         "path": "/dev/dax0.0"}], "CXL RAM mapping/layout")
    else:
        require(not dax, "DRAM process retains a DAX mapping")
        matches = [row for row in rows if row["length"] == memory_bytes and row["permissions"] == "rw-p"
                   and row["path"] == (memory_path or "") and row["offset"] == 0]
        require(len(matches) == 1, "private local RAM mapping missing")


def image(e, item):
    hashes = re.findall(r"^([0-9a-f]{64})  /disk/sandbox.raw\s*$", e.text(item["file"]), re.M)
    require(hashes == [item["sha256"]], "raw backing image hash differs from report")
    return hashes[0]


def fence(e, name):
    text = e.text(name)
    fields = re.findall(r"^FENCED (\d+) (\d+)$", text, re.M)
    states = re.findall(r"^FENCE_STATE (missing|reused|Z|X) (\d+)$", text, re.M)
    require(len(fields) == len(states) == 1, "incarnation-aware source fence missing")
    pid, expected = map(int, fields[0])
    reason, observed = states[0][0], int(states[0][1])
    require(pid > 1 and expected > 0 and ((reason == "missing" and observed == 0) or
            (reason in ("Z", "X") and observed == expected) or
            (reason == "reused" and observed > 0 and observed != expected)), "source may still execute")


def nfs_write_counters(e, item):
    rows = [line.split() for line in e.text(item["file"]).splitlines() if line.startswith("proc3 ")]
    require(len(rows) == 1 and len(rows[0]) == 24 and rows[0][1] == "22", "invalid NFSv3 procedure counters")
    observed = {"write": int(rows[0][9]), "commit": int(rows[0][23])}
    require(all(item[key] == value for key, value in observed.items()), "NFS write raw/report mismatch")
    return observed


def nfs_read_counters(e, directory):
    rows = [line.split() for line in e.text(directory + "/nfs.txt").splitlines() if line.startswith("proc3 ")]
    require(len(rows) == 1 and len(rows[0]) == 24, "NFS read raw counters missing")
    result = {"rx_bytes": int(e.text(directory + "/rx_bytes.txt")),
              "tx_bytes": int(e.text(directory + "/tx_bytes.txt")), "nfs_read_calls": int(rows[0][8])}
    active, matches = False, []
    for line in e.text(directory + "/mountstats.txt").splitlines():
        words = line.split()
        if words and words[0] == "device":
            active = " mounted on /remote " in line
        if active and words and words[0] == "READ:":
            matches.append([int(words[index]) for index in (1, 4, 5)])
    require(len(matches) == 1, "unique NFS mount READ counters missing")
    result.update(zip(("mount_read_ops", "mount_read_sent_bytes", "mount_read_received_bytes"), matches[0]))
    return result


def mounts(text):
    return [line.split() for line in text.splitlines()
            if len(line.split()) == 6 and line.split()[1].startswith("/")]


def audit(report_path, report=None, overrides=None, pilot=False, full_patterns=False):
    report_path = Path(report_path)
    r = copy.deepcopy(report) if report is not None else json.loads(report_path.read_text())
    require(r["schema_version"] == 1 and r["result"] == "passed", "experiment did not pass")
    require(not r["cleanup_errors"], "controller cleanup errors")
    cycles, memory_bytes = r["arguments"]["cycles"], r["arguments"]["memory_mib"] * MIB
    require(type(cycles) is int and 1 <= cycles <= 10 and 128*MIB <= memory_bytes <= 2048*MIB,
            "unsupported cycle/RAM configuration")
    require(1 <= r["arguments"]["vcpus"] <= r["config"]["l1_vcpus"], "vCPU configuration")
    config = r["config"]
    require(config["dax_guest"] and not config["dax_handoff"] and not config["dax_kvm_state"],
            "ordinary snapshot experiment mislabeled as direct/reuse")
    home = config["home_storage_image"]
    require(home["owner"] == "A" and home["readonly"] is False and home["path"] != home["template"] and
            home["initial_sha256"] == home["template_sha256"], "private home A storage configuration")
    for role in ("A", "B"):
        args = config["vms"][role]["command"]
        drives = [args[index+1] for index, value in enumerate(args[:-1]) if value == "-drive"]
        require(any("id=home-storage" in d and f"file={home['path']}," in d and
                    "readonly=on" not in d for d in drives) == (role == "A"), "home disk attached to wrong host")
        if role == "B":
            require(not drives, "B has a local disk rather than only NFS")
    e = Evidence(report_path.parent, r, overrides)
    require({"home_storage_bench.py", "home_storage_probe.c", "home-storage-l2-init.sh", "ram_transfer.c",
             "shared_probe.c", "lab.py", "nfs_bench.py", "trace_bench.py", "firecracker.patch"} <= set(r["runtime_sources"]),
            "missing measured source")
    for name, expected in r["runtime_sources"].items():
        require(sha(e.read("measured-sources/" + name)) == expected, "measured source SHA256")
    prepared = config["prepared"]
    for name in ("home_storage_probe.c", "home-storage-l2-init.sh"):
        require(prepared["source_hashes"][name] == r["runtime_sources"][name], "prepared guest source differs from measured source")
    for name in ("firecracker", "ram-transfer", "home-storage-probe", "l1-initramfs.cpio", "home-storage-l2-initramfs.cpio"):
        require(re.fullmatch(r"[0-9a-f]{64}", prepared["artifacts"][name]), "prepared binary hash missing")
    raw_mounts = {}
    for kind in ("home-storage", "remote-storage"):
        items = [item for item in r["receipts"] if item["kind"] == kind]
        require(len(items) == 1, "storage mount observation missing")
        raw_mounts[kind] = e.text(items[0]["file"])
    a_disk = [row for row in mounts(raw_mounts["home-storage"]) if row[1] == "/disk"]
    b_disk = [row for row in mounts(raw_mounts["remote-storage"]) if row[1] == "/disk"]
    require(len(a_disk) == 1 and a_disk[0][0] == home["guest_device"] and a_disk[0][2] == "ext4" and
            "rw" in a_disk[0][3].split(","), "home backing is not writable local ext4")
    require(len(b_disk) == 1 and b_disk[0][0] == "172.30.0.1:/storage" and b_disk[0][2] == "nfs" and
            {"rw", "vers=3"} <= set(b_disk[0][3].split(",")), "remote backing is not writable home NFS")
    block_names = [token for line in raw_mounts["remote-storage"].splitlines() for token in line.split()
                   if re.fullmatch(r"vd[a-z]+", token)]
    require(not block_names, "remote block list unexpectedly has a local virtio disk")

    def guest(item, event="home_storage"):
        lines = [json.loads(line) for line in e.text(item["file"]).splitlines() if line.startswith('{"event":')]
        require(item["file"] in e.registered and len(lines) == 1 and lines[0] == item["receipt"], "guest raw/report mismatch")
        value = lines[0]
        require(value["event"] == event and value.get("ok", True) is True, "guest event failed")
        if event == "home_storage":
            require(value["op"] == item["operation"], "guest operation disagreement")
        return value

    def boot(item, auditing):
        value = guest(item, "home_audit" if auditing else "home_ready")
        require(len(item["api"]) == 4, "boot API sequence incomplete")
        for phase in item["api"]: api(e, phase)
        phases = item["api"]
        require([p["path"] for p in phases] == ["/machine-config", "/boot-source", "/drives/data", "/actions"] and
                all(p["role"] == "A" and p["method"] == "PUT" for p in phases), "boot API graph")
        require(phases[0]["request"] == {"vcpu_count": r["arguments"]["vcpus"], "mem_size_mib": memory_bytes // MIB},
                "boot machine configuration")
        drive = phases[2]["request"]
        require(drive["path_on_host"] == "/disk/sandbox.raw" and drive["is_read_only"] is auditing and
                drive["io_engine"] == "Sync" and drive["cache_type"] == "Writeback", "boot disk configuration")
        require(phases[3]["request"] == {"action_type": "InstanceStart"}, "fresh boot action")
        mapping(e, item["ram_mapping"], "dram", memory_bytes)
        e.in_order(*(phase["file"] for phase in phases), item["file"], item["ram_mapping"]["file"])
        if not pilot:
            require(item["mount_file"] in e.registered, "unregistered guest mount")
            rows = [line.removeprefix("HOME_STORAGE_MOUNT ").split()
                    for line in e.text(item["mount_file"]).splitlines() if line.startswith("HOME_STORAGE_MOUNT ")]
            require(len(rows) == 1 and len(rows[0]) == 6 and rows[0][:3] == ["/dev/vda", "/data", "ext4"] and
                    ("ro" if auditing else "rw") in rows[0][3].split(","), "raw guest disk mount")
        return value

    initial = boot(r["boot"], False)
    require(initial["generation"] == 0, "initial image is not generation zero")
    fixture = prepared["home_storage"]["image"]["generator"]
    require(all(initial[key] == fixture[key] for key in ("mutable_bytes", "payload_bytes")), "fixture sizes differ")
    transitions = r["transitions"]
    require(len(transitions) == 4 * cycles and len(r["commands"]) == 24 * cycles + 2, "missing lifecycle stages/commands")
    expected_graph = [("A", "dram", "A", "cxl"), ("A", "cxl", "B", "cxl"),
                      ("B", "cxl", "A", "cxl"), ("A", "cxl", "A", "dram")]
    all_events = list(r["commands"])
    mailbox_nonce, mailbox_sessions, helper_count, api_count = None, set(), 0, 8
    for index, transition in enumerate(transitions):
        epoch = index + 1
        require(transition["epoch"] == epoch and transition["result"] == "passed", "transition epoch/result")
        require(tuple(transition[key] for key in ("source", "source_tier", "destination", "destination_tier")) ==
                expected_graph[index % 4], "lifecycle graph does not return home")
        source, old_tier, target, tier = expected_graph[index % 4]
        before, after = guest(transition["before"]), guest(transition["after"])
        all_events.extend([transition["before"], transition["after"]])
        require(transition["before"]["role"] == source and transition["before"]["tier"] == old_tier and
                transition["after"]["role"] == target and transition["after"]["tier"] == tier, "identity location mismatch")
        require(all(before[key] == after[key] for key in (*IDENTITY, "generation", "cursor_offset", "unlinked_offset", "mapped_hash")),
                "identity or file state changed during handoff")
        fence(e, transition["source_fence_file"])
        require(image(e, transition["source_image"]) == image(e, transition["destination_image"]), "backing disk bytes changed/stale at handoff")
        phases = transition["api"]
        require(len(phases) == 4 and [p["path"] for p in phases] == ["/vm", "/snapshot/create", "/snapshot/load", "/vm"],
                "transition API phases")
        require([p["role"] for p in phases] == [source, source, target, target] and
                [p["method"] for p in phases] == ["PATCH", "PUT", "PUT", "PATCH"], "transition API direction")
        for phase in phases: api(e, phase)
        api_count += len(phases)
        require(phases[0]["request"] == {"state": "Paused"} and phases[3]["request"] == {"state": "Resumed"}, "pause/resume request")
        require(phases[1]["request"]["snapshot_type"] == "Full" and phases[2]["request"]["resume_vm"] is False,
                "snapshot/load request")
        memory_path = phases[2]["request"]["mem_backend"]["backend_path"]
        require(phases[2]["request"]["mem_backend"]["backend_type"] == "File", "RAM restore backend")
        if old_tier == "cxl": require(phases[1]["request"]["mem_file_path"] == "/dev/dax0.0", "CXL snapshot copied RAM unexpectedly")
        else: require(phases[1]["request"]["mem_file_path"].startswith("/run/home-"), "DRAM snapshot file")
        if tier == "cxl": require(memory_path == "/dev/dax0.0", "CXL restore path")
        else: require(memory_path.startswith("/run/home-") and memory_path != phases[1]["request"]["mem_file_path"], "DRAM restore file")
        mapping(e, transition["ram_mapping"], tier, memory_bytes, memory_path if tier == "dram" else None)
        require(transition["ram_transfer_bytes"] == (memory_bytes if tier != old_tier else 0), "RAM copy byte accounting")
        expected_ops = ["import"] if index % 4 == 0 else (["export", "poison"] if index % 4 == 3 else [])
        require([x["receipt"]["operation"] for x in transition["ram"]] == expected_ops, "RAM import/export/poison sequence")
        ordered = [transition["before"]["file"], phases[0]["file"], phases[1]["file"],
                   transition["source_fence_file"], transition["source_image"]["file"]]
        ordered += [item["file"] for item in transition["ram"] if item["receipt"]["operation"] != "poison"]
        ordered += [item["file"] for item in transition["mailbox"]]
        ordered += [transition["destination_image"]["file"], phases[2]["file"], transition["ram_mapping"]["file"]]
        ordered += [item["file"] for item in transition["ram"] if item["receipt"]["operation"] == "poison"]
        ordered += [phases[3]["file"], transition["after"]["file"]]
        e.in_order(*ordered)
        for item in transition["ram"]:
            receipt = e.native(item, "RAM_TRANSFER_JSON ")
            poison = receipt["operation"] == "poison"
            align = receipt["dax_align"]
            require(receipt["result"] == "passed" and item["role"] == "A" and
                    receipt["bytes"] == receipt["verified_bytes"] == memory_bytes and
                    receipt["copied_bytes"] == (0 if poison else memory_bytes) and receipt["dax_offset"] == 2*MIB,
                    "RAM native range/copy evidence")
            require(4096 <= align <= 2*MIB and not align & (align-1) and receipt["dax_capacity"] == config["cxl_mib"]*MIB and
                    receipt["mapping_bytes"] == (memory_bytes+align-1)//align*align and
                    receipt["mapping_bytes"]+2*MIB <= receipt["dax_capacity"], "RAM DAX mapping capacity")
            require(re.fullmatch(r"[0-9a-f]{16}", receipt["source_fnv1a64"]) and
                    receipt["source_fnv1a64"] == receipt["destination_fnv1a64"] and
                    receipt["byte_comparison"] is (not poison) and receipt["coherent_quiesced_lab_required"] is True,
                    "RAM full byte comparison/checksum")
            require(receipt["poisoned_bytes"] == (memory_bytes if poison else 0) and receipt["poison_byte"] == (165 if poison else -1),
                    "old CXL poison coverage")
            require(all(type(receipt[key]) is int and receipt[key] >= 0 for key in ("operation_ns", "verify_ns", "total_ns")) and
                    receipt["total_ns"] >= receipt["operation_ns"] + receipt["verify_ns"], "RAM native timings")
            helper_count += 1
        states = transition["state_files"]
        require(len(states) == 2 and [s["side"] for s in states] == ["source", "destination"] and
                states[0]["file"] != states[1]["file"], "source/destination state artifacts")
        data = [e.integrity(s["file"], s["integrity"]) for s in states]
        require(data[0] == data[1] and 0 < len(data[0]) <= 262144, "serialized state bytes differ")
        for side, state in enumerate(states):
            require(state["sha256"] == sha(data[side]) and state["bytes"] == len(data[side]), "state SHA256/length metadata")
            require(state["integrity"]["path"] == phases[1+side]["request"]["snapshot_path"], "fetched state path differs from API")
        mailbox = transition["mailbox"]
        require(len(mailbox) == (2 if source != target else 0), "mailbox phase count")
        for position, item in enumerate(mailbox):
            value = e.native(item, "STATE_MAILBOX_JSON ")
            phase = "publish" if position == 0 else "receive"
            expected = {"phase": phase, "role": source if position == 0 else target, "source_role": source,
                        "round": 1 if source == "A" else 2, "schema": "534e415053484f02", "offset": MIB,
                        "capacity": 262144, "memory_bytes": memory_bytes, "state_bytes": len(data[0]), "state_hash": fnv(data[0])}
            require(all(value.get(k) == v for k, v in expected.items()), "mailbox layout/role/payload mismatch")
            require(re.fullmatch(r"[0-9a-f]{16}", value["nonce"]), "mailbox nonce format")
            if position == 0 and source == "A":
                require(value["nonce"] not in mailbox_sessions, "mailbox nonce reused across cycles")
                mailbox_nonce = value["nonce"]
                mailbox_sessions.add(mailbox_nonce)
            require(value["nonce"] == mailbox_nonce, "mailbox nonce changed before return")
        e.integrity(transition["source_log"]["file"], transition["source_log"]["integrity"])

    # Reconstruct each command's place in the experiment rather than trusting
    # reported totals; identity commands surround every pause and restore.
    expected_events = []
    for index, transition in enumerate(transitions):
        commands = r["commands"][index*6:index*6+6]
        require([c["operation"] for c in commands] == ["write", "read", "read", "sync", "evict", "coldread"], "workload command sequence")
        expected_events.extend(commands[:2] + [transition["before"], transition["after"]] + commands[2:])
        source, source_tier, target, tier = expected_graph[index % 4]
        require(all((c["role"], c["tier"]) == (source, source_tier) for c in commands[:2]) and
                all((c["role"], c["tier"]) == (target, tier) for c in commands[2:]), "workload command location")
        write = commands[0]
        generation = index+1
        expected_mode = "sync" if source == "B" and (index//4)%2 == 0 else ("dirty" if generation%2 else "sync")
        if not pilot:
            require(transition["require_dirty"] is (expected_mode == "dirty"), "dirty pause precondition mismatch")
        require(write["arguments"] == [generation, expected_mode] and
                commands[1]["arguments"] == commands[2]["arguments"] == [generation], "write/read generation arguments")
        before = nfs_write_counters(e, write["nfs_before"])
        after = nfs_write_counters(e, write["nfs_after"])
        changes = {key: after[key]-before[key] for key in before}
        require(all(value >= 0 for value in changes.values()) and write["nfs_write_delta"] == changes["write"] and
                write["nfs_commit_delta"] == changes["commit"], "NFS write delta mismatch")
        if source == "A": require(changes["write"] == 0, "home writes unexpectedly use NFS")
        elif expected_mode == "sync": require(changes["write"] > 0, "remote synced write has no NFS WRITE")
        e.in_order(write["nfs_before"]["file"], write["file"], write["nfs_after"]["file"],
                   commands[1]["file"], transition["before"]["file"], transition["after"]["file"],
                   *(c["file"] for c in commands[2:]))
        if expected_mode == "dirty":
            require(write["receipt"]["dirty_kib"] > 0 and transition["before"]["receipt"]["dirty_kib"] > 0,
                    "dirty guest pages missing before pause")
        hashes = [c["receipt"]["mutable_hash"] for c in commands[:3]]
        require(len(set(hashes)) == 1, "mutable data changed across handoff")
        cold = commands[-1]
        require(cold["arguments"] == [0, 8*MIB], "cold read range")
        before = nfs_read_counters(e, cold["nfs_before_file"])
        after = nfs_read_counters(e, cold["nfs_after_file"])
        changes = {key: after[key]-before[key] for key in before}
        require(changes == cold["nfs_delta"] and all(value >= 0 for value in changes.values()), "NFS cold read delta mismatch")
        require((changes["nfs_read_calls"] > 0) == (target == "B") and
                (changes["mount_read_ops"] > 0) == (target == "B"), "cold read used wrong host backing path")
        require(cold["receipt"]["payload_resident_before"] == 0 and cold["receipt"]["block_delta"]["read_sectors"] > 0,
                "cold read did not miss guest cache and use virtio-blk")
    expected_events.extend(r["commands"][-2:])
    require([c["operation"] for c in r["commands"][-2:]] == ["sync", "quit"], "final sync/quit missing")
    require(len({item["file"] for item in all_events}) == len(all_events), "guest evidence file reused")
    cursor, current_generation, reference = 0, 0, None
    labels = set()
    for number, item in enumerate(expected_events, 1):
        value = guest(item)
        require(value["command"] == number and value["label"] not in labels, "guest command sequence duplicated/skipped")
        labels.add(value["label"])
        require(value["boot_id"] == initial["boot_id"] and value["pid"] == initial["pid"], "guest reboot/process replacement")
        if reference is None: reference = value
        require(all(value[key] == reference[key] for key in IDENTITY) and value["unlinked_nlink"] == 0,
                "retained guest FD identity changed")
        if value["op"] == "write": current_generation += 1
        if value["op"] == "read": cursor += 16
        require(value["generation"] == current_generation and value["cursor_offset"] == value["unlinked_offset"] == cursor,
                "file generation or retained FD offset regressed")
        require(value["mutable_bytes"] == initial["mutable_bytes"] and value["payload_bytes"] == initial["payload_bytes"], "file sizes changed")
        if value["op"] in ("write", "read", "identity"):
            require(value["mapped_hash"] == pattern_hash("mapped", current_generation, 4096), "mapped file independent pattern mismatch")
        if full_patterns and value["op"] in ("write", "read"):
            require(value["mutable_hash"] == pattern_hash("mutable", current_generation, initial["mutable_bytes"]), "mutable independent pattern mismatch")
        if full_patterns and value["op"] == "coldread":
            require(value["payload_hash"] == pattern_hash("payload", 0, item["arguments"][1], item["arguments"][0]), "cold payload independent pattern mismatch")
        expected_sync = value["op"] in ("sync", "quit") or (value["op"] == "write" and item["arguments"][1] == "sync")
        require(value["synced"] is expected_sync and value["block_delta"] is not None and
                all(type(x) is int and x >= 0 for x in value["block_delta"].values()), "guest I/O evidence")
    require(current_generation == r["final_generation"] == 4*cycles, "final generation incomplete")
    final = boot(r["fresh_boot_audit"], True)
    require(final["boot_id"] != initial["boot_id"] and final["generation"] == current_generation and
            final["mutable_hash"] == r["commands"][-8]["receipt"]["mutable_hash"] and
            final["mapped_hash"] == pattern_hash("mapped", current_generation, 4096), "fresh guest lost persistent state")
    require(final["mutable_bytes"] == initial["mutable_bytes"] and final["payload_bytes"] == initial["payload_bytes"], "fresh audit file sizes")
    if full_patterns:
        require(final["payload_hash"] == pattern_hash("payload", 0, initial["payload_bytes"]) and
                final["cursor_hash"] == pattern_hash("cursor", 0, 65536), "fresh audit independent persistent pattern")
    image(e, r["final_image"])

    final_fences = [row for row in r["receipts"] if row["kind"] == "final-fence"]
    require(len(final_fences) == 1, "final original guest fence missing")
    fence(e, final_fences[0]["file"])
    e.in_order(r["commands"][-1]["file"], final_fences[0]["file"], r["final_image"]["file"],
               r["fresh_boot_audit"]["api"][0]["file"])
    log_entries = r.get("firecracker_logs", [])
    if not pilot:
        require(len(log_entries) == len(transitions)+2, "complete Firecracker logs missing")
        require(r["storage_cleanly_unmounted"] is True, "host storage cleanup did not finish")
        for kind in ("remote-unmounted", "home-unmounted"):
            rows = [row for row in r["receipts"] if row["kind"] == kind]
            require(len(rows) == 1 and not any(row[1] in ("/disk", "/storage", "/remote")
                    for row in mounts(e.text(rows[0]["file"]))), "host storage remains mounted")
    observed_events, ready_events, audit_events, guest_mounts = [], [], [], []
    used_logs = set()
    for index, entry in enumerate(log_entries):
        require(entry["file"] not in used_logs, "duplicate Firecracker log")
        used_logs.add(entry["file"])
        text = e.integrity(entry["file"], entry["integrity"]).decode().replace("\r", "")
        require(entry["sha256"] == entry["integrity"]["sha256"] and
                entry["integrity"]["path"] == entry["directory"] + "/fc.log", "full log metadata mismatch")
        expected_role = "A" if index in (0, len(transitions)+1) else transitions[index-1]["destination"]
        require(entry["role"] == expected_role and entry["directory"].endswith(f"/fc-{index:03d}"), "full log process graph")
        require(not FATAL.search(text), "guest or backend error in Firecracker log")
        for line in text.splitlines():
            if line.startswith('{"event":'):
                value = json.loads(line)
                if value["event"] == "home_storage": observed_events.append(value)
                elif value["event"] == "home_ready": ready_events.append(value)
                elif value["event"] == "home_audit": audit_events.append(value)
            if line.startswith("HOME_STORAGE_MOUNT "):
                guest_mounts.append(line.removeprefix("HOME_STORAGE_MOUNT ").split())
    if not pilot:
        require(ready_events == [initial] and audit_events == [final], "fresh versus restored boot count")
        require(sorted(observed_events, key=lambda x: x["command"]) == [x["receipt"] for x in expected_events], "full logs differ from command observations")
        require(len(guest_mounts) == 2 and all(len(row) == 6 and row[:3] == ["/dev/vda", "/data", "ext4"] for row in guest_mounts), "guest ext4 mount evidence")
        require("rw" in guest_mounts[0][3].split(",") and "ro" in guest_mounts[1][3].split(","), "guest writable/fresh-readonly mount")
        require("HOME_STORAGE_UNMOUNTED" in e.text(log_entries[-2]["file"]) and
                "HOME_STORAGE_AUDIT_DONE" in e.text(log_entries[-1]["file"]), "clean guest/audit unmount markers missing")
    for index, transition in enumerate(transitions):
        text = e.text(transition["source_log"]["file"])
        if not pilot:
            require(e.read(transition["source_log"]["file"]) == e.read(log_entries[index]["file"]), "retired process log changed")
        require(not FATAL.search(text), "error in source Firecracker log")
        before = transition["before"]["receipt"]
        rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"event":"home_storage"')]
        require(sum(value == before for value in rows) == 1, "before identity absent from source full log")
    registered_guest = [x["file"] for x in r["receipts"] if x["kind"] == "guest"]
    require(set(registered_guest) == {x["file"] for x in expected_events} | {r["boot"]["file"], r["fresh_boot_audit"]["file"]},
            "unaccounted guest receipt")
    return {"result": "passed", "evidence_level": "pilot" if pilot else "complete", "cycles": cycles,
            "transitions": len(transitions), "commands": len(r["commands"]), "guest_events": len(expected_events),
            "native_api_phases": api_count, "native_ram_operations": helper_count, "mailbox_phases": cycles*4,
            "state_files_byte_compared": len(transitions)*2, "cold_reads_local": cycles*3, "cold_reads_nfs": cycles,
            "mapped_patterns_independently_recomputed": current_generation, "full_data_patterns": full_patterns,
            "fresh_guest_disk_audit": True, "source_files": len(r["runtime_sources"]), "full_firecracker_logs": len(log_entries),
            "limitations": ["Coherent nested-KVM lab, not physical multi-host or noncoherent CXL validation",
                            "Native RAM full byte comparisons verified by receipts; full RAM payload not archived",
                            "Fresh clean guest boot verifies disk persistence, not power-loss durability",
                            "API response bytes are native receipts; request metadata comes from measured controller",
                            "Host cache drops are laboratory control, not a distributed storage coherence protocol"]}


def negative_tests(report_path, pilot=False, full_patterns=False):
    original = json.loads(Path(report_path).read_text())
    directory = Path(report_path).parent
    cases = []

    def raw_update(report, overrides, name, data):
        overrides[name] = data
        for row in report["receipts"]:
            if row["file"] == name: row["sha256"] = sha(data)

    def native_update(report, overrides, item, prefix, changes, key="receipt"):
        item[key].update(changes)
        name = item["file"]
        text = overrides.get(name, (directory/name).read_bytes()).decode()
        lines = text.splitlines(keepends=True)
        matched = 0
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = prefix + json.dumps(item[key], separators=(",", ":")) + "\n"
                matched += 1
        require(matched == 1, "negative fixture native row missing")
        raw_update(report, overrides, name, "".join(lines).encode())

    def guest_update(report, overrides, item, changes):
        item["receipt"].update(changes)
        name = item["file"]
        lines = overrides.get(name, (directory/name).read_bytes()).decode().splitlines(keepends=True)
        matched = 0
        for index, line in enumerate(lines):
            if line.startswith('{"event":'):
                lines[index] = json.dumps(item["receipt"], separators=(",", ":")) + "\n"
                matched += 1
        require(matched == 1, "negative fixture guest row missing")
        raw_update(report, overrides, name, "".join(lines).encode())

    for name in ("source_hash", "cycle_count", "wrong_lifecycle_tier", "missing_transition", "state_byte_corruption",
                 "state_fetch_unstable", "source_fence_running", "ram_reserved_prefix", "ram_copy_underreported",
                 "ram_hash_disagreement", "missing_poison", "poison_short_range", "mailbox_nonce", "mailbox_payload_hash",
                 "guest_boot_changed", "guest_fd_offset_reset", "mapped_pattern", "generation_jump", "nfs_write_counter",
                 "nfs_read_delta", "local_backing_wrong_filesystem", "destination_image_stale", "fresh_boot_reused",
                 "fresh_audit_data_changed", "api_status", "api_restore_path", "duplicate_guest_event", "cleanup_failure",
                 "poison_after_resume", "state_wrong_fetch_path", "final_fence_missing"):
        r, overrides = copy.deepcopy(original), {}
        t = r["transitions"]
        if name == "source_hash": r["runtime_sources"]["home_storage_probe.c"] = "0"*64
        elif name == "cycle_count": r["arguments"]["cycles"] += 1
        elif name == "wrong_lifecycle_tier": t[0]["destination_tier"] = "dram"
        elif name == "missing_transition": t.pop()
        elif name == "state_byte_corruption":
            state = t[0]["state_files"][1]
            data = bytearray((directory/state["file"]).read_bytes()); data[-1] ^= 1
            overrides[state["file"]] = bytes(data)
            state["sha256"] = state["integrity"]["sha256"] = sha(data)
        elif name == "state_fetch_unstable": t[0]["state_files"][1]["integrity"]["source_unchanged_during_fetch"] = False
        elif name == "source_fence_running":
            f = t[0]["source_fence_file"]
            text = (directory/f).read_text()
            raw_update(r, overrides, f, re.sub(r"FENCE_STATE \S+ \d+", "FENCE_STATE Running 1", text).encode())
        elif name == "ram_reserved_prefix": native_update(r, overrides, t[0]["ram"][0], "RAM_TRANSFER_JSON ", {"dax_offset": 0})
        elif name == "ram_copy_underreported": native_update(r, overrides, t[0]["ram"][0], "RAM_TRANSFER_JSON ", {"copied_bytes": 0})
        elif name == "ram_hash_disagreement": native_update(r, overrides, t[0]["ram"][0], "RAM_TRANSFER_JSON ", {"destination_fnv1a64": "0"*16})
        elif name == "missing_poison": t[3]["ram"].pop()
        elif name == "poison_short_range": native_update(r, overrides, t[3]["ram"][-1], "RAM_TRANSFER_JSON ", {"poisoned_bytes": 4096})
        elif name == "mailbox_nonce": native_update(r, overrides, t[1]["mailbox"][1], "STATE_MAILBOX_JSON ", {"nonce": "0"*16})
        elif name == "mailbox_payload_hash": native_update(r, overrides, t[1]["mailbox"][0], "STATE_MAILBOX_JSON ", {"state_hash": "0"*16})
        elif name == "guest_boot_changed": guest_update(r, overrides, r["commands"][2], {"boot_id": "changed"})
        elif name == "guest_fd_offset_reset": guest_update(r, overrides, r["commands"][2], {"cursor_offset": 0})
        elif name == "mapped_pattern": guest_update(r, overrides, r["commands"][0], {"mapped_hash": "0"*16})
        elif name == "generation_jump": r["commands"][0]["arguments"][0] = 2
        elif name == "nfs_write_counter": r["commands"][0]["nfs_after"]["write"] += 1
        elif name == "nfs_read_delta": r["commands"][5]["nfs_delta"]["nfs_read_calls"] += 1
        elif name == "local_backing_wrong_filesystem":
            f = next(row["file"] for row in r["receipts"] if row["kind"] == "home-storage")
            raw_update(r, overrides, f, (directory/f).read_text().replace(" /disk ext4 ", " /disk tmpfs ").encode())
        elif name == "destination_image_stale":
            item = t[0]["destination_image"]; item["sha256"] = "0"*64
            raw_update(r, overrides, item["file"], ("0"*64+"  /disk/sandbox.raw\n").encode())
        elif name == "fresh_boot_reused": guest_update(r, overrides, r["fresh_boot_audit"], {"boot_id": r["boot"]["receipt"]["boot_id"]})
        elif name == "fresh_audit_data_changed": guest_update(r, overrides, r["fresh_boot_audit"], {"mutable_hash": "0"*16})
        elif name == "api_status": native_update(r, overrides, t[0]["api"][0], "LAB_API_JSON ", {"status": 400}, "response")
        elif name == "api_restore_path": t[0]["api"][2]["request"]["mem_backend"]["backend_path"] = "/tmp/wrong"
        elif name == "duplicate_guest_event": r["commands"][1] = copy.deepcopy(r["commands"][0])
        elif name == "cleanup_failure": r["cleanup_errors"] = ["owned process remains"]
        elif name == "poison_after_resume":
            poison, resume = t[3]["ram"][-1], t[3]["api"][-1]
            old_poison, old_resume = poison["file"], resume["file"]
            raw_update(r, overrides, old_poison, (directory/old_resume).read_bytes())
            raw_update(r, overrides, old_resume, (directory/old_poison).read_bytes())
            poison["file"], resume["file"] = old_resume, old_poison
        elif name == "state_wrong_fetch_path": t[0]["state_files"][0]["integrity"]["path"] = "/run/wrong/state"
        elif name == "final_fence_missing":
            next(row for row in r["receipts"] if row["kind"] == "final-fence")["kind"] = "not-a-fence"
        try:
            audit(report_path, r, overrides, pilot, full_patterns)
        except (AssertionError, KeyError, ValueError) as exc:
            cases.append({"test": name, "result": "rejected", "reason": str(exc)})
        else:
            raise AssertionError(f"negative evidence accepted: {name}")
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pilot", action="store_true", help="Allow early pilot without complete final FC logs; label output accordingly")
    parser.add_argument("--full-patterns", action="store_true", help="Recompute complete deterministic data hashes; CPU-intensive, run after VMs stop")
    parser.add_argument("--negative-tests", action="store_true")
    args = parser.parse_args()
    result = audit(args.report, pilot=args.pilot, full_patterns=args.full_patterns)
    if args.negative_tests: result["negative_tests"] = negative_tests(args.report, args.pilot, args.full_patterns)
    result.update(report_sha256=sha(args.report.read_bytes()), verifier_sha256=sha(Path(__file__).read_bytes()))
    args.out.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({"result": result["result"], "transitions": result["transitions"],
                      "negative_rejections": len(result.get("negative_tests", [])), "output": str(args.out)}))


if __name__ == "__main__": main()
