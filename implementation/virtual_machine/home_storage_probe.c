#define _GNU_SOURCE
#include <ctype.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* A deliberately small, disk-backed workload. Only generate creates a fixture.
 * serve DIR retains its FDs and mapping until quit; migration restores that
 * process, it must not start a second serve process on the same disk.
 *
 * write LABEL NEXT_GENERATION sync|dirty
 * read LABEL EXPECTED_GENERATION   (also advances two retained FD cursors)
 * identity LABEL
 * sync LABEL
 * evict LABEL                     (immutable payload only)
 * coldread LABEL OFFSET LENGTH     (immutable payload; 4096-byte alignment)
 * quit LABEL
 *
 * audit DIR EXPECTED_GENERATION is an independent, read-only inspection after
 * a fresh guest boot. Checks every data word, generation rename and journal.
 * FNV is an evidence checksum; correctness also compares every word directly.
 */
#define MIB (1024ULL * 1024ULL)
#define CHUNK (64U * 1024U)
#define MAP_BYTES 4096U
#define CURSOR_BYTES CHUNK
#define TAG_PAYLOAD UINT64_C(0x5041594c4f414400)
#define TAG_MUTABLE UINT64_C(0x4d555441424c4500)
#define TAG_MAPPING UINT64_C(0x4d415050494e4700)
#define TAG_CURSOR UINT64_C(0x435552534f520000)
#define TAG_UNLINKED UINT64_C(0x554e4c494e4b4544)

struct stats { uint64_t reads, read_sectors, writes, write_sectors; int valid; };
struct probe {
    int dir, mutable, mapped, payload, journal, cursor, unlinked;
    uint64_t generation, cookie, mutable_bytes, payload_bytes, command;
    uint64_t *mapping;
    char boot_id[40];
};

