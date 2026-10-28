#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

/* Laboratory helper, not a migration protocol. The controller must own a fresh
 * disposable L1 and externally quiesce/fence EVERY guest, VMM and kernel writer
 * before calling this program. Input files must also be stable. It does not
 * acquire ownership, change KVM mappings, handle noncoherent CXL, or establish
 * crash durability of CXL RAM. FNV-1a64 is an integrity checksum, not a
 * cryptographic digest. Every successful copy is additionally byte-compared.
 *
 * The first 2 MiB belong to the existing control/mailbox ABI and are never
 * mapped or overwritten here. A packed RAM file contains guest RAM regions in
 * snapshot order; preserving the guest GPA layout is the controller's job.
 */
#define DAX_PATH "/dev/dax0.0"
#define DAX_SYSFS "/sys/bus/dax/devices/dax0.0/"
#define RAM_OFFSET UINT64_C(2097152)
#define PAGE_BYTES UINT64_C(4096)
#define MAX_BYTES UINT64_C(2147483648)
#define CHUNK_BYTES (1024 * 1024)
#define FNV_OFFSET UINT64_C(14695981039346656037)
#define FNV_PRIME UINT64_C(1099511628211)
#define POISON_BYTE 0xa5

_Static_assert(sizeof(size_t) >= 8 && sizeof(off_t) >= 8, "64-bit userspace required");

static char last_error[256];

static int failure(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vsnprintf(last_error, sizeof(last_error), format, args);
    va_end(args);
    return -1;
}

static int syscall_failure(const char *operation)
{
    return failure("%s: %s", operation, strerror(errno));
}

static uint64_t now_ns(void)
{
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time)) {
        perror("clock_gettime");
        exit(1);
    }
    return (uint64_t)time.tv_sec * UINT64_C(1000000000) + (uint64_t)time.tv_nsec;
}

static uint64_t fnv_update(uint64_t hash, const unsigned char *data, size_t bytes)
{
    for (size_t i = 0; i < bytes; i++) hash = (hash ^ data[i]) * FNV_PRIME;
    return hash;
}

static int decimal(const char *text, uint64_t *value)
{
    if (!text || !*text) return failure("empty decimal integer");
    uint64_t result = 0;
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        if (*p < '0' || *p > '9') return failure("integer must contain decimal digits only");
        unsigned digit = *p - '0';
        if (result > (UINT64_MAX - digit) / 10) return failure("decimal integer overflow");
        result = result * 10 + digit;
    }
    *value = result;
    return 0;
}

static int parse_bytes(const char *text, uint64_t *bytes)
{
    if (decimal(text, bytes)) return -1;
    if (!*bytes || *bytes > MAX_BYTES || *bytes % PAGE_BYTES)
        return failure("BYTES must be nonzero, 4096-byte aligned and <= 2147483648");
    return 0;
}

static int read_attribute(const char *attribute, char *value, size_t capacity)
{
    char path[160];
    int length = snprintf(path, sizeof(path), DAX_SYSFS "%s", attribute);
    if (length < 0 || (size_t)length >= sizeof(path)) return failure("sysfs path overflow");
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return syscall_failure("open DAX sysfs");
    ssize_t got;
    do { got = read(fd, value, capacity - 1); } while (got < 0 && errno == EINTR);
    int saved_errno = errno;
    close(fd);
    errno = saved_errno;
    if (got < 0) return syscall_failure("read DAX sysfs");
    if (!got || (size_t)got == capacity - 1) return failure("empty or oversized DAX attribute");
    value[got] = '\0';
    if (value[got - 1] == '\n') value[got - 1] = '\0';
    return 0;
}

static int layout(uint64_t bytes, uint64_t align, uint64_t capacity, uint64_t *mapping_bytes)
{
    if (!bytes || bytes > MAX_BYTES || bytes % PAGE_BYTES)
        return failure("invalid RAM byte range");
    if (align < PAGE_BYTES || align > RAM_OFFSET || (align & (align - 1)) || RAM_OFFSET % align)
        return failure("unsupported DAX alignment");
    uint64_t rounded = (bytes + align - 1) / align * align;
    if (capacity % align || capacity < RAM_OFFSET || rounded > capacity - RAM_OFFSET)
        return failure("RAM range exceeds DAX capacity after reserved prefix");
    *mapping_bytes = rounded;
    return 0;
}

