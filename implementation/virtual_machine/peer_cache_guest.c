/* SPDX-License-Identifier: GPL-2.0 */
/* Functional probe: pins survive until process/VM teardown, no reclamation. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <errno.h>
#include <endian.h>
#include <inttypes.h>
#include <linux/vm_sockets.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "guest-pool-uapi.h"
static uint64_t now_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &t)) exit(2);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
/* Versioned little-endian wire: request {magic,id,file_page,disk_offset};
 * response {magic,id,status(0 absent/1 found/2 error),gpa,disk_offset,ioctl_ns}.
 * Every connection is VM-local. Guest inode/folio metadata never leaves it. */
#define LOOKUP_MAGIC UINT64_C(0x5043434800000001)
struct lookup_server { int listener, file, device; };
static pthread_mutex_t lookup_lock = PTHREAD_MUTEX_INITIALIZER;
static int lookup(int device, struct guest_pool_lookup *q, uint64_t *elapsed) {
    pthread_mutex_lock(&lookup_lock);
    uint64_t started = now_ns();
    int ret = ioctl(device, GUEST_POOL_LOOKUP, q);
    *elapsed = now_ns() - started;
    pthread_mutex_unlock(&lookup_lock);
    return ret;
}
static int transfer(int fd, void *buffer, size_t length, int sending) {
    unsigned char *p = buffer;
    while (length) {
        ssize_t n = sending ? send(fd, p, length, MSG_NOSIGNAL) : recv(fd, p, length, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n; length -= n;
    }
    return 0;
}
static void *serve_lookup(void *opaque) {
    struct lookup_server *s = opaque;
    for (;;) {
        int client = accept4(s->listener, NULL, NULL, SOCK_CLOEXEC);
        if (client < 0) { if (errno == EINTR) continue; perror("accept vsock"); exit(2); }
        uint64_t wire[4];
        while (!transfer(client, wire, sizeof(wire), 0)) {
            for (size_t i=0;i<4;i++) wire[i]=le64toh(wire[i]);
            if (wire[0] != LOOKUP_MAGIC) break;
            struct guest_pool_lookup q = {.fd=s->file, .file_page=wire[2], .disk_offset=wire[3]};
            uint64_t ns;
            int ret = lookup(s->device, &q, &ns);
            uint64_t answer[6] = {LOOKUP_MAGIC, wire[1], ret ? 2 : q.found,
                                  q.gpa, q.disk_offset, ns};
            for (size_t i=0;i<6;i++) answer[i]=htole64(answer[i]);
            if (transfer(client, answer, sizeof(answer), 1)) break;
        }
        close(client);
    }
    return NULL;
}
static int resident(int fd, uint64_t page) {
    unsigned char bit = 0;
    void *p = mmap(NULL, 4096, PROT_NONE, MAP_PRIVATE, fd, page * 4096);
    if (p == MAP_FAILED || mincore(p, 4096, &bit)) exit(2);
    munmap(p, 4096);
    return bit & 1;
}
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int fd = open(argv[1], O_RDONLY), device = open("/dev/guest-pool", O_RDWR);
    if (fd < 0 || device < 0) { perror("open"); return 2; }
    struct lookup_server server = {.file=fd, .device=device};
    server.listener = socket(AF_VSOCK, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_vm address = {.svm_family=AF_VSOCK, .svm_cid=VMADDR_CID_ANY, .svm_port=19000};
    if (server.listener < 0 || bind(server.listener, (struct sockaddr *)&address, sizeof(address)) ||
        listen(server.listener, 1)) { perror("listen vsock"); return 2; }
    pthread_t worker;
    if (pthread_create(&worker, NULL, serve_lookup, &server)) return 2;
    posix_fadvise(fd, 0, 0, POSIX_FADV_RANDOM);
    setvbuf(stdout, NULL, _IONBF, 0);
    puts("{\"event\":\"peer_ready\",\"ok\":true}");
    char line[256], op[32];
    unsigned long long id, page, disk;
    while (fgets(line, sizeof(line), stdin)) {
        disk = 0;
        int n = sscanf(line, "%31s %llu %llu %llu", op, &id, &page, &disk);
        if (n < 3) continue;
        if (!strcmp(op, "lookup") && n == 4) {
            struct guest_pool_lookup q = {.fd=fd, .file_page=page, .disk_offset=disk};
            uint64_t elapsed;
            int ret = lookup(device, &q, &elapsed);
            printf("{\"event\":\"peer_lookup\",\"id\":%llu,\"ok\":%s,\"found\":%u,\"gpa\":%llu,\"disk_offset\":%llu,\"lookup_ns\":%" PRIu64 "}\n",
                   id, ret ? "false" : "true", q.found, q.gpa, q.disk_offset, elapsed);
        } else if (!strcmp(op, "read") || !strcmp(op, "warm")) {
            unsigned char data[4096];
            /* Prefault the destination; exclude diagnostics and serial output. */
            memset(data, 0, sizeof(data));
            __asm__ __volatile__("" : : "r"(data) : "memory");
            uint64_t hash = UINT64_C(14695981039346656037);
            int before = resident(fd, page);
            uint64_t started = now_ns();
            ssize_t bytes = pread(fd, data, sizeof(data), page * 4096);
            uint64_t elapsed = now_ns() - started;
            if (bytes == sizeof(data)) for (size_t i=0;i<sizeof(data);i++) hash=(hash^data[i])*UINT64_C(1099511628211);
            printf("{\"event\":\"peer_read\",\"id\":%llu,\"ok\":%s,\"page\":%llu,\"before\":%d,\"after\":%d,\"hash\":\"%016" PRIx64 "\",\"read_ns\":%" PRIu64 "}\n",
                   id, bytes == sizeof(data) ? "true" : "false", page, before, resident(fd,page), hash, elapsed);
        } else if (!strcmp(op, "cache")) {
            printf("{\"event\":\"peer_cache\",\"id\":%llu,\"ok\":true,\"resident\":%d}\n",id,resident(fd,page));
        }
    }
    /* The harness fences both VMs before this owner may exit. */
    for (;;) pause();
}
