# sandbox 크기 정책 sweep: hard cap, cap 없음, image별 크기, hand-off (2026-11-04)

simulator 결과다. 실측이 아니다. 코드는 [`cap_sweep.py`](../../../experiments/contention/cap_sweep.py)이고, node 모델은 [`fleet.py`](../../../experiments/contention/fleet.py)를 그대로 쓴다.

## 0. 결론

1. **hard cap은 그 자체로 느리다.** 264개 다언어 trace(Go, Rust, Java, JS, PHP, Ruby, C)를 cap 1·2·4·8 vCPU로 줄이면 contention이 없어도 tool 시간이 ×3.53, ×2.07, ×1.43, ×1.15가 된다. LLM 대기를 포함한 rollout 시간 p99는 ×4.48, ×2.62, ×1.73, ×1.29다. cap 1에서는 60초를 새로 넘는 명령이 241개 생긴다.
2. **cap을 풀면 노드 사이 contention이 생긴다.** 사용률 목표 90%에서 cap 없음의 잃은 CPU는 192코어 노드 14.6%, 96코어 28.6%, 48코어 50.8%다. 같은 조건에서 cap 1은 0.04%, 0.32%, 1.07%다.
3. **고정된 크기 중에 둘 다 피하는 값은 없다.** tool 시간(cap 때문에 늘어난 몫 × contention 대기)의 최솟값은 목표 90%에서 다음과 같다.
   - 192코어: cap 16의 ×1.14
   - 96코어: cap 16의 ×1.23
   - 48코어: cap 8의 ×1.42

   image별 크기(image90)도 cap 16과 거의 같다.
4. **hand-off는 두 비용을 함께 줄인다.** cap 없음 + 10ms hand-off의 tool 시간은 192, 96, 48코어에서 ×1.03, ×1.07, ×1.14다. rollout p99는 1.02, 1.04, 1.07이고, cap 없음만 쓴 경우는 1.11, 1.21, 1.34다. pre-copy migration은 잃은 CPU를 14.6% → 13.4%로 거의 줄이지 못한다.
5. **이득은 노드가 작을수록, 사용률이 높을수록 크다.** 192코어·목표 80%에서는 cap 없음만으로도 tool 시간이 ×1.06이라 hand-off의 몫(→ ×1.00)이 작다. GPU 노드에서 빌려 쓰는 남는 코어처럼 작은 pool이 hand-off가 필요한 조건에 가깝다.
6. **동기 RL step에서도 같은 모양이다.** 192코어 노드, 실제 사용률 약 76%에서 step 시간 p99는 다음과 같다(4절).
   - cap 4: 1.76
   - cap 16: 1.12
   - cap 없음: 1.09
   - cap 없음 + hand-off: 1.03
   - 이상적 공유: 1.02

   사용률 약 89%에서는 cluster 전체가 포화되어 이상적 공유도 1.13이다. 이때 hand-off는 1.15로, cap 없음의 1.23보다 낫다.

## 1. 질문

운영자는 sandbox 크기를 job마다 상수 하나로 정한다. 보통 1~2 vCPU이고, 넓은 task가 생기면 손으로 고친다(조사 결과는 아래 6절).

- 작은 hard cap은 넓은 task를 느리게 한다.
- cap이 없거나 request만 거는 방식은 넓은 burst 때문에 노드 사이 평탄화를 깨뜨린다.

이 두 비용의 크기를 재고, hand-off가 어느 고정 크기보다 나은지를 본다.

## 2. 방법

