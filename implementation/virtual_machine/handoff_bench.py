#!/usr/bin/env python3
"""Shared EPT lab helpers: DRAM boot, CXL promotion and exact SWE commands."""
import argparse
import base64
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import statistics
import time

import lab
import nfs_bench as nfs
from experiment_sources import record_sources

HERE = Path(__file__).resolve().parent
MIB = 1024 * 1024
PAGE = 4096

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
        import time
        started = time.monotonic_ns()
        raw = nfs.shell(self.vms[role], body, timeout=timeout)
        name = self.raw(kind, raw)
        if not hasattr(self, 'control_timings'):
            self.control_timings = []
        self.control_timings.append({'role': role, 'kind': kind, 'file': name,
                                     'elapsed_ns': time.monotonic_ns() - started})
        if self.vms[role].get('control_rpc'):
            self.control_timings[-1]['rpc'] = dict(self.vms[role]['_last_control_timing'])
        return raw, name
    def api(self, role, directory, path, method, body):
        if self.vms[role].get("control_rpc"):
            import control_rpc
            raw = control_rpc.http(self.vms[role], directory+'/api.sock', method, path, body)
        else:
            raw = nfs.api(self.vms[role], directory, path, method, body)
        name = self.raw("api", raw)
        values = json_lines(raw, "LAB_API_JSON ")
        require(len(values) == 1 and values[0]["status"] == 204, f"API failed: {raw}")
        return {"file": name, "role": role, "path": path, "method": method, "request": body, "response": values[0]}

def source_fence_script(directory):
    # A zombie may be reaped between existence testing and reading /proc.
    # Read first; a failed read is safe only if that process entry is gone.
    return (f"read p expected < {directory}/fc.pid\n"
        "case \"$p:$expected\" in *[!0-9:]*|:|*:) exit 1 ;; esac\n"
        "[ \"$p\" -gt 1 ]\n[ \"$expected\" -gt 0 ]\n"
        "observed=0\nreason=missing\nif record=$(cat /proc/$p/stat 2>/dev/null); then\n"
        "record=${record##*) }\nset -- $record\nstate=$1\nshift 19\nobserved=$1\n"
        "case \"$observed\" in ''|*[!0-9]*) exit 1 ;; esac\n[ \"$observed\" -gt 0 ]\n"
        "if [ \"$observed\" != \"$expected\" ]; then reason=reused\n"
        "else case \"$state\" in Z|X) reason=$state ;; *) exit 1 ;; esac\nfi\n"
        "else [ ! -e /proc/$p/stat ] || exit 1\nfi\n"
        "printf 'FENCED %s %s\\nFENCE_STATE %s %s\\n' \"$p\" \"$expected\" \"$reason\" \"$observed\"")

@dataclass
class Grant:
    generation: int
    frames: set

def offset_ranges(offsets):
    result = []
    for offset in sorted(offsets):
        if result and result[-1]["offset"] + result[-1]["pages"] * PAGE == offset:
            result[-1]["pages"] += 1
        else:
            result.append({"offset": offset, "pages": 1})
    return result

