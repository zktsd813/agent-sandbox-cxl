#define _GNU_SOURCE
#include <ctype.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/*
 * File-cache probe for the host-NFS experiment.  The serve mode retains the
 * same open file description across a VM snapshot/restore.  All read results
 * are checked against a deterministic value derived from their file offset.
 *
 * stdin protocol (numbers are decimal bytes, ranges are page aligned):
 *   read LABEL OFFSET LENGTH BLOCK_BYTES seq|random SEED
 *   resident LABEL OFFSET LENGTH
 *   evict LABEL OFFSET LENGTH
 *   quit
 *
 * Each read visits every block of the range exactly once. read_ns counts only
 * time inside pread calls; wall_ns includes validation between requests and is
 * the end-to-end benchmark time. Neither includes mincore or disk-stat reads.
 */

#define MAX_LABEL 64
#define MAX_BLOCK (16U * 1024U * 1024U)
#define MAX_ORDER_BYTES (64U * 1024U * 1024U)
#define MIB (1024ULL * 1024ULL)

struct disk_stats {
    uint64_t reads, read_sectors, read_ms, writes, write_sectors;
    int valid;
};

struct probe {
    int fd;
    uint64_t size, cookie;
    size_t page_size;
    const char *stat_path;
};

static void die(const char *what)
{
    fprintf(stderr, "nfs-probe: %s: %s\n", what, strerror(errno));
    exit(1);
}

static void invalid(const char *what)
{
    fprintf(stderr, "nfs-probe: %s\n", what);
    exit(2);
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) die("clock_gettime");
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static uint64_t number(const char *s)
{
    char *end;
    unsigned long long value;
    if (!s[0] || !isdigit((unsigned char)s[0])) invalid("expected unsigned decimal integer");
    errno = 0;
    value = strtoull(s, &end, 10);
    if (errno || *end) invalid("invalid or overflowing integer");
    return (uint64_t)value;
}

static void label_check(const char *s)
{
    size_t n = strlen(s);
    if (!n || n > MAX_LABEL) invalid("label must contain 1..64 characters");
    for (size_t i = 0; i < n; i++)
        if (!isalnum((unsigned char)s[i]) && s[i] != '_' && s[i] != '-')
            invalid("label may contain only letters, digits, underscore and hyphen");
}

