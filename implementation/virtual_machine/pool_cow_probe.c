#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kvm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

/* Destructive lab probe: caller owns exactly 64 KiB at the supplied offset.
 * Run only inside a fresh disposable L1, never on a live pool allocation.
 * Proves host and KVM CoW and actual PFN sharing, not non-coherent CXL safety.
 */
#define PAGE 4096
#define BYTES (16 * PAGE)
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL line=%d check=%s errno=%d (%s)\n", __LINE__, #x, errno, strerror(errno)); exit(1); } } while (0)

static unsigned char pattern(size_t n)
{
    return (unsigned char)(((n * UINT64_C(101)) ^ (n >> 7) ^ (n >> 11)) + 0x31);
}

static uint64_t pfn(const void *address)
{
    /* A fork may omit special DAX PTEs; fault the read alias before inspection. */
    (void)*(volatile const unsigned char *)address;
    uint64_t entry;
    int fd = open("/proc/self/pagemap", O_RDONLY);
    CHECK(fd >= 0);
    CHECK(pread(fd, &entry, sizeof(entry), (off_t)((uintptr_t)address / PAGE * 8)) == sizeof(entry));
    CHECK(!close(fd));
    CHECK(entry & (UINT64_C(1) << 63));
    entry &= (UINT64_C(1) << 55) - 1;
    CHECK(entry); /* Require CAP_SYS_ADMIN; a masked PFN is not evidence. */
    return entry;
}

static void check_pattern(const unsigned char *p, size_t length)
{
    for (size_t i = 0; i < length; i++) CHECK(p[i] == pattern(i));
}

