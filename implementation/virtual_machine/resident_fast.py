"""Evidence adapter for the bounded TCP handoff coordinator.

The measured C interval finishes before Python fetches or verifies receipts.
API records below are extracted from one authenticated coordinator result;
verify_swe_pool binds them back to that original record and full VMM/guest logs.
"""
import hashlib
import json
import secrets
import shlex
import time

import nfs_bench as nfs
from home_storage_bench import require, json_lines
from pool_handoff_control import validate_export, validate_handoff_handle


def prepare_servers(c):
    if not hasattr(c,'resident_servers'):
        c.resident_servers = {}
        c.resident_control_nonce = secrets.token_hex(16)
    started=time.monotonic_ns()
    for role, worker in c.resident_workers.items():
        if role in c.resident_servers: continue
        directory=worker['directory'];control=directory+'/control'
        ip='172.30.0.1' if role=='A' else '172.30.0.2'
        if getattr(c.args,'resident_ready_control',False):
            command=f'/lab/resident-control launch {directory} {ip} {c.resident_control_nonce}\nsha256sum /lab/resident-control'
        else:
            command=(f'mkdir -p {control}\n'
                f'/lab/resident-control serve {directory} {ip} {c.resident_control_nonce} > {control}/log 2>&1 &\n'
                + nfs.record_pid(control)
                +f'n=0\nwhile ! grep -q RESIDENT_CONTROL_READY {control}/log; do\n'
                 f'read p t < {control}/fc.pid\nkill -0 $p\nn=$((n+1))\n[ $n -lt 500 ]\nsleep .01\ndone\n'
                 'sha256sum /lab/resident-control')
        raw,name=c.shell(role,command,'resident-controller-prepare')
        expected=c.fixture['artifacts']['resident-control']
        require(expected+'  /lab/resident-control' in raw,'Wrong TCP coordinator binary')
        c.resident_servers[role]={'directory':control,'file':name,'sha256':expected}
        if getattr(c.args,'resident_ready_control',False):
            boots=json_lines(raw,'RESIDENT_SERVER_BOOT_JSON ')
            require(len(boots)==1,'Native controller readiness missing')
            c.resident_servers[role]['native_boot']=boots[0]
    return time.monotonic_ns()-started


def prepare(c,s):
    """Explicitly prepare both endpoints after promotion, before a request exists."""
    started = time.monotonic_ns()
    require(s.role == 'A' and s.tier == 'cxl' and not c.resident_readiness,
            'Readiness must be established once after initial promotion')
    active = c.resident_resources(s.role,s.directory,s.epoch,'active')
    cold = c.prepare_resident_target(s,'B')
    cold['phase'] = 'post-promotion-prewarm'
    cold['control_server_prepare_ns'] = prepare_servers(c)
    cold['servers'] = {role:dict(server) for role,server in c.resident_servers.items()}
    c.resident_readiness.update({
        'A': {'directory':s.directory, 'ready_file':active['file'], 'kind':'booted-owner'},
        'B': {'directory':cold['directory'], 'ready_file':cold['api']['file'], 'kind':'native-prepared'}})
    cold['full_prewarm_controller_ns'] = time.monotonic_ns()-started


def prepared_target(c,s,destination):
    started = time.monotonic_ns()
    require(set(c.resident_readiness) == set(c.resident_servers) == {'A','B'}, 'Endpoints are not ready')
    ready = c.resident_readiness[destination]
    require(ready['directory'] == c.resident_workers[destination]['directory'], 'Ready process was replaced')
    return {'source':s.role, 'destination':destination, 'before_epoch':s.epoch,
        'fresh':False, 'cached':True, 'api':None, 'source_paused':False,
        'directory':ready['directory'], 'ready_file':ready['ready_file'],
        'controller_wall_ns':time.monotonic_ns()-started}


