#!/usr/bin/env python3
"""Independently compare SWE outputs and summarize raw guest CPU/ACK evidence."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import tarfile

from experiment_sources import verify_sources
from verify_swe_handoff import verify


def need(ok, message):
    if not ok:
        raise ValueError(message)


def reference(path):
    return {'path':str(path.resolve()), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def summary(values):
    values = sorted(values)
    return dict(n=len(values), p50=statistics.median(values), p95=values[math.ceil(.95*len(values))-1],
                mean=statistics.mean(values), min=values[0], max=values[-1])


def cpu_ticks(data):
    return {line.split()[0]:list(map(int,line.split()[1:9])) for line in data.decode().splitlines()}


def cpu_delta(before, after, wall=None):
    need(before.keys()==after.keys(), 'CPU topology changed')
    delta = {cpu:[b-a for a,b in zip(before[cpu],after[cpu])] for cpu in before}
    need(all(min(row)>=0 for row in delta.values()), 'CPU counters went backwards')
    # x86 Linux USER_HZ=100; exclude idle, iowait and steal from execution time.
    busy = {cpu:sum(row[i] for i in (0,1,2,5,6))/100 for cpu,row in delta.items()}
    accounted = statistics.mean(sum(row)/100 for row in delta.values())
    elapsed = wall if wall is not None else accounted
    need(elapsed>0, 'Empty CPU interval')
    return {'wall_s':wall, 'accounted_elapsed_s':accounted, 'per_cpu_busy_s':busy,
            'working_cpus':sum(v>=max(.01,elapsed*.01) for v in busy.values()),
            'average_executing_cores':sum(busy.values())/elapsed}


def swe_report(path):
    result = verify(path)
    report = json.loads(path.read_text())
    with tarfile.open(path.parent/report['guest_archive']) as archive:
        def ticks(name):
            return cpu_ticks(archive.extractfile('results/'+name).read())
        tests = {}
        for test in result['tests']:
            index = test['index']
            command = report['commands'][index]
            tests[str(index)] = cpu_delta(ticks(f'{index:03d}.cpu-before'), ticks(f'{index:03d}.cpu-after'),
                                         command['guest_duration_s'])
            if 'inflight' in command:
                names = [f'{index:03d}.cpu-before', *[f"{index:03d}.cpu-{o['receipt']['label']}"
                         for o in command['inflight']['observations']], f'{index:03d}.cpu-after']
                tests[str(index)]['intervals'] = [cpu_delta(ticks(a),ticks(b)) for a,b in zip(names,names[1:])]
        build = report.get('parallel_build')
        build_cpu = None
        if build:
            end = build['ended']['receipt']
            names = ['parallel-build.cpu-before', *['parallel-build.cpu-'+o['receipt']['label']
                     for o in build['observations']], 'parallel-build.cpu-after']
            build_cpu = cpu_delta(ticks(names[0]),ticks(names[-1]),end['end_s']-end['start_s'])
            build_cpu['intervals'] = [cpu_delta(ticks(a),ticks(b)) for a,b in zip(names,names[1:])]
            build_cpu['handoffs'] = len(build['hops'])
            build_cpu['command'] = build['command']
    return {'raw':reference(path), 'verification':result, 'go_test_cpu':tests, 'parallel_build_cpu':build_cpu}


def resident_report(path):
    trial = json.loads(path.read_text())
    need(trial['result']=='passed' and trial['exclusive_quiet'] and trial['cleanup']['backing_removed'],
         'Resident trial failed, lacks quiet lock, or leaked backing')
    raw = Path(trial['report'])
    report = json.loads(raw.read_text())
    verify_sources(report,raw.parent)
    for receipt in report['receipts']:
        need(hashlib.sha256((raw.parent/receipt['file']).read_bytes()).hexdigest()==receipt['sha256'],
             'Resident raw receipt checksum changed')
    need(report['result']=='passed' and set(report['resident_processes'])=={'A','B'}, 'Missing Firecracker pair')
    for evidence in report['resident_processes'].values():
        need('/lab/firecracker' in (raw.parent/evidence['file']).read_text(), 'Endpoint is not Firecracker')
    rows = [row for row in report['transitions'] if row.get('kind')=='direct']
    need(len(rows)==2*(report['arguments']['warmup']+report['arguments']['iterations']), 'Handoff sample count')
    for i,row in enumerate(rows):
        text = (raw.parent/row['fast_file']).read_text()
        receipts = [json.loads(line.removeprefix('RESIDENT_FAST_JSON ')) for line in text.splitlines()
                    if line.startswith('RESIDENT_FAST_JSON ')]
        need(receipts==[row['fast']], 'Raw handoff reply changed')
        ack = row['fast']['ack']
        need(ack['epoch']==i+1==ack['verified'] and row['fast']['guest_ack_ns']>0, 'Guest ACK gap')
    selected = [row for row in rows if row['measured']]
    result = {'trial':reference(path),'raw':reference(raw),'vcpus':trial['vcpus'],
              'state':report['arguments']['resident_bench'],'profile':report['arguments']['profile'],
              'resume_profile':report['arguments']['resume_profile'],'directions':{},'cleanup':trial['cleanup']}
    for source in ('A','B'):
        group = [row for row in selected if row['source']==source]
        need(len(group)==report['arguments']['iterations'], 'Direction count')
        stages = {'pause_to_guest_ack_ms':[r['fast']['guest_ack_ns']/1e6 for r in group],
                  'send_api_ms':[r['fast']['apis']['send']['duration_us']/1000 for r in group],
                  'receive_api_ms':[r['fast']['apis']['receive']['duration_us']/1000 for r in group],
                  'pause_to_resume_response_ms':[r['fast']['pause_to_resume_response_ns']/1e6 for r in group],
                  'resume_response_to_guest_ack_ms':[(r['fast']['guest_ack_ns']-r['fast']['pause_to_resume_response_ns'])/1e6 for r in group]}
        if result['profile']:
            for side in ('detach','attach'):
                for i in range(8):
                    stages[f'{side}_{i}_ms'] = [r['fast']['ack'][side+'_ns'][i]/1e6 for r in group]
        if result['resume_profile']:
            need(all(r['fast']['ack']['entry_ns']>=r['fast']['ack']['run_ns']
                     and r['fast']['ack']['ack_guest_tsc']>=r['fast']['ack']['entry_guest_tsc'] for r in group),
                 'Invalid entry/guest timestamp ordering')
            stages['first_run_to_entry_ms'] = [(r['fast']['ack']['entry_ns']-r['fast']['ack']['run_ns'])/1e6 for r in group]
            def guest_interval(row, end, begin):
                ack=row['fast']['ack']
                return (ack[end]-ack[begin])/ack['tsc_khz']
            new_boundary='epoch_detect_guest_tsc' in group[0]['fast']['ack']
            stages['entry_to_guest_ack_ms' if new_boundary else 'entry_to_epoch_detection_ms'] = [
                guest_interval(r,'ack_guest_tsc','entry_guest_tsc') for r in group]
            if new_boundary and result['state']=='busy':
                for name,end,begin in [('entry_to_epoch_detection_ms','epoch_detect_guest_tsc','entry_guest_tsc'),
                     ('guest_wait_writers_ms','writers_ready_guest_tsc','epoch_detect_guest_tsc'),
                     ('guest_interrupt_accounting_ms','ack_guest_tsc','writers_ready_guest_tsc')]:
                    stages[name]=[guest_interval(r,end,begin) for r in group]
                need(all(min(stages[name])>=0 for name in ('entry_to_epoch_detection_ms','guest_wait_writers_ms',
                    'guest_interrupt_accounting_ms')), 'Guest ACK stages went backwards')
        result['directions'][source+('→B' if source=='A' else '→A')] = {k:summary(v) for k,v in stages.items()}
    result['smp_validation'] = report.get('smp_validation')
    return result


def native_report(path):
    trial=json.loads(path.read_text())
    need(trial['result']=='passed' and trial['exclusive_quiet'] and trial['cleanup']['backing_removed'],
         'Native diagnosis failed or leaked backing')
    raw=Path(trial['report']); report=json.loads(raw.read_text())
    need(report['result']=='passed' and report['profile'] and report['resume_profile'], 'Missing native profiles')
    for role,expected in report['samples'].items():
        lines=(raw.parent/(role+'.log')).read_text().splitlines()
        actual=[json.loads(line.removeprefix('LINUX_DIRECT_JSON ')) for line in lines if line.startswith('LINUX_DIRECT_JSON ')]
        need(actual==expected, 'Native raw samples changed')
    samples=sum(report['samples'].values(),[])
    sources=sorted((x for x in samples if x['event']=='source'),key=lambda x:x['round'])
    destinations=sorted((x for x in samples if x['event']=='destination'),key=lambda x:x['round'])
    rounds=report['iterations']+report['warmup']
    need(len(sources)==len(destinations)==rounds, 'Native handoff count')
    for i,(source,target) in enumerate(zip(sources,destinations)):
        need(source['round']==target['round']==i and target['sequence']>source['sequence']
             and target['role']!=source['role'], 'Native execution continuity')
    for cpu in range(trial['vcpus']):
        history=sorted((x for x in samples if x['event']=='smp' and x['cpu']==cpu),key=lambda x:x['round'])
        need(len(history)==rounds and all(x['round']==i and x['ack']==i+1 and
             not any(x[k] for k in ('errors','clock_errors','tsc_errors')) for i,x in enumerate(history)),
             'Native per-CPU continuity')
    result={'trial':reference(path),'raw':reference(raw),'vcpus':trial['vcpus'],'directions':{},'cleanup':trial['cleanup']}
    for role in ('A','B'):
        pairs=[(a,b) for a,b in zip(sources,destinations) if a['role']==role and a['round']>=report['warmup']]
        need(len(pairs)==100, 'Native direction sample count')
        stages={
            'pause_to_guest_ack_ms':[a['guest_observed_us']/1000 for a,b in pairs],
            'pause_to_remote_controller_ack_ms':[a['handoff_us']/1000 for a,b in pairs],
            'kernel_detach_ms':[a['kernel_detach_us']/1000 for a,b in pairs],
            'kernel_attach_ms':[b['kernel_attach_us']/1000 for a,b in pairs],
            'first_run_to_entry_ms':[b['run_to_entry_us']/1000 for a,b in pairs],
            'entry_to_guest_ack_ms':[b['entry_to_guest_write_us']/1000 for a,b in pairs]}
        result['directions'][role+('→B' if role=='A' else '→A')]={k:summary(v) for k,v in stages.items()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--swe',type=Path,nargs='*',default=[])
    parser.add_argument('--native',type=Path,nargs='*',default=[])
    parser.add_argument('--resident',type=Path,nargs='*',default=[])
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    swe = [swe_report(path) for path in args.swe]
    comparisons = []
    for vcpus in sorted({json.loads(Path(row['raw']['path']).read_text())['arguments']['vcpus'] for row in swe}):
        group = [row for row in swe if json.loads(Path(row['raw']['path']).read_text())['arguments']['vcpus']==vcpus]
        modes = {row['verification']['mode']:row['verification'] for row in group}
        need(set(modes)=={'baseline','direct'}, 'Missing matched SWE baseline/direct pair')
        a,b = modes['baseline'],modes['direct']
        keys = ('execution_inputs_sha256','workload_inputs_sha256','returncodes','tests','submission')
        need(all(a[k]==b[k] for k in keys), 'Baseline/direct inputs or results differ')
        comparisons.append({'vcpus':vcpus,'matched':True,'compared':list(keys)})
    result = {'schema':'multi-vcpu-followup-v1','swe':swe,'comparisons':comparisons,
              'resident':[resident_report(path) for path in args.resident],
              'native_diagnosis':[native_report(path) for path in args.native],
              'cpu_accounting':'x86 Linux USER_HZ=100; busy=user+nice+system+irq+softirq; active threshold=max(1 tick,1% wall); interval wall uses mean per-CPU accounted ticks when no uptime timestamp exists'}
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(args.output)


if __name__=='__main__':
    main()
