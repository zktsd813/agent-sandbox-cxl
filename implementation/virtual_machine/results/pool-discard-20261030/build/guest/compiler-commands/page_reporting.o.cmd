savedcmd_mm/page_reporting.o := gcc -Wp,-MMD,mm/.page_reporting.o.d -nostdinc -I/tmp/pool-discard-guest-linux-20261030/arch/x86/include -I./arch/x86/include/generated -I/tmp/pool-discard-guest-linux-20261030/include -I./include -I/tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi -I./arch/x86/include/generated/uapi -I/tmp/pool-discard-guest-linux-20261030/include/uapi -I./include/generated/uapi -include /tmp/pool-discard-guest-linux-20261030/include/linux/compiler-version.h -include /tmp/pool-discard-guest-linux-20261030/include/linux/kconfig.h -include /tmp/pool-discard-guest-linux-20261030/include/linux/compiler_types.h -D__KERNEL__ -fmacro-prefix-map=/tmp/pool-discard-guest-linux-20261030/= -Werror -std=gnu11 -fshort-wchar -funsigned-char -fno-common -fno-PIE -fno-strict-aliasing -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -fcf-protection=branch -fno-jump-tables -m64 -falign-jumps=1 -falign-loops=1 -mno-80387 -mno-fp-ret-in-387 -mpreferred-stack-boundary=3 -mskip-rax-setup -mtune=generic -mno-red-zone -mcmodel=kernel -Wno-sign-compare -fno-asynchronous-unwind-tables -mindirect-branch=thunk-extern -mindirect-branch-register -mindirect-branch-cs-prefix -mfunction-return=thunk-extern -fno-jump-tables -fpatchable-function-entry=16,16 -fno-delete-null-pointer-checks -O2 -fno-allow-store-data-races -fstack-protector-strong -fomit-frame-pointer -fno-stack-clash-protection -falign-functions=16 -fno-strict-overflow -fno-stack-check -fconserve-stack -Wall -Wundef -Werror=implicit-function-declaration -Werror=implicit-int -Werror=return-type -Werror=strict-prototypes -Wno-format-security -Wno-trigraphs -Wno-frame-address -Wno-address-of-packed-member -Wframe-larger-than=2048 -Wno-main -Wno-unused-but-set-variable -Wno-unused-const-variable -Wvla -Wno-pointer-sign -Wcast-function-type -Wno-array-bounds -Wno-alloc-size-larger-than -Wimplicit-fallthrough=5 -Werror=date-time -Werror=incompatible-pointer-types -Werror=designated-init -Wenum-conversion -Wno-unused-but-set-variable -Wno-unused-const-variable -Wno-restrict -Wno-packed-not-aligned -Wno-format-overflow -Wno-format-truncation -Wno-stringop-overflow -Wno-stringop-truncation -Wno-missing-field-initializers -Wno-type-limits -Wno-shift-negative-value -Wno-maybe-uninitialized -Wno-sign-compare -I /tmp/pool-discard-guest-linux-20261030/mm -I ./mm    -DKBUILD_MODFILE='"mm/page_reporting"' -DKBUILD_BASENAME='"page_reporting"' -DKBUILD_MODNAME='"page_reporting"' -D__KBUILD_MODNAME=kmod_page_reporting -c -o mm/page_reporting.o /tmp/pool-discard-guest-linux-20261030/mm/page_reporting.c  

source_mm/page_reporting.o := /tmp/pool-discard-guest-linux-20261030/mm/page_reporting.c

