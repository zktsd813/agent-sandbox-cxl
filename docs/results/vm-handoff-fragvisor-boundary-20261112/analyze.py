#!/usr/bin/env python3
"""Recompute the 09 report from immutable lab reports and separate trace runs."""
import gzip
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[2]
VM = REPO / "implementation/virtual_machine"
LOGS = REPO / ".codex-runs"


def stats(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": statistics.median(values),
            "p95": values[math.ceil(.95 * len(values)) - 1],
            "mean": statistics.mean(values), "min": values[0], "max": values[-1]}


def read(name):
    evidence = json.loads((LOGS / f"09-{name}-evidence.json").read_text())
    assert evidence["result"] == "passed", name
    report = json.loads(Path(evidence["report"]).read_text())
    assert report["result"] == "passed", name
    rows = sum(report["samples"].values(), [])
    return evidence, report, rows


def events(rows, event):
    return {r["round"]: r for r in rows if r["event"] == event}


def identity(evidence, report):
    config = json.loads((Path(evidence["run"]) / "run.json").read_text())
    return {"commit": evidence["commit"], "run": evidence["run"], "report": evidence["report"],
            "evidence": str(LOGS / f"09-{evidence['name']}-evidence.json"),
            "assets": config["assets"], "prepared_artifacts": config["prepared"]["artifacts"],
            "source_patches": config["source_patches"],
            "poll_us": report["poll_us"], "spin_us": report["spin_us"],
            "timer_slack_ns": report["timer_slack_ns"],
            "guest_cpu": report["guest_cpu"], "executor_cpu": report["executor_cpu"],
            "warmup": report["warmup"], "iterations": report["iterations"]}


def ms_to_us(summaries):
    return {key: {stat: value if stat == "n" else value * 1000 for stat, value in values.items()}
            for key, values in summaries.items()}


