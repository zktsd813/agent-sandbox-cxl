// SPDX-License-Identifier: GPL-2.0
/* Experimental, privileged VM-private CXL RAM. One execution owner per fd.
 * Private frames are assigned by the controller; no distributed allocator
 * metadata or cross-kernel locks are introduced here.
 */
#include <linux/capability.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/sort.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/random.h>
#include <linux/kvm_dax.h>
#include <linux/bitmap.h>
#include <linux/ktime.h>
#include <asm/cpufeature.h>
#include "pool_memory_uapi.h"

#define CPM_MAX_PAGES (1UL << 20)
#define CPM_MAGIC 0x43584c504d415034ULL
#define CPM_ACTIVE 1
#define CPM_SEALED 2
#define CPM_CLOSED 3

struct cpm_header {
    u64 magic;
    struct cpm_handle handle;
    struct cpm_config config;
};
struct cpm {
    struct mutex lock;
    struct cpm_config cfg;
    struct cpm_entry *map;
    struct cpm_handle local, *state;
    struct cpm_header *header;
    u64 epoch, cookie_lo, cookie_hi;
    void *base;
    bool ready, mapped, local_owner;
    struct vm_area_struct *owner_vma;
    void **dram; /* Host-local vmalloc chunks, never in persistent metadata. */
    /* Host DRAM only. Observer never changes the owner's CXL metadata. */
    struct cpm_cache observer;
    struct cpm_entry *observer_map;
    void *observer_base;
    u64 observer_init_inv_lines;
};

static u64 invalidate_range(void *address, u64 bytes)
{
    u64 i, emitted = 0;
    for (i = 0; i < bytes; i += 64) {
        asm volatile("clflushopt (%0)" : : "r" ((char *)address + i) : "memory");
        emitted++;
    }
    asm volatile("mfence" : : : "memory");
    return emitted;
}

static int cache_pages(struct cpm *c, void __user *user, bool writeback)
{
    struct cpm_cache q;
    struct cpm_header *header;
    unsigned long *bits = NULL, *covered = NULL;
    u64 page, line, start;
    size_t bytes;
    int ret = -EINVAL;
    if (copy_from_user(&q, user, sizeof(q))) return -EFAULT;
    if (!boot_cpu_has(X86_FEATURE_CLFLUSHOPT) || (writeback && !boot_cpu_has(X86_FEATURE_CLWB))) return -EOPNOTSUPP;
    if (!q.pages || q.pages > CPM_MAX_PAGES || q.reserved!=0 || q.skip_page != U64_MAX ||
        (q.phys_base | q.device_bytes | q.metadata_offset) & (PAGE_SIZE-1) ||
        !q.device_bytes || q.phys_base + q.device_bytes < q.phys_base ||
        q.metadata_offset > q.device_bytes ||
        PAGE_SIZE + q.pages * sizeof(struct cpm_entry) > q.device_bytes-q.metadata_offset)
        return -EINVAL;
    mutex_lock(&c->lock);
    if (!c->observer_base) {
        c->observer_base = memremap(q.phys_base, q.device_bytes, MEMREMAP_WB);
        if (!c->observer_base) { ret = -ENXIO; goto out; }
        header = c->observer_base + q.metadata_offset;
        c->observer_init_inv_lines += invalidate_range(header, PAGE_SIZE);
        if (header->magic != CPM_MAGIC || header->handle.cookie_lo != q.cookie_lo ||
            header->handle.cookie_hi != q.cookie_hi || header->handle.count != q.pages ||
            header->handle.phys_base != q.phys_base || header->handle.device_bytes != q.device_bytes ||
            header->handle.metadata_offset != q.metadata_offset) goto bad_observer;
        c->observer_init_inv_lines += invalidate_range((void *)header+PAGE_SIZE, q.pages*sizeof(struct cpm_entry));
        c->observer_map = kvmemdup((void *)header+PAGE_SIZE,
                                  q.pages*sizeof(struct cpm_entry), GFP_KERNEL);
        if (!c->observer_map) { ret = -ENOMEM; goto bad_observer; }
        for (page = 0; page < q.pages; page++) {
            u64 offset = c->observer_map[page].offset;
            if (c->observer_map[page].flags || offset & (PAGE_SIZE-1) ||
                offset > q.device_bytes-PAGE_SIZE ||
                (offset >= q.metadata_offset && offset-q.metadata_offset < PAGE_SIZE+q.pages*16))
                goto bad_observer;
        }
        c->observer = q;
    }
    if (memcmp(&q, &c->observer, 6*sizeof(u64))) { ret = -ESTALE; goto out; }
    bytes = BITS_TO_LONGS(q.pages)*sizeof(unsigned long);
    bits = memdup_user(u64_to_user_ptr(q.bitmap), bytes);
    if (IS_ERR(bits)) { ret = PTR_ERR(bits); bits = NULL; goto out; }
    covered = bitmap_zalloc(q.pages, GFP_KERNEL);
    if (!covered) { ret = -ENOMEM; goto out; }
    if (q.pages % BITS_PER_LONG && bits[BITS_TO_LONGS(q.pages)-1] >> (q.pages % BITS_PER_LONG))
        goto out;
    start = ktime_get_ns(); q.lines = 0;
    for_each_set_bit(page, bits, q.pages) {
        void *address = c->observer_base + c->observer_map[page].offset;
        for (line = 0; line < PAGE_SIZE; line += 64) {
            if (writeback) asm volatile("clwb (%0)" : : "r" ((char *)address+line) : "memory");
            else asm volatile("clflushopt (%0)" : : "r" ((char *)address+line) : "memory");
            q.lines++;
        }
        __set_bit(page, covered);
    }
    if (writeback) asm volatile("sfence" : : : "memory");
    else asm volatile("mfence" : : : "memory");
    q.elapsed_ns = ktime_get_ns()-start;
    if (!bitmap_equal(bits, covered, q.pages)) { ret = -EIO; goto out; }
    ret = copy_to_user(u64_to_user_ptr(q.covered), covered, bytes) ||
          copy_to_user(user, &q, sizeof(q)) ? -EFAULT : 0;
    goto out;
bad_observer:
    kvfree(c->observer_map); c->observer_map = NULL;
    memunmap(c->observer_base); c->observer_base = NULL;
out:
    kvfree(bits); bitmap_free(covered);
    mutex_unlock(&c->lock);
    return ret;
}

