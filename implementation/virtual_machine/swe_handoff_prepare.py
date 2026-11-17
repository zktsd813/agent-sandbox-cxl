#!/usr/bin/env python3
"""Package trace assets for private RAM tier changes and resident handoff.

Inputs remain read-only. The output directory must be new. Original command
bytes, environment and image hashes come from trace_prepare.py's manifest.
"""
import argparse
import copy
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent


def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, **kwargs)


def unpack(archive, directory):
    directory.mkdir()
    directory.chmod(0o755)
    with archive.open('rb') as stream:
        run(['cpio', '-id', '--quiet', '--no-absolute-filenames'], cwd=directory, stdin=stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-assets', type=Path, required=True)
    parser.add_argument('--link-storage', action='store_true', help='Reference the read-only storage template without copying it')
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--firecracker', type=Path, required=True)
    parser.add_argument('--guest-kernel', type=Path, required=True)
    parser.add_argument('--guest-config', type=Path, required=True)
    parser.add_argument('--mapper-module', type=Path, required=True)
    parser.add_argument('--kvm-module-dir', type=Path, required=True)
    parser.add_argument('--l1-kernel', type=Path, required=True)
    parser.add_argument('--io-fixture', type=Path)
    parser.add_argument('--shared-probe', type=Path)
    parser.add_argument('--l1-config', type=Path, help='Require built-in NFS server support and record the L1 config')
    args = parser.parse_args()
    if args.l1_config and 'CONFIG_NFSD=y' not in args.l1_config.read_text().splitlines():
        raise ValueError('L1 needs built-in CONFIG_NFSD=y for the NFS fixture')
    base = args.trace_assets.resolve(strict=True)
    dest = args.assets.resolve()
    if dest.exists():
        raise FileExistsError(dest)
    manifest = copy.deepcopy(json.loads((base/'manifest.json').read_text()))
    config = set(args.guest_config.read_text().splitlines())
    for setting in ('CONFIG_EXT4_FS=y', 'CONFIG_OVERLAY_FS=y', 'CONFIG_TMPFS=y',
                    'CONFIG_TMPFS_XATTR=y', 'CONFIG_VIRTIO_BLK=y'):
        if setting not in config:
            raise ValueError('Missing guest configuration: '+setting)
    # Verify all borrowed inputs before creating output. No guest registration
    # or image residency selection is involved in whole-RAM promotion.
    for name, digest in manifest['artifacts'].items():
        if lab.sha256(base/name) != digest:
            raise ValueError('Changed trace asset: '+name)
    for name, trace in manifest['traces'].items():
        source = base/'traces'/(name+'.traj.json')
        if lab.sha256(source) != trace['trace_sha256']:
            raise ValueError('Changed source trace: '+name)
    dest.mkdir(parents=True)
    for path in base.iterdir():
        if path.name == 'storage.raw' and args.link_storage:
            (dest/path.name).symlink_to(path.resolve())
        elif path.name != 'manifest.json':
            run(['cp', '-a', '--sparse=always', '--reflink=auto', path, dest/path.name])
    replacements = {'firecracker': args.firecracker, 'l2-vmlinux': args.guest_kernel,
                    'l1-kernel': args.l1_kernel, 'pool_memory_module.ko': args.mapper_module}
    for name, source in replacements.items():
        shutil.copy2(source, dest/name)
    if args.l1_config:
        shutil.copy2(args.l1_config, dest/'l1-kernel.config')
    shutil.copy2(args.guest_config, dest/'l2-handoff.config')
    commands = {}
    for source, binary in (('resident_control.c', 'resident-control'), ('control_rpc.c', 'control-rpc'),
                           ('resident_bootstrap.c', 'resident-bootstrap')):
        commands[binary] = ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', HERE/source, '-o', dest/binary]
        run(commands[binary])
    if args.shared_probe:
        shutil.copy2(args.shared_probe, dest/'shared-probe')
    with tempfile.TemporaryDirectory(prefix='swe-handoff-build-', dir=dest) as tmp:
        temporary = Path(tmp)
        host = temporary/'l1'
        unpack(base/'l1-initramfs.cpio', host)
        for name in ('firecracker', 'l2-vmlinux', 'pool_memory_module.ko',
                     'resident-control', 'resident-bootstrap', 'control-rpc'):
            shutil.copy2(dest/name, host/'lab'/name)
        shutil.copy2(dest/'shared-probe', host/'lab/shared-probe')
        if args.io_fixture: shutil.copy2(args.io_fixture, host/'lab/io-mix')
        shutil.copy2(HERE/'guest-init.sh', host/'init')
        (host/'init').chmod(0o755)
        modules = {}
        for name in ('kvm.ko', 'kvm-intel.ko'):
            source = args.kvm_module_dir/name
            shutil.copy2(source, host/'modules'/name)
            modules[name] = lab.sha256(source)
        for name, trace in manifest['traces'].items():
            guest = temporary/name
            archive = name+'-initramfs.cpio'
            unpack(base/archive, guest)
            shutil.copy2(dest/'shared-probe', guest/'lab/shared-probe')
            if args.io_fixture: shutil.copy2(args.io_fixture, guest/'lab/io-mix')
            shutil.copy2(HERE/'swe-handoff-l2-init.sh', guest/'init')
            (guest/'init').chmod(0o755)
            shutil.copy2(HERE/'swe-handoff-guest-run.sh', guest/'lab/swe-handoff-guest-run.sh')
            build = 'go build -a -p "$(getconf _NPROCESSORS_ONLN)" ./...'
            argv = ["/trace/busybox", "env", "-i", *[f"{k}={v}" for k,v in trace['effective_environment'].items()],
                    *trace['environment']['interpreter'], build]
            build_path = guest/'lab/trace/parallel-build.sh'
            build_path.write_text('#!/trace/busybox sh\ncd /testbed || exit 125\nexec '+shlex.join(argv)+'\n')
            build_path.chmod(0o755)
            for command in trace['commands']:
                path = guest/'lab/trace/cmd'/f"{command['index']:03d}.command"
                if path.read_text() != command['command'] or lab.sha256(path) != command['command_sha256']:
                    raise ValueError('Changed original command bytes')
            lab.pack_initramfs(guest, dest/archive)
            shutil.copy2(dest/archive, host/'lab'/archive)
            trace['initramfs_path'] = str(dest/archive)
        lab.pack_initramfs(host, dest/'l1-initramfs.cpio')
    manifest.update(schema='cxl-swe-handoff-fixture-v1', base_assets=str(base),
                    base_manifest_sha256=lab.sha256(base/'manifest.json'))
    manifest['inputs'].update({key: {'path': str(path.resolve()), 'sha256': lab.sha256(path)}
        for key, path in (('firecracker', args.firecracker), ('l1_kernel', args.l1_kernel), ('l2_kernel', args.guest_kernel))})
    manifest['inputs']['kvm_modules'] = modules
    if args.shared_probe:
        # The inherited probe source belongs to the base fixture, not this
        # replacement. Keep that provenance and hash the actual replacement.
        if 'probe_source' in manifest['inputs']:
            manifest['inputs']['base_probe_source'] = manifest['inputs'].pop('probe_source')
        manifest['source_hashes'].pop('shared_probe.c', None)
        manifest['inputs']['shared_probe_binary'] = {
            'path': str(args.shared_probe.resolve()), 'sha256': lab.sha256(args.shared_probe)}
    if args.io_fixture:
        manifest['inputs']['io_fixture'] = {'path':str(args.io_fixture.resolve()),'sha256':lab.sha256(args.io_fixture),'source_sha256':lab.sha256(HERE/'io_mix.c')}
    manifest['handoff'] = {'inputs': {name: {'path': str(path.resolve()), 'sha256': lab.sha256(path)}
                                     for name, path in replacements.items()}}
    manifest['resident_control'] = {'source_sha256': lab.sha256(HERE/'resident_control.c'),
        'wire_sha256': lab.sha256(HERE/'resident_wire.h'), 'binary_sha256': lab.sha256(dest/'resident-control'),
        'build_argv': [str(x) for x in commands['resident-control']]}
    manifest['control_rpc'] = {'operations': [1, 2, 3], 'binary_sha256': lab.sha256(dest/'control-rpc')}
    manifest['source_hashes'].update({name: lab.sha256(HERE/name) for name in
        ('swe_handoff_prepare.py', 'swe-handoff-l2-init.sh', 'swe-handoff-guest-run.sh')})
    manifest['artifacts'] = {p.name: lab.sha256(p) for p in dest.iterdir() if p.is_file()}
    lab.save_json(dest/'manifest.json', manifest)
    print(json.dumps({'assets': str(dest), 'traces': list(manifest['traces'])}))


if __name__ == '__main__':
    main()
