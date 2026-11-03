// Best-effort (BE) co-located load for the DSec-style interference runs (docs/dsec-interference-design.md).
// One thread per listed CPU, each busy LEVEL of every PERIOD_MS (random phase), doing KIND work:
//   cpu  integer hashing in L1/L2 (compile-like),  avx  AVX-512 FMA (lowers turbo),
//   mem  stream triad over MEM_MB per thread (memory bandwidth, LLC),  mix  cpu and mem in alternate periods.
// --idle runs under SCHED_IDLE; --core-cookie puts all threads in one core-scheduling cookie so they never share an
// SMT core with untagged (latency-sensitive) tasks. Prints JSON with work done per kind on SIGTERM/SIGINT or after
// --duration-s. Build: gcc -O2 -mavx512f -pthread be_load.c -o be_load
#define _GNU_SOURCE
#include <immintrin.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

#ifndef PR_SCHED_CORE
#define PR_SCHED_CORE 62
#define PR_SCHED_CORE_CREATE 1
#endif
#define PIDTYPE_TGID 1

static volatile sig_atomic_t stop;
static double level = 0.3, period_ms = 10, duration_s = 0;
static size_t mem_mb = 256;
static const char *kind = "mix";
static int idle, cookie;

struct worker { int cpu; uint64_t cpu_ops, avx_ops, mem_bytes; double busy_s; pthread_t thread; };

static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }

static uint64_t cpu_work(double until) {
    uint64_t ops = 0, x = 0x9e3779b97f4a7c15ull;
    while (now() < until) {
        for (int i = 0; i < 4096; i++) { x ^= x << 13; x ^= x >> 7; x ^= x << 17; x *= 0xff51afd7ed558ccdull; }
        ops += 4096;
    }
    __asm__ volatile("" :: "r"(x));
    return ops;
}

__attribute__((target("avx512f"))) static uint64_t avx_work(double until) {
    __m512 a = _mm512_set1_ps(1.0001f), b = _mm512_set1_ps(0.9999f), c0 = _mm512_set1_ps(0.5f), c1 = c0, c2 = c0, c3 = c0;
    uint64_t ops = 0;
    while (now() < until) {
        for (int i = 0; i < 1024; i++) {
            c0 = _mm512_fmadd_ps(a, c0, b); c1 = _mm512_fmadd_ps(a, c1, b);
            c2 = _mm512_fmadd_ps(a, c2, b); c3 = _mm512_fmadd_ps(a, c3, b);
        }
        ops += 4096;
    }
    float sink[16];
    _mm512_storeu_ps(sink, _mm512_add_ps(_mm512_add_ps(c0, c1), _mm512_add_ps(c2, c3)));
    __asm__ volatile("" :: "r"(sink[0]));
    return ops;
}

static uint64_t mem_work(double *x, double *y, double *z, size_t n, size_t *pos, double until) {
    uint64_t bytes = 0;
    while (now() < until) {
        size_t end = *pos + 65536 < n ? *pos + 65536 : n;
        for (size_t i = *pos; i < end; i++) x[i] = y[i] + 1.5 * z[i];
        bytes += (end - *pos) * 3 * sizeof(double);
        *pos = end == n ? 0 : end;
    }
    return bytes;
}

static void *run(void *arg) {
    struct worker *w = arg;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(w->cpu, &set);
    pthread_setaffinity_np(pthread_self(), sizeof set, &set);
    int memory = !strcmp(kind, "mem") || !strcmp(kind, "mix");
    size_t n = memory ? mem_mb * 1024 * 1024 / 3 / sizeof(double) : 0, pos = 0;
    double *x = NULL, *y = NULL, *z = NULL;
    if (memory) {  // first touch on this thread's CPU, so pages land on its NUMA node
        x = malloc(n * sizeof(double)); y = malloc(n * sizeof(double)); z = malloc(n * sizeof(double));
        for (size_t i = 0; i < n; i++) { x[i] = 0; y[i] = i; z[i] = 1; }
    }
    double period = period_ms / 1000, start = now();
    unsigned seed = w->cpu * 2654435761u;
    double next = start + period * (rand_r(&seed) / (double)RAND_MAX);  // random phase across threads
    for (long k = 0; !stop && (!duration_s || now() - start < duration_s); k++) {
        double until = next + period * level, t0 = now();
        const char *what = strcmp(kind, "mix") ? kind : (k % 2 ? "mem" : "cpu");
        if (!strcmp(what, "cpu")) w->cpu_ops += cpu_work(until);
        else if (!strcmp(what, "avx")) w->avx_ops += avx_work(until);
        else w->mem_bytes += mem_work(x, y, z, n, &pos, until);
        w->busy_s += now() - t0;
        next += period;
        double rest = next - now();
        if (rest > 0) {
            struct timespec t = {(time_t)rest, (long)((rest - (time_t)rest) * 1e9)};
            nanosleep(&t, NULL);
        } else {
            next = now();
        }
    }
    free(x); free(y); free(z);
    return NULL;
}

