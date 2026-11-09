#!/usr/bin/env bash
# 10 s scheduler trace of node0 CPUs during the blocking-sync run (g1-C1s-blk), 6 min after it starts.
# Gives on-CPU slice and sleep-gap lengths of each trainer worker's threads.
set -u
# usage: sched_watch.sh OUT_DIR
S=$1
R=$(cd -- "$(dirname -- "$0")/.." && pwd)/results/g1-C1s-blk-20261103
until [[ -f $R/env.json ]]; do sleep 20; done
sleep 360
pgrep -u "$(id -u)" -f "ray::WorkerDict" > $S/sched-blk.workers
sudo -n perf record -q -k monotonic -e sched:sched_switch -e sched:sched_wakeup -a -C 0-15,32-47 \
    -o $S/sched-blk.data -- sleep 10
sudo -n perf script -i $S/sched-blk.data -F cpu,tid,time,event,trace > $S/sched-blk.txt 2>/dev/null
sudo -n chown "$(id -u)" $S/sched-blk.data
echo "done $(date)"
