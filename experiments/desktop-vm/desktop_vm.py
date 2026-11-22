#!/usr/bin/env python3
"""Desktop agent VMs (OSWorld Ubuntu image under QEMU/KVM) replaying public OSWorld-Verified trajectories (11-22).

  desktop_vm.py tasks  [--models M,...]                       list replayable (task, trajectory) pairs -> tasks.json
  desktop_vm.py smoke  [--task DOMAIN/ID] [--mem-mib 4096 --vcpus 4]   one VM, one episode, print timings
  desktop_vm.py fill   --run DIR --slots N --minutes T [--mem-mib 4096 --vcpus 4] [--scale 1.0] [--seed 1]

Why: the scenario (RESEARCH 1.1, 11-22) targets work that really needs a VM. SWE practically never does (13,600 commands
of the 264 traces: Docker 0, systemd 0, kernel modules 0, GUI 0, mount 2), while a desktop agent needs a full OS with a
display server and real applications. OSWorld runs its tasks in such a VM (its Docker provider defaults to 4 GB, 4 cores).

What one slot does, in a loop until the deadline: create a qcow2 overlay on the shared base image, boot QEMU (NUMA 0
memory, the VM CPU set), wait for the in-guest OSWorld server, run the task's setup steps (download+upload, launch,
open, execute, activate/close window, Chrome tabs, sleep), then replay the recorded actions open-loop: action i is sent
at the recorded time offset (action_timestamp, 1 s resolution) and followed by a screenshot, as the OSWorld agent loop
does. The screen may differ from the recording; the next action is sent anyway (like the SWE replays). The VM is then
destroyed. The wait before the first action and after the last is the median recorded step gap.

Records (RUN/): vms.jsonl (one line per VM), steps.jsonl (one line per action), proc.csv (per VM every second: QEMU
anonymous and file RSS, vCPU-thread and whole-process run time from schedstat), host.csv (node 0 memory), live.json
(for ../footprint/memscan.py: pid, mem_mib, sampled). Times are time.monotonic_ns().

Server rules: NUMA 0 only (memory bind 0, VM CPUs 4-31,68-95, controller 0-3,64-67); NUMA 1 belongs to another user.
fill holds .codex-runs/lab.lock and quiet.lock exclusively (other sessions run QEMU labs on this host).
"""
import argparse
import collections
import contextlib
import fcntl
import glob
import json
import os
from pathlib import Path
import random
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid

import requests

OSW = Path("/Serverless/agent-sandbox-cxl/osworld")
BASE = OSW / "Ubuntu.qcow2"
REPO = OSW / "OSWorld"
TRAJS = OSW / "trajs"
CACHE = OSW / "cache"
VMDIR = OSW / "vms"
HERE = Path(__file__).resolve().parent
LOCKS = [HERE.parents[1] / ".codex-runs/lab.lock", HERE.parents[1] / ".codex-runs/quiet.lock"]
VM_CPUS = "4-31,68-95"
CTL_CPUS = "0-3,64-67"
SUPPORTED = {"download", "launch", "execute", "command", "open", "activate_window", "close_window", "sleep",
             "chrome_open_tabs"}
PREFIX = (  # desktop_env/controllers/python.py PYAUTOGUI_PKGS_PREFIX (OSWorld main, 11-22)
    "import pyautogui; import time; import platform; "
    "pyautogui.FAILSAFE = False; "
    "_osworld_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\"<>?'; "
    "_osworld_linux_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\">?'; "
    "pyautogui.isShiftCharacter = lambda character: character.isupper() or "
    "character in (_osworld_linux_shift_chars if platform.system() == 'Linux' else _osworld_shift_chars); "
    "{command}"
)
DEFAULT_MODELS = ("claude-3-7-sonnet-20250219-15steps",)
LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ------------------------------------------------------------------ tasks and trajectories
def parse_ts(s):
    """'20250723@084710' -> seconds of day (episodes never cross midnight in practice; checked by the caller)."""
    hh, mm, ss = int(s[9:11]), int(s[11:13]), int(s[13:15])
    day = int(s[:8])
    return day * 86400 + hh * 3600 + mm * 60 + ss


