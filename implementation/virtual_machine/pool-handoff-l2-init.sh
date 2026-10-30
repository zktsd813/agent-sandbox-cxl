#!/bin/sh
set -eu
trap 'echo POOL_GUEST_FATAL_init' EXIT
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /data /private /merged /sys/kernel/tracing
mount -t tracefs nodev /sys/kernel/tracing
mount -t ext4 -o ro,noload,noatime /dev/vda /data
mount -t tmpfs -o size=256m tmpfs /private
mkdir -p /private/overlay-upper /private/overlay-work
mount -t overlay overlay -o lowerdir=/data,upperdir=/private/overlay-upper,workdir=/private/overlay-work /merged
test -c /dev/guest-pool
vm=
image=
for arg in $(cat /proc/cmdline); do
    case "$arg" in pool.vm=*) vm=${arg#*=} ;; pool.image_id=*) image=${arg#*=} ;; esac
done
case "$vm" in ''|*[!0-9]*) exit 2 ;; esac
test "${#image}" -eq 64
stty -F /dev/ttyS0 -echo
printf 'POOL_GUEST_CMDLINE '
cat /proc/cmdline
printf 'POOL_GUEST_MOUNT '
awk '$2 == "/data" || $2 == "/private" || $2 == "/merged" {print}' /proc/mounts
/lab/pool-handoff-probe serve /data/payload.bin "$vm" /private/upper.bin "$image" </dev/ttyS0
umount /merged
umount /data
umount /private
echo POOL_GUEST_DONE
trap - EXIT
while :; do sleep 3600; done
