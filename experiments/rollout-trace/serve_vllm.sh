#!/usr/bin/env bash
# Start vLLM for trace collection.
# Defaults are for deep8 (shared A6000 server): GPU0 belongs to another
# user and port 8000 is taken, so GPU1,2 (NV4-linked, NUMA1) with TP2 on
# port 18000. On a cloud H100 node set GPUS/CPUSET (and TP) to suit.
# Refuses to start if a chosen GPU already has a process or the port is
# taken. The image is pinned so A6000 and H100 run the same vLLM.
#
# usage: [GPUS=1,2] [TP=2] [PORT=18000] [CPUSET=16-23,48-55] \
#        serve_vllm.sh <model dir> <served name> [extra vllm args...]
#   deep8: serve_vllm.sh $DATA_ROOT/models/agentica-org--DeepSWE-Preview deepswe --quantization fp8
#   H100:  GPUS=0,1 CPUSET=0-7 serve_vllm.sh /data/models/agentica-org--DeepSWE-Preview deepswe
set -euo pipefail
model=$1 name=$2; shift 2
image=vllm/vllm-openai:v0.28.0   # sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14
GPUS=${GPUS:-1,2}
TP=${TP:-2}
PORT=${PORT:-18000}
CPUSET=${CPUSET:-16-23,48-55}

for g in ${GPUS//,/ }; do
  used=$(nvidia-smi -i "$g" --query-compute-apps=pid --format=csv,noheader | wc -l)
  [ "$used" -eq 0 ] || { echo "GPU $g is in use; not starting"; exit 1; }
done
if ss -ltnH | awk '{print $4}' | grep -q ":$PORT\$"; then echo "port $PORT in use"; exit 1; fi

exec docker run --rm --name "trace-vllm-$name" \
  --label trace.run=vllm --label trace.owner="$USER" \
  --gpus "\"device=$GPUS\"" --cpuset-cpus "$CPUSET" --shm-size 16g \
  -p 127.0.0.1:$PORT:8000 \
  -v "$model":/model:ro \
  "$image" \
  --model /model --served-model-name "$name" \
  --tensor-parallel-size "$TP" --max-model-len 36864 \
  --gpu-memory-utilization 0.92 --enable-prefix-caching \
  --enable-prompt-tokens-details "$@"
