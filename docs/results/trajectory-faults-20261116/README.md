# 궤적 전체의 복원 fault 비용 (2026-11-16)

1–4단계의 측정 결과다. Prefault 추가는 다음 단계에서 진행한다.

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
- 모든 방식의 L1 CPU 모델에서 AMX(Advanced Matrix Extensions)와
  XFD(Extended Feature Disable)를 숨긴다. 주소 무작위 배치도 공통으로 끈다.
- Guest CPU의 가상화·시간·성능 계수 기능은 direct 호환 규칙으로 맞췄다.
  실제 guest CPU flag 파일의 해시가 세 방식에서 같은지 독립 검증한다.
- L1 KVM은 모두 `nx_huge_pages=never enable_pmu=0`이다.
  Intel 모듈은 `pml=0 enable_apicv=0 flexpriority=0 preemption_timer=0`이다.
  PMU(performance monitoring unit)와 page modification logging(PML)을 끈다.
  인터럽트 가상화(APICv)와 관련 timer도 같은 설정이다.
  이 값은 각 L1의 sysfs에서 읽어 결과 identity에 넣었다.
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
분류기 규칙에 따라 PHPUnit `--version`도 test 범주에 포함된다.
모든 명령 정책은 마지막 제출 명령을 포함해 매 명령 전에 옮긴다.
각 fixture × 정책 × 방식은 3회다.
fixture·정책·방식·반복 순서를 한꺼번에 섞는다.
계수 seed는 20261116, 시간 seed는 20261117이다.

모든 이동 요청·Resume 응답·명령 송신·결과 수신은 L0의
`CLOCK_MONOTONIC` 나노초 시계로 기록한다.
명령 시간은 FIFO 송신 요청 직전부터 완전한 결과 receipt 수신까지다.
Guest 실행만의 시간은 아니다. 제어 전달과 최대 20 ms의 polling이 들어간다.
각 run의 loadavg와 `ps -eo pid,user,psr,pcpu,comm` 원본을 전후에 남긴다.
수집용 `ps`를 제외한 다른 node 0 프로세스가 5% 이상이면 분석에서 흔들린 run으로 표시한다.
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
먼저 여섯 번 쓴다. vCPU가 멈춘 동안 같은 VM의 RAM slot을 삭제한다.
실제 EPT leaf가 0개가 됐는지 확인한다. 같은 backing을 다시 등록한다.
재접근 scan에서 다음 반복 scan 다섯 번의 중앙값을 뺀다.
RAM·guest register·데이터는 유지한다. 초기 scan 값도 원시 자료에 남긴다.
`MADV_NOHUGEPAGE`와 `MADV_HUGEPAGE`로 backing만 바꾼다.
매핑 크기는 실제 KVM leaf 수로 확인한다. host 설정은 바꾸지 않는다.

| 실제 leaf | 반복 | fault당 비용 중앙값 | 범위 |
|---|---:|---:|---:|
| 4 KiB | 3 | 0.986 µs | 0.981–0.999 µs |
| 2 MiB | 3 | 1.060 µs | 1.041–1.073 µs |

EPT를 비운 뒤 첫 scan의 fixed fault와 leaf는 각각 131,073개와 257개였다.
뒤의 다섯 scan은 모두 fixed fault 0회였다.
분모는 L0 KVM vCPU의 `pf_fixed` 증가분이다.
Host tracefs는 접근할 수 없어 L0 EPT exit를 직접 세지 못했다.
이 값에도 cache/TLB 효과와 VM-entry 비용이 남는다. 순수 상수로 보지 않는다.
기존 host 설정은 TDP MMU 켬, NX huge-page 보완 끔, THP madvise다.
TDP MMU는 two-dimensional paging memory management unit이다.
NX는 no-execute, THP는 transparent huge page다. 이 설정은 바꾸지 않았다.
원시 scan 값과 바이너리 해시는 [physical-summary.json](physical-summary.json)에 있다.

