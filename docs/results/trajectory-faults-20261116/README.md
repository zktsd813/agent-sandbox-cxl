# 궤적 전체의 복원 fault 비용 (2026-11-16)

진행 중인 측정이다. 아래 예비 결과는 본 측정 결과와 구분한다.

가상 머신(VM, virtual machine)의 메모리를 Compute Express Link(CXL)에 둔다.
Snapshot은 매번 새 Kernel-based Virtual Machine(KVM) 객체를 만든다.
Direct hand-off는 기존 확장 페이지 테이블(EPT, extended page tables)과 KVM 객체를 이어받는다.
하드웨어 매핑 항목은 아래에서 SPTE(shadow page table entry)라고 부른다.

## 환경과 비교 경계

- bud05, Xeon Gold 6548Y+, host 메모리 node 0만 쓴다.
- 모든 실행은 `numactl --cpunodebind=0 --membind=0` 아래다.
- 물리 host를 L0이라 쓴다. Nested lab의 바깥 QEMU가 L1 A/B다. 그 안의 Firecracker VM이 L2다.
- L1은 각각 6 GiB, 가상 CPU(vCPU) 4개다. 공유 CXL backing은 8 GiB다.
  공유 제어 메모리는 64 MiB다. 예산은 20,544 MiB다.
- L2는 2 GiB, 1 vCPU다. 실행 CPU 범위는 8–15다.
- VM을 띄울 때마다 `lab.lock`을 잡고 node 0 free를 확인한다.
  예산에 4 GiB를 더한 24,640 MiB보다 적으면 시작하지 않는다.
- 조건별 본 측정은 `quiet.lock`의 배타 잠금 아래다.
  빌드와 C 시험은 공유 잠금, `nice -n 19`, 병렬도 16 이하를 쓴다.
- 네트워크는 QEMU socket이다. 실물 CXL과 host 설정은 바꾸지 않는다.

세 방식의 가상 머신 모니터(VMM, virtual machine monitor)는
같은 CXL 주기억장치(RAM, random-access memory)에서 명령 전체를 실행한다.
처음의 RAM promotion과 마지막 demotion은 궤적 시간 밖이다.
무이동은 A에서 계속 실행한다. 나머지는 A와 B를 번갈아 쓴다.
Gin은 27개, PHP-CS-Fixer는 35개, MicroPython은 43개 명령이다.
원래 명령, 환경, 순서, 반환값, 최종 diff를 독립 검증한다.

Snapshot의 목적지 프로세스와 제어 인터페이스(API, application programming interface) 연결은 pause 전에 만든다.
준비 때 KVM VM/vCPU의 파일 서술자(FD, file descriptor)가 없는지 기록한다.
계수 실행에서는 매 복원 때 VM/vCPU가 하나씩 생성됐는지 확인한다.
같은 공유 mapper handle을 쓰며 RAM dump와 RAM 전송은 0 byte다.
복원한 프로세스의 `rw-s /dev/cxl_pool` 매핑과 FD도 보존한다.
양쪽 L1에는 메모리 없는 보조 KVM VM을 둔다.
KVM 하드웨어를 매번 끄고 켜는 차이를 없애기 위해서다.
NFS(Network File System) 설정도 읽기 전용·동일 캐시 정책으로 맞췄다.

바깥 제어 연결은 두 방식 모두 미리 연결한 virtio-serial RPC다.
RPC는 remote procedure call을 뜻한다.
내부 제어는 완전히 같지 않다.
Snapshot은 영속 HTTP(Hypertext Transfer Protocol) API와 L0를 경유하는 상태 파일 전달을 쓴다.
Direct는 상주 제어기, native Unix endpoint, 준비한 L1 간 TCP(Transmission Control Protocol)를 쓴다.
Direct만 읽기 전용 block backend를 미리 연다.
따라서 정지 시간 차이 전체를 EPT 보존 효과라고 해석하지 않는다.

## 정책과 계측

무거운 명령은 [heavy.json](heavy.json)의 고정 목록으로 정했다.
다른 checkout의 `cmdkind.py`를 읽어서 test/build/install을 골랐다.
MicroPython의 명시적 test driver 호출도 포함했다.
분류기 경로, 해시, 보정한 기준과 최종 index를 함께 기록했다.
모든 명령 정책은 마지막 제출 명령을 포함해 매 명령 전에 옮긴다.
각 fixture × 정책 × 방식은 3회다.
fixture·정책·방식·반복 순서를 한꺼번에 섞는다.
계수 seed는 20261116, 시간 seed는 20261117이다.

모든 이동 요청·Resume 응답·명령 송신·결과 수신은 L0의
`CLOCK_MONOTONIC` 나노초 시계로 기록한다.
명령 시간은 FIFO 송신 요청 직전부터 완전한 결과 receipt 수신까지다.
Guest 실행만의 시간은 아니다. 제어 전달과 최대 20 ms의 polling이 들어간다.
각 run의 loadavg와 `ps -eo pid,user,psr,pcpu,comm` 원본을 전후에 남긴다.
다른 node 0 프로세스가 5% 이상이면 분석에서 흔들린 run으로 표시한다.
이 값은 `ps`의 프로세스 수명 평균이라 짧은 간섭을 놓칠 수 있다.

