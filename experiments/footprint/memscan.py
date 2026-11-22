#!/usr/bin/env python3
"""Guest RAM footprint of sampled Firecracker microVMs, for the capacity simulator. Runs as root beside fill.py.

    sudo -n python3 memscan.py --run RUN [--idle-interval 5] [--shards 4] [--hash-interval 300]
                               [--compress-frac 0.02] [--settle 30] [--cpus 0-3,64-67] [--no-dirty]

Reads RUN/live.json every idle round and scans the VMs with "sampled": true. Stops on RUN/memscan.stop, SIGTERM or
SIGINT. Times are time.monotonic_ns() (the clock of sandbox.py and fill.py). A page is 4 KiB. The guest page index is
the position in the guest RAM mapping(s) taken in address order, which for Firecracker is guest physical order
([0, 3 GiB) then [4 GiB, ...) when the guest is larger than 3 GiB).

--shards N runs N scanner processes; shard k owns the VMs with id % N == k (crc32 of the id if not numeric), so the
kernel work of page_idle runs in parallel. Threads do not help: each of the many short page_idle reads drops and
retakes the GIL, and 4 threads took 2.5x longer than 1 in the host test. All shards append to the same CSV files (one
write per flush; the parent writes the headers and meta.json first); stdout lines start with "s<k> ".

Idle rounds (every --idle-interval s, main thread of each shard)
  - present, swapped and soft-dirty pages from /proc/<pid>/pagemap. Pages mapped to the shared zero page are not
    present (they hold no memory). Accessed pages from /sys/kernel/mm/page_idle/bitmap: KVM clears the EPT A bit through
    the mmu notifier, so guest accesses show. A page that became present since the previous round counts as accessed.
    Only pages that read as accessed are marked idle again; THP tail pages take the head's bit.
  - written: pages soft-dirty since the previous round. The round ends with `echo 4 > /proc/<pid>/clear_refs`, which
    write-protects the VMM's PTEs and zaps the VM's whole EPT. The first round of a VM only arms this (written empty),
    so the boot is not counted. pagemap is read a second time just before the clear so that writes during the idle
    scan are not lost; the clear comes after the idle read because a zap hands EPT A bits to MGLRU, not to page_idle.
  memscan/idle.csv       t_ns, vm, present, accessed, new_present, age_lt30, age_30_120, age_ge120, swapped, scan_ms,
                         written, file
                         page counts; age_* = seconds since last access, over present pages; written empty = unknown;
                         file = present pages backed by a file or shmem (e.g. a snapshot memory file)
  memscan/access/<vm>.bin  one record per idle round, little-endian, no file header:
                           uint64 t_ns, uint32 n_pages, uint32 p_len, uint32 a_len, uint32 w_len   (struct "<QIIII")
                           p_len bytes  zlib(np.packbits(present))    pages present at this round
                           a_len bytes  zlib(np.packbits(accessed))   accessed since the previous round (incl. new pages)
                           w_len bytes  zlib(np.packbits(written))    soft-dirty since the previous round; w_len 0 =
                                                                      unknown (first round of a VM, or --no-dirty)
                         each mask covers all n_pages guest pages (np.packbits default bit order; unpack with
                         count=n_pages). read_access_log(path) yields (t_ns, present, accessed, written) bool arrays,
                         written None when unknown. A torn last record (scanner killed) is skipped.

Hash rounds (second thread of each shard, so a long round never delays the idle rounds; it holds the GIL only briefly
per 16 MiB chunk)
  every --hash-interval s for all settled VMs, and once for each VM when it is first --settle s old (process age).
  Reads only present pages from /proc/<pid>/mem (16 MiB at a time). Per page: 64-bit hash (two multilinear sums over
  the 512 premixed words with fixed random odd multipliers, seed HASH_SEED; 0 for zero pages), zero flag, and age from
  the idle rounds. A random --compress-frac sample of non-zero pages is compressed one page at a time: lz4 = raw LZ4
  block (what zswap stores; an lz4.frame adds about 15 B), zlib1 = raw deflate level 1 (zlib.compress adds 6 B).
  memscan/hash/<round:04d>.npz  (<round:04d>-s<k>.npz with --shards > 1; rounds count per shard) flat arrays with per-VM slices off[k]:off[k+1]: idx (uint32 guest page index),
                         h (uint64), age (float32 s, 0 = became present after the last idle round, NaN = not yet
                         scanned), zero (bool); and coff[k]:coff[k+1]: cidx (uint32), clz4, czlib (uint16 bytes).
                         Per VM: vms, reason (first|periodic), trace, group, pid, t_ns (read start), n_pages.
                         Round: round, t_start, t_end.
  memscan/compress.csv   t_ns, vm, kind (hot = age < 30 s or unknown, cold), pages, raw_bytes, lz4_bytes, zlib1_bytes

Also memscan/meta.json (settings, kernel knobs, zero-page pfn) and memscan/vms.jsonl (one line per VM: chosen guest
RAM mapping and how it was found). Files are chowned to the owner of RUN.

Limits: a page_idle read checks 64 pfns per bitmap word and does an rmap walk (plus a KVM mmu-notifier call) for every
tracked idle page, so the idle cost grows with present guest memory. Reading /proc/<pid>/mem marks pages accessed for
MGLRU (refs) but leaves page_idle alone; with MGLRU off it would clear PG_idle and make hashed pages look accessed
(warned at start).
"""

