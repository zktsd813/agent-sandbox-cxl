#define _GNU_SOURCE
#include <ctype.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/random.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>
#include <dirent.h>

/* Discard-handshake measurement variant; original comparison source is frozen. Keep pool_guest_probe.c unchanged.
 * Timings include each operation body and exclude residency/stat probes,
 * correctness postchecks, and serial output. The read operation intentionally
 * includes the complete deterministic word oracle and FNV hash; it is not a
 * pure storage bandwidth benchmark. Guest CPU affinity is fixed before work.
 *
 * The guest supplies evidence only; it does not reveal inode/page-cache metadata
 * to the pool. Every immutable word and every private word has an independent
 * deterministic oracle. mincore is queried through a fresh MAP_SHARED mapping,
 * so private CoW pages cannot masquerade as resident file-cache pages. */
#define MIB (1024ULL * 1024ULL)
#define CHUNK (64U * 1024U)
#define PRIVATE_BYTES (4U * 1024U * 1024U)
#define TAG UINT64_C(0x504f4f4c50414745)

struct io_stats { uint64_t reads, sectors; int valid; };
struct cache_range { uint64_t offset, gpa, pages; };
struct state {
    int payload, upper;
    uint64_t bytes, vm, generation, cookie, commands;
    uint64_t *cow;
    void *cachemap;
    struct cache_range *ranges;
    size_t range_count;
    uint64_t churn_bytes, checked_churn_words;
    volatile uint64_t *anon;
    uint64_t anon_bytes, anon_seed, anon_checked_words, mono_start, mono_finish;
    int anon_filled, anon_pinned;
    char boot[48];
};
static void fatal(const char *s) { fprintf(stderr,"POOL_GUEST_FATAL %s: %s\n",s,strerror(errno)); exit(1); }
static void require(int c,const char *s) { if(!c) { errno=EINVAL; fatal(s); } }
static uint64_t number(const char *s) {
    char *e; require(*s && isdigit((unsigned char)*s),"unsigned decimal"); errno=0;
    uint64_t n=strtoull(s,&e,10); require(!errno && !*e,"invalid number"); return n;
}
static uint64_t now_ns(void) {
    struct timespec ts; if(clock_gettime(CLOCK_MONOTONIC_RAW,&ts)) fatal("clock_gettime RAW");
    require(ts.tv_sec>=0 && ts.tv_nsec>=0 && ts.tv_nsec<1000000000L,"invalid RAW clock");
    return (uint64_t)ts.tv_sec*UINT64_C(1000000000)+(uint64_t)ts.tv_nsec;
}
static uint64_t mono_ns(void) {
    struct timespec ts; if(clock_gettime(CLOCK_MONOTONIC,&ts)) fatal("clock_gettime monotonic");
    return (uint64_t)ts.tv_sec*UINT64_C(1000000000)+(uint64_t)ts.tv_nsec;
}
static int current_cpu(void) {
    int cpu=sched_getcpu(); if(cpu<0) fatal("sched_getcpu");
    require(cpu==0,"guest helper left pinned CPU 0"); return cpu;
}
static void pin_cpu(void) {
    cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(0,&mask);
    if(sched_setaffinity(0,sizeof(mask),&mask)) fatal("pin helper to guest CPU 0");
    (void)current_cpu();
}
static const char *timing_scope(const char *op) {
    if(!strcmp(op,"warm") || !strcmp(op,"check")) return "full_payload_read_word_validation_and_fnv";
    if(!strcmp(op,"write")) return "overwrite_initialized_4MiB_private_mapping_and_4MiB_upper_file";
    if(!strcmp(op,"recycle")) return "payload_eviction_then_anonymous_fill_verify_unmap";
    if(!strcmp(op,"churn")) return "anonymous_fill_verify_unmap";
    if(!strcmp(op,"evict")) return "payload_fadvise_dontneed";
    if(!strcmp(op,"cachemap")) return "validation_mlock_and_pagemap_walk_without_serial_output";
    if(!strcmp(op,"anon_alloc")) return "anonymous_mmap_without_touch";
    if(!strcmp(op,"anon_fill")) return "first_touch_and_full_volatile_stores";
    if(!strcmp(op,"anon_verify")) return "full_volatile_loads_and_oracle";
    if(!strcmp(op,"anon_free")) return "anonymous_munlock_and_munmap";
    if(!strcmp(op,"wait")) return "guest_requested_sleep_including_reporter_wait";
    return "control_command_body";
}
static uint64_t mix(uint64_t x) {
    x+=UINT64_C(0x9e3779b97f4a7c15);
    x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);
    x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb); return x^(x>>31);
}
static uint64_t word(uint64_t tag,uint64_t i) { return mix(tag^i); }
static uint64_t private_tag(struct state *s) { return mix(s->vm^UINT64_C(0x564d000000000000))^mix(s->generation)^UINT64_C(0x50524956415445); }
static int open_regular(const char *name,int flags) {
    int fd=open(name,flags|O_CLOEXEC|O_NOFOLLOW,0600); if(fd<0) fatal(name);
    struct stat st; if(fstat(fd,&st)) fatal("fstat"); require(S_ISREG(st.st_mode),"regular file required"); return fd;
}
static void exact(int fd,void *buf,size_t bytes,uint64_t off,int write_mode) {
    size_t done=0;
    while(done<bytes) {
        ssize_t n=write_mode?pwrite(fd,(char*)buf+done,bytes-done,(off_t)(off+done)):
                             pread(fd,(char*)buf+done,bytes-done,(off_t)(off+done));
        if(n<0 && errno==EINTR) continue;
        if(n<=0) { if(!n) errno=EIO; fatal("exact I/O"); } done+=(size_t)n;
    }
}
static uint64_t file_pattern(int fd,uint64_t bytes,uint64_t tag,int write_mode) {
    uint64_t b[CHUNK/8],hash=UINT64_C(14695981039346656037);
    require(bytes && bytes%8==0,"pattern length");
    for(uint64_t off=0;off<bytes;off+=CHUNK) {
        size_t n=bytes-off<CHUNK?(size_t)(bytes-off):CHUNK;
        if(write_mode) for(size_t i=0;i<n/8;i++) b[i]=htole64(word(tag,off/8+i));
        exact(fd,b,n,off,write_mode);
        for(size_t i=0;i<n/8;i++) {
            require(le64toh(b[i])==word(tag,off/8+i),"file word mismatch");
            hash=(hash^le64toh(b[i]))*UINT64_C(1099511628211);
        }
    }
    return hash;
}
static uint64_t resident(struct state *s) {
    size_t count=(size_t)(s->bytes/4096); unsigned char *vec=malloc(count);
    if(!vec) fatal("mincore allocation");
    void *p=mmap(NULL,(size_t)s->bytes,PROT_READ,MAP_SHARED,s->payload,0);
    if(p==MAP_FAILED || mincore(p,(size_t)s->bytes,vec)) fatal("file mincore");
    uint64_t pages=0; for(size_t i=0;i<count;i++) pages+=!!(vec[i]&1);
    if(munmap(p,(size_t)s->bytes)) fatal("mincore munmap");
    free(vec); return pages;
}
static uint64_t meminfo(const char *key) {
    FILE *f=fopen("/proc/meminfo","r"); if(!f) fatal("meminfo");
    char line[160],name[80]; uint64_t n,result=UINT64_MAX;
    while(fgets(line,sizeof(line),f)) if(sscanf(line,"%79s %" SCNu64,name,&n)==2 && !strcmp(name,key)) result=n;
    require(!ferror(f),"read meminfo"); if(fclose(f)) fatal("close meminfo");
    require(result!=UINT64_MAX,"missing meminfo field"); return result;
}
static struct io_stats stats(void) {
    struct io_stats s={0}; FILE *f=fopen("/sys/class/block/vda/stat","r"); if(!f) return s;
    uint64_t merged;
    require(fscanf(f,"%" SCNu64 " %" SCNu64 " %" SCNu64,&s.reads,&merged,&s.sectors)==3,"block stats");
    if(fclose(f)) fatal("close stats");
    s.valid=1; return s;
}
static uint64_t check_cow(struct state *s) {
    uint64_t hash=UINT64_C(14695981039346656037),tag=private_tag(s);
    for(size_t i=0;i<PRIVATE_BYTES/8;i++) {
        require(le64toh(s->cow[i])==word(tag,i),"private CoW mismatch");
        hash=(hash^le64toh(s->cow[i]))*UINT64_C(1099511628211);
    }
    return hash;
}
static void mutate(struct state *s,uint64_t generation) {
    require(generation==s->generation+1,"generation must advance once"); s->generation=generation;
    uint64_t tag=private_tag(s);
    for(size_t i=0;i<PRIVATE_BYTES/8;i++) s->cow[i]=htole64(word(tag,i));
    file_pattern(s->upper,PRIVATE_BYTES,tag^UINT64_C(0x5550504552),1);
}
static void cachemap(struct state *s,const char *label) {
    require(!s->cachemap,"file cache mapping already pinned");
    struct rlimit limit={.rlim_cur=RLIM_INFINITY,.rlim_max=RLIM_INFINITY};
    if(setrlimit(RLIMIT_MEMLOCK,&limit)) fatal("raise validation memlock limit");
    s->cachemap=mmap(NULL,(size_t)s->bytes,PROT_READ,MAP_SHARED,s->payload,0);
    if(s->cachemap==MAP_FAILED) fatal("validation file mmap");
    if(mlock(s->cachemap,(size_t)s->bytes)) fatal("pin validation file pages");
    /* This is a validation-only observer. These guest GFNs never enter the
     * production hint index or select pages for pool admission. Pinning holds
     * the actual file-cache pages stable until the host has paused and audited
     * their mappings; mere mincore + old block-I/O GPAs would be insufficient. */
    int pagemap=open("/proc/self/pagemap",O_RDONLY|O_CLOEXEC);
    if(pagemap<0) fatal("guest pagemap open");
    s->ranges=calloc((size_t)(s->bytes/4096),sizeof(*s->ranges));
    if(!s->ranges) fatal("validation range allocation");
    s->range_count=0;
    uint64_t run_offset=0,run_gpa=0,run_pages=0;
    for(uint64_t off=0;off<s->bytes;off+=4096) {
        volatile unsigned char touch=((volatile unsigned char*)s->cachemap)[off]; (void)touch;
        uint64_t entry=0,hva=(uintptr_t)s->cachemap+off;
        exact(pagemap,&entry,8,(hva/4096)*8,0);
        uint64_t gfn=entry&((UINT64_C(1)<<55)-1);
        require((entry>>63) && !(entry&(UINT64_C(1)<<62)) && gfn && gfn<=UINT64_MAX/4096,"present guest PFN unavailable");
        uint64_t gpa=gfn*4096;
        if(run_pages && gpa!=run_gpa+run_pages*4096) {
            s->ranges[s->range_count++]=(struct cache_range){run_offset,run_gpa,run_pages};
            run_pages=0;
        }
        if(!run_pages) { run_offset=off; run_gpa=gpa; }
        run_pages++;
    }
    if(run_pages) s->ranges[s->range_count++]=(struct cache_range){run_offset,run_gpa,run_pages};
    (void)label;
    if(close(pagemap)) fatal("guest pagemap close");
}
static void cacheunmap(struct state *s) {
    require(s->cachemap!=NULL,"validation cache mapping not pinned");
    if(munlock(s->cachemap,(size_t)s->bytes)||munmap(s->cachemap,(size_t)s->bytes)) fatal("unpin validation cache map");
    s->cachemap=NULL;
}
static void print_ranges(struct state *s,const char *label) {
    for(size_t i=0;i<s->range_count;i++) {
        struct cache_range *r=&s->ranges[i];
        printf("POOL_CACHE_RANGE %s %" PRIu64 " %" PRIu64 " %" PRIu64 "\n",label,r->offset,r->gpa,r->pages);
    }
    free(s->ranges); s->ranges=NULL; s->range_count=0;
}
static void evict_payload(struct state *s) {
    require(!s->cachemap,"cannot evict while validation mapping is pinned");
    int rc=posix_fadvise(s->payload,0,0,POSIX_FADV_DONTNEED);
    if(rc) { errno=rc; fatal("evict"); }
}
static void churn(struct state *s,uint64_t mib) {
    require(!s->cachemap && mib>=1 && mib<=512,"churn range/pinned validation map");
    uint64_t bytes=mib*MIB;
    volatile uint64_t *p=mmap(NULL,(size_t)bytes,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(p==MAP_FAILED) fatal("churn mmap");
    /* Volatile stores and loads keep both complete memory passes observable. */
    for(uint64_t i=0;i<bytes/8;i++) p[i]=word(s->vm^s->commands,i);
    for(uint64_t i=0;i<bytes/8;i++) require(p[i]==word(s->vm^s->commands,i),"churn memory mismatch");
    if(munmap((void*)p,(size_t)bytes)) fatal("churn munmap");
    s->churn_bytes=bytes; s->checked_churn_words=bytes/8;
}
static void trace_dump(const char *label) {
    const char *base="/sys/kernel/tracing";
    char path[256],line[4096];
    snprintf(path,sizeof(path),"%s/trace",base);
    FILE *f=fopen(path,"r"); if(!f) fatal("trace open");
    printf("POOL_REPORT_TRACE_BEGIN %s\n",label);
    while(fgets(line,sizeof(line),f)) {
        require(strchr(line,'\n')!=NULL,"overlong trace line");
        printf("POOL_REPORT_TRACE %s %s",label,line);
    }
    require(!ferror(f),"trace read"); if(fclose(f)) fatal("trace close");
    snprintf(path,sizeof(path),"%s/per_cpu",base);
    DIR *d=opendir(path); if(!d) fatal("trace per_cpu open");
    struct dirent *entry;
    while((entry=readdir(d))) {
        if(strncmp(entry->d_name,"cpu",3)) continue;
        require(strlen(entry->d_name)<32,"trace cpu name");
        snprintf(path,sizeof(path),"%s/per_cpu/%.31s/stats",base,entry->d_name);
        f=fopen(path,"r"); if(!f) fatal("trace stats open");
        while(fgets(line,sizeof(line),f)) printf("POOL_REPORT_STATS %s %.31s %s",label,entry->d_name,line);
        require(!ferror(f),"trace stats read"); if(fclose(f)) fatal("trace stats close");
    }
    if(closedir(d)) fatal("trace per_cpu close");
    printf("POOL_REPORT_TRACE_END %s\n",label);
}
static void anon_alloc(struct state *s,uint64_t mib) {
    require(!s->anon && !s->cachemap && mib>=1 && mib<=256,"anonymous allocation state/range");
    s->anon_bytes=mib*MIB; s->anon_seed=mix(s->vm)^mix(s->commands)^UINT64_C(0x414e4f4e);
    s->anon=mmap(NULL,(size_t)s->anon_bytes,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(s->anon==MAP_FAILED) fatal("held anonymous mmap");
    s->anon_filled=s->anon_pinned=0;s->anon_checked_words=0;
}
static void anon_fill(struct state *s) {
    require(s->anon && !s->anon_filled,"anonymous first fill state");
    for(uint64_t i=0;i<s->anon_bytes/8;i++) s->anon[i]=word(s->anon_seed,i);
    s->anon_filled=1;
}
static void anon_verify(struct state *s) {
    require(s->anon && s->anon_filled,"anonymous verification state");
    for(uint64_t i=0;i<s->anon_bytes/8;i++) require(s->anon[i]==word(s->anon_seed,i),"held anonymous word mismatch");
    s->anon_checked_words=s->anon_bytes/8;
}
static void anon_map(struct state *s,const char *label) {
    require(s->anon && s->anon_filled && !s->anon_pinned,"anonymous GPA state");
    struct rlimit limit={.rlim_cur=RLIM_INFINITY,.rlim_max=RLIM_INFINITY};
    if(setrlimit(RLIMIT_MEMLOCK,&limit)||mlock((void*)s->anon,(size_t)s->anon_bytes)) fatal("anonymous validation pin");
    s->anon_pinned=1;
    int fd=open("/proc/self/pagemap",O_RDONLY|O_CLOEXEC);if(fd<0) fatal("anonymous pagemap");
    uint64_t run_offset=0,run_gpa=0,run_pages=0;
    for(uint64_t off=0;off<s->anon_bytes;off+=4096) {
        uint64_t entry=0;exact(fd,&entry,8,(((uintptr_t)s->anon+off)/4096)*8,0);
        uint64_t pfn=entry&((UINT64_C(1)<<55)-1);
        require((entry>>63) && !(entry&(UINT64_C(1)<<62)) && pfn,"anonymous PFN absent");
        uint64_t gpa=pfn*4096;
        if(run_pages && gpa!=run_gpa+run_pages*4096) {
            printf("POOL_ANON_RANGE %s %" PRIu64 " %" PRIu64 " %" PRIu64 "\n",label,run_offset,run_gpa,run_pages);
            run_pages=0;
        }
        if(!run_pages){run_offset=off;run_gpa=gpa;}run_pages++;
    }
    if(run_pages)printf("POOL_ANON_RANGE %s %" PRIu64 " %" PRIu64 " %" PRIu64 "\n",label,run_offset,run_gpa,run_pages);
    if(close(fd))fatal("anonymous pagemap close");
}
static void anon_free(struct state *s) {
    require(s->anon && s->anon_filled,"anonymous free state");
    if(s->anon_pinned && munlock((void*)s->anon,(size_t)s->anon_bytes))fatal("anonymous munlock");
    if(munmap((void*)s->anon,(size_t)s->anon_bytes))fatal("anonymous held munmap");
    s->anon=NULL;s->anon_pinned=s->anon_filled=0;
}
static void emit(struct state *s,const char *label,const char *op,uint64_t hash,uint64_t before,struct io_stats io,uint64_t elapsed_ns,int cpu_before,int cpu_after) {
    struct io_stats after=stats();
    uint64_t cow_hash=check_cow(s),upper_hash=file_pattern(s->upper,PRIVATE_BYTES,private_tag(s)^UINT64_C(0x5550504552),0);
    printf("{\"event\":\"pool_guest\",\"label\":\"%s\",\"op\":\"%s\",\"vm\":%" PRIu64
           ",\"boot_id\":\"%s\",\"pid\":%ld,\"cookie\":\"%016" PRIx64 "\",\"command\":%" PRIu64
           ",\"generation\":%" PRIu64 ",\"payload_bytes\":%" PRIu64 ",\"resident_before\":%" PRIu64
           ",\"resident_after\":%" PRIu64 ",\"cached_kib\":%" PRIu64 ",\"shmem_kib\":%" PRIu64
           ",\"payload_hash\":\"%016" PRIx64 "\",\"private_hash\":\"%016" PRIx64
           "\",\"upper_hash\":\"%016" PRIx64 "\",\"checked_payload_words\":%" PRIu64,
           label,op,s->vm,s->boot,(long)getpid(),s->cookie,++s->commands,s->generation,s->bytes,
           before,resident(s),meminfo("Cached:"),meminfo("Shmem:"),hash,cow_hash,upper_hash,hash?s->bytes/8:0);
    printf(",\"elapsed_ns\":%" PRIu64 ",\"timing_clock\":\"CLOCK_MONOTONIC_RAW\",\"timing_scope\":\"%s\""
           ",\"pinned_cpu\":0,\"cpu\":%d,\"cpu_after\":%d,\"churn_bytes\":%" PRIu64
           ",\"checked_churn_words\":%" PRIu64,
           elapsed_ns,timing_scope(op),cpu_before,cpu_after,s->churn_bytes,s->checked_churn_words);
    if(io.valid && after.valid) {
        require(after.reads>=io.reads && after.sectors>=io.sectors,"block stats regression");
        printf(",\"block_read_ios\":%" PRIu64 ",\"block_read_sectors\":%" PRIu64,after.reads-io.reads,after.sectors-io.sectors);
    } else printf(",\"block_read_ios\":null,\"block_read_sectors\":null");
    printf(",\"mono_start_ns\":%" PRIu64 ",\"mono_finish_ns\":%" PRIu64
           ",\"anon_bytes\":%" PRIu64 ",\"anon_seed\":%" PRIu64
           ",\"anon_checked_words\":%" PRIu64 ",\"anon_live\":%s,\"anon_filled\":%s,\"anon_pinned\":%s",
           s->mono_start,s->mono_finish,s->anon_bytes,s->anon_seed,s->anon_checked_words,
           s->anon?"true":"false",s->anon_filled?"true":"false",s->anon_pinned?"true":"false");
    puts(",\"ok\":true}");
}
static void serve(const char *path,uint64_t vm,const char *upper) {
    require(vm>0 && vm<=1024,"VM ID range"); pin_cpu(); struct state s={.vm=vm};
    s.payload=open_regular(path,O_RDONLY); s.upper=open_regular(upper,O_RDWR|O_CREAT|O_EXCL);
    struct stat st; if(fstat(s.payload,&st)) fatal("payload size");
    require(st.st_size>=(off_t)(160*MIB) && st.st_size<=(off_t)(1024*MIB) && st.st_size%4096==0,"payload must be 160..1024MiB and page aligned");
    s.bytes=(uint64_t)st.st_size;
    FILE *boot=fopen("/proc/sys/kernel/random/boot_id","r"); if(!boot || !fgets(s.boot,sizeof(s.boot),boot)) fatal("boot ID");
    if(fclose(boot)) fatal("close boot ID");
    s.boot[strcspn(s.boot,"\r\n")]=0;
    if(getrandom(&s.cookie,sizeof(s.cookie),0)!=sizeof(s.cookie)) fatal("cookie");
    s.cow=mmap(NULL,PRIVATE_BYTES,PROT_READ|PROT_WRITE,MAP_PRIVATE,s.payload,0);
    if(s.cow==MAP_FAILED) fatal("private mapping");
    mutate(&s,1);
    printf("{\"event\":\"pool_ready\",\"vm\":%" PRIu64 ",\"boot_id\":\"%s\",\"payload_bytes\":%" PRIu64 "}\n",s.vm,s.boot,s.bytes);
    char line[256],*words[5];
    while(fgets(line,sizeof(line),stdin)) {
        require(strchr(line,'\n')!=NULL,"overlong command"); size_t count=0; char *save=NULL;
        for(char *w=strtok_r(line," \t\r\n",&save);w;w=strtok_r(NULL," \t\r\n",&save)) { require(count<5,"too many fields"); words[count++]=w; }
        require(count>=2 && strlen(words[1])<=64,"command/label missing");
        for(char *p=words[1];*p;p++) require(isalnum((unsigned char)*p)||*p=='_'||*p=='-',"label syntax");
        uint64_t before=resident(&s),hash=0; struct io_stats io=stats(); int quit=0;
        s.churn_bytes=0; s.checked_churn_words=0;
        int cpu_before=current_cpu(); s.mono_start=mono_ns(); uint64_t started=now_ns();
        if(!strcmp(words[0],"warm") || !strcmp(words[0],"check")) { require(count==2,"read syntax"); hash=file_pattern(s.payload,s.bytes,TAG,0); }
        else if(!strcmp(words[0],"identity")) require(count==2,"identity syntax");
        else if(!strcmp(words[0],"cachemap")) { require(count==2,"cachemap syntax"); cachemap(&s,words[1]); }
        else if(!strcmp(words[0],"cacheunmap")) { require(count==2,"cacheunmap syntax"); cacheunmap(&s); }
        else if(!strcmp(words[0],"write")) { require(count==3,"write syntax"); mutate(&s,number(words[2])); }
        else if(!strcmp(words[0],"trace")) { require(count==2,"trace syntax"); trace_dump(words[1]); }
        else if(!strcmp(words[0],"wait")) { require(count==3,"wait syntax"); uint64_t ms=number(words[2]); require(ms<=60000,"wait limit"); struct timespec ts={.tv_sec=(time_t)(ms/1000),.tv_nsec=(long)((ms%1000)*1000000)}; while(nanosleep(&ts,&ts) && errno==EINTR) {} }
        else if(!strcmp(words[0],"anon_alloc")) { require(count==3,"anonymous allocate syntax");anon_alloc(&s,number(words[2])); }
        else if(!strcmp(words[0],"anon_fill")) { require(count==2,"anonymous fill syntax");anon_fill(&s); }
        else if(!strcmp(words[0],"anon_verify")) { require(count==2,"anonymous verify syntax");anon_verify(&s); }
        else if(!strcmp(words[0],"anonmap")) { require(count==2,"anonymous map syntax");anon_map(&s,words[1]); }
        else if(!strcmp(words[0],"anon_free")) { require(count==2,"anonymous free syntax");anon_free(&s); }
        else if(!strcmp(words[0],"evict")) { require(count==2,"evict syntax"); evict_payload(&s); }
        else if(!strcmp(words[0],"churn") || !strcmp(words[0],"recycle")) {
            require(count==3 && !s.cachemap,"churn/recycle syntax or pinned validation map");
            uint64_t mib=number(words[2]); require(mib>=1 && mib<=512,"churn range");
            if(!strcmp(words[0],"recycle")) evict_payload(&s);
            churn(&s,mib);
        } else if(!strcmp(words[0],"quit")) { require(count==2,"quit syntax"); quit=1; }
        else require(0,"unknown operation");
        uint64_t finished=now_ns(); s.mono_finish=mono_ns(); int cpu_after=current_cpu();
        require(finished>=started,"RAW clock regressed");
        /* Printing potentially thousands of PFN ranges must not enter timing. */
        print_ranges(&s,words[1]);
        emit(&s,words[1],words[0],hash,before,io,finished-started,cpu_before,cpu_after); if(quit) { require(!s.anon,"cannot quit with held anonymous memory"); if(s.cachemap) cacheunmap(&s); if(munmap(s.cow,PRIVATE_BYTES)||close(s.payload)||close(s.upper)) fatal("close"); return; }
    }
    require(0,"protocol EOF");
}
int main(int argc,char **argv) {
    setvbuf(stdout,NULL,_IONBF,0);
    if(argc==4 && !strcmp(argv[1],"generate")) {
        uint64_t mib=number(argv[3]); require(mib>=160 && mib<=1024,"generator range"); uint64_t bytes=mib*MIB;
        int fd=open_regular(argv[2],O_WRONLY|O_CREAT|O_EXCL); uint64_t hash=file_pattern(fd,bytes,TAG,1);
        if(fsync(fd)||close(fd)) fatal("generated sync");
        printf("{\"bytes\":%" PRIu64 ",\"hash\":\"%016" PRIx64 "\"}\n",bytes,hash);
    } else if(argc==5 && !strcmp(argv[1],"serve")) serve(argv[2],number(argv[3]),argv[4]);
    else { fprintf(stderr,"Usage: pool-compare-probe generate FILE MIB | serve PAYLOAD VM_ID PRIVATE_FILE\n"); return 2; }
    return 0;
}
