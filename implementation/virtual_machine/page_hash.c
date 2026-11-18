/* Read-only L0 oracle: one XXH64 per 4KiB page, little-endian u64 output. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc != 3 && argc != 5) { fprintf(stderr, "usage: page_hash BACKING HASHES [LINE_STATE LINE_COUNTS]\n"); return 2; }
    void *lib = dlopen("libxxhash.so.0", RTLD_NOW | RTLD_LOCAL);
    uint64_t (*hash)(const void *, size_t, uint64_t) = lib ? dlsym(lib, "XXH64") : NULL;
    if (!hash || hash("", 0, 0) != UINT64_C(0xef46db3751d8e999)) {
        fprintf(stderr, "XXH64 unavailable or self-test failed\n"); return 1;
    }
    int fd = open(argv[1], O_RDONLY | O_CLOEXEC);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || st.st_size <= 0 || st.st_size % 4096) { perror("backing"); return 1; }
    const unsigned char *mem = mmap(NULL, st.st_size, PROT_READ, MAP_SHARED, fd, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 1; }
    FILE *out = fopen(argv[2], "wbx");
    if (!out) { perror("output"); return 1; }
    /* Optional persistent line hashes. Only changed pages need 64 line hashes
     * after the baseline. This is lab state, deleted by the oracle at shutdown. */
    size_t pages = st.st_size / 4096;
    uint64_t *state = NULL;
    size_t state_bytes = pages * 65 * sizeof(uint64_t);
    FILE *counts = NULL;
    int state_fd = -1, baseline = 0;
    if (argc == 5) {
        state_fd = open(argv[3], O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (state_fd >= 0) { baseline = 1; if (ftruncate(state_fd, state_bytes)) { perror("line state size"); return 1; } }
        else if (errno == EEXIST) state_fd = open(argv[3], O_RDWR | O_CLOEXEC);
        struct stat saved;
        if (state_fd < 0 || fstat(state_fd,&saved) || saved.st_size != (off_t)state_bytes) { perror("line state"); return 1; }
        state = mmap(NULL,state_bytes,PROT_READ|PROT_WRITE,MAP_SHARED,state_fd,0);
        if (state == MAP_FAILED) { perror("line state mmap"); return 1; }
        counts = fopen(argv[4],"wbx");
        if (!counts) { perror("line counts"); return 1; }
    }
    uint64_t buffer[4096];
    unsigned char line_counts[4096];
    for (size_t page = 0; page < pages;) {
        size_t n = pages - page < 4096 ? pages - page : 4096;
        for (size_t i = 0; i < n; ++i) {
            const unsigned char *address = mem + (page + i) * 4096;
            buffer[i] = hash(address,4096,0);
            line_counts[i] = 0;
            if (!state) continue;
            uint64_t *previous = state + (page+i)*65;
            if (baseline || previous[0] != buffer[i]) {
                for (size_t line=0;line<64;line++) {
                    uint64_t value=hash(address+line*64,64,0);
                    if (!baseline && previous[line+1] != value) line_counts[i]++;
                    previous[line+1]=value;
                }
                previous[0]=buffer[i];
            }
        }
        if (counts && fwrite(line_counts,1,n,counts)!=n) { perror("line counts write"); return 1; }
        if (fwrite(buffer, sizeof(uint64_t), n, out) != n) { perror("write"); return 1; }
        page += n;
    }
    int error = fclose(out);
    if (counts) { error |= fclose(counts); munmap(state,state_bytes); close(state_fd); }
    munmap((void *)mem, st.st_size);
    close(fd);
    dlclose(lib);
    return error ? 1 : 0;
}
