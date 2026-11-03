#!/usr/bin/env python3
"""Replay discard experiments offline from hashed raw receipts.

The comparison verifier supplies receipt/JSON/maps parsing only. This module
does not import a workload/controller and independently joins reporting epochs,
actual current-cache GPAs, canonical audits, ACKs and held anonymous PFNs.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics

from verify_pool_compare import Evidence, PAGE, MIB, PFN_MASK, file_sha, parse_maps, require, sha
from verify_pool_discard_protocol import correlate, guest_batches, host_batches, trace_stats

MINIMUM = 128 * MIB // PAGE
RAM = 512 * MIB


def pages(ranges):
    return {gpa for row in ranges for gpa in range(row["gpa"], row["gpa"] + row["bytes"], PAGE)}


def timing(values):
    require(values, "empty timing series")
    return dict(samples=len(values), median_ms=statistics.median(values)/1e6,
                minimum_ms=min(values)/1e6, maximum_ms=max(values)/1e6)


class Audit(Evidence):
    def anon_bytes(self):
        value = self.report["configuration"]["anon_mib"]
        require(type(value) is int and 1 <= value <= 448, "anonymous workload size outside declared safe fixture")
        return value*MIB

    def command(self, row):
        value = super().command(row)
        require(value["mono_finish_ns"] >= value["mono_start_ns"] >= 0,
                "guest MONOTONIC operation boundaries invalid")
        require(row["vm"] == value["vm"] and row["operation"] == value["op"],
                "guest command identity differs")
        return value

    def paused(self, scan, epoch, first, last=None):
        start = self.order[first]
        end = self.order[last or scan["pfns_file"]]
        socket = f"/vm{scan['vm']}-epoch{epoch}/api.sock"
        calls = [a for a in self.report["api_timings"] if a["path"] == "/vm" and
                 a["role"] == scan["role"] and socket in self.console_command(a["file"])]
        prior = [a for a in calls if self.order[a["file"]] < start]
        require(prior and prior[-1]["request"] == {"state": "Paused"}, "scan not paused")
        pause = self.order[prior[-1]["file"]]
        require(not any(pause < self.order[a["file"]] < end for a in calls),
                "VMM run state changed during physical audit")
        return pause

    def mapping(self, scan, epoch):
        vm, base = scan["vm"], scan["ram_base"]
        text = self.text(scan["maps_file"])
        require(re.findall(r"^PROCESS (\d+) (\d+)\s*$", text, re.M) ==
                [(str(scan["pid"]), str(scan["process_start_time"]))], "mapping process identity")
        digest = self.report["configuration"]["expected_binary_sha256"]
        require(re.findall(r"^([0-9a-f]{64})  /proc/(\d+)/exe\s*$",
                           self.text(scan["executable_file"]), re.M) == [(digest, str(scan["pid"]))],
                "running VMM hash differs from measured runtime")
        env = dict(re.findall(r"^([A-Z_][A-Z_0-9]*)=([^\r\n]*)$",
                             self.text(scan["environment_file"]).replace("\r", ""), re.M))
        require(env == self.report["configuration"]["env_by_vm"][f"{vm}:{epoch}"],
                "running environment differs from VM epoch")
        initial = next((b["mapping"] for b in self.report["boots"] if b["vm"] == vm), None) if epoch == 0 else next(
            (b["mapping"] for b in self.report["restore_mappings"] if b["vm"] == vm and b["epoch"] == epoch), None)
        require(initial and all(scan[k] == initial[k] for k in ("ram_base", "pid", "process_start_time", "role")),
                "PFN scan is not the independently captured VMM incarnation")
        mappings = parse_maps(text)
        selected = [m for m in mappings if m["start"] < base + RAM and m["end"] > base]
        require([dict(m, gpa_base=m["start"]-base, hva_base=m["start"], bytes=m["end"]-m["start"])
                 for m in selected] == scan["segments"], "reported RAM segments differ from raw mappings")
        cursor = base
        for m in selected:
            require(m["start"] == cursor and m["permissions"].startswith("rw"), "RAM gap/protection")
            cursor = m["end"]
        require(cursor == base + RAM, "RAM not exactly 512MiB")
        boot_maps = parse_maps(self.text(initial["maps_file"]))
        if epoch == 0:
            originals = [m for m in boot_maps if m["start"] == base and m["end"] == base+RAM and
                         m["permissions"] == "rw-s" and m["path"] == "/dev/dax0.0" and
                         m["file_offset"] == int(env["FC_DAX_OFFSET"])]
            require(len(originals) == 1, "initial CXL RAM allocation not independently identified")
        elif env["FC_DAX_SNAPSHOT"] == "0":
            require(not any(m["path"] == "/dev/dax0.0" for m in mappings), "DRAM restore retains DAX")
        return env

    def scan(self, scan, epoch, anonymous=False):
        anon_bytes = self.anon_bytes()
        command = self.command(scan["guest_command"] if anonymous else scan["cachemap"])
        require(command["op"] == ("anonmap" if anonymous else "cachemap"), "incorrect GPA probe")
        if anonymous:
            require(command["anon_live"] and command["anon_filled"] and command["anon_pinned"] and
                    command["anon_bytes"] == anon_bytes and command["anon_checked_words"] == anon_bytes//8,
                    "anonymous PFNs not held/pinned after full verification")
        else:
            require(scan["current_file_cache"] and command["block_read_ios"] == 0 and
                    command["resident_before"] * PAGE == 160*MIB, "cache GPA probe refilled pages")
        self.mapping(scan, epoch)
        pause = self.paused(scan, epoch, scan["maps_file"])
        require(self.order[(scan["guest_command"] if anonymous else scan["cachemap"])["file"]] < pause,
                "pause preceded current GPA pin")
        size = anon_bytes if anonymous else 160*MIB
        rows = [tuple(map(int, line.split())) for line in self.blobs[scan["pfns_file"]].splitlines()]
        require(len(rows) == size//PAGE and all(len(row) == 5 for row in rows) and
                [r[0] for r in rows] == list(range(0, size, PAGE)), "PFN coverage/shape")
        inputs = [tuple(map(int, line.split())) for line in self.blobs[scan["rows_file"]].splitlines()]
        require(inputs == [r[:3] for r in rows], "PFN input/output mismatch")
        for _, gpa, hva, pfn, raw in rows:
            require(0 < gpa < RAM and gpa % PAGE == 0 and hva == scan["ram_base"]+gpa and
                    raw >> 63 == 1 and not raw & (1 << 62) and 0 < pfn == raw & PFN_MASK,
                    "invalid present GPA/HVA/PFN/pagemap")
        require(all(len({r[k] for r in rows}) == len(rows) for k in (1, 2, 3)), "duplicate GPA/HVA/PFN")
        summary = self.record(scan["summary_file"], "POOL_COMPARE_PFN_JSON ")
        require(summary == scan["summary"] and summary["all_checked"] and
                summary["rows"] == summary["unique_pfns"] == len(rows) and summary["bytes"] == size and
                summary["pid"] == scan["pid"] and summary["process_start_time"] == scan["process_start_time"],
                "PFN reader summary differs")
        if anonymous:
            entries = []
            for line in self.text(scan["gpa_file"]).splitlines():
                f = line.split()
                require(len(f) == 5 and f[:2] == ["POOL_ANON_RANGE", command["label"]], "anon range record")
                off, gpa, count = map(int, f[2:])
                require(0 < count <= size//PAGE and len(entries)+count <= size//PAGE, "anon range size")
                entries.extend((off+i*PAGE, gpa+i*PAGE) for i in range(count))
        else:
            entries = self.guest_pages(scan["cache_gpa_file"], command["label"])
        require(entries == [r[:2] for r in rows], "actual guest GPA oracle differs from host PFN inputs")
        return rows

    def canonical(self, scan, rows, epoch):
        audit = scan["canonical_audit"]
        require(audit["current_file_cache_proven"] and audit["cache_probe"] == scan["cachemap"] and
                audit["vm"] == scan["vm"] and audit["epoch"] == epoch, "canonical audit not same pinned cache")
        self.paused(scan, epoch, scan["maps_file"], audit["shared_offsets_file"])
        require(self.guest_pages(audit["cache_gpa_file"], scan["cachemap"]["receipt"]["label"]) == [r[:2] for r in rows],
                "canonical audit references different guest pages")
        admission = json.loads(self.blobs[audit["admission_file"]])
        require(str(admission["vm_id"]) == str(scan["vm"]) and str(admission["run_epoch"]) == str(epoch),
                "canonical admission belongs to another incarnation")
        by_gpa = {r["gpa"]: r for r in admission["mapped_pages"]}
        image = self.report["fixture_manifest"]["pool"]["image"]
        pool_offset = int(self.report["configuration"]["env_by_vm"][f"{scan['vm']}:{epoch}"]["FC_POOL_OFFSET"])
        selected, pool_to_gpa, pool_to_file = [], {}, {}
        for file_offset, gpa, hva, _, _ in rows:
            cursor, disk = file_offset, None
            for start, size in image["payload_extents"]:
                if cursor < size:
                    disk = start + cursor
                    break
                cursor -= size
            require(disk is not None, "payload extent coverage")
            item = by_gpa.get(gpa)
            if item is not None and item["pool_offset"] == pool_offset+disk:
                require(item["hva"] == hva, "admitted HVA differs from current VMM translation")
                selected.append((hva, item["pool_offset"]))
                pool_to_gpa[item["pool_offset"]], pool_to_file[item["pool_offset"]] = gpa, file_offset
        inputs = self.blobs[audit["pfn_input_file"]]
        require(sha(inputs) == audit["pfn_input_sha256"] and
                [tuple(map(int, line.split())) for line in inputs.splitlines()] == selected,
                "canonical scanner input is not exact current cache/admission intersection")
        offsets = [int(line) for line in self.blobs[audit["shared_offsets_file"]].splitlines()]
        result = self.record(audit["file"], "POOL_PFN_JSON ")
        require(all(audit[k] == v for k, v in result.items()) and result["rows"] == len(selected) and
                len(offsets) == len(set(offsets)) == result["shared_pages"] and
                result["shared_bytes"] == len(offsets)*PAGE and set(offsets) <= pool_to_gpa.keys(),
                "physical canonical equality proof differs")
        require(audit["shared_offsets"] == sorted(offsets) and
                audit["shared_file_offsets"] == sorted(pool_to_file[o] for o in offsets) and
                len(offsets) >= MINIMUM, "less than128MiB actual canonical current cache")
        return {pool_to_gpa[o] for o in offsets}

    def trace(self, row):
        command = self.command(row["command"])
        require(command["op"] == "trace", "trace command mismatch")
        text, label = self.text(row["file"]), command["label"]
        require(text.count(f"POOL_REPORT_TRACE_BEGIN {label}") == 1 and
                text.count(f"POOL_REPORT_TRACE_END {label}") == 1, "trace capture boundaries")
        cpus = defaultdict(list)
        for line in text.splitlines():
            if line.startswith("POOL_REPORT_STATS "):
                match = re.fullmatch(r"POOL_REPORT_STATS (\S+) cpu(\d+) (.*)", line)
                require(match and match[1] == label, "trace stats identity")
                cpus[int(match[2])].append(match[3])
        require(set(cpus) == {0, 1}, "missing guest CPU trace buffer")
        for lines in cpus.values():
            trace_stats("\n".join(lines))
        return text

    def host_epochs(self, vm, last_epoch):
        result = []
        declared = {r["file"] for r in self.report.get("report_logs", []) if r["vm"] == vm}
        for epoch in range(last_epoch+1):
            candidates = []
            for name, data in self.blobs.items():
                if name not in declared and "discard-events" not in name and not re.search(
                        rf"(?:v{vm}-e{epoch}-(?:vmm-)?log|vm{vm}-final-(?:vmm-)?log)", name):
                    continue
                text = data.decode()
                lines = []
                for line in text.splitlines():
                    if "POOL_DISCARD_JSON " in line:
                        value = json.loads(line.split("POOL_DISCARD_JSON ", 1)[1])
                        if value["vm_id"] == str(vm) and value["run_epoch"] == str(epoch):
                            lines.append(line)
                marker_identity = any(json.loads(line.split("POOL_DISCARD_SNAPSHOT_JSON ",1)[1]).get("vm_id") == str(vm)
                                      and json.loads(line.split("POOL_DISCARD_SNAPSHOT_JSON ",1)[1]).get("run_epoch") == str(epoch)
                                      for line in text.splitlines() if "POOL_DISCARD_SNAPSHOT_JSON " in line)
                if lines or marker_identity:
                    candidates.append((len(lines), self.order[name], name, "\n".join(lines), text))
            require(candidates, f"no host reporting epoch {vm}:{epoch}")
            _, _, name, text, full_text = max(candidates)
            complete, tail = host_batches(text, vm, epoch)
            require(tail is None or epoch == last_epoch, "incomplete retired host epoch")
            cutoff = None
            abandoned = 0
            if epoch < last_epoch:
                markers = [(i,json.loads(line.split("POOL_DISCARD_SNAPSHOT_JSON ",1)[1]))
                           for i,line in enumerate(full_text.splitlines()) if "POOL_DISCARD_SNAPSHOT_JSON " in line]
                require(len(markers) == 1, "retired epoch lacks an unambiguous exact snapshot cutoff")
                line_number, cutoff = markers[0]
                require(cutoff["version"] == 1 and cutoff["phase"] == "complete" and
                        cutoff["vm_id"] == str(vm) and cutoff["run_epoch"] == str(epoch) and
                        type(cutoff["discard_sequence"]) is int and cutoff["discard_sequence"] >= 0,
                        "snapshot cutoff identity/sequence")
                expected_paths = {"snapshot_path": f"/vm{vm}-epoch{epoch}/state-snapshot",
                                  "mem_file_path": f"/vm{vm}-epoch{epoch}/ram-snapshot"}
                require(all(cutoff[k].endswith(v) for k,v in expected_paths.items()), "snapshot cutoff path")
                calls = [a for a in self.report["api_timings"] if a["path"] == "/snapshot/create" and
                         a["request"] == {"snapshot_type":"Full", **{k:cutoff[k] for k in expected_paths}}]
                require(len(calls) == 1, "snapshot cutoff not bound to successful full logical snapshot API")
                selected = []
                for index,line in enumerate(full_text.splitlines()):
                    if "POOL_DISCARD_JSON " not in line:
                        continue
                    row = json.loads(line.split("POOL_DISCARD_JSON ",1)[1])
                    require(row["vm_id"] == str(vm) and row["run_epoch"] == str(epoch), "retired VMM log mixed incarnations")
                    keep = row["batch"] <= cutoff["discard_sequence"]
                    require(keep == (index < line_number), "snapshot cutoff contradicts host log ordering")
                    if keep:
                        selected.append(line)
                kept, pending = host_batches("\n".join(selected), vm, epoch)
                require(pending is None and len(kept) == cutoff["discard_sequence"], "snapshot cut splits report transaction")
                abandoned = len(complete)-len(kept)
                text = "\n".join(selected)
            result.append(dict(text=text, vm_id=vm, run_epoch=epoch, file=name,
                               snapshot_cutoff=cutoff, abandoned_after_snapshot_batches=abandoned))
        return result


def verify_sample(e, sample):
    vm, epoch = sample["vm"], sample["epoch"]
    anon_bytes = e.anon_bytes()
    require(sample["tier"] == "cxl", "reuse sample was not CXL")
    before = e.scan(sample["before"], epoch)
    canonical = e.canonical(sample["before"], before, epoch)
    after = e.scan(sample["anon"], epoch, anonymous=True)
    old, new = {r[1]: r[3] for r in before}, {r[1]: r[3] for r in after}
    reused = set(old) & set(new)
    require(len(reused) >= MINIMUM and all(old[g] != new[g] for g in reused) and
            sample["reused_original_pages"] == sample["changed_reused_pages"] == len(reused),
            "reused GPAs did not all detach from original PFNs")
    operations = {key: e.command(sample[key]) for key in
                  ("hot", "evicted", "allocated", "filled", "verified", "freed", "refill")}
    memory = e.command(sample["memory_before_allocate"])
    require(memory["op"] == "identity" and memory["mem_available_kib"]*1024 >= anon_bytes+16*MIB and
            0 < memory["mem_available_kib"] <= memory["mem_total_kib"] <= RAM//1024 and
            e.order[sample["evicted"]["file"]] < e.order[sample["memory_before_allocate"]["file"]] < e.order[sample["allocated"]["file"]],
            "anonymous workload lacks measured guest memory reserve")
    require([operations[k]["op"] for k in operations] ==
            ["check", "evict", "anon_alloc", "anon_fill", "anon_verify", "anon_free", "warm"], "operation sequence")
    require(operations["hot"]["block_read_ios"] == 0 and operations["evicted"]["resident_after"] == 0 and
            operations["refill"]["resident_before"] == 0 and operations["refill"]["block_read_ios"] > 0,
            "hot/evict/cold-refill contract")
    chronology = [e.order[sample[k]["file"]] for k in operations]
    require(chronology == sorted(set(chronology)), "operation receipt order")
    ordered = list(operations.values())
    require(all(a["mono_finish_ns"] <= b["mono_start_ns"] for a,b in zip(ordered, ordered[1:])),
            "guest operation MONOTONIC order")
    require(operations["allocated"]["anon_bytes"] == anon_bytes and operations["allocated"]["anon_live"] and
            not operations["allocated"]["anon_filled"] and not operations["allocated"]["anon_pinned"] and
            operations["verified"]["anon_checked_words"] == anon_bytes//8 and
            len({operations[k]["anon_seed"] for k in ("allocated", "filled", "verified")}) == 1,
            "anonymous fill coverage/state/seed")
    require(e.order[sample["verified"]["file"]] < e.order[sample["anon"]["guest_command"]["file"]] <
            e.order[sample["anon"]["pfns_file"]] < e.order[sample["freed"]["file"]], "anonymous lifetime")
    require(len(sample["peers"]) == 2 and {p["vm"] for p in sample["peers"]} == {1,2,3}-{vm}, "peer coverage")
    for peer in sample["peers"]:
        require(e.order[peer["file"]] > chronology[-1] and e.command(peer)["op"] == "check", "peer checked before reuse")
    proved = set()
    threshold_pages = set()
    threshold_ack = None
    last_proved_ack = None
    wait = sample["waited"]
    if wait is not None:
        before_trace = e.trace(sample["trace_before"])
        batches, tail = guest_batches(before_trace)
        maximum = max([b["seq"] for b in batches]+([tail["seq"]] if tail else [0]))
        require(wait["after_guest_sequence"] == maximum, "wait guest baseline changed")
        text = e.trace(wait["trace_before_reuse"])
        joined = correlate(text, e.host_epochs(vm, epoch))
        host_before, host_tail = host_batches(e.text(sample["host_before"]["file"]), vm, epoch)
        baseline = max([b["batch"] for b in host_before]+([host_tail["batch"]] if host_tail else [0]))
        require(wait["after_batch"] == baseline == sample["host_before"]["maximum_batch"], "wait host baseline changed")
        waited_host, waited_tail = host_batches(e.text(wait["log"]["file"]), vm, epoch)
        waited_max = max([b["batch"] for b in waited_host]+([waited_tail["batch"]] if waited_tail else [0]))
        require(waited_max == wait["log"]["maximum_batch"], "wait capture host maximum changed")
        waited_acks = {b["batch"] for b in waited_host}
        for row in joined["batches"]:
            if (row["guest_seq"] > maximum and int(row["run_epoch"]) == epoch and
                    row["host_batch"] > baseline and row["host_batch"] in waited_acks):
                require(row["guest_ack_ns"] < operations["allocated"]["mono_start_ns"] <= operations["filled"]["mono_start_ns"],
                        "ACK not observed before allocation/reuse")
                actual = pages(row["discarded"])
                proved |= actual
                involved = actual & reused & canonical
                if involved:
                    last_proved_ack = row["guest_ack_ns"]
                threshold_pages |= involved
                if threshold_ack is None and len(threshold_pages) >= MINIMUM:
                    threshold_ack = row["guest_ack_ns"]
        detached = proved & set(old)
        recorded = {int(line) for line in e.blobs[wait["detached_gpas_file"]].splitlines()}
        require(recorded == detached and wait["detached_original_pages"] == len(detached), "detached proof GPA set changed")
        require(len(detached & reused) == sample["detached_then_reused_pages"] >= MINIMUM and
                len(detached & reused & canonical) >= MINIMUM,
                "fewer than128MiB canonical→actual detach→guest ACK→held reuse")
    else:
        require(sample["detached_then_reused_pages"] is None, "no-wait sample falsely claims pre-ACK detach")
    if sample["nfs"]:
        nfs = sample["nfs"]
        counts = []
        for key in ("before_file", "after_file"):
            lines = [line.split() for line in e.text(nfs[key]).splitlines() if line.startswith("proc3 ")]
            require(len(lines) == 1, "NFS raw counter missing")
            counts.append(int(lines[0][8]))
        require(sample["role"] == "B" and counts == [nfs["before_reads"], nfs["after_reads"]] and
                counts[1]-counts[0] == nfs["read_delta"] > 0 and
                e.order[nfs["before_file"]] < e.order[sample["refill"]["file"]] < e.order[nfs["after_file"]],
                "NFS refill not evidenced")
    wait_timing = None
    if wait is not None:
        require(threshold_ack is not None and last_proved_ack is not None, "missing ACK threshold timestamp")
        offset = threshold_ack - operations["evicted"]["mono_finish_ns"]
        wait_timing = dict(observed_controller_wall_ns=wait["observed_wall_ns"],
                           guest_threshold_ack_ns=threshold_ack,
                           guest_threshold_ack_offset_from_eviction_end_ns=offset,
                           guest_wait_after_eviction_until_128mib_ack_ns=max(0,offset),
                           guest_last_proved_ack_offset_from_eviction_end_ns=last_proved_ack-operations["evicted"]["mono_finish_ns"],
                           scope="Same guest MONOTONIC domain; retrospective128MiB canonical/reused-page ACK threshold. Includes reporter scheduling, host service and guest completion; not pure reporter or native host latency. Controller wall includes polling and evidence capture.")
    return dict(vm=vm, epoch=epoch, number=sample["number"], canonical_pages=len(canonical),
                reused_pages=len(reused), canonical_ack_detached_reused_pages=len(proved & reused & canonical),
                reporting_wait=wait_timing,
                timings_ns={key: value["elapsed_ns"] for key, value in operations.items()})


def verify_group(e, group):
    require(len(group["vms"]) == 3 and {s["vm"] for s in group["vms"]} == {1,2,3}, "group VM coverage")
    frozen = e.order[group["frozen_file"]]
    require("POOL_COMPARE_GROUP_FROZEN 1 2 3" in e.text(group["frozen_file"]), "group freeze marker")
    final = max(e.order[s["pfns_file"]] for s in group["vms"])
    values = []
    for scan in group["vms"]:
        epoch = scan["cachemap"]["epoch"]
        require(scan["role"] == "A" and e.order[scan["cachemap"]["file"]] < frozen, "group pin interval/host")
        pause = e.paused(scan, epoch, group["frozen_file"], max(group["vms"], key=lambda s:e.order[s["pfns_file"]])["pfns_file"])
        require(e.order[scan["cachemap"]["file"]] < pause < frozen < final, "group pause interval")
        values.append(e.scan(scan, epoch))
    require(len({(s["pid"],s["process_start_time"]) for s in group["vms"]}) == 3, "group process identity")
    sets = [{r[3] for r in rows} for rows in values]
    counts = dict(union_pages=len(set.union(*sets)), intersection_pages=len(set.intersection(*sets)),
                  same_offset_shared_pages=sum(len({rows[i][3] for rows in values}) == 1 for i in range(len(values[0]))))
    require(all(group[k] == v for k,v in counts.items()) and counts["same_offset_shared_pages"] >= MINIMUM,
            "group actual sharing count/threshold")
    return counts


def verify_microprobe(e):
    text = e.text(e.report["one_page_latency"]["file"])
    config = e.record(e.report["one_page_latency"]["file"], "POOL_COW_LATENCY_CONFIG ")
    require(config["samples_per_mode"] == 100 and config["modes"] == 3 and config["page_bytes"] == PAGE and
            config["written_bytes"] == 1 and config["clock"] == "CLOCK_MONOTONIC_RAW" and
            config["owned_offset"] == 3758096384 and config["read_setup_before_each_write"], "microprobe scope/config")
    groups = defaultdict(list)
    for line in text.splitlines():
        if not line.startswith("POOL_COW_LATENCY "):
            continue
        row = json.loads(line.removeprefix("POOL_COW_LATENCY "))
        require(row["pass"] and row["peer_unchanged"] and row["remaining_4095_bytes_preserved"] and
                row["page_bytes"] == PAGE and row["written_bytes"] == 1 and row["cpu"] == config["cpu"] and
                row["after_pfn"] != row["canonical_pfn"], "microprobe byte preservation/isolation")
        require((row["before_pfn"] == row["canonical_pfn"]) == (row["mode"] == "canonical_cow"), "microprobe source mapping")
        if row["mode"] == "already_private":
            require(row["before_pfn"] == row["after_pfn"], "private baseline incurred PFN change")
        require(all(type(row[k]) is int and row[k] >= 0 for k in ("detach_ns", "first_write_ns", "second_write_ns")),
                "microprobe invalid time")
        groups[(row["kind"], row["mode"])].append(row)
    expected = {(kind, mode) for kind in ("host_store", "kvm_run_store") for mode in
                ("canonical_cow", "already_private", "discard_zero")}
    require(set(groups) == expected, "microprobe treatment coverage")
    for rows in groups.values():
        require([r["iteration"] for r in rows] == list(range(100)), "microprobe replicate coverage")
    return dict(scope="One byte in4KiB; setup reads excluded; KVM entry/HLT exit included. Zero checks are this microprobe, not a pre-fill scan of every guest discarded page.",
                groups={kind+"/"+mode: {field:timing([r[field] for r in rows]) for field in
                       ("detach_ns", "first_write_ns", "second_write_ns")} for (kind,mode),rows in groups.items()})


def verify_arm(report, root):
    require(report["schema"] == "pool-discard-arm-v1" and report["result"] == "passed" and
            not report.get("error") and not report.get("cleanup_errors"), "arm failed/incomplete")
    require(report["mode"] in ("baseline", "reporting"), "unknown arm mode")
    e, cfg = Audit(report, root), report["configuration"]
    require(cfg["memory_mib"] == 512 and cfg["vcpus"] == 2 and cfg["payload_mib"] == 160 and
            cfg["io_engine"] == "Async" and cfg["no_seccomp"], "fixture configuration")
    manifest = report["fixture_manifest"]
    require(cfg["expected_binary_sha256"] == manifest["comparison"]["patched"]["sha256"] ==
            manifest["inputs"]["firecracker"]["sha256"], "runtime manifest mismatch")
    runtime_names = {"firecracker", "l2-vmlinux", "pool-discard-l2-initramfs.cpio", "pool-discard-probe", "pool-cow-latency"}
    require(set(report["fixture_runtime_hashes"]) == {"A", "B"}, "live fixture hashes lack a host")
    for fixture in report["fixture_runtime_hashes"].values():
        hashes = {name:digest for digest,name in re.findall(r"^([0-9a-f]{64})  /lab/([^\r\n]+)\s*$",
                                                           e.text(fixture["file"]), re.M)}
        require(hashes == fixture["hashes"] == {name:manifest["artifacts"][name] for name in runtime_names},
                "live L1 kernel/initramfs/helper/runtime hashes differ from built fixture")
    for name,digest in report["source_hashes"].items():
        require(Path(name).name == name and file_sha(root/"measured-sources"/name) == digest, "measured source hash: "+name)
    require(report["source_hashes"]["guest.config"] == manifest["discard"]["guest_config_sha256"], "guest config hash")
    settings = (root/"measured-sources"/"guest.config").read_text().splitlines()
    require(all(x in settings for x in ("CONFIG_PAGE_REPORTING=y", "CONFIG_VIRTIO_BALLOON=y", "CONFIG_EVENT_TRACING=y")),
            "guest reporting/trace kernel configuration")
    for local, path in (("guest-virtio_balloon.c","drivers/virtio/virtio_balloon.c"),
                        ("guest-page_reporting.c","mm/page_reporting.c"),("guest-pool_discard.h","include/trace/events/pool_discard.h")):
        require(report["source_hashes"][local] == manifest["discard"]["guest_source_hashes"][path], "compiled guest source identity")
    for api in report["api_timings"]:
        raw = e.record(api["file"], "LAB_API_JSON ")
        require(raw == api["response"] and raw["status"] == 204 and raw["duration_us"] >= 0 and
                json.dumps(api["request"]) in e.console_command(api["file"]), "raw API request/result binding")
    identities, labels, prior_command = {}, set(), {}
    for row in report["commands"]:
        value = e.command(row)
        require(value["label"] not in labels, "duplicate command label")
        labels.add(value["label"])
        identity = tuple(value[k] for k in ("boot_id", "pid", "cookie", "generation", "private_hash", "upper_hash"))
        require(row["vm"] not in identities or identities[row["vm"]] == identity, "guest private identity lost during reuse/migration")
        identities[row["vm"]] = identity
        require(value["command"] > prior_command.get(row["vm"], 0), "guest command sequence regressed")
        prior_command[row["vm"]] = value["command"]
    require(len(identities) == 3 and len({v[0] for v in identities.values()}) == 3, "three independent guest boots")
    for key,env in cfg["env_by_vm"].items():
        vm, epoch = key.split(":")
        require(env["FC_POOL_VM_ID"] == vm and env["FC_POOL_RUN_EPOCH"] == epoch and
                env["FC_POOL_IMAGE_ID"] == manifest["pool"]["image"]["sha256"], "owner/epoch/image env")
        require((env.get("FC_POOL_DISCARD") == "1" and env.get("FC_POOL_DISCARD_TRACE") == "1") == (report["mode"] == "reporting"),
                "discard arm opt-in differs")
    require(len(report["boots"]) == 3, "fresh boot coverage")
    balloon = dict(amount_mib=0, deflate_on_oom=False, stats_polling_interval_s=0, free_page_hinting=False, free_page_reporting=True)
    for boot in report["boots"]:
        components = boot["components"]
        require((components.get("/balloon") == balloon) if report["mode"] == "reporting" else "/balloon" not in components,
                "balloon configuration differs")
        require(components["/machine-config"] == dict(vcpu_count=2,mem_size_mib=512) and
                components["/drives/data"] == dict(drive_id="data",path_on_host="/disk/sandbox.raw",is_root_device=False,
                                                   is_read_only=True,cache_type="Writeback",io_engine="Async"), "disk/machine boot policy")
        bootargs = components["/boot-source"]["boot_args"]
        require(all(x in bootargs.split() for x in ("page_reporting.page_reporting_order=0", "trace_event=pool_discard:pool_free_report",
                                                   "trace_buf_size=8M", "trace_clock=mono")), "guest trace boot controls")
        for path,body in components.items():
            calls = [api for api in report["api_timings"] if api["path"] == path and
                     f"/vm{boot['vm']}-epoch0/api.sock" in e.console_command(api["file"])]
            require(len(calls) == 1 and calls[0]["request"] == body, "boot request binding")
    for restored in report["restore_mappings"]:
        e.mapping(restored["mapping"], restored["epoch"])
    initial, final = verify_group(e, report["initial_group"]), verify_group(e, report["final_group"])
    require(len(report["samples"]) == 3*cfg["rounds"] and
            {(s["number"], s["vm"]) for s in report["samples"]} == {(n,v) for n in range(cfg["rounds"]) for v in (1,2,3)},
            "sample round/VM coverage")
    samples = [verify_sample(e,s) for s in report["samples"]]
    extras = []
    if report["mode"] == "reporting":
        require(not report["immediate_reuse"]["wait_for_reporting"] and report["immediate_reuse"]["waited"] is None,
                "immediate fallback exercise is not immediate")
        extras.append(verify_sample(e, report["immediate_reuse"]))
        extras += [verify_sample(e,s) for s in report.get("lifecycle_reuse", [])]
    protocol = {}
    for vm in (1,2,3):
        captures = [row for row in report["guest_traces"] if row["vm"] == vm]
        require(captures, "final guest trace absent")
        row = max(captures, key=lambda r:(r["epoch"], e.order[r["file"]]))
        text = e.trace(row)
        if report["mode"] == "reporting":
            epochs = e.host_epochs(vm, row["epoch"])
            joined = correlate(text, epochs)
            protocol[str(vm)] = {k:v for k,v in joined.items() if k != "batches"}
            protocol[str(vm)]["host_epochs"] = [{k:v for k,v in h.items() if k != "text"} for h in epochs]
            protocol[str(vm)]["discarded_pages"] = sum(b["discarded_pages"] for b in joined["batches"])
            protocol[str(vm)]["active_host_work"] = timing([b["host_work_ns"] for b in joined["batches"] if b["discarded_pages"]])
        else:
            require("pool_free_report:" not in text, "baseline guest unexpectedly reporting")
    transitions = report["transitions"]
    require(len(transitions) == (3 if cfg["lifecycle"] else 0), "lifecycle coverage")
    if transitions:
        require([(t["source"],t["source_tier"],t["destination"],t["destination_tier"]) for t in transitions] ==
                [("A","cxl","B","cxl"),("B","cxl","A","dram"),("A","dram","A","cxl")], "lifecycle route")
        for t in transitions:
            require(t["result"] == "passed" and t["logical_snapshot_bytes"] == RAM, "full snapshot lifecycle")
            before, after = e.command(t["before"]), e.command(t["after"])
            require(all(before[k] == after[k] for k in ("boot_id","pid","cookie","generation","private_hash","upper_hash","payload_bytes")),
                    "migration private identity continuity")
            require("POOL_SNAPSHOT_TRANSPORT_FULL_COMPARE_PASSED" in e.text(t["snapshot_transport_file"]) and
                    re.search(r"^FENCED ", e.text(t["fence"]), re.M), "snapshot transport/fence proof")
            require(e.order[t["before"]["file"]] < e.order[t["snapshot_transport_file"]] < e.order[t["fence"]] < e.order[t["after"]["file"]],
                    "source fence ordering")
            require(e.command(t["check"])["block_read_ios"] == 0, "warm migration cache refilled")
            for key in ("import", "poison"):
                item = t[key]
                if item:
                    raw = e.record(item["file"], "POOL_DAX_COPY_JSON ")
                    require(raw == item["receipt"] and raw["result"] == "passed" and raw["bytes"] == RAM,
                            "logical RAM import/poison receipt")
                    if key == "import": require(raw["full_compare"], "DAX import full byte compare")
            require((t["import"] is not None) == (t["destination_tier"] == "cxl") and
                    (t["poison"] is not None) == (t["destination_tier"] == "dram"), "tier import/poison coverage")
            if t["destination_tier"] == "dram":
                admission = json.loads(e.blobs[t["pool"]["vmm_file"]])
                require(not admission["mapped_pages"], "DRAM stage admitted canonical CXL overlays")
        require(len(report["lifecycle_reuse"]) == 2 and any(s["nfs"] for s in report["lifecycle_reuse"]), "remote NFS reuse coverage")
    check = report["canonical_check"]
    canonical = e.record(check["file"], "POOL_DAX_COPY_JSON ")
    image = manifest["pool"]["image"]
    require(canonical == check["receipt"] and canonical["result"] == "passed" and canonical["full_compare"] and
            canonical["operation"] == "verify" and canonical["bytes"] == image["bytes"] and
            {canonical["offset"]} == {int(env["FC_POOL_OFFSET"]) for env in cfg["env_by_vm"].values()} and
            check["image"]["sha256"] == image["sha256"] and image["sha256"] in e.text(check["image"]["file"]),
            "canonical immutable image changed")
    require(len(report["final_checks"]) == 3 and {row["vm"] for row in report["final_checks"]} == {1,2,3} and
            all(e.command(row)["op"] == "check" for row in report["final_checks"]), "final guest payload/private checks")
    return dict(result="passed", mode=report["mode"], anon_mib=cfg["anon_mib"], raw_receipts=len(e.blobs), initial_group=initial, final_group=final,
                samples=samples, additional_samples=extras, protocol=protocol, lifecycle_transitions=len(transitions),
                timings={key:timing([s["timings_ns"][key] for s in samples]) for key in samples[0]["timings_ns"]},
                one_page_latency=verify_microprobe(e), report_sha256=file_sha(root/"report.json"),
                limits=["Delayed free-page reporter can miss immediate reuse; ordinary CoW remains.",
                        "ACK can be a no-op for nonpooled/busy pages; only discarded ranges count.",
                        "A discarded GPA was previously pooled but may have already CoWed before the callback. copied_bytes=0 applies to the detach callback, not the whole interval or an exact number of copies avoided.",
                        "Physical PFN measurements are within each L1 namespace; no cross-node PFN equality claim.",
                        "Lazy fresh anonymous backing is not preallocated physical RAM; original DAX reservation is retained.",
                        "Successful ordinary snapshot transitions are covered; forced in-flight migration boundary requires separate injection evidence."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report["schema"] == "pool-discard-suite-v1":
        require(report["result"] == "passed" and not report.get("error"), "suite incomplete")
        require(len(report["arms"]) == 2 and {row["mode"] for row in report["arms"]} == {"baseline","reporting"},
                "full suite requires both matched arms")
        for name,digest in report["source_hashes"].items():
            require(Path(name).name == name and file_sha(args.report.parent/"measured-sources"/name) == digest,
                    "suite measured source hash: "+name)
        arms = []
        sizes = set()
        for record in report["arms"]:
            relative = Path(record["report_file"])
            require(not relative.is_absolute() and ".." not in relative.parts, "child path")
            path = args.report.parent/relative
            require(file_sha(path) == record["sha256"], "child report hash")
            child = json.loads(path.read_text())
            require(child["mode"] == record["mode"] and child["fixture_manifest"] == report["fixture_manifest"] and
                    child["configuration"]["rounds"] == report["rounds"], "suite arms changed fixture/rounds/mode")
            sizes.add(child["configuration"]["anon_mib"])
            arms.append(verify_arm(child, path.parent))
        require(len(sizes) == 1, "baseline/reporting anonymous workload sizes differ")
        result = dict(result="passed", schema="pool-discard-suite-audit-v1", arms=arms,
                      report_sha256=file_sha(args.report))
    else:
        result = verify_arm(report, args.report.parent)
    result["verifier_sources"] = {name:file_sha(Path(__file__).with_name(name)) for name in
                                  ("verify_pool_discard.py","verify_pool_discard_protocol.py","verify_pool_compare.py")}
    data = json.dumps(result, indent=2)+"\n"
    if args.out: args.out.write_text(data)
    print(data,end="")


if __name__ == "__main__":
    main()
