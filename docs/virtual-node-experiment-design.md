# Virtual node 필요성 실험 설계 (bud04·bud05)

작성일: 2026-10-26. 초안이며 실행 전에 판정 기준(8절)을 고정한다.

## 0. 무엇을 보이려는가

Virtual node가 필요하다는 주장은 다섯 단계로 이뤄진다. 이 실험은 1~3단계를 다룬다. 4·5단계(hand-off 메커니즘, CXL tax)는 DAX와 구현이 필요하므로 따로 한다.

| # | 주장 | 이 실험에서 |
|---|---|---|
| 1 | 현실적인 밀도(물리 코어당 sandbox 4~6)에서 노드별 CPU contention이 생기고, 손해가 꼬리 tool call과 step time에 떨어진다 | E1 |
| 2 | 한 노드가 contention일 때 다른 노드에는 빈 코어가 있다. 즉 격리된 노드 N개와 같은 코어 수의 통합 노드 1개 사이에 격차가 있다 | **E2, E3 (핵심)** |
| 3 | 배치, 노드 안 QoS, 경계 migration·live migration은 그 격차를 대부분 메우지 못한다 | E4 |
| 4 | ms 단위 hand-off는 메운다 | 범위 밖 (Phase D) |
| 5 | CPU를 많이 쓰는 sandbox가 곧 tax가 큰 sandbox는 아니라서, 일부만 CXL에 두면 된다 | 범위 밖 (A4) |

격차(2)는 virtual node가 얻을 수 있는 이득의 상한이다. 이 값이 작으면 3~5단계는 의미가 없다.

**DAX는 쓰지 않는다.** contention과 격차는 CPU에서 생기고 메모리 위치와 무관하다. guest 메모리는 각 서버의 NUMA node 0 DRAM에 둔다.

## 1. 비교 구성

같은 sandbox 집합, 같은 도착 schedule, 같은 seed로 아래 구성을 비교한다.

| 구성 | 설명 | 측정 방법 |
|---|---|---|
| **ISO** | 노드마다 자기 코어만 쓴다. 현재 production | 실측 |
| **POOL** | 모든 노드의 코어를 하나로 합친다. 지연 0인 이상적 virtual node (상한) | 같은 서버 안에서는 실측(cpuset 합치기). 서버 사이는 simulator |
| 배치 정책 | 3.5절의 초기 배치 시나리오. 이동은 하지 않는다 | 실측 |
| 노드 안 QoS | 꼬리 rollout(critical)에 `cpu.weight` 가중, 나머지 SCHED_IDLE | 실측 |
| 이동 메커니즘 | 경계 이동(oracle), live migration 1.42초, snapshot restore, hand-off 10ms, in-place 0.5ms | simulator (`fleet.py`)에 E1~E3 실측 수요 시계열 입력 |

## 2. 실험 단계

| 단계 | 서버 | 내용 | 산출 |
|---|---|---|---|
| **E0 준비** | 둘 다 | Firecracker와 guest kernel 빌드, rootfs를 `/Serverless`에 생성, 1 vCPU 단독 재생으로 종료 코드 일치, CPU, guest RSS 측정 | 사용할 trace 목록, VM당 실제 메모리, 단독 기준값 |
| **E1 한 노드 contention** | bud04 | node 0 30코어 한 노드, 밀도 4/5/6, LLM 모델 수준별 | 잃은 CPU, 단독 대비 tool call 느려짐, rollout p99 |
| **E2 격리 vs 통합 (서버 안)** | 둘 다 (seed 나눔) | 30코어를 2×15 또는 3×10 노드로 나눈 ISO 대 30코어 POOL. 노드 크기 효과도 함께 봄 | 격차, 노드 크기별 격차 |
| **E3 서버 사이 불균형** | bud04 + bud05 | 두 서버의 node 0을 30코어 노드 2개로 쓰는 ISO. 동시 여유(한쪽이 contention일 때 다른 쪽 빈 코어)를 시간축으로 맞춰 계산 | 실제 노드 간 동시 여유, simulator 보정값 |
| **E4 대안 비교** | 실측 + simulator | 배치·QoS는 E2/E3 구성에서 실측. 이동 메커니즘은 실측 수요로 simulator | Fig 4: 메커니즘 지연 대 회수율, oracle 수평선 |