def direct(name):
    evidence, report, rows = read(name)
    source, dest = events(rows, "boundary_source"), events(rows, "boundary_destination")
    rates = {r["l1_tsc_khz"] for r in dest.values()}
    assert len(rates) == 1
    ticks_per_us = rates.pop() / 1000
    calibrations = []
    for phase in (0, 1):
        pings = [r for r in rows if r["event"] == "clock_sync" and r["phase"] == phase]
        assert len(pings) == 200
        best = min(pings, key=lambda r: r["a_end_tsc"] - r["a_begin_tsc"])
        delta = best["b_tsc"] - (best["a_end_tsc"] + best["a_begin_tsc"]) / 2
        error = (best["a_end_tsc"] - best["a_begin_tsc"]) / 2
        calibrations.append({"phase": "start" if phase == 0 else "end", "delta_ticks": delta,
                             "delta_us": delta / ticks_per_us, "error_bound_us": error / ticks_per_us,
                             "rtt_us": 2 * error / ticks_per_us, "best_sample": best})
    delta = statistics.mean(c["delta_ticks"] for c in calibrations)
    drift = calibrations[1]["delta_us"] - calibrations[0]["delta_us"]
    measured = []
    for i in range(report["warmup"], report["warmup"] + report["iterations"]):
        s, d = source[i], dest[i]
        sign = 1 if s["role"] == "A" else -1
        raw = (d["entry_tsc"] - s["exit_tsc"]) / ticks_per_us
        stages = {
            "exit_to_detach_return": (s["detached_tsc"] - s["exit_tsc"]) / ticks_per_us,
            "detach_return_to_publish": (s["published_tsc"] - s["detached_tsc"]) / ticks_per_us,
            "publish_to_notified": (d["notified_tsc"] - s["published_tsc"] - sign * delta) / ticks_per_us,
            "notified_to_entry": (d["entry_tsc"] - d["notified_tsc"]) / ticks_per_us,
        }
        one_way = raw - sign * delta / ticks_per_us
        assert one_way > 0 and all(v >= 0 for v in stages.values()), (name, i, stages)
        assert abs(one_way - sum(stages.values())) < 1e-8
        measured.append({"round": i, "direction": s["role"] + "→" + d["role"],
                         "one_way_us": one_way, "raw_one_way_us": raw, "stages_us": stages,
                         "request_to_exit_signed_us": (s["exit_tsc"] - s["request_tsc"]) / ticks_per_us,
                         "entry_to_controller_observed_us": (d["observed_tsc"] - d["entry_tsc"]) / ticks_per_us})
    pairs = [(a["raw_one_way_us"] + b["raw_one_way_us"]) / 2
             for a, b in zip(measured[::2], measured[1::2])]
    assert len(pairs) * 2 == len(measured)
    assert abs(statistics.mean(r["one_way_us"] for r in measured) - statistics.mean(pairs)) < 1e-8
    assert abs(drift) <= sum(c["error_bound_us"] for c in calibrations)
    result = identity(evidence, report)
    result.update(memory_mib=report["memory_mib"], profile=report["profile"],
                  resume_profile=report["resume_profile"], l1_tsc_khz=ticks_per_us * 1000,
                  calibration=calibrations, delta_used_us=delta / ticks_per_us, observed_drift_us=drift,
                  conservative_offset_error_us=max(c["error_bound_us"] for c in calibrations) + abs(drift) / 2,
                  one_way_us=stats(r["one_way_us"] for r in measured),
                  directions={direction: stats(r["one_way_us"] for r in measured if r["direction"] == direction)
                              for direction in ("A→B", "B→A")},
                  direction_stage_us={direction: {stage: stats(r["stages_us"][stage] for r in measured if r["direction"] == direction)
                                                   for stage in measured[0]["stages_us"]}
                                      for direction in ("A→B", "B→A")},
                  stages_us={stage: stats(r["stages_us"][stage] for r in measured)
                             for stage in measured[0]["stages_us"]},
                  outside_boundary_us={key: stats(r[key] for r in measured) for key in
                                       ("request_to_exit_signed_us", "entry_to_controller_observed_us")},
                  method_b_uncorrected_pair_average_us=stats(pairs),
                  method_a_minus_b_mean_us=statistics.mean(r["one_way_us"] for r in measured) - statistics.mean(pairs),
                  method_b_note="A→B/B→A pair averages cancel a constant offset algebraically. The matching mean is a consistency check, not an independent validation of directional calibration. Pair and pooled percentiles describe different distributions.",
                  local_profile_us=ms_to_us(report["summary_ms"]),
                  validation=report["validation"])
    if report["memory_mib"] == 2:
        result["outside_boundary_note"] = "CPU guest already completed HLT before the controller starts DETACH. Negative request→exit denotes exit→request preparation, not a negative kick latency. Observed completion is HLT, not a Linux heartbeat."
    return result


