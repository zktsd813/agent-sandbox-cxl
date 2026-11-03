"""Independent receipt/statistics audit; does not import the project verifier."""
import argparse
import collections
import datetime
import hashlib
import json
import pathlib
import re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('report', type=pathlib.Path)
parser.add_argument('output', type=pathlib.Path)
parser.add_argument('--current-cache-audit', type=pathlib.Path)
args = parser.parse_args()
RUN = args.report.resolve().parent
OUT = args.output.resolve()
LAB = pathlib.Path(__file__).resolve().parents[3]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def raw(name):
    return (RUN / name).read_bytes().decode().replace('\r', '')

def record(name, prefix):
    found = [json.loads(line[len(prefix):]) for line in raw(name).splitlines()
             if line.startswith(prefix)]
    assert len(found) == 1, (name, prefix, len(found))
    return found[0]

def shell_pass(name):
    matches = re.findall(r'^LAB_COMMAND_DONE_[a-f0-9]+:(\d+)$', raw(name), re.M)
    assert matches and matches[-1] == '0', name

data = json.loads((RUN / 'report.json').read_text())
assert data['result'] == 'passed'
current_cache = bool(data['fixture_manifest']['pool'].get('guest_cache_gpa_audit', False))
cycles = data['arguments']['cycles']
assert data['arguments']['sandboxes'] == 3 and data['arguments']['memory_mib'] == 512
assert data['arguments']['minimum_cache_mib'] == 128
receipts = data['receipts']
assert len({r['file'] for r in receipts}) == len(receipts)
kind_counts = collections.Counter()
kind_bytes = collections.Counter()
for r in receipts:
    p = RUN / r['file']
    assert digest(p) == r['sha256'], p
    kind_counts[r['kind']] += 1
    kind_bytes[r['kind']] += p.stat().st_size

sources = {}
for name, expected in data['runtime_source_hashes'].items():
    p = RUN / 'measured-sources' / name
    assert digest(p) == expected, name
    sources[name] = {'sha256': expected, 'bytes': p.stat().st_size}
for name, expected in data['fixture_manifest']['source_hashes'].items():
    assert sources[name]['sha256'] == expected
artifacts = {}
for name in ('firecracker', 'l1-kernel'):
    p = pathlib.Path(data['config']['assets']) / name
    expected = data['fixture_manifest']['artifacts'][name]
    assert digest(p) == expected
    artifacts[name] = {'sha256': expected, 'bytes': p.stat().st_size}

payload = data['payload_bytes']
payload_hash = data['fixture_manifest']['pool']['image']['generator']['hash']
commands = data['commands']
identities = {}
counts = collections.Counter()
ops = collections.Counter()
payload_checks = []
for c in commands:
    observed = [json.loads(line) for line in
        raw(c['file']).splitlines() if line.startswith('{"event":"pool_guest"')]
    assert observed == [c['receipt']], c['file']
    r = observed[0]
    shell_pass(c['file'])
    assert r['ok'] and r['vm'] == c['vm'] and r['op'] == c['operation']
    counts[c['vm']] += 1
    assert r['command'] == counts[c['vm']]
    identity = tuple(r[k] for k in ('boot_id', 'pid', 'cookie'))
    assert identities.setdefault(c['vm'], identity) == identity
    assert r['payload_bytes'] == payload
    ops[r['op']] += 1
    if r['checked_payload_words']:
        assert r['checked_payload_words'] * 8 == payload and r['payload_hash'] == payload_hash
        payload_checks.append(r)
assert len(set(identities.values())) == 3
assert len(commands) == 10 + cycles*89 + (cycles*34 if current_cache else 0)

