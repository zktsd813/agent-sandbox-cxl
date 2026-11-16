// Exercise only the disposable L1 prefault ABI; never run on the L0 kernel.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/kvm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include CXL_KVM_PREFAULT_HEADER

#define PAGE 4096UL
static unsigned checks;
static void need(int ok, const char *name) {
    if (!ok) { fprintf(stderr,"prefault probe: %s: %s\n",name,strerror(errno)); exit(1); }
}
static void expect(int fd, struct kvm_lab_mmu_request q, int error, const char *name) {
    errno=0; int rc=ioctl(fd,KVM_LAB_MMU_RANGE,&q);
    need(error ? rc==-1 && errno==error : rc==0,name); checks++;
}
int main(void) {
    int kvm=open("/dev/kvm",O_RDWR|O_CLOEXEC); need(kvm>=0,"open KVM");
    int vm=ioctl(kvm,KVM_CREATE_VM,0); need(vm>=0,"create VM");
    void *ram=mmap(NULL,16*PAGE,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    need(ram!=MAP_FAILED,"RAM"); need(!madvise(ram,16*PAGE,MADV_NOHUGEPAGE),"4K backing");
    memset(ram,0,16*PAGE);
    struct kvm_userspace_memory_region slot={.slot=0,.memory_size=16*PAGE,.userspace_addr=(uintptr_t)ram};
    need(!ioctl(vm,KVM_SET_USER_MEMORY_REGION,&slot),"register RAM");
    int cpu=ioctl(vm,KVM_CREATE_VCPU,0); need(cpu>=0,"create vCPU");
    struct kvm_lab_mmu_request q={.abi=1,.operation=KVM_LAB_MMU_QUERY,.bytes=16*PAGE};
    need(!ioctl(cpu,KVM_LAB_MMU_RANGE,&q) && q.count==0,"empty root query");
    q.abi=0; expect(cpu,q,EINVAL,"unknown ABI"); q.abi=1;
    q.operation=99; expect(cpu,q,EINVAL,"unknown operation"); q.operation=KVM_LAB_MMU_QUERY;
    q.flags=1; expect(cpu,q,EINVAL,"query flags"); q.flags=0;
    q.gpa=1; expect(cpu,q,EINVAL,"unaligned query"); q.gpa=0;
    q.bytes=17*PAGE; expect(cpu,q,EINVAL,"RAM hole"); q.bytes=16*PAGE;
    q.capacity=(1UL<<20)+1; expect(cpu,q,EINVAL,"range budget"); q.capacity=0;
    struct kvm_lab_mmu_range ranges[2]={{PAGE,2*PAGE},{8*PAGE,2*PAGE}};
    q=(struct kvm_lab_mmu_request){.abi=1,.operation=KVM_LAB_MMU_FILL,.ranges=(uintptr_t)ranges,.count=2};
    ranges[0].gpa=1; expect(cpu,q,EINVAL,"unaligned fill"); ranges[0].gpa=PAGE;
    ranges[1].gpa=PAGE; expect(cpu,q,EINVAL,"overlap"); ranges[1].gpa=8*PAGE;
    q.ranges=1; expect(cpu,q,EFAULT,"bad userspace vector"); q.ranges=(uintptr_t)ranges;
    ranges[0].gpa=UINT64_MAX-PAGE+1; expect(cpu,q,EINVAL,"overflow"); ranges[0].gpa=PAGE;
    need(!ioctl(cpu,KVM_LAB_MMU_RANGE,&q) && q.processed_pages==4,"prefetch fill four pages");
    q=(struct kvm_lab_mmu_request){.abi=1,.operation=KVM_LAB_MMU_QUERY,.bytes=16*PAGE};
    need(ioctl(cpu,KVM_LAB_MMU_RANGE,&q)==-1 && errno==ENOSPC && q.count==2,"query count");
    struct kvm_lab_mmu_range found[2]={{0}};
    q.capacity=1; q.ranges=(uintptr_t)found; expect(cpu,q,ENOSPC,"small output buffer");
    q.capacity=2; need(!ioctl(cpu,KVM_LAB_MMU_RANGE,&q) && q.count==2 && q.processed_pages==4 &&
        !memcmp(found,ranges,sizeof(ranges)),"exact mapped ranges");
    q=(struct kvm_lab_mmu_request){.abi=1,.operation=KVM_LAB_MMU_FILL,.flags=KVM_LAB_MMU_ACTIVATE,
        .ranges=(uintptr_t)ranges,.count=2};
    need(!ioctl(cpu,KVM_LAB_MMU_RANGE,&q) && q.processed_pages==4,"activate existing pages");
    int second=ioctl(vm,KVM_CREATE_VCPU,1); need(second>=0,"second vCPU");
    expect(cpu,q,EOPNOTSUPP,"reject multiple vCPUs");
    close(second); close(cpu); close(vm); close(kvm); munmap(ram,16*PAGE);
    printf("PREFAULT_PROBE_JSON {\"result\":\"passed\",\"negative_checks\":%u,\"mapped_pages\":4,\"ranges\":2,\"guest_runs\":0}\n",checks);
    return 0;
}
