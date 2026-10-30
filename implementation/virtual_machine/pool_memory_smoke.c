#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <signal.h>
#include <unistd.h>
#include "pool_memory_uapi.h"

#define PAGE 4096ULL
#define MIB (1024ULL*1024ULL)
static void fail(const char *what) { perror(what);exit(1); }
static void check(int condition,const char *what) { if(!condition){errno=EINVAL;fail(what);} }
static uint64_t number_file(const char *path) {
    char b[96];FILE *f=fopen(path,"r");if(!f||!fgets(b,sizeof(b),f))fail(path);
    if(fclose(f))fail("close attribute");
    char *end;errno=0;
    uint64_t n=strtoull(b,&end,0);check(!errno&&end!=b,"attribute number");return n;
}
static int configured(uint64_t base,uint64_t bytes,uint64_t canonical,
                      struct cpm_entry *entries,size_t count,uint64_t *credits,size_t ncredits) {
    int fd=open("/dev/cxl_pool",O_RDWR|O_CLOEXEC);if(fd<0)fail("open cxl pool");
    struct cpm_config cfg={.abi=CPM_ABI,.phys_base=base,.device_bytes=bytes,
        .canonical_start=canonical,.canonical_bytes=PAGE,.count=count,.entries=(uintptr_t)entries,
        .credit_count=ncredits,.credits=(uintptr_t)credits};
    if(ioctl(fd,CPM_CONFIG,&cfg))fail("CPM_CONFIG");
    return fd;
}
int main(void) {
    uint64_t base=number_file("/sys/bus/dax/devices/dax0.0/resource");
    uint64_t bytes=number_file("/sys/bus/dax/devices/dax0.0/size");
    uint64_t canonical=3500*MIB,private=3490*MIB;
    check(bytes>=canonical+PAGE,"smoke requires 4GiB isolated DAX");
    int dax=open("/dev/dax0.0",O_RDWR|O_CLOEXEC);if(dax<0)fail("open dax");
    volatile uint64_t *original=mmap(NULL,PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,dax,(off_t)canonical);
    if(original==MAP_FAILED)fail("canonical map");
    for(uint64_t i=0;i<PAGE/8;i++)original[i]=UINT64_C(0xabcddcba00000000)^i;
    struct cpm_entry entries[3]={{canonical,CPM_SHARED},{canonical,CPM_SHARED},{private,0}};
    uint64_t credits[3]={private+PAGE,private+2*PAGE,private+3*PAGE};
    int fd=configured(base,bytes,canonical,entries,3,credits,3);
    volatile uint64_t *ram=mmap(NULL,3*PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    if(ram==MAP_FAILED)fail("logical RAM map");
    struct cpm_entry peer_entry={canonical,CPM_SHARED};
    int peer_fd=configured(base,bytes,canonical,&peer_entry,1,NULL,0);
    volatile uint64_t *peer=mmap(NULL,PAGE,PROT_READ,MAP_SHARED,peer_fd,0);
    if(peer==MAP_FAILED)fail("peer map");
    for(uint64_t i=0;i<PAGE/8;i++)check(ram[i]==original[i]&&ram[PAGE/8+i]==original[i]&&peer[i]==original[i],"initial aliases");
    ram[0]=UINT64_C(0x1111222233334444);
    for(uint64_t i=0;i<PAGE/8;i++) {
        check(original[i]==(UINT64_C(0xabcddcba00000000)^i)&&peer[i]==original[i],"CoW changed canonical/peer");
        check(ram[i]==(i?original[i]:UINT64_C(0x1111222233334444)),"CoW lost untouched bytes");
    }
    struct cpm_range range={.address=(uintptr_t)ram+PAGE,.first=1,.count=1};
    if(ioctl(fd,CPM_DISCARD,&range))fail("CPM_DISCARD");
    for(uint64_t i=0;i<PAGE/8;i++)check(ram[PAGE/8+i]==0&&peer[i]==original[i],"discard copied old canonical");
    ram[PAGE/8]=UINT64_C(0x5555666677778888);
    ram[2*PAGE/8]=UINT64_C(0x9999aaaabbbbcccc);
    struct cpm_entry actual[3];uint64_t unused[3];
    struct cpm_query query={.count=3,.entries=(uintptr_t)actual,.credit_count=3,.credits=(uintptr_t)unused};
    if(ioctl(fd,CPM_QUERY,&query))fail("CPM_QUERY");
    check(query.cow_pages==1&&query.discard_pages==1&&query.credit_count==1&&!query.exhausted,"unexpected mapper accounting");
    check(actual[0].flags==0&&actual[1].flags==0&&actual[2].offset==private,"incorrect mapper transitions");
    check(actual[0].offset!=actual[1].offset&&actual[0].offset!=private&&actual[1].offset!=private,"private alias");
    uint64_t generation=query.generation;
    struct cpm_credits duplicate={.count=1,.offsets=(uintptr_t)unused};
    errno=0;check(ioctl(fd,CPM_ADD_CREDITS,&duplicate)==-1&&errno==EINVAL,"duplicate unused credit accepted");
    query.count=3;query.credit_count=3;
    if(ioctl(fd,CPM_QUERY,&query))fail("query after invalid credit");
    check(query.generation==generation&&query.credit_count==1,"invalid credit mutated context");
    errno=0;check(mremap((void*)ram,3*PAGE,4*PAGE,MREMAP_MAYMOVE)==MAP_FAILED,"unsupported mremap accepted");
    void *relocation=mmap(NULL,3*PAGE,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(relocation==MAP_FAILED)fail("reserve mremap target");
    errno=0;check(mremap((void*)ram,3*PAGE,3*PAGE,MREMAP_MAYMOVE|MREMAP_FIXED,relocation)==MAP_FAILED,
                  "same-size relocation accepted");
    check(ram[0]==UINT64_C(0x1111222233334444),"failed relocation lost original owner mapping");
    /* Failure may already have removed the target reservation; munmap accepts
     * unmapped pages and never touches the original owner range. */
    if(munmap(relocation,3*PAGE))fail("release relocation reservation");
    void *alias=mmap(NULL,3*PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    check(alias==MAP_FAILED,"second VMA alias accepted after rejected mremap");
    errno=0;check(mprotect((void*)(ram+PAGE/8),PAGE,PROT_READ)==-1,"partial protection split accepted");
    ram[2*PAGE/8]=UINT64_C(0x123456789abcdef0);
    check(ram[2*PAGE/8]==UINT64_C(0x123456789abcdef0),"private read-to-write failed");
    uint64_t first_credit=private+4*PAGE;
    int first_fd=configured(base,bytes,canonical,&peer_entry,1,&first_credit,1);
    volatile uint64_t *first=mmap(NULL,PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,first_fd,0);
    if(first==MAP_FAILED)fail("first-write map");
    first[0]=UINT64_C(0x4321123443211234); /* no read installed a PTE first */
    for(uint64_t i=0;i<PAGE/8;i++)check(first[i]==(i?original[i]:UINT64_C(0x4321123443211234)),"first-write CoW content");
    if(munmap((void*)first,PAGE)||close(first_fd))fail("close first-write context");
    pid_t child=fork();if(child<0)fail("fork no-credit test");
    if(!child) {
        struct rlimit limit={0,0};setrlimit(RLIMIT_CORE,&limit);
        int exhausted=configured(base,bytes,canonical,&peer_entry,1,NULL,0);
        volatile uint64_t *p=mmap(NULL,PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,exhausted,0);
        if(p==MAP_FAILED)_exit(22);
        p[0]=0;_exit(23);
    }
    int status;if(waitpid(child,&status,0)!=child)fail("wait no-credit child");
    check(WIFSIGNALED(status)&&WTERMSIG(status)==SIGBUS,"no-credit write did not fail closed");
    for(uint64_t i=0;i<PAGE/8;i++)check(original[i]==(UINT64_C(0xabcddcba00000000)^i)&&peer[i]==original[i],"negative test changed canonical");
    printf("POOL_MEMORY_SMOKE_JSON {\"result\":\"passed\",\"pages\":3,\"cow_pages\":%llu,\"discard_pages\":%llu,"
           "\"unused_credits\":%llu,\"faults\":%llu,\"canonical_full_words_checked\":512,\"peer_full_words_checked\":512,"
           "\"alias_rejected\":true,\"mremap_rejected\":true,\"split_rejected\":true,\"duplicate_credit_rejected\":true,"
           "\"no_credit_sigbus\":true,\"first_write_cow\":true,\"private_write\":true}\n",
           query.cow_pages,query.discard_pages,query.credit_count,query.faults);
    if(munmap((void*)ram,3*PAGE)||munmap((void*)peer,PAGE)||munmap((void*)original,PAGE)||
       close(fd)||close(peer_fd)||close(dax))fail("close smoke");
    return 0;
}
