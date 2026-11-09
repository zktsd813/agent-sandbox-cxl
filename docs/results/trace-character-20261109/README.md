# trace 성격 다시 보기와 sandbox 밖 CPU 작업 (2026-11-09)

지금까지 trace는 sandbox당 평균 코어, burst 폭, LLM 대기 같은 수명 전체의 값으로만 봤다([workload-diversity-plan.md](../../workload-diversity-plan.md) 2절, [실험 계획 v2](../../experiment-plan-tail-placement.md) 8.5). 이 문서는 두 가지를 더 본다.
- **sandbox가 무엇에 CPU를 쓰는지**: 명령 종류별로 나눈다. HANDOFF T1의 남은 일이다.
- **sandbox 밖에서 생기는 CPU 작업**: 공개 RL job 기록과 문헌·framework 조사로 본다.

## 0. 결론

1. **sandbox CPU는 몇 개의 test·build 명령에 몰려 있고, 명령 문자열만 보고도 알아볼 수 있다.**
   - 명령 수의 36~45%를 차지하는 탐색(cat, grep, ls 등)은 CPU의 1~4%만 쓴다.
   - test·build·install이 trace CPU의 중앙값 60%(Python RL)와 94%(다언어)다.
   - trace 하나에서 가장 무거운 명령 3개가 CPU의 36%(Python RL)와 80%(다언어)를 쓴다.
   - 그래서 무거운 burst는 tool call이 시작될 때 이미 보인다. 이 경계에서 배치를 정하면 된다는 HANDOFF Q3의 반론이 더 강해진다.
2. **burst 폭은 언어가 정한다.** Python test는 실행 중 1.1코어(pytest 단일 thread)다. 다언어 test는 p90 6.4코어다(gradle, jest, cargo의 병렬 실행).
3. **기록된 RL job에서 reward 평가는 작고 몰리지 않는다.** sweagent/iter2-rl-rollouts, rollout 18,883개다.
   - rollout 시간의 구성: LLM 85.3%, env 생성 6.7%, tool+agent 6.3%, reward 평가 1.8%
   - reward 평가 시간: 중앙값 19초, p90 50초
   - 마지막 명령 뒤에 평가를 붙이면 step이 중앙값 0.7%(p90 5.7%) 길어진다.
   - 동시에 도는 평가는 살아 있는 rollout 150~190개 중 중앙값 2~3개, 최대 17~25개다. rollout마다 끝나는 시각이 달라서 평가가 퍼진다.
   - CPU 양은 무시할 수 없다. 평가 시간이 agent 명령 전체의 vCPU 시간의 0.6배(중앙값)이고, p90은 2.1배다.
4. **기록된 tool+agent 시간의 4분의 1은 sandbox 명령이 아니다.** 같은 trace를 재생한 명령 시간보다 중앙값 1.67배 길다. 명령마다 약 0.26초씩 agent·harness 쪽 시간이 붙는다.
5. **sandbox 밖 CPU 작업 중 크고, 몰리고, 옮길 수 있는 것은 하나뿐이다.** 동기 RL에서 생성이 끝날 때 몰리는 reward·test 실행이다. 그 밖의 것은 다음과 같다.
   - GPU 노드의 CPU 작업(추론 엔진 host 쪽, tokenizer, optimizer offload, checkpoint)은 GPU process에 묶여 있고 대부분 꾸준하다. 직접 해 본 GPU harvest가 고정 분할에서 끝난 것과 같은 그림이다.
   - env 생성은 크게 몰리지만 병목이 CPU보다 image pull, 디스크, network다.
   - browser나 desktop VM 같은 무거운 환경은 꾸준해서, 빠른 이동보다 배치의 문제다.
6. **hand-off motivation으로 보면 이번에도 새 기회는 작다.**
   - sandbox 안의 무거운 CPU는 미리 알 수 있다.
   - sandbox 밖에서 몰리는 reward burst는 이 job에서는 몰리지 않았다.
   - 남은 후보는 "test가 무겁고, 동기 step이고, 평가를 한꺼번에 돌리는" RL 구성의 reward 단계다. rStar2-Agent는 step당 최대 4.5만 번 코드를 실행하고, RollPacker에서는 reward가 학습 시간의 약 22%다(3절).

## 1. sandbox가 CPU를 쓰는 곳: 명령 종류별

`experiments/contention/trace_kinds.py kinds`. 명령 문자열에 정규식을 맞춰 아홉 종류로 나눈다. 순서대로 맞추므로, 편집한 뒤 test를 돌리는 명령은 test로 센다.

