#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

/* Bounded import/poison for the disposable lab. The controller fences the
 * source and owns the specified disjoint allocation. This is not a lease. */
static void die(const char *s) { fprintf(stderr,"pool-dax-copy: %s: %s\n",s,strerror(errno)); exit(1); }
static void need(int c,const char *s) { if(!c) { errno=EINVAL; die(s); } }
static uint64_t number(const char *s) {
    char *e; need(*s && isdigit((unsigned char)*s),"unsigned integer"); errno=0;
    uint64_t n=strtoull(s,&e,10); need(!errno && !*e,"integer syntax"); return n;
}
static uint64_t attr(const char *name) {
    char path[160],line[80]; snprintf(path,sizeof(path),"/sys/bus/dax/devices/dax0.0/%s",name);
    FILE *f=fopen(path,"r"); if(!f || !fgets(line,sizeof(line),f)) die("read attribute");
    if(fclose(f)) die("close attribute");
    line[strcspn(line,"\n")]=0; return number(line);
}
int main(int argc,char **argv) {
    need(argc==6,"usage: pool-dax-copy import|verify|poison FILE /dev/dax0.0 OFFSET BYTES");
    int poison=!strcmp(argv[1],"poison"),verify=!strcmp(argv[1],"verify"); need(poison || verify || !strcmp(argv[1],"import"),"operation");
    need(!strcmp(argv[3],"/dev/dax0.0"),"owned DAX path");
    uint64_t off=number(argv[4]),bytes=number(argv[5]),align=attr("align"),capacity=attr("size");
    need(align==4096 && off>=2*1024*1024 && off%align==0 && bytes && bytes%align==0 && off<=capacity && bytes<=capacity-off,"bounded aligned span");
    int d=open(argv[3],(verify?O_RDONLY:O_RDWR)|O_CLOEXEC|O_NOFOLLOW); if(d<0) die("open DAX");
    struct stat ds; if(fstat(d,&ds)) die("DAX stat"); need(S_ISCHR(ds.st_mode),"DAX character device");
    char identity[80]; FILE *f=fopen("/sys/bus/dax/devices/dax0.0/dev","r");
    unsigned maj,min; need(f && fgets(identity,sizeof(identity),f) && sscanf(identity,"%u:%u",&maj,&min)==2,"DAX identity");
    if(fclose(f)) die("identity close");
    need(major(ds.st_rdev)==maj && minor(ds.st_rdev)==min,"DAX device mismatch");
    unsigned char *p=mmap(NULL,(size_t)bytes,PROT_READ|(verify?0:PROT_WRITE),MAP_SHARED,d,(off_t)off); if(p==MAP_FAILED) die("DAX mmap");
    uint64_t hash=UINT64_C(14695981039346656037);
    if(poison) {
        need(getenv("POOL_ALLOW_POISON") && !strcmp(getenv("POOL_ALLOW_POISON"),"1"),"poison requires controller opt-in");
        memset(p,0xa7,(size_t)bytes);
        for(uint64_t i=0;i<bytes;i++) need(p[i]==0xa7,"poison compare");
    } else {
        int in=open(argv[2],O_RDONLY|O_CLOEXEC|O_NOFOLLOW); if(in<0) die("input open");
        struct stat st; if(fstat(in,&st)) die("input stat"); need(S_ISREG(st.st_mode) && st.st_size>=0 && (uint64_t)st.st_size==bytes,"exact regular file size");
        unsigned char buffer[65536];
        for(uint64_t pos=0;pos<bytes;) {
            size_t n=bytes-pos<sizeof(buffer)?(size_t)(bytes-pos):sizeof(buffer),done=0;
            while(done<n) { ssize_t got=pread(in,buffer+done,n-done,(off_t)(pos+done)); if(got<0 && errno==EINTR) continue; if(got<=0) die("input read"); done+=(size_t)got; }
            if(!verify) memcpy(p+pos,buffer,n);
            need(!memcmp(p+pos,buffer,n),"full copy comparison");
            for(size_t i=0;i<n;i++) hash=(hash^buffer[i])*UINT64_C(1099511628211);
            pos+=n;
        }
        if(close(in)) die("input close");
    }
    /* Coherent QEMU lab only. This does not establish a physical multi-host
     * noncoherent CXL publication protocol. */
    if(munmap(p,(size_t)bytes)||close(d)) die("DAX close");
    printf("POOL_DAX_COPY_JSON {\"operation\":\"%s\",\"offset\":%" PRIu64 ",\"bytes\":%" PRIu64 ",\"hash\":\"%016" PRIx64 "\",\"full_compare\":true,\"result\":\"passed\"}\n",argv[1],off,bytes,hash);
    return 0;
}
