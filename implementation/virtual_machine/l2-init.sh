#!/bin/sh
# Small boot smoke for lab asset preparation; SWE workers use swe-handoff-l2-init.sh.
export PATH=/bin:/lab
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
role=A
for arg in $(cat /proc/cmdline); do case "$arg" in lab.role=*) role=${arg#*=} ;; esac; done
printf 'L2_BOOTED role=%s kernel=%s\n' "$role" "$(uname -r)"
/lab/shared-probe memory "$role" || { echo L2_FAILED; halt -f; }
while :; do sleep 1; done
