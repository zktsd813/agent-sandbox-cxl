#!/usr/bin/env python3
"""Independently verify nfs_bench records against raw logs and state files.

Requires Python 3 and a C compiler (cc) for the independent deterministic-pattern
checksum calculation. No VM, NFS service or privileged access is required.

Keep the report, c*-A/B.log and c*-state.bin files together, or pass
--evidence-dir after extracting the evidence archive. Exact measured sources
are taken from EVIDENCE_DIR/measured-sources when available; --source-dir can
override this. --artifacts additionally checks original large build artifacts;
it is intentionally optional when those files are not distributed.

New runs also save per-sample host counter/cache observations and independently
derive their report values. Older archives remain readable but their audit is
explicitly marked host_observations=report_only_legacy.
"""
import argparse
import collections
import hashlib
import json
import math
import pathlib
import statistics
import subprocess
import tempfile
MIB = 1024 * 1024
SOURCE_DIR = pathlib.Path(__file__).resolve().parent

def require(ok, message):
    if not ok:
        raise AssertionError(message)

def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for b in iter(lambda : stream.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def checksums(ranges):
    # Recalculate the pattern independently, without using the probe binary.
    code = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
int main(int n,char**v){for(int a=1;a<n;a+=2){uint64_t off=strtoull(v[a],0,10)/8,len=strtoull(v[a+1],0,10)/8,sum=0;
for(uint64_t j=off;j<off+len;j++){uint64_t x=(j^UINT64_C(0x4e465350524f4245))+UINT64_C(0x9e3779b97f4a7c15);
x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb);sum+=x^(x>>31);}
printf("%016" PRIx64 "\n",sum);}return 0;}
"""
    ranges = sorted(ranges)
    with tempfile.TemporaryDirectory(prefix='nfs-audit-') as tmp:
        source = pathlib.Path(tmp) / 'checksum.c'
        exe = pathlib.Path(tmp) / 'checksum'
        source.write_text(code)
        subprocess.run(['cc', '-O2', str(source), '-o', str(exe)], check=True, capture_output=True)
        out = subprocess.check_output([str(exe), *[str(n) for r in ranges for n in r]], text=True)
    return dict(zip(ranges, out.splitlines()))

def log_events(path):
    result = []
    for line in path.read_text().splitlines():
        if line.startswith('{"event":'):
            result.append(json.loads(line))
    return result

def same_num(actual, expected, what):
    require(math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-06), f'{what}: {actual} != {expected}')

def raw_host_counters(text):
    """Parse the saved serial output without importing the benchmark parser."""
    result = {}
    for prefix, names in [
        ('COUNTERS=', ('rx_bytes', 'tx_bytes', 'nfs_read_calls')),
        ('MOUNT_READ=', ('mount_read_ops', 'mount_read_sent_bytes', 'mount_read_received_bytes')),
    ]:
        lines = [line[len(prefix):].strip() for line in text.splitlines() if line.startswith(prefix)]
        require(len(lines) == 1, f'raw host counters: missing/duplicate {prefix}')
        values = [int(value) for value in lines[0].split()]
        require(len(values) == len(names) and all(value >= 0 for value in values), f'raw host counters: invalid {prefix}')
        result.update(zip(names, values))
    return result

def audit_host_observation(sample, directory, image_bytes, image_name='sandbox.raw'):
    label = sample['label']
    filename = sample.get('host_observation_file')
    require(filename == label + '-host.json' and pathlib.Path(filename).name == filename,
            f'{label}: invalid/missing host observation filename')
    observation = json.loads((directory / filename).read_text())
    require(observation['schema_version'] == 1 and observation['label'] == label, f'{label}: host observation identity')
    before = raw_host_counters(observation['before'])
    after = raw_host_counters(observation['after'])
    delta = {key: after[key] - value for key, value in before.items()}
    require(all(value >= 0 for value in delta.values()), f'{label}: raw counter moved backwards')
    require(delta == sample['network_delta'], f'{label}: raw host counter/report disagreement')
    expected = sample.get('host_cache_state') or {}
    expected_roles = {role for role in ('A', 'B') if f'{role}_image_resident_pages' in expected}
    require(set(observation['cache']) == expected_roles, f'{label}: raw host cache roles')
    for role, snapshots in observation['cache'].items():
        require(len(snapshots) == 1, f'{label}/{role}: host cache snapshot count')
        snap = snapshots[0]
        image_path = ('/storage/' if role == 'A' else '/disk/') + image_name
        require(snap['path'] == image_path and snap['size'] == image_bytes, f'{label}/{role}: host cache image/range')
        rows = [json.loads(line) for line in snap['text'].splitlines() if line.startswith('{"event":"resident"')]
        require(len(rows) == 1, f'{label}/{role}: raw host mincore record count')
        row = rows[0]
        require(row['event'] == 'resident' and row['label'] == 'host' and row['offset'] == 0 and
                row['bytes'] == image_bytes and row['pages'] == image_bytes // 4096, f'{label}/{role}: raw host mincore range')
        require(0 <= row['resident_after'] <= row['pages'] and
                row['resident_after'] == expected[f'{role}_image_resident_pages'], f'{label}/{role}: raw host cache/report disagreement')
    if 'image_pages' in expected:
        require(expected['image_pages'] == image_bytes // 4096, f'{label}: host image page count')

def audit(path, args):
    # The report and raw evidence must agree before aggregate claims are accepted.
    r = json.loads(path.read_text())
    directory = args.evidence_dir or path.parent
    source_dir = args.source_dir or (directory / 'measured-sources' if (directory / 'measured-sources').is_dir() else SOURCE_DIR)
    if r['result'] == 'running':
        print(json.dumps({'result': 'pending', 'samples': len(r['samples']), 'report': str(path)}))
        return 3
    require(r['result'] == 'passed', f"benchmark result={r['result']}: {r.get('error')}")
    require(not r.get('cleanup_errors'), f"cleanup errors: {r.get('cleanup_errors')}")
    cycles = r['arguments']['cycles']
    reps = r['arguments']['repetitions']
    if args.formal:
        require((cycles, reps) == (4, 3), 'formal run must have cycles=4,repetitions=3')
    samples = r['samples']
    host_schema = r.get('host_observation_schema')
    require(host_schema in (None, 1), 'unsupported host observation schema')
    if host_schema is None:
        require(not any('host_observation_file' in s for s in samples) and not list(directory.glob('s*-host.json')),
                'host observation schema missing despite raw host evidence')
    image_bytes = (r['config']['prepared']['nfs']['images']['data_mib'] + 64) * MIB
    handoffs = r['handoffs']
    groups = collections.defaultdict(list)
    require(len(handoffs) == cycles * 2, 'handoff count')
    require(len({(h['cycle'], h['backend']) for h in handoffs}) == cycles * 2, 'duplicate handoff')
    expected_cs = checksums({(s['offset'], s['bytes']) for s in samples})
    labels = [s['label'] for s in samples]
    require(len(set(labels)) == len(labels), 'duplicate sample labels')
    all_logs = []
    per_trial = {}
    rpc_counter_differences = []
    for h in handoffs:
        key = (h['cycle'], h['backend'])
        trial = f'c{key[0]}_{key[1]}'
        ae = log_events(directory / (trial + '-A.log'))
        be = log_events(directory / (trial + '-B.log'))
        a_reads = [x for x in ae if x['event'] == 'read']
        b_reads = [x for x in be if x['event'] == 'read']
        require(len(a_reads) == 1 and a_reads[0]['label'] == trial + '_prime', f'{trial}: source prime record')
        ready = [x for x in ae if x['event'] == 'ready']
        require(len(ready) == 1, f'{trial}: source ready')
        require(not [x for x in be if x['event'] == 'ready'], f'{trial}: destination rebooted probe')
        cookie = h['source_cookie']
        require(cookie == h['destination_cookie'], f'{trial}: handoff cookie')
        require(all((x.get('open_cookie') == cookie for x in ae + be)), f'{trial}: guest identity changed')
        require(all((x['pid'] == ready[0]['pid'] for x in ae + be)), f'{trial}: guest pid changed')
        require(h['ram_transfer_bytes'] == 0, f'{trial}: RAM copied')
        state = directory / (trial + '-state.bin')
        require(state.stat().st_size == h['state_bytes'] == h['state_integrity']['bytes'], f'{trial}: state length')
        require(digest(state) == h['state_integrity']['sha256'], f'{trial}: state checksum')
        require(h['state_integrity']['source_unchanged_during_fetch'], f'{trial}: state changed')
        own = [s for s in samples if (s['cycle'], s['backend']) == key]
        raw = {x['label']: x for x in b_reads}
        require(len(raw) == len(b_reads), f'{trial}: duplicate raw read labels')
        measured_raw = {k: v for (k, v) in raw.items() if k.startswith('s')}
        require(set(measured_raw) == {s['label'] for s in own}, f'{trial}: raw/report label disagreement')
        for s in own:
            for (k, v) in measured_raw[s['label']].items():
                require(s[k] == v, f"{trial}/{s['label']}: raw field {k}")
        for x in a_reads + b_reads:
            require(x['ok'] and x['mismatched_words'] == 0 and (x['first_bad_offset'] is None), f"{trial}/{x['label']}: data error")
            require(x['checksum'] == expected_cs[x['offset'], x['bytes']], f"{trial}/{x['label']}: independent pattern checksum")
        for x in be:
            if x['event'] == 'evict':
                require(x['resident_after'] == 0, f"{trial}/{x['label']}: guest eviction failed")
        per_trial[key] = own
        all_logs += ae + be
    for s in samples:
        label = s['label']
        if host_schema == 1:
            audit_host_observation(s, directory, image_bytes)
        condition = s['condition']
        backend = s['backend']
        pattern = s['pattern']
        require(s['event'] == 'read' and s['mode'] == pattern and s['measured'], f'{label}: sample type')
        require(s['ok'] and s['mismatched_words'] == 0 and (s['first_bad_offset'] is None), f'{label}: checksum error')
        require(s['checksum'] == expected_cs[s['offset'], s['bytes']], f'{label}: independent checksum')
        require(s['page_size'] == 4096 and s['pages'] == s['bytes'] // 4096, f'{label}: page count')
        require(s['resident_after'] == s['pages'], f'{label}: not all pages retained after read')
        require(s['blocks'] == s['bytes'] // s['block_bytes'], f'{label}: block count')
        require(s['pread_calls'] >= s['blocks'], f'{label}: too few preads')
        t = s['pread_latency_ns']
        require(t['samples'] == s['pread_calls'], f'{label}: latency count')
        require(0 < t['min'] <= t['p50'] <= t['p95'] <= t['p99'] <= t['max'], f'{label}: latency ordering')
        require(t['min'] * t['samples'] <= s['read_ns'] <= t['max'] * t['samples'], f'{label}: latency aggregate bounds')
        require(s['pread_p99_ns'] == t['p99'], f'{label}: copied p99')
        require(s['wall_ns'] >= s['read_ns'] + s['validation_ns'], f'{label}: time accounting')
        same_num(s['read_mib_s'], s['bytes'] / MIB * 1000000000.0 / s['read_ns'], label + ' read throughput')
        same_num(s['wall_mib_s'], s['bytes'] / MIB * 1000000000.0 / s['wall_ns'], label + ' wall throughput')
        disk = s['block_delta']
        net = s['network_delta']
        cache = s['host_cache_state']
        require(all((v >= 0 for v in disk.values())) and all((v >= 0 for v in net.values())), f'{label}: negative counter')
        require(disk['write_ios'] == disk['write_sectors'] == 0, f'{label}: unexpected writes')
        # proc3 increments at call_start; mountstats accounts completed RPCs.
        # They are sampled sequentially, so exact per-window equality is not
        # an invariant. Preserve discrepancies instead of hiding or tolerating
        # a chosen numeric threshold; zero/positive evidence must still agree.
        if net['mount_read_ops'] != net['nfs_read_calls']:
            rpc_counter_differences.append({
                'label': label, 'backend': backend, 'cycle': s['cycle'],
                'condition': condition, 'pattern': pattern,
                'global_started_read_calls': net['nfs_read_calls'],
                'mount_completed_read_ops': net['mount_read_ops'],
                'completed_minus_started': net['mount_read_ops'] - net['nfs_read_calls'],
            })
        if condition in {'retained', 'retouch', 'warm'}:
            require(s['resident_before'] == s['pages'], f'{label}: warm precondition')
            require(disk['read_ios'] == disk['read_sectors'] == net['nfs_read_calls'] == net['mount_read_ops'] == net['mount_read_received_bytes'] == 0, f'{label}: warm backing I/O')
        else:
            require(s['resident_before'] == 0 and disk['read_ios'] > 0 and (disk['read_sectors'] > 0), f'{label}: cold guest precondition')
        if backend == 'local' or condition in {'retained', 'retouch', 'warm', 'cold_guest'}:
            require(net['nfs_read_calls'] == net['mount_read_ops'] == net['mount_read_received_bytes'] == 0, f'{label}: unexpected NFS reads')
        elif backend == 'nfs':
            require(net['nfs_read_calls'] > 0 and net['mount_read_ops'] > 0 and net['mount_read_received_bytes'] > 0, f'{label}: missing NFS read')
        if condition == 'retained':
            require(cache['B_image_resident_pages'] == 0, f'{label}: retained host image not cold')
        if condition in {'cold_guest', 'cold_host', 'cold_all'}:
            require(cache['image_pages'] > 0, f'{label}: absent image page count')
            a = 0 if condition == 'cold_all' else cache['image_pages']
            b = cache['image_pages'] if condition == 'cold_guest' else 0
            require(cache['A_image_resident_pages'] == a and cache['B_image_resident_pages'] == b, f'{label}: host cache state')
        groups[backend, pattern, condition].append(s)
    has_retouch = any((s['condition'] == 'retouch' for s in samples))
    if args.formal:
        require(has_retouch, 'formal run missing retouch')
    expected_groups = {}
    for backend in ['local', 'nfs']:
        for pattern in ['seq', 'random']:
            for condition in ['warm', 'cold_guest', 'cold_host'] + (['cold_all'] if backend == 'nfs' else []):
                expected_groups[backend, pattern, condition] = cycles * reps
        for condition in ['retained', 'first_miss'] + (['retouch'] if has_retouch else []):
            expected_groups[backend, 'seq', condition] = cycles
    require(set(groups) == set(expected_groups), 'unexpected/missing groups')
    for (key, expected) in expected_groups.items():
        require(len(groups[key]) == expected, f'{key}: count != {expected}')
    for cycle in range(cycles):
        for pattern in ['seq', 'random']:
            for condition in ['warm', 'cold_guest', 'cold_host']:
                seeds = []
                for backend in ['local', 'nfs']:
                    seeds.append(sorted((s['seed'] for s in per_trial[cycle, backend] if s['pattern'] == pattern and s['condition'] == condition)))
                require(seeds[0] == seeds[1], f'{cycle}/{pattern}/{condition}: unmatched backend seeds')
                require(seeds[0] == [r['arguments']['seed'] + cycle * 1000 + rep for rep in range(reps)], 'seed not cycle/rep matched')
    summaries = {(s['backend'], s['pattern'], s['condition']): s for s in r['summary']}
    require(set(summaries) == set(groups), 'summary group mismatch')
    for (key, rows) in groups.items():
        summary = summaries[key]
        require(summary['n'] == len(rows), f'{key}: summary count')
        for metric in ['read_ns', 'wall_ns', 'read_mib_s', 'wall_mib_s', 'pread_p99_ns']:
            vals = [s[metric] for s in rows]
            for (stat, expected) in [('median', statistics.median(vals)), ('min', min(vals)), ('max', max(vals))]:
                same_num(summary[metric][stat], expected, f'{key}/{metric}/{stat}')
        for (metric, vals) in [('guest_read_bytes_median', [s['block_delta']['read_sectors'] * 512 for s in rows]), ('nfs_read_calls_median', [s['network_delta']['nfs_read_calls'] for s in rows]), ('network_rx_bytes_median', [s['network_delta']['rx_bytes'] for s in rows]), ('nfs_read_received_bytes_median', [s['network_delta']['mount_read_received_bytes'] for s in rows])]:
            if metric in summary:
                same_num(summary[metric], statistics.median(vals), f'{key}/{metric}')
            elif args.formal:
                raise AssertionError(f'{key}: missing {metric}')
    source_checks = []
    build_runtime_differences = {}
    runtime = r.get('runtime_sources', {})
    if args.formal:
        require(bool(runtime), 'missing runtime source provenance')
    for (name, expected) in runtime.items():
        require(digest(source_dir / name) == expected, f'runtime source drift: {name}')
        source_checks.append(name)
    prepared = r['config']['prepared']
    built = prepared['nfs']['sources']
    for (name, expected) in built.items():
        if name in runtime and runtime[name] != expected:
            build_runtime_differences[name] = {'build': expected, 'runtime': runtime[name]}
    for compiled in ['nfs_probe.c', 'nfs-l2-init.sh']:
        if runtime:
            require(built[compiled] == runtime[compiled], f'compiled source drift: {compiled}')
    artifact_checks = []
    if args.artifacts:
        for (name, expected) in prepared['artifacts'].items():
            artifact_path = (args.assets_dir or pathlib.Path(r['config']['assets'])) / name
            require(digest(artifact_path) == expected, f'prepared artifact drift: {artifact_path}')
            artifact_checks.append(name)
    result = {'result': 'passed', 'report': str(path), 'samples': len(samples), 'handoffs': len(handoffs), 'groups': len(groups), 'raw_events': len(all_logs), 'host_observations': 'raw_validated' if host_schema == 1 else 'report_only_legacy', 'host_observation_samples_checked': len(samples) if host_schema == 1 else 0, 'rpc_counter_differences': rpc_counter_differences, 'rpc_counter_note': 'proc3 counts calls at start; mountstats counts completed tasks; snapshots are sequential. Exact cause of an observed difference is not identified by these aggregate counters.', 'independent_checksums': {str(k): v for (k, v) in expected_cs.items()}, 'steady_samples_per_group': cycles * reps, 'runtime_sources_checked': source_checks, 'measured_source_directory': str(source_dir), 'schema_version': 2, 'artifact_files_checked': artifact_checks, 'build_runtime_source_differences': build_runtime_differences}
    serialized = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.json_out:
        args.json_out.write_text(serialized)
    print(serialized, end='')
    return 0
if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report', type=pathlib.Path)
    p.add_argument('--formal', action='store_true', help='Require the published 4-cycle, 3-repeat matrix and full provenance')
    p.add_argument('--artifacts', action='store_true', help='Also verify original build artifacts (requires assets directory)')
    p.add_argument('--source-dir', type=pathlib.Path, help='Exact measured runtime sources; defaults to evidence-dir/measured-sources')
    p.add_argument('--evidence-dir', type=pathlib.Path, help='Extracted raw log/state directory, defaults to report parent')
    p.add_argument('--assets-dir', type=pathlib.Path, help='Override original asset directory for --artifacts')
    p.add_argument('--json-out', type=pathlib.Path, help='Write the successful audit evidence as deterministic JSON')
    args = p.parse_args()
    raise SystemExit(audit(args.report, args))
