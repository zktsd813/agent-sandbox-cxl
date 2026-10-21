#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ethtool.h>
#include <linux/kvm.h>
#include <linux/sockios.h>
#include "../linux/include/uapi/linux/kvm_dax.h"
#include <linux/futex.h>
#include <net/if.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/io.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* Each writer owns a separate page; sequence publication uses release/acquire. */
#define SLOT_SIZE 4096
#define PAYLOAD_SIZE 3072
struct slot {
    _Atomic uint32_t sequence;
    uint32_t checksum;
    uint64_t nonce;
    unsigned char payload[PAYLOAD_SIZE];
};
_Static_assert(sizeof(struct slot) <= SLOT_SIZE, "slot exceeds its page");
_Static_assert(offsetof(struct slot, payload) == 16, "unexpected wire layout");

static void die(const char *message)
{
    perror(message);
    exit(1);
}

static int env_enabled(const char *name)
{
    const char *value = getenv(name);
    return value && !strcmp(value, "1");
}

static double monotonic(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) die("clock_gettime");
    return t.tv_sec + t.tv_nsec / 1e9;
}

static uint64_t monotonic_ns(void)
{
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t))die("clock_gettime monotonic ns");
    return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;
}

static unsigned char pattern(uint64_t nonce, uint32_t round, unsigned role, size_t i)
{
    return (unsigned char)((nonce >> ((i % 8) * 8)) ^ (round * 17u) ^
                           (i * 31u) ^ (role * 93u));
}

static uint32_t checksum(const unsigned char *data, size_t size)
{
    uint32_t hash = 2166136261u;
    for (size_t i = 0; i < size; i++) hash = (hash ^ data[i]) * 16777619u;
    return hash;
}

static int read_hex(const char *path, unsigned *value)
{
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    int rc = fscanf(f, "%x", value) == 1 ? 0 : -1;
    fclose(f);
    return rc;
}

static void find_ivshmem(char *resource, size_t size, char *bdf, size_t bdf_size)
{
    DIR *dir = opendir("/sys/bus/pci/devices");
    if (!dir) die("PCI sysfs");
    struct dirent *entry;
    unsigned matches = 0;
    while ((entry = readdir(dir))) {
        if (entry->d_name[0] == '.') continue;
        char path[512];
        unsigned vendor, device;
        snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/vendor", entry->d_name);
        if (read_hex(path, &vendor)) continue;
        snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/device", entry->d_name);
        if (read_hex(path, &device)) continue;
        if (vendor != 0x1af4 || device != 0x1110) continue;
        snprintf(resource, size, "/sys/bus/pci/devices/%s/resource2", entry->d_name);
        snprintf(bdf, bdf_size, "%s", entry->d_name);
        matches++;
    }
    closedir(dir);
    if (matches != 1) {
        fprintf(stderr, "Expected one ivshmem device, found %u\n", matches);
        exit(1);
    }
}

static void wait_sequence(struct slot *slot, uint32_t expected, double deadline)
{
    while (atomic_load_explicit(&slot->sequence, memory_order_acquire) != expected) {
        if (monotonic() >= deadline) {
            fprintf(stderr, "Shared-memory timeout at round %u\n", expected);
            exit(1);
        }
        struct timespec pause = {.tv_nsec = 100000};
        nanosleep(&pause, NULL);
    }
}

static void publish(struct slot *slot, uint64_t nonce, uint32_t round, unsigned role)
{
    slot->nonce = nonce;
    for (size_t i = 0; i < PAYLOAD_SIZE; i++) slot->payload[i] = pattern(nonce, round, role, i);
    slot->checksum = checksum(slot->payload, PAYLOAD_SIZE);
    atomic_store_explicit(&slot->sequence, round, memory_order_release);
}

static void verify_slot(struct slot *slot, uint64_t nonce, uint32_t round, unsigned role)
{
    if (slot->nonce != nonce || slot->checksum != checksum(slot->payload, PAYLOAD_SIZE)) {
        fprintf(stderr, "Shared-memory nonce/checksum mismatch at round %u\n", round);
        exit(1);
    }
    for (size_t i = 0; i < PAYLOAD_SIZE; i++) {
        if (slot->payload[i] != pattern(nonce, round, role, i)) {
            fprintf(stderr, "Shared-memory data mismatch at round %u byte %zu\n", round, i);
            exit(1);
        }
    }
}

static uint64_t dax_attribute(const char *path, const char *attribute)
{
    const char *name = strrchr(path, '/');
    if (!name || strncmp(path, "/dev/dax", 8) || !name[4]) {
        fprintf(stderr, "Expected /dev/daxX.Y\n");
        exit(1);
    }
    for (const char *p = name + 4; *p; p++) {
        if ((*p < '0' || *p > '9') && *p != '.') {
            fprintf(stderr, "Invalid device-DAX name\n");
            exit(1);
        }
    }
    char sysfs[512];
    snprintf(sysfs, sizeof(sysfs), "/sys/bus/dax/devices/%s/%s", name + 1, attribute);
    FILE *f = fopen(sysfs, "r");
    if (!f) die("open DAX sysfs attribute");
    uint64_t value;
    if (fscanf(f, "%" SCNu64, &value) != 1 || value == 0) {
        fprintf(stderr, "Invalid DAX attribute: %s\n", sysfs);
        exit(1);
    }
    fclose(f);
    return value;
}

static int open_dax(const char *path)
{
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) die("open device-DAX");
    struct stat st;
    if (fstat(fd, &st)) die("fstat device-DAX");
    if (!S_ISCHR(st.st_mode)) {
        fprintf(stderr, "DAX input must be a character device\n");
        exit(1);
    }
    return fd;
}

static void exchange(char role, uint32_t rounds, uint64_t nonce, const char *dax)
{
    char path[512], bdf[256] = "";
    uint64_t align = SLOT_SIZE, size = 0;
    int fd;
    if (dax) {
        align = dax_attribute(dax, "align");
        size = dax_attribute(dax, "size");
        fd = open_dax(dax);
    } else {
        find_ivshmem(path, sizeof(path), bdf, sizeof(bdf));
        fd = open(path, O_RDWR | O_CLOEXEC);
        if (fd < 0) die("open ivshmem resource2");
    }
    size_t length = ((SLOT_SIZE * 2 + align - 1) / align) * align;
    if (dax && length > size) { fprintf(stderr, "DAX device too small\n"); exit(1); }
    unsigned char *mapping = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED) die("mmap shared device");
    if (dax && (uintptr_t)mapping % align) { fprintf(stderr, "Unaligned DAX mapping\n"); exit(1); }
    struct slot *a = (struct slot *)mapping;
    struct slot *b = (struct slot *)(mapping + SLOT_SIZE);
    double start = monotonic(), deadline = start + 90;
    for (uint32_t round = 1; round <= rounds; round++) {
        if (role == 'A') {
            publish(a, nonce, round, 0);
            wait_sequence(b, round, deadline);
            verify_slot(b, nonce, round, 1);
        } else {
            wait_sequence(a, round, deadline);
            verify_slot(a, nonce, round, 0);
            publish(b, nonce, round, 1);
        }
    }
    printf("LAB_JSON {\"event\":\"%s\",\"role\":\"%c\","
           "\"result\":\"passed\",\"rounds\":%u,\"payload_bytes\":%u,"
           "\"verified_bytes\":%" PRIu64 ",\"nonce\":\"%016" PRIx64 "\","
           "\"pci_bdf\":\"%s\",\"device\":\"%s\",\"align\":%" PRIu64 ","
           "\"device_bytes\":%" PRIu64 ",\"elapsed_s\":%.6f}\n",
           dax ? "dax_shared_memory" : "shared_memory", role, rounds, PAYLOAD_SIZE,
           (uint64_t)rounds * PAYLOAD_SIZE, nonce, bdf, dax ? dax : "", align, size,
           monotonic() - start);
    munmap(mapping, length);
    close(fd);
}

static void dax_word(const char *path, uint64_t offset, const char *value)
{
    uint64_t align = dax_attribute(path, "align"), size = dax_attribute(path, "size");
    if (offset % sizeof(uint32_t) || size < sizeof(uint32_t) || offset > size - sizeof(uint32_t)) {
        fprintf(stderr, "DAX word offset outside device or unaligned\n");
        exit(1);
    }
    uint64_t start = offset / align * align;
    int fd = open_dax(path);
    unsigned char *mapping = mmap(NULL, align, PROT_READ | PROT_WRITE, MAP_SHARED, fd, start);
    if (mapping == MAP_FAILED) die("mmap DAX word");
    volatile uint32_t *word = (volatile uint32_t *)(mapping + offset - start);
    if (value) {
        char *end;
        uint64_t parsed = strtoull(value, &end, 0);
        if (*end || parsed > UINT32_MAX) { fprintf(stderr, "Invalid DAX word\n"); exit(1); }
        *word = (uint32_t)parsed;
        atomic_thread_fence(memory_order_seq_cst);
    }
    printf("LAB_DAX_WORD=0x%08" PRIx32 "\n", *word);
    munmap(mapping, align);
    close(fd);
}

static void check_kvm(char role)
{
    int fd = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    if (fd < 0) die("open /dev/kvm");
    int api = ioctl(fd, KVM_GET_API_VERSION, 0);
    if (api != 12) { fprintf(stderr, "Unexpected KVM API %d\n", api); exit(1); }
    int vm = ioctl(fd, KVM_CREATE_VM, 0);
    if (vm < 0) die("KVM_CREATE_VM");
    int vcpu = ioctl(vm, KVM_CREATE_VCPU, 0);
    if (vcpu < 0) die("KVM_CREATE_VCPU");
    close(vcpu);
    close(vm);
    close(fd);
    printf("LAB_JSON {\"event\":\"l1_kvm\",\"role\":\"%c\","
           "\"result\":\"passed\",\"api\":%d,\"create_vcpu\":true}\n", role, api);
}

static void check_kvm_api(char role)
{
    int fd=open("/dev/kvm",O_RDWR|O_CLOEXEC);
    if(fd<0 || ioctl(fd,KVM_GET_API_VERSION,0)!=12)die("KVM API-only check");
    close(fd);
    printf("LAB_JSON {\"event\":\"l1_kvm\",\"role\":\"%c\",\"result\":\"passed\",\"api\":12,\"create_vcpu\":false,\"check\":\"API only; no VM created\"}\n",role);
}

static void check_memory(char role)
{
    const size_t size = 16 * 1024 * 1024;
    volatile unsigned char *data = malloc(size);
    if (!data) die("malloc memory test");
    for (unsigned pass = 0; pass < 3; pass++) {
        for (size_t i = 0; i < size; i++) data[i] = (unsigned char)(i * 13u + pass * 57u);
        for (size_t i = 0; i < size; i++) {
            if (data[i] != (unsigned char)(i * 13u + pass * 57u)) {
                fprintf(stderr, "L2 memory mismatch at %zu\n", i);
                exit(1);
            }
        }
    }
    printf("LAB_JSON {\"event\":\"l2_memory\",\"role\":\"%c\","
           "\"result\":\"passed\",\"bytes\":%zu,\"passes\":3,\"checksum\":%u}\n",
           role, size, checksum((const unsigned char *)data, size));
    free((void *)data);
}

/* Lab-only checkpoint timing. Control/state use [1MiB,2MiB) of shared DAX;
 * guest RAM begins at 2MiB. Each duration uses one node's monotonic clock. */
#define HB_MAGIC UINT64_C(0x48424d4943524f56)
struct heartbeat {
    _Atomic uint64_t magic, sequence, epoch, acknowledged, verified, errors;
    _Atomic uint64_t clock_ns, clock_errors;
    _Atomic uint64_t tsc, tsc_errors;
    _Atomic uint64_t ack_tsc, ack_epoch;
    /* Appended lab workload diagnostics; legacy heartbeat offsets stay fixed. */
    _Atomic uint64_t working_set_bytes, available_bytes, instance_id;
    _Atomic uint64_t first_sweep_ns, warm_sweep_ns, warm_done;
};
_Static_assert(sizeof(struct heartbeat) <= 4096, "heartbeat page");
struct handoff_control {
    _Atomic uint64_t nonce, ready[2], preparing, published, acknowledged;
    uint64_t schema, state_round, state_size, state_hash, memory_bytes, sequence;
    uint64_t guest_clock_ns, guest_tsc;
    unsigned char state[256 * 1024];
    _Atomic uint64_t scan_ready, warm_ready;
    uint64_t working_set_bytes, instance_id;
};
#define HANDOFF_SCHEMA UINT64_C(0x534e415053484f02)
_Static_assert(sizeof(struct handoff_control) <= 1024 * 1024, "snapshot mailbox exceeds reservation");
static volatile sig_atomic_t bench_child;
static const char *bench_socket = "/run/bench-fc.sock";
static char bench_directory[128] = "/run";
static char bench_socket_storage[108];
static char bench_config_path[160] = "/run/bench-config.json";
static char bench_checkpoint_path[160] = "/run/bench.state";
static char bench_full_state_path[160] = "/run/bench-full.state";
static char bench_full_memory_path[160] = "/run/bench-full.mem";
static char bench_pid_path[160] = "/run/bench-fc.pid";
static _Atomic uint64_t *bench_session;
static uint64_t bench_session_nonce;
static int bench_ack_trace_fd = -1;
static int bench_ack_trace_gate_fd = -1;
struct bench_ack_anchor {
    uint64_t clock_before_ns, clock_after_ns, tsc;
};

static void bench_ack_trace_gate(int enabled)
{
    if (bench_ack_trace_gate_fd < 0) return;
    const char value = enabled ? '1' : '0';
    if (write(bench_ack_trace_gate_fd, &value, 1) != 1) die("snapshot ACK tracing_on");
}

static void bench_ack_trace_cleanup(void)
{
    if (bench_ack_trace_gate_fd >= 0) {
        /* Best effort on a failed experiment; never recurse through die/exit. */
        ssize_t ignored = write(bench_ack_trace_gate_fd, "0", 1);
        (void)ignored;
        close(bench_ack_trace_gate_fd);
        bench_ack_trace_gate_fd = -1;
    }
    if (bench_ack_trace_fd >= 0) { close(bench_ack_trace_fd); bench_ack_trace_fd = -1; }
}

/* Opt-in only: use the L1 monotonic clock also selected by the trace collector.
 * The FD is opened before measured work and never inherited by Firecracker. */
static uint64_t bench_ack_marker(char role, unsigned round, uint64_t nonce,
                                 const char *phase, struct bench_ack_anchor *anchor)
{
    if (bench_ack_trace_fd < 0) return 0;
    uint64_t ns;
    if (anchor) {
        unsigned low, high;
        anchor->clock_before_ns = monotonic_ns();
        __asm__ __volatile__("lfence; rdtsc; lfence" : "=a"(low), "=d"(high) :: "memory");
        anchor->clock_after_ns = monotonic_ns();
        anchor->tsc = ((uint64_t)high << 32) | low;
        ns = anchor->clock_before_ns + (anchor->clock_after_ns - anchor->clock_before_ns) / 2;
    } else ns = monotonic_ns();
    char marker[256];
    int size = snprintf(marker, sizeof(marker),
        "SNAP_ACK nonce=%016" PRIx64 " role=%c round=%u phase=%s monotonic_ns=%" PRIu64 "\n",
        nonce, role, round, phase, ns);
    if (size < 0 || (size_t)size >= sizeof(marker)) {
        fprintf(stderr, "Snapshot ACK trace marker exceeds buffer\n"); exit(1);
    }
    if (write(bench_ack_trace_fd, marker, (size_t)size) != size)
        die("snapshot ACK trace marker");
    return ns;
}

static void bench_check_session(void)
{
    if (bench_session && atomic_load_explicit(bench_session, memory_order_acquire) != bench_session_nonce) {
        fprintf(stderr, "Snapshot session changed while controller was active\n"); exit(1);
    }
}

static uint64_t bench_process_start(pid_t pid)
{
    char path[64], line[4096];
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    FILE *stat = fopen(path, "r");
    if (!stat || !fgets(line, sizeof(line), stat)) die("benchmark process identity");
    fclose(stat);
    char *fields = strrchr(line, ')'), *save = NULL;
    if (!fields) { fprintf(stderr, "Malformed process stat\n"); exit(1); }
    char *field = strtok_r(fields + 1, " ", &save);
    for (unsigned number = 3; number < 22 && field; number++) field = strtok_r(NULL, " ", &save);
    if (!field) { fprintf(stderr, "Missing process starttime\n"); exit(1); }
    return strtoull(field, NULL, 10);
}

static void bench_pid_record(const char *path, pid_t pid)
{
    char temporary[192];
    snprintf(temporary, sizeof(temporary), "%s.tmp-%d", path, getpid());
    FILE *record = fopen(temporary, "w");
    if (!record) die("benchmark PID record");
    fprintf(record, "%d %" PRIu64 "\n", pid, bench_process_start(pid));
    if (fclose(record) || rename(temporary, path)) die("publish benchmark PID record");
}

/* Only semantic-snapshot runs opt in; direct handoff keeps its old paths. */
static void bench_transaction_paths(void)
{
    const char *directory = getenv("BENCH_RUN_DIR");
    if (!directory) return;
    if (strncmp(directory, "/run/snapshot-bench-", 20) || strlen(directory) != 36 ||
        strspn(directory + 20, "0123456789abcdef") != 16) {
        fprintf(stderr, "Invalid snapshot transaction directory\n"); exit(1);
    }
    snprintf(bench_directory, sizeof(bench_directory), "%s", directory);
    snprintf(bench_socket_storage, sizeof(bench_socket_storage), "%s/bench-fc.sock", directory);
    bench_socket = bench_socket_storage;
    snprintf(bench_config_path, sizeof(bench_config_path), "%s/bench-config.json", directory);
    snprintf(bench_checkpoint_path, sizeof(bench_checkpoint_path), "%s/bench.state", directory);
    snprintf(bench_full_state_path, sizeof(bench_full_state_path), "%s/bench-full.state", directory);
    snprintf(bench_full_memory_path, sizeof(bench_full_memory_path), "%s/bench-full.mem", directory);
    snprintf(bench_pid_path, sizeof(bench_pid_path), "%s/bench-fc.pid", directory);
    char controller[160];
    snprintf(controller, sizeof(controller), "%s/controller.pid", directory);
    bench_pid_record(controller, getpid());
}

static void bench_terminate(int signal)
{
    if (bench_ack_trace_gate_fd >= 0) {
        ssize_t ignored = write(bench_ack_trace_gate_fd, "0", 1);
        (void)ignored;
    }
    if (bench_child > 0) kill(bench_child, SIGKILL);
    unlink(bench_socket);
    _exit(128 + signal);
}

static void bench_cleanup(void)
{
    if (bench_child > 0) {
        kill(bench_child, SIGKILL);
        while (waitpid(bench_child, NULL, 0) < 0 && errno == EINTR) {}
        bench_child = 0;
        unlink(bench_socket);
    }
}

static unsigned handoff_poll_us=50;
static unsigned handoff_spin_us;
static uint64_t context_now(void);
static void handoff_poll(void)
{
    if(handoff_poll_us)usleep(handoff_poll_us);
    else __asm__ __volatile__("pause");
}

static void wait_value(_Atomic uint64_t *value, uint64_t expected, const char *what)
{
    bench_check_session();
    double until = monotonic() + 60;
    uint64_t spin_until = context_now() + (uint64_t)handoff_spin_us * 1000;
    while (atomic_load_explicit(value, memory_order_acquire) != expected) {
        bench_check_session();
        if (monotonic() > until) { fprintf(stderr, "Timed out: %s\n", what); exit(1); }
        if (handoff_spin_us && context_now() < spin_until) __asm__ __volatile__("pause");
        else handoff_poll();
    }
    bench_check_session();
}

static void snapshot_wait_published(struct handoff_control *ctrl, uint64_t expected)
{
    double until = monotonic() + 60;
    uint64_t spin_until = 0;
    int armed = 0;
    while (atomic_load_explicit(&ctrl->published, memory_order_acquire) != expected) {
        bench_check_session();
        if (monotonic() > until) { fprintf(stderr, "Timed out: source checkpoint\n"); exit(1); }
        if (!armed && atomic_load_explicit(&ctrl->preparing, memory_order_acquire) == expected) {
            armed = 1;
            spin_until = context_now() + (uint64_t)handoff_spin_us * 1000;
        }
        if (armed && handoff_spin_us && context_now() < spin_until) __asm__ __volatile__("pause");
        else handoff_poll();
    }
    bench_check_session();
}

