#!/usr/bin/env python3
"""Two real static CXL microVMs: donor guest lookup, shared PFNs, no image prefill.

Requires a fresh lab --dax-handoff --migration-network --home-storage-image.
The consumer uses the host NFS image only on fallback. No tier changes.
"""
import argparse, json, re, shlex, time
from pathlib import Path
import lab, nfs_bench as nfs
from pool_bench import PoolController, MIB
from home_storage_bench import require
HERE=Path(__file__).resolve().parent
BLOOM=3072*MIB

def setup(c):
    for role in ('A','B'):
        c.shell(role,lab.snapshot_cleanup_script(include_boot_vm=True)+f'\nmkdir -p {c.root} /storage /disk /remote\ninsmod /lab/pool_memory_module.ko','setup')
    device=c.config['home_storage_image']['guest_device']
    c.shell('A',f'mount -t ext4 -o rw,noatime {device} /storage\nmount -o bind /storage /disk\n'
        'if [ ! -e /storage/sandbox.raw ]; then test -f /storage/backend.raw; ln -s backend.raw /storage/sandbox.raw; fi\n'
        'export PATH=/usr/sbin:/usr/bin:/bin:/sbin:/lab\nmkdir -p /var/lib/nfs /run/rpcbind /proc/fs/nfsd\n'
        'mount -t nfsd nfsd /proc/fs/nfsd\n'
        "printf '/storage 172.30.0.2(rw,sync,no_subtree_check,no_root_squash,fsid=19)\\n' > /etc/exports\n"
        f'rpcbind -f -w > {c.root}/rpcbind.log 2>&1 &\nsleep .2\nexportfs -ra\n'
        f'rpc.mountd --foreground --no-nfs-version 2 --no-nfs-version 4 --port 20048 > {c.root}/mountd.log 2>&1 &\n'
        'rpc.nfsd -V3 -N4 4','nfs-server')
    c.shell('B','/usr/sbin/mount.nfs 172.30.0.1:/storage /remote '
        '-o rw,vers=3,proto=tcp,mountproto=tcp,port=2049,mountport=20048,nolock,hard,timeo=50,retrans=2,actimeo=0\n'
        'mount -o bind /remote /disk','nfs-client')
    c.write_bytes('A',c.root+'/empty-bloom',bytes(4096))
    c.shell('A',f'/lab/pool-dax-copy import {c.root}/empty-bloom /dev/dax0.0 {BLOOM} 4096','empty-bloom')

