# 비일관 CXL hand-off

2026-11-17 설계. 0단계 기준선은 `a1b5848`에서 통과했다.
Compute Express Link(CXL)의 서버 사이 cache 일관성을 가정하지 않는다.
가상 머신(VM, Virtual Machine)의 실행 소유자는 항상 하나다.
아래는 구현 계약이다. 시험 결과와 미완료 항목은 결과 문서에 따로 적는다.

## guest RAM 쓰기와 장부

주기억장치(RAM, Random Access Memory)는 4 KiB page 단위로 기록한다.
장부는 생성 때부터 켠다. 중간에 켜면 첫 정리에서 RAM 전체를 처리한다.

| 쓰는 주체 | 기록 경로 | 주의할 점 |
|---|---|---|
| guest CPU | KVM(Kernel-based Virtual Machine) memslot dirty bitmap | 새 쓰기 가능 SPTE(Shadow Page Table Entry)는 make_spte가 기록한다 |
| 기존 EPT(Extended Page Tables) mapping의 CPU 쓰기 | PML(Page Modification Logging), 또는 쓰기 보호 fault | VM exit에서 PML buffer를 bitmap으로 옮긴다 |
| KVM kvm_write_guest, pvclock | KVM dirty bitmap | gfn_to_pfn_cache 쓰기도 포함한다 |
| Firecracker boot loader와 GuestMemory write | vm-memory bitmap | 생성 때부터 track_dirty_pages를 켠다 |
| block 읽기, network와 vsock 수신 | vm-memory bitmap | iovec와 async 완료 경로를 확인한다. drain 뒤에 걷는다 |
| virtqueue used ring | vm-memory bitmap과 queue 범위 | raw pointer를 저장하므로 bitmap reset 뒤에도 queue page를 보수적으로 포함한다 |
| VM 생성의 0 채우기 | 명시적 전체 범위 표시 | raw pointer 뒤 bitmap 표시와 전체 범위 내려쓰기가 필요하다 |
| promotion/demotion 복사 | 명시적 전체 범위 표시 | 복사 목적지를 기록한다. CXL promotion은 전체 RAM record를 발행한다 |

KVM_GET_DIRTY_LOG 기본 모드로 bitmap을 걷는다.
수동 보호 모드는 켜지 않는다. dirty ring은 계속 거절한다.
KVM bitmap과 Firecracker bitmap의 합집합을 쓴다.
장치 save와 비동기 I/O(Input/Output) drain이 끝난 뒤에 걷는다.
소유하지 않은 VMM(Virtual Machine Monitor)의 로컬 bitmap은 공유하지 않는다.
오래 남은 표시가 섞여도 허용한다. 누락은 허용하지 않는다.

## raw pointer 감사

기준은 Firecracker 원본 95f868c8에 현재 patch를 적용한 tree다.
검색 원문은 `.local/run18/raw-pointer-audit-all.txt`에 둔다.
테스트와 generated 구조체 초기화는 guest RAM 쓰기와 구분한다.

| 위치 | 동작 | 처리 |
|---|---|---|
| vstate/memory.rs devdax initialize의 write_bytes | guest RAM 전체 0 채우기 | 전체 bitmap 표시와 범위 내려쓰기 |
| pool_memory.rs promotion copy_nonoverlapping | DRAM에서 CXL로 전체 복사 | 목적지 전체 표시, 내려쓰기, 전체 record |
| pool_memory.rs demotion copy_nonoverlapping | CXL에서 DRAM으로 전체 복사 | 목적지 전체 표시. 공유 범위의 소유권 종료와 구분 |
| devices/virtio/iovec.rs IoVecBufferMut | guest 주소를 libc iovec로 변환 | 변환 전에 bitmap mark_dirty |
| devices/virtio/block/virtio/io/async_io.rs | io_uring이 guest 주소에 쓰기 | 완료 수집에서 mark_dirty. save의 drain이 먼저다 |
| devices/virtio/queue.rs get_aligned_slice_ptr와 used_ring_* | 저장한 pointer로 used ring 쓰기 | 매 정리 시 queue 범위를 다시 포함 |
| devices/virtio/net/tap.rs, vsock packet | iovec/VolatileSlice로 수신 | 위 iovec와 vm-memory 표시를 따라간다 |
| vstate/memory.rs mprotect, madvise, mincore | 주소/상주 상태 syscall | guest RAM 내용 쓰기가 아니다 |
| direct.rs ioctl, pool_memory.rs Query/Audit | 로컬 출력 buffer에 커널이 쓰기 | guest RAM 내용 쓰기가 아니다 |
| devices/vmclock.rs | clock page 갱신 | GuestMemory.write_obj의 bitmap을 따른다 |
| resident_control.c heartbeat epoch | 시험 ACK trigger | export 뒤 raw 쓰기를 제거한다. 정지 후 VMM의 GuestMemory.write_obj로 옮긴다 |
| iovec 테스트의 copy_nonoverlapping, memory 테스트의 write_bytes | 테스트 전용 | 생산 경로와 분리 |

