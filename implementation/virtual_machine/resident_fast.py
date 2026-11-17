"""Resident handoff control without per-transition diagnostic work.

Offline verifiers consume the saved reply and final workload output.
"""
import secrets
import time

import lab
import nfs_bench as nfs
from handoff_bench import require, json_lines


def request(c,source,directory,nonce,first,label,peer,vm_id,epoch,start,size,kind):
    vm=c.vms[source]
    if 3 in vm.get('control_rpc_operations',[]) and not getattr(c.args, 'resident_bench', None):
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
        f'{int(first)} {label} {peer} {vm_id} {epoch} {start} {size}',kind,120)
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
        command=f'/lab/resident-control launch {directory} {ip} {c.resident_control_nonce}'
        if role == 'B':
            command += ' 172.30.0.1'
            command += f'\ncat {control}/log'
        if getattr(c.args,'restore_timing',False):
            command='export FC_RESIDENT_PROFILE=1\n'+command
        if hasattr(c, 'bench_offset'):
            if getattr(c.args,'noncoherent',False): command='export RESIDENT_BENCH_TRACKED_EPOCH=1\n'+command
            command = (f'export RESIDENT_BENCH_HEARTBEAT={c.bench_offset} RESIDENT_BENCH_ARENA={c.kvm_arena+16*1024*1024}\n'
                + ('export RESIDENT_BENCH_PROFILE=1\n' if c.args.profile else '')
                + ('export RESIDENT_BENCH_RESUME_PROFILE=1\n' if c.args.resume_profile else '') + command)
        raw,name=c.shell(role,command,'resident-controller-prepare')
        c.resident_servers[role]={'directory':control,'file':name}
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
    require(s.role in ('A','B') and s.tier == 'cxl' and not c.resident_readiness,
            'Readiness must be established once after initial promotion')
    destination='B' if s.role=='A' else 'A'
    cold = c.prepare_resident_target(s,destination)
    cold['phase'] = 'post-promotion-prewarm'
    cold['control_server_prepare_ns'] = prepare_servers(c)
    cold['servers'] = {role:dict(server) for role,server in c.resident_servers.items()}
    c.resident_readiness.update({
        s.role: {'directory':s.directory, 'ready_file':c.resident_servers[s.role]['file'], 'kind':'booted-owner'},
        destination: {'directory':cold['directory'], 'ready_file':cold['api']['file'], 'kind':'native-prepared'}})
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
    preparation=prepared_target(c,s,destination)
    preparation['control_server_prepare_ns']=0
    source,old_dir,old_epoch=s.role,s.directory,s.epoch
    worker=c.resident_workers[destination]
    target=worker['directory'];new_epoch=old_epoch+1
    c.serial_number+=1
    label=f'v{s.vm}e{new_epoch}c{c.serial_number}'
    target_ip='172.30.0.1' if destination=='A' else '172.30.0.2'
    control_start=time.monotonic_ns()
    raw,fast_file=request(c,source,old_dir,c.resident_control_nonce,not worker['imported'],label,
        target_ip,s.vm,new_epoch,0,0,'resident-fast-run')
    controller_ns=time.monotonic_ns()-control_start
    controller_rpc=c.control_timings[-1].get('rpc')
    values=json_lines(raw,'RESIDENT_FAST_JSON ')
    require(len(values)==1 and values[0]['version']==2,'Missing handoff result')
    fast=values[0]
    require(set(fast['apis'])=={'send','receive'} and
            all(a['status']==204 for a in fast['apis'].values()),'Handoff failed')
    worker['imported']=True
    s.role,s.epoch,s.manifest=destination,new_epoch,None
    for key in ('directory','plan','report','manifest_path'): setattr(s,key,worker[key])
    row={'kind':'direct','runtime_path':'resident-v2','resident':True,'fast_control':True,
         'ready_control':True,'fast_file':fast_file,'fast':fast,
         'vm':s.vm,'source':source,'destination':destination,'source_epoch':old_epoch,'destination_epoch':s.epoch,
         'source_tier':'cxl','destination_tier':'cxl','source_directory':old_dir,'destination_directory':target,
         'preparation':preparation,'controller_command_wall_ns':controller_ns,
         'controller_rpc':controller_rpc,'result':'passed'}
    row['request_transport']=c.control_timings[-1].get('request_transport','legacy')
    c.transitions.append(row)
    return row