struct dax_mapping {
    int fd;
    unsigned char *data;
    uint64_t bytes, align, capacity;
};

static int device_identity(mode_t mode, dev_t device, const char *identity)
{
    if (!S_ISCHR(mode)) return failure("DAX path must be a character device");
    char value[64];
    if (strlen(identity) >= sizeof(value)) return failure("DAX device identity is too long");
    strcpy(value, identity);
    char *colon = strchr(value, ':');
    uint64_t major_value, minor_value;
    if (!colon || strchr(colon + 1, ':')) return failure("invalid DAX sysfs device identity");
    *colon = '\0';
    if (decimal(value, &major_value) || decimal(colon + 1, &minor_value)) return -1;
    if (major_value != major(device) || minor_value != minor(device))
        return failure("opened character device does not match DAX sysfs identity");
    return 0;
}

static int map_dax(const char *path, uint64_t bytes, int writable, struct dax_mapping *mapping)
{
    if (strcmp(path, DAX_PATH)) return failure("only the owned lab /dev/dax0.0 is supported");
    mapping->fd = open(path, (writable ? O_RDWR : O_RDONLY) | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (mapping->fd < 0) return syscall_failure("open device-DAX");
    struct stat st;
    if (fstat(mapping->fd, &st)) return syscall_failure("fstat device-DAX");
    if (!S_ISCHR(st.st_mode)) return failure("DAX path must be a character device");
    char value[64];
    if (read_attribute("align", value, sizeof(value)) || decimal(value, &mapping->align) ||
        read_attribute("size", value, sizeof(value)) || decimal(value, &mapping->capacity) ||
        layout(bytes, mapping->align, mapping->capacity, &mapping->bytes)) return -1;
    if (read_attribute("dev", value, sizeof(value))) return -1;
    if (device_identity(st.st_mode, st.st_rdev, value)) return -1;
    mapping->data = mmap(NULL, (size_t)mapping->bytes, PROT_READ | (writable ? PROT_WRITE : 0),
                         MAP_SHARED, mapping->fd, (off_t)RAM_OFFSET);
    if (mapping->data == MAP_FAILED) return syscall_failure("mmap device-DAX RAM");
    if ((uintptr_t)mapping->data % mapping->align) return failure("DAX mapping address is unaligned");
    return 0;
}

static int validate_file(int fd, uint64_t bytes)
{
    struct stat st;
    if (fstat(fd, &st)) return syscall_failure("fstat RAM file");
    if (!S_ISREG(st.st_mode) || st.st_size < 0 || (uint64_t)st.st_size != bytes)
        return failure("RAM input must be a regular file of exactly BYTES length");
    return 0;
}

static int open_input(const char *path, uint64_t bytes)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) { syscall_failure("open RAM input"); return -1; }
    if (validate_file(fd, bytes)) { close(fd); return -1; }
    return fd;
}

static int create_output(const char *path)
{
    int fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
    if (fd < 0) { syscall_failure("create new RAM output"); return -1; }
    if (validate_file(fd, 0)) { close(fd); return -1; }
    return fd;
}

static int io_exact(int fd, unsigned char *buffer, size_t bytes, uint64_t offset, int writing)
{
    size_t done = 0;
    while (done < bytes) {
        ssize_t amount = writing ? pwrite(fd, buffer + done, bytes - done, (off_t)(offset + done)) :
                                   pread(fd, buffer + done, bytes - done, (off_t)(offset + done));
        if (amount < 0 && errno == EINTR) continue;
        if (amount < 0) return syscall_failure(writing ? "write RAM output" : "read RAM input");
        if (!amount) return failure("short RAM file I/O at offset %" PRIu64, offset + done);
        done += (size_t)amount;
    }
    return 0;
}

/* These functions operate on a bounded memory span and regular-file FD. Unit
 * tests call them with allocated memory; that does NOT emulate a DAX device. */
