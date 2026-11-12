#!/usr/bin/env python3
"""Corrupt completed resident evidence, rehash it, and require semantic rejection.

Original evidence is read-only. Each case uses a private symlink view and
replaces only the files it changes, including their recorded SHA-256 values.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from verify_swe_handoff import verify


class Mutation:
    def __init__(self, source, root):
        self.root = root
        self.report = json.loads((source / 'report.json').read_bytes())
        for item in source.iterdir():
            (root / item.name).symlink_to(item, target_is_directory=item.is_dir())

    def write(self, name, text):
        target = self.root / name
        target.unlink()
        target.write_text(text)
        for item in self.report['receipts']:
            if item['file'] == name:
                item['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()

    def text(self, name):
        return (self.root / name).read_bytes().decode()

    def record(self, name, prefix, change):
        lines = self.text(name).splitlines(keepends=True)
        found = []
        for index, line in enumerate(lines):
            if prefix in line and not line.lstrip().startswith('>'):
                head, body = line.split(prefix, 1)
                row = json.loads(body)
                change(row)
                lines[index] = head + prefix + json.dumps(row) + '\n'
                found.append(row)
        assert len(found) == 1
        self.write(name, ''.join(lines))
        return found[0]

    def hops(self):
        return [t for t in self.report['transitions'] if t.get('resident')]

    def save(self):
        p = self.root / 'report.json'
        p.unlink()
        p.write_text(json.dumps(self.report))
        return p


def preparation_paused(m):
    m.hops()[0]['preparation']['source_paused'] = True


def omitted_request(m):
    t = m.hops()[0]
    t['fast'] = m.record(t['fast_file'], 'RESIDENT_FAST_JSON ', lambda r: r.update(request_ns=1))


def cached_directory_changed(m):
    m.hops()[0]['preparation']['directory'] += '-other'


def created_server_in_request(m):
    m.hops()[0]['preparation']['control_server_prepare_ns'] = 1


def omitted_native_start(m):
    p = m.report['resident_preparations'][0]
    p['native_boot'] = m.record(p['native_boot_file'], 'RESIDENT_BOOT_JSON ', lambda r: r.update(native_ready_ns=1))


def wrong_source_epoch(m):
    m.hops()[0]['source_epoch'] += 1


def missing_lifecycle(m):
    m.report['transitions'].pop()


def missing_command(m):
    m.report['commands'].pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.report.resolve().parent
    baseline = verify(args.report)
    results = []
    for mutate in (preparation_paused, omitted_request, cached_directory_changed, created_server_in_request,
                   omitted_native_start, wrong_source_epoch, missing_lifecycle, missing_command):
        with tempfile.TemporaryDirectory(prefix='resident-evidence-') as directory:
            m = Mutation(source, Path(directory))
            mutate(m)
            try:
                verify(m.save())
            except ValueError as error:
                results.append({'case': mutate.__name__, 'result': 'rejected', 'reason': str(error)})
            else:
                raise AssertionError(f'{mutate.__name__}: corruption was accepted')
    result = {'result': 'passed', 'baseline': baseline['result'], 'cases': results}
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
