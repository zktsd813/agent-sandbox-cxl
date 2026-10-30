/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kvm.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "pool_memory_uapi.h"

/* Destructive test for an OWNED, DISPOSABLE L1 before any sandbox starts.
 * Distinct from pool_memory_smoke.c's 3490/3500 MiB areas. Nothing in the VM
 * RAM/canonical/Bloom production ranges is touched. No latency claim is made.
 */
#define PAGE UINT64_C(4096)
#define MIB (UINT64_C(1024) * 1024)
#define CANONICAL_OFFSET (UINT64_C(3504) * MIB)
#define PRIVATE_OFFSET (UINT64_C(3494) * MIB)
#define PRIVATE_BYTES (8 * PAGE)
#define RAM_BYTES (2 * PAGE)
#define REQUIRE(x) do { if (!(x)) { fprintf(stderr, "POOL_MEMORY_KVM_FAIL line=%d check=%s errno=%d\n", __LINE__, #x, errno); exit(1); } } while (0)

struct outcome {
    const char *name;
    uint64_t before_pfn, after_pfn, private_offset;
    uint64_t faults, cow_pages, initial_present, read_exit_rip, write_exit_rip;
};

static void timeout_handler(int signal_number)
{
    (void)signal_number;
    const char message[] = "POOL_MEMORY_KVM_FAIL timeout\n";
    ssize_t ignored = write(STDERR_FILENO, message, sizeof(message) - 1);
    (void)ignored;
    _exit(124);
}

static uint64_t attribute(const char *path)
{
    char buffer[96], *end;
    FILE *stream = fopen(path, "r");
    REQUIRE(stream && fgets(buffer, sizeof(buffer), stream));
    REQUIRE(!fclose(stream));
    errno = 0;
    uint64_t value = strtoull(buffer, &end, 0);
    REQUIRE(!errno && end != buffer && (*end == '\n' || !*end));
    return value;
}

/* /proc/pagemap intentionally skips VM_PFNMAP. Inspect the actual PTE through
 * the mapper's read-only follow_pte ioctl; it never instantiates a PTE. */
static struct cpm_pte audit(int fd, const void *address)
{
    struct cpm_pte pte = {0};
    struct cpm_audit request = {.address = (uintptr_t)address, .count = 1,
        .ptes = (uintptr_t)&pte};
    REQUIRE(!ioctl(fd, CPM_AUDIT, &request));
    REQUIRE(!(pte.flags & ~UINT64_C(3)));
    REQUIRE((pte.flags & 1) || (!pte.flags && !pte.pfn));
    return pte;
}

static uint64_t pfn(int fd, const void *address, int writable)
{
    struct cpm_pte pte = audit(fd, address);
    REQUIRE(pte.flags == (writable ? 3U : 1U) && pte.pfn);
    return pte.pfn;
}

static unsigned char pattern(uint64_t index)
{
    return (unsigned char)(((index * 101U) ^ (index >> 7)) + 0x31);
}

static void check_page(volatile const unsigned char *page, int changed)
{
    for (uint64_t i = 0; i < PAGE; i++)
        REQUIRE(page[i] == (i || changed < 0 ? pattern(i) : changed));
}

static int configure(uint64_t base, uint64_t bytes, struct cpm_entry *entries,
                     uint64_t count, uint64_t *credits, uint64_t credit_count)
{
    int fd = open("/dev/cxl_pool", O_RDWR | O_CLOEXEC);
    REQUIRE(fd >= 0);
    struct cpm_config cfg = {.abi = CPM_ABI, .phys_base = base, .device_bytes = bytes,
        .canonical_start = CANONICAL_OFFSET, .canonical_bytes = PAGE,
        .count = count, .entries = (uintptr_t)entries,
        .credit_count = credit_count, .credits = (uintptr_t)credits};
    REQUIRE(!ioctl(fd, CPM_CONFIG, &cfg));
    return fd;
}

static struct cpm_query query(int fd, struct cpm_entry entries[2], uint64_t credits[1])
{
    struct cpm_query q = {.count = 2, .entries = (uintptr_t)entries,
        .credit_count = 1, .credits = (uintptr_t)credits};
    REQUIRE(!ioctl(fd, CPM_QUERY, &q));
    REQUIRE(q.count == 2 && q.credit_count <= 1);
    return q;
}

static struct kvm_regs run_halt(int vcpu, struct kvm_run *run, uint64_t expected_rip)
{
    REQUIRE(!ioctl(vcpu, KVM_RUN, 0));
    if (run->exit_reason != KVM_EXIT_HLT)
        fprintf(stderr, "POOL_MEMORY_KVM_FAIL exit_reason=%u internal_suberror=%u\n",
                run->exit_reason, run->internal.suberror);
    REQUIRE(run->exit_reason == KVM_EXIT_HLT);
    struct kvm_regs regs;
    REQUIRE(!ioctl(vcpu, KVM_GET_REGS, &regs));
    REQUIRE(regs.rip == expected_rip);
    return regs;
}

