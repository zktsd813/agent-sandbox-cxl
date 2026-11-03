#!/usr/bin/env python3
"""Separate resident preparation, client API service, and instrumented pause-to-ACK.

Use the independent SWE verifier first. No cold/first visits are discarded.
The serial diagnostic controller interval is not a production handoff latency.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def need(ok, message):
    if not ok:
        raise ValueError(message)


def stats(values):
    values = sorted(values)
    return {'n':len(values), 'mean':statistics.mean(values), 'p50':statistics.median(values),
            'min':values[0], 'max':values[-1]}


def analyze(path):
    r = json.loads(path.read_bytes())
    need(r['result']=='passed' and r['arguments'].get('resident_direct'), 'Passed resident report required')
    receipts = {x['file']:x for x in r['receipts']}
    def read(name):
        raw = (path.parent/name).read_bytes()
        need(hashlib.sha256(raw).hexdigest()==receipts[name]['sha256'], 'Changed receipt '+name)
        return raw.decode()
    profiles, preparations = {}, []
    for launch in r['launches']:
        logs = [x for x in receipts.values() if x['kind']=='final-vmm-log'
                and x['integrity']['path']==launch['directory']+'/vmm.log']
        need(len(logs)==1, 'Missing final log')
        last = None
        for line in read(logs[0]['file']).splitlines():
            if 'DIRECT_HANDOFF_JSON ' in line:
                last = json.loads(line.split('DIRECT_HANDOFF_JSON ',1)[1])
            if 'DIRECT_RESIDENT_JSON ' in line:
                value = json.loads(line.split('DIRECT_RESIDENT_JSON ',1)[1])
                if value['event']=='prepare': preparations.append(dict(value,role=launch['role']))
            if 'RESTORE_TIMING ' in line:
                t = json.loads(line.split('RESTORE_TIMING ',1)[1])
                if t['scope'] not in ('direct_export','direct_import','direct_resident_import'): continue
                need(last is not None, 'Timing lacks generation')
                key = (last['event'],last['token']['generation'])
                need(key not in profiles, 'Duplicate operation profile')
                profiles[key] = t
    rows=[]
    for hop in r['transitions']:
        if not hop.get('resident'): continue
        for a in (hop['export_api'],hop['attach_api'],hop['preparation']['api']):
            if a is None: continue
            raw=read(a['file'])
            api=[json.loads(line.split('LAB_API_JSON ',1)[1]) for line in raw.splitlines()
                 if line.startswith('LAB_API_JSON ')]
            need(api==[a['response']], 'Client API changed')
        api_us=sum(hop[k]['response']['duration_us'] for k in ('export_api','attach_api'))
        pause=[a for a in hop['critical_apis'] if a['path']=='/vm' and a['request']=={'state':'Paused'}]
        resume=[a for a in hop['critical_apis'] if a['path']=='/vm' and a['request']=={'state':'Resumed'}]
        need(len(pause)==len(resume)==1,'Pause/resume evidence missing')
        kernel_us=(hop['token']['kernel_ns']+hop['import_event']['token']['kernel_ns'])/1000
        row={'source':hop['source'],'destination':hop['destination'],'generation':hop['token']['generation'],
             'allocation_generation':hop['destination_placement']['manifest']['allocation_generation']
                 if 'manifest' in hop['destination_placement'] else json.loads(read(hop['destination_placement']['file']))['allocation_generation'],
             'resident_import':hop['import_event'].get('resident',False), 'control_path':'tcp' if hop.get('fast_control') else 'serial-diagnostic',
             'prepare_fresh':hop['preparation']['fresh'],
             'prepare_api_us':hop['preparation']['api']['response']['duration_us'] if hop['preparation']['api'] else 0,
             'prepare_controller_us':hop['preparation']['controller_wall_ns']/1000,
             'export_api_us':hop['export_api']['response']['duration_us'],
             'import_api_us':hop['attach_api']['response']['duration_us'],
             'export_import_api_us':api_us,'kernel_us':kernel_us,
             'pause_export_import_resume_api_us':api_us+pause[0]['response']['duration_us']+resume[0]['response']['duration_us'],
             'pause_to_ack_us':hop['controller_pause_to_ack_ns']/1000}
        if hop.get('ready_control'):
            row.update({k:hop['fast'][k.replace('_us','_ns')]/1000 for k in
                ('request_us','request_to_resume_response_us','pause_to_resume_response_us')})
        if profiles:
            g=hop['token']['generation'];parts={}
            for event in ('export','import'):
                profile=profiles[(event,g)]
                need(profile['total_us']>=sum(profile['stages_us'].values())-.1,'Stage overlap')
                parts.update({event+'.'+k:v for k,v in profile['stages_us'].items()})
            parts['export.export_detach_ioctls']-=hop['token']['kernel_ns']/1000
            attach_key='import.attach' if row['resident_import'] else 'import.attach_and_vm_wrapper'
            parts[attach_key]-=hop['import_event']['token']['kernel_ns']/1000
            parts['kernel.detach_attach']=kernel_us
            parts['api_dispatch_response_profile_residual']=api_us-sum(parts.values())
            need(all(v>=-.05 for v in parts.values()) and abs(sum(parts.values())-api_us)<.05,'Overlapping profile')
            row['exclusive_components_us']=parts
        rows.append(row)
    metrics=('prepare_api_us','prepare_controller_us','export_import_api_us','kernel_us',
             'pause_export_import_resume_api_us','pause_to_ack_us')
    def summary(group):
        return {k:stats([x[k] for x in group if k in x]) for k in (*metrics,
            'request_us','request_to_resume_response_us','pause_to_resume_response_us') if any(k in x for x in group)}
    return {'schema':'resident-direct-profile-v1','report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'rows':rows,'preparation_vmm':preparations,'preparation_controller':r.get('resident_preparations',[]),'all':summary(rows),
            'first_import':summary([x for x in rows if not x['resident_import']]),
            'resident_imports':summary([x for x in rows if x['resident_import']]),
            'by_control_path':{kind:summary([x for x in rows if x['control_path']==kind]) for kind in sorted({x['control_path'] for x in rows})},
            'notes':['No first visits or warmup samples discarded.',
                     'Export/import API excludes pause/resume, controller metadata transport and guest ACK.',
                     'The four-API sum includes pause/resume, but still excludes metadata transport and ACK.',
                     'Serial-diagnostic pause-to-ACK includes receipt checks/rejection tests. TCP pause-to-ACK is measured on the source host across pause, export, metadata transport, attach, checkpoint, resume and the remote guest ACK response.',
                     'Preparation occurs while source is running; fresh preparation includes process launch in controller time.',
                     'Ready-control preparation occurs once after promotion. Cached readiness issues no API; parked RAM/KVM access is still probed after each handoff.',
                     'Request timings include the local control connection and peer connection. Resume response means the VMM accepted Resume, not first guest execution; serial guest ACK remains separately measured.',
                     'Single coherent host with nested KVM; not a noncoherent multi-host CXL result.']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('report',type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();result=analyze(a.report)
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['all'],indent=2))


if __name__=='__main__': main()
