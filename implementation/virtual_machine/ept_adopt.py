#!/usr/bin/env python3
"""EPT-only prepared-worker handoff in the existing two-L1 lab."""
import argparse
import copy
import hashlib
import json
import mmap
from pathlib import Path
import struct
import shutil
import time

import lab
import nfs_bench as nfs
from handoff_bench import HandoffController, MIB, PAGE, require


def environment(c, s):
    return dict(FC_EPT_ADOPT='1',
        FC_EPT_PHYS=str(c.ept_phys[s.role]+c.kvm_arena), FC_EPT_OFFSET=str(c.kvm_arena),
        FC_EPT_DATA_BASE=str(c.ept_phys[s.role]), FC_EPT_DATA_BYTES=str(c.data_bytes),
        FC_EPT_TIER_REQUEST=s.directory+'/tier.json', FC_EPT_PACKET=s.directory+'/ept.packet', FC_EPT_FAIL=c.args.fail or '',
        FC_RESIDENT_NATIVE='1' if c.args.native else '0',
        FC_EPT_BASELINE='1' if c.args.baseline else '0')


def configure(c):
    c.ept_phys = {}
    for role in ('A', 'B'):
        raw, _ = c.shell(role, 'cat /sys/bus/dax/devices/dax0.0/resource\n'
            'echo 1 > /sys/module/kvm/parameters/dax_nested_root_sync', 'ept-configure')
        values = [int(line, 0) for line in raw.splitlines() if line.startswith('0x')]
        require(len(values) == 1, 'DAX physical base missing')
        c.ept_phys[role] = values[0]
    require(c.ept_phys['A'] == c.ept_phys['B'], 'Different CXL physical layouts')


def remember(c, s):
    if not hasattr(c, 'ept_workers'):
        c.ept_workers = {}
    c.ept_workers[s.role] = copy.copy(s)


def prepare(c, s):
    """Boot a disposable DRAM seed, then prepare its ordinary VM/vCPU/memslots."""
    target = copy.copy(s)
    target.role, target.epoch = 'B', s.epoch + 1
    ready=c.boot_vm(target)
    c.pause(target)
    c.api(target.role, target.directory, '/actions', 'PUT', {'action_type':'EptAdoptInit'})
    prefix=c.fetch_bytes(target.role,target.directory+'/fc.log','prepared-seed-log')
    c.prepared_seed=dict(directory=target.directory,file=c.receipts[-1]['file'],
        bytes=len(prefix),sha256=hashlib.sha256(prefix).hexdigest(),ready=ready)
    remember(c, target)


def prepare_native(c,offset=None):
    from resident_fast import prepare_servers
    if offset is not None:c.bench_offset=offset
    c.resident_workers={role:{**{key:getattr(w,key) for key in ('directory','plan','report','manifest_path')},'imported':True}
        for role,w in c.ept_workers.items()}
    prepare_servers(c)
    c.resident_readiness={role:dict(directory=w['directory'],ready_file=c.resident_servers[role]['file'])
        for role,w in c.resident_workers.items()}


def handoff(c, s, destination):
    if c.args.native:
        from resident_fast import handoff as native_handoff
        row=native_handoff(c,s,destination)
        row.update(kind='ept-adopt',packet_bytes=row['fast']['state_bytes'],
            pause_to_ack_ns=row['fast']['guest_ack_ns'])
        return row
    source, old_dir, old_epoch = s.role, s.directory, s.epoch
    target = c.ept_workers[destination]
    started = time.monotonic_ns()
    sent = c.api(source, old_dir, '/actions', 'PUT', {'action_type':'EptAdoptExport'})
    data = c.fetch_bytes(source, old_dir+'/ept.packet', 'ept-packet')
    packet_file = c.receipts[-1]['file']
    c.write_bytes(destination, target.directory+'/ept.packet', data, 'ept-packet-transfer')
    received = c.api(destination, target.directory, '/actions', 'PUT', {'action_type':'EptAdoptImport'})
    for key in ('directory','plan','report','manifest_path'):
        setattr(s, key, getattr(target,key))
    s.role, s.epoch, s.manifest = destination, old_epoch+1, None
    row = dict(kind='ept-adopt', source=source, destination=destination, source_epoch=old_epoch,
        destination_epoch=s.epoch, packet_file=packet_file, packet_bytes=len(data),
        ram_transfer_bytes=0, request_ns=started, resume_ns=time.monotonic_ns(),
        source_directory=old_dir, destination_directory=target.directory, send=sent, receive=received)
    c.transitions.append(row)
    return row


