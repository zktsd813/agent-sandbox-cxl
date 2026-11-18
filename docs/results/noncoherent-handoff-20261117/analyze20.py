#!/usr/bin/env python3
"""Stage20: replay saved evidence; no live VM or device access."""
import json
import math
from pathlib import Path
import re
import statistics
import gzip
import bisect
from collections import Counter
import hashlib

R=None

def distribution(values):
    if not values:return {'n':0}
    values = sorted(values)
    n = len(values)
    return dict(n=n,median=statistics.median(values),p95=values[math.ceil(.95*n)-1],p99=values[math.ceil(.99*n)-1],max=values[-1],over50=sum(x>50 for x in values))


def events(path, marker):
    return [json.loads(l.split(marker,1)[1]) for l in path.read_text().splitlines() if marker in l]


def perf_analysis(trial, rows):
    script=trial/'perf-script.txt'
    if not script.exists():return None
    symbols=[]
    for line in (trial.parent/'l0-kallsyms.txt').read_text().splitlines():
        f=line.split()
        if len(f)>2 and f[1].lower()=='t':symbols.append((int(f[0],16),f[2]))
    symbols.sort()
    addresses = [v[0] for v in symbols]
    def resolve(address):
        i=bisect.bisect_right(addresses,address)-1
        return symbols[i][1] if i>=0 and address-symbols[i][0]<=1024*1024 else hex(address)
    samples=[]
    for block in script.read_text().split('\n\n'):
        lines=block.splitlines()
        if not lines:continue
        match=re.search(r'\s(\d+)\s+(\d+\.\d+):',lines[0])
        if not match:continue
        stack=[resolve(int(m[1],16)) for line in lines[1:] if (m:=re.match(r'\s*([0-9a-f]+)\s+',line))]
        samples.append((int(match[1]),float(match[2]),stack))
    out={'clock':'L0 CLOCK_MONOTONIC host_before/after; 1us text rounding tolerance',
         'exclude_guest':True,'requested_frequency_hz':499,'unit':'samples; not wall-time percentages'}
    for label,selected in [('tail',[r for r in rows if r['total_ms']>50]),('normal',[r for r in rows if r['total_ms']<=50])]:
        leaf = Counter()
        anywhere = Counter()
        n = 0
        duration = 0
        for row in selected:
            begin, end = (row['host_before_ns'] / 1000000000.0, row['host_after_ns'] / 1000000000.0)
            duration += 1000 * (end - begin)
            own={int(k.split(':')[1]) for k in row['l0_sched'] if k.startswith(row['role']+':')}
            matched=[s for s in samples if s[0] in own and begin-.000001<=s[1]<=end+.000001]
            row['perf_samples'] = len(matched)
            row['perf_leaf'] = dict(Counter((s[2][0] for s in matched if s[2])).most_common(12))
            writers=[s for s in matched if 'mmu_sync_children' in s[2]]
            waiters=[s for s in matched if s[2] and s[2][0]=='queued_read_lock_slowpath' and 'kvm_tdp_page_fault' in s[2]]
            row['perf_root_sync_samples']=len(writers)
            row['perf_ept_fault_read_wait_samples']=len(waiters)
            row['perf_paired_different_tid_within_2ms']=sum(any(a[0]!=b[0] and abs(a[1]-b[1])<=.002 for b in writers) for a in waiters)
            for _,_,stack in matched:
                n+=1
                if stack:leaf[stack[0]]+=1
                anywhere.update(set(stack))
        out[label]=dict(samples=n,window_ms=duration,leaf=leaf.most_common(30),anywhere=anywhere.most_common(30))
    return out


