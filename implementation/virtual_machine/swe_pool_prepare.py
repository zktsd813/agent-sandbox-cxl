#!/usr/bin/env python3
"""Package recorded SWE commands with the frozen guest-pool kernel and helper.

Inputs are only read. Every output fixture gets a new directory. Candidate
lower files are selected from immutable ext4 metadata, never read in the guest
to manufacture cache residency.
"""
import argparse
import copy
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent
COMMON = ["/usr/bin/bash", "/usr/bin/git", "/usr/bin/grep", "/usr/bin/sed",
          "/usr/lib/x86_64-linux-gnu/libc.so.6", "/usr/lib/x86_64-linux-gnu/libm.so.6"]
CANDIDATES = {
    "gin-gonic__gin-3741": ["/usr/local/go/bin/go", "/usr/local/go/pkg/tool/linux_amd64/compile",
        "/usr/local/go/pkg/tool/linux_amd64/link", "/usr/local/go/pkg/tool/linux_amd64/asm",
        "/usr/local/go/pkg/tool/linux_amd64/vet", "/testbed/logger.go", "/testbed/logger_test.go", *COMMON],
    "php-cs-fixer__php-cs-fixer-8331": ["/usr/local/bin/php", "/usr/local/lib/libphp.so",
        "/usr/lib/x86_64-linux-gnu/libxml2.so.2", "/usr/lib/x86_64-linux-gnu/libpcre2-8.so.0",
        "/usr/lib/x86_64-linux-gnu/libssl.so.3", "/usr/lib/x86_64-linux-gnu/libcrypto.so.3",
        "/testbed/vendor/phpunit/phpunit/phpunit", "/testbed/src/Tokenizer/Tokens.php",
        "/testbed/src/Fixer/LanguageConstruct/NullableTypeDeclarationFixer.php", *COMMON],
    "micropython__micropython-13039": ["/usr/bin/python3.10", "/usr/bin/x86_64-linux-gnu-gcc-11",
        "/usr/lib/gcc/x86_64-linux-gnu/11/cc1", "/usr/bin/x86_64-linux-gnu-as",
        "/usr/bin/x86_64-linux-gnu-ld.bfd", "/usr/bin/make", "/testbed/py/objslice.c",
        "/usr/lib/x86_64-linux-gnu/libgmp.so.10", "/usr/lib/x86_64-linux-gnu/libmpfr.so.6", *COMMON],
}


def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, **kwargs)


def debug(image, request):
    return run(["debugfs", "-R", request, image], capture_output=True, text=True).stdout


def normalize(path):
    pieces = []
    for piece in PurePosixPath(path).parts:
        if piece in ("/", "."):
            continue
        if piece == "..":
            if pieces:
                pieces.pop()
        else:
            pieces.append(piece)
    return "/" + "/".join(pieces)


def resolve_file(image, path):
    """Resolve ext4 fast symlinks without consulting the host filesystem."""
    path = normalize(path)
    for _ in range(40):
        parts = PurePosixPath(path).parts[1:]
        for i in range(1, len(parts) + 1):
            prefix = "/" + "/".join(parts[:i])
            stat = debug(image, "stat " + prefix)
            if not stat.startswith("Inode:"):
                return None
            if re.search(r"Type:\s+symlink", stat):
                target = re.search(r'Fast link dest:\s+"([^"]+)"', stat)
                if not target:
                    return None
                value = target[1]
                base = value if value.startswith("/") else str(PurePosixPath(prefix).parent / value)
                path = normalize(str(PurePosixPath(base).joinpath(*parts[i:])))
                break
        else:
            return (path, stat) if re.search(r"Type:\s+regular", stat) else None
    raise ValueError("Symlink loop: " + path)