SWE의 실제 매핑은 4 KiB다.
L1에서 센 RAM 주소 fault 수에 0.986 µs를 곱한 값은 물리 서버의 **추정 매핑 비용**이다.
장치 주소 fault는 이 환산에서 제외한다.
VMM 생성, EPT 해제, 장치 복원, cache와 주소 변환 캐시(TLB, translation lookaside buffer)의 warm-up, 전송 비용은 그 추정에 없다.
2 MiB 비용에 같은 4 KiB fault 수를 곱해서 huge-page 궤적을 예측하지 않는다.

## 예비 검증

CPU 조건을 맞춘 Gin heavy 계수 실행(`cpucheck3`)은 세 방식 모두 독립 검증을 통과했다.
실제 guest CPU flag의 SHA-256 해시는 세 방식 모두
`e1f41f5ed6b13e3822ae62064cc3f9f90cc6772cb244e8a6567b6f5300a5a262`였다.
양쪽 L1의 여섯 KVM 모듈 설정도 같았다.

기존 lab은 direct에서만 CPU 기능과 KVM 옵션을 제한했다.
CPU mask만 맞춘 `cpucheck`도 PMU 기능 차이로 독립 검증에서 거절됐다.
그 앞의 `pilot5`, `count1/2`, `time1/2`는 진단 자료로 보존했다.
최종 비교에는 CPU 조건을 맞춘 `count3`와 `time3`만 쓴다.

Python 시험 50개는 통과했다. 기존 증거가 필요한 8개는 skip이다.
선택한 Rust 계약 시험 15개가 통과했다.
각 SWE run은 `lab.py verify`와 독립 workload 검증을 거친다.

## 해석 범위

Nested 시간은 물리 서버의 시간으로 옮길 수 없다.
Direct가 L1 EPT를 보존해도 L0의 합성 변환은 다시 채운다.
`dax_nested_root_sync`도 nested 전용 보완이다. 시간을 임의로 빼지 않는다.
Histogram은 각 L1에서 순서대로 읽는다. 명령 경계에 RPC 시간만큼의 오차가 있다.
Direct의 fault가 0이어야 하는 대상은 이미 매핑한 page다.
새로 쓰는 page의 fault까지 없어지는 것은 아니다.
무이동의 fault와 나란히 봐야 한다.
A는 로컬 lower disk, B는 NFS를 쓴다. 무이동 대비 시간에는 이 차이도 들어간다.
메모리는 coherent 파일 backing이다. 실물 비일관 CXL multi-host는 검증하지 않았다.

분석은 표준 Python만 쓴다.
`analyze.py SUITE/report.json ... --physical PHYSICAL/physical.json --output OUTPUT`으로
조건별 요약 JSON과 명령별 CSV(comma-separated values)를 만든다.
원시 run은 `implementation/virtual_machine/.local/run14/`와 `.local/runs/`에 둔다.

재실행의 공통 runner 옵션은 아래와 같다. 기존 SWE fixture를 쓰되,
L1 커널에는 `CONFIG_HIST_TRIGGERS=y`가 필요하다.
`shared-probe kvm-keepalive`와 control RPC operation 4도 fixture에 넣어야 한다.
`resident_prepare.py`의 `--shared-probe`, `--l1-kernel`, `--kvm-module-dir`로
별도 빌드한 바이너리를 포장했다.

```bash
numactl --cpunodebind=0 --membind=0 python3 implementation/virtual_machine/swe_handoff_run.py \
  --assets ASSETS --qemu QEMU --qemu-root QEMU_ROOT --session FRESH_SESSION --cpus 8-15 \
  --trajectory-policies heavy all --repetitions 3 --seed 20261116 \
  --heavy-plan docs/results/trajectory-faults-20261116/heavy.json \
  --l1-memory-mib 6144 --cxl-mib 8192 --count-faults --output OUTPUT
```

시간 실행은 `--count-faults`를 빼고 seed 20261117을 쓴다.
Runner가 lab·quiet 잠금을 관리한다. 빌드는 별도로 quiet 공유 잠금을 잡는다.
`physical_ept_cost.c`는 `gcc -O2 -Wall -Wextra -Werror`로 빌드한다.
`analyze.py --calibrate BINARY --output RAW_OUTPUT`은 node 0과 두 잠금을 지키며
물리 VM을 한 번에 하나씩 띄운다.

