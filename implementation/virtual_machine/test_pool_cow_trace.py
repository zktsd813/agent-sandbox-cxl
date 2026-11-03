"""Evidence failures must not turn into convincing fault-latency numbers."""
import unittest

from pool_cow_trace import FUNCTIONS, analyze_trace, profile_threads


class CowTraceTest(unittest.TestCase):
    def test_serial_crlf_threads(self):
        raw = "PROFILE_THREAD 268\r\nPROFILE_THREAD 269\r\n"
        self.assertEqual(profile_threads(raw), {268, 269})
        self.assertEqual(profile_threads(raw.replace("\r\n", "\n")), {268, 269})
        self.assertEqual(profile_threads("echo PROFILE_THREAD 99\n"), set())

    def setUp(self):
        self.group = "pcow12345678"
        self.trace = """ sh-2 [001] .... 1.000000: tracing_mark_write: pcow12345678 BEGIN
 fc_vcpu 0-23 [000] d... 1.000500: pcow12345678_fault_enter: (__handle_mm_fault+0x0/0x120)
 fc_vcpu 0-23 [000] d... 1.001000: pcow12345678_wp_enter: (do_wp_page+0x0/0x120)
 fc_vcpu 0-23 [000] d... 1.001100: pcow12345678_mmu_enter: (x)
 fc_vcpu 0-23 [000] d... 1.001200: pcow12345678_tlb_enter: (x)
 fc_vcpu 0-23 [000] d... 1.001250: pcow12345678_tlb_exit: (x)
 fc_vcpu 0-23 [000] d... 1.001300: pcow12345678_mmu_exit: (x)
 fc_vcpu 0-23 [000] d... 1.001500: pcow12345678_wp_exit: (x)
 fc_vcpu 0-23 [000] d... 1.001600: pcow12345678_fault_exit: (x)
 sh-2 [001] .... 1.002000: tracing_mark_write: pcow12345678 END
"""
        self.profile = "\n".join(f"{self.group}_{fn}_{edge} 1 0"
                                 for fn in FUNCTIONS for edge in ("enter", "exit"))
        self.stats = "overrun: 0\ncommit overrun: 0\ndropped events: 0\n"

    def audit(self, trace=None, stats=None, profile=None, tids=None):
        return analyze_trace(self.trace if trace is None else trace,
                             self.stats if stats is None else stats,
                             self.profile if profile is None else profile,
                             self.group, {23} if tids is None else tids)

    def test_nested_inclusive_times(self):
        out = self.audit()
        self.assertEqual(out["functions"]["fault"]["sum_ns"], 1_100_000)
        self.assertEqual(out["functions"]["wp"]["sum_ns"], 500_000)
        self.assertEqual(out["functions"]["mmu"]["sum_ns"], 200_000)
        self.assertEqual(out["functions"]["tlb"]["sum_ns"], 50_000)
        self.assertEqual(out["printed_timestamp_resolution_ns"], 1000)

    def test_missing_return(self):
        with self.assertRaises(ValueError):
            self.audit(trace=self.trace.replace("wp_exit:", "unrecognized_exit:"))

    def test_missing_end(self):
        with self.assertRaises(ValueError):
            self.audit(trace=self.trace.replace("pcow12345678 END", "OTHER END"))

    def test_no_host_fault_observed(self):
        with self.assertRaises(ValueError):
            self.audit(trace="\n".join(line for line in self.trace.splitlines() if "_fault_" not in line))

    def test_absent_pte_path_is_not_called_write_protect(self):
        out = self.audit(trace="\n".join(line for line in self.trace.splitlines() if "_wp_" not in line))
        self.assertEqual(out["functions"]["wp"]["calls"], 0)
        self.assertEqual(out["functions"]["fault"]["calls"], 1)

    def test_ring_loss(self):
        with self.assertRaises(ValueError):
            self.audit(stats=self.stats.replace("overrun: 0", "overrun: 1"))

    def test_missed_return_probe(self):
        with self.assertRaises(ValueError):
            self.audit(profile=self.profile.replace(" 1 0", " 1 1"))

    def test_another_thread(self):
        with self.assertRaises(ValueError):
            self.audit(tids={99})

    def test_return_before_entry(self):
        with self.assertRaises(ValueError):
            self.audit(trace=self.trace.replace("wp_enter:", "wp_exit:"))

    def test_impossible_hit_count(self):
        with self.assertRaises(ValueError):
            self.audit(profile=self.profile.replace(" 1 0", " 0 0"))

    def test_wrong_probe_identity(self):
        with self.assertRaises(ValueError):
            self.audit(profile=self.profile.replace("wp_enter", "wrong_enter"))


if __name__ == "__main__":
    unittest.main()