| 종류 | 포함하는 것 |
|---|---|
| test | pytest, tox, go test, cargo test, npm·yarn test, ant test, mvn·gradle test, rspec, phpunit 등 |
| install | pip, conda, npm, gem, composer, go mod 등 |
| build | make, cmake, cargo build, go build, mvn, gradle, ant, tsc, gcc 등 |
| run | python 스크립트·`-c`·heredoc, node, ruby, php 등 |
| edit | sed -i, cat >, tee, patch, git apply, 파일을 쓰는 python |
| git | git 명령 |
| explore | cat, grep, ls, find, head, sed -n 등 |
| submit | 제출 명령 |
| other | 위에 맞지 않는 것 |

데이터는 세 묶음이다.
- **Python RL 기록**: 공개 GRPO job의 변환 trace 2,760개, 명령 28만 개. 시간은 assistant timestamp에서 tool timestamp까지다. agent 쪽 시간이 들어 있다.
- **Python RL CPU**: 그중 51개를 8 vCPU microVM에서 하나씩 재생했다. 명령마다 vCPU 실행 시간을 쟀다.
- **다언어 CPU**: SWE-bench Multilingual(Gemini) 264개를 Docker로 DRAM에서 재생했다. cgroup CPU를 20 ms 간격으로 쟀다.

| 종류 | 명령 수 (RL 기록 / RL / 다언어) | tool 시간 | CPU (RL / 다언어) | 실행 중 코어 p50 (RL / 다언어) | 다언어 코어 p90 |
|---|---|---|---|---|---|
| test | 8.6% / 10.5% / 8.1% | 22% / 49% / 63% | 48.8% / 73.4% | 1.09 / 1.68 | 6.38 |
| install | 1.4% / 1.4% / 0.1% | 5% / 23% / 1% | 12.6% / 0.1% | 0.84 / 0.51 | 1.57 |
| build | 0.3% / 0.4% / 5.8% | 0.3% / 0.2% / 19% | 0.3% / 21.5% | 1.34 / 0.52 | 5.09 |
| run | 27.3% / 27.8% / 22.1% | 30% / 20% / 6% | 29.9% / 1.9% | 1.24 / 0.35 | 0.93 |
| edit | 9.4% / 9.4% / 7.6% | 7% / 2% / 1% | 2.5% / 0.2% | 1.31 / 0.35 | 0.75 |
| git | 5.6% / 5.4% / 11.4% | 3% / 0.6% / 1.7% | 0.8% / 0.6% | 1.27 / 0.41 | 1.38 |
| explore | 45.4% / 41.9% / 36.0% | 31% / 3% / 4% | 3.9% / 1.0% | 1.06 / 0.32 | 0.40 |
| submit, other | 1.9% / 3.4% / 8.7% | 1% / 1% / 4% | 1.2% / 1.2% | – | – |

- **탐색은 많지만 가볍다.** 재생에서 탐색 명령은 중앙값 0.02초(RL), 0.11초(다언어, docker exec 경로 포함)다. 기록에서는 0.17초인데, 차이는 agent·harness 쪽 시간이다.
- **Python RL에서는 run도 CPU의 30%를 쓴다.** 재현 스크립트나 `python -c` 확인이 많기 때문이다. 이 종류는 문자열만으로 무거운지 알기 어렵다.
- **다언어는 test와 build가 거의 전부다(95%).** 폭도 넓다. ant test 하나가 30초 동안 CPU 170초를 쓴다.
- **명령 시간의 꼬리.** 다언어 test는 p90 29초이고, p99는 60초 timeout에 걸린다. Python RL test는 p90 5.4초, p99 48초다.

**rollout 안의 위치.** 명령 번호를 10등분해 CPU가 어디서 쓰이는지 봤다.

| 묶음 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| Python RL | 5% | 5% | 6% | 8% | 8% | 12% | 13% | 15% | 11% | 17% |
| 다언어 | 4% | 6% | 13% | 10% | 8% | 9% | 14% | 17% | 15% | 3% |

- CPU는 rollout 뒤쪽으로 기운다. 수정한 뒤 test로 확인하는 흐름이라서다.
- Python RL은 뒤 절반에 68%가 있다. 다언어는 마지막 10%(제출 직전)에서 다시 줄어든다.

**몰림.** trace 하나에서 가장 무거운 명령 하나가 CPU의 중앙값 17%(RL), 40%(다언어)를 쓴다. 상위 3개는 36%, 80%다. test·build·install의 몫은 trace 중앙값 60%(RL), 94%(다언어)다.

## 2. 기록된 RL job의 시간 구성과 reward 평가

`trace_kinds.py eval`. iter2 rewards archive의 `reward_eval_time`을 index의 timing에 붙였다. 23,139개 rollout 중 reward 파일이 있는 18,883개다.

평가는 agent가 끝난 뒤 sandbox 안에서 돈다. `pip install -e`, test 파일 checkout, `git apply` test patch, 지정 test 파일의 pytest, test 파일 되돌리기 순서다(reward 출력의 `set -x` 기록).

