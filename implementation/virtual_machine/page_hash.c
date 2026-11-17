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
    if (argc != 3) { fprintf(stderr, "usage: page_hash BACKING HASHES\n"); return 2; }
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
    uint64_t buffer[4096];
    size_t pages = st.st_size / 4096;
    for (size_t page = 0; page < pages;) {
        size_t n = pages - page < 4096 ? pages - page : 4096;
        for (size_t i = 0; i < n; ++i) buffer[i] = hash(mem + (page + i) * 4096, 4096, 0);
        if (fwrite(buffer, sizeof(uint64_t), n, out) != n) { perror("write"); return 1; }
        page += n;
    }
    int error = fclose(out);
    munmap((void *)mem, st.st_size);
    close(fd);
    dlclose(lib);
    return error ? 1 : 0;
}
