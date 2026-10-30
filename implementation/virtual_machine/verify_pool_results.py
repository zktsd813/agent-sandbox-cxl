#!/usr/bin/env python3
"""Independently audit saved pooling lifecycle evidence; never boots a VM."""
import argparse
import hashlib
import json
from pathlib import Path

MIB = 1024 * 1024
PAGE = 4096


def require(ok, message):
    if not ok:
        raise ValueError(message)


def audit(report, root):
    require(report["schema"] == "cxl-pool-lifecycle-v1" and report["result"] == "passed",
            "experiment did not pass")
    require(not report.get("cleanup_errors"), "cleanup failed")
    args = report["arguments"]
    require(args["sandboxes"] == 3 and args["cycles"] >= 1, "three sandbox lifecycle required")
    minimum = args["minimum_cache_mib"] * MIB
    require(minimum > 100*MIB and report["payload_bytes"] >= minimum, "cache size requirement")
    blobs = {}
    for receipt in report["receipts"]:
        name = receipt["file"]
        require(name not in blobs and Path(name).name == name, "duplicate/unsafe receipt filename")
        data = (root/name).read_bytes()
        require(hashlib.sha256(data).hexdigest() == receipt["sha256"], "receipt hash mismatch: " + name)
        blobs[name] = data

    def receipt_json(name, prefix):
        values = [json.loads(line.removeprefix(prefix))
                  for line in blobs[name].decode().splitlines() if line.startswith(prefix)]
        require(len(values) == 1, "ambiguous/missing raw JSON: " + name)
        return values[0]

    def pfn(measured, minimum_bytes=minimum):
        observed = receipt_json(measured["file"], "POOL_PFN_JSON ")
        require(all(measured[k] == v for k,v in observed.items()), "PFN evidence altered")
        require(measured["all_rows_checked"], "incomplete PFN scan")
        require(measured["shared_pages"]+measured["private_pages"]+measured["absent_pages"] == measured["rows"], "PFN accounting")
        require(measured["shared_bytes"] == measured["shared_pages"]*PAGE >= minimum_bytes, "shared PFN threshold")
        offsets = [int(line) for line in blobs[measured["shared_offsets_file"]].splitlines()]
        require(len(offsets) == measured["shared_pages"] and sorted(set(offsets)) == measured["shared_offsets"], "shared offset evidence mismatch")
        require(len(set(offsets)) == measured["unique_shared_offsets"], "unique frame count")
        if measured.get("pfn_input_file"):
            source = blobs[measured["pfn_input_file"]]
            require(hashlib.sha256(source).hexdigest() == measured["pfn_input_sha256"], "PFN input hash mismatch")
            inputs = [tuple(map(int, line.split())) for line in source.splitlines()]
            require(all(len(row) == 2 for row in inputs) and len(inputs) == measured["rows"], "PFN input scan size")
            admitted = json.loads(blobs[measured["admission_file"]])
            if minimum_bytes == 0:
                require(inputs == [(p["hva"],p["pool_offset"]) for p in admitted["mapped_pages"]], "historical PFN scan changed address set")
        if report["fixture_manifest"]["pool"].get("guest_cache_gpa_audit") and minimum_bytes:
            require(measured.get("current_file_cache_proven"), "current-file-cache GPA proof absent")
            require(measured.get("pfn_input_file") and measured.get("admission_file"), "current cache scan inputs absent")
            probe = guest(measured["cache_probe"])
            require(probe["op"] == "cachemap" and probe["block_read_ios"] == 0 and
                    probe["resident_before"]*PAGE >= minimum, "cache mapping probe read missing data")
            entries = []
            for line in blobs[measured["cache_gpa_file"]].decode().splitlines():
                fields = line.split()
                require(len(fields) in (4,5) and fields[1] == probe["label"], "cache GPA record label")
                require((fields[0], len(fields)) in (("POOL_CACHE_GPA",4),("POOL_CACHE_RANGE",5)), "cache GPA record shape")
                start, gpa = map(int,fields[2:4])
                count = int(fields[4]) if len(fields) == 5 else 1
                require(start%PAGE == gpa%PAGE == 0 and gpa > 0 and 0 < count <= report["payload_bytes"]//PAGE,
                        "invalid cache GPA range")
                require(len(entries)+count <= report["payload_bytes"]//PAGE, "oversized cache GPA expansion")
                entries.extend((start+i*PAGE,gpa+i*PAGE) for i in range(count))
            require([v[0] for v in entries] == list(range(0,report["payload_bytes"],PAGE)) and
                    len({v[1] for v in entries}) == len(entries) == measured["cache_pages"], "actual file-cache GPA coverage")
            require(admitted["vm_id"] == str(measured["vm"]) == str(probe["vm"]) and
                    admitted["run_epoch"] == str(measured["epoch"]), "cache oracle/admission owner mismatch")
            mappings = {p["gpa"]:p for p in admitted["mapped_pages"]}
            expected_inputs, canonical_to_file = [], {}
            for file_offset,gpa in entries:
                cursor = file_offset
                for start,length in report["fixture_manifest"]["pool"]["image"]["payload_extents"]:
                    if cursor < length:
                        canonical = report["pool_offset"]+start+cursor
                        break
                    cursor -= length
                else:
                    raise ValueError("file offset outside immutable image extents")
                if gpa in mappings and mappings[gpa]["pool_offset"] == canonical:
                    row = mappings[gpa]
                    expected_inputs.append((row["hva"],canonical))
                    canonical_to_file[canonical] = file_offset
            require(inputs == expected_inputs and len(entries)-len(inputs) == measured["unmatched_cache_pages"],
                    "PFN scan includes pages outside actual resident cache")
            require(sorted(canonical_to_file[o] for o in set(offsets)) == measured["shared_file_offsets"],
                    "shared file-offset intersection not backed by physical scan")
        return set(offsets)

    def guest(row):
        data = row["receipt"]
        raw = blobs[row["file"]].decode(errors="strict")
        parsed = [json.loads(line) for line in raw.splitlines() if line.startswith('{"event":"pool_guest"')]
        require(data in parsed and data.get("ok") is True, "guest observation absent from raw evidence")
        require(data["vm"] == row["vm"] and data["payload_bytes"] == report["payload_bytes"], "guest identity/payload")
        require(data["pid"] > 0 and len(data["boot_id"]) == 36, "guest process/boot evidence")
        return data

    commands = report["commands"]
    require(commands, "no guest commands")
    by_vm = {vm: [] for vm in range(1, 4)}
    hashes = set()
    cached_sizes = []
    for row in commands:
        data = guest(row)
        by_vm[row["vm"]].append(data)
        if row["operation"] in ("warm", "check"):
            require(data["checked_payload_words"] == report["payload_bytes"]//8,
                    "partial file verification")
            require(data["resident_after"]*PAGE >= minimum and data["cached_kib"]*1024 >= minimum,
                    "file cache residency below threshold")
            require(data["block_read_ios"] is not None, "missing block device counters")
            hashes.add(data["payload_hash"])
            cached_sizes.append(data["resident_after"]*PAGE)
    require(len(hashes) == 1, "immutable content differs between sandboxes or epochs")
    manifest = report["fixture_manifest"]
    require(manifest["pool"]["image"]["sha256"] == report["image_id"], "wrong canonical image identity")
    require(hashes == {manifest["pool"]["image"]["generator"]["hash"]}, "payload differs from fixture oracle")
    require(set(report["initial_image_hashes"]) == {"A", "B"}, "local/NFS initial identity missing")
    for row in report["initial_image_hashes"].values():
        require(row["sha256"] == report["image_id"] and
                (report["image_id"] + "  /disk/sandbox.raw") in blobs[row["file"]].decode(),
                "initial backing differs from prepared image")
    boot_ids = []
    for vm, rows in by_vm.items():
        require(rows and all(r["vm"] == vm for r in rows), "missing sandbox")
        require(len({r["boot_id"] for r in rows}) == len({r["cookie"] for r in rows}) == 1,
                "sandbox rebooted or replaced during migration")
        require([r["command"] for r in rows] == list(range(1, len(rows)+1)), "guest command gap/replay")
        boot_ids.append(rows[0]["boot_id"])
    require(len(set(boot_ids)) == 3, "sandboxes are not independent")

    transitions = report["transitions"]
    require(len(transitions) == args["cycles"]*10, "missing lifecycle transitions")
    states = {vm: ("A", "dram", 0) for vm in by_vm}
    route_indexes = {vm: 0 for vm in by_vm}
    shared_measurements = []
    for row in transitions:
        vm = row["vm"]
        source, tier, epoch = states[vm]
        require((row["source"], row["source_tier"], row["epoch"]) == (source, tier, epoch+1),
                "transition chain/epoch mismatch")
        require(row["result"] == "passed" and b"FENCED " in blobs[row["fence"]], "source not fenced")
        require(b"\nPOOL_SNAPSHOT_TRANSPORT_FULL_COMPARE_PASSED" in blobs[row["snapshot_transport_file"]],
                "snapshot transport bytes not compared")
        route = [("A", "cxl"), ("A", "dram")] if vm == 2 else [("A", "cxl"), ("B", "cxl"), ("A", "cxl"), ("A", "dram")]
        require((row["destination"], row["destination_tier"]) == route[route_indexes[vm] % len(route)], "wrong lifecycle route")
        route_indexes[vm] += 1
        for label, expected in (("before", (source,tier,epoch)), ("after", (row["destination"],row["destination_tier"],epoch+1)), ("check", (row["destination"],row["destination_tier"],epoch+1))):
            event = row[label]
            require(event["vm"] == vm and (event["role"],event["tier"],event["epoch"]) == expected, "guest event attached to wrong transition")
        before, after, checked = guest(row["before"]), guest(row["after"]), guest(row["check"])
        for field in ("boot_id", "pid", "cookie", "generation", "private_hash", "upper_hash", "payload_bytes"):
            require(before[field] == after[field], "migration state discontinuity: " + field)
        require(checked["block_read_ios"] == 0 and checked["checked_payload_words"]*8 == report["payload_bytes"],
                "post-migration hot cache/content check")
        require(row["logical_snapshot_bytes"] == args["memory_mib"]*MIB, "snapshot size")
        pooled = row["pool"]
        vmm = pooled["vmm"]
        require(json.loads(blobs[pooled["vmm_file"]]) == vmm, "VMM admission raw evidence mismatch")
        require(vmm["vm_id"] == str(vm) and vmm["run_epoch"] == str(row["epoch"]), "admission identity")
        require(vmm["mapped_bytes"] == vmm["mapped"]*PAGE == len(vmm["mapped_pages"])*PAGE,
                "mapping accounting")
        require(vmm["requested"] == vmm["mapped"]+vmm["rejected_hint"]+vmm["rejected_content"],
                "unaccounted requested pages")
        require(len({p["gpa"] for p in vmm["mapped_pages"]}) == vmm["mapped"], "duplicate GPA mapping")
        if row["destination_tier"] == "cxl":
            imported = row["import"]
            require(receipt_json(imported["file"], "POOL_DAX_COPY_JSON ") == imported["receipt"], "RAM import receipt mismatch")
            require(imported and imported["receipt"]["full_compare"] and
                    imported["receipt"]["bytes"] == args["memory_mib"]*MIB, "unverified RAM import")
            measured = row["pfn_audit"]
            require(measured["rows"] <= vmm["mapped"] if measured.get("current_file_cache_proven")
                    else measured["rows"] == vmm["mapped"], "incomplete PFN audit")
            pfn(measured)
            require(set(measured["shared_offsets"]) <= {p["pool_offset"] for p in vmm["mapped_pages"]}, "unmapped shared offset")
            shared_measurements.append(measured["shared_bytes"])
        else:
            require(vmm["mapped"] == 0 and row["poison"] and row["poison"]["receipt"]["result"] == "passed",
                    "demotion still depends on CXL")
            require(receipt_json(row["poison"]["file"], "POOL_DAX_COPY_JSON ") == row["poison"]["receipt"], "demotion poison evidence mismatch")
        states[vm] = (row["destination"], row["destination_tier"], row["epoch"])
    require(all(state[:2] == ("A", "dram") for state in states.values()), "incomplete return to home DRAM")
    require(all(count == args["cycles"]*(2 if vm == 2 else 4) for vm,count in route_indexes.items()), "per-sandbox cycle count")
    require(len(report["remote_reclaims"]) == args["cycles"], "remote reclaim missing")
    for row in report["remote_reclaims"]:
        require(guest(row["evicted"])["resident_after"] == 0, "cache eviction failed")
        require(guest(row["cold"])["block_read_ios"] > 0 and row["cold"]["nfs_read_delta"] > 0,
                "cache miss did not reach host NFS")
        require(guest(row["hot"])["block_read_ios"] == 0, "refilled hot read missed cache")
        pfn(row["reclaimed_pfn_audit"], 0)
        require(row["reclaimed_pfn_audit"]["private_pages"] > 0, "guest memory reuse did not split pooled PFNs")
        stale = row["stale_admission"]
        require(json.loads(blobs[stale["vmm_file"]]) == stale["vmm"] and stale["vmm"]["rejected_content"] > 0, "stale candidate rejection absent")
        pfn(row["pfn_audit"])
    counter_rows = {kind: [r for r in report["receipts"] if r["kind"] == kind]
                    for kind in ("nfs-cold-before", "nfs-cold-after")}
    require(all(len(v) == args["cycles"] for v in counter_rows.values()), "NFS observations missing")
    def nfs_reads(receipt):
        lines = [line.split() for line in blobs[receipt["file"]].decode().splitlines() if line.startswith("proc3 ")]
        require(len(lines) == 1, "NFS raw counter ambiguous")
        return int(lines[0][8])
    for i, row in enumerate(report["remote_reclaims"]):
        require(nfs_reads(counter_rows["nfs-cold-after"][i])-nfs_reads(counter_rows["nfs-cold-before"][i]) == row["cold"]["nfs_read_delta"], "NFS delta differs from raw counters")
    sharing = report["concurrent_sharing"]
    require(len(sharing) == args["cycles"]*3, "missing simultaneous sharing stages")
    common = []
    for row in sharing:
        require(row["vms"] == [1,2,3] and len(row["audits"]) == 3, "incomplete concurrent sandbox set")
        require(len({a["file"] for a in row["audits"]}) == 3, "same PFN audit reused for multiple VMs")
        for vm, a in zip(row["vms"], row["audits"]):
            require(f"/vm{vm}-epoch" in blobs[a["file"]].decode(), "PFN audit targeted wrong sandbox")
        offsets = set.intersection(*(pfn(a) for a in row["audits"]))
        require(len(offsets)*PAGE == row["shared_bytes_all_vms"] >= minimum and len(offsets) == row["shared_pages_all_vms"], "three-VM shared intersection wrong")
        if row["stage"] == "split-hosts": require(row["roles"] == ["B","A","B"], "not sharing across A/B")
        if manifest["pool"].get("guest_cache_gpa_audit"):
            file_offsets = set.intersection(*(set(a["shared_file_offsets"]) for a in row["audits"]))
            require(len(file_offsets) == row["shared_current_file_pages_all_vms"] and
                    len(file_offsets)*PAGE == row["shared_current_file_bytes_all_vms"] >= minimum,
                    "actual current-file-cache intersection incorrect")
            groups = {a["frozen_group_file"] for a in row["audits"]}
            require(len(groups) == 1, "cache measurements were not frozen together")
            group = next(iter(groups))
            require(b"\nPOOL_CACHE_GROUP_FROZEN 1 2 3" in blobs[group], "missing frozen group receipt")
            order = {r["file"]: i for i,r in enumerate(report["receipts"])}
            require(all(order[a["cache_probe"]["file"]] < order[group] < order[a["file"]]
                        for a in row["audits"]), "physical scan precedes cache group freeze")
        common.append(len(offsets)*PAGE)
    require(len(report["canonical_checks"]) == args["cycles"], "canonical immutability checks missing")
    for row in report["canonical_checks"]:
        measured = receipt_json(row["file"], "POOL_DAX_COPY_JSON ")
        require(measured == row["receipt"] and measured["operation"] == "verify" and measured["full_compare"]
                and measured["result"] == "passed" and measured["bytes"] == manifest["pool"]["image"]["bytes"],
                "canonical bytes not fully verified")
        require(row["image"]["sha256"] == report["image_id"] and
                (report["image_id"] + "  /disk/sandbox.raw") in blobs[row["image"]["file"]].decode(),
                "backing image changed")
    bloom = report["bloom_negative_tests"]
    require(bloom["result"] == "passed" and bloom["false_negative_existing_keys"] > 0
            and bloom["false_positive_rejected_keys"] > 0, "Bloom fault injection missing")
    for field, hits in (("zero_receipt", 0), ("ones_receipt", bloom["false_positive_rejected_keys"])):
        raw = receipt_json(bloom[field]["file"], "POOL_BLOOM_JSON ")
        require(raw["operation"] == "probe" and raw["hits"] == hits, "Bloom fault injection raw evidence")
    require(len(report["admission_negative_tests"]) == 1, "admission rejection tests missing")
    negative = report["admission_negative_tests"][0]
    require(negative["result"] == "passed" and len(negative["cases"]) == 8, "admission rejection cases incomplete")
    require({c["case"] for c in negative["cases"]} == {"vm", "epoch", "image", "unaligned", "duplicate", "out_of_range", "stale_hint", "content_mismatch"}, "wrong rejection cases")
    raw_negative_reports = [json.loads(blobs[r["file"]]) for r in report["receipts"] if r["kind"] == "negative-admission"]
    for case in negative["cases"]:
        if "status" in case:
            require(case["status"] == receipt_json(case["file"], "LAB_API_JSON ")["status"] == 400, "unsafe plan accepted")
        else:
            require(case["report"] in raw_negative_reports, "negative report absent from raw evidence")
            require(case["report"]["mapped"] == 0, "stale data admitted")
            require(case["report"]["rejected_hint" if case["case"] == "stale_hint" else "rejected_content"] == 1,
                    "stale data not rejected")
    runtime = report["runtime_safety_negatives"]
    require({r["case"] for r in runtime} == {"pool-action-while-running", "state-only-snapshot-with-overlays"}, "runtime safety guard tests missing")
    for row in runtime:
        require(row["status"] == receipt_json(row["file"], "LAB_API_JSON ")["status"] == 400, "runtime guard accepted unsafe request")
    sentinels = [blobs[r["file"]] for r in report["receipts"] if r["kind"] == "state-sentinel-after"]
    require(sentinels == [b"pool state-only rejection must preserve existing state bytes\n"], "rejected snapshot overwrote existing state")
    for name, digest in report["runtime_source_hashes"].items():
        require(Path(name).name == name, "unsafe source name")
        require(hashlib.sha256((root/"measured-sources"/name).read_bytes()).hexdigest() == digest,
                "measured source mismatch: " + name)
    return {"result": "passed", "sandboxes": 3, "transitions": len(transitions),
            "guest_commands": len(commands), "raw_receipts": len(blobs),
            "minimum_guest_resident_bytes": min(cached_sizes),
            "minimum_measured_shared_bytes": min(shared_measurements),
            "minimum_three_vm_common_bytes": min(common),
            "current_file_cache_gpa_proven": bool(manifest["pool"].get("guest_cache_gpa_audit")),
            "logical_snapshot_bytes": sum(r["logical_snapshot_bytes"] for r in transitions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--negative-tests", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    result = audit(report, args.report.parent)
    if args.negative_tests:
        mutations = {
            "false_success": (["result"], "running"),
            "missing_transition": (["transitions"], report["transitions"][:-1]),
            "wrong_epoch": (["transitions",0,"epoch"], 999),
            "lost_private_data": (["transitions",0,"after","receipt","private_hash"], "bad"),
            "fake_cache_residency": (["commands",0,"receipt","resident_after"], 1),
            "false_shared_count": (["transitions",0,"pfn_audit","shared_pages"], 1),
            "missing_pfn_scan": (["transitions",0,"pfn_audit","all_rows_checked"], False),
            "nfs_not_used": (["remote_reclaims",0,"cold","nfs_read_delta"], 0),
            "cleanup_failed": (["cleanup_errors"], ["live owner"]),
            "wrong_receipt_hash": (["receipts",0,"sha256"], "0"*64),
        }
        rejected = []
        for name, (path, value) in mutations.items():
            # Copy only the modified ancestor chain. Audit is read-only; copying
            # every repeated 40k-page mapping table would multiply GB of data.
            altered = report.copy()
            source, destination = report, altered
            for key in path[:-1]:
                child = source[key].copy()
                destination[key] = child
                source, destination = source[key], child
            destination[path[-1]] = value
            try:
                audit(altered, args.report.parent)
            except (ValueError, KeyError):
                rejected.append(name)
            else:
                raise ValueError("audit accepted injected false evidence: " + name)
        result["rejected_evidence_mutations"] = rejected
    text = json.dumps(result, indent=2)+"\n"
    if args.out: args.out.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
