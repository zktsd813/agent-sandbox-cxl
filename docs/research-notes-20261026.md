# 연구 노트: 실험 결과와 논의 정리 (2026-10-19 ~ 2026-10-26)

이 문서는 2026-10-19부터 10-26까지 진행한 실험과 논의를 나중에 다시 볼 수 있게 정리한 것이다. 세부 수치는 원본 문서에 있고, 여기에는 결론, 그렇게 판단한 이유, 버린 주장, 아직 열린 질문을 적는다.

| 원본 문서 | 내용 |
|---|---|
| [HANDOFF.md](HANDOFF.md) | 전체 설계, 실험 계획(Phase A~F), 리뷰어 질문 |
| [experiments/contention/RESULTS-fleet.md](../experiments/contention/RESULTS-fleet.md) | DSec 규모 fleet simulation 결과(S1~S7), 판정 |
| [trace-collection-plan.md](trace-collection-plan.md) | RL rollout trace 수집 설계 v3 (W1~W5, H100 본 수집) |
| [rl-trace-survey.md](rl-trace-survey.md) | SWE dataset·image, 모델, RL framework, 비SWE 환경 조사 |

---

## 0. 한눈에 보기

- **contention은 192코어 node에서도 있다.** 다만 사용률 80% 이하에서는 대부분이 node 안의 scheduler 대기다. node 사이 이동으로 줄일 수 있는 몫은 4 vCPU 기준 20~35%이고, 85~90%에서는 40~60%로 커진다. 여기서 hand-off는 이상적 공유 이득의 70~90%를 되찾고, migration은 그 1/3 이하다.
- **RL step 시간 이득은 지금 모델에서는 작다(1.1% 이하).** step 시간을 정하는 가장 늦은 rollout은 LLM 대기가 대부분이라 CPU 대기에 둔감하다.
- **지금 trace(264개)는 RL 학습 trace가 아니다.** 평가용 rollout이고, 시작 시각과 상관 구조가 없다. 그래서 RL 학습 rollout trace를 직접 수집하기로 했다(H100 cloud 본 수집, A6000 시험).
- **CXL 메모리는 싸고 큰 메모리가 아니라 귀한 자원이다.** 그래서 CXL은 이동성과 host 간 공유에만 쓴다.
- **새 초점은 G(꼬리 sandbox의 CPU 간섭)다.** 동기 RL에서 step을 늦추는 critical-path sandbox가 CPU 간섭으로 더 느려진다는 것을 보이면, CPU를 관리하지 않는 기존 시스템(VenusRL 등)을 정면으로 반박할 수 있다.

---

## 1. 실험 결과 (시간순)

### 1.1 작은 node 실측과 pool 효과 (10-19 ~ 10-21)

- optane 서버에서 5코어 node 여러 개에 microVM trace를 동시에 재생했다. 공유 pool을 쓰면 contention이 줄었다.
- 이 이득은 **node가 작아서** 생긴 것이다. node당 sandbox 수(밀도)가 높아서 생긴 것이 아니다. 같은 사용률에서 node 수요의 흔들림은 √(사용률 × node 코어 수 × burst 크기)에 비례한다. 상대적인 흔들림은 node가 클수록 작아진다(2.1절).
- 이 실측으로 simulator(`fleet.py`)를 검증했다. simulator는 실측보다 contention을 작게 보고 이동 효과를 크게 본다(RESULTS-fleet.md 1절).

### 1.2 DSec 규모 fleet simulation (S1~S6, commit 4dcabd06d)

- 192코어 node 16개에 trace 264개를 무작위로 시작해 연속 재생하고, power-of-2 배치를 썼다.
- 이상적 유체 모델에서는 192코어·사용률 80%의 잃은 CPU가 1 vCPU 0.001%, 4 vCPU 0.21~0.45%로 거의 없었다.
- RL 동기 rollout(S3c): 4 vCPU B8G8 사용률 80%에서 1.06%, B64G1 79%에서 2.34%. 처음 S3·S3b는 모두 같이 시작하는 과도기를 잰 것이어서 철회했다.
- S4rl: hand-off는 즉시 이동과 거의 같고(이상적 이득의 85~90%), migration은 그 1/4 이하다.
- step 지연은 평균 1.1% 이하였다.

