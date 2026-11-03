#!/usr/bin/env python3
"""Offline checks for the runtime without per-handoff diagnostic probes.

Consumes completed workload output and optional VMM profiles. It never asks a
running VMM to hash an image, scan RAM, checkpoint, or run a negative API test.
"""
import collections
import json
from pathlib import Path
import re


def verify(path):
    from verify_swe_pool import (Evidence, MIB, PAGE, PRIVATE_START, PRIVATE_END,
        CANONICAL_START, need, sha, execution_identity, verify_trace, guest,
        raw_authority, status_proof, placement, ranges, identity_pair)
    path = Path(path)
    r = json.loads(path.read_bytes())
    need(r['schema'] == 'swe-pool-trace-v2' and r['result'] == 'passed', 'Incomplete runtime trial')
    e = Evidence(r, path.parent)
    memory = r['arguments']['memory_mib'] * MIB
    need(memory == 2048*MIB and r['mode'] in ('baseline', 'state-only', 'direct'), 'Unexpected runtime geometry')
    for name, digest in r['source_hashes'].items():
        need(Path(name).name == name and sha((path.parent/'measured-sources'/name).read_bytes()) == digest,
             'Saved runtime source changed')
    workload = verify_trace(e, r)
    launches = {}
    for launch in r['launches']:
        pids = re.findall(r'^EXEC_PROCESS (\d+) (\d+)\s*$', e.text(launch['file']), re.M)
        need(len(pids) == 1 and launch['directory'] not in launches, 'Duplicate/missing runtime incarnation')
        launches[launch['directory']] = (pids[0], launch['role'])
    for row in r['cleanup']:
        need(row.get('fenced') and row['directory'] in launches, 'Unfenced runtime')
        pid, role = launches[row['directory']]
        need(row['role'] == role and re.findall(r'^FENCED (\d+) (\d+)\s*$', e.text(row['file']), re.M) == [pid],
             'Cleanup fenced a different process')
    need(len(r['cleanup']) == len(launches), 'Runtime cleanup missing')
    configs = [a['request'] for a in r['api_events'] if a['path'] == '/machine-config']
    need(configs == [{'vcpu_count': 1, 'mem_size_mib': 2048}], 'Wrong VM configuration')
    disks = [a['request'] for a in r['api_events'] if a['path'] == '/drives/data']
    need(len(disks) == 1 and disks[0]['is_read_only'] and disks[0]['path_on_host'] == '/disk/sandbox.raw',
         'Wrong immutable block device')
    for api in r['api_events']:
        need(e.record(api['file'], 'LAB_API_JSON ') == api['response'] and api['response']['status'] in (200,204),
             'Failed/altered API result')
        if api['path'] == '/snapshot/create':
            need(r['mode'] == 'state-only' and api['request']['mem_file_path'] == '/dev/dax0.0', 'Full-RAM snapshot')
        if api['path'] == '/snapshot/load':
            need(r['mode'] == 'state-only' and api['request']['mem_backend']['backend_path'] == '/dev/dax0.0',
                 'Wrong snapshot RAM backend')
    setup = e.record(r['pool_setup']['file'], 'POOL_DAX_COPY_JSON ')
    need(setup['result'] == 'passed' and setup['operation'] == 'import' and
         setup['offset'] == CANONICAL_START and setup['bytes'] == r['trace']['image_bytes'], 'Wrong canonical preload')
    authorities = {a['file']:raw_authority(e,a,r['trace'],memory) for a in r['authority_events']}
    grants, initial = {}, {}
    for event in r['allocator_events']:
        owner, frames = event['owner'], ranges(event['ranges'])
        need(len(frames) == event['pages'] and all(PRIVATE_START <= x < PRIVATE_END for x in frames), 'Bad grant')
        if event['operation'] == 'allocate':
            need(not any(frames & old for old in grants.values()), 'Private allocation alias')
            grants.setdefault(owner,set()).update(frames)
            initial.setdefault(event['generation'],set()).update(frames)
        else:
            need(event['operation'] == 'release' and grants.get(owner) == frames and
                 event['fenced'] and event['detached'], 'Released live/foreign grant')
            del grants[owner]
    need(not grants, 'Leaked pool grant')
    counts, shared = collections.Counter(), []
    handoffs = []
    for transition in r['transitions']:
        kind = transition['kind']; counts[kind] += 1
        need(transition['result'] == 'passed', 'Failed transition')
        if kind in ('promotion','demotion'):
            operation = transition['operation']; raw = e.json(operation['file'])
            need(raw == operation['report'] and raw['result'] == 'passed' and
                 raw['ram_snapshot_bytes'] == raw['file_page_compare_bytes'] == 0, 'Invalid tier-change operation')
            if kind == 'promotion':
                authority = authorities[transition['authority']['file']]
                status_proof(raw,authority)
                value, private, pages, credits = placement(e,transition['export']['file'],memory,r['trace'],
                                                          e.json(operation['plan_file']))
                need(private | credits == initial[value['allocation_generation']], 'Promotion escaped grant')
                need(len(private)*PAGE == raw['copied_private_bytes'] and len(pages)*PAGE == raw['shared_bytes']
                     and raw['copied_private_bytes']+raw['shared_bytes'] == memory, 'Promotion accounting')
                need(all(authority['by_gpa'].get(gpa,-1)+CANONICAL_START == off for gpa,off in pages.items()),
                     'Shared page lacks guest authority')
                released = guest(e,transition['release'],'swe_pool_released')
                need(released['inactive'] and released['epoch'] == authority['summary']['vm_epoch'], 'Authority not released')
                shared.append(raw['shared_bytes'])
            else:
                need(raw['copied_private_bytes'] == memory, 'Demotion did not materialize RAM')
                identity_pair(e,transition['before'],transition['after'])
        elif kind == 'direct':
            need(transition.get('runtime_path') == 'resident-v2', 'Unexpected direct runtime')
            fast = e.record(transition['fast_file'],'RESIDENT_FAST_JSON ')
            from resident_proxy import verify_receipt
            verify_receipt(e.text(transition['fast_file']),fast)
            need(fast == transition['fast'] and fast['version'] == 2 and fast['ack'] is None and
                 set(fast['apis']) == {'send','receive'} and all(x['status'] == 204 for x in fast['apis'].values()),
                 'Runtime retained diagnostic API/guest probe or handoff failed')
            handoffs.append(transition)
        elif kind == 'state-only':
            state=e.read(transition['state_file']);manifest=e.json(transition['manifest_file'])
            need(0 < len(state) < MIB and sha(state) == manifest['state_sha256'] and
                 transition['ram_transfer_bytes'] == transition['ram_snapshot_bytes'] == 0, 'Bad state-only transfer')
            if transition.get('runtime_path') == 'state-v2':
                need('stop_snapshot_process' in e.text(transition['source_stop_file']), 'Source stop absent')
                need(transition['load_api']['request']['resume_vm'] is True, 'Restore did not resume')
            else:
                identity_pair(e,transition['before'],transition['after'])
            handoffs.append(transition)
        else:raise ValueError('Unknown transition')
    for preparation in r.get('resident_preparations',[]):
        server=preparation.get('servers',{}).get('B',{})
        if 'peer_ready' in server:
            peer=e.record(server['file'],'RESIDENT_PEER_READY_JSON ')
            need(peer==server['peer_ready'] and peer['owner_changed'] is False and
                 0<peer['connection_prepare_ns']<=server['native_boot']['native_ready_ns'],
                 'Peer connection preparation changed')
            need(all(hop['fast']['connection_prepare_ns']==0 for hop in handoffs),
                 'Prepared peer reconnected during handoff')
    events = {}
    for name,item in e.items.items():
        if item['kind'] != 'final-vmm-log':continue
        for line in e.text(name).splitlines():
            if 'DIRECT_HANDOFF_JSON ' in line:
                event=json.loads(line.split('DIRECT_HANDOFF_JSON ',1)[1])
                key=(event['event'],event['token']['generation'])
                need(key not in events,'Repeated handoff event');events[key]=event
    if r['mode'] == 'direct' and r['arguments'].get('restore_timing'):
        need(len(events) == 2*len(handoffs),'Missing profiled handoff')
        retained=None
        for i,hop in enumerate(handoffs,1):
            exp,imp=events[('export',i)],events[('import',i)]
            keys=('vm_offset','vcpu_offset','vmcs_offset','run_offset','ram_hva','ram_bytes','vm_creates','vcpu_creates')
            graph={k:exp['token'][k] for k in keys}
            need(all(imp['token'][k] == v for k,v in graph.items()) and
                 exp['token']['flags'] == 2 and imp['token']['flags'] == 1,'Changed kernel graph/owner')
            need(graph['vm_creates'] == graph['vcpu_creates'] == 1 and
                 exp['snapshot_bytes'] == exp['ram_transfer_bytes'] == imp['snapshot_bytes'] == imp['ram_transfer_bytes'] == 0,
                 'Snapshot or RAM copy on direct handoff')
            if retained is not None:need(graph == retained,'Recreated kernel graph')
            retained=graph
            need(imp['mapper']['handle']['state'] == 1 and int(imp['mapper']['run_epoch']) == hop['destination_epoch'],
                 'Wrong imported mapper epoch')
    if r['mode'] == 'baseline':
        need(not counts and not grants and not authorities,'Baseline changed memory tier')
    else:
        extra=2 if r['arguments'].get('resident_repromote_handoff') else 0
        if r['arguments'].get('inflight_handoff_index') is not None: extra += 2
        nhops=2+2*r['arguments'].get('direct_profile_roundtrips',0)+extra
        need(counts == {'promotion':2,'demotion':2,r['mode']:nhops},'Missing lifecycle case')
        need(shared and max(shared)>0,'No eligible guest cache shared')
        need([(x['source'],x['destination'],x['source_epoch'],x['destination_epoch']) for x in handoffs] ==
             [('A' if i%2==0 else 'B','B' if i%2==0 else 'A',i,i+1) for i in range(nhops)],'Wrong itinerary')
        need(r['allocator_final']['allocated_bytes'] == 0,'Allocator not empty')
    inflight = [command for command in r['commands'] if 'inflight' in command]
    requested = r['arguments'].get('inflight_handoff_index')
    need(len(inflight) == (0 if requested is None else 1), 'Missing/extra in-flight command')
    for command in inflight:
        proof = command['inflight']; start = guest(e,proof['started'],'trace_async_started')
        end = guest(e,command,'trace_end')
        need(start['index'] == end['index'] == requested and end['run_label'] == start['label'],
             'In-flight command identity mismatch')
        observed = proof['observations']; files = proof['handoff_files']
        need(len(observed) == 3 and len(files) == 2, 'Incomplete in-flight itinerary')
        selected = [hop for hop in handoffs if hop['fast_file'] in files]
        need([hop['fast_file'] for hop in selected] == files and
             [(hop['source'],hop['destination']) for hop in selected] == [('B','A'),('A','B')],
             'Wrong in-flight handoffs')
        for i, observation in enumerate(observed):
            live = guest(e,observation,'trace_running')
            need(live['active'] is True and live['process_state'] not in ('Z','X','x','missing') and
                 live['start_ticks'] > 0 and all(live[key] == start[key] for key in
                    ('index','command_pid','start_ticks')) and observation['role'] == ('A' if i==1 else 'B'),
                 'Command not alive across handoff')
        order = [proof['started']['file'],observed[0]['file'],files[0],observed[1]['file'],
                 files[1],observed[2]['file'],command['file']]
        need([e.order[f] for f in order] == sorted(e.order[f] for f in order), 'In-flight evidence reordered')
    return {'result':'passed','mode':r['mode'],'trace':r['trace']['name'],
        'report_sha256':sha(path.read_bytes()),**execution_identity(r),**workload,
        'transitions':dict(counts),'shared_cache_bytes':shared,'inflight_commands':len(inflight),
        'registered_cache_bytes':[x['count']*PAGE for x in authorities.values()],
        'runtime_image_rehash':False,'remote_nfs_reads':None,
        'per_handoff_guest_probes':any('before' in hop or 'after' in hop for hop in handoffs),
        'profiled_kernel_handoffs':len(events)//2,
        'limitations':['Coherent nested lab; immutable fixture identity comes from build records, not runtime rehash',
                       'No per-handoff source fault probes; those are separate backend-ablation tests',
                       'Per-command NFS counter polling removed; NFS read count was not measured']}
