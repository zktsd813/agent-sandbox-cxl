# Extract and independently replay the formal evidence

All three archives are required. They contain the exact successful suite and
arm reports, all 3,300 raw receipts, measured-source snapshots and the frozen
fixture manifest. Each arm also includes its stopped outer run configuration,
QEMU/serial logs and packaging proof. Outer storage images are excluded.

| Archive | Extracted prefix | Verification |
| --- | --- | --- |
| [Suite](formal-suite-evidence.tar.xz) | `formal-suite/` | [member/hash metadata](formal-suite-package.json) |
| [Reporting](reporting-evidence.tar.xz) | `reporting/` | [member/hash metadata](reporting-package.json) |
| [Baseline](baseline-evidence.tar.xz) | `baseline/` | [member/hash metadata](baseline-package.json) |

The suite report must be at the replay root, beside `measured-sources/`,
`reporting/` and `baseline/`. The following commands produce that layout and
run the same independent verifier source used for the accepted audit. Run from
the repository root; no KVM, lab VM, network, or disk image is needed.

```sh
evidence_dir="$PWD/implementation/virtual_machine/results/pool-discard-20261030/evidence"
replay_dir=$(mktemp -d /tmp/pool-discard-replay.XXXXXX)

taskset -c 8-11 python3 - "$evidence_dir" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for label in ('formal-suite', 'reporting', 'baseline'):
    meta = json.loads((root / (label + '-package.json')).read_text())
    archive = root / meta['file']
    assert archive.stat().st_size == meta['bytes'] < 100 * (1 << 20)
    assert hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() == meta['sha256']
for name, digest in json.loads((root / 'replay-verifier/source-hashes.json').read_text()).items():
    assert hashlib.sha256((root / 'replay-verifier' / name).read_bytes()).hexdigest() == digest
print('Archive and verifier hashes passed')
PY

taskset -c 8-11 tar -xJf "$evidence_dir/formal-suite-evidence.tar.xz" \
  --strip-components=1 -C "$replay_dir"
taskset -c 8-11 tar -xJf "$evidence_dir/reporting-evidence.tar.xz" -C "$replay_dir"
taskset -c 8-11 tar -xJf "$evidence_dir/baseline-evidence.tar.xz" -C "$replay_dir"

taskset -c 8-11 python3 "$evidence_dir/replay-verifier/verify_pool_discard.py" \
  "$replay_dir/report.json" --out "$replay_dir/independent-audit.json"
```

The replay verifies recorded source and raw receipt hashes, actual cache and
anonymous GPA/PFN sets, canonical equality, host remap/ACK and guest completion
ordering, full snapshot cutoffs, lifecycle/NFS evidence, and both matching
400MiB arms. It recomputes the timing summary from raw guest receipts. The
output includes the verifier source hashes and should match the committed
[formal suite audit](../verification/formal-suite-audit.json) as a JSON object.

Archive creation checked the exact member set, type, length and SHA-256 of every
regular file against frozen staging sources. A separate fresh extraction was
also checked file by file; its temporary path and result are recorded in
[fresh-extraction verification](fresh-extraction-verification.json). This
packaging check is distinct from the semantic verifier above.

[Reporting stop proof](reporting-packaging-proof.json),
[baseline stop proof](baseline-packaging-proof.json) and
[suite binding](suite-packaging-proof.json) record the original locations,
source identities and recorded process PID/start times. Matching QEMU
processes were absent, network cleanup was recorded, and temporary memory
backing was removed before copying. All packaging and fresh-extraction work
was pinned to CPUs 8–11. Archives and individual regular files are below
100MiB. Runtime/kernel/image binaries are identified by the frozen fixture
manifest; recreating the lab is a separate operation from this offline replay.
