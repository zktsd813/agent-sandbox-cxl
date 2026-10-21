/* Included by shared_probe.c: no additional executable. This measures cache
 * maintenance, not VM handoff. The only writer is a retained flat 32-bit KVM
 * guest; TCP carries an immutable epoch bitmap and separates all writers. */
#include <cpuid.h>

#define CACHE_SCHEMA UINT64_C(0x43584c4341434801)
#define CACHE_DATA_GPA UINT32_C(0x10000000)
#define CACHE_DAX_OFFSET UINT64_C(0x200000)
#define CACHE_PRIVATE_BYTES UINT32_C(0x400000)
enum { CACHE_INIT = 1, CACHE_PREFAULT, CACHE_INITIALIZED, CACHE_READY,
       CACHE_MANIFEST, CACHE_ACK, CACHE_VERIFIED };
struct cache_packet {
    uint64_t schema, nonce, kind, round, memory_bytes, dirty_pages, bitmap_bytes;
    uint64_t bitmap_hash, mode, write_bytes, dirty_permyriad, rounds, logging;
};
struct cache_vm {
    int kvm, vm, vcpu, stats_fd, stats_errno;
    off_t pf_fixed_offset;
    void *private;
    struct kvm_run *run;
    size_t run_bytes;
    unsigned logging;
};

static void cache_open_stats(struct cache_vm *v)
{
    v->stats_fd = ioctl(v->vcpu, KVM_GET_STATS_FD, 0);
    if (v->stats_fd < 0) { v->stats_errno = errno; return; }
    struct kvm_stats_header header;
    if (pread(v->stats_fd, &header, sizeof(header), 0) != sizeof(header) ||
        !header.name_size || header.name_size > 4096 || header.num_desc > 4096)
        goto unavailable;
    size_t size = sizeof(struct kvm_stats_desc) + header.name_size;
    struct kvm_stats_desc *desc = malloc(size);
    if (!desc) die("cache KVM stats descriptor");
    for (unsigned i = 0; i < header.num_desc; i++) {
        if (pread(v->stats_fd, desc, size, header.desc_offset + (off_t)i * size) != (ssize_t)size)
            break;
        if (memchr(desc->name, '\0', header.name_size) && !strcmp(desc->name, "pf_fixed") &&
            desc->size == 1 && !desc->exponent && (desc->flags & KVM_STATS_TYPE_MASK) == KVM_STATS_TYPE_CUMULATIVE) {
            v->pf_fixed_offset = (off_t)header.data_offset + desc->offset;
            free(desc);
            return;
        }
    }
    free(desc);
unavailable:
    close(v->stats_fd);
    v->stats_fd = -1;
    v->stats_errno = ENOTSUP;
}

static uint64_t cache_pf_fixed(struct cache_vm *v)
{
    uint64_t count;
    if (v->stats_fd < 0) return UINT64_MAX;
    if (pread(v->stats_fd, &count, sizeof(count), v->pf_fixed_offset) != sizeof(count))
        die("cache read KVM pf_fixed");
    return count;
}

static void cache_fail(const char *what)
{
    fprintf(stderr, "Cache benchmark: %s\n", what);
    exit(3);
}

static int cache_module_flag(const char *name)
{
    char path[160], value;
    snprintf(path, sizeof(path), "/sys/module/kvm_intel/parameters/%s", name);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    int ok = fscanf(f, " %c", &value);
    fclose(f);
    return ok == 1 ? (value == 'Y' || value == '1') : -1;
}

static void cache_print_flag(const char *name, int flag)
{
    printf(",\"%s\":", name);
    if (flag < 0) printf("null");
    else printf("%s", flag ? "true" : "false");
}

static void cache_feature_check(void)
{
    unsigned a, b, c, d;
    if (!__get_cpuid(1, &a, &b, &c, &d) || !(d & (1u << 19)) ||
        (((b >> 8) & 255) * 8 != 64) ||
        !__get_cpuid_count(7, 0, &a, &b, &c, &d) ||
        !(b & (1u << 23)) || !(b & (1u << 24)))
        cache_fail("requires CLFLUSHOPT, CLWB and a 64-byte cache line; no fallback");
}

