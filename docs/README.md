# 문서와 실험 결과

2026-11-03 정리. 실행 방법과 설정은 [저장소 README](../README.md)를 기준으로 한다.

## 처음 읽는 순서

1. [HANDOFF](HANDOFF.md): 연구 배경, VM hand-off 설계, Phase A~F 원래 계획(2026-10-19)
2. [CXL microVM 연구와 구현 정리](cxl-microvm-overview.md): 실행 이전 구현, PML·MPTCP 검증, 파일 공유(2026-10-26)
3. [실험 계획 v2](experiment-plan-tail-placement.md) 8장: CPU 노드 사이 contention screen 결과. sandbox CPU는 critical path에서 얇아 contention 비용이 작다(2026-11-03)
4. [GPU 노드 harvest 설계](gpu-node-harvest-design.md): 현재 방향과 다음 실험 G1~G5(2026-11-03)

## 현재 방향 (2026-11-03)

| 문서 | 내용 |
|---|---|
| [GPU 노드 harvest 설계](gpu-node-harvest-design.md) | GPU 노드의 남는 CPU에 sandbox를 올리고, 학습이 CPU를 원하면 hand-off로 CPU만 비운다. A6000 × 2에서 CXL 없이 G1~G3를 먼저 본다 |
| [실험 계획 v2](experiment-plan-tail-placement.md) | locality 배치의 tail contention 가설과 sync·async screen 결과, 원본 job critical path. 방향 전환의 근거 |

## 완료한 측정

| 결과 | 날짜 | 내용 |
|---|---|---|
| [264 trace DRAM/CXL](results/numa-immediate-20261014/README.md) · [비교표](results/numa-immediate-20261014/report.md) | 10-14 | Docker replay의 latency, MPKI, 메모리. CXL tax trace 중앙값 +11.2% |
| [tool call write footprint](results/write-pages-node1-20261021/write-report.md) | 10-21 | 명령 단위 write page와 메모리 비율 CDF |
| [microVM CPU scheduling](microvm-scheduling.md) | 10-14 | Preact trace의 1·2 vCPU 비교, guest interrupt와 host vCPU 스케줄링 |
| [microVM handoff 초기 실험](microvm-handoff.md) | 10-14 | 상태 분류, NUMA 실행 실험, Firecracker 빌드 |
| [microVM contention 실측](../experiments/contention/RESULTS-20261021.md) · [fleet 시뮬레이션](../experiments/contention/RESULTS-fleet.md) | 10-21 | 4 node × 5코어 microVM pilot(짧은 SWE trace 67개)과 DSec 규모 fleet simulation(trace 264개) |
| [공개 RL rollout microVM 실측](results/virtual-node-rl-20261026/README.md) | 10-26~11-03 | virtual node 필요성 실측, fleet sim 보정, placement screen(`measured/placement-screen-*.json`) |
| [초기 16개 pilot](replay-report.md) | 10-07 | 첫 Docker replay |

## CXL microVM 구현 (hand-off, pooling)

다른 머신에서 진행한 메커니즘 구현이다. 전체 흐름은 [CXL microVM 연구와 구현 정리](cxl-microvm-overview.md), 실행 방법은 [구현·사용법](../implementation/virtual_machine/README.md)에 있다.

| 문서 | 내용 |
|---|---|
| [resident direct](cxl-resident-direct.md) | 목적지 FD와 장치 실행 환경을 유지하는 direct handoff. 2026-11-02 최신 구현 |
| [direct handoff API 비용](cxl-direct-api-latency.md) | 매번 목적지 VMM을 만들던 경로의 분석과 과거 1 ms 미만 결과 |
| [RAM 전수 순회를 제거한 pooling handoff](cxl-pool-incremental-handoff.md) | pooling handoff의 증분 처리 |
| [guest metadata 기반 pooling](cxl-guest-pool-handoff.md) | guest page cache 공유와 direct·state-only 두 handoff 모드 |
| [읽기 데이터 페이지 pooling](cxl-data-pooling.md) | immutable data page pooling |
| [버린 공유 페이지의 재사용](cxl-page-discard.md) | guest가 버린 page를 복사 없이 재사용(10-30) |
| [실제 SWE trace 위 pooling과 왕복 이전](cxl-swe-pool-traces.md) | Gin, PHP-CS-Fixer, MicroPython의 기록된 명령 재생 |
| [일반 Firecracker와의 차이](firecracker-pooling-comparison.md) | 수정하지 않은 Firecracker v1.17.0과 비교 |

## 실험 설계 (bud04·bud05, CPU 노드)

| 문서 | 상태 |
|---|---|
| [virtual node 필요성 실험](virtual-node-experiment-design.md) | E0·E1·E2·E4 일부 실행. 결과는 [results/virtual-node-rl-20261026](results/virtual-node-rl-20261026/README.md) |
| [동기 step 재생 v2와 image pool](sync-replay-design.md) | 구현과 검증 완료(10~11절) |
| [다음 실험 계획 (11-02)](experiment-program-20261102.md) | 실험 계획 v2로 대체됨 |
| [실험 계획 v2](experiment-plan-tail-placement.md) | 8장 screen 완료. M2(쏠림이 group 완료를 늦춘다)는 기준 미달 |
| [DSec식 co-location 간섭](dsec-interference-design.md) | host microbenchmark(4절)만 실행. I1 대기열은 실행 전 |
| [밀도 sweep](density-sweep-design.md) | 대기열 작성, 실행 전 |

## RL trace

| 문서 | 내용 |
|---|---|
| [RL 학습 sandbox trace 조사](rl-trace-survey.md) | image가 있는 SWE 문제 set, model, RL framework (10-23) |
| [RL rollout trace 수집 계획](trace-collection-plan.md) | A6000 시험 + H100 본 수집 계획(10-25). 지금까지의 실측은 공개 rollout(sweagent/iter2-rl-rollouts)을 썼다 |
| [호출 시간 trace와 DSec burstiness](timing-traces.md) | LLM wait 정의와 DSec 관측의 해석 범위 (10-18) |

## 조사

| 문서 | 내용 |
|---|---|
| [sandbox load balancing](load-balancing-survey.md) | 왜 불균형이 안 보이고 언제 깨지나, 문헌과 우리 데이터 (11-02) |
| [논문별 harness 조사](harness-survey.md) | agent 논문의 harness와 worker 연결 지점 |
| [GPU 없이 tool trace 재생](trace-replay.md) | SWE-agent, mini-swe-agent의 replay 경로 |
| [공개 trace 재실행 가능성](replay-readiness.md) | Open-SWE-Traces 실행 준비 |

## 초기 기록 (2026-10 초 스냅샷)

아래 문서와 [`evidence/`](evidence/)는 10월 초의 조사·준비 기록이다. 서버의 여유 공간, CXL online 상태, 설치 여부는 당시 관측이며 현재 상태가 아니다. 각 문서 맨 위에 "초기 조사/실험 기록" 표시가 있다.

- [node 0 초기 계획](node0-replay-plan.md)
- [이 서버에서 Open-SWE-Traces 실행](server-run-guide.md)
- [bud04·bud05 worker 설계안](worker-design.md)

`docs/archive/`(AgentCgroup 이전 소스)와 upstream checkout은 Git 배포에서 제외한다.
