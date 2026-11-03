#!/usr/bin/env python3
"""Exercise the independent verifier against saved evidence and corrupt summaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from verify_backend_ablation import trial


class EvidenceTest(unittest.TestCase):
    source = None

    def setUp(self):
        if self.source is None:
            self.skipTest('Pass a recorded NFS trial directory to run evidence mutation tests')

    def check_edit(self, edit):
        source = self.source
        report = json.loads((source/'report.json').read_text())
        edit(report)
        with tempfile.TemporaryDirectory(prefix='backend-evidence-') as directory:
            root = Path(directory)
            for receipt in report['receipts']:
                (root/receipt['file']).symlink_to(source/receipt['file'])
            (root/'report.json').write_text(json.dumps(report))
            with self.assertRaises((ValueError, KeyError, StopIteration)):
                trial(root/'report.json')

    def test_original(self):
        self.assertEqual(trial(self.source/'report.json')['result'], 'passed')

    def test_faster_invented_timing(self):
        self.check_edit(lambda r: r['hops'][0]['timing'].__setitem__('request_to_resume_response_ns', 1))

    def test_replaced_kernel_vm(self):
        self.check_edit(lambda r: r['hops'][0]['export_token'].__setitem__('vm_creates', 2))

    def test_wrong_backend_arm(self):
        self.check_edit(lambda r: r.__setitem__('arm', 'none'))

    def test_false_preparation_omission(self):
        self.check_edit(lambda r: r.__setitem__('arm', 'nfs-unprepared'))

    def test_missing_source_probe(self):
        self.check_edit(lambda r: r['source_probes'].pop())

    def test_changed_payload_oracle(self):
        self.check_edit(lambda r: r['after']['receipt'].__setitem__('payload_hash', '0000000000000000'))

    def test_lost_cache(self):
        self.check_edit(lambda r: r['after']['receipt'].__setitem__('resident_after', 0))

    def test_failed_disk_read(self):
        self.check_edit(lambda r: r['backend_reads'][0]['receipt'].__setitem__('read_ios', 0))

    def test_missing_owner_rejection(self):
        self.check_edit(lambda r: r['negative_apis'].pop())

    def test_fabricated_native_start(self):
        self.check_edit(lambda r: r['preparation']['worker']['native_boot'].__setitem__('native_ready_ns', 1))

    def test_modified_raw_receipt_hash(self):
        self.check_edit(lambda r: r['receipts'][0].__setitem__('sha256', '0'*64))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: test_backend_ablation_evidence.py PASSED_NFS_TRIAL_DIRECTORY')
    EvidenceTest.source = Path(sys.argv.pop()).resolve(strict=True)
    unittest.main()
