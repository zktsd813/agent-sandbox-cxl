"""Independent negative checks of emitted ranges and the CXL log wire format."""
import struct
import unittest

from dirty_oracle import DirtyOracle, MIB, kernel_receipt


class InstructionReceiptVersions(unittest.TestCase):
    def test_initial_invalidation_is_separate_from_sender_writeback(self):
        memory=bytearray(MIB)
        values=(4096,0,1026,0,74,2,80,0)
        struct.pack_into('<8Q',memory,MIB-64,*values)
        self.assertEqual(kernel_receipt(memory,0,64),values)

    def test_partial_initial_range_and_reserved_word_are_rejected(self):
        for initial,reserved in ((79,0),(80,1)):
            memory=bytearray(MIB)
            struct.pack_into('<8Q',memory,MIB-64,4096,0,1026,0,74,2,initial,reserved)
            with self.assertRaisesRegex(ValueError,'initialization receipt'):
                kernel_receipt(memory,0,64)


class MaintenanceReceipts(unittest.TestCase):
    def setUp(self):
        self.oracle = object.__new__(DirtyOracle)
        self.oracle.metadata = 0
        self.oracle.mem = bytearray(32*MIB+1024)
        self.oracle.demoted_pending = 0
        self.oracle.records, self.oracle.consumed = {}, {}
        struct.pack_into('<2Q', self.oracle.mem, 48, 1, 2)
        self.full = (1 << 64)-1
        self.publish(1, self.full)

    def packet(self, bitmap, wb=0, requested=0, invalidated=0):
        return struct.pack('<6Q', bitmap, 0, bitmap, wb, requested, invalidated)

    def extra(self, epoch, start=0, end=0):
        return [epoch, start, end, 0, 0, 0, 0, 0, 0, 4096, 4096, 64, 64, 1, 1, 0]

    def publish(self, epoch, bitmap):
        base = 32*MIB
        struct.pack_into('<8Q', self.oracle.mem, base,
                         0x4e43484c4f473031, 1, 1, 2, 64, epoch+1, 8, 64)
        struct.pack_into('<3Q', self.oracle.mem, base+64+((epoch-1)%8)*64,
                         epoch, bitmap.bit_count(), bitmap)
        return self.oracle.check_maintenance(1 if epoch==1 else 2, 1, 64,
            0 if epoch==1 else 4096, self.extra(epoch), self.packet(bitmap, wb=bitmap))

    def test_demotion_requires_a_fresh_record_even_if_preclean_published_the_page(self):
        self.publish(2, 1)
        self.oracle.demoted_pending = 1
        with self.assertRaisesRegex(ValueError, 'Demotion pages missing'):
            self.publish(3, 2)

    def test_source_omitted_writeback_range_is_rejected(self):
        self.publish(2, 3)
        # A physically emitted range is missing even though the published list is intact.
        self.oracle.records.pop(2)
        with self.assertRaisesRegex(ValueError, 'writeback ranges'):
            self.oracle.check_maintenance(2, 1, 64, 4096, self.extra(2), self.packet(3, wb=2))

    def test_receiver_missing_page_and_kernel_tail_are_rejected(self):
        extra = self.extra(1, 0, 1)
        with self.assertRaisesRegex(ValueError, 'receiver ranges'):
            self.oracle.check_maintenance(3, 2, 64, 4096, extra,
                self.packet(self.full, requested=self.full, invalidated=self.full ^ 1))
        extra[10] = 4095
        with self.assertRaisesRegex(ValueError, 'kernel invalidation'):
            self.oracle.check_maintenance(3, 2, 64, 4096, extra,
                self.packet(self.full, requested=self.full, invalidated=self.full))

    def test_reader_cannot_skip_unconsumed_records(self):
        with self.assertRaisesRegex(ValueError, 'unobserved records'):
            self.oracle.check_maintenance(3, 2, 64, 4096, self.extra(1, 1, 1),
                self.packet(0, requested=0, invalidated=0))

    def test_lagged_reader_requires_full_invalidation_after_wrap(self):
        for epoch in range(2, 11):
            self.publish(epoch, 1 << epoch)
        row = self.oracle.check_maintenance(3, 2, 64, 4096, self.extra(10, 0, 10),
            self.packet(self.full, requested=self.full, invalidated=self.full))
        self.assertTrue(row['full_invalidate_fallback'])
        self.assertEqual(self.oracle.consumed[2], 10)


class AccumulatedReplay(unittest.TestCase):
    def test_cleaned_pages_count_at_handoff_and_missing_union_is_rejected(self):
        import hashlib
        from pathlib import Path
        import tempfile
        from verify_swe_handoff import verify_cache_receipts
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dirty-oracle').mkdir();rows=[]
            def event(phase,epoch,node,bitmap,changed=None,start=0):
                sequence=len(rows)+1
                source=phase in (1,2,4)
                planes=[bitmap,0,bitmap,bitmap if source else 0,0 if source else bitmap,0 if source else bitmap]
                data=struct.pack('<6Q',*planes)
                (root/'dirty-oracle'/f'{sequence}.bitmap').write_bytes(data)
                row=dict(sequence=sequence,phase=phase,epoch=epoch,node=node,pages=64,
                    missing_pages=0,other_changed_pages=0,coverage_verified=True,inv_from=start,inv_to=epoch)
                if changed is not None:
                    raw=struct.pack('<Q',changed)
                    (root/'dirty-oracle'/f'{sequence}.changed').write_bytes(raw)
                    row.update(changed_bitmap_sha256=hashlib.sha256(raw).hexdigest(),bitmap_sha256=hashlib.sha256(data).hexdigest())
                rows.append(row)
            full=(1<<64)-1
            event(1,1,1,full,changed=0)
            event(4,2,1,3)
            event(5,2,2,full)
            event(2,3,1,4,changed=3)  # both changed pages were only in the live clean
            event(3,3,2,4,start=2)
            result=verify_cache_receipts({'dirty_oracle':rows},root)
            self.assertEqual((result['cleans'],result['preinvalidations']),(1,1))
            (root/'dirty-oracle/2.bitmap').write_bytes(struct.pack('<6Q',1,0,1,1,0,0))
            with self.assertRaisesRegex(ValueError,'accumulated published sets'):
                verify_cache_receipts({'dirty_oracle':rows},root)


if __name__ == '__main__':
    unittest.main()
