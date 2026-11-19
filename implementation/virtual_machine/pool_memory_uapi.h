/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef CXL_POOL_MEMORY_UAPI_H
#define CXL_POOL_MEMORY_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>

#define CPM_ABI 4
/* Reserved fields must be zero. ABI 4 rejects all previous handles.
 * Logical page index is the array index; offsets are relative to the DAX device. */
struct cpm_entry { __u64 offset, flags; };
struct cpm_config {
    __u64 abi, phys_base, device_bytes, reserved[2];
    __u64 count, entries;
};
/* Cache instructions for this observer's reserved 64MiB metadata extent.
 * offset is relative to metadata_offset; operation: 0=writeback, 1=invalidate.
 * lines is incremented at each instruction, never inferred from a bitmap. */
struct cpm_cache_range {
    __u64 phys_base, device_bytes, metadata_offset, cookie_lo, cookie_hi, pages;
    __u64 offset, bytes, operation, lines, elapsed_ns, reserved;
};
#define CPM_CACHE_RANGE _IOWR('P', 0x80, struct cpm_cache_range)
/* Read-only, per-observer count of metadata validation CLFLUSHOPT instructions. */
#define CPM_CACHE_INIT_STATS _IOR('P', 0x81, __u64)
/* ABI 4 keeps this VM's authoritative mapper in a reserved CXL extent.
 * Handles contain no per-page arrays. Import consumes a sealed owner epoch;
 * callers must fence the source process and drain every writer first. */
struct cpm_handle {
    __u64 abi, phys_base, device_bytes, metadata_offset, metadata_bytes;
    __u64 cookie_lo, cookie_hi, owner_epoch, generation, allocation_generation;
    __u64 count, reserved[4];
    __u64 faults, exhausted, full_scan_pages, state;
};
struct cpm_create {
    struct cpm_config config;
    __u64 metadata_offset, metadata_bytes, owner_epoch, allocation_generation;
};
#define CPM_CREATE _IOW('P', 0x75, struct cpm_create)
#define CPM_STATUS _IOWR('P', 0x76, struct cpm_handle)
#define CPM_SEAL _IOWR('P', 0x77, struct cpm_handle)
#define CPM_IMPORT _IOWR('P', 0x78, struct cpm_handle)
/* Read-only observer. No lease, epoch, statistics or guest PTE updates.
 * Bitmap and receipt use one bit per GPA page. The receipt is produced while
 * issuing cache instructions, then copied out after MFENCE. */
struct cpm_cache {
    __u64 phys_base, device_bytes, metadata_offset, cookie_lo, cookie_hi, pages;
    __u64 bitmap, covered, lines, elapsed_ns;
    __u64 skip_page, reserved; /* Fixed ABI sentinel: U64_MAX, 0. */
};
#define CPM_PARK _IO('P', 0x7c)
#define CPM_WRITEBACK _IOWR('P', 0x7e, struct cpm_cache)
#define CPM_CACHE _IOWR('P', 0x7b, struct cpm_cache)
/* Stopped-owner local overlay. The persistent GPA -> home table is immutable.
 * dirty is a pointer to eight u64 words (512 pages). */
struct cpm_tier { __u64 chunk, dram, dirty, copied, elapsed_ns, reserved; };
#define CPM_TIER _IOWR('P', 0x82, struct cpm_tier)
#endif
