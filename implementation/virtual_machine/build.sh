#!/usr/bin/env bash
# Build current sources using the existing offline toolchain and caches.
set -euo pipefail
lab_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$lab_dir/../.." && pwd)"
fc_source="$repo_dir/firecracker"

fc_cargo() {
    [[ -f "$fc_source/Cargo.toml" ]] || {
        echo "Firecracker source missing: $fc_source (see README.md bootstrap steps)." >&2
        return 1
    }
    docker run --rm --pull never --network none --user "$(id -u):$(id -g)" \
        --volume "$fc_source:/firecracker" \
        --volume "$fc_source/build/cargo_registry:/usr/local/rust/registry" \
        --volume "$fc_source/build/cargo_git_registry:/usr/local/rust/git" \
        --workdir /firecracker public.ecr.aws/firecracker/fcuvm:v93 \
        cargo "$@" --offline --locked --release --target x86_64-unknown-linux-musl
}

build_firecracker() {
    # Never reapply an old patch over the working tree during a build.
    fc_cargo build --bin firecracker
    printf 'Firecracker: %s\n' "$fc_source/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker"
}

build_kernel() (
    kernel_source="$(cd -- "$lab_dir/../linux" && pwd)"
    kernel_build="${CXL_LAB_KERNEL_BUILD:-$lab_dir/.local/kernel-build}"
    kernel_modules="${CXL_LAB_KERNEL_MODULES:-$lab_dir/.local/kernel-modules}"
    kernel_config="${CXL_LAB_KERNEL_CONFIG:-$lab_dir/kernel.config}"
    build_jobs="${JOBS:-24}"
    cxl_emulation_test="${CXL_EMULATION_TEST:-1}"
    kvm_dax_test="${KVM_DAX_TEST:-1}"
    mptcp_test="${MPTCP_TEST:-0}"
    mkdir -p "$kernel_build" "$kernel_modules"
    # The supplied DCD archive needs the static-capacity fixes before the KVM
    # experiment patch. Accept an already patched source tree on later builds.
    if ! patch --silent --dry-run --reverse -p1 -d "$kernel_source" \
            -i "$lab_dir/cxl-static-capacity.patch" >/dev/null 2>&1; then
        patch --dry-run -p1 -d "$kernel_source" -i "$lab_dir/cxl-static-capacity.patch"
        patch -p1 -d "$kernel_source" -i "$lab_dir/cxl-static-capacity.patch"
    fi
    if [[ "$kvm_dax_test" == 1 && -f "$lab_dir/kvm-dax-state.patch" ]]; then
        if ! patch --silent --dry-run --reverse -p1 -d "$kernel_source" \
                -i "$lab_dir/kvm-dax-state.patch" >/dev/null 2>&1; then
            patch --dry-run -p1 -d "$kernel_source" -i "$lab_dir/kvm-dax-state.patch"
            patch -p1 -d "$kernel_source" -i "$lab_dir/kvm-dax-state.patch"
        fi
    fi

    if [[ ! -f "$kernel_build/.config" ]]; then
        if [[ -f "$kernel_config" ]]; then
            cp "$kernel_config" "$kernel_build/.config"
        else
            make -C "$kernel_source" O="$kernel_build" x86_64_defconfig
        fi
    fi
    "$kernel_source/scripts/config" --file "$kernel_build/.config" \
        --set-str LOCALVERSION '-dcd-v3' --disable LOCALVERSION_AUTO \
        --enable MODULES --enable VIRTUALIZATION --module KVM \
        --module KVM_INTEL --module KVM_AMD \
        --enable BLK_DEV_INITRD --enable DEVTMPFS --enable DEVTMPFS_MOUNT \
        --enable SERIAL_8250 --enable SERIAL_8250_CONSOLE --enable DEVMEM \
        --enable PCI --enable PCI_MSI --enable VIRTIO_PCI --enable VIRTIO_MMIO \
        --enable NUMA --enable MEMORY_HOTPLUG --enable MEMORY_HOTREMOVE \
        --enable MEMORY_HOTPLUG_DEFAULT_ONLINE --enable ZONE_DEVICE \
        --enable TRANSPARENT_HUGEPAGE --enable TRANSPARENT_HUGEPAGE_MADVISE \
        --enable LIBNVDIMM --enable BLK_DEV_PMEM --enable ND_PFN --enable ACPI_NFIT \
        --enable NVDIMM_PFN --enable NVDIMM_DAX --enable DAX \
        --enable FS_DAX --enable DEV_DAX --enable DEV_DAX_PMEM --enable DEV_DAX_KMEM \
        --enable CXL_BUS --enable CXL_PCI --enable CXL_ACPI --enable CXL_MEM \
        --enable CXL_PMEM --enable CXL_REGION --enable DEV_DAX_CXL \
        --enable CHECKPOINT_RESTORE --disable DEBUG_INFO --enable DEBUG_INFO_NONE \
        --disable DEBUG_INFO_BTF --disable MODULE_SIG \
        --set-str SYSTEM_TRUSTED_KEYS '' --set-str SYSTEM_REVOCATION_KEYS ''
    if [[ "$mptcp_test" == 1 ]]; then
        "$kernel_source/scripts/config" --file "$kernel_build/.config" \
            --enable MPTCP --enable INET_DIAG --enable INET_MPTCP_DIAG \
            --enable TUN --enable IP_ADVANCED_ROUTER --enable IP_MULTIPLE_TABLES
    fi
    # The CXL driver rejects region commit under a hypervisor without this lab-only
    # switch. Real hardware builds must use CXL_EMULATION_TEST=0.
    if [[ "$cxl_emulation_test" == 1 ]]; then
        "$kernel_source/scripts/config" --file "$kernel_build/.config" --enable CXL_REGION_INVALIDATION_TEST
    else
        "$kernel_source/scripts/config" --file "$kernel_build/.config" --disable CXL_REGION_INVALIDATION_TEST
    fi
    if [[ "$kvm_dax_test" == 1 ]]; then
        "$kernel_source/scripts/config" --file "$kernel_build/.config" --enable KVM_DAX_POOL_TEST
    else
        "$kernel_source/scripts/config" --file "$kernel_build/.config" --disable KVM_DAX_POOL_TEST
    fi
    make -C "$kernel_source" O="$kernel_build" LOCALVERSION= olddefconfig
    for symbol in BLK_DEV_INITRD DEVTMPFS SERIAL_8250_CONSOLE DEVMEM NUMA \
                  CXL_BUS CXL_MEM CXL_REGION DEV_DAX DEV_DAX_PMEM DEV_DAX_KMEM DEV_DAX_CXL; do
        grep -qx "CONFIG_${symbol}=y" "$kernel_build/.config" || {
            echo "Missing required built-in option: $symbol" >&2; exit 1;
        }
    done
    grep -qx 'CONFIG_KVM=m' "$kernel_build/.config"
    grep -qx 'CONFIG_KVM_INTEL=m' "$kernel_build/.config"
    if [[ "$mptcp_test" == 1 ]]; then
        for symbol in MPTCP INET_DIAG INET_MPTCP_DIAG TUN IP_ADVANCED_ROUTER IP_MULTIPLE_TABLES; do
            grep -qx "CONFIG_${symbol}=y" "$kernel_build/.config" || {
                echo "Missing required MPTCP built-in option: $symbol" >&2; exit 1;
            }
        done
    fi
    make -C "$kernel_source" O="$kernel_build" LOCALVERSION= -j"$build_jobs" bzImage modules
    make -C "$kernel_source" O="$kernel_build" LOCALVERSION= \
        INSTALL_MOD_PATH="$kernel_modules" INSTALL_MOD_STRIP=1 modules_install
    kernel_release="$(cat "$kernel_build/include/config/kernel.release")"
    depmod -b "$kernel_modules" "$kernel_release"
    cp "$kernel_build/.config" "$kernel_config"
    printf 'Kernel: %s\nL1 image: %s\nL2 image: %s\nModules: %s\n' \
        "$kernel_release" "$kernel_build/arch/x86/boot/bzImage" \
        "$kernel_build/vmlinux" "$kernel_modules/lib/modules/$kernel_release"
)

