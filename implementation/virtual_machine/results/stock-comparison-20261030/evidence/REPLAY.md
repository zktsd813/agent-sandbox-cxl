# Offline evidence replay

Run these commands from this package directory. They check the recorded evidence;
they do not rerun the VMs or require the intentionally excluded `*-storage.raw`
outer disks. Archive paths preserve the reports' original relative references.

```sh
sha256sum -c SHA256SUMS
python3 package_pool_comparison.py --verify-only . --extract-to /tmp/pool-comparison-replay
```

The second command validates every part, the SHA-256 of each complete compressed
archive, exact tar-member membership, and the size/SHA-256 of every extracted file.
It streams split parts in their recorded order; no manual concatenation is needed.
The extraction destination must not already exist. Validation runs on CPUs 8-15;
on a replay host with a different CPU topology, use `--no-affinity`.

When this package is in the checkout under
`implementation/virtual_machine/results/stock-comparison-20261030/evidence`,
the following command locates the verifier directory through its ancestors.
If the evidence was copied elsewhere, set `VM_IMPL` explicitly to the checkout's
`implementation/virtual_machine` directory instead.

```sh
VM_IMPL=$(python3 -c 'from pathlib import Path; print(next(p for p in Path.cwd().parents if (p / "verify_pool_compare.py").is_file()))')
```

Then replay the primary suite and the tail stock arm:

```sh
python3 "$VM_IMPL/verify_pool_compare.py" /tmp/pool-comparison-replay/primary/report.json --negative-tests --out /tmp/pool-comparison-primary-audit.json
python3 "$VM_IMPL/verify_pool_compare.py" /tmp/pool-comparison-replay/tail/stock-dram/report.json --negative-tests --out /tmp/pool-comparison-tail-audit.json
python3 "$VM_IMPL/verify_stock_snapshot.py" /tmp/pool-comparison-replay/primary/stock-snapshot/report.json --out /tmp/pool-comparison-stock-snapshot-audit.json
```

The primary suite contains four timed arms and the separate stock snapshot proof.
The tail contains only an additional stock-dram run; it is not a full four-arm
suite, so the tail command selects its child report. Both trees include the exact
measured harness sources. `manifest.json` lists every archived file's bytes/hash;
`metadata.json` records report hashes, exclusions, and archive/part hashes.

For manual extraction, concatenate a split archive's `.part001`, `.part002`, ...
in the order listed in `metadata.json`, verify the resulting full archive hash,
then use `tar -xJf`. Extract every archive into one directory; this creates sibling
`primary/` and `tail/` directories. The automated command above also audits the
individual member hashes and rejects unsafe or duplicate paths.
