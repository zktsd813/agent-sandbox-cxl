# sandbox 크기 정책 sweep: hard cap, cap 없음, image별 크기, hand-off (2026-11-04)

이 문서의 1~5절은 simulator 결과이고, 실측이 아니다. 코드는 [`cap_sweep.py`](../../../experiments/contention/cap_sweep.py)이고 node 모델은 [`fleet.py`](../../../experiments/contention/fleet.py)를 그대로 쓴다.

simulator에는 runqueue 경합(코어 나눠 갖기)만 있다. cache, 메모리 대역폭, 주파수, quota throttling, I/O에 의한 간섭은 없다. 같은 구성을 Docker로 잰 결과는 8절에 있다. 실측은 1~7절의 두 가정을 고친다. 첫째, 경합 때 CPU를 thread가 아니라 container 단위로 나누므로 넓은 sandbox의 대기가 simulator보다 훨씬 크다. 둘째, 입력 profile이 낮은 clock에서 측정됐다.

## 0. 결론

1. **hard cap은 그 자체로 느리다.** 264개 다언어 trace(Go, Rust, Java, JS, PHP, Ruby, C)를 cap 1·2·4·8 vCPU로 줄이면 contention이 없어도 다음과 같다.
   - tool 시간: ×3.18, ×1.89, ×1.31, ×1.09
   - LLM 대기를 포함한 rollout 시간 p99: ×4.29, ×2.48, ×1.59, ×1.20
   - cap 1에서는 60초를 새로 넘는 명령이 218개 생긴다.
2. **cap을 풀면 노드 사이 contention이 생긴다.** 사용률 목표 90%에서 cap 없음의 잃은 CPU는 192코어 노드 14.6%, 96코어 28.8%, 48코어 50.2%다. 같은 조건에서 cap 1은 0.0%, 0.3%, 1.2%다.
3. **고정된 크기 중에는 둘 다 피하는 값이 없다.** tool 시간(cap 때문에 늘어난 몫 × contention 대기)의 최솟값은 목표 90%에서 다음과 같다.
   - 192코어: cap 16의 ×1.13
   - 96코어: cap 8·16의 ×1.23
   - 48코어: cap 8의 ×1.40

   image별 크기(image90)는 cap 16과 같다.
4. **hand-off는 두 비용을 함께 줄인다.** cap 없음 + 10ms hand-off의 tool 시간은 192, 96, 48코어에서 ×1.03, ×1.07, ×1.14다. rollout p99는 1.02, 1.04, 1.07이고, cap 없음만 쓴 경우는 1.11, 1.21, 1.34다. pre-copy migration은 잃은 CPU를 14.6% → 13.4%로 거의 줄이지 못한다.
5. **이득은 노드가 작을수록, 사용률이 높을수록 크다.** 192코어·목표 80%에서는 가장 좋은 고정 크기도 tool 시간이 ×1.05라 hand-off의 몫(→ ×1.00)이 작다.
6. **동기 RL step에서도 모양은 같지만 차이는 작다.** 192코어, 실제 사용률 약 76%에서 step 시간 p99는 다음과 같다(4절).
   - cap 4: 1.65
   - cap 16: 1.09
   - cap 없음: 1.08
   - cap 없음 + hand-off: 1.02
   - 이상적 공유: 1.02
7. **Python RL rollout에서는 cap의 비용이 거의 없다.** 공개 RL rollout(SWE-rebench 계열, Python) 48개를 8 vCPU로 단독 실행했다. 1코어를 넘는 구간에서 쓴 CPU가 19%뿐이고, cap 1에서도 rollout이 0.8%만 늘어난다(6절). 1~6은 compile이 많은 다언어 task에만 해당한다.
8. **실측에서는 cap 없음의 contention이 simulator보다 훨씬 크다(8절).** 16코어 pool 하나, 사용률 37%에서 cap 없음의 CPU 가중 slowdown은 실측 1.66, simulator 1.18이다.
   - 넓은 빌드는 2~3배 느려진다. Docker는 경합 때 CPU를 container마다 똑같이 나눈다. simulator는 thread마다 나눠서 넓은 sandbox를 thread 수만큼 대접한다.
   - cache·메모리 대역폭·clock 간섭은 4~8%로 작다.
   - 같은 부하에서 cap 4와 cap 없음은 비긴다(0.97~0.98). cap 1은 1.5~2배 느리다.
   - 그래서 2~5의 cap 없음 수치는 Docker·Kubernetes 환경에 낙관적이다. 대신 hand-off가 되찾을 몫은 더 크다.

