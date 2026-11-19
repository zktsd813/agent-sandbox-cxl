#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ethtool.h>
#include <linux/kvm.h>
#include <linux/fs.h>
#include <linux/sockios.h>
#ifndef CXL_KVM_DAX_HEADER
#define CXL_KVM_DAX_HEADER "../linux/include/uapi/linux/kvm_dax.h"
#endif
#include CXL_KVM_DAX_HEADER
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

#define HB_MAGIC UINT64_C(0x48424d4943524f56)
struct smp_cpu {
    _Atomic uint64_t sequence, acknowledged, timer, ipi, ping, errors, clock_errors, tsc_errors;
};
struct heartbeat {
    _Atomic uint64_t magic, sequence, epoch, acknowledged, verified, errors;
    _Atomic uint64_t clock_ns, clock_errors;
    _Atomic uint64_t tsc, tsc_errors;
    _Atomic uint64_t ack_tsc, ack_epoch;
    /* Guest workload fields; offsets are shared with the lab ACK observer. */
    _Atomic uint64_t working_set_bytes, available_bytes, instance_id;
    _Atomic uint64_t first_sweep_ns, warm_sweep_ns, warm_done;
    struct smp_cpu cpus[KVM_EPT_MAX_VCPUS];
    _Atomic uint64_t epoch_detect_tsc, writers_ready_tsc;
};
_Static_assert(sizeof(struct heartbeat) <= 4096, "heartbeat page");
static const char *bench_socket;
static int bench_api_expected_status=204;
static int bench_api_inspect=1;
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

static uint64_t sample_tsc(void)
{
    unsigned low, high;
    __asm__ __volatile__("lfence; rdtsc" : "=a"(low), "=d"(high) :: "memory");
    return ((uint64_t)high << 32) | low;
}

/* SMP correctness workload: one checksum writer plus one futex ring thread
 * pinned to each CPU. Writers keep every vCPU busy; ring wakeups exercise IPIs. */
