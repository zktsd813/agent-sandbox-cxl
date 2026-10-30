# Instrumented CoW mechanism diagnostic

This stopped diagnostic demonstrates the CoW path separately from the formal
baseline/reporting timing comparison. It does not contribute formal samples.

After canonical admission and a guest read, the diagnostic evicted the 160MiB
file cache and filled, verified and unmapped a 224MiB anonymous allocation.
All 40,960 original cache GPAs had changed host PFNs afterwards. The separate
kernel trace recorded 40,960 calls each to the fault, write-protect, KVM MMU
invalidation and TLB-flush functions, with no lost events or missed returns.

| Measurement | Observed duration |
| --- | ---: |
| Whole guest recycle operation | 5.123356s |
| Sum of `__handle_mm_fault` calls, inclusive | 0.967074s |
| Sum of `do_wp_page` calls, inclusive | 0.922784s |
| Sum of KVM MMU invalidation calls, inclusive | 0.078379s |
| Sum of KVM TLB flush calls, inclusive | 0.031068s |

These function durations overlap and must not be added. They include nested
work, scheduling delays and probe overhead. They are not pure page-copy times.
The whole operation includes eviction, allocation, all data stores, full data
verification and unmapping. This diagnostic does not attribute the remainder
to any individual operation. The printed trace resolution is 1µs, so a recorded
zero-duration call does not establish zero execution cost.

- [Exact diagnostic report](report.json)
- [Raw evidence archive](cow-diagnostic-evidence.tar.xz)
- [Archive verification](cow-diagnostic-package.json)
- [Stopped-state, receipt and source provenance](packaging-proof.json)
- [Independent replay of 327,682 trace entries, all four CPU buffers and PFN splits](../verification/cow-diagnostic-independent-audit.json)

The archive includes all 118 receipts, the full trace and per-CPU/probe stats,
PFN inputs/outputs, the four measured source files, stopped outer run logs and
configuration, and the exact Firecracker executable, patch and verified build
provenance. Firecracker SHA-256 is
`a4d6f75f0cad5885009097221e464952a1706e3d1792826c14a0dfa84c8a6023`.
Outer storage images are excluded. Every archive member is checked against its
source copy by name, type, size and SHA-256; archive and individual files are
below 100MiB. Staging, hashing, compression and verification ran on CPUs 8–11,
outside the live formal experiment's CPU set.

This short diagnostic copied its own script and three named helpers at run
time, but did not archive every imported controller module. That limitation is
recorded in its provenance. The full formal experiment uses its own exhaustive
measured-source manifest.