def action_code(action):
    """Python code for one recorded action, or None for DONE/FAIL/WAIT/empty."""
    if isinstance(action, dict):
        code = action.get("command")
        if code is None and action.get("action_type") in ("DONE", "FAIL", "WAIT"):
            return None
    else:
        code = action
    if not isinstance(code, str) or code.strip() in ("", "DONE", "FAIL", "WAIT"):
        return None
    return code


def action_kind(code):
    if code is None:
        return "none"
    for word in ("typewrite", "write", "hotkey", "press", "doubleClick", "tripleClick", "rightClick", "click", "drag",
                 "scroll", "moveTo", "keyDown", "sleep"):
        if f"pyautogui.{word}" in code:
            return word
    return "other"


def load_traj(path):
    steps = [json.loads(line) for line in open(path) if line.strip()]
    out = []
    for s in steps:
        if "action_timestamp" not in s:
            continue
        out.append({"t": parse_ts(s["action_timestamp"]), "code": action_code(s.get("action"))})
    return out


def find_tasks(models):
    rows = []
    for model in models:
        for traj in sorted(glob.glob(str(TRAJS / model / "*" / "*" / "traj.jsonl"))):
            domain, task_id = traj.split("/")[-3], traj.split("/")[-2]
            cfg_path = REPO / "evaluation_examples/examples" / domain / f"{task_id}.json"
            if not cfg_path.exists():
                continue
            cfg = json.load(open(cfg_path))
            types = [c["type"] for c in cfg.get("config", [])]
            if not set(types) <= SUPPORTED or cfg.get("proxy"):
                continue
            steps = load_traj(traj)
            if len(steps) < 2:
                continue
            gaps = [b["t"] - a["t"] for a, b in zip(steps, steps[1:])]
            if min(gaps) < 0 or max(gaps) > 3600:
                continue
            rows.append({"model": model, "domain": domain, "task": task_id, "traj": traj, "steps": len(steps),
                         "actions": sum(s["code"] is not None for s in steps), "span_s": steps[-1]["t"] - steps[0]["t"],
                         "median_gap_s": statistics.median(gaps), "setup_types": types})
    return rows


# ------------------------------------------------------------------ guest server calls
class Guest:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.chrome = port + 1

    def get(self, path, timeout=30):
        return requests.get(self.base + path, timeout=timeout)

    def post(self, path, timeout=60, **kw):
        return requests.post(self.base + path, timeout=timeout, **kw)

    def ready(self, deadline, keep_going=lambda: True):
        while time.time() < deadline and keep_going():
            try:
                if self.get("/platform", timeout=3).ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(1)
        return False

    def execute(self, code, timeout=90):
        payload = {"command": ["python", "-c", PREFIX.format(command=code)], "shell": False}
        return self.post("/execute", json=payload, timeout=timeout)

    def screenshot(self):
        return self.get("/screenshot", timeout=30)


def fetch(url):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{uuid.uuid5(uuid.NAMESPACE_URL, url)}_{os.path.basename(url.split('?')[0]) or 'file'}"
    if not path.exists():
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        os.replace(tmp, path)
    return path


