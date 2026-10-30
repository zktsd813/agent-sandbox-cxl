#!/usr/bin/env python3
"""Semantic rejection tests using completed SWE pool evidence, without VMs.

Mutations are confined to temporary copies. Checksums and, for authority
records, the public UAPI digest are recomputed so rejection must come from the
independent semantic checks rather than an ordinary damaged-file checksum.
"""
import argparse
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import tarfile
import tempfile

import verify_swe_pool as audit


def encoded(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def replace_event(data, before, after):
    result = []
    for line in data.splitlines(keepends=True):
        if line.startswith(b'{"event":'):
            row = json.loads(line)
            if row.get("event") == before["event"] and row.get("label") == before.get("label"):
                audit.need(row == before, "mutation input event mismatch")
                line = encoded(after)
        result.append(line)
    return b"".join(result)


def replace_report_events(value, before, after):
    if isinstance(value, dict):
        if value.get("event") == before["event"] and value.get("label") == before.get("label"):
            audit.need(value == before, "mutation input report event mismatch")
            value.clear()
            value.update(after)
        else:
            for child in value.values():
                replace_report_events(child, before, after)
    elif isinstance(value, list):
        for child in value:
            replace_report_events(child, before, after)


class ChangedEvidence:
    """Present changed bytes with matching hashes to an independent reader."""
    def __init__(self, original):
        self.root, self.report = original.root, copy.deepcopy(original.report)
        self.items, self.order = copy.deepcopy(original.items), original.order
        self.original, self.changed = original, {}

    def put(self, name, data):
        self.changed[name] = data
        self.items[name]["sha256"] = audit.sha(data)

    def read(self, name):
        data = self.changed[name] if name in self.changed else self.original.read(name)
        audit.need(audit.sha(data) == self.items[name]["sha256"], "mutation failed to rehash evidence")
        return data

    def text(self, name):
        return self.read(name).decode()

    def json(self, name):
        return json.loads(self.read(name))


def authority_digest(rows):
    head = rows[0]
    files = rows[1:1 + head["nr_files"]]
    records = rows[1 + head["nr_files"]:-1]
    data = bytearray(b"GPOOL_RECORDS_V1")
    data += struct.pack("<IIQQQQ32s", 1, len(files), len(records), head["session_cookie"],
                        head["vm_epoch"], head["plan_generation"], bytes.fromhex(head["image_id"]))
    for row in files:
        data += struct.pack("<QQQQII16sII", row["inode"], row["file_bytes"], row["start_page"], row["page_count"],
            row["inode_generation"], row["file_slot"], bytes.fromhex(row["fs_uuid"]), row["dev_major"], row["dev_minor"])
    for row in records:
        data += struct.pack("<IIQQQ", row["file_slot"], row["flags"], row["file_page"], row["gpa"], row["disk_offset"])
    return hashlib.sha256(data).hexdigest()


def wrong_returncode(path):
    """Rebind all serial/archive checksums; only the original SWE rc disagrees."""
    report = json.loads(path.read_bytes())
    before = copy.deepcopy(report["commands"][0]["receipt"])
    after = {**before, "rc": before["rc"] + 37}
    export_before = copy.deepcopy(report["guest_export"]["receipt"])
    original_archive = (path.parent / report["guest_archive"]).read_bytes()
    archive = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(original_archive), mode="r:gz") as source:
        with tarfile.open(fileobj=archive, mode="w:gz") as target:
            for member in source.getmembers():
                data = source.extractfile(member).read() if member.isfile() else None
                if member.name.removeprefix("./") == "results/000.json":
                    audit.need(json.loads(data) == before, "wrong baseline archive command")
                    data = encoded(after)
                    member.size = len(data)
                target.addfile(member, io.BytesIO(data) if data is not None else None)
    data = archive.getvalue()
    export_after = {**export_before, "bytes": len(data), "sha256": audit.sha(data)}
    replace_report_events(report, before, after)
    replace_report_events(report, export_before, export_after)
    report["guest_archive_sha256"] = export_after["sha256"]
    token = re.escape(export_before["label"])
    pattern = re.compile(rf"^TRACE_EXPORT_BEGIN {token} [0-9]+ [0-9a-f]{{64}}\r?\n.*?^TRACE_EXPORT_END {token}", re.M | re.S)
    b64 = base64.b64encode(data).decode()
    frame = (f"TRACE_EXPORT_BEGIN {export_after['label']} {len(data)} {export_after['sha256']}\n" +
             "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76)) +
             f"\nTRACE_EXPORT_END {export_after['label']}")
    with tempfile.TemporaryDirectory(prefix="swe-semantic-negative-") as directory:
        root = Path(directory)
        for source in path.parent.iterdir():
            if source.name != path.name:
                (root / source.name).symlink_to(source)
        for item in report["receipts"]:
            name = item["file"]
            value = (path.parent / name).read_bytes()
            if name == report["guest_archive"]:
                value = data
            elif item["kind"] == "guest" or item["kind"].endswith(("serial", "guest-log")):
                value = replace_event(replace_event(value, before, after), export_before, export_after)
                value = pattern.sub(lambda _: frame, value.decode()).encode()
            if value != (path.parent / name).read_bytes():
                (root / name).unlink()
                (root / name).write_bytes(value)
                item["sha256"] = audit.sha(value)
        changed = root / path.name
        changed.write_bytes(encoded(report))
        return audit.verify(changed)


def wrong_transition(path, change):
    report = json.loads(path.read_bytes())
    transition = next(row for row in report["transitions"] if row["kind"] == report["mode"])
    change(report, transition)
    with tempfile.TemporaryDirectory(prefix="swe-transition-negative-") as directory:
        root = Path(directory)
        for source in path.parent.iterdir():
            if source.name != path.name:
                (root / source.name).symlink_to(source)
        target = root / path.name
        target.write_bytes(encoded(report))
        return audit.verify(target)


