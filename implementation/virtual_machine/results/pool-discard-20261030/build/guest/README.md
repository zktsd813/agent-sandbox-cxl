# Guest reporting kernel and probe provenance

This directory preserves the guest-side inputs used by both formal arms. The
kernel allocator/reporting algorithm was not changed. The patch adds validation
trace events to the real `virtballoon_free_page_report()` callback: isolated SG
submission, exact GPA ranges, consumed used-ring ACK, or submission failure.
The unchanged `mm/page_reporting.c` isolates pages before that callback and
returns them to the allocator after its completion.

| Measured artifact | SHA-256 |
| --- | --- |
| Guest `vmlinux`, `6.6.0-rc6-dcd-v3` | `ea81dc49b729e8d5111765c11ace1a11bd53db59f75fd65d933f0d983a368d05` |
| Guest config | `08d8c9ecf79f63a2b30470466de70c5285f4b73fb8cf3f5bbe62a2c5f2bbf127` |
| Previous guest config | `793c44048f74d4c2e29cca2a33021f2ff3a50876f7436e590ab2b9019fba31b6` |
| Guest trace patch | `211468b6a9ea3a18eeb51d84299ee8db9de6721f58905177b62ba3b8178c6296` |
| Formal guest probe | `7c91d4bbcf139fb22e195cac60e44034fa7ab00304086c1086bbdfa365bb92ad` |
| Initial pilot guest probe | `77a5f969167da67f7a7bdf721edae2c5099bd4eb9e7fcd80b9727c05146f9f09` |

[Provenance](provenance.json) binds the hashes above to the actual build output,
fixture files, and four raw runtime hash receipts: both L1 nodes in both formal
arms. The receipts include Firecracker, guest kernel, guest initramfs, probe, and
single-page diagnostic tool. They are copied unchanged from the formal evidence.
The [fixture manifest](fixture-manifest.json) also records the assembled images.
No kernel binary, vendor archive, generated payload, or mutable test disk is
duplicated in this source/provenance bundle.

## Source and build

The vendor input is the supplied `dcd-v3-2023-10-30.zip`, SHA-256
`5b29096b3c7e4d9dceeb431d5ff35cd549159524c055301d1e5d4cb6b5819975`.
[Kernel source metadata](kernel-source.json) records its archive root and the
existing preparation patches. This source is not a Git checkout. The actual
prepared baseline was `/home/ijkim/agent-sandbox-cxl/implementation/linux`;
the isolated guest source and build were respectively
`/tmp/pool-discard-guest-linux-20261030` and
`/tmp/pool-discard-guest-build-20261030`.

During packaging, the vendor archive hash was checked again. Its
`drivers/virtio/virtio_balloon.c` and `mm/page_reporting.c` were byte-identical to
those files in the prepared baseline. Their exact vendor versions are under
`vendor-base/`, and the three compiled relevant sources are under
`compiled-source/`. Applying the archived trace patch to the vendor files in a
fresh temporary directory reproduced all three compiled-source hashes. The
patch replay stdout/stderr is preserved. This verifies the relevant source
delta; it is not a new complete kernel rebuild.

[The original build record](original-build-record.json),
[kernel build log](kernel-build.log), [config log](kernel-config.log),
[actual config](guest.config), and `compiler-commands/*.o.cmd` preserve the
executed make/compiler inputs and their source paths. `compile.h` records GCC
11.4.0 and GNU ld 2.38. Relative to `base-guest.config`, the newly enabled options
were `VIRTIO_BALLOON`, `PAGE_REPORTING`, `MEMORY_BALLOON`, and
`BALLOON_COMPACTION`; the latter two were selected as dependencies. Tracing and
debugfs were already enabled. Both formal arms booted this same kernel.

To rebuild in fresh paths, first prepare the vendor source using the repository
kernel instructions and its existing CXL/KVM patches. From
`implementation/virtual_machine`, with `DISCARD_BASE_SOURCE` pointing to that
prepared source:

```sh
DISCARD_RECORD="$PWD/results/pool-discard-20261030/build/guest"
DISCARD_SOURCE=/tmp/discard-guest-source-repro
DISCARD_BUILD=/tmp/discard-guest-build-repro
test ! -e "$DISCARD_SOURCE"
test ! -e "$DISCARD_BUILD"
cp -a --reflink=auto "$DISCARD_BASE_SOURCE" "$DISCARD_SOURCE"
mkdir "$DISCARD_BUILD"
cp "$DISCARD_RECORD/base-guest.config" "$DISCARD_BUILD/.config"
patch --batch -p1 -d "$DISCARD_SOURCE" -i "$DISCARD_RECORD/pool-discard-guest-trace.patch"
"$DISCARD_SOURCE/scripts/config" --file "$DISCARD_BUILD/.config" \
  --enable VIRTIO_BALLOON --enable PAGE_REPORTING
make -C "$DISCARD_SOURCE" O="$DISCARD_BUILD" LOCALVERSION= olddefconfig
make -C "$DISCARD_SOURCE" O="$DISCARD_BUILD" LOCALVERSION= -j24 vmlinux
```

Build paths, toolchain, and kernel build metadata can affect a rebuilt ELF hash.
The measured binary identity above belongs to the original output, whose hash
was checked against the actual runtime fixture; it is not a promise that every
fresh build will be byte-identical.

The formal guest boot arguments additionally enabled
`page_reporting.page_reporting_order=0`,
`trace_event=pool_discard:pool_free_report`, `trace_buf_size=8M`, and
`trace_clock=mono`. The original delayed/budgeted Linux reporter remains in use.
The guest init script and actual runtime logs preserve these settings.

## Probe and host smoke evidence

`probe/pilot/` preserves the first probe source, nine-command 224MiB host smoke,
and six rejected state cases. `probe/formal/` preserves the final source and a
later host smoke: a valid 1MiB allocation/fill/verify/free flow, plus rejection of
449MiB allocation, verify-before-fill, and duplicate allocation. These are host
correctness checks, not microVM performance results. Their original summaries
and stdout/stderr remain unchanged; the early summary contains its negative
stderr strings, while the later smoke retains separate stream files.

The final probe was built with:

```sh
gcc -std=c11 -O2 -static -Wall -Wextra -Werror \
  pool_discard_probe.c -o /tmp/pool-discard-probe-v3
```

Packaging rebuilt the archived final source using these flags in a new temporary
directory. The resulting binary exactly matched the measured final probe hash;
[the replay record](probe/formal/rebuild.json) and compiler stdout/stderr retain
that check. The [commands record](commands.json) preserves the original kernel,
probe, fixture, and formal-run commands, plus the retained host-smoke input
protocols. The initial smoke's outer launcher was not captured as a command log;
its preserved protocol/output and source/binary identity are the available
evidence.

All retained files except the hash list itself are covered by
[artifact-hashes.json](artifact-hashes.json).
