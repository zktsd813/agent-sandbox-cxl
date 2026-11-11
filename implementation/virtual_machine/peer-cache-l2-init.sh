#!/bin/sh
set -eu
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /data
mount -t ext4 -o ro,noload,noatime /dev/vda /data
stty -F /dev/ttyS0 -echo
exec /lab/peer-cache-guest /data/payload.bin </dev/ttyS0
