#!/usr/bin/env bash
# G1 C4: the real agentic SWE RL loop (train.py: Qwen3-32B LoRA, FSDP2 CPUOffloadPolicy, R2E-Gym Docker sandboxes)
# in the "GPU node" cpuset, with the 100 ms sampler, Docker scope sampling and phase log.
# Usage: bash experiments/gpu-harvest/run_c4.sh OUT_DIR HOURS [CPUS]
#   Runs a new train.py output under OUT_DIR/train until the deadline (HOURS from now); ~85 min per step.
#   Docker containers are created by dockerd, so they are not inside the cpuset; their CPU is sampled per scope.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
HERE="$ROOT/experiments/gpu-harvest"
OUT=$(realpath -m "$1") HOURS=$2 CPUS=${3:-0-15,32-47}
mkdir -p "$OUT"
deadline=$(date -d "@$(( $(date +%s) + $(printf %.0f "$(echo "$HOURS * 3600" | bc)") ))" --iso-8601=seconds)
export PYTHONPATH="$HERE/hook" HARVEST_PHASE_LOG="$OUT/phases.jsonl"
: > "$HARVEST_PHASE_LOG"
"$ROOT/.venv-train/bin/python" - "$OUT/env.json" "$CPUS" "$deadline" <<'PY'
import json, subprocess, sys, time
def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
json.dump({"config": "C4", "train_cpus": sys.argv[2], "deadline": sys.argv[3],
           "start_wall": time.time(), "start_monotonic_ns": time.monotonic_ns(),
           "smt_active": open("/sys/devices/system/cpu/smt/active").read().strip(),
           "governor": open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip(),
           "gpu_processes": sh("nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader"),
           "loadavg": open("/proc/loadavg").read().strip()}, open(sys.argv[1], "w"), indent=2)
PY
cd "$ROOT"
# sg gives this process tree the docker group without setup.sh's sudo re-exec, which would drop PYTHONPATH.
sg docker -c "numactl --physcpubind=$CPUS --membind=0 bash setup.sh run python train.py \
    --output '$OUT/train' --until '$deadline'" > "$OUT/train.log" 2>&1 &
train_pid=$!
taskset -c 31,63 "$ROOT/.venv-train/bin/python" "$HERE/sampler.py" --out "$OUT/samples" --root-pid "$train_pid" \
    --cgroup-glob '/sys/fs/cgroup/system.slice/docker-*.scope' > "$OUT/sampler.log" 2>&1 &
sampler_pid=$!
status=0
wait "$train_pid" || status=$?
kill -TERM "$sampler_pid" 2>/dev/null || true
wait "$sampler_pid" || true
printf '{"exit_status": %d, "end_wall": %s}\n' "$status" "$(date +%s.%N)" > "$OUT/done.json"
exit "$status"
