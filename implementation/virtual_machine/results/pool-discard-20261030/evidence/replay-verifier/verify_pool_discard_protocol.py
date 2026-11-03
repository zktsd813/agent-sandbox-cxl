#!/usr/bin/env python3
"""Independent replay of guest free-page-reporting submit/ACK evidence.

This module never imports the workload or VMM controller. It joins an exact,
loss-free guest trace prefix with host mapped/used-ring-ACK pairs. Host and guest
clocks are separate domains: their absolute timestamps are never compared.
"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

PAGE = 4096
DEFAULT_MEMORY = 512 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def intervals(ranges, memory_bytes=DEFAULT_MEMORY, max_ranges=256):
    """Validate full-page nonoverlapping ranges and return their interval union."""
    require(isinstance(ranges, list) and 0 < len(ranges) <= max_ranges, "range list length")
    values = []
    for row in ranges:
        require(set(row) == {"gpa", "bytes"}, "range fields")
        start, size = row["gpa"], row["bytes"]
        require(type(start) is int and type(size) is int and start >= 0 and size > 0 and
                start % PAGE == size % PAGE == 0 and start + size <= memory_bytes,
                "range alignment, length or guest ownership bounds")
        values.append((start, start + size))
    values.sort()
    require(all(a[1] <= b[0] for a, b in zip(values, values[1:])), "range overlap")
    union = []
    for start, end in values:
        if union and union[-1][1] == start:
            union[-1] = (union[-1][0], end)
        else:
            union.append((start, end))
    return union


def subset(inner, outer):
    return all(any(a <= start and end <= b for a, b in outer) for start, end in inner)


def trace_events(text):
    events = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if "pool_free_report:" not in line:
            continue
        match = re.search(r"(\d+\.\d+):\s+(?:pool_discard:)?pool_free_report:\s+(.*)$", line)
        require(match is not None, "malformed pool trace line")
        fields = dict(re.findall(r"([a-z_]+)=(-?\d+)", match[2]))
        require(set(fields) == {"seq", "phase", "index", "nents", "gpa", "bytes", "result"},
                "guest event fields changed")
        event = {key: int(value) for key, value in fields.items()}
        event.update(trace_line=line_number, timestamp_ns=int(Decimal(match[1]) * 10**9))
        events.append(event)
    require(events, "no guest reporting events")
    require(all(a["timestamp_ns"] <= b["timestamp_ns"] for a, b in zip(events, events[1:])),
            "guest monotonic trace reordered")
    return events


def guest_batches(text, memory_bytes=DEFAULT_MEMORY):
    output, active, last_seq = [], None, 0
    for event in trace_events(text):
        seq, phase, nents = event["seq"], event["phase"], event["nents"]
        require(1 <= nents <= 256 and phase in (0, 1, 2, 3), "guest phase/nents invalid")
        if phase == 0:
            require(active is None and seq == last_seq + 1 and
                    event["index"] == event["gpa"] == event["bytes"] == event["result"] == 0,
                    "guest submission gap, duplicate or overlapping batch")
            active = dict(seq=seq, nents=nents, ranges=[], submit_ns=event["timestamp_ns"],
                          submit_trace_line=event["trace_line"])
            last_seq = seq
            continue
        require(active is not None and active["seq"] == seq and active["nents"] == nents,
                "guest range/completion without matching submission")
        if phase == 1:
            require(event["index"] == len(active["ranges"]) < nents and event["result"] == 0,
                    "guest range index order or result")
            active["ranges"].append({key: event[key] for key in ("gpa", "bytes")})
        else:
            require(len(active["ranges"]) == nents and
                    event["index"] == event["gpa"] == event["bytes"] == 0,
                    "guest completed before all ranges were recorded")
            active["intervals"] = intervals(active["ranges"], memory_bytes)
            require((phase == 2 and event["result"] == 0) or
                    (phase == 3 and event["result"] != 0), "guest completion result contradicts phase")
            active.update(completed=(phase == 2), result=event["result"],
                          completion_ns=event["timestamp_ns"], completion_trace_line=event["trace_line"])
            output.append(active)
            active = None
    return output, active


def host_batches(text, vm_id, run_epoch, memory_bytes=DEFAULT_MEMORY):
    output, active, last_seq = [], None, 0
    for line_number, line in enumerate(text.splitlines(), 1):
        if "POOL_DISCARD_JSON " not in line:
            continue
        value = json.loads(line.split("POOL_DISCARD_JSON ", 1)[1])
        require(value["vm_id"] == str(vm_id) and value["run_epoch"] == str(run_epoch),
                "host trace VM incarnation mismatch")
        require(value["phase"] in ("mapped", "ack") and type(value["queue_head"]) is int and
                0 <= value["queue_head"] < 256, "host phase/queue head invalid")
        requested = intervals(value["requested"], memory_bytes)
        # Intersecting at most256 guest SG descriptors with sparse pooled GPAs
        # can produce many more than256 disjoint actual remap extents.
        discarded = intervals(value["discarded"], memory_bytes, memory_bytes // PAGE) if value["discarded"] else []
        require(subset(discarded, requested), "discarded bytes outside guest submitted ranges")
        for key in ("batch", "requested_pages", "discarded_pages", "skipped_busy_pages", "hints_removed", "copied_bytes", "host_work_ns"):
            require(type(value[key]) is int and value[key] >= 0, "host counter type/range")
        require(value["requested_pages"] == sum(b-a for a, b in requested)//PAGE and
                value["discarded_pages"] == sum(b-a for a, b in discarded)//PAGE and
                value["discarded_pages"] + value["skipped_busy_pages"] <= value["requested_pages"] and
                value["hints_removed"] <= value["requested_pages"] and value["copied_bytes"] == 0,
                "host range accounting or no-copy claim differs")
        if value["phase"] == "mapped":
            require(active is None and value["batch"] == last_seq + 1, "host batch gap/overlap")
            active, last_seq = dict(value, mapped_log_line=line_number), value["batch"]
        else:
            require(active is not None, "ACK without completed mapping")
            expected = {key: item for key, item in active.items() if key not in ("phase", "mapped_log_line")}
            actual = {key: item for key, item in value.items() if key != "phase"}
            require(expected == actual, "ACK differs from completed mapping batch")
            output.append(dict(active, ack_log_line=line_number, requested_intervals=requested,
                               discarded_intervals=discarded))
            active = None
    return output, active


def trace_stats(text):
    """Require a complete, nonoverwritten per-CPU ftrace buffer snapshot."""
    counters = {}
    for line in text.splitlines():
        match = re.match(r"\s*(entries|overrun|commit overrun|dropped events|read events):\s*(\d+)\s*$", line)
        if match:
            require(match[1] not in counters, "duplicate trace stats counter")
            counters[match[1]] = int(match[2])
    require({"entries", "overrun", "commit overrun", "dropped events", "read events"} <= counters.keys(),
            "missing per-CPU trace stats")
    require(all(counters[key] == 0 for key in ("overrun", "commit overrun", "dropped events", "read events")),
            "trace was overwritten, dropped or consumed")
    return counters


def correlate(guest_text, host_epochs, memory_bytes=DEFAULT_MEMORY):
    """Join a closed guest prefix with ordered complete host epochs.

