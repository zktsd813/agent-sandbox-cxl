#!/usr/bin/env python3
"""Controlled stock/patched Firecracker comparison in disposable nested VMs.

Guest timings include the full data oracle, exclude serial control, and are not
raw storage/memory bandwidth. Physical frame accounting covers pinned current
file-cache pages only; no reserved DAX capacity is released by this experiment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import time

import lab
import nfs_bench as nfs
from home_storage_bench import setup, require, json_lines
from pool_bench import PoolController, Sandbox, MIB, HERE
from pool_control import PoolControl
from trace_bench import source_fence_script

ARMS = ("stock-dram", "patched-dram-disabled", "patched-cxl-hints-unpooled",
        "patched-cxl-pooled", "stock-snapshot")
PAGE = 4096


def parse_maps(raw):
    result = []
    for line in raw.splitlines():
        m = re.fullmatch(r"([0-9a-f]+)-([0-9a-f]+) ([-rwxps]{4}) ([0-9a-f]+) (\S+) (\d+)\s*(.*)", line)
        if m:
            a,b,permissions,offset,dev,inode,path=m.groups()
            result.append(dict(start=int(a,16),end=int(b,16),permissions=permissions,
                file_offset=int(offset,16),device=dev,inode=int(inode),path=path))
    require(result,"No process VMAs parsed")
    return result


def ram_segments(mappings, memory_bytes, tier, dax_offset, existing_base=None, snapshot=None):
    """Fail closed: one fresh RAM VMA, or one identified snapshot file extent."""
    if snapshot is not None:
        selected=[m for m in mappings if m["device"]==snapshot["device"] and m["inode"]==snapshot["inode"]]
        require(selected and all(m["permissions"]=="rw-p" for m in selected),"Snapshot RAM mapping missing/private flag wrong")
        require(all(m["path"]==snapshot["path"] for m in selected),"Snapshot mapping path changed")
        bases={m["start"]-m["file_offset"] for m in selected}
        require(len(bases)==1,"Snapshot RAM file offsets imply multiple bases")
        base=bases.pop()
    elif existing_base is None:
        selected=[m for m in mappings if m["end"]-m["start"]==memory_bytes and
                  ((tier=="dram" and m["permissions"]=="rw-p" and m["inode"]==0 and m["path"]=="") or
                   (tier=="cxl" and m["permissions"]=="rw-s" and m["path"]=="/dev/dax0.0" and m["file_offset"]==dax_offset))]
        require(len(selected)==1,f"Ambiguous/missing fresh guest RAM: {selected}")
        base=selected[0]["start"]
    else:
        base=existing_base
        selected=[m for m in mappings if m["start"]<base+memory_bytes and m["end"]>base]
    selected=sorted(selected,key=lambda m:m["start"])
    cursor=base
    for m in selected:
        require(m["start"]==cursor and m["end"]<=base+memory_bytes and m["permissions"].startswith("rw"),"Guest RAM VMA coverage changed")
        cursor=m["end"]
    require(cursor==base+memory_bytes,"Incomplete guest RAM VMA coverage")
    return base,[dict(m,gpa_base=m["start"]-base,hva_base=m["start"],bytes=m["end"]-m["start"]) for m in selected]


class CompareController(PoolController):
    def __init__(self,config,out,args):
        super().__init__(config,out,args)
        self.arm=args.arm
        self.hints_enabled="cxl" in args.arm
        self.pooled=args.arm=="patched-cxl-pooled"
        self.binary="/lab/firecracker-stock" if args.arm.startswith("stock") else "/lab/firecracker"
        fixture=json.loads((Path(config["assets"])/"manifest.json").read_text())
        self.expected_binary_sha256=fixture["comparison"]["stock" if args.arm.startswith("stock") else "patched"]["sha256"]
        self.env_by_vm={}
        self.control_events=[]
        self.serial=0
        self.api_timings=[]
        self.boots=[]
        for s in self.sandboxes:
            s.tier="cxl" if self.hints_enabled else "dram"
            s.guest_vm=s.vm
            s.ram_base=None
            s.snapshot_backing=None

    def api(self,*args,**kwargs):
        start=time.monotonic_ns()
        receipt=super().api(*args,**kwargs)
        self.api_timings.append(dict(receipt,observed_api_wall_ns=time.monotonic_ns()-start))
        return receipt

    def start_vm(self,s,restore_hints=None):
        require(restore_hints is None,"Comparison does not restore pooling hints")
        s.directory=f"{self.root}/vm{s.vm}-epoch{s.epoch}"
        s.hints,s.plan,s.report=(s.directory+"/"+name+".json" for name in ("hints","plan","pool-report"))
        env={"PATH":"/bin:/sbin:/usr/bin:/usr/sbin:/lab"}
        if self.hints_enabled:
            env.update(FC_POOL_VM_ID=str(s.vm),FC_POOL_OFFSET=str(self.pool_offset),FC_POOL_BYTES=str(self.image_bytes),
                FC_POOL_IMAGE_PATH="/disk/sandbox.raw",FC_POOL_IMAGE_ID=self.image_id,FC_POOL_HINTS=s.hints,
                FC_POOL_PLAN=s.plan,FC_POOL_REPORT=s.report,FC_POOL_RUN_EPOCH=str(s.epoch),
                FC_DAX_RAM="/dev/dax0.0",FC_DAX_OFFSET=str(s.offset),FC_DAX_SNAPSHOT="1")
        self.env_by_vm[f"{s.vm}:{s.epoch}"]=env
        envline=" ".join(shlex.quote(k+"="+v) for k,v in env.items())
        self.active.append((s.role,s.directory))
        self.shell(s.role,f"mkdir -p {s.directory}\nmkfifo {s.directory}/input\n"
            f"(exec 3<>{s.directory}/input\nexec env -i {envline} taskset c {self.binary} "
            f"--no-seccomp --api-sock {s.directory}/api.sock <&3 > {s.directory}/fc.log 2>&1) &\n"+
            nfs.record_pid(s.directory)+f"n=0\nwhile [ ! -S {s.directory}/api.sock ]; do\n"
            f"read p t < {s.directory}/fc.pid\nkill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 1000 ]\nsleep .02\ndone", "start")

    def pin_threads(self,s):
        raw,name=self.shell(s.role,f"read p t < {s.directory}/fc.pid\n"
            "for d in /proc/$p/task/*; do\n"
            "name=$(cat $d/comm)\ncase \"$name\" in 'fc_vcpu 0') taskset -p 1 ${d##*/};; 'fc_vcpu 1') taskset -p 2 ${d##*/};; esac\n"
            "printf 'THREAD %s %s\\n' ${d##*/} \"$name\"\ncat $d/status | sed -n '/Cpus_allowed_list/p'\ndone", "thread-affinity")
        require("fc_vcpu 0" in raw and "fc_vcpu 1" in raw,"Missing vCPU threads for affinity")
        return name

    def command_vm(self,s,op,*args,require_cache=False):
        label=f"v{s.vm}e{s.epoch}c{len(self.commands)}"
        self.shell(s.role,"printf '%s\\n' "+shlex.quote(" ".join([op,label,*map(str,args)]))+f" > {s.directory}/input","guest-input")
        row=self.event(s,"pool_guest",label)
        row.update(operation=op,arguments=list(args))
        d=row["receipt"]
        require(d["vm"]==s.guest_vm and d["generation"]==s.generation,"Guest identity/generation mismatch")
        require(d["payload_bytes"]==self.payload_bytes,"Payload size mismatch")
        require(d["timing_clock"]=="CLOCK_MONOTONIC_RAW" and d["cpu"]==d["cpu_after"]==d["pinned_cpu"]==0,"Invalid guest operation timing")
        if op in ("check","warm"):
            require(d["payload_hash"]==self.payload_hash and d["checked_payload_words"]==self.payload_bytes//8,"Full read oracle missing")
        if require_cache:
            require(d["resident_after"]*PAGE==self.payload_bytes and d["cached_kib"]*1024>=self.minimum_cache_bytes,"File cache not fully resident")
        self.commands.append(row)
        return row

    def mapping(self,s,initial=False):
        self.shell(s.role,f"read p t < {s.directory}/fc.pid\nprintf 'PROCESS %s %s\\n' $p $t > {s.directory}/maps.capture\n"
            f"cat /proc/$p/maps >> {s.directory}/maps.capture","maps-capture")
        raw=self.fetch_bytes(s.role,s.directory+"/maps.capture","maps").decode();name=self.receipts[-1]["file"]
        process=re.findall(r"^PROCESS (\d+) (\d+)$",raw,re.M)
        require(len(process)==1,"Process identity missing")
        mappings=parse_maps(raw)
        base,segments=ram_segments(mappings,self.memory_mib*MIB,s.tier,s.offset,
            existing_base=None if initial else s.ram_base,snapshot=s.snapshot_backing)
        s.ram_base=base
        smaps_source="smaps" if initial else "smaps_rollup"
        _,smaps=self.shell(s.role,f"read p t < {s.directory}/fc.pid\ncat /proc/$p/{smaps_source}",smaps_source,120)
        _,environment=self.shell(s.role,f"read p t < {s.directory}/fc.pid\ntr '\\000' '\\n' < /proc/$p/environ","process-environment")
        raw,executable=self.shell(s.role,f"read p t < {s.directory}/fc.pid\nprintf 'PROCESS %s %s\\n' $p $t\nsha256sum /proc/$p/exe","process-executable")
        require(re.findall(r"^([0-9a-f]{64})  /proc/\d+/exe\s*$",raw,re.M)==[self.expected_binary_sha256],"Running VMM executable differs from arm manifest")
        return {"vm":s.vm,"role":s.role,"pid":int(process[0][0]),"process_start_time":int(process[0][1]),
                "maps_file":name,"smaps_file":smaps,"smaps_kind":smaps_source,"environment_file":environment,
                "executable_file":executable,"segments":segments,"ram_base":base}

    def boot_vm(self,s):
        self.start_vm(s)
        components={"/machine-config":{"vcpu_count":self.vcpus,"mem_size_mib":self.memory_mib},
            "/boot-source":{"kernel_image_path":"/lab/l2-vmlinux","initrd_path":"/lab/pool-compare-l2-initramfs.cpio",
                "boot_args":f"console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init pool.vm={s.guest_vm}"},
            "/drives/data":{"drive_id":"data","path_on_host":"/disk/sandbox.raw","is_root_device":False,
                "is_read_only":True,"cache_type":"Writeback","io_engine":"Async"}}
        for path,body in components.items():self.api(s.role,s.directory,path,"PUT",body)
        self.api(s.role,s.directory,"/actions","PUT",{"action_type":"InstanceStart"})
        ready=self.event(s,"pool_ready")
        require(ready["receipt"]["vm"]==s.guest_vm,"Wrong fresh guest identity")
        pin=self.pin_threads(s)
        mapping=self.mapping(s,initial=True)
        warm=self.command_vm(s,"warm",require_cache=True)
        self.boots.append({"vm":s.vm,"ready":ready,"warm":warm,"mapping":mapping,"affinity_file":pin,"components":components})

    def hostwarm(self,role="A"):
        raw,name=self.shell(role,"/lab/pool-compare-pfn hostwarm /disk/sandbox.raw","hostwarm",180)
        rows=json_lines(raw,"POOL_COMPARE_HOSTWARM_JSON ")
        require(len(rows)==1 and rows[0]["all_resident"] and rows[0]["bytes_read"]==self.image_bytes,"Host backing not prewarmed")
        return {"file":name,"receipt":rows[0]}

    def treatment(self,s,control,phase):
        if not self.hints_enabled:return None
        start=time.monotonic_ns();api_start=len(self.api_timings)
        self.pause(s)
        paused=time.monotonic_ns()
        if self.pooled:
            admission=control.admit(s)
            self.latest_admissions[s.vm]=admission
        else:
            plan={"vm_id":str(s.vm),"run_epoch":str(s.epoch),"pool_path":"/dev/dax0.0","pages":[]}
            self.write_json(s.role,s.plan,plan)
            self.api(s.role,s.directory,"/actions","PUT",{"action_type":"PoolCache"})
            admission={"vmm":self.fetch_json(s.role,s.report,"unpooled-report")}
            admission["vmm_file"]=self.receipts[-1]["file"]
            require(not admission["vmm"]["mapped_pages"],"Unpooled control installed data mappings")
        self.resume(s)
        end=time.monotonic_ns()
        row={"vm":s.vm,"phase":phase,"admission":admission,"observed_control_wall_ns":end-start,
             "observed_after_pause_until_resume_ns":end-paused,"apis":self.api_timings[api_start:]}
        self.control_events.append(row)
        return row

    def cache_entries(self,s,pinned):
        self.serial+=1;prefix=f"/run/compare-pfn-{self.serial}"
        label=pinned["receipt"]["label"]
        self.shell(s.role,"grep -E "+shlex.quote("^POOL_CACHE_(GPA|RANGE) "+label+" ")+" "+
            shlex.quote(s.directory+"/fc.log")+" > "+prefix+".cache","cache-gpa-select")
        data=self.fetch_bytes(s.role,prefix+".cache","cache-gpas");name=self.receipts[-1]["file"]
        entries=[]
        for line in data.decode().splitlines():
            f=line.split()
            require(len(f) in (4,5) and f[1]==label and f[0] in ("POOL_CACHE_RANGE","POOL_CACHE_GPA"),"Malformed cache ranges")
            require((f[0]=="POOL_CACHE_RANGE")== (len(f)==5),"Cache record shape mismatch")
            off,gpa=map(int,f[2:4]);count=int(f[4]) if len(f)==5 else 1
            require(0<count<=self.payload_bytes//PAGE and len(entries)+count<=self.payload_bytes//PAGE,"Cache range outside payload")
            entries.extend((off+i*PAGE,gpa+i*PAGE) for i in range(count))
        require([a for a,b in entries]==list(range(0,self.payload_bytes,PAGE)) and len({b for a,b in entries})==len(entries),"Cache GPA coverage/uniqueness invalid")
        return entries,name,prefix

    def scan(self,s,pinned=None,reuse=None):
        mapping=self.mapping(s)
        if reuse is None:
            require(pinned["receipt"]["block_read_ios"]==0 and pinned["receipt"]["resident_before"]*PAGE==self.payload_bytes,"Cache pin caused refill")
            entries,cache_file,prefix=self.cache_entries(s,pinned)
            require(all(0<=gpa<self.memory_mib*MIB and gpa%PAGE==0 for _,gpa in entries),"GPA outside guest RAM")
            rows="".join(f"{off} {gpa} {s.ram_base+gpa}\n" for off,gpa in entries).encode()
            self.write_bytes(s.role,prefix+".rows",rows,"pfn-rows-upload")
            rows_file=self.raw("pfn-rows",rows.decode())
        else:
            prefix=reuse["remote_prefix"];rows_file=reuse["rows_file"];cache_file=reuse["cache_gpa_file"]
        raw,summary_file=self.shell(s.role,f"/lab/pool-compare-pfn {mapping['pid']} {prefix}.rows {prefix}.pfns","pfn-scan",180)
        summary=json_lines(raw,"POOL_COMPARE_PFN_JSON ")
        require(len(summary)==1 and summary[0]["all_checked"] and summary[0]["rows"]*PAGE==self.payload_bytes,"Incomplete actual PFN scan")
        require(summary[0]["pid"]==mapping["pid"] and summary[0]["process_start_time"]==mapping["process_start_time"],"Process incarnation changed")
        data=self.fetch_bytes(s.role,prefix+".pfns","pfn-values");pfns_file=self.receipts[-1]["file"]
        values=[list(map(int,line.split())) for line in data.decode().splitlines()]
        require(len(values)==self.payload_bytes//PAGE and len({r[3] for r in values})==len(values),"PFN aliases within unique file")
        inputs=[list(map(int,line.split())) for line in (self.out/rows_file).read_text().splitlines()]
        require(all(len(v)==5 and v[:3]==i and v[4]&(1<<63) and not v[4]&(1<<62) and
                    (v[4]&((1<<55)-1))==v[3] and v[3]>0 for i,v in zip(inputs,values)),"PFN scanner output does not bind original input/pagemap")
        return {**mapping,"cachemap":pinned,"cache_gpa_file":cache_file,"rows_file":rows_file,"pfns_file":pfns_file,
            "summary_file":summary_file,"summary":summary[0],"remote_prefix":prefix,"current_file_cache":reuse is None},values

    def audit_group(self,sandboxes):
        pinned={};paused=[]
        try:
            for s in sandboxes:
                pinned[s.vm]=self.command_vm(s,"cachemap",require_cache=True);self.pause(s);paused.append(s)
            _,frozen=self.shell("A","echo POOL_COMPARE_GROUP_FROZEN "+" ".join(str(s.vm) for s in sandboxes),"group-frozen")
            results=[];values=[]
            for s in sandboxes:
                result,value=self.scan(s,pinned=pinned[s.vm]);results.append(result);values.append(value)
            sets=[{r[3] for r in value} for value in values]
            same=sum(len({v[i][3] for v in values})==1 for i in range(len(values[0])))
            return {"frozen_file":frozen,"vms":results,"union_pages":len(set.union(*sets)),
                    "intersection_pages":len(set.intersection(*sets)),"same_offset_shared_pages":same}
        finally:
            for s in paused:self.resume(s)
            for s in sandboxes:
                if s.vm in pinned:self.command_vm(s,"cacheunmap",require_cache=True)

    def audit_one(self,s):
        group=self.audit_group([s])
        return group["vms"][0]


def timed_round(c,s,control,round_number):
    treatment=c.treatment(s,control,f"round-{round_number}-before-hot")
    before=c.audit_one(s)
    if c.pooled:
        values=[list(map(int,line.split())) for line in (c.out/before["pfns_file"]).read_text().splitlines()]
        shared=sum(row[3]==c.initial_shared_pfns.get(row[0]) for row in values)
        require(shared*PAGE>=128*MIB,"Pooled hot sample lost its actual shared-frame treatment")
        before.update(initial_shared_pfns_file=c.initial_shared_pfns_file,shared_with_initial_group_pages=shared)
    hot=c.command_vm(s,"check",require_cache=True)
    require(hot["receipt"]["block_read_ios"]==0,"Warm sample used block IO")
    s.generation+=1;write=c.command_vm(s,"write",s.generation,require_cache=True)
    recycle=c.command_vm(s,"recycle",224)
    require(recycle["receipt"]["resident_after"]==0 and recycle["receipt"]["checked_churn_words"]==224*MIB//8,"Recycle did not fully validate/evict")
    c.pause(s)
    try:after,after_values=c.scan(s,reuse=before)
    finally:c.resume(s)
    before_values=[list(map(int,line.split())) for line in (c.out/before["pfns_file"]).read_text().splitlines()]
    changed=sum(a[3]!=b[3] for a,b in zip(before_values,after_values))
    if c.pooled:require(changed>0,"Pooled recycle did not exercise host CoW")
    hostwarm=c.hostwarm()
    cold=c.command_vm(s,"warm",require_cache=True)
    require(cold["receipt"]["resident_before"]==0 and cold["receipt"]["block_read_ios"]>0,"Cold sample not guest-cold/block-backed")
    readmit=c.treatment(s,control,f"round-{round_number}-after-refill")
    # Observe correctness of every peer after this VM's private writes/reuse.
    peers=[c.command_vm(peer,"check",require_cache=True) for peer in c.sandboxes if peer.vm!=s.vm]
    return {"round":round_number,"vm":s.vm,"hot":hot,"private_write":write,"recycle":recycle,
        "cold_refill":cold,"hostwarm":hostwarm,"pfn_before":before,"pfn_after_recycle":after,
        "changed_pfns":changed,"control_events":[x for x in (treatment,readmit) if x],"peer_checks":peers}


def snapshot_proof(c,report,save):
    donor=c.sandboxes[0]
    c.boot_vm(donor)
    donor.generation=2;c.command_vm(donor,"write",2,require_cache=True)
    before=c.command_vm(donor,"identity",require_cache=True)
    c.pause(donor)
    snapshot=c.root+"/common.vmstate";ram=c.root+"/common.ram"
    create=c.api("A",donor.directory,"/snapshot/create","PUT",{"snapshot_type":"Full","snapshot_path":snapshot,"mem_file_path":ram})
    raw,inode_file=c.shell("A",f"stat -c 'SNAPSHOT %d %i %s' {ram}\nsha256sum {snapshot} {ram}","snapshot-inode",180)
    values=re.findall(r"^SNAPSHOT (\d+) (\d+) (\d+)\s*$",raw,re.M)
    require(len(values)==1 and int(values[0][2])==512*MIB,"Snapshot RAM size invalid")
    # maps uses hexadecimal major/minor, unlike stat's decimal dev_t.
    import os
    dev,inode,_=map(int,values[0]);identity={"device":f"{os.major(dev):02x}:{os.minor(dev):02x}","inode":inode,"path":ram}
    c.fetch_bytes("A",donor.directory+"/fc.log","donor-retired-log")
    nfs.stop_fc(c.vms["A"],donor.directory)
    fence_raw,fence_file=c.shell("A",source_fence_script(donor.directory),"donor-fence")
    require("FENCED" in fence_raw,"Donor not fenced")
    report["snapshot"]={"before":before,"create":create,"inode_file":inode_file,"identity":identity,"fence_file":fence_file}
    for s in c.sandboxes:
        s.epoch+=1;s.guest_vm=1;s.generation=2;s.ram_base=None;s.snapshot_backing=identity
        c.start_vm(s)
        c.api("A",s.directory,"/snapshot/load","PUT",{"snapshot_path":snapshot,"mem_backend":{"backend_path":ram,"backend_type":"File"},"enable_diff_snapshots":False,"resume_vm":False})
        c.resume(s);c.pin_threads(s)
        mapping=c.mapping(s,initial=True)
        checked=c.command_vm(s,"check",require_cache=True)
        for key in ("vm","boot_id","pid","cookie","generation"):
            require(checked["receipt"][key]==before["receipt"][key],"Snapshot clone lost guest identity")
        report.setdefault("clones",[]).append({"controller_vm":s.vm,"mapping":mapping,"check":checked})
    report["shared_snapshot_group"]=c.audit_group(c.sandboxes)
    require(report["shared_snapshot_group"]["same_offset_shared_pages"]*PAGE>=128*MIB,"Stock shared snapshot did not share current file cache")
    save()
    # Demonstrate private clone isolation, then ordinary stock full restore to NFS.
    s=c.sandboxes[0];s.generation+=1
    report["clone_write"]=c.command_vm(s,"write",s.generation,require_cache=True)
    report["clone_peers"]=[c.command_vm(peer,"check",require_cache=True) for peer in c.sandboxes[1:]]
    continuity=c.command_vm(s,"identity",require_cache=True);c.pause(s)
    localstate=c.root+"/remote.vmstate";localram=c.root+"/remote.ram"
    c.api("A",s.directory,"/snapshot/create","PUT",{"snapshot_type":"Full","snapshot_path":localstate,"mem_file_path":localram})
    remote=c.storage+"/stock-remote"
    _,transport=c.shell("A",f"mkdir -p {c.storage}\ncp {localstate} {remote}.vmstate\ncp {localram} {remote}.ram\n"
        f"cmp {localstate} {remote}.vmstate\ncmp {localram} {remote}.ram\necho STOCK_SNAPSHOT_TRANSPORT_VERIFIED","snapshot-transport",600)
    c.fetch_bytes("A",s.directory+"/fc.log","stock-retired-log")
    nfs.stop_fc(c.vms["A"],s.directory)
    fence_raw,fence=c.shell("A",source_fence_script(s.directory),"stock-source-fence")
    require("FENCED" in fence_raw,"Stock migration source not fenced")
    s.role="B";s.epoch+=1;s.ram_base=None;s.snapshot_backing=None
    c.shell("B",f"mkdir -p {c.root}\ncp {remote}.vmstate {localstate}\ncp {remote}.ram {localram}\n"
        f"cmp {remote}.vmstate {localstate}\ncmp {remote}.ram {localram}\necho STOCK_SNAPSHOT_RECEIVE_VERIFIED","snapshot-receive",600)
    c.start_vm(s)
    c.api("B",s.directory,"/snapshot/load","PUT",{"snapshot_path":localstate,"mem_backend":{"backend_path":localram,"backend_type":"File"},"enable_diff_snapshots":False,"resume_vm":False})
    c.resume(s);c.pin_threads(s)
    restored=c.command_vm(s,"check",require_cache=True)
    for key in ("vm","boot_id","pid","cookie","generation"):
        require(restored["receipt"][key]==continuity["receipt"][key],"Ordinary stock NFS restore lost identity")
    evicted=c.command_vm(s,"recycle",224)
    require(evicted["receipt"]["resident_after"]==0,"Stock NFS test did not empty guest cache")
    raw,nfs_before=c.shell("B","sync\necho 3 > /proc/sys/vm/drop_caches\ncat /proc/net/rpc/nfs","nfs-before")
    reads=int(next(line.split()[8] for line in raw.splitlines() if line.startswith("proc3 ")))
    refill=c.command_vm(s,"warm",require_cache=True)
    raw,nfs_after=c.shell("B","cat /proc/net/rpc/nfs","nfs-after")
    delta=int(next(line.split()[8] for line in raw.splitlines() if line.startswith("proc3 ")))-reads
    require(delta>0 and refill["receipt"]["block_read_ios"]>0,"Stock NFS refill did not use network block backing")
    report["stock_nfs_restore"]={"continuity":continuity,"transport_file":transport,"fence_file":fence,"restored":restored,
        "evicted":evicted,"refill":refill,"nfs_before_file":nfs_before,"nfs_after_file":nfs_after,"nfs_read_delta":delta}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm",choices=ARMS,required=True)
    parser.add_argument("--rounds",type=int,default=5)
    args=parser.parse_args()
    args.memory_mib,args.vcpus,args.sandboxes,args.minimum_cache_mib,args.io_engine=512,2,3,128,"Async"
    require(1<=args.rounds<=10,"Invalid repeat count")
    run,config=lab.current_run();out=run/("compare-"+args.arm+"-"+str(time.time_ns()));out.mkdir()
    c=CompareController(config,out,args)
    manifest=json.loads((Path(config["assets"])/"manifest.json").read_text())
    sources=("pool_compare_bench.py","pool_compare_run.py","pool_compare_prepare.py","pool_compare_probe.c","pool_compare_pfn.c",
        "pool_bench.py","pool_control.py","home_storage_bench.py","lab.py","nfs_bench.py","trace_bench.py","firecracker.patch")
    (out/"measured-sources").mkdir()
    for name in sources:shutil.copy2(HERE/name,out/"measured-sources"/name)
    report={"schema":"pool-compare-arm-v1","result":"running","arm":args.arm,"config":config,
        "configuration":{"binary_sha256":manifest["comparison"]["stock" if args.arm.startswith("stock") else "patched"]["sha256"],
            "env_by_vm":c.env_by_vm,"memory_mib":512,"vcpus":2,"io_engine":"Async","rounds":args.rounds,"no_seccomp":True,
            "cold_definition":"guest file cache zero after recycle224; host backing fully prewarmed; not storage cold",
            "independent_guest_boots":3,"observations_per_operation":3*args.rounds},
        "fixture_manifest":manifest,"source_hashes":{name:lab.sha256(HERE/name) for name in sources},"samples":[],
        "receipts":c.receipts,"commands":c.commands,"control_events":c.control_events,"api_timings":c.api_timings,"boots":c.boots}
    if args.arm=="stock-snapshot":
        report["configuration"].update(independent_guest_boots=1,restored_clones=3,rounds=0,observations_per_operation=0)
    def save():lab.save_json(out/"report.json",report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report["affinity"]=nfs.pin_vms(config,argparse.Namespace(host_cpus_a="0-3",host_cpus_b="4-7"))
        setup(c)
        require(all(x["sha256"]==c.image_id for x in c.last_image_hashes.values()),"Backing identity mismatch")
        report["initial_image_hashes"]=dict(c.last_image_hashes)
        for role in ("A","B"):
            raw,name=c.shell(role,"echo 1048576 > /proc/sys/vm/max_map_count\ncat /proc/sys/vm/max_map_count\n"
                "if [ -e /sys/kernel/mm/transparent_hugepage/enabled ]; then echo never > /sys/kernel/mm/transparent_hugepage/enabled; fi\n"
                "if [ -e /sys/kernel/mm/ksm/run ]; then test $(cat /sys/kernel/mm/ksm/run) = 0; fi\n"
                "uname -a\ncat /proc/cmdline\nfor p in /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/ksm/run; do if [ -f $p ]; then echo POLICY $p; cat $p; else echo ABSENT $p; fi; done","system-policy")
            report.setdefault("system_policy",{})[role]={"file":name}
        control=PoolControl(c)
        if c.hints_enabled:report["canonical_publication"]=control.setup()
        if args.arm=="stock-snapshot":snapshot_proof(c,report,save)
        else:
            for s in c.sandboxes:c.hostwarm();c.boot_vm(s)
            require(len({b["ready"]["receipt"]["boot_id"] for b in c.boots})==3,"Primary arm did not independently boot three guests")
            for s in c.sandboxes:c.treatment(s,control,"initial")
            report["initial_group_pfn"]=c.audit_group(c.sandboxes)
            if c.pooled:
                require(report["initial_group_pfn"]["same_offset_shared_pages"]*PAGE>=128*MIB,"Pooled group lacks actual sharing")
                references=[[list(map(int,line.split())) for line in (out/v["pfns_file"]).read_text().splitlines()]
                            for v in report["initial_group_pfn"]["vms"]]
                c.initial_shared_pfns={rows[0][0]:rows[0][3] for rows in zip(*references) if len({r[3] for r in rows})==1}
                c.initial_shared_pfns_file=c.raw("initial-shared-pfns","".join(f"{off} {pfn}\n" for off,pfn in sorted(c.initial_shared_pfns.items())))
            else:require(report["initial_group_pfn"]["union_pages"]==3*c.payload_bytes//PAGE,"Independent unpooled file caches unexpectedly share")
            save()
            for round_number in range(args.rounds):
                order=c.sandboxes[round_number%3:]+c.sandboxes[:round_number%3]
                for s in order:
                    report["samples"].append(timed_round(c,s,control,round_number));save()
                    print(f"COMPARE_SAMPLE {args.arm} round={round_number} vm={s.vm}",flush=True)
            report["final_group_pfn"]=c.audit_group(c.sandboxes)
            if c.pooled:require(report["final_group_pfn"]["same_offset_shared_pages"]*PAGE>=128*MIB,"Final pooled group lost sharing")
            report["final_checks"]=[c.command_vm(s,"check",require_cache=True) for s in c.sandboxes]
        if c.hints_enabled:report["canonical_check"]=c.verify_canonical()
        report["result"]="passed"
    except BaseException as exc:
        report["result"]="failed";report["error"]=repr(exc);raise
    finally:
        for s in c.sandboxes:
            if s.directory:
                try:c.fetch_bytes(s.role,s.directory+"/fc.log",f"vm{s.vm}-final-log");nfs.stop_fc(c.vms[s.role],s.directory)
                except Exception as exc:report.setdefault("cleanup_errors",[]).append(repr(exc))
        save();print("POOL_COMPARE_REPORT "+str(out/"report.json"),flush=True)


if __name__=="__main__":main()
