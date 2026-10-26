#!/microvm/busybox sh
# Sandbox replay agent, run by the root disk's /microvm/init stub after it mounts the job disk
# (/dev/vdb) at /job. The job disk holds the commands to replay (cmd/NNN.sh, cmd/NNN.gap) and
# receives the results (out/). An optional job file `net` ("ADDR/PREFIX GATEWAY DNS[,DNS]")
# configures eth0 for NAT egress.
set -u
B=/microvm/busybox
J=/job
$B mount -t proc proc /proc
$B mount -t sysfs sysfs /sys
$B mkdir -p /dev/pts /dev/shm /run /tmp
$B mount -t devpts devpts /dev/pts
$B mount -t tmpfs -o mode=1777,size=64m tmpfs /dev/shm
$B chmod 1777 /tmp
$B ip link set lo up
$B hostname swe-microvm
$B printf '127.0.0.1 localhost swe-microvm\n::1 localhost ip6-localhost ip6-loopback\n' > /etc/hosts
$B mkdir -p $J/out
if [ -f $J/net ]; then
  read ADDR GW DNS < $J/net
  $B ip link set eth0 up && $B ip addr add "$ADDR" dev eth0 && $B ip route add default via "$GW" \
    || { echo SWE_NET_FAILED; $B poweroff -f; }
  $B rm -f /etc/resolv.conf
  for d in $(echo "$DNS" | $B tr ',' ' '); do echo "nameserver $d"; done > /etc/resolv.conf
fi
read COUNT TIMEOUT SCALE < $J/meta
$B uname -a > $J/out/uname.txt
$B cat /proc/meminfo > $J/out/meminfo-start.txt
cd "$($B cat $J/cwd)" || { echo SWE_CWD_FAILED; $B poweroff -f; }
echo "SWE_READY $($B cut -d' ' -f1 /proc/uptime)"
i=0
while [ "$i" -lt "$COUNT" ]; do
  n=$($B printf '%03d' "$i")
  gap=$($B cat $J/cmd/$n.gap)
  wait=$($B awk -v g="$gap" -v s="$SCALE" 'BEGIN { printf "%.3f", g * s }')
  [ "$wait" = "0.000" ] || $B sleep "$wait"
  $B head -1 /proc/stat > $J/out/$n.stat0
  t0=$($B cut -d' ' -f1 /proc/uptime)
  echo "SWE_CMD_START $i $t0"
  $B timeout -s KILL "$TIMEOUT" $J/run $J/cmd/$n.sh < /dev/null > $J/out/$n.out 2>&1
  rc=$?
  t1=$($B cut -d' ' -f1 /proc/uptime)
  $B head -1 /proc/stat > $J/out/$n.stat1
  echo "$rc $t0 $t1" > $J/out/$n.rc
  echo "SWE_CMD_END $i $rc $t1"
  i=$((i + 1))
done
$B cat /proc/meminfo > $J/out/meminfo-end.txt
$B dmesg > $J/out/dmesg.txt
echo done > $J/out/complete
$B sync
$B mount -o remount,ro $J || true
echo SWE_COMPLETE
$B poweroff -f
while true; do $B sleep 1; done