static int bench_api_expected_status = 204;
static int bench_api_inspect;

static void bench_print_json_string(const char *value)
{
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)value; *p; p++) {
        if (*p == '"' || *p == '\\') printf("\\%c", *p);
        else if (*p < 0x20) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

static double bench_api(const char *method, const char *path, const char *body)
{
    bench_check_session();
    double start = monotonic();
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) die("API socket");
    struct timeval timeout = {.tv_sec = 30};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout))) die("API timeout");
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    snprintf(address.sun_path, sizeof(address.sun_path), "%s", bench_socket);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) die("API connect");
    char request[2048], response[16384];
    int length = snprintf(request, sizeof(request),
        "%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        "Content-Length: %zu\r\nConnection: close\r\n\r\n%s", method, path, strlen(body), body);
    if (length < 0 || (size_t)length >= sizeof(request)) { fprintf(stderr, "API request too large\n"); exit(1); }
    size_t sent = 0, received = 0;
    while (sent < (size_t)length) {
        ssize_t n = send(fd, request + sent, length - sent, MSG_NOSIGNAL);
        if (n <= 0) die("API send");
        sent += n;
    }
    /* Firecracker returns no body on 204. Do not depend on Connection: close. */
    while (received < sizeof(response) - 1) {
        ssize_t n = read(fd, response + received, sizeof(response) - 1 - received);
        if (n < 0) die("API response");
        if (!n) break;
        received += n;
        response[received] = 0;
        char *header_end = strstr(response, "\r\n\r\n");
        if (header_end) {
            if (!bench_api_inspect) break;
            char *content_length = strcasestr(response, "\r\nContent-Length:");
            if (!content_length || content_length > header_end) break;
            uint64_t body_length = strtoull(content_length + strlen("\r\nContent-Length:"), NULL, 10);
            if (received >= (size_t)(header_end + 4 - response) + body_length) break;
        }
    }
    close(fd);
    response[received] = 0;
    int status = 0;
    int parsed = sscanf(response, "HTTP/1.1 %d", &status);
    double duration_us = (monotonic() - start) * 1e6;
    if (bench_api_inspect) {
        printf("LAB_API_JSON {\"status\":%d,\"expected_status\":%d,\"duration_us\":%.3f,\"response\":",
               status, bench_api_expected_status, duration_us);
        bench_print_json_string(response);
        printf("}\n");
    }
    if (parsed != 1 || status != bench_api_expected_status) {
        fprintf(stderr, "API %s %s failed: %s\n", method, path, response);
        exit(1);
    }
    return duration_us;
}

static int bench_control_fd=-1;
static double bench_control_service_us;
static double bench_control_parts_us[3];
static double bench_control(int resume)
{
    const char *path=getenv("FC_DAX_CONTROL");
    bench_control_service_us=0;
    memset(bench_control_parts_us,0,sizeof(bench_control_parts_us));
    if(!path)return bench_api("PATCH","/vm",resume?"{\"state\":\"Resumed\"}":"{\"state\":\"Paused\"}");
    double start=monotonic();
    if(bench_control_fd<0){
        bench_control_fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
        if(bench_control_fd<0)die("DAX control socket");
        struct sockaddr_un address={.sun_family=AF_UNIX};
        snprintf(address.sun_path,sizeof(address.sun_path),"%s",path);
        if(connect(bench_control_fd,(void *)&address,sizeof(address)))die("DAX control connect");
        struct timeval timeout={.tv_sec=30};
        if(setsockopt(bench_control_fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout)))die("DAX control timeout");
    }
    unsigned char command=resume?2:1;
    if(send(bench_control_fd,&command,1,MSG_NOSIGNAL)!=1)die("DAX control send");
    uint64_t reply[5];size_t done=0;
    while(done<sizeof(reply)){
        ssize_t n=read(bench_control_fd,(char *)reply+done,sizeof(reply)-done);
        if(n<=0)die("DAX control response");
        done+=n;
    }
    if(reply[0]){errno=reply[0];die("DAX control failed");}
    bench_control_service_us=(double)reply[1]/1000;
    for(unsigned i=0;i<3;i++)bench_control_parts_us[i]=(double)reply[i+2]/1000;
    return (monotonic()-start)*1e6;
}

static int bench_private_ram;

static double bench_start(int boot, unsigned generation)
{
    double start = monotonic();
    int serial_file = env_enabled("BENCH_SERIAL_FILE");
    unlink(bench_socket);
    char log[192];
    snprintf(log, sizeof(log), "%s/bench-fc-%u.log", bench_directory, generation);
    int transactional = getenv("BENCH_RUN_DIR") != NULL;
    sigset_t blocked, previous_mask;
    if (transactional) {
        sigemptyset(&blocked);
        sigaddset(&blocked, SIGTERM);
        sigaddset(&blocked, SIGINT);
        if (sigprocmask(SIG_BLOCK, &blocked, &previous_mask)) die("block snapshot startup signals");
    }
    bench_child = fork();
    if (bench_child < 0) die("fork Firecracker");
    if (!bench_child) {
        if (transactional && sigprocmask(SIG_SETMASK, &previous_mask, NULL)) _exit(126);
        int fd = open(log, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd < 0 || dup2(fd, STDOUT_FILENO) < 0 || dup2(fd, STDERR_FILENO) < 0) _exit(126);
        close(fd);
        if (bench_private_ram) {
            /* This control deliberately exercises normal RAM + new KVM objects. */
            const char *variables[] = {"FC_DAX_RAM", "FC_DAX_OFFSET", "FC_DAX_HEAP",
                "FC_DAX_HEAP_OFFSET", "FC_DAX_HEAP_BYTES", "FC_DAX_SNAPSHOT",
                "FC_DAX_DIRECT", "FC_DAX_CONTROL", "FC_DAX_REUSE", "FC_DAX_REUSE_FD",
                "FC_DAX_FIXED_HVA", "FC_DAX_REUSE_QUIET_IRQFD", "FC_DAX_EXACT_TSC",
                "FC_DAX_SNAPSHOT_COMPAT", "FC_DAX_NO_RESUME_KICK", "FC_DAX_IRQFD_BATCH",
                "FC_DAX_CHANNEL_SPIN_US", "BENCH_REUSE_KVM",
                "BENCH_REUSE_PREPARE", "BENCH_REUSE_PREPARE_ON_DEMAND"};
            for (size_t i = 0; i < sizeof(variables) / sizeof(variables[0]); i++)
                unsetenv(variables[i]);
        } else {
            setenv("FC_DAX_RAM", "/dev/dax0.0", 1);
            setenv("FC_DAX_OFFSET", "2097152", 1);
        }
        int direct = env_enabled("FC_DAX_DIRECT");
        int no_seccomp = direct || env_enabled("BENCH_NO_SECCOMP");
        if (!bench_private_ram) setenv("FC_DAX_SNAPSHOT", direct ? "0" : "1", 1);
        if (!direct) unsetenv("FC_DAX_HEAP");
        if (no_seccomp && boot && !serial_file) execl("/lab/firecracker", "firecracker", "--no-seccomp", "--api-sock", bench_socket,
                        "--config-file", bench_config_path, (char *)NULL);
        if (no_seccomp) execl("/lab/firecracker", "firecracker", "--no-seccomp", "--api-sock", bench_socket, (char *)NULL);
        if (boot && !serial_file) execl("/lab/firecracker", "firecracker", "--api-sock", bench_socket,
                        "--config-file", bench_config_path, (char *)NULL);
        else execl("/lab/firecracker", "firecracker", "--api-sock", bench_socket, (char *)NULL);
        _exit(127);
    }
    if (transactional) bench_pid_record(bench_pid_path, bench_child);
    else {
        FILE *pidfile = fopen(bench_pid_path, "w");
        if (!pidfile) die("PID file");
        fprintf(pidfile, "%d\n", bench_child);
        fclose(pidfile);
    }
    if (transactional && sigprocmask(SIG_SETMASK, &previous_mask, NULL)) die("restore snapshot startup signals");
    double until = start + 30;
    struct stat st;
    while (stat(bench_socket, &st)) {
        if (waitpid(bench_child, NULL, WNOHANG) == bench_child) {
            bench_child = 0;
            fprintf(stderr, "Firecracker startup failed; see %s\n", log); exit(1);
        }
        if (monotonic() > until) {
            fprintf(stderr, "Firecracker startup failed; see %s\n", log); exit(1);
        }
        usleep(50);
    }
    if (serial_file) {
        char request[2048], path[192];
        snprintf(request, sizeof(request), "{\"serial_out_path\":\"%s/bench-serial-%u.log\"}",
                 bench_directory, generation);
        bench_api("PUT", "/serial", request);
        if (boot) {
            const char *names[] = {"machine-config", "boot-source"};
            for (unsigned i = 0; i < 2; i++) {
                snprintf(path, sizeof(path), "%s/bench-%s.json", bench_directory, names[i]);
                FILE *body = fopen(path, "r");
                if (!body) die("serial-file boot configuration");
                size_t size = fread(request, 1, sizeof(request) - 1, body);
                if (!size || ferror(body) || !feof(body)) {
                    fprintf(stderr, "Invalid serial-file boot configuration\n"); exit(1);
                }
                fclose(body);
                request[size] = 0;
                snprintf(path, sizeof(path), "/%s", names[i]);
                bench_api("PUT", path, request);
            }
            bench_api("PUT", "/actions", "{\"action_type\":\"InstanceStart\"}");
        }
    }
    return (monotonic() - start) * 1e6;
}

static size_t read_checkpoint(unsigned char *data, size_t capacity)
{
    int fd = open(bench_checkpoint_path, O_RDONLY | O_CLOEXEC);
    struct stat st;
    if (fd < 0 || fstat(fd, &st)) die("checkpoint open");
    if (st.st_size <= 0 || (uint64_t)st.st_size > capacity) { fprintf(stderr, "Checkpoint exceeds mailbox\n"); exit(1); }
    size_t length = st.st_size, done = 0;
    while (done < length) {
        ssize_t n = read(fd, data + done, length - done);
        if (n <= 0) die("checkpoint read");
        done += n;
    }
    close(fd);
    return length;
}

static void write_checkpoint(const unsigned char *data, size_t length)
{
    int fd = open(bench_checkpoint_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) die("checkpoint create");
    size_t done = 0;
    while (done < length) {
        ssize_t n = write(fd, data + done, length - done);
        if (n <= 0) die("checkpoint write");
        done += n;
    }
    close(fd);
}

static uint64_t checkpoint_hash(const unsigned char *data, size_t length)
{
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < length; i++) hash = (hash ^ data[i]) * UINT64_C(1099511628211);
    return hash;
}

/* Observe the RAM VMA in the actual Firecracker process, not this controller. */
static uint64_t bench_ram_hva(unsigned memory_mib)
{
    char name[80], line[1024];
    snprintf(name, sizeof(name), "/proc/%d/maps", bench_child);
    FILE *maps = fopen(name, "r");
    if (!maps) die("Firecracker maps");
    uint64_t found = 0;
    while (fgets(line, sizeof(line), maps)) {
        unsigned long long first, last, offset;
        char permissions[5];
        if (sscanf(line, "%llx-%llx %4s %llx", &first, &last, permissions, &offset) == 4 &&
            !strcmp(permissions, "rw-s") && offset == 2 * UINT64_C(1024) * 1024 &&
            last - first == memory_mib * UINT64_C(1024) * 1024 && strstr(line, "/dev/dax0.0")) {
            if (found) { fprintf(stderr, "Ambiguous Firecracker DAX RAM VMA\n"); exit(1); }
            found = first;
        }
    }
    fclose(maps);
    if (!found) { fprintf(stderr, "Firecracker shared DAX RAM VMA missing\n"); exit(1); }
    const char *fixed = getenv("FC_DAX_FIXED_HVA");
    if (fixed && found != strtoull(fixed, NULL, 0)) {
        fprintf(stderr, "Firecracker fixed RAM HVA mismatch\n"); exit(1);
    }
    return found;
}

/* Outside the timed path: prove the worker which ran a previous guest keeps
 * its process, KVM file descriptors and vCPU thread across semantic restores. */
static void bench_native_identity(char role, const char *phase, unsigned round)
{
    char path[96], target[256];
    int vm_fd = -1, vcpu_fd = -1;
    pid_t vcpu_tid = -1;
    snprintf(path, sizeof(path), "/proc/%d/fd", bench_child);
    DIR *directory = opendir(path);
    if (!directory) die("Firecracker fd identities");
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        if (strspn(entry->d_name, "0123456789") != strlen(entry->d_name)) continue;
        snprintf(path, sizeof(path), "/proc/%d/fd/%.16s", bench_child, entry->d_name);
        ssize_t size = readlink(path, target, sizeof(target) - 1);
        if (size < 0) die("Firecracker fd identity link");
        target[size] = 0;
        if (strstr(target, "kvm-vcpu")) vcpu_fd = atoi(entry->d_name);
        else if (strstr(target, "kvm-vm")) vm_fd = atoi(entry->d_name);
    }
    closedir(directory);
    snprintf(path, sizeof(path), "/proc/%d/task", bench_child);
    directory = opendir(path);
    if (!directory) die("Firecracker thread identities");
    while ((entry = readdir(directory))) {
        if (strspn(entry->d_name, "0123456789") != strlen(entry->d_name)) continue;
        snprintf(path, sizeof(path), "/proc/%d/task/%.16s/comm", bench_child, entry->d_name);
        FILE *comm = fopen(path, "r");
        if (!comm) die("Firecracker thread name");
        if (fgets(target, sizeof(target), comm) && strstr(target, "fc_vcpu"))
            vcpu_tid = atoi(entry->d_name);
        fclose(comm);
    }
    closedir(directory);
    if (vm_fd < 0 || vcpu_fd < 0 || vcpu_tid <= 0) {
        fprintf(stderr, "Missing native KVM VM/vCPU/thread identity\n"); exit(1);
    }
    printf("BENCH_JSON {\"event\":\"native_identity\",\"role\":\"%c\",\"phase\":\"%s\","
           "\"round\":%u,\"pid\":%d,\"process_start\":%" PRIu64 ",\"vm_fd\":%d,"
           "\"vcpu_fd\":%d,\"vcpu_tid\":%d}\n", role, phase, round, bench_child,
           bench_process_start(bench_child), vm_fd, vcpu_fd, vcpu_tid);
}

static uint64_t migration_tsc(void);
static uint64_t migration_available(void);
static uint64_t migration_word(uint64_t instance, size_t page, unsigned word);
static uint64_t migration_sweep(unsigned char *memory, size_t length, uint64_t instance,
                                 uint64_t *errors);

static void guest_heartbeat(int sleeping, int quiet)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) die("guest /dev/mem");
    struct heartbeat *hb = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 64 * 1024 * 1024);
    if (hb == MAP_FAILED) die("guest heartbeat mmap");
    close(fd);
    int dense = env_enabled("LAB_HEARTBEAT_DENSE");
    size_t size = 16 * 1024 * 1024;
    uint64_t available = 0, instance = 0;
    unsigned char *state;
    if (dense) {
        available = migration_available();
        const uint64_t headroom = 16 * UINT64_C(1024) * 1024;
        if (available < headroom + 16 * UINT64_C(1024) * 1024) {
            fprintf(stderr, "Insufficient guest memory for dense heartbeat\n"); exit(1);
        }
        size = (available - headroom) & ~(size_t)4095;
        state = mmap(NULL, size, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (state == MAP_FAILED || madvise(state, size, MADV_NOHUGEPAGE))
            die("dense heartbeat memory");
        instance = migration_tsc() ^ monotonic_ns() ^ UINT64_C(0x6461786865617274);
        if (!instance) instance = 1;
        for (size_t offset = 0; offset < size; offset += 4096) {
            uint64_t *words = (void *)(state + offset);
            for (unsigned word = 0; word < 8; word++)
                words[word] = migration_word(instance, offset / 4096, word);
        }
    } else {
        state = malloc(size);
        if (!state) die("heartbeat retained heap");
        for (size_t i = 0; i < size; i++) state[i] = (unsigned char)(i * 31u + 77u);
    }
    atomic_store(&hb->sequence, 0);
    atomic_store(&hb->epoch, 0);
    atomic_store(&hb->acknowledged, 0);
    atomic_store(&hb->verified, 0);
    atomic_store(&hb->errors, 0);
    atomic_store(&hb->clock_ns,0);atomic_store(&hb->clock_errors,0);
    atomic_store(&hb->tsc,0);atomic_store(&hb->tsc_errors,0);
    atomic_store(&hb->ack_tsc,0);atomic_store(&hb->ack_epoch,0);
    atomic_store(&hb->working_set_bytes, size);
    atomic_store(&hb->available_bytes, available);
    atomic_store(&hb->instance_id, instance);
    atomic_store(&hb->first_sweep_ns, 0);atomic_store(&hb->warm_sweep_ns, 0);
    atomic_store(&hb->warm_done, 0);
    int timing=env_enabled("LAB_HEARTBEAT_TIMING");
    unsigned spins=getenv("LAB_HEARTBEAT_SPINS")?strtoul(getenv("LAB_HEARTBEAT_SPINS"),NULL,10):128;
    int churn=env_enabled("LAB_HEARTBEAT_CHURN");
    if (dense && churn) {
        fprintf(stderr, "Dense heartbeat does not combine with changing-memory stress\n"); exit(1);
    }
    unsigned char *changing=churn?mmap(NULL,32*1024*1024,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0):NULL;
    uint64_t written[32]={0},previous_clock=0,previous_tsc=0;
    if(churn && changing==MAP_FAILED)die("heartbeat changing memory");
    if(env_enabled("LAB_HEARTBEAT_PIT")){
        if(ioperm(0x40,4,1))die("guest PIT I/O permissions");
        /* Keep channel 0 periodic at approximately 1kHz, exercising the
         * shared PIT state, native hrtimer and cached worker at handoff. */
        outb(0x34,0x43);outb(1193&0xff,0x40);outb(1193>>8,0x40);
        if(ioperm(0x40,4,0))die("guest PIT I/O permissions reset");
    }
    if (dense) {
        printf("L2_DENSE_READY working_set_bytes=%zu available_bytes=%" PRIu64
               " headroom_mib=16 instance_id=%" PRIu64 "\n", size, available, instance);
        fflush(stdout);
    }
    atomic_store_explicit(&hb->magic, HB_MAGIC, memory_order_release);
    uint64_t previous = 0;
    for (;;) {
        uint64_t epoch = atomic_load_explicit(&hb->epoch, memory_order_acquire);
        atomic_fetch_add_explicit(&hb->sequence, 1, memory_order_release);
        if(timing && epoch!=previous){
            unsigned low,high;
            __asm__ __volatile__("lfence; rdtsc":"=a"(low),"=d"(high)::"memory");
            atomic_store_explicit(&hb->ack_tsc,((uint64_t)high<<32)|low,memory_order_relaxed);
            atomic_store_explicit(&hb->ack_epoch,epoch,memory_order_relaxed);
        }
        if (!dense) atomic_store_explicit(&hb->acknowledged, epoch, memory_order_release);
        if (epoch != previous) {
            unsigned low,high;
            __asm__ __volatile__("lfence; rdtsc":"=a"(low),"=d"(high)::"memory");
            uint64_t tsc=((uint64_t)high<<32)|low;
            if(previous_tsc && tsc<previous_tsc)atomic_fetch_add(&hb->tsc_errors,1);
            previous_tsc=tsc;atomic_store(&hb->tsc,tsc);
            struct timespec now;
            if(clock_gettime(CLOCK_MONOTONIC,&now))die("guest monotonic clock");
            uint64_t ns=(uint64_t)now.tv_sec*1000000000+now.tv_nsec;
            if(previous_clock && ns<previous_clock)atomic_fetch_add(&hb->clock_errors,1);
            previous_clock=ns;atomic_store(&hb->clock_ns,ns);
            if (dense) {
                /* One ACK before W; no further ACK writes occur while either
                 * sweep is running. The source must wait for warm_done before
                 * requesting another pause. */
                atomic_store_explicit(&hb->acknowledged, epoch, memory_order_release);
                uint64_t errors = 0;
                uint64_t first_ns = migration_sweep(state, size, instance, &errors);
                atomic_store(&hb->first_sweep_ns, first_ns);
                atomic_fetch_add(&hb->errors, errors);
                atomic_store_explicit(&hb->verified, epoch, memory_order_release);
                errors = 0;
                uint64_t warm_ns = migration_sweep(state, size, instance, &errors);
                atomic_store(&hb->warm_sweep_ns, warm_ns);
                atomic_fetch_add(&hb->errors, errors);
                atomic_store_explicit(&hb->warm_done, epoch, memory_order_release);
            } else {
                for (size_t i = 0; i < size; i++) {
                    if (state[i] != (unsigned char)(i * 31u + 77u)) atomic_fetch_add(&hb->errors, 1);
                }
            }
            if(churn){
                unsigned block=epoch%32;
                unsigned char *region=changing+block*1024*1024;
                for(size_t i=0;i<1024*1024;i++){
                    if(written[block] && region[i]!=(unsigned char)(i*17+written[block]))atomic_fetch_add(&hb->errors,1);
                    region[i]=(unsigned char)(i*17+epoch);
                }
                written[block]=epoch;
            }
            if(!quiet){
                printf("L2_HANDOFF epoch=%" PRIu64 " sequence=%" PRIu64 " errors=%" PRIu64 "\n",
                       epoch, atomic_load(&hb->sequence), atomic_load(&hb->errors));
                fflush(stdout);
            }
            if (!dense) atomic_store_explicit(&hb->verified, epoch, memory_order_release);
            previous = epoch;
        }
        if (sleeping) usleep(100);
        else for (unsigned spin = 0; spin < spins; spin++) __asm__ __volatile__("pause");
    }
}

