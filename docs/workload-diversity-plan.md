# workload를 넓힌 노드 사이 CPU·LLC 경합 실험 계획 (2026-11-06)

[pool 이동 실측(11-05)](results/pool-mover-20261105/README.md)은 조건 사이 비교로는 공정했다. 같은 대기열을 static, 이동, 공유 pool에 똑같이 넣었기 때문이다. 하지만 부하는 실제에서 가져오지 않고, 경합이 생기도록 골랐다. trace는 한 benchmark, 한 agent, 한 모델이었고, 사용률은 sandbox 수 K로 맞췄다. 그래서 "이런 부하라면 빠른 이동이 얼마나 되찾나"에는 답하지만, "실제 agentic RL에서 이런 부하가 생기나"에는 답하지 못한다.

이 계획은 하나의 "실제 부하"를 가정하지 않는다. 대신 다음 두 질문에 답한다.

> 1. 어떤 workload와 운영 조건에서 노드 사이 CPU 경합이 생기고, 빠른 hand-off가 그것을 되찾는가?
> 2. sandbox끼리의 LLC·메모리 대역폭 경합은 CPU 경합과 따로 있는가, 다른 노드로 옮기면 피할 수 있는가?

**전제**
- GPU가 없다. LLM을 한 번도 부르지 않고, 모든 rollout은 공개 trajectory를 재생한다. RL 학습에서만 얻는 값(serving 지연, step 구조)은 공개 RL rollout(iter2)의 기록에서 가져온다.
- 12시간 안에 끝낸다(11-06 02:00~14:00 UTC). bud05 NUMA 0만 쓰고, 측정 한 번은 30분을 넘기지 않는다.

## 0. 요약

- **부하를 입력이 아니라 출력으로 둔다.** 밀도는 메모리 한도로, sandbox CPU는 framework 기본값(1, 2, 4 vCPU)으로, LLM 대기는 iter2의 RL serving 기록으로 정한다. 사용률은 그 결과다.
- **simulator가 중심이다.** 이미 있는 profile(Python RL F1, 다언어 F3)로 먼저 지도를 그린다. 새로 profile하는 묶음은 두 개다. SWE-bench Verified 48개(F2)와 Terminal-Bench 2.0 2개 모델 × 25 task(F5)다.
- **LLC는 세 단계로 본다.** (L1) 11-05 실측의 느려짐을 runqueue 대기, clock, IPC로 나눈다. 기계 시간이 들지 않는다. (L2) 새 profile에서 sandbox별 LLC 점유와 메모리 대역폭을 잰다. (L3) 두 pool에 L3를 따로 나눠 주고(CAT), 옮기면 LLC 경합도 피하는지 잰다.
- **관문을 앞에 둔다.** Python SWE(F1, F2)로 첫 판단(GA-1)을 약 05:00 UTC에 내린다.
- **기계를 쓰는 시간은 약 5시간이다.** 나머지는 분석과 simulator다.

## 1. 11-05 실험에서 고정했던 것

