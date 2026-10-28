#!/bin/sh
# The mutable fixture lives on ext4 /dev/vda, never in an OverlayFS/tmpfs upper.
set -eu
trap 'echo HOME_STORAGE_FATAL_init' EXIT
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /data
audit=
for arg in $(cat /proc/cmdline); do
    case "$arg" in home.audit=*) audit=${arg#*=} ;; esac
done
stty -F /dev/ttyS0 -echo
if [ -n "$audit" ]; then
    case "$audit" in *[!0-9]*) exit 2 ;; esac
    # The controller requires a successful clean quit/unmount before this boot.
    # This checks persisted bytes without replaying a journal or changing disk;
    # it is not a crash-recovery test.
    mount -t ext4 -o ro,noload,noatime /dev/vda /data
    printf 'HOME_STORAGE_MOUNT '
    awk '$2 == "/data" {print}' /proc/mounts
    /lab/home-storage-probe audit /data "$audit"
    umount /data
    echo HOME_STORAGE_UNMOUNTED
    echo HOME_STORAGE_AUDIT_DONE
else
    # Keep dirty pages present long enough to exercise migration. Explicit fsync
    # still commits the journal and file contents. This is a correctness fixture.
    mount -t ext4 -o rw,noatime,commit=600 /dev/vda /data
    echo 0 > /proc/sys/vm/dirty_writeback_centisecs
    printf 'HOME_STORAGE_MOUNT '
    awk '$2 == "/data" {print}' /proc/mounts
    /lab/home-storage-probe serve /data </dev/ttyS0
    umount /data
    echo HOME_STORAGE_UNMOUNTED
    echo HOME_STORAGE_GUEST_DONE
fi
trap - EXIT
while :; do sleep 3600; done
