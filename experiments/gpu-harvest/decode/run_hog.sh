#!/usr/bin/env bash
# usage: run_hog.sh NHOG_PER_CORE BATCH ENGINE(vllm|K) -> one RESULT line. Hogs are SCHED_OTHER pause loops on cores 4-7.
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
cd $ROOT
n=$1 batch=$2 eng=$3 hogs=()
for c in 4 5 6 7; do for i in $(seq 1 $n); do taskset -c $c $HERE/../bench/pause & hogs+=($!); done; done
if [ "$eng" = vllm ]; then
  CUDA_VISIBLE_DEVICES=0 taskset -c 4-7 .venv-train/bin/python $HERE/prof_vllm.py hog$n $batch 20 2>&1 | grep -m1 "wall/step"
else
  CUDA_VISIBLE_DEVICES=0 taskset -c 4-7 .venv-train/bin/python $HERE/msdecode.py bench $batch $eng 15 2>&1 | grep RESULT | python3 -c "import json,sys; r=json.loads(sys.stdin.read()[7:]); print(f\"K={r['K']} batch={r['batch']} wall/step={r['wall_ms_per_step']:.2f}ms host_cpu/step={r['host_cpu_ms_per_step']:.3f}ms\")"
fi
[ ${#hogs[@]} -gt 0 ] && kill ${hogs[@]}