static uint64_t mix64(uint64_t x)
{
    x += UINT64_C(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
    return x ^ (x >> 31);
}

static uint64_t pattern(uint64_t word_offset)
{
    return mix64(word_offset ^ UINT64_C(0x4e465350524f4245));
}

static void generate(const char *path, uint64_t mib)
{
    if (!mib || mib > (uint64_t)INT64_MAX / MIB) invalid("invalid file size in MiB");
    uint64_t bytes = mib * MIB;
    size_t chunk = (size_t)MIB;
    uint64_t *buffer = malloc(chunk);
    if (!buffer) die("malloc generator");
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
    if (fd < 0) die("create data file (existing files are not overwritten)");
    for (uint64_t off = 0; off < bytes; off += chunk) {
        for (size_t i = 0; i < chunk / sizeof(*buffer); i++)
            buffer[i] = htole64(pattern(off / sizeof(*buffer) + i));
        size_t done = 0;
        while (done < chunk) {
            ssize_t n = write(fd, (char *)buffer + done, chunk - done);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { if (!n) errno = EIO; die("write data file"); }
            done += (size_t)n;
        }
    }
    if (fsync(fd)) die("fsync data file");
    if (close(fd)) die("close data file");
    free(buffer);
    printf("{\"event\":\"generated\",\"pattern\":\"splitmix64-offset-v1\",\"bytes\":%" PRIu64 "}\n", bytes);
}

static struct disk_stats disk_stats(const char *path)
{
    struct disk_stats s = {0};
    if (!path) return s;
    FILE *f = fopen(path, "r");
    if (!f) die("open block stat path");
    uint64_t merged_reads, merged_writes;
    int n = fscanf(f, "%" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64
                       " %" SCNu64 " %" SCNu64 " %" SCNu64,
                   &s.reads, &merged_reads, &s.read_sectors, &s.read_ms,
                   &s.writes, &merged_writes, &s.write_sectors);
    if (fclose(f)) die("close block stat path");
    if (n != 7) invalid("malformed block stat file");
    s.valid = 1;
    return s;
}

static void emit_stats(struct disk_stats a, struct disk_stats b)
{
    if (!a.valid || !b.valid) { printf(",\"block_delta\":null"); return; }
    if (b.reads < a.reads || b.read_sectors < a.read_sectors || b.read_ms < a.read_ms ||
        b.writes < a.writes || b.write_sectors < a.write_sectors)
        invalid("block counters moved backwards");
    printf(",\"block_delta\":{\"read_ios\":%" PRIu64 ",\"read_sectors\":%" PRIu64
           ",\"read_ms\":%" PRIu64 ",\"write_ios\":%" PRIu64 ",\"write_sectors\":%" PRIu64 "}",
           b.reads-a.reads, b.read_sectors-a.read_sectors, b.read_ms-a.read_ms,
           b.writes-a.writes, b.write_sectors-a.write_sectors);
}

static void range_check(const struct probe *p, uint64_t off, uint64_t len)
{
    if (!len || off > p->size || len > p->size-off || len > SIZE_MAX ||
        off % p->page_size || len % p->page_size)
        invalid("range must be nonempty, page aligned and within the file");
}

static uint64_t resident(const struct probe *p, uint64_t off, uint64_t len)
{
    range_check(p, off, len);
    size_t pages = (size_t)(len / p->page_size);
    unsigned char *vec = malloc(pages);
    if (!vec) die("malloc mincore vector");
    void *mapping = mmap(NULL, (size_t)len, PROT_READ, MAP_SHARED, p->fd, (off_t)off);
    if (mapping == MAP_FAILED) die("mmap for mincore");
    if (mincore(mapping, (size_t)len, vec)) die("mincore");
    uint64_t count = 0;
    for (size_t i = 0; i < pages; i++) count += !!(vec[i] & 1);
    if (munmap(mapping, (size_t)len)) die("munmap mincore");
    free(vec);
    return count;
}

static void emit_identity(const struct probe *p, const char *event, const char *label)
{
    printf("{\"event\":\"%s\",\"label\":\"%s\",\"pid\":%ld,\"open_cookie\":\"%016" PRIx64 "\"",
           event, label, (long)getpid(), p->cookie);
}

static int compare_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

static uint64_t percentile(const uint64_t *values, size_t count, unsigned percent)
{
    /* Nearest-rank percentile, with ranks starting at one. */
    size_t rank = (count * percent + 99) / 100;
    return values[rank ? rank - 1 : 0];
}

static void run_read(struct probe *p, const char *label, uint64_t off,
                     uint64_t len, uint64_t block, const char *mode, uint64_t seed)
{
    label_check(label);
    range_check(p, off, len);
    int random = !strcmp(mode, "random");
    if (!random && strcmp(mode, "seq")) invalid("read mode must be seq or random");
    if (!block || block > MAX_BLOCK || block % p->page_size || len % block)
        invalid("block must divide range and be a page multiple <= 16 MiB");
    uint64_t count = len / block;
    if (count > MAX_ORDER_BYTES / sizeof(uint64_t)) invalid("read block-order table exceeds 64 MiB");
    uint64_t *order = malloc((size_t)count * sizeof(*order));
    uint64_t *buffer = malloc((size_t)block);
    size_t latency_capacity = (size_t)count, latency_count = 0;
    uint64_t *latencies = malloc(latency_capacity * sizeof(*latencies));
    if (!order || !buffer || !latencies) die("malloc read buffers");
    /* Fault in the user buffer before measuring read latency. */
    for (size_t i = 0; i < block; i += p->page_size)
        ((volatile unsigned char *)buffer)[i] = 0;
    for (size_t i = 0; i < latency_capacity * sizeof(*latencies); i += p->page_size)
        ((volatile unsigned char *)latencies)[i] = 0;
    for (uint64_t i = 0; i < count; i++) order[i] = i;
    uint64_t rng = seed;
    if (random) {
        for (uint64_t i = count; i > 1; i--) {
            rng = mix64(rng);
            uint64_t j = rng % i, tmp = order[i-1];
            order[i-1] = order[j];
            order[j] = tmp;
        }
    }
    /* RANDOM disables readahead for random I/O; NORMAL resets prior advice. */
    int rc = posix_fadvise(p->fd, (off_t)off, (off_t)len,
                          random ? POSIX_FADV_RANDOM : POSIX_FADV_NORMAL);
    if (rc) { errno = rc; die("posix_fadvise access mode"); }
    uint64_t before = resident(p, off, len);
    struct disk_stats ds_before = disk_stats(p->stat_path);
    uint64_t start = now_ns(), read_ns = 0, validation_ns = 0;
    uint64_t checksum = 0, mismatches = 0, first_bad = UINT64_MAX, syscalls = 0;
    for (uint64_t i = 0; i < count; i++) {
        uint64_t position = off + order[i] * block;
        size_t done = 0;
        while (done < block) {
            uint64_t t = now_ns();
            ssize_t n = pread(p->fd, (char *)buffer+done, (size_t)block-done,
                              (off_t)(position+done));
            int saved_errno = errno;
            uint64_t latency = now_ns()-t;
            read_ns += latency;
            syscalls++;
            if (latency_count == latency_capacity) {
                /* Short reads or EINTR can require more calls than blocks. */
                size_t max_samples = MAX_ORDER_BYTES / sizeof(*latencies);
                if (latency_capacity == max_samples) invalid("too many pread latency samples");
                latency_capacity = latency_capacity > max_samples / 2 ? max_samples : latency_capacity * 2;
                uint64_t *grown = realloc(latencies, latency_capacity * sizeof(*latencies));
                if (!grown) die("grow pread latency samples");
                latencies = grown;
            }
            latencies[latency_count++] = latency;
            if (n < 0 && saved_errno == EINTR) continue;
            if (n <= 0) { errno = n ? saved_errno : EIO; die("pread data file"); }
            done += (size_t)n;
        }
        uint64_t t = now_ns();
        for (uint64_t w = 0; w < block / sizeof(*buffer); w++) {
            uint64_t value = le64toh(buffer[w]);
            uint64_t word_offset = position / sizeof(*buffer) + w;
            if (value != pattern(word_offset)) {
                mismatches++;
                if (first_bad == UINT64_MAX) first_bad = word_offset * sizeof(*buffer);
            }
            /* An order-independent checksum permits comparing random and seq. */
            checksum += value;
        }
        validation_ns += now_ns()-t;
    }
    uint64_t wall_ns = now_ns()-start;
    struct disk_stats ds_after = disk_stats(p->stat_path);
    uint64_t after = resident(p, off, len);
    qsort(latencies, latency_count, sizeof(*latencies), compare_u64);
    emit_identity(p, "read", label);
    printf(",\"mode\":\"%s\",\"offset\":%" PRIu64 ",\"bytes\":%" PRIu64
           ",\"block_bytes\":%" PRIu64 ",\"seed\":%" PRIu64
           ",\"blocks\":%" PRIu64 ",\"pread_calls\":%" PRIu64
           ",\"read_ns\":%" PRIu64 ",\"validation_ns\":%" PRIu64 ",\"wall_ns\":%" PRIu64
           ",\"read_mib_s\":%.6f,\"wall_mib_s\":%.6f"
           ",\"page_size\":%zu,\"pages\":%" PRIu64
           ",\"resident_before\":%" PRIu64 ",\"resident_after\":%" PRIu64
           ",\"checksum\":\"%016" PRIx64 "\",\"mismatched_words\":%" PRIu64,
           mode, off, len, block, seed, count, syscalls, read_ns, validation_ns, wall_ns,
           read_ns ? ((double)len / MIB) * 1e9 / read_ns : 0,
           wall_ns ? ((double)len / MIB) * 1e9 / wall_ns : 0,
           p->page_size, len / p->page_size, before, after, checksum, mismatches);
    printf(",\"pread_latency_ns\":{\"samples\":%zu,\"min\":%" PRIu64
           ",\"p50\":%" PRIu64 ",\"p95\":%" PRIu64 ",\"p99\":%" PRIu64 ",\"max\":%" PRIu64 "}",
           latency_count, latencies[0], percentile(latencies, latency_count, 50),
           percentile(latencies, latency_count, 95), percentile(latencies, latency_count, 99),
           latencies[latency_count-1]);
    if (mismatches) printf(",\"first_bad_offset\":%" PRIu64, first_bad);
    else printf(",\"first_bad_offset\":null");
    emit_stats(ds_before, ds_after);
    printf(",\"ok\":%s}\n", mismatches ? "false" : "true");
    free(buffer);
    free(order);
    free(latencies);
    if (mismatches) exit(1);
}

static struct probe open_probe(const char *path, const char *stat_path)
{
    struct probe p = {0};
    struct stat st;
    p.fd = open(path, O_RDONLY | O_CLOEXEC);
    if (p.fd < 0) die("open data file");
    if (fstat(p.fd, &st)) die("fstat data file");
    if (!S_ISREG(st.st_mode) || st.st_size <= 0) invalid("data file must be a nonempty regular file");
    p.size = (uint64_t)st.st_size;
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0) invalid("cannot determine page size");
    p.page_size = (size_t)page;
    p.cookie = mix64(now_ns() ^ (uint64_t)getpid());
    p.stat_path = stat_path;
    /* Fail before starting if an explicitly requested counter is unavailable. */
    (void)disk_stats(stat_path);
    return p;
}

