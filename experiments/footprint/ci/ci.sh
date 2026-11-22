#!/bin/bash
# Code-interpreter sandbox client, run once per replayed tool call (protocol: kernel.py).
#   ci.sh start  start the persistent kernel in its own session (the command's `timeout` cannot kill it) and wait
#   ci.sh run N  run cell R/N.py and print its output (first MiB). Exit 0 ok, 1 exception, 124 cell timeout,
#                125 no answer within CI_WAIT s or kernel gone: it is killed and restarted, state is lost
H=${CI_HOME:-/opt/ci} R=${CI_RUN:-/tmp/ci}

start() {
  mkdir -p "$R" && rm -f "$R/ready"
  setsid nohup python3 "$H/kernel.py" "$R" >> "$R/kernel.log" 2>&1 < /dev/null 3>&- 4>&- &
  for _ in $(seq 200); do
    [ -e "$R/ready" ] && return 0
    sleep 0.05
  done
  echo "[ci] kernel did not start"
  tail -20 "$R/kernel.log"
  return 1
}

run() {
  local n=$1 pid s waited=0
  pid=$(cat "$R/kernel.pid")
  exec 3<> "$R/resp" 4<> "$R/req"
  echo "$n" >&4
  # A line on resp only wakes us: a read cut by its 1 s timeout can split a line, so the .done file is the answer.
  while [ ! -e "$R/$n.done" ] && [ "$waited" -lt "${CI_WAIT:-45}" ]; do
    read -t 1 -u 3 _ || { waited=$((waited + 1)); kill -0 "$pid" 2> /dev/null || break; }
  done
  if [ ! -e "$R/$n.done" ]; then
    echo lost > "$R/$n.done"
    kill -9 "$pid" 2> /dev/null
    exec 3<&- 4<&-
    echo "[ci] no answer for cell $n; kernel restarted, state lost"
    start
    exit 125
  fi
  head -c 1048576 "$R/$n.out"
  read -r s _ < "$R/$n.done"
  case $s in ok) exit 0 ;; error) exit 1 ;; timeout) exit 124 ;; *) exit 2 ;; esac
}

case $1 in
  start) start ;;
  run) run "$2" ;;
  *) echo "usage: $0 start | run N" >&2; exit 2 ;;
esac
