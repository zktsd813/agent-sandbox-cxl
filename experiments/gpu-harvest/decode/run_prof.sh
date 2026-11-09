#!/usr/bin/env bash
# usage: run_prof.sh LABEL MODE(spin|blkfill) BATCH [SECONDS] -> $OUT/LABEL.log (default results/decode-prof)
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
OUT=${OUT:-$ROOT/experiments/gpu-harvest/results/decode-prof}
cd $ROOT
label=$1 mode=$2 batch=$3 secs=${4:-30}
mkdir -p $OUT
fill=()
if [ "$mode" = blkfill ]; then
  export PYTHONPATH=experiments/gpu-harvest/hook HARVEST_PHASE_LOG=$OUT/phases-null.jsonl HARVEST_CUDA_BLOCKING=1
  for c in 4 5 6 7; do taskset -c $c chrt -i 0 $HERE/../bench/pause & fill+=($!); done
fi
CUDA_VISIBLE_DEVICES=0 taskset -c 4-7 .venv-train/bin/python $HERE/prof_vllm.py $label $batch $secs > $OUT/$label.log 2>&1
[ ${#fill[@]} -gt 0 ] && kill ${fill[@]}
sed -n '/^RESULT/,$p' $OUT/$label.log | tail -n +2