def run_setup(guest, config):
    """OSWorld SetupController steps that do not need playwright, Google Drive or a proxy. Returns error list."""
    errors = []
    for step in config:
        kind, p = step["type"], step.get("parameters", {})
        try:
            if kind == "download":
                for f in p["files"]:
                    local = fetch(f["url"])
                    with open(local, "rb") as fh:
                        r = guest.post("/setup/upload", timeout=600, data={"file_path": f["path"]},
                                       files={"file_data": (os.path.basename(f["path"]), fh)})
                    if not r.ok:
                        errors.append(f"upload {f['path']}: {r.status_code}")
            elif kind == "launch":
                r = guest.post("/setup/launch", json={"command": p["command"], "shell": p.get("shell", False)})
                if not r.ok:
                    errors.append(f"launch: {r.status_code}")
            elif kind in ("execute", "command"):
                r = guest.post("/setup/execute", timeout=300,
                               json={"command": p["command"], "shell": p.get("shell", False)})
                if not r.ok:
                    errors.append(f"execute: {r.status_code}")
            elif kind == "open":
                r = guest.post("/setup/open_file", timeout=1810, json={"path": p["path"]})
                if not r.ok:
                    errors.append(f"open {p['path']}: {r.status_code}")
            elif kind in ("activate_window", "close_window"):
                guest.post(f"/setup/{kind}", json={"window_name": p["window_name"], "strict": p.get("strict", False),
                                                   "by_class": p.get("by_class", False)})
            elif kind == "sleep":
                time.sleep(float(p["seconds"]))
            elif kind == "chrome_open_tabs":
                for url in p["urls_to_open"]:
                    for _ in range(20):  # Chrome may still be starting
                        try:
                            r = requests.put(f"http://127.0.0.1:{guest.chrome}/json/new?{url}", timeout=10)
                            if r.ok:
                                break
                        except requests.RequestException:
                            pass
                        time.sleep(1)
                    else:
                        errors.append(f"chrome tab {url}")
        except Exception as e:  # noqa: BLE001 - one failed step must not end the episode
            errors.append(f"{kind}: {type(e).__name__}: {e}"[:300])
    return errors


# ------------------------------------------------------------------ VM lifecycle
def cpu_set(spec):
    return {c for part in spec.split(",") for c in range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1)}


def free_port(port):
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


class VM:
    def __init__(self, vid, port, mem_mib, vcpus, rundir, balloon=False):
        self.vid, self.port, self.mem_mib, self.vcpus = vid, port, mem_mib, vcpus
        self.balloon = balloon
        self.disk = VMDIR / f"{vid}.qcow2"
        self.log = (rundir or VMDIR) / "qemu" / f"{vid}.log"
        self.proc = None

    def start(self):
        VMDIR.mkdir(parents=True, exist_ok=True)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(BASE), str(self.disk)],
                       check=True)
        p = self.port
        fwd = f"hostfwd=tcp:127.0.0.1:{p}-:5000,hostfwd=tcp:127.0.0.1:{p + 1}-:9222,hostfwd=tcp:127.0.0.1:{p + 2}-:8080"
        # CPU affinity is set in the child just before exec: numactl --physcpubind refuses CPUs outside the
        # caller's own affinity, and fill pins itself to the controller CPUs.
        cmd = ["numactl", "--membind=0",
               "qemu-system-x86_64", "-name", f"{self.vid},debug-threads=on", "-enable-kvm", "-machine", "q35,accel=kvm", "-cpu", "host",
               "-smp", str(self.vcpus), "-m", str(self.mem_mib),
               "-drive", f"file={self.disk},if=virtio,format=qcow2",
               "-netdev", f"user,id=n0,{fwd}", "-device", "virtio-net-pci,netdev=n0",
               "-vga", "none", "-device", "VGA,edid=on,xres=1920,yres=1080",
               "-display", "none", "-serial", "none", "-monitor", "none"]
        if self.balloon:  # free page reporting: the guest hands freed pages back (per VM, no host setting)
            cmd += ["-device", "virtio-balloon-pci,free-page-reporting=on"]
        self.proc = subprocess.Popen(cmd, stdout=open(self.log, "w"), stderr=subprocess.STDOUT,
                                     start_new_session=True, preexec_fn=lambda: os.sched_setaffinity(0, cpu_set(VM_CPUS)))
        return self.proc.pid

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        with contextlib.suppress(FileNotFoundError):
            self.disk.unlink()