## 1. 질문

운영자는 sandbox 크기를 job마다 상수 하나로 정한다. 보통 1~2 vCPU이고, 넓은 task가 생기면 손으로 고친다(7절).

- 작은 hard cap은 넓은 task를 느리게 한다.
- cap이 없거나 request만 거는 방식은 넓은 burst 때문에 노드 사이 평탄화를 깨뜨린다.

이 두 비용의 크기를 재고, hand-off가 어느 고정 크기보다 나은지를 본다.

## 2. 방법

| 항목 | 내용 |
|---|---|
| 수요 | 10-14 Docker DRAM 재생(`harness/trace-replay/results/numa-immediate-20261014/node0`). trace 264개, cpuset 30코어, CPU quota 없음, cgroup CPU 20ms 간격. 명령 구간마다 10ms slot의 코어 수요로 바꿨다 |
| cap 적용 | 수요 d > V인 slot은 V의 속도로 d/V slot 동안 이어지고, 뒤 slot은 그만큼 밀린다. 늘어난 곡선을 다시 10ms 격자에 맞춘다. 일의 양은 그대로이고, 명령마다 마지막 slot만 올림된다 |
| LLM 대기 | trace에 기록된 응답 간격(`replay.load_trace`의 `gap`). Gemini 3.5 Flash eval trace라 공개 RL rollout보다 짧다. tool 비중이 rollout 시간의 26%다 |
| 크기 정책 | `cap1`~`cap16`, `cap30`(cap 없음: 재생 때 보인 30코어 전부), `image50`·`image90`(CPU 시간의 50%·90%가 그 폭 이하에서 돈 2의 거듭제곱, 최대 16) |
| cluster | 노드 16개 × 48/96/192코어. 새 sandbox는 power-of-2로 덜 바쁜 노드에 놓는다(DSec). 자리마다 무작위 trace를 이어서 재생한다. 노드당 sandbox 수는 목표 사용률 60·70·80·90% ÷ 정책별 평균 수요 |
| 메커니즘 | 없음, 10ms hand-off(출발 노드 CPU 2ms), pre-copy migration(100Gb/s, trace별 guest 메모리), 이상적 공유(모든 노드가 코어 하나의 pool) |
| 시간 | ramp 600초 + 측정 5,400초, seed 2개 평균. 명령은 60초에서 자르지 않는다(자르면 cap의 비용이 숨는다) |
| 지표 | 잃은 CPU = runqueue 대기 / 실행. tool 시간 = 단독 실행에서 cap으로 늘어난 tool 시간 × (1 + 잃은 CPU). rollout = 측정 구간 안에서 끝난 trace의 (LLM 대기 포함 시간) ÷ cap 없는 단독 시간 |

**정정 (2026-11-04).** 처음 버전의 cap 적용은 V를 조금 넘는 slot(예: 1.03코어)을 V짜리 slot과 나머지 0.03짜리 10ms slot 둘로 바꿨다. 그래서 cap의 비용을 부풀렸다(cap 1 tool ×3.53, 고친 뒤 ×3.18). local commit 68ed89d의 수치가 이 처음 버전이고, 이 문서는 고친 버전으로 모두 다시 돌린 결과다.

## 3. 결과

### 3.1 cap 자체의 비용 (contention 없음)

| 정책 | vCPU 분포 | tool 시간 | rollout p50 | p90 | p99 | 60초를 새로 넘는 명령 | sandbox 평균 수요(코어) |
|---|---|---|---|---|---|---|---|
| cap1 | 1 | ×3.18 | 1.17 | 2.47 | 4.29 | 218 | 0.490 |
| cap2 | 2 | ×1.89 | 1.05 | 1.61 | 2.48 | 115 | 0.624 |
| cap4 | 4 | ×1.31 | 1.01 | 1.24 | 1.59 | 30 | 0.710 |
| cap8 | 8 | ×1.09 | 1.00 | 1.08 | 1.20 | 8 | 0.751 |
| cap16 | 16 | ×1.02 | 1.00 | 1.02 | 1.05 | 3 | 0.764 |
| cap 없음 | 30 | ×1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.768 |
| image50 | 1:86, 2:30, 4:25, 8:55, 16:68 | ×1.05 | 1.01 | 1.03 | 1.08 | 8 | 0.757 |
| image90 | 2:91, 4:16, 8:18, 16:139 | ×1.02 | 1.00 | 1.02 | 1.05 | 3 | 0.764 |

