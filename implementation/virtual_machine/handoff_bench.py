#!/usr/bin/env python3
"""SWE replay with VM-private RAM promotion and state-only/resident handoff.

Tier changes copy the entire RAM; CPU handoff retains CXL RAM. Preparation,
request-to-resume timing and independent workload verification remain separate.
"""
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
        if getattr(getattr(self, 'args', None), 'trajectory_policy', None):
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
    def __init__(self, start, end, require_invalidation=False):
        require(0 <= start < end and (start | end) % PAGE == 0, "invalid allocator bounds")
        self.require_invalidation = require_invalidation
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

    def release(self, owner, *, fenced, detached, invalidated_hosts=()):
        require(fenced is True and detached is True, "cannot release live/unfenced CXL backing")
        require(not self.require_invalidation or set(invalidated_hosts)=={"A","B"}, "both hosts must acknowledge invalidation before CXL reuse")
        require(owner in self.grants, "unknown or already released owner")
        grant = self.grants.pop(owner)
        returned = offset_ranges(grant.frames)
        ranges = sorted(self.free_ranges + [(r["offset"], r["offset"] + r["pages"]*PAGE) for r in returned])
        merged = []
        for start, end in ranges:
            require(not merged or merged[-1][1] <= start, "double-free CXL allocation")
            if merged and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        self.free_ranges = merged
        self.free_pages += len(grant.frames)
        self.events.append({"operation": "release", "owner": owner, "generation": grant.generation,
                            "pages": len(grant.frames), "ranges": returned,
                            "fenced": True, "detached": True, "invalidated_hosts":sorted(invalidated_hosts)})
        return sorted(grant.frames)

    def check(self):
        # Explicit diagnostic only; never run on allocate/release.
        seen = {p for start, end in self.free_ranges for p in range(start, end, PAGE)}
        require(len(seen) == self.free_pages, "free extent accounting mismatch")
        for grant in self.grants.values():
            require(not (seen & grant.frames), "cross-VM private frame alias")
            seen.update(grant.frames)
        require(seen == set(range(self.start, self.end, PAGE)), "allocator lost/out-of-range frames")

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
    direct: bool = False
    manifest: dict | None = None
    owner: str | None = None
    manifest_path: str = ""

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

