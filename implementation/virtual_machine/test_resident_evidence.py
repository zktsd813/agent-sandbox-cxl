#!/usr/bin/env python3
"""Corrupt completed resident evidence, rehash it, and require semantic rejection.

Original evidence is read-only. Each case uses a private symlink view and
replaces only the files it changes, including their recorded SHA-256 values.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile

from verify_swe_pool import verify


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


def accessible_ram(m):
    t = m.hops()[0]
    name = t['source_fence_file']
    text, count = re.subn(r'(?m)^([0-9a-f]+-[0-9a-f]+) ---p ', r'\1 rw-p ', m.text(name))
    assert count == 1
    m.write(name, text)


def unrevoked_kvm(m):
    p = m.report['resident_probes'][0]
    p['receipt'] = m.record(p['file'], 'DIRECT_RESIDENT_PROBE_JSON ', lambda r: r.update(kvm_errno=0))


def missing_probe(m):
    m.report['resident_probes'].pop()


def resource_changed(m, field, prefix, match):
    row = [r for r in m.report['resident_resources'] if r['role'] == 'A' and r['phase'] == 'active'][-1]
    key = next(k for k, v in row[field].items() if match(v))
    value = row[field].pop(key)
    replacement = str(max(map(int, row[field])) + 100)
    row[field][replacement] = value
    raw, count = re.subn(rf'(?m)^{prefix} {key} ', f'{prefix} {replacement} ', m.text(row['file']))
    assert count == 1
    m.write(row['file'], raw)


def disk_reopened(m):
    resource_changed(m, 'fds', 'RESOURCE_FD', lambda v: v.endswith('/sandbox.raw'))


def thread_recreated(m):
    resource_changed(m, 'tasks', 'RESOURCE_TASK', lambda v: v.startswith('fc_vcpu'))


def omitted_elapsed(m):
    t = next(t for t in m.hops() if t.get('fast_control'))
    t['fast'] = m.record(t['fast_file'], 'RESIDENT_FAST_JSON ', lambda r: r.update(handoff_ns=1))
    t['controller_pause_to_ack_ns'] = 1


def wrong_negative(m):
    n = next(n for n in m.report['resident_negatives'] if n['kind'] == 'foreign-graph')
    valid = json.loads(m.text(n['valid_state_file']))
    m.write(n['candidate_file'], json.dumps(valid))


CASES = [
    (preparation_paused, 'preparation was on paused path'),
    (accessible_ram, 'parked mapping still accessible'),
    (unrevoked_kvm, 'parked memory or KVM access allowed'),
    (missing_probe, 'parked probe coverage'),
    (disk_reopened, 'disk FD/io_uring recreated'),
    (thread_recreated, 'vCPU thread recreated'),
    (omitted_elapsed, 'TCP interval excludes measured work'),
    (wrong_negative, 'invalid import did not test intended mismatch'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.report.resolve().parent
    baseline = verify(args.report)
    results = []
    for mutate, expected in CASES:
        with tempfile.TemporaryDirectory(prefix='resident-evidence-') as directory:
            m = Mutation(source, Path(directory))
            mutate(m)
            try:
                verify(m.save())
            except ValueError as error:
                if str(error) != expected:
                    raise AssertionError(f'{mutate.__name__}: unexpected rejection: {error}') from error
                results.append({'case': mutate.__name__, 'result': 'rejected', 'reason': str(error)})
            else:
                raise AssertionError(f'{mutate.__name__}: corruption was accepted')
    result = {'result': 'passed', 'baseline': baseline['result'], 'cases': results}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
