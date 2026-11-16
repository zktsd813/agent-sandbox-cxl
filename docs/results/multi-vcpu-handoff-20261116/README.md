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

## 후속 검증

16번 실행이다. 양쪽 가상 머신 모니터(Virtual Machine Monitor, VMM)를 Firecracker로
상주시킨 resident direct 경로를 확인했다. 준비 helper만 native 코드다.
가상 CPU 2개와 8개에서 Gin의 원문 명령 27개를 모두 재생했다.
각 구성의 무이동 baseline과 반환값, 세 번의 test 패키지 요약, 최종 diff가 같았다.
독립 verifier와 별도 분석기가 원시 자료를 다시 읽었다.

`go test ./...`의 Go 동시 실행 한도(`GOMAXPROCS`)는 바꾸지 않았다.
명령 전후와 이동 사이에 `/proc/stat`의 CPU별 누적 시간을 기록했다.
사용자 시간 단위(`USER_HZ`)는 초당 100회다.
작업 시간은 user, nice, system, irq, softirq의 합이다. idle, iowait, steal은 제외했다.
아래 평균 코어 수는 작업 시간 합을 guest 명령 경과 시간으로 나눈 값이다.
명령 실행시간은 병렬성 확인용 단일 표본이다.

| vCPU | 명령 | CPU 사용 개수 | baseline 평균 코어 | 이동 실행 평균 코어 | 이동 실행 guest 초 |
|---:|---|---:|---:|---:|---:|
| 2 | 원문 test, 명령 12 | 2 | 1.56 | 1.54 | 2.46 |
| 8 | 원문 test, 명령 12 | 8 | 2.32 | 2.27 | 1.75 |
| 2 | 강제 패키지 재빌드 | 2 | 1.94 | 1.94 | 16.10 |
| 8 | 강제 패키지 재빌드 | 8 | 4.84 | 4.79 | 7.07 |

CPU 사용 개수의 기준은 CPU별 작업 시간이 1 tick과 구간의 1% 중 큰 값 이상인 경우다.
원문 test는 B→A→B로 옮겼다. 같은 프로세스 식별자(Process Identifier, PID)와
시작 tick을 세 번 확인했다.

추가 명령은 같은 Gin guest의 `go build -a -p "$(getconf _NPROCESSORS_ONLN)" ./...`다.
`-a`는 캐시된 패키지도 다시 컴파일한다. `-p`에는 guest의 vCPU 수를 넣는다.
원문 명령의 변경 없이 병렬 컴파일 시간을 확보하려고 골랐다.
이 명령이 도는 동안 A→B→A를 세 번 반복했다. 같은 PID와 시작 tick을 일곱 번 확인했다.
CPU별 누적 값은 모든 구간에서 감소하지 않았다.
8 vCPU 이동 빌드의 CPU별 작업 시간은 4.53, 4.28, 4.00, 4.05, 4.28, 4.14, 4.17, 4.42초다.
한 구간에서 관측한 평균 실행 코어 수의 최대는 5.27개다.
각 direct trial은 원문 test 중 2회, 재빌드 중 6회를 포함해 총 12회 이동했다.

기능 시험은 바깥 VM마다 4 GiB, guest는 2 GiB다.
빈 주소 공간을 줄인 4 GiB 파일 CXL 구성을 추가했다.
RAM 영역은 2–3072 MiB다. 장부는 3072 MiB 뒤, KVM 객체 arena는 3584 MiB 뒤다.
시작 전 node 0 free가 전체 예산과 4 GiB 여유보다 큰지 검사했다.
큰 기존 구성의 메모리 검사가 두 번 시작을 거절했다. 해당 시도는 VM을 띄우지 않았다.


### 양쪽 Firecracker 시간

방향마다 예열 10회 뒤 100회를 측정했다. 주 측정은 무계측이다. 단위는 ms다.

| vCPU | 상태 | A→B 중앙값 | A→B p95 | B→A 중앙값 | B→A p95 |
|---:|---|---:|---:|---:|---:|
| 1 | idle | 2.247 | 2.660 | 2.174 | 2.517 |
| 1 | 모두 busy | 3.316 | 4.177 | 3.433 | 4.074 |
| 2 | idle | 2.196 | 2.698 | 2.192 | 2.572 |
| 2 | 모두 busy | 2.361 | 3.597 | 2.490 | 3.691 |
| 4 | idle | 2.823 | 3.137 | 2.770 | 3.098 |
| 4 | 모두 busy | 2.547 | 3.298 | 2.505 | 4.395 |
| 8 | idle | 3.621 | 3.931 | 3.564 | 3.870 |
| 8 | 모두 busy | 3.271 | 4.426 | 3.273 | 5.165 |

source의 상주 C 제어기가 정지 요청을 보내기 직전부터 guest 응답을 관측할 때까지다.
측정 시계는 source의 `CLOCK_MONOTONIC`이다. 50 µs polling이 포함된다.
진단 응답은 기존 C 요청 helper가 읽는다. helper 생성과 host의 원격 호출 시간은 구간 밖이다.
양쪽 VMM은 같은 Firecracker binary다. 각 구성에서 공유 VM 생성은 1회, vCPU 생성은 구성 수만큼이었다.
객체를 다시 만들지 않고 220회 이동했다. busy는 15와 같은 CPU별 512 KiB checksum writer와 futex ring이다.
모든 CPU의 진행·타이머·프로세서 간 인터럽트가 증가했다. 데이터와 시계 오류는 없었다.