class HandoffController(Controller):
    def __init__(self, config, out, args):
        Controller.__init__(self, config, out, args.memory_mib, getattr(args, "vcpus", 1))
        self.args, self.mode = args, args.mode
        # (metadata/data bytes, KVM arena) in MiB by emulated CXL size.
        metadata, arena = {2048: (512, 1024), 4096: (3072, 3584), 8192: (4096, 6144)}.get(config.get('cxl_mib'), (8192, 12288))
        self.metadata_base = self.data_bytes = metadata * MIB
        self.kvm_arena = self.arena_base = arena * MIB
        self.fixture = json.loads((Path(config['assets']) / 'manifest.json').read_text())
        self.trace = self.fixture['traces'][args.name]
        self.root = '/run/swe-handoff-' + os.urandom(8).hex()
        self.sandboxes = [Sandbox(vm=1)]
        s = self.sandboxes[0]
        s.direct, s.manifest, s.owner = self.mode == 'direct', None, None
        self.io_engine = 'Async'
        self.allocator = FrameAllocator(2 * MIB, (256 if config['cxl_mib']==2048 else 3072) * MIB, bool(getattr(args,'noncoherent',False)))
        self.commands, self.trace_commands = [], []
        self.memory_events, self.api_events, self.launches = [], [], []
        self.audits = []
        self.resident_readiness, self.resident_preparations = {}, []
        self.serial_number = 0
        self.kernel_traces = []

    def fetch_bytes(self, role, path, kind="fetch"):
        data, integrity = lab.migration_trace_fetch(self.vms[role], self.config, path)
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
        # Owned ivshmem scratch upload avoids giant serial commands. This region
        # is separate from CXL RAM and all transfers are sequential.
        compressed = gzip.compress(data)
        offset = 4*MIB
        require(len(compressed) <= self.config["shared_mib"]*MIB-offset, "Scratch upload exceeds ivshmem")
        if not hasattr(self, '_scratch_bars'):
            self._scratch_bars = {}
        if role not in self._scratch_bars:
            event = next(e for e in lab.events(Path(self.vms[role]["serial_log"]))
                         if e.get("event") == "shared_memory" and e.get("nonce") == self.config["nonce"])
            bdf = event["pci_bdf"]
            require(re.fullmatch(r"[0-9a-fA-F:.]+", bdf), "Invalid scratch PCI BDF")
            raw, _ = self.shell(role, f"sed -n '3p' /sys/bus/pci/devices/{bdf}/resource", "scratch-resource")
            rows = [line.split() for line in raw.splitlines() if re.fullmatch(r"\s*0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s*", line)]
            require(len(rows) == 1, "Scratch BAR missing")
            start, end, flags = (int(v, 16) for v in rows[0])
            require(flags & 0x200 and end-start+1 == self.config["shared_mib"]*MIB and start%4096 == 0, "Scratch BAR invalid")
            self._scratch_bars[role] = start
        start = self._scratch_bars[role]
        with Path(self.config["backing"]).open("r+b", buffering=0) as stream:
            stream.seek(offset)
            require(stream.write(compressed) == len(compressed), "Scratch write short")
        qpath = shlex.quote(path)
        return self.shell(role, f"dd if=/dev/mem bs=4096 skip={(start+offset)//4096} count={math.ceil(len(compressed)/4096)} 2>/dev/null | "
                   f"head -c {len(compressed)} | gzip -dc > {qpath}" + ("\n" + after if after else ""), kind, 120)

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
        if path == "/snapshot/create":
            require(self.mode == "state-only", "Direct mode must not invoke snapshot creation")
            require(body.get("mem_file_path") == "/dev/dax0.0", "Full-RAM snapshot path forbidden")
        start = time.monotonic_ns()
        value = super().api(role, directory, path, method, body)
        self.api_events.append(dict(value, control_wall_ns=time.monotonic_ns() - start))
        return value

    def start_vm(self, s, restore_manifest=None, direct_import=None, purpose="normal"):
        suffix = "" if purpose == "normal" else "-" + purpose
        s.directory = f"{self.root}/vm{s.vm}-epoch{s.epoch}{suffix}"
        s.plan = s.directory + "/memory-plan.json"
        s.report = s.directory + "/memory-report.json"
        s.manifest_path = s.directory + "/memory-manifest.json"
        env = {"PATH": "/bin:/sbin:/usr/bin:/usr/sbin:/lab", "FC_POOL_MEMORY": "1",
            "FC_POOL_VM_ID": str(s.vm), "FC_POOL_RUN_EPOCH": str(s.epoch),
            "FC_POOL_MEMORY_PLAN": s.plan, "FC_POOL_MEMORY_REPORT": s.report,
            "FC_POOL_MEMORY_METADATA_OFFSET": str(getattr(self, "metadata_base", 3072 * MIB) + s.vm * 64 * MIB),
            "FC_POOL_FULL_AUDIT": "1" if getattr(self.args, "full_audit", False) else "0",
            "FC_POOL_MEMORY_MANIFEST": s.manifest_path, "FC_POOL_MEMORY_DEVICE": "/dev/cxl_pool",
            "FC_DAX_FIXED_HVA": "0x400000000000", "FC_DAX_SNAPSHOT": "1",
            "FC_DAX_SNAPSHOT_COMPAT": "1"}
        if self.mode == 'ept-adopt':
            from ept_adopt import environment
            env.update(environment(self, s))
        if getattr(self.args,'resident_bench',None):
            env.update(FC_BENCH_EPOCH_GPA=str(64*MIB),FC_BENCH_INITIAL_EPOCH=str(s.epoch))
        if getattr(self.args, 'noncoherent', False):
            env['FC_NONCOHERENT']='1'
            if getattr(self.args,'clean_helper',False):env['FC_NONCOHERENT_HELPER']='1'
            if getattr(self.args,'service_probe',False) or getattr(self.args,'guest_io_probe',False):env['FC_SERVICE_PROBE']='1'
            if getattr(self.args,'tail_profile',False): env['FC_DIRTY_TIMING']='1'
            failure=getattr(self.args,'fail_maintenance',None)
            if failure and (s.role=='A' if failure=='writeback' else s.role=='B'): env['FC_NONCOHERENT_FAIL']=failure
            if s.role=='B' and getattr(self.args,'skip_invalidate_page',-1)>=0:
                env['FC_NONCOHERENT_SKIP_PAGE']=str(self.args.skip_invalidate_page)
        if getattr(self.args, 'dirty_ledger', False):
            env['FC_NONCOHERENT_NODE']='1' if s.role=='A' else '2'
            env.update(FC_DIRTY_LEDGER='1', FC_DIRTY_OUTPUT=s.directory+'/dirty')
            if getattr(self.args, 'dirty_oracle', False): env['FC_DIRTY_ORACLE'] = '1'
        if getattr(self.args, 'trajectory_policy', None):
            env['FC_DAX_NO_NESTED_GUEST'] = '1'
        if getattr(self.args, 'prefault', None) and purpose == 'snapshot-prepare':
            env.update(FC_PREFAULT_MODE=self.args.prefault,
                FC_PREFAULT_RANGES=s.directory+'/prefault-ranges.json',
                FC_PREFAULT_REPORT=s.directory+'/prefault-report.json',
                FC_PREFAULT_ACTIVATE='1' if self.args.prefault_activate else '0')
        resident = s.direct and not getattr(self.args, "mixed_io", False)
        if getattr(self.args, "mixed_io", False): env["FC_DIRTY_IO_TEST"] = "1"
        if resident:
            env.update(FC_DAX_RESIDENT="1", FC_RESIDENT_NATIVE="1",
                FC_RESIDENT_BLOCK_PATH="/disk/sandbox.raw")
        if restore_manifest:
            local = s.directory + "/restore-manifest.json"
            if purpose not in ("resident-prepare", "snapshot-prepare"):
                self.shell(s.role, f"mkdir -p {s.directory}\ncp {shlex.quote(restore_manifest)} {local}",
                           "local-memory-manifest", 120)
            env["FC_POOL_MEMORY_RESTORE"] = local
            env["FC_POOL_MEMORY_LEASE"] = str(self.allocator.grants[s.owner].generation)
        if s.direct:
            env.update(FC_DAX_DIRECT="1", FC_DAX_DIRECT_IO="1",
                FC_DAX_DIRECT_EXPORT=s.directory + "/direct-export.json")
        if getattr(self.args, "restore_timing", False):
            env["FC_RESTORE_TIMING"] = "1"
        if direct_import:
            env["FC_DAX_DIRECT_IMPORT"] = direct_import
            env["FC_DAX_RAM"] = "/dev/dax0.0"
        self.active.append((s.role, s.directory))
        # Upload a launch script: BusyBox interactive ash truncates very long
        # physical serial lines before execution. Keep console commands short.
        native = purpose == 'resident-prepare'
        if not native:
            self.shell(s.role, f"mkdir -p {s.directory}", "launch-directory")
        script = "set -e\n" + "\n".join("export " + shlex.quote(k + "=" + v)
            for k, v in env.items()) + "\n"
        script += f"cat > {s.directory}/environment.json <<'RESIDENT_ENV_JSON'\n" + json.dumps(env) + "\nRESIDENT_ENV_JSON\n"
        script += (f"exec /lab/firecracker --no-seccomp --api-sock {s.directory}/api.sock "
                   f"--log-path {s.directory}/vmm.log\n")
        script_path = self.root + '/resident-launch-' + s.role + '.sh' if native else s.directory + '/launch.sh'
        if native:
            start_command = f'/lab/resident-bootstrap {s.directory} {script_path}'
        else:
            start_command = (f"mkfifo {s.directory}/input\n"
                f"(exec 3<>{s.directory}/input\nexec env -i /bin/sh {s.directory}/launch.sh "
                f"<&3 > {s.directory}/fc.log 2> {s.directory}/stderr.log) &\n" + nfs.record_pid(s.directory) +
                f"n=0\nwhile [ ! -S {s.directory}/api.sock ]; do\nread p t < {s.directory}/fc.pid\n"
                "kill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 1000 ]\nsleep .02\ndone")
        # Upload and launch in one console transaction. The bootstrap still
        # measures native fork/API readiness independently of this transport.
        raw, executable_file = self.write_bytes(s.role, script_path, script.encode(),
            "native-prepare" if native else "start",
            after=start_command + f"\nread p t < {s.directory}/fc.pid\nprintf 'EXEC_PROCESS %s %s\\n' $p $t")
        self.launches.append({"vm": s.vm, "epoch": s.epoch, "role": s.role,
                              "file": executable_file, "directory": s.directory, "purpose": purpose, "runtime_rehashed": False})
        if resident:
            if not hasattr(self, "resident_workers"): self.resident_workers = {}
            self.resident_workers[s.role] = {key: getattr(s, key) for key in
                ("directory", "plan", "report", "manifest_path")}
            self.resident_workers[s.role]["imported"] = purpose != "resident-prepare"
            if native:
                boots = json_lines(raw, 'RESIDENT_BOOT_JSON ')
                answers = json_lines(raw, 'LAB_API_JSON ')
                require(len(boots) == 1, 'Native process readiness missing')
                require(len(answers) == 1 and answers[0]['status'] == 204, 'Native preparation failed')
                api = {'file':executable_file, 'role':s.role, 'path':'/actions', 'method':'PUT',
                    'request':{'action_type':'DirectPrepare'}, 'response':answers[0],
                    'control_wall_ns':0, 'embedded_in':executable_file}
                self.api_events.append(api)
                self.resident_workers[s.role].update(prepared_api=api, native_boot=boots[0], native_boot_file=executable_file)
        if s.direct and not native:
            self.shell(s.role, f"read p t < {s.directory}/fc.pid\necho $p > /sys/module/kvm/parameters/dax_owner_pid\n"
                       "cat /sys/module/kvm/parameters/dax_owner_pid", "select-direct-owner")

    def configure_direct(self):
        for role, node, mode in (('A', 1, 1), ('B', 2, 2)):
            if getattr(self.args,'tail_profile',False):
                self.shell(role,'echo 1 > /proc/sys/kernel/sched_schedstats\necho 1 > /sys/module/kvm/parameters/dax_dirty_timing','enable-dirty-timing')
            timing='echo 1 > /proc/sys/kernel/sched_schedstats\necho 1 > /sys/module/kvm/parameters/dax_dirty_timing\n' if getattr(self.args,'tail_profile',False) else ''
            cache = 'echo 1 > /sys/module/kvm/parameters/dax_noncoherent\necho 1 > /sys/module/kvm/parameters/dax_cache_check\n' if getattr(self.args,'noncoherent',False) else ''
            if role=='A' and getattr(self.args,'cache_skip_line',-1)>=0:
                cache+=f'echo {self.args.cache_skip_line} > /sys/module/kvm/parameters/dax_cache_skip_line\n'
            if getattr(self.args,'arena_limit_bytes',0): cache+=f'echo {self.args.arena_limit_bytes} > /sys/module/kvm/parameters/dax_vm_limit_bytes\n'
            self.shell(role, timing+cache+'echo 2147483647 > /sys/module/kvm/parameters/dax_owner_pid\n'
                + ('echo 1 > /sys/module/kvm/parameters/dax_handoff_profile\n' if self.args.restore_timing else '') +
                (f'echo {int(getattr(self.args, "resume_profile",False))} > /sys/module/kvm/parameters/dax_resume_profile\n' if getattr(self.args,'resident_bench',None) else '') +
                'resource=$(cat /sys/bus/dax/devices/dax0.0/resource)\n'
                'echo 3 > /sys/module/kvm/parameters/dax_handoff_mode\n'
                'echo 1 > /sys/module/kvm/parameters/dax_nested_root_sync\n'
                'echo 1 > /sys/module/kvm/parameters/dax_scattered\n'
                'echo $resource > /sys/module/kvm/parameters/dax_data_base\n'
                f'echo {self.data_bytes} > /sys/module/kvm/parameters/dax_data_bytes\n'
                f'echo {node} > /sys/module/kvm/parameters/dax_node\necho {mode} > /sys/module/kvm/parameters/dax_shared\n'
                f"printf '%x,%x\\n' \"$((resource + {self.kvm_arena}))\" {128*MIB} > /sys/module/kvm/parameters/dax_pool\n"
                'cat /sys/module/kvm/parameters/dax_pool', 'direct-kernel-setup')

    def boot_vm(self, s):
        self.start_vm(s)
        components = {'/machine-config': {'vcpu_count': self.vcpus, 'mem_size_mib': self.memory_mib},
            '/boot-source': {'kernel_image_path': '/lab/l2-vmlinux',
                'initrd_path': f'/lab/{self.trace["name"]}-initramfs.cpio',
                'boot_args': 'console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init' + ((' memmap=2M$64M nmi_watchdog=0'+(' memmap=256M$128M' if getattr(self.args,'dirty_lines',0) else '')) if getattr(self.args,'resident_bench',None) else '')},
            '/drives/data': {'drive_id': 'data', 'path_on_host': '/disk/sandbox.raw', 'is_root_device': False,
                'is_read_only': True, 'cache_type': 'Writeback', 'io_engine': self.io_engine}}
        if self.mode == 'ept-adopt' and getattr(self.args,'dirty_mib',0)==1024:
            components['/boot-source']['boot_args']=components['/boot-source']['boot_args'].replace('memmap=256M$128M','memmap=1024M$128M')
        if getattr(self.args, 'dirty_ledger', False):
            components['/machine-config']['track_dirty_pages'] = True
        if getattr(self.args, 'mixed_io', False):
            components['/network-interfaces/net'] = {'iface_id':'net','host_dev_name':'fcnet0','guest_mac':'06:00:ac:1f:00:02'}
            components['/vsock'] = {'guest_cid':3,'uds_path':self.root+'/io.sock'}
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
                lab.snapshot_cleanup_script(include_boot_vm=True) +
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
            self.shell(role, 'insmod /lab/pool_memory_module.ko\ntest -c /dev/cxl_pool\n'
                'echo 1 > /sys/module/kvm/parameters/dax_scattered', 'pool-driver')
        for role in ('A', 'B'):
            self.shell(role, 'sync\necho 3 > /proc/sys/vm/drop_caches', 'initial-host-cache-drop')
        self.keepalives = []
        for role in (() if self.mode == 'ept-adopt' else ('A', 'B')):
            directory = self.root+'/keepalive'
            raw, name = self.shell(role, f'mkdir {directory}\n'
                f'/lab/shared-probe kvm-keepalive > {directory}/log 2>&1 &\n'+nfs.record_pid(directory)+
                f'n=0\nwhile ! grep -q KVM_KEEPALIVE_READY {directory}/log; do\n'
                'n=$((n+1))\n[ $n -lt 100 ]\nsleep .01\ndone\n'+f'cat {directory}/log', 'keepalive')
            self.keepalives.append({'role': role, 'directory': directory, 'file': name})
        parameters = {}
        for role in ('A', 'B'):
            raw, file = self.shell(role,
                'for entry in kvm/nx_huge_pages kvm/enable_pmu kvm_intel/pml kvm_intel/enable_apicv '
                'kvm_intel/flexpriority kvm_intel/preemption_timer; do\n'
                'module=${entry%/*}; option=${entry#*/}\n'
                'printf "KVM_PARAMETER %s " "$entry"; cat /sys/module/$module/parameters/$option\ndone',
                'kvm-parameters')
            parameters[role] = {'file': file, 'values':dict(re.findall(r'^KVM_PARAMETER (\S+) (\S+)\s*$', raw, re.M))}
        return {'immutable_nfs_cache': immutable_cache, 'keepalive': self.keepalives, 'kvm_parameters': parameters}

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

    def inflight_command(self, s, index, timeout):
        require(s.direct and s.role == 'B' and s.tier == 'cxl', 'In-flight test requires remote CXL execution')
        started = self.trace_command(s, 'run_async', index, event='trace_async_started')
        expected = started['receipt']
        observations, hops = [], []
        for destination in ('A', 'B', None):
            live = self.trace_command(s, 'running', event='trace_running')
            value = live['receipt']
            require(value['active'] and value['process_state'] not in ('Z','X','x') and
                    all(value[key] == expected[key] for key in ('index','command_pid','start_ticks')),
                    'Workload ended/replaced before the in-flight check')
            observations.append(live)
            if destination is not None:
                hops.append(self.direct_handoff(s, destination))
        ended = self.trace_command(s, 'collect', event='trace_end', timeout=timeout)
        require(ended['receipt']['run_label'] == expected['label'], 'Collected another command')
        ended['inflight'] = {'started':started, 'observations':observations,
                            'handoff_files':[hop['fast_file'] for hop in hops]}
        return ended

    def parallel_build(self, s):
        started = self.trace_command(s, 'build_async', event='trace_async_started')
        observations, hops = [], []
        for destination in (('B', 'A') * 3 + (None,) if s.direct else (None,) * 7):
            time.sleep(.15)
            live = self.trace_command(s, 'running', event='trace_running')
            value = live['receipt']
            require(value['active'] and all(value[k] == started['receipt'][k]
                    for k in ('index', 'command_pid', 'start_ticks')), 'Parallel build ended or was replaced')
            observations.append(live)
            if destination:
                hops.append(self.direct_handoff(s, destination))
        ended = self.trace_command(s, 'collect', event='trace_build_end', timeout=330)
        require(ended['receipt']['rc'] == 0 and not ended['receipt']['timed_out'], 'Parallel build failed')
        return {'command':'go build -a -p "$(getconf _NPROCESSORS_ONLN)" ./...',
                'started':started, 'observations':observations, 'hops':[h['fast_file'] for h in hops], 'ended':ended}

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

    def prepare_snapshot_target(self, s, destination):
        import copy
        target = copy.copy(s)
        target.role, target.epoch = destination, s.epoch + 1
        started = time.monotonic_ns()
        self.start_vm(target, restore_manifest='deferred', purpose='snapshot-prepare')
        # An empty API process has no KVM VM/vCPU. Its deferred manifest is
        # published only after snapshot/create has sealed the source mapper.
        raw, evidence = self.shell(destination,
            f'read p t < {target.directory}/fc.pid\ncat /proc/$p/maps\nls -l /proc/$p/fd', 'snapshot-ready')
        require('kvm-vm' not in raw and 'kvm-vcpu' not in raw, 'Prepared snapshot worker owns a VM')
        if getattr(self.args, 'trajectory_policy', None):
            import control_rpc
            answer = control_rpc.http(self.vms[destination], target.directory+'/api.sock', 'GET', '/', {})
            self.raw('snapshot-control-ready', answer)
            require(json_lines(answer, 'LAB_API_JSON ')[0]['status'] == 200, 'Snapshot API readiness failed')
        target.preparation = {'ready_ns': time.monotonic_ns(), 'start_ns': started,
                              'file': evidence, 'directory': target.directory, 'source_paused': False}
        self.snapshot_target = target
        return target.preparation

    def state_handoff(self, s, destination):
        require(self.mode == 'state-only' and s.tier == 'cxl' and destination != s.role, 'Invalid state handoff')
        source, old_directory, old_epoch = s.role, s.directory, s.epoch
        target = getattr(self, 'snapshot_target', None)
        require(target is not None and target.role == destination and target.epoch == s.epoch + 1,
                'Prepare a fresh snapshot destination before requesting handoff')
        started = time.monotonic_ns()
        self.pause(s)
        source_ranges = None
        if getattr(self.args, 'prefault', None) == 'mapped':
            source_ranges, event = self.memory_operation(s, 'prefault-ranges')
            require(source_ranges['result'] == 'passed' and source_ranges['ram_snapshot_bytes'] == 0,
                    'Source mapped-range query failed')
            self.write_json(destination, target.directory+'/prefault-ranges.json', source_ranges,
                            'prefault-range-transfer')
        state_path = old_directory + '/state-only.bin'
        # Snapshot creation already drains devices and seals the current mapper.
        create = self.api(source, old_directory, '/snapshot/create', 'PUT',
            {'snapshot_type': 'Full', 'snapshot_path': state_path, 'mem_file_path': '/dev/dax0.0'})
        state = self.fetch_bytes(source, state_path, 'state-only')
        state_file = self.receipts[-1]['file']
        manifest = self.fetch_bytes(source, s.manifest_path, 'state-bound-manifest')
        manifest_file = self.receipts[-1]['file']
        require(0 < len(state) < MIB, 'Unexpected state-only payload size')
        stopped = self.raw('state-source-stop', nfs.stop_fc(self.vms[source], old_directory))
        self.write_bytes(destination, target.directory+'/state-only.bin', state, 'state-only-transfer')
        self.write_bytes(destination, target.directory+'/restore-manifest.json', manifest, 'placement-transfer')
        s.epoch, s.role = target.epoch, destination
        for key in ('directory', 'plan', 'report', 'manifest_path'):
            setattr(s, key, getattr(target, key))
        # Restore consumes the sealed handle and resumes only after validation.
        load = self.api(destination, s.directory, '/snapshot/load', 'PUT', {'snapshot_path': target.directory+'/state-only.bin',
            'mem_backend': {'backend_type': 'File', 'backend_path': '/dev/dax0.0'}, 'resume_vm': True})
        s.manifest = None
        row = {'kind': 'state-only', 'runtime_path': 'state-v2', 'vm': s.vm, 'source': source, 'destination': destination,
            'source_epoch': old_epoch, 'destination_epoch': s.epoch, 'source_tier': 'cxl', 'destination_tier': 'cxl',
            'source_stop_file': stopped, 'create_api': create, 'load_api': load,
            'state_file': state_file, 'state_bytes': len(state),
            'manifest_file': manifest_file, 'manifest_bytes': len(manifest), 'ram_snapshot_bytes': 0,
            'ram_transfer_bytes': 0, 'request_ns': started, 'resume_ns': time.monotonic_ns(),
            'preparation': target.preparation, 'target_directory':target.directory,
            'prefault_mode':getattr(self.args, 'prefault', None), 'source_ranges':source_ranges,
            'controller_pause_to_resume_ns': time.monotonic_ns()-started, 'result': 'passed'}
        self.snapshot_target = None
        self.transitions.append(row)
        return row

    def direct_handoff(self, s, destination):
        if not getattr(self.args, 'kernel_trace', False):
            return self.resident_handoff(s, destination)
        import kernel_handoff_trace
        # Restrict probes to one handoff at a time: do not trace the whole SWE
        # workload or let unrelated TLB events overflow the diagnostic buffer.
        kernel_handoff_trace.start(self, reuse=bool(self.kernel_traces))
        epoch = s.epoch + 1
        kernel_handoff_trace.mark(self, epoch)
        row = self.resident_handoff(s, destination)
        self.kernel_traces.append({'epoch':epoch, 'destination':destination,
                                  'nodes':kernel_handoff_trace.finish(self)})
        return row

    def resident_handoff(self, s, destination):
        require(self.mode == 'direct' and s.direct and s.tier == 'cxl' and destination != s.role,
                'Direct handoff requires selected CXL VM and another host')
        from resident_fast import handoff
        return handoff(self, s, destination)

    def prepare_resident_target(self, s, destination):
        import copy
        if destination in getattr(self, 'resident_readiness', {}):
            from resident_fast import prepared_target
            return prepared_target(self, s, destination)
        first_control = len(getattr(self, 'control_timings', []))
        fresh = destination not in self.resident_workers
        started = time.monotonic_ns()
        if fresh:
            target = copy.copy(s)
            target.role, target.epoch = destination, s.epoch + 1
            directory = f"{self.root}/vm{s.vm}-epoch{target.epoch}-resident-prepare"
            self.start_vm(target, restore_manifest=directory+"/restore-manifest.json",
                direct_import=directory+"/direct-import.json", purpose="resident-prepare")
            if getattr(self.args,'noncoherent',False) and not getattr(self.args,'legacy_observer',False):
                require(s.manifest is not None,'Cold observer needs the initial allocation identity')
                self.write_json(destination,directory+'/restore-manifest.json',s.manifest,'observer-baseline')
                self.api(destination,directory,'/actions','PUT',{'action_type':'DirectObserve'})
        worker = self.resident_workers[destination]
        api = worker.get('prepared_api') if fresh else None
        if api is None:
            api = self.api(destination, worker["directory"], "/actions", "PUT", {"action_type":"DirectPrepare"})
        row = {"source":s.role, "destination":destination, "before_epoch":s.epoch,
            "fresh":fresh, "api":api, "controller_wall_ns":time.monotonic_ns()-started,
            "source_paused":False, "directory":worker["directory"]}
        row['control_stages'] = getattr(self, 'control_timings', [])[first_control:]
        row['api_control_wall_ns'] = self.api_events[-1]['control_wall_ns']
        row['unattributed_control_ns'] = (row['controller_wall_ns'] - row['api_control_wall_ns']
            - sum(stage['elapsed_ns'] for stage in row['control_stages']))
        if 'native_boot' in worker:
            row.update(native_boot=worker['native_boot'], native_boot_file=worker['native_boot_file'])
        if not hasattr(self, "resident_preparations"): self.resident_preparations=[]
        self.resident_preparations.append(row)
        return row

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

    def demote(self, s):
        require(s.tier == "cxl", "Demotion requires CXL VM")
        before = self.command_vm(s, "identity")
        self.pause(s)
        previous = self.export(s)
        result, operation = self.memory_operation(s, "demote")
        require(result["ram_snapshot_bytes"] == 0, "Demotion generated a full RAM snapshot")
        raw, maps_file = self.shell(s.role, f"read p t < {s.directory}/fc.pid\ncat /proc/$p/maps", "post-demotion-maps")
        base, end = 0x400000000000, 0x400000000000 + self.memory_mib * MIB
        mappings = [m for m in parse_maps(raw) if m["start"] < end and m["end"] > base]
        cursor = base
        for m in mappings:
            require(m["start"] == cursor and m["end"] <= end and m["permissions"] == "rw-p"
                    and m["inode"] == 0 and m["path"] == "", "Demotion retains nonprivate/non-DRAM backing")
            cursor = m["end"]
        require(cursor == end, "Demotion anonymous RAM mapping incomplete")
        invalidated=[]
        if getattr(self.args,'noncoherent',False):
            from resident_fast import invalidate_grant
            invalidated=invalidate_grant(self,s,previous['manifest'])
        freed = self.allocator.release(s.owner, fenced=True, detached=True,invalidated_hosts=invalidated)
        s.owner, s.tier, s.manifest = None, "dram", None
        self.resume(s)
        after = self.command_vm(s, "check")
        self.same_identity(before, after)
        row = {"kind": "demotion", "vm": s.vm, "epoch": s.epoch, "source_tier": "cxl",
            "destination_tier": "dram", "source": s.role, "destination": s.role, "before": before,
            "after": after, "previous": self.export_evidence(previous), "operation": operation,
            "returned_ranges": offset_ranges(freed),
            "dram_maps_file": maps_file, "dram_maps": mappings,
            "allocator": self.allocator.summary(), "result": "passed"}
        self.transitions.append(row)
        return row

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

    def audit_memory(self, s):
        self.pause(s)
        raw, operation = self.memory_operation(s, 'audit')
        pages = raw['pages']
        grant = self.allocator.grants[s.owner].frames
        require(len(pages)*PAGE == self.memory_mib*MIB, 'Audit omitted RAM')
        for index, page in enumerate(pages):
            require(page['gpa'] == index*PAGE and page['offset'] in grant, 'Audit escaped private grant')
            if page['present']:
                require(page['writable'] and page['actual_pfn']*PAGE == raw['phys_base']+page['offset'],
                        'Private RAM PTE mismatch')
        self.resume(s)
        self.audits.append(operation)
        return operation