A final in-flight guest batch or host tail after the guest trace capture is
reported explicitly and is not counted as completed. Interior missing records
are always rejected. Individual epochs must begin at their first host batch.
"""
    guest, guest_tail = guest_batches(guest_text, memory_bytes)
    require(all(row["completed"] for row in guest), "guest submission failure in healthy proof")
    host, host_tails = [], []
    for index, epoch in enumerate(host_epochs):
        rows, tail = host_batches(epoch["text"], epoch["vm_id"], epoch["run_epoch"], memory_bytes)
        require(tail is None or index == len(host_epochs)-1, "unfinished host batch before migration")
        host.extend(dict(row, epoch_index=index) for row in rows)
        if tail:
            host_tails.append(dict(tail, epoch_index=index))
    require(len(host) >= len(guest), "guest ACK lacks host mapped/ACK pair")
    joined = []
    for g, h in zip(guest, host):
        require(g["intervals"] == h["requested_intervals"], "guest/host request ranges or batch order differ")
        joined.append(dict(guest_seq=g["seq"], guest_submit_ns=g["submit_ns"],
                           guest_ack_ns=g["completion_ns"], guest_wait_ns=g["completion_ns"]-g["submit_ns"],
                           vm_id=h["vm_id"], run_epoch=h["run_epoch"], host_batch=h["batch"],
                           queue_head=h["queue_head"], requested=h["requested"], discarded=h["discarded"],
                           requested_pages=h["requested_pages"], discarded_pages=h["discarded_pages"],
                           skipped_busy_pages=h["skipped_busy_pages"], hints_removed=h["hints_removed"],
                           host_work_ns=h["host_work_ns"], copied_bytes=h["copied_bytes"]))
    require(joined, "no completed guest/host batch correlation")
    return dict(result="passed", completed_batches=len(joined), batches=joined,
                incomplete_guest_tail=guest_tail, complete_host_tail_batches=len(host)-len(guest),
                incomplete_host_tail_batches=len(host_tails),
                scope="Exact trace protocol correlation; not a claim that ACKed no-op ranges were detached, or that lazy anonymous pages were physically prefaulted.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("guest_trace", type=Path)
    p.add_argument("host_log", type=Path)
    p.add_argument("--vm-id", required=True)
    p.add_argument("--run-epoch", required=True)
    p.add_argument("--trace-stats", type=Path, nargs="+", required=True)
    p.add_argument("--memory-mib", type=int, default=512)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    stats = [trace_stats(path.read_text()) for path in a.trace_stats]
    result = correlate(a.guest_trace.read_text(), [dict(text=a.host_log.read_text(), vm_id=a.vm_id, run_epoch=a.run_epoch)], a.memory_mib*1024*1024)
    result["trace_stats"] = stats
    result["source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["inputs"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (a.guest_trace, a.host_log, *a.trace_stats)}
    data = json.dumps(result, indent=2)+"\n"
    if a.out:
        a.out.write_text(data)
    print(data, end="")


if __name__ == "__main__":
    main()
