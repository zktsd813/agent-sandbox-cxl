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
printf '%s 0\n' "$BOOT" > "$T/state"
i=0
while [ "$i" -lt 4096 ]; do printf 'FD-%s-%s\n' "$BOOT" "$i"; i=$((i + 1)); done > "$T/fd-sentinel"
exec 9<"$T/fd-sentinel"
rm "$T/fd-sentinel"
printf '{"event":"trace_ready","boot_id":"%s","pid":%s,"command_count":%s}\n' "$BOOT" "$$" "$COUNT"

RUNNING=0
# USER_HZ is 100 in this Linux guest. Keep raw per-CPU counters in the archive.
cpu_stat() { awk '/^cpu[0-9]+ / {print}' /proc/stat; }
# Only used by the opt-in in-flight handoff test. Normal run still waits here.
finish_command() {
    RC=0
    wait "$COMMAND_PID" || RC=$?
    kill -KILL "-$WATCHDOG_PID" "$WATCHDOG_PID" 2>/dev/null || true
    wait "$WATCHDOG_PID" 2>/dev/null || true
    # A command that leaves children running is not allowed to contaminate the next command.
    kill -KILL "-$COMMAND_PID" 2>/dev/null || true
    read END _ < /proc/uptime
    cat /sys/class/block/vda/stat > "$R/$N.block-after"
    cpu_stat > "$R/$N.cpu-after"
    OUT_SHA=$(sha256sum "$R/$N.out"); OUT_SHA=${OUT_SHA%% *}
    if [ "$N" != parallel-build ]; then NEXT=$((NEXT + 1)); fi
    printf '%s %s %s\n' "$BOOT" "$NEXT" "$OUT_SHA" > "$T/state"
    TIMED_OUT=false; [ ! -e "$R/$N.timeout" ] || TIMED_OUT=true
    END_EVENT=trace_end; [ "$N" != parallel-build ] || END_EVENT=trace_build_end
    printf '{"event":"%s","label":"%s","run_label":"%s","index":%s,"command_sha256":"%s","rc":%s,"start_s":%s,"end_s":%s,"output_sha256":"%s","next_index":%s,"timed_out":%s}\n' "$END_EVENT" "$TOKEN" "$RUN_TOKEN" "$INDEX" "$CMD_SHA" "$RC" "$START" "$END" "$OUT_SHA" "$NEXT" "$TIMED_OUT" > "$R/$N.json"
    cat "$R/$N.json"
    if [ "$NEXT" -eq "$COUNT" ]; then printf '{"completed_commands":%s}\n' "$COUNT" > "$R/complete.json"; fi
    RUNNING=0
}

command_live() {
    LIVE_STATE=missing
    LIVE_START=0
    if [ -r "/proc/$COMMAND_PID/stat" ]; then
        read -r PROC_STAT < "/proc/$COMMAND_PID/stat" || PROC_STAT=
        # Strip the parenthesized comm field; state and starttime are fields 3/22.
        PROC_TAIL=${PROC_STAT##*) }
        set -- $PROC_TAIL
        if [ "$#" -ge 20 ]; then LIVE_STATE=$1; shift 19; LIVE_START=$1; fi
    fi
    LIVE=false
    case "$LIVE_STATE" in missing|Z|X|x) ;; *)
        [ "$LIVE_START" = "$COMMAND_START" ] && LIVE=true ;;
    esac
    return 0
}