def trajectory_replay(c, s, report, save):
    import kernel_handoff_trace as kt
    args = c.args
    plan = Path(args.heavy_plan).read_text()
    report['heavy_plan_file'] = c.raw('heavy-plan', plan)
    heavy = json.loads(plan)['indices'][args.name]
    moving = args.mode != 'baseline'
    boundaries = [x['index'] for x in c.trace['commands']
                  if moving and (args.trajectory_policy == 'all' or x['index'] in heavy)]
    report['boundaries'] = boundaries
    c.promote(s)  # All modes, including no-move, execute from the same CXL RAM.
    if args.mode == 'direct':
        from resident_fast import prepare
        prepare(c, s)
    if args.mode == 'state-only' and boundaries:
        c.prepare_snapshot_target(s, 'B')
    if args.count_faults:
        kt.trajectory_start(c)
        report['fault_initial'] = kt.trajectory_counts(c)
    recorded_waits = {}
    if args.clean_wait == 'recorded':
        source = Path(c.config['assets'])/'traces'/f'{args.name}.traj.json'
        messages = json.loads(source.read_text())['messages']
        batch = -1
        last_tool = None
        for message in messages:
            stamp = message.get('extra',{}).get('timestamp')
            if message['role'] == 'assistant' and message.get('extra',{}).get('actions'):
                if last_tool is not None and stamp is not None:
                    recorded_waits[batch] = max(0.0, float(stamp)-float(last_tool))
                batch += 1
                last_tool = None
            elif message['role'] == 'tool' and stamp is not None:
                last_tool = stamp
        report['recorded_waits_s'] = recorded_waits
        report['wait_source'] = str(source)
    started = time.monotonic_ns()
    for command in c.trace['commands']:
        index = command['index']
        if index in boundaries:
            destination = 'B' if s.role == 'A' else 'A'
            if args.count_faults:
                kt.mark(c, s.epoch+1)
            requested = time.monotonic_ns()
            hop = (c.direct_handoff if s.direct else c.state_handoff)(s, destination)
            hop.update(host_request_ns=requested, host_resume_ns=time.monotonic_ns(), before_command=index)
            if args.count_faults and args.mode == 'state-only':
                raw, file = c.shell(s.role, f'read p t < {s.directory}/fc.pid\n'
                    'cat /proc/$p/maps\nls -l /proc/$p/fd', 'snapshot-restored')
                require('kvm-vm' in raw and 'kvm-vcpu' in raw, 'Restored KVM objects missing')
                hop['restored_file'] = file
        before = kt.trajectory_counts(c) if args.count_faults else None
        row = c.trace_command(s, 'run', index, event='trace_end',
                              timeout=int(c.trace['environment'].get('timeout', 60))+30)
        value = row['receipt']
        require(not value['timed_out'] and value['command_sha256'] == command['command_sha256'] and
                value['rc'] == (command['expected_returncode'] or 0), f'Trace diverged at {index}: {value}')
        row['guest_duration_s'] = value['end_s']-value['start_s']
        row['host_duration_ns'] = row['host_receive_ns']-row['host_send_ns']
        if args.count_faults:
            after = kt.trajectory_counts(c)
            row['fault_before'], row['fault_after'] = before, after
            row['faults'] = {role: {key: after[role][key]-before[role][key]
                for key in ('ept_violation', 'page_fault', 'ram_ept_violation', 'mmio_ept_violation')} for role in ('A', 'B')}
        c.trace_commands.append(row)
        if args.preclean:
            from resident_fast import maintain
            report.setdefault('cleans',[]).append(maintain(c,s))
            # Wait belongs to the next LLM interval, before a heavy-command move.
            if index+1 in boundaries:
                same_batch=(index+1<len(c.trace['commands']) and c.trace['commands'][index+1]['batch']==command['batch'])
                delay=0
                if args.clean_wait=='1': delay=1.0
                elif args.clean_wait=='recorded' and not same_batch: delay=min(10.0,recorded_waits[command['batch']])
                # LLM time starts when the tool result is received. Cleaning
                # uses that interval; only the remaining part is slept here.
                deadline=row['host_receive_ns']+int(delay*1e9)
                begin=time.monotonic_ns()
                time.sleep(max(0,(deadline-begin)/1e9))
                ended=time.monotonic_ns()
                report.setdefault('waits',[]).append(dict(after_command=index,planned_s=delay,
                    sleep_ns=ended-begin,actual_ns=ended-row['host_receive_ns'],
                    late_ns=max(0,ended-deadline)))
        # Keep the next fresh destination ready while the current VM owns RAM.
        # This preparation is outside pause and command intervals; its wall
        # cost remains visible in observed_replay_ns.
        if args.mode == 'state-only' and index in boundaries and index != boundaries[-1]:
            c.prepare_snapshot_target(s, 'B' if s.role == 'A' else 'A')
        save()
    report['observed_replay_ns'] = time.monotonic_ns()-started
    report['trajectory_handoff_ns'] = sum(t['host_resume_ns']-t['host_request_ns']
        for t in c.transitions if 'host_request_ns' in t)
    report['trajectory_total_ns'] = report['trajectory_handoff_ns'] + sum(x['host_duration_ns'] for x in c.trace_commands)
    report['trajectory_clock'] = 'L0 CLOCK_MONOTONIC ns; send FIFO request through RPC to complete trace_end receipt. Includes control polling. Total=sum(command intervals)+request-to-resume intervals. Observed wall includes preparation, collection and JSON writes.'
    if args.count_faults:
        report['fault_final'] = kt.trajectory_counts(c)
        report['spte_final'] = kt.trajectory_mappings(c, s)
        for role in ('A', 'B'):
            raw, file = c.shell(role, 'T=/sys/kernel/tracing\necho 0 > $T/tracing_on\n'
                'cat $T/trace\ncat $T/per_cpu/cpu*/stats', 'trajectory-nested-trace')
            report.setdefault('nested_traces', []).append({'role': role, 'file': file,
                'intervals': kt.parse(raw) if args.mode == 'direct' and boundaries else []})
    if args.prefault:
        for hop in c.transitions:
            if hop.get('prefault_mode'):
                receipt = c.fetch_json(hop['destination'], hop['target_directory']+'/prefault-report.json', 'prefault-report')
                require(receipt['result'] == 'passed' and receipt['processed_pages'] == receipt['expected_pages'],
                        'Incomplete prefault before Resume')
                hop.update(prefault=receipt, prefault_file=c.receipts[-1]['file'])
    c.demote(s)


