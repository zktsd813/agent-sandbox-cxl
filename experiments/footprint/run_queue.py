#!/usr/bin/env python3
"""Run the footprint measurement queue unattended (plan: docs/results/sandbox-footprint-20261118, section 9).

  run_queue.py precheck              refuse to start while other Firecracker/QEMU VMs run; report CPUs, governors, memory
  run_queue.py run [--only 1,2]      precheck, bring CPUs 96-127 online (user instruction 11-18), run the items in order
                                 while each still fits before --end, then put CPUs 96-127 back offline

Three experiments (user, 11-18: at most three, core only):
  1. fill      RL (GRPO 8), SWE, CI on 60 cores with the whole memory, sandboxes admitted while memory has room:
               which ceiling comes first at ~2.5 GiB/core, and the per-sandbox records (memscan) for compression,
               dedup, snapshot and the simulator
  2. overflow  RL at the count experiment 1 reached, memory budget 100/80/65% of what it used, no admission, swap
               allowed in its own cgroup: thrashing (refaults, swap, PSI, command slowdown, OOM)
  3. 4 GiB/core the counts of experiment 1 (RL, CI) on 38 cores: direct check of the simulator
  run_queue.py smoke                 two VMs of a synthetic trace with known memory use (smoke_trace.py), then four RL
                                 sandboxes for 4 minutes, memscan on all of them (tool check)

Each item runs `fill.py` in its own systemd user scope (MemoryMax = budget; swap only for experiment 2). The budget is fixed once at the
start: free + file memory of NUMA nodes 0-1 minus --host-gib, capped at --max-budget-gib. A failed item is logged and
the queue moves on; nothing waits for approval.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
FILL = HERE / "fill.py"
LOG_DIR = HERE / ".local/queue"
KST = datetime.timezone(datetime.timedelta(hours=9))
SMT_SIBLINGS = range(96, 128)  # node 1 siblings, offline at boot (nosmt)
SHAPE = {"60": "4-63,68-127", "38": "4-41,68-105"}  # sandbox CPUs: 60 or 38 physical cores with both threads
SWAP_GIB = 16  # /dev/sda3 (SATA SSD); only the overflow runs may use it, through their own cgroup limit


def fill_plan(cores, budget, swap, extra):
    return {"cores": cores, "budget": budget, "swap": swap, "extra": extra}


def overflow(results, full, level):
    """Experiment 2: the RL count reached in experiment 1, budget = level x the memory it used, no admission."""
    last = results["1-rl"]["steps"][-1]
    n = max(8, int(last["live_mean"]) // 8 * 8)
    budget = max(8, round(last["nonfile_gib"] * level))
    return fill_plan("60", budget, SWAP_GIB, f"rl --group-size 8 --no-admission --stagger-s 240 --ramp {n}:1320")


# name, minutes, plan(results, full budget) -> fill plan; items whose inputs are missing are skipped
ITEMS = [
    # 1. Fill: which ceiling comes first at ~2.3 GiB/core; records for compression, dedup, snapshot, simulator
    ("1-rl", 46, lambda r, f: fill_plan("60", f, 0, "rl --group-size 8 --ramp 96:420,192:420,320:420,512:1500")),
    ("1-swe", 28, lambda r, f: fill_plan("60", f, 0, "swe --ramp 24:420,40:420,56:420,72:420")),
    # 1-swe used ~6 CPUs at 56 sandboxes (1 vCPU each, 0.39 GiB each): continue to the memory ceiling (~320)
    ("1-swe-hi", 24, lambda r, f: fill_plan("60", f, 0, "swe --ramp 96:420,160:420,240:600")),
    ("1-ci", 32, lambda r, f: fill_plan("60", f, 0, "ci --ramp 256:300,512:300,1024:300,1600:900")),
    # 1-rl reached 512 without a memory or CPU limit (0.19 GiB per sandbox): continue to the ceiling
    ("1-rl-hi", 32, lambda r, f: fill_plan("60", f, 0, "rl --group-size 8 --ramp 512:420,768:420,1024:1080")),
    # 2. Overflow: thrashing past the memory ceiling (overcommit 1.0, 1.25, 1.5), swap allowed
    ("2-rl-100", 22, lambda r, f: overflow(r, f, 1.00)),
    ("2-rl-80", 22, lambda r, f: overflow(r, f, 0.80)),
    ("2-rl-65", 22, lambda r, f: overflow(r, f, 0.65)),
    # 3. ~3.7 GiB/core: same memory on 38 cores, filled with admission, which ceiling comes first there
    ("3-rl-38", 26, lambda r, f: fill_plan("38", f, 0, "rl --group-size 8 --ramp 384:420,640:420,1024:720")),
    ("3-ci-38", 22, lambda r, f: fill_plan("38", f, 0, "ci --ramp 512:300,1024:300,1600:600")),
    ("3-swe-38", 20, lambda r, f: fill_plan("38", f, 0, "swe --ramp 128:420,240:600")),
]


def log(message):
    line = f"{datetime.datetime.now(KST):%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "queue.log").open("a") as stream:
        stream.write(line + "\n")


MEM_NODES = (0,)  # guest memory on node 0 only since 11-18 21:45 (node 1 belongs to another user's cpuset)


def node_room_gib():
    total = 0
    for n in MEM_NODES:
        f = {l.split()[2].rstrip(":"): int(l.split()[3]) for l in open(f"/sys/devices/system/node/node{n}/meminfo")}
        total += f["MemFree"] + f["FilePages"] - f["Shmem"] + f.get("SReclaimable", 0)  # dentry/inode caches give way
    return total / 2**20


def foreign_vms():
    out = subprocess.run(["pgrep", "-a", "-f", "firecracker|qemu-system"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "pgrep" not in l]


def precheck(strict=True):
    vms = foreign_vms()
    online = Path("/sys/devices/system/cpu/online").read_text().strip()
    governors = {}
    for c in range(os.cpu_count() or 128):
        g = Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor")
        if g.exists():
            governors.setdefault(g.read_text().strip(), []).append(c)
    heavy = subprocess.run(["ps", "-eo", "user,pcpu,rss,args", "--sort=-pcpu"], capture_output=True, text=True).stdout
    others = [l for l in heavy.splitlines()[1:] if not l.startswith(("ijkim", "root")) and float(l.split()[1]) > 20]
    report = {"vms": vms, "cpus_online": online, "governors": {k: len(v) for k, v in governors.items()},
              "node01_room_gib": round(node_room_gib(), 1), "other_users_busy": others[:5]}
    log("precheck " + json.dumps(report))
    if strict and vms:
        raise SystemExit("Other VMs are running; not starting (the other session's experiment must finish first)")
    return report


def set_siblings(online):
    for c in SMT_SIBLINGS:
        p = Path(f"/sys/devices/system/cpu/cpu{c}/online")
        if p.exists() and p.read_text().strip() != str(int(online)):
            subprocess.run(["sudo", "-n", "sh", "-c", f"echo {int(online)} > {p}"], check=True)
    if online:
        for c in SMT_SIBLINGS:
            g = Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor")
            if g.exists() and g.read_text().strip() != "performance":
                subprocess.run(["sudo", "-n", "sh", "-c", f"echo performance > {g}"], check=True)
    log(f"CPUs 96-127 {'online' if online else 'offline'}; online = "
        f"{Path('/sys/devices/system/cpu/online').read_text().strip()}")


def run_item(name, minutes, plan, out_root, baseline=None):
    out = out_root / name
    # OOMPolicy=continue: a kernel OOM kill of one VM must not stop the whole run (systemd's default stops the scope;
    # 1-swe-hi and 1-rl-hi ended that way on 11-18).
    command = ["systemd-run", "--user", "--scope", "-q", "-p", "OOMPolicy=continue", "-p", f"MemoryMax={plan['budget']}G",
               "-p", f"MemorySwapMax={plan['swap']}G", sys.executable, str(FILL), *plan["extra"].split(),
               "--cpus", SHAPE[plan["cores"]], "--out", str(out)]
    log(f"start {name}: {' '.join(command)}")
    begin = time.monotonic()
    with (out_root / f"{name}.log").open("w") as stream:
        rc = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                            timeout=(minutes + 20) * 60).returncode
    log(f"end {name}: rc {rc}, {(time.monotonic() - begin) / 60:.1f} min")
    freed = 0  # a killed fill leaves its VMs' writable disks behind (11-18 22:00: they filled /)
    for disk in list(out.glob("vm/*/upper.ext4")) + list(out.glob("vm/*/job.ext4")):
        try:
            freed += disk.stat().st_blocks * 512
            disk.unlink()
        except OSError:
            pass
    if freed:
        log(f"{name}: removed leftover VM disks, {freed / 2**30:.1f} GiB")
    leftovers = foreign_vms()
    if leftovers:
        log(f"{len(leftovers)} VMs still running after {name}; killing ours")
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", f"firecracker.*{out}"])
        time.sleep(10)
    summary = subprocess.run([sys.executable, str(HERE / "to_sim.py"), "steps", str(out), "--json",
                              str(out / "steps-summary.json")]
                             + (["--baseline", str(out_root / baseline)] if baseline else []),
                             capture_output=True, text=True)
    log(f"{name} steps:\n{summary.stdout}{summary.stderr[-500:]}")
    path = out / "steps-summary.json"
    return json.loads(path.read_text()) if path.exists() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("precheck", "run", "smoke"))
    ap.add_argument("--only", help="Comma-separated experiment numbers or item names, e.g. 1 or 1-rl,2-rl-80")
    ap.add_argument("--end", default="2026-11-19T13:00+09:00", help="Every item must finish before this time")
    ap.add_argument("--host-gib", type=float, default=6.0, help="Node 0 memory left to the host outside the budget")
    ap.add_argument("--max-budget-gib", type=float, default=152.0)
    ap.add_argument("--reuse", type=Path, help="Queue directory of an earlier run: items finished there are not rerun "
                                               "(their summaries feed later items) and new items write there too")
    ap.add_argument("--wait-vms-min", type=float, default=0, help="Wait up to this long for other VMs to end")
    args = ap.parse_args()
    if args.action == "precheck":
        precheck(strict=False)
        return
    waited = time.monotonic()
    while foreign_vms() and time.monotonic() - waited < args.wait_vms_min * 60:
        time.sleep(20)
    precheck()
    if args.reuse:
        out_root = args.reuse
    else:
        out_root = LOG_DIR / (datetime.datetime.now(KST).strftime("%Y%m%dT%H%M") + ("-smoke" if args.action == "smoke" else ""))
        out_root.mkdir(parents=True)
    set_siblings(True)
    try:
        budget_full = int(min(args.max_budget_gib, node_room_gib() - args.host_gib))
        log(f"budget {budget_full} GiB for full-memory items")
        if args.action == "smoke":
            subprocess.run([sys.executable, str(HERE / "smoke_trace.py"), "--copies", "2"], check=True)
            run_item("smoke-known", 4, fill_plan("60", budget_full, 0, "rl --candidates "
                     f"{HERE / '.local/smoke/candidates-smoke.json'} --ramp 2:90 --stagger-s 1 --scan 2 "
                     "--memscan-args=--hash-interval=30"), out_root)
            run_item("smoke-rl", 6, fill_plan("60", budget_full, 0, "rl --names " + " ".join(smoke_traces())
                     + " --ramp 4:240 --scan 4 --memscan-args=--hash-interval=60"), out_root)
            return
        end = datetime.datetime.fromisoformat(args.end)
        results = {}
        chosen = [i for i in ITEMS if not args.only or i[0].split("-")[0] in args.only.split(",")
                  or i[0] in args.only.split(",")]
        for name, minutes, make in chosen:
            done = out_root / name / "done.json"
            for _ in range(18):  # an item still finishing in the reused directory (VMs gone, files closing)
                if done.exists() or not (out_root / name).exists():
                    break
                time.sleep(10)
            if (out_root / name).exists() and not done.exists():
                log(f"skip {name}: {out_root / name} exists without done.json")
                continue
            if done.exists():  # finished in the reused directory
                summary = out_root / name / "steps-summary.json"
                if not summary.exists():
                    subprocess.run([sys.executable, str(HERE / "to_sim.py"), "steps", str(out_root / name), "--json",
                                    str(summary)], capture_output=True)
                results[name] = json.loads(summary.read_text()) if summary.exists() else None
                log(f"reuse {name} from {out_root}")
                continue
            fs = os.statvfs(out_root)
            if fs.f_bavail * fs.f_frsize < 30 * 2**30:
                log(f"skip {name}: less than 30 GiB free on the run file system")
                continue
            left = (end - datetime.datetime.now(KST)).total_seconds() / 60
            if left < minutes + 5:
                log(f"skip {name}: needs {minutes} min, {left:.0f} min left before {args.end}")
                continue
            try:
                plan = make(results, budget_full)
            except (KeyError, IndexError, TypeError) as exc:
                log(f"skip {name}: its input from an earlier item is missing ({exc!r})")
                continue
            try:
                source = ("1-ci" if name.startswith("3-ci") else "1-swe" if name in ("1-swe-hi", "3-swe-38")
                          else "1-rl" if name[0] in "23" or name == "1-rl-hi" else None)
                results[name] = run_item(name, minutes, plan, out_root, source)
            except Exception as exc:  # keep going overnight
                log(f"item {name} failed: {exc!r}")
    finally:
        set_siblings(False)
        log("queue finished")


def smoke_traces():
    cands = json.loads(Path("/home/ijkim/agent-sandbox-cxl/experiments/contention/candidates-plan.json").read_text())
    images = Path("/home/ijkim/agent-sandbox-cxl/experiments/contention/.local/images")
    have = [e["name"] for e in cands["traces"] if (images / e["image_key"] / "image.json").exists()]
    return have[:4]


if __name__ == "__main__":
    main()