### 1.3 trace 수 검사 (commit aac9fbd84)

- 질문: "Q1에서 contention이 거의 없다는 게 trace 수가 부족해서인가?"
- trace를 15~120개로 줄여도 contention은 같거나 커졌다. 시작 시각이 무작위이면 복제는 문제가 되지 않는다. 따라서 trace 부족이 원인은 아니다(RESULTS-fleet.md 6절).
- 단, **동기 RL step에서는 복제가 문제다.** 같은 trace 두 개가 t=0에 함께 시작하면 burst가 ms 단위로 겹쳐서 가짜 상관이 생긴다. 이것이 trace 수집 규모의 기준이 되었다(5.2절).

### 1.4 실측 scheduler 대기 (S7, commit 8be33ae66)

질문: "DSec는 contention이 있다고 하는데, node와 CPU가 커진다고 작은 CPU set에서 본 구조가 깨질 것 같지 않다."

1. **pilot audit** (`pilot_audit.py`): 실측 사용률이 모델보다 10~15% 높았다. 원인은 세 가지였다.
   - 수요가 빠져 있었다.
   - contention이 60초 timeout을 부르고 timeout 뒤에는 replay 경로가 갈라진다. vCPU 시간의 10~36%가 경로가 갈라진 뒤에 쓰였다.
   - 소켓 사이 CPU 사용.
2. **처음 맞춘 값(b=9)은 오염되어 있었다.** 사용률 85% 근처에 가짜 상한이 생겼고, slope 외삽이 pool 전체(P=3072)에서 폭발했다. 깨끗한 run만 쓰고(경로가 갈라진 CPU 5% 미만), 사용률을 맞춘 뒤 다시 맞추어 b=6, log RMS 0.12/0.13을 얻었다. `kernel_wait_size="node"`를 썼다.
3. 모델은 wait/run = a(P)·exp(b·util)이다. **같은 사용률에서 a(P)는 pool이 5→20코어로 커져도 줄지 않았다.** capacity 방식(잃은 코어 시간)과 latency 방식(work-conserving 재분배)을 두었다.
4. 192코어 결과(4 vCPU, floor / slope):
   - 사용률 80%: 잃은 CPU 1.70 / 3.66%. 이상적 공유로 되찾는 몫 +35 / +32%, hand-off +25 / +22%, pre-copy +9 / +8%.
   - 사용률 85%: 3.76 / 7.13%. 이상적 공유 +57 / +46%, hand-off +50 / +40%.
   - node 절감: 4 vCPU 2~6%. 유체 모델에서는 7~8%였다.
5. 판정: Q1 "성립(수정)", Q2 성립, Q3 약함.

### 1.5 node당 2,400개 (S8, 문서와 commit에 아직 없음)

- 질문: "DSec는 node당 sandbox가 훨씬 많다. 5 node 실험에서 pool 이득이 보였으니 2,400개쯤 돌려야 하지 않나?"
- 설정: 192코어 node 16개, node당 2,400개. LLM 대기를 늘려(`gap_scale` 4.5~9.8) 가벼운 sandbox로 만들고 사용률을 70~92%로 맞췄다. ramp 4,000초 + 측정 1,800초. grid는 `experiments/contention/grids/S8-v{1,4}.json`, 결과는 `.local/report/S8-v{1,4}.json`(git 밖)에 있다.
- 처음 실행은 무효였다. 모두 함께 시작하는 과도기만 잰 것이었다(끝난 trace 84개와 0개). ramp를 늘려 다시 돌렸다.

잃은 CPU(%). floor는 실측 scheduler 대기의 floor 외삽을 넣은 값이다.

| | 사용률 | 이동 없음 (유체 / floor) | 이상적 공유 (floor) | hand-off 10ms (floor) | pre-copy (floor) |
|---|---|---|---|---|---|
| 4 vCPU | 72% | 0.04 / 0.95 | 0.68 | 0.84 | 0.91 |
| 4 vCPU | 82% | 0.63 / 2.58 | 1.25 | 1.45 | 2.13 |
| 4 vCPU | 87% | 1.74 / 4.89 | 1.72 | 1.97 | 3.82 |
| 4 vCPU | 91~92% | 4.88 / 10.77 | 2.61 | 3.35 | 8.77 |
| 1 vCPU | 81% | 0.01 / 1.25 | 1.14 | 1.24 | 1.25 |
| 1 vCPU | 91% | 0.47 / 3.23 | 2.18 | 2.34 | 2.95 |