def mixed_io(args):
    """Exercise all RAM writers with closed I/O connections and fresh VMM wrappers."""
    from dirty_oracle import DirtyOracle
    run, config = lab.current_run()
    out = run / ('io-mix-' + str(time.time_ns()))
    out.mkdir()
    c = HandoffController(config, out, args)
    s = c.sandboxes[0]
    report = {'schema':'noncoherent-io-mix-v1', 'result':'running', 'arguments':vars(args),
              'config':config, 'transitions':c.transitions, 'receipts':c.receipts, 'io':[]}
    report['source_hashes'], report['source_reference'] = record_sources(HERE,
        ('handoff_bench.py','io_mix.c','dirty_oracle.py','page_hash.c','firecracker.patch',
         'kvm-dax-state.patch','swe-handoff-guest-run.sh','multi_vcpu.py'))
    oracle = DirtyOracle(config, out, c.metadata_base+s.vm*64*MIB, c.kvm_arena, args.page_hash)
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report['storage_setup'] = c.setup()
        c.configure_direct()
        for role in ('A','B'):
            c.shell(role, 'test -x /lab/io-mix\n/lab/io-mix tap fcnet0 172.31.0.1', 'L1-only-tap')
        report['boot'] = c.boot_vm(s)
        identity = c.command_vm(s, 'identity')
        c.promote(s)
        generation = 0
        for epoch in range(1, 2*args.iterations+1):
            c.trace_command(s,'io_start',str(epoch),event='trace_io_started')
            raw, host_file = c.shell(s.role, f'/lab/io-mix host {epoch} {c.root}/io.sock', 'io-host')
            require(f'IO_MIX_HOST_PASS {epoch}' in raw, 'I/O sender did not verify reply')
            guest = c.trace_command(s,'io_finish',str(epoch),event='trace_io_result')
            payload = guest['receipt']['payload']
            require(payload == {'epoch':epoch,'ram_bytes':16*MIB,'block_bytes':MIB,
                    'udp_bytes':16384,'vsock_bytes':65536,'checksum':8355840}, 'I/O payload verification failed')
            report['io'].append({'source':s.role,'epoch':epoch,'host_file':host_file,'guest':guest})
            source, old_directory = s.role, s.directory
            identity = c.command_vm(s, 'identity')
            c.pause(s)
            c.api(source,old_directory,'/actions','PUT',{'action_type':'DirectExport'})
            state = c.fetch_bytes(source,old_directory+'/direct-export.json','io-direct-state')
            manifest = c.fetch_bytes(source,s.manifest_path,'io-sealed-manifest')
            token = json.loads(state)['token']
            require(token['generation']==generation+1 and token['vm_creates']==1
                    and token['vcpu_creates']==args.vcpus, 'I/O test recreated or skipped the KVM graph')
            generation = token['generation']
            nfs.stop_fc(c.vms[source],old_directory)
            c.shell(source,source_fence_script(old_directory),'io-source-fence')
            destination = 'B' if source=='A' else 'A'
            state_path = f'{c.root}/io-state-{epoch}.json'
            manifest_path = f'{c.root}/io-manifest-{epoch}.json'
            c.write_bytes(destination,state_path,state,'io-state-transfer')
            c.write_bytes(destination,manifest_path,manifest,'io-manifest-transfer')
            # This socket belongs only to the fenced previous wrapper in this L1.
            c.shell(destination,f'rm -f {c.root}/io.sock','io-retired-socket')
            if args.drop_consumed_once and epoch==12:
                handle=json.loads(manifest)['handle']
                path=f"/run/noncoherent-{handle['cookie_lo']:016x}-{handle['cookie_hi']:016x}.epoch"
                c.shell(destination,'rm -f '+path,'drop-owned-consumed-state')
                report['consumed_reset']={'epoch':epoch,'node':destination,'path':path}

            s.role, s.epoch = destination, s.epoch+1
            c.start_vm(s,restore_manifest=manifest_path,direct_import=state_path,purpose='mixed-import')
            c.api(destination,s.directory,'/actions','PUT',{'action_type':'InstanceStart'})
            c.resume(s)
            c.same_identity(identity,c.command_vm(s,'identity'))
            c.transitions.append({'kind':'mixed-direct','source':source,'destination':destination,
                                  'generation':generation,'token':token,'result':'passed'})
            lab.save_json(out/'report.json',report)
        c.demote(s)
        report['result'] = 'passed'
    except BaseException as error:
        report.update(result='failed',error=repr(error))
        raise
    finally:
        try:
            oracle.close()
        except Exception as error:
            report.update(result='failed',oracle_error=repr(error))
        report['dirty_oracle'] = oracle.rows
        for role,directory in reversed(c.active):
            try:
                nfs.stop_fc(c.vms[role],directory)
                c.fetch_bytes(role,directory+'/vmm.log','io-final-vmm')
                c.fetch_bytes(role,directory+'/stderr.log','io-final-stderr')
            except Exception as error:
                report.setdefault('collection_errors',[]).append(repr(error))
        for keeper in getattr(c,'keepalives',[]):
            nfs.stop_fc(c.vms[keeper['role']],keeper['directory'])
        lab.save_json(out/'report.json',report)
        print('IO_MIX_REPORT '+str(out/'report.json'),flush=True)