static void fail(const char *message)
{
    fprintf(stderr, "home-storage-probe: %s: %s\n", message, strerror(errno));
    exit(1);
}
static void require(int condition, const char *message)
{
    if (!condition) { errno = EINVAL; fail(message); }
}
static uint64_t number(const char *s)
{
    char *end;
    require(*s && isdigit((unsigned char)*s), "unsigned decimal required");
    errno = 0;
    uint64_t value = strtoull(s, &end, 10);
    require(!errno && !*end, "invalid integer");
    return value;
}
static void label_check(const char *s)
{
    require(*s && strlen(s) <= 64, "invalid label length");
    for (; *s; s++) require(isalnum((unsigned char)*s) || *s == '_' || *s == '-', "invalid label");
}
static uint64_t mix64(uint64_t x)
{
    x += UINT64_C(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
    return x ^ (x >> 31);
}
static uint64_t pattern(uint64_t tag, uint64_t generation, uint64_t word)
{
    return mix64(tag ^ (generation * UINT64_C(0xd6e8feb86659fd93)) ^ word);
}
static uint64_t hash_bytes(uint64_t hash, const void *data, size_t count)
{
    const unsigned char *p = data;
    for (size_t i = 0; i < count; i++) hash = (hash ^ p[i]) * UINT64_C(1099511628211);
    return hash;
}
static int open_file(const char *name, int flags)
{
    int fd = open(name, flags | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) fail(name);
    struct stat st;
    if (fstat(fd, &st)) fail("fstat");
    require(S_ISREG(st.st_mode), "expected regular file");
    return fd;
}
static uint64_t file_size(int fd)
{
    struct stat st;
    if (fstat(fd, &st)) fail("fstat size");
    require(st.st_size >= 0, "negative size");
    return (uint64_t)st.st_size;
}
static void exact_io(int fd, void *buffer, size_t bytes, uint64_t offset, int write_mode)
{
    size_t done = 0;
    while (done < bytes) {
        ssize_t n = write_mode ? pwrite(fd, (char *)buffer + done, bytes - done, (off_t)(offset + done))
                               : pread(fd, (char *)buffer + done, bytes - done, (off_t)(offset + done));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { if (!n) errno = EIO; fail(write_mode ? "pwrite" : "pread"); }
        done += (size_t)n;
    }
}
static uint64_t fill_file(int fd, uint64_t bytes, uint64_t tag, uint64_t generation)
{
    uint64_t buffer[CHUNK / 8], hash = UINT64_C(14695981039346656037);
    require(bytes && bytes % 8 == 0, "bad fill size");
    for (uint64_t off = 0; off < bytes;) {
        size_t count = bytes - off < CHUNK ? (size_t)(bytes - off) : CHUNK;
        for (size_t i = 0; i < count / 8; i++) buffer[i] = htole64(pattern(tag, generation, off / 8 + i));
        exact_io(fd, buffer, count, off, 1);
        hash = hash_bytes(hash, buffer, count);
        off += count;
    }
    return hash;
}
static uint64_t check_file(int fd, uint64_t offset, uint64_t bytes, uint64_t tag, uint64_t generation)
{
    uint64_t buffer[CHUNK / 8], hash = UINT64_C(14695981039346656037);
    require(bytes && bytes % 8 == 0 && offset % 8 == 0, "bad check size");
    for (uint64_t off = 0; off < bytes;) {
        size_t count = bytes - off < CHUNK ? (size_t)(bytes - off) : CHUNK;
        exact_io(fd, buffer, count, offset + off, 0);
        for (size_t i = 0; i < count / 8; i++)
            require(le64toh(buffer[i]) == pattern(tag, generation, (offset + off) / 8 + i), "file data mismatch");
        hash = hash_bytes(hash, buffer, count);
        off += count;
    }
    return hash;
}
static uint64_t generation_read(void)
{
    int fd = open_file("generation", O_RDONLY);
    char text[64] = {0};
    uint64_t size = file_size(fd);
    require(size > 1 && size < sizeof(text), "generation size");
    exact_io(fd, text, (size_t)size, 0, 0);
    require(text[size - 1] == '\n', "generation newline");
    text[size - 1] = 0;
    if (close(fd)) fail("close generation");
    uint64_t value = number(text);
    require(value <= UINT32_MAX, "generation too large");
    return value;
}
static void fixture_sizes(uint64_t *payload, uint64_t *mutable)
{
    int fd = open_file("fixture", O_RDONLY);
    char text[160] = {0}, extra;
    uint64_t bytes = file_size(fd);
    require(bytes > 0 && bytes < sizeof(text), "fixture descriptor size");
    exact_io(fd, text, (size_t)bytes, 0, 0);
    require(sscanf(text, "home-storage-v1\n%" SCNu64 " %" SCNu64 " %c", payload, mutable, &extra) == 2,
            "fixture descriptor syntax");
    require(*payload >= MIB && *payload <= 1024 * MIB && *payload % MIB == 0 &&
            *mutable >= MIB && *mutable <= 256 * MIB && *mutable % MIB == 0, "fixture descriptor sizes");
    if (close(fd)) fail("close fixture descriptor");
}
static void generation_write(uint64_t generation)
{
    int fd = open_file("generation.next", O_WRONLY | O_CREAT | O_EXCL);
    char text[64];
    int n = snprintf(text, sizeof(text), "%" PRIu64 "\n", generation);
    exact_io(fd, text, (size_t)n, 0, 1);
    if (close(fd) || rename("generation.next", "generation")) fail("rename generation");
}
static void journal_check(uint64_t expected)
{
    FILE *f = fopen("journal.log", "r");
    if (!f) fail("open journal");
    char line[96], want[96];
    for (uint64_t i = 1; i <= expected; i++) {
        snprintf(want, sizeof(want), "generation=%" PRIu64 "\n", i);
        require(fgets(line, sizeof(line), f) != NULL && !strcmp(line, want), "journal sequence");
    }
    require(fgetc(f) == EOF && !ferror(f), "journal trailing data");
    if (fclose(f)) fail("close journal");
}
static struct stats stats_read(void)
{
    struct stats s = {0};
    FILE *f = fopen("/sys/class/block/vda/stat", "r");
    if (!f) return s; /* Host unit tests have no vda. */
    uint64_t rm, rtime, wm;
    int n = fscanf(f, "%" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64
                       " %" SCNu64 " %" SCNu64 " %" SCNu64,
                   &s.reads, &rm, &s.read_sectors, &rtime, &s.writes, &wm, &s.write_sectors);
    if (fclose(f)) fail("close block stats");
    require(n == 7, "invalid block stats");
    s.valid = 1;
    return s;
}
static uint64_t resident(int fd, uint64_t size)
{
    require(size && size % 4096 == 0, "resident size");
    unsigned char *vec = malloc((size_t)(size / 4096));
    if (!vec) fail("mincore allocation");
    void *p = mmap(NULL, (size_t)size, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED || mincore(p, (size_t)size, vec)) fail("mincore");
    uint64_t pages = 0;
    for (uint64_t i = 0; i < size / 4096; i++) pages += !!(vec[i] & 1);
    if (munmap(p, (size_t)size)) fail("munmap resident");
    free(vec);
    return pages;
}
static uint64_t dirty_kib(void)
{
    FILE *f = fopen("/proc/meminfo", "r");
    if (!f) fail("open meminfo");
    char line[256];
    uint64_t value = UINT64_MAX;
    while (fgets(line, sizeof(line), f)) if (sscanf(line, "Dirty: %" SCNu64 " kB", &value) == 1) break;
    if (fclose(f)) fail("close meminfo");
    require(value != UINT64_MAX, "missing Dirty");
    return value;
}
static void boot_id_read(char *buffer, size_t size)
{
    FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r");
    if (!f || !fgets(buffer, (int)size, f)) fail("boot id");
    if (fclose(f)) fail("close boot id");
    buffer[strcspn(buffer, "\r\n")] = 0;
    require(strlen(buffer) == 36, "boot id length");
}
static void sync_files(struct probe *p)
{
    if (msync(p->mapping, MAP_BYTES, MS_SYNC) || fsync(p->mutable) || fsync(p->mapped) || fsync(p->journal))
        fail("fsync workload");
    int fd = open_file("generation", O_RDONLY);
    if (fsync(fd) || close(fd) || fsync(p->dir)) fail("fsync generation/directory");
}
static void emit(struct probe *p, const char *label, const char *op,
                 uint64_t mutable_hash, uint64_t mapped_hash, uint64_t payload_hash,
                 uint64_t resident_before, struct stats before, int synced)
{
    struct stat ms, cs, us;
    if (fstat(p->mutable, &ms) || fstat(p->cursor, &cs) || fstat(p->unlinked, &us)) fail("identity fstat");
    off_t co = lseek(p->cursor, 0, SEEK_CUR), uo = lseek(p->unlinked, 0, SEEK_CUR);
    if (co < 0 || uo < 0) fail("identity offset");
    require(us.st_nlink == 0 && ms.st_nlink == 1, "link identity mismatch");
    struct stats after = stats_read();
    printf("{\"event\":\"home_storage\",\"label\":\"%s\",\"op\":\"%s\",\"command\":%" PRIu64
           ",\"boot_id\":\"%s\",\"pid\":%ld,\"open_cookie\":\"%016" PRIx64
           "\",\"generation\":%" PRIu64 ",\"mutable_inode\":%" PRIu64 ",\"cursor_inode\":%" PRIu64
           ",\"cursor_offset\":%" PRIu64 ",\"unlinked_inode\":%" PRIu64 ",\"unlinked_offset\":%" PRIu64
           ",\"unlinked_nlink\":%" PRIu64 ",\"mutable_bytes\":%" PRIu64 ",\"payload_bytes\":%" PRIu64
           ",\"mutable_hash\":\"%016" PRIx64 "\",\"mapped_hash\":\"%016" PRIx64
           "\",\"payload_hash\":\"%016" PRIx64 "\",\"payload_resident_before\":%" PRIu64
           ",\"payload_resident_after\":%" PRIu64 ",\"dirty_kib\":%" PRIu64 ",\"synced\":%s",
           label, op, ++p->command, p->boot_id, (long)getpid(), p->cookie, p->generation,
           (uint64_t)ms.st_ino, (uint64_t)cs.st_ino, (uint64_t)co, (uint64_t)us.st_ino, (uint64_t)uo,
           (uint64_t)us.st_nlink, p->mutable_bytes, p->payload_bytes, mutable_hash, mapped_hash, payload_hash,
           resident_before, resident(p->payload, p->payload_bytes), dirty_kib(), synced ? "true" : "false");
    if (before.valid && after.valid) {
        require(after.reads >= before.reads && after.read_sectors >= before.read_sectors &&
                after.writes >= before.writes && after.write_sectors >= before.write_sectors, "block counters regressed");
        printf(",\"block_delta\":{\"read_ios\":%" PRIu64 ",\"read_sectors\":%" PRIu64
               ",\"write_ios\":%" PRIu64 ",\"write_sectors\":%" PRIu64 "}",
               after.reads - before.reads, after.read_sectors - before.read_sectors,
               after.writes - before.writes, after.write_sectors - before.write_sectors);
    } else printf(",\"block_delta\":null");
    printf(",\"ok\":true}\n");
}
static void advance(int fd, uint64_t tag)
{
    off_t offset = lseek(fd, 0, SEEK_CUR);
    require(offset >= 0 && (uint64_t)offset + 16 <= CURSOR_BYTES, "cursor exhausted");
    uint64_t buffer[2];
    ssize_t n = read(fd, buffer, sizeof(buffer));
    require(n == (ssize_t)sizeof(buffer), "cursor short read");
    for (unsigned i = 0; i < 2; i++)
        require(le64toh(buffer[i]) == pattern(tag, 0, (uint64_t)offset / 8 + i), "cursor contents");
}
static uint64_t check_mapping(struct probe *p)
{
    for (size_t i = 0; i < MAP_BYTES / 8; i++)
        require(le64toh(p->mapping[i]) == pattern(TAG_MAPPING, p->generation, i), "mapping mismatch");
    return hash_bytes(UINT64_C(14695981039346656037), p->mapping, MAP_BYTES);
}
static void serve(const char *directory)
{
    if (chdir(directory)) fail("chdir fixture");
    struct probe p = {0};
    p.dir = open(".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (p.dir < 0) fail("directory fd");
    p.mutable = open_file("mutable.bin", O_RDWR);
    p.mapped = open_file("mapped.bin", O_RDWR);
    p.payload = open_file("payload.bin", O_RDONLY);
    p.journal = open_file("journal.log", O_WRONLY | O_APPEND);
    p.cursor = open_file("cursor.bin", O_RDONLY);
    p.unlinked = open_file("unlinked-live.bin", O_RDWR | O_CREAT | O_EXCL);
    fill_file(p.unlinked, CURSOR_BYTES, TAG_UNLINKED, 0);
    if (unlink("unlinked-live.bin")) fail("unlink retained file");
    fixture_sizes(&p.payload_bytes, &p.mutable_bytes);
    require(file_size(p.mutable) == p.mutable_bytes && file_size(p.payload) == p.payload_bytes &&
            file_size(p.mapped) == MAP_BYTES && file_size(p.cursor) == CURSOR_BYTES, "fixture sizes");
    p.generation = generation_read();
    journal_check(p.generation);
    if (getrandom(&p.cookie, sizeof(p.cookie), 0) != sizeof(p.cookie)) fail("getrandom");
    boot_id_read(p.boot_id, sizeof(p.boot_id));
    p.mapping = mmap(NULL, MAP_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, p.mapped, 0);
    if (p.mapping == MAP_FAILED) fail("persistent mmap");
    check_file(p.mutable, 0, p.mutable_bytes, TAG_MUTABLE, p.generation);
    check_mapping(&p);
    printf("{\"event\":\"home_ready\",\"boot_id\":\"%s\",\"pid\":%ld,\"generation\":%" PRIu64
           ",\"mutable_bytes\":%" PRIu64 ",\"payload_bytes\":%" PRIu64 "}\n",
           p.boot_id, (long)getpid(), p.generation, p.mutable_bytes, p.payload_bytes);
    char line[256], *words[8];
    while (fgets(line, sizeof(line), stdin)) {
        require(strchr(line, '\n') != NULL, "overlong or incomplete command");
        size_t count = 0;
        char *save = NULL;
        for (char *w = strtok_r(line, " \t\r\n", &save); w; w = strtok_r(NULL, " \t\r\n", &save)) {
            require(count < 8, "too many command fields");
            words[count++] = w;
        }
        require(count >= 2, "missing operation or label");
        label_check(words[1]);
        uint64_t mh = 0, ah = 0, ph = 0, rb = resident(p.payload, p.payload_bytes);
        struct stats before = stats_read();
        int synced = 0, quitting = 0;
        if (!strcmp(words[0], "write")) {
            require(count == 4, "write syntax");
            uint64_t next = number(words[2]);
            require(next <= UINT32_MAX && next == p.generation + 1, "write generation must be next");
            require(!strcmp(words[3], "sync") || !strcmp(words[3], "dirty"), "write sync mode");
            mh = fill_file(p.mutable, p.mutable_bytes, TAG_MUTABLE, next);
            for (size_t i = 0; i < MAP_BYTES / 8; i++) p.mapping[i] = htole64(pattern(TAG_MAPPING, next, i));
            p.generation = next;
            ah = check_mapping(&p);
            generation_write(next);
            char text[96];
            int n = snprintf(text, sizeof(text), "generation=%" PRIu64 "\n", next);
            require(write(p.journal, text, (size_t)n) == n, "journal write");
            if (!strcmp(words[3], "sync")) { sync_files(&p); synced = 1; }
        } else if (!strcmp(words[0], "read")) {
            require(count == 3 && number(words[2]) == p.generation, "read generation");
            require(generation_read() == p.generation, "renamed generation mismatch");
            journal_check(p.generation);
            mh = check_file(p.mutable, 0, p.mutable_bytes, TAG_MUTABLE, p.generation);
            ah = check_mapping(&p);
            advance(p.cursor, TAG_CURSOR);
            advance(p.unlinked, TAG_UNLINKED);
        } else if (!strcmp(words[0], "coldread")) {
            require(count == 4, "coldread syntax");
            uint64_t off = number(words[2]), bytes = number(words[3]);
            require(bytes && bytes % 4096 == 0 && off % 4096 == 0 && off <= p.payload_bytes &&
                    bytes <= p.payload_bytes - off, "coldread range");
            ph = check_file(p.payload, off, bytes, TAG_PAYLOAD, 0);
        } else if (!strcmp(words[0], "identity")) {
            require(count == 2, "identity syntax");
            ah = check_mapping(&p);
        } else if (!strcmp(words[0], "sync") || !strcmp(words[0], "quit")) {
            require(count == 2, "sync/quit syntax");
            sync_files(&p);
            synced = 1;
            quitting = !strcmp(words[0], "quit");
        } else if (!strcmp(words[0], "evict")) {
            require(count == 2, "evict syntax");
            int rc = posix_fadvise(p.payload, 0, 0, POSIX_FADV_DONTNEED);
            if (rc) { errno = rc; fail("evict immutable payload"); }
        } else require(0, "unknown command");
        emit(&p, words[1], words[0], mh, ah, ph, rb, before, synced);
        if (quitting) {
            if (munmap(p.mapping, MAP_BYTES) || close(p.mutable) || close(p.mapped) || close(p.payload) ||
                close(p.journal) || close(p.cursor) || close(p.unlinked) || fsync(p.dir) || close(p.dir))
                fail("close workload");
            return;
        }
    }
    require(0, "unexpected protocol EOF");
}
static void generate(const char *directory, uint64_t payload_mib, uint64_t mutable_mib)
{
    require(payload_mib >= 1 && payload_mib <= 1024 && mutable_mib >= 1 && mutable_mib <= 256, "generator sizes");
    if (mkdir(directory, 0700) || chdir(directory)) fail("create fresh fixture directory");
    const char *names[] = {"payload.bin", "mutable.bin", "mapped.bin", "cursor.bin"};
    uint64_t sizes[] = {payload_mib * MIB, mutable_mib * MIB, MAP_BYTES, CURSOR_BYTES};
    uint64_t tags[] = {TAG_PAYLOAD, TAG_MUTABLE, TAG_MAPPING, TAG_CURSOR};
    for (size_t i = 0; i < 4; i++) {
        int fd = open_file(names[i], O_WRONLY | O_CREAT | O_EXCL);
        fill_file(fd, sizes[i], tags[i], 0);
        if (fsync(fd) || close(fd)) fail("sync generated file");
    }
    int journal = open_file("journal.log", O_WRONLY | O_CREAT | O_EXCL);
    if (fsync(journal) || close(journal)) fail("sync empty journal");
    int descriptor = open_file("fixture", O_WRONLY | O_CREAT | O_EXCL);
    char description[160];
    int length = snprintf(description, sizeof(description), "home-storage-v1\n%" PRIu64 " %" PRIu64 "\n",
                          sizes[0], sizes[1]);
    exact_io(descriptor, description, (size_t)length, 0, 1);
    if (fsync(descriptor) || close(descriptor)) fail("sync fixture descriptor");
    generation_write(0);
    int gen = open_file("generation", O_RDONLY), dir = open(".", O_DIRECTORY | O_RDONLY);
    if (gen < 0 || dir < 0 || fsync(gen) || fsync(dir) || close(gen) || close(dir)) fail("sync fixture");
    printf("{\"event\":\"home_generated\",\"payload_bytes\":%" PRIu64 ",\"mutable_bytes\":%" PRIu64 "}\n", sizes[0], sizes[1]);
}
static void audit(const char *directory, uint64_t expected)
{
    require(expected <= UINT32_MAX, "audit generation");
    if (chdir(directory)) fail("audit chdir");
    require(generation_read() == expected, "audit generation mismatch");
    journal_check(expected);
    struct stat st;
    errno = 0;
    require(lstat("generation.next", &st) == -1 && errno == ENOENT, "incomplete generation rename");
    errno = 0;
    require(lstat("unlinked-live.bin", &st) == -1 && errno == ENOENT, "unlinked file persisted as a name");
    int m = open_file("mutable.bin", O_RDONLY), a = open_file("mapped.bin", O_RDONLY);
    int p = open_file("payload.bin", O_RDONLY), c = open_file("cursor.bin", O_RDONLY);
    uint64_t payload_bytes, mutable_bytes;
    fixture_sizes(&payload_bytes, &mutable_bytes);
    require(file_size(m) == mutable_bytes && file_size(p) == payload_bytes &&
            file_size(a) == MAP_BYTES && file_size(c) == CURSOR_BYTES, "audit fixture sizes");
    uint64_t mh = check_file(m, 0, file_size(m), TAG_MUTABLE, expected);
    uint64_t ah = check_file(a, 0, MAP_BYTES, TAG_MAPPING, expected);
    uint64_t ph = check_file(p, 0, file_size(p), TAG_PAYLOAD, 0);
    uint64_t ch = check_file(c, 0, CURSOR_BYTES, TAG_CURSOR, 0);
    char boot[40];
    boot_id_read(boot, sizeof(boot));
    printf("{\"event\":\"home_audit\",\"boot_id\":\"%s\",\"generation\":%" PRIu64
           ",\"mutable_bytes\":%" PRIu64 ",\"payload_bytes\":%" PRIu64
           ",\"mutable_hash\":\"%016" PRIx64 "\",\"mapped_hash\":\"%016" PRIx64
           "\",\"payload_hash\":\"%016" PRIx64 "\",\"cursor_hash\":\"%016" PRIx64 "\",\"ok\":true}\n",
           boot, expected, file_size(m), file_size(p), mh, ah, ph, ch);
    if (close(m) || close(a) || close(p) || close(c)) fail("audit close");
}
int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc == 5 && !strcmp(argv[1], "generate")) generate(argv[2], number(argv[3]), number(argv[4]));
    else if (argc == 3 && !strcmp(argv[1], "serve")) serve(argv[2]);
    else if (argc == 4 && !strcmp(argv[1], "audit")) audit(argv[2], number(argv[3]));
    else { fprintf(stderr, "Usage: home-storage-probe generate DIR PAYLOAD_MIB MUTABLE_MIB | serve DIR | audit DIR GENERATION\n"); return 2; }
    return 0;
}