static int copy_span(int fd, unsigned char *memory, uint64_t bytes, int exporting,
                     unsigned char *buffer, uint64_t *hash)
{
    *hash = FNV_OFFSET;
    for (uint64_t offset = 0; offset < bytes;) {
        size_t count = (size_t)(bytes - offset < CHUNK_BYTES ? bytes - offset : CHUNK_BYTES);
        if (exporting) memcpy(buffer, memory + offset, count);
        else if (io_exact(fd, buffer, count, offset, 0)) return -1;
        *hash = fnv_update(*hash, buffer, count);
        if (exporting) {
            if (io_exact(fd, buffer, count, offset, 1)) return -1;
        } else memcpy(memory + offset, buffer, count);
        offset += count;
    }
    atomic_thread_fence(memory_order_seq_cst);
    if (exporting && fsync(fd)) return syscall_failure("fsync RAM output");
    return 0;
}

static int compare_span(int fd, const unsigned char *memory, uint64_t bytes,
                        unsigned char *buffer, uint64_t *hash)
{
    if (validate_file(fd, bytes)) return -1;
    *hash = FNV_OFFSET;
    for (uint64_t offset = 0; offset < bytes;) {
        size_t count = (size_t)(bytes - offset < CHUNK_BYTES ? bytes - offset : CHUNK_BYTES);
        if (io_exact(fd, buffer, count, offset, 0)) return -1;
        if (memcmp(buffer, memory + offset, count)) {
            size_t mismatch = 0;
            while (mismatch < count && buffer[mismatch] == memory[offset + mismatch]) mismatch++;
            return failure("RAM byte comparison failed at offset %" PRIu64, offset + mismatch);
        }
        *hash = fnv_update(*hash, memory + offset, count);
        offset += count;
    }
    return validate_file(fd, bytes);
}

static int poison_allowed(const char *token, const char *gate)
{
    return token && gate && !strcmp(token, "POST_DEMOTION_TEST") && !strcmp(gate, "1");
}

static int verify_poison(const unsigned char *memory, uint64_t bytes, uint64_t *hash)
{
    *hash = FNV_OFFSET;
    for (uint64_t i = 0; i < bytes; i++) {
        if (memory[i] != POISON_BYTE) return failure("poison verification failed at offset %" PRIu64, i);
        *hash = (*hash ^ memory[i]) * FNV_PRIME;
    }
    return 0;
}

