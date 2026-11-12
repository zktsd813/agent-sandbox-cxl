#!/usr/bin/env python3
"""Prepare read-only block/NFS fixtures and control owned nested lab VMs."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

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


def record_pid(directory):
    return ("p=$!\ns=$(cat /proc/$p/stat)\ns=${s##*) }\nset -- $s\nshift 19\n"
            f"printf '%s %s\\n' \"$p\" \"$1\" > {directory}/fc.pid\n")


def stop_fc(vm, directory):
    return shell(vm, lab.snapshot_cleanup_script("/run/snapshot-bench-0000000000000000") +
          f"if [ -f {directory}/fc.pid ]; then\nread target_pid expected_start < {directory}/fc.pid\n"
          "case \"$target_pid:$expected_start\" in *[!0-9:]*|:|*:) exit 1 ;; esac\n"
          "[ \"$target_pid\" -gt 1 ]\nstop_snapshot_process\nfi\n")


def api(vm, directory, path, method, body):
    return shell(vm, f"/lab/shared-probe api {directory}/api.sock {method} {path} " + shlex.quote(json.dumps(body)))


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
            if (config.get("migration_network") or {}).get("backend") != "tap":
                continue
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--dependencies", type=Path, default=lab.LOCAL / "nfs-deps")
    prep.add_argument("--firecracker", type=Path, required=True)
    prep.add_argument("--busybox", default=None)
    prep.add_argument("--data-mib", type=int, default=256)
    prep.set_defaults(func=prepare)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
