// SPDX-License-Identifier: GPL-2.0
/*
 * Cooperative guest authority for paused-host page pooling.
 *
 * No file data is read or faulted in. Only resident, clean, uptodate, order-0
 * folios of plain files on a read-only ext4/block device are exported. A folio
 * reference and its lock remain held until explicit release or FD close.
 * The lock excludes migration/removal; the reference excludes physical reuse.
 * A permanent status page invalidates exported records before cleanup begins.
 *
 * This is a trusted guest-kernel protocol, not attestation of a hostile guest.
 * Host policy must match image identity and inode/index against frozen extents,
 * and validate the ACTIVE status and digest while all vCPUs are paused.
 */
#include <crypto/sha2.h>
#include <linux/blkdev.h>
#include <linux/capability.h>
#include <linux/file.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pagemap.h>
#include <linux/random.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include "guest-pool-uapi.h"

struct guest_pool_session {
	struct mutex mutex;
	struct guest_pool_prepare *prepared;
	struct file *files[GUEST_POOL_MAX_FILES];
	struct folio **folios;
	struct guest_pool_record *records;
	unsigned int held;
	bool active;
};

static atomic_t guest_pool_opened = ATOMIC_INIT(0);
static struct guest_pool_status *guest_pool_status_page;

static void guest_pool_drop(struct guest_pool_session *s)
{
	unsigned int i;

	/* Publish invalidity before permitting migration, reclaim or frame reuse. */
	smp_store_release(&guest_pool_status_page->state, GUEST_POOL_INACTIVE);
	smp_mb();
	guest_pool_status_page->release_mono_ns = ktime_get_ns();
	guest_pool_status_page->transition_sequence++;
	for (i = 0; i < s->held; i++) {
		folio_unlock(s->folios[i]);
		folio_put(s->folios[i]);
	}
	for (i = 0; i < GUEST_POOL_MAX_FILES; i++) {
		if (s->files[i])
			fput(s->files[i]);
		s->files[i] = NULL;
	}
	kvfree(s->folios);
	kvfree(s->records);
	kfree(s->prepared);
	s->folios = NULL;
	s->records = NULL;
	s->prepared = NULL;
	s->held = 0;
	s->active = false;
}

static int guest_pool_open(struct inode *inode, struct file *file)
{
	struct guest_pool_session *s;

	if (!ns_capable(&init_user_ns, CAP_SYS_ADMIN))
		return -EPERM;
	if (atomic_cmpxchg(&guest_pool_opened, 0, 1))
		return -EBUSY;
	s = kzalloc(sizeof(*s), GFP_KERNEL);
	if (!s) {
		atomic_set(&guest_pool_opened, 0);
		return -ENOMEM;
	}
	mutex_init(&s->mutex);
	file->private_data = s;
	return nonseekable_open(inode, file);
}

static int guest_pool_close(struct inode *inode, struct file *file)
{
	struct guest_pool_session *s = file->private_data;

	mutex_lock(&s->mutex);
	guest_pool_drop(s);
	mutex_unlock(&s->mutex);
	kfree(s);
	atomic_set(&guest_pool_opened, 0);
	return 0;
}