궤적 총 시간은 첫 이동/명령 직전부터 마지막 명령 결과까지의 실제 wall time이다.
반복 수신 프로세스 준비와 결과 JSON(JavaScript Object Notation) 저장도 포함한다.
이동 정지 시간은 요청부터 Resume 응답까지의 합이다.
별도로 명령 구간과 이동 구간만 합한 `active_s`도 남긴다.
그 값에서는 준비와 기록 구간이 빠진다.

시간 실행에는 fault probe를 켜지 않는다.
계수 실행은 L1 tracefs의 `kvm:kvm_exit` 중 reason 48(EPT violation)과
`kvm:kvm_page_fault`를 별도 histogram으로 센다.
명령 직전·직후의 각 L1 카운터 차이를 쓴다.
Histogram dropped가 0이 아니면 거절한다. `pf_fixed`로 대신하지 않는다.
`kvm_page_fault`의 guest 물리 주소도 2 GiB 구간으로 나눠 센다.
이 fixture의 RAM은 `[0, 2 GiB)`다. 장치 주소의 fault는 별도로 남긴다.
Intel EPT 경로에서 이 tracepoint는 EPT 위반의 guest 물리 주소를 기록한다.
이동 중 fault는 명령 합과 구분한다. 전체 계수 구간 합도 남긴다.
L1 커널은 기존 설정에서 `CONFIG_HIST_TRIGGERS=y`만 추가해 별도 빌드했다.
계수 실행의 hand-off 함수 trace에는 `dax_nested_root_sync` 시간도 있다.
궤적 끝의 실제 4 KiB/2 MiB/1 GiB leaf 수는 KVM debugfs에서 읽는다.
다른 guest는 모두 멈춘 상태다. 반대 L1의 leaf 수가 0인지도 확인한다.

## 물리 L0의 fault 비용

[physical_ept_cost.c](../../../implementation/virtual_machine/physical_ept_cost.c)는
일반 L0 KVM에 작은 32-bit guest를 만든다. guest paging은 끈다.
L0에서 이미 할당한 512 MiB를 guest가 4 KiB 간격으로 쓴다.
첫 scan에서 반복 scan 다섯 번의 중앙값을 뺀다.
`MADV_NOHUGEPAGE`와 `MADV_HUGEPAGE`로 backing만 바꾼다.
매핑 크기는 실제 KVM leaf 수로 확인한다. host 설정은 바꾸지 않는다.

| 실제 leaf | 반복 | fault당 비용 중앙값 | 범위 |
|---|---:|---:|---:|
| 4 KiB | 3 | 0.989 µs | 0.987–1.027 µs |
| 2 MiB | 3 | 1.376 µs | 1.363–1.453 µs |

첫 scan의 fixed fault와 leaf는 각각 131,073개와 257개였다.
뒤의 다섯 scan은 모두 fixed fault 0회였다.
분모는 L0 KVM vCPU의 `pf_fixed` 증가분이다.
Host tracefs는 접근할 수 없어 L0 EPT exit를 직접 세지 못했다.
이 값에는 cache 효과와 첫 VM-entry 비용도 남는다. 순수 상수로 보지 않는다.
원시 scan 값과 바이너리 해시는 [physical-summary.json](physical-summary.json)에 있다.

SWE의 실제 매핑은 4 KiB다.
L1에서 센 RAM 주소 fault 수에 0.989 µs를 곱한 값은 물리 서버의 **추정 매핑 비용**이다.
장치 주소 fault는 이 환산에서 제외한다.
VMM 생성, EPT 해제, 장치 복원, cache와 주소 변환 캐시(TLB, translation lookaside buffer)의 warm-up, 전송 비용은 그 추정에 없다.
2 MiB 비용에 같은 4 KiB fault 수를 곱해서 huge-page 궤적을 예측하지 않는다.

## 예비 검증

Gin heavy 정책, 계수 실행 각 1회(`pilot5`)는 세 방식 모두 독립 검증을 통과했다.

| 방식 | 명령 중 EPT violation 합 |
|---|---:|
| 무이동 | 261,424 |
| Snapshot | 512,511 |
| Direct | 259,469 |

이 표는 본 측정의 3회 집계에 섞지 않는다.
기존 Python 시험 49개는 통과했다. 기존 증거가 필요한 8개는 skip이다.
선택한 Rust 계약 시험 15개가 통과했다.
각 SWE run은 `lab.py verify`와 독립 workload 검증을 거친다.

## 해석 범위

Nested 시간은 물리 서버의 시간으로 옮길 수 없다.
Direct가 L1 EPT를 보존해도 L0의 합성 변환은 다시 채운다.
`dax_nested_root_sync`도 nested 전용 보완이다. 시간을 임의로 빼지 않는다.
Direct의 fault가 0이어야 하는 대상은 이미 매핑한 page다.
새로 쓰는 page의 fault까지 없어지는 것은 아니다.
무이동의 fault와 나란히 봐야 한다.
A는 로컬 lower disk, B는 NFS를 쓴다. 무이동 대비 시간에는 이 차이도 들어간다.
메모리는 coherent 파일 backing이다. 실물 비일관 CXL multi-host는 검증하지 않았다.

분석은 표준 Python만 쓴다.
`analyze.py SUITE/report.json ... --physical PHYSICAL/physical.json --output OUTPUT`으로
조건별 요약 JSON과 명령별 CSV(comma-separated values)를 만든다.
원시 run은 `implementation/virtual_machine/.local/run14/`와 `.local/runs/`에 둔다.