def reuse():
    evidence, report, rows = read("reuse128")
    source, dest = events(rows, "source"), events(rows, "destination")
    values = []
    for i in range(report["warmup"], report["warmup"] + report["iterations"]):
        s, d = source[i], dest[i]
        proxy = s["snapshot_us"] + s["publish_us"] + d["receive_us"] + d["restore_us"] + d["resume_us"]
        values.append({"round": i, "direction": s["role"] + "→" + d["role"], "proxy_us": proxy})
    result = identity(evidence, report)
    transport = [source[i]["publish_us"] + dest[i]["receive_us"] for i in source if i >= report["warmup"]]
    save_internal = {p["round"]: p for p in report["save_profiles"] if p["scope"] == "create_snapshot"}
    restore_internal = {p["round"]: p for p in report["restore_profiles"] if p["scope"] == "reuse"}
    restore_vcpu = {p["round"]: p for p in report["restore_profiles"] if p["scope"] == "reuse_vcpu"}
    indices = range(report["warmup"], report["warmup"] + report["iterations"])
    result.update(exact_exit_to_entry_us=None, closest_stage_sum_us=stats(r["proxy_us"] for r in values),
                  transport_publish_plus_receive_us=stats(transport),
                  profile_gaps_us={
                      "save_api_minus_create_snapshot_internal": stats(source[i]["snapshot_us"] - save_internal[i]["total_us"] for i in indices),
                      "restore_api_minus_reuse_internal": stats(dest[i]["restore_us"] - restore_internal[i]["total_us"] for i in indices),
                      "vcpu_restore_request_minus_vcpu_thread_internal": stats(restore_internal[i]["stages_us"]["vcpu_state_restore"] - restore_vcpu[i]["total_us"] for i in indices)},
                  profile_interpretation={"code_confirmed": "Two HTTP/API calls surround collection and restore; VMM↔vCPU thread messaging, separate KVM ioctls, bitcode plus CRC64 and tmpfs checkpoint/mailbox copies are present. MicrovmState includes VM/CPUID/debug/clock/device state beyond registers.",
                                          "code_references": ["firecracker.patch:2552 save_state profile", "firecracker.patch:2571 vCPU request/collection", "firecracker.patch:2715 encoding/file write", "firecracker.patch:4195 deserialize", "firecracker.patch:4244 vCPU restore request", "firecracker.patch:5072 apply_arch_state"],
                                          "inference": "More MSRs and user/kernel/thread boundaries help explain higher cost than FragVisor's reported in-kernel 38 us, but per-ioctl costs and an exactly matching register-dump boundary were not measured."},
                  closest_stage_sum_definition="snapshot API + publish mailbox + receive mailbox/file + restore API + resume API; per-round sum",
                  closest_stage_sum_differences=["starts after pause API completes, later than source last VM-exit",
                                                "omits notification polling/wake gaps between separately measured stages",
                                                "ends at resume API response rather than first VM-entry; response can follow entry"],
                  exact_boundary_unavailable_reason="The optional DAX resume profile is attached to shared KVM objects. Native reuse keeps separate private KVM objects; instrumenting that path would extend beyond the authorized kernel profile paths.",
                  directions={direction: stats(r["proxy_us"] for r in values if r["direction"] == direction)
                              for direction in ("A→B", "B→A")},
                  local_profile_us=ms_to_us(report["summary_ms"]),
                  save_profile_us=ms_to_us(report["save_profile_ms"]),
                  restore_profile_us=ms_to_us(report["restore_profile_ms"]),
                  prepare_profile_us=ms_to_us(report["prepare_profile_ms"]),
                  state_payload_bytes=report["validation"]["state_bytes"],
                  saved_msr_count=report["cpu_state_handoffs"][0]["source"]["msrs"],
                  ioctl_count_code_audit={"vcpu_save_common_path": 11, "vcpu_restore_common_path": 12,
                                          "restore_pending_io_completion_kvm_run": 1,
                                          "prepared_translation_invalidation_get_set_msr": 2,
                                          "caveat": "Static common-path count, not a syscall trace; excludes VM/IRQFD ioctls, error/retry paths and extra MSR chunks. 190 MSRs fit one GET batch; deferred MSRs split SET into two batches."},
                  samples=values, validation=report["validation"])
    return result


def dense(name):
    evidence, report, rows = read(name)
    source = events(rows, "source")
    sweeps = events(rows, "working_set")
    values = []
    for i, w in sorted(sweeps.items()):
        values.append({"round": i, "destination": w["role"],
                       "first_scan_ms": w["first_sweep_ns"] / 1e6,
                       "warm_scan_ms": w["warm_sweep_ns"] / 1e6,
                       "first_minus_warm_ms": (w["first_sweep_ns"] - w["warm_sweep_ns"]) / 1e6,
                       "pause_to_working_set_ready_ms": source[i]["pause_to_working_set_ready_us"] / 1000,
                       "first_destination_visit": i in (0, 1),
                       "new_kvm_ept": report.get("preset") == "snapshot"})

    def group(selected):
        return {key: stats(r[key] for r in selected) for key in
                ("first_scan_ms", "warm_scan_ms", "first_minus_warm_ms", "pause_to_working_set_ready_ms")}

    result = identity(evidence, report)
    measured = [r for r in values if r["round"] >= report["warmup"]]
    result.update(memory_mib=report["memory_mib"], working_set=report["working_set"],
                  measured=group(measured), first_B_arrival=group(values[:1]),
                  first_A_return=group(values[1:2]), repeat_visits_including_warmup=group(values[2:]),
                  directions={role: group([r for r in measured if r["destination"] == role]) for role in ("A", "B")},
                  first_visit_note="First primary-guest arrival on B is round 0; A is the already-warm boot owner on round 1. Reuse B has a separately booted seed's EPT. Snapshot creates empty EPT on every arrival, including repeats. First-visit samples n=1 per destination occur within warmup.",
                  validation=report["validation"], samples=values)
    return result


