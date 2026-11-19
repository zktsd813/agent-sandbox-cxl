#!/usr/bin/env python3
"""Run an EPT worker trial in a fresh, owned two-L1 lab and preserve its evidence."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import lab

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NAMES = ('gin-gonic__gin-3741', 'php-cs-fixer__php-cs-fixer-8331', 'micropython__micropython-13039')


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assets', type=Path, required=True)
    p.add_argument('--qemu', type=Path, required=True)
    p.add_argument('--qemu-root', type=Path)
    p.add_argument('--session', required=True)
    p.add_argument('--name', choices=NAMES, default=NAMES[0])
    p.add_argument('--vcpus', type=int, choices=(1, 2), default=1)
    p.add_argument('--l1-memory-mib', type=int, default=4096)
    p.add_argument('--cxl-mib', type=int, choices=(4096, 8192, 16384), default=4096)
    p.add_argument('--heavy-plan', type=Path, default=ROOT/'docs/results/trajectory-faults-20261116/heavy.json')
    p.add_argument('--workload', choices=('idle', 'busy', 'dirty', 'race'), help='Use a checksum workload instead of SWE replay')
    p.add_argument('--iterations', type=int, default=2)
    p.add_argument('--warmup', type=int, default=0)
    p.add_argument('--dirty-mib', type=int, choices=(16, 256, 1024), default=256)
    p.add_argument('--dirty-lines', type=int, choices=(0, 1, 8, 64), default=0)
    p.add_argument('--baseline', action='store_true', help='Same worker path; rebuild an empty local EPT')
    p.add_argument('--cache-maintenance', action=argparse.BooleanOptionalAction, default=True)
    p.add_argument('--native', action=argparse.BooleanOptionalAction, default=True)
    p.add_argument('--preclean', action='store_true')
    p.add_argument('--clean-wait', choices=('0', '1'), default='0')
    p.add_argument('--dirty-oracle', action='store_true')
    p.add_argument('--page-hash', type=Path)
    p.add_argument('--count-faults', action='store_true')
    p.add_argument('--measure', action='store_true', help='Take the exclusive quiet lock and record host load')
    p.add_argument('--fail', choices=('outside', 'bump', 'ept-invalidate', 'ram-invalidate','dram-release','demote-copy','demote-record','tier-flush'))
    p.add_argument('--tier-chunks', default='0', help='Count, half, or written (previous SWE dirty records)')
    p.add_argument('--tier-refault',choices=('lazy','eager'),default='lazy')
    p.add_argument('--tier-start', type=int, default=0)
    p.add_argument('--tier-pre-demote', action='store_true')
    p.add_argument('--tier-history', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not re.fullmatch(r'[a-z0-9_-]{1,32}', args.session):
        p.error('Use a fresh lowercase lab session name, at most 32 characters')
    if args.dirty_oracle and not args.page_hash:
        p.error('Oracle requires --page-hash')
    if args.preclean and not args.cache_maintenance:
        p.error('Preclean requires cache maintenance')
    return args


def run(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, CXL_LAB_ASSETS=str(args.assets.resolve()), CXL_LAB_SESSION=args.session)
    if args.qemu_root:
        env['CXL_LAB_QEMU_ROOT'] = str(args.qemu_root.resolve())
    prefix = [sys.executable, str(HERE/'lab.py')]
    def command(argv, name, timeout):
        (out/(name+'-argv.json')).write_text(json.dumps(argv, indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:
            return subprocess.run(argv, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    config = None
    # Order is LAB then QUIET. Builds take only QUIET shared; no held-lock cycle.
    with (ROOT/'.codex-runs/lab.lock').open('a') as gate, (ROOT/'.codex-runs/quiet.lock').open('a') as quiet:
        fcntl.flock(gate, fcntl.LOCK_EX)
        fcntl.flock(quiet, fcntl.LOCK_EX if args.measure else fcntl.LOCK_SH)
        hardware = subprocess.check_output(['numactl', '--hardware'], text=True)
        free = int(re.search(r'node 0 free: (\d+) MB', hardware)[1])
        if free < 2*args.l1_memory_mib+args.cxl_mib+64+4096:
            raise RuntimeError('node 0 free memory below lab budget plus 4 GiB')
        processes = subprocess.check_output(['ps', '-eo', 'pid,ppid,comm,args'], text=True)
        peers = [line for line in processes.splitlines() if len(line.split()) > 2
                 and (line.split()[2] == 'firecracker' or line.split()[2].startswith('qemu-system'))]
        (out/'host-before.json').write_text(json.dumps(dict(time=time.time(), loadavg=Path('/proc/loadavg').read_text(),
            processes=peers, node0_hardware=hardware, node0_free_mib=free, measured=args.measure), indent=2)+'\n')
        try:
            command([*prefix, 'start', '--qemu', str(args.qemu.resolve()), '--vcpus', str(max(4, args.vcpus+2)),
                '--memory-mib', str(args.l1_memory_mib), '--cxl-mib', str(args.cxl_mib),
                '--home-storage-image', str(args.assets.resolve()/'storage.raw'), '--pml', 'on', *(['--tier-oracle'] if args.dirty_oracle and args.tier_chunks!='0' else [])], 'start', 180).check_returncode()
            current = HERE/'.local'/('current-'+args.session+'.json')
            folder = Path(json.loads(current.read_text())['run'])
            config = json.loads((folder/'run.json').read_text())
            bench = [*prefix, 'ept-adopt-bench', '--output', str(out/'trial'), '--name', args.name,
                '--vcpus', str(args.vcpus), '--iterations', str(args.iterations), '--warmup', str(args.warmup),
                '--dirty-mib', str(args.dirty_mib), '--dirty-lines', str(args.dirty_lines), '--clean-wait', args.clean_wait]
            bench += ['--tier-refault',args.tier_refault,'--tier-chunks',args.tier_chunks,'--tier-start',str(args.tier_start)]
            if args.tier_pre_demote:bench+=['--tier-pre-demote']
            if args.tier_history:bench+=['--tier-history',str(args.tier_history.resolve())]
            bench += ['--workload' , args.workload] if args.workload else ['--heavy-plan', str(args.heavy_plan.resolve())]
            for name in ('baseline', 'native', 'preclean', 'dirty_oracle', 'count_faults'):
                if getattr(args, name):
                    bench += ['--'+name.replace('_', '-')]
            if not args.cache_maintenance:
                bench += ['--no-cache-maintenance']
            if args.page_hash:
                bench += ['--page-hash', str(args.page_hash.resolve())]
            if args.fail:
                bench += ['--fail', args.fail]
            result = command(bench, 'trial', 3600)
            report = json.loads((out/'trial/report.json').read_text())
            if result.returncode or report['result'] != 'passed':
                raise RuntimeError(f"EPT trial failed: {report.get('error', report.get('oracle_error'))}")
        finally:
            command([*prefix, 'stop'], 'stop', 60)
            if config:
                (out/'outer-lab.json').write_text(json.dumps(config, indent=2)+'\n')
                if any(lab.alive(vm) for vm in config['vms'].values()):
                    raise RuntimeError('Owned QEMU survived cleanup')
                if Path(config['ephemeral_memory_dir']).exists():
                    raise RuntimeError('Owned memory backing survived cleanup')
    print(json.dumps(dict(result='passed', report=str(out/'trial/report.json'))))


if __name__ == '__main__':
    run(arguments())
