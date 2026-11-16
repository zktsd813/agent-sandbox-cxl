// Plain L0 KVM, no host configuration changes. Host-prefaulted RAM; first
// guest write scan versus repeated scans. Run under node-0 NUMA/quiet locks.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kvm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define MIB (1024ul*1024)
static void fail(const char *s) { perror(s); exit(1); }
static void need(int ok, const char *s) { if(!ok) { fprintf(stderr,"%s\n",s); exit(1); } }
static uint64_t ns(void) { struct timespec t; if(clock_gettime(CLOCK_MONOTONIC,&t))fail("clock"); return t.tv_sec*1000000000ull+t.tv_nsec; }
static void call(int fd, unsigned long op, void *arg) { if(ioctl(fd,op,arg)<0)fail("KVM ioctl"); }
static uint64_t stat_value(int owner, const char *name) {
    int fd=ioctl(owner,KVM_GET_STATS_FD,0); if(fd<0)fail("stats fd");
    struct kvm_stats_header h;
    need(pread(fd,&h,sizeof(h),0)==sizeof(h),"stats header");
    size_t stride=sizeof(struct kvm_stats_desc)+h.name_size;
    need(stride<4096&&h.num_desc<4096,"stats bounds");
    char *data=malloc(stride); need(data!=NULL,"allocate stats");
    uint64_t value=0; int found=0;
    for(unsigned i=0;i<h.num_desc;i++) {
        need(pread(fd,data,stride,h.desc_offset+i*stride)==(ssize_t)stride,"stats descriptor");
        struct kvm_stats_desc *d=(void *)data;
        need(memchr(d->name,0,h.name_size)!=NULL,"stat name");
        if(!strcmp(d->name,name)) {
            need(pread(fd,&value,8,h.data_offset+d->offset)==8,"stat data"); found=1; break;
        }
    }
    need(found,"requested statistic unavailable"); free(data); close(fd); return value;
}
int main(int argc,char **argv) {
    need(argc==3,"usage: physical-ept-cost 4k|2m WORKING_SET_MiB");
    int huge=!strcmp(argv[1],"2m");
    need(huge||!strcmp(argv[1],"4k"),"page size");
    unsigned long mib=strtoul(argv[2],NULL,10);
    need(mib>=64&&mib<=2048&&!(mib%2),"working set 64..2048MiB, aligned to 2MiB");
    size_t bytes=(mib+2)*MIB,alignment=2*MIB;
    void *allocation=mmap(NULL,bytes+alignment,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(allocation==MAP_FAILED)fail("RAM mmap");
    uintptr_t aligned=((uintptr_t)allocation+alignment-1)&~(alignment-1);
    if(aligned>(uintptr_t)allocation)munmap(allocation,aligned-(uintptr_t)allocation);
    size_t suffix=(uintptr_t)allocation+bytes+alignment-aligned-bytes;
    if(suffix)munmap((void *)(aligned+bytes),suffix);
    unsigned char *ram=(void *)aligned;
    if(madvise(ram,bytes,huge?MADV_HUGEPAGE:MADV_NOHUGEPAGE))fail("RAM advice");
    memset(ram,0,bytes); // Resolve host allocation/zero faults before timing.
    int collapse=0,collapse_errno=0;
    if(huge) { collapse=madvise(ram,bytes,25); collapse_errno=collapse?errno:0; }
    int kvm=open("/dev/kvm",O_RDWR|O_CLOEXEC); if(kvm<0)fail("open KVM");
    int vm=ioctl(kvm,KVM_CREATE_VM,0); if(vm<0)fail("create VM");
    struct kvm_userspace_memory_region slot={.slot=0,.guest_phys_addr=0,.memory_size=bytes,.userspace_addr=aligned};
    call(vm,KVM_SET_USER_MEMORY_REGION,&slot);
    int vcpu=ioctl(vm,KVM_CREATE_VCPU,0); if(vcpu<0)fail("create vCPU");
    int run_size=ioctl(kvm,KVM_GET_VCPU_MMAP_SIZE,0);
    struct kvm_run *run=mmap(NULL,run_size,PROT_READ|PROT_WRITE,MAP_SHARED,vcpu,0);
    if(run==MAP_FAILED)fail("run mmap");
    struct kvm_sregs sr; call(vcpu,KVM_GET_SREGS,&sr);
    struct kvm_segment cs={.base=0,.limit=0xffffffff,.selector=8,.type=11,.present=1,.db=1,.s=1,.g=1};
    sr.cs=cs; cs.type=3; cs.selector=16; sr.ds=sr.es=sr.fs=sr.gs=sr.ss=cs;
    sr.cr0=(sr.cr0|1)&~((1ul<<31)|(1ul<<30)|(1ul<<29)); sr.cr4=0; sr.efer=0;
    call(vcpu,KVM_SET_SREGS,&sr);
    // 32-bit flat guest, paging disabled. One write per 4KiB data page.
    // mov esi,2MiB; mov ecx,pages; inc byte [esi]; add esi,4096;
    // loop; out 0xe9,al; jmp back. Only EPT translates the GPA.
    unsigned char code[]={0xbe,0,0,0x20,0,0xb9,0,0,0,0,
                          0xfe,0x06,0x81,0xc6,0,0x10,0,0,0xe2,0xf6,
                          0xb0,1,0xe6,0xe9,0xeb,0xe6};
    uint32_t pages=mib*MIB/4096; memcpy(code+6,&pages,4); memcpy(ram+0x1000,code,sizeof(code));
    struct kvm_regs regs={.rip=0x1000,.rsp=0x1ff000,.rflags=2}; call(vcpu,KVM_SET_REGS,&regs);
    printf("{\"page_mode\":\"%s\",\"working_set_mib\":%lu,\"collapse_result\":%d,\"collapse_errno\":%d,\"ept_reset\":\"delete and re-add identical memslot while vCPU stopped at PIO\"",argv[1],mib,collapse,collapse_errno);
    for(unsigned phase=0;phase<2;phase++) {
      if(phase) {
        // Remove only this VM's slot. KVM zaps its EPT; the host mapping,
        // physical RAM, guest registers and data cache stay in place.
        slot.memory_size=0; call(vm,KVM_SET_USER_MEMORY_REGION,&slot);
        need(!stat_value(vm,"pages_4k")&&!stat_value(vm,"pages_2m")&&!stat_value(vm,"pages_1g"),"memslot removal did not clear EPT leaves");
        slot.memory_size=bytes; call(vm,KVM_SET_USER_MEMORY_REGION,&slot);
      }
      printf(",\"%s\":[",phase?"scans":"cold_scans");
      for(unsigned scan=0;scan<6;scan++) {
        uint64_t faults_before=stat_value(vcpu,"pf_fixed"),begin=ns();
        int rc; do { rc=ioctl(vcpu,KVM_RUN,0); } while(rc<0&&errno==EINTR);
        uint64_t elapsed=ns()-begin;
        need(rc==0&&run->exit_reason==KVM_EXIT_IO&&run->io.direction==KVM_EXIT_IO_OUT&&run->io.port==0xe9,"scan guest exit");
        uint64_t fixed=stat_value(vcpu,"pf_fixed")-faults_before;
        printf("%s{\"host_ns\":%"PRIu64",\"pf_fixed\":%"PRIu64",\"pages_4k\":%"PRIu64",\"pages_2m\":%"PRIu64",\"pages_1g\":%"PRIu64"}",
            scan?",":"",elapsed,fixed,stat_value(vm,"pages_4k"),stat_value(vm,"pages_2m"),stat_value(vm,"pages_1g"));
      }
      putchar(']');
    }
    puts("}");
    // Check that every page was reached on every scan.
    for(size_t offset=2*MIB;offset<bytes;offset+=4096)need(ram[offset]==12,"scan did not visit each page twelve times");
    munmap(run,run_size); close(vcpu); close(vm); close(kvm); munmap(ram,bytes); return 0;
}
