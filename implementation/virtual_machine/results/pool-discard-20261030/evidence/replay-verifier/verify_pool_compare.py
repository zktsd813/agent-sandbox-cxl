#!/usr/bin/env python3
"""Offline comparison audit; derive sharing and timing from raw evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics

PAGE = 4096
MIB = 1024 * 1024
PFN_MASK = (1 << 55) - 1
ARMS = ("stock-dram", "patched-dram-disabled", "patched-cxl-hints-unpooled",
        "patched-cxl-pooled")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Evidence:
    def __init__(self, report, root):
        self.report, self.root = report, root
        self.blobs, self.order = {}, {}
        for index, item in enumerate(report["receipts"]):
            name = item["file"]
            require(Path(name).name == name and name not in self.blobs,
                    "unsafe or duplicate receipt filename")
            data = (root / name).read_bytes()
            require(sha(data) == item["sha256"], "receipt hash mismatch: " + name)
            self.blobs[name], self.order[name] = data, index

    def text(self, name):
        return self.blobs[name].decode()

    def console_command(self, name):
        # The L1 serial terminal inserts visual line wraps even inside words.
        # JSON output records are unwrapped; only echoed command matching uses this.
        return self.text(name).split("LAB_API_JSON ", 1)[0].replace("\r", "").replace("\n", "")

    def record(self, name, prefix):
        records = [json.loads(line[len(prefix):]) for line in self.text(name).splitlines()
                   if line.startswith(prefix)]
        require(len(records) == 1, "missing or ambiguous raw record: " + name)
        return records[0]

    def command(self, row):
        raw = [json.loads(line) for line in self.text(row["file"]).splitlines()
               if line.startswith('{"event":"pool_guest"')]
        matches = [value for value in raw if value["label"] == row["receipt"]["label"]]
        require(len(matches) == 1 and matches[0] == row["receipt"],
                "guest result differs from raw receipt")
        value = matches[0]
        require(value["ok"] and value["payload_bytes"] == 160 * MIB,
                "guest workload failed or changed size")
        require(value["timing_clock"] == "CLOCK_MONOTONIC_RAW" and
                value["pinned_cpu"] == value["cpu"] == value["cpu_after"] == 0 and
                isinstance(value["elapsed_ns"], int) and value["elapsed_ns"] >= 0,
                "invalid guest timing or CPU affinity")
        if value["op"] in ("warm", "check"):
            expected = self.report["fixture_manifest"]["pool"]["image"]["generator"]["hash"]
            require(value["checked_payload_words"] == 160 * MIB // 8 and
                    value["payload_hash"] == expected and
                    value["resident_after"] == 160 * MIB // PAGE,
                    "complete resident payload oracle missing")
        return value

    def guest_pages(self, name, label):
        pages = []
        for line in self.text(name).splitlines():
            fields = line.split()
            if not fields or fields[0] not in ("POOL_CACHE_RANGE", "POOL_CACHE_GPA"):
                continue
            require(fields[1] == label, "cache oracle label mismatch")
            require((fields[0], len(fields)) in (("POOL_CACHE_RANGE", 5),
                                               ("POOL_CACHE_GPA", 4)),
                    "invalid cache oracle shape")
            offset, gpa = map(int, fields[2:4])
            count = int(fields[4]) if len(fields) == 5 else 1
            require(offset % PAGE == gpa % PAGE == 0 and gpa > 0 and
                    0 < count <= 160 * MIB // PAGE and
                    len(pages) + count <= 160 * MIB // PAGE,
                    "invalid or oversized cache oracle")
            pages.extend((offset + i * PAGE, gpa + i * PAGE) for i in range(count))
        require([offset for offset, _ in pages] == list(range(0, 160 * MIB, PAGE)) and
                len({gpa for _, gpa in pages}) == len(pages),
                "cache oracle does not cover every distinct payload page")
        return pages

    def pfn_rows(self, name):
        rows = [tuple(map(int, line.split())) for line in self.blobs[name].splitlines()]
        require(len(rows) == 160 * MIB // PAGE and all(len(row) == 5 for row in rows),
                "PFN scan size or shape changed")
        require([row[0] for row in rows] == list(range(0, 160 * MIB, PAGE)),
                "PFN scan file offsets incomplete")
        for offset, gpa, hva, pfn, raw in rows:
            require(gpa % PAGE == hva % PAGE == 0 and gpa > 0 and hva > 0 and
                    raw >> 63 == 1 and not raw & (1 << 62) and
                    0 < pfn == raw & PFN_MASK, "invalid actual host PFN evidence")
        for column in (1, 2, 3):
            require(len({row[column] for row in rows}) == len(rows),
                    "duplicate GPA, HVA or PFN within one random payload")
        return rows


def timings(values):
    require(values, "no timing samples")
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": statistics.median(values) / 1e6,
            "minimum_ms": ordered[0] / 1e6, "maximum_ms": ordered[-1] / 1e6}


def load_child(root, record):
    relative = Path(record["report_file"])
    require(not relative.is_absolute() and ".." not in relative.parts,
            "unsafe child report path")
    path = root / relative
    data = path.read_bytes()
    require(sha(data) == record["sha256"], "child report hash mismatch")
    return json.loads(data), path.parent


def parse_maps(text):
    mappings = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]+)-([0-9a-f]+) ([-rwxps]{4}) ([0-9a-f]+) (\S+) (\d+)\s*(.*)", line)
        if match:
            start, end, permissions, offset, device, inode, path = match.groups()
            mappings.append(dict(start=int(start, 16), end=int(end, 16), permissions=permissions,
                                 file_offset=int(offset, 16), device=device, inode=int(inode), path=path))
    require(mappings, "no raw process mappings")
    return mappings


def paused_scan(evidence, scan, after=None):
    start, end = evidence.order[scan["maps_file"]], evidence.order[scan["pfns_file"]]
    socket = f"/vm{scan['vm']}-epoch0/api.sock"
    apis = [api for api in evidence.report["api_timings"]
            if api["path"] == "/vm" and api["role"] == scan["role"] and
            socket in evidence.console_command(api["file"])]
    prior = [api for api in apis if evidence.order[api["file"]] < start]
    require(prior and prior[-1]["request"] == {"state": "Paused"}, "PFN scan did not start paused")
    paused = evidence.order[prior[-1]["file"]]
    if after is not None:
        require(after < paused, "pause acknowledgment predates the operation being measured")
    require(not any(paused < evidence.order[api["file"]] < end for api in apis),
            "VMM changed state during PFN scan")


def pfn_scan(evidence, scan):
    paused_scan(evidence, scan, evidence.order[scan["cachemap"]["file"]]
                if scan["current_file_cache"] else None)
    rows = evidence.pfn_rows(scan["pfns_file"])
    inputs = [tuple(map(int, line.split())) for line in evidence.blobs[scan["rows_file"]].splitlines()]
    require(inputs == [row[:3] for row in rows], "PFN reader input/output correspondence lost")
    summary = evidence.record(scan["summary_file"], "POOL_COMPARE_PFN_JSON ")
    require(summary == scan["summary"] and summary["all_checked"] and
            summary["rows"] == summary["unique_pfns"] == len(rows) and
            summary["bytes"] == len(rows) * PAGE and
            summary["pid"] == scan["pid"] and
            summary["process_start_time"] == scan["process_start_time"],
            "PFN summary or process identity altered")
    raw_maps = evidence.text(scan["maps_file"])
    identities = re.findall(r"^PROCESS (\d+) (\d+)\s*$", raw_maps, re.M)
    require(identities == [(str(scan["pid"]), str(scan["process_start_time"]))],
            "mapping process does not match PFN process")
    executable = re.findall(r"^([0-9a-f]{64})  /proc/(\d+)/exe\s*$",
                            evidence.text(scan["executable_file"]), re.M)
    require(executable == [(evidence.report["configuration"]["binary_sha256"], str(scan["pid"]))],
            "running executable hash does not match selected arm")
    environment = dict(re.findall(r"^([A-Z_][A-Z_0-9]*)=([^\r\n]*)$",
                                  evidence.text(scan["environment_file"]).replace("\r", ""), re.M))
    expected_env = evidence.report["configuration"]["env_by_vm"][f"{scan['vm']}:0"]
    require(environment == expected_env, "observed VMM environment differs from primary arm")
    if "cxl" not in evidence.report["arm"]:
        require(not any(key.startswith("FC_") for key in environment),
                "stock/disabled process contains experimental environment")
    mappings = parse_maps(raw_maps)
    base = scan["ram_base"]
    boots = [boot for boot in evidence.report["boots"] if boot["vm"] == scan["vm"]]
    require(len(boots) == 1, "PFN scan lacks its independent boot mapping")
    boot_mapping = boots[0]["mapping"]
    require(base == boot_mapping["ram_base"] and scan["pid"] == boot_mapping["pid"] and
            scan["process_start_time"] == boot_mapping["process_start_time"],
            "PFN scan moved away from its original VMM memory")
    boot_maps = parse_maps(evidence.text(boot_mapping["maps_file"]))
    if "cxl" in evidence.report["arm"]:
        initial = [m for m in boot_maps if m["end"] - m["start"] == 512 * MIB and
                   m["permissions"] == "rw-s" and m["path"] == "/dev/dax0.0" and
                   m["file_offset"] == int(expected_env["FC_DAX_OFFSET"])]
    else:
        initial = [m for m in boot_maps if m["end"] - m["start"] == 512 * MIB and
                   m["permissions"] == "rw-p" and m["inode"] == 0 and m["path"] == "" and
                   m["file_offset"] == 0]
    require(len(initial) == 1 and initial[0]["start"] == base,
            "original RAM base does not identify the unique expected fresh allocation")
    selected = [row for row in mappings if row["start"] < base + 512 * MIB and row["end"] > base]
    expected = [dict(row, gpa_base=row["start"] - base, hva_base=row["start"],
                     bytes=row["end"] - row["start"]) for row in selected]
    require(expected == scan["segments"], "RAM segment selection differs from raw mappings")
    cursor = base
    for segment in selected:
        require(segment["start"] == cursor and segment["permissions"].startswith("rw"),
                "RAM mapping coverage or protection changed")
        cursor = segment["end"]
    require(cursor == base + 512 * MIB, "RAM map is not complete 512MiB")
    require(all(0 <= gpa < 512 * MIB and hva == base + gpa for _, gpa, hva, _, _ in rows),
            "GPA to HVA translation mismatch")
    if scan["current_file_cache"]:
        command = evidence.command(scan["cachemap"])
        require(command["op"] == "cachemap" and command["resident_before"] == len(rows) and
                command["block_read_ios"] == 0, "cache oracle created a new cache fill")
        pages = evidence.guest_pages(scan["cache_gpa_file"], command["label"])
        require(pages == [row[:2] for row in rows], "scan is not the current guest file cache")
    return rows


def group_scan(evidence, group):
    scans = group["vms"]
    require(len(scans) == 3 and {scan["vm"] for scan in scans} == {1, 2, 3} and
            len({scan["pid"] for scan in scans}) == 3 and
            len({(scan["pid"], scan["process_start_time"]) for scan in scans}) == 3 and
            {scan["role"] for scan in scans} == {"A"}, "three distinct same-host VMMs required")
    frozen = evidence.order[group["frozen_file"]]
    require("POOL_COMPARE_GROUP_FROZEN 1 2 3" in evidence.text(group["frozen_file"]),
            "common frozen interval marker missing")
    final_scan = max(evidence.order[scan["pfns_file"]] for scan in scans)
    values = []
    for scan in scans:
        require(scan["current_file_cache"] and
                evidence.order[scan["cachemap"]["file"]] < frozen < evidence.order[scan["summary_file"]],
                "current cache was not pinned before the common scan interval")
        socket = f"/vm{scan['vm']}-epoch{scan['cachemap']['epoch']}/api.sock"
        vm_apis = [api for api in evidence.report["api_timings"]
                   if api["path"] == "/vm" and socket in evidence.console_command(api["file"])]
        preceding = [api for api in vm_apis if evidence.order[api["file"]] < frozen]
        require(preceding and preceding[-1]["request"] == {"state": "Paused"},
                "VMM was not paused at start of common scan interval")
        require(evidence.order[scan["cachemap"]["file"]] < evidence.order[preceding[-1]["file"]],
                "common scan pause predates the pinned cache oracle")
        require(not any(frozen < evidence.order[api["file"]] < final_scan for api in vm_apis),
                "VMM changed run state during common PFN scan interval")
        values.append(pfn_scan(evidence, scan))
    sets = [{row[3] for row in rows} for rows in values]
    actual = {"union_pages": len(set.union(*sets)), "intersection_pages": len(set.intersection(*sets)),
              "same_offset_shared_pages": sum(len({rows[i][3] for rows in values}) == 1
                                              for i in range(len(values[0])))}
    require(all(group[key] == value for key, value in actual.items()), "reported sharing counts altered")
    return {**actual, "union_bytes": actual["union_pages"] * PAGE,
            "common_file_bytes": actual["same_offset_shared_pages"] * PAGE}


def base_audit(report, root):
    require(report["schema"] == "pool-compare-arm-v1" and report["result"] == "passed" and
            not report.get("cleanup_errors") and not report.get("error"), "comparison run failed or incomplete")
    evidence = Evidence(report, root)
    config = report["configuration"]
    require(config["memory_mib"] == 512 and config["vcpus"] == 2 and config["io_engine"] == "Async" and
            config["no_seccomp"] is True, "comparison fixture settings differ")
    for name, digest in report["source_hashes"].items():
        require(Path(name).name == name and sha((root / "measured-sources" / name).read_bytes()) == digest,
                "measured source hash mismatch: " + name)
    for api in report["api_timings"]:
        raw = evidence.record(api["file"], "LAB_API_JSON ")
        require(raw == api["response"] and raw["status"] == 204 and raw["duration_us"] >= 0,
                "API receipt or successful response altered")
        require(json.dumps(api["request"]) in evidence.console_command(api["file"]),
                "API request does not match recorded command")
    labels = set()
    for row in report["commands"]:
        result = evidence.command(row)
        require(result["label"] not in labels, "duplicate command label")
        labels.add(result["label"])
    expected_binary = report["fixture_manifest"]["comparison"][
        "stock" if report["arm"].startswith("stock") else "patched"]["sha256"]
    require(config["binary_sha256"] == expected_binary, "wrong runtime selected")
    for vm_epoch, env in config["env_by_vm"].items():
        if "cxl" in report["arm"]:
            vm, epoch = vm_epoch.split(":")
            require(env["FC_POOL_VM_ID"] == vm and env["FC_POOL_RUN_EPOCH"] == epoch and
                    env["FC_DAX_RAM"] == "/dev/dax0.0" and
                    env["FC_POOL_IMAGE_ID"] == report["fixture_manifest"]["pool"]["image"]["sha256"],
                    "CXL observer configuration inconsistent")
        else:
            require(not any(key.startswith("FC_") for key in env), "stock/disabled arm has experimental env")
    for policy in report["system_policy"].values():
        text = evidence.text(policy["file"]).replace("\r", "")
        require("[never]" in text or "ABSENT /sys/kernel/mm/transparent_hugepage/enabled" in text,
                "host THP policy differs from controlled comparison")
        require(re.search(r"^POLICY /sys/kernel/mm/ksm/run\n0\n", text, re.M) or
                "ABSENT /sys/kernel/mm/ksm/run" in text, "KSM is not disabled in this comparison")
    return evidence


def audit_arm(report, root):
    evidence = base_audit(report, root)
    name, config = report["arm"], report["configuration"]
    require(name in ARMS and config["rounds"] >= 5 and config["independent_guest_boots"] == 3,
            "primary arm or repeat count incomplete")
    require(len(report["boots"]) == 3 and {row["vm"] for row in report["boots"]} == {1, 2, 3},
            "fresh independent boots missing")
    for boot in report["boots"]:
        components = boot["components"]
        require(components["/machine-config"] == {"vcpu_count": 2, "mem_size_mib": 512} and
                components["/drives/data"] == {"drive_id": "data", "path_on_host": "/disk/sandbox.raw",
                    "is_root_device": False, "is_read_only": True, "cache_type": "Writeback", "io_engine": "Async"} and
                components["/boot-source"]["kernel_image_path"] == "/lab/l2-vmlinux" and
                components["/boot-source"]["initrd_path"] == "/lab/pool-compare-l2-initramfs.cpio",
                "boot machine, image, engine, cache policy or guest fixture differs")
        socket = f"/vm{boot['vm']}-epoch0/api.sock"
        for path, body in components.items():
            calls = [api for api in report["api_timings"] if api["path"] == path and
                     socket in evidence.console_command(api["file"])]
            require(len(calls) == 1 and calls[0]["request"] == body,
                    "configured boot component is not bound to the executed API call")
    boot_ids = {evidence.command(boot["warm"])["boot_id"] for boot in report["boots"]}
    require(len(boot_ids) == 3, "fresh-boot arm accidentally uses snapshot clones")
    private_state, last_receipt = {}, -1
    for row in report["commands"]:
        value, vm = evidence.command(row), row["vm"]
        require(value["vm"] == vm and evidence.order[row["file"]] > last_receipt,
                "guest commands are reordered or target another VM")
        last_receipt = evidence.order[row["file"]]
        previous = private_state.get(vm)
        if previous is None:
            require(value["op"] == "warm" and value["generation"] == value["command"] == 1,
                    "independent workload does not start at its initial state")
        else:
            require(all(value[key] == previous[key] for key in ("boot_id", "pid", "cookie")) and
                    value["command"] == previous["command"] + 1, "guest identity or command sequence changed")
            if value["op"] == "write":
                require(value["generation"] == previous["generation"] + 1 and
                        value["private_hash"] != previous["private_hash"] and
                        value["upper_hash"] != previous["upper_hash"], "private mutation did not advance")
            else:
                require(all(value[key] == previous[key] for key in ("generation", "private_hash", "upper_hash")),
                        "another operation or VM changed private state")
        private_state[vm] = value
    initial = group_scan(evidence, report["initial_group_pfn"])
    final = group_scan(evidence, report["final_group_pfn"])
    pooled = name == "patched-cxl-pooled"
    for counts in (initial, final):
        if pooled:
            require(counts["common_file_bytes"] >= 128 * MIB, "shared current cache threshold not met")
        else:
            require(counts["union_bytes"] == 3 * 160 * MIB and counts["intersection_pages"] == 0,
                    "unpooled baseline shares payload frames")
    samples = report["samples"]
    require(len(samples) == 3 * config["rounds"] and
            {(row["round"], row["vm"]) for row in samples} ==
            {(rnd, vm) for rnd in range(config["rounds"]) for vm in (1, 2, 3)},
            "timed samples do not cover every VM and round exactly once")
    measured = {key: [] for key in ("hot", "private_write", "recycle", "cold_refill")}
    changed = []
    initial_rows = [evidence.pfn_rows(scan["pfns_file"]) for scan in report["initial_group_pfn"]["vms"]]
    initial_common = {row[0]: row[3] for index, row in enumerate(initial_rows[0])
                      if len({rows[index][3] for rows in initial_rows}) == 1}
    shared_during_samples = []
    for sample in samples:
        operations = {key: evidence.command(sample[key]) for key in measured}
        require(operations["hot"]["op"] == "check" and operations["hot"]["block_read_ios"] == 0,
                "hot sample was not a guest cache hit")
        require(operations["private_write"]["op"] == "write", "private-write sample changed")
        recycle = operations["recycle"]
        require(recycle["op"] == "recycle" and recycle["resident_after"] == 0 and
                recycle["churn_bytes"] == 224 * MIB and recycle["checked_churn_words"] == 224 * MIB // 8,
                "recycle did not evict and validate all anonymous data")
        cold = operations["cold_refill"]
        require(cold["op"] == "warm" and cold["resident_before"] == 0 and cold["block_read_ios"] > 0 and
                cold["block_read_sectors"] * 512 >= 160 * MIB, "refill is not guest-cold block IO")
        host = evidence.record(sample["hostwarm"]["file"], "POOL_COMPARE_HOSTWARM_JSON ")
        image_bytes = report["fixture_manifest"]["pool"]["image"]["bytes"]
        require(host == sample["hostwarm"]["receipt"] and host["all_resident"] and
                host["bytes"] == host["bytes_read"] == image_bytes and
                host["resident_pages"] == host["pages"], "host backing was not prewarmed")
        before, after = pfn_scan(evidence, sample["pfn_before"]), pfn_scan(evidence, sample["pfn_after_recycle"])
        require(sample["pfn_before"]["current_file_cache"] and not sample["pfn_after_recycle"]["current_file_cache"] and
                [row[:3] for row in before] == [row[:3] for row in after], "reuse scan changed address set")
        count = sum(a[3] != b[3] for a, b in zip(before, after))
        require(count == sample["changed_pfns"] and (not pooled or count > 0), "CoW PFN change count altered")
        changed.append(count)
        ordered_files = [sample["pfn_before"]["pfns_file"], sample["hot"]["file"],
                         sample["private_write"]["file"], sample["recycle"]["file"],
                         sample["pfn_after_recycle"]["pfns_file"], sample["hostwarm"]["file"],
                         sample["cold_refill"]["file"]]
        chronology = [evidence.order[file] for file in ordered_files]
        require(all(a < b for a, b in zip(chronology, chronology[1:])) and
                all(sample[key]["vm"] == sample["vm"] for key in measured),
                "sample operations are not ordered on the same VM")
        paused_scan(evidence, sample["pfn_after_recycle"], evidence.order[sample["recycle"]["file"]])
        if pooled:
            proof = sample["pfn_before"]
            reference = dict(tuple(map(int, line.split())) for line in
                             evidence.blobs[proof["initial_shared_pfns_file"]].splitlines())
            require(reference == initial_common, "initial shared PFN reference differs from three-VM proof")
            matched = sum(row[3] == initial_common.get(row[0]) for row in before)
            require(matched == proof["shared_with_initial_group_pages"] and matched * PAGE >= 128 * MIB,
                    "pooled timed sample lost actual shared-frame treatment")
            shared_during_samples.append(matched * PAGE)
        require(len(sample["peer_checks"]) == 2 and
                {peer["vm"] for peer in sample["peer_checks"]} == {1, 2, 3} - {sample["vm"]},
                "private-write peer isolation unchecked")
        for peer in sample["peer_checks"]:
            require(evidence.order[peer["file"]] > chronology[-1], "peer checked before writer reuse/refill")
            evidence.command(peer)
        for key, value in operations.items():
            measured[key].append(value["elapsed_ns"])
    if name == "patched-cxl-hints-unpooled":
        for event in report["control_events"]:
            raw = json.loads(evidence.blobs[event["admission"]["vmm_file"]])
            require(not raw["mapped_pages"], "unpooled control installed shared aliases")
    if "cxl" in name:
        check = report["canonical_check"]
        canonical = evidence.record(check["file"], "POOL_DAX_COPY_JSON ")
        expected_image = report["fixture_manifest"]["pool"]["image"]
        require(canonical == check["receipt"] and canonical["full_compare"] and
                canonical["result"] == "passed" and canonical["operation"] == "verify" and
                canonical["bytes"] == expected_image["bytes"] and
                {canonical["offset"]} == {int(env["FC_POOL_OFFSET"]) for env in config["env_by_vm"].values()},
                "immutable canonical full comparison missing or wrong allocation")
        require(check["image"]["sha256"] == expected_image["sha256"] and
                expected_image["sha256"] in evidence.text(check["image"]["file"]),
                "final backing image identity changed")
    control_wall, action_time = [], []
    known_apis = {api["file"]: api for api in report["api_timings"]}
    for event in report["control_events"]:
        require(event["observed_control_wall_ns"] >= event["observed_after_pause_until_resume_ns"] >= 0,
                "invalid controller wall-clock accounting")
        require(all(api == known_apis[api["file"]] for api in event["apis"]),
                "controller timing points at another API invocation")
        control_wall.append(event["observed_control_wall_ns"])
        action_time.append(sum(api["response"]["duration_us"] * 1000 for api in event["apis"]
                               if api["request"] == {"action_type": "PoolCache"}))
    return {"result": "passed", "arm": name, "report_sha256": file_sha(root / "report.json"),
            "verifier_sha256": file_sha(Path(__file__)), "raw_receipts": len(evidence.blobs),
            "commands": len(report["commands"]), "independent_boots": 3,
            "initial_current_cache": initial, "final_current_cache": final,
            "timings": {key: timings(values) for key, values in measured.items()},
            "timings_by_round": {str(rnd): {key: timings([
                sample[key]["receipt"]["elapsed_ns"] for sample in samples if sample["round"] == rnd
            ]) for key in measured} for rnd in range(config["rounds"])},
            "timings_by_vm": {str(vm): {key: timings([
                sample[key]["receipt"]["elapsed_ns"] for sample in samples if sample["vm"] == vm
            ]) for key in measured} for vm in (1, 2, 3)},
            "changed_pfns_after_recycle": changed,
            "shared_payload_bytes_before_timed_samples": shared_during_samples,
            "pool_control": {"events": len(control_wall),
                "controller_reported_wall": timings(control_wall) if control_wall else None,
                "raw_poolcache_api_sum_per_event": timings(action_time) if action_time else None,
                "scope": "Paused experimental admission, including evidence transport/control. This is not migration or direct-handoff latency."},
            "timing_scope": "Within-run repeated guest workload observations, including its word oracle; not independent fleet trials or pure memory bandwidth."}


def audit_suite(path):
    suite = json.loads(path.read_text())
    require(suite["schema"] == "pool-compare-suite-v1" and suite["result"] == "passed" and
            not suite.get("error"), "comparison suite did not complete")
    require(len(suite["arms"]) == 4 and {row["arm"] for row in suite["arms"]} == set(ARMS),
            "all four primary comparison arms required")
    for name, digest in suite["source_hashes"].items():
        require(Path(name).name == name and sha((path.parent / "measured-sources" / name).read_bytes()) == digest,
                "suite measured source hash mismatch")
    output = []
    for record in suite["arms"]:
        report, root = load_child(path.parent, record)
        require(report["arm"] == record["arm"] and report["fixture_manifest"] == suite["fixture_manifest"],
                "arm does not use the common immutable fixture")
        require(all(report["source_hashes"][name] == digest for name, digest in suite["source_hashes"].items()),
                "comparison implementation changed between primary arms")
        output.append(audit_arm(report, root))
    stock_record = suite["stock_snapshot_proof"]
    stock_report, stock_root = load_child(path.parent, stock_record)
    require(stock_report["arm"] == "stock-snapshot" and
            stock_report["fixture_manifest"] == suite["fixture_manifest"], "stock clone fixture mismatch")
    require(all(stock_report["source_hashes"][name] == digest for name, digest in suite["source_hashes"].items()),
            "stock clone proof used a different comparison implementation")
    from verify_stock_snapshot import audit as audit_stock
    stock = audit_stock(stock_root / "report.json")
    require(stock["result"] == "passed", "stock clone/NFS proof failed")
    return {"schema": "pool-compare-offline-audit-v1", "result": "passed",
            "suite_sha256": sha(path.read_bytes()), "verifier_sha256": file_sha(Path(__file__)),
            "stock_verifier_sha256": file_sha(Path(__file__).with_name("verify_stock_snapshot.py")),
            "arms": output, "stock_snapshot_proof": stock}


def changed_path(value, path, replacement):
    """Copy only the modified branch; raw evidence stays immutable on disk."""
    if not path:
        return replacement
    result = value.copy()
    key = path[0]
    result[key] = changed_path(value[key], path[1:], replacement)
    return result


def negative_tests(report, root):
    cases = [
        ("false_success", ["result"], "failed"),
        ("missing_sample", ["samples"], report["samples"][:-1]),
        ("wrong_elapsed", ["samples", 0, "hot", "receipt", "elapsed_ns"],
         report["samples"][0]["hot"]["receipt"]["elapsed_ns"] + 1),
        ("false_sharing", ["initial_group_pfn", "union_pages"], 1),
        ("missing_cache_oracle", ["initial_group_pfn", "vms", 0, "current_file_cache"], False),
        ("wrong_cow_count", ["samples", 0, "changed_pfns"], report["samples"][0]["changed_pfns"] + 1),
        ("wrong_binary", ["configuration", "binary_sha256"], "0" * 64),
        ("wrong_receipt_hash", ["receipts", 0, "sha256"], "0" * 64),
    ]
    rejected = []
    for label, path, replacement in cases:
        try:
            audit_arm(changed_path(report, path, replacement), root)
        except (ValueError, KeyError, OSError):
            rejected.append(label)
        else:
            raise ValueError("audit accepted manipulated evidence: " + label)
    return rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--negative-tests", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report["schema"] == "pool-compare-suite-v1":
        result = audit_suite(args.report)
        if args.negative_tests:
            result["rejected_evidence_mutations"] = {}
            for record in report["arms"]:
                child, root = load_child(args.report.parent, record)
                result["rejected_evidence_mutations"][record["arm"]] = negative_tests(child, root)
    else:
        result = audit_arm(report, args.report.parent)
        if args.negative_tests:
            result["rejected_evidence_mutations"] = negative_tests(report, args.report.parent)
    data = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(data)
    print(data, end="")


if __name__ == "__main__":
    main()
