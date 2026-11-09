#!/usr/bin/env python3
"""CPU cost of creating a sandbox environment, step by step, for a few RL images.

  pull     download the image's compressed layers from Docker Hub (curl, one at a time): wall, bytes, CPU
  unpack   gunzip + untar each layer in order into one directory (what an image unpack does): wall, CPU
  ext4     build the microVM root disk from that directory (mkfs.ext4 -d), and copy it once (a per-VM disk copy)
  start    for an image already in the local Docker store: `docker run --rm IMAGE true`, with the CPU that dockerd and
           containerd spend on it (their systemd cgroups) and the container's own

CPU is user + system seconds of the step's processes (/usr/bin/time), so "cores" = CPU / wall.

  python3 env_cost.py IMAGE... --work DIR [--docker LOCAL_IMAGE...] [--out FILE]
"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import sandbox

CGROUPS = [Path("/sys/fs/cgroup/system.slice/docker.service/cpu.stat"),
           Path("/sys/fs/cgroup/system.slice/containerd.service/cpu.stat")]


def timed(argv, **kwargs):
    """(wall s, CPU s) of a command under /usr/bin/time."""
    t0 = time.monotonic()
    p = subprocess.run(["/usr/bin/time", "-f", "TIME %U %S", *argv], capture_output=True, text=True, **kwargs)
    wall = time.monotonic() - t0
    line = [x for x in p.stderr.splitlines() if x.startswith("TIME ")][-1].split()
    return wall, float(line[1]) + float(line[2]), p.returncode


def manifest(reference):
    ref = reference.removeprefix("docker.io/")
    name, tag = ref.rsplit(":", 1) if ":" in ref.split("/")[-1] else (ref, "latest")
    token = json.loads(sandbox.output(["curl", "-sf", "--retry", "5",
                                       f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{name}:pull"]))["token"]
    auth = ["-H", f"Authorization: Bearer {token}"]

    def get(path):
        return json.loads(subprocess.run(["curl", "-sfL", "--retry", "5", *auth, "-H", f"Accept: {sandbox.MANIFEST_TYPES}",
                                          f"{sandbox.REGISTRY}/v2/{name}/{path}"], check=True, capture_output=True).stdout)

    m = get(f"manifests/{tag}")
    if "manifests" in m:
        m = get("manifests/" + next(x["digest"] for x in m["manifests"]
                                    if x.get("platform", {}).get("os") == "linux" and x["platform"].get("architecture") == "amd64"))
    return name, auth, m["layers"]


def image_cost(reference, work):
    work.mkdir(parents=True, exist_ok=True)
    name, auth, layers = manifest(reference)
    out = {"image": reference, "layers": len(layers), "compressed_bytes": sum(x["size"] for x in layers)}
    wall = cpu = 0.0
    paths = []
    for i, layer in enumerate(layers):
        path = work / f"layer{i:03d}.tgz"
        for attempt in range(4):
            w, c, rc = timed(["curl", "-sfL", "--retry", "8", *auth, "-o", str(path),
                              f"{sandbox.REGISTRY}/v2/{name}/blobs/{layer['digest']}"])
            wall, cpu = wall + w, cpu + c
            if not rc:
                break
            if attempt == 3:
                raise RuntimeError(f"download failed: {reference} layer {i}")
            name, auth, _ = manifest(reference)  # tokens expire after about 5 minutes
        paths.append(path)
    out["pull"] = {"wall_s": wall, "cpu_s": cpu}
    root = work / "root"
    root.mkdir()
    wall = cpu = 0.0
    for path in paths:
        w, c, _ = timed(["tar", "-xzf", str(path), "-C", str(root), "--no-same-owner", "--no-same-permissions",
                         "--exclude=dev/*"])  # device nodes need root; errors on them are expected and skipped
        wall, cpu = wall + w, cpu + c
    out["unpack"] = {"wall_s": wall, "cpu_s": cpu,
                     "bytes": int(sandbox.output(["du", "-sb", str(root)]).split()[0])}
    disk = work / "rootfs.ext4"
    subprocess.run(["truncate", "-s", f"{int(out['unpack']['bytes'] * 1.3) + (1 << 30)}", str(disk)], check=True)
    w, c, _ = timed(["mkfs.ext4", "-q", "-F", "-d", str(root), str(disk)])
    out["ext4"] = {"wall_s": w, "cpu_s": c}
    w, c, _ = timed(["cp", "--sparse=always", str(disk), str(work / "copy.ext4")])
    out["copy"] = {"wall_s": w, "cpu_s": c}
    shutil.rmtree(work)
    return out


def cgroup_usage():
    return sum(int(next(l for l in p.read_text().splitlines() if l.startswith("usage_usec")).split()[1])
               for p in CGROUPS if p.exists()) / 1e6


def start_cost(image, runs=5):
    rows = []
    for _ in range(runs):
        before = cgroup_usage()
        w, c, rc = timed(["docker", "run", "--rm", "--network", "none", image, "true"])
        rows.append({"wall_s": w, "client_cpu_s": c, "daemon_cpu_s": cgroup_usage() - before, "rc": rc})
    return {"image": image, "runs": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="*")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--docker", nargs="*", default=[], help="Local Docker images for the start step")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = {"images": [], "start": []}
    for kind, images, fn in (("images", args.images, lambda i: image_cost(i, args.work / i.replace("/", "_").replace(":", "_"))),
                             ("start", args.docker, start_cost)):
        for image in images:
            r = fn(image)
            report[kind].append(r)
            print(json.dumps(r, default=lambda x: round(x, 3)), flush=True)
            if args.out:  # after every image, so a later failure keeps the earlier results
                args.out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