static void bench_prepare_reuse(char role, unsigned round, int seed)
{
    /* One coarse operation per incoming handoff. It only retires local host
     * state; no incoming snapshot or shared guest-RAM writes are permitted. */
    double prepare_us = bench_api("PUT", "/actions", "{\"action_type\":\"PrepareReuse\"}");
    printf("BENCH_JSON {\"event\":\"reuse_prepare\",\"role\":\"%c\",\"round\":%u,"
           "\"prepare_us\":%.3f,\"seed\":%s,\"outside_pause_to_ack\":true}\n",
           role, round, prepare_us, seed ? "true" : "false");
}

static void handoff_bench(char role, const char *dax, unsigned rounds,
                          int warm, unsigned memory_mib, uint64_t nonce)
{
    bench_transaction_paths();
    int reuse = env_enabled("BENCH_REUSE_KVM");
    int dense = env_enabled("LAB_HEARTBEAT_DENSE");
    int reuse_prepare = env_enabled("BENCH_REUSE_PREPARE");
    int prepare_on_demand = env_enabled("BENCH_REUSE_PREPARE_ON_DEMAND");
    if (reuse && !warm) {
        fprintf(stderr, "KVM reuse requires warm mode\n"); exit(1);
    }
    if (reuse_prepare && !reuse) {
        fprintf(stderr, "Reuse preparation requires KVM reuse\n"); exit(1);
    }
    if (prepare_on_demand && !reuse_prepare) {
        fprintf(stderr, "On-demand preparation requires reuse preparation\n"); exit(1);
    }
    int profile_ack = env_enabled("BENCH_ACK_PROFILE");
    if (profile_ack) {
        bench_ack_trace_fd = open("/sys/kernel/tracing/trace_marker", O_WRONLY | O_CLOEXEC);
        if (bench_ack_trace_fd < 0) die("open snapshot ACK trace marker");
        atexit(bench_ack_trace_cleanup);
        if (env_enabled("BENCH_ACK_TRACE_GATE")) {
            bench_ack_trace_gate_fd = open("/sys/kernel/tracing/tracing_on", O_WRONLY | O_CLOEXEC);
            if (bench_ack_trace_gate_fd < 0) die("open snapshot ACK tracing_on");
            bench_ack_trace_gate(0);
        }
    }
    if (getenv("BENCH_RUN_DIR")) {
        struct sigaction action = {.sa_handler = bench_terminate};
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL)) die("snapshot termination handler");
    }
    unsigned dwell_us = getenv("BENCH_DWELL_US") ? strtoul(getenv("BENCH_DWELL_US"), NULL, 10) : 20000;
    unsigned full_samples = getenv("BENCH_FULL_SAMPLES") ? strtoul(getenv("BENCH_FULL_SAMPLES"), NULL, 10) : 6;
    handoff_poll_us = getenv("BENCH_POLL_US") ? strtoul(getenv("BENCH_POLL_US"), NULL, 10) : 50;
    handoff_spin_us = getenv("BENCH_SPIN_US") ? strtoul(getenv("BENCH_SPIN_US"), NULL, 10) : 0;
    if (getenv("BENCH_TIMER_SLACK_NS") &&
        prctl(PR_SET_TIMERSLACK, strtoul(getenv("BENCH_TIMER_SLACK_NS"), NULL, 10)))
        die("snapshot controller timer slack");
    if (strcmp(dax, "/dev/dax0.0") ||
        dax_attribute(dax, "size") < (memory_mib + UINT64_C(2)) * 1024 * 1024) {
        fprintf(stderr, "Benchmark requires /dev/dax0.0 and RAM allocation at 2MiB\n"); exit(1);
    }
    int fd = open_dax(dax);
    size_t length = 2 * 1024 * 1024;
    unsigned char *map = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    struct heartbeat *hb = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 66 * 1024 * 1024);
    if (map == MAP_FAILED || hb == MAP_FAILED) die("benchmark DAX mmap");
    close(fd);
    struct handoff_control *ctrl = (void *)(map + 1024 * 1024);
    unsigned me = role == 'A' ? 0 : 1;
    atexit(bench_cleanup);
    setvbuf(stdout, NULL, _IONBF, 0);
    /* Optional control: keep KVM hardware enabled across VMM replacement.
     * This empty VM is outside the measured path and has no vCPU or guest RAM. */
    int keepalive = -1;
    if (env_enabled("BENCH_KVM_KEEPALIVE")) {
        int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
        if (kvm < 0) die("keepalive /dev/kvm");
        keepalive = ioctl(kvm, KVM_CREATE_VM, 0);
        if (keepalive < 0 || fcntl(keepalive, F_SETFD, FD_CLOEXEC)) die("keepalive VM");
        close(kvm);
    }
    if (!me) {
        memset(ctrl, 0, sizeof(*ctrl));
        ctrl->schema = HANDOFF_SCHEMA;
        ctrl->memory_bytes = memory_mib * UINT64_C(1024) * 1024;
        atomic_store(&hb->magic, 0);
        if (reuse) {
            /* B first runs and pauses disposable microVM X. Only then may A
             * cold-boot Y into the same shared RAM. Seed time is not measured. */
            atomic_store_explicit(&ctrl->nonce, nonce, memory_order_release);
            bench_session = &ctrl->nonce;
            bench_session_nonce = nonce;
            wait_value(&ctrl->ready[1], UINT64_MAX, "previous microVM paused on B");
            atomic_store(&hb->magic, 0);
        }
        bench_start(1, 0);
        wait_value(&hb->magic, HB_MAGIC, "initial guest heartbeat");
        if (dense) {
            ctrl->working_set_bytes = atomic_load(&hb->working_set_bytes);
            ctrl->instance_id = atomic_load(&hb->instance_id);
            if (!ctrl->working_set_bytes || ctrl->working_set_bytes >= ctrl->memory_bytes ||
                !ctrl->instance_id || !atomic_load(&hb->available_bytes)) {
                fprintf(stderr, "Dense primary guest workload identity missing\n"); exit(1);
            }
            printf("BENCH_JSON {\"event\":\"working_set_boot\",\"role\":\"A\","
                   "\"working_set_bytes\":%" PRIu64 ",\"available_bytes\":%" PRIu64
                   ",\"instance_id\":%" PRIu64 "}\n", ctrl->working_set_bytes,
                   atomic_load(&hb->available_bytes), ctrl->instance_id);
        }
        bench_api("PATCH", "/vm", "{\"state\":\"Paused\"}");
        for (unsigned i = 0; i < full_samples; i++) {
            char request[512];
            snprintf(request, sizeof(request), "{\"snapshot_type\":\"Full\",\"snapshot_path\":\"%s\","
                "\"mem_file_path\":\"%s\",\"sync_snapshot_files\":false}", bench_full_state_path, bench_full_memory_path);
            double duration = bench_api("PUT", "/snapshot/create", request);
            struct stat st;
            if (stat(bench_full_memory_path, &st) || st.st_size != (off_t)memory_mib * 1024 * 1024) {
                fprintf(stderr, "Full snapshot size mismatch\n"); exit(1);
            }
            printf("BENCH_JSON {\"event\":\"full_snapshot\",\"sample\":%u,\"us\":%.3f,\"ram_bytes\":%jd}\n",
                   i, duration, (intmax_t)st.st_size);
        }
        unlink(bench_full_memory_path);
        unlink(bench_full_state_path);
        bench_api("PATCH", "/vm", "{\"state\":\"Resumed\"}");
        atomic_store_explicit(&ctrl->nonce, nonce, memory_order_release);
        bench_session = &ctrl->nonce;
        bench_session_nonce = nonce;
        if (reuse) bench_native_identity(role, "primary_boot", 0);
    } else {
        wait_value(&ctrl->nonce, nonce, "benchmark session");
        bench_session = &ctrl->nonce;
        bench_session_nonce = nonce;
        if (ctrl->schema != HANDOFF_SCHEMA || ctrl->memory_bytes != memory_mib * UINT64_C(1024) * 1024) {
            fprintf(stderr, "Snapshot mailbox schema or RAM layout mismatch\n"); exit(1);
        }
        if (reuse) {
            bench_start(1, 0);
            wait_value(&hb->magic, HB_MAGIC, "previous microVM heartbeat");
            bench_api("PATCH", "/vm", "{\"state\":\"Paused\"}");
            uint64_t sequence = atomic_load(&hb->sequence);
            usleep(1000);
            if (atomic_load(&hb->sequence) != sequence) {
                fprintf(stderr, "Previous microVM not paused\n"); exit(1);
            }
            printf("BENCH_JSON {\"event\":\"reuse_seed\",\"role\":\"B\",\"sequence\":%" PRIu64 ","
                   "\"paused_before_primary_boot\":true,\"excluded_from_handoff_timing\":true}\n", sequence);
            bench_native_identity(role, "previous_guest_paused", 0);
            if (reuse_prepare) bench_prepare_reuse(role, 0, 1);
            atomic_store_explicit(&ctrl->ready[1], UINT64_MAX, memory_order_release);
            wait_value(&ctrl->ready[0], 1, "primary microVM booted on A");
        } else if (warm) bench_start(0, 1);
    }
    atomic_store_explicit(&ctrl->ready[me], 1, memory_order_release);
    for (unsigned r = 0; r < rounds; r++) {
        unsigned source = r % 2;
        if (me == source) {
            double cycle_start = monotonic(), ready_wait_us = 0, request_start = 0;
            int ready_at_request = 0;
            if (!reuse_prepare) {
                double ready_start = monotonic();
                ready_at_request = atomic_load_explicit(&ctrl->ready[1 - me], memory_order_acquire) == r + 1;
                wait_value(&ctrl->ready[1 - me], r + 1, "destination ready");
                ready_wait_us = (monotonic() - ready_start) * 1e6;
            }
            wait_value(&hb->verified, r, "retained guest heap verified");
            if (dense) wait_value(&hb->warm_done, r, "previous dense guest warm scan");
            /* The optional coarse preparation overlaps this active interval.
             * The source remains running while waiting for destination readiness. */
            usleep(dwell_us);
            if (reuse_prepare) {
                double ready_start = monotonic();
                request_start = ready_start;
                /* Test control: withhold the next preparation until this
                 * request, so post-ACK verification cannot hide its latency. */
                if (prepare_on_demand)
                    atomic_store_explicit(&ctrl->preparing, r + 1, memory_order_release);
                ready_at_request = atomic_load_explicit(&ctrl->ready[1 - me], memory_order_acquire) == r + 1;
                wait_value(&ctrl->ready[1 - me], r + 1, "prepared destination ready");
                ready_wait_us = (monotonic() - ready_start) * 1e6;
            }
            double start = monotonic();
            if (!reuse_prepare) request_start = start;
            atomic_store_explicit(&ctrl->preparing, r + 1, memory_order_release);
            double pause_us = bench_api("PATCH", "/vm", "{\"state\":\"Paused\"}");
            ctrl->sequence = atomic_load_explicit(&hb->sequence, memory_order_acquire);
            ctrl->guest_clock_ns = atomic_load(&hb->clock_ns);
            ctrl->guest_tsc = atomic_load(&hb->tsc);
            char request[512];
            snprintf(request, sizeof(request), "{\"snapshot_type\":\"Full\",\"snapshot_path\":\"%s\","
                "\"mem_file_path\":\"/dev/dax0.0\",\"sync_snapshot_files\":false}", bench_checkpoint_path);
            double snapshot_us = bench_api("PUT", "/snapshot/create", request);
            double publish_start = monotonic();
            ctrl->state_size = read_checkpoint(ctrl->state, sizeof(ctrl->state));
            ctrl->state_round = r + 1;
            ctrl->state_hash = checkpoint_hash(ctrl->state, ctrl->state_size);
            uint64_t state_size = ctrl->state_size, state_hash = ctrl->state_hash;
            uint64_t sequence = ctrl->sequence, guest_clock_ns = ctrl->guest_clock_ns, guest_tsc = ctrl->guest_tsc;
            if (!r && env_enabled("BENCH_CORRUPT_STATE"))
                ctrl->state[state_size / 2] ^= 1;
            atomic_store_explicit(&ctrl->published, r + 1, memory_order_release);
            double publish_us = (monotonic() - publish_start) * 1e6;
            wait_value(&ctrl->acknowledged, r + 1, "destination guest acknowledgement");
            double end = monotonic(), handoff_us = (end - start) * 1e6;
            double cycle_us = (end - cycle_start) * 1e6;
            double request_to_ack_us = (end - request_start) * 1e6;
            double pause_to_working_set_ready_us = 0, pause_to_warm_ready_us = 0;
            if (dense) {
                wait_value(&ctrl->scan_ready, r + 1, "destination dense working set ready");
                pause_to_working_set_ready_us = (monotonic() - start) * 1e6;
                wait_value(&ctrl->warm_ready, r + 1, "destination dense warm scan ready");
                pause_to_warm_ready_us = (monotonic() - start) * 1e6;
            }
            printf("BENCH_JSON {\"event\":\"source\",\"round\":%u,\"role\":\"%c\","
                "\"pause_us\":%.3f,\"snapshot_us\":%.3f,\"publish_us\":%.3f,"
                "\"handoff_us\":%.3f,\"destination_ready_wait_us\":%.3f,"
                "\"scheduling_wait_plus_handoff_us\":%.3f,\"request_to_ack_us\":%.3f,\"cycle_us\":%.3f,"
                "\"destination_ready_at_request\":%s,\"state_bytes\":%" PRIu64 ",\"state_hash\":%" PRIu64 ","
                "\"ram_hva\":%" PRIu64 ",\"guest_clock_ns\":%" PRIu64 ",\"guest_tsc\":%" PRIu64 ","
                "\"sequence\":%" PRIu64,
                r, role, pause_us, snapshot_us, publish_us, handoff_us, ready_wait_us,
                ready_wait_us + handoff_us, request_to_ack_us, cycle_us,
                ready_at_request ? "true" : "false", state_size,
                state_hash, bench_ram_hva(memory_mib), guest_clock_ns, guest_tsc, sequence);
            if (dense)
                printf(",\"pause_to_working_set_ready_us\":%.3f,\"pause_to_warm_ready_us\":%.3f",
                       pause_to_working_set_ready_us, pause_to_warm_ready_us);
            puts("}");
            if (reuse) bench_native_identity(role, "source_paused", r);
            else bench_cleanup();
            if (r + 1 < rounds) {
                if (warm && !reuse) bench_start(0, r + 2);
                if (reuse_prepare) {
                    if (prepare_on_demand)
                        wait_value(&ctrl->preparing, r + 2, "next source requested preparation");
                    bench_prepare_reuse(role, r + 1, 0);
                }
                atomic_store_explicit(&ctrl->ready[me], r + 2, memory_order_release);
            }
        } else {
            snapshot_wait_published(ctrl, r + 1);
            double received = monotonic();
            if (atomic_load(&ctrl->nonce) != nonce || ctrl->schema != HANDOFF_SCHEMA ||
                ctrl->state_round != r + 1 || ctrl->memory_bytes != memory_mib * UINT64_C(1024) * 1024 ||
                !ctrl->state_size || ctrl->state_size > sizeof(ctrl->state) ||
                ctrl->state_hash != checkpoint_hash(ctrl->state, ctrl->state_size)) {
                fprintf(stderr, "Snapshot mailbox session/round/length/hash validation failed\n");
                printf("BENCH_JSON {\"event\":\"payload_rejected\",\"role\":\"%c\",\"round\":%u,\"before_deserialize\":true}\n", role, r);
                exit(3);
            }
            uint64_t state_size = ctrl->state_size, state_hash = ctrl->state_hash;
            write_checkpoint(ctrl->state, ctrl->state_size);
            double receive_us = (monotonic() - received) * 1e6;
            double startup_us = warm ? 0 : bench_start(0, r + 1);
            char load[512];
            snprintf(load, sizeof(load),
                "{\"snapshot_path\":\"%s\",\"mem_backend\":{\"backend_type\":\"File\","
                "\"backend_path\":\"/dev/dax0.0\"},\"resume_vm\":false,\"clock_realtime\":%s}",
                bench_checkpoint_path,
                env_enabled("BENCH_CLOCK_REALTIME") ? "true" : "false");
            bench_ack_trace_gate(1);
            uint64_t load_begin_ns = bench_ack_marker(role, r, nonce, "LOAD_BEGIN", NULL);
            double restore_us = bench_api("PUT", "/snapshot/load", load);
            uint64_t load_end_ns = bench_ack_marker(role, r, nonce, "LOAD_END", NULL);
            if (atomic_load(&hb->sequence) != ctrl->sequence || atomic_load(&hb->magic) != HB_MAGIC) {
                fprintf(stderr, "RAM changed during paused restore\n"); exit(1);
            }
            atomic_store_explicit(&hb->epoch, r + 1, memory_order_release);
            struct bench_ack_anchor resume_begin_anchor = {0}, resume_end_anchor = {0}, ack_anchor = {0};
            uint64_t resume_begin_ns = bench_ack_marker(role, r, nonce, "RESUME_BEGIN", &resume_begin_anchor);
            double resume_us = bench_api("PATCH", "/vm", "{\"state\":\"Resumed\"}");
            uint64_t resume_end_ns = bench_ack_marker(role, r, nonce, "RESUME_END", &resume_end_anchor);
            double ack_start = monotonic();
            wait_value(&hb->acknowledged, r + 1, "guest instruction after resume");
            double ack_us = (monotonic() - ack_start) * 1e6;
            uint64_t ack_observed_ns = bench_ack_marker(role, r, nonce, "ACK_OBSERVED", &ack_anchor);
            uint64_t guest_ack_tsc = profile_ack ? atomic_load_explicit(&hb->ack_tsc, memory_order_relaxed) : 0;
            uint64_t guest_ack_epoch = profile_ack ? atomic_load_explicit(&hb->ack_epoch, memory_order_relaxed) : 0;
            if (profile_ack && (!guest_ack_tsc || guest_ack_epoch != r + 1)) {
                fprintf(stderr, "Guest ACK profile timestamp/epoch mismatch\n"); exit(1);
            }
            uint64_t guest_clock_ns = atomic_load(&hb->clock_ns), guest_tsc = atomic_load(&hb->tsc);
            atomic_store_explicit(&ctrl->acknowledged, r + 1, memory_order_release);
            /* Mapping diagnostics are outside the source downtime boundary. */
            wait_value(&hb->verified, r + 1, "destination retained heap and clock sample");
            uint64_t heap_verified_ns = bench_ack_marker(role, r, nonce, "HEAP_VERIFIED", NULL);
            if (dense) {
                atomic_store_explicit(&ctrl->scan_ready, r + 1, memory_order_release);
                wait_value(&hb->warm_done, r + 1, "destination dense warm scan");
                if (atomic_load(&hb->working_set_bytes) != ctrl->working_set_bytes ||
                    atomic_load(&hb->instance_id) != ctrl->instance_id ||
                    atomic_load(&hb->errors) || atomic_load(&hb->clock_errors) ||
                    atomic_load(&hb->tsc_errors)) {
                    fprintf(stderr, "Dense retained workload/instance/clock check failed\n"); exit(1);
                }
                atomic_store_explicit(&ctrl->warm_ready, r + 1, memory_order_release);
                printf("BENCH_JSON {\"event\":\"working_set\",\"round\":%u,\"role\":\"%c\","
                       "\"working_set_bytes\":%" PRIu64 ",\"available_bytes\":%" PRIu64
                       ",\"instance_id\":%" PRIu64 ",\"first_sweep_ns\":%" PRIu64
                       ",\"warm_sweep_ns\":%" PRIu64 ",\"errors\":%" PRIu64
                       ",\"clock_errors\":%" PRIu64 ",\"tsc_errors\":%" PRIu64 "}\n",
                       r, role, atomic_load(&hb->working_set_bytes), atomic_load(&hb->available_bytes),
                       atomic_load(&hb->instance_id), atomic_load(&hb->first_sweep_ns),
                       atomic_load(&hb->warm_sweep_ns), atomic_load(&hb->errors),
                       atomic_load(&hb->clock_errors), atomic_load(&hb->tsc_errors));
            }
            bench_ack_trace_gate(0);
            uint64_t ram_hva = bench_ram_hva(memory_mib);
            if (reuse) bench_native_identity(role, "destination_restored", r);
            if (profile_ack)
                printf("BENCH_JSON {\"event\":\"ack_profile\",\"round\":%u,\"role\":\"%c\",\"nonce\":\"%016" PRIx64 "\","
                    "\"load_begin_ns\":%" PRIu64 ",\"load_end_ns\":%" PRIu64 ","
                    "\"resume_begin_ns\":%" PRIu64 ",\"resume_end_ns\":%" PRIu64 ",\"ack_observed_ns\":%" PRIu64 ","
                    "\"heap_verified_ns\":%" PRIu64 ","
                    "\"resume_begin_clock_before_ns\":%" PRIu64 ",\"resume_begin_clock_after_ns\":%" PRIu64 ",\"resume_begin_tsc\":%" PRIu64 ","
                    "\"resume_end_clock_before_ns\":%" PRIu64 ",\"resume_end_clock_after_ns\":%" PRIu64 ",\"resume_end_tsc\":%" PRIu64 ","
                    "\"ack_observed_clock_before_ns\":%" PRIu64 ",\"ack_observed_clock_after_ns\":%" PRIu64 ",\"ack_observed_tsc\":%" PRIu64 ","
                    "\"guest_ack_tsc\":%" PRIu64 ",\"guest_ack_epoch\":%" PRIu64 "}\n",
                    r, role, nonce, load_begin_ns, load_end_ns, resume_begin_ns, resume_end_ns,
                    ack_observed_ns, heap_verified_ns, resume_begin_anchor.clock_before_ns, resume_begin_anchor.clock_after_ns, resume_begin_anchor.tsc,
                    resume_end_anchor.clock_before_ns, resume_end_anchor.clock_after_ns, resume_end_anchor.tsc,
                    ack_anchor.clock_before_ns, ack_anchor.clock_after_ns, ack_anchor.tsc, guest_ack_tsc, guest_ack_epoch);
            printf("BENCH_JSON {\"event\":\"destination\",\"round\":%u,\"role\":\"%c\","
                "\"receive_us\":%.3f,\"startup_us\":%.3f,\"restore_us\":%.3f,"
                "\"resume_us\":%.3f,\"guest_ack_wait_us\":%.3f,\"state_bytes\":%" PRIu64 ","
                "\"state_hash\":%" PRIu64 ",\"ram_hva\":%" PRIu64 ",\"guest_clock_ns\":%" PRIu64 ","
                "\"guest_tsc\":%" PRIu64 "}\n",
                r, role, receive_us, startup_us, restore_us, resume_us, ack_us,
                state_size, state_hash, ram_hva, guest_clock_ns, guest_tsc);
        }
    }
    wait_value(&hb->verified, rounds, "final retained guest heap");
    if (dense) wait_value(&hb->warm_done, rounds, "final dense guest warm scan");
    if (atomic_load(&hb->errors) || atomic_load(&hb->clock_errors) || atomic_load(&hb->tsc_errors)) {
        fprintf(stderr, "Retained state failed: heap=%" PRIu64 " clock=%" PRIu64 " tsc=%" PRIu64 "\n",
                atomic_load(&hb->errors), atomic_load(&hb->clock_errors), atomic_load(&hb->tsc_errors));
        exit(1);
    }
    printf("BENCH_JSON {\"event\":\"complete\",\"role\":\"%c\",\"rounds\":%u,"
           "\"heap_bytes\":%" PRIu64 ",\"errors\":0,\"clock_errors\":0,\"tsc_errors\":0,\"sequence\":%" PRIu64 "}\n",
           role, rounds, dense ? atomic_load(&hb->working_set_bytes) : UINT64_C(16777216),
           atomic_load(&hb->sequence));
    /* Leave the final owner's resumed microVM alive for inspection. */
    if (reuse || me == rounds % 2) bench_child = 0;
    munmap(map, length);
    munmap(hb, length);
    bench_ack_trace_cleanup();
    if (keepalive >= 0) close(keepalive);
}