class FrameAllocator:
    def __init__(self, start, end):
        require(0 <= start < end and (start | end) % PAGE == 0, "invalid allocator bounds")
        self.start, self.end = start, end
        self.free_ranges = [(start, end)]
        self.free_pages = (end-start)//PAGE
        self.grants = {}
        self.generation = 0
        self.events = []

    def allocate(self, owner, pages):
        require(type(pages) is int and 0 <= pages <= self.free_pages, "CXL private capacity exhausted")
        frames, remaining = [], pages
        while remaining:
            start, end = self.free_ranges[0]
            take = min(remaining, (end-start)//PAGE)
            stop = start + take*PAGE
            frames.extend(range(start, stop, PAGE))
            if stop == end:
                self.free_ranges.pop(0)
            else:
                self.free_ranges[0] = (stop, end)
            remaining -= take
        self.free_pages -= pages
        if owner not in self.grants:
            self.generation += 1
            self.grants[owner] = Grant(self.generation, set())
        grant = self.grants[owner]
        grant.frames.update(frames)
        self.events.append({"operation": "allocate", "owner": owner, "generation": grant.generation,
                            "pages": pages, "ranges": offset_ranges(frames)})
        return frames



    def summary(self):
        return {"capacity_bytes": self.end - self.start, "free_bytes": self.free_pages * PAGE,
                "allocated_bytes": sum(len(g.frames) * PAGE for g in self.grants.values()),
                "owners": {owner: {"generation": g.generation, "allocated_bytes": len(g.frames) * PAGE}
                           for owner, g in sorted(self.grants.items())}}

def promotion_layout(allocator, owner, memory_bytes):
    require(memory_bytes > 0 and memory_bytes % PAGE == 0, "invalid promotion RAM bytes")
    require(owner not in allocator.grants, "owner already has a promotion allocation")
    frames = allocator.allocate(owner, memory_bytes // PAGE)
    return {"mappings": [{"gpa": i * PAGE, "offset": offset} for i, offset in enumerate(frames)],
            "allocation_generation": allocator.grants[owner].generation}

def validate_export(allocator, owner, manifest, memory_bytes):
    require(owner in allocator.grants, "export without allocation owner")
    h = manifest["handle"]
    require(manifest["version"] == 4 and h["abi"] == 4 and h["count"] * PAGE == memory_bytes
            and h["allocation_generation"] == manifest["allocation_generation"] == allocator.grants[owner].generation
            and h["owner_epoch"] == int(manifest["run_epoch"]) and h["state"] in (1, 2),
            "invalid persistent ownership handle")
    require(not manifest.get("mappings") and not manifest.get("credits"), "export contains full mapping")
    require(h["count"] == len(allocator.grants[owner].frames), "mapper differs from grant")
    return {"logical_bytes": memory_bytes, "private_mapped_bytes": memory_bytes,
            "allocated_private_bytes": len(allocator.grants[owner].frames) * PAGE}

@dataclass
class Sandbox:
    vm: int
    role: str = "A"
    tier: str = "dram"
    directory: str = ""
    epoch: int = 0
    plan: str = ""
    report: str = ""
    manifest: dict | None = None
    owner: str | None = None
    manifest_path: str = ""

class HandoffController(Controller):
    def __init__(self, config, out, args):
        Controller.__init__(self, config, out, args.memory_mib, getattr(args, "vcpus", 1))
        self.args = args
        # (metadata/data bytes, KVM arena) in MiB by emulated CXL size.
        metadata, arena = {2048: (512, 1024), 4096: (3072, 3584), 8192: (4096, 6144)}.get(config.get('cxl_mib'), (8192, 12288))
        self.metadata_base = self.data_bytes = metadata * MIB
        self.kvm_arena = self.arena_base = arena * MIB
        self.fixture = json.loads((Path(config['assets']) / 'manifest.json').read_text())
        self.trace = self.fixture['traces'][args.name]
        self.root = '/run/swe-handoff-' + os.urandom(8).hex()
        self.sandboxes = [Sandbox(vm=1)]
        self.io_engine = 'Async'
        self.allocator = FrameAllocator(2 * MIB, (256 if config['cxl_mib']==2048 else 3072) * MIB)
        self.commands, self.trace_commands = [], []
        self.memory_events, self.api_events, self.launches = [], [], []
        self.serial_number = 0

    def fetch_bytes(self, role, path, kind="fetch"):
        data, integrity = lab.fetch_file(self.vms[role], path)
        name = f"{len(self.receipts):04d}-{kind}.bin"
        (self.out/name).write_bytes(data)
        self.receipts.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, "integrity": integrity})
        return data

    def fetch_json(self, role, path, kind="json"):
        return json.loads(self.fetch_bytes(role, path, kind))

    def write_bytes(self, role, path, data, kind="write", after=""):
        if self.vms[role].get('control_rpc'):
            import control_rpc
            started = time.monotonic_ns()
            require(lab.alive(self.vms[role]), 'VM is not running')
            raw = control_rpc.put(self.vms[role], path, data, after)
            name = self.raw(kind, raw)
            if not hasattr(self, 'control_timings'):
                self.control_timings = []
            self.control_timings.append({'role':role, 'kind':kind, 'file':name,
                'elapsed_ns':time.monotonic_ns()-started,
                'rpc':dict(self.vms[role]['_last_control_timing'])})
            return raw, name
    def write_json(self, role, path, value, kind="write-json"):
        return self.write_bytes(role, path, json.dumps(value, separators=(",", ":")).encode(), kind)

    def event(self, s, event, label=None):
        pattern = f'"event":"{event}"' + (f',"label":"{label}"' if label else "")
        complete = re.escape(pattern)+r".*}[[:space:]]*$"
        raw, name = self.shell(s.role, f"n=0\nwhile ! grep -Eq {shlex.quote(complete)} {s.directory}/fc.log; do\n"
                   f"read p t < {s.directory}/fc.pid\nkill -0 \"$p\"\n"
                   f"! grep -Eq 'Kernel panic' {s.directory}/fc.log\n"
                   "n=$((n+1))\n[ $n -lt 9000 ]\nsleep .02\ndone\n"
                   f"grep -E {shlex.quote(complete)} {s.directory}/fc.log", "guest", 200)
        rows = [json.loads(line) for line in raw.splitlines() if line.startswith('{"event":')]
        rows = [r for r in rows if r["event"] == event and (not label or r.get("label") == label)]
        require(len(rows) == 1 and rows[0].get("ok", True), "Missing, duplicate or failed guest event")
        return {"file": name, "receipt": rows[0], "vm":s.vm, "role":s.role, "tier":s.tier, "epoch":s.epoch}

    def pause(self,s): self.api(s.role,s.directory,"/vm","PATCH",{"state":"Paused"})

    def resume(self,s): self.api(s.role,s.directory,"/vm","PATCH",{"state":"Resumed"})

    def api(self, role, directory, path, method, body):
        start = time.monotonic_ns()
        value = super().api(role, directory, path, method, body)
        self.api_events.append(dict(value, control_wall_ns=time.monotonic_ns() - start))
        return value

    def start_vm(self, s):
        from ept_adopt import environment
        s.directory=f"{self.root}/vm{s.vm}-epoch{s.epoch}"
        s.plan=s.directory+"/memory-plan.json";s.report=s.directory+"/memory-report.json"
        s.manifest_path=s.directory+"/memory-manifest.json"
        env={"PATH":"/bin:/sbin:/usr/bin:/usr/sbin:/lab", "FC_POOL_MEMORY":"1",
            "FC_POOL_VM_ID":str(s.vm), "FC_POOL_RUN_EPOCH":str(s.epoch),
            "FC_POOL_MEMORY_PLAN":s.plan, "FC_POOL_MEMORY_REPORT":s.report,
            "FC_POOL_MEMORY_METADATA_OFFSET":str(self.metadata_base+s.vm*64*MIB),
            "FC_POOL_MEMORY_MANIFEST":s.manifest_path, "FC_POOL_MEMORY_DEVICE":"/dev/cxl_pool",
            "FC_DAX_FIXED_HVA":"0x400000000000", "FC_DIRTY_LEDGER":"1",
            "FC_NONCOHERENT":"1" if self.args.noncoherent else "0",
            "FC_NONCOHERENT_NODE":"1" if s.role=="A" else "2", **environment(self,s)}
        if self.args.workload:env.update(FC_BENCH_EPOCH_GPA=str(64*MIB),FC_BENCH_INITIAL_EPOCH=str(s.epoch))
        if self.args.dirty_oracle:env["FC_DIRTY_ORACLE"]="1"
        if self.args.restore_timing:env["FC_RESTORE_TIMING"]="1"
        self.shell(s.role,f"mkdir -p {s.directory}","launch-directory")
        script="set -e\n"+"\n".join("export "+shlex.quote(k+"="+v) for k,v in env.items())+"\n"
        script+=f"cat > {s.directory}/environment.json <<'EPT_ENV_JSON'\n"+json.dumps(env)+"\nEPT_ENV_JSON\n"
        script+=f"exec /lab/firecracker --no-seccomp --api-sock {s.directory}/api.sock --log-path {s.directory}/vmm.log\n"
        start=(f"mkfifo {s.directory}/input\n(exec 3<>{s.directory}/input\nexec env -i /bin/sh {s.directory}/launch.sh <&3 > {s.directory}/fc.log 2> {s.directory}/stderr.log) &\n"
            +nfs.record_pid(s.directory)+f"n=0\nwhile [ ! -S {s.directory}/api.sock ]; do\nread p t < {s.directory}/fc.pid\nkill -0 $p\nn=$((n+1))\n[ $n -lt 1000 ]\nsleep .02\ndone")
        _,file=self.write_bytes(s.role,s.directory+"/launch.sh",script.encode(),"start",after=start)
        self.active.append((s.role,s.directory))
        self.launches.append(dict(vm=s.vm,epoch=s.epoch,role=s.role,file=file,directory=s.directory,purpose="boot",runtime_rehashed=False))

    def boot_vm(self, s):
        self.start_vm(s)
        components = {'/machine-config': {'vcpu_count': self.vcpus, 'mem_size_mib': self.memory_mib, 'track_dirty_pages': True},
            '/boot-source': {'kernel_image_path': '/lab/l2-vmlinux',
                'initrd_path': f'/lab/{self.trace["name"]}-initramfs.cpio',
                'boot_args': 'console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init' + ((' memmap=2M$64M nmi_watchdog=0'+(' memmap=256M$128M' if getattr(self.args,'dirty_lines',0) else '')) if getattr(self.args,'workload',None) else '')},
            '/drives/data': {'drive_id': 'data', 'path_on_host': '/disk/sandbox.raw', 'is_root_device': False,
                'is_read_only': True, 'cache_type': 'Writeback', 'io_engine': self.io_engine}}
        if getattr(self.args,'dirty_mib',0)==1024:
            components['/boot-source']['boot_args']=components['/boot-source']['boot_args'].replace('memmap=256M$128M','memmap=1024M$128M')
        for path, body in components.items():
            self.api(s.role, s.directory, path, 'PUT', body)
        self.api(s.role, s.directory, '/actions', 'PUT', {'action_type': 'InstanceStart'})
        ready = self.event(s, 'trace_ready')
        require(ready['receipt']['command_count'] == self.trace['command_count'], 'Wrong trace boot')
        return ready

    def setup(self):
        device = self.config['home_storage_image']['guest_device']
        for role in ('A', 'B'):
            self.shell(role, 'test ! -e /run/home-storage-claimed\nmkdir /run/home-storage-claimed\n' +

                f'\nmkdir -p {self.root} /storage /disk /remote', 'prepare')
        name = self.trace['name']
        immutable_cache = True
        self.shell('A', f'mount -t ext4 -o rw,noatime {device} /storage\n'
            f'ln {shlex.quote("/storage/" + name + ".raw")} /storage/sandbox.raw\n'
            + ('mount -o remount,ro /storage\n' if immutable_cache else '') +
            'mount -o bind /storage /disk\nexport PATH=/usr/sbin:/usr/bin:/bin:/sbin:/lab\n'
            'mkdir -p /var/lib/nfs /run/rpcbind /proc/fs/nfsd\nmount -t nfsd nfsd /proc/fs/nfsd\n'
            "printf '/storage 172.30.0.2(ro,sync,no_subtree_check,no_root_squash,fsid=19)\\n' > /etc/exports\n"
            f'rpcbind -f -w > {self.root}/rpcbind.log 2>&1 &\nsleep .2\nexportfs -ra\n'
            f'rpc.mountd --foreground --no-nfs-version 2 --no-nfs-version 4 --port 20048 > {self.root}/mountd.log 2>&1 &\n'
            'rpc.nfsd -V3 -N4 4\nexportfs -v\ncat /proc/mounts', 'home-storage')
        self.shell('B', '/usr/sbin/mount.nfs 172.30.0.1:/storage /remote '
            '-o ro,vers=3,proto=tcp,mountproto=tcp,port=2049,mountport=20048,nolock,hard,timeo=50,retrans=2,'
            + ('actimeo=60,nocto\n' if immutable_cache else 'actimeo=0\n') +
            'mount -o bind /remote /disk\ncat /proc/mounts', 'remote-storage')
        for role in ('A', 'B'):
            self.shell(role, 'insmod /lab/pool_memory_module.ko\ntest -c /dev/cxl_pool', 'pool-driver')
        for role in ('A', 'B'):
            self.shell(role, 'sync\necho 3 > /proc/sys/vm/drop_caches', 'initial-host-cache-drop')
        parameters = {}
        for role in ('A', 'B'):
            raw, file = self.shell(role,
                'for entry in kvm/nx_huge_pages kvm/enable_pmu kvm_intel/pml kvm_intel/enable_apicv '
                'kvm_intel/flexpriority kvm_intel/preemption_timer; do\n'
                'module=${entry%/*}; option=${entry#*/}\n'
                'printf "KVM_PARAMETER %s " "$entry"; cat /sys/module/$module/parameters/$option\ndone',
                'kvm-parameters')
            parameters[role] = {'file': file, 'values':dict(re.findall(r'^KVM_PARAMETER (\S+) (\S+)\s*$', raw, re.M))}
        return {'immutable_nfs_cache': immutable_cache, 'kvm_parameters': parameters}

    def wait_event(self, role, directory, event, label=None, timeout=120):
        pattern = f'"event":"{event}"'
        if label is not None:
            pattern += f',"label":"{label}"'
        complete = re.escape(pattern) + r'.*}[[:space:]]*$'
        raw, name = self.shell(role, f'n=0\nwhile ! grep -Eq {shlex.quote(complete)} {directory}/fc.log; do\n'
            f'read p s < {directory}/fc.pid\nkill -0 "$p"\n'
            f"! grep -Eq 'Kernel panic|TRACE_FATAL' {directory}/fc.log\n"
            f'n=$((n+1))\n[ $n -lt {timeout*50} ]\nsleep .02\ndone\n'
            f'grep -E {shlex.quote(complete)} {directory}/fc.log', 'guest', timeout+15)
        rows = [json.loads(line) for line in raw.splitlines() if line.startswith('{"event":')]
        rows = [r for r in rows if r.get('event') == event and (label is None or r.get('label') == label)]
        require(len(rows) == 1 and rows[0].get('ok', True), f'Guest event failed: {raw}')
        return {'file': name, 'receipt': rows[0]}

    def trace_command(self, s, operation, *args, event=None, timeout=180):
        self.serial_number += 1
        token = f'v{s.vm}e{s.epoch}c{self.serial_number}'
        command = f'{operation} {args[0]} {token}' if operation in ('run', 'run_async') else ' '.join([operation, token, *map(str,args)])
        sent_ns = time.monotonic_ns()
        self.shell(s.role, f"printf '%s\\n' {shlex.quote(command)} > {s.directory}/input", 'guest-input')
        row = self.wait_event(s.role, s.directory, event or 'trace_identity', token, timeout)
        row.update(vm=s.vm, role=s.role, tier=s.tier, epoch=s.epoch, operation=operation, arguments=list(args),
                   host_send_ns=sent_ns, host_receive_ns=time.monotonic_ns())
        self.commands.append(row)
        return row

    def command_vm(self, s, op, *args):
        require(op in ('identity', 'check'), 'Unexpected synthetic command in SWE workload')
        return self.trace_command(s, 'identity')

    @staticmethod
    def same_identity(before, after):
        a, b = before['receipt'], after['receipt']
        for key in ('boot_id', 'pid', 'next_index', 'sentinel_sha256', 'fd_inode', 'fd_deleted'):
            require(a[key] == b[key], 'Transition changed trace identity ' + key)
        require(a['fd_deleted'] and b['fd_ordinal'] == a['fd_ordinal'] + 1 and
                b['fd_position'] > a['fd_position'], 'Open deleted FD offset did not continue')

    def frame(self, s, row, prefix, kind):
        raw = self.fetch_bytes(s.role, s.directory + '/fc.log', kind + '-serial')
        token = row['receipt']['label']
        match = re.search(rf'{prefix}_BEGIN {re.escape(token)} ([0-9]+) ([0-9a-f]{{64}})\r?\n(.*?){prefix}_END {re.escape(token)}', raw.decode(), re.S)
        require(match is not None, 'Missing guest export frame')
        data = base64.b64decode(''.join(match[3].split()), validate=True)
        require(len(data) == int(match[1]) == row['receipt']['bytes'] and
                hashlib.sha256(data).hexdigest() == match[2] == row['receipt']['sha256'], 'Guest export integrity')
        name = f'{len(self.receipts):04d}-{kind}.bin'
        (self.out / name).write_bytes(data)
        self.receipts.append({'file': name, 'kind': kind, 'sha256': hashlib.sha256(data).hexdigest()})
        return data, name

    def memory_operation(self, s, operation, **fields):
        plan = {"operation": operation, "vm_id": str(s.vm), "run_epoch": str(s.epoch),
                "pool_path": "/dev/dax0.0", **fields}
        self.write_json(s.role, s.plan, plan, "memory-plan")
        plan_file = self.raw("memory-plan-json", json.dumps(plan, separators=(",", ":")))
        before = len(self.api_events)
        self.api(s.role, s.directory, "/actions", "PUT", {"action_type": "MemoryTier"})
        result = self.fetch_json(s.role, s.report, "memory-operation")
        event = {"vm": s.vm, "epoch": s.epoch, "role": s.role, "operation": operation,
                 "report": {k: v for k, v in result.items() if k != "pages"},
                 "file": self.receipts[-1]["file"], "plan_file": plan_file, "apis": self.api_events[before:]}
        self.memory_events.append(event)
        return result, event

    def export(self, s):
        self.memory_operation(s, "export")
        manifest = self.fetch_json(s.role, s.manifest_path, "memory-manifest")
        accounting = validate_export(self.allocator, s.owner, manifest, self.memory_mib * MIB)
        s.manifest = manifest
        return {"manifest": manifest, "file": self.receipts[-1]["file"], "accounting": accounting}

    @staticmethod
    def export_evidence(value):
        return {k: v for k, v in value.items() if k != "manifest"}

    def promote(self, s):
        require(s.tier == 'dram', 'Promotion requires DRAM')
        before = self.command_vm(s, 'identity')
        self.pause(s)
        owner = f'vm{s.vm}:allocation{secrets.randbits(63) or 1}'
        layout = promotion_layout(self.allocator, owner, self.memory_mib * MIB)
        result, operation = self.memory_operation(s, 'promote', mappings=layout['mappings'],
            allocation_generation=layout['allocation_generation'])
        require(result['copied_private_bytes'] == self.memory_mib * MIB
                and result['ram_snapshot_bytes'] == 0, 'Promotion accounting')
        s.owner, s.tier = owner, 'cxl'
        exported = self.export(s)
        self.resume(s)
        after = self.command_vm(s, 'check')
        self.same_identity(before, after)
        row = {'kind': 'promotion', 'vm': s.vm, 'epoch': s.epoch, 'source_tier': 'dram',
            'destination_tier': 'cxl', 'source': s.role, 'destination': s.role,
            'operation': operation, 'export': self.export_evidence(exported),
            'before': before, 'after': after, 'allocator': self.allocator.summary(), 'result': 'passed'}
        self.transitions.append(row)
        return row
