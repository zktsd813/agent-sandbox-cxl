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
#include <unistd.h>

/* Run as root inside the disposable L1. Observe, never modify, target RAM.
 * After guest read verification, compare every mapped HVA's actual PFN to a
 * read-only canonical devDAX alias. A VMA alone is not proof of page sharing. */
static void fail(const char *s) { fprintf(stderr,"pool-pfn-audit: %s (%s)\n",s,strerror(errno)); exit(1); }
static void need(int ok,const char *s) { if(!ok) { errno=EINVAL; fail(s); } }
static uint64_t number(const char *s) { char *e; need(isdigit((unsigned char)*s),"unsigned integer"); errno=0; uint64_t n=strtoull(s,&e,10); need(!errno&&!*e,"integer syntax"); return n; }
static uint64_t entry(int fd,uint64_t hva) { uint64_t value; need(hva<UINT64_C(1)<<63,"HVA bound"); if(pread(fd,&value,8,(off_t)((hva/4096)*8))!=8) fail("pagemap read"); return value; }
int main(int argc,char **argv) {
    need(argc==7,"usage: pool-pfn-audit PID /dev/dax0.0 POOL_OFFSET POOL_BYTES ROWS_TSV SHARED_TSV");
    uint64_t pid=number(argv[1]),off=number(argv[3]),bytes=number(argv[4]);
    need(pid>0 && pid<INT32_MAX && !strcmp(argv[2],"/dev/dax0.0"),"target identity");
    need(off%4096==0 && bytes && bytes%4096==0 && bytes<=UINT64_C(1)<<30,"pool bounds");
    int d=open(argv[2],O_RDONLY|O_CLOEXEC|O_NOFOLLOW); if(d<0) fail("open DAX");
    unsigned char *canonical=mmap(NULL,(size_t)bytes,PROT_READ,MAP_SHARED,d,(off_t)off); if(canonical==MAP_FAILED) fail("canonical map");
    char path[80]; snprintf(path,sizeof(path),"/proc/%" PRIu64 "/pagemap",pid);
    int target=open(path,O_RDONLY|O_CLOEXEC),self=open("/proc/self/pagemap",O_RDONLY|O_CLOEXEC);
    if(target<0||self<0) fail("open pagemap");
    FILE *in=fopen(argv[5],"r"),*out=fopen(argv[6],"wx"); if(!in||!out) fail("open rows");
    uint64_t rows=0,shared=0,private=0,absent=0,hva,pool;
    char line[128],extra;
    while(fgets(line,sizeof(line),in)) {
        need(strchr(line,'\n') && sscanf(line,"%" SCNu64 " %" SCNu64 " %c",&hva,&pool,&extra)==2,"row syntax");
        need(hva%4096==0 && pool>=off && pool-off<=bytes-4096 && pool%4096==0,"aligned row bounds");
        volatile unsigned char touch=canonical[pool-off]; (void)touch;
        uint64_t a=entry(target,hva),b=entry(self,(uintptr_t)(canonical+pool-off));
        const uint64_t mask=(UINT64_C(1)<<55)-1;
        need((b>>63) && (b&mask),"canonical PFN requires root capability"); rows++;
        if(!(a>>63)) absent++;
        else if((a&mask)==(b&mask)) { shared++; if(fprintf(out,"%" PRIu64 "\n",pool)<0) fail("write shared offsets"); }
        else private++;
    }
    if(ferror(in)||fclose(in)||fclose(out)) fail("row IO");
    if(munmap(canonical,(size_t)bytes)||close(d)||close(target)||close(self)) fail("close maps");
    printf("POOL_PFN_JSON {\"rows\":%" PRIu64 ",\"shared_pages\":%" PRIu64 ",\"private_pages\":%" PRIu64 ",\"absent_pages\":%" PRIu64 ",\"shared_bytes\":%" PRIu64 ",\"all_rows_checked\":true}\n",rows,shared,private,absent,shared*4096);
    return 0;
}