static int guest_pool_validate_file(struct guest_pool_session *s, unsigned int slot)
{
	struct guest_pool_file *request = &s->prepared->files[slot];
	struct guest_pool_file_identity *id = &request->identity;
	struct inode *inode;
	struct super_block *sb;
	unsigned int i;

	if (request->reserved || request->fd < 0 || !request->page_count ||
	    request->start_page > ULONG_MAX - request->page_count)
		return -EINVAL;
	s->files[slot] = fget(request->fd);
	if (!s->files[slot])
		return -EBADF;
	inode = file_inode(s->files[slot]);
	sb = inode->i_sb;
	if (!(s->files[slot]->f_mode & FMODE_READ) ||
	    (s->files[slot]->f_mode & FMODE_WRITE) || !S_ISREG(inode->i_mode) ||
	    strcmp(sb->s_type->name, "ext4") || !sb_rdonly(sb) ||
	    !sb->s_bdev || !bdev_read_only(sb->s_bdev) || sb->s_blocksize != PAGE_SIZE ||
	    IS_DAX(inode) || IS_ENCRYPTED(inode) || IS_VERITY(inode))
		return -EOPNOTSUPP;
	for (i = 0; i < slot; i++)
		if (file_inode(s->files[i]) == inode)
			return -EINVAL;
	memset(id, 0, sizeof(*id));
	id->inode = inode->i_ino;
	id->file_bytes = i_size_read(inode);
	id->start_page = request->start_page;
	id->page_count = request->page_count;
	id->inode_generation = inode->i_generation;
	id->file_slot = slot;
	memcpy(id->fs_uuid, sb->s_uuid.b, sizeof(id->fs_uuid));
	id->dev_major = MAJOR(sb->s_dev);
	id->dev_minor = MINOR(sb->s_dev);
	return 0;
}

static void guest_pool_scan_file(struct guest_pool_session *s, unsigned int slot)
{
	struct guest_pool_file *request = &s->prepared->files[slot];
	struct guest_pool_stats *stats = &s->prepared->stats;
	struct inode *inode = file_inode(s->files[slot]);
	struct address_space *mapping = inode->i_mapping;
	u64 end = request->start_page + request->page_count;
	u64 full_pages = request->identity.file_bytes >> PAGE_SHIFT;
	u64 index;

	for (index = request->start_page; index < end; index++) {
		struct guest_pool_record *record;
		struct folio *folio;
		sector_t block = index;

		stats->requested++;
		if (index >= full_pages) {
			stats->beyond_full_pages++;
			continue;
		}
		/* No FGP_CREAT/READ: a cache miss never instantiates a data folio. */
		folio = filemap_get_folio(mapping, index);
		if (IS_ERR(folio)) {
			stats->absent++;
			continue;
		}
		if (folio_test_large(folio)) {
			stats->large++;
			goto put;
		}
		/* bmap may fetch filesystem metadata, never the selected file data.
		 * It is done before locking, since ext4_bmap may wait on writeback.
		 * Read-only device + superblock keep the block identity immutable.
		 */
		if (bmap(inode, &block)) {
			stats->unsupported_block++;
			goto put;
		}
		if (!block) {
			stats->hole_or_unwritten++;
			goto put;
		}
		if (!folio_trylock(folio)) {
			stats->busy++;
			goto put;
		}
		if (folio_mapping(folio) != mapping || folio->index != index ||
		    folio_test_large(folio)) {
			stats->mapping_changed++;
			goto unlock;
		}
		if (!folio_test_uptodate(folio)) {
			stats->not_uptodate++;
			goto unlock;
		}
		if (folio_test_dirty(folio)) {
			stats->dirty++;
			goto unlock;
		}
		if (folio_test_writeback(folio)) {
			stats->writeback++;
			goto unlock;
		}
		record = &s->records[s->held];
		record->file_slot = slot;
		record->flags = GUEST_POOL_RECORD_FLAGS;
		record->file_page = index;
		record->gpa = (u64)folio_pfn(folio) << PAGE_SHIFT;
		record->disk_offset = (u64)block << PAGE_SHIFT;
		s->folios[s->held++] = folio;
		stats->selected++;
		cond_resched();
		continue;
unlock:
		folio_unlock(folio);
put:
		folio_put(folio);
		cond_resched();
	}
}

static void guest_pool_digest(struct guest_pool_session *s)
{
	struct guest_pool_prepare *p = s->prepared;
	struct guest_pool_digest_header header = {
		.version = GUEST_POOL_ABI, .nr_files = p->nr_files,
		.record_count = s->held, .session_cookie = p->session_cookie,
		.vm_epoch = p->vm_epoch, .plan_generation = p->plan_generation,
	};
	struct sha256_state hash;
	unsigned int i;

	memcpy(header.image_id, p->image_id, sizeof(header.image_id));
	sha256_init(&hash);
	sha256_update(&hash, GUEST_POOL_DIGEST_DOMAIN, GUEST_POOL_DIGEST_DOMAIN_BYTES);
	sha256_update(&hash, (const u8 *)&header, sizeof(header));
	for (i = 0; i < p->nr_files; i++)
		sha256_update(&hash, (const u8 *)&p->files[i].identity, sizeof(p->files[i].identity));
	sha256_update(&hash, (const u8 *)s->records, s->held * sizeof(*s->records));
	sha256_final(&hash, p->records_sha256);
}

