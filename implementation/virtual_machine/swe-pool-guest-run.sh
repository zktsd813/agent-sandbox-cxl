#!/bin/sh
# A sequential command protocol. Raw workload output never enters the serial protocol.
set -eu
trap 'echo TRACE_FATAL_controller' EXIT
export PATH=/bin:/lab
B=/bin/busybox
T=/newroot/trace
R=$T/results
mkdir -p "$R"
read COUNT TIMEOUT < "$T/meta"
BOOT=$(cat /proc/sys/kernel/random/boot_id)
NEXT=0
FD_ORDINAL=0
POOL_ACTIVE=0
POOL_PID=
POOL_EPOCH=0
POOL_PLAN=0
POOL_IMAGE=$(cat /lab/pool-image-id)
printf '%s 0\n' "$BOOT" > "$T/state"
i=0
while [ "$i" -lt 64 ]; do printf 'FD-%s-%s\n' "$BOOT" "$i"; i=$((i + 1)); done > "$T/fd-sentinel"
exec 9<"$T/fd-sentinel"
rm "$T/fd-sentinel"
printf '{"event":"trace_ready","boot_id":"%s","pid":%s,"command_count":%s}\n' "$BOOT" "$$" "$COUNT"

while read -r OP ARG TOKEN EXTRA; do
    case "$OP" in
        run) INDEX=$ARG ;;
        pool_prepare) POOL_EPOCH=$TOKEN; POOL_PLAN=$EXTRA; TOKEN=$ARG ;;
        *) TOKEN=$ARG ;;
    esac
    case "$TOKEN" in ''|*[!a-zA-Z0-9_-]*) echo '{"event":"trace_protocol_error"}'; continue ;; esac
    case "$OP" in
        run)
            case "$INDEX" in ''|*[!0-9]*) exit 1 ;; esac
            if [ "$INDEX" -ne "$NEXT" ] || [ "$INDEX" -ge "$COUNT" ] || [ -n "$EXTRA" ]; then
                printf '{"event":"trace_error","label":"%s","reason":"command_order","next_index":%s}\n' "$TOKEN" "$NEXT"
                continue
            fi
            N=$(printf '%03d' "$INDEX")
            CMD_SHA=$(sha256sum "$T/cmd/$N.command"); CMD_SHA=${CMD_SHA%% *}
            cp "$T/cmd/$N.command" "$R/$N.command"
            cat /sys/class/block/vda/stat > "$R/$N.block-before"
            read START _ < /proc/uptime
            printf '{"event":"trace_start","label":"%s","index":%s,"command_sha256":"%s","start_s":%s}\n' "$TOKEN" "$INDEX" "$CMD_SHA" "$START"
            setsid "$B" chroot /newroot "/trace/cmd/$N.sh" </dev/null > "$R/$N.out" 2>&1 &
            COMMAND_PID=$!
            setsid "$B" sh -c 'sleep "$1"; echo timeout > "$2"; kill -TERM "-$3" 2>/dev/null || true; sleep 5; kill -KILL "-$3" 2>/dev/null || true' watchdog "$TIMEOUT" "$R/$N.timeout" "$COMMAND_PID" </dev/null >/dev/null 2>&1 &
            WATCHDOG_PID=$!
            RC=0
            wait "$COMMAND_PID" || RC=$?
            kill -KILL "-$WATCHDOG_PID" "$WATCHDOG_PID" 2>/dev/null || true
            wait "$WATCHDOG_PID" 2>/dev/null || true
            # A command that leaves children running is not allowed to contaminate the next command.
            kill -KILL "-$COMMAND_PID" 2>/dev/null || true
            read END _ < /proc/uptime
            cat /sys/class/block/vda/stat > "$R/$N.block-after"
            OUT_SHA=$(sha256sum "$R/$N.out"); OUT_SHA=${OUT_SHA%% *}
            NEXT=$((NEXT + 1))
            printf '%s %s %s\n' "$BOOT" "$NEXT" "$OUT_SHA" > "$T/state"
            TIMED_OUT=false; [ ! -e "$R/$N.timeout" ] || TIMED_OUT=true
            printf '{"event":"trace_end","label":"%s","index":%s,"command_sha256":"%s","rc":%s,"start_s":%s,"end_s":%s,"output_sha256":"%s","next_index":%s,"timed_out":%s}\n' "$TOKEN" "$INDEX" "$CMD_SHA" "$RC" "$START" "$END" "$OUT_SHA" "$NEXT" "$TIMED_OUT" > "$R/$N.json"
            cat "$R/$N.json"
            if [ "$NEXT" -eq "$COUNT" ]; then printf '{"completed_commands":%s}\n' "$COUNT" > "$R/complete.json"; fi
            ;;
        identity)
            READS_BEFORE=$(awk '{print $1}' /sys/class/block/vda/stat)
            read -r FD_LINE <&9
            FD_POSITION=$(awk '$1=="pos:" {print $2}' /proc/$$/fdinfo/9)
            FD_INODE=$(stat -Lc '%i' /proc/$$/fd/9)
            case "$(readlink /proc/$$/fd/9)" in *' (deleted)') FD_DELETED=true ;; *) FD_DELETED=false ;; esac
            STATE_SHA=$(sha256sum "$T/state"); STATE_SHA=${STATE_SHA%% *}
            AVAILABLE=$(awk '$1=="MemAvailable:" {print $2}' /proc/meminfo)
            CACHED=$(awk '$1=="Cached:" {print $2}' /proc/meminfo)
            READS_AFTER=$(awk '{print $1}' /sys/class/block/vda/stat)
            printf '{"event":"trace_identity","label":"%s","boot_id":"%s","pid":%s,"next_index":%s,"sentinel_sha256":"%s","fd_line":"%s","fd_ordinal":%s,"fd_position":%s,"fd_inode":%s,"fd_deleted":%s,"mem_available_kib":%s,"cached_kib":%s,"block_read_ios":%s}\n' "$TOKEN" "$BOOT" "$$" "$NEXT" "$STATE_SHA" "$FD_LINE" "$FD_ORDINAL" "$FD_POSITION" "$FD_INODE" "$FD_DELETED" "$AVAILABLE" "$CACHED" "$((READS_AFTER - READS_BEFORE))"
            FD_ORDINAL=$((FD_ORDINAL + 1))
            ;;
        pool_prepare)
            [ "$POOL_ACTIVE" -eq 0 ]
            case "$POOL_EPOCH:$POOL_PLAN" in *[!0-9:]*|:*) exit 1 ;; esac
            [ -n "$POOL_EPOCH" ] && [ -n "$POOL_PLAN" ] && [ "$POOL_PLAN" != 0 ]
            mkdir -p /ram/pool
            rm -f /ram/pool/control /ram/pool/registration.jsonl /ram/pool/helper.stderr
            mkfifo /ram/pool/control
            set --
            while IFS= read -r FILE; do
                case "$FILE" in /lower/*) set -- "$@" "$FILE" ;; *) exit 1 ;; esac
            done < /lab/pool-files
            [ "$#" -gt 0 ] && [ "$#" -le 16 ]
            READS_BEFORE=$(awk '{print $1}' /sys/class/block/vda/stat)
            # Parent retains the FIFO writer. The child owns the authority FD,
            # locks only resident lower folios, and blocks until explicit commit.
            exec 8<>/ram/pool/control
            /lab/guest-pool hold "$POOL_IMAGE" "$POOL_EPOCH" "$POOL_PLAN" "$@" \
                </ram/pool/control 8>&- >/ram/pool/registration.jsonl 2>/ram/pool/helper.stderr &
            POOL_PID=$!
            WAIT=0
            until grep -q '"kind":"guest_pool_status","state":2,' /ram/pool/registration.jsonl; do
                kill -0 "$POOL_PID"
                WAIT=$((WAIT + 1)); [ "$WAIT" -lt 6000 ]; sleep .02
            done
            POOL_ACTIVE=1
            READS_AFTER=$(awk '{print $1}' /sys/class/block/vda/stat)
            SIZE=$(stat -c '%s' /ram/pool/registration.jsonl)
            SHA=$(sha256sum /ram/pool/registration.jsonl); SHA=${SHA%% *}
            SELECTED=$(sed -n 's/.*"selected_pages":\([0-9][0-9]*\).*/\1/p' /ram/pool/registration.jsonl)
            printf '{"event":"swe_pool_prepared","label":"%s","boot_id":"%s","pid":%s,"helper_pid":%s,"epoch":%s,"plan_generation":%s,"selected_pages":%s,"bytes":%s,"sha256":"%s","block_read_ios":%s}\n' \
                "$TOKEN" "$BOOT" "$$" "$POOL_PID" "$POOL_EPOCH" "$POOL_PLAN" "$SELECTED" "$SIZE" "$SHA" "$((READS_AFTER - READS_BEFORE))"
            ;;
        pool_fetch)
            [ "$POOL_ACTIVE" -eq 1 ]; kill -0 "$POOL_PID"
            SIZE=$(stat -c '%s' /ram/pool/registration.jsonl)
            SHA=$(sha256sum /ram/pool/registration.jsonl); SHA=${SHA%% *}
            printf 'SWE_POOL_EXPORT_BEGIN %s %s %s\n' "$TOKEN" "$SIZE" "$SHA"
            uuencode -m /ram/pool/registration.jsonl registration.jsonl | sed '1d;$d'
            printf 'SWE_POOL_EXPORT_END %s\n' "$TOKEN"
            printf '{"event":"swe_pool_export","label":"%s","bytes":%s,"sha256":"%s"}\n' "$TOKEN" "$SIZE" "$SHA"
            ;;
        pool_release)
            [ "$POOL_ACTIVE" -eq 1 ]; kill -0 "$POOL_PID"
            printf 'commit\n' >&8
            wait "$POOL_PID"
            exec 8>&-
            grep -q '"kind":"guest_pool_status","state":3,' /ram/pool/registration.jsonl
            POOL_ACTIVE=0
            printf '{"event":"swe_pool_released","label":"%s","epoch":%s,"plan_generation":%s,"inactive":true}\n' "$TOKEN" "$POOL_EPOCH" "$POOL_PLAN"
            ;;
        evict)
            sync
            BEFORE=$(awk '$1=="Cached:" {print $2}' /proc/meminfo)
            echo 3 > /proc/sys/vm/drop_caches
            AFTER=$(awk '$1=="Cached:" {print $2}' /proc/meminfo)
            printf '{"event":"trace_evicted","label":"%s","next_index":%s,"drop_caches":3,"cached_before_kib":%s,"cached_after_kib":%s}\n' "$TOKEN" "$NEXT" "$BEFORE" "$AFTER"
            ;;
        export)
            cat /proc/mounts > "$R/guest-mounts.txt"
            cat /proc/meminfo > "$R/meminfo.txt"
            cat /sys/class/block/vda/stat > "$R/block-final"
            dmesg > "$R/dmesg.txt"
            cp "$T/state" "$R/state"
            tar -C "$T" -czf /ram/results.tar.gz results
            SIZE=$(stat -c '%s' /ram/results.tar.gz)
            SHA=$(sha256sum /ram/results.tar.gz); SHA=${SHA%% *}
            printf 'TRACE_EXPORT_BEGIN %s %s %s\n' "$TOKEN" "$SIZE" "$SHA"
            # Ubuntu's static BusyBox has uuencode -m but no base64 applet.
            uuencode -m /ram/results.tar.gz results.tar.gz | sed '1d;$d'
            printf 'TRACE_EXPORT_END %s\n' "$TOKEN"
            printf '{"event":"trace_export","label":"%s","bytes":%s,"sha256":"%s"}\n' "$TOKEN" "$SIZE" "$SHA"
            ;;
        *) printf '{"event":"trace_error","label":"%s","reason":"unknown_operation"}\n' "$TOKEN" ;;
    esac
done
