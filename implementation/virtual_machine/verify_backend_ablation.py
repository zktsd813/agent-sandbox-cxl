#!/usr/bin/env python3
"""Verify raw backend-ablation receipts and derive matched latency summaries."""
import argparse
from experiment_sources import verify_sources
import hashlib
import json
from pathlib import Path
import statistics


def need(ok, message):
    if not ok: raise ValueError(message)


def records(text, marker):
    return [json.loads(line.split(marker,1)[1]) for line in text.splitlines()
            if marker in line and line.split(marker,1)[1].lstrip().startswith('{')]


def stats(values):
    return {'n':len(values),'p50':statistics.median(values),'min':min(values),'max':max(values)}


def trial(path):
    r=json.loads(path.read_text());need(r['result']=='passed','Unpassed trial')
    receipts={e['file']:e for e in r['receipts']}
    need(len(receipts)==len(r['receipts']),'Duplicate receipt')
    def read_bytes(name):
        data=(path.parent/name).read_bytes()
        need(hashlib.sha256(data).hexdigest()==receipts[name]['sha256'],'Receipt changed: '+name)
        return data
    def read(name): return read_bytes(name).decode()
    for name in receipts:read_bytes(name)
    def receipt_record(row,marker):
        found=records(read(row['file']),marker)
        value=row.get('receipt',row.get('response'))
        need(value in found,'Summary differs from raw record')
        return value
    logs={role:read(next(e['file'] for e in r['logs'] if e['role']==role and e['name']=='vmm.log')) for role in ('A','B')}
    guest={role:read(next(e['file'] for e in r['logs'] if e['role']==role and e['name']=='fc.log')) for role in ('A','B')}
    profiles={};events={};prepares=[];disk_profiles=[]
    for role,text in logs.items():
        last=None
        for line in text.splitlines():
            if 'DIRECT_HANDOFF_JSON ' in line:
                value=records(line,'DIRECT_HANDOFF_JSON ')[0]
                last=(value['event'],value['token']['generation'])
                need(last not in events,'Repeated handoff generation');events[last]=value
            if 'DIRECT_RESIDENT_JSON ' in line:
                value=records(line,'DIRECT_RESIDENT_JSON ')[0]
                if value['event']=='prepare':prepares.append({'role':role,**value})
            if 'RESTORE_TIMING ' in line:
                value=records(line,'RESTORE_TIMING ')[0]
                if value['scope'] in ('direct_export','direct_import','direct_resident_import'):
                    need(last is not None and last not in profiles,'Unbound/repeated profile');profiles[last]=value
                if value['scope']=='disk_properties':disk_profiles.append({'role':role,**value})
    for text in guest.values():
        need('Kernel panic' not in text and 'POOL_GUEST_FATAL' not in text,'Guest failed')
    promotion=r['promotion']['operation']['report']
    need(promotion['shared_bytes']==0 and promotion['copied_private_bytes']==2*1024**3
        and promotion['file_page_compare_bytes']==promotion['ram_snapshot_bytes']==0,'Promotion control differs')
    hops=[];previous=json.loads(read(r['promotion']['export']['file']));retained=None
    need(len(r['hops'])>=4 and len(r['hops'])%2==0,'Insufficient roundtrips')
    before=r['before']['receipt']
    for row in (r['before'],r['after'],r['demotion']['after']):
        raw=read(row['file'])
        need(any(json.loads(line)==row['receipt'] for line in raw.splitlines() if line.startswith('{"event":')),'Guest oracle not in raw log')
        value=row['receipt']
        need(value['resident_after']*4096>=128*1024*1024 and value['payload_hash']==r['fixture']['pool']['image']['generator']['hash']
            and value['checked_payload_words']==160*1024*1024//8,'Warm payload oracle failed')
        for key in ('boot_id','pid','cookie','generation','private_hash','upper_hash'):
            need(value[key]==before[key],'Guest identity/private state changed')
    for index,h in enumerate(r['hops']):
        timing=h['timing']
        need(records(read(h['file']),'RESIDENT_FAST_JSON ')==[timing],'Native timing summary changed')
        from resident_proxy import verify_receipt
        verify_receipt(read(h['file']),timing)
        need(all(v['status']==204 for v in timing['apis'].values()),'API error')
        if timing['version']==2:need(set(timing['apis'])=={'send','receive'},'Extra/missing runtime API')
        state=json.loads(read(h['state_file']));sealed=json.loads(read(h['sealed_file']));manifest=json.loads(read(h['manifest_file']))
        token=state['token'];generation=token['generation']
        if h.get('kernel_profile'):
            kp=h['kernel_profile'];value=json.loads(read(kp['file']))
            need(value=={k:v for k,v in kp.items() if k!='file'} and value['generation']==generation
                and value['bytes']==128 and value['offset']==12288*1024**2+512
                and len(value['attach_ns'])==len(value['detach_ns'])==8,'Kernel profile binding')
        need(token==h['export_token'] and generation==index+1,'Token/generation mismatch')
        if timing['manifest_bytes']==0:
            need(h.get('packet_file') and len(read_bytes(h['packet_file']))==timing['state_bytes'] and
                 state['memory_manifest_sha256']=='', 'Binary packet receipt mismatch')
        else:
            need(len(read_bytes(h['state_file']))==timing['state_bytes'] and len(read_bytes(h['sealed_file']))==timing['manifest_bytes'], 'Wrong wire byte count')
            need(hashlib.sha256(read_bytes(h['sealed_file'])).hexdigest()==state['memory_manifest_sha256'],'Manifest digest mismatch')
        devices=state['devices']['virtio_state']['Mmio']
        need(len(devices['block_devices'])==(r['arm']!='none'),'Wrong block graph')
        for key in ('abi','phys_base','device_bytes','metadata_offset','metadata_bytes','cookie_lo','cookie_hi','allocation_generation','count','full_scan_pages'):
            need(sealed['handle'][key]==manifest['handle'][key],'Mapper changed during handoff')
        need(sealed['handle']['state']==2 and manifest['handle']['state']==1
            and manifest['handle']['owner_epoch']==sealed['handle']['owner_epoch']+1,'Invalid mapper ownership handoff')
        if previous is not None:need(previous['handle']['full_scan_pages']==manifest['handle']['full_scan_pages'],'Handoff scanned mapping table')
        previous=manifest
        identity={k:token[k] for k in ('vm_offset','vcpu_offset','vmcs_offset','run_offset','ept_root_offset','ram_hva','ram_bytes','vm_creates','vcpu_creates')}
        if retained is not None:need(identity==retained,'Kernel graph replaced')
        retained=identity
        need(token['vm_creates']==token['vcpu_creates']==1 and token['ram_bytes']==2*1024**3,'Wrong retained VM')
        exp,imp=events[('export',generation)],events[('import',generation)]
        if timing['version']==2:need(imp.get('mapper')==manifest,'Imported mapper log differs')
        need(exp['token']==token and all(imp['token'][k]==v for k,v in identity.items()),'Kernel attach graph differs')
        need(exp['snapshot_bytes']==exp['ram_transfer_bytes']==imp['snapshot_bytes']==imp['ram_transfer_bytes']==0,'RAM transfer on handoff')
        need(imp['token']['flags']==1 and token['flags']==2,'Kernel ownership flags')
        a=timing['ack']
        if not r.get('inline_guest_ack',True):
            need(a is None and timing['guest_ack_ns']==0,'Guest probe in runtime interval')
            a=h['guest_ack']['receipt']
            need(any(json.loads(line)==a for line in read(h['guest_ack']['file']).splitlines()
                if line.startswith('{"event":')),'Separate guest ACK summary changed')
        need(isinstance(a,dict) and a['label']==f'hop{index+1}' and all(a[k]==before[k] for k in ('boot_id','pid','cookie')),'Invalid ACK')
        guest_pings=[json.loads(line) for line in guest[h['destination']].splitlines() if line.startswith('{"event":"trace_ping"')]
        need(a in guest_pings,'ACK absent from actual guest log')
        probe=next(p for p in r['source_probes'] if p['role']==h['source'] and p['receipt']['generation']==generation)
        p=receipt_record(probe,'DIRECT_RESIDENT_PROBE_JSON ')
        need(p['memory_errno']==14 and p['kvm_errno']!=0,'Old source can access VM')
        row={'generation':generation,'destination':h['destination'],'first':h['first_import'],
            'request_resume_ms':timing['request_to_resume_response_ns']/1e6,'request_complete_ms':timing['request_ns']/1e6,
            'external_command_ms':h.get('controller_command_wall_ns',0)/1e6,
            'export_attach_api_ms':sum(timing['apis'][k]['duration_us'] for k in (('send','receive') if timing['version']==2 else ('export','attach')))/1000,
            'kernel_ms':(token['kernel_ns']+imp['token']['kernel_ns'])/1e6,
            'kernel_profile':h.get('kernel_profile'),
            'export_profile':profiles.get(('export',generation)),'import_profile':profiles.get(('import',generation))}
        need(bool(row['export_profile'])==bool(row['import_profile'])==r.get('profiled',True),'Unexpected/missing profiler records')
        if timing.get('wire_version')==3 and index>0:
            need(timing['connection_prepare_ns']==0,'Warm handoff reopened peer connection')
        hops.append(row)
    expected_apis=2*len(hops)
    combined=[x for x in r['negative_apis'] if x['kind']=='parked-combined-send']
    if combined or r.get('parked_combined_action_test'):
        need(len(combined)==len(hops),'Incomplete combined API fencing test')
        expected_apis+=len(hops)
    need(len(r['negative_apis'])==expected_apis,'Missing parked-owner rejection checks')
    for row in r['negative_apis']:
        need(receipt_record(row,'LAB_API_JSON ')['status']==400,'Parked owner operation accepted')
    servers=r['preparation'].get('servers',{})
    if servers.get('B',{}).get('peer_ready') is not None:
        server=servers['B'];peer=server['peer_ready']
        need(records(read(server['file']),'RESIDENT_PEER_READY_JSON ')==[peer] and
             peer['owner_changed'] is False and 0<peer['connection_prepare_ns']<=server['native_boot']['native_ready_ns'],
             'Peer connection preparation changed')
        need(all(h['timing']['connection_prepare_ns']==0 for h in r['hops']),
             'Prepared peer reconnected during handoff')
    boot=r['preparation']['worker']['native_boot']
    need(records(read(r['preparation']['worker']['native_boot_file']),'RESIDENT_BOOT_JSON ')==[boot],'Preparation summary changed')
    if r['arm'] in ('none','nfs-unprepared'):
        need(not prepares and boot['disk_prepared'] is False and boot['prepare_api_ns']==0,'Unexpected preprepared disk')
    else:need(len(prepares)==1 and prepares[0]['role']=='B' and boot['prepare_api_ns']>0,'Disk not preprepared')
    need(len(disk_profiles)==(0 if r['arm']=='none' or not r.get('profiled',True) else 2),'Disk properties reopened on warm handoff')
    need(logs['B'].count('DIRECT_DISK_PREPARED consumed=true')==(r['arm'] in ('local','nfs')),'Prepared backend not consumed exactly once')
    need(bool(r['backend_reads'])==(r['arm']!='none'),'Missing/unexpected disk I/O validation')
    for row in r['backend_reads']:
        value=row['receipt'];raw=read(row['file'])
        need(any(json.loads(l)==value for l in raw.splitlines() if l.startswith('{"event":')),'Disk read summary changed')
        need(value['read_ios']>0 and value['hash']==r['fixture']['backend_prefix_fnv'],'Disk data mismatch')
    warm=hops[1:]
    return {'arm':r['arm'],'result':'passed','hops':hops,'preparation':r['preparation'],
        'prepares':prepares,'disk_profiles':disk_profiles,'first':hops[0],
        'inline_guest_ack':r.get('inline_guest_ack',True),'profiled':r.get('profiled',True),
        'warm':{k:stats([h[k] for h in warm]) for k in ('request_resume_ms','request_complete_ms','external_command_ms','export_attach_api_ms','kernel_ms')},
        'to_b':{k:stats([h[k] for h in warm if h['destination']=='B']) for k in ('request_resume_ms','export_attach_api_ms','kernel_ms')},
        'source_fault_probes':len(r['source_probes']),'parked_rejections':expected_apis}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('suite',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r=json.loads(a.suite.read_text());need(r['result']=='passed','Unpassed suite')
    verify_sources(r, a.suite.parent, hashes_key='sources', legacy_directory='sources')
    results=[]
    for row in r['trials']:
        need(row['result']=='passed' and row['fenced'] and row['network_removed'] and row['memory_removed'],'Unfinished trial/cleanup')
        results.append({'id':row['id'],**trial(a.suite.parent/row['id']/'report.json')})
    output={'result':'passed','trials':results}
    a.output.write_text(json.dumps(output,indent=2)+'\n')
    print(json.dumps([{'id':x['id'],'warm':x['warm'],'first_resume_ms':x['first']['request_resume_ms'],
        'native_prepare_ms':x['preparation']['worker']['native_boot']['native_ready_ns']/1e6} for x in results],indent=2))


if __name__=='__main__':main()