routes = collections.Counter()
vm_routes = collections.defaultdict(list)
imports = []
poisons = []
for t in data['transitions']:
    assert t['result'] == 'passed' and t['logical_snapshot_bytes'] == 512 << 20
    src = t['source'] + '/' + t['source_tier']
    dst = t['destination'] + '/' + t['destination_tier']
    routes[src + ' -> ' + dst] += 1
    vm_routes[t['vm']].append(dst)
    before, after = t['before']['receipt'], t['after']['receipt']
    for k in ('boot_id', 'pid', 'cookie', 'generation', 'private_hash', 'upper_hash'):
        assert before[k] == after[k], (t['vm'], t['epoch'], k)
    assert after['resident_after'] * 4096 >= 128 << 20
    assert t['check']['receipt']['block_read_ios'] == 0
    transport = raw(t['snapshot_transport_file'])
    shell_pass(t['snapshot_transport_file'])
    assert '\nPOOL_SNAPSHOT_TRANSPORT_FULL_COMPARE_PASSED\n' in transport
    assert len(re.findall(r'^> cmp ', transport, re.M)) == 3
    assert re.search(r'^FENCED \d+ \d+$', raw(t['fence']), re.M)
    shell_pass(t['fence'])
    for key, output in [('import', imports), ('poison', poisons)]:
        if t[key]:
            item = t[key]
            r = record(item['file'], 'POOL_DAX_COPY_JSON ')
            assert r == item['receipt'] and r['result'] == 'passed' and r['full_compare']
            assert r['bytes'] == 512 << 20
            shell_pass(item['file'])
            output.append(r)
assert vm_routes[1] == ['A/cxl', 'B/cxl', 'A/cxl', 'A/dram'] * cycles
assert vm_routes[2] == ['A/cxl', 'A/dram'] * cycles
assert vm_routes[3] == vm_routes[1]
assert len(imports) == 7*cycles and len(poisons) == 3*cycles

pool_events = [event['report'] for event in data['pool_events']]
positive_admissions = []
for event in pool_events:
    actual = json.loads((RUN / event['vmm_file']).read_text())
    assert actual == event['vmm']
    assert actual['vm_id'] == str(event['vm']) and actual['run_epoch'] == str(event['epoch'])
    assert len(actual['mapped_pages']) == actual['mapped']
    assert actual['mapped_bytes'] == actual['mapped'] * 4096
    by_gpa = {p['gpa']: p for p in event['plan_pages']}
    for page in actual['mapped_pages']:
        assert page['gpa'] in by_gpa and page['pool_offset'] == by_gpa[page['gpa']]['pool_offset']
        assert by_gpa[page['gpa']]['image_id'] == data['image_id']
        assert page['pool_offset'] == data['pool_offset'] + by_gpa[page['gpa']]['image_offset']
    if actual['mapped']:
        positive_admissions.append(event)

audits = {}
def collect(node):
    if isinstance(node, dict):
        if 'shared_offsets_file' in node and 'all_rows_checked' in node:
            if node['file'] in audits:
                assert audits[node['file']] == node
            audits[node['file']] = node
        else:
            for v in node.values():
                collect(v)
    elif isinstance(node, list):
        for v in node:
            collect(v)
collect(data)
audit_summaries = []
offset_sets = {}
for name, audit in sorted(audits.items()):
    actual = record(name, 'POOL_PFN_JSON ')
    assert all(audit[k] == v for k, v in actual.items())
    assert actual['all_rows_checked'] and sum(actual[k] for k in
        ('shared_pages', 'private_pages', 'absent_pages')) == actual['rows']
    assert actual['shared_bytes'] == actual['shared_pages'] * 4096
    offsets = [int(s) for s in (RUN / audit['shared_offsets_file']).read_text().splitlines()]
    assert len(offsets) == actual['shared_pages']
    assert sorted(set(offsets)) == audit['shared_offsets']
    assert len(set(offsets)) == audit['unique_shared_offsets']
    assert all(data['pool_offset'] <= x < data['pool_offset'] + 234881024 and x % 4096 == 0
               for x in offsets)
    owner = re.search(r'/vm(\d+)-epoch(\d+)/fc.pid', raw(name).replace('\n', ''))
    assert owner
    vm, epoch = map(int, owner.groups())
    candidates = [a for a in positive_admissions if a['vm'] == vm and a['epoch'] == epoch
                  and a['vmm_file'] < name]
    if audit.get('admission_file'):
        admission = next(a for a in positive_admissions if a['vmm_file'] == audit['admission_file'])
    else:
        admission = max(candidates, key=lambda a: a['vmm_file'])
    pages = admission['vmm']['mapped_pages']
    if audit.get('pfn_input_file'):
        tsv = (RUN / audit['pfn_input_file']).read_bytes()
        assert hashlib.sha256(tsv).hexdigest() == audit['pfn_input_sha256']
        inputs = [tuple(map(int, line.split())) for line in tsv.splitlines()]
        assert len(inputs) == actual['rows']
        assert len(set(inputs)) == len(inputs)
        assert set(inputs) <= {(p['hva'], p['pool_offset']) for p in pages}
    else:
        assert actual['rows'] == len(pages)
        tsv = ''.join(f"{p['hva']} {p['pool_offset']}\n" for p in pages).encode()
        row_receipt = max(r['file'] for r in receipts if r['kind'] == 'pfn-rows' and r['file'] < name)
        assert hashlib.sha256(tsv).hexdigest() in raw(row_receipt).replace('\n', ''), (name, row_receipt, admission['vmm_file'])
    assert set(offsets) <= {p['pool_offset'] for p in pages}
    shell_pass(name)
    offset_sets[name] = set(offsets)
    audit_summaries.append(dict(file=name, vm=vm, epoch=epoch, admission_file=admission['vmm_file'],
                                unique_shared_offsets=len(set(offsets)), **actual))
