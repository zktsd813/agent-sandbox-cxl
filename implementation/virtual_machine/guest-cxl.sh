#!/bin/sh
# Configure one fixed, volatile CXL Type-3 device as device-DAX, without ndctl.
set -eu
role=$1
size=$2
nonce=$3
kvm_dax=${4:-0}
base=/sys/bus/cxl/devices
attempt=0
while :; do
    root= endpoint=
    for decoder in "$base"/decoder*.0; do
        [ ! -f "$decoder/create_ram_region" ] || root=$decoder
        [ ! -f "$decoder/dpa_size" ] || endpoint=$decoder
    done
    [ -z "$root" ] || [ -z "$endpoint" ] || break
    # In this DCD kernel a memdev can remain unbound after the initial
    # topology/probe ordering. Retry only unbound memdevs while discovering.
    for memdev in "$base"/mem*; do
        if [ -d "$memdev" ] && [ ! -L "$memdev/driver" ]; then
            basename "$memdev" > /sys/bus/cxl/drivers_probe 2>/dev/null || true
        fi
    done
    attempt=$((attempt + 1))
    [ "$attempt" -lt 200 ] || { dmesg; echo 'CXL decoder discovery timed out' >&2; exit 1; }
    sleep .1
done
region=$(cat "$root/create_ram_region")
device="dax${region#region}.0"
# CXL RAM defaults to kmem in this kernel. Prevent it from becoming shared
# System RAM, then explicitly select device_dax for the new character device.
echo 0 > /sys/bus/dax/drivers_autoprobe
printf '%s\n' "$region" > "$root/create_ram_region"
path="$base/$region"
echo 1 > "$path/interleave_ways"
echo 256 > "$path/interleave_granularity"
echo ram > "$endpoint/mode"
printf '%s\n' "$size" > "$endpoint/dpa_size"
printf '%s\n' "$size" > "$path/size"
basename "$endpoint" > "$path/target0"
echo 1 > "$path/commit"
[ -L "$path/driver" ] || printf '%s\n' "$region" > /sys/bus/cxl/drivers/cxl_region/bind
# KVM EPT pages use per-4KiB struct page metadata. Do not suballocate those
# pages from a device-DAX mapping with compound 2MiB vmemmap metadata.
if [ "$kvm_dax" -eq 1 ]; then
    echo 4096 > "/sys/bus/dax/devices/$device/align"
fi
printf '%s\n' "$device" > /sys/bus/dax/drivers/device_dax/new_id
test "$(basename "$(readlink -f "/sys/bus/dax/devices/$device/driver")")" = device_dax
echo 1 > /sys/bus/dax/drivers_autoprobe
attempt=0
while [ ! -c "/dev/$device" ]; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 200 ] || { dmesg; echo 'CXL device-DAX probe timed out' >&2; exit 1; }
    sleep .1
done
test "$(cat "/sys/bus/dax/devices/$device/size")" -eq "$size"
test "$(cat "$path/mode")" = ram
echo "/dev/$device" > /run/cxl-dax-device
printf 'CXL_DAX_READY role=%s region=%s device=/dev/%s size=%s align=%s resource=%s\n' \
    "$role" "$region" "$device" "$size" \
    "$(cat "/sys/bus/dax/devices/$device/align")" "$(cat "$path/resource")"
/lab/shared-probe dax-exchange "$role" 100 "$nonce" "/dev/$device"