| 항목 | 내용 |
|---|---|
| 수요 | 10-14 Docker DRAM 재생(`harness/trace-replay/results/numa-immediate-20261014/node0`). trace 264개, cpuset 30코어, CPU quota 없음, cgroup CPU 20ms 간격. 명령 구간마다 10ms slot의 코어 수요로 바꿨다 |
| cap 적용 | slot 수요 d가 cap V보다 크면 d/V개의 V짜리 slot으로 늘인다. 일의 양은 같고 명령이 길어진다. 넘친 일을 다음 slot으로 미루는 순차 모형이라 100ms 단위 하한보다 조금 크다(cap 1에서 ×3.53 대 ×3.1) |
| LLM 대기 | trace에 기록된 응답 간격(`replay.load_trace`의 `gap`). Gemini 3.5 Flash eval trace라 공개 RL rollout보다 짧다 |
| 크기 정책 | `cap1`~`cap16`, `cap30`(cap 없음: 재생 때 보인 30코어 전부), `image50`·`image90`(CPU 시간의 50%·90%가 그 폭 이하에서 돈 2의 거듭제곱, 최대 16) |
| cluster | 노드 16개 × 48/96/192코어. 새 sandbox는 power-of-2로 덜 바쁜 노드에 놓는다(DSec). 노드당 sandbox 수는 목표 사용률 60·70·80·90% ÷ 정책별 평균 수요 |
| 메커니즘 | 없음, 10ms hand-off(출발 노드 CPU 2ms), pre-copy migration(100Gb/s, trace별 guest 메모리), 이상적 공유(모든 노드가 코어 하나의 pool) |
| 시간 | ramp 600초 + 측정 5,400초, seed 2개 평균. 명령은 60초에서 자르지 않는다(자르면 cap의 비용이 숨는다) |
| 지표 | 잃은 CPU = runqueue 대기 / 실행. tool 시간 = 단독 실행에서 cap으로 늘어난 tool 시간 × (1 + 잃은 CPU). rollout = 측정 구간 안에서 끝난 trace의 (LLM 대기 포함 시간) ÷ cap 없는 단독 시간 |

## 3. 결과

### 3.1 cap 자체의 비용 (contention 없음)

| 정책 | vCPU 분포 | tool 시간 | rollout p50 | p90 | p99 | 60초를 새로 넘는 명령 | sandbox 평균 수요(코어) |
|---|---|---|---|---|---|---|---|
| cap1 | 1 | ×3.53 | 1.24 | 2.61 | 4.48 | 241 | 0.464 |
| cap2 | 2 | ×2.07 | 1.08 | 1.73 | 2.62 | 130 | 0.601 |
| cap4 | 4 | ×1.43 | 1.02 | 1.30 | 1.73 | 43 | 0.690 |
| cap8 | 8 | ×1.15 | 1.00 | 1.11 | 1.29 | 13 | 0.739 |
| cap16 | 16 | ×1.03 | 1.00 | 1.03 | 1.08 | 5 | 0.762 |
| cap 없음 | 30 | ×1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.768 |
| image50 | 1:86, 2:30, 4:25, 8:55, 16:68 | ×1.15 | 1.02 | 1.10 | 1.16 | 17 | 0.739 |
| image90 | 2:91, 4:16, 8:18, 16:139 | ×1.03 | 1.00 | 1.03 | 1.08 | 5 | 0.761 |

image90은 264개 중 139개에 16 vCPU를 준다. 넓게 쓰는 repo가 많아서 image별 크기도 결국 넓은 sandbox가 섞인 fleet이 된다.

### 3.2 노드 사이 contention: 잃은 CPU(%), 이동 없음, 목표 80% / 90%

| 노드 코어 | cap1 | cap2 | cap4 | cap8 | cap16 | cap 없음 | image90 |
|---|---|---|---|---|---|---|---|
| 48 | 0.1 / 1.1 | 1.0 / 4.9 | 4.9 / 12.8 | 12.0 / 23.3 | 21.2 / 38.6 | 33.7 / 50.8 | 21.4 / 38.1 |
| 96 | 0.0 / 0.3 | 0.2 / 2.0 | 1.2 / 5.7 | 4.5 / 12.0 | 9.2 / 19.0 | 14.5 / 28.6 | 9.0 / 20.3 |
| 192 | 0.0 / 0.0 | 0.0 / 0.5 | 0.2 / 2.1 | 1.2 / 6.1 | 3.0 / 10.1 | 5.6 / 14.6 | 2.9 / 9.9 |

