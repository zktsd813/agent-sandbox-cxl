#!/usr/bin/env bash
# Build/extract dependencies for isolated L1 NFS experiments; no host services.
set -euo pipefail

lab_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
nfs_out="${CXL_NFS_OUT:-$lab_dir/.local/nfs-deps}"
nfs_cache="${CXL_NFS_KERNEL_CACHE:-}"
nfs_source="${CXL_NFS_KERNEL_SOURCE:-$lab_dir/../linux}"
nfs_cpus="${CXL_NFS_CPUS:-0-5}"
nfs_jobs="${JOBS:-6}"

if [[ "${1:-}" == --help ]]; then
    cat <<'USAGE'
Usage: nfs_dependencies.sh

Builds an isolated NFSv3-server kernel and extracts NFS tools plus their shared
libraries into a tree suitable for the lab L1 initramfs. Does not install host
packages, load host modules, modify /etc, or start any host service.

Environment:
  CXL_NFS_KERNEL_SOURCE  Prepared Linux source with the lab CXL patches.
  CXL_NFS_KERNEL_CACHE   Existing compatible O= build tree to COPY (optional).
  CXL_NFS_OUT            Output directory (default .local/nfs-deps).
  CXL_NFS_CPUS           CPU affinity (default 0-5); JOBS defaults to 6.

Requires Ubuntu/Debian apt metadata and gcc/make/rsync/python3/ldd/depmod.
Outputs kernel-build/, kernel-modules/, runtime/, and manifest.json.
USAGE
    exit 0