- 같은 사용률에서 619/354개 밀도(S7)보다 contention이 1.3~1.5배 큰 정도다. 밀도를 4~7배로 올려도 구조는 크게 바뀌지 않는다.
- 흔들림이 √(U·C·A)로 정해지므로, 독립인 sandbox는 개수가 많아져도 평균화된다. 가벼운 sandbox를 많이 넣으면 burst 크기 A가 같아서 흔들림도 비슷하다.
- 4 vCPU 82~87%에서는 node 사이 몫이 커져서 hand-off가 floor 대기의 40~60%를 되찾는다. pre-copy migration은 17~22%로, hand-off의 약 2/5다.

---

## 2. 논의에서 정한 것

### 2.1 burst는 있는가, 없는가

- **sandbox 하나는 매우 bursty하다.** 시간의 21~27%만 바쁘고, 바쁠 때는 평균의 4~5배를 쓴다.
- **node 수요는 독립이면 평균화된다.** n개를 더하면 상대 흔들림이 √n으로 줄어든다. 192코어 node에서는 흔들림이 작아진다.
- 그래서 "node CPU가 커지면 node 안에서 해결된다"는 말은 **sandbox가 독립일 때만** 맞다. 이 기법이 쓸모 있으려면 상관이 있어야 한다.

### 2.2 sandbox는 독립인가

- 상관 계수 ρ가 약 1/n보다 크면 평균화가 깨진다.
- **node에만 있는 상관은 이동으로 고칠 수 있다. cluster 전체의 상관은 고칠 수 없다**(옮길 곳이 없다).
- RL 학습에서 독립이 아닐 이유(논문 근거로 추론, 측정은 아님):
  - 동기 step: 모든 rollout이 t=0에 시작한다.
  - GRPO group: 같은 task의 G개는 같은 image와 비슷한 명령을 쓴다.
  - LLM 응답이 batch로 함께 돌아온다(첫 turn 물결).
  - image locality: 같은 image를 가진 node에 몰린다.
  - 생성 폭주: step 시작에 수백 개 container·VM을 한꺼번에 만든다.
- 지금 trace로 잰 상관은 **단독 trace를 인위적으로 맞춰 놓은 것**이라 조건부 진술일 뿐이다. 다른 repo 0.03, 같은 repo 0.05~0.07, 같은 task 0.64~0.77(자기 자신을 이동한 상한)이었다. 처음에 이것을 "실측 trace로 잰 상관"이라고 쓴 것은 과장이었고 철회했다.
- **least-loaded 배치**는 이미 모델에 있다(power-of-2). k=4로 늘려도 82%에서 0.45%이고, hand-off는 약 0.03%다. 배치만으로는 부족하다.
- image locality 때문에 배치가 쏠린다면 **이동 대상 node에 image·파일이 없는 문제**가 생긴다. hand-off에는 image나 rootfs도 함께 다루는 기능이 필요할 수 있다(writable layer를 CXL이나 공유 스토리지에 두는 HANDOFF 2.3의 device 연속성과 같은 문제).

### 2.3 DSec에서 확인한 사실 (arXiv 2609.22978)

- node: 2 × 96코어 EPYC 9655, 1.5TB. 약 160 node, 30K 코어.
- 동시 sandbox 380K, node당 약 2,400개. node당 microVM 800개 또는 container 3,200개까지 허용하고, 관측된 최대는 524 / 1,048이다.
- sandbox의 90%가 요청 CPU의 5% 이하를 쓴다.
- QoS: LS/BE 두 등급, SCHED_IDLE + core scheduling. 사용률이 80%를 넘으면 cloud로 넘긴다.
- §8.5: 성능 저하 +45.2% → +17.3%. 원인은 turbo, 메모리 대역폭, LLC 간섭이라고 했다(runqueue 대기만이 아님).
- 이미지: EROFS on-demand. virtio-pmem DAX로 메모리 −40.2%, free page reporting + DAMON으로 −21.2%.
- pause/resume은 Firecracker snapshot으로 한다. fork와 migration은 없다.
- **DSec의 높은 밀도는 sandbox가 대부분 놀기 때문이다.** 이것은 우리 trace에서 본 것(LLM 대기가 대부분)과 같은 현상이고, 정도만 다르다.

