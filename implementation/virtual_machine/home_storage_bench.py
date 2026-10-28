#!/usr/bin/env python3
"""Disk-backed A/DRAM -> A/CXL -> B/CXL -> A/CXL -> A/DRAM correctness lab.

The active disposable pair must have --home-storage-image (only A has a disk).
This is an externally fenced, coherent, stop-and-copy experiment, not a
distributed lease implementation or a performance benchmark.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import time

import lab
import nfs_bench as nfs
from trace_bench import source_fence_script, observe

HERE = Path(__file__).resolve().parent
MIB = 1024 * 1024


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def json_lines(raw, prefix):
    return [json.loads(line[len(prefix):]) for line in raw.splitlines() if line.startswith(prefix)]


class Controller:
    def __init__(self, config, out, memory_mib, vcpus):
        self.config, self.vms, self.out = config, config["vms"], out
        self.memory_mib, self.vcpus = memory_mib, vcpus
        self.root = "/run/home-" + os.urandom(8).hex()
        self.mailbox = "/run/trace-" + os.urandom(8).hex()
        self.role, self.tier, self.directory = None, None, None
        self.number = 0
        self.receipts = []
        self.transitions = []
        self.active = []
        self.nonce = None
        self.round = 0

    def raw(self, kind, text):
        name = f"{len(self.receipts):04d}-{kind}.txt"
        (self.out / name).write_text(text)
        self.receipts.append({"file": name, "sha256": hashlib.sha256(text.encode()).hexdigest(), "kind": kind})
        return name

    def shell(self, role, body, kind, timeout=90):
        raw = nfs.shell(self.vms[role], body, timeout=timeout)
        return raw, self.raw(kind, raw)

    def api(self, role, directory, path, method, body):
        raw = nfs.api(self.vms[role], directory, path, method, body)
        name = self.raw("api", raw)
        values = json_lines(raw, "LAB_API_JSON ")
        require(len(values) == 1 and values[0]["status"] == 204, f"API failed: {raw}")
        return {"file": name, "role": role, "path": path, "method": method, "request": body, "response": values[0]}

    def start(self, role, tier):
        directory = self.root + f"/fc-{self.number:03d}"
        self.number += 1
        env = "FC_DAX_RAM=/dev/dax0.0 FC_DAX_OFFSET=2097152 FC_DAX_SNAPSHOT=1" if tier == "cxl" else "FC_DAX_SNAPSHOT=0"
        reset = " ".join("-u " + name for name in ("FC_DAX_RAM", "FC_DAX_OFFSET", "FC_DAX_DIRECT", "FC_DAX_REUSE",
                         "FC_DAX_HEAP", "FC_DAX_HEAP_OFFSET", "FC_DAX_HEAP_BYTES", "FC_DAX_FIXED_HVA"))
        self.active.append((role, directory))
        self.shell(role, f"mkdir -p {directory}\nmkfifo {directory}/input\n"
                   f"(exec 3<>{directory}/input\nexec env {reset} {env} "
                   f"/lab/firecracker --api-sock {directory}/api.sock <&3 > {directory}/fc.log 2>&1) &\n" +
                   nfs.record_pid(directory) + f"n=0\nwhile [ ! -S {directory}/api.sock ]; do\n"
                   f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 500 ]\nsleep .02\ndone",
                   "start")
        return directory

    def wait_event(self, role, directory, event, label=None, timeout=120):
        pattern = f'"event":"{event}"'
        if label is not None:
            pattern += f',"label":"{label}"'
        complete = re.escape(pattern) + r".*}[[:space:]]*$"
        raw, name = self.shell(role, f"n=0\nwhile ! grep -Eq {shlex.quote(complete)} {directory}/fc.log; do\n"
                  f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\n"
                  f"! grep -Eq 'Kernel panic|HOME_STORAGE_FATAL' {directory}/fc.log\n"
                  f"n=$((n+1))\n[ $n -lt {timeout * 50} ]\nsleep .02\ndone\n"
                  f"grep -E {shlex.quote(complete)} {directory}/fc.log", "guest", timeout+15)
        rows = [json.loads(line) for line in raw.splitlines() if line.startswith('{"event":')]
        rows = [r for r in rows if r.get("event") == event and (label is None or r.get("label") == label)]
        require(len(rows) == 1 and rows[0].get("ok", True), f"Guest event failed: {raw}")
        return {"file": name, "receipt": rows[0]}

    def command(self, operation, *arguments):
        label = f"cmd{len(self.receipts):05d}"
        command = " ".join([operation, label, *map(str, arguments)])
        self.shell(self.role, f"printf '%s\\n' {shlex.quote(command)} > {self.directory}/input", "guest-input")
        event = self.wait_event(self.role, self.directory, "home_storage", label)
        event.update(role=self.role, tier=self.tier, operation=operation, arguments=list(arguments))
        return event

    def maps(self, role, directory, tier, memory_path=None):
        raw, name = self.shell(role, f"read p s < {directory}/fc.pid\ncat /proc/$p/maps", "maps")
        mappings = []
        for line in raw.splitlines():
            match = re.match(r"^([0-9a-f]+)-([0-9a-f]+) ([-rwxps]{4}) ([0-9a-f]+) \S+ \d+\s*(.*)$", line)
            if match:
                a, b, perms, offset, path = match.groups()
                mappings.append({"length": int(b,16)-int(a,16), "permissions": perms,
                                 "offset": int(offset,16), "path": path})
        dax = [m for m in mappings if m["path"] == "/dev/dax0.0"]
        if tier == "cxl":
            require(len(dax) == 1 and dax[0]["length"] == self.memory_mib*MIB and
                    dax[0]["offset"] == 2*MIB and dax[0]["permissions"] == "rw-s", "Incorrect CXL RAM mapping")
        else:
            require(not dax, "DRAM process still maps CXL RAM")
            candidates = [m for m in mappings if m["length"] == self.memory_mib*MIB and m["permissions"] == "rw-p"
                          and m["path"] == (memory_path or "")]
            require(len(candidates) == 1, f"Local RAM VMA missing: {candidates}")
        return {"file": name, "tier": tier, "memory_path": memory_path, "mappings": mappings}

    def helper(self, role, operation, file):
        command = "/lab/ram-transfer " + shlex.join([operation, file, "/dev/dax0.0", str(self.memory_mib*MIB)])
        if operation == "poison":
            command = "RAM_TRANSFER_ALLOW_POISON=1 " + command
        raw, name = self.shell(role, command, "ram-"+operation, timeout=180)
        values = json_lines(raw, "RAM_TRANSFER_JSON ")
        require(len(values) == 1 and values[0]["result"] == "passed", f"RAM helper failed: {raw}")
        return {"file": name, "role": role, "receipt": values[0]}

    def image_hash(self, role):
        raw, name = self.shell(role, "sync\necho 3 > /proc/sys/vm/drop_caches\nsha256sum /disk/sandbox.raw", "image-hash", 120)
        hashes = re.findall(r"^([0-9a-f]{64})  /disk/sandbox.raw\s*$", raw, re.M)
        require(len(hashes) == 1, "Image hash missing")
        return {"file": name, "sha256": hashes[0], "role": role}

    def nfs_writes(self):
        raw, name = self.shell("B", "cat /proc/net/rpc/nfs", "nfs-writes")
        rows = [line.split() for line in raw.splitlines() if line.startswith("proc3 ")]
        require(len(rows) == 1 and len(rows[0]) == 24, "NFSv3 counters missing")
        return {"file": name, "write": int(rows[0][9]), "commit": int(rows[0][23])}

    def boot(self, audit=None):
        role, tier = "A", "dram"
        directory = self.start(role, tier)
        args = "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init"
        if audit is not None:
            args += f" home.audit={audit}"
        components = {"/machine-config": {"vcpu_count": self.vcpus, "mem_size_mib": self.memory_mib},
            "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux", "initrd_path": "/lab/home-storage-l2-initramfs.cpio", "boot_args": args},
            "/drives/data": {"drive_id": "data", "path_on_host": "/disk/sandbox.raw", "is_root_device": False,
                             "is_read_only": audit is not None, "cache_type": "Writeback", "io_engine": "Sync"}}
        apis = [self.api(role, directory, path, "PUT", body) for path, body in components.items()]
        apis.append(self.api(role, directory, "/actions", "PUT", {"action_type": "InstanceStart"}))
        self.role, self.tier, self.directory = role, tier, directory
        result = self.wait_event(role, directory, "home_audit" if audit is not None else "home_ready")
        result["api"] = apis
        raw, name = self.shell(role, f"grep '^HOME_STORAGE_MOUNT ' {directory}/fc.log", "guest-mount")
        require("/dev/vda /data ext4 " in raw, "Guest is not using the disk ext4 filesystem")
        result["mount_file"] = name
        result["ram_mapping"] = self.maps(role, directory, tier)
        return result

    def checkpoint(self, target, target_tier, require_dirty=False):
        source, old_tier, old_dir = self.role, self.tier, self.directory
        row = {"source": source, "destination": target, "source_tier": old_tier, "destination_tier": target_tier,
               "epoch": len(self.transitions)+1, "api": [], "mailbox": [], "ram": [], "result": "running"}
        self.transitions.append(row)
        row["before"] = self.command("identity")
        row["require_dirty"] = require_dirty
        if require_dirty:
            require(row["before"]["receipt"]["dirty_kib"] > 0, "Guest dirty pages absent immediately before pause")
        row["api"].append(self.api(source, old_dir, "/vm", "PATCH", {"state": "Paused"}))
        # Never truncate the file backing an earlier private DRAM restore.
        ramfile = old_dir + "/ram-snapshot" if old_tier == "dram" else "/dev/dax0.0"
        row["api"].append(self.api(source, old_dir, "/snapshot/create", "PUT", {
            "snapshot_type": "Full", "snapshot_path": old_dir+"/state", "mem_file_path": ramfile}))
        nfs.stop_fc(self.vms[source], old_dir)
        raw, row["source_fence_file"] = self.shell(source, source_fence_script(old_dir), "fence")
        require("FENCED " in raw, "Source fence missing")
        row["source_image"] = self.image_hash(source)
        new_dir = self.root + f"/fc-{self.number:03d}"
        self.shell(target, f"mkdir -p {new_dir}", "destination-directory")
        if old_tier == "dram" and target_tier == "cxl":
            row["ram"].append(self.helper(source, "import", ramfile))
            memory_path = "/dev/dax0.0"
        elif old_tier == "cxl" and target_tier == "dram":
            memory_path = new_dir+"/ram-restore"
            row["ram"].append(self.helper(target, "export", memory_path))
        else:
            require(old_tier == target_tier == "cxl", "Only CXL can move without RAM copying")
            memory_path = "/dev/dax0.0"
        if source != target:
            if source == "A":
                self.nonce, self.round = os.urandom(8).hex(), 1
            else:
                require(self.nonce and self.round == 1, "B return without A departure")
                self.round = 2
            for role in (source, target):
                self.shell(role, f"mkdir -p {self.mailbox}", "mailbox-directory")
            self.shell(source, f"cp {old_dir}/state {self.mailbox}/state", "mailbox-source")
            for role, phase in ((source,"publish"),(target,"receive")):
                raw, name = self.shell(role, "/lab/shared-probe state-mailbox " + shlex.join([
                    role, phase, "/dev/dax0.0", self.mailbox+"/state", self.nonce, str(self.round), str(self.memory_mib)]), "mailbox")
                values = json_lines(raw, "STATE_MAILBOX_JSON ")
                require(len(values) == 1, "Mailbox receipt missing")
                row["mailbox"].append({"file": name, "receipt": values[0]})
            self.shell(target, f"cp {self.mailbox}/state {new_dir}/state", "mailbox-destination")
        else:
            self.shell(target, f"cp {old_dir}/state {new_dir}/state", "local-state")
        row["destination_image"] = self.image_hash(target)
        require(row["source_image"]["sha256"] == row["destination_image"]["sha256"], "Destination sees stale/wrong disk bytes")
        require(self.start(target, target_tier) == new_dir, "Unexpected destination directory")
        row["api"].append(self.api(target, new_dir, "/snapshot/load", "PUT", {
            "snapshot_path": new_dir+"/state", "mem_backend": {"backend_type":"File","backend_path":memory_path}, "resume_vm":False}))
        row["ram_mapping"] = self.maps(target, new_dir, target_tier, memory_path if target_tier == "dram" else None)
        if old_tier == "cxl" and target_tier == "dram":
            # All previous owners are dead and restored RAM is a local file VMA.
            # Poison before resume proves the new VM does not depend on old CXL.
            row["ram"].append(self.helper(target, "poison", "POST_DEMOTION_TEST"))
        row["api"].append(self.api(target, new_dir, "/vm", "PATCH", {"state":"Resumed"}))
        self.role, self.tier, self.directory = target, target_tier, new_dir
        row["after"] = self.command("identity")
        for key in ("boot_id", "pid", "open_cookie", "generation", "mutable_inode", "cursor_inode",
                    "cursor_offset", "unlinked_inode", "unlinked_offset", "unlinked_nlink", "mapped_hash"):
            require(row["before"]["receipt"][key] == row["after"]["receipt"][key], f"Identity lost: {key}")
        a, integrity_a = lab.guest_fetch_file(self.vms[source], old_dir+"/state")
        b, integrity_b = lab.guest_fetch_file(self.vms[target], new_dir+"/state")
        require(a == b, "Destination state bytes differ")
        row["state_files"] = []
        for side, data, integrity in (("source",a,integrity_a),("destination",b,integrity_b)):
            name = f"epoch{row['epoch']:03d}-{side}-state.bin"
            (self.out/name).write_bytes(data)
            row["state_files"].append({"side":side,"file":name,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),"integrity":integrity})
        raw, integrity = lab.guest_fetch_file(self.vms[source], old_dir+"/fc.log")
        name = f"epoch{row['epoch']:03d}-source-fc.log"
        (self.out/name).write_bytes(raw)
        row["source_log"] = {"file":name,"integrity":integrity}
        row["ram_transfer_bytes"] = self.memory_mib*MIB if old_tier != target_tier else 0
        row["result"] = "passed"
        # A fully instantiated private restore can still fault in the file;
        # unlink only the retired source's promotion file, never current RAM.
        if old_tier == "dram":
            self.shell(source, f"rm -f {old_dir}/ram-snapshot {old_dir}/ram-restore", "retired-ram-cleanup")
        return row


def setup(controller):
    c = controller
    device = c.config["home_storage_image"]["guest_device"]
    for role in ("A","B"):
        c.shell(role, "test ! -e /run/home-storage-claimed", "storage-claim-check")
    for role in ("A","B"):
        c.shell(role, f"mkdir /run/home-storage-claimed\nprintf '%s\\n' {c.root} > /run/home-storage-claimed/owner", "storage-claim")
    for role in ("A","B"):
        c.shell(role, lab.snapshot_cleanup_script(include_boot_vm=True)+f"\nmkdir -p {c.root} /storage /disk /remote", "prepare")
    c.shell("A", f"mount -t ext4 -o rw,noatime {device} /storage\nmount -o bind /storage /disk\n"
        "export PATH=/usr/sbin:/usr/bin:/bin:/sbin:/lab\nmkdir -p /var/lib/nfs /run/rpcbind /proc/fs/nfsd\n"
        "mount -t nfsd nfsd /proc/fs/nfsd\n"
        "printf '/storage 172.30.0.2(rw,sync,no_subtree_check,no_root_squash,fsid=19)\\n' > /etc/exports\n"
        f"rpcbind -f -w > {c.root}/rpcbind.log 2>&1 &\nsleep .2\nexportfs -ra\n"
        f"rpc.mountd --foreground --no-nfs-version 2 --no-nfs-version 4 --port 20048 > {c.root}/mountd.log 2>&1 &\n"
        "rpc.nfsd -V3 -N4 4\nexportfs -v\ncat /proc/mounts", "home-storage")
    c.shell("B", "/usr/sbin/mount.nfs 172.30.0.1:/storage /remote "
        "-o rw,vers=3,proto=tcp,mountproto=tcp,port=2049,mountport=20048,nolock,hard,timeo=50,retrans=2,actimeo=0\n"
        "mount -o bind /remote /disk\ncat /proc/mounts\nls /sys/class/block", "remote-storage")
    require(c.image_hash("A")["sha256"] == c.image_hash("B")["sha256"], "Initial disk identity mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles",type=int,default=3)
    parser.add_argument("--memory-mib",type=int,default=512)
    parser.add_argument("--vcpus",type=int,default=2)
    args = parser.parse_args()
    require(1 <= args.cycles <= 10 and 128 <= args.memory_mib <= 2048 and 1 <= args.vcpus, "Invalid cycles/RAM/vCPUs")
    run, config = lab.current_run()
    require(config.get("home_storage_image",{}).get("owner") == "A" and config.get("dax_guest") and
            not config.get("dax_handoff") and not config.get("dax_kvm_state"), "Requires ordinary DAX lab with A-only writable disk")
    require(args.memory_mib+2 <= config["cxl_mib"] and args.vcpus <= config["l1_vcpus"], "Lab capacity insufficient")
    out = run/("home-storage-"+str(time.time_ns()))
    out.mkdir()
    c = Controller(config,out,args.memory_mib,args.vcpus)
    sources = ("home_storage_bench.py","home_storage_probe.c","home-storage-l2-init.sh","home_storage_prepare.py",
               "ram_transfer.c","shared_probe.c","lab.py","nfs_bench.py","trace_bench.py","firecracker.patch")
    (out/"measured-sources").mkdir()
    for name in sources:
        shutil.copy2(HERE/name,out/"measured-sources"/name)
    report = {"schema_version":1,"result":"running","arguments":vars(args),"config":config,"receipts":c.receipts,
        "transitions":c.transitions,"commands":[],"runtime_sources":{name:lab.sha256(HERE/name) for name in sources},
        "limitations":["One physical host, nested KVM, coherent emulated CXL", "Stop-and-copy RAM tier transitions; serial control, no latency claim",
                       "Controller fences owned processes, not a fault-tolerant distributed lease", "Fresh guest reboot validates disk persistence, not power-loss durability",
                       "Host cache drops only inside owned L1 VMs; not a production cache protocol"]}
    def save(): lab.save_json(out/"report.json",report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report["affinity"] = nfs.pin_vms(config,argparse.Namespace(host_cpus_a="0-2",host_cpus_b="3-5"))
        setup(c)
        report["boot"] = c.boot()
        generation = 0
        for cycle in range(args.cycles):
            for role,tier in (("A","cxl"),("B","cxl"),("A","cxl"),("A","dram")):
                generation += 1
                mode = "sync" if c.role == "B" and cycle%2 == 0 else ("dirty" if generation%2 else "sync")
                nfs_before = c.nfs_writes()
                written = c.command("write",generation,mode)
                nfs_after = c.nfs_writes()
                written.update(nfs_before=nfs_before, nfs_after=nfs_after,
                               nfs_write_delta=nfs_after["write"]-nfs_before["write"],
                               nfs_commit_delta=nfs_after["commit"]-nfs_before["commit"])
                if c.role == "B" and mode == "sync":
                    require(written["nfs_write_delta"] > 0, "Synced remote write did not reach NFS")
                if c.role == "A":
                    require(written["nfs_write_delta"] == 0, "Home write unexpectedly used NFS")
                require(written["receipt"]["generation"] == generation and
                        written["receipt"]["synced"] == (mode == "sync"), "Write mode/generation mismatch")
                if mode == "dirty":
                    require(written["receipt"]["dirty_kib"] > 0, "Unsynced workload did not leave dirty guest pages")
                report["commands"].append(written)
                readback = c.command("read",generation)
                require(all(readback["receipt"][key] == written["receipt"][key] for key in
                            ("generation", "mutable_hash", "mapped_hash")), "Written content differs before handoff")
                report["commands"].append(readback)
                c.checkpoint(role,tier,require_dirty=(mode == "dirty"))
                resumed = c.command("read",generation)
                require(all(resumed["receipt"][key] == written["receipt"][key] for key in
                            ("generation", "mutable_hash", "mapped_hash")), "Written content lost across handoff")
                require(all(resumed["receipt"][key] == readback["receipt"][key]+16 for key in
                            ("cursor_offset", "unlinked_offset")), "Retained file cursor did not advance")
                report["commands"].append(resumed)
                report["commands"].append(c.command("sync"))
                report["commands"].append(c.command("evict"))
                c.shell("B", "sync\necho 3 > /proc/sys/vm/drop_caches", "cold-host-cache")
                before_path = f"nfs/epoch{len(c.transitions):03d}-before"
                after_path = f"nfs/epoch{len(c.transitions):03d}-after"
                before = observe(c.vms["B"], out, before_path)
                cold = c.command("coldread",0,8*MIB)
                after = observe(c.vms["B"], out, after_path)
                cold.update(nfs_before_file=before_path, nfs_after_file=after_path,
                            nfs_delta=nfs.delta(before,after))
                require(cold["receipt"]["payload_resident_before"] == 0 and
                        cold["receipt"]["block_delta"]["read_sectors"] > 0, "Cold read did not reach virtio-blk")
                require((cold["nfs_delta"]["nfs_read_calls"] > 0) == (role == "B"), "Cold read used wrong storage path")
                report["commands"].append(cold)
                save()
        report["commands"].append(c.command("sync"))
        report["commands"].append(c.command("quit"))
        c.shell("A", f"n=0\nwhile ! grep -q HOME_STORAGE_UNMOUNTED {c.directory}/fc.log; do n=$((n+1)); [ $n -lt 500 ]; sleep .02; done", "unmounted")
        nfs.stop_fc(c.vms["A"],c.directory)
        c.shell("A",source_fence_script(c.directory),"final-fence")
        report["final_generation"] = generation
        report["final_image"] = c.image_hash("A")
        report["fresh_boot_audit"] = c.boot(audit=generation)
        require(report["fresh_boot_audit"]["receipt"]["boot_id"] != report["boot"]["receipt"]["boot_id"], "Persistence audit reused original guest")
        require(report["fresh_boot_audit"]["receipt"]["generation"] == generation and
                all(report["fresh_boot_audit"]["receipt"][key] == written["receipt"][key] for key in
                    ("mutable_hash", "mapped_hash")), "Fresh boot lost persistent file changes")
        c.shell("A",f"n=0\nwhile ! grep -q HOME_STORAGE_AUDIT_DONE {c.directory}/fc.log; do n=$((n+1)); [ $n -lt 500 ]; sleep .02; done", "audit-unmounted")
        nfs.stop_fc(c.vms["A"],c.directory)
        c.shell("A","sync", "final-host-sync")
        report["result"] = "passed"
    except BaseException as exc:
        report.update(result="failed",error=repr(exc))
        raise
    finally:
        report["cleanup_errors"] = []
        report["firecracker_logs"] = []
        for role,directory in c.active:
            try:
                nfs.stop_fc(c.vms[role],directory)
                raw, integrity = lab.guest_fetch_file(c.vms[role],directory+"/fc.log")
                name = f"all-{role}-{directory.rsplit('/',1)[1]}.log"
                (out/name).write_bytes(raw)
                report["firecracker_logs"].append({"role":role,"directory":directory,"file":name,
                                                  "sha256":hashlib.sha256(raw).hexdigest(),"integrity":integrity})
            except Exception as exc: report["cleanup_errors"].append(str(exc))
        if report["result"] == "passed" and not report["cleanup_errors"]:
            try:
                c.shell("B", "umount /disk\numount /remote\ncat /proc/mounts", "remote-unmounted")
                c.shell("A", "export PATH=/usr/sbin:/usr/bin:/bin:/sbin:/lab\nexportfs -ua\nrpc.nfsd 0\n"
                        "umount /disk\nsync\numount /storage\ncat /proc/mounts", "home-unmounted")
                report["storage_cleanly_unmounted"] = True
            except Exception as exc:
                report["cleanup_errors"].append(str(exc))
        if report["cleanup_errors"]: report["result"] = "failed"
        save()
        print(f"HOME_STORAGE_REPORT={out/'report.json'}",flush=True)
    require(report["result"] == "passed" and report.get("storage_cleanly_unmounted") is True,
            f"Home storage cleanup failed: {report['cleanup_errors']}")


if __name__ == "__main__":
    main()
