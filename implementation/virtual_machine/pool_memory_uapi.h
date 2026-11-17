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
struct cpm_query {
    __u64 count, entries;
    __u64 generation, reserved[2], faults, exhausted;
};
/* Read-only, no-prefault observation of actual native PTEs (not the map table).
 * flags: bit0 present, bit1 writable. A missing PTE has pfn=flags=0. */
struct cpm_pte { __u64 pfn, flags; };
struct cpm_audit { __u64 address, count, ptes; };
#define CPM_QUERY _IOWR('P', 0x71, struct cpm_query)
#define CPM_AUDIT _IOWR('P', 0x74, struct cpm_audit)
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
#define CPM_RETIRE _IOWR('P', 0x7a, struct cpm_handle)
/* Read-only observer. No lease, epoch, statistics or guest PTE updates.
 * Bitmap and receipt use one bit per GPA page. The receipt is produced while
 * issuing cache instructions, then copied out after MFENCE. */
struct cpm_cache {
    __u64 phys_base, device_bytes, metadata_offset, cookie_lo, cookie_hi, pages;
    __u64 bitmap, covered, lines, elapsed_ns;
    __u64 skip_page, reserved;
};
#define CPM_CACHE _IOWR('P', 0x7b, struct cpm_cache)
#endif
