// SPDX-License-Identifier: Apache-2.0
// Start one owned, pre-boot VMM and prepare its immutable disk backend locally.
// The parent reports native timings separately from the outer lab console.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void die(const char *message) { perror(message); exit(1); }
static void need(int ok, const char *message) {
    if (!ok) { fprintf(stderr, "resident-bootstrap: %s\n", message); exit(1); }
}
static uint64_t now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) die("clock");
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static unsigned long long starttime(pid_t pid) {
    char name[64], line[4096], *save = NULL;
    snprintf(name, sizeof(name), "/proc/%d/stat", pid);
    FILE *f = fopen(name, "r");
    if (!f || !fgets(line, sizeof(line), f)) die("process stat");
    fclose(f);
    char *end = strrchr(line, ')');
    need(end != NULL, "process stat format");
    char *field = strtok_r(end + 1, " ", &save);
    for (int i = 3; i < 22 && field; ++i) field = strtok_r(NULL, " ", &save);
    need(field != NULL, "process starttime");
    return strtoull(field, NULL, 10);
}
static void write_all(int fd, const char *data, size_t size) {
    while (size) {
        ssize_t n = write(fd, data, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) die("write");
        data += n; size -= n;
    }
}
static void file(const char *name, const char *data) {
    int fd = open(name, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) die("create owned file");
    write_all(fd, data, strlen(data));
    if (close(fd)) die("close owned file");
}
int main(int argc, char **argv) {
    need(argc == 3 || (argc == 4 && !strcmp(argv[3], "--no-prepare")),
         "usage: resident-bootstrap DIRECTORY LAUNCH_SCRIPT [--no-prepare]");
    int prepare_disk = argc == 3;
    need(!strncmp(argv[1], "/run/swe-pool-", 14) && strlen(argv[1]) < 90,
         "owned bounded directory required");
    need(!strncmp(argv[2], "/run/swe-pool-", 14), "owned launch script required");
    uint64_t begin = now();
    if (mkdir(argv[1], 0700)) die("fresh directory");
    char input[128], log[128], error[128], socket_name[108], pidfile[128];
    snprintf(input, sizeof(input), "%s/input", argv[1]);
    snprintf(log, sizeof(log), "%s/fc.log", argv[1]);
    snprintf(error, sizeof(error), "%s/stderr.log", argv[1]);
    snprintf(socket_name, sizeof(socket_name), "%s/api.sock", argv[1]);
    snprintf(pidfile, sizeof(pidfile), "%s/fc.pid", argv[1]);
    if (mkfifo(input, 0600)) die("input FIFO");
    uint64_t fork_start = now();
    pid_t pid = fork();
    if (pid < 0) die("fork");
    if (!pid) {
        int in = open(input, O_RDWR), out = open(log, O_WRONLY | O_CREAT | O_EXCL, 0600);
        int err = open(error, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (in < 0 || out < 0 || err < 0) _exit(121);
        if (dup2(in, 0) < 0 || dup2(out, 1) < 0 || dup2(err, 2) < 0) _exit(122);
        close(in); close(out); close(err);
        char *const args[] = {"/bin/sh", argv[2], NULL};
        char *const env[] = {"PATH=/bin:/sbin:/usr/bin:/usr/sbin:/lab", NULL};
        execve(args[0], args, env);
        _exit(123);
    }
    uint64_t fork_end = now();
    unsigned long long identity = starttime(pid);
    char record[80];
    snprintf(record, sizeof(record), "%d %llu\n", pid, identity);
    file(pidfile, record);
    int fd = -1;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, socket_name);
    for (;;) {
        fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) die("API socket");
        if (!connect(fd, (void *)&address, sizeof(address))) break;
        int error_number = errno;
        close(fd);
        need(error_number == ENOENT || error_number == ECONNREFUSED, "API readiness connect");
        int status;
        need(waitpid(pid, &status, WNOHANG) == 0, "VMM exited during startup");
        if (now() - fork_start > 10000000000ull) {
            kill(pid, SIGKILL); waitpid(pid, NULL, 0);
            need(0, "VMM startup timeout");
        }
        struct timespec delay = {.tv_nsec = 100000};
        nanosleep(&delay, NULL);
    }
    uint64_t ready = now();
    need(starttime(pid) == identity, "VMM PID changed");
    int owner = open("/sys/module/kvm/parameters/dax_owner_pid", O_WRONLY | O_CLOEXEC);
    if (owner < 0) die("owner parameter");
    snprintf(record, sizeof(record), "%d\n", pid);
    write_all(owner, record, strlen(record)); close(owner);
    if (!prepare_disk) {
        close(fd);
        printf("RESIDENT_BOOT_JSON {\"pid\":%d,\"starttime\":%llu,\"fork_ns\":%"PRIu64
               ",\"fork_to_api_ready_ns\":%"PRIu64",\"prepare_api_ns\":0,\"disk_prepared\":false"
               ",\"native_ready_ns\":%"PRIu64"}\n", pid, identity,
               fork_end - fork_start, ready - fork_start, now() - begin);
        return 0;
    }
    uint64_t prepare_start = now();
    struct timeval timeout = {.tv_sec = 30};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) die("API timeout");
    const char *body = "{\"action_type\": \"DirectPrepare\"}";
    char request[512], response[2048] = {0};
    int size = snprintf(request, sizeof(request), "PUT /actions HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Type: application/json\r\nContent-Length: %zu\r\n\r\n%s", strlen(body), body);
    need(size > 0 && (size_t)size < sizeof(request), "API request bounds");
    write_all(fd, request, size);
    size_t used = 0;
    while (!strstr(response, "\r\n\r\n")) {
        need(used < sizeof(response) - 1, "API response bound");
        ssize_t n = read(fd, response + used, sizeof(response) - 1 - used);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) die("API response");
        used += n; response[used] = 0;
    }
    close(fd);
    uint64_t prepared = now();
    int status = 0;
    need(sscanf(response, "HTTP/1.1 %d", &status) == 1 && status == 204, "prepare API failed");
    printf("PREPARE_API PUT /actions %s\n", body);
    printf("LAB_API_JSON {\"status\":204,\"expected_status\":204,\"duration_us\":%.3f}\n",
           (prepared - prepare_start) / 1000.);
    printf("RESIDENT_BOOT_JSON {\"pid\":%d,\"starttime\":%llu,\"fork_ns\":%"PRIu64
           ",\"fork_to_api_ready_ns\":%"PRIu64",\"prepare_api_ns\":%"PRIu64
           ",\"native_ready_ns\":%"PRIu64"}\n", pid, identity,
           fork_end - fork_start, ready - fork_start, prepared - prepare_start, prepared - begin);
    return 0;
}
