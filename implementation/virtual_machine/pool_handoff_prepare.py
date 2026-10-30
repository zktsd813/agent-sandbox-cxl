#!/usr/bin/env python3
"""Build a new immutable lab fixture without mutating previous experiment inputs."""
import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent


def run(argv, **kwargs):
    return subprocess.run([str(v) for v in argv], check=True, **kwargs)


def file_identity(image, payload_bytes):
    stat = run(["debugfs", "-R", "stat /payload.bin", image], capture_output=True, text=True).stdout
    header = run(["dumpe2fs", "-h", image], capture_output=True, text=True).stdout
    inode = int(re.search(r"^Inode:\s+(\d+)", stat, re.M)[1])
    generation = int(re.search(r"^Generation:\s+(\d+)", stat, re.M)[1])
    size = int(re.search(r"Project:.*?Size:\s+(\d+)", stat)[1])
    uuid = re.search(r"^Filesystem UUID:\s+([0-9a-f-]+)", header, re.M)[1].replace("-", "")
    block = int(re.search(r"^Block size:\s+(\d+)", header, re.M)[1])
    if size != payload_bytes or block != 4096:
        raise ValueError("Fixture file size or ext4 block size differs")
    extents = []
    for first, last, physical, physical_last in re.findall(r"\((\d+)-(\d+)\):(\d+)-(\d+)", stat):
        first, last, physical, physical_last = map(int, (first, last, physical, physical_last))
        if last - first != physical_last - physical:
            raise ValueError("Unexpected extent length")
        extents.append([first * block, physical * block, (last - first + 1) * block])
    if not extents or sum(e[2] for e in extents) != size:
        raise ValueError("Incomplete full-page fixture extent map")
    return {"inode": inode, "inode_generation": generation, "fs_uuid": uuid, "file_bytes": size}, extents, stat, header


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-assets", type=Path, required=True)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--firecracker", type=Path, required=True)
    p.add_argument("--guest-kernel", type=Path, required=True)
    p.add_argument("--guest-config", type=Path, required=True)
    p.add_argument("--probe", type=Path, required=True)
    p.add_argument("--guest-tool", type=Path)
    p.add_argument("--shared-probe", type=Path)
    p.add_argument("--l1-kernel", type=Path)
    p.add_argument("--kvm-module", type=Path)
    p.add_argument("--kvm-intel-module", type=Path)
    p.add_argument("--extra-tool", type=Path, action="append", default=[])
    a = p.parse_args()
    source, dest = a.base_assets.resolve(strict=True), a.assets.resolve()
    if dest.exists():
        raise FileExistsError(dest)
    config = set(a.guest_config.read_text().splitlines())
    for option in ("CONFIG_GUEST_POOL=y", "CONFIG_EXT4_FS=y", "CONFIG_VIRTIO_BLK=y"):
        if option not in config:
            raise ValueError("Missing guest configuration " + option)
    dest.mkdir(parents=True)
    manifest = copy.deepcopy(json.loads((source / "manifest.json").read_text()))
    for path in source.iterdir():
        if path.is_file() and path.name not in ("manifest.json", "l1-initramfs.cpio", "storage.raw"):
            shutil.copy2(path, dest / path.name)
    run(["cp", "--sparse=always", "--reflink=auto", source / "storage.raw", dest / "storage.raw"])
    replacements = [(a.firecracker, "firecracker"), (a.guest_kernel, "l2-vmlinux"),
                    (a.guest_config, "l2-handoff.config"), (a.probe, "pool-handoff-probe")]
    if a.guest_tool:
        replacements.append((a.guest_tool, "guest-pool"))
    if a.shared_probe:
        replacements.append((a.shared_probe, "shared-probe"))
    if a.l1_kernel:
        replacements.append((a.l1_kernel, "l1-kernel"))
    replacements.extend((path, path.name) for path in a.extra_tool)
    for path, name in replacements:
        shutil.copy2(path, dest / name)
    with tempfile.TemporaryDirectory(prefix="handoff-build-", dir=dest) as tmp:
        temp = Path(tmp)
        for name, archive in (("l1", "l1-initramfs.cpio"), ("l2", "pool-discard-l2-initramfs.cpio")):
            directory = temp / name
            directory.mkdir()
            with (source / archive).open("rb") as stream:
                run(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=directory, stdin=stream)
            directory.chmod(0o755)
        shutil.copy2(a.probe, temp / "l2/lab/pool-handoff-probe")
        if a.guest_tool:
            shutil.copy2(a.guest_tool, temp / "l2/lab/guest-pool")
        shutil.copy2(HERE / "pool-handoff-l2-init.sh", temp / "l2/init")
        (temp / "l2/init").chmod(0o755)
        lab.pack_initramfs(temp / "l2", dest / "pool-handoff-l2-initramfs.cpio")
        for _, name in replacements:
            if name not in ("l1-kernel", "l2-handoff.config"):
                shutil.copy2(dest / name, temp / "l1/lab" / name)
        shutil.copy2(dest / "pool-handoff-l2-initramfs.cpio", temp / "l1/lab/pool-handoff-l2-initramfs.cpio")
        for module, name in ((a.kvm_module, "kvm.ko"), (a.kvm_intel_module, "kvm-intel.ko")):
            if module:
                shutil.copy2(module, temp / "l1/modules" / name)
        lab.pack_initramfs(temp / "l1", dest / "l1-initramfs.cpio")
        image = temp / "sandbox.raw"
        run(["debugfs", "-R", f"dump -p /sandbox.raw {image}", dest / "storage.raw"], capture_output=True)
        if lab.sha256(image) != manifest["pool"]["image"]["sha256"]:
            raise ValueError("Immutable image identity changed")
        identity, extents, stat, header = file_identity(image, manifest["pool"]["image"]["files"]["payload.bin"]["bytes"])
        (dest / "payload-debugfs-stat.txt").write_text(stat)
        (dest / "image-dumpe2fs.txt").write_text(header)
    manifest.update(schema="cxl-guest-pool-handoff-fixture-v1", base_assets=str(source),
                    base_manifest_sha256=lab.sha256(source / "manifest.json"))
    manifest["handoff"] = {"file_identity": identity, "logical_extents": extents,
        "guest_authority": "guest kernel current lower inode mapping; locked resident clean folios",
        "file_scope": "full 4KiB pages in one immutable plain ext4 lower file",
        "modes": ["state-only", "direct"],
        "inputs": {name: {"path": str(path.resolve()), "sha256": lab.sha256(path)} for path, name in replacements}}
    manifest["inputs"]["firecracker"] = manifest["handoff"]["inputs"]["firecracker"]
    manifest["inputs"]["l2_kernel"] = manifest["handoff"]["inputs"]["l2-vmlinux"]
    if a.l1_kernel:
        manifest["inputs"]["l1_kernel"] = manifest["handoff"]["inputs"]["l1-kernel"]
    for module, name in ((a.kvm_module, "kvm.ko"), (a.kvm_intel_module, "kvm-intel.ko")):
        if module:
            manifest["inputs"].setdefault("pool_kernel_modules", {})[name] = lab.sha256(module)
            manifest["handoff"]["inputs"][name] = {"path": str(module.resolve()), "sha256": lab.sha256(module)}
    manifest["pool"]["guest_filesystem"] = "read-only ext4 lower + per-VM tmpfs OverlayFS upper/work"
    manifest["comparison"]["patched"] = {"sha256": lab.sha256(a.firecracker)}
    manifest["source_hashes"].update({name: lab.sha256(HERE / name) for name in
        ("pool_handoff_prepare.py", "pool_handoff_probe.c", "pool-handoff-l2-init.sh", "guest-pool-uapi.h")})
    manifest["artifacts"] = {path.name: lab.sha256(path) for path in dest.iterdir() if path.is_file()}
    lab.save_json(dest / "manifest.json", manifest)
    print(json.dumps({"assets": str(dest), "image_id": manifest["pool"]["image"]["sha256"],
                      "file_identity": identity, "logical_extents": extents}))


if __name__ == "__main__":
    main()
