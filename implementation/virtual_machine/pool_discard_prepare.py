#!/usr/bin/env python3
"""Prepare isolated discard-handshake fixtures; preserve all prior experiment assets."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent


def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-assets", type=Path, required=True)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--firecracker", type=Path, required=True)
    p.add_argument("--guest-kernel", type=Path, required=True)
    p.add_argument("--guest-source", type=Path, required=True)
    p.add_argument("--guest-config", type=Path, required=True)
    p.add_argument("--probe", type=Path, default=Path("/tmp/pool-discard-probe"))
    p.add_argument("--extra-tool", type=Path, action="append", default=[])
    a = p.parse_args()
    source, dest = a.base_assets.resolve(strict=True), a.assets.resolve()
    if dest.exists():
        raise FileExistsError(dest)
    original = json.loads((source / "manifest.json").read_text())
    settings = set(a.guest_config.read_text().splitlines())
    for setting in ("CONFIG_VIRTIO_BALLOON=y", "CONFIG_PAGE_REPORTING=y", "CONFIG_EVENT_TRACING=y"):
        if setting not in settings:
            raise ValueError("Missing guest setting " + setting)
    dest.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file() and path.name not in ("manifest.json", "storage.raw", "l1-initramfs.cpio"):
            shutil.copy2(path, dest / path.name)
    for path, name in ((a.firecracker, "firecracker"), (a.guest_kernel, "l2-vmlinux"),
                       (a.probe, "pool-discard-probe"), (a.guest_config, "l2-discard.config")):
        shutil.copy2(path, dest / name)
    for path in a.extra_tool:
        shutil.copy2(path, dest / path.name)
    with tempfile.TemporaryDirectory(prefix="discard-assets-", dir=dest) as temporary:
        temp = Path(temporary)
        for name, archive in (("l1", "l1-initramfs.cpio"), ("l2", "pool-l2-initramfs.cpio")):
            directory = temp / name
            directory.mkdir()
            with (source / archive).open("rb") as stream:
                run(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=directory, stdin=stream)
            directory.chmod(0o755)
        shutil.copy2(a.probe, temp / "l2/lab/pool-guest-probe")
        shutil.copy2(HERE / "pool-discard-l2-init.sh", temp / "l2/init")
        (temp / "l2/init").chmod(0o755)
        lab.pack_initramfs(temp / "l2", dest / "pool-discard-l2-initramfs.cpio")
        for name in ("firecracker", "l2-vmlinux", "pool-discard-probe", "pool-discard-l2-initramfs.cpio"):
            shutil.copy2(dest / name, temp / "l1/lab" / name)
        for path in a.extra_tool:
            shutil.copy2(path, temp / "l1/lab" / path.name)
        lab.pack_initramfs(temp / "l1", dest / "l1-initramfs.cpio")
        images = temp / "images"
        images.mkdir()
        inner = images / "sandbox.raw"
        run(["debugfs", "-R", f"dump -p /sandbox.raw {inner}", source / "storage.raw"], capture_output=True)
        if not inner.is_file() or lab.sha256(inner) != original["pool"]["image"]["sha256"]:
            raise RuntimeError("Immutable inner image changed")
        with (dest / "storage.raw").open("xb") as stream:
            stream.truncate(4096 * 1024 * 1024)
        run(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0", "-E",
             "lazy_itable_init=0,lazy_journal_init=0", "-d", images, dest / "storage.raw"])
    manifest = copy.deepcopy(original)
    guest_sources = ("drivers/virtio/virtio_balloon.c", "mm/page_reporting.c", "include/trace/events/pool_discard.h")
    manifest.update(schema="cxl-pool-discard-fixture-v1", base_assets=str(source),
                    base_manifest_sha256=lab.sha256(source / "manifest.json"))
    manifest["comparison"]["patched"] = {"sha256": lab.sha256(a.firecracker)}
    manifest["inputs"]["firecracker"] = {"path": str(a.firecracker.resolve()), "sha256": lab.sha256(a.firecracker)}
    manifest["inputs"]["l2_kernel"] = {"path": str(a.guest_kernel.resolve()), "sha256": lab.sha256(a.guest_kernel)}
    manifest["discard"] = {"guest_kernel_sha256": lab.sha256(a.guest_kernel),
        "guest_config_sha256": lab.sha256(a.guest_config),
        "guest_source": str(a.guest_source.resolve()),
        "guest_source_hashes": {name: lab.sha256(a.guest_source / name) for name in guest_sources},
        "trace_protocol": "phase0 isolated-SG submit, phase1 GPA/bytes, phase2 used-ring ACK consumed, phase3 failed-submit",
        "reporter": "Linux standard free page reporting; delayed and budgeted, no synchronous allocator modification",
        "timing": "Separate RAW guest evict/mmap/first-fill/verify/unmap, MONOTONIC ordering timestamps"}
    manifest["source_hashes"].update({name: lab.sha256(HERE / name) for name in
        ("pool_discard_prepare.py", "pool_discard_probe.c", "pool-discard-l2-init.sh", "pool-discard-guest-trace.patch")})
    manifest["artifacts"] = {path.name: lab.sha256(path) for path in dest.iterdir() if path.is_file()}
    lab.save_json(dest / "manifest.json", manifest)
    print(json.dumps({"assets": str(dest), "discard": manifest["discard"]}))


if __name__ == "__main__":
    main()
