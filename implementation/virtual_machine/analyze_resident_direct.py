#!/usr/bin/env python3
"""Analyze resident request-to-resume and optional exclusive VMM profiles.

Keep first visits and preparation separate. Profile stages are nested inside
API intervals; means are additive, independently computed medians are not.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from verify_swe_handoff import verify, Evidence, need


def stats(values):
    values = sorted(values)
    return {'n': len(values), 'mean': statistics.mean(values), 'p50': statistics.median(values),
            'min': values[0], 'max': values[-1]}


def analyze(path):
    verify(path)
    r = json.loads(path.read_bytes())
    need(r['mode'] == 'direct', 'Passed resident direct report required')
    e = Evidence(r, path.parent)
    profiles, events, preparations = {}, {}, []
    for receipt in r['receipts']:
        if receipt['kind'] != 'final-vmm-log':
            continue
        last = None
        for line in e.text(receipt['file']).splitlines():
            if 'DIRECT_HANDOFF_JSON ' in line:
                last = json.loads(line.split('DIRECT_HANDOFF_JSON ', 1)[1])
                key = (last['event'], last['token']['generation'])
                need(key not in events, 'Duplicate handoff event')
                events[key] = last
            if 'DIRECT_RESIDENT_JSON ' in line:
                item = json.loads(line.split('DIRECT_RESIDENT_JSON ', 1)[1])
                if item['event'] == 'prepare':
                    preparations.append(item)
            if 'RESTORE_TIMING ' in line:
                profile = json.loads(line.split('RESTORE_TIMING ', 1)[1])
                if profile['scope'] not in ('direct_export', 'direct_import', 'direct_resident_import'):
                    continue
                need(last is not None, 'Timing lacks generation')
                key = (last['event'], last['token']['generation'])
                need(key not in profiles, 'Duplicate operation profile')
                profiles[key] = profile
    rows = []
    for hop in r['transitions']:
        if hop['kind'] != 'direct':
            continue
        fast = hop['fast']
        generation = hop['destination_epoch']
        api_us = sum(a['duration_us'] for a in fast['apis'].values())
        row = {'source': hop['source'], 'destination': hop['destination'], 'generation': generation,
               'resident_import': generation != 1, 'control_path': 'native-binary',
               'export_api_us': fast['apis']['send']['duration_us'],
               'import_api_us': fast['apis']['receive']['duration_us'], 'export_import_api_us': api_us,
               **{key[:-3]+'_us': value/1000 for key, value in fast.items() if key.endswith('_ns')}}
        if profiles:
            exp, imp = events[('export', generation)], events[('import', generation)]
            row['kernel_us'] = (exp['token']['kernel_ns'] + imp['token']['kernel_ns'])/1000
            row['allocation_generation'] = imp['mapper']['allocation_generation']
            parts = {}
            for event in ('export', 'import'):
                profile = profiles[(event, generation)]
                need(profile['total_us'] >= sum(profile['stages_us'].values())-.1, 'Stage overlap')
                parts.update({event+'.'+key: value for key, value in profile['stages_us'].items()})
            parts['export.export_detach_ioctls'] -= exp['token']['kernel_ns']/1000
            attach_key = 'import.attach' if imp.get('resident', False) else 'import.attach_and_vm_wrapper'
            parts[attach_key] -= imp['token']['kernel_ns']/1000
            parts['kernel.detach_attach'] = row['kernel_us']
            parts['api_dispatch_response_profile_residual'] = api_us-sum(parts.values())
            need(all(value >= -.05 for value in parts.values()), 'Overlapping profile')
            row['exclusive_components_us'] = parts
        rows.append(row)
    def summary(group):
        keys = {key for row in group for key in row if key.endswith('_us')}
        return {key: stats([row[key] for row in group if key in row]) for key in sorted(keys)}
    return {'schema': 'resident-direct-profile-v2', 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'rows': rows, 'preparation_vmm': preparations, 'preparation_controller': r.get('resident_preparations', []),
            'all': summary(rows), 'first_import': summary([row for row in rows if not row['resident_import']]),
            'resident_imports': summary([row for row in rows if row['resident_import']]),
            'notes': ['All first visits and warm samples retained.',
                      'Send/receive API timings include pause/resume in the native endpoint.',
                      'Preparation occurs while the source runs; do not add nested preparation intervals.',
                      'Resume response is API acceptance, not first guest execution.',
                      'Coherent nested lab; physical multi-host CXL remains unmeasured.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = analyze(args.report)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result['all'], indent=2))


if __name__ == '__main__':
    main()
