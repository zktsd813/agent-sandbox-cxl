"""Read-only host observations for the noncoherent tail experiment."""
import json
from pathlib import Path
import signal
import subprocess
import time


def host_state(vms):
    import lab
    tasks = {}
    for role, vm in vms.items():
        if '_profile_tids' not in vm:
            vm['_profile_tids'] = [row['thread-id'] for row in lab.qmp(vm['qmp'],'query-cpus-fast')]
        for task in Path(f"/proc/{vm['pid']}/task").iterdir():
            try:
                name = (task/'comm').read_text().strip()
                if int(task.name) in vm['_profile_tids']:
                    tasks[f'{role}:{task.name}'] = dict(name=name,
                        schedstat=list(map(int, (task/'schedstat').read_text().split())))
            except FileNotFoundError:
                continue
    vmstat = {k:int(v) for k,v in (line.split() for line in Path('/proc/vmstat').read_text().splitlines())
              if k.startswith(('pgscan','pgsteal','allocstall','compact','pgmajfault','pswp'))}
    return dict(mono_ns=time.monotonic_ns(), wall_ns=time.time_ns(), tasks=tasks, vmstat=vmstat,
        cpu_stat=Path('/proc/stat').read_text(), schedstat=Path('/proc/schedstat').read_text(),
        loadavg=Path('/proc/loadavg').read_text(), node0=Path('/sys/devices/system/node/node0/meminfo').read_text())


def start_perf(vms, output):
    tids = [key.split(':')[1] for key in host_state(vms)['tasks']]
    perf = '/usr/lib/linux-tools/6.8.0-149-generic/perf'
    argv = [perf,'record','--clockid','monotonic','-F','499','-e','cycles','-g','-t',','.join(tids),'-o',str(output/'perf.data')]
    (output/'perf-command.json').write_text(json.dumps(argv)+'\n')
    log = (output/'perf.log').open('wb')
    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
    return process, log


def stop_perf(handle):
    process, log = handle
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    log.close()
    return process.returncode
