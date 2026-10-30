#!/usr/bin/env python3
"""Independent direct-token, stopped-import and anonymous-demotion receipt audit."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def audit(report):
    p = report.parent
    d = json.loads(report.read_bytes())
    def sha(b):
        return hashlib.sha256(b).hexdigest()
    for r in d['receipts']:
        assert sha((p / r['file']).read_bytes()) == r['sha256'], r['file']
    def rows(name, prefix):
        return [json.loads(s[len(prefix):]) for s in (p / name).read_text().splitlines()
                if s.startswith(prefix)]
    def api(a):
        r = rows(a['file'], 'LAB_API_JSON ')
        assert len(r) == 1 and r[0]['status'] == 204 and r[0] == a['response']
        return int(a['file'].split('-')[0])
    def guest(a):
        r = [json.loads(s) for s in (p / a['file']).read_text().splitlines()
             if s.startswith('{"event":"pool_guest"')]
        r = [x for x in r if x['label'] == a['receipt']['label']]
        assert len(r) == 1 and r[0] == a['receipt'] and r[0]['ok']
        return r[0]
    def same(a, b):
        for k in ['boot_id', 'pid', 'cookie', 'generation', 'private_hash', 'upper_hash', 'payload_bytes']:
            assert a[k] == b[k], k
        # The pre-transition command is deliberately identity-only; its zero
        # payload_hash is not an observation of file bytes.
        assert b['payload_hash'] == d['fixture']['pool']['image']['generator']['hash']
        assert b['resident_after'] == 40960 and b['checked_payload_words'] == 20971520
        assert b['block_read_ios'] == 0
    fixed = ['vm_offset', 'vcpu_offset', 'vmcs_offset', 'run_offset', 'ept_root_offset',
             'ram_hva', 'ram_bytes', 'vm_creates', 'vcpu_creates']
    summary, last = [], None
    for t in d['transitions']:
        if t['kind'] != 'direct':
            continue
        state = json.loads((p / t['device_state_file']).read_bytes())
        assert len((p / t['device_state_file']).read_bytes()) == t['device_state_bytes']
        assert set(state) == {'schema', 'token', 'machine_config', 'boot_source', 'memory',
                              'memory_manifest_sha256', 'devices'}
        assert state['schema'] == 'direct-device-handoff-v1' and state['token'] == t['token']
        assert sha((p / t['manifest_file']).read_bytes()) == state['memory_manifest_sha256']
        old = json.loads((p / t['manifest_file']).read_bytes())
        new = json.loads((p / t['destination_placement']['file']).read_bytes())
        assert old['mappings'] == new['mappings'] and set(old['credits']) == set(new['credits'])
        logs = [r['file'] for r in d['receipts'] if r['kind'] == 'direct-import-log'
                and int(t['attach_api']['file'].split('-')[0]) < int(r['file'].split('-')[0])
                < int(t['after']['file'].split('-')[0])]
        assert len(logs) == 1
        imports = [json.loads(s.split('DIRECT_HANDOFF_JSON ', 1)[1])
                   for s in (p / logs[0]).read_text().splitlines() if 'DIRECT_HANDOFF_JSON ' in s]
        imports = [r for r in imports if r['event'] == 'import']
        assert len(imports) == 1 and imports[0] == t['import_event']
        imp, src = imports[0]['token'], state['token']
        for k in fixed + ['generation']:
            assert src[k] == imp[k], k
        assert src['flags'] == 2 and imp['flags'] == 1
        assert src['vm_creates'] == src['vcpu_creates'] == 1
        assert imp['node'] == {'A': 1, 'B': 2}[t['destination']]
        assert all(t[k] == 0 for k in ['ram_snapshot_bytes', 'ram_transfer_bytes', 'cpu_snapshot_bytes'])
        if last:
            assert all(src[k] == last[k] for k in fixed) and src['generation'] > last['generation']
        last = src
        assert re.search(r'^FENCED \d+ \d+\s*$', (p / t['source_fence_file']).read_text(), re.M)
        ex, at = api(t['export_api']), api(t['attach_api'])
        assert ex < int(t['source_fence_file'].split('-')[0]) < at
        later = [a for a in d['api_events'] if a['role'] == t['destination']
                 and int(a['file'].split('-')[0]) > at]
        assert later[0]['request'] == {'action_type': 'PoolCache'}
        assert later[1]['request'] == {'state': 'Resumed'}
        assert at < api(later[0]) < api(later[1])
        same(guest(t['before']), guest(t['after']))
        summary.append({'source': t['source'], 'destination': t['destination'],
                        'generation': src['generation'], 'ept_pages': src['ept_page_count'],
                        'device_state_bytes': t['device_state_bytes'],
                        'placement_metadata_bytes': t['manifest_bytes'],
                        'guest_identity_preserved': True, 'placement_unchanged': True,
                        'paused_operation_before_resume': True, 'source_fenced': True})
    assert len(summary) >= 2 and last
    dem = []
    for t in d['transitions']:
        if t['kind'] != 'demotion':
            continue
        native = json.loads((p / t['operation']['file']).read_bytes())
        assert native == t['operation']['report'] and native['copied_private_bytes'] == 536870912
        assert native['shared_bytes'] == 0 and native['ram_snapshot_bytes'] == 0
        maps = (p / t['dram_maps_file']).read_text()
        assert re.search(r'^400000000000-400020000000 rw-p 00000000 00:00 0\s*$', maps, re.M)
        same(guest(t['before']), guest(t['after']))
        api(t['operation']['apis'][0])
        dem.append({'vm': t['vm'], 'epoch': t['epoch'], 'anonymous_rw_private_bytes': 536870912,
                    'guest_identity_and_payload_preserved': True})
    for t in d['remote_refills']:
        g = guest(t['command'])
        assert g['resident_before'] == 0 and g['resident_after'] == 40960
        assert g['block_read_ios'] > 0 and g['checked_payload_words'] == 20971520
        def nfs(n):
            r = [s.split() for s in (p / n).read_text().splitlines() if s.startswith('proc3 ')]
            assert len(r) == 1
            return int(r[0][8])
        assert nfs(t['after_file']) - nfs(t['before_file']) == t['nfs_read_delta'] > 0
    assert not any(a['path'].startswith('/snapshot/') for a in d['api_events'])
    return {'result': 'passed', 'scope': 'direct_token_and_demote_receipts',
            'report': str(report), 'report_sha256': sha(report.read_bytes()),
            'verified_receipts': len(d['receipts']), 'direct_handoffs': summary, 'demotions': dem,
            'remote_nfs_refills': len(d['remote_refills']),
            'fixed_kernel_identity': {k: last[k] for k in fixed},
            'limits': [
                'Runtime token equality proves retained object offsets and creation counters; EPT leaf contents/page count may change as the guest executes.',
                'Paused import is established by successful Paused-only PoolCache before Resume plus the frozen source API gate; device-event nonexecution is a source-control property, not separately traced.',
                'Anonymous rw-p mapping proves backing replacement; physical NUMA placement was not measured.',
                'No CPU/RAM snapshot API and no payload in direct metadata, with frozen source, support the no-snapshot path; this is not an independent syscall/data-copy trace.',
                'Coherent nested lab, trusted kernel/controller, one direct graph plus two ordinary peer VMs.'
            ]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = audit(args.report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