static struct heartbeat *smp_hb;
static unsigned smp_count;
static _Atomic int smp_turn[KVM_EPT_MAX_VCPUS];
static void smp_pin(unsigned cpu)
{
    cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) die("SMP CPU affinity");
}
static void *smp_ping(void *arg)
{
    unsigned cpu = (uintptr_t)arg, next = (cpu + 1) % smp_count;
    smp_pin(cpu);
    for (;;) {
        while (!atomic_load_explicit(&smp_turn[cpu], memory_order_acquire))
            syscall(SYS_futex, &smp_turn[cpu], FUTEX_WAIT_PRIVATE, 0, NULL);
        atomic_store(&smp_turn[cpu], 0);
        atomic_fetch_add(&smp_hb->cpus[cpu].ping, 1);
        atomic_store_explicit(&smp_turn[next], 1, memory_order_release);
        syscall(SYS_futex, &smp_turn[next], FUTEX_WAKE_PRIVATE, 1);
    }
    return NULL;
}
static void *smp_write(void *arg)
{
    unsigned cpu = (uintptr_t)arg;
    smp_pin(cpu);
    struct smp_cpu *s = &smp_hb->cpus[cpu];
    const size_t words = 65536;
    uint64_t *data = calloc(words, sizeof(*data));
    if (!data) die("SMP retained memory");
    uint64_t generation = 0, old_ns = 0, old_tsc = 0;
    for (;;) {
        uint64_t errors = 0;
        for (size_t i = 0; i < words; i++) {
            uint64_t expected = generation ? (generation ^ (i * UINT64_C(0x9e3779b97f4a7c15)) ^ cpu) : 0;
            if (data[i] != expected) errors++;
            data[i] = (generation + 1) ^ (i * UINT64_C(0x9e3779b97f4a7c15)) ^ cpu;
        }
        generation++;
        atomic_fetch_add(&s->errors, errors);
        uint64_t now_ns = monotonic_ns(), now_tsc = sample_tsc();
        if (now_ns < old_ns) atomic_fetch_add(&s->clock_errors, 1);
        if (now_tsc < old_tsc) atomic_fetch_add(&s->tsc_errors, 1);
        old_ns = now_ns; old_tsc = now_tsc;
        atomic_store_explicit(&s->sequence, generation, memory_order_release);
        atomic_store_explicit(&s->acknowledged, atomic_load(&smp_hb->epoch), memory_order_release);
    }
    return NULL;
}
static void smp_interrupts(void)
{
    FILE *f = fopen("/proc/interrupts", "r");
    if (!f) die("SMP interrupts");
    char line[2048];
    while (fgets(line, sizeof(line), f)) {
        char *colon = strchr(line, ':');
        if (!colon) continue;
        int timer = strstr(line, "LOC:") != NULL;
        int ipi = strstr(line, "RES:") != NULL || strstr(line, "CAL:") != NULL;
        if (!timer && !ipi) continue;
        for (unsigned cpu = 0; cpu < smp_count; cpu++) {
            char *end; uint64_t value = strtoull(colon + 1, &end, 10);
            if (end == colon + 1) { fprintf(stderr, "SMP interrupt columns missing\n"); exit(1); }
            if (timer) atomic_store(&smp_hb->cpus[cpu].timer, value);
            else atomic_fetch_add(&smp_hb->cpus[cpu].ipi, value);
            colon = end - 1;
        }
    }
    fclose(f);
}
static void guest_smp_heartbeat(unsigned count)
{
    if (!count || count > KVM_EPT_MAX_VCPUS || sysconf(_SC_NPROCESSORS_ONLN) != count) {
        fprintf(stderr, "SMP CPU count mismatch\n"); exit(1);
    }
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) die("SMP /dev/mem");
    int race = env_enabled("LAB_HEARTBEAT_RACE_PAGES");
    smp_hb = mmap(NULL, race ? 17*4096 : 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 64 * 1024 * 1024);
    if (smp_hb == MAP_FAILED) die("SMP heartbeat map");
    close(fd); memset(smp_hb, 0, sizeof(*smp_hb)); smp_count = count;
    pthread_t writers[8], pings[8];
    for (unsigned cpu = 0; cpu < count; cpu++) {
        int rc = pthread_create(&writers[cpu], NULL, smp_write, (void *)(uintptr_t)cpu);
        if (!rc) rc = pthread_create(&pings[cpu], NULL, smp_ping, (void *)(uintptr_t)cpu);
        if (rc) { errno = rc; die("SMP thread"); }
    }
    atomic_store(&smp_turn[0], 1);
    syscall(SYS_futex, &smp_turn[0], FUTEX_WAKE_PRIVATE, 1);
    for (unsigned cpu = 0; cpu < count; cpu++)
        while (!atomic_load(&smp_hb->cpus[cpu].sequence) || !atomic_load(&smp_hb->cpus[cpu].ping)) usleep(1000);
    smp_pin(0); smp_interrupts();
    atomic_store_explicit(&smp_hb->magic, HB_MAGIC, memory_order_release);
    uint64_t previous = 0;
    for (;;) {
        uint64_t epoch = atomic_load_explicit(&smp_hb->epoch, memory_order_acquire);
        atomic_fetch_add(&smp_hb->sequence, 1);
        if (race) {
            uint64_t value=atomic_load(&smp_hb->sequence);
            for (unsigned page=1;page<=16;page++)
                *(volatile uint64_t *)((char *)smp_hb+page*4096)=value;
        }
        if (epoch != previous) {
            atomic_store(&smp_hb->epoch_detect_tsc, sample_tsc());
            for (unsigned cpu = 0; cpu < count; cpu++)
                while (atomic_load_explicit(&smp_hb->cpus[cpu].acknowledged, memory_order_acquire) != epoch) usleep(50);
            atomic_store(&smp_hb->writers_ready_tsc, sample_tsc());
            for (unsigned cpu = 0; cpu < count; cpu++) atomic_store(&smp_hb->cpus[cpu].ipi, 0);
            smp_interrupts();
            for (unsigned cpu = 0; cpu < count; cpu++) {
                struct smp_cpu *s = &smp_hb->cpus[cpu];
                if (atomic_load(&s->errors) || atomic_load(&s->clock_errors) || atomic_load(&s->tsc_errors))
                    atomic_fetch_add(&smp_hb->errors, 1);
            }
            atomic_store(&smp_hb->ack_tsc, sample_tsc());
            atomic_store(&smp_hb->ack_epoch, epoch);
            atomic_store_explicit(&smp_hb->acknowledged, epoch, memory_order_release);
            atomic_store_explicit(&smp_hb->verified, epoch, memory_order_release);
            previous = epoch;
        }
        usleep(50);
    }
}