deps_mm/page_reporting.o := \
  /tmp/pool-discard-guest-linux-20261030/include/linux/compiler-version.h \
    $(wildcard include/config/CC_VERSION_TEXT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kconfig.h \
    $(wildcard include/config/CPU_BIG_ENDIAN) \
    $(wildcard include/config/BOOGER) \
    $(wildcard include/config/FOO) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/compiler_types.h \
    $(wildcard include/config/DEBUG_INFO_BTF) \
    $(wildcard include/config/PAHOLE_HAS_BTF_TAG) \
    $(wildcard include/config/FUNCTION_ALIGNMENT) \
    $(wildcard include/config/CC_IS_GCC) \
    $(wildcard include/config/X86_64) \
    $(wildcard include/config/ARM64) \
    $(wildcard include/config/HAVE_ARCH_COMPILER_H) \
    $(wildcard include/config/CC_HAS_ASM_INLINE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/compiler_attributes.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/compiler-gcc.h \
    $(wildcard include/config/RETPOLINE) \
    $(wildcard include/config/ARCH_USE_BUILTIN_BSWAP) \
    $(wildcard include/config/SHADOW_CALL_STACK) \
    $(wildcard include/config/KCOV) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mm.h \
    $(wildcard include/config/NUMA) \
    $(wildcard include/config/SYSCTL) \
    $(wildcard include/config/HAVE_ARCH_MMAP_RND_BITS) \
    $(wildcard include/config/HAVE_ARCH_MMAP_RND_COMPAT_BITS) \
    $(wildcard include/config/SPARSEMEM) \
    $(wildcard include/config/SPARSEMEM_VMEMMAP) \
    $(wildcard include/config/MMU) \
    $(wildcard include/config/MEM_SOFT_DIRTY) \
    $(wildcard include/config/ARCH_USES_HIGH_VMA_FLAGS) \
    $(wildcard include/config/ARCH_HAS_PKEYS) \
    $(wildcard include/config/PPC) \
    $(wildcard include/config/X86_USER_SHADOW_STACK) \
    $(wildcard include/config/X86) \
    $(wildcard include/config/PARISC) \
    $(wildcard include/config/IA64) \
    $(wildcard include/config/SPARC64) \
    $(wildcard include/config/ARM64_MTE) \
    $(wildcard include/config/HAVE_ARCH_USERFAULTFD_MINOR) \
    $(wildcard include/config/STACK_GROWSUP) \
    $(wildcard include/config/NUMA_BALANCING) \
    $(wildcard include/config/PER_VMA_LOCK) \
    $(wildcard include/config/SHMEM) \
    $(wildcard include/config/ZONE_DEVICE) \
    $(wildcard include/config/FS_DAX) \
    $(wildcard include/config/KASAN_SW_TAGS) \
    $(wildcard include/config/KASAN_HW_TAGS) \
    $(wildcard include/config/MIGRATION) \
    $(wildcard include/config/CMA) \
    $(wildcard include/config/64BIT) \
    $(wildcard include/config/HIGHMEM) \
    $(wildcard include/config/SCHED_MM_CID) \
    $(wildcard include/config/ARCH_HAS_PTE_SPECIAL) \
    $(wildcard include/config/ARCH_HAS_PTE_DEVMAP) \
    $(wildcard include/config/TRANSPARENT_HUGEPAGE) \
    $(wildcard include/config/DEBUG_VM_RB) \
    $(wildcard include/config/PAGE_POISONING) \
    $(wildcard include/config/INIT_ON_ALLOC_DEFAULT_ON) \
    $(wildcard include/config/INIT_ON_FREE_DEFAULT_ON) \
    $(wildcard include/config/DEBUG_PAGEALLOC) \
    $(wildcard include/config/MEMORY_HOTPLUG) \
    $(wildcard include/config/ARCH_WANT_OPTIMIZE_DAX_VMEMMAP) \
    $(wildcard include/config/MEMORY_FAILURE) \
    $(wildcard include/config/KSM) \
    $(wildcard include/config/HUGETLBFS) \
    $(wildcard include/config/MAPPING_DIRTY_HELPERS) \
    $(wildcard include/config/PRINTK) \
    $(wildcard include/config/ANON_VMA_NAME) \
    $(wildcard include/config/UNACCEPTED_MEMORY) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/errno.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/errno.h \
  arch/x86/include/generated/uapi/asm/errno.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/errno.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/errno-base.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mmdebug.h \
    $(wildcard include/config/DEBUG_VM) \
    $(wildcard include/config/DEBUG_VM_IRQSOFF) \
    $(wildcard include/config/DEBUG_VIRTUAL) \
    $(wildcard include/config/DEBUG_VM_PGFLAGS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bug.h \
    $(wildcard include/config/GENERIC_BUG) \
    $(wildcard include/config/BUG_ON_DATA_CORRUPTION) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/bug.h \
    $(wildcard include/config/X86_32) \
    $(wildcard include/config/DEBUG_BUGVERBOSE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stringify.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/instrumentation.h \
    $(wildcard include/config/NOINSTR_VALIDATION) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/objtool.h \
    $(wildcard include/config/OBJTOOL) \
    $(wildcard include/config/FRAME_POINTER) \
    $(wildcard include/config/CPU_UNRET_ENTRY) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/objtool_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/types.h \
    $(wildcard include/config/HAVE_UID16) \
    $(wildcard include/config/UID16) \
    $(wildcard include/config/ARCH_DMA_ADDR_T_64BIT) \
    $(wildcard include/config/PHYS_ADDR_T_64BIT) \
    $(wildcard include/config/ARCH_32BIT_USTAT_F_TINODE) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/types.h \
  arch/x86/include/generated/uapi/asm/types.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/types.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/int-ll64.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/int-ll64.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/bitsperlong.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitsperlong.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/bitsperlong.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/posix_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stddef.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/stddef.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/posix_types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/posix_types_64.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/posix_types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/asm.h \
    $(wildcard include/config/KPROBES) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/extable_fixup_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bug.h \
    $(wildcard include/config/BUG) \
    $(wildcard include/config/GENERIC_BUG_RELATIVE_POINTERS) \
    $(wildcard include/config/SMP) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/compiler.h \
    $(wildcard include/config/TRACE_BRANCH_PROFILING) \
    $(wildcard include/config/PROFILE_ALL_BRANCHES) \
  arch/x86/include/generated/asm/rwonce.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/rwonce.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kasan-checks.h \
    $(wildcard include/config/KASAN_GENERIC) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kcsan-checks.h \
    $(wildcard include/config/KCSAN) \
    $(wildcard include/config/KCSAN_WEAK_MEMORY) \
    $(wildcard include/config/KCSAN_IGNORE_ATOMICS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/once_lite.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/panic.h \
    $(wildcard include/config/PANIC_TIMEOUT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/printk.h \
    $(wildcard include/config/MESSAGE_LOGLEVEL_DEFAULT) \
    $(wildcard include/config/CONSOLE_LOGLEVEL_DEFAULT) \
    $(wildcard include/config/CONSOLE_LOGLEVEL_QUIET) \
    $(wildcard include/config/EARLY_PRINTK) \
    $(wildcard include/config/PRINTK_INDEX) \
    $(wildcard include/config/DYNAMIC_DEBUG) \
    $(wildcard include/config/DYNAMIC_DEBUG_CORE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stdarg.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/init.h \
    $(wildcard include/config/HAVE_ARCH_PREL32_RELOCATIONS) \
    $(wildcard include/config/STRICT_KERNEL_RWX) \
    $(wildcard include/config/STRICT_MODULE_RWX) \
    $(wildcard include/config/LTO_CLANG) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/build_bug.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kern_levels.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/linkage.h \
    $(wildcard include/config/ARCH_USE_SYM_ANNOTATIONS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/export.h \
    $(wildcard include/config/MODVERSIONS) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/linkage.h \
    $(wildcard include/config/CALL_PADDING) \
    $(wildcard include/config/RETHUNK) \
    $(wildcard include/config/SLS) \
    $(wildcard include/config/FUNCTION_PADDING_BYTES) \
    $(wildcard include/config/UML) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/ibt.h \
    $(wildcard include/config/X86_KERNEL_IBT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ratelimit_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bits.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/const.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/const.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/const.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/bits.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/param.h \
  arch/x86/include/generated/uapi/asm/param.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/param.h \
    $(wildcard include/config/HZ) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/param.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/spinlock_types_raw.h \
    $(wildcard include/config/DEBUG_SPINLOCK) \
    $(wildcard include/config/DEBUG_LOCK_ALLOC) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/spinlock_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/qspinlock_types.h \
    $(wildcard include/config/NR_CPUS) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/qrwlock_types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/byteorder.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/byteorder/little_endian.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/byteorder/little_endian.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/swab.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/swab.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/swab.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/byteorder/generic.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/lockdep_types.h \
    $(wildcard include/config/PROVE_RAW_LOCK_NESTING) \
    $(wildcard include/config/LOCKDEP) \
    $(wildcard include/config/LOCK_STAT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/gfp.h \
    $(wildcard include/config/ZONE_DMA) \
    $(wildcard include/config/ZONE_DMA32) \
    $(wildcard include/config/CONTIG_ALLOC) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/gfp_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mmzone.h \
    $(wildcard include/config/ARCH_FORCE_MAX_ORDER) \
    $(wildcard include/config/MEMORY_ISOLATION) \
    $(wildcard include/config/ZSMALLOC) \
    $(wildcard include/config/SWAP) \
    $(wildcard include/config/LRU_GEN) \
    $(wildcard include/config/LRU_GEN_STATS) \
    $(wildcard include/config/MEMCG) \
    $(wildcard include/config/COMPACTION) \
    $(wildcard include/config/FLATMEM) \
    $(wildcard include/config/PAGE_EXTENSION) \
    $(wildcard include/config/DEFERRED_STRUCT_PAGE_INIT) \
    $(wildcard include/config/HAVE_MEMORYLESS_NODES) \
    $(wildcard include/config/SPARSEMEM_EXTREME) \
    $(wildcard include/config/HAVE_ARCH_PFN_VALID) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/spinlock.h \
    $(wildcard include/config/PREEMPTION) \
    $(wildcard include/config/PREEMPT_RT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/typecheck.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/preempt.h \
    $(wildcard include/config/PREEMPT_COUNT) \
    $(wildcard include/config/DEBUG_PREEMPT) \
    $(wildcard include/config/TRACE_PREEMPT_TOGGLE) \
    $(wildcard include/config/PREEMPT_NOTIFIERS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cleanup.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/list.h \
    $(wildcard include/config/LIST_HARDENED) \
    $(wildcard include/config/DEBUG_LIST) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/container_of.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/poison.h \
    $(wildcard include/config/ILLEGAL_POINTER_VALUE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/barrier.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/alternative.h \
    $(wildcard include/config/CALL_THUNKS) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/nops.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/barrier.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/preempt.h \
    $(wildcard include/config/PREEMPT_DYNAMIC) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/rmwcc.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/args.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/percpu.h \
    $(wildcard include/config/X86_64_SMP) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kernel.h \
    $(wildcard include/config/PREEMPT_VOLUNTARY_BUILD) \
    $(wildcard include/config/HAVE_PREEMPT_DYNAMIC_CALL) \
    $(wildcard include/config/HAVE_PREEMPT_DYNAMIC_KEY) \
    $(wildcard include/config/PREEMPT_) \
    $(wildcard include/config/DEBUG_ATOMIC_SLEEP) \
    $(wildcard include/config/PROVE_LOCKING) \
    $(wildcard include/config/TRACING) \
    $(wildcard include/config/FTRACE_MCOUNT_RECORD) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/align.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/limits.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/limits.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/limits.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bitops.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/kernel.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/sysinfo.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/generic-non-atomic.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/bitops.h \
    $(wildcard include/config/X86_CMOV) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/sched.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/arch_hweight.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cpufeatures.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/required-features.h \
    $(wildcard include/config/X86_MINIMUM_CPU_FAMILY) \
    $(wildcard include/config/MATH_EMULATION) \
    $(wildcard include/config/X86_PAE) \
    $(wildcard include/config/X86_CMPXCHG64) \
    $(wildcard include/config/X86_P6_NOP) \
    $(wildcard include/config/MATOM) \
    $(wildcard include/config/PARAVIRT_XXL) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/disabled-features.h \
    $(wildcard include/config/X86_UMIP) \
    $(wildcard include/config/X86_INTEL_MEMORY_PROTECTION_KEYS) \
    $(wildcard include/config/X86_5LEVEL) \
    $(wildcard include/config/PAGE_TABLE_ISOLATION) \
    $(wildcard include/config/CALL_DEPTH_TRACKING) \
    $(wildcard include/config/ADDRESS_MASKING) \
    $(wildcard include/config/INTEL_IOMMU_SVM) \
    $(wildcard include/config/X86_SGX) \
    $(wildcard include/config/XEN_PV) \
    $(wildcard include/config/INTEL_TDX_GUEST) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/const_hweight.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/instrumented-atomic.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/instrumented.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kmsan-checks.h \
    $(wildcard include/config/KMSAN) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/instrumented-non-atomic.h \
    $(wildcard include/config/KCSAN_ASSUME_PLAIN_WRITES_ATOMIC) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/instrumented-lock.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/le.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/bitops/ext2-atomic-setbit.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/hex.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kstrtox.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/log2.h \
    $(wildcard include/config/ARCH_HAS_ILOG2_U32) \
    $(wildcard include/config/ARCH_HAS_ILOG2_U64) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/math.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/div64.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/div64.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/minmax.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sprintf.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/static_call_types.h \
    $(wildcard include/config/HAVE_STATIC_CALL) \
    $(wildcard include/config/HAVE_STATIC_CALL_INLINE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/instruction_pointer.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/percpu.h \
    $(wildcard include/config/HAVE_SETUP_PER_CPU_AREA) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/threads.h \
    $(wildcard include/config/BASE_SMALL) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/percpu-defs.h \
    $(wildcard include/config/DEBUG_FORCE_WEAK_PER_CPU) \
    $(wildcard include/config/AMD_MEM_ENCRYPT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/current.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cache.h \
    $(wildcard include/config/ARCH_HAS_CACHE_LINE_SIZE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cache.h \
    $(wildcard include/config/X86_L1_CACHE_SHIFT) \
    $(wildcard include/config/X86_INTERNODE_CACHE_SHIFT) \
    $(wildcard include/config/X86_VSMP) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/thread_info.h \
    $(wildcard include/config/THREAD_INFO_IN_TASK) \
    $(wildcard include/config/GENERIC_ENTRY) \
    $(wildcard include/config/HAVE_ARCH_WITHIN_STACK_FRAMES) \
    $(wildcard include/config/HARDENED_USERCOPY) \
    $(wildcard include/config/SH) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/restart_block.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/time64.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/math64.h \
    $(wildcard include/config/ARCH_SUPPORTS_INT128) \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/math64.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/time64.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/time.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/time_types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/thread_info.h \
    $(wildcard include/config/VM86) \
    $(wildcard include/config/X86_IOPL_IOPERM) \
    $(wildcard include/config/COMPAT) \
    $(wildcard include/config/IA32_EMULATION) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/page.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/page_types.h \
    $(wildcard include/config/PHYSICAL_START) \
    $(wildcard include/config/PHYSICAL_ALIGN) \
    $(wildcard include/config/DYNAMIC_PHYSICAL_MASK) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mem_encrypt.h \
    $(wildcard include/config/ARCH_HAS_MEM_ENCRYPT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/mem_encrypt.h \
    $(wildcard include/config/X86_MEM_ENCRYPT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cc_platform.h \
    $(wildcard include/config/ARCH_HAS_CC_PLATFORM) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/bootparam.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/screen_info.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/screen_info.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/apm_bios.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/apm_bios.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/ioctl.h \
  arch/x86/include/generated/uapi/asm/ioctl.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/ioctl.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/ioctl.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/edd.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/edd.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/ist.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/ist.h \
  /tmp/pool-discard-guest-linux-20261030/include/video/edid.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/video/edid.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/page_64_types.h \
    $(wildcard include/config/KASAN) \
    $(wildcard include/config/DYNAMIC_MEMORY_LAYOUT) \
    $(wildcard include/config/RANDOMIZE_BASE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/kaslr.h \
    $(wildcard include/config/RANDOMIZE_MEMORY) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/page_64.h \
    $(wildcard include/config/X86_VSYSCALL_EMULATION) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/range.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/memory_model.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pfn.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/getorder.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cpufeature.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/processor.h \
    $(wildcard include/config/X86_VMX_FEATURE_NAMES) \
    $(wildcard include/config/STACKPROTECTOR) \
    $(wildcard include/config/X86_DEBUGCTLMSR) \
    $(wildcard include/config/CPU_SUP_AMD) \
    $(wildcard include/config/XEN) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/processor-flags.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/processor-flags.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/math_emu.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/ptrace.h \
    $(wildcard include/config/PARAVIRT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/segment.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/ptrace.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/ptrace-abi.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/paravirt_types.h \
    $(wildcard include/config/PGTABLE_LEVELS) \
    $(wildcard include/config/ZERO_CALL_USED_REGS) \
    $(wildcard include/config/PARAVIRT_DEBUG) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/desc_defs.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pgtable_types.h \
    $(wildcard include/config/HAVE_ARCH_USERFAULTFD_WP) \
    $(wildcard include/config/PROC_FS) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pgtable_64_types.h \
    $(wildcard include/config/DEBUG_KMAP_LOCAL_FORCE_MAP) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/sparsemem.h \
    $(wildcard include/config/NUMA_KEEP_MEMINFO) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/nospec-branch.h \
    $(wildcard include/config/CALL_THUNKS_DEBUG) \
    $(wildcard include/config/CPU_SRSO) \
    $(wildcard include/config/CPU_IBPB_ENTRY) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/static_key.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/jump_label.h \
    $(wildcard include/config/JUMP_LABEL) \
    $(wildcard include/config/HAVE_ARCH_JUMP_LABEL_RELATIVE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/jump_label.h \
    $(wildcard include/config/HAVE_JUMP_LABEL_HACK) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/msr-index.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/unwind_hints.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/orc_types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/asm-offsets.h \
  include/generated/asm-offsets.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/GEN-for-each-reg.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/proto.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/ldt.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/sigcontext.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cpuid.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/string.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/string_64.h \
    $(wildcard include/config/ARCH_HAS_UACCESS_FLUSHCACHE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/msr.h \
    $(wildcard include/config/TRACEPOINTS) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cpumask.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cpumask.h \
    $(wildcard include/config/FORCE_NR_CPUS) \
    $(wildcard include/config/HOTPLUG_CPU) \
    $(wildcard include/config/DEBUG_PER_CPU_MAPS) \
    $(wildcard include/config/CPUMASK_OFFSTACK) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bitmap.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/find.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/string.h \
    $(wildcard include/config/BINARY_PRINTF) \
    $(wildcard include/config/FORTIFY_SOURCE) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/string.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/atomic.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/atomic.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cmpxchg.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cmpxchg_64.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/atomic64_64.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/atomic/atomic-arch-fallback.h \
    $(wildcard include/config/GENERIC_ATOMIC64) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/atomic/atomic-long.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/atomic/atomic-instrumented.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/numa.h \
    $(wildcard include/config/NODES_SHIFT) \
    $(wildcard include/config/HAVE_ARCH_NODE_DEV_GROUP) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/msr.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/shared/msr.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/tracepoint-defs.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/special_insns.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/irqflags.h \
    $(wildcard include/config/TRACE_IRQFLAGS) \
    $(wildcard include/config/IRQSOFF_TRACER) \
    $(wildcard include/config/PREEMPT_TRACER) \
    $(wildcard include/config/DEBUG_IRQFLAGS) \
    $(wildcard include/config/TRACE_IRQFLAGS_SUPPORT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/irqflags.h \
    $(wildcard include/config/DEBUG_ENTRY) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/fpu/types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/vmxfeatures.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/vdso/processor.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/shstk.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/personality.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/personality.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/err.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bottom_half.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/lockdep.h \
    $(wildcard include/config/DEBUG_LOCKING_API_SELFTESTS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/smp.h \
    $(wildcard include/config/UP_LATE_INIT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/smp_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/llist.h \
    $(wildcard include/config/ARCH_HAVE_NMI_SAFE_CMPXCHG) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/smp.h \
    $(wildcard include/config/DEBUG_NMI_SELFTEST) \
  arch/x86/include/generated/asm/mmiowb.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/mmiowb.h \
    $(wildcard include/config/MMIOWB) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/spinlock_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rwlock_types.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/spinlock.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/paravirt.h \
    $(wildcard include/config/PARAVIRT_SPINLOCKS) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/frame.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/qspinlock.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/qspinlock.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/qrwlock.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/qrwlock.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rwlock.h \
    $(wildcard include/config/PREEMPT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/spinlock_api_smp.h \
    $(wildcard include/config/INLINE_SPIN_LOCK) \
    $(wildcard include/config/INLINE_SPIN_LOCK_BH) \
    $(wildcard include/config/INLINE_SPIN_LOCK_IRQ) \
    $(wildcard include/config/INLINE_SPIN_LOCK_IRQSAVE) \
    $(wildcard include/config/INLINE_SPIN_TRYLOCK) \
    $(wildcard include/config/INLINE_SPIN_TRYLOCK_BH) \
    $(wildcard include/config/UNINLINE_SPIN_UNLOCK) \
    $(wildcard include/config/INLINE_SPIN_UNLOCK_BH) \
    $(wildcard include/config/INLINE_SPIN_UNLOCK_IRQ) \
    $(wildcard include/config/INLINE_SPIN_UNLOCK_IRQRESTORE) \
    $(wildcard include/config/GENERIC_LOCKBREAK) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rwlock_api_smp.h \
    $(wildcard include/config/INLINE_READ_LOCK) \
    $(wildcard include/config/INLINE_WRITE_LOCK) \
    $(wildcard include/config/INLINE_READ_LOCK_BH) \
    $(wildcard include/config/INLINE_WRITE_LOCK_BH) \
    $(wildcard include/config/INLINE_READ_LOCK_IRQ) \
    $(wildcard include/config/INLINE_WRITE_LOCK_IRQ) \
    $(wildcard include/config/INLINE_READ_LOCK_IRQSAVE) \
    $(wildcard include/config/INLINE_WRITE_LOCK_IRQSAVE) \
    $(wildcard include/config/INLINE_READ_TRYLOCK) \
    $(wildcard include/config/INLINE_WRITE_TRYLOCK) \
    $(wildcard include/config/INLINE_READ_UNLOCK) \
    $(wildcard include/config/INLINE_WRITE_UNLOCK) \
    $(wildcard include/config/INLINE_READ_UNLOCK_BH) \
    $(wildcard include/config/INLINE_WRITE_UNLOCK_BH) \
    $(wildcard include/config/INLINE_READ_UNLOCK_IRQ) \
    $(wildcard include/config/INLINE_WRITE_UNLOCK_IRQ) \
    $(wildcard include/config/INLINE_READ_UNLOCK_IRQRESTORE) \
    $(wildcard include/config/INLINE_WRITE_UNLOCK_IRQRESTORE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/list_nulls.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/wait.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/wait.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/seqlock.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mutex.h \
    $(wildcard include/config/MUTEX_SPIN_ON_OWNER) \
    $(wildcard include/config/DEBUG_MUTEXES) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/osq_lock.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/debug_locks.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/nodemask.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/random.h \
    $(wildcard include/config/VMGENID) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/random.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/irqnr.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/irqnr.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/prandom.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/once.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/percpu.h \
    $(wildcard include/config/MODULES) \
    $(wildcard include/config/RANDOM_KMALLOC_CACHES) \
    $(wildcard include/config/NEED_PER_CPU_PAGE_FIRST_CHUNK) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pageblock-flags.h \
    $(wildcard include/config/HUGETLB_PAGE) \
    $(wildcard include/config/HUGETLB_PAGE_SIZE_VARIABLE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page-flags-layout.h \
  include/generated/bounds.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mm_types.h \
    $(wildcard include/config/HAVE_ALIGNED_STRUCT_PAGE) \
    $(wildcard include/config/USERFAULTFD) \
    $(wildcard include/config/HAVE_ARCH_COMPAT_MMAP_BASES) \
    $(wildcard include/config/MEMBARRIER) \
    $(wildcard include/config/AIO) \
    $(wildcard include/config/MMU_NOTIFIER) \
    $(wildcard include/config/ARCH_WANT_BATCHED_UNMAP_TLB_FLUSH) \
    $(wildcard include/config/IOMMU_SVA) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mm_types_task.h \
    $(wildcard include/config/SPLIT_PTLOCK_CPUS) \
    $(wildcard include/config/ARCH_ENABLE_SPLIT_PMD_PTLOCK) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/tlbbatch.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/auxvec.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/auxvec.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/auxvec.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kref.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/refcount.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rbtree.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rbtree_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rcupdate.h \
    $(wildcard include/config/PREEMPT_RCU) \
    $(wildcard include/config/TINY_RCU) \
    $(wildcard include/config/RCU_STRICT_GRACE_PERIOD) \
    $(wildcard include/config/RCU_LAZY) \
    $(wildcard include/config/TASKS_RCU_GENERIC) \
    $(wildcard include/config/RCU_STALL_COMMON) \
    $(wildcard include/config/NO_HZ_FULL) \
    $(wildcard include/config/KVM_XFER_TO_GUEST_WORK) \
    $(wildcard include/config/RCU_NOCB_CPU) \
    $(wildcard include/config/TASKS_RCU) \
    $(wildcard include/config/TASKS_TRACE_RCU) \
    $(wildcard include/config/TASKS_RUDE_RCU) \
    $(wildcard include/config/TREE_RCU) \
    $(wildcard include/config/DEBUG_OBJECTS_RCU_HEAD) \
    $(wildcard include/config/PROVE_RCU) \
    $(wildcard include/config/ARCH_WEAK_RELEASE_ACQUIRE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/context_tracking_irq.h \
    $(wildcard include/config/CONTEXT_TRACKING_IDLE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rcutree.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/maple_tree.h \
    $(wildcard include/config/MAPLE_RCU_DISABLED) \
    $(wildcard include/config/DEBUG_MAPLE_TREE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rwsem.h \
    $(wildcard include/config/RWSEM_SPIN_ON_OWNER) \
    $(wildcard include/config/DEBUG_RWSEMS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/completion.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/swait.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/uprobes.h \
    $(wildcard include/config/UPROBES) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/uprobes.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/notifier.h \
    $(wildcard include/config/TREE_SRCU) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/srcu.h \
    $(wildcard include/config/TINY_SRCU) \
    $(wildcard include/config/NEED_SRCU_NMI_SAFE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/workqueue.h \
    $(wildcard include/config/DEBUG_OBJECTS_WORK) \
    $(wildcard include/config/FREEZER) \
    $(wildcard include/config/SYSFS) \
    $(wildcard include/config/WQ_WATCHDOG) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/timer.h \
    $(wildcard include/config/DEBUG_OBJECTS_TIMERS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ktime.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/time.h \
    $(wildcard include/config/POSIX_TIMERS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/time32.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/timex.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/timex.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/timex.h \
    $(wildcard include/config/X86_TSC) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/tsc.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/time32.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/time.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/jiffies.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/jiffies.h \
  include/generated/timeconst.h \
  /tmp/pool-discard-guest-linux-20261030/include/vdso/ktime.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/timekeeping.h \
    $(wildcard include/config/GENERIC_CMOS_UPDATE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/clocksource_ids.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/debugobjects.h \
    $(wildcard include/config/DEBUG_OBJECTS) \
    $(wildcard include/config/DEBUG_OBJECTS_FREE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rcu_segcblist.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/srcutree.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rcu_node_tree.h \
    $(wildcard include/config/RCU_FANOUT) \
    $(wildcard include/config/RCU_FANOUT_LEAF) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/percpu_counter.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/mmu.h \
    $(wildcard include/config/MODIFY_LDT_SYSCALL) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page-flags.h \
    $(wildcard include/config/ARCH_USES_PG_UNCACHED) \
    $(wildcard include/config/PAGE_IDLE_FLAG) \
    $(wildcard include/config/ARCH_USES_PG_ARCH_X) \
    $(wildcard include/config/HUGETLB_PAGE_OPTIMIZE_VMEMMAP) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/local_lock.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/local_lock_internal.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/memory_hotplug.h \
    $(wildcard include/config/HAVE_ARCH_NODEDATA_EXTENSION) \
    $(wildcard include/config/ARCH_HAS_ADD_PAGES) \
    $(wildcard include/config/MEMORY_HOTREMOVE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/mmzone.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/mmzone_64.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/topology.h \
    $(wildcard include/config/USE_PERCPU_NUMA_NODE_ID) \
    $(wildcard include/config/SCHED_SMT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/arch_topology.h \
    $(wildcard include/config/ACPI_CPPC_LIB) \
    $(wildcard include/config/GENERIC_ARCH_TOPOLOGY) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/topology.h \
    $(wildcard include/config/SCHED_MC_PRIO) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/mpspec.h \
    $(wildcard include/config/EISA) \
    $(wildcard include/config/X86_LOCAL_APIC) \
    $(wildcard include/config/X86_MPPARSE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/mpspec_def.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/x86_init.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/apicdef.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/topology.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cpu_smt.h \
    $(wildcard include/config/HOTPLUG_SMT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mmap_lock.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/percpu-refcount.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bit_spinlock.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/shrinker.h \
    $(wildcard include/config/SHRINKER_DEBUG) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/resource.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/resource.h \
  arch/x86/include/generated/uapi/asm/resource.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/resource.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/resource.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page_ext.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stacktrace.h \
    $(wildcard include/config/ARCH_STACKWALK) \
    $(wildcard include/config/STACKTRACE) \
    $(wildcard include/config/HAVE_RELIABLE_STACKTRACE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stackdepot.h \
    $(wildcard include/config/STACKDEPOT_ALWAYS_INIT) \
    $(wildcard include/config/STACKDEPOT) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page_ref.h \
    $(wildcard include/config/DEBUG_PAGE_REF) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/overflow.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sizes.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched.h \
    $(wildcard include/config/VIRT_CPU_ACCOUNTING_NATIVE) \
    $(wildcard include/config/SCHED_INFO) \
    $(wildcard include/config/SCHEDSTATS) \
    $(wildcard include/config/SCHED_CORE) \
    $(wildcard include/config/FAIR_GROUP_SCHED) \
    $(wildcard include/config/RT_GROUP_SCHED) \
    $(wildcard include/config/RT_MUTEXES) \
    $(wildcard include/config/UCLAMP_TASK) \
    $(wildcard include/config/UCLAMP_BUCKETS_COUNT) \
    $(wildcard include/config/KMAP_LOCAL) \
    $(wildcard include/config/CGROUP_SCHED) \
    $(wildcard include/config/BLK_DEV_IO_TRACE) \
    $(wildcard include/config/COMPAT_BRK) \
    $(wildcard include/config/CGROUPS) \
    $(wildcard include/config/BLK_CGROUP) \
    $(wildcard include/config/PSI) \
    $(wildcard include/config/PAGE_OWNER) \
    $(wildcard include/config/EVENTFD) \
    $(wildcard include/config/CPU_SUP_INTEL) \
    $(wildcard include/config/TASK_DELAY_ACCT) \
    $(wildcard include/config/ARCH_HAS_SCALED_CPUTIME) \
    $(wildcard include/config/VIRT_CPU_ACCOUNTING_GEN) \
    $(wildcard include/config/POSIX_CPUTIMERS) \
    $(wildcard include/config/POSIX_CPU_TIMERS_TASK_WORK) \
    $(wildcard include/config/KEYS) \
    $(wildcard include/config/SYSVIPC) \
    $(wildcard include/config/DETECT_HUNG_TASK) \
    $(wildcard include/config/IO_URING) \
    $(wildcard include/config/AUDIT) \
    $(wildcard include/config/AUDITSYSCALL) \
    $(wildcard include/config/UBSAN) \
    $(wildcard include/config/UBSAN_TRAP) \
    $(wildcard include/config/TASK_XACCT) \
    $(wildcard include/config/CPUSETS) \
    $(wildcard include/config/X86_CPU_RESCTRL) \
    $(wildcard include/config/FUTEX) \
    $(wildcard include/config/PERF_EVENTS) \
    $(wildcard include/config/RSEQ) \
    $(wildcard include/config/FAULT_INJECTION) \
    $(wildcard include/config/LATENCYTOP) \
    $(wildcard include/config/KUNIT) \
    $(wildcard include/config/FUNCTION_GRAPH_TRACER) \
    $(wildcard include/config/BCACHE) \
    $(wildcard include/config/VMAP_STACK) \
    $(wildcard include/config/LIVEPATCH) \
    $(wildcard include/config/SECURITY) \
    $(wildcard include/config/BPF_SYSCALL) \
    $(wildcard include/config/GCC_PLUGIN_STACKLEAK) \
    $(wildcard include/config/X86_MCE) \
    $(wildcard include/config/KRETPROBES) \
    $(wildcard include/config/RETHOOK) \
    $(wildcard include/config/ARCH_HAS_PARANOID_L1D_FLUSH) \
    $(wildcard include/config/RV) \
    $(wildcard include/config/USER_EVENTS) \
    $(wildcard include/config/ARCH_TASK_STRUCT_ON_STACK) \
    $(wildcard include/config/PREEMPT_NONE) \
    $(wildcard include/config/PREEMPT_VOLUNTARY) \
    $(wildcard include/config/DEBUG_RSEQ) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/sched.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pid.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rculist.h \
    $(wildcard include/config/PROVE_RCU_LIST) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sem.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/sem.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ipc.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/uidgid.h \
    $(wildcard include/config/MULTIUSER) \
    $(wildcard include/config/USER_NS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/highuid.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rhashtable-types.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/ipc.h \
  arch/x86/include/generated/uapi/asm/ipcbuf.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/ipcbuf.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/sembuf.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/shm.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/shm.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/hugetlb_encode.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/shmbuf.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/shmbuf.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/shmparam.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kmsan_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/plist.h \
    $(wildcard include/config/DEBUG_PLIST) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/hrtimer.h \
    $(wildcard include/config/HIGH_RES_TIMERS) \
    $(wildcard include/config/TIME_LOW_RES) \
    $(wildcard include/config/TIMERFD) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/hrtimer_defs.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/timerqueue.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/seccomp.h \
    $(wildcard include/config/SECCOMP) \
    $(wildcard include/config/HAVE_ARCH_SECCOMP_FILTER) \
    $(wildcard include/config/SECCOMP_FILTER) \
    $(wildcard include/config/CHECKPOINT_RESTORE) \
    $(wildcard include/config/SECCOMP_CACHE_DEBUG) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/seccomp.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/seccomp.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/unistd.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/unistd.h \
  arch/x86/include/generated/uapi/asm/unistd_64.h \
  arch/x86/include/generated/asm/unistd_64_x32.h \
  arch/x86/include/generated/asm/unistd_32_ia32.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/ia32_unistd.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/seccomp.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/unistd.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/latencytop.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/prio.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/signal_types.h \
    $(wildcard include/config/OLD_SIGACTION) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/signal.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/signal.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/signal.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/signal-defs.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/siginfo.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/siginfo.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/syscall_user_dispatch.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/task_io_accounting.h \
    $(wildcard include/config/TASK_IO_ACCOUNTING) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/posix-timers.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/alarmtimer.h \
    $(wildcard include/config/RTC_CLASS) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/rseq.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kcsan.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rv.h \
    $(wildcard include/config/RV_REACTORS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/livepatch_sched.h \
  arch/x86/include/generated/asm/kmap_size.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/kmap_size.h \
    $(wildcard include/config/DEBUG_KMAP_LOCAL) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pgtable.h \
    $(wildcard include/config/HIGHPTE) \
    $(wildcard include/config/ARCH_HAS_NONLEAF_PMD_YOUNG) \
    $(wildcard include/config/GUP_GET_PXX_LOW_HIGH) \
    $(wildcard include/config/ARCH_WANT_PMD_MKWRITE) \
    $(wildcard include/config/HAVE_ARCH_TRANSPARENT_HUGEPAGE_PUD) \
    $(wildcard include/config/HAVE_ARCH_SOFT_DIRTY) \
    $(wildcard include/config/ARCH_ENABLE_THP_MIGRATION) \
    $(wildcard include/config/HAVE_ARCH_HUGE_VMAP) \
    $(wildcard include/config/X86_ESPFIX64) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pgtable.h \
    $(wildcard include/config/DEBUG_WX) \
    $(wildcard include/config/PAGE_TABLE_CHECK) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pkru.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/fpu/api.h \
    $(wildcard include/config/X86_DEBUG_FPU) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/coco.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/pgtable_uffd.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page_table_check.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pgtable_64.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/fixmap.h \
    $(wildcard include/config/PROVIDE_OHCI1394_DMA_INIT) \
    $(wildcard include/config/X86_IO_APIC) \
    $(wildcard include/config/PCI_MMCONFIG) \
    $(wildcard include/config/ACPI_APEI_GHES) \
    $(wildcard include/config/INTEL_TXT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/vsyscall.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/fixmap.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pgtable-invert.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kasan.h \
    $(wildcard include/config/KASAN_STACK) \
    $(wildcard include/config/KASAN_VMALLOC) \
    $(wildcard include/config/KASAN_INLINE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kasan-enabled.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/memremap.h \
    $(wildcard include/config/DEVICE_PRIVATE) \
    $(wildcard include/config/PCI_P2PDMA) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ioport.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/slab.h \
    $(wildcard include/config/DEBUG_SLAB) \
    $(wildcard include/config/SLUB_TINY) \
    $(wildcard include/config/FAILSLAB) \
    $(wildcard include/config/MEMCG_KMEM) \
    $(wildcard include/config/KFENCE) \
    $(wildcard include/config/SLAB) \
    $(wildcard include/config/SLUB) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/hash.h \
    $(wildcard include/config/HAVE_ARCH_HASH) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/huge_mm.h \
    $(wildcard include/config/READ_ONLY_THP_FOR_FS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/coredump.h \
    $(wildcard include/config/CORE_DUMP_DEFAULT_ELF_HEADERS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/fs.h \
    $(wildcard include/config/FS_POSIX_ACL) \
    $(wildcard include/config/CGROUP_WRITEBACK) \
    $(wildcard include/config/IMA) \
    $(wildcard include/config/FILE_LOCKING) \
    $(wildcard include/config/FSNOTIFY) \
    $(wildcard include/config/FS_ENCRYPTION) \
    $(wildcard include/config/FS_VERITY) \
    $(wildcard include/config/EPOLL) \
    $(wildcard include/config/UNICODE) \
    $(wildcard include/config/QUOTA) \
    $(wildcard include/config/BLOCK) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/wait_bit.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kdev_t.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/kdev_t.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dcache.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rculist_bl.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/list_bl.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/lockref.h \
    $(wildcard include/config/ARCH_USE_CMPXCHG_LOCKREF) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stringhash.h \
    $(wildcard include/config/DCACHE_WORD_ACCESS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/path.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/stat.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/uapi/asm/stat.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/stat.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/list_lru.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/xarray.h \
    $(wildcard include/config/XARRAY_MULTI) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/mm.h \
    $(wildcard include/config/MMU_LAZY_TLB_REFCOUNT) \
    $(wildcard include/config/ARCH_HAS_MEMBARRIER_CALLBACKS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sync_core.h \
    $(wildcard include/config/ARCH_HAS_SYNC_CORE_BEFORE_USERMODE) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/sync_core.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/radix-tree.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/capability.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/capability.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/semaphore.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/fcntl.h \
    $(wildcard include/config/ARCH_32BIT_OFF_T) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/fcntl.h \
  arch/x86/include/generated/uapi/asm/fcntl.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/asm-generic/fcntl.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/openat2.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/migrate_mode.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/percpu-rwsem.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rcuwait.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/signal.h \
    $(wildcard include/config/SCHED_AUTOGROUP) \
    $(wildcard include/config/BSD_PROCESS_ACCT) \
    $(wildcard include/config/TASKSTATS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/signal.h \
    $(wildcard include/config/DYNAMIC_SIGFRAME) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/jobctl.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/task.h \
    $(wildcard include/config/HAVE_EXIT_THREAD) \
    $(wildcard include/config/ARCH_WANTS_DYNAMIC_TASK_STRUCT) \
    $(wildcard include/config/HAVE_ARCH_THREAD_STRUCT_WHITELIST) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/uaccess.h \
    $(wildcard include/config/ARCH_HAS_SUBPAGE_FAULTS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/fault-inject-usercopy.h \
    $(wildcard include/config/FAULT_INJECTION_USERCOPY) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/uaccess.h \
    $(wildcard include/config/CC_HAS_ASM_GOTO_OUTPUT) \
    $(wildcard include/config/CC_HAS_ASM_GOTO_TIED_OUTPUT) \
    $(wildcard include/config/ARCH_HAS_COPY_MC) \
    $(wildcard include/config/X86_INTEL_USERCOPY) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/smap.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/extable.h \
    $(wildcard include/config/BPF_JIT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/tlbflush.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mmu_notifier.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/interval_tree.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/invpcid.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pti.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/uaccess_64.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/access_ok.h \
    $(wildcard include/config/ALTERNATE_USER_ADDRESS_SPACE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cred.h \
    $(wildcard include/config/DEBUG_CREDENTIALS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/key.h \
    $(wildcard include/config/KEY_NOTIFICATIONS) \
    $(wildcard include/config/NET) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sysctl.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/sysctl.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/assoc_array.h \
    $(wildcard include/config/ASSOCIATIVE_ARRAY) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/user.h \
    $(wildcard include/config/VFIO_PCI_ZDEV_KVM) \
    $(wildcard include/config/IOMMUFD) \
    $(wildcard include/config/WATCH_QUEUE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ratelimit.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rcu_sync.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/delayed_call.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/uuid.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/errseq.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ioprio.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/rt.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/iocontext.h \
    $(wildcard include/config/BLK_ICQ) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/ioprio.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/fs_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mount.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/mnt_idmapping.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/fs.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/quota.h \
    $(wildcard include/config/QUOTA_NETLINK_INTERFACE) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/dqblk_xfs.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dqblk_v1.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dqblk_v2.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dqblk_qtree.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/projid.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/quota.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/vmstat.h \
    $(wildcard include/config/VM_EVENT_COUNTERS) \
    $(wildcard include/config/DEBUG_TLBFLUSH) \
    $(wildcard include/config/PER_VMA_LOCK_STATS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/vm_event_item.h \
    $(wildcard include/config/MEMORY_BALLOON) \
    $(wildcard include/config/BALLOON_COMPACTION) \
    $(wildcard include/config/ZSWAP) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page_reporting.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/scatterlist.h \
    $(wildcard include/config/NEED_SG_DMA_LENGTH) \
    $(wildcard include/config/NEED_SG_DMA_FLAGS) \
    $(wildcard include/config/DEBUG_SG) \
    $(wildcard include/config/SGL_ALLOC) \
    $(wildcard include/config/ARCH_NO_SG_CHAIN) \
    $(wildcard include/config/SG_POOL) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/io.h \
    $(wildcard include/config/MTRR) \
    $(wildcard include/config/X86_PAT) \
  arch/x86/include/generated/asm/early_ioremap.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/early_ioremap.h \
    $(wildcard include/config/GENERIC_EARLY_IOREMAP) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/shared/io.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/io.h \
    $(wildcard include/config/GENERIC_IOMAP) \
    $(wildcard include/config/TRACE_MMIO_ACCESS) \
    $(wildcard include/config/GENERIC_IOREMAP) \
    $(wildcard include/config/HAS_IOPORT_MAP) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/iomap.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/pci_iomap.h \
    $(wildcard include/config/PCI) \
    $(wildcard include/config/NO_GENERIC_PCI_IOPORT_MAP) \
    $(wildcard include/config/GENERIC_PCI_IOMAP) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/logic_pio.h \
    $(wildcard include/config/INDIRECT_PIO) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/fwnode.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/vmalloc.h \
    $(wildcard include/config/HAVE_ARCH_HUGE_VMALLOC) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/vmalloc.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/pgtable_areas.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/module.h \
    $(wildcard include/config/MODULES_TREE_LOOKUP) \
    $(wildcard include/config/STACKTRACE_BUILD_ID) \
    $(wildcard include/config/ARCH_USES_CFI_TRAPS) \
    $(wildcard include/config/MODULE_SIG) \
    $(wildcard include/config/KALLSYMS) \
    $(wildcard include/config/BPF_EVENTS) \
    $(wildcard include/config/DEBUG_INFO_BTF_MODULES) \
    $(wildcard include/config/EVENT_TRACING) \
    $(wildcard include/config/MODULE_UNLOAD) \
    $(wildcard include/config/CONSTRUCTORS) \
    $(wildcard include/config/FUNCTION_ERROR_INJECTION) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/buildid.h \
    $(wildcard include/config/CRASH_CORE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kmod.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/umh.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/elf.h \
    $(wildcard include/config/ARCH_USE_GNU_PROPERTY) \
    $(wildcard include/config/ARCH_HAVE_ELF_PROT) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/elf.h \
    $(wildcard include/config/X86_X32_ABI) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/user.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/user_64.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/fsgsbase.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/vdso.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/elf.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/elf-em.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kobject.h \
    $(wildcard include/config/UEVENT_HELPER) \
    $(wildcard include/config/DEBUG_KOBJECT_RELEASE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sysfs.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kernfs.h \
    $(wildcard include/config/KERNFS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/idr.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kobject_ns.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/moduleparam.h \
    $(wildcard include/config/ALPHA) \
    $(wildcard include/config/PPC64) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rbtree_latch.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/error-injection.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/error-injection.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dynamic_debug.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/module.h \
    $(wildcard include/config/UNWINDER_ORC) \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/module.h \
    $(wildcard include/config/HAVE_MOD_ARCH_SPECIFIC) \
    $(wildcard include/config/MODULES_USE_ELF_REL) \
    $(wildcard include/config/MODULES_USE_ELF_RELA) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/delay.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/delay.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/delay.h \
  /tmp/pool-discard-guest-linux-20261030/mm/page_reporting.h \
    $(wildcard include/config/PAGE_REPORTING) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page-isolation.h \
  /tmp/pool-discard-guest-linux-20261030/mm/internal.h \
    $(wildcard include/config/DEBUG_MEMORY_INIT) \
    $(wildcard include/config/HAVE_FAST_GUP) \
    $(wildcard include/config/DEBUG_VM_MAPLE_TREE) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pagemap.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/highmem.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cacheflush.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/cacheflush.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/cacheflush.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kmsan.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dma-direction.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/hardirq.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/context_tracking_state.h \
    $(wildcard include/config/CONTEXT_TRACKING_USER) \
    $(wildcard include/config/CONTEXT_TRACKING) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ftrace_irq.h \
    $(wildcard include/config/HWLAT_TRACER) \
    $(wildcard include/config/OSNOISE_TRACER) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/vtime.h \
    $(wildcard include/config/VIRT_CPU_ACCOUNTING) \
    $(wildcard include/config/IRQ_TIME_ACCOUNTING) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/hardirq.h \
    $(wildcard include/config/KVM_INTEL) \
    $(wildcard include/config/HAVE_KVM) \
    $(wildcard include/config/X86_THERMAL_VECTOR) \
    $(wildcard include/config/X86_MCE_THRESHOLD) \
    $(wildcard include/config/X86_MCE_AMD) \
    $(wildcard include/config/X86_HV_CALLBACK_VECTOR) \
    $(wildcard include/config/HYPERV) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/highmem-internal.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/hugetlb_inline.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/rmap.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/memcontrol.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cgroup.h \
    $(wildcard include/config/DEBUG_CGROUP_REF) \
    $(wildcard include/config/CGROUP_CPUACCT) \
    $(wildcard include/config/SOCK_CGROUP_DATA) \
    $(wildcard include/config/CGROUP_DATA) \
    $(wildcard include/config/CGROUP_BPF) \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/cgroupstats.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/taskstats.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/seq_file.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/string_helpers.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ctype.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/string_choices.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/ns_common.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/nsproxy.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/user_namespace.h \
    $(wildcard include/config/INOTIFY_USER) \
    $(wildcard include/config/FANOTIFY) \
    $(wildcard include/config/PERSISTENT_KEYRINGS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kernel_stat.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/interrupt.h \
    $(wildcard include/config/IRQ_FORCED_THREADING) \
    $(wildcard include/config/GENERIC_IRQ_PROBE) \
    $(wildcard include/config/IRQ_TIMINGS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/irqreturn.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/irq.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/irq_vectors.h \
    $(wildcard include/config/PCI_MSI) \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/sections.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/sections.h \
    $(wildcard include/config/HAVE_FUNCTION_DESCRIPTORS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cgroup-defs.h \
    $(wildcard include/config/CGROUP_NET_CLASSID) \
    $(wildcard include/config/CGROUP_NET_PRIO) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/u64_stats_sync.h \
  arch/x86/include/generated/asm/local64.h \
  /tmp/pool-discard-guest-linux-20261030/include/asm-generic/local64.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/local.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bpf-cgroup-defs.h \
    $(wildcard include/config/BPF_LSM) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/psi_types.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/kthread.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cgroup_subsys.h \
    $(wildcard include/config/CGROUP_DEVICE) \
    $(wildcard include/config/CGROUP_FREEZER) \
    $(wildcard include/config/CGROUP_PERF) \
    $(wildcard include/config/CGROUP_HUGETLB) \
    $(wildcard include/config/CGROUP_PIDS) \
    $(wildcard include/config/CGROUP_RDMA) \
    $(wildcard include/config/CGROUP_MISC) \
    $(wildcard include/config/CGROUP_DEBUG) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/cgroup_refcnt.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/page_counter.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/vmpressure.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/eventfd.h \
  /tmp/pool-discard-guest-linux-20261030/include/uapi/linux/eventfd.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/writeback.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/flex_proportions.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/backing-dev-defs.h \
    $(wildcard include/config/DEBUG_FS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/blk_types.h \
    $(wildcard include/config/FAIL_MAKE_REQUEST) \
    $(wildcard include/config/BLK_CGROUP_IOCOST) \
    $(wildcard include/config/BLK_INLINE_ENCRYPTION) \
    $(wildcard include/config/BLK_DEV_INTEGRITY) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/bvec.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/device.h \
    $(wildcard include/config/HAS_IOMEM) \
    $(wildcard include/config/GENERIC_MSI_IRQ) \
    $(wildcard include/config/ENERGY_MODEL) \
    $(wildcard include/config/PINCTRL) \
    $(wildcard include/config/DMA_OPS) \
    $(wildcard include/config/DMA_DECLARE_COHERENT) \
    $(wildcard include/config/DMA_CMA) \
    $(wildcard include/config/SWIOTLB) \
    $(wildcard include/config/SWIOTLB_DYNAMIC) \
    $(wildcard include/config/ARCH_HAS_SYNC_DMA_FOR_DEVICE) \
    $(wildcard include/config/ARCH_HAS_SYNC_DMA_FOR_CPU) \
    $(wildcard include/config/ARCH_HAS_SYNC_DMA_FOR_CPU_ALL) \
    $(wildcard include/config/DMA_OPS_BYPASS) \
    $(wildcard include/config/PM_SLEEP) \
    $(wildcard include/config/OF) \
    $(wildcard include/config/DEVTMPFS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/dev_printk.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/energy_model.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/cpufreq.h \
    $(wildcard include/config/CPU_FREQ) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/topology.h \
    $(wildcard include/config/SCHED_DEBUG) \
    $(wildcard include/config/SCHED_CLUSTER) \
    $(wildcard include/config/SCHED_MC) \
    $(wildcard include/config/CPU_FREQ_GOV_SCHEDUTIL) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/idle.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/sched/sd_flags.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/klist.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pm.h \
    $(wildcard include/config/VT_CONSOLE_SLEEP) \
    $(wildcard include/config/CXL_SUSPEND) \
    $(wildcard include/config/PM) \
    $(wildcard include/config/PM_CLK) \
    $(wildcard include/config/PM_GENERIC_DOMAINS) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/device/bus.h \
    $(wildcard include/config/ACPI) \
  /tmp/pool-discard-guest-linux-20261030/include/linux/device/class.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/device/driver.h \
  /tmp/pool-discard-guest-linux-20261030/arch/x86/include/asm/device.h \
  /tmp/pool-discard-guest-linux-20261030/include/linux/pm_wakeup.h \

mm/page_reporting.o: $(deps_mm/page_reporting.o)

$(deps_mm/page_reporting.o):