| 구성 | p10 | p50 | p90 | p99 | 전체 시간 몫 |
|---|---|---|---|---|---|
| env 생성 | 23초 | 94초 | 286초 | 410초 | 6.7% |
| LLM | 454초 | 1,361초 | 2,407초 | 2,798초 | 85.3% |
| tool+agent | 27초 | 87초 | 192초 | 354초 | 6.3% |
| reward 평가 | 9초 | 19초 | 50초 | 249초 | 1.8% |

- **critical path에 주는 영향.** 각 rollout의 마지막 명령 뒤에 평가를 붙이면 step 길이가 중앙값 +0.7%(p90 +5.7%), 8개 group이 +0.8%(p90 +3.0%) 늘어난다. 이전 critical path 분석(8.5)은 이 단계를 넣지 않았다.
- **동시 실행.** job 두 개의 시간축에서 동시에 도는 평가는 다음과 같다.

  | job | 살아 있는 rollout | 동시 평가 중앙값 | p99 | 최대 |
  |---|---|---|---|---|
  | 1 | 192 | 3 | 11 | 17 |
  | 2 | 154 | 2 | 14 | 25 |

  rollout의 turn 수가 달라 끝나는 시각이 퍼진다. 평가가 한 순간에 몰리지 않는다.
- **CPU 양.** 재생한 Python RL trace 45개에서 평가 시간과 agent 명령 전체의 vCPU 시간을 비교했다. agent vCPU 시간 중앙값 30초, 평가 17초로, 비율은 중앙값 0.60, p90 2.09다. 평가 CPU는 재지 않았다. pytest가 대부분 단일 thread라 1코어 상한으로 본 값이다.
- **기록과 재생의 차이.** 같은 51개 trace에서 기록된 tool+agent 시간은 재생한 명령 시간의 중앙값 1.67배다. 명령마다 중앙값 0.26초가 더 붙는다. 전체로는 기록의 74%만 실제 명령 시간이다. 나머지는 agent 처리, 원격 sandbox 호출 경로, 첫 LLM 호출이다.

## 3. sandbox 밖 CPU 작업 (문헌·framework 조사)

subagent로 조사했고, 1차 출처(논문, 공식 문서, 코드)를 우선했다. "미확인"은 2차 출처뿐인 값이다.

