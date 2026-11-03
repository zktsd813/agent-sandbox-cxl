#!/usr/bin/env python3
"""Offline verification for ABI 2 handle handoff; never invoked in a VM pause.

Unlike the historical v1 verifier, handoff mapping identity is proved by the
kernel-owned descriptor/cookie/generation and ownership transition. Full PTE
observations remain explicit stress evidence, not a per-handoff requirement.
"""
import argparse
import collections
import json
from pathlib import Path
import re

from verify_pool_handoff import (Evidence, PAGE, MIB, need, sha, registration,
                                status_proof, same, ranges, dram_mapping, paused, extended)


def handle(raw, memory):
    need(raw['version'] == 2 and not raw.get('mappings') and not raw.get('credits'), 'full mapping in normal export')
    h = raw['handle']
    need(h['abi'] == 2 and h['count']*PAGE == memory and h['state'] in (1, 2)
         and h['owner_epoch'] == int(raw['run_epoch'])
         and h['allocation_generation'] == raw['allocation_generation'] > 0
         and h['phys_base'] == raw['phys_base'] and h['device_bytes'] == raw['device_bytes']
         and h['exhausted'] == 0 and 0 <= h['shared_pages'] <= h['count'], 'invalid ownership handle')
    return h


def verify(path):
    path=Path(path); r=json.loads(path.read_text());e=Evidence(r,path.parent)
    need(r['result']=='passed' and r['arguments']['sandboxes']==3 and r['arguments']['cycles']>=2, 'incomplete stress matrix')
    memory=r['arguments']['memory_mib']*MIB;minimum=r['arguments']['minimum_cache_mib']*MIB
    for name,digest in r['source_hashes'].items():
        need(sha((path.parent/'measured-sources'/name).read_bytes())==digest,'source hash '+name)
    commands = [e.guest(c) for c in r['commands']]
    boots_by_vm = {}
    for c in commands: boots_by_vm.setdefault(c['vm'], set()).add(c['boot_id'])
    need(len(boots_by_vm) == 3 and all(len(v)==1 for v in boots_by_vm.values()), 'guest reboot during lifecycle')
    for api in r['api_events']:
        need(e.record(api['file'], 'LAB_API_JSON ') == api['response'] and api['response']['status'] in (200,204), 'API evidence mismatch/failure')
    incarnations = {}
    for launch in r['launches']:
        text = e.text(launch['file'])
        identity = re.findall(r'^EXEC_PROCESS (\d+) (\d+)\s*$', text, re.M)
        digest = re.findall(r'^([0-9a-f]{64})  /proc/(\d+)/exe\s*$', text, re.M)
        need(len(identity)==1, 'missing runtime incarnation')
        if launch.get('runtime_rehashed') is not False:
            need(len(digest)==1 and identity[0][0]==digest[0][1]
                 and digest[0][0]==launch['sha256']==r['fixture']['artifacts']['firecracker'], 'running VMM differs')
        incarnations[launch['directory']] = identity[0]
    authorities={a['file']:registration(e,a) for a in r['authority_events']}
    for key,prefix in [('mapper_smoke','POOL_MEMORY_SMOKE_JSON '),('kvm_mapper_smoke','POOL_MEMORY_KVM_SMOKE_JSON '),('handle_smoke','POOL_HANDLE_SMOKE_JSON ')]:
        value=e.record(r[key]['file'],prefix);need(value['result']=='passed','mapper smoke')
    need(e.record(r['handle_smoke']['file'],'POOL_HANDLE_SMOKE_JSON ')['handoff_full_scan_pages']==0,'handle import scanned')
    boots=[e.guest(x) for x in r['independent_boots']]
    need(len({x['boot_id'] for x in boots})==3,'not independent guests')
    grants={};lifetime={};initial={};released={}
    for event in r['allocator_events']:
        owner,generation=event['owner'],event['generation']
        if event['operation']=='allocate':
            frames=ranges(event['ranges']);need(len(frames)==event['pages'],'grant count')
            occupied=set().union(*grants.values()) if grants else set()
            need(not frames&occupied and all(2*MIB<=f<2048*MIB for f in frames),'cross-owner allocation')
            grants.setdefault(owner,set()).update(frames);lifetime.setdefault(generation,set()).update(frames)
            if generation not in initial: initial[generation] = set(frames)
        elif event['operation']=='release':
            frames=ranges(event['ranges']);need(grants.pop(owner)==frames and event['fenced'] and event['detached'],'release ownership')
            released[generation]=frames
        else:
            need(owner not in grants,'transfer aliases owner');grants[owner]=grants.pop(event['source_owner'])
    need(not grants and r['allocator_final']['allocated_bytes']==0,'allocation leak')
    hops=collections.Counter();counts=collections.Counter();sizes=[];timings=[];direct={}
    for t in r['transitions']:
        kind=t['kind'];counts[kind]+=1;need(t['result']=='passed','transition failed')
        if kind=='promotion':
            op=t['operation'];result=e.json(op['file']);plan=e.json(op['plan_file'])
            authority=authorities[t['authority']['file']];status_proof(result,authority,False)
            m=e.json(t['export']['file']);h=handle(m,memory)
            private=set();shared=0
            need(len(plan['mappings'])==memory//PAGE,'promotion coverage')
            for i,p in enumerate(plan['mappings']):
                need(p['gpa']==i*PAGE,'promotion GPA')
                if p['shared']:
                    need(authority['by_gpa'].get(p['gpa'],-1)+m['canonical_start']==p['offset'],'unauthorized shared page')
                    shared+=1
                else:
                    need(p['offset'] not in private,'private alias');private.add(p['offset'])
            credits=set(plan['credits']);need(len(credits)==len(plan['credits']) and not credits&private,'credits alias')
            need(private|credits==initial[h['allocation_generation']],'promotion grant mismatch')
            need(shared==h['shared_pages'] and len(credits)==h['credit_count'],'initial mapper counters')
            need(result['copied_private_bytes']==len(private)*PAGE and result['shared_bytes']==shared*PAGE>=minimum,'copy/shared accounting')
            same(e.guest(t['authority']['identity']),e.guest(t['after']))
            paused(e,t['vm'],t['epoch'],t['source'],op['file'])
        elif kind=='demotion':
            result=e.json(t['operation']['file']);h=handle(e.json(t['previous']['file']),memory)
            need(result['copied_private_bytes']==memory and result['module'] is None,'demotion incomplete')
            dram_mapping(result,memory,False)
            returned=ranges(t['returned_ranges'])
            need(returned==released[h['allocation_generation']] and len(returned)==h['count']-h['shared_pages']+h['credit_count'],'release count/lease')
            same(e.guest(t['before']),e.guest(t['after']))
        else:
            need(kind==r['mode'],'wrong mode');hops[t['vm']]+=1
            incoming=e.json(t['manifest_file']);target=e.json(t['destination_placement']['file'])
            a,b=handle(incoming,memory),handle(target,memory)
            for key in ('abi','phys_base','device_bytes','metadata_offset','metadata_bytes','cookie_lo','cookie_hi','allocation_generation','count','full_scan_pages'):
                need(a[key]==b[key],'handoff mapper identity/scan changed '+key)
            need(a['state']==2 and b['state']==1 and b['owner_epoch']==a['owner_epoch']+1,'ownership not consumed')
            need(b['generation']>=a['generation'] and b['cow_pages']>=a['cow_pages'],'generation rollback')
            need(len(lifetime[b['allocation_generation']])>=b['count']-b['shared_pages']+b['credit_count'],'grant accounting')
            need(t['ram_snapshot_bytes']==t['ram_transfer_bytes']==0,'RAM moved in handoff')
            need(e.order[t['manifest_file']]<e.order[t['source_fence_file']]<e.order[t['destination_placement']['file']], 'source fencing order')
            source = [v for v in r['launches'] if v['vm']==t['vm'] and v['epoch']==t['source_epoch'] and v.get('purpose','normal')=='normal']
            need(len(source)==1 and re.findall(r'^FENCED (\d+) (\d+)\s*$',e.text(t['source_fence_file']),re.M)==[incarnations[source[0]['directory']]], 'wrong source fenced')
            before,after=e.guest(t['before']),e.guest(t['after']);same(before,after);need(after['block_read_ios']==0,'warm cache lost')
            sizes.append(len(e.read(t['manifest_file'])))
            if kind=='state-only':
                state=e.read(t['state_file']);need(sha(state)==incoming['state_sha256'] and len(state)<MIB,'state binding')
                apis=[t['create_api'],t['load_api']]
            else:
                state=e.json(t['device_state_file']);need(sha(e.read(t['manifest_file']))==state['memory_manifest_sha256'],'device/mapper binding')
                need(t['cpu_snapshot_bytes']==0 and t['token']['vm_creates']==t['token']['vcpu_creates']==1,'direct object replacement')
                token=t['token'];old=direct.get(t['vm'])
                if old:
                    for key in ('vm_offset','vcpu_offset','vmcs_offset','run_offset','ept_root_offset','ram_hva','ram_bytes'):
                        need(old[key]==token[key],'direct object identity changed')
                direct[t['vm']]=token
                apis=[t['export_api'],t['attach_api']]
            timings.append([e.record(a['file'],'LAB_API_JSON ') for a in apis])
    need(set(hops)==({1,2,3} if r['mode']=='state-only' else {1}) and all(n==2*r['arguments']['cycles'] for n in hops.values()),'missing handoff cycles')
    shared_sizes=[]
    for group in r['sharing_audits']:
        common=None
        for audit in group['audits']:
            authority=authorities[audit['authority']['file']];raw=e.json(audit['pte_audit_file']);status_proof(raw,authority,False)
            selected=set()
            for p in raw['pages']:
                if p['gpa'] in authority['by_gpa'] and p['shared']:
                    need(p['present'] and not p['writable'] and p['offset']==2048*MIB+authority['by_gpa'][p['gpa']]
                         and p['actual_pfn']*PAGE==raw['phys_base']+p['offset'],'shared PTE/canonical mismatch')
                    selected.add(p['offset'])
            common=selected if common is None else common&selected
        need(len(common)*PAGE==group['common_shared_bytes']>=minimum,'shared cache too small')
        shared_sizes.append(len(common)*PAGE)
    ack=extended(e,authorities,memory,minimum,False)
    for refill in r['remote_refills']:
        guest=e.guest(refill['command'])
        def reads(name):return int(next(x for x in e.text(name).splitlines() if x.startswith('proc3 ')).split()[8])
        need(reads(refill['after_file'])-reads(refill['before_file'])==refill['nfs_read_delta']>0 and guest['block_read_ios']>0,'NFS cold refill absent')
    final=e.record(r['canonical_final']['file'],'POOL_DAX_COPY_JSON ')
    need(final['result']=='passed' and final['full_compare'],'canonical changed')
    for c in r['cleanup']:need(c['fenced'] and re.search(r'^FENCED \d+ \d+\s*$',e.text(c['file']),re.M),'cleanup')
    return {'result':'passed','mode':r['mode'],'transitions':dict(counts),'receipts':len(e.items),
            'handoff_full_scan_pages':0,'handoff_manifest_bytes':sizes,'minimum_shared_bytes':min(shared_sizes),
            'ack_before_reuse_bytes':ack,'api_records':timings,
            'scope':'Coherent nested lab, trusted kernel mapper. Per-handoff physical identity uses persistent handles; PTE audits are separate stress evidence.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('report',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args();value=verify(a.report);text=json.dumps(value,indent=2)+'\n'
    if a.output:a.output.write_text(text)
    print(text)
