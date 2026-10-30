# Failed pilots, excluded from formal performance results

These stopped attempts are preserved as failure history. Their partial timings
must not be combined with the formal baseline/reporting comparison.

| Attempt | Failure | Evidence |
| --- | --- | --- |
| Pilot 1, baseline | After three completed samples, the profile setup parser failed to recognize `PROFILE_THREAD` lines ending in CRLF. | [analysis](pilot1/failure-analysis.json), [suite](pilot1/suite.json), [arm](pilot1/arm-report.json), [archive](pilot1-evidence.tar.xz) |
| Pilot 2, reporting | All 40,960 original pages (160MiB) were detached and ACKed, but the 224MiB anonymous allocation reused only 3,072 original pages (12MiB). The required 128MiB overlap check correctly rejected the run. | [analysis](pilot2/failure-analysis.json), [suite](pilot2/suite.json), [arm](pilot2/arm-report.json), [archive](pilot2-evidence.tar.xz) |
| Pilot 3, both arms at 400MiB | Reporting passed controller assertions but failed independent audit because a guest trace record was interrupted by host JSON. Baseline completed three samples, then its separate diagnostic found no matched `do_wp_page` calls and failed. | [analysis](pilot3/failure-analysis.json), [suite](pilot3/suite.json), [independent rejection](pilot3/independent-reporting-rejection.json), [archive](pilot3-evidence.tar.xz) |

Pilot 1's raw setup receipt contains vCPU TIDs 268 and 269 followed by carriage
returns. Its original anchored regex matches neither; exact `splitlines()`
parsing matches both. This was a harness parsing failure after the samples,
not evidence that the vCPUs were absent.

Pilot 2's raw PFN sets independently reproduce 160MiB detached and guest-ACKed,
followed by only 12MiB actually reused with changed PFNs. Allocation size alone
does not establish which old page frames the guest allocator selected. A later
[preparatory memory smoke](../preparatory/README.md) used 400MiB and demonstrated
150.82MiB reuse. That sizing evidence informed the fixed 400MiB workload used by
both formal arms; it is also excluded from formal comparison statistics.

Pilot 3 retains its original reporting `passed` and baseline `failed` statuses
unchanged. The independent reporting rejection is preserved separately. Both
arms used the same recorded harness, runtime and 400MiB workload, but neither
provides an accepted formal comparison. Logger separation must use an explicit
VMM `--log-path`, because the default logger and guest serial both write stdout.
The empty profile is a failed diagnostic, not evidence that CoW did not occur.

Each archive contains the exact suite report, arm report, every raw receipt,
suite and arm measured sources, and the stopped outer run's configuration,
report, QEMU logs and serial logs. Outer storage images are excluded. All
receipt and measured-source hashes were checked against the saved reports
before packaging. Recorded QEMU PID/start-time identities were confirmed
absent, network cleanup was recorded, and temporary memory backing was absent.
No live console was accessed.

[Pilot 1 package verification](pilot1-package.json),
[pilot 2 package verification](pilot2-package.json), and
[pilot 3 package verification](pilot3-package.json) record the archive SHA-256
and the exact member-manifest digest. Every archive member's name, type, size
and SHA-256 was checked against the staging copy. Archives and individual files
are below 100MiB. Compression ran on host CPUs 8–11, outside the lab's 0–7 set.