/* Full stop-and-copy control: ordinary private RAM, real TCP bytes, and a fresh
 * KVM VM/vCPU on every restore. The pattern/scan helpers are also used by the
 * opt-in dense DAX workload; this migration controller does not use DAX. */
#define MIGRATION_MAGIC UINT64_C(0x4d49475241544531)
#define MIGRATION_SCHEMA UINT64_C(0x4d49475200000001)
#define MIGRATION_GPA (64 * UINT64_C(1024) * 1024)
struct migration_heartbeat {
    _Atomic uint64_t magic, schema, instance_id, working_set_bytes, available_bytes;
    _Atomic uint64_t sequence, epoch, first_ack, ready, warm_done;
    _Atomic uint64_t errors, clock_errors, tsc_errors, clock_ns, tsc;
    _Atomic uint64_t first_sweep_ns, warm_sweep_ns;
};
_Static_assert(sizeof(struct migration_heartbeat) <= 4096, "migration heartbeat page");

static uint64_t migration_tsc(void)
{
    unsigned low, high;
    __asm__ __volatile__("lfence; rdtsc" : "=a"(low), "=d"(high) :: "memory");
    return ((uint64_t)high << 32) | low;
}

static uint64_t migration_available(void)
{
    FILE *file = fopen("/proc/meminfo", "r");
    if (!file) die("migration guest meminfo");
    char line[256];
    uint64_t kib = 0;
    while (fgets(line, sizeof(line), file))
        if (sscanf(line, "MemAvailable: %" SCNu64 " kB", &kib) == 1) break;
    fclose(file);
    if (!kib) { fprintf(stderr, "Guest MemAvailable missing\n"); exit(1); }
    return kib * 1024;
}

static uint64_t migration_word(uint64_t instance, size_t page, unsigned word)
{
    return instance ^ ((page + 1) * UINT64_C(0x9e3779b97f4a7c15)) ^
           ((word + 1) * UINT64_C(0xd6e8feb86659fd93));
}

static uint64_t migration_sweep(unsigned char *memory, size_t length, uint64_t instance,
                                 uint64_t *errors)
{
    uint64_t start = monotonic_ns();
    for (size_t offset = 0; offset < length; offset += 4096) {
        volatile uint64_t *words = (void *)(memory + offset);
        for (unsigned word = 0; word < 8; word++)
            if (words[word] != migration_word(instance, offset / 4096, word)) (*errors)++;
    }
    return monotonic_ns() - start;
}

static void migration_workload(char role)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) die("migration guest /dev/mem");
    struct migration_heartbeat *hb = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                                         MAP_SHARED, fd, MIGRATION_GPA);
    if (hb == MAP_FAILED) die("migration heartbeat map");
    close(fd);
    memset(hb, 0, sizeof(*hb));
    uint64_t available = migration_available();
    const char *headroom_env = getenv("LAB_MIGRATION_HEADROOM_MIB");
    uint64_t headroom_mib = headroom_env ? strtoull(headroom_env, NULL, 10) : 16;
    if (headroom_mib < 8 || headroom_mib > 128) {
        fprintf(stderr, "Migration guest headroom must be 8..128 MiB\n"); exit(1);
    }
    uint64_t headroom = headroom_mib * 1024 * 1024;
    if (available < headroom + 16 * UINT64_C(1024) * 1024) {
        fprintf(stderr, "Insufficient guest memory for migration workload\n"); exit(1);
    }
    size_t length = (available - headroom) & ~(size_t)4095;
    unsigned char *memory = mmap(NULL, length, PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (memory == MAP_FAILED || madvise(memory, length, MADV_NOHUGEPAGE))
        die("migration workload memory");
    uint64_t instance = migration_tsc() ^ monotonic_ns() ^ UINT64_C(0x6d6967726174696f);
    if (!instance) instance = 1;
    for (size_t offset = 0; offset < length; offset += 4096) {
        uint64_t *words = (void *)(memory + offset);
        for (unsigned word = 0; word < 8; word++)
            words[word] = migration_word(instance, offset / 4096, word);
    }
    atomic_store(&hb->schema, MIGRATION_SCHEMA);
    atomic_store(&hb->instance_id, instance);
    atomic_store(&hb->working_set_bytes, length);
    atomic_store(&hb->available_bytes, available);
    atomic_store(&hb->clock_ns, monotonic_ns());
    atomic_store(&hb->tsc, migration_tsc());
    printf("L2_MIGRATION_READY role=%c working_set_bytes=%zu available_bytes=%" PRIu64
           " headroom_mib=%" PRIu64 " instance_id=%" PRIu64 "\n",
           role, length, available, headroom_mib, instance);
    fflush(stdout);
    atomic_store_explicit(&hb->magic, MIGRATION_MAGIC, memory_order_release);
    uint64_t previous = 0, previous_clock = atomic_load(&hb->clock_ns);
    uint64_t previous_tsc = atomic_load(&hb->tsc);
    for (;;) {
        atomic_fetch_add_explicit(&hb->sequence, 1, memory_order_relaxed);
        uint64_t epoch = atomic_load_explicit(&hb->epoch, memory_order_acquire);
        if (epoch != previous) {
            uint64_t clock = monotonic_ns(), tsc = migration_tsc();
            if (clock < previous_clock) atomic_fetch_add(&hb->clock_errors, 1);
            if (tsc < previous_tsc) atomic_fetch_add(&hb->tsc_errors, 1);
            previous_clock = clock;
            previous_tsc = tsc;
            atomic_store(&hb->clock_ns, clock);
            atomic_store(&hb->tsc, tsc);
            /* First observable instruction response deliberately precedes W. */
            atomic_store_explicit(&hb->first_ack, epoch, memory_order_release);
            uint64_t errors = 0;
            uint64_t first_ns = migration_sweep(memory, length, instance, &errors);
            atomic_store(&hb->first_sweep_ns, first_ns);
            atomic_fetch_add(&hb->errors, errors);
            atomic_store_explicit(&hb->ready, epoch, memory_order_release);
            errors = 0;
            uint64_t warm_ns = migration_sweep(memory, length, instance, &errors);
            atomic_store(&hb->warm_sweep_ns, warm_ns);
            atomic_fetch_add(&hb->errors, errors);
            atomic_store_explicit(&hb->warm_done, epoch, memory_order_release);
            previous = epoch;
        }
        for (unsigned spin = 0; spin < 64; spin++) __asm__ __volatile__("pause");
    }
}

struct migration_packet {
    uint64_t schema, nonce, kind, round, memory_bytes, state_bytes, state_hash;
    struct migration_heartbeat heartbeat;
};
enum { MIG_READY = 1, MIG_PAYLOAD, MIG_ACK, MIG_SCAN_READY, MIG_WARM_READY, MIG_RECEIVED };

static void migration_io(int fd, void *buffer, size_t length, int sending)
{
    size_t done = 0;
    while (done < length) {
        ssize_t count = sending ? send(fd, (char *)buffer + done, length - done, MSG_NOSIGNAL) :
                                 read(fd, (char *)buffer + done, length - done);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) {
            if (!count) errno = ECONNRESET;
            die(sending ? "migration TCP send" : "migration TCP receive");
        }
        done += (size_t)count;
    }
}

static int migration_socket(char role, const char *peer, unsigned port)
{
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) die("migration TCP socket");
    int one = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one))) die("migration reuseaddr");
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(port)};
    if (role == 'B') {
        address.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(fd, (void *)&address, sizeof(address)) || listen(fd, 1))
            die("migration TCP listen");
        int accepted = accept4(fd, NULL, NULL, SOCK_CLOEXEC);
        if (accepted < 0) die("migration TCP accept");
        close(fd);
        fd = accepted;
    } else {
        if (inet_pton(AF_INET, peer, &address.sin_addr) != 1) {
            fprintf(stderr, "Migration peer must be an IPv4 address\n"); exit(1);
        }
        double until = monotonic() + 60;
        while (connect(fd, (void *)&address, sizeof(address))) {
            if ((errno != ECONNREFUSED && errno != ENETUNREACH && errno != EHOSTUNREACH) ||
                monotonic() > until) die("migration TCP connect");
            usleep(10000);
        }
    }
    if (setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one))) die("migration nodelay");
    struct timeval timeout = {.tv_sec = 180};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) die("migration TCP timeout");
    return fd;
}

static struct migration_packet migration_packet(uint64_t nonce, unsigned round,
                                                 unsigned memory_mib, unsigned kind)
{
    struct migration_packet packet = {0};
    packet.schema = MIGRATION_SCHEMA;
    packet.nonce = nonce;
    packet.round = round;
    packet.memory_bytes = memory_mib * UINT64_C(1024) * 1024;
    packet.kind = kind;
    return packet;
}

static void migration_receive_packet(int fd, struct migration_packet *packet,
                                      uint64_t nonce, unsigned round,
                                      unsigned memory_mib, unsigned kind)
{
    migration_io(fd, packet, sizeof(*packet), 0);
    if (packet->schema != MIGRATION_SCHEMA || packet->nonce != nonce ||
        packet->round != round || packet->kind != kind ||
        packet->memory_bytes != memory_mib * UINT64_C(1024) * 1024) {
        fprintf(stderr, "Migration schema/nonce/round/kind/RAM validation failed\n"); exit(3);
    }
}

static uint64_t migration_file_size(const char *path)
{
    struct stat st;
    if (stat(path, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0) die("migration snapshot stat");
    return st.st_size;
}

static double migration_file(int socket_fd, const char *path, uint64_t length, int sending,
                               unsigned char *buffer, size_t capacity)
{
    uint64_t start = monotonic_ns();
    int fd = open(path, (sending ? O_RDONLY : O_WRONLY | O_CREAT | O_TRUNC) | O_CLOEXEC, 0600);
    if (fd < 0) die("migration file open");
    uint64_t done = 0;
    while (done < length) {
        size_t chunk = length - done > capacity ? capacity : (size_t)(length - done);
        if (!sending) migration_io(socket_fd, buffer, chunk, 0);
        size_t local = 0;
        while (local < chunk) {
            ssize_t count = sending ? read(fd, buffer + local, chunk - local) :
                                      write(fd, buffer + local, chunk - local);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) die("migration file copy");
            local += count;
        }
        if (sending) migration_io(socket_fd, buffer, chunk, 1);
        done += chunk;
    }
    struct stat st;
    if (fstat(fd, &st) || st.st_size != (off_t)length) {
        fprintf(stderr, "Migration file size changed\n"); exit(3);
    }
    if (close(fd)) die("migration file close");
    return (monotonic_ns() - start) / 1000.0;
}

static uint64_t migration_state_hash(const char *path)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) die("migration state hash open");
    unsigned char buffer[4096];
    uint64_t hash = UINT64_C(14695981039346656037);
    ssize_t count;
    while ((count = read(fd, buffer, sizeof(buffer))) > 0)
        for (ssize_t i = 0; i < count; i++) hash = (hash ^ buffer[i]) * UINT64_C(1099511628211);
    if (count < 0 || close(fd)) die("migration state hash read");
    return hash;
}

static void migration_process_copy(uint64_t address, void *buffer, size_t length, int writing)
{
    struct iovec local = {.iov_base = buffer, .iov_len = length};
    struct iovec remote = {.iov_base = (void *)(uintptr_t)address, .iov_len = length};
    ssize_t count = writing ? process_vm_writev(bench_child, &local, 1, &remote, 1, 0) :
                              process_vm_readv(bench_child, &local, 1, &remote, 1, 0);
    if (count != (ssize_t)length) die(writing ? "migration private RAM write" : "migration private RAM read");
}

static uint64_t migration_ram_hva(unsigned memory_mib, int require_heartbeat)
{
    char path[80], line[1024];
    snprintf(path, sizeof(path), "/proc/%d/maps", bench_child);
    FILE *maps = fopen(path, "r");
    if (!maps) die("migration private RAM maps");
    uint64_t found = 0;
    while (fgets(line, sizeof(line), maps)) {
        unsigned long long first, last, offset;
        char permissions[5];
        if (sscanf(line, "%llx-%llx %4s %llx", &first, &last, permissions, &offset) != 4 ||
            strcmp(permissions, "rw-p") || offset ||
            last - first != memory_mib * UINT64_C(1024) * 1024 || strstr(line, "/dev/dax")) continue;
        if (require_heartbeat) {
            struct migration_heartbeat hb;
            migration_process_copy(first + MIGRATION_GPA, &hb, sizeof(hb), 0);
            if (atomic_load(&hb.magic) != MIGRATION_MAGIC) continue;
        }
        if (found) { fprintf(stderr, "Ambiguous migration RAM mapping\n"); exit(1); }
        found = first;
    }
    fclose(maps);
    if (!found) { fprintf(stderr, "Missing private Firecracker RAM mapping\n"); exit(1); }
    return found;
}

struct migration_ram_stats {
    uint64_t rss_bytes, private_bytes, anonymous_bytes, anon_huge_bytes;
};

/* Diagnostic only: read after the guest's ready response has already been sent. */
static struct migration_ram_stats migration_ram_stats(uint64_t hva)
{
    struct migration_ram_stats stats = {0};
    char path[80], line[1024];
    snprintf(path, sizeof(path), "/proc/%d/smaps", bench_child);
    FILE *file = fopen(path, "r");
    if (!file) die("migration RAM residency");
    int selected = 0;
    while (fgets(line, sizeof(line), file)) {
        unsigned long long first, last;
        if (sscanf(line, "%llx-%llx", &first, &last) == 2) {
            selected = first == hva;
            continue;
        }
        if (!selected) continue;
        uint64_t kib;
        if (sscanf(line, "Rss: %" SCNu64 " kB", &kib) == 1) stats.rss_bytes = kib * 1024;
        else if (sscanf(line, "Private_Clean: %" SCNu64 " kB", &kib) == 1 ||
                 sscanf(line, "Private_Dirty: %" SCNu64 " kB", &kib) == 1)
            stats.private_bytes += kib * 1024;
        else if (sscanf(line, "Anonymous: %" SCNu64 " kB", &kib) == 1) stats.anonymous_bytes = kib * 1024;
        else if (sscanf(line, "AnonHugePages: %" SCNu64 " kB", &kib) == 1) stats.anon_huge_bytes = kib * 1024;
    }
    fclose(file);
    return stats;
}

static struct migration_heartbeat migration_read(uint64_t hva)
{
    struct migration_heartbeat heartbeat;
    migration_process_copy(hva + MIGRATION_GPA, &heartbeat, sizeof(heartbeat), 0);
    return heartbeat;
}

