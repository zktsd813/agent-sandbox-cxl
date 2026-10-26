"""Write <root>/_global/manifest.json: hardware, versions, commits, models."""

import json
import platform
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()
    except subprocess.TimeoutExpired:
        return None


def write(root, extra):
    m = {
        "time": time.time(),
        "host": platform.node(),
        "kernel": platform.release(),
        "cpu": sh("lscpu | grep 'Model name' | cut -d: -f2").strip(),
        "nproc": sh("nproc"),
        "mem_gb": sh("free -g | awk '/Mem:/{print $2}'"),
        "gpus": sh("nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader"),
        "gpu_topo": sh("nvidia-smi topo -m"),
        "docker": sh("docker version --format '{{.Server.Version}}'"),
        "cgroup": sh("docker info --format '{{.CgroupDriver}} {{.CgroupVersion}}'"),
        "repo_commit": sh(f"git -C {HERE} rev-parse HEAD"),
        "repo_dirty": bool(sh(f"git -C {HERE} status --porcelain -- .")),
        **extra,
    }
    out = Path(root) / "_global"
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(m, indent=1))
    return m
