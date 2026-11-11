#!/usr/bin/env python3
"""Five 4KiB read paths plus shared-cache reread, timed inside the guest.

Requires the peer_cache_prepare.py assets and a fresh static CXL/NFS lab.
Cold means NFS client page cache cold, server page cache warm. Donor RPC
remains INSIDE the guest read timer; serial/fresh-TCP options are ablations.
"""
import argparse
import json
import math
import random
import re
import statistics
from pathlib import Path

import lab
import nfs_bench as nfs
from home_storage_bench import require
from peer_cache_bench import Bench, setup
from pool_bench import PoolController, MIB


def stats(values):
    ordered = sorted(values)
    return {"n": len(values), "p50_us": statistics.median(values) / 1000,
            "p95_us": ordered[math.ceil(len(values) * .95) - 1] / 1000,
            "min_us": ordered[0] / 1000, "max_us": ordered[-1] / 1000}


def nfs_reads(raw):
    rows = [line.split() for line in raw.splitlines() if line.startswith("proc3 ")]
    require(len(rows) == 1, "missing NFSv3 client counters")
    return int(rows[0][2 + 6])  # NFSv3 procedure READ, after label and count.


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--samples', type=int, default=64)
    p.add_argument('--rounds', type=int, default=2)
    p.add_argument('--cache', choices=['warm', 'cold', 'both'], default='both')
    p.add_argument('--no-profile', action='store_true')
    p.add_argument('--guest-transport', choices=['vsock','serial'], default='vsock')
    p.add_argument('--fresh-tcp', action='store_true')
    a = p.parse_args()
    caches = ['warm', 'cold'] if a.cache == 'both' else [a.cache]
    require(0 < a.samples * a.rounds * len(caches) <= 768, 'donor pin budget')
    _, config = lab.current_run()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    args = argparse.Namespace(memory_mib=256, vcpus=1, sandboxes=2,
                              minimum_cache_mib=0, io_engine='Sync')
    c = PoolController(config, out, args)
    manifest = json.loads((Path(config['assets']) / 'manifest.json').read_text())
    b = Bench(c, manifest, guest_transport=a.guest_transport, reuse_tcp=not a.fresh_tcp)
    report = {'result': 'running', 'source_head': manifest['peer_cache']['source_head'],
              'uncommitted': manifest['peer_cache'].get('uncommitted', True),
              'parameters': vars(a) | {'output': str(out)},
              'lab_run': config['run'], 'assets': config['assets'],
              'network_backend': config['migration_network']['backend'],
              'timer': 'guest CLOCK_MONOTONIC_RAW around one pread(4096); prefaulted buffer',
              'conditions': 'static CXL, single donor, NFSv3 Sync, one outstanding read',
              'cold_definition': 'client page cache dropped; server exact block warmed',
              'canonical_prefill_bytes': 0, 'samples': [], 'summaries': {}, 'audits': []}
    pages = iter(random.Random(1105).sample(range(128, 40800),
                 a.samples * a.rounds * len(caches) * 4))
    logs = {}

    def stop_b(label):
        directory = b.guests['B']
        logs[label] = c.fetch_bytes('B', directory + '/vmm.log', label + '-vmm').decode()
        c.fetch_bytes('B', directory + '/fc.log', label + '-guest')
        b.stop('B', directory)
        c.active.remove(('B', directory))

    def sample(case, page, cache, round_):
        offset = b.offset(page)
        # Both endpoints are owned lab VMs; never drop caches on the real host.
        c.shell('A', f'dd if=/disk/sandbox.raw of=/dev/null bs=4096 skip={offset//4096} count=1 2>/dev/null', 'server-warm')
        prep = (f'dd if=/disk/sandbox.raw of=/dev/null bs=4096 skip={offset//4096} count=1 2>/dev/null'
                if cache == 'warm' else 'echo 3 > /proc/sys/vm/drop_caches')
        raw, _ = c.shell('B', prep + '\ncat /proc/net/rpc/nfs', 'client-cache')
        before = nfs_reads(raw)
        result = b.guest('B', 'read', page)
        raw, _ = c.shell('B', 'cat /proc/net/rpc/nfs', 'nfs-after')
        reads = nfs_reads(raw) - before
        require(result['before'] == 0 and result['after'] == 1, 'not a guest cold read')
        require(reads == 0 if case == 'shared_hit' or cache == 'warm' else reads > 0,
                f'unexpected NFS READ delta {case} {cache}: {reads}')
        row = dict(result, case=case, cache=cache, round=round_, offset=offset, nfs_reads=reads)
        report['samples'].append(row)
        return row

    try:
        lab.verify(argparse.Namespace(timeout=180))
        setup(c)
        b.boot('A', [], profile=not a.no_profile)
        # Warm guest inode/extent metadata before any timed measurements.
        b.guest('A', 'warm', 0)
        for round_ in range(a.rounds):
            for cache in caches:
                groups = {name: [next(pages) for _ in range(a.samples)]
                          for name in ['baseline_miss', 'shared_hit', 'exact_miss', 'bloom_negative']}
                tag = f'{round_}-{cache}'
                b.boot('B', [], peer_cache=False, profile=not a.no_profile, label=tag+'-base')
                b.guest('B', 'warm', 0)
                for page in groups['baseline_miss']:
                    row = sample('baseline_miss', page, cache, round_)
                    # Immediate second read is the ordinary local guest cache hit.
                    hit = b.guest('B', 'read', page)
                    require(hit['before'] == 1 and hit['hash'] == row['hash'], 'local hit mismatch')
                    report['samples'].append(dict(hit, case='baseline_hit', cache=cache,
                                                round=round_, offset=b.offset(page), nfs_reads=0))
                    require(b.guest('A', 'read', page)['hash'] == row['hash'], 'baseline data mismatch')
                stop_b(tag+'-base')
                donor = {}
                b.bloom_fill(0)
                for page in groups['shared_hit']:
                    donor[page] = b.guest('A', 'warm', page)
                    ad = b.rpc('A', op='advertise', image=c.image_id, offset=b.offset(page))
                    require(ad.get('found') and ad['frame'] == 2*MIB + ad['donor_gpa'], 'nonresident/copied donor')
                    donor[page].update(ad)
                b.boot('B', groups['shared_hit'], profile=not a.no_profile, label=tag+'-peer')
                b.guest('B', 'warm', 0)
                for case in ['shared_hit', 'exact_miss', 'bloom_negative']:
                    if case == 'exact_miss': b.bloom_fill(255)
                    if case == 'bloom_negative': b.bloom_fill(0)
                    for page in groups[case]:
                        if case == 'exact_miss':
                            require(b.guest('A', 'cache', page)['resident'] == 0, 'donor already resident')
                        row = sample(case, page, cache, round_)
                        if case == 'exact_miss':
                            require(b.guest('A', 'cache', page)['resident'] == 0, 'lookup faulted in donor data')
                        expected = donor[page]['hash'] if case == 'shared_hit' else b.guest('A', 'read', page)['hash']
                        require(row['hash'] == expected, f'{case} data mismatch')
                        if case == 'shared_hit':
                            hit = b.guest('B', 'read', page)
                            require(hit['before'] == hit['after'] == 1 and hit['hash'] == expected,
                                    'shared cache hit mismatch')
                            report['samples'].append(dict(hit, case='shared_cache_hit', cache=cache,
                                round=round_, offset=b.offset(page), nfs_reads=0))
                    print(f'{tag} {case}: {stats([v["read_ns"] for v in report["samples"] if v["case"] == case and v["cache"] == cache and v["round"] == round_])}', flush=True)
                # Actual native PTE identity and write protection, outside all timers.
                raw = c.fetch_bytes('B', b.guests['B']+'/vmm.log', tag+'-audit').decode()
                for page in groups['shared_hit']:
                    hits = re.findall(rf'PEER_READ_HIT offset={b.offset(page)} gpa=(\d+) frame=(\d+)', raw)
                    require(len(hits) == 1, 'missing/duplicate shared read')
                    gpa, frame = map(int, hits[0])
                    require(frame == donor[page]['frame'], 'donor frame changed')
                    b.guest('A', 'read', page)
                    ap = b.rpc('A', op='audit', gpa=donor[page]['donor_gpa'])['pte']
                    bp = b.rpc('B', op='audit', gpa=gpa)['pte']
                    require(ap['pfn'] == bp['pfn'] != 0 and ap['flags'] == bp['flags'] == 1, 'PFN/RO audit failed')
                    report['audits'].append(dict(page=page, donor=ap, consumer=bp))
                stop_b(tag+'-peer')
                for row in report['samples']:
                    if row['round'] != round_ or row['cache'] != cache: continue
                    base = row['case'].startswith('baseline')
                    log = logs[tag + ('-base' if base else '-peer')]
                    records = [json.loads(line.split(' ', 1)[1]) for line in log.splitlines()
                               if line.startswith(('PEER_LATENCY ', 'PEER_BACKEND_LATENCY '))]
                    records = [r for r in records if r['offset'] == row['offset']]
                    cache_hit = row['case'] in ('baseline_hit', 'shared_cache_hit')
                    row['vmm'] = [] if cache_hit else records
                    marker = {'shared_hit':'PEER_READ_HIT', 'exact_miss':'PEER_EXACT_MISS',
                              'bloom_negative':'PEER_BLOOM_NEGATIVE'}.get(row['case'])
                    if marker:
                        require(len(re.findall(rf'{marker} offset={row["offset"]}(?: |\n)', log)) == 1,
                                'expected path marker absent/duplicated')
                    if not a.no_profile and not cache_hit:
                        peer = [r for r in records if 'path' in r]
                        backend = [r for r in records if 'backend_ns' in r]
                        require(len(peer) == (0 if base else 1), f'peer count {row}')
                        if peer: require(peer[0]['path'] == row['case'], 'wrong path')
                        require(len(backend) == (0 if row['case'] == 'shared_hit' else 1), 'backend count')
                        require(all(r.get('bytes', 4096) == 4096 for r in records), 'not single 4KiB')
                peer_log = logs[tag+'-peer']
                connects = peer_log.count('PEER_TCP_CONNECT ')
                expected_connects = 2*a.samples if a.fresh_tcp else 1
                require(connects == expected_connects, f'TCP connects: {connects}')
                require('PEER_LOOKUP_ERROR' not in peer_log, 'unexpected lookup fallback')
                report.setdefault('tcp_connects', []).append(dict(round=round_, cache=cache, count=connects))
                lab.save_json(out/'report.json', report)
        for cache in caches:
            summary = {}
            for case in ['baseline_hit','baseline_miss','shared_hit','exact_miss','bloom_negative','shared_cache_hit']:
                rows = [r for r in report['samples'] if r['cache'] == cache and r['case'] == case]
                summary[case] = stats([r['read_ns'] for r in rows])
                summary[case]['nfs_reads'] = sum(r['nfs_reads'] for r in rows)
                timings = [v for r in rows for v in r['vmm']]
                for key in ['bloom_ns','connect_ns','rpc_ns','donor_rpc_ns','guest_connect_ns','donor_ioctl_ns','publish_ns','attach_ns','total_ns','backend_ns']:
                    values = [v[key] for v in timings if key in v]
                    if values: summary[case][key] = stats(values)
            report['summaries'][cache] = summary
        donor_log = c.fetch_bytes('A', b.guests['A']+'/vmm.log', 'donor-channel').decode()
        donor_guest = c.fetch_bytes('A', b.guests['A']+'/fc.log', 'donor-guest-channel').decode()
        if a.guest_transport == 'vsock':
            require(donor_log.count('PEER_GUEST_CONNECT ') == 1, 'guest channel not reused')
            require('"event":"peer_lookup"' not in donor_guest, 'serial lookup still used')
        report['guest_channel_connects'] = donor_log.count('PEER_GUEST_CONNECT ')
        report['result'] = 'passed'
    except BaseException as exc:
        report.update(result='failed', error=str(exc))
        raise
    finally:
        for role, directory in reversed(c.active):
            c.fetch_bytes(role, directory+'/vmm.log', 'final-'+role)
            c.fetch_bytes(role, directory+'/fc.log', 'final-guest-'+role)
            b.stop(role, directory)
        lab.save_json(out/'report.json', report)
    print(json.dumps(report['summaries'], indent=2))


if __name__ == '__main__':
    main()
