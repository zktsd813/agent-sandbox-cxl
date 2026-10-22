#!/usr/bin/env python3
"""Short-trace sandboxes for the contention experiment.

select: choose short, light traces from the measured Docker replay.
build:  turn each pinned task image into a Firecracker ext4 root disk with the guest replay agent.
solo:   replay each trace alone in a 1-vCPU microVM and record per-command CPU demand.
"""

import argparse
import csv
import datetime
import importlib.util
import json
import os
from pathlib import Path
import queue
import random
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCAL = HERE / ".local"
IMAGES = LOCAL / "images"
RUNS = LOCAL / "runs"
CANDIDATES = HERE / "candidates.json"
TRACES = REPO / "harness/trace-replay/data/native"
DOCKER_RESULTS = REPO / "docs/results/numa-immediate-20261014"
KERNEL = REPO / ".local/microvm/vmlinux-6.1.188"
# Pinned copy (sha256 9aa1b69b...) of the Firecracker build used since the first solo run; the source tree's build
# output belongs to another agent and may be rebuilt at any time.
FIRECRACKER = LOCAL / "bin/firecracker"
BUSYBOX = Path("/usr/bin/busybox")
GIB = 1024 ** 3
USER_HZ = os.sysconf("SC_CLK_TCK")
# The root disk only carries this stub, so changing guest-init.sh (copied to each job disk) needs no image rebuild.
INIT_STUB = """#!/microvm/busybox sh
/microvm/busybox mount -t devtmpfs devtmpfs /dev
/microvm/busybox mkdir -p /job
/microvm/busybox mount -t ext4 /dev/vdb /job || { echo SWE_JOB_MOUNT_FAILED; /microvm/busybox poweroff -f; }
exec /microvm/busybox sh /job/init
"""
# NAT egress for sandboxes: one host bridge, one TAP per running microVM.
BRIDGE, SUBNET, GATEWAY = "ctbr0", "10.200.0.0/16", "10.200.0.1"
RULE_TAG = ["-m", "comment", "--comment", "contention-sandbox"]
RULES = [("nat", "POSTROUTING", ["-s", SUBNET, "!", "-o", BRIDGE, "-j", "MASQUERADE"]),
         ("filter", "DOCKER-USER", ["-i", BRIDGE, "-j", "ACCEPT"]),
         ("filter", "DOCKER-USER", ["-o", BRIDGE, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]),
         ("filter", "INPUT", ["-i", BRIDGE, "-j", "DROP"])]  # sandboxes reach the Internet, not host services