def clean(c, s):
    if not getattr(c, "ept_observers_ready", False):
        require(s.manifest is not None, "Initial RAM manifest missing for observers")
        manifest_bytes=json.dumps(s.manifest).encode()
        for role,worker in c.ept_workers.items():
            c.write_bytes(role,worker.manifest_path,manifest_bytes,"observer-manifest")
        c.ept_observers_ready=True
    if c.args.native:
        from resident_fast import maintain
        return maintain(c,s)
    start=time.monotonic_ns()
    result=c.api(s.role,s.directory,'/actions','PUT',{'action_type':'EptAdoptClean'})
    role='B' if s.role=='A' else 'A'
    observer=c.api(role,c.ept_workers[role].directory,'/actions','PUT',{'action_type':'EptAdoptClean'})
    return dict(role=s.role,epoch=s.epoch,host_ns=time.monotonic_ns()-start,api=result,observer=observer)


def start_faults(c):
    for role in ('A','B'):
        c.shell(role,'mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true\n'
            f"echo 'hist:keys=common_pid if fault_address >= {128*MIB} && fault_address < {(128+c.args.dirty_mib)*MIB}' > "
            '/sys/kernel/tracing/events/kvm/kvm_page_fault/trigger','payload-fault-start')


def faults(c,role):
    import re
    raw,file=c.shell(role,'cat /sys/kernel/tracing/events/kvm/kvm_page_fault/hist','payload-fault-count')
    matches=re.findall(r'Totals:\s*Hits:\s*(\d+)\s+Entries:\s*(\d+)\s+Dropped:\s*(\d+)',raw)
    require(len(matches)==1 and int(matches[0][2])==0,'Missing/dropped EPT fault histogram')
    return dict(count=int(matches[0][0]),file=file)


def wait_ack(memory, offset, epoch, payload=False):
    deadline=time.monotonic()+180
    while True:
        ack, verified = struct.unpack_from('<2Q', memory, offset+24)
        errors=struct.unpack_from('<Q',memory,offset+40)[0]
        clock_errors=struct.unpack_from('<Q',memory,offset+56)[0]
        tsc_errors=struct.unpack_from('<Q',memory,offset+72)[0]
        done=struct.unpack_from('<Q',memory,offset+136)[0]
        require(errors==clock_errors==tsc_errors==0, 'Guest checksum/clock/TSC failed')
        if ack==verified==epoch and (not payload or done==epoch):
            return dict(epoch=ack,verified=verified,errors=errors,payload_done=done)
        require(time.monotonic()<deadline, f'Guest ACK timed out: {epoch=} {ack=} {verified=} {done=}')
        time.sleep(.001)


def release_exit_proof(c,s,memory):
    c.pause(s)
    inactive='B' if s.role=='A' else 'A'
    before=hashlib.sha256(memory[c.kvm_arena:c.kvm_arena+16*MIB]).hexdigest()
    raw=nfs.stop_fc(c.vms[inactive],c.ept_workers[inactive].directory)
    file=c.raw('released-source-exit',raw)
    after=hashlib.sha256(memory[c.kvm_arena:c.kvm_arena+16*MIB]).hexdigest()
    require(before==after, 'Released source exit changed the EPT extent')
    return dict(sha256=after,unchanged=True,source=inactive,file=file,
        operations=['RAM munmap after release','Firecracker exit','VM/vCPU/mapper fd release'])