cap 없음에서 메커니즘별로 보면 다음과 같다.

| 노드 코어 | 이동 없음 | 10ms hand-off | pre-copy | 이상적 공유 |
|---|---|---|---|---|
| 48 | 33.7 / 50.8 | 7.0 / 13.8 | 32.6 / 47.9 | 0.35 / 2.8 |
| 96 | 14.5 / 28.6 | 2.0 / 6.9 | 13.5 / 27.1 | 0.02 / 0.89 |
| 192 | 5.6 / 14.6 | 0.42 / 2.8 | 5.0 / 13.4 | 0.00 / 0.16 |

- 대기가 길어지면 같은 sandbox 수에서도 실제 사용률이 목표보다 낮아진다. cap 없음·목표 90%의 실제 사용률은 48·96·192코어에서 84%, 87%, 88%다.
- hand-off는 cluster 전체에서 초당 54~62번 옮긴다. 노드당 초당 3.4~3.9번이다.

### 3.3 정책 비교: tool 시간, 목표 90%

| 노드 코어 | cap1 | cap2 | cap4 | cap8 | cap16 | cap 없음 | image50 | image90 | cap 없음 + hand-off | cap16 + hand-off | cap 없음 + pre-copy |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 48 | ×3.57 | ×2.17 | ×1.62 | ×1.42 | ×1.43 | ×1.51 | ×1.52 | ×1.43 | **×1.14** | ×1.15 | ×1.48 |
| 96 | ×3.54 | ×2.11 | ×1.51 | ×1.29 | ×1.23 | ×1.29 | ×1.35 | ×1.24 | **×1.07** | ×1.08 | ×1.27 |
| 192 | ×3.53 | ×2.08 | ×1.46 | ×1.22 | ×1.14 | ×1.15 | ×1.26 | ×1.14 | **×1.03** | ×1.05 | ×1.13 |

목표 80%에서 가장 좋은 고정 크기와 hand-off를 비교하면 다음과 같다.
- 48코어: cap16 ×1.25 대 ×1.07
- 96코어: cap16 ×1.13 대 ×1.02
- 192코어: cap 없음 ×1.06 대 ×1.00

### 3.4 rollout 시간 p90 / p99, 목표 90%

| 노드 코어 | cap4 | cap8 | cap16 | cap 없음 | cap 없음 + hand-off |
|---|---|---|---|---|---|
| 48 | 1.40 / 1.91 | 1.21 / 1.50 | 1.16 / 1.37 | 1.15 / 1.34 | 1.04 / 1.07 |
| 96 | 1.34 / 1.82 | 1.16 / 1.39 | 1.09 / 1.22 | 1.09 / 1.21 | 1.02 / 1.04 |
| 192 | 1.32 / 1.77 | 1.14 / 1.34 | 1.06 / 1.15 | 1.05 / 1.11 | 1.01 / 1.02 |

rollout 시간에는 LLM 대기가 들어 있어서 tool 시간보다 차이가 작다. 동기 RL의 step 시간은 가장 느린 rollout이 정하므로 p99가 더 중요하다(4절).

## 4. 동기 RL step

설정은 다음과 같다.
- 노드 16개 × 192코어, job마다 batch 16 × group 8.
- group 8개는 같은 trace를 복제했다. burst 상관이 가장 큰 경우다.
- step은 128개 rollout 중 가장 늦은 것이 끝나면 끝나고, train 30초 뒤에 다음 step을 시작한다.
- ramp 1,800초 + 측정 7,200초, seed 2개.

step 시간은 cap 없는 단독 실행으로 계산한 이상적 step 시간에 대한 비율이다.

