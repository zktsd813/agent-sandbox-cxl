#!/usr/bin/env python3
"""Control-protocol unit checks; these do not substitute for KVM/CXL evidence."""
import copy
from types import SimpleNamespace
import unittest

from pool_control import PoolControl


class MembershipOnly:
    """Owner index must not be scanned before a positive Bloom result."""
    def __init__(self, keys):
        self.keys, self.lookups = set(keys), []

    def __contains__(self, key):
        self.lookups.append(key)
        return key in self.keys

    def __iter__(self):
        raise AssertionError("owner index scanned without candidate lookup")


class FakeVmm:
    image_id = "a" * 64
    image_bytes = 16384
    payload_extents = [(4096, 8192)]
    pool_offset = 2 << 30

    def __init__(self):
        self.receipts = []
        self.plans = []

    def write_json(self, role, path, value, kind):
        self.plans.append(copy.deepcopy(value))

    def api(self, *args):
        pass

    def fetch_json(self, role, path, kind):
        self.receipts.append({"file": kind + ".json"})
        if kind == "owner-hints":
            return {"version": 1, "vm_id": "2", "run_epoch": "3", "hints": [
                {"gpa": 12288, "image_id": self.image_id, "image_offset": 4096, "sequence": 1}]}
        pages = self.plans[-1]["pages"]
        return {"mapped": len(pages), "mapped_pages": [{"gpa": p["gpa"]} for p in pages]}


class ControlTests(unittest.TestCase):
    def exercise(self, hits, owner_keys, ready=True, tier="cxl"):
        c = FakeVmm()
        control = PoolControl(c)
        owner = MembershipOnly(owner_keys)
        control.index[1] = {"keys": owner, "epoch": 1, "tier": "dram", "role": "A"}
        if ready:
            control.ready_keys.add(4096)
        calls = []
        def bloom(sandbox, operation, keys, slot):
            calls.append((operation, slot, sandbox.tier))
            return (set(hits) if operation == "probe" else set(), {"slot": slot})
        control._bloom = bloom
        sandbox = SimpleNamespace(vm=2, epoch=3, role="B", tier=tier,
                                  plan="plan", hints="hints", report="report", directory="fc")
        result = control.admit(sandbox)
        return c, control, owner, calls, result

    def test_stale_negative_keeps_private_and_never_reads_owner_index(self):
        c, _, owner, _, result = self.exercise([], [4096])
        self.assertEqual(owner.lookups, [])
        self.assertEqual(c.plans[-1]["pages"], [])
        self.assertEqual(result["retained_private_after_hint_miss"], 1)

    def test_false_positive_requires_exact_owner_match(self):
        c, _, owner, _, result = self.exercise([4096], [])
        self.assertEqual(owner.lookups, [4096])
        self.assertEqual(c.plans[-1]["pages"], [])
        self.assertEqual(result["discovery"][0]["false_positive_candidates"], 1)

    def test_positive_queries_only_candidate_and_reuses_canonical(self):
        c, _, owner, _, _ = self.exercise([4096], [4096, 8192])
        self.assertEqual(owner.lookups, [4096])
        self.assertEqual(c.plans[-1]["pages"][0]["pool_offset"], c.pool_offset + 4096)

    def test_first_admission_can_publish_new_ready_entry(self):
        c, control, owner, _, _ = self.exercise([], [], ready=False)
        self.assertEqual(owner.lookups, [])
        self.assertEqual(len(c.plans[-1]["pages"]), 1)
        self.assertEqual(control.ready_keys, {4096})

    def test_dram_updates_stable_bloom_without_data_mapping(self):
        c, _, owner, calls, _ = self.exercise([4096], [4096], tier="dram")
        self.assertEqual(owner.lookups, [4096])
        self.assertIn(("build", 2, "dram"), calls)
        self.assertEqual(c.plans[-1]["pages"], [])


if __name__ == "__main__":
    unittest.main()
