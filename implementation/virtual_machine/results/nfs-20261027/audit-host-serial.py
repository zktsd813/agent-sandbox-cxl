#!/usr/bin/env python3
"""Recover legacy NFS host observations from completed L1 serial transactions.

This independently audits network_delta and B host cache values against raw
serial stdout. It does not re-run the benchmark, import its parser, or replace
verify_nfs_results.py's guest/state/summary checks. A host cache observations
are reported as unassigned: the separate A log lacks sample identifiers or a
shared timestamp needed to establish individual cross-VM associations.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re

FIELDS = ('rx_bytes', 'tx_bytes', 'nfs_read_calls', 'mount_read_ops',
          'mount_read_sent_bytes', 'mount_read_received_bytes')
DONE = re.compile(r'LAB_COMMAND_DONE_([0-9a-f]{16}):([0-9]+)')
MEASURED = re.compile(r's[0-9]+_[A-Za-z0-9_-]+')


def require(value, message):
    if not value:
        raise AssertionError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def transactions(path):
    raw = path.read_bytes()
    lines = raw.decode().replace('\r', '').splitlines()
    output, start, seen = [], 0, set()
    for position, line in enumerate(lines):
        match = DONE.fullmatch(line)
        if not match:
            continue
        token, rc = match.group(1), int(match.group(2))
        require(token not in seen, f'{path}: duplicate completed command marker {token}')
        seen.add(token)
        body = lines[start:position + 1]
        events = [json.loads(text) for text in body if text.startswith('{"event":')]
        counter_lines = [text for text in body if text.startswith('COUNTERS=')]
        mount_lines = [text for text in body if text.startswith('MOUNT_READ=')]
        measured = [event for event in events if event['event'] == 'read' and MEASURED.fullmatch(event['label'])]
        host_cache = [event for event in events if event['event'] == 'resident' and event['label'] == 'host']
        require(sum(bool(value) for value in (counter_lines or mount_lines, measured, host_cache)) <= 1,
                f'{path}:{start + 1}: ambiguous transaction types')
        item = {'id': token, 'rc': rc, 'start_line': start + 1, 'end_line': position + 1,
                'kind': 'other', 'body': body}
        if counter_lines or mount_lines:
            require(len(counter_lines) == len(mount_lines) == 1,
                    f'{path}:{start + 1}: incomplete/duplicate counter stdout')
            counters = []
            for prefix, text in (('COUNTERS=', counter_lines[0]), ('MOUNT_READ=', mount_lines[0])):
                require(re.fullmatch(re.escape(prefix) + r'[0-9]+ [0-9]+ [0-9]+', text),
                        f'{path}:{start + 1}: malformed counter stdout')
                counters.extend(map(int, text[len(prefix):].split()))
            item.update(kind='counters', counters=dict(zip(FIELDS, counters)))
        elif measured:
            require(len(measured) == 1, f'{path}:{start + 1}: duplicate measured read stdout')
            item.update(kind='sample', event=measured[0])
        elif host_cache:
            require(len(host_cache) == 1, f'{path}:{start + 1}: duplicate host mincore stdout')
            item.update(kind='host_cache', event=host_cache[0])
        output.append(item)
        start = position + 1
    # A truncated observation must not be mistaken for a completed transaction.
    require(not any(line.startswith(('COUNTERS=', 'MOUNT_READ=', '{"event":')) for line in lines[start:]),
            f'{path}: uncompleted observation at end of serial log')
    return output, digest(raw)


def receipt(transaction):
    return {key: transaction[key] for key in ('id', 'start_line', 'end_line', 'rc')}


def audit(report_path, evidence_dir):
    report_raw = report_path.read_bytes()
    report = json.loads(report_raw)
    require(report['result'] == 'passed', 'report did not pass')
    require(not report.get('cleanup_errors'), 'report has cleanup errors')
    samples = report['samples']
    require(bool(samples), 'no samples')
    labels = [sample['label'] for sample in samples]
    require(len(set(labels)) == len(labels), 'duplicate report sample labels')
    b_path = evidence_dir / 'L1-b-serial.log'
    a_path = evidence_dir / 'L1-a-serial.log'
    b_commands, b_sha = transactions(b_path)
    a_commands, a_sha = transactions(a_path)
    raw_labels = [tx['event']['label'] for tx in b_commands if tx['kind'] == 'sample']
    require(raw_labels == labels, 'report sample labels/order differ from raw B serial observations')
    expected_image_bytes = (report['config']['prepared']['nfs']['images']['data_mib'] + 64) * 1024 * 1024
    by_label = dict(zip(labels, samples))
    used_counters, used_cache, associations, differences = set(), set(), [], []
    for position, measured in enumerate(b_commands):
        if measured['kind'] != 'sample':
            continue
        label = measured['event']['label']
        sample = by_label[label]
        require(measured['rc'] == 0, f'{label}: measured read serial command failed')
        for key, value in measured['event'].items():
            require(sample[key] == value, f'{label}: raw measured field mismatch: {key}')
        require(0 < position < len(b_commands) - 1, f'{label}: missing counter transactions')
        before, after = b_commands[position - 1], b_commands[position + 1]
        require(before['kind'] == after['kind'] == 'counters',
                f'{label}: read is not immediately bracketed by complete counter commands')
        require(before['rc'] == after['rc'] == 0, f'{label}: counter serial command failed')
        require(before['id'] not in used_counters and after['id'] not in used_counters,
                f'{label}: counter transaction reused by multiple samples')
        used_counters.update((before['id'], after['id']))
        delta = {key: after['counters'][key] - before['counters'][key] for key in FIELDS}
        require(min(delta.values()) >= 0, f'{label}: counter moved backwards')
        require(delta == sample['network_delta'], f'{label}: raw serial/report network_delta mismatch')
        association = {'label': label, 'sample': receipt(measured), 'before': receipt(before),
                       'after': receipt(after), 'before_values': before['counters'],
                       'after_values': after['counters'], 'network_delta': delta}
        if delta['nfs_read_calls'] != delta['mount_read_ops']:
            differences.append({'label': label, 'started': delta['nfs_read_calls'],
                                'completed': delta['mount_read_ops']})
        expected_cache = sample.get('host_cache_state') or {}
        cache_tx = b_commands[position - 2] if position >= 2 else None
        has_cache = cache_tx is not None and cache_tx['kind'] == 'host_cache'
        require(has_cache == ('B_image_resident_pages' in expected_cache),
                f'{label}: report/raw cache observation presence differs')
        if has_cache:
            require(cache_tx['rc'] == 0 and cache_tx['id'] not in used_cache,
                    f'{label}: failed/reused host mincore transaction')
            used_cache.add(cache_tx['id'])
            value = cache_tx['event']
            require(value['offset'] == 0 and value['bytes'] == expected_image_bytes and
                    value['pages'] == expected_image_bytes // 4096, f'{label}: host mincore range mismatch')
            require(all(0 <= value[key] <= value['pages'] for key in ('resident_before', 'resident_after')),
                    f'{label}: invalid host mincore residency')
            require(value['resident_after'] == expected_cache['B_image_resident_pages'],
                    f'{label}: raw serial/report B host cache mismatch')
            require(f'> /lab/nfs-probe resident /disk/sandbox.raw host 0 {expected_image_bytes}' in cache_tx['body'],
                    f'{label}: host mincore pathname not independently identifiable')
            if 'image_pages' in expected_cache:
                require(expected_cache['image_pages'] == value['pages'], f'{label}: reported image pages mismatch')
            association['B_host_cache'] = {'transaction': receipt(cache_tx), 'event': value}
        associations.append(association)
    counter_total = sum(tx['kind'] == 'counters' for tx in b_commands)
    cache_total = sum(tx['kind'] == 'host_cache' for tx in b_commands)
    require(len(used_counters) == counter_total == 2 * len(samples), 'unassigned/reused B counter observations')
    require(len(used_cache) == cache_total, 'unassigned/reused B host cache observations')
    a_cache = [tx for tx in a_commands if tx['kind'] == 'host_cache']
    return {'schema_version': 1, 'result': 'passed', 'report': str(report_path),
            'report_sha256': digest(report_raw), 'evidence_dir': str(evidence_dir),
            'serial_sha256': {'A': a_sha, 'B': b_sha}, 'samples_checked': len(samples),
            'network_fields_per_sample': list(FIELDS), 'network_delta': 'raw_serial_validated',
            'counter_snapshots_checked': counter_total, 'B_host_cache_samples_checked': cache_total,
            'B_host_cache': 'raw_serial_validated',
            'A_host_cache': {'status': 'not_individually_assigned', 'raw_observations': len(a_cache),
                             'reason': 'Separate A log has no sample labels or shared timestamps; cross-VM sample attribution is not inferred.'},
            'association_rule': 'Successful B read command is immediately preceded/followed by distinct successful counter commands. When present, B mincore is immediately before the preceding counter command. Every observed counter/mincore is consumed exactly once.',
            'line_number_note': 'One-based serial lines after removing carriage returns; source raw byte hashes are retained.',
            'rpc_counter_differences': differences, 'associations': associations,
            'scope': 'Retrospective host observation audit only; use verify_nfs_results.py for guest/state/source/summary checks.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--json-out', type=Path)
    args = parser.parse_args()
    result = audit(args.report, args.evidence_dir)
    text = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.json_out:
        args.json_out.write_text(text)
    print(json.dumps({key: value for key, value in result.items() if key != 'associations'}, indent=2))


if __name__ == '__main__':
    main()