Direct의 기존 장치 계약은 read-only virtio block이다.
Network와 vsock은 장부 경로 감사와 실제 hand-off 지원을 구분해 검증한다.
장치를 거절하는 기존 검사를 단순히 지우지 않는다.
Balloon, memory device, pmem, vhost는 이 direct 장치 계약에서 거절한다.
KVM의 steal-time과 asynchronous page fault도 기존 preflight에서 거절한다.
vcpu_load의 pvclock 갱신 요청은 다음 KVM_RUN에서 처리한다.
장부 수집 뒤 실제 guest RAM에 쓰는 허용 경로는 확인되지 않았다.

## KVM 객체와 제어 영역

공유 bump arena에는 VMCS(Virtual Machine Control Structure), VM/vCPU 객체,
EPT page와 CPU가 바꾼 accessed/dirty bit, PML page, dirty bitmap,
kvm_run page가 있다. dirty bitmap의 두 배 크기 전체를 DAX에 할당한다.
PML page도 DAX에 둔다. detach는 모든 vCPU의 PML index가 초기값인지 검사한다.
VMCS를 VMCLEAR하고 모든 observer를 drain한 뒤 arena를 내려쓴다.
이번 범위는 VM별 할당이 아닌 `[base, bump)` 전체다. byte와 line 수를 기록한다.

Token, mailbox, mapper metadata와 쓰기 기록은 guest RAM과 다른 영역이다.
어느 영역을 누가 썼는지 정답지에서 따로 센다.
Attach는 공유 header를 무효화하고 범위를 검사한 뒤 객체 arena를 무효화한다.
그 뒤에만 VM과 vCPU 객체를 읽는다.
소유하지 않은 host의 kernel 경로 감사와 영역 재사용은 다음 brief 범위다.

## 쓰기 기록 ABI

ABI(Application Binary Interface)는 little-endian 고정 폭 정수다.
VM별 고정 CXL 영역에 header와 K=8개의 slot을 둔다.
Header에는 magic, version, RAM page 수, VM identity, next_epoch, K가 있다.
각 slot에는 epoch, page 수, RAM page bitmap이 있다.
Header와 slot은 cache line 경계에 맞춘다. page bit i는 GPA(Guest Physical Address) i*4096이다.
2 GiB RAM의 bitmap은 64 KiB다. slot 여덟 개는 약 512 KiB다.
Log는 현재 소유 host 하나만 쓴다. 받는 host는 log를 고치지 않는다.
Host별 consumed는 그 host의 DRAM에 둔다. 자신이 쓴 epoch도 consumed에 반영한다.
Packet에는 마지막 epoch만 싣는다. bitmap을 packet에 싣지 않는다.

쓰는 순서는 RAM page clwb, sfence, slot 쓰기, slot clwb, sfence,
header next_epoch 공개, header clwb, sfence다.
읽는 쪽은 header와 slot을 clflushopt/mfence한 뒤 읽고 identity와 epoch를 검사한다.
consumed 다음부터 마지막 epoch까지 bitmap을 합친다.
K개보다 뒤처지면 전체 RAM을 무효화한다. record가 깨졌으면 abort한다.
받는 host가 bit를 지우는 host별 bitmap은 쓰지 않는다.
같은 비일관 cache line에 두 host가 쓰면 갱신을 잃을 수 있기 때문이다.

