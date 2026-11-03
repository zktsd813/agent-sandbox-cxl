#!/usr/bin/env python3
"""Reject missing/reordered liveness evidence from a completed in-flight SWE trial."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from verify_swe_runtime import verify


class InflightEvidenceTest(unittest.TestCase):
    source = None

    @staticmethod
    def command(report):
        return next(row for row in report['commands'] if 'inflight' in row)

    def check_edit(self, edit):
        report = json.loads((self.source/'report.json').read_text())
        edit(report)
        with tempfile.TemporaryDirectory(prefix='swe-inflight-evidence-') as temporary:
            root = Path(temporary)
            for path in self.source.iterdir():
                if path.name != 'report.json':
                    (root/path.name).symlink_to(path, target_is_directory=path.is_dir())
            (root/'report.json').write_text(json.dumps(report))
            with self.assertRaises((ValueError, KeyError, StopIteration)):
                verify(root/'report.json')

    def test_original(self):
        self.assertEqual(verify(self.source/'report.json')['inflight_commands'], 1)

    def test_missing_liveness_proof(self):
        self.check_edit(lambda r: self.command(r).pop('inflight'))

    def test_wrong_command(self):
        self.check_edit(lambda r: r['arguments'].__setitem__('inflight_handoff_index', 0))

    def test_missing_destination_observation(self):
        self.check_edit(lambda r: self.command(r)['inflight']['observations'].pop(1))

    def test_exited_command(self):
        self.check_edit(lambda r: self.command(r)['inflight']['observations'][1]['receipt'].__setitem__('active', False))

    def test_replaced_process(self):
        self.check_edit(lambda r: self.command(r)['inflight']['observations'][1]['receipt'].__setitem__('start_ticks', 1))

    def test_reversed_handoff_order(self):
        self.check_edit(lambda r: self.command(r)['inflight']['handoff_files'].reverse())

    def test_reversed_observations(self):
        self.check_edit(lambda r: self.command(r)['inflight']['observations'].reverse())


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: test_swe_inflight_evidence.py PASSED_TRIAL_DIRECTORY')
    InflightEvidenceTest.source = Path(sys.argv.pop()).resolve(strict=True)
    unittest.main()
