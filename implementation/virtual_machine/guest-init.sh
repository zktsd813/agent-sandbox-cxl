#!/bin/sh
# PID 1 in each independent L1 Linux VM. All tools and images are in the initramfs.
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run

role= nonce= rounds=1000 cxl_bytes=0 dax_guest=0 dax_kvm_state=0 dax_handoff=0 dax_4k=0 migration_ip=
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        lab.role=*) role=${arg#*=} ;;
        lab.nonce=*) nonce=${arg#*=} ;;
        lab.rounds=*) rounds=${arg#*=} ;;
        lab.cxl_bytes=*) cxl_bytes=${arg#*=} ;;
        lab.dax_guest=*) dax_guest=${arg#*=} ;;
        lab.dax_kvm_state=*) dax_kvm_state=${arg#*=} ;;
        lab.dax_handoff=*) dax_handoff=${arg#*=} ;;
        lab.dax_4k=*) dax_4k=${arg#*=} ;;
        lab.migration_ip=*) migration_ip=${arg#*=} ;;
    esac
done

shell() {
    while :; do
        setsid cttyhack sh
        sleep 1
    done
}
fail() {
    printf 'LAB_JSON {"event":"failure","role":"%s","message":"%s"}\n' "$role" "$1"
    shell
}
case "$role" in A|B) ;; *) fail "invalid role" ;; esac
hostname "nested-lab-$role"
printf 'LAB_BOOT role=%s kernel=%s\n' "$role" "$(uname -r)"
grep -m 1 '^flags' /proc/cpuinfo
if [ -n "$migration_ip" ]; then
    case "$migration_ip" in 172.30.0.1|172.30.0.2) ;; *) fail "invalid migration IP" ;; esac
    ifconfig lo up || fail "cannot bring loopback up"
    ifconfig eth0 "$migration_ip" netmask 255.255.255.252 up || fail "cannot configure migration network"
    printf 'LAB_JSON {"event":"migration_network","role":"%s","result":"passed","ip":"%s"}\n' "$role" "$migration_ip"
fi

while read -r module; do
    module_args=
    if [ "$dax_handoff" -eq 1 ]; then
        case "$module" in
            kvm.ko) module_args='nx_huge_pages=never enable_pmu=0' ;;
            kvm-intel.ko) module_args='pml=0 enable_apicv=0 flexpriority=0 preemption_timer=0' ;;
        esac
    fi
    insmod "/modules/$module" $module_args || fail "cannot load $module"
done < /lab/modules.list
if [ "$dax_handoff" -eq 1 ]; then
    /lab/shared-probe kvm-api "$role" || fail "L1 KVM API check failed"
else
    /lab/shared-probe kvm "$role" || fail "L1 KVM check failed"
fi
/lab/shared-probe exchange "$role" "$rounds" "$nonce" || fail "shared-memory exchange failed"
if [ "$cxl_bytes" -gt 0 ]; then
    dax_pages=$dax_kvm_state
    [ "$dax_4k" -eq 0 ] || dax_pages=1
    [ "$dax_handoff" -eq 0 ] || dax_pages=1
    /lab/guest-cxl.sh "$role" "$cxl_bytes" "$nonce" "$dax_pages" || fail "CXL device-DAX setup/exchange failed"
fi
if [ "$dax_handoff" -eq 1 ]; then
    printf 'LAB_JSON {"event":"direct_ready","role":"%s","result":"passed"}\n' "$role"
    echo "LAB_READY role=$role (direct shared KVM handoff)"
    shell
fi

extra_boot=
if [ "$dax_guest" -eq 1 ]; then
    if [ "$role" = B ]; then
        printf 'LAB_JSON {"event":"dax_observer","role":"B","result":"passed"}\n'
        echo 'LAB_READY role=B (DAX observer; no L2 running)'
        shell
    fi
    export FC_DAX_RAM="$(cat /run/cxl-dax-device)" FC_DAX_OFFSET=2097152
    if [ "$dax_kvm_state" -eq 1 ]; then
        dax_name=$(basename "$FC_DAX_RAM")
        resource=$(cat "/sys/bus/dax/devices/$dax_name/resource")
        # [256MiB,384MiB) is exclusively reserved for KVM execution state.
        pool_phys=$((resource + 268435456))
        printf '%x,%x\n' "$pool_phys" 134217728 > /sys/module/kvm/parameters/dax_pool || fail "KVM DAX pool configuration failed"
        cat /sys/module/kvm/parameters/dax_pool
        # [512MiB,576MiB) is exclusively reserved for the VMM heap.
        export FC_DAX_HEAP="$FC_DAX_RAM" FC_DAX_HEAP_OFFSET=536870912 FC_DAX_HEAP_BYTES=67108864
    fi
    # Reserve a guest physical page range for an externally observable counter.
    extra_boot='memmap=2M$64M lab.dax_counter=1'
fi

cat > /run/firecracker.json <<EOF
{
  "boot-source": {
    "kernel_image_path": "/lab/l2-vmlinux",
    "initrd_path": "/lab/l2-initramfs.cpio",
    "boot_args": "console=ttyS0 loglevel=4 reboot=k panic=1 pci=off rdinit=/init lab.role=$role $extra_boot"
  },
  "machine-config": {"vcpu_count": 1, "mem_size_mib": 128},
  "drives": []
}
EOF
/lab/firecracker --no-api --config-file /run/firecracker.json > /run/l2.log 2>&1 &
fc_pid=$!
echo "$fc_pid" > /run/firecracker.pid
attempt=0
# Linux's serial console emits CRLF; accept the trailing carriage return.
while ! grep -q '^L2_COUNTER=3[[:space:]]*$' /run/l2.log; do
    kill -0 "$fc_pid" 2>/dev/null || { cat /run/l2.log; fail "Firecracker exited"; }
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 600 ]; then
        cat /run/l2.log
        fail "nested microVM boot timed out"
    fi
    sleep .1
done
grep -q '"event":"l2_memory".*"result":"passed"' /run/l2.log || fail "L2 memory check missing"
printf 'L2_LOG_BEGIN role=%s\n' "$role"
cat /run/l2.log
printf 'L2_LOG_END role=%s\n' "$role"
printf 'LAB_JSON {"event":"nested_microvm","role":"%s","result":"passed","counter":3,"firecracker_pid":%s}\n' "$role" "$fc_pid"
printf 'LAB_READY role=%s (Firecracker continues running; log: /run/l2.log)\n' "$role"
shell
