#!/usr/bin/env python3
"""Summarize saved run21 evidence. No VM, device, or network access."""
import argparse
import ast
from collections import defaultdict
import difflib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


def distribution(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return dict(n=len(values), median=statistics.median(values),
                p95=values[math.ceil(.95 * len(values)) - 1],
                p99=values[math.ceil(.99 * len(values)) - 1], max=values[-1])


def events(folder, marker):
    files = sorted(folder.glob("?-vmm.log"))
    if not files:
        files = sorted(folder.glob("*final-*-vmm.log.bin"))
    return [json.loads(line.split(marker, 1)[1]) for file in files
            for line in file.read_text().splitlines() if marker in line]


def summarize(folder):
    report = json.loads((folder / "report.json").read_text())
    hops = [h for h in report["transitions"] if h["kind"] in ("direct", "ept-adopt")]
    measured = [h for h in hops if h.get("measured", False)]
    oracle = report.get("oracle", report.get("dirty_oracle", []))
    if not oracle and (folder / "dirty-oracle/summary.json").exists():
        oracle = json.loads((folder / "dirty-oracle/summary.json").read_text())
    result = dict(result=report["result"], raw=str(folder), hops=len(hops),
        measured_hops=len(measured), arguments=report["arguments"],
        source_reference=report.get("source_reference"),
        inputs=report["config"].get("prepared", {}).get("inputs"),
        missing_pages=sum(r.get("missing_pages", 0) for r in oracle) if oracle else None,
        other_changed_pages=sum(r.get("other_changed_pages", 0) for r in oracle) if oracle else None,
        oracle_boundaries=len(oracle),
        adopt_hash_checks=sum(r["phase"] == 3 and r.get("ept_hash_unchanged", False) for r in oracle),
        released_source_exit=report.get("released_source_exit"),
        independent_trace_verification=report.get("independent_trace_verification"),
        host=report.get("measurement_host"), error=report.get("error"),
        oracle_error=report.get("oracle_error"),
        payload_faults=[h["payload_ept_violations"] for h in hops if "payload_ept_violations" in h],
        measured_payload_faults=[h["payload_ept_violations"] for h in measured if "payload_ept_violations" in h],
        packet_bytes=distribution([h.get("packet_bytes", h.get("fast", {}).get("state_bytes", 0)) for h in hops]),
        pause_to_ack_ms=distribution([h["fast"]["guest_ack_ns"] / 1e6 for h in measured if "fast" in h]))
    if (folder / "verification.json").exists():
        result["external_verification"] = json.loads((folder / "verification.json").read_text())
    # Join per-role log order to transitions, including warm-up before filtering.
    timings = events(folder, "RESTORE_TIMING ")
    result["stages_ms"] = {}
    for scope in sorted({t["scope"] for t in timings}):
        rows = [t for t in timings if t["scope"] == scope]
        # Logs are grouped by role. Each role exports/imports once per round.
        warmup = int(report["arguments"].get("warmup", 0))
        # Strip each role's warm-up separately, rather than a grouped prefix.
        selected = []
        logfiles = sorted(folder.glob("?-vmm.log")) or sorted(folder.glob("*final-*-vmm.log.bin"))
        for file in logfiles:
            role_rows = [json.loads(line.split("RESTORE_TIMING ", 1)[1])
                         for line in file.read_text().splitlines() if "RESTORE_TIMING " in line]
            role = re.search(r"(?:^|final-)([AB])-", file.name)[1]
            role_rows = [r for r in role_rows if r["scope"] == scope]
            direction = "source" if scope in ("ept_export", "direct_export") else "destination"
            expected = sum(h[direction] == role for h in hops)
            selected.extend(role_rows[:expected][warmup:])
        if not measured:
            selected = rows
        names = sorted({k for r in selected for k in r["stages_us"]})
        result["stages_ms"][scope] = {k: distribution([r["stages_us"][k] / 1000 for r in selected if k in r["stages_us"]]) for k in names}
        result["stages_ms"][scope]["total"] = distribution([r["total_us"] / 1000 for r in selected])
    result["table_pages"] = distribution([r["bytes"] // 4096 for r in events(folder, "EPT_CACHE_JSON ") if r["invalidate"]])
    commands = report.get("trace_commands", report.get("commands", []))
    result["heavy_commands"] = []
    for hop in hops:
        if "before_command" not in hop:
            continue
        index = hop["before_command"]
        command = next(c for c in commands if c.get("operation") == "run" and c["arguments"] == [index])
        receipt = command["receipt"]
        result["heavy_commands"].append(dict(index=index, rc=receipt["rc"],
            guest_s=receipt["end_s"] - receipt["start_s"],
            host_s=(command["host_receive_ns"] - command["host_send_ns"]) / 1e9))
    return result


def c_function(text, name):
    # Mask comments and literals, preserving line positions and braces in code.
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                    lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)
    match = re.search(r"(?m)^[A-Za-z_][\w\s*]*\b" + name + r"\([^;{]*\)\s*\{", masked)
    if match is None:
        raise ValueError(name)
    depth = 1
    end = match.end()
    while depth:
        depth += (masked[end] == "{") - (masked[end] == "}")
        end += 1
    return text[match.start():end]


def source_lines(raw, repo):
    result = {}
    for category, tree, baseline in (("kvm", "linux", "baseline-linux"),
                                      ("firecracker", "firecracker", "baseline-fc")):
        files = {}
        for original in sorted((raw / baseline).rglob("*")):
            if not original.is_file():
                continue
            relative = original.relative_to(raw / baseline)
            current = raw / tree / relative
            changes = [o for o in difflib.SequenceMatcher(None, original.read_text().splitlines(), current.read_text().splitlines(), autojunk=False).get_opcodes() if o[0] != "equal"]
            if changes:
                files[str(relative)] = dict(added=sum(o[4]-o[3] for o in changes), removed=sum(o[2]-o[1] for o in changes))
        if category == "firecracker":
            files["src/vmm/src/ept_adopt.rs"] = dict(added=len((raw/tree/"src/vmm/src/ept_adopt.rs").read_text().splitlines()), removed=0)
        result[category] = dict(files=files, added=sum(f["added"] for f in files.values()), removed=sum(f["removed"] for f in files.values()))
    paths = ["ept_adopt.py", "dirty_oracle.py", "handoff_bench.py", "lab.py", "resident_control.c", "resident_fast.py", "shared_probe.c", "verify_swe_handoff.py"]
    diff = subprocess.check_output(["git", "-C", str(repo), "diff", "--numstat", "8de3e8e", "--", *["implementation/virtual_machine/"+p for p in paths]], text=True)
    files = {line.split("\t")[2]: dict(added=int(line.split("\t")[0]), removed=int(line.split("\t")[1])) for line in diff.splitlines()}
    result["test_tools"] = dict(files=files, added=sum(f["added"] for f in files.values()), removed=sum(f["removed"] for f in files.values()))
    result["analysis_tool_lines"] = len(Path(__file__).read_text().splitlines())
    # Conservative direct-only core count. Shared mapper/noncoherent/reuse
    # code and dispersed allocation/audit hooks are intentionally excluded.
    direct = {"virt/kvm/dax_pool.c": {"whole_file": len((raw/"baseline-linux/virt/kvm/dax_pool.c").read_text().splitlines())}}
    functions = {
        "virt/kvm/kvm_main.c": ["dax_profile_step", "kvm_dax_access", "kvm_dax_data_map", "kvm_dax_result", "kvm_dax_vm_ioctl_locked", "kvm_dax_vm_ioctl", "kvm_dax_attach"],
        "virt/kvm/eventfd.c": ["kvm_dax_irqfd_quiesce"],
        "arch/x86/kvm/mmu/mmu.c": ["kvm_mmu_dax_rebind", "kvm_dax_nested_root_sync", "kvm_mmu_dax_retire", "kvm_mmu_dax_ept_rebind", "kvm_mmu_dax_invalidate"],
        "arch/x86/kvm/x86.c": ["kvm_arch_dax_rebase_tsc", "kvm_arch_dax_handoff", "kvm_arch_dax_vm_handoff", "kvm_arch_dax_vm_resume"],
        "arch/x86/kvm/vmx/vmx.c": ["vmx_dax_handoff"],
    }
    unchanged = {}
    for file, names in functions.items():
        old = (raw / "baseline-linux" / file).read_text()
        new = (raw / "linux" / file).read_text()
        direct[file] = {name: len(c_function(old, name).splitlines()) for name in names}
        unchanged[file] = {name: c_function(old, name) == c_function(new, name) for name in names}
    result["direct_kernel_core_lower_bound"] = dict(files=direct, lines=sum(sum(v.values()) for v in direct.values()), unchanged_functions=unchanged)
    result["direct_firecracker_core"] = dict(file="src/vmm/src/direct.rs", lines=len((raw/"baseline-fc/src/vmm/src/direct.rs").read_text().splitlines()), unchanged=(raw/"baseline-fc/src/vmm/src/direct.rs").read_bytes() == (raw/"firecracker/src/vmm/src/direct.rs").read_bytes())
    tools = {}
    for file, names in {"handoff_bench.py": ["configure_direct", "direct_handoff", "resident_handoff", "prepare_resident_target", "resident_measure"], "resident_fast.py": ["prepare", "retire_arena", "audit_parked", "reuse_arena"]}.items():
        text = subprocess.check_output(["git", "-C", str(repo), "show", "8de3e8e:implementation/virtual_machine/"+file], text=True)
        nodes = {n.name: n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)}
        tools[file] = {name: nodes[name].end_lineno - nodes[name].lineno + 1 for name in names if name in nodes}
    result["direct_test_core_lower_bound"] = dict(files=tools, lines=sum(sum(v.values()) for v in tools.values()))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=Path("implementation/virtual_machine/.local/run21"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("summary.json"))
    args = parser.parse_args()
    raw = args.raw.resolve()
    trials = {p.parent.name: summarize(p.parent) for p in sorted(raw.glob("*/report.json"))}
    result = dict(schema="ept-adopt-results-v1", raw=str(raw), trials=trials,
                  source_lines=source_lines(raw, Path(__file__).resolve().parents[3]))
    result["latency_abab"] = {}
    for pair, other in (("direct", "direct"), ("reuse", "reuse-prepared")):
        blocks = []
        pooled = defaultdict(list)
        for arm, mode in enumerate(("ept-adopt", other, "ept-adopt", other)):
            tag = f"lat-{pair}-{arm}"
            if tag not in trials or trials[tag]["result"] != "passed":
                continue
            report = json.loads((raw/tag/"report.json").read_text())
            values = [h["fast"]["guest_ack_ns"] / 1e6 for h in report["transitions"] if h.get("measured")]
            pooled[mode].extend(values)
            blocks.append(dict(arm=arm, mode=mode, trial=tag, pause_to_ack_ms=distribution(values), host=trials[tag]["host"]))
        result["latency_abab"][pair] = dict(blocks=blocks, pooled_ms={mode: distribution(v) for mode, v in pooled.items()})
    result["heavy_abab"] = {}
    for pair, other in (("direct", "direct"), ("reuse", "reuse-prepared")):
        groups = defaultdict(list)
        for tag, trial in trials.items():
            match = re.fullmatch(r"heavy-"+pair+r"-([0-3])-\d+-([\w-]+)", tag)
            if not match or trial["result"] != "passed":
                continue
            mode = "ept-adopt" if int(match[1]) % 2 == 0 else other
            for command in trial["heavy_commands"]:
                groups[(mode, match[2], command["index"])].append(command)
        result["heavy_abab"][pair] = [dict(mode=m, fixture=f, command=i,
            guest_s=distribution([v["guest_s"] for v in values]),
            host_s=distribution([v["host_s"] for v in values]))
            for (m, f, i), values in sorted(groups.items())]
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