E2가 POOL을 실측하는 유일한 경로다. 서버 사이 POOL은 물리적으로 만들 수 없으므로 E2에서 맞춘 simulator로 E3를 외삽한다.

**결정 (2026-10-26)**: bud05 한 대로 먼저 측정하고, 아래 세 경우가 필요해지면 bud04를 더한다.

**bud05 한 대로 충분한 범위**: E0, E1, E2, E4는 bud05만으로 된다. 노드가 서로 영향을 주지 않는 조건(비동기 pool, group이 한 노드 안에 있는 S-pack·S-job)에서는 E3도 30코어 노드 설정을 bud05에서 차례로 돌리고 시간축을 겹쳐서 얻을 수 있다. bud04가 꼭 필요한 경우는 세 가지다.
1. 동기 GRPO에서 group이 두 노드에 걸쳐(S-spread, S-split) step 끝이 두 노드를 함께 묶는 경우.
2. seed를 병렬로 돌려 시간을 줄일 때.
3. 이후 hand-off·DAX 실험(Phase D).

bud05는 node 1에서 다른 사용자의 작업이 돌고 있다. node 0과 코어·메모리 채널이 겹치지 않지만 실행마다 기록하고, 잡음이 크면 비어 있는 bud04로 옮긴다.

## 3. Workload

### 3.1 trace

- **본 실험 (E0부터)**: 공개 RL 학습 rollout `sweagent/iter2-rl-rollouts` (10절). 같은 harness 형식으로 바꿔 넣는다.
- **264 trace(Gemini 3.5 Flash)는 보조로만 쓴다.** token 수와 기록된 응답 시각이 둘 다 있어 LLM 시간 모델의 형태를 검증하는 데 쓰고(3.3절), microVM 경로의 smoke test에 썼다.
- 264 trace는 RL trace가 아니고, 같은 task의 group(G개) 상관이 없다. group은 같은 trace를 jitter를 두고 복제해서 흉내 낸다. 이것은 상관의 상한(자기 자신 복제)이라는 점을 결과에 적는다.
- 서버당 동시 120~180개라 trace를 중복해서 쓴다. 기존 `trace_count.py` 결과로는 같은 사용률에서 trace 15~120개의 contention이 같았다.

### 3.2 rollout 진행 방식

| 모드 | 설명 |
|---|---|
| 비동기 pool (기본) | 동시 sandbox 수를 일정하게 유지한다. 끝나면 새 rollout을 시작한다. ramp 구간은 분석에서 뺀다 |
| 동기 GRPO step | task B개 × G개를 t=0에 함께 시작하고, 마지막 rollout이 끝나면 step이 끝난다. step time을 잰다 |

### 3.3 LLM 시간 모델

turn마다 다음 명령 전에 기다릴 시간을 token 수로 정한다.

```text
T_turn = t0 + α · context_tokens + TPOT · output_tokens
output_tokens = completion + reasoning
```

- 264 trace로 맞춘 값(Gemini API): t0 0.57초, α 0.045ms/token, TPOT 4.65ms(215 token/s). R² 0.61, 상대 오차 중앙값 27%.
- RL 추론 서버 수준은 TPOT으로 바꾼다: 20 / 40 / 80ms. `experiments/rollout-trace/estimate_time.py`의 Qwen3-32B·H100 추정(turn 7~28초)과 같은 범위다.
- v1은 미리 계산한 정적 대기다. `guest-init.sh`가 읽는 `cmd/NNN.gap`에 모델 값을 쓰면 되므로 guest 쪽 변경이 없다.
- v2는 그 순간 진행 중인 LLM 요청 수에 따라 TPOT이 늘어나는 동적 모델이다. 동기 GRPO의 "첫 turn 물결"을 만들려면 필요하다. host 쪽에서 gap을 내려 주는 경로가 있어야 한다.