def resident_measure(args):
    """Exercise the actual resident device/mapper path with a shared guest ACK.

    No Python clock crosses a handoff boundary. The source C controller samples
    CLOCK_MONOTONIC before send/pause and after acquire-observing the guest ACK.
    Each direction has its own warmup and measured sample count.
    """
    run, config = lab.current_run()
    out = run / ('resident-bench-' + str(time.time_ns()))
    out.mkdir()
    c = HandoffController(config, out, args)
    s = c.sandboxes[0]
    report = {'result':'running', 'arguments':vars(args), 'config':config,
              'transitions':c.transitions, 'receipts':c.receipts,
              'boundary':'source C CLOCK_MONOTONIC immediately before resident send/pause to acquire-observed remote guest ACK; 50us polling included',
              'idle':'one heartbeat sleeps 1ms; other vCPUs have no workload',
              'busy':'same pinned 512KiB checksum writers and futex ring as stage 15'}
    sources = ('dirty_oracle.py','page_hash.c','handoff_bench.py','resident_fast.py','resident_control.c','resident_wire.h',
               'shared_probe.c','swe-handoff-guest-run.sh','multi_vcpu.py','tail_probe.py')
    report['source_hashes'],report['source_reference'] = record_sources(HERE,sources)
    oracle = None
    if args.dirty_oracle:
        from dirty_oracle import DirtyOracle
        oracle = DirtyOracle(config, out, c.metadata_base+s.vm*64*MIB, c.kvm_arena, args.page_hash)
    try:
        lab.verify(argparse.Namespace(timeout=180))
        c.setup()
        c.configure_direct()
        report['boot'] = c.boot_vm(s)
        report['heartbeat'] = c.trace_command(s,'heartbeat',('dirty'+str(args.dirty_mib)+(f':{args.dirty_lines}:{args.dirty_mode}' if args.dirty_lines else '')) if args.resident_bench=='dirty' else args.resident_bench,event='trace_heartbeat')
        time.sleep(.2)
        c.promote(s)
        frames = sorted(c.allocator.grants[s.owner].frames)
        require(frames == list(range(2*MIB, (2+args.memory_mib)*MIB, PAGE)), 'Benchmark needs the observed contiguous private grant')
        c.bench_offset = frames[64*MIB//PAGE]
        from resident_fast import prepare
        prepare(c,s)
        if args.guest_io_probe:
            report['guest_io_probe_start']=c.trace_command(s,'io_probe_start',event='trace_io_probe_started')
        if args.service_probe:
            for role,worker in c.resident_workers.items():
                directory=worker['directory']
                c.shell(role,f'/lab/resident-control service-probe {directory} 0 {c.resident_control_nonce} > {directory}/service-probe.log 2>&1 &\necho $! > {directory}/service-probe.pid','service-probe-start')
        def wait_payload():
            if args.resident_bench!='dirty': return
            import struct
            deadline=time.monotonic()+120
            with Path(config['cxl_backing']).open('rb',buffering=0) as stream:
                while True:
                    stream.seek(c.bench_offset+136)
                    ready=struct.unpack('<Q',stream.read(8))[0]
                    if ready==s.epoch:
                        stream.seek(c.bench_offset+40)
                        require(struct.unpack('<Q',stream.read(8))[0]==0,'Controlled payload checksum failed')
                        return
                    require(time.monotonic()<deadline,'Controlled writer did not complete before next pause')
                    time.sleep(.001)
        if args.count_faults:
            from ept_adopt import start_faults
            start_faults(c)
        for i in range(2*(args.warmup+args.iterations)):
            time.sleep(.02)  # active dwell is excluded from the C interval
            wait_payload()
            if args.preclean:
                from resident_fast import maintain
                for _ in range(args.clean_repeats):
                    report.setdefault('cleans',[]).append(maintain(c,s))
                time.sleep(1.0 if args.clean_wait=='1' else 0)

            if args.resident_bench=='race':
                import struct
                with Path(config['cxl_backing']).open('rb',buffering=0) as stream:
                    stream.seek(c.bench_offset+PAGE);before=struct.unpack('<Q',stream.read(8))[0]
                    deadline=time.monotonic()+30
                    while True:
                        values=[]
                        for page in range(1,17):
                            stream.seek(c.bench_offset+page*PAGE);values.append(struct.unpack('<Q',stream.read(8))[0])
                        if min(values)>before:break
                        require(time.monotonic()<deadline,'Race writer did not progress after last clean')
                        time.sleep(.001)
                report.setdefault('race_progress',[]).append(dict(before=before,after=values,host_ns=time.monotonic_ns()))
            destination='B' if s.role=='A' else 'A'
            if args.count_faults:
                from ept_adopt import faults
                before=faults(c,destination)
            row = c.direct_handoff(s,destination)
            if args.count_faults:
                wait_payload()
                after=faults(c,destination)
                row.update(payload_ept_violations=after['count']-before['count'],fault_before=before,fault_after=after)
            row['sample_index'] = i//2
            row['measured'] = i//2 >= args.warmup
            if args.audit_parked and i>=0:
                from resident_fast import audit_parked
                report.setdefault('parked_audits',[]).append(audit_parked(c,row['source']))
            ack = row['fast']['ack']
            require(ack['epoch'] == s.epoch and ack['verified'] == s.epoch
                    and row['fast']['guest_ack_ns'] > 0, 'Missing guest acknowledgement')
            if i == 1:
                report['resident_processes'] = {}
                for role,worker in c.resident_workers.items():
                    raw,name=c.shell(role,f"read p t < {worker['directory']}/fc.pid\nreadlink /proc/$p/exe\ncat /proc/$p/stat\nls /proc/$p/task",'resident-process-identity')
                    require('/lab/firecracker' in raw, 'Endpoint is not Firecracker')
                    report['resident_processes'][role] = {'file':name,'identity':raw}
            lab.save_json(out/'report.json',report)
        wait_payload()
        if args.guest_io_probe:
            result=c.trace_command(s,'io_probe_stop',event='trace_io_probe_result',timeout=300)
            _,name=c.frame(s,result,'IO_LATENCY','guest-io-latency')
            report['guest_io_probe']={'file':name,'receipt':result['receipt']}
        report['direction_summary_ms'] = {}
        for source in ('A','B'):
            rows = [r for r in c.transitions if r.get('measured') and r['source']==source]
            require(len(rows)==args.iterations, 'Wrong direction sample count')
            values=sorted(r['fast']['guest_ack_ns']/1e6 for r in rows)
            report['direction_summary_ms'][source+('→B' if source=='A' else '→A')] = {
                'n':len(values),'p50':statistics.median(values),'p95':values[math.ceil(.95*len(values))-1],
                'min':values[0],'max':values[-1],'mean':statistics.mean(values)}
        report['validation'] = {}
        for role,vm in c.vms.items():
            raw,name=c.shell(role,'cat /sys/module/kvm/parameters/dax_pool\ncat /proc/sys/kernel/tainted','final-kernel-identity')
            require(re.search(rf'vm_create_calls=1 vcpu_create_calls={args.vcpus}\b',raw), 'KVM graph recreated')
            report['validation'][role] = {'file':name,'raw':raw}
        if args.resident_bench in ('busy','race'):
            samples=[]
            for hop,row in enumerate(r for r in c.transitions if r.get('kind')=='direct'):
                for cpu,values in enumerate(row['fast']['ack']['cpus'][:args.vcpus]):
                    samples.append(dict(event='smp',round=hop,cpu=cpu,sequence=values[0],ack=values[1],
                         timer=values[2],ipi=values[3],ping=values[4],errors=0,clock_errors=0,tsc_errors=0))
            report['smp_validation'] = lab.validate_smp_handoff(samples,len(samples)//args.vcpus,args.vcpus)
        if args.resident_bench=='race':
            require(oracle is not None,'Race test requires oracle')
            for boundary in oracle.rows:
                if boundary['phase']!=2:continue
                data=(oracle.output/f"{boundary['sequence']}.bitmap").read_bytes()
                bitmap=int.from_bytes(data[:((boundary['pages']+63)//64)*8],'little')
                mask=((1<<16)-1)<<(64*MIB//PAGE+1)
                require(bitmap&mask==mask,'Continuously written fixed GPA pages absent at handoff')
            report['race_pages']={'gpa_start':64*MIB+PAGE,'pages':16,'handoff_lists_verified':True}
        if args.reuse_test:
            require(oracle is not None,'Reuse test requires oracle')
            from resident_fast import reuse_after,admin
            admin(c,s.role,'retire')
            oracle.close();report['dirty_oracle']=oracle.rows;oracle=None
            report['reuse']=reuse_after(c,s,args,out)
        report['result']='passed'
    except BaseException as error:
        report.update(result='failed',error=repr(error))
        raise
    finally:
        if oracle:
            try:
                oracle.close()
            except Exception as exc:
                report.update(result='failed', oracle_error=repr(exc))
            report['dirty_oracle'] = oracle.rows
        if report['result']=='failed':
            for role,worker in getattr(c,'resident_workers',{}).items():
                directory=worker['directory']
                try:c.shell(role,f'read p t < {directory}/fc.pid\nfor task in /proc/$p/task/*; do cat $task/comm $task/schedstat $task/syscall $task/stack || true; done\ndmesg | tail -n 80','failure-thread-stacks')
                except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
        for role in ('A','B'):
            if args.tail_profile:
                try:c.shell(role,'dmesg','dirty-kernel-times-'+role)
                except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
            try:c.shell(role,'cat /sys/module/kvm/parameters/dax_pool','final-cache-state-'+role)
            except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
        for role,worker in getattr(c,'resident_workers',{}).items():
            if args.service_probe:
                directory=worker['directory']
                try:
                    c.shell(role,f'kill $(cat {directory}/service-probe.pid)','service-probe-stop')
                    c.fetch_bytes(role,directory+'/service-probe.log','service-probe-'+role)
                except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
            for name in ('fc.log','vmm.log','stderr.log'):
                try:c.fetch_bytes(role,worker['directory']+'/'+name,'final-'+role+'-'+name)
                except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
        for role,server in getattr(c,'resident_servers',{}).items():
            try:c.fetch_bytes(role,server['directory']+'/log','controller-'+role+'-log')
            except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
        lab.save_json(out/'report.json',report)
        print('RESIDENT_BENCH_REPORT '+str(out/'report.json'),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resident-bench', choices=('idle','busy','dirty','race'))
    parser.add_argument('--mixed-io', action='store_true')
    parser.add_argument('--io-fixture')
    parser.add_argument('--dirty-lines',type=int,choices=(0,1,8,64),default=0)
    parser.add_argument('--dirty-mode',choices=('change','same','read'),default='change')
    parser.add_argument('--dirty-mib',type=int,choices=(0,16,64,256),default=0)
    parser.add_argument('--iterations',type=int,default=100)
    parser.add_argument('--warmup',type=int,default=10)
    parser.add_argument('--profile',action='store_true')
    parser.add_argument('--resume-profile',action='store_true')
    parser.add_argument('--mode', choices=('baseline','state-only','direct'), required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--memory-mib', type=int, default=2048)
    parser.add_argument('--vcpus', type=int, choices=(1,2,4,8), default=1)
    parser.add_argument("--parallel-build", action="store_true")
    parser.add_argument("--full-audit", action="store_true")
    parser.add_argument("--restore-timing", action="store_true")
    parser.add_argument("--kernel-trace", action="store_true")
    parser.add_argument("--inflight-handoff-index", type=int, help="Test two handoffs while this real command is alive")
    parser.add_argument("--resident-repromote-handoff", action="store_true")
    parser.add_argument("--direct-profile-roundtrips", type=int, default=0)
    parser.add_argument('--trajectory-policy', choices=('heavy', 'all'))
    parser.add_argument('--heavy-plan')
    parser.add_argument('--count-faults', action='store_true')
    parser.add_argument('--dirty-ledger', action='store_true')
    parser.add_argument('--noncoherent', action='store_true')
    parser.add_argument('--reuse-test', action='store_true')
    parser.add_argument('--legacy-observer', action='store_true', help='Diagnostic: old resident fixtures without DirectObserve')
    parser.add_argument('--audit-parked', action='store_true')
    parser.add_argument('--arena-limit-bytes',type=int,default=0)
    parser.add_argument('--fail-maintenance',choices=('writeback','epoch','record'))
    parser.add_argument('--tail-profile', action='store_true')
    parser.add_argument('--clean-helper',action='store_true')
    parser.add_argument('--guest-io-probe',action='store_true')
    parser.add_argument('--service-probe',action='store_true')
    parser.add_argument('--preclean', action='store_true')
    parser.add_argument('--clean-paused', action='store_true')
    parser.add_argument('--clean-wait', choices=('0','1','recorded'), default='0')
    parser.add_argument('--clean-repeats', type=int, default=1)
    parser.add_argument('--cache-skip-line',type=int,default=-1)
    parser.add_argument('--skip-invalidate-page',type=int,default=-1)
    parser.add_argument('--drop-consumed-once', action='store_true')
    parser.add_argument('--dirty-oracle', action='store_true')
    parser.add_argument('--page-hash')
    parser.add_argument('--prefault', choices=('all','mapped'))
    parser.add_argument('--prefault-activate', action='store_true')
    parser.add_argument('--prefault-probe', help='Static ABI probe uploaded only to these disposable L1s')
    args = parser.parse_args()
    if args.clean_helper and (not args.preclean or args.clean_paused):parser.error("Helper requires live preclean")
    if args.preclean and not args.noncoherent: parser.error("Preclean requires noncoherent mode")
    if not 1 <= args.clean_repeats <= 1000: parser.error("clean-repeats must be 1..1000")
    if args.clean_paused and not args.preclean: parser.error("clean-paused requires preclean")
    if args.noncoherent: args.dirty_ledger=True
    require(not args.prefault or (args.mode == 'state-only' and args.trajectory_policy), 'Prefault requires snapshot trajectory')
    require(not args.prefault_activate or args.prefault, 'Activation requires prefault')
    require(0 <= args.direct_profile_roundtrips <= 20 and
            (not args.direct_profile_roundtrips or args.mode == 'direct'), 'Invalid direct profile repetitions')
    require(args.inflight_handoff_index is None or (args.mode == 'direct' and not args.full_audit),
            'In-flight test requires direct runtime mode')
    require(not args.kernel_trace or args.mode == 'direct', 'Kernel tracing requires direct mode')
    if args.mixed_io:
        require(args.mode=='direct' and args.dirty_ledger and args.io_fixture and args.memory_mib==2048, 'Mixed I/O requires tracked direct and the fixture binary')
        return mixed_io(args)
    if args.resident_bench:
        require(args.mode=='direct' and args.memory_mib in (128,2048), 'Resident benchmark needs a bounded direct VM')
        return resident_measure(args)
    require(args.memory_mib == 2048, 'Use the bounded 2GiB workload layout')
    run, config = lab.current_run()
    require(config['cxl_mib'] in (4096, 8192, 16384) and bool(config.get('dax_handoff')) == (args.mode == 'direct'), 'Wrong fresh pair mode/capacity')
    out = run / ('swe-handoff-' + str(time.time_ns()))
    out.mkdir()
    c = HandoffController(config, out, args)
    s = c.sandboxes[0]
    oracle = None
    if args.dirty_oracle:
        from dirty_oracle import DirtyOracle
        oracle = DirtyOracle(config, out, c.metadata_base+s.vm*64*MIB, c.kvm_arena, args.page_hash)
    report = {'schema': 'swe-handoff-trace-v1', 'result': 'running', 'mode': args.mode, 'name': args.name,
        'arguments': dict(vars(args)), 'config': config, 'fixture': c.fixture, 'trace': c.trace,
        'commands': c.trace_commands, 'control_commands': c.commands, 'receipts': c.receipts,
        'transitions': c.transitions, 'memory_events': c.memory_events,
        'api_events': c.api_events, 'launches': c.launches,
        'resident_readiness':c.resident_readiness, 'resident_preparations':c.resident_preparations,
        'allocator_events': c.allocator.events, 'audits': c.audits,
        'layout': {'private_start': 2*MIB, 'private_end': 3072*MIB, 'kvm_arena': c.kvm_arena, 'metadata_base':c.metadata_base},
        'limitations': ['Coherent nested KVM; one run per trace/mode', ('Two extra handoffs inside a running command' if args.inflight_handoff_index is not None else 'Transitions between recorded commands'),
            'Same patched DRAM baseline, not pristine Firecracker', 'One VM per trial; full private RAM copy for tier changes']}
    sources = ('dirty_oracle.py', 'page_hash.c', 'experiment_sources.py', 'handoff_bench.py', 'swe-handoff-l2-init.sh', 'swe-handoff-guest-run.sh',
        'resident_fast.py', 'resident_control.c', 'resident_wire.h', 'resident_proxy.py', 'resident_bootstrap.c',
        'control_rpc.c', 'control_rpc.py', 'guest-init.sh', 'nfs_bench.py', 'lab.py')
    report['source_hashes'], report['source_reference'] = record_sources(HERE, sources)
    source = Path(config['assets'])/'traces'/f'{args.name}.traj.json'
    shutil.copy2(source,out/'source.traj.json')
    report['source_trace'] = {'file':'source.traj.json','sha256':lab.sha256(source)}
    def save():
        lab.save_json(out/'report.json',report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report['storage_setup'] = c.setup()
        if args.prefault_probe:
            probe = Path(args.prefault_probe).read_bytes()
            report['prefault_probes'] = []
            for role in ('A','B'):
                raw, file = c.write_bytes(role, '/tmp/prefault-probe', probe, 'prefault-probe',
                    after='chmod 755 /tmp/prefault-probe\n/tmp/prefault-probe')
                values = json_lines(raw,'PREFAULT_PROBE_JSON ')
                require(len(values) == 1 and values[0]['result'] == 'passed', 'Prefault ABI self-test failed')
                report['prefault_probes'].append({'role':role,'file':file,'result':values[0],
                    'binary_sha256':hashlib.sha256(probe).hexdigest()})
        if args.mode == 'direct':
            c.configure_direct()
        report['boot'] = c.boot_vm(s)
        report['initial_identity'] = c.command_vm(s,'identity')
        first, second = c.trace['handoff_before'], c.trace['command_count']-2
        report['boundaries'] = [first,second]
        require(args.inflight_handoff_index is None or first <= args.inflight_handoff_index < second,
                'In-flight command must run on remote CXL before demotion')
        started = time.monotonic_ns()
        if args.trajectory_policy:
            trajectory_replay(c, s, report, save)
        for command in ([] if args.trajectory_policy else c.trace['commands']):
            index = command['index']
            if args.mode != 'baseline' and index in (first,second):
                if index == first:
                    c.promote(s)
                    if args.mode == 'direct':
                        from resident_fast import prepare
                        prepare(c, s)
                    else:
                        c.prepare_snapshot_target(s, 'B')
                    for _ in range(args.direct_profile_roundtrips):
                        c.direct_handoff(s, 'B')
                        c.direct_handoff(s, 'A')
                        save()
                    (c.direct_handoff if s.direct else c.state_handoff)(s,'B')
                else:
                    if args.full_audit: c.audit_memory(s)
                    if args.mode == 'state-only': c.prepare_snapshot_target(s, 'A')
                    (c.direct_handoff if s.direct else c.state_handoff)(s,'A')
                    c.demote(s)
                save()
            timeout = int(c.trace['environment'].get('timeout',60))+30
            row = (c.inflight_command(s,index,timeout) if index == args.inflight_handoff_index else
                   c.trace_command(s,'run',index,event='trace_end',timeout=timeout))
            value = row['receipt']
            require(not value['timed_out'] and value['command_sha256'] == command['command_sha256'] and
                    value['rc'] == (command['expected_returncode'] if command['expected_returncode'] is not None else 0),
                    f'Trace command diverged at {index}: {value}')
            row.update(guest_duration_s=value['end_s']-value['start_s'])
            c.trace_commands.append(row)
            save()
        if args.mode != 'baseline' and not args.trajectory_policy:
            c.promote(s)
            if args.parallel_build:
                report['parallel_build'] = c.parallel_build(s)
            if args.resident_repromote_handoff:
                c.direct_handoff(s, 'B')
                c.direct_handoff(s, 'A')
            if args.full_audit: c.audit_memory(s)
            c.demote(s)
        if args.mode == 'baseline' and args.parallel_build:
            report['parallel_build'] = c.parallel_build(s)
        report['final_identity'] = c.command_vm(s,'identity')
        exported = c.trace_command(s,'export',event='trace_export',timeout=180)
        _, archive = c.frame(s,exported,'TRACE_EXPORT','guest-archive')
        report['guest_archive'], report['guest_archive_sha256'] = archive, exported['receipt']['sha256']
        report['guest_export'] = exported
        report['controller_replay_ns'] = time.monotonic_ns()-started
        report['guest_duration_s'] = sum(x['guest_duration_s'] for x in c.trace_commands)
        report['runtime_audits'] = False
        if args.kernel_trace:
            report['kernel_trace'] = c.kernel_traces
        report['allocator_final'] = c.allocator.summary()
        require(report['allocator_final']['allocated_bytes'] == 0, 'CXL grant leak')
        report['result'] = 'passed'
    except BaseException as exc:
        report.update(result='failed',error=repr(exc))
        raise
    finally:
        if oracle:
            try:
                oracle.close()
            except Exception as exc:
                report.update(result='failed', oracle_error=repr(exc))
            report['dirty_oracle'] = oracle.rows
        report['keepalive_cleanup'] = []
        for keeper in getattr(c, 'keepalives', []):
            role, directory = keeper['role'], keeper['directory']
            nfs.stop_fc(c.vms[role], directory)
            raw, name = c.shell(role, source_fence_script(directory), 'keepalive-final-fence')
            report['keepalive_cleanup'].append({**keeper, 'file': name, 'fenced': 'FENCED ' in raw})
        if args.tail_profile:
            for role in ('A','B'):
                c.shell(role,'dmesg','dirty-kernel-times-'+role)
        report['controller_cleanup'] = []
        for role, server in getattr(c, 'resident_servers', {}).items():
            nfs.stop_fc(c.vms[role], server['directory'])
            if args.restore_timing or args.tail_profile:
                c.fetch_bytes(role,server['directory']+'/log','final-controller-log')
            raw, name = c.shell(role,source_fence_script(server['directory']),'controller-final-fence')
            report['controller_cleanup'].append({**server, 'role':role, 'prepare_file':server['file'], 'file':name, 'fenced':'FENCED ' in raw})
        if args.tail_profile:
            for role in ('A','B'):
                try:c.shell(role,'dmesg','dirty-kernel-times-'+role)
                except Exception as error:report.setdefault('collection_errors',[]).append(str(error))
        report['cleanup'] = []
        for role, directory in reversed(c.active):
            try:
                nfs.stop_fc(c.vms[role],directory)
                raw, name = c.shell(role,source_fence_script(directory),'final-fence')
                c.fetch_bytes(role,directory+'/fc.log','final-guest-log')
                c.fetch_bytes(role,directory+'/vmm.log','final-vmm-log')
                if args.prefault:
                    c.shell(role, f'cat {directory}/stderr.log\ncat {directory}/prefault-report.json 2>/dev/null || true\ndmesg | tail -n 40',
                            'final-prefault-diagnostic')
                report['cleanup'].append({'role':role,'directory':directory,'file':name,'fenced':'FENCED ' in raw})
            except BaseException as exc:
                report['cleanup'].append({'role':role,'directory':directory,'error':repr(exc)})
        if not report['cleanup'] or not all(row.get('fenced') for row in report['cleanup']):
            report['result'] = 'failed'
        save()
        print('SWE_HANDOFF_REPORT '+str(out/'report.json'),flush=True)


if __name__ == '__main__':
    main()
