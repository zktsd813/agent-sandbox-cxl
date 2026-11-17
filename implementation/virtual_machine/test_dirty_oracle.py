"""Independent negative checks of emitted ranges and the CXL log wire format."""
import struct
import unittest

from dirty_oracle import DirtyOracle, MIB


class MaintenanceReceipts(unittest.TestCase):
    def setUp(self):
        self.oracle = object.__new__(DirtyOracle)
        self.oracle.metadata = 0
        self.oracle.mem = bytearray(32*MIB+1024)
        self.oracle.records, self.oracle.consumed = {}, {}
        struct.pack_into('<2Q', self.oracle.mem, 48, 1, 2)
        self.full = (1 << 64)-1
        self.publish(1, self.full)

    def packet(self, bitmap, wb=0, requested=0, invalidated=0):
        return struct.pack('<6Q', bitmap, 0, bitmap, wb, requested, invalidated)

    def extra(self, epoch, start=0, end=0):
        return [epoch, start, end, 0, 0, 0, 0, 0, 0, 4096, 4096, 64, 128, 1, 1, 0]

    def publish(self, epoch, bitmap):
        base = 32*MIB
        struct.pack_into('<8Q', self.oracle.mem, base,
                         0x4e43484c4f473031, 1, 1, 2, 64, epoch+1, 8, 64)
        struct.pack_into('<3Q', self.oracle.mem, base+64+((epoch-1)%8)*64,
                         epoch, bitmap.bit_count(), bitmap)
        return self.oracle.check_maintenance(1 if epoch==1 else 2, 1, 64,
            0 if epoch==1 else 4096, self.extra(epoch), self.packet(bitmap, wb=bitmap))

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


if __name__ == '__main__':
    unittest.main()
