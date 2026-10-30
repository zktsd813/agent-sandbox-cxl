# Discard runtime build and source audit

The release binary and patch were frozen together on 2026-10-30. The patch was
applied to a clean archive of Firecracker commit
`95f868c8e345b1cc8faccd1a3c910b4989dc3f58`; all 28 changed source files were then
compared byte for byte with the build tree. This check passed.

| Artifact | SHA-256 |
| --- | --- |
| New Firecracker release | `75a9b6e52dd152b17962fb52cbf04d983ba7c035629517a91d2b43eb9ea45127` |
| `firecracker.patch` | `3600228b2076bf60fc01fdd74e22b4416100f4c020446e465e5d3bff31f0fb8b` |
| Preserved previous Firecracker release | `866f5b44b96535c0c525f6cee0a9ee09ff64900fbd83458c51c45b4de3505d08` |

The build tree is `/tmp/firecracker-pool-discard-20261030`. Its release binary is
`build/cargo_target/x86_64-unknown-linux-musl/release/firecracker`. Builds used
the existing `public.ecr.aws/firecracker/fcuvm:v93` image, no network, the existing
read-only Cargo dependency cache, and `--offline --locked --release --target
x86_64-unknown-linux-musl`. The original source and release were preserved.

See [build provenance](pool-discard-build-provenance.json) for all source hashes
and [artifact hashes](artifact-hashes.json) for the archived logs.

## Final tests

| Selection | Passed | Evidence |
| --- | ---: | --- |
| Pool implementation and reporting protocol | 9 | [log](pool-discard-tests-final3.log) |
| Entire balloon module | 31 | [log](pool-discard-balloon-regression.log) |
| Device snapshot preparation | 4 | [log](pool-discard-privileged-regression.log) |
| Guest memory creation | 1 | same privileged log |
| Direct handoff device admission | 1 | same privileged log |
| Failed storage snapshot produces no checkpoint | 1 | same privileged log |
| Async and synchronous file engines | 2 | [log](pool-discard-io-regression.log) |

Selections overlap: the two reporting configuration/protocol tests are included
in both the pool and balloon groups. Do not sum these rows as distinct tests.
The final executable was rebuilt after the last production changes, and its
source matches the release and regenerated patch. The protocol test runs an
isolated child process with `FC_POOL_DISCARD=1`; other balloon tests verify the
ordinary path with that opt-in absent.

The tests check real used-ring publication, busy-page no-op ACK, successful
remap then ACK, invalid request without ACK, full-size descriptor-chain cycles,
zero-copy private zero semantics, untouched peer/canonical content, unchanged
neighbor pages, partial-byte I/O overlap, coalescing, dirty tracking, invalid
range preflight, remap failure poison and restored balloon configuration.

## Retained earlier attempts

- `pool-discard-test-build-final.log`: an earlier test-only compile failed when
  accessing a private saved-state field. The test was rewritten through its
  serialized state; `final2` and `final3` test builds succeeded.
- `pool-discard-test-build-final2.log` and `pool-discard-tests-final2.log`: an
  earlier successful 9-test pass, before the final raw-NEXT cycle regression and
  async file-replacement drain change. Use `final3` as the final test evidence.
- `pool-discard-save-regression.log` and
  `pool-discard-persist-regression.log`: sandbox runs lacked io_uring, KVM, TAP,
  or Unix-socket permissions. Relevant snapshot tests were rerun with host
  device access and passed. The broader unrelated persistence selection was
  not rerun in full and is not claimed to have passed.
- The final entry in `pool-discard-privileged-regression.log` is an incorrectly
  named `async_io` filter that selected **zero tests**. It is not validation.
  The correct parent `io::tests` filter subsequently ran both file-engine
  tests successfully, in `pool-discard-io-regression.log`.

## Runtime contract and source review

`FC_POOL_DISCARD=1` permits only a balloon with target zero, no OOM deflation,
no statistics, no free-page hinting, and free-page reporting enabled. Runtime
configuration and final restored features/target/actual state are checked.
DAX restore rejects an ordinary balloon and Mem/Pmem devices. The allowed path
uses Linux's real page-reporting quarantine and virtqueue used-ring ACK.

The host freezes and validates the entire request before remapping. Only GPA
pages previously overlaid with canonical pool mappings are replaced. Active
block I/O protects both its data and status ranges; any byte overlap skips the
whole page. A skipped or nonpooled page may be ACKed unchanged because reporting
is a best-effort operation. The recorded `discarded` ranges, rather than all
`requested` ranges, identify pages actually detached.

Replacement uses anonymous private zero backing and never copies or overwrites
canonical data. Physical allocation and zero-page faults remain lazy. All
reported unbusy hints are invalidated. Dirty tracking records the new zero
content. A failed fixed mapping poisons state and aborts the VMM before ACK;
normal invalid requests receive no ACK. Other VMs retain their canonical
mapping. Canonical storage and the original DAX RAM reservation are not freed.

The active async block request owns its I/O guard until completion. During
backing-file replacement, `drain(false)` waits for every old kernel operation
before dropping the old ring/request slab. Source review and the existing
io_uring regressions support this lifetime; no dedicated simultaneous
discard plus hot-file-replacement experiment is claimed.

Snapshot API actions and balloon callbacks execute on one VMM event loop.
With vCPUs paused, device queue state and the logical RAM dump are collected
without a reporting callback between them. A report is wholly represented as
completed or pending in that checkpoint. Resume kicks the restored queues.
A fresh process has no live pool overlays until repooling, so a pending report
may complete as a no-op on materialized RAM. Reports processed on the paused
source after snapshot completion but before fencing affect only that source;
they are outside the checkpoint and cannot be treated as destination ACKs.

Once any pooled overlay or replacement exists, state-only DAX snapshots remain
rejected. Migration must use a full materialized logical RAM snapshot. This
experiment does not establish state-only/direct handoff for hybrid mappings.

`FC_POOL_DISCARD_TRACE=1` enables unrestricted per-batch JSON evidence; ordinary
rate-limited logging would be incomplete. This explicit lab trace can be
verbose. `host_work_ns` excludes JSON logging, while the guest wait until ACK
includes the pre-ACK trace cost. A `mapped` record precedes the used-index
publication; its matching `ack` record follows that release publication.