## 본 측정 결과 (1–4단계)

108개 run과 두 suite 전체 검증이 통과했다. 각 셀은 시간 3회와 계수 3회다.
실행 입력 identity와 실제 guest CPU flag 해시는 전체에서 같았다.
수집용 `ps`는 외부 부하 표시에서 제외했다. 시간 run의 부하 표시는 0개다.
계수 run 1개에는 외부 `python3` 부하가 있었다. 이 run도 집계에 포함했다.
정확한 값과 run 표시는 [summary.json](summary.json), [trials.csv](trials.csv)에 있다.

### 궤적 시간

초 단위 중앙값이다. Δ는 같은 fixture·정책의 무이동 중앙값과의 차이다.
`active`는 명령 구간과 이동 구간의 합이다. 실제 wall에는 반복 수신 준비와 기록도 들어간다.
수신 준비를 이전 명령이나 LLM 대기와 겹쳐 실행하는 것은 구현하지 않았다.

| Fixture | 정책 | 무이동 wall | Snapshot Δwall / Δactive | Direct Δwall / Δactive |
|---|---|---:|---:|---:|
| Gin | 무거운 명령 | 13.201 | 2.707 / 2.654 | 1.029 / 1.024 |
| Gin | 모든 명령 | 13.130 | 4.319 / 3.667 | 1.688 / 1.673 |
| PHP-CS-Fixer | 무거운 명령 | 3.952 | 0.906 / 0.802 | 0.756 / 0.750 |
| PHP-CS-Fixer | 모든 명령 | 3.935 | 3.010 / 2.143 | 1.288 / 1.267 |
| MicroPython | 무거운 명령 | 8.802 | 1.364 / 1.234 | 0.772 / 0.764 |
| MicroPython | 모든 명령 | 8.877 | 3.260 / 2.187 | 1.186 / 1.157 |

정지 시간의 공통 L0 경계는 요청부터 완전한 재개 확인 응답 수신까지다.
이 값은 실제 guest pause의 상한이다. 특히 direct의 바깥 relay 응답 지연이 크다.
아래 native 값은 source L1의 한 시계로 잰 pause→remote Resume 응답이다.
공통 L0 절대 시각을 대체하는 값으로 쓰지 않는다.

| Fixture | 정책 | Snapshot L0 합(ms) | Direct L0 합(ms) | Direct native 합(ms) | nested root 합(ms, 계수 평균) |
|---|---|---:|---:|---:|---:|
| Gin | heavy | 71.18 | 29.50 | 11.06 | 4.99 |
| Gin | all | 423.46 | 570.51 | 61.60 | 23.65 |
| PHP-CS-Fixer | heavy | 84.20 | 96.14 | 11.40 | 2.81 |
| PHP-CS-Fixer | all | 572.66 | 677.15 | 64.49 | 11.34 |
| MicroPython | heavy | 87.83 | 100.94 | 15.81 | 5.50 |
| MicroPython | all | 533.44 | 759.13 | 76.45 | 15.22 |

### 명령 중 EPT 위반

각 칸은 3회 평균이다. `/이동`은 명령 합을 이동 횟수로 나눈 값이다.
이동·명령 사이까지 포함한 전체 계수 합은 JSON의 `whole_interval_ept_mean`이다.

| Fixture | 정책 | 무이동 | Snapshot 합 / 이동당 | Direct 합 / 이동당 | 이동 횟수 |
|---|---|---:|---:|---:|---:|
| Gin | heavy | 260,870 | 517,034 / 172,345 | 263,285 / 87,762 | 3 |
| Gin | all | 260,779 | 583,527 / 21,612 | 265,020 / 9,816 | 27 |
| PHP-CS-Fixer | heavy | 34,112 | 112,090 / 22,418 | 38,676 / 7,735 | 5 |
| PHP-CS-Fixer | all | 34,096 | 208,298 / 5,951 | 38,810 / 1,109 | 35 |
| MicroPython | heavy | 86,957 | 180,409 / 30,068 | 88,134 / 14,689 | 6 |
| MicroPython | all | 86,966 | 284,289 / 6,611 | 88,309 / 2,054 | 43 |

