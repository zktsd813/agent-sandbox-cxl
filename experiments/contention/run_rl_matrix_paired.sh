#!/usr/bin/env bash
# Virtual-node motivation matrix on one host (docs/virtual-node-experiment-design.md, E1/E2/E4):
# NUMA 0 cores 2-31 as two isolated 15-core nodes (ISO) or one pooled 30-core node (POOL), RL rollouts
# (candidates-rl.json, real GRPO groups of 8), token-model LLM waits. NUMA 1 is not used.
# Paired variant: every condition uses the same seed, so ISO and POOL draw the same group sequence.
# SYNC_STEPS=N runs synchronous RL steps (all groups start together, the next step waits for the slowest; set RAMP_S=0).
# PILOT_ARGS adds pilot options (e.g. "--base-pmem --mem-sample-s 5"). CACHE_RESET between conditions: drop (whole
# host page cache, default), images (only the image files, for shared hosts), none. IMAGE_CACHE_LOG=1 samples the
# images' host page cache every 60 s into <run>/image-cache.csv.
# Usage: run_rl_matrix.sh SOLO_RUN_DIR CONDITION... where CONDITION = name:per_node:pool:placement:gap_scale:move
set -uo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
solo="$1"; shift
ramp="${RAMP_S:-600}"; duration="${DURATION_S:-1800}"; seed="${SEED:-1}"; sync="${SYNC_STEPS:-0}"; reset="${CACHE_RESET:-drop}"
log="${MATRIX_LOG:-/Serverless/agent-sandbox-cxl/logs/rl-matrix.log}"
for condition in "$@"; do
    IFS=: read -r name per_node pool placement scale move <<< "$condition"
    echo "$(date -u +%FT%TZ) START $condition" >> "$log"
    sampler=""
    if [[ "${IMAGE_CACHE_LOG:-0}" == 1 ]]; then
        cache_log="$(mktemp -p /tmp image-cache.XXXX.csv)"
        taskset -c 0 nice -n 19 python3 "$here/image_blocks.py" resident --out "$cache_log" --interval 60 &
        sampler=$!
    fi
    out=$(numactl --cpunodebind=0 --membind=0 python3 "$here/sandbox.py" --candidates "$here/candidates-rl.json" pilot \
        --nodes 2-16/17-31 --sibling-offset 0 --harness-cpus 0-1 --per-node "$per_node" --pool "$pool" \
        --group-size 8 --placement "$placement" --llm-model tokens --gap-scale "$scale" --move "$move" \
        --ramp-s "$ramp" --duration-s "$duration" --seed "$seed" --sync-steps "$sync" ${PILOT_ARGS:-} 2>&1 | tail -3)
    [[ -n "$sampler" ]] && kill "$sampler"
    run=$(echo "$out" | grep -o '/[^ ]*-pilot-[^ ]*' | tail -1)
    echo "$(date -u +%FT%TZ) DONE $condition -> $run" >> "$log"
    echo "$out" >> "$log"
    if [[ -n "$run" && -d "$run" ]]; then
        echo "$name $condition" > "$run/condition.txt"
        [[ -n "$sampler" ]] && mv "$cache_log" "$run/image-cache.csv"
        taskset -c 0-1 python3 "$here/sandbox.py" --candidates "$here/candidates-rl.json" episodes "$run" --solo "$solo" --quiet \
            >> "$log" 2>&1 || echo "episodes failed for $run" >> "$log"
    fi
    case "$reset" in
        drop) sudo -n python3 -c 'open("/proc/sys/vm/drop_caches","w").write("1")' 2>/dev/null || true ;;
        images) taskset -c 0 python3 "$here/image_blocks.py" evict >> "$log" 2>&1 ;;
    esac
done
echo "$(date -u +%FT%TZ) MATRIX FINISHED" >> "$log"
