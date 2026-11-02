#!/usr/bin/env python3
"""Compare cold preparation and ready-service timings from verified lab receipts.

Native intervals are nested inside console intervals; never add them together.
The resume response is API acceptance, not a guest execution timestamp.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from verify_swe_pool import verify

NFS3 = ('NULL','GETATTR','SETATTR','LOOKUP','ACCESS','READLINK','READ','WRITE',
        'CREATE','MKDIR','SYMLINK','MKNOD','REMOVE','RMDIR','RENAME','LINK',
        'READDIR','READDIRPLUS','FSSTAT','FSINFO','PATHCONF','COMMIT')


def stats(values):
    return {'n':len(values),'p50':statistics.median(values),'min':min(values),
            'max':max(values),'mean':statistics.mean(values)}


def analyze(path):
    verified=verify(path)
    report=json.loads(path.read_bytes())
    receipts={r['file']:r for r in report['receipts']}
    def read(name):
        raw=(path.parent/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=receipts[name]['sha256']:
            raise ValueError('Changed receipt: '+name)
        return raw.decode()
    hops=[t for t in report['transitions'] if t.get('resident')]
    preparations=report.get('resident_preparations') or [t['preparation'] for t in hops]
    cold=next(p for p in preparations if p['fresh'])
    result={'result':verified['result'],'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'vmm_sha256':report['fixture']['inputs']['firecracker']['sha256'],
        'handoffs':len(hops),'trace_commands':len(report['commands']),
        'cold_prepare_controller_ms':cold['controller_wall_ns']/1e6,
        'cold_prepare_api_ms':cold['api']['response']['duration_us']/1000,
        'server_prepare_controller_ms':cold.get('control_server_prepare_ns',0)/1e6,
        'exclusive_control_stages_ms':[
            {'kind':s['kind'],'ms':s['elapsed_ns']/1e6} for s in cold['control_stages']],
        'separate_api_control_ms':cold['api_control_wall_ns']/1e6,
        'unattributed_control_ms':cold['unattributed_control_ns']/1e6}
    if 'native_boot' in cold:
        boot=cold['native_boot']
        before,after=boot['nfs_proc3_before'],boot['nfs_proc3_after']
        result['native_vmm_ms']={k:v/1e6 for k,v in boot.items() if k.endswith('_ns')}
        if 'servers' in cold:
            result['native_server_ms']={role:s['native_boot']['native_ready_ns']/1e6 for role,s in cold['servers'].items()}
            result['full_prewarm_controller_ms']=cold['full_prewarm_controller_ns']/1e6
    else:
        def counters(name):
            lines=[l.split() for l in read(name).splitlines() if l.startswith('proc3 22 ')]
            if len(lines)!=1: raise ValueError('Missing NFS counter evidence')
            return list(map(int,lines[0][2:]))
        before=counters(cold['nfs_counters']['before_file'])
        after=counters(cold['nfs_counters']['after_file'])
    result['prepare_nfs_rpc_delta']={k:a-b for k,a,b in zip(NFS3,after,before)}
    fast=[t for t in hops if t.get('fast_control')]
    result['fast_samples']=[{'generation':t['token']['generation'],
        'first_import':not t['import_event'].get('resident',False),
        **{k:v/1e6 for k,v in t['fast'].items() if k.endswith('_ns')}} for t in fast]
    result['fast_ms']={k[:-3]+'_ms':stats([t['fast'][k]/1e6 for t in fast])
        for k in ('handoff_ns','guest_ack_ns','connection_prepare_ns','request_ns',
                  'request_to_resume_response_ns','pause_to_resume_response_ns')
        if all(k in t['fast'] for t in fast)}
    result['fast_export_import_api_ms']=stats([sum(t[k]['response']['duration_us'] for k in ('export_api','attach_api'))/1000 for t in fast])
    result['disk_properties_profiles']=[]
    for launch in report['launches']:
        if launch['role']!='B': continue
        for receipt in report['receipts']:
            if receipt['kind']!='final-vmm-log' or receipt['integrity']['path']!=launch['directory']+'/vmm.log': continue
            for line in read(receipt['file']).splitlines():
                if 'RESTORE_TIMING ' not in line: continue
                row=json.loads(line.split('RESTORE_TIMING ',1)[1])
                if row['scope']=='disk_properties': result['disk_properties_profiles'].append(row)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline',type=Path);parser.add_argument('ready',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result={'schema':'resident-preparation-audit-v1','baseline':analyze(args.baseline),'ready':analyze(args.ready),
        'notes':['All samples, including first visits and slow guest ACKs, retained.',
                 'Native preparation intervals are inside controller wall time; do not sum overlapping clocks.',
                 'Full prewarm controller time includes console transfers, executable hash checks and resource audit.',
                 'Request includes local and peer TCP connections, metadata transport and all five VMM APIs.',
                 'Resume response is API acceptance; guest serial ACK is a separate completion milestone.',
                 'No conventional RAM migration was measured in this comparison.']}
    if result['baseline']['vmm_sha256']!=result['ready']['vmm_sha256']:
        raise ValueError('VMM binaries differ')
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:{n:v for n,v in result[k].items() if n.endswith('_ms')} for k in ('baseline','ready')},indent=2))


if __name__=='__main__': main()
