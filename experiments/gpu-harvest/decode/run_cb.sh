#!/bin/bash
# Co-location runs: engine on CPUs 4-7 (GPU 0) with or without 2 streams of replayed C4 sandbox commands on the same
# CPUs, plus sandbox alone. 2 repetitions, shuffled order (seed 1). Outputs in $OUT (default results/decode-cb).
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
OUT=${OUT:-$ROOT/experiments/gpu-harvest/results/decode-cb}
PY=$ROOT/.venv-train/bin/python
DUR=120
cd $HERE; mkdir -p $OUT; export CUDA_VISIBLE_DEVICES=0 PYTHONPATH=$HERE
conds=()
for rep in 1 2; do
  for e in vllm-sync vllm-async K8-sync K8-overlap; do conds+=("$e:0:$rep" "$e:2:$rep"); done
  conds+=("none:2:$rep")
done
mapfile -t conds < <(printf '%s\n' "${conds[@]}" | shuf --random-source=<(yes 1))
for c in "${conds[@]}"; do
  IFS=: read e sb rep <<< "$c"; run="$e-sb$sb-r$rep"; mark=$OUT/$run.mark; rm -f $mark
  echo "$(date +%T) start $run"
  case $e in
    vllm-sync)  MARK=$mark timeout 600 taskset -c 4-7 $PY vllm_cb.py 256 $DUR > $OUT/$run.log 2>&1 & ;;
    vllm-async) MARK=$mark ASYNC=1 timeout 600 taskset -c 4-7 $PY vllm_cb.py 256 $DUR > $OUT/$run.log 2>&1 & ;;
    K8-sync)    MARK=$mark timeout 600 taskset -c 4-7 $PY msengine.py 256 8 sync $DUR > $OUT/$run.log 2>&1 & ;;
    K8-overlap) MARK=$mark timeout 600 taskset -c 4-7 $PY msengine.py 256 8 overlap $DUR > $OUT/$run.log 2>&1 & ;;
    none) date +%s.%N > $mark; epid=$$ ;;
  esac
  [ $e != none ] && epid=$!
  until [ -f $mark ] || ! kill -0 $epid 2>/dev/null; do sleep 1; done
  if [ $sb -gt 0 ]; then sg docker -c "taskset -c 8-11 python3 sbreplay.py $OUT/$run.jsonl $sb $DUR 4-7"; fi
  [ $e != none ] && wait $epid
  pkill -u $(id -u) -f "[V]LLM::" ; sleep 15
done
echo "$(date +%T) all done"