### 2.4 테스트베드 제약

- 32코어 host 2대(필요하면 4대). host마다 DRAM 128GB, 공유 CXL pool 64GB.
- 밀도는 메모리가 정한다. sandbox가 0.5~1GB면 2대에 약 300~600개, 4대에 약 540~1,100개다.
- **CPU 수를 줄여 밀도를 맞추면 안 된다.** 작은 node는 contention을 부풀린다.
- 역할 분담: 테스트베드는 메커니즘을 재고 32코어에서 simulator를 보정한다. node 크기를 192코어까지 늘리는 것은 simulator가 한다.

### 2.5 CXL은 귀한 자원이다 (사용자 지적으로 정정)

- 처음에는 CXL을 싸고 큰 메모리로 가정했다. 이는 틀렸다. 테스트베드에서 pool 64GB는 전체 메모리의 10~20%다.
- 그래서 CXL은 **이동성과 host 간 공유가 필요한 곳에만** 쓴다. 어떤 VM을 CXL에 상주시킬지가 "이동성 예산(mobility budget)" 문제가 된다.
- 놀고 있는 sandbox를 CXL에 주차하는 아이디어(C)는 방향이 반대다(2.7절).

### 2.6 RL 학습 구조 (GRPO, 동기·비동기)

- **GRPO group**: task마다 G개를 sample한다. group은 step마다 새로 만들어지고, 같은 초기 환경에서 시작한다. R2E-Gym 코드로 확인했다.
  - rollout마다 새 container를 만든다(이름은 시각+pid hash).
  - `setup_env`은 결정적이다(고정되지 않은 `uv pip install chardet` 하나만 예외).
  - 도구는 `put_archive`로 복사하고, 명령은 `exec_run`으로 실행한다. LLM은 litellm과 `LLM_BASE_URL`로 부른다.
  - 따라서 첫 group의 G1~G8과 학습 이후 group의 G1~G8은 초기 환경이 같다.
- 문제 64 × 8이면 group은 8개 단위로만 묶이고, 문제 사이는 따로 센다. 학습 batch는 group이 모두 끝나야 들어간다.
- **동기 RL**: step barrier가 있다. 가장 늦은 rollout이 step 시간을 정한다.
- **비동기 RL**: in-flight weight update(PipelineRL, AReaL, NeMo, Olmo 3, INTELLECT-3, Motif 3). 생성을 멈추지 않고 weight만 바꾼다. sandbox는 멈추지 않는다. group 안 G개가 모두 끝나야 학습 batch에 들어가는 것은 같다.
- **partial rollout**(APRIL in slime, Kimi k1.5): 동기 RL에서 쓰는 꼬리 처리법이다.
  - 필요한 것보다 많이 시작하고(over-provision), 목표 개수가 모이면 나머지를 중단한다.
  - 중단된 rollout은 다음 step에서 **새 weight로 이어서** 생성한다. 버리지 않는다.
  - group은 여전히 모두 끝나야 학습에 들어간다.
  - APRIL에서 이어 가는 몫은 꼬리 몇 개가 아니라 token의 약 40%다. 처음에 "꼬리 몇 개만"이라고 설명한 것은 틀렸고 정정했다.
  - 이것은 특정 연구(APRIL, Kimi)가 주장하고 쓴 방법이다. 비동기 RL의 일반적인 방법은 아니다.
- **critical path**: 꼬리 rollout, 그리고 group의 마지막 멤버.
- **LS(지연에 민감한) sandbox는 RL 상태에서 알 수 있다.** 어느 group이 아직 덜 끝났는지, 어느 rollout이 꼬리인지를 orchestrator가 안다.

### 2.7 CPU contention 외의 이득 (A~G)

| | 아이디어 | 판단 |
|---|---|---|
| A | group fork: G개를 같은 초기 상태에서 복제 | 이미 실무에서 한다(Tensorlake 20시간 → 4.4시간, AgentENV, TrEnv). 새로움이 약하다 |
| C | 노는 sandbox를 CXL에 주차 | CXL이 귀하다는 전제와 충돌. 가장 가까운 선행: arXiv 2608.15127(offloading으로 평균 메모리 4.6배 감소), FaaSMem, Pond |
| D | pause/resume | 동기 + partial rollout(APRIL, Kimi)에서만 필요하다. 비동기는 in-flight update라 sandbox가 멈추지 않는다 |
| **G** | **간섭과 critical-path CPU** | **초점으로 정함** |

