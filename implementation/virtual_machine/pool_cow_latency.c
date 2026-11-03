#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kvm.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Destructive diagnostic, only in an owned disposable L1. Own 64 KiB at
 * offset 3.5 GiB, outside the lab's RAM, canonical image and Bloom slots.
 * A sample writes ONE BYTE of ONE 4 KiB page. CoW preserves the other bytes.
 * KVM timings include KVM_RUN entry/HLT exit; they are not pure fault latency.
 * Setup, remapping (except separately reported detach_ns), PFN inspection,
 * validation and output are outside each write timer. No bulk-time division.
 */
#define PAGE 4096U
#define RAM_BYTES (16U * PAGE)
#define OWNED_OFFSET UINT64_C(3758096384)
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "LATENCY_FAIL line=%d check=%s errno=%d\n", __LINE__, #x, errno); exit(1); } } while (0)

static uint64_t now(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC_RAW, &ts));
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static uint64_t pfn(const unsigned char *p) {
    (void)*(volatile const unsigned char *)p;
    int fd = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
    uint64_t entry;
    CHECK(fd >= 0);
    CHECK(pread(fd, &entry, sizeof(entry), (off_t)((uintptr_t)p / PAGE * 8)) == sizeof(entry));
    CHECK(!close(fd));
    CHECK((entry & (UINT64_C(1) << 63)) && !(entry & (UINT64_C(1) << 62)));
    entry &= (UINT64_C(1) << 55) - 1;
    CHECK(entry);
    return entry;
}

static unsigned char pattern(size_t i) {
    return (unsigned char)(((i * 101U) ^ (i >> 7)) + 0x31);
}

static void verify(const unsigned char *p, int zero, int written) {
    for (size_t i = 0; i < PAGE; i++)
        CHECK(p[i] == (written && !i ? 0x5a : zero ? 0 : pattern(i)));
}

static uint64_t bind_page(unsigned char *p, int fd, off_t offset, int mode) {
    /* 0: read-faulted shared canonical; 1: already-private populated page;
     * 2: shared canonical detached to a fresh zero anonymous mapping. */
    CHECK(mmap(p, PAGE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_FIXED,
               fd, offset) == p);
    verify(p, 0, 0);
    if (!mode) return 0;
    uint64_t begin = now();
    CHECK(mmap(p, PAGE, PROT_READ | PROT_WRITE,
               MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) == p);
    uint64_t elapsed = now() - begin;
    if (mode == 1) {
        for (size_t i = 0; i < PAGE; i++) p[i] = pattern(i);
        return 0;
    }
    verify(p, 1, 0);
    return elapsed;
}

static void output(const char *kind, int mode, unsigned iteration,
                   uint64_t detach, uint64_t first, uint64_t second,
                   uint64_t before, uint64_t after, uint64_t canonical) {
    static const char *modes[] = {"canonical_cow", "already_private", "discard_zero"};
    printf("POOL_COW_LATENCY {\"kind\":\"%s\",\"mode\":\"%s\",\"iteration\":%u,"
           "\"page_bytes\":4096,\"written_bytes\":1,\"detach_ns\":%" PRIu64 ","
           "\"first_write_ns\":%" PRIu64 ",\"second_write_ns\":%" PRIu64 ","
           "\"before_pfn\":%" PRIu64 ",\"after_pfn\":%" PRIu64 ",\"canonical_pfn\":%" PRIu64 ","
           "\"remaining_4095_bytes_preserved\":true,\"peer_unchanged\":true,\"cpu\":%d,\"pass\":true}\n",
           kind, modes[mode], iteration, detach, first, second, before, after, canonical, sched_getcpu());
}

