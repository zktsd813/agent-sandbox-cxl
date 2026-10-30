savedcmd_/tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o := gcc -Wp,-MMD,/tmp/pool-memory-module-build-20261030/.pool_memory_module.mod.o.d -nostdinc -I/tmp/pool-handoff-linux-20261030/arch/x86/include -I./arch/x86/include/generated -I/tmp/pool-handoff-linux-20261030/include -I./include -I/tmp/pool-handoff-linux-20261030/arch/x86/include/uapi -I./arch/x86/include/generated/uapi -I/tmp/pool-handoff-linux-20261030/include/uapi -I./include/generated/uapi -include /tmp/pool-handoff-linux-20261030/include/linux/compiler-version.h -include /tmp/pool-handoff-linux-20261030/include/linux/kconfig.h -include /tmp/pool-handoff-linux-20261030/include/linux/compiler_types.h -D__KERNEL__ -fmacro-prefix-map=/tmp/pool-handoff-linux-20261030/= -Werror -std=gnu11 -fshort-wchar -funsigned-char -fno-common -fno-PIE -fno-strict-aliasing -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -fcf-protection=branch -fno-jump-tables -m64 -falign-jumps=1 -falign-loops=1 -mno-80387 -mno-fp-ret-in-387 -mpreferred-stack-boundary=3 -mskip-rax-setup -mtune=generic -mno-red-zone -mcmodel=kernel -Wno-sign-compare -fno-asynchronous-unwind-tables -mindirect-branch=thunk-extern -mindirect-branch-register -mindirect-branch-cs-prefix -mfunction-return=thunk-extern -fno-jump-tables -fpatchable-function-entry=16,16 -fno-delete-null-pointer-checks -O2 -fno-allow-store-data-races -fstack-protector-strong -fomit-frame-pointer -fno-stack-clash-protection -falign-functions=16 -fno-strict-overflow -fno-stack-check -fconserve-stack -Wall -Wundef -Werror=implicit-function-declaration -Werror=implicit-int -Werror=return-type -Werror=strict-prototypes -Wno-format-security -Wno-trigraphs -Wno-frame-address -Wno-address-of-packed-member -Wframe-larger-than=2048 -Wno-main -Wno-unused-but-set-variable -Wno-unused-const-variable -Wvla -Wno-pointer-sign -Wcast-function-type -Wno-array-bounds -Wno-alloc-size-larger-than -Wimplicit-fallthrough=5 -Werror=date-time -Werror=incompatible-pointer-types -Werror=designated-init -Wenum-conversion -Wno-unused-but-set-variable -Wno-unused-const-variable -Wno-restrict -Wno-packed-not-aligned -Wno-format-overflow -Wno-format-truncation -Wno-stringop-overflow -Wno-stringop-truncation -Wno-missing-field-initializers -Wno-type-limits -Wno-shift-negative-value -Wno-maybe-uninitialized -Wno-sign-compare -DMODULE -DKBUILD_BASENAME='"pool_memory_module.mod"' -DKBUILD_MODNAME='"pool_memory_module"' -D__KBUILD_MODNAME=kmod_pool_memory_module -c -o /tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o /tmp/pool-memory-module-build-20261030/pool_memory_module.mod.c

source_/tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o := /tmp/pool-memory-module-build-20261030/pool_memory_module.mod.c