static uint64_t cache_bitmap_hash(const uint64_t *bitmap, size_t bytes)
{
    const unsigned char *p = (const void *)bitmap;
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < bytes; i++) hash = (hash ^ p[i]) * UINT64_C(1099511628211);
    return hash;
}

static void cache_send(int fd, struct cache_packet packet, unsigned kind)
{
    packet.kind = kind;
    migration_io(fd, &packet, sizeof(packet), 1);
}

static struct cache_packet cache_receive(int fd, struct cache_packet expected, unsigned kind)
{
    struct cache_packet got;
    migration_io(fd, &got, sizeof(got), 0);
    if (got.schema != expected.schema || got.nonce != expected.nonce ||
        got.kind != kind || got.round != expected.round ||
        got.memory_bytes != expected.memory_bytes || got.dirty_pages != expected.dirty_pages ||
        got.bitmap_bytes != expected.bitmap_bytes || got.bitmap_hash != expected.bitmap_hash ||
        got.mode != expected.mode || got.write_bytes != expected.write_bytes ||
        got.dirty_permyriad != expected.dirty_permyriad || got.rounds != expected.rounds ||
        got.logging != expected.logging)
        cache_fail("TCP epoch/configuration/bitmap hash mismatch");
    return got;
}

static struct cache_vm cache_create_vm(void *data, size_t bytes, unsigned logging)
{
    struct cache_vm v = {.logging = logging};
    v.kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    if (v.kvm < 0) die("cache /dev/kvm");
    if (ioctl(v.kvm, KVM_GET_API_VERSION, 0) != 12) cache_fail("KVM API version");
    v.vm = ioctl(v.kvm, KVM_CREATE_VM, 0);
    if (v.vm < 0) die("cache KVM_CREATE_VM");
    if (logging) {
        int flags = ioctl(v.kvm, KVM_CHECK_EXTENSION, KVM_CAP_MANUAL_DIRTY_LOG_PROTECT2);
        if (!(flags & KVM_DIRTY_LOG_MANUAL_PROTECT_ENABLE))
            cache_fail("KVM manual dirty-log protection unavailable");
        struct kvm_enable_cap cap = {.cap = KVM_CAP_MANUAL_DIRTY_LOG_PROTECT2,
            .args = {KVM_DIRTY_LOG_MANUAL_PROTECT_ENABLE}};
        if (ioctl(v.vm, KVM_ENABLE_CAP, &cap)) die("cache manual dirty log enable");
    }
    v.private = mmap(NULL, CACHE_PRIVATE_BYTES, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (v.private == MAP_FAILED) die("cache private guest code");
    struct kvm_userspace_memory_region slot = {.slot = 0,
        .guest_phys_addr = 0, .memory_size = CACHE_PRIVATE_BYTES,
        .userspace_addr = (uintptr_t)v.private};
    if (ioctl(v.vm, KVM_SET_USER_MEMORY_REGION, &slot)) die("cache code memslot");
    slot = (struct kvm_userspace_memory_region){.slot = 1,
        .flags = logging ? KVM_MEM_LOG_DIRTY_PAGES : 0,
        .guest_phys_addr = CACHE_DATA_GPA, .memory_size = bytes,
        .userspace_addr = (uintptr_t)data};
    if (ioctl(v.vm, KVM_SET_USER_MEMORY_REGION, &slot)) die("cache data memslot");
    v.vcpu = ioctl(v.vm, KVM_CREATE_VCPU, 0);
    if (v.vcpu < 0) die("cache KVM_CREATE_VCPU");
    int run_bytes = ioctl(v.kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    if (run_bytes < (int)sizeof(struct kvm_run)) cache_fail("KVM run mmap size");
    v.run_bytes = (size_t)run_bytes;
    v.run = mmap(NULL, v.run_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, v.vcpu, 0);
    if (v.run == MAP_FAILED) die("cache kvm_run mmap");
    struct kvm_sregs sr;
    if (ioctl(v.vcpu, KVM_GET_SREGS, &sr)) die("cache get sregs");
    struct kvm_segment flat = {.base = 0, .limit = UINT32_MAX, .selector = 8,
        .type = 11, .present = 1, .db = 1, .s = 1, .g = 1};
    sr.cs = flat;
    flat.selector = 16;
    flat.type = 3;
    sr.ds = sr.es = sr.fs = sr.gs = sr.ss = flat;
    sr.cr0 = 0x11;
    sr.cr3 = sr.cr4 = sr.efer = 0;
    if (ioctl(v.vcpu, KVM_SET_SREGS, &sr)) die("cache protected-mode sregs");
    cache_open_stats(&v);
    return v;
}

static uint64_t cache_guest_run(struct cache_vm *v, uint32_t count,
                                 uint32_t token, unsigned write_bytes, int writing)
{
    /* lodsd gives a page index. The write loop fills exactly write_bytes at
     * DATA_GPA + index*4096 with index XOR token, then exits with HLT. */
    const unsigned char write_code[] = {
        0x85,0xed,0x74,0x14,0xad,0x89,0xc7,0xc1,0xe7,0x0c,0x01,0xd7,
        0x31,0xd8,0xb9,0,0,0,0,0xf3,0xab,0x4d,0x75,0xec,0xf4};
    const unsigned char read_code[] = {
        0x85,0xed,0x74,0x0b,0xad,0xc1,0xe0,0x0c,0x01,0xd0,0x8b,0x38,
        0x4d,0x75,0xf5,0xf4};
    unsigned char *code = (unsigned char *)v->private + 0x1000;
    memcpy(code, writing ? write_code : read_code, writing ? sizeof(write_code) : sizeof(read_code));
    if (writing) {
        uint32_t words = write_bytes / 4;
        memcpy(code + 15, &words, sizeof(words));
    }
    struct kvm_regs regs = {.rip = 0x1000, .rsp = CACHE_PRIVATE_BYTES - 16,
        .rflags = 2, .rsi = 0x2000, .rbp = count, .rbx = token, .rdx = CACHE_DATA_GPA};
    if (ioctl(v->vcpu, KVM_SET_REGS, &regs)) die("cache set guest regs");
    uint64_t begin = monotonic_ns();
    for (;;) {
        int rc = ioctl(v->vcpu, KVM_RUN, 0);
        if (rc < 0 && errno == EINTR) continue;
        if (rc < 0) die("cache KVM_RUN");
        if (v->run->exit_reason == KVM_EXIT_HLT) break;
        if (v->run->exit_reason != KVM_EXIT_INTR) {
            fprintf(stderr, "Cache guest unexpected KVM exit %u fail_reason=%" PRIu64 "\n",
                v->run->exit_reason, (uint64_t)v->run->fail_entry.hardware_entry_failure_reason);
            exit(3);
        }
    }
    return monotonic_ns() - begin;
}

static uint64_t cache_harvest(struct cache_vm *v, uint64_t *bitmap, size_t bytes)
{
    memset(bitmap, 0, bytes);
    if (!v->logging) return 0;
    struct kvm_dirty_log log = {.slot = 1, .dirty_bitmap = bitmap};
    uint64_t begin = monotonic_ns();
    if (ioctl(v->vm, KVM_GET_DIRTY_LOG, &log)) die("cache GET_DIRTY_LOG");
    return monotonic_ns() - begin;
}

static uint64_t cache_rearm(struct cache_vm *v, uint64_t *bitmap, uint32_t pages)
{
    if (!v->logging) return 0;
    struct kvm_clear_dirty_log clear = {.slot = 1, .num_pages = pages,
        .first_page = 0, .dirty_bitmap = bitmap};
    uint64_t begin = monotonic_ns();
    if (ioctl(v->vm, KVM_CLEAR_DIRTY_LOG, &clear)) die("cache CLEAR_DIRTY_LOG");
    return monotonic_ns() - begin;
}

static uint64_t cache_scan(const uint64_t *bitmap, const uint64_t *expected,
                            uint32_t pages, uint32_t wanted, uint32_t *indices)
{
    uint64_t begin = monotonic_ns();
    uint32_t found = 0;
    for (uint32_t w = 0; w < pages / 64; w++) {
        uint64_t bits = bitmap[w];
        if (bits != expected[w]) cache_fail("KVM bitmap missing or extra dirty pages");
        while (bits) {
            if (found >= wanted) cache_fail("dirty list overflow");
            indices[found++] = w * 64 + (uint32_t)__builtin_ctzll(bits);
            bits &= bits - 1;
        }
    }
    if (found != wanted) cache_fail("dirty page count mismatch");
    return monotonic_ns() - begin;
}

static uint64_t cache_maintain(unsigned char *data, const uint32_t *indices,
                               uint32_t count, int all, int writer)
{
    uint64_t begin = monotonic_ns();
    for (uint32_t i = 0; i < count; i++) {
        unsigned char *page = data + (size_t)(all ? i : indices[i]) * 4096;
        for (unsigned line = 0; line < 4096; line += 64) {
            if (writer) __asm__ __volatile__("clwb (%0)" :: "r"(page + line) : "memory");
            else __asm__ __volatile__("clflushopt (%0)" :: "r"(page + line) : "memory");
        }
    }
    if (writer) __asm__ __volatile__("sfence" ::: "memory");
    else __asm__ __volatile__("mfence" ::: "memory");
    return monotonic_ns() - begin;
}

static uint64_t cache_verify(const unsigned char *data, const uint32_t *values,
                              uint32_t pages, unsigned write_bytes, uint32_t seed)
{
    uint64_t begin = monotonic_ns();
    for (uint32_t page = 0; page < pages; page++) {
        const volatile uint32_t *p = (const void *)(data + (size_t)page * 4096);
        for (unsigned word = 0; word < write_bytes / 4; word++)
            if (p[word] != values[page]) cache_fail("retained page data mismatch");
        if (write_bytes < 4096 && p[1023] != (page ^ seed))
            cache_fail("guest wrote beyond the configured page prefix");
    }
    return monotonic_ns() - begin;
}

static void cache_bench(char role, const char *path, const char *peer, unsigned port,
                         unsigned memory_mib, unsigned dirty_permyriad, unsigned write_bytes,
                         unsigned rounds, uint64_t nonce, unsigned mode)
{
    if (getenv("BENCH_RUN_DIR")) bench_transaction_paths();
    setvbuf(stdout, NULL, _IONBF, 0);
    const char *cpu_option = getenv("CACHE_BENCH_CPU");
    if (cpu_option) {
        char *end;
        unsigned long cpu = strtoul(cpu_option, &end, 10);
        if (!*cpu_option || *end || cpu >= CPU_SETSIZE) cache_fail("invalid CACHE_BENCH_CPU");
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(cpu, &set);
        if (sched_setaffinity(0, sizeof(set), &set)) die("cache CPU affinity");
    }
    cache_feature_check();
    const char *log_option = getenv("CACHE_BENCH_DIRTY_LOG");
    if (log_option && strcmp(log_option, "0") && strcmp(log_option, "1"))
        cache_fail("CACHE_BENCH_DIRTY_LOG must be 0 or 1");
    unsigned logging = !log_option || strcmp(log_option, "0");
    if (!logging && mode == 1) cache_fail("dirty mode requires actual KVM dirty logging");
    const char *mode_name = mode == 0 ? "full" : mode == 1 ? "dirty" : "none";
    size_t bytes = (size_t)memory_mib * 1024 * 1024;
    uint32_t pages = (uint32_t)(bytes / 4096);
    uint32_t dirty = (uint32_t)(((uint64_t)pages * dirty_permyriad + 9999) / 10000);
    size_t bitmap_bytes = pages / 8;
    int dax = open(path, O_RDWR | O_CLOEXEC);
    if (dax < 0) die("cache shared backing");
    struct stat st;
    if (fstat(dax, &st)) die("cache backing stat");
    int file = S_ISREG(st.st_mode) && env_enabled("CACHE_BENCH_ALLOW_FILE");
    uint64_t align = file ? 4096 : dax_attribute(path, "align");
    uint64_t size = file ? (uint64_t)st.st_size : dax_attribute(path, "size");
    if ((!file && !S_ISCHR(st.st_mode)) || CACHE_DAX_OFFSET % align || bytes % align ||
        size < CACHE_DAX_OFFSET || bytes > size - CACHE_DAX_OFFSET)
        cache_fail("backing type/alignment/extent mismatch");
    unsigned char *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, dax, CACHE_DAX_OFFSET);
    if (data == MAP_FAILED) die("cache shared memory mmap");
    if (madvise(data, bytes, MADV_NOHUGEPAGE) && file) die("cache MADV_NOHUGEPAGE");
    uint64_t *bitmap = calloc(1, bitmap_bytes), *expected = calloc(1, bitmap_bytes);
    uint32_t *indices = malloc((size_t)pages * sizeof(*indices));
    uint32_t *values = malloc((size_t)pages * sizeof(*values));
    if (!bitmap || !expected || !indices || !values) die("cache metadata buffers");
    uint32_t seed = (uint32_t)nonce ^ (uint32_t)(nonce >> 32) ^ UINT32_C(0xa17c9e35);
    for (uint32_t i = 0; i < pages; i++) values[i] = i ^ seed;
    struct cache_vm vm = cache_create_vm(data, bytes, logging);
    uint32_t *command = (void *)((unsigned char *)vm.private + 0x2000);
    for (uint32_t i = 0; i < pages; i++) command[i] = i;
    int fd = migration_socket(role, peer, port);
    struct cache_packet packet = {.schema = CACHE_SCHEMA, .nonce = nonce, .round = UINT64_MAX,
        .memory_bytes = bytes, .dirty_pages = dirty, .bitmap_bytes = bitmap_bytes, .mode = mode,
        .write_bytes = write_bytes, .dirty_permyriad = dirty_permyriad, .rounds = rounds, .logging = logging};
    printf("CACHE_BENCH_JSON {\"event\":\"start\",\"schema\":1,\"role\":\"%c\",\"nonce\":\"%016" PRIx64
        "\",\"mode\":\"%s\",\"memory_mib\":%u,\"memory_bytes\":%zu,\"pages\":%u,"
        "\"dirty_permyriad\":%u,\"expected_dirty_pages\":%u,\"write_bytes\":%u,\"rounds\":%u,"
        "\"bitmap_bytes\":%zu,\"logging_enabled\":%s,\"bitmap_source\":\"%s\","
        "\"backing\":\"%s\",\"data_gpa\":%u,\"dax_offset\":%" PRIu64 ",\"dax_align\":%" PRIu64
        ",\"clwb\":true,\"clflushopt\":true,\"cache_line_bytes\":64,\"pid\":%d,"
        "\"process_start\":%" PRIu64 ",\"current_cpu\":%d,\"vm_creates\":1,\"vcpu_creates\":1,"
        "\"verification\":\"every_page_write_prefix_and_unwritten_tail_word\","
        "\"prefault\":\"both_retained_KVMs_guest_write_all_4096_bytes_per_page_with_original_seed_before_timing\","
        "\"stats_pf_fixed_supported\":%s,\"stats_pf_fixed_errno\":%d",
        role, nonce, mode_name, memory_mib, bytes, pages, dirty_permyriad, dirty, write_bytes,
        rounds, bitmap_bytes, logging ? "true" : "false",
        logging ? "KVM_GET_DIRTY_LOG" : "known_workload_oracle",
        file ? "shared-file-native-control" : "device-dax", CACHE_DATA_GPA, CACHE_DAX_OFFSET,
        align, getpid(), bench_process_start(getpid()), sched_getcpu(),
        vm.stats_fd >= 0 ? "true" : "false", vm.stats_errno);
    cache_print_flag("module_pml", cache_module_flag("pml"));
    cache_print_flag("module_ept_ad", cache_module_flag("eptad"));
    cache_print_flag("module_nested", cache_module_flag("nested"));
    int pml = cache_module_flag("pml");
    printf(",\"dirty_logging_backend\":\"%s\"", !logging ? "disabled_known_workload_oracle" :
        pml > 0 ? "kvm_pml_enabled" : pml == 0 ? "kvm_non_pml_dirty_logging" : "unknown");
    printf("}\n");
    /* Setup explicitly establishes clean local caches; none disables only the
     * measured epoch operations, not this shared initial baseline. */
    if (role == 'A') {
        for (uint32_t p = 0; p < pages; p++) {
            uint32_t *words = (void *)(data + (size_t)p * 4096);
            for (unsigned w = 0; w < 1024; w++) words[w] = values[p];
        }
        cache_maintain(data, NULL, pages, 1, 1);
        cache_send(fd, packet, CACHE_INIT);
        cache_receive(fd, packet, CACHE_PREFAULT);
        cache_maintain(data, NULL, pages, 1, 0);
        cache_guest_run(&vm, pages, seed, 4096, 1);
        cache_maintain(data, NULL, pages, 1, 1);
        cache_harvest(&vm, bitmap, bitmap_bytes);
        cache_rearm(&vm, bitmap, pages);
        cache_send(fd, packet, CACHE_INITIALIZED);
    } else {
        cache_receive(fd, packet, CACHE_INIT);
        cache_maintain(data, NULL, pages, 1, 0);
        cache_verify(data, values, pages, write_bytes, seed);
        cache_guest_run(&vm, pages, seed, 4096, 1);
        cache_maintain(data, NULL, pages, 1, 1);
        cache_harvest(&vm, bitmap, bitmap_bytes);
        cache_rearm(&vm, bitmap, pages);
        cache_send(fd, packet, CACHE_PREFAULT);
        cache_receive(fd, packet, CACHE_INITIALIZED);
        cache_maintain(data, NULL, pages, 1, 0);
    }
    for (unsigned round = 0; round < rounds; round++) {
        unsigned source = round % 2;
        uint32_t token = seed ^ ((round + 1) * UINT32_C(0x9e3779b9));
        memset(expected, 0, bitmap_bytes);
        for (uint32_t i = 0; i < dirty; i++) {
            uint32_t p = (uint32_t)(((uint64_t)i * 131071 + (uint64_t)round * 8191) % pages);
            command[i] = p;
            expected[p / 64] |= UINT64_C(1) << (p % 64);
            values[p] = p ^ token;
        }
        packet.round = round;
        packet.bitmap_hash = cache_bitmap_hash(expected, bitmap_bytes);
        uint32_t maintained = mode == 0 ? pages : mode == 1 ? dirty : 0;
        if ((role == 'A' ? 0u : 1u) == source) {
            cache_receive(fd, packet, CACHE_READY);
            uint64_t pf_before = cache_pf_fixed(&vm);
            uint64_t run_ns = cache_guest_run(&vm, dirty, token, write_bytes, 1);
            uint64_t stopped = monotonic_ns();
            uint64_t harvest_ns = cache_harvest(&vm, bitmap, bitmap_bytes);
            if (!logging) memcpy(bitmap, expected, bitmap_bytes);
            uint64_t scan_ns = cache_scan(bitmap, expected, pages, dirty, indices);
            uint64_t rearm_ns = cache_rearm(&vm, bitmap, pages);
            uint64_t flush_ns = mode == 2 ? 0 : cache_maintain(data, indices, maintained, mode == 0, 1);
            uint64_t publish = monotonic_ns();
            cache_send(fd, packet, CACHE_MANIFEST);
            migration_io(fd, bitmap, bitmap_bytes, 1);
            cache_receive(fd, packet, CACHE_ACK);
            uint64_t ack = monotonic_ns();
            uint64_t pf_after = cache_pf_fixed(&vm);
            cache_receive(fd, packet, CACHE_VERIFIED);
            uint64_t ready = monotonic_ns();
            printf("CACHE_BENCH_JSON {\"event\":\"source\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
                "\",\"round\":%u,\"direction\":\"%c->%c\",\"mode\":\"%s\",\"memory_mib\":%u,"
                "\"logging_enabled\":%s,\"dirty_pages\":%u,\"maintained_pages\":%u,\"bitmap_bytes\":%zu,"
                "\"guest_run_ns\":%" PRIu64 ",\"harvest_ns\":%" PRIu64 ",\"rearm_ns\":%" PRIu64
                ",\"bitmap_scan_ns\":%" PRIu64 ",\"writer_flush_ns\":%" PRIu64
                ",\"manifest_to_ack_ns\":%" PRIu64 ",\"pause_to_ack_ns\":%" PRIu64
                ",\"pause_to_verified_ns\":%" PRIu64 ",\"bitmap_hash\":\"%016" PRIx64 "\",\"errors\":0",
                role, nonce, round, source ? 'B' : 'A', source ? 'A' : 'B', mode_name, memory_mib,
                logging ? "true" : "false", dirty, maintained, bitmap_bytes, run_ns, harvest_ns,
                rearm_ns, scan_ns, flush_ns, ack - publish, ack - stopped, ready - stopped, packet.bitmap_hash);
            printf(",\"guest_pf_fixed\":");
            if (pf_before == UINT64_MAX || pf_after == UINT64_MAX) printf("null");
            else if (pf_after < pf_before) cache_fail("pf_fixed counter moved backwards");
            else printf("%" PRIu64, pf_after - pf_before);
            cache_print_flag("module_pml", cache_module_flag("pml"));
            printf("}\n");
        } else {
            cache_send(fd, packet, CACHE_READY);
            cache_receive(fd, packet, CACHE_MANIFEST);
            migration_io(fd, bitmap, bitmap_bytes, 0);
            uint64_t hash_begin = monotonic_ns();
            uint64_t hash = cache_bitmap_hash(bitmap, bitmap_bytes);
            uint64_t hash_ns = monotonic_ns() - hash_begin;
            if (hash != packet.bitmap_hash)
                cache_fail("received manifest integrity failure");
            uint64_t scan_ns = cache_scan(bitmap, expected, pages, dirty, indices);
            uint64_t invalidate_ns = mode == 2 ? 0 : cache_maintain(data, indices, maintained, mode == 0, 0);
            cache_send(fd, packet, CACHE_ACK);
            uint64_t verify_ns = cache_verify(data, values, pages, write_bytes, seed);
            cache_send(fd, packet, CACHE_VERIFIED);
            printf("CACHE_BENCH_JSON {\"event\":\"destination\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
                "\",\"round\":%u,\"direction\":\"%c->%c\",\"mode\":\"%s\",\"memory_mib\":%u,"
                "\"logging_enabled\":%s,\"dirty_pages\":%u,\"maintained_pages\":%u,\"bitmap_bytes\":%zu,"
                "\"bitmap_scan_ns\":%" PRIu64 ",\"manifest_hash_ns\":%" PRIu64 ",\"reader_invalidate_ns\":%" PRIu64
                ",\"verify_ns\":%" PRIu64 ",\"verified_pages\":%u,\"verified_prefix_bytes_per_page\":%u,"
                "\"bitmap_hash\":\"%016" PRIx64 "\",\"errors\":0",
                role, nonce, round, source ? 'B' : 'A', source ? 'A' : 'B', mode_name, memory_mib,
                logging ? "true" : "false", dirty, maintained, bitmap_bytes, scan_ns, hash_ns, invalidate_ns,
                verify_ns, pages, write_bytes, packet.bitmap_hash);
            cache_print_flag("module_pml", cache_module_flag("pml"));
            printf("}\n");
        }
    }
    printf("CACHE_BENCH_JSON {\"event\":\"complete\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
        "\",\"rounds\":%u,\"result\":\"passed\",\"errors\":0,\"vm_creates\":1,\"vcpu_creates\":1}\n", role, nonce, rounds);
    close(fd);
    if (vm.stats_fd >= 0) close(vm.stats_fd);
    munmap(vm.run, vm.run_bytes);
    close(vm.vcpu);
    close(vm.vm);
    close(vm.kvm);
    munmap(vm.private, CACHE_PRIVATE_BYTES);
    munmap(data, bytes);
    close(dax);
    free(bitmap); free(expected); free(indices); free(values);
}
