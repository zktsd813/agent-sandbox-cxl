// Cost of cache-line write-back / invalidate over a range, for DRAM vs CXL memory.
// Run under numactl --membind=N. Usage: wbcost SIZE_MB THREADS OP STATE REPS
//   OP:    clwb | clflushopt
//   STATE: dirty  - every line written just before the timed flush (resident if SIZE < LLC)
//          clean  - lines read just before (resident clean copies), timed flush = invalidate
//          absent - lines flushed beforehand (not in cache), timed flush = receiver invalidate of non-cached lines
#define _GNU_SOURCE
#include <immintrin.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>

static char *buf;
static size_t size, per;
static int nthr, op, reps;
static const char *state;
static pthread_barrier_t bar;
static double tsum[64][64];

static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e9 + t.tv_nsec; }

__attribute__((target("clwb,clflushopt"))) static void flush(char *p, size_t n, int o) {
  if (o == 0) for (size_t i = 0; i < n; i += 64) _mm_clwb(p + i);
  else for (size_t i = 0; i < n; i += 64) _mm_clflushopt(p + i);
  _mm_sfence();
}

static void *worker(void *arg) {
  long id = (long)arg;
  cpu_set_t c; CPU_ZERO(&c); CPU_SET(2 + id, &c); sched_setaffinity(0, sizeof c, &c);
  char *p = buf + id * per;
  volatile uint64_t sink = 0;
  for (int r = 0; r < reps + 1; r++) {
    if (!strcmp(state, "dirty")) for (size_t i = 0; i < per; i += 64) *(volatile uint64_t *)(p + i) = r + i;
    else if (!strcmp(state, "clean")) { flush(p, per, 1); for (size_t i = 0; i < per; i += 64) sink += *(volatile uint64_t *)(p + i); }
    else flush(p, per, 1);
    pthread_barrier_wait(&bar);
    double t0 = now();
    flush(p, per, op);
    double t1 = now();
    tsum[id][r] = t1 - t0;
    pthread_barrier_wait(&bar);
  }
  return (void *)(uintptr_t)sink;
}

int main(int argc, char **argv) {
  size = (size_t)atol(argv[1]) << 20; nthr = atoi(argv[2]); op = strcmp(argv[3], "clwb") ? 1 : 0; state = argv[4]; reps = atoi(argv[5]);
  per = size / nthr;
  buf = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
  if (buf == MAP_FAILED) { perror("mmap"); return 1; }
  memset(buf, 1, size);
  pthread_barrier_init(&bar, NULL, nthr);
  pthread_t th[64];
  for (long i = 0; i < nthr; i++) pthread_create(&th[i], NULL, worker, (void *)i);
  for (int i = 0; i < nthr; i++) pthread_join(th[i], NULL);
  // wall per rep = slowest thread; report median over reps (skip warmup rep 0)
  double w[64];
  for (int r = 1; r <= reps; r++) { double m = 0; for (int i = 0; i < nthr; i++) if (tsum[i][r] > m) m = tsum[i][r]; w[r - 1] = m; }
  for (int i = 0; i < reps; i++) for (int j = i + 1; j < reps; j++) if (w[j] < w[i]) { double t = w[i]; w[i] = w[j]; w[j] = t; }
  double med = w[reps / 2];
  double lines = size / 64.0;
  printf("{\"size_mb\":%zu,\"threads\":%d,\"op\":\"%s\",\"state\":\"%s\",\"median_ms\":%.3f,\"ns_per_line\":%.2f,\"gb_per_s\":%.2f}\n",
         size >> 20, nthr, argv[3], state, med / 1e6, med / lines, size / med);
  return 0;
}