def run(path):
    path = Path(path).resolve()
    report = json.loads(path.read_bytes())
    audit.verify(path)
    original = audit.Evidence(report, path.parent)
    results = []

    def reject(name, check, expected):
        try:
            check()
        except (ValueError, AssertionError) as error:
            audit.need(expected in str(error), f"{name}: rejected for unexpected reason: {error}")
            results.append({"case": name, "result": "rejected", "reason": str(error)})
        else:
            raise AssertionError("unsafe semantic mutation accepted: " + name)

    reject("rehash_serial_archive_wrong_returncode", lambda: wrong_returncode(path), "recorded command failed/diverged")
    if report["mode"] != "baseline":
        event = report["authority_events"][0]
        plan, memory = report["trace"], report["arguments"]["memory_mib"] * audit.MIB
        authority = audit.raw_authority(original, event, plan, memory)
        audit.need(authority["count"] >= 2, "semantic corpus requires two real registered pages")

        def bad_authority(change):
            evidence, altered = ChangedEvidence(original), copy.deepcopy(event)
            rows = [json.loads(line) for line in original.read(event["file"]).splitlines()]
            change(rows, 1 + rows[0]["nr_files"])
            rows[0]["records_sha256"] = rows[-1]["records_sha256"] = authority_digest(rows)
            raw = b"".join(encoded(row) for row in rows)
            altered["summary"] = rows[0]
            before = altered["command"]["receipt"]
            after = {**before, "bytes": len(raw), "sha256": audit.sha(raw)}
            evidence.put(event["file"], raw)
            evidence.put(altered["command"]["file"], replace_event(original.read(altered["command"]["file"]), before, after))
            altered["command"]["receipt"] = after
            return audit.raw_authority(evidence, altered, plan, memory)

        reject("rehash_authority_wrong_bmap", lambda: bad_authority(
            lambda rows, i: rows[i].update(disk_offset=rows[i]["disk_offset"] + audit.PAGE)), "bmap/frozen extent")
        reject("rehash_authority_dirty_flag", lambda: bad_authority(
            lambda rows, i: rows[i].update(flags=31)), "dirty/unlocked")
        reject("rehash_authority_duplicate_gpa", lambda: bad_authority(
            lambda rows, i: rows[i + 1].update(gpa=rows[i]["gpa"])), "GPA alias/bounds")
        reject("rehash_authority_inode_identity", lambda: bad_authority(
            lambda rows, i: rows[1].update(inode=rows[1]["inode"] + 1)), "frozen inode")
        promotion = next(row for row in report["transitions"] if row["kind"] == "promotion")
        raw_status = original.json(promotion["operation"]["file"])

        def bad_status(offset, value):
            raw = copy.deepcopy(raw_status)
            data = bytearray.fromhex(raw["registration_status_hex"])
            struct.pack_into("<I" if offset == 8 else "<Q", data, offset, value)
            raw["registration_status_hex"] = data.hex()
            return audit.status_proof(raw, authority)

        reject("inactive_live_authority", lambda: bad_status(8, 3), "stale live kernel authority")
        reject("stale_live_authority_token", lambda: bad_status(32, event["token"] + 1), "stale live kernel authority")
        manifest_name = promotion["export"]["file"]

        def bad_mapping(change):
            evidence, manifest = ChangedEvidence(original), original.json(manifest_name)
            change(manifest)
            evidence.put(manifest_name, encoded(manifest))
            return audit.placement(evidence, manifest_name, memory, plan)

        def private_alias(manifest):
            private = [row for row in manifest["mappings"] if not row["shared"]]
            private[1]["offset"] = private[0]["offset"]

        def canonical_private(manifest):
            next(row for row in manifest["mappings"] if row["shared"])["shared"] = False

        reject("rehash_private_frame_alias", lambda: bad_mapping(private_alias), "private alias/range")
        reject("rehash_canonical_frame_marked_private", lambda: bad_mapping(canonical_private), "private alias/range")
        reject("rehash_mapped_frame_in_credit_list", lambda: bad_mapping(
            lambda manifest: manifest["credits"].append(next(row["offset"] for row in manifest["mappings"] if not row["shared"]))),
            "credit alias")
        reject("handoff_claims_ram_transfer", lambda: wrong_transition(path,
            lambda _, row: row.update(ram_transfer_bytes=audit.PAGE)), "handoff mode/epoch/RAM copy")
        reject("handoff_skips_ownership_epoch", lambda: wrong_transition(path,
            lambda _, row: row.update(destination_epoch=row["source_epoch"] + 2)), "handoff mode/epoch/RAM copy")

        def wrong_fence(value, transition):
            final = next(row for row in value["cleanup"] if row["directory"].endswith("-epoch2"))
            transition["source_fence_file"] = final["file"]

        reject("handoff_uses_another_process_fence", lambda: wrong_transition(path, wrong_fence), "wrong source fence")
    return {"trace": report["name"], "mode": report["mode"], "report_sha256": audit.sha(path.read_bytes()),
            "result": "passed", "semantic_rejections": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source_names = ("verify_swe_pool_negatives.py", "verify_swe_pool.py", "verify_pool_handoff.py",
                    "verify_pool_discard_protocol.py", "verify_trace_results.py")
    sources = {name: audit.sha((Path(__file__).resolve().parent / name).read_bytes()) for name in source_names}
    result = {"schema": "swe-pool-semantic-negatives-v1", "result": "passed", "source_hashes": sources,
              "reports": [run(path) for path in args.reports]}
    data = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(data)
    print(data, end="")


if __name__ == "__main__":
    main()