def maintain(c,s):
    """Clean on the owner VMM thread, then notify the parked observer."""
    require(c.args.noncoherent and s.tier=='cxl', 'Maintenance requires shared tracked owner')
    directory=c.resident_workers[s.role]['directory']
    ip='172.30.0.1' if s.role=='A' else '172.30.0.2'
    started=time.monotonic_ns()
    raw,name=c.shell(s.role,f'/lab/resident-control maintain {directory} {ip} {c.resident_control_nonce} '
        f'{int(c.args.clean_paused)}','noncoherent-maintain',180)
    values=json_lines(raw,'NONCOHERENT_MAINT_JSON ')
    require(len(values)==1,'Maintenance acknowledgement missing')
    return dict(role=s.role,epoch=s.epoch,file=name,host_duration_ns=time.monotonic_ns()-started,**values[0])


def admin(c,role,operation,*arguments):
    directory=c.resident_workers[role]['directory']
    ip='172.30.0.1' if role=='A' else '172.30.0.2'
    raw,name=c.shell(role,f'/lab/resident-control {operation} {directory} {ip} {c.resident_control_nonce} '+
        ' '.join(map(str,arguments)),'noncoherent-'+operation,180)
    prefix={'cache':'NONCOHERENT_CACHE_JSON ','arena':'NONCOHERENT_ARENA_JSON '}.get(operation,'NONCOHERENT_ADMIN_JSON ')
    values=json_lines(raw,prefix);require(len(values)==1,'Administrative receipt missing')
    return dict(role=role,file=name,**values[0])


def invalidate_grant(c,s,manifest):
    handle=manifest['handle'];acks=[]
    for role in ('A','B'):
        receipt=admin(c,role,'cache',handle['metadata_offset'],handle['cookie_lo'],handle['cookie_hi'],handle['count'],'invalidate')
        require(receipt['pages']==handle['count'] and receipt['lines']==handle['count']*64,'Full grant invalidation incomplete')
        acks.append(role)
    require(set(acks)=={'A','B'},'Missing invalidation acknowledgement')
    admin(c,s.role,'cache',handle['metadata_offset'],handle['cookie_lo'],handle['cookie_hi'],handle['count'],'zero')
    return acks


def audit_parked(c,role):
    result=admin(c,role,'audit')
    script='for i in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do echo $i > /sys/module/kvm/parameters/dax_audit_index; printf "PATH %s " "$i"; cat /sys/module/kvm/parameters/dax_nonowner_count; done\ncat /sys/module/pool_memory_module/parameters/nonowner_counts'
    raw,name=c.shell(role,script,'nonowner-counters')
    import re
    counts={int(k):int(v) for k,v in re.findall(r'^PATH (\d+) (\d+)$',raw,re.M)}
    require(len(counts)==16 and counts[9]>0 and counts[10]>0,'Parked ioctl/mmap guards not exercised')
    require(all(counts[k]==0 for k in (0,1,2,3,5,6,7,8,14)),'Unexpected nonowner asynchronous or reference access')
    return dict(result,counters=counts,counter_file=name)


