#!/bin/sh
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
migration=0 migration_headroom=16
role=A dax_counter=0 heartbeat=0 heartbeat_sleep=0 heartbeat_quiet=0 heartbeat_churn=0 heartbeat_pit=0 heartbeat_spins=128 heartbeat_timing=0 heartbeat_dense=0
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        lab.role=*) role=${arg#*=} ;;
        lab.migration=*) migration=${arg#*=} ;;
        lab.migration_headroom=*) migration_headroom=${arg#*=} ;;
        lab.dax_counter=*) dax_counter=${arg#*=} ;;
        lab.heartbeat=*) heartbeat=${arg#*=} ;;
        lab.heartbeat_dense=*) heartbeat_dense=${arg#*=} ;;
        lab.heartbeat_sleep=*) heartbeat_sleep=${arg#*=} ;;
        lab.heartbeat_quiet=*) heartbeat_quiet=${arg#*=} ;;
        lab.heartbeat_churn=*) heartbeat_churn=${arg#*=} ;;
        lab.heartbeat_pit=*) heartbeat_pit=${arg#*=} ;;
        lab.heartbeat_spins=*) heartbeat_spins=${arg#*=} ;;
        lab.heartbeat_timing=*) heartbeat_timing=${arg#*=} ;;
    esac
done
printf 'L2_BOOTED role=%s kernel=%s\n' "$role" "$(uname -r)"
if [ "$migration" -eq 1 ] || [ "$heartbeat_dense" -eq 1 ]; then
    # Boot-only diagnostics: explain RAM reserved outside the workload without
    # adding serial output to any migration timing interval.
    echo L2_MIGRATION_MEMORY_DIAGNOSTICS_BEGIN
    grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SReclaimable|SUnreclaim|Slab|PageTables|KernelStack|Percpu|HugePages_Total|Hugetlb):' /proc/meminfo || true
    dmesg | grep -i -E 'Memory:|swiotlb|software IO|reserved|memblock|percpu:|Freeing.*memory' || true
    echo L2_MIGRATION_MEMORY_DIAGNOSTICS_END
    export LAB_MIGRATION_HEADROOM_MIB="$migration_headroom"
    if [ "$migration" -eq 1 ]; then
        exec /lab/shared-probe migration-workload "$role"
    fi
fi
if [ "$heartbeat_dense" -eq 0 ] && ! /lab/shared-probe memory "$role"; then
    echo L2_FAILED
    halt -f
fi
if [ "$heartbeat" -eq 1 ]; then
    export LAB_HEARTBEAT_CHURN="$heartbeat_churn"
    export LAB_HEARTBEAT_PIT="$heartbeat_pit"
    export LAB_HEARTBEAT_SPINS="$heartbeat_spins"
    export LAB_HEARTBEAT_TIMING="$heartbeat_timing"
    export LAB_HEARTBEAT_DENSE="$heartbeat_dense"
    printf 'L2_CLOCKSOURCE=%s\n' "$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource)"
    exec /lab/shared-probe heartbeat "$role" "$heartbeat_sleep" "$heartbeat_quiet"
fi
counter=0
while :; do
    counter=$((counter + 1))
    if [ "$dax_counter" -eq 1 ]; then
        devmem 0x4000000 32 "$counter" || { echo L2_FAILED; halt -f; }
    fi
    printf 'L2_COUNTER=%s\n' "$counter"
    sleep 1
done