int main(int argc, char **argv) {
    CHECK(argc == 5 && !strcmp(argv[4], "OWNED_POOL_TEST"));
    char *end;
    errno = 0;
    uint64_t offset = strtoull(argv[2], &end, 10);
    CHECK(!errno && argv[2][0] && !*end && offset == OWNED_OFFSET);
    errno = 0;
    unsigned long count = strtoul(argv[3], &end, 10);
    CHECK(!errno && argv[3][0] && !*end && count >= 10 && count <= 1000);
    cpu_set_t allowed, chosen;
    CPU_ZERO(&allowed); CPU_ZERO(&chosen);
    CHECK(!sched_getaffinity(0, sizeof(allowed), &allowed));
    int cpu_id = -1;
    for (int i = 0; i < CPU_SETSIZE; i++) if (CPU_ISSET(i, &allowed)) { cpu_id = i; break; }
    CHECK(cpu_id >= 0); CPU_SET(cpu_id, &chosen);
    CHECK(!sched_setaffinity(0, sizeof(chosen), &chosen));
    int fd = open(argv[1], O_RDWR | O_CLOEXEC);
    struct stat st;
    CHECK(fd >= 0 && !fstat(fd, &st) && S_ISCHR(st.st_mode));
    unsigned char *canonical = mmap(NULL, RAM_BYTES, PROT_READ | PROT_WRITE,
                                    MAP_SHARED, fd, (off_t)offset);
    CHECK(canonical != MAP_FAILED);
    for (size_t i = 0; i < RAM_BYTES; i++) canonical[i] = pattern(i % PAGE);
    CHECK(!mprotect(canonical, RAM_BYTES, PROT_READ));
    unsigned char *peer = mmap(NULL, PAGE, PROT_READ, MAP_PRIVATE, fd, (off_t)offset);
    unsigned char *ram = mmap(NULL, RAM_BYTES, PROT_READ | PROT_WRITE,
                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(peer != MAP_FAILED && ram != MAP_FAILED);
    uint64_t canonical_pfn = pfn(canonical);
    CHECK(pfn(peer) == canonical_pfn);
    printf("POOL_COW_LATENCY_CONFIG {\"samples_per_mode\":%lu,\"modes\":3,\"page_bytes\":4096,"
           "\"written_bytes\":1,\"cpu\":%d,\"clock\":\"CLOCK_MONOTONIC_RAW\",\"owned_offset\":%" PRIu64 ","
           "\"owned_bytes\":%u,\"read_setup_before_each_write\":true,"
           "\"scope\":\"Setup faults and validation excluded; fresh-zero read setup may itself allocate backing\","
           "\"kvm_timing\":\"KVM_RUN entry, one byte store, HLT exit\"}\n",
           count, cpu_id, offset, RAM_BYTES);
    for (unsigned n = 0; n < count; n++) for (int mode = 0; mode < 3; mode++) {
        uint64_t detach = bind_page(ram + PAGE, fd, (off_t)offset, mode);
        uint64_t before = pfn(ram + PAGE);
        CHECK((before == canonical_pfn) == (mode == 0));
        uint64_t begin = now();
        *(volatile unsigned char *)(ram + PAGE) = 0x5a;
        uint64_t first = now() - begin;
        uint64_t after = pfn(ram + PAGE);
        CHECK(after != canonical_pfn && (mode != 1 || before == after));
        begin = now();
        *(volatile unsigned char *)(ram + PAGE) = 0x5a;
        uint64_t second = now() - begin;
        CHECK(pfn(ram + PAGE) == after);
        verify(ram + PAGE, mode == 2, 1); verify(canonical, 0, 0); verify(peer, 0, 0);
        output("host_store", mode, n, detach, first, second, before, after, canonical_pfn);
    }
    int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    CHECK(kvm >= 0 && ioctl(kvm, KVM_GET_API_VERSION, 0) == 12);
    int vm = ioctl(kvm, KVM_CREATE_VM, 0);
    CHECK(vm >= 0 && !ioctl(vm, KVM_SET_TSS_ADDR, 0xfffbd000));
    const unsigned char code[] = {0xa0, 0x00, 0x10, 0xf4, 0xc6, 0x06, 0x00, 0x10, 0x5a, 0xf4};
    memcpy(ram, code, sizeof(code));
    struct kvm_userspace_memory_region region = {
        .slot = 0, .guest_phys_addr = 0, .memory_size = RAM_BYTES, .userspace_addr = (uintptr_t)ram,
    };
    CHECK(!ioctl(vm, KVM_SET_USER_MEMORY_REGION, &region));
    int vcpu = ioctl(vm, KVM_CREATE_VCPU, 0);
    int run_bytes = ioctl(kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    CHECK(vcpu >= 0 && run_bytes > 0);
    struct kvm_run *run = mmap(NULL, (size_t)run_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, vcpu, 0);
    CHECK(run != MAP_FAILED);
    struct kvm_sregs sregs;
    CHECK(!ioctl(vcpu, KVM_GET_SREGS, &sregs));
    sregs.cs.base = 0; sregs.cs.selector = 0;
    CHECK(!ioctl(vcpu, KVM_SET_SREGS, &sregs));
    for (unsigned n = 0; n < count; n++) for (int mode = 0; mode < 3; mode++) {
        uint64_t detach = bind_page(ram + PAGE, fd, (off_t)offset, mode);
        struct kvm_regs regs = {.rip = 0, .rflags = 2};
        CHECK(!ioctl(vcpu, KVM_SET_REGS, &regs));
        CHECK(!ioctl(vcpu, KVM_RUN, 0) && run->exit_reason == KVM_EXIT_HLT);
        CHECK(!ioctl(vcpu, KVM_GET_REGS, &regs));
        CHECK((regs.rax & 0xff) == (mode == 2 ? 0 : pattern(0)));
        uint64_t before = pfn(ram + PAGE);
        CHECK((before == canonical_pfn) == (mode == 0));
        uint64_t begin = now();
        CHECK(!ioctl(vcpu, KVM_RUN, 0));
        uint64_t first = now() - begin;
        CHECK(run->exit_reason == KVM_EXIT_HLT);
        uint64_t after = pfn(ram + PAGE);
        CHECK(after != canonical_pfn && (mode != 1 || before == after));
        regs = (struct kvm_regs){.rip = 4, .rflags = 2};
        CHECK(!ioctl(vcpu, KVM_SET_REGS, &regs));
        begin = now();
        CHECK(!ioctl(vcpu, KVM_RUN, 0));
        uint64_t second = now() - begin;
        CHECK(run->exit_reason == KVM_EXIT_HLT && pfn(ram + PAGE) == after);
        verify(ram + PAGE, mode == 2, 1); verify(canonical, 0, 0); verify(peer, 0, 0);
        output("kvm_run_store", mode, n, detach, first, second, before, after, canonical_pfn);
    }
    CHECK(!munmap(run, (size_t)run_bytes) && !close(vcpu) && !close(vm) && !close(kvm));
    CHECK(!munmap(ram, RAM_BYTES) && !munmap(peer, PAGE) && !munmap(canonical, RAM_BYTES) && !close(fd));
    puts("POOL_COW_LATENCY_DONE {\"pass\":true}");
    return 0;
}