사용자는 C, D, G에 관심을 보였고, C는 CXL 희소성 때문에, D는 적용 범위가 좁아서 G에 집중하기로 했다.

### 2.8 G: 꼬리 sandbox 처리 조사

동기 RL에서 꼬리를 다루는 방법:

- **생성 쪽**: RollPacker, Seer, TLT, DAS, WAR, Libra, Heddle, VenusRL.
- **partial rollout**: Kimi, APRIL.
- **oversample하고 버리기**: DAPO, T1, SemiAnalysis, ROLL Flash, Nebius.
- **masking**: DeepSWE, SkyRL-Agent.
- **sandbox 쪽**
  - 시작 속도: GKE, Kimi, RollArt, DSec.
  - 상한 설정: T1, Nebius, Amazon.
  - 정적 CPU: Nebius는 cpuset으로 591.8초 중 29.1초를 줄였다(→ 196.1초). DSec는 정적 LS/BE.
  - OOM 때 이동: VenusRL(RDMA, 2.7초).
  - 자원을 늘려 재시도: Orchard.
- **빈 곳: tool call 시간 단위에서 꼬리를 알고 CPU나 배치를 동적으로 관리하는 시스템은 없다.**
- 꼬리를 버리면 학습이 치우친다. T1은 취소된 꼬리에 가장 어려운 task가 몰린다고 지적했다.

### 2.9 VenusRL (arXiv 2610.03286)과 반박

- 분리형, 비동기.
- critical group 우선순위: Qwen3-0.6B 길이 예측기(Spearman 0.98)로 GPU slot과 KV cache를 배정한다.
- group 단위 CoW page 공유로 400GB node에 sandbox 905개(E2B는 100개).
- OOM이면 Mooncake RDMA로 2.7초에 이동.
- 성능: slime 대비 1.07~3.26배. strict on-policy에서 4B 1.07~1.15배, 32B 1.05~1.85배.
- **CPU는 약 14배 oversubscription하고 관리하지 않는다.** 논문은 이것을 "density를 위한 QoS trade-off"라고 부른다.

반박의 틀:
- CPU 지연은 critical group의 꼬리 sandbox에 떨어진다. tool 시간 비중 5~15% × 2배 느려짐 ≈ step 시간 5~15%. VenusRL의 on-policy 이득과 비슷한 크기다.
- 필요한 증거 사슬:
  1. 꼬리 rollout이 tool을 얼마나 쓰는가(tool 시간 비중).
  2. 고밀도 node에서 그 tool call이 얼마나 느려지는가(CPU 간섭).
  3. 꼬리 sandbox가 실제로 간섭을 받는 node에 있는가(배치와 상관).
  4. 그 느려짐이 step 시간에 얼마나 전달되는가.
  5. 우선순위와 hand-off로 얼마나 되찾는가.
- 지금 Q3(step 지연 1.1% 이하)는 이 반박에 불리하다. 지금 모델에는 꼬리 우선순위, 메모리·cache 간섭, 높은 밀도의 VenusRL식 oversubscription이 없다. 새 trace와 모델로 다시 봐야 한다.

### 2.10 G의 설계 방향 (두 단계)

1. **node 안 우선순위**: critical-path sandbox에 cgroup weight를 더 준다. CXL이 필요 없다.
2. **node 사이 hand-off**: node가 포화되었거나 간섭이 runqueue 바깥(메모리 대역폭, LLC)에서 올 때만 쓴다. 이 단계만 CXL 상주가 필요하고, 꼬리가 될 가능성이 높은 소수 sandbox만 CXL에 둔다(이동성 예산).

---

## 3. trace 수집 결정

세부는 [trace-collection-plan.md](trace-collection-plan.md) v3에 있다. 여기에는 왜 그렇게 정했는지를 적는다.

### 3.1 왜 직접 모으는가

- 공개 trajectory에는 명령은 있지만 시각이 없다.
  - Kwai mini-swe-agent 66K: shell 명령.
  - nebius SWE-rebench OpenHands 67K: tool call.
  - SWE-Gym sampled 6K, nebius SWE-agent 80K.
