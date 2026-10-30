// SPDX-License-Identifier: GPL-2.0
/* Experimental, privileged CXL-backed logical RAM. One execution owner per fd.
 * Canonical data never becomes writable. Both CPU and host writes take the
 * same PFN write fault and receive a private CXL frame before they may proceed.
 * Private allocation credits are assigned by the controller; no distributed
 * allocator metadata or cross-kernel locks are introduced here.
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
#include "pool_memory_uapi.h"

#define CPM_MAX_PAGES (1UL << 20)
struct cpm {
    struct mutex lock;
    struct cpm_config cfg;
    struct cpm_entry *map;
    u64 *credits;
    u64 ncredits, generation, cow_pages, discard_pages, faults, exhausted;
    void *base;
    bool ready, mapped;
    struct vm_area_struct *owner_vma;
};

static bool cpm_in_range(u64 offset, u64 start, u64 bytes)
{
    return offset >= start && offset - start < bytes;
}

static bool valid_offset(struct cpm *c, u64 offset, bool shared)
{
    if (offset & (PAGE_SIZE - 1) || offset >= c->cfg.device_bytes)
        return false;
    return cpm_in_range(offset, c->cfg.canonical_start, c->cfg.canonical_bytes) == shared;
}

static int cmp_u64(const void *a, const void *b)
{
    u64 x = *(const u64 *)a, y = *(const u64 *)b;
    return (x > y) - (x < y);
}

static int validate_private(struct cpm *c, const u64 *more, u64 nmore)
{
    u64 i, n = 0, *all;
    int ret = 0;
    if (nmore > CPM_MAX_PAGES || c->cfg.count + c->ncredits + nmore > 3 * CPM_MAX_PAGES)
        return -E2BIG;
    all = kvmalloc_array(c->cfg.count + c->ncredits + nmore, sizeof(*all), GFP_KERNEL);
    if (!all)
        return -ENOMEM;
    for (i = 0; i < c->cfg.count; i++) {
        if (c->map[i].flags & ~CPM_SHARED ||
            !valid_offset(c, c->map[i].offset, !!(c->map[i].flags & CPM_SHARED))) {
            ret = -EINVAL;
            goto out;
        }
        if (!(c->map[i].flags & CPM_SHARED))
            all[n++] = c->map[i].offset;
    }
    for (i = 0; i < c->ncredits + nmore; i++) {
        u64 offset = i < c->ncredits ? c->credits[i] : more[i - c->ncredits];
        if (!valid_offset(c, offset, false)) {
            ret = -EINVAL;
            goto out;
        }
        all[n++] = offset;
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
    u64 index = vmf->pgoff, offset;
    pgprot_t prot = vmf->vma->vm_page_prot;
    vm_fault_t ret;
    bool write = vmf->flags & FAULT_FLAG_WRITE;
    mutex_lock(&c->lock);
    if (index >= c->cfg.count) { ret = VM_FAULT_SIGBUS; goto out; }
    c->faults++;
    if (write && c->map[index].flags & CPM_SHARED) {
        if (!c->ncredits) {
            c->exhausted++;
            ret = VM_FAULT_SIGBUS;
            goto out;
        }
        offset = c->credits[--c->ncredits];
        memcpy(c->base + offset, c->base + c->map[index].offset, PAGE_SIZE);
        /* A read-only canonical PTE cannot authorize any writer. The context
         * lock holds competing faults until the old PTE is invalidated below. */
        c->map[index].offset = offset;
        c->map[index].flags = 0;
        c->generation++;
        c->cow_pages++;
    }
    if (write)
        zap_vma_ptes(vmf->vma, vmf->address & PAGE_MASK, PAGE_SIZE);
    if (c->map[index].flags & CPM_SHARED)
        prot = __pgprot(pgprot_val(prot) & ~_PAGE_RW);
    else if (vmf->vma->vm_flags & VM_WRITE)
        /* pfn_mkwrite triggers write-notify, which clears RW in vm_page_prot.
         * Our private CXL frame is exclusively owned, so install writable. */
        prot = __pgprot(pgprot_val(prot) | _PAGE_RW);
    ret = vmf_insert_pfn_prot(vmf->vma, vmf->address & PAGE_MASK,
                            PHYS_PFN(c->cfg.phys_base + c->map[index].offset), prot);
out:
    mutex_unlock(&c->lock);
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
    .pfn_mkwrite = cpm_fault,
    .close = cpm_vma_close,
    .may_split = cpm_may_split,
    .mremap = cpm_mremap,
};

static int cpm_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct cpm *c = file->private_data;
    u64 first = vma->vm_pgoff, count = vma_pages(vma);
    mutex_lock(&c->lock);
    /* A single whole-RAM mapping per context. Otherwise another alias could
     * retain a stale canonical PTE after this VMA takes CoW. */
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

