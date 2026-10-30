#!/usr/bin/env python3
"""Adversarial mutations of recorded handoff evidence, with receipt rehashing.

Run against each completed mode. Source evidence is never edited: unchanged
files are linked into a temporary view and each changed receipt is replaced.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile

from verify_pool_handoff import PAGE, verify


class Mutation:
    def __init__(self, source, report, directory):
        self.source, self.report, self.directory = source, copy.deepcopy(report), directory
        for item in self.report["receipts"]:
            (directory / item["file"]).symlink_to(source / item["file"])
        (directory / "measured-sources").symlink_to(source / "measured-sources", target_is_directory=True)

    def data(self, name):
        return (self.directory / name).read_bytes()

    def write(self, name, data):
        target = self.directory / name
        target.unlink()
        target.write_bytes(data)
        for item in self.report["receipts"]:
            if item["file"] == name:
                item["sha256"] = hashlib.sha256(data).hexdigest()

    def raw_json(self, name, change):
        raw = json.loads(self.data(name))
        change(raw)
        self.write(name, (json.dumps(raw, separators=(",", ":")) + "\n").encode())
        return raw

    def replace_report(self, name, field, value):
        def walk(node):
            if isinstance(node, dict):
                if node.get("file") == name and field in node:
                    node[field] = copy.deepcopy(value)
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(self.report)

    def transition(self, kind):
        return next(row for row in self.report["transitions"] if row["kind"] == kind)

    def operation(self, transition, change):
        name = transition["operation"]["file"]
        raw = self.raw_json(name, change)
        self.replace_report(name, "report", raw)

    def save(self):
        path = self.directory / "report.json"
        path.write_text(json.dumps(self.report))
        return path


def pte(m, field, value):
    name = m.report["sharing_audits"][0]["audits"][0]["pte_audit_file"]
    def change(raw):
        row = next(p for p in raw["pages"] if p["shared"] and p["present"])
        row[field] = value(row[field]) if callable(value) else value
    m.raw_json(name, change)


def remove_shared(m):
    name = m.report["sharing_audits"][0]["audits"][0]["pte_audit_file"]
    def change(raw):
        count = 0
        for row in raw["pages"]:
            if row["shared"] and row["present"] and count < 100 * 1024 * 1024 // PAGE:
                row["shared"] = False
                count += 1
        assert count == 100 * 1024 * 1024 // PAGE
    m.raw_json(name, change)


def unauthorized_page(m):
    transition = m.transition("promotion")
    def change(raw):
        shared = next(row for row in raw["mappings"] if row["shared"])
        private = next(row for row in raw["mappings"] if not row["shared"])
        shared["offset"], private["offset"] = private["offset"], shared["offset"]
        shared["shared"], private["shared"] = False, True
    m.raw_json(transition["export"]["file"], change)


def closed_status(m):
    transition = m.transition("promotion")
    def change(raw):
        data = bytearray.fromhex(raw["registration_status_hex"])
        data[8:12] = (3).to_bytes(4, "little")
        raw["registration_status_hex"] = data.hex()
    m.operation(transition, change)


def stale_status_digest(m):
    transition = m.transition("promotion")
    def change(raw):
        data = bytearray.fromhex(raw["registration_status_hex"])
        data[88] ^= 1
        raw["registration_status_hex"] = data.hex()
    m.operation(transition, change)


def retained_cxl_demotion(m):
    transition = m.transition("demotion")
    def change(raw):
        row = raw["dram_vmas"][0]
        row["maps"] = [f"{row['hva']:x}-{row['hva']+row['bytes']:x} rw-s 00000000 00:01 123 /dev/cxl_pool"]
    m.operation(transition, change)


def no_nfs_read(m):
    refill = m.report["remote_refills"][0]
    before = [line.split() for line in m.data(refill["before_file"]).decode().splitlines() if line.startswith("proc3 ")][0]
    text = m.data(refill["after_file"]).decode()
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("proc3 "):
            fields = line.split()
            fields[8] = before[8]
            lines[i] = " ".join(fields) + "\n"
    m.write(refill["after_file"], "".join(lines).encode())
    refill["nfs_read_delta"] = 0


def missing_fence(m):
    transition = m.transition(m.report["mode"])
    name = transition["source_fence_file"]
    m.write(name, m.data(name).replace(b"FENCED ", b"ALIVE "))


def different_fenced_process(m):
    name = m.transition(m.report["mode"])["source_fence_file"]
    text = m.data(name).decode()
    text, changed = re.subn(r"^FENCED (\d+) (\d+)(\r?)$",
                           lambda match: f"FENCED {int(match[1]) + 1} {match[2]}{match[3]}", text, flags=re.M)
    assert changed == 1
    m.write(name, text.encode())


def dirty_authority(m):
    authority = m.report["authority_events"][0]
    summary = copy.deepcopy(authority["summary"])
    summary["clean"] = False
    lines = []
    for line in m.data(authority["file"]).decode().splitlines(keepends=True):
        if line.startswith('{"event":"guest_pool_registration"'):
            line = json.dumps(summary, separators=(",", ":")) + "\n"
        lines.append(line)
    m.write(authority["file"], "".join(lines).encode())
    m.replace_report(authority["file"], "summary", summary)


def full_ram_snapshot(m):
    api = next(a for a in m.report["api_events"] if a["path"] == "/snapshot/create")
    name = api["file"]
    old = api["request"]["mem_file_path"]
    api["request"]["mem_file_path"] = "/tmp/full-ram-snapshot"
    m.write(name, m.data(name).replace(old.encode(), b"/tmp/full-ram-snapshot"))


def alias_allocation(m):
    live = {}
    for event in m.report["allocator_events"]:
        if event["operation"] == "release":
            live.pop(event["owner"])
        else:
            other = next((entry for owner, entry in live.items() if owner != event["owner"]), None)
            if other:
                event["ranges"] = copy.deepcopy(other["ranges"])
                event["pages"] = other["pages"]
                return
            live[event["owner"]] = event
    raise AssertionError("no simultaneous live allocation pair")


def grant_credit_only(m):
    reused = m.report["cross_vm_reuse"][0]
    transitions = [t for t in m.report["transitions"] if t["kind"] == "promotion"
                   and t["vm"] == reused["destination_vm"]]
    raw = json.loads(m.data(transitions[-1]["export"]["file"]))
    credits = set(raw["credits"])
    claimed = {r["offset"] + i * PAGE for r in reused["ranges"] for i in range(r["pages"])}
    selected = sorted(credits & claimed)
    assert selected
    reused["ranges"] = [{"offset": offset, "pages": 1} for offset in selected]
    reused["pages"] = len(selected)


def stale_manifest_epoch(m):
    transition = m.transition(m.report["mode"])
    m.raw_json(transition["destination_placement"]["file"],
               lambda raw: raw.update(run_epoch=str(transition["source_epoch"])))


def new_direct_vm(m):
    transition = m.transition("direct")
    raw = m.raw_json(transition["device_state_file"], lambda raw: raw["token"].update(vm_creates=2))
    transition["token"] = raw["token"]


def different_direct_token(m):
    transitions = [t for t in m.report["transitions"] if t["kind"] == "direct"]
    transition = transitions[-1]
    raw = m.raw_json(transition["device_state_file"],
                     lambda raw: raw["token"].update(vm_offset=raw["token"]["vm_offset"] + PAGE))
    transition["token"] = raw["token"]
    old_event = copy.deepcopy(transition["import_event"])
    transition["import_event"]["token"]["vm_offset"] = raw["token"]["vm_offset"]
    for item in m.report["receipts"]:
        if item.get("kind") != "direct-import-log":
            continue
        lines, changed = [], False
        for line in m.data(item["file"]).decode().splitlines(keepends=True):
            if "DIRECT_HANDOFF_JSON " in line:
                prefix, data = line.split("DIRECT_HANDOFF_JSON ", 1)
                if json.loads(data) == old_event:
                    line = prefix + "DIRECT_HANDOFF_JSON " + json.dumps(transition["import_event"]) + "\n"
                    changed = True
            lines.append(line)
        if changed:
            m.write(item["file"], "".join(lines).encode())


def event_change(m, event, change):
    name = event["file"]
    old = copy.deepcopy(event["receipt"])
    raw = copy.deepcopy(old)
    change(raw)
    lines = []
    for line in m.data(name).decode().splitlines(keepends=True):
        if line.startswith('{"event":') and json.loads(line) == old:
            line = json.dumps(raw, separators=(",", ":")) + "\n"
        lines.append(line)
    m.write(name, "".join(lines).encode())
    m.replace_report(name, "receipt", raw)


def reuse_before_ack(m):
    event_change(m, m.report["reuse_tests"][0]["allocated"], lambda row: row.update(mono_start_ns=1))


def reuse_aliases_canonical(m):
    reuse = m.report["reuse_tests"][0]
    before = json.loads(m.data(reuse["before"]["audits"][0]["pte_audit_file"]))
    old = {p["gpa"]: p for p in before["pages"] if p["shared"] and p["present"]}
    gpas = set()
    for line in m.data(reuse["anon_gpa_file"]).decode().splitlines():
        _, _, _, gpa, count = line.split()
        gpas.update(range(int(gpa), int(gpa) + int(count) * PAGE, PAGE))
    gpa = next(iter(gpas & old.keys()))
    def change(raw):
        page = raw["pages"][gpa // PAGE]
        page["offset"] = old[gpa]["offset"]
        page["actual_pfn"] = old[gpa]["actual_pfn"]
    m.raw_json(reuse["pte_audit_file"], change)


def negative_accepted(m):
    case = m.report["negative_controls"][0]["cases"][0]
    text = m.data(case["file"]).decode()
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("LAB_API_JSON "):
            row = json.loads(line.split(" ", 1)[1])
            row["status"] = 204
            line = "LAB_API_JSON " + json.dumps(row) + "\n"
        lines.append(line)
    m.write(case["file"], "".join(lines).encode())
    case["status"] = 204


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.report.resolve()
    report = json.loads(path.read_bytes())
    positive = verify(path, pilot=args.pilot)
    cases = [
        ("wrong_actual_pfn", lambda m: pte(m, "actual_pfn", lambda x: x + 1), "actual native PTE"),
        ("writable_canonical", lambda m: pte(m, "writable", True), "canonical PTE writable"),
        ("remove_100mib_sharing", remove_shared, "sharing count"),
        ("omit_one_vm", lambda m: m.report["sharing_audits"][0]["audits"].pop(), "sharing group omits"),
        ("bloom_extra_page", unauthorized_page, "unauthorized file page"),
        ("private_grant_alias", alias_allocation, "physical allocation alias"),
        ("missing_source_fence", missing_fence, "source incarnation fence"),
        ("dirty_guest_authority", dirty_authority, "ineligible guest page-cache authority"),
        ("stale_manifest_epoch", stale_manifest_epoch, "manifest incarnation"),
        ("no_nfs_read", no_nfs_read, "bypassed NFS"),
    ]
    if not args.pilot:
        cases += [("fenced_wrong_process", different_fenced_process, "source fence refers to another process")]
    if report["mode"] == "state-only":
        cases += [("full_ram_snapshot_path", full_ram_snapshot, "full-RAM snapshot"),
                  ("credits_not_actual_reuse", grant_credit_only, "actually reused")]
    if report["mode"] == "direct":
        cases += [("new_direct_vm", new_direct_vm, "reconstructed KVM"),
                  ("different_retained_vm", different_direct_token, "replaced execution objects")]
    missing = []
    first = next(t for t in report["transitions"] if t["kind"] == "promotion")
    if "registration_status_hex" in json.loads((path.parent / first["operation"]["file"]).read_bytes()):
        cases += [("closed_kernel_authority", closed_status, "inactive/stale kernel status"),
                  ("wrong_live_status_digest", stale_status_digest, "status digest")]
    else:
        missing.append("live status mutations require new formal instrumentation")
    first = next(t for t in report["transitions"] if t["kind"] == "demotion")
    if "dram_vmas" in json.loads((path.parent / first["operation"]["file"]).read_bytes()):
        cases += [("demotion_retains_cxl", retained_cxl_demotion, "retained file/CXL")]
    else:
        missing.append("demotion maps mutation requires new formal instrumentation")
    if report.get("reuse_tests"):
        cases += [("reuse_before_guest_ack", reuse_before_ack, "reused before guest ACK"),
                  ("reused_private_pte_aliases_canonical", reuse_aliases_canonical, "still aliases canonical")]
    if report.get("negative_controls"):
        cases += [("unsafe_plan_accepted", negative_accepted, "unsafe authority plan was accepted")]
    if report.get("overlay_tests"):
        cases += [("overlay_changed_lower", lambda m: event_change(m, m.report["overlay_tests"][0]["event"],
                   lambda row: row.update(lower_unchanged=False)), "OverlayFS copy-up incomplete/changed lower")]
    results = []
    for name, change, reason in cases:
        with tempfile.TemporaryDirectory(prefix="handoff-evidence-mutation-") as tmp:
            mutation = Mutation(path.parent, report, Path(tmp))
            change(mutation)
            try:
                verify(mutation.save(), pilot=args.pilot)
            except ValueError as exc:
                if reason not in str(exc):
                    raise AssertionError(f"{name}: rejected for unrelated reason {exc}") from exc
                results.append({"case": name, "rejected": True, "reason": str(exc), "receipts_rehashed": True})
            else:
                raise AssertionError("Accepted corrupt evidence: " + name)
    result = {"result": "passed", "positive": positive, "mutations": results, "not_applicable": missing}
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")


if __name__ == "__main__":
    main()
