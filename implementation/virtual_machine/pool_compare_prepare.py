#!/usr/bin/env python3
"""Build separate comparison assets without changing validated pool fixtures."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-assets", type=Path, required=True)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--stock", type=Path, required=True)
    p.add_argument("--probe", type=Path, default=Path("/tmp/pool-compare-probe"))
    p.add_argument("--pfn-tool", type=Path, default=Path("/tmp/pool-compare-pfn"))
    args = p.parse_args()
    source, dest = args.base_assets.resolve(strict=True), args.assets.resolve()
    if dest.exists():
        raise FileExistsError(dest)
    original = json.loads((source/"manifest.json").read_text())
    dest.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file() and path.name not in ("manifest.json", "storage.raw", "l1-initramfs.cpio"):
            shutil.copy2(path, dest/path.name)
    for path,name in ((args.stock,"firecracker-stock"),(args.probe,"pool-compare-probe"),
                      (args.pfn_tool,"pool-compare-pfn")):
        shutil.copy2(path,dest/name)
    with tempfile.TemporaryDirectory(prefix="pool-compare-assets-",dir=dest) as temporary:
        temp=Path(temporary)
        for name,archive in (("l1","l1-initramfs.cpio"),("l2","pool-l2-initramfs.cpio")):
            directory=temp/name;directory.mkdir()
            with (source/archive).open("rb") as stream:
                run(["cpio","-id","--quiet","--no-absolute-filenames"],cwd=directory,stdin=stream)
            directory.chmod(0o755)
        shutil.copy2(args.probe,temp/"l2/lab/pool-guest-probe")
        lab.pack_initramfs(temp/"l2",dest/"pool-compare-l2-initramfs.cpio")
        for name in ("firecracker-stock","pool-compare-probe","pool-compare-pfn","pool-compare-l2-initramfs.cpio"):
            shutil.copy2(dest/name,temp/"l1/lab"/name)
        lab.pack_initramfs(temp/"l1",dest/"l1-initramfs.cpio")
        images=temp/"images";images.mkdir()
        inner=images/"sandbox.raw"
        run(["debugfs","-R",f"dump -p /sandbox.raw {inner}",source/"storage.raw"],capture_output=True)
        if not inner.is_file() or lab.sha256(inner)!=original["pool"]["image"]["sha256"]:
            raise RuntimeError("Extracted immutable image differs from original fixture")
        with (dest/"storage.raw").open("xb") as stream:stream.truncate(4096*1024*1024)
        run(["mke2fs","-q","-F","-t","ext4","-b","4096","-m","0","-E",
             "lazy_itable_init=0,lazy_journal_init=0","-d",images,dest/"storage.raw"])
    manifest=copy.deepcopy(original)
    manifest.update(schema="cxl-pool-compare-fixture-v1",base_assets=str(source),
                    base_manifest_sha256=lab.sha256(source/"manifest.json"))
    manifest["comparison"]={"same_inner_image_sha256":original["pool"]["image"]["sha256"],
        "fresh_outer_image":True,"guest_probe":"pool_compare_probe.c",
        "stock":{"path":str(args.stock.resolve()),"sha256":lab.sha256(args.stock)},
        "patched":{"sha256":lab.sha256(dest/"firecracker")},
        "timing":"Guest CLOCK_MONOTONIC_RAW around operation body; read/recycle oracle included"}
    manifest["source_hashes"].update({name:lab.sha256(HERE/name) for name in
        ("pool_compare_prepare.py","pool_compare_probe.c","pool_compare_pfn.c")})
    manifest["artifacts"]={f.name:lab.sha256(f) for f in dest.iterdir() if f.is_file()}
    lab.save_json(dest/"manifest.json",manifest)
    print(json.dumps({"assets":str(dest),"image_id":manifest["pool"]["image"]["sha256"],
                      "binaries":manifest["comparison"]}))


if __name__=="__main__":main()