static void guest_heartbeat(int sleeping, int quiet)
{
    unsigned vcpus = getenv("LAB_HEARTBEAT_VCPUS") ? strtoul(getenv("LAB_HEARTBEAT_VCPUS"), NULL, 10) : 1;
    if (vcpus > 1 || env_enabled("LAB_HEARTBEAT_SMP")) { guest_smp_heartbeat(vcpus); return; }
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) die("guest /dev/mem");
    struct heartbeat *hb = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 64 * 1024 * 1024);
    if (hb == MAP_FAILED) die("guest heartbeat mmap");
    close(fd);
    size_t size = 16 * 1024 * 1024;
    unsigned char *state=malloc(size);
    if (!state) die("heartbeat retained heap");
    for (size_t i=0;i<size;i++) state[i]=(unsigned char)(i*31u+77u);
    atomic_store(&hb->sequence, 0);
    atomic_store(&hb->epoch, 0);
    atomic_store(&hb->acknowledged, 0);
    atomic_store(&hb->verified, 0);
    atomic_store(&hb->errors, 0);
    atomic_store(&hb->clock_ns,0);atomic_store(&hb->clock_errors,0);
    atomic_store(&hb->tsc,0);atomic_store(&hb->tsc_errors,0);
    atomic_store(&hb->ack_tsc,0);atomic_store(&hb->ack_epoch,0);
    atomic_store(&hb->warm_done, 0);
    const char *dirty_setting=getenv("LAB_HEARTBEAT_DIRTY_MIB");
    unsigned dirty_mib=dirty_setting?strtoul(dirty_setting,NULL,10):0;
    if(dirty_setting && dirty_mib!=0 && dirty_mib!=16 && dirty_mib!=64 && dirty_mib!=256 && dirty_mib!=1024) {
        fprintf(stderr,"Unsupported controlled dirty size\n");exit(1);
    }
    size_t dirty_words=(size_t)dirty_mib*1024*1024/8;
    volatile uint64_t *dirty=NULL;
    const char *line_setting=getenv("LAB_HEARTBEAT_DIRTY_LINES");
    unsigned dirty_lines=line_setting?strtoul(line_setting,NULL,10):0;
    const char *dirty_mode=getenv("LAB_HEARTBEAT_DIRTY_MODE");
    int read_only=dirty_mode && !strcmp(dirty_mode,"read");
    int same_value=dirty_mode && !strcmp(dirty_mode,"same");
    if (dirty_lines && dirty_lines!=1 && dirty_lines!=8 && dirty_lines!=64) {fprintf(stderr,"dirty line count\n");exit(1);}
    if (dirty_lines && dirty_words) {
        int payload_fd=open("/dev/mem",O_RDWR|O_CLOEXEC);
        if (payload_fd<0) die("open controlled physical payload");
        dirty=mmap(NULL,dirty_words*8,PROT_READ|PROT_WRITE,MAP_SHARED,payload_fd,128*1024*1024);
        close(payload_fd);
        if ((void *)dirty==MAP_FAILED) die("controlled physical payload");
    } else if (dirty_words) {
        dirty=mmap(NULL,dirty_words*8,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
        if ((void *)dirty==MAP_FAILED) die("controlled dirty memory");
    }
    for(size_t i=0;i<dirty_words;i++) dirty[i]=i;
    const char *page_setting=getenv("LAB_HEARTBEAT_DIRTY_PAGES");
    size_t changed_pages=page_setting?strtoul(page_setting,NULL,10):dirty_words/512;
    if(changed_pages>dirty_words/512) {fprintf(stderr,"dirty page count outside payload\n");exit(1);}
    uint64_t dirty_epoch=0;
    int timing=env_enabled("LAB_HEARTBEAT_TIMING");
    const unsigned spins=128;
    uint64_t previous_clock=0,previous_tsc=0;
    atomic_store_explicit(&hb->magic, HB_MAGIC, memory_order_release);
    if(dirty_setting){printf("L2_DIRTY_READY mib=%u\n",dirty_mib);fflush(stdout);}
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
        atomic_store_explicit(&hb->acknowledged, epoch, memory_order_release);
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

                for (size_t i = 0; i < size; i++) {
                    if (state[i] != (unsigned char)(i * 31u + 77u)) atomic_fetch_add(&hb->errors, 1);
                }


            if(!quiet){
                printf("L2_HANDOFF epoch=%" PRIu64 " sequence=%" PRIu64 " errors=%" PRIu64 "\n",
                       epoch, atomic_load(&hb->sequence), atomic_load(&hb->errors));
                fflush(stdout);
            }
            atomic_store_explicit(&hb->verified, epoch, memory_order_release);
            if(dirty_setting) {
                /* Outside pause-to-ACK: validate the previous epoch, then write
                 * the full requested payload before allowing the next pause. */
                for(size_t i=0;i<changed_pages*512;i++) {
                    int changed=i/512<changed_pages && (!dirty_lines || (!read_only && !same_value && i%512<dirty_lines*8 && i%8==0));
                    uint64_t expected=i^(changed?dirty_epoch:0);
                    if(dirty[i]!=expected) atomic_fetch_add(&hb->errors,1);
                }
                if (!dirty_lines) for(size_t i=0;i<changed_pages*512;i++) dirty[i]=i^epoch;
                else if (!read_only) {
                    for(size_t page=0;page<changed_pages*512;page+=512)
                        for(unsigned line=0;line<dirty_lines;line++) {
                            size_t i=page+line*8;
                            dirty[i]=i^(same_value?0:epoch);
                        }
                }
                dirty_epoch=epoch;
                atomic_store_explicit(&hb->warm_done,epoch,memory_order_release);
            }
            previous = epoch;
        }
        if (sleeping) usleep(sleeping == 2 ? 1000 : 100);
        else for (unsigned spin = 0; spin < spins; spin++) __asm__ __volatile__("pause");
    }
}

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "api")) {
        if (argc != 6 && argc != 7) return 2;
        bench_socket=argv[2];
        if (argc==7) bench_api_expected_status=strtoul(argv[6],NULL,10);
        bench_api(argv[3],argv[4],argv[5]);
        return 0;
    }
    if (argc < 3 || strlen(argv[2])!=1 || (argv[2][0]!='A' && argv[2][0]!='B')) return 2;
    char role=argv[2][0];
    if (!strcmp(argv[1],"kvm")) check_kvm(role);
    else if (!strcmp(argv[1],"kvm-api")) check_kvm_api(role);
    else if (!strcmp(argv[1],"memory")) check_memory(role);
    else if (!strcmp(argv[1],"heartbeat") && argc>=3 && argc<=5)
        guest_heartbeat(argc==3?1:(!strcmp(argv[3],"2")?2:!strcmp(argv[3],"1")),argc==5 && !strcmp(argv[4],"1"));
    else if ((!strcmp(argv[1],"exchange") && argc==5) || (!strcmp(argv[1],"dax-exchange") && argc==6)) {
        char *end; unsigned long rounds=strtoul(argv[3],&end,10);
        if (*end || !rounds || rounds>1000000) return 2;
        uint64_t nonce=strtoull(argv[4],&end,16); if (*end) return 2;
        exchange(role,rounds,nonce,argc==6?argv[5]:NULL);
    } else if (!strcmp(argv[1],"dax-word") && (argc==5 || argc==6)) {
        char *end;uint64_t offset=strtoull(argv[4],&end,0);if (*end) return 2;
        dax_word(argv[3],offset,argc==6?argv[5]:NULL);
    } else return 2;
    return 0;
}
