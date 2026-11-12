import unittest

from handoff_bench import PAGE, FrameAllocator, promotion_layout, validate_export


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.a = FrameAllocator(PAGE, 33 * PAGE)

    def test_entire_ram_gets_exclusive_frames(self):
        layout = promotion_layout(self.a, 'v1', 4*PAGE)
        self.assertEqual([m['gpa'] for m in layout['mappings']], list(range(0, 4*PAGE, PAGE)))
        self.assertEqual({m['offset'] for m in layout['mappings']}, self.a.grants['v1'].frames)

    def test_live_release_and_double_release_rejected(self):
        self.a.allocate("v1", 4)
        for fenced, detached in ((False, True), (True, False)):
            with self.assertRaises(RuntimeError):
                self.a.release("v1", fenced=fenced, detached=detached)
        self.a.release("v1", fenced=True, detached=True)
        with self.assertRaises(RuntimeError):
            self.a.release("v1", fenced=True, detached=True)

    def test_retired_frames_reallocated_to_different_owner_generation(self):
        before = self.a.allocate("v1", 4)
        generation = self.a.grants["v1"].generation
        self.a.release("v1", fenced=True, detached=True)
        after = self.a.allocate("v2", 4)
        self.assertEqual(before, after)
        self.assertGreater(self.a.grants["v2"].generation, generation)

    def test_old_valid_manifest_rejected_after_same_owner_frame_reuse(self):
        old = promotion_layout(self.a, 'v1', 4*PAGE)
        self.a.release('v1', fenced=True, detached=True)
        new = promotion_layout(self.a, 'v1', 4*PAGE)
        self.assertEqual(old['mappings'], new['mappings'])
        manifest = {'version': 4, 'allocation_generation': old['allocation_generation'], 'run_epoch': '1',
                    'handle': {'abi': 4, 'count': 4, 'owner_epoch': 1, 'state': 1,
                               'allocation_generation': old['allocation_generation']}}
        with self.assertRaises(RuntimeError):
            validate_export(self.a, 'v1', manifest, 4*PAGE)

    def test_extent_allocator_matches_page_model_under_fragmentation(self):
        import random
        rng = random.Random(1102)
        free = set(range(PAGE, 33*PAGE, PAGE))
        live = {}
        for step in range(500):
            if live and (not free or rng.randrange(3) == 0):
                owner = rng.choice(list(live))
                returned = self.a.release(owner, fenced=True, detached=True)
                self.assertEqual(set(returned), live.pop(owner))
                free.update(returned)
            else:
                owner = 'owner-' + str(step)
                count = rng.randrange(1, min(len(free), 5)+1)
                wanted = sorted(free)[:count]
                self.assertEqual(self.a.allocate(owner, count), wanted)
                free.difference_update(wanted)
                live[owner] = set(wanted)
            self.assertEqual(self.a.free_pages, len(free))
            self.a.check()  # Explicit test audit, absent from runtime operations.

    def test_handoff_never_enumerates_granted_frames(self):
        self.a.allocate('v1', 4)
        class NoIteration(set):
            def __iter__(self):
                raise AssertionError('handoff scanned allocation')
        self.a.grants['v1'].frames = NoIteration(self.a.grants['v1'].frames)
        manifest = {'version': 4, 'allocation_generation': self.a.grants['v1'].generation, 'run_epoch': '1',
            'handle': {'abi': 4, 'count': 4, 'owner_epoch': 1, 'state': 1,
                       'allocation_generation': self.a.grants['v1'].generation}}
        validate_export(self.a, 'v1', manifest, 4*PAGE)


class RunnerLaunchTest(unittest.TestCase):
    def test_boot_and_prepared_target_have_private_resident_contract(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import Mock
        from handoff_bench import HandoffController, Sandbox
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'manifest.json').write_text(json.dumps({'traces': {'sample': {'name': 'sample'}}}))
            c = HandoffController({'assets': str(root), 'vms': {'A': {}, 'B': {}}}, root,
                SimpleNamespace(mode='direct', name='sample', memory_mib=2048, full_audit=False, restore_timing=False))
            c.shell = Mock(return_value=('', 'shell'))
            c.write_bytes = Mock(return_value=('EXEC_PROCESS 12 34\n', 'launch'))
            s = c.sandboxes[0]
            c.start_vm(s)
            script = c.write_bytes.call_args.args[2].decode()
            self.assertIn('FC_RESIDENT_NATIVE=1', script)
            self.assertIn('FC_RESIDENT_BLOCK_PATH=/disk/sandbox.raw', script)
            self.assertIn('FC_DAX_DIRECT_IO=1', script)
            self.assertIn('FC_POOL_MEMORY=1', script)
            self.assertIn('FC_DAX_FIXED_HVA=0x400000000000', script)
            self.assertTrue(c.resident_workers['A']['imported'])
            c.allocator.allocate('owner', 1)
            target = Sandbox(vm=1, role='B', epoch=1, direct=True, owner='owner')
            c.write_bytes.return_value = ('RESIDENT_BOOT_JSON {"pid":56,"starttime":78}\n'
                'LAB_API_JSON {"status":204}\nEXEC_PROCESS 56 78\n', 'native')
            c.start_vm(target, restore_manifest='/incoming', direct_import='/direct', purpose='resident-prepare')
            script = c.write_bytes.call_args.args[2].decode()
            self.assertIn('FC_POOL_MEMORY_LEASE=1', script)
            self.assertIn('FC_DAX_DIRECT_IMPORT=/direct', script)
            self.assertIn('/lab/resident-bootstrap', c.write_bytes.call_args.kwargs['after'])
            self.assertFalse(c.resident_workers['B']['imported'])
            self.assertEqual(c.resident_workers['B']['prepared_api']['request'], {'action_type': 'DirectPrepare'})


if __name__ == "__main__":
    unittest.main()
