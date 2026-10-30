#!/usr/bin/env python3
"""Real guest free-page-report/host detach/ACK/reuse lab with staged timings."""
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
from pool_bench import PoolController, MIB, HERE
from pool_compare_bench import CompareController, parse_maps, PAGE
from pool_control import PoolControl


def compact_admission(value):
    result = {k: v for k, v in value.items() if k != "plan_pages"}
    result["vmm"] = {k: v for k, v in value["vmm"].items() if k not in ("mapped_pages", "mapped_gpas")}
    return result


def ranges_pages(ranges):
    pages = set()
    for row in ranges:
        gpa, size = row["gpa"], row["bytes"]
        require(gpa % PAGE == size % PAGE == 0 and size > 0, "Bad host discard range")
        pages.update(range(gpa, gpa + size, PAGE))
    return pages


class DiscardController(CompareController):
    def __init__(self, config, out, args):
        args.arm = "patched-cxl-pooled"
        super().__init__(config, out, args)
        self.mode = args.mode
        self.anon_mib = args.anon_mib
        self.restore_mappings = []
        self.report_logs = []
        self.guest_traces = []
        for s in self.sandboxes:
            s.snapshot_file = None

    def start_vm(self, s, restore_hints=None):
        s.directory = f"{self.root}/vm{s.vm}-epoch{s.epoch}"
        s.hints, s.plan, s.report = (s.directory + "/" + name + ".json" for name in ("hints", "plan", "pool-report"))
        s.ram_base = None
        s.snapshot_backing = None
        s.snapshot_file = str(Path(restore_hints).with_name("ram")) if restore_hints and s.tier == "dram" else None
        env = {"PATH": "/bin:/sbin:/usr/bin:/usr/sbin:/lab", "FC_POOL_VM_ID": str(s.vm),
            "FC_POOL_OFFSET": str(self.pool_offset), "FC_POOL_BYTES": str(self.image_bytes),
            "FC_POOL_IMAGE_PATH": "/disk/sandbox.raw", "FC_POOL_IMAGE_ID": self.image_id,
            "FC_POOL_HINTS": s.hints, "FC_POOL_PLAN": s.plan, "FC_POOL_REPORT": s.report,
            "FC_POOL_RUN_EPOCH": str(s.epoch), "FC_DAX_SNAPSHOT": "1" if s.tier == "cxl" else "0"}
        if self.mode == "reporting":
            env.update(FC_POOL_DISCARD="1", FC_POOL_DISCARD_TRACE="1")
        if s.tier == "cxl":
            env.update(FC_DAX_RAM="/dev/dax0.0", FC_DAX_OFFSET=str(s.offset))
        if restore_hints:
            local = s.directory + "/restore-hints.json"
            self.shell(s.role, f"mkdir -p {s.directory}\ncp {shlex.quote(restore_hints)} {local}\n"
                       f"cmp {shlex.quote(restore_hints)} {local}", "local-owner-hints", 600)
            env["FC_POOL_HINTS_RESTORE"] = local
        self.env_by_vm[f"{s.vm}:{s.epoch}"] = env
        envline = " ".join(shlex.quote(k + "=" + v) for k, v in env.items())
        self.active.append((s.role, s.directory))
        self.shell(s.role, f"mkdir -p {s.directory}\nmkfifo {s.directory}/input\n"
            f"(exec 3<>{s.directory}/input\nexec env -i {envline} taskset c /lab/firecracker "
            f"--no-seccomp --api-sock {s.directory}/api.sock --log-path {s.directory}/vmm.log "
            f"<&3 > {s.directory}/fc.log 2> {s.directory}/stderr.log) &\n" +
            nfs.record_pid(s.directory) + f"n=0\nwhile [ ! -S {s.directory}/api.sock ]; do\n"
            f"read p t < {s.directory}/fc.pid\nkill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 1000 ]\nsleep .02\ndone", "start")

    def boot_vm(self, s):
        self.start_vm(s)
        components = {"/machine-config": {"vcpu_count": 2, "mem_size_mib": 512},
            "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux",
                "initrd_path": "/lab/pool-discard-l2-initramfs.cpio",
                "boot_args": f"console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init pool.vm={s.vm} "
                    "page_reporting.page_reporting_order=0 trace_event=pool_discard:pool_free_report "
                    "trace_buf_size=8M trace_clock=mono"},
            "/drives/data": {"drive_id": "data", "path_on_host": "/disk/sandbox.raw", "is_root_device": False,
                "is_read_only": True, "cache_type": "Writeback", "io_engine": "Async"}}
        if self.mode == "reporting":
            components["/balloon"] = {"amount_mib": 0, "deflate_on_oom": False,
                "stats_polling_interval_s": 0, "free_page_hinting": False, "free_page_reporting": True}
        for path, body in components.items():
            self.api(s.role, s.directory, path, "PUT", body)
        self.api(s.role, s.directory, "/actions", "PUT", {"action_type": "InstanceStart"})
        ready = self.event(s, "pool_ready")
        require(ready["receipt"]["vm"] == s.vm, "Wrong fresh VM")
        affinity = self.pin_threads(s)
        mapping = self.mapping(s, initial=True)
        warm = self.command_vm(s, "warm", require_cache=True)
        self.boots.append({"vm": s.vm, "ready": ready, "warm": warm, "mapping": mapping,
                           "affinity_file": affinity, "components": components})

    def admit(self, s, control, phase):
        if s.ram_base is None:
            if s.snapshot_file:
                raw, _ = self.shell(s.role, f"read p t < {s.directory}/fc.pid\ncat /proc/$p/maps", "restore-maps")
                candidates = [m for m in parse_maps(raw) if m["path"] == s.snapshot_file]
                require(candidates, "Snapshot restore VMA missing")
                first = candidates[0]
                s.snapshot_backing = {"path": first["path"], "device": first["device"], "inode": first["inode"]}
            mapping = self.mapping(s, initial=True)
            self.restore_mappings.append({"vm": s.vm, "epoch": s.epoch, "mapping": mapping})
        record = control.admit(s)
        self.latest_admissions[s.vm] = record
        self.pool_events.append({"vm": s.vm, "epoch": s.epoch, "role": s.role, "tier": s.tier,
                                "phase": phase, "report": compact_admission(record)})
        return record

    def treatment(self, s, control, phase):
        start = time.monotonic_ns()
        before = len(self.api_timings)
        self.pause(s)
        paused = time.monotonic_ns()
        record = self.admit(s, control, phase)
        self.resume(s)
        end = time.monotonic_ns()
        row = {"vm": s.vm, "epoch": s.epoch, "phase": phase, "admission": compact_admission(record),
               "observed_control_wall_ns": end - start, "observed_after_pause_until_resume_ns": end - paused,
               "apis": self.api_timings[before:]}
        self.control_events.append(row)
        return row

    def transition(self, s, role, tier, control):
        old_epoch, old_role, old_directory = s.epoch, s.role, s.directory
        trace_before = self.trace(s)
        row = super().transition(s, role, tier, control)
        row["pool"] = compact_admission(row["pool"])
        self.fetch_bytes(old_role, old_directory + "/vmm.log", f"v{s.vm}-e{old_epoch}-vmm-log")
        source = self.receipts[-1]
        source_text = (self.out/source["file"]).read_text()
        rows = [json.loads(line.split("POOL_DISCARD_JSON ", 1)[1])
                for line in source_text.splitlines() if "POOL_DISCARD_JSON " in line]
        cuts = [json.loads(line.split("POOL_DISCARD_SNAPSHOT_JSON ", 1)[1])
                for line in source_text.splitlines() if "POOL_DISCARD_SNAPSHOT_JSON " in line]
        expected_snapshot = old_directory + "/state-snapshot"
        cuts = [cut for cut in cuts if cut["snapshot_path"] == expected_snapshot]
        require(len(cuts) == 1 and cuts[0]["phase"] == "complete" and
                str(cuts[0]["vm_id"]) == str(s.vm) and str(cuts[0]["run_epoch"]) == str(old_epoch),
                "Successful snapshot has no unique reporting cutoff")
        log = {"vm": s.vm, "epoch": old_epoch, "role": old_role, "file": source["file"],
               "events": len(rows), "maximum_batch": max((item["batch"] for item in rows), default=0),
               "retired_after_source_fence": True, "snapshot_cut": cuts[0]}
        self.report_logs.append(log)
        row.update(source_report_log=log, guest_trace_before_transition=trace_before)
        return row

    def report_log(self, s):
        path = s.directory + "/discard-events.jsonl"
        self.shell(s.role, f"grep 'POOL_DISCARD_JSON ' {s.directory}/vmm.log > {path} || test $? = 1", "discard-log-select")
        data = self.fetch_bytes(s.role, path, "discard-events")
        rows = []
        for line in data.decode().splitlines():
            marker = "POOL_DISCARD_JSON "
            if marker in line:
                rows.append(json.loads(line.split(marker, 1)[1]))
        require(all(str(row["vm_id"]) == str(s.vm) and str(row["run_epoch"]) == str(s.epoch) for row in rows),
                "Host reporting identity/epoch mismatch")
        result = {"vm": s.vm, "epoch": s.epoch, "role": s.role, "file": self.receipts[-1]["file"],
                  "events": len(rows), "maximum_batch": max((row["batch"] for row in rows), default=0)}
        self.report_logs.append(result)
        return result, rows

    def trace(self, s):
        command = self.command_vm(s, "trace")
        label = command["receipt"]["label"]
        path = s.directory + "/guest-report-trace.txt"
        self.shell(s.role, "grep -E " + shlex.quote("^POOL_REPORT_(TRACE|STATS)(_| )" + ".*" + label) +
                   f" {s.directory}/fc.log > {path}", "report-trace-select")
        data = self.fetch_bytes(s.role, path, "guest-report-trace")
        text = data.decode()
        require(f"POOL_REPORT_TRACE_BEGIN {label}" in text and f"POOL_REPORT_TRACE_END {label}" in text,
                "Guest trace missing complete boundary")
        stats = [line for line in text.splitlines() if line.startswith("POOL_REPORT_STATS ")]
        require(stats, "Trace stats absent")
        for line in stats:
            if re.search(r"(?:overrun|dropped events)\s*:", line):
                require(int(line.rsplit(":", 1)[1].strip()) == 0, "Guest trace lost events")
        row = {"vm": s.vm, "epoch": s.epoch, "role": s.role, "command": command,
               "file": self.receipts[-1]["file"]}
        self.guest_traces.append(row)
        return row

    def audit_pooled(self, s, control):
        pinned = self.command_vm(s, "cachemap", require_cache=True)
        self.pause(s)
        try:
            before, _ = self.scan(s, pinned=pinned)
            before["canonical_audit"] = control._audit_current_file_cache(
                s, self.latest_admissions[s.vm], pre_pinned=pinned)
            require(before["canonical_audit"]["shared_bytes"] >= self.minimum_cache_bytes,
                    "Current pinned file cache is not canonical")
            return before
        finally:
            self.resume(s)
            self.command_vm(s, "cacheunmap", require_cache=True)

    def guest_ack_pages(self, trace, after_sequence=0):
        text = (self.out / trace["file"]).read_text()
        entries = [tuple(map(int, values)) for values in re.findall(
            r"seq=(\d+) phase=(\d+) index=(\d+) nents=(\d+) gpa=(\d+) bytes=(\d+) result=(-?\d+)", text)]
        ranges = {}
        acked = set()
        for seq, phase, index, nents, gpa, size, result in entries:
            if phase == 1:
                ranges.setdefault(seq, []).append({"gpa": gpa, "bytes": size})
            elif phase == 2:
                require(result == 0, "Guest report ACK failed")
                acked.add(seq)
        pages = set()
        for seq in acked:
            if seq > after_sequence:
                pages |= ranges_pages(ranges.get(seq, []))
        return pages, max((entry[0] for entry in entries), default=0)

    def wait_reported(self, s, original_gpas, after_batch, after_guest_sequence):
        start = time.monotonic_ns()
        waits = []
        for _ in range(35):
            waits.append(self.command_vm(s, "wait", 2000))
            log, events = self.report_log(s)
            mapped = {row["batch"]: row for row in events if row["phase"] == "mapped"}
            acked = {row["batch"] for row in events if row["phase"] == "ack"}
            detached = set()
            for batch in sorted(acked):
                if batch <= after_batch:
                    continue
                require(batch in mapped and mapped[batch]["copied_bytes"] == 0, "ACK without no-copy host work")
                detached |= ranges_pages(mapped[batch]["discarded"])
            overlap = detached & original_gpas
            if len(overlap) * PAGE >= self.minimum_cache_bytes:
                trace = self.trace(s)
                guest_acked, _ = self.guest_ack_pages(trace, after_guest_sequence)
                overlap &= guest_acked
                if len(overlap) * PAGE < self.minimum_cache_bytes:
                    continue
                return {"observed_wall_ns": time.monotonic_ns() - start, "guest_waits": waits,
                        "log": log, "after_batch": after_batch, "after_guest_sequence": after_guest_sequence,
                        "trace_before_reuse": trace, "detached_original_pages": len(overlap),
                        "detached_gpas_file": self.raw("detached-target-gpas", "".join(f"{gpa}\n" for gpa in sorted(overlap)))}
        raise RuntimeError("Reporter did not detach >=128MiB of original cache before reuse")

    def scan_anon(self, s, command):
        label = command["receipt"]["label"]
        self.serial += 1
        prefix = f"/run/discard-anon-{self.serial}"
        self.shell(s.role, "grep -E " + shlex.quote("^POOL_ANON_RANGE " + label + " ") +
                   f" {s.directory}/fc.log > {prefix}.gpas", "anon-gpa-select")
        data = self.fetch_bytes(s.role, prefix + ".gpas", "anon-gpas")
        gpa_file = self.receipts[-1]["file"]
        entries = []
        for line in data.decode().splitlines():
            name, tag, off, gpa, count = line.split()
            require(name == "POOL_ANON_RANGE" and tag == label, "Anonymous oracle label mismatch")
            off, gpa, count = int(off), int(gpa), int(count)
            require(0 < count <= self.anon_mib * MIB // PAGE, "Anonymous oracle count")
            entries.extend((off + index * PAGE, gpa + index * PAGE) for index in range(count))
        require([off for off, _ in entries] == list(range(0, self.anon_mib * MIB, PAGE)) and
                len({gpa for _, gpa in entries}) == len(entries), "Anonymous oracle coverage")
        mapping = self.mapping(s)
        rows = "".join(f"{off} {gpa} {s.ram_base+gpa}\n" for off, gpa in entries).encode()
        self.write_bytes(s.role, prefix + ".rows", rows, "anon-pfn-upload")
        rows_file = self.raw("anon-pfn-rows", rows.decode())
        raw, summary_file = self.shell(s.role, f"/lab/pool-compare-pfn {mapping['pid']} {prefix}.rows {prefix}.pfns", "anon-pfn-scan", 180)
        summary = json_lines(raw, "POOL_COMPARE_PFN_JSON ")
        require(len(summary) == 1 and summary[0]["all_checked"] and summary[0]["rows"] == len(entries),
                "Anonymous PFN scan incomplete")
        data = self.fetch_bytes(s.role, prefix + ".pfns", "anon-pfn-values")
        values = [list(map(int, line.split())) for line in data.decode().splitlines()]
        require(len(values) == len(entries) and all(row[:3] == [off, gpa, s.ram_base+gpa]
                for row, (off, gpa) in zip(values, entries)), "Anonymous PFN rows mismatch")
        return {**mapping, "guest_command": command, "gpa_file": gpa_file, "rows_file": rows_file,
                "pfns_file": self.receipts[-1]["file"], "summary_file": summary_file, "summary": summary[0]}, values


def sample(c, s, control, number, wait_for_reporting=True, nfs_refill=False):
    admission = c.treatment(s, control, f"sample-{number}-before")
    before = c.audit_pooled(s, control)
    original = [list(map(int, line.split())) for line in (c.out / before["pfns_file"]).read_text().splitlines()]
    original_by_gpa = {row[1]: row[3] for row in original}
    mapped = c.latest_admissions[s.vm]["vmm"]["mapped_pages"]
    require(len(mapped) * PAGE >= 128 * MIB, "Sample not actually pooled")
    host_before, _ = c.report_log(s)
    trace_before = c.trace(s)
    _, guest_sequence = c.guest_ack_pages(trace_before)
    hot = c.command_vm(s, "check", require_cache=True)
    require(hot["receipt"]["block_read_ios"] == 0, "Hot sample issued block IO")
    evicted = c.command_vm(s, "evict")
    require(evicted["receipt"]["resident_after"] == 0, "Eviction failed")
    waited = None
    if c.mode == "reporting" and wait_for_reporting:
        waited = c.wait_reported(s, set(original_by_gpa), host_before["maximum_batch"], guest_sequence)
    memory = c.command_vm(s, "identity")
    require(memory["receipt"]["mem_available_kib"] >= (c.anon_mib + 16) * 1024,
            "Insufficient measured guest memory reserve for anonymous workload")
    allocated = c.command_vm(s, "anon_alloc", c.anon_mib)
    filled = c.command_vm(s, "anon_fill")
    verified = c.command_vm(s, "anon_verify")
    require(verified["receipt"]["anon_checked_words"] == c.anon_mib * MIB // 8, "Incomplete anonymous verification")
    anonmap = c.command_vm(s, "anonmap")
    c.pause(s)
    try:
        anon, values = c.scan_anon(s, anonmap)
    finally:
        c.resume(s)
    by_gpa = {row[1]: row[3] for row in values}
    reused = set(by_gpa) & set(original_by_gpa)
    changed = {gpa for gpa in reused if by_gpa[gpa] != original_by_gpa[gpa]}
    require(len(reused) * PAGE >= 128 * MIB and changed == reused,
            "Anonymous workload did not safely reuse >=128MiB of originally shared GPAs")
    host_after, _ = c.report_log(s)
    trace = c.trace(s)
    detached_reused = None
    if waited:
        detached = {int(line) for line in (c.out / waited["detached_gpas_file"]).read_text().splitlines()}
        detached_reused = len(detached & reused)
        require(detached_reused * PAGE >= 128 * MIB, "Reported pages were not actually reused")
    freed = c.command_vm(s, "anon_free")
    nfs_evidence = None
    if nfs_refill:
        require(s.role == "B", "NFS refill requires remote B")
        raw, file = c.shell("B", "sync\necho 3 > /proc/sys/vm/drop_caches\ncat /proc/net/rpc/nfs", "nfs-before")
        reads = int(next(line.split()[8] for line in raw.splitlines() if line.startswith("proc3 ")))
        nfs_evidence = {"before_file": file, "before_reads": reads}
    else:
        c.hostwarm(s.role)
    refill = c.command_vm(s, "warm", require_cache=True)
    require(refill["receipt"]["resident_before"] == 0 and refill["receipt"]["block_read_ios"] > 0, "Refill not guest-cold")
    if nfs_refill:
        raw, file = c.shell("B", "cat /proc/net/rpc/nfs", "nfs-after")
        reads = int(next(line.split()[8] for line in raw.splitlines() if line.startswith("proc3 ")))
        nfs_evidence.update(after_file=file, after_reads=reads, read_delta=reads-nfs_evidence["before_reads"])
        require(nfs_evidence["read_delta"] > 0, "Remote refill did not use NFS")
    c.treatment(s, control, f"sample-{number}-after-refill")
    peers = [c.command_vm(peer, "check", require_cache=True) for peer in c.sandboxes if peer.vm != s.vm]
    return {"vm": s.vm, "epoch": s.epoch, "role": s.role, "tier": s.tier, "number": number,
            "wait_for_reporting": wait_for_reporting, "admission": admission,
            "before": before, "host_before": host_before, "trace_before": trace_before, "hot": hot, "evicted": evicted,
            "waited": waited, "memory_before_allocate": memory,
            "allocated": allocated, "filled": filled, "verified": verified,
            "anon": anon, "host_after": host_after, "trace": trace, "freed": freed, "refill": refill,
            "reused_original_pages": len(reused), "changed_reused_pages": len(changed),
            "detached_then_reused_pages": detached_reused, "peers": peers, "nfs": nfs_evidence}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("baseline", "reporting"), required=True)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--anon-mib", type=int, default=400)
    p.add_argument("--lifecycle", action="store_true")
    p.add_argument("--profile", action="store_true")
    a = p.parse_args()
    a.memory_mib, a.vcpus, a.sandboxes, a.minimum_cache_mib, a.io_engine = 512, 2, 3, 128, "Async"
    require(1 <= a.rounds <= 5, "Invalid rounds")
    require(1 <= a.anon_mib <= 448, "Invalid anonymous workload size")
    run, config = lab.current_run()
    out = run / ("discard-" + a.mode + "-" + str(time.time_ns()))
    out.mkdir()
    c = DiscardController(config, out, a)
    control = PoolControl(c)
    manifest = json.loads((Path(config["assets"]) / "manifest.json").read_text())
    sources = ["pool_discard_bench.py", "pool_discard_run.py", "pool_discard_prepare.py", "pool_discard_probe.c",
        "pool-discard-l2-init.sh", "pool-discard-guest-trace.patch", "pool_compare_bench.py", "pool_compare_pfn.c",
        "pool_bench.py", "pool_control.py", "home_storage_bench.py", "lab.py", "nfs_bench.py", "trace_bench.py", "firecracker.patch",
        "pool_cow_trace.py", "pool_cow_latency.c"]
    (out / "measured-sources").mkdir()
    for name in sources:
        shutil.copy2(HERE / name, out / "measured-sources" / name)
    guest_source = Path(manifest["discard"]["guest_source"])
    guest_names = {"guest-virtio_balloon.c": "drivers/virtio/virtio_balloon.c",
                   "guest-page_reporting.c": "mm/page_reporting.c",
                   "guest-pool_discard.h": "include/trace/events/pool_discard.h"}
    for name, path in guest_names.items():
        require(lab.sha256(guest_source/path) == manifest["discard"]["guest_source_hashes"][path],
                "Guest compiled source changed")
        shutil.copy2(guest_source/path, out/"measured-sources"/name)
    shutil.copy2(Path(config["assets"])/"l2-discard.config", out/"measured-sources"/"guest.config")
    report = {"schema": "pool-discard-arm-v1", "result": "running", "mode": a.mode, "config": config,
        "configuration": {"rounds": a.rounds, "lifecycle": a.lifecycle, "profile": a.profile,
            "memory_mib": 512, "vcpus": 2, "io_engine": "Async", "payload_mib": 160, "anon_mib": a.anon_mib,
            "env_by_vm": c.env_by_vm, "no_seccomp": True, "expected_binary_sha256": c.expected_binary_sha256},
        "fixture_manifest": manifest, "source_hashes": {name: lab.sha256(HERE / name) for name in sources},
        "receipts": c.receipts, "commands": c.commands, "api_timings": c.api_timings,
        "boots": c.boots, "samples": [], "control_events": c.control_events, "pool_events": c.pool_events,
        "transitions": c.transitions, "restore_mappings": c.restore_mappings,
        "report_logs": c.report_logs, "guest_traces": c.guest_traces}
    report["source_hashes"].update({name: lab.sha256(out/"measured-sources"/name)
                                    for name in (*guest_names, "guest.config")})
    def save():
        lab.save_json(out / "report.json", report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report["affinity"] = nfs.pin_vms(config, argparse.Namespace(host_cpus_a="0-3", host_cpus_b="4-7"))
        setup(c)
        require(all(row["sha256"] == c.image_id for row in c.last_image_hashes.values()), "Image identity mismatch")
        report["initial_image_hashes"] = dict(c.last_image_hashes)
        for role in ("A", "B"):
            _, file = c.shell(role, "echo 1048576 > /proc/sys/vm/max_map_count\n"
                "if [ -e /sys/kernel/mm/transparent_hugepage/enabled ]; then echo never > /sys/kernel/mm/transparent_hugepage/enabled; fi\n"
                "if [ -e /sys/kernel/mm/ksm/run ]; then test $(cat /sys/kernel/mm/ksm/run) = 0; fi\n"
                "uname -a\ncat /proc/cmdline", "system-policy")
            report.setdefault("system_policy", {})[role] = {"file": file}
            names = ("firecracker", "l2-vmlinux", "pool-discard-l2-initramfs.cpio",
                     "pool-discard-probe", "pool-cow-latency")
            raw, file = c.shell(role, "sha256sum " + " ".join("/lab/" + name for name in names),
                                "runtime-fixture-hashes")
            hashes = dict((name, digest) for digest, name in re.findall(
                r"^([0-9a-f]{64})  /lab/([^\r\n]+)\s*$", raw, re.M))
            require(hashes == {name: manifest["artifacts"][name] for name in names},
                    "Live L1 kernel/helper/runtime fixture differs from manifest")
            report.setdefault("fixture_runtime_hashes", {})[role] = {"file": file, "hashes": hashes}
        c.shell("A", f"mkdir -p {c.storage}", "storage-directory")
        raw, file = c.shell("A", "/lab/pool-cow-latency /dev/dax0.0 3758096384 100 OWNED_POOL_TEST", "one-page-latency", 180)
        report["one_page_latency"] = {"file": file}
        report["canonical_setup"] = control.setup()
        for s in c.sandboxes:
            c.hostwarm()
            c.boot_vm(s)
            c.treatment(s, control, "initial")
        report["initial_group"] = c.audit_group(c.sandboxes)
        require(report["initial_group"]["same_offset_shared_pages"] * PAGE >= 128 * MIB, "Initial actual cache sharing missing")
        save()
        for number in range(a.rounds):
            for s in c.sandboxes[number % 3:] + c.sandboxes[:number % 3]:
                report["samples"].append(sample(c, s, control, number))
                save()
                print(f"DISCARD_SAMPLE {a.mode} round={number} vm={s.vm}", flush=True)
        if a.mode == "reporting":
            report["immediate_reuse"] = sample(c, c.sandboxes[0], control, "immediate", wait_for_reporting=False)
            save()
        if a.lifecycle:
            require(a.mode == "reporting", "Lifecycle requires reporting arm")
            s = c.sandboxes[0]
            for role, tier in (("B", "cxl"), ("A", "dram"), ("A", "cxl")):
                c.transition(s, role, tier, control)
                for peer in c.sandboxes:
                    c.command_vm(peer, "check", require_cache=True)
                if tier == "cxl":
                    report.setdefault("lifecycle_reuse", []).append(sample(c, s, control, "lifecycle-"+str(s.epoch), nfs_refill=role=="B"))
                save()
        if a.profile:
            from pool_cow_trace import profile_recycle
            target = c.sandboxes[0]
            c.treatment(target, control, "before-profile")
            before = c.audit_pooled(target, control)
            hot = c.command_vm(target, "check", require_cache=True)
            report["profile"] = profile_recycle(c, target)
            c.pause(target)
            try:
                after, values = c.scan(target, reuse=before)
            finally:
                c.resume(target)
            old = [list(map(int, line.split())) for line in (c.out/before["pfns_file"]).read_text().splitlines()]
            report["profile"].update(before=before, hot=hot, after=after,
                original_cache_gpas_with_changed_pfns=sum(a[3] != b[3] for a, b in zip(old, values)))
            c.hostwarm(target.role)
            c.command_vm(target, "warm", require_cache=True)
            c.treatment(target, control, "after-profile-refill")
            save()
        report["final_group"] = c.audit_group(c.sandboxes)
        require(report["final_group"]["same_offset_shared_pages"] * PAGE >= 128 * MIB, "Final shared cache missing")
        report["canonical_check"] = c.verify_canonical()
        report["final_checks"] = [c.command_vm(s, "check", require_cache=True) for s in c.sandboxes]
        for s in c.sandboxes:
            c.trace(s)
            c.pause(s)
            c.report_log(s)
        report["result"] = "passed"
    except BaseException as exc:
        report["result"] = "failed"
        report["error"] = repr(exc)
        raise
    finally:
        for s in c.sandboxes:
            if s.directory:
                try:
                    c.fetch_bytes(s.role, s.directory+"/fc.log", f"vm{s.vm}-final-log")
                    c.fetch_bytes(s.role, s.directory+"/vmm.log", f"vm{s.vm}-final-vmm-log")
                    c.fetch_bytes(s.role, s.directory+"/stderr.log", f"vm{s.vm}-final-stderr-log")
                    nfs.stop_fc(c.vms[s.role], s.directory)
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(repr(exc))
        save()
        print("POOL_DISCARD_REPORT " + str(out / "report.json"), flush=True)


if __name__ == "__main__":
    main()
