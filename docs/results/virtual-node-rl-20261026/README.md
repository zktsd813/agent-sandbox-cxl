# Virtual node 필요성 실측: 공개 RL 학습 rollout, bud05·bud04 (2026-10-26~28)

설계: [virtual-node-experiment-design.md](../../virtual-node-experiment-design.md). 이 문서는 그 설계의 E0·E1·E2·E4(일부)를 실제로 돌린 기록이다.

## 0. 결론

1. **실제 RL 학습 엔진 속도로 LLM 시간을 넣으면, 물리 코어당 4~6개에서 CPU contention은 거의 없다.** sandbox 하나의 평균 CPU 수요가 0.022코어이고, 87%가 vCPU 하나의 5% 미만을 쓴다(DSec가 보고한 "약 90%가 요청 CPU의 5% 이하"와 같은 모습). 30코어 사용률은 11~15%, runqueue 대기는 실행 시간의 1.3~1.8%이고, 그중 tool call 중의 vCPU 대기는 0.2% 정도다.
2. **contention은 사용률이 높을 때만 커진다.** LLM 대기를 0.15배(turn당 약 2초)로 줄이고 코어당 6.4개를 두면 사용률 79%에서 runqueue 대기가 15%가 된다. 사용률 42~51%(LLM 0.25배)에서는 2.8~4.0%다.
3. **높은 사용률에서는 노드 사이에 되찾을 여유가 있다.** 잃은 CPU의 70%가 다른 노드에 빈 코어가 있던 순간에 생겼고, 두 노드를 합치면(POOL) 대기가 15.0% → 5.8%로 61% 줄었다. 같은 group 순서로 짝을 맞춘 반복에서는 10.1% → 6.0%로 41% 줄었다(7절). 낮은 사용률(42~51%)에서는 POOL로 줄어드는 몫이 12~14%뿐이다.
4. **contention 구간은 대부분 매우 짧다.** 사용률 50% 이하에서는 잃은 CPU의 거의 전부가 10ms보다 짧은 구간에 있다. 10ms hand-off도 못 잡고 커널 scheduler 수준의 공유만 효과가 있다. 사용률 79%에서는 잃은 CPU의 60%가 10ms보다 긴 구간에 있고, 이동 지연별 상한은 1ms 59%, 10ms 32%, 100ms 10%, 초 단위 migration 1~3%다.
5. **실제 시스템에서 흉내 낸 이동**(같은 조건, 사용률 74~83%): 10ms hand-off는 대기를 15.0% → 11.7%로, pre-copy migration(2GiB, 1.4초)은 9.0%로 줄였다. 다만 이 비교는 조건마다 group 순서가 달라 사용률이 다르므로(6절) 순위로 읽으면 안 된다. 짝을 맞춘 반복 실행은 7절.
6. **tool call이 느려지는 주원인은 runqueue 대기가 아니다.** 단독 실행 대비 늘어난 시간 중 runqueue 대기는 사용률 79%에서도 40%이고, 나머지는 같은 일을 하는 데 CPU 시간이 더 드는 것(주파수, cache, 메모리 대역폭: 60%)과 I/O·network다. 사용률 42%에서는 대기가 0.3%뿐이다. 같은 socket 안의 POOL은 runqueue 대기만 없앤다. 다른 host의 조용한 코어로 옮기면 이 부분도 줄어들 수 있지만 이번 실험은 그것을 재지 못했다(한 서버).
7. **192코어 노드(DSec 규모)로 외삽하면 여유가 거의 없다.** 실측에 맞춰 보정한 simulator에서 사용률 75%까지 잃은 CPU가 1% 이하이고, 84~96%에서야 2~25%가 되며 그때는 모든 노드가 함께 바빠 POOL도 16~50%만 줄인다.
8. **동기 step에서도 POOL은 step 시간을 바꾸지 못했다.** runqueue 대기는 27~44% 줄었지만 step 시간 차이는 1% 이내다. step 시간은 가장 긴 rollout이 정하고, step 대기의 30~45%는 시작 후 30초의 몰림이다(9절).
9. **메모리 용량 pooling의 이득은 한 자릿수 %다.** 같은 총 메모리에서 늘어나는 밀도는 CXL 비율 10/25/50%에서 중앙값 +2/+6/+9%다(10절).
10. **image 쪽 메모리 이득도 크지 않다.** guest 안 base image 사본은 sandbox 메모리의 약 19%다. 그러나 DAX로 없애면 pmem struct page 비용 때문에 오히려 19% 늘었다(11절). 반면 고정 image home + hand-off 배치는 대기를 유지하면서 host의 image page cache를 노드당 3~6배 줄인다. 노드 sandbox 메모리로는 11~16%다(simulator, 12절).