static void kvm_probe(int dax_fd, off_t offset, const unsigned char *canonical)
{
    int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    CHECK(kvm >= 0);
    CHECK(ioctl(kvm, KVM_GET_API_VERSION, 0) == 12);
    int vm = ioctl(kvm, KVM_CREATE_VM, 0);
    CHECK(vm >= 0);
    CHECK(!ioctl(vm, KVM_SET_TSS_ADDR, 0xfffbd000));
    unsigned char *ram = mmap(NULL, BYTES, PROT_READ | PROT_WRITE,
                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(ram != MAP_FAILED);
    CHECK(mmap(ram + PAGE, PAGE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_FIXED,
               dax_fd, offset) == ram + PAGE);
    /* 16-bit real mode: load byte at 0x1000, HLT; write 0x5a, HLT. */
    const unsigned char code[] = {0xa0, 0x00, 0x10, 0xf4, 0xc6, 0x06, 0x00, 0x10, 0x5a, 0xf4};
    memcpy(ram, code, sizeof(code));
    struct kvm_userspace_memory_region mem = {
        .slot = 0, .guest_phys_addr = 0, .memory_size = BYTES,
        .userspace_addr = (uintptr_t)ram,
    };
    CHECK(!ioctl(vm, KVM_SET_USER_MEMORY_REGION, &mem));
    int cpu = ioctl(vm, KVM_CREATE_VCPU, 0);
    CHECK(cpu >= 0);
    int run_bytes = ioctl(kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    CHECK(run_bytes > 0);
    struct kvm_run *run = mmap(NULL, (size_t)run_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, cpu, 0);
    CHECK(run != MAP_FAILED);
    struct kvm_sregs sregs;
    CHECK(!ioctl(cpu, KVM_GET_SREGS, &sregs));
    sregs.cs.base = 0;
    sregs.cs.selector = 0;
    CHECK(!ioctl(cpu, KVM_SET_SREGS, &sregs));
    struct kvm_regs regs = {.rip = 0, .rflags = 2};
    CHECK(!ioctl(cpu, KVM_SET_REGS, &regs));
    CHECK(!ioctl(cpu, KVM_RUN, 0));
    CHECK(run->exit_reason == KVM_EXIT_HLT);
    CHECK(!ioctl(cpu, KVM_GET_REGS, &regs));
    CHECK((regs.rax & 0xff) == canonical[0]);
    uint64_t before = pfn(ram + PAGE);
    CHECK(before == pfn(canonical));
    CHECK(!ioctl(cpu, KVM_RUN, 0));
    CHECK(run->exit_reason == KVM_EXIT_HLT);
    CHECK(ram[PAGE] == 0x5a);
    CHECK(canonical[0] == pattern(0));
    uint64_t after = pfn(ram + PAGE);
    CHECK(after != before);
    printf("KVM_COW shared_pfn=%" PRIu64 " private_pfn=%" PRIu64 " pass=1\n", before, after);
    /* Rebind the same HVA after EPT was populated and written. mmap's MMU
     * notifier must invalidate it; a stale EPT entry would read old bytes.
     */
    for (size_t i = 1; i < BYTES / PAGE; i++) {
        CHECK(mmap(ram + PAGE, PAGE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_FIXED,
                   dax_fd, offset + (off_t)(i * PAGE)) == ram + PAGE);
        regs = (struct kvm_regs){.rip = 0, .rflags = 2};
        CHECK(!ioctl(cpu, KVM_SET_REGS, &regs));
        CHECK(!ioctl(cpu, KVM_RUN, 0));
        CHECK(run->exit_reason == KVM_EXIT_HLT);
        CHECK(!ioctl(cpu, KVM_GET_REGS, &regs));
        CHECK((regs.rax & 0xff) == canonical[i * PAGE]);
        CHECK(pfn(ram + PAGE) == pfn(canonical + i * PAGE));
        CHECK(!ioctl(cpu, KVM_RUN, 0));
        CHECK(run->exit_reason == KVM_EXIT_HLT);
        CHECK(ram[PAGE] == 0x5a);
        CHECK(canonical[i * PAGE] == pattern(i * PAGE));
        CHECK(pfn(ram + PAGE) != pfn(canonical + i * PAGE));
    }
    puts("KVM_REMAP aliases=16 mmu_notifier=passed cow=passed");
    CHECK(!munmap(run, (size_t)run_bytes));
    CHECK(!close(cpu));
    CHECK(!close(vm));
    CHECK(!close(kvm));
    CHECK(!munmap(ram, BYTES));
}

int main(int argc, char **argv)
{
    CHECK(argc == 4 && (!strcmp(argv[3], "OWNED_POOL_TEST") || !strcmp(argv[3], "EXPECT_DISABLED")));
    char *end;
    errno = 0;
    uint64_t offset = strtoull(argv[2], &end, 10);
    CHECK(!errno && *argv[2] && !*end && !(offset % PAGE) && offset >= 2 * 1024 * 1024);
    CHECK(offset <= INT64_MAX - BYTES);
    int fd = open(argv[1], O_RDWR | O_CLOEXEC);
    CHECK(fd >= 0);
    struct stat st;
    CHECK(!fstat(fd, &st) && S_ISCHR(st.st_mode));
    if (!strcmp(argv[3], "EXPECT_DISABLED")) {
        void *mapping = mmap(NULL, PAGE, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, (off_t)offset);
        CHECK(mapping == MAP_FAILED && errno == EINVAL);
        puts("POOL_COW_DISABLED rejection=passed errno=EINVAL");
        CHECK(!close(fd));
        return 0;
    }
    unsigned char *canonical = mmap(NULL, BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)offset);
    CHECK(canonical != MAP_FAILED);
    for (size_t i = 0; i < BYTES; i++) canonical[i] = pattern(i);
    unsigned char *a = mmap(NULL, BYTES, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, (off_t)offset);
    unsigned char *b = mmap(NULL, BYTES, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, (off_t)offset);
    CHECK(a != MAP_FAILED && b != MAP_FAILED);
    check_pattern(a, BYTES);
    check_pattern(b, BYTES);
    for (size_t i = 0; i < BYTES; i += PAGE) {
        CHECK(pfn(a + i) == pfn(canonical + i));
        CHECK(pfn(b + i) == pfn(canonical + i));
    }
    uint64_t original = pfn(a);
    a[0] ^= 0xff;
    CHECK(pfn(a) != original && a[0] == (unsigned char)(pattern(0) ^ 0xff));
    check_pattern(b, BYTES);
    check_pattern(canonical, BYTES);

    unsigned char *first_write = mmap(NULL, PAGE, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, (off_t)offset);
    CHECK(first_write != MAP_FAILED);
    first_write[23] = 0xd2; /* No read/fault before this store. */
    CHECK(first_write[23] == 0xd2 && pfn(first_write) != pfn(canonical));
    for (size_t i = 0; i < PAGE; i++) if (i != 23) CHECK(first_write[i] == pattern(i));

    char name[] = "/tmp/pool-cow-io-XXXXXX";
    int input = mkstemp(name);
    CHECK(input >= 0 && !unlink(name));
    unsigned char expected[PAGE];
    memset(expected, 0xa7, sizeof(expected));
    CHECK(write(input, expected, sizeof(expected)) == sizeof(expected));
    CHECK(pread(input, a + PAGE, PAGE, 0) == PAGE);
    CHECK(!memcmp(a + PAGE, expected, PAGE));
    CHECK(pfn(a + PAGE) != pfn(canonical + PAGE));
    CHECK(!close(input));

    pid_t child = fork();
    CHECK(child >= 0);
    if (!child) {
        b[2 * PAGE] = 0x8b;
        CHECK(pfn(b + 2 * PAGE) != pfn(canonical + 2 * PAGE));
        CHECK(canonical[2 * PAGE] == pattern(2 * PAGE));
        _exit(0);
    }
    int status;
    CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    check_pattern(b, BYTES);
    check_pattern(canonical, BYTES);
    kvm_probe(fd, (off_t)offset, canonical);
    check_pattern(b, BYTES);
    check_pattern(canonical, BYTES);
    printf("POOL_COW_PROBE {\"pass\":true,\"shared_bytes\":%d,\"cpu_cow\":true,\"first_write_cow\":true,\"pread_cow\":true,\"fork_isolation\":true,\"kvm_cow\":true}\n", BYTES);
    CHECK(!munmap(a, BYTES) && !munmap(b, BYTES) && !munmap(first_write, PAGE) && !munmap(canonical, BYTES));
    CHECK(!close(fd));
    return 0;
}