deps_/tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o := \
    $(wildcard include/config/UNWINDER_ORC) \
    $(wildcard include/config/MODULE_UNLOAD) \
    $(wildcard include/config/RETPOLINE) \
  /tmp/pool-handoff-linux-20261030/include/linux/compiler-version.h \
    $(wildcard include/config/CC_VERSION_TEXT) \
  /tmp/pool-handoff-linux-20261030/include/linux/kconfig.h \
    $(wildcard include/config/CPU_BIG_ENDIAN) \
    $(wildcard include/config/BOOGER) \
    $(wildcard include/config/FOO) \
  /tmp/pool-handoff-linux-20261030/include/linux/compiler_types.h \
    $(wildcard include/config/DEBUG_INFO_BTF) \
    $(wildcard include/config/PAHOLE_HAS_BTF_TAG) \
    $(wildcard include/config/FUNCTION_ALIGNMENT) \
    $(wildcard include/config/CC_IS_GCC) \
    $(wildcard include/config/X86_64) \
    $(wildcard include/config/ARM64) \
    $(wildcard include/config/HAVE_ARCH_COMPILER_H) \
    $(wildcard include/config/CC_HAS_ASM_INLINE) \
  /tmp/pool-handoff-linux-20261030/include/linux/compiler_attributes.h \
  /tmp/pool-handoff-linux-20261030/include/linux/compiler-gcc.h \
    $(wildcard include/config/ARCH_USE_BUILTIN_BSWAP) \
    $(wildcard include/config/SHADOW_CALL_STACK) \
    $(wildcard include/config/KCOV) \
  /tmp/pool-handoff-linux-20261030/include/linux/module.h \
    $(wildcard include/config/MODULES) \
    $(wildcard include/config/SYSFS) \
    $(wildcard include/config/MODULES_TREE_LOOKUP) \
    $(wildcard include/config/LIVEPATCH) \
    $(wildcard include/config/STACKTRACE_BUILD_ID) \
    $(wildcard include/config/ARCH_USES_CFI_TRAPS) \
    $(wildcard include/config/MODULE_SIG) \
    $(wildcard include/config/GENERIC_BUG) \
    $(wildcard include/config/KALLSYMS) \
    $(wildcard include/config/SMP) \
    $(wildcard include/config/TRACEPOINTS) \
    $(wildcard include/config/TREE_SRCU) \
    $(wildcard include/config/BPF_EVENTS) \
    $(wildcard include/config/DEBUG_INFO_BTF_MODULES) \
    $(wildcard include/config/JUMP_LABEL) \
    $(wildcard include/config/TRACING) \
    $(wildcard include/config/EVENT_TRACING) \
    $(wildcard include/config/FTRACE_MCOUNT_RECORD) \
    $(wildcard include/config/KPROBES) \
    $(wildcard include/config/HAVE_STATIC_CALL_INLINE) \
    $(wildcard include/config/KUNIT) \
    $(wildcard include/config/PRINTK_INDEX) \
    $(wildcard include/config/CONSTRUCTORS) \
    $(wildcard include/config/FUNCTION_ERROR_INJECTION) \
    $(wildcard include/config/DYNAMIC_DEBUG_CORE) \
  /tmp/pool-handoff-linux-20261030/include/linux/list.h \
    $(wildcard include/config/LIST_HARDENED) \
    $(wildcard include/config/DEBUG_LIST) \
  /tmp/pool-handoff-linux-20261030/include/linux/container_of.h \
  /tmp/pool-handoff-linux-20261030/include/linux/build_bug.h \
  /tmp/pool-handoff-linux-20261030/include/linux/compiler.h \
    $(wildcard include/config/TRACE_BRANCH_PROFILING) \
    $(wildcard include/config/PROFILE_ALL_BRANCHES) \
    $(wildcard include/config/OBJTOOL) \
  arch/x86/include/generated/asm/rwonce.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/rwonce.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kasan-checks.h \
    $(wildcard include/config/KASAN_GENERIC) \
    $(wildcard include/config/KASAN_SW_TAGS) \
  /tmp/pool-handoff-linux-20261030/include/linux/types.h \
    $(wildcard include/config/HAVE_UID16) \
    $(wildcard include/config/UID16) \
    $(wildcard include/config/ARCH_DMA_ADDR_T_64BIT) \
    $(wildcard include/config/PHYS_ADDR_T_64BIT) \
    $(wildcard include/config/64BIT) \
    $(wildcard include/config/ARCH_32BIT_USTAT_F_TINODE) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/types.h \
  arch/x86/include/generated/uapi/asm/types.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/types.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/int-ll64.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/int-ll64.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/bitsperlong.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitsperlong.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/bitsperlong.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/posix_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/stddef.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/stddef.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/posix_types.h \
    $(wildcard include/config/X86_32) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/posix_types_64.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/posix_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kcsan-checks.h \
    $(wildcard include/config/KCSAN) \
    $(wildcard include/config/KCSAN_WEAK_MEMORY) \
    $(wildcard include/config/KCSAN_IGNORE_ATOMICS) \
  /tmp/pool-handoff-linux-20261030/include/linux/poison.h \
    $(wildcard include/config/ILLEGAL_POINTER_VALUE) \
  /tmp/pool-handoff-linux-20261030/include/linux/const.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/const.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/const.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/barrier.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/alternative.h \
    $(wildcard include/config/CALL_THUNKS) \
  /tmp/pool-handoff-linux-20261030/include/linux/stringify.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/asm.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/extable_fixup_types.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/nops.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/barrier.h \
  /tmp/pool-handoff-linux-20261030/include/linux/stat.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/stat.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/stat.h \
  /tmp/pool-handoff-linux-20261030/include/linux/time.h \
    $(wildcard include/config/POSIX_TIMERS) \
  /tmp/pool-handoff-linux-20261030/include/linux/cache.h \
    $(wildcard include/config/ARCH_HAS_CACHE_LINE_SIZE) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/kernel.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/sysinfo.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cache.h \
    $(wildcard include/config/X86_L1_CACHE_SHIFT) \
    $(wildcard include/config/X86_INTERNODE_CACHE_SHIFT) \
    $(wildcard include/config/X86_VSMP) \
  /tmp/pool-handoff-linux-20261030/include/linux/linkage.h \
    $(wildcard include/config/ARCH_USE_SYM_ANNOTATIONS) \
  /tmp/pool-handoff-linux-20261030/include/linux/export.h \
    $(wildcard include/config/MODVERSIONS) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/linkage.h \
    $(wildcard include/config/CALL_PADDING) \
    $(wildcard include/config/RETHUNK) \
    $(wildcard include/config/SLS) \
    $(wildcard include/config/FUNCTION_PADDING_BYTES) \
    $(wildcard include/config/UML) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/ibt.h \
    $(wildcard include/config/X86_KERNEL_IBT) \
  /tmp/pool-handoff-linux-20261030/include/linux/math64.h \
    $(wildcard include/config/ARCH_SUPPORTS_INT128) \
  /tmp/pool-handoff-linux-20261030/include/linux/math.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/div64.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/div64.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/math64.h \
  /tmp/pool-handoff-linux-20261030/include/linux/time64.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/time64.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/time.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/time_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/time32.h \
  /tmp/pool-handoff-linux-20261030/include/linux/timex.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/timex.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/param.h \
  arch/x86/include/generated/uapi/asm/param.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/param.h \
    $(wildcard include/config/HZ) \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/param.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/timex.h \
    $(wildcard include/config/X86_TSC) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/processor.h \
    $(wildcard include/config/X86_VMX_FEATURE_NAMES) \
    $(wildcard include/config/X86_IOPL_IOPERM) \
    $(wildcard include/config/STACKPROTECTOR) \
    $(wildcard include/config/VM86) \
    $(wildcard include/config/X86_USER_SHADOW_STACK) \
    $(wildcard include/config/PARAVIRT_XXL) \
    $(wildcard include/config/X86_DEBUGCTLMSR) \
    $(wildcard include/config/CPU_SUP_AMD) \
    $(wildcard include/config/XEN) \
    $(wildcard include/config/X86_SGX) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/processor-flags.h \
    $(wildcard include/config/PAGE_TABLE_ISOLATION) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/processor-flags.h \
  /tmp/pool-handoff-linux-20261030/include/linux/mem_encrypt.h \
    $(wildcard include/config/ARCH_HAS_MEM_ENCRYPT) \
    $(wildcard include/config/AMD_MEM_ENCRYPT) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/mem_encrypt.h \
    $(wildcard include/config/X86_MEM_ENCRYPT) \
  /tmp/pool-handoff-linux-20261030/include/linux/init.h \
    $(wildcard include/config/HAVE_ARCH_PREL32_RELOCATIONS) \
    $(wildcard include/config/STRICT_KERNEL_RWX) \
    $(wildcard include/config/STRICT_MODULE_RWX) \
    $(wildcard include/config/LTO_CLANG) \
  /tmp/pool-handoff-linux-20261030/include/linux/cc_platform.h \
    $(wildcard include/config/ARCH_HAS_CC_PLATFORM) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/bootparam.h \
  /tmp/pool-handoff-linux-20261030/include/linux/screen_info.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/screen_info.h \
  /tmp/pool-handoff-linux-20261030/include/linux/apm_bios.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/apm_bios.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/ioctl.h \
  arch/x86/include/generated/uapi/asm/ioctl.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/ioctl.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/ioctl.h \
  /tmp/pool-handoff-linux-20261030/include/linux/edd.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/edd.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/ist.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/ist.h \
  /tmp/pool-handoff-linux-20261030/include/video/edid.h \
    $(wildcard include/config/X86) \
  /tmp/pool-handoff-linux-20261030/include/uapi/video/edid.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/math_emu.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/ptrace.h \
    $(wildcard include/config/PARAVIRT) \
    $(wildcard include/config/IA32_EMULATION) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/segment.h \
    $(wildcard include/config/XEN_PV) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/page_types.h \
    $(wildcard include/config/PHYSICAL_START) \
    $(wildcard include/config/PHYSICAL_ALIGN) \
    $(wildcard include/config/DYNAMIC_PHYSICAL_MASK) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/page_64_types.h \
    $(wildcard include/config/KASAN) \
    $(wildcard include/config/DYNAMIC_MEMORY_LAYOUT) \
    $(wildcard include/config/X86_5LEVEL) \
    $(wildcard include/config/RANDOMIZE_BASE) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/kaslr.h \
    $(wildcard include/config/RANDOMIZE_MEMORY) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/ptrace.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/ptrace-abi.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/paravirt_types.h \
    $(wildcard include/config/PGTABLE_LEVELS) \
    $(wildcard include/config/ZERO_CALL_USED_REGS) \
    $(wildcard include/config/PARAVIRT_DEBUG) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/desc_defs.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/pgtable_types.h \
    $(wildcard include/config/X86_INTEL_MEMORY_PROTECTION_KEYS) \
    $(wildcard include/config/X86_PAE) \
    $(wildcard include/config/MEM_SOFT_DIRTY) \
    $(wildcard include/config/HAVE_ARCH_USERFAULTFD_WP) \
    $(wildcard include/config/PROC_FS) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/pgtable_64_types.h \
    $(wildcard include/config/KMSAN) \
    $(wildcard include/config/DEBUG_KMAP_LOCAL_FORCE_MAP) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/sparsemem.h \
    $(wildcard include/config/SPARSEMEM) \
    $(wildcard include/config/NUMA_KEEP_MEMINFO) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/nospec-branch.h \
    $(wildcard include/config/CALL_THUNKS_DEBUG) \
    $(wildcard include/config/CALL_DEPTH_TRACKING) \
    $(wildcard include/config/NOINSTR_VALIDATION) \
    $(wildcard include/config/CPU_UNRET_ENTRY) \
    $(wildcard include/config/CPU_SRSO) \
    $(wildcard include/config/CPU_IBPB_ENTRY) \
  /tmp/pool-handoff-linux-20261030/include/linux/static_key.h \
  /tmp/pool-handoff-linux-20261030/include/linux/jump_label.h \
    $(wildcard include/config/HAVE_ARCH_JUMP_LABEL_RELATIVE) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/jump_label.h \
    $(wildcard include/config/HAVE_JUMP_LABEL_HACK) \
  /tmp/pool-handoff-linux-20261030/include/linux/objtool.h \
    $(wildcard include/config/FRAME_POINTER) \
  /tmp/pool-handoff-linux-20261030/include/linux/objtool_types.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cpufeatures.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/required-features.h \
    $(wildcard include/config/X86_MINIMUM_CPU_FAMILY) \
    $(wildcard include/config/MATH_EMULATION) \
    $(wildcard include/config/X86_CMPXCHG64) \
    $(wildcard include/config/X86_CMOV) \
    $(wildcard include/config/X86_P6_NOP) \
    $(wildcard include/config/MATOM) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/disabled-features.h \
    $(wildcard include/config/X86_UMIP) \
    $(wildcard include/config/ADDRESS_MASKING) \
    $(wildcard include/config/INTEL_IOMMU_SVM) \
    $(wildcard include/config/INTEL_TDX_GUEST) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/msr-index.h \
  /tmp/pool-handoff-linux-20261030/include/linux/bits.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/bits.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/unwind_hints.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/orc_types.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/byteorder.h \
  /tmp/pool-handoff-linux-20261030/include/linux/byteorder/little_endian.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/byteorder/little_endian.h \
  /tmp/pool-handoff-linux-20261030/include/linux/swab.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/swab.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/swab.h \
  /tmp/pool-handoff-linux-20261030/include/linux/byteorder/generic.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/percpu.h \
    $(wildcard include/config/X86_64_SMP) \
  /tmp/pool-handoff-linux-20261030/include/linux/kernel.h \
    $(wildcard include/config/PREEMPT_VOLUNTARY_BUILD) \
    $(wildcard include/config/PREEMPT_DYNAMIC) \
    $(wildcard include/config/HAVE_PREEMPT_DYNAMIC_CALL) \
    $(wildcard include/config/HAVE_PREEMPT_DYNAMIC_KEY) \
    $(wildcard include/config/PREEMPT_) \
    $(wildcard include/config/DEBUG_ATOMIC_SLEEP) \
    $(wildcard include/config/MMU) \
    $(wildcard include/config/PROVE_LOCKING) \
  /tmp/pool-handoff-linux-20261030/include/linux/stdarg.h \
  /tmp/pool-handoff-linux-20261030/include/linux/align.h \
  /tmp/pool-handoff-linux-20261030/include/linux/limits.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/limits.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/limits.h \
  /tmp/pool-handoff-linux-20261030/include/linux/bitops.h \
  /tmp/pool-handoff-linux-20261030/include/linux/typecheck.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/generic-non-atomic.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/bitops.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/rmwcc.h \
  /tmp/pool-handoff-linux-20261030/include/linux/args.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/sched.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/arch_hweight.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/const_hweight.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/instrumented-atomic.h \
  /tmp/pool-handoff-linux-20261030/include/linux/instrumented.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kmsan-checks.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/instrumented-non-atomic.h \
    $(wildcard include/config/KCSAN_ASSUME_PLAIN_WRITES_ATOMIC) \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/instrumented-lock.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/le.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bitops/ext2-atomic-setbit.h \
  /tmp/pool-handoff-linux-20261030/include/linux/hex.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kstrtox.h \
  /tmp/pool-handoff-linux-20261030/include/linux/log2.h \
    $(wildcard include/config/ARCH_HAS_ILOG2_U32) \
    $(wildcard include/config/ARCH_HAS_ILOG2_U64) \
  /tmp/pool-handoff-linux-20261030/include/linux/minmax.h \
  /tmp/pool-handoff-linux-20261030/include/linux/panic.h \
    $(wildcard include/config/PANIC_TIMEOUT) \
  /tmp/pool-handoff-linux-20261030/include/linux/printk.h \
    $(wildcard include/config/MESSAGE_LOGLEVEL_DEFAULT) \
    $(wildcard include/config/CONSOLE_LOGLEVEL_DEFAULT) \
    $(wildcard include/config/CONSOLE_LOGLEVEL_QUIET) \
    $(wildcard include/config/EARLY_PRINTK) \
    $(wildcard include/config/PRINTK) \
    $(wildcard include/config/DYNAMIC_DEBUG) \
  /tmp/pool-handoff-linux-20261030/include/linux/kern_levels.h \
  /tmp/pool-handoff-linux-20261030/include/linux/ratelimit_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/spinlock_types_raw.h \
    $(wildcard include/config/DEBUG_SPINLOCK) \
    $(wildcard include/config/DEBUG_LOCK_ALLOC) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/spinlock_types.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/qspinlock_types.h \
    $(wildcard include/config/NR_CPUS) \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/qrwlock_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/lockdep_types.h \
    $(wildcard include/config/PROVE_RAW_LOCK_NESTING) \
    $(wildcard include/config/LOCKDEP) \
    $(wildcard include/config/LOCK_STAT) \
  /tmp/pool-handoff-linux-20261030/include/linux/once_lite.h \
  /tmp/pool-handoff-linux-20261030/include/linux/sprintf.h \
  /tmp/pool-handoff-linux-20261030/include/linux/static_call_types.h \
    $(wildcard include/config/HAVE_STATIC_CALL) \
  /tmp/pool-handoff-linux-20261030/include/linux/instruction_pointer.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/percpu.h \
    $(wildcard include/config/DEBUG_PREEMPT) \
    $(wildcard include/config/HAVE_SETUP_PER_CPU_AREA) \
  /tmp/pool-handoff-linux-20261030/include/linux/threads.h \
    $(wildcard include/config/BASE_SMALL) \
  /tmp/pool-handoff-linux-20261030/include/linux/percpu-defs.h \
    $(wildcard include/config/DEBUG_FORCE_WEAK_PER_CPU) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/current.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/asm-offsets.h \
  include/generated/asm-offsets.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/GEN-for-each-reg.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/proto.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/ldt.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/sigcontext.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cpuid.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/string.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/string_64.h \
    $(wildcard include/config/ARCH_HAS_UACCESS_FLUSHCACHE) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/page.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/page_64.h \
    $(wildcard include/config/DEBUG_VIRTUAL) \
    $(wildcard include/config/X86_VSYSCALL_EMULATION) \
  /tmp/pool-handoff-linux-20261030/include/linux/range.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/memory_model.h \
    $(wildcard include/config/FLATMEM) \
    $(wildcard include/config/SPARSEMEM_VMEMMAP) \
  /tmp/pool-handoff-linux-20261030/include/linux/pfn.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/getorder.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/msr.h \
  arch/x86/include/generated/uapi/asm/errno.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/errno.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/errno-base.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cpumask.h \
  /tmp/pool-handoff-linux-20261030/include/linux/cpumask.h \
    $(wildcard include/config/FORCE_NR_CPUS) \
    $(wildcard include/config/HOTPLUG_CPU) \
    $(wildcard include/config/DEBUG_PER_CPU_MAPS) \
    $(wildcard include/config/CPUMASK_OFFSTACK) \
  /tmp/pool-handoff-linux-20261030/include/linux/bitmap.h \
  /tmp/pool-handoff-linux-20261030/include/linux/find.h \
  /tmp/pool-handoff-linux-20261030/include/linux/string.h \
    $(wildcard include/config/BINARY_PRINTF) \
    $(wildcard include/config/FORTIFY_SOURCE) \
  /tmp/pool-handoff-linux-20261030/include/linux/errno.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/errno.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/string.h \
  /tmp/pool-handoff-linux-20261030/include/linux/atomic.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/atomic.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cmpxchg.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cmpxchg_64.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/atomic64_64.h \
  /tmp/pool-handoff-linux-20261030/include/linux/atomic/atomic-arch-fallback.h \
    $(wildcard include/config/GENERIC_ATOMIC64) \
  /tmp/pool-handoff-linux-20261030/include/linux/atomic/atomic-long.h \
  /tmp/pool-handoff-linux-20261030/include/linux/atomic/atomic-instrumented.h \
  /tmp/pool-handoff-linux-20261030/include/linux/bug.h \
    $(wildcard include/config/BUG_ON_DATA_CORRUPTION) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/bug.h \
    $(wildcard include/config/DEBUG_BUGVERBOSE) \
  /tmp/pool-handoff-linux-20261030/include/linux/instrumentation.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/bug.h \
    $(wildcard include/config/BUG) \
    $(wildcard include/config/GENERIC_BUG_RELATIVE_POINTERS) \
  /tmp/pool-handoff-linux-20261030/include/linux/gfp_types.h \
    $(wildcard include/config/KASAN_HW_TAGS) \
  /tmp/pool-handoff-linux-20261030/include/linux/numa.h \
    $(wildcard include/config/NODES_SHIFT) \
    $(wildcard include/config/NUMA) \
    $(wildcard include/config/HAVE_ARCH_NODE_DEV_GROUP) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/msr.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/shared/msr.h \
  /tmp/pool-handoff-linux-20261030/include/linux/tracepoint-defs.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/special_insns.h \
  /tmp/pool-handoff-linux-20261030/include/linux/irqflags.h \
    $(wildcard include/config/TRACE_IRQFLAGS) \
    $(wildcard include/config/PREEMPT_RT) \
    $(wildcard include/config/IRQSOFF_TRACER) \
    $(wildcard include/config/PREEMPT_TRACER) \
    $(wildcard include/config/DEBUG_IRQFLAGS) \
    $(wildcard include/config/TRACE_IRQFLAGS_SUPPORT) \
  /tmp/pool-handoff-linux-20261030/include/linux/cleanup.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/irqflags.h \
    $(wildcard include/config/DEBUG_ENTRY) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/fpu/types.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/vmxfeatures.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/vdso/processor.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/shstk.h \
  /tmp/pool-handoff-linux-20261030/include/linux/personality.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/personality.h \
  /tmp/pool-handoff-linux-20261030/include/linux/err.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/tsc.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/cpufeature.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/time32.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/time.h \
  /tmp/pool-handoff-linux-20261030/include/linux/uidgid.h \
    $(wildcard include/config/MULTIUSER) \
    $(wildcard include/config/USER_NS) \
  /tmp/pool-handoff-linux-20261030/include/linux/highuid.h \
  /tmp/pool-handoff-linux-20261030/include/linux/buildid.h \
    $(wildcard include/config/CRASH_CORE) \
  /tmp/pool-handoff-linux-20261030/include/linux/mm_types.h \
    $(wildcard include/config/HAVE_ALIGNED_STRUCT_PAGE) \
    $(wildcard include/config/MEMCG) \
    $(wildcard include/config/USERFAULTFD) \
    $(wildcard include/config/PER_VMA_LOCK) \
    $(wildcard include/config/ANON_VMA_NAME) \
    $(wildcard include/config/SWAP) \
    $(wildcard include/config/NUMA_BALANCING) \
    $(wildcard include/config/SCHED_MM_CID) \
    $(wildcard include/config/HAVE_ARCH_COMPAT_MMAP_BASES) \
    $(wildcard include/config/MEMBARRIER) \
    $(wildcard include/config/AIO) \
    $(wildcard include/config/MMU_NOTIFIER) \
    $(wildcard include/config/TRANSPARENT_HUGEPAGE) \
    $(wildcard include/config/ARCH_WANT_BATCHED_UNMAP_TLB_FLUSH) \
    $(wildcard include/config/HUGETLB_PAGE) \
    $(wildcard include/config/IOMMU_SVA) \
    $(wildcard include/config/KSM) \
    $(wildcard include/config/LRU_GEN) \
  /tmp/pool-handoff-linux-20261030/include/linux/mm_types_task.h \
    $(wildcard include/config/SPLIT_PTLOCK_CPUS) \
    $(wildcard include/config/ARCH_ENABLE_SPLIT_PMD_PTLOCK) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/tlbbatch.h \
  /tmp/pool-handoff-linux-20261030/include/linux/auxvec.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/auxvec.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/auxvec.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kref.h \
  /tmp/pool-handoff-linux-20261030/include/linux/spinlock.h \
    $(wildcard include/config/PREEMPTION) \
  /tmp/pool-handoff-linux-20261030/include/linux/preempt.h \
    $(wildcard include/config/PREEMPT_COUNT) \
    $(wildcard include/config/TRACE_PREEMPT_TOGGLE) \
    $(wildcard include/config/PREEMPT_NOTIFIERS) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/preempt.h \
  /tmp/pool-handoff-linux-20261030/include/linux/thread_info.h \
    $(wildcard include/config/THREAD_INFO_IN_TASK) \
    $(wildcard include/config/GENERIC_ENTRY) \
    $(wildcard include/config/HAVE_ARCH_WITHIN_STACK_FRAMES) \
    $(wildcard include/config/HARDENED_USERCOPY) \
    $(wildcard include/config/SH) \
  /tmp/pool-handoff-linux-20261030/include/linux/restart_block.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/thread_info.h \
    $(wildcard include/config/COMPAT) \
  /tmp/pool-handoff-linux-20261030/include/linux/bottom_half.h \
  /tmp/pool-handoff-linux-20261030/include/linux/lockdep.h \
    $(wildcard include/config/DEBUG_LOCKING_API_SELFTESTS) \
  /tmp/pool-handoff-linux-20261030/include/linux/smp.h \
    $(wildcard include/config/UP_LATE_INIT) \
  /tmp/pool-handoff-linux-20261030/include/linux/smp_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/llist.h \
    $(wildcard include/config/ARCH_HAVE_NMI_SAFE_CMPXCHG) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/smp.h \
    $(wildcard include/config/DEBUG_NMI_SELFTEST) \
  arch/x86/include/generated/asm/mmiowb.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/mmiowb.h \
    $(wildcard include/config/MMIOWB) \
  /tmp/pool-handoff-linux-20261030/include/linux/spinlock_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rwlock_types.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/spinlock.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/paravirt.h \
    $(wildcard include/config/PARAVIRT_SPINLOCKS) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/frame.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/qspinlock.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/qspinlock.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/qrwlock.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/qrwlock.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rwlock.h \
    $(wildcard include/config/PREEMPT) \
  /tmp/pool-handoff-linux-20261030/include/linux/spinlock_api_smp.h \
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
  /tmp/pool-handoff-linux-20261030/include/linux/rwlock_api_smp.h \
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
  /tmp/pool-handoff-linux-20261030/include/linux/refcount.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rbtree.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rbtree_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rcupdate.h \
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
  /tmp/pool-handoff-linux-20261030/include/linux/context_tracking_irq.h \
    $(wildcard include/config/CONTEXT_TRACKING_IDLE) \
  /tmp/pool-handoff-linux-20261030/include/linux/rcutree.h \
  /tmp/pool-handoff-linux-20261030/include/linux/maple_tree.h \
    $(wildcard include/config/MAPLE_RCU_DISABLED) \
    $(wildcard include/config/DEBUG_MAPLE_TREE) \
  /tmp/pool-handoff-linux-20261030/include/linux/rwsem.h \
    $(wildcard include/config/RWSEM_SPIN_ON_OWNER) \
    $(wildcard include/config/DEBUG_RWSEMS) \
  /tmp/pool-handoff-linux-20261030/include/linux/osq_lock.h \
  /tmp/pool-handoff-linux-20261030/include/linux/completion.h \
  /tmp/pool-handoff-linux-20261030/include/linux/swait.h \
  /tmp/pool-handoff-linux-20261030/include/linux/wait.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/wait.h \
  /tmp/pool-handoff-linux-20261030/include/linux/uprobes.h \
    $(wildcard include/config/UPROBES) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/uprobes.h \
  /tmp/pool-handoff-linux-20261030/include/linux/notifier.h \
  /tmp/pool-handoff-linux-20261030/include/linux/mutex.h \
    $(wildcard include/config/MUTEX_SPIN_ON_OWNER) \
    $(wildcard include/config/DEBUG_MUTEXES) \
  /tmp/pool-handoff-linux-20261030/include/linux/debug_locks.h \
  /tmp/pool-handoff-linux-20261030/include/linux/srcu.h \
    $(wildcard include/config/TINY_SRCU) \
    $(wildcard include/config/NEED_SRCU_NMI_SAFE) \
  /tmp/pool-handoff-linux-20261030/include/linux/workqueue.h \
    $(wildcard include/config/DEBUG_OBJECTS_WORK) \
    $(wildcard include/config/FREEZER) \
    $(wildcard include/config/WQ_WATCHDOG) \
  /tmp/pool-handoff-linux-20261030/include/linux/timer.h \
    $(wildcard include/config/DEBUG_OBJECTS_TIMERS) \
  /tmp/pool-handoff-linux-20261030/include/linux/ktime.h \
  /tmp/pool-handoff-linux-20261030/include/linux/jiffies.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/jiffies.h \
  include/generated/timeconst.h \
  /tmp/pool-handoff-linux-20261030/include/vdso/ktime.h \
  /tmp/pool-handoff-linux-20261030/include/linux/timekeeping.h \
    $(wildcard include/config/GENERIC_CMOS_UPDATE) \
  /tmp/pool-handoff-linux-20261030/include/linux/clocksource_ids.h \
  /tmp/pool-handoff-linux-20261030/include/linux/debugobjects.h \
    $(wildcard include/config/DEBUG_OBJECTS) \
    $(wildcard include/config/DEBUG_OBJECTS_FREE) \
  /tmp/pool-handoff-linux-20261030/include/linux/rcu_segcblist.h \
  /tmp/pool-handoff-linux-20261030/include/linux/srcutree.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rcu_node_tree.h \
    $(wildcard include/config/RCU_FANOUT) \
    $(wildcard include/config/RCU_FANOUT_LEAF) \
  /tmp/pool-handoff-linux-20261030/include/linux/page-flags-layout.h \
  include/generated/bounds.h \
  /tmp/pool-handoff-linux-20261030/include/linux/seqlock.h \
  /tmp/pool-handoff-linux-20261030/include/linux/percpu_counter.h \
  /tmp/pool-handoff-linux-20261030/include/linux/percpu.h \
    $(wildcard include/config/RANDOM_KMALLOC_CACHES) \
    $(wildcard include/config/NEED_PER_CPU_PAGE_FIRST_CHUNK) \
  /tmp/pool-handoff-linux-20261030/include/linux/mmdebug.h \
    $(wildcard include/config/DEBUG_VM) \
    $(wildcard include/config/DEBUG_VM_IRQSOFF) \
    $(wildcard include/config/DEBUG_VM_PGFLAGS) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/mmu.h \
    $(wildcard include/config/MODIFY_LDT_SYSCALL) \
  /tmp/pool-handoff-linux-20261030/include/linux/kmod.h \
  /tmp/pool-handoff-linux-20261030/include/linux/umh.h \
  /tmp/pool-handoff-linux-20261030/include/linux/gfp.h \
    $(wildcard include/config/HIGHMEM) \
    $(wildcard include/config/ZONE_DMA) \
    $(wildcard include/config/ZONE_DMA32) \
    $(wildcard include/config/ZONE_DEVICE) \
    $(wildcard include/config/CONTIG_ALLOC) \
  /tmp/pool-handoff-linux-20261030/include/linux/mmzone.h \
    $(wildcard include/config/ARCH_FORCE_MAX_ORDER) \
    $(wildcard include/config/CMA) \
    $(wildcard include/config/MEMORY_ISOLATION) \
    $(wildcard include/config/ZSMALLOC) \
    $(wildcard include/config/UNACCEPTED_MEMORY) \
    $(wildcard include/config/LRU_GEN_STATS) \
    $(wildcard include/config/MEMORY_HOTPLUG) \
    $(wildcard include/config/COMPACTION) \
    $(wildcard include/config/MEMORY_FAILURE) \
    $(wildcard include/config/PAGE_EXTENSION) \
    $(wildcard include/config/DEFERRED_STRUCT_PAGE_INIT) \
    $(wildcard include/config/HAVE_MEMORYLESS_NODES) \
    $(wildcard include/config/SPARSEMEM_EXTREME) \
    $(wildcard include/config/HAVE_ARCH_PFN_VALID) \
  /tmp/pool-handoff-linux-20261030/include/linux/list_nulls.h \
  /tmp/pool-handoff-linux-20261030/include/linux/nodemask.h \
  /tmp/pool-handoff-linux-20261030/include/linux/random.h \
    $(wildcard include/config/VMGENID) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/random.h \
  /tmp/pool-handoff-linux-20261030/include/linux/irqnr.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/irqnr.h \
  /tmp/pool-handoff-linux-20261030/include/linux/prandom.h \
  /tmp/pool-handoff-linux-20261030/include/linux/once.h \
  /tmp/pool-handoff-linux-20261030/include/linux/pageblock-flags.h \
    $(wildcard include/config/HUGETLB_PAGE_SIZE_VARIABLE) \
  /tmp/pool-handoff-linux-20261030/include/linux/page-flags.h \
    $(wildcard include/config/ARCH_USES_PG_UNCACHED) \
    $(wildcard include/config/PAGE_IDLE_FLAG) \
    $(wildcard include/config/ARCH_USES_PG_ARCH_X) \
    $(wildcard include/config/HUGETLB_PAGE_OPTIMIZE_VMEMMAP) \
  /tmp/pool-handoff-linux-20261030/include/linux/local_lock.h \
  /tmp/pool-handoff-linux-20261030/include/linux/local_lock_internal.h \
  /tmp/pool-handoff-linux-20261030/include/linux/memory_hotplug.h \
    $(wildcard include/config/HAVE_ARCH_NODEDATA_EXTENSION) \
    $(wildcard include/config/ARCH_HAS_ADD_PAGES) \
    $(wildcard include/config/MEMORY_HOTREMOVE) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/mmzone.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/mmzone_64.h \
  /tmp/pool-handoff-linux-20261030/include/linux/topology.h \
    $(wildcard include/config/USE_PERCPU_NUMA_NODE_ID) \
    $(wildcard include/config/SCHED_SMT) \
  /tmp/pool-handoff-linux-20261030/include/linux/arch_topology.h \
    $(wildcard include/config/ACPI_CPPC_LIB) \
    $(wildcard include/config/GENERIC_ARCH_TOPOLOGY) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/topology.h \
    $(wildcard include/config/SCHED_MC_PRIO) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/mpspec.h \
    $(wildcard include/config/EISA) \
    $(wildcard include/config/X86_LOCAL_APIC) \
    $(wildcard include/config/X86_MPPARSE) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/mpspec_def.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/x86_init.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/apicdef.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/topology.h \
  /tmp/pool-handoff-linux-20261030/include/linux/cpu_smt.h \
    $(wildcard include/config/HOTPLUG_SMT) \
  /tmp/pool-handoff-linux-20261030/include/linux/sysctl.h \
    $(wildcard include/config/SYSCTL) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/sysctl.h \
  /tmp/pool-handoff-linux-20261030/include/linux/elf.h \
    $(wildcard include/config/ARCH_USE_GNU_PROPERTY) \
    $(wildcard include/config/ARCH_HAVE_ELF_PROT) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/elf.h \
    $(wildcard include/config/X86_X32_ABI) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/user.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/user_64.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/fsgsbase.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/vdso.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/elf.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/elf-em.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kobject.h \
    $(wildcard include/config/UEVENT_HELPER) \
    $(wildcard include/config/DEBUG_KOBJECT_RELEASE) \
  /tmp/pool-handoff-linux-20261030/include/linux/sysfs.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kernfs.h \
    $(wildcard include/config/KERNFS) \
  /tmp/pool-handoff-linux-20261030/include/linux/idr.h \
  /tmp/pool-handoff-linux-20261030/include/linux/radix-tree.h \
  /tmp/pool-handoff-linux-20261030/include/linux/xarray.h \
    $(wildcard include/config/XARRAY_MULTI) \
  /tmp/pool-handoff-linux-20261030/include/linux/sched/mm.h \
    $(wildcard include/config/MMU_LAZY_TLB_REFCOUNT) \
    $(wildcard include/config/ARCH_HAS_MEMBARRIER_CALLBACKS) \
  /tmp/pool-handoff-linux-20261030/include/linux/sched.h \
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
    $(wildcard include/config/FUNCTION_GRAPH_TRACER) \
    $(wildcard include/config/BCACHE) \
    $(wildcard include/config/VMAP_STACK) \
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
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/sched.h \
  /tmp/pool-handoff-linux-20261030/include/linux/pid.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rculist.h \
    $(wildcard include/config/PROVE_RCU_LIST) \
  /tmp/pool-handoff-linux-20261030/include/linux/sem.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/sem.h \
  /tmp/pool-handoff-linux-20261030/include/linux/ipc.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rhashtable-types.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/ipc.h \
  arch/x86/include/generated/uapi/asm/ipcbuf.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/ipcbuf.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/sembuf.h \
  /tmp/pool-handoff-linux-20261030/include/linux/shm.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/shm.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/hugetlb_encode.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/shmbuf.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/shmbuf.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/shmparam.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kmsan_types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/plist.h \
    $(wildcard include/config/DEBUG_PLIST) \
  /tmp/pool-handoff-linux-20261030/include/linux/hrtimer.h \
    $(wildcard include/config/HIGH_RES_TIMERS) \
    $(wildcard include/config/TIME_LOW_RES) \
    $(wildcard include/config/TIMERFD) \
  /tmp/pool-handoff-linux-20261030/include/linux/hrtimer_defs.h \
  /tmp/pool-handoff-linux-20261030/include/linux/timerqueue.h \
  /tmp/pool-handoff-linux-20261030/include/linux/seccomp.h \
    $(wildcard include/config/SECCOMP) \
    $(wildcard include/config/HAVE_ARCH_SECCOMP_FILTER) \
    $(wildcard include/config/SECCOMP_FILTER) \
    $(wildcard include/config/CHECKPOINT_RESTORE) \
    $(wildcard include/config/SECCOMP_CACHE_DEBUG) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/seccomp.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/seccomp.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/unistd.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/unistd.h \
  arch/x86/include/generated/uapi/asm/unistd_64.h \
  arch/x86/include/generated/asm/unistd_64_x32.h \
  arch/x86/include/generated/asm/unistd_32_ia32.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/ia32_unistd.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/seccomp.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/unistd.h \
  /tmp/pool-handoff-linux-20261030/include/linux/resource.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/resource.h \
  arch/x86/include/generated/uapi/asm/resource.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/resource.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/resource.h \
  /tmp/pool-handoff-linux-20261030/include/linux/latencytop.h \
  /tmp/pool-handoff-linux-20261030/include/linux/sched/prio.h \
  /tmp/pool-handoff-linux-20261030/include/linux/sched/types.h \
  /tmp/pool-handoff-linux-20261030/include/linux/signal_types.h \
    $(wildcard include/config/OLD_SIGACTION) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/signal.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/signal.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/signal.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/signal-defs.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/uapi/asm/siginfo.h \
  /tmp/pool-handoff-linux-20261030/include/uapi/asm-generic/siginfo.h \
  /tmp/pool-handoff-linux-20261030/include/linux/syscall_user_dispatch.h \
  /tmp/pool-handoff-linux-20261030/include/linux/task_io_accounting.h \
    $(wildcard include/config/TASK_IO_ACCOUNTING) \
  /tmp/pool-handoff-linux-20261030/include/linux/posix-timers.h \
  /tmp/pool-handoff-linux-20261030/include/linux/alarmtimer.h \
    $(wildcard include/config/RTC_CLASS) \
  /tmp/pool-handoff-linux-20261030/include/uapi/linux/rseq.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kcsan.h \
  /tmp/pool-handoff-linux-20261030/include/linux/rv.h \
    $(wildcard include/config/RV_REACTORS) \
  /tmp/pool-handoff-linux-20261030/include/linux/livepatch_sched.h \
  arch/x86/include/generated/asm/kmap_size.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/kmap_size.h \
    $(wildcard include/config/DEBUG_KMAP_LOCAL) \
  /tmp/pool-handoff-linux-20261030/include/linux/sync_core.h \
    $(wildcard include/config/ARCH_HAS_SYNC_CORE_BEFORE_USERMODE) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/sync_core.h \
  /tmp/pool-handoff-linux-20261030/include/linux/kobject_ns.h \
  /tmp/pool-handoff-linux-20261030/include/linux/moduleparam.h \
    $(wildcard include/config/ALPHA) \
    $(wildcard include/config/IA64) \
    $(wildcard include/config/PPC64) \
  /tmp/pool-handoff-linux-20261030/include/linux/rbtree_latch.h \
  /tmp/pool-handoff-linux-20261030/include/linux/error-injection.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/error-injection.h \
  /tmp/pool-handoff-linux-20261030/include/linux/dynamic_debug.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/module.h \
  /tmp/pool-handoff-linux-20261030/include/asm-generic/module.h \
    $(wildcard include/config/HAVE_MOD_ARCH_SPECIFIC) \
    $(wildcard include/config/MODULES_USE_ELF_REL) \
    $(wildcard include/config/MODULES_USE_ELF_RELA) \
  /tmp/pool-handoff-linux-20261030/include/linux/build-salt.h \
    $(wildcard include/config/BUILD_SALT) \
  /tmp/pool-handoff-linux-20261030/include/linux/elfnote.h \
  /tmp/pool-handoff-linux-20261030/include/linux/elfnote-lto.h \
    $(wildcard include/config/LTO) \
  /tmp/pool-handoff-linux-20261030/include/linux/export-internal.h \
    $(wildcard include/config/PARISC) \
  /tmp/pool-handoff-linux-20261030/include/linux/vermagic.h \
    $(wildcard include/config/PREEMPT_BUILD) \
  include/generated/utsrelease.h \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/vermagic.h \
    $(wildcard include/config/M486SX) \
    $(wildcard include/config/M486) \
    $(wildcard include/config/M586) \
    $(wildcard include/config/M586TSC) \
    $(wildcard include/config/M586MMX) \
    $(wildcard include/config/MCORE2) \
    $(wildcard include/config/M686) \
    $(wildcard include/config/MPENTIUMII) \
    $(wildcard include/config/MPENTIUMIII) \
    $(wildcard include/config/MPENTIUMM) \
    $(wildcard include/config/MPENTIUM4) \
    $(wildcard include/config/MK6) \
    $(wildcard include/config/MK7) \
    $(wildcard include/config/MK8) \
    $(wildcard include/config/MELAN) \
    $(wildcard include/config/MCRUSOE) \
    $(wildcard include/config/MEFFICEON) \
    $(wildcard include/config/MWINCHIPC6) \
    $(wildcard include/config/MWINCHIP3D) \
    $(wildcard include/config/MCYRIXIII) \
    $(wildcard include/config/MVIAC3_2) \
    $(wildcard include/config/MVIAC7) \
    $(wildcard include/config/MGEODEGX1) \
    $(wildcard include/config/MGEODE_LX) \
  /tmp/pool-handoff-linux-20261030/arch/x86/include/asm/orc_header.h \
  arch/x86/include/generated/asm/orc_hash.h \

/tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o: $(deps_/tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o)

$(deps_/tmp/pool-memory-module-build-20261030/pool_memory_module.mod.o):
