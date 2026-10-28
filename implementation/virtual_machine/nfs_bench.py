#!/usr/bin/env python3
"""Read-only virtio-blk local/NFS experiment using the existing nested CXL lab.

No host NFS service is installed or changed. Both NFS endpoints run in owned
L1 VMs. The L2 retains its ext4 filesystem, open FD and page cache in shared DAX.
"""
import argparse
import base64
import collections
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time

import lab

HERE = Path(__file__).resolve().parent
MIB = 1024 * 1024


def checked(argv, **kw):
    return subprocess.run([str(v) for v in argv], check=True, **kw)


def prepare(args):
    """Extend standard assets without changing the original lab's initramfs."""
    if not 192 <= args.data_mib <= 1024:
        raise ValueError("data-mib must be 192..1024")
    lab.no_active_run()
    if lab.ASSETS.exists() and any(lab.ASSETS.iterdir()):
        raise FileExistsError(f"Refusing to overwrite existing assets: {lab.ASSETS}; choose new CXL_LAB_ASSETS")
    deps = args.dependencies.resolve()
    manifest = json.loads((deps / "manifest.json").read_text())
    build = deps / "kernel-build"
    release = (build / "include/config/kernel.release").read_text().strip()
    lab.prepare(argparse.Namespace(bootstrap_qemu=False, busybox=args.busybox,
                module_dir=deps / "kernel-modules/lib/modules" / release,
                l1_kernel=build / "arch/x86/boot/bzImage", l2_kernel=build / "vmlinux",
                firecracker=args.firecracker.resolve()))
    checked(["gcc", "-O2", "-static", "-Wall", "-Wextra", "-Werror",
             HERE / "nfs_probe.c", "-o", lab.ASSETS / "nfs-probe"])
    busybox = Path(args.busybox or shutil.which("busybox")).resolve()
    with tempfile.TemporaryDirectory(prefix="nfs-build-", dir=lab.LOCAL) as tmp:
        root = Path(tmp)
        l2 = root / "l2"
        lab.base_root(l2, busybox)
        shutil.copy2(HERE / "nfs-l2-init.sh", l2 / "init")
        (l2 / "init").chmod(0o755)
        shutil.copy2(lab.ASSETS / "nfs-probe", l2 / "lab/nfs-probe")
        lab.pack_initramfs(l2, lab.ASSETS / "nfs-l2-initramfs.cpio")
        l1 = root / "l1"
        l1.mkdir()
        with (lab.ASSETS / "l1-initramfs.cpio").open("rb") as stream:
            checked(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=l1, stdin=stream)
        shutil.copytree(deps / "runtime", l1, dirs_exist_ok=True, symlinks=True)
        shutil.copy2(lab.ASSETS / "nfs-l2-initramfs.cpio", l1 / "lab/nfs-l2-initramfs.cpio")
        shutil.copy2(lab.ASSETS / "nfs-probe", l1 / "lab/nfs-probe")
        lab.pack_initramfs(l1, lab.ASSETS / "l1-initramfs.cpio")
        payload = root / "payload"
        payload.mkdir()
        checked([lab.ASSETS / "nfs-probe", "generate", payload / "payload.bin", args.data_mib])
        images = root / "images"
        images.mkdir()
        inner = images / "sandbox.raw"
        with inner.open("xb") as stream:
            stream.truncate((args.data_mib + 64) * MIB)
        checked(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0",
                 "-E", "lazy_itable_init=0,lazy_journal_init=0", "-d", payload, inner])
        outer = lab.ASSETS / "storage.raw"
        if outer.exists():
            raise FileExistsError(f"Refusing to overwrite {outer}; choose new CXL_LAB_ASSETS")
        with outer.open("xb") as stream:
            stream.truncate((args.data_mib + 128) * MIB)
        checked(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0",
                 "-E", "lazy_itable_init=0,lazy_journal_init=0", "-d", images, outer])
        image_manifest = {"data_mib": args.data_mib, "payload_sha256": lab.sha256(payload / "payload.bin"),
                          "inner_image_sha256": lab.sha256(inner), "outer_image_sha256": lab.sha256(outer)}
    prepared = json.loads((lab.ASSETS / "manifest.json").read_text())
    prepared["nfs"] = {"dependencies": manifest, "images": image_manifest,
                       "sources": {name: lab.sha256(HERE / name) for name in
                                   ("nfs_probe.c", "nfs_bench.py", "nfs-l2-init.sh", "nfs_dependencies.sh", "lab.py")}}
    prepared["artifacts"] = {p.name: lab.sha256(p) for p in lab.ASSETS.iterdir()
                              if p.is_file() and p.name != "manifest.json"}
    lab.save_json(lab.ASSETS / "manifest.json", prepared)
    print(f"NFS assets prepared: {lab.ASSETS}", flush=True)