image90은 264개 중 139개에 16 vCPU를 준다. 넓게 쓰는 repo가 많아서 image별 크기도 결국 넓은 sandbox가 섞인 fleet이 된다.

### 3.2 노드 사이 contention: 잃은 CPU(%), 이동 없음, 목표 80% / 90%

| 노드 코어 | cap1 | cap2 | cap4 | cap8 | cap16 | cap 없음 | image90 |
|---|---|---|---|---|---|---|---|
| 48 | 0.1 / 1.2 | 1.2 / 4.5 | 5.2 / 14.0 | 13.8 / 28.4 | 24.3 / 41.7 | 33.5 / 50.2 | 24.3 / 42.0 |
| 96 | 0.0 / 0.3 | 0.2 / 1.8 | 1.4 / 6.6 | 5.0 / 13.5 | 9.1 / 21.1 | 14.5 / 28.8 | 10.2 / 21.5 |
| 192 | 0.0 / 0.0 | 0.0 / 0.6 | 0.2 / 2.5 | 1.4 / 6.2 | 3.5 / 11.0 | 5.8 / 14.6 | 3.5 / 11.0 |

cap 없음에서 메커니즘별로 보면 다음과 같다.

| 노드 코어 | 이동 없음 | 10ms hand-off | pre-copy | 이상적 공유 |
|---|---|---|---|---|
| 48 | 33.5 / 50.2 | 7.0 / 13.7 | 32.4 / 47.8 | 0.35 / 2.8 |
| 96 | 14.5 / 28.8 | 2.0 / 6.9 | 13.7 / 26.6 | 0.02 / 0.89 |
| 192 | 5.8 / 14.6 | 0.42 / 2.8 | 5.0 / 13.4 | 0.00 / 0.16 |

- 대기가 길어지면 같은 sandbox 수에서도 실제 사용률이 목표보다 낮아진다. cap 없음·목표 90%의 실제 사용률은 48·96·192코어에서 84%, 87%, 88%다.
- hand-off는 cluster 전체에서 초당 54~62번 옮긴다. 노드당 초당 3.4~3.9번이다.

### 3.3 정책 비교: tool 시간

목표 90%:

| 노드 코어 | cap1 | cap2 | cap4 | cap8 | cap16 | cap 없음 | image50 | image90 | cap 없음 + hand-off | cap16 + hand-off | cap 없음 + pre-copy |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 48 | ×3.22 | ×1.97 | ×1.50 | ×1.40 | ×1.44 | ×1.50 | ×1.45 | ×1.45 | **×1.14** | ×1.12 | ×1.48 |
| 96 | ×3.19 | ×1.92 | ×1.40 | ×1.23 | ×1.23 | ×1.29 | ×1.26 | ×1.24 | **×1.07** | ×1.06 | ×1.27 |
| 192 | ×3.18 | ×1.90 | ×1.35 | ×1.16 | ×1.13 | ×1.15 | ×1.16 | ×1.13 | **×1.03** | ×1.03 | ×1.13 |

목표 80%에서 가장 좋은 고정 크기와 cap 없음 + hand-off를 비교하면 다음과 같다.
- 48코어: cap8 ×1.24 대 ×1.07
- 96코어: cap16 ×1.11 대 ×1.02
- 192코어: cap16 ×1.05 대 ×1.00

### 3.4 rollout 시간 p90 / p99, 목표 90%

| 노드 코어 | cap4 | cap8 | cap16 | cap 없음 | cap 없음 + hand-off |
|---|---|---|---|---|---|
| 48 | 1.32 / 1.78 | 1.18 / 1.45 | 1.15 / 1.35 | 1.15 / 1.34 | 1.04 / 1.07 |
| 96 | 1.27 / 1.68 | 1.12 / 1.32 | 1.08 / 1.20 | 1.09 / 1.21 | 1.02 / 1.04 |
| 192 | 1.25 / 1.63 | 1.09 / 1.26 | 1.05 / 1.13 | 1.05 / 1.11 | 1.01 / 1.02 |

rollout 시간에는 LLM 대기가 들어 있어서 tool 시간보다 차이가 작다. tool이 rollout 시간의 26%뿐이라, tool 시간 ×1.15는 rollout에서 약 1.04가 된다.