def pool_files(image, candidates):
    header = run(["dumpe2fs", "-h", image], capture_output=True, text=True).stdout
    block = int(re.search(r"^Block size:\s+(\d+)", header, re.M)[1])
    uuid = re.search(r"^Filesystem UUID:\s+([a-f0-9-]+)", header, re.M)[1].replace("-", "")
    if block != 4096:
        raise ValueError("Pooling requires 4KiB ext4 blocks")
    result, stats, seen, pages = [], {}, set(), 0
    for candidate in candidates:
        resolved = resolve_file(image, candidate)
        if not resolved:
            continue
        path, stat = resolved
        inode = int(re.search(r"^Inode:\s+(\d+)", stat, re.M)[1])
        size = int(re.search(r"Project:.*?Size:\s+(\d+)", stat)[1])
        count = (size + block - 1) // block
        if inode in seen or size < block or len(result) == 16 or pages + count > 65536:
            continue
        extents = []
        text = stat.partition("EXTENTS:")[2]
        for a, b, c, d in re.findall(r"\((\d+)(?:-(\d+))?\):(\d+)(?:-(\d+))?", text):
            first, last, physical, end = int(a), int(b or a), int(c), int(d or c)
            if last - first != end - physical or last < first:
                raise ValueError("Invalid extent: " + path)
            extents.append([first * block, physical * block, (last - first + 1) * block])
        if not extents:
            continue
        if any(left[0] + left[2] > right[0] for left, right in zip(extents, extents[1:])):
            raise ValueError("Overlapping extents: " + path)
        result.append({"path": "/lower" + path, "source_path": candidate,
            "inode": inode, "inode_generation": int(re.search(r"^Generation:\s+(\d+)", stat, re.M)[1]),
            "fs_uuid": uuid, "file_bytes": size, "extents": extents})
        stats[path] = stat
        pages += count
        seen.add(inode)
    if not result:
        raise ValueError("No eligible lower files: " + str(image))
    return result, header, stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pool-assets", type=Path, required=True)
    p.add_argument("--trace-assets", type=Path, required=True)
    p.add_argument("--assets", type=Path, required=True)
    a = p.parse_args()
    base, traces, dest = a.pool_assets.resolve(strict=True), a.trace_assets.resolve(strict=True), a.assets.resolve()
    if dest.exists():
        raise FileExistsError(dest)
    manifest = copy.deepcopy(json.loads((base / "manifest.json").read_text()))
    old = json.loads((traces / "manifest.json").read_text())
    for option in ("CONFIG_GUEST_POOL=y", "CONFIG_EXT4_FS=y", "CONFIG_OVERLAY_FS=y", "CONFIG_TMPFS=y",
                   "CONFIG_TMPFS_XATTR=y", "CONFIG_VIRTIO_BLK=y", "CONFIG_PAGE_REPORTING=y"):
        if option not in (base / "l2-handoff.config").read_text().splitlines():
            raise ValueError("Missing current guest kernel config: " + option)
    if lab.sha256(base / "guest-pool") != manifest["handoff"]["inputs"]["guest-pool"]["sha256"]:
        raise ValueError("Guest helper differs from frozen fixture")
    prepared = {}
    for name in CANDIDATES:
        trace = copy.deepcopy(old["traces"][name])
        source_trace = traces / "traces" / (name + ".traj.json")
        image = Path(trace["source_image"])
        if lab.sha256(source_trace) != trace["trace_sha256"] or lab.sha256(image) != trace["image_sha256"]:
            raise ValueError("Changed original trace/image: " + name)
        files, header, stat = pool_files(image, CANDIDATES[name])
        trace.update(pool_files=files, pool_requested_pages=sum((f["file_bytes"] + 4095) // 4096 for f in files),
            pool_selection="fixed runtime/compiler/source whitelist; resident-only registration, no guest warmup")
        prepared[name] = (trace, header, stat)
    if lab.sha256(traces / "storage.raw") != old["artifacts"]["storage.raw"]:
        raise ValueError("Changed prepared read-only storage")
    dest.mkdir(parents=True)
    (dest / "traces").mkdir()
    (dest / "pool-identities").mkdir()
    for source in base.iterdir():
        if source.is_file() and source.name not in ("manifest.json", "l1-initramfs.cpio", "storage.raw"):
            shutil.copy2(source, dest / source.name)
    run(["cp", "--sparse=always", "--reflink=auto", traces / "storage.raw", dest / "storage.raw"])
    with tempfile.TemporaryDirectory(prefix="swe-pool-build-", dir=dest) as tmp:
        temporary = Path(tmp)
        l1 = temporary / "l1"
        l1.mkdir()
        with (base / "l1-initramfs.cpio").open("rb") as stream:
            run(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=l1, stdin=stream)
        l1.chmod(0o755)
        for name, (trace, header, stats) in prepared.items():
            root = temporary / name
            root.mkdir()
            archive = traces / (name + "-initramfs.cpio")
            if lab.sha256(archive) != old["artifacts"][archive.name]:
                raise ValueError("Changed original command initramfs: " + name)
            with archive.open("rb") as stream:
                run(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=root, stdin=stream)
            root.chmod(0o755)
            shutil.copy2(HERE / "swe-pool-l2-init.sh", root / "init")
            (root / "init").chmod(0o755)
            shutil.copy2(HERE / "swe-pool-guest-run.sh", root / "lab/swe-pool-guest-run.sh")
            shutil.copy2(base / "guest-pool", root / "lab/guest-pool")
            (root / "lab/pool-files").write_text("".join(f["path"] + "\n" for f in trace["pool_files"]))
            (root / "lab/pool-image-id").write_text(trace["image_sha256"] + "\n")
            for command in trace["commands"]:
                path = root / "lab/trace/cmd" / f"{command['index']:03d}.command"
                if path.read_text() != command["command"] or lab.sha256(path) != command["command_sha256"]:
                    raise ValueError("Original command bytes changed")
            lab.pack_initramfs(root, dest / archive.name)
            shutil.copy2(dest / archive.name, l1 / "lab" / archive.name)
            shutil.copy2(traces / "traces" / (name + ".traj.json"), dest / "traces" / (name + ".traj.json"))
            lab.save_json(dest / "pool-identities" / (name + ".json"),
                          {"image_sha256": trace["image_sha256"], "header": header, "stat": stats})
        lab.pack_initramfs(l1, dest / "l1-initramfs.cpio")
    manifest.update(schema="cxl-swe-pool-fixture-v1", base_assets=str(base),
        base_manifest_sha256=lab.sha256(base / "manifest.json"), trace_assets=str(traces),
        trace_manifest_sha256=lab.sha256(traces / "manifest.json"), traces={name: t[0] for name, t in prepared.items()},
        schedule=old["schedule"], storage=old["storage"], timeout_policy=old["timeout_policy"],
        target={"memory_mib": 2048, "vcpus": 1, "modes": ["baseline", "state-only", "direct"]})
    manifest["source_hashes"].update({name: lab.sha256(HERE / name) for name in
        ("swe_pool_prepare.py", "swe-pool-l2-init.sh", "swe-pool-guest-run.sh", "guest-pool.c", "guest-pool-uapi.h")})
    manifest["artifacts"] = {path.name: lab.sha256(path) for path in dest.iterdir() if path.is_file()}
    lab.save_json(dest / "manifest.json", manifest)
    print(json.dumps({"assets": str(dest), "traces": {name: {"files": len(t[0]["pool_files"]),
        "requested_pages": t[0]["pool_requested_pages"]} for name, t in prepared.items()}}, indent=2))


if __name__ == "__main__":
    main()