static void migration_check_heartbeat(const struct migration_heartbeat *hb, uint64_t instance,
                                        uint64_t epoch)
{
    if (atomic_load(&hb->magic) != MIGRATION_MAGIC || atomic_load(&hb->schema) != MIGRATION_SCHEMA ||
        !atomic_load(&hb->working_set_bytes) || !atomic_load(&hb->instance_id) ||
        (instance && atomic_load(&hb->instance_id) != instance) ||
        atomic_load(&hb->epoch) != epoch || atomic_load(&hb->errors) ||
        atomic_load(&hb->clock_errors) || atomic_load(&hb->tsc_errors)) {
        fprintf(stderr, "Migration retained-memory/epoch/instance/clock validation failed: "
            "magic=%016" PRIx64 " expected_magic=%016" PRIx64
            " schema=%016" PRIx64 " expected_schema=%016" PRIx64
            " epoch=%" PRIu64 " expected_epoch=%" PRIu64
            " instance=%" PRIu64 " expected_instance=%" PRIu64
            " working_set_bytes=%" PRIu64 " errors=%" PRIu64
            " clock_errors=%" PRIu64 " tsc_errors=%" PRIu64
            " clock_ns=%" PRIu64 " tsc=%" PRIu64
            " sequence=%" PRIu64 " first_ack=%" PRIu64
            " ready=%" PRIu64 " warm_done=%" PRIu64 "\n",
            atomic_load(&hb->magic), MIGRATION_MAGIC, atomic_load(&hb->schema), MIGRATION_SCHEMA,
            atomic_load(&hb->epoch), epoch, atomic_load(&hb->instance_id), instance,
            atomic_load(&hb->working_set_bytes), atomic_load(&hb->errors),
            atomic_load(&hb->clock_errors), atomic_load(&hb->tsc_errors),
            atomic_load(&hb->clock_ns), atomic_load(&hb->tsc), atomic_load(&hb->sequence),
            atomic_load(&hb->first_ack), atomic_load(&hb->ready), atomic_load(&hb->warm_done));
        exit(3);
    }
}

static struct migration_heartbeat migration_wait(uint64_t hva, size_t offset, uint64_t expected)
{
    uint64_t start = monotonic_ns();
    struct migration_heartbeat hb;
    for (;;) {
        hb = migration_read(hva);
        uint64_t value;
        memcpy(&value, (char *)&hb + offset, sizeof(value));
        if (value == expected) return hb;
        if (monotonic_ns() - start > UINT64_C(120) * 1000000000) {
            fprintf(stderr, "Migration heartbeat timeout offset=%zu expected=%" PRIu64
                    " actual=%" PRIu64 "\n", offset, expected, value); exit(1);
        }
        /* Poll cost is reported separately; process_vm_readv avoids a shared-RAM shortcut. */
        if (handoff_poll_us) usleep(handoff_poll_us);
        else __asm__ __volatile__("pause");
    }
}

static int migration_trace_fd = -1, migration_trace_gate = -1;
static void migration_trace(const char *phase, char role, unsigned round)
{
    if (migration_trace_fd < 0) return;
    if (!strcmp(phase, "BEGIN") && pwrite(migration_trace_gate, "1", 1, 0) != 1)
        die("migration EPT tracing enable");
    char marker[160];
    int length = snprintf(marker, sizeof(marker),
        "MIGRATION_EPT_%s iteration=%u role=%c epoch=%u\n", phase, round, role, round + 1);
    if (write(migration_trace_fd, marker, length) != length) die("migration EPT trace marker");
    if (!strcmp(phase, "SCAN2") && pwrite(migration_trace_gate, "0", 1, 0) != 1)
        die("migration EPT tracing disable");
}

static void migration_bench(char role, const char *peer, unsigned port, unsigned rounds,
                              uint64_t nonce, unsigned memory_mib)
{
    bench_transaction_paths();
    if (!getenv("BENCH_RUN_DIR")) {
        fprintf(stderr, "Migration requires a transaction-specific BENCH_RUN_DIR\n"); exit(2);
    }
    bench_private_ram = 1;
    handoff_poll_us = getenv("BENCH_POLL_US") ? strtoul(getenv("BENCH_POLL_US"), NULL, 10) : 50;
    if (prctl(PR_SET_TIMERSLACK, 1UL)) die("migration timer slack");
    struct sigaction action = {.sa_handler = bench_terminate};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL)) die("migration signals");
    atexit(bench_cleanup);
    setvbuf(stdout, NULL, _IONBF, 0);
    if (env_enabled("MIGRATION_EPT_TRACE")) {
        migration_trace_fd = open("/sys/kernel/tracing/trace_marker", O_WRONLY | O_CLOEXEC);
        migration_trace_gate = open("/sys/kernel/tracing/tracing_on", O_WRONLY | O_CLOEXEC);
        if (migration_trace_fd < 0 || migration_trace_gate < 0) die("migration EPT tracing files");
    }
    char incoming_state[192], incoming_memory[192];
    snprintf(incoming_state, sizeof(incoming_state), "%s/migration-in.state", bench_directory);
    snprintf(incoming_memory, sizeof(incoming_memory), "%s/migration-in.mem", bench_directory);
    size_t capacity = 1024 * 1024;
    unsigned char *buffer = malloc(capacity);
    if (!buffer) die("migration transport buffer");
    unsigned me = role == 'A' ? 0 : 1;
    int connection = migration_socket(role, peer, port);
    double startup_us = bench_start(!me, 0);
    uint64_t hva = 0, instance = 0;
    struct migration_heartbeat last = {0};
    if (!me) {
        hva = migration_ram_hva(memory_mib, 0);
        last = migration_wait(hva, offsetof(struct migration_heartbeat, magic), MIGRATION_MAGIC);
        migration_check_heartbeat(&last, 0, 0);
        instance = atomic_load(&last.instance_id);
        printf("MIGRATION_JSON {\"event\":\"boot\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
               "\",\"memory_mib\":%u,\"startup_us\":%.3f,\"working_set_bytes\":%" PRIu64
               ",\"working_set_ratio\":%.6f,\"guest_available_bytes\":%" PRIu64
               ",\"instance_id\":%" PRIu64 "}\n", role, nonce, memory_mib, startup_us,
               atomic_load(&last.working_set_bytes), atomic_load(&last.working_set_bytes) /
               (double)(memory_mib * UINT64_C(1024) * 1024), atomic_load(&last.available_bytes), instance);
    }
    for (unsigned round = 0; round < rounds; round++) {
        if (me == round % 2) {
            struct migration_packet packet;
            migration_receive_packet(connection, &packet, nonce, round, memory_mib, MIG_READY);
            last = migration_wait(hva, offsetof(struct migration_heartbeat, warm_done), round);
            migration_check_heartbeat(&last, instance, round);
            uint64_t start = monotonic_ns();
            double pause_us = bench_api("PATCH", "/vm", "{\"state\":\"Paused\"}");
            last = migration_read(hva);
            uint64_t next_epoch = round + 1;
            migration_process_copy(hva + MIGRATION_GPA + offsetof(struct migration_heartbeat, epoch),
                                   &next_epoch, sizeof(next_epoch), 1);
            char request[768];
            snprintf(request, sizeof(request), "{\"snapshot_type\":\"Full\",\"snapshot_path\":\"%s\","
                "\"mem_file_path\":\"%s\",\"sync_snapshot_files\":false}",
                bench_full_state_path, bench_full_memory_path);
            double snapshot_us = bench_api("PUT", "/snapshot/create", request);
            packet = migration_packet(nonce, round, memory_mib, MIG_PAYLOAD);
            packet.state_bytes = migration_file_size(bench_full_state_path);
            if (!packet.state_bytes || packet.state_bytes > 256 * 1024 ||
                migration_file_size(bench_full_memory_path) != packet.memory_bytes) {
                fprintf(stderr, "Migration Full snapshot size mismatch\n"); exit(3);
            }
            packet.state_hash = migration_state_hash(bench_full_state_path);
            packet.heartbeat = last;
            uint64_t send_start = monotonic_ns();
            migration_io(connection, &packet, sizeof(packet), 1);
            double state_send_us = migration_file(connection, bench_full_state_path, packet.state_bytes,
                                                   1, buffer, capacity);
            double ram_send_us = migration_file(connection, bench_full_memory_path, packet.memory_bytes,
                                                 1, buffer, capacity);
            double wire_send_us = (monotonic_ns() - send_start) / 1000.0;
            uint64_t completion_start = monotonic_ns();
            struct migration_packet response;
            migration_receive_packet(connection, &response, nonce, round, memory_mib, MIG_RECEIVED);
            double receive_completion_wait_us = (monotonic_ns() - completion_start) / 1000.0;
            double pause_to_remote_received_us = (monotonic_ns() - start) / 1000.0;
            uint64_t ack_start = monotonic_ns();
            migration_receive_packet(connection, &response, nonce, round, memory_mib, MIG_ACK);
            double peer_ack_wait_us = (monotonic_ns() - ack_start) / 1000.0;
            double pause_to_ack_us = (monotonic_ns() - start) / 1000.0;
            uint64_t ready_start = monotonic_ns();
            migration_receive_packet(connection, &response, nonce, round, memory_mib, MIG_SCAN_READY);
            double peer_ready_wait_us = (monotonic_ns() - ready_start) / 1000.0;
            double pause_to_ready_us = (monotonic_ns() - start) / 1000.0;
            migration_receive_packet(connection, &response, nonce, round, memory_mib, MIG_WARM_READY);
            last = response.heartbeat;
            migration_check_heartbeat(&last, instance, round + 1);
            if (atomic_load(&last.first_ack) != round + 1 || atomic_load(&last.ready) != round + 1 ||
                atomic_load(&last.warm_done) != round + 1) {
                fprintf(stderr, "Migration destination readiness mismatch\n"); exit(3);
            }
            uint64_t cleanup_start = monotonic_ns();
            bench_cleanup();
            /* The old receiver file cannot be truncated while still mapped by
             * its VMM. Retire it only after waitpid confirms that VMM exited. */
            unlink(incoming_memory);
            unlink(incoming_state);
            unlink(bench_full_memory_path);
            unlink(bench_full_state_path);
            double cleanup_us = (monotonic_ns() - cleanup_start) / 1000.0;
            /* A now-empty API process is prepared outside the measured outage. */
            double empty_startup_us = round + 1 < rounds ? bench_start(0, round + 1) : 0;
            printf("MIGRATION_JSON {\"event\":\"source\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
                "\",\"memory_mib\":%u,\"round\":%u,\"pause_us\":%.3f,\"snapshot_us\":%.3f,"
                "\"state_send_us\":%.3f,\"ram_send_us\":%.3f,\"wire_send_us\":%.3f,"
                "\"receive_completion_wait_us\":%.3f,\"pause_to_remote_received_us\":%.3f,"
                "\"peer_ack_wait_us\":%.3f,\"peer_ready_wait_us\":%.3f,"
                "\"pause_to_peer_ack_us\":%.3f,\"pause_to_peer_ready_us\":%.3f,"
                "\"source_cleanup_us\":%.3f,\"empty_startup_us\":%.3f,"
                "\"state_bytes\":%" PRIu64 ",\"ram_bytes\":%" PRIu64 ",\"wire_payload_bytes\":%" PRIu64
                ",\"working_set_bytes\":%" PRIu64 ",\"sequence\":%" PRIu64
                ",\"guest_clock_ns\":%" PRIu64 ",\"guest_tsc\":%" PRIu64 "}\n",
                role, nonce, memory_mib, round, pause_us, snapshot_us, state_send_us, ram_send_us,
                wire_send_us, receive_completion_wait_us, pause_to_remote_received_us,
                peer_ack_wait_us, peer_ready_wait_us, pause_to_ack_us, pause_to_ready_us,
                cleanup_us, empty_startup_us, packet.state_bytes, packet.memory_bytes,
                packet.state_bytes + packet.memory_bytes, atomic_load(&last.working_set_bytes),
                atomic_load(&last.sequence), atomic_load(&last.clock_ns), atomic_load(&last.tsc));
        } else {
            struct migration_packet packet = migration_packet(nonce, round, memory_mib, MIG_READY);
            migration_io(connection, &packet, sizeof(packet), 1);
            migration_receive_packet(connection, &packet, nonce, round, memory_mib, MIG_PAYLOAD);
            if (!packet.state_bytes || packet.state_bytes > 256 * 1024) {
                fprintf(stderr, "Migration incoming state exceeds limit\n"); exit(3);
            }
            uint64_t receive_start = monotonic_ns();
            double state_receive_us = migration_file(connection, incoming_state, packet.state_bytes,
                                                      0, buffer, capacity);
            double ram_receive_us = migration_file(connection, incoming_memory, packet.memory_bytes,
                                                    0, buffer, capacity);
            if (migration_state_hash(incoming_state) != packet.state_hash) {
                fprintf(stderr, "Migration state hash mismatch\n"); exit(3);
            }
            double receive_us = (monotonic_ns() - receive_start) / 1000.0;
            struct migration_packet received = migration_packet(nonce, round, memory_mib, MIG_RECEIVED);
            migration_io(connection, &received, sizeof(received), 1);
            char request[768];
            snprintf(request, sizeof(request), "{\"snapshot_path\":\"%s\",\"mem_backend\":{"
                "\"backend_type\":\"File\",\"backend_path\":\"%s\"},\"resume_vm\":false}",
                incoming_state, incoming_memory);
            double restore_us = bench_api("PUT", "/snapshot/load", request);
            uint64_t paused_validation_start = monotonic_ns();
            hva = migration_ram_hva(memory_mib, 1);
            last = migration_read(hva);
            migration_check_heartbeat(&last, atomic_load(&packet.heartbeat.instance_id), round + 1);
            instance = atomic_load(&last.instance_id);
            if (atomic_load(&last.sequence) != atomic_load(&packet.heartbeat.sequence)) {
                fprintf(stderr, "Migration RAM sequence changed while paused\n"); exit(3);
            }
            double paused_validation_us = (monotonic_ns() - paused_validation_start) / 1000.0;
            uint64_t resume_start = monotonic_ns();
            migration_trace("BEGIN", role, round);
            double resume_us = bench_api("PATCH", "/vm", "{\"state\":\"Resumed\"}");
            uint64_t ack_wait_start = monotonic_ns();
            last = migration_wait(hva, offsetof(struct migration_heartbeat, first_ack), round + 1);
            double ack_wait_us = (monotonic_ns() - ack_wait_start) / 1000.0;
            double resume_to_ack_us = (monotonic_ns() - resume_start) / 1000.0;
            migration_trace("ACK", role, round);
            struct migration_packet response = migration_packet(nonce, round, memory_mib, MIG_ACK);
            response.heartbeat = last;
            migration_io(connection, &response, sizeof(response), 1);
            uint64_t ready_wait_start = monotonic_ns();
            last = migration_wait(hva, offsetof(struct migration_heartbeat, ready), round + 1);
            double ready_wait_us = (monotonic_ns() - ready_wait_start) / 1000.0;
            double resume_to_ready_us = (monotonic_ns() - resume_start) / 1000.0;
            migration_trace("SCAN1", role, round);
            response.kind = MIG_SCAN_READY;
            response.heartbeat = last;
            migration_io(connection, &response, sizeof(response), 1);
            uint64_t warm_start = monotonic_ns();
            last = migration_wait(hva, offsetof(struct migration_heartbeat, warm_done), round + 1);
            double warm_wait_us = (monotonic_ns() - warm_start) / 1000.0;
            migration_trace("SCAN2", role, round);
            migration_check_heartbeat(&last, instance, round + 1);
            response.kind = MIG_WARM_READY;
            response.heartbeat = last;
            migration_io(connection, &response, sizeof(response), 1);
            struct migration_ram_stats stats = migration_ram_stats(hva);
            printf("MIGRATION_JSON {\"event\":\"destination\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
                "\",\"memory_mib\":%u,\"round\":%u,\"state_receive_us\":%.3f,\"ram_receive_us\":%.3f,"
                "\"receive_us\":%.3f,\"restore_us\":%.3f,\"paused_validation_us\":%.3f,\"resume_us\":%.3f,\"first_ack_wait_us\":%.3f,"
                "\"working_set_ready_wait_us\":%.3f,\"warm_wait_us\":%.3f,\"resume_to_ack_us\":%.3f,"
                "\"resume_to_ready_us\":%.3f,\"guest_first_sweep_ns\":%" PRIu64
                ",\"guest_warm_sweep_ns\":%" PRIu64 ",\"state_bytes\":%" PRIu64
                ",\"ram_bytes\":%" PRIu64 ",\"wire_payload_bytes\":%" PRIu64
                ",\"working_set_bytes\":%" PRIu64 ",\"working_set_ratio\":%.6f,"
                "\"instance_id\":%" PRIu64 ",\"errors\":0,\"clock_errors\":0,\"tsc_errors\":0,"
                "\"ram_hva\":%" PRIu64 ",\"pid\":%d,\"process_start\":%" PRIu64
                ",\"ram_rss_bytes\":%" PRIu64 ",\"ram_private_bytes\":%" PRIu64
                ",\"ram_anonymous_bytes\":%" PRIu64 ",\"ram_anon_huge_bytes\":%" PRIu64 "}\n",
                role, nonce, memory_mib, round, state_receive_us, ram_receive_us, receive_us,
                restore_us, paused_validation_us, resume_us, ack_wait_us, ready_wait_us, warm_wait_us, resume_to_ack_us,
                resume_to_ready_us, atomic_load(&last.first_sweep_ns), atomic_load(&last.warm_sweep_ns),
                packet.state_bytes, packet.memory_bytes, packet.state_bytes + packet.memory_bytes,
                atomic_load(&last.working_set_bytes), atomic_load(&last.working_set_bytes) /
                (double)packet.memory_bytes, instance, hva, bench_child, bench_process_start(bench_child),
                stats.rss_bytes, stats.private_bytes, stats.anonymous_bytes, stats.anon_huge_bytes);
        }
    }
    /* Retire the final receiver too. Leaving its unmapped RAM file in tmpfs
     * would consume M more bytes after every independently completed case. */
    bench_cleanup();
    unlink(incoming_memory);
    unlink(incoming_state);
    unlink(bench_full_memory_path);
    unlink(bench_full_state_path);
    printf("MIGRATION_JSON {\"event\":\"complete\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
        "\",\"memory_mib\":%u,\"rounds\":%u,\"errors\":0,\"clock_errors\":0,\"tsc_errors\":0,"
        "\"last_instance_id\":%" PRIu64 "}\n", role, nonce, memory_mib, rounds, instance);
    if (migration_trace_gate >= 0) {
        if (pwrite(migration_trace_gate, "0", 1, 0) != 1) die("migration trace final disable");
        close(migration_trace_gate);
        close(migration_trace_fd);
    }
    close(connection);
    free(buffer);
}

/* Network-only control: the existing socket transport, with no snapshot/file
 * I/O. One reusable buffer bounds the memory footprint independently of M. */
#define TCP_BENCH_SCHEMA UINT64_C(0x54435042454e4301)
struct tcp_bench_packet {
    uint64_t schema, nonce, kind, round, source, bytes;
};
struct tcp_bench_stats {
    struct tcp_info info;
    int send_buffer, receive_buffer;
};

static struct tcp_bench_stats tcp_bench_stats(int fd)
{
    struct tcp_bench_stats stats = {0};
    socklen_t length = sizeof(stats.info);
    if (getsockopt(fd, IPPROTO_TCP, TCP_INFO, &stats.info, &length) ||
        length < offsetof(struct tcp_info, tcpi_total_retrans) + sizeof(stats.info.tcpi_total_retrans))
        die("TCP benchmark TCP_INFO");
    length = sizeof(stats.send_buffer);
    if (getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &stats.send_buffer, &length)) die("TCP benchmark SO_SNDBUF");
    length = sizeof(stats.receive_buffer);
    if (getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &stats.receive_buffer, &length)) die("TCP benchmark SO_RCVBUF");
    return stats;
}

static void tcp_bench_print_stats(const struct tcp_bench_stats *stats,
                                  const struct tcp_bench_stats *before)
{
    printf(",\"tcp_rtt_us\":%u,\"tcp_rttvar_us\":%u,\"tcp_snd_cwnd\":%u,"
        "\"tcp_snd_ssthresh\":%u,\"tcp_snd_mss\":%u,\"tcp_rcv_mss\":%u,"
        "\"tcp_unacked\":%u,\"tcp_total_retrans\":%u,\"tcp_retrans_delta\":%u,"
        "\"socket_send_buffer_bytes\":%d,\"socket_receive_buffer_bytes\":%d",
        stats->info.tcpi_rtt, stats->info.tcpi_rttvar, stats->info.tcpi_snd_cwnd,
        stats->info.tcpi_snd_ssthresh, stats->info.tcpi_snd_mss, stats->info.tcpi_rcv_mss,
        stats->info.tcpi_unacked, stats->info.tcpi_total_retrans,
        before ? stats->info.tcpi_total_retrans - before->info.tcpi_total_retrans : 0,
        stats->send_buffer, stats->receive_buffer);
}

/* Query enabled NIC features after virtio/backend negotiation. Unsupported
 * legacy ethtool diagnostics are represented explicitly, never as disabled. */
