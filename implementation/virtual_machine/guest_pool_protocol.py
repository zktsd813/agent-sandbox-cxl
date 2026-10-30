"""Validate a live guest page-cache registration before creating a pool plan.

This module deliberately has no dependency on virtio read-history hints.  The
registration is produced by the guest kernel while it holds the selected
folios.  Its token is usable only for the VM incarnation/transition that asked
for it.  Bloom results may narrow the returned keys but cannot add a page.
"""
import hashlib
import json
import struct

PAGE = 4096
MAX_PAGES = 65536


class RegistrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RegistrationError(message)


def integer(value, field, minimum=0, maximum=(1 << 64) - 1):
    require(type(value) is int and minimum <= value <= maximum,
            "invalid registration " + field)
    return value


def registration_digest(registration):
    files, records = registration["files"], registration["records"]
    digest = hashlib.sha256(b"GPOOL_RECORDS_V1")
    digest.update(struct.pack("<IIQQQQ32s", 1, len(files), len(records), registration["session_cookie"],
                              registration["vm_epoch"], registration["plan_generation"],
                              bytes.fromhex(registration["image_id"])))
    for f in files:
        digest.update(struct.pack("<QQQQII16sII", f["inode"], f["file_bytes"], f["start_page"], f["page_count"],
                                  f["inode_generation"], f["file_slot"], bytes.fromhex(f["fs_uuid"]),
                                  f["dev_major"], f["dev_minor"]))
    for r in records:
        digest.update(struct.pack("<IIQQQ", r["file_slot"], r["flags"], r["file_page"], r["gpa"], r["disk_offset"]))
    return digest.hexdigest()


def parse_registration(text, *, label, vm, boot_id, cookie, epoch, token,
                       image_id, file_identity, extents, memory_bytes):
    """Return the checked summary, pages and a host plan, never fault in pages.

    ``extents`` is a frozen list of [logical offset, image offset, length]
    entries for this exact immutable image/inode.  Holes and partial pages are
    deliberately absent from the first implementation's eligible extents.
    ``file_identity`` is independently measured when preparing the image.
    """
    require(len(image_id) == 64 and all(c in "0123456789abcdef" for c in image_id),
            "invalid immutable image identity")
    require(memory_bytes > 0 and memory_bytes % PAGE == 0, "invalid guest memory size")
    rows = []
    for line in text.splitlines():
        if line.startswith('{"event":'):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RegistrationError("malformed guest event") from exc
            if row.get("label") == label and row.get("event", "").startswith("guest_pool_"):
                rows.append(row)
    summaries = [r for r in rows if r["event"] == "guest_pool_registration"]
    require(len(summaries) == 1, "registration summary missing or duplicated")
    summary = summaries[0]
    for field, expected in (("vm", vm), ("boot_id", boot_id), ("cookie", cookie),
                            ("epoch", epoch), ("token", token)):
        require(summary.get(field) == expected, "stale/wrong registration " + field)
    for field in ("inode", "inode_generation", "fs_uuid", "file_bytes"):
        require(summary.get(field) == file_identity[field], "wrong lower file " + field)
    require(summary.get("registration_active") is True, "guest registration already released")
    require(summary.get("readonly") is True and summary.get("uptodate") is True
            and summary.get("clean") is True and summary.get("writeback") is False,
            "guest did not establish clean immutable resident folios")
    count = integer(summary.get("selected_pages"), "selected_pages", maximum=MAX_PAGES)
    integer(summary.get("session"), "session", minimum=1)
    status_gpa = integer(summary.get("status_gpa"), "status_gpa")
    require(status_gpa % PAGE == 0 and status_gpa + PAGE <= memory_bytes, "invalid kernel status GPA")
    require(summary.get("image_id") == image_id, "guest registration bound to wrong image")
    file_bytes = integer(summary.get("file_bytes"), "file_bytes", minimum=PAGE)
    checked_extents = []
    logical_end = 0
    for item in extents:
        require(type(item) is list and len(item) == 3, "invalid frozen extent")
        logical, disk, length = (integer(x, "extent") for x in item)
        require(length > 0 and (logical | disk | length) % PAGE == 0
                and logical >= logical_end and logical + length <= file_bytes,
                "overlapping, unaligned or out-of-file frozen extent")
        logical_end = logical + length
        checked_extents.append((logical, disk, length))
    pages, records = [], []
    gpas = set()
    previous_page = -1
    ranges = [r for r in rows if r["event"] == "guest_pool_range"]
    for row in ranges:
        first = integer(row.get("file_page"), "file_page")
        gpa = integer(row.get("gpa"), "gpa")
        length = integer(row.get("pages"), "range pages", minimum=1, maximum=MAX_PAGES)
        require(row.get("flags") == 63 and row.get("file_slot") == 0, "incomplete guest folio conditions")
        disk_offset = integer(row.get("disk_offset"), "disk_offset")
        require(first > previous_page and first + length <= file_bytes // PAGE,
                "duplicate, unordered or out-of-file registration")
        require(gpa % PAGE == 0 and gpa + length * PAGE <= memory_bytes,
                "registration GPA outside guest RAM")
        require(len(pages) + length <= count, "more ranges than registered pages")
        for index in range(length):
            file_page, address = first + index, gpa + index * PAGE
            require(address not in gpas, "one GPA registered for multiple file pages")
            gpas.add(address)
            offset = file_page * PAGE
            matches = [(disk + offset - logical) for logical, disk, size in checked_extents
                       if logical <= offset and offset + PAGE <= logical + size]
            require(len(matches) == 1, "registered page has no unique frozen extent")
            require(disk_offset + index * PAGE == matches[0], "guest bmap and frozen image extent differ")
            pages.append({"gpa": address, "image_id": image_id, "image_offset": matches[0],
                          "file_page": file_page, "inode": summary["inode"],
                          "inode_generation": summary["inode_generation"]})
            records.append({"file_slot": 0, "flags": 63, "file_page": file_page,
                            "gpa": address, "disk_offset": matches[0]})
        previous_page = first + length - 1
    require(len(pages) == count, "registration ranges incomplete")
    file = {key: summary[key] for key in ("inode", "file_bytes", "start_page", "page_count",
             "inode_generation", "file_slot", "fs_uuid", "dev_major", "dev_minor")}
    require(file["file_slot"] == 0 and file["start_page"] == 0
            and file["page_count"] == file_bytes // PAGE, "unexpected registered file range")
    registration = {"status_gpa": status_gpa, "session_cookie": summary["session"], "vm_epoch": epoch,
                    "plan_generation": token, "image_id": image_id, "files": [file], "records": records}
    require(registration_digest(registration) == summary.get("records_sha256"), "registration digest mismatch")
    return {"summary": summary, "pages": pages, "registration": registration, "registered_bytes": count * PAGE}


def select_registered_pages(registration, bloom_keys, ready_keys):
    """Only registered pages with a positive hint AND exact READY entry qualify."""
    require(type(bloom_keys) is set and type(ready_keys) is set, "key sets required")
    return [p for p in registration["pages"]
            if p["image_offset"] in bloom_keys and p["image_offset"] in ready_keys]