static long guest_pool_prepare(struct guest_pool_session *s, void __user *user)
{
	struct guest_pool_prepare *p;
	u64 requested = 0;
	unsigned int i;
	int error;

	if (s->active)
		return -EBUSY;
	p = memdup_user(user, sizeof(*p));
	if (IS_ERR(p))
		return PTR_ERR(p);
	if (p->version != GUEST_POOL_ABI || p->flags ||
	    !p->plan_generation || !p->nr_files || p->nr_files > GUEST_POOL_MAX_FILES ||
	    !p->max_records || p->max_records > GUEST_POOL_MAX_PAGES ||
	    !memchr_inv(p->image_id, 0, sizeof(p->image_id))) {
		kfree(p);
		return -EINVAL;
	}
	for (i = 0; i < p->nr_files; i++) {
		if (p->files[i].page_count > p->max_records - requested) {
			kfree(p);
			return -E2BIG;
		}
		requested += p->files[i].page_count;
	}
	if (!requested) {
		kfree(p);
		return -EINVAL;
	}
	memset(&p->stats, 0, sizeof(p->stats));
	memset(p->records_sha256, 0, sizeof(p->records_sha256));
	memset(&p->files[p->nr_files], 0,
	       (GUEST_POOL_MAX_FILES - p->nr_files) * sizeof(p->files[0]));
	p->session_cookie = get_random_u64();
	if (!p->session_cookie)
		p->session_cookie = 1;
	p->status_gpa = virt_to_phys(guest_pool_status_page);
	s->prepared = p;
	s->folios = kvcalloc(requested, sizeof(*s->folios), GFP_KERNEL);
	s->records = kvcalloc(requested, sizeof(*s->records), GFP_KERNEL);
	if (!s->folios || !s->records) {
		error = -ENOMEM;
		goto fail;
	}
	smp_store_release(&guest_pool_status_page->state, GUEST_POOL_PREPARING);
	for (i = 0; i < p->nr_files; i++) {
		error = guest_pool_validate_file(s, i);
		if (error)
			goto fail;
	}
	for (i = 0; i < p->nr_files; i++)
		guest_pool_scan_file(s, i);
	guest_pool_digest(s);
	guest_pool_status_page->session_cookie = p->session_cookie;
	guest_pool_status_page->vm_epoch = p->vm_epoch;
	guest_pool_status_page->plan_generation = p->plan_generation;
	guest_pool_status_page->record_count = s->held;
	guest_pool_status_page->nr_files = p->nr_files;
	guest_pool_status_page->record_bytes = sizeof(*s->records);
	memcpy(guest_pool_status_page->image_id, p->image_id, sizeof(p->image_id));
	memcpy(guest_pool_status_page->records_sha256, p->records_sha256, sizeof(p->records_sha256));
	guest_pool_status_page->prepare_mono_ns = ktime_get_ns();
	guest_pool_status_page->release_mono_ns = 0;
	guest_pool_status_page->transition_sequence++;
	s->active = true;
	smp_store_release(&guest_pool_status_page->state, GUEST_POOL_ACTIVE);
	if (copy_to_user(user, p, sizeof(*p))) {
		error = -EFAULT;
		goto fail;
	}
	return 0;
fail:
	guest_pool_drop(s);
	return error;
}

static bool guest_pool_matches(struct guest_pool_session *s, u64 cookie, u64 epoch, u64 generation)
{
	return s->active && cookie == s->prepared->session_cookie &&
		epoch == s->prepared->vm_epoch && generation == s->prepared->plan_generation;
}