## 4. 동기 RL step

설정은 다음과 같다.
- 노드 16개 × 192코어, job마다 batch 16 × group 8.
- group 8개는 같은 trace를 복제했다. burst 상관이 가장 큰 경우다.
- step은 128개 rollout 중 가장 늦은 것이 끝나면 끝나고, train 30초 뒤에 다음 step을 시작한다.
- ramp 1,800초 + 측정 7,200초, seed 2개.

step 시간은 cap 없는 단독 실행으로 계산한 이상적 step 시간에 대한 비율이다.

동기 step에서는 sandbox가 가장 늦은 rollout과 학습을 기다리는 동안 논다. 그래서 async와 같은 밀도에서는 실제 사용률이 23~34%에 그친다. 아래는 밀도를 async 목표의 200%·240%로 올린 결과다.

step 시간 p90 / p99:

| 정책 | 실제 사용률 약 76% | 실제 사용률 약 89% |
|---|---|---|
| cap1 | 3.02 / 4.24 (사용률 66%) | 3.02 / 4.31 (79%) |
| cap2 | 1.81 / 2.52 (73%) | 1.88 / 2.58 (87%) |
| cap4 | 1.28 / 1.65 | 1.36 / 1.75 |
| cap8 | 1.07 / 1.25 | 1.16 / 1.36 |
| cap16 | 1.04 / 1.09 | 1.12 / 1.24 |
| image90 | 1.05 / 1.09 | 1.12 / 1.24 |
| cap 없음 | 1.04 / 1.08 | 1.12 / 1.25 |
| cap 없음 + pre-copy | 1.03 / 1.06 | 1.10 / 1.21 |
| **cap 없음 + 10ms hand-off** | **1.01 / 1.02** | **1.07 / 1.17** |
| cap 없음 + 이상적 공유 | 1.01 / 1.02 | 1.05 / 1.13 |

- **사용률 약 76%**: hand-off는 이상적 공유와 같다.
- **사용률 약 89%**: cluster 전체가 포화되기 시작해서, 이상적 공유도 잃은 CPU가 15.6%다(hand-off 17.0%, cap 없음 26.8%). 그 아래는 이동으로 되찾을 수 없다.
- **cap의 비용은 사용률과 상관없이 step 시간에 남는다.**

## 5. 한계

- **runqueue 경합만 있는 유체 모델이다.** cache·메모리 대역폭·주파수 간섭, quota throttling, 실측 scheduler 대기(`kernel_wait`)가 없다. bud05 microVM 실측에서는 runqueue 대기가 0%일 때도 명령이 1.21배 느렸고, 그중 67%가 같은 일에 CPU 시간이 더 드는 몫이었다([virtual-node 결과](../virtual-node-rl-20261026/README.md) 6번). Docker 실측(8절)에서는 이 몫이 4~8%로 작았다.
- **경합 때 CPU를 thread 단위로 나눈다.** `fleet._water_fill`은 수요 d인 sandbox를 thread ⌈d⌉개로 보고 thread마다 똑같이 나눈다. Docker(cgroup v2, container마다 같은 `cpu.weight`), Kubernetes(weight ∝ request), cgroup에 넣은 Firecracker는 container(VM)마다 나눈다. 그래서 넓은 sandbox의 대기를 크게 과소평가한다(8.3절, 16코어·사용률 37%에서 CPU 가중 1.18 vs 실측 1.66).
- **입력 profile을 낮은 clock에서 쟀다.** 10-14 재생은 governor schedutil, 저부하라 clock이 낮았다. performance로 고정한 실측(약 4 GHz)과 비교하면 같은 trace의 tool 시간이 1.62배, CPU 시간이 2.12배다. 그래서 rollout에서 tool이 차지하는 비중이 실제보다 크고(48개 기준 16.7% vs 11.0%), 노드당 sandbox 수는 같은 사용률에서 약 절반으로 잡혔다(8.1절).
- **cap 없음의 폭이 30코어에서 잘렸다.** 192코어 노드에서 cap이 없으면 tool은 더 많은 worker를 띄운다.
- **cap을 걸면 tool이 `nproc`에 맞춰 worker를 줄인다.** 병렬 overhead가 줄어서 실제 cap의 비용은 조금 작을 수 있다. CFS quota(`--cpus`)는 `nproc`을 바꾸지 않아서 반대로 throttling이 생긴다.
- **hand-off 모델이 넓은 sandbox에 낙관적이다.** 목적지 조건이 "빈 코어 1개 이상"이고, 정지 10ms와 CPU 2ms를 vCPU 수와 상관없이 같게 뒀다.
- **rollout 지표는 측정 구간 안에서 끝난 trace만 센다.** 그래서 짧은 trace 쪽으로 조금 치우친다.