static struct outcome scenario(int kvm, uint64_t base, uint64_t bytes, unsigned int which,
                              volatile const unsigned char *canonical,
                              volatile const unsigned char *peer)
{
    struct outcome out = {.name = which ? "first_write_absent_pte" : "read_then_write_retained_spte"};
    uint64_t code_offset = PRIVATE_OFFSET + which * 4 * PAGE;
    uint64_t credit = code_offset + PAGE;
    struct cpm_entry initial[2] = {{code_offset, 0}, {CANONICAL_OFFSET, CPM_SHARED}};
    int fd = configure(base, bytes, initial, 2, &credit, 1);
    unsigned char *ram = mmap(NULL, RAM_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    REQUIRE(ram != MAP_FAILED);
    /* No data-HVA load, mincore fault, or guest read has occurred here. */
    REQUIRE(!audit(fd, ram + PAGE).flags);
    struct cpm_entry actual[2];
    uint64_t unused[1];
    struct cpm_query before = query(fd, actual, unused);
    REQUIRE(!before.faults && !before.cow_pages && before.credit_count == 1);
    REQUIRE(actual[1].flags == CPM_SHARED && actual[1].offset == CANONICAL_OFFSET);

    int vm = ioctl(kvm, KVM_CREATE_VM, 0);
    REQUIRE(vm >= 0 && !ioctl(vm, KVM_SET_TSS_ADDR, 0xfffbd000));
    struct kvm_userspace_memory_region region = {.slot = 0, .guest_phys_addr = 0,
        .memory_size = RAM_BYTES, .userspace_addr = (uintptr_t)ram};
    REQUIRE(!ioctl(vm, KVM_SET_USER_MEMORY_REGION, &region));
    int vcpu = ioctl(vm, KVM_CREATE_VCPU, 0);
    int run_bytes = ioctl(kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    REQUIRE(vcpu >= 0 && run_bytes > 0);
    struct kvm_run *run = mmap(NULL, (size_t)run_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, vcpu, 0);
    REQUIRE(run != MAP_FAILED);
    struct kvm_sregs sregs;
    REQUIRE(!ioctl(vcpu, KVM_GET_SREGS, &sregs));
    sregs.cs.base = 0;
    sregs.cs.selector = 0;
    REQUIRE(!ioctl(vcpu, KVM_SET_SREGS, &sregs));
    struct kvm_regs regs = {.rip = which ? 4 : 0, .rflags = 2};
    REQUIRE(!ioctl(vcpu, KVM_SET_REGS, &regs));
    if (!which) {
        regs = run_halt(vcpu, run, 4);
        REQUIRE((regs.rax & 0xff) == pattern(0));
        out.read_exit_rip = regs.rip;
        out.before_pfn = pfn(fd, ram + PAGE, 0);
        out.initial_present = 1;
        REQUIRE(out.before_pfn == (base + CANONICAL_OFFSET) / PAGE);
        struct cpm_query read = query(fd, actual, unused);
        REQUIRE(!read.cow_pages && read.credit_count == 1 && read.faults == 2);
        REQUIRE(actual[1].flags == CPM_SHARED && actual[1].offset == CANONICAL_OFFSET);
        /* Keep the same VM, memslot and vCPU: the read-installed EPT leaf is
         * retained. No KVM_SET_USER_MEMORY_REGION or data-PTE zap intervenes. */
    } else {
        REQUIRE(!audit(fd, ram + PAGE).flags);
    }
    regs = run_halt(vcpu, run, 10);
    out.write_exit_rip = regs.rip;
    struct cpm_query written = query(fd, actual, unused);
    REQUIRE(written.cow_pages == 1 && !written.credit_count && !written.exhausted);
    /* One private code-page fault, then either target write alone or target
     * read + write. A speculative target read would invalidate case two. */
    REQUIRE(written.faults == (which ? 2U : 3U));
    REQUIRE(actual[0].offset == code_offset && !actual[0].flags);
    REQUIRE(actual[1].offset == credit && !actual[1].flags);
    out.after_pfn = pfn(fd, ram + PAGE, 1);
    out.private_offset = actual[1].offset;
    REQUIRE(out.after_pfn == (base + credit) / PAGE);
    REQUIRE(out.after_pfn != (base + CANONICAL_OFFSET) / PAGE);
    check_page(ram + PAGE, 0x5a);
    check_page(canonical, -1);
    check_page(peer, -1);
    regs = run_halt(vcpu, run, 14);
    REQUIRE((regs.rax & 0xff) == 0x5a);
    (void)run_halt(vcpu, run, 20);
    regs = run_halt(vcpu, run, 24);
    REQUIRE((regs.rax & 0xff) == 0x6b);
    struct cpm_query final = query(fd, actual, unused);
    REQUIRE(final.cow_pages == 1 && !final.credit_count && !final.exhausted);
    REQUIRE(final.faults == written.faults && final.faults <= 8);
    REQUIRE(actual[1].offset == credit && !actual[1].flags);
    REQUIRE(pfn(fd, ram + PAGE, 1) == out.after_pfn);
    check_page(ram + PAGE, 0x6b);
    check_page(canonical, -1);
    check_page(peer, -1);
    out.faults = final.faults;
    out.cow_pages = final.cow_pages;
    REQUIRE(!munmap(run, (size_t)run_bytes) && !close(vcpu) && !close(vm));
    REQUIRE(!munmap(ram, RAM_BYTES) && !close(fd));
    return out;
}

int main(int argc, char **argv)
{
    REQUIRE(argc == 2 && !strcmp(argv[1], "OWNED_POOL_TEST"));
    REQUIRE(signal(SIGALRM, timeout_handler) != SIG_ERR);
    alarm(60);
    uint64_t base = attribute("/sys/bus/dax/devices/dax0.0/resource");
    uint64_t bytes = attribute("/sys/bus/dax/devices/dax0.0/size");
    REQUIRE(!(base % PAGE) && bytes >= CANONICAL_OFFSET + PAGE);
    int dax = open("/dev/dax0.0", O_RDWR | O_CLOEXEC);
    REQUIRE(dax >= 0);
    unsigned char *canonical = mmap(NULL, PAGE, PROT_READ | PROT_WRITE,
                                    MAP_SHARED, dax, CANONICAL_OFFSET);
    unsigned char *private = mmap(NULL, PRIVATE_BYTES, PROT_READ | PROT_WRITE,
                                  MAP_SHARED, dax, PRIVATE_OFFSET);
    REQUIRE(canonical != MAP_FAILED && private != MAP_FAILED);
    for (uint64_t i = 0; i < PAGE; i++)
        canonical[i] = pattern(i);
    memset(private, 0xcc, PRIVATE_BYTES);
    const unsigned char code[] = {
        0xa0, 0x00, 0x10, 0xf4,
        0xc6, 0x06, 0x00, 0x10, 0x5a, 0xf4,
        0xa0, 0x00, 0x10, 0xf4,
        0xc6, 0x06, 0x00, 0x10, 0x6b, 0xf4,
        0xa0, 0x00, 0x10, 0xf4,
    };
    memcpy(private, code, sizeof(code));
    memcpy(private + 4 * PAGE, code, sizeof(code));
    REQUIRE(!mprotect(canonical, PAGE, PROT_READ));
    REQUIRE(!mprotect(private, PRIVATE_BYTES, PROT_READ));
    struct cpm_entry shared = {CANONICAL_OFFSET, CPM_SHARED};
    int peer_fd = configure(base, bytes, &shared, 1, NULL, 0);
    volatile unsigned char *peer = mmap(NULL, PAGE, PROT_READ, MAP_SHARED, peer_fd, 0);
    REQUIRE(peer != MAP_FAILED);
    check_page(peer, -1);
    REQUIRE(pfn(peer_fd, (const void *)peer, 0) == (base + CANONICAL_OFFSET) / PAGE);
    int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    REQUIRE(kvm >= 0 && ioctl(kvm, KVM_GET_API_VERSION, 0) == 12);
    printf("POOL_MEMORY_KVM_CONFIG_JSON {\"canonical_offset\":%" PRIu64 ","
           "\"private_offset\":%" PRIu64 ",\"private_bytes\":%" PRIu64 ","
           "\"phys_base\":%" PRIu64 ",\"cases\":2}\n",
           CANONICAL_OFFSET, PRIVATE_OFFSET, PRIVATE_BYTES, base);
    fflush(stdout);
    struct outcome outcomes[2];
    for (unsigned int i = 0; i < 2; i++)
        outcomes[i] = scenario(kvm, base, bytes, i, canonical, peer);
    printf("POOL_MEMORY_KVM_SMOKE_JSON {\"result\":\"passed\",\"pte_evidence\":\"CPM_AUDIT follow_pte no-prefault\",\"canonical_bytes_checked\":4096,"
           "\"peer_bytes_checked\":4096,\"remaining_4095_bytes_preserved\":true,"
           "\"guest_readback\":true,\"second_write_no_extra_cow\":true,\"cases\":[");
    for (unsigned int i = 0; i < 2; i++) {
        struct outcome *o = &outcomes[i];
        printf("%s{\"mode\":\"%s\",\"initial_data_pte_present\":%s,\"before_pfn\":%" PRIu64 ","
               "\"private_pfn\":%" PRIu64 ",\"private_offset\":%" PRIu64 ",\"cow_pages\":%" PRIu64 ","
               "\"faults\":%" PRIu64 ",\"read_exit_rip\":%" PRIu64 ",\"write_exit_rip\":%" PRIu64 "}",
               i ? "," : "", o->name, o->initial_present ? "true" : "false", o->before_pfn,
               o->after_pfn, o->private_offset, o->cow_pages, o->faults, o->read_exit_rip, o->write_exit_rip);
    }
    puts("]}");
    REQUIRE(!close(kvm) && !munmap((void *)peer, PAGE) && !close(peer_fd));
    REQUIRE(!munmap(canonical, PAGE) && !munmap(private, PRIVATE_BYTES) && !close(dax));
    alarm(0);
    return 0;
}
