import hashlib
import json
import struct
import unittest

from swe_pool_protocol import PAGE, parse_registration


def fixture():
    files = [dict(path="/lower/usr/bin/tool", inode=42, inode_generation=0, fs_uuid="ab" * 16,
                  file_bytes=2 * PAGE + 73, extents=[[0, 20 * PAGE, 2 * PAGE]]),
             dict(path="/lower/usr/lib/libtool.so", inode=77, inode_generation=12, fs_uuid="ab" * 16,
                  file_bytes=2 * PAGE, extents=[[0, 31 * PAGE, 2 * PAGE]])]
    header = dict(kind="guest_pool_registration", version=1, session_cookie=55, vm_epoch=0,
                  plan_generation=99, status_gpa=PAGE, image_id="cd" * 32, nr_files=2,
                  selected_pages=2, requested_pages=5)
    ids = [dict(kind="guest_pool_file", file_slot=i, inode=f["inode"], inode_generation=f["inode_generation"],
                file_bytes=f["file_bytes"], fs_uuid=f["fs_uuid"], start_page=0,
                page_count=(f["file_bytes"] + PAGE - 1) // PAGE, dev_major=253, dev_minor=0)
           for i, f in enumerate(files)]
    records = [dict(kind="guest_pool_record", file_slot=0, flags=63, file_page=0, gpa=10 * PAGE,
                    disk_offset=20 * PAGE),
               dict(kind="guest_pool_record", file_slot=1, flags=63, file_page=1, gpa=11 * PAGE,
                    disk_offset=32 * PAGE)]
    status = dict(kind="guest_pool_status", state=2, session_cookie=55, vm_epoch=0,
                  plan_generation=99, record_count=2)
    return files, [header, *ids, *records, status]


def encode(rows):
    """Independent UAPI encoder lets corrupt records carry valid replacement hashes."""
    header, status = rows[0], rows[-1]
    files = [r for r in rows if r["kind"] == "guest_pool_file"]
    records = [r for r in rows if r["kind"] == "guest_pool_record"]
    b = bytearray(b"GPOOL_RECORDS_V1")
    b += struct.pack("<IIQQQQ", 1, len(files), len(records), header["session_cookie"],
                     header["vm_epoch"], header["plan_generation"])
    b += bytes.fromhex(header["image_id"])
    for f in files:
        for field in ("inode", "file_bytes", "start_page", "page_count"):
            b += f[field].to_bytes(8, "little")
        for field in ("inode_generation", "file_slot"):
            b += f[field].to_bytes(4, "little")
        b += bytes.fromhex(f["fs_uuid"])
        for field in ("dev_major", "dev_minor"):
            b += f[field].to_bytes(4, "little")
    for r in records:
        b += struct.pack("<IIQQQ", r["file_slot"], r["flags"], r["file_page"], r["gpa"], r["disk_offset"])
    header["selected_pages"] = status["record_count"] = len(records)
    header["records_sha256"] = status["records_sha256"] = hashlib.sha256(b).hexdigest()
    return "\n".join(json.dumps(r) for r in rows) + "\n"


class SweRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.files, self.rows = fixture()

    def parse(self, text=None):
        return parse_registration(encode(self.rows) if text is None else text, image_id="cd" * 32,
                                  epoch=0, token=99, files=self.files,
                                  memory_bytes=512 * 1024**2, image_bytes=4 * 1024**3)

    def test_multifile_current_cache_with_partial_eof_excluded(self):
        result = self.parse()
        self.assertEqual(result["registered_bytes"], 2 * PAGE)
        self.assertEqual([p["image_offset"] for p in result["pages"]], [20 * PAGE, 32 * PAGE])
        self.assertEqual(result["registration"]["files"][0]["page_count"], 3)

    def test_zero_resident_pages_is_explicit_private_fallback(self):
        self.rows = [r for r in self.rows if r["kind"] != "guest_pool_record"]
        self.assertEqual(self.parse()["registered_bytes"], 0)

    def test_extents_may_describe_last_partial_disk_block(self):
        self.files[0]["extents"][0][2] = 3 * PAGE
        self.assertEqual(self.parse()["registered_bytes"], 2 * PAGE)

    def test_rehashed_wrong_extent_and_unknown_gpa_rejected(self):
        for field, value, reason in (("disk_offset", 21 * PAGE, "bmap"),
                                     ("gpa", 512 * 1024**2, "GPA"),
                                     ("gpa", PAGE, "GPA")):
            with self.subTest(field=field, value=value):
                self.files, self.rows = fixture()
                self.rows[3][field] = value
                with self.assertRaisesRegex(ValueError, reason):
                    self.parse()

    def test_rehashed_dirty_and_partial_eof_rejected(self):
        self.rows[3]["flags"] = 31
        with self.assertRaisesRegex(ValueError, "clean"):
            self.parse()
        self.files, self.rows = fixture()
        self.rows[3]["file_page"] = 2
        with self.assertRaisesRegex(ValueError, "partial EOF"):
            self.parse()

    def test_duplicate_inode_and_cross_file_gpa_rejected(self):
        self.rows[4]["gpa"] = self.rows[3]["gpa"]
        with self.assertRaisesRegex(ValueError, "aliased GPA"):
            self.parse()
        self.files, self.rows = fixture()
        self.files[1]["inode"] = self.rows[2]["inode"] = 42
        with self.assertRaisesRegex(ValueError, "duplicate immutable inode"):
            self.parse()

    def test_wrong_file_identity_and_device_rejected(self):
        for field, value in (("inode", 99), ("inode_generation", 1), ("fs_uuid", "00" * 16),
                             ("dev_minor", 1)):
            with self.subTest(field=field):
                self.files, self.rows = fixture()
                self.rows[1][field] = value
                with self.assertRaises(ValueError):
                    self.parse()

    def test_stale_and_closed_status_rejected(self):
        for field, value in (("state", 3), ("session_cookie", 56), ("vm_epoch", 1),
                             ("plan_generation", 98)):
            with self.subTest(field=field):
                self.files, self.rows = fixture()
                self.rows[-1][field] = value
                with self.assertRaisesRegex(ValueError, "status"):
                    self.parse()

    def test_digest_damage_truncation_and_duplicate_rows_rejected(self):
        text = encode(self.rows)
        for damaged in (text.replace('"gpa": 40960', '"gpa": 40961'),
                        "\n".join(text.splitlines()[:-1]), text + text.splitlines()[-1]):
            with self.subTest(damaged=damaged[-30:]):
                with self.assertRaises(ValueError):
                    self.parse(damaged)

    def test_frozen_extent_outside_image_rejected(self):
        self.files[0]["extents"][0][1] = 4 * 1024**3
        with self.assertRaisesRegex(ValueError, "frozen extent"):
            self.parse()


if __name__ == "__main__":
    unittest.main()
