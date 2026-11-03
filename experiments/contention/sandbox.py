#!/usr/bin/env python3
"""Short-trace sandboxes for the contention experiment.

select: choose short, light traces from the measured Docker replay.
build:  turn each pinned task image into a Firecracker ext4 root disk with the guest replay agent.
solo:   replay each trace alone in a 1-vCPU microVM and record per-command CPU demand.
"""

import argparse
import collections
import csv
import datetime
import hashlib
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
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCAL = HERE / ".local"
IMAGES = LOCAL / "images"
RUNS = LOCAL / "runs"
CANDIDATES = HERE / "candidates.json"
PINS = HERE / "image-pins.json"  # RL image tag -> repo@manifest digest of the first build, so deleted images rebuild
TRACES = REPO / "harness/trace-replay/data/native"
DOCKER_RESULTS = REPO / "docs/results/numa-immediate-20261014"
KERNEL = REPO / ".local/microvm/vmlinux-6.1.188"
# Pinned copy (sha256 9aa1b69b...) of the Firecracker build used since the first solo run; the source tree's build
# output belongs to another agent and may be rebuilt at any time.
FIRECRACKER = LOCAL / "bin/firecracker"
BE_LOAD = LOCAL / "bin/be_load"  # built from be_load.c on first use
RESCTRL = Path("/sys/fs/resctrl")
TURBOSTAT = Path("/usr/lib/linux-tools-6.8.0-146/turbostat")
BUSYBOX = Path("/usr/bin/busybox")
LLM_MODEL = None  # (t0 s, s per context token, s per output token); set by --llm-model tokens
BALLOON = True
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


def image_dir(entry):
    """Root disk directory. RL rollouts of one task share an image (`image_key`); 264-trace entries use their name."""
    return IMAGES / entry.get("image_key", entry["name"])


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


REGISTRY = "https://registry-1.docker.io"
MANIFEST_TYPES = ", ".join(["application/vnd.docker.distribution.manifest.v2+json",
                            "application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.oci.image.index.v1+json"])