static bool owns(struct cpm *c)
{
    return c->local_owner && c->epoch == READ_ONCE(c->state->owner_epoch) &&
        c->cookie_lo == READ_ONCE(c->state->cookie_lo) &&
        c->cookie_hi == READ_ONCE(c->state->cookie_hi);
}

static bool cpm_in_range(u64 offset, u64 start, u64 bytes)
{
    return offset >= start && offset - start < bytes;
}

static bool valid_offset(struct cpm *c, u64 offset)
{
    if (offset & (PAGE_SIZE - 1) || offset >= c->cfg.device_bytes)
        return false;
    return !c->header || !cpm_in_range(offset, c->state->metadata_offset, c->state->metadata_bytes);
}

static int cmp_u64(const void *a, const void *b)
{
    u64 x = *(const u64 *)a, y = *(const u64 *)b;
    return (x > y) - (x < y);
}

static int validate_private(struct cpm *c)
{
    u64 i, n = 0, *all;
    int ret = 0;
    all = kvmalloc_array(c->cfg.count, sizeof(*all), GFP_KERNEL);
    if (!all)
        return -ENOMEM;
    for (i = 0; i < c->cfg.count; i++) {
        if (c->map[i].flags || !valid_offset(c, c->map[i].offset)) {
            ret = -EINVAL;
            goto out;
        }
        all[n++] = c->map[i].offset;
    }

    sort(all, n, sizeof(*all), cmp_u64, NULL);
    for (i = 1; i < n; i++)
        if (all[i] == all[i - 1]) { ret = -EINVAL; break; }
out:
    kvfree(all);
    return ret;
}

/* The mmap lock is held by the fault caller. The context lock serializes
 * competing host I/O and guest CPU faults. The fault path performs no user copy. */