## 6. Python RL rollout의 폭

공개 RL rollout(Qwen3.5 GRPO, `candidates-plan.json`)에서 image마다 하나씩 51개를 8 vCPU microVM으로 단독 실행했다(bud05 NUMA 0, 명령 연속 재생). 분석에는 48개를 썼다.

| 항목 | Python RL 48개 | 다언어 264개 |
|---|---|---|
| CPU 사용 시간 중 1 / 2 / 4코어를 넘는 구간의 비율 | 19% / 10% / 6% | 73% / 59% / 41% |
| trace별 100ms 최대 폭, 중앙값 | 1.5코어 | 11코어 |
| cap 1 / 2 / 4의 tool 시간 | ×1.21 / ×1.05 / ×1.02 | ×3.18 / ×1.89 / ×1.31 |
| rollout 시간 중 tool 비중 | 3.9% | 26% |
| cap 1의 rollout 시간 | ×1.008 | p50 ×1.17, p90 ×2.47 |

trace의 21%가 순간적으로 4코어를 넘지만, 그 구간이 짧고 tool 비중도 작다. 그래서 Python 위주 RL에서는 1 vCPU hard cap이 거의 비용이 없고, 1~4절의 trade-off가 생기지 않는다. 이 trade-off는 빌드와 테스트가 무거운 다언어 task에서 생긴다.

실행: `.local/runs/20261104T092752Z-solo-pdkup4sy`, 이름 목록 `.local/cap/py-width-names.txt`.

## 7. 운영자는 vCPU 수를 어떻게 정하나 (조사, 2026-11-04)

- **framework 상수**: R2E-Gym/DeepSWE request 1 CPU, SkyRL 1 CPU, ROCK 2 CPU, ROLL SWE 4 CPU, SandboxFusion CFS quota 1 CPU
- **provider 기본값**: E2B 2, Daytona 1, Runloop 1, Modal 0.125코어 request
- **task별 선언**: Terminal-Bench 2는 89개 중 84개가 1, 3개가 2, 2개가 4
- **실패 뒤 키우기**: OpenHands는 SWE-Gym 2,119개 중 33개만 2배로 정해 두고, 실행이 실패하면 2배씩 8배까지 키운다

RL에서 16 vCPU를 쓰는 예는 찾지 못했다. 강제 방식은 둘로 나뉜다. Docker 계열은 hard cap(`--cpus`, CFS quota)이고, Kubernetes와 managed provider는 burst가 가능한 request다. request 방식에서는 sandbox가 보이는 코어만큼 퍼진다. 예를 들어 SkyRL에서는 Jest가 64 worker를 띄워 `--maxWorkers=2`로 막아야 했다.

