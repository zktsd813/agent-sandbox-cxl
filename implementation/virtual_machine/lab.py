#!/usr/bin/env python3
"""Two QEMU/KVM Linux guests, one ivshmem region, and Firecracker in each guest."""
import argparse
import base64
import datetime
import fcntl
import gzip
import hashlib
import io
import json
import lzma
import math
import mmap
import os
import pwd
from pathlib import Path
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import struct
import statistics
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import tty

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCAL = HERE / ".local"
ASSETS = Path(os.environ.get("CXL_LAB_ASSETS", str(LOCAL / "assets"))).resolve()
SESSION = os.environ.get("CXL_LAB_SESSION", "")
if SESSION and not re.fullmatch(r"[a-z0-9_-]{1,32}", SESSION):
    raise RuntimeError("CXL_LAB_SESSION must contain 1..32 lowercase letters, digits, _ or -")
CURRENT = LOCAL / (f"current-{SESSION}.json" if SESSION else "current.json")
QEMU_ROOT = Path(os.environ.get("CXL_LAB_QEMU_ROOT", str(LOCAL / "qemu/root"))).resolve()
KERNEL_BUILD = Path(os.environ.get("CXL_LAB_KERNEL_BUILD", str(LOCAL / "kernel-build"))).resolve()
KERNEL_MODULES = Path(os.environ.get("CXL_LAB_KERNEL_MODULES", str(LOCAL / "kernel-modules"))).resolve() / "lib/modules"
PACKAGES = ["qemu-system-x86", "qemu-system-common", "qemu-system-data", "seabios",
            "librdmacm1", "libibverbs1", "liburing2", "libnl-3-200", "libnl-route-3-200"]
HANDOFF_PRESET_DEFAULTS = {
    "mode": "warm", "guest_cpu": 0, "executor_cpu": 1,
    "poll_us": 50, "spin_us": 500, "keep_kvm_active": True,
    "no_seccomp": True, "direct_cpu_model": True, "no_nested_guest": True,
    "serial_file": True, "fixed_ram_hva": 0x400000000000,
    "full_snapshot_samples": 0,
}
HANDOFF_PRESETS = {
    "snapshot": {"reuse_kvm": False, "reuse_prepare": False},
    "reuse": {"reuse_kvm": True, "reuse_prepare": False},
    "reuse-prepared": {"reuse_kvm": True, "reuse_prepare": True},
}