static int ram_transfer_main(int argc, char **argv)
{
    const char *usage = "usage: ram-transfer import|export|compare FILE /dev/dax0.0 BYTES\n"
        "       RAM_TRANSFER_ALLOW_POISON=1 ram-transfer poison POST_DEMOTION_TEST /dev/dax0.0 BYTES\n"
        "Requires externally fenced, quiescent, coherent owned lab; no ownership or durability protocol.\n";
    if (argc != 5) { fputs(usage, stderr); return 2; }
    const char *operation = argv[1];
    int importing = !strcmp(operation, "import"), exporting = !strcmp(operation, "export");
    int comparing = !strcmp(operation, "compare"), poisoning = !strcmp(operation, "poison");
    uint64_t bytes;
    if ((!importing && !exporting && !comparing && !poisoning) || parse_bytes(argv[4], &bytes) ||
        strcmp(argv[3], DAX_PATH) || (poisoning && !poison_allowed(argv[2], getenv("RAM_TRANSFER_ALLOW_POISON")))) {
        fputs(usage, stderr);
        return 2;
    }
    uint64_t started = now_ns(), operation_ns = 0, verify_ns = 0;
    uint64_t source_hash = FNV_OFFSET, destination_hash = FNV_OFFSET;
    struct dax_mapping dax = {.fd = -1, .data = MAP_FAILED};
    int fd = -1, result = 1;
    unsigned char *buffer = NULL;
    /* Validate DAX before creating an export file. No fallback to ordinary files. */
    if (map_dax(argv[3], bytes, importing || poisoning, &dax)) goto cleanup;
    if (!poisoning) {
        fd = exporting ? create_output(argv[2]) : open_input(argv[2], bytes);
        if (fd < 0) goto cleanup;
        buffer = malloc(CHUNK_BYTES);
        if (!buffer) { syscall_failure("allocate copy buffer"); goto cleanup; }
    }
    uint64_t phase = now_ns();
    if (poisoning) {
        memset(dax.data, POISON_BYTE, (size_t)bytes);
        atomic_thread_fence(memory_order_seq_cst);
    } else if (!comparing && copy_span(fd, dax.data, bytes, exporting, buffer, &source_hash)) goto cleanup;
    operation_ns = now_ns() - phase;
    phase = now_ns();
    if (poisoning) {
        if (verify_poison(dax.data, bytes, &destination_hash)) goto cleanup;
        source_hash = destination_hash;
    } else {
        if (compare_span(fd, dax.data, bytes, buffer, &destination_hash)) goto cleanup;
        if (comparing) source_hash = destination_hash;
        else if (source_hash != destination_hash) { failure("source changed during copy/verification"); goto cleanup; }
    }
    verify_ns = now_ns() - phase;
    if (fd >= 0) {
        int closed = close(fd);
        fd = -1;
        if (closed) { syscall_failure("close RAM file"); goto cleanup; }
    }
    if (munmap(dax.data, (size_t)dax.bytes)) { syscall_failure("unmap DAX"); goto cleanup; }
    dax.data = MAP_FAILED;
    if (close(dax.fd)) { dax.fd = -1; syscall_failure("close DAX"); goto cleanup; }
    dax.fd = -1;
    printf("RAM_TRANSFER_JSON {\"schema_version\":1,\"operation\":\"%s\",\"result\":\"passed\","
           "\"bytes\":%" PRIu64 ",\"copied_bytes\":%" PRIu64 ",\"verified_bytes\":%" PRIu64 ","
           "\"poisoned_bytes\":%" PRIu64 ",\"poison_byte\":%d,\"dax_offset\":%" PRIu64 ","
           "\"dax_align\":%" PRIu64 ",\"dax_capacity\":%" PRIu64 ",\"mapping_bytes\":%" PRIu64 ","
           "\"source_fnv1a64\":\"%016" PRIx64 "\",\"destination_fnv1a64\":\"%016" PRIx64 "\","
           "\"byte_comparison\":%s,\"operation_ns\":%" PRIu64 ",\"verify_ns\":%" PRIu64 ","
           "\"total_ns\":%" PRIu64 ",\"coherent_quiesced_lab_required\":true}\n",
           operation, bytes, importing || exporting ? bytes : UINT64_C(0), bytes,
           poisoning ? bytes : UINT64_C(0), poisoning ? POISON_BYTE : -1, RAM_OFFSET,
           dax.align, dax.capacity, dax.bytes, source_hash, destination_hash,
           poisoning ? "false" : "true", operation_ns, verify_ns, now_ns() - started);
    result = 0;
cleanup:
    if (result) fprintf(stderr, "ram-transfer: %s\n", last_error);
    free(buffer);
    if (fd >= 0) close(fd);
    if (dax.data != MAP_FAILED) munmap(dax.data, (size_t)dax.bytes);
    if (dax.fd >= 0) close(dax.fd);
    /* Failed exports deliberately remain incomplete and cannot be overwritten
     * by a retry. The owning controller decides when to remove its artifacts. */
    return result;
}

#ifndef RAM_TRANSFER_UNIT_TEST
int main(int argc, char **argv) { return ram_transfer_main(argc, argv); }
#else
/* Real ordinary-file I/O plus allocated-memory fixtures. No fake DAX mode. */
#define CHECK(name, condition) do { \
    if (!(condition)) { fprintf(stderr, "FAIL %s: %s\n", name, last_error); exit(1); } \
    printf("{\"test\":\"%s\",\"result\":\"passed\"}\n", name); checks++; \
} while (0)

