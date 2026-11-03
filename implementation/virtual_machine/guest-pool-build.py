#!/usr/bin/env python3
"""Build an isolated guest authority kernel; never edit the frozen base tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
KCONFIG = '''
config GUEST_POOL
    bool "Experimental read-only resident file folio authority"
    depends on X86_64 && EXT4_FS=y
    select CRYPTO_LIB_SHA256
    help
      Built-in cooperative guest helper for paused-host CXL pooling. It keeps
      a permanent status page and bounded locked folio registrations. No module
      unloading is permitted because host validation uses that status page.
'''


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-source", type=Path, required=True)
    parser.add_argument("--base-build", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--resume", action="store_true",
                        help="Resume this isolated build after a recorded compile failure")
    args = parser.parse_args()
    base_source, base_build = args.base_source.resolve(strict=True), args.base_build.resolve(strict=True)
    source, build, output = args.source.resolve(), args.build.resolve(), args.output.resolve()
    for path in (source, build, output):
        if args.resume and not path.is_dir():
            raise FileNotFoundError(path)
        if not args.resume and path.exists():
            raise FileExistsError(path)
    if source == build or source in build.parents or build in source.parents or args.jobs < 1:
        raise ValueError("Use distinct, separate source/build paths and positive job count")
    output.mkdir(parents=True, exist_ok=args.resume)
    commands = json.loads((output / "commands.json").read_text()) if args.resume else []

    def run(argv, log=None):
        argv = [str(x) for x in argv]
        commands.append(argv)
        (output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        if log:
            if (output / log).exists():
                attempt = 1
                while (output / f"{log}.attempt-{attempt}").exists():
                    attempt += 1
                (output / log).rename(output / f"{log}.attempt-{attempt}")
            with (output / log).open("w") as stream:
                subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, check=True)
        else:
            subprocess.run(argv, check=True)

    bases = ["drivers/virtio/virtio_balloon.c", "mm/page_reporting.c",
             "include/trace/events/pool_discard.h", "drivers/misc/Kconfig", "drivers/misc/Makefile"]
    base_hashes = {name: sha(base_source / name) for name in bases}
    base_config_hash = sha(base_build / ".config")
    if not args.resume:
        run(["cp", "-a", "--reflink=auto", base_source, source])
        run(["cp", "-a", "--reflink=auto", base_build, build])
    else:
        for name in bases[:3]:
            if sha(source / name) != base_hashes[name]:
                raise RuntimeError("Reporting source changed in resumed tree: " + name)
    for name in ("guest-pool.c", "guest-pool-uapi.h"):
        shutil.copy2(HERE / name, source / "drivers/misc" / name)
    if not args.resume:
        with (source / "drivers/misc/Kconfig").open("a") as stream:
            stream.write(KCONFIG)
        with (source / "drivers/misc/Makefile").open("a") as stream:
            stream.write("\nobj-$(CONFIG_GUEST_POOL) += guest-pool.o\n")
    elif ((source / "drivers/misc/Kconfig").read_text() !=
          (base_source / "drivers/misc/Kconfig").read_text() + KCONFIG or
          (source / "drivers/misc/Makefile").read_text() !=
          (base_source / "drivers/misc/Makefile").read_text() + "\nobj-$(CONFIG_GUEST_POOL) += guest-pool.o\n"):
        raise RuntimeError("Resumed build integration differs from the recorded isolated patch")
    run([source / "scripts/config", "--file", build / ".config", "--enable", "GUEST_POOL"])
    run(["make", "-C", source, f"O={build}", "LOCALVERSION=", "olddefconfig"], "olddefconfig.log")
    settings = set((build / ".config").read_text().splitlines())
    for setting in ("CONFIG_GUEST_POOL=y", "CONFIG_VIRTIO_BALLOON=y", "CONFIG_PAGE_REPORTING=y",
                    "CONFIG_EVENT_TRACING=y", "CONFIG_CRYPTO_LIB_SHA256=y"):
        if setting not in settings:
            raise RuntimeError("Missing required setting " + setting)
    run(["make", "-C", source, f"O={build}", "LOCALVERSION=", f"-j{args.jobs}", "vmlinux"], "kernel-build.log")
    run(["gcc", "-std=c11", "-O2", "-static", "-Wall", "-Wextra", "-Werror",
         HERE / "guest-pool-cli.c", "-o", output / "guest-pool"], "cli-build.log")
    run([output / "guest-pool", "--abi"], "cli-abi.json")
    for path in (build / ".config", build / "include/generated/compile.h"):
        shutil.copy2(path, output / ("guest.config" if path.name == ".config" else path.name))
    source_inputs = ("guest-pool.c", "guest-pool-uapi.h", "guest-pool-cli.c", "guest-pool-build.py")
    result = {
        "schema": "guest-pool-build-v1", "base_source": str(base_source), "base_build": str(base_build),
        "source": str(source), "build": str(build), "base_config_sha256": base_config_hash,
        "base_source_hashes": base_hashes, "source_inputs": {name: sha(HERE / name) for name in source_inputs},
        "kernel": {"path": str(build / "vmlinux"), "sha256": sha(build / "vmlinux")},
        "config_sha256": sha(build / ".config"), "cli_sha256": sha(output / "guest-pool"),
        "compiled_sources": {name: sha(source / name) for name in bases +
                             ["drivers/misc/guest-pool.c", "drivers/misc/guest-pool-uapi.h"]},
        "scope": "Built-in helper; copied frozen reporting guest source/build; no base files modified",
    }
    if base_config_hash != sha(base_build / ".config") or any(
            sha(base_source / name) != value for name, value in base_hashes.items()):
        raise RuntimeError("Frozen base changed during build")
    (output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