## 넘기는 순서

보내는 쪽은 vCPU 전체 정지 → device save/I/O drain → 두 장부 합치기 →
RAM page clwb/sfence → log 발행 → KVM detach/객체 arena clwb/sfence →
token·mailbox·제어 상태 clwb/sfence → ACK(Acknowledgement) 순서다.
RAM mapping을 park하기 전에 RAM을 내려쓴다.

받는 쪽은 token·mailbox clflushopt/mfence → token 검사 →
kernel attach 첫머리의 객체 무효화 → log 읽기 → RAM clflushopt/mfence →
기존 TLB(Translation Lookaside Buffer) 무효화 요청 → 재개 순서다.
INVEPT(Invalidate Translations Derived from EPT)는 기존 attach의 요청을 유지한다.
어느 단계든 실패하면 재개하지 않는다.

## 정답지와 주장 범위

L0(물리 host)의 읽기 전용 C 도구로 CXL backing의 모든 4 KiB page hash를 구한다.
첫 CXL 실행 전과 export 완료/import 전의 정지된 경계에서 비교한다.
이 lab은 DRAM에서 부팅한 뒤 정지해 promotion한다. 그 전체 복사가 첫 기준이다.
장부는 DRAM VM 생성 때부터 켠다. 첫 CXL record는 RAM 전체다.
Mapper의 GPA→file offset으로 바뀐 guest page와 이번 목록을 대조한다.
바뀐 page가 목록에 없으면 실패다. 이전 hash는 다음 경계까지 보존한다.
RAM 밖 변경은 KVM arena, mapper/token/log/제어, 그 밖으로 나눈다.
나머지는 0이거나 구체적인 쓰기 주체가 설명돼야 한다.

내려쓴 범위와 무효화한 범위의 영수증을 별도로 검사한다.
목록과 같은 함수가 만든 요약 수만으로 덮임을 주장하지 않는다.
Lab은 cache가 일관이다. 정확성 주장은 목록 완전성과 실제 범위 영수증에 한정한다.
실제 stale 재현과 물리 multi-host 성능을 주장하지 않는다.
실측은 node 0 DRAM backing이다. line 수에 주어진 CXL 단가를 곱한 값은 환산으로 표시한다.

## 구현에서 확정한 세부 사항

Log header의 next_epoch는 다음 미발행 번호다. Packet의 마지막 epoch는 next_epoch-1이다.
Header는 64 byte다. Cookie 두 개로 mapper allocation identity를 묶는다.
Slot은 16 byte(epoch와 page 수)와 bitmap을 64 byte 경계에 맞춘다.
64 MiB mapper metadata 예약 안에서 log는 +32 MiB, 시험 gate는 +40 MiB,
CXL mailbox는 +48 MiB에 둔다. Mapper table은 최대 RAM 4 GiB에서도 +17 MiB 아래다.
Consumed는 cookie별 /run 파일에 둔다. L1의 /run은 host별 DRAM tmpfs다.
VMM wrapper를 바꿔도 이어진다. 읽는 쪽은 CXL log에 쓰지 않는다.

Mapper table은 allocation 동안 불변이다. 처음 보내기/방문 때 table 전체를 처리한다.
매 이동에서는 변하는 header 4 KiB를 처리한다. Token·장치 packet의 CXL mailbox 사본도
내려쓰고, 받는 쪽에서 먼저 무효화해 전송된 packet과 비교한다.
Kernel arena 영수증은 host DRAM에 둔다. 시간 기록 때문에 내려쓴 객체를 다시 더럽히지 않는다.