assert len(audits) == kind_counts['pfn-audit'] == 18*cycles
groups = []
for group in data['concurrent_sharing']:
    sets = [offset_sets[a['file']] for a in group['audits']]
    intersection = set.intersection(*sets)
    assert len(sets) == 3 and len(intersection) == group['shared_pages_all_vms']
    assert len(intersection) * 4096 == group['shared_bytes_all_vms'] == payload
    groups.append(dict(cycle=group['cycle'], stage=group['stage'],
                       common_canonical_offsets=len(intersection), bytes=len(intersection)*4096,
                       simultaneous_guest_cache_mapping_proof=current_cache))

reclaims = []
for cycle, reclaim in enumerate(data['remote_reclaims']):
    cold, hot = reclaim['cold'], reclaim['hot']
    assert reclaim['evicted']['receipt']['resident_after'] == 0
    assert cold['receipt']['resident_before'] == 0 and cold['receipt']['resident_after']*4096 == payload
    assert cold['receipt']['block_read_sectors'] * 512 == payload and hot['receipt']['block_read_ios'] == 0
    counters = []
    for key in ('nfs_before_file', 'nfs_after_file'):
        lines = [line.split() for line in raw(cold[key]).splitlines() if line.startswith('proc3 ')]
        assert len(lines) == 1 and int(lines[0][1]) == 22
        counters.append(int(lines[0][8]))
    delta = counters[1] - counters[0]
    assert delta == cold['nfs_read_delta'] and delta > 0
    old = reclaim['reclaimed_pfn_audit']
    stale = reclaim['stale_admission']['vmm']
    assert old['private_pages'] >= payload // 4096 and old['shared_pages'] == 0
    assert stale['mapped'] == 0 and stale['rejected_content'] >= payload // 4096
    reclaims.append(dict(cycle=cycle, host_private_pages_after_guest_reuse=old['private_pages'],
                         stale_content_rejections=stale['rejected_content'], nfs_read_before=counters[0],
                         nfs_read_after=counters[1], nfs_read_rpc_delta=delta,
                         guest_cold_read_bytes=cold['receipt']['block_read_sectors']*512,
                         guest_hot_read_ios=hot['receipt']['block_read_ios']))

canonical = []
for item in data['canonical_checks'] + [x['canonical_unchanged'] for x in data['runtime_safety_negatives']
                                       if 'canonical_unchanged' in x]:
    r = record(item['file'], 'POOL_DAX_COPY_JSON ')
    assert r == item['receipt'] and r['full_compare'] and r['result'] == 'passed'
    assert r['bytes'] == 234881024 and r['offset'] == data['pool_offset']
    assert data['image_id'] == item['image']['sha256'] and data['image_id'] in raw(item['image']['file'])
    canonical.append(dict(file=item['file'], **r))
for negative in data['runtime_safety_negatives']:
    assert negative['status'] == 400 and 'HTTP/1.1 400' in raw(negative['file'])
sentinel = next(r['file'] for r in receipts if r['kind'] == 'state-sentinel-after')
assert (RUN / sentinel).read_bytes() == b'pool state-only rejection must preserve existing state bytes\n'
for r in receipts:
    if r['kind'] == 'dram-no-dax':
        shell_pass(r['file'])
        assert not re.search(r'^[0-9a-f]+-[0-9a-f]+ .* /dev/dax', raw(r['file']), re.M)
assert all(f['tier'] == 'dram' and f['role'] == 'A' and f['receipt']['ok'] for f in data['final'])

