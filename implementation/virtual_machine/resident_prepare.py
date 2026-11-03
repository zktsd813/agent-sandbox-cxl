#!/usr/bin/env python3
"""Build the resident controller and repack an existing immutable SWE fixture.

The base fixture and guest image are preserved. Output must be a fresh directory.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import lab

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-assets',type=Path,required=True)
    parser.add_argument('--firecracker',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    base=args.base_assets.resolve(strict=True);fc=args.firecracker.resolve(strict=True)
    dest=args.output.resolve()
    if dest.exists(): raise FileExistsError(dest)
    source=HERE/'resident_control.c'
    original=json.loads((base/'manifest.json').read_text())
    subprocess.run(['cp','-a','--reflink=auto',str(base),str(dest)],check=True)
    command=['gcc','-O2','-Wall','-Wextra','-Werror','-static',str(source),'-o',str(dest/'resident-control')]
    subprocess.run(command,check=True)
    subprocess.run(['gcc','-O2','-Wall','-Wextra','-Werror','-static',str(HERE/'resident_bootstrap.c'),
                    '-o',str(dest/'resident-bootstrap')],check=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as temporary:
        root=Path(temporary);root.chmod(0o755)
        with (base/'l1-initramfs.cpio').open('rb') as stream:
            subprocess.run(['cpio','-id','--quiet','--no-absolute-filenames'],cwd=root,stdin=stream,check=True)
        shutil.copy2(fc,root/'lab/firecracker');shutil.copy2(fc,dest/'firecracker')
        shutil.copy2(dest/'resident-control',root/'lab/resident-control')
        shutil.copy2(dest/'resident-bootstrap',root/'lab/resident-bootstrap')
        for name in original['traces']:
            archive=name+'-initramfs.cpio'
            with tempfile.TemporaryDirectory(dir=dest.parent) as nested:
                guest=Path(nested);guest.chmod(0o755)
                with (base/archive).open('rb') as stream:
                    subprocess.run(['cpio','-id','--quiet','--no-absolute-filenames'],cwd=guest,stdin=stream,check=True)
                shutil.copy2(HERE/'swe-pool-guest-run.sh',guest/'lab/swe-pool-guest-run.sh')
                lab.pack_initramfs(guest,dest/archive)
                shutil.copy2(dest/archive,root/'lab'/archive)
                original['traces'][name]['initramfs_path']=str(dest/archive)
        lab.pack_initramfs(root,dest/'l1-initramfs.cpio')
    original['base_assets']=str(base)
    original['base_manifest_sha256']=lab.sha256(base/'manifest.json')
    vmm={'path':str(dest/'firecracker'),'sha256':lab.sha256(dest/'firecracker')}
    original['inputs']['firecracker']=vmm
    original['handoff']['inputs']['firecracker']=vmm
    original['comparison']['patched']['sha256']=vmm['sha256']
    original['source_hashes']['swe-pool-guest-run.sh']=lab.sha256(HERE/'swe-pool-guest-run.sh')
    original['resident_control']={'source_sha256':lab.sha256(source),
        'binary_sha256':lab.sha256(dest/'resident-control'),'build_argv':command,
        'compiler':subprocess.check_output(['gcc','--version'],text=True).splitlines()[0]}
    original['resident_bootstrap']={'source_sha256':lab.sha256(HERE/'resident_bootstrap.c'),
        'binary_sha256':lab.sha256(dest/'resident-bootstrap')}
    original['artifacts']={p.name:lab.sha256(p) for p in dest.iterdir() if p.is_file() and p.name!='manifest.json'}
    lab.save_json(dest/'manifest.json',original)
    print(json.dumps({'assets':str(dest),'firecracker':vmm,'controller':original['resident_control']},indent=2))


if __name__=='__main__': main()