static void on_signal(int s) { (void)s; stop = 1; }

int main(int argc, char **argv) {
    char *cpus = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--cpus") && i + 1 < argc) cpus = argv[++i];
        else if (!strcmp(argv[i], "--level") && i + 1 < argc) level = atof(argv[++i]);
        else if (!strcmp(argv[i], "--kind") && i + 1 < argc) kind = argv[++i];
        else if (!strcmp(argv[i], "--period-ms") && i + 1 < argc) period_ms = atof(argv[++i]);
        else if (!strcmp(argv[i], "--mem-mb") && i + 1 < argc) mem_mb = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--duration-s") && i + 1 < argc) duration_s = atof(argv[++i]);
        else if (!strcmp(argv[i], "--idle")) idle = 1;
        else if (!strcmp(argv[i], "--core-cookie")) cookie = 1;
        else { fprintf(stderr, "unknown argument %s\n", argv[i]); return 2; }
    }
    if (!cpus || level <= 0 || level > 1 || (strcmp(kind, "cpu") && strcmp(kind, "avx") && strcmp(kind, "mem")
                                             && strcmp(kind, "mix"))) {
        fprintf(stderr, "usage: be_load --cpus LIST --level (0,1] [--kind cpu|avx|mem|mix] [--period-ms 10] "
                        "[--mem-mb 256] [--duration-s S] [--idle] [--core-cookie]\n");
        return 2;
    }
    if (idle) {
        struct sched_param p = {0};
        if (sched_setscheduler(0, SCHED_IDLE, &p)) { perror("SCHED_IDLE"); return 1; }
    }
    if (cookie && prctl(PR_SCHED_CORE, PR_SCHED_CORE_CREATE, 0, PIDTYPE_TGID, 0)) { perror("core cookie"); return 1; }
    struct worker w[1024];
    int count = 0;
    for (char *part = strtok(cpus, ","); part && count < 1024; part = strtok(NULL, ",")) {
        int a, b;
        if (sscanf(part, "%d-%d", &a, &b) != 2) b = a = atoi(part);
        for (int c = a; c <= b && count < 1024; c++) w[count++] = (struct worker){.cpu = c};
    }
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    double start = now();
    for (int i = 0; i < count; i++) pthread_create(&w[i].thread, NULL, run, &w[i]);  // threads inherit policy, cookie
    for (int i = 0; i < count; i++) pthread_join(w[i].thread, NULL);
    double wall = now() - start, busy = 0;
    uint64_t cpu_ops = 0, avx_ops = 0, mem_bytes = 0;
    for (int i = 0; i < count; i++) { busy += w[i].busy_s; cpu_ops += w[i].cpu_ops; avx_ops += w[i].avx_ops; mem_bytes += w[i].mem_bytes; }
    printf("{\"kind\": \"%s\", \"level\": %g, \"threads\": %d, \"idle\": %d, \"core_cookie\": %d, \"wall_s\": %.3f, "
           "\"busy_s\": %.3f, \"cpu_ops\": %llu, \"avx_ops\": %llu, \"mem_gib\": %.3f}\n", kind, level, count, idle,
           cookie, wall, busy, (unsigned long long)cpu_ops, (unsigned long long)avx_ops, mem_bytes / 1073741824.0);
    return 0;
}