class Bench:
    def __init__(self,c,manifest,*,guest_transport='vsock',reuse_tcp=True):
        self.c=c; self.extents=manifest['handoff']['logical_extents']; self.seq=10000
        self.guests={}; self.answers=[]
        self.guest_transport=guest_transport; self.reuse_tcp=reuse_tcp
    def offset(self,page):
        for f,d,n in self.extents:
            if f<=page*4096<f+n:return d+page*4096-f
        raise ValueError(page)
    def guest(self,role,op,page,disk=None):
        self.seq+=1; directory=self.guests[role]
        command=f'{op} {self.seq} {page}'+(' '+str(disk) if disk is not None else '')
        event='peer_lookup' if op=='lookup' else 'peer_cache' if op=='cache' else 'peer_read'
        pattern=f'"event":"{event}","id":{self.seq},'
        complete=re.escape(pattern)+r'.*}[[:space:]]*$'
        self.c.shell(role,f"printf '%s\\n' {shlex.quote(command)} > {directory}/input",'command')
        raw,_=self.c.shell(role,f"n=0\nwhile ! grep -Eq {shlex.quote(complete)} {directory}/fc.log; do n=$((n+1)); [ $n -lt 1000 ]; sleep .01; done\ngrep -E {shlex.quote(complete)} {directory}/fc.log",'answer',30)
        rows=[json.loads(line) for line in raw.splitlines() if line.startswith('{"event":') and pattern in line]
        require(len(rows)==1 and rows[0]['ok'],f'guest operation failed: {raw}')
        self.answers.append(dict(rows[0],role=role));return rows[0]
    def rpc(self,role,**q):
        body=json.dumps(q,separators=(',',':'))
        raw,_=self.c.shell(role,f"printf '%s\\n' {shlex.quote(body)} | nc -w 8 127.0.0.1 9010",'peer-rpc',15)
        rows=[json.loads(l) for l in raw.splitlines() if l.startswith('{')]
        require(len(rows)==1,f'RPC reply: {raw}');return rows[0]
    def boot(self,role,deny,*,peer_cache=True,profile=False,label=None):
        c=self.c; i=0 if role=='A' else 1; directory=f'{c.root}/{label or role}';self.guests[role]=directory
        c.shell(role,f'mkdir -p {directory}\nmkfifo {directory}/input','directory')
        config={'image_id':c.image_id,'listen':'0.0.0.0:9010','peer':'172.30.0.1:9010' if i else None,
            'ram_offset':(2+i*512)*MIB,'credit_offset':(1024+i*64)*MIB,'credit_pages':4096,
            'bloom_offset':BLOOM,'extents':self.extents,'input':directory+'/input','log':directory+'/fc.log',
            'guest_socket':directory+'/vsock' if self.guest_transport=='vsock' else None,
            'reuse_tcp':self.reuse_tcp,
            'deny_backend':[self.offset(p) for p in deny]}
        c.write_json(role,directory+'/peer.json',config)
        env={'FC_DAX_RAM':'/dev/dax0.0','FC_DAX_OFFSET':str(config['ram_offset']),'FC_DAX_SNAPSHOT':'1',
            'FC_POOL_IMAGE_PATH':'/disk/sandbox.raw','FC_POOL_IMAGE_ID':c.image_id,
            'FC_POOL_VM_ID':str(i+1),'FC_POOL_RUN_EPOCH':'0','FC_PEER_CACHE_CONFIG':directory+'/peer.json'}
        if not peer_cache:
            for key in ('FC_PEER_CACHE_CONFIG', 'FC_POOL_IMAGE_PATH', 'FC_POOL_IMAGE_ID',
                        'FC_POOL_VM_ID', 'FC_POOL_RUN_EPOCH'):
                env.pop(key)
        if profile: env['FC_PEER_PROFILE']='1'
        script='#!/bin/sh\n'+ '\n'.join('export '+shlex.quote(k+'='+v) for k,v in env.items())+'\n'+f'exec /lab/firecracker --no-seccomp --api-sock {directory}/api.sock\n'
        c.write_bytes(role,directory+'/start.sh',script.encode())
        c.shell(role,f'(exec 3<>{directory}/input\nexec env -i /bin/sh {directory}/start.sh <&3 > {directory}/fc.log 2> {directory}/vmm.log) &\n'+nfs.record_pid(directory)+f'n=0\nwhile [ ! -S {directory}/api.sock ]; do n=$((n+1)); [ $n -lt 500 ]; sleep .02; done','launch')
        c.active.append((role,directory))
        components={'/machine-config':{'vcpu_count':1,'mem_size_mib':256},'/boot-source':{'kernel_image_path':'/lab/l2-vmlinux','initrd_path':'/lab/peer-l2-initramfs.cpio','boot_args':'console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init'},'/drives/data':{'drive_id':'data','path_on_host':'/disk/sandbox.raw','is_root_device':False,'is_read_only':True,'io_engine':'Sync'}}
        components['/vsock']={'guest_cid':3+i,'uds_path':directory+'/vsock'}
        for path,body in components.items():c.api(role,directory,path,'PUT',body)
        c.api(role,directory,'/actions','PUT',{'action_type':'InstanceStart'})
        c.shell(role,f'n=0\nwhile ! grep -q peer_ready {directory}/fc.log; do n=$((n+1)); [ $n -lt 1000 ]; sleep .02; done','ready',40)
    def stop(self,role,directory):
        nfs.stop_fc(self.c.vms[role],directory)
    def bloom_fill(self,byte):
        c=self.c;c.write_bytes('A',c.root+'/bloom.bin',bytes([byte])*4096)
        c.shell('A',f'/lab/pool-dax-copy import {c.root}/bloom.bin /dev/dax0.0 {BLOOM} 4096','bloom-test-injection')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    _,config=lab.current_run();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    args=argparse.Namespace(memory_mib=256,vcpus=1,sandboxes=2,minimum_cache_mib=0,io_engine='Sync')
    c=PoolController(config,out,args);manifest=json.loads((Path(config['assets'])/'manifest.json').read_text());b=Bench(c,manifest)
    report={'result':'running','source_head':manifest['peer_cache']['source_head'],'uncommitted':manifest['peer_cache'].get('uncommitted',True),'canonical_prefill_bytes':0,'lifetime':'infinite','guest_events':b.answers,'shared':[],'limitations':['coherent nested QEMU CXL','static tiers','synchronous 4KiB block reads','displaced frames not reclaimed']}
    try:
        lab.verify(argparse.Namespace(timeout=180));setup(c)
        pages=[0,17,129,1023,26000,40959];b.boot('A',[])
        warm={page:b.guest('A','warm',page) for page in pages}
        ads={page:b.rpc('A',op='advertise',image=c.image_id,offset=b.offset(page)) for page in pages}
        require(all(v.get('found') for v in ads.values()),'donor advertise absent')
        require(all(v['frame']==2*MIB+v['donor_gpa'] for v in ads.values()),'donor was copied into a separate pool')
        # Advertise only actual guest-cache pages, with no full-image preload.
        b.boot('B',pages)
        for page in pages:
            read=b.guest('B','read',page)
            require(read['before']==0 and read['after']==1,'consumer cache was not cold')
            require(read['hash']==warm[page]['hash'],'data mismatch')
            logs=c.fetch_bytes('B',b.guests['B']+'/vmm.log','vmm-log').decode()
            pattern=rf'PEER_READ_HIT offset={b.offset(page)} gpa=(\d+) frame=(\d+)'
            found=re.findall(pattern,logs);require(len(found)==1,'shared read absent/duplicated')
            gpa,frame=map(int,found[0]);require(frame==ads[page]['frame'],'donor frame changed')
            # Fault donor's now-read-only mapping back in, then observe real native PTEs.
            require(b.guest('A','read',page)['hash']==read['hash'],'donor changed')
            ap=b.rpc('A',op='audit',gpa=ads[page]['donor_gpa'])['pte'];bp=b.rpc('B',op='audit',gpa=gpa)['pte']
            require(ap['flags']==1 and bp['flags']==1 and ap['pfn']==bp['pfn'] and ap['pfn']!=0,'PFN sharing/RO protection failed')
            report['shared'].append({'page':page,'frame':frame,'donor_pte':ap,'consumer_pte':bp,'cold_guest_cache':True,'backend_denied':True})
        # A repeat hits B's own guest cache and never issues another peer request.
        before=c.fetch_bytes('B',b.guests['B']+'/vmm.log','before-repeat').count(b'PEER_READ_HIT')
        repeat=b.guest('B','read',0);require(repeat['before']==1,'repeat not resident')
        after=c.fetch_bytes('B',b.guests['B']+'/vmm.log','after-repeat').count(b'PEER_READ_HIT');require(before==after,'local cache hit queried peer')
        # False-positive filter with absent donor page must fall back to host NFS.
        absent=2047;require(b.guest('A','cache',absent)['resident']==0,'negative page already cached')
        b.bloom_fill(255);r=b.guest('B','read',absent);require(r['before']==0,'negative consumer warm')
        require(b.guest('A','cache',absent)['resident']==0,'donor lookup faulted file data in')
        require(b.guest('A','read',absent)['hash']==r['hash'],'fallback data mismatch')
        # False negative is only a missed sharing opportunity.
        other=4095;w=b.guest('A','warm',other);b.rpc('A',op='advertise',image=c.image_id,offset=b.offset(other))
        b.bloom_fill(0);r=b.guest('B','read',other);require(r['before']==0 and r['hash']==w['hash'],'Bloom-negative fallback failed')
        logs=c.fetch_bytes('B',b.guests['B']+'/vmm.log','final-consumer-log').decode()
        require(f'PEER_EXACT_MISS offset={b.offset(absent)}' in logs,'missing exact-miss fallback')
        require(f'PEER_BLOOM_NEGATIVE offset={b.offset(other)}' in logs,'missing Bloom negative')
        for page in pages:require(f'PEER_BACKEND_READ offset={b.offset(page)} ' not in logs,'shared hit used backend')
        report.update(result='passed',false_positive_fallback=True,false_negative_fallback=True,local_cache_hit_skips_peer=True)
    except BaseException as exc:
        report.update(result='failed',error=str(exc))
        raise
    finally:
        for role,directory in reversed(c.active):
            c.fetch_bytes(role,directory+'/vmm.log','vmm-'+role)
            c.fetch_bytes(role,directory+'/fc.log','guest-'+role)
            b.stop(role,directory)
        lab.save_json(out/'report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='guest_events'},indent=2))
if __name__=='__main__':main()
