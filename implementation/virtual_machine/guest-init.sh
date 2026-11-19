#!/bin/sh
# Lab-only L1 init: local KVM objects, shared CXL RAM/EPT, private socket network.
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run
role= nonce= rounds=1000 cxl_bytes=0 ip= pml=1
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        lab.role=*) role=${arg#*=} ;;
        lab.nonce=*) nonce=${arg#*=} ;;
        lab.rounds=*) rounds=${arg#*=} ;;
        lab.cxl_bytes=*) cxl_bytes=${arg#*=} ;;
        lab.ip=*) ip=${arg#*=} ;;
        lab.pml=*) pml=${arg#*=} ;;
    esac
done
shell() { while :; do setsid cttyhack sh; sleep 1; done; }
fail() {
    printf 'LAB_JSON {"event":"failure","role":"%s","message":"%s"}\n' "$role" "$1"
    shell
}
case "$role:$ip:$pml" in A:172.30.0.1:[01]|B:172.30.0.2:[01]) ;; *) fail 'invalid lab arguments' ;; esac
hostname "nested-lab-$role"
printf 'LAB_BOOT role=%s kernel=%s\n' "$role" "$(uname -r)"
grep -m 1 '^flags' /proc/cpuinfo
ifconfig lo up || fail 'loopback'
ifconfig eth0 "$ip" netmask 255.255.255.252 up || fail 'socket network'
printf 'LAB_JSON {"event":"lab_network","role":"%s","result":"passed","ip":"%s"}\n' "$role" "$ip"
while read -r module; do
    module_args=
    case "$module" in
        kvm.ko) module_args='nx_huge_pages=never enable_pmu=0' ;;
        kvm-intel.ko) module_args="pml=$pml enable_apicv=0 flexpriority=0 preemption_timer=0" ;;
    esac
    insmod "/modules/$module" $module_args || fail "cannot load $module"
done < /lab/modules.list
/lab/control-rpc /dev/vport0p1 "$nonce" > /run/control-rpc.log 2>&1 &
control_pid=$!;attempt=0
while ! grep -q '^LAB_CONTROL_READY$' /run/control-rpc.log; do
    kill -0 "$control_pid" || fail 'control channel exited'
    attempt=$((attempt+1));[ "$attempt" -lt 100 ] || fail 'control channel timeout'
    sleep .01
done
/lab/shared-probe kvm-api "$role" || fail 'L1 KVM API'
/lab/shared-probe exchange "$role" "$rounds" "$nonce" || fail 'shared mailbox'
/lab/guest-cxl.sh "$role" "$cxl_bytes" "$nonce" 1 || fail 'CXL setup'
printf 'LAB_JSON {"event":"ept_ready","role":"%s","result":"passed"}\n' "$role"
printf 'LAB_READY role=%s (prepared for EPT workers)\n' "$role"
shell