def reuse_after(c,s,args,out):
    """Destroy VM1, collect A+B ACKs, start VM2 on B in the same extents."""
    import json,re,struct
    from pathlib import Path
    from dirty_oracle import DirtyOracle,MIB
    from handoff_bench import HandoffController,source_fence_script
    handle=c.fetch_json('A',c.resident_workers['A']['manifest_path'],'retiring-manifest')['handle']
    old_frames=set(c.allocator.grants[s.owner].frames)
    raw,_=c.shell(s.role,'cat /sys/module/kvm/parameters/dax_pool','retiring-arena')
    offset=int(re.search(r'vm_base=(\d+)',raw)[1])
    fences=[]
    for role,worker in c.resident_workers.items():
        # The only lab holders of VM/vCPU fds are these VMMs; controllers hold
        # pidfds and Unix sockets only. Close every wrapper before arena reuse.
        for directory in (worker['directory']+'/control',worker['directory']):
            script=f'read p t < {directory}/fc.pid\nkill -KILL "$p" 2>/dev/null || true\n'
            script+='n=0; while [ -e /proc/$p/stat ]; do state=$(cat /proc/$p/stat); case "$state" in *") Z "*|*") X "*) break;; esac; n=$((n+1)); [ $n -lt 500 ]; sleep .01; done\n'
            raw,name=c.shell(role,script+source_fence_script(directory),'retired-wrapper-fence')
            require('FENCED ' in raw,'Old wrapper fd lifetime not fenced')
            fences.append(dict(role=role,directory=directory,file=name))
    object_acks=[admin(c,role,'arena',offset,1,0) for role in ('A','B')]
    require(all(r['capacity']==16*MIB and r['lines']==16*MIB//64 for r in object_acks),'Object invalidation ACK incomplete')
    directory=c.resident_workers['B']['directory']
    raw,partial_ack_file=c.shell('B',f'if /lab/resident-control arena {directory} 172.30.0.2 '
        f'{c.resident_control_nonce} {offset} 2 1; then exit 1; else echo REUSE_ACK_REFUSED; fi',
        'partial-reuse-ack')
    require('REUSE_ACK_REFUSED' in raw,'Kernel accepted an incomplete reuse ACK set')
    ram_acks=invalidate_grant(c,s,{'handle':handle})
    freed=c.allocator.release(s.owner,fenced=True,detached=True,invalidated_hosts=ram_acks)
    require(set(freed)==old_frames,'Released another VM grant')
    import array,subprocess
    zero_page=out/'zero-page';zero_page.write_bytes(bytes(4096))
    zero_hash=out/'zero.hash';ram_hash=out/'reuse-zero.hashes'
    try:
        subprocess.run([str(args.page_hash),str(zero_page),str(zero_hash)],check=True)
        expected=zero_hash.read_bytes()
        subprocess.run([str(args.page_hash),str(c.config['cxl_backing']),str(ram_hash)],check=True)
        hashes=ram_hash.read_bytes()
        require(all(hashes[(frame//4096)*8:(frame//4096+1)*8]==expected for frame in old_frames),'Reused RAM is not all zero')
    finally:
        for path in (zero_page,zero_hash,ram_hash):path.unlink(missing_ok=True)
    arm=admin(c,'B','arena',offset,2,3)
    # This is the L0-only hash gate, not the CXL ownership protocol.
    with Path(c.config['cxl_backing']).open('r+b',buffering=0) as stream:
        stream.seek(handle['metadata_offset']+40*MIB);stream.write(bytes(256))
    destination=out/'reuse-vm2';destination.mkdir()
    c2=HandoffController(c.config,destination,args);c2.allocator.generation=c.allocator.generation
    s2=c2.sandboxes[0];s2.role='B'
    for role in ('A','B'):c2.shell(role,f'mkdir -p {c2.root}','reuse-root')
    oracle=DirtyOracle(c.config,destination,c2.metadata_base+s2.vm*64*MIB,c2.kvm_arena,args.page_hash)
    report=dict(result='running',arguments=vars(args),config=c.config,transitions=c2.transitions,receipts=c2.receipts)
    parent=json.loads((out/'report.json').read_text())
    for key in ('source_hashes','source_reference'): report[key]=parent[key]
    try:
        report['boot']=c2.boot_vm(s2)
        report['heartbeat']=c2.trace_command(s2,'heartbeat',('dirty'+str(args.dirty_mib)) if args.resident_bench=='dirty' else args.resident_bench,event='trace_heartbeat')
        time.sleep(.2);c2.promote(s2)
        require(set(c2.allocator.grants[s2.owner].frames)==old_frames,'VM2 did not reuse exact RAM frames')
        current=c2.fetch_json('B',s2.manifest_path,'new-manifest')['handle']
        require((current['cookie_lo'],current['cookie_hi'])!=(handle['cookie_lo'],handle['cookie_hi']) and
            current['allocation_generation']>handle['allocation_generation'],'VM2 allocation identity was reused')
        c2.bench_offset=c.bench_offset
        prepare(c2,s2)
        for _ in range(6):
            time.sleep(.03)
            hop=c2.direct_handoff(s2,'A' if s2.role=='B' else 'B')
            require(hop['fast']['ack']['verified']==s2.epoch,'VM2 guest verification failed')
            report.setdefault('audits',[]).append(audit_parked(c2,hop['source']))
        raw,name=c2.shell(s2.role,'cat /sys/module/kvm/parameters/dax_pool','new-arena')
        require(int(re.search(r'vm_base=(\d+)',raw)[1])==offset,'VM2 did not reuse object arena')
        report['arena_file']=name
        admin(c2,s2.role,'retire')
        report['result']='passed'
    except BaseException as error:
        report.update(result='failed',error=repr(error));raise
    finally:
        oracle.close();report['dirty_oracle']=oracle.rows
        for role,worker in getattr(c2,'resident_workers',{}).items():
            for name in ('fc.log','vmm.log','stderr.log'):
                try:c2.fetch_bytes(role,worker['directory']+'/'+name,'final-'+role+'-'+name)
                except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
        lab.save_json(destination/'report.json',report)
    return dict(result=report['result'],first_host='B',same_ram_frames=True,same_object_arena=True,
        object_offset=offset,object_capacity=16*MIB,object_acks=object_acks,ram_acks=ram_acks,
        zero_pages_verified=len(old_frames),partial_ack_refused=True,partial_ack_file=partial_ack_file,
        fences=fences,arm=arm,vm2_report='reuse-vm2/report.json',vm2_handoffs=6,
        old_cookie=[handle['cookie_lo'],handle['cookie_hi']],new_cookie=[current['cookie_lo'],current['cookie_hi']])