- LLM 시간을 token 수로 추정하면 리뷰어가 납득하기 어렵다. RL 학습 중 policy의 실제 LLM 시간이 필요하다. 공개 데이터는 보조로만 쓴다.
- 지금 264 trace는 Gemini 3.5 Flash의 평가 trace다. 학습 trace가 아니다.

### 3.2 무엇을 가져오는가

- GPU 쪽에서 가져오는 것: 명령과 순서, image digest, turn마다 LLM 요청 원문, token 수, 지연, 보상.
- CPU burst, 메모리, dirty page, contention은 가져오지 않는다. 우리 bare-metal 서버에서 Firecracker로 재생하며 잰다.
- 동시에 많이 돌리면 trace가 오염되므로(timeout으로 경로가 갈림) 순차 또는 낮은 병렬도로 모은다. agent 행동은 속도와 무관하다. 시간상의 상관(G개 동시 시작, 첫 turn 물결)은 합성에서 넣는다.
- LLM 지연은 수집한 요청을 H100에서 동시 요청 수 N을 바꿔 다시 보내서 모델로 만든다(M7).

### 3.3 GPU 선택

- 선택지: A6000 × 2(NVLink), Blackwell PRO 4500 × 4, cloud H100 1~8대(비용이 들고 CPU 간섭은 모델링할 수 없음).
- 결정: **A6000에서 계측·smoke·pilot, H100 cloud에서 BF16 본 수집과 지연 profiling.** A6000의 FP8 pilot 데이터는 본 데이터에 섞지 않는다.
- 시간 추정(`experiments/rollout-trace/estimate_time.py`): A6000으로 전부 하면 12.7~30.9일이다. 처음에 1.5~2주라고 말한 것은 30K context의 KV 읽기 비용과 KV 용량을 빠뜨린 틀린 값이었다.

### 3.4 모델과 workload

- 모델: 32B급 rollout이 주력이고 30B MoE는 짧은 보조로 쓴다(사용자 결정). 작은 모델은 production과 거리가 멀다.
- workload (v3, 합계 task 2,137개, rollout 19,144개):
  - W1 R2E-Gym: DeepSWE-Preview 512 × 8, Qwen3-32B 128 × 8.
  - W2: 같은 128 task, mini-swe-agent + Qwen3-Coder-30B-A3B.
  - W3: Terminal-Bench 2.0 89개 × 8.
  - W4: DeepCoder 1,024 × 8 (temperature 0.6, 32K).
  - W5: SWE-rebench에서 repo가 모두 다른 512 task × 8.
- 제한은 각 policy의 RL **학습** 설정을 따른다. 50 step, prompt 4K + response 32K, 생성 timeout 20분, 보상 5분. 50 step을 264 trace에 걸면 42%가 잘린다(평균 turn 51.5 → 43.1).
- 규모 기준: 동시에 정렬되는 범위(job 하나의 step) 안에서 rollout을 복제하지 않는다(1.3절). 리뷰 관점에서 trace가 수천 개 있어야 현실적이라는 사용자 판단도 반영했다.

### 3.5 dataset과 image 확인 결과

- R2E-Gym-Subset: 4,578 task, task마다 image 하나, repo 10개(pandas 1,444, numpy 781, pillow 620, orange3 482, aiohttp 299, tornado 261, scrapy 215, pyramid 189, datalad 179, coveragepy 108). 무작위 20개 image가 모두 Docker Hub에 있었고 압축 0.3~1.4GB였다.
- SWE-Gym image 1~6GB. SWE-rebench image 약 1.3GB.
- TB2: 89 task, prebuilt image 전체 49GB. `task.toml`에 cpus=1, memory 2GB, agent timeout 900초.
- SandboxFusion image 9.9GB.
- 터미널 RL set(LiteCoder 602 MIT, Terminal-Lego-15k, endless-terminals, Nemotron-Terminal-Synthetic)은 Dockerfile만 있다.
- disk 제약 때문에 image 단위로 받고, 그 image를 쓰는 rollout을 모두 돌리고, 지운다. digest를 기록해 나중에 다시 받는다.

### 3.6 264 trace에서 잰 값 (시간 추정과 합성의 입력)