result = {
    'schema': 1, 'result': 'passed', 'audited_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'method': 'Independent Python derivation from archived raw receipts and measured source files; does not import or execute verify_pool_results.py.',
    'report': str(RUN/'report.json'), 'report_sha256': digest(RUN/'report.json'),
    'current_guest_file_cache_gpa_proof': current_cache,
    'proof_scope': 'Guest mincore residency and host canonical PFNs of historical virtio-blk candidate GPAs were measured separately. This Sync run does not prove the two sets coincide at measurement time. The six three-VM offset intersections use sequential scans, not one common frozen interval.',
    'arguments': data['arguments'],
    'environment': {k:data['config'][k] for k in ('l1_vcpus','l1_memory_mib','cxl_mib','shared_mib','qemu_version','host_kernel')},
    'raw_evidence': {'receipt_count':len(receipts), 'receipt_bytes':sum(kind_bytes.values()),
                     'all_sha256_verified':True, 'counts_by_kind':dict(sorted(kind_counts.items())),
                     'bytes_by_kind':dict(sorted(kind_bytes.items())),
                     'report_bytes':(RUN/'report.json').stat().st_size},
    'measured_sources': sources, 'measured_source_bytes':sum(s['bytes'] for s in sources.values()),
    'binary_artifacts_verified': artifacts,
    'guest_workload': {'command_count':len(commands), 'commands_by_vm':dict(counts),
                       'operations':dict(ops), 'stable_identities_by_vm':identities,
                       'payload_bytes_per_vm':payload, 'full_payload_check_count':len(payload_checks),
                       'full_payload_bytes_verified':len(payload_checks)*payload,
                       'minimum_resident_bytes_after_full_payload_check':min(r['resident_after'] for r in payload_checks)*4096,
                       'final_all_three_on_home_dram':True},
    'migration': {'transition_count':len(data['transitions']), 'routes':dict(routes),
                  'full_logical_snapshot_bytes':sum(t['logical_snapshot_bytes'] for t in data['transitions']),
                  'transport_state_ram_hints_cmp_count':len(data['transitions'])*3,
                  'source_fences':len(data['transitions']), 'cxl_imports_full_compare':len(imports),
                  'cxl_import_bytes':sum(i['bytes'] for i in imports),
                  'demotion_poison_cases':len(poisons), 'demotion_poison_bytes':sum(i['bytes'] for i in poisons),
                  'dram_no_dax_map_checks':kind_counts['dram-no-dax'],
                  'post_transition_guest_block_reads':0},
    'candidate_pfn_audits':audit_summaries, 'three_vm_historical_candidate_intersections':groups,
    'reuse_cow_and_nfs_cold_refill':reclaims, 'canonical_full_byte_comparisons':canonical,
    'runtime_rejection_cases':[x['case'] for x in data['runtime_safety_negatives']],
    'admission_negative_cases':[x['case'] for event in data['admission_negative_tests'] for x in event['cases']],
    'limits': ['Validation uses two nested QEMU/KVM nodes with shared emulated CXL; it is not a physical noncoherent CXL protocol validation.',
               'CoW copies are private DRAM; CXL stages are hybrid.',
               'Canonical image slots and each VM base DAX reservation are retained; no net memory-capacity saving is claimed.',
               'Whole logical-RAM snapshots materialize aliases and private CoW bytes; existing state-only direct handoff is rejected while overlays exist.',
               'Measured shared bytes are candidate-frame alias evidence, not retrospective current guest-file-cache proof.']
}
if current_cache:
    assert args.current_cache_audit, 'current-cache capability requires independent cache join audit'
    proof = json.loads(args.current_cache_audit.read_text())
    assert proof['result'] == 'passed' and proof['run_result_at_read'] == 'passed'
    assert proof['report_sha256'] == result['report_sha256']
    assert len(proof['current_cache_audits']) == 17*cycles
    assert len(proof['frozen_three_vm_groups']) == 3*cycles
    result['current_cache_join_audit'] = proof
    result['proof_scope'] = 'Actual current guest payload-cache pages were joined from pinned file offsets to GPA, raw VMM admission HVA, and canonical PFNs. All three VMs stayed paused throughout each common-offset group scan.'
    result['three_vm_current_cache_intersections'] = result.pop('three_vm_historical_candidate_intersections')
    result['limits'][-1] = 'Current cache-page identity is proven by a validation-only guest oracle; its GPA records never construct pooling plans.'
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')
print(json.dumps({k:result[k] for k in ('result','raw_evidence','guest_workload','migration','reuse_cow_and_nfs_cold_refill')}, indent=2))
print('WROTE', OUT)
