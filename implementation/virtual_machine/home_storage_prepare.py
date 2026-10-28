#!/usr/bin/env python3
"""Build a disposable, disk-backed writable fixture; never mount host images.

storage.raw is a template outer ext4 holding sandbox.raw (the guest ext4).
lab.py must attach a private writable copy to the home L1 only. Other L1s
access that same sandbox.raw through host NFS, never another writable copy.
"""
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
REQUIRED_APPLETS = frozenset("awk cat echo mount umount mkdir sh sleep stty sync".split())


def checked(argv, **kwargs):
    return subprocess.run([str(a) for a in argv], check=True, **kwargs)


def prepare(args):
    baseline = args.base_assets.resolve(strict=True)
    destination = args.assets.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    if not 1 <= args.payload_mib <= 1024 or not 1 <= args.mutable_mib <= 256:
        raise ValueError("payload-mib must be 1..1024 and mutable-mib 1..256")
    baseline_manifest = json.loads((baseline / "manifest.json").read_text())
    busybox = (args.busybox or Path(shutil.which("busybox") or "/usr/bin/busybox")).resolve(strict=True)
    applets = set(subprocess.check_output([str(busybox), "--list"], text=True).splitlines())
    if missing := REQUIRED_APPLETS - applets:
        raise ValueError(f"BusyBox lacks applets: {sorted(missing)}")
    if "INTERP" in subprocess.check_output(["readelf", "-l", str(busybox)], text=True):
        raise ValueError("BusyBox must be static")
    for path in (args.firecracker, args.shared_probe, args.ram_transfer):
        if path is not None and not path.is_file():
            raise FileNotFoundError(path)
    destination.mkdir(parents=True)
    compiler = shutil.which("gcc")
    flags = ["-std=c11", "-O2", "-static", "-Wall", "-Wextra", "-Werror"]
    checked([compiler, *flags, HERE / "home_storage_probe.c", "-o", destination / "home-storage-probe"])
    for name in ("l1-kernel", "l2-initramfs.cpio", "shared-probe", "nfs-probe"):
        if (baseline / name).is_file():
            shutil.copy2(baseline / name, destination / name)
    if args.shared_probe:
        shutil.copy2(args.shared_probe, destination / "shared-probe")
    if args.ram_transfer:
        shutil.copy2(args.ram_transfer, destination / "ram-transfer")
    with tempfile.TemporaryDirectory(prefix="home-storage-assets-", dir=destination) as tmp:
        temp = Path(tmp)
        l1 = temp / "l1"
        l1.mkdir()
        with (baseline / "l1-initramfs.cpio").open("rb") as stream:
            checked(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=l1, stdin=stream)
        if args.firecracker:
            shutil.copy2(args.firecracker, l1 / "lab/firecracker")
        if args.shared_probe:
            shutil.copy2(args.shared_probe, l1 / "lab/shared-probe")
        if args.ram_transfer:
            shutil.copy2(args.ram_transfer, l1 / "lab/ram-transfer")
        shutil.copy2(l1 / "lab/firecracker", destination / "firecracker")
        shutil.copy2(l1 / "lab/l2-vmlinux", destination / "l2-vmlinux")
        l2 = temp / "l2"
        lab.base_root(l2, busybox)
        shutil.copy2(HERE / "home-storage-l2-init.sh", l2 / "init")
        (l2 / "init").chmod(0o755)
        shutil.copy2(destination / "home-storage-probe", l2 / "lab/home-storage-probe")
        lab.pack_initramfs(l2, destination / "home-storage-l2-initramfs.cpio")
        shutil.copy2(destination / "home-storage-l2-initramfs.cpio", l1 / "lab/home-storage-l2-initramfs.cpio")
        shutil.copy2(destination / "home-storage-probe", l1 / "lab/home-storage-probe")
        lab.pack_initramfs(l1, destination / "l1-initramfs.cpio")

        payload = temp / "payload"
        generated = checked([destination / "home-storage-probe", "generate", payload,
                             args.payload_mib, args.mutable_mib], capture_output=True, text=True)
        generated_record = json.loads(generated.stdout)
        files = {p.name: {"bytes": p.stat().st_size, "sha256": lab.sha256(p)}
                 for p in sorted(payload.iterdir())}
        images = temp / "images"
        images.mkdir()
        inner = images / "sandbox.raw"
        inner_bytes = (args.payload_mib + args.mutable_mib + 64) * MIB
        with inner.open("xb") as stream:
            stream.truncate(inner_bytes)
        mkfs = ["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0",
                "-E", "lazy_itable_init=0,lazy_journal_init=0"]
        checked([*mkfs, "-d", payload, inner])
        inner_uuid = subprocess.check_output(["blkid", "-p", "-s", "UUID", "-o", "value", str(inner)], text=True).strip()
        initial_image = {"name": "sandbox.raw", "bytes": inner_bytes, "sha256": lab.sha256(inner),
                         "ext4_uuid": inner_uuid, "files": files, "generator": generated_record}
        outer = destination / "storage.raw"
        with outer.open("xb") as stream:
            stream.truncate(inner_bytes + 64 * MIB)
        checked([*mkfs, "-d", images, outer])

    manifest = {key: copy.deepcopy(baseline_manifest[key]) for key in
                ("vendor", "l1_kernel_release", "modules", "qemu", "qemu_version", "inputs")}
    manifest["firecracker_version"] = subprocess.check_output(
        [str(destination / "firecracker"), "--version"], text=True, stderr=subprocess.STDOUT).strip()
    for key, path in (("firecracker", args.firecracker), ("shared_probe", args.shared_probe),
                      ("ram_transfer", args.ram_transfer)):
        if path:
            manifest["inputs"][key] = {"path": str(path.resolve()), "sha256": lab.sha256(path)}
    if args.shared_probe:
        for key, name in (("probe_source", "shared_probe.c"), ("cache_probe_source", "cache_probe.h"),
                          ("mptcp_probe_source", "mptcp_probe.h")):
            manifest["inputs"][key] = {"path": str(HERE / name), "sha256": lab.sha256(HERE / name)}
    manifest.update({
        "schema": "cxl-home-storage-fixture-v1", "base_assets": str(baseline),
        "base_manifest_sha256": lab.sha256(baseline / "manifest.json"),
        "home_storage": {
            "home_node": "A", "guest_filesystem": "ext4 writable /dev/vda mounted /data; no tmpfs upper",
            "image": initial_image,
            "template_policy": "private RW outer copy attached to home A only; B uses A's NFS export",
            "dirty_fixture_policy": "guest ext4 commit=600; dirty_writeback_centisecs=0; explicit fsync unchanged",
            "persistent_checks": "mutable data, MAP_SHARED file, generation rename, ordered journal, immutable payload",
            "memory_checks": "retained linked and unlinked FD offsets; boot ID, PID, cookie and mapping contents",
        },
        "build": {"compiler": compiler, "version": subprocess.check_output([compiler, "--version"], text=True),
                  "flags": flags, "source": "home_storage_probe.c"},
        "source_hashes": {name: lab.sha256(HERE / name) for name in
                          ("home_storage_prepare.py", "home_storage_probe.c", "home-storage-l2-init.sh", "lab.py")},
        "artifacts": {p.name: lab.sha256(p) for p in destination.iterdir() if p.is_file()},
    })
    lab.save_json(destination / "manifest.json", manifest)
    print(json.dumps({"assets": str(destination), "storage_image": str(destination / "storage.raw"),
                      "image": initial_image["name"], "image_bytes": initial_image["bytes"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-assets", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--firecracker", type=Path)
    parser.add_argument("--shared-probe", type=Path)
    parser.add_argument("--ram-transfer", type=Path)
    parser.add_argument("--busybox", type=Path)
    parser.add_argument("--payload-mib", type=int, default=64)
    parser.add_argument("--mutable-mib", type=int, default=16)
    prepare(parser.parse_args())
