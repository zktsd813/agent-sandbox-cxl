#!/usr/bin/env bash
# Synchronous replay v2 (docs/sync-replay-design.md): a step plan (rl_rollouts.py plan) is run in segments of
# SEGMENT_STEPS steps. Per segment: build its images (pinned digests, image-pins.json), replay its traces alone
# (solo, for the expected step time; skipped when SOLO is given), run every condition on the same steps with only
# the image files' page cache evicted in between, then, with DROP_IMAGES=1, delete the segment's images
# (images of candidates-rl.json are always kept). NUMA 0 only: work on cores 2-31, harness on 0-1; NUMA 1 untouched.
# Usage: run_plan_segments.sh PLAN CONDITION... where CONDITION = name:per_node:pool:placement:gap_scale:move
# Env: SEGMENT_STEPS (2), SEGMENTS (segment numbers, default all), SOLO, DROP_IMAGES (0), BUILD_JOBS (4),
#      DURATION_S (cap per run, 21600), IMAGE_CACHE_LOG (0), PILOT_ARGS, MATRIX_LOG,
#      LLM (tokens, or recorded for the validation run; solo uses the same so expected step times match),
#      NODES (2-16/17-31; with SMT on e.g. 2-16,66-80/17-31,81-95), SOLO_SIBLING (0; 64 with SMT on: VMM threads
#      of the solo run on the vCPU core's sibling).
set -uo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
plan="$(realpath "$1")"; shift
candidates="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["candidates"])' "$plan")"
nsteps="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["steps"]))' "$plan")"
per="${SEGMENT_STEPS:-2}"; duration="${DURATION_S:-21600}"; llm="${LLM:-tokens}"; nodes="${NODES:-2-16/17-31}"
log="${MATRIX_LOG:-/Serverless/agent-sandbox-cxl/logs/plan-segments.log}"
segments="${SEGMENTS:-$(seq -s ' ' 0 $(( (nsteps + per - 1) / per - 1 )))}"
sb=(numactl --cpunodebind=0 --membind=0 python3 "$here/sandbox.py" --candidates "$candidates")
for seg in $segments; do
    first=$(( seg * per )); last=$(( first + per - 1 )); (( last >= nsteps )) && last=$(( nsteps - 1 ))
    range="$first-$last"
    echo "$(date -u +%FT%TZ) SEGMENT $seg steps $range BUILD" >> "$log"
    taskset -c 2-31 "${sb[@]}" build --plan "$plan" --plan-steps "$range" --jobs "${BUILD_JOBS:-4}" >> "$log" 2>&1 \
        || { echo "$(date -u +%FT%TZ) SEGMENT $seg build failed; stopping" >> "$log"; exit 1; }
    solo="${SOLO:-}"
    if [[ -z "$solo" ]]; then
        out=$("${sb[@]}" solo --plan "$plan" --plan-steps "$range" --cores 2-31 --sibling-offset "${SOLO_SIBLING:-0}" --node 0 \
            --harness-cpus 0-1 --llm-model "$llm" --gap-scale 0 2>&1 | tail -3)
        echo "$out" >> "$log"
        solo=$(echo "$out" | grep -o '/[^ ]*-solo-[^ ]*' | tail -1)
        [[ -d "$solo" ]] || { echo "$(date -u +%FT%TZ) SEGMENT $seg solo failed; stopping" >> "$log"; exit 1; }
        find "$solo" -name job.ext4 -delete  # writable disks, about 15 GiB per segment; solo-summary.csv is kept
    fi
    for condition in "$@"; do
        IFS=: read -r name per_node pool placement scale move <<< "$condition"
        taskset -c 0 python3 "$here/image_blocks.py" evict >> "$log" 2>&1
        echo "$(date -u +%FT%TZ) START seg$seg $condition" >> "$log"
        sampler=""
        if [[ "${IMAGE_CACHE_LOG:-0}" == 1 ]]; then
            cache_log="$(mktemp -p /tmp image-cache.XXXX.csv)"
            taskset -c 0 nice -n 19 python3 "$here/image_blocks.py" resident --out "$cache_log" --interval 60 &
            sampler=$!
        fi
        out=$("${sb[@]}" pilot --nodes "$nodes" --sibling-offset 0 --harness-cpus 0-1 --per-node "$per_node" \
            --pool "$pool" --group-size 8 --placement "$placement" --llm-model "$llm" --gap-scale "$scale" \
            --move "$move" --ramp-s 0 --duration-s "$duration" --seed "plan-seg$seg" \
            --sync-plan "$plan" --plan-steps "$range" ${PILOT_ARGS:-} 2>&1 | tail -3)
        [[ -n "$sampler" ]] && kill "$sampler"
        run=$(echo "$out" | grep -o '/[^ ]*-pilot-[^ ]*' | tail -1)
        echo "$(date -u +%FT%TZ) DONE seg$seg $condition -> $run" >> "$log"
        echo "$out" >> "$log"
        if [[ -n "$run" && -d "$run" ]]; then
            echo "$name seg$seg $condition" > "$run/condition.txt"
            echo "$solo" > "$run/solo.txt"
            [[ -n "$sampler" ]] && mv "$cache_log" "$run/image-cache.csv"
            taskset -c 0-1 python3 "$here/sync_steps.py" "$run" --solo "$solo" > /dev/null 2>> "$log" \
                || echo "sync_steps failed for $run" >> "$log"
        fi
    done
    if [[ "${DROP_IMAGES:-0}" == 1 ]]; then
        "${sb[@]}" drop-images --plan "$plan" --plan-steps "$range" >> "$log" 2>&1
    fi
    echo "$(date -u +%FT%TZ) SEGMENT $seg FINISHED" >> "$log"
done
echo "$(date -u +%FT%TZ) PLAN SEGMENTS FINISHED" >> "$log"
