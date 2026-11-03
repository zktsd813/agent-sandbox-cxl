# Preparatory memory sizing smoke

This stopped one-sandbox smoke supports workload sizing. It is not part of the
formal baseline/reporting performance comparison or the full migration audit.

The 224MiB workload in [failed pilot 2](../failures/pilot2/failure-analysis.json)
reused only 12MiB of the old shared GPA set. The smoke selected a safe 400MiB
allocation from measured guest free/available memory and verified that 38,611
old GPAs (150.82421875MiB) were reused with new PFNs. The guest verified the
anonymous contents before freeing the allocation. Both formal arms subsequently
use the same fixed 400MiB workload.

- [Exact smoke report](memory-smoke-report.json)
- [Raw receipts, executed smoke script, wrapper and stopped outer logs](memory-smoke-evidence.tar.xz)
- [Stopped-state and receipt-hash verification](memory-smoke-packaging-proof.json)
- [Archive member and SHA-256 verification](memory-smoke-package.json)

The smoke saved its executed script and all 121 receipts. Unlike the full
suites, it did not archive or hash every imported controller module at run
time. The wrapper was supplied after completion and is labeled accordingly.
The archive retains this provenance limit rather than assigning the current
source hashes retroactively. Outer storage images are excluded; the recorded
QEMU processes were verified stopped before packaging.