### 3.4 예상 사용률 (설계 근거)

1 vCPU sandbox의 평균 수요를 대략 추정하면 다음과 같다. Docker 실측 CPU 시간을 1 vCPU 실측 비율(0.25 대 0.575)로 줄인 값이라 대략적인 수치다. E0 단독 실행으로 다시 계산한다.

| LLM 모델 | turn 평균 | sandbox 평균 수요 | 코어당 4 / 5 / 6일 때 사용률 |
|---|---|---|---|
| Gemini 맞춤 | 3.6초 | 약 0.25코어 | 100 / 125 / 150% (포화) |
| TPOT 20ms | 10초 | 약 0.11코어 | 45 / 57 / 68% |
| TPOT 40ms | 18초 | 약 0.07코어 | 28 / 35 / 42% |
| TPOT 80ms | 34초 | 약 0.04코어 | 16 / 20 / 24% |

- 코어당 4~6에 현실적인 RL 추론 속도를 넣으면 사용률이 낮아 contention이 작을 수 있다. 그래서 밀도와 TPOT을 둘 다 축으로 두고 사용률로 함께 보고한다.
- DSec는 sandbox의 약 90%가 요청 CPU의 5% 이하를 쓴다고 했다. 우리 trace는 같은 기준으로 32~55%(TPOT 20~80ms, Docker 기준)라 DSec보다 바쁘다.
- 실제 sandbox는 vCPU를 여러 개 받는 경우가 많다. burst가 여러 코어로 넓어지면 같은 밀도에서 contention이 크게 늘었다(작은 노드 실측에서 4 vCPU가 1 vCPU의 40배). 2 vCPU를 보조 축으로 둔다.

### 3.5 초기 배치 시나리오