동기 step에서는 sandbox가 가장 늦은 rollout과 학습을 기다리는 동안 논다. 그래서 async와 같은 밀도로 두면 실제 사용률이 23~34%에 그친다(1차 실행, `rl.jsonl`). 그때는 contention이 없어 step 시간이 cap으로 늘어난 몫만 보인다(cap 1 ×3.0, cap 4 ×1.37, cap 8 ×1.12). 아래는 밀도를 async 목표의 160~280%로 올린 2차 실행이다.

step 시간 p90 / p99:

| 정책 | 실제 사용률 약 76% | 실제 사용률 약 89% |
|---|---|---|
| cap1 | 3.25 / 4.46 | 3.25 / 4.46 (사용률 79%) |
| cap2 | 1.99 / 2.64 | 2.02 / 2.69 (86%) |
| cap4 | 1.38 / 1.76 | 1.45 / 1.85 |
| cap8 | 1.14 / 1.32 | 1.21 / 1.45 |
| cap16 | 1.05 / 1.12 | 1.13 / 1.28 |
| image90 | 1.05 / 1.13 | 1.13 / 1.24 |
| cap 없음 | 1.05 / 1.09 | 1.12 / 1.23 |
| cap 없음 + pre-copy | 1.03 / 1.05 | 1.11 / 1.21 |
| **cap 없음 + 10ms hand-off** | **1.01 / 1.03** | **1.07 / 1.15** |
| cap 없음 + 이상적 공유 | 1.01 / 1.02 | 1.05 / 1.13 |

- **사용률 약 76%**에서 hand-off는 이상적 공유와 거의 같다. 가장 좋은 고정 크기(cap 없음, cap 16)는 p99가 1.09~1.12다.
- **사용률 약 89%**에서는 cluster 전체가 포화되기 시작한다. 이상적 공유도 잃은 CPU가 15.6%다. hand-off(17.3%)는 그 근처까지 내려가지만, 그 아래는 이동으로 되찾을 수 없다. 측정 구간에서 끝난 step 수는 다음과 같다. cap 없음 688, hand-off 706(+2.6%), 이상적 공유 708이다.
- **cap의 비용은 사용률과 상관없이 step 시간에 그대로 남는다.** cap 4는 어느 사용률에서도 p90이 1.38 이상이다.
- **목표 280%(실제 96~99%)**에서는 모든 정책이 포화되어 비교 의미가 없다(`rl-summary.csv`).

## 5. 한계

- **유체 모델이다.** 실측 scheduler 대기(`kernel_wait`)를 넣지 않았다. [fleet 결과](../../../experiments/contention/RESULTS-fleet.md) 7절에서 이 모델은 contention을 작게 봤다.
- **cap 없음의 폭이 30코어에서 잘렸다.** 192코어 노드에서 cap이 없으면 tool은 더 많은 worker를 띄운다.
- **cap을 걸면 tool이 `nproc`에 맞춰 worker를 줄인다.** 병렬 overhead가 줄어서 실제 cap의 비용은 이 표보다 조금 작을 수 있다.
- **hand-off 모델이 넓은 sandbox에 낙관적이다.** 목적지 조건이 "빈 코어 1개 이상"이라 16~30폭 sandbox도 그런 노드로 옮긴다. 10ms 정지와 2ms CPU도 vCPU 수와 상관없이 같게 뒀다. 넓은 VM의 hand-off 비용과 목적지 조건은 따로 재야 한다.
- **workload가 다르다.** 264개 trace는 다언어 eval trace다. 우리가 재생한 공개 RL rollout 448개(SWE-rebench, ScaleSWE)는 샘플이 모두 Python이었고 폭이 좁다. Python 위주 RL에서는 cap의 비용이 훨씬 작을 것이다.
- **rollout 지표는 측정 구간 안에서 끝난 trace만 센다.** 그래서 짧은 trace 쪽으로 조금 치우친다(측정 5,400초, cap 1 단독 trace 최대 2,123초).

## 6. 운영자는 vCPU 수를 어떻게 정하나 (조사, 2026-11-04)