def episode(task, vm, scale, on_step, stop_evt=None):
    """Boot is done by the caller. Returns a dict for vms.jsonl."""
    guest = Guest(vm.port)
    rec = {"vm": vm.vid, "task": f"{task['domain']}/{task['task']}", "model": task["model"]}
    cfg = json.load(open(REPO / "evaluation_examples/examples" / task["domain"] / f"{task['task']}.json"))
    t = time.monotonic_ns()
    rec["setup_errors"] = run_setup(guest, cfg.get("config", []))
    rec["setup_s"] = (time.monotonic_ns() - t) / 1e9
    steps = load_traj(task["traj"])
    gaps = [b["t"] - a["t"] for a, b in zip(steps, steps[1:])]
    lead = statistics.median(gaps) if gaps else 10
    t0 = time.monotonic()
    offsets = [lead + (s["t"] - steps[0]["t"]) for s in steps]
    rec["episode_start_ns"] = time.monotonic_ns()
    n_ok = 0
    for i, (s, off) in enumerate(zip(steps, offsets)):
        if stop_evt is not None and stop_evt.is_set():
            rec["cut"] = True
            break
        sched = t0 + off * scale
        delay = sched - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        st = {"vm": vm.vid, "task": rec["task"], "step": i, "kind": action_kind(s["code"]),
              "late_s": round(max(0.0, -delay), 3), "start_ns": time.monotonic_ns()}
        try:
            if s["code"] is not None:
                r = guest.execute(s["code"])
                st["exec_ok"] = r.ok
            st["exec_ns"] = time.monotonic_ns()
            shot = guest.screenshot()
            st["shot_bytes"] = len(shot.content) if shot.ok else 0
            n_ok += bool(shot.ok)
        except requests.RequestException as e:
            st["error"] = f"{type(e).__name__}"[:100]
        st["end_ns"] = time.monotonic_ns()
        on_step(st)
    else:
        time.sleep(lead * scale)
    rec["episode_end_ns"] = time.monotonic_ns()
    rec["steps"] = len(steps)
    rec["screens_ok"] = n_ok
    return rec


