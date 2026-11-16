#!/usr/bin/env python3
"""Summarize verified trajectory suites, or collect plain L0 KVM scan costs."""
import argparse
from collections import defaultdict
import csv
import fcntl
import hashlib
import json
from pathlib import Path
import random
import statistics as st
import subprocess
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')


def load_snapshot():
    return Path('/proc/loadavg').read_text()+subprocess.check_output(
        ['ps', '-eo', 'pid,user,psr,pcpu,comm'], text=True)


def calibrate(args):
    import re
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    tasks = [(mode, repeat) for mode in ('4k', '2m') for repeat in range(args.repetitions)]
    random.Random(args.seed).shuffle(tasks)
    rows = []
    for mode, repeat in tasks:
        with (REPO/'.codex-runs/lab.lock').open('a') as lab_lock:
            fcntl.flock(lab_lock, fcntl.LOCK_EX)
            hardware = subprocess.check_output(['numactl', '--hardware'], text=True)
            if int(re.search(r'node 0 free: (\d+) MB', hardware)[1]) < 512+2+4096:
                raise RuntimeError('Node 0 free < physical VM budget + 4GiB')
            with (REPO/'.codex-runs/quiet.lock').open('a') as quiet:
                fcntl.flock(quiet, fcntl.LOCK_EX)
                before = load_snapshot()
                started = time.monotonic_ns()
                command = ['numactl', '--cpunodebind=0', '--membind=0', '--physcpubind=8',
                           str(args.calibrate.resolve()), mode, '512']
                raw = subprocess.check_output(command, text=True)
                ended = time.monotonic_ns()
                after = load_snapshot()
        row = json.loads(raw)
        row.update(repeat=repeat, start_ns=started, end_ns=ended, hardware=hardware,
                   load_before=before, load_after=after, argv=command)
        first, warm = row['scans'][0], row['scans'][1:]
        row['excess_ns'] = first['host_ns']-st.median(x['host_ns'] for x in warm)
        row['fixed_faults'] = first['pf_fixed']-st.median(x['pf_fixed'] for x in warm)
        if row['fixed_faults'] <= 0 or any(x['pf_fixed'] for x in warm):
            raise RuntimeError('Calibration did not produce first-only fixed faults')
        row['us_per_fixed_fault'] = row['excess_ns']/row['fixed_faults']/1000
        if mode == '4k' and first['pages_2m'] or mode == '2m' and first['pages_2m'] < 256:
            raise RuntimeError('Requested EPT leaf size was not observed')
        rows.append(row)
        save(output/'physical.json', {'seed': args.seed, 'rows': rows,
            'method': 'After six initial scans, delete/re-add this VM memslot with identical host mapping and assert zero EPT leaves. Reaccess minus median of five subsequent warm scans. Denominator=KVM vCPU pf_fixed delta. No L0 tracefs access. Approximate fault service cost; cache/TLB effects remain.'})
        print(mode, repeat, row['us_per_fixed_fault'], flush=True)


def percentile(values, p):
    values = sorted(values)
    position = (len(values)-1)*p
    lower = int(position)
    return values[lower]+(values[min(lower+1,len(values)-1)]-values[lower])*(position-lower)