def registry_rootfs(reference, work):
    """Download an image's layers from Docker Hub with curl and flatten them into work/rootfs.tar, applying OCI
    whiteouts. Returns (image config, manifest digest, [repo@digest])."""
    ref = reference.removeprefix("docker.io/")
    if "@" in ref:  # repo@sha256:... (a pinned platform manifest)
        name, tag = ref.split("@", 1)
    else:
        name, tag = ref.rsplit(":", 1) if ":" in ref.split("/")[-1] else (ref, "latest")
    auth = []

    def login():
        # Registry tokens expire after about 5 minutes; large images outlive one, so blob fetches renew it.
        token = json.loads(output(["curl", "-sf", "--retry", "5",
                                   f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{name}:pull"]))["token"]
        auth[:] = ["-H", f"Authorization: Bearer {token}"]

    login()

    def get(path, *extra):
        return subprocess.run(["curl", "-sfL", "--retry", "5", *auth, *extra, f"{REGISTRY}/v2/{name}/{path}"],
                              check=True, capture_output=True).stdout

    raw = get(f"manifests/{tag}", "-H", f"Accept: {MANIFEST_TYPES}")
    manifest = json.loads(raw)
    if "manifests" in manifest:
        choice = next(m for m in manifest["manifests"]
                      if m.get("platform", {}).get("os") == "linux" and m["platform"].get("architecture") == "amd64")
        raw = get(f"manifests/{choice['digest']}", "-H", f"Accept: {MANIFEST_TYPES}")
        manifest = json.loads(raw)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    config = json.loads(get(f"blobs/{manifest['config']['digest']}"))

    def fetch(item):
        index, layer = item
        if "gzip" not in layer["mediaType"] and not layer["mediaType"].endswith(".tar"):
            raise RuntimeError(f"{reference}: unsupported layer type {layer['mediaType']}")
        path = work / f"layer{index:03d}.tgz"
        for attempt in range(4):
            try:
                subprocess.run(["curl", "-sfL", "--retry", "8", "--retry-all-errors", *auth, "-o", str(path),
                                f"{REGISTRY}/v2/{name}/blobs/{layer['digest']}"], check=True)
                break
            except subprocess.CalledProcessError:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
                login()
        actual = "sha256:" + output(["sha256sum", str(path)]).split()[0]
        if actual != layer["digest"]:
            raise RuntimeError(f"{reference}: layer {index} digest {actual} != {layer['digest']}")
        return path

    with ThreadPoolExecutor(4) as pool:
        paths = list(pool.map(fetch, enumerate(manifest["layers"])))
    flatten_layers(paths, work / "rootfs.tar")
    for path in paths:
        path.unlink()
    return config.get("config") or {}, digest, [f"{name}@{digest}"]


def layer_path(name):
    path = os.path.normpath("/" + name).lstrip("/")
    return path if path != "." else ""


def flatten_layers(paths, out_path):
    """Two passes: find which layer provides each path (whiteouts delete lower-layer paths, an opaque marker
    clears the directory below), then copy those members in layer order."""
    final = {}
    for index, path in enumerate(paths):
        with tarfile.open(path, "r:*") as tf:
            names = [layer_path(m.name) for m in tf]
        for name in names:
            base, parent = os.path.basename(name), os.path.dirname(name)
            if base == ".wh..wh..opq":
                prefix = parent + "/" if parent else ""
                for key in [k for k in final if k.startswith(prefix) and k != parent]:
                    del final[key]
            elif base.startswith(".wh."):
                target = os.path.join(parent, base[4:])
                for key in [k for k in final if k == target or k.startswith(target + "/")]:
                    del final[key]
        for name in names:
            if name and not os.path.basename(name).startswith(".wh."):
                final[name] = index
    with tarfile.open(out_path, "w", format=tarfile.PAX_FORMAT) as out:
        for index, path in enumerate(paths):
            with tarfile.open(path, "r:*") as tf:
                for member in tf:
                    name = layer_path(member.name)
                    if final.get(name) != index:
                        continue
                    if member.islnk():
                        target = layer_path(member.linkname)
                        if final.get(target) is None or final[target] > index:
                            continue  # its target was deleted or replaced later
                        member.linkname = target
                    member.name = name
                    out.addfile(member, tf.extractfile(member) if member.isreg() else None)


def run_setup(entry, rootfs_tar):
    """Task setup the RL harness ran before the agent (Scale-SWE pre_commands: base-commit checkout, git gc).
    Runs in a local container imported from the flattened root file system; no registry access."""
    image = output(["docker", "import", str(rootfs_tar)]).strip()
    try:
        container = output(["docker", "run", "-d", "--network", "none", "-w", entry["cwd"], image,
                            "bash", "-c", entry["setup"]]).strip()
        try:
            status = int(output(["docker", "wait", container]).strip())
            if status:
                log = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
                raise RuntimeError(f"{entry['name']}: setup exited {status}: {(log.stdout + log.stderr)[-1500:]}")
            run(["docker", "export", "-o", str(rootfs_tar), container])
        finally:
            run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL)
    finally:
        run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL)


def build_image(entry, keep_image, pin=None):
    out = image_dir(entry)
    if (out / "image.json").exists():
        image = json.loads((out / "image.json").read_text())
        if image.get("init_stub") == INIT_STUB:
            return f"{entry['name']}: exists"
        install_stub(out / "rootfs.ext4")
        image.pop("guest_init", None)
        image["init_stub"] = INIT_STUB
        save_json(out / "image.json", image)
        return f"{entry['name']}: init stub updated"
    work = IMAGES / (image_dir(entry).name + ".partial")
    if work.exists():
        run(["chmod", "-R", "u+w", work])
        shutil.rmtree(work)
    work.mkdir(parents=True)
    if entry.get("repo_digest"):
        # 264-trace entries: pinned digest through dockerd, as before.
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
        config, image_id, digests = inspect["Config"], inspect["Id"], inspect.get("RepoDigests") or []
        if not keep_image:
            run(["docker", "image", "rm", reference], stdout=subprocess.DEVNULL)
    else:
        # RL rollouts name only a tag. dockerd pulls at 1-2 MB/s on this host while one curl stream gets ~18 MB/s,
        # so the layers come straight from the registry and are flattened here; the manifest digest is recorded.
        # A pin (repo@digest from an earlier build, image-pins.json) rebuilds exactly that image after deletion.
        reference = pin or entry["image"]
        config, image_id, digests = registry_rootfs(reference, work)
        if pin and digests != [pin.removeprefix("docker.io/")]:
            raise RuntimeError(f"{entry['name']}: pinned {pin} resolved to {digests}")
        if entry.get("setup"):
            run_setup(entry, work / "rootfs.tar")
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
    save_json(work / "image.json", {"name": entry["name"], "repo_digest": reference, "image_id": image_id,
                                    "repo_digests": digests, "setup": entry.get("setup") or "",
                                    "env": config.get("Env") or [], "working_dir": config.get("WorkingDir"),
                                    "export_tar_bytes": tar_bytes, "disk_bytes": disk_bytes,
                                    "disk_allocated_bytes": (work / "rootfs.ext4").stat().st_blocks * 512,
                                    "init_stub": INIT_STUB,
                                    "busybox": str(BUSYBOX), "built_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    work.rename(out)
    return f"{entry['name']}: {tar_bytes / GIB:.2f} GiB export, {disk_bytes // GIB} GiB disk"


def plan_entries(entries, plan_path, steps):
    """Entries of the groups in steps 'a-b' of a step plan."""
    plan = json.loads(plan_path.read_text())
    first, _, last = (steps or f"0-{len(plan['steps']) - 1}").partition("-")
    wanted = {g for s in plan["steps"][int(first):int(last or first) + 1] for w in s["waves"] for g in w["groups"]}
    return [e for e in entries if e.get("group") in wanted]


def load_pins():
    return json.loads(PINS.read_text()) if PINS.exists() else {}


def build(args):
    IMAGES.mkdir(parents=True, exist_ok=True)
    entries = candidates(args.names)
    if args.plan:
        entries = plan_entries(entries, args.plan, args.plan_steps)
    entries = list({image_dir(e): e for e in entries}.values())  # one build per shared image
    pins = load_pins()
    failures = []
    # Processes, not threads: flattening layers is pure Python and would serialize on the GIL.
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(build_image, e, args.keep_image, None if args.unpinned else pins.get(e.get("image"))): e
                   for e in entries}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                print(future.result(), flush=True)
            except Exception as error:
                failures.append(entry["name"])
                print(f"{entry['name']}: FAILED {error}", flush=True)
                continue
            built = json.loads((image_dir(entry) / "image.json").read_text())
            if entry.get("image") and not entry.get("repo_digest") and built.get("repo_digests"):
                pins.setdefault(entry["image"], built["repo_digests"][0])
                save_json(PINS, dict(sorted(pins.items())))
    if failures:
        raise SystemExit(f"{len(failures)} image builds failed: {sorted(failures)}")


def drop_images(args):
    """Segment cleanup: remove the root disks of a plan segment's images once every condition has run on them.
    The pin stays in image-pins.json, so `build --plan` makes the same image again."""
    keep_files = args.keep or [HERE / "candidates-rl.json"]
    keep = {image_dir(e) for f in keep_files for e in json.loads(f.read_text())["traces"]}
    pins = load_pins()
    targets = sorted({image_dir(e) for e in plan_entries(candidates(), args.plan, args.plan_steps)} - keep)
    freed = 0
    for path in targets:
        if not (path / "image.json").exists():
            continue
        image = json.loads((path / "image.json").read_text())
        if not any(d in pins.values() for d in image.get("repo_digests") or []):
            print(f"{path.name}: no pin recorded, kept")
            continue
        freed += sum(f.stat().st_blocks * 512 for f in path.iterdir())
        if not args.dry_run:
            run(["chmod", "-R", "u+w", path])
            shutil.rmtree(path)
    print(f"{'would free' if args.dry_run else 'freed'} {freed / GIB:.1f} GiB in {len(targets)} images "
          f"({len(keep)} kept by {[str(f) for f in keep_files]})")


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


def llm_gaps(trace):
    """Modeled LLM wait before each batch: t0 + alpha * context tokens + beta * output tokens of the response that
    carries the batch (tokens from the converted RL rollouts). None keeps the recorded gaps."""
    if not LLM_MODEL:
        return None
    raw = json.loads(Path(trace["source"]).read_text())
    tokens = [m["extra"]["tokens"] for m in raw["messages"]
              if m["role"] == "assistant" and (m.get("extra") or {}).get("actions")]
    if len(tokens) != len(trace["batches"]):
        raise RuntimeError(f"{trace['name']}: {len(tokens)} token rows for {len(trace['batches'])} batches")
    t0, alpha, beta = LLM_MODEL
    return [max(0.0, t0 + alpha * t["context"] + beta * t["output"]) for t in tokens]


def make_job(entry, image, directory, scale, net=None, mem_sample_s=0.0):
    trace = replay.load_trace(str(REPO / entry["trace"]))
    modeled = llm_gaps(trace)
    environment = trace["environment"]
    env = dict(item.split("=", 1) for item in image["env"])
    env.update({"HOME": "/root", "HOSTNAME": "swe-microvm"})
    env.update(environment.get("env", {}))
    job = directory / "job"
    (job / "cmd").mkdir(parents=True)
    shutil.copy2(HERE / "guest-init.sh", job / "init")
    if net:
        (job / "net").write_text(" ".join(net) + "\n")
    if mem_sample_s:
        (job / "memsample").write_text(f"{mem_sample_s:g}\n")
    commands = []
    for b, batch in enumerate(trace["batches"]):
        for a, action in enumerate(batch["actions"]):
            index = len(commands)
            gap = (modeled[b] if modeled else batch["gap"]) if a == 0 else 0.0
            (job / "cmd" / f"{index:03d}.sh").write_text(action["command"])
            (job / "cmd" / f"{index:03d}.gap").write_text(f"{gap:.3f}\n")
            commands.append({"index": index, "batch": b, "action": a, "gap_s": round(gap, 3),
                             "expected_returncode": batch["observations"][a]["returncode"] if batch["observations"] else None})
    argv = ["/microvm/busybox", "env", "-i", *[f"{k}={v}" for k, v in env.items()], *environment["interpreter"]]
    (job / "run").write_text("#!/microvm/busybox sh\nexec " + " ".join(map(shlex.quote, argv))
                             + ' "$(/microvm/busybox cat "$1")"\n')
    (job / "run").chmod(0o755)
    (job / "meta").write_text(f"{len(commands)} {environment.get('timeout', 60)} {scale}\n")
    (job / "cwd").write_text(entry.get("cwd") or environment["cwd"])
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
    # Same disk layout as the pilot (shared read-only base + sparse writable overlay), so solo is its baseline and
    # 30 parallel solo VMs do not need 30 full copies of the root disk.
    upper = directory / "rootfs.ext4"
    run(["truncate", "-s", "8G", upper])
    run(["mkfs.ext4", "-q", "-F", "-L", "swe-upper", upper])
    config = {"boot-source": {"kernel_image_path": str(KERNEL), "initrd_path": str(overlay_initrd()),
                              "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"},
              "drives": [{"drive_id": "base", "path_on_host": str(image_dir(entry) / "rootfs.ext4"),
                          "is_root_device": False, "is_read_only": True},
                         {"drive_id": "job", "path_on_host": str(job_disk), "is_root_device": False, "is_read_only": False},
                         {"drive_id": "upper", "path_on_host": str(upper), "is_root_device": False, "is_read_only": False}],
              "machine-config": {"vcpu_count": vcpus, "mem_size_mib": memory_mib}}
    if BALLOON:
        config["balloon"] = balloon_config()
    if tap:
        config["network-interfaces"] = [{"iface_id": "eth0", "host_dev_name": tap,
                                         "guest_mac": f"06:00:0a:c8:01:{core:02x}"}]
    save_json(directory / "config.json", config)
    limit = len(commands) * int(trace["environment"].get("timeout", 60)) + sum(c["gap_s"] for c in commands) * scale + 180
    serial = (directory / "serial.log").open("w")
    samples = (directory / "threads.csv").open("w", newline="")
    writer = csv.writer(samples)
    writer.writerow(["t_ns", "tid", "comm", "run_ns", "wait_ns"])
    rss = (directory / "rss.csv").open("w")
    rss.write("t_ns,rss_kib\n")
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
    pinned, halted, tick = set(), False, 0
    try:
        while process.poll() is None:
            now = time.monotonic_ns()
            if (now - started) / 1e9 > limit:
                raise TimeoutError(f"VM exceeded {limit:.0f}s")
            if tick % 50 == 0:
                rss.write(f"{now},{vm_rss_kib(process.pid)}\n")
            tick += 1
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
        rss.close()
        serial.close()
    out = directory / "out"
    run(["debugfs", "-R", f"rdump /out {directory}", str(job_disk)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return trace, commands, markers, halted, process.returncode, out


def vm_rss_parts_kib(pid):
    """(RssAnon, RssFile) of a VMM: guest RAM and touched file mappings (a DAX base image is RssFile, shared)."""
    try:
        fields = {l.split(":")[0]: int(l.split()[1]) for l in open(f"/proc/{pid}/status") if l.startswith(("RssAnon", "RssFile"))}
        return fields.get("RssAnon", 0), fields.get("RssFile", 0)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return 0, 0


def vm_rss_kib(pid):
    try:
        return next(int(l.split()[1]) for l in open(f"/proc/{pid}/status") if l.startswith("VmRSS:"))
    except (FileNotFoundError, ProcessLookupError, StopIteration, ValueError):
        return 0


def peak_rss_kib(directory):
    path = directory / "rss.csv"
    if not path.exists():
        return 0
    with path.open() as stream:
        return max((int(row["rss_kib"]) for row in csv.DictReader(stream)), default=0)


def balloon_config():
    """Free page reporting returns pages the guest frees (exited test processes) to the host; page cache stays."""
    return {"amount_mib": 0, "deflate_on_oom": True, "free_page_reporting": True}


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
            "docker_cpu_time_s": entry.get("docker_cpu_time_s"), "docker_duration_s": entry.get("docker_duration_s"),
            "peak_rss_mib": round(peak_rss_kib(directory) / 1024, 1), "group": entry.get("group"),
            "image_key": entry.get("image_key")}


def solo_one(entry, groups, root, args):
    directory = root / entry["name"]
    directory.mkdir()
    group = groups.get()
    tap = None
    try:
        image = json.loads((image_dir(entry) / "image.json").read_text())
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
    entries = [e for e in candidates(args.names) if (image_dir(e) / "image.json").exists()]
    if args.plan:
        entries = plan_entries(entries, args.plan, args.plan_steps)
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
                                  "llm_model": LLM_MODEL, "balloon": BALLOON,
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


# --base-pmem: the base image is a read-only virtio-pmem device mounted with DAX, so the guest maps the host's page
# cache of the image file instead of keeping its own copy; the block devices shift to job = vda, upper = vdb.
OVERLAY_INIT_PMEM = OVERLAY_INIT.replace("-o ro /dev/vda /lower", "-o ro,dax,noload /dev/pmem0 /lower") \
    .replace("/dev/vdc /upper", "/dev/vdb /upper").replace("/dev/vdb /newroot/job", "/dev/vda /newroot/job")


def overlay_initrd(pmem=False):
    init = OVERLAY_INIT_PMEM if pmem else OVERLAY_INIT
    path = LOCAL / ("overlay-initrd-pmem.cpio" if pmem else "overlay-initrd.cpio")
    stamp = LOCAL / ("overlay-initrd-pmem.init" if pmem else "overlay-initrd.init")
    if path.exists() and stamp.exists() and stamp.read_text() == init:
        return path
    with tempfile.TemporaryDirectory(dir=LOCAL) as temp:
        root = Path(temp)
        for directory in ("bin", "dev", "proc", "sys"):
            (root / directory).mkdir()
        shutil.copy2(BUSYBOX, root / "bin/busybox")
        (root / "init").write_text(init)
        (root / "init").chmod(0o755)
        names = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        with path.open("wb") as stream:
            run(["cpio", "--quiet", "-o", "--format=newc", "--owner=0:0"], cwd=root,
                input=("\n".join(["."] + names) + "\n").encode(), stdout=stream)
    stamp.write_text(init)
    return path


class Sandbox:
    """One trace replay in a microVM pinned to a node: vCPUs on the node's cores, VMM threads on their siblings."""

    def __init__(self, entry, directory, node, slot, args):
        self.entry, self.directory, self.node, self.slot, self.args = entry, directory, node, slot, args
        self.image = json.loads((image_dir(entry) / "image.json").read_text())
        self.markers, self.complete, self.vcpus = [], threading.Event(), {}
        self.process = self.reader = self.tap = None
        self.net_id = None  # plan mode: network identity fixed before the node is chosen
        self.prepared, self.prepare_error = False, None

    def start(self):
        if not self.prepared:
            self.prepare()
        self.launch()

    def prepare(self):
        """Job disk, overlay disk and VMM config: done before a synchronous step so all VMs launch at the barrier."""
        args, node = self.args, self.node
        self.directory.mkdir(parents=True)
        net = None
        if args.network:
            if self.net_id is None:
                hi, lo, name = node["index"], self.slot, f"ctt{node['index']}x{self.slot}"
            else:
                hi, lo, name = 100 + self.net_id // 250, self.net_id % 250, f"ctv{self.net_id}"
            self.tap = tap_up(name)
            self.mac = f"06:00:0a:c8:{2 + hi:02x}:{lo + 2:02x}"
            net = (f"10.200.{2 + hi}.{lo + 2}/{SUBNET.split('/')[1]}", GATEWAY, ",".join(host_dns()))
        self.trace, self.commands, job_disk = make_job(self.entry, self.image, self.directory, args.gap_scale, net,
                                                       getattr(args, "mem_sample_s", 0.0))
        upper = self.directory / "upper.ext4"
        run(["truncate", "-s", f"{args.upper_gib}G", upper])
        run(["mkfs.ext4", "-q", "-F", "-L", "swe-upper", upper])
        pmem = getattr(args, "base_pmem", False)
        base = str(image_dir(self.entry) / "rootfs.ext4")
        drives = [{"drive_id": "job", "path_on_host": str(job_disk), "is_root_device": False, "is_read_only": False},
                  {"drive_id": "upper", "path_on_host": str(upper), "is_root_device": False, "is_read_only": False}]
        if not pmem:
            drives.insert(0, {"drive_id": "base", "path_on_host": base, "is_root_device": False, "is_read_only": True})
        config = {"boot-source": {"kernel_image_path": str(KERNEL), "initrd_path": str(overlay_initrd(pmem)),
                                  "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"},
                  "drives": drives,
                  "machine-config": {"vcpu_count": args.vcpus,
                                     "mem_size_mib": args.memory_mib or self.entry.get("memory_mib", 2048)}}
        if pmem:
            config["pmem"] = [{"id": "base", "path_on_host": base, "root_device": False, "read_only": True}]
        if BALLOON:
            config["balloon"] = balloon_config()
        if self.tap:
            config["network-interfaces"] = [{"iface_id": "eth0", "host_dev_name": self.tap, "guest_mac": self.mac}]
        save_json(self.directory / "config.json", config)
        self.prepared = True

    def launch(self):
        node = self.node
        if self.prepare_error:
            raise RuntimeError(self.prepare_error)
        self.serial = (self.directory / "serial.log").open("w")
        command = ["numactl", f"--membind={node['memory_node']}", "taskset", "-c", node["vmm_cpus"], str(FIRECRACKER),
                   "--no-api", "--config-file", str(self.directory / "config.json")]
        if self.args.ls_resctrl:  # join the group before Firecracker starts its vCPU threads, which inherit it
            command = ["sh", "-c", f'echo $$ > {RESCTRL / self.args.ls_resctrl}/tasks && exec "$@"', "sh", *command]
        self.process = subprocess.Popen(
            command,
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
        # SWE_MEM samples: [host_ns, uptime, MemFree, Cached, Buffers, AnonPages, Shmem, Dirty (KiB),
        #                   read and written sectors of vda, vdb, vdc]
        mem = [[t] + [float(x) for x in m.split()[1:]] for t, m in self.markers if m.startswith("SWE_MEM ")]
        return {"trace": self.entry["name"], "node": self.node["index"], "slot": self.slot, "mem": mem,
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


def numa_reclaimable_kib(memory_node):
    """MemFree + FilePages of one NUMA node: what sandboxes bound to it can still get without swapping anon pages."""
    fields = {}
    for line in open(f"/sys/devices/system/node/node{memory_node}/meminfo"):
        parts = line.split()
        fields[parts[2].rstrip(":")] = int(parts[3])
    return fields.get("MemFree", 0) + fields.get("FilePages", 0) - fields.get("Shmem", 0), fields.get("MemFree", 0)


def sampler(sandboxes, lock, nodes, stop, directory, interval_s):
    """Every interval, per-VM vCPU run/wait deltas (only nonzero rows) and per-node totals.
    Every 2 s, per-VM VMM RSS and the memory node's free memory; the run stops before the node runs out."""
    previous = {}
    memory_nodes = sorted({n["memory_node"] for n in nodes})
    tick, every = 0, max(1, round(2.0 / interval_s))
    with (directory / "vcpu.csv").open("w", newline="") as vm_stream, \
            (directory / "nodes.csv").open("w", newline="") as node_stream, \
            (directory / "memory.csv").open("w", newline="") as mem_stream:
        vm_writer, node_writer, mem_writer = csv.writer(vm_stream), csv.writer(node_stream), csv.writer(mem_stream)
        vm_writer.writerow(["t_ns", "dt_ns", "node", "vm", "run_ns", "wait_ns"])
        node_writer.writerow(["t_ns", "dt_ns", "node", "live_vms", "run_ns", "wait_ns", "mem_available_kib"])
        mem_writer.writerow(["t_ns", "vm", "rss_kib"])
        last = time.monotonic_ns()
        while not stop.wait(max(0.0, interval_s - (time.monotonic_ns() - last) / 1e9)):
            now = time.monotonic_ns()
            dt, last = now - last, now
            with lock:
                live = list(sandboxes.items())
            tick += 1
            if tick % every == 0:
                for vm, box in live:
                    if box.process:
                        mem_writer.writerow([now, vm, vm_rss_kib(box.process.pid)])
                        anon, mapped = vm_rss_parts_kib(box.process.pid)
                        mem_writer.writerow([now, f"{vm}_anon", anon])
                        mem_writer.writerow([now, f"{vm}_file", mapped])
                for m in memory_nodes:
                    reclaimable, free = numa_reclaimable_kib(m)
                    mem_writer.writerow([now, f"numa{m}_free", free])
                    mem_writer.writerow([now, f"numa{m}_reclaimable", reclaimable])
                    if reclaimable < 6 * 1024 * 1024:
                        print(f"NUMA {m} free + file pages below 6GiB; stopping run", flush=True)
                        stop.set()
                mem_stream.flush()
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


VMSTAT = ("pgfault", "pgmajfault", "pgscan_kswapd", "pgscan_direct", "pgsteal_direct", "allocstall_normal",
          "compact_stall", "workingset_refault_file", "thp_fault_alloc", "pswpin", "pswpout")


def host_sampler(directory, stop, cpus, disk="nvme0n1", interval_s=1.0):
    """Host-wide counters besides CPU, every second (host.csv, cumulative values; diff them): PSI totals (us), reclaim
    and fault counters, disk sectors/ms of the run disk, sandbox network bytes (TAPs and bridge), IRQ and softirq
    time of the node CPUs (jiffies), conntrack entries, and free space of the run file system. Point-in-time values:
    memory of this run's cgroup (run it in its own scope, e.g. `systemd-run --user --scope`; total, anon and file
    GiB, OOM kills) and allocated GiB of all sandbox job and writable disks."""
    def psi(kind):
        lines = Path(f"/proc/pressure/{kind}").read_text().splitlines()
        return [line.rsplit("total=", 1)[1] for line in lines]

    columns = (["t_ns", "psi_cpu_some", "psi_cpu_full", "psi_mem_some", "psi_mem_full", "psi_io_some", "psi_io_full"]
               + list(VMSTAT) + ["disk_rd_sectors", "disk_rd_ms", "disk_wr_sectors", "disk_wr_ms", "disk_io_ms",
                                 "tap_rx_bytes", "tap_tx_bytes", "tap_drops", "irq_jiffies", "softirq_jiffies",
                                 "conntrack", "fs_free_gib", "cg_mem_gib", "cg_anon_gib", "cg_file_gib",
                                 "cg_oom_kill", "vm_disk_gib"])
    cgroup = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().split("::", 1)[1].strip().lstrip("/")
    with (directory / "host.csv").open("w") as stream:
        stream.write(",".join(columns) + "\n")
        while not stop.wait(interval_s):
            row = [time.monotonic_ns()] + psi("cpu") + psi("memory") + psi("io")
            vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            row += [vm.get(k, "") for k in VMSTAT]
            fields = next((line.split() for line in Path("/proc/diskstats").read_text().splitlines()
                           if line.split()[2] == disk), None)
            row += [fields[5], fields[6], fields[9], fields[10], fields[12]] if fields else [""] * 5
            rx = tx = drops = 0
            for link in Path("/sys/class/net").iterdir():
                if link.name.startswith(("ctv", "ctap", "ctbr")):
                    try:
                        stats = link / "statistics"
                        rx += int((stats / "rx_bytes").read_text())
                        tx += int((stats / "tx_bytes").read_text())
                        drops += int((stats / "rx_dropped").read_text()) + int((stats / "tx_dropped").read_text())
                    except OSError:
                        pass  # a TAP removed between listing and reading
            irq = softirq = 0
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("cpu") and line[3].isdigit() and int(line.split()[0][3:]) in cpus:
                    parts = line.split()
                    irq += int(parts[6])
                    softirq += int(parts[7])
            try:
                conntrack = Path("/proc/sys/net/netfilter/nf_conntrack_count").read_text().strip()
            except OSError:
                conntrack = ""
            free = os.statvfs(directory)
            row += [rx, tx, drops, irq, softirq, conntrack, round(free.f_bavail * free.f_frsize / 2**30, 2)]
            try:
                stat = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
                events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
                row += [round(int((cgroup / "memory.current").read_text()) / 2**30, 3),
                        round(int(stat["anon"]) / 2**30, 3), round(int(stat["file"]) / 2**30, 3), events["oom_kill"]]
            except (OSError, KeyError):
                row += [""] * 4
            blocks = 0
            for vm in os.scandir(directory / "vm") if (directory / "vm").exists() else []:
                for name in ("job.ext4", "upper.ext4"):
                    try:
                        blocks += os.stat(os.path.join(vm.path, name)).st_blocks
                    except OSError:
                        pass
            row.append(round(blocks * 512 / 2**30, 2))
            stream.write(",".join(map(str, row)) + "\n")
            stream.flush()


class CoLoad:
    """DSec-style co-located best-effort (BE) load (docs/dsec-interference-design.md): `be_load` busy LEVEL of every
    CPU of the BE nodes for the whole run, under QoS none / idle (SCHED_IDLE) / core (+ one core-scheduling cookie,
    so BE never shares an SMT core with the sandboxes) / rdt (+ RDT: BE limited to --be-rdt L3 ways and memory
    bandwidth percent). With resctrl mounted, each node's BE runs in its own resctrl group and LLC occupancy and
    memory traffic of the BE groups and of everything else (root group) are logged every second (resctrl.csv);
    turbostat logs per-CPU busy frequency and IPC (turbostat.tsv)."""

    def __init__(self, args, nodes, directory, sampler_cpu):
        kind, _, level = (args.be or "").partition(":")
        self.kind, self.level, self.qos = kind, float(level or 0), args.be_qos
        self.nodes = [n for n in nodes if args.be_nodes is None or n["index"] in replay.cpu_ids(args.be_nodes)]
        self.args, self.directory, self.sampler_cpu = args, directory, sampler_cpu
        self.resctrl = (RESCTRL / "info").exists()
        self.processes, self.groups, self.stop = [], [], threading.Event()
        if self.qos == "rdt" and not self.resctrl:
            raise SystemExit("--be-qos rdt needs resctrl mounted at /sys/fs/resctrl")

    def config(self):
        return {"kind": self.kind or None, "level": self.level, "qos": self.qos,
                "nodes": [n["index"] for n in self.nodes],
                "rdt": self.args.be_rdt if self.qos == "rdt" else None, "mem_mb": self.args.be_mem_mb,
                "resctrl": self.resctrl}

    def start(self):
        if self.kind:
            source = HERE / "be_load.c"
            if not BE_LOAD.exists() or BE_LOAD.stat().st_mtime < source.stat().st_mtime:
                subprocess.run(["gcc", "-O2", "-pthread", str(source), "-o", str(BE_LOAD)], check=True)
            for node in self.nodes:
                cpus = ",".join(map(str, sorted(node["cores"] | node["siblings"])))
                command = ["taskset", "-c", cpus, "numactl", f"--membind={node['memory_node']}", str(BE_LOAD),
                           "--cpus", cpus, "--level", str(self.level), "--kind", self.kind,
                           "--mem-mb", str(self.args.be_mem_mb)]
                command += ["--idle"] if self.qos != "none" else []
                command += ["--core-cookie"] if self.qos in ("core", "rdt") else []
                stream = (self.directory / f"be-node{node['index']}.json").open("w")
                if self.resctrl:
                    group = RESCTRL / f"ctbe{node['index']}"
                    subprocess.run(["sudo", "-n", "mkdir", "-p", str(group)], check=True)
                    self.groups.append((group, node["memory_node"]))
                    if self.qos == "rdt":
                        ways, _, mb = self.args.be_rdt.partition(":")
                        schemata = f"L3:{node['memory_node']}={(1 << int(ways)) - 1:x}\nMB:{node['memory_node']}={mb}\n"
                        subprocess.run(["sudo", "-n", "sh", "-c", f"printf '{schemata}' > {group}/schemata"],
                                       check=True)
                    command = ["sudo", "-n", "sh", "-c",
                               f"echo $$ > {group}/tasks && exec " + " ".join(command)]  # threads inherit the group
                self.processes.append((subprocess.Popen(command, stdout=stream), stream))
        if self.resctrl:
            self.sampler = threading.Thread(target=self.sample, daemon=True)
            self.sampler.start()
        self.turbostat = subprocess.Popen(
            ["sudo", "-n", "taskset", "-c", str(self.sampler_cpu), str(TURBOSTAT), "--quiet", "--interval", "1",
             "--enable", "Time_Of_Day_Seconds", "--show", "Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,IPC",
             "--cpu", ",".join(str(c) for n in self.args.nodes_parsed for c in sorted(n["cores"] | n["siblings"])),
             "--out", str(self.directory / "turbostat.tsv")]) if TURBOSTAT.exists() else None

    def sample(self):
        """Every second: LLC occupancy and cumulative memory bytes of the root group and of each BE group."""
        domains = sorted({n["memory_node"] for n in self.args.nodes_parsed})
        with (self.directory / "resctrl.csv").open("w") as stream:
            stream.write("t_ns,group,domain,llc_occupancy,mbm_total_bytes,mbm_local_bytes\n")
            while not self.stop.wait(1.0):
                now = time.monotonic_ns()
                for name, group in [("root", RESCTRL)] + [(g.name, g) for g, _ in self.groups]:
                    for d in domains:
                        base = group / "mon_data" / f"mon_L3_{d:02d}"
                        try:
                            values = [(base / f).read_text().strip() for f in
                                      ("llc_occupancy", "mbm_total_bytes", "mbm_local_bytes")]
                        except OSError:
                            continue
                        stream.write(f"{now},{name},{d}," + ",".join(values) + "\n")
                stream.flush()

    def finish(self):
        self.stop.set()
        for process, stream in self.processes:
            subprocess.run(["sudo", "-n", "kill", "-TERM", str(process.pid)] if self.resctrl
                           else ["kill", "-TERM", str(process.pid)])
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                subprocess.run(["sudo", "-n", "kill", "-KILL", str(process.pid)])
            stream.close()
        if self.turbostat:
            subprocess.run(["sudo", "-n", "kill", "-TERM", str(self.turbostat.pid)])
            self.turbostat.wait(timeout=10)
        for group, _ in self.groups:
            subprocess.run(["sudo", "-n", "rmdir", str(group)])


def pilot(args):
    entries = [e for e in candidates(args.names) if (image_dir(e) / "image.json").exists()]
    nodes = parse_nodes(args.nodes, args.sibling_offset, args.pool)
    args.nodes_parsed = nodes
    used = set().union(*(n["cores"] | n["siblings"] for n in nodes))
    reserved = replay.cpu_ids(args.harness_cpus) if args.harness_cpus else set(range(os.cpu_count())) - used
    if reserved & used or len(reserved) < 2:
        raise SystemExit(f"--harness-cpus must be >= 2 CPUs outside the node cores and siblings: {sorted(reserved)}")
    sampler_cpu = max(reserved)
    os.sched_setaffinity(0, reserved - {sampler_cpu})  # harness, mkfs and serial readers stay off the node CPUs
    RUNS.mkdir(parents=True, exist_ok=True)
    if args.placement == "p2c" and not (args.sync_plan or (args.group_size and not args.sync_steps)):
        raise SystemExit("--placement p2c is implemented for --sync-plan and the per-group loop only")
    if args.sync_steps and not args.group_size:
        raise SystemExit("--sync-steps needs --group-size")
    plan, plan_range = None, None
    if args.sync_plan:
        plan = json.loads(args.sync_plan.read_text())
        first, _, last = (args.plan_steps or f"0-{len(plan['steps']) - 1}").partition("-")
        plan_range = (int(first), int(last or first))
        if not (0 <= plan_range[0] <= plan_range[1] < len(plan["steps"])):
            raise SystemExit(f"--plan-steps {args.plan_steps}: plan has {len(plan['steps'])} steps")
        if args.group_size != plan["group_size"] or args.placement == "pack" or args.sync_steps:
            raise SystemExit("--sync-plan needs --group-size as in the plan, no --sync-steps, and spread/home/least/p2c")
        have = collections.Counter(e.get("group") for e in entries)
        missing = sorted({g for s in plan["steps"][plan_range[0]:plan_range[1] + 1] for w in s["waves"]
                          for g in w["groups"] if have[g] != plan["group_size"]})
        if missing:
            raise SystemExit(f"{len(missing)} plan groups lack traces or built images in {CANDIDATES.name}: {missing[:8]}")
    tag = (f"k{args.per_node}-{args.pool}" + (f"-g{args.group_size}" if args.group_size else "")
           + (f"-sync{args.sync_steps}" if args.sync_steps else "")
           + (f"-plan{plan_range[0]}-{plan_range[1]}" if plan else "")
           + (f"-m{args.move}" if args.move != "none" else "") + f"-v{args.vcpus}-s{args.seed}")
    directory = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime(
        f"%Y%m%dT%H%M%SZ-pilot-{tag}-"), dir=RUNS))
    if args.network:
        network_up()
    overlay_initrd(args.base_pmem)
    co_load = CoLoad(args, nodes, directory, sampler_cpu)
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
        "placement": args.placement, "base_pmem": args.base_pmem, "mem_sample_s": args.mem_sample_s,
        "sync_steps": args.sync_steps, "step_gap_s": args.step_gap_s,
        "sync_plan": str(args.sync_plan) if plan else None, "plan_steps": plan_range, "llm_model": LLM_MODEL, "balloon": BALLOON, "candidates_file": str(CANDIDATES),
        "co_load": co_load.config(), "smt": Path("/sys/devices/system/cpu/smt/active").read_text().strip() == "1",
        "network": "host NAT" if args.network else "loopback only", "kernel": str(KERNEL),
        "firecracker_sha256": output(["sha256sum", str(FIRECRACKER)]).split()[0], "host_kernel": os.uname().release})
    if plan:
        save_json(directory / "plan.json", plan)
    cpu_log = subprocess.Popen(
        ["taskset", "-c", str(sampler_cpu), sys.executable, str(Path(__file__).resolve()), "cpu-sampler",
         "--groups", "/".join(",".join(map(str, sorted(n["cores"]))) for n in nodes),
         "--interval-ms", str(args.cpu_sample_ms), "--output", str(directory / "cpu1ms.csv")])
    co_load.start()
    host_stop = threading.Event()
    host_log = threading.Thread(target=host_sampler, args=(directory, host_stop, used), daemon=True)
    host_log.start()
    if co_load.kind and args.be_warmup_s:
        time.sleep(args.be_warmup_s)  # BE threads touch their memory and settle before the first sandbox
    sandboxes, lock, stop = {}, threading.Lock(), threading.Event()
    events = (directory / "sandboxes.jsonl").open("w")
    steps = (directory / "steps.jsonl").open("w")
    started = time.monotonic()
    deadline = started + args.ramp_s + args.duration_s
    counter = [0]

    def new_box(entry, node, slot):
        with lock:
            vm = counter[0]
            counter[0] += 1
        return vm, Sandbox(entry, directory / "vm" / f"{vm:05d}", node, slot, args)

    def run_sandbox(entry, node, slot, delay=0.0, prepared=None):
        if delay and stop.wait(delay):
            if prepared:
                prepared[1].stop()
                shutil.rmtree(prepared[1].directory, ignore_errors=True)
            return None
        vm, box = prepared or new_box(entry, node, slot)
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

    # RL rollouts carry their real GRPO group: G different samples of one task. Without it, one trace is replicated.
    real_groups = collections.defaultdict(list)
    for e in entries:
        if e.get("group"):
            real_groups[e["group"]].append(e)
    by_group = {g: sorted(v, key=lambda e: e["name"]) for g, v in real_groups.items() if len(v) == args.group_size}
    real_groups = [v for _, v in sorted(by_group.items())]

    # Dynamic group placement: home = the node fixed by the task's image (hash of image_key), least = the node with
    # fewer live sandboxes (DSec's less-loaded choice with N = 2). Slots are handed out per group, so a node can hold
    # more or fewer than --per-node sandboxes.
    free_slots = {n["index"]: list(range(239, -1, -1)) for n in nodes}
    live_count = {n["index"]: 0 for n in nodes}
    p2c_rng = random.Random(f"{args.seed}-p2c")

    def home_node(entry):
        key = entry.get("image_key", entry["name"]).encode()
        return nodes[int(hashlib.sha1(key).hexdigest(), 16) % len(nodes)]

    def choose_node(entry):
        """Node for a whole group under home or least placement; the caller holds the lock."""
        if args.placement == "home":
            return home_node(entry)
        return min(nodes, key=lambda n: (live_count[n["index"]], n["index"]))

    def group_loop(group, members):
        """One GRPO group slot: G rollouts of one task start within the jitter and the slot's next group waits for
        the slowest. Slots run independently, so the fleet is asynchronous across groups (nodes never drain)."""
        rng = random.Random(f"{args.seed}-group-{group}")
        if stop.wait(rng.uniform(0, args.ramp_s)):
            return
        step = 0
        while time.monotonic() < deadline and not stop.is_set():
            if real_groups:
                chosen = rng.choice(real_groups)
                entry = chosen[0]
            else:
                entry = rng.choice(entries)
                chosen = [entry] * len(members)
            delays = [rng.uniform(0, args.group_jitter_s) for _ in members]
            results = [None] * len(members)
            placed = members
            if args.placement in ("home", "least", "p2c"):
                # Dynamic slots, released as each member ends (as in plan_loop); p2c picks per member.
                with lock:
                    placed = []
                    target = None if args.placement == "p2c" else choose_node(entry)
                    for _ in members:
                        node = target or min(p2c_rng.sample(nodes, min(2, len(nodes))),
                                             key=lambda n: (live_count[n["index"]], p2c_rng.random()))
                        placed.append((node, free_slots[node["index"]].pop()))
                        live_count[node["index"]] += 1

            def member(i, node, slot):
                try:
                    results[i] = run_sandbox(chosen[i], node, slot, delays[i])
                finally:
                    if placed is not members:
                        with lock:
                            live_count[node["index"]] -= 1
                            free_slots[node["index"]].append(slot)

            begin = time.monotonic_ns()
            workers = [threading.Thread(target=member, args=(i, n, s)) for i, (n, s) in enumerate(placed)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            done = [r for r in results if r]
            with lock:
                steps.write(json.dumps({"group": group, "step": step, "trace": entry["name"], "begin_ns": begin,
                                        "node": placed[0][0]["index"], "member_node": [n["index"] for n, _ in placed],
                                        "rl_group": entry.get("group"), "traces": [e["name"] for e in chosen],
                                        "end_ns": time.monotonic_ns(), "members": len(members),
                                        "completed": sum(bool(r.get("completed")) for r in done),
                                        "member_end_ns": [r["end_ns"] for r in done]}) + "\n")
                steps.flush()
            step += 1

    def prepare(item):
        try:
            item[1].prepare()
        except Exception as exc:
            item[1].prepare_error = str(exc)

    def sync_loop(placements):
        """Synchronous RL: every step starts B groups (all slots) together after preparing their disks, and the next
        step waits for the slowest of all rollouts. The group draw depends only on the seed, so conditions pair exactly."""
        rng = random.Random(f"{args.seed}-sync")
        count = len(placements) // args.group_size
        step = 0
        while step < args.sync_steps and time.monotonic() < deadline and not stop.is_set():
            if real_groups:
                groups = rng.sample(real_groups, count) if len(real_groups) >= count else [rng.choice(real_groups) for _ in range(count)]
            else:
                groups = [[e] * args.group_size for e in (rng.choice(entries) for _ in range(count))]
            chosen = [e for g in groups for e in g]
            delays = [rng.uniform(0, args.group_jitter_s) for _ in chosen]
            prepare_begin = time.monotonic_ns()
            boxes = [new_box(e, n, s) for e, (n, s) in zip(chosen, placements)]
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(prepare, boxes))
            results = [None] * len(chosen)

            def member(i):
                results[i] = run_sandbox(chosen[i], *placements[i], delays[i], boxes[i])

            begin = time.monotonic_ns()
            workers = [threading.Thread(target=member, args=(i,)) for i in range(len(chosen))]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            done = [r for r in results if r]
            with lock:
                steps.write(json.dumps({"group": "all", "step": step, "trace": chosen[0]["name"], "begin_ns": begin,
                                        "prepare_s": round((begin - prepare_begin) / 1e9, 3),
                                        "rl_groups": [g[0].get("group") for g in groups],
                                        "traces": [e["name"] for e in chosen], "end_ns": time.monotonic_ns(),
                                        "members": len(chosen), "completed": sum(bool(r.get("completed")) for r in done),
                                        "member_end_ns": [r["end_ns"] for r in done]}) + "\n")
                steps.flush()
            step += 1
            if args.step_gap_s and stop.wait(args.step_gap_s):
                break

    def plan_loop(plan, first, last):
        """Synchronous steps that follow the original job (rl_rollouts.py plan). Every disk of the step is prepared
        first (outside the measurement); wave w launches at its offset from the step start, and each member boots
        after its recorded start delay. Nodes are chosen per group when its wave launches (home/least), or member by
        member (spread, or p2c: the less loaded of two random nodes), so `least` sees the first wave's sandboxes. The
        step ends when every member has ended; the idle phase (training) follows, except after the last step."""
        p2c_rng = random.Random(f"{args.seed}-p2c")
        for k in range(first, last + 1):
            if time.monotonic() >= deadline or stop.is_set():
                break
            step = plan["steps"][k]
            waves = step["waves"]
            members = [(w, g, e, wave["start_delay_s"][e["name"]])
                       for w, wave in enumerate(waves) for g in wave["groups"] for e in by_group[g]]
            prepare_begin = time.monotonic_ns()
            boxes = []
            for i, (_, _, e, _) in enumerate(members):
                vm, box = new_box(e, None, None)
                box.net_id = i  # unique while the step runs; steps never overlap
                boxes.append((vm, box))
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(prepare, boxes))
            results, placed, launches = [None] * len(members), [None] * len(members), [None] * len(waves)
            begin = time.monotonic_ns()

            def member(i, node, slot):
                try:
                    results[i] = run_sandbox(members[i][2], node, slot, members[i][3], boxes[i])
                finally:
                    with lock:
                        live_count[node["index"]] -= 1
                        free_slots[node["index"]].append(slot)

            def wave_loop(w):
                mine = [i for i, m in enumerate(members) if m[0] == w]
                if stop.wait(max(0.0, begin / 1e9 + waves[w]["offset_s"] - time.monotonic_ns() / 1e9)):
                    for i in mine:
                        boxes[i][1].stop()
                        shutil.rmtree(boxes[i][1].directory, ignore_errors=True)
                    return
                launches[w] = time.monotonic_ns()
                threads = []
                with lock:
                    for g in waves[w]["groups"]:
                        idx = [i for i in mine if members[i][1] == g]
                        if args.placement == "spread":
                            targets = [nodes[(len(threads) + j) % len(nodes)] for j in range(len(idx))]
                        elif args.placement == "p2c":  # per sandbox, as members are counted in (DSec power-of-k)
                            targets = [None] * len(idx)
                        else:
                            targets = [choose_node(members[idx[0]][2])] * len(idx)
                        for i, node in zip(idx, targets):
                            if node is None:
                                node = min(p2c_rng.sample(nodes, min(2, len(nodes))),
                                           key=lambda n: (live_count[n["index"]], p2c_rng.random()))
                            slot = free_slots[node["index"]].pop()
                            live_count[node["index"]] += 1
                            placed[i] = node["index"]
                            boxes[i][1].node, boxes[i][1].slot = node, slot
                            threads.append(threading.Thread(target=member, args=(i, node, slot)))
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            workers = [threading.Thread(target=wave_loop, args=(w,)) for w in range(len(waves))]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            done = [r for r in results if r]
            with lock:
                steps.write(json.dumps({
                    "group": "all", "step": k, "orig_step": step["orig_step"], "trace": members[0][2]["name"],
                    "begin_ns": begin, "prepare_s": round((begin - prepare_begin) / 1e9, 3),
                    "rl_groups": [g for wave in waves for g in wave["groups"]], "images": step["images"],
                    "wave_offset_s": [wave["offset_s"] for wave in waves], "wave_launch_ns": launches,
                    "traces": [m[2]["name"] for m in members], "member_wave": [m[0] for m in members],
                    "start_delay_s": [m[3] for m in members], "member_node": placed,
                    "member_vm": [vm for vm, _ in boxes], "end_ns": time.monotonic_ns(),
                    "members": len(members), "completed": sum(bool(r.get("completed")) for r in done),
                    "member_end_ns": [r["end_ns"] for r in done], "idle_after_s": step["idle_after_s"]}) + "\n")
                steps.flush()
            if k < last and step["idle_after_s"] and stop.wait(step["idle_after_s"]):
                break

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
        if args.placement == "pack" and args.per_node % args.group_size:
            raise SystemExit("--placement pack needs --per-node to be a multiple of --group-size")

        def place(i):
            # spread: member i of the fleet on node i mod N (a group spans nodes); pack: a whole group on one node
            # (image/snapshot locality, group CoW).
            if args.placement == "pack":
                return nodes[i // args.per_node], i % args.per_node
            return nodes[i % len(nodes)], i // len(nodes)

        if plan:
            threads = [threading.Thread(target=plan_loop, args=(plan, *plan_range))]
        elif args.sync_steps:
            threads = [threading.Thread(target=sync_loop, args=([place(i) for i in range(total)],))]
        else:
            threads = [threading.Thread(target=group_loop, args=(g, [place(i) for i in range(g * args.group_size,
                                                                                             (g + 1) * args.group_size)]))
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
        co_load.finish()
        host_stop.set()
        host_log.join(timeout=5)
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
    if config.get("sync_steps") and (directory / "steps.jsonl").exists():
        # synchronous steps: from the first step's launch to the last step's end (preparation between steps included)
        records = [json.loads(line) for line in (directory / "steps.jsonl").open()]
        if records:
            begin, end = records[0]["begin_ns"], records[-1]["end_ns"]
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
            # A real GRPO group has G different rollouts: the step's solo time is its slowest member's.
            traces = step.get("traces") or [step["trace"]]
            if step["completed"] == step["members"] and step["begin_ns"] >= begin and all(t in expected for t in traces):
                step_slowdown.append((step["end_ns"] - step["begin_ns"]) / 1e9
                                     / (max(expected[t] for t in traces) + config["group_jitter_s"]))
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


def add_llm_args(p):
    p.add_argument("--llm-model", choices=("recorded", "tokens"), default="recorded",
                   help="LLM wait before each response: recorded gap, or t0 + alpha*context + beta*output tokens")
    # Defaults: least-squares fit on the 2.49M turns of sweagent/iter2-rl-rollouts (training engine timing)
    p.add_argument("--llm-t0", type=float, default=6.156)
    p.add_argument("--llm-alpha", type=float, default=0.0)
    p.add_argument("--llm-beta", type=float, default=0.02898)
    p.add_argument("--no-balloon", dest="balloon", action="store_false",
                   help="No virtio-balloon free page reporting")


def main():
    global CANDIDATES, LLM_MODEL, BALLOON
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
    p.add_argument("--plan", type=Path, help="Only the images of this step plan's groups")
    p.add_argument("--plan-steps", help="With --plan: steps a-b only")
    p.add_argument("--unpinned", action="store_true", help="Ignore image-pins.json and pull the current tag")
    p.set_defaults(func=build)
    p = sub.add_parser("drop-images", help="Delete built images of a plan's steps (never those in --keep lists)")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--plan-steps")
    p.add_argument("--keep", type=Path, action="append", default=[],
                   help="Candidates file whose images are kept (default: candidates-rl.json)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=drop_images)
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
    p.add_argument("--plan", type=Path, help="Only the traces of this step plan's groups")
    p.add_argument("--plan-steps", help="With --plan: steps a-b only")
    add_llm_args(p)
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
    p.add_argument("--placement", choices=("spread", "pack", "home", "least", "p2c"), default="spread",
                   help="Group mode: spread a group over nodes (i mod N), pack it on one node (fixed per group slot), "
                        "home (the node fixed by the task's image), or least (the node with fewer live sandboxes); "
                        "p2c (with --sync-plan): each sandbox on the less loaded of two random nodes")
    p.add_argument("--sync-steps", type=int, default=0,
                   help="Group mode: synchronous RL steps (all groups start together, next step waits for the slowest); "
                        "the run ends after this many steps or --duration-s. Use --ramp-s 0")
    p.add_argument("--step-gap-s", type=float, default=0, help="Idle time between synchronous steps (training phase)")
    p.add_argument("--sync-plan", type=Path,
                   help="Group mode: synchronous steps from a step plan (rl_rollouts.py plan): two waves, recorded "
                        "start delays, idle between steps, new groups every step. Use --ramp-s 0 and a long --duration-s")
    p.add_argument("--plan-steps", help="Steps of the plan to run, e.g. 0-1 (default: all)")
    p.add_argument("--base-pmem", action="store_true",
                   help="Base image as a read-only virtio-pmem device mounted with DAX (guest reads the host page cache)")
    p.add_argument("--mem-sample-s", type=float, default=0,
                   help="Guest memory/disk sampling interval (SWE_MEM serial lines, kept in sandboxes.jsonl); 0 = off")
    p.add_argument("--be", help="Co-located best-effort load KIND:LEVEL on the BE nodes' CPUs, e.g. mix:0.3 "
                                "(kinds cpu, avx, mem, mix; LEVEL = busy fraction of every CPU)")
    p.add_argument("--be-nodes", help="Node indices that get the BE load, e.g. 0 or 0,1 (default: all)")
    p.add_argument("--be-qos", choices=("none", "idle", "core", "rdt"), default="none",
                   help="BE isolation: none; idle = SCHED_IDLE; core = + core scheduling; rdt = + L3 ways and MB cap")
    p.add_argument("--be-rdt", default="4:30", help="WAYS:MB_PERCENT for BE with --be-qos rdt (L3 has 15 ways)")
    p.add_argument("--be-mem-mb", type=int, default=64, help="Stream array per BE thread (mem/mix kinds)")
    p.add_argument("--be-warmup-s", type=float, default=10)
    p.add_argument("--ls-resctrl", help="Existing resctrl group (tasks file writable by this user) for every sandbox, "
                                        "e.g. with fewer L3 ways to keep LLC per core when the nodes are shrunk")
    add_llm_args(p)
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
    if getattr(args, "llm_model", "recorded") == "tokens":
        LLM_MODEL = (args.llm_t0, args.llm_alpha, args.llm_beta)
    BALLOON = getattr(args, "balloon", True)
    args.func(args)


if __name__ == "__main__":
    main()
