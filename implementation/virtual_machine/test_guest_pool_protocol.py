"""Authority, identity and stale-token negatives for guest pooling registration."""
import copy
import json
import unittest

from guest_pool_protocol import RegistrationError, parse_registration, registration_digest, select_registered_pages


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"inode": 12, "inode_generation": 7, "fs_uuid": "ab" * 16, "file_bytes": 16384}
        self.summary = {"event": "guest_pool_registration", "label": "p1", "vm": 1,
            "boot_id": "boot-a", "cookie": "d30e", "epoch": 4, "token": 123, "session": 1,
            **self.identity, "registration_active": True, "readonly": True,
            "uptodate": True, "clean": True, "writeback": False, "selected_pages": 3,
            "status_gpa": 4096, "image_id": "a" * 64, "file_slot": 0, "start_page": 0,
            "page_count": 4, "dev_major": 252, "dev_minor": 0}
        self.ranges = [{"event": "guest_pool_range", "label": "p1", "file_page": 0, "gpa": 32768,
                        "pages": 2, "file_slot": 0, "flags": 63, "disk_offset": 1048576},
                       {"event": "guest_pool_range", "label": "p1", "file_page": 3, "gpa": 65536,
                        "pages": 1, "file_slot": 0, "flags": 63, "disk_offset": 2101248}]
        records = [{"file_slot": 0, "flags": 63, "file_page": r["file_page"] + i,
                    "gpa": r["gpa"] + i * 4096, "disk_offset": r["disk_offset"] + i * 4096}
                   for r in self.ranges for i in range(r["pages"])]
        file = {key: self.summary[key] for key in ("inode", "file_bytes", "start_page", "page_count",
                 "inode_generation", "file_slot", "fs_uuid", "dev_major", "dev_minor")}
        self.summary["records_sha256"] = registration_digest({"files": [file], "records": records,
            "session_cookie": 1, "vm_epoch": 4, "plan_generation": 123, "image_id": "a" * 64})

    def parse(self, summary=None, ranges=None):
        text = "\n".join(json.dumps(x, separators=(",", ":")) for x in
                         [summary or self.summary, *(self.ranges if ranges is None else ranges)])
        return parse_registration(text, label="p1", vm=1, boot_id="boot-a", cookie="d30e",
            epoch=4, token=123, image_id="a" * 64, file_identity=self.identity,
            extents=[[0, 1048576, 8192], [8192, 2097152, 8192]], memory_bytes=512 * 1024 * 1024)

    def test_logical_file_offsets_use_frozen_extents(self):
        value = self.parse()
        self.assertEqual([p["image_offset"] for p in value["pages"]], [1048576, 1052672, 2101248])
        self.assertEqual(value["registered_bytes"], 12288)

    def test_old_incarnation_or_release_cannot_authorize(self):
        for field, value in (("vm", 2), ("boot_id", "other"), ("cookie", "other"),
                             ("epoch", 3), ("token", 122), ("registration_active", False)):
            with self.subTest(field=field), self.assertRaises(RegistrationError):
                self.parse({**self.summary, field: value})

    def test_lower_identity_and_state_required(self):
        for field, value in (("inode", 13), ("inode_generation", 8), ("fs_uuid", "cd" * 16),
                             ("readonly", False), ("clean", False), ("uptodate", False), ("writeback", True)):
            with self.subTest(field=field), self.assertRaises(RegistrationError):
                self.parse({**self.summary, field: value})

    def test_kernel_authority_digest_and_status_address_required(self):
        for field, value in (("records_sha256", "0" * 64), ("status_gpa", 5), ("image_id", "b" * 64)):
            with self.subTest(field=field), self.assertRaises(RegistrationError):
                self.parse({**self.summary, field: value})

    def test_partial_duplicate_and_alias_ranges_rejected(self):
        mutants = [self.ranges[:1], self.ranges + self.ranges[:1]]
        for field, value in (("file_page", 0), ("gpa", 32768), ("gpa", 3),
                             ("gpa", 512 * 1024 * 1024), ("pages", 0), ("pages", True)):
            mutant = copy.deepcopy(self.ranges)
            mutant[1][field] = value
            mutants.append(mutant)
        for mutant in mutants:
            with self.subTest(mutant=mutant), self.assertRaises(RegistrationError):
                self.parse(ranges=mutant)

    def test_bloom_false_positive_never_introduces_page(self):
        registration = self.parse()
        self.assertEqual(select_registered_pages(registration, {17, 1048576}, {17}), [])
        result = select_registered_pages(registration, {17, 1048576}, {17, 1048576})
        self.assertEqual([p["image_offset"] for p in result], [1048576])

    def test_bloom_false_negative_only_reduces_sharing(self):
        registration = self.parse()
        keys = {p["image_offset"] for p in registration["pages"]}
        self.assertEqual(select_registered_pages(registration, set(), keys), [])


if __name__ == "__main__":
    unittest.main()
