#!/usr/bin/env python3
"""Plot completed host-NFS measurements; bars are medians, dots are observations."""
import argparse
import csv
import json
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report["result"] != "passed" or report.get("cleanup_errors"):
        raise SystemExit("Only a successfully completed report can be plotted")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    args.out.mkdir(parents=True, exist_ok=True)
    samples = [s for s in report["samples"] if s.get("measured")]
    with (args.out / "samples.csv").open("w", newline="") as stream:
        fields = ["cycle", "backend", "condition", "pattern", "bytes", "block_bytes", "seed",
                  "read_ns", "validation_ns", "wall_ns", "read_mib_s", "wall_mib_s",
                  "pread_p99_ns", "resident_before", "resident_after", "guest_read_bytes",
                  "nfs_read_calls", "nfs_read_received_bytes", "open_cookie", "checksum"]
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for sample in samples:
            row = {key: sample[key] for key in fields if key in sample}
            row.update(guest_read_bytes=sample["block_delta"]["read_sectors"]*512,
                       nfs_read_calls=sample["network_delta"]["nfs_read_calls"],
                       nfs_read_received_bytes=sample["network_delta"]["mount_read_received_bytes"])
            writer.writerow(row)
    colors = {"local": "#277DA1", "nfs": "#E67E22"}
    conditions = ["warm", "cold_guest", "cold_host", "cold_all"]
    labels = ["Guest warm", "Guest cold\nB host warm", "Guest + B cold\nA server warm", "All OS caches\ncold (NFS only)"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), constrained_layout=True)
    for ax, pattern, title in zip(axes, ("seq", "random"), ("Sequential: 64 KiB reads, 64 MiB range", "Random: 4 KiB reads, 8 MiB range")):
        for backend, shift in (("local", -.18), ("nfs", .18)):
            for index, condition in enumerate(conditions):
                values = [s["wall_mib_s"] for s in samples if s["backend"] == backend and s["pattern"] == pattern and s["condition"] == condition]
                if not values:
                    continue
                x = index + shift
                median = statistics.median(values)
                ax.bar(x, median, .32, color=colors[backend], alpha=.8, label=backend if index == 0 else None)
                ax.scatter([x+(i-(len(values)-1)/2)*.008 for i in range(len(values))], values,
                           s=9, color=colors[backend], edgecolors="black", linewidths=.2, zorder=3)
                ax.annotate(f"{median:,.1f}", (x, max(values)), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=8)
        ax.set_title(title, fontsize=11)
        ax.set_xticks(range(len(conditions)), labels, fontsize=8)
        ax.set_yscale("log")
        ax.set_ylabel("Read + content verification throughput (MiB/s)")
        ax.grid(axis="y", alpha=.2)
        ax.set_axisbelow(True)
        ax.margins(y=.25)
        ax.legend()
    fig.suptitle("Host-backed virtio-blk: local image vs NFS image\nNested KVM / emulated shared CXL; QD1; 12 observations per steady condition", fontsize=12)
    for suffix in ("png", "pdf"):
        fig.savefig(args.out / f"throughput.{suffix}", dpi=180)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(6.5, 4), constrained_layout=True)
    for backend, shift in (("local", -.16), ("nfs", .16)):
        for index, condition in enumerate(("retained", "retouch")):
            values = [s["wall_ns"]/1e6 for s in samples if s["backend"] == backend and s["condition"] == condition]
            x = index+shift
            ax.bar(x, statistics.median(values), .28, color=colors[backend], alpha=.8, label=backend if index == 0 else None)
            ax.scatter([x]*len(values), values, s=18, facecolors="none", edgecolors="black", zorder=3)
            ax.annotate(f"{statistics.median(values):.1f} ms", (x, max(values)), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=9)
    ax.set_xticks([0, 1], ["First read after restore", "Immediate second read"])
    ax.set_ylabel("64 MiB read + verification time (ms)")
    ax.set_title("Retained guest cache: zero block reads and zero NFS READs\n4 independent handoffs per backend")
    ax.margins(y=.25)
    ax.legend()
    for suffix in ("png", "pdf"):
        fig.savefig(args.out / f"first-access.{suffix}", dpi=180)
    print(args.out)


if __name__ == "__main__":
    main()
