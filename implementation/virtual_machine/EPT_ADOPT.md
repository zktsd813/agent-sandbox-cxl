# EPT 표만 CXL에 두는 hand-off

CXL에는 guest RAM과 VM별 16 MiB EPT 표 영역만 둔다.
VMCS, KVM·vCPU 객체, PML page, dirty bitmap, `kvm_run`, EPT 장부는 로컬 DRAM이다.
표는 소유 host의 로컬 bump와 회수 bitmap으로 할당한다.
영역이 차거나 검증에 실패하면 guest 진입을 막는다.

실험용 VM-fd ioctl은 `KVM_EPT_RELEASE`, `KVM_EPT_ADOPT`, `KVM_EPT_TIER`다.
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

## EPT 나누기

2 MiB chunk마다 데이터 계층을 고른다. 모든 데이터 page의 집은 기존 CXL offset이다.
PML4·PDPT·PD 등뼈는 항상 VM의 CXL 영역이다. 4 KiB leaf만 쓴다.
CXL chunk의 PT는 CXL, DRAM chunk의 PT는 소유 host의 DRAM이다.
기존 mapper fd에 로컬 page overlay를 두어 HVA·memslot·집 주소를 유지한다.
계층 bit와 올린 뒤 쓴 page bitmap, DRAM 사본·PT는 공유하거나 직렬화하지 않는다.

올리기: vCPU 정지 → 장치 저장·I/O drain → 기록 수집 → 2 MiB 로컬 복사 →
mapper 전환 → chunk PT zap → 계층 bit 설정 → 변환 비움. 다음 fault가 로컬 PT를 만든다.
내리기: 정지·drain → 기록 수집 → 누적 dirty page만 집에 복사·clwb·sfence →
복사 page를 다음 쓰기 기록에 합침 → 집 mapping 복원 → 로컬 PT zap·해제 →
계층 bit 해제 → 변환 비움. 미리 내리기도 같은 정지 경계를 쓴다.
Hand-off는 모든 DRAM chunk를 내린 뒤 기존 export를 따른다.
Release는 등뼈를 걸어 모든 존재하는 PD 칸의 PT 주소가 VM CXL 영역 안인지 확인한다.
검증 실패는 guest 진입을 막는다. 받는 host는 전부 CXL로 시작한다.

조건 1: release 전 계층 bitmap이 비고 등뼈의 모든 non-leaf 주소를 검증한다.
따라서 공유 표에 로컬 PT 주소가 남은 채 넘어갈 수 없다.
조건 2: 매 KVM·장치 기록 수집 합집합을 DRAM chunk별 누적 bitmap에 OR한다.
중간 clean으로 원래 bitmap을 지워도 누적 bitmap은 내리기까지 보존한다.
조건 3: 내리기의 복사 집합을 pending bitmap에 보존하고 다음 record에 OR한다.
조건 4: 전환 API는 paused·장치 drain 경계만 허용하고 마지막에 KVM 변환을 비운다.
조건 5: 기존 mapper owner epoch를 검사한다. 로컬 상태는 packet·공유 표에 담지 않는다.

16 MiB 한도는 유지한다. 기존 append-only 방식은 절반 chunk의 반복 왕복 전에 소진된다.
따라서 변환 비움과 RCU(읽기 경로의 지연 회수) 종료 뒤 도달 불가능한 표를 재사용한다.
재사용 bitmap도 소유 host 로컬이다. Adopt는 상위 표 순회로 도달 집합을 구하고
그 밖의 이전 할당 page만 free로 만든다. Release는 이전 owner의 RCU까지 비운다.
붙이는 순간 공유 EPT byte는 바꾸지 않는다. 실행 중 새 fault의 할당만 재사용한다.

`KVM_EPT_TIER`는 한 chunk의 PT를 떼고 계층을 바꾸거나 PT 위치를 조회한다.
DRAM leaf PFN은 mapper의 같은 chunk에서 조회한 로컬 PFN과 대조한다.
EPT fault 중 mmap lock을 잡지 않도록 이 PFN 목록도 정지 경계에서 로컬에 보관한다.
Eager 옵션은 정지한 vCPU 문맥에서 기존 KVM fault 경로로 512 page를 채운다.
유효한 root는 reload 검사로 보존한다. 새 root 참조를 중복 획득하지 않는다.
비동기 RCU callback이 끝난 표만 재사용 가능하므로 chunk마다 RCU를 기다릴 필요는 없다.
Release는 모든 callback을 기다린 뒤 소유권을 보낸다.

시험 driver는 `MemoryTier`에 기존 요청 파일을 사용한다.
`FC_EPT_TIER_REQUEST`가 가리킨 JSON의 `chunks`가 DRAM 목표 집합이고 `eager`는 선택이다.
요청 파일은 한 번 소비한다. 기존 전체 promotion·checkpoint 요청과 구분한다.
API는 paused 상태를 검사하고 장치 저장·I/O drain 뒤 전환한다.
실패 후에는 worker를 다시 실행하지 않는다.