static void tcp_bench_print_interface(int fd)
{
    const char *name = getenv("TCP_BENCH_INTERFACE");
    if (!name) return;
    printf(",\"nic_interface\":");
    bench_print_json_string(name);
    size_t length = strlen(name);
    if (!length || length >= IFNAMSIZ ||
        strspn(name, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-") != length) {
        printf(",\"nic_interface_errno\":%d", EINVAL);
        return;
    }
    struct ifreq request = {0};
    memcpy(request.ifr_name, name, length + 1);
    int result = ioctl(fd, SIOCGIFMTU, &request);
    int error = result < 0 ? errno : 0;
    printf(",\"nic_mtu\":");
    if (error) printf("null");
    else printf("%d", request.ifr_mtu);
    printf(",\"nic_mtu_errno\":%d", error);
    const struct { const char *name; uint32_t command; } features[] = {
        {"tx_csum", ETHTOOL_GTXCSUM}, {"rx_csum", ETHTOOL_GRXCSUM},
        {"sg", ETHTOOL_GSG}, {"tso", ETHTOOL_GTSO},
        {"gso", ETHTOOL_GGSO}, {"gro", ETHTOOL_GGRO},
    };
    for (size_t i = 0; i < sizeof(features) / sizeof(features[0]); i++) {
        struct ethtool_value value = {.cmd = features[i].command};
        request.ifr_data = (void *)&value;
        result = ioctl(fd, SIOCETHTOOL, &request);
        error = result < 0 ? errno : 0;
        printf(",\"nic_%s\":", features[i].name);
        if (error) printf("null");
        else printf("%u", !!value.data);
        printf(",\"nic_%s_errno\":%d", features[i].name, error);
    }
}

static uint64_t tcp_bench_cpu_ns(const struct timeval *after, const struct timeval *before)
{
    return (uint64_t)((int64_t)(after->tv_sec - before->tv_sec) * INT64_C(1000000000) +
                       (int64_t)(after->tv_usec - before->tv_usec) * 1000);
}

static unsigned char tcp_bench_byte(uint64_t nonce, size_t offset)
{
    return (unsigned char)(((nonce >> ((offset % 8) * 8)) ^ (offset * 29u) ^ 0xadu) | 1u);
}

static void tcp_bench_check_sample(const unsigned char *buffer, size_t length, uint64_t nonce)
{
    /* Verify the first/last 16 bytes of EVERY chunk, without a bytewise bulk
     * hash that could become the throughput limit being measured. */
    size_t sample = length < 16 ? length : 16;
    for (size_t i = 0; i < sample; i++) {
        size_t last = length - sample + i;
        if (buffer[i] != tcp_bench_byte(nonce, i) ||
            buffer[last] != tcp_bench_byte(nonce, last)) {
            fprintf(stderr, "TCP benchmark sampled payload verification failed\n"); exit(3);
        }
    }
}

static void tcp_bench_receive_packet(int fd, struct tcp_bench_packet *packet,
                                      const struct tcp_bench_packet *expected)
{
    migration_io(fd, packet, sizeof(*packet), 0);
    if (packet->schema != expected->schema || packet->nonce != expected->nonce ||
        packet->kind != expected->kind || packet->round != expected->round ||
        packet->source != expected->source || packet->bytes != expected->bytes) {
        fprintf(stderr, "TCP benchmark schema/nonce/kind/round/source/bytes mismatch\n"); exit(3);
    }
}

static void tcp_bench(char role, const char *peer, unsigned port, uint64_t bytes,
                        unsigned rounds, uint64_t nonce)
{
    if (getenv("BENCH_RUN_DIR")) bench_transaction_paths();
    setvbuf(stdout, NULL, _IONBF, 0);
    const size_t capacity = 1024 * 1024;
    unsigned char *buffer = malloc(capacity);
    if (!buffer) die("TCP benchmark buffer");
    for (size_t i = 0; i < capacity; i++) buffer[i] = tcp_bench_byte(nonce, i);
    int fd = migration_socket(role, peer, port);
    cpu_set_t affinity;
    if (sched_getaffinity(0, sizeof(affinity), &affinity)) die("TCP benchmark affinity");
    struct tcp_bench_stats initial = tcp_bench_stats(fd);
    printf("TCP_BENCH_JSON {\"event\":\"start\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
        "\",\"bytes\":%" PRIu64 ",\"rounds\":%u,\"chunk_bytes\":%zu,\"pid\":%d,"
        "\"process_start\":%" PRIu64 ",\"current_cpu\":%d,\"tcp_nodelay\":true,\"affinity\":[",
        role, nonce, bytes, rounds, capacity, getpid(), bench_process_start(getpid()), sched_getcpu());
    int first = 1;
    for (unsigned cpu = 0; cpu < CPU_SETSIZE; cpu++) {
        if (!CPU_ISSET(cpu, &affinity)) continue;
        printf("%s%u", first ? "" : ",", cpu);
        first = 0;
    }
    printf("]");
    tcp_bench_print_stats(&initial, NULL);
    tcp_bench_print_interface(fd);
    printf("}\n");
    unsigned me = role == 'A' ? 0 : 1;
    uint64_t total_sent = 0, total_received = 0;
    for (unsigned round = 0; round < rounds; round++) {
        struct tcp_bench_packet expected = {.schema = TCP_BENCH_SCHEMA, .nonce = nonce,
            .kind = 1, .round = round, .source = round % 2, .bytes = bytes};
        struct tcp_bench_packet packet;
        if (me == round % 2) {
            uint64_t ready_start = monotonic_ns();
            tcp_bench_receive_packet(fd, &packet, &expected);
            uint64_t ready_wait = monotonic_ns() - ready_start;
            struct tcp_bench_stats before = tcp_bench_stats(fd);
            struct rusage cpu_before, cpu_sent, cpu_acked;
            if (getrusage(RUSAGE_SELF, &cpu_before)) die("TCP benchmark source rusage");
            packet = expected;
            packet.kind = 2;
            migration_io(fd, &packet, sizeof(packet), 1);
            uint64_t start = monotonic_ns();
            for (uint64_t done = 0; done < bytes;) {
                size_t chunk = bytes - done > capacity ? capacity : (size_t)(bytes - done);
                migration_io(fd, buffer, chunk, 1);
                done += chunk;
            }
            uint64_t sent = monotonic_ns();
            if (getrusage(RUSAGE_SELF, &cpu_sent)) die("TCP benchmark sent rusage");
            expected.kind = 3;
            uint64_t ack_start = monotonic_ns();
            tcp_bench_receive_packet(fd, &packet, &expected);
            uint64_t acked = monotonic_ns();
            if (getrusage(RUSAGE_SELF, &cpu_acked)) die("TCP benchmark acknowledged rusage");
            struct tcp_bench_stats after = tcp_bench_stats(fd);
            total_sent += bytes;
            printf("TCP_BENCH_JSON {\"event\":\"source\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
                "\",\"round\":%u,\"bytes\":%" PRIu64 ",\"payload_tx_ns\":%" PRIu64
                ",\"receipt_wait_ns\":%" PRIu64 ",\"confirmed_ns\":%" PRIu64
                ",\"ready_wait_ns\":%" PRIu64 ",\"tx_user_ns\":%" PRIu64 ",\"tx_system_ns\":%" PRIu64
                ",\"ack_user_ns\":%" PRIu64 ",\"ack_system_ns\":%" PRIu64 ",\"current_cpu\":%d",
                role, nonce, round, bytes, sent - start, acked - ack_start, acked - start, ready_wait,
                tcp_bench_cpu_ns(&cpu_sent.ru_utime, &cpu_before.ru_utime),
                tcp_bench_cpu_ns(&cpu_sent.ru_stime, &cpu_before.ru_stime),
                tcp_bench_cpu_ns(&cpu_acked.ru_utime, &cpu_sent.ru_utime),
                tcp_bench_cpu_ns(&cpu_acked.ru_stime, &cpu_sent.ru_stime), sched_getcpu());
            tcp_bench_print_stats(&after, &before);
            printf("}\n");
        } else {
            packet = expected;
            migration_io(fd, &packet, sizeof(packet), 1);
            expected.kind = 2;
            tcp_bench_receive_packet(fd, &packet, &expected);
            struct tcp_bench_stats before = tcp_bench_stats(fd);
            struct rusage cpu_before, cpu_received;
            if (getrusage(RUSAGE_SELF, &cpu_before)) die("TCP benchmark receiver rusage");
            uint64_t start = monotonic_ns();
            uint64_t chunks = 0;
            for (uint64_t done = 0; done < bytes;) {
                size_t chunk = bytes - done > capacity ? capacity : (size_t)(bytes - done);
                migration_io(fd, buffer, chunk, 0);
                tcp_bench_check_sample(buffer, chunk, nonce);
                done += chunk;
                chunks++;
            }
            uint64_t received = monotonic_ns();
            if (getrusage(RUSAGE_SELF, &cpu_received)) die("TCP benchmark received rusage");
            packet.kind = 3;
            uint64_t ack_start = monotonic_ns();
            migration_io(fd, &packet, sizeof(packet), 1);
            uint64_t ack_sent = monotonic_ns();
            struct tcp_bench_stats after = tcp_bench_stats(fd);
            total_received += bytes;
            printf("TCP_BENCH_JSON {\"event\":\"destination\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
                "\",\"round\":%u,\"bytes\":%" PRIu64 ",\"payload_rx_ns\":%" PRIu64
                ",\"receipt_send_ns\":%" PRIu64 ",\"rx_user_ns\":%" PRIu64 ",\"rx_system_ns\":%" PRIu64
                ",\"verified_chunks\":%" PRIu64 ",\"verification_comparisons_per_full_chunk\":32,"
                "\"errors\":0,\"current_cpu\":%d", role, nonce, round, bytes, received - start,
                ack_sent - ack_start, tcp_bench_cpu_ns(&cpu_received.ru_utime, &cpu_before.ru_utime),
                tcp_bench_cpu_ns(&cpu_received.ru_stime, &cpu_before.ru_stime), chunks, sched_getcpu());
            tcp_bench_print_stats(&after, &before);
            printf("}\n");
        }
    }
    printf("TCP_BENCH_JSON {\"event\":\"complete\",\"role\":\"%c\",\"nonce\":\"%016" PRIx64
        "\",\"rounds\":%u,\"bytes_per_round\":%" PRIu64 ",\"total_sent_bytes\":%" PRIu64
        ",\"total_received_bytes\":%" PRIu64 ",\"errors\":0}\n",
        role, nonce, rounds, bytes, total_sent, total_received);
    close(fd);
    free(buffer);
}

#include "cache_probe.h"

/* Blocking futex ping-pong on one CPU: two handoffs per round trip.
 * The time includes futex wait/wake, scheduler and userspace loop overhead;
 * it is not a measurement of __switch_to's instructions in isolation. */
struct context_control {
    _Atomic int turn;
    unsigned warmup, rounds;
    int private_futex;
    struct rusage child_start, child_end;
};

static void context_wait(struct context_control *c, int wanted)
{
    while (atomic_load_explicit(&c->turn, memory_order_acquire) != wanted) {
        long rc = syscall(SYS_futex, &c->turn,
            FUTEX_WAIT | (c->private_futex ? FUTEX_PRIVATE_FLAG : 0), 1 - wanted, NULL);
        if (rc < 0 && errno != EAGAIN && errno != EINTR) die("futex wait");
    }
}

static void context_signal(struct context_control *c, int turn)
{
    atomic_store_explicit(&c->turn, turn, memory_order_release);
    if (syscall(SYS_futex, &c->turn,
        FUTEX_WAKE | (c->private_futex ? FUTEX_PRIVATE_FLAG : 0), 1) < 0) die("futex wake");
}

static uint64_t context_now(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &t)) die("context clock");
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}

static void *context_worker(void *argument)
{
    struct context_control *c = argument;
    for (unsigned i = 0; i < c->warmup + c->rounds; i++) {
        if (i == c->warmup && getrusage(RUSAGE_THREAD, &c->child_start)) die("child rusage start");
        context_wait(c, 1);
        context_signal(c, 0);
    }
    if (getrusage(RUSAGE_THREAD, &c->child_end)) die("child rusage end");
    return NULL;
}

/* Same physical DAX VM/vCPU/VMCS across two kernels. No snapshot or SET_REGS
 * after the initial boot. HLT gives a completed-instruction handoff boundary. */
struct direct_control {
    _Atomic uint64_t nonce, ready[2], published, acknowledged;
    struct kvm_dax_handoff token;
    uint64_t counter;
    _Atomic uint64_t preparing;
    _Atomic uint64_t dense_observed;
};

static void direct_interrupt(int signal_number) { (void)signal_number; }

static off_t direct_layout_offset(unsigned mib)
{
    const char *value=getenv("LAB_DAX_LAYOUT_SHIFT_MIB");
    char *end=NULL;
    errno=0;
    uint64_t shift=value?strtoull(value,&end,10):0;
    if(value && (!*value || *value=='-' || !end || *end || errno ||
                 shift>(uint64_t)(INT64_MAX>>20)-mib)){
        fprintf(stderr,"Invalid direct DAX layout shift\n");exit(1);
    }
    return (off_t)((shift+mib)*UINT64_C(1024)*1024);
}

static void direct_telemetry(char role,int dax)
{
    handoff_poll_us=getenv("LAB_DAX_POLL_US")?strtoul(getenv("LAB_DAX_POLL_US"),NULL,10):50;
    handoff_spin_us=getenv("LAB_DAX_SPIN_US")?strtoul(getenv("LAB_DAX_SPIN_US"),NULL,10):0;
    if(getenv("LAB_DAX_TIMER_SLACK_NS")){
        unsigned long slack=strtoul(getenv("LAB_DAX_TIMER_SLACK_NS"),NULL,10);
        if(slack && prctl(PR_SET_TIMERSLACK,slack,0,0,0))die("Controller timer slack");
    }
    if(!getenv("LAB_DAX_TELEMETRY"))return;
    size_t bytes=64*1024*1024;
    off_t offset=direct_layout_offset(768+(role=='B'?64:0));
    if((uint64_t)offset+bytes>dax_attribute("/dev/dax0.0","size")){
        fprintf(stderr,"Direct telemetry exceeds DAX capacity\n");exit(1);
    }
    char *data=mmap(NULL,bytes,PROT_READ|PROT_WRITE,MAP_SHARED,dax,offset);
    if(data==MAP_FAILED)die("Direct DAX telemetry mmap");
    stdout=fmemopen(data,bytes,"w");
    if(!stdout)die("Direct DAX telemetry stream");
    setvbuf(stdout,NULL,_IONBF,0);
}

static void direct_revoked(int vcpu)
{
    struct kvm_regs regs;
    if(ioctl(vcpu,KVM_GET_REGS,&regs)!=-1 || errno!=EIO){
        fprintf(stderr,"Detached vCPU FD still accessible\n");exit(1);
    }
}

static void direct_duplicate(int kvm,struct kvm_dax_handoff *token)
{
    int fd=ioctl(kvm,KVM_DAX_ATTACH,token);
    if(fd>=0 || errno!=EBUSY){
        if(fd>=0)close(fd);
        fprintf(stderr,"Duplicate attach was not rejected\n");exit(1);
    }
}

static unsigned direct_step(int vcpu, struct kvm_run *run, unsigned char *ram)
{
    struct itimerval timer = {.it_value.tv_sec=2};
    if(setitimer(ITIMER_REAL,&timer,NULL))die("direct watchdog");
    int result=ioctl(vcpu, KVM_RUN, 0), saved_errno=errno;
    memset(&timer,0,sizeof(timer));setitimer(ITIMER_REAL,&timer,NULL);
    if (result < 0) {
        struct kvm_regs diagnostic;
        if(!ioctl(vcpu,KVM_GET_REGS,&diagnostic))
            fprintf(stderr,"Direct interrupted rip=%" PRIx64 " rax=%" PRIu64 "\n",(uint64_t)diagnostic.rip,(uint64_t)diagnostic.rax);
        errno=saved_errno;die("direct KVM_RUN");
    }
    if (run->exit_reason != KVM_EXIT_HLT) {
        fprintf(stderr, "Unexpected direct guest exit=%u fail=%" PRIu64 "\n",
                run->exit_reason, (uint64_t)run->fail_entry.hardware_entry_failure_reason);
        exit(1);
    }
    struct kvm_regs regs;
    if (ioctl(vcpu, KVM_GET_REGS, &regs)) die("direct GET_REGS");
    unsigned counter;
    double fp;
    memcpy(&counter, ram + 0x2000, sizeof(counter));
    memcpy(&fp, ram + 0x2010, sizeof(fp));
    if (regs.rax != counter || regs.rbx != UINT64_C(0xc0dec0de) ||
        regs.rip != 0x1013 || fp != counter) {
        fprintf(stderr, "Direct state mismatch eax=%" PRIu64 " count=%u rbx=%" PRIx64 " rip=%" PRIx64 " fp=%.1f\n",
                (uint64_t)regs.rax, counter, (uint64_t)regs.rbx, (uint64_t)regs.rip, fp);
        exit(1);
    }
    return counter;
}