static void run_cache(struct probe *p, const char *operation, const char *label,
                      uint64_t off, uint64_t len)
{
    label_check(label);
    uint64_t before = resident(p, off, len);
    if (!strcmp(operation, "evict")) {
        int rc = posix_fadvise(p->fd, (off_t)off, (off_t)len, POSIX_FADV_DONTNEED);
        if (rc) { errno = rc; die("posix_fadvise DONTNEED"); }
    }
    uint64_t after = resident(p, off, len);
    emit_identity(p, operation, label);
    printf(",\"offset\":%" PRIu64 ",\"bytes\":%" PRIu64 ",\"pages\":%" PRIu64
           ",\"resident_before\":%" PRIu64 ",\"resident_after\":%" PRIu64 "}\n",
           off, len, len / p->page_size, before, after);
}

static void serve(struct probe *p)
{
    emit_identity(p, "ready", "ready");
    printf(",\"file_bytes\":%" PRIu64 ",\"page_size\":%zu,\"pattern\":\"splitmix64-offset-v1\"}\n",
           p->size, p->page_size);
    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        if (!strchr(line, '\n') && !feof(stdin)) invalid("command line too long");
        char *args[9], *save = NULL;
        int argc = 0;
        for (char *a = strtok_r(line, " \t\r\n", &save); a;
             a = strtok_r(NULL, " \t\r\n", &save)) {
            if (argc == 9) invalid("too many command arguments");
            args[argc++] = a;
        }
        if (!argc) continue;
        if (!strcmp(args[0], "quit") && argc == 1) {
            emit_identity(p, "done", "done");
            printf("}\n");
            return;
        }
        if (!strcmp(args[0], "read") && argc == 7) {
            run_read(p, args[1], number(args[2]), number(args[3]), number(args[4]), args[5], number(args[6]));
        } else if ((!strcmp(args[0], "resident") || !strcmp(args[0], "evict")) && argc == 4) {
            run_cache(p, args[0], args[1], number(args[2]), number(args[3]));
        } else invalid("expected read, resident, evict or quit command");
    }
    if (ferror(stdin)) die("read command stream");
}

