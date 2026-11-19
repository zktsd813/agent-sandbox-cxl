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
    kernel_uapi = Path(os.environ.get("CXL_LAB_KERNEL_SOURCE", str(HERE / "../linux"))) / "include/uapi/linux"
    command(["gcc", "-std=c11", "-O2", "-static", "-pthread", "-Wall", "-Wextra", "-Werror",
             "-DCXL_KVM_DAX_HEADER=" + json.dumps(str(kernel_uapi / "kvm_dax.h")),
             HERE / "shared_probe.c", "-o", ASSETS / "shared-probe"])
    command(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", HERE/"control_rpc.c", "-o", ASSETS/"control-rpc"])
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
                             (ASSETS / "shared-probe", "shared-probe"), (ASSETS/"control-rpc", "control-rpc")]:
            shutil.copy2(source, l1 / "lab" / name)
        modules = copy_modules(args.module_dir, l1 / "modules", vendor)
        (l1 / "lab/modules.list").write_text("\n".join(modules) + "\n")
        pack_initramfs(l1, ASSETS / "l1-initramfs.cpio")
    shutil.copy2(l1_kernel, ASSETS / "l1-kernel")
    inputs = {"l1_kernel": l1_kernel, "firecracker": args.firecracker,
              "l2_kernel": args.l2_kernel, "busybox": busybox,
              "probe_source": HERE / "shared_probe.c", "l1_init": HERE / "guest-init.sh",
              "l2_init": HERE / "l2-init.sh", "cxl_init": HERE / "guest-cxl.sh"}
    if l1_kernel.resolve() == (KERNEL_BUILD / "arch/x86/boot/bzImage").resolve():
        inputs["kernel_config"] = KERNEL_BUILD / ".config"
        inputs["kernel_source_metadata"] = HERE / "kernel-source.json"
        if (HERE / "cxl-static-capacity.patch").is_file():
            inputs["kernel_local_patch"] = HERE / "cxl-static-capacity.patch"
            inputs["kernel_cxl_hdm"] = kernel_uapi.parents[2] / "drivers/cxl/core/hdm.c"
            inputs["kernel_cxl_mbox"] = kernel_uapi.parents[2] / "drivers/cxl/core/mbox.c"
    manifest = {"control_rpc":{"operations":[1,2,3,4]}, "vendor": vendor, "l1_kernel_release": release, "modules": modules,
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
    if config.get("ephemeral_memory_dir"):
        deadline = time.monotonic() + 5
        while any(alive(vm) for vm in config["vms"].values()) and time.monotonic() < deadline:
            time.sleep(.05)
        if any(alive(vm) for vm in config["vms"].values()):
            raise RuntimeError("A lab QEMU is still alive; preserving its network and memory backing")
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
        if directory.exists() and directory.name.startswith("cxl-lab-") and not directory.is_symlink():
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

def ept_adopt_bench(args):
    import ept_adopt
    ept_adopt.validate_arguments(args, argparse.ArgumentParser())
    ept_adopt.run_existing(args)

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

def start(args):
    """Start only this lab's two L1s, with a private socket network and CXL file."""
    no_active_run()
    host_kvm()
    hardware=output(["numactl", "--hardware"])
    free=int(re.search(r"node 0 free: (\d+) MB",hardware)[1])
    if free < 2*args.memory_mib+args.cxl_mib+args.shared_mib+4096:
        raise RuntimeError("node 0 free memory is below the lab budget plus 4 GiB")
    prepared=json.loads((ASSETS/"manifest.json").read_text())
    if not prepared.get("control_rpc"):raise RuntimeError("Prepare assets with the lab RPC first")
    if prepared["vendor"]!="intel":raise RuntimeError("EPT lab requires Intel VMX")
    if Path("/sys/module/kvm_intel/parameters/nested").read_text().strip().lower() not in ("y","1"):
        raise RuntimeError("Nested VMX is disabled")
    qemu=args.qemu.resolve() if args.qemu else find_qemu()
    if "x-direct-ram" not in output([qemu,"-device","cxl-type3,help"],env=qemu_env(qemu)):
        raise RuntimeError("QEMU needs qemu-cxl-direct-ram.patch")
    storage=args.home_storage_image.resolve(strict=True)
    if not storage.is_file() or "," in str(storage):raise RuntimeError("Invalid storage image")
    runs=LOCAL/"runs";runs.mkdir(exist_ok=True)
    run=Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-"),dir=runs))
    run.chmod(0o700)
    control=Path(tempfile.mkdtemp(prefix="qvl-",dir="/tmp"))
    memory=Path(tempfile.mkdtemp(prefix="cxl-lab-",dir=args.memory_backing_dir))
    backing=memory/"shared-memory.bin";cxl=memory/"cxl-memory.bin"
    for path,mib in ((backing,args.shared_mib),(cxl,args.cxl_mib)):
        with path.open("xb") as stream:stream.truncate(mib*1024**2)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    nonce=secrets.token_hex(8)
    private=run/"home-storage.qcow2"
    command(["qemu-img","create","-q","-f","qcow2","-F","raw","-b",storage,private])
    config=dict(run=str(run),control_dir=str(control),nonce=nonce,rounds=args.rounds,
        l1_vcpus=args.vcpus,l1_memory_mib=args.memory_mib,shared_mib=args.shared_mib,
        backing=str(backing),ephemeral_memory_dir=str(memory),cxl_mib=args.cxl_mib,cxl_backing=str(cxl),
        qemu=str(qemu),qemu_sha256=sha256(qemu),qemu_version=output([qemu,"--version"],env=qemu_env(qemu)).splitlines()[0],
        ept_adopt=True,prepared=prepared,assets=str(ASSETS),host_kernel=os.uname().release,vms={},
        node0_hardware=hardware,
        home_storage_image=dict(template=str(storage),template_sha256=prepared["artifacts"].get(storage.name),
            path=str(private),runtime_rehashed=False,owner="A",readonly=False,format="qcow2",cache="none",guest_device="/dev/vda"),
        network=dict(backend="socket",ips={"A":"172.30.0.1","B":"172.30.0.2"},host_port=port),
        source_patches={name:sha256(HERE/name) for name in ("kvm-dax-state.patch","firecracker.patch","qemu-cxl-direct-ram.patch")})
    save_json(run/"run.json",config);save_json(CURRENT,{"run":str(run)})
    try:
        for role in ("A","B"):
            prefix=run/("vm-"+role.lower());console=str(control/(role.lower()+".console.sock"))
            monitor=str(control/(role.lower()+".qmp.sock"));channel=str(control/(role.lower()+".command.sock"))
            cmd=[str(qemu),"-name","nested-lab-"+role,"-machine","q35,accel=kvm,cxl=on",
                "-cpu","host,vmx=on,xfd=off,amx-tile=off,amx-bf16=off,amx-int8=off",
                "-smp",str(args.vcpus),"-m",f"{args.memory_mib}M,maxmem={args.memory_mib+args.cxl_mib}M,slots=8",
                "-nodefaults","-no-user-config","-display","none","-monitor","none","-no-reboot",
                "-kernel",str(ASSETS/"l1-kernel"),"-initrd",str(ASSETS/"l1-initramfs.cpio"),
                "-append",f"console=ttyS0 loglevel=4 rdinit=/init panic=1 lab.role={role} lab.nonce={nonce} lab.rounds={args.rounds} lab.cxl_bytes={args.cxl_mib*1024**2} lab.ip={config['network']['ips'][role]} lab.pml={int(args.pml=='on')}",
                "-object",f"memory-backend-file,id=shared,size={args.shared_mib}M,mem-path={backing},share=on",
                "-device","ivshmem-plain,memdev=shared",
                "-chardev",f"socket,id=console,path={console},server=on,wait=off,logfile={prefix}.serial.log",
                "-serial","chardev:console","-qmp",f"unix:{monitor},server=on,wait=off",
                "-device","virtio-serial-pci,id=labcontrol","-chardev",f"socket,id=labcmd,path={channel},server=on,wait=off",
                "-device","virtserialport,bus=labcontrol.0,nr=1,chardev=labcmd,name=cxl.control",
                "-netdev",f"socket,id=peer,{'listen' if role=='A' else 'connect'}=127.0.0.1:{port}",
                "-device","virtio-net-pci,romfile=,netdev=peer,mac=52:54:00:ca:10:"+("01" if role=='A' else "02"),
                "-object",f"memory-backend-file,id=cxl-shared,size={args.cxl_mib}M,mem-path={cxl},share=on",
                "-device","pxb-cxl,bus_nr=12,bus=pcie.0,id=cxl.0",
                "-device","cxl-rp,port=0,bus=cxl.0,id=cxl-rp0,chassis=0,slot=2",
                "-device","cxl-type3,bus=cxl-rp0,volatile-memdev=cxl-shared,id=cxl-mem0,sn=0x100,x-direct-ram=on",
                "-machine",f"cxl-fmw.0.targets.0=cxl.0,cxl-fmw.0.size={args.cxl_mib}M,cxl-fmw.0.interleave-granularity=256"]
            if role=="A":cmd += ["-drive",f"file={private},if=none,id=home-storage,format=qcow2,cache=none","-device","virtio-blk-pci,drive=home-storage,bus=pcie.0"]
            firmware=next((p for p in (qemu.parent.parent/"share/qemu",qemu.parent.parent/"src/pc-bios",QEMU_ROOT/"usr/share/qemu") if (p/"bios-256k.bin").is_file()),None)
            if firmware is None:raise RuntimeError("QEMU BIOS is missing")
            cmd += ["-L",str(firmware),"-bios",str(firmware/"bios-256k.bin")]
            with Path(str(prefix)+".qemu.log").open("wb") as log:
                process=subprocess.Popen(cmd,env=qemu_env(qemu),stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            identity=process_identity(process.pid)
            if identity is None:raise RuntimeError(f"QEMU {role} exited at launch")
            vm=dict(pid=process.pid,starttime=identity,command=cmd,console=console,qmp=monitor,
                serial_log=str(prefix)+".serial.log",control_rpc=channel,control_nonce=nonce,control_rpc_operations=[1,2,3,4])
            config["vms"][role]=vm;save_json(run/"run.json",config)
            deadline=time.monotonic()+20
            while not Path(monitor).exists():
                if not alive(vm) or time.monotonic()>deadline:raise RuntimeError(f"QEMU {role} failed before QMP")
                time.sleep(.05)
            if not qmp(monitor,"query-kvm").get("enabled"):raise RuntimeError("QEMU lacks KVM")
        print(f"Started EPT lab: {run}")
    except BaseException:
        stop_config(config);raise


def verify(args):
    run,config=current_run();deadline=time.monotonic()+args.timeout
    required={"l1_kvm","shared_memory","dax_shared_memory","ept_ready","lab_network"}
    while True:
        records={role:events(Path(vm["serial_log"])) for role,vm in config["vms"].items()}
        if any(e["event"]=="failure" for rows in records.values() for e in rows):raise RuntimeError("L1 boot check failed")
        if len(records)==2 and all(required <= {e["event"] for e in rows if e.get("result")=="passed"} for rows in records.values()):break
        if any(not alive(vm) for vm in config["vms"].values()) or time.monotonic()>deadline:raise RuntimeError(f"L1 verification failed: {run}")
        time.sleep(.25)
    for role,rows in records.items():
        for event in rows:
            if event.get("role")!=role:raise RuntimeError("L1 role mismatch")
            if event["event"] in ("shared_memory","dax_shared_memory") and event["nonce"]!=config["nonce"]:raise RuntimeError("L1 nonce mismatch")
        text=Path(config["vms"][role]["serial_log"]).read_text()
        if f"LAB_BOOT role={role} kernel={config['prepared']['l1_kernel_release']}" not in text:raise RuntimeError("L1 kernel mismatch")
    save_json(run/"verification.json",dict(result="passed",guest_events=records))
    print("EPT lab checks passed")


def guest_command(vm,body,timeout=15):
    import control_rpc
    if not alive(vm):raise RuntimeError("VM is not running")
    return control_rpc.command(vm,body,timeout)


def fetch_file(vm,path):
    import control_rpc
    compressed=control_rpc.call(vm,1,("set -e\ngzip -c "+shlex.quote(path)+"\n").encode(),60,binary=True)
    data=gzip.decompress(compressed)
    return data,dict(path=path,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),gzip_bytes=len(compressed),transport="lab RPC")


def process_cleanup_script():
    """PID-incarnation fence used only for processes this runner recorded."""
    return r'''same_worker_process() {
    [ -r "/proc/$target_pid/stat" ] || return 1
    process_stat=$(cat "/proc/$target_pid/stat") || return 1
    process_fields=${process_stat##*) }; set -- $process_fields
    [ "$#" -ge 20 ] || return 1
    process_state=$1; shift 19
    [ "$1" = "$expected_start" ] && [ "$process_state" != Z ]
}
stop_worker_process() {
    if same_worker_process; then
        kill -TERM "$target_pid" 2>/dev/null || true
        attempt=0
        while same_worker_process && [ "$attempt" -lt 100 ]; do sleep .02; attempt=$((attempt+1)); done
        if same_worker_process; then kill -KILL "$target_pid" 2>/dev/null || true; fi
        attempt=0
        while same_worker_process && [ "$attempt" -lt 100 ]; do sleep .02; attempt=$((attempt+1)); done
        ! same_worker_process
    fi
}
'''


def configure(args):
    _,config=current_run()
    for vm in config["vms"].values():
        print(guest_command(vm,"cat /sys/module/kvm/parameters/nx_huge_pages\ncat /sys/module/kvm_intel/parameters/pml"))


def main():
    p=argparse.ArgumentParser(description=__doc__);subs=p.add_subparsers(dest="command",required=True)
    q=subs.add_parser("prepare")
    q.add_argument("--firecracker",type=Path,required=True);q.add_argument("--busybox",type=Path)
    q.add_argument("--l1-kernel",type=Path);q.add_argument("--l2-kernel",type=Path);q.add_argument("--module-dir",type=Path)
    q.add_argument("--bootstrap-qemu",action="store_true");q.set_defaults(func=prepare)
    q=subs.add_parser("start")
    q.add_argument("--vcpus",type=int,default=4);q.add_argument("--memory-mib",type=int,default=4096)
    q.add_argument("--shared-mib",type=int,default=64);q.add_argument("--cxl-mib",type=int,choices=(4096,8192,16384),default=4096)
    q.add_argument("--memory-backing-dir",type=Path,default=Path("/dev/shm"));q.add_argument("--qemu",type=Path)
    q.add_argument("--home-storage-image",type=Path,required=True);q.add_argument("--rounds",type=int,default=1000)
    q.add_argument("--pml",choices=("on","off"),default="on");q.set_defaults(func=start)
    q=subs.add_parser("verify");q.add_argument("--timeout",type=float,default=180);q.set_defaults(func=verify)
    for name,function in [("status",status),("stop",stop),("configure",configure)]:subs.add_parser(name).set_defaults(func=function)
    q=subs.add_parser("console");q.add_argument("role",choices=("A","B"));q.set_defaults(func=console)
    q=subs.add_parser("ept-adopt-bench")
    from ept_adopt import add_arguments
    add_arguments(q);q.set_defaults(func=ept_adopt_bench)
    args=p.parse_args();args.func(args)

if __name__=="__main__":main()
