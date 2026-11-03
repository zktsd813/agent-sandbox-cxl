#!/usr/bin/env python3
"""Actual SWE command replay through the guest-authorized CXL RAM provider.

Transitions occur between original commands. No synthetic cache warming or RAM
snapshot file is used. The baseline uses the same patched VMM entirely in DRAM.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import time

import lab
import nfs_bench as nfs
from home_storage_bench import Controller, require, json_lines
from pool_bench import Sandbox, MIB
from pool_handoff_bench import HandoffController
from pool_handoff_control import FrameAllocator, promotion_layout
from pool_control import PoolControl
from swe_pool_protocol import parse_registration
from trace_bench import source_fence_script

HERE = Path(__file__).resolve().parent


class SWEController(HandoffController):
    def __init__(self, config, out, args):
        Controller.__init__(self, config, out, args.memory_mib, 1)
        self.args, self.mode = args, args.mode
        self.metadata_base = 8192 * MIB
        self.fixture = json.loads((Path(config['assets']) / 'manifest.json').read_text())
        self.trace = self.fixture['traces'][args.name]
        self.root = '/run/swe-pool-' + os.urandom(8).hex()
        self.sandboxes = [Sandbox(vm=1)]
        s = self.sandboxes[0]
        s.direct, s.manifest, s.owner = self.mode == 'direct', None, None
        self.pool_offset, self.bloom_offset = 4096 * MIB, 3072 * MIB
        self.image_id, self.image_bytes = self.trace['image_sha256'], self.trace['image_bytes']
        self.io_engine = 'Async'
        self.allocator = FrameAllocator(2 * MIB, 3072 * MIB)
        self.pool = PoolControl(self)
        self.commands, self.trace_commands, self.authority_events = [], [], []
        self.memory_events, self.api_events, self.launches, self.import_negatives = [], [], [], []
        self.report_logs, self.guest_traces, self.audits = [], [], []
        self.ready_keys, self.registrations, self.owners = set(), {}, {}
        self.resident_resource_events, self.resident_probes = [], []
        self.resident_readiness, self.resident_preparations = {}, []
        self.serial_number = 0
        self.kernel_profile_offset = 12288 * MIB + 512 if args.restore_timing else None

    def wait_event(self, role, directory, event, label=None, timeout=120):
        pattern = f'"event":"{event}"'
        if label is not None:
            pattern += f',"label":"{label}"'
        complete = re.escape(pattern) + r'.*}[[:space:]]*$'
        raw, name = self.shell(role, f'n=0\nwhile ! grep -Eq {shlex.quote(complete)} {directory}/fc.log; do\n'
            f'read p s < {directory}/fc.pid\nkill -0 "$p"\n'
            f"! grep -Eq 'Kernel panic|TRACE_FATAL|POOL_GUEST_FATAL' {directory}/fc.log\n"
            f'n=$((n+1))\n[ $n -lt {timeout*50} ]\nsleep .02\ndone\n'
            f'grep -E {shlex.quote(complete)} {directory}/fc.log', 'guest', timeout+15)
        rows = [json.loads(line) for line in raw.splitlines() if line.startswith('{"event":')]
        rows = [r for r in rows if r.get('event') == event and (label is None or r.get('label') == label)]
        require(len(rows) == 1 and rows[0].get('ok', True), f'Guest event failed: {raw}')
        return {'file': name, 'receipt': rows[0]}

    def setup(self):
        device = self.config['home_storage_image']['guest_device']
        for role in ('A', 'B'):
            self.shell(role, 'test ! -e /run/home-storage-claimed\nmkdir /run/home-storage-claimed\n' +
                lab.snapshot_cleanup_script(include_boot_vm=True) +
                f'\nmkdir -p {self.root} /storage /disk /remote', 'prepare')
        name = self.trace['name']
        immutable_cache = getattr(self.args, 'immutable_nfs_cache', False)
        self.shell('A', f'mount -t ext4 -o rw,noatime {device} /storage\n'
            f'ln {shlex.quote("/storage/" + name + ".raw")} /storage/sandbox.raw\n'
            + ('mount -o remount,ro /storage\n' if immutable_cache else '') +
            'mount -o bind /storage /disk\nexport PATH=/usr/sbin:/usr/bin:/bin:/sbin:/lab\n'
            'mkdir -p /var/lib/nfs /run/rpcbind /proc/fs/nfsd\nmount -t nfsd nfsd /proc/fs/nfsd\n'
            "printf '/storage 172.30.0.2(ro,sync,no_subtree_check,no_root_squash,fsid=19)\\n' > /etc/exports\n"
            f'rpcbind -f -w > {self.root}/rpcbind.log 2>&1 &\nsleep .2\nexportfs -ra\n'
            f'rpc.mountd --foreground --no-nfs-version 2 --no-nfs-version 4 --port 20048 > {self.root}/mountd.log 2>&1 &\n'
            'rpc.nfsd -V3 -N4 4\nexportfs -v\ncat /proc/mounts', 'home-storage')
        self.shell('B', '/usr/sbin/mount.nfs 172.30.0.1:/storage /remote '
            '-o ro,vers=3,proto=tcp,mountproto=tcp,port=2049,mountport=20048,nolock,hard,timeo=50,retrans=2,'
            + ('actimeo=60,nocto\n' if immutable_cache else 'actimeo=0\n') +
            'mount -o bind /remote /disk\ncat /proc/mounts', 'remote-storage')
        for role in ('A', 'B'):
            self.shell(role, 'insmod /lab/pool_memory_module.ko\ntest -c /dev/cxl_pool\n'
                'echo 1 > /sys/module/kvm/parameters/dax_scattered', 'pool-driver')
        require(self.pool_offset + self.image_bytes <= 8192 * MIB, 'Canonical image exceeds reserved layout')
        raw, receipt = self.shell('A', f'/lab/pool-dax-copy import /disk/sandbox.raw /dev/dax0.0 '
                                 f'{self.pool_offset} {self.image_bytes}', 'canonical-publish', 300)
        require('"result":"passed"' in raw, 'Canonical publication failed')
        self.pool._bloom(self.sandboxes[0], 'zero', [], 1)
        for role in ('A', 'B'):
            self.shell(role, 'sync\necho 3 > /proc/sys/vm/drop_caches', 'initial-host-cache-drop')
        return {'file': receipt, 'bytes': self.image_bytes, 'image_id': self.image_id}

    def configure_direct(self):
        for role, node, mode in (('A', 1, 1), ('B', 2, 2)):
            self.shell(role, 'echo 2147483647 > /sys/module/kvm/parameters/dax_owner_pid\n'
                + ('echo 1 > /sys/module/kvm/parameters/dax_handoff_profile\n' if self.args.restore_timing else '') +
                'resource=$(cat /sys/bus/dax/devices/dax0.0/resource)\n'
                'echo 3 > /sys/module/kvm/parameters/dax_handoff_mode\n'
                'echo 1 > /sys/module/kvm/parameters/dax_nested_root_sync\n'
                'echo 1 > /sys/module/kvm/parameters/dax_scattered\n'
                'echo $resource > /sys/module/kvm/parameters/dax_data_base\n'
                f'echo {8192*MIB} > /sys/module/kvm/parameters/dax_data_bytes\n'
                f'echo {node} > /sys/module/kvm/parameters/dax_node\necho {mode} > /sys/module/kvm/parameters/dax_shared\n'
                f"printf '%x,%x\\n' \"$((resource + {12288*MIB}))\" {128*MIB} > /sys/module/kvm/parameters/dax_pool\n"
                'cat /sys/module/kvm/parameters/dax_pool', 'direct-kernel-setup')

    def boot_vm(self, s):
        self.start_vm(s)
        components = {'/machine-config': {'vcpu_count': 1, 'mem_size_mib': self.memory_mib},
            '/boot-source': {'kernel_image_path': '/lab/l2-vmlinux',
                'initrd_path': f'/lab/{self.trace["name"]}-initramfs.cpio',
                'boot_args': 'console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init '
                             'page_reporting.page_reporting_order=0'},
            '/drives/data': {'drive_id': 'data', 'path_on_host': '/disk/sandbox.raw', 'is_root_device': False,
                'is_read_only': True, 'cache_type': 'Writeback', 'io_engine': self.io_engine},
            '/balloon': {'amount_mib': 0, 'deflate_on_oom': False, 'stats_polling_interval_s': 0,
                'free_page_hinting': False, 'free_page_reporting': True}}
        for path, body in components.items():
            self.api(s.role, s.directory, path, 'PUT', body)
        self.api(s.role, s.directory, '/actions', 'PUT', {'action_type': 'InstanceStart'})
        ready = self.event(s, 'trace_ready')
        require(ready['receipt']['command_count'] == self.trace['command_count'], 'Wrong trace boot')
        return ready

    def trace_command(self, s, operation, *args, event=None, timeout=180):
        self.serial_number += 1
        token = f'v{s.vm}e{s.epoch}c{self.serial_number}'
        command = f'run {args[0]} {token}' if operation == 'run' else ' '.join([operation, token, *map(str,args)])
        self.shell(s.role, f"printf '%s\\n' {shlex.quote(command)} > {s.directory}/input", 'guest-input')
        row = self.wait_event(s.role, s.directory, event or 'trace_identity', token, timeout)
        row.update(vm=s.vm, role=s.role, tier=s.tier, epoch=s.epoch, operation=operation, arguments=list(args))
        self.commands.append(row)
        return row

    def command_vm(self, s, op, *args, require_cache=False):
        require(op in ('identity', 'check'), 'Unexpected synthetic command in SWE workload')
        return self.trace_command(s, 'identity')

    @staticmethod
    def same_identity(before, after):
        a, b = before['receipt'], after['receipt']
        for key in ('boot_id', 'pid', 'next_index', 'sentinel_sha256', 'fd_inode', 'fd_deleted'):
            require(a[key] == b[key], 'Transition changed trace identity ' + key)
        require(a['fd_deleted'] and b['fd_ordinal'] == a['fd_ordinal'] + 1 and
                b['fd_position'] > a['fd_position'], 'Open deleted FD offset did not continue')

    def frame(self, s, row, prefix, kind):
        raw = self.fetch_bytes(s.role, s.directory + '/fc.log', kind + '-serial')
        token = row['receipt']['label']
        match = re.search(rf'{prefix}_BEGIN {re.escape(token)} ([0-9]+) ([0-9a-f]{{64}})\r?\n(.*?){prefix}_END {re.escape(token)}', raw.decode(), re.S)
        require(match is not None, 'Missing guest export frame')
        data = base64.b64decode(''.join(match[3].split()), validate=True)
        require(len(data) == int(match[1]) == row['receipt']['bytes'] and
                hashlib.sha256(data).hexdigest() == match[2] == row['receipt']['sha256'], 'Guest export integrity')
        name = f'{len(self.receipts):04d}-{kind}.bin'
        (self.out / name).write_bytes(data)
        self.receipts.append({'file': name, 'kind': kind, 'sha256': hashlib.sha256(data).hexdigest()})
        return data, name

    def registration(self, s):
        identity = self.command_vm(s, 'identity')
        token = secrets.randbits(63) or 1
        command = self.trace_command(s, 'pool_prepare', s.epoch, token, event='swe_pool_prepared')
        fetched = self.trace_command(s, 'pool_fetch', event='swe_pool_export')
        raw, name = self.frame(s, fetched, 'SWE_POOL_EXPORT', 'guest-registration')
        value = parse_registration(raw.decode(), image_id=self.image_id, epoch=s.epoch, token=token,
            files=self.trace['pool_files'], memory_bytes=self.memory_mib * MIB, image_bytes=self.image_bytes)
        require(command['receipt']['selected_pages'] * 4096 == value['registered_bytes'], 'Authority count mismatch')
        event = {'vm': s.vm, 'epoch': s.epoch, 'role': s.role, 'token': token, 'file': name,
                 'command': command, 'identity': identity, 'summary': value['summary']}
        self.authority_events.append(event)
        self.registrations[s.vm] = value
        return value, event

    def release_registration(self, s, token, abort=False):
        require(not abort, 'SWE workload uses explicit commit on successful transition')
        row = self.trace_command(s, 'pool_release', event='swe_pool_released')
        require(row['receipt']['plan_generation'] == token and row['receipt']['inactive'], 'Registration release mismatch')
        self.registrations.pop(s.vm, None)
        return row

    def promote(self, s):
        require(s.tier == 'dram', 'Promotion requires DRAM')
        value, authority = self.registration(s)
        require(value['registered_bytes'] > 0, 'No naturally resident lower pages in selected whitelist')
        keys = {p['image_offset'] for p in value['pages']}
        hits, probe = self.pool._bloom(s, 'probe', keys, s.vm)
        shared_hits = hits & self.ready_keys
        selected = [dict(p, pool_offset=self.pool_offset+p['image_offset']) for p in value['pages']
                    if p['image_offset'] not in self.ready_keys or p['image_offset'] in shared_hits]
        self.pool._bloom(s, 'build', keys, s.vm)
        self.pause(s)
        owner = f'vm{s.vm}:allocation{authority["token"]}'
        layout = promotion_layout(self.allocator, owner, self.memory_mib*MIB, selected,
                                  self.args.credit_mib*MIB//4096)
        result, operation = self.memory_operation(s, 'promote', registration=value['registration'],
            mappings=layout['mappings'], credits=layout['credits'], allocation_generation=layout['allocation_generation'])
        require(result['shared_bytes'] == layout['shared_bytes'] and result['copied_private_bytes'] == layout['private_bytes']
                and result['file_page_compare_bytes'] == result['ram_snapshot_bytes'] == 0, 'Promotion accounting')
        s.owner, s.tier = owner, 'cxl'
        exported = self.export(s)
        self.ready_keys.update(p['image_offset'] for p in selected)
        self.owners[s.vm] = {'keys': keys, 'epoch': s.epoch, 'tier': 'cxl'}
        self.resume(s)
        released = self.release_registration(s, authority['token'])
        after = self.command_vm(s, 'check')
        self.same_identity(authority['identity'], after)
        row = {'kind': 'promotion', 'vm': s.vm, 'epoch': s.epoch, 'source_tier': 'dram', 'destination_tier': 'cxl',
            'source': s.role, 'destination': s.role, 'authority': authority, 'discovery': probe,
            'operation': operation, 'export': self.export_evidence(exported), 'release': released,
            'after': after, 'allocator': self.allocator.summary(), 'result': 'passed'}
        self.transitions.append(row)
        return row

    def audit_cache(self, s):
        value, authority = self.registration(s)
        self.pause(s)
        raw, operation = self.memory_operation(s, 'audit', registration=value['registration'])
        current = {p['gpa']: p['image_offset'] for p in value['pages']}
        actual = []
        for page in raw['pages']:
            if page['gpa'] in current and page['shared'] and page['present']:
                require(not page['writable'] and page['offset'] == self.pool_offset + current[page['gpa']]
                    and page['actual_pfn']*4096 == raw['phys_base']+page['offset'], 'Actual lower cache PFN mismatch')
                actual.append(page['gpa'])
        self.resume(s)
        released = self.release_registration(s, authority['token'])
        row = {'authority': authority, 'operation': operation, 'release': released,
               'present_shared_bytes': len(actual)*4096, 'registered_bytes': value['registered_bytes']}
        self.audits.append(row)
        return row

    def state_handoff(self, s, destination):
        require(self.mode == 'state-only' and s.tier == 'cxl' and destination != s.role, 'Invalid state handoff')
        source, old_directory, old_epoch = s.role, s.directory, s.epoch
        started = time.monotonic_ns()
        self.pause(s)
        state_path = old_directory + '/state-only.bin'
        # Snapshot creation already drains devices and seals the current mapper.
        create = self.api(source, old_directory, '/snapshot/create', 'PUT',
            {'snapshot_type': 'Full', 'snapshot_path': state_path, 'mem_file_path': '/dev/dax0.0'})
        state = self.fetch_bytes(source, state_path, 'state-only')
        state_file = self.receipts[-1]['file']
        manifest = self.fetch_bytes(source, s.manifest_path, 'state-bound-manifest')
        manifest_file = self.receipts[-1]['file']
        require(0 < len(state) < MIB, 'Unexpected state-only payload size')
        stopped = self.raw('state-source-stop', nfs.stop_fc(self.vms[source], old_directory))
        s.epoch += 1
        s.role = destination
        target = f'{self.root}/vm{s.vm}-epoch{s.epoch}'
        self.shell(destination, f'mkdir -p {target}', 'destination-directory')
        self.write_bytes(destination, target+'/state-only.bin', state, 'state-only-transfer')
        self.write_bytes(destination, target+'/incoming-manifest.json', manifest, 'placement-transfer')
        self.start_vm(s, restore_manifest=target+'/incoming-manifest.json')
        # Restore consumes the sealed handle and resumes only after validation.
        load = self.api(destination, s.directory, '/snapshot/load', 'PUT', {'snapshot_path': target+'/state-only.bin',
            'mem_backend': {'backend_type': 'File', 'backend_path': '/dev/dax0.0'}, 'resume_vm': True})
        s.manifest = None
        row = {'kind': 'state-only', 'runtime_path': 'state-v2', 'vm': s.vm, 'source': source, 'destination': destination,
            'source_epoch': old_epoch, 'destination_epoch': s.epoch, 'source_tier': 'cxl', 'destination_tier': 'cxl',
            'source_stop_file': stopped, 'create_api': create, 'load_api': load,
            'state_file': state_file, 'state_bytes': len(state),
            'manifest_file': manifest_file, 'manifest_bytes': len(manifest), 'ram_snapshot_bytes': 0,
            'ram_transfer_bytes': 0, 'controller_pause_to_resume_ns': time.monotonic_ns()-started, 'result': 'passed'}
        self.transitions.append(row)
        return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('baseline','state-only','direct'), required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--memory-mib', type=int, default=2048)
    parser.add_argument('--credit-mib', type=int, default=256)
    parser.add_argument("--full-audit", action="store_true")
    parser.add_argument("--restore-timing", action="store_true")
    parser.add_argument("--resume-nokick", action="store_true")
    parser.add_argument("--exec-request", action="store_true")
    parser.add_argument("--alternate-request", action="store_true",help="Diagnostic comparison: alternate relay/exec every roundtrip")
    parser.add_argument("--resident-direct", action="store_true")
    parser.add_argument("--resident-fast-control", action="store_true")
    parser.add_argument("--resident-ready-control", action="store_true")
    parser.add_argument("--resident-repromote-handoff", action="store_true")
    parser.add_argument("--immutable-nfs-cache", action="store_true")
    parser.add_argument("--direct-profile-roundtrips", type=int, default=0)
    args = parser.parse_args()
    if args.mode == 'direct':
        args.resident_direct = args.resident_fast_control = args.resident_ready_control = args.immutable_nfs_cache = True
    require(not args.resident_fast_control or args.resident_direct, 'TCP control requires resident direct')
    require(not args.resident_ready_control or args.resident_fast_control, 'Prepared RPC control requires TCP control')
    require(not args.resident_direct or (args.mode == 'direct' and args.immutable_nfs_cache),
            'Resident direct requires immutable NFS and direct mode')
    require(0 <= args.direct_profile_roundtrips <= 20 and
            (not args.direct_profile_roundtrips or args.mode == 'direct'), 'Invalid direct profile repetitions')
    require(args.memory_mib == 2048 and args.credit_mib == 256, 'Use the bounded 2GiB/256MiB workload layout')
    run, config = lab.current_run()
    require(config['cxl_mib'] == 16384 and bool(config.get('dax_handoff')) == (args.mode == 'direct'), 'Wrong fresh pair mode/capacity')
    out = run / ('swe-pool-' + str(time.time_ns()))
    out.mkdir()
    c = SWEController(config, out, args)
    s = c.sandboxes[0]
    report = {'schema': 'swe-pool-trace-v2', 'result': 'running', 'mode': args.mode, 'name': args.name,
        'arguments': dict(vars(args)), 'config': config, 'fixture': c.fixture, 'trace': c.trace,
        'commands': c.trace_commands, 'control_commands': c.commands, 'receipts': c.receipts,
        'transitions': c.transitions, 'authority_events': c.authority_events, 'memory_events': c.memory_events,
        'api_events': c.api_events, 'launches': c.launches, 'resident_negatives':c.import_negatives,
        'resident_resources':c.resident_resource_events, 'resident_probes':c.resident_probes,
        'resident_readiness':c.resident_readiness, 'resident_preparations':c.resident_preparations,
        'allocator_events': c.allocator.events, 'audits': c.audits,
        'layout': {'private_start': 2*MIB, 'private_end': 3072*MIB, 'bloom': 3072*MIB,
                   'canonical_start': 4096*MIB, 'canonical_bytes': c.image_bytes, 'kvm_arena': 12288*MIB},
        'limitations': ['Coherent nested KVM; one run per trace/mode', 'Transitions between recorded commands',
            'Same patched DRAM baseline, not pristine Firecracker', 'Natural resident pages in at most16 selected lower files',
            'One VM per trial: no multi-VM memory savings claim; full canonical image preloaded and counted']}
    sources = ('swe_pool_bench.py','swe_pool_protocol.py','swe-pool-l2-init.sh','swe-pool-guest-run.sh',
        'resident_fast.py','resident_control.c','resident_wire.h','resident_proxy.py','resident_bootstrap.c','control_rpc.c','control_rpc.py','guest-init.sh','guest-pool-cli.c','guest-pool.c','guest-pool-uapi.h','pool_handoff_bench.py','pool_handoff_control.py',
        'pool_bench.py','pool_control.py','home_storage_bench.py','nfs_bench.py','trace_bench.py','lab.py')
    (out/'measured-sources').mkdir()
    for name in sources:
        shutil.copy2(HERE/name,out/'measured-sources'/name)
    report['source_hashes'] = {name: lab.sha256(out/'measured-sources'/name) for name in sources}
    source = Path(config['assets'])/'traces'/f'{args.name}.traj.json'
    shutil.copy2(source,out/'source.traj.json')
    report['source_trace'] = {'file':'source.traj.json','sha256':lab.sha256(source)}
    def save():
        lab.save_json(out/'report.json',report)
    save()
    try:
        lab.verify(argparse.Namespace(timeout=180))
        report['pool_setup'] = c.setup()
        if args.mode == 'direct':
            c.configure_direct()
        report['boot'] = c.boot_vm(s)
        report['initial_identity'] = c.command_vm(s,'identity')
        first, second = c.trace['handoff_before'], c.trace['command_count']-2
        report['boundaries'] = [first,second]
        started = time.monotonic_ns()
        for command in c.trace['commands']:
            index = command['index']
            if args.mode != 'baseline' and index in (first,second):
                if index == first:
                    c.promote(s)
                    if args.resident_ready_control:
                        from resident_fast import prepare
                        prepare(c, s)
                    for _ in range(args.direct_profile_roundtrips):
                        c.direct_handoff(s, 'B')
                        c.direct_handoff(s, 'A')
                        save()
                    (c.direct_handoff if s.direct else c.state_handoff)(s,'B')
                else:
                    if args.full_audit: c.audit_cache(s)
                    (c.direct_handoff if s.direct else c.state_handoff)(s,'A')
                    c.demote(s)
                save()
            row = c.trace_command(s,'run',index,event='trace_end',timeout=int(c.trace['environment'].get('timeout',60))+30)
            value = row['receipt']
            require(not value['timed_out'] and value['command_sha256'] == command['command_sha256'] and
                    value['rc'] == (command['expected_returncode'] if command['expected_returncode'] is not None else 0),
                    f'Trace command diverged at {index}: {value}')
            row.update(guest_duration_s=value['end_s']-value['start_s'])
            c.trace_commands.append(row)
            save()
        if args.mode != 'baseline':
            c.promote(s)
            if args.resident_repromote_handoff:
                c.direct_handoff(s, 'B')
                c.direct_handoff(s, 'A')
            if args.full_audit: c.audit_cache(s)
            c.demote(s)
        report['final_identity'] = c.command_vm(s,'identity')
        exported = c.trace_command(s,'export',event='trace_export',timeout=180)
        _, archive = c.frame(s,exported,'TRACE_EXPORT','guest-archive')
        report['guest_archive'], report['guest_archive_sha256'] = archive, exported['receipt']['sha256']
        report['guest_export'] = exported
        report['controller_replay_ns'] = time.monotonic_ns()-started
        report['guest_duration_s'] = sum(x['guest_duration_s'] for x in c.trace_commands)
        report['runtime_audits'] = False
        report['allocator_final'] = c.allocator.summary()
        require(report['allocator_final']['allocated_bytes'] == 0, 'CXL grant leak')
        report['result'] = 'passed'
    except BaseException as exc:
        report.update(result='failed',error=repr(exc))
        raise
    finally:
        report['controller_cleanup'] = []
        for role, server in getattr(c, 'resident_servers', {}).items():
            nfs.stop_fc(c.vms[role], server['directory'])
            raw, name = c.shell(role,source_fence_script(server['directory']),'controller-final-fence')
            report['controller_cleanup'].append({**server, 'role':role, 'prepare_file':server['file'], 'file':name, 'fenced':'FENCED ' in raw})
        report['cleanup'] = []
        for role, directory in reversed(c.active):
            try:
                nfs.stop_fc(c.vms[role],directory)
                raw, name = c.shell(role,source_fence_script(directory),'final-fence')
                c.fetch_bytes(role,directory+'/fc.log','final-guest-log')
                c.fetch_bytes(role,directory+'/vmm.log','final-vmm-log')
                report['cleanup'].append({'role':role,'directory':directory,'file':name,'fenced':'FENCED ' in raw})
            except BaseException as exc:
                report['cleanup'].append({'role':role,'directory':directory,'error':repr(exc)})
        if not report['cleanup'] or not all(row.get('fenced') for row in report['cleanup']):
            report['result'] = 'failed'
        save()
        print('SWE_POOL_REPORT '+str(out/'report.json'),flush=True)


if __name__ == '__main__':
    main()
