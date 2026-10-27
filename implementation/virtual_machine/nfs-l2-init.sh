#!/bin/sh
# The data filesystem stays ext4 across handoff; only its host backing changes.
set -eu
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /data
mount -t ext4 -o ro,noload,noatime /dev/vda /data
stty -F /dev/ttyS0 -echo
printf 'NFS_GUEST_BOOT boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
exec /lab/nfs-probe serve /data/payload.bin /sys/class/block/vda/stat </dev/ttyS0