static void usage(void)
{
    fprintf(stderr,
        "Usage: nfs-probe generate FILE SIZE_MIB\n"
        "       nfs-probe serve FILE [BLOCK_STAT_PATH]\n"
        "       nfs-probe resident|evict FILE LABEL OFFSET LENGTH\n"
        "       nfs-probe read FILE LABEL OFFSET LENGTH BLOCK_BYTES seq|random SEED [BLOCK_STAT_PATH]\n");
    exit(2);
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc < 2) usage();
    if (!strcmp(argv[1], "generate")) {
        if (argc != 4) usage();
        generate(argv[2], number(argv[3]));
    } else if (!strcmp(argv[1], "serve")) {
        if (argc != 3 && argc != 4) usage();
        struct probe p = open_probe(argv[2], argc == 4 ? argv[3] : NULL);
        serve(&p);
        if (close(p.fd)) die("close data file");
    } else if (!strcmp(argv[1], "read")) {
        if (argc != 9 && argc != 10) usage();
        struct probe p = open_probe(argv[2], argc == 10 ? argv[9] : NULL);
        run_read(&p, argv[3], number(argv[4]), number(argv[5]), number(argv[6]), argv[7], number(argv[8]));
        if (close(p.fd)) die("close data file");
    } else if (!strcmp(argv[1], "resident") || !strcmp(argv[1], "evict")) {
        if (argc != 6) usage();
        struct probe p = open_probe(argv[2], NULL);
        run_cache(&p, argv[1], argv[3], number(argv[4]), number(argv[5]));
        if (close(p.fd)) die("close data file");
    } else usage();
    return 0;
}