fi
[[ $# == 0 ]] || { echo 'Only --help is accepted.' >&2; exit 2; }
nfs_source="$(realpath "$nfs_source")"
[[ -f "$nfs_source/Makefile" && -x "$nfs_source/scripts/config" ]]
mkdir -p "$nfs_out"
nfs_out="$(realpath "$nfs_out")"
if [[ -f "$nfs_out/manifest.json" ]]; then
    echo "Refusing to overwrite completed dependencies: $nfs_out; choose a new CXL_NFS_OUT." >&2
    exit 1
fi
nfs_build="$nfs_out/kernel-build"
nfs_modules="$nfs_out/kernel-modules"
nfs_packages="$nfs_out/packages"
nfs_extract="$nfs_out/package-root"
nfs_runtime="$nfs_out/runtime"

if [[ ! -f "$nfs_build/.nfs-cache-copy-complete" ]]; then
    mkdir -p "$nfs_build"
    if [[ -n "$nfs_cache" ]]; then
        nfs_cache="$(realpath "$nfs_cache")"
        [[ "$nfs_cache" != "$nfs_build" && -f "$nfs_cache/.config" ]]
        # No hard links: configuring this build must never change the cache.
        cp -a --reflink=auto "$nfs_cache/." "$nfs_build/"
    else
        cp "$lab_dir/kernel.config" "$nfs_build/.config"
    fi
    touch "$nfs_build/.nfs-cache-copy-complete"
fi
"$nfs_source/scripts/config" --file "$nfs_build/.config" \
    --enable NFSD --disable NFSD_V2 --disable NFSD_V3_ACL --disable NFSD_V4 \
    --enable NFS_FS --enable NFS_V3 --enable VIRTIO_BLK --enable EXT4_FS \
    --enable OVERLAY_FS --enable TMPFS --enable TMPFS_XATTR
taskset -c "$nfs_cpus" make -C "$nfs_source" O="$nfs_build" LOCALVERSION= olddefconfig
taskset -c "$nfs_cpus" make -C "$nfs_source" O="$nfs_build" LOCALVERSION= -j"$nfs_jobs" bzImage modules
mkdir -p "$nfs_modules"
taskset -c "$nfs_cpus" make -C "$nfs_source" O="$nfs_build" LOCALVERSION= \
    INSTALL_MOD_PATH="$nfs_modules" INSTALL_MOD_STRIP=1 modules_install
nfs_release="$(cat "$nfs_build/include/config/kernel.release")"
depmod -b "$nfs_modules" "$nfs_release"
rg -q '^CONFIG_NFSD=y$' "$nfs_build/.config"

mkdir -p "$nfs_packages" "$nfs_extract" "$nfs_runtime"
(
    cd "$nfs_packages"
    apt-get download nfs-kernel-server nfs-common rpcbind libwrap0 libnfsidmap1
)
for nfs_name in nfs-kernel-server nfs-common rpcbind libwrap0 libnfsidmap1; do
    nfs_matches=("$nfs_packages/${nfs_name}_"*.deb)
    [[ ${#nfs_matches[@]} == 1 && -f "${nfs_matches[0]}" ]] || {
        echo "Ambiguous package versions for $nfs_name; choose a new CXL_NFS_OUT." >&2
        exit 1
    }
done
for nfs_package in "$nfs_packages"/*.deb; do
    dpkg-deb -x "$nfs_package" "$nfs_extract"
done

python3 - "$nfs_out" "$nfs_source" "$nfs_release" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

out, source, release = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
extract, runtime = out / 'package-root', out / 'runtime'
tools = ['usr/sbin/rpc.nfsd', 'usr/sbin/rpc.mountd', 'usr/sbin/exportfs',
         'sbin/rpcbind', 'sbin/mount.nfs', 'usr/bin/rpcinfo']
search = [extract / name for name in ('lib/x86_64-linux-gnu', 'usr/lib/x86_64-linux-gnu', 'lib', 'usr/lib')]
env = dict(os.environ, LD_LIBRARY_PATH=':'.join(str(p) for p in search))
copied = {}

def copy_file(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest, follow_symlinks=True)
    copied[str(dest.relative_to(runtime))] = str(src)

for tool in tools:
    origin = extract / tool
    if not origin.is_file():
        matches = [extract / prefix / Path(tool).name
                   for prefix in ('usr/sbin', 'sbin', 'usr/bin', 'bin')
                   if (extract / prefix / Path(tool).name).is_file()]
        if len(matches) != 1:
            raise RuntimeError(f'Missing or ambiguous NFS tool: {tool}')
        origin = matches[0]
    # Normalize commands to /usr/sbin; init scripts can call absolute paths.
    target = runtime / 'usr/sbin' / origin.name
    copy_file(origin, target)
    deps = subprocess.run(['ldd', str(origin)], env=env, text=True, capture_output=True, check=True).stdout
    if 'not found' in deps:
        raise RuntimeError(f'Unresolved dependencies for {origin}:\n{deps}')
    for match in re.finditer(r'(?:=>\s+)?(/[^\s]+)\s+\(', deps):
        dependency = Path(match[1])
        try:
            relative = dependency.relative_to(extract)
        except ValueError:
            relative = dependency.relative_to('/')
        copy_file(dependency, runtime / relative)

for name in ('netconfig', 'services', 'protocols'):
    copy_file(Path('/etc') / name, runtime / 'etc' / name)
(runtime / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n'
                                  'nobody:x:65534:65534:nobody:/:/bin/false\n'
                                  '_rpc:x:105:105:RPC:/run/rpcbind:/bin/false\n')
(runtime / 'etc/group').write_text('root:x:0:\nnogroup:x:65534:\n_rpc:x:105:\n')
(runtime / 'etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nhosts: files\n')
(runtime / 'etc/hosts').write_text('127.0.0.1 localhost\n172.30.0.1 nfs-a\n172.30.0.2 nfs-b\n')
for directory in ('run/rpcbind', 'var/lib/nfs', 'var/lib/rpcbind', 'proc/fs/nfsd'):
    (runtime / directory).mkdir(parents=True, exist_ok=True)

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

manifest = {'kernel_source': str(source), 'kernel_release': release,
            'kernel_config_sha256': sha(out / 'kernel-build/.config'),
            'kernel_sha256': sha(out / 'kernel-build/arch/x86/boot/bzImage'),
            'vmlinux_sha256': sha(out / 'kernel-build/vmlinux'),
            'nfs_server': 'Linux kernel NFSD built-in, NFSv3; NFSD_V2/NFSD_V4 disabled',
            'packages': [], 'runtime_files': {},
            'host_changes': 'No package installation, host module loading, host services or /etc changes'}
for package in sorted((out / 'packages').glob('*.deb')):
    metadata = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture'], text=True)
    manifest['packages'].append({'file': package.name, 'metadata': metadata.strip(), 'sha256': sha(package)})
for path in sorted(runtime.rglob('*')):
    if path.is_file():
        relative = str(path.relative_to(runtime))
        manifest['runtime_files'][relative] = {'sha256': sha(path), 'source': copied.get(relative, 'generated')}
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps({'kernel': str(out / 'kernel-build/arch/x86/boot/bzImage'),
                  'vmlinux': str(out / 'kernel-build/vmlinux'),
                  'modules': str(out / 'kernel-modules/lib/modules' / release),
                  'runtime': str(runtime), 'manifest': str(out / 'manifest.json')}, indent=2))
PY
