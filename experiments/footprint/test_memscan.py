#!/usr/bin/env python3
"""Host-only check of memscan.py without a VM (the KVM/EPT path is not exercised here).

    taskset -c 28,29 python3 test_memscan.py [--dir DIR] [--seconds 42]

A child process maps 512 MiB like Firecracker guest RAM (anonymous private, 2 MiB aligned, PROT_NONE guards) and fills
pages [0, 65536) with unique content, 1000 identical pages and 2000 zero pages (68536 present pages). It then writes
pages [0, 16384) (64 MiB) and reads pages [16384, 24576) (32 MiB) every 4 s, and lists itself in DIR/live.json.
memscan.py runs as root against it (idle 5 s, hash 15 s, settle 10 s, 2 shards). The test checks idle.csv, the access records,
the hash file and compress.csv, and reports the scanner's cost per GiB. Before memscan starts, the child measures the
soft-dirty cost on host PTEs: rewriting 64 MiB after `echo 4 > /proc/self/clear_refs` vs without (DIR/child.json).
"""

import argparse
import ctypes
import csv
import json
import os
from pathlib import Path
import re
import resource
import signal
import statistics
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memscan  # noqa: E402

PAGE = 4096
MEM_MIB = 512
N_PAGES = MEM_MIB * 256
UNIQUE = (0, 65536)
WRITE = (0, 16384)
READ = (16384, 24576)
DUP = (65536, 66536)
ZERO = (66536, 68536)
PRESENT = ZERO[1]
TOUCH_S = 4.0