def summarize(args):
    groups, trials, commands = defaultdict(list), [], []
    for suite_path in args.suites:
        suite = json.loads(suite_path.read_text())
        for arm in suite['trials']:
            if arm['result'] != 'passed':
                continue
            path = suite_path.parent/arm['report_file']
            r = json.loads(path.read_text())
            if not r['arguments'].get('trajectory_policy'):
                continue
            method = 'prefault-'+r['arguments']['prefault'] if r['arguments'].get('prefault') else r['mode']
            key = (r['name'], r['arguments']['trajectory_policy'], method)
            row = {'fixture':key[0], 'policy':key[1], 'mode':key[2], 'repeat':arm['repeat'],
                   'count_run':r['arguments']['count_faults'], 'report':str(path),
                   'total_s':r['observed_replay_ns']/1e9, 'active_s':r['trajectory_total_ns']/1e9,
                   'stop_s':r['trajectory_handoff_ns']/1e9, 'moves':len(r['boundaries'])}
            if r['arguments'].get('prefault'):
                fills = [x['prefault'] for x in r['transitions'] if x.get('prefault_mode')]
                queries = [x['source_ranges'] for x in r['transitions'] if x.get('source_ranges')]
                row.update(prefault_kernel_s=sum(x['kernel_ns'] for x in fills)/1e9,
                    prefault_wall_s=sum(x['wall_ns'] for x in fills)/1e9,
                    query_kernel_s=sum(x['kernel_ns'] for x in queries)/1e9,
                    query_wall_s=sum(x['wall_ns'] for x in queries)/1e9,
                    prefault_pages=sum(x['processed_pages'] for x in fills),
                    source_ranges=sum(len(x['ranges']) for x in queries))
            node0 = set(range(32)) | set(range(64,96))
            owned = {str(vm['pid']) for vm in r['config']['vms'].values()}
            owned.add(str(suite.get('controller_pid', -1)))
            noise = []
            for phase in ('before','after'):
                for line in (suite_path.parent/(arm['id']+'-load-'+phase+'.txt')).read_text().splitlines()[2:]:
                    fields = line.split()
                    if len(fields) == 5 and fields[4] != 'ps' and int(fields[2]) in node0 and float(fields[3]) >= 5 and fields[0] not in owned:
                        noise.append(line.strip())
            row['other_cpu_load'] = noise
            row['disturbed'] = bool(noise)
            row['native_pause_to_resume_s'] = (sum(x['fast']['pause_to_resume_response_ns'] for x in r['transitions'] if x['kind']=='direct')/1e9
                if r['mode']=='direct' else None)
            if row['count_run']:
                row['ept_violations'] = sum(x['faults'][role]['ept_violation'] for x in r['commands'] for role in ('A','B'))
                row['page_faults'] = sum(x['faults'][role]['page_fault'] for x in r['commands'] for role in ('A','B'))
                row['ram_ept_violations'] = sum(x['faults'][role]['ram_ept_violation'] for x in r['commands'] for role in ('A','B'))
                row['mmio_ept_violations'] = row['page_faults']-row['ram_ept_violations']
                row['spte'] = r['spte_final']
                row['whole_interval_ept'] = sum(r['fault_final'][role]['ept_violation']-r['fault_initial'][role]['ept_violation'] for role in ('A','B'))
                row['whole_interval_ram_ept'] = sum(r['fault_final'][role]['ram_ept_violation']-r['fault_initial'][role]['ram_ept_violation'] for role in ('A','B'))
                row['vm_creates'] = sum(r['fault_final'][role]['create_vm']-r['fault_initial'][role]['create_vm'] for role in ('A','B'))
                row['vcpu_creates'] = sum(r['fault_final'][role]['create_vcpu']-r['fault_initial'][role]['create_vcpu'] for role in ('A','B'))
                row['nested_root_ns'] = sum(x['duration_ns'] for node in r['nested_traces'] for x in node['intervals'] if x['function']=='nested_root')
            groups[key].append((row, r))
            trials.append(row)
    for key, items in groups.items():
        baseline = groups.get((key[0],key[1],'baseline'), [])
        for row, r in items:
            since = None
            moves = {x['before_command']:x for x in r['transitions'] if 'before_command' in x}
            for command in r['commands']:
                index = command['receipt']['index']
                if index in r['boundaries']: since = 0
                if since is not None: since += 1
                controls = [x['commands'][index]['host_duration_ns']/1e9 for b,x in baseline
                            if b['repeat']==row['repeat'] and b['count_run']==row['count_run']]
                duration = command['host_duration_ns']/1e9
                step_start = moves[index]['host_request_ns'] if index in moves else command['host_send_ns']
                step_duration = (command['host_receive_ns']-step_start)/1e9
                out = {k:row[k] for k in ('fixture','policy','mode','repeat','count_run','disturbed')}
                out.update(index=index, role=command['role'], epoch=command['epoch'], after_move_ordinal=since,
                    duration_s=duration, excess_s=duration-st.mean(controls) if controls else None)
                out.update(step_duration_s=step_duration, step_excess_s=step_duration-st.mean(controls) if controls else None)
                for role in ('A','B'):
                    for kind in ('ept_violation','page_fault','ram_ept_violation','mmio_ept_violation'):
                        out[role+'_'+kind] = command.get('faults',{}).get(role,{}).get(kind)
                out['excess_ept'] = None
                if row['count_run']:
                    base_faults = [sum(x['commands'][index]['faults'][role]['ept_violation'] for role in ('A','B'))
                                   for b,x in baseline if b['repeat']==row['repeat'] and b['count_run']]
                    if base_faults:
                        out['excess_ept'] = out['A_ept_violation']+out['B_ept_violation']-st.mean(base_faults)
                commands.append(out)
    costs = {}
    if args.physical:
        physical = json.loads(args.physical.read_text())
        costs = {mode:st.median(x['us_per_fixed_fault'] for x in physical['rows'] if x['page_mode']==mode) for mode in ('4k','2m')}
    summary = []
    for key, items in sorted(groups.items()):
        times = [row for row,r in items if not row['count_run']]
        counts = [row for row,r in items if row['count_run']]
        base = [row['total_s'] for row,r in groups.get((key[0],key[1],'baseline'), []) if not row['count_run']]
        out = dict(zip(('fixture','policy','mode'),key))
        out.update(n_timing=len(times), n_count=len(counts), disturbed=sum(row['disturbed'] for row,r in items))
        if times and base:
            out.update(total_s_median=st.median(x['total_s'] for x in times),
                       extra_s=st.median(x['total_s'] for x in times)-st.median(base),
                       stop_s_median=st.median(x['stop_s'] for x in times))
            base_active = [b['active_s'] for b,r in groups[(key[0],key[1],'baseline')] if not b['count_run']]
            out.update(active_s_median=st.median(x['active_s'] for x in times),
                       active_extra_s=st.median(x['active_s'] for x in times)-st.median(base_active))
            if key[2].startswith('prefault-'):
                for field in ('prefault_kernel_s','prefault_wall_s','query_kernel_s','query_wall_s','prefault_pages','source_ranges'):
                    out[field+'_median'] = st.median(x[field] for x in times)
            if key[2]=='direct':
                out['native_pause_to_resume_s_median'] = st.median(x['native_pause_to_resume_s'] for x in times)
            excess = [x['excess_s'] for x in commands if (x['fixture'],x['policy'],x['mode'])==key and not x['count_run'] and x['excess_s'] is not None]
            if excess:
                out.update(command_extra_s_median=st.median(excess), command_extra_s_p95=percentile(excess,.95), command_extra_s_max=max(excess))
            step = [x['step_excess_s'] for x in commands if (x['fixture'],x['policy'],x['mode'])==key and not x['count_run'] and x['step_excess_s'] is not None]
            if step:
                out.update(step_extra_s_median=st.median(step), step_extra_s_p95=percentile(step,.95), step_extra_s_max=max(step))
        if counts:
            faults = st.mean(x['ept_violations'] for x in counts)
            moves = counts[0]['moves']
            out.update(ept_mean=faults, page_fault_mean=st.mean(x['page_faults'] for x in counts),
                       whole_interval_ept_mean=st.mean(x['whole_interval_ept'] for x in counts),
                       whole_interval_ram_ept_mean=st.mean(x['whole_interval_ram_ept'] for x in counts),
                       ram_ept_mean=st.mean(x['ram_ept_violations'] for x in counts),
                       mmio_ept_mean=st.mean(x['mmio_ept_violations'] for x in counts),
                       moves=moves, ept_per_move=faults/moves if moves else None,
                       spte=[x['spte'] for x in counts], nested_root_ms_mean=st.mean(x['nested_root_ns'] for x in counts)/1e6)
            if costs:
                out['estimated_physical_4k_s']=out['whole_interval_ram_ept_mean']*costs['4k']/1e6
                out['estimated_command_physical_4k_s']=out['ram_ept_mean']*costs['4k']/1e6
                base_faults = [b['ept_violations'] for b,r in groups.get((key[0],key[1],'baseline'), []) if b['count_run']]
                if base_faults:
                    out['excess_ept_mean'] = faults-st.mean(base_faults)
                    base_ram = [b['ram_ept_violations'] for b,r in groups.get((key[0],key[1],'baseline'), []) if b['count_run']]
                    out['excess_ram_ept_mean'] = out['ram_ept_mean']-st.mean(base_ram)
                    base_whole = [b['whole_interval_ram_ept'] for b,r in groups[(key[0],key[1],'baseline')] if b['count_run']]
                    out['whole_interval_excess_ram_ept_mean'] = out['whole_interval_ram_ept_mean']-st.mean(base_whole)
                    out['estimated_excess_physical_4k_s'] = out['whole_interval_excess_ram_ept_mean']*costs['4k']/1e6
            lag, lag_excess = defaultdict(list), defaultdict(list)
            for x in commands:
                if (x['fixture'],x['policy'],x['mode'])==key and x['count_run'] and x['after_move_ordinal']:
                    lag[x['after_move_ordinal']].append(x['A_ept_violation']+x['B_ept_violation'])
                    if x['excess_ept'] is not None: lag_excess[x['after_move_ordinal']].append(x['excess_ept'])
            out['faults_by_after_move_ordinal'] = {str(k):{'mean':st.mean(v),'sum':sum(v),'n':len(v),
                'excess_over_baseline_mean':st.mean(lag_excess[k]) if lag_excess[k] else None} for k,v in sorted(lag.items())}
        summary.append(out)
    args.output.mkdir(parents=True, exist_ok=True)
    save(args.output/'summary.json', {'physical_us_per_fixed_fault':costs, 'conditions':summary, 'trials':trials,
        'analysis_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'suite_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in args.suites}})
    for name, rows in (('commands', commands), ('conditions', summary), ('trials', trials)):
        flat = [{k:v for k,v in row.items() if not isinstance(v, (dict,list))} for row in rows]
        if flat:
            fields = list(dict.fromkeys(k for row in flat for k in row))
            with (args.output/(name+'.csv')).open('w') as stream:
                writer=csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(flat)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suites',nargs='*',type=Path)
    parser.add_argument('--calibrate',type=Path)
    parser.add_argument('--physical',type=Path)
    parser.add_argument('--repetitions',type=int,default=3)
    parser.add_argument('--seed',type=int,default=20261116)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    calibrate(args) if args.calibrate else summarize(args)


if __name__=='__main__': main()
