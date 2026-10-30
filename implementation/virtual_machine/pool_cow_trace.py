#!/usr/bin/env python3
"""Separate diagnostic trace of host write-protect fault handling in the L1.

Not enabled during benchmark samples. Function durations include nested work,
preemption and probe overhead, and must not be added across nested functions.
do_wp_page includes more than copying; a matched trace is not a page-copy count.
"""
import json
from pathlib import Path
import re
import secrets
import shlex
import statistics


FUNCTIONS = {
    "fault": "__handle_mm_fault",  # Also covers inlined do_cow_fault on absent file PTEs.
    "wp": "do_wp_page",  # wp_page_copy is inlined in this actual L1 build.
    "mmu": "kvm_mmu_notifier_invalidate_range_start",
    "tlb": "kvm_flush_remote_tlbs",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def profile_threads(raw):
    # The L1 serial console returns CRLF; splitlines also accepts LF receipts.
    return {int(match[1]) for line in raw.splitlines()
            if (match := re.fullmatch(r"PROFILE_THREAD ([1-9]\d*)", line))}


def analyze_trace(data, statistics_text, probe_profile, group, tids):
    require(re.fullmatch(r"pcow[0-9a-f]{8}", group), "unsafe trace group")
    for line in statistics_text.splitlines():
        match = re.match(r"\s*(overrun|commit overrun|dropped events):\s*(\d+)\s*$", line)
        if match:
            require(int(match[2]) == 0, "trace ring lost events: " + line)
    require("overrun:" in statistics_text and "dropped events:" in statistics_text,
            "missing per-CPU trace ring statistics")
    profile_rows = [line.split() for line in probe_profile.splitlines() if group + "_" in line]
    require(len(profile_rows) == len(FUNCTIONS) * 2, "missing kprobe hit/miss evidence")
    expected_names = {group + "_" + key + "_" + phase
                      for key in FUNCTIONS for phase in ("enter", "exit")}
    require({row[0] for row in profile_rows} == expected_names, "wrong or duplicate kprobe identities")
    for fields in profile_rows:
        require(len(fields) == 3 and int(fields[-1]) == 0, "missed kretprobe instances")
    hits = {row[0]: int(row[1]) for row in profile_rows}
    # Decode decimal timestamps without float rounding. trace emits seconds.
    pattern = re.compile(r"-(\d+)\s+(?:\(\s*\d+\)\s+)?\[\d+\]\s+\S+\s+"
                         r"(\d+)\.(\d+):\s+(?:" + group + r":)?" + group +
                         r"_(" + "|".join(FUNCTIONS) + r")_(enter|exit):")
    active, finished, pending = False, False, {}
    durations = {key: [] for key in FUNCTIONS}
    enters = {key: 0 for key in FUNCTIONS}
    resolution = 1
    for line in data.splitlines():
        if "tracing_mark_write:" in line and group + " BEGIN" in line:
            require(not active and not finished, "duplicate profile start")
            active = True
            continue
        if "tracing_mark_write:" in line and group + " END" in line:
            require(active, "profile end before start")
            active, finished = False, True
            continue
        if not active:
            continue
        match = pattern.search(line)
        if not match:
            # No event from this group may silently evade the parser.
            require(not re.search(r"(?:" + "|".join(FUNCTIONS) + r")_(?:enter|exit):", line),
                    "unparsed profile event: " + line)
            continue
        tid, seconds, fraction, key, phase = match.groups()
        tid = int(tid)
        require(tid in tids, "profile includes another process/thread")
        require(len(fraction) <= 9, "unsupported trace timestamp precision")
        resolution = max(resolution, 10 ** (9 - len(fraction)))
        stamp = int(seconds) * 1_000_000_000 + int((fraction + "000000000")[:9])
        stack = pending.setdefault((tid, key), [])
        if phase == "enter":
            stack.append(stamp)
            enters[key] += 1
        else:
            require(stack, "return without entry inside measured window")
            begin = stack.pop()
            require(stamp >= begin, "trace clock regressed")
            durations[key].append(stamp - begin)
    require(finished and not any(pending.values()), "unfinished profile or function calls")
    require(durations["fault"], "no host memory-fault calls observed")
    output = {}
    for key, values in durations.items():
        require(len(values) == enters[key], "entry/return count mismatch")
        require(all(hits[group + "_" + key + "_" + phase] >= len(values)
                    for phase in ("enter", "exit")), "trace exceeds probe hit counters")
        output[key] = {"symbol": FUNCTIONS[key], "calls": len(values),
                       "sum_ns": sum(values), "median_ns": statistics.median(values) if values else None,
                       "maximum_ns": max(values) if values else None}
    return {"result": "passed", "threads": sorted(tids), "functions": output,
            "printed_timestamp_resolution_ns": resolution,
            "scope": "Separate instrumented run. Inclusive wall durations contain nested work, preemption and probe overhead; do not add nested function sums or call them pure copy latency."}


def profile_recycle(controller, sandbox):
    """Caller must re-admit/warm before this and refill afterwards if needed."""
    c, s = controller, sandbox
    group = "pcow" + secrets.token_hex(4)
    base = "/sys/kernel/tracing"
    instance = base + "/instances/" + group
    prefix = "/run/" + group
    pidfile = shlex.quote(s.directory + "/fc.pid")
    definitions = []
    for key, symbol in FUNCTIONS.items():
        definitions.extend((f"p:{group}/{group}_{key}_enter {symbol}",
                            f"r128:{group}/{group}_{key}_exit {symbol}"))
    install = "\n".join("printf '%s\\n' " + shlex.quote(row) + f" >> {base}/kprobe_events" for row in definitions)
    script = f"""set -e
mkdir -p {base}
test -r {base}/trace || mount -t tracefs tracefs {base}
read p expected < {pidfile}
stat=$(cat /proc/$p/stat)
stat=${{stat##*) }}
set -- $stat
shift 19
test "$1" = "$expected"
filter=
for d in /proc/$p/task/*; do
    name=$(cat $d/comm)
    case "$name" in
      'fc_vcpu 0'|'fc_vcpu 1')
        tid=${{d##*/}}
        printf 'PROFILE_THREAD %s\\n' "$tid"
        if [ -n "$filter" ]; then filter="$filter || "; fi
        filter="${{filter}}common_pid == $tid"
        ;;
    esac
done
test -n "$filter"
test ! -d {instance}
mkdir {instance}
echo 0 > {instance}/tracing_on
echo mono > {instance}/trace_clock
echo 32768 > {instance}/buffer_size_kb
{install}
printf '%s\\n' "$filter" > {instance}/events/{group}/filter
echo 1 > {instance}/events/{group}/enable
echo 1 > {instance}/tracing_on
printf '%s\\n' '{group} BEGIN' > {instance}/trace_marker
"""
    installed = False
    try:
        raw, setup_file = c.shell(s.role, script, "cow-profile-setup", 120)
        installed = True
        tids = profile_threads(raw)
        require(tids, "no profiled vCPU identity")
        operation = c.command_vm(s, "recycle", 224)
        stop = f"""set -e
printf '%s\\n' '{group} END' > {instance}/trace_marker
echo 0 > {instance}/tracing_on
echo 0 > {instance}/events/{group}/enable
cat {instance}/trace > {prefix}.trace
for stats in {instance}/per_cpu/cpu*/stats; do
  printf '%s\\n' "$stats"
  cat "$stats"
done > {prefix}.stats
cat {base}/kprobe_profile > {prefix}.profile
"""
        _, stop_file = c.shell(s.role, stop, "cow-profile-stop", 120)
        values, files = {}, {}
        for kind in ("trace", "stats", "profile"):
            values[kind] = c.fetch_bytes(s.role, prefix + "." + kind, "cow-profile-" + kind).decode()
            files[kind] = c.receipts[-1]["file"]
        analysis = analyze_trace(values["trace"], values["stats"], values["profile"], group, tids)
        return {"setup_file": setup_file, "stop_file": stop_file, "files": files,
                "group": group, "operation": operation, "analysis": analysis}
    finally:
        # Only the uniquely owned instance/probe group is changed.
        # A failed setup may already have installed a subset of these probes.
        cleanup = f"""if [ -d {instance} ]; then
echo 0 > {instance}/tracing_on
test ! -f {instance}/events/{group}/enable || echo 0 > {instance}/events/{group}/enable
rmdir {instance}
fi
"""
        for key in FUNCTIONS:
            for phase in ("enter", "exit"):
                cleanup += f"if grep -q ':{group}/{group}_{key}_{phase} ' {base}/kprobe_events; then printf '%s\\n' '-:{group}/{group}_{key}_{phase}' >> {base}/kprobe_events; fi\n"
        if installed:
            c.shell(s.role, cleanup, "cow-profile-cleanup", 120)
        else:
            try:
                c.shell(s.role, cleanup, "cow-profile-failed-setup-cleanup", 120)
            except Exception:
                pass  # Original setup error remains authoritative; pair teardown follows.


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("stats", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("group")
    parser.add_argument("tids", type=int, nargs="+")
    args = parser.parse_args()
    print(json.dumps(analyze_trace(args.trace.read_text(), args.stats.read_text(),
                                  args.profile.read_text(), args.group, set(args.tids)), indent=2))
