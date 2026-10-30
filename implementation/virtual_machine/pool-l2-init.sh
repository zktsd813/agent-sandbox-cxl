#!/bin/sh
set -eu
trap 'echo POOL_GUEST_FATAL_init' EXIT
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /data /private
mount -t ext4 -o ro,noload,noatime /dev/vda /data
mount -t tmpfs -o size=32m tmpfs /private
vm=
for arg in $(cat /proc/cmdline); do
    case "$arg" in pool.vm=*) vm=${arg#*=} ;; esac
done
case "$vm" in ''|*[!0-9]*) exit 2 ;; esac
stty -F /dev/ttyS0 -echo
printf 'POOL_GUEST_MOUNT '
awk '$2 == "/data" || $2 == "/private" {print}' /proc/mounts
/lab/pool-guest-probe serve /data/payload.bin "$vm" /private/upper.bin </dev/ttyS0
umount /data
umount /private
echo POOL_GUEST_DONE
trap - EXIT
while :; do sleep 3600; done