def handoff(c,s,destination):
    if not c.resident_resource_events:
        c.resident_resources(s.role,s.directory,s.epoch,'active')
    ready = getattr(c.args,'resident_ready_control',False)
    preparation=prepared_target(c,s,destination) if ready else c.prepare_resident_target(s,destination)
    preparation['control_server_prepare_ns']=0 if ready else prepare_servers(c)
    source,old_dir,old_epoch=s.role,s.directory,s.epoch
    worker=c.resident_workers[destination]
    target=worker['directory'];new_epoch=old_epoch+1
    # Only the plan's epoch is known in advance; incoming queue/ownership state
    # is generated after source pause and transported inside the C interval.
    plan={'operation':'export','vm_id':str(s.vm),'run_epoch':str(new_epoch),
          'pool_path':'/dev/dax0.0','canonical_start':c.pool_offset,'canonical_bytes':c.image_bytes}
    if not ready:
        c.write_json(destination,worker['plan'],plan,'resident-checkpoint-plan')
    before=c.command_vm(s,'identity',require_cache=True)
    c.serial_number+=1
    label=f'v{s.vm}e{new_epoch}c{c.serial_number}'
    target_ip='172.30.0.1' if destination=='A' else '172.30.0.2'
    action='DirectImport' if worker['imported'] else 'InstanceStart'
    control_start=time.monotonic_ns()
    source_ip='172.30.0.1' if source=='A' else '172.30.0.2'
    command=(f'/lab/resident-control {"request" if ready else "handoff"} {old_dir} '
        f'{source_ip if ready else target_ip} {c.resident_control_nonce} '
        f'{int(not worker["imported"])} {shlex.quote(label)}')
    if ready:
        command += f' {target_ip} {s.vm} {new_epoch} {c.pool_offset} {c.image_bytes}'
    raw,fast_file=c.shell(source,command,'resident-fast-run',120)
    controller_ns=time.monotonic_ns()-control_start
    values=json_lines(raw,'RESIDENT_FAST_JSON ')
    require(len(values)==1 and values[0]['version']==1,'Missing TCP handoff result')
    fast=values[0]
    require(fast['ack']['label']==label,'Wrong handoff ACK')
    worker['imported']=True
    def api(name,role,path,method,body):
        response=fast['apis'][name]
        require(response['status']==204,'Coordinator API failed')
        # Preserve the measured response exactly and record its source receipt.
        text=f'COORDINATOR_API {role} {method} {path} '+json.dumps(body)+'\nLAB_API_JSON '+json.dumps(response)+'\n'
        file=c.raw('coordinator-api',text)
        value={'file':file,'role':role,'path':path,'method':method,'request':body,'response':response,
               'coordinator_file':fast_file,'coordinator_stage':name}
        c.api_events.append(value)
        return value
    pause=api('pause',source,'/vm','PATCH',{'state':'Paused'})
    export=api('export',source,'/actions','PUT',{'action_type':'DirectExport'})
    metadata=c.fetch_bytes(source,old_dir+'/direct-export.json','direct-device-state')
    metadata_file=c.receipts[-1]['file'];state=json.loads(metadata);token=state['token']
    manifest=c.fetch_bytes(source,s.manifest_path,'direct-placement')
    manifest_file=c.receipts[-1]['file'];parsed=json.loads(manifest)
    require(len(metadata)==fast['state_bytes'] and len(manifest)==fast['manifest_bytes']
            and hashlib.sha256(manifest).hexdigest()==state['memory_manifest_sha256'],'Coordinator payload changed')
    accounting=validate_export(c.allocator,s.owner,parsed,c.memory_mib*1024*1024,c.pool_offset,c.image_bytes)
    fence=c.resident_fence(source,old_dir,token['generation'])
    attach=api('attach',destination,'/actions','PUT',{'action_type':action})
    checkpoint=api('checkpoint',destination,'/actions','PUT',{'action_type':'PoolCache'})
    s.role,s.epoch=destination,new_epoch
    for key in ('directory','plan','report','manifest_path','hints'): setattr(s,key,worker[key])
    restored_manifest=c.fetch_json(destination,s.manifest_path,'memory-manifest')
    restored_file=c.receipts[-1]['file']
    validate_handoff_handle(parsed,restored_manifest)
    restored={'file':restored_file,'accounting':validate_export(c.allocator,s.owner,restored_manifest,
        c.memory_mib*1024*1024,c.pool_offset,c.image_bytes)}
    s.manifest=restored_manifest
    log=c.fetch_bytes(destination,target+'/vmm.log','resident-full-log').decode()
    imports=[json.loads(line.split('DIRECT_HANDOFF_JSON ',1)[1]) for line in log.splitlines()
             if 'DIRECT_HANDOFF_JSON ' in line]
    imports=[r for r in imports if r['event']=='import' and r['token']['generation']==token['generation']]
    require(len(imports)==1,'Coordinator import evidence absent')
    imported=imports[0];c.raw('direct-import-log','DIRECT_HANDOFF_JSON '+json.dumps(imported)+'\n')
    retained=('vm_offset','vcpu_offset','vmcs_offset','run_offset','ept_root_offset','ram_hva','ram_bytes')
    previous=getattr(s,'direct_token',None)
    if previous:
        require(all(token[k]==previous[k] for k in retained) and token['generation']>previous['generation'],'Coordinator replaced graph')
    require(all(imported['token'][k]==token[k] for k in (*retained,'generation','vm_creates','vcpu_creates'))
            and token['vm_creates']==token['vcpu_creates']==1 and imported['token']['flags']==1
            and imported['snapshot_bytes']==imported['ram_transfer_bytes']==0,'Coordinator import changed identity')
    s.direct_token=token
    resume=api('resume',destination,'/vm','PATCH',{'state':'Resumed'})
    ack_file=c.raw('coordinator-guest-ack',json.dumps(fast['ack'],separators=(',',':'))+'\n')
    ping={'file':ack_file,'receipt':fast['ack'],'vm':s.vm,'role':s.role,'tier':s.tier,'epoch':s.epoch,
           'operation':'ping','arguments':[],'coordinator_file':fast_file}
    c.commands.append(ping)
    require(all(ping['receipt'][k]==before['receipt'][k] for k in ('boot_id','pid','next_index')), 'Guest ping identity changed')
    after=c.command_vm(s,'check',require_cache=True)
    c.same_identity(before,after)
    require(after['receipt']['block_read_ios']==0,'Coordinator handoff lost warm page cache')
    c.resident_resources(destination,target,s.epoch,'active')
    row={'kind':'direct','resident':True,'fast_control':True,'ready_control':ready,'fast_file':fast_file,'fast':fast,
         'vm':s.vm,'source':source,'destination':destination,'source_epoch':old_epoch,'destination_epoch':s.epoch,
         'source_tier':'cxl','destination_tier':'cxl','source_directory':old_dir,'destination_directory':target,
         'before':before,'after':after,'ping':ping,'source_fence_file':fence,'export_api':export,'attach_api':attach,
         'device_state_file':metadata_file,'device_state_bytes':len(metadata),'manifest_file':manifest_file,
         'manifest_bytes':len(manifest),'token':token,'import_event':imported,
         'ram_snapshot_bytes':0,'ram_transfer_bytes':0,'cpu_snapshot_bytes':0,'source_accounting':accounting,
         'destination_placement':restored,'preparation':preparation,'critical_apis':[pause,export,attach,checkpoint,resume],
         'controller_pause_to_ack_ns':fast['handoff_ns'],'controller_command_wall_ns':controller_ns,'result':'passed'}
    c.transitions.append(row)
    return row
