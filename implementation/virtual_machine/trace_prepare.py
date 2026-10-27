#!/usr/bin/env python3
"""Package exact recorded SWE commands and existing immutable disks for the CXL lab.

No Docker service, host mounts, or input images are modified. The writable root
is a guest tmpfs overlay; its contents migrate with the shared guest RAM.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

import lab

HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
BOUNDARIES = {"gin-gonic__gin-3741": 12, "php-cs-fixer__php-cs-fixer-8331": 21,
              "micropython__micropython-13039": 26}
TRACE_HASHES = {
    "gin-gonic__gin-3741": "5837d6d1dc2e53031ed8c4e11c122fd1f822097c0735c741ece201285947f997",
    "php-cs-fixer__php-cs-fixer-8331": "bbcf3a9cd07fc7d3c324f5c7d3338309ff88e0b126a3bd91e9e05ac62397a022",
    "micropython__micropython-13039": "2d7e8554e2c813758727a7bea75588a99a473f9d2cf70ec8280856a9d0519f84",
}
# External commands used by both init scripts and generated command launchers.
# Shell builtins (read, printf, kill, wait, test, and so on) are not applets here.
REQUIRED_APPLETS = frozenset("awk cat chmod chroot cp dmesg env gzip hostname ip mkdir mount "
                            "readlink rm sed setsid sh sha256sum sleep stat stty sync tar uuencode".split())


def checked(argv, **kwargs):
    subprocess.run([str(a) for a in argv], check=True, **kwargs)


def prepare(args):
    destination = args.assets.resolve()
    source = args.source_repo.resolve()
    baseline = args.base_assets.resolve()
    baseline_manifest = json.loads((baseline / "manifest.json").read_text())
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite assets: {destination}")
    if len(args.names) != len(set(args.names)) or not set(args.names) <= BOUNDARIES.keys():
        raise ValueError("Choose distinct supported trace names")
    parser_path = HERE.parents[1] / "harness/trace-replay/experiment.py"
    spec = importlib.util.spec_from_file_location("trace_source", parser_path)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    busybox = (args.busybox or Path(shutil.which("busybox"))).resolve()
    applets = set(subprocess.check_output([str(busybox), "--list"], text=True).splitlines())
    if missing := REQUIRED_APPLETS - applets:
        raise RuntimeError(f"BusyBox lacks required runtime applets: {', '.join(sorted(missing))}")
    traces = {}
    # Validate inputs before creating any output artifacts.
    for name in args.names:
        trace_path = source / "harness/trace-replay/data/native" / f"{name}.traj.json"
        image_dir = source / "experiments/contention/.local/images" / name
        trace = replay.load_trace(str(trace_path))
        image = json.loads((image_dir / "image.json").read_text())
        disk = image_dir / "rootfs.ext4"
        if not disk.is_file() or trace["name"] != name or trace["sha256"] != TRACE_HASHES[name]:
            raise ValueError(f"Invalid trace/image inputs: {name}")
        env = dict(item.split("=", 1) for item in image["env"])
        env.update({"HOME": "/root", "HOSTNAME": "swe-microvm"})
        env.update(trace["environment"].get("env", {}))
        commands = []
        for batch_id, batch in enumerate(trace["batches"]):
            for action_id, action in enumerate(batch["actions"]):
                observation = batch["observations"][action_id] if batch["observations"] else None
                commands.append({"index": len(commands), "batch": batch_id, "action": action_id,
                                 "command": action["command"],
                                 "command_sha256": hashlib.sha256(action["command"].encode()).hexdigest(),
                                 "expected_returncode": observation["returncode"] if observation else None,
                                 "source_output_sha256": hashlib.sha256(observation["raw_output"].encode()).hexdigest() if observation else None})
        traces[name] = {"name": name, "source_trace": str(trace_path), "trace_sha256": trace["sha256"],
                        "source_image": str(disk), "image_sha256": lab.sha256(disk),
                        "image_bytes": disk.stat().st_size, "image_metadata": image,
                        "image_path": f"/storage/{name}.raw", "initramfs_path": f"/lab/{name}-initramfs.cpio",
                        "command_count": len(commands), "commands": commands,
                        "handoff_before": BOUNDARIES[name], "environment": trace["environment"],
                        "effective_environment": env,
                        "source_submission_fingerprint": trace["source_submission_fingerprint"]}
    destination.mkdir(parents=True)
    (destination / "traces").mkdir()
    for name, trace in traces.items():
        shutil.copy2(trace["source_trace"], destination / "traces" / f"{name}.traj.json")
    for name in ("l1-kernel", "l2-initramfs.cpio", "shared-probe", "nfs-probe"):
        shutil.copy2(baseline / name, destination / name)
    if args.l1_kernel:
        shutil.copy2(args.l1_kernel, destination / "l1-kernel")
    with tempfile.TemporaryDirectory(prefix="trace-assets-", dir=destination) as tmp:
        temporary = Path(tmp)
        l1 = temporary / "l1"
        l1.mkdir()
        with (baseline / "l1-initramfs.cpio").open("rb") as stream:
            checked(["cpio", "-id", "--quiet", "--no-absolute-filenames"], cwd=l1, stdin=stream)
        if args.l2_kernel:
            shutil.copy2(args.l2_kernel, l1 / "lab/l2-vmlinux")
        if args.module_dir:
            shutil.rmtree(l1 / "modules")
            (l1 / "modules").mkdir()
            modules = lab.copy_modules(args.module_dir.resolve(), l1 / "modules", baseline_manifest["vendor"])
            (l1 / "lab/modules.list").write_text("\n".join(modules) + "\n")
            baseline_manifest.update(modules=modules, l1_kernel_release=args.module_dir.name)
        images = temporary / "images"
        images.mkdir()
        for name, trace in traces.items():
            root = temporary / name
            lab.base_root(root, busybox)
            shutil.copy2(HERE / "trace-l2-init.sh", root / "init")
            (root / "init").chmod(0o755)
            shutil.copy2(HERE / "trace-guest-run.sh", root / "lab/trace-guest-run.sh")
            payload = root / "lab/trace"
            (payload / "cmd").mkdir(parents=True)
            shutil.copy2(busybox, payload / "busybox")
            env = trace["effective_environment"]
            timeout = trace["environment"].get("timeout", 60)
            (payload / "meta").write_text(f"{trace['command_count']} {timeout}\n")
            for command in trace["commands"]:
                number = f"{command['index']:03d}"
                (payload / "cmd" / f"{number}.command").write_text(command["command"])
                argv = ["/trace/busybox", "env", "-i", *[f"{k}={v}" for k, v in env.items()],
                        *trace["environment"]["interpreter"], command["command"]]
                script = payload / "cmd" / f"{number}.sh"
                script.write_text("#!/trace/busybox sh\ncd " + shlex.quote(trace["environment"]["cwd"]) +
                                  " || exit 125\nexec " + shlex.join(argv) + "\n")
                script.chmod(0o755)
            initramfs = destination / f"{name}-initramfs.cpio"
            lab.pack_initramfs(root, initramfs)
            shutil.copy2(initramfs, l1 / "lab" / initramfs.name)
            # A sparse copy avoids modifying or reflinking writable state into borrowed images.
            checked(["cp", "--sparse=always", trace["source_image"], images / f"{name}.raw"])
            if lab.sha256(images / f"{name}.raw") != trace["image_sha256"]:
                raise RuntimeError(f"Image changed while preparing assets: {name}")
        lab.pack_initramfs(l1, destination / "l1-initramfs.cpio")
        outer = destination / "storage.raw"
        total = sum(trace["image_bytes"] for trace in traces.values())
        outer_bytes = math.ceil((total * 1.08 + GIB) / GIB) * GIB
        with outer.open("xb") as stream:
            stream.truncate(outer_bytes)
        checked(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0",
                 "-E", "lazy_itable_init=0,lazy_journal_init=0", "-d", images, outer])
    manifest = {key: baseline_manifest[key] for key in
                ("vendor", "l1_kernel_release", "modules", "qemu", "qemu_version", "firecracker_version", "inputs")}
    for name, path in (("l1_kernel", args.l1_kernel), ("l2_kernel", args.l2_kernel)):
        if path:
            manifest["inputs"][name] = {"path": str(path.resolve()), "sha256": lab.sha256(path)}
    if args.dependency_manifest:
        manifest["dependencies"] = json.loads(args.dependency_manifest.read_text())
        manifest["inputs"]["dependency_manifest"] = {"path": str(args.dependency_manifest.resolve()),
                                                     "sha256": lab.sha256(args.dependency_manifest)}
        kernel_config = args.dependency_manifest.parent / "kernel-build/.config"
        manifest["inputs"]["kernel_config"] = {"path": str(kernel_config.resolve()), "sha256": lab.sha256(kernel_config)}
    manifest.update({"schema": "cxl-swe-traces-v1", "base_assets": str(baseline),
                "busybox_required_applets": sorted(REQUIRED_APPLETS),
                "base_manifest_sha256": lab.sha256(baseline / "manifest.json"), "traces": traces,
                "schedule": "exact recorded commands in order; recorded waiting gaps omitted",
                "storage": "read-only ext4 lower via virtio-blk; tmpfs overlay upper in guest RAM",
                "timeout_policy": "TERM command process group after original timeout; KILL after 5 seconds; clean remaining group after completion",
                "source_hashes": {name: lab.sha256(HERE / name) for name in
                                  ("trace_prepare.py", "trace-l2-init.sh", "trace-guest-run.sh", "lab.py")},
                "artifacts": {p.name: lab.sha256(p) for p in destination.iterdir() if p.is_file()}})
    lab.save_json(destination / "manifest.json", manifest)
    print(json.dumps({"assets": str(destination), "traces": list(traces), "storage": str(destination / "storage.raw")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--base-assets", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--l1-kernel", type=Path)
    parser.add_argument("--l2-kernel", type=Path)
    parser.add_argument("--module-dir", type=Path)
    parser.add_argument("--dependency-manifest", type=Path)
    parser.add_argument("--busybox", type=Path)
    parser.add_argument("--names", nargs="+", default=list(BOUNDARIES))
    prepare(parser.parse_args())
