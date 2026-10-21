#!/usr/bin/env python3
"""Plot cache-maintenance stages from the published comparison JSON.

Requires Python 3 and matplotlib. Run from any working directory; output files
are written beside this script. Use --input to select another comparison JSON.
"""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=HERE.parents[1] / "cache-maintenance-comparison.json")
    parser.add_argument("--allow-partial", action="store_true", help="Internal preview only; metadata records incomplete evidence")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = json.loads(raw)
    if not args.allow_partial and any(data["scopes"][s]["status"] != "complete" for s in ("host", "guest")):
        parser.error("Both primary matrices must be complete; rerun analyze.py first")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import NullLocator
    from matplotlib.lines import Line2D

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.linewidth": .8, "pdf.fonttype": 42, "svg.fonttype": "none"})
    colors = {"host": "#4779C4", "guest": "#C57943"}
    names = {"host": "Native", "guest": "Nested DAX"}
    points, references = [], []
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.65), sharey=True)
    for ax, memory in zip(axes, (1024, 2048)):
        for scope in ("host", "guest"):
            cases = data["scopes"][scope]["cases"]
            selected = sorted((c for c in cases if c["parameters"]["memory_mib"] == memory
                               and c["parameters"]["write_bytes_per_dirty_page"] == 64
                               and c["parameters"]["mode"] == "dirty" and c["parameters"]["logging"] == "on"
                               and c["parameters"]["dirty_permyriad"] in (10, 100, 1000, 10000)),
                              key=lambda c: c["parameters"]["dirty_permyriad"])
            xs = [c["parameters"]["dirty_permyriad"] / 100 for c in selected]
            ys = [c["metrics_ms"]["cache_maintenance"]["p50"] for c in selected]
            ax.plot(xs, ys, color=colors[scope], marker="o", markersize=4,
                    linewidth=1.8, markeredgewidth=.6, markeredgecolor="white", zorder=3)
            for case, x, y in zip(selected, xs, ys):
                points.append({"scope": scope, "memory_mib": memory, "dirty_percent": x,
                               "p50_ms": y, "stats_ms": case["metrics_ms"]["cache_maintenance"],
                               "case": case["key"], "report": case["provenance"][0]})
            full = next((c for c in cases if c["parameters"]["memory_mib"] == memory
                         and c["parameters"]["dirty_permyriad"] == 100
                         and c["parameters"]["write_bytes_per_dirty_page"] == 64
                         and c["parameters"]["mode"] == "full" and c["parameters"]["logging"] == "on"), None)
            if full:
                value = full["metrics_ms"]["cache_maintenance"]["p50"]
                ax.axhline(value, color=colors[scope], linestyle=(0, (2.5, 2.5)), linewidth=1.4, zorder=2)
                references.append({"scope": scope, "memory_mib": memory, "dirty_percent_measured": 1,
                                   "p50_ms": value, "stats_ms": full["metrics_ms"]["cache_maintenance"],
                                   "case": full["key"], "report": full["provenance"][0]})
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(.075, 133)
        ax.set_ylim(.035, 310)
        ax.set_xticks((.1, 1, 10, 100), labels=("0.1", "1", "10", "100"))
        ax.set_yticks((.1, 1, 10, 100), labels=("0.1", "1", "10", "100"))
        ax.xaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_minor_locator(NullLocator())
        ax.grid(axis="y", color="#DCE2E8", linewidth=.6, alpha=.85)
        ax.set_title(f"{memory // 1024} GiB", fontsize=11, pad=8)
        ax.tick_params(axis="both", which="major", labelsize=9, length=3)
    axes[0].set_ylabel("Cache maintenance (ms)")
    fig.supxlabel("Dirty pages (%)", fontsize=10, y=.145)
    handles = [Line2D([0], [0], color=colors[s], marker="o", markersize=4, linewidth=1.8,
                      label=names[s] + " selective") for s in ("host", "guest")]
    handles += [Line2D([0], [0], color=colors[s], linestyle=(0, (2.5, 2.5)), linewidth=1.4,
                       label=names[s] + " full range") for s in ("host", "guest")]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(.5, -.008),
               ncol=4, frameon=False, fontsize=9, columnspacing=1.4, handlelength=2.2)
    fig.subplots_adjust(left=.095, right=.985, top=.895, bottom=.285, wspace=.16)
    stem = HERE / "cache-maintenance"
    fig.savefig(stem.with_suffix(".png"), dpi=220, bbox_inches="tight", pad_inches=.04)
    fig.savefig(stem.with_suffix(".pdf"), bbox_inches="tight", pad_inches=.04)
    plt.close(fig)
    caption = {
        "source": {"path": str(args.input.resolve()), "sha256": hashlib.sha256(raw).hexdigest()},
        "plot_script": {"path": str(Path(__file__).resolve()),
                        "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "matplotlib_version": matplotlib.__version__,
        "status": data["status"], "metric": "cache_maintenance", "statistic": "p50 of per-epoch writer_flush + reader_invalidate",
        "unit": "milliseconds", "panels_memory_mib": [1024, 2048], "write_bytes_per_dirty_page": 64,
        "memory_size_scope": "shared data memslot only; separate 4MiB private guest code/commands excluded",
        "caption": "Measured cache-maintenance component only: writer CLWB+SFENCE and reader CLFLUSHOPT+MFENCE. Curves select dirty 4KiB pages; dashed references process full RAM, measured at 1% dirty. Both use KVM dirty logging. Median of 20 balanced measured epochs after 4 warmups per case.",
        "excluded_from_plotted_latency": ["KVM guest execution", "PML harvest", "EPT dirty-bit rearm and translation invalidation",
                                          "bitmap scanning", "manifest hash", "TCP control/ACK", "post-ACK data verification",
                                          "Firecracker snapshot or direct detach/attach"],
        "limits": ["Native: native KVM and shared memfd RAM. Nested DAX: two nested-KVM L1s and emulated shared device-DAX on one coherent physical host.",
                   "Scope differences include kernel, nested virtualization, mapping and backing; curve differences are not an isolated measurement of CXL device latency.",
                   "These results do not reproduce noncoherent hardware stale caches. None mode correctness and PML tracking costs are reported separately in the source JSON.",
                   "Dashed horizontal values are a fixed 1%-dirty full-range reference, not a separate full-policy measurement at every plotted density.",
                   "Every selected 4KiB page issues maintenance for all 64 cachelines; address-range bytes are not actual writeback traffic.",
                   "0% dirty cases remain in the source data and are omitted from logarithmic X axes."],
        "points": points, "full_references": references,
    }
    (HERE / "cache-maintenance-data.json").write_text(json.dumps(caption, indent=2) + "\n")
    print(json.dumps({"png": str(stem.with_suffix('.png')), "pdf": str(stem.with_suffix('.pdf')),
                      "points": len(points), "full_references": len(references), "status": data["status"]}))


if __name__ == "__main__":
    main()