def shell(vm, body, timeout=60):
    return lab.guest_command(vm, body, timeout=timeout)


def extract(text, prefix):
    matches = [line.strip()[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {prefix} line: {text}")
    return matches[0]


def record_pid(directory):
    return ("p=$!\ns=$(cat /proc/$p/stat)\ns=${s##*) }\nset -- $s\nshift 19\n"
            f"printf '%s %s\\n' \"$p\" \"$1\" > {directory}/fc.pid\n")


def stop_fc(vm, directory):
    shell(vm, lab.snapshot_cleanup_script("/run/snapshot-bench-0000000000000000") +
          f"if [ -f {directory}/fc.pid ]; then\nread target_pid expected_start < {directory}/fc.pid\n"
          "case \"$target_pid:$expected_start\" in *[!0-9:]*|:|*:) exit 1 ;; esac\n"
          "[ \"$target_pid\" -gt 1 ]\nstop_snapshot_process\nfi\n")


def start_fc(vm, directory, ram_tier="cxl"):
    if ram_tier not in ("cxl", "dram"):
        raise ValueError("RAM tier must be cxl or dram")
    ram_env = ("FC_DAX_RAM=/dev/dax0.0 FC_DAX_OFFSET=2097152 FC_DAX_SNAPSHOT=1"
               if ram_tier == "cxl" else "FC_DAX_SNAPSHOT=0")
    shell(vm, f"mkdir -p {directory}\nrm -f {directory}/api.sock {directory}/input\n"
          f"mkfifo {directory}/input\n"
          f"(exec 3<>{directory}/input\n"
          f"exec env -u FC_DAX_RAM -u FC_DAX_OFFSET {ram_env} "
          f"/lab/firecracker --api-sock {directory}/api.sock <&3 > {directory}/fc.log 2>&1) &\n" +
          record_pid(directory) + f"n=0\nwhile [ ! -S {directory}/api.sock ]; do\n"
          f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 500 ]\nsleep .01\ndone")


def api(vm, directory, path, method, body):
    return shell(vm, f"/lab/shared-probe api {directory}/api.sock {method} {path} " + shlex.quote(json.dumps(body)))


def probe(vm, directory, command, label):
    # Commands and output labels contain no shell metacharacters, and are still quoted.
    body = f"printf '%s\\n' {shlex.quote(command)} > {directory}/input\n"
    pattern = f'"label":"{label}"'
    ending = r'"ok":(true|false)}[[:space:]]*$' if command.startswith("read ") else r'"resident_after":[0-9]+}[[:space:]]*$'
    complete = pattern + ".*" + ending
    body += (f"n=0\nwhile ! grep -Eq {shlex.quote(complete)} {directory}/fc.log; do\n"
             f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\n"
             f"! grep -q 'nfs-probe:' {directory}/fc.log\n"
             "n=$((n+1))\n[ $n -lt 6000 ]\nsleep .01\ndone\n"
             f"grep {shlex.quote(pattern)} {directory}/fc.log")
    text = shell(vm, body, timeout=90)
    rows = []
    for line in text.splitlines():
        if line.startswith('{"event":'):
            item = json.loads(line)
            if item.get("label") == label:
                rows.append(item)
    if len(rows) != 1:
        raise RuntimeError(f"Missing/duplicate probe result for {label}: {text}")
    return rows[0]


def counters(vm, *, raw=None):
    text = shell(vm, "printf 'COUNTERS='\n"
                 "printf '%s ' \"$(cat /sys/class/net/eth0/statistics/rx_bytes)\" "
                 "\"$(cat /sys/class/net/eth0/statistics/tx_bytes)\"\n"
                 "awk '$1==\"proc3\" {print $9; found=1} END {if (!found) print 0}' /proc/net/rpc/nfs\n"
                 "printf 'MOUNT_READ='\n"
                 "awk '$1==\"device\" {active=($0 ~ / mounted on \\/remote /)} "
                 "active && $1==\"READ:\" {print $2, $5, $6; found=1} "
                 "END {if (!found) print \"0 0 0\"}' /proc/self/mountstats\n")
    if raw is not None:
        raw.append(text)
    values = [int(v) for v in extract(text, "COUNTERS=").split()]
    if len(values) != 3:
        raise RuntimeError(f"Invalid counters {text}")
    result = dict(zip(("rx_bytes", "tx_bytes", "nfs_read_calls"), values))
    mount_values = [int(v) for v in extract(text, "MOUNT_READ=").split()]
    if len(mount_values) != 3:
        raise RuntimeError("Invalid NFS mount counters")
    result.update(zip(("mount_read_ops", "mount_read_sent_bytes", "mount_read_received_bytes"), mount_values))
    return result


def host_resident(vm, path, size, *, raw=None):
    text = shell(vm, f"/lab/nfs-probe resident {shlex.quote(path)} host 0 {size}")
    if raw is not None:
        raw.append({"path": path, "size": size, "text": text})
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"event":"resident"')]
    if len(rows) != 1:
        raise RuntimeError(f"Invalid host mincore result: {text}")
    return rows[0]["resident_after"]


def delta(before, after):
    result = {k: after[k] - v for k, v in before.items()}
    if any(v < 0 for v in result.values()):
        raise RuntimeError("Counter moved backwards")
    return result


def save_host_observation(out, label, counter_raw, cache_raw=None):
    """Persist serial observations independently of the derived report values."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label) or len(counter_raw) != 2:
        raise ValueError("A host observation requires a safe label and two counter snapshots")
    filename = label + "-host.json"
    lab.save_json(out / filename, {"schema_version": 1, "label": label,
                  "before": counter_raw[0], "after": counter_raw[1], "cache": cache_raw or {}})
    return filename


def upload(vm, path, data):
    encoded = base64.b64encode(data).decode()
    shell(vm, f"printf 'begin-base64 600 state\\n' > {path}.b64")
    for off in range(0, len(encoded), 512):
        shell(vm, f"printf '%s\\n' '{encoded[off:off+512]}' >> {path}.b64")
    shell(vm, f"printf '====\\n' >> {path}.b64\nuudecode -o {path} {path}.b64\n"
          f"printf '%s  %s\\n' {hashlib.sha256(data).hexdigest()} {path} | sha256sum -c -")


def summarize(samples):
    groups = collections.defaultdict(list)
    for sample in samples:
        if sample.get("measured"):
            groups[(sample["backend"], sample["pattern"], sample["condition"])].append(sample)
    summary = []
    for (backend, pattern, condition), rows in sorted(groups.items()):
        item = {"backend": backend, "pattern": pattern, "condition": condition, "n": len(rows)}
        for key in ("read_ns", "wall_ns", "read_mib_s", "wall_mib_s", "pread_p99_ns"):
            values = [row[key] for row in rows if key in row]
            if values:
                item[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
        item["guest_read_bytes_median"] = statistics.median(row["block_delta"]["read_sectors"]*512 for row in rows)
        item["nfs_read_calls_median"] = statistics.median(row["network_delta"]["nfs_read_calls"] for row in rows)
        item["network_rx_bytes_median"] = statistics.median(row["network_delta"]["rx_bytes"] for row in rows)
        item["nfs_read_received_bytes_median"] = statistics.median(row["network_delta"]["mount_read_received_bytes"] for row in rows)
        summary.append(item)
    return summary


def pin_vms(config, args):
    """Pin only the currently owned QEMU processes and their vhost workers."""
    a, b = config["vms"]["A"], config["vms"]["B"]
    vhost_affinity = {}
    for role, vm in (("A", a), ("B", b)):
        specification = getattr(args, "host_cpus_" + role.lower(), None)
        if specification:
            cpus = set()
            for part in specification.split(","):
                ends = [int(v) for v in part.split("-")]
                if len(ends) == 1:
                    cpus.add(ends[0])
                elif len(ends) == 2 and ends[0] <= ends[1]:
                    cpus.update(range(ends[0], ends[1]+1))
                else:
                    raise ValueError("Invalid host CPU range")
            if not cpus or not cpus <= os.sched_getaffinity(0):
                raise ValueError("Requested VM CPUs must be within the harness affinity")
            if not lab.alive(vm):
                raise RuntimeError("Owned QEMU exited before CPU affinity setup")
            for thread in Path(f"/proc/{vm['pid']}/task").iterdir():
                os.sched_setaffinity(int(thread.name), cpus)
            # vhost-net workers are kernel threads outside QEMU's thread group.
            # Match only this live, incarnation-checked lab VM's exact worker name.
            if not lab.alive(vm):
                raise RuntimeError("Owned QEMU exited before vhost affinity setup")
            workers = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if (entry / "comm").read_text().strip() == f"vhost-{vm['pid']}":
                        workers.append(int(entry.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue
            if len(workers) != 1:
                raise RuntimeError(f"Expected one owned single-queue vhost worker for {role}: {workers}")
            worker = workers[0]
            identity = lab.process_identity(worker)
            if identity is None or not lab.alive(vm):
                raise RuntimeError("Owned QEMU/vhost exited before affinity change")
            checked(["sudo", "-n", "taskset", "-pc", ",".join(str(c) for c in sorted(cpus)), worker],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if lab.process_identity(worker) != identity or os.sched_getaffinity(worker) != cpus:
                raise RuntimeError("Owned vhost worker identity/affinity changed")
            vhost_affinity[role] = {"pid": worker, "starttime": identity, "cpus": sorted(cpus)}
    return vhost_affinity


def setup_storage(a, b, directory, image_name="sandbox.raw"):
    """Set up read-only backing inside a fresh, disposable L1 pair.

    A pair is single-use: even after failure, stop it with lab.py stop and start
    a fresh pair before another storage experiment. This avoids stacked mounts
    and accidentally reusing NFS daemons or an old Firecracker on the DAX pool.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", image_name) or image_name in {".", ".."}:
        raise ValueError("image_name must be a plain filename")
    if not re.fullmatch(r"/run/[A-Za-z0-9_-]+", directory):
        raise ValueError("Invalid storage transaction directory")
    marker = "/run/nfs-storage-claimed"
    for vm in (a, b):
        shell(vm, f"if [ -e {marker} ]; then echo 'Storage pair already used; lab.py stop and start a fresh pair' >&2; exit 1; fi")
    # mkdir is an atomic claim; leave the marker on failure to reject dirty reuse.
    for vm in (a, b):
        shell(vm, f"mkdir {marker}\nprintf '%s\\n' {directory} > {marker}/owner")
    observations = {}
    for vm in (a, b):
        shell(vm, lab.snapshot_cleanup_script(include_boot_vm=True) +
              f"mkdir -p {directory} /storage /disk\nmount -t ext4 -o ro,noload,noatime /dev/vda /storage\n"
              f"test -f /storage/{image_name}\ntest -c /dev/dax0.0\n")
    for role, vm in (("A", a), ("B", b)):
        observations[f"{role}-environment"] = shell(vm,
            "uname -a\ncat /proc/meminfo\ncat /proc/cpuinfo\ncat /proc/mounts\n"
            "cat /sys/class/net/eth0/mtu\ncat /sys/class/block/vda/queue/read_ahead_kb\n")
    # Services exist only inside these owned disposable VMs; export just to B.
    shell(a, "export PATH=/usr/sbin:/usr/bin:/bin:/sbin:/lab\n"
          "mkdir -p /var/lib/nfs /run/rpcbind /proc/fs/nfsd\n"
          "mount -t nfsd nfsd /proc/fs/nfsd\n"
          "printf '/storage 172.30.0.2(ro,sync,no_subtree_check,no_root_squash,fsid=17)\\n' > /etc/exports\n"
          f"rpcbind -f -w > {directory}/rpcbind.log 2>&1 &\nsleep .2\n"
          "exportfs -ra\n"
          f"rpc.mountd --foreground --no-nfs-version 2 --no-nfs-version 4 --port 20048 > {directory}/mountd.log 2>&1 &\n"
          "rpc.nfsd -V3 -N4 4\n"
          "mount -o bind /storage /disk\n"
          "cat /proc/fs/nfsd/versions\nexportfs -v", timeout=30)
    shell(b, "mkdir -p /remote\n/usr/sbin/mount.nfs 172.30.0.1:/storage /remote "
          "-o ro,vers=3,proto=tcp,mountproto=tcp,port=2049,mountport=20048,nolock,hard,timeo=50,retrans=2,rsize=1048576\n"
          f"test -f /remote/{image_name}\ncat /proc/mounts", timeout=45)
    observations["mounts"] = shell(b, "cat /proc/mounts\ncat /proc/self/mountstats")
    return observations


def select_backend(vm, backend):
    """Replace B's backing mount and verify the effective readonly filesystem.

    The static lab BusyBox does not provide mountpoint. An unavailable command
    in an if-condition would silently skip unmounting despite set -e.
    """
    source, device, filesystem = {"local": ("/storage", "/dev/vda", "ext4"),
        "nfs": ("/remote", "172.30.0.1:/storage", "nfs")}[backend]
    raw = shell(vm, "while awk '$2==\"/disk\" {found=1} END {exit !found}' /proc/mounts; do\n"
                "umount /disk\ndone\n" + f"mount -o bind {source} /disk\ncat /proc/mounts")
    rows = [line.split() for line in raw.splitlines()]
    mounts = [row for row in rows if len(row) == 6 and row[1] == "/disk"]
    if len(mounts) != 1 or mounts[0][0] != device or mounts[0][2] != filesystem or "ro" not in mounts[0][3].split(","):
        raise RuntimeError(f"Invalid or stacked {backend} backing mount: {raw}")
    return raw


def run(args):
    if not 1 <= args.repetitions <= 50 or not 1 <= args.cycles <= 10:
        raise ValueError("Require 1..50 repetitions and 1..10 cycles")
    run_path, config = lab.current_run()
    if not config.get("dax_guest") or config.get("dax_kvm_state") or config.get("dax_handoff"):
        raise RuntimeError("Requires ordinary shared-DAX lab")
    if (config.get("migration_network") or {}).get("backend") != "tap" or not config.get("storage_image"):
        raise RuntimeError("Requires --migration-network --network-backend tap --storage-image")
    if not 256 <= args.memory_mib <= 2048 or args.memory_mib + 2 > config["cxl_mib"]:
        raise ValueError("Guest RAM must be 256..2048MiB and fit CXL")
    lab.verify(argparse.Namespace(timeout=180))
    a, b = config["vms"]["A"], config["vms"]["B"]
    vhost_affinity = pin_vms(config, args)
    image_bytes = (config["prepared"]["nfs"]["images"]["data_mib"] + 64) * MIB
    directory = "/run/nfs-" + os.urandom(8).hex()
    out = run_path / ("nfs-" + str(time.time_ns()))
    out.mkdir()
    report = {"result": "running", "run": str(run_path), "output": str(out),
              "config": config, "arguments": vars(args), "samples": [], "handoffs": [],
              "host_observation_schema": 1,
              "limitations": ["Nested KVM and emulated shared CXL on one coherent physical host",
                              "Read-only ext4 disk; no writable-image or noncoherent handoff proof",
                              "Guest RAM begins on DAX; no initial DRAM-to-CXL promotion",
                              "State file crosses serial controller; stage times are not optimized downtime",
                              "Outer read-only raw disk uses QEMU cache=none; storage device/controller cache remains",
                              "read_ns excludes full-content verification between pread calls; wall_ns includes verification",
                              "QD1 workloads; no network bandwidth/latency shaping; no physical NIC"]}
    report["runtime_sources"] = {name: lab.sha256(HERE / name) for name in
                                 ("nfs_bench.py", "nfs_probe.c", "nfs-l2-init.sh", "nfs_dependencies.sh", "lab.py")}
    report["host_cpu_affinity"] = {role: {thread.name: sorted(os.sched_getaffinity(int(thread.name)))
        for thread in Path(f"/proc/{vm['pid']}/task").iterdir()} for role, vm in config["vms"].items()}
    report["vhost_cpu_affinity"] = vhost_affinity
    report["cache_conditions"] = {"retained": "First read of A-warmed guest pages immediately after B restore",
        "retouch": "Immediate second read of the retained range on B",
        "warm": "Guest cache explicitly primed immediately before measurement",
        "cold_guest": "Guest file-data cache empty; full B image cache warm",
        "cold_host": "Guest and B image cache empty; full A image cache warm",
        "cold_all": "Guest, B client and A server image caches empty; storage cache uncontrolled",
        "first_miss": "First read of untouched guest range after B restore"}
    report["arguments"] = {k: str(v) if isinstance(v, Path) else v for k, v in report["arguments"].items() if k != "func"}
    lab.save_json(out / "report.json", report)
    rng = random.Random(args.seed)
    sequence = 0
    try:
        for name, text in setup_storage(a, b, directory).items():
            (out / f"{name}.txt").write_text(text)
        components = {
            "/machine-config": {"vcpu_count": 1, "mem_size_mib": args.memory_mib},
            "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux", "initrd_path": "/lab/nfs-l2-initramfs.cpio",
                             "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init"},
            "/drives/data": {"drive_id": "data", "path_on_host": "/disk/sandbox.raw", "is_root_device": False,
                             "is_read_only": True, "cache_type": "Writeback", "io_engine": "Sync"}}
        report["guest_configuration"] = components
        for cycle in range(args.cycles):
            # Counterbalance backend order; randomize condition order within trials.
            backends = ["nfs", "local"] if cycle % 2 == 0 else ["local", "nfs"]
            for backend in backends:
                trial = f"c{cycle}_{backend}"
                select_backend(b, backend)
                start_fc(a, directory)
                for path, body in components.items():
                    api(a, directory, path, "PUT", body)
                api(a, directory, "/actions", "PUT", {"action_type": "InstanceStart"})
                shell(a, f"n=0\nwhile ! grep -q '\"event\":\"ready\"' {directory}/fc.log; do\n"
                      f"read p s < {directory}/fc.pid\nkill -0 \"$p\"\nn=$((n+1))\n[ $n -lt 3000 ]\nsleep .01\ndone")
                warm = probe(a, directory, f"read {trial}_prime 0 {64*MIB} 65536 seq 1", f"{trial}_prime")
                if warm["resident_after"] != 64*MIB//4096 or warm["mismatched_words"]:
                    raise RuntimeError("Failed to prime guest cache")
                before_cookie = warm["open_cookie"]
                # A has a warm storage image. B starts with its own host caches cold.
                shell(a, "dd if=/storage/sandbox.raw of=/dev/null bs=1048576 2>/dev/null")
                shell(b, "echo 3 > /proc/sys/vm/drop_caches")
                stages = {}
                t = time.monotonic_ns()
                api(a, directory, "/vm", "PATCH", {"state": "Paused"})
                stages["pause_api_ns"] = time.monotonic_ns()-t
                t = time.monotonic_ns()
                api(a, directory, "/snapshot/create", "PUT", {"snapshot_type": "Full", "snapshot_path": directory+"/state", "mem_file_path": "/dev/dax0.0"})
                stages["snapshot_api_ns"] = time.monotonic_ns()-t
                stop_fc(a, directory)
                raw, integrity = lab.guest_fetch_file(a, directory+"/state")
                (out / f"{trial}-state.bin").write_bytes(raw)
                t = time.monotonic_ns()
                upload(b, directory+"/state", raw)
                stages["serial_state_upload_ns"] = time.monotonic_ns()-t
                content, _ = lab.guest_fetch_file(a, directory+"/fc.log")
                (out / f"{trial}-A.log").write_bytes(content)
                start_fc(b, directory)
                t = time.monotonic_ns()
                api(b, directory, "/snapshot/load", "PUT", {"snapshot_path": directory+"/state",
                    "mem_backend": {"backend_type": "File", "backend_path": "/dev/dax0.0"}, "resume_vm": False})
                stages["restore_api_ns"] = time.monotonic_ns()-t
                api(b, directory, "/vm", "PATCH", {"state": "Resumed"})
                handoff = {"cycle": cycle, "backend": backend, "ram_transfer_bytes": 0,
                           "state_bytes": len(raw), "state_integrity": integrity, "stages_ns": stages,
                           "source_cookie": before_cookie}
                report["handoffs"].append(handoff)

                def measure(condition, pattern, off, length, block, measured=True, read_seed=1, cache_state=None, cache_raw=None):
                    nonlocal sequence
                    sequence += 1
                    label = f"s{sequence}_{condition}_{pattern}"
                    counter_raw = []
                    before = counters(b, raw=counter_raw)
                    row = probe(b, directory, f"read {label} {off} {length} {block} {pattern} {read_seed}", label)
                    row.update(pread_p99_ns=row["pread_latency_ns"]["p99"], backend=backend, cycle=cycle, condition=condition, pattern=pattern,
                               measured=measured, host_cache_state=cache_state, network_delta=delta(before, counters(b, raw=counter_raw)))
                    row["host_observation_file"] = save_host_observation(out, label, counter_raw, cache_raw)
                    if row["open_cookie"] != before_cookie or row["mismatched_words"]:
                        raise RuntimeError(f"Guest identity/content mismatch: {row}")
                    if condition in ("retained", "retouch", "warm"):
                        if row["resident_before"] != length//4096 or row["block_delta"]["read_sectors"] or row["network_delta"]["nfs_read_calls"]:
                            raise RuntimeError(f"Warm cache unexpectedly read backing: {row}")
                    elif row["resident_before"] != 0 or row["block_delta"]["read_sectors"] == 0:
                        raise RuntimeError(f"Cold-cache precondition or block read missing: {row}")
                    if backend == "nfs" and condition == "cold_guest" and row["network_delta"]["nfs_read_calls"]:
                        raise RuntimeError(f"Host-warm condition had NFS read misses: {row}")
                    if backend == "nfs" and condition in ("cold_host", "cold_all", "first_miss") and not row["network_delta"]["nfs_read_calls"]:
                        raise RuntimeError(f"Expected remote NFS read: {row}")
                    report["samples"].append(row)
                    lab.save_json(out / "report.json", report)
                    print(json.dumps({"trial": trial, "condition": condition, "pattern": pattern,
                                      "read_mib_s": row["read_mib_s"], "guest_read_bytes": row["block_delta"]["read_sectors"]*512,
                                      "nfs_reads": row["network_delta"]["nfs_read_calls"]}), flush=True)
                    return row

                retained_raw = {"B": []}
                retained_pages = host_resident(b, "/disk/sandbox.raw", image_bytes, raw=retained_raw["B"])
                if retained_pages:
                    raise RuntimeError("Retained-cache test requires a cold B host image")
                first = measure("retained", "seq", 0, 64*MIB, 65536,
                                cache_state={"B_image_resident_pages": retained_pages}, cache_raw=retained_raw)
                handoff["destination_cookie"] = first["open_cookie"]
                measure("retouch", "seq", 0, 64*MIB, 65536)
                measure("first_miss", "seq", 128*MIB, 64*MIB, 65536)
                combinations = [(pattern, condition) for pattern in ("seq", "random") for condition in
                                (("warm", "cold_guest", "cold_host", "cold_all") if backend == "nfs" else ("warm", "cold_guest", "cold_host"))]
                for rep in range(args.repetitions):
                    rng.shuffle(combinations)
                    for pattern, condition in combinations:
                        length, block = (64*MIB, 65536) if pattern == "seq" else (8*MIB, 4096)
                        off = 0
                        cache_state = {}
                        cache_raw = {}
                        if condition == "warm":
                            probe(b, directory, f"read p{sequence}_{rep} {off} {length} {block} {pattern} 1", f"p{sequence}_{rep}")
                        else:
                            # Fully warm each host image before selectively dropping caches.
                            shell(a, "dd if=/storage/sandbox.raw of=/dev/null bs=1048576 2>/dev/null")
                            shell(b, "dd if=/disk/sandbox.raw of=/dev/null bs=1048576 2>/dev/null")
                            ev = probe(b, directory, f"evict e{sequence}_{rep} {off} {length}", f"e{sequence}_{rep}")
                            if ev["resident_after"]:
                                raise RuntimeError(f"Guest cache eviction failed: {ev}")
                            if condition in ("cold_host", "cold_all"):
                                shell(b, "echo 3 > /proc/sys/vm/drop_caches")
                            if condition == "cold_all":
                                shell(a, "echo 3 > /proc/sys/vm/drop_caches")
                            cache_raw = {"A": [], "B": []}
                            cache_state = {"A_image_resident_pages": host_resident(a, "/storage/sandbox.raw", image_bytes, raw=cache_raw["A"]),
                                           "B_image_resident_pages": host_resident(b, "/disk/sandbox.raw", image_bytes, raw=cache_raw["B"]),
                                           "image_pages": image_bytes//4096}
                            expected_b = image_bytes//4096 if condition == "cold_guest" else 0
                            expected_a = 0 if condition == "cold_all" else image_bytes//4096
                            if cache_state["B_image_resident_pages"] != expected_b or cache_state["A_image_resident_pages"] != expected_a:
                                raise RuntimeError(f"Host cache precondition failed ({condition}): {cache_state}")
                        measure(condition, pattern, off, length, block, read_seed=args.seed+cycle*1000+rep,
                                cache_state=cache_state, cache_raw=cache_raw)
                stop_fc(b, directory)
                content, _ = lab.guest_fetch_file(b, directory+"/fc.log")
                (out / f"{trial}-B.log").write_bytes(content)
                report["summary"] = summarize(report["samples"])
                lab.save_json(out / "report.json", report)
        report["result"] = "passed"
    except BaseException as exc:
        report["result"] = "failed"
        report["error"] = repr(exc)
        raise
    finally:
        report["cleanup_errors"] = []
        for role, vm in (("A", a), ("B", b)):
            try:
                stop_fc(vm, directory)
                raw, _ = lab.guest_fetch_file(vm, directory+"/fc.log")
                (out / f"last-{role}.log").write_bytes(raw)
            except Exception as exc:
                report["cleanup_errors"].append(f"{role}: {exc}")
        report["summary"] = summarize(report["samples"])
        cleanup_failed = bool(report["cleanup_errors"]) and report["result"] == "passed"
        if cleanup_failed:
            report["result"] = "failed"
            report["error"] = "Data checks passed, but owned Firecracker cleanup/log collection failed"
        lab.save_json(out / "report.json", report)
        print(f"NFS_REPORT={out / 'report.json'}", flush=True)
        if cleanup_failed:
            raise RuntimeError(report["error"])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--dependencies", type=Path, default=lab.LOCAL / "nfs-deps")
    prep.add_argument("--firecracker", type=Path, required=True)
    prep.add_argument("--busybox", default=None)
    prep.add_argument("--data-mib", type=int, default=256)
    prep.set_defaults(func=prepare)
    bench = sub.add_parser("run", help="Use a fresh L1 pair; stop/start the lab before each storage run")
    bench.add_argument("--memory-mib", type=int, default=512)
    bench.add_argument("--cycles", type=int, default=2)
    bench.add_argument("--repetitions", type=int, default=5)
    bench.add_argument("--seed", type=int, default=20261027)
    bench.add_argument("--host-cpus-a", default=None)
    bench.add_argument("--host-cpus-b", default=None)
    bench.set_defaults(func=run)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
