#!/usr/bin/env python3
"""Summarize verified, unpacked result directories; does not run guest scans."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text())


def stats(values):
    return {"samples": values, "count": len(values), "p50": statistics.median(values),
            "min": min(values), "max": max(values)} if values else None


def trial(path):
    r = read(path)
    assert r["result"] == "passed"
    api, kernel, sizes, promotions, demotions, scans = [], [], [], [], [], []
    observed_scan_counters = []
    for t in r["transitions"]:
        placements = ([t["export"]["file"]] if t["kind"] == "promotion" else
                      [t["previous"]["file"]] if t["kind"] == "demotion" else
                      [t["manifest_file"], t["destination_placement"]["file"]])
        for name in placements:
            h = read(path.parent / name)["handle"]
            observed_scan_counters.append(h["full_scan_pages"])
            if not r["arguments"].get("full_audit", False):
                # The only full-table scan is initial admission, including
                # source-side exports before sealing and after re-promotion.
                assert h["full_scan_pages"] == h["count"], name
        if t["kind"] in ("promotion", "demotion"):
            values = promotions if t["kind"] == "promotion" else demotions
            values.append(sum(a["response"]["duration_us"] for a in t["operation"]["apis"]) / 1000)
            continue
        keys = ("create_api", "load_api") if t["kind"] == "state-only" else ("export_api", "attach_api")
        api.append(sum(t[k]["response"]["duration_us"] for k in keys) / 1000)
        sizes.append(t["manifest_bytes"])
        before = read(path.parent / t["manifest_file"])["handle"]
        after = read(path.parent / t["destination_placement"]["file"])["handle"]
        scans.append(after["full_scan_pages"] - before["full_scan_pages"])
        assert scans[-1] == 0 and t["ram_transfer_bytes"] == t["ram_snapshot_bytes"] == 0
        if t["kind"] == "direct":
            kernel.append((t["token"]["kernel_ns"] + t["import_event"]["token"]["kernel_ns"]) / 1e6)
    return {"mode": r["mode"], "memory_mib": r["arguments"]["memory_mib"],
            "transitions": dict(Counter(t["kind"] for t in r["transitions"])),
            "handoff_api_pair_ms": stats(api), "direct_kernel_pair_ms": stats(kernel),
            "manifest_bytes": stats(sizes), "handoff_full_scan_pages": scans,
            "observed_full_scan_counters": observed_scan_counters,
            "promotion_api_ms": stats(promotions), "demotion_api_ms": stats(demotions),
            "guest_command_seconds": r.get("guest_duration_s"),
            "controller_replay_seconds": r.get("controller_replay_ns", 0) / 1e9 or None,
            "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--synthetic", type=Path, required=True)
    p.add_argument("--swe", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    synthetic, swe = read(a.synthetic / "report.json"), read(a.swe / "report.json")
    assert synthetic["result"] == swe["result"] == "passed"
    out = {"schema": "pool-incremental-results-v1", "date": "2026-11-02",
           "scope": "Coherent nested KVM, one physical host; separate 512MiB stress and 2GiB Gin SWE trials",
           "timing": "API pairs exclude serial orchestration, process launch/fencing and guest ACK; kernel pairs are a subset of direct API time",
           "synthetic": {}, "swe": {}}
    for arm in synthetic["arms"]:
        out["synthetic"][arm["mode"]] = trial(a.synthetic / arm["report_file"])
        out["synthetic"][arm["mode"]]["verification"] = read(a.synthetic / (arm["mode"] + "-verification.json"))
    for arm in swe["trials"]:
        out["swe"][arm["id"]] = trial(a.swe / arm["id"] / "report.json")
    a.output.write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
