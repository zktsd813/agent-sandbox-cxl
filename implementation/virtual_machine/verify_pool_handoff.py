#!/usr/bin/env python3
"""Independent offline audit of guest-authorized CXL placement and handoff.

No benchmark/controller imports. Reconstructs guest registration digests,
physical placement, allocation ownership and transition ordering from receipts.
--pilot explicitly permits missing later instrumentation and never returns a
formal validation result. Neither mode treats one handoff mode as the other.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import struct

from verify_pool_discard_protocol import correlate, trace_stats

PAGE = 4096
MIB = 1024 * 1024
IDENTITY = ("boot_id", "pid", "cookie", "generation", "private_hash", "upper_hash", "payload_bytes")


def need(ok, message):
    if not ok:
        raise ValueError(message)


def integer(value, name, limit=(1 << 64) - 1):
    need(type(value) is int and 0 <= value <= limit, "invalid integer " + name)
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


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

    def guest(self, event):
        rows = [json.loads(line) for line in self.text(event["file"]).splitlines()
                if line.startswith('{"event":')]
        matches = [r for r in rows if r.get("event") == "pool_guest" and
                   r.get("label") == event["receipt"]["label"]]
        need(len(matches) == 1 and matches[0] == event["receipt"], "altered guest command")
        row = matches[0]
        need(row["ok"] is True and row["payload_bytes"] == 160 * MIB, "guest command failed/changed workload")
        need(row["mono_finish_ns"] >= row["mono_start_ns"], "guest clock order")
        if row["op"] in ("check", "warm"):
            generator = self.report["fixture"]["pool"]["image"]["generator"]
            need(row["resident_after"] == 160 * MIB // PAGE and
                 row["checked_payload_words"] == 160 * MIB // 8 and
                 row["payload_hash"] == generator["hash"], "incomplete full guest payload check")
        return row


def same(before, after):
    need(all(before[k] == after[k] for k in IDENTITY), "guest identity/private files changed")


def ranges(items):
    out = set()
    for item in items:
        start, count = integer(item["offset"], "range offset"), integer(item["pages"], "range pages")
        need(start % PAGE == 0 and 0 < count <= 1 << 20, "bad allocation range")
        new = set(range(start, start + count * PAGE, PAGE))
        need(not out & new, "duplicate allocation ranges")
        out.update(new)
    return out


def registration(e, event):
    command, identity = e.guest(event["command"]), e.guest(event["identity"])
    need(command["op"] == "prepare" and command["block_read_ios"] == 0 and
         command["resident_before"] == command["resident_after"], "registration faulted data into cache")
    same(identity, command)
    rows = [json.loads(line) for line in e.text(event["file"]).splitlines()
            if line.startswith('{"event":')]
    summaries = [r for r in rows if r.get("event") == "guest_pool_registration" and
                 r.get("label") == command["label"]]
    need(len(summaries) == 1 and summaries[0] == event["summary"], "registration summary altered")
    s = summaries[0]
    for key, value in (("vm", event["vm"]), ("epoch", event["epoch"]), ("token", event["token"]),
                       ("boot_id", identity["boot_id"]), ("cookie", identity["cookie"])):
        need(s[key] == value, "registration binding " + key)
    frozen = e.report["fixture"]["handoff"]["file_identity"]
    for key in ("inode", "inode_generation", "file_bytes", "fs_uuid"):
        need(s[key] == frozen[key], "registration inode/version mismatch")
    image_id = e.report["fixture"]["pool"]["image"]["sha256"]
    need(s["image_id"] == image_id and s["registration_active"] is True and s["readonly"] is True
         and s["uptodate"] is True and s["clean"] is True and s["writeback"] is False,
         "ineligible guest page-cache authority")
    count = integer(s["selected_pages"], "selected", 65536)
    skipped = sum(integer(s[k], k, 65536) for k in ("absent", "busy", "dirty", "writeback_skipped",
                  "not_uptodate", "large", "mapping_changed", "hole_or_unwritten", "unsupported_block", "beyond_full_pages"))
    need(s["requested"] == count + skipped == s["page_count"], "guest eligibility accounting incomplete")
    need(s["session"] > 0 and s["token"] > 0 and s["file_slot"] == 0 and s["start_page"] == 0
         and s["page_count"] == s["file_bytes"] // PAGE, "registration session/range")
    memory = e.report["arguments"]["memory_mib"] * MIB
    status_gpa = integer(s["status_gpa"], "status_gpa")
    need(status_gpa % PAGE == 0 and status_gpa + PAGE <= memory, "status GPA bounds")
    records, gpas, previous = [], set(), -1
    extents = e.report["fixture"]["handoff"]["logical_extents"]
    for row in rows:
        if row.get("event") != "guest_pool_range":
            continue
        need(row["label"] == command["label"] and row["flags"] == 63 and row["file_slot"] == 0,
             "registration range flags/label")
        start, gpa, disk, pages = (integer(row[k], k) for k in ("file_page", "gpa", "disk_offset", "pages"))
        need(0 < pages <= 65536 and start > previous and start + pages <= s["file_bytes"] // PAGE,
             "registration duplicate/out-of-file page")
        need(gpa % PAGE == 0 and gpa + pages * PAGE <= memory and len(records) + pages <= count,
             "registration physical bounds")
        for index in range(pages):
            file_page, address, offset = start + index, gpa + index * PAGE, disk + index * PAGE
            expected = [physical + file_page * PAGE - logical for logical, physical, size in extents
                        if logical <= file_page * PAGE and (file_page + 1) * PAGE <= logical + size]
            need(expected == [offset] and address not in gpas and address != status_gpa,
                 "registration outside frozen extents/duplicate/status page")
            gpas.add(address)
            records.append((0, 63, file_page, address, offset))
        previous = start + pages - 1
    need(len(records) == count and count <= command["resident_before"], "registration incomplete/not resident")
    header = struct.pack("<IIQQQQ32s", 1, 1, count, s["session"], s["epoch"], s["token"], bytes.fromhex(image_id))
    file = struct.pack("<QQQQII16sII", s["inode"], s["file_bytes"], s["start_page"], s["page_count"],
                       s["inode_generation"], s["file_slot"], bytes.fromhex(s["fs_uuid"]), s["dev_major"], s["dev_minor"])
    digest = hashlib.sha256(b"GPOOL_RECORDS_V1" + header + file)
    for record in records:
        digest.update(struct.pack("<IIQQQ", *record))
    need(digest.hexdigest() == s["records_sha256"], "kernel registration digest mismatch")
    return {"summary": s, "records": records, "by_gpa": {r[3]: r[4] for r in records}, "count": count, "event": event}


def status_proof(raw, authority, pilot):
    if "registration_status_hex" not in raw:
        need(pilot, "missing raw live kernel registration status")
        return
    data = bytes.fromhex(raw["registration_status_hex"])
    s = authority["summary"]
    need(len(data) == 208, "status size")
    magic, state, version, cookie, epoch, token, count, files, size = struct.unpack_from("<QIIQQQQII", data)
    need((magic, state, version, cookie, epoch, token, count, files, size) ==
         (0x47504f4f4c535431, 2, 1, s["session"], s["epoch"], s["token"], authority["count"], 1, 32),
         "inactive/stale kernel status")
    need(data[56:88].hex() == s["image_id"] and data[88:120].hex() == s["records_sha256"] and
         data[128:136] == b"\0" * 8 and data[144:] == b"\0" * 64, "live status digest/reserved mismatch")


def manifest(e, name):
    raw = e.json(name)
    memory = e.report["arguments"]["memory_mib"] * MIB
    need(len(raw["mappings"]) == memory // PAGE and raw["version"] == 1 and raw["exhausted"] == 0,
         "incomplete/exhausted manifest")
    private, shared = set(), {}
    for index, page in enumerate(raw["mappings"]):
        offset = integer(page["offset"], "manifest offset")
        need(page["gpa"] == index * PAGE and type(page["shared"]) is bool and offset % PAGE == 0
             and offset + PAGE <= raw["device_bytes"], "manifest GPA/offset")
        canonical = raw["canonical_start"] <= offset < raw["canonical_start"] + raw["canonical_bytes"]
        need(canonical == page["shared"], "canonical/private range mismatch")
        if page["shared"]:
            shared[page["gpa"]] = offset
        else:
            need(offset not in private, "private alias in manifest")
            private.add(offset)
    credits = set(raw["credits"])
    need(len(credits) == len(raw["credits"]) and not credits & private, "duplicate/mapped unused credits")
    for offset in credits | private:
        need(type(offset) is int and offset % PAGE == 0 and 2 * MIB <= offset < 2048 * MIB,
             "private frame outside allocator")
    return raw, private, shared, credits


def paused(e, vm, epoch, role, end_file, after_file=None):
    target = f"/vm{vm}-epoch{epoch}/api.sock"
    relevant = [a for a in e.report["api_events"] if a["role"] == role and a["path"] == "/vm"
                and target in e.text(a["file"]).replace("\r", "").replace("\n", "")
                and e.order[a["file"]] < e.order[end_file]]
    need(relevant and relevant[-1]["request"] == {"state": "Paused"}, "operation did not execute paused")
    if after_file:
        need(e.order[after_file] < e.order[relevant[-1]["file"]], "authority prepared after pause")


def dram_mapping(raw, memory, pilot):
    if "dram_vmas" not in raw:
        need(pilot, "missing actual demotion DRAM VMA proof")
        return
    end = 0
    for region in raw["dram_vmas"]:
        need(region["gpa"] == end and region["bytes"] > 0, "demotion geometry")
        low, high = region["hva"], region["hva"] + region["bytes"]
        cursor = low
        for line in region["maps"]:
            match = re.fullmatch(r"([0-9a-f]+)-([0-9a-f]+) (\S+) ([0-9a-f]+) (\S+) (\d+)\s*(.*)", line)
            need(match is not None, "bad raw demotion maps line")
            start, finish, permissions, _, _, inode, pathname = match.groups()
            start, finish = int(start, 16), int(finish, 16)
            need(start <= cursor < finish and permissions == "rw-p" and int(inode) == 0
                 and pathname == "", "demotion retained file/CXL/shared backing")
            cursor = min(high, finish)
        need(cursor == high, "demotion maps do not cover RAM")
        end += region["bytes"]
    need(end == memory, "demotion did not cover all logical RAM")


def guest_event(e, event, name):
    rows = [json.loads(line) for line in e.text(event["file"]).splitlines() if line.startswith('{"event":')]
    selected = [r for r in rows if r.get("event") == name and r.get("label") == event["receipt"]["label"]]
    need(selected == [event["receipt"]], "guest auxiliary event altered " + name)
    return selected[0]


def extended(e, authorities, memory, minimum, pilot):
    r = e.report
    if not pilot:
        for key in ("negative_controls", "bloom_negatives", "overlay_tests", "reuse_tests", "runtime_hashes"):
            need(r.get(key), "formal matrix missing " + key)
    runtime = r.get("runtime_hashes", {})
    if runtime:
        need(set(runtime) == {"A", "B"}, "runtime hashes omit a node")
        expected = dict(r["fixture"]["artifacts"])
        expected.update({name: value["sha256"] for name, value in r["fixture"]["handoff"]["inputs"].items()})
        for override, name in (("memory_module", "pool_memory_module.ko"), ("firecracker", "firecracker"),
                               ("memory_smoke", "pool-memory-smoke-20261030"),
                               ("kvm_memory_smoke", "pool-memory-kvm-smoke-20261030")):
            if override in r["runtime_overrides"]:
                expected[name] = r["runtime_overrides"][override]["sha256"]
        required = {"firecracker", "l2-vmlinux", "pool-handoff-probe", "pool-handoff-l2-initramfs.cpio",
                    "pool_memory_module.ko", "pool-memory-smoke-20261030", "pool-memory-kvm-smoke-20261030",
                    "kvm.ko", "kvm-intel.ko"}
        for role, event in runtime.items():
            found = dict((path, digest) for digest, path in re.findall(
                r"^([0-9a-f]{64})  (/[A-Za-z0-9_./-]+)\s*$", e.text(event["file"]), re.M))
            need(found == event["sha256"] and {Path(p).name for p in found} == required, "runtime hash receipt changed/incomplete")
            for path, digest in found.items():
                need(expected.get(Path(path).name) == digest, "runtime input differs from frozen fixture " + path)
    for negative in r.get("negative_controls", []):
        authority = authorities[negative["authority"]["file"]]
        cases = {case["case"]: case for case in negative["cases"]}
        need(set(cases) == {"wrong-session-cookie", "stale-vm-epoch", "changed-record-digest",
                            "wrong-canonical-offset", "released-status"}, "missing live authority negative")
        base = e.json(cases["released-status"]["plan_file"])
        for name, case in cases.items():
            reply = e.record(case["file"], "LAB_API_JSON ")
            need(case["status"] == reply["status"] == 400, "unsafe authority plan was accepted")
            plan = e.json(case["plan_file"])
            need(plan["operation"] == "promote" and plan["vm_id"] == str(negative["authority"]["vm"]), "negative plan identity")
            expected = copy.deepcopy(base)
            if name == "wrong-session-cookie":
                expected["registration"]["session_cookie"] ^= 1
            elif name == "stale-vm-epoch":
                expected["registration"]["vm_epoch"] += 1
            elif name == "changed-record-digest":
                expected["registration"]["records"][0]["disk_offset"] += PAGE
            elif name == "wrong-canonical-offset":
                first_shared = next(p for p in expected["mappings"] if p["shared"])
                first_shared["offset"] = expected["canonical_start"]
                need(expected != base, "canonical negative did not change a mapping")
            need(plan == expected, "negative authority plan mutation differs from claimed case")
            if name == "released-status":
                reg, s = plan["registration"], authority["summary"]
                need((reg["status_gpa"], reg["session_cookie"], reg["vm_epoch"], reg["plan_generation"], reg["image_id"]) ==
                     (s["status_gpa"], s["session"], s["epoch"], s["token"], s["image_id"]) and
                     [tuple(row[k] for k in ("file_slot", "flags", "file_page", "gpa", "disk_offset"))
                      for row in reg["records"]] == authority["records"] and
                     e.order[negative["close"]["file"]] < e.order[case["file"]],
                     "released authority test changed the original registration")
                need("guest registration no longer ACTIVE or digest mismatch" in reply["response"],
                     "closed registration was not rejected by live status validation")
        # The serial command receipt captures pool_guest, not the optional detailed
        # close event. The frozen guest helper checks INACTIVE before completion;
        # pair that completion with the unchanged plan's live host rejection.
        closed = e.guest(negative["close"])
        need(closed["op"] == "close_abort" and closed["vm"] == negative["authority"]["vm"] and
             closed["mono_start_ns"] >= negative["authority"]["command"]["receipt"]["mono_finish_ns"],
             "owner close did not complete after registration")
        if negative.get("detailed_guest_log_file"):
            rows = [json.loads(line) for line in e.text(negative["detailed_guest_log_file"]).splitlines()
                    if line.startswith('{"event":"guest_pool_close_abort"')]
            detailed = [row for row in rows if row.get("label") == closed["label"]]
            need(len(detailed) == 1 and detailed[0]["passed"] is True and detailed[0]["state"] == 3,
                 "detailed kernel close event did not revoke authority")
        e.guest(negative["after"])
    for negative in r.get("import_negatives", []):
        # The harness groups rejected imports under the actual target epoch.
        cases = negative.get("cases", []) if isinstance(negative, dict) else []
        if not cases and isinstance(negative, dict) and "file" in negative:
            cases = [negative]
        for case in cases:
            reply = e.record(case["file"], "LAB_API_JSON ")
            need(reply["status"] == 400 and case["status"] == 400,
                 "unsafe imported checkpoint was accepted")
            transitions = [t for t in r["transitions"] if t["kind"] == "state-only" and
                           t["vm"] == case["vm"] and t["destination_epoch"] == case["epoch"]]
            need(len(transitions) == 1, "negative import has no corresponding valid handoff")
            transition = transitions[0]
            original = e.json(transition["manifest_file"])
            changed = e.json(case["manifest_file"])
            expected = copy.deepcopy(original)
            if case["case"] == "stale-allocation-lease":
                expected["allocation_generation"] += 1
                reason = "stale placement epoch or allocation lease"
            elif case["case"] == "stale-placement-epoch":
                expected["run_epoch"] = str(int(expected["run_epoch"]) + 1)
                reason = "stale placement epoch or allocation lease"
            elif case["case"] == "corrupted-state-binding":
                reason = "state/placement manifest mismatch"
                need(case["original_sha256"] == original["state_sha256"] != case["damaged_sha256"],
                     "corrupt-state negative did not change state digest")
                if "damaged_state_file" in case:
                    damaged = e.read(case["damaged_state_file"])
                    state = e.read(transition["state_file"])
                    need(sha(damaged) == case["damaged_sha256"] and len(damaged) == len(state) and
                         damaged[0] == state[0] ^ 1 and damaged[1:] == state[1:], "damaged checkpoint bytes differ")
            else:
                raise ValueError("unknown imported-state negative")
            need(changed == expected and reason in reply["response"], "negative import mutation/rejection reason differs")
            launches = [a for a in r["launches"] if a.get("directory") == case["directory"] and
                        a.get("purpose") == "negative-" + case["case"]]
            need(len(launches) == 1 and e.order[launches[0]["file"]] < e.order[case["file"]] <
                 e.order[case["fence_file"]] < e.order[transition["destination_placement"]["file"]] and
                 re.search(r"^FENCED \d+ \d+\s*$", e.text(case["fence_file"]), re.M),
                 "negative restore wrapper not separately launched and fenced before valid handoff")
    if not pilot and r["mode"] == "state-only":
        need({x["case"] for x in r["import_negatives"]} ==
             {"stale-allocation-lease", "stale-placement-epoch", "corrupted-state-binding"},
             "formal state-only import negatives incomplete")
    bloom = r.get("bloom_negatives")
    if bloom:
        zero, ones = e.record(bloom["zero"]["file"], "POOL_BLOOM_JSON "), e.record(bloom["ones"]["file"], "POOL_BLOOM_JSON ")
        need(zero["hits"] == 0 and ones["hits"] == bloom["existing_pages_filtered"] +
             bloom["false_positive_pages_rejected_by_exact_lookup"] and
             bloom["candidate_pages_all_zero"] == 0 and bloom["candidate_pages_all_ones"] == 160 * MIB // PAGE,
             "Bloom negative accounting mismatch")
    for overlay in r.get("overlay_tests", []):
        copyup = e.guest(overlay["copyup"])
        raw = guest_event(e, overlay["event"], "guest_overlay_copyup")
        need(raw["vm"] == overlay["vm"] == copyup["vm"] and raw["passed"] is True and
             raw["upper_bytes"] == 160 * MIB and raw["checked_upper_words"] == 160 * MIB // 8 and
             raw["lower_unchanged"] is True and raw["lower_hash"] == r["fixture"]["pool"]["image"]["generator"]["hash"],
             "real OverlayFS copy-up incomplete/changed lower")
        need(len(overlay["peer_checks"]) == r["arguments"]["sandboxes"], "overlay omitted peers")
        for peer in overlay["peer_checks"]:
            e.guest(peer)
        need(e.guest(overlay["cleanup"])["op"] == "overlay_cleanup", "overlay upper cleanup absent")
    reuse_counts = []
    for reuse in r.get("reuse_tests", []):
        need(reuse["epoch"] == 0, "reuse replay currently requires the initial pre-handoff epoch")
        before = reuse["before"]["audits"]
        need(len(before) == 1 and before[0]["vm"] == reuse["vm"], "reuse source cache identity")
        before = before[0]
        authority = authorities[before["authority"]["file"]]
        old = e.json(before["pte_audit_file"])
        status_proof(old, authority, pilot)
        old_pages = {p["gpa"]: p for p in old["pages"]}
        original = {}
        for gpa, disk in authority["by_gpa"].items():
            pte = old_pages[gpa]
            if pte["shared"]:
                need(pte["present"] is True and pte["writable"] is False and
                     pte["offset"] == 2048 * MIB + disk and
                     pte["actual_pfn"] == (old["phys_base"] + pte["offset"]) // PAGE, "reuse source was not canonical")
                original[gpa] = pte["actual_pfn"]
        need(len(original) * PAGE >= minimum, "reuse lacks current canonical cache")
        allocated, filled, verified = (e.guest(reuse[k]) for k in ("allocated", "filled", "verified"))
        need(allocated["anon_bytes"] == filled["anon_bytes"] == verified["anon_bytes"] == 400 * MIB and
             verified["anon_checked_words"] == 400 * MIB // 8 and verified["anon_filled"] is True,
             "anonymous reuse workload incomplete")
        need(e.guest(reuse["evicted"])["resident_after"] == 0, "file cache was not evicted")
        trace_event = reuse["waited"]["trace_before_reuse"]
        trace = e.text(trace_event["file"])
        label = e.guest(trace_event["command"])["label"]
        need(f"POOL_REPORT_TRACE_BEGIN {label}" in trace and f"POOL_REPORT_TRACE_END {label}" in trace, "reuse trace boundaries")
        per_cpu = {}
        for line in trace.splitlines():
            match = re.match(r"POOL_REPORT_STATS " + re.escape(label) + r" (cpu\d+) (.*)", line)
            if match:
                per_cpu.setdefault(match[1], []).append(match[2])
        need(per_cpu, "reuse trace stats missing")
        for values in per_cpu.values():
            trace_stats("\n".join(values))
        logs = list(r.get("report_logs", [])) + list(r.get("host_reporting_logs", []))
        if reuse.get("host_after_trace"):
            logs.append(reuse["host_after_trace"])
        logs = [x for x in logs if x["vm"] == reuse["vm"] and x["epoch"] == reuse["epoch"]]
        need(logs, "reuse host mapping/ACK log missing")
        latest = max(logs, key=lambda x: e.order[x["file"]])
        joined = correlate(trace, [{"text": e.text(latest["file"]), "vm_id": str(reuse["vm"]),
                                    "run_epoch": str(reuse["epoch"])}], memory)
        detached = set()
        for batch in joined["batches"]:
            if (batch["host_batch"] <= reuse["waited"]["after_batch"] or
                    batch["guest_seq"] <= reuse["waited"]["after_guest_sequence"]):
                continue
            need(batch["guest_ack_ns"] <= allocated["mono_start_ns"] and batch["copied_bytes"] == 0,
                 "page reused before guest ACK or canonical data copied")
            for item in batch["discarded"]:
                detached.update(range(item["gpa"], item["gpa"] + item["bytes"], PAGE))
        declared = {int(line) for line in e.text(reuse["waited"]["detached_gpas_file"]).splitlines()}
        need(declared <= detached & set(original), "claimed discard lacks actual mapped+guest ACK")
        anon = e.guest(reuse["anonmap"])
        rows = []
        for line in e.text(reuse["anon_gpa_file"]).splitlines():
            fields = line.split()
            need(len(fields) == 5 and fields[:2] == ["POOL_ANON_RANGE", anon["label"]], "anonymous GPA evidence grammar")
            offset, gpa, count = map(int, fields[2:])
            need(gpa % PAGE == 0 and 0 < count <= 400 * MIB // PAGE and gpa + count * PAGE <= memory, "anonymous GPA bounds")
            rows.extend((offset + n * PAGE, gpa + n * PAGE) for n in range(count))
        need([offset for offset, _ in rows] == list(range(0, 400 * MIB, PAGE)) and len({gpa for _, gpa in rows}) == len(rows),
             "anonymous GPA coverage incomplete/duplicate")
        actual = e.json(reuse["pte_audit_file"])
        current = {p["gpa"]: p for p in actual["pages"]}
        for _, gpa in rows:
            pte = current[gpa]
            need(pte["present"] is True and pte["writable"] is True and pte["shared"] is False and
                 pte["actual_pfn"] == (actual["phys_base"] + pte["offset"]) // PAGE, "reused anonymous GPA lacks private CXL PTE")
        reused = {gpa for _, gpa in rows} & set(original)
        need(all(current[gpa]["actual_pfn"] != original[gpa] for gpa in reused), "reused page still aliases canonical")
        proven = reused & detached
        # A controller poll can finish before the final guest trace capture.
        # Validate its conservative declared subset exactly; derive any later
        # observed completion independently, still before guest allocation.
        need(len(reused) == reuse["reused_pages"] and len(reused & declared) == reuse["acked_then_reused_pages"] and
             len(proven) * PAGE >= minimum,
             f"ACK-before-reuse exact GPA/PFN overlap insufficient: actual reused={len(reused)}, "
             f"actual ACKed={len(proven)}, claimed reused={reuse['reused_pages']}, "
             f"claimed ACKed={reuse['acked_then_reused_pages']}")
        need(e.order[reuse["anonmap"]["file"]] < e.order[reuse["pte_audit_file"]] < e.order[reuse["freed"]["file"]],
             "anonymous memory freed before PTE observation")
        e.guest(reuse["freed"])
        e.guest(reuse["after"])
        for peer in reuse["peer_checks"]:
            e.guest(peer)
        reuse_counts.append(len(proven) * PAGE)
    return reuse_counts


def verify(path, pilot=False):
    path = Path(path)
    report = json.loads(path.read_bytes())
    need(report["schema"] == "guest-pool-handoff-v1" and report["result"] == "passed", "suite incomplete/failed")
    need(report["mode"] in ("state-only", "direct", "guest-smoke"), "unknown handoff mode")
    e = Evidence(report, path.parent)
    if report["mode"] == "guest-smoke":
        # This earlier preparatory run has complete raw registrations/commands,
        # but predates formal source and runtime incarnation instrumentation.
        commands = [e.guest(c) for c in report["commands"]]
        cold, warm, closed = [registration(e, report[k]) for k in
                              ("cold_registration", "warm_registration", "close_registration")]
        need(cold["summary"]["absent"] > 0 and cold["count"] < warm["count"] == 160 * MIB // PAGE and
             closed["count"] == warm["count"], "cold/warm guest helper coverage incomplete")
        ops = [c["op"] for c in commands]
        need(ops.count("authority_negative") == 2 and all(k in ops for k in ("abort", "release", "close_abort", "check")),
             "guest helper failure/release lifecycle incomplete")
        for prior, after in zip(commands, commands[1:]):
            same(prior, after)
        return {"result": "preparatory_audit_passed", "mode": "guest-smoke", "receipts": len(e.items),
                "cold_resident_pages": cold["count"], "cold_absent_pages": cold["summary"]["absent"],
                "warm_resident_pages": warm["count"], "prepare_data_read_ios": 0,
                "limitations": ["Preparatory receipts predate full formal source/runtime instrumentation",
                                "Negative/release commands verified through completion records; auxiliary kernel states not recaptured"]}
    for name, expected in report["source_hashes"].items():
        need(Path(name).name == name and sha((path.parent / "measured-sources" / name).read_bytes()) == expected,
             "measured source changed " + name)
    commands = {c["file"]: e.guest(c) for c in report["commands"]}
    boots = {}
    for row in commands.values():
        boots.setdefault(row["vm"], set()).add(row["boot_id"])
    need(len(boots) == report["arguments"]["sandboxes"] and all(len(x) == 1 for x in boots.values())
         and len(set.union(*boots.values())) == len(boots), "VM boots are not independent/continuous")
    for api in report["api_events"]:
        need(e.record(api["file"], "LAB_API_JSON ") == api["response"] and api["response"]["status"] in (200, 204),
             "API result altered/failed")
        echo = e.text(api["file"]).split("LAB_API_JSON ", 1)[0].replace("\r", "").replace("\n", "")
        need(api["path"] in echo and json.dumps(api["request"]) in echo, "API request differs from raw command")
        if api["path"] == "/snapshot/create":
            need(report["mode"] == "state-only" and api["request"]["mem_file_path"] == "/dev/dax0.0",
                 "wrong-mode/full-RAM snapshot")
    if not pilot:
        need(report.get("launches"), "missing runtime VMM incarnation evidence")
    launched, incarnations = set(), {}
    expected_vmm = report.get("runtime_overrides", {}).get("firecracker", {}).get(
        "sha256", report["fixture"]["artifacts"]["firecracker"])
    for launch in report.get("launches", []):
        text = e.text(launch["file"])
        found = re.findall(r"^([0-9a-f]{64})  /proc/([0-9]+)/exe\s*$", text, re.M)
        need(len(found) == 1 and found[0][0] == launch["sha256"] == expected_vmm, "VMM executable hash mismatch")
        identity = re.findall(r"^EXEC_PROCESS (\d+) (\d+)\s*$", text, re.M)
        need(len(identity) == 1 and identity[0][0] == found[0][1], "launch incarnation mismatch")
        incarnations[launch["directory"]] = (identity[0], launch["role"])
        if launch.get("purpose", "normal") == "normal":
            incarnation = launch["vm"], launch["epoch"]
            need(incarnation not in launched, "duplicate normal VMM incarnation")
            launched.add(incarnation)
    if not pilot:
        expected_launches = {(vm, 0) for vm in boots}
        expected_launches.update((t["vm"], t["destination_epoch"]) for t in report["transitions"]
                                 if t["kind"] in ("state-only", "direct"))
        need(launched == expected_launches, "normal VMM incarnation matrix incomplete")
    for key, prefix in (("mapper_smoke", "POOL_MEMORY_SMOKE_JSON "),
                        ("kvm_mapper_smoke", "POOL_MEMORY_KVM_SMOKE_JSON ")):
        need(key in report, "missing mapper smoke")
        raw = e.record(report[key]["file"], prefix)
        need(report[key]["records"] == [raw] and raw["result"] == "passed", "mapper smoke altered/failed")
        if key == "kvm_mapper_smoke":
            need(raw["pte_evidence"] == "CPM_AUDIT follow_pte no-prefault" and len(raw["cases"]) == 2,
                 "KVM smoke lacks actual PTE evidence")
            for case, count, present in zip(raw["cases"], (3, 2), (True, False)):
                need(case["faults"] == count and case["cow_pages"] == 1 and
                     case["initial_data_pte_present"] is present and case["write_exit_rip"] == 10 and
                     case["private_pfn"] != case["before_pfn"], "KVM fault branch unproven")
    authorities = {a["file"]: registration(e, a) for a in report["authority_events"]}
    memory = report["arguments"]["memory_mib"] * MIB
    minimum = report["arguments"]["minimum_cache_mib"] * MIB
    grants, generations, released, initial_grants, lifetime_grants = {}, {}, {}, {}, {}
    for event in report["allocator_events"]:
        owner = event["owner"]
        frames = ranges(event["ranges"])
        need(len(frames) == event["pages"] and all(2 * MIB <= x < 2048 * MIB for x in frames), "allocation bounds/count")
        if event["operation"] == "allocate":
            need(not any(frames & owned for owned in grants.values()), "cross-VM physical allocation alias")
            if owner in grants:
                need(generations[owner] == event["generation"], "allocation generation changed")
                grants[owner].update(frames)
            else:
                need(event["generation"] not in generations.values(), "reused live allocation generation")
                grants[owner], generations[owner] = set(frames), event["generation"]
                initial_grants[event["generation"]] = (owner, set(frames))
            lifetime_grants.setdefault(event["generation"], set()).update(frames)
        elif event["operation"] == "release":
            need(owner in grants and frames == grants[owner] and event["fenced"] and event["detached"],
                 "release unknown/live/partial grant")
            released[event["generation"]] = (owner, set(frames))
            del grants[owner]
        else:
            raise ValueError("unsupported allocator event")
    need(not grants or report["mode"] == "guest-smoke", "final allocator leaks live grants")
    shared_counts, actual_reuse, kinds = [], [], {}
    promotion_private = []
    direct_previous = {}
    for transition in report["transitions"]:
        kind = transition["kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
        need(transition["result"] == "passed", "failed transition")
        if kind in ("promotion", "demotion"):
            event = transition["operation"]
            raw = e.json(event["file"])
            need(raw == event["report"] and raw["result"] == "passed" and raw["operation"] == event["operation"]
                 and raw["vm_id"] == str(transition["vm"]) and raw["run_epoch"] == str(transition["epoch"]),
                 "memory operation result changed")
            need(raw["ram_snapshot_bytes"] == raw["file_page_compare_bytes"] == 0, "copy/compare scope violated")
            paused(e, transition["vm"], transition["epoch"], transition["source"], event["file"])
            if kind == "promotion":
                authority = authorities[transition["authority"]["file"]]
                status_proof(raw, authority, pilot)
                m, private, shared, credits = manifest(e, transition["export"]["file"])
                need(m["vm_id"] == str(transition["vm"]) and m["run_epoch"] == str(transition["epoch"]), "promotion placement identity")
                owner, allocation = initial_grants[m["allocation_generation"]]
                need(owner.startswith(f"vm{transition['vm']}:") and private | credits == allocation,
                     "promotion maps frames outside its exact initial grant")
                need(len(shared) * PAGE >= minimum and len(shared) * PAGE == raw["shared_bytes"] and
                     len(private) * PAGE == raw["copied_private_bytes"] and
                     raw["copied_private_bytes"] + raw["shared_bytes"] == memory, "promotion copy/shared accounting")
                for gpa, offset in shared.items():
                    need(authority["by_gpa"].get(gpa, -1) + m["canonical_start"] == offset,
                         "Bloom/plan admitted an unauthorized file page")
                same(e.guest(transition["authority"]["identity"]), e.guest(transition["after"]))
                release = e.guest(transition["release"])
                need(release["op"] == "release" and e.order[event["file"]] < e.order[transition["release"]["file"]],
                     "registration released before host operation")
                promotion_private.append((transition, m, private))
            else:
                need(raw["copied_private_bytes"] == memory and raw["shared_bytes"] == 0 and raw["module"] is None,
                     "demotion did not materialize full logical RAM")
                dram_mapping(raw, memory, pilot)
                if not pilot or "dram_maps_file" in transition:
                    observed = e.text(transition["dram_maps_file"]).splitlines()
                    need(all(line in observed for region in raw["dram_vmas"] for line in region["maps"]),
                         "independent proc maps do not confirm demotion DRAM backing")
                    need(e.order[event["file"]] < e.order[transition["dram_maps_file"]] <
                         e.order[transition["after"]["file"]], "DRAM maps observation not between demotion and resume check")
                m, private, _, credits = manifest(e, transition["previous"]["file"])
                need(ranges(transition["returned_ranges"]) == private | credits and
                     m["allocation_generation"] in released and
                     released[m["allocation_generation"]][1] == private | credits, "demotion released wrong physical frames")
                same(e.guest(transition["before"]), e.guest(transition["after"]))
        elif kind in ("state-only", "direct"):
            need(kind == report["mode"] and transition["destination_epoch"] == transition["source_epoch"] + 1,
                 "handoff mode/epoch mismatch")
            need(transition["ram_snapshot_bytes"] == transition["ram_transfer_bytes"] == 0, "handoff copied RAM")
            before, after = e.guest(transition["before"]), e.guest(transition["after"])
            same(before, after)
            need(after["block_read_ios"] == 0, "handoff lost hot guest cache")
            fence = e.text(transition["source_fence_file"])
            need(re.search(r"^FENCED \d+ \d+\s*$", fence, re.M), "missing source incarnation fence")
            if not pilot:
                sources = [a for a in report["launches"] if a["vm"] == transition["vm"] and
                           a["epoch"] == transition["source_epoch"] and a.get("purpose", "normal") == "normal"]
                need(len(sources) == 1 and sources[0]["role"] == transition["source"] and
                     re.findall(r"^FENCED (\d+) (\d+)\s*$", fence, re.M) ==
                     [incarnations[sources[0]["directory"]][0]], "source fence refers to another process incarnation")
            incoming = e.json(transition["manifest_file"])
            target, private, _, credits = manifest(e, transition["destination_placement"]["file"])
            need(incoming["vm_id"] == target["vm_id"] == str(transition["vm"]) and
                 incoming["run_epoch"] == str(transition["source_epoch"]) and
                 target["run_epoch"] == str(transition["destination_epoch"]) and
                 incoming["allocation_generation"] == target["allocation_generation"], "manifest incarnation/lease mismatch")
            need(private | credits <= lifetime_grants[target["allocation_generation"]] and
                 initial_grants[target["allocation_generation"]][0].startswith(f"vm{transition['vm']}:"),
                 "handoff maps another owner's frames")
            need(incoming["mappings"] == target["mappings"] and set(incoming["credits"]) == set(target["credits"]),
                 "handoff changed physical backing")
            need(e.order[transition["manifest_file"]] < e.order[transition["source_fence_file"]] <
                 e.order[transition["destination_placement"]["file"]] < e.order[transition["after"]["file"]],
                 "destination used backing before source fence")
            if kind == "state-only":
                state = e.read(transition["state_file"])
                need(len(state) == transition["state_bytes"] < memory // 16 and
                     sha(state) == transition["state_sha256"] == incoming["state_sha256"], "state/placement binding mismatch")
            else:
                state = e.json(transition["device_state_file"])
                need(set(state) == {"schema", "token", "machine_config", "boot_source", "memory", "memory_manifest_sha256", "devices"}
                     and state["schema"] == "direct-device-handoff-v1", "direct device bundle contains CPU snapshot")
                need(state["memory_manifest_sha256"] == sha(e.read(transition["manifest_file"])), "direct placement digest")
                token = state["token"]
                need(token == transition["token"] and token["vm_creates"] == token["vcpu_creates"] == 1
                     and transition["cpu_snapshot_bytes"] == 0, "direct reconstructed KVM objects")
                imports = []
                for name, item in e.items.items():
                    if item.get("kind") == "direct-import-log":
                        imports.extend((name, row) for row in e.rows(name, "DIRECT_HANDOFF_JSON ")
                                       if row.get("event") == "import" and row == transition["import_event"])
                need(len(imports) == 1, "direct import event missing/altered/duplicated")
                name, imported = imports[0]
                need(imported["snapshot_bytes"] == imported["ram_transfer_bytes"] == 0 and
                     e.order[transition["source_fence_file"]] < e.order[name] < e.order[transition["after"]["file"]],
                     "direct import precedes source fencing or copies state")
                retained = ("vm_offset", "vcpu_offset", "vmcs_offset", "run_offset", "ept_root_offset", "ram_hva", "ram_bytes")
                for key in (*retained, "generation", "vm_creates", "vcpu_creates"):
                    need(imported["token"][key] == token[key], "direct import changed retained " + key)
                need(imported["token"]["flags"] == 1 and imported["token"]["node"] ==
                     (1 if transition["destination"] == "A" else 2), "direct target ownership mismatch")
                previous = direct_previous.get(transition["vm"])
                if previous:
                    need(all(token[k] == previous[k] for k in retained) and token["generation"] > previous["generation"],
                         "direct cycle replaced execution objects or reused generation")
                direct_previous[transition["vm"]] = token
        else:
            raise ValueError("unknown transition " + kind)
    for group in report.get("sharing_audits", []):
        need(len(group["audits"]) == report["arguments"]["sandboxes"] and
             len({a["vm"] for a in group["audits"]}) == len(group["audits"]), "sharing group omits/duplicates a VM")
        common = None
        for audit in group["audits"]:
            authority = authorities[audit["authority"]["file"]]
            raw = e.json(audit["pte_audit_file"])
            status_proof(raw, authority, pilot)
            paused(e, audit["vm"], audit["epoch"], audit["role"], audit["pte_audit_file"], audit["authority"]["command"]["file"])
            pages = raw["pages"]
            need(len(pages) == memory // PAGE and [p["gpa"] for p in pages] == list(range(0, memory, PAGE)), "actual PTE audit coverage")
            selected = set()
            for gpa, disk in authority["by_gpa"].items():
                pte = pages[gpa // PAGE]
                need(pte["present"] is True and pte["actual_pfn"] == (raw["phys_base"] + pte["offset"]) // PAGE,
                     "actual native PTE disagrees with table")
                if pte["shared"]:
                    need(pte["writable"] is False and pte["offset"] == 2048 * MIB + disk,
                         "shared canonical PTE writable/wrong file frame")
                    selected.add(disk)
            need(len(selected) * PAGE >= minimum and len(selected) * PAGE == audit["shared_bytes"]
                 and selected == set(audit["shared_offsets"]), "sharing count differs from current pinned cache")
            common = selected if common is None else common & selected
        need(common is not None and len(common) * PAGE == group["common_shared_bytes"] >= minimum,
             "VMs do not share same current file pages")
        shared_counts.append(len(common) * PAGE)
    for reused in report.get("cross_vm_reuse", []):
        claimed = ranges(reused["ranges"])
        need(len(claimed) == reused["pages"], "reassignment count")
        matches = []
        for transition, m, private in promotion_private:
            if transition["vm"] != reused["destination_vm"]:
                continue
            for generation, (owner, freed) in released.items():
                if owner.startswith(f"vm{reused['source_vm']}:") and generation < m["allocation_generation"]:
                    matches.append((freed & private, freed))
        proven = [actual & claimed for actual, freed in matches if claimed <= freed and len(actual & claimed) * PAGE >= minimum]
        need(proven, "retired grant was not actually reused as private mapped/copied pages")
        actual_reuse.append(max(len(p) for p in proven) * PAGE)
    for refill in report.get("remote_refills", []):
        command = e.guest(refill["command"])
        def reads(name):
            rows = [line.split() for line in e.text(name).splitlines() if line.startswith("proc3 ")]
            need(len(rows) == 1, "NFS counter evidence")
            return int(rows[0][8])
        delta = reads(refill["after_file"]) - reads(refill["before_file"])
        need(delta == refill["nfs_read_delta"] > 0 and command["block_read_ios"] > 0, "cold refill bypassed NFS/virtio")
    for entry in report["cleanup"]:
        need(entry.get("fenced") is True and re.search(r"^FENCED \d+ \d+\s*$", e.text(entry["file"]), re.M),
             "final process not fenced")
        if not pilot:
            need(entry["directory"] in incarnations and incarnations[entry["directory"]][1] == entry["role"] and
                 re.findall(r"^FENCED (\d+) (\d+)\s*$", e.text(entry["file"]), re.M) ==
                 [incarnations[entry["directory"]][0]], "cleanup fence refers to another process incarnation")
    ack_reuse = extended(e, authorities, memory, minimum, pilot)
    if not pilot:
        final = report["canonical_final"]
        raw = e.record(final["file"], "POOL_DAX_COPY_JSON ")
        need(raw == final["receipt"] and raw["operation"] == "verify" and raw["result"] == "passed" and
             raw["offset"] == 2048 * MIB and raw["bytes"] == report["fixture"]["pool"]["image"]["bytes"] and
             raw["full_compare"] is True, "final canonical full-image verification missing/failed")
        image = final["image"]
        hashes = re.findall(r"^([0-9a-f]{64})  /disk/sandbox.raw\s*$", e.text(image["file"]), re.M)
        need(hashes == [image["sha256"]] == [report["fixture"]["pool"]["image"]["sha256"]],
             "canonical reference image changed")
        allocator = report["allocator_final"]
        need(allocator["allocated_bytes"] == 0 and allocator["owners"] == {} and
             allocator["free_bytes"] == allocator["capacity_bytes"] == 2046 * MIB,
             "final allocator capacity not completely returned")
    if report["mode"] != "guest-smoke":
        need(kinds.get(report["mode"], 0) >= 2 and kinds.get("promotion", 0) >= 3 and kinds.get("demotion", 0) >= 3,
             "required transition matrix absent")
        need(shared_counts and report.get("remote_refills"), "physical sharing or NFS case absent")
        if not pilot:
            args = report["arguments"]
            need(args["sandboxes"] >= 3 and args["cycles"] >= 2 and minimum >= 128 * MIB,
                 "formal workload below three sandboxes/two cycles/128 MiB")
            hops = {}
            for transition in report["transitions"]:
                if transition["kind"] != report["mode"]:
                    continue
                vm = transition["vm"]
                expected_epoch = hops.get(vm, 0)
                need(transition["source_epoch"] == expected_epoch and
                     (transition["source"], transition["destination"]) ==
                     (("A", "B") if expected_epoch % 2 == 0 else ("B", "A")),
                     "handoff cycles do not alternate nodes/epochs")
                hops[vm] = expected_epoch + 1
            moving = set(boots) if report["mode"] == "state-only" else {1}
            need(set(hops) == moving and all(n == 2 * args["cycles"] for n in hops.values()),
                 "formal mode omitted required VM/cycle handoffs")
            need(len(report["remote_refills"]) >= args["cycles"], "formal NFS cycle coverage incomplete")
            if report["mode"] == "state-only":
                need(len(actual_reuse) >= args["cycles"], "formal allocator reassignment cycles incomplete")
    return {"result": "preparatory_audit_passed" if pilot else "passed", "mode": report["mode"],
            "receipts": len(e.items), "authorities": len(authorities), "transitions": kinds,
            "minimum_common_current_shared_bytes": min(shared_counts) if shared_counts else 0,
            "actual_private_mapping_reuse_bytes": actual_reuse,
            "ack_before_guest_reuse_bytes": ack_reuse,
            "controller_declared_ack_before_reuse_bytes": [x["acked_then_reused_pages"] * PAGE for x in report.get("reuse_tests", [])],
            "limitations": ["Coherent nested lab; trusted guest kernel/controller", "Preparatory run; complete formal matrix is not asserted"] if pilot
                           else ["Coherent nested lab; trusted guest kernel/controller"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.report, pilot=args.pilot)
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")


if __name__ == "__main__":
    main()