def guest_like_ram():
    """512 MiB anonymous private rw at a 2 MiB boundary, with PROT_NONE guards so it cannot merge with neighbours."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    size, two_mib = MEM_MIB << 20, 2 << 20
    private_anon_noreserve, fixed = 0x02 | 0x20 | 0x4000, 0x10
    base = libc.mmap(None, size + 2 * two_mib, 0, private_anon_noreserve, -1, 0)
    addr = (base + PAGE + two_mib - 1) // two_mib * two_mib
    if libc.mmap(addr, size, 3, private_anon_noreserve | fixed, -1, 0) != addr:
        raise OSError(ctypes.get_errno(), "mmap fixed failed")
    return np.ctypeslib.as_array((ctypes.c_uint64 * (size // 8)).from_address(addr)).reshape(-1, 512)


def child(run):
    run = Path(run)
    pages = guest_like_ram()
    rng = np.random.default_rng(7)
    lo, hi = UNIQUE
    pages[lo:hi, 0] = np.arange(lo, hi, dtype=np.uint64) + 1
    pages[lo:hi, 1:64] = rng.integers(0, 2 ** 63, size=(hi - lo, 63), dtype=np.uint64)  # 1/8 random: compressible
    pages[DUP[0]:DUP[1]] = np.uint64(0xABABABABABABABAB)
    pages[ZERO[0]:ZERO[1], 0] = 1
    pages[ZERO[0]:ZERO[1]] = 0

    # soft-dirty cost on host PTEs: one store per page of the 64 MiB slice, with and without a clear_refs before it
    w = pages[WRITE[0]:WRITE[1]]
    plain, after_clear, clears = [], [], []
    for _ in range(5):
        t = time.perf_counter()
        w[:, 3] += np.uint64(1)
        plain.append(time.perf_counter() - t)
    for _ in range(5):
        t = time.perf_counter()
        with open("/proc/self/clear_refs", "w") as stream:
            stream.write("4")
        clears.append(time.perf_counter() - t)
        t = time.perf_counter()
        w[:, 3] += np.uint64(1)
        after_clear.append(time.perf_counter() - t)
    n = WRITE[1] - WRITE[0]
    bench = {"pages": n, "plain_us_per_page": statistics.median(plain) / n * 1e6,
             "after_clear_us_per_page": statistics.median(after_clear) / n * 1e6,
             "clear_refs_ms": statistics.median(clears) * 1e3, "present_pages": PRESENT}
    (run / "child.json").write_text(json.dumps(bench, indent=1))

    live = {"t_ns": time.monotonic_ns(), "vms": {"test": {"pid": os.getpid(), "mem_mib": MEM_MIB, "sampled": True,
                                                          "trace": "synthetic", "group": None,
                                                          "start_ns": time.monotonic_ns()}}}
    (run / "live.json.tmp").write_text(json.dumps(live))
    os.replace(run / "live.json.tmp", run / "live.json")
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    with open(run / "touch.log", "w") as touches:
        while not stop:
            pages[WRITE[0]:WRITE[1], 2] += np.uint64(1)
            int(pages[READ[0]:READ[1], 0].sum())
            touches.write(f"{time.monotonic_ns()}\n")
            touches.flush()
            time.sleep(TOUCH_S)


def near(value, want, rel=0.03, slack=16):
    return abs(value - want) <= max(slack, rel * want)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "child":
        return child(sys.argv[2])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", help="Run directory (default: a new temporary directory)")
    ap.add_argument("--seconds", type=float, default=42.0)
    args = ap.parse_args()
    run = Path(args.dir or tempfile.mkdtemp(prefix="memscan-test-"))
    run.mkdir(parents=True, exist_ok=True)
    cpus = ",".join(map(str, sorted(os.sched_getaffinity(0))))
    proc = subprocess.Popen([sys.executable, __file__, "child", str(run)])
    deadline = time.monotonic() + 60
    while not (run / "live.json").exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            sys.exit("child failed to start")
        time.sleep(0.2)
    with open(run / "memscan.out", "w") as out:
        scan = subprocess.Popen(["sudo", "-n", sys.executable, str(HERE / "memscan.py"), "--run", str(run),
                                 "--idle-interval", "5", "--hash-interval", "15", "--settle", "10", "--cpus", cpus,
                                 "--shards", "2"],
                                stdout=out, stderr=subprocess.STDOUT)
        time.sleep(args.seconds)
        (run / "memscan.stop").touch()
        scan.wait(timeout=60)
    maxrss_mib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    report(run, maxrss_mib)


def report(run, maxrss_mib):
    out = run / "memscan"
    checks = []

    def check(name, ok, detail):
        checks.append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    bench = json.loads((run / "child.json").read_text())
    rows = list(csv.DictReader(open(out / "idle.csv")))
    num = lambda r, k: int(r[k]) if r[k] != "" else None
    check("rounds", len(rows) >= 6, f"{len(rows)} idle rounds")
    check("present", all(near(num(r, "present"), PRESENT, 0, 64) for r in rows),
          f"{[num(r, 'present') for r in rows]} (want {PRESENT})")
    first, steady = rows[0], rows[1:]
    check("first round", num(first, "new_present") == num(first, "present") and first["written"] == "",
          f"new_present={first['new_present']} written='{first['written']}' (boot not counted as written)")
    want_acc = (WRITE[1] - WRITE[0]) + (READ[1] - READ[0])
    check("accessed", all(near(num(r, "accessed"), want_acc) for r in steady),
          f"{[num(r, 'accessed') for r in steady]} (want {want_acc}: 64 MiB written + 32 MiB read)")
    check("written", all(near(num(r, "written"), WRITE[1] - WRITE[0]) for r in steady),
          f"{[num(r, 'written') for r in steady]} (want {WRITE[1] - WRITE[0]})")
    check("new_present", all(num(r, "new_present") <= 16 for r in steady), f"{[num(r, 'new_present') for r in steady]}")
    last = rows[-1]
    t_span = (int(last["t_ns"]) - int(first["t_ns"])) / 1e9
    if t_span >= 31:
        check("ages", near(num(last, "age_lt30"), want_acc) and near(num(last, "age_30_120"), PRESENT - want_acc),
              f"last round at +{t_span:.0f}s: lt30={last['age_lt30']} 30_120={last['age_30_120']} "
              f"ge120={last['age_ge120']}")
    records = list(memscan.read_access_log(next((out / "access").glob("*.bin"))))
    pres, acc, wr = records[-1][1], records[-1][2], records[-1][3]
    hot = np.zeros(N_PAGES, bool)
    hot[WRITE[0]:READ[1]] = True
    dirty = np.zeros(N_PAGES, bool)
    dirty[WRITE[0]:WRITE[1]] = True
    resident = np.zeros(N_PAGES, bool)
    resident[:PRESENT] = True
    check("access records", len(records) == len(rows) and records[0][3] is None
          and np.count_nonzero(pres != resident) <= 16 and np.count_nonzero(acc != hot) <= 16
          and np.count_nonzero(wr != dirty) <= 16,
          f"{len(records)} records; last: present mismatches {np.count_nonzero(pres != resident)}, accessed "
          f"mismatches {np.count_nonzero(acc != hot)}, written mismatches {np.count_nonzero(wr != dirty)}")

    files = sorted((out / "hash").glob("*.npz"))
    check("hash files", len(files) >= 2, f"{[f.name for f in files]}")
    z = np.load(files[-1])
    idx, h, zero, age = z["idx"], z["h"], z["zero"], z["age"]
    _, counts = np.unique(h[~zero], return_counts=True)
    unique_ok = len(np.unique(h[(idx < UNIQUE[1])])) == UNIQUE[1] - UNIQUE[0]
    check("hash", len(idx) == PRESENT and zero.sum() == ZERO[1] - ZERO[0] and counts.max() >= DUP[1] - DUP[0]
          and unique_ok, f"{len(idx)} pages, zero={int(zero.sum())}, largest duplicate set={counts.max()}, "
          f"unique pages distinct={unique_ok}, reasons={list(z['reason'])}")
    check("hash ages", np.all(age[idx < READ[1]] < 6) and np.nanmin(age[idx >= READ[1]]) > 6,
          f"hot slice max age {age[idx < READ[1]].max():.1f}s, others min age {np.nanmin(age[idx >= READ[1]]):.1f}s")
    comp = list(csv.DictReader(open(out / "compress.csv")))
    cold = [r for r in comp if r["kind"] == "cold" and int(r["pages"])]
    ratio = lambda r, k: int(r[k]) / int(r["raw_bytes"])
    check("compress", bool(cold) and all(0 < ratio(r, "lz4_bytes") < 1 for r in cold),
          f"{len(comp)} rows; last cold: pages={cold[-1]['pages']} lz4={ratio(cold[-1], 'lz4_bytes'):.3f} "
          f"zlib1={ratio(cold[-1], 'zlib1_bytes'):.3f}" if cold else "no cold rows")
    check("memory", maxrss_mib < 700, f"max RSS of a child process {maxrss_mib:.0f} MiB")

    text = (out.parent / "memscan.out").read_text()
    idle = [(float(a), float(b), float(c)) for a, b, c in
            re.findall(r"^(?:s\d+ )?idle .*vms=1 .*present=([\d.]+)GiB.*wall=([\d.]+)s cpu=([\d.]+)s", text, re.M)]
    hashes = [(float(a), float(b), float(c)) for a, b, c in
              re.findall(r"^(?:s\d+ )?hash .*read=([\d.]+)GiB wall=([\d.]+)s cpu=([\d.]+)s", text, re.M)]
    if len(idle) > 1:
        gib = idle[1][0]
        print(f"cost idle: first round {idle[0][1]:.3f}s wall {idle[0][2]:.3f}s cpu; steady median "
              f"{statistics.median(w for _, w, _ in idle[1:]):.3f}s wall "
              f"{statistics.median(c for _, _, c in idle[1:]):.3f}s cpu for {gib:.3f} GiB present "
              f"= {statistics.median(c for _, _, c in idle[1:]) / gib:.3f} s cpu per GiB per round")
    if hashes:
        print(f"cost hash: " + ", ".join(f"{g:.3f} GiB {w:.2f}s wall {c:.2f}s cpu" for g, w, c in hashes)
              + f" = {statistics.median(c / g for g, _, c in hashes):.2f} s cpu per GiB")
    print(f"soft-dirty on host PTEs: store per page {bench['plain_us_per_page']:.3f} us plain, "
          f"{bench['after_clear_us_per_page']:.3f} us after clear_refs; clear_refs itself "
          f"{bench['clear_refs_ms']:.2f} ms for {bench['present_pages']} present pages")
    print(f"output in {run}")
    sys.exit(0 if all(checks) else 1)


if __name__ == "__main__":
    main()