while read -r OP ARG TOKEN EXTRA; do
    case "$OP" in
        run|run_async) INDEX=$ARG ;;
        *) EXTRA=$TOKEN; TOKEN=$ARG ;;
    esac
    case "$TOKEN" in ''|*[!a-zA-Z0-9_-]*) echo '{"event":"trace_protocol_error"}'; continue ;; esac
    case "$OP" in
        heartbeat)
            VCPUS=$(awk '/^cpu[0-9]+ / {n++} END {print n}' /proc/stat)
            MODE=1; SLEEP=0
            if [ "$EXTRA" = idle ]; then MODE=0; VCPUS=1; SLEEP=2; fi
            LAB_HEARTBEAT_VCPUS=$VCPUS LAB_HEARTBEAT_SMP=$MODE LAB_HEARTBEAT_TIMING=1 taskset 1 /lab/shared-probe heartbeat A "$SLEEP" 1 > "$R/heartbeat.log" 2>&1 &
            printf '{"event":"trace_heartbeat","label":"%s","pid":%s}\n' "$TOKEN" "$!"
            ;;
        build_async)
            [ "$RUNNING" -eq 0 ] && [ "$NEXT" -eq "$COUNT" ] || exit 1
            N=parallel-build; INDEX=$COUNT; TIMEOUT=300
            cp "$T/parallel-build.sh" "$R/$N.command"
            CMD_SHA=$(sha256sum "$R/$N.command"); CMD_SHA=${CMD_SHA%% *}
            cat /sys/class/block/vda/stat > "$R/$N.block-before"
            cpu_stat > "$R/$N.cpu-before"
            read START _ < /proc/uptime
            setsid "$B" chroot /newroot /trace/parallel-build.sh </dev/null > "$R/$N.out" 2>&1 &
            COMMAND_PID=$!
            setsid "$B" sh -c 'sleep "$1"; echo timeout > "$2"; kill -TERM "-$3" 2>/dev/null || true' watchdog "$TIMEOUT" "$R/$N.timeout" "$COMMAND_PID" </dev/null >/dev/null 2>&1 &
            WATCHDOG_PID=$!; RUN_TOKEN=$TOKEN; RUNNING=1
            COMMAND_START=$(awk '{print $22}' "/proc/$COMMAND_PID/stat")
            printf '{"event":"trace_async_started","label":"%s","index":%s,"command_pid":%s,"start_ticks":%s}\n' "$TOKEN" "$INDEX" "$COMMAND_PID" "$COMMAND_START"
            ;;
        run|run_async)
            [ "$RUNNING" -eq 0 ] || exit 1
            case "$INDEX" in ''|*[!0-9]*) exit 1 ;; esac
            if [ "$INDEX" -ne "$NEXT" ] || [ "$INDEX" -ge "$COUNT" ] || [ -n "$EXTRA" ]; then
                printf '{"event":"trace_error","label":"%s","reason":"command_order","next_index":%s}\n' "$TOKEN" "$NEXT"
                continue
            fi
            N=$(printf '%03d' "$INDEX")
            CMD_SHA=$(sha256sum "$T/cmd/$N.command"); CMD_SHA=${CMD_SHA%% *}
            cp "$T/cmd/$N.command" "$R/$N.command"
            cat /sys/class/block/vda/stat > "$R/$N.block-before"
            cpu_stat > "$R/$N.cpu-before"
            read START _ < /proc/uptime
            printf '{"event":"trace_start","label":"%s","index":%s,"command_sha256":"%s","start_s":%s}\n' "$TOKEN" "$INDEX" "$CMD_SHA" "$START"
            setsid "$B" chroot /newroot "/trace/cmd/$N.sh" </dev/null > "$R/$N.out" 2>&1 &
            COMMAND_PID=$!
            setsid "$B" sh -c 'sleep "$1"; echo timeout > "$2"; kill -TERM "-$3" 2>/dev/null || true; sleep 5; kill -KILL "-$3" 2>/dev/null || true' watchdog "$TIMEOUT" "$R/$N.timeout" "$COMMAND_PID" </dev/null >/dev/null 2>&1 &
            WATCHDOG_PID=$!
            RUN_TOKEN=$TOKEN
            RUNNING=1
            if [ "$OP" = run_async ]; then
                COMMAND_START=$(awk '{print $22}' "/proc/$COMMAND_PID/stat")
                printf '{"event":"trace_async_started","label":"%s","index":%s,"command_pid":%s,"start_ticks":%s}\n' "$TOKEN" "$INDEX" "$COMMAND_PID" "$COMMAND_START"
            else
                finish_command
            fi
            ;;
        running)
            [ "$RUNNING" -eq 1 ] || exit 1
            command_live
            cpu_stat > "$R/$N.cpu-$TOKEN"
            printf '{"event":"trace_running","label":"%s","index":%s,"command_pid":%s,"start_ticks":%s,"process_state":"%s","active":%s}\n' "$TOKEN" "$INDEX" "$COMMAND_PID" "$LIVE_START" "$LIVE_STATE" "$LIVE"
            ;;
        collect)
            [ "$RUNNING" -eq 1 ] || exit 1
            finish_command
            ;;
        ping)
            # Only shell builtins: first execution ACK, no FD/hash/cache diagnostics.
            printf '{"event":"trace_ping","label":"%s","boot_id":"%s","pid":%s,"next_index":%s}\n' "$TOKEN" "$BOOT" "$$" "$NEXT"
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
