import unittest

from handoff_bench import PAGE, FrameAllocator, promotion_layout, validate_export


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.a = FrameAllocator(PAGE, 33 * PAGE)

    def test_entire_ram_gets_exclusive_frames(self):
        layout = promotion_layout(self.a, 'v1', 4*PAGE)
        self.assertEqual([m['gpa'] for m in layout['mappings']], list(range(0, 4*PAGE, PAGE)))
        self.assertEqual({m['offset'] for m in layout['mappings']}, self.a.grants['v1'].frames)






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
    def test_ept_launch_uses_only_local_vm_and_shared_ept_contract(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import Mock
        from handoff_bench import HandoffController
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'manifest.json').write_text(json.dumps({'traces':{'sample':{'name':'sample'}}}))
            args=SimpleNamespace(name='sample',memory_mib=2048,vcpus=2,workload='dirty',
                noncoherent=True,dirty_oracle=False,restore_timing=True,fail=None,native=True,baseline=False)
            c=HandoffController({'assets':str(root),'cxl_mib':4096,'vms':{'A':{},'B':{}}},root,args)
            c.ept_phys={'A':0x100000000,'B':0x100000000}
            c.shell=Mock();c.write_bytes=Mock(return_value=('', 'launch'))
            c.start_vm(c.sandboxes[0])
            script=c.write_bytes.call_args.args[2].decode()
            self.assertIn('FC_EPT_ADOPT=1',script)
            self.assertIn('FC_EPT_BASELINE=0',script)
            self.assertIn('FC_NONCOHERENT=1',script)
            self.assertIn('FC_POOL_MEMORY=1',script)
            self.assertNotIn('FC_DAX_DIRECT',script)
            self.assertNotIn('FC_DAX_SNAPSHOT',script)


if __name__=='__main__':unittest.main()
