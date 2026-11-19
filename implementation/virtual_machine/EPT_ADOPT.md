# EPT 표만 CXL에 두는 hand-off

CXL에는 guest RAM과 VM별 16 MiB EPT 표 영역만 둔다.
VMCS, KVM·vCPU 객체, PML page, dirty bitmap, `kvm_run`, EPT 장부는 로컬 DRAM이다.
표는 소유 host의 로컬 bump로 할당한다. 같은 VM 생존 중 재사용하지 않는다.
영역이 차거나 검증에 실패하면 guest 진입을 막는다.

실험용 VM-fd ioctl은 `KVM_EPT_RELEASE`, `KVM_EPT_ADOPT` 두 개다.
ADOPT의 INIT은 영역 등록, WAIT는 준비된 수신자다. 공유 descriptor는 없다.
Packet은 공개 KVM CPU·VM 상태, 장치 상태, RAM lease와 EPT 위치·bump·role을 담는다.
RAM·EPT 내용과 kernel pointer는 직렬화하지 않는다.

Release는 모든 vCPU가 멈춘 상태에서 root 참조와 로컬 장부를 제거한다.
일반 zap은 쓰지 않는다. 표의 칸은 한 byte도 바꾸지 않는다.
일반 로컬 RCU reader·callback이 끝난 뒤 장부를 반납한다.
그 뒤 root 목록과 vCPU root는 비어 있고, adopt 전 일반 KVM_RUN은 거절한다.
Notifier, memslot 변경, munmap, fd·VMM 종료가 공유 표에 도달할 경로가 없다.
그래서 비소유 kernel 진입 감사와 공유 객체 refcount·parking이 필요 없다.
이 성질은 release 뒤 이전 host 종료 전후의 전체 표 hash로 검사한다.

Adopt는 상위 표를 걸어 하위 표 주소를 검증하고 로컬 장부·page_private를 만든다.
Leaf 칸은 읽지 않는다. 영역·bump 밖 주소, 중복·순환, 다른 root role은 거절한다.
Root를 로컬 TDP MMU 목록에 넣는다. 다음 MMU load가 같은 root와 EPTP를 쓴다.
기존 KVM의 INVEPT와 TLB 요청으로 이전 변환을 비운다.
세 leaf mapping gauge도 packet으로 넘겨 leaf 순회 없이 로컬 통계를 잇는다.

보내기: pause → 장치 저장·I/O drain → 마지막 KVM·장치 dirty log 수집 →
RAM 내려쓰기·쓰기 기록 발행 → release → EPT 사용 구간 내려쓰기 → 상태 직렬화·전달.
받기: packet 검사 → RAM·EPT 무효화 → adopt → CPU·장치 상태 복원 → 재개.
마지막 KVM_GET_DIRTY_LOG는 PML을 비우고 D bit를 지운다. 이후 source guest 쓰기는 없다.
수신 bitmap은 비어 있고, 재개 뒤 D=0→1 쓰기는 새 PML에 기록된다.
새 mapping과 장치 쓰기도 각각 KVM bitmap과 vm-memory bitmap이 기록한다.
빠른 왕복의 TSC 역행은 공개 TSC-offset 복원으로 막는다.

`--baseline`은 같은 worker 경로에서 adopt를 끈다. 이전 로컬 EPT를 버리고 빈 표를 채운다.
별도 hand-off 모드는 없다. Cache 정리와 상태·장치 복원은 같은 순서다.

Lab은 DRAM에서 부팅한 뒤 CXL로 promotion한다.
영역 좌표는 VMM 생성 때 받고, 등록은 promotion 직후 첫 CXL 실행 전에 한다.
준비된 수신자의 고정 HVA에 같은 CXL RAM lease를 붙인다.
`dax_nested_root_sync`는 L0의 오래된 nested shadow를 버리는 lab 전용 보완이다.
Adopt 때 같은 byte를 다시 쓰며 표 내용은 유지한다. 기본값은 꺼짐이다.
QEMU의 CXL direct RAM과 4 KiB page metadata 구성도 lab 전용이다.

지원 범위는 같은 CXL 물리 배치, 단일 RAM memslot, 같은 root role, 읽기 전용 block,
vCPU 1·2개다. Dirty ring, manual protect, nested guest와 NX 큰 page 완화는 거절한다.
실제 비일관 장비와 다른 물리 서버는 검증 범위 밖이다.
