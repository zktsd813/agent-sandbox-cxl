# 복원 전 주소표 채우기

기본 비교 결과는 commit `1616fb0`에서 확정했다. 이후 구현은 두 overlay로 분리했다. 기존 `kvm-dax-state.patch`와 `firecracker.patch`는 수정하지 않았다.

`CXL_LAB_PREFAULT=1`인 격리 빌드는 기존 patch 다음에 `kvm-prefault.patch`, `firecracker-prefault.patch`를 각각 적용한다. 기본 빌드는 이전 동작을 유지한다. 호스트 커널에는 적용하지 않는다.

새 ioctl은 실험용 x86 vCPU 인터페이스다. upstream `KVM_PRE_FAULT_MEMORY`와 호환되는 ABI라고 주장하지 않는다. 80 byte 요청과 16 byte GPA(guest physical address) 범위를 쓴다. 실행 주체는 정지한 단일 vCPU와 고정된 메모리 슬롯을 보장해야 한다. 커널은 정렬, 오버플로, 범위 중복, RAM 슬롯, 쓰기 권한, 개수 상한을 검사한다. MMIO(memory-mapped input/output)는 거부한다. 오류나 부분 완료 때 재개하지 않는다. 무효화 순서나 슬롯 세대가 바뀌면 전체 작업을 다시 해야 한다.

QUERY는 현재 직접 매핑 주소표의 존재하는 leaf를 순회한다. 연속 RAM 범위를 합친다. source VMM(virtual machine monitor)이 자기 vCPU0 파일 서술자를 복제해 조회한다. 여러 VM이나 vCPU를 한 프로세스에서 다루는 용도가 아니다. 범위 목록은 VM 식별자, 이동 세대, RAM 크기와 함께 전달한다. 목적지는 이 값과 총 page 수를 검사한다. 메모리 내용은 읽거나 복사하지 않는다.

FILL은 `kvm_mmu_reload` 뒤 범위의 4 KiB마다 `kvm_mmu_do_page_fault`를 호출한다. 기본은 `prefetch=true`다. 별도 ACTIVATE 옵션은 접근 추적 entry의 잔여 fault를 확인하기 위해 쓰기 접근 방식으로 채운다. 이 옵션은 guest 비동기 page fault가 켜져 있으면 거부한다. 일반 MMU(memory management unit) 잠금과 fault 경로를 사용한다. source 조회는 MMU 잠금과 RCU(read-copy-update) 아래에서 leaf를 읽는다. 이 실험은 고정된 메모리 슬롯만 지원한다.

Firecracker는 CPU·VM·장치 상태 복원 뒤, vCPU thread 시작 전에 FILL을 호출한다. `--prefault all`은 RAM 전체, `--prefault mapped`는 이동 직전 source의 매핑 범위를 채운다. `--prefault-activate`는 ACTIVATE를 선택한다. source 조회·목록 전달·목적지 채우기는 모두 이동 요청과 재개 사이에 있다. 목적지 완료 보고서는 궤적 시간 측정 뒤 수집한다. 조회 두 번과 채우기의 커널 시간, 전체 경과 시간, 실제 처리 page 수를 별도로 남긴다.

기능 검증과 추가 측정 결과는 아래에 기록한다.

## 기능 시험

Gin heavy 전체 궤적을 두 경로로 한 번씩 실행했다. 두 run 모두 `lab verify`와 독립 workload 검증을 통과했다. A/B 각각 별도 VM에서 잘못된 요청 12종의 거부, 빈 root, 4 page 채우기, 정확한 2개 범위 조회, ACTIVATE도 확인했다. 이 ABI 시험에서는 guest를 실행하지 않았다. 원시 요약은 `prefault-functional.json`이다.

RAM 전체 경로는 이동마다 524,288개 4 KiB page를 채웠다. kernel 시간은 이동마다 약 0.616–0.620초였다. 첫 이동 전 RAM fault는 8,925회다. 첫 이동 뒤 모든 명령의 RAM fault는 0회였다. 마지막 실제 leaf도 4 KiB 524,288개다. 이 하드웨어와 설정에서는 기본 `prefetch=true`로 충분했다. 본 비교에 ACTIVATE를 쓰지 않는다.

매핑 범위 경로는 세 이동에서 각각 9,707 / 146,977 / 159,584개 page를 채웠다. 목록은 각각 902 / 1,858 / 1,851개 범위다. 두 번 조회한 kernel 시간 합은 1.15 / 5.69 / 6.09 ms였다. 채우기 kernel 시간 합은 11.73 / 175.03 / 188.27 ms였다. 궤적 RAM fault는 261,929회였다. 새로 쓰는 page는 여전히 fault를 일으킨다. 전체 RAM 경로와 구분해야 한다.

초기 구현은 첫 채우기 뒤 `EAGAIN`으로 중단했다. 진단에서 메모리 슬롯 세대는 고정됐지만, 첫 DAX 사용자 주소 변환이 생길 때 MMU 무효화 순서가 page마다 변했다. 전체 작업의 보수적인 순서 검사가 이를 감지했다. Firecracker는 최대 네 번 전체 범위를 다시 시도한다. 위 시험은 매 이동 두 번으로 완료했다. 두 번째 시도 시간도 정지 시간에 포함했다. 이 구현의 비용은 최적화된 prefault의 하한이 아니다. 오류 상태에서는 재개하지 않는다.

기존 Python 시험은 42개 통과, 증거 의존 8개 skip이다. 기존 Rust 계약과 새 ABI·범위 시험은 합계 17개 통과, 기존 1개 ignore였다. 커널 모듈과 Firecracker를 각각 격리 빌드했다. 모든 기능 시험의 QEMU와 공유 메모리 backing은 정리했다.

다음 본 측정은 새 모듈·VMM의 동일한 binary로 무이동, snapshot, direct, RAM 전체 prefault, 매핑 범위 prefault를 함께 재실행한다. fixture와 방식 순서를 무작위로 섞는다. 시간 run seed는 20261119, 수를 세는 run seed는 20261118이다. 조건마다 3회다.

추가 본 측정에는 `--trajectory-policies heavy all --prefault-variants all mapped --repetitions 3`을 쓴다. 단일 기능 시험은 `--modes state-only --prefault all` 또는 `--prefault mapped`로 고를 수 있다. `--prefault-probe`는 정적 ABI 시험 binary 경로다. 일반 host 커널에서 이 binary를 실행하지 않는다.

추가 실험의 물리 비용 계산은 재개 이후 demand fault만 다룬다. 예비 채우기 자체의 물리 실행 비용을 측정한 값은 아니다. 전체 RAM 경로는 미래에 처음 쓸 page의 fault도 미리 처리하므로, 잔여 demand 비용이 무이동보다 작아질 수 있다. Nested 채우기 시간을 물리 서버 시간으로 사용하거나, 이 음수 차이를 전체 성능 향상이라고 해석하지 않는다.
