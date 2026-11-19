# EPT 표만 CXL에 두는 hand-off

Compute Express Link(CXL)에 guest RAM과 Extended Page Tables(EPT) 표 page만 둔다.
VM마다 16 MiB EPT 영역을 받는다. 소유 host의 로컬 bump로 표를 할당한다.
표는 VM 생존 중 재사용하지 않는다. 부족하면 실패한다.
Virtual Machine Control Structure(VMCS), KVM(Kernel-based Virtual Machine) 객체,
vCPU, Page Modification Logging(PML) page, dirty bitmap, EPT 장부는 로컬 DRAM이다.

Release는 멈춘 모든 vCPU의 root 참조와 로컬 EPT 장부를 제거한다.
표의 칸은 지우지 않는다. 일반 zap 경로를 쓰지 않는다.
Read-Copy Update(RCU) reader가 끝난 뒤 로컬 장부를 반납한다.
그 뒤 root 목록과 vCPU root는 비어 있다.
메모리 notifier, memslot 변경, fd 종료는 일반 로컬 객체만 본다.
공유 표에 이르는 KVM 경로가 없으므로 비소유 host 진입 감사와 공유 refcount가 필요 없다.
이 성질은 release 뒤 munmap·fd·VMM 종료 전후의 표 hash로 검사한다.

Adopt는 root 위치, bump, role을 받는다. 상위 표만 읽어 하위 표 주소를 찾는다.
모든 표 주소를 VM 영역과 bump 안에서 검증한다. 중복·순환도 거절한다.
Leaf 표의 칸은 읽지 않는다. 표마다 로컬 장부와 page_private backlink를 만든다.
Root를 로컬 TDP(Two-Dimensional Paging) MMU(Memory Management Unit) 목록에 넣는다.
다음 MMU load는 같은 role의 root를 찾는다. 이전 EPT 변환을 무효화한다.
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