# ------------------------------------------------------------------ sampling
def schedstat_ns(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return 0


def proc_sample(pid):
    rss = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith(("RssAnon:", "RssFile:", "RssShmem:")):
                    k, v = line.split(":")
                    rss[k] = int(v.split()[0])
    except (FileNotFoundError, ProcessLookupError):
        return None
    vcpu, total = 0, 0
    for task in glob.glob(f"/proc/{pid}/task/*"):
        ns = schedstat_ns(task + "/schedstat")
        total += ns
        try:
            with open(task + "/comm") as f:
                if "/KVM" in f.read():
                    vcpu += ns
        except (FileNotFoundError, ProcessLookupError):
            pass
    return rss.get("RssAnon", 0), rss.get("RssFile", 0), rss.get("RssShmem", 0), vcpu, total


def node0_meminfo():
    out = {}
    with open("/sys/devices/system/node/node0/meminfo") as f:
        for line in f:
            parts = line.split()
            out[parts[2].rstrip(":")] = int(parts[3])
    return out


def node0_room_kib():
    """Free memory plus inactive page cache on node 0 (the base image cache is reclaimable)."""
    mi = node0_meminfo()
    return mi.get("MemFree", 0) + mi.get("Inactive(file)", 0)


@contextlib.contextmanager
def held(paths):
    files = []
    try:
        for path in paths:
            f = open(path, "a")
            files.append(f)
            log(f"waiting for {path.name}")
            fcntl.flock(f, fcntl.LOCK_EX)
        log("locks held")
        yield
    finally:
        for f in reversed(files):
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


# ------------------------------------------------------------------ commands
def cmd_tasks(args):
    rows = find_tasks(args.models.split(","))
    out = HERE / "tasks.json"
    json.dump(rows, open(out, "w"), indent=1)
    by = collections.Counter(r["domain"] for r in rows)
    print(f"{len(rows)} replayable pairs -> {out}")
    print(dict(by))
    if rows:
        print(f"span s median {statistics.median(r['span_s'] for r in rows):.0f}, median step gap "
              f"{statistics.median(r['median_gap_s'] for r in rows):.0f} s, actions median "
              f"{statistics.median(r['actions'] for r in rows)}")


def cmd_smoke(args):
    tasks = json.load(open(HERE / "tasks.json"))
    task = next(t for t in tasks if not args.task or f"{t['domain']}/{t['task']}" == args.task)
    vm = VM("smoke", 21000, args.mem_mib, args.vcpus, None, args.balloon)
    t = time.time()
    pid = vm.start()
    try:
        ok = Guest(vm.port).ready(time.time() + 300)
        log(f"boot {time.time() - t:.1f} s ready={ok} pid={pid}")
        if not ok:
            return
        print("screen", Guest(vm.port).post("/screen_size").text.strip())
        rec = episode(task, vm, args.scale, lambda st: log(f"step {st['step']} {st['kind']} late {st['late_s']} "
                                                            f"exec {(st.get('exec_ns', st['start_ns']) - st['start_ns']) / 1e6:.0f} ms "
                                                            f"shot {(st['end_ns'] - st.get('exec_ns', st['start_ns'])) / 1e6:.0f} ms"))
        print(json.dumps(rec, indent=1))
        s = proc_sample(pid)
        print(f"QEMU RSS anon {s[0] / 1024:.0f} MiB file {s[1] / 1024:.0f} MiB, vCPU {s[3] / 1e9:.1f} s, "
              f"process {s[4] / 1e9:.1f} s")
    finally:
        vm.stop()


def cmd_fill(args):
    run = Path(args.run)
    run.mkdir(parents=True, exist_ok=True)
    tasks = json.load(open(HERE / "tasks.json"))
    if args.models:
        tasks = [t for t in tasks if t["model"] in args.models.split(",")]
    rng = random.Random(args.seed)
    rng.shuffle(tasks)
    order = collections.deque(tasks)
    json.dump(vars(args) | {"n_tasks": len(tasks), "start_ns": time.monotonic_ns(), "start_wall": time.time(),
                            "vm_cpus": VM_CPUS, "ctl_cpus": CTL_CPUS}, open(run / "run.json", "w"), indent=1)
    os.sched_setaffinity(0, cpu_set(CTL_CPUS))
    stop = threading.Event()
    lock = threading.Lock()
    live = {}
    deadline = time.monotonic() + args.minutes * 60
    files = {name: open(run / name, "a") for name in ("vms.jsonl", "steps.jsonl")}
    counter = [0]

    def write(name, rec):
        with lock:
            files[name].write(json.dumps(rec) + "\n")
            files[name].flush()

    def next_task():
        with lock:
            t = order.popleft()
            order.append(t)
            counter[0] += 1
            return t, f"v{counter[0]:04d}"

    def slot(k):
        time.sleep(k * args.stagger_s)
        port = 22000 + 10 * k
        while not stop.is_set() and time.monotonic() < deadline:
            # memory admission: a new VM starts only while node 0 keeps --min-free-gib free (guards the host, and
            # is the memory ceiling when slots are many)
            waited = 0.0
            while node0_room_kib() < args.min_free_gib * 1024 * 1024 and not stop.is_set() \
                    and time.monotonic() < deadline:
                time.sleep(2)
                waited += 2
            if stop.is_set() or time.monotonic() >= deadline:
                break
            task, vid = next_task()
            vm = VM(vid, port, args.mem_mib, args.vcpus, run, args.balloon)
            rec = {"vm": vid, "slot": k, "start_ns": time.monotonic_ns(), "admission_wait_s": waited}
            try:
                pid = vm.start()
                with lock:
                    live[vid] = {"pid": pid, "mem_mib": args.mem_mib, "sampled": True, "trace": task["task"],
                                 "group": task["domain"], "start_ns": rec["start_ns"]}
                ok = Guest(port).ready(time.time() + 300, lambda: vm.alive() and not stop.is_set())
                rec["boot_s"] = (time.monotonic_ns() - rec["start_ns"]) / 1e9
                if not ok:
                    rec["error"] = "not ready"
                else:
                    rec |= episode(task, vm, args.scale, lambda st: write("steps.jsonl", st), stop)
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            finally:
                with lock:
                    live.pop(vid, None)
                vm.stop()
                rec["end_ns"] = time.monotonic_ns()
                write("vms.jsonl", rec)
                log(f"slot {k} {vid} {rec.get('task')} done ({rec.get('error', 'ok')}, setup errors "
                    f"{len(rec.get('setup_errors', []))})")

    def sampler():
        with open(run / "proc.csv", "a") as pf, open(run / "host.csv", "a") as hf:
            if pf.tell() == 0:
                pf.write("t_ns,vm,rss_anon_kib,rss_file_kib,rss_shmem_kib,vcpu_run_ns,proc_run_ns\n")
                hf.write("t_ns,mem_free_kib,file_pages_kib,anon_pages_kib,live_vms\n")
            while not stop.is_set():
                t = time.monotonic_ns()
                with lock:
                    snap = dict(live)
                for vid, m in snap.items():
                    s = proc_sample(m["pid"])
                    if s:
                        pf.write(f"{t},{vid},{s[0]},{s[1]},{s[2]},{s[3]},{s[4]}\n")
                mi = node0_meminfo()
                hf.write(f"{t},{mi.get('MemFree', 0)},{mi.get('FilePages', 0)},{mi.get('AnonPages', 0)},{len(snap)}\n")
                pf.flush()
                hf.flush()
                tmp = run / "live.json.tmp"
                tmp.write_text(json.dumps({"t_ns": t, "vms": snap}))
                os.replace(tmp, run / "live.json")
                time.sleep(max(0.0, 1.0 - (time.monotonic_ns() - t) / 1e9))

    def on_signal(*_):
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    ctx = held(LOCKS) if not args.no_locks else contextlib.nullcontext()
    with ctx:
        threads = [threading.Thread(target=sampler, daemon=True)]
        threads += [threading.Thread(target=slot, args=(k,)) for k in range(args.slots)]
        for th in threads:
            th.start()
        for th in threads[1:]:
            th.join()
        stop.set()
        threads[0].join(5)
    (run / "done").write_text(str(time.monotonic_ns()))
    log("fill done")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tasks")
    p.add_argument("--models", default=",".join(DEFAULT_MODELS))
    for name in ("smoke", "fill"):
        p = sub.add_parser(name)
        p.add_argument("--mem-mib", type=int, default=4096)
        p.add_argument("--vcpus", type=int, default=4)
        p.add_argument("--scale", type=float, default=1.0, help="multiply recorded step gaps")
        p.add_argument("--balloon", action="store_true", help="virtio-balloon with free page reporting")
        if name == "smoke":
            p.add_argument("--task")
        else:
            p.add_argument("--run", required=True)
            p.add_argument("--slots", type=int, required=True)
            p.add_argument("--minutes", type=float, required=True)
            p.add_argument("--stagger-s", type=float, default=20.0, help="start slot k after k x this")
            p.add_argument("--seed", type=int, default=1)
            p.add_argument("--models", help="comma list; default all in tasks.json")
            p.add_argument("--min-free-gib", type=float, default=12.0,
                           help="node 0 MemFree + Inactive(file) needed to start a VM")
            p.add_argument("--no-locks", action="store_true")
    a = ap.parse_args()
    {"tasks": cmd_tasks, "smoke": cmd_smoke, "fill": cmd_fill}[a.cmd](a)


if __name__ == "__main__":
    main()
