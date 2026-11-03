#!/usr/bin/env python3
"""Per-image working sets in the host page cache, as 4 KiB content hashes.

snapshot: mincore() over every image's rootfs.ext4, hash the resident blocks (blake2b-64), save {image: uint64 array}
          to OUT/blocks-<utc>.npz. Run it repeatedly during pilot runs (the matrix drops caches between conditions).
merge:    union per image over all snapshots -> one npz (the working set each image showed over the runs).
resident: every --interval s, append "t_ns,image,resident_bytes" per image (mincore only, no hashing) until killed.
evict:    drop the images' pages from the host page cache (posix_fadvise DONTNEED), leaving every other file alone.
"""

import argparse
import ctypes
import datetime
import glob
import hashlib
import os
from pathlib import Path
import time

import numpy as np

PAGE = 4096


def _libc():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    return libc


def _resident(libc, path):
    """mincore() vector of a file (1 byte per page)."""
    size = os.path.getsize(path)
    fd = os.open(path, os.O_RDONLY)
    addr = libc.mmap(None, size, 1, 1, fd, 0)  # PROT_READ, MAP_SHARED
    n = size // PAGE
    vec = (ctypes.c_ubyte * n)()
    libc.mincore(addr, n * PAGE, vec)
    libc.munmap(addr, size)
    return fd, bytes(vec)


def snapshot(images, out):
    libc = _libc()
    blocks = {}
    for path in sorted(glob.glob(str(images / "*" / "rootfs.ext4"))):
        fd, vec = _resident(libc, path)
        hashes = [int.from_bytes(hashlib.blake2b(os.pread(fd, PAGE, i * PAGE), digest_size=8).digest(), "little")
                  for i, b in enumerate(vec) if b & 1]
        os.close(fd)
        if hashes:
            blocks[Path(path).parent.name] = np.unique(np.array(hashes, dtype=np.uint64))
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"blocks-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.npz"
    np.savez_compressed(dest, **blocks)
    print(f"{len(blocks)} images, {sum(len(v) for v in blocks.values()) * PAGE / 2**30:.2f} GiB -> {dest}")


def merge(inputs, out):
    union = {}
    for path in inputs:
        with np.load(path) as data:
            for name in data.files:
                union[name] = np.union1d(union.get(name, np.array([], np.uint64)), data[name])
    np.savez_compressed(out, **union)
    print(f"{len(inputs)} snapshots -> {len(union)} images, "
          f"{sum(len(v) for v in union.values()) * PAGE / 2**30:.2f} GiB per-image sum -> {out}")


def resident(images, out, interval):
    libc = _libc()
    with open(out, "a") as stream:
        while True:
            now = time.monotonic_ns()
            for path in sorted(glob.glob(str(images / "*" / "rootfs.ext4"))):
                fd, vec = _resident(libc, path)
                os.close(fd)
                stream.write(f"{now},{Path(path).parent.name},{sum(b & 1 for b in vec) * PAGE}\n")
            stream.flush()
            time.sleep(interval)


def evict(images):
    total = 0
    for path in sorted(glob.glob(str(images / "*" / "rootfs.ext4"))):
        fd = os.open(path, os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)
        total += 1
    print(f"evicted page cache of {total} image files")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--images", type=Path, default=Path(__file__).resolve().parent / ".local/images")
    p.add_argument("--out", type=Path, required=True)
    for name in ("resident", "evict"):
        p = sub.add_parser(name)
        p.add_argument("--images", type=Path, default=Path(__file__).resolve().parent / ".local/images")
        if name == "resident":
            p.add_argument("--out", type=Path, required=True)
            p.add_argument("--interval", type=float, default=60)
    p = sub.add_parser("merge")
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(args.images, args.out)
    elif args.command == "resident":
        resident(args.images, args.out, args.interval)
    elif args.command == "evict":
        evict(args.images)
    else:
        merge(args.inputs, args.out)


if __name__ == "__main__":
    main()