static vm_fault_t cpm_fault(struct vm_fault *vmf)
{
    struct cpm *c = vmf->vma->vm_private_data;
    u64 index = vmf->pgoff;
    pgprot_t prot = vmf->vma->vm_page_prot;
    vm_fault_t ret;
    bool write = vmf->flags & FAULT_FLAG_WRITE;
    mutex_lock(&c->lock);
    if (c->header && (!owns(c) ||
        (write && READ_ONCE(c->state->state) != CPM_ACTIVE) ||
        (c->local_owner && READ_ONCE(c->state->state) == CPM_CLOSED))) {
        ret = VM_FAULT_SIGBUS; goto out;
    }
    if (index >= c->cfg.count) { ret = VM_FAULT_SIGBUS; goto out; }
    c->state->faults++;
    /* Install writable mappings at their final permission. Never remove an
     * existing PTE here: another vCPU may be following it after its fault
     * completed, and a transient hole makes KVM's PFN lookup fail with EFAULT. */
    if (vmf->vma->vm_flags & VM_WRITE)
        prot = __pgprot(pgprot_val(prot) | _PAGE_RW);
    ret = vmf_insert_pfn_prot(vmf->vma, vmf->address & PAGE_MASK,
        c->dram && c->dram[index >> 9] ?
        vmalloc_to_pfn(c->dram[index >> 9] + (index & 511) * PAGE_SIZE) :
        PHYS_PFN(c->cfg.phys_base + c->map[index].offset), prot);
out:
    mutex_unlock(&c->lock);
    return ret;
}

static vm_fault_t cpm_pfn_mkwrite(struct vm_fault *vmf)
{
    struct cpm *c = vmf->vma->vm_private_data;
    vm_fault_t ret = 0;
    mutex_lock(&c->lock);
    if (vmf->pgoff >= c->cfg.count ||
        (c->header && (!owns(c) || READ_ONCE(c->state->state) != CPM_ACTIVE)))
        ret = VM_FAULT_SIGBUS;
    mutex_unlock(&c->lock);
    /* Let finish_mkwrite_fault upgrade the original PTE under its page-table
     * lock. Calling the insertion fault handler here skips that upgrade. */
    return ret;
}

static void cpm_vma_close(struct vm_area_struct *vma)
{
    struct cpm *c = vma->vm_private_data;
    mutex_lock(&c->lock);
    if (c->owner_vma == vma) {
        c->mapped = false;
        c->owner_vma = NULL;
    }
    mutex_unlock(&c->lock);
}
static int cpm_may_split(struct vm_area_struct *vma, unsigned long address) { return -EINVAL; }
static int cpm_mremap(struct vm_area_struct *vma) { return -EINVAL; }
static const struct vm_operations_struct cpm_vm_ops = {
    .fault = cpm_fault,
    .pfn_mkwrite = cpm_pfn_mkwrite,
    .close = cpm_vma_close,
    .may_split = cpm_may_split,
    .mremap = cpm_mremap,
};

static int cpm_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct cpm *c = file->private_data;
    u64 first = vma->vm_pgoff, count = vma_pages(vma);
    mutex_lock(&c->lock);
    /* A single whole-RAM mapping per context preserves source fencing. */
    if (!c->ready || c->mapped || !(vma->vm_flags & VM_SHARED) ||
        first != 0 || count != c->cfg.count) {
        mutex_unlock(&c->lock);
        return -EINVAL;
    }
    vm_flags_set(vma, VM_IO | VM_PFNMAP | VM_DONTEXPAND | VM_DONTDUMP | VM_DONTCOPY);
    vma->vm_ops = &cpm_vm_ops;
    vma->vm_private_data = c;
    c->mapped = true;
    c->owner_vma = vma;
    mutex_unlock(&c->lock);
    return 0;
}

static long configure(struct cpm *c, const struct cpm_config *input)
{
    struct cpm_config cfg;
    int ret;
    if (c->map) return -EBUSY;
    cfg = *input;
    if (cfg.abi != CPM_ABI || !cfg.count || cfg.count > CPM_MAX_PAGES ||
        !cfg.device_bytes ||
        (cfg.phys_base | cfg.device_bytes) & (PAGE_SIZE - 1) ||
        cfg.phys_base + cfg.device_bytes < cfg.phys_base || cfg.reserved[0] || cfg.reserved[1])
        return -EINVAL;
    c->cfg = cfg;
    c->map = kvmalloc_array(cfg.count, sizeof(*c->map), GFP_KERNEL);
    if (!c->map) { ret = -ENOMEM; goto fail; }
    if (copy_from_user(c->map, u64_to_user_ptr(cfg.entries), cfg.count * sizeof(*c->map))) {
        ret = -EFAULT;
        goto fail;
    }
    ret = validate_private(c);
    if (ret)
        goto fail;
    c->base = memremap(cfg.phys_base, cfg.device_bytes, MEMREMAP_WB);
    if (!c->base) { ret = -ENXIO; goto fail; }
    c->state->generation = 1;
    c->state->count = cfg.count;
    c->state->full_scan_pages = cfg.count;
    smp_store_release(&c->ready, true);
    return 0;
fail:
    if (c->base) { memunmap(c->base); c->base = NULL; }
    kvfree(c->map); c->map = NULL;
    return ret;
}