추가 raw writer도 찾았다. resident_control.c의 시험용 heartbeat epoch는
기존에 export 뒤 raw DAX mapping으로 썼다. Cache 유지보수 모드에서는 이 쓰기를 없앤다.
VMM이 모든 vCPU를 정지한 뒤 GuestMemory.write_obj로 쓰고 장부를 걷는다.
이 변경은 시험용 ACK trigger의 순서도 정리한다. Gate와 ACK polling은 coherent lab 전용이다.

Promotion은 전체 RAM record를 발행한다. 그 뒤 두 장부를 다시 시작한다.
0/16/64/256 MiB 시험의 payload 쓰기는 guest ACK 뒤에 한다.
완료 표식을 본 뒤 다음 pause를 요청한다. Payload 쓰기 시간은 pause→ACK에 포함하지 않는다.
0 MiB도 heartbeat와 clock 등 실제로 쓰인 page가 조금 있다. 실제 목록을 함께 보고한다.

## 3단계: 실행 중 정리의 경합 계약

정리는 장치 event를 처리하는 VMM thread에서만 호출한다. 정리는 VM을 멈추지 않는다.
KVM_GET_DIRTY_LOG → Firecracker bitmap 수집/초기화 → RAM clwb/sfence → log 발행 순서다.
정리 중 실패하면 장부를 이미 지웠을 수 있으므로 프로세스를 abort한다.

| 쓰기 경로 | 정리와 겹칠 때 | 처리 |
|---|---|---|
| guest CPU | D bit 초기화 뒤 쓰기는 다음 장부. TLB flush 완료 전 쓰기는 뒤 clwb가 덮음 | PML buffer의 미처리 기록은 다음 장부. 최종 hand-off는 vCPU 정지/exit 뒤 수집 |
| iovec.rs, queue.rs 동기 쓰기 | bitmap을 먼저 표시하고 씀 | 같은 VMM thread라 정리와 실행이 끼어들지 않음. 저장된 queue pointer 범위는 매번 보수적으로 표시 |
| io_uring block 읽기 | kernel worker가 VMM 밖에서 guest RAM에 씀 | async_io.rs의 pop → mark_dirty_mem_and_unwrap에서 완료 후 표시. 미완료 읽기는 다음 정리. 최종 hand-off는 drain 뒤 수집 |
| KVM kvm_write_guest, pvclock | 쓴 뒤 dirty 표시 | 초기화 전 표시는 이번 clwb, 뒤 표시는 다음 장부. hand-off 때 vCPU/observer drain |
| VMClock | GuestMemory.write_obj로 표시 | VMM event loop에서 실행 |
| boot loader, 0 채우기, promotion/demotion | 실행 전 또는 정지 상태의 대량 쓰기 | 정리와 동시 실행 금지. 전체 범위 처리 계약 유지 |
| network/vsock 수신 | VMM 동기 iovec 경로 | 기존 resident 장치 계약에서 거절. 혼합 I/O 시험만 연결 종료 후 export |
| balloon/pmem/vhost/외부 memory device | VMM 밖 writer 가능 | 기존 direct preflight에서 거절 |
| 시험 heartbeat 제어기 | 과거 export 뒤 raw 쓰기 | 18에서 제거. 정지한 VMM의 추적 쓰기만 허용 |

허용하는 VMM 밖 RAM writer는 guest vCPU/KVM과 io_uring worker다.
전자는 KVM 장부, 후자는 완료 장부와 최종 drain으로 덮는다.
그 밖의 RAM writer thread를 추가하면 direct 계약을 확장하기 전까지 거절해야 한다.

받는 쪽은 mapper의 kernel WB direct mapping에서 RAM bitmap을 무효화한다.
Guest HVA의 PTE를 만들지 않는다. 독립 observer fd는 불변 GPA→offset 표의 로컬 사본만 가진다.
Observer는 공유 mapper lease/epoch/stat을 쓰지 않는다. consumed는 기존 host DRAM tmpfs다.

## 4단계: VM 영역과 수명 계약