static long configure(struct cpm *c, void __user *arg)
{
    struct cpm_config cfg;
    int ret;
    if (c->map || copy_from_user(&cfg, arg, sizeof(cfg)))
        return c->map ? -EBUSY : -EFAULT;
    if (cfg.abi != CPM_ABI || !cfg.count || cfg.count > CPM_MAX_PAGES ||
        cfg.credit_count > CPM_MAX_PAGES || !cfg.device_bytes ||
        (cfg.phys_base | cfg.device_bytes | cfg.canonical_start | cfg.canonical_bytes) & (PAGE_SIZE - 1) ||
        cfg.phys_base + cfg.device_bytes < cfg.phys_base ||
        cfg.canonical_start > cfg.device_bytes || cfg.canonical_bytes > cfg.device_bytes - cfg.canonical_start)
        return -EINVAL;
    c->cfg = cfg;
    c->map = kvmalloc_array(cfg.count, sizeof(*c->map), GFP_KERNEL);
    c->credits = kvmalloc_array(CPM_MAX_PAGES, sizeof(*c->credits), GFP_KERNEL);
    if (!c->map || !c->credits) { ret = -ENOMEM; goto fail; }
    if (copy_from_user(c->map, u64_to_user_ptr(cfg.entries), cfg.count * sizeof(*c->map)) ||
        copy_from_user(c->credits, u64_to_user_ptr(cfg.credits), cfg.credit_count * sizeof(*c->credits))) {
        ret = -EFAULT;
        goto fail;
    }
    c->ncredits = cfg.credit_count;
    ret = validate_private(c, NULL, 0);
    if (ret)
        goto fail;
    c->base = memremap(cfg.phys_base, cfg.device_bytes, MEMREMAP_WB);
    if (!c->base) { ret = -ENXIO; goto fail; }
    c->generation = 1;
    smp_store_release(&c->ready, true);
    return 0;
fail:
    kvfree(c->credits); c->credits = NULL;
    kvfree(c->map); c->map = NULL;
    c->ncredits = 0;
    return ret;
}