/* VM-private control state in CXL. No guest data or per-page PTE inspection is
 * needed when a fenced owner hands this exact mapper to the next owner. */
static u64 metadata_need(const struct cpm_config *cfg)
{
    return PAGE_ALIGN(PAGE_SIZE + cfg->count * sizeof(struct cpm_entry));
}
static void persistent_pointers(struct cpm *c, struct cpm_header *h)
{
    c->header = h; c->state = &h->handle; c->cfg = h->config;
    c->map = (void *)h + PAGE_SIZE;
    c->local_owner=true;
    c->epoch = c->state->owner_epoch;
    c->cookie_lo = c->state->cookie_lo; c->cookie_hi = c->state->cookie_hi;
}
static int create_persistent(struct cpm *c, void __user *user)
{
    struct cpm_create p;
    struct cpm_header *h;
    struct cpm_entry *old_map;
    u64 i;
    int ret;
    if (copy_from_user(&p, user, sizeof(p))) return -EFAULT;
    if (!p.allocation_generation || p.metadata_offset & (PAGE_SIZE - 1) ||
        !p.metadata_bytes || p.metadata_bytes & (PAGE_SIZE - 1) ||
        p.metadata_offset > p.config.device_bytes ||
        p.metadata_bytes > p.config.device_bytes - p.metadata_offset ||
        p.config.count > CPM_MAX_PAGES || metadata_need(&p.config) > p.metadata_bytes)
        return -EINVAL;
    /* configure has the one required baseline admission/uniqueness check. */
    ret = configure(c, &p.config);
    if (ret) return ret;
    h = c->base + p.metadata_offset;
    if (READ_ONCE(h->magic) == CPM_MAGIC)
        return -EBUSY;
    for (i = 0; i < c->cfg.count; i++)
        if (cpm_in_range(c->map[i].offset, p.metadata_offset, p.metadata_bytes)) return -EINVAL;
    old_map = c->map;
    memset(h, 0, PAGE_SIZE);
    h->config = c->cfg; h->config.entries = 0;
    h->handle = c->local;
    h->handle.abi = CPM_ABI; h->handle.phys_base = p.config.phys_base;
    h->handle.device_bytes = p.config.device_bytes;
    h->handle.metadata_offset = p.metadata_offset; h->handle.metadata_bytes = p.metadata_bytes;
    get_random_bytes(&h->handle.cookie_lo, 2 * sizeof(u64));
    h->handle.owner_epoch = p.owner_epoch;
    h->handle.allocation_generation = p.allocation_generation;
    h->handle.state = CPM_ACTIVE;
    persistent_pointers(c, h);
    memcpy(c->map, old_map, c->cfg.count * sizeof(*c->map));
    kvfree(old_map);
    smp_store_release(&h->magic, CPM_MAGIC);
    return 0;
}
static int import_persistent(struct cpm *c, void __user *user)
{
    struct cpm_handle in, *h;
    struct cpm_header *header;
    void *base;
    int ret = -ESTALE;
    if (c->ready || copy_from_user(&in, user, sizeof(in))) return -EINVAL;
    if (in.abi != CPM_ABI || in.reserved[0] || in.reserved[1] || in.reserved[2] || in.reserved[3] ||
        in.state != CPM_SEALED || !in.allocation_generation ||
        !in.device_bytes || (in.phys_base | in.device_bytes | in.metadata_offset | in.metadata_bytes) & (PAGE_SIZE-1) ||
        in.phys_base + in.device_bytes < in.phys_base ||
        in.metadata_offset > in.device_bytes || in.metadata_bytes < PAGE_SIZE ||
        in.metadata_bytes > in.device_bytes - in.metadata_offset || in.owner_epoch == U64_MAX)
        return -EINVAL;
    base = memremap(in.phys_base, in.device_bytes, MEMREMAP_WB);
    if (!base) return -ENXIO;
    header = base + in.metadata_offset; h = &header->handle;
    if (smp_load_acquire(&header->magic) != CPM_MAGIC ||
        h->abi != in.abi || h->phys_base != in.phys_base || h->device_bytes != in.device_bytes ||
        h->metadata_offset != in.metadata_offset || h->metadata_bytes != in.metadata_bytes ||
        h->cookie_lo != in.cookie_lo || h->cookie_hi != in.cookie_hi ||
        h->owner_epoch != in.owner_epoch || h->generation != in.generation ||
        h->allocation_generation != in.allocation_generation || h->count != in.count ||
        !h->count || h->count > CPM_MAX_PAGES ||
        header->config.count != h->count || header->config.phys_base != in.phys_base ||
        header->config.device_bytes != in.device_bytes || metadata_need(&header->config) > in.metadata_bytes)
        goto fail;
    /* Coherent lab ownership transaction. Not a noncoherent CXL lease. */
    if (cmpxchg(&h->state, CPM_SEALED, CPM_ACTIVE) != CPM_SEALED) goto fail;
    WRITE_ONCE(h->owner_epoch, in.owner_epoch + 1);
    c->base = base;
    persistent_pointers(c, header);
    smp_store_release(&c->ready, true);
    if (copy_to_user(user, h, sizeof(*h))) return -EFAULT;
    return 0;
fail:
    memunmap(base);
    return ret;
}
static int mapping_identity(struct vm_area_struct *vma, struct kvm_dax_mapping_identity *out)
{
    struct cpm *c = vma->vm_private_data;
    struct cpm_handle *h;
    int ret = -ESTALE;
    mutex_lock(&c->lock);
    h = c->state;
    if (!c->header || !c->ready || c->owner_vma != vma ||
        !owns(c) || h->state == CPM_CLOSED) goto done;
    {
    u64 page = out->query_page;
    *out = (struct kvm_dax_mapping_identity) {
        .descriptor = h->phys_base + h->metadata_offset,
        .cookie_lo = h->cookie_lo, .cookie_hi = h->cookie_hi,
        .generation = h->generation, .owner_epoch = h->owner_epoch,
        .allocation_generation = h->allocation_generation,
        .ram_bytes = h->count << PAGE_SHIFT, .sealed = h->state == CPM_SEALED,
    };
    out->local_pfn = page < c->cfg.count && c->dram && c->dram[page >> 9] ?
        vmalloc_to_pfn(c->dram[page >> 9] + (page & 511) * PAGE_SIZE) : 0;
    }
    ret = 0;
done:
    mutex_unlock(&c->lock);
    return ret;
}

