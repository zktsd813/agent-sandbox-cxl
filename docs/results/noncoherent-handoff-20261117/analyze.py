#!/usr/bin/env python3
"""Summarize stopped-page proofs and local phase clocks; never read live RAM."""
import json
import math
import re
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "implementation/virtual_machine"))
from verify_swe_handoff import verify_cache_receipts


def stats(values):
    values = sorted(values)
    return {"n": len(values), "min": values[0], "median": statistics.median(values),
            "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}


def load(path):
    return json.loads(path.read_text())


def clocks(report, receipt_dir):
    events, observers = {}, {}
    for receipt in report["receipts"]:
        if "vmm" not in receipt["kind"]:
            continue
        for line in (receipt_dir / receipt["file"]).read_text().splitlines():
            if "NONCOHERENT_JSON " in line:
                value = json.loads(line.split("NONCOHERENT_JSON ", 1)[1])
                events[value["event"], value["epoch"]] = value
            if "NONCOHERENT_ORACLE_JSON " in line:
                value = json.loads(line.split("NONCOHERENT_ORACLE_JSON ", 1)[1])
                observers[value["phase"], value["epoch"]] = value["ns"]
    return events, observers


def summarize(path, receipt_dir):
    report = load(path)
    assert report["result"] == "passed", path
    boundaries = report["dirty_oracle"]
    proof = verify_cache_receipts(report, path.parent)
    assert all(row["missing_pages"] == row["other_changed_pages"] == 0
               and row["coverage_verified"] for row in boundaries)
    exports = {row["epoch"]: row for row in boundaries if row["phase"] == 2}
    imports = {row["epoch"]: row for row in boundaries if row["phase"] == 3}
    terminal=set(exports)-set(imports)
    assert not terminal or (report['arguments'].get('reuse_test') and len(terminal)==1 and max(exports) in terminal)
    exports={e:r for e,r in exports.items() if e in imports}
    events, observers = clocks(report, receipt_dir)
    samples = []
    transitions=[t for t in report["transitions"] if t["kind"]=="direct"]
    for ordinal,(epoch, source) in enumerate(exports.items()):
        target = imports[epoch]
        event_s, event_t = events["export", epoch], events["import", epoch]
        row = {key: source[key] for key in
               ["epoch", "listed_pages", "changed_guest_pages", "unchanged_listed_pages",
                "kvm_only", "device_only", "both", "arena_bytes", "writeback_lines"]}
        row["unchanged_fraction"] = source["unchanged_listed_pages"] / source["listed_pages"]
        row.update({key: target[key] for key in
                    ["invalidate_lines", "full_invalidate_fallback", "inv_from", "inv_to"]})
        row["phase_ms"] = {
            "collect": source["collect_ns"] / 1e6,
            "ram_writeback": source["writeback_ns"] / 1e6,
            "log_publish": source["publish_ns"] / 1e6,
            "objects_writeback": source["kernel_writeback_ns"] / 1e6,
            "control_writeback": source["control_ns"] / 1e6,
            "log_read": target["log_read_ns"] / 1e6,
            "ram_invalidate": target["invalidate_ns"] / 1e6,
            "objects_invalidate": target["kernel_invalidate_ns"] / 1e6,
            "control_invalidate": target["control_ns"] / 1e6,
        }
        row["object_writeback_lines"] = source["kernel_writeback_lines"]
        row["object_invalidate_lines"] = target["kernel_invalidate_lines"]
        row["log_publish_lines"] = event_s["publish_lines"]
        row["log_read_lines"] = event_t["read_lines"]
        row["control_writeback_lines"] = event_s["control_lines"]
        row["control_invalidate_lines"] = event_t["control_lines"]
        wb = sum(row[key] for key in ["writeback_lines", "object_writeback_lines",
                                      "log_publish_lines", "control_writeback_lines"])
        inv = sum(row[key] for key in ["invalidate_lines", "object_invalidate_lines",
                                       "log_read_lines", "control_invalidate_lines"])
        row["cxl_cache_only_ms"] = {
            "ram_writeback_dirty": row["writeback_lines"] * 16 / 1e6,
            "ram_invalidate_absent": row["invalidate_lines"] * 6.25 / 1e6,
            "ram_writeback_absent": row["writeback_lines"] * 6.25 / 1e6,
            "ram_invalidate_clean": row["invalidate_lines"] * 2 / 1e6,
            "ram_large_dirty_wb_6_2ns":row["writeback_lines"]*6.2/1e6,
            "ram_large_dirty_wb_6_7ns":row["writeback_lines"]*6.7/1e6,
            "ram_large_dirty_wb_and_absent_inv_6_2ns":(row["writeback_lines"]*6.2+row["invalidate_lines"]*6.25)/1e6,
            "ram_large_dirty_wb_and_absent_inv_6_7ns":(row["writeback_lines"]*6.7+row["invalidate_lines"]*6.25)/1e6,
            "all_dirty_wb_absent_inv": (wb * 16 + inv * 6.25) / 1e6,
            "all_absent_wb_clean_inv": (wb * 6.25 + inv * 2) / 1e6,
        }
        row["oracle_gate_ms"] = sum(observers[phase, epoch] for phase in [2, 3]) / 1e6
        if ordinal < len(transitions):
            transition = transitions[ordinal]
            row["measured"] = transition.get("measured", True)
            row["direction"] = transition["source"] + "->" + transition["destination"]
            if "fast" in transition:
                row["pause_to_guest_ack_ms"] = transition["fast"]["guest_ack_ns"] / 1e6
                row["ack_excluding_explicit_oracle_gate_ms"] = (
                    row["pause_to_guest_ack_ms"] - row["oracle_gate_ms"])
        samples.append(row)
    assert len(exports) == len(imports) == len(samples)
    measured = [s for s in samples if s.get("measured", True)]
    result = {"report": str(path), "result": "passed", "handoffs": len(samples),
              "hash_boundaries": sum(r["hash_boundary"] for r in boundaries),
              "coverage_boundaries": len(boundaries), "missing_pages": 0,
              "unexplained_outside_pages": 0, "samples": samples,
              "independent_saved_bitmap_replay": proof,
              "measured": {"samples": len(measured)}}
    result["initial_publication"] = [{key: row[key] for key in
                                      ["pages", "epoch", "writeback_ns", "publish_ns", "writeback_lines"]}
                                     for row in boundaries if row["phase"] == 1]
    for key in ["listed_pages", "invalidate_lines", "arena_bytes", "oracle_gate_ms",
                "pause_to_guest_ack_ms", "ack_excluding_explicit_oracle_gate_ms"]:
        values = [s[key] for s in measured if key in s]
        if values:
            result["measured"][key] = stats(values)
    for key in ["phase_ms", "cxl_cache_only_ms"]:
        result["measured"][key] = {phase: stats([s[key][phase] for s in measured])
                                    for phase in measured[0][key]}
    result["measured"]["by_direction"] = {
        direction: {"n": sum(s.get("direction") == direction for s in measured),
                    "ram_writeback_ms": stats([s["phase_ms"]["ram_writeback"]
                                               for s in measured if s.get("direction") == direction]),
                    "ram_invalidate_ms": stats([s["phase_ms"]["ram_invalidate"]
                                                for s in measured if s.get("direction") == direction])}
        for direction in sorted({s["direction"] for s in measured if "direction" in s})}
    result['terminal_exports']=len(terminal)
    result['cleans']=[]
    for source in boundaries:
        if source['phase'] not in (4,6):continue
        row=dict(source)
        row['collect_ns']=events.get(('clean',source['epoch']),{}).get('collect_ns',row.get('collect_ns',0))
        row['mib']=row['listed_pages']/256
        row['cache_and_ledger_ms']=sum(row[k] for k in ('collect_ns','writeback_ns','publish_ns'))/1e6
        result['cleans'].append(row)
    if result['cleans']:
        result['clean_cost']={k:stats([r[k] for r in result['cleans']]) for k in ('listed_pages','mib','collect_ns','writeback_ns','publish_ns','cache_and_ledger_ms')}
    result['arguments']=report['arguments']
    result['source_reference']=report.get('source_reference')
    result['reuse']=report.get('reuse')
    result['race_pages']=report.get('race_pages')
    result['parked_audits']=report.get('parked_audits',report.get('audits',[]))
    used=[]
    for receipt in report['receipts']:
        if receipt['file'].endswith(('.txt','.bin')) and ('kernel' in receipt['kind'] or 'cache-state' in receipt['kind'] or 'arena' in receipt['kind']):
            text=(receipt_dir/receipt['file']).read_text()
            if 'vm_base=' in text:used.extend(int(x) for x in re.findall(r'\bused=(\d+)',text))
    result['arena_end_used_max']=max(used,default=max((r['arena_bytes'] for r in samples),default=0))
    result['preinvalidations']=[r for r in boundaries if r['phase']==5]
    result['waits']=report.get('waits',[])
    return result


def negative_tests(root):
    cases={
        'kernel-wb':('negative-kernel-wb',r'I/O error|Input/output error'),
        'ram-wb':('negative-ram-wb',r'injected writeback failure'),
        'invalidate':('negative-invalidate-retry',r'I/O error|Input/output error'),
        'epoch':('negative-epoch-retry',r'after retries'),
        'record':('negative-record-retry',r'missing/corrupt log record'),
        'overflow':('negative-overflow-retry',r'Out of memory \(os error 12\)')}
    results={}
    for tag,(name,pattern) in cases.items():
        directory=root/name
        if not (directory/'handoff.json').exists():results[tag]={'result':'pending'};continue
        trial=load(directory/'trial.json');report=load(directory/'handoff.json')
        base=Path(trial['run'])
        raw=next(base.glob('resident-bench-*/report.json')).parent
        texts={receipt['kind']:(raw/receipt['file']).read_text() for receipt in report['receipts']}
        text='\n'.join(texts.values());receiver=texts.get('final-B-vmm.log','')
        handoffs=[t for t in report['transitions']if t.get('kind')=='direct']
        reason=bool(re.search(pattern,text,re.I))
        resumed='VcpuEvent::Resume' in receiver
        ok=trial['result']==report['result']=='failed' and not handoffs and reason and trial['cleanup']['backing_removed']
        if tag in ('invalidate','epoch','record'):ok=ok and not resumed
        results[tag]=dict(result='passed'if ok else'failed',directory=str(directory),completed_handoffs=len(handoffs),
            reason_verified=reason,receiver_resumed=resumed,
            fatal_lines=[line for line in text.splitlines()if '[anonymous-instance:' in line and 'FATAL' in line][-8:])
    return results


def stage19(root):
    cases={}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():continue
        trial=directory/'trial.json'
        suite=directory/'report.json'
        if trial.exists():
            meta=load(trial)
            if meta['result']=='passed' and (directory/'handoff.json').exists():
                report=load(directory/'handoff.json')
                if report.get('dirty_oracle'):
                    cases[directory.name]=summarize(directory/'handoff.json',Path(meta['report']).parent)
                    if report.get('reuse'):
                        child=directory/report['reuse']['vm2_report']
                        if not child.exists():child=Path(meta['report']).parent/report['reuse']['vm2_report']
                        cases[directory.name+'/vm2']=summarize(child,child.parent)
        elif suite.exists() and directory.name.startswith('swe-'):
            meta=load(suite)
            if meta['result']!='passed': continue
            for trial in meta['trials']:
                path=directory/trial['report_file']
                cases[directory.name+'/'+trial['id']]=summarize(path,path.parent)
    controls={}
    for mode in ('off','on'):
        directory=root/f'final-control-{mode}'
        if not (directory/'trial.json').exists():continue
        trial=load(directory/'trial.json')
        if trial['result']!='passed':continue
        report=load(directory/'handoff.json')
        rows=[r for r in report['transitions'] if r.get('kind')=='direct']
        events,_=clocks(report,Path(trial['report']).parent)
        epochs=sorted(e for event,e in events if event=='export')
        assert len(rows)==len(epochs)
        selected=[(row,e)for row,e in zip(rows,epochs)if row.get('measured')]
        controls[mode]={'n':len(selected),'ack_ms':stats([r['fast']['guest_ack_ns']/1e6 for r,e in selected]),
            'phase_ms':{key:stats([events[event,e][key]/1e6 for r,e in selected]) for event,key in
                [('export','collect_ns'),('export','writeback_ns'),('export','publish_ns'),('import','log_read_ns'),('import','invalidate_ns')]}}
        clean_events = [value for (event, _), value in events.items() if event == 'clean']
        if clean_events:
            controls[mode]['clean_cost'] = {
                key: stats([value[key] / 1e6 for value in clean_events])
                for key in ('collect_ns', 'writeback_ns', 'publish_ns')}
            controls[mode]['clean_cost']['total_ms'] = stats([
                sum(value[key] for key in ('collect_ns', 'writeback_ns', 'publish_ns')) / 1e6
                for value in clean_events])
    selected_names = ['final-256-off', 'final-256-on-r2', 'final-paused',
                      'race-verified', 'smp-verified', 'mixed-complete',
                      'reuse-retire-ept', 'reuse-retire-ept/vm2']
    for mode in ('off', 'zero', 'one', 'recorded'):
        for fixture in ('gin-gonic__gin-3741', 'php-cs-fixer__php-cs-fixer-8331',
                        'micropython__micropython-13039'):
            selected_names.append(f'swe-verified-{mode}/{fixture}-direct-heavy-r0')
    selected_names.append('swe-verified-gin2/gin-gonic__gin-3741-direct-heavy-r0')
    selected = {name: cases[name] for name in selected_names if name in cases}
    negatives = negative_tests(root)
    final = dict(complete=len(selected) == len(selected_names),
                 missing_cases=sorted(set(selected_names) - set(selected)),
                 cases=list(selected),
                 handoffs=sum(c['handoffs'] for c in selected.values()),
                 cleans=sum(len(c['cleans']) for c in selected.values()),
                 hash_boundaries=sum(c['hash_boundaries'] for c in selected.values()),
                 missing_pages=0, unexplained_outside_pages=0)
    complete = final['complete'] and all(row['result'] == 'passed' for row in negatives.values())
    return dict(result='passed' if complete else 'incomplete',cases=cases,selected_final=final,
        oracle_off_controls=controls,negative_tests=negatives,total_handoffs=sum(c['handoffs'] for c in cases.values()),
        cleans=sum(len(c['cleans']) for c in cases.values()),
        preinvalidations=sum(len(c['preinvalidations']) for c in cases.values()),
        hash_boundaries=sum(c['hash_boundaries'] for c in cases.values()),
        missing_pages=0,unexplained_outside_pages=0)


def main():
    root = Path(sys.argv[1]).resolve()
    if "--stage19" in sys.argv:
        print(json.dumps(stage19(root),indent=2));return
    cases = {}
    for suite in ["nc-swe2", "nc-gin2"]:
        manifest = load(root / suite / "report.json")
        assert manifest["result"] == "passed"
        for trial in manifest["trials"]:
            path = root / suite / trial["report_file"]
            # The suite copies receipts alongside its copied report.
            cases[suite + "/" + trial["id"]] = summarize(path, path.parent)
    for name in ["nc-minimal", "nc-mixed", "nc-time-0", "nc-time-16", "nc-time-64", "nc-time-256"]:
        trial = load(root / name / "trial.json")
        assert trial["result"] == "passed" and trial["cleanup"]["backing_removed"]
        cases[name] = summarize(root / name / "handoff.json", Path(trial["report"]).parent)
    output = {"result": "passed", "source_commit": "3acb0f835f6847d251825c3b59deb6bfe6455f91",
              "cases": cases, "total_handoffs": sum(c["handoffs"] for c in cases.values()),
              "hash_boundaries": sum(c["hash_boundaries"] for c in cases.values()),
              "coverage_boundaries": sum(c["coverage_boundaries"] for c in cases.values()),
              "missing_pages": 0, "unexplained_outside_pages": 0,
              "interpretation": "Local phase durations include PFNMAP faults and checks. CXL estimates cover cache lines only. ACK includes oracle; subtracting its gates does not remove cache perturbation."}
    controls = {}
    for size in [0, 16, 64, 256]:
        arms = {}
        for mode in ["baseline", "active"]:
            directory = root / f"nc-control-{mode}-{size}"
            trial, report = load(directory / "trial.json"), load(directory / "handoff.json")
            assert trial["result"] == report["result"] == "passed"
            assert trial["cleanup"]["backing_removed"] and not report["arguments"]["dirty_oracle"]
            samples = [r for r in report["transitions"] if r.get("measured")]
            assert len(samples) == 200
            arms[mode] = {"report": str(directory / "handoff.json"),
                          "pause_to_guest_ack_ms": stats([r["fast"]["guest_ack_ns"] / 1e6 for r in samples]),
                          "by_direction_ms": {source: stats([r["fast"]["guest_ack_ns"] / 1e6
                                                             for r in samples if r["source"] == source])
                                              for source in ["A", "B"]}}
            if mode == "active":
                events, observers = clocks(report, Path(trial["report"]).parent)
                assert not observers
                selected = [r["destination_epoch"] + 1 for r in samples]
                arms[mode]["phase_ms"] = {
                    key: stats([events[event, epoch][key] / 1e6 for epoch in selected])
                    for event, key in [("export", "collect_ns"), ("export", "writeback_ns"),
                                       ("export", "publish_ns"), ("import", "log_read_ns"),
                                       ("import", "invalidate_ns")]}
        controls[str(size)] = dict(arms, median_increment_ms=(
            arms["active"]["pause_to_guest_ack_ms"]["median"] -
            arms["baseline"]["pause_to_guest_ack_ms"]["median"]))
    output["oracle_off_timing_controls"] = controls
    output["timing_control_limitations"] = "Fixed baseline then active order, one lab per arm; 100 measured samples each direction after 10 warmups. No stopped-page hash in these auxiliary controls. Main correctness/time cohorts retain the oracle."
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