def command(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def output(args, **kwargs):
    return command(args, stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()


def save_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def qemu_env(binary=None):
    env = os.environ.copy()
    if QEMU_ROOT.exists() and (binary is None or binary == QEMU_ROOT / "usr/bin/qemu-system-x86_64"):
        lib = str(QEMU_ROOT / "usr/lib/x86_64-linux-gnu")
        env["LD_LIBRARY_PATH"] = lib + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        env["QEMU_MODULE_DIR"] = str(QEMU_ROOT / "usr/lib/x86_64-linux-gnu/qemu")
    return env


def find_qemu():
    binary = shutil.which("qemu-system-x86_64")
    if binary:
        return Path(binary)
    binary = QEMU_ROOT / "usr/bin/qemu-system-x86_64"
    if binary.exists():
        return binary
    raise RuntimeError("QEMU missing. Install qemu-system-x86 or use prepare --bootstrap-qemu (Ubuntu 22.04).")


def bootstrap_qemu():
    """Extract Ubuntu packages locally; do not install packages or change host services."""
    downloads = LOCAL / "qemu/downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    command(["apt-get", "download", *PACKAGES], cwd=downloads)
    QEMU_ROOT.mkdir(exist_ok=True)
    for package in sorted(downloads.glob("*.deb")):
        command(["dpkg-deb", "-x", package, QEMU_ROOT])
    print(output([find_qemu(), "--version"], env=qemu_env()).splitlines()[0])


def process_identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return fields[19]  # field 22: starttime, after removing pid and comm
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(vm):
    return process_identity(vm["pid"]) == vm["starttime"]


def current_run():
    if not CURRENT.exists():
        raise RuntimeError("No lab run. Use start first.")
    run = Path(json.loads(CURRENT.read_text())["run"])
    return run, json.loads((run / "run.json").read_text())


def no_active_run():
    if CURRENT.exists():
        _, config = current_run()
        if any(alive(vm) for vm in config["vms"].values()):
            raise RuntimeError("A lab VM is running. Use stop before preparing or starting another run.")


def base_root(root, busybox):
    for directory in ["bin", "dev", "proc", "sys", "run", "tmp", "lab", "modules"]:
        (root / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy2(busybox, root / "bin/busybox")
    for applet in output([busybox, "--list"]).splitlines():
        path = root / "bin" / applet
        if not path.exists():
            path.symlink_to("busybox")


def pack_initramfs(root, destination):
    names = ["."] + sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    with destination.open("wb") as stream:
        command(["cpio", "--quiet", "--reproducible", "-o", "--format=newc", "--owner=0:0"],
                cwd=root, input=("\n".join(names) + "\n").encode(), stdout=stream)


def copy_modules(module_dir, target, vendor):
    dependencies = {}
    for line in (module_dir / "modules.dep").read_text().splitlines():
        name, deps = line.split(":", 1)
        dependencies[name] = deps.split()
    name = "kvm-intel" if vendor == "intel" else "kvm-amd"
    matches = [p for p in dependencies if Path(p).name.split(".ko")[0] == name]
    if len(matches) != 1:
        raise RuntimeError(f"Cannot find {name} in {module_dir}/modules.dep")
    ordered = []

    def visit(module):
        if module in ordered:
            return
        for dep in dependencies[module]:
            visit(dep)
        ordered.append(module)

    visit(matches[0])
    copied = []
    for module in ordered:
        source = module_dir / module
        data = source.read_bytes()
        if source.suffix == ".xz":
            data = lzma.decompress(data)
        elif source.suffix == ".gz":
            data = gzip.decompress(data)
        elif source.suffix == ".zst":
            data = subprocess.check_output(["zstd", "-d", "--stdout", str(source)])
        destination = target / (source.name.split(".ko")[0] + ".ko")
        destination.write_bytes(data)
        copied.append(destination.name)
    return copied


def prepare(args):
    LOCAL.mkdir(parents=True, exist_ok=True)
    no_active_run()
    if args.bootstrap_qemu:
        bootstrap_qemu()
    qemu = find_qemu()
    print(output([qemu, "--version"], env=qemu_env()).splitlines()[0])
    busybox = Path(args.busybox or shutil.which("busybox") or "/usr/bin/busybox").resolve()
    if "INTERP" in output(["readelf", "-l", busybox]):
        raise RuntimeError("Use a static BusyBox binary (e.g. busybox-static).")
    local_release = KERNEL_BUILD / "include/config/kernel.release"
    built_release = local_release.read_text().strip() if local_release.exists() else None
    if args.module_dir is None:
        args.module_dir = (KERNEL_MODULES / built_release if built_release
                           else Path("/lib/modules") / os.uname().release)
    if args.l2_kernel is None:
        args.l2_kernel = (KERNEL_BUILD / "vmlinux" if built_release
                          else REPO / ".local/microvm/vmlinux-6.1.188")
    release = args.module_dir.name
    l1_kernel = args.l1_kernel
    if not l1_kernel:
        candidates = ([KERNEL_BUILD / "arch/x86/boot/bzImage"] if release == built_release else [])
        candidates += [REPO / ".local/microvm/sched-preact/vmlinux-tracing", Path("/boot") / f"vmlinuz-{release}"]
        l1_kernel = next((p for p in candidates if p.exists() and os.access(p, os.R_OK)), None)
    if not l1_kernel:
        raise RuntimeError("Provide a readable --l1-kernel matching --module-dir.")
    if release not in output(["file", "-b", l1_kernel]):
        raise RuntimeError(f"L1 kernel must match module release {release}; check --l1-kernel/--module-dir.")
    for path in [args.firecracker, args.l2_kernel, busybox]:
        if not path.is_file():
            raise RuntimeError(f"Required input missing: {path}")
    vendor = "amd" if "AuthenticAMD" in Path("/proc/cpuinfo").read_text() else "intel"
    ASSETS.mkdir(exist_ok=True)
    command(["gcc", "-std=c11", "-O2", "-static", "-pthread", "-Wall", "-Wextra", "-Werror",
             HERE / "shared_probe.c", "-o", ASSETS / "shared-probe"])
    with tempfile.TemporaryDirectory(prefix="build-", dir=LOCAL) as temp:
        root = Path(temp)
        l2 = root / "l2"
        base_root(l2, busybox)
        shutil.copy2(HERE / "l2-init.sh", l2 / "init")
        (l2 / "init").chmod(0o755)
        shutil.copy2(ASSETS / "shared-probe", l2 / "lab/shared-probe")
        pack_initramfs(l2, ASSETS / "l2-initramfs.cpio")
        l1 = root / "l1"
        base_root(l1, busybox)
        shutil.copy2(HERE / "guest-init.sh", l1 / "init")
        (l1 / "init").chmod(0o755)
        shutil.copy2(HERE / "guest-cxl.sh", l1 / "lab/guest-cxl.sh")
        (l1 / "lab/guest-cxl.sh").chmod(0o755)
        for source, name in [(args.firecracker, "firecracker"), (args.l2_kernel, "l2-vmlinux"),
                             (ASSETS / "l2-initramfs.cpio", "l2-initramfs.cpio"),
                             (ASSETS / "shared-probe", "shared-probe")]:
            shutil.copy2(source, l1 / "lab" / name)
        modules = copy_modules(args.module_dir, l1 / "modules", vendor)
        (l1 / "lab/modules.list").write_text("\n".join(modules) + "\n")
        pack_initramfs(l1, ASSETS / "l1-initramfs.cpio")
    shutil.copy2(l1_kernel, ASSETS / "l1-kernel")
    inputs = {"l1_kernel": l1_kernel, "firecracker": args.firecracker,
              "l2_kernel": args.l2_kernel, "busybox": busybox,
              "probe_source": HERE / "shared_probe.c", "l1_init": HERE / "guest-init.sh",
              "l2_init": HERE / "l2-init.sh", "cxl_init": HERE / "guest-cxl.sh"}
    if (HERE / "cache_probe.h").exists():
        inputs["cache_probe_source"] = HERE / "cache_probe.h"
    if (HERE / "mptcp_probe.h").exists():
        inputs["mptcp_probe_source"] = HERE / "mptcp_probe.h"
    if l1_kernel.resolve() == (KERNEL_BUILD / "arch/x86/boot/bzImage").resolve():
        inputs["kernel_config"] = KERNEL_BUILD / ".config"
        inputs["kernel_source_metadata"] = HERE / "kernel-source.json"
        if (HERE / "cxl-static-capacity.patch").is_file():
            inputs["kernel_local_patch"] = HERE / "cxl-static-capacity.patch"
            inputs["kernel_cxl_hdm"] = REPO / "implementation/linux/drivers/cxl/core/hdm.c"
            inputs["kernel_cxl_mbox"] = REPO / "implementation/linux/drivers/cxl/core/mbox.c"
    manifest = {"vendor": vendor, "l1_kernel_release": release, "modules": modules,
                "qemu": str(qemu), "qemu_version": output([qemu, "--version"], env=qemu_env()).splitlines()[0],
                "firecracker_version": output([args.firecracker, "--version"]),
                "inputs": {k: {"path": str(p.resolve()), "sha256": sha256(p)} for k, p in inputs.items()},
                "artifacts": {p.name: sha256(p) for p in ASSETS.iterdir() if p.is_file() and p.name != "manifest.json"}}
    save_json(ASSETS / "manifest.json", manifest)
    print(f"Prepared {ASSETS}; L1 modules: {', '.join(modules)}")


def qmp(path, execute, arguments=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        with sock.makefile("rwb") as stream:
            json.loads(stream.readline())
            for number, name in enumerate(["qmp_capabilities", execute]):
                request = {"execute": name, "id": number}
                if name == execute and arguments is not None:
                    request["arguments"] = arguments
                stream.write((json.dumps(request) + "\n").encode())
                stream.flush()
                while True:
                    line = stream.readline()
                    if not line:
                        raise RuntimeError("QMP closed before replying")
                    reply = json.loads(line)
                    if reply.get("id") == number:
                        if "error" in reply:
                            raise RuntimeError(str(reply["error"]))
                        break
            return reply["return"]


def host_kvm():
    with open("/dev/kvm", "rb+", buffering=0) as device:
        if fcntl.ioctl(device, 0xAE00, 0) != 12:
            raise RuntimeError("Unexpected KVM API")
        vm = fcntl.ioctl(device, 0xAE01, 0)
        os.close(vm)


def migration_host_link(name):
    result = subprocess.run(["sudo", "-n", "ip", "-j", "-d", "link", "show", "dev", name],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        if "does not exist" in result.stderr or "Cannot find device" in result.stderr:
            return None
        raise RuntimeError(f"Cannot inspect lab network interface {name}: {result.stderr.strip()}")
    links = json.loads(result.stdout)
    if len(links) != 1 or links[0]["ifname"] != name:
        raise RuntimeError(f"Unexpected lab network interface response for {name}")
    return links[0]


def migration_host_network_setup(config):
    network = config.get("migration_network") or {}
    if network.get("backend") != "tap":
        return
    for device in ("/dev/net/tun", "/dev/vhost-net"):
        if not os.access(device, os.R_OK | os.W_OK):
            raise RuntimeError(f"TAP/vhost backend requires read/write access to {device}")
    host = network["host_network"]
    objects = host["objects"]
    for obj in objects.values():
        if migration_host_link(obj["name"]) is not None:
            raise RuntimeError(f"Refusing existing host network interface {obj['name']}")
    record = Path(config["run"]) / "run.json"
    bridge = objects["bridge"]
    command(["sudo", "-n", "ip", "link", "add", "name", bridge["name"], "address", bridge["mac"],
             "type", "bridge", "stp_state", "0", "forward_delay", "0",
             "nf_call_iptables", "0", "nf_call_ip6tables", "0", "nf_call_arptables", "0"])
    link = migration_host_link(bridge["name"])
    bridge.update(ifindex=link["ifindex"], created=True)
    save_json(record, config)
    command(["sudo", "-n", "ip", "link", "set", "dev", bridge["name"], "alias", host["alias"], "addrgenmode", "none", "up"])
    bridge["configured"] = True
    save_json(record, config)
    for role in ("A", "B"):
        obj = objects[role]
        command(["sudo", "-n", "ip", "tuntap", "add", "dev", obj["name"], "mode", "tap",
                 "user", host["owner_uid"], "vnet_hdr"])
        link = migration_host_link(obj["name"])
        obj.update(ifindex=link["ifindex"], initial_mac=link["address"], created=True)
        save_json(record, config)
        command(["sudo", "-n", "ip", "link", "set", "dev", obj["name"], "address", obj["mac"],
                 "alias", host["alias"], "addrgenmode", "none", "master", bridge["name"], "up"])
        obj["configured"] = True
        save_json(record, config)
    # No host address, route, external bridge port, firewall, or DNS change.
    host["ready"] = True
    save_json(record, config)


def migration_host_network_cleanup(config):
    network = config.get("migration_network") or {}
    if network.get("backend") != "tap":
        return
    host = network["host_network"]
    objects = host["objects"]
    bridge = objects["bridge"]
    owners = (host["owner_uid"], str(host["owner_uid"]), pwd.getpwuid(host["owner_uid"]).pw_name)
    for key in ("B", "A", "bridge"):
        obj = objects[key]
        if not obj.get("created"):
            continue
        link = migration_host_link(obj["name"])
        if link is None:
            obj["removed"] = True
            continue
        kind = link.get("linkinfo", {}).get("info_kind")
        valid = (obj.get("created") and link["ifindex"] == obj.get("ifindex") and
                 kind == obj["kind"] and link.get("address") in (obj["mac"], obj.get("initial_mac")))
        if key == "bridge":
            aliases = (host["alias"],) if obj.get("configured") else ("", host["alias"])
            valid = valid and link.get("ifalias", "") in aliases
            members = json.loads(output(["sudo", "-n", "ip", "-j", "link", "show", "master", obj["name"]]))
            valid = valid and not members
            addresses = json.loads(output(["sudo", "-n", "ip", "-j", "address", "show", "dev", obj["name"]]))
            valid = valid and all(not address.get("addr_info") for address in addresses)
        else:
            info = link.get("linkinfo", {}).get("info_data", {})
            valid = valid and info.get("type") == "tap" and info.get("user") in owners
            valid = valid and link.get("master") in (None, bridge["name"], bridge.get("ifindex"))
            valid = valid and link.get("ifalias", "") in ("", host["alias"])
        if not valid:
            raise RuntimeError(f"Refusing to delete host interface with changed identity/type/MAC/membership: {obj['name']}")
        command(["sudo", "-n", "ip", "link", "delete", "dev", obj["name"]])
        obj["removed"] = True
        save_json(Path(config["run"]) / "run.json", config)
    host["ready"] = False
    host["cleaned"] = True
    save_json(Path(config["run"]) / "run.json", config)


def start(args):
    no_active_run()
    host_kvm()
    prepared = json.loads((ASSETS / "manifest.json").read_text())
    for name, expected in prepared["artifacts"].items():
        if sha256(ASSETS / name) != expected:
            raise RuntimeError(f"Asset changed: {name}; run prepare again.")
    vendor = prepared["vendor"]
    parameter = Path(f"/sys/module/kvm_{'intel' if vendor == 'intel' else 'amd'}/parameters/nested")
    if parameter.read_text().strip().lower() not in ["y", "1"]:
        raise RuntimeError(f"Nested virtualization disabled: {parameter}")
    qemu = args.qemu.resolve() if args.qemu else find_qemu()
    qemu_version = output([qemu, "--version"], env=qemu_env(qemu)).splitlines()[0]
    storage_image = getattr(args, "storage_image", None)
    if storage_image:
        storage_image = storage_image.resolve(strict=True)
        if not storage_image.is_file():
            raise RuntimeError("--storage-image must be a regular raw image file")
    if args.cxl_mib and "volatile-memdev" not in output([qemu, "-device", "cxl-type3,help"], env=qemu_env(qemu)):
        raise RuntimeError("CXL mode requires QEMU with cxl-type3 volatile-memdev; pass --qemu PATH")
    if args.dax_guest and "x-direct-ram" not in output([qemu, "-device", "cxl-type3,help"], env=qemu_env(qemu)):
        raise RuntimeError("DAX guest RAM requires the locally patched QEMU with x-direct-ram")
    runs = LOCAL / "runs"
    runs.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=runs))
    run.chmod(0o700)
    # Keep UNIX socket names below sockaddr_un's 108-byte limit, even in a long checkout path.
    control = Path(tempfile.mkdtemp(prefix="qvl-", dir="/tmp"))
    memory_dir = run
    backing_dir = getattr(args, "memory_backing_dir", None)
    if backing_dir:
        if not backing_dir.is_dir():
            raise RuntimeError("Memory backing directory must already exist")
        memory_dir = Path(tempfile.mkdtemp(prefix="cxl-lab-", dir=backing_dir))
    backing = memory_dir / "shared-memory.bin"
    with backing.open("xb") as stream:
        stream.truncate(args.shared_mib * 1024 * 1024)
    cxl_backing = memory_dir / "cxl-memory.bin" if args.cxl_mib else None
    if cxl_backing:
        with cxl_backing.open("xb") as stream:
            stream.truncate(args.cxl_mib * 1024 * 1024)
    nonce = secrets.token_hex(8)
    migration_network = None
    if args.migration_network:
        migration_network = {"backend": args.network_backend,
                             "ips": {"A": "172.30.0.1", "B": "172.30.0.2"}, "prefix": 30}
        if args.network_backend == "socket":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                migration_port = listener.getsockname()[1]
            migration_network.update(transport="virtio-net over QEMU point-to-point TCP socket",
                                     socket_listener=args.socket_listener, host_address="127.0.0.1", host_port=migration_port)
        else:
            token = nonce[:8]
            suffix = ":".join(nonce[index:index + 2] for index in (0, 2, 4, 6))
            migration_network.update(transport="virtio-net single-queue TAP/vhost-net over an isolated host Linux bridge",
                vhost=True, queues=1, host_network={"owner_uid": os.getuid(), "alias": "cxl-lab:" + nonce,
                    "host_ip": None, "external_uplink": None, "ipv6_addrgen": "none", "bridge_netfilter": False,
                    "objects": {"bridge": {"name": "cxb" + token, "kind": "bridge", "mac": "02:ca:" + suffix},
                                "A": {"name": "cxt" + token + "a", "kind": "tun", "mac": "02:cb:" + suffix},
                                "B": {"name": "cxt" + token + "b", "kind": "tun", "mac": "02:cc:" + suffix}}})
    config = {"run": str(run), "control_dir": str(control), "nonce": nonce, "rounds": args.rounds,
              "l1_vcpus": args.vcpus, "l1_memory_mib": args.memory_mib,
              "shared_mib": args.shared_mib, "backing": str(backing),
              "ephemeral_memory_dir": str(memory_dir) if memory_dir != run else None,
              "cxl_mib": args.cxl_mib, "cxl_backing": str(cxl_backing) if cxl_backing else None,
              "qemu": str(qemu), "qemu_version": qemu_version,
              "qemu_sha256": sha256(qemu),
              "dax_guest": args.dax_guest,
              "dax_kvm_state": args.dax_kvm_state,
              "dax_handoff": args.dax_handoff,
              "dax_4k": args.dax_4k or args.dax_handoff,
              "migration_network": migration_network,
              "prepared": prepared, "assets": str(ASSETS), "host_kernel": os.uname().release, "vms": {}}
    if storage_image:
        config["storage_image"] = {"path": str(storage_image), "sha256": sha256(storage_image),
                                   "readonly": True, "format": "raw", "cache": "none"}
    if args.dax_guest:
        config["source_patches"] = {name: sha256(HERE / name) for name in
                                    ["qemu-cxl-direct-ram.patch", "firecracker.patch", "ram-backend-source.json"]}
    if args.dax_kvm_state or args.dax_handoff:
        config["source_patches"].update({name: sha256(HERE / name) for name in
                                         ["kvm-dax-state.patch", "kvm-dax-source.json", "pointer-audit.json"]})
    save_json(run / "run.json", config)
    save_json(CURRENT, {"run": str(run)})
    try:
        migration_host_network_setup(config)
        launch_order = ([args.socket_listener, "B" if args.socket_listener == "A" else "A"]
                        if migration_network and args.network_backend == "socket" else ["A", "B"])
        for role in launch_order:
            prefix = run / ("vm-" + role.lower())
            console_path = str(control / (role.lower() + ".console.sock"))
            qmp_path = str(control / (role.lower() + ".qmp.sock"))
            # QEMU's unix socket paths must fit sockaddr_un.sun_path.
            if max(len(console_path.encode()), len(qmp_path.encode())) >= 108:
                raise RuntimeError("Runtime path is too long for UNIX sockets.")
            cmd = [str(qemu), "-name", "nested-lab-" + role,
                   "-machine", "q35,accel=kvm" + (",cxl=on" if cxl_backing else ""),
                   "-cpu", "host," + ("vmx=on" if vendor == "intel" else "svm=on"),
                   "-smp", str(args.vcpus), "-m", (f"{args.memory_mib}M,maxmem={args.memory_mib + args.cxl_mib}M,slots=8"
                                                             if cxl_backing else str(args.memory_mib)),
                   "-nodefaults", "-no-user-config", "-display", "none", "-monitor", "none", "-no-reboot",
                   "-kernel", str(ASSETS / "l1-kernel"), "-initrd", str(ASSETS / "l1-initramfs.cpio"),
                   # Keep informational printk output from interleaving with result JSON.
                   "-append", f"console=ttyS0 loglevel=4 rdinit=/init panic=1 lab.role={role} lab.nonce={nonce} lab.rounds={args.rounds} lab.cxl_bytes={args.cxl_mib * 1024 * 1024} lab.dax_guest={int(args.dax_guest)} lab.dax_kvm_state={int(args.dax_kvm_state)} lab.dax_handoff={int(args.dax_handoff)} lab.dax_4k={int(args.dax_4k or args.dax_handoff)}" + (" nokaslr kvm_dax_sync_init=1" if args.dax_handoff else ""),
                   "-object", f"memory-backend-file,id=shared,size={args.shared_mib}M,mem-path={backing},share=on",
                   "-device", "ivshmem-plain,memdev=shared",
                   "-chardev", f"socket,id=console,path={console_path},server=on,wait=off,logfile={prefix}.serial.log",
                   "-serial", "chardev:console", "-qmp", f"unix:{qmp_path},server=on,wait=off"]
            if migration_network:
                cmd[cmd.index("-append") + 1] += " lab.migration_ip=" + migration_network["ips"][role]
                if args.network_backend == "socket":
                    mode = "listen" if role == args.socket_listener else "connect"
                    backend = f"socket,id=migration,{mode}=127.0.0.1:{migration_port}"
                else:
                    tap = migration_network["host_network"]["objects"][role]["name"]
                    backend = f"tap,id=migration,ifname={tap},script=no,downscript=no,vhost=on,queues=1"
                cmd.extend(["-netdev", backend,
                            "-device", "virtio-net-pci,romfile=,netdev=migration,mac=52:54:00:ca:10:" + ("01" if role == "A" else "02")])
            if storage_image:
                cmd.extend(["-drive", f"file={storage_image},if=none,id=storage,format=raw,readonly=on,cache=none",
                            "-device", "virtio-blk-pci,drive=storage"])
            if cxl_backing:
                cmd.extend(["-object", f"memory-backend-file,id=cxl-shared,size={args.cxl_mib}M,mem-path={cxl_backing},share=on",
                            "-device", "pxb-cxl,bus_nr=12,bus=pcie.0,id=cxl.0",
                            "-device", "cxl-rp,port=0,bus=cxl.0,id=cxl-rp0,chassis=0,slot=2",
                            "-device", "cxl-type3,bus=cxl-rp0,volatile-memdev=cxl-shared,id=cxl-mem0,sn=0x100" + (",x-direct-ram=on" if args.dax_guest else ""),
                            "-machine", f"cxl-fmw.0.targets.0=cxl.0,cxl-fmw.0.size={args.cxl_mib}M,cxl-fmw.0.interleave-granularity=256"])
            if qemu == QEMU_ROOT / "usr/bin/qemu-system-x86_64":
                cmd.extend(["-L", str(QEMU_ROOT / "usr/share/qemu"),
                            "-bios", str(QEMU_ROOT / "usr/share/seabios/bios-256k.bin")])
            elif (qemu.parent.parent / "share/qemu/bios-256k.bin").is_file():
                cmd.extend(["-L", str(qemu.parent.parent / "share/qemu"),
                            "-bios", str(qemu.parent.parent / "share/qemu/bios-256k.bin")])
            elif args.dax_guest:
                cmd.extend(["-L", str(QEMU_ROOT / "usr/share/qemu"),
                            "-bios", str(QEMU_ROOT / "usr/share/seabios/bios-256k.bin")])
            with Path(str(prefix) + ".qemu.log").open("wb") as stream:
                process = subprocess.Popen(cmd, env=qemu_env(qemu), stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            identity = process_identity(process.pid)
            if identity is None:
                raise RuntimeError(f"QEMU {role} exited at launch; see {prefix}.qemu.log")
            config["vms"][role] = {"pid": process.pid, "starttime": identity, "command": cmd,
                                    "console": console_path, "qmp": qmp_path,
                                    "serial_log": str(prefix) + ".serial.log"}
            config["vms"] = {name: config["vms"][name] for name in ("A", "B") if name in config["vms"]}
            save_json(run / "run.json", config)
            if migration_network and args.network_backend == "socket" and role == args.socket_listener:
                deadline = time.monotonic() + 20
                while not Path(qmp_path).exists():
                    if not alive(config["vms"][role]) or time.monotonic() > deadline:
                        raise RuntimeError("Migration network listener failed before starting peer")
                    time.sleep(.05)
                qmp(qmp_path, "query-status")
        deadline = time.monotonic() + 20
        for role, vm in config["vms"].items():
            while not Path(vm["qmp"]).exists():
                if not alive(vm) or time.monotonic() > deadline:
                    raise RuntimeError(f"QEMU {role} failed; see {run}/vm-{role.lower()}.qemu.log")
                time.sleep(.1)
            acceleration = qmp(vm["qmp"], "query-kvm")
            if not acceleration.get("enabled"):
                raise RuntimeError(f"QEMU {role} is not using KVM")
        print(f"Started both VMs: {run}\nUse verify to wait for guest checks; stop shuts down only this lab.")
    except BaseException:
        stop_config(config)
        raise


def events(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("LAB_JSON "):
            try:
                result.append(json.loads(line[len("LAB_JSON "):]))
            except json.JSONDecodeError:
                pass
    return result


def verify(args):
    run, config = current_run()
    deadline = time.monotonic() + args.timeout
    required = {"l1_kvm", "shared_memory", "l2_memory", "nested_microvm"}
    if config.get("cxl_mib"):
        required.add("dax_shared_memory")
    if config.get("migration_network"):
        required.add("migration_network")
    requirements = {role: set(required) for role in config["vms"]}
    if config.get("dax_guest"):
        requirements["B"] -= {"l2_memory", "nested_microvm"}
        requirements["B"].add("dax_observer")
    if config.get("dax_handoff"):
        requirements = {role: {"l1_kvm", "shared_memory", "dax_shared_memory", "direct_ready"} for role in config["vms"]}
    results = {}
    while True:
        results = {role: events(Path(vm["serial_log"])) for role, vm in config["vms"].items()}
        for role, records in results.items():
            failed = [e for e in records if e["event"] == "failure"]
            if failed:
                raise RuntimeError(f"VM {role}: {failed[-1]}; inspect its serial log/console.")
        if len(results) == 2 and all(requirements[role] <= {e["event"] for e in records if e.get("result") == "passed"}
                                     for role, records in results.items()):
            break
        if any(not alive(vm) for vm in config["vms"].values()):
            raise RuntimeError(f"A QEMU VM exited; inspect logs in {run}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"Verification timed out; inspect logs in {run}")
        time.sleep(.25)
    for role, records in results.items():
        for record in records:
            if record.get("role") != role:
                raise RuntimeError(f"Unexpected role in {role} log: {record}")
        shared = next(e for e in records if e["event"] == "shared_memory")
        if shared["nonce"] != config["nonce"] or shared["rounds"] != config["rounds"]:
            raise RuntimeError("Shared-memory result does not match this run")
        if config.get("cxl_mib"):
            dax = next(e for e in records if e["event"] == "dax_shared_memory")
            if dax["nonce"] != config["nonce"] or dax["rounds"] != 100 or dax["device_bytes"] != config["cxl_mib"] * 1024 * 1024:
                raise RuntimeError("CXL device-DAX result does not match this run")
    final_slots = {}
    guest_kernels = {}
    l2_kernels = {}
    expected_release = config["prepared"]["l1_kernel_release"]
    for role, vm in config["vms"].items():
        text = Path(vm["serial_log"]).read_text(errors="replace")
        match = re.search(r"^LAB_BOOT role=" + role + r" kernel=(\S+)", text, re.MULTILINE)
        if not match or match[1] != expected_release:
            raise RuntimeError(f"VM {role} did not boot expected kernel {expected_release}")
        guest_kernels[role] = match[1]
        if config.get("dax_handoff") or (config.get("dax_guest") and role == "B"):
            continue
        inner = re.search(r"^L2_BOOTED role=" + role + r" kernel=(\S+)", text, re.MULTILINE)
        if not inner:
            raise RuntimeError(f"VM {role} has no L2 kernel boot identification")
        l2_kernels[role] = inner[1]
    with Path(config["backing"]).open("rb") as stream:
        with mmap.mmap(stream.fileno(), 8192, access=mmap.ACCESS_READ) as memory:
            for role, offset in [("A", 0), ("B", 4096)]:
                sequence, checksum, nonce = struct.unpack_from("<IIQ", memory, offset)
                if sequence != config["rounds"] or f"{nonce:016x}" != config["nonce"]:
                    raise RuntimeError(f"Host backing does not contain final {role} mailbox")
                final_slots[role] = {"sequence": sequence, "checksum": checksum,
                                     "payload_sha256": hashlib.sha256(memory[offset + 16:offset + 16 + 3072]).hexdigest()}
    acceleration = {role: qmp(vm["qmp"], "query-kvm") for role, vm in config["vms"].items()}
    if not all(a.get("enabled") for a in acceleration.values()):
        raise RuntimeError("An outer VM is not using KVM")
    report = {"result": "passed", "run": str(run), "host_kernel": config["host_kernel"],
              "l1_guest_kernels": guest_kernels,
              "l2_guest_kernels": l2_kernels,
              "outer_kvm": acceleration, "guest_events": results, "shared_backing_final_slots": final_slots,
              "scope": "Two independent L1 kernels; ivshmem round trip; actual Firecracker L2 boot and memory checks.",
              "limitations": ["Same physical host, not actual CXL or cross-host coherence.",
                              "L2 guest RAM is private; ivshmem is a shared transport between L1 VMs.",
                              "No microVM handoff or RAM backend modification is implemented."]}
    if config.get("dax_handoff"):
        addresses = {}
        for role, vm in config["vms"].items():
            text = guest_command(vm, "cat /sys/module/kvm/sections/.text\ncat /sys/module/kvm_intel/sections/.text")
            addresses[role] = re.findall(r"^0x[0-9a-f]+\s*$", text, re.MULTILINE)
            addresses[role] = [x.strip() for x in addresses[role]]
        if len(addresses["A"]) != 2 or addresses["A"] != addresses["B"]:
            raise RuntimeError(f"KVM module text addresses differ: {addresses}")
        report["module_text_addresses"] = addresses
    if config.get("cxl_mib"):
        with Path(config["cxl_backing"]).open("rb") as stream:
            data = stream.read(8192)
        dax_slots = {}
        for role, offset in [("A", 0), ("B", 4096)]:
            sequence, checksum, nonce = struct.unpack_from("<IIQ", data, offset)
            if sequence != 100 or f"{nonce:016x}" != config["nonce"]:
                raise RuntimeError("CXL backing final mailbox does not match this run")
            dax_slots[role] = {"sequence": sequence, "checksum": checksum}
        report["cxl_dax"] = {"backing": config["cxl_backing"], "bytes": config["cxl_mib"] * 1024 * 1024,
                             "final_slots": dax_slots, "mode": "CXL Type-3 fixed volatile region → device-DAX"}
        report["scope"] += " CXL Type-3 region, device-DAX mmap, and bidirectional DAX exchange."
        report["limitations"][1] = "L2 guest RAM is private; ivshmem and device-DAX are shared between L1 VMs."
        report["limitations"].append("DCD dynamic capacity events and physical multi-host CXL coherence are not tested.")
        report["limitations"].append("The QEMU lab kernel uses CONFIG_CXL_REGION_INVALIDATION_TEST to bypass the hypervisor cache-invalidation restriction.")
    if config.get("dax_guest"):
        report["scope"] = "One Firecracker on VM A; all 128MiB guest RAM in shared device-DAX; VM B is an observer."
        report["dax_guest_ram"] = {"owner": "A", "offset": 2097152, "bytes": 128 * 1024 * 1024}
        report["limitations"][1] = "VMM heap and KVM execution metadata remain local to VM A."
        report["limitations"][2] = "No execution handoff is implemented; QEMU direct RAM mode is limited to one fixed volatile CXL region."
    if config.get("dax_kvm_state"):
        report["scope"] += " Opt-in KVM execution objects use a reserved device-DAX arena; Firecracker uses a DAX heap."
        report["limitations"][1] = "Host OS bindings (mm/task/PID/FD/per-CPU SRCU and scheduler state) remain local; kernel allocator metadata is local."
        report["limitations"].append("KVM DAX is an experimental single-owner Intel/EPT path; use kvm-state-test to check actual placement and external references.")
    save_json(run / "report.json", report)
    print(json.dumps(report, indent=2))


def stop_config(config):
    for vm in config["vms"].values():
        if not alive(vm):
            continue
        try:
            qmp(vm["qmp"], "quit")
        except (OSError, RuntimeError):
            if alive(vm):
                os.kill(vm["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while any(alive(vm) for vm in config["vms"].values()) and time.monotonic() < deadline:
        time.sleep(.1)
    for vm in config["vms"].values():
        if alive(vm):
            os.kill(vm["pid"], signal.SIGKILL)
    if (config.get("migration_network") or {}).get("backend") == "tap" or config.get("ephemeral_memory_dir"):
        deadline = time.monotonic() + 5
        while any(alive(vm) for vm in config["vms"].values()) and time.monotonic() < deadline:
            time.sleep(.05)
        if any(alive(vm) for vm in config["vms"].values()):
            raise RuntimeError("A lab QEMU is still alive; preserving its network and memory backing")
        migration_host_network_cleanup(config)
    control = config.get("control_dir")
    if control and not any(alive(vm) for vm in config["vms"].values()):
        for vm in config["vms"].values():
            Path(vm["console"]).unlink(missing_ok=True)
            Path(vm["qmp"]).unlink(missing_ok=True)
        try:
            Path(control).rmdir()
        except OSError:
            pass
    memory_dir = config.get("ephemeral_memory_dir")
    if memory_dir and not any(alive(vm) for vm in config["vms"].values()):
        directory = Path(memory_dir)
        if directory.name.startswith("cxl-lab-") and not directory.is_symlink():
            for name in ("shared-memory.bin", "cxl-memory.bin"):
                (directory / name).unlink(missing_ok=True)
            directory.rmdir()


def stop(_):
    run, config = current_run()
    stop_config(config)
    print(f"Stopped lab VMs. Results retained: {run}")


def status(_):
    run, config = current_run()
    result = {"run": str(run), "vms": {}}
    for role, vm in config["vms"].items():
        info = {"pid": vm["pid"], "alive": alive(vm), "console": vm["console"],
                "completed_checks": [e["event"] for e in events(Path(vm["serial_log"]))]}
        if info["alive"]:
            info["qemu_status"] = qmp(vm["qmp"], "query-status")
        result["vms"][role] = info
    print(json.dumps(result, indent=2))


def guest_command(vm, body, timeout=15):
    """Run a bounded command through the L1 serial shell, preserving its session."""
    if not alive(vm):
        raise RuntimeError("VM is not running")
    marker = "LAB_COMMAND_DONE_" + secrets.token_hex(8)
    script = f"\n(\nset -e\n{body}\n)\nprintf '\\n{marker}:%s\\n' \"$?\"\n"
    data = bytearray()
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        sock.connect(vm["console"])
        sock.sendall(script.encode())
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError("Guest console closed")
            data.extend(chunk)
            text = data.decode(errors="replace")
            for line in text.splitlines():
                match = re.fullmatch(re.escape(marker) + r":(\d+)", line.strip())
                if match:
                    if int(match[1]):
                        raise RuntimeError(f"Guest command failed:\n{text}")
                    return text
    raise RuntimeError(f"Guest console command timed out:\n{data.decode(errors='replace')}")


def guest_fetch_file(vm, path):
    """Read completed logs in bounded serial bursts, checking every original byte."""
    quoted = shlex.quote(path)
    tag = "LAB_FETCH_" + secrets.token_hex(8)
    temporary = "/tmp/" + tag.lower() + ".gz"
    command_retries = 0

    def read_command(body, timeout=15):
        nonlocal command_retries
        for attempt in range(3):
            try:
                return guest_command(vm, body, timeout=timeout)
            except (RuntimeError, OSError) as exc:
                if (isinstance(exc, RuntimeError) and
                        not str(exc).startswith(("Guest console command timed out:", "Guest console closed"))):
                    raise
                if attempt == 2:
                    raise
                command_retries += 1
                try:
                    guest_command(vm, ":", timeout=10)
                except (RuntimeError, OSError):
                    pass

    try:
        # Create the temporary file once; only read-only commands below retry.
        guest_command(vm, f"gzip -c {quoted} > {temporary}", timeout=30)
        text = read_command(f"printf '\\n{tag}_BYTES='; wc -c < {quoted}\n"
                            f"printf '{tag}_SHA='; sha256sum {quoted}\n"
                            f"printf '{tag}_GZIP_BYTES='; wc -c < {temporary}", timeout=30)
        size_match = re.search(rf"^{tag}_BYTES=\s*(\d+)\s*$", text, re.MULTILINE)
        hash_match = re.search(rf"^{tag}_SHA=([0-9a-f]{{64}})\s", text, re.MULTILINE)
        gzip_match = re.search(rf"^{tag}_GZIP_BYTES=\s*(\d+)\s*$", text, re.MULTILINE)
        if not all((size_match, hash_match, gzip_match)):
            raise RuntimeError(f"Missing log fetch metadata for {path}")
        size, digest, compressed_size = int(size_match[1]), hash_match[1], int(gzip_match[1])
        compressed = bytearray()
        retries = 0
        for index in range((compressed_size + 4095) // 4096):
            for attempt in range(3):
                marker = "LAB_CHUNK_" + secrets.token_hex(8)
                text = read_command(f"printf '\\n{marker}\\n'\n"
                                    f"dd if={temporary} bs=4096 skip={index} count=1 2>/dev/null | uuencode -m chunk\n"
                                    f"printf '\\n{marker}_END\\n'").replace("\r", "")
                envelope = re.search(rf"^{marker}\n([\s\S]*?)^{marker}_END$", text, re.MULTILINE)
                match = (re.search(r"^begin-base64 [0-7]+ chunk\n([A-Za-z0-9+/=\n]*?)^====$",
                                   envelope[1], re.MULTILINE) if envelope else None)
                try:
                    block = base64.b64decode("".join(match[1].split()), validate=True) if match else b""
                except ValueError:
                    block = b""
                if len(block) == min(4096, compressed_size - index * 4096):
                    compressed.extend(block)
                    break
                retries += 1
            else:
                raise RuntimeError(f"Incomplete log fetch chunk {index} for {path}")
        data = gzip.decompress(compressed)
        final = read_command(f"printf '\\n{tag}_FINAL_SHA='; sha256sum {quoted}")
        final_hash = re.search(rf"^{tag}_FINAL_SHA=([0-9a-f]{{64}})\s", final, re.MULTILINE)
        if (len(data) != size or hashlib.sha256(data).hexdigest() != digest or
                not final_hash or final_hash[1] != digest):
            raise RuntimeError(f"Log changed or byte/hash verification failed for {path}")
        return data, {"path": path, "bytes": size, "sha256": digest, "gzip_bytes": compressed_size,
                      "chunks": (compressed_size + 4095) // 4096, "chunk_retries": retries,
                      "read_command_retries": command_retries,
                      "source_unchanged_during_fetch": True}
    finally:
        try:
            guest_command(vm, f"rm -f {temporary}")
        except (RuntimeError, OSError):
            pass


def shared_test(args):
    """Both L1 VMs write the same BAR offset and read the other VM's new value."""
    run, config = current_run()
    size = config["shared_mib"] * 1024 * 1024
    if args.offset < 8192 or args.offset % 4 or args.offset + 4 > size:
        raise RuntimeError("Choose a 4-byte aligned offset >= 8192 within the shared region")
    addresses, transcripts = {}, {}
    boot_report = json.loads((run / "report.json").read_text())
    for role, vm in config["vms"].items():
        shared = next(e for e in boot_report["guest_events"][role] if e["event"] == "shared_memory")
        bdf = shared["pci_bdf"]
        if not re.fullmatch(r"[0-9a-fA-F:.]+", bdf):
            raise RuntimeError("Invalid PCI BDF in report")
        text = guest_command(vm, f"sed -n '3p' /sys/bus/pci/devices/{bdf}/resource")
        ranges = [line.split() for line in text.splitlines()
                  if re.fullmatch(r"\s*0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s*", line)]
        if len(ranges) != 1:
            raise RuntimeError(f"Cannot read {role} shared BAR range:\n{text}")
        start, end, flags = (int(value, 16) for value in ranges[0])
        if not flags & 0x200 or end - start + 1 != size:
            raise RuntimeError(f"Unexpected shared BAR in VM {role}")
        addresses[role] = start + args.offset
        transcripts[f"{role}_bar"] = text

    def write_word(role, value):
        text = guest_command(config["vms"][role], f"devmem {addresses[role]:#x} 32 {value:#x}")
        transcripts[f"{role}_write"] = text

    def read_word(role):
        text = guest_command(config["vms"][role],
                             f"printf 'LAB_SHARED_WORD='\ndevmem {addresses[role]:#x} 32")
        transcripts[f"{role}_read"] = text
        match = re.search(r"^LAB_SHARED_WORD=(0x[0-9a-fA-F]+)\s*$", text, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Cannot parse VM {role} shared-memory read:\n{text}")
        return int(match[1], 16)

    def host_read():
        with Path(config["backing"]).open("rb") as stream:
            stream.seek(args.offset)
            return struct.unpack("<I", stream.read(4))[0]

    a_value = secrets.randbits(32)
    b_value = a_value ^ 0xffffffff
    write_word("A", a_value)
    b_read, host_after_a = read_word("B"), host_read()
    if b_read != a_value or host_after_a != a_value:
        raise RuntimeError("A write was not visible in B and the host backing")
    write_word("B", b_value)
    a_read, host_after_b = read_word("A"), host_read()
    if a_read != b_value or host_after_b != b_value:
        raise RuntimeError("B write was not visible in A and the host backing")
    report = {"result": "passed", "run": str(run), "offset": args.offset,
              "access": "L1 guest /dev/mem mapping of ivshmem PCI BAR2; host only reads backing",
              "guest_physical_addresses": {k: hex(v) for k, v in addresses.items()},
              "A_to_B": {"A_wrote": hex(a_value), "B_read": hex(b_read), "host_read": hex(host_after_a)},
              "B_to_A": {"B_wrote": hex(b_value), "A_read": hex(a_read), "host_read": hex(host_after_b)},
              "transcripts": transcripts}
    name = "shared-rw-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3) + ".json"
    save_json(run / name, report)
    print(json.dumps({k: v for k, v in report.items() if k != "transcripts"}, indent=2))
    print(f"Report: {run / name}")


def context_bench(args):
    """Measure pinned, blocking futex handoffs with actual switch counts."""
    if not 1000 <= args.roundtrips <= 10000000 or not 1 <= args.samples <= 100:
        raise RuntimeError("Require 1000..10000000 round trips and 1..100 samples")
    directory = LOCAL / "context-switch"
    directory.mkdir(exist_ok=True)
    out = directory / str(time.time_ns())
    out.mkdir()
    cpu = args.cpu
    config = vm = None
    if args.scope == "host":
        if cpu is None:
            cpu = next((c for c in sorted(os.sched_getaffinity(0)) if c != 0), min(os.sched_getaffinity(0)))
        binary = ASSETS / "shared-probe"
        if not binary.is_file():
            raise RuntimeError("Build the existing shared-probe with prepare first")
    else:
        _, config = current_run()
        vm = config["vms"][args.role]
        cpu = 0 if cpu is None else cpu
    report = {"result": "running", "scope": args.scope, "cpu": cpu,
              "host_kernel": os.uname().release, "roundtrips": args.roundtrips,
              "samples_per_mode": args.samples, "warmup_roundtrips_per_sample": 10000,
              "method": "two tasks pinned to the same CPU, blocking futex ping-pong; 2 handoffs per roundtrip; CLOCK_MONOTONIC_RAW",
              "limits": ["includes futex wait/wake syscalls, scheduler and loop overhead; not isolated __switch_to instructions",
                         "small hot working set; no CPU migration or large application cache working set",
                         "percentiles summarize per-trial averages, not individual-switch latency percentiles"],
              "samples": {}, "summary_us": {}}
    if config:
        report.update({"guest_role": args.role, "run": config["run"], "guest_kernel": config["prepared"]["l1_kernel_release"]})
    else:
        report["probe_sha256"] = sha256(binary)
        report["cpu_model"] = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name"))
    save_json(out / "report.json", report)
    for mode in ("thread-private", "thread-shared", "process-shared"):
        if vm:
            text = guest_command(vm, f"/lab/shared-probe context {args.role} {mode} {cpu} {args.roundtrips} {args.samples}", timeout=110)
        else:
            text = output([binary, "context", "A", mode, cpu, args.roundtrips, args.samples], timeout=120)
        (out / f"{mode}.log").write_text(text)
        rows = [json.loads(line.removeprefix("CONTEXT_JSON ")) for line in text.splitlines() if line.startswith("CONTEXT_JSON ")]
        if len(rows) != args.samples:
            raise RuntimeError("Missing context-switch samples")
        report["samples"][mode] = rows
        values = sorted(row["ns_per_handoff"] / 1000 for row in rows)
        report["summary_us"][mode] = {"median_trial_mean": statistics.median(values),
            "p95_trial_mean": values[(95 * len(values) + 99) // 100 - 1],
            "min_trial_mean": values[0], "max_trial_mean": values[-1],
            "observed_switches": sum(row["observed_switches"] for row in rows),
            "expected_handoffs": sum(row["expected_handoffs"] for row in rows)}
    report["result"] = "passed"
    save_json(out / "report.json", report)
    print(json.dumps({"result": report["result"], "report": str(out / "report.json"), "summary_us": report["summary_us"]}, indent=2))



def shared_dax_alignment(config):
    """Record the device alignment, rather than infer it from launch options."""
    result = {}
    for role, vm in config["vms"].items():
        text = guest_command(vm, "printf 'DAX_ALIGNMENT='; cat /sys/bus/dax/devices/dax0.0/align")
        match = re.search(r"^DAX_ALIGNMENT=(0x[0-9a-fA-F]+|[0-9]+)\s*$", text, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Missing device-DAX alignment on {role}")
        result[role] = int(match[1], 0) if match[1].startswith("0x") else int(match[1])
    if any(value != 4096 for value in result.values()):
        raise RuntimeError("Dense comparison requires actual 4KiB device-DAX alignment; start with --dax-4k")
    return result


def validate_dense_working_set(report, samples, sources, rounds, memory_mib):
    """Require each resumed owner to verify the same primary guest allocation."""
    sweeps = sorted((s for s in samples if s["event"] == "working_set"), key=lambda s: s["round"])
    if len(sweeps) != rounds:
        raise RuntimeError("Missing dense working-set samples")
    identities = set()
    for round_id, (source, sweep) in enumerate(zip(sources, sweeps)):
        if (sweep["round"] != round_id or source["round"] != round_id or
                sweep["role"] != ("B" if round_id % 2 == 0 else "A") or
                not 0 < sweep["working_set_bytes"] < memory_mib * 1024 * 1024 or
                sweep["working_set_bytes"] % 4096 or
                sweep["available_bytes"] < sweep["working_set_bytes"] or not sweep["instance_id"] or
                sweep["first_sweep_ns"] <= 0 or sweep["warm_sweep_ns"] <= 0 or
                any(sweep[key] for key in ("errors", "clock_errors", "tsc_errors")) or
                not (0 < source["handoff_us"] <= source["pause_to_working_set_ready_us"] <=
                     source["pause_to_warm_ready_us"])):
            raise RuntimeError(f"Dense working-set continuity/timing failed at round {round_id}")
        identities.add((sweep["instance_id"], sweep["working_set_bytes"], sweep["available_bytes"]))
    if len(identities) != 1:
        raise RuntimeError("Primary guest instance or working-set allocation changed")
    instance, working_set, available = next(iter(identities))
    completions = [s for s in samples if s["event"] == "complete"]
    if len(completions) != 2 or any(s.get("heap_bytes") != working_set for s in completions):
        raise RuntimeError("Dense completion heap size differs from retained working set")
    report["working_set_samples"] = sweeps
    report["retained_guest_heap_bytes"] = working_set
    report["working_set"] = {"bytes": working_set, "available_bytes_at_boot": available,
                             "fraction_of_memory": working_set / (memory_mib * 1024 * 1024),
                             "instance_id": instance, "scan_stride_bytes": 4096,
                             "access_bytes_per_page": 64,
                             "timing": "ACK precedes demand page scan; first and warm scans are separate; source intervals share one pause start"}
    report["validation"].update(dense_working_set_verified_every_round=True,
                                 primary_guest_instance_preserved=True,
                                 dense_scan_rounds=rounds)


def direct_handoff_bench(args):
    """Direct CXL object attach; deliberately starts with a device-free CPU guest."""
    run, config = current_run()
    collect_from = getattr(args, "collect_from", None)
    legacy_tsc = getattr(args, "legacy_tsc", False)
    if not config.get("dax_handoff"):
        raise RuntimeError("Start a fresh --dax-handoff --dax-guest --cxl-mib 2048 lab")
    rounds = args.iterations + args.warmup
    linux = args.guest == "linux"
    if not 128 <= args.memory_mib <= 2048:
        raise RuntimeError("Require 128..2048MiB guest RAM")
    if not linux and (args.memory_mib != 128 or args.dense_working_set):
        raise RuntimeError("--memory-mib overrides and --dense-working-set require --guest linux")
    layout_shift_mib = 2048 if linux and (args.memory_mib > 128 or args.dense_working_set) else 0
    if layout_shift_mib and config["cxl_mib"] < 4096:
        raise RuntimeError("Large/dense direct guests require --cxl-mib 4096 for nonoverlapping RAM, KVM, heap and telemetry")
    if args.resume_profile and not linux:
        raise RuntimeError("--resume-profile requires --guest linux")
    if args.guest_cpu is not None and not 0 <= args.guest_cpu < config["l1_vcpus"]:
        raise RuntimeError("--guest-cpu requires an available L1 CPU")
    if args.executor_cpu is not None and (not linux or not 0 <= args.executor_cpu < config["l1_vcpus"]):
        raise RuntimeError("--executor-cpu requires Linux and an available L1 CPU")
    if args.test_pfn_mismatch:
        if not linux or args.kernel_mode < 3:
            raise RuntimeError("PFN mismatch test requires --guest linux --kernel-mode 3")
        rounds = 2
    if args.test_new_pfn:
        if linux or args.kernel_mode != 3:
            raise RuntimeError("New PFN rejection test requires --guest cpu --kernel-mode 3")
        rounds = 2
    limit = 25000 if linux and args.kernel_mode >= 3 else 500
    if not 1 <= args.iterations <= limit or not 0 <= args.warmup <= 100:
        raise RuntimeError(f"Require 1..{limit} measured handoffs and 0..100 warmups")
    if rounds < 2 or rounds > limit or (linux and rounds % 2):
        raise RuntimeError(f"Require 2..{limit} handoffs; Linux mode requires an even total")
    out = collect_from or run / f"direct-{'linux-' if linux else ''}handoff-{time.time_ns()}"
    if not collect_from:
        out.mkdir()
    report = {"result": "running", "run": str(run), "iterations": args.iterations,
              "memory_mib": args.memory_mib if linux else 2, "dense_working_set": args.dense_working_set,
              "dax_layout_shift_mib": layout_shift_mib,
              "dax_layout": {"ram_offset_mib": 2, "ram_bytes": args.memory_mib * 1024 * 1024 if linux else 2 * 1024 * 1024,
                             "kvm_pool_offset_mib": 256 + layout_shift_mib, "kvm_pool_bytes": 128 * 1024 * 1024,
                             "vmm_heap_offset_mib": 512 + layout_shift_mib, "vmm_heap_bytes": 64 * 1024 * 1024,
                             "telemetry_offsets_mib": {"A": 768 + layout_shift_mib, "B": 832 + layout_shift_mib}},
              "warmup": args.warmup, "snapshot_bytes": 0, "ram_transfer_bytes": 0,
              "kernel_handoff_mode": args.kernel_mode,
              "executor": args.executor if linux else "native",
              "guest_cpu": args.guest_cpu if linux else 0,
              "poll_us": args.poll_us,
              "spin_us": args.spin_us,
              "control": args.control,
              "reuse_fds": args.reuse_fds,
              "profile": args.profile,
              "resume_profile": args.resume_profile,
              "heartbeat_spins": args.heartbeat_spins,
              "executor_cpu": args.executor_cpu,
              "irqfd_batch": args.irqfd_batch,
              "resume_nokick": args.resume_nokick,
              "channel_spin_us": args.channel_spin_us,
              "timer_slack_ns": args.timer_slack_ns,
              "telemetry": args.telemetry, "dwell_ms": args.dwell_ms,
              "test_pfn_mismatch": args.test_pfn_mismatch,
              "test_new_pfn": args.test_new_pfn,
              "heartbeat_churn": args.heartbeat_churn,
              "heartbeat_pit": args.heartbeat_pit,
              "heartbeat": args.heartbeat if linux else None,
              "guest": (f"{args.memory_mib}MiB Linux microVM cold-booted once by Firecracker on A; B executes same shared KVM objects using existing shared-probe" if linux else "2MiB real-mode CPU guest; integer register, RIP, x87 accumulator and RAM continuity"),
              "boundary": ("source pause/kick begin to observed remote Linux guest acknowledgement; configured polling/spinning included" if linux else "completed HLT exit on source to observed remote HLT completion; source guest exit cost excluded; configured polling included"),
              "limitations": ["single vCPU, identical kernel/module code addresses and CXL GPA/KVA, nokaslr",
                              ("PIC/IOAPIC/LAPIC/PIT and PV clock retained; timers/workers/cache rebound; VM logical clock freezes during handoff; no APF/steal/PMU/host-device IRQFD or block/network/vsock; source VMM remains on A" if linux else "no IRQ/PIT/pvclock/async PF/devices"),
                              ("EPT is retained with matching PFNs and node-local page metadata rebound" if args.kernel_mode >= 3 else "EPT is invalidated and rebuilt; RAM stays shared"),
                              "shared bump arena retained until outer VM shutdown; not a reusable production allocator",
                              "nested KVM/emulated shared CXL on one physical host"], "samples": {}}
    if collect_from:
        report = json.loads((out / "report.json").read_text())
        report.pop("error", None)
        report.update(result="running", collection_recovered=True)
    save_json(out / "report.json", report)

    def probe_log(role):
        if args.telemetry == "serial":
            return guest_command(config["vms"][role], "cat /run/direct.log", timeout=60)
        with open(config["cxl_backing"], "rb", buffering=0) as backing:
            data = os.pread(backing.fileno(), 64 * 1024 * 1024,
                            (768 + layout_shift_mib + (64 if role == "B" else 0)) * 1024 * 1024)
        return data.split(b"\0", 1)[0].decode()

    host_tsc_warnings = {}
    try:
        if args.dense_working_set:
            report["dax_alignment_bytes"] = shared_dax_alignment(config)
        if collect_from:
            for vm in config["vms"].values():
                guest_command(vm, 'test "$(cat /run/direct.status)" -eq 0')
        if not collect_from:
            for role, vm in config["vms"].items():
                mode, node = (1, 1) if role == "A" else (2, 2)
                text = guest_command(vm, f"""resource=$(cat /sys/bus/dax/devices/dax0.0/resource)
    echo {args.kernel_mode} > /sys/module/kvm/parameters/dax_handoff_mode
    echo {int(args.profile)} > /sys/module/kvm/parameters/dax_handoff_profile
    echo {int(args.resume_profile)} > /sys/module/kvm/parameters/dax_resume_profile
    echo {node} > /sys/module/kvm/parameters/dax_node
    echo {mode} > /sys/module/kvm/parameters/dax_shared
    printf '%x,%x\\n' "$((resource + {(256 + layout_shift_mib) * 1024 * 1024}))" 134217728 > /sys/module/kvm/parameters/dax_pool
    cat /sys/module/kvm/parameters/dax_pool""")
                (out / f"{role}-pool-setup.log").write_text(text)
            nonce = secrets.token_hex(8)
            for role, vm in config["vms"].items():
                body = ""
                if linux:
                    configuration = {"boot-source": {"kernel_image_path": "/lab/l2-vmlinux", "initrd_path": "/lab/l2-initramfs.cpio",
                        "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init lab.role=A memmap=2M$64M lab.heartbeat=1 " + f"lab.heartbeat_sleep={int(args.heartbeat == 'sleep')} lab.heartbeat_churn={int(args.heartbeat_churn)} lab.heartbeat_pit={int(args.heartbeat_pit)} lab.heartbeat_spins={args.heartbeat_spins} lab.heartbeat_timing={int(args.resume_profile)} lab.heartbeat_quiet=1 nmi_watchdog=0" + (" lab.heartbeat_dense=1 transparent_hugepage=never" if args.dense_working_set else "")},
                        "machine-config": {"vcpu_count": 1, "mem_size_mib": args.memory_mib}, "drives": []}
                    body += "cat > /run/bench-config.json <<'DIRECT_CONFIG'\n" + json.dumps(configuration) + "\nDIRECT_CONFIG\n"
                env = f"FC_DAX_DIRECT=1 FC_DAX_HEAP=/dev/dax0.0 FC_DAX_HEAP_OFFSET={(512 + layout_shift_mib) * 1024 * 1024} FC_DAX_HEAP_BYTES=67108864 " if linux else ""
                env += f"LAB_DAX_LAYOUT_SHIFT_MIB={layout_shift_mib} LAB_HEARTBEAT_DENSE={int(args.dense_working_set)} "
                if linux and args.executor == "native":
                    env += "LAB_DAX_NATIVE_A=1 "
                if args.telemetry == "dax":
                    env += "LAB_DAX_TELEMETRY=1 "
                env += f"LAB_DAX_DWELL_US={args.dwell_ms * 1000} "
                env += f"LAB_DAX_POLL_US={args.poll_us} "
                env += f"LAB_DAX_SPIN_US={args.spin_us} "
                if linux and args.control == "local":
                    env += "FC_DAX_CONTROL=/run/bench-dax.sock "
                if args.reuse_fds:
                    env += "FC_DAX_REUSE_FD=1 "
                if args.profile:
                    env += "LAB_DAX_PROFILE=1 "
                if args.resume_profile:
                    env += "LAB_DAX_RESUME_PROFILE=1 "
                if args.executor_cpu is not None:
                    env += f"LAB_DAX_EXECUTOR_CPU={args.executor_cpu} FC_DAX_VCPU_CPU={args.executor_cpu} "
                if args.irqfd_batch:
                    env += "FC_DAX_IRQFD_BATCH=1 "
                if args.resume_nokick:
                    env += "FC_DAX_NO_RESUME_KICK=1 "
                env += f"FC_DAX_CHANNEL_SPIN_US={args.channel_spin_us} LAB_DAX_STOP_SPIN_US={args.channel_spin_us} "
                if args.timer_slack_ns:
                    env += f"LAB_DAX_TIMER_SLACK_NS={args.timer_slack_ns} "
                if args.test_pfn_mismatch and role == "B":
                    env += "LAB_DAX_RAM_SHIFT=4096 "
                if args.test_new_pfn:
                    env += "LAB_DAX_NEW_PFN_TEST=1 "
                affinity = f"taskset {1 << args.guest_cpu:x} " if args.guest_cpu is not None else ""
                body += f"(set +e; {env}{affinity}/lab/shared-probe {'linux-handoff' if linux else 'direct-handoff'} {role} {rounds} {nonce} > /run/direct.log 2>&1; echo $? > /run/direct.status) &"
                guest_command(vm, body)
            deadline = time.monotonic() + 180 + rounds * (args.dwell_ms / 1000 + (args.memory_mib * .012 if args.dense_working_set else .05))
            while True:
                statuses = {}
                for role, vm in config["vms"].items():
                    text = guest_command(vm, "if [ -f /run/direct.status ]; then printf 'DIRECT_STATUS='; cat /run/direct.status; fi")
                    m = re.search(r"^DIRECT_STATUS=(\d+)\s*$", text, re.MULTILINE)
                    statuses[role] = int(m[1]) if m else None
                if args.test_new_pfn and statuses["B"] is not None:
                    diagnostic = guest_command(config["vms"]["B"], "cat /run/direct.log")
                    if statuses["B"] != 1 or "direct KVM_RUN: Invalid cross-device link" not in diagnostic:
                        raise RuntimeError(f"New non-DAX leaf was not rejected: {diagnostic}")
                    for vm in config["vms"].values():
                        text = guest_command(vm, "cat /sys/module/kvm/parameters/dax_pool\ntest \"$(cat /proc/sys/kernel/tainted)\" -eq 0")
                        if "vm_create_calls=1 vcpu_create_calls=1" not in text:
                            raise RuntimeError("Negative test recreated VM/vCPU")
                    report.update(result="passed", validation={"new_non_dax_leaf_rejected": True,
                        "errno": "EXDEV", "vm_create_calls": 1, "vcpu_create_calls": 1,
                        "kernel_taint": 0, "source_hlt_steps_before_handoff": 101})
                    print(json.dumps({"result":"passed","report":str(out / "report.json"),"validation":report["validation"]},indent=2))
                    return
                if args.test_pfn_mismatch and statuses["B"] is not None:
                    diagnostic = guest_command(config["vms"]["B"], "cat /run/direct.log")
                    if statuses["B"] != 1 or "Linux direct ATTACH: Invalid cross-device link" not in diagnostic:
                        raise RuntimeError(f"Wrong result for intentionally different RAM PFNs: {diagnostic}")
                    for vm in config["vms"].values():
                        text = guest_command(vm, "cat /sys/module/kvm/parameters/dax_pool\ntest \"$(cat /proc/sys/kernel/tainted)\" -eq 0")
                        if "state=2 owner=0" not in text or "vm_create_calls=1 vcpu_create_calls=1" not in text:
                            raise RuntimeError("Failed attach did not fail closed")
                    with open(config["cxl_backing"], "rb", buffering=0) as backing:
                        before = struct.unpack("<6Q", os.pread(backing.fileno(), 48, 66 * 1024 * 1024))
                        time.sleep(.1)
                        after = struct.unpack("<6Q", os.pread(backing.fileno(), 48, 66 * 1024 * 1024))
                    if before != after or after[2] != 1 or after[3] != 0 or after[5] != 0:
                        raise RuntimeError("Guest executed after mismatched attach")
                    report.update(result="passed", validation={"pfn_mismatch_rejected": True,
                        "errno": "EXDEV", "rejected_before_rebinding": True, "guest_remained_paused": True,
                        "vm_create_calls": 1, "vcpu_create_calls": 1, "kernel_taint": 0})
                    print(json.dumps({"result": "passed", "report": str(out / "report.json"), "validation": report["validation"]}, indent=2))
                    return
                if any(v is not None and v != 0 for v in statuses.values()):
                    raise RuntimeError(f"Direct handoff failed: {statuses}")
                if all(v == 0 for v in statuses.values()):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Direct handoff timed out: {statuses}")
                time.sleep(1)
        for role, vm in config["vms"].items():
            text = probe_log(role)
            (out / f"{role}.log").write_text(text)
            prefix = "LINUX_DIRECT_JSON " if linux else "DIRECT_JSON "
            report["samples"][role] = [json.loads(line.removeprefix(prefix)) for line in text.splitlines() if line.startswith(prefix)]
            guest_command(vm, "test ! -f /run/bench.state\ntest \"$(cat /proc/sys/kernel/tainted)\" -eq 0")
            warnings = guest_command(vm, "printf 'HOST_TSC_WARNINGS='; dmesg | grep -c 'KVM discovered backwards TSC' || true")
            match = re.search(r"^HOST_TSC_WARNINGS=(\d+)\s*$", warnings, re.MULTILINE)
            if not match:
                raise RuntimeError("Missing host TSC warning count")
            host_tsc_warnings[role] = int(match[1])
            if not legacy_tsc and host_tsc_warnings[role]:
                raise RuntimeError("Host discovered backwards TSC")
            pool = guest_command(vm, "cat /sys/module/kvm/parameters/dax_pool")
            (out / f"{role}-final-pool.log").write_text(pool)
            if not re.search(r"vm_create_calls=1 vcpu_create_calls=1", pool):
                raise RuntimeError("VM/vCPU creation was called again during handoff")
        samples = sum(report["samples"].values(), [])
        sources = sorted((s for s in samples if s["event"] == "source"), key=lambda s: s["round"])
        destinations = sorted((s for s in samples if s["event"] == "destination"), key=lambda s: s["round"])
        if len(sources) != rounds or len(destinations) != rounds:
            raise RuntimeError("Missing direct handoff samples")
        for i, (src, dst) in enumerate(zip(sources, destinations)):
            continued = dst["sequence"] > src["sequence"] if linux else dst["counter"] == src["counter"] + 1
            if src["round"] != i or dst["round"] != i or not continued:
                raise RuntimeError("Direct guest continuation failed")
        identity = {(s["vm_offset"], s["vcpu_offset"], s["vmcs_offset"]) for s in sources}
        if len(identity) != 1:
            raise RuntimeError("Shared KVM/vCPU/VMCS object identity changed")
        report["validation"] = {"handoffs": rounds, "identical_object_offsets": list(identity)[0],
                                "vm_creates": 1, "vcpu_creates": 1,
                                "continuity_checks": ("Linux heartbeat and retained dense working set" if args.dense_working_set else "Linux heartbeat and retained 16MiB heap") if linux else "integer register, RIP and x87 accumulator",
                                "kernel_taint": 0, "snapshot_api_calls": 0}
        report["validation"].update({"duplicate_attach_rejected": True, "old_owner_vcpu_access_revoked": True})
        if args.dense_working_set:
            validate_dense_working_set(report, samples, sources, rounds, args.memory_mib)
        if args.kernel_mode >= 3:
            roots = {s["ept_root_offset"] for s in sources}
            if len(roots) != 1 or not next(iter(roots)):
                raise RuntimeError("Retained EPT root identity changed")
            report["validation"]["identical_ept_root_offset"] = next(iter(roots))
            report["validation"]["ept_page_count"] = max(s["ept_page_count"] for s in sources)
        if linux:
            fc_log = guest_command(config["vms"]["A"], """printf 'FC_BOOT_COUNT='
grep -c '^L2_BOOTED ' /run/bench-fc-0.log || true
if grep -q '/snapshot/' /run/bench-fc-0.log; then printf 'FC_SNAPSHOT_USED=1\\n'; else printf 'FC_SNAPSHOT_USED=0\\n'; fi
grep -E '^(L2_BOOTED |L2_CLOCKSOURCE=)' /run/bench-fc-0.log || true""", timeout=30)
            (out / "A-firecracker-summary.log").write_text(fc_log)
            boot = re.search(r"^FC_BOOT_COUNT=(\d+)\s*$", fc_log, re.MULTILINE)
            snapshot = re.search(r"^FC_SNAPSHOT_USED=([01])\s*$", fc_log, re.MULTILINE)
            if not boot or not snapshot:
                raise RuntimeError("Missing full-log Firecracker checks")
            boot_count = int(boot[1])
            completions = [s for s in samples if s["event"] == "complete"]
            tsc_fields = ["tsc_errors" in s for s in completions]
            if len(tsc_fields) != 2 or (not legacy_tsc and not all(tsc_fields)):
                raise RuntimeError("Missing guest TSC validation")
            if legacy_tsc and any(tsc_fields):
                raise RuntimeError("Legacy TSC mode cannot be used for an instrumented image")
            if boot_count != 1 or int(snapshot[1]) or any(s.get("errors") or s.get("clock_errors") or s.get("tsc_errors") for s in samples):
                raise RuntimeError("Linux guest rebooted, snapshot API used, or heap check failed")
            report["validation"].update({"boot_markers": boot_count, "retained_heap_errors": 0})
            report["validation"].update({"guest_clock_errors": 0, "guest_tsc_errors": None if legacy_tsc else 0,
                "raw_tsc_check_enabled": not legacy_tsc,
                "host_backwards_tsc_warnings": host_tsc_warnings if legacy_tsc else 0,
                "firecracker_checks_scanned_full_log": True, "changing_memory_mib": 32 if args.heartbeat_churn else 0})
            if args.heartbeat_pit:
                with open(config["cxl_backing"], "rb", buffering=0) as backing:
                    pit_active = struct.unpack("<I", os.pread(backing.fileno(), 4, (256 + layout_shift_mib) * 1024 * 1024 + 180))[0]
                if pit_active != 1:
                    raise RuntimeError("PIT stress workload did not retain an active periodic timer")
                report["validation"]["periodic_pit_handoff"] = True
            accounting = all("controller_cpu_us" in s and "controller_wall_us" in s for s in completions)
            if not accounting and not legacy_tsc:
                raise RuntimeError("Missing controller CPU accounting")
            report["controller_accounting_available"] = accounting
            if accounting:
                report["controller_cpu_ms"] = {role: next(s["controller_cpu_us"] / 1000 for s in rows if s["event"] == "complete")
                                                for role, rows in report["samples"].items()}
                report["controller_wall_ms"] = {role: next(s["controller_wall_us"] / 1000 for s in rows if s["event"] == "complete")
                                                 for role, rows in report["samples"].items()}
            clocksource = re.search(r"^L2_CLOCKSOURCE=(\S+)", fc_log, re.MULTILINE)
            report["validation"]["guest_clocksource"] = clocksource[1] if clocksource else None
        report["summary_ms"] = {}
        def timing_applies(sample, key):
            # Zero on the other execution path means unavailable, not zero latency.
            if key in ("kick_to_exit_us", "stop_ack_us", "executor_wake_us", "wake_to_run_us"):
                return report["executor"] == "native" or sample["role"] == "B"
            if "control_service_us" in key or "control_part_" in key:
                return (report["executor"] == "firecracker" and sample["role"] == "A"
                        and report.get("control", "http") == "local")
            return True
        for group in (sources, destinations):
            for key in sorted(set().union(*(s.keys() for s in group))):
                if key.endswith("_us"):
                    values = sorted(s[key] / 1000 for s in group if s["round"] >= args.warmup
                                    and key in s and timing_applies(s, key))
                    if not values:
                        continue
                    report["summary_ms"][key[:-3]] = {"n": len(values), "p50": statistics.median(values),
                        "p95": values[(95 * len(values) + 99) // 100 - 1], "min": values[0], "max": values[-1],
                        "mean": statistics.mean(values)}
        report["result"] = "passed"
        report["direction_summary_ms"] = {}
        for role in ("A", "B"):
            rows = [s for s in sources if s["role"] == role and s["round"] >= args.warmup]
            values = sorted(s["handoff_us"] / 1000 for s in rows)
            if values:
                report["direction_summary_ms"][role + ("→B" if role == "A" else "→A")] = {
                    "n": len(values), "p50": statistics.median(values), "p95": values[(95 * len(values) + 99) // 100 - 1],
                    "mean": statistics.mean(values)}
    except BaseException as exc:
        report["result"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        for role, vm in config["vms"].items():
            try:
                (out / f"{role}-diagnostic.log").write_text(guest_command(vm, "tail -n 80 /run/direct.log 2>/dev/null || true\ntail -n 80 /run/bench-fc-0.log 2>/dev/null || true\ntail -n 80 /run/direct-linux-console.log 2>/dev/null || true\ndmesg | tail -n 100", timeout=10))
                if args.telemetry == "dax":
                    (out / f"{role}-probe.log").write_text(probe_log(role))
            except Exception as exc:
                (out / f"{role}-diagnostic-error.txt").write_text(str(exc))
        save_json(out / "report.json", report)
    print(json.dumps({"result": report["result"], "report": str(out / "report.json"), "validation": report["validation"], "summary_ms": report["summary_ms"]}, indent=2))


def direct_handoff_report(args):
    """Validate completed shared telemetry without restarting or mutating the VM."""
    run, _ = current_run()
    out = args.output_dir.resolve()
    if out.parent != run.resolve() or not out.name.startswith("direct-linux-handoff-"):
        raise RuntimeError("Select a Linux handoff report directory in the current run")
    report = json.loads((out / "report.json").read_text())
    if report["result"] != "failed" or not report.get("error", "").startswith("Guest console command timed out"):
        raise RuntimeError("Report-only recovery is limited to completed tests with serial collection timeouts")
    backup = out / "report-before-collection-recovery.json"
    if not backup.exists():
        shutil.copyfile(out / "report.json", backup)
    values = dict(guest="linux", iterations=report["iterations"], warmup=report["warmup"],
                  kernel_mode=report["kernel_handoff_mode"], collect_from=out,
                  legacy_tsc=args.legacy_tsc)
    for key, default in {"executor":"firecracker", "guest_cpu":None, "poll_us":50,
                         "spin_us":0, "control":"http",
                         "reuse_fds":False, "profile":False, "resume_profile":False, "heartbeat_spins":128,
                         "executor_cpu":None, "irqfd_batch":False, "resume_nokick":False,
                         "channel_spin_us":0,
                         "timer_slack_ns":0,
                         "telemetry":"dax", "dwell_ms":20, "heartbeat":"busy",
                         "heartbeat_churn":False, "heartbeat_pit":False,
                         "memory_mib":128, "dense_working_set":False,
                         "test_pfn_mismatch":False, "test_new_pfn":False}.items():
        values[key] = report.get(key, default)
    direct_handoff_bench(argparse.Namespace(**values))


def snapshot_cleanup_script(directory=None, include_boot_vm=False):
    """Stop recorded snapshot processes only after checking PID incarnation."""
    if directory is not None and not re.fullmatch(r"/run/snapshot-bench-[0-9a-f]{16}", directory):
        raise RuntimeError("Invalid snapshot transaction directory")
    records = (f"{directory}/controller.pid {directory}/bench-fc.pid" if directory else
               "/run/snapshot-bench-*/controller.pid /run/snapshot-bench-*/bench-fc.pid")
    script = """same_snapshot_process() {
    [ -r "/proc/$target_pid/stat" ] || return 1
    process_stat=$(cat "/proc/$target_pid/stat") || return 1
    process_fields=${process_stat##*) }
    set -- $process_fields
    [ "$#" -ge 20 ] || return 1
    process_state=$1
    shift 19
    [ "$1" = "$expected_start" ] && [ "$process_state" != Z ]
}
stop_snapshot_process() {
    if same_snapshot_process; then
        kill -TERM "$target_pid" 2>/dev/null || true
        attempt=0
        while same_snapshot_process && [ "$attempt" -lt 100 ]; do
            sleep .02
            attempt=$((attempt + 1))
        done
        if same_snapshot_process; then kill -KILL "$target_pid" 2>/dev/null || true; fi
        attempt=0
        while same_snapshot_process && [ "$attempt" -lt 100 ]; do
            sleep .02
            attempt=$((attempt + 1))
        done
        if same_snapshot_process; then
            echo "Snapshot process did not exit: $target_pid" >&2
            return 1
        fi
    fi
}
"""
    script += f"""for pid_record in {records}; do
    [ -f "$pid_record" ] || continue
    read target_pid expected_start < "$pid_record"
    case "$target_pid:$expected_start" in *[!0-9:]*|:|*:) continue ;; esac
    [ "$target_pid" -gt 1 ] || continue
    stop_snapshot_process
done
"""
    if include_boot_vm:
        script += """if [ -f /run/firecracker.pid ]; then
    target_pid=$(cat /run/firecracker.pid)
    case "$target_pid" in ''|*[!0-9]*) target_pid=0 ;; esac
    if [ "$target_pid" -gt 1 ] && [ -r "/proc/$target_pid/cmdline" ]; then
        process_command=$(tr '\\000' ' ' < "/proc/$target_pid/cmdline")
        case "$process_command" in '/lab/firecracker --no-api --config-file /run/firecracker.json '*)
            process_stat=$(cat "/proc/$target_pid/stat")
            process_fields=${process_stat##*) }
            set -- $process_fields
            shift 19
            expected_start=$1
            stop_snapshot_process
            ;;
        esac
    fi
fi
"""
    return script


def configure(args):
    """Inspect or explicitly configure KVM only inside the selected semantic pair."""
    run, config = current_run()
    if (not (config.get("dax_guest") or config.get("migration_network")) or config.get("dax_kvm_state") or
            config.get("dax_handoff") or set(config["vms"]) != {"A", "B"} or
            not all(alive(vm) for vm in config["vms"].values())):
        raise RuntimeError("configure requires two running native-KVM --dax-guest or --migration-network lab VMs")
    vendor = config["prepared"]["vendor"]
    if vendor not in ("intel", "amd"):
        raise RuntimeError("Unsupported lab KVM vendor")
    module = "kvm-intel" if vendor == "intel" else "kvm-amd"
    module_name = module.replace("-", "_")
    pml = getattr(args, "pml", None)
    if pml is not None and vendor != "intel":
        raise RuntimeError("PML requires Intel KVM")
    inspection = """printf 'NX_HUGE_PAGES='; cat /sys/module/kvm/parameters/nx_huge_pages
printf 'RCU_EXPEDITED='; cat /sys/kernel/rcu_expedited
printf 'RCU_NORMAL='; cat /sys/kernel/rcu_normal
printf 'KVM_DAX_POOL='; cat /sys/module/kvm/parameters/dax_pool
printf 'TAINT='; cat /proc/sys/kernel/tainted
"""
    if vendor == "intel":
        inspection += "printf 'PML='; cat /sys/module/kvm_intel/parameters/pml\n"
    # Check both guests before stopping a recorded microVM or changing any setting.
    preflight = "test -r /sys/module/kvm/parameters/nx_huge_pages\n"
    if args.nx_huge_pages is not None or pml is not None:
        preflight += f"test -r /modules/kvm.ko\ntest -r /modules/{module}.ko\n"
    if args.rcu is not None:
        preflight += "test -w /sys/kernel/rcu_expedited\n"
        preflight += "test \"$(cat /sys/kernel/rcu_normal)\" = 0\n"
    for vm in config["vms"].values():
        guest_command(vm, preflight)
    script = ""
    if args.nx_huge_pages is not None or pml is not None:
        script += snapshot_cleanup_script(include_boot_vm=True)
        script += "cache_nx_policy=$(cat /sys/module/kvm/parameters/nx_huge_pages)\n"
        if vendor == "intel":
            script += "cache_pml_policy=$(cat /sys/module/kvm_intel/parameters/pml)\n"
        for name in (module_name, "kvm"):
            script += f"""attempt=0
while [ -d /sys/module/{name} ]; do
    if rmmod {name}; then break; fi
    attempt=$((attempt + 1))
    [ "$attempt" -lt 50 ] || exit 1
    sleep .1
done
"""
        nx_policy = args.nx_huge_pages if args.nx_huge_pages is not None else '"$cache_nx_policy"'
        script += f"insmod /modules/kvm.ko nx_huge_pages={nx_policy}\n"
        pml_argument = f" pml={int(pml == 'on')}" if pml is not None else (' pml="$cache_pml_policy"' if vendor == "intel" else "")
        script += f"insmod /modules/{module}.ko{pml_argument}\n"
    if args.rcu is not None:
        script += f"echo {int(args.rcu == 'expedited')} > /sys/kernel/rcu_expedited\n"
    report_path = run / f"configuration-{time.time_ns()}.json"
    report = {"result": "running", "scope": "selected L1 guests only; physical host unchanged",
              "requested": {"nx_huge_pages": args.nx_huge_pages, "rcu": args.rcu, "pml": pml},
              "read_only": not script, "transcripts": {}}
    try:
        for role, vm in config["vms"].items():
            report["transcripts"][role] = guest_command(vm, script + inspection, timeout=30)
        report["result"] = "passed"
        config["runtime_configuration"] = {"report": str(report_path), **report}
        save_json(run / "run.json", config)
    except BaseException as exc:
        report.update(result="failed", error=str(exc))
        raise
    finally:
        save_json(report_path, report)
    for role, transcript in report["transcripts"].items():
        print(f"VM {role}:\n{transcript}")
    print(f"Configuration report: {report_path}")


def cache_bench(args):
    """Measure cache maintenance with real KVM guest writes and epoch bitmaps."""
    if not 128 <= args.memory_mib <= 2048 or args.memory_mib % 128:
        raise RuntimeError("Require 128..2048MiB RAM in multiples of 128MiB")
    if args.cpu < 0 or not 0 <= args.dirty_permyriad <= 10000 or not 2 <= args.iterations <= 500 or args.iterations % 2:
        raise RuntimeError("Require dirty fraction 0..10000 and an even 2..500 iterations")
    if not 0 <= args.warmup <= 100 or args.warmup % 2:
        raise RuntimeError("Require an even 0..100 warmup iterations")
    if args.mode == "dirty" and args.dirty_log == "off":
        raise RuntimeError("Selective maintenance requires actual KVM dirty logging")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise RuntimeError("Require a finite positive timeout")
    if args.scope == "guest":
        run, config = current_run()
        if (not config.get("dax_guest") or not config.get("dax_4k") or
                not config.get("migration_network") or config.get("dax_handoff") or
                config.get("dax_kvm_state") or set(config["vms"]) != {"A", "B"} or
                not all(alive(vm) for vm in config["vms"].values())):
            raise RuntimeError("Use two running --dax-guest --dax-4k --migration-network VMs with local KVM objects")
        if config["cxl_mib"] < args.memory_mib + 2 or args.cpu >= config["l1_vcpus"]:
            raise RuntimeError("DAX must hold RAM + 2MiB, and the selected L1 CPU must exist")
        network = config["migration_network"]
    else:
        host_kvm()
        run = LOCAL / "cache-host"
        run.mkdir(exist_ok=True, parents=True)
        config = None
        network = {"backend": "loopback", "ips": {"A": "127.0.0.1", "B": "127.0.0.1"}}
        allowed = os.sched_getaffinity(0)
        if not {args.host_cpu_a, args.host_cpu_b} <= allowed or args.host_cpu_a == args.host_cpu_b:
            raise RuntimeError("Select two distinct allowed host CPUs")
    rounds = args.warmup + args.iterations
    out = run / f"cache-{args.mode}-{args.memory_mib}m-d{args.dirty_permyriad}-w{args.write_bytes}-{time.time_ns()}"
    out.mkdir()
    nonce = secrets.token_hex(8)
    transaction = f"/run/cache-bench-{nonce}"
    port = 20000 + secrets.randbelow(20000)
    report = {"result": "running", "output": str(out), "scope": args.scope, "nonce": nonce,
              "ram_backing": "device-DAX" if config else "shared memfd",
              "memory_mib": args.memory_mib, "dirty_permyriad": args.dirty_permyriad,
              "write_bytes_per_dirty_page": args.write_bytes, "mode": args.mode,
              "dirty_logging": args.dirty_log, "iterations": args.iterations, "warmup": args.warmup,
              "require_pml": args.require_pml,
              "rounds": rounds, "network": network, "cpu": args.cpu,
              "run": str(run), "host_cpus": {"A": args.host_cpu_a, "B": args.host_cpu_b} if not config else None,
              "assets": str(config["assets"] if config else ASSETS),
              "probe_sha256": (config["prepared"]["artifacts"]["shared-probe"] if config else sha256(ASSETS / "shared-probe")),
              "runtime_configuration": config.get("runtime_configuration") if config else None,
              "qemu_backing_directory": config.get("ephemeral_memory_dir") if config else None,
              "host_kernel": os.uname().release, "samples": [], "summary_ms": {},
              "semantics": "alternating writers; epoch bitmap over TCP; CLWB/SFENCE writer and CLFLUSHOPT/MFENCE reader; no guest RAM copy",
              "timing": "local CLOCK_MONOTONIC durations; source KVM_RUN/HLT completion to reader-maintenance ACK uses only source clock; guest run/stop is outside this pause interval; verification is separate",
              "limitations": ["cache protocol microbenchmark, separate from Firecracker state/direct handoff",
                              "one physical coherent host; does not reproduce stale caches of a physical noncoherent CXL fabric",
                              "flush byte counts denote address ranges, not actual writeback traffic",
                              "guest KVM_RUN includes VMEXIT and PML draining; GET_DIRTY_LOG is bitmap harvest, not pure PML drain",
                              "nested PML is emulated" if config else "native KVM/PML control uses shared memfd RAM, not device-DAX",
                              "dirty logging on/off includes tracking-induced translation state changes; it is not pure hardware log-instruction cost"]}
    save_json(out / "report.json", report)
    processes = {}
    streams = []
    backing_fd = None
    cleanup = f"""if [ -f {transaction}/probe.pid ]; then
read cache_pid cache_start < {transaction}/probe.pid
case "$cache_pid" in ''|*[!0-9]*) exit 1 ;; esac
case "$cache_start" in ''|*[!0-9]*) exit 1 ;; esac
[ "$cache_pid" -gt 1 ] || exit 1
cache_alive() {{ [ -r /proc/$cache_pid/stat ] && [ "$(awk '{{print $22}}' /proc/$cache_pid/stat)" = "$cache_start" ]; }}
if cache_alive; then
    kill "$cache_pid" 2>/dev/null || true
    cache_attempt=0
    while cache_alive && [ "$cache_attempt" -lt 30 ]; do sleep .1; cache_attempt=$((cache_attempt + 1)); done
    if cache_alive; then kill -9 "$cache_pid"; fi
    cache_attempt=0
    while cache_alive && [ "$cache_attempt" -lt 30 ]; do sleep .1; cache_attempt=$((cache_attempt + 1)); done
    if cache_alive; then exit 1; fi
fi
fi
"""
    try:
        if config:
            for vm in config["vms"].values():
                guest_command(vm, snapshot_cleanup_script(include_boot_vm=True) +
                              f"\ntaskset -p 1 $$\nmkdir -m 700 {transaction}", timeout=30)
                if args.require_pml and args.dirty_log == "on":
                    guest_command(vm, 'test "$(cat /sys/module/kvm_intel/parameters/pml)" = Y\n'
                                  'test "$(cat /sys/module/kvm_intel/parameters/eptad)" = Y', timeout=15)
        else:
            if args.require_pml and args.dirty_log == "on":
                if any(Path(f"/sys/module/kvm_intel/parameters/{name}").read_text().strip() != "Y"
                       for name in ("pml", "eptad")):
                    raise RuntimeError("Native KVM PML/EPT A/D is unavailable")
            backing_fd = os.memfd_create("cxl-cache-native", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            os.ftruncate(backing_fd, (args.memory_mib + 2) * 1024 * 1024)
            fcntl.fcntl(backing_fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW)
        for role in ("B", "A"):
            peer = network["ips"]["A" if role == "B" else "B"]
            argv = ["cache-bench", role, "DAX", peer, str(port), str(args.memory_mib),
                    str(args.dirty_permyriad), str(args.write_bytes), str(rounds), nonce, args.mode]
            if config:
                argv[2] = '"$(cat /run/cxl-dax-device)"'
                invocation = " ".join(argv)
                script = (f"(set +e\nCACHE_BENCH_DIRTY_LOG={int(args.dirty_log == 'on')} "
                          f"taskset {1 << args.cpu:x} /lab/shared-probe {invocation} > {transaction}/bench.log 2>&1 &\n"
                          "cache_pid=$!\n"
                          f"printf '%s %s\\n' \"$cache_pid\" \"$(awk '{{print $22}}' /proc/$cache_pid/stat)\" > {transaction}/probe.pid\n"
                          f"wait \"$cache_pid\"\nprintf '%s\\n' \"$?\" > {transaction}/bench.status\n) </dev/null &")
                guest_command(config["vms"][role], script, timeout=20)
            else:
                argv[2] = f"/proc/self/fd/{backing_fd}"
                probe = ASSETS / "shared-probe"
                if not probe.is_file():
                    raise RuntimeError("Run prepare with the selected assets before host cache-bench")
                env = {**os.environ, "CACHE_BENCH_DIRTY_LOG": str(int(args.dirty_log == "on")),
                       "CACHE_BENCH_ALLOW_FILE": "1"}
                cpu = args.host_cpu_a if role == "A" else args.host_cpu_b
                stream = (out / f"{role}.log").open("wb")
                streams.append(stream)
                processes[role] = subprocess.Popen(["taskset", "-c", str(cpu), str(probe), *argv], env=env,
                                                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                                    pass_fds=(backing_fd,))
        deadline = time.monotonic() + args.timeout
        while True:
            statuses = {}
            if config:
                # The console shell is pinned to CPU0; the probe runs on its selected CPU.
                for role, vm in config["vms"].items():
                    result = guest_command(vm, f"if [ -f {transaction}/bench.status ]; then printf 'CACHE_STATUS='; cat {transaction}/bench.status; fi", timeout=30)
                    match = re.search(r"CACHE_STATUS=(\d+)", result)
                    statuses[role] = int(match[1]) if match else None
            else:
                statuses = {role: process.poll() for role, process in processes.items()}
            if any(value is not None and value != 0 for value in statuses.values()):
                raise RuntimeError(f"Cache probe failed: {statuses}")
            if all(value == 0 for value in statuses.values()):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Cache probe timed out: {statuses}")
            time.sleep(1)
        for role in ("A", "B"):
            if config:
                data, metadata = guest_fetch_file(config["vms"][role], f"{transaction}/bench.log")
                (out / f"{role}.log").write_bytes(data)
                report.setdefault("artifact_integrity", {})[role] = metadata
            text_log = (out / f"{role}.log").read_text()
            for line in text_log.splitlines():
                if line.startswith("CACHE_BENCH_JSON "):
                    row = json.loads(line.removeprefix("CACHE_BENCH_JSON "))
                    if row.get("role") != role:
                        raise RuntimeError("Cache probe role mismatch")
                    report["samples"].append(row)
        starts = [s for s in report["samples"] if s["event"] == "start"]
        expected_pages = args.memory_mib * 256
        expected_dirty = (expected_pages * args.dirty_permyriad + 9999) // 10000
        expected_maintained = expected_pages if args.mode == "full" else expected_dirty if args.mode == "dirty" else 0
        if len(starts) != 2 or {s["role"] for s in starts} != {"A", "B"}:
            raise RuntimeError("Missing or duplicate cache probe start records")
        for row in report["samples"]:
            if row.get("nonce") != nonce or row.get("errors", 0) != 0:
                raise RuntimeError("Cache probe nonce mismatch or data validation error")
            if row["event"] in ("start", "source", "destination"):
                if (row.get("memory_mib") != args.memory_mib or row.get("mode") != args.mode or
                        row.get("logging_enabled") != (args.dirty_log == "on") or
                        row.get("bitmap_bytes") != expected_pages // 8):
                    raise RuntimeError("Cache probe configuration mismatch")
            if row["event"] in ("source", "destination"):
                if row.get("dirty_pages") != expected_dirty or row.get("maintained_pages") != expected_maintained:
                    raise RuntimeError("Cache probe dirty/maintenance page counts mismatch")
            if args.require_pml and args.dirty_log == "on" and row.get("module_pml") is not True:
                if row["event"] in ("start", "source", "destination"):
                    raise RuntimeError("Requested PML silently disabled")
        for event in ("source", "destination"):
            rows = sorted((s for s in report["samples"] if s["event"] == event), key=lambda s: s["round"])
            if len(rows) != rounds or [s["round"] for s in rows] != list(range(rounds)):
                raise RuntimeError(f"Missing or duplicate {event} rounds")
            for row in rows:
                source = "A" if row["round"] % 2 == 0 else "B"
                if row["role"] != (source if event == "source" else ("B" if source == "A" else "A")):
                    raise RuntimeError("Cache probe direction mismatch")
            selected = [s for s in rows if s["round"] >= args.warmup]
            for key in selected[0]:
                if key.endswith("_ns") and all(isinstance(s.get(key), (int, float)) for s in selected):
                    values = sorted(s[key] / 1e6 for s in selected)
                    report["summary_ms"][event + "." + key.removesuffix("_ns")] = {
                        "n": len(values), "mean": statistics.mean(values), "p50": statistics.median(values),
                        "p95": values[(95 * len(values) + 99) // 100 - 1], "min": values[0], "max": values[-1]}
        completed = [s for s in report["samples"] if s["event"] == "complete"]
        if (len(completed) != 2 or {s["role"] for s in completed} != {"A", "B"} or
                any(s.get("result") != "passed" or s.get("rounds") != rounds for s in completed)):
            raise RuntimeError("Cache probe did not verify all epochs on both nodes")
        for r in range(rounds):
            pair = [s for s in report["samples"] if s["event"] in ("source", "destination") and s["round"] == r]
            if pair[0]["bitmap_hash"] != pair[1]["bitmap_hash"]:
                raise RuntimeError("Source/destination bitmap hashes differ")
        report["validation"] = {"epochs": rounds, "data_errors": 0, "pages_verified_per_epoch": expected_pages,
                                "dirty_pages_per_epoch": expected_dirty, "maintained_pages_per_node": expected_maintained,
                                "cacheline_commands_per_node": expected_maintained * 64,
                                "address_range_bytes_per_node": expected_maintained * 4096,
                                "guest_ram_transfer_bytes": 0, "bitmap_bytes_per_epoch": expected_pages // 8,
                                "one_kvm_vm_and_vcpu_per_node": all(s["vm_creates"] == s["vcpu_creates"] == 1 for s in starts)}
        report["result"] = "passed"
    except BaseException as exc:
        report.update(result="failed", error=str(exc))
        if config:
            for role, vm in config["vms"].items():
                try:
                    data, _ = guest_fetch_file(vm, f"{transaction}/bench.log")
                    (out / f"{role}.log").write_bytes(data)
                except (OSError, RuntimeError):
                    pass
        raise
    finally:
        if config:
            for role, vm in config["vms"].items():
                try:
                    guest_command(vm, cleanup, timeout=20)
                    report.setdefault("cleanup", {})[role] = "recorded probe exited"
                except (OSError, RuntimeError) as exc:
                    report.setdefault("cleanup_errors", {})[role] = str(exc)
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in streams:
            stream.close()
        if backing_fd is not None:
            os.close(backing_fd)
        if report.get("cleanup_errors"):
            report.update(result="failed", error="Probe cleanup did not complete")
        save_json(out / "report.json", report)
    if report["result"] != "passed":
        raise RuntimeError(report.get("error", "Cache benchmark failed"))
    print(json.dumps({"result": report["result"], "report": str(out / "report.json"),
                      "summary_ms": report["summary_ms"]}, indent=2))


def migration_ept_functions(detail=False):
    functions = {"pf": "kvm_mmu_page_fault", "root": "kvm_mmu_load"}
    if detail:
        functions.update(map="kvm_tdp_mmu_map", pfn="kvm_faultin_pfn",
                         cache="mmu_topup_memory_caches", root_alloc="kvm_tdp_mmu_get_vcpu_root_hpa")
    return functions


def migration_ept_summary(trace_text, stats_text="", detail=False):
    """Summarize diagnostic L1 EPT traces; never add nested path times together.

    Baseline latency must come from untraced runs. Trace path durations include
    probe overhead, scheduling, host page faults, and all called functions.
    """
    import collections
    import re
    import statistics
    kinds = tuple(migration_ept_functions(detail))
    expected_probes = {kind + edge for kind in kinds for edge in ("_begin", "_end")}

    line_re = re.compile(
        r"^.*?-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+)\.(\d+):\s+(\w+):\s*(.*)$"
    )
    marker_re = re.compile(r"MIGRATION_EPT_(BEGIN|ACK|SCAN1|SCAN2)\b(.*)")
    groups, active = [], None
    unmatched_returns = 0
    for line in trace_text.splitlines():
        match = line_re.match(line.strip())
        if not match:
            continue
        pid, cpu, sec, frac, event, text = match.groups()
        ns = int(sec) * 1_000_000_000 + int((frac + "000000000")[:9])
        marker = marker_re.search(text) if event == "tracing_mark_write" else None
        if marker:
            phase, fields = marker.groups()
            if phase == "BEGIN":
                active = {
                    "identity": dict(re.findall(r"(\w+)=([^\s]+)", fields)),
                    "markers_ns": {}, "events": [], "intervals": [],
                    "stacks": collections.defaultdict(list),
                }
                groups.append(active)
            if active is not None:
                active["markers_ns"][phase] = ns
            if phase == "SCAN2":
                active = None
            continue
        if active is None:
            continue
        if event in expected_probes and event.endswith("_begin"):
            kind = event.removesuffix("_begin")
            active["stacks"][(kind, int(pid))].append(ns)
            active["events"].append((ns, event, text, int(pid), int(cpu)))
        elif event in expected_probes and event.endswith("_end"):
            kind = event.removesuffix("_end")
            stack = active["stacks"][(kind, int(pid))]
            if stack:
                active["intervals"].append((kind, stack.pop(), ns, int(pid)))
            else:
                unmatched_returns += 1
        elif event in ("kvm_mmu_set_spte", "kvm_mmu_get_page"):
            active["events"].append((ns, event, text, int(pid), int(cpu)))

    losses = []
    for match in re.finditer(
        r"^\s*(overrun|commit overrun|dropped events):\s*(\d+)\s*$",
        stats_text, re.MULTILINE,
    ):
        if int(match[2]):
            losses.append({"field": match[1], "count": int(match[2])})
    probe_stats = {}
    for match in re.finditer(
        r"^\s*([a-z_]+_(?:begin|end))\s+(\d+)\s+(\d+)\s*$",
        stats_text, re.MULTILINE,
    ):
        if match[1] not in expected_probes:
            continue
        probe_stats[match[1]] = {"nhit": int(match[2]), "nmissed": int(match[3])}
        if int(match[3]):
            losses.append({"probe": match[1], "nmissed": int(match[3])})
    header = re.search(r"entries-in-buffer/entries-written:\s*(\d+)/(\d+)", trace_text)
    if header and header[1] != header[2]:
        losses.append({"entries_in_buffer": int(header[1]), "entries_written": int(header[2])})

    def window(group, begin, end):
        lo, hi = group["markers_ns"][begin], group["markers_ns"][end]
        counts, leaves, unique_leaves, pages = collections.Counter(), collections.Counter(), set(), collections.Counter()
        pids, cpus = set(), set()
        for ns, event, text, pid, cpu in group["events"]:
            if not lo <= ns < hi:
                continue
            counts[event] += 1
            pids.add(pid)
            cpus.add(cpu)
            if event == "kvm_mmu_set_spte":
                level = re.search(r"\blevel ([1-5])\b", text)
                gfn = re.search(r"\bgfn ([0-9a-f]+)\b", text)
                if level:
                    leaves[level[1]] += 1
                    if gfn:
                        unique_leaves.add((level[1], int(gfn[1], 16)))
            elif event == "kvm_mmu_get_page" and text.replace("\x00", "").strip().endswith("new"):
                level = re.search(r"\bl([1-5])\b", text)
                pages[level[1] if level else "unknown"] += 1
        paths = {}
        for kind in kinds:
            intervals = [(max(a, lo), min(b, hi)) for k, a, b, _ in group["intervals"]
                         if k == kind and b > lo and a < hi]
            lengths = [(b - a) / 1000.0 for a, b in intervals]
            merged_ns, end_ns = 0, -1
            for a, b in sorted(intervals):
                merged_ns += max(0, b - max(a, end_ns))
                end_ns = max(end_ns, b)
            paths[kind] = {
                "completed_intervals": len(lengths),
                "inclusive_path_sum_us": sum(lengths),
                "wall_union_us": merged_ns / 1000.0,
                "median_path_us": statistics.median(lengths) if lengths else None,
                "max_path_us": max(lengths) if lengths else None,
            }
        distinct = collections.Counter(level for level, _ in unique_leaves)
        return {
            "wall_us": (hi - lo) / 1000.0,
            "events": dict(counts),
            "leaf_installs_by_level": dict(leaves),
            "unique_leaf_gfns_by_level": dict(distinct),
            "new_mmu_pages_by_level": dict(pages),
            "paths": paths,
            "pids": sorted(pids), "cpus": sorted(cpus),
        }

    rounds = []
    for group in groups:
        missing = sorted(set(("BEGIN", "ACK", "SCAN1", "SCAN2")) - group["markers_ns"].keys())
        dangling = sum(len(stack) for stack in group["stacks"].values())
        row = {"identity": group["identity"], "missing_markers": missing,
               "unmatched_entries": dangling}
        if not missing:
            row["windows"] = {
                "resume_to_ack": window(group, "BEGIN", "ACK"),
                "ack_to_scan1": window(group, "ACK", "SCAN1"),
                "scan1_to_scan2": window(group, "SCAN1", "SCAN2"),
                "resume_to_scan1": window(group, "BEGIN", "SCAN1"),
                "resume_to_scan2": window(group, "BEGIN", "SCAN2"),
            }
        rounds.append(row)
    missing_probes = sorted(expected_probes - probe_stats.keys())
    missing_hits = [kind for kind in ("map", "pfn", "cache") if detail and
                    any(not probe_stats.get(kind + edge, {}).get("nhit") for edge in ("_begin", "_end"))]
    return {
        "valid": bool(rounds) and not missing_probes and not missing_hits and not losses and not unmatched_returns
                 and all(not row["missing_markers"] and not row["unmatched_entries"] for row in rounds),
        "losses": losses, "probe_stats": probe_stats, "missing_probes": missing_probes,
        "missing_required_hits": missing_hits, "functions": migration_ept_functions(detail),
        "unmatched_returns": unmatched_returns, "rounds": rounds,
        "interpretation": (
            "L1 KVM EPT/MMU diagnostic only. Level 1=4KiB, 2=2MiB, 3=1GiB. "
            "Counts are mapping events, not guest bytes; unique GFNs are per window. "
            "Fault paths include backing-page faults, MMU work, preemption and trace overhead; "
            "All paths are inclusive and can nest, so do not sum scopes. "
            "Text timestamps are typically rounded to 1us. Hardware nested L0 EPT work is not separately timed."
        ),
    }


def migration_trace_fetch(vm, config, path):
    """Export completed diagnostic traces through unused ivshmem, after timing."""
    offset = 4 * 1024 * 1024
    capacity = config["shared_mib"] * 1024 * 1024 - offset
    if capacity <= 0:
        return guest_fetch_file(vm, path)
    event = next((event for event in events(Path(vm["serial_log"]))
                  if event.get("event") == "shared_memory" and event.get("nonce") == config["nonce"]), None)
    bdf = event.get("pci_bdf", "") if event else ""
    if not re.fullmatch(r"[0-9a-fA-F:.]+", bdf):
        raise RuntimeError("Missing validated ivshmem BAR for diagnostic export")
    resource = guest_command(vm, f"sed -n '3p' /sys/bus/pci/devices/{bdf}/resource")
    ranges = [line.split() for line in resource.splitlines()
              if re.fullmatch(r"\s*0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s*", line)]
    if len(ranges) != 1:
        raise RuntimeError("Cannot read diagnostic export BAR")
    start, end, flags = (int(value, 16) for value in ranges[0])
    if not flags & 0x200 or end - start + 1 != config["shared_mib"] * 1024 * 1024 or start % 4096:
        raise RuntimeError("Invalid diagnostic export BAR range")
    tag = "MIGRATION_EXPORT_" + secrets.token_hex(8)
    temporary = "/tmp/" + tag.lower() + ".gz"
    quoted = shlex.quote(path)
    try:
        metadata = guest_command(vm, f"gzip -c {quoted} > {temporary}\n"
                                 f"printf '\\n{tag}_BYTES='; wc -c < {quoted}\n"
                                 f"printf '{tag}_SHA='; sha256sum {quoted}\n"
                                 f"printf '{tag}_GZIP_BYTES='; wc -c < {temporary}\n"
                                 f"printf '{tag}_GZIP_SHA='; sha256sum {temporary}", timeout=60)
        values = {}
        for name, pattern in (("BYTES", r"\s*(\d+)\s*$"), ("SHA", r"([0-9a-f]{64})\s"),
                              ("GZIP_BYTES", r"\s*(\d+)\s*$"), ("GZIP_SHA", r"([0-9a-f]{64})\s")):
            match = re.search(rf"^{tag}_{name}={pattern}", metadata, re.MULTILINE)
            if not match:
                raise RuntimeError("Missing diagnostic export integrity metadata")
            values[name] = match[1]
        size, compressed_size = int(values["BYTES"]), int(values["GZIP_BYTES"])
        if compressed_size > capacity:
            return guest_fetch_file(vm, path)
        guest_command(vm, f"dd if={temporary} of=/dev/mem bs=4096 seek={(start + offset) // 4096} conv=notrunc", timeout=60)
        with Path(config["backing"]).open("rb") as stream:
            stream.seek(offset)
            compressed = stream.read(compressed_size)
        if len(compressed) != compressed_size or hashlib.sha256(compressed).hexdigest() != values["GZIP_SHA"]:
            raise RuntimeError("Diagnostic ivshmem export compressed bytes/hash mismatch")
        data = gzip.decompress(compressed)
        if len(data) != size or hashlib.sha256(data).hexdigest() != values["SHA"]:
            raise RuntimeError("Diagnostic ivshmem export original bytes/hash mismatch")
        return data, {"path": path, "bytes": size, "sha256": values["SHA"], "gzip_bytes": compressed_size,
                      "gzip_sha256": values["GZIP_SHA"], "ivshmem_offset": offset,
                      "transport": "post-migration diagnostic export only; experimental guest RAM travels over TCP"}
    finally:
        try:
            guest_command(vm, f"rm -f {temporary}")
        except (RuntimeError, OSError):
            pass


def migration_trace_script(enable, detail=False):
    """Optional diagnostic probes; never enabled in the primary timing run."""
    probes = {}
    for kind, function in migration_ept_functions(detail).items():
        arguments = " gpa=$arg2:x64 error=$arg3:x64" if kind == "pf" else ""
        probes[kind + "_begin"] = f"p:migration/{kind}_begin kvm:{function}{arguments}"
        result = "x64" if kind == "root_alloc" else "s32"
        probes[kind + "_end"] = f"r4096:migration/{kind}_end kvm:{function} ret=$retval:{result}"
    events = ["migration/" + name for name in probes] + ["kvmmmu/kvm_mmu_set_spte", "kvmmmu/kvm_mmu_get_page"]
    script = "mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true\nT=/sys/kernel/tracing\n"
    if not enable:
        script += "if [ ! -w $T/tracing_on ]; then exit 0; fi\n"
    script += "echo 0 > $T/tracing_on\n"
    if enable:
        buffer_kib = 524288 if detail else 262144
        script += f"echo nop > $T/current_tracer\necho mono > $T/trace_clock\necho {buffer_kib} > $T/buffer_size_kb\necho > $T/trace\n"
        for name, probe in probes.items():
            script += f"if [ ! -d $T/events/migration/{name} ]; then printf '%s\\n' {shlex.quote(probe)} >> $T/kprobe_events; fi\n"
    for event in events:
        if enable:
            value = int(not detail or event.startswith("migration/"))
            script += f"echo {value} > $T/events/{event}/enable\n"
        else:
            script += f"if [ -w $T/events/{event}/enable ]; then echo 0 > $T/events/{event}/enable; fi\n"
    if not enable:
        for name in probes:
            script += f"if [ -d $T/events/migration/{name} ]; then printf '%s\\n' '-:migration/{name}' >> $T/kprobe_events; fi\n"
    return script


def migration_bench(args):
    """Full RAM snapshot migration over the private L1-to-L1 TCP link."""
    detail = getattr(args, "trace_ept_detail", False)
    args.trace_ept = args.trace_ept or detail
    run, config = current_run()
    if (not config.get("migration_network") or config.get("dax_kvm_state") or config.get("dax_handoff") or
            set(config["vms"]) != {"A", "B"} or not all(alive(vm) for vm in config["vms"].values())):
        raise RuntimeError("migration-bench requires two running --migration-network L1 VMs with private KVM objects")
    if not 128 <= args.memory_mib <= 2048 or not 1 <= args.iterations <= 500 or not 0 <= args.warmup <= 100:
        raise RuntimeError("Require 128..2048MiB RAM, 1..500 iterations, and 0..100 warmups")
    if config["l1_vcpus"] < 2 or config["l1_memory_mib"] < args.memory_mib * 4 + 512:
        raise RuntimeError("Use at least two L1 CPUs and 4 * microVM RAM + 512MiB L1 memory")
    rounds = args.iterations + args.warmup
    out = run / f"migration-{args.memory_mib}m-{time.time_ns()}"
    out.mkdir()
    nonce = secrets.token_hex(8)
    transaction = f"/run/snapshot-bench-{nonce}"
    port = 19000 + secrets.randbelow(20000)
    report = {"result": "running", "run": str(run), "output": str(out), "nonce": nonce,
              "memory_mib": args.memory_mib, "vcpus": 1, "iterations": args.iterations, "warmup": args.warmup,
              "guest_transaction_directory": transaction, "migration_port": port,
              "network": config["migration_network"], "runtime_configuration": config.get("runtime_configuration"),
              "profile_restore": args.profile_restore, "trace_ept": args.trace_ept,
              "trace_ept_detail": detail,
              "cpu_restore": "normal semantic CPU snapshot with explicit FC_EXACT_TSC=1 public KVM TSC-offset restoration and retention of the incoming ordered MSR save list",
              "transport": "full ordinary guest RAM and semantic snapshot over TCP between independent L1 Linux kernels",
              "ram_backing": "initial anonymous RAM; subsequent private mappings of local snapshot files; independent destination tmpfs file; no shared DAX RAM",
              "working_set": "guest MemAvailable minus 16MiB headroom, one nonzero cacheline per 4KiB page; verified after restore",
              "timing": "each local duration uses CLOCK_MONOTONIC; cross-node downtime is source pause request to received peer ACK/ready, no subtraction of clocks across L1s",
              "ept": "fresh destination KVM VM/vCPU and EPT per restore; guest reaccesses every working-set page then repeats the scan",
              "limitations": ["nested KVM on one physical host; the lab virtio-net transport is not a physical NIC",
                              "diskless one-vCPU microVM; no block, guest network or vsock; snapshot files in tmpfs without fsync",
                              "first scan includes EPT faults, host file faults and cold caches; warm-scan difference alone is not pure EPT time",
                              "empty API process startup and completed source cleanup are separately reported outside downtime"],
              "samples": {}, "summary_ms": {}, "artifact_integrity": {}}
    if args.trace_ept:
        report["ept_trace_profile"] = {"functions": migration_ept_functions(detail),
                                       "per_leaf_tracepoints": not detail,
                                       "buffer_kib_per_cpu": 524288 if detail else 262144,
                                       "timing": "inclusive function entry-to-return intervals; nested scopes must not be added; includes scheduling and probe overhead; mono text timestamps round to 1us"}
    save_json(out / "report.json", report)
    try:
        for vm in config["vms"].values():
            guest_command(vm, snapshot_cleanup_script(include_boot_vm=True) + f"\nmkdir -m 700 {transaction}", timeout=15)
        for role, vm in config["vms"].items():
            peer = config["migration_network"]["ips"]["B" if role == "A" else "A"]
            guest_command(vm, f"ping -c 1 -W 5 {peer}", timeout=10)
        if args.trace_ept:
            for vm in config["vms"].values():
                guest_command(vm, migration_trace_script(True, detail), timeout=30)
        configuration = {"boot-source": {"kernel_image_path": "/lab/l2-vmlinux", "initrd_path": "/lab/l2-initramfs.cpio",
                         "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init lab.role=A memmap=2M$64M lab.migration=1 lab.migration_headroom=16 nmi_watchdog=0 transparent_hugepage=never"},
                         "machine-config": {"vcpu_count": 1, "mem_size_mib": args.memory_mib}, "drives": []}
        report["guest_configuration"] = configuration
        for role in ("B", "A"):
            vm = config["vms"][role]
            body = ""
            for name, value in [("config", configuration), ("machine-config", configuration["machine-config"]),
                                ("boot-source", configuration["boot-source"])]:
                body += f"cat > {transaction}/bench-{name}.json <<'MIGRATION_CONFIG'\n{json.dumps(value)}\nMIGRATION_CONFIG\n"
            peer = config["migration_network"]["ips"]["A" if role == "B" else "B"]
            env = (f"BENCH_RUN_DIR={transaction} BENCH_NO_SECCOMP=1 BENCH_SERIAL_FILE=1 "
                   f"FC_RESTORE_TIMING={int(args.profile_restore)} "
                   f"FC_DAX_NO_NESTED_GUEST=1 FC_VCPU_CPU=1 FC_EXACT_TSC=1 "
                   f"MIGRATION_EPT_TRACE={int(args.trace_ept)} ")
            body += (f"(set +e; {env}taskset 1 /lab/shared-probe migration {role} {peer} {port} {rounds} {nonce} {args.memory_mib} "
                     f"> {transaction}/bench.log 2>&1; echo $? > {transaction}/bench.status) </dev/null &")
            guest_command(vm, body, timeout=15)
        deadline = time.monotonic() + 180 + rounds * 30
        report["status_poll_retries"] = []
        while True:
            statuses = {}
            for role, vm in config["vms"].items():
                for attempt in range(3):
                    try:
                        text = guest_command(vm, f"if [ -f {transaction}/bench.status ]; then printf 'BENCH_STATUS='; cat {transaction}/bench.status; fi", timeout=30)
                        break
                    except (RuntimeError, OSError) as exc:
                        report["status_poll_retries"].append({"role": role, "attempt": attempt + 1,
                                                              "error": str(exc)[:1024], "read_only": True})
                        save_json(out / "report.json", report)
                        if attempt == 2:
                            raise RuntimeError(f"Read-only migration status polling failed three times on {role}") from exc
                        # Start a fresh serial command with a newline and a new marker.
                        try:
                            guest_command(vm, ":", timeout=10)
                        except (RuntimeError, OSError):
                            pass
                match = re.search(r"^BENCH_STATUS=(\d+)\s*$", text, re.MULTILINE)
                statuses[role] = int(match[1]) if match else None
            if any(value is not None and value != 0 for value in statuses.values()):
                raise RuntimeError(f"Migration failed: {statuses}")
            if all(value == 0 for value in statuses.values()):
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"Migration timed out: {statuses}")
            time.sleep(1)
        for role, vm in config["vms"].items():
            guest_command(vm, f"cd {transaction}\nset -- bench.log\n"
                          "for f in bench-fc-*.log bench-serial-*.log; do [ -f \"$f\" ] || continue; set -- \"$@\" \"$f\"; done\n"
                          "tar -cf collected-logs.tar \"$@\"", timeout=30)
            data, integrity = guest_fetch_file(vm, f"{transaction}/collected-logs.tar")
            (out / f"{role}-guest-logs.tar").write_bytes(data)
            report["artifact_integrity"][role] = integrity
            files = {}
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                for member in archive.getmembers():
                    if (not member.isfile() or member.name in files or
                            not re.fullmatch(r"bench(?:\.log|-(?:fc|serial)-\d+\.log)", member.name)):
                        raise RuntimeError(f"Unexpected or duplicate migration log {member.name!r}")
                    files[member.name] = archive.extractfile(member).read()
            if "bench.log" not in files:
                raise RuntimeError(f"Missing migration controller log on {role}")
            raw = out / "guest-artifacts" / role
            raw.mkdir(parents=True)
            for name, value in files.items():
                (raw / name).write_bytes(value)
            text = files["bench.log"].decode(errors="replace")
            (out / f"{role}.log").write_text(text)
            report["samples"][role] = [json.loads(line.removeprefix("MIGRATION_JSON "))
                                       for line in text.splitlines() if line.startswith("MIGRATION_JSON ")]
            for name, value in files.items():
                generation = re.fullmatch(r"bench-fc-(\d+)\.log", name)
                if not generation:
                    continue
                generation = int(generation[1])
                log_text = value.decode(errors="replace")
                for phase, vcpu, cr3, msrs, indices in re.findall(
                        r"DAX_CPU_STATE phase=(save|restore) vcpu=(\d+) cr3=0x([0-9a-fA-F]+) "
                        r"msrs=(\d+) indices=([0-9a-fA-F]{16})(?![0-9a-fA-F])", log_text):
                    cpu_round = (0 if role == "A" and generation == 0 else generation + 1) if phase == "save" else generation
                    report.setdefault("cpu_state_signatures", []).append(
                        {"role": role, "round": cpu_round, "phase": phase, "vcpu": int(vcpu),
                         "cr3": int(cr3, 16), "msr_count": int(msrs), "ordered_msr_indices_hash": indices.lower()})
                for record in re.findall(r"RESTORE_TIMING (\{[^\r\n]+\})", log_text):
                    profile = json.loads(record)
                    save_phase = profile["scope"].startswith("save") or profile["scope"] in ("create_snapshot", "snapshot_create", "snapshot_memory")
                    if not save_phase and role == "A" and generation == 0:
                        continue  # Initial cold boot is outside migration timing.
                    profile_round = (0 if role == "A" and generation == 0 else generation + 1) if save_phase else generation
                    report.setdefault("profiles", []).append({"role": role, "log": name, "round": profile_round, **profile})
            if args.trace_ept:
                guest_command(vm, "echo 0 > /sys/kernel/tracing/tracing_on\n"
                              f"cat /sys/kernel/tracing/trace > {transaction}/ept.trace\n"
                              f"cat /sys/kernel/tracing/per_cpu/cpu*/stats > {transaction}/ept.stats\n"
                              f"cat /sys/kernel/tracing/kprobe_profile > {transaction}/ept.kprobes", timeout=30)
                for name in ("trace", "stats", "kprobes"):
                    fetch = migration_trace_fetch if name == "trace" else lambda vm, config, path: guest_fetch_file(vm, path)
                    value, integrity = fetch(vm, config, f"{transaction}/ept.{name}")
                    (out / f"{role}-ept.{name}").write_bytes(value)
                    report["artifact_integrity"][role].setdefault("ept", {})[name] = integrity
                trace = migration_ept_summary((out / f"{role}-ept.trace").read_text(),
                                              (out / f"{role}-ept.stats").read_text() + "\n" +
                                              (out / f"{role}-ept.kprobes").read_text(), detail)
                report.setdefault("ept_diagnostics", {})[role] = trace
                if not trace["valid"] and (rounds > 1 or role == "B"):
                    raise RuntimeError(f"Invalid EPT diagnostic on {role}: losses={trace['losses']}, missing_probes={trace['missing_probes']}, missing_hits={trace['missing_required_hits']}")
        samples = sum(report["samples"].values(), [])
        if any(s.get("nonce") != nonce or s.get("memory_mib") != args.memory_mib for s in samples):
            raise RuntimeError("Migration report nonce or memory size mismatch")
        sources = sorted((s for s in samples if s["event"] == "source"), key=lambda s: s["round"])
        destinations = sorted((s for s in samples if s["event"] == "destination"), key=lambda s: s["round"])
        complete = [s for s in samples if s["event"] == "complete"]
        boots = [s for s in samples if s["event"] == "boot"]
        if (len(sources) != rounds or len(destinations) != rounds or len(complete) != 2 or len(boots) != 1 or
                any(s["round"] != r or d["round"] != r for r, (s, d) in enumerate(zip(sources, destinations)))):
            raise RuntimeError("Missing, duplicate, or incomplete migration rounds")
        expected_ram = args.memory_mib * 1024 * 1024
        for source, destination in zip(sources, destinations):
            expected_source = "A" if source["round"] % 2 == 0 else "B"
            if (source["ram_bytes"] != expected_ram or destination["ram_bytes"] != expected_ram or
                    source["role"] != expected_source or destination["role"] == expected_source or
                    source["state_bytes"] != destination["state_bytes"] or
                    source["wire_payload_bytes"] != expected_ram + source["state_bytes"] or
                    source["wire_payload_bytes"] != destination["wire_payload_bytes"]):
                raise RuntimeError("Migration did not transfer the complete RAM and matching state bytes")
        if any(s.get(key, 0) for s in destinations + complete for key in ("errors", "clock_errors", "tsc_errors")):
            raise RuntimeError("Retained guest data, clock, or TSC continuity validation failed")
        if any(s["instance_id"] != boots[0]["instance_id"] or s["working_set_bytes"] != boots[0]["working_set_bytes"] for s in destinations):
            raise RuntimeError("Guest instance rebooted or retained working set changed")
        if any(b["sequence"] <= a["sequence"] for a, b in zip(sources, sources[1:])):
            raise RuntimeError("Guest command sequence did not advance across migrations")
        if args.profile_restore:
            signatures = report.get("cpu_state_signatures", [])
            initial_signature = None
            if len(signatures) != rounds * 2:
                raise RuntimeError("Missing or duplicate CPU save/restore diagnostics")
            for r in range(rounds):
                source_role, destination_role = ("A", "B") if r % 2 == 0 else ("B", "A")
                saved = [row for row in signatures if row["round"] == r and row["phase"] == "save"]
                restored = [row for row in signatures if row["round"] == r and row["phase"] == "restore"]
                if (len(saved) != 1 or len(restored) != 1 or saved[0]["role"] != source_role or
                        restored[0]["role"] != destination_role or saved[0]["vcpu"] or restored[0]["vcpu"]):
                    raise RuntimeError(f"Invalid CPU diagnostic round or vCPU identity at migration {r}")
                saved, restored = saved[0], restored[0]
                signature = saved["msr_count"], saved["ordered_msr_indices_hash"]
                if initial_signature is None:
                    initial_signature = signature
                if (saved["cr3"] != restored["cr3"] or signature != initial_signature or
                        signature != (restored["msr_count"], restored["ordered_msr_indices_hash"])):
                    raise RuntimeError(f"Guest CR3 or incoming ordered MSR save list changed at migration {r}")
        def summary(values):
            values = sorted(value / 1000 for value in values)
            return {"n": len(values), "p50": statistics.median(values), "p95": values[max(0, (95 * len(values) + 99) // 100 - 1)],
                    "min": values[0], "max": values[-1], "mean": statistics.mean(values)}
        for event, group in (("source", sources), ("destination", destinations)):
            selected = [s for s in group if s["round"] >= args.warmup]
            for key in selected[0]:
                if key.endswith("_us"):
                    report["summary_ms"][event + "." + key.removesuffix("_us")] = summary([s[key] for s in selected])
                elif key.endswith("_sweep_ns"):
                    report["summary_ms"][event + "." + key.removesuffix("_ns")] = summary([s[key] / 1000 for s in selected])
        for scope in sorted({p["scope"] for p in report.get("profiles", [])}):
            selected = [p for p in report["profiles"] if p["scope"] == scope and args.warmup <= p["round"] < rounds]
            if selected:
                report.setdefault("profile_summary_ms", {})[scope + ".total"] = summary([p["total_us"] for p in selected])
                for stage in selected[0]["stages_us"]:
                    report["profile_summary_ms"][scope + "." + stage] = summary([p["stages_us"][stage] for p in selected])
        report["validation"] = {"migrations": rounds, "full_ram_bytes_per_migration": expected_ram,
                                "total_ram_bytes_transferred": expected_ram * rounds, "semantic_state_bytes": sorted({s["state_bytes"] for s in sources}),
                                "one_initial_boot": True, "guest_instance_preserved": True, "retained_data_errors": 0,
                                "working_set_bytes": boots[0]["working_set_bytes"], "working_set_ratio": boots[0]["working_set_ratio"],
                                "new_destination_kvm_per_restore": True, "no_shared_guest_ram": True}
        if args.profile_restore:
            report["validation"].update(guest_cr3_preserved_each_restore=True,
                                         ordered_msr_count_and_hash_preserved_each_restore=True,
                                         initial_ordered_msr_save_list_preserved_all_migrations=True)
        report["result"] = "passed"
    except BaseException as exc:
        report.update(result="failed", error=str(exc))
        for role, vm in config["vms"].items():
            try:
                value, _ = guest_fetch_file(vm, f"{transaction}/bench.log")
                (out / f"{role}.log").write_bytes(value)
                diagnostics = guest_command(vm, f"cd {transaction}\n"
                                            "for f in $(ls -t bench-fc-*.log bench-serial-*.log 2>/dev/null | head -n 4); do "
                                            "printf '\\nMIGRATION_FAILURE_LOG=%s\\n' \"$f\"; tail -c 8192 \"$f\"; done", timeout=20)
                (out / f"{role}-failure.log").write_text(diagnostics)
            except (RuntimeError, OSError):
                pass
        raise
    finally:
        report["cleanup"] = {}
        for role, vm in config["vms"].items():
            try:
                guest_command(vm, snapshot_cleanup_script(transaction), timeout=30)
                # Only this transaction's RAM files, after all recorded owners exited.
                guest_command(vm, f"rm -f {transaction}/*.mem", timeout=15)
                report["cleanup"][role] = {"recorded_processes_exited": True, "transaction_ram_files_removed": True}
                if args.trace_ept:
                    guest_command(vm, migration_trace_script(False, detail), timeout=15)
            except (RuntimeError, OSError) as exc:
                report["cleanup"][role] = {"error": str(exc)[:1024]}
        save_json(out / "report.json", report)
    print(json.dumps({"result": report["result"], "report": str(out / "report.json"),
                      "validation": report["validation"], "summary_ms": report["summary_ms"]}, indent=2))


def handoff_bench(args):
    """State-only Firecracker checkpoints with guest RAM kept in shared DAX."""
    run, config = current_run()
    if not config.get("dax_guest") or config.get("dax_kvm_state") or config.get("dax_handoff"):
        raise RuntimeError("Use a fresh --dax-guest lab without --dax-kvm-state or --dax-handoff; each kernel needs private KVM objects/heap.")
    if not 128 <= args.memory_mib <= 2048 or args.memory_mib + 2 > config["cxl_mib"]:
        raise RuntimeError("Require 128..2048MiB guest RAM fitting the CXL allocation.")
    if not 1 <= args.iterations <= 500 or not 0 <= args.warmup <= 100:
        raise RuntimeError("Require 1..500 iterations and 0..100 warmups.")
    if args.test_corrupt_state and (args.iterations != 1 or args.warmup != 0):
        raise RuntimeError("--test-corrupt-state requires --iterations 1 --warmup 0")
    if args.reuse_kvm and args.mode != "warm":
        raise RuntimeError("--reuse-kvm requires --mode warm; both used workers remain alive")
    if args.reuse_kvm and args.heartbeat_pit:
        raise RuntimeError("--reuse-kvm does not support --heartbeat-pit: pending PIT state is not reset by this experiment")
    if args.reuse_quiet_irqfd and not args.reuse_kvm:
        raise RuntimeError("--reuse-quiet-irqfd requires --reuse-kvm")
    if args.reuse_prepare and not args.reuse_kvm:
        raise RuntimeError("--reuse-prepare requires --reuse-kvm")
    if args.reuse_prepare_on_demand and not args.reuse_prepare:
        raise RuntimeError("--reuse-prepare-on-demand requires --reuse-prepare")
    for name in ("guest_cpu", "executor_cpu"):
        cpu = getattr(args, name)
        if cpu is not None and not 0 <= cpu < config["l1_vcpus"]:
            raise RuntimeError(f"--{name.replace('_', '-')} requires an available L1 CPU")
    if args.fixed_ram_hva is not None and (args.fixed_ram_hva < 2 * 1024 * 1024 or
            args.fixed_ram_hva % (2 * 1024 * 1024) or
            args.fixed_ram_hva + args.memory_mib * 1024 * 1024 >= 1 << 47):
        raise RuntimeError("--fixed-ram-hva requires a positive 2MiB-aligned userspace address below 2^47")
    rounds = args.iterations + args.warmup
    out = run / f"handoff-{args.memory_mib}m-{args.mode}-{time.time_ns()}"
    out.mkdir()
    nonce = secrets.token_hex(8)
    transaction = f"/run/snapshot-bench-{nonce}"
    report = {"result": "running", "memory_mib": args.memory_mib, "vcpus": 1,
              "mode": args.mode, "iterations": args.iterations, "warmup": args.warmup,
              "preset": getattr(args, "preset", None),
              "runtime_configuration": config.get("runtime_configuration"),
              "heartbeat": args.heartbeat, "clock_realtime": args.clock_realtime,
              "dense_working_set": args.dense_working_set,
              "guest_clocksource": args.guest_clocksource,
              "profile_restore": args.profile_restore,
              "profile_ack": args.profile_ack,
              "ack_trace_gated": args.profile_ack,
              "no_seccomp": args.no_seccomp,
              "no_nested_guest": args.no_nested_guest,
              "serial_file": args.serial_file or args.reuse_kvm,
              "exact_tsc": args.exact_tsc,
              "direct_cpu_model": args.direct_cpu_model,
              "keep_kvm_active": args.keep_kvm_active,
              "reuse_kvm": args.reuse_kvm,
              "reuse_prepare": args.reuse_prepare,
              "reuse_prepare_on_demand": args.reuse_prepare_on_demand,
              "reuse_irqfd_mode": ("quiet" if args.reuse_quiet_irqfd else "strict") if args.reuse_kvm else None,
              "guest_cpu": args.guest_cpu, "executor_cpu": args.executor_cpu,
              "poll_us": args.poll_us, "spin_us": args.spin_us,
              "dwell_ms": args.dwell_ms, "timer_slack_ns": args.timer_slack_ns,
              "heartbeat_spins": args.heartbeat_spins,
              "heartbeat_churn": args.heartbeat_churn, "heartbeat_pit": args.heartbeat_pit,
              "fixed_ram_hva": args.fixed_ram_hva,
              "full_snapshot_samples": args.full_snapshot_samples,
              "test_corrupt_state": args.test_corrupt_state,
              "run": str(run), "output": str(out), "nonce": nonce,
              "guest_transaction_directory": transaction,
              "process_ownership": "transaction-private paths; PID plus /proc starttime; cleanup confirms exit before shared RAM cold boot",
              "ram_offset": 2097152, "ram_transfer_bytes": 0,
              "sync_snapshot_files": False, "retained_guest_heap_bytes": None if args.dense_working_set else 16777216,
              "transport": "state bytes via emulated shared CXL DAX mailbox; no network",
              "timing": ("local CLOCK_MONOTONIC; handoff_us is source pause-request begin to observed remote guest heartbeat ACK; "
                         "cycle_us spans the source round including verification, deliberate active dwell and readiness wait; "
                         "destination_ready_wait_us measures only the readiness wait; scheduling_wait_plus_handoff_us adds "
                         "that wait to downtime, excluding deliberate dwell; request_to_ack_us starts after dwell before "
                         "readiness wait in prepared mode, at pause request otherwise; RAM VMA diagnostics follow ACK"),
              "snapshot_payload": {"schema": "SNAPSHO version 2", "checksum": "FNV-1a 64-bit (integrity check, not authentication)",
                                   "validation": "session nonce, schema, round, RAM size, payload length and hash before restore"},
              "ept": "new native KVM/MMU/EPT per restore; identical HVA preserves no EPT by itself; guest page-table bytes/CR3 stay in shared RAM",
              "kernel_state_placement": "separate native KVM objects and VMM heap in each L1",
              "limitations": ["two independent L1 kernels on one physical host, nested KVM, emulated CXL; not physical multi-host CXL latency",
                              "minimal guest, no block/network/vsock, fixed shared RAM; snapshot files in tmpfs, no persistence flush",
                              "warm mode prestarts only the Firecracker API process; KVM VM/vCPU/device creation remains inside restore API",
                              "observed heartbeat time is an upper bound, not exact first instruction timestamp"],
              "samples": {}, "summary_ms": {}, "artifact_integrity": {}}
    if args.reuse_kvm:
        report["ept"] = ("each destination retains its own native KVM/MMU/VMCS allocation; "
                         "KVM state-setting ioctls may invalidate translations; fixed CXL backing, no imported EPT")
        report["limitations"][2] = ("reuse mode keeps already-executed VM/vCPU/thread objects on both nodes; "
                                    "B independently boots disposable X, pauses, then A cold-boots Y into the same RAM; "
                                    "seed and initial boot are outside the handoff measurements")
        report["reuse_contract"] = ("same virtual-hardware fields of VmInfo, CPU/vCPU/RAM/device topology; "
                                    "boot metadata may differ; one static shared DAX RAM allocation; "
                                    "this benchmark does not switch between distinct CXL backing allocations")
        report["reuse_irqfd"] = ("quiet comparison: kernel IRQFD work is not flushed; valid only for this "
                                  "controlled workload with no external I/O or active PIT" if args.reuse_quiet_irqfd else
                                  "strict default: deassign IRQFDs, flush pending kernel work, restore state and reassign")
        if args.reuse_quiet_irqfd:
            report["limitations"].append("quiet IRQFD comparison does not flush pending kernel IRQFD work; "
                                         "it does not establish safety for external I/O or PIT-active workloads")
        if args.reuse_prepare:
            report["reuse_preparation"] = ("one PrepareReuse API per incoming handoff, with a single outstanding prepared "
                                            "worker; B prepares its paused seed before A boots, and each retired source prepares "
                                            "after snapshot publication and peer ACK; readiness is published only after preparation; "
                                            "the next source waits for readiness after its active dwell, while its guest still runs; "
                                            "save_restore_prepare includes background preparation API cost and is not elapsed downtime")
        if args.reuse_prepare_on_demand:
            report["limitations"].append("on-demand test control withholds each retired worker's preparation until the "
                                         "next source requests a handoff after dwell, exposing preparation in readiness wait; "
                                         "only the initial B seed remains preprepared; no added API or mailbox fields")
    save_json(out / "report.json", report)
    try:
        if args.dense_working_set:
            report["dax_alignment_bytes"] = shared_dax_alignment(config)
        # Stop recorded prior controllers before any new shared-RAM cold boot.
        for vm in config["vms"].values():
            guest_command(vm, snapshot_cleanup_script(include_boot_vm=True) +
                          f"\ngrep -q '^enabled=0 ' /sys/module/kvm/parameters/dax_pool\nmkdir -m 700 {transaction}", timeout=10)
        configuration = {"boot-source": {"kernel_image_path": "/lab/l2-vmlinux",
                         "initrd_path": "/lab/l2-initramfs.cpio",
                         "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init lab.role=A memmap=2M$64M lab.heartbeat=1 "
                                      f"lab.heartbeat_sleep={int(args.heartbeat == 'sleep')}"
                                      f" lab.heartbeat_spins={args.heartbeat_spins} lab.heartbeat_churn={int(args.heartbeat_churn)} lab.heartbeat_pit={int(args.heartbeat_pit)} lab.heartbeat_timing={int(args.profile_ack)} lab.heartbeat_quiet=1 nmi_watchdog=0"
                                      + (" lab.heartbeat_dense=1 transparent_hugepage=never" if args.dense_working_set else "")
                                      + (" clocksource=tsc tsc=reliable" if args.guest_clocksource == "tsc" else "")},
                         "machine-config": {"vcpu_count": 1, "mem_size_mib": args.memory_mib}, "drives": []}
        for role, vm in config["vms"].items():
            role_configuration = json.loads(json.dumps(configuration))
            if args.reuse_kvm and role == "B":
                role_configuration["boot-source"]["boot_args"] = role_configuration["boot-source"]["boot_args"].replace("lab.role=A", "lab.role=B")
            body = f"cat > {transaction}/bench-config.json <<'BENCH_CONFIG'\n" + json.dumps(role_configuration) + "\nBENCH_CONFIG\n"
            if args.serial_file or args.reuse_kvm:
                for name in ("machine-config", "boot-source"):
                    body += (f"cat > {transaction}/bench-{name}.json <<'BENCH_COMPONENT'\n" +
                             json.dumps(role_configuration[name]) + "\nBENCH_COMPONENT\n")
            env = (f"BENCH_RUN_DIR={transaction} FC_DAX_DIRECT=0 FC_RESTORE_TIMING={int(args.profile_restore)} BENCH_KVM_KEEPALIVE={int(args.keep_kvm_active)} "
                   f"BENCH_CLOCK_REALTIME={int(args.clock_realtime)} BENCH_POLL_US={args.poll_us} BENCH_SPIN_US={args.spin_us} "
                   f"BENCH_DWELL_US={args.dwell_ms * 1000} BENCH_FULL_SAMPLES={args.full_snapshot_samples} ")
            env += f"LAB_HEARTBEAT_DENSE={int(args.dense_working_set)} "
            env += f"BENCH_NO_SECCOMP={int(args.no_seccomp)} "
            env += f"BENCH_ACK_PROFILE={int(args.profile_ack)} "
            env += f"BENCH_ACK_TRACE_GATE={int(args.profile_ack)} "
            env += f"FC_DAX_EXACT_TSC={int(args.exact_tsc)} "
            env += f"FC_DAX_SNAPSHOT_COMPAT={int(args.direct_cpu_model)} "
            env += f"BENCH_REUSE_KVM={int(args.reuse_kvm)} FC_DAX_REUSE={int(args.reuse_kvm)} "
            env += f"BENCH_REUSE_PREPARE={int(args.reuse_prepare)} "
            env += f"BENCH_REUSE_PREPARE_ON_DEMAND={int(args.reuse_prepare_on_demand)} "
            if args.reuse_kvm:
                env += f"FC_DAX_REUSE_QUIET_IRQFD={int(args.reuse_quiet_irqfd)} "
            env += f"FC_DAX_NO_NESTED_GUEST={int(args.no_nested_guest)} BENCH_SERIAL_FILE={int(args.serial_file or args.reuse_kvm)} "
            if args.fixed_ram_hva is not None:
                env += f"FC_DAX_FIXED_HVA={args.fixed_ram_hva} "
            if args.executor_cpu is not None:
                env += f"FC_DAX_VCPU_CPU={args.executor_cpu} "
            if args.timer_slack_ns:
                env += f"BENCH_TIMER_SLACK_NS={args.timer_slack_ns} "
            if args.test_corrupt_state:
                env += "BENCH_CORRUPT_STATE=1 "
            affinity = f"taskset {1 << args.guest_cpu:x} " if args.guest_cpu is not None else ""
            body += (f"(set +e; {env}{affinity}/lab/shared-probe handoff {role} /dev/dax0.0 {rounds} {args.mode} {args.memory_mib} {nonce} "
                     f"> {transaction}/bench.log 2>&1; echo $? > {transaction}/bench.status) &")
            guest_command(vm, body, timeout=10)
        deadline = time.monotonic() + 180 + rounds * (args.dwell_ms / 1000 + (args.memory_mib * .012 if args.dense_working_set else .05))
        while True:
            statuses = {}
            for role, vm in config["vms"].items():
                text = guest_command(vm, f"if [ -f {transaction}/bench.status ]; then printf 'BENCH_STATUS='; cat {transaction}/bench.status; fi")
                match = re.search(r"^BENCH_STATUS=(\d+)\s*$", text, re.MULTILINE)
                statuses[role] = int(match[1]) if match else None
            if args.test_corrupt_state and statuses["B"] == 3:
                for role, vm in config["vms"].items():
                    data, integrity = guest_fetch_file(vm, f"{transaction}/bench.log")
                    (out / f"{role}.log").write_bytes(data)
                    text = data.decode(errors="replace")
                    report["artifact_integrity"][role] = {"controller": integrity}
                    report["samples"][role] = [json.loads(line.removeprefix("BENCH_JSON "))
                                              for line in text.splitlines() if line.startswith("BENCH_JSON ")]
                rejected = [s for s in report["samples"]["B"] if s["event"] == "payload_rejected"]
                if rejected != [{"event": "payload_rejected", "role": "B", "round": 0, "before_deserialize": True}]:
                    raise RuntimeError("Receiver failed without the expected payload-integrity rejection")
                for vm in config["vms"].values():
                    guest_command(vm, snapshot_cleanup_script(transaction), timeout=10)
                report.update(result="passed", validation={"tampered_payload_rejected_before_deserialize": True,
                              "guest_resume_after_corruption": False, "source_paused_until_test_cleanup": True})
                save_json(out / "report.json", report)
                print(json.dumps({"result": "passed", "report": str(out / "report.json"), "validation": report["validation"]}, indent=2))
                return
            if any(s is not None and s != 0 for s in statuses.values()):
                raise RuntimeError(f"Benchmark failed: {statuses}")
            if all(s == 0 for s in statuses.values()):
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"Benchmark timed out: {statuses}")
            time.sleep(1)
        for role, vm in config["vms"].items():
            # Snapshot all completed logs once: hundreds of short/empty files
            # otherwise each cost several serial command round trips.
            guest_command(vm, f"cd {transaction}\nset -- bench.log\n"
                          "for f in bench-fc-*.log bench-serial-*.log; do "
                          "[ -f \"$f\" ] || continue; set -- \"$@\" \"$f\"; done\n"
                          "tar -cf collected-logs.tar \"$@\"", timeout=30)
            archive_data, integrity = guest_fetch_file(vm, f"{transaction}/collected-logs.tar")
            (out / f"{role}-guest-logs.tar").write_bytes(archive_data)
            files = {}
            with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:") as archive:
                for member in archive.getmembers():
                    if (not member.isfile() or member.name in files or
                            not re.fullmatch(r"bench(?:\.log|-(?:fc|serial)-\d+\.log)", member.name)):
                        raise RuntimeError(f"Unexpected or duplicate collected log {member.name!r}")
                    files[member.name] = archive.extractfile(member).read()
            if "bench.log" not in files or not any(name.startswith("bench-fc-") for name in files):
                raise RuntimeError(f"Missing controller or VMM logs on {role}")
            def member_integrity(name):
                return {"path": f"{transaction}/{name}", "bytes": len(files[name]),
                        "sha256": hashlib.sha256(files[name]).hexdigest(), "verified_archive_member": True}
            data = files.pop("bench.log")
            (out / f"{role}.log").write_bytes(data)
            text = data.decode(errors="replace")
            report["artifact_integrity"][role] = {"archive": integrity, "vmm_logs": [],
                                                   "controller": {"path": f"{transaction}/bench.log", "bytes": len(data),
                                                                  "sha256": hashlib.sha256(data).hexdigest(),
                                                                  "verified_archive_member": True}}
            report["samples"][role] = [json.loads(line.removeprefix("BENCH_JSON "))
                                        for line in text.splitlines() if line.startswith("BENCH_JSON ")]
            raw_directory = out / "guest-artifacts" / role
            raw_directory.mkdir(parents=True)
            logs = []
            for name, data in files.items():
                (raw_directory / name).write_bytes(data)
                report["artifact_integrity"][role]["vmm_logs"].append(member_integrity(name))
                prefix = "FC_LOG" if name.startswith("bench-fc-") else "SERIAL_LOG"
                logs.append(f"\n{prefix}={transaction}/{name}\n" + data.decode(errors="replace"))
            (out / f"{role}-firecracker.log").write_text("".join(logs))
        all_samples = sum(report["samples"].values(), [])
        sources = sorted([s for s in all_samples if s["event"] == "source"], key=lambda s: s["round"])
        destinations = sorted([s for s in all_samples if s["event"] == "destination"], key=lambda s: s["round"])
        complete = [s for s in all_samples if s["event"] == "complete"]
        if len(sources) != rounds or len(destinations) != rounds or len(complete) != 2:
            raise RuntimeError("Missing benchmark samples")
        if any(s["round"] != i for i, s in enumerate(sources)) or any(s["round"] != i for i, s in enumerate(destinations)):
            raise RuntimeError("Missing or duplicate handoff round")
        if any(b["sequence"] <= a["sequence"] for a, b in zip(sources, sources[1:])):
            raise RuntimeError("Guest heartbeat did not advance across handoffs")
        boot_count = sum(len(re.findall(r"^L2_BOOTED ", (out / f"{r}-firecracker.log").read_text(), re.MULTILINE)) for r in ["A", "B"])
        if boot_count != (2 if args.reuse_kvm else 1) or any(s["errors"] or s["clock_errors"] or s["tsc_errors"] for s in complete):
            raise RuntimeError("Guest rebooted or retained heap/clock/TSC check failed")
        if any(s["state_bytes"] != d["state_bytes"] or s["state_hash"] != d["state_hash"] for s, d in zip(sources, destinations)):
            raise RuntimeError("Published snapshot bytes/hash differ at destination")
        ram_hvas = sorted({s["ram_hva"] for s in sources + destinations})
        if args.fixed_ram_hva is not None and ram_hvas != [args.fixed_ram_hva]:
            raise RuntimeError("Firecracker RAM HVA changed across snapshots")
        report["validation"] = {"handoffs": rounds, "boot_markers": boot_count,
                                "retained_heap_errors": 0, "paused_heartbeat_unchanged": True,
                                "guest_clock_errors": 0, "guest_tsc_errors": 0,
                                "payload_integrity_verified": True, "ram_hvas": ram_hvas,
                                "state_bytes": sorted({s["state_bytes"] for s in sources}),
                                "snapshot_payload_bytes_total": sum(s["state_bytes"] for s in sources)}
        if args.dense_working_set:
            validate_dense_working_set(report, all_samples, sources, rounds, args.memory_mib)
        if args.reuse_kvm:
            seeds = [s for s in all_samples if s["event"] == "reuse_seed"]
            if (len(seeds) != 1 or seeds[0]["role"] != "B" or
                    not seeds[0]["paused_before_primary_boot"] or not seeds[0]["excluded_from_handoff_timing"]):
                raise RuntimeError("Missing proof of previously executed disposable microVM")
            identities = [s for s in all_samples if s["event"] == "native_identity"]
            for role in ("A", "B"):
                selected = [s for s in identities if s["role"] == role]
                signature = {(s["pid"], s["process_start"], s["vm_fd"], s["vcpu_fd"], s["vcpu_tid"])
                             for s in selected}
                if len(selected) != rounds + 1 or len(signature) != 1:
                    raise RuntimeError(f"Reused native process/fd/thread identity changed on {role}")
                if len(re.findall(r"^L2_BOOTED ", (out / f"{role}-firecracker.log").read_text(), re.MULTILINE)) != 1:
                    raise RuntimeError(f"Unexpected cold boot count in persistent worker {role}")
            report["reuse_seed"] = seeds[0]
            report["native_identities"] = identities
            report["validation"].update(previous_guest_executed_on_destination=True,
                                         primary_guest_boot_markers=1, disposable_guest_boot_markers=1,
                                         persistent_process_kvm_fds_vcpu_thread=True)
        preparations = sorted([s for s in all_samples if s["event"] == "reuse_prepare"],
                              key=lambda s: s["round"])
        if args.reuse_prepare:
            if (len(preparations) != rounds or
                    any(s["round"] != r or s["role"] != ("B" if r % 2 == 0 else "A") or
                        s["seed"] != (r == 0) or not s["outside_pause_to_ack"]
                        for r, s in enumerate(preparations))):
                raise RuntimeError("Missing, duplicate, or misordered destination preparation")
            report["preparations"] = preparations
            report["validation"]["one_coarse_preparation_per_incoming_handoff"] = True
        elif preparations:
            raise RuntimeError("Unexpected preparation in the unchanged default mode")
        if args.profile_ack:
            ack_profiles = sorted([s for s in all_samples if s["event"] == "ack_profile"],
                                  key=lambda s: s["round"])
            if len(ack_profiles) != rounds or any(s["round"] != r for r, s in enumerate(ack_profiles)):
                raise RuntimeError("Missing or duplicate snapshot ACK profile round")
            for sample in ack_profiles:
                stamps = [sample[key] for key in ("load_begin_ns", "load_end_ns", "resume_begin_ns",
                                                  "resume_end_ns", "ack_observed_ns", "heap_verified_ns")]
                if (sample["nonce"] != nonce or sample["guest_ack_epoch"] != sample["round"] + 1 or
                        not sample["guest_ack_tsc"] or any(stamp <= 0 for stamp in stamps) or
                        stamps != sorted(stamps)):
                    raise RuntimeError("Snapshot ACK profile session, timestamp, or guest epoch mismatch")
                for phase in ("resume_begin", "resume_end", "ack_observed"):
                    before, after = (sample[f"{phase}_clock_{edge}_ns"] for edge in ("before", "after"))
                    if (before <= 0 or before > after or not sample[f"{phase}_tsc"] or
                            sample[f"{phase}_ns"] != before + (after - before) // 2):
                        raise RuntimeError("Snapshot ACK TSC/CLOCK_MONOTONIC anchor is invalid")
            report["ack_profiles"] = ack_profiles
            report["validation"]["ack_profile_timestamps_and_guest_epochs"] = True
            report["ack_profile_timing"] = ("L1 CLOCK_MONOTONIC timestamps immediately before trace_marker writes; "
                                            "resume/ACK timestamps are midpoints of CLOCK_MONOTONIC before/after "
                                            "lfence-RDTSC-lfence, with interval bounds and raw L1 TSC retained; "
                                            "trace collection is enabled immediately before LOAD_BEGIN and disabled "
                                            "after HEAP_VERIFIED, excluding initial boot and between-round idle work; "
                                            "guest_ack_tsc belongs to the guest clock domain and must not be directly "
                                            "subtracted from L1 timestamps; opt-in marker writes perturb instrumented runs")
        for role, vm in config["vms"].items():
            text = guest_command(vm, "cat /proc/sys/kernel/tainted\ncat /sys/module/kvm/parameters/dax_pool")
            (out / f"{role}-kernel-final.log").write_text(text)
            if not re.search(r"^0\s*$", text, re.MULTILINE) or not re.search(r"^enabled=0 ", text, re.MULTILINE):
                raise RuntimeError("Kernel tainted or native snapshot path enabled the shared KVM pool")
        report["validation"]["both_kernels_untainted_and_pool_disabled"] = True
        def summary(values):
            values = sorted(v / 1000 for v in values)
            return {"n": len(values), "p50": statistics.median(values),
                    "p95": values[max(0, (95 * len(values) + 99) // 100 - 1)],
                    "min": values[0], "max": values[-1], "mean": statistics.mean(values)}
        for group in [sources, destinations]:
            selected = [s for s in group if s["round"] >= args.warmup]
            for key in selected[0]:
                if key.endswith("_us"):
                    report["summary_ms"][key.removesuffix("_us")] = summary([s[key] for s in selected])
        measured_sources = [s for s in sources if s["round"] >= args.warmup]
        report["validation"]["destination_ready_at_request_samples"] = sum(s["destination_ready_at_request"]
                                                                            for s in measured_sources)
        report["paired_costs_us"] = [{"round": s["round"],
                                      "save_restore": s["snapshot_us"] + d["restore_us"],
                                      "save_transport_restore": (s["snapshot_us"] + s["publish_us"] +
                                                                 d["receive_us"] + d["restore_us"]),
                                      "pause_save_transport_restore": (s["pause_us"] + s["snapshot_us"] +
                                                                       s["publish_us"] + d["receive_us"] +
                                                                       d["restore_us"])}
                                     for s, d in zip(sources, destinations)]
        measured_pairs = [s for s in report["paired_costs_us"] if s["round"] >= args.warmup]
        for key in ("save_restore", "save_transport_restore", "pause_save_transport_restore"):
            report["summary_ms"][key] = summary([s[key] for s in measured_pairs])
        report["validation"]["save_restore_under_1ms_samples"] = sum(s["save_restore"] < 1000 for s in measured_pairs)
        report["validation"]["save_transport_restore_under_1ms_samples"] = sum(s["save_transport_restore"] < 1000
                                                                               for s in measured_pairs)
        if args.reuse_prepare:
            for pair, preparation in zip(report["paired_costs_us"], preparations):
                pair["save_restore_prepare"] = pair["save_restore"] + preparation["prepare_us"]
            measured_preparations = [s for s in preparations if s["round"] >= args.warmup]
            report["summary_ms"]["prepare"] = summary([s["prepare_us"] for s in measured_preparations])
            report["summary_ms"]["save_restore_prepare"] = summary([s["save_restore_prepare"] for s in measured_pairs])
            report["validation"]["save_restore_prepare_under_1ms_samples"] = sum(s["save_restore_prepare"] < 1000
                                                                                  for s in measured_pairs)
        full = [s for s in all_samples if s["event"] == "full_snapshot"]
        if full:
            selected = [s["us"] for s in full if s["sample"] > 0] or [s["us"] for s in full]
            report["summary_ms"]["full_snapshot"] = summary(selected)
        if args.profile_restore:
            profiles = []
            preparation_profiles = []
            save_profiles = []
            cpu_records = []
            for role in ("A", "B"):
                text = (out / f"{role}-firecracker.log").read_text()
                reuse_cpu_counts = {"save": 0, "restore": 0}
                reuse_profile_counts = {}
                reuse_save_profile_counts = {}
                for generation, log in re.findall(r"FC_LOG=/run/(?:snapshot-bench-[0-9a-f]{16}/)?bench-fc-(\d+)\.log\s*([\s\S]*?)(?=FC_LOG=|\Z)", text):
                    for phase, vcpu, cr3, msrs, indices in re.findall(
                            r"DAX_CPU_STATE phase=(save|restore) vcpu=(\d+) cr3=0x([0-9a-fA-F]+) "
                            r"msrs=(\d+) indices=([0-9a-fA-F]{16})(?![0-9a-fA-F])", log):
                        mapped_generation = int(generation)
                        if args.reuse_kvm:
                            index = reuse_cpu_counts[phase]
                            reuse_cpu_counts[phase] += 1
                            if phase == "save" and role == "A":
                                index -= args.full_snapshot_samples
                                if index < 0:
                                    continue
                            source_round = (0 if role == "A" else 1) + 2 * index
                            destination_round = (1 if role == "A" else 0) + 2 * index
                            mapped_generation = source_round if phase == "save" else destination_round + 1
                        cpu_records.append({"role": role, "generation": mapped_generation, "phase": phase,
                                            "vcpu": int(vcpu), "cr3": int(cr3, 16), "msrs": int(msrs),
                                            "indices": indices.lower()})
                    for record in re.findall(r"RESTORE_TIMING (\{[^\r\n]+\})", log):
                        parsed = json.loads(record)
                        if parsed["scope"].startswith("save") or parsed["scope"] in ("create_snapshot", "snapshot_create"):
                            if args.reuse_kvm:
                                index = reuse_save_profile_counts.get(parsed["scope"], 0)
                                reuse_save_profile_counts[parsed["scope"]] = index + 1
                                if role == "A":
                                    index -= args.full_snapshot_samples
                                if index < 0:
                                    continue
                                profile_round = (0 if role == "A" else 1) + 2 * index
                            else:
                                profile_round = int(generation)
                            save_profiles.append({"role": role, "round": profile_round, **parsed})
                            continue
                        if args.reuse_kvm:
                            if parsed["scope"] not in ("reuse", "reuse_vcpu", "reuse_prepare", "reuse_prepare_vcpu"):
                                continue
                            index = reuse_profile_counts.get(parsed["scope"], 0)
                            profile_round = (1 if role == "A" else 0) + 2 * index
                            reuse_profile_counts[parsed["scope"]] = index + 1
                            if parsed["scope"] in ("reuse_prepare", "reuse_prepare_vcpu"):
                                preparation_profiles.append({"role": role, "round": profile_round, **parsed})
                                continue
                        else:
                            if int(generation) == 0:
                                continue
                            profile_round = int(generation) - 1
                        profiles.append({"role": role, "round": profile_round, **parsed})
            scopes = ("reuse", "reuse_vcpu") if args.reuse_kvm else ("restore", "build", "vm_new", "vm_common")
            if len(profiles) != rounds * len(scopes):
                raise RuntimeError(f"Expected {rounds * len(scopes)} restore profiles, got {len(profiles)}")
            if {(p["round"], p["scope"]) for p in profiles} != {(r, s) for r in range(rounds) for s in scopes}:
                raise RuntimeError("Missing or duplicate restore profile round/scope")
            report["cpu_state_records"] = cpu_records
            report["cpu_state_handoffs"] = []
            first_msr_signature = None
            for r in range(rounds):
                source_role, destination_role = ("A", "B") if r % 2 == 0 else ("B", "A")
                saved = [c for c in cpu_records if c["role"] == source_role and
                         c["generation"] == r and c["phase"] == "save"]
                restored = [c for c in cpu_records if c["role"] == destination_role and
                            c["generation"] == r + 1 and c["phase"] == "restore"]
                if not saved or len(restored) != 1 or any(c["vcpu"] != 0 for c in saved + restored):
                    raise RuntimeError(f"Missing, duplicate, or unexpected vCPU state diagnostics at handoff {r}")
                # Generation 0 also contains the RAM-inclusive snapshot controls.
                # The last save belongs to the state-only snapshot we publish.
                saved, restored = saved[-1], restored[0]
                signature = (saved["msrs"], saved["indices"])
                if first_msr_signature is None:
                    first_msr_signature = signature
                pair = {"round": r, "source": saved, "destination": restored,
                        "cr3_preserved": saved["cr3"] == restored["cr3"],
                        "msr_signature_preserved": signature == (restored["msrs"], restored["indices"]),
                        "initial_msr_save_list_preserved": signature == first_msr_signature}
                report["cpu_state_handoffs"].append(pair)
                if not all(pair[key] for key in ("cr3_preserved", "msr_signature_preserved", "initial_msr_save_list_preserved")):
                    raise RuntimeError(f"Guest CR3 or MSR save-list continuity failed at handoff {r}")
            report["validation"].update(guest_cr3_preserved_each_restore=True,
                                        msr_count_and_index_hash_preserved_each_restore=True,
                                        initial_msr_save_list_preserved_all_handoffs=True)
            report["restore_profiles"] = profiles
            if args.reuse_prepare:
                prepare_scopes = ("reuse_prepare", "reuse_prepare_vcpu")
                if (len(preparation_profiles) != rounds * len(prepare_scopes) or
                        {(p["round"], p["scope"]) for p in preparation_profiles} !=
                        {(r, s) for r in range(rounds) for s in prepare_scopes}):
                    raise RuntimeError("Missing or duplicate coarse preparation timing round/scope")
                report["prepare_profiles"] = preparation_profiles
                report["prepare_profile_ms"] = {}
                for scope in prepare_scopes:
                    selected = [p for p in preparation_profiles if p["scope"] == scope and p["round"] >= args.warmup]
                    report["prepare_profile_ms"][scope + ".total"] = summary([p["total_us"] for p in selected])
                    for stage in selected[0]["stages_us"]:
                        report["prepare_profile_ms"][scope + "." + stage] = summary([p["stages_us"][stage] for p in selected])
            elif preparation_profiles:
                raise RuntimeError("Unexpected preparation profile in unchanged default mode")
            # In fresh mode generation 0 also logs RAM-inclusive controls;
            # its final save per scope is the state-only handoff of round 0.
            save_profiles = list({(p["round"], p["scope"]): p for p in save_profiles}.values())
            report["save_profiles"] = save_profiles
            report["save_profile_ms"] = {}
            for scope in sorted({p["scope"] for p in save_profiles}):
                scoped = [p for p in save_profiles if p["scope"] == scope]
                if sorted(p["round"] for p in scoped) != list(range(rounds)):
                    raise RuntimeError(f"Missing source save timing round for {scope}")
                selected = [p for p in scoped if p["round"] >= args.warmup]
                report["save_profile_ms"][scope + ".total"] = summary([p["total_us"] for p in selected])
                for stage in selected[0]["stages_us"]:
                    report["save_profile_ms"][scope + "." + stage] = summary([p["stages_us"][stage] for p in selected])
            report["profile_round_mapping"] = ("persistent generation 0: source A rounds 0,2,... / B 1,3,...; "
                                                "destination A rounds 1,3,... / B 0,2,...; seed has no snapshot; "
                                                "RAM-inclusive controls excluded" if args.reuse_kvm else
                                                "source generation = round; destination generation = round+1; "
                                                "RAM-inclusive generation-0 controls excluded")
            report["restore_profile_ms"] = {}
            for scope in scopes:
                selected = [p for p in profiles if p["scope"] == scope and p["round"] >= args.warmup]
                report["restore_profile_ms"][scope + ".total"] = summary([p["total_us"] for p in selected])
                for stage in selected[0]["stages_us"]:
                    report["restore_profile_ms"][scope + "." + stage] = summary([p["stages_us"][stage] for p in selected])
        final_role = "A" if rounds % 2 == 0 else "B"
        maps = guest_command(config["vms"][final_role], f'read final_pid final_start < {transaction}/bench-fc.pid\ncat /proc/$final_pid/maps')
        (out / "final-vmm.maps").write_text(maps)
        dax_maps = re.findall(r"^([0-9a-f]+)-([0-9a-f]+) (rw-s) ([0-9a-f]+).* /dev/dax0\.0\s*$", maps, re.MULTILINE)
        if not any(int(end, 16) - int(start, 16) == args.memory_mib * 1024 * 1024 and int(offset, 16) == 2097152
                   for start, end, _, offset in dax_maps):
            raise RuntimeError("Restored RAM is not the expected MAP_SHARED DAX region")
        report["validation"]["restored_shared_dax_mapping"] = True
        report["result"] = "passed"
        save_json(out / "report.json", report)
        print(json.dumps({"result": report["result"], "report": str(out / "report.json"),
                          "validation": report["validation"], "summary_ms": report["summary_ms"]}, indent=2))
    except BaseException as exc:
        report["result"] = "failed"
        report["error"] = str(exc)
        for role, vm in config["vms"].items():
            try:
                text = guest_command(vm, f"cat {transaction}/bench.log 2>/dev/null; for f in {transaction}/bench-fc-*.log {transaction}/bench-serial-*.log; do [ -f \"$f\" ] || continue; tail -60 \"$f\"; done", timeout=15)
                (out / f"{role}-failure.log").write_text(text)
            except (RuntimeError, OSError):
                pass
            try:
                guest_command(vm, snapshot_cleanup_script(transaction), timeout=10)
            except (RuntimeError, OSError):
                pass
        save_json(out / "report.json", report)
        raise


def console(args):
    _, config = current_run()
    vm = config["vms"][args.role]
    if not alive(vm):
        raise RuntimeError("VM is not running")
    print("Connected. Ctrl-] returns to the host without stopping the VM.", flush=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    try:
        if old:
            tty.setraw(fd)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(vm["console"])
            sock.sendall(b"\n")
            while True:
                readable, _, _ = select.select([sock, fd], [], [])
                if sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                if fd in readable:
                    data = os.read(fd, 4096)
                    if not data or b"\x1d" in data:
                        break
                    sock.sendall(data)
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def mptcp_test(args):
    """Exercise DAX-backed L2 sockets and optional semantic snapshot handoff."""
    run, config = current_run()
    if (not config.get("dax_guest") or not config.get("migration_network") or
            config.get("dax_kvm_state") or config.get("dax_handoff") or
            set(config["vms"]) != {"A", "B"} or not all(alive(vm) for vm in config["vms"].values())):
        raise RuntimeError("mptcp-test requires two running --dax-guest --migration-network native-KVM lab VMs")
    if not 100 <= args.rounds <= 100000 or not 1 <= args.interval_us <= 1000000:
        raise RuntimeError("Require 100..100000 exchanges and 1..1000000us interval")
    if args.switch_path and (args.protocol != "mptcp" or args.rounds * args.interval_us < 4000000):
        raise RuntimeError("Path switching requires MPTCP and at least four seconds of exchanges")
    if not 0 <= args.pause_ms <= 10000:
        raise RuntimeError("Require 0..10000ms pause")
    if args.snapshot_handoff and (not args.switch_path or args.protocol != "mptcp" or args.pause_ms):
        raise RuntimeError("Snapshot handoff requires --protocol mptcp --switch-path and no --pause-ms")
    # The normal boot proves nested KVM and DAX RAM before replacing only its recorded Firecracker.
    verify(argparse.Namespace(timeout=180))
    source, peer_vm = config["vms"]["A"], config["vms"]["B"]
    if config["migration_network"]["ips"] != {"A": "172.30.0.1", "B": "172.30.0.2"}:
        raise RuntimeError("Unexpected private lab network")
    for vm in (source, peer_vm):
        guest_command(vm, "test -c /dev/kvm\ngrep -q '^enabled=0 ' /sys/module/kvm/parameters/dax_pool")
    guest_command(source, "test ! -d /sys/class/net/cxlmpt0\ntest -c \"$(cat /run/cxl-dax-device)\"")
    guest_command(peer_vm, "test -z \"$(ip route show 10.88.0.0/24)\"\ntest -z \"$(ip route show 10.89.0.0/24)\"")
    if args.snapshot_handoff:
        guest_command(peer_vm, "test ! -d /sys/class/net/cxlmpt0\ntest -z \"$(ip route show 10.88.0.2/32)\"")
        for vm in (source, peer_vm):
            guest_command(vm, "test \"$(cat /run/cxl-dax-device)\" = /dev/dax0.0\ntest -c /dev/dax0.0")
    nonce = secrets.token_hex(8)
    transaction = f"/run/mptcp-{nonce}"
    port = 19000 + secrets.randbelow(20000)
    out = run / f"mptcp-{args.protocol}-{time.time_ns()}"
    out.mkdir()
    boot_args = ("console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init lab.role=A "
                 f"lab.net_test={args.protocol} lab.net_peer=172.30.0.2 lab.net_port={port} "
                 f"lab.net_rounds={args.rounds} lab.net_interval_us={args.interval_us} "
                 f"lab.net_nonce={nonce} lab.net_switch={int(args.switch_path)}")
    if args.snapshot_handoff:
        boot_args += " lab.net_gate=1 memmap=2M$64M"
    components = {"/machine-config": {"vcpu_count": 1, "mem_size_mib": 128},
                  "/boot-source": {"kernel_image_path": "/lab/l2-vmlinux", "initrd_path": "/lab/l2-initramfs.cpio", "boot_args": boot_args},
                  "/network-interfaces/eth0": {"iface_id": "eth0", "host_dev_name": "cxlmpt0", "guest_mac": "02:ca:00:00:88:02"}}
    report = {"result": "running", "protocol": args.protocol, "switch_path": args.switch_path,
              "snapshot_handoff": args.snapshot_handoff,
              "rounds": args.rounds, "interval_us": args.interval_us, "pause_ms": args.pause_ms,
              "run": str(run), "nonce": nonce, "port": port, "guest_directory": transaction,
              "guest_configuration": components, "guest_ram": {"backing": "shared CXL device-DAX", "offset": 2097152, "bytes": 128 * 1024 * 1024},
              "topology": "L2 on A -> virtio-net/TAP cxlmpt0 -> A IPv4 forwarding -> private L1 link -> L1 B peer",
              "limitations": ["No VM migration, KVM detach/attach or CXL packet staging queue is exercised",
                              "Both guest addresses use one virtio-net interface and the same A forwarding host",
                              "Nested KVM and emulated networking on one coherent physical host; no noncoherent-CXL proof",
                              "Per-exchange latency includes the echo application; this is not a bulk-throughput benchmark"],
              "events": {}, "artifact_integrity": {}, "cleanup_errors": []}
    if args.snapshot_handoff:
        report["topology"] = "L2 on A -> shared-DAX state-only snapshot -> L2 restored on B; MPTCP peer remains L1 B"
        report["limitations"][:2] = [
            "Ordinary semantic snapshot with fresh destination KVM/vCPU/EPT; no direct shared-object detach/attach",
            "Guest RAM remains shared DAX; serialized state crosses the controller's serial channels, not a CXL mailbox",
            "Old source IP is blackholed and source TAP disabled before pause; 50ms quiet period is a controlled lab quiescence assumption",
            "Host monotonic stage durations include serial API/control/file transport; they are diagnostic, not optimized handoff latency",
            "The destination network and its peer run on L1 B; no CXL packet staging buffer or physical-fabric routing cutover"]
        report["handoff"] = {"stages_ns": {}, "processes": {}}
    save_json(out / "report.json", report)

    active_vm = source
    active_log = "firecracker"

    def api(path, method, body):
        return guest_command(active_vm, f"/lab/shared-probe api {transaction}/api.sock {method} {path} {shlex.quote(json.dumps(body))}", timeout=30)

    def record_process(name):
        return (f"recorded_pid=$!\nprocess_stat=$(cat /proc/$recorded_pid/stat)\n"
                "process_fields=${process_stat##*) }\nset -- $process_fields\nshift 19\n"
                f"printf '%s %s\\n' \"$recorded_pid\" \"$1\" > {transaction}/{name}.pid\n")

    def stop_recorded(name):
        # Include the existing incarnation-checked primitive, without matching any global processes.
        return snapshot_cleanup_script("/run/snapshot-bench-" + nonce) + (
            f"if [ -f {transaction}/{name}.pid ]; then\n"
            f"read target_pid expected_start < {transaction}/{name}.pid\n"
            "case \"$target_pid:$expected_start\" in *[!0-9:]*|:|*:) exit 1 ;; esac\n"
            "[ \"$target_pid\" -gt 1 ]\nstop_snapshot_process\nfi\n")

    initialized = set()
    try:
        for role, vm in config["vms"].items():
            guest_command(vm, f"mkdir -m 700 {transaction}")
            initialized.add(role)
        guest_command(source, snapshot_cleanup_script(include_boot_vm=True) +
                      f"cat /proc/sys/net/ipv4/ip_forward > {transaction}/ip_forward\n"
                      "echo 1 > /proc/sys/net/ipv4/ip_forward\n")
        peer_setup = (f"ip route add 10.88.0.0/24 via 172.30.0.1 dev eth0\n"
                      f"touch {transaction}/route88\nip route add 10.89.0.0/24 via 172.30.0.1 dev eth0\n"
                      f"touch {transaction}/route89\n")
        if args.protocol == "mptcp":
            peer_setup += ("test -r /proc/sys/net/mptcp/enabled\n"
                           f"cat /proc/sys/net/mptcp/enabled > {transaction}/mptcp_enabled\n"
                           "echo 1 > /proc/sys/net/mptcp/enabled\n/lab/shared-probe mptcp-pm B limits 2 2\n")
        peer_setup += (f"MPTCP_PROTOCOL={args.protocol} /lab/shared-probe mptcp-stream B server 172.30.0.2 {port} "
                       f"{args.rounds} {args.interval_us} {nonce} > {transaction}/server.log 2>&1 </dev/null &\n" + record_process("server"))
        guest_command(peer_vm, peer_setup)
        guest_command(peer_vm, f"attempt=0\nwhile ! grep -q '\"event\":\"listening\"' {transaction}/server.log; do\n"
                      f"read server_pid unused < {transaction}/server.pid\nkill -0 \"$server_pid\"\n"
                      "attempt=$((attempt + 1))\n[ \"$attempt\" -lt 250 ]\nsleep .02\ndone", timeout=10)
        if args.snapshot_handoff:
            guest_command(source, "/lab/shared-probe dax-word A /dev/dax0.0 69206016 0")
        guest_command(source, f"FC_DAX_RAM=\"$(cat /run/cxl-dax-device)\" FC_DAX_OFFSET=2097152 FC_DAX_SNAPSHOT=1 "
                      f"/lab/firecracker --api-sock {transaction}/api.sock > {transaction}/firecracker.log 2>&1 </dev/null &\n" + record_process("firecracker"))
        guest_command(source, f"attempt=0\nwhile [ ! -S {transaction}/api.sock ]; do\n"
                      f"read fc_pid unused < {transaction}/firecracker.pid\nkill -0 \"$fc_pid\"\n"
                      "attempt=$((attempt + 1))\n[ \"$attempt\" -lt 250 ]\nsleep .02\ndone", timeout=10)
        for path, body in components.items():
            api(path, "PUT", body)
        guest_command(source, "ifconfig cxlmpt0 10.88.0.1 netmask 255.255.255.0 up\n"
                      "ifconfig cxlmpt0:1 10.89.0.1 netmask 255.255.255.0 up\n")
        api("/actions", "PUT", {"action_type": "InstanceStart"})
        if args.snapshot_handoff:
            stages = report["handoff"]["stages_ns"]
            guest_command(source, f"attempt=0\nwhile ! grep -q '^L2_NETWORK_CONNECTED' {transaction}/firecracker.log; do\n"
                          f"read fc_pid unused < {transaction}/firecracker.pid\nkill -0 \"$fc_pid\"\n"
                          f"! grep -q '^L2_NETWORK_RESULT=failed' {transaction}/firecracker.log\n"
                          "attempt=$((attempt + 1))\n[ \"$attempt\" -lt 1000 ]\nsleep .02\ndone", timeout=30)
            text = guest_command(source, f"printf 'TAP_MAC='; cat /sys/class/net/cxlmpt0/address\n"
                                 f"printf 'SOURCE_FC='; cat {transaction}/firecracker.pid")
            mac = re.search(r"^TAP_MAC=([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s*$", text, re.MULTILINE)
            source_pid = re.search(r"^SOURCE_FC=(\d+) (\d+)\s*$", text, re.MULTILINE)
            if not mac or not source_pid:
                raise RuntimeError("Missing source TAP/process identity")
            report["handoff"]["source_tap_mac"] = mac[1]
            report["handoff"]["processes"]["source"] = {"role": "A", "pid": int(source_pid[1]), "starttime": source_pid[2]}
            start = time.monotonic_ns()
            guest_command(peer_vm, f"ip route add blackhole 10.88.0.2/32\ntouch {transaction}/old_path_blackhole")
            guest_command(source, "ifconfig cxlmpt0 down\nsleep .05")
            stages["old_path_block_and_quiet"] = time.monotonic_ns() - start
            start = time.monotonic_ns()
            api("/vm", "PATCH", {"state": "Paused"})
            stages["source_pause_api"] = time.monotonic_ns() - start
            start = time.monotonic_ns()
            api("/snapshot/create", "PUT", {"snapshot_type": "Full", "snapshot_path": transaction + "/state",
                                            "mem_file_path": "/dev/dax0.0"})
            stages["source_snapshot_api"] = time.monotonic_ns() - start
            start = time.monotonic_ns()
            guest_command(source, stop_recorded("firecracker"), timeout=15)
            stages["source_fence"] = time.monotonic_ns() - start
            start = time.monotonic_ns()
            state, integrity = guest_fetch_file(source, transaction + "/state")
            (out / "snapshot-state.bin").write_bytes(state)
            report["handoff"]["state"] = integrity
            encoded = base64.b64encode(state).decode()
            guest_command(peer_vm, f"printf 'begin-base64 600 state\\n' > {transaction}/state.b64")
            # BusyBox's interactive shell silently truncates long input lines,
            # including their redirection. Keep the whole command below 1KiB.
            for index in range(0, len(encoded), 512):
                guest_command(peer_vm, f"printf '%s\\n' {shlex.quote(encoded[index:index + 512])} >> {transaction}/state.b64")
            digest = hashlib.sha256(state).hexdigest()
            guest_command(peer_vm, f"printf '====\\n' >> {transaction}/state.b64\n"
                          f"uudecode -o {transaction}/state {transaction}/state.b64\n"
                          f"printf '%s  %s\\n' {digest} {transaction}/state | sha256sum -c -")
            stages["state_transfer_via_controller"] = time.monotonic_ns() - start
            start = time.monotonic_ns()
            guest_command(peer_vm, f"FC_DAX_RAM=/dev/dax0.0 FC_DAX_OFFSET=2097152 FC_DAX_SNAPSHOT=1 "
                          f"/lab/firecracker --api-sock {transaction}/api.sock > {transaction}/firecracker-destination.log 2>&1 </dev/null &\n" + record_process("firecracker"))
            guest_command(peer_vm, f"attempt=0\nwhile [ ! -S {transaction}/api.sock ]; do\n"
                          f"read fc_pid unused < {transaction}/firecracker.pid\nkill -0 \"$fc_pid\"\n"
                          "attempt=$((attempt + 1))\n[ \"$attempt\" -lt 250 ]\nsleep .02\ndone", timeout=10)
            active_vm, active_log = peer_vm, "firecracker-destination"
            stages["destination_process_start"] = time.monotonic_ns() - start
            start = time.monotonic_ns()
            api("/snapshot/load", "PUT", {"snapshot_path": transaction + "/state",
                                          "mem_backend": {"backend_type": "File", "backend_path": "/dev/dax0.0"},
                                          "network_overrides": [{"iface_id": "eth0", "host_dev_name": "cxlmpt0"}],
                                          "resume_vm": False})
            stages["destination_load_api"] = time.monotonic_ns() - start
            guest_command(peer_vm, f"ip route del 10.88.0.0/24 via 172.30.0.1 dev eth0\nrm {transaction}/route88\n"
                          f"ip route del 10.89.0.0/24 via 172.30.0.1 dev eth0\nrm {transaction}/route89\n"
                          f"ifconfig cxlmpt0 hw ether {mac[1]}\n"
                          "ifconfig cxlmpt0 10.88.0.1 netmask 255.255.255.0 up\n"
                          "ifconfig cxlmpt0:1 10.89.0.1 netmask 255.255.255.0 up\n")
            text = guest_command(peer_vm, f"printf 'DESTINATION_FC='; cat {transaction}/firecracker.pid")
            destination_pid = re.search(r"^DESTINATION_FC=(\d+) (\d+)\s*$", text, re.MULTILINE)
            if not destination_pid:
                raise RuntimeError("Missing destination process identity")
            report["handoff"]["processes"]["destination"] = {"role": "B", "pid": int(destination_pid[1]), "starttime": destination_pid[2]}
            start = time.monotonic_ns()
            api("/vm", "PATCH", {"state": "Resumed"})
            guest_command(peer_vm, "/lab/shared-probe dax-word B /dev/dax0.0 69206016 1")
            stages["destination_resume_and_gate"] = time.monotonic_ns() - start
        deadline = time.monotonic() + 60 + args.rounds * args.interval_us / 250000
        pause_done = not args.pause_ms
        while time.monotonic() < deadline:
            text = guest_command(active_vm, f"tail -n 4 {transaction}/{active_log}.log")
            if "L2_NETWORK_RESULT=failed" in text:
                raise RuntimeError("Guest network test failed")
            if "L2_NETWORK_RESULT=passed" in text:
                break
            if not pause_done:
                # Pause only after the peer has observed verified application traffic.
                ready = guest_command(peer_vm, f"grep '\"event\":\"ready\"' {transaction}/server.log || true")
                if re.search(r'^MPTCP_JSON .*"event":"ready"', ready, re.MULTILINE):
                    api("/vm", "PATCH", {"state": "Paused"})
                    time.sleep(args.pause_ms / 1000)
                    api("/vm", "PATCH", {"state": "Resumed"})
                    pause_done = True
            time.sleep(.1)
        else:
            raise RuntimeError("Guest network test timed out")
        if not pause_done:
            raise RuntimeError("Test completed before requested pause")
        logs = [("A", source, "firecracker"), ("B", peer_vm, "server")]
        if args.snapshot_handoff:
            logs.append(("A", peer_vm, "firecracker-destination"))
        for role, vm, name in logs:
            data, integrity = guest_fetch_file(vm, f"{transaction}/{name}.log")
            (out / f"{role}-{name}.log").write_bytes(data)
            report["artifact_integrity"][name] = integrity
            report["events"].setdefault(role, []).extend(
                json.loads(line[len("MPTCP_JSON "):]) for line in data.decode(errors="replace").splitlines()
                if line.startswith("MPTCP_JSON "))
        for role in ("A", "B"):
            complete = [event for event in report["events"][role] if event.get("event") == "complete"]
            if (len(complete) != 1 or complete[0].get("errors") != 0 or
                    complete[0].get("rounds") != args.rounds or complete[0].get("nonce") != nonce):
                raise RuntimeError(f"Peer {role} did not complete verified exchanges")
            if args.protocol == "mptcp" and not any(event.get("event") == "ready" and event.get("fallback") is False
                                                    for event in report["events"][role]):
                raise RuntimeError(f"Peer {role} did not confirm native MPTCP")
            if args.protocol == "mptcp":
                ready = next(event for event in report["events"][role] if event.get("event") == "ready")
                if (complete[0].get("token") != ready.get("token") or
                        complete[0].get("socket_cookie") != ready.get("socket_cookie") or
                        complete[0].get("connect_attempts") != (1 if role == "A" else 0)):
                    raise RuntimeError(f"Peer {role} did not preserve its original MPTCP connection")
        if args.switch_path:
            switched = [event for event in report["events"]["A"] if event.get("paths") and
                        {path.get("local_ip") for path in event["paths"]
                         if path.get("tcp_state") == 1} == {"10.89.0.2"}]
            if not switched:
                raise RuntimeError("No MPTCP state observed with only the new source-address subflow established")
            final = next(event for event in report["events"]["A"] if event.get("event") == "final_paths")
            old = [path for path in final["paths"] if path.get("local_ip") == "10.88.0.2"]
            new = [path for path in final["paths"]
                   if path.get("local_ip") == "10.89.0.2" and path.get("tcp_state") == 1]
            if (any(path.get("tcp_state") == 1 for path in old) or not new or
                    not all(path.get("bytes_sent", 0) > 0 and path.get("bytes_received", 0) > 0 for path in new)):
                raise RuntimeError("Final new MPTCP subflow did not carry bidirectional data with the old subflow retired")
        report["result"] = "passed"
    except BaseException as exc:
        report.update(result="failed", error=str(exc))
        raise
    finally:
        cleanup_actions = [("A", source, "stop_source_firecracker", stop_recorded("firecracker"))]
        if args.snapshot_handoff:
            cleanup_actions.append(("B", peer_vm, "stop_destination_firecracker", stop_recorded("firecracker")))
        cleanup_actions.extend([
            ("B", peer_vm, "stop_server", stop_recorded("server")),
            ("A", source, "restore_ip_forward", f"if [ -f {transaction}/ip_forward ]; then cat {transaction}/ip_forward > /proc/sys/net/ipv4/ip_forward; fi"),
            ("A", source, "delete_source_tap", "if [ -d /sys/class/net/cxlmpt0 ]; then ip link delete cxlmpt0; fi"),
            ("B", peer_vm, "delete_route88", f"if [ -f {transaction}/route88 ]; then ip route del 10.88.0.0/24 via 172.30.0.1 dev eth0; fi"),
            ("B", peer_vm, "delete_route89", f"if [ -f {transaction}/route89 ]; then ip route del 10.89.0.0/24 via 172.30.0.1 dev eth0; fi"),
            ("B", peer_vm, "delete_old_path_blackhole", f"if [ -f {transaction}/old_path_blackhole ]; then ip route del blackhole 10.88.0.2/32; fi"),
            ("B", peer_vm, "restore_mptcp_enabled", f"if [ -f {transaction}/mptcp_enabled ]; then cat {transaction}/mptcp_enabled > /proc/sys/net/mptcp/enabled; fi"),
        ])
        if args.snapshot_handoff:
            cleanup_actions.append(("B", peer_vm, "delete_destination_tap", "if [ -d /sys/class/net/cxlmpt0 ]; then ip link delete cxlmpt0; fi"))
        for role, vm, action, script in cleanup_actions:
            if role not in initialized:
                continue
            try:
                guest_command(vm, script, timeout=15)
            except Exception as exc:
                report["cleanup_errors"].append({"role": role, "action": action, "error": str(exc)})
        if report["cleanup_errors"]:
            report["result"] = "failed"
            report.setdefault("error", "MPTCP test cleanup failed")
        if report["result"] == "failed":
            # Processes have been stopped before collecting partial logs, including a failed restore.
            diagnostic_logs = [("A", source, "firecracker"), ("B", peer_vm, "server")]
            if args.snapshot_handoff:
                diagnostic_logs.append(("B", peer_vm, "firecracker-destination"))
            for role, vm, name in diagnostic_logs:
                if role not in initialized:
                    continue
                try:
                    path = f"{transaction}/{name}.log"
                    present = guest_command(vm, f"if [ -f {path} ]; then echo MPTCP_LOG_PRESENT; fi")
                    if not re.search(r"^MPTCP_LOG_PRESENT\s*$", present, re.MULTILINE):
                        continue
                    data, integrity = guest_fetch_file(vm, path)
                    (out / f"diagnostic-{role}-{name}.log").write_bytes(data)
                    report["artifact_integrity"][f"diagnostic-{role}-{name}"] = integrity
                except Exception as exc:
                    report.setdefault("diagnostic_errors", []).append({"role": role, "log": name, "error": str(exc)})
        save_json(out / "report.json", report)
    if report["cleanup_errors"]:
        raise RuntimeError(f"MPTCP test cleanup failed; inspect {out / 'report.json'}")
    print(json.dumps({"result": report["result"], "report": str(out / "report.json"),
                      "protocol": args.protocol, "switch_path": args.switch_path,
                      "snapshot_handoff": args.snapshot_handoff}, indent=2))


def dax_test(_):
    """Use only device-DAX mmap in guests; test both directions near start and end."""
    run, config = current_run()
    if not config.get("cxl_mib"):
        raise RuntimeError("Start with --cxl-mib 2048 and a QEMU supporting CXL Type-3")
    boot = json.loads((run / "report.json").read_text())
    devices = {role: next(e["device"] for e in records if e["event"] == "dax_shared_memory")
               for role, records in boot["guest_events"].items()}
    for device in devices.values():
        if not re.fullmatch(r"/dev/dax\d+\.\d+", device):
            raise RuntimeError("Invalid DAX device name")
    result = {"result": "passed", "run": str(run), "backing": config["cxl_backing"],
              "bytes": config["cxl_mib"] * 1024 * 1024, "devices": devices, "checks": [], "transcripts": []}

    def word(role, offset, value=None):
        body = f"/lab/shared-probe dax-word {role} {devices[role]} {offset}"
        if value is not None:
            body += f" {value:#x}"
        text = guest_command(config["vms"][role], body)
        result["transcripts"].append(text)
        match = re.search(r"^LAB_DAX_WORD=(0x[0-9a-fA-F]+)\s*$", text, re.MULTILINE)
        if not match:
            raise RuntimeError("Missing DAX read result")
        return int(match[1], 16)

    def host_read(offset):
        with Path(config["cxl_backing"]).open("rb") as stream:
            stream.seek(offset)
            return struct.unpack("<I", stream.read(4))[0]

    for offset in [65536, result["bytes"] - 4]:
        for writer, reader in [("A", "B"), ("B", "A")]:
            value = secrets.randbits(32)
            observed_write = word(writer, offset, value)
            observed_read = word(reader, offset)
            observed_host = host_read(offset)
            if any(v != value for v in [observed_write, observed_read, observed_host]):
                raise RuntimeError(f"CXL DAX {writer}→{reader} mismatch at {offset}")
            result["checks"].append({"writer": writer, "reader": reader, "offset": offset,
                                     "value": hex(value), "peer_read": hex(observed_read), "host_read": hex(observed_host)})
    name = "dax-rw-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    save_json(run / name, result)
    print(json.dumps({k: v for k, v in result.items() if k != "transcripts"}, indent=2))
    print(f"Report: {run / name}")


def ram_test(_):
    """Check the complete RAM VMA and guest CPU writes visible through VM B."""
    run, config = current_run()
    if not config.get("dax_guest"):
        raise RuntimeError("Start with --dax-guest --cxl-mib 2048 and the patched QEMU")
    offset, size, counter_gpa = 2097152, 128 * 1024 * 1024, 0x4000000
    transcripts = {}
    text = guest_command(config["vms"]["A"], """pid=$(cat /run/firecracker.pid)
kill -0 "$pid"
grep /dev/dax /proc/$pid/maps
grep 'DAX_RAM ' /run/l2.log
grep '^L2_BOOTED' /run/l2.log
""")
    transcripts["A_mapping"] = text
    mappings = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]+)-([0-9a-f]+)\s+(\S+)\s+([0-9a-f]+)\s+\S+\s+\d+\s+(/dev/dax\d+\.\d+)", line.strip())
        if match:
            mappings.append({"hva_start": int(match[1], 16), "hva_end": int(match[2], 16),
                             "permissions": match[3], "offset": int(match[4], 16), "device": match[5]})
    ram_mappings = [mapping for mapping in mappings if mapping["offset"] == offset]
    if len(ram_mappings) != 1 or ram_mappings[0]["hva_end"] - ram_mappings[0]["hva_start"] != size or ram_mappings[0]["permissions"] != "rw-s":
        raise RuntimeError("Firecracker does not have exactly the expected shared 128MiB DAX RAM VMA")
    device = ram_mappings[0]["device"]
    if not re.search(r"DAX_RAM .*offset=2097152 gpa=0 bytes=134217728", text):
        raise RuntimeError("Missing allocation record for the entire guest RAM")

    def sample():
        for attempt in range(5):
            observed = guest_command(config["vms"]["B"],
                                     f"/lab/shared-probe dax-word B {device} {offset + counter_gpa}")
            transcripts[f"B_sample_{len(transcripts)}"] = observed
            match = re.search(r"^LAB_DAX_WORD=(0x[0-9a-fA-F]+)\s*$", observed, re.MULTILINE)
            if not match:
                raise RuntimeError("No DAX counter read from observer VM")
            value = int(match[1], 16)
            with Path(config["cxl_backing"]).open("rb") as backing:
                backing.seek(offset + counter_gpa)
                host_value = struct.unpack("<I", backing.read(4))[0]
            if value == host_value and value > 0:
                return {"B_read": value, "host_read": host_value}
        raise RuntimeError("Observer and backing counter did not agree")

    first = sample()
    time.sleep(2)
    second = sample()
    if second["B_read"] <= first["B_read"]:
        raise RuntimeError("The guest CPU counter did not advance")
    trees = {}
    for role, vm in config["vms"].items():
        tree = qmp(vm["qmp"], "human-monitor-command", {"command-line": "info mtree -f"})
        lines = [line.strip() for line in tree.splitlines()
                 if "ram): cxl-shared" in line and " KVM" in line
                 and not line.strip().startswith("0000000000000000-")]
        if not lines:
            raise RuntimeError(f"VM {role} CPU-visible CXL window is not RAM")
        trees[role] = lines
    report = {"result": "passed", "run": str(run), "owner": "A", "observer": "B",
              "guest_ram_bytes": size, "dax_offset": offset, "mapping": ram_mappings[0],
              "counter_gpa": hex(counter_gpa), "counter_dax_offset": offset + counter_gpa,
              "counter_first": first, "counter_second": second, "qemu_ram_ranges": trees,
              "transcripts": transcripts}
    save_json(run / "dax-guest-ram.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "transcripts"}, indent=2))
    print(f"Report: {run / 'dax-guest-ram.json'}")


def kvm_state_test(_):
    """Observe actual DAX allocations and read kernel pointer fields through B."""
    run, config = current_run()
    if not config.get("dax_kvm_state"):
        raise RuntimeError("Start with --dax-guest --dax-kvm-state --cxl-mib 2048")
    transcript = guest_command(config["vms"]["A"], """pid=$(cat /run/firecracker.pid)
kill -0 "$pid"
cat /sys/module/kvm/parameters/dax_pool
dmesg | grep KVM_DAX_
grep /dev/dax /proc/$pid/maps
grep -E 'DAX_HEAP |DAX_VMM_OBJECT ' /run/l2.log
""")
    header = re.search(r"enabled=1 phys=([0-9a-f]+) bytes=(\d+) available=(\d+) failures=(\d+)", transcript)
    if not header or int(header[2]) != 134217728 or int(header[4]):
        raise RuntimeError("KVM DAX arena is not active or has allocation failures")
    counts = {m[1]: {"live": int(m[2]), "allocs": int(m[3]), "frees": int(m[4]), "bytes": int(m[5])}
              for m in re.finditer(r"^([A-Za-z_]+) live=(\d+) allocs=(\d+) frees=(\d+) bytes=(\d+)\s*$", transcript, re.MULTILINE)}
    required = {"vm", "vcpu", "vmcs", "msr_bitmap", "memslot", "ept_page", "mmu_cache_object",
                "guest_fpu", "kvm_run", "kvm_create_lapic", "kvm_create_pit", "kvm_ioapic_init",
                "kvm_pic_init", "kvm_set_irq_routing", "cpuid", "alloc_emulate_ctxt"}
    if any(not counts.get(kind, {}).get("live") for kind in required):
        raise RuntimeError("Missing live KVM objects in the DAX arena")
    fields = {m[1]: {"storage_offset": int(m[2]), "value": int(m[3], 16), "target": m[4]}
              for m in re.finditer(r"KVM_DAX_FIELD field=(\S+) storage_offset=(\d+) value=([0-9a-f]+) target=(\w+)", transcript)}
    pool_refs = {m[1]: {"target": m[2], "address": int(m[3], 16), "offset": int(m[4])}
                 for m in re.finditer(r"KVM_DAX_REF field=(\S+) target=(\w+) kva=([0-9a-f]+) offset=(\d+)", transcript)}
    if any(pool_refs.get(name, {}).get("target") != "pool" for name in ("vm", "vcpu", "ept.root")):
        raise RuntimeError("VM/vCPU/EPT root is outside the DAX arena")
    stable = ["vm.mm", "vcpu.kvm", "vcpu.run", "vcpu.apic", "vcpu.cpuid", "vcpu.fpu",
              "vcpu.emulate_ctxt", "vmx.vmcs", "vmx.msr_bitmap", "lapic.regs"]
    script = []
    for name in stable:
        field = fields[name]
        offset = 268435456 + field["storage_offset"]
        script.append(f"echo POINTER_FIELD={name}")
        for word in (offset, offset + 4):
            script.append(f"/lab/shared-probe dax-word B /dev/dax0.0 {word}")
    observed = guest_command(config["vms"]["B"], "\n".join(script), timeout=30)
    reads = re.findall(r"^POINTER_FIELD=(\S+)\s*\nLAB_DAX_WORD=(0x[0-9a-fA-F]+)\s*\nLAB_DAX_WORD=(0x[0-9a-fA-F]+)", observed, re.MULTILINE)
    if len(reads) != len(stable):
        raise RuntimeError("Missing kernel pointer reads from VM B")
    checked = {}
    with Path(config["cxl_backing"]).open("rb") as backing:
        for name, low, high in reads:
            value = int(low, 16) | (int(high, 16) << 32)
            backing.seek(268435456 + fields[name]["storage_offset"])
            host_value = struct.unpack("<Q", backing.read(8))[0]
            if value != fields[name]["value"] or value != host_value:
                raise RuntimeError(f"Kernel pointer {name} differs between A, B and backing")
            checked[name] = {"value": hex(value), "target": fields[name]["target"], "B_and_host_match": True}
        heap = re.search(r"DAX_HEAP path=/dev/dax0.0 offset=536870912 bytes=67108864 base=(0x[0-9a-f]+)", transcript)
        if not heap:
            raise RuntimeError("Missing Firecracker DAX heap allocation")
        heap_base = int(heap[1], 16)
        objects = {m[1]: int(m[2], 16) for m in re.finditer(r"DAX_VMM_OBJECT name=(\w+) addr=(0x[0-9a-f]+)", transcript)}
        if any(not heap_base <= objects.get(name, 0) < heap_base + 67108864 for name in ("Vmm", "Vcpu")):
            raise RuntimeError("Firecracker VM/vCPU wrapper is outside its DAX heap")
        backing.seek(536870912)
        magic, base, length, head, allocs, frees, live, failures = struct.unpack("<8Q", backing.read(64))
        if magic != 0x4643444158484541 or base != heap_base or length != 67108864 or not allocs or not live or failures:
            raise RuntimeError("Invalid Firecracker heap control or allocation failure")
    if not re.search(r"rw-s\s+20000000\s+\S+\s+\d+\s+/dev/dax0.0", transcript):
        raise RuntimeError("Missing shared DAX heap VMA")
    report = {"result": "passed", "run": str(run), "kernel_arena": {"phys": hex(int(header[1], 16)),
              "offset": 268435456, "bytes": int(header[2]), "available": int(header[3]), "failures": 0},
              "allocations": counts, "object_refs": pool_refs, "pointer_fields": fields,
              "cross_vm_pointer_reads": checked, "firecracker_heap": {"offset": 536870912,
              "bytes": length, "base": hex(base), "allocs": allocs, "frees": frees,
              "live_bytes": live, "failures": failures, "objects": {k: hex(v) for k, v in objects.items()}},
              "host_refs": [name for name, field in fields.items() if field["target"] == "host"],
              "limitations": ["Single execution owner; no handoff or serialization.",
                              "Host mm/task/PID/FD/per-CPU data, kernel page metadata and host thread stacks are not moved.",
                              "Kernel binding references remain host-specific even when the containing object is DAX-backed."],
              "transcripts": {"A": transcript, "B": observed}}
    save_json(run / "kvm-dax-state.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("transcripts", "pointer_fields", "allocations")}, indent=2))
    print(f"Report: {run / 'kvm-dax-state.json'}")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    p = subs.add_parser("prepare", help="Build diskless guests and static probe; no host installation")
    p.add_argument("--bootstrap-qemu", action="store_true", help="Extract Ubuntu 22.04 QEMU packages locally")
    p.add_argument("--busybox", type=Path)
    p.add_argument("--l1-kernel", type=Path)
    p.add_argument("--module-dir", type=Path, help="Default: locally built DCD modules, otherwise host modules")
    p.add_argument("--firecracker", type=Path, default=REPO / "firecracker/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker")
    p.add_argument("--l2-kernel", type=Path, help="Default: locally built DCD vmlinux, otherwise Linux 6.1.188")
    p.set_defaults(func=prepare)
    p = subs.add_parser("start", help="Start two VMs; leave both outer and inner VMs running")
    p.add_argument("--vcpus", type=int, default=2)
    p.add_argument("--memory-mib", type=int, default=1024)
    p.add_argument("--shared-mib", type=int, default=64)
    p.add_argument("--memory-backing-dir", type=Path,
                   help="Create owned backing files here, e.g. /dev/shm for tmpfs; remove them on stop")
    p.add_argument("--rounds", type=int, default=1000)
    p.add_argument("--qemu", type=Path, help="Use an explicit QEMU binary, e.g. one with CXL Type-3 support")
    p.add_argument("--cxl-mib", type=int, default=0, help="Add shared CXL Type-3 RAM exposed as device-DAX (e.g. 2048)")
    p.add_argument("--dax-guest", action="store_true", help="Run one microVM on A with all 128MiB guest RAM in DAX; B observes")
    p.add_argument("--dax-kvm-state", action="store_true", help="Use the experimental KVM device-DAX state allocator on VM A")
    p.add_argument("--dax-handoff", action="store_true", help="Direct shared KVM handoff lab; fixed kernel addresses, 4KiB DAX, no automatic L2")
    p.add_argument("--dax-4k", action="store_true", help="Configure the shared device-DAX allocation with 4KiB alignment for matched EPT measurements")
    p.add_argument("--migration-network", action="store_true", help="Connect L1 A/B with a private virtio-net link for full RAM migration")
    p.add_argument("--network-backend", choices=["socket", "tap"], default="socket", help="Use a QEMU socket or isolated host TAP/bridge with vhost-net (default socket)")
    p.add_argument("--socket-listener", choices=["A", "B"], default="A", help="Choose the QEMU socket listener; starts it before the connecting peer (default A)")
    p.add_argument("--storage-image", type=Path, help="Attach the same read-only raw disk to both L1s for storage experiments; QEMU uses cache=none")
    p.set_defaults(func=start)
    p = subs.add_parser("verify", help="Wait for guest checks and validate host backing")
    p.add_argument("--timeout", type=float, default=180)
    p.set_defaults(func=verify)
    p = subs.add_parser("configure", help="Inspect selected native KVM pair; settings change only with explicit options")
    p.add_argument("--nx-huge-pages", choices=["auto", "never"],
                   help="Reload KVM inside both L1s, stopping their recorded microVMs; never disables this mitigation")
    p.add_argument("--rcu", choices=["normal", "expedited"],
                   help="Set grace-period policy inside both L1s; no physical host changes")
    p.add_argument("--pml", choices=["on", "off"],
                   help="Reload Intel KVM inside the selected L1s with PML enabled/disabled")
    p.set_defaults(func=configure)
    for name, function in [("status", status), ("stop", stop)]:
        subs.add_parser(name).set_defaults(func=function)
    p = subs.add_parser("shared-test", help="Live A→B and B→A writes through the shared PCI BAR")
    p.add_argument("--offset", type=int, default=65536, help="Byte offset; keep initial 8KiB mailboxes intact")
    p.set_defaults(func=shared_test)
    subs.add_parser("dax-test", help="Live A↔B mmap through CXL device-DAX at start/end offsets").set_defaults(func=dax_test)
    subs.add_parser("ram-test", help="Verify all guest RAM is DAX-backed and VM B sees guest CPU writes").set_defaults(func=ram_test)
    subs.add_parser("kvm-state-test", help="Verify native KVM DAX objects, VMM heap and pointer fields observed by B").set_defaults(func=kvm_state_test)
    p = subs.add_parser("mptcp-test", help="Verify DAX-backed guest TCP/MPTCP, with optional A-to-B state-only snapshot handoff")
    p.add_argument("--protocol", choices=["tcp", "mptcp"], default="mptcp")
    p.add_argument("--switch-path", action="store_true", help="Add a guest source address and retire the original MPTCP subflow")
    p.add_argument("--snapshot-handoff", action="store_true", help="Move the paused guest from A to B through a state-only shared-DAX snapshot; requires --switch-path")
    p.add_argument("--rounds", type=int, default=8000)
    p.add_argument("--interval-us", type=int, default=1000)
    p.add_argument("--pause-ms", type=int, default=0, help="Pause/resume the source microVM after traffic starts; no migration")
    p.set_defaults(func=mptcp_test)
    p = subs.add_parser("context-bench", help="Measure blocking futex context switches on one CPU")
    p.add_argument("--scope", choices=["host", "guest"], default="host")
    p.add_argument("--role", choices=["A", "B"], default="B")
    p.add_argument("--cpu", type=int)
    p.add_argument("--roundtrips", type=int, default=100000)
    p.add_argument("--samples", type=int, default=9)
    p.set_defaults(func=context_bench)
    p = subs.add_parser("cache-bench", help="Measure writer writeback, reader invalidation and PML dirty epochs")
    p.add_argument("--scope", choices=["guest", "host"], default="guest")
    p.add_argument("--memory-mib", type=int, default=1024)
    p.add_argument("--dirty-permyriad", type=int, default=100, help="Dirty pages per 10000 RAM pages")
    p.add_argument("--write-bytes", type=int, choices=[64, 4096], default=64)
    p.add_argument("--mode", choices=["full", "dirty", "none"], default="dirty")
    p.add_argument("--dirty-log", choices=["on", "off"], default="on")
    p.add_argument("--require-pml", action=argparse.BooleanOptionalAction, default=True,
                   help="Reject silent PML fallback when dirty logging is enabled")
    p.add_argument("--iterations", type=int, default=20)
    p.add_argument("--warmup", type=int, default=4)
    p.add_argument("--cpu", type=int, default=1, help="L1 CPU for the controller and its KVM vCPU")
    p.add_argument("--host-cpu-a", type=int, default=0)
    p.add_argument("--host-cpu-b", type=int, default=2)
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(func=cache_bench)
    p = subs.add_parser("migration-bench", help="Full RAM snapshot migration over the private L1-to-L1 network, with near-full retained working set")
    p.add_argument("--memory-mib", type=int, default=128)
    p.add_argument("--iterations", type=int, default=20)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--profile-restore", action="store_true", help="Record nested Firecracker save/RAM dump/KVM creation/restore timings")
    p.add_argument("--trace-ept", action="store_true", help="Diagnostic kprobe/EPT traces; perturbs timing, use separate one-round runs")
    p.add_argument("--trace-ept-detail", action="store_true", help="Imply --trace-ept; split fault/map/PFN/cache/root paths with 12 probes and no per-leaf tracepoints")
    p.set_defaults(func=migration_bench)
    p = subs.add_parser("handoff-bench", help="Measure state-only snapshots and A↔B restore with RAM retained in DAX")
    handoff_parser = p
    p.add_argument("--preset", choices=HANDOFF_PRESETS,
                   help="Matched two-CPU experimental profile, including no seccomp; explicit options override preset values")
    p.add_argument("--memory-mib", type=int, default=128)
    p.add_argument("--dense-working-set", action="store_true", help="Retain and verify a near-full guest working set; measure first and warm page scans after every handoff")
    p.add_argument("--iterations", type=int, default=30)
    p.add_argument("--warmup", type=int, default=6)
    p.add_argument("--mode", choices=["warm", "cold"], default="warm")
    p.add_argument("--heartbeat", choices=["busy", "sleep"], default="busy")
    p.add_argument("--clock-realtime", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--guest-clocksource", choices=["default", "tsc"], default="default")
    p.add_argument("--profile-restore", action="store_true")
    p.add_argument("--profile-ack", action="store_true",
                   help="Record opt-in L1 trace markers and guest ACK TSC; requires configured tracefs collection")
    p.add_argument("--no-seccomp", action="store_true", help="Match the direct runner's Firecracker seccomp setting")
    p.add_argument("--no-nested-guest", action="store_true",
                   help="Hide VMX/SVM from the L2 guest; use the same setting for matched fresh/reuse comparisons")
    p.add_argument("--serial-file", action="store_true",
                   help="Use a file-only guest serial console; enabled automatically for --reuse-kvm")
    p.add_argument("--exact-tsc", action=argparse.BooleanOptionalAction, default=True,
                   help="Restore semantic TSC through the public offset attribute, avoiding KVM's near-zero synchronization heuristic")
    p.add_argument("--direct-cpu-model", action="store_true",
                   help="Match direct handoff's guest CPUID restrictions while keeping semantic snapshot control")
    p.add_argument("--keep-kvm-active", action="store_true", help="Keep an empty VM on each node to avoid first/last-VM hardware enable/disable")
    p.add_argument("--reuse-kvm", action="store_true",
                   help="Reuse already-executed KVM VM/vCPU/thread objects; B boots a disposable same-configuration guest first")
    p.add_argument("--reuse-quiet-irqfd", action="store_true",
                   help="Controlled reuse comparison without IRQFD work flush; requires --reuse-kvm, no external I/O or active PIT")
    p.add_argument("--reuse-prepare", action="store_true",
                   help="Prepare each paused reused worker with one coarse API while the peer guest runs; requires --reuse-kvm")
    p.add_argument("--reuse-prepare-on-demand", action="store_true",
                   help="Test preparation with no advance lead time; requires --reuse-prepare; initial B seed remains prepared")
    p.add_argument("--fixed-ram-hva", type=lambda value: int(value, 0),
                   help="Map DAX RAM at this 2MiB-aligned HVA on every VMM; does not preserve native EPT")
    p.add_argument("--full-snapshot-samples", type=int, choices=range(0, 11), default=6,
                   help="RAM-inclusive snapshot controls before handoffs; 0 skips them (default 6)")
    p.add_argument("--guest-cpu", type=int, help="Pin controller and inherited Firecracker runtime to an L1 CPU")
    p.add_argument("--executor-cpu", type=int, help="Pin Firecracker vCPU thread independently of the controller")
    p.add_argument("--poll-us", type=int, choices=[0, 5, 10, 50], default=50)
    p.add_argument("--spin-us", type=int, choices=range(0, 2001), metavar="0..2000", default=0)
    p.add_argument("--timer-slack-ns", type=int, choices=[0, 1], default=0)
    p.add_argument("--dwell-ms", type=int, choices=range(0, 1001), metavar="0..1000", default=20)
    p.add_argument("--heartbeat-spins", type=int, choices=range(0, 8193), metavar="0..8192", default=128)
    p.add_argument("--heartbeat-churn", action="store_true")
    p.add_argument("--heartbeat-pit", action="store_true")
    p.add_argument("--test-corrupt-state", action="store_true",
                   help="Flip a payload byte after hashing; require rejection before receiver deserialization (1 iteration, 0 warmup)")
    p.set_defaults(func=handoff_bench)
    p = subs.add_parser("direct-handoff-bench", help="Attach the same shared KVM/vCPU/VMCS across independent kernels")
    p.add_argument("--memory-mib", type=int, default=128, help="Linux guest RAM, 128..2048MiB (default 128)")
    p.add_argument("--dense-working-set", action="store_true", help="Retain and verify a near-full guest working set; measure first and warm page scans after every handoff")
    p.add_argument("--kernel-mode", type=int, choices=[0, 2, 3], default=3,
                   help="0 conservative, 2 deferred retirement, 3 retained EPT (default)")
    p.add_argument("--executor", choices=["firecracker", "native"], default="firecracker",
                   help="Native uses the existing probe executor on both nodes after the initial Firecracker boot")
    p.add_argument("--control", choices=["http", "local"], default="http",
                   help="Local uses an opt-in persistent binary socket on Firecracker's VMM event loop")
    p.add_argument("--spin-us", type=int, choices=range(0, 2001), metavar="0..2000", default=0,
                   help="Bounded spin after source preparing notification, then fall back to polling sleeps")
    p.add_argument("--reuse-fds", action="store_true", help="Reattach through an existing VM FD after the first visit")
    p.add_argument("--profile", action="store_true", help="Record optional per-stage kernel timings in reserved DAX metadata")
    p.add_argument("--resume-profile", action="store_true", help="Separate first VM-entry attempt, guest heartbeat timestamp and controller observation")
    p.add_argument("--heartbeat-spins", type=int, choices=range(0, 8193), metavar="0..8192", default=128,
                   help="Guest PAUSE instructions per busy heartbeat loop (default 128); does not change sleep mode")
    p.add_argument("--executor-cpu", type=int, choices=range(0, 2), help="Pin native and Firecracker vCPU threads independently of the controller")
    p.add_argument("--irqfd-batch", action="store_true", help="Quiesce simple IRQFDs synchronously with one SRCU grace period")
    p.add_argument("--resume-nokick", action="store_true", help="Wake an acknowledged paused Firecracker vCPU through its channel without a signal")
    p.add_argument("--channel-spin-us", type=int, choices=range(0, 501), default=0, metavar="0..500", help="Bounded spin before blocking for vCPU stop/resume completion")
    p.add_argument("--timer-slack-ns", type=int, choices=[0, 1], default=0, help="0 keeps native timer slack; 1 disables sleep batching for controller polling")
    p.add_argument("--guest-cpu", type=int,
                   help="Pin the controller and its executor/Firecracker threads to one L1 CPU to measure cross-CPU VMCS costs")
    p.add_argument("--poll-us", type=int, choices=[0, 5, 10, 50], default=50,
                   help="0 busy-spins while waiting for shared-memory notifications; 50 sleeps between checks (default)")
    p.add_argument("--telemetry", choices=["dax", "serial"], default="dax",
                   help="Keep benchmark JSON outside VM state in a reserved DAX range; avoids slow serial log copying")
    p.add_argument("--dwell-ms", type=int, choices=range(0, 1001), metavar="0..1000", default=20,
                   help="Run guest between handoffs; excluded from measured downtime")
    p.add_argument("--test-pfn-mismatch", action="store_true",
                   help="Negative test: B maps different DAX RAM PFNs; attach must reject without running guest")
    p.add_argument("--test-new-pfn", action="store_true",
                   help="Negative CPU test: a new EPT leaf targeting native RAM must fail before installation")
    p.add_argument("--heartbeat-churn", action="store_true",
                   help="Touch a new 1MiB slice of retained 32MiB guest memory after each handoff to exercise new page mappings")
    p.add_argument("--heartbeat-pit", action="store_true",
                   help="Keep guest PIT channel 0 periodic at approximately 1kHz to exercise active timer/worker handoff")
    p.add_argument("--guest", choices=["cpu", "linux"], default="cpu")
    p.add_argument("--heartbeat", choices=["busy", "sleep"], default="busy")
    p.add_argument("--iterations", type=int, default=30)
    p.add_argument("--warmup", type=int, default=6)
    p.set_defaults(func=direct_handoff_bench)
    p = subs.add_parser("direct-handoff-report", help="Read-only recovery after completed handoff log collection timed out")
    p.add_argument("output_dir", type=Path)
    p.add_argument("--legacy-tsc", action="store_true", help="Historical image without raw TSC checks; record known host warnings")
    p.set_defaults(func=direct_handoff_report)
    p = subs.add_parser("console", help="Connect to a guest serial shell")
    p.add_argument("role", choices=["A", "B"])
    p.set_defaults(func=console)
    args = parser.parse_args()
    if args.action == "handoff-bench" and args.preset:
        handoff_parser.set_defaults(**HANDOFF_PRESET_DEFAULTS, **HANDOFF_PRESETS[args.preset])
        args = parser.parse_args()
    if args.action == "start":
        if not args.migration_network and (args.network_backend != "socket" or args.socket_listener != "A"):
            parser.error("network-backend/socket-listener overrides require --migration-network")
        if args.network_backend == "tap" and args.socket_listener != "A":
            parser.error("--socket-listener applies only to --network-backend socket")
        if args.dax_handoff and (not args.dax_guest or args.cxl_mib < 1024 or args.dax_kvm_state):
            parser.error("--dax-handoff requires --dax-guest, --cxl-mib >= 1024, and excludes --dax-kvm-state")
        if args.vcpus < 1 or args.memory_mib < 512 or not 1 <= args.rounds <= 1000000:
            parser.error("Require vcpus >= 1, memory-mib >= 512, and 1 <= rounds <= 1000000")
        if args.shared_mib < 1 or args.shared_mib & (args.shared_mib - 1):
            parser.error("shared-mib must be a positive power of two")
        if args.cxl_mib and (args.cxl_mib < 256 or args.cxl_mib & (args.cxl_mib - 1)):
            parser.error("cxl-mib must be zero or a power of two >= 256")
        if args.dax_4k and not args.cxl_mib:
            parser.error("--dax-4k requires --cxl-mib")
        if args.dax_guest and args.cxl_mib < 256:
            parser.error("dax-guest requires --cxl-mib >= 256")
        if args.dax_kvm_state and (not args.dax_guest or args.cxl_mib < 1024):
            parser.error("dax-kvm-state requires --dax-guest and --cxl-mib >= 1024")
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