static long cache_metadata_range(struct cpm *c, void __user *user)
{
    struct cpm_cache_range q;
    u64 first, end, offset, start;
    int ret = -EINVAL;
    if (copy_from_user(&q, user, sizeof(q))) return -EFAULT;
    if (!boot_cpu_has(X86_FEATURE_CLFLUSHOPT) || !boot_cpu_has(X86_FEATURE_CLWB)) return -EOPNOTSUPP;
    if (q.reserved || q.operation > 1 || !q.bytes || q.offset >= 64ULL*1024*1024 ||
        q.bytes > 64ULL*1024*1024-q.offset) return -EINVAL;
    mutex_lock(&c->lock);
    /* cache_pages initialized and validated an immutable observer mapping.
     * No owner lease, shared epoch, or shared statistics are modified here. */
    if (!c->observer_base || !c->observer_map ||
        memcmp(&q, &c->observer, 6*sizeof(u64)) ||
        q.metadata_offset > q.device_bytes || 64ULL*1024*1024 > q.device_bytes-q.metadata_offset)
        goto out;
    first = q.offset & ~63ULL;
    end = ALIGN(q.offset+q.bytes,64);
    q.lines = 0; start = ktime_get_ns();
    for (offset=first;offset<end;offset+=64) {
        void *address = c->observer_base+q.metadata_offset+offset;
        if (q.operation) asm volatile("clflushopt (%0)" : : "r" (address) : "memory");
        else asm volatile("clwb (%0)" : : "r" (address) : "memory");
        q.lines++;
    }
    if (q.operation) asm volatile("mfence" : : : "memory");
    else asm volatile("sfence" : : : "memory");
    q.elapsed_ns=ktime_get_ns()-start;
    ret=copy_to_user(user,&q,sizeof(q)) ? -EFAULT : 0;
out:
    mutex_unlock(&c->lock);
    return ret;
}