idle은 command shell이 대기하고 heartbeat 하나가 1 ms마다 깨어나는 상태다. 나머지 vCPU에는 작업이 없다.
guest는 128 MiB다. 바깥 VM은 각각 4 GiB다. 파일 CXL은 2 GiB다.
실제 resident 장치 경로인 읽기 전용 block, RAM mapper, NFS를 포함한다.
15의 장치 없는 A Firecracker/B native 수치와는 실행 경로도 다르다.

긴 꼬리도 모두 포함했다. 8 vCPU busy B→A 최대는 42.714 ms다. p95는 5.165 ms다.
해당 표본의 send/receive API 합은 2.383 ms였다. 나머지는 API 밖의 제어·전송·관측 구간이다.
그 구간의 세부 원인은 아직 확정하지 못했다.

### 1 vCPU 역전의 원인

주 측정의 idle 차이는 작았다. A→B에서 1개가 2개보다 0.051 ms 길었다.
B→A에서는 0.018 ms 짧았다. 두 방향에서 일관된 역전은 없었다.
busy에서는 1개가 약 0.9–1.0 ms 길었다.

`--profile`과 `--resume-profile`을 켜서 별도로 반복했다.
heartbeat를 CPU 0에 고정했다. 방향마다 예열 10회와 측정 100회다.
첫 진입 시도, epoch 감지, 모든 writer 응답, 최종 guest ACK 시각을 구분했다.
아래는 각 구간의 중앙값이다. 단위는 ms다.

| 상태 | vCPU | 커널 detach+attach A→B | 첫 진입 시도→guest ACK A→B | 같은 구간 B→A |
|---|---:|---:|---:|---:|
| idle | 1 | 0.273 | 0.291 | 0.298 |
| idle | 2 | 0.314 | 0.287 | 0.286 |
| busy | 1 | 0.220 | 1.698 | 1.587 |
| busy | 2 | 0.295 | 0.835 | 0.888 |

busy A→B의 epoch 감지는 1개에서 0.627 ms, 2개에서 0.277 ms였다.
writer 응답 대기는 각각 0.494, 0.260 ms였다. interrupt 집계는 0.273, 0.274 ms였다.
세부 구간의 중앙값은 각각 계산했다. 중앙값끼리 더한 값은 전체 중앙값과 다를 수 있다.

1 vCPU의 futex ring은 자기 자신을 깨워 계속 실행 가능한 상태를 유지한다.
checksum writer, ring, ACK 처리기가 한 코어를 나눈다.
측정한 역전의 큰 부분은 재개 뒤 guest 스케줄링과 writer 동기화에서 나왔다.
커널 구간은 2 vCPU가 더 길었다. 첫 KVM 실행 요청→진입 시도는 약 0.025 ms로 비슷했다.

15의 A Firecracker/B native 경로도 다시 쟀다. 방향마다 예열 10회와 측정 100회다.
아래 정지→ACK는 15와 같은 원격 제어기 응답 관측 경계다.

| vCPU | 정지→ACK A→B / B→A ms | 첫 진입 시도→guest ACK A→B / B→A ms |
|---:|---:|---:|
| 1 | 1.228 / 0.913 | 0.557 / 0.466 |
| 2 | 0.957 / 0.724 | 0.207 / 0.218 |

기존 경로에서도 1개가 느린 방향을 재현했다. guest ACK 구간의 차이도 확인했다.
CPU 개수에 따른 커널 attach 비용과 busy 검증 작업의 ACK 비용을 구분해야 한다.

### 문제와 정리

초기 계측은 idle heartbeat가 다른 CPU에서 응답해 vCPU 0 진입 기록이 없을 수 있었다.
계측기가 warmup에서 중단했다. CPU 0 고정 뒤 재측정은 모두 통과했다.
초기의 진단 응답 크기 거절도 수정했다. 최종 통계에는 실패 trial과 이전 계측 세트를 섞지 않았다.
정확히 고정한 ABI와 단일 arena 조건은 유지했다. 실패 복구는 구현하지 않았다.

Python 시험은 54개 중 46개 통과, 조건부 8개 건너뜀이다.
실제 Gin 자료를 변조해 누락·순서·PID 교체를 검사한 시험 8개도 통과했다.
외부 mapper 모듈로 인한 커널 상태 표시값 4096만 있었다. 추가 경고 비트는 없었다.
native 대조의 커널 상태 표시값은 0이었다.

이 실행의 24쌍, QEMU process identity 48개를 대조했다.
살아 있는 소유 QEMU와 남은 공유 backing은 모두 0개다. 각 실행에서 `lab.py stop`을 호출했다.
worktree `.local`은 `du` 기준 18.21 GiB다. 실패 trial의 private disk 증거를 포함한다.
기준 repo의 입력은 읽기 전용으로 사용했다. host 설정을 바꾸지 않았고 push하지 않았다.

원시 자료 경로, 해시, 방향별 전체 분포와 CPU별 구간은 [followup.json](followup.json)에 있다.
