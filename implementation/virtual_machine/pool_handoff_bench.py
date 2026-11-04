#!/usr/bin/env python3
"""Guest-authorized pooling with in-place tier changes and RAM-preserving handoff.

Full-RAM snapshot creation is forbidden here. State-only and direct transitions
are separate operations; reports must never count one as evidence for the other.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import shutil
import time

import lab
from experiment_sources import record_sources
import nfs_bench as nfs
from home_storage_bench import setup, require, json_lines
from pool_bench import PoolController, HERE, MIB
from pool_control import PoolControl
from pool_handoff_control import FrameAllocator, promotion_layout, validate_export, offset_ranges, validate_handoff_handle
from guest_pool_protocol import PAGE, parse_registration
from trace_bench import source_fence_script
from pool_compare_bench import parse_maps
from pool_discard_bench import DiscardController


class HandoffController(PoolController):
    def __init__(self, config, out, args):
        super().__init__(config, out, args)
        self.args = args
        self.mode = args.mode
        self.fixture = json.loads((Path(config["assets"]) / "manifest.json").read_text())
        self.authority_events, self.memory_events, self.api_events = [], [], []
        self.launches = []
        self.import_negatives = []
        self.report_logs, self.guest_traces = [], []
        self.allocator = FrameAllocator(2 * MIB, 2048 * MIB)
        self.pool = PoolControl(self)
        self.ready_keys = set()
        self.registrations = {}
        self.owners = {}
        for s in self.sandboxes:
            s.direct = self.mode == "direct" and s.vm == 1
            s.manifest = None
            s.owner = None

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
        s.hints = s.directory + "/unused-read-history.json"
        env = {"PATH": "/bin:/sbin:/usr/bin:/usr/sbin:/lab", "FC_POOL_MEMORY": "1",
            "FC_POOL_VM_ID": str(s.vm), "FC_POOL_RUN_EPOCH": str(s.epoch),
            "FC_POOL_IMAGE_ID": self.image_id, "FC_POOL_IMAGE_PATH": "/disk/sandbox.raw",
            "FC_POOL_MEMORY_PLAN": s.plan, "FC_POOL_MEMORY_REPORT": s.report,
            "FC_POOL_MEMORY_METADATA_OFFSET": str(getattr(self, "metadata_base", 3072 * MIB) + s.vm * 64 * MIB),
            "FC_POOL_FULL_AUDIT": "1" if getattr(self.args, "full_audit", False) else "0",
            "FC_POOL_MEMORY_MANIFEST": s.manifest_path, "FC_POOL_MEMORY_DEVICE": "/dev/cxl_pool",
            "FC_DAX_FIXED_HVA": "0x400000000000", "FC_DAX_SNAPSHOT": "1",
            "FC_POOL_DISCARD": "1"}
        if getattr(self.args, "full_audit", False):
            # The discard oracle joins host mapping/ACK receipts with guest
            # trace ranges. Ordinary runtime deliberately omits these logs.
            env["FC_POOL_DISCARD_TRACE"] = "1"
        resident = getattr(self.args, "resident_direct", False)
        if resident:
            env.update(FC_DAX_RESIDENT="1", FC_DAX_RESIDENT_IMPORT=s.directory+"/direct-import.json",
                FC_DAX_RESIDENT_MANIFEST=s.directory+"/restore-manifest.json")
            if getattr(self.args, 'resident_native_control', False) or getattr(self.args, 'resident_memory_control', False):
                env['FC_RESIDENT_NATIVE'] = '1'
            if getattr(self.args, 'resident_memory_control', False):
                env['FC_RESIDENT_MEMORY'] = '1'
        if restore_manifest:
            local = s.directory + "/restore-manifest.json"
            if purpose != "resident-prepare":
                self.shell(s.role, f"mkdir -p {s.directory}\ncp {shlex.quote(restore_manifest)} {local}",
                           "local-memory-manifest", 120)
            env["FC_POOL_MEMORY_RESTORE"] = local
            env["FC_POOL_MEMORY_LEASE"] = str(self.allocator.grants[s.owner].generation)
        if s.direct:
            env.update(FC_DAX_DIRECT="1", FC_DAX_DIRECT_IO="1",
                FC_DAX_DIRECT_EXPORT=s.directory + "/direct-export.json")
            if getattr(self.args, 'resume_nokick', False):
                env['FC_DAX_NO_RESUME_KICK'] = '1'
        if getattr(self.args, "restore_timing", False):
            env["FC_RESTORE_TIMING"] = "1"
        if direct_import:
            env["FC_DAX_DIRECT_IMPORT"] = direct_import
            env["FC_DAX_RAM"] = "/dev/dax0.0"
        self.active.append((s.role, s.directory))
        # Upload a launch script: BusyBox interactive ash truncates very long
        # physical serial lines before execution. Keep console commands short.
        native = purpose == 'resident-prepare' and getattr(self.args, 'resident_ready_control', False)
        skip_prepare = native and getattr(self.args, 'resident_skip_disk_prepare', False)
        if not native:
            self.shell(s.role, f"mkdir -p {s.directory}", "launch-directory")
        script = "set -e\n" + "\n".join("export " + shlex.quote(k + "=" + v)
            for k, v in env.items()) + "\n"
        script += f"cat > {s.directory}/environment.json <<'RESIDENT_ENV_JSON'\n" + json.dumps(env) + "\nRESIDENT_ENV_JSON\n"
        script += (f"exec /lab/firecracker --no-seccomp --api-sock {s.directory}/api.sock "
                   f"--log-path {s.directory}/vmm.log\n")
        script_path = self.root + '/resident-launch-' + s.role + '.sh' if native else s.directory + '/launch.sh'
        if native:
            start_command = f'/lab/resident-bootstrap {s.directory} {script_path}' + (' --no-prepare' if skip_prepare else '')
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
                ("directory", "plan", "report", "manifest_path", "hints")}
            self.resident_workers[s.role]["imported"] = purpose != "resident-prepare"
            if native:
                from home_storage_bench import json_lines
                boots = json_lines(raw, 'RESIDENT_BOOT_JSON ')
                answers = json_lines(raw, 'LAB_API_JSON ')
                require(len(boots) == 1, 'Native process readiness missing')
                if skip_prepare:
                    require(not answers and boots[0].get('disk_prepared') is False, 'Disk preparation was not skipped')
                    self.resident_workers[s.role].update(native_boot=boots[0], native_boot_file=executable_file)
                    return
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
        for role, node, mode in (("A", 1, 1), ("B", 2, 2)):
            self.shell(role, "echo 2147483647 > /sys/module/kvm/parameters/dax_owner_pid\n"
                "echo 1 > /sys/module/kvm_intel/parameters/dump_invalid_vmcs\n"
                "echo 1 > /sys/module/kvm/parameters/dax_nested_root_sync\n"
                "resource=$(cat /sys/bus/dax/devices/dax0.0/resource)\n"
                "echo 3 > /sys/module/kvm/parameters/dax_handoff_mode\n"
                "echo 1 > /sys/module/kvm/parameters/dax_scattered\n"
                "echo $resource > /sys/module/kvm/parameters/dax_data_base\n"
                f"echo {2304*MIB} > /sys/module/kvm/parameters/dax_data_bytes\n"
                f"echo {node} > /sys/module/kvm/parameters/dax_node\n"
                f"echo {mode} > /sys/module/kvm/parameters/dax_shared\n"
                f"printf '%x,%x\\n' \"$((resource + {2304*MIB}))\" {128*MIB} > /sys/module/kvm/parameters/dax_pool\n"
                "cat /sys/module/kvm/parameters/dax_pool", "direct-kernel-setup")

    def boot_vm(self, s, warm=True):
        self.start_vm(s)
        components = {"/machine-config": {"vcpu_count": self.vcpus, "mem_size_mib": self.memory_mib},
            "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux",
                "initrd_path": "/lab/pool-handoff-l2-initramfs.cpio",
                "boot_args": f"console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init "
                    f"pool.vm={s.vm} pool.image_id={self.image_id} "
                    "page_reporting.page_reporting_order=0 trace_event=pool_discard:pool_free_report "
                    "trace_buf_size=8M trace_clock=mono"},
            "/drives/data": {"drive_id": "data", "path_on_host": "/disk/sandbox.raw", "is_root_device": False,
                "is_read_only": True, "cache_type": "Writeback", "io_engine": self.io_engine},
            "/balloon": {"amount_mib": 0, "deflate_on_oom": False, "stats_polling_interval_s": 0,
                "free_page_hinting": False, "free_page_reporting": True}}
        for path, body in components.items():
            self.api(s.role, s.directory, path, "PUT", body)
        self.api(s.role, s.directory, "/actions", "PUT", {"action_type": "InstanceStart"})
        ready = self.event(s, "pool_ready")
        require(ready["receipt"]["vm"] == s.vm, "Wrong guest boot")
        if warm:
            self.command_vm(s, "warm", require_cache=True)
        return ready

    def registration(self, s):
        identity = self.command_vm(s, "identity")
        token = secrets.randbits(63) or 1
        command = self.command_vm(s, "prepare", s.epoch, token)
        label = command["receipt"]["label"]
        selected = s.directory + "/registration.jsonl"
        self.shell(s.role, f"grep {shlex.quote(chr(34)+'label'+chr(34)+':'+chr(34)+label+chr(34))} "
                   f"{s.directory}/fc.log > {selected}", "registration-select")
        raw = self.fetch_bytes(s.role, selected, "guest-registration")
        raw_file = self.receipts[-1]["file"]
        value = parse_registration(raw.decode(), label=label, vm=s.vm,
            boot_id=identity["receipt"]["boot_id"], cookie=identity["receipt"]["cookie"], epoch=s.epoch,
            token=token, image_id=self.image_id, file_identity=self.fixture["handoff"]["file_identity"],
            extents=self.fixture["handoff"]["logical_extents"], memory_bytes=self.memory_mib * MIB)
        require(command["receipt"]["block_read_ios"] == 0, "Registration faulted file pages into cache")
        event = {"vm": s.vm, "epoch": s.epoch, "role": s.role, "token": token, "file": raw_file,
                 "command": command, "identity": identity, "summary": value["summary"]}
        self.authority_events.append(event)
        self.registrations[s.vm] = value
        return value, event

    def release_registration(self, s, token, abort=False):
        result = self.command_vm(s, "abort" if abort else "release", token)
        self.registrations.pop(s.vm, None)
        return result

    def memory_operation(self, s, operation, **fields):
        plan = {"operation": operation, "vm_id": str(s.vm), "run_epoch": str(s.epoch),
                "pool_path": "/dev/dax0.0", "canonical_start": self.pool_offset,
                "canonical_bytes": self.image_bytes, **fields}
        self.write_json(s.role, s.plan, plan, "memory-plan")
        plan_file = self.raw("memory-plan-json", json.dumps(plan, separators=(",", ":")))
        before = len(self.api_events)
        self.api(s.role, s.directory, "/actions", "PUT", {"action_type": "PoolCache"})
        result = self.fetch_json(s.role, s.report, "memory-operation")
        event = {"vm": s.vm, "epoch": s.epoch, "role": s.role, "operation": operation,
                 "report": {k: v for k, v in result.items() if k != "pages"},
                 "file": self.receipts[-1]["file"], "plan_file": plan_file, "apis": self.api_events[before:]}
        self.memory_events.append(event)
        return result, event

    def export(self, s):
        self.memory_operation(s, "export")
        manifest = self.fetch_json(s.role, s.manifest_path, "memory-manifest")
        accounting = validate_export(self.allocator, s.owner, manifest, self.memory_mib * MIB,
                                     self.pool_offset, self.image_bytes)
        s.manifest = manifest
        return {"manifest": manifest, "file": self.receipts[-1]["file"], "accounting": accounting}

    @staticmethod
    def export_evidence(value):
        return {k: v for k, v in value.items() if k != "manifest"}

    def promote(self, s):
        require(s.tier == "dram", "Promotion requires current DRAM VM")
        registration, authority = self.registration(s)
        require(registration["registered_bytes"] >= self.minimum_cache_bytes, "Too little authoritative file cache")
        keys = {p["image_offset"] for p in registration["pages"]}
        discovered, discovery = set(), []
        for vm, owner in sorted(self.owners.items()):
            if vm == s.vm:
                continue
            hits, receipt = self.pool._bloom(s, "probe", keys, vm)
            exact = hits & owner["keys"] & self.ready_keys
            discovered |= exact
            discovery.append({"owner_vm": vm, "bloom": receipt, "exact_keys": len(exact)})
        eligible = [dict(p, pool_offset=self.pool_offset + p["image_offset"])
                    for p in registration["pages"] if p["image_offset"] in discovered
                    or p["image_offset"] not in self.ready_keys]
        self.pool._bloom(s, "build", keys, s.vm)
        self.pause(s)
        owner = f"vm{s.vm}:allocation{authority['token']}"
        layout = promotion_layout(self.allocator, owner, self.memory_mib * MIB, eligible,
                                  self.args.credit_mib * MIB // PAGE)
        result, operation = self.memory_operation(s, "promote", registration=registration["registration"],
            mappings=layout["mappings"], credits=layout["credits"],
            allocation_generation=layout["allocation_generation"])
        require(result["copied_private_bytes"] == layout["private_bytes"]
                and result["shared_bytes"] == layout["shared_bytes"]
                and result["file_page_compare_bytes"] == 0 and result["ram_snapshot_bytes"] == 0,
                "Promotion copied shared payload or used data comparison/snapshot")
        s.owner, s.tier = owner, "cxl"
        exported = self.export(s)
        self.ready_keys.update(p["image_offset"] for p in eligible)
        self.owners[s.vm] = {"keys": keys, "epoch": s.epoch, "tier": "cxl"}
        self.resume(s)
        released = self.release_registration(s, authority["token"])
        checked = self.command_vm(s, "check", require_cache=True)
        require(checked["receipt"]["block_read_ios"] == 0, "Promotion lost warm file cache")
        row = {"kind": "promotion", "vm": s.vm, "epoch": s.epoch, "source_tier": "dram",
            "destination_tier": "cxl", "source": s.role, "destination": s.role, "authority": authority,
            "discovery": discovery, "operation": operation, "export": self.export_evidence(exported), "release": released,
            "after": checked, "allocator": self.allocator.summary(), "result": "passed"}
        self.transitions.append(row)
        return row

    def demote(self, s):
        require(s.tier == "cxl", "Demotion requires CXL VM")
        before = self.command_vm(s, "identity", require_cache=True)
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
        freed = self.allocator.release(s.owner, fenced=True, detached=True)
        s.owner, s.tier, s.manifest = None, "dram", None
        self.owners[s.vm] = {"keys": self.owners[s.vm]["keys"], "epoch": s.epoch, "tier": "dram"}
        self.resume(s)
        after = self.command_vm(s, "check", require_cache=True)
        self.same_identity(before, after)
        row = {"kind": "demotion", "vm": s.vm, "epoch": s.epoch, "source_tier": "cxl",
            "destination_tier": "dram", "source": s.role, "destination": s.role, "before": before,
            "after": after, "previous": self.export_evidence(previous), "operation": operation,
            "returned_ranges": offset_ranges(freed),
            "dram_maps_file": maps_file, "dram_maps": mappings,
            "allocator": self.allocator.summary(), "result": "passed"}
        self.transitions.append(row)
        return row

    def state_handoff(self, s, destination):
        require(self.mode == "state-only" and s.tier == "cxl" and destination != s.role,
                "State-only handoff requires CXL and another host")
        before = self.command_vm(s, "identity", require_cache=True)
        source, old_directory, old_epoch = s.role, s.directory, s.epoch
        self.pause(s)
        old = self.export(s)
        state_path = old_directory + "/state-only.bin"
        create = self.api(source, old_directory, "/snapshot/create", "PUT",
            {"snapshot_type": "Full", "snapshot_path": state_path, "mem_file_path": "/dev/dax0.0"})
        # create_snapshot binds the exact CPU/device bytes to the current backing
        # manifest. Fetch AFTER creation; an earlier export lacks that binding.
        state = self.fetch_bytes(source, state_path, "state-only")
        state_receipt = self.receipts[-1]["file"]
        require(0 < len(state) < self.memory_mib * MIB // 16, "State file unexpectedly contains RAM")
        manifest = self.fetch_bytes(source, s.manifest_path, "state-bound-manifest")
        manifest_receipt = self.receipts[-1]["file"]
        parsed = json.loads(manifest)
        require(parsed.get("state_sha256") == hashlib.sha256(state).hexdigest(), "State/manifest binding absent")
        nfs.stop_fc(self.vms[source], old_directory)
        raw, fence = self.shell(source, source_fence_script(old_directory), "source-fence")
        require("FENCED " in raw, "Source remains runnable")
        s.epoch += 1
        s.role = destination
        target_directory = f"{self.root}/vm{s.vm}-epoch{s.epoch}"
        self.shell(destination, f"mkdir -p {target_directory}", "destination-directory")
        target_state, target_manifest = target_directory + "/state-only.bin", target_directory + "/incoming-manifest.json"
        self.write_bytes(destination, target_state, state, "state-only-transfer")
        self.write_bytes(destination, target_manifest, manifest, "placement-transfer")
        load_request = {"snapshot_path": target_state,
            "mem_backend": {"backend_type": "File", "backend_path": "/dev/dax0.0"}, "resume_vm": False}
        if not self.import_negatives:
            # A failed SnapshotLoad exits the preboot API process. Each negative
            # gets its own fenced wrapper before the real destination is built.
            for case, field, value in (("stale-allocation-lease", "allocation_generation", parsed["allocation_generation"] + 1),
                                       ("stale-placement-epoch", "run_epoch", str(s.epoch)),
                                       ("corrupted-state-binding", None, None)):
                bad = dict(parsed)
                if field:
                    bad[field] = value
                negative_manifest = target_directory + "/negative-manifest.json"
                self.write_json(destination, negative_manifest, bad, "negative-restore-manifest")
                self.start_vm(s, restore_manifest=negative_manifest, purpose="negative-" + case)
                request = dict(load_request)
                if field is None:
                    damaged = bytes([state[0] ^ 1]) + state[1:]
                    damaged_path = target_directory + "/damaged-state.bin"
                    self.write_bytes(destination, damaged_path, damaged, "negative-state-upload")
                    request["snapshot_path"] = damaged_path
                failed = self.rejected_api(s, "/snapshot/load", request, case)
                failed["manifest_file"] = self.raw("negative-restore-manifest", json.dumps(bad, separators=(",", ":")))
                if field is None:
                    failed["damaged_sha256"] = hashlib.sha256(damaged).hexdigest()
                    failed["original_sha256"] = hashlib.sha256(state).hexdigest()
                nfs.stop_fc(self.vms[destination], s.directory)
                raw, rejected_fence = self.shell(destination, source_fence_script(s.directory), "negative-target-fence")
                require("FENCED " in raw, "Rejected destination wrapper not fenced")
                failed["fence_file"] = rejected_fence
                failed["directory"] = s.directory
                self.import_negatives.append(failed)
        self.start_vm(s, restore_manifest=target_manifest)
        load = self.api(destination, s.directory, "/snapshot/load", "PUT", load_request)
        restored = self.export(s)
        validate_handoff_handle(parsed, restored["manifest"])
        self.resume(s)
        after = self.command_vm(s, "check", require_cache=True)
        self.same_identity(before, after)
        require(after["receipt"]["block_read_ios"] == 0, "Handoff lost warm guest cache")
        row = {"kind": "state-only", "vm": s.vm, "source": source, "destination": destination,
            "source_epoch": old_epoch, "destination_epoch": s.epoch, "source_tier": "cxl", "destination_tier": "cxl",
            "before": before, "after": after, "source_fence_file": fence, "create_api": create, "load_api": load,
            "state_file": state_receipt, "state_bytes": len(state), "state_sha256": hashlib.sha256(state).hexdigest(),
            "manifest_file": manifest_receipt, "manifest_bytes": len(manifest),
            "ram_snapshot_bytes": 0, "ram_transfer_bytes": 0, "source_placement": self.export_evidence(old),
            "destination_placement": self.export_evidence(restored), "result": "passed"}
        self.transitions.append(row)
        return row

    def resident_resources(self, role, directory, epoch, phase):
        raw, name = self.shell(role, f"read p t < {directory}/fc.pid\n"
            "printf 'RESOURCE_PROCESS %s %s\\n' $p $t\n"
            "for f in /proc/$p/fd/*; do\n"
            "printf 'RESOURCE_FD %s %s\\n' ${f##*/} \"$(readlink $f)\"\ndone\n"
            "for f in /proc/$p/task/*; do\n"
            "printf 'RESOURCE_TASK %s %s\\n' ${f##*/} \"$(cat $f/comm)\"\ndone", "resident-resources")
        process = re.findall(r'^RESOURCE_PROCESS (\d+) (\d+)\s*$', raw, re.M)
        fds = dict(re.findall(r'^RESOURCE_FD (\d+) (.+)$',raw,re.M))
        tasks = dict(re.findall(r'^RESOURCE_TASK (\d+) (.+)$',raw,re.M))
        require(len(process)==1 and fds and tasks, "resident resource proof missing")
        row = {"role":role,"directory":directory,"epoch":epoch,"phase":phase,"file":name,
            "process":list(process[0]),"fds":{k:v.strip() for k,v in fds.items()},
            "tasks":{k:v.strip() for k,v in tasks.items()}}
        self.resident_resource_events.append(row)
        return row

    def prepare_resident_target(self, s, destination):
        import copy
        if getattr(self.args, 'resident_ready_control', False) and destination in getattr(self, 'resident_readiness', {}):
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

    def resident_fence(self, role, directory, generation):
        raw, name = self.shell(role, f"read p t < {directory}/fc.pid\nkill -0 $p\n"
            f"grep '400000000000-' /proc/$p/maps\n"
            f"grep 'DIRECT_RESIDENT_JSON ' {directory}/vmm.log | tail -n 1\n"
            "printf 'PARKED %s %s\\n' $p $t", "resident-fence")
        require(re.search(r'^400000000000-[0-9a-f]+ ---p ',raw,re.M), "parked source still maps RAM")
        rows = [json.loads(x) for x in re.findall(r'DIRECT_RESIDENT_JSON (\{.*\})', raw)]
        require(len(rows)==1 and rows[0]['event']=='park' and rows[0]['generation']==generation
            and rows[0]['ram_access_revoked'] and rows[0]['io_drained'] and rows[0]['devices_unsubscribed'], "park evidence missing")
        for path, method, body in (("/vm","PATCH",{"state":"Resumed"}),
                                   ("/actions","PUT",{"action_type":"PoolCache"})):
            answer = nfs.shell(self.vms[role], f"/lab/shared-probe api {directory}/api.sock {method} {path} " + shlex.quote(json.dumps(body)) + " 400")
            evidence = self.raw("resident-negative-api", answer)
            values = json_lines(answer, "LAB_API_JSON ")
            require(len(values)==1 and values[0]['status']==400, "parked source accepted mutation")
            self.import_negatives.append({"kind":"parked-operation", "role":role, "file":evidence,
                "request":body,"response":values[0],"generation":generation})
        self.api(role, directory, "/actions", "PUT", {"action_type":"DirectPrepare"})
        probe_raw, probe_file = self.shell(role, f"grep 'DIRECT_RESIDENT_PROBE_JSON ' {directory}/vmm.log | tail -n 1", "resident-probe")
        probes = [json.loads(x) for x in re.findall(r'DIRECT_RESIDENT_PROBE_JSON (\{.*\})',probe_raw)]
        require(len(probes)==1 and probes[0]["generation"]==generation and probes[0]["memory_errno"]==14
            and probes[0]["kvm_errno"] != 0, "parked memory/KVM access not rejected")
        self.resident_probes.append({"role":role,"directory":directory,"file":probe_file,"receipt":probes[0]})
        return name

    def direct_handoff(self, s, destination):
        if getattr(self.args, "resident_fast_control", False):
            from resident_fast import handoff
            return handoff(self,s,destination)
        require(self.mode == "direct" and s.direct and s.tier == "cxl" and destination != s.role,
                "Direct handoff requires selected CXL VM and another host")
        resident = getattr(self.args, "resident_direct", False)
        if resident and not self.resident_resource_events:
            self.resident_resources(s.role, s.directory, s.epoch, "active")
        preparation = self.prepare_resident_target(s, destination) if resident else None
        before = self.command_vm(s, "identity", require_cache=True)
        source, old_directory, old_epoch = s.role, s.directory, s.epoch
        critical_start = time.monotonic_ns()
        api_begin = len(self.api_events)
        self.pause(s)
        self.export(s)
        api = self.api(source, old_directory, "/actions", "PUT", {"action_type": "DirectExport"})
        metadata = self.fetch_bytes(source, old_directory + "/direct-export.json", "direct-device-state")
        metadata_file = self.receipts[-1]["file"]
        state = json.loads(metadata)
        require(state["schema"] == "direct-device-handoff-v1" and set(state) ==
                {"schema", "token", "machine_config", "boot_source", "memory", "memory_manifest_sha256", "devices"},
                "Direct export contains unsupported or CPU snapshot state")
        token = state["token"]
        require(token["vm_creates"] == token["vcpu_creates"] == 1, "Direct export created extra KVM objects")
        previous = getattr(s, "direct_token", None)
        if previous:
            for key in ("vm_offset", "vcpu_offset", "vmcs_offset", "run_offset", "ept_root_offset", "ram_hva", "ram_bytes"):
                require(token[key] == previous[key], "Direct handoff replaced retained " + key)
            require(token["generation"] > previous["generation"], "Direct generation did not advance")
        manifest = self.fetch_bytes(source, s.manifest_path, "direct-placement")
        manifest_file = self.receipts[-1]["file"]
        require(hashlib.sha256(manifest).hexdigest() == state["memory_manifest_sha256"],
                "Direct state/placement binding absent")
        parsed = json.loads(manifest)
        source_accounting = validate_export(self.allocator, s.owner, parsed, self.memory_mib * MIB,
                                             self.pool_offset, self.image_bytes)
        if resident:
            fence = self.resident_fence(source, old_directory, token["generation"])
        else:
            nfs.stop_fc(self.vms[source], old_directory)
            raw, fence = self.shell(source, source_fence_script(old_directory), "source-fence")
            require("FENCED " in raw, "Detached source process not fenced")
        self.shell(source, "echo 2147483647 > /sys/module/kvm/parameters/dax_owner_pid", "retire-direct-owner")
        s.epoch += 1
        s.role = destination
        if resident:
            worker = self.resident_workers[destination]
            for key in ("directory", "plan", "report", "manifest_path", "hints"): setattr(s,key,worker[key])
            target = s.directory
        else:
            target = f"{self.root}/vm{s.vm}-epoch{s.epoch}"
            self.shell(destination, f"mkdir -p {target}", "destination-directory")
        target_state = target + "/direct-import.json"
        target_manifest = target + ("/restore-manifest.json" if resident else "/incoming-manifest.json")
        if resident and worker["imported"] and not worker.get("negative_checked"):
            for case in ("stale-generation", "foreign-graph", "wrong-manifest-digest"):
                invalid = json.loads(metadata)
                if case == "stale-generation": invalid["token"]["generation"] -= 1
                elif case == "foreign-graph": invalid["token"]["vm_offset"] += 4096
                else: invalid["memory_manifest_sha256"] = "0"*64
                bad = json.dumps(invalid,separators=(",",":")).encode()
                self.write_bytes(destination,target_state,bad,"resident-invalid-transfer")
                candidate = self.raw("resident-invalid-state",bad.decode())
                self.write_bytes(destination,target_manifest,manifest,"resident-invalid-manifest-transfer")
                body = {"action_type":"DirectImport"}
                answer = nfs.shell(self.vms[destination], f"/lab/shared-probe api {target}/api.sock PUT /actions " + shlex.quote(json.dumps(body)) + " 400")
                evidence = self.raw("resident-negative-api", answer)
                values = json_lines(answer,"LAB_API_JSON ")
                require(len(values)==1 and values[0]["status"]==400,"resident invalid import accepted")
                self.import_negatives.append({"kind":case,"role":destination,"candidate_file":candidate,
                    "valid_state_file":metadata_file,"file":evidence,"request":body,"response":values[0]})
            worker["negative_checked"] = True
        self.write_bytes(destination, target_state, metadata, "direct-device-transfer")
        self.write_bytes(destination, target_manifest, manifest, "direct-placement-transfer")
        if not resident:
            self.start_vm(s, restore_manifest=target_manifest, direct_import=target_state)
        else:
            self.shell(destination, f"read p t < {target}/fc.pid\necho $p > /sys/module/kvm/parameters/dax_owner_pid", "select-resident-owner")
        action = "DirectImport" if resident and worker["imported"] else "InstanceStart"
        attach = self.api(destination, s.directory, "/actions", "PUT", {"action_type": action})
        if resident: worker["imported"] = True
        restored = self.export(s)
        validate_handoff_handle(parsed, restored["manifest"])
        log = self.fetch_bytes(destination, s.directory + "/vmm.log", "resident-full-log" if resident else "direct-import-log")
        if resident:
            selected = [line for line in log.decode().splitlines() if "DIRECT_HANDOFF_JSON " in line
                and json.loads(line.split("DIRECT_HANDOFF_JSON ",1)[1]).get("event") == "import"
                and json.loads(line.split("DIRECT_HANDOFF_JSON ",1)[1])["token"]["generation"] == token["generation"]]
            require(len(selected)==1, "resident import generation evidence")
            log = (selected[0]+"\n").encode()
            self.raw("direct-import-log", log.decode())
        import_rows = [json.loads(line.split("DIRECT_HANDOFF_JSON ", 1)[1]) for line in log.decode().splitlines()
                       if "DIRECT_HANDOFF_JSON " in line]
        imports = [r for r in import_rows if r["event"] == "import"]
        require(len(imports) == 1 and imports[0]["snapshot_bytes"] == imports[0]["ram_transfer_bytes"] == 0,
                "Direct target used snapshot or RAM transfer")
        for key in ("vm_offset", "vcpu_offset", "vmcs_offset", "run_offset", "ept_root_offset", "ram_hva",
                    "ram_bytes", "generation", "vm_creates", "vcpu_creates"):
            require(imports[0]["token"][key] == token[key], "Direct target changed retained " + key)
        require(imports[0]["token"]["flags"] == 1 and imports[0]["token"]["node"] == (1 if destination == "A" else 2),
                "Direct target has wrong ownership flags/node")
        s.direct_token = token
        kernel_profile = None
        if getattr(self, "kernel_profile_offset", None) is not None:
            # Read existing bounded kernel diagnostics after the API interval.
            # This never touches guest RAM or participates in admission.
            offsets = " ".join(str(self.kernel_profile_offset + i * 4) for i in range(32))
            raw, profile_file = self.shell(destination, f"for offset in {offsets}; do\n"
                f"/lab/shared-probe dax-word {destination} /dev/dax0.0 $offset\ndone", "kernel-handoff-profile")
            words = [int(value, 16) for value in re.findall(r"^LAB_DAX_WORD=0x([0-9a-f]{8})\s*$", raw, re.M)]
            require(len(words) == 32, "Incomplete kernel timing record")
            values = [words[i] | words[i+1] << 32 for i in range(0, 32, 2)]
            kernel_profile = {"file": profile_file, "detach_ns": values[:8], "attach_ns": values[8:]}
        self.resume(s)
        after = self.command_vm(s, "check", require_cache=True)
        critical_end = time.monotonic_ns()
        self.same_identity(before, after)
        require(after["receipt"]["block_read_ios"] == 0, "Direct handoff lost warm guest cache")
        row = {"kind": "direct", "vm": s.vm, "source": source, "destination": destination,
            "source_epoch": old_epoch, "destination_epoch": s.epoch, "source_tier": "cxl", "destination_tier": "cxl",
            "before": before, "after": after, "source_fence_file": fence, "export_api": api, "attach_api": attach,
            "device_state_file": metadata_file, "device_state_bytes": len(metadata), "manifest_file": manifest_file,
            "manifest_bytes": len(manifest), "token": token, "import_event": imports[0],
            "ram_snapshot_bytes": 0, "ram_transfer_bytes": 0, "cpu_snapshot_bytes": 0,
            "source_accounting": source_accounting, "destination_placement": self.export_evidence(restored), "result": "passed"}
        if resident:
            row.update(resident=True, preparation=preparation, source_directory=old_directory,
                destination_directory=s.directory, controller_pause_to_ack_ns=critical_end-critical_start,
                critical_apis=self.api_events[api_begin:])
            # Source remains parked while the destination is executing.
            self.resident_fence(source, old_directory, token["generation"])
            self.resident_resources(destination,s.directory,s.epoch,"active")
        if kernel_profile:
            row["kernel_profile"] = kernel_profile
        self.transitions.append(row)
        return row

    def grow_credits(self, s, mib):
        self.pause(s)
        frames = self.allocator.allocate(s.owner, mib * MIB // PAGE)
        _, event = self.memory_operation(s, "grow", credits=frames)
        after = self.export(s)
        self.resume(s)
        return {"operation": event, "export": self.export_evidence(after)}

    def remote_cold_refill(self, s):
        require(s.role == "B", "Remote refill requires B's host NFS path")
        self.command_vm(s, "evict")
        before, before_file = self.shell("B", "sync\necho 3 > /proc/sys/vm/drop_caches\ncat /proc/net/rpc/nfs", "nfs-before")
        rows = [line.split() for line in before.splitlines() if line.startswith("proc3 ")]
        require(len(rows) == 1, "NFS counters absent")
        count = int(rows[0][8])
        cold = self.command_vm(s, "warm", require_cache=True)
        after, after_file = self.shell("B", "cat /proc/net/rpc/nfs", "nfs-after")
        rows = [line.split() for line in after.splitlines() if line.startswith("proc3 ")]
        require(len(rows) == 1 and int(rows[0][8]) > count and cold["receipt"]["block_read_ios"] > 0,
                "Cold file read did not traverse host NFS and virtio-blk")
        return {"command": cold, "before_file": before_file, "after_file": after_file,
                "nfs_read_delta": int(rows[0][8]) - count}

    # Reuse only trace/receipt parsers; old snapshot-based transitions are never
    # inherited by this controller.
    report_log = DiscardController.report_log
    trace = DiscardController.trace
    guest_ack_pages = DiscardController.guest_ack_pages
    wait_reported = DiscardController.wait_reported

    def negative_authority(self, s):
        registration, authority = self.registration(s)
        eligible = [dict(p, pool_offset=self.pool_offset + p["image_offset"])
                    for p in registration["pages"]]
        owner = f"negative:{s.vm}:{authority['token']}"
        layout = promotion_layout(self.allocator, owner, self.memory_mib * MIB, eligible,
                                  self.args.credit_mib * MIB // PAGE)
        original = {"operation": "promote", "vm_id": str(s.vm), "run_epoch": str(s.epoch),
            "pool_path": "/dev/dax0.0", "canonical_start": self.pool_offset,
            "canonical_bytes": self.image_bytes, "registration": registration["registration"],
            "mappings": layout["mappings"], "credits": layout["credits"],
            "allocation_generation": layout["allocation_generation"]}
        negatives = []
        self.pause(s)
        mutations = [
            ("wrong-session-cookie", lambda p: p["registration"].update(
                session_cookie=p["registration"]["session_cookie"] ^ 1)),
            ("stale-vm-epoch", lambda p: p["registration"].update(vm_epoch=s.epoch + 1)),
            ("changed-record-digest", lambda p: p["registration"]["records"][0].update(
                disk_offset=p["registration"]["records"][0]["disk_offset"] + PAGE)),
            ("wrong-canonical-offset", lambda p: next(m for m in p["mappings"] if m["shared"]).update(
                offset=self.pool_offset)),
        ]
        for case, mutate in mutations:
            plan = copy.deepcopy(original)
            mutate(plan)
            self.write_json(s.role, s.plan, plan, "negative-memory-plan")
            failure = self.rejected_api(s, "/actions", {"action_type": "PoolCache"}, case)
            failure["plan_file"] = self.raw("negative-plan", json.dumps(plan, separators=(",", ":")))
            negatives.append(failure)
        self.resume(s)
        closed = self.command_vm(s, "close_abort", authority["token"])
        self.registrations.pop(s.vm, None)
        self.pause(s)
        self.write_json(s.role, s.plan, original, "negative-released-plan")
        failure = self.rejected_api(s, "/actions", {"action_type": "PoolCache"}, "released-status")
        failure["plan_file"] = self.raw("negative-plan", json.dumps(original, separators=(",", ":")))
        negatives.append(failure)
        self.resume(s)
        checked = self.command_vm(s, "check", require_cache=True)
        # No plan was installed. All API responses rejected during preflight;
        # the unchanged guest remained runnable on its original anonymous RAM.
        returned = self.allocator.release(owner, fenced=True, detached=True)
        return {"authority": authority, "cases": negatives, "close": closed, "after": checked,
                "uninstalled_grant_returned_pages": len(returned)}

    def bloom_negatives(self, reader, owner):
        keys = {offset + i * PAGE for _, offset, size in self.fixture["handoff"]["logical_extents"]
                for i in range(size // PAGE)}
        self.pool._bloom(owner, "zero", [], owner.vm)
        absent, zero = self.pool._bloom(reader, "probe", keys, owner.vm)
        require(not absent, "Zero Bloom unexpectedly discovered existing file pages")
        self.pool._bloom(owner, "ones", [], owner.vm)
        missing = {self.image_bytes + i * PAGE for i in range(1, 258)}
        hits, ones = self.pool._bloom(reader, "probe", keys | missing, owner.vm)
        require(hits == keys | missing and not (missing & keys), "Bloom false positive experiment failed")
        self.pool._bloom(owner, "build", keys, owner.vm)
        return {"zero": zero, "ones": ones, "existing_pages_filtered": len(keys),
                "false_positive_pages_rejected_by_exact_lookup": len(missing),
                "candidate_pages_all_zero": 0, "candidate_pages_all_ones": len(keys),
                "authorization": "current guest registration remains required after exact canonical lookup"}

    def overlay_test(self, s):
        self.grow_credits(s, 192)
        copied = self.command_vm(s, "overlay_copyup")
        event = self.event(s, "guest_overlay_copyup", copied["receipt"]["label"])
        require(event["receipt"]["upper_bytes"] == self.payload_bytes and
                event["receipt"]["checked_upper_words"] == self.payload_bytes // 8 and
                event["receipt"]["lower_hash"] == self.payload_hash and event["receipt"]["lower_unchanged"], "Incomplete actual OverlayFS copy-up")
        peers = [self.command_vm(peer, "check", require_cache=True) for peer in self.sandboxes]
        cleanup = self.command_vm(s, "overlay_cleanup")
        return {"vm": s.vm, "epoch": s.epoch, "copyup": copied, "event": event,
                "peer_checks": peers, "cleanup": cleanup}

    def discard_reuse(self, s):
        # Must run immediately after promotion: remote refill or overlay memory
        # pressure can already have replaced the original canonical GPAs.
        self.grow_credits(s, 192)
        before = self.audit_current_cache([s])
        one = before["audits"][0]
        registration = parse_registration((self.out / one["authority"]["file"]).read_text(),
            label=one["authority"]["command"]["receipt"]["label"], vm=s.vm,
            boot_id=one["authority"]["identity"]["receipt"]["boot_id"],
            cookie=one["authority"]["identity"]["receipt"]["cookie"], epoch=s.epoch,
            token=one["authority"]["token"], image_id=self.image_id,
            file_identity=self.fixture["handoff"]["file_identity"],
            extents=self.fixture["handoff"]["logical_extents"], memory_bytes=self.memory_mib * MIB)
        ptes = json.loads((self.out / one["pte_audit_file"]).read_bytes())
        by_gpa = {p["gpa"]: p for p in ptes["pages"]}
        original = {p["gpa"]: by_gpa[p["gpa"]]["actual_pfn"] for p in registration["pages"]
                    if by_gpa[p["gpa"]]["shared"]}
        host_before, _ = self.report_log(s)
        trace_before = self.trace(s)
        _, sequence = self.guest_ack_pages(trace_before)
        evicted = self.command_vm(s, "evict")
        require(evicted["receipt"]["resident_after"] == 0, "Shared cache eviction incomplete")
        waited = self.wait_reported(s, set(original), host_before["maximum_batch"], sequence)
        host_after_trace, _ = self.report_log(s)
        allocated = self.command_vm(s, "anon_alloc", 400)
        filled = self.command_vm(s, "anon_fill")
        verified = self.command_vm(s, "anon_verify")
        require(verified["receipt"]["anon_checked_words"] == 400 * MIB // 8, "400MiB anonymous oracle incomplete")
        anonmap = self.command_vm(s, "anonmap")
        label = anonmap["receipt"]["label"]
        path = s.directory + "/anon-gpas.txt"
        self.shell(s.role, "grep -E " + shlex.quote("^POOL_ANON_RANGE " + label + " ") +
                   f" {s.directory}/fc.log > {path}", "anon-gpa-select")
        raw = self.fetch_bytes(s.role, path, "anon-gpas")
        gpa_file = self.receipts[-1]["file"]
        entries = []
        for line in raw.decode().splitlines():
            name, tag, offset, gpa, count = line.split()
            require(name == "POOL_ANON_RANGE" and tag == label, "Anonymous label mismatch")
            offset, gpa, count = int(offset), int(gpa), int(count)
            require(0 < count <= 400 * MIB // PAGE, "Anonymous count invalid")
            entries.extend((offset + i * PAGE, gpa + i * PAGE) for i in range(count))
        require([off for off, _ in entries] == list(range(0, 400 * MIB, PAGE))
                and len({gpa for _, gpa in entries}) == len(entries), "Anonymous GPA coverage incomplete")
        self.pause(s)
        actual, audit = self.memory_operation(s, "audit")
        current = {p["gpa"]: p for p in actual["pages"]}
        for _, gpa in entries:
            pte = current[gpa]
            require(pte["present"] and pte["writable"] and not pte["shared"]
                    and pte["actual_pfn"] == (actual["phys_base"] + pte["offset"]) // PAGE,
                    "Anonymous page lacks actual private writable CXL PTE")
        self.resume(s)
        reused = {gpa for _, gpa in entries} & set(original)
        require(all(current[gpa]["actual_pfn"] != original[gpa] for gpa in reused), "Reused GPA still aliases canonical")
        detached = {int(line) for line in (self.out / waited["detached_gpas_file"]).read_text().splitlines()}
        joined = reused & detached
        require(len(joined) * PAGE >= self.minimum_cache_bytes, "Insufficient ACK-before-reuse actual GPA/PFN overlap")
        freed = self.command_vm(s, "anon_free")
        after = self.command_vm(s, "warm", require_cache=True)
        peers = [self.command_vm(peer, "check", require_cache=True) for peer in self.sandboxes]
        return {"vm": s.vm, "epoch": s.epoch, "before": before, "host_before": host_before,
            "trace_before": trace_before, "evicted": evicted, "waited": waited, "host_after_trace": host_after_trace,
            "allocated": allocated, "filled": filled, "verified": verified, "anonmap": anonmap,
            "anon_gpa_file": gpa_file, "pte_audit_file": audit["file"], "reused_pages": len(reused),
            "acked_then_reused_pages": len(joined), "freed": freed, "after": after, "peer_checks": peers}

    def audit_current_cache(self, sandboxes, minimum=True):
        """Hold CURRENT guest folios while independently walking actual host PTEs."""
        prepared, paused, audits = [], [], []
        try:
            for s in sandboxes:
                require(s.tier == "cxl", "Physical sharing audit requires CXL VMs")
                self.command_vm(s, "check", require_cache=True)
                registration, authority = self.registration(s)
                prepared.append((s, registration, authority))
                self.pause(s)
                paused.append(s)
            for s, registration, authority in prepared:
                actual, event = self.memory_operation(s, "audit", registration=registration["registration"])
                by_gpa = {p["gpa"]: p for p in actual["pages"]}
                require(len(by_gpa) == self.memory_mib * MIB // PAGE, "PTE audit omitted logical RAM")
                shared = []
                for page in registration["pages"]:
                    pte = by_gpa[page["gpa"]]
                    require(pte["present"] is True, "Current hot file page lacks actual host PTE")
                    require(pte["actual_pfn"] == (actual["phys_base"] + pte["offset"]) // PAGE,
                            "Actual PTE differs from module placement table")
                    if pte["shared"]:
                        require(pte["offset"] == self.pool_offset + page["image_offset"]
                                and pte["writable"] is False, "Canonical alias wrong or writable")
                        shared.append(page["image_offset"])
                if minimum:
                    require(len(shared) * PAGE >= self.minimum_cache_bytes, "Actual current file-cache sharing below target")
                audits.append({"vm": s.vm, "epoch": s.epoch, "role": s.role, "authority": authority,
                    "pte_audit_file": event["file"], "registered_bytes": registration["registered_bytes"],
                    "shared_bytes": len(shared) * PAGE, "shared_offsets": shared})
            common = set.intersection(*(set(a["shared_offsets"]) for a in audits)) if audits else set()
            if minimum:
                require(len(common) * PAGE >= self.minimum_cache_bytes, "VMs do not share the same current file pages")
            return {"audits": audits, "common_shared_bytes": len(common) * PAGE,
                    "freeze": "all registered guest folios held and all target VMs paused during PTE walks"}
        finally:
            for s in paused:
                self.resume(s)
            for s, _, authority in prepared:
                self.release_registration(s, authority["token"])

    @staticmethod
    def same_identity(before, after):
        for key in ("boot_id", "pid", "cookie", "generation", "private_hash", "upper_hash", "payload_bytes"):
            require(before["receipt"][key] == after["receipt"][key], "Transition changed guest " + key)


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("state-only", "direct", "guest-smoke"), default="state-only")
    p.add_argument("--sandboxes", type=int, default=3)
    p.add_argument("--cycles", type=int, default=2)
    p.add_argument("--memory-mib", type=int, default=512)
    p.add_argument("--vcpus", type=int, default=1)
    p.add_argument("--minimum-cache-mib", type=int, default=128)
    p.add_argument("--credit-mib", type=int, default=16)
    p.add_argument("--io-engine", choices=("Sync", "Async"), default="Async")
    p.add_argument("--memory-module", type=Path)
    p.add_argument("--firecracker", type=Path)
    p.add_argument("--memory-smoke", type=Path)
    p.add_argument("--kvm-memory-smoke", type=Path)
    p.add_argument("--full-audit", action="store_true", help="Explicit full PTE evidence for stress verification")
    return p.parse_args()


def main():
    args = arguments()
    require(args.mode == "guest-smoke" or args.full_audit,
            "This stress suite includes explicit PTE evidence: pass --full-audit; SWE replay defaults to no full audit")
    require(args.sandboxes in (1, 3) and args.memory_mib == 512 and args.vcpus == 1,
            "Current lab supports one smoke or three 512MiB/1vCPU sandboxes")
    run, config = lab.current_run()
    out = run / ("pool-handoff-" + str(time.time_ns()))
    out.mkdir()
    c = HandoffController(config, out, args)
    report = {"schema": "guest-pool-handoff-v1", "result": "running", "mode": args.mode,
        "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "config": config, "receipts": c.receipts, "commands": c.commands,
        "authority_events": c.authority_events, "memory_events": c.memory_events,
        "import_negatives": c.import_negatives, "launches": c.launches, "report_logs": c.report_logs, "guest_traces": c.guest_traces,
        "transitions": c.transitions, "api_events": c.api_events, "allocator_events": c.allocator.events,
        "fixture": c.fixture, "limitations": ["Coherent nested KVM on one physical host",
            "Frozen canonical image counted at full physical size", "Trusted fixed guest kernel and controller"]}
    sources = ("pool_handoff_bench.py", "pool_handoff_control.py", "guest_pool_protocol.py",
        "pool_handoff_probe.c", "guest-pool-uapi.h", "guest-pool.c", "pool_memory_uapi.h",
        "pool_memory_module.c", "pool_memory_smoke.c", "pool_memory_kvm_smoke.c", "pool-handoff-l2-init.sh",
        "pool_discard_bench.py", "pool_bench.py", "pool_control.py", "home_storage_bench.py",
        "nfs_bench.py", "trace_bench.py", "lab.py")
    report["source_hashes"], report["source_reference"] = record_sources(HERE, sources)
    report["runtime_overrides"] = {name: {"path": str(path.resolve()), "sha256": lab.sha256(path)}
        for name, path in (("memory_module", args.memory_module), ("firecracker", args.firecracker),
                           ("memory_smoke", args.memory_smoke), ("kvm_memory_smoke", args.kvm_memory_smoke)) if path}
    def save():
        lab.save_json(out / "report.json", report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        setup(c)
        report["runtime_hashes"] = {}
        def upload_override(role, name, path, kind):
            # RPC uploads are restricted to /run and /tmp. Stage the explicit
            # test override there, then install it in this disposable L1.
            staged = "/run/pool-handoff-override-" + name
            c.write_bytes(role, staged, path.read_bytes(), kind,
                          after="mv " + staged + " /lab/" + name)
        for role in ("A", "B"):
            if args.memory_module:
                upload_override(role, "pool_memory_module.ko", args.memory_module, "module-upload")
            for path, name in ((args.firecracker, "firecracker"), (args.memory_smoke, "pool-memory-smoke-20261030"),
                               (args.kvm_memory_smoke, "pool-memory-kvm-smoke-20261030")):
                if path:
                    upload_override(role, name, path, "runtime-upload")
                    c.shell(role, "chmod 755 /lab/" + name, "runtime-executable")
            c.shell(role, "insmod /lab/pool_memory_module.ko\ntest -c /dev/cxl_pool", "load-memory-module")
            raw, name = c.shell(role, "sha256sum /lab/firecracker /lab/l2-vmlinux /lab/pool-handoff-probe "
                "/lab/pool-handoff-l2-initramfs.cpio /lab/pool_memory_module.ko /lab/pool-memory-smoke-20261030 "
                "/lab/pool-memory-kvm-smoke-20261030 /modules/kvm.ko /modules/kvm-intel.ko", "runtime-hashes")
            report["runtime_hashes"][role] = {"file": name, "sha256": {
                path: digest for digest, path in re.findall(r"^([0-9a-f]{64})  (/[A-Za-z0-9_./-]+)\s*$", raw, re.M)}}
        smoke, name = c.shell("A", "/lab/pool-memory-smoke-20261030", "memory-module-smoke", 120)
        report["mapper_smoke"] = {"file": name, "records": json_lines(smoke, "POOL_MEMORY_SMOKE_JSON ")}
        require(len(report["mapper_smoke"]["records"]) == 1, "Mapper smoke did not finish")
        handle, name = c.shell("A", "/lab/pool-memory-handle-smoke", "persistent-handle-smoke", 120)
        report["handle_smoke"] = {"file":name,"records":json_lines(handle,"POOL_HANDLE_SMOKE_JSON ")}
        require(len(report["handle_smoke"]["records"]) == 1, "Handle smoke did not finish")
        if args.kvm_memory_smoke:
            smoke, name = c.shell("A", "/lab/pool-memory-kvm-smoke-20261030 OWNED_POOL_TEST", "kvm-memory-smoke", 100)
            report["kvm_mapper_smoke"] = {"file": name, "records": json_lines(smoke, "POOL_MEMORY_KVM_SMOKE_JSON ")}
            require(len(report["kvm_mapper_smoke"]["records"]) == 1, "KVM mapper smoke did not finish")
        if args.mode == "guest-smoke":
            s = c.sandboxes[0]
            c.boot_vm(s, warm=False)
            c.command_vm(s, "authority_negative")
            cold, event = c.registration(s)
            report["cold_registration"] = event
            require(event["command"]["receipt"]["resident_before"] == event["command"]["receipt"]["resident_after"],
                    "Cold registration faulted pages")
            c.release_registration(s, event["token"], abort=True)
            c.command_vm(s, "warm", require_cache=True)
            warm, event = c.registration(s)
            report["warm_registration"] = event
            require(warm["registered_bytes"] >= c.minimum_cache_bytes, "Warm registration below required size")
            c.command_vm(s, "authority_negative")
            c.release_registration(s, event["token"])
            again, event = c.registration(s)
            report["close_registration"] = event
            c.command_vm(s, "close_abort", event["token"])
            c.registrations.pop(s.vm, None)
            c.command_vm(s, "check", require_cache=True)
        elif args.mode == "state-only":
            report["pool_setup"] = c.pool.setup()
            for s in c.sandboxes:
                c.boot_vm(s)
            report["independent_boots"] = [c.command_vm(s, "identity", require_cache=True) for s in c.sandboxes]
            require(len({r["receipt"]["boot_id"] for r in report["independent_boots"]}) == len(c.sandboxes),
                    "Sandboxes are not independent boots")
            report["negative_controls"] = [c.negative_authority(c.sandboxes[0])]
            report["bloom_negatives"] = c.bloom_negatives(c.sandboxes[0], c.sandboxes[1])
            for cycle in range(args.cycles):
                for s in c.sandboxes:
                    if s.tier == "dram":
                        c.promote(s)
                        for peer in c.sandboxes:
                            c.command_vm(peer, "check", require_cache=True)
                        save()
                report.setdefault("sharing_audits", []).append(c.audit_current_cache(c.sandboxes))
                if cycle == 0:
                    report.setdefault("reuse_tests", []).append(c.discard_reuse(c.sandboxes[0]))
                    c.demote(c.sandboxes[0])
                    c.promote(c.sandboxes[0])
                for s in c.sandboxes:
                    s.generation += 1
                    c.command_vm(s, "write", s.generation)
                    c.state_handoff(s, "B")
                    for peer in c.sandboxes:
                        c.command_vm(peer, "check", require_cache=True)
                    save()
                # A missing guest page may refill a canonical lineage GPA. Grant
                # enough CXL private credits before stressing reclaim/writeback.
                target = c.sandboxes[0]
                c.grow_credits(target, 192)
                report.setdefault("remote_refills", []).append(c.remote_cold_refill(target))
                if cycle == 0:
                    report.setdefault("overlay_tests", []).append(c.overlay_test(target))
                for s in c.sandboxes:
                    c.state_handoff(s, "A")
                    save()
                retired = c.demote(c.sandboxes[0])
                if len(c.sandboxes) >= 2:
                    c.demote(c.sandboxes[1])
                    recipient = c.sandboxes[1]
                    c.promote(recipient)
                    retired_frames = {r["offset"] + i * PAGE for r in retired["returned_ranges"] for i in range(r["pages"])}
                    reused = retired_frames & c.allocator.grants[recipient.owner].frames
                    require(len(reused) * PAGE >= c.minimum_cache_bytes, "Retired frames not reassigned across VMs")
                    report.setdefault("cross_vm_reuse", []).append({"source_vm": c.sandboxes[0].vm,
                        "destination_vm": recipient.vm, "pages": len(reused), "ranges": offset_ranges(reused)})
                for peer in c.sandboxes:
                    c.command_vm(peer, "check", require_cache=True)
                for s in c.sandboxes:
                    if s.tier == "cxl":
                        c.demote(s)
                save()
        else:
            report["pool_setup"] = c.pool.setup()
            c.configure_direct()
            # Ordinary peers are independent VMs. The new PID selector keeps
            # their allocations and EPT bookkeeping out of the shared KVM arena.
            for s in sorted(c.sandboxes, key=lambda item: item.direct):
                c.boot_vm(s)
            report["independent_boots"] = [c.command_vm(s, "identity", require_cache=True) for s in c.sandboxes]
            require(len({r["receipt"]["boot_id"] for r in report["independent_boots"]}) == len(c.sandboxes),
                    "Sandboxes are not independent boots")
            report["negative_controls"] = [c.negative_authority(c.sandboxes[0])]
            report["bloom_negatives"] = c.bloom_negatives(c.sandboxes[0], c.sandboxes[1])
            for s in c.sandboxes:
                c.promote(s)
            report.setdefault("sharing_audits", []).append(c.audit_current_cache(c.sandboxes))
            target = c.sandboxes[0]
            report.setdefault("reuse_tests", []).append(c.discard_reuse(target))
            c.demote(target)
            c.promote(target)
            for cycle in range(args.cycles):
                for role in ("B", "A"):
                    target.generation += 1
                    c.command_vm(target, "write", target.generation)
                    c.direct_handoff(target, role)
                    for peer in c.sandboxes:
                        c.command_vm(peer, "check", require_cache=True)
                    if role == "B":
                        c.grow_credits(target, 192)
                        report.setdefault("remote_refills", []).append(c.remote_cold_refill(target))
                        if cycle == 0:
                            report.setdefault("overlay_tests", []).append(c.overlay_test(target))
                    save()
                c.demote(target)
                for peer in c.sandboxes:
                    c.command_vm(peer, "check", require_cache=True)
                c.promote(target)
                save()
            for s in c.sandboxes:
                c.demote(s)
        if args.mode != "guest-smoke":
            report["canonical_final"] = c.verify_canonical()
            report["allocator_final"] = c.allocator.summary()
            require(report["allocator_final"]["allocated_bytes"] == 0, "Unreleased CXL allocator grants")
        report["result"] = "passed"
    except BaseException as exc:
        report.update(result="failed", error=repr(exc))
        raise
    finally:
        report["diagnostics"] = []
        # Capture before owned L1 shutdown, also when a VMM has already died.
        # These reads are outside all handoff timing windows.
        for role in ("A", "B"):
            try:
                _, name = c.shell(role, "dmesg | tail -n 300", "host-kernel-diagnostic")
                report["diagnostics"].append({"role":role,"file":name})
            except BaseException as exc:
                report["diagnostics"].append({"role":role,"error":repr(exc)})
        for role, directory in c.active:
            for log in ("fc.log", "vmm.log", "stderr.log"):
                try:
                    _, name = c.shell(role, f"tail -n 100 {directory}/{log} 2>/dev/null || true", "vmm-diagnostic")
                    report["diagnostics"].append({"role":role,"directory":directory,"log":log,"file":name})
                except BaseException as exc:
                    report["diagnostics"].append({"role":role,"directory":directory,"error":repr(exc)})
        report["cleanup"] = []
        for role, directory in reversed(c.active):
            try:
                nfs.stop_fc(c.vms[role], directory)
                raw, name = c.shell(role, source_fence_script(directory), "final-fence")
                report["cleanup"].append({"role": role, "directory": directory, "file": name,
                                          "fenced": "FENCED " in raw})
            except BaseException as exc:
                report["cleanup"].append({"role": role, "directory": directory, "error": repr(exc)})
        save()
        print("POOL_HANDOFF_REPORT " + str(out / "report.json"), flush=True)


if __name__ == "__main__":
    main()
