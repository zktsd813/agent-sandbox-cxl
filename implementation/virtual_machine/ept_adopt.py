#!/usr/bin/env python3
"""EPT-only prepared-worker handoff in the existing two-L1 lab."""
import argparse
import copy
import hashlib
import json
import mmap
from pathlib import Path
import struct
import time

import lab
import nfs_bench as nfs
from handoff_bench import HandoffController, MIB, PAGE, require


def environment(c, s):
    return dict(FC_EPT_ADOPT='1', FC_DAX_REUSE='1', FC_DAX_NO_NESTED_GUEST='1',
        FC_EPT_PHYS=str(c.ept_phys[s.role]+c.kvm_arena), FC_EPT_OFFSET=str(c.kvm_arena),
        FC_EPT_DATA_BASE=str(c.ept_phys[s.role]), FC_EPT_DATA_BYTES=str(c.data_bytes),
        FC_EPT_PACKET=s.directory+'/ept.packet', FC_EPT_FAIL=c.args.fail or '')


def configure(c):
    c.ept_phys = {}
    for role in ('A', 'B'):
        raw, _ = c.shell(role, 'cat /sys/bus/dax/devices/dax0.0/resource\n'
            'echo 1 > /sys/module/kvm/parameters/dax_nested_root_sync\n'
            "grep -q '^enabled=0 ' /sys/module/kvm/parameters/dax_pool", 'ept-configure')
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
    c.boot_vm(target)
    c.pause(target)
    c.api(target.role, target.directory, '/actions', 'PUT', {'action_type':'EptAdoptInit'})
    remember(c, target)


def handoff(c, s, destination):
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
    start=time.monotonic_ns()
    result=c.api(s.role,s.directory,'/actions','PUT',{'action_type':'EptAdoptClean'})
    return dict(role=s.role,epoch=s.epoch,host_ns=time.monotonic_ns()-start,api=result)


def wait_ack(memory, offset, epoch, payload=False):
    deadline=time.monotonic()+180
    while True:
        ack, verified = struct.unpack_from('<2Q', memory, offset+24)
        errors=struct.unpack_from('<Q',memory,offset+40)[0]
        done=struct.unpack_from('<Q',memory,offset+136)[0]
        require(errors==0, 'Guest checksum failed')
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


def run_existing(args):
    _,config=lab.current_run()
    config=dict(config,ept_adopt=True)
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    c=HandoffController(config,out,args);s=c.sandboxes[0]
    report=dict(result='running',config=config,transitions=c.transitions,receipts=c.receipts,
        arguments={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
        timing='L0 CLOCK_MONOTONIC; includes HTTP control and packet file transport',
        scope='coherent DRAM-backed CXL emulation; nested lab synchronization enabled')
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
        workload='dirty'+str(args.dirty_mib) if args.resident_bench=='dirty' else args.resident_bench
        report['heartbeat']=c.trace_command(s,'heartbeat',workload,event='trace_heartbeat')
        c.promote(s)
        frames=sorted(c.allocator.grants[s.owner].frames)
        require(frames==list(range(2*MIB,(2+args.memory_mib)*MIB,PAGE)), 'Contiguous test grant absent')
        offset=frames[64*MIB//PAGE]
        with Path(config['cxl_backing']).open('r+b') as backing, mmap.mmap(backing.fileno(),0) as memory:
            wait_ack(memory,offset,s.epoch,args.resident_bench=='dirty')
            for i in range(2*(args.warmup+args.iterations)):
                time.sleep(.02)
                if args.preclean:
                    report.setdefault('cleans',[]).append(clean(c,s))
                    time.sleep(float(args.clean_wait))
                wait_ack(memory,offset,s.epoch,args.resident_bench=='dirty')
                row=handoff(c,s,'B' if s.role=='A' else 'A')
                row['ack']=wait_ack(memory,offset,s.epoch,args.resident_bench=='dirty')
                row['ack_ns']=time.monotonic_ns();row['pause_to_ack_ns']=row['ack_ns']-row['request_ns']
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
            for name in ('vmm.log','fc.log','stderr.log'):
                try:
                    raw=c.fetch_bytes(role,worker.directory+'/'+name,'final-'+role+'-'+name)
                    (out/(role+'-'+name)).write_bytes(raw)
                except Exception as exc:report.setdefault('log_errors',[]).append(repr(exc))
        if oracle:
            try:oracle.close()
            except Exception as exc:report['oracle_error']=repr(exc)
        report.update(trace_commands=c.trace_commands,api_events=c.api_events,memory_events=c.memory_events)
        for role,directory in reversed(c.active):
            try:nfs.stop_fc(c.vms[role],directory)
            except Exception as exc:report.setdefault('cleanup_errors',[]).append(repr(exc))
        for keeper in getattr(c,'keepalives',[]):
            try:nfs.stop_fc(c.vms[keeper['role']],keeper['directory'])
            except Exception as exc:report.setdefault('cleanup_errors',[]).append(repr(exc))
        save()
    print(json.dumps(dict(result=report['result'],output=str(out))))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--name',default='gin-gonic__gin-3741')
    p.add_argument('--memory-mib',type=int,default=2048)
    p.add_argument('--vcpus',type=int,choices=(1,2),default=1)
    p.add_argument('--iterations',type=int,default=2)
    p.add_argument('--warmup',type=int,default=0)
    p.add_argument('--resident-bench',choices=('idle','busy','dirty','race'),default='dirty')
    p.add_argument('--dirty-mib',type=int,default=256)
    p.add_argument('--dirty-oracle',action='store_true')
    p.add_argument('--page-hash',type=Path)
    p.add_argument('--preclean',action='store_true')
    p.add_argument('--clean-wait',choices=('0','1'),default='0')
    p.add_argument('--fail',choices=('outside','bump','ept-invalidate','ram-invalidate'))
    args=p.parse_args()
    if args.dirty_oracle and not args.page_hash:p.error('Oracle needs --page-hash')
    args.mode='ept-adopt';args.noncoherent=True;args.dirty_ledger=True
    args.restore_timing=True;args.profile=False;args.resume_profile=False
    run_existing(args)


if __name__=='__main__':main()