static long guest_pool_read(struct guest_pool_session *s, void __user *user)
{
	struct guest_pool_read request;
	u64 count;

	if (copy_from_user(&request, user, sizeof(request)))
		return -EFAULT;
	if (request.version != GUEST_POOL_ABI || request.reserved ||
	    !request.capacity || request.capacity > GUEST_POOL_MAX_PAGES)
		return -EINVAL;
	if (!guest_pool_matches(s, request.session_cookie, request.vm_epoch, request.plan_generation))
		return -ESTALE;
	if (request.offset > s->held)
		return -ERANGE;
	count = min_t(u64, request.capacity, s->held - request.offset);
	if (copy_to_user(u64_to_user_ptr(request.records_ptr), s->records + request.offset,
			 count * sizeof(*s->records)))
		return -EFAULT;
	request.returned = count;
	request.total = s->held;
	return copy_to_user(user, &request, sizeof(request)) ? -EFAULT : 0;
}

static long guest_pool_release(struct guest_pool_session *s, void __user *user)
{
	struct guest_pool_release request;

	if (copy_from_user(&request, user, sizeof(request)))
		return -EFAULT;
	if (request.version != GUEST_POOL_ABI ||
	    (request.disposition != GUEST_POOL_COMMIT && request.disposition != GUEST_POOL_ABORT))
		return -EINVAL;
	if (!guest_pool_matches(s, request.session_cookie, request.vm_epoch, request.plan_generation))
		return -ESTALE;
	request.released = s->held;
	guest_pool_drop(s);
	return copy_to_user(user, &request, sizeof(request)) ? -EFAULT : 0;
}

static long guest_pool_ioctl(struct file *file, unsigned int command, unsigned long arg)
{
	struct guest_pool_session *s = file->private_data;
	void __user *user = (void __user *)arg;
	long result;

	if (mutex_lock_interruptible(&s->mutex))
		return -ERESTARTSYS;
	switch (command) {
	case GUEST_POOL_PREPARE:
		result = guest_pool_prepare(s, user);
		break;
	case GUEST_POOL_READ:
		result = guest_pool_read(s, user);
		break;
	case GUEST_POOL_RELEASE:
		result = guest_pool_release(s, user);
		break;
	case GUEST_POOL_STATUS:
		result = copy_to_user(user, guest_pool_status_page,
				      sizeof(*guest_pool_status_page)) ? -EFAULT : 0;
		break;
	default:
		result = -ENOTTY;
	}
	mutex_unlock(&s->mutex);
	return result;
}

static const struct file_operations guest_pool_operations = {
	.owner = THIS_MODULE,
	.open = guest_pool_open,
	.release = guest_pool_close,
	.unlocked_ioctl = guest_pool_ioctl,
	.llseek = no_llseek,
};

static struct miscdevice guest_pool_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "guest-pool",
	.fops = &guest_pool_operations,
	.mode = 0600,
};

static int __init guest_pool_init(void)
{
	int error;

	BUILD_BUG_ON(PAGE_SIZE != 4096);
	BUILD_BUG_ON(sizeof(struct guest_pool_record) != 32);
	BUILD_BUG_ON(sizeof(struct guest_pool_file_identity) != 64);
	BUILD_BUG_ON(sizeof(struct guest_pool_digest_header) != 72);
	BUILD_BUG_ON(sizeof(struct guest_pool_status) != 208);
	BUILD_BUG_ON(offsetof(struct guest_pool_status, records_sha256) != 88);
	guest_pool_status_page = (void *)get_zeroed_page(GFP_KERNEL);
	if (!guest_pool_status_page)
		return -ENOMEM;
	guest_pool_status_page->magic = GUEST_POOL_STATUS_MAGIC;
	guest_pool_status_page->version = GUEST_POOL_ABI;
	error = misc_register(&guest_pool_device);
	if (error) {
		free_page((unsigned long)guest_pool_status_page);
		guest_pool_status_page = NULL;
	}
	return error;
}
device_initcall(guest_pool_init);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Bounded read-only ext4 resident-folio authority for paused-host pooling");