def ept(name):
    evidence, report, rows = read(name)
    sweep_rip = 0x406373
    result = identity(evidence, report)
    result.update(trace_only=True, working_set_pages=report["working_set"]["bytes"] // 4096,
                  scan_load_rip=hex(sweep_rip), raw_traces=evidence["ept_traces"], rounds=[])
    destinations = events(rows, "destination")
    sweeps = events(rows, "working_set")
    for role, trace in evidence["ept_traces"].items():
        text = gzip.decompress(Path(trace["path"]).read_bytes()).decode()
        faults = []
        active_round = None
        for line in text.splitlines():
            marker = re.search(r"SNAP_ACK.*round=(\d+) phase=LOAD_BEGIN", line)
            if marker:
                active_round = int(marker[1])
            m = re.search(r"(\d+\.\d+): kvm_page_fault: vcpu \d+ rip 0x([0-9a-f]+) address 0x([0-9a-f]+) error_code 0x([0-9a-f]+)", line)
            if m:
                faults.append({"seconds": float(m[1]), "rip": int(m[2], 16), "gpa": int(m[3], 16),
                               "qualification": int(m[4], 16), "round": active_round})
        for i, d in destinations.items():
            if d["role"] != role:
                continue
            if name == "ept-direct":
                begin = d["entry_ns"] / 1e9
                end = (d["observed_ns"] + sweeps[i]["first_ack_to_working_set_ready_us"] * 1000) / 1e9
                selected = [f for f in faults if begin <= f["seconds"] <= end]
            else:
                selected = [f for f in faults if f["round"] == i]
            scan = [f for f in selected if f["rip"] == sweep_rip]
            unique = len({f["gpa"] >> 12 for f in scan})
            assert unique == len(scan), (name, role, i)
            data = [f for f in scan if f["qualification"] & (1 << 8)]
            page_walk = [f for f in scan if not f["qualification"] & (1 << 8)]
            result["rounds"].append({"round": i, "destination": role, "first_destination_visit": i in (0, 1),
                                     "scan_ept_violations": len(scan), "data_page_violations": len(data),
                                     "page_walk_violations": len(page_walk), "unique_scan_gpa_pages": unique,
                                     "all_faults_in_window": len(selected)})
    result["rounds"].sort(key=lambda r: r["round"])
    assert len(result["rounds"]) == 4
    if name == "ept-snapshot":
        assert all(r["data_page_violations"] == result["working_set_pages"] for r in result["rounds"])
    result["method"] = ("L1 kvm:kvm_page_fault tracepoint in handle_ept_violation. Filter exact L2 scan load RIP from objdump. "
                        "Direct uses destination-local entry→first-scan-observed timestamps, excluding A boot. Native uses per-round SNAP_ACK gated windows. "
                        "Native windows cover both scans, but no GPA repeats: each fault is on its first scan access; no warm-scan refault. "
                        "Trace error_code is VMX exit qualification here: bit 8 separates final data accesses from page-table walks. All per-CPU overrun/drop counters are zero.")
    return result