계수 실행에서 snapshot은 이동 357회마다 VM/vCPU를 새로 만들었다.
Direct는 같은 357회 동안 VM/vCPU 생성이 0회였다.
실제 leaf는 모든 조건에서 4 KiB뿐이었다. 2 MiB와 1 GiB leaf는 0개였다.
이는 `--dax-4k`와 현재 mapper의 조건이다. SWE huge-page 궤적은 측정하지 않았다.

### 이동 뒤 남은 fault와 명령 시간 분포

아래 명령 시간 증가는 이동 구간을 제외한다.
각 반복을 같은 반복 번호의 baseline 명령과 비교했다.
3회×명령 수의 표본을 모아 중앙값 / 95백분위수(p95) / 최댓값을 구했다.
단위는 ms다. 이동을 포함한 step 분포도 JSON·CSV에 따로 있다.

| Fixture | 정책 | Snapshot 중앙 / p95 / 최대 | Direct 중앙 / p95 / 최대 |
|---|---|---:|---:|
| Gin | heavy | 0.04 / 818.92 / 1010.33 | -0.06 / 144.36 / 813.47 |
| Gin | all | 20.49 / 867.44 / 1036.63 | 0.26 / 170.41 / 791.74 |
| PHP-CS-Fixer | heavy | -0.16 / 123.12 / 247.53 | -0.27 / 172.03 / 417.64 |
| PHP-CS-Fixer | all | 20.88 / 123.55 / 244.95 | 0.52 / 129.71 / 202.85 |
| MicroPython | heavy | -0.00 / 86.33 / 651.55 | -0.31 / 33.90 / 668.57 |
| MicroPython | all | 20.63 / 82.81 / 383.79 | 0.13 / 40.65 / 202.88 |

명령 순번별 fault는 JSON의 `faults_by_after_move_ordinal`에 있다.
원시 명령 시각·노드·epoch·fault·baseline 대비 증가는 [commands.csv](commands.csv)에 있다.

| Fixture(heavy) | Snapshot 2번째 이후 EPT 합 | Direct 2번째 이후 EPT 합 | Snapshot fault가 남은 마지막 순번 |
|---|---:|---:|---:|
| Gin | 5,191 | 367 | 11 |
| PHP-CS-Fixer | 19,517 | 17,744 | 8 |
| MicroPython | 35,025 | 973 | 19 |

이 tail 합은 3회 전체다. 새로 쓰는 page와 background 작업도 포함한다.
원래 매핑한 page의 refault만을 식별한 수는 아니다.
모든 명령 정책에서는 다음 명령 전에 다시 옮기므로 이동 뒤 순번은 항상 1이다.

### 물리 비용 추정

전체 계수 구간 RAM fault의 baseline 대비 증가에 0.9855441853 µs를 곱했다.
아래는 **추정 추가 RAM fault 처리 비용(ms)**이다. 실제 물리 궤적을 잰 값이 아니다.
중첩 시간에서 보정값을 뺀 결과도 아니다.

| Fixture | 정책 | Snapshot 추정 추가(ms) | Direct 추정 추가(ms) |
|---|---|---:|---:|
| Gin | heavy | 252.98 | 2.38 |
| Gin | all | 324.69 | 4.18 |
| PHP-CS-Fixer | heavy | 77.91 | 4.50 |
| PHP-CS-Fixer | all | 180.40 | 4.66 |
| MicroPython | heavy | 93.46 | 1.16 |
| MicroPython | all | 205.19 | 1.33 |

[conditions.csv](conditions.csv)는 조건별 수치다. [validation.json](validation.json)은
입력·검증·생성 횟수·매핑 크기·정리 증거의 요약이다.
이 단계에서는 물리 multi-host 지연이나 hand-off의 전체 물리 이득을 결론내리지 않는다.

5단계 구현과 기능 시험은 [PREFAULT.md](PREFAULT.md)에 기록했다. 기본 1–4단계의 확정 결과와 추가 실험의 실행 binary는 구분한다.