출처:
- [E2B](https://docs.e2b.dev/sandbox/compute), [Modal](https://modal.com/docs/guide/resources), [Daytona](https://www.daytona.io/docs/en/sandboxes.md)
- [R2E-Gym](https://github.com/R2E-Gym/R2E-Gym/blob/main/src/r2egym/agenthub/runtime/docker.py), [SkyRL](https://github.com/NovaSky-AI/SkyRL/blob/main/examples/train_integrations/harbor/harbor_trial_config/default.yaml), [ROCK](https://github.com/alibaba/ROCK/blob/main/rock/deployments/docker.py), [SandboxFusion](https://github.com/bytedance/SandboxFusion/blob/main/sandbox/runners/base.py)
- [OpenHands resource map](https://github.com/All-Hands-AI/OpenHands/blob/0.40.0/evaluation/benchmarks/swe_bench/resource/mapping.py), [Terminal-Bench 2](https://github.com/laude-institute/terminal-bench-2), [DSec](https://arxiv.org/html/2609.22978v1)

## 8. 실측: 16코어 pool 하나 (2026-11-05)

simulator에 없는 간섭까지 넣어서 같은 구성을 bud05 NUMA 0에서 Docker로 돌렸다([`real_cap.py`](../../../experiments/contention/real_cap.py)).

| 항목 | 설정 |
|---|---|
| 노드 | 코어 16개(2-17) 하나의 pool. SMT sibling(66-81)은 비워 둔다. governor `performance`, turbo 켜짐(busy clock 3.96~4.03 GHz, 조건 사이 차이 1.5%) |
| trace | 다언어 264개 중 48개(`measured-traces.json`). 넓게 쓰는 것 24개(100ms 최대 폭 8코어 이상)와 좁게 쓰는 것 24개(4코어 미만). Docker peak 4 GiB 이하, 10-14 측정에서 timeout 없음, 전체 시간 900초 이하 |
| 구조 | `experiment.py --schedule gaps --starts together --setup on-arrival --max-active K`. 기록된 LLM 간격을 넣는다. container 하나가 끝나면 대기열의 다음 trace를 바로 시작해 늘 K개가 돈다 |
| 정책 | cap 없음, `--cpus 4`, `--cpus 1`(CFS quota. `nproc`은 16 그대로) |
| 부하 | K = 동시에 도는 sandbox 수. K=4는 기준이다(trace마다 1번, 사용률 약 5%). K=32는 trace마다 4번 돌리고, 측정 구간 사용률은 30~37%다 |
| 측정 | 명령마다 시간, cgroup CPU 시간, throttling, PSI cpu some. socket 0 메모리 대역폭(resctrl MBM), workload CPU의 IPC와 clock(perf cycles, ref-cycles) |
| 비교 | 명령마다 같은 명령의 K=4 시간과 비교한다. K=32는 동시 실행 수가 32인 구간만 쓴다. 600초 timeout에 걸린 명령(1개)과 CPU 시간이 기준과 2배 넘게 다른 명령(다운로드 정지 등, 비교마다 0~42개)은 비율에서 뺀다 |
| 지표 | 시간 합계 = Σ실측 시간 ÷ Σ기준 시간. CPU 가중 = 명령별 (실측 ÷ 기준) 시간 비를 기준 CPU로 가중 평균한 값. simulator의 1 + 잃은 CPU가 이 값과 같은 정의다. 무거운 명령 = 기준 CPU 1초 이상(K=32 구간에서 514~526개) |
| simulator | `real_cap.py simulate`. fleet.py, 노드 1개 × 16코어, 같은 K와 정책. 수요는 이번 cap 없음 K=4 측정에서 만들고, seed 4개 |

### 8.1 governor: schedutil 기준은 느리다

첫 실행(11-04)은 governor를 schedutil로 둔 채 돌렸다. 같은 일을 하는데 K=4가 K=32보다 CPU 시간을 1.35배 썼다(cycles 합은 같다). 부하가 낮으면 clock도 낮아지기 때문이다. 그래서 이 실행(`measured-schedutil-analysis.json`)은 부하 비교에 쓰지 않는다. bud05의 모든 CPU를 performance로 고정하고 다시 돌렸다. 아래는 모두 이 재실행 결과다.

1~7절 simulator의 입력인 10-14 profile도 저부하 schedutil에서 쟀다. schedutil 재실행과 10-14는 같은 48개 trace의 tool 시간이 1.02배로 같다. 반면 고정 clock 재실행과 비교하면 10-14는 tool 시간이 1.62배, CPU 시간이 2.12배다. 그래서 rollout에서 tool이 차지하는 비중은 16.7%가 아니라 11.0%이고, sandbox 하나의 평균 수요는 절반쯤이다.

### 8.2 조건별 측정

| 조건 | 사용률 | 노드 포화 시간 | PSI some / 명령 시간 | throttled / 명령 시간 | IPC | 메모리 대역폭 GB/s | clock GHz |
|---|---|---|---|---|---|---|---|
| cap 없음 K=4 | 약 5% | 0.9% | 0.02 | 0 | 1.94 | 0.72 | 4.02 |
| cap 4 K=4 | 약 5% | 0% | 0.08 | 0.98 | 1.93 | 0.69 | 4.03 |
| cap 1 K=4 | 약 4% | 0% | 0.41 | 4.26 | 1.78 | 0.67 | 4.03 |
| cap 없음 K=32 | 37.4% | 18.1% | 0.17 | 0 | 1.85 | 7.36 | 3.96 |
| cap 4 K=32 | 35.5% | 11.9% | 0.14 | 0.26 | 1.89 | 6.44 | 3.97 |
| cap 1 K=32 | 30.4% | 0.1% | 0.39 | 3.64 | 1.76 | 6.04 | 3.98 |

노드 포화 시간은 container 전체가 15.5코어 이상 쓴 100ms 구간의 비율이다. throttled는 CPU마다 더한 값이라 1을 넘는다. K=4의 사용률은 같은 조건 simulator 값이다.

### 8.3 실측과 simulator

| 비교 | 실측 시간 합계 | 실측 CPU 가중 | 무거운 명령 p50 / p90 | 무거운 명령 CPU | simulator CPU 가중 |
|---|---|---|---|---|---|
| 부하만: cap 없음 K=32 ÷ K=4 | 1.28 | **1.66** | 1.09 / 1.95 | 1.06 | **1.18** |
| 부하만: cap 4 K=32 ÷ K=4 | 1.16 | 1.24 | 1.06 / 1.41 | 1.05 | 1.03 |
| 부하만: cap 1 K=32 ÷ K=4 | 1.07 | 1.08 | 1.05 / 1.15 | 1.04 | 1.00 |
| cap만: cap 4 ÷ cap 없음, K=4 | 1.08 | 1.25 | 1.02 / 1.39 | 0.96 | 1.33 (시간 합계 1.13) |
| cap만: cap 1 ÷ cap 없음, K=4 | 1.89 | 3.32 | 1.64 / 4.43 | 0.95 | 3.57 (시간 합계 2.07) |
| 같은 부하: cap 4 ÷ cap 없음, K=32 | **0.97** | **0.98** | 0.99 / 1.12 | 0.96 | 1.16 (곱으로 추정) |
| 같은 부하: cap 1 ÷ cap 없음, K=32 | 1.52 | 2.01 | 1.51 / 2.52 | 0.92 | 3.03 (곱으로 추정) |

같은 부하 행의 simulator 값은 (cap 늘어남 × (1 + cap의 잃은 CPU)) ÷ (1 + cap 없음의 잃은 CPU)로 추정했다. simulator 사용률은 cap 없음 K=32에서 38.1%로 실측(37.4%)과 맞는다.

1. **runqueue 밖 간섭은 작다.** K=32에서 같은 무거운 명령의 CPU 시간은 4~6% 늘고, IPC는 1.94에서 1.85로 4% 떨어지고, clock은 1.4% 낮아진다. 메모리 대역폭은 7 GB/s로 socket 대역폭에 한참 못 미친다. cap 1 K=32는 노드가 거의 포화되지 않는데(0.1%) 명령이 5~8% 느려진다. 이 몫이 runqueue 밖 간섭이다.
2. **CPU를 기다리는 일은 노드가 꽉 찼을 때만 생긴다.** cap 없음 K=32에서 PSI some의 94%가 노드 포화 구간(시간의 18%)에 몰린다. 빈 코어가 있는데 기다리는 일은 거의 없다. scheduler는 work-conserving이다.
3. **그런데 simulator는 넓은 명령의 대기를 크게 과소평가한다.** cap 없음 K=32의 CPU 가중 slowdown이 실측 1.66, simulator 1.18이다. 잃은 몫이 0.66 대 0.18로 3.7배다.
   - fmt, caddy, hugo, preact 빌드는 4번 반복 모두 2~3배 느려졌다. 예를 들어 fmt 49번 명령은 K=4에서 20.7초 동안 평균 4.2코어를 썼고, K=32에서는 50~57초 동안 평균 1.85코어를 받았다.
   - 원인은 CPU를 나누는 단위다. simulator(`_water_fill`)는 수요 16코어인 sandbox를 thread 16개로 보고 thread마다 똑같이 나눈다. Docker는 container cgroup마다 같은 `cpu.weight`라 container마다 똑같이 나눈다.
   - 포화 구간에서는 평균 7개 container가 기다린다. 그래서 넓은 빌드는 16/8 ≈ 2코어를 받는다.
4. **cap 자체의 비용은 simulator와 비슷하고 조금 작다.** CPU 가중으로 cap 4는 1.25(simulator 1.33), cap 1은 3.32(3.57)다. cap을 걸면 같은 명령의 CPU 시간이 4~5% 준다. tool이 quota에 맞춰 덜 병렬로 돌면서 병렬 overhead가 줄어든 것으로 보인다.
5. **같은 부하에서 cap 4와 cap 없음은 비긴다.** K=32에서 cap 4 ÷ cap 없음은 시간 합계 0.97, CPU 가중 0.98, 무거운 명령 p90 1.12다. simulator는 cap 4가 1.16배 느리다고 봤다. cap 없음의 실제 경합이 커서 cap 4의 비용을 상쇄한다. cap 1은 같은 부하에서도 1.5~2배 느리다.
   - cap 4 K=32에서 caddy-5870의 35번 명령(보통 5초)이 4번 중 1번 600초 timeout까지 멈췄다. cap과 관계가 있는지는 이 측정으로 알 수 없다.

### 8.4 1~7절에 주는 뜻

- 1~7절의 cap 없음 경합은 thread 단위로 나눈다는 가정의 값이다. Docker·Kubernetes처럼 container 단위로 나누면 넓은 sandbox에 낙관적이다. 3.3절에서 작은 노드는 cap 없음이 고정 cap보다 낫다고 했지만, 16코어 실측에서는 cap 4와 비겼다.
- 반대로 hand-off가 되찾을 몫은 simulator보다 크다. 손실은 넓은 빌드에 몰리고, 노드가 꽉 찬 시간은 18%뿐이다. 그 시간에 다른 노드에 빈 코어가 있으면 넓은 빌드나 그 이웃을 옮겨 되찾을 수 있다. 이번 측정은 노드 하나라 hand-off 자체는 재지 않았다.
- 1~7절을 고치려면 fleet.py에 container 단위 분배(sandbox마다 같은 weight)를 넣고, 이번 측정으로 맞춘 뒤 고정 clock profile로 다시 돌려야 한다.

## 9. 파일과 재현

| 파일 | 내용 |
|---|---|
| `async-summary.csv` | 조건별 seed 평균: 사용률, 잃은 CPU, tool 시간, rollout p50/p90/p99, 초당 이동 수 |
| `rl-summary.csv` | 동기 RL step 조건별 seed 평균(4절). step 시간 p50/p90/p99 |
| `solo.json` | 정책별 단독 비용(3.1절 표) |
| `async-grid.json`, `rl-dense-grid.json` | 조건 768개, 256개 |
| `measured-analysis.json` | 8절 실측, 조건별 지표(`real_cap.py analyze --runs runs-perf`) |
| `measured-sim.json` | 8절과 같은 구성의 simulator(`real_cap.py simulate --runs runs-perf`) |
| `measured-schedutil-analysis.json` | 11-04 schedutil 실행의 지표(8.1절, 부하 비교에는 쓰지 않음) |
| `measured-traces.json` | 실측에 쓴 trace 48개 |

원시 결과(조건별 JSON 한 줄)는 bud05 `experiments/contention/.local/cap/`에 있다(Git 제외). Docker 실측 원시 결과(명령별 events, cgroup 시계열, host perf·MBM)는 `experiments/contention/.local/real/runs-perf/`에 있다.

```bash
PY=/Serverless/agent-sandbox-cxl/venv/bin/python   # numba
C=experiments/contention
$PY $C/cap_sweep.py test
$PY $C/cap_sweep.py grid --out $C/.local/cap/async-grid.json --ramp-s 600 --duration-s 5400
numactl --cpunodebind=0 --membind=0 nice -n 19 $PY $C/cap_sweep.py run --grid $C/.local/cap/async-grid.json --jobs 24 --output $C/.local/cap/async.jsonl
$PY $C/cap_sweep.py report $C/.local/cap/async.jsonl --csv async-summary.csv
$PY $C/cap_sweep.py grid --out $C/.local/cap/rl-dense-grid.json --cores 192 --util 160 200 240 280 --rl --batch 16 --group 8 --ramp-s 1800 --duration-s 7200
numactl --cpunodebind=0 --membind=0 nice -n 19 $PY $C/cap_sweep.py run --grid $C/.local/cap/rl-dense-grid.json --jobs 24 --output $C/.local/cap/rl-dense.jsonl
$PY $C/cap_sweep.py report $C/.local/cap/rl-dense.jsonl --csv rl-summary.csv
```

bud05 NUMA 0, 24개 동시 실행으로 async는 약 13분, 동기 RL은 약 17분 걸린다.

8절 실측은 governor가 performance가 아니면 `run`이 실행을 거부한다. 6개 조건에 약 4시간 걸린다.

```bash
$PY $C/real_cap.py select
$PY $C/real_cap.py run --runs runs-perf
$PY $C/real_cap.py analyze --runs runs-perf
$PY $C/real_cap.py simulate --runs runs-perf
```
