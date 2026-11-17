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