**virtual node 주장에 주는 의미**: 이 workload(공개 RL SWE rollout, 1 vCPU)와 현실적 LLM 속도에서는 "노드 사이 CPU 불균형을 ms 이동으로 되찾는다"는 동기가 약하다. 동기가 서는 조건은 (a) 작은 노드 또는 노드 분할(코어 15개 수준), (b) 높은 사용률(LLM이 빠르거나 밀도가 더 높음), (c) tool call 속도를 떨어뜨리는 비-runqueue 간섭을 host를 바꿔 피하는 경우다. (c)는 두 host로 따로 재야 한다.

## 1. 무엇을 쟀나

같은 코어 수(30)에서 노드를 **둘로 나눈 경우(ISO, 15코어 × 2)**와 **하나로 합친 경우(POOL, 30코어)**를 같은 sandbox 집합·같은 도착 순서로 돌렸다. POOL은 지연 0의 이상적 virtual node이고, 두 값의 차이가 virtual node가 되찾을 수 있는 몫의 상한이다. 그 위에 이동 메커니즘(ms hand-off, 초 단위 migration)을 실제 시스템에서 흉내 내어 그 몫을 얼마나 되찾는지 쟀다.

## 2. 환경

| 항목 | 값 |
|---|---|
| 서버 | bud05 (143.248.188.122), Xeon Gold 6548Y+, SMT 꺼짐, governor `schedutil`, turbo 켬 |
| 사용 범위 | **NUMA 0만** (CPU 0-31, node 0 DRAM). CPU 0-1은 harness와 sampler, 2-31(30코어)은 sandbox. NUMA 1은 쓰지 않았다(다른 사용자의 gem5가 실행 중) |
| ISO | `--nodes 2-16/17-31`: 15코어 노드 2개. vCPU와 VMM thread를 노드 코어에 고정 |
| POOL | 같은 두 노드, `--pool all`: vCPU는 30코어 어디서나 실행(Linux scheduler가 µs~ms 단위로 옮김), VMM thread와 메모리는 원래 노드 |
| sandbox | Firecracker v1.17.0(공식 release, SHA-256은 release `SHA256SUMS`와 일치), guest kernel 6.1.188(공식 CI), 1 vCPU, 2 GiB(lazy), virtio-balloon free page reporting, task image 읽기 전용 공유 + sandbox별 sparse overlay disk, host NAT |
| 메모리 | guest 메모리는 NUMA 0 DRAM. DAX/CXL은 쓰지 않았다 |
| 측정 | 노드별 `/proc/schedstat` 실행·runqueue 대기 1ms, vCPU thread 10ms, VMM RSS와 NUMA 0 여유 메모리 2초, serial marker로 명령 시작·끝 |

## 3. Workload: 공개 RL 학습 rollout

