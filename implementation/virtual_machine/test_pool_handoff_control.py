import copy
import unittest

from guest_pool_protocol import PAGE, RegistrationError
from pool_handoff_control import FrameAllocator, promotion_layout, validate_export


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.a = FrameAllocator(PAGE, 33 * PAGE)

    def test_shared_pages_never_receive_private_frames(self):
        layout = promotion_layout(self.a, "vm1:e1", 4 * PAGE,
                                  [{"gpa": PAGE, "pool_offset": 100 * PAGE}], 2)
        self.assertEqual(self.a.summary()["allocated_bytes"], 5 * PAGE)
        self.assertEqual(layout["mappings"][1], {"gpa": PAGE, "offset": 100 * PAGE, "shared": True})
        measured = validate_export(self.a, "vm1:e1", layout, 4 * PAGE, 100 * PAGE, PAGE)
        self.assertEqual(measured["unused_credit_bytes"], 2 * PAGE)

    def test_cow_consumes_credit_without_losing_ownership(self):
        layout = promotion_layout(self.a, "v1", 4 * PAGE, [{"gpa": PAGE, "pool_offset": 100 * PAGE}], 2)
        layout["mappings"][1].update(offset=layout["credits"].pop(), shared=False)
        measured = validate_export(self.a, "v1", layout, 4 * PAGE, 100 * PAGE, PAGE)
        self.assertEqual(measured["private_mapped_bytes"], 4 * PAGE)
        self.assertEqual(measured["unused_credit_bytes"], PAGE)

    def test_cross_owner_and_unaccounted_frames_rejected(self):
        layout = promotion_layout(self.a, "v1", 4 * PAGE, [], 2)
        foreign = self.a.allocate("v2", 1)[0]
        for offset in (foreign, 1000 * PAGE):
            bad = copy.deepcopy(layout)
            bad["mappings"][0]["offset"] = offset
            with self.assertRaises(RegistrationError):
                validate_export(self.a, "v1", bad, 4 * PAGE, 100 * PAGE, PAGE)

    def test_live_release_and_double_release_rejected(self):
        self.a.allocate("v1", 4)
        for fenced, detached in ((False, True), (True, False)):
            with self.assertRaises(RegistrationError):
                self.a.release("v1", fenced=fenced, detached=detached)
        self.a.release("v1", fenced=True, detached=True)
        with self.assertRaises(RegistrationError):
            self.a.release("v1", fenced=True, detached=True)

    def test_retired_frames_reallocated_to_different_owner_generation(self):
        before = self.a.allocate("v1", 4)
        generation = self.a.grants["v1"].generation
        self.a.release("v1", fenced=True, detached=True)
        after = self.a.allocate("v2", 4)
        self.assertEqual(before, after)
        self.assertGreater(self.a.grants["v2"].generation, generation)

    def test_handoff_transfers_ownership_without_allocating_or_copying(self):
        original = self.a.allocate("v1:e1", 4)
        generation = self.a.grants["v1:e1"].generation
        self.a.transfer("v1:e1", "v1:e2")
        self.assertEqual(self.a.grants["v1:e2"].frames, set(original))
        self.assertEqual(self.a.grants["v1:e2"].generation, generation)
        self.assertEqual(self.a.summary()["allocated_bytes"], 4 * PAGE)

    def test_old_valid_manifest_rejected_after_same_owner_frame_reuse(self):
        old = promotion_layout(self.a, "v1", 4 * PAGE, [], 1)
        self.a.release("v1", fenced=True, detached=True)
        new = promotion_layout(self.a, "v1", 4 * PAGE, [], 1)
        self.assertEqual(old["mappings"], new["mappings"])
        self.assertNotEqual(old["allocation_generation"], new["allocation_generation"])
        with self.assertRaises(RegistrationError):
            validate_export(self.a, "v1", old, 4 * PAGE, 100 * PAGE, PAGE)


if __name__ == "__main__":
    unittest.main()