| 작업 | 어디서 | 크기 | 시간 모양 | 출처 |
|---|---|---|---|---|
| 추론 엔진 host 쪽 (API server, EngineCore loop, GPU당 worker) | GPU 노드 | GPU N개에 최소 2+N 물리 코어. 모자라면 처리량과 지연이 크게 나빠진다 | 꾸준함 | [vLLM 최적화 문서](https://docs.vllm.ai/en/latest/configuration/optimization.html) |
| CPU 부족 시 tokenization, kernel launch, broadcast | GPU 노드 | CPU를 충분히 주면 TTFT 1.36~5.40배 단축. GPU당 4~8코어 미만이 위험 | 요청 burst에 따라 | [arXiv 2603.22774](https://arxiv.org/html/2603.22774) |
| 여러 turn 문맥의 재tokenization | API server | prefix cache 적중이 높을수록 TTFT의 10% → 64% | turn마다 | [TokTier 2607.29678](https://arxiv.org/abs/2607.29678) (초록만 확인) |
| agent ↔ LLM proxy, HTTP | 추론 노드 | Miles: 코어 하나가 모든 agent 요청 처리, 연결 100개 상한. 다른 변경과 함께 풀어 시간당 860 → 3,480 trajectory | 꾸준함, 동시성에 비례 | [LMSYS Miles](https://www.lmsys.org/blog/2026-10-22-miles-mi355x-agentic-rl) |
| env 생성 (image pull, container 시작, reset) | CPU cluster | RollArt: iteration의 15%, 실패 iteration에서는 rollout의 78%. DSec: 8,192개 eager pull에 60분 이상. MegaFlow: 단독 1분 → 1,000개 동시 13분 | step 시작에 몰림 | [RollArt](https://arxiv.org/html/2512.22560v1), [DSec](https://arxiv.org/html/2609.22978v1), [MegaFlow](https://arxiv.org/html/2601.07526v2) |
| reward용 코드·test 실행 | GPU cluster의 남는 CPU 또는 별도 CPU cluster | rStar2-Agent: step당 최대 4.5만 번, 평균 0.3초, 노드당 worker 1,024개(로컬 실행은 "CPU를 압도"). RollPacker: reward가 학습 시간의 약 22%(test timeout 30초) | 동기 RL에서 생성이 끝날 때 몰림, 비동기에서는 퍼짐 | [rStar2-Agent](https://arxiv.org/html/2508.20722), [RollPacker](https://arxiv.org/html/2509.21009) |
| math_verify·sympy 검증 | reward worker | 가끔 매우 오래 걸림. verl thread pool에서 timeout이 안 들어 멈춤 | 드문 spike | [verl PR 5839](https://github.com/verl-project/verl/pull/5839) |
| trainer → rollout 가중치 동기화 | GPU 노드 | Kimi K2 30초 미만, AReaL-Hex 4.75~26초(iteration의 1.9%) | step마다 한 번 | [Kimi K2](https://arxiv.org/html/2507.20534), [AReaL-Hex](https://arxiv.org/pdf/2511.00796) |
| CPU optimizer offload (ZeRO-Offload) | GPU 노드의 모든 코어 | 10B Adam step 2.57초 vs GPU 1.0초 (미확인, 강의 자료 값) | optimizer step마다 여러 코어 burst | [ZeRO-Offload](https://arxiv.org/abs/2101.06840) |
| 비동기 checkpoint | GPU 노드 | 학습 정지는 30초 미만, 흔히 약 6초. 직렬화는 뒤에서 | N step마다 | [PyTorch blog](https://pytorch.org/blog/reducing-checkpointing-times/) |
| 검색·retrieval (정확한 최근접 탐색) | retriever | 115 GB corpus에서 종단 지연의 81~89% | 꾸준함 | [arXiv 2511.00739](https://arxiv.org/html/2511.00739v3) |
| browser, computer-use desktop VM | env 노드 | desktop VM은 "CPU를 많이 쓰고 높은 동시성에서 불안정". 수천 개 VM | 꾸준함 + step마다 burst | [ComputerRL](https://arxiv.org/html/2508.14040v2), [UI-TARS-2](https://arxiv.org/abs/2509.02544) |

**"sandbox CPU가 얇다"와 어긋나는 관측**
- [arXiv 2511.00739](https://arxiv.org/abs/2511.00739v2)(Georgia Tech, "CPU-centric perspective")는 batch 추론에서 SWE-agent의 bash·python 실행이 종단 지연의 25~38%였다. H200·Grace 시스템에서는 최대 65%였다. batch 128에서 코어가 모자라 bash 지연이 1.53~1.94배가 됐다.
- 이것은 RL 학습이 아니라 추론이다. 다만 GPU가 빨라질수록 tool 몫이 커진다는 점은 8.5의 ×0.25 분석과 같다.
- Miles(MI355X)에서는 sandbox 쪽이 trajectory 시간의 22%였다. 2 vCPU microVM에서 test가 8.5초 더 걸렸다.

**순위: 크고, 몰리고, GPU process에 묶이지 않은 것**
1. **동기 RL에서 생성이 끝날 때 몰리는 reward·test 실행.** 빠른 CPU 이동에 가장 잘 맞는 후보다. 다만 iter2 기록에서는 몰리지 않았다(2절). 비동기 pipeline(AReaL, SkyRL)도 이 몰림을 퍼뜨린다.
2. **step 시작의 env 생성.** 크고 몰리지만 병목이 I/O와 registry다. hand-off보다 image 공유가 맞다.
3. **browser·desktop VM 환경.** 무겁지만 꾸준해서, 빠른 이동보다 배치의 문제다.
4. **GPU process에 묶인 것.** 추론 엔진 host 쪽, tokenizer, optimizer offload, checkpoint다. 옮길 수 없다. 엔진은 코어를 잃는 순간 느려진다(vLLM 문서, 2603.22774). 고정 분할이 낫다는 직접 실험 결과와 같다.

## 4. 한계

- **분류는 정규식이다.** compound 명령은 맨 앞에서 맞는 종류 하나로 센다.
- **Python RL의 CPU는 51개 trace의 단독 재생 값이다.** 8 vCPU라 폭은 제한 없는 경우에 가깝다.
- **reward 평가 CPU는 재지 않았다.** 시간만 있다. pytest-xdist처럼 병렬로 도는 평가는 1코어 가정보다 클 수 있다.
- **iter2 job 두 개뿐이다.** Qwen3.5-35B-A3B GRPO, Python SWE에 대한 결과다. 동기 step에 wave가 두 개다.
- **조사 결과에는 미확인 값이 있다.** 2차 출처뿐인 값은 표에 표시했다. Chromium 수치, OSWorld 권장 사양, CPU:GPU 비율 전망은 신뢰할 출처를 찾지 못해 표에서 뺐다.

## 5. 재현

```bash
cd experiments/contention
python3 trace_kinds.py kinds --out .local/trace-kinds.json
python3 trace_kinds.py eval --out .local/trace-eval.json
```

원값은 [kinds.json](kinds.json)과 [eval.json](eval.json)에 있다.