- 출처: [sweagent/iter2-rl-rollouts](https://huggingface.co/datasets/sweagent/iter2-rl-rollouts)(MIT). Qwen3.5-35B-A3B를 GRPO로 학습할 때 policy가 만든 rollout 전부(23,139개). bash만 쓰는 mini-swe-agent류 scaffold.
- **group 복원**: 같은 run·같은 task에서 sample index가 연속인 묶음이 한 GRPO group이다. dynamic sampling 때문에 대부분 8개다(완전한 8개 group 2,065개, 중복 sample id 0).
- **선택**: 8개짜리 group이 있는 task를 image 종류(swerebench / swerebenchv2 / scaleswe)별로 학습 rollout 비율에 맞춰 **무작위로** 뽑았다. image 크기는 순서에 영향을 주지 않고, 디스크 예산(압축 해제 추정 120 GiB)에 닿을 때 멈췄다. 결과: image 46개, group 56개, rollout 448개(swerebench 184, swerebenchv2 136, scaleswe 128). 해결률 42%, Submitted 401 / TimeExceeded 32 / LimitsExceeded 15.
- **변환**: `experiments/contention/rl_rollouts.py`. assistant의 `extra.actions` 명령, tool 결과의 return code, `model_patch`를 기존 replay 형식으로 옮겼다. 작업 경로는 task의 `workdir`(없으면 `/testbed`). Scale-SWE의 `pre_commands`(base commit checkout, git gc)는 image를 만들 때 미리 실행했다(데이터의 끝에 붙은 문자 그대로의 `\n`은 제거).
- **image**: Docker Hub에서 layer를 직접 받아(이 서버에서 dockerd pull은 1~2 MB/s, curl은 약 18 MB/s) whiteout을 적용해 펼친 뒤 ext4로 만들었다. 받은 manifest digest를 `image.json`에 기록했다. 수집 당시 digest는 데이터에 없다.

## 4. LLM 시간 모델

trace의 LLM 시간을 그대로 쓰지 않고 token 수로 모델링했다. rollout마다 전체 token 수(`all_tokens_length`)와 전체 글자 수의 비율로 turn별 context·출력 token을 추정했다.

- 같은 RL run의 2.49M turn에 최소제곱으로 맞춘 값: **turn 대기 = 6.16초 + 29.0ms × 출력 token** (context 계수는 0에 가까워 뺐다). R² 0.45.
- 선택한 448개에서: turn 대기 중앙값 기록 8.0초 / 모델 9.7초, p90 34.4 / 27.2초. turn 단위 상관 0.69, rollout 합계 상관 0.79, rollout 합계 비(모델/기록) 중앙값 1.015. 모델은 평균을 맞추지만 짧은 대기(6초 미만)가 없고 분산이 작다. [llm-model-check.json](llm-model-check.json)
- 이 값은 학습 중 경합이 있는 추론 엔진의 속도다(약 34 token/s/요청). 추론이 더 빠른 경우를 보기 위해 대기에 배율 s = 1, 0.25, 0.15를 곱했다(simulator는 0.10도).

## 5. 조건과 지표

**단독 실행(E0)**: 440개 rollout(46번째 image는 받지 못해 1개 group을 뺐다)을 LLM 대기 없이 하나씩 실행했다(30코어에 VM 24~30개를 각자 코어 하나에). 명령 47,475개 중 return code 불일치 1.0%, 제출까지 간 394개는 제출 내용이 모두 원본과 같았다. timeout 22개. guest RSS 중앙값 246 MiB, p90 445 MiB, 최대 2.0 GiB. rollout당 vCPU 시간 중앙값 18.9초(평균 35.9초), 기록된 LLM 대기 합 중앙값 1,599초.

**동시 실행**: 각 조건은 ramp 600초 + 측정 1,800초. sandbox 슬롯을 group(8개) 단위로 돌린다. group 하나가 끝나면(가장 늦은 멤버까지) 같은 슬롯들에 무작위 group을 새로 시작한다(group 단위 동기, 비동기 RL에 가까움).

| 축 | 수준 |
|---|---|
| 밀도 | 노드당 80 / 96 = 물리 코어당 5.33 / 6.4 |
| LLM 속도 배율 | 1.0(학습 엔진 실측), 0.25, 0.15 |
| 구성 | ISO(15 × 2), POOL(30) |
| 초기 배치 | pack(group 8개를 한 노드에), spread(group을 두 노드에 4 + 4) |
| 이동 흉내 | 없음, hand-off(10ms 정지 후 vCPU를 다른 노드 코어로), pre-copy(2GiB: 출발 노드에서 0.6초 CPU를 쓰고 1.4초 뒤 이동) |

이동 흉내의 정책은 기존 `sandbox.py`의 것을 그대로 썼다: 노드의 runnable vCPU가 코어 수를 넘는 정도를 10ms마다 보고 50ms 평균이 0.1코어 이상이면, 그 노드에서 최근 CPU를 가장 많이 쓴 VM 하나를 빈 코어가 있는 노드로 옮기고 1초 동안 그 노드에서 다시 옮기지 않는다. 메모리는 그대로 두고 vCPU·VMM thread의 affinity만 바꾼다.

| 지표 | 정의 |
|---|---|
| 사용률 | 30코어의 평균 실행 코어 수 ÷ 30 (모든 thread) |
| runqueue 대기 / 실행 | 노드 CPU의 `/proc/schedstat` 대기 합 ÷ 실행 합 (VMM·host thread 포함) |
| 잃은 CPU | 1ms마다 노드 대기가 0.1코어 이상인 구간의 대기 합 ÷ 실행 합 |
| 다른 노드에 빈 코어 | 잃은 CPU 중 그 1ms에 다른 노드에 빈 코어가 1개 이상 있던 몫 |
| 이동 지연별 상한 | 구간 시작에 이동을 결정하고 지연 L 뒤부터 그 구간의 남은 대기를 없앤다고 할 때 되찾는 몫 |
| 명령 느려짐 | 같은 명령의 단독 실행 시간 대비 (단독 0.5초 이상만) |

## 6. 실측 결과 (bud05, seed 1)

| 조건 | 사용률 | 대기/실행 | 잃은 CPU | 다른 노드 빈 코어 | 10ms 상한 | 명령 느려짐 p50 / p99 | rollout p90 |
|---|---|---|---|---|---|---|---|
| ×1.0, 5.33, ISO pack | 11% | 1.8% | 1.5% | 67% | 0.02% | 1.14 / 2.94 | 1.01 |
| ×1.0, 5.33, POOL | 15% | 1.3% | 1.1% | – | – | 1.13 / 3.84 | 1.01 |
| ×0.25, 5.33, ISO pack | 42% | 2.8% | 2.5% | 55% | 0.6% | 1.29 / 3.48 | 1.08 |
| ×0.25, 5.33, POOL | 44% | 2.5% | 2.2% | – | – | 1.29 / 2.77 | 1.06 |
| ×0.25, 6.4, ISO pack | 51% | 4.0% | 3.7% | 50% | 2.2% | 1.30 / 3.86 | 1.07 |
| ×0.25, 6.4, ISO spread | 57% | 3.8% | 3.5% | 51% | 2.8% | 1.29 / 2.98 | 1.08 |
| ×0.25, 6.4, POOL | 44% | 3.4% | 3.2% | – | – | 1.27 / 3.83 | 1.09 |
| ×0.15, 6.4, ISO pack | 79% | 15.0% | 14.9% | 70% | 32% | 1.30 / 3.14 | 1.11 |
| ×0.15, 6.4, ISO spread | 82% | 14.4% | 14.2% | 68% | 31% | 1.28 / 4.80 | 1.13 |
| ×0.15, 6.4, ISO pack + hand-off | 83% | 11.7% | 11.5% | 64% | – | 1.26 / 3.18 | 1.11 |
| ×0.15, 6.4, ISO pack + pre-copy | 74% | 9.0% | 8.9% | 66% | – | 1.28 / 3.45 | 1.11 |
| ×0.15, 6.4, POOL | 73% | 5.8% | 5.7% | – | – | 1.24 / 3.95 | 1.11 |

전체 열은 [measured/runs.csv](measured/runs.csv). 그림: [사용률별 대기](figures/1-wait-vs-util.png), [구간 길이](figures/2-episodes.png), [되찾는 몫](figures/3-recovery.png), [느려짐 분해](figures/4-breakdown.png).

**runqueue 대기가 어디서 생기나** (ISO pack):

| 조건 | 대기/실행 | 그중 vCPU 대기 | vCPU 대기 중 tool call 안 |
|---|---|---|---|
| ×1.0, 5.33 (11%) | 1.8% | 18% | 5% |
| ×0.25, 5.33 (42%) | 2.8% | 26% | 14% |
| ×0.25, 6.4 (51%) | 4.0% | 36% | 28% |
| ×0.15, 6.4 (79%) | 15.0% | 81% | 83% |

나머지는 VMM·host thread의 대기와 LLM 대기 중 guest가 잠깐 깨어날 때의 대기다. sandbox 부팅 중 대기는 1% 이하였다.

**명령이 단독보다 느려진 시간의 분해** (단독 0.5초 이상 명령):

| 조건 | 평균 느려짐 | runqueue 대기 | CPU 시간 증가 | 그 밖(I/O, network) |
|---|---|---|---|---|
| ×1.0, 5.33, ISO | 1.21 | 0% | 67% | 33% |
| ×0.25, 5.33, ISO | 1.31 | 0.3% | 72% | 28% |
| ×0.15, 6.4, ISO | 1.36 | 40% | 60% | 0% |
| ×0.15, 6.4, POOL | 1.30 | 4% | 75% | 21% |
| ×0.15, 6.4, ISO + hand-off | 1.34 | 25% | 66% | 9% |

"CPU 시간 증가"는 같은 명령에 쓴 vCPU 실행 시간이 단독보다 늘어난 양이다. 단독 실행도 VM 24~30개가 동시에 돌았으므로(서로 다른 코어), 완전히 조용한 기준이 아니다. 주파수는 이 matrix의 세 번째 조건부터 `turbostat`으로 기록했다(NUMA 0, 10초): [logs](#11-파일).

### 해석

- 사용률이 낮으면 POOL과 ISO의 대기 차이가 작다. 이때 대기는 tool call 밖(VMM thread, 깨어나는 idle guest)에 많아, 노드를 합쳐도 사라지지 않는다.
- 사용률 79%에서는 대기의 대부분이 tool call 중 vCPU 대기이고, 다른 노드의 빈 코어로 없앨 수 있는 몫이 크다. 이것이 virtual node가 노리는 상황이다.
- 그러나 rollout 시간(p90 1.10~1.13)과 group step 시간(p90 약 1.10)은 ISO와 POOL이 거의 같다. rollout 시간의 대부분이 LLM 대기라, tool call이 늦어져도 전체에 주는 영향이 작다. step 시간은 가장 늦은 멤버(LLM 대기가 가장 긴 rollout)가 정한다.
- spread(group을 두 노드에 나눔)는 pack과 대기가 비슷했다(14.4% 대 15.0%). 이 workload에서는 group 멤버들이 같은 시점에 CPU를 쓰는 정도(상관)가 노드 부하를 크게 바꾸지 않았다.

## 7. 짝을 맞춘 반복 (같은 seed의 ISO / POOL)

6절은 조건마다 seed가 달랐다(13절). 가장 중요한 비교인 ×0.15, 6.4, pack의 ISO / POOL을 두 조건에 **같은 seed(`p1`)**를 주어 한 번 더 돌렸다(`run_rl_matrix_paired.sh`). 같은 seed는 같은 group 순서를 뽑지만, group이 끝나는 시점이 조건마다 달라 이후 순서의 시각은 어긋난다.

| 실행 | 사용률 | 대기/실행 | 잃은 CPU | 다른 노드 빈 코어 | 10ms 상한 | 구간 길이 p50 / p90 (잃은 CPU 가중) | 명령 느려짐 p50 / p99 | step p90 |
|---|---|---|---|---|---|---|---|---|
| seed 1, ISO pack | 79% | 15.0% | 14.9% | 70% | 32% | 17 / 378 ms | 1.30 / 3.14 | 1.10 |
| seed 1, POOL | 73% | 5.8% | 5.7% | – | – | 7 / 264 ms | 1.24 / 3.95 | 1.11 |
| **p1, ISO pack** | 73% | 10.1% | 10.0% | 71% | 27% | 9 / 115 ms | 1.27 / 8.92 | 1.14 |
| **p1, POOL** | 76% | 6.0% | 5.9% | – | – | 8 / 172 ms | 1.25 / 4.69 | 1.11 |

- **POOL이 줄인 몫**: seed 1에서 61%, 짝 맞춘 p1에서 41%. p1에서는 POOL 쪽 사용률이 더 높은데도(76% 대 73%) 대기가 적다. 두 반복 모두 잃은 CPU의 약 70%가 다른 노드에 빈 코어가 있던 순간에 생겼다.
- **흔들림**: 같은 명목 조건의 ISO 대기가 10.1~15.0%로 반복 사이에 1.5배 차이가 난다. 사용률 75% 부근에서는 대기가 사용률에 매우 민감해(6절 그림 1), 몇 % 차이가 대기를 크게 바꾼다. 반복이 2개뿐이라 gap은 "40~60%" 범위로만 말할 수 있다.
- **p1 ISO의 명령 p99 8.9**: 느려진 시간 분해에서 runqueue 대기 14%, CPU 시간 증가 40%, 그 밖 45%였고, 가장 느린 1%는 90%가 "그 밖"(vCPU가 실행도 대기도 아닌 시간: I/O, network)이었다. runqueue가 아닌 원인의 꼬리다. POOL p1은 대기 5%, CPU 62%, 그 밖 33%.
- step 시간 p90은 여전히 1.11~1.14로 ISO와 POOL이 거의 같다.

같은 seed로 이동 흉내(hand-off, pre-copy)를 다시 돌리지는 못했다. 그래서 6절의 hand-off 11.7% / pre-copy 9.0%는 여전히 다른 seed끼리의 비교다.

## 8. 큰 노드로 외삽 (simulator)

`fleet.py`(numba 유체 모델 + 측정에 맞춘 scheduler 대기 보정)에 단독 실행 440개를 넣고, 실측 사용률에 맞추기 위해 명령 CPU를 1.4배로 키웠다(`cpu_inflation`; 같은 모양의 실측 사용률이 보정 전 simulator의 1.36~1.45배였다). 보정 후 실측과 같은 모양(2 × 15코어, ×0.15, 6.4, pack)에서 simulator는 사용률 0.73 / 잃은 CPU 16.8% / POOL 4.4%로, 실측 0.79 / 14.9% / 5.7%에 가깝다. hand-off 회수는 simulator 66% 대 실측 흉내 22%로 차이가 크다(이동 정책과 실제 affinity 변경 비용 차이). [fleet-sim-calibrated-summary.json](fleet-sim-calibrated-summary.json)

| 노드 | 밀도 | LLM | 사용률 | 잃은 CPU ISO | POOL | 줄어든 몫 | 10ms hand-off 회수 |
|---|---|---|---|---|---|---|---|
| 16 × 192코어 | 5.33 | ×1.0 | 11% | 0.02% | 0.02% | 4% | 0 |
| 16 × 192코어 | 5.33 | ×0.25 | 41% | 0.10% | 0.09% | 9% | 0 |
| 16 × 192코어 | 6.42 | ×0.15 | 75% | 0.98% | 0.77% | 21% | 8% |
| 16 × 192코어 | 5.33 | ×0.10 | 84% | 2.3% | 1.4% | 38% | 31% |
| 16 × 192코어 | 6.42 | ×0.10 | 96% | 24.6% | 20.8% | 16% | 16% |
| 2 × 15코어 | 6.4 | ×0.15 | 73% | 16.8% | 4.4% | 74% | 66% |

(pack 배치, seed 2개 평균. spread도 거의 같다.) 192코어 노드에서는 노드 안의 많은 코어가 burst를 흡수해, 노드 사이로 옮겨 되찾을 몫이 사용률 75% 이하에서 CPU의 0.2% 이하다.

## 9. 동기 step (job 전체가 함께 시작)

동기 RL처럼 매 step에 sandbox 192개(노드당 96개)를 모두 준비한 뒤 함께 시작하고, 가장 늦은 rollout이 끝나야 다음 step을 시작했다(`--sync-steps`). ISO / POOL은 같은 seed라 step마다 같은 group을 뽑는다. step 수는 ×0.15에서 5개, ×0.25에서 4개, ×1.0에서 2개다. 결과는 [measured/sync-steps.json](measured/sync-steps.json)(`sync_steps.py`)에 있다.

| LLM | 사용률 | 대기/실행 ISO → POOL | 줄어든 몫 | step 시간 ISO / POOL | step 느려짐 ISO / POOL | 대기 중 첫 30초의 몫 | 꼬리 |
|---|---|---|---|---|---|---|---|
| ×0.15 | 48% | 7.8% → 4.4% | 44% | 596 / 593초 | 1.06 / 1.03 | 29% / 40% | 23% |
| ×0.25 | 36% | 4.3% → 3.2% | 27% | 832 / 839초 | 1.02 / 1.01 | 45% / 47% | 18% |
| ×1.0 | 11% | 4.0% → 2.9% | 28% | 2,978 / 2,983초 | 1.00 / 1.01 | 45% / 46% | 17% |
| ×0.15, ISO spread | 47% | 5.6% | – | 631초 | 1.07 | 31% | 26% |

- **step 느려짐**은 step 시간을 "그 step에서 가장 느린 멤버의 단독 실행 시간 + 시작 jitter"로 나눈 값이다. 단독 실행이나 이번 실행에서 60초 명령 timeout이 난 멤버는 뺐다. timeout은 contention이 아닌데, 단독 기준을 부풀려 느려짐을 1보다 작게 만들었다.
- **꼬리**: rollout의 90%가 끝난 뒤 step이 더 걸린 시간의 비율이다.
- **POOL은 대기를 27~44% 줄였지만 step 시간은 그대로다(차이 1% 이내).** step 시간은 가장 긴 rollout의 길이(단독 실행 기준)가 정하고, contention은 그 위에 1~6%만 더한다.
- **step 대기의 30~45%가 시작 후 30초 안에 생긴다.** 모든 rollout의 부팅과 첫 명령이 겹치는 순간이다. 이 몰림은 노드 사이로 옮겨서가 아니라 시작 시각을 엇갈리게 해서 줄일 수 있는 몫이다.
- 사용률은 비동기(group 단위)보다 낮다. step 꼬리 동안 대부분의 sandbox가 끝나 코어가 논다.

## 10. 메모리 pooling (실측 메모리 시계열로 계산)

17개 실행(비동기 14개, 동기 3개)의 VM별 RSS 시계열(VM 평균 200~300 MiB)로 두 가지를 계산했다(`memory_pool.py`, [memory-pool.json](memory-pool.json)).

- **노드별 여유분(headroom) 대 공유 여유분**: 각 노드가 자기 peak에 맞춰 여유를 두는 대신 두 노드가 여유분을 공유하면, 필요한 여유 메모리가 실행별로 1~58%, 중앙값 21% 줄었다.
- **같은 총 메모리(iso-capacity)에서 늘어나는 밀도**: 노드마다 local L, 공유 CXL P = r·T를 두는 구성을 노드마다 자기 peak에 맞춘 구성과 비교했다. 실행 17개의 중앙값으로 r = 10%에서 +2%, 25%에서 +6%(RSS 중 CXL에 놓이는 몫 14%), 50%에서 +9%(38%)였다. 일부 실행은 음수다(r이 작으면 local이 부족해짐).
- 새 page를 처음부터 CXL에 두는 fallback 할당은 복사가 없다. 반면 local이 차면 내리는 demotion 방식은 local을 중앙값 수준(p50)으로 잡으면 시간당 수백 GiB를 옮겨야 했다.
- **결론**: 이 workload에서 메모리 용량 pooling의 밀도 이득은 한 자릿수 %다.

## 11. guest 메모리와 virtio-pmem DAX (bud04)

[guest-memory/README.md](guest-memory/README.md). 요약:

- VM 하나가 host에 쓰는 메모리(RssAnon 평균 228 MiB)는 guest kernel 쪽 약 107 MiB와 guest page cache 약 96 MiB(평균)가 대부분이다. guest 프로세스 메모리(AnonPages)는 거의 0이다.
- page cache 중 base image 사본은 VM당 약 48 MiB로, host 전체 sandbox 메모리의 약 19%다.
- base를 virtio-pmem DAX로 붙이면 이 사본은 사라진다. 하지만 장치 크기의 1/64인 struct page(평균 image 5.5 GiB → 88 MiB)가 생겨 sandbox 메모리가 오히려 19% 늘었다. 명령 지연은 조금 줄었다(p50 1.28 → 1.22).
- 옮겨 간 sandbox가 새로 읽을 base 비율 ρ는 중앙값 0.25다. 읽기의 2/3가 처음 2분에 몰린다.

## 12. 배치와 image page cache (simulator)

[placement-cache/README.md](placement-cache/README.md). 요약:

- 고정 image home + 10 ms hand-off(P5+H)는 CPU 대기를 power-of-k(P1) + hand-off 수준으로 유지하면서 노드당 image cache를 약 3~6배 줄인다(노드 16대, ×0.25에서 5.3 → 0.95 GiB). 노드 sandbox 메모리의 11~16%다.
- locality만 쓰면 대기가 12~108%로 무너진다. 현실 LLM 속도(×1.0)에서는 hand-off 없이 locality만으로 충분하다.
- bud04에서 실제 microVM으로 배치를 재는 실험은 bud04가 재부팅되어 중단됐다.

## 13. 한계

- **한 서버, 한 socket.** POOL은 같은 socket 안에서 코어를 합친 것이라, 다른 host로 옮길 때 줄어들 수 있는 주파수·cache·메모리 대역폭 간섭은 재지 못했다. bud04는 처음에 이 계정의 `/dev/kvm` 권한이 없어 VM이 시작되지 않았다(그 실행은 결과에서 뺐다). 권한을 받은 뒤 bud04에서는 11절의 메모리 실험만 돌렸다.
- **조건마다 seed가 달랐다.** driver가 조건 이름을 seed에 붙여, 같은 밀도·LLM 속도라도 조건마다 다른 group 순서를 뽑았다. 그래서 같은 명목 조건의 사용률이 0.73~0.83으로 흔들린다. 이동 흉내의 순위(hand-off 11.7% 대 pre-copy 9.0%)는 이 차이에 묻힌다. 7절의 짝 맞춘 ISO / POOL 반복으로 핵심 gap만 보완했다(41%, seed 1의 61%와 함께 40~60%). 이동 흉내는 짝을 맞추지 못했다.
- **조건마다 1회**(×0.15, 6.4, ISO / POOL만 2회). 신뢰구간이 없다.
- **LLM 시간 모델.** 평균은 맞지만 분산이 작고(최소 6.2초 × 배율), 추론 server의 동시 요청 수에 따른 변화(첫 turn 물결)가 없다. 배율 0.15·0.25는 "더 빠른 추론"을 가정한 것이다.
- **group 단위 동기.** 6~8절은 group 단위다. job 전체가 함께 시작하는 동기 step은 9절에서 따로 돌렸다(조건마다 1회, step 2~5개).
- **1 vCPU만.** 여러 vCPU sandbox에서는 burst가 넓어져 contention이 훨씬 크다는 이전 결과가 있다(RESULTS-20261021, 4 vCPU에서 40배).
- **단독 기준의 잡음.** 단독 실행도 24~30개를 동시에 돌렸고, 그중 일부는 image 빌드와 겹쳤다.
- **이동 흉내.** 메모리를 옮기지 않고 affinity만 바꾼다. hand-off의 attach 뒤 cache·TLB 손실, pre-copy의 실제 dirty page 재전송은 없다.

## 14. 재현

```bash
# 데이터: 받기, 색인, group 복원, 선택, 변환
python3 experiments/contention/rl_rollouts.py index
python3 experiments/contention/rl_rollouts.py groups --gap 1 --window-s 7200
python3 experiments/contention/rl_rollouts.py select --budget-gib 120
python3 experiments/contention/rl_rollouts.py convert
# image (NUMA 0에서)
numactl --cpunodebind=0 --membind=0 python3 experiments/contention/sandbox.py --candidates experiments/contention/candidates-rl.json build --jobs 8
# 단독 기준
numactl --cpunodebind=0 --membind=0 python3 experiments/contention/sandbox.py --candidates experiments/contention/candidates-rl.json solo --cores 2-31 --sibling-offset 0 --node 0 --harness-cpus 0-1 --llm-model tokens --gap-scale 0
# 동시 실행 matrix와 분석
experiments/contention/run_rl_matrix.sh <solo-run> s015-k96-iso-pack:96:none:pack:0.15:none s015-k96-pool:96:all:pack:0.15:none
SEED=p1 experiments/contention/run_rl_matrix_paired.sh <solo-run> s015-k96-iso-pack-p:96:none:pack:0.15:none s015-k96-pool-p:96:all:pack:0.15:none  # 7절, 모든 조건 같은 seed
python3 experiments/contention/report_rl.py <runs...> --out docs/results/virtual-node-rl-20261026/measured
python3 experiments/contention/slowdown_breakdown.py <run> --solo <solo-run>
python3 experiments/contention/plot_rl.py --table docs/results/virtual-node-rl-20261026/measured/runs.csv --runs-root experiments/contention/.local/runs --out docs/results/virtual-node-rl-20261026/figures
# 9절 동기 step (조건마다 같은 seed)
SEED=sync1 RAMP_S=0 SYNC_STEPS=5 DURATION_S=7200 experiments/contention/run_rl_matrix_paired.sh <solo-run> sync-s015-k96-iso-pack:96:none:pack:0.15:none sync-s015-k96-pool:96:all:pack:0.15:none
python3 experiments/contention/sync_steps.py --solo <solo-run> <runs...>
# 10절 메모리 pooling
python3 experiments/contention/memory_pool.py <runs...> --out docs/results/virtual-node-rl-20261026/memory-pool.json
# 11절은 guest-memory/README.md, 12절 simulator
python3 experiments/contention/image_blocks.py snapshot --out <dir>   # 실행 중 주기적으로
python3 experiments/contention/image_blocks.py merge <dir>/blocks-*.npz --out image-blocks-merged-v2.npz
python3 experiments/contention/fleet.py --solo <solo-run> --candidates experiments/contention/candidates-rl.json --grid docs/results/virtual-node-rl-20261026/placement-cache/grid-cache.json --blocks image-blocks-merged-v2.npz --jobs 28 --output cache-v2.json
python3 experiments/contention/cache_report.py cache-v2.json --out docs/results/virtual-node-rl-20261026/placement-cache
```

## 15. 파일

| 경로 | 내용 |
|---|---|
| `experiments/contention/rl_rollouts.py` | RL rollout 색인·group 복원·선택·변환 |
| `experiments/contention/sandbox.py` | 추가: image 공유(`image_key`), registry 직접 받기와 layer 펼치기, Scale-SWE setup, token LLM 대기(`--llm-model tokens`), 실제 group, `--placement`(spread·pack·home·least), balloon, RSS(anon·file)·NUMA 메모리 기록과 안전 정지, 동기 step(`--sync-steps`), guest 메모리 표본(`--mem-sample-s`), virtio-pmem DAX base(`--base-pmem`) |
| `experiments/contention/fleet.py` | 추가: 실제 group, pack 배치, locality·home 배치와 image cache 재생(`--blocks`) |
| `experiments/contention/run_rl_matrix.sh`, `run_rl_matrix_paired.sh` | 조건 matrix (paired: 모든 조건 같은 seed. `SYNC_STEPS`, `PILOT_ARGS`, `CACHE_RESET`, `IMAGE_CACHE_LOG`) |
| `experiments/contention/report_rl.py`, `slowdown_breakdown.py`, `plot_rl.py` | 집계, 느려짐 분해, 그림 |
| `experiments/contention/sync_steps.py` | 동기 step 시간, 느려짐, 시작 몰림 (9절) |
| `experiments/contention/memory_pool.py` | 여유분 공유와 iso-capacity 밀도 (10절) |
| `experiments/contention/mem_report.py` | guest 메모리 구성, host RSS·image cache, ρ (11절) |
| `experiments/contention/image_blocks.py`, `cache_report.py` | image block snapshot·병합·상주량·비우기, 배치 대 cache 표와 그림 (12절) |
| `experiments/contention/candidates-rl.json` | 선택한 448개 rollout(실행은 440개) |
| `measured/` | 실행별 지표 표, `sync-steps.json`(9절) |
| `memory-pool.json`, `guest-memory/`, `placement-cache/` | 10~12절 결과 |
| `fleet-sim*.json` | simulator 결과(보정 전, 보정 후, 요약) |
| `llm-model-check.json` | LLM 시간 모델 맞춤과 검증 |
| 원시 데이터 (Git 제외) | bud05 `/Serverless/agent-sandbox-cxl/` 아래: `rl-iter2/`(데이터·변환 trace), `contention-local/runs/`(실행), `logs/`(build, matrix, turbostat), `image-blocks/`(block snapshot). bud04 `~/agent-sandbox-cxl-run/experiments/contention/.local/runs/`(11절 실행), `/Serverless/ijkim-vnode-sim/out/`(simulator 출력) |
