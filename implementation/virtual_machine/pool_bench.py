#!/usr/bin/env python3
"""Real multi-microVM immutable data-pool lifecycle correctness experiment.

Uses full logical RAM snapshots at every boundary, including CXL handoff, so
private CoW pages and remapped data are materialized. This is not a zero-copy
handoff benchmark. The private L1 NFS mount supplies only host backing files.
"""
import argparse
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import time

import lab
import nfs_bench as nfs
from home_storage_bench import Controller, setup, require, json_lines
from trace_bench import source_fence_script

HERE = Path(__file__).resolve().parent
MIB = 1024 * 1024


@dataclass
class Sandbox:
    vm: int
    role: str = "A"
    tier: str = "dram"
    directory: str = ""
    epoch: int = 0
    generation: int = 1
    hints: str = ""
    plan: str = ""
    report: str = ""
    offset: int = 0


class PoolController(Controller):
    def __init__(self, config, out, args):
        super().__init__(config, out, args.memory_mib, args.vcpus)
        self.root = "/run/pool-" + os.urandom(8).hex()
        self.storage = "/disk/" + self.root.rsplit("/", 1)[1]
        self.sandboxes = [Sandbox(vm=i+1, offset=2*MIB+i*args.memory_mib*MIB) for i in range(args.sandboxes)]
        self.pool_offset, self.bloom_offset = 2048*MIB, 3072*MIB
        manifest = json.loads((Path(config["assets"])/"manifest.json").read_text())
        self.image_id = manifest["pool"]["image"]["sha256"]
        self.image_bytes = manifest["pool"]["image"]["bytes"]
        self.payload_extents = manifest["pool"]["image"]["payload_extents"]
        self.payload_hash = manifest["pool"]["image"]["generator"]["hash"]
        self.payload_bytes = manifest["pool"]["image"]["files"]["payload.bin"]["bytes"]
        self.cache_gpa_audit = manifest["pool"].get("guest_cache_gpa_audit",False)
        self.commands, self.pool_events = [], []
        self.latest_admissions = {}
        self.last_image_hashes = {}
        self.admission_negative_tests = []
        self.runtime_safety_negatives = []
        self.minimum_cache_bytes = args.minimum_cache_mib * MIB
        self.io_engine = args.io_engine

    def fetch_bytes(self, role, path, kind="fetch"):
        data, integrity = lab.migration_trace_fetch(self.vms[role], self.config, path)
        name = f"{len(self.receipts):04d}-{kind}.bin"
        (self.out/name).write_bytes(data)
        self.receipts.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, "integrity": integrity})
        return data

    def image_hash(self,role):
        measured=super().image_hash(role)
        self.last_image_hashes[role]=measured
        return measured

    def fetch_json(self, role, path, kind="json"):
        return json.loads(self.fetch_bytes(role, path, kind))

    def write_bytes(self, role, path, data, kind="write"):
        # Owned ivshmem scratch upload avoids giant serial commands. This region
        # is separate from CXL RAM/pool/Bloom and all transfers are sequential.
        compressed = gzip.compress(data)
        offset = 4*MIB
        require(len(compressed) <= self.config["shared_mib"]*MIB-offset, "Scratch upload exceeds ivshmem")
        event = next(e for e in lab.events(Path(self.vms[role]["serial_log"]))
                     if e.get("event") == "shared_memory" and e.get("nonce") == self.config["nonce"])
        bdf = event["pci_bdf"]
        require(re.fullmatch(r"[0-9a-fA-F:.]+", bdf), "Invalid scratch PCI BDF")
        raw, _ = self.shell(role, f"sed -n '3p' /sys/bus/pci/devices/{bdf}/resource", "scratch-resource")
        rows = [line.split() for line in raw.splitlines() if re.fullmatch(r"\s*0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s*", line)]
        require(len(rows) == 1, "Scratch BAR missing")
        start, end, flags = (int(v, 16) for v in rows[0])
        require(flags & 0x200 and end-start+1 == self.config["shared_mib"]*MIB and start%4096 == 0, "Scratch BAR invalid")
        with Path(self.config["backing"]).open("r+b", buffering=0) as stream:
            stream.seek(offset)
            require(stream.write(compressed) == len(compressed), "Scratch write short")
        qpath = shlex.quote(path)
        digest = hashlib.sha256(data).hexdigest()
        self.shell(role, f"dd if=/dev/mem bs=4096 skip={(start+offset)//4096} count={math.ceil(len(compressed)/4096)} 2>/dev/null | "
                   f"head -c {len(compressed)} | gzip -dc > {qpath}\n"
                   f"printf '%s  %s\\n' {shlex.quote(digest)} {qpath} | sha256sum -c -", kind, 120)

    def write_json(self, role, path, value, kind="write-json"):
        return self.write_bytes(role, path, json.dumps(value, separators=(",", ":")).encode(), kind)

    def start_vm(self, s, restore_hints=None):
        s.directory = f"{self.root}/vm{s.vm}-epoch{s.epoch}"
        s.hints, s.plan, s.report = (s.directory+"/"+name+".json" for name in ("hints", "plan", "pool-report"))
        env = {"FC_DAX_SNAPSHOT":"1" if s.tier == "cxl" else "0",
               "FC_POOL_VM_ID":str(s.vm),
               "FC_POOL_OFFSET":str(self.pool_offset), "FC_POOL_BYTES":str(self.image_bytes),
               "FC_POOL_IMAGE_PATH":"/disk/sandbox.raw", "FC_POOL_IMAGE_ID":self.image_id,
               "FC_POOL_HINTS":s.hints, "FC_POOL_PLAN":s.plan, "FC_POOL_REPORT":s.report,
               "FC_POOL_RUN_EPOCH":str(s.epoch)}
        if s.tier == "cxl":
            env.update(FC_DAX_RAM="/dev/dax0.0", FC_DAX_OFFSET=str(s.offset))
        if restore_hints:
            local_hints=s.directory+"/restore-hints.json"
            self.shell(s.role,f"mkdir -p {s.directory}\ncp {shlex.quote(restore_hints)} {local_hints}\n"
                       f"cmp {shlex.quote(restore_hints)} {local_hints}","local-owner-hints",600)
            env["FC_POOL_HINTS_RESTORE"] = local_hints
        reset = " ".join("-u "+name for name in ("FC_DAX_RAM", "FC_DAX_OFFSET", "FC_DAX_DIRECT", "FC_DAX_REUSE", "FC_DAX_HEAP", "FC_DAX_HEAP_OFFSET", "FC_DAX_HEAP_BYTES", "FC_DAX_FIXED_HVA", "FC_POOL_HINTS_RESTORE"))
        envline = " ".join(shlex.quote(k+"="+v) for k,v in env.items())
        self.active.append((s.role, s.directory))
        self.shell(s.role, f"mkdir -p {s.directory}\nmkfifo {s.directory}/input\n"
                   f"(exec 3<>{s.directory}/input\nexec env {reset} {envline} /lab/firecracker "
                   f"--no-seccomp --api-sock {s.directory}/api.sock <&3 > {s.directory}/fc.log 2>&1) &\n" +
                   nfs.record_pid(s.directory) + f"n=0\nwhile [ ! -S {s.directory}/api.sock ]; do\nread p t < {s.directory}/fc.pid\n"
                   "kill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 1000 ]\nsleep .02\ndone", "start")

    def event(self, s, event, label=None):
        pattern = f'"event":"{event}"' + (f',"label":"{label}"' if label else "")
        complete = re.escape(pattern)+r".*}[[:space:]]*$"
        raw, name = self.shell(s.role, f"n=0\nwhile ! grep -Eq {shlex.quote(complete)} {s.directory}/fc.log; do\n"
                   f"read p t < {s.directory}/fc.pid\nkill -0 \"$p\"\n"
                   f"! grep -Eq 'POOL_GUEST_FATAL|Kernel panic' {s.directory}/fc.log\n"
                   "n=$((n+1))\n[ $n -lt 9000 ]\nsleep .02\ndone\n"
                   f"grep -E {shlex.quote(complete)} {s.directory}/fc.log", "guest", 200)
        rows = [json.loads(line) for line in raw.splitlines() if line.startswith('{"event":')]
        rows = [r for r in rows if r["event"] == event and (not label or r.get("label") == label)]
        require(len(rows) == 1 and rows[0].get("ok", True), "Missing, duplicate or failed guest event")
        return {"file": name, "receipt": rows[0], "vm":s.vm, "role":s.role, "tier":s.tier, "epoch":s.epoch}

    def command_vm(self, s, op, *args, require_cache=False):
        label = f"v{s.vm}e{s.epoch}c{len(self.commands)}"
        cmd = " ".join([op,label,*map(str,args)])
        self.shell(s.role, f"printf '%s\\n' {shlex.quote(cmd)} > {s.directory}/input", "guest-input")
        row = self.event(s,"pool_guest",label)
        row.update(operation=op, arguments=list(args))
        data = row["receipt"]
        require(data["vm"] == s.vm and data["generation"] == s.generation, "Guest identity/generation mismatch")
        require(data["payload_bytes"] == self.payload_bytes, "Payload size mismatch")
        if op in ("check", "warm"):
            require(data["payload_hash"] == self.payload_hash and data["checked_payload_words"] == self.payload_bytes//8, "Full payload oracle missing")
        if require_cache:
            require(data["resident_after"]*4096 >= self.minimum_cache_bytes and data["cached_kib"]*1024 >= self.minimum_cache_bytes,
                    "Guest file page cache below required size")
        self.commands.append(row)
        return row

    def boot_vm(self, s):
        self.start_vm(s)
        components = {"/machine-config":{"vcpu_count":self.vcpus,"mem_size_mib":self.memory_mib},
            "/boot-source":{"kernel_image_path":"/lab/l2-vmlinux","initrd_path":"/lab/pool-l2-initramfs.cpio",
                            "boot_args":f"console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init pool.vm={s.vm}"},
            "/drives/data":{"drive_id":"data","path_on_host":"/disk/sandbox.raw","is_root_device":False,
                            "is_read_only":True,"cache_type":"Writeback","io_engine":self.io_engine}}
        for path, body in components.items(): self.api(s.role,s.directory,path,"PUT",body)
        self.api(s.role,s.directory,"/actions","PUT",{"action_type":"InstanceStart"})
        ready = self.event(s,"pool_ready")
        require(ready["receipt"]["vm"] == s.vm, "Booted wrong sandbox")
        self.command_vm(s,"warm",require_cache=True)

    def pause(self,s): self.api(s.role,s.directory,"/vm","PATCH",{"state":"Paused"})
    def resume(self,s): self.api(s.role,s.directory,"/vm","PATCH",{"state":"Resumed"})

    def rejected_api(self,s,path,body,case):
        raw,name=self.shell(s.role,f"/lab/shared-probe api {s.directory}/api.sock PUT {shlex.quote(path)} "
                            +shlex.quote(json.dumps(body))+" 400","negative-api")
        rows=json_lines(raw,"LAB_API_JSON ")
        require(len(rows)==1 and rows[0]["status"]==400,"Unsafe API request was accepted: "+case)
        row={"case":case,"vm":s.vm,"epoch":s.epoch,"file":name,"status":400}
        self.runtime_safety_negatives.append(row)
        return row

    def verify_canonical(self):
        raw,name=self.shell("A",f"/lab/pool-dax-copy verify /disk/sandbox.raw /dev/dax0.0 {self.pool_offset} {self.image_bytes}","canonical-verify",180)
        rows=json_lines(raw,"POOL_DAX_COPY_JSON ")
        require(len(rows)==1 and rows[0]["full_compare"] and rows[0]["result"]=="passed","Canonical image mutated")
        image=self.image_hash("A")
        require(image["sha256"]==self.image_id,"Backing image mutated")
        return {"file":name,"receipt":rows[0],"image":image}

    def admit(self, s, control, phase):
        record = control.admit(s)
        self.latest_admissions[s.vm] = record
        self.pool_events.append({"vm":s.vm,"epoch":s.epoch,"role":s.role,"tier":s.tier,"phase":phase,"report":record})
        return record

    def reclaim_refill(self, s, control, nfs_check=False):
        evicted=self.command_vm(s,"evict")
        require(evicted["receipt"]["resident_after"]==0,"Payload eviction did not empty cache")
        self.command_vm(s,"churn",224)
        self.pause(s)
        reclaimed_audit=control.audit(s,self.latest_admissions[s.vm],require_shared=False) if s.tier=="cxl" else None
        if reclaimed_audit is not None:
            require(reclaimed_audit["private_pages"]>0,"Guest page reuse did not exercise host CoW")
        stale=self.admit(s,control,"stale-after-reclaim")
        if s.tier=="cxl":
            require(stale["vmm"]["rejected_content"]>0,"Reused guest candidates were not rejected by full byte comparison")
        self.resume(s)
        before=None
        if nfs_check:
            require(s.role=="B","NFS check must execute on remote node B")
            raw,nfs_before_file=self.shell("B","sync\necho 3 > /proc/sys/vm/drop_caches\ncat /proc/net/rpc/nfs","nfs-cold-before")
            rows=[line.split() for line in raw.splitlines() if line.startswith("proc3 ")]
            require(len(rows)==1,"NFS counters missing")
            before=int(rows[0][8])
        cold=self.command_vm(s,"warm",require_cache=True)
        require(cold["receipt"]["block_read_ios"]>0,"Cold read did not use virtio-blk")
        if nfs_check:
            raw,nfs_after_file=self.shell("B","cat /proc/net/rpc/nfs","nfs-cold-after")
            rows=[line.split() for line in raw.splitlines() if line.startswith("proc3 ")]
            require(len(rows)==1,"NFS counters missing")
            cold["nfs_read_delta"]=int(rows[0][8])-before
            cold["nfs_before_file"],cold["nfs_after_file"]=nfs_before_file,nfs_after_file
            require(cold["nfs_read_delta"]>0,"Remote refill did not issue host NFS READ")
        self.pause(s); admission=self.admit(s,control,"refilled-"+s.tier); self.resume(s)
        hot=self.command_vm(s,"check",require_cache=True)
        require(hot["receipt"]["block_read_ios"]==0,"Hot reread missed guest cache")
        audit=control.audit(s,admission) if s.tier=="cxl" else None
        if audit is not None:
            require(audit["shared_bytes"]>=self.minimum_cache_bytes,"Refilled cache physical sharing below required size")
        return {"evicted":evicted,"cold":cold,"hot":hot,"reclaimed_pfn_audit":reclaimed_audit,"stale_admission":stale,"pfn_audit":audit}

    def transition(self, s, role, tier, control):
        before = self.command_vm(s,"identity",require_cache=True)
        self.pause(s)
        old_role, old_tier, old_dir, old_hints = s.role,s.tier,s.directory,s.hints
        transfer = f"{self.storage}/vm{s.vm}-epoch{s.epoch}"
        self.shell(old_role,f"mkdir -p {transfer}","transfer-directory")
        # A regular destination forces FC to dump logical HVA bytes, including
        # canonical overlays and host-private CoW, instead of copying raw DAX.
        self.api(old_role,old_dir,"/snapshot/create","PUT",{"snapshot_type":"Full",
                 "snapshot_path":old_dir+"/state-snapshot","mem_file_path":old_dir+"/ram-snapshot"})
        # Keep bounded control API work on local tmpfs. Copy the completed
        # artifacts through local/NFS storage separately, with a transfer-sized
        # timeout and byte comparison while the source remains paused.
        _,transport_file=self.shell(old_role,
            f"cp {old_dir}/state-snapshot {transfer}/state\n"
            f"cp {old_dir}/ram-snapshot {transfer}/ram\n"
            f"cp {old_hints} {transfer}/hints.json\n"
            f"cmp {old_dir}/state-snapshot {transfer}/state\n"
            f"cmp {old_dir}/ram-snapshot {transfer}/ram\n"
            f"cmp {old_hints} {transfer}/hints.json\nsync\n"
            f"rm {old_dir}/ram-snapshot\necho POOL_SNAPSHOT_TRANSPORT_FULL_COMPARE_PASSED",
            "snapshot-transport",600)
        nfs.stop_fc(self.vms[old_role],old_dir)
        raw, fence = self.shell(old_role,source_fence_script(old_dir),"fence")
        require("FENCED " in raw,"Source not fenced")
        s.role,s.tier,s.epoch = role,tier,s.epoch+1
        memory = transfer+"/ram"
        imported = None
        if tier == "cxl":
            raw, name = self.shell(role,f"/lab/pool-dax-copy import {memory} /dev/dax0.0 {s.offset} {self.memory_mib*MIB}","import",600)
            rows = json_lines(raw,"POOL_DAX_COPY_JSON ")
            require(len(rows)==1 and rows[0]["full_compare"] and rows[0]["result"]=="passed","RAM import not verified")
            imported = {"file":name,"receipt":rows[0]}
            memory = "/dev/dax0.0"
        self.start_vm(s,transfer+"/hints.json")
        self.api(role,s.directory,"/snapshot/load","PUT",{"snapshot_path":transfer+"/state",
                 "mem_backend":{"backend_type":"File","backend_path":memory},"resume_vm":False})
        if tier=="dram":
            raw,_=self.shell(role,f"read p t < {s.directory}/fc.pid\nawk '$NF == \"/dev/dax0.0\" {{print}}' /proc/$p/maps","dram-no-dax")
            require(not any(re.match(r"^[0-9a-f]+-[0-9a-f]+ .* /dev/dax0[.]0$",line) for line in raw.splitlines()),"DRAM restore retains a CXL VMA")
        pooled = self.admit(s,control,"restore")
        if tier=="cxl" and not self.admission_negative_tests:
            self.admission_negative_tests.append(control.admission_negative_tests(s,pooled))
            sentinel=s.directory+"/forbidden-state"
            marker=b"pool state-only rejection must preserve existing state bytes\n"
            self.write_bytes(s.role,sentinel,marker,"state-sentinel")
            negative=self.rejected_api(s,"/snapshot/create",{"snapshot_type":"Full",
                "snapshot_path":sentinel,"mem_file_path":"/dev/dax0.0"},"state-only-snapshot-with-overlays")
            require(self.fetch_bytes(s.role,sentinel,"state-sentinel-after")==marker,"Rejected snapshot modified sentinel state")
            negative["canonical_unchanged"]=self.verify_canonical()
        poison = None
        if old_tier == "cxl" and tier == "dram":
            raw,name = self.shell(role,f"POOL_ALLOW_POISON=1 /lab/pool-dax-copy poison DEMOTED /dev/dax0.0 {s.offset} {self.memory_mib*MIB}","demotion-poison",180)
            rows = json_lines(raw,"POOL_DAX_COPY_JSON ")
            require(len(rows)==1 and rows[0]["result"]=="passed","Poison failed")
            poison = {"file":name,"receipt":rows[0]}
        self.resume(s)
        after = self.command_vm(s,"identity",require_cache=True)
        for key in ("boot_id","pid","cookie","generation","private_hash","upper_hash","payload_bytes"):
            require(before["receipt"][key]==after["receipt"][key],f"Migration lost {key}")
        checked = self.command_vm(s,"check",require_cache=True)
        require(checked["receipt"]["block_read_ios"] == 0,"Warm cache after migration performed block reads")
        audit=control.audit(s,pooled) if tier=="cxl" else None
        if audit is not None:
            require(audit["shared_bytes"]>=self.minimum_cache_bytes,"CXL physical sharing below required size")
        row={"vm":s.vm,"source":old_role,"source_tier":old_tier,"destination":role,"destination_tier":tier,
             "epoch":s.epoch,"before":before,"after":after,"check":checked,"fence":fence,"import":imported,
             "poison":poison,"pool":pooled,"pfn_audit":audit,"logical_snapshot_bytes":self.memory_mib*MIB,"result":"passed"}
        row["snapshot_transport_file"]=transport_file
        self.transitions.append(row)
        self.fetch_bytes(old_role,old_dir+"/fc.log",f"v{s.vm}-e{s.epoch-1}-log")
        # Unlinking a restored regular RAM file is safe: its private VMA holds
        # the inode. No future transition opens an earlier file by name.
        self.shell(role,f"rm -f {transfer}/ram", "retired-snapshot-unlink")
        return row


def main():
    from pool_control import PoolControl
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sandboxes",type=int,default=3)
    p.add_argument("--cycles",type=int,default=2)
    p.add_argument("--memory-mib",type=int,default=512)
    p.add_argument("--vcpus",type=int,default=1)
    p.add_argument("--io-engine",choices=("Sync","Async"),default="Sync")
    p.add_argument("--minimum-cache-mib",type=int,default=128)
    args=p.parse_args()
    require(3<=args.sandboxes<=3 and 1<=args.cycles<=10,"Require exactly three sandboxes and 1..10 cycles")
    require(args.minimum_cache_mib>100 and args.memory_mib>=384 and args.sandboxes*args.memory_mib+2<=2048,"RAM/cache capacity")
    run,config=lab.current_run()
    require(config.get("pool_cow") and config.get("dax_4k") and config.get("cxl_mib",0)>=4096 and config.get("home_storage_image",{}).get("owner")=="A","Wrong lab configuration")
    out=run/("pool-"+str(time.time_ns())); out.mkdir()
    c=PoolController(config,out,args); control=PoolControl(c)
    sources=["pool_bench.py","pool_control.py","pool_prepare.py","pool_run.py","pool_guest_probe.c","pool-l2-init.sh","pool_dax_copy.c","pool_bloom_tool.c","pool_pfn_audit.c","pool_cow_probe.c","devdax-pool-cow.patch","lab.py","firecracker.patch"]
    (out/"measured-sources").mkdir()
    for name in sources: shutil.copy2(HERE/name,out/"measured-sources"/name)
    report={"schema":"cxl-pool-lifecycle-v1","result":"running","arguments":vars(args),"config":config,
            "fixture_manifest":json.loads((Path(config["assets"])/"manifest.json").read_text()),
            "runtime_source_hashes":{name:lab.sha256(HERE/name) for name in sources},
            "receipts":c.receipts,"commands":c.commands,"transitions":c.transitions,"pool_events":c.pool_events,
            "admission_negative_tests":c.admission_negative_tests,
            "runtime_safety_negatives":c.runtime_safety_negatives,
            "image_id":c.image_id,"payload_bytes":c.payload_bytes,"pool_offset":c.pool_offset,"bloom_offset":c.bloom_offset,
            "limitations":["One coherent physical host with two nested-KVM L1 nodes; not noncoherent physical CXL",
                "Every transition materializes full logical RAM; no zero-copy handoff or latency claim",
                "devDAX private CoW pages may be host DRAM; CXL stage is a hybrid after writes",
                "Frozen immutable canonical image; no frame reuse or mutable base-image sharing",
                "Experimental FC runtime pooling uses --no-seccomp inside disposable L1 VMs"]}
    def save(): lab.save_json(out/"report.json",report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        setup(c)
        for role in ("A","B"):
            require(c.last_image_hashes[role]["sha256"]==c.image_id,"Mounted image differs from the independent fixture digest")
        report["initial_image_hashes"]=dict(c.last_image_hashes)
        for role in ("A","B"):
            c.shell(role,"echo 1048576 > /proc/sys/vm/max_map_count\ncat /proc/cmdline\ncat /sys/module/device_dax/parameters/pool_cow", "pool-kernel")
        c.shell("A",f"mkdir -p {c.storage}","pool-storage")
        report["pool_setup"]=control.setup()
        for s in c.sandboxes:
            c.boot_vm(s); c.pause(s); c.admit(s,control,"initial-dram"); c.resume(s)
        report["all_warm"]=[c.command_vm(s,"check",require_cache=True) for s in c.sandboxes]
        require(len({r["receipt"]["boot_id"] for r in report["all_warm"]})==args.sandboxes,"Sandboxes not independent boots")
        require(len({r["receipt"]["private_hash"] for r in report["all_warm"]})==args.sandboxes,"Private VM data not distinct")
        c.rejected_api(c.sandboxes[0],"/actions",{"action_type":"PoolCache"},"pool-action-while-running")
        c.command_vm(c.sandboxes[0],"check",require_cache=True)
        for s in c.sandboxes: c.pause(s)
        report["bloom_negative_tests"]=control.approximate_negative_tests(c.sandboxes[0],c.sandboxes[1])
        for s in c.sandboxes: c.resume(s)
        for cycle in range(args.cycles):
            for role,tier in (("A","cxl"),(None,"cxl"),("A","cxl"),("A","dram")):
                for s in c.sandboxes:
                    destination=role or ("B" if s.vm%2 else "A")
                    if destination==s.role and tier==s.tier: continue
                    s.generation+=1
                    c.command_vm(s,"write",s.generation)
                    c.transition(s,destination,tier,control)
                    # Check all donors/consumers after each one migrates. This
                    # catches one VM's demotion/poison corrupting another VM.
                    for peer in c.sandboxes:
                        c.command_vm(peer,"check",require_cache=True)
                    save()
                if role is None:
                    remote=next(s for s in c.sandboxes if s.role=="B")
                    report.setdefault("remote_reclaims",[]).append(c.reclaim_refill(remote,control,nfs_check=True))
                    for peer in c.sandboxes: c.command_vm(peer,"check",require_cache=True)
                    save()
                if tier=="cxl":
                    audits=control.audit_group(c.sandboxes,c.latest_admissions)
                    shared=set.intersection(*(set(a["shared_offsets"]) for a in audits))
                    require(len(shared)*4096>=c.minimum_cache_bytes,"Three live sandboxes do not share the same >=128MiB canonical data")
                    simultaneous={"cycle":cycle,"stage":role or "split-hosts",
                        "vms":[s.vm for s in c.sandboxes],"roles":[s.role for s in c.sandboxes],
                        "shared_pages_all_vms":len(shared),"shared_bytes_all_vms":len(shared)*4096,"audits":audits}
                    if c.cache_gpa_audit:
                        file_shared=set.intersection(*(set(a["shared_file_offsets"]) for a in audits))
                        require(len(file_shared)*4096>=c.minimum_cache_bytes,"Three current guest file caches do not share >=128MiB of the same file pages")
                        simultaneous.update(shared_current_file_pages_all_vms=len(file_shared),
                            shared_current_file_bytes_all_vms=len(file_shared)*4096)
                    report.setdefault("concurrent_sharing",[]).append(simultaneous)
                    save()
            # Exercise evicted/reused guest buffers and a fresh block read on
            # each VM, then register new hints before the next promotion.
            for s in c.sandboxes:
                c.reclaim_refill(s,control)
                save()
            report.setdefault("canonical_checks",[]).append(c.verify_canonical())
        report["final"]=[c.command_vm(s,"check",require_cache=True) for s in c.sandboxes]
        report["result"]="passed"
    except BaseException as exc:
        report["result"]="failed"; report["error"]=repr(exc)
        raise
    finally:
        for s in c.sandboxes:
            if s.directory:
                try:
                    c.fetch_bytes(s.role,s.directory+"/fc.log",f"v{s.vm}-final-log")
                    nfs.stop_fc(c.vms[s.role],s.directory)
                except Exception as exc: report.setdefault("cleanup_errors",[]).append(repr(exc))
        save()
        print("POOL_REPORT "+str(out/"report.json"),flush=True)


if __name__=="__main__": main()
