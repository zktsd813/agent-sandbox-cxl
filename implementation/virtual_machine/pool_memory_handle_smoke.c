/* Run only in a disposable, owned 4 GiB CXL lab. Exercises ABI 2 ownership,
 * mutation continuity and stale/duplicate handle rejection without full QUERY. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "pool_memory_uapi.h"
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
    int dax=open("/dev/dax0.0",O_RDWR);check(dax>=0,"dax");
    uint64_t *canonical=mmap(NULL,PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,dax,3500*MIB);
    check(canonical!=MAP_FAILED,"canonical");
    for(size_t i=0;i<PAGE/8;i++)canonical[i]=0xabcddcba00000000ULL^i;
    struct cpm_entry map[3]={{3500*MIB,CPM_SHARED},{3500*MIB,CPM_SHARED},{3490*MIB,0}};
    uint64_t credits[3]={3490*MIB+PAGE,3490*MIB+2*PAGE,3490*MIB+3*PAGE};
    struct cpm_create create={.config={CPM_ABI,base,bytes,3500*MIB,PAGE,3,(uintptr_t)map,3,(uintptr_t)credits},
        .metadata_offset=3584*MIB,.metadata_bytes=64*MIB,.owner_epoch=1,.allocation_generation=100};
    int old=open_pool();check(!ioctl(old,CPM_CREATE,&create),"create");
    uint64_t *ram=mmap(NULL,3*PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,old,0);
    check(ram!=MAP_FAILED,"map"); ram[0]=42;ram[2*PAGE/8]=73;
    struct cpm_handle h;check(!ioctl(old,CPM_STATUS,&h),"status");
    check(h.cow_pages==1&&h.shared_pages==1&&h.credit_count==2,"CoW counters");
    struct cpm_handle bad=h;bad.state=2;rejected(bad,"unsealed imported");
    check(ioctl(old,CPM_RETIRE,&h)==-1,"retired live mapping");
    check(!ioctl(old,CPM_SEAL,&h),"seal");
    struct cpm_handle sealed=h;
    bad=h;bad.cookie_lo^=1;rejected(bad,"wrong cookie accepted");
    bad=h;bad.generation++;rejected(bad,"wrong map generation accepted");
    bad=h;bad.allocation_generation++;rejected(bad,"wrong lease accepted");
    bad=h;bad.owner_epoch++;rejected(bad,"wrong epoch accepted");
    uint64_t extra=3490*MIB+4*PAGE;
    struct cpm_credits grant={1,(uintptr_t)&extra};
    check(ioctl(old,CPM_ADD_CREDITS,&grant)==-1,"sealed mutation accepted");
    check(!munmap(ram,3*PAGE),"unmap source"); /* no runnable source remains */
    int next=open_pool();check(!ioctl(next,CPM_IMPORT,&h),"import");
    check(h.owner_epoch==2&&h.full_scan_pages==sealed.full_scan_pages,"import scanned or epoch wrong");
    rejected(sealed,"duplicate import accepted");
    check(ioctl(old,CPM_STATUS,&bad)==-1,"old fd still owns mapper");
    ram=mmap(NULL,3*PAGE,PROT_READ|PROT_WRITE,MAP_SHARED,next,0);check(ram!=MAP_FAILED,"target map");
    check(ram[0]==42&&ram[2*PAGE/8]==73&&ram[PAGE/8]==canonical[0],"handoff content");
    unsigned char selected[3];struct cpm_select select={0,3,(uintptr_t)selected};
    check(!ioctl(next,CPM_SELECT,&select)&&selected[0]&&selected[1]&&!selected[2],"lineage lost across import");
    struct cpm_range discard={(uintptr_t)ram+PAGE,1,1};
    check(!ioctl(next,CPM_DISCARD,&discard),"discard after import");
    for(size_t i=0;i<PAGE/8;i++)check(ram[PAGE/8+i]==0&&canonical[i]==(0xabcddcba00000000ULL^i),"discard/canonical");
    check(!ioctl(next,CPM_SELECT,&select)&&!selected[1],"discard lineage retained");
    uint64_t mixed[2]={extra,credits[0]};struct cpm_credits duplicate={2,(uintptr_t)mixed};
    check(ioctl(next,CPM_ADD_CREDITS,&duplicate)==-1,"duplicate credit accepted");
    check(!ioctl(next,CPM_ADD_CREDITS,&grant),"failed grant did not roll back bitmap");
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
    close(reused);munmap(canonical,PAGE);close(dax);
    puts("POOL_HANDLE_SMOKE_JSON {\"result\":\"passed\",\"handoff_full_scan_pages\":0,\"stale_cookie_rejected\":true,\"stale_epoch_rejected\":true,\"stale_generation_rejected\":true,\"stale_lease_rejected\":true,\"duplicate_import_rejected\":true,\"lineage_restored\":true,\"credit_rollback\":true,\"slot_reuse_safe\":true}");
    return 0;
}