- **framework 상수**: R2E-Gym/DeepSWE request 1 CPU, SkyRL 1 CPU, ROCK 2 CPU, ROLL SWE 4 CPU, SandboxFusion CFS quota 1 CPU
- **provider 기본값**: E2B 2, Daytona 1, Runloop 1, Modal 0.125코어 request
- **task별 선언**: Terminal-Bench 2는 89개 중 84개가 1, 3개가 2, 2개가 4
- **실패 뒤 키우기**: OpenHands는 SWE-Gym 2,119개 중 33개만 2배로 정해 두고, 실행이 실패하면 2배씩 8배까지 키운다

RL에서 16 vCPU를 쓰는 예는 찾지 못했다. 강제 방식은 둘로 나뉜다. Docker 계열은 hard cap(`--cpus`, CFS quota)이고, Kubernetes와 managed provider는 burst가 가능한 request다. request 방식에서는 sandbox가 보이는 코어만큼 퍼진다. 예를 들어 SkyRL에서는 Jest가 64 worker를 띄워 `--maxWorkers=2`로 막아야 했다.

출처:
- [E2B](https://docs.e2b.dev/sandbox/compute), [Modal](https://modal.com/docs/guide/resources), [Daytona](https://www.daytona.io/docs/en/sandboxes.md)
- [R2E-Gym](https://github.com/R2E-Gym/R2E-Gym/blob/main/src/r2egym/agenthub/runtime/docker.py), [SkyRL](https://github.com/NovaSky-AI/SkyRL/blob/main/examples/train_integrations/harbor/harbor_trial_config/default.yaml), [ROCK](https://github.com/alibaba/ROCK/blob/main/rock/deployments/docker.py), [SandboxFusion](https://github.com/bytedance/SandboxFusion/blob/main/sandbox/runners/base.py)
- [OpenHands resource map](https://github.com/All-Hands-AI/OpenHands/blob/0.40.0/evaluation/benchmarks/swe_bench/resource/mapping.py), [Terminal-Bench 2](https://github.com/laude-institute/terminal-bench-2), [DSec](https://arxiv.org/html/2609.22978v1)

## 7. 파일과 재현

| 파일 | 내용 |
|---|---|
| `async-summary.csv` | 조건별 seed 평균: 사용률, 잃은 CPU, tool 시간, rollout p50/p90/p99, 초당 이동 수 |
| `solo.json` | 정책별 단독 비용(3.1절 표) |
| `async-grid.json` | 조건 768개 |
| `rl-summary.csv`, `rl-dense-grid.json` | 동기 RL step 조건 256개(4절). step 시간 p50/p90/p99 열이 채워져 있다 |

원시 결과(조건별 JSON 한 줄)는 bud05 `experiments/contention/.local/cap/`에 있다(Git 제외).

```bash
cd experiments/contention
PY=/Serverless/agent-sandbox-cxl/venv/bin/python   # numba
$PY cap_sweep.py test
$PY cap_sweep.py grid --out .local/cap/async-grid.json --ramp-s 600 --duration-s 5400
numactl --cpunodebind=0 --membind=0 nice -n 19 $PY cap_sweep.py run --grid .local/cap/async-grid.json --jobs 24 --output .local/cap/async.jsonl
$PY cap_sweep.py report .local/cap/async.jsonl --csv async-summary.csv
```

bud05 NUMA 0, 24개 동시 실행으로 약 15분 걸린다. 동기 RL은 아래처럼 돌렸고, 16개 동시 실행으로 약 24분 걸렸다.

```bash
$PY cap_sweep.py grid --out .local/cap/rl-dense-grid.json --cores 192 --util 160 200 240 280 --rl --batch 16 --group 8 --ramp-s 1800 --duration-s 7200
numactl --cpunodebind=0 --membind=0 nice -n 19 $PY cap_sweep.py run --grid .local/cap/rl-dense-grid.json --jobs 16 --output .local/cap/rl-dense.jsonl
$PY cap_sweep.py report .local/cap/rl-dense.jsonl --csv rl-summary.csv
```
