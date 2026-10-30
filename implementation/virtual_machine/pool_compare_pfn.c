/* Validation-only PFN reader for identical payloads in separately owned VMMs.
 * Input: file_offset guest_gpa host_hva (decimal, one page per line).
 * Output: the same three columns, actual host PFN, raw pagemap entry.
 * GPA-to-HVA translation and pausing are controller responsibilities. This
 * reader has no canonical-pool knowledge and cannot manufacture shared PFNs.
 */
#define _GNU_SOURCE
#include <ctype.h>
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
#include <sys/types.h>
#include <unistd.h>

#define PAGE 4096U
#define MAX_ROWS (1024U * 1024U)
#define PFN_MASK ((UINT64_C(1) << 55) - 1)
struct row { uint64_t file_offset, gpa, hva, pfn, raw; };

static void die(const char *message) {
    fprintf(stderr, "POOL_COMPARE_FATAL %s: %s\n", message, strerror(errno));
    exit(1);
}
static void require(int condition, const char *message) {
    if (!condition) { errno = EINVAL; die(message); }
}
static uint64_t number(char **cursor) {
    char *start = *cursor, *end;
    while (isspace((unsigned char)*start)) start++;
    require(isdigit((unsigned char)*start), "unsigned decimal field required");
    errno = 0;
    uint64_t value = strtoull(start, &end, 10);
    require(errno == 0 && end != start && (!*end || isspace((unsigned char)*end)),
            "invalid or overflowing decimal field");
    *cursor = end;
    return value;
}
static void end_line(char *cursor) {
    while (isspace((unsigned char)*cursor)) cursor++;
    require(!*cursor, "unexpected trailing input");
}
static int cmp_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}
static void unique(uint64_t *values, size_t count, const char *message) {
    qsort(values, count, sizeof(*values), cmp_u64);
    for (size_t i = 1; i < count; i++) require(values[i-1] != values[i], message);
}
static uint64_t process_start_time(int procdir) {
    int fd = openat(procdir, "stat", O_RDONLY | O_CLOEXEC);
    if (fd < 0) die("open process stat");
    char buffer[4096];
    ssize_t n = read(fd, buffer, sizeof(buffer)-1);
    if (n <= 0) die("read process stat");
    require((size_t)n < sizeof(buffer)-1, "truncated process stat");
    buffer[n] = 0;
    if (close(fd)) die("close process stat");
    char *tail = strrchr(buffer, ')'), *save = NULL;
    require(tail != NULL, "process stat command field");
    char *token = strtok_r(tail+1, " \t\n", &save);
    for (unsigned field = 3; field <= 22; field++) {
        require(token != NULL, "incomplete process stat");
        if (field == 3) require(strcmp(token,"Z") && strcmp(token,"X"), "VMM is dead");
        if (field == 22) {
            char *cursor = token;
            uint64_t value = number(&cursor);
            end_line(cursor);
            require(value != 0, "missing process incarnation");
            return value;
        }
        token = strtok_r(NULL, " \t\n", &save);
    }
    errno = EINVAL; die("process stat start time"); return 0;
}
static void pread_exact(int fd, void *buffer, size_t bytes, uint64_t offset) {
    size_t done = 0;
    require(offset <= INT64_MAX && bytes <= (uint64_t)INT64_MAX-offset,
            "file offset overflow");
    while (done < bytes) {
        ssize_t n = pread(fd, (char *)buffer+done, bytes-done, (off_t)(offset+done));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { if (!n) errno = EIO; die("pread exact"); }
        done += (size_t)n;
    }
}
static int hostwarm(const char *path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) die("open hostwarm file");
    struct stat st;
    if (fstat(fd, &st)) die("stat hostwarm file");
    require(S_ISREG(st.st_mode) && st.st_size > 0, "hostwarm needs nonempty regular file");
    require((uint64_t)st.st_size <= SIZE_MAX-PAGE, "hostwarm size overflow");
    uint64_t bytes = (uint64_t)st.st_size, hash = UINT64_C(14695981039346656037);
    unsigned char buffer[256*1024];
    for (uint64_t off = 0; off < bytes;) {
        size_t count = bytes-off < sizeof(buffer) ? (size_t)(bytes-off) : sizeof(buffer);
        pread_exact(fd, buffer, count, off);
        for (size_t i = 0; i < count; i++) hash = (hash ^ buffer[i]) * UINT64_C(1099511628211);
        off += count;
    }
    size_t pages = ((size_t)bytes+PAGE-1)/PAGE;
    unsigned char *vec = malloc(pages);
    if (!vec) die("allocate mincore vector");
    void *mapping = mmap(NULL, (size_t)bytes, PROT_READ, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED) die("hostwarm mmap");
    if (mincore(mapping, (size_t)bytes, vec)) die("hostwarm mincore");
    size_t resident = 0;
    for (size_t i = 0; i < pages; i++) resident += !!(vec[i]&1);
    require(resident == pages, "host image not fully resident after pread");
    if (munmap(mapping, (size_t)bytes) || close(fd)) die("hostwarm cleanup");
    free(vec);
    printf("POOL_COMPARE_HOSTWARM_JSON {\"bytes\":%" PRIu64 ",\"bytes_read\":%" PRIu64
           ",\"pages\":%zu,\"resident_pages\":%zu,\"all_resident\":true,"
           "\"fnv64\":\"%016" PRIx64 "\"}\n", bytes, bytes, pages, resident, hash);
    return 0;
}
int main(int argc, char **argv) {
    require(sysconf(_SC_PAGESIZE) == PAGE, "only 4KiB host pages supported");
    if (argc == 3 && !strcmp(argv[1], "hostwarm")) return hostwarm(argv[2]);
    require(argc == 4, "usage: pool-compare-pfn PID ROWS_TSV OUT_TSV | hostwarm FILE");
    char *pid_text = argv[1];
    uint64_t pid = number(&pid_text); end_line(pid_text);
    require(pid > 1 && pid <= INT_MAX, "invalid VMM PID");
    FILE *input = fopen(argv[2], "re");
    if (!input) die("open input rows");
    size_t count = 0, capacity = 0, line_capacity = 0;
    struct row *rows = NULL;
    char *line = NULL;
    while (getline(&line, &line_capacity, input) >= 0) {
        require(count < MAX_ROWS, "too many input rows");
        if (count == capacity) {
            capacity = capacity ? capacity*2 : 4096;
            void *next = realloc(rows, capacity*sizeof(*rows));
            if (!next) die("allocate rows");
            rows = next;
        }
        char *cursor = line;
        struct row r = {0};
        r.file_offset = number(&cursor);
        r.gpa = number(&cursor);
        r.hva = number(&cursor);
        end_line(cursor);
        require(r.file_offset == count*(uint64_t)PAGE, "file offsets must cover consecutive pages from zero");
        require(r.gpa%PAGE == 0 && r.hva%PAGE == 0 && r.hva != 0, "unaligned or null address");
        rows[count++] = r;
    }
    require(!ferror(input), "read input rows");
    if (fclose(input)) die("close input rows");
    free(line);
    require(count != 0, "empty input rows");
    uint64_t *values = malloc(count*sizeof(*values));
    if (!values) die("allocate uniqueness index");
    for (size_t i = 0; i < count; i++) values[i] = rows[i].gpa;
    unique(values, count, "duplicate guest GPA");
    for (size_t i = 0; i < count; i++) values[i] = rows[i].hva;
    unique(values, count, "duplicate host HVA");
    char path[80];
    int n = snprintf(path, sizeof(path), "/proc/%" PRIu64, pid);
    require(n > 0 && (size_t)n < sizeof(path), "process path overflow");
    int procdir = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (procdir < 0) die("open VMM proc directory");
    struct stat process_stat;
    if (fstat(procdir, &process_stat)) die("stat VMM proc directory");
    require(process_stat.st_uid == geteuid(), "VMM is not owned by scanner effective UID");
    uint64_t start = process_start_time(procdir);
    int pagemap = openat(procdir, "pagemap", O_RDONLY | O_CLOEXEC);
    if (pagemap < 0) die("open VMM pagemap");
    for (size_t i = 0; i < count; i++) {
        uint64_t index = rows[i].hva/PAGE;
        require(index <= INT64_MAX/sizeof(uint64_t), "pagemap index overflow");
        pread_exact(pagemap, &rows[i].raw, sizeof(rows[i].raw), index*sizeof(uint64_t));
        require((rows[i].raw >> 63) && !(rows[i].raw & (UINT64_C(1)<<62)),
                "payload host page absent or swapped");
        rows[i].pfn = rows[i].raw & PFN_MASK;
        require(rows[i].pfn != 0, "PFN unavailable or zero; privileges/evidence insufficient");
        values[i] = rows[i].pfn;
    }
    unique(values, count, "duplicate PFN within one random payload");
    require(process_start_time(procdir) == start, "VMM process incarnation changed");
    if (close(pagemap) || close(procdir)) die("close VMM proc files");
    FILE *output = fopen(argv[3], "we");
    if (!output) die("open output rows");
    for (size_t i = 0; i < count; i++) {
        const struct row *r = &rows[i];
        if (fprintf(output, "%" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 "\n",
                    r->file_offset, r->gpa, r->hva, r->pfn, r->raw) < 0) die("write output rows");
    }
    if (fclose(output)) die("close output rows");
    printf("POOL_COMPARE_PFN_JSON {\"pid\":%" PRIu64 ",\"process_start_time\":%" PRIu64
           ",\"rows\":%zu,\"unique_pfns\":%zu,\"bytes\":%zu,\"all_checked\":true}\n",
           pid, start, count, count, count*PAGE);
    free(values); free(rows);
    return 0;
}