build_qemu() (
    base_source="${1:-/home/ijkim/Trenv-openwhisk/labs/niagara-vm/host/state/qemu-src}"
    qemu_root="$lab_dir/.local/qemu-cxl"
    mkdir -p "$qemu_root/src" "$qemu_root/build"
    if [[ ! -f "$qemu_root/src/configure" ]]; then
        cp -a --reflink=auto "$base_source/." "$qemu_root/src/"
    fi
    if ! patch --silent --dry-run --reverse -p1 -d "$qemu_root/src" \
            -i "$lab_dir/qemu-cxl-direct-ram.patch" >/dev/null 2>&1; then
        patch -p1 -d "$qemu_root/src" -i "$lab_dir/qemu-cxl-direct-ram.patch"
    fi
    cd "$qemu_root/build"
    if [[ ! -f build.ninja ]]; then
        ../src/configure --target-list=x86_64-softmmu --disable-docs --disable-werror \
            --disable-download --disable-tools --disable-guest-agent --disable-gtk \
            --disable-sdl --disable-vnc --disable-spice --disable-opengl \
            --enable-kvm --disable-debug-info
    fi
    ninja -j"${JOBS:-24}" qemu-system-x86_64
    printf 'QEMU: %s\n' "$qemu_root/build/qemu-system-x86_64"
)