| 항목 | 11-05 실험 | 실제 범위 | 이 계획 |
|---|---|---|---|
| task | SWE-bench Multilingual 264개 중 48개(넓은 24개를 일부러 넣음). Python 없음 | RL 학습 set은 대부분 Python SWE(SWE-Gym, SWE-rebench, R2E-Gym). terminal, 계산 도구, ML 등은 부하 모양이 다르다 | workload 묶음 축(3절) |
| agent, 모델 | mini-SWE-agent, Gemini 3.5 Flash API. turn당 LLM 대기 중앙값 3.4초 | 학습 중 policy를 vLLM으로 생성. 공개 Qwen3.5 GRPO rollout은 turn당 14.5초 | iter2 RL serving 기록과 그 배율 |
| sandbox CPU | Docker, 제한 없음(15코어까지 burst) | SWE-rebench 0.5, R2E-Gym·SkyRL 1, ROCK 2, ROLL 4, E2B 2, Daytona 1 | 1 / 2 / 4, 제한 없음은 상한으로만 |
| 밀도 | 사용률 41~95%가 되도록 K를 고름 | 메모리가 밀도를 정한다(DSec microVM 코어당 2.7~4.2개, container는 그 약 2배) | 묶음별 메모리 최대치로 계산 |
| 실행 구조 | 끝나면 바로 채우는 정상 상태, 무작위 순서 | 동기 step(함께 시작, 끝에서 빔), group 단위 비동기 | 정상 상태, 동기 |
| 노드 | 15코어 2개 | 192코어급 | 15(실측), 64·192(simulator) |
| LLC | 두 pool이 socket 0의 L3를 같이 씀 | 노드마다 LLC가 따로 있음 | pool마다 L3를 나눠 줌(4절 L3) |
| 반복 | 대부분 1회 | – | 이번에도 1회(7절). 판단에는 반복 차이보다 큰 차이만 쓴다 |

## 2. 지금까지 확인한 것 (11-06)

숫자는 `experiments/contention/workload_mix.py`로 다시 만든다.

### 2.1 sandbox 하나의 부하는 trace에 따라 10배 넘게 다르다

| trace 묶음 | sandbox당 평균 코어 (중앙값, 수명 전체) | 100 ms 최대 폭 중앙값 | tool 시간 비중 | turn당 LLM 대기 | 15코어의 70%를 채우는 sandbox 수 |
|---|---|---|---|---|---|
| Python RL (iter2, Qwen3.5 GRPO, 51개) | 0.022 | 약 1.5 | 2% | 14.5초 | 261개 (코어당 17) |
| 다언어 Ruby·PHP (86개) | 0.08~0.09 | 1.1 | 11~16% | 3.2~3.4초 | 70개 (코어당 4.7) |
| 다언어 C/C++ (38개) | 0.31 | 3.3 | 21% | 3.3초 | 28개 (코어당 1.8) |
| 11-05 실험의 48개 | 0.24 | 6.4 | 17% | 3.5초 | 28개 (코어당 1.9) |
| 다언어 264개 전체 | 0.37 | 10.7 | 21% | 3.4초 | 14개 (코어당 0.9) |
| 다언어 Java·Rust·JS·Go (140개) | 0.51~1.37 | 12~29 | 16~32% | 3.0~4.0초 | 9개 (코어당 0.6) |

