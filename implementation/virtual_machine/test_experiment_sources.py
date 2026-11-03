"""Source references survive later edits and reject uncommitted/mismatched code."""
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from experiment_sources import record_sources, verify_sources


class SourceReferenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git('init', '-q')
        self.source = self.root / 'runner.py'
        self.source.write_text('print("first revision")\n')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), '-c', 'user.name=Test',
            '-c', 'user.email=test@example.invalid', *args], stderr=subprocess.STDOUT)

    def commit(self):
        self.git('add', 'runner.py')
        self.git('commit', '-qm', 'Measured source')

    def report(self):
        hashes, reference = record_sources(self.root, ['runner.py'])
        return {'source_hashes': hashes, 'source_reference': reference}

    def test_original_commit_survives_later_worktree_and_commit(self):
        report = self.report()
        self.source.write_text('print("later revision")\n')
        verify_sources(report, self.root)
        self.commit()
        verify_sources(report, self.root)
        self.assertNotEqual(report['source_reference']['commit'], self.git('rev-parse', 'HEAD').decode().strip())
        self.assertFalse((self.root / 'measured-sources').exists())

    def test_uncommitted_source_is_not_mislabelled_as_head(self):
        self.source.write_text('print("uncommitted")\n')
        with self.assertRaisesRegex(ValueError, 'Commit measured source'):
            self.report()

    def test_changed_digest_or_missing_path_is_rejected(self):
        report = self.report()
        wrong_digest = copy.deepcopy(report)
        wrong_digest['source_hashes']['runner.py'] = '0' * 64
        with self.assertRaises(ValueError):
            verify_sources(wrong_digest, self.root)
        report['source_reference']['paths']['runner.py'] = 'missing.py'
        with self.assertRaises(ValueError):
            verify_sources(report, self.root)

    def test_old_saved_sources_still_verify(self):
        saved = self.root / 'measured-sources'
        saved.mkdir()
        (saved / 'runner.py').write_bytes(self.source.read_bytes())
        report = {'source_hashes': {'runner.py': hashlib.sha256(self.source.read_bytes()).hexdigest()}}
        verify_sources(report, self.root)
        (saved / 'runner.py').write_text('changed')
        with self.assertRaises(ValueError):
            verify_sources(report, self.root)


if __name__ == '__main__':
    unittest.main()