int main(void)
{
    unsigned checks = 0;
    uint64_t value = 0, mapped = 0;
    const char *invalid[] = {"", "0", "4095", "4097", "2147487744", "18446744073709551616",
                             "-4096", "+4096", " 4096", "4096 ", "4096\n", "0x1000", "4K", "1e6"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        char name[64]; snprintf(name, sizeof(name), "invalid_bytes_%zu", i);
        CHECK(name, parse_bytes(invalid[i], &value) != 0);
    }
    CHECK("minimum_bytes", !parse_bytes("4096", &value) && value == 4096);
    CHECK("maximum_bytes", !parse_bytes("2147483648", &value) && value == MAX_BYTES);
    CHECK("page_aligned_non_huge_range", !layout(4096, RAM_OFFSET, RAM_OFFSET * 2, &mapped) && mapped == RAM_OFFSET);
    CHECK("alignment_too_small", layout(4096, 2048, RAM_OFFSET * 2, &mapped) != 0);
    CHECK("alignment_non_power_two", layout(4096, 12288, RAM_OFFSET * 2, &mapped) != 0);
    CHECK("alignment_exceeds_reserved_prefix", layout(4096, RAM_OFFSET * 2, RAM_OFFSET * 4, &mapped) != 0);
    CHECK("capacity_unaligned", layout(4096, RAM_OFFSET, RAM_OFFSET * 2 - 1, &mapped) != 0);
    CHECK("capacity_only_reserved_prefix", layout(4096, RAM_OFFSET, RAM_OFFSET, &mapped) != 0);
    CHECK("capacity_below_prefix", layout(4096, PAGE_BYTES, PAGE_BYTES, &mapped) != 0);
    CHECK("maximum_range_fits", !layout(MAX_BYTES, RAM_OFFSET, MAX_BYTES + RAM_OFFSET, &mapped) && mapped == MAX_BYTES);
    CHECK("maximum_range_short", layout(MAX_BYTES, RAM_OFFSET, MAX_BYTES, &mapped) != 0);
    CHECK("device_identity_matches", !device_identity(S_IFCHR, makedev(4, 9), "4:9"));
    CHECK("regular_file_is_not_dax", device_identity(S_IFREG, makedev(4, 9), "4:9") != 0);
    CHECK("device_major_mismatch", device_identity(S_IFCHR, makedev(4, 9), "5:9") != 0);
    CHECK("device_minor_mismatch", device_identity(S_IFCHR, makedev(4, 9), "4:10") != 0);
    CHECK("device_identity_malformed", device_identity(S_IFCHR, makedev(4, 9), "4:9:1") != 0);
    CHECK("device_identity_incomplete", device_identity(S_IFCHR, makedev(4, 9), "4:") != 0);
    CHECK("poison_requires_token", !poison_allowed("wrong", "1"));
    CHECK("poison_requires_env", !poison_allowed("POST_DEMOTION_TEST", NULL));
    CHECK("poison_rejects_other_env", !poison_allowed("POST_DEMOTION_TEST", "true"));
    CHECK("poison_gate", poison_allowed("POST_DEMOTION_TEST", "1"));
    CHECK("fnv_known_hello", fnv_update(FNV_OFFSET, (const unsigned char *)"hello", 5) == UINT64_C(0xa430d84680aabd0b));
    char template[] = "/tmp/ram-transfer-unit-XXXXXX";
    char *directory = mkdtemp(template);
    CHECK("fixture_directory", directory != NULL);
    char source[256], output[256], link[256], fifo[256];
    snprintf(source, sizeof(source), "%s/source", directory);
    snprintf(output, sizeof(output), "%s/output", directory);
    snprintf(link, sizeof(link), "%s/symlink", directory);
    snprintf(fifo, sizeof(fifo), "%s/fifo", directory);
    const uint64_t bytes = CHUNK_BYTES + PAGE_BYTES;
    unsigned char *allocation = malloc((size_t)(RAM_OFFSET + bytes + PAGE_BYTES));
    unsigned char *buffer = malloc(CHUNK_BYTES), *contents = malloc((size_t)bytes);
    CHECK("fixture_allocation", allocation && buffer && contents);
    memset(allocation, 0x7b, (size_t)(RAM_OFFSET + bytes + PAGE_BYTES));
    unsigned char *memory = allocation + RAM_OFFSET;
    uint64_t prefix_hash = fnv_update(FNV_OFFSET, allocation, (size_t)RAM_OFFSET);
    uint64_t tail_hash = fnv_update(FNV_OFFSET, memory + bytes, (size_t)PAGE_BYTES);
    for (uint64_t i = 0; i < bytes; i++) contents[i] = (unsigned char)(i * 71 + i / 4096);
    int fd = create_output(source);
    CHECK("new_output", fd >= 0);
    CHECK("write_fixture", !io_exact(fd, contents, (size_t)bytes, 0, 1));
    CHECK("exact_file_size", !validate_file(fd, bytes));
    CHECK("wrong_file_size", validate_file(fd, bytes - PAGE_BYTES) != 0);
    uint64_t first_hash, second_hash;
    CHECK("import_chunks", !copy_span(fd, memory, bytes, 0, buffer, &first_hash));
    CHECK("import_full_byte_verification", !compare_span(fd, memory, bytes, buffer, &second_hash) && first_hash == second_hash);
    CHECK("import_exact_bytes", !memcmp(memory, contents, (size_t)bytes));
    memory[CHUNK_BYTES + 17] ^= 1;
    CHECK("corruption_last_chunk", compare_span(fd, memory, bytes, buffer, &second_hash) != 0);
    memory[CHUNK_BYTES + 17] ^= 1;
    memory[0] ^= 1;
    CHECK("corruption_first_byte", compare_span(fd, memory, bytes, buffer, &second_hash) != 0);
    memory[0] ^= 1;
    close(fd);
    fd = open_input(source, bytes);
    CHECK("open_exact_input", fd >= 0);
    CHECK("write_failure_propagates", copy_span(fd, memory, bytes, 1, buffer, &first_hash) != 0);
    close(fd);
    CHECK("input_size_rejected", open_input(source, bytes + PAGE_BYTES) < 0);
    CHECK("directory_rejected", open_input(directory, bytes) < 0);
    CHECK("create_symlink", !symlink(source, link));
    CHECK("symlink_input_rejected", open_input(link, bytes) < 0);
    CHECK("existing_output_rejected", create_output(source) < 0);
    CHECK("symlink_output_rejected", create_output(link) < 0);
    CHECK("create_fifo", !mkfifo(fifo, 0600));
    CHECK("fifo_input_rejected_without_blocking", open_input(fifo, bytes) < 0);
    fd = create_output(output);
    CHECK("export_output_new", fd >= 0);
    CHECK("export_chunks_and_fsync", !copy_span(fd, memory, bytes, 1, buffer, &first_hash));
    CHECK("export_full_byte_verification", !compare_span(fd, memory, bytes, buffer, &second_hash) && first_hash == second_hash);
    CHECK("short_file_read", io_exact(fd, buffer, PAGE_BYTES, bytes, 0) != 0);
    CHECK("truncate_fixture", !ftruncate(fd, (off_t)(bytes - PAGE_BYTES)));
    CHECK("truncated_export_rejected", compare_span(fd, memory, bytes, buffer, &second_hash) != 0);
    close(fd);
    memset(memory, POISON_BYTE, (size_t)bytes);
    CHECK("poison_entire_span", !verify_poison(memory, bytes, &first_hash));
    memory[bytes - 1] = 0;
    CHECK("poison_corruption_rejected", verify_poison(memory, bytes, &second_hash) != 0);
    CHECK("reserved_prefix_untouched", prefix_hash == fnv_update(FNV_OFFSET, allocation, (size_t)RAM_OFFSET));
    CHECK("trailing_bytes_untouched", tail_hash == fnv_update(FNV_OFFSET, memory + bytes, (size_t)PAGE_BYTES));
    char *bad_cli[] = {"ram-transfer", "import", source, "/tmp/fake-dax", "4096", NULL};
    CHECK("cli_rejects_fake_dax_path", ram_transfer_main(5, bad_cli) == 2);
    bad_cli[1] = "unknown"; bad_cli[3] = DAX_PATH;
    CHECK("cli_rejects_unknown_operation", ram_transfer_main(5, bad_cli) == 2);
    CHECK("cli_rejects_extra_argument", ram_transfer_main(6, bad_cli) == 2);
    free(allocation); free(buffer); free(contents);
    CHECK("remove_fixture_source", !unlink(source));
    CHECK("remove_fixture_output", !unlink(output));
    CHECK("remove_fixture_symlink", !unlink(link));
    CHECK("remove_fixture_fifo", !unlink(fifo));
    CHECK("remove_fixture_directory", !rmdir(directory));
    printf("RAM_TRANSFER_UNIT_JSON {\"result\":\"passed\",\"checks\":%u,\"real_dax_exercised\":false}\n", checks);
    return 0;
}
#endif
