# Independent evidence audits

These scripts independently derive counts and inspect archived raw receipts. They do not import `verify_pool_results.py`. The main verifier remains the complete acceptance check; these scripts supply a second implementation of key receipt, identity, migration, PFN, and current-cache correspondence checks.

Pass a completed run's original `report.json` and an output filename. Referenced receipt and `measured-sources` files must remain beside the report. `audit_receipts.py` also checks the Firecracker and kernel binaries at the assets path recorded in the report.

```sh
python3 audit_receipts.py /absolute/path/to/sync/report.json /tmp/sync-audit.json

python3 audit_current_cache.py /absolute/path/to/async/report.json /tmp/current-cache-audit.json
python3 audit_receipts.py /absolute/path/to/async/report.json /tmp/async-audit.json \
  --current-cache-audit /tmp/current-cache-audit.json
```

The current-cache audit expands the guest's lossless file-offset/GPA ranges, reconstructs the exact PFN scanner input from the raw VMM admission, and checks that all three VMs remain paused during every group scan. The older Sync run lacks these records; its guest cache residency and candidate-frame sharing remain separate observations.

These tools assume this fixture: three 512-MiB sandboxes, a 160-MiB payload, and the recorded promotion/handoff/demotion schedule. Their full-suite command and stage counts deliberately reject incomplete runs.

## Extract and replay the archived evidence

The `sync-evidence.tar.xz` and `async-evidence.tar.xz` archives contain each complete report directory: `report.json`, every raw receipt, and `measured-sources/`. The adjacent `sync-package.json` and `async-package.json` record the compressed size and SHA256, report SHA256, member counts, and verification of every archived file against its source. Archive roots are `sync/` and `async/`.

From this `audit-tools` directory, with Python 3 and a tar implementation supporting xz:

```sh
mkdir -p /tmp/pool-evidence
tar -xJf ../sync-evidence.tar.xz -C /tmp/pool-evidence
tar -xJf ../async-evidence.tar.xz -C /tmp/pool-evidence

python3 ../../../verify_pool_results.py /tmp/pool-evidence/sync/report.json \
  --negative-tests --out /tmp/sync-official-audit.json
python3 ../../../verify_pool_results.py /tmp/pool-evidence/async/report.json \
  --negative-tests --out /tmp/async-official-audit.json
python3 audit_current_cache.py /tmp/pool-evidence/async/report.json \
  /tmp/async-current-cache-audit.json
```

These replay commands use the archived evidence and repository verifier; they do not need the original VMs or asset directories. They validate recorded evidence rather than run the experiment again. Extraction needs about 1.5 GB of free space, and the JSON reports also require working memory during replay.

The broader `audit_receipts.py` additionally hashes the actual Firecracker and kernel binaries at the original asset paths stored in `config.assets`. Those binaries are not in these evidence archives. That independent audit requires the original asset directory, and fails if it is unavailable; the binary checks are never silently skipped.
