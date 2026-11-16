# 다중 vCPU hand-off — 2026-11-16

가상 중앙처리장치(virtual Central Processing Unit, vCPU)를 2 → 4 → 8개로 넓혔다.
각 단계에서 기존 1개 경로도 다시 확인했다. 설계는 [MULTI_VCPU.md](../../../implementation/virtual_machine/MULTI_VCPU.md)에 있다.
원시 자료의 경로·해시와 단계별 commit은 [summary.json](summary.json)에 있다.

가상 머신(Virtual Machine, VM) 공통 상태와 vCPU별 상태를 나눴다. 공유 응용 바이너리 인터페이스
(Application Binary Interface, ABI)는 2다. token은 464바이트다.
각 vCPU의 객체·실행 page·가상 머신 제어 구조(Virtual Machine Control Structure, VMCS)·
확장 페이지 테이블(Extended Page Tables, EPT) root identity를 확인한다.
모든 vCPU의 정지와 입출력 완료를 확인한 뒤 소유권을 넘긴다.
시계·타이머·가상 프로세서 간 인터럽트(Inter-Processor Interrupt, IPI) 연결도 복원한다.

2/4/8개 각각 **410회 hand-off, 205회 왕복**을 통과했다.
각 CPU에 고정한 512 KiB checksum writer와 futex ping-pong thread를 사용했다.
모든 CPU의 데이터·시계 오류는 0이었다. 타이머·IPI·진행 카운터가 계속 증가했다.
가상 머신과 vCPU를 다시 만들지 않았다.
마지막 vCPU만 source에서 계속 실행하면 `EBUSY`로 거절했다.
이전 ABI, 다른 vCPU 수, 마지막 VMCS identity 변조도 거절했다.

정지 요청부터 원격 guest 응답(acknowledgement, ACK)을 source가 관측할 때까지 쟀다.
각 구성은 warmup 10회, 측정 100회다. `quiet.lock`을 독점했다.
측정에는 50 µs polling과 guest 스케줄링이 포함된다.
아래 수치는 순수 커널 비용의 증가 곡선이 아니다.

| vCPU | 표본 | 중앙값 ms | 95백분위 ms | 최대 ms |
|---:|---:|---:|---:|---:|
| 1 | 100 | 1.321 | 2.928 | 3.126 |
| 2 | 100 | 0.756 | 1.615 | 3.265 |
| 4 | 100 | 0.959 | 2.005 | 3.113 |
| 8 | 100 | 1.090 | 1.929 | 3.005 |

최소 Linux guest는 128 MiB다. 바깥 가상 머신은 각각 4 GiB다.
NUMA(Non-Uniform Memory Access) node 0의 CPU 0–23과 메모리만 할당했다.
공유 Compute Express Link(CXL) 메모리는 2 GiB 파일로 에뮬레이트했다.
매 실행 전 node 0의 빈 메모리가 예산 + 4 GiB 이상인지 확인했다.
실행한 가상 머신과 `/dev/shm` backing은 모두 정리했다.

A는 Firecracker다. B는 vCPU별 native 실행 thread다.
B는 정지 응답을 순서대로 기다린다. 방향별 수치도 JSON에 남겼다.
한 물리 host의 Kernel-based Virtual Machine(KVM) 중첩 환경이다.
물리 host 사이 성능과 비일관 CXL 정확성을 증명하지 않는다.
고정 vCPU 구성과 VM 한 개짜리 arena를 전제로 한다. 실패 후 복구는 없다.
인터럽트 제어기 가상화(Advanced Programmable Interrupt Controller virtualization, APICv)와
posted interrupt는 계속 끈다. 8 vCPU resident 경로는 별도 시험하지 않았다.

소프트웨어 개발(Software Engineering, SWE) Gin fixture도 4 vCPU에서 통과했다.
원본 `go test ./...`를 실행하는 동안 B→A→B로 옮겼다. 같은 명령 PID와 시작 시각을
세 번 확인했다. 실제 동시 Go worker 수는 따로 세지 않았다.
전체 27개 명령의 반환값, 세 번의 Go test 패키지 결과, 최종 제출 diff가 baseline과 같았다.
읽기 전용 block과 네트워크 파일 시스템(Network File System, NFS)을 포함한다.
promotion·demotion과 총 6회 resident hand-off를 확인했다.

이 시험에서 RAM(Random Access Memory) mapper의 쓰기 fault 경쟁도 고쳤다.
기존 page-table entry를 지우면 다른 vCPU의 물리 page 조회가 `EFAULT`로 실패했다.
이제 entry를 유지한다. 쓰기 권한 변경은 Linux의 page-table lock 아래에서 처리한다.
수정 후 Gin 비교와 기존 mapper lease/import smoke test가 통과했다.
SWE L1에는 `CONFIG_NFSD=y`를 추가했다. 측정용 KVM module은 바뀌지 않았다.

Python 시험은 54개 중 46개 통과, 조건부 8개 건너뜀이다.
Rust 시험은 target별 18개 통과, 별도 raw packet fixture가 필요한 1개 건너뜀이다.
C probe는 경고를 오류로 처리해 빌드했다. 기존 1 vCPU의 EPT 폐기 경로도 통과했다.

최종 정리에서 이 실행의 20쌍, QEMU process identity 40개를 대조했다.
살아 있는 QEMU와 남은 공유 backing은 0개다. 기준 `.local`은 `du` 기준 35G다.
이 worktree의 `.local`은 약 11.29 GiB다(`du -sh`: 12G).
빌드 입력 복사본·산출물·원시 자료와 실패 trial의 private disk 증거를 포함한다.
큰 storage 원본은 symlink로 참조했다. 원본 입력과 host 설정은 바꾸지 않았다.
