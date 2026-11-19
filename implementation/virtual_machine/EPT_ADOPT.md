# EPT 표만 CXL에 두는 hand-off

Compute Express Link(CXL)에 guest RAM과 Extended Page Tables(EPT) 표 page만 둔다.
VM마다 16 MiB EPT 영역을 받는다. 소유 host의 로컬 bump로 표를 할당한다.
표는 VM 생존 중 재사용하지 않는다. 부족하면 실패한다.
Direct의 전역 CXL 객체 allocator는 꺼야 한다. 켜져 있으면 등록을 거절한다.
Virtual Machine Control Structure(VMCS), KVM(Kernel-based Virtual Machine) 객체,
vCPU, Page Modification Logging(PML) page, dirty bitmap, EPT 장부는 로컬 DRAM이다.

Release는 멈춘 모든 vCPU의 root 참조와 로컬 EPT 장부를 제거한다.
표의 칸은 지우지 않는다. 일반 zap 경로를 쓰지 않는다.
Read-Copy Update(RCU) reader가 끝난 뒤 로컬 장부를 반납한다.
그 뒤 root 목록과 vCPU root는 비어 있다.
Adopt 전 guest 진입은 거절하므로 새 CXL root를 만들 수도 없다.
메모리 notifier, memslot 변경, fd 종료는 일반 로컬 객체만 본다.
공유 표에 이르는 KVM 경로가 없으므로 비소유 host 진입 감사와 공유 refcount가 필요 없다.
이 성질은 release 뒤 munmap·fd·VMM 종료 전후의 표 hash로 검사한다.

Adopt는 root 위치, bump, role을 받는다. 상위 표만 읽어 하위 표 주소를 찾는다.
모든 표 주소를 VM 영역과 bump 안에서 검증한다. 중복·순환도 거절한다.
Leaf 표의 칸은 읽지 않는다. 표마다 로컬 장부와 page_private backlink를 만든다.
Root를 로컬 TDP(Two-Dimensional Paging) MMU(Memory Management Unit) 목록에 넣는다.
다음 MMU load는 같은 role의 root를 찾는다. 기본 kvm_mmu_load가 그 EPTP에
INVEPT(EPT 변환 무효화)를 실행한다. 기존 TLB(Translation Lookaside Buffer) 요청도 보낸다.
중첩 lab 보완은 adopt에서만 쓰며 기본값은 꺼짐이다. 동일 byte 재쓰기도 내용은 유지한다.

보내는 순서: vCPU pause → 장치 저장·I/O drain → 마지막 dirty log 수집 →
RAM 내려쓰기와 epoch 발행 → release → EPT [base,bump) 내려쓰기 → 상태 직렬화.
받는 순서: packet 검사 → RAM·EPT 무효화 → adopt → vCPU·장치 상태 복원 → 재개.
최종 KVM_GET_DIRTY_LOG는 PML을 비우고 D bit를 지운다. 그 뒤 guest 쓰기는 없다.
송신 RAM 목록은 그 경계까지의 모든 쓰기를 덮는다.
수신 bitmap은 비어 있고, 재개 뒤 D=0→1 쓰기는 수신 PML에 새로 기록된다.
새 mapping의 쓰기는 기존 make_spte dirty 기록으로 덮는다.
EPT의 A/D bit는 CPU도 쓰므로 목록 없이 사용한 표 영역 전체를 처리한다.
명령 수는 실제 cache 명령 발행 지점에서 센다.
실제 비일관 하드웨어는 이번 시험 범위 밖이다.

구현·시험 중 확정한 차이는 진행 기록과 결과에 적는다.

실험 ABI(Application Binary Interface)는 VM fd의 KVM_EPT_RELEASE/KVM_EPT_ADOPT 두 개다.
Adopt의 INIT flag는 영역 등록, WAIT flag는 준비된 수신자를 뜻한다.
16 MiB 영역에는 descriptor를 두지 않는다. 첫 표가 offset 0부터 시작한다.
Bump는 정렬·영역 한계·이전 로컬 값·도달 가능한 모든 표와 대조한다.
정확한 high-water 값은 소유자가 발행한 packet과 기존 CXL mailbox의 일치로 묶는다.
읽지 않는 폐기 표의 할당 이력을 수신자가 추측하지 않는다.
Leaf mapping gauge 세 개(24 byte)도 packet에 넣어 leaf를 읽지 않고 통계를 이어 간다.

기존 fixture는 DRAM boot 뒤 promotion한다. 영역 좌표는 VMM 생성 때 받는다.
커널 등록은 promotion 직후, 첫 CXL 실행 전에 한다. 그 이전의 로컬 boot EPT만 버린다.
이 lab은 Intel EPT A/D(Accessed/Dirty) bit와 기본 dirty bitmap을 쓴다.
실행 금지 큰 page 완화(NX huge-page mitigation)는 기존 lab처럼 끈다.
그 완화의 별도 장부는 이 모드에서 이전하지 않으므로 켜져 있으면 등록을 거절한다.

비소유 VMM의 RAM mapping을 끊는 기존 mapper 기능은 유지한다.
이것은 공유 KVM 객체를 parked 상태로 보존하는 direct 장치가 아니다.
기존 RAM mapper·쓰기 log·mailbox 제어 영역은 유지한다. 새 공유 구조는 없다.
Native 전송은 기존 worker와 resident-control을 쓴다. 새 상주 제어기는 없다.
Direct용 KVM keepalive도 이 모드에서는 띄우지 않는다.
비교용 reuse-prepared arm은 같은 상태·장치 경로를 쓰되 EPT ioctl을 호출하지 않는다.
표와 장부를 로컬에 두고 이전 translation을 버린 뒤 빈 표를 다시 채운다.

빠른 왕복에서도 Time Stamp Counter(TSC)가 역행하지 않아야 한다.
KVM_SET_MSRS의 가까운 TSC 쓰기 합치기를 피하려고 기존 공개 TSC-offset 복원을 쓴다.
EPT와 로컬 EPT 대조군 모두 같은 semantic 상태 복원 경로를 쓴다.

현재 지원 범위는 단일 RAM memslot과 같은 root role이다.
Dirty ring, manual protect, 중첩 guest와 NX 큰 page 완화는 거절한다.

미리 정리는 기존 worker 연결에서 소유자의 수집·내려쓰기·발행 뒤 수신자의
RAM 선무효화를 이어서 한다. 준비된 수신자는 기존 RAM manifest의 identity로
쓰기 기록만 읽는다. KVM이나 EPT 표를 건드리지 않는다. 보조 thread는 없다.