spec = importlib.util.spec_from_file_location("replay", REPO / "harness/trace-replay/experiment.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def output(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs).stdout


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def candidates(names=None):
    entries = json.loads(CANDIDATES.read_text())["traces"]
    if names:
        unknown = set(names) - {e["name"] for e in entries}
        if unknown:
            raise SystemExit(f"Not in {CANDIDATES.name}: {sorted(unknown)}")
        entries = [e for e in entries if e["name"] in names]
    return entries


def guest_memory_mib(peak_gib):
    """Guest RAM for a trace: 1.5x its Docker peak (page cache included), at least 2 GiB, in 512 MiB steps."""
    if peak_gib is None:
        return 4096
    return int(min(24576, max(2048, -(-peak_gib * 1.5 * 1024 // 512) * 512)))


def select(args):
    paired = {row["trace"]: row for row in csv.DictReader((DOCKER_RESULTS / "paired-traces.csv").open())}
    lock = json.loads((DOCKER_RESULTS / "image-lock.json").read_text())["images"]
    chosen = []
    for path in sorted(TRACES.glob("*.traj.json")):
        trace = replay.load_trace(str(path))
        row = paired.get(trace["name"]) or {}
        window = trace["last"] - trace["first"]
        peak = float(row["peak_memory_gib_node0"]) if row.get("peak_memory_gib_node0") else None
        cpu = float(row["cpu_time_s_node0"]) if row.get("cpu_time_s_node0") else None
        duration = float(row["duration_s_node0"]) if row.get("duration_s_node0") else None
        if not args.all and (row.get("eligible") != "True" or peak is None or window > args.max_window_s
                             or peak >= args.max_peak_gib or cpu <= args.min_cpu_s):
            continue
        image = trace["environment"]["image"]
        # The lock keys one image (gin-3741) by its pinned digest instead of the trace tag.
        pinned = lock.get(image) or next(v for k, v in lock.items() if k.startswith(image.rsplit(":", 1)[0] + "@"))
        chosen.append({"name": trace["name"], "trace": str(path.relative_to(REPO)), "trace_sha256": trace["sha256"],
                       "image": image, "image_id": pinned["image_id"], "repo_digest": pinned["repo_digests"][0],
                       "window_s": round(window, 3), "commands": trace["command_count"],
                       "recorded_gap_s": round(sum(b["gap"] for b in trace["batches"]), 3),
                       "docker_eligible": row.get("eligible") == "True",
                       "docker_peak_memory_gib": round(peak, 4) if peak is not None else None,
                       "docker_cpu_time_s": round(cpu, 3) if cpu is not None else None,
                       "docker_duration_s": round(duration, 3) if duration is not None else None,
                       "memory_mib": guest_memory_mib(peak)})
    save_json(CANDIDATES, {"criteria": {"eligible_in": str(DOCKER_RESULTS.relative_to(REPO)),
                                        "all_traces": args.all,
                                        "max_window_s": args.max_window_s, "max_peak_gib": args.max_peak_gib,
                                        "min_cpu_s": args.min_cpu_s,
                                        "window": "first assistant timestamp to last tool timestamp",
                                        "memory_mib": "1.5 x Docker peak, >= 2 GiB, 512 MiB steps, <= 24 GiB"},
                           "traces": chosen})
    print(f"Selected {len(chosen)} traces -> {CANDIDATES}")


def install_stub(disk):
    with tempfile.NamedTemporaryFile("w", dir=disk.parent, delete=False) as stream:
        stream.write(INIT_STUB)
    try:
        requests = ["rm /microvm/init", f"write {stream.name} /microvm/init", "sif /microvm/init uid 0",
                    "sif /microvm/init gid 0", "sif /microvm/init mode 0100755"]
        run(["debugfs", "-w", "-f", "-", str(disk)], input="\n".join(requests) + "\n", text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        installed = subprocess.run(["debugfs", "-R", "cat /microvm/init", str(disk)], capture_output=True, text=True).stdout
        if installed != INIT_STUB:
            raise RuntimeError(f"{disk}: init stub was not installed")
    finally:
        os.unlink(stream.name)


def build_image(entry, keep_image):
    out = IMAGES / entry["name"]
    if (out / "image.json").exists():
        image = json.loads((out / "image.json").read_text())
        if image.get("init_stub") == INIT_STUB:
            return f"{entry['name']}: exists"
        install_stub(out / "rootfs.ext4")
        image.pop("guest_init", None)
        image["init_stub"] = INIT_STUB
        save_json(out / "image.json", image)
        return f"{entry['name']}: init stub updated"
    work = IMAGES / (entry["name"] + ".partial")
    if work.exists():
        run(["chmod", "-R", "u+w", work])
        shutil.rmtree(work)
    work.mkdir(parents=True)
    reference = entry["repo_digest"]
    run(["docker", "pull", "-q", reference], stdout=subprocess.DEVNULL)
    inspect = json.loads(output(["docker", "image", "inspect", reference]))[0]
    if inspect["Id"] != entry["image_id"]:
        raise RuntimeError(f"{entry['name']}: image id {inspect['Id']} != pinned {entry['image_id']}")
    container = output(["docker", "create", reference]).strip()
    try:
        run(["docker", "export", "-o", str(work / "rootfs.tar"), container])
    finally:
        run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL)
    tar_bytes = (work / "rootfs.tar").stat().st_size
    disk_bytes = -(-int(tar_bytes * 1.25 + 2 * GIB) // GIB) * GIB
    # fakeroot keeps the image's root ownership for mkfs without creating root-owned files on the host.
    (work / "init").write_text(INIT_STUB)
    script = "\n".join([
        "set -e", "mkdir root", "tar --numeric-owner -xpf rootfs.tar -C root",
        "mkdir -p root/microvm", f"cp {shlex.quote(str(BUSYBOX))} root/microvm/busybox",
        "mv init root/microvm/init",
        "chown 0:0 root/microvm root/microvm/busybox root/microvm/init",
        "chmod 755 root/microvm/busybox root/microvm/init",
        f"truncate -s {disk_bytes} rootfs.ext4", "mkfs.ext4 -q -F -L swe-root -d root rootfs.ext4"])
    run(["fakeroot", "sh", "-c", script], cwd=work)
    run(["e2fsck", "-fn", str(work / "rootfs.ext4")], stdout=subprocess.DEVNULL)
    run(["chmod", "-R", "u+w", work / "root"])
    shutil.rmtree(work / "root")
    (work / "rootfs.tar").unlink()
    config = inspect["Config"]
    save_json(work / "image.json", {"name": entry["name"], "repo_digest": reference, "image_id": inspect["Id"],
                                    "env": config.get("Env") or [], "working_dir": config.get("WorkingDir"),
                                    "export_tar_bytes": tar_bytes, "disk_bytes": disk_bytes,
                                    "disk_allocated_bytes": (work / "rootfs.ext4").stat().st_blocks * 512,
                                    "init_stub": INIT_STUB,
                                    "busybox": str(BUSYBOX), "built_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    work.rename(out)
    if not keep_image:
        run(["docker", "image", "rm", reference], stdout=subprocess.DEVNULL)
    return f"{entry['name']}: {tar_bytes / GIB:.2f} GiB export, {disk_bytes // GIB} GiB disk"


def build(args):
    IMAGES.mkdir(parents=True, exist_ok=True)
    entries = candidates(args.names)
    failures = []
    with ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(build_image, e, args.keep_image): e["name"] for e in entries}
        for future in as_completed(futures):
            try:
                print(future.result(), flush=True)
            except Exception as error:
                failures.append(futures[future])
                print(f"{futures[future]}: FAILED {error}", flush=True)
    if failures:
        raise SystemExit(f"{len(failures)} image builds failed: {sorted(failures)}")


def sudo(*command, check=True):
    return subprocess.run(["sudo", "-n", *command], check=check, capture_output=True, text=True)


def host_dns():
    lines = Path("/run/systemd/resolve/resolv.conf").read_text().splitlines()
    servers = [line.split()[1] for line in lines if line.startswith("nameserver ")]
    if not servers:
        raise RuntimeError("No upstream DNS server in /run/systemd/resolve/resolv.conf")
    return servers


def network_up():
    if sudo("ip", "link", "show", BRIDGE, check=False).returncode:
        sudo("ip", "link", "add", BRIDGE, "type", "bridge")
        sudo("ip", "addr", "add", f"{GATEWAY}/{SUBNET.split('/')[1]}", "dev", BRIDGE)
        sudo("ip", "link", "set", BRIDGE, "up")
    if sudo("sysctl", "-n", "net.ipv4.ip_forward").stdout.strip() != "1":
        raise RuntimeError("net.ipv4.ip_forward is 0; not changing host routing")
    for table, chain, rule in RULES:
        if sudo("iptables", "-t", table, "-C", chain, *rule, *RULE_TAG, check=False).returncode:
            sudo("iptables", "-t", table, "-I", chain, *rule, *RULE_TAG)


def network_down(_=None):
    for table, chain, rule in RULES:
        while not sudo("iptables", "-t", table, "-C", chain, *rule, *RULE_TAG, check=False).returncode:
            sudo("iptables", "-t", table, "-D", chain, *rule, *RULE_TAG)
    for line in sudo("ip", "-o", "link", "show", "master", BRIDGE, check=False).stdout.splitlines():
        sudo("ip", "link", "del", line.split(":")[1].strip().split("@")[0], check=False)
    sudo("ip", "link", "del", BRIDGE, check=False)
    print(f"Removed {BRIDGE}, its TAPs and the tagged iptables rules")


def tap_up(name):
    sudo("ip", "link", "del", name, check=False)
    sudo("ip", "tuntap", "add", "dev", name, "mode", "tap", "user", str(os.getuid()))
    sudo("ip", "link", "set", name, "master", BRIDGE, "up")
    return name


def make_job(entry, image, directory, scale, net=None):
    trace = replay.load_trace(str(REPO / entry["trace"]))
    environment = trace["environment"]
    env = dict(item.split("=", 1) for item in image["env"])
    env.update({"HOME": "/root", "HOSTNAME": "swe-microvm"})
    env.update(environment.get("env", {}))
    job = directory / "job"
    (job / "cmd").mkdir(parents=True)
    shutil.copy2(HERE / "guest-init.sh", job / "init")
    if net:
        (job / "net").write_text(" ".join(net) + "\n")
    commands = []
    for b, batch in enumerate(trace["batches"]):
        for a, action in enumerate(batch["actions"]):
            index = len(commands)
            gap = batch["gap"] if a == 0 else 0.0
            (job / "cmd" / f"{index:03d}.sh").write_text(action["command"])
            (job / "cmd" / f"{index:03d}.gap").write_text(f"{gap:.3f}\n")
            commands.append({"index": index, "batch": b, "action": a, "gap_s": round(gap, 3),
                             "expected_returncode": batch["observations"][a]["returncode"] if batch["observations"] else None})
    argv = ["/microvm/busybox", "env", "-i", *[f"{k}={v}" for k, v in env.items()], *environment["interpreter"]]
    (job / "run").write_text("#!/microvm/busybox sh\nexec " + " ".join(map(shlex.quote, argv))
                             + ' "$(/microvm/busybox cat "$1")"\n')
    (job / "run").chmod(0o755)
    (job / "meta").write_text(f"{len(commands)} {environment.get('timeout', 60)} {scale}\n")
    (job / "cwd").write_text(environment["cwd"])
    disk = directory / "job.ext4"
    run(["truncate", "-s", "1G", disk])
    run(["mkfs.ext4", "-q", "-F", "-L", "swe-job", "-d", job, disk])
    return trace, commands, disk


def thread_stats(pid):
    rows = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            comm = (task / "comm").read_text().strip()
            run_ns, wait_ns, _ = (task / "schedstat").read_text().split()
            rows.append((int(task.name), comm, int(run_ns), int(wait_ns)))
        except (FileNotFoundError, ProcessLookupError, ValueError):
            pass
    return rows


def boot_and_replay(entry, image, directory, cores, siblings, node, memory_mib, vcpus, scale, sample_ms, tap=None):
    core = min(cores)
    net = None
    if tap:
        net = (f"10.200.1.{core}/{SUBNET.split('/')[1]}", GATEWAY, ",".join(host_dns()))
    trace, commands, job_disk = make_job(entry, image, directory, scale, net)
    root_disk = directory / "rootfs.ext4"
    run(["cp", "--sparse=always", IMAGES / entry["name"] / "rootfs.ext4", root_disk])
    config = {"boot-source": {"kernel_image_path": str(KERNEL),
                              "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/microvm/init"},
              "drives": [{"drive_id": "rootfs", "path_on_host": str(root_disk), "is_root_device": True, "is_read_only": False},
                         {"drive_id": "job", "path_on_host": str(job_disk), "is_root_device": False, "is_read_only": False}],
              "machine-config": {"vcpu_count": vcpus, "mem_size_mib": memory_mib}}
    if tap:
        config["network-interfaces"] = [{"iface_id": "eth0", "host_dev_name": tap,
                                         "guest_mac": f"06:00:0a:c8:01:{core:02x}"}]
    save_json(directory / "config.json", config)
    limit = len(commands) * int(trace["environment"].get("timeout", 60)) + sum(c["gap_s"] for c in commands) * scale + 180
    serial = (directory / "serial.log").open("w")
    samples = (directory / "threads.csv").open("w", newline="")
    writer = csv.writer(samples)
    writer.writerow(["t_ns", "tid", "comm", "run_ns", "wait_ns"])
    process = subprocess.Popen(["numactl", f"--membind={node}", "taskset", "-c", ",".join(map(str, sorted(cores | siblings))),
                                str(FIRECRACKER),
                                "--no-api", "--config-file", str(directory / "config.json")],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               errors="replace")
    started = time.monotonic_ns()
    complete = threading.Event()
    markers = []

    def read_serial():
        for line in process.stdout:
            now = time.monotonic_ns()
            serial.write(f"{now}\t{line}")
            if line.startswith("SWE_"):
                markers.append((now, line.strip()))
                if line.startswith("SWE_COMPLETE"):
                    complete.set()
        serial.flush()

    reader = threading.Thread(target=read_serial, daemon=True)
    reader.start()
    pinned, halted = set(), False
    try:
        while process.poll() is None:
            now = time.monotonic_ns()
            if (now - started) / 1e9 > limit:
                raise TimeoutError(f"VM exceeded {limit:.0f}s")
            for tid, comm, run_ns, wait_ns in thread_stats(process.pid):
                writer.writerow([now, tid, comm, run_ns, wait_ns])
                if tid not in pinned:
                    try:
                        os.sched_setaffinity(tid, cores if comm.startswith("fc_vcpu") else siblings)
                        pinned.add(tid)
                    except OSError:
                        pass
            if complete.is_set():
                time.sleep(.5)
                halted = True
                break
            time.sleep(sample_ms / 1000)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=10)
        samples.close()
        serial.close()
    out = directory / "out"
    run(["debugfs", "-R", f"rdump /out {directory}", str(job_disk)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return trace, commands, markers, halted, process.returncode, out


def busy_jiffies(path):
    fields = [int(x) for x in path.read_text().split()[1:]]
    return sum(fields) - fields[3] - fields[4]  # exclude idle and iowait


def analyze(entry, trace, commands, markers, out, directory, scale):
    if not (out / "complete").exists():
        raise RuntimeError("guest did not complete")
    starts = {int(m.split()[1]): t for t, m in markers if m.startswith("SWE_CMD_START")}
    ends = {int(m.split()[1]): t for t, m in markers if m.startswith("SWE_CMD_END")}
    vcpu = {}
    with (directory / "threads.csv").open() as stream:
        for row in csv.DictReader(stream):
            if row["comm"].startswith("fc_vcpu"):  # sum of all vCPU threads at each sample
                t = int(row["t_ns"])
                vcpu[t] = vcpu.get(t, 0) + int(row["run_ns"])
    times = sorted(vcpu)

    def vcpu_run_between(t0, t1):
        before = [t for t in times if t <= t0]
        after = [t for t in times if t >= t1]
        if not before or not after:
            return None
        return (vcpu[after[0]] - vcpu[before[-1]]) / 1e9

    rows = []
    for c in commands:
        n = f"{c['index']:03d}"
        rc, t0, t1 = (out / f"{n}.rc").read_text().split()
        guest_cpu = (busy_jiffies(out / f"{n}.stat1") - busy_jiffies(out / f"{n}.stat0")) / USER_HZ
        host0, host1 = starts.get(c["index"]), ends.get(c["index"])
        rows.append({**c, "returncode": int(rc), "matches": None if c["expected_returncode"] is None
                     else int(rc) == c["expected_returncode"], "timed_out": int(rc) == 137,
                     "guest_duration_s": round(float(t1) - float(t0), 3), "guest_cpu_s": round(guest_cpu, 3),
                     "host_start_ns": host0, "host_end_ns": host1,
                     "vcpu_run_s": None if host0 is None or host1 is None else vcpu_run_between(host0, host1)})
    with (directory / "commands.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    last = (out / f"{len(rows) - 1:03d}.out").read_text(errors="replace").splitlines(keepends=True)
    marker = [i for i, line in enumerate(last) if line.strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
    submission = None
    if len(marker) == 1:
        actual = replay.submission_fingerprint("".join(last[:marker[0]] + last[marker[0] + 1:]))
        expected = trace["source_submission_fingerprint"]
        submission = all(actual[k] == expected[k] for k in ("without_startup_warning_sha256", "startup_warning_count"))
    replay_s = sum(r["guest_duration_s"] for r in rows)
    cpu_s = sum(r["guest_cpu_s"] for r in rows)
    vcpu_s = sum(r["vcpu_run_s"] or 0 for r in rows)
    gap_s = sum(r["gap_s"] for r in rows)
    checked = [r for r in rows if r["matches"] is not None]
    return {"name": entry["name"], "commands": len(rows), "returncode_checked": len(checked),
            "returncode_mismatches": sum(not r["matches"] for r in checked),
            "timeouts": sum(r["timed_out"] for r in rows), "submission_matches": submission,
            "replay_s": round(replay_s, 3), "guest_cpu_s": round(cpu_s, 3), "vcpu_run_s": round(vcpu_s, 3),
            "recorded_gap_s": round(gap_s, 3), "gap_scale": scale,
            "mean_demand_cores_with_recorded_gaps": round(vcpu_s / (replay_s + gap_s), 4) if replay_s + gap_s else None,
            "docker_cpu_time_s": entry["docker_cpu_time_s"], "docker_duration_s": entry["docker_duration_s"]}


def solo_one(entry, groups, root, args):
    directory = root / entry["name"]
    directory.mkdir()
    group = groups.get()
    tap = None
    try:
        image = json.loads((IMAGES / entry["name"] / "image.json").read_text())
        if image.get("init_stub") != INIT_STUB:
            raise RuntimeError("image has an old init; run build again to install the stub")
        core = min(group)
        tap = tap_up(f"ctt{core}") if args.network else None
        memory = args.memory_mib or entry.get("memory_mib", 2048)
        trace, commands, markers, halted, vmm_rc, out = boot_and_replay(
            entry, image, directory, set(group), {c + args.sibling_offset for c in group}, args.node, memory, args.vcpus,
            args.gap_scale, args.sample_ms, tap)
        result = analyze(entry, trace, commands, markers, out, directory, args.gap_scale)
        result.update({"cores": list(group), "vcpus": args.vcpus, "memory_mib": memory, "network": bool(tap),
                       "halted_after_complete": halted, "vmm_returncode": vmm_rc})
        save_json(directory / "summary.json", result)
        return result
    finally:
        if tap:
            sudo("ip", "link", "del", tap, check=False)
        if not args.keep_disk:
            (directory / "rootfs.ext4").unlink(missing_ok=True)
        groups.put(group)


def solo(args):
    entries = [e for e in candidates(args.names) if (IMAGES / e["name"] / "image.json").exists()]
    RUNS.mkdir(parents=True, exist_ok=True)
    if args.resume:
        # Continue an interrupted solo run: keep finished traces, redo unfinished ones.
        root = args.resume.resolve()
        previous = json.loads((root / "run.json").read_text())
        if previous["vcpus"] != args.vcpus:
            raise SystemExit(f"{root} was run with {previous['vcpus']} vCPUs")
        for entry in entries:
            directory = root / entry["name"]
            if directory.exists() and not (directory / "summary.json").exists():
                shutil.rmtree(directory)
        entries = [e for e in entries if not (root / e["name"] / "summary.json").exists()]
    else:
        root = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-solo-"),
                                     dir=RUNS))
    if args.harness_cpus:
        os.sched_setaffinity(0, replay.cpu_ids(args.harness_cpus))  # harness, sampler and disk copies
    core_list = sorted(replay.cpu_ids(args.cores))
    group_list = [tuple(core_list[i:i + args.vcpus]) for i in range(0, len(core_list) - args.vcpus + 1, args.vcpus)]
    if args.network:
        network_up()
    save_json(root / (f"resume-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.json" if args.resume
                      else "run.json"), {"kind": "solo", "traces": [e["name"] for e in entries], "cores": core_list,
                                  "core_groups": group_list, "candidates": str(CANDIDATES),
                                  "sibling_offset": args.sibling_offset, "node": args.node,
                                  "memory_mib": args.memory_mib or "per trace (candidates memory_mib, else 2048)",
                                  "vcpus": args.vcpus, "gap_scale": args.gap_scale, "sample_ms": args.sample_ms,
                                  "kernel": str(KERNEL), "firecracker": str(FIRECRACKER),
                                  "firecracker_sha256": output(["sha256sum", str(FIRECRACKER)]).split()[0],
                                  "host_kernel": os.uname().release,
                                  "network": {"bridge": BRIDGE, "subnet": SUBNET, "egress": "host NAT",
                                              "dns": host_dns()} if args.network else "loopback only"})
    groups = queue.Queue()
    for group in group_list:
        groups.put(group)
    results, failures = [], {}
    with ThreadPoolExecutor(len(group_list)) as pool:
        futures = {pool.submit(solo_one, e, groups, root, args): e["name"] for e in entries}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"{name}: {result['commands']} cmds, mismatches {result['returncode_mismatches']}/"
                      f"{result['returncode_checked']}, timeouts {result['timeouts']}, replay {result['replay_s']}s, "
                      f"vCPU {result['vcpu_run_s']}s, submission {result['submission_matches']}", flush=True)
            except Exception as error:
                failures[name] = str(error)
                print(f"{name}: FAILED {error}", flush=True)
    results = sorted((json.loads(p.read_text()) for p in root.glob("*/summary.json")), key=lambda r: r["name"])
    if results:
        with (root / "solo-summary.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=results[0])
            writer.writeheader()
            writer.writerows(results)
    save_json(root / "failures.json", failures)
    print(f"{len(results)} completed, {len(failures)} failed -> {root}")


# Multi-node runs share each image's root disk read-only. An initramfs overlays a per-sandbox
# writable disk (/dev/vdc) on the base (/dev/vda), then hands over to the job disk's agent.
OVERLAY_INIT = """#!/bin/busybox sh
B=/bin/busybox
$B mount -t devtmpfs devtmpfs /dev
$B mkdir -p /lower /upper /newroot
$B mount -t ext4 -o ro /dev/vda /lower && $B mount -t ext4 /dev/vdc /upper || { echo SWE_OVERLAY_FAILED; $B poweroff -f; }
$B mkdir -p /upper/data /upper/work
$B mount -t overlay overlay -o lowerdir=/lower,upperdir=/upper/data,workdir=/upper/work /newroot \\
  || { echo SWE_OVERLAY_FAILED; $B poweroff -f; }
$B mkdir -p /newroot/job
$B mount -t ext4 /dev/vdb /newroot/job || { echo SWE_JOB_MOUNT_FAILED; $B poweroff -f; }
$B mount --move /dev /newroot/dev
exec $B switch_root /newroot /microvm/busybox sh /job/init
"""


def overlay_initrd():
    path = LOCAL / "overlay-initrd.cpio"
    stamp = LOCAL / "overlay-initrd.init"
    if path.exists() and stamp.exists() and stamp.read_text() == OVERLAY_INIT:
        return path
    with tempfile.TemporaryDirectory(dir=LOCAL) as temp:
        root = Path(temp)
        for directory in ("bin", "dev", "proc", "sys"):
            (root / directory).mkdir()
        shutil.copy2(BUSYBOX, root / "bin/busybox")
        (root / "init").write_text(OVERLAY_INIT)
        (root / "init").chmod(0o755)
        names = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        with path.open("wb") as stream:
            run(["cpio", "--quiet", "-o", "--format=newc", "--owner=0:0"], cwd=root,
                input=("\n".join(["."] + names) + "\n").encode(), stdout=stream)
    stamp.write_text(OVERLAY_INIT)
    return path


class Sandbox:
    """One trace replay in a microVM pinned to a node: vCPUs on the node's cores, VMM threads on their siblings."""

    def __init__(self, entry, directory, node, slot, args):
        self.entry, self.directory, self.node, self.slot, self.args = entry, directory, node, slot, args
        self.image = json.loads((IMAGES / entry["name"] / "image.json").read_text())
        self.markers, self.complete, self.vcpus = [], threading.Event(), {}
        self.process = self.reader = self.tap = None

    def start(self):
        args, node = self.args, self.node
        self.directory.mkdir(parents=True)
        net = None
        if args.network:
            self.tap = tap_up(f"ctt{node['index']}x{self.slot}")
            net = (f"10.200.{2 + node['index']}.{self.slot + 2}/{SUBNET.split('/')[1]}", GATEWAY, ",".join(host_dns()))
        self.trace, self.commands, job_disk = make_job(self.entry, self.image, self.directory, args.gap_scale, net)
        upper = self.directory / "upper.ext4"
        run(["truncate", "-s", f"{args.upper_gib}G", upper])
        run(["mkfs.ext4", "-q", "-F", "-L", "swe-upper", upper])
        config = {"boot-source": {"kernel_image_path": str(KERNEL), "initrd_path": str(overlay_initrd()),
                                  "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"},
                  "drives": [{"drive_id": "base", "path_on_host": str(IMAGES / self.entry["name"] / "rootfs.ext4"),
                              "is_root_device": False, "is_read_only": True},
                             {"drive_id": "job", "path_on_host": str(job_disk), "is_root_device": False, "is_read_only": False},
                             {"drive_id": "upper", "path_on_host": str(upper), "is_root_device": False, "is_read_only": False}],
                  "machine-config": {"vcpu_count": args.vcpus,
                                     "mem_size_mib": args.memory_mib or self.entry.get("memory_mib", 2048)}}
        if self.tap:
            config["network-interfaces"] = [{"iface_id": "eth0", "host_dev_name": self.tap,
                                             "guest_mac": f"06:00:0a:c8:{2 + node['index']:02x}:{self.slot + 2:02x}"}]
        save_json(self.directory / "config.json", config)
        self.serial = (self.directory / "serial.log").open("w")
        self.process = subprocess.Popen(
            ["numactl", f"--membind={node['memory_node']}", "taskset", "-c", node["vmm_cpus"], str(FIRECRACKER),
             "--no-api", "--config-file", str(self.directory / "config.json")],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        self.started_ns = time.monotonic_ns()
        self.reader = threading.Thread(target=self.read_serial, daemon=True)
        self.reader.start()

    def read_serial(self):
        for line in self.process.stdout:
            now = time.monotonic_ns()
            self.serial.write(f"{now}\t{line}")
            if line.startswith("SWE_"):
                self.markers.append((now, line.strip()))
                if line.startswith(("SWE_COMPLETE", "SWE_OVERLAY_FAILED", "SWE_JOB_MOUNT_FAILED", "SWE_NET_FAILED",
                                    "SWE_CWD_FAILED")):
                    self.complete.set()
        self.complete.set()

    def pin_vcpus(self):
        """Find new vCPU threads, move them onto the node cores, and return {tid: schedstat path}."""
        if len(self.vcpus) < self.args.vcpus and self.process.poll() is None:
            for task in Path(f"/proc/{self.process.pid}/task").iterdir():
                tid = int(task.name)
                try:
                    if tid not in self.vcpus and (task / "comm").read_text().startswith("fc_vcpu"):
                        os.sched_setaffinity(tid, self.node["vcpu_cpus"])
                        self.vcpus[tid] = str(task / "schedstat")
                except (FileNotFoundError, ProcessLookupError, OSError):
                    pass
        return self.vcpus

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.reader:
            self.reader.join(timeout=10)
            self.serial.close()
        if self.tap:
            sudo("ip", "link", "del", self.tap, check=False)
        (self.directory / "upper.ext4").unlink(missing_ok=True)

    def result(self):
        """Command timing and return codes from the serial markers, which survive a VM stopped at the deadline."""
        starts, ends = {}, {}
        for t, marker in self.markers:
            fields = marker.split()
            if fields[0] == "SWE_CMD_START":
                starts[int(fields[1])] = t
            elif fields[0] == "SWE_CMD_END":
                ends[int(fields[1])] = (t, int(fields[2]))
        ready = next((t for t, m in self.markers if m.startswith("SWE_READY")), None)
        rows = []
        for c in self.commands:
            if c["index"] not in starts:
                break
            end, rc = ends.get(c["index"], (None, None))
            rows.append({"index": c["index"], "rc": rc, "expected": c["expected_returncode"], "gap_s": c["gap_s"],
                         "host_start_ns": starts[c["index"]], "host_end_ns": end})
        (self.directory / "job.ext4").unlink(missing_ok=True)
        done = [r for r in rows if r["rc"] is not None]
        return {"trace": self.entry["name"], "node": self.node["index"], "slot": self.slot,
                "start_ns": self.started_ns, "ready_ns": ready,
                "completed": any(m.startswith("SWE_COMPLETE") for _, m in self.markers),
                "commands_total": len(self.commands), "commands_done": len(done),
                "mismatches": sum(r["expected"] is not None and r["rc"] != r["expected"] for r in done),
                "timeouts": sum(r["rc"] == 137 for r in done), "commands": rows}


def parse_nodes(spec, sibling_offset, pool="none"):
    """Placement nodes. `pool` lets vCPUs run on every core of nodes in the same socket or on all node cores,
    while memory and VMM threads stay on the home node: an idealized instant cross-node move by the host scheduler."""
    nodes = []
    for index, part in enumerate(spec.split("/")):
        cores = replay.cpu_ids(part)
        siblings = {c + sibling_offset for c in cores}
        socket_nodes = {int(Path(f"/sys/devices/system/cpu/cpu{c}/topology/physical_package_id").read_text()) for c in cores}
        if len(socket_nodes) != 1:
            raise SystemExit(f"Node {part} spans sockets")
        nodes.append({"index": index, "cores": cores, "siblings": siblings, "memory_node": socket_nodes.pop(),
                      "vmm_cpus": ",".join(map(str, sorted(siblings)))})
    for node in nodes:
        members = [n for n in nodes if pool == "all" or pool == "socket" and n["memory_node"] == node["memory_node"]
                   or n is node]
        node["pool"] = min(n["index"] for n in members)
        node["vcpu_cpus"] = set().union(*(n["cores"] for n in members))
    return nodes


def cpu_sampler(args):
    """Per-node sums of /proc/schedstat run and runqueue-wait time (all tasks on the node's CPUs), every interval."""
    groups = [replay.cpu_ids(part) for part in args.groups.split("/")]
    owner = {cpu: g for g, cpus in enumerate(groups) for cpu in cpus}
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def read():
        sums = [[0, 0] for _ in groups]
        with open("/proc/schedstat") as stream:
            for line in stream:
                if line.startswith("cpu"):
                    fields = line.split()
                    group = owner.get(int(fields[0][3:]))
                    if group is not None:
                        sums[group][0] += int(fields[7])
                        sums[group][1] += int(fields[8])
        return sums

    interval = args.interval_ms / 1e3
    with args.output.open("w", buffering=1 << 20) as stream:
        stream.write("t_ns,dt_ns," + ",".join(f"run{g},wait{g}" for g in range(len(groups))) + "\n")
        last_t, last = time.monotonic_ns(), read()
        target = time.monotonic()
        while not stop.is_set():
            target += interval
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                target = time.monotonic()
            now, sums = time.monotonic_ns(), read()
            stream.write(f"{now},{now - last_t}," + ",".join(f"{s[0] - p[0]},{s[1] - p[1]}" for s, p in zip(sums, last)) + "\n")
            last_t, last = now, sums


MOVES = ("none", "instant", "hand-off", "stop-and-copy", "pre-copy")


def move_controller(sandboxes, lock, nodes, stop, directory, args):
    """Emulated cross-node moves on the real system, with the policy of fleet.py/sim.py.

    Overload is counted from vCPU thread states (R = running or runnable), which has no reporting lag.
    A move pauses the whole Firecracker process (SIGSTOP) for its downtime, burns the source CPU it would spend
    (a busy process on the source node's cores), and finally rebinds the sandbox's vCPU and VMM threads to the
    destination node. Guest memory stays where it is; both nodes must share a memory node for a fair comparison.
    """
    detect = max(1, round(args.move_detect_ms / 10))
    history = {n["index"]: [0.0] * detect for n in nodes}
    cooldown = {n["index"]: 0.0 for n in nodes}
    previous, recent, locked, busy = {}, {}, {}, set()
    log = (directory / "moves.jsonl").open("w")
    cores = {n["index"]: len(n["cores"]) for n in nodes}

    def burn(node, seconds):
        if seconds > 0:
            subprocess.Popen(["taskset", "-c", ",".join(map(str, sorted(node["cores"]))), "timeout", "-s", "KILL",
                              f"{seconds:.3f}", "sh", "-c", "while :; do :; done"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rebind(box, node):
        try:
            tasks = list(Path(f"/proc/{box.process.pid}/task").iterdir())
        except (FileNotFoundError, ProcessLookupError):
            return  # the sandbox finished while it was moving
        for task in tasks:
            try:
                comm = (task / "comm").read_text()
                os.sched_setaffinity(int(task.name), node["vcpu_cpus"] if comm.startswith("fc_vcpu") else node["siblings"])
            except (FileNotFoundError, ProcessLookupError, OSError):
                pass

    def move(vm, box, source, destination, paused_s, dump_s, delay_s):
        started = time.monotonic_ns()
        try:
            if paused_s > 0:
                os.kill(box.process.pid, signal.SIGSTOP)
            burn(source, dump_s)
            time.sleep(delay_s)
            with lock:
                box.node = destination
            rebind(box, destination)
        finally:
            if paused_s > 0:
                try:
                    os.kill(box.process.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            busy.discard(vm)
        with lock:
            log.write(json.dumps({"vm": vm, "trace": box.entry["name"], "from": source["index"], "to": destination["index"],
                                  "mechanism": args.move, "start_ns": started, "end_ns": time.monotonic_ns(),
                                  "paused_s": paused_s, "source_cpu_s": dump_s}) + "\n")
            log.flush()

    by_index = {n["index"]: n for n in nodes}
    step = 0
    while not stop.wait(0.01):
        now = time.monotonic()
        with lock:
            live = list(sandboxes.items())
        runnable = {n: 0 for n in cores}
        for vm, box in live:
            run_total = 0
            for tid, path in list(box.vcpus.items()):
                try:
                    if open(path.replace("schedstat", "stat")).read().rsplit(")", 1)[1].split()[0] == "R":
                        runnable[box.node["index"]] += 1
                    run_total += int(open(path).read().split()[0])
                except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
                    pass
            old = previous.get(vm, run_total)
            previous[vm] = run_total
            recent[vm] = 0.9 * recent.get(vm, 0.0) + 0.1 * min(args.vcpus, (run_total - old) / 1e7)
        for n in cores:
            history[n][step % detect] = max(0.0, runnable[n] - cores[n])
        step += 1
        if args.move == "none":
            continue
        for n in cores:
            if cooldown[n] > now or sum(history[n]) / detect < args.move_threshold:
                continue
            spare = [m for m in cores if m != n and runnable[m] <= cores[m] - 1]
            candidates = [(recent.get(vm, 0.0), vm, box) for vm, box in live
                          if box.node["index"] == n and vm not in busy and locked.get(vm, 0.0) <= now
                          and recent.get(vm, 0.0) > 0.05 and box.process and box.process.poll() is None]
            if not spare or not candidates:
                continue
            destination = by_index[min(spare, key=lambda m: runnable[m])]
            _, vm, box = max(candidates, key=lambda c: c[0])
            memory_gib = (args.memory_mib or box.entry.get("memory_mib", 2048)) / 1024
            if args.move == "instant":
                paused_s, dump_s, delay_s = 0.0, 0.0, 0.0
            elif args.move == "hand-off":
                paused_s, dump_s, delay_s = args.handoff_ms / 1000, 0.0, args.handoff_ms / 1000
            else:
                dump_s = args.dump_s_per_gib * memory_gib
                delay_s = dump_s + args.xfer_s_per_gib * memory_gib
                paused_s = delay_s if args.move == "stop-and-copy" else 0.0
            busy.add(vm)
            runnable[destination["index"]] += 1
            locked[vm] = now + delay_s + args.move_hold_s
            cooldown[n] = now + delay_s + args.move_hold_s
            threading.Thread(target=move, args=(vm, box, by_index[n], destination, paused_s, dump_s, delay_s),
                             daemon=True).start()
    log.close()


def sampler(sandboxes, lock, nodes, stop, directory, interval_s):
    """Every interval, per-VM vCPU run/wait deltas (only nonzero rows) and per-node totals."""
    previous = {}
    with (directory / "vcpu.csv").open("w", newline="") as vm_stream, \
            (directory / "nodes.csv").open("w", newline="") as node_stream:
        vm_writer, node_writer = csv.writer(vm_stream), csv.writer(node_stream)
        vm_writer.writerow(["t_ns", "dt_ns", "node", "vm", "run_ns", "wait_ns"])
        node_writer.writerow(["t_ns", "dt_ns", "node", "live_vms", "run_ns", "wait_ns", "mem_available_kib"])
        last = time.monotonic_ns()
        while not stop.wait(max(0.0, interval_s - (time.monotonic_ns() - last) / 1e9)):
            now = time.monotonic_ns()
            dt, last = now - last, now
            with lock:
                live = list(sandboxes.items())
            totals = {n["index"]: [0, 0, 0] for n in nodes}
            for vm, box in live:
                totals[box.node["index"]][0] += 1
                for tid, path in list(box.pin_vcpus().items()):
                    try:
                        run_ns, wait_ns, _ = map(int, open(path).read().split())
                    except (FileNotFoundError, ProcessLookupError, ValueError):
                        continue
                    old = previous.get((vm, tid), (run_ns, wait_ns))
                    previous[(vm, tid)] = (run_ns, wait_ns)
                    d_run, d_wait = run_ns - old[0], wait_ns - old[1]
                    if d_run or d_wait:
                        vm_writer.writerow([now, dt, box.node["index"], vm, d_run, d_wait])
                        totals[box.node["index"]][1] += d_run
                        totals[box.node["index"]][2] += d_wait
            available = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemAvailable"))
            for index, (count, d_run, d_wait) in totals.items():
                node_writer.writerow([now, dt, index, count, d_run, d_wait, available])
            if available < 16 * 1024 * 1024:
                print("MemAvailable below 16GiB; stopping run", flush=True)
                stop.set()


def pilot(args):
    entries = [e for e in candidates(args.names) if (IMAGES / e["name"] / "image.json").exists()]
    nodes = parse_nodes(args.nodes, args.sibling_offset, args.pool)
    used = set().union(*(n["cores"] | n["siblings"] for n in nodes))
    reserved = replay.cpu_ids(args.harness_cpus) if args.harness_cpus else set(range(os.cpu_count())) - used
    if reserved & used or len(reserved) < 2:
        raise SystemExit(f"--harness-cpus must be >= 2 CPUs outside the node cores and siblings: {sorted(reserved)}")
    sampler_cpu = max(reserved)
    os.sched_setaffinity(0, reserved - {sampler_cpu})  # harness, mkfs and serial readers stay off the node CPUs
    RUNS.mkdir(parents=True, exist_ok=True)
    tag = (f"k{args.per_node}-{args.pool}" + (f"-g{args.group_size}" if args.group_size else "")
           + (f"-m{args.move}" if args.move != "none" else "") + f"-v{args.vcpus}-s{args.seed}")
    directory = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime(
        f"%Y%m%dT%H%M%SZ-pilot-{tag}-"), dir=RUNS))
    if args.network:
        network_up()
    overlay_initrd()
    save_json(directory / "run.json", {
        "kind": "pilot", "per_node": args.per_node, "pool": args.pool, "group_size": args.group_size,
        "move": args.move, "move_detect_ms": args.move_detect_ms, "move_threshold": args.move_threshold,
        "move_hold_s": args.move_hold_s, "handoff_ms": args.handoff_ms, "dump_s_per_gib": args.dump_s_per_gib,
        "xfer_s_per_gib": args.xfer_s_per_gib, "candidates": str(CANDIDATES),
        "group_jitter_s": args.group_jitter_s, "vcpus": args.vcpus, "memory_mib": args.memory_mib,
        "nodes": [{**n, "cores": sorted(n["cores"]), "siblings": sorted(n["siblings"]), "vcpu_cpus": sorted(n["vcpu_cpus"])}
                  for n in nodes],
        "harness_cpus": sorted(reserved - {sampler_cpu}), "cpu_sampler_cpu": sampler_cpu, "cpu_sample_ms": args.cpu_sample_ms,
        "duration_s": args.duration_s, "ramp_s": args.ramp_s, "gap_scale": args.gap_scale,
        "seed": args.seed, "sample_ms": args.sample_ms, "traces": [e["name"] for e in entries],
        "network": "host NAT" if args.network else "loopback only", "kernel": str(KERNEL),
        "firecracker_sha256": output(["sha256sum", str(FIRECRACKER)]).split()[0], "host_kernel": os.uname().release})
    cpu_log = subprocess.Popen(
        ["taskset", "-c", str(sampler_cpu), sys.executable, str(Path(__file__).resolve()), "cpu-sampler",
         "--groups", "/".join(",".join(map(str, sorted(n["cores"]))) for n in nodes),
         "--interval-ms", str(args.cpu_sample_ms), "--output", str(directory / "cpu1ms.csv")])
    sandboxes, lock, stop = {}, threading.Lock(), threading.Event()
    events = (directory / "sandboxes.jsonl").open("w")
    steps = (directory / "steps.jsonl").open("w")
    started = time.monotonic()
    deadline = started + args.ramp_s + args.duration_s
    counter = [0]

    def run_sandbox(entry, node, slot, delay=0.0):
        if delay and stop.wait(delay):
            return None
        with lock:
            vm = counter[0]
            counter[0] += 1
        box = Sandbox(entry, directory / "vm" / f"{vm:05d}", node, slot, args)
        error = None
        try:
            box.start()
            with lock:
                sandboxes[vm] = box
            while not box.complete.wait(.5):
                if time.monotonic() >= deadline or stop.is_set():
                    break
        except Exception as exc:
            error = str(exc)
        finally:
            with lock:
                sandboxes.pop(vm, None)
            box.stop()
        try:
            record = box.result() if box.process else {"trace": entry["name"], "node": node["index"], "slot": slot}
        except Exception as exc:
            record = {"trace": entry["name"], "node": node["index"], "slot": slot, "result_error": str(exc)}
        record.update({"vm": vm, "end_ns": time.monotonic_ns(), "error": error})
        with lock:
            events.write(json.dumps(record) + "\n")
            events.flush()
        if not args.keep_vm_dirs:
            shutil.rmtree(box.directory, ignore_errors=True)
        return record

    def slot_loop(node, slot):
        rng = random.Random(f"{args.seed}-{node['index']}-{slot}")
        if stop.wait(rng.uniform(0, args.ramp_s)):
            return
        while time.monotonic() < deadline and not stop.is_set():
            run_sandbox(rng.choice(entries), node, slot)

    def group_loop(group, members):
        """Synchronous RL step: G rollouts of one task start within the jitter; the next step waits for the slowest."""
        rng = random.Random(f"{args.seed}-group-{group}")
        if stop.wait(rng.uniform(0, args.ramp_s)):
            return
        step = 0
        while time.monotonic() < deadline and not stop.is_set():
            entry = rng.choice(entries)
            delays = [rng.uniform(0, args.group_jitter_s) for _ in members]
            results = [None] * len(members)

            def member(i, node, slot):
                results[i] = run_sandbox(entry, node, slot, delays[i])

            begin = time.monotonic_ns()
            workers = [threading.Thread(target=member, args=(i, n, s)) for i, (n, s) in enumerate(members)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            done = [r for r in results if r]
            with lock:
                steps.write(json.dumps({"group": group, "step": step, "trace": entry["name"], "begin_ns": begin,
                                        "end_ns": time.monotonic_ns(), "members": len(members),
                                        "completed": sum(bool(r.get("completed")) for r in done),
                                        "member_end_ns": [r["end_ns"] for r in done]}) + "\n")
                steps.flush()
            step += 1

    sampling = threading.Thread(target=sampler, args=(sandboxes, lock, nodes, stop, directory, args.sample_ms / 1000))
    sampling.start()
    controller = None
    if args.move != "none":
        if len({n["memory_node"] for n in nodes}) != 1:
            raise SystemExit("--move needs all nodes on one memory node (moves keep guest memory in place)")
        controller = threading.Thread(target=move_controller, args=(sandboxes, lock, nodes, stop, directory, args))
        controller.start()
    if args.group_size:
        total = args.per_node * len(nodes)
        if total % args.group_size:
            raise SystemExit("per-node x nodes must be a multiple of --group-size")
        # Member i of the whole fleet goes to node i mod N, so each group is spread across nodes.
        threads = [threading.Thread(target=group_loop, args=(g, [(nodes[i % len(nodes)], i // len(nodes))
                                                                 for i in range(g * args.group_size, (g + 1) * args.group_size)]))
                   for g in range(total // args.group_size)]
    else:
        threads = [threading.Thread(target=slot_loop, args=(n, s)) for n in nodes for s in range(args.per_node)]
    for thread in threads:
        thread.start()
    print(f"pilot {tag} × {len(nodes)} nodes, {args.ramp_s}s ramp + {args.duration_s}s -> {directory}", flush=True)
    try:
        for thread in threads:
            thread.join()
    finally:
        stop.set()
        sampling.join()
        if controller:
            controller.join()
        cpu_log.terminate()
        cpu_log.wait(timeout=10)
        events.close()
        steps.close()
    save_json(directory / "done.json", {"wall_s": round(time.monotonic() - started, 3), "sandboxes": counter[0]})
    print(f"done: {counter[0]} sandboxes -> {directory}", flush=True)


# Experiment 3 (implementation/virtual_machine, TAP ~37 Gb/s, nested): stop-and-copy p50 for 512MiB..2GiB guests.
MIGRATION_S = {"512MiB first ACK": 0.435, "1GiB first ACK": 0.770, "2GiB first ACK": 1.417,
               "512MiB working set": 2.368, "1GiB working set": 4.894, "2GiB working set": 9.469}


def percentile(values, q, weights=None):
    if not values:
        return None
    pairs = sorted(zip(values, weights or [1] * len(values)))
    total, acc = sum(w for _, w in pairs), 0
    for value, weight in pairs:
        acc += weight
        if acc >= q * total:
            return value
    return pairs[-1][0]


MOVE_LATENCY_S = {"instant": 0.0, "1ms": 0.001, "5ms": 0.005, "10ms": 0.010, "50ms": 0.050, "100ms": 0.100, **MIGRATION_S}


def load_ticks(directory, config, source, bin_ms):
    """[(t_ns, {unit: (dt_ns, run_ns, wait_ns, live)})] per pool unit (a node when pool is none)."""
    pool_of = {n["index"]: n.get("pool", n["index"]) for n in config["nodes"]}
    cores = {}
    for n in config["nodes"]:
        cores[pool_of[n["index"]]] = cores.get(pool_of[n["index"]], 0) + len(n["cores"])
    ticks = {}
    if source == "10ms":
        with (directory / "nodes.csv").open() as stream:
            for row in csv.DictReader(stream):
                unit = ticks.setdefault(int(row["t_ns"]), {}).setdefault(pool_of[int(row["node"])], [int(row["dt_ns"]), 0, 0, 0])
                unit[1] += int(row["run_ns"])
                unit[2] += int(row["wait_ns"])
                unit[3] += int(row["live_vms"])
    else:
        with (directory / "cpu1ms.csv").open() as stream:
            for row in csv.DictReader(stream):
                units = ticks.setdefault(int(row["t_ns"]), {})
                for n in config["nodes"]:
                    unit = units.setdefault(pool_of[n["index"]], [int(row["dt_ns"]), 0, 0, 0])
                    unit[1] += int(row[f"run{n['index']}"])
                    unit[2] += int(row[f"wait{n['index']}"])
    ordered = sorted(ticks.items())
    if bin_ms:
        binned, acc, acc_t = [], None, None
        for t, units in ordered:
            if acc is None:
                acc = {u: list(v) for u, v in units.items()}
            else:
                for u, v in units.items():
                    a = acc[u]
                    a[0] += v[0]; a[1] += v[1]; a[2] += v[2]; a[3] = max(a[3], v[3])
            if next(iter(acc.values()))[0] >= bin_ms * 1e6:
                binned.append((t, acc))
                acc = None
        ordered = binned
    return cores, [(t, {u: tuple(v) for u, v in units.items()}) for t, units in ordered]


def solo_expectations(solo, gap_scale):
    expected = {}
    if solo:
        for row in csv.DictReader((solo / "solo-summary.csv").open()):
            expected[row["name"]] = float(row["replay_s"]) + float(row["recorded_gap_s"]) * gap_scale
    return expected


def episodes(args):
    directory = args.run
    config = json.loads((directory / "run.json").read_text())
    source = args.source or ("1ms" if (directory / "cpu1ms.csv").exists() else "10ms")
    cores, ordered = load_ticks(directory, config, source, args.bin_ms)
    begin = ordered[0][0] + int(config["ramp_s"] * 1e9)
    end = begin + int(config["duration_s"] * 1e9)
    ordered = [(t, u) for t, u in ordered if begin <= t <= end]
    threshold = args.threshold_cores
    per_unit = {u: {"run_s": 0.0, "wait_s": 0.0, "time_s": 0.0, "contended_s": 0.0} for u in cores}
    open_episode, found = {}, []
    for t, tick in ordered:
        for u, (dt, d_run, d_wait, _) in tick.items():
            stats = per_unit[u]
            stats["run_s"] += d_run / 1e9
            stats["wait_s"] += d_wait / 1e9
            stats["time_s"] += dt / 1e9
            if dt and d_wait / dt >= threshold:
                stats["contended_s"] += dt / 1e9
                idle_elsewhere = sum(max(0.0, cores[m] - r / d) for m, (d, r, w, _) in tick.items() if m != u and d)
                episode = open_episode.setdefault(u, {"unit": u, "start_ns": t - dt, "length_s": 0.0, "lost_s": 0.0,
                                                      "lost_with_idle_elsewhere_s": 0.0, "peak_demand_cores": 0.0})
                episode["length_s"] += dt / 1e9
                episode["lost_s"] += d_wait / 1e9
                if idle_elsewhere >= 1:
                    episode["lost_with_idle_elsewhere_s"] += min(d_wait / 1e9, idle_elsewhere * dt / 1e9)
                episode["peak_demand_cores"] = max(episode["peak_demand_cores"], (d_run + d_wait) / dt)
            elif u in open_episode:
                found.append(open_episode.pop(u))
    found.extend(open_episode.values())
    lost = sum(e["lost_s"] for e in found)
    total_wait = sum(s["wait_s"] for s in per_unit.values())
    lengths, weights = [e["length_s"] for e in found], [e["lost_s"] for e in found]
    run_s = sum(s["run_s"] for s in per_unit.values())
    window_s = (ordered[-1][0] - ordered[0][0]) / 1e9 if len(ordered) > 1 else 0
    # Upper bound: a move triggered at episode start, effective after the latency, only for lost time with idle cores elsewhere.
    recover = {label: round(sum(e["lost_with_idle_elsewhere_s"] * max(0.0, 1 - latency / e["length_s"]) for e in found)
                            / lost, 4) if lost else None for label, latency in MOVE_LATENCY_S.items()}
    shorter = {label: round(sum(e["lost_s"] for e in found if e["length_s"] < latency) / lost, 4) if lost else None
               for label, latency in MOVE_LATENCY_S.items() if latency}
    sandboxes = [json.loads(line) for line in (directory / "sandboxes.jsonl").open()]
    expected = solo_expectations(args.solo, config["gap_scale"])
    command_slowdown, trace_slowdown = [], []
    for record in sandboxes:
        reference = args.solo / record["trace"] / "commands.csv" if args.solo else None
        commands = [c for c in record.get("commands", []) if c["host_end_ns"]]
        if reference and reference.exists():
            solo_s = {int(r["index"]): float(r["guest_duration_s"]) for r in csv.DictReader(reference.open())}
            for c in commands:
                if c["host_start_ns"] >= begin and c["host_end_ns"] <= end and solo_s.get(c["index"], 0) >= args.min_solo_s:
                    command_slowdown.append((c["host_end_ns"] - c["host_start_ns"]) / 1e9 / solo_s[c["index"]])
        if record.get("completed") and commands and record["trace"] in expected and commands[0]["host_start_ns"] >= begin:
            wall = (commands[-1]["host_end_ns"] - commands[0]["host_start_ns"]) / 1e9 + commands[0]["gap_s"] * config["gap_scale"]
            trace_slowdown.append(wall / expected[record["trace"]])
    step_slowdown = []
    steps_path = directory / "steps.jsonl"
    if steps_path.exists():
        for line in steps_path.open():
            step = json.loads(line)
            if step["completed"] == step["members"] and step["begin_ns"] >= begin and step["trace"] in expected:
                step_slowdown.append((step["end_ns"] - step["begin_ns"]) / 1e9 / (expected[step["trace"]] + config["group_jitter_s"]))
    q = lambda values: {f"p{int(x * 100)}": round(percentile(values, x), 3) if values else None for x in (.5, .9, .99)}
    summary = {
        "run": str(directory), "source": source, "bin_ms": args.bin_ms, "per_node": config["per_node"],
        "pool": config.get("pool", "none"), "group_size": config.get("group_size", 0), "vcpus": config["vcpus"],
        "seed": config["seed"], "cores_per_unit": cores,
        "sandboxes_per_core": round(config["per_node"] * len(config["nodes"]) / sum(cores.values()), 3),
        "window_s": round(window_s, 3), "threshold_wait_cores": threshold,
        "mean_run_cores_per_unit": {u: round(s["run_s"] / s["time_s"], 3) for u, s in per_unit.items() if s["time_s"]},
        "mean_demand_cores_per_unit": {u: round((s["run_s"] + s["wait_s"]) / s["time_s"], 3) for u, s in per_unit.items() if s["time_s"]},
        "contended_time_fraction": {u: round(s["contended_s"] / s["time_s"], 4) for u, s in per_unit.items() if s["time_s"]},
        "run_cpu_s": round(run_s, 3), "total_wait_s": round(total_wait, 3), "lost_cpu_s": round(lost, 3),
        "total_wait_over_run": round(total_wait / run_s, 4) if run_s else None,
        "lost_over_run": round(lost / run_s, 4) if run_s else None,
        "episodes": len(found), "episodes_per_unit_minute": round(len(found) / len(cores) / (window_s / 60), 2) if window_s else None,
        "episode_length_s": {f"p{int(x * 100)}": percentile(lengths, x) for x in (.5, .9, .99)},
        "episode_length_s_lost_weighted": {f"p{int(x * 100)}": percentile(lengths, x, weights) for x in (.5, .9, .99)},
        "lost_fraction_with_idle_core_elsewhere": round(sum(e["lost_with_idle_elsewhere_s"] for e in found) / lost, 4) if lost else None,
        "lost_fraction_in_episodes_shorter_than": shorter,
        "recoverable_upper_bound_by_move_latency": recover,
        "sandboxes": len(sandboxes), "sandboxes_completed": sum(bool(s.get("completed")) for s in sandboxes),
        "command_mismatches": sum(s.get("mismatches", 0) for s in sandboxes),
        "command_timeouts": sum(s.get("timeouts", 0) for s in sandboxes),
        "errors": sum(bool(s.get("error") or s.get("result_error")) for s in sandboxes),
        "command_slowdown_vs_solo": {"n": len(command_slowdown), "min_solo_s": args.min_solo_s, **q(command_slowdown)},
        "trace_slowdown_vs_solo": {"n": len(trace_slowdown), **q(trace_slowdown)},
        "step_slowdown_vs_solo": {"n": len(step_slowdown), **q(step_slowdown)}}
    suffix = source + (f"-bin{args.bin_ms:g}ms" if args.bin_ms else "")
    with (directory / f"episodes-{suffix}.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["unit", "start_ns", "length_s", "lost_s", "lost_with_idle_elsewhere_s",
                                                    "peak_demand_cores"])
        writer.writeheader()
        writer.writerows(found)
    save_json(directory / f"contention-{suffix}.json", summary)
    if not args.quiet:
        print(json.dumps(summary, indent=2))


def plot(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    runs = sorted(args.runs, key=lambda r: json.loads((r / "run.json").read_text())["per_node"])
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.5))
    labels, recover = [], {}
    for directory in runs:
        summary = json.loads((directory / "contention.json").read_text())
        label = f"{summary['sandboxes_per_core']:g}/core"
        labels.append(label)
        rows = list(csv.DictReader((directory / "episodes.csv").open()))
        pairs = sorted((float(r["length_s"]), float(r["lost_s"])) for r in rows)
        total, acc, xs, ys = sum(l for _, l in pairs), 0.0, [], []
        for length, lost in pairs:
            acc += lost
            xs.append(length)
            ys.append(acc / total)
        left.step(xs, ys, where="post", label=f"{label} (lost {summary['lost_over_run']:.0%} of run)")
        for name, value in summary["recoverable_upper_bound_by_move_latency"].items():
            recover.setdefault(name, []).append(value)
    for name, latency in MIGRATION_S.items():
        left.axvline(latency, color="gray", lw=.8, ls=":" if "working" in name else "--")
    left.set(xscale="log", xlabel="contention episode length (s)", ylabel="CDF of lost CPU time",
             title="Lost-CPU-weighted episode length\n(dashed: exp-3 migration to first ACK, dotted: to working set)")
    left.legend(fontsize=7)
    x = range(len(labels))
    for name, values in recover.items():
        right.plot(x, values, marker="o", label=name if name == "instant" else f"{name} ({MIGRATION_S[name]}s)",
                   ls="-" if name == "instant" else "--")
    right.set_xticks(list(x), labels)
    right.set(ylim=(0, 1.02), xlabel="sandboxes per physical core (5-core nodes, 1 vCPU each)",
              ylabel="recoverable fraction of lost CPU (upper bound)",
              title="Move at episode start to a node with an idle core")
    right.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(args.output, dpi=150)
    print(args.output)


def statistics_mean(values):
    values = list(values)
    return sum(values) / len(values)


def main():
    global CANDIDATES
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES, help="Trace list (select writes it)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("select")
    p.add_argument("--all", action="store_true", help="Keep every trace (with a per-trace guest memory size)")
    p.add_argument("--max-window-s", type=float, default=180)
    p.add_argument("--max-peak-gib", type=float, default=1.6)
    p.add_argument("--min-cpu-s", type=float, default=5)
    p.set_defaults(func=select)
    p = sub.add_parser("build")
    p.add_argument("names", nargs="*")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--keep-image", action="store_true", help="Keep the Docker image after export")
    p.set_defaults(func=build)
    p = sub.add_parser("solo")
    p.add_argument("names", nargs="*")
    p.add_argument("--cores", default="13-23", help="Physical cores, one microVM each")
    p.add_argument("--sibling-offset", type=int, default=24, help="SMT sibling = core + offset, used for VMM threads")
    p.add_argument("--node", type=int, default=1)
    p.add_argument("--memory-mib", type=int, help="Guest RAM for every sandbox (default: per trace from candidates, else 2048)")
    p.add_argument("--gap-scale", type=float, default=0, help="0 replays commands back to back")
    p.add_argument("--sample-ms", type=float, default=10)
    p.add_argument("--keep-disk", action="store_true")
    p.add_argument("--no-network", dest="network", action="store_false", help="Loopback only, no NAT egress")
    p.add_argument("--vcpus", type=int, default=1, help="vCPUs per microVM; each gets that many dedicated cores")
    p.add_argument("--harness-cpus", help="CPUs for the harness process (default: unrestricted)")
    p.add_argument("--resume", type=Path, help="Continue this solo run directory (finished traces are kept)")
    p.set_defaults(func=solo)
    p = sub.add_parser("pilot", help="Concurrent replay on cpuset nodes, no migration")
    p.add_argument("names", nargs="*")
    p.add_argument("--nodes", default="2-6/7-11/13-17/18-22", help="Physical cores per node, '/'-separated")
    p.add_argument("--sibling-offset", type=int, default=24)
    p.add_argument("--per-node", type=int, required=True, help="Concurrent sandboxes per node")
    p.add_argument("--vcpus", type=int, default=1)
    p.add_argument("--memory-mib", type=int, help="Guest RAM for every sandbox (default: per trace from candidates, else 2048)")
    p.add_argument("--upper-gib", type=int, default=8, help="Sparse per-sandbox writable overlay disk")
    p.add_argument("--duration-s", type=float, default=600)
    p.add_argument("--ramp-s", type=float, default=60, help="Slots start uniformly within this window; excluded from analysis")
    p.add_argument("--gap-scale", type=float, default=1, help="Recorded LLM wait multiplier")
    p.add_argument("--seed", default="1")
    p.add_argument("--sample-ms", type=float, default=10)
    p.add_argument("--keep-vm-dirs", action="store_true")
    p.add_argument("--pool", choices=("none", "socket", "all"), default="none",
                   help="vCPUs may run on all cores of nodes in the same socket / all nodes (idealized instant moves)")
    p.add_argument("--group-size", type=int, default=0, help="GRPO mode: G rollouts of one task per synchronous step")
    p.add_argument("--group-jitter-s", type=float, default=2.0)
    p.add_argument("--cpu-sample-ms", type=float, default=1.0, help="Per-node /proc/schedstat sampling interval")
    p.add_argument("--harness-cpus", help="CPUs for harness and samplers (default: every CPU outside the nodes)")
    p.add_argument("--move", choices=MOVES, default="none", help="Emulated cross-node moves (split nodes)")
    p.add_argument("--move-detect-ms", type=float, default=50)
    p.add_argument("--move-threshold", type=float, default=0.1, help="Mean runnable vCPUs over the cores that triggers a move")
    p.add_argument("--move-hold-s", type=float, default=1.0)
    p.add_argument("--handoff-ms", type=float, default=10)
    p.add_argument("--dump-s-per-gib", type=float, default=0.30, help="Source CPU and downtime per GiB (experiment 3)")
    p.add_argument("--xfer-s-per-gib", type=float, default=0.39, help="Transfer time per GiB (experiment 3, ~37 Gb/s)")
    p.add_argument("--no-network", dest="network", action="store_false")
    p.set_defaults(func=pilot)
    p = sub.add_parser("cpu-sampler", help=argparse.SUPPRESS)
    p.add_argument("--groups", required=True)
    p.add_argument("--interval-ms", type=float, default=1.0)
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(func=cpu_sampler)
    p = sub.add_parser("episodes", help="Contention episodes and migration recovery bounds for a pilot run")
    p.add_argument("run", type=Path)
    p.add_argument("--threshold-cores", type=float, default=0.1, help="Runqueue wait per sample (cores) that counts as contended")
    p.add_argument("--solo", type=Path, help="Solo run directory for per-command slowdown")
    p.add_argument("--min-solo-s", type=float, default=0.5)
    p.add_argument("--source", choices=("1ms", "10ms"), help="Per-node schedstat (default when present) or per-VM 10ms totals")
    p.add_argument("--bin-ms", type=float, default=0, help="Re-bin samples to this width before finding episodes")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=episodes)
    p = sub.add_parser("plot", help="Episode CDF and recovery bound across analyzed pilot runs (needs matplotlib)")
    p.add_argument("runs", nargs="+", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(func=plot)
    p = sub.add_parser("network-down", help="Remove the sandbox bridge, TAPs and iptables rules")
    p.set_defaults(func=network_down)
    args = parser.parse_args()
    CANDIDATES = args.candidates.resolve()
    args.func(args)


if __name__ == "__main__":
    main()