static void direct_handoff(char role, unsigned rounds, uint64_t nonce)
{
    cpu_set_t mask;
    CPU_ZERO(&mask); CPU_SET(0, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) die("direct affinity");
    alarm(180);
    struct sigaction action = {.sa_handler=direct_interrupt};
    sigemptyset(&action.sa_mask);
    if(sigaction(SIGALRM,&action,NULL))die("direct watchdog handler");
    setvbuf(stdout, NULL, _IONBF, 0);
    int dax = open_dax("/dev/dax0.0"), kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    direct_telemetry(role,dax);
    if (kvm < 0) die("direct /dev/kvm");
    unsigned char *shared = mmap(NULL, 2 * 1024 * 1024, PROT_READ | PROT_WRITE, MAP_SHARED, dax, 0);
    unsigned char *ram = mmap((void *)UINT64_C(0x4000000000), 2 * 1024 * 1024,
        PROT_READ | PROT_WRITE, MAP_SHARED | MAP_FIXED_NOREPLACE, dax, 2 * 1024 * 1024);
    if (shared == MAP_FAILED || ram == MAP_FAILED) die("direct DAX map");
    close(dax);
    struct direct_control *ctrl = (void *)(shared + 1024 * 1024);
    unsigned me = role == 'A' ? 0 : 1;
    int foreign_test=env_enabled("LAB_DAX_NEW_PFN_TEST");
    void *foreign=NULL;
    if(foreign_test){
        foreign=mmap((void *)UINT64_C(0x6000000000),4096,PROT_READ|PROT_WRITE,
            MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED_NOREPLACE,-1,0);
        if(foreign==MAP_FAILED)die("negative native page map");
        memset(foreign,0xa5,4096);
    }
    int vm = -1, vcpu = -1;
    int run_size = ioctl(kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    struct kvm_run *run = MAP_FAILED;
    unsigned counter = 0;
    struct kvm_dax_handoff identity = {0};
    if (!me) {
        memset(ctrl, 0, sizeof(*ctrl));
        memset(ram, 0, 2 * 1024 * 1024);
        /* fninit; fldz; loop: inc eax; store eax; fld1; faddp; fst f64; hlt; jmp loop */
        const unsigned char code[] = {0xdb,0xe3,0xd9,0xee,0x66,0x40,0x66,0xa3,0x00,0x20,
            0xd9,0xe8,0xde,0xc1,0xdd,0x16,0x10,0x20,0xf4,0xeb,0xef};
        memcpy(ram + 0x1000, code, sizeof(code));
        if(foreign_test){
            /* On the first B entry, access GPA 64KiB through DS=0x1000.
             * The preceding 101 HLT steps touch only the shared RAM slot. */
            const unsigned char tail[]={0x66,0x83,0xf8,101,0x75,0xeb,
                0xb8,0x00,0x10,0x8e,0xd8,0xa0,0x00,0x00,0xf4};
            memcpy(ram+0x1013,tail,sizeof(tail));
        }
        vm = ioctl(kvm, KVM_CREATE_VM, 0);
        if (vm < 0) die("direct initial KVM_CREATE_VM");
        struct kvm_userspace_memory_region slot = {.slot=0, .guest_phys_addr=0,
            .memory_size=foreign_test?65536:2*1024*1024, .userspace_addr=(uintptr_t)ram};
        if (ioctl(vm, KVM_SET_USER_MEMORY_REGION, &slot)) die("direct memory slot");
        if(foreign_test){
            struct kvm_userspace_memory_region bad={.slot=1,.guest_phys_addr=65536,
                .memory_size=4096,.userspace_addr=(uintptr_t)foreign};
            if(ioctl(vm,KVM_SET_USER_MEMORY_REGION,&bad))die("negative native RAM slot");
        }
        vcpu = ioctl(vm, KVM_CREATE_VCPU, 0);
        if (vcpu < 0) die("direct initial KVM_CREATE_VCPU");
        run = mmap(NULL, run_size, PROT_READ | PROT_WRITE, MAP_SHARED, vcpu, 0);
        if (run == MAP_FAILED) die("direct run mmap");
        struct kvm_sregs sr;
        if (ioctl(vcpu, KVM_GET_SREGS, &sr)) die("direct get sregs");
        sr.cs.base=sr.ds.base=sr.es.base=sr.ss.base=0;
        sr.cs.selector=sr.ds.selector=sr.es.selector=sr.ss.selector=0;
        if (ioctl(vcpu, KVM_SET_SREGS, &sr)) die("direct initial set sregs");
        struct kvm_regs regs = {.rip=0x1000, .rflags=2, .rbx=UINT64_C(0xc0dec0de)};
        if (ioctl(vcpu, KVM_SET_REGS, &regs)) die("direct initial set regs");
        counter = direct_step(vcpu, run, ram);
        if (ioctl(vm, KVM_DAX_EXPORT, &identity)) die("direct EXPORT");
        ctrl->token = identity;
        atomic_store_explicit(&ctrl->nonce, nonce, memory_order_release);
    } else {
        wait_value(&ctrl->nonce, nonce, "direct nonce");
        identity = ctrl->token;
    }
    atomic_store_explicit(&ctrl->ready[me], 1, memory_order_release);
    wait_value(&ctrl->ready[1-me], 1, "direct peer ready");
    for (unsigned r=0; r<rounds; r++) {
        if (me == r%2) {
            for (unsigned i=0; i<100; i++) counter=direct_step(vcpu,run,ram);
            struct kvm_dax_handoff token;
            double start=monotonic();
            if (ioctl(vm,KVM_DAX_DETACH,&token)) die("direct DETACH");
            double detached=monotonic();
            direct_revoked(vcpu);
            ctrl->token=token;
            ctrl->counter=counter;
            atomic_store_explicit(&ctrl->published,r+1,memory_order_release);
            wait_value(&ctrl->acknowledged,r+1,"direct remote guest");
            double elapsed=(monotonic()-start)*1e6;
            printf("DIRECT_JSON {\"event\":\"source\",\"round\":%u,\"role\":\"%c\",\"detach_us\":%.3f,\"kernel_detach_us\":%.3f,\"handoff_us\":%.3f,\"counter\":%u,\"vm_offset\":%" PRIu64 ",\"vcpu_offset\":%" PRIu64 ",\"vmcs_offset\":%" PRIu64 ",\"pool_used\":%" PRIu64 ",\"ept_root_offset\":%" PRIu64 ",\"ept_page_count\":%" PRIu64 "}\n",
                r,role,(detached-start)*1e6,(double)token.kernel_ns/1000,elapsed,counter,
                (uint64_t)token.vm_offset,(uint64_t)token.vcpu_offset,(uint64_t)token.vmcs_offset,(uint64_t)token.pool_used,(uint64_t)token.ept_root_offset,(uint64_t)token.ept_page_count);
            munmap(run,run_size);close(vcpu);close(vm);vm=vcpu=-1;
        } else {
            wait_value(&ctrl->published,r+1,"direct source detached");
            struct kvm_dax_handoff token=ctrl->token;
            double start=monotonic();
            vm=ioctl(kvm,KVM_DAX_ATTACH,&token);
            if (vm<0) die("direct ATTACH");
            if (token.vm_creates!=1 || token.vcpu_creates!=1) {
                fprintf(stderr,"Direct VM/vCPU was recreated\n");exit(1);
            }
            double attached=monotonic();
            direct_duplicate(kvm,&token);
            if (token.vm_offset!=identity.vm_offset || token.vcpu_offset!=identity.vcpu_offset ||
                token.run_offset!=identity.run_offset || token.vmcs_offset!=ctrl->token.vmcs_offset) {
                fprintf(stderr,"Direct KVM object identity changed\n");exit(1);
            }
            vcpu=ioctl(vm,KVM_DAX_VCPU,0);
            if(vcpu<0)die("direct existing vCPU FD");
            run=mmap(NULL,run_size,PROT_READ|PROT_WRITE,MAP_SHARED,vcpu,0);
            if(run==MAP_FAILED)die("direct attached run mmap");
            counter=direct_step(vcpu,run,ram);
            if(counter!=ctrl->counter+1){fprintf(stderr,"Direct continuation counter mismatch\n");exit(1);}
            double resumed=monotonic();
            atomic_store_explicit(&ctrl->acknowledged,r+1,memory_order_release);
            printf("DIRECT_JSON {\"event\":\"destination\",\"round\":%u,\"role\":\"%c\",\"attach_us\":%.3f,\"kernel_attach_us\":%.3f,\"run_to_hlt_us\":%.3f,\"counter\":%u,\"generation\":%" PRIu64 ",\"vm_creates\":1,\"vcpu_creates\":1}\n",
                r,role,(attached-start)*1e6,(double)token.kernel_ns/1000,(resumed-attached)*1e6,counter,(uint64_t)token.generation);
        }
    }
    printf("DIRECT_JSON {\"event\":\"complete\",\"role\":\"%c\",\"rounds\":%u,\"result\":\"passed\"}\n",role,rounds);
    alarm(0);
    /* Shared graph is pinned until both outer kernels are shut down. */
    if(vcpu>=0){munmap(run,run_size);close(vcpu);close(vm);}
    munmap(ram,2*1024*1024);munmap(shared,2*1024*1024);close(kvm);
}

struct linux_executor {
    _Atomic int go, stop, stopped, error;
    _Atomic uint64_t wake_request_ns, wake_observed_ns, run_begin_ns, stopped_ns;
    uint64_t stop_request_ns, stop_return_ns;
    int vcpu, console;
    struct kvm_run *run;
};

static void *linux_execute(void *argument)
{
    struct linux_executor *e=argument;
    if(getenv("LAB_DAX_EXECUTOR_CPU")){
        unsigned cpu=strtoul(getenv("LAB_DAX_EXECUTOR_CPU"),NULL,10);
        if(cpu>=CPU_SETSIZE){fprintf(stderr,"Invalid executor CPU\n");exit(1);}
        cpu_set_t mask;CPU_ZERO(&mask);CPU_SET(cpu,&mask);
        if(sched_setaffinity(0,sizeof(mask),&mask))die("Executor CPU affinity");
    }
    for (;;) {
        while(!atomic_load_explicit(&e->go,memory_order_acquire))
            syscall(SYS_futex,&e->go,FUTEX_WAIT|FUTEX_PRIVATE_FLAG,0,NULL);
        if(atomic_load(&e->go)==2)return NULL;
        atomic_store(&e->wake_observed_ns,context_now());
        int pending=0;
        int first=1;
        for (;;) {
            if(atomic_load(&e->stop) && !pending)break;
            if(first){atomic_store(&e->run_begin_ns,context_now());first=0;}
            int rc=ioctl(e->vcpu,KVM_RUN,0);
            pending=0;
            if(rc<0){if(errno==EINTR)continue;atomic_store(&e->error,errno);break;}
            if(e->run->exit_reason==KVM_EXIT_IO){
                unsigned char *data=(void *)((unsigned char *)e->run+e->run->io.data_offset);
                size_t count=e->run->io.size*e->run->io.count;
                if(e->run->io.direction==KVM_EXIT_IO_IN)
                    memset(data,e->run->io.port==0x3fd?0x60:(e->run->io.port==0x3fa?1:0),count);
                else if(e->run->io.port==0x3f8 && write(e->console,data,count)<0){atomic_store(&e->error,errno);break;}
                pending=1;
            } else if(e->run->exit_reason!=KVM_EXIT_HLT){
                fprintf(stderr,"Linux direct unexpected exit=%u hw=%" PRIu64 "\n",e->run->exit_reason,(uint64_t)e->run->fail_entry.hardware_entry_failure_reason);
                atomic_store(&e->error,EIO);break;
            }
        }
        atomic_store(&e->go,0);
        atomic_store(&e->stopped_ns,context_now());
        atomic_store_explicit(&e->stopped,1,memory_order_release);
        syscall(SYS_futex,&e->stopped,FUTEX_WAKE|FUTEX_PRIVATE_FLAG,1);
    }
}

static void linux_wait(_Atomic uint64_t *value,uint64_t expected,struct linux_executor *e)
{
    double until=monotonic()+30;
    uint64_t spin_until=context_now()+(uint64_t)handoff_spin_us*1000;
    while(atomic_load_explicit(value,memory_order_acquire)!=expected){
        if(atomic_load(&e->error)){errno=atomic_load(&e->error);die("Linux direct executor");}
        if(monotonic()>until){fprintf(stderr,"Linux direct heartbeat timeout\n");exit(1);}
        if(handoff_spin_us && context_now()<spin_until)__asm__ __volatile__("pause");
        else handoff_poll();
    }
}

static void linux_stop_executor(struct linux_executor *e,pthread_t thread)
{
    e->stop_request_ns=context_now();
    atomic_store(&e->stop,1);
    __atomic_store_n(&e->run->immediate_exit,1,__ATOMIC_RELEASE);
    int rc=pthread_kill(thread,SIGUSR1);
    if(rc){errno=rc;die("Linux direct kick");}
    double until=monotonic()+5;
    unsigned spin_us=getenv("LAB_DAX_STOP_SPIN_US")?strtoul(getenv("LAB_DAX_STOP_SPIN_US"),NULL,10):0;
    uint64_t spin_until=context_now()+(uint64_t)spin_us*1000;
    while(!atomic_load_explicit(&e->stopped,memory_order_acquire)){
        if(monotonic()>until){fprintf(stderr,"Linux direct stop timeout\n");exit(1);}
        if(spin_us && context_now()<spin_until){__asm__ __volatile__("pause");continue;}
        struct timespec timeout={.tv_sec=1};
        syscall(SYS_futex,&e->stopped,FUTEX_WAIT|FUTEX_PRIVATE_FLAG,0,&timeout);
    }
    e->stop_return_ns=context_now();
    if(atomic_load(&e->error)){errno=atomic_load(&e->error);die("Linux direct run error");}
}

static void linux_wait_published(struct direct_control *ctrl,uint64_t expected)
{
    double until=monotonic()+60;
    uint64_t spin_until=0;
    int armed=0;
    while(atomic_load_explicit(&ctrl->published,memory_order_acquire)!=expected){
        if(monotonic()>until){fprintf(stderr,"Linux direct publish timeout\n");exit(1);}
        if(!armed && atomic_load_explicit(&ctrl->preparing,memory_order_acquire)==expected){
            armed=1;spin_until=context_now()+(uint64_t)handoff_spin_us*1000;
        }
        if(handoff_spin_us && context_now()<spin_until)__asm__ __volatile__("pause");
        else handoff_poll();
    }
}

static void linux_handoff(char role,unsigned rounds,uint64_t nonce)
{
    double controller_start=monotonic();
    struct rusage cpu_before,cpu_after;
    if(getrusage(RUSAGE_THREAD,&cpu_before))die("controller CPU accounting");
    unsigned dwell_us=getenv("LAB_DAX_DWELL_US")?strtoul(getenv("LAB_DAX_DWELL_US"),NULL,10):20000;
    int dense=env_enabled("LAB_HEARTBEAT_DENSE");
    /* Match the host timeout allowance for heap verification between switches. */
    setvbuf(stdout,NULL,_IONBF,0);alarm(180+(uint64_t)rounds*((uint64_t)dwell_us+(dense?15000000:50000))/1000000);
    struct sigaction action={.sa_handler=direct_interrupt};sigemptyset(&action.sa_mask);
    if(sigaction(SIGUSR1,&action,NULL))die("Linux direct signal");
    int dax=open_dax("/dev/dax0.0"),kvm=open("/dev/kvm",O_RDWR|O_CLOEXEC);
    direct_telemetry(role,dax);
    if(kvm<0)die("Linux direct /dev/kvm");
    unsigned char *mapping=mmap(NULL,2*1024*1024,PROT_READ|PROT_WRITE,MAP_SHARED,dax,0);
    struct heartbeat *hb=mmap(NULL,2*1024*1024,PROT_READ|PROT_WRITE,MAP_SHARED,dax,66*1024*1024);
    if(mapping==MAP_FAILED || hb==MAP_FAILED)die("Linux direct control map");
    struct direct_control *ctrl=(void *)(mapping+1024*1024);
    struct kvm_dax_profile *profile=MAP_FAILED;
    struct kvm_dax_resume_profile *resume_profile=MAP_FAILED;
    if(getenv("LAB_DAX_PROFILE")){
        void *page=mmap(NULL,4096,PROT_READ,MAP_SHARED,dax,direct_layout_offset(256));
        if(page==MAP_FAILED)die("Linux direct profile map");
        profile=(void *)((char *)page+KVM_DAX_PROFILE_OFFSET);
    }
    if(getenv("LAB_DAX_RESUME_PROFILE")){
        void *page=mmap(NULL,4096,PROT_READ,MAP_SHARED,dax,direct_layout_offset(256));
        if(page==MAP_FAILED)die("Linux direct resume profile map");
        resume_profile=(void *)((char *)page+KVM_DAX_RESUME_PROFILE_OFFSET);
    }
    unsigned me=role=='A'?0:1;
    int native_a=env_enabled("LAB_DAX_NATIVE_A");
    struct linux_executor executor={0};pthread_t thread;
    int vm=-1,vcpu=-1,run_size=ioctl(kvm,KVM_GET_VCPU_MMAP_SIZE,0);
    struct kvm_run *run=MAP_FAILED;
    unsigned char *ram=MAP_FAILED;
    if(!me){
        atexit(bench_cleanup);memset(ctrl,0,sizeof(*ctrl));atomic_store(&hb->magic,0);
        bench_start(1,0);wait_value(&hb->magic,HB_MAGIC,"Linux direct boot");
        atomic_store_explicit(&ctrl->nonce,nonce,memory_order_release);
    }
    if(me || native_a){
        executor.console=open("/run/direct-linux-console.log",O_WRONLY|O_CREAT|O_TRUNC,0600);
        if(executor.console<0)die("Linux direct console");
        int rc=pthread_create(&thread,NULL,linux_execute,&executor);
        if(rc){errno=rc;die("Linux direct executor thread");}
    }
    if(me)wait_value(&ctrl->nonce,nonce,"Linux direct nonce");
    atomic_store_explicit(&ctrl->ready[me],1,memory_order_release);
    wait_value(&ctrl->ready[1-me],1,"Linux direct peer ready");
    for(unsigned r=0;r<rounds;r++){
        if(me==r%2){
            linux_wait(&hb->verified,r,&executor);
            if(dense)linux_wait(&hb->warm_done,r,&executor);
            usleep(dwell_us);
            double start=monotonic(),pause_us=0;
            atomic_store_explicit(&ctrl->preparing,r+1,memory_order_release);
            struct kvm_dax_handoff token={0};
            if(!me && (!native_a || !r)){
                pause_us=bench_control(0);
                if(ioctl(kvm,KVM_DAX_PEEK,&token))die("Linux direct metadata");
            }else{
                linux_stop_executor(&executor,thread);
                pause_us=(monotonic()-start)*1e6;
                if(ioctl(vm,KVM_DAX_DETACH,&token))die("Linux direct DETACH");
                direct_revoked(vcpu);
            }
            double detached=monotonic();
            ctrl->token=token;ctrl->counter=atomic_load(&hb->sequence);
            atomic_store_explicit(&hb->epoch,r+1,memory_order_release);
            atomic_store_explicit(&ctrl->published,r+1,memory_order_release);
            wait_value(&hb->acknowledged,r+1,"Linux direct guest heartbeat");
            double guest_observed=monotonic();
            wait_value(&ctrl->acknowledged,r+1,"Linux direct remote acknowledgement");
            double handoff_us=(monotonic()-start)*1e6;
            double working_set_ready_us=0,warm_ready_us=0;
            if(dense){
                linux_wait(&hb->verified,r+1,&executor);
                working_set_ready_us=(monotonic()-start)*1e6;
                linux_wait(&hb->warm_done,r+1,&executor);
                warm_ready_us=(monotonic()-start)*1e6;
                if(atomic_load(&hb->errors) || atomic_load(&hb->clock_errors) || atomic_load(&hb->tsc_errors)){
                    fprintf(stderr,"Linux direct dense continuity failed\n");exit(1);
                }
            }
            printf("LINUX_DIRECT_JSON {\"event\":\"source\",\"round\":%u,\"role\":\"%c\",\"pause_us\":%.3f,\"guest_observed_us\":%.3f,\"detach_total_us\":%.3f,\"kernel_detach_us\":%.3f,\"handoff_us\":%.3f,\"sequence\":%" PRIu64 ",\"vm_offset\":%" PRIu64 ",\"vcpu_offset\":%" PRIu64 ",\"vmcs_offset\":%" PRIu64 ",\"vm_creates\":%" PRIu64 ",\"vcpu_creates\":%" PRIu64 ",\"quiesce_us\":%.3f,\"mmu_drain_us\":%.3f,\"binding_us\":%.3f,\"pool_used\":%" PRIu64 ",\"ept_root_offset\":%" PRIu64 ",\"ept_page_count\":%" PRIu64 ",\"source_control_service_us\":%.3f,\"kick_to_exit_us\":%.3f,\"stop_ack_us\":%.3f",
                r,role,pause_us,(guest_observed-start)*1e6,(detached-start)*1e6,(double)token.kernel_ns/1000,handoff_us,(uint64_t)ctrl->counter,
                (uint64_t)token.vm_offset,(uint64_t)token.vcpu_offset,(uint64_t)token.vmcs_offset,(uint64_t)token.vm_creates,(uint64_t)token.vcpu_creates,
                (double)token.quiesce_ns/1000,(double)token.mmu_drain_ns/1000,(double)token.binding_ns/1000,(uint64_t)token.pool_used,(uint64_t)token.ept_root_offset,(uint64_t)token.ept_page_count,
                (!me && !native_a)?bench_control_service_us:0,
                (me || (native_a && r))?(atomic_load(&executor.stopped_ns)-executor.stop_request_ns)/1000.0:0,
                (me || (native_a && r))?(executor.stop_return_ns-atomic_load(&executor.stopped_ns))/1000.0:0);
            if(profile!=MAP_FAILED)for(unsigned i=0;i<8;i++)printf(",\"detach_stage_%u_us\":%.3f",i,profile->detach[i]/1000.0);
            if(!me && !native_a)for(unsigned i=0;i<3;i++)printf(",\"source_control_part_%u_us\":%.3f",i,bench_control_parts_us[i]);
            if(dense)printf(",\"pause_to_working_set_ready_us\":%.3f,\"pause_to_warm_ready_us\":%.3f",
                            working_set_ready_us,warm_ready_us);
            puts("}");
            /* The next source must not advance the epoch before these samples
             * have been consumed. This guard is outside the reported timings. */
            if(dense)atomic_store_explicit(&ctrl->dense_observed,r+1,memory_order_release);
        }else{
            linux_wait_published(ctrl,r+1);
            struct kvm_dax_handoff token=ctrl->token;
            if(token.vm_creates!=1 || token.vcpu_creates!=1){fprintf(stderr,"Linux direct VM recreated\n");exit(1);}
            double start=monotonic();
            if(!me && !native_a){
                bench_control(1);
                if(ioctl(kvm,KVM_DAX_PEEK,&token))die("Linux direct attached metadata");
            }else{
                if(ram==MAP_FAILED){
                    off_t ram_shift=getenv("LAB_DAX_RAM_SHIFT")?strtoll(getenv("LAB_DAX_RAM_SHIFT"),NULL,0):0;
                    ram=mmap((void *)(uintptr_t)token.ram_hva,token.ram_bytes,PROT_READ|PROT_WRITE,
                        MAP_SHARED|MAP_FIXED_NOREPLACE,dax,2*1024*1024+ram_shift);
                    if(ram==MAP_FAILED)die("Linux direct fixed RAM HVA");
                }
                int reuse=vm>=0 && getenv("FC_DAX_REUSE_FD");
                int attached_vm=ioctl(reuse?vm:kvm,reuse?KVM_DAX_REATTACH:KVM_DAX_ATTACH,&token);
                if(attached_vm<0)die("Linux direct ATTACH");
                if(vm<0)vm=attached_vm;else if(!reuse)close(attached_vm);
                if(vcpu<0){
                    vcpu=ioctl(vm,KVM_DAX_VCPU,0);if(vcpu<0)die("Linux direct vCPU FD");
                    run=mmap(NULL,run_size,PROT_READ|PROT_WRITE,MAP_SHARED,vcpu,0);
                    if(run==MAP_FAILED)die("Linux direct run map");
                }
                executor.vcpu=vcpu;executor.run=run;atomic_store(&executor.stop,0);atomic_store(&executor.stopped,0);
                __atomic_store_n(&run->immediate_exit,0,__ATOMIC_RELEASE);
                atomic_store(&executor.wake_request_ns,context_now());
                atomic_store_explicit(&executor.go,1,memory_order_release);
                syscall(SYS_futex,&executor.go,FUTEX_WAKE|FUTEX_PRIVATE_FLAG,1);
            }
            uint64_t attached_ns=monotonic_ns();
            double attached=attached_ns/1e9;
            direct_duplicate(kvm,&token);
            uint64_t wait_start_ns=monotonic_ns();
            linux_wait(&hb->acknowledged,r+1,&executor);
            uint64_t observed_ns=monotonic_ns();
            double acknowledged=observed_ns/1e9;
            struct kvm_dax_resume_profile resume={0};
            uint64_t guest_ack_tsc=0;
            if(resume_profile!=MAP_FAILED){
                if(!__atomic_load_n(&resume_profile->entry_ready,__ATOMIC_ACQUIRE) ||
                   resume_profile->generation!=token.generation ||
                   atomic_load(&hb->ack_epoch)!=r+1){
                    fprintf(stderr,"Linux direct resume timing generation mismatch\n");exit(1);
                }
                resume=*resume_profile;
                guest_ack_tsc=atomic_load(&hb->ack_tsc);
                if(!resume.run_ns || !resume.virtual_tsc_khz || guest_ack_tsc<resume.guest_tsc ||
                   (resume.exit_ready && resume.entry_failed)){
                    fprintf(stderr,"Linux direct invalid VM-entry timing sample\n");exit(1);
                }
            }
            if(atomic_load(&hb->sequence)<=ctrl->counter || atomic_load(&hb->errors)){
                fprintf(stderr,"Linux direct execution/heap continuity failed\n");exit(1);
            }
            atomic_store_explicit(&ctrl->acknowledged,r+1,memory_order_release);
            printf("LINUX_DIRECT_JSON {\"event\":\"destination\",\"round\":%u,\"role\":\"%c\",\"attach_resume_us\":%.3f,\"kernel_attach_us\":%.3f,\"guest_ack_us\":%.3f,\"sequence\":%" PRIu64 ",\"vm_offset\":%" PRIu64 ",\"vcpu_offset\":%" PRIu64 ",\"vmcs_offset\":%" PRIu64 ",\"destination_control_service_us\":%.3f,\"executor_wake_us\":%.3f,\"wake_to_run_us\":%.3f",
                r,role,(attached-start)*1e6,(double)token.kernel_ns/1000,(acknowledged-attached)*1e6,(uint64_t)atomic_load(&hb->sequence),
                (uint64_t)token.vm_offset,(uint64_t)token.vcpu_offset,(uint64_t)token.vmcs_offset,
                (!me && !native_a)?bench_control_service_us:0,
                (me || native_a)?(atomic_load(&executor.wake_observed_ns)-atomic_load(&executor.wake_request_ns))/1000.0:0,
                (me || native_a)?(atomic_load(&executor.run_begin_ns)-atomic_load(&executor.wake_observed_ns))/1000.0:0);
            if(profile!=MAP_FAILED)for(unsigned i=0;i<8;i++)printf(",\"attach_stage_%u_us\":%.3f",i,profile->attach[i]/1000.0);
            if(resume_profile!=MAP_FAILED){
                /* Both TSC samples are in this guest's virtual TSC domain. The
                 * destination's paired MONOTONIC sample maps only this interval;
                 * no clock subtraction between different nodes is performed. */
                double entry_guest_us=(guest_ack_tsc-resume.guest_tsc)*1000.0/resume.virtual_tsc_khz;
                printf(",\"attached_to_run_us\":%.3f,\"run_to_entry_us\":%.3f,\"attached_to_entry_us\":%.3f,\"entry_to_guest_write_us\":%.3f,\"guest_write_to_observed_us\":%.3f,\"duplicate_check_us\":%.3f,\"tsc_pair_span_us\":%.3f,\"timing_generation\":%" PRIu64 ",\"attached_ns\":%" PRIu64 ",\"run_ns\":%" PRIu64 ",\"entry_ns\":%" PRIu64 ",\"observed_ns\":%" PRIu64 ",\"entry_guest_tsc\":%" PRIu64 ",\"ack_guest_tsc\":%" PRIu64 ",\"virtual_tsc_khz\":%" PRIu64 ",\"first_exit_ready\":%" PRIu64 ",\"first_exit_ns\":%" PRIu64 ",\"first_exit_reason\":%" PRIu64 ",\"entry_rip\":%" PRIu64,
                    (int64_t)(resume.run_ns-attached_ns)/1000.0,
                    (int64_t)(resume.entry_ns-resume.run_ns)/1000.0,
                    (int64_t)(resume.entry_ns-attached_ns)/1000.0,
                    entry_guest_us,(int64_t)(observed_ns-resume.entry_ns)/1000.0-entry_guest_us,
                    (wait_start_ns-attached_ns)/1000.0,resume.sample_span_ns/1000.0,
                    (uint64_t)resume.generation,attached_ns,(uint64_t)resume.run_ns,(uint64_t)resume.entry_ns,observed_ns,
                    (uint64_t)resume.guest_tsc,guest_ack_tsc,(uint64_t)resume.virtual_tsc_khz,
                    (uint64_t)resume.exit_ready,(uint64_t)resume.exit_ns,(uint64_t)resume.exit_reason,(uint64_t)resume.entry_rip);
            }
            if(!me && !native_a)for(unsigned i=0;i<3;i++)printf(",\"destination_control_part_%u_us\":%.3f",i,bench_control_parts_us[i]);
            puts("}");
            if(dense){
                linux_wait(&hb->verified,r+1,&executor);
                double first_ready=monotonic();
                linux_wait(&hb->warm_done,r+1,&executor);
                double warm_ready=monotonic();
                uint64_t errors=atomic_load(&hb->errors),clock_errors=atomic_load(&hb->clock_errors),
                         tsc_errors=atomic_load(&hb->tsc_errors);
                if(errors || clock_errors || tsc_errors){
                    fprintf(stderr,"Linux direct dense continuity failed\n");exit(1);
                }
                printf("LINUX_DIRECT_JSON {\"event\":\"working_set\",\"round\":%u,\"role\":\"%c\","
                    "\"working_set_bytes\":%" PRIu64 ",\"available_bytes\":%" PRIu64 ",\"instance_id\":%" PRIu64
                    ",\"first_sweep_ns\":%" PRIu64 ",\"warm_sweep_ns\":%" PRIu64
                    ",\"first_ack_to_working_set_ready_us\":%.3f,\"first_ack_to_warm_ready_us\":%.3f,"
                    "\"errors\":%" PRIu64 ",\"clock_errors\":%" PRIu64 ",\"tsc_errors\":%" PRIu64 "}\n",
                    r,role,(uint64_t)atomic_load(&hb->working_set_bytes),(uint64_t)atomic_load(&hb->available_bytes),
                    (uint64_t)atomic_load(&hb->instance_id),(uint64_t)atomic_load(&hb->first_sweep_ns),
                    (uint64_t)atomic_load(&hb->warm_sweep_ns),(first_ready-acknowledged)*1e6,
                    (warm_ready-acknowledged)*1e6,errors,clock_errors,tsc_errors);
                linux_wait(&ctrl->dense_observed,r+1,&executor);
            }
        }
    }
    linux_wait(&hb->verified,rounds,&executor);
    if(dense)linux_wait(&hb->warm_done,rounds,&executor);
    if(!me && native_a){
        /* Restore Firecracker's original local mm/executor after measurement. */
        struct kvm_dax_handoff token={0};
        linux_stop_executor(&executor,thread);
        if(ioctl(vm,KVM_DAX_DETACH,&token))die("Linux direct final executor detach");
        munmap(run,run_size);close(vcpu);close(vm);vm=vcpu=-1;
        bench_control(1);
    }
    if(getrusage(RUSAGE_THREAD,&cpu_after))die("controller CPU accounting");
    double controller_cpu_us=(cpu_after.ru_utime.tv_sec-cpu_before.ru_utime.tv_sec+cpu_after.ru_stime.tv_sec-cpu_before.ru_stime.tv_sec)*1e6+cpu_after.ru_utime.tv_usec-cpu_before.ru_utime.tv_usec+cpu_after.ru_stime.tv_usec-cpu_before.ru_stime.tv_usec;
    printf("LINUX_DIRECT_JSON {\"event\":\"complete\",\"role\":\"%c\",\"rounds\":%u,\"heap_bytes\":%" PRIu64 ",\"errors\":%" PRIu64 ",\"clock_errors\":%" PRIu64 ",\"tsc_errors\":%" PRIu64 ",\"clock_ns\":%" PRIu64 ",\"controller_cpu_us\":%.3f,\"controller_wall_us\":%.3f}\n",role,rounds,dense?(uint64_t)atomic_load(&hb->working_set_bytes):UINT64_C(16777216),(uint64_t)atomic_load(&hb->errors),(uint64_t)atomic_load(&hb->clock_errors),(uint64_t)atomic_load(&hb->tsc_errors),(uint64_t)atomic_load(&hb->clock_ns),controller_cpu_us,(monotonic()-controller_start)*1e6);
    if(me || native_a){atomic_store(&executor.go,2);syscall(SYS_futex,&executor.go,FUTEX_WAKE|FUTEX_PRIVATE_FLAG,1);pthread_join(thread,NULL);close(executor.console);}
    if(vcpu>=0){munmap(run,run_size);close(vcpu);close(vm);}
    if(!me)bench_child=0;
    close(kvm);close(dax);alarm(0);
}

static void context_bench(const char *mode, unsigned cpu, unsigned rounds, unsigned repeats)
{
    int processes = !strcmp(mode, "process-shared");
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) die("context CPU affinity");
    setvbuf(stdout, NULL, _IONBF, 0);
    alarm(120);
    for (unsigned sample = 0; sample < repeats; sample++) {
        struct context_control *c = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
            MAP_SHARED | MAP_ANONYMOUS, -1, 0);
        if (c == MAP_FAILED) die("context mmap");
        c->warmup = 10000;
        c->rounds = rounds;
        c->private_futex = !strcmp(mode, "thread-private");
        pid_t child = 0;
        pthread_t thread;
        if (processes) {
            child = fork();
            if (child < 0) die("context fork");
            if (!child) { alarm(120); context_worker(c); _exit(0); }
        } else {
            int rc = pthread_create(&thread, NULL, context_worker, c);
            if (rc) { errno = rc; die("context pthread_create"); }
        }
        struct rusage start, end;
        uint64_t begin = 0;
        for (unsigned i = 0; i < c->warmup + rounds; i++) {
            if (i == c->warmup) {
                if (getrusage(RUSAGE_THREAD, &start)) die("parent rusage start");
                begin = context_now();
            }
            context_signal(c, 1);
            context_wait(c, 0);
        }
        uint64_t elapsed = context_now() - begin;
        if (getrusage(RUSAGE_THREAD, &end)) die("parent rusage end");
        if (processes) {
            int status;
            if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) {
                fprintf(stderr, "Context worker failed\n"); exit(1);
            }
        } else {
            int rc = pthread_join(thread, NULL);
            if (rc) { errno = rc; die("context pthread_join"); }
        }
        long voluntary = end.ru_nvcsw - start.ru_nvcsw
            + c->child_end.ru_nvcsw - c->child_start.ru_nvcsw;
        long involuntary = end.ru_nivcsw - start.ru_nivcsw
            + c->child_end.ru_nivcsw - c->child_start.ru_nivcsw;
        long observed = voluntary + involuntary;
        if (observed < (long)rounds * 2 - 8) {
            fprintf(stderr, "Insufficient actual context switches: %ld\n", observed); exit(1);
        }
        printf("CONTEXT_JSON {\"mode\":\"%s\",\"cpu\":%u,\"sample\":%u,"
            "\"roundtrips\":%u,\"expected_handoffs\":%u,\"elapsed_ns\":%" PRIu64 ","
            "\"ns_per_handoff\":%.3f,\"voluntary_switches\":%ld,\"involuntary_switches\":%ld,"
            "\"observed_switches\":%ld,\"ns_per_observed_switch\":%.3f}\n",
            mode, cpu, sample, rounds, rounds * 2, elapsed,
            (double)elapsed / (rounds * 2), voluntary, involuntary, observed, (double)elapsed / observed);
        munmap(c, 4096);
    }
    alarm(0);
}

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "api")) {
        if ((argc != 6 && argc != 7) || !*argv[2] || strlen(argv[2]) >= sizeof(((struct sockaddr_un *)0)->sun_path) ||
            !*argv[3] || strpbrk(argv[3], " \t\r\n") || argv[4][0] != '/' || strpbrk(argv[4], " \r\n")) {
            fprintf(stderr, "Usage: shared-probe api SOCKET METHOD PATH JSON [EXPECTED_STATUS]\n");
            return 2;
        }
        if (argc == 7) {
            char *end;
            unsigned long status = strtoul(argv[6], &end, 10);
            if (*end || status < 100 || status > 599) return 2;
            bench_api_expected_status = status;
        }
        bench_socket = argv[2];
        bench_api_inspect = 1;
        bench_api(argv[3], argv[4], argv[5]);
        return 0;
    }
    if (argc < 3 || strlen(argv[2]) != 1 || (argv[2][0] != 'A' && argv[2][0] != 'B')) {
        fprintf(stderr, "Usage: shared-probe kvm|memory A|B; exchange A|B rounds nonce_hex; "
                "dax-exchange A|B rounds nonce_hex /dev/daxX.Y; dax-word A|B /dev/daxX.Y offset [value]\n");
        return 2;
    }
    char role = argv[2][0];
    if (!strcmp(argv[1], "kvm")) check_kvm(role);
    else if (!strcmp(argv[1], "kvm-api")) check_kvm_api(role);
    else if (!strcmp(argv[1], "memory")) check_memory(role);
    else if (!strcmp(argv[1], "context") && argc == 7) {
        char *end;
        if (strcmp(argv[3], "thread-private") && strcmp(argv[3], "thread-shared") && strcmp(argv[3], "process-shared")) return 2;
        unsigned long cpu = strtoul(argv[4], &end, 10);
        if (*end || cpu >= CPU_SETSIZE) return 2;
        unsigned long rounds = strtoul(argv[5], &end, 10);
        if (*end || rounds < 1000 || rounds > 10000000) return 2;
        unsigned long repeats = strtoul(argv[6], &end, 10);
        if (*end || !repeats || repeats > 100) return 2;
        context_bench(argv[3], cpu, rounds, repeats);
    }
    else if (!strcmp(argv[1], "direct-handoff") && argc == 5) {
        char *end;
        unsigned long rounds=strtoul(argv[3],&end,10);
        if(*end || !rounds || rounds>500)return 2;
        uint64_t nonce=strtoull(argv[4],&end,16);
        if(*end || !nonce)return 2;
        direct_handoff(role,rounds,nonce);
    }
    else if (!strcmp(argv[1], "linux-handoff") && argc == 5) {
        char *end;unsigned long rounds=strtoul(argv[3],&end,10);
        if(*end || !rounds || rounds>25000 || rounds%2)return 2;
        uint64_t nonce=strtoull(argv[4],&end,16);if(*end || !nonce)return 2;
        linux_handoff(role,rounds,nonce);
    }
    else if (!strcmp(argv[1], "tcp-bench") && argc == 8) {
        char *end;
        unsigned long port = strtoul(argv[4], &end, 10);
        if (*end || !port || port > 65535) return 2;
        uint64_t bytes = strtoull(argv[5], &end, 10);
        if (*end || !bytes || bytes > UINT64_C(4) * 1024 * 1024 * 1024) return 2;
        unsigned long rounds = strtoul(argv[6], &end, 10);
        if (*end || !rounds || rounds > 1000) return 2;
        uint64_t nonce = strtoull(argv[7], &end, 16);
        if (*end || !nonce) return 2;
        tcp_bench(role, argv[3], port, bytes, rounds, nonce);
    }
    else if (!strcmp(argv[1], "cache-bench") && argc == 12) {
        char *end;
        unsigned long port = strtoul(argv[5], &end, 10);
        if (*end || !port || port > 65535) return 2;
        unsigned long memory_mib = strtoul(argv[6], &end, 10);
        if (*end || memory_mib < 128 || memory_mib > 2048) return 2;
        unsigned long dirty = strtoul(argv[7], &end, 10);
        if (*end || dirty > 10000) return 2;
        unsigned long write_bytes = strtoul(argv[8], &end, 10);
        if (*end || (write_bytes != 64 && write_bytes != 4096)) return 2;
        unsigned long rounds = strtoul(argv[9], &end, 10);
        if (*end || !rounds || rounds > 1000 || rounds % 2) return 2;
        uint64_t nonce = strtoull(argv[10], &end, 16);
        if (*end || !nonce) return 2;
        unsigned mode;
        if (!strcmp(argv[11], "full")) mode = 0;
        else if (!strcmp(argv[11], "dirty")) mode = 1;
        else if (!strcmp(argv[11], "none")) mode = 2;
        else return 2;
        cache_bench(role, argv[3], argv[4], port, memory_mib, dirty, write_bytes, rounds, nonce, mode);
    }
    else if (!strcmp(argv[1], "migration-workload") && argc == 3)
        migration_workload(role);
    else if (!strcmp(argv[1], "migration") && argc == 8) {
        char *end;
        unsigned long port = strtoul(argv[4], &end, 10);
        if (*end || !port || port > 65535) return 2;
        unsigned long rounds = strtoul(argv[5], &end, 10);
        if (*end || !rounds || rounds > 1000) return 2;
        uint64_t nonce = strtoull(argv[6], &end, 16);
        if (*end || !nonce) return 2;
        unsigned long memory_mib = strtoul(argv[7], &end, 10);
        if (*end || memory_mib < 128 || memory_mib > 2048) return 2;
        migration_bench(role, argv[3], port, rounds, nonce, memory_mib);
    }
    else if (!strcmp(argv[1], "heartbeat") && argc >= 3 && argc <= 5)
        guest_heartbeat(argc == 3 || !strcmp(argv[3], "1"), argc == 5 && !strcmp(argv[4],"1"));
    else if (!strcmp(argv[1], "handoff") && argc == 8) {
        char *end;
        unsigned long rounds = strtoul(argv[4], &end, 10);
        if (*end || !rounds || rounds > 1000) return 2;
        if (strcmp(argv[5], "warm") && strcmp(argv[5], "cold")) return 2;
        unsigned long memory_mib = strtoul(argv[6], &end, 10);
        if (*end || memory_mib < 128 || memory_mib > 2048) return 2;
        uint64_t nonce = strtoull(argv[7], &end, 16);
        if (*end || !nonce) return 2;
        handoff_bench(role, argv[3], rounds, !strcmp(argv[5], "warm"), memory_mib, nonce);
    }
    else if ((!strcmp(argv[1], "exchange") && argc == 5) ||
             (!strcmp(argv[1], "dax-exchange") && argc == 6)) {
        char *end;
        unsigned long rounds = strtoul(argv[3], &end, 10);
        if (*end || rounds == 0 || rounds > 1000000) return 2;
        uint64_t nonce = strtoull(argv[4], &end, 16);
        if (*end) return 2;
        exchange(role, (uint32_t)rounds, nonce, argc == 6 ? argv[5] : NULL);
    } else if (!strcmp(argv[1], "dax-word") && (argc == 5 || argc == 6)) {
        char *end;
        uint64_t offset = strtoull(argv[4], &end, 0);
        if (*end) return 2;
        dax_word(argv[3], offset, argc == 6 ? argv[5] : NULL);
    } else return 2;
    return 0;
}