def placement_summary(names):
    labs = {}
    for name in names:
        evidence, report, _ = read(name)
        other = {}
        for snap in [evidence["host_before"], *evidence["host_samples"], evidence["host_after"]]:
            for line in snap["ps"].splitlines()[1:]:
                pid, user, cpu, percent, comm = line.split(None, 4)
                if 32 <= int(cpu) <= 63 and user != "ijkim" and (user != "root" or float(percent) > 0):
                    other[pid] = {"user": user, "comm": comm, "lifetime_cpu_percent": percent}
        labs[name] = {"lab_budget_mib": evidence["lab_budget_mib"],
                      "node1_free_before_mib": int(re.search(r"node 1 free: (\d+) MB", evidence["hardware_before"])[1]),
                      "before": evidence["placement_before"], "after": evidence["placement_after"],
                      "loadavg_before": evidence["host_before"]["loadavg"], "loadavg_after": evidence["host_after"]["loadavg"],
                      "other_processes_observed_on_cpus_32_63": other,
                      "interference": "Other users' pre-existing mostly idle codex/node/sshd and system services observed; no new compute-heavy job identified. One-second ps snapshots cannot exclude short interference or explain all outliers.",
                      "owned_qemu_remaining": evidence["owned_qemu_remaining"],
                      "backing_remaining": evidence["backing_remaining"]}
        assert not evidence["owned_qemu_remaining"] and not evidence["backing_remaining"]
    return labs


