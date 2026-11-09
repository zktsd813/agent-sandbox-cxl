# 문서와 실험 결과

2026-11-03 정리. 실행 방법과 설정은 [저장소 README](../README.md)를 기준으로 한다.

**GPU 실험만 할 때는 [GPU 노드 harvest 설계](gpu-node-harvest-design.md) 하나만 읽으면 된다.** 배경 결과, 실험 G1~G5, A6000 실행 준비가 모두 들어 있다. 아래 목록은 출처를 찾을 때만 본다.

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
| [sandbox 크기 정책 sweep](results/cap-sweep-20261104/README.md) | 11-04 | simulator. 다언어 trace에서 hard cap은 넓은 task를 늦추고(cap 1 tool ×3.2), cap 없음은 노드 사이 contention을 만든다(192코어·90% 잃은 CPU 14.6%). 10ms hand-off는 둘 다 줄인다. Python RL rollout은 폭이 좁아 cap 비용이 거의 없다. Docker 실측(11-05, 16코어): 시작 순서를 섞으면 simulator와 맞는다(CPU 가중 1.175 vs 1.18). 같은 trace 사본이 함께 시작하면 1.66으로 커진다. runqueue 밖 간섭은 4~8% |
| [pool 이동 실측과 극단 부하](results/pool-mover-20261105/README.md) | 11-05 | Docker, 15코어 pool 2개 사이 실제 이동(cpuset). slowdown은 사용률에 비선형(CPU 가중 1.35, 1.84, 2.52, 4.26 at 41/65/83/95%). 60 ms 이동은 사용률 40~83%에서 공유 pool 대비 대기 차이의 61~96%를 되찾고 5초 이동은 10~14%. 한 노드에 몰린 GRPO group은 tool ×2.87 → ×1.98이지만 5초 이동도 같다. 95%에서 처음 규칙이 안 들은 것은 문턱 탓이었고, 고친 규칙도 명령 시간은 3~5%만 줄인다. 부하를 골라 만든 조건(빌드가 무거운 다언어 task, 빠른 API LLM, CPU 제한 없음)의 값이다 |
| [microVM 시스템 자원 경합](results/microvm-system-20261106/README.md) | 11-06 | Firecracker, 32코어 kernel 하나에 동시 VM 32~192개. exit·virtio·VMM syscall의 이벤트당 비용은 거의 그대로이고, 블록 latency(37배)·journal·lock 대기(VM 생성·종료)는 크게 오르지만 guest가 기다리지 않는다. 명령 ×1.10은 clock과 CPU 대기로 설명된다. task별 시스템 부하는 CPU를 따른다(순위 상관 0.71). 디스크 쓰기가 많은 task 3개만 예외다. Docker K=120에서 L3를 나눠도 slowdown 3.02 → 2.99. CPU 밖 자원에서 따로 생기는 기회는 없다 |
| [trace 성격 다시 보기, env 생성, sandbox 밖 CPU 작업](results/trace-character-20261109/README.md) | 11-09 | 명령 종류별 CPU: 탐색이 명령의 36~45%지만 CPU 1~4%이고, test·build·install이 trace CPU의 60%(Python RL)·94%(다언어)라 문자열만으로 미리 안다. 채점 45개를 microVM에서 다시 돌리니 agent CPU의 0.12배이고, iter2 기록에서도 step +0.7%로 몰리지 않는다. env 생성(기록 중앙값 94초, Azure Kubernetes pod)은 image가 아니라 생성 대기열이 정했다. 생성 한 번은 20~30초이고 우리 microVM boot는 0.28초다. 모두 1초로 줄여도 step은 약 3% 준다. image를 새로 풀면 압축 GiB당 약 15 CPU초지만 rollout은 풀린 image의 2%만 읽는다. DSec은 EROFS image를 3FS에서 lazy로 읽는다. GPU 노드 CPU 작업은 GPU process에 묶여 꾸준하다 |
| [초기 16개 pilot](replay-report.md) | 10-07 | 첫 Docker replay |

## CXL microVM 구현 (hand-off, pooling)

다른 머신에서 진행한 메커니즘 구현이다. 전체 흐름은 [CXL microVM 연구와 구현 정리](cxl-microvm-overview.md), 실행 방법은 [구현·사용법](../implementation/virtual_machine/README.md)에 있다.

| 문서 | 내용 |
|---|---|
| [구현 지도·기준 소스 대비 변경량](cxl-implementation-map.md) | 2026-11-04 누적 patch의 파일별 규모·역할, 자체 모듈, 두 handoff·NFS·pooling의 구현 흐름 |
| [resident direct](cxl-resident-direct.md) | 2026-11-04 native binary 제어, FD/장치 재사용, 동일 handoff의 구간별 비용과 nested EPT 분석 |
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
| [workload를 넓힌 노드 사이 CPU·LLC 경합](workload-diversity-plan.md) | 11-06 계획, GPU 없이 12시간. 11-05 실험은 부하를 골랐다. 밀도·vCPU·LLM 대기(iter2 RL serving)를 실제 조건에서 정하고, Python RL, Verified, 다언어, Terminal-Bench 묶음으로 지도를 그린다. LLC는 pool마다 L3를 나눠 옮김의 효과를 본다. sandbox 부하는 trace에 따라 10배 넘게 다르고, 노드 사이 burst는 묶음과 무관하게 독립이다 |

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
