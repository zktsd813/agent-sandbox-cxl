#!/usr/bin/env python3
"""Independent raw replay of the separate224MiB host fault diagnostic."""
import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re
import statistics

from verify_pool_discard import Audit, PAGE, MIB, PFN_MASK, file_sha, require
from verify_pool_discard_protocol import trace_stats


def audit(path, manifest_path):
    original=json.loads(path.read_text());manifest=json.loads(manifest_path.read_text())
    require(original["result"]=="passed", "diagnostic incomplete")
    # The narrow diagnostic report did not capture a complete benchmark config.
    # Supply its external, hash-bound fixture manifest to shared raw readers;
    # still check every observed process field and boot API below.
    report=dict(original,fixture_manifest=manifest,restore_mappings=[])
    report["configuration"]={"anon_mib":224,"expected_binary_sha256":manifest["comparison"]["patched"]["sha256"],"env_by_vm":{}}
    e=Audit(report,path.parent)
    env=dict(re.findall(r"^([A-Z_][A-Z_0-9]*)=([^\r\n]*)$",
                        e.text(report["before"]["environment_file"]).replace("\r",""),re.M))
    require(env["FC_POOL_VM_ID"]=="1" and env["FC_POOL_RUN_EPOCH"]=="0" and
            env["FC_DAX_RAM"]=="/dev/dax0.0" and env["FC_DAX_SNAPSHOT"]=="1" and
            env["FC_POOL_IMAGE_ID"]==manifest["pool"]["image"]["sha256"] and
            "FC_POOL_DISCARD" not in env, "diagnostic runtime mode")
    report["configuration"]["env_by_vm"]["1:0"]=env
    require(len(report["boots"])==1 and report["boots"][0]["components"]["/machine-config"]==
            {"vcpu_count":2,"mem_size_mib":512}, "diagnostic boot fixture")
    for api in report["api_timings"]:
        raw=e.record(api["file"],"LAB_API_JSON ")
        require(raw==api["response"] and raw["status"]==204 and
                json.dumps(api["request"]) in e.console_command(api["file"]), "diagnostic raw API binding")
    identities=[]
    for command in report["commands"]:
        value=e.command(command)
        identities.append(tuple(value[k] for k in ("boot_id","pid","cookie","generation","private_hash","upper_hash")))
    require(len(set(identities))==1, "private guest identity changed")
    before=e.scan(report["before"],0)
    canonical=e.canonical(report["before"],before,0)
    require(len(canonical)==160*MIB//PAGE, "diagnostic source not entirely canonical")
    hot=e.command(report["hot"])
    require(hot["op"]=="check" and hot["block_read_ios"]==0, "pre-profile guest cache not hot")
    profile=report["profile"];operation=e.command(profile["operation"])
    require(operation["op"]=="recycle" and operation["churn_bytes"]==224*MIB and
            operation["checked_churn_words"]==224*MIB//8 and operation["resident_after"]==0,
            "224MiB complete recycle oracle")
    after=report["after"];e.mapping(after,0)
    require(not after["current_file_cache"], "post-recycle rows incorrectly called live file cache")
    pause=e.paused(after,0,after["maps_file"])
    require(e.order[profile["operation"]["file"]] < pause, "PFNs scanned before reuse")
    after_rows=e.pfn_rows(after["pfns_file"])
    require([row[:3] for row in after_rows]==[row[:3] for row in before], "old GPA scan correspondence")
    summary=e.record(after["summary_file"],"POOL_COMPARE_PFN_JSON ")
    require(summary==after["summary"] and summary["all_checked"] and summary["rows"]==len(before) and
            summary["pid"]==after["pid"] and summary["process_start_time"]==after["process_start_time"], "PFN summary")
    changed=sum(a[3]!=b[3] for a,b in zip(before,after_rows))
    require(changed==len(before)==report["original_cache_gpas_with_changed_pfns"], "original shared PFNs did not all split")
    group=profile["group"];require(re.fullmatch(r"pcow[0-9a-f]{8}",group), "trace group")
    tids={int(m[1]) for line in e.text(profile["setup_file"]).splitlines()
          if (m:=re.fullmatch(r"PROFILE_THREAD ([1-9]\d*)",line))}
    require(len(tids)==2, "vCPU thread identities")
    trace=e.text(profile["files"]["trace"]);stats=e.text(profile["files"]["stats"])
    headers=re.findall(r"entries-in-buffer/entries-written:\s*(\d+)/(\d+)\s+#P:(\d+)",trace)
    require(len(headers)==1 and int(headers[0][2])==4, "four-CPU trace header")
    sections=re.split(r"(?m)^/sys/kernel/tracing/instances/"+group+r"/per_cpu/cpu(\d+)/stats\r?$",stats)
    require(len(sections)==9 and not sections[0].strip(), "complete four-CPU stats blocks")
    cpu_stats={}
    for cpu,body in zip(sections[1::2],sections[2::2]):
        require(int(cpu) not in cpu_stats, "duplicate CPU stats")
        cpu_stats[int(cpu)]=trace_stats(body)
    require(set(cpu_stats)=={0,1,2,3}, "missing CPU stats")
    functions={"fault":"__handle_mm_fault","wp":"do_wp_page","mmu":"kvm_mmu_notifier_invalidate_range_start","tlb":"kvm_flush_remote_tlbs"}
    pattern=re.compile(r"-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+\.\d+):\s+(?:"+group+r":)?"+group+r"_(fault|wp|mmu|tlb)_(enter|exit):")
    pending=defaultdict(list);durations=defaultdict(list);active=False;finished=False;events=0;markers=0
    for line in trace.splitlines():
        if "tracing_mark_write:" in line and group+" BEGIN" in line:
            require(not active and not finished, "profile start ordering");active=True;markers+=1;continue
        if "tracing_mark_write:" in line and group+" END" in line:
            require(active and not any(pending.values()), "profile ends during a traced call");active=False;finished=True;markers+=1;continue
        if group+"_" not in line: continue
        match=pattern.search(line);require(match and active, "malformed/outside-window trace event")
        tid,cpu,stamp,key,phase=match.groups();tid,cpu=int(tid),int(cpu)
        require(tid in tids and cpu in cpu_stats, "event thread/CPU identity")
        ns=int(Decimal(stamp)*10**9);stack=pending[(tid,key)];events+=1
        if phase=="enter":stack.append(ns)
        else:
            require(stack, "return without entry");begin=stack.pop();require(ns>=begin,"clock regression");durations[key].append(ns-begin)
    require(finished and markers==2 and not any(pending.values()), "incomplete trace interval")
    count=sum(row["entries"] for row in cpu_stats.values())
    require(count==events+markers==int(headers[0][0])==int(headers[0][1]), "trace event/header/per-CPU count mismatch")
    probes={row[0]:(int(row[1]),int(row[2])) for line in e.text(profile["files"]["profile"]).splitlines()
            if (row:=line.split()) and row[0].startswith(group+"_")}
    require(set(probes)=={group+"_"+key+"_"+phase for key in functions for phase in ("enter","exit")}, "probe profile identity")
    calculated={}
    for key,symbol in functions.items():
        values=durations[key]
        require(len(values)==40960, "diagnostic call coverage")
        for phase in ("enter","exit"):
            hits,misses=probes[group+"_"+key+"_"+phase]
            require(hits>=len(values) and misses==0, "probe hits/missed instances")
        calculated[key]={"symbol":symbol,"calls":len(values),"sum_ns":sum(values),
                         "median_ns":statistics.median(values),"maximum_ns":max(values)}
    require(calculated==profile["analysis"]["functions"] and sorted(tids)==profile["analysis"]["threads"], "published trace analysis differs from raw calls")
    check=report["canonical_check"];canonical_check=e.record(check["file"],"POOL_DAX_COPY_JSON ")
    require(canonical_check==check["receipt"] and canonical_check["result"]=="passed" and canonical_check["full_compare"] and
            canonical_check["bytes"]==manifest["pool"]["image"]["bytes"] and canonical_check["operation"]=="verify" and
            check["image"]["sha256"]==manifest["pool"]["image"]["sha256"], "immutable canonical changed")
    sources={f.name:file_sha(f) for f in (path.parent/"measured-sources").iterdir() if f.is_file()}
    return {"result":"passed","schema":"pool-cow-diagnostic-independent-audit-v1","report_sha256":file_sha(path),
            "fixture_manifest_sha256":file_sha(manifest_path),"runtime_sha256":report["configuration"]["expected_binary_sha256"],
            "measured_source_hashes":sources,"raw_receipts":len(e.blobs),"cpu_stats":cpu_stats,"trace_entries":count,
            "original_cache_pages_with_changed_pfns":changed,"functions":calculated,"guest_aggregate_ns":operation["elapsed_ns"],
            "unattributed_difference_ns":operation["elapsed_ns"]-calculated["fault"]["sum_ns"],
            "scope":"Separate instrumented224MiB aggregate. Function durations include nesting/preemption/probe overhead, and the host trace window spans the serial round trip. The arithmetic difference is unattributed, not a measured cause. No pure copy latency claim.",
            "provenance_limit":"Diagnostic archives four measured source files, not every imported Python module. VMM hash is independently observed in /proc/exe; fixture manifest supplied separately and hash-bound here.",
            "verifier_sources":{name:file_sha(Path(__file__).with_name(name)) for name in
                                ("verify_pool_cow_diagnostic.py","verify_pool_discard.py","verify_pool_discard_protocol.py","verify_pool_compare.py")}}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("report",type=Path);p.add_argument("--manifest",type=Path,required=True);p.add_argument("--out",type=Path,required=True);a=p.parse_args()
    result=audit(a.report,a.manifest);data=json.dumps(result,indent=2)+"\n";a.out.write_text(data);print(data,end="")


if __name__=="__main__":main()