VM 생성은 전역 pool에서 16 MiB를 한 번 예약한다. 첫 page는 그 VM의 descriptor다.
나머지는 VM·vCPU, VMCS, EPT, bitmap, PML, kvm_run, APIC map 등의 bump 할당이다.
전역 cmpxchg는 예약 때만 쓴다. VM bump는 소유 host의 로컬 spinlock 아래에서만 고친다.
캐시 유지 범위는 descriptor를 포함한 `[vm_base, vm_bump)`다.
범위 초과와 정수 넘침은 NULL과 FAILED로 끝낸다. DRAM fallback을 하지 않는다.
현재 lab의 host당 한 active graph 계약은 유지한다. 다른 graph 생성은 EBUSY다.
동시 여러 VM 실행을 지원하는 스케줄러를 이번에 만들었다고 주장하지 않는다.
실제 비일관 서버에서는 네트워크의 중앙 관리자가 겹치지 않는 VM 영역을 배정해야 한다.
공유 전역 bump를 실제 비일관 서버의 분산 할당기로 사용하지 않는다.

VM 제거는 pause·I/O drain·detach가 끝난 뒤에만 시작한다.
Lab의 모든 VM/vCPU fd 보유자는 해당 Firecracker 두 프로세스뿐이다.
두 wrapper와 제어기를 종료하고 PID(Process Identifier) 재사용까지 검사한다.
그 뒤 A와 B가 guest RAM 전체와 VM 객체의 예약 범위 전체를 무효화한다.
객체 영역 회수 ioctl은 RCU(Read-Copy Update) 콜백도 먼저 기다린다.
A·B 영수증이 모두 있어야 재사용을 arm한다. 일부 ACK만으로는 다음 VM을 만들지 않는다.
새 RAM은 non-temporal store로 0을 쓰고 SFENCE한다. 새 mapper는 새 cookie와 allocation generation을 갖는다.
새 VM의 promotion 복사도 non-temporal store와 SFENCE를 쓴다.
기존 객체의 DAX bump free는 메모리를 반환하지 않는다. 공유 RCU head도 enqueue하지 않는다.
오래된 wrapper의 fd가 살아 있는 상태로 객체 주소를 재사용하는 것은 허용하지 않는다.
이 lab의 pinned graph가 보유한 native 참조는 L1 종료 때까지 남는다.
일반 kernel에서 모든 참조를 파괴하는 생산용 수명 관리와 구분한다.

## 비소유 kernel 진입 감사의 코드 계약

진입 판정은 host DRAM의 영역별 owner 표를 쓴다. 공유 struct kvm을 먼저 읽지 않는다.
Detach 마지막 WB 뒤 owner 표를 닫는다. Attach는 영역을 무효화한 뒤 내부 복원을 허용한다.
공유 객체가 아닌 로컬 file·VMA·카운터만 다루는 작업은 계속 허용한다.

| 경로 | 카운터 | 비소유 처리 |
|---|---:|---|
| allocator | 0 | NULL |
| mmu notifier의 invalidate/change/young/release | 1 | 공유 lock·SRCU에 들어가기 전에 반환. detach에서 unregister/drain |
| kvm_get_kvm, get_safe | 2 | 공유 refcount를 고치지 않음. safe는 false |
| kvm_put_kvm, put_no_destroy | 3 | pinned graph의 참조 반납을 지연 |
| VM/vCPU file release | 4 | vCPU→kvm pointer를 읽기 전 반환 |
| stats fd, debugfs open/read/write/release, PEEK | 5 | EACCES 또는 로컬 자원만 반납 |
| APIC hrtimer | 6 | 재등록 없이 중단. detach가 이미 cancel/drain |
| irqfd eventfd wakeup, inject/resample/shutdown work | 7 | 공유 irqfd→kvm pointer를 읽기 전 반환. 기존 quiesce 유지 |
| preempt notifier | 8 | 공유 vCPU 필드 접근 전에 반환 |
| VM/vCPU ioctl | 9 | REATTACH 외에는 공유 객체 접근 전에 EIO |
| vCPU mmap/fault | 10 | EACCES 또는 SIGBUS |
| arena 회수·조회 | 12 | 관리 작업의 비소유 진입을 별도 계수 |
| dax_pool sysfs 조회 | 13 | detach 때 만든 host DRAM 사본만 반환 |
| DAX MMU/APIC map 지연 free | 14 | 공유 RCU head enqueue 자체를 제거 |
| 전원 관리 notifier | 15 | 공유 객체와 lock 접근 전 반환. detach에서 unregister |
| mapper lease/epoch/stat/release | 별도 모듈 카운터 | CPM_PARK 뒤 로컬 판정으로 거절. observer는 불변 표 사본만 사용 |

