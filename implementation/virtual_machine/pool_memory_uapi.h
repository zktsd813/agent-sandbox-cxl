/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef CXL_POOL_MEMORY_UAPI_H
#define CXL_POOL_MEMORY_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>

#define CPM_ABI 1
#define CPM_SHARED 1
/* Logical page index is the array index; offsets are relative to the DAX device. */
struct cpm_entry { __u64 offset, flags; };
struct cpm_config {
    __u64 abi, phys_base, device_bytes, canonical_start, canonical_bytes;
    __u64 count, entries, credit_count, credits;
};
struct cpm_query {
    __u64 count, entries, credit_count, credits;
    __u64 generation, cow_pages, discard_pages, faults, exhausted;
};
struct cpm_range { __u64 address, first, count; };
struct cpm_credits { __u64 count, offsets; };
/* Read-only, no-prefault observation of actual native PTEs (not the map table).
 * flags: bit0 present, bit1 writable. A missing PTE has pfn=flags=0. */
struct cpm_pte { __u64 pfn, flags; };
struct cpm_audit { __u64 address, count, ptes; };
#define CPM_CONFIG _IOW('P', 0x70, struct cpm_config)
#define CPM_QUERY _IOWR('P', 0x71, struct cpm_query)
#define CPM_DISCARD _IOW('P', 0x72, struct cpm_range)
#define CPM_ADD_CREDITS _IOW('P', 0x73, struct cpm_credits)
#define CPM_AUDIT _IOWR('P', 0x74, struct cpm_audit)
#endif
