#!/usr/bin/env python3
"""Matched current-direct-path device/backend ablation, with fresh lab pairs.

All arms warm the same readonly loop-ext4 payload from the same initramfs and
promote 2GiB through the real guest-authorized CPM path. No canonical pages are
admitted: the experiment isolates device handling, not sharing efficiency.
The optional virtio disk is activated and checked with guest O_DIRECT reads.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import struct
import sys
import tempfile
import time

import lab
from experiment_sources import record_sources
from home_storage_bench import require, json_lines
from pool_bench import MIB
from pool_handoff_bench import HandoffController
from pool_handoff_control import FrameAllocator, promotion_layout, validate_export, validate_handoff_handle
from swe_pool_bench import SWEController
import resident_fast
from swe_pool_run import SOURCES

HERE = Path(__file__).resolve().parent
ARMS = ('none', 'local', 'nfs', 'nfs-unprepared')


def run(argv, **kw):
    return subprocess.run(list(map(str, argv)), check=True, **kw)


def build(a):
    base, dest = a.base.resolve(strict=True), a.output.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((base/'manifest.json').read_text())
    for name in ('firecracker', 'l1-kernel', 'l2-vmlinux', 'l2-initramfs.cpio',
                 'shared-probe', 'resident-control', 'pool_memory_module.ko', 'pool-bloom-tool', 'pool-dax-copy'):
        require(lab.sha256(base/name) == manifest['artifacts'][name], 'Base input changed: '+name)
        shutil.copy2(base/name, dest/name)
    for source, binary in (('resident_bootstrap.c', 'resident-bootstrap'), ('pool_handoff_probe.c', 'pool-handoff-probe')):
        run(['gcc', '-O2', '-static', '-Wall', '-Wextra', '-Werror', HERE/source, '-o', dest/binary])
    with tempfile.TemporaryDirectory(dir=dest) as directory:
        root = Path(directory)
        image = root/'image.raw'
        run(['debugfs', '-R', f'dump -p /sandbox.raw {image}', a.pool_storage], capture_output=True)
        require(lab.sha256(image) == manifest['pool']['image']['sha256'], 'Wrong pool image')
        digest = 14695981039346656037
        for byte in image.read_bytes()[:MIB]:
            digest = ((digest ^ byte) * 1099511628211) & ((1 << 64)-1)
        manifest['backend_prefix_fnv'] = f'{digest:016x}'
        outer = root/'outer'; outer.mkdir()
        shutil.copy2(image, outer/'backend.raw')
        with (dest/'storage.raw').open('wb') as f: f.truncate(320*MIB)
        run(['mkfs.ext4', '-q', '-F', '-b', '4096',
             '-E', 'lazy_itable_init=0,lazy_journal_init=0', '-d', outer, dest/'storage.raw'])
        guest, host = root/'guest', root/'host'
        for directory, archive in ((guest, 'pool-handoff-l2-initramfs.cpio'), (host, 'l1-initramfs.cpio')):
            directory.mkdir(); directory.chmod(0o755)
            with (base/archive).open('rb') as stream:
                run(['cpio', '-id', '--quiet', '--no-absolute-filenames'], cwd=directory, stdin=stream)
        shutil.copy2(image, guest/'lab/backend.raw')
        shutil.copy2(dest/'pool-handoff-probe', guest/'lab/pool-handoff-probe')
        init = (HERE/'pool-handoff-l2-init.sh').read_text().replace(
            'mount -t ext4 -o ro,noload,noatime /dev/vda /data',
            'losetup -r /dev/loop0 /lab/backend.raw\nmount -t ext4 -o ro,noload,noatime /dev/loop0 /data')
        (guest/'init').write_text(init); (guest/'init').chmod(0o755)
        lab.pack_initramfs(guest, dest/'backend-initramfs.cpio')
        for path in (host/'lab').glob('*-initramfs.cpio'):
            if path.name != 'l2-initramfs.cpio': path.unlink()
        for path in dest.iterdir():
            if path.is_file() and path.name not in ('l1-kernel', 'storage.raw'):
                shutil.copy2(path, host/'lab'/path.name)
        lab.pack_initramfs(host, dest/'l1-initramfs.cpio')
    manifest['base_assets'] = str(base)
    manifest['schema'] = 'backend-ablation-fixture-v1'
    manifest['artifacts'] = {p.name:lab.sha256(p) for p in dest.iterdir() if p.is_file()}
    manifest['build_sources'] = {n:lab.sha256(HERE/n) for n in ('backend_ablation.py', 'pool_handoff_probe.c', 'resident_bootstrap.c')}
    lab.save_json(dest/'manifest.json', manifest)
    print(dest, flush=True)


class Controller(HandoffController):
    setup = SWEController.setup
    configure_direct = SWEController.configure_direct

    def __init__(self, config, out, arm, *, profiled=True, inline_guest_ack=False, resume_nokick=False, exec_request=False):
        args = argparse.Namespace(memory_mib=2048, vcpus=1, sandboxes=1, minimum_cache_mib=128,
            io_engine='Async', mode='direct', credit_mib=16, resident_direct=True,
            resident_ready_control=True, resident_skip_disk_prepare=arm in ('none', 'nfs-unprepared'),
            immutable_nfs_cache=True, restore_timing=profiled, full_audit=False, resume_nokick=resume_nokick,
            exec_request=exec_request)
        super().__init__(config, out, args)
        self.root = '/run/swe-pool-'+secrets.token_hex(8)
        self.arm = arm
        self.inline_guest_ack = inline_guest_ack
        self.trace = {'name':'backend'}
        self.metadata_base, self.pool_offset, self.bloom_offset = 8192*MIB, 4096*MIB, 3072*MIB
        self.allocator = FrameAllocator(2*MIB, 3072*MIB)
        self.resident_resource_events, self.resident_probes = [], []
        self.resident_readiness, self.resident_preparations = {}, []

    def boot_vm(self, s):
        self.start_vm(s)
        components = {'/machine-config':{'vcpu_count':1,'mem_size_mib':self.memory_mib},
            '/boot-source':{'kernel_image_path':'/lab/l2-vmlinux', 'initrd_path':'/lab/backend-initramfs.cpio',
                'boot_args':f'console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init pool.vm=1 pool.image_id={self.image_id} page_reporting.page_reporting_order=0'},
            '/balloon':{'amount_mib':0,'deflate_on_oom':False,'stats_polling_interval_s':0,
                'free_page_hinting':False,'free_page_reporting':True}}
        if self.arm != 'none':
            components['/drives/data'] = {'drive_id':'data','path_on_host':'/disk/sandbox.raw',
                'is_root_device':False,'is_read_only':True,'cache_type':'Writeback','io_engine':'Async'}
        for path, body in components.items(): self.api(s.role,s.directory,path,'PUT',body)
        self.api(s.role,s.directory,'/actions','PUT',{'action_type':'InstanceStart'})
        self.event(s,'pool_ready')
        self.command_vm(s,'warm',require_cache=True)

    def promote(self, s):
        registration, authority = self.registration(s)
        require(registration['registered_bytes'] >= self.minimum_cache_bytes, 'No real guest authority')
        self.pause(s)
        s.owner = f'vm{s.vm}:allocation{authority["token"]}'
        layout = promotion_layout(self.allocator,s.owner,self.memory_mib*MIB,[],self.args.credit_mib*MIB//4096)
        result, operation = self.memory_operation(s,'promote',registration=registration['registration'],
            mappings=layout['mappings'],credits=layout['credits'],allocation_generation=layout['allocation_generation'])
        require(result['shared_bytes']==0 and result['copied_private_bytes']==self.memory_mib*MIB
            and result['ram_snapshot_bytes']==0 and result['file_page_compare_bytes']==0,'Ablation promotion differs')
        s.tier='cxl'
        exported=self.export(s)
        self.owners[s.vm]={'keys':set(),'tier':'cxl','epoch':s.epoch}
        self.resume(s)
        self.release_registration(s,authority['token'])
        return {'authority':authority,'operation':operation,'export':self.export_evidence(exported)}

    def backend_read(self,s):
        label=f'io{len(self.receipts)}'
        self.shell(s.role,f"printf 'backend_read {label}\\n' > {s.directory}/input",'backend-command')
        row=self.event(s,'backend_read',label)
        require(row['receipt']['hash']==self.fixture['backend_prefix_fnv']
            and row['receipt']['read_ios']>0 and row['receipt']['bytes']==MIB,'Backend data read failed')
        return row

    def prepare(self,s):
        target=copy.copy(s);target.role='B';target.epoch=1
        directory=f'{self.root}/vm1-epoch1-resident-prepare'
        begin=time.monotonic_ns();first=len(self.control_timings)
        self.start_vm(target,restore_manifest=directory+'/restore-manifest.json',
            direct_import=directory+'/direct-import.json',purpose='resident-prepare')
        cold={'wall_ns':time.monotonic_ns()-begin,'stages':self.control_timings[first:],
            'worker':dict(self.resident_workers['B'])}
        cold['server_wall_ns']=resident_fast.prepare_servers(self)
        cold['servers']={role:dict(server) for role,server in self.resident_servers.items()}
        return cold

    def hop(self,s):
        source=s.role;dest='B' if source=='A' else 'A'
        old_dir=s.directory;worker=self.resident_workers[dest];epoch=s.epoch+1
        first=not worker['imported'];label=f'hop{epoch}'
        ip={'A':'172.30.0.1','B':'172.30.0.2'}
        raw,name=resident_fast.request(self,source,old_dir,self.resident_control_nonce,first,label,ip[dest],
            1,epoch,self.pool_offset,self.image_bytes,'handoff-request',observe=self.inline_guest_ack)
        rows=json_lines(raw,'RESIDENT_FAST_JSON ')
        controller_ns=self.control_timings[-1]['elapsed_ns']
        controller_rpc=self.control_timings[-1].get('rpc')
        require(len(rows)==1,'Missing native handoff receipt')
        timing=rows[0]
        require(all(x['status']==204 for x in timing['apis'].values()),'Handoff API failed')
        state_bytes=self.fetch_bytes(source,old_dir+'/direct-export.json','device-state')
        state=json.loads(state_bytes);state_file=self.receipts[-1]['file']
        kernel_profile=None
        if self.args.restore_timing:
            # Diagnostic-only fixed 128-byte timing record, after the timed
            # request has completed. Never walk guest RAM or mapper entries.
            offset=12288*MIB+512
            with Path(self.config['cxl_backing']).open('rb',buffering=0) as stream:
                stream.seek(offset);data=stream.read(128)
            require(len(data)==128,'Short kernel profile')
            stages=struct.unpack('<16Q',data)
            kernel_profile={'generation':state['token']['generation'],'offset':offset,'bytes':128,
                'detach_ns':list(stages[:8]),'attach_ns':list(stages[8:])}
            kernel_profile={'file':self.raw('kernel-stage-profile',json.dumps(kernel_profile)),**kernel_profile}
        sealed_bytes=self.fetch_bytes(source,s.manifest_path,'sealed-manifest')
        sealed=json.loads(sealed_bytes);sealed_file=self.receipts[-1]['file']
        require(len(state_bytes)==timing['state_bytes'] and len(sealed_bytes)==timing['manifest_bytes']
            and hashlib.sha256(sealed_bytes).hexdigest()==state['memory_manifest_sha256'],'Transferred metadata mismatch')
        log=self.fetch_bytes(dest,worker['directory']+'/vmm.log','handoff-import-log').decode()
        imports=[json.loads(line.split('DIRECT_HANDOFF_JSON ',1)[1]) for line in log.splitlines() if 'DIRECT_HANDOFF_JSON ' in line]
        imported=next(x for x in imports if x['event']=='import' and x['token']['generation']==state['token']['generation'])
        manifest=imported['mapper']
        manifest_file=self.raw('handoff-manifest',json.dumps(manifest))
        require(len(state['devices']['virtio_state']['Mmio']['block_devices']) == (0 if self.arm=='none' else 1),
                'Unexpected block graph')
        validate_export(self.allocator,s.owner,manifest,self.memory_mib*MIB,self.pool_offset,self.image_bytes)
        validate_handoff_handle(sealed,manifest)
        self.resident_fence(source,old_dir,state['token']['generation'])
        raw,negative=self.shell(source,
            f'/lab/shared-probe api {old_dir}/api.sock PUT /actions '\
            "'{\"action_type\": \"DirectSend\"}' 400",'parked-combined-send')
        rejected=json_lines(raw,'LAB_API_JSON ')
        require(len(rejected)==1 and rejected[0]['status']==400,'Parked source accepted DirectSend')
        self.import_negatives.append({'kind':'parked-combined-send','role':source,'file':negative,
            'request':{'action_type':'DirectSend'},'response':rejected[0],'generation':state['token']['generation']})
        for key in ('directory','plan','report','manifest_path','hints'): setattr(s,key,worker[key])
        s.role,s.epoch,s.manifest=dest,epoch,manifest
        worker['imported']=True
        return {'source':source,'destination':dest,'first_import':first,'epoch':epoch,'file':name,
            'controller_command_wall_ns':controller_ns,
            'controller_rpc':controller_rpc,
            'kernel_profile':kernel_profile,
            'timing':timing,'export_token':state['token'],'state_file':state_file,
            'sealed_file':sealed_file,'manifest_file':manifest_file}


def bench(a):
    _,config=lab.current_run();out=a.output;out.mkdir(parents=True,exist_ok=False)
    c=Controller(config,out,a.arm,profiled=True,inline_guest_ack=a.inline_guest_ack,
                 resume_nokick=a.resume_nokick,exec_request=a.exec_request);s=c.sandboxes[0]
    report={'schema':'backend-ablation-trial-v1','arm':a.arm,'result':'running','fixture':c.fixture,
        'config':config,'receipts':c.receipts,'commands':c.commands,'api_events':c.api_events,
        'launches':c.launches,'hops':[],'backend_reads':[],'logs':[],'parked_combined_action_test':True,
        'memory_events':c.memory_events,'source_probes':c.resident_probes,'negative_apis':c.import_negatives,
        'profiled':True,'inline_guest_ack':a.inline_guest_ack,'resume_nokick':a.resume_nokick}
    def save(): lab.save_json(out/'report.json',report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        c.setup()
        if a.arm=='local':
            c.shell('B','mkdir /replica\nmount -t ext4 -o ro,noload /dev/vda /replica\n'
                'umount /disk\ntouch /disk/sandbox.raw\n'
                'mount -o bind /replica/backend.raw /disk/sandbox.raw\n'
                f'printf "%s  /disk/sandbox.raw\\n" {c.image_id} | sha256sum -c -','local-backend')
        c.configure_direct();c.boot_vm(s)
        if a.arm!='none': report['backend_reads'].append(c.backend_read(s))
        report['before']=c.command_vm(s,'check',require_cache=True)
        report['promotion']=c.promote(s)
        report['preparation']=c.prepare(s)
        if a.kernel_trace:
            import kernel_handoff_trace
            kernel_handoff_trace.start(c)
        for _ in range(a.hops):
            if a.kernel_trace:kernel_handoff_trace.mark(c,s.epoch+1)
            hop=c.hop(s);report['hops'].append(hop)
            ack=hop['timing']['ack']
            if not a.inline_guest_ack:
                require(ack is None,'Unexpected guest probe in runtime request')
                label=f'hop{s.epoch}'
                c.shell(s.role,f"printf 'ping {label}\\n' > {s.directory}/input",'post-handoff-guest-input')
                hop['guest_ack']=c.event(s,'trace_ping',label)
                ack=hop['guest_ack']['receipt']
            for key in ('boot_id','pid','cookie'):
                require(ack[key]==report['before']['receipt'][key],'Guest continuity failed')
            if a.arm!='none' and s.epoch<=2: report['backend_reads'].append(c.backend_read(s))
            save()
        require(s.role=='A','Even number of hops required')
        if a.kernel_trace:report['kernel_trace']=kernel_handoff_trace.finish(c)
        report['after']=c.command_vm(s,'check',require_cache=True)
        c.same_identity(report['before'],report['after'])
        if a.arm!='none':report['backend_reads'].append(c.backend_read(s))
        report['demotion']=c.demote(s)
        report['result']='passed'
    except BaseException as exc:
        report.update(result='failed',error=repr(exc));raise
    finally:
        for role,worker in getattr(c,'resident_workers',{}).items():
            for filename in ('vmm.log','fc.log','stderr.log','control/log'):
                try:
                    c.fetch_bytes(role,worker['directory']+'/'+filename,'final-'+filename.replace('/','-'))
                    report['logs'].append({'role':role,'name':filename,'file':c.receipts[-1]['file']})
                except Exception as exc: report['logs'].append({'role':role,'name':filename,'error':repr(exc)})
        save()


def suite(a):
    require(re.fullmatch(r'[a-z0-9_-]{1,29}',a.session),'Use a fresh lowercase session of at most 29 characters')
    assets=a.assets.resolve(strict=True);a.output.mkdir(parents=True,exist_ok=False)
    frozen, source_reference = record_sources(HERE, (*SOURCES,'backend_ablation.py','pool_handoff_probe.c'))
    manifest=json.loads((assets/'manifest.json').read_text())
    for name,digest in manifest['artifacts'].items():require(lab.sha256(assets/name)==digest,'Changed asset '+name)
    summary={'schema':'backend-ablation-suite-v1','result':'running','sources':frozen,'source_reference':source_reference,'fixture':manifest,'trials':[]}
    def save():lab.save_json(a.output/'suite.json',summary)
    save()
    try:
        for i,arm in enumerate(a.arms):
            require(all(lab.sha256(HERE/n)==d for n,d in frozen.items()),'Measured sources changed')
            session=a.session+f'-{i:02d}';ident=f'{i:02d}-{arm}'
            env=dict(os.environ,CXL_LAB_ASSETS=str(assets),CXL_LAB_SESSION=session,CXL_LAB_QEMU_ROOT=str(a.qemu_root.resolve()))
            prefix=['taskset','-c','0-7',sys.executable,'-u']
            row={'arm':arm,'id':ident,'result':'running'};summary['trials'].append(row);save()
            pointer=HERE/'.local'/f'current-{session}.json'
            require(not pointer.exists(),'Fresh session required')
            def command(args,suffix):
                with (a.output/(ident+'-'+suffix+'.log')).open('w') as stream:
                    return subprocess.run(list(map(str,args)),env=env,stdout=stream,stderr=subprocess.STDOUT).returncode
            try:
                start=prefix+[HERE/'lab.py','start','--vcpus','4','--memory-mib','8192','--shared-mib','64',
                    '--cxl-mib','16384','--dax-guest','--dax-4k','--migration-network','--network-backend','tap',
                    '--memory-backing-dir','/dev/shm','--home-storage-image',assets/'storage.raw',
                    '--replicated-home-storage','--qemu',a.qemu.resolve(),'--dax-handoff']
                if a.serial_control:start.append('--serial-control')
                require(command(start,'start')==0,'Lab start failed')
                flags=[name for name,value in (('--inline-guest-ack',a.inline_guest_ack),
                    ('--resume-nokick',a.resume_nokick),('--exec-request',a.exec_request),('--kernel-trace',a.kernel_trace)) if value]
                require(command(prefix+[HERE/'backend_ablation.py','bench','--arm',arm,'--hops',str(a.hops),
                    '--output',a.output/ident]+flags,'bench')==0,'Benchmark failed')
                row['result']='passed'
            except BaseException as exc:
                row.update(result='failed',error=repr(exc))
                raise
            finally:
                if pointer.exists():
                    original=Path(json.loads(pointer.read_text())['run']);config=json.loads((original/'run.json').read_text())
                    row['original_run']=str(original)
                    row['stop_rc']=command([sys.executable,HERE/'lab.py','stop'],'stop')
                    row['fenced']=all(not lab.alive(vm) for vm in config['vms'].values())
                    row['memory_removed']=not Path(config['ephemeral_memory_dir']).exists()
                    row['network_removed']=all(not Path('/sys/class/net',v['name']).exists()
                        for v in config['migration_network']['host_network']['objects'].values())
                    for path in original.glob('vm-*.log'):shutil.copy2(path,a.output/(ident+'-'+path.name))
                    require(row['stop_rc']==0 and row['fenced'] and row['memory_removed'] and row['network_removed'],'Pair cleanup failed')
                    if row['result']=='passed':
                        disk=Path(config['home_storage_image']['path'])
                        require(disk==original/'home-storage.raw' and not disk.is_symlink(),'Owned disk path')
                        disk.unlink()
                save()
            print('ABLATION_DONE '+ident,flush=True)
        require(all(lab.sha256(HERE/n)==d for n,d in frozen.items()),'Measured sources changed')
        summary['result']='passed'
    except BaseException as exc:summary.update(result='failed',error=repr(exc));raise
    finally:save()


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build');b.add_argument('--base',type=Path,required=True);b.add_argument('--pool-storage',type=Path,required=True)
    b.add_argument('--output',type=Path,required=True)
    b=sub.add_parser('bench');b.add_argument('--arm',choices=ARMS,required=True);b.add_argument('--hops',type=int,default=24)
    for flag in ('--inline-guest-ack','--resume-nokick','--exec-request','--kernel-trace'):b.add_argument(flag,action='store_true')
    b.add_argument('--output',type=Path,required=True)
    b=sub.add_parser('run');b.add_argument('--assets',type=Path,required=True);b.add_argument('--qemu',type=Path,required=True)
    for flag in ('--inline-guest-ack','--resume-nokick','--serial-control','--exec-request','--kernel-trace'):b.add_argument(flag,action='store_true')
    b.add_argument('--qemu-root',type=Path,required=True);b.add_argument('--session',required=True)
    b.add_argument('--arms',choices=ARMS,nargs='+',default=list(ARMS));b.add_argument('--hops',type=int,default=24)
    b.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if hasattr(a,'hops'):require(a.hops>=4 and a.hops%2==0,'Even hops >=4 required')
    {'build':build,'bench':bench,'run':suite}[a.command](a)


if __name__=='__main__':main()
