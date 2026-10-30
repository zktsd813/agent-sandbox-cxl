#!/usr/bin/env python3
"""Build an immutable ext4 payload and evidence-only guest workload for pooling."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent
MIB = 1024 * 1024


def checked(argv, **kwargs):
    return subprocess.run([str(a) for a in argv], check=True, **kwargs)


def prepare(args):
    baseline = args.base_assets.resolve(strict=True)
    destination = args.assets.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    if not 128 <= args.payload_mib <= 1024:
        raise ValueError("payload-mib must be 128..1024")
    if not 2048 <= args.storage_mib <= 16384:
        raise ValueError("storage-mib must be 2048..16384 for logical RAM snapshots")
    baseline_manifest = json.loads((baseline / "manifest.json").read_text())
    busybox = (args.busybox or Path(shutil.which("busybox") or "/usr/bin/busybox")).resolve(strict=True)
    if "INTERP" in subprocess.check_output(["readelf", "-l", str(busybox)], text=True):
        raise ValueError("BusyBox must be static")
    applets = set(subprocess.check_output([str(busybox), "--list"], text=True).splitlines())
    if missing := set("awk cat echo mount umount mkdir sh sleep stty".split()) - applets:
        raise ValueError(f"BusyBox lacks applets: {sorted(missing)}")
    destination.mkdir(parents=True)
    compiler = shutil.which("gcc")
    flags = ["-std=c11", "-O2", "-static", "-Wall", "-Wextra", "-Werror"]
    checked([compiler, *flags, HERE / "pool_guest_probe.c", "-o", destination / "pool-guest-probe"])
    checked([compiler, *flags, HERE / "pool_dax_copy.c", "-o", destination / "pool-dax-copy"])
    for name in ("l1-kernel", "l2-initramfs.cpio", "shared-probe", "nfs-probe", "ram-transfer"):
        if (baseline / name).is_file():
            shutil.copy2(baseline / name, destination / name)
    if args.l1_kernel:
        shutil.copy2(args.l1_kernel, destination / "l1-kernel")
        config = args.l1_kernel.resolve().parents[3] / ".config"
        if config.is_file():
            settings = set(config.read_text().splitlines())
            for setting in ("CONFIG_NFSD=y", "CONFIG_NFS_FS=y", "CONFIG_NFS_V3=y"):
                if setting not in settings:
                    raise ValueError(f"Pool lab host NFS requires {setting} in {config}")
            shutil.copy2(config, destination / "l1-kernel.config")
    with tempfile.TemporaryDirectory(prefix="pool-build-", dir=destination) as tmp:
        temp = Path(tmp)
        l1 = temp / "l1"
        l1.mkdir()
        with (baseline / "l1-initramfs.cpio").open("rb") as stream:
            checked(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=l1, stdin=stream)
        if args.firecracker:
            shutil.copy2(args.firecracker, l1 / "lab/firecracker")
        if args.kernel_modules:
            for name in ("kvm.ko", "kvm-intel.ko"):
                shutil.copy2(args.kernel_modules / name, l1 / "modules" / name)
        for tool in args.extra_tool:
            shutil.copy2(tool, l1 / "lab" / tool.name)
        shutil.copy2(l1 / "lab/firecracker", destination / "firecracker")
        shutil.copy2(l1 / "lab/l2-vmlinux", destination / "l2-vmlinux")
        l2 = temp / "l2"
        lab.base_root(l2, busybox)
        shutil.copy2(HERE / "pool-l2-init.sh", l2 / "init")
        (l2 / "init").chmod(0o755)
        shutil.copy2(destination / "pool-guest-probe", l2 / "lab/pool-guest-probe")
        lab.pack_initramfs(l2, destination / "pool-l2-initramfs.cpio")
        shutil.copy2(destination / "pool-l2-initramfs.cpio", l1 / "lab/pool-l2-initramfs.cpio")
        shutil.copy2(destination / "pool-guest-probe", l1 / "lab/pool-guest-probe")
        shutil.copy2(destination / "pool-dax-copy", l1 / "lab/pool-dax-copy")
        # Preserve original initramfs root mode: NFS daemons drop privileges and
        # must be able to traverse /. TemporaryDirectory's 0700 is not suitable.
        (l1).chmod(0o755)
        lab.pack_initramfs(l1, destination / "l1-initramfs.cpio")

        payload = temp / "payload"
        payload.mkdir()
        generated = checked([destination / "pool-guest-probe", "generate", payload / "payload.bin",
                             args.payload_mib], capture_output=True, text=True)
        generated_record = json.loads(generated.stdout)
        files = {p.name: {"bytes": p.stat().st_size, "sha256": lab.sha256(p)} for p in payload.iterdir()}
        images = temp / "images"
        images.mkdir()
        inner = images / "sandbox.raw"
        inner_bytes = (args.payload_mib + 64) * MIB
        with inner.open("xb") as stream:
            stream.truncate(inner_bytes)
        mkfs = ["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0",
                "-E", "lazy_itable_init=0,lazy_journal_init=0"]
        checked([*mkfs, "-d", payload, inner])
        blocks = subprocess.check_output(["debugfs", "-R", "blocks /payload.bin", str(inner)], text=True)
        block_numbers = [int(value) for value in blocks.split()]
        if len(block_numbers) != args.payload_mib * MIB // 4096 or len(set(block_numbers)) != len(block_numbers):
            raise RuntimeError("Payload must have exactly one non-sparse block per file page")
        extents = []
        for block in block_numbers:
            offset = block * 4096
            if extents and extents[-1][0] + extents[-1][1] == offset:
                extents[-1][1] += 4096
            else:
                extents.append([offset, 4096])
        image = {"bytes": inner_bytes, "sha256": lab.sha256(inner), "files": files, "generator": generated_record,
                 "payload_extents": extents}
        # Canonical pool bytes must be byte-identical to the image, including
        # ext4 metadata. The controller copies this image into a sealed pool.
        outer = destination / "storage.raw"
        with outer.open("xb") as stream:
            stream.truncate(max(inner_bytes + 64 * MIB, args.storage_mib * MIB))
        checked([*mkfs, "-d", images, outer])

    manifest = {key: copy.deepcopy(baseline_manifest[key]) for key in
                ("vendor", "l1_kernel_release", "modules", "qemu", "qemu_version", "inputs")}
    for key, path in (("firecracker", args.firecracker), ("l1_kernel", args.l1_kernel)):
        if path:
            manifest["inputs"][key] = {"path": str(path.resolve()), "sha256": lab.sha256(path)}
    if args.kernel_modules:
        manifest["inputs"]["pool_kernel_modules"] = {name: lab.sha256(args.kernel_modules/name)
            for name in ("kvm.ko", "kvm-intel.ko")}
    manifest["inputs"]["pool_tools"] = {p.name: {"path":str(p.resolve()),"sha256":lab.sha256(p)} for p in args.extra_tool}
    manifest.update({
        "schema": "cxl-immutable-pool-fixture-v1", "base_assets": str(baseline),
        "base_manifest_sha256": lab.sha256(baseline / "manifest.json"),
        "pool": {"image": image, "guest_filesystem": "ext4 read-only; private tmpfs and MAP_PRIVATE writes",
                 "guest_cache_gpa_audit": True,
                 "payload_mib": args.payload_mib, "cache_evidence": "fresh MAP_SHARED mincore + full-word oracle"},
        "build": {"compiler": compiler, "flags": flags, "source": "pool_guest_probe.c"},
        "source_hashes": {name: lab.sha256(HERE / name) for name in
                          ("pool_prepare.py", "pool_guest_probe.c", "pool_dax_copy.c", "pool-l2-init.sh", "lab.py")},
        "artifacts": {p.name: lab.sha256(p) for p in destination.iterdir() if p.is_file()},
    })
    lab.save_json(destination / "manifest.json", manifest)
    print(json.dumps({"assets": str(destination), "storage_image": str(destination / "storage.raw"), "image": image}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-assets", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--firecracker", type=Path)
    parser.add_argument("--l1-kernel", type=Path)
    parser.add_argument("--kernel-modules", type=Path)
    parser.add_argument("--extra-tool", type=Path, action="append", default=[])
    parser.add_argument("--busybox", type=Path)
    parser.add_argument("--payload-mib", type=int, default=160)
    parser.add_argument("--storage-mib", type=int, default=4096)
    prepare(parser.parse_args())