- turn 51.5개, 평균 context 30.6K token, turn당 새 입력 862 token.
- turn당 출력: 보이는 출력 83 + reasoning 약 290 token.
- tool 1.35초/명령. tool 합계 66초 대 LLM 간격 189초.

---

## 4. 정정·철회한 주장 (다시 하지 않기 위해)

| 주장 | 무엇이 틀렸나 |
|---|---|
| S3·S3b의 RL contention | 모두 함께 시작하는 과도기를 쟀다. S3c로 대체 |
| 첫 S8 실행 | 같은 과도기 문제. ramp 4,000초로 다시 실행 |
| kernel wait b=9 | timeout으로 경로가 갈린 run과 빠진 수요로 오염. b=6으로 다시 맞춤 |
| 처음 latency 방식 | 실제로는 CPU 부풀리기였다. work-conserving 재분배로 다시 씀 |
| "실측 trace로 잰 상관" | 단독 trace를 인위적으로 맞춘 조건부 값일 뿐 |
| A6000 1.5~2주 | KV 읽기 비용과 용량을 빠뜨림. 12.7~30.9일 |
| partial rollout은 꼬리 몇 개만 넘긴다 | APRIL은 token의 약 40%를 넘긴다. sandbox를 유지해야 하는 것은 끝나지 않은 rollout뿐 |
| CXL은 싸고 크다 | 귀한 자원이다. C 아이디어의 전제가 무너짐 |

---

## 5. 열린 질문과 다음 단계

**열린 질문**
- 실제 RL 학습에서 sandbox 사이 상관이 얼마나 되는가. 새 trace와 합성으로만 답할 수 있다.
- 192코어 node에서 scheduler 대기가 정말 줄지 않는가. floor와 slope가 2배 다르다. 192코어급 서버에서 직접 재야 확정된다.
- 메모리 대역폭·LLC 간섭(DSec §8.5의 원인)이 runqueue 대기보다 큰가. 지금 모델에는 없다.
- 50 step 제한에서 잘린 꼬리가 결론에 영향을 주는가(Nemotron 3 Super는 최대 200 turn).
- image locality를 쓰면 hand-off에 image·rootfs 이동이 필요해지는가.

**다음 단계 후보**
1. **G의 simulator 시험**: `fleet.py` RL 모드에 critical-path·꼬리 우선순위, VenusRL 수준의 밀도(14배 oversubscription), CXL 예산을 넣는다. 비교 대상은 정적 cpuset, 정적 LS/BE, oversample하고 버리기, OOM 때만 이동. (제안했고 아직 승인 전)
2. trace 수집 S0~S4(A6000): 계측 도구 작성, smoke, pilot, 우리 서버 재생 확인.
3. S8 결과를 RESULTS-fleet.md에 넣고 grid를 commit한다.

---

## 6. 파일 위치

| 경로 | 내용 |
|---|---|
| `experiments/contention/fleet.py` | fleet simulator(numba 유체 모델, `KERNEL_WAIT`, 이동 메커니즘, RL 모드) |
| `experiments/contention/validate.py` | 작은 node 실측과 simulator 비교(`--kernel-wait`) |
| `experiments/contention/pilot_audit.py` | 경로 갈림, 같은 경로 CPU 비율, 수요 계수 |
| `experiments/contention/kernel_wait_fit.py` | pool 크기별 a(P), b 맞추기 |
| `experiments/contention/trace_count.py` | trace 수 부분집합 검사 |
| `experiments/contention/plot_fleet.py` | fleet 그림(`figures/fleet-*.png`) |
| `experiments/contention/grids/` | S, S3b, S3c, S4rl, S5b, S6, S7(commit됨), S8(commit 안 됨) |
| `experiments/contention/.local/report/` | 결과 JSON (git 밖) |
| `experiments/rollout-trace/estimate_time.py` | 수집 시간 추정 |
| `docs/trace-collection-plan.md` | 수집 설계 v3 |
| `docs/rl-trace-survey.md` | 조사 |
| `CLAUDE.md` | 응답 언어(한국어), 작업 범위 규칙 |

관련 commit: 4dcabd06d(fleet simulation), aac9fbd84(trace 수 검사), 8be33ae66(scheduler 대기), 33424ed37(수집 계획·조사), bbce04584(rollout 제한).