def main():
    assert set(os.sched_getaffinity(0)) <= set(range(32, 64))
    direct_names = ["final-cpu50", "final-cpu0", "final-linux50", "final-linux0", "final-linux0-session2"]
    dense_names = ["dense-direct", "dense-reuse", "dense-snapshot2"]
    ept_names = ["ept-direct", "ept-reuse", "ept-snapshot"]
    result = {
        "schema_version": 1, "date_utc": "2026-11-12", "unit": "microseconds unless field ends in _ms or _bytes",
        "source_base_commit": "2a4bb976dfd121d6deb1bd123c0138630c5f8dbd",
        "analysis_source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "implementation_commits": ["dcf120e raw L1 TSC fields", "3cb0e29 Cristian clock/probe samples and CPU support",
                                   "55c6577 snapshot vm_local_wrapper scope collection", "6d17d96 assembly-call timestamp placement"],
        "cleanup": {"evidence": str(LOGS / "09-final-cleanup.log"), "owned_labs_checked": 19,
                    "remaining_owned_qemu": 0, "remaining_owned_shm_backing": 0},
        "environment": {"server_label": "bud05", "hostname": os.uname().nodename,
                        "cpu": "Intel Xeon Gold 6548Y+", "host_kernel": os.uname().release,
                        "l1_kernel": "6.6.0-rc6-dcd-v3", "qemu": "10.2.2", "firecracker": "1.17.0",
                        "host_cpus": "32-63", "host_memory_node": 1, "network_backend": "socket",
                        "l1_vcpus_each": 2, "l1_memory_mib_each": 2048,
                        "maximum_lab_budget_mib": 8256, "build_jobs": 24},
        "asset_provenance": {"log": str(LOGS / "09-final-build-provenance.log"),
                             "firecracker": "Reused build07 binary, SHA256 0759cfbac12f7e2e5867a6f3d78925434aa43388730e0b0047e19fab7870d2f4; unchanged patch matches base commit",
                             "qemu": "Reused 10.2.2 build, byte-identical local copy and copied loader/libraries allocated on node 1; reverse patch check passed",
                             "l1_kernel": "Rebuilt on node 1 with JOBS=24; only existing DAX profile path changed; final KVM module reverse patch check and all final lab verifies passed",
                             "guest_kernel": "Locally built vmlinux from the same kernel build; per-run input/artifact hashes retained in run.json",
                             "shared_probe": "Original assets07 source hash matched base commit; rebuilt statically from the committed new probe for assets09 and assets09-final; final manifest source hash checked"},
        "profile_code_audit": {
            "kick_to_exit": {"start": "shared_probe.c:2261 stop_request_ns", "end": "shared_probe.c:2241 stopped_ns",
                             "clock": "same-L1 CLOCK_MONOTONIC_RAW", "note": "Includes KVM_RUN return/executor stop handling after actual exit; B path only"},
            "stop_ack": {"start": "shared_probe.c:2241", "end": "shared_probe.c:2272", "clock": "same-L1 CLOCK_MONOTONIC_RAW"},
            "executor_wake": {"start": "shared_probe.c:2425 before go/futex wake", "end": "shared_probe.c:2219 after futex wait",
                              "clock": "same-L1 CLOCK_MONOTONIC_RAW", "note": "B uses futex, not the shared-memory polling sleep"},
            "wake_to_run": {"start": "shared_probe.c:2219", "end": "shared_probe.c:2224 before ioctl", "clock": "same-L1 CLOCK_MONOTONIC_RAW"},
            "kernel_detach": {"start": "kvm-dax-state.patch:2717", "end": "kvm_dax_result after source DETACHED publication",
                              "clock": "same-L1 ktime_get_ns", "stages": ["0 validation + arch quiesce/MMU drain", "1 debugfs/list/PM observers", "2 MMU notifier unregister", "3 xarray retirement", "4 SRCU cleanup", "5 PID RCU drain", "6 mm/owner/generation/unlock"],
                              "stage_lines": [2835, 2845, 2851, 2857, 2860, 2866, 2876]},
            "kernel_attach": {"start": "kvm-dax-state.patch:2892", "end": "kvm-dax-state.patch:3030 attach_ns before result/copy_to_user",
                              "clock": "same-L1 ktime_get_ns", "stages": ["0 validation/PFN/owner claim", "1 KVM hardware/mm binding", "2 SRCU init", "3 MMU notifier", "4 vCPU bindings", "5 arch/EPT/timer rebind", "6 PM/list/FD", "7 profile init/owner/result"],
                              "stage_lines": [2952, 2965, 2976, 2981, 2994, 2999, 3016, 3032]},
            "resume_profile_run_to_entry": {"start": "kvm-dax-state.patch:2516 KVM_RUN dispatch", "end": "kvm-dax-state.patch:886 paired entry_ns before wrapper",
                                            "clock": "same-L1 ktime_get_ns", "note": "Existing local diagnostic ends before wrapper preparation; raw exact-boundary anchor is later inside wrapper"},
            "guest_ack": {"start": "shared_probe.c:2429 attached_ns after resume control return", "end": "shared_probe.c:2435 observed_ns", "clock": "same-L1 CLOCK_MONOTONIC", "note": "API return and actual entry can overlap"},
            "entry_to_guest_write": {"start": "kvm-dax-state.patch:884 kvm_read_l1_tsc paired sample", "end": "shared_probe.c:914 L2 ACK TSC",
                                     "clock": "same L2 virtual TSC domain, divided by virtual_tsc_khz; no cross-L1 subtraction"},
            "new_raw_boundary": {"start": "kvm-dax-state.patch:857 immediately after entry assembly returns", "end": "kvm-dax-state.patch:853 before first entry assembly call",
                                 "clock": "cross-L1 raw ordered TSC with Δ correction; assembly prologue/epilogue remains inside the called function"},
            "clock_sync": {"code": "shared_probe.c:1933", "method": "shared DAX Cristian, min RTT of 200 start + 200 end samples"}},
        "boundary": {"start": "source L1 ordered RDTSC immediately after __vmx_vcpu_run returns from the final L2 VM-exit",
                     "end": "destination L1 ordered RDTSC immediately before the first __vmx_vcpu_run call on the transferred vCPU",
                     "precision_limit": "C-level assembly-call boundary: __vmx_vcpu_run still contains register/VM-entry/exit assembly. This is not a hardware timestamp on the VMLAUNCH/VMRESUME instruction itself.",
                     "outside": "controller request→kick→exit and entry→guest ACK/observation reported separately; CPU guest HLT predates controller request",
                     "clock": "raw L1 TSC, 2.5 GHz, shared-DAX Cristian minimum RTT of 200 samples at each end; constant Δ=B−A correction uses average start/end offset",
                     "stats": "nearest-rank p95, median p50; 100 measured handoffs total, 50 per direction; pair averages n=50",
                     "fragvisor": "86 us mean/38 us register dump as reported in local 08 study; its exact published boundary is unknown, so exit→entry comparison remains an inference"},
        "direct": {name: direct(name) for name in direct_names}, "reuse_prepared": reuse(),
        "dense": {name: dense(name) for name in dense_names},
        "ept": {name: ept(name) for name in ept_names},
        "placement": placement_summary(direct_names + ["reuse128"] + dense_names + ept_names),
        "direct_shared_writes": {
            "serialized_cpu_vm_state_bytes": 0, "guest_ram_copy_bytes": 0, "ept_copy_bytes": 0,
            "token_mailbox_bytes": 136, "counter_mailbox_bytes": 8,
            "cpu_notification_store_bytes": 16, "linux_notification_store_bytes": 24,
            "linux_guest_epoch_store_bytes": 8, "linux_guest_ack_store_bytes": 8,
            "cpu_fixed_protocol_store_bytes_excluding_profiles_and_guest_activity": 160,
            "linux_fixed_protocol_store_bytes_including_epoch_ack_excluding_profiles_and_guest_activity": 184,
            "profile_reserved_footprint_bytes": 256, "extra_raw_last_exit_store_bytes_per_vm_exit": 8,
            "clock_sync_store_bytes_per_ping": 24, "clock_sync_pings_per_run": 400,
            "byte_count_method": "Semantic field store widths; not cache-line coherence traffic. Kernel object/header updates, profile writes and guest/telemetry writes are excluded from fixed protocol counts.",
            "not_counted_as_serialization": "In-place KVM/VMCS/EPT metadata, ownership/epoch, timer/PV-clock/TSС fields are updated. Total shared-memory stores depend on VM exits, EPT metadata and guest execution; no exact aggregate write-traffic count was measured. JSON telemetry/profile diagnostics are excluded from the fixed protocol count.",
            "code": ["shared_probe.c:1919 direct_control", "shared_probe.c:2144 token/counter publication and ACK", "shared_probe.c:2362 Linux epoch/publication", "kvm-dax-state.patch UAPI kvm_dax_handoff and in-place detach/attach paths"]},
        "superseded_or_failed": {"preliminary_wrapper_boundary": ["cpu50", "cpu0", "linux50", "linux0", "linux0-session2"],
                                 "dense-snapshot": "24 handoffs completed, collection rejected extra vm_local_wrapper profile scope; excluded and rerun as dense-snapshot2",
                                 "smoke_start": "isolated QEMU wrapper initially lacked BIOS search path; no benchmark ran; lab.py stop cleaned owned processes/backing"},
        "limitations": ["Nested L1→L2 VM-entry traverses L0; dax_nested_root_sync/shadow EPT02 refill affects times in all modes. These are not physical-host timings.",
                        "Shared memory is coherent; no noncoherent CXL/RDMA flush cost is represented.",
                        "TSC correction has RTT/2 uncertainty and a constant-offset assumption; start/end drift is reported per condition.",
                        "The FragVisor original timing boundary and raw measurements are unavailable.",
                        "Reuse-prepared exact exit→entry is unavailable; its separately labeled API stage sum is an approximation, not the same measured boundary.",
                        "Dense scan checks 64 bytes per 4 KiB page, not every byte. First-visit counts are n=1 per destination; steady-state numbers use 20 measured visits.",
                        "Direct CPU/Linux defaults remain spin=0, timer slack native. Native reuse/snapshot use preset spin=500, poll=50, timer slack native. Dense direct uses poll=0."]}
    (OUT / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for name, r in result["direct"].items():
        print(name, "one-way", r["one_way_us"], "directions", r["directions"], "offset_error_us", r["conservative_offset_error_us"])
    print("reuse proxy", result["reuse_prepared"]["closest_stage_sum_us"])
    for name, r in result["dense"].items():
        print(name, r["measured"])
    for name, r in result["ept"].items():
        print(name, r["rounds"])


if __name__ == "__main__":
    main()