/* Caller VMM has stopped vCPUs and drained I/O. mmap write excludes host
 * faults; notifier invalidation removes KVM leaves before a local page is freed. */
static long tier_chunk(struct cpm *c, void __user *user)
{
    struct cpm_tier q;
    DECLARE_BITMAP(dirty, 512);
    void *local = NULL;
    u64 *pfns = NULL;
    struct vm_area_struct *vma;
    u64 page, first, start;
    int ret = -EINVAL;
    if (copy_from_user(&q, user, sizeof(q))) return -EFAULT;
    if (q.dram > 2 || q.reserved || !boot_cpu_has(X86_FEATURE_CLWB)) return -EINVAL;
    if (!q.dram && copy_from_user(dirty, u64_to_user_ptr(q.dirty), sizeof(dirty))) return -EFAULT;
    if (q.dram == 1) {
        local = vzalloc(SZ_2M);
        if (!local) return -ENOMEM;
    }
    mmap_write_lock(current->mm);
    mutex_lock(&c->lock);
    vma = c->owner_vma;
    if (!c->ready || !c->header || !owns(c) || c->state->state != CPM_ACTIVE ||
        !vma || vma->vm_mm != current->mm || c->cfg.count % 512 ||
        q.chunk >= c->cfg.count / 512) goto out;
    if (q.dram == 2) {
        if (!c->dram || !c->dram[q.chunk]) goto out;
        pfns = kmalloc_array(512, sizeof(u64), GFP_KERNEL);
        if (!pfns) { ret = -ENOMEM; goto out; }
        for (page = 0; page < 512; page++)
            pfns[page] = vmalloc_to_pfn(c->dram[q.chunk] + page * PAGE_SIZE);
        ret = 0;
        goto out;
    }
    if (!c->dram) {
        c->dram = kvcalloc(c->cfg.count / 512, sizeof(void *), GFP_KERNEL);
        if (!c->dram) { ret = -ENOMEM; goto out; }
    }
    if (!!c->dram[q.chunk] == !!q.dram) goto out;
    first = q.chunk * 512; q.copied = 0; start = ktime_get_ns();
    if (q.dram) {
        for (page = 0; page < 512; page++)
            memcpy(local + page * PAGE_SIZE, c->base + c->map[first + page].offset, PAGE_SIZE);
        q.copied = 512;
    } else {
        for_each_set_bit(page, dirty, 512) {
            u64 line;
            void *home = c->base + c->map[first + page].offset;
            memcpy(home, c->dram[q.chunk] + page * PAGE_SIZE, PAGE_SIZE);
            for (line = 0; line < PAGE_SIZE; line += 64)
                asm volatile("clwb (%0)" : : "r" (home + line) : "memory");
            q.copied++;
        }
        asm volatile("sfence" : : : "memory");
    }
    /* Zap calls KVM's notifier while lock ordering stays mmap -> cpm -> MMU. */
    zap_vma_ptes(vma, vma->vm_start + first * PAGE_SIZE, SZ_2M);
    if (q.dram) { c->dram[q.chunk] = local; local = NULL; }
    else { local = c->dram[q.chunk]; c->dram[q.chunk] = NULL; }
    q.elapsed_ns = ktime_get_ns() - start;
    ret = 0;
out:
    mutex_unlock(&c->lock);
    mmap_write_unlock(current->mm);
    vfree(local);
    /* A user fault may acquire mmap_lock; never copy while holding it. */
    if (!ret && pfns && copy_to_user(u64_to_user_ptr(q.dirty), pfns, 512 * sizeof(u64))) ret = -EFAULT;
    kfree(pfns);
    return ret ? ret : copy_to_user(user, &q, sizeof(q)) ? -EFAULT : 0;
}

