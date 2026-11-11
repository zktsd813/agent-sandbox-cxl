#!/usr/bin/env python3
"""Package the static-tier peer-cache experiment. Generated assets stay local."""
import argparse, json, shutil, subprocess, tempfile
from pathlib import Path
import lab
HERE=Path(__file__).resolve().parent

def source_state():
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=HERE, text=True).strip()
    dirty = subprocess.check_output(
        ['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=HERE, text=True)
    return {'source_head': head, 'uncommitted': bool(dirty.strip())}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-assets',type=Path,required=True)
    p.add_argument('--assets',type=Path,required=True)
    p.add_argument('--firecracker',type=Path,required=True)
    p.add_argument('--guest-kernel',type=Path,required=True)
    p.add_argument('--mapper',type=Path,required=True)
    a=p.parse_args(); base=a.base_assets.resolve(); out=a.assets.resolve(); out.mkdir(exist_ok=True)
    subprocess.run(['gcc','-O2','-static','-pthread','-Wall','-Wextra','-Werror',str(HERE/'peer_cache_guest.c'),'-o',str(out/'peer-cache-guest')],check=True)
    for name in ('l1-kernel','storage.raw'):
        target=out/name
        if not target.exists(): target.symlink_to(base/name)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        root=Path(tmp)
        for name,archive in [('l1','l1-initramfs.cpio'),('l2','l2-initramfs.cpio')]:
            directory=root/name; directory.mkdir(); directory.chmod(0o755)
            with (base/archive).open('rb') as f:
                subprocess.run(['cpio','-id','--quiet','--no-absolute-filenames'],cwd=directory,stdin=f,check=True)
        shutil.copy2(out/'peer-cache-guest',root/'l2/lab/peer-cache-guest')
        shutil.copy2(HERE/'peer-cache-l2-init.sh',root/'l2/init')
        lab.pack_initramfs(root/'l2',out/'peer-l2-initramfs.cpio')
        for source,name in [(a.firecracker,'firecracker'),(a.guest_kernel,'l2-vmlinux'),(a.mapper,'pool_memory_module.ko'),(out/'peer-l2-initramfs.cpio','peer-l2-initramfs.cpio')]:
            shutil.copy2(source,root/'l1/lab'/name)
        lab.pack_initramfs(root/'l1',out/'l1-initramfs.cpio')
    manifest=json.loads((base/'manifest.json').read_text())
    manifest['peer_cache']={'base_assets':str(base),'canonical_prefill_bytes':0,'lifetime':'infinite for experiment',**source_state()}
    manifest['artifacts']={}
    lab.save_json(out/'manifest.json',manifest)
    print(out)
if __name__=='__main__':main()