배치는 생성 때 한 번 정하고 옮기지 않는다는 것이 production의 공통점이다([sandbox-placement-survey](https://github.com/zktsd813/agent-sandbox-cxl/blob/contention-fleet-sim/docs/sandbox-placement-survey.md)는 `contention-fleet-sim` branch에 있다). 격차는 노드에만 있는 상관이 있을 때 커지므로, 상관을 만드는 실제 배치 방식을 축으로 둔다.

| 시나리오 | 배치 | 실제 예 | 노드에 생기는 상관 |
|---|---|---|---|
| **S-spread** | group의 G개를 노드에 돌아가며 흩음 (현재 기본) | DSec power-of-k, AgentENV round-robin, ROCK SPREAD | 거의 없음. 기준선 |
| **S-pack** | group의 G개를 한 노드에 모음 | SkyRL-v0 image hash 배치, image·snapshot locality(Daytona, CubeSandbox), VenusRL group CoW | group 안 상관이 그대로 노드 상관이 됨 |
| **S-split** | group을 노드 k개에 G/k개씩 (예: 8 = 4 + 4) | locality를 일부만 지키는 배치 | 중간 |
| **S-job** | RL job 하나의 batch(B×G) 전체를 노드 하나(또는 묶음)에 둠. 노드마다 다른 job | DSec 사용자 격리, quota·backend로 노드 묶음이 나뉨 | job의 step 단계(tool 단계, LLM 대기, 학습 구간)가 노드 전체를 함께 움직임 |
| **S-mix** | 노드마다 workload 종류 비율이 다름 (SWE 대 가벼운 sandbox) | 배치 때 미래 수요를 모름 | 오래가는 사용률 차이 |

- **S-job은 서버 두 대와 잘 맞는다.** bud04에 job A, bud05에 job B를 두고 두 job의 step 시작을 어긋나게 한다. 한 job이 학습 구간이나 LLM 대기에 있을 때 다른 job은 tool 단계에서 CPU가 모자라다. virtual node가 이 시간차를 쓸 수 있는지가 질문이다. 동기 RL의 학습 구간은 `fleet.py`의 `train_s`처럼 sweep한다.
- S-job의 POOL 상한은 서버 안 E2(2 × 15코어 노드에 job 하나씩)에서 실측하고, 서버 사이 E3는 그 결과로 맞춘 simulator로 계산한다.
- **264 trace의 한계**: group을 같은 trace 복제로 만들기 때문에 group 안 상관이 실제보다 강하다(자기 자신 복제). S-pack의 격차는 상한으로만 읽는다. 실제 G개 sampling이 있는 RL trace(10절)로 바꾸면 이 한계가 없어진다.
- S-mix는 두 번째 workload 종류가 필요하다. 264 trace만으로는 CPU가 적은 합성 sandbox(같은 대기, 명령 CPU를 줄인 것)로 흉내 낸다. 우선순위는 가장 낮다.

## 4. 조건

| 축 | 수준 | 쓰는 단계 |
|---|---|---|
| 밀도 (물리 코어당 동시 sandbox) | 4, 5, 6 | 전부 |
| TPOT | Gemini 맞춤, 20, 40, 80ms | E1 전부, E2·E3는 사용률 60~90%가 되는 두 수준 |
| vCPU | 1 (기본), 2 | E1, E2 |
| 초기 배치 (3.5절) | S-spread, S-pack, S-split(4+4), S-job. S-mix는 여유가 있을 때 | E2, E3 |
| job 사이 step 어긋남 (S-job) | 0, 학습 구간의 ½, 무작위 | E2, E3 |
| 노드 크기 | 30, 15, 10코어 | E2 |
| 모드 | 비동기 pool, 동기 GRPO (B=16, G=8) | E2, E3 |
| 반복 | seed 3개, 순서 무작위 | 전부 |

모든 조합을 다 돌리지 않는다. E1에서 사용률 60~90% 구간을 만드는 (밀도, TPOT) 두세 쌍을 고르고 E2~E4는 그 쌍으로만 돈다.

## 5. 측정과 지표

| 측정 | 방법 (기존 `sandbox.py pilot` 기능 재사용) |
|---|---|
| 노드별 runqueue 대기, 실행 시간 | `/proc/schedstat` 1ms 샘플 |
| VM별 vCPU·VMM thread 대기 | thread `schedstat` 10ms |
| tool call 시작·끝 | serial marker(`SWE_CMD_START/END`), host monotonic ns |
| guest 메모리 | VM별 RSS, 노드 free 메모리 |
| 주파수 | `turbostat` 또는 `scaling_cur_freq` 1초 |
| 서버 사이 시각 | NTP 동기(둘 다 synchronized). 서버 사이 지표는 10ms bin으로 맞춘다 |

| 지표 | 정의 |
|---|---|
| 잃은 CPU | 노드 runqueue 대기 합 ÷ 실행 시간 합 |
| **격차** | (잃은 CPU_ISO − 잃은 CPU_POOL) ÷ 잃은 CPU_ISO. 꼬리 지표에도 같은 식을 쓴다 |
| 동시 여유 | 한 노드가 contention인 시간 동안 다른 노드에 빈 코어가 있던 비율. 잃은 CPU로 가중 |
| tool call 느려짐 | 같은 명령의 단독 실행 대비 시간 비율. p50, p99 |
| rollout 시간, step time | p50, p99. 동기 모드는 step time |
| 오염 | timeout, 종료 코드 불일치 비율. 높은 밀도에서 경로가 갈라지는지 |

## 6. 실행 환경

| 항목 | 설정 |
|---|---|
| CPU | 각 서버 node 0. CPU 0-1은 harness와 sampler, 2-31(30코어)은 sandbox |
| VMM thread | SMT가 꺼져 있어 sibling이 없다. vCPU와 같은 노드 cpuset에 둔다(VMM CPU도 노드 부하에 포함) |
| 메모리 | node 0 DRAM. Firecracker가 guest 메모리를 lazy하게 잡으므로 VM 크기 합이 물리 메모리보다 커도 된다. 실제 RSS는 E0에서 확인 |
| 주파수 | turbo 켬, governor `schedutil` 그대로 두고 주파수를 기록한다. DSec가 말한 간섭(turbo, 대역폭, LLC)을 지우지 않기 위해서다. 대조로 한 조건만 turbo 끔 |
| 다른 사용자 | bud05 node 1에 다른 사용자의 gem5가 있다. node 0과 겹치지 않지만 실행마다 기록한다 |
| 네트워크 | host NAT. pip 같은 외부 접근은 실행 결과를 흔들 수 있어 오염 지표로 본다 |

## 7. 용량

| 항목 | bud04 | bud05 | 판단 |
|---|---|---|---|
| node 0 DRAM 여유 | 약 75GB | 약 64GB | 서버당 150~180개면 VM당 평균 RSS가 350~450MiB 안쪽이어야 한다. **E0에서 가장 먼저 확인할 위험** |
| `/Serverless` 여유 | 204G | 214G | rootfs 저장소. 264개 전부(약 500GB)는 안 들어가므로 일부만 쓴다 |
| `/` 여유 (Docker) | 61G | 97G | 이미지를 받아 ext4로 바꾼 뒤 바로 지운다. bud05에는 이미지가 이미 있다 |

RSS가 넘치면 순서대로 시도한다.
1. Docker peak가 작은 trace를 고른다. 기존 `select --max-peak-gib`.
2. guest 메모리를 1GiB로 줄인다. guest page cache가 자라는 것을 막는다.
3. balloon의 free page reporting을 쓴다. 쓰는 Firecracker 버전에서 지원하는지 확인해야 한다.

## 8. 판정 기준 (실행 전에 고정)

- **주장 1 성립**: 사용률 80% 이하의 한 조건 이상에서 tool call p99 느려짐이 단독 대비 1.2배 이상이거나 잃은 CPU가 5% 이상.
- **주장 2 성립 (virtual node 필요)**: S-spread 이외의 배치 시나리오(S-pack, S-split, S-job) 중 하나 이상에서, 격차가 잃은 CPU 기준 30% 이상이고 rollout p99 또는 step time이 5% 이상 줄어든다. 264 trace의 S-pack 결과만으로는 성립으로 보지 않는다(group 상관이 상한이므로).
- **배치로 해결되는가**: 같은 조건에서 S-spread로 바꾸기만 해도 격차의 대부분이 사라지면, "locality를 포기하면 된다"는 반론이 선다. 이때는 locality를 포기하는 비용(image 사본, 생성 시간, 메모리)을 함께 보여야 한다.
- **주장 2 약함**: 모든 조건에서 step time 감소가 2% 미만. 이때는 방향을 다시 본다(연구 노트 7.7의 다른 쓰임).
- **주장 3 성립**: 가장 좋은 이동 없는 방법(배치 + QoS)이 격차의 50% 미만을 메우고, 경계 oracle 이동이 80% 미만을 메운다(HANDOFF 7.5의 P3 기준과 같음).

## 9. 구현 작업

| 작업 | 위치 | 크기 |
|---|---|---|
| Firecracker, guest kernel을 bud04·bud05에서 빌드 | `implementation/virtual_machine/build.sh`, `experiments/contention/.local/` | 작음 |
| SMT 꺼진 서버용 VMM thread 배치 | `sandbox.py`의 `--sibling-offset` 경로 | 작음 |
| token 모델로 `cmd/NNN.gap` 생성 (`--llm-model`) | `sandbox.py` | 작음 |
| rootfs를 `/Serverless`에 두는 경로 설정 | `sandbox.py` (`LOCAL`) | 작음 |
| 두 서버 동시 실행과 결과 모으기 | `sandbox.py pilot`을 각 서버에서 같은 seed·시작 시각으로 실행하고 분석에서 합침 | 중간 |
| 동시 여유와 격차 지표 | `sandbox.py episodes`, `report.py` | 중간 |
| 초기 배치 `--placement {spread,pack,split,job}` | `sandbox.py pilot`의 group 멤버 → 노드 매핑(지금은 `i mod N` 한 줄), `fleet.py`의 `slot_home`(RL 모드에서 같은 한 줄) | 작음 |
| S-job: job별 step 시작 오프셋과 학습 구간 | `sandbox.py pilot`의 `group_loop`에 job 단위 barrier 추가. `fleet.py`는 `train_s`가 이미 있음 | 중간 |
| S-mix용 가벼운 합성 sandbox | candidates에 CPU를 줄인 변형 추가 | 중간 |
| QoS 구성 | `sandbox.py pilot` | 중간 |
| 동적 LLM 모델(v2) | host 쪽 gap 공급 경로 | 큼, E1~E3 이후 |

## 10. RL trace

RL trace로 바꾸면 세 가지를 얻는다. RL policy의 행동(명령, 길이), RL policy의 token 길이(대기 모델 입력), 같은 task에서 G개를 sampling한 실제 group 상관이다. LLM 시간은 모델로 넣으므로 trace의 수집 속도는 상관없다.

### 10.1 결론: 쓸 수 있다. 주 후보는 `sweagent/iter2-rl-rollouts`

[sweagent/iter2-rl-rollouts](https://huggingface.co/datasets/sweagent/iter2-rl-rollouts)는 실제 GRPO **학습 중** policy가 만든 rollout 전부를 공개한 데이터다(MIT). 2026-10-26에 card를 읽고, shard 하나(`trajectories_00`, 3,857개)를 받아 직접 확인했다.

| 항목 | 값 | 확인 방법 |
|---|---|---|
| policy, 알고리즘 | Qwen3.5-35B-A3B, GRPO + dynamic sampling, rollout-batch 32 × n-samples 4 (최대 8), 50 step | card |
| 규모 | 학습 rollout 20,248개, task 2,417개(task당 약 8.4개), step당 약 405개. 별도로 eval 5,330개 | card |
| 행동 형식 | mini-swe-agent류, **bash만**. turn당 call 1개(418,720개 중 2개인 turn 4개). 작업 경로 `/testbed`. assistant 메시지에 `extra.actions`가 있다 | shard 직접 확인 |
| reasoning | assistant 본문에 `<think>` 포함(think 부분 글자 수 중앙값 143, p90 1,564). 전체 응답 token id(`response_token_ids`)도 있음 | shard |
| 시각 | 메시지마다 `timestamp`. tool 결과 → 다음 응답 간격 중앙값 7.7초, p90 33초, p99 80초(경합 있는 학습 중 값). `env_creation_time`도 있음 | shard |
| group | 파일 이름이 `<instance_id>_<sample_index>`. 같은 step의 group은 sample index가 가깝고(예: 8632, 8638) mtime이 비슷하다. 한 task가 여러 step에 걸쳐 group 2개 정도 나온다. group 멤버가 여러 shard에 흩어져 있어 6개 shard(2.8GB)를 다 받아야 한다 | shard |
| image | task마다 `instance.image_url`. `swerebench/sweb.eval.*`, `docker.io/swerebenchv2/*`, `aweaiteam/scaleswe:*` 세 종류 | shard |
| 길이 | turn 중앙값 106, p90 174, 최대 200. 마지막 context 중앙값 6.9만 token, p90 10만 | shard |
| 종료 | Submitted 89%, TimeExceeded 8%, LimitsExceeded 3% | shard |

### 10.2 264 trace와 비교

| | 264 trace | iter2 RL rollouts |
|---|---|---|
| 출처 | Gemini 3.5 Flash 평가 | RL **학습 중** rollout |
| group | 없음(복제로 흉내) | 실제 G=4(최대 8) |
| turn 수 | 중앙값 45~51 | 중앙값 106 |
| LLM 시간 | 기록(빠른 API) + token | 기록(학습 중 경합 상태) + token |
| image | bud05에 이미 있음 | 새로 받아야 함 |

RL trace는 rollout이 두 배 이상 길고 context가 길어서, 같은 TPOT에서도 LLM 대기 비중이 크다. 3.4절의 사용률 추정은 E0 단독 실행 뒤 다시 계산한다.

### 10.3 남은 일과 위험

- **변환기**: assistant 메시지에 이미 `extra.actions[].command`가 있어, 기존 `replay.load_trace`가 읽는 mini 형식으로 감싸기만 하면 된다. tool 메시지 본문의 `<returncode>N</returncode>`와 메시지의 `timestamp`를 `extra`로 옮기고, `model_patch`를 제출 내용으로 둔다. 끝 신호 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 처리를 맞춘다.
- **작업 경로가 image 종류마다 다르다** (shard 확인).
  - `swerebench/*`: `/testbed`.
  - `swerebenchv2/*`: `instance.workdir` (예: `/websockets`).
  - `aweaiteam/scaleswe:*`: `instance.workdir` (`/workspace/<repo>`). 재생 전에 `instance.pre_commands`(base commit checkout 등)를 setup으로 실행해야 한다.
  - 변환기는 `workdir`를 cwd로 쓰고 없으면 `/testbed`로 둔다.
- **token 수**: rollout 전체의 `response_token_ids`만 있고 turn별 값은 없다. Qwen3.5 tokenizer로 turn별로 다시 세거나, rollout별 token/글자 비율(중앙값 0.61)로 나눈다.
- **group 복원**: 6개 shard를 받아 `instance_id`, sample index, mtime으로 step별 group을 묶는다. card에는 training과 eval이 디스크에서 섞였다는 경고가 있고, 같은 sample index가 두 번 나오는 경우도 봤다. 묶는 규칙을 먼저 검증한다.
- **image와 디스크**: task마다 image가 하나라 task 수가 곧 image 수다. 압축 크기는 scaleswe 0.5~0.7GB, swerebench 계열 0.8~2.4GB(조사 agent가 Docker Hub에서 표본 확인, 압축 해제 크기는 미확인). `/Serverless` 200GB면 task 30~40개(group 60~80개, rollout 240~320개) 정도가 안전하다. scaleswe image를 우선 고른다.
- **image digest**: trace에는 tag만 있다. 수집 당시 digest를 알 수 없어, 받은 digest를 기록하고 E0 단독 재생의 종료 코드 일치율로 확인한다.
- **network**: 명령 첫 단어 기준 `pip` 1.6%, `curl` 0.3%. 외부 접근이 결과를 바꿀 수 있어 오염 지표로 본다.
- **실행 시간**: turn이 길어 rollout 하나가 TPOT 20~40ms에서 30분~1시간 걸릴 수 있다. 측정 구간을 더 길게 잡는다.

### 10.4 다른 후보

| 데이터 | 쓰임 | 한계 |
|---|---|---|
| `sweagent/combo2-rl-rollouts` | 같은 계열의 다른 run(27,787개). `group_info`로 group이 명시됨 | 대체된 run. 학습과 eval이 섞임 |
| `AxT-dev/qwen3-32b-rl-step200-r2e-gym-trajectories` | DeepSWE 계열 RL checkpoint, task당 4회. step별 `llm_exec_time`, `env_exec_time` 있음 | R2E-Gym의 `file_editor`·`search` 도구를 image 안에 설치해야 함. 약 100GB. card·license 없음 |
| `r2e-edits/deepswe-swebv-eval-n16-verifier-v1` | SWE-bench Verified 500 × 16 | 16개가 여러 checkpoint의 섞임이라 진짜 G=16 group이 아님. 로그가 문자열 하나로 펼쳐져 있음 |
| `Rtian/MiRL-TB2.1-trajectories` | terminal task, bash만, 89 × 16 seed | 작은 모델(4B), 무거운 설치 명령 |

AxT-dev, r2e-edits, MiRL은 조사 agent의 확인 결과이며 직접 받아 보지는 않았다.