def tier_chunks(c,command=None):
    arg=c.args.tier_chunks
    if arg=='written':
        require(c.args.tier_history is not None and command is not None,'written tier needs prior SWE records')
        history=c.args.tier_history
        old=json.loads(history.read_text())
        rows=[r for r in old['oracle'] if r['phase']==4]
        row=rows[command]
        data=(history.parent/'dirty-oracle'/f"{row['sequence']}.bitmap").read_bytes()
        words=(row['pages']+63)//64
        union=int.from_bytes(data[:words*8],'little')
        return [chunk for chunk in range(c.args.memory_mib//2) if union>>(chunk*512)&((1<<512)-1)]
    count=c.args.memory_mib//4 if arg=='half' else int(arg)
    chunks=list(range(c.args.tier_start,c.args.tier_start+count))
    # The existing ACK transport reads GPA 64MiB from its CXL home.
    if c.args.workload and 32 in chunks:
        chunks.remove(32);chunks.append(c.args.tier_start+count)
    return chunks

def set_tiers(c,s,chunks):
    started=time.monotonic_ns()
    c.pause(s)
    c.write_bytes(s.role,s.directory+'/tier.json',json.dumps({'chunks':chunks,'eager':c.args.tier_refault=='eager'}).encode(),'tier-request')
    c.api(s.role,s.directory,'/actions','PUT',{'action_type':'MemoryTier'})
    c.resume(s)
    return dict(role=s.role,chunks=chunks,host_ns=time.monotonic_ns()-started)


def replay(c,s,report,save):
    source=Path(c.config['assets'])/'traces'/f'{c.args.name}.traj.json'
    shutil.copy2(source,c.out/'source.traj.json')
    report.update(schema='ept-adopt-trace-v1',mode='ept-adopt',trace=c.trace,fixture=c.fixture,
        commands=c.trace_commands,control_commands=c.commands,launches=c.launches,
        source_trace={'file':'source.traj.json','sha256':lab.sha256(source)})
    report['initial_identity']=c.command_vm(s,'identity')
    heavy=json.loads(c.args.heavy_plan.read_text())['indices'][c.args.name]
    report['boundaries']=heavy
    c.promote(s)
    if c.args.native:prepare_native(c)
    for command in c.trace['commands']:
        index=command['index']
        if index in heavy:
            hop=handoff(c,s,'B' if s.role=='A' else 'A')
            hop['before_command']=index
            if c.args.tier_chunks!='0':report.setdefault('tiers',[]).append(set_tiers(c,s,tier_chunks(c,index)))
        row=c.trace_command(s,'run',index,event='trace_end',
            timeout=int(c.trace['environment'].get('timeout',60))+30)
        end=row['receipt']
        require(not end['timed_out'] and end['rc']==(command['expected_returncode'] or 0)
            and end['command_sha256']==command['command_sha256'],f'Trace diverged at {index}')
        row['guest_duration_s']=end['end_s']-end['start_s']
        if index in heavy and c.args.tier_chunks!='0':report.setdefault('tiers',[]).append(set_tiers(c,s,[]))
        c.trace_commands.append(row)
        if c.args.preclean:
            report.setdefault('cleans',[]).append(clean(c,s))
            if index+1 in heavy:
                deadline=row['host_receive_ns']+int(float(c.args.clean_wait)*1e9)
                begin=time.monotonic_ns();time.sleep(max(0,(deadline-begin)/1e9));ended=time.monotonic_ns()
                report.setdefault('waits',[]).append(dict(after_command=index,planned_s=float(c.args.clean_wait),
                    actual_ns=ended-row['host_receive_ns'],sleep_ns=ended-begin,late_ns=max(0,ended-deadline)))
        save()
    report['final_identity']=c.command_vm(s,'identity')
    exported=c.trace_command(s,'export',event='trace_export',timeout=180)
    _,archive=c.frame(s,exported,'TRACE_EXPORT','guest-archive')
    report.update(guest_archive=archive,guest_archive_sha256=exported['receipt']['sha256'],
        guest_export=exported,guest_duration_s=sum(x['guest_duration_s'] for x in c.trace_commands),
        prepared_seed=c.prepared_seed)


def run_existing(args):
    _,config=lab.current_run()
    config=dict(config,ept_adopt=True,ept_shared=not args.baseline)
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    c=HandoffController(config,out,args);s=c.sandboxes[0]
    report=dict(result='running',experiment_mode='ept-adopt',baseline=args.baseline,config=config,transitions=c.transitions,receipts=c.receipts,
        arguments={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items() if not callable(v)},
        timing='L0 CLOCK_MONOTONIC; includes HTTP control and packet file transport',
        scope='coherent DRAM-backed CXL emulation; nested lab synchronization enabled')
    from experiment_sources import record_sources
    report['source_hashes'],report['source_reference']=record_sources(Path(__file__).resolve().parent,
        ('ept_adopt.py','handoff_bench.py','dirty_oracle.py','verify_swe_handoff.py','lab.py',
         'firecracker.patch','kvm-dax-state.patch','kvm-dax-source.json','resident_control.c',
         'resident_fast.py','resident_proxy.py','resident_wire.h','control_rpc.py','control_rpc.c','shared_probe.c',
         'pool_memory_module.c','pool_memory_uapi.h','guest-init.sh','swe-handoff-guest-run.sh','swe_handoff_run.py'))
    report['layout']=dict(ept_arena=c.kvm_arena,ept_capacity=16*MIB,metadata=c.metadata_base+s.vm*64*MIB)
    oracle=None
    def save():lab.save_json(out/'report.json',report)
    save()
    try:
        if args.dirty_oracle:
            from dirty_oracle import DirtyOracle
            oracle=DirtyOracle(config,out,c.metadata_base+s.vm*64*MIB,c.kvm_arena,args.page_hash)
        lab.verify(argparse.Namespace(timeout=180))
        c.setup();configure(c)
        prepare(c,s)
        report['boot']=c.boot_vm(s)
        remember(c,s)
        workload=('dirty'+str(args.dirty_mib)+(f':{args.dirty_lines}:change' if args.dirty_lines else '')) if args.workload=='dirty' else args.workload
        if args.dirty_pages is not None:
            require(args.dirty_lines>0,'dirty-pages requires a physical payload (dirty-lines > 0)')
            workload+=':'+str(args.dirty_pages)
        if args.heavy_plan:
            replay(c,s,report,save)
        else:
            report['heartbeat']=c.trace_command(s,'heartbeat',workload,event='trace_heartbeat')
        if not args.heavy_plan:
            c.promote(s)
            frames=sorted(c.allocator.grants[s.owner].frames)
            require(frames==list(range(2*MIB,(2+args.memory_mib)*MIB,PAGE)), 'Contiguous test grant absent')
            offset=frames[64*MIB//PAGE]
            if args.native:
                prepare_native(c,offset)
                report['timing']='source L1 CLOCK_MONOTONIC; existing native worker transport, pause to guest ACK'
            if args.count_faults:start_faults(c)
            with Path(config['cxl_backing']).open('r+b') as backing, mmap.mmap(backing.fileno(),0) as memory:
                wait_ack(memory,offset,s.epoch,args.workload=='dirty')
                for i in range(2*(args.warmup+args.iterations)):
                    time.sleep(.02)
                    if args.preclean:
                        report.setdefault('cleans',[]).append(clean(c,s))
                        time.sleep(float(args.clean_wait))
                    wait_ack(memory,offset,s.epoch,args.workload=='dirty')
                    if args.workload=='race':
                        before=struct.unpack_from('<Q',memory,offset+PAGE)[0]
                        deadline=time.monotonic()+30
                        while True:
                            values=[struct.unpack_from('<Q',memory,offset+page*PAGE)[0] for page in range(1,17)]
                            if min(values)>before:break
                            require(time.monotonic()<deadline,'Race writer stopped progressing after clean')
                            time.sleep(.001)
                        report.setdefault('race_progress',[]).append(dict(before=before,after=values))
                    if args.tier_chunks!='0':
                        tier_before=faults(c,s.role) if args.count_faults else None
                        report.setdefault('tiers',[]).append(set_tiers(c,s,tier_chunks(c)))
                        # Request another full guest write/verify while its selected chunks are local.
                        s.epoch+=1
                        struct.pack_into('<Q',memory,offset+16,s.epoch)
                        wait_ack(memory,offset,s.epoch,args.workload=='dirty')
                        if tier_before is not None:
                            tier_after=faults(c,s.role)
                            report['tiers'][-1]['first_access_faults']=tier_after['count']-tier_before['count']
                        if args.preclean:
                            report.setdefault('cleans',[]).append(clean(c,s))
                        if args.tier_pre_demote:
                            report['tiers'].append(set_tiers(c,s,[]))
                    destination='B' if s.role=='A' else 'A'
                    before=faults(c,destination) if args.count_faults else None
                    row=handoff(c,s,destination)
                    row['ack']=wait_ack(memory,offset,s.epoch,args.workload=='dirty')
                    if before is not None:
                        after=faults(c,destination)
                        row['payload_ept_violations']=after['count']-before['count']
                        row['fault_before'],row['fault_after']=before,after
                    row['ack_ns']=time.monotonic_ns()
                    if not args.native:row['pause_to_ack_ns']=row['ack_ns']-row['request_ns']
                    row['measured']=i//2>=args.warmup
                    save()
                report['released_source_exit']=release_exit_proof(c,s,memory)
        if oracle:
            oracle.close();report['oracle']=oracle.rows;oracle=None
        report['result']='passed'
    except BaseException as exc:
        report.update(result='failed',error=repr(exc));raise
    finally:
        for role,worker in getattr(c,'ept_workers',{}).items():
            for name in ('vmm.log','fc.log','stderr.log')+ (('control/log',) if args.native else ()):
                try:
                    raw=c.fetch_bytes(role,worker.directory+'/'+name,'final-'+role+'-'+name.replace('/','-'))
                    (out/(role+'-'+name.replace('/','-'))).write_bytes(raw)
                except Exception as exc:report.setdefault('log_errors',[]).append(repr(exc))
        if oracle:
            try:oracle.close()
            except Exception as exc:report['oracle_error']=repr(exc)
        report.update(trace_commands=c.trace_commands,api_events=c.api_events,memory_events=c.memory_events)
        for role,server in getattr(c,'resident_servers',{}).items():
            try:nfs.stop_fc(c.vms[role],server['directory'])
            except Exception as exc:report.setdefault('cleanup_errors',[]).append(repr(exc))
        report['cleanup']=[]
        from handoff_bench import source_fence_script
        for role,directory in reversed(c.active):
            try:
                nfs.stop_fc(c.vms[role],directory)
                raw,file=c.shell(role,source_fence_script(directory),'final-fence')
                c.fetch_bytes(role,directory+'/fc.log','final-guest-log')
                c.fetch_bytes(role,directory+'/vmm.log','final-vmm-log')
                report['cleanup'].append(dict(role=role,directory=directory,file=file,fenced='FENCED ' in raw))
            except Exception as exc:report.setdefault('cleanup_errors',[]).append(repr(exc))
        if args.heavy_plan and report['result']=='passed':
            from verify_swe_handoff import Evidence,verify_trace
            try:report['independent_trace_verification']=verify_trace(Evidence(report,out),report)
            except Exception as exc:
                report.update(result='failed',verification_error=repr(exc));save();raise
        save()
    print(json.dumps(dict(result=report['result'],output=str(out))))


def add_arguments(p):
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--name',default='gin-gonic__gin-3741')
    p.add_argument('--memory-mib',type=int,default=2048)
    p.add_argument('--vcpus',type=int,choices=(1,2),default=1)
    p.add_argument('--iterations',type=int,default=2)
    p.add_argument('--warmup',type=int,default=0)
    p.add_argument('--workload',choices=('idle','busy','dirty','race'),default='dirty')
    p.add_argument('--dirty-mib',type=int,default=256)
    p.add_argument('--dirty-pages',type=int,help='Limit touched payload pages; checksum verifies those pages')
    p.add_argument('--dirty-lines',type=int,choices=(0,1,8,64),default=0)
    p.add_argument('--count-faults',action='store_true')
    p.add_argument('--native',action='store_true')
    p.add_argument('--cache-maintenance',action=argparse.BooleanOptionalAction,default=True)
    p.add_argument('--baseline',action='store_true',help='Comparison: native local EPT, prepared semantic restore, no EPT ioctls')
    p.add_argument('--dirty-oracle',action='store_true')
    p.add_argument('--page-hash',type=Path)
    p.add_argument('--preclean',action='store_true')
    p.add_argument('--clean-wait',choices=('0','1'),default='0')
    p.add_argument('--heavy-plan',type=Path)
    p.add_argument('--tier-chunks',default='0')
    p.add_argument('--tier-refault',choices=('lazy','eager'),default='lazy')
    p.add_argument('--tier-start',type=int,default=0)
    p.add_argument('--tier-pre-demote',action='store_true')
    p.add_argument('--tier-history',type=Path)
    p.add_argument('--fail',choices=('outside','bump','ept-invalidate','ram-invalidate','dram-release','demote-copy','demote-record','tier-flush'))


def validate_arguments(args,p):
    if args.preclean and not args.cache_maintenance:p.error('Preclean needs cache maintenance')
    if args.dirty_oracle and not args.page_hash:p.error('Oracle needs --page-hash')
    if args.fail and not args.dirty_oracle:p.error('Fault injection requires --dirty-oracle')
    if args.tier_chunks!='0' and (args.baseline or not args.cache_maintenance):p.error('tier split needs retained EPT and cache maintenance')
    args.trajectory_policy='heavy' if args.heavy_plan else None
    if args.heavy_plan:args.workload=None
    args.noncoherent=args.cache_maintenance
    args.restore_timing=True


def main():
    p=argparse.ArgumentParser(description=__doc__)
    add_arguments(p)
    args=p.parse_args()
    validate_arguments(args,p)
    run_existing(args)


if __name__=='__main__':main()