import argparse
import csv
import json
import mmap
import os
from pathlib import Path
import platform
import re
import signal
import struct
import sys
import threading
import time
import subprocess
import zlib

import lz4
import lz4.block
import numpy as np

PAGE = 4096
MIB = 1 << 20
PFN_MASK = np.uint64((1 << 55) - 1)
ONE = np.uint64(1)
IDLE_BITMAP = "/sys/kernel/mm/page_idle/bitmap"
KPAGEFLAGS = "/proc/kpageflags"
KPF_COMPOUND_HEAD, KPF_THP = 1 << 15, 1 << 22
THP_PAGES = 512
SYSFS_IO = 4096    # kernfs caps one read or write of the bitmap at a page (512 words)
READ_GAP = 1       # merge bitmap words into one read across gaps up to this (a read checks all 64 pfns of a word)
WRITE_GAP = 8      # zero words in a write cost only a bit test per pfn
HASH_CHUNK = 4096  # pages read from /proc/<pid>/mem at a time (16 MiB)
HASH_SEED = 0x6D656D7363616E
COLD_S = 30
FIND_GRACE_S = 10  # wait this long for the VMM to map guest RAM before falling back to the largest anonymous mappings
RECORD = struct.Struct("<QIIII")
IDLE_FIELDS = ["t_ns", "vm", "present", "accessed", "new_present", "age_lt30", "age_30_120", "age_ge120", "swapped",
               "scan_ms", "written", "file"]
COMPRESS_FIELDS = ["t_ns", "vm", "kind", "pages", "raw_bytes", "lz4_bytes", "zlib1_bytes"]
CLK_TCK = os.sysconf("SC_CLK_TCK")

_rng = np.random.default_rng(HASH_SEED)
M1 = _rng.integers(0, 2 ** 64, size=512, dtype=np.uint64) | ONE
M2 = _rng.integers(0, 2 ** 64, size=512, dtype=np.uint64) | ONE


LOG_PREFIX = ""


def log(message):
    print(LOG_PREFIX + message, flush=True)


def parse_cpus(text):
    cpus = set()
    for part in text.split(","):
        a, _, b = part.partition("-")
        cpus.update(range(int(a), int(b or a) + 1))
    return cpus