def analyze(trial, report_path=None):
    if report_path is None:
        meta = json.loads((trial / 'trial.json').read_text())
        report_path=trial/'handoff.json'
        out=Path(meta['report']).parent
    else:
        meta={'source_commit':json.loads(report_path.read_text())['source_reference']['commit']}
        out=report_path.parent
    report = json.loads(report_path.read_text())
    clean_by_role={}
    for role in ('A','B'):
        kernel=[]
        for f in out.glob('*dirty-kernel-times-'+role+'.txt'):
            kernel += [{k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',l)} for l in f.read_text().splitlines() if 'DIRTY_TIME ' in l]
        kernel=list({(k['begin'],k['tid']):k for k in kernel}.values())
        cleans = []
        last = None
        dispatch = None
        vmm_files=list(out.glob('*final-'+role+'-vmm.log.bin'))
        if not vmm_files:
            directories=[v['directory']+'/' for v in report['launches'] if v['role']==role]
            vmm_files=[p for p in out.glob('*final-vmm-log.bin') if any(d in p.read_text() for d in directories)]
        for f in vmm_files:
            for line in f.read_text().splitlines():
                if 'CLEAN_DISPATCH_JSON ' in line:dispatch=json.loads(line.split('CLEAN_DISPATCH_JSON ')[1])
                if 'COLLECT_JSON ' in line:last=json.loads(line.split('COLLECT_JSON ')[1])
                if 'NONCOHERENT_JSON ' not in line:continue
                value=json.loads(line.split('NONCOHERENT_JSON ')[1])
                if value['event']!='clean':continue
                value['collect'] = last
                value['dispatch'] = dispatch
                if last:
                    candidates=[k for k in kernel if last['begin_ns']<=k['begin']<=last['end_ns']]
                    if len(candidates)!=1:raise ValueError(f'kernel join {trial} {role} {len(candidates)}')
                    value['kernel']=candidates[0]
                cleans.append(value)
        queues=[]
        controller_files=list(out.glob('*controller-'+role+'-log.bin'))
        if not controller_files:
            for fence in report.get('controller_cleanup',[]):
                if fence['role']==role:
                    candidates=[p for p in out.glob('*final-controller-log.bin') if int(p.name.split('-')[0])<int(fence['file'].split('-')[0])]
                    controller_files.append(max(candidates))
        for f in controller_files:queues+=events(f,'CLEAN_QUEUE_JSON ')
        queues=[q for q in queues if q['command'] in (5,7)]
        if len(queues)!=len(cleans):raise ValueError('queue count mismatch')
        for c,q in zip(cleans,queues):
            c['queue_ns']=c['dispatch']['begin_ns']-q['begin_ns']
        clean_by_role[role]=iter(cleans)
    rows=[]
    for i,control in enumerate(report['cleans']):
        c = next(clean_by_role[control['role']])
        kernel = c['kernel']
        collect = c['collect']
        row=dict(index=i,role=control['role'],epoch=c['epoch'],total_ms=control['clean_ns']/1e6,
            collect_ms=c['collect_ns']/1e6,queue_ms=c['queue_ns']/1e6,ioctl_ms=collect['ioctl_ns']/1e6,
            scan_ms=collect['scan_ns']/1e6,reset_ms=collect['reset_ns']/1e6,
            writeback_ms=c['writeback_ns']/1e6,publish_ms=c['publish_ns']/1e6,
            kernel={k:v/1e6 for k,v in kernel.items() if k in ('total','slots','sync','mmu_wait','clear','flush','copy','arch_sum','arch_max')},
            kernel_counts={k:v for k,v in kernel.items() if k in ('arch_gfn','calls','spte_marks','spte_prefetch_marks','spte_payload_pages')})
        l1={}
        for tid,b in collect['before']['tasks'].items():
            if tid not in collect['after']['tasks']:continue
            a = collect['after']['tasks'][tid]
            before = list(map(int, b['schedstat'].split()))
            after = list(map(int, a['schedstat'].split()))
            l1[tid]=dict(name=b['name'],run_ms=(after[0]-before[0])/1e6,delay_ms=(after[1]-before[1])/1e6,slices=after[2]-before[2])
        row['l1_sched']=l1
        b = control['host_before']
        a = control['host_after']
        row['l0_sched']={tid:dict(run_ms=(a['tasks'][tid]['schedstat'][0]-v['schedstat'][0])/1e6,delay_ms=(a['tasks'][tid]['schedstat'][1]-v['schedstat'][1])/1e6) for tid,v in b['tasks'].items()}
        row['host_before_ns'] = b['mono_ns']
        row['host_after_ns'] = a['mono_ns']
        row['host_vmstat_delta']={k:a['vmstat'][k]-v for k,v in b['vmstat'].items()}
        row['host_loadavg_before'] = b['loadavg']
        row['host_loadavg_after'] = a['loadavg']
        row['node0_before'] = b['node0']
        row['node0_after'] = a['node0']
        def cpus(value):return {l.split()[0]:list(map(int,l.split()[1:9])) for l in value.splitlines() if re.match(r'cpu\d+ ',l)}
        cb, ca = (cpus(b['cpu_stat']), cpus(a['cpu_stat']))
        busy = total = 0
        for cpu in [*range(32),*range(64,96)]:
            key=f'cpu{cpu}'
            d = [y - x for x, y in zip(cb[key], ca[key])]
            total += sum(d)
            busy += sum(d) - d[3] - d[4]
        row['node0_cpu_busy_percent']=100*busy/total if total else None
        def steal(value):
            return {l.split()[0]:int(l.split()[8]) for l in value.splitlines() if re.match(r'cpu\d+ ',l)}
        for label,before,after in [('l0',b['cpu_stat'],a['cpu_stat']),('l1',collect['before']['stat'],collect['after']['stat'])]:
            x, y = (steal(before), steal(after))
            row[label + '_steal_ticks'] = {k: y[k] - v for k, v in x.items()}
        row['host_jiffies_before'] = int(b['schedstat'].splitlines()[1].split()[1])
        row['host_jiffies_after'] = int(a['schedstat'].splitlines()[1].split()[1])
        stages={k:row[k] for k in ('queue_ms','scan_ms','reset_ms','writeback_ms','publish_ms')}
        stages.update({'kernel_'+k:v for k,v in row['kernel'].items() if k!='total'})
        row['largest_stage']=max(stages,key=stages.get)
        rows.append(row)
    stats={k:distribution([r[k] for r in rows]) for k in ('total_ms','collect_ms','queue_ms','ioctl_ms','scan_ms','reset_ms','writeback_ms','publish_ms')}
    stats['pure_work_ms']=distribution([r['collect_ms']+r['writeback_ms']+r['publish_ms'] for r in rows])
    stats['handoff_ack_ms']=distribution([h['fast']['guest_ack_ns']/1e6 for h in report['transitions'] if h.get('measured')])
    prepared=report['config']['prepared']
    result=dict(trial=str(trial),source_commit=meta['source_commit'],artifacts=prepared['artifacts'],
        kvm_modules=prepared.get('kvm_module_override',{}).get('modules',{}),build=prepared.get('stage20_build'),
        stats=stats,rows=rows,tails=[r for r in rows if r['total_ms']>50],perf=perf_analysis(trial,rows))
    (trial/'tail-analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    print(trial.name,stats['total_ms'],[(r['index'],r['largest_stage']) for r in result['tails']])
    return result



def nc_events(directory):
    result=[]
    # Each completed resident report saves exactly one final VMM log per role.
    files=list(directory.glob('*final-*-vmm.log.bin')) or list(directory.glob('*final-vmm-log.bin'))
    for file in files:
        result += events(file,'NONCOHERENT_JSON ')
    return result


def timing_trial(trial):
    meta = json.loads((trial / 'trial.json').read_text())
    report = json.loads((trial / 'handoff.json').read_text())
    if report['result']!='passed':raise ValueError(str(trial))
    raw = Path(meta['report']).parent
    rows = nc_events(raw)
    warm = 2 * report['arguments']['warmup']
    cleans=sorted((r for r in rows if r['event']=='clean'),key=lambda r:r['epoch'])[warm:]
    exports=sorted((r for r in rows if r['event']=='export'),key=lambda r:r['epoch'])[warm:]
    imports=sorted((r for r in rows if r['event']=='import'),key=lambda r:r['epoch'])[warm:]
    transitions=[r for r in report['transitions'] if r.get('measured')]
    metrics={'ack_ms':distribution([r['fast']['guest_ack_ns']/1e6 for r in transitions]),
        'clean_collect_ms':distribution([r['collect_ns']/1e6 for r in cleans]),
        'clean_wb_ms':distribution([r['writeback_ns']/1e6 for r in cleans]),
        'clean_control_ms':distribution([r['clean_ns']/1e6 for r in report.get('cleans',[])[warm:]])}
    metrics['clean_rearm_ms']=distribution([r.get('rearm_ns',0)/1e6 for r in cleans])
    metrics['clean_work_ms']=distribution([sum(r.get(k,0) for k in ('collect_ns','rearm_ns','writeback_ns','publish_ns'))/1e6 for r in cleans])
    for kind,selected,keys in [('send',exports,('collect_ns','writeback_ns','publish_ns','control_ns')),
                               ('receive',imports,('log_read_ns','invalidate_ns','control_ns'))]:
        for key in keys:metrics[kind+'_'+key.replace('_ns','_ms')]=distribution([r[key]/1e6 for r in selected])
    metrics['send_object_wb_ms']=distribution([r['kernel'][0]/1e6 for r in exports])
    metrics['receive_object_inv_ms']=distribution([r['kernel'][1]/1e6 for r in imports])
    receive_epochs={r['epoch']:r for r in imports}
    explicit_nc=[]
    for send in exports:
        if send['epoch'] not in receive_epochs:continue
        receive=receive_epochs[send['epoch']]
        explicit_nc.append((sum(send[k] for k in ('collect_ns','writeback_ns','publish_ns','control_ns'))+send['kernel'][0]+
            sum(receive[k] for k in ('log_read_ns','invalidate_ns','control_ns'))+receive['kernel'][1])/1e6)
    metrics['explicit_noncoherent_path_ms']=distribution(explicit_nc)
    phases = {}
    phase_epochs = {}
    for file in raw.glob('*controller*log.bin'):
        for row in events(file,'RESIDENT_PHASES_JSON '):
            # Epoch is the actual handoff epoch (initial guest epoch is zero).
            if row['epoch']<=warm:continue
            phase_epochs.setdefault(row['epoch'],{})[row['side']]=row['stages_ns']
            for name,value in row['stages_ns'].items():phases.setdefault(row['side']+':'+name,[]).append(value/1e6)
    metrics['controller_phases_ms']={k:distribution(v) for k,v in phases.items()}
    metrics['controller_and_ack_gap_ms']=distribution([(t['fast']['guest_ack_ns']-
        phase_epochs[t['fast']['ack']['epoch']]['send']['export_api']-phase_epochs[t['fast']['ack']['epoch']]['receive']['import_api'])/1e6
        for t in transitions if t['fast']['ack']['epoch'] in phase_epochs and all(k in phase_epochs[t['fast']['ack']['epoch']] for k in ('send','receive'))])
    restore={}
    all_transitions=[t for t in report['transitions'] if t['kind']=='direct']
    for role in ('A','B'):
        records=[]
        for file in raw.glob('*final-'+role+'-vmm.log.bin'):records+=events(file,'RESTORE_TIMING ')
        for side,scopes,field in [('send',('direct_export',),'source'),('receive',('direct_import','direct_resident_import'),'destination')]:
            selected=[r for r in records if r['scope'] in scopes]
            ordered=[t for t in all_transitions if t[field]==role]
            if selected:
                if len(selected)!=len(ordered):raise ValueError('Restore profile count mismatch')
                for row,transition in zip(selected,ordered):
                    if not transition.get('measured'):continue
                    for name,value in row['stages_us'].items():restore.setdefault(side+':'+name,[]).append(value/1000)
    metrics['firecracker_phases_ms']={k:distribution(v) for k,v in restore.items()}
    profile={}
    for transition in transitions if report['arguments'].get('profile') else []:
        for name in ('detach_ns','attach_ns'):
            for i,value in enumerate(transition['fast']['ack'].get(name,[])):
                profile.setdefault(f'{name}:{i}',[]).append(value/1e6)
    metrics['kernel_profile_ms']={k:distribution(v) for k,v in profile.items()}
    probe={}
    for role in ('A','B'):
        samples = []
        spans = []
        for f in raw.glob('*service-probe-'+role+'.bin'):samples+=events(f,'SERVICE_PROBE_JSON ')
        for f in raw.glob('*final-'+role+'-vmm.log.bin'):spans+=events(f,'WRITEBACK_SPAN_JSON ')
        if samples:
            overlap=[s for s in samples if any(w['begin_ns']<=s['begin_ns']<w['end_ns'] for w in spans)]
            completed_inside=sum(any(w['begin_ns']<=s['begin_ns']<s['end_ns']<=w['end_ns'] for w in spans) for s in samples)
            probe[role]=dict(all_ms=distribution([(s['end_ns']-s['begin_ns'])/1e6 for s in samples]),
                issued_during_wb_ms=distribution([(s['end_ns']-s['begin_ns'])/1e6 for s in overlap]),completed_inside_wb=completed_inside)
    guest_io={}
    if report.get('guest_io_probe'):
        samples=[json.loads(l.split('IO_LATENCY_JSON ',1)[1]) for l in gzip.decompress((raw/report['guest_io_probe']['file']).read_bytes()).decode().splitlines() if 'IO_LATENCY_JSON ' in l]
        measured_epochs={c['epoch'] for c in report['cleans'][warm:]}
        selected=[s for s in samples if s['epoch']==s['after_epoch'] and s['epoch'] in measured_epochs]
        normal_epochs={control['epoch'] for control,clean in zip(report['cleans'][warm:],cleans) if clean['collect_ns']<5_000_000}
        normal=[s for s in selected if s['epoch'] in normal_epochs]
        by_epoch={}
        for s in selected:by_epoch.setdefault(s['epoch'],[]).append((s['end_ns']-s['begin_ns'])/1e6)
        guest_io={'all_reads':len(samples),'same_epoch_measured_reads':len(selected),
                  'latency_ms':distribution([(s['end_ns']-s['begin_ns'])/1e6 for s in selected]),
                  'normal_collect_epochs':len(normal_epochs),
                  'normal_collect_reads_ms':distribution([(s['end_ns']-s['begin_ns'])/1e6 for s in normal]),
                  'epoch_max_ms':distribution([max(v) for v in by_epoch.values()]),
                  'normal_collect_epoch_max_ms':distribution([max(v) for k,v in by_epoch.items() if k in normal_epochs])}
    tail_rows=[]
    for control,clean in zip(report.get('cleans',[])[warm:],cleans):
        work={k:clean.get(k,0)/1e6 for k in ('collect_ns','rearm_ns','writeback_ns','publish_ns')}
        if control['clean_ns']>50_000_000 or sum(work.values())>50:
            tail_rows.append(dict(guest_epoch=control['epoch'],control_ms=control['clean_ns']/1e6,stages_ms=work,
                largest_stage=max(work,key=work.get),kernel_subdivision=False))
    configuration={k:report['arguments'].get(k) for k in ('vcpus','memory_mib','dirty_mib','warmup','iterations','preclean','clean_helper','manual_protect','profile')}
    configuration['l1_memory_mib']=report['config']['l1_memory_mib']
    configuration['l1_overcommit']=meta.get('l1_overcommit',{})
    return dict(metrics=metrics,clean_tails=tail_rows,configuration=configuration,guest_io=guest_io,service_probe=probe,source_revision=meta['source_commit'],
        artifacts=report['config']['prepared']['artifacts'],kvm_modules=report['config']['prepared'].get('kvm_module_override',{}).get('modules',{}),build=report['config']['prepared'].get('stage20_build'),raw=str(raw))


def cxl_extra(lines):
    return {name:lines*cost/1e6 for name,cost in [('dirty_wb_ms',16),('absent_wb_ms',6.25),('clean_invalidate_ms',2),('dirty_wb_absent_inv_ms',22.25),('large_wb_6_2_ms',6.2),('large_wb_6_7_ms',6.7)]}


def oracle_proof(report, directory):
    """Recompute saved bitmap containment independently of reported missing counts."""
    boundaries=0
    for row in report['dirty_oracle']:
        seq = row['sequence']
        words = (row['pages'] + 63) // 64
        maps=(directory/f'{seq}.bitmap').read_bytes()
        if 'bitmap_sha256' in row:
            assert hashlib.sha256(maps).hexdigest()==row['bitmap_sha256']
        union,kernel,device=[int.from_bytes(maps[i*words*8:(i+1)*words*8],'little') for i in range(3)]
        assert union==kernel|device
        if not row['hash_boundary']:continue
        changed=int.from_bytes((directory/f'{seq}.changed').read_bytes(),'little')
        assert hashlib.sha256((directory/f'{seq}.changed').read_bytes()).hexdigest()==row['changed_bitmap_sha256']
        if 'line_counts_sha256' in row:
            assert hashlib.sha256((directory/f'{seq}.lines').read_bytes()).hexdigest()==row['line_counts_sha256']
        checked=int.from_bytes((directory/f'{seq}.checked').read_bytes(),'little')
        assert changed&~checked==0
        assert changed.bit_count()==row['changed_guest_pages']
        unchanged=union&~changed
        if 'unchanged_origin_pages' in row:
            assert row['unchanged_origin_pages']=={'kvm_only':(unchanged&kernel&~device).bit_count(),
                'device_only':(unchanged&device&~kernel).bit_count(),'both':(unchanged&kernel&device).bit_count()}
        boundaries+=1
    return dict(hash_boundaries=boundaries,missing_pages=0)


def write_tail_csv(result, output):
    import csv
    rows=[]
    for row in result['historical_tails']['tails']:
        rows.append(dict(trial='stage19-final-256-on-r2',index=row['index'],collect_ms=row['collect_ms'],
            largest_stage='collect',scope='historical; no substage counters'))
    for name,trial in result['tail'].items():
        for row in trial['tails']:
            item={k:row[k] for k in ('index','total_ms','collect_ms','queue_ms','ioctl_ms','scan_ms','reset_ms','writeback_ms','publish_ms','largest_stage')}
            item.update(trial=name,scope='kernel counters',epoch=row['epoch'],node0_cpu_busy_percent=row['node0_cpu_busy_percent'])
            item.update({'kernel_'+k+'_ms':v for k,v in row['kernel'].items()})
            item['l0_max_run_delay_ms']=max(v['delay_ms'] for v in row['l0_sched'].values())
            item['l1_vmm_run_delay_ms']=max((v['delay_ms'] for v in row['l1_sched'].values() if v['name']=='firecracker'),default=0)
            for key in ('perf_root_sync_samples','perf_ept_fault_read_wait_samples','perf_paired_different_tid_within_2ms'):
                item[key]=row.get(key)
            rows.append(item)
    for name,trial in result['optimization'].items():
        for row in trial['clean_tails']:
            item=dict(trial=name,epoch=row['guest_epoch'],total_ms=row['control_ms'],
                      largest_stage=row['largest_stage'],scope='timing; kernel subdivision disabled')
            item.update({k.replace('_ns','_ms'):v for k,v in row['stages_ms'].items()})
            rows.append(item)
    fields=['trial','index','epoch','scope','total_ms','collect_ms','queue_ms','ioctl_ms','scan_ms','reset_ms',
        'kernel_slots_ms','kernel_sync_ms','kernel_mmu_wait_ms','kernel_clear_ms','kernel_flush_ms','kernel_copy_ms',
        'kernel_arch_sum_ms','kernel_arch_max_ms','rearm_ms','writeback_ms','publish_ms','largest_stage',
        'l0_max_run_delay_ms','l1_vmm_run_delay_ms','node0_cpu_busy_percent',
        'perf_root_sync_samples','perf_ept_fault_read_wait_samples','perf_paired_different_tid_within_2ms']
    with output.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def collect_figure(result, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    groups=[('Shared DAX handoff, L1 live',[r['ioctl_ms'] for k,v in result['tail'].items() if k.startswith('repro-') for r in v['rows']])]
    for level in ('l0','l1'):
        values=[]
        prefix='dirtycount' if result.get('counted_full_write_dirty_log') else 'dirtyfull'
        for path in Path(result['raw_root']).glob(f'{prefix}-{level}-[01].log'):
            values.extend(float(s)*1000 for s in re.findall(r'Iteration \d+ get dirty log time: ([0-9.]+)s',path.read_text()))
        groups.append(('Direct L0, full 256 MiB' if level=='l0' else 'Ordinary L1, full 256 MiB',values))
    fig,ax=plt.subplots(figsize=(7.2,3.6),layout='constrained')
    for label,values in groups:
        values=sorted(values)
        ax.step(values,[(i+1)/len(values) for i in range(len(values))],where='post',label=f'{label} (n={len(values)})')
    ax.set(xscale='log',xlabel='KVM_GET_DIRTY_LOG (ms, log scale)',ylabel='Empirical cumulative fraction',ylim=(0,1.01))
    ax.axvline(50,color='0.5',linestyle=':',linewidth=1)
    ax.grid(alpha=.2)
    ax.legend(loc='lower right',fontsize=8)
    fig.savefig(output,dpi=240)
    plt.close(fig)


def accounting(report_path, oracle_directory):
    import struct
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'implementation/virtual_machine'))
    from verify_swe_handoff import verify_cache_receipts
    report = json.loads(report_path.read_text())
    assert report['result'] == 'passed'
    proof=verify_cache_receipts(report,report_path.parent)
    proof['bitmap_replay']=oracle_proof(report,oracle_directory)
    rows = report['dirty_oracle']
    assert all((r['missing_pages'] == r['other_changed_pages'] == 0 for r in rows))
    warm = 2 * report['arguments'].get('warmup', 0) if report['arguments'].get('resident_bench') else 0
    hop = 0
    windows = []
    handoffs = []
    invalidations = []
    preparation = []
    last_window = None
    writes = 0
    union = 0
    repeat_changed = repeat_unchanged = 0
    mapping=(oracle_directory/'mapping.bin').read_bytes()
    args = report['arguments']
    payload_pages = args.get('dirty_mib', 0) * 256 if args.get('dirty_lines') else 0
    for row in rows:
        if row['phase'] in (3,5):
            if last_window:
                emitted = row['kernel_emitted_lines']
                changed = last_window['changed_lines']['ram']
                invalidations.append(dict(sequence=row['sequence'],phase=row['phase'],measured=last_window['measured'],
                    emitted_lines=emitted,changed_ram_lines=changed,ram_line_ratio=emitted['ram']/changed if changed else None,
                    full_fallback=row.get('full_invalidate_fallback',False)))
            else:preparation.append(dict(sequence=row['sequence'],phase=row['phase'],emitted_lines=row['kernel_emitted_lines']))
            continue
        if row['phase']==1:preparation.append(dict(sequence=row['sequence'],phase=1,emitted_lines=row['kernel_emitted_lines']))
        if row['phase'] not in (2,6):continue
        if not row.get('hash_boundary') or 'changed_lines' not in row:raise ValueError('Every accounting clean/export must be hashed')
        seq = row['sequence']
        words = (row['pages'] + 63) // 64
        bitmaps = (oracle_directory / f'{seq}.bitmap').read_bytes()
        bits = int.from_bytes(bitmaps[:words * 8], 'little')
        changed_bits=int.from_bytes((oracle_directory/f'{seq}.changed').read_bytes(),'little')
        assert changed_bits&~bits==0, 'Paused windows must be covered by this snapshot'
        emitted = row['kernel_emitted_lines']
        ram_changed = row['changed_lines']['ram']
        page_changed = row['changed_guest_pages']
        assert emitted['ram']==bits.bit_count()*64
        w=dict(sequence=seq,phase=row['phase'],epoch=row['epoch'],measured=hop>=warm,
            listed_pages=row['listed_pages'],changed_pages=page_changed,changed_lines=row['changed_lines'],
            emitted_lines=emitted,page_ratio=row['listed_pages']/page_changed if page_changed else None,
            line_ratio=emitted['ram']/ram_changed if ram_changed else None,
            unchanged_origins=row['unchanged_origin_pages'],changed_origins=row['changed_origin_pages'],
            changed_object_pages=row['object_changed_pages'],changed_control_pages=row['control_changed_pages'],
            object_arena_bytes=row['arena_bytes'],
            extra_ram_lines=emitted['ram']-ram_changed,cxl_extra_ram=cxl_extra(emitted['ram']-ram_changed))
        if payload_pages:
            values = (oracle_directory / f'{seq}.lines').read_bytes()
            payload = range(128 * 256, 128 * 256 + payload_pages)
            w['payload_changed_lines']=sum(values[struct.unpack_from('<Q',mapping,p*16)[0]//4096] for p in payload)
            w['payload_listed_pages']=sum(bool(bitmaps[p//8]&(1<<(p%8))) for p in payload)
            expected=payload_pages*args['dirty_lines'] if args['dirty_mode']=='change' and row['phase']==6 and hop>0 else 0
            w['expected_payload_changed_lines']=expected
            if hop>=warm:assert w['payload_changed_lines']==expected,(str(report_path),w)
        repeat_changed+=(bits&union&changed_bits).bit_count()
        repeat_unchanged+=(bits&union&~changed_bits).bit_count()
        windows.append(w)
        last_window = w
        writes += emitted['ram']
        union |= bits
        if row['phase']==2:
            net_lines=(oracle_directory/f'{seq}.handoff-lines').read_bytes()
            obj_begin = row['object_base_offset'] // 4096
            obj_end = (row['object_limit_offset'] + 4095) // 4096
            net=row['handoff_changed_lines']['ram'];h=dict(sequence=seq,measured=hop>=warm,
                summed_ram_wb_lines=writes,unique_listed_pages=union.bit_count(),
                repeated_ram_wb_lines=writes-union.bit_count()*64,net_changed_lines=net,
                repeated_changed_page_flushes=repeat_changed,repeated_unchanged_page_flushes=repeat_unchanged,
                net_changed_pages=row['handoff_changed_pages'],
                wb_to_net_line_ratio=writes/net if net else None,
                changed_lines_by_region=row['handoff_changed_lines'],net_changed_object_pages=sum(v>0 for v in net_lines[obj_begin:obj_end]))
            assert (repeat_changed+repeat_unchanged)*64==h['repeated_ram_wb_lines']
            handoffs.append(h)
            writes = 0
            union = 0
            repeat_changed = repeat_unchanged = 0
            hop += 1
    measured = [w for w in windows if w['measured']]
    cleans = [w for w in measured if w['phase'] == 6]
    total = {}
    for region in ('ram','objects','log','control'):
        emitted = sum((w['emitted_lines'][region] for w in measured))
        changed = sum((w['changed_lines'][region] for w in measured))
        total[region]=dict(emitted_lines=emitted,changed_lines=changed,line_ratio=emitted/changed if changed else None,
                           extra_lines=emitted-changed,cxl_extra=cxl_extra(emitted-changed))
    # Objects change at cleans but are only flushed at handoff; compare each
    # handoff's full arena receipt with its independent handoff hash state.
    object_wb=sum(w['emitted_lines']['objects'] for w in measured if w['phase']==2)
    object_net=sum(h['changed_lines_by_region']['objects'] for h in handoffs if h['measured'])
    total['objects_handoff']=dict(emitted_lines=object_wb,net_changed_lines=object_net,line_ratio=object_wb/object_net if object_net else None)
    total['objects_handoff'].update(extra_lines=object_wb-object_net,cxl_extra=cxl_extra(object_wb-object_net))
    total['invalidate_lines']={region:sum(w['emitted_lines'][region] for w in invalidations if w['measured']) for region in ('ram','objects','log','control')}
    init_values=[r['kernel_emitted_lines'].get('control_init_invalidate') for r in rows]
    initial_complete=all(v is not None for v in init_values)
    initial_measured=sum(w['emitted_lines'].get('control_init_invalidate') or 0 for w in invalidations if w['measured'])
    initial_measured+=sum(w['emitted_lines'].get('control_init_invalidate') or 0 for w in measured)
    total['control_initial_invalidate']=dict(available=initial_complete,all_boundaries=sum(init_values) if initial_complete else None,
        measured_windows=initial_measured if initial_complete else None)
    total['invalidate_lines']['control_with_initial']=total['invalidate_lines']['control']+initial_measured if initial_complete else None
    all_instructions={direction:{region:sum(r['kernel_emitted_lines'][region] for r in rows if r['phase'] in phases)
        for region in ('ram','objects','log','control')} for direction,phases in [('writeback',(1,2,4,6)),('invalidate',(3,5))]}
    all_instructions['invalidate']['control_with_initial']=all_instructions['invalidate']['control']+sum(init_values) if initial_complete else None
    all_instructions['scope']='All oracle receipts, including preparation and warmup; initial control invalidate is separate in v2'
    open_window=dict(summed_ram_wb_lines=writes,unique_listed_pages=union.bit_count(),
        repeated_ram_wb_lines=(repeat_changed+repeat_unchanged)*64,net_changed_lines=None,
        note='No terminal handoff; individual paused windows remain fully measured')
    assert sum(w['emitted_lines']['ram'] for w in windows)==sum(h['summed_ram_wb_lines'] for h in handoffs)+writes
    return dict(report=str(report_path),proof=proof,windows=windows,invalidation_windows=invalidations,preparation_receipts=preparation,handoff_windows=handoffs,open_handoff_window=open_window,totals=total,all_instruction_receipts=all_instructions,
        clean_medians={k:statistics.median(w[k] for w in cleans) for k in ['listed_pages','changed_pages','page_ratio','line_ratio'] if cleans and all(w[k] is not None for w in cleans)},
        payload_clean_lines=[w.get('payload_changed_lines') for w in cleans] if payload_pages else None,
        handoffs=sum(r['phase']==2 for r in rows),cleans=sum(r['phase'] in (4,6) for r in rows),
        hash_boundaries=sum(r['hash_boundary'] for r in rows),missing_pages=0,
        artifacts=report['config']['prepared']['artifacts'],kvm_modules=report['config']['prepared'].get('kvm_module_override',{}).get('modules',{}),build=report['config']['prepared'].get('stage20_build'),source_reference=report.get('source_reference'))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw', type=Path)
    parser.add_argument('--merge', action='store_true')
    parser.add_argument('--figure', action='store_true')
    args = parser.parse_args()
    global R
    R = args.raw.resolve()
    result = {'raw_root': str(R), 'tail': {}, 'accounting': {}, 'optimization': {}, 'diagnostic_optimization': {}}
    result['historical_tails']=json.loads((R/'historical-tails.json').read_text())
    result['direct_dirty_log']={p.stem:distribution([float(s)*1000 for s in re.findall(r'Iteration \d+ get dirty log time: ([0-9.]+)s',p.read_text())]) for p in sorted(R.glob('dirtytest-l[01]-[01].log'))}
    result['direct_dirty_log_manifest']=json.loads((R/'dirtytest.json').read_text())
    result['l0_lock_module_evidence']=json.loads((R/'l0-module-source.json').read_text())
    result['live_direct_dirty_log']={p.stem:distribution([float(s)*1000 for s in re.findall(r'Iteration \d+ get dirty log time: ([0-9.]+)s',p.read_text())]) for p in sorted(R.glob('dirtylive-l[01]-[01].log'))}
    if (R/'dirtylive.json').exists():result['live_direct_dirty_log_manifest']=json.loads((R/'dirtylive.json').read_text())
    if (R/'dirtyfull.json').exists():
        result['full_write_dirty_log_manifest']=json.loads((R/'dirtyfull.json').read_text())
        result['full_write_dirty_log']={p.stem:distribution([float(s)*1000 for s in re.findall(r'Iteration \d+ get dirty log time: ([0-9.]+)s',p.read_text())]) for p in sorted(R.glob('dirtyfull-l[01]-[01].log'))}
    if (R/'dirtycount.json').exists():
        manifest=json.loads((R/'dirtycount.json').read_text())
        assert manifest['result']=='passed'
        result['counted_full_write_dirty_log_manifest']=manifest
        result['counted_full_write_dirty_log']={}
        for path in sorted(R.glob('dirtycount-l[01]-[01].log')):
            text=path.read_text()
            counts=[(int(a),int(b)) for a,b in re.findall(r'Iteration \d+ dirty pages: (\d+) expected: (\d+)',text)]
            times=[float(s)*1000 for s in re.findall(r'Iteration \d+ get dirty log time: ([0-9.]+)s',text)]
            assert len(counts)==len(times)==220 and all(a==b==65536 for a,b in counts),(str(path),counts)
            result['counted_full_write_dirty_log'][path.stem]={**distribution(times),'verified_dirty_pages':65536,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    result['limitations']=['Coherent nested lab; no real noncoherent CXL hardware run',
        'Value changes are a correctness lower bound; same-value stores are invisible',
        'Historical 13 tails only have collect timing; no retrospective kernel subdivision',
        'Ordinary direct KVM dirty-log test excludes shared DAX EPT and handoff',
        'Bitmap exchanges still scan all u64 words; output traversal skips zero words']
    for trial in sorted(R.glob('repro-*'))+sorted(R.glob('tail-final*')):
        if trial.is_dir() and (trial/'handoff.json').exists() and json.loads((trial/'trial.json').read_text())['result']=='passed':
            result['tail'][trial.name]=analyze(trial)
    swe_tail=R/'swe-tail/gin-gonic__gin-3741-direct-heavy-r0'
    result['tail']['swe-gin-live']=analyze(swe_tail,swe_tail/'report.json')
    result['diagnostic_accounting']={}
    current_mixed=(R/'mixed-count-all'/'handoff.json')
    current_mixed_ok=current_mixed.exists() and json.loads(current_mixed.read_text())['result']=='passed'
    for pattern in ('account-*','mixed-lines-final','mixed-count-all'):
        for trial in sorted(R.glob(pattern)):
            if not trial.is_dir() or not (trial/'handoff.json').exists():continue
            report=json.loads((trial/'handoff.json').read_text())
            if report['result']!='passed' or not report.get('dirty_oracle'):continue
            if all('changed_lines' in r for r in report['dirty_oracle'] if r['phase'] in (2,6)) and any(r['phase']==2 for r in report['dirty_oracle']):
                selected=trial.name.startswith('account-final-') or trial.name==('mixed-count-all' if current_mixed_ok else 'mixed-lines-final')
                target='accounting' if selected else 'diagnostic_accounting'
                result[target][trial.name]=accounting(trial/'handoff.json',trial/'dirty-oracle')
    current_swe=R/'swe-lines-count-all'
    suite_verification=current_swe/'suite-verification.json'
    current_swe_ok=suite_verification.exists() and json.loads(suite_verification.read_text())['result']=='passed' and len(list(current_swe.glob('*/report.json')))==3
    for parent in ('swe-lines-final','swe-lines-count-all'):
        for path in (R/parent).glob('*/report.json'):
            if (path.parent/'dirty-oracle').exists() and json.loads(path.read_text())['result']=='passed':
                selected=parent==('swe-lines-count-all' if current_swe_ok else 'swe-lines-final')
                result['accounting' if selected else 'diagnostic_accounting'][parent+'/'+path.parent.name]=accounting(path,path.parent/'dirty-oracle')
    for pattern in ('opt-*','critical-*','service-*','manual-*','io-final-r3-*','helper-api-final*'):
        for trial in R.glob(pattern):
            if trial.is_dir() and (trial/'trial.json').exists() and json.loads((trial/'trial.json').read_text())['result']=='passed':
                target='optimization' if trial.name.startswith(('opt-final-','critical-final-','io-final-r3-','helper-api-final-current')) else 'diagnostic_optimization'
                result[target][trial.name]=timing_trial(trial)
    result['correctness']={}
    result['correctness']['swe-gin-live']=oracle_proof(json.loads((swe_tail/'report.json').read_text()),swe_tail/'dirty-oracle')
    for pattern in ('repro-on-*','spte-read-cause','helper-final-*','helper-count-all-*','regression-*','smp-*','guest-io-smoke-r2'):
        for trial in R.glob(pattern):
            path=trial/'handoff.json'
            if not path.exists():continue
            report=json.loads(path.read_text())
            if report['result']=='passed' and report.get('dirty_oracle'):
                result['correctness'][trial.name]=oracle_proof(report,trial/'dirty-oracle')
                if report.get('reuse'):
                    raw=Path(json.loads((trial/'trial.json').read_text())['report']).parent/'reuse-vm2'
                    result['correctness'][trial.name+'-vm2']=oracle_proof(json.loads((raw/'report.json').read_text()),raw/'dirty-oracle')
    result['failed_trials']=[]
    result['smp_regressions']={}
    for path in sorted(R.glob('*/trial.json')):
        trial=json.loads(path.read_text())
        if path.parent.name.startswith('smp-') or path.parent.name=='regression-legacy':
            report_path=path.with_name('handoff.json')
            if report_path.exists() and trial['result']!='running':
                smp_report=json.loads(report_path.read_text())
                hops=[h for h in smp_report['transitions'] if h['kind']=='direct']
                result['smp_regressions'][path.parent.name]=dict(result=trial['result'],error=smp_report.get('error'),
                    direct_handoffs=len(hops),legacy=smp_report['arguments']['legacy_observer'],
                    initial_cpus=hops[0]['fast']['ack']['cpus'][:2] if hops else [],final_cpus=hops[-1]['fast']['ack']['cpus'][:2] if hops else [],
                    missing_pages=sum(r['missing_pages'] for r in smp_report.get('dirty_oracle',[])),artifacts=smp_report['config']['prepared']['artifacts'])
        if trial['result']!='passed':
            failure=dict(name=path.parent.name,result=trial['result'],error=trial.get('error'),cleanup=trial.get('cleanup'),raw=str(path))
            report_path=path.with_name('handoff.json')
            if report_path.exists():
                failed_report=json.loads(report_path.read_text())
                failure.update(nested_error=failed_report.get('error'),completed_transitions=len(failed_report.get('transitions',[])),oracle_missing_pages=sum(r['missing_pages'] for r in failed_report.get('dirty_oracle',[])))
            result['failed_trials'].append(failure)
    result['validation']={}
    for name in ('stage18-replay.json','stage19-replay.json'):
        result['validation'][name]=dict(raw=str(R/name),sha256=hashlib.sha256((R/name).read_bytes()).hexdigest())
    for name in ('python-tests-count-all.log','final-python-tests.log','rust-tests-count-all.log','final-patch-contracts.log'):
        if (R/name).exists():result['validation'][name]=dict(raw=str(R/name),sha256=hashlib.sha256((R/name).read_bytes()).hexdigest(),last_lines=(R/name).read_text().splitlines()[-8:])
    result['analysis_source_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (R/'stage20-analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    if args.merge:
        output = Path(__file__).with_name('summary.json')
        prior = json.loads(output.read_text())
        prior['stage20'] = result
        output.write_text(json.dumps(prior,indent=2)+'\n')
        write_tail_csv(result,output.with_name('stage20-tails.csv'))
    if args.figure:collect_figure(result,Path(__file__).with_name('stage20-collect-cdf.png'))
    print('stage20: tails',len(result['tail']),'accounting',len(result['accounting']),'optimization',len(result['optimization']))

if __name__=='__main__':main()
