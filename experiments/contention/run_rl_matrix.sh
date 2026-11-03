#!/usr/bin/env bash
# Virtual-node motivation matrix on one host (docs/virtual-node-experiment-design.md, E1/E2/E4):
# NUMA 0 cores 2-31 as two isolated 15-core nodes (ISO) or one pooled 30-core node (POOL), RL rollouts
# (candidates-rl.json, real GRPO groups of 8), token-model LLM waits. NUMA 1 is not used.
# Usage: run_rl_matrix.sh SOLO_RUN_DIR CONDITION... where CONDITION = name:per_node:pool:placement:gap_scale:move
set -uo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
solo="$1"; shift
ramp="${RAMP_S:-600}"; duration="${DURATION_S:-1800}"; seed="${SEED:-1}"
log="${MATRIX_LOG:-/Serverless/agent-sandbox-cxl/logs/rl-matrix.log}"
for condition in "$@"; do
    IFS=: read -r name per_node pool placement scale move <<< "$condition"
    echo "$(date -u +%FT%TZ) START $condition" >> "$log"
    out=$(numactl --cpunodebind=0 --membind=0 python3 "$here/sandbox.py" --candidates "$here/candidates-rl.json" pilot \
        --nodes 2-16/17-31 --sibling-offset 0 --harness-cpus 0-1 --per-node "$per_node" --pool "$pool" \
        --group-size 8 --placement "$placement" --llm-model tokens --gap-scale "$scale" --move "$move" \
        --ramp-s "$ramp" --duration-s "$duration" --seed "$seed-$name" 2>&1 | tail -3)
    run=$(echo "$out" | grep -o '/[^ ]*-pilot-[^ ]*' | tail -1)
    echo "$(date -u +%FT%TZ) DONE $condition -> $run" >> "$log"
    echo "$out" >> "$log"
    if [[ -n "$run" && -d "$run" ]]; then
        echo "$name $condition" > "$run/condition.txt"
        taskset -c 0-1 python3 "$here/sandbox.py" --candidates "$here/candidates-rl.json" episodes "$run" --solo "$solo" --quiet \
            >> "$log" 2>&1 || echo "episodes failed for $run" >> "$log"
    fi
    sudo -n python3 -c 'open("/proc/sys/vm/drop_caches","w").write("1")' 2>/dev/null || true
done
echo "$(date -u +%FT%TZ) MATRIX FINISHED" >> "$log"
