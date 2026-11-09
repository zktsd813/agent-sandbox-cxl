#!/usr/bin/env bash
# G1 run: verl GRPO on GSM8K inside the "GPU node" cpuset, with the 100 ms sampler and phase log.
# Usage: bash experiments/gpu-harvest/run_g1.sh CONFIG OUT_DIR [STEPS] [CPUS]
#   CONFIG: C1 (Qwen2.5-3B, no offload) | C1o (C1 + verl param/optimizer offload) | C2 (Qwen3-8B, FSDP2 CPUOffloadPolicy)
#           | C3 (Qwen2.5-3B, one-step-off-policy: GPU0 rollout, GPU1 training; recipe in .local/verl-v0.6.1)
#   CPUS:   training cpuset, default node0 = 0-15,32-47 (16 threads per GPU)
#   NGPUS=1 runs on GPU0 only with half the batch (same per-GPU work); OMP sets OMP_NUM_THREADS (setup.sh uses 4).
#   SMALL picks the C1/C1o/C3 model under /mnt/ssd1/Qwen (3B full fine-tuning needs ~48 GB, so 1-GPU runs use 1.5B).
#   HARVEST_CUDA_BLOCKING=1 makes CUDA waits block instead of spin (hook/sitecustomize.py).
#   SAVE=1 keeps checkpoint saving (every 10 steps and at the last step; ~100 GB per save for C2). Default off.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
HERE="$ROOT/experiments/gpu-harvest"
CONFIG=$1 OUT=$(realpath -m "$2") STEPS=${3:-20} CPUS=${4:-0-15,32-47}
NGPUS=${NGPUS:-2} OMP=${OMP:-4} SAVE=${SAVE:-0} SMALL=${SMALL:-Qwen2.5-3B-Instruct}
MGMT_CPUS=31,63
VENV="$ROOT/.venv-train"
DATA="$HERE/data/gsm8k"
MODELS=/mnt/ssd1/Qwen
mkdir -p "$OUT"

# Same runtime environment as setup.sh runtime_env, without its sudo re-exec (which drops PYTHONPATH).
user_cache="$ROOT/.cache/runtime-$(id -u)"
export VIRTUAL_ENV="$VENV" PATH="$VENV/bin:$PATH" OMP_NUM_THREADS="$OMP"
export HF_HOME="$user_cache/huggingface" VLLM_CACHE_ROOT="$user_cache/vllm"
export TRITON_CACHE_DIR="$user_cache/triton" TORCHINDUCTOR_CACHE_DIR="$user_cache/torchinductor"
export RAY_TMPDIR="${TMPDIR:-/tmp}/asxl-$(id -u)"
mkdir -p -m 700 "$RAY_TMPDIR"
unset CUDA_HOME CUDA_PATH
[[ -d /usr/local/cuda-12.8 ]] && export CUDA_HOME=/usr/local/cuda-12.8
"$VENV/bin/python" "$ROOT/dependency_patches.py" >/dev/null
export PYTHONPATH="$HERE/hook${PYTHONPATH:+:$PYTHONPATH}" HARVEST_PHASE_LOG="$OUT/phases.jsonl"
: > "$HARVEST_PHASE_LOG"

[[ -f "$DATA/train.parquet" ]] || "$VENV/bin/python" "$HERE/prepare_gsm8k.py" "$HERE/data/gsm8k-raw" "$DATA"