실제 진입 수와 검증 결과는 결과 문서에 기록한다. 카운터 0이라는 주장만으로 경로를 안전하다고 하지 않는다.
직접 매핑으로 보이는 객체에 비소유 CPU의 추측 실행이 cache line을 들일 수 있다.
이것은 architectural writer가 생긴다는 뜻은 아니다. Guard 뒤의 쓰기는 commit하지 못한다.
새 attach의 객체 영역 무효화는 그때까지 들어온 line을 버린다.
RAM은 새 log record의 page를 무효화한다. 범위를 넘긴 log는 전체 RAM을 처리한다.
따라서 추측으로 생긴 옛 line도 재개 전에 처리된다. 실제 비일관 장치의 증명은 별도 시험이다.

## 실패와 사전 처리의 경계

정리 알림은 단일 작성자가 동기적으로 보낸다. 상대의 처리가 끝나기 전 다음 record를 발행하지 않는다.
주기적인 독립 reader와 동시에 ring slot을 덮어쓰는 일반 프로토콜은 이번 범위가 아니다.
소비 실패, 잘못된 record, cache 명령/영수증 오류는 abort한다.
Packet epoch가 무효화 뒤 header보다 앞서 보이면 header line을 네 번 다시 무효화해 읽는다.
여전히 맞지 않으면 재개하지 않는다. 미래 consumed도 거절한다.
K개를 넘긴 reader는 기록을 추측하지 않고 RAM 전체를 무효화한다.
송신과 수신 RAM 유지보수는 둘 다 mapper의 kernel 매핑을 사용한다.
수신만 바꾸면 park의 HVA fault가 다음 송신 WB로 옮겨 가므로 두 경로를 함께 바꾼다.

## 같은 주소 재사용과 중첩 변환

VM descriptor에 임의 allocation_id를 둔다. 새 VM마다 값을 바꾼다.
Native EPT(Extended Page Table) page의 로컬 backlink cache는 주소뿐 아니라 이 값도 비교한다.
같은 주소를 가진 새 VM에 이전 graph의 로컬 page metadata를 붙이지 않는다.

Lab의 L0 KVM은 L1 EPT page를 shadow한다. VM의 vCPU 수가 바뀌면 EPT root의 주소도 바뀔 수 있다.
새 root만 건드리면 이전 root의 shadow를 버리지 못하는 경우가 있었다.
따라서 영역 회수 전에 이전 EPT page 목록을 범위 검사하고 각 page의 같은 byte를 다시 쓴다.
L0의 write protection이 이전 shadow page를 버리게 한다. 마지막에 그 line도 무효화한다.
이 동작은 두 host가 VM을 멈추고 fd 보유자를 종료한 회수 단계에만 있다.
살아 있는 비소유 VM 객체를 바꾸지 않는다.
기존 dax_nested_root_sync=1인 중첩 lab에서만 실행한다. 실제 하드웨어의 기본값은 꺼짐이다.
실제 비일관 하드웨어의 EPT 변환 처리는 attach의 INVEPT(확장 페이지 테이블 변환 무효화 명령)와 별도로 검증해야 한다.

Callback guard만으로 scheduler의 공유 queue 접근까지 막을 수는 없다.
Detach의 동기 timer 취소, work drain, notifier unregister가 먼저 끝나야 한다.
kvm_arch_dax_vm_handoff는 kvmclock sync/update delayed work를 cancel_delayed_work_sync한다.
PIT도 기존 정지·work drain 계약을 따른다. Guest vCPU 정지 뒤 새 work를 발행하지 않는다.
