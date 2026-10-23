#!/bin/sh
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
migration=0 migration_headroom=16
net_test= net_peer= net_port= net_rounds= net_interval_us= net_nonce= net_switch=0 net_gate=0
role=A dax_counter=0 heartbeat=0 heartbeat_sleep=0 heartbeat_quiet=0 heartbeat_churn=0 heartbeat_pit=0 heartbeat_spins=128 heartbeat_timing=0 heartbeat_dense=0
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        lab.role=*) role=${arg#*=} ;;
        lab.migration=*) migration=${arg#*=} ;;
        lab.migration_headroom=*) migration_headroom=${arg#*=} ;;
        lab.net_test=*) net_test=${arg#*=} ;;
        lab.net_peer=*) net_peer=${arg#*=} ;;
        lab.net_port=*) net_port=${arg#*=} ;;
        lab.net_rounds=*) net_rounds=${arg#*=} ;;
        lab.net_interval_us=*) net_interval_us=${arg#*=} ;;
        lab.net_nonce=*) net_nonce=${arg#*=} ;;
        lab.net_switch=*) net_switch=${arg#*=} ;;
        lab.net_gate=*) net_gate=${arg#*=} ;;
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
if [ -n "$net_test" ]; then
    network_test() {
        case "$net_test" in tcp|mptcp) ;; *) return 1 ;; esac
        [ "$net_peer" = 172.30.0.2 ] || return 1
        ifconfig lo up && ifconfig eth0 10.88.0.2 netmask 255.255.255.0 up || return 1
        route add default gw 10.88.0.1 || return 1
        export MPTCP_PROTOCOL="$net_test" MPTCP_LOCAL_IP=10.88.0.2
        if [ "$net_test" = mptcp ]; then
            [ "$(cat /proc/sys/net/mptcp/enabled)" = 1 ] || return 1
            /lab/shared-probe mptcp-pm "$role" limits 2 2 || return 1
        fi
        cat /proc/net/netstat > /tmp/netstat-before
        /lab/shared-probe mptcp-stream "$role" client "$net_peer" "$net_port" \
            "$net_rounds" "$net_interval_us" "$net_nonce" > /tmp/network-client.log 2>&1 &
        client_pid=$!
        attempt=0
        while ! grep -q '"event":"ready"' /tmp/network-client.log; do
            kill -0 "$client_pid" 2>/dev/null || { cat /tmp/network-client.log; return 1; }
            attempt=$((attempt + 1))
            [ "$attempt" -lt 200 ] || { kill "$client_pid"; return 1; }
            sleep .02
        done
        echo L2_NETWORK_CONNECTED
        if [ "$net_switch" = 1 ]; then
            if [ "$net_gate" = 1 ]; then
                # The reserved page is a lab control signal, not TCP packet data.
                attempt=0
                while [ "$(devmem 0x4000000 32)" != 0x00000001 ]; do
                    attempt=$((attempt + 1))
                    [ "$attempt" -lt 3000 ] || return 1
                    sleep .01
                done
                echo L2_NETWORK_HANDOFF_GATE=opened
            else
                sleep 1
            fi
            ip addr add 10.89.0.2/24 dev eth0 || return 1
            /lab/shared-probe mptcp-pm "$role" add 10.89.0.2 1 subflow || return 1
            echo L2_NETWORK_PATH_ADDED=10.89.0.2
            sleep 1
            /lab/shared-probe mptcp-pm "$role" delete 10.88.0.2 0 || return 1
            echo L2_NETWORK_PATH_REMOVED=10.88.0.2
        fi
        wait "$client_pid" || { cat /tmp/network-client.log; return 1; }
        cat /tmp/network-client.log
        echo L2_NETWORK_COUNTERS_BEFORE
        cat /tmp/netstat-before
        echo L2_NETWORK_COUNTERS_AFTER
        cat /proc/net/netstat
    }
    if network_test; then
        echo L2_NETWORK_RESULT=passed
    else
        echo L2_NETWORK_RESULT=failed
    fi
    while :; do sleep 3600; done
fi
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
