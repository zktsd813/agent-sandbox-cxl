"""Validate current guest cache registrations for immutable SWE root images.

The CLI only enumerates resident lower-ext4 folios. This parser compares each
file identity and bmap result with independently frozen image metadata. Bloom
results may subsequently discard candidates, but cannot introduce new pages.
"""
import json
import struct
import hashlib

PAGE = 4096
MAX_FILES = 16
MAX_PAGES = 65536


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, name, maximum=(1 << 64) - 1):
    require(type(value) is int and 0 <= value <= maximum, "invalid integer " + name)
    return value


def hex_bytes(value, length, name):
    require(isinstance(value, str) and len(value) == length * 2 and
            all(c in "0123456789abcdef" for c in value), "invalid hex " + name)
    return bytes.fromhex(value)


def registration_digest(registration):
    """The public kernel UAPI's ordered identity/record digest."""
    files, records = registration["files"], registration["records"]
    digest = hashlib.sha256(b"GPOOL_RECORDS_V1")
    digest.update(struct.pack("<IIQQQQ32s", 1, len(files), len(records),
                              registration["session_cookie"], registration["vm_epoch"],
                              registration["plan_generation"], bytes.fromhex(registration["image_id"])))
    for f in files:
        digest.update(struct.pack("<QQQQII16sII", f["inode"], f["file_bytes"], f["start_page"],
                                  f["page_count"], f["inode_generation"], f["file_slot"],
                                  bytes.fromhex(f["fs_uuid"]), f["dev_major"], f["dev_minor"]))
    for r in records:
        digest.update(struct.pack("<IIQQQ", r["file_slot"], r["flags"], r["file_page"],
                                  r["gpa"], r["disk_offset"]))
    return digest.hexdigest()


def parse_registration(text, *, image_id, epoch, token, files, memory_bytes, image_bytes,
                       device=(253, 0)):
    hex_bytes(image_id, 32, "image_id")
    integer(epoch, "epoch")
    require(integer(token, "token") != 0, "zero transition token")
    require(memory_bytes > 0 and memory_bytes % PAGE == 0 and image_bytes > 0,
            "invalid memory/image geometry")
    require(1 <= len(files) <= MAX_FILES, "file count outside guest ABI")
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed CLI registration") from exc
        require(type(value) is dict, "CLI record is not an object")
        rows.append(value)
    require(len(rows) >= len(files) + 2 and rows[0].get("kind") == "guest_pool_registration"
            and rows[-1].get("kind") == "guest_pool_status", "missing or truncated CLI registration")
    header, status = rows[0], rows[-1]
    require(header.get("version") == 1 and header.get("nr_files") == len(files), "registration ABI/files")
    require(header.get("image_id") == image_id and header.get("vm_epoch") == epoch
            and header.get("plan_generation") == token, "stale image/epoch/token")
    cookie = integer(header.get("session_cookie"), "session_cookie")
    require(cookie != 0, "zero session cookie")
    status_gpa = integer(header.get("status_gpa"), "status_gpa")
    require(status_gpa % PAGE == 0 and status_gpa + PAGE <= memory_bytes, "status outside guest RAM")
    count = integer(header.get("selected_pages"), "selected_pages", MAX_PAGES)
    require(len(rows) == count + len(files) + 2, "record count incomplete or duplicated")
    file_rows, record_rows = rows[1:1 + len(files)], rows[1 + len(files):-1]
    identities, checked_extents, inodes = [], [], set()
    requested = 0
    for slot, (frozen, raw) in enumerate(zip(files, file_rows)):
        require(raw.get("kind") == "guest_pool_file" and raw.get("file_slot") == slot,
                "file slot order")
        for field in ("inode", "inode_generation", "file_bytes", "fs_uuid"):
            require(raw.get(field) == frozen[field], "wrong immutable file " + field)
        inode = integer(raw.get("inode"), "inode")
        require(inode not in inodes, "duplicate immutable inode")
        inodes.add(inode)
        integer(raw.get("inode_generation"), "inode_generation", (1 << 32) - 1)
        file_bytes = integer(raw.get("file_bytes"), "file_bytes")
        require(file_bytes > 0, "empty immutable file")
        hex_bytes(raw.get("fs_uuid"), 16, "fs_uuid")
        page_count = (file_bytes + PAGE - 1) // PAGE
        require(raw.get("start_page") == 0 and raw.get("page_count") == page_count,
                "unexpected file range")
        require((raw.get("dev_major"), raw.get("dev_minor")) == tuple(device), "wrong lower device")
        requested += page_count
        extents = []
        last_end = 0
        for extent in frozen["extents"]:
            require(type(extent) is list and len(extent) == 3, "invalid frozen extent")
            logical, disk, length = [integer(x, "extent") for x in extent]
            require(length > 0 and (logical | disk | length) % PAGE == 0 and logical >= last_end
                    and logical + length <= page_count * PAGE
                    and disk + length <= image_bytes, "overlapping or out-of-range frozen extent")
            extents.append((logical, disk, length))
            last_end = logical + length
        checked_extents.append(extents)
        identities.append({k: raw[k] for k in ("inode", "file_bytes", "start_page", "page_count",
            "inode_generation", "file_slot", "fs_uuid", "dev_major", "dev_minor")})
    require(requested <= MAX_PAGES and header.get("requested_pages") == requested,
            "requested pages outside frozen file scope")
    require(count <= requested, "selected pages exceed requested pages")
    gpas, previous, records, pages = set(), None, [], []
    for raw in record_rows:
        require(raw.get("kind") == "guest_pool_record" and raw.get("flags") == 63,
                "guest page is not locked clean immutable data")
        slot = integer(raw.get("file_slot"), "file_slot", len(files) - 1)
        index = integer(raw.get("file_page"), "file_page")
        gpa = integer(raw.get("gpa"), "gpa")
        disk = integer(raw.get("disk_offset"), "disk_offset")
        key = (slot, index)
        require(previous is None or previous < key, "duplicate or unordered file page")
        previous = key
        require(gpa % PAGE == 0 and gpa + PAGE <= memory_bytes and gpa != status_gpa
                and gpa not in gpas, "invalid or aliased GPA")
        gpas.add(gpa)
        require(index < identities[slot]["file_bytes"] // PAGE, "partial EOF or outside file")
        logical = index * PAGE
        matches = [start + logical - first for first, start, size in checked_extents[slot]
                   if first <= logical and logical + PAGE <= first + size]
        require(len(matches) == 1 and matches[0] == disk, "guest bmap differs from frozen image")
        record = {k: raw[k] for k in ("file_slot", "flags", "file_page", "gpa", "disk_offset")}
        records.append(record)
        pages.append({"gpa": gpa, "image_id": image_id, "image_offset": disk,
                      "file_slot": slot, "file_page": index, "inode": identities[slot]["inode"]})
    registration = {"status_gpa": status_gpa, "session_cookie": cookie, "vm_epoch": epoch,
                    "plan_generation": token, "image_id": image_id, "files": identities, "records": records}
    digest = registration_digest(registration)
    require(header.get("records_sha256") == digest, "registration digest mismatch")
    require(status.get("state") == 2 and status.get("session_cookie") == cookie
            and status.get("vm_epoch") == epoch and status.get("plan_generation") == token
            and status.get("record_count") == count and status.get("records_sha256") == digest,
            "inactive or mismatched final guest status")
    return {"summary": header, "files": identities, "pages": pages, "registration": registration,
            "registered_bytes": len(pages) * PAGE}