test_sources() {
    python3 -m py_compile "$lab_dir/lab.py"
    local probe_test
    probe_test="$(mktemp "${TMPDIR:-/tmp}/cxl-shared-probe.XXXXXX")"
    if ! gcc -O2 -Wall -Wextra -Werror -static -pthread \
            "$lab_dir/shared_probe.c" -o "$probe_test"; then
        rm -f "$probe_test"
        return 1
    fi
    rm -f "$probe_test"
    # Filter to the modified pure contracts and the actions API parser.
    fc_cargo test -p vmm reuse::tests
    fc_cargo test -p firecracker test_parse_put_actions
}

usage() {
    cat <<'USAGE'
Usage: build.sh {firecracker|kernel|qemu [SOURCE]|all|test}

firecracker  Compile the current Firecracker source without applying patches.
kernel       Build the DCD lab kernel and stage its modules.
qemu         Build an isolated QEMU copy (optionally from SOURCE).
all          Build kernel, QEMU and Firecracker.
test         Check Python/C and the related Firecracker unit tests.

Optional environment: JOBS=24, CXL_EMULATION_TEST=1, KVM_DAX_TEST=1, MPTCP_TEST=0.
Isolated kernel paths: CXL_LAB_KERNEL_BUILD, CXL_LAB_KERNEL_MODULES,
                      CXL_LAB_KERNEL_CONFIG (defaults preserve the lab paths).
USAGE
}

case "${1:---help}" in
    firecracker|kernel|test|all)
        [[ $# == 1 ]] || { usage >&2; exit 2; }
        case "$1" in
            firecracker) build_firecracker ;;
            kernel) build_kernel ;;
            test) test_sources ;;
            all) build_kernel; build_qemu; build_firecracker ;;
        esac
        ;;
    qemu)
        [[ $# -le 2 ]] || { usage >&2; exit 2; }
        build_qemu "${2:-/home/ijkim/Trenv-openwhisk/labs/niagara-vm/host/state/qemu-src}"
        ;;
    -h|--help) usage ;;
    *) usage >&2; exit 2 ;;
esac