- 마지막 열은 묶음 전체 CPU ÷ 전체 시간(sandbox 사이 준비 0.5초 포함)으로 계산했다. Python RL의 최대 폭은 [cap sweep 6절](results/cap-sweep-20261104/README.md#6-python-rl-rollout의-폭)의 값이다.
- iter2의 swerebenchv2 계열도 이 sample에서는 모두 Python repo였다.
- Python RL로 11-05 수준의 사용률을 만들려면 코어당 17개가 필요하다. 메모리 한도로 정해지는 밀도(DSec 운영점 코어당 2.7~4.2개, bud05 NUMA 0 메모리 기준 8~10개)를 넘는다. 실제로 10-30 microVM 실측(2×15코어, 코어당 6.4개, LLM 4배 빠르게)에서도 노드 사용률은 평균 20%였고, 꽉 찬 시간은 0.2% 미만이었다.

### 2.2 노드 사이 관계: 초 단위는 trace와 무관하게 독립, 분 단위는 실행 구조가 정한다

| 실행 | 사용률 | 노드 사이 상관 1초 | 빠른 성분 (30초 평균을 뺌) | 60초 평균 | 노드 합계 분산 ÷ 독립 가정 |
|---|---|---|---|---|---|
| Docker 다언어 48개, 정상 상태 (11-05, K=60~150) | 0.41~0.95 | −0.09~0.10 | 0.06~0.19 | −0.19~0.11 | 0.17~0.84 |
| microVM Python RL, 동기 step (10-30, 2×15코어, ×0.25) | 0.20~0.21 | 0.66~0.72 | 0.05~0.07 | 0.73~0.80 | 5.2~5.7 |
| microVM Python RL, 동기 step (11-02, 4×4코어 + SMT) | 0.22 | 0.77 | 0.10 | 0.96 | 3.0 |
| microVM Python RL, group 단위 비동기 (11-03) | 0.50 | 0.36 | 0.04 | 0.66 (ramp 포함) | 1.3 |

- **burst는 두 trace 묶음 모두에서 노드 사이에 독립이다.** 그래서 배치 때 몰라서 생기는 "한쪽만 꽉 참"은 trace를 바꿔도 생긴다. 얼마나 자주 꽉 차는지는 2.1의 부하 수준이 정한다.
- **동기 step은 노드를 함께 오르내리게 한다.** step 시작에 모두 함께 시작하고 끝에서 비기 때문이다. 11-05 실험은 정상 상태라 이 상관이 없다.
- 11-05 Docker 실행에서 합계 분산이 독립 가정보다 작은 것은 노드가 15코어에서 잘리고, 경합이 sandbox끼리 서로 밀어내기 때문이다.

### 2.3 simulator: 되찾는 비율은 비슷하고, 되찾을 양이 다르다

`fleet.py`, 2노드 × 15코어, CPU 제한 없음, 정상 상태, seed 10개, `PYTHONHASHSEED=0`. 사용률 목표 50 / 70 / 85%.

| 묶음 | 목표 70%의 대기/실행 (static) | hand-off(10 ms)가 되찾는 몫 | 5초 migration이 되찾는 몫 |
|---|---|---|---|
| 다언어 264개 | 0.92 | 69% | 25% |
| 11-05의 48개 | 0.85 | 68% | 30% |
| Java·Rust·JS·Go | 0.81 | 64% | 18% |
| C/C++ | 0.53 | 70% | 34% |
| Ruby·PHP | 0.06 | 62% | 21% |

- 되찾는 몫은 (static − 이동) ÷ (static − 공유 pool)이다. 세 사용률 전체에서 hand-off는 55~74%(Ruby·PHP 41~68%), 5초 migration은 14~39%(Ruby·PHP 5~30%)다.
- `fleet.py`는 배치 난수를 Python `hash()`로 뽑는다. hash는 프로세스마다 달라서, seed 3개일 때는 같은 설정을 다시 돌리면 되찾는 몫이 최대 15%p 바뀌었다. 위 값은 seed 10개이고, `PYTHONHASHSEED`를 0과 1로 바꿔도 9%p 안에서 같았다.
- 되찾을 손실 자체는 묶음에 따라 20배 차이 난다. Ruby·PHP는 사용률 83%에서도 trace slowdown이 1.017이다.
- 동기 step 모드는 같은 trace를 group 크기만큼 복제해 함께 돌린다. 그래서 묶인 순서의 상한이 되고, 이 비교에서 뺐다.
- simulator는 clock 저하와 Docker 제어 경로를 모델하지 않는다. 묶음 사이 상대 비교로만 쓴다.

## 3. 12시간 동안 볼 묶음과 축

| ID | 묶음 | 출처 | 이번에 하는 것 |
|---|---|---|---|
| F1 | Python SWE RL | [sweagent/iter2-rl-rollouts](https://huggingface.co/datasets/sweagent/iter2-rl-rollouts): Qwen3.5-35B-A3B GRPO rollout 23,139개, 완전한 8개 group 2,065개. 로컬 사본 `/Serverless/agent-sandbox-cxl/rl-iter2` | 기존 profile(1 vCPU 440개, 8 vCPU 51개)로 simulator만. 실제 group과 step 구조를 쓴다 |
| F2 | Python SWE, F3과 같은 agent·모델 | [SWE-bench/20260901_mini-v2.4.2_gemini-3-5-flash](https://github.com/SWE-bench/20260901_mini-v2.4.2_gemini-3-5-flash): Verified `.traj.json` 441개, 12개 repo | repo 비율대로 48개를 profile. 지금 harness 그대로 |
| F3 | 다언어 SWE | 지금 쓰는 264개, image가 로컬에 있다 | 기존 profile. L3 실측에 쓴다 |
| F5 | terminal 작업 | [Terminal-Bench 2.0 공식 제출](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard): 제출 75개, trial마다 ATIF `trajectory.json`(단계별 timestamp, keystroke와 대기 시간) | Terminus-2의 open model 2개(GLM-5, DeepSeek-V3.2) × 같은 task 25개. 모델 민감도도 이 둘로 본다 |

빼는 것: F1 image 추가, Multi-SWE-bench(F6), 계산 도구형(F4), ML engineering, microVM 실측, 조건마다 3회 반복.

| 축 | 값 |
|---|---|
| sandbox CPU | 1, 2, 4 vCPU. 제한 없음은 상한으로만 |
| 밀도 | 묶음별 메모리 p90으로 NUMA 0의 쓸 수 있는 메모리(약 110 GiB)를 나눈 수까지. 비교점으로 DSec 운영점(코어당 2.7, 4.2) |
| LLM 대기 | iter2 turn 기록(문맥 token, 출력 token, LLM 시간)에서 출력 token 구간별 분포를 만들어 turn마다 뽑는다. 배율 ×0.25, ×0.5, ×1. API 기록값은 비교용 |
| 실행 구조 | 정상 상태, 동기 step(F1 실제 group, F5 같은 모델 5회) |
| 노드 | 15코어(실측), 64·192코어(simulator) |

**시각의 의미는 묶음마다 확인한다.** mini의 timestamp는 응답을 받고 명령을 파싱한 뒤 기록된다([timing-traces.md](timing-traces.md)). ATIF 단계 timestamp가 LLM 응답 시점인지 명령 실행 후인지는 Harbor·Terminus-2 코드에서 확인한다.

## 4. LLC·메모리 경합

**이미 아는 것.** [메모리 간섭 계획](memory-interference-plan.md)이 microbenchmark로 쟀다.
- stream류 가해 workload가 다른 코어를 채우면, LLC MPKI 1.95인 Python류 피해 proxy가 3~3.7배 느려진다.
- RDT(L3 way, MBA)로 가해를 묶으면 대부분 되찾는다.
- 10-26 실측에서는 runqueue 대기가 0이어도 명령이 1.21배 느렸고, 그중 67%가 CPU 시간 증가였다.

**모르는 것.** 실제 SWE sandbox끼리도 가해·피해가 되는가. 되는 경우, 다른 노드(다른 LLC)로 옮기면 피하는가. 같은 socket 안의 11-05 pool 실험은 두 pool이 L3를 같이 써서 이것을 볼 수 없었다.

| 단계 | 방법 | 기계 시간 |
|---|---|---|
| L1 | 11-05 실측의 명령 CPU 시간 증가(1.19~1.36배)를 clock 몫(4.02 GHz ÷ 바쁜 clock)과 IPC 몫(K=4 기준 IPC ÷ 조건 IPC)으로 나눈다. 대상 CPU의 instructions, cycles, ref-cycles와 MBM은 이미 기록돼 있다 | 0 |
| L2 | F2·F5 profile 때 container마다 resctrl monitor group을 붙여 LLC 점유(`llc_occupancy`)와 메모리 대역폭(`mbm_total_bytes`)을 1초마다 남긴다. host perf에 LLC miss event를 더한다. CPU를 많이 쓰는 sandbox가 LLC도 많이 쓰는지 본다(HANDOFF 2.4절의 위험) | profile에 포함 |
| L3 | 두 pool에 L3를 나눠 준다(CAT, socket 0의 15 way 중 pool마다 7 way). 노드마다 LLC가 따로 있는 상황을 흉내 낸다. pool별 IPC(perf `-C`)와 LLC 점유·대역폭(control group의 monitor)을 재고, static과 이동을 비교한다. 옮긴 container의 IPC가 이동 전후로 어떻게 바뀌는지도 본다 | 실측에 포함 |

L3의 한계:
- pool마다 LLC가 실제 노드의 절반이라 LLC 경합을 크게 잡는다.
- 메모리 대역폭은 같은 memory controller를 쓰므로 옮겨도 나뉘지 않는다.
- resctrl은 지금 기본 group만 있다(11-06 확인). 실험용 group 두 개를 만들고 끝나면 지운다. socket 1(L3 domain 1)과 NUMA 1은 건드리지 않는다.

## 5. 판단 기준 (실행 전에 고정)

**cell마다 재는 것**
- 노드가 꽉 찬 시간 비율, 한쪽만 꽉 찼을 때 생긴 대기의 몫, 그때 다른 노드의 빈 코어
- 잃은 CPU(대기/실행), 공유 pool로 줄어드는 몫, 그중 hand-off(10 ms)와 5초 migration이 되찾는 몫
- 결과 지표: rollout p50·p90, 동기 step 시간, group 완료 시간
- LLC: 명령 CPU 시간 증가 중 IPC 몫, pool별 IPC와 LLC 점유·대역폭

**기준 (제안값)**
- 경합이 있다: 한 노드가 시간의 5% 이상 꽉 차고, 잃은 CPU가 실행의 5% 이상이다.
- hand-off가 의미 있다: static 대비 step 시간 중앙값이나 rollout p90이 5% 이상 줄고, 5초 migration보다 3%p 이상 낫다.
- 현실적인 cell: 밀도가 메모리 한도 안이고, sandbox가 4 vCPU 이하이고, LLM 대기가 iter2 기준 ×0.25 이상이다.
- LLC 경합이 의미 있다: 명령 CPU 시간 증가 중 IPC 몫이 5% 이상이다.
- 옮기면 LLC 경합을 피한다: L3에서 이동이 static 대비 pool IPC 손실을 5% 이상 줄인다.

**관문**
- **GA-1 (05:00, Python SWE):** F1·F2의 현실적인 cell 중 "hand-off가 의미 있다"를 넘는 곳이 없으면, RL 학습의 주류인 Python SWE에서는 CPU 경합이 hand-off의 동기가 되지 않는다고 결론 낸다. 이후는 "어떤 다른 workload에서 생기나"로 범위를 바꾼다.
- **GA-2 (08:00, 전체):** 어떤 묶음이나 혼합의 현실적인 cell에서도 CPU 기준을 넘지 못하면, CPU 경합 motivation을 접는다. LLC 기준도 넘지 못하면 [GPU 노드 harvest](gpu-node-harvest-design.md)와 메모리 쪽 동기에 집중한다.

## 6. 12시간 일정 (11-06, UTC)

기계 열은 bud05 NUMA 0을 쓰는 시간이다. 각 측정에는 상한이 있다. 상한에 닿으면 그때까지 끝난 것만 쓴다.

| 시각 | 작업 | 기계 |
|---|---|---|
| 02:00~02:30 | (이동 규칙 K=150 balance가 도는 중) F2 trajectory 441개 받기, 48개 고르기, image 크기 조회. F5 ATIF 2개 모델 받기, task image 크기 조회. profile에 resctrl monitor와 LLC event를 붙이는 코드 | 이전 실험 |
| 02:30~03:00 | K=150 ratio·balance 분석. 95%에서 이동이 안 들은 것이 규칙 탓이었는지 답한다 | – |
| 03:00~04:30 | **F2 profile.** 48개를 8코어 cpuset 세 개(CPU 2-9, 10-17, 18-25)에서 셋씩 단독 재생한다(LLM 대기 없이 명령 연속). image는 받으면서 돌린다. CPU 20 ms, LLC 점유·대역폭 1초. 상한 90분 | 90분 |
| 03:00~04:30 | (동시에, 분석만) **L1** 11-05 느려짐 분해. **P0** F1·F3로 simulator 지도 | – |
| 04:30~05:00 | **GA-1.** F1·F2 지도에 기준을 적용한다 | – |
| 04:30~06:00 | **F5 profile.** tmux 실행기(04:30까지 작성): 기록된 상대 시각에 keystroke를 보내고, 기록된 대기만큼 기다린다. 명령이 다음 LLM 대기 동안 계속 도는 것을 그대로 둔다. 2개 모델 × 25 task를 셋씩. 상한 80분 | 80분 |
| 06:00~06:30 | 모델 민감도(F5 두 모델, F1 대 F2 명령 종류별 CPU). L2 정리 | – |
| 06:30~08:00 | **보정과 전체 지도.** simulator를 11-05 Docker static과 10-30 microVM 동기 step에 다시 맞춘 뒤, 축 전체와 혼합(F1만, F1 + F3·F5 10%·30%)으로 지도를 그린다. **GA-2** | – |
| 08:00~10:00 | **실측(L3 포함).** 두 pool에 CAT를 건다. (a) 가장 현실적인 cell(GA-1 지도에서 고름): static 한 번. 경합이 기준을 넘을 때만 balance와 공유 pool을 더한다. (b) 무거운 cell(F3 48개, K=90): static, balance 각 20분. 11-05의 같은 조건(L3 공유)과 비교한다. 조건마다 상한 25분 | 약 100분 |
| 10:00~11:30 | 분석, 결과 문서, pool 이동 문서의 결론 범위 정정, 로컬 커밋 | – |
| 11:30~14:00 | 여유. 상한에 걸려 못 한 profile을 채우거나, (a)를 한 번 더 돌린다 | 필요할 때만 |

**디스크.** Docker 저장소(`/var/lib/docker`)는 `/Serverless`와 같은 nvme에 있고 249 GB가 남았다. 받은 image 이름을 목록으로 남기고, 그 목록에 있는 것만 지운다. OpenWhisk image, container, volume은 건드리지 않는다. 한 번에 받는 양은 100 GB 이하로 둔다.

## 7. 한계

- **RL serving 지연은 iter2 하나에서 온다.** 모델(Qwen3.5-35B-A3B), GPU, batch가 다르면 대기가 달라진다. 그래서 결론은 LLM 대기 배율(×0.25~×1)의 함수로 쓴다.
- **F1을 뺀 묶음은 평가용 agent·모델의 행동이다.** RL policy가 명령을 다르게 고르는지는 모델 민감도로만 간접적으로 본다. 이번에는 모델 두 개뿐이다.
- **반복이 없다.** 실측은 조건마다 1회다. 11-05 K=120에서 같은 조건 두 번의 차이는 대기 1~5%, CPU 가중 2~7%, p99 21~26%였다. 이보다 작은 차이는 판단에 쓰지 않는다.
- **F5는 실행 경로가 다르다.** keystroke를 tmux에 보내므로, 명령마다 bash를 새로 띄우는 지금 harness와 다르다.
- **L3는 LLC를 반으로 나눈 흉내다.** 실제 노드보다 LLC 경합을 크게 잡고, 메모리 대역폭은 나누지 못한다.
- **혼합 비율은 시나리오다.** 운영 fleet에서 묶음이 얼마씩 섞이는지는 공개된 자료가 없다.

## 8. 정해 주실 것

- 5절 기준값(5%, 5%, 3%p, LLC 5%)
- L3에서 socket 0의 L3 way를 두 pool에 나눠 주는 것(실험 중 약 2시간, 끝나면 원래대로)
