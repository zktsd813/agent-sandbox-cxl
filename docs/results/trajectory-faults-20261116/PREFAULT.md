# 복원 전 주소표 채우기

기본 비교 결과는 commit `1616fb0`에서 확정했다. 이후 구현은 두 overlay로 분리했다. 기존 `kvm-dax-state.patch`와 `firecracker.patch`는 수정하지 않았다.

`CXL_LAB_PREFAULT=1`인 격리 빌드는 기존 patch 다음에 `kvm-prefault.patch`, `firecracker-prefault.patch`를 각각 적용한다. 기본 빌드는 이전 동작을 유지한다. 호스트 커널에는 적용하지 않는다.

새 ioctl은 실험용 x86 vCPU 인터페이스다. upstream `KVM_PRE_FAULT_MEMORY`와 호환되는 ABI라고 주장하지 않는다. 80 byte 요청과 16 byte GPA(guest physical address) 범위를 쓴다. 실행 주체는 정지한 단일 vCPU와 고정된 메모리 슬롯을 보장해야 한다. 커널은 정렬, 오버플로, 범위 중복, RAM 슬롯, 쓰기 권한, 개수 상한을 검사한다. MMIO(memory-mapped input/output)는 거부한다. 오류나 부분 완료 때 재개하지 않는다. 무효화 순서나 슬롯 세대가 바뀌면 전체 작업을 다시 해야 한다.

QUERY는 현재 직접 매핑 주소표의 존재하는 leaf를 순회한다. 연속 RAM 범위를 합친다. source VMM(virtual machine monitor)이 자기 vCPU0 파일 서술자를 복제해 조회한다. 여러 VM이나 vCPU를 한 프로세스에서 다루는 용도가 아니다. 범위 목록은 VM 식별자, 이동 세대, RAM 크기와 함께 전달한다. 목적지는 이 값과 총 page 수를 검사한다. 메모리 내용은 읽거나 복사하지 않는다.

FILL은 `kvm_mmu_reload` 뒤 범위의 4 KiB마다 `kvm_mmu_do_page_fault`를 호출한다. 기본은 `prefetch=true`다. 별도 ACTIVATE 옵션은 접근 추적 entry의 잔여 fault를 확인하기 위해 쓰기 접근 방식으로 채운다. 이 옵션은 guest 비동기 page fault가 켜져 있으면 거부한다. 일반 MMU(memory management unit) 잠금과 fault 경로를 사용한다. source 조회는 MMU 잠금과 RCU(read-copy-update) 아래에서 leaf를 읽는다. 이 실험은 고정된 메모리 슬롯만 지원한다.

Firecracker는 CPU·VM·장치 상태 복원 뒤, vCPU thread 시작 전에 FILL을 호출한다. `--prefault all`은 RAM 전체, `--prefault mapped`는 이동 직전 source의 매핑 범위를 채운다. `--prefault-activate`는 ACTIVATE를 선택한다. source 조회·목록 전달·목적지 채우기는 모두 이동 요청과 재개 사이에 있다. 목적지 완료 보고서는 궤적 시간 측정 뒤 수집한다. 조회 두 번과 채우기의 커널 시간, 전체 경과 시간, 실제 처리 page 수를 별도로 남긴다.

기능 검증과 추가 측정 결과는 아래에 기록한다.
