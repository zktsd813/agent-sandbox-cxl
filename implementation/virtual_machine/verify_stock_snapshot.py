#!/usr/bin/env python3
"""Independently replay stock warm-snapshot sharing and NFS-restore evidence.

This verifier does not import the benchmark or the four-arm verifier. It checks
recorded observations; it does not start VMs or treat a snapshot's configured
MAP_PRIVATE flag as proof of physical sharing.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re

PAGE = 4096
MIB = 1024 * 1024
PAYLOAD = 160 * MIB
MEMORY = 512 * MIB
PAGES = PAYLOAD // PAGE
PFN_MASK = (1 << 55) - 1


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Raw:
    def __init__(self, report, root):
        self.report, self.root = report, root
        self.data, self.order, self.kinds = {}, {}, {}
        for index, receipt in enumerate(report["receipts"]):
            name = receipt["file"]
            require(Path(name).name == name and name not in self.data, "unsafe/duplicate receipt")
            value = (root / name).read_bytes()
            require(hashlib.sha256(value).hexdigest() == receipt["sha256"], "raw receipt hash: " + name)
            self.data[name], self.order[name], self.kinds[name] = value, index, receipt["kind"]

    def text(self, name):
        return self.data[name].decode().replace("\r", "")

    def command_text(self, name):
        # BusyBox echoes long input lines with CR CR LF soft wraps. This is
        # only for parsing the echoed command, never for parsing JSON output.
        return self.data[name].decode().replace("\r\r\n", "").replace("\r", "")

    def record(self, name, prefix):
        rows = [json.loads(line[len(prefix):]) for line in self.text(name).splitlines()
                if line.startswith(prefix)]
        require(len(rows) == 1, "missing/duplicate raw JSON: " + name)
        return rows[0]

    def shell_ok(self, name):
        codes = re.findall(r"^LAB_COMMAND_DONE_[a-f0-9]+:(\d+)$", self.text(name), re.M)
        require(codes and codes[-1] == "0", "shell command did not succeed: " + name)

    def guest(self, row, operation=None):
        values = [json.loads(line) for line in self.text(row["file"]).splitlines()
                  if line.startswith('{"event":"pool_guest"')]
        values = [r for r in values if r["label"] == row["receipt"]["label"]]
        require(values == [row["receipt"]], "guest result differs from raw bytes")
        r = values[0]
        require(r["ok"] and r["payload_bytes"] == PAYLOAD, "guest failure/payload size")
        require(r["op"] == row["operation"] and (operation is None or r["op"] == operation),
                "guest operation mismatch")
        require(r["timing_clock"] == "CLOCK_MONOTONIC_RAW" and
                r["pinned_cpu"] == r["cpu"] == r["cpu_after"] == 0,
                "guest affinity/clock changed")
        if r["op"] in ("warm", "check"):
            expected = self.report["fixture_manifest"]["pool"]["image"]["generator"]["hash"]
            require(r["payload_hash"] == expected and r["checked_payload_words"] == PAYLOAD // 8,
                    "full payload validation missing")
            require(r["resident_after"] == PAGES, "payload not fully guest resident")
        self.shell_ok(row["file"])
        return r

    def api(self, row):
        name = row["file"]
        actual = self.record(name, "LAB_API_JSON ")
        require(actual == row["response"] and actual["status"] == 204, "API acknowledgment mismatch")
        command = self.command_text(name)
        start = command.find("'{")
        require(start >= 0, "API request JSON absent")
        request, length = json.JSONDecoder().raw_decode(command[start+1:])
        require(command[start+1+length] == "'" and request == row["request"],
                "API request differs from raw command")
        owner = re.findall(r"/vm(\d+)-epoch(\d+)/api\.sock", command)
        require(len(owner) == 1 and row["path"] in command and row["method"] in command,
                "API endpoint/owner missing")
        self.shell_ok(name)
        return int(owner[0][0]), int(owner[0][1])

    def fence(self, name, pid, start):
        text = self.text(name)
        found = re.findall(r"^FENCED (\d+) (\d+)$", text, re.M)
        require(found == [(str(pid), str(start))], "fence refers to another VMM incarnation")
        states = re.findall(r"^FENCE_STATE (\S+) (\d+)$", text, re.M)
        require(len(states) == 1, "source fence state missing")
        state, actual = states[0][0], int(states[0][1])
        require((state == "missing" and actual == 0) or
                (state in ("Z", "X") and actual == start) or
                (state == "reused" and actual != start), "source remains live")
        self.shell_ok(name)


def maps_from_raw(text):
    result = []
    for line in text.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5 or not re.fullmatch(r"[0-9a-f]+-[0-9a-f]+", fields[0]):
            continue
        left, right = fields[0].split("-")
        result.append(dict(start=int(left, 16), end=int(right, 16), permissions=fields[1],
                           file_offset=int(fields[2], 16), device=fields[3], inode=int(fields[4]),
                           path=fields[5] if len(fields) == 6 else ""))
    require(result, "no raw VMAs")
    return result


def inspect_mapping(raw, mapping, backing=None):
    pid, start = mapping["pid"], mapping["process_start_time"]
    owners = re.findall(r"^PROCESS (\d+) (\d+)$", raw.text(mapping["maps_file"]), re.M)
    require(owners == [(str(pid), str(start))], "mapping process incarnation mismatch")
    parsed = maps_from_raw(raw.text(mapping["maps_file"]))
    base = mapping["ram_base"]
    if backing is None:
        selected = [m for m in parsed if m["end"] - m["start"] == MEMORY and
                    m["permissions"] == "rw-p" and m["inode"] == 0 and not m["path"]]
        require(len(selected) == 1 and selected[0]["start"] == base, "fresh anonymous RAM ambiguous")
    else:
        selected = [m for m in parsed if (m["device"], m["inode"]) ==
                    (backing["device"], backing["inode"])]
        require(selected and all(m["permissions"] == "rw-p" and m["path"] == backing["path"]
                                 for m in selected), "snapshot backing identity/private mapping changed")
        require({m["start"] - m["file_offset"] for m in selected} == {base},
                "snapshot offsets do not identify one GPA-zero base")
    selected.sort(key=lambda m: m["start"])
    expected, cursor = [], base
    for m in selected:
        require(m["start"] == cursor and m["end"] <= base + MEMORY, "RAM mapping has gaps/overlap")
        expected.append(dict(m, gpa_base=m["start"]-base, hva_base=m["start"], bytes=m["end"]-m["start"]))
        cursor = m["end"]
    require(cursor == base + MEMORY and expected == mapping["segments"], "RAM segment report not raw-derived")
    executable = mapping["executable_file"]
    owner = re.findall(r"^PROCESS (\d+) (\d+)$", raw.text(executable), re.M)
    hashes = re.findall(r"^([a-f0-9]{64})  /proc/(\d+)/exe$", raw.text(executable), re.M)
    expected_binary = raw.report["configuration"]["binary_sha256"]
    require(owner == [(str(pid), str(start))] and hashes == [(expected_binary, str(pid))],
            "running stock executable hash/owner mismatch")
    require(any(m["path"] == "/lab/firecracker-stock" and "x" in m["permissions"] for m in parsed),
            "VMM executable maps do not identify stock binary")
    environment = [line.split("=", 1)[0] for line in raw.text(mapping["environment_file"]).splitlines()
                   if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line)]
    require(not any(name.startswith("FC_") for name in environment), "experimental environment in stock VMM")
    raw.shell_ok(executable)
    return pid, start


def current_cache_pfns(raw, item, backing):
    inspect_mapping(raw, item, backing)
    pin = raw.guest(item["cachemap"], "cachemap")
    require(pin["resident_before"] == pin["resident_after"] == PAGES and pin["block_read_ios"] == 0,
            "cache measurement induced a refill")
    entries = []
    for line in raw.text(item["cache_gpa_file"]).splitlines():
        fields = line.split()
        require(len(fields) in (4, 5) and fields[1] == pin["label"] and
                (fields[0], len(fields)) in (("POOL_CACHE_GPA", 4), ("POOL_CACHE_RANGE", 5)),
                "invalid guest cache record")
        offset, gpa = map(int, fields[2:4])
        count = int(fields[4]) if len(fields) == 5 else 1
        require(0 < count <= PAGES and len(entries)+count <= PAGES and
                offset % PAGE == gpa % PAGE == 0 and 0 < gpa < MEMORY,
                "guest cache range invalid")
        entries.extend((offset+i*PAGE, gpa+i*PAGE) for i in range(count))
    require([p[0] for p in entries] == list(range(0, PAYLOAD, PAGE)) and
            len({p[1] for p in entries}) == PAGES and all(gpa < MEMORY for _, gpa in entries),
            "guest file-cache coverage/uniqueness invalid")
    rows = [tuple(map(int, line.split())) for line in raw.data[item["rows_file"]].splitlines()]
    expected = [(offset, gpa, item["ram_base"]+gpa) for offset, gpa in entries]
    require(rows == expected, "GPA-to-HVA scanner input is not exact")
    values = [tuple(map(int, line.split())) for line in raw.data[item["pfns_file"]].splitlines()]
    require(len(values) == PAGES and all(len(v) == 5 for v in values), "PFN output shape")
    require([v[:3] for v in values] == rows, "PFN output/input binding mismatch")
    for _, _, _, pfn, bits in values:
        require(bits >> 63 == 1 and not bits & (1 << 62) and pfn == bits & PFN_MASK and pfn > 0,
                "absent/swapped/hidden or forged PFN")
    pfns = [v[3] for v in values]
    require(len(set(pfns)) == PAGES, "nonunique payload PFNs within one VM")
    summary = raw.record(item["summary_file"], "POOL_COMPARE_PFN_JSON ")
    require(summary == item["summary"] and summary["pid"] == item["pid"] and
            summary["process_start_time"] == item["process_start_time"] and
            summary["all_checked"] and summary["rows"] == summary["unique_pfns"] == PAGES and
            summary["bytes"] == PAYLOAD and item["current_file_cache"], "PFN summary mismatch")
    raw.shell_ok(item["summary_file"])
    return pfns


def same_guest(a, b, private=True):
    keys = ("vm", "boot_id", "pid", "cookie", "generation")
    if private:
        keys += ("private_hash", "upper_hash")
    require(all(a[k] == b[k] for k in keys), "guest execution/private state changed")


def _audit(report_path, report_override=None):
    report_path = Path(report_path)
    report_bytes = report_path.read_bytes()
    r = json.loads(report_bytes) if report_override is None else report_override
    require(r["schema"] == "pool-compare-arm-v1" and r["arm"] == "stock-snapshot" and
            r["result"] == "passed" and not r.get("cleanup_errors"), "stock snapshot run incomplete")
    require(not r["samples"] and r["configuration"]["memory_mib"] == 512 and
            r["configuration"]["vcpus"] == 2 and r["configuration"]["io_engine"] == "Async" and
            r["configuration"]["no_seccomp"] and
            r["configuration"]["independent_guest_boots"] == 1 and
            r["configuration"]["restored_clones"] == 3 and
            r["configuration"]["rounds"] == 0, "stock proof configuration changed")
    stock = r["fixture_manifest"]["comparison"]["stock"]["sha256"]
    require(r["configuration"]["binary_sha256"] == stock and
            stock != r["fixture_manifest"]["comparison"]["patched"]["sha256"], "stock/patched binary identity invalid")
    raw = Raw(r, report_path.parent)
    require(set(r["system_policy"]) == {"A", "B"}, "dedicated-host memory policies missing")
    for policy in r["system_policy"].values():
        name = policy["file"]
        lines = raw.text(name).splitlines()
        for path, expected in (("/sys/kernel/mm/ksm/run", "0"),
                               ("/sys/kernel/mm/transparent_hugepage/enabled", "[never]")):
            if "ABSENT " + path in lines:
                continue
            positions = [i for i, line in enumerate(lines) if line == "POLICY " + path]
            require(len(positions) == 1 and positions[0]+1 < len(lines), "policy value not captured")
            value = lines[positions[0]+1].strip()
            require(value == expected if expected == "0" else expected in value,
                    "KSM/THP policy differs from controlled proof")
        raw.shell_ok(name)
    image_id = r["fixture_manifest"]["pool"]["image"]["sha256"]
    require(set(r["initial_image_hashes"]) == {"A", "B"}, "both backing image identities required")
    for image in r["initial_image_hashes"].values():
        hashes = re.findall(r"^([a-f0-9]{64})  /disk/sandbox\.raw$", raw.text(image["file"]), re.M)
        require(hashes == [image_id] and image["sha256"] == image_id, "backing image digest mismatch")
    require({"pool_compare_bench.py", "pool_compare_probe.c", "pool_compare_pfn.c"} <=
            set(r["source_hashes"]), "measurement source provenance incomplete")
    for name, digest in r["source_hashes"].items():
        require(Path(name).name == name and hashlib.sha256((report_path.parent/"measured-sources"/name).read_bytes()).hexdigest() == digest,
                "measured source mismatch: " + name)
    command_files, incarnation_counters = set(), {}
    for index, row in enumerate(r["commands"]):
        command = raw.guest(row)
        require(row["file"] not in command_files and
                command["label"] == f"v{row['vm']}e{row['epoch']}c{index}",
                "guest command replay/missing sequence")
        command_files.add(row["file"])
        incarnation = (row["vm"], row["epoch"])
        if incarnation in incarnation_counters:
            require(command["command"] == incarnation_counters[incarnation]+1,
                    "guest command counter skipped/replayed within clone")
        incarnation_counters[incarnation] = command["command"]
    apis = []
    for row in r["api_timings"]:
        vm, epoch = raw.api(row)
        apis.append((row, vm, epoch))
    require(len(r["boots"]) == 1 and r["boots"][0]["vm"] == 1, "proof must start from one donor")
    boot = r["boots"][0]
    require(boot["components"]["/drives/data"]["is_read_only"] and
            boot["components"]["/drives/data"]["cache_type"] == "Writeback", "drive semantics changed")
    donor_pid, donor_start = inspect_mapping(raw, boot["mapping"])
    raw.guest(boot["warm"], "warm")
    snapshot = r["snapshot"]
    before = raw.guest(snapshot["before"], "identity")
    require(before["generation"] == 2 and before["resident_after"] == PAGES, "donor not initialized/warm")
    raw.api(snapshot["create"])
    require(snapshot["create"]["path"] == "/snapshot/create" and
            snapshot["create"]["request"]["snapshot_type"] == "Full", "donor snapshot is not full")
    backing = snapshot["identity"]
    require(snapshot["create"]["request"]["mem_file_path"] == backing["path"], "donor RAM path mismatch")
    stats = re.findall(r"^SNAPSHOT (\d+) (\d+) (\d+)$", raw.text(snapshot["inode_file"]), re.M)
    require(len(stats) == 1, "snapshot inode receipt missing")
    dev, inode, size = map(int, stats[0])
    require(size == MEMORY and inode == backing["inode"] and
            f"{os.major(dev):02x}:{os.minor(dev):02x}" == backing["device"], "snapshot backing stat mismatch")
    hashes = re.findall(r"^([a-f0-9]{64})  (\S+)$", raw.text(snapshot["inode_file"]), re.M)
    require({path for _, path in hashes} == {backing["path"], snapshot["create"]["request"]["snapshot_path"]},
            "full snapshot file checksums missing")
    raw.shell_ok(snapshot["inode_file"])
    raw.fence(snapshot["fence_file"], donor_pid, donor_start)
    require(raw.order[snapshot["before"]["file"]] < raw.order[snapshot["create"]["file"]] <
            raw.order[snapshot["inode_file"]] < raw.order[snapshot["fence_file"]], "donor snapshot/fence ordering")
    require(len(r["clones"]) == 3 and {c["controller_vm"] for c in r["clones"]} == {1, 2, 3},
            "three independent clone processes required")
    clones = {}
    for clone in r["clones"]:
        vm = clone["controller_vm"]
        pid, start = inspect_mapping(raw, clone["mapping"], backing)
        check = raw.guest(clone["check"], "check")
        same_guest(before, check)
        require(check["block_read_ios"] == 0 and clone["check"]["role"] == "A" and
                clone["check"]["epoch"] == 1, "warm clone did block IO or changed placement")
        require(raw.order[snapshot["fence_file"]] < raw.order[clone["mapping"]["maps_file"]],
                "clone observed before original donor fence")
        loads = [a for a, owner, epoch in apis if owner == vm and epoch == 1 and a["path"] == "/snapshot/load"]
        require(len(loads) == 1 and loads[0]["request"]["mem_backend"] ==
                {"backend_path": backing["path"], "backend_type": "File"} and
                loads[0]["request"]["snapshot_path"] == snapshot["create"]["request"]["snapshot_path"],
                "clones did not restore the same RAM/state files")
        require(raw.order[snapshot["fence_file"]] < raw.order[loads[0]["file"]] <
                raw.order[clone["check"]["file"]], "clone restore ordering")
        clones[vm] = (pid, start, check)
    require(len({(p, s) for p, s, _ in clones.values()}) == 3 and
            len({p for p, _, _ in clones.values()}) == 3, "clones are not distinct simultaneous VMMs")

    group = r["shared_snapshot_group"]
    require(len(group["vms"]) == 3 and {v["vm"] for v in group["vms"]} == {1, 2, 3}, "clone PFN group incomplete")
    frozen = group["frozen_file"]
    require("\nPOOL_COMPARE_GROUP_FROZEN 1 2 3\n" in raw.text(frozen), "group freeze marker missing")
    raw.shell_ok(frozen)
    scans, last_scan, pause_orders = [], 0, []
    for item in group["vms"]:
        vm = item["vm"]
        require(item["role"] == "A" and (item["pid"], item["process_start_time"]) == clones[vm][:2],
                "PFN group scanned another clone")
        scans.append(current_cache_pfns(raw, item, backing))
        pin = item["cachemap"]
        same_guest(before, pin["receipt"])
        require(raw.order[pin["file"]] < raw.order[frozen] < raw.order[item["maps_file"]] <
                raw.order[item["cache_gpa_file"]] < raw.order[item["rows_file"]] <
                raw.order[item["summary_file"]] < raw.order[item["pfns_file"]], "cache scan order invalid")
        pauses = [a for a, owner, epoch in apis if owner == vm and epoch == 1 and
                  a["path"] == "/vm" and a["request"] == {"state": "Paused"} and
                  raw.order[pin["file"]] < raw.order[a["file"]] < raw.order[frozen]]
        require(len(pauses) == 1, "clone not acknowledged paused before group scan")
        pause_orders.append(raw.order[pauses[0]["file"]])
        last_scan = max(last_scan, raw.order[item["pfns_file"]])
    require(not any(min(pause_orders) <= raw.order[a["file"]] <= last_scan and
                    a["path"] == "/vm" and a["request"] == {"state": "Resumed"}
                    for a, _, _ in apis), "a clone resumed during common PFN interval")
    sets = list(map(set, scans))
    union, intersection = len(set.union(*sets)), len(set.intersection(*sets))
    same = sum(len({scan[i] for scan in scans}) == 1 for i in range(PAGES))
    require((union, intersection, same) == (group["union_pages"], group["intersection_pages"],
                                          group["same_offset_shared_pages"]) and
            same == intersection and same*PAGE >= 128*MIB, "actual same-file-offset PFN sharing unproven")

    written = raw.guest(r["clone_write"], "write")
    require(r["clone_write"]["vm"] == 1 and written["generation"] == 3 and
            all(written[k] == before[k] for k in ("vm", "boot_id", "pid", "cookie")) and
            written["private_hash"] != before["private_hash"] and written["upper_hash"] != before["upper_hash"],
            "writer did not change its private state")
    require(raw.order[r["clone_write"]["file"]] > last_scan, "private write occurred inside frozen scan")
    require(len(r["clone_peers"]) == 2 and {p["vm"] for p in r["clone_peers"]} == {2, 3}, "peer isolation checks missing")
    for peer in r["clone_peers"]:
        same_guest(before, raw.guest(peer, "check"))
        require(raw.order[peer["file"]] > raw.order[r["clone_write"]["file"]], "peer checked before private write")

    migration = r["stock_nfs_restore"]
    continuity = raw.guest(migration["continuity"], "identity")
    same_guest(written, continuity)
    restored = raw.guest(migration["restored"], "check")
    same_guest(continuity, restored)
    require(migration["restored"]["role"] == "B" and migration["restored"]["epoch"] == 2 and
            restored["block_read_ios"] == 0, "stock full restore lost hot guest cache")
    remote_creates = [a for a, vm, epoch in apis if vm == 1 and epoch == 1 and a["path"] == "/snapshot/create"]
    remote_loads = [a for a, vm, epoch in apis if vm == 1 and epoch == 2 and a["path"] == "/snapshot/load"]
    require(len(remote_creates) == len(remote_loads) == 1 and
            remote_creates[0]["request"]["snapshot_type"] == "Full" and
            remote_loads[0]["request"]["mem_backend"]["backend_type"] == "File" and
            remote_creates[0]["role"] == "A" and remote_loads[0]["role"] == "B",
            "ordinary full snapshot A-to-B path missing")
    transport = migration["transport_file"]
    require("\nSTOCK_SNAPSHOT_TRANSPORT_VERIFIED\n" in raw.text(transport) and
            len(re.findall(r"^> cmp ", raw.text(transport), re.M)) == 2, "source full file comparison missing")
    raw.shell_ok(transport)
    raw.fence(migration["fence_file"], clones[1][0], clones[1][1])
    receives = [name for name, kind in raw.kinds.items() if kind == "snapshot-receive"]
    require(len(receives) == 1 and "\nSTOCK_SNAPSHOT_RECEIVE_VERIFIED\n" in raw.text(receives[0]) and
            len(re.findall(r"^> cmp ", raw.text(receives[0]), re.M)) == 2, "destination full file comparison missing")
    raw.shell_ok(receives[0])
    # Match every copied and compared state/RAM path to the actual API inputs.
    # A pair of unrelated successful cmp commands cannot prove this transfer.
    def transfer_pairs(name, operation):
        pairs = re.findall(r"^> " + operation + r" (\S+) (\S+)$",
                           raw.command_text(name), re.M)
        require(len(pairs) == 2 and len(set(pairs)) == 2,
                "missing/duplicate exact snapshot transfer pairs")
        return pairs

    source_pairs = transfer_pairs(transport, "cp")
    receive_pairs = transfer_pairs(receives[0], "cp")
    require(transfer_pairs(transport, "cmp") == source_pairs and
            transfer_pairs(receives[0], "cmp") == receive_pairs,
            "snapshot file comparison does not cover the copied bytes")
    created = remote_creates[0]["request"]
    loaded = remote_loads[0]["request"]
    require({source for source, _ in source_pairs} ==
            {created["snapshot_path"], created["mem_file_path"]} and
            all(destination.startswith("/disk/") for _, destination in source_pairs) and
            {source for source, _ in receive_pairs} == {destination for _, destination in source_pairs},
            "transport did not copy both created files through the shared mount")
    routes = dict(receive_pairs)
    destinations = {source: routes[transported] for source, transported in source_pairs}
    require(destinations[created["snapshot_path"]] == loaded["snapshot_path"] and
            destinations[created["mem_file_path"]] == loaded["mem_backend"]["backend_path"],
            "stock restore does not use the exact copied state and RAM files")
    ordered = [migration["continuity"]["file"], remote_creates[0]["file"], transport,
               migration["fence_file"], receives[0], remote_loads[0]["file"], migration["restored"]["file"]]
    require([raw.order[n] for n in ordered] == sorted({raw.order[n] for n in ordered}), "snapshot transport/fence/restore order invalid")
    mounts = [raw.text(name) for name, kind in raw.kinds.items() if kind == "remote-storage"]
    require(len(mounts) == 1 and any(len(fields) >= 4 and fields[1] == "/disk" and fields[2] == "nfs"
                                   and fields[0].startswith("172.30.0.1:")
                                   for fields in map(str.split, mounts[0].splitlines())), "B disk is not the host NFS image mount")
    evicted = raw.guest(migration["evicted"], "recycle")
    require(evicted["resident_after"] == 0 and evicted["checked_churn_words"] == 224*MIB//8,
            "stock NFS test did not evict/reuse file cache")
    refill = raw.guest(migration["refill"], "warm")
    same_guest(continuity, refill)
    require(refill["resident_before"] == 0 and refill["block_read_ios"] > 0 and
            refill["block_read_sectors"]*512 >= PAYLOAD, "NFS refill not guest-cold/block-backed")
    counts = []
    for key in ("nfs_before_file", "nfs_after_file"):
        name = migration[key]
        lines = [line.split() for line in raw.text(name).splitlines() if line.startswith("proc3 ")]
        require(len(lines) == 1 and int(lines[0][1]) == 22 and len(lines[0]) >= 24, "NFSv3 counters missing")
        counts.append(int(lines[0][8]))
        raw.shell_ok(name)
    require(counts[1] - counts[0] == migration["nfs_read_delta"] > 0, "raw NFS READ delta invalid")
    ordered = [migration["restored"]["file"], migration["evicted"]["file"], migration["nfs_before_file"],
               migration["refill"]["file"], migration["nfs_after_file"]]
    require([raw.order[n] for n in ordered] == sorted({raw.order[n] for n in ordered}), "NFS refill measurement order invalid")
    return {
        "result": "passed", "arm": "stock-snapshot", "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "stock_binary_sha256": stock, "raw_receipts": len(raw.data),
        "raw_receipt_bytes": sum(map(len, raw.data.values())), "measured_sources": len(r["source_hashes"]),
        "independent_donors": 1, "simultaneous_clone_vmm_processes": 3,
        "deliberately_identical_initial_guest_identity": True, "same_snapshot_inode": backing,
        "current_cache_bytes_per_clone": PAYLOAD, "payload_unique_host_frame_bytes": union*PAGE,
        "three_clone_same_offset_shared_bytes": same*PAGE, "all_three_paused_during_pfn_scans": True,
        "private_writer_changed_and_two_peers_unchanged": True,
        "source_incarnations_fenced": 2, "donor_snapshot_ram_bytes_observed": MEMORY,
        "remote_snapshot_type": "Full", "exact_state_and_ram_transport_paths_verified": True,
        "stock_restore_route": "A/DRAM -> B/DRAM; host image backend local -> NFS",
        "restored_hot_payload_block_reads": restored["block_read_ios"],
        "nfs_cold_guest_read_bytes": refill["block_read_sectors"]*512,
        "nfs_read_rpc_delta": counts[1]-counts[0], "guest_commands": len(r["commands"]),
        "scope": "Recorded same-host clean snapshot-file sharing and ordinary stock full restore with host NFS; no performance, net-capacity, or noncoherent-CXL claim.",
    }


def audit(report_path):
    """Verify one completed stock-snapshot arm and return derived observations."""
    return _audit(report_path)


def negative_tests(report_path):
    """Reject changed claims while preserving every archived evidence byte."""
    original = json.loads(Path(report_path).read_text())
    cases = [
        ("false_success", ("result",), "running"),
        ("false_shared_count", ("shared_snapshot_group", "same_offset_shared_pages"),
         original["shared_snapshot_group"]["same_offset_shared_pages"] + 1),
        ("historical_cache_claim", ("shared_snapshot_group", "vms", 0, "current_file_cache"), False),
        ("wrong_snapshot_inode", ("snapshot", "identity", "inode"), original["snapshot"]["identity"]["inode"] + 1),
        ("wrong_clone_vmm", ("clones", 0, "mapping", "pid"), original["clones"][0]["mapping"]["pid"] + 1),
        ("lost_clone_private_state", ("clone_peers", 0, "receipt", "private_hash"), "bad"),
        ("wrong_source_fence", ("stock_nfs_restore", "fence_file"), original["snapshot"]["fence_file"]),
        ("lost_hot_cache", ("stock_nfs_restore", "restored", "receipt", "block_read_ios"), 1),
        ("wrong_nfs_read_delta", ("stock_nfs_restore", "nfs_read_delta"), original["stock_nfs_restore"]["nfs_read_delta"] + 1),
        ("wrong_raw_digest", ("receipts", 0, "sha256"), "0" * 64),
    ]
    rejected = []
    for name, path, replacement in cases:
        altered = copy.deepcopy(original)
        target = altered
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        try:
            _audit(report_path, altered)
        except (ValueError, KeyError, OSError):
            rejected.append(name)
        else:
            raise ValueError("stock proof accepted manipulated evidence: " + name)
    return rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--negative-tests", action="store_true")
    args = parser.parse_args()
    result = audit(args.report)
    if args.negative_tests:
        result["rejected_evidence_mutations"] = negative_tests(args.report)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