static long cpm_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct cpm *c = file->private_data;
    void __user *user = (void __user *)arg;
    long ret = 0;
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (cmd == CPM_CONFIG) {
        mutex_lock(&c->lock);
        ret = configure(c, user);
        mutex_unlock(&c->lock);
        return ret;
    }
    if (!smp_load_acquire(&c->ready))
        return -EINVAL;
    if (cmd == CPM_AUDIT) {
        struct cpm_audit a;
        struct cpm_pte *out;
        struct vm_area_struct *vma;
        u64 i;
        if (copy_from_user(&a, user, sizeof(a))) return -EFAULT;
        if (!a.count || a.count > c->cfg.count || a.address & (PAGE_SIZE - 1) ||
            a.address + a.count * PAGE_SIZE < a.address) return -EINVAL;
        out = kvcalloc(a.count, sizeof(*out), GFP_KERNEL);
        if (!out) return -ENOMEM;
        mmap_read_lock(current->mm);
        mutex_lock(&c->lock);
        vma = find_vma(current->mm, a.address);
        if (!vma || vma != c->owner_vma || vma->vm_start > a.address ||
            vma->vm_end < a.address + a.count * PAGE_SIZE) {
            ret = -EINVAL;
        } else for (i = 0; i < a.count; i++) {
            pte_t *pte, value;
            spinlock_t *ptl;
            if (follow_pte(current->mm, a.address + i * PAGE_SIZE, &pte, &ptl)) continue;
            value = ptep_get(pte);
            if (pte_present(value)) {
                out[i].pfn = pte_pfn(value);
                out[i].flags = 1 | (pte_write(value) ? 2 : 0);
            }
            pte_unmap_unlock(pte, ptl);
        }
        mutex_unlock(&c->lock);
        mmap_read_unlock(current->mm);
        if (!ret && copy_to_user(u64_to_user_ptr(a.ptes), out, a.count * sizeof(*out))) ret = -EFAULT;
        kvfree(out);
        return ret;
    }
    if (cmd == CPM_QUERY) {
        struct cpm_query q;
        struct cpm_entry *map;
        u64 *credits;
        if (copy_from_user(&q, user, sizeof(q)))
            return -EFAULT;
        if (q.count < c->cfg.count || q.credit_count > CPM_MAX_PAGES)
            return -ENOSPC;
        map = kvmalloc_array(c->cfg.count, sizeof(*map), GFP_KERNEL);
        credits = kvmalloc_array(CPM_MAX_PAGES, sizeof(*credits), GFP_KERNEL);
        if (!map || !credits) { kvfree(map); kvfree(credits); return -ENOMEM; }
        mutex_lock(&c->lock);
        if (q.credit_count < c->ncredits) {
            ret = -ENOSPC;
        } else {
            q.count = c->cfg.count; q.credit_count = c->ncredits;
            q.generation = c->generation; q.cow_pages = c->cow_pages;
            q.discard_pages = c->discard_pages; q.faults = c->faults; q.exhausted = c->exhausted;
            memcpy(map, c->map, q.count * sizeof(*map));
            memcpy(credits, c->credits, q.credit_count * sizeof(*credits));
        }
        mutex_unlock(&c->lock);
        if (!ret && (copy_to_user(u64_to_user_ptr(q.entries), map, q.count * sizeof(*map)) ||
                     copy_to_user(u64_to_user_ptr(q.credits), credits, q.credit_count * sizeof(*credits)) ||
                     copy_to_user(user, &q, sizeof(q)))) ret = -EFAULT;
        kvfree(map); kvfree(credits);
        return ret;
    }
    if (cmd == CPM_ADD_CREDITS) {
        struct cpm_credits add;
        u64 *offsets;
        if (copy_from_user(&add, user, sizeof(add))) return -EFAULT;
        if (!add.count || add.count > CPM_MAX_PAGES) return -EINVAL;
        offsets = memdup_user(u64_to_user_ptr(add.offsets), add.count * sizeof(*offsets));
        if (IS_ERR(offsets)) return PTR_ERR(offsets);
        mutex_lock(&c->lock);
        if (c->ncredits + add.count > CPM_MAX_PAGES) ret = -ENOSPC;
        else ret = validate_private(c, offsets, add.count);
        if (!ret) {
            memcpy(c->credits + c->ncredits, offsets, add.count * sizeof(*offsets));
            c->ncredits += add.count; c->generation++;
        }
        mutex_unlock(&c->lock);
        kfree(offsets);
        return ret;
    }
    if (cmd == CPM_DISCARD) {
        struct cpm_range r;
        struct vm_area_struct *vma;
        u64 i, needed = 0;
        if (copy_from_user(&r, user, sizeof(r))) return -EFAULT;
        if (!r.count || r.first >= c->cfg.count || r.count > c->cfg.count - r.first ||
            r.address & (PAGE_SIZE - 1) || r.address + r.count * PAGE_SIZE < r.address)
            return -EINVAL;
        mmap_write_lock(current->mm);
        vma = find_vma(current->mm, r.address);
        if (!vma || vma->vm_start > r.address || vma->vm_end < r.address + r.count * PAGE_SIZE ||
            vma->vm_private_data != c || vma->vm_ops != &cpm_vm_ops ||
            vma->vm_pgoff + ((r.address - vma->vm_start) >> PAGE_SHIFT) != r.first) {
            ret = -EINVAL;
            goto unlock_mm;
        }
        mutex_lock(&c->lock);
        for (i = r.first; i < r.first + r.count; i++) needed += !!(c->map[i].flags & CPM_SHARED);
        if (needed > c->ncredits) { ret = -ENOSPC; goto unlock_ctx; }
        /* Guest free-page reporting has quarantined the pages before this
         * call, and the VMM excluded pending I/O. Invalidate before reuse. */
        zap_vma_ptes(vma, r.address, r.count * PAGE_SIZE);
        for (i = r.first; i < r.first + r.count; i++) {
            if (c->map[i].flags & CPM_SHARED) c->map[i].offset = c->credits[--c->ncredits];
            memset(c->base + c->map[i].offset, 0, PAGE_SIZE);
            c->map[i].flags = 0;
        }
        c->generation++; c->discard_pages += r.count;
unlock_ctx:
        mutex_unlock(&c->lock);
unlock_mm:
        mmap_write_unlock(current->mm);
        return ret;
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
    file->private_data = c;
    return 0;
}
static int cpm_release(struct inode *inode, struct file *file)
{
    struct cpm *c = file->private_data;
    if (c->base) memunmap(c->base);
    kvfree(c->map); kvfree(c->credits); kfree(c);
    return 0;
}
static const struct file_operations cpm_fops = {
    .owner = THIS_MODULE, .open = cpm_open, .release = cpm_release,
    .unlocked_ioctl = cpm_ioctl, .mmap = cpm_mmap, .llseek = no_llseek,
};
static struct miscdevice cpm_device = {
    .minor = MISC_DYNAMIC_MINOR, .name = "cxl_pool", .fops = &cpm_fops, .mode = 0600,
};
static int __init cpm_init(void) { return misc_register(&cpm_device); }
static void __exit cpm_exit(void) { misc_deregister(&cpm_device); }
module_init(cpm_init);
module_exit(cpm_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Experimental CXL data-pool logical RAM with CXL private CoW");
