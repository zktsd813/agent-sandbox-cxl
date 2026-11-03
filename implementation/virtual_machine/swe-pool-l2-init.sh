#!/bin/sh
# Keep the controller in initramfs; only recorded commands enter the overlay root.
set -eu
trap 'echo TRACE_FATAL_init' EXIT
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /lower /ram /newroot
mount -t ext4 -o ro,noload,noatime /dev/vda /lower
mount -t tmpfs -o mode=0755 tmpfs /ram
mkdir -p /ram/upper /ram/work
mount -t overlay overlay -o lowerdir=/lower,upperdir=/ram/upper,workdir=/ram/work /newroot
mkdir -p /newroot/trace /newroot/proc /newroot/sys /newroot/dev
cp -a /lab/trace/. /newroot/trace/
mount -t proc proc /newroot/proc
mount --bind /sys /newroot/sys
mount --bind /dev /newroot/dev
mkdir -p /newroot/dev/pts /newroot/dev/shm /newroot/run /newroot/tmp
mount -t devpts devpts /newroot/dev/pts
mount -t tmpfs -o mode=1777,size=64m tmpfs /newroot/dev/shm
chmod 1777 /newroot/tmp
ip link set lo up
hostname swe-microvm
printf '127.0.0.1 localhost swe-microvm\n::1 localhost ip6-localhost ip6-loopback\n' > /newroot/etc/hosts
stty -F /dev/ttyS0 -echo
exec /bin/busybox sh /lab/swe-pool-guest-run.sh </dev/ttyS0 >/dev/ttyS0 2>&1
