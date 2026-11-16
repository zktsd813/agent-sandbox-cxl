"""Independent checks of the observed SMP continuity evidence."""
import copy
import unittest

from lab import validate_smp_handoff


class SmpEvidenceTests(unittest.TestCase):
    def evidence(self):
        return [dict(event="smp", cpu=cpu, round=hop, ack=hop + 1,
                     sequence=100 + hop, timer=20 + hop, ipi=10 + hop, ping=30 + hop,
                     errors=0, clock_errors=0, tsc_errors=0)
                for hop in range(4) for cpu in range(2)]

    def test_all_cpus_continue(self):
        result = validate_smp_handoff(self.evidence(), 4, 2)
        self.assertEqual(set(result), {"0", "1"})
        self.assertEqual(result["1"]["timer_increase"], 3)

    def test_missing_duplicate_or_wrong_ack(self):
        rows = self.evidence()
        variants = [rows[:-1], rows + [rows[0]]]
        wrong = copy.deepcopy(rows)
        wrong[-1]["ack"] = 3
        variants.append(wrong)
        for evidence in variants:
            with self.subTest(evidence=evidence), self.assertRaises(RuntimeError):
                validate_smp_handoff(evidence, 4, 2)

    def test_every_cpu_clock_and_checksum_error_rejects(self):
        for key in ("errors", "clock_errors", "tsc_errors"):
            rows = self.evidence()
            rows[-1][key] = 1
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                validate_smp_handoff(rows, 4, 2)

    def test_stalled_timer_ipi_or_worker_rejects(self):
        for key in ("sequence", "timer", "ipi", "ping"):
            rows = self.evidence()
            for row in rows:
                if row["cpu"] == 1:
                    row[key] = 10
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                validate_smp_handoff(rows, 4, 2)

    def test_regression_between_endpoints_rejects(self):
        rows = self.evidence()
        rows[3]["sequence"] = 2
        with self.assertRaises(RuntimeError):
            validate_smp_handoff(rows, 4, 2)


if __name__ == "__main__":
    unittest.main()