def read_text(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def starttime(pid):
    """Process start in clock ticks after boot (/proc/<pid>/stat field 22); survives exec, changes on pid reuse."""
    with open(f"/proc/{pid}/stat") as stream:
        stat = stream.read()
    return int(stat[stat.rindex(")") + 2:].split()[19])


def process_age_s(start_ticks):
    return time.clock_gettime(time.CLOCK_BOOTTIME) - start_ticks / CLK_TCK


def read_maps(pid):
    """[(start, end, perms, path)] from /proc/<pid>/maps."""
    maps = []
    with open(f"/proc/{pid}/maps") as stream:
        for line in stream:
            parts = line.split(None, 5)
            lo, hi = (int(x, 16) for x in parts[0].split("-"))
            maps.append((lo, hi, parts[1], parts[5].strip() if len(parts) > 5 else ""))
    return maps


def guest_ranges(maps, mem_mib, allow_fallback):
    """Choose the guest RAM mapping(s). Firecracker maps guest RAM anonymous private rw, 2 MiB aligned, one mapping per
    region (two when the guest is larger than 3 GiB). Returns (mappings, how, warning) or (None, None, None)."""
    want = mem_mib * MIB
    rw = [m for m in maps if m[2].startswith("rw")]
    pools = [("anon", [m for m in rw if not m[3]]),
             ("file", [m for m in rw if m[3] and not m[3].startswith("[")])]  # memfd, snapshot memory file, hugetlbfs
    for how, pool in pools:
        single = [[m] for m in pool if m[1] - m[0] == want]
        big = [m for m in pool if m[1] - m[0] >= MIB]
        pairs = [[a, b] for i, a in enumerate(big) for b in big[i + 1:] if (a[1] - a[0]) + (b[1] - b[0]) == want]
        found = single + pairs
        if found:
            warning = f"{len(found)} {how} candidates of {mem_mib} MiB, took the first" if len(found) > 1 else None
            if any("huge" in m[3] for m in found[0]):
                warning = "hugetlbfs guest RAM: page_idle cannot track it"
            return found[0], how + ("-split" if len(found[0]) == 2 else ""), warning
    if allow_fallback:
        big = [m for m in pools[0][1] if m[1] - m[0] >= 64 * MIB]
        if big:
            total = sum(m[1] - m[0] for m in big) // MIB
            return big, "fallback", f"no mapping set of {mem_mib} MiB; took {len(big)} anonymous >= 64 MiB ({total} MiB)"
    return None, None, None


def read_into(fd, array, offset, piece=None, tolerant=False):
    """pread until array is full (piece = max bytes per call); returns bytes read, short at EOF (or at an I/O error
    when tolerant, e.g. /proc/<pid>/mem hitting a page that vanished)."""
    view = memoryview(array).cast("B")
    done = 0
    while done < len(view):
        end = len(view) if piece is None else min(len(view), done + piece)
        try:
            n = os.preadv(fd, [view[done:end]], offset + done)
        except OSError:
            if not tolerant:
                raise
            break
        if n <= 0:
            break
        done += n
    return done


def segments(sorted_words, gap):
    """(start, end) index pairs of runs of sorted words whose gaps are at most gap."""
    brk = np.flatnonzero(np.diff(sorted_words) > gap) + 1
    return zip(np.r_[0, brk].tolist(), np.r_[brk, len(sorted_words)].tolist())


def spans(words, gap):
    """Group sorted unique bitmap words into spans whose gaps are at most gap. Returns (lo, length, pos, where): span
    start word and length, the span's offset in one buffer holding all spans back to back, and each word's index in it."""
    brk = np.flatnonzero(np.diff(words) > gap) + 1
    first = np.r_[0, brk]
    lo = words[first]
    length = (words[np.r_[brk - 1, len(words) - 1]] - lo + ONE).astype(np.int64)
    pos = np.r_[0, np.cumsum(length)[:-1]].astype(np.int64)
    span = np.repeat(np.arange(len(first)), np.diff(np.r_[first, len(words)]))
    return lo, length, pos, pos[span] + (words - lo[span]).astype(np.int64)


def bitmap_io(fd, buf, lo, length, pos, write):
    """One pread/pwrite per span (kernfs takes at most a page per call). Reads past max_pfn come back short (zeros)."""
    view = memoryview(buf).cast("B")
    for offset, size, at in zip((lo * np.uint64(8)).tolist(), (length * 8).tolist(), (pos * 8).tolist()):
        for k in range(0, size, SYSFS_IO):
            part = view[at + k:at + min(size, k + SYSFS_IO)]
            if write:
                os.pwrite(fd, part, offset + k)
            else:
                os.preadv(fd, [part], offset + k)


def idle_bits(fd, pfns):
    """True where the pfn's page is still idle (not accessed since marked)."""
    uniq, inverse = np.unique(pfns >> np.uint64(6), return_inverse=True)
    lo, length, pos, where = spans(uniq, READ_GAP)
    buf = np.zeros(int(length.sum()), np.uint64)
    bitmap_io(fd, buf, lo, length, pos, False)
    return ((buf[where][inverse] >> (pfns & np.uint64(63))) & ONE).astype(bool)


def mark_idle(fd, pfns):
    """Set the idle flag of these pfns only: the kernel skips zero bits, so other pages in the words are untouched."""
    if not len(pfns):
        return
    pfns = np.sort(pfns)
    words = pfns >> np.uint64(6)
    first = np.flatnonzero(np.r_[True, words[1:] != words[:-1]])
    lo, length, pos, where = spans(words[first], WRITE_GAP)
    buf = np.zeros(int(length.sum()), np.uint64)
    buf[where] = np.bitwise_or.reduceat(ONE << (pfns & np.uint64(63)), first)
    bitmap_io(fd, buf, lo, length, pos, True)


def zero_page_pfn():
    """pfn of the shared zero page: read-fault one anonymous page of our own and look it up in pagemap."""
    area = mmap.mmap(-1, PAGE, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    _ = area[0]
    view = np.frombuffer(area, np.uint8)
    entry = np.zeros(1, np.uint64)
    fd = os.open("/proc/self/pagemap", os.O_RDONLY)
    try:
        read_into(fd, entry, view.ctypes.data // PAGE * 8)
    finally:
        os.close(fd)
    del view
    area.close()
    return entry[0] & PFN_MASK


def page_hash(words):
    """64-bit hash of each row of 512 uint64 words."""
    x = words ^ (words >> np.uint64(29))
    h1 = (x * M1).sum(axis=1, dtype=np.uint64)
    h2 = (x * M2).sum(axis=1, dtype=np.uint64)
    return h1 ^ ((h2 >> np.uint64(32)) | (h2 << np.uint64(32)))


def raw_deflate_len(page):
    c = zlib.compressobj(1, zlib.DEFLATED, -15)
    return len(c.compress(page)) + len(c.flush())


def pack_mask(mask):
    return zlib.compress(np.packbits(mask).tobytes(), 1)


def read_access_log(path):
    """Yield (t_ns, present, accessed, written) per idle round from memscan/access/<vm>.bin: bool arrays over the guest
    pages; written is None when unknown (first round of a VM, or --no-dirty)."""
    data = Path(path).read_bytes()
    pos = 0
    while pos + RECORD.size <= len(data):
        t_ns, n, *lengths = RECORD.unpack_from(data, pos)
        pos += RECORD.size
        if pos + sum(lengths) > len(data):
            break  # torn last record
        masks = []
        for size in lengths:
            blob = data[pos:pos + size]
            pos += size
            masks.append(np.unpackbits(np.frombuffer(zlib.decompress(blob), np.uint8), count=n).astype(bool)
                         if size else None)
        yield (t_ns, *masks)


class Ctx:
    """Shared state: output files, kernel fds, tracked VMs."""

    def __init__(self, args):
        self.args = args
        self.run = Path(args.run)
        self.out = self.run / "memscan"
        owner = self.run.stat()
        self.uid, self.gid = owner.st_uid, owner.st_gid
        for d in (self.out, self.out / "access", self.out / "hash"):
            d.mkdir(parents=True, exist_ok=True)
            self.own(d)
        self.dirty = not args.no_dirty
        self.shard, self.shards = args.shard or 0, args.shards
        self.idle_fd = os.open(IDLE_BITMAP, os.O_RDWR)
        self.kpf_fd = os.open(KPAGEFLAGS, os.O_RDONLY)
        self.zero_pfn = zero_page_pfn()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.vms = {}      # vm id -> Vm
        self.pending = {}  # vm id -> (pid, starttime) whose guest RAM mapping is not found yet
        self.idle_csv, self.idle_writer = self.csv("idle.csv", IDLE_FIELDS)
        self.compress_csv, self.compress_writer = self.csv("compress.csv", COMPRESS_FIELDS)
        self.vms_log = self.open("vms.jsonl", "a")

    def mine(self, vid):
        key = int(vid) if str(vid).isdigit() else zlib.crc32(str(vid).encode())
        return key % self.shards == self.shard

    def own(self, path):
        try:
            os.chown(path, self.uid, self.gid)
        except OSError:
            pass

    def open(self, name, mode):
        stream = open(self.out / name, mode)
        self.own(self.out / name)
        return stream

    def csv(self, name, fields):
        new = not (self.out / name).exists()
        stream = self.open(name, "a")
        writer = csv.DictWriter(stream, fieldnames=fields)
        if new:
            writer.writeheader()
        return stream, writer

    def close(self):
        for vm in list(self.vms.values()):
            vm.close()
        for stream in (self.idle_csv, self.compress_csv, self.vms_log):
            stream.close()
        for fd in (self.idle_fd, self.kpf_fd):
            os.close(fd)
        self.close_output()

    def close_output(self):
        for root, dirs, files in os.walk(self.out):
            for name in dirs + files:
                self.own(os.path.join(root, name))


class Vm:
    """A sampled VM whose guest RAM mapping is known. Its fds are opened before the starttime check, so they stay bound
    to this process even if the pid is later reused."""

    def __init__(self, vid, info, start, mappings, how, warning, ctx):
        self.id, self.pid, self.start = vid, info["pid"], start
        self.mem_mib, self.trace, self.group = info.get("mem_mib"), info.get("trace") or "", info.get("group") or ""
        self.ranges = [(lo, (hi - lo) // PAGE) for lo, hi, _, _ in mappings]
        self.n = sum(n for _, n in self.ranges)
        self.vpn = np.concatenate([np.arange(lo // PAGE, lo // PAGE + n, dtype=np.uint64) for lo, n in self.ranges])
        slots = np.flatnonzero(self.vpn % np.uint64(THP_PAGES) == 0)
        slots = slots[slots + THP_PAGES - 1 < self.n]
        self.thp_slots = slots[self.vpn[slots + THP_PAGES - 1] == self.vpn[slots] + np.uint64(THP_PAGES - 1)]
        self.thp_seen = {}  # slot index -> (head pfn, is THP)
        self.prev = np.zeros(self.n, bool)  # present at the previous idle round
        self.last = np.full(self.n, -1, np.int64)  # t_ns of the last access, -1 = never
        self.rounds = 0
        self.armed = False  # soft-dirty cleared at least once
        self.hashed = False
        self.pm_fd = os.open(f"/proc/{self.pid}/pagemap", os.O_RDONLY)
        self.cr_fd = os.open(f"/proc/{self.pid}/clear_refs", os.O_WRONLY) if ctx.dirty else None
        if starttime(self.pid) != start:
            self.close_fds()
            raise ProcessLookupError("pid reused")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(vid))
        self.access = open(ctx.out / "access" / f"{name}.bin", "ab")
        ctx.own(ctx.out / "access" / f"{name}.bin")
        record = {"t_ns": time.monotonic_ns(), "vm": vid, "pid": self.pid, "starttime": start, "mem_mib": self.mem_mib,
                  "trace": self.trace, "group": self.group, "how": how, "warning": warning, "n_pages": self.n,
                  "mappings": [{"start": hex(lo), "end": hex(hi), "perms": p, "path": path}
                               for lo, hi, p, path in mappings]}
        ctx.vms_log.write(json.dumps(record) + "\n")
        ctx.vms_log.flush()
        if warning:
            log(f"warning: vm {vid}: {warning}")

    def close_fds(self):
        for fd in (self.pm_fd, self.cr_fd):
            if fd is not None:
                os.close(fd)
        self.pm_fd = self.cr_fd = None

    def close(self):
        self.close_fds()
        if hasattr(self, "access"):
            self.access.close()


def pagemap(fd, ranges, n):
    entries = np.empty(n, np.uint64)
    pos = 0
    for lo, count in ranges:
        if read_into(fd, entries[pos:pos + count], lo // PAGE * 8) != count * 8:
            raise ProcessLookupError("pagemap read short (process exited)")
        pos += count
    return entries


def decode(entries, zero_pfn):
    """(pfn, present, top): zero-page mappings count as not present; top = byte 7 of each entry (bit 63 present 0x80,
    62 swapped 0x40, 61 file/shmem 0x20), read through a byte view because full uint64 passes dominate the fixed cost."""
    top = entries.view(np.uint8)[7::8]  # little-endian (x86)
    pfn = entries & PFN_MASK
    present = top >= 0x80
    present[present] = pfn[present] != zero_pfn
    return pfn, present, top


def soft_dirty(entries):
    """Present pages with soft-dirty (bit 55 = top bit of byte 6)."""
    return (entries.view(np.uint8)[7::8] >= 0x80) & (entries.view(np.uint8)[6::8] >= 0x80)


def thp_rep(vm, ctx, pfn, present):
    """pfn to use for page_idle: THP tail pages read as accessed and ignore writes, so they take the head's pfn."""
    slots = vm.thp_slots
    if not len(slots):
        return pfn
    tail = slots + THP_PAGES - 1
    cand = slots[present[slots] & present[tail] & (pfn[slots] % np.uint64(THP_PAGES) == 0)
                 & (pfn[tail] == pfn[slots] + np.uint64(THP_PAGES - 1))]
    heads = []
    for i in cand.tolist():
        head = int(pfn[i])
        seen = vm.thp_seen.get(i)
        if seen is None or seen[0] != head:
            flags = np.zeros(1, np.uint64)
            read_into(ctx.kpf_fd, flags, head * 8)
            seen = vm.thp_seen[i] = (head, bool(int(flags[0]) & KPF_THP and int(flags[0]) & KPF_COMPOUND_HEAD))
        if seen[1]:
            heads.append(i)
    if not heads:
        return pfn
    heads = np.array(heads)
    rep = pfn.copy()
    rep[(heads[:, None] + np.arange(THP_PAGES)).ravel()] = np.repeat(pfn[heads], THP_PAGES)
    return rep


def idle_vm(vm, ctx):
    """One idle round of one VM: returns the idle.csv row and appends the access record."""
    began = time.perf_counter()
    if starttime(vm.pid) != vm.start:
        raise ProcessLookupError("pid reused")
    pfn, present, top = decode(pagemap(vm.pm_fd, vm.ranges, vm.n), ctx.zero_pfn)
    rep = thp_rep(vm, ctx, pfn, present)
    old = present & vm.prev
    accessed = present.copy()
    if old.any():
        accessed[old] = ~idle_bits(ctx.idle_fd, rep[old])
    mark_idle(ctx.idle_fd, rep[accessed])
    now = time.monotonic_ns()
    written = None
    if ctx.dirty:
        if vm.armed:  # a zero-page PTE is never soft-dirty once the VMA's VM_SOFTDIRTY is cleared
            written = soft_dirty(pagemap(vm.pm_fd, vm.ranges, vm.n))
        os.write(vm.cr_fd, b"4")
        vm.armed = True
    new = present & ~vm.prev
    vm.last[accessed] = now
    vm.prev = present
    vm.rounds += 1
    age = now - vm.last[present]
    n_present = int(np.count_nonzero(present))
    lt30 = int(np.count_nonzero(age < 30 * 10 ** 9))
    lt120 = int(np.count_nonzero(age < 120 * 10 ** 9))
    blobs = [pack_mask(present), pack_mask(accessed), pack_mask(written) if written is not None else b""]
    vm.access.write(RECORD.pack(now, vm.n, *map(len, blobs)) + b"".join(blobs))
    return {"t_ns": now, "vm": vm.id, "present": n_present, "accessed": int(np.count_nonzero(accessed)),
            "new_present": int(np.count_nonzero(new)), "age_lt30": lt30, "age_30_120": lt120 - lt30,
            "age_ge120": n_present - lt120, "swapped": int(np.count_nonzero(top & 0x40)),
            "scan_ms": round((time.perf_counter() - began) * 1e3, 1),
            "written": "" if written is None else int(np.count_nonzero(written)),
            "file": int(np.count_nonzero(present & ((top & 0x20) > 0)))}


def read_live(run):
    try:
        with open(run / "live.json") as stream:
            return json.load(stream).get("vms", {})
    except (OSError, ValueError):
        return None  # not written yet or caught mid-replace: keep the current set


def refresh(ctx, live):
    """Add sampled VMs from live.json once their guest RAM mapping is found; drop VMs that left or exited."""
    if live is None:
        return
    sampled = {vid: info for vid, info in live.items() if info.get("sampled") and info.get("pid") and ctx.mine(vid)}
    for vid in [v for v in ctx.vms if v not in sampled or sampled[v]["pid"] != ctx.vms[v].pid]:
        drop(ctx, vid, "left live.json")
    for vid in [v for v in ctx.pending if v not in sampled or sampled[v]["pid"] != ctx.pending[v][0]]:
        del ctx.pending[vid]
    for vid, info in sampled.items():
        if vid in ctx.vms:
            continue
        pid = info["pid"]
        try:
            if vid not in ctx.pending:
                ctx.pending[vid] = (pid, starttime(pid))
            start = ctx.pending[vid][1]
            if starttime(pid) != start:
                raise ProcessLookupError
            mappings, how, warning = guest_ranges(read_maps(pid), info.get("mem_mib") or 0,
                                                  process_age_s(start) >= FIND_GRACE_S)
            if mappings is None:
                continue
            vm = Vm(vid, info, start, mappings, how, warning, ctx)
        except (ProcessLookupError, FileNotFoundError, PermissionError):
            ctx.pending.pop(vid, None)  # exited (live.json lags a little); it will not come back with this pid
            continue
        with ctx.lock:
            ctx.vms[vid] = vm
        del ctx.pending[vid]


def drop(ctx, vid, why):
    with ctx.lock:
        vm = ctx.vms.pop(vid, None)
    if vm:
        vm.close()
        log(f"drop vm {vid}: {why} after {vm.rounds} rounds")


def idle_round(ctx, t0):
    began, cpu = time.perf_counter(), time.thread_time()
    refresh(ctx, read_live(ctx.run))
    present = accessed = written = 0
    scanned = 0
    for vid, vm in list(ctx.vms.items()):
        try:
            row = idle_vm(vm, ctx)
        except OSError as error:  # exited, or pid reused
            drop(ctx, vid, repr(error))
            continue
        except Exception as error:  # a bug for this VM should not stop the others
            drop(ctx, vid, f"scan failed: {error!r}")
            continue
        ctx.idle_writer.writerow(row)
        vm.access.flush()
        scanned += 1
        present += row["present"]
        accessed += row["accessed"]
        written += row["written"] or 0
    ctx.idle_csv.flush()
    log(f"idle +{time.monotonic() - t0:7.1f}s vms={scanned} pending={len(ctx.pending)} "
        f"present={present * PAGE / 2 ** 30:.3f}GiB acc={accessed * PAGE / 2 ** 20:.0f}MiB "
        f"wr={written * PAGE / 2 ** 20:.0f}MiB wall={time.perf_counter() - began:.3f}s "
        f"cpu={time.thread_time() - cpu:.3f}s")


def hash_vm(vm, ctx, rng, frac):
    """Hash the present pages of one VM; returns the per-VM arrays (None if it exited)."""
    mem_fd = os.open(f"/proc/{vm.pid}/mem", os.O_RDONLY)
    pm_fd = os.open(f"/proc/{vm.pid}/pagemap", os.O_RDONLY)
    try:
        if starttime(vm.pid) != vm.start:
            return None
        t_ns = time.monotonic_ns()
        pfn, present, _ = decode(pagemap(pm_fd, vm.ranges, vm.n), ctx.zero_pfn)
        idx = np.flatnonzero(present)
        prev, last = vm.prev[idx], vm.last[idx]
        age = np.where(prev, (t_ns - last) / 1e9, 0.0).astype(np.float32)
        age[prev & (last < 0)] = np.nan
        h = np.zeros(len(idx), np.uint64)
        zero = np.zeros(len(idx), bool)
        ok = np.ones(len(idx), bool)
        cidx, clz4, czlib = [], [], []
        buf = np.empty(HASH_CHUNK * PAGE, np.uint8)
        view = memoryview(buf)
        for a in range(0, len(idx), HASH_CHUNK):
            if ctx.stop.is_set():
                return None
            sub = idx[a:a + HASH_CHUNK]
            vpn = vm.vpn[sub]
            for s, e in segments(vpn, 1):  # runs of consecutive virtual pages; never touch a non-present page
                got = read_into(mem_fd, buf[s * PAGE:e * PAGE], int(vpn[s]) * PAGE, tolerant=True)
                if got < (e - s) * PAGE:
                    ok[a + s + got // PAGE:a + e] = False
            words = buf[:len(sub) * PAGE].view(np.uint64).reshape(-1, 512)
            z = ~words.any(axis=1)
            zero[a:a + len(sub)] = z
            nz = np.flatnonzero(~z)
            if len(nz) < 0.75 * len(sub):  # many zero pages: hash only the others
                for c in range(0, len(nz), 1024):
                    h[a + nz[c:c + 1024]] = page_hash(words[nz[c:c + 1024]])
            else:
                for c in range(0, len(sub), 1024):
                    h[a + c:a + min(c + 1024, len(sub))] = page_hash(words[c:c + 1024])
                h[a + np.flatnonzero(z)] = 0
            for j in nz[rng.random(len(nz)) < frac].tolist():
                if ok[a + j]:
                    page = view[j * PAGE:(j + 1) * PAGE]
                    cidx.append(int(sub[j]))
                    clz4.append(len(lz4.block.compress(page, store_size=False)))
                    czlib.append(raw_deflate_len(page))
        if not ok.any() and len(idx):
            return None
        keep = ok  # pages whose read failed (the VM exited or discarded them meanwhile) are left out
        cidx = np.array(cidx, np.uint32)
        return {"t_ns": t_ns, "idx": idx[keep].astype(np.uint32), "h": h[keep], "age": age[keep], "zero": zero[keep],
                "cidx": cidx, "clz4": np.array(clz4, np.uint16), "czlib": np.array(czlib, np.uint16),
                "cage": age[np.searchsorted(idx, cidx)] if len(cidx) else np.zeros(0, np.float32)}
    finally:
        os.close(mem_fd)
        os.close(pm_fd)


def hash_round(ctx, number, batch, t0):
    """Hash a batch of (vm, reason); write hash/<number>.npz and compress.csv rows."""
    began, cpu, t_start = time.perf_counter(), time.thread_time(), time.monotonic_ns()
    rng = np.random.default_rng([HASH_SEED, number])
    done = []
    for vm, reason in batch:
        try:
            result = hash_vm(vm, ctx, rng, ctx.args.compress_frac)
        except (ProcessLookupError, FileNotFoundError, OSError):
            result = None
        if ctx.stop.is_set():
            return False
        if result is not None:
            vm.hashed = True
            done.append((vm, reason, result))
    if not done:
        return False
    flat = {key: np.concatenate([r[key] for _, _, r in done]) for key in ("idx", "h", "age", "zero", "cidx", "clz4",
                                                                           "czlib")}
    off = np.cumsum([0] + [len(r["idx"]) for _, _, r in done])
    coff = np.cumsum([0] + [len(r["cidx"]) for _, _, r in done])
    path = ctx.out / "hash" / (f"{number:04d}.npz" if ctx.shards == 1 else f"{number:04d}-s{ctx.shard}.npz")
    np.savez_compressed(path, **flat, off=off, coff=coff, vms=np.array([str(vm.id) for vm, _, _ in done]),
                        reason=np.array([reason for _, reason, _ in done]),
                        trace=np.array([vm.trace for vm, _, _ in done]), group=np.array([vm.group for vm, _, _ in done]),
                        pid=np.array([vm.pid for vm, _, _ in done]), t_ns=np.array([r["t_ns"] for _, _, r in done]),
                        n_pages=np.array([vm.n for vm, _, _ in done]), round=number, t_start=t_start,
                        t_end=time.monotonic_ns())
    ctx.own(path)
    pages = 0
    for vm, _, r in done:
        pages += len(r["idx"])
        cold = r["cage"] >= COLD_S  # NaN (not yet scanned) counts as hot
        for kind, sel in (("hot", ~cold), ("cold", cold)):
            ctx.compress_writer.writerow({"t_ns": r["t_ns"], "vm": vm.id, "kind": kind, "pages": int(sel.sum()),
                                          "raw_bytes": int(sel.sum()) * PAGE,
                                          "lz4_bytes": int(r["clz4"][sel].astype(np.int64).sum()),
                                          "zlib1_bytes": int(r["czlib"][sel].astype(np.int64).sum())})
    ctx.compress_csv.flush()
    reasons = sorted({reason for _, reason, _ in done})
    log(f"hash +{time.monotonic() - t0:7.1f}s round={number:04d} {'+'.join(reasons)} vms={len(done)} "
        f"read={pages * PAGE / 2 ** 30:.3f}GiB wall={time.perf_counter() - began:.2f}s "
        f"cpu={time.thread_time() - cpu:.2f}s")
    return True


def hash_loop(ctx, t0):
    """Periodic rounds over settled VMs, plus a first round for each VM when it settles."""
    number = 0
    next_periodic = time.monotonic() + ctx.args.hash_interval
    while not ctx.stop.wait(1.0):
        with ctx.lock:
            vms = [vm for vm in ctx.vms.values() if vm.rounds > 0]
        settled = [vm for vm in vms if process_age_s(vm.start) >= ctx.args.settle]
        if time.monotonic() >= next_periodic:
            next_periodic = max(next_periodic + ctx.args.hash_interval, time.monotonic())
            batch = [(vm, "periodic" if vm.hashed else "first") for vm in settled]
        else:
            batch = [(vm, "first") for vm in settled if not vm.hashed]
        if batch:
            try:
                if hash_round(ctx, number, batch, t0):
                    number += 1
            except Exception as error:  # keep the idle rounds going
                log(f"hash round {number} failed: {error!r}")


def write_meta(ctx):
    knobs = {name: read_text(path) for name, path in [
        ("lru_gen", "/sys/kernel/mm/lru_gen/enabled"), ("thp", "/sys/kernel/mm/transparent_hugepage/enabled"),
        ("ept", "/sys/module/kvm_intel/parameters/ept"), ("eptad", "/sys/module/kvm_intel/parameters/eptad"),
        ("ksm_run", "/sys/kernel/mm/ksm/run"), ("numa_balancing", "/proc/sys/kernel/numa_balancing"),
        ("zswap", "/sys/module/zswap/parameters/enabled")]}
    meta = {"t_ns": time.monotonic_ns(), "args": vars(ctx.args), "kernel": platform.release(), "knobs": knobs,
            "zero_pfn": int(ctx.zero_pfn), "hash_seed": HASH_SEED, "numpy": np.__version__, "lz4": lz4.__version__,
            "page": PAGE, "idle_fields": IDLE_FIELDS, "record": RECORD.format}
    with ctx.open("meta.json", "w") as stream:
        json.dump(meta, stream, indent=1)
    if knobs["lru_gen"] in ("", "0x0000"):
        log("warning: MGLRU is off: hash reads clear PG_idle, so hashed pages show as accessed in the next idle round")
    if knobs["eptad"] not in ("Y", "1"):
        log("warning: KVM EPT A/D bits are off: guest accesses may not show in page_idle")


def run_shards(args):
    """Parent of the shard processes: create the output files with headers and meta.json, start one child per shard,
    pass SIGTERM on, wait, and hand every file to the owner of RUN."""
    ctx = Ctx(args)
    write_meta(ctx)
    ctx.close()
    command = [sys.executable, os.path.abspath(__file__), *sys.argv[1:]]
    children = [subprocess.Popen(command + ["--shard", str(k)]) for k in range(args.shards)]
    forward = lambda *_: [child.send_signal(signal.SIGTERM) for child in children if child.poll() is None]
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    codes = [child.wait() for child in children]
    Ctx.close_output(ctx)
    log(f"memscan: {args.shards} shards exited {codes}")
    return max(codes)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True, help="Run directory with live.json; output goes to RUN/memscan/")
    ap.add_argument("--idle-interval", type=float, default=5.0, help="Seconds between idle/written rounds")
    ap.add_argument("--hash-interval", type=float, default=300.0, help="Seconds between page hash rounds")
    ap.add_argument("--compress-frac", type=float, default=0.02, help="Fraction of non-zero pages compressed")
    ap.add_argument("--settle", type=float, default=30.0, help="Process age (s) before a VM's first hash round")
    ap.add_argument("--cpus", help="CPU list for this scanner, e.g. 0-3,64-67")
    ap.add_argument("--shards", type=int, default=4, help="Scanner processes; shard k owns VMs with id %% N == k")
    ap.add_argument("--shard", type=int, help=argparse.SUPPRESS)  # set by the parent for each child
    ap.add_argument("--no-dirty", action="store_true", help="No soft-dirty write tracking (no clear_refs)")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("memscan: run as root (pagemap pfns, page_idle and other users' /proc/<pid>/mem need it)")
    if args.cpus:
        os.sched_setaffinity(0, parse_cpus(args.cpus))
    os.umask(0o022)
    if args.shards > 1 and args.shard is None:
        return run_shards(args)
    global LOG_PREFIX
    LOG_PREFIX = f"s{args.shard} " if args.shards > 1 else ""
    ctx = Ctx(args)
    if args.shards == 1:
        write_meta(ctx)
    signal.signal(signal.SIGTERM, lambda *_: ctx.stop.set())
    signal.signal(signal.SIGINT, lambda *_: ctx.stop.set())
    stop_file = ctx.run / "memscan.stop"
    t0 = time.monotonic()
    log(f"memscan: run={ctx.run} idle={args.idle_interval}s hash={args.hash_interval}s dirty={ctx.dirty} "
        f"cpus={sorted(os.sched_getaffinity(0))} zero_pfn={int(ctx.zero_pfn)}")
    hasher = threading.Thread(target=hash_loop, args=(ctx, t0), daemon=True)
    hasher.start()
    next_round = time.monotonic()
    try:
        while not ctx.stop.is_set():
            if stop_file.exists():
                break
            if time.monotonic() >= next_round:
                idle_round(ctx, t0)
                next_round += args.idle_interval
                if next_round < time.monotonic():  # overran: skip the missed rounds
                    log("warning: idle round longer than the interval")
                    next_round = time.monotonic() + args.idle_interval
            ctx.stop.wait(min(0.5, max(0.0, next_round - time.monotonic())))
    finally:
        ctx.stop.set()
        hasher.join(timeout=30)
        ctx.close()
        log(f"memscan: stopped after {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
