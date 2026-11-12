/* Run only in a disposable, owned 4 GiB CXL lab. Exercises ABI 4 ownership,
 * mutation continuity and stale/duplicate handle rejection without full QUERY. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "pool_memory_uapi.h"
_Static_assert(sizeof(struct cpm_config)==56 && sizeof(struct cpm_query)==56 &&
    sizeof(struct cpm_handle)==152 && offsetof(struct cpm_handle,owner_epoch)==56 &&
    offsetof(struct cpm_handle,full_scan_pages)==136,"private mapper UAPI layout");
#define PAGE 4096ULL
#define MIB (1024ULL*1024ULL)
static void check(int ok,const char *why) { if(!ok){perror(why);exit(1);} }
static uint64_t number(const char *path) {
    FILE *f=fopen(path,"r"); char s[100]; check(f&&fgets(s,sizeof(s),f),path);
    check(!fclose(f),"fclose"); return strtoull(s,NULL,0);
}
static int open_pool(void) { int fd=open("/dev/cxl_pool",O_RDWR);check(fd>=0,"open");return fd; }
static void rejected(struct cpm_handle h,const char *why) {
    int fd=open_pool();errno=0;check(ioctl(fd,CPM_IMPORT,&h)==-1,why);close(fd);
}
int main(void) {
    uint64_t base=number("/sys/bus/dax/devices/dax0.0/resource");
    uint64_t bytes=number("/sys/bus/dax/devices/dax0.0/size");
    check(bytes>=4096*MIB,"requires disposable 4GiB pool");
    struct cpm_entry map[3]={{3490*MIB,0},{3490*MIB+PAGE,0},{3490*MIB+2*PAGE,0}};
    struct cpm_create create={.config={.abi=CPM_ABI,.phys_base=base,.device_bytes=bytes,
        .count=3,.entries=(uintptr_t)map},
        .metadata_offset=3584*MIB,.metadata_bytes=64*MIB,.owner_epoch=1,.allocation_generation=100};
    int configured=open_pool();
    create.config.abi=3;check(ioctl(configured,CPM_CREATE,&create)==-1,"old create ABI accepted");
    create.config.abi=CPM_ABI;map[0].flags=1;
    check(ioctl(configured,CPM_CREATE,&create)==-1,"nonprivate frame accepted");
    map[0].flags=0;map[1].offset=map[0].offset;
    check(ioctl(configured,CPM_CREATE,&create)==-1,"duplicate private frame accepted");
    map[1].offset=bytes;
    check(ioctl(configured,CPM_CREATE,&create)==-1,"out of range frame accepted");
    map[1].offset=3490*MIB+PAGE;close(configured);
    int old=open_pool();check(!ioctl(old,CPM_CREATE,&create),"create");
    uint64_t *ram=mmap(NULL,3*PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,old,0);
    check(ram!=MAP_FAILED,"map"); ram[0]=42;ram[PAGE/8]=99;ram[2*PAGE/8]=73;
    struct cpm_handle h;check(!ioctl(old,CPM_STATUS,&h),"status");
    check(h.abi==CPM_ABI&&h.count==3,"private RAM counters");
    struct cpm_handle bad=h;bad.state=2;rejected(bad,"unsealed imported");
    check(ioctl(old,CPM_RETIRE,&h)==-1,"retired live mapping");
    check(!ioctl(old,CPM_SEAL,&h),"seal");
    struct cpm_handle sealed=h;
    bad=h;bad.reserved[0]=1;rejected(bad,"nonzero reserved handle accepted");
    bad=h;bad.cookie_lo^=1;rejected(bad,"wrong cookie accepted");
    bad=h;bad.generation++;rejected(bad,"wrong map generation accepted");
    bad=h;bad.allocation_generation++;rejected(bad,"wrong lease accepted");
    bad=h;bad.owner_epoch++;rejected(bad,"wrong epoch accepted");
    check(!munmap(ram,3*PAGE),"unmap source"); /* no runnable source remains */
    int next=open_pool();check(!ioctl(next,CPM_IMPORT,&h),"import");
    check(h.owner_epoch==2&&h.full_scan_pages==sealed.full_scan_pages,"import scanned or epoch wrong");
    rejected(sealed,"duplicate import accepted");
    check(ioctl(old,CPM_STATUS,&bad)==-1,"old fd still owns mapper");
    ram=mmap(NULL,3*PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,next,0);check(ram!=MAP_FAILED,"target map");
    check(ram[0]==42&&ram[2*PAGE/8]==73&&ram[PAGE/8]==99,"handoff content");
    struct cpm_entry actual[3];
    struct cpm_query query={.count=3,.entries=(uintptr_t)actual};
    check(!ioctl(next,CPM_QUERY,&query)&&query.count==3,"query");
    for(size_t i=0;i<3;i++)check(actual[i].offset==map[i].offset&&!actual[i].flags,"mapping continuity");
    struct cpm_pte ptes[3];struct cpm_audit audit={(uintptr_t)ram,3,(uintptr_t)ptes};
    check(!ioctl(next,CPM_AUDIT,&audit),"audit");
    for(size_t i=0;i<3;i++)check((ptes[i].flags&3)==3&&ptes[i].pfn==(base+map[i].offset)/PAGE,"private PTE");
    check(!ioctl(next,CPM_STATUS,&h),"audit status");sealed.full_scan_pages=h.full_scan_pages;
    check(!ioctl(next,CPM_STATUS,&h)&&h.full_scan_pages==sealed.full_scan_pages,"normal operations scanned");
    check(!munmap(ram,3*PAGE)&&!ioctl(next,CPM_RETIRE,&h),"retire");
    /* Simulate re-promotion while GuestMemory still retains the old fd. A new
     * cookie must isolate it, even at the same VM epoch/metadata address. */
    create.owner_epoch=2;create.allocation_generation=101;
    int reused=open_pool();check(!ioctl(reused,CPM_CREATE,&create),"recreate metadata extent");
    check(!close(next)&&!close(old),"close stale contexts");
    check(!ioctl(reused,CPM_STATUS,&h)&&h.state==1,"old close retired new owner");
    rejected(sealed,"old incarnation accepted after slot reuse");
    check(!ioctl(reused,CPM_RETIRE,&h),"retire reused");
    close(reused);
    puts("POOL_HANDLE_SMOKE_JSON {\"result\":\"passed\",\"handoff_full_scan_pages\":0,\"stale_cookie_rejected\":true,\"stale_epoch_rejected\":true,\"stale_generation_rejected\":true,\"stale_lease_rejected\":true,\"duplicate_import_rejected\":true,\"private_mapping_restored\":true,\"slot_reuse_safe\":true}");
    return 0;
}