static long cpm_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct cpm *c = file->private_data;
    void __user *user = (void __user *)arg;
    long ret = 0;
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (cmd == CPM_TIER) return tier_chunk(c, user);
    if (cmd == CPM_CACHE_INIT_STATS) {
        u64 lines;
        mutex_lock(&c->lock);
        if (!c->observer_base) ret = -ENOENT;
        lines = c->observer_init_inv_lines;
        mutex_unlock(&c->lock);
        return ret ? ret : copy_to_user(user, &lines, sizeof(lines)) ? -EFAULT : 0;
    }
    if (cmd == CPM_CACHE_RANGE) return cache_metadata_range(c,user);
    if (cmd == CPM_CACHE || cmd == CPM_WRITEBACK) return cache_pages(c,user,cmd==CPM_WRITEBACK);
    if (cmd == CPM_PARK) { c->local_owner=false; return 0; }
    if (cmd == CPM_CREATE || cmd == CPM_IMPORT) {
        mutex_lock(&c->lock);
        ret = cmd == CPM_CREATE ? create_persistent(c, user) : import_persistent(c, user);
        mutex_unlock(&c->lock);
        return ret;
    }
    if (!smp_load_acquire(&c->ready))
        return -EINVAL;
    if (c->header && !c->local_owner) return -ESTALE;
    if (c->header && !owns(c)) return -ESTALE;
    if (cmd == CPM_STATUS || cmd == CPM_SEAL) {
        struct cpm_handle h;
        if (!c->header) return -EOPNOTSUPP;
        mutex_lock(&c->lock);
        if (!owns(c) || c->state->state == CPM_CLOSED) { ret = -ESTALE; goto status_done; }
        if (cmd == CPM_SEAL && c->dram) {
            u64 chunk;
            for (chunk = 0; chunk < c->cfg.count / 512; chunk++)
                if (c->dram[chunk]) { ret = -EBUSY; goto status_done; }
        }
        if (cmd == CPM_SEAL && c->state->state == CPM_ACTIVE)
            smp_store_release(&c->state->state, CPM_SEALED);

        h = *c->state;
status_done:
        mutex_unlock(&c->lock);
        if (ret) return ret;
        return copy_to_user(user, &h, sizeof(h)) ? -EFAULT : 0;
    }


    return -ENOTTY;
}

static int cpm_open(struct inode *inode, struct file *file)
{
    struct cpm *c;
    if (!capable(CAP_SYS_RAWIO)) return -EPERM;
    c = kzalloc(sizeof(*c), GFP_KERNEL);
    if (!c) return -ENOMEM;
    mutex_init(&c->lock);
    c->state = &c->local;
    file->private_data = c;
    return 0;
}
static int cpm_release(struct inode *inode, struct file *file)
{
    struct cpm *c = file->private_data;
    if (c->dram) {
        u64 chunk;
        for (chunk = 0; chunk < c->cfg.count / 512; chunk++) vfree(c->dram[chunk]);
        kvfree(c->dram);
    }
    kvfree(c->observer_map);
    if (c->observer_base) memunmap(c->observer_base);
    if (c->header) {
        if (c->local_owner && owns(c))
            cmpxchg(&c->state->state, CPM_ACTIVE, CPM_CLOSED);
    } else {
        kvfree(c->map);
    }
    if (c->base) memunmap(c->base);
    kfree(c);
    return 0;
}
static const struct file_operations cpm_fops = {
    .owner = THIS_MODULE, .open = cpm_open, .release = cpm_release,
    .unlocked_ioctl = cpm_ioctl, .mmap = cpm_mmap, .llseek = no_llseek,
};
static struct miscdevice cpm_device = {
    .minor = MISC_DYNAMIC_MINOR, .name = "cxl_pool", .fops = &cpm_fops, .mode = 0600,
};
static int __init cpm_init(void) {
    int ret = kvm_dax_register_mapping_provider(&cpm_vm_ops, mapping_identity);
    if (ret) return ret;
    ret = misc_register(&cpm_device);
    if (ret) kvm_dax_unregister_mapping_provider(&cpm_vm_ops);
    return ret;
}
static void __exit cpm_exit(void) {
    misc_deregister(&cpm_device);
    kvm_dax_unregister_mapping_provider(&cpm_vm_ops);
}
module_init(cpm_init);
module_exit(cpm_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Experimental VM-private CXL logical RAM with owner epochs");
