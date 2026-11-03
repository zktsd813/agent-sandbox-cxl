"""Resident handoff control without per-transition diagnostic work.

Offline verifiers consume the saved reply and final workload output.
"""
import secrets
import time

import lab
import nfs_bench as nfs
from home_storage_bench import require, json_lines


def request(c,source,directory,nonce,first,label,peer,vm_id,epoch,start,size,kind,observe=False):
    vm=c.vms[source]
    if 3 in vm.get('control_rpc_operations',[]) and not observe and not getattr(c.args,'exec_request',False):
        import resident_proxy
        started=time.monotonic_ns()
        require(lab.alive(vm),'VM is not running')
        raw=resident_proxy.request(vm,directory,nonce,first,label,peer,vm_id,epoch,start,size)
        name=c.raw(kind,raw)
        c.control_timings.append({'role':source,'kind':kind,'file':name,
            'elapsed_ns':time.monotonic_ns()-started,'rpc':dict(vm['_last_control_timing']),
            'request_transport':'relay'})
        return raw,name
    source_ip='172.30.0.1' if source=='A' else '172.30.0.2'
    result=c.shell(source,f'/lab/resident-control request {directory} {source_ip} {nonce} '
        f'{int(first)} {label} {peer} {vm_id} {epoch} {start} {size}'+(' 1' if observe else ''),kind,120)
    c.control_timings[-1]['request_transport']='exec'
    return result


def prepare_servers(c):
    if not hasattr(c,'resident_servers'):
        c.resident_servers = {}
        c.resident_control_nonce = secrets.token_hex(16)
    started=time.monotonic_ns()
    for role in ('A','B'):
        worker=c.resident_workers[role]
        if role in c.resident_servers: continue
        directory=worker['directory'];control=directory+'/control'
        ip='172.30.0.1' if role=='A' else '172.30.0.2'
        if getattr(c.args,'resident_ready_control',False):
            command=f'/lab/resident-control launch {directory} {ip} {c.resident_control_nonce}'
            if role == 'B':
                command += ' 172.30.0.1'
                command += f'\ncat {control}/log'
        else:
            command=(f'mkdir -p {control}\n'
                f'/lab/resident-control serve {directory} {ip} {c.resident_control_nonce} > {control}/log 2>&1 &\n'
                + nfs.record_pid(control)
                +f'n=0\nwhile ! grep -q RESIDENT_CONTROL_READY {control}/log; do\n'
                 f'read p t < {control}/fc.pid\nkill -0 $p\nn=$((n+1))\n[ $n -lt 500 ]\nsleep .01\ndone\n'
                 ':')
        raw,name=c.shell(role,command,'resident-controller-prepare')
        c.resident_servers[role]={'directory':control,'file':name}
        if getattr(c.args,'resident_ready_control',False):
            boots=json_lines(raw,'RESIDENT_SERVER_BOOT_JSON ')
            require(len(boots)==1,'Native controller readiness missing')
            c.resident_servers[role]['native_boot']=boots[0]
            if role == 'B':
                peers=json_lines(raw,'RESIDENT_PEER_READY_JSON ')
                require(len(peers)==1 and peers[0]['connection_prepare_ns']>0 and peers[0]['owner_changed'] is False,
                        'Peer preparation did not complete')
                c.resident_servers[role]['peer_ready']=peers[0]
    return time.monotonic_ns()-started


def prepare(c,s):
    """Explicitly prepare both endpoints after promotion, before a request exists."""
    started = time.monotonic_ns()
    require(s.role == 'A' and s.tier == 'cxl' and not c.resident_readiness,
            'Readiness must be established once after initial promotion')
    cold = c.prepare_resident_target(s,'B')
    cold['phase'] = 'post-promotion-prewarm'
    cold['control_server_prepare_ns'] = prepare_servers(c)
    cold['servers'] = {role:dict(server) for role,server in c.resident_servers.items()}
    c.resident_readiness.update({
        'A': {'directory':s.directory, 'ready_file':c.resident_servers['A']['file'], 'kind':'booted-owner'},
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
    """Transfer ownership without guest probes or post-handoff audits.

    Correctness experiments observe the guest separately. The runtime returns
    when the destination has resumed; it does not collect manifests or logs.
    """
    ready = getattr(c.args,'resident_ready_control',False)
    preparation=prepared_target(c,s,destination) if ready else c.prepare_resident_target(s,destination)
    preparation['control_server_prepare_ns']=0 if ready else prepare_servers(c)
    source,old_dir,old_epoch=s.role,s.directory,s.epoch
    worker=c.resident_workers[destination]
    target=worker['directory'];new_epoch=old_epoch+1
    c.serial_number+=1
    label=f'v{s.vm}e{new_epoch}c{c.serial_number}'
    target_ip='172.30.0.1' if destination=='A' else '172.30.0.2'
    source_ip='172.30.0.1' if source=='A' else '172.30.0.2'
    command=(f'/lab/resident-control {"request" if ready else "handoff"} {old_dir} '
        f'{source_ip if ready else target_ip} {c.resident_control_nonce} '
        f'{int(not worker["imported"])} {label}')
    if ready:
        command += f' {target_ip} {s.vm} {new_epoch} {c.pool_offset} {c.image_bytes}'
    control_start=time.monotonic_ns()
    if getattr(c.args,'alternate_request',False):
        c.args.exec_request=bool(((new_epoch-1)//2)%2)
    if ready:
        raw,fast_file=request(c,source,old_dir,c.resident_control_nonce,not worker['imported'],label,
            target_ip,s.vm,new_epoch,c.pool_offset,c.image_bytes,'resident-fast-run')
    else:
        raw,fast_file=c.shell(source,command,'resident-fast-run',120)
    controller_ns=time.monotonic_ns()-control_start
    controller_rpc=c.control_timings[-1].get('rpc')
    values=json_lines(raw,'RESIDENT_FAST_JSON ')
    require(len(values)==1 and values[0]['version']==2,'Missing handoff result')
    fast=values[0]
    require(set(fast['apis'])=={'send','receive'} and
            all(a['status']==204 for a in fast['apis'].values()),'Handoff failed')
    worker['imported']=True
    s.role,s.epoch,s.manifest=destination,new_epoch,None
    for key in ('directory','plan','report','manifest_path','hints'): setattr(s,key,worker[key])
    row={'kind':'direct','runtime_path':'resident-v2','resident':True,'fast_control':True,
         'ready_control':ready,'fast_file':fast_file,'fast':fast,
         'vm':s.vm,'source':source,'destination':destination,'source_epoch':old_epoch,'destination_epoch':s.epoch,
         'source_tier':'cxl','destination_tier':'cxl','source_directory':old_dir,'destination_directory':target,
         'preparation':preparation,'controller_command_wall_ns':controller_ns,
         'controller_rpc':controller_rpc,'result':'passed'}
    row['request_transport']=c.control_timings[-1].get('request_transport','legacy')
    c.transitions.append(row)
    return row