common=(
    algorithm.adv_estimator=grpo algorithm.use_kl_in_reward=False
    data.train_files="$DATA/train.parquet" data.val_files="$DATA/test.parquet"
    data.max_prompt_length=512 data.max_response_length=1024
    data.filter_overlong_prompts=True data.truncation=error data.seed=1
    actor_rollout_ref.model.use_remove_padding=True
    actor_rollout_ref.model.enable_gradient_checkpointing=True
    actor_rollout_ref.actor.optim.lr=1e-6
    actor_rollout_ref.actor.use_kl_loss=False actor_rollout_ref.actor.entropy_coeff=0
    actor_rollout_ref.rollout.name=vllm actor_rollout_ref.rollout.n=5
    trainer.critic_warmup=0 trainer.logger=[console] trainer.val_before_train=False
    trainer.project_name=gpu-harvest trainer.experiment_name="$CONFIG"
    trainer.nnodes=1
    trainer.total_epochs=1 trainer.total_training_steps="$STEPS"
    trainer.save_freq=$([[ $SAVE == 1 ]] && echo 10 || echo -1) trainer.test_freq=10
    trainer.default_local_dir="$OUT/checkpoints"
    actor_rollout_ref.actor.checkpoint.save_contents=[model,optimizer,extra]
)
module=verl.trainer.main_ppo
case $CONFIG in
    C1|C1o)
        offload=False; [[ $CONFIG == C1o ]] && offload=True
        config=(
            trainer.n_gpus_per_node="$NGPUS"
            actor_rollout_ref.model.path="$MODELS/$SMALL"
            data.train_batch_size=$((64 * NGPUS))
            actor_rollout_ref.actor.ppo_mini_batch_size=$((32 * NGPUS)) actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=16
            actor_rollout_ref.actor.fsdp_config.param_offload=$offload
            actor_rollout_ref.actor.fsdp_config.optimizer_offload=$offload
            actor_rollout_ref.rollout.tensor_model_parallel_size=1
            actor_rollout_ref.rollout.gpu_memory_utilization=0.5
            actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=32
        ) ;;
    C2)
        config=(
            trainer.n_gpus_per_node="$NGPUS"
            actor_rollout_ref.model.path="$MODELS/Qwen3-8B"
            data.train_batch_size=$((32 * NGPUS))
            actor_rollout_ref.actor.strategy=fsdp2 critic.strategy=fsdp2
            actor_rollout_ref.actor.fsdp_config.offload_policy=True
            actor_rollout_ref.actor.ppo_mini_batch_size=$((16 * NGPUS)) actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=4
            actor_rollout_ref.rollout.tensor_model_parallel_size="$NGPUS"
            actor_rollout_ref.rollout.gpu_memory_utilization=0.6
            actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=8
        ) ;;
    C3)
        module=recipe.one_step_off_policy.main_ppo
        export PYTHONPATH="$PYTHONPATH:$HERE/.local/verl-v0.6.1"
        config=(
            # the recipe's searchpath is file://verl/trainer/config, relative to the source tree
            'hydra.searchpath=[pkg://verl.trainer.config]'
            trainer.n_gpus_per_node=1 rollout.nnodes=1 rollout.n_gpus_per_node=1
            actor_rollout_ref.hybrid_engine=False
            actor_rollout_ref.model.path="$MODELS/$SMALL"
            data.train_batch_size=128
            actor_rollout_ref.actor.strategy=fsdp2 critic.strategy=fsdp2
            actor_rollout_ref.actor.ppo_mini_batch_size=64 actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=16
            actor_rollout_ref.actor.fsdp_config.param_offload=False
            actor_rollout_ref.actor.fsdp_config.optimizer_offload=False
            actor_rollout_ref.rollout.tensor_model_parallel_size=1
            actor_rollout_ref.rollout.gpu_memory_utilization=0.6
            actor_rollout_ref.rollout.load_format=safetensors actor_rollout_ref.rollout.layered_summon=True
            actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=32
        ) ;;
    *) printf 'unknown CONFIG %s\n' "$CONFIG" >&2; exit 2 ;;
esac
if [[ $NGPUS == 1 ]]; then
    [[ $CONFIG == C1 || $CONFIG == C1o || $CONFIG == C2 ]] || { printf 'NGPUS=1 needs C1, C1o or C2\n' >&2; exit 2; }
    export CUDA_VISIBLE_DEVICES=0
fi

"$VENV/bin/python" - "$OUT/env.json" "$CONFIG" "$CPUS" "$STEPS" "$NGPUS" "$OMP" "$SMALL" <<'PY'
import json, os, subprocess, sys, time
def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
def cat(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None
json.dump({
    "config": sys.argv[2], "train_cpus": sys.argv[3], "steps": int(sys.argv[4]),
    "ngpus": int(sys.argv[5]), "omp_num_threads": int(sys.argv[6]), "small_model": sys.argv[7],
    "cuda_blocking_sync": os.environ.get("HARVEST_CUDA_BLOCKING") == "1",
    "start_wall": time.time(), "start_monotonic_ns": time.monotonic_ns(),
    "smt_active": cat("/sys/devices/system/cpu/smt/active"),
    "governor": cat("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
    "no_turbo": cat("/sys/devices/system/cpu/intel_pstate/no_turbo"),
    "resctrl_mounted": "resctrl" in sh("mount"),
    "gpu_clocks": sh("nvidia-smi --query-gpu=index,clocks.sm,clocks.max.sm,clocks_event_reasons.active --format=csv,noheader"),
    "gpu_processes": sh("nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader"),
    "loadavg": cat("/proc/loadavg"),
    "other_users_top": sh("ps -eo user,pcpu,comm --sort=-pcpu --no-headers | awk '$1!=\"ijkim\"' | head -5"),
    "repo_commit": sh("git -C %s rev-parse HEAD" % os.environ["VIRTUAL_ENV"].rsplit("/", 1)[0]),
    "rllm_commit": sh("git -C %s/vendor/rllm rev-parse HEAD" % os.environ["VIRTUAL_ENV"].rsplit("/", 1)[0]),
    "kernel": os.uname().release,
}, open(sys.argv[1], "w"), indent=2)
PY

numactl --physcpubind="$CPUS" --membind=0 \
    "$VENV/bin/python" -m "$module" "${common[@]}" "${config[@]}" > "$OUT/train.log" 2>&1 &
train_pid=$!
taskset -c "$MGMT_CPUS" "$VENV/bin/python" "$HERE/sampler.py" --out "$OUT/samples" --root-pid "$train_pid" \
    > "$OUT/sampler.log" 2>&1 &
sampler_pid=$!
status=0
wait "$train_pid" || status=$?
kill -TERM "$sampler_pid" 2>/dev/null || true
wait "$sampler_pid" || true
ray stop --force >/dev/null 2>&1 || true
printf '{"exit_status": %d, "end_wall": %s}\n' "$status" "$(date +%s.%N)" > "$OUT/done.json"
exit "$status"
