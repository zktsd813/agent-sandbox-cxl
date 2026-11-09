# trace 성격 다시 보기, env 생성, sandbox 밖 CPU 작업 (2026-11-09)

지금까지 trace는 sandbox당 평균 코어, burst 폭, LLM 대기 같은 수명 전체의 값으로만 봤다([workload-diversity-plan.md](../../workload-diversity-plan.md) 2절, [실험 계획 v2](../../experiment-plan-tail-placement.md) 8.5). 이 문서는 세 가지를 더 본다.
- **sandbox가 무엇에 CPU를 쓰는지**: 명령 종류별로 나눈다. HANDOFF T1의 남은 일이다(1절). reward 평가(채점)도 직접 다시 돌린다(2절).
- **env 생성이 왜 느리고 step에 얼마나 들어가는지**: 기록된 생성 시간을 쪼개고, 생성 단계별 CPU를 재고, DSec의 방식과 비교한다(3절).
- **sandbox 밖에서 생기는 CPU 작업**: 공개 RL job 기록과 문헌·framework 조사로 본다(4절).

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
   - **직접 재실행해도 평가 CPU는 작다(2.1절).** 45개를 microVM에서 다시 돌렸고, 43개가 기록과 같은 통과·실패 수를 냈다.
     - 평가 CPU는 중앙값 4.0초(p90 20초)로, agent CPU의 0.12배(p90 0.59배)다. agent와 평가를 합친 sandbox CPU의 11%다.
     - 그 절반 이상(56%)은 `pip install -e`다.
     - 45개 중 11개는 코어를 주는 만큼 넓게 쓴다(peak 4코어 이상). C 확장 compile과 pytest-xdist 때문이다.
     - 처음에 시간 기록으로 추정한 "agent CPU의 0.6배"는 과대평가였다.
4. **기록된 tool+agent 시간의 4분의 1은 sandbox 명령이 아니다.** 같은 trace를 재생한 명령 시간보다 중앙값 1.67배 길다. 명령마다 약 0.26초씩 agent·harness 쪽 시간이 붙는다.
5. **env 생성은 기록된 job에서 image가 아니라 생성 대기열이 정했고, 병목은 아니다(3절).**
   - 기록의 생성 시간(중앙값 94초)은 Azure 위 Kubernetes pod 경로의 값이다. 우리 microVM은 만들어 둔 image 디스크로 0.28초에 뜬다.
   - 생성 한 번은 20~30초다. 같은 wave에서 2초 늦게 요청한 것은 147초가 걸린다. image 종류·크기·재사용과 무관하고, 앞 sandbox가 끝나기를 기다리는 것도 아니다.
   - critical path 위에 있지만 가장 늦은 rollout의 4~6%다. 모두 1초로 줄여도 step은 약 3%(p90 8%) 준다. 추론이 4배 빠르면 group 기준 약 12%다.
   - image가 노드에 없으면 image가 시간과 CPU를 정한다(펼치기에 압축 GiB당 약 15 CPU초). 다만 rollout은 풀린 image의 2%(p90 4%)만 읽는다.
   - DSec도 생성을 critical path로 보고, EROFS image를 3FS에서 lazy로 읽는다(실행 중 4~13%). 8,192개 burst에서 eager pull은 60분 이상, on-demand는 35분이었고, CPU 차이는 거의 없었다.
6. **sandbox 밖 CPU 작업 중 크고, 몰리고, 옮길 수 있는 것은 하나뿐이다.** 동기 RL에서 생성이 끝날 때 몰리는 reward·test 실행이다. 그 밖의 것은 다음과 같다.
   - GPU 노드의 CPU 작업(추론 엔진 host 쪽, tokenizer, optimizer offload, checkpoint)은 GPU process에 묶여 있고 대부분 꾸준하다. 직접 해 본 GPU harvest가 고정 분할에서 끝난 것과 같은 그림이다.
   - env 생성은 step 시작에 몰리지만, 푸는 방법은 이동이 아니라 image 저장 방식과 warm pool이다(3절).
   - browser나 desktop VM 같은 무거운 환경은 꾸준해서, 빠른 이동보다 배치의 문제다.
7. **hand-off motivation으로 보면 이번에도 새 기회는 작다.**
   - sandbox 안의 무거운 CPU는 미리 알 수 있다.
   - sandbox 밖에서 몰리는 reward burst는 이 job에서는 몰리지 않았다.
   - env 생성도 이동이 아니라 image 저장 방식과 warm pool의 문제다.
   - 남은 후보는 "test가 무겁고, 동기 step이고, 평가를 한꺼번에 돌리는" RL 구성의 reward 단계다. rStar2-Agent는 step당 최대 4.5만 번 코드를 실행하고, RollPacker에서는 reward가 학습 시간의 약 22%다(4절).

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
- **CPU 양(시간 기록으로 본 첫 추정, 2.1에서 직접 재서 바로잡음).** 재생한 Python RL trace 45개에서 평가 시간과 agent 명령 전체의 vCPU 시간을 비교했다. agent vCPU 시간 중앙값 30초, 평가 17초로, 비율은 중앙값 0.60, p90 2.09다. 평가 CPU는 재지 않았다. pytest가 대부분 단일 thread라 1코어 상한으로 본 값이다.
- **기록과 재생의 차이.** 같은 51개 trace에서 기록된 tool+agent 시간은 재생한 명령 시간의 중앙값 1.67배다. 명령마다 중앙값 0.26초가 더 붙는다. 전체로는 기록의 74%만 실제 명령 시간이다. 나머지는 agent 처리, 원격 sandbox 호출 경로, 첫 LLM 호출이다.

### 2.1 채점을 직접 다시 돌림

`experiments/contention/reward_eval.py`. 채점 스크립트를 이렇게 다시 만들었다.
- 명령은 rewards archive의 `set -x` 기록에서 가져왔다.
- 기록에는 test patch 내용이 heredoc이라 빠져 있다. 이것은 raw 기록의 `instance.test_patch`로 채웠다.
- 깨끗한 image에서 agent의 최종 patch(`model_patch`)를 먼저 적용했다.

그 뒤 명령 세 개를 8 vCPU microVM에서 하나씩 실행했다(`sandbox.py solo`). 대상은 agent 명령을 이미 재생한 51개 trace였다.
1. patch 적용
2. 설치(test 파일 checkout 전까지의 줄)
3. test patch와 test 실행

45개를 돌렸고, 6개는 기록된 출력이 잘려 있거나 reward 기록이 없어서 뺐다. 원값은 [reward-eval.json](reward-eval.json)에 있다.

| 항목 | 값 |
|---|---|
| 기록과 같은 통과·실패 수 | 45개 중 43개. marko는 요약 줄을 못 읽었고, onnx는 1개가 실패로 바뀌었다 |
| 채점 시간 (우리 재실행) | 중앙값 7.0초, p90 27.5초. 설치 2.7초(p90 21.8), test 1.4초(p90 9.4) |
| 채점 시간 (기록) | 중앙값 17초, p90 40초. 같은 trace의 우리 시간은 기록의 중앙값 0.38배다 |
| 채점 vCPU | p10 0.7초, p50 4.0초, p90 20.1초 |
| 실행 중 코어 | 설치 0.60, test 0.91 (중앙값) |
| 설치의 몫 | 채점 CPU의 56% |
| agent CPU 대비 | 중앙값 0.12배, p90 0.59배. agent + 채점 sandbox CPU의 11% |
| peak (100 ms) | 중앙값 1.05코어, p90 7.8코어. 45개 중 11개가 4코어를 넘는다 |

- **기록 시간이 더 길다.** 기록된 채점은 원격 sandbox 호출과 그 job의 부하를 포함한다. 반대로 network에서 package를 받는 설치(pennylane, pyatv, oceanparcels)는 이 서버에서 더 오래 걸렸다.
- **넓은 채점은 코어를 주는 만큼 쓴다.**
  - yt는 Cython 확장을 OpenMP로 compile한다.
  - vyper는 pytest-xdist가 vCPU 수만큼(8개) worker를 띄운다.
  - cf-python, dbt 등은 wheel을 build한다.
  - 1 vCPU sandbox였다면 이들은 더 길고 좁게 돌았을 것이다.
- **결론**: 채점은 sandbox CPU를 약 11% 늘린다. 시간 기록만 보고 0.6배로 추정한 것보다 훨씬 작다. 지금까지의 "sandbox CPU가 얇다"는 결론은 바뀌지 않는다.

## 3. env 생성

`trace_kinds.py env`(분포, 대기열 검정), `critical_path.py`(env 생성을 줄였을 때의 step), `env_cost.py`(생성 단계별 CPU, image 공유, 실제로 읽는 양). 원값은 [env.json](env.json), [env-cost.json](env-cost.json), [env-share.json](env-share.json)에 있다.

### 3.1 무엇을 재는 값인가

- **기록의 `env_creation_time`은 sandbox 생성을 요청한 뒤 준비될 때까지의 시간이다.** 숫자 하나뿐이고 단계별 분해는 없다. p10 23초, p50 94초, p90 277초다.
- **이 시간은 기록된 job의 sandbox 경로에서 나온 값이다.** 우리 microVM의 값이 아니다.
  - sandbox는 Azure 위 Kubernetes pod로 보인다. dataset card는 채점 환경을 "azure-modal sandbox"라고만 적었다.
  - tool 출력에 kernel `5.15.0-1102-azure`, `KUBERNETES_SERVICE_HOST`, 호스트 이름 `sandbox-<8hex>`가 보인다.
  - host kernel을 공유하는 container다. pod 배치, container 시작, agent 연결이 들어 있을 것이다. 단계마다 얼마인지는 기록에 없다.
- **우리 microVM은 1초가 안 걸린다.** image를 ext4 디스크로 한 번 바꿔 두면(3.4) 매번 하는 일은 아래뿐이다.

| 단계 | 시간 | 근거 |
|---|---|---|
| job 디스크와 쓰기 디스크 준비 | step(192개)당 7~10초. VM당 약 0.05초 | 10-27 동기 step run의 `prepare_s` |
| Firecracker 시작 → guest agent 준비 | 중앙값 0.28초, p90 0.29초, 최대 0.71초 | 11-06 pilot의 VM 4,293개. 동시 24~192개에서 같다 |
| (비교) image가 있는 Docker container 시작 | 0.27~0.39초 | 3.4 |

### 3.2 왜 느린가: image가 아니라 생성 대기열

**image가 시간을 정하지 않는다.** 기록된 job의 image 2,430개는 run 안에서 대부분 한 번(GRPO group 하나, 8회)씩만 쓰였다.

| 기준 | env 생성 중앙값 | 45초 안에 준비된 비율 |
|---|---|---|
| image 종류 aweaiteam(scaleswe) / swerebench / swerebenchv2 | 90 / 94 / 97초 | 42 / 44 / 43% |
| 압축 크기(47개, 순위 상관) | −0.15 | – |
| run 안에서 그 image를 처음 쓰는가 / 이후 | 98 / 93초 | – |
| 같은 image를 함께 시작한 group 8개 안의 차이 | p50 84초, p90 259초 | – |

**분포가 세 덩어리로 갈린다.** 10초 단위 histogram에서 20~30초에 큰 봉우리(27%)가 있고, 90~130초에 두 번째 봉우리, 140~430초에 긴 꼬리가 있다. 450~460초에 따로 114개가 있다(주로 step 세 개의 첫 wave다).

| 묶음 | 개수 | 중앙값 | 45초 미만 | 80~140초 | 140초 이상 |
|---|---|---|---|---|---|
| step 첫 wave | 12,117 | 32초 | 61% | 28% | 8% |
| 두 번째 wave | 9,898 | 125초 | 18% | 33% | 44% |
| run 1 (08-14) | 8,502 | 33초 | 58% | 16% | 21% |
| run 2 (08-15) | 14,637 | 104초 | 35% | 37% | 25% |

**두 번째 wave가 6~8배 느리다.** wave 100개 이상인 step에서 준비 완료 곡선은 다음과 같다.

| wave | 개수 | 처음 준비 | 10% | 50% | 90% | 마지막 | 10~90% 준비 속도 |
|---|---|---|---|---|---|---|---|
| step 첫 wave (53개) | 232 | 19초 | 24초 | 30초 | 49초 | 174초 | 초당 7.0개 |
| 두 번째 wave (49개) | 210 | 22초 | 74초 | 183초 | 309초 | 408초 | 초당 0.9개 |

두 번째 wave는 첫 wave가 시작한 지 752초 뒤에 요청된다. 그때 첫 wave 233개 중 167개가 아직 돌고 있다.

**요청이 조금만 늦어도 늦게 준비된다.** wave 하나는 약 230개를 2초 안에 요청한다. 요청 시각에 따라 다음과 같이 갈린다.

| wave 시작 뒤 요청 시각 | 0~0.5초 | 0.5~1초 | 1~1.4초 | 1.4~2초 | 2초 이후 |
|---|---|---|---|---|---|
| env 생성 중앙값 | 39초 | 46초 | 82초 | 95초 | 147초 |

**자리가 비기를 기다리는 것은 아니다.** sandbox 수에 고정 상한이 있다면, 두 번째 wave는 앞 sandbox가 끝나는 만큼만 준비돼야 한다. 그런데 두 번째 wave 49개에서 결과가 달랐다.
- 준비 속도가 앞 sandbox가 끝나는 속도의 중앙값 9.1배(p10 3.8배)였다.
- 그 사이 준비된 채 돌고 있는 sandbox 수는 49개 모두에서 늘었다.

**준비가 몰려서 온다.** 예를 들어 run 2 step 9의 두 번째 wave에서 15초마다 준비된 개수는 다음과 같다.

| 15초 구간 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| 준비된 개수 | 0 | 32 | 8 | 6 | 0 | 6 | 76 | 55 | 34 | 4 |

멈춰 있다가 15초에 70~90개가 한꺼번에 준비된다. run 2에서 살펴본 wave 6개가 모두 이 모양이었다. run 1의 두 번째 wave는 조금씩 꾸준히 준비됐다.

**해석**
- **생성 한 번의 실제 길이는 20~30초다.** 비어 있는 cluster에서 먼저 요청한 것들의 값이다.
- **나머지는 생성 경로의 대기열에서 기다린 시간이다.** 이 대기열은 한 번에 처리하는 양이 정해져 있고, 묶음 단위로 풀린다. 이미 sandbox가 많이 돌고 있으면 더 느리게 빠진다.
- **provider 안에서 무엇이 막혔는지는 기록에 없다.** 동시 생성 상한이나 속도 제한일 수도 있고, node 추가(autoscaling) 대기일 수도 있다. 멈춰 있다가 한꺼번에 준비되는 모양은 pod가 node 자리를 기다리다가 node가 붙을 때 함께 뜨는 경우와 맞는다. 다만 이것은 추정이다.

### 3.3 critical path 위에 있지만 병목은 아니다

모든 rollout은 env 생성으로 시작하므로 step의 critical path에 그대로 들어간다. 다만 가장 늦은 rollout에서 LLM이 67%(step)~90%(group)이고, env 생성은 4~6%다.

모든 rollout의 env 생성을 25초(빠른 봉우리)나 1초(우리 microVM, warm pool)로 바꿔 단위 길이를 다시 계산했다. 나머지 시간은 기록대로 두었다.

| 단위 | 가장 늦은 rollout에서 env 생성의 몫 | 25초로 | 1초로 |
|---|---|---|---|
| step, LLM ×1 (53개) | 5.5% (p90 11.2%) | −2.7% (p90 −7.2%) | −3.4% (p90 −7.8%) |
| group 8개, LLM ×1 (2,065개) | 3.7% (p90 13.8%) | −2.3% (p90 −10.5%) | −3.5% (p90 −12.3%) |
| step 첫 wave, LLM ×0.25 | 8.4% (p90 33.3%) | −4.4% (p90 −18.6%) | −6.4% (p90 −20.7%) |
| group 8개, LLM ×0.25 | 12.4% (p90 39.6%) | −8.5% (p90 −29.6%) | −11.6% (p90 −33.5%) |

- 지금 추론 속도에서는 env 생성을 없애도 step이 약 3% 준다. 추론이 4배 빨라지면 group 기준 약 12%(p90 34%)다.
- step 길이의 22%는 두 번째 wave가 늦게 요청되는 시간이다([실험 계획 v2](../../experiment-plan-tail-placement.md) 8.5). 이것은 env 생성과 별개라 위 계산에서 그대로다.

### 3.4 image 비용: 노드에 없을 때

**생성 단계별 CPU (직접 측정).** CPU 26-31에서 쟀다.
- layer는 Docker Hub에서 curl로 하나씩 받았다.
- `tar -xzf`로 차례로 펼쳤다(gunzip + untar).
- microVM 디스크를 만들고(`mkfs.ext4 -d`), 한 번 복사했다.
- image가 이미 있을 때는 `docker run --rm IMAGE true`의 dockerd·containerd CPU를 쟀다.

| image (압축 / 풀린 크기) | pull 시간 / CPU | 펼치기 시간 / CPU | 압축 GiB당 CPU | ext4 만들기 / 복사 CPU |
|---|---|---|---|---|
| scaleswe eliot (0.41 / 1.16 GB) | 24초 / 0.7초 | 5.9초 / 7.0초 | 17초 | 1.9초 / 0.5초 |
| scaleswe geopandas (0.99 / 2.43 GB) | 58초 / 1.5초 | 11.9초 / 14.0초 | 14초 | 3.2초 / 1.0초 |
| swerebenchv2 qiskit (1.89 / 5.54 GB) | 667초 / 8.5초 | 26.3초 / 31.2초 | 17초 | (mkfs 실패, 재지 못함) |
| image가 이미 있을 때 container 시작 (3개 × 5회) | – | 0.27~0.39초 / daemon 0.11~0.16초 | – | – |

- **pull은 시간은 길지만 CPU는 적다.** 이 서버에서 Docker Hub는 약 3~18 MB/s다.
- **펼치기는 CPU를 쓴다.** 압축 GiB당 약 15 CPU초, 실행 중 1.2코어다. gzip 해제가 단일 thread라서다.
- **크기 중앙값 image(0.99 GiB)를 펼치면 CPU 14초다.** 같은 trace의 agent 명령 vCPU 중앙값(30초)의 절반이고, 채점 CPU(4초)의 3배다.
- **image가 노드에 있으면 비용이 거의 없다.** 0.1초 CPU다.

**image를 sandbox마다 새로 받으면 image가 생성 시간과 CPU를 정한다.** 이 job의 생성 시간이 크기와 무관했으므로, image는 미리 받아 두었거나 provider가 cache했을 것이다. 노드에 없는 경우 줄일 여지는 두 가지다.

| 항목 | 값 |
|---|---|
| layer 공유 (선택한 47개) | image 크기 합 44.5 GiB, 서로 다른 layer 28.2 GiB(63%) |
| image 하나 중 다른 image와 겹치지 않는 몫 | p10 35%, p50 52%, p90 100%(scaleswe는 거의 전부 고유) |
| rollout 동안 guest가 image 디스크에서 실제로 읽은 양 (40개, 11-06 pilot) | 중앙값 59 MiB(p90 129, 최대 510) |
| 풀린 image 대비 | 중앙값 2.0%, p90 4.0%, 최대 17% |

- layer 공유는 받을 양을 3분의 1쯤 줄인다.
- lazy loading은 받고 풀 바이트를 25~50배 줄인다. 읽은 양은 agent 명령만의 값이고, 채점의 `pip install -e`는 더 읽는다.

**CPU 관점의 정리**
- **image가 노드에 없으면 env 생성이 sandbox 수명에서 가장 큰 CPU 덩어리 중 하나다.** 0.5~1분 안에 몰려서 쓴다.
- **동기 step에서는 wave 하나가 2초 안에 230개를 요청한다.** 노드마다 image를 새로 풀어야 한다면 230 × 약 15 CPU초 ≈ 3,500 CPU초다. 30~50초 안에 70~115코어가 필요한 burst다. lazy loading이면 바이트에 비례한다고 볼 때 70~140 CPU초다.
- **image가 노드에 있으면 이 burst는 사라진다.** 그래서 env 생성 CPU가 문제인지는 image를 어디에 두고 언제 받는지(locality, lazy loading)에 달려 있다. 실험 계획 v2의 M1(locality)과 같은 축이다.

### 3.5 DSec와 다른 시스템은 env 생성을 어떻게 하나

subagent로 조사했고, DSec 8.2절의 수치와 CPU 문장은 원문에서 다시 확인했다.

**DSec** ([arXiv 2609.22978](https://arxiv.org/html/2609.22978v1))
- **생성은 critical path로 본다.**
  - 규모: scale unit 하나에 노드 약 160대, 동시 최대 약 38만 개, 생성 초당 5,000개 이상(DSec 2.4절).
  - job 하나가 sandbox를 최대 32K개 요청하고, 요청이 짧은 시간에 몰린다. 환경이 준비돼야 batch가 쓸 수 있기 때문이다(4.1절).
- **image가 많다.** 한 주에 container base image 11,266개, workspace 102,171개, 82.8 TB였다(4.2절). image를 미리 받아 두는 것은 "비용을 앞으로 옮길 뿐 없애지 않는다"고 본다.
- **image를 lazy로 읽는다.**
  - OCI image를 미리 EROFS로 바꿔 둔다. metadata만 로컬 디스크에 두고, data는 분산 파일시스템 3FS(RDMA)에서 필요할 때 FUSE로 읽는다.
  - 쓰기는 로컬 overlayfs로 간다. registry와 P2P 배포 층은 따로 두지 않는다.
  - microVM은 OverlayBD(ublk, 256 KiB 단위 + 로컬 cache)를 쓴다.
- **실행 중 읽는 양은 image의 4.2~13.3%다**(언어별, Python 6.0%).
- **8,192 container burst(노드 10대)**
  - on-demand는 약 35분에 끝난다. image가 모두 로컬에 있을 때와 같다.
  - eager pull은 60분 이상(1.71배)이다.
  - 노드당 디스크 쓰기는 eager 1,600 GB 이상, on-demand 약 700 GB, 로컬 약 600 GB다.
  - 원문: "CPU and memory utilization show negligible differences across configurations and are omitted."
- **workspace를 tar.gz 대신 EROFS layer로 두면** 79분 → 45분이고, 디스크 쓰기가 5.5배 적다.
- **sandbox 하나의 생성 지연은 보고하지 않는다.** container와 microVM에는 warm pool이 없다. snapshot은 선점(pause/resume)에만 쓴다.

**다른 시스템**

| 시스템 | 방식 | 수치 |
|---|---|---|
| [GKE Agent Sandbox](https://cloud.google.com/blog/products/containers-kubernetes/accelerate-agentic-rl-with-gke-agent-sandbox) | gVisor, Image Streaming(lazy), warm pool | 첫 명령까지 45~85초 → 1~9초, 최악 7.5분 → 10초 미만. R2E image 4,578개 |
| [Modal](https://modal.com/blog/vm-sandbox-filesystem) | lazy FUSE image | 시작 중앙값 0.5초 미만 |
| [E2B](https://e2b.dev/docs/template/how-it-works) | Firecracker snapshot template | 약 80 ms. 생성 속도 제한은 요금제별 초당 1~5개 |
| [AWS Lambda (ATC'23)](https://arxiv.org/abs/2305.13162) | ext4를 512 KiB chunk로 나눠 lazy, 노드·AZ cache | 고객 하나에 초당 최대 15,000 container |
| [DeepSWE](https://www.together.ai/blog/deepswe) | SWE-bench image를 노드에 미리 받음 | 노드당 NVMe 6 TB 이상 |
| [SWE-MiniSandbox](https://arxiv.org/html/2602.11210) | (Docker 기준선) | 환경 준비 평균 89~91초. 병렬 64 → 320개에서 67 → 132초 |
| [SWE-smith](https://arxiv.org/html/2504.21798) | task가 아니라 repo마다 image 하나 | 50k task가 295 GB(SWE-Gym 6 TB) |

**우리 결과와 맞춰 보면**
- **DSec도 env 생성을 critical path로 본다.** 다만 푸는 방식은 sandbox 이동이 아니라 image 저장 방식(lazy, 공유 저장소)이다.
- **DSec에서 eager pull의 비용은 시간과 디스크 쓰기였다.** CPU 차이는 보고되지 않았다. 3.4의 "image를 새로 풀면 큰 CPU 덩어리"는 gzip layer를 노드마다 푸는 경우의 값이다. 그 비용이 학습 시간에 보일지는 따로 확인해야 한다.
- **실행 중 읽는 비율은 우리 측정(2%)이 DSec(Python 6%)보다 작다.** 우리는 agent 명령만 쟀고 test·install은 뺐다.
- **기록된 job의 느린 생성은 대기열이다(3.2).** image 저장 방식만으로는 줄지 않을 수 있다. warm pool이 이 대기열을 직접 없앤다(GKE 45~85초 → 1~9초).
- **CXL과의 접점은 hand-off가 아니라 image 공유다.** DSec의 3FS 자리에 CXL 공유 메모리를 두는 방식이다([load balancing 조사](../../load-balancing-survey.md)). 이 경우 이득은 lazy read 지연과 노드 사이 page cache 중복 제거다.

## 4. sandbox 밖 CPU 작업 (문헌·framework 조사)

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
2. **step 시작의 env 생성.** 크고 몰리지만, 기록된 job에서는 생성 대기열이 시간을 정했고 step의 약 3%다(3절). image가 노드에 없을 때의 비용은 I/O, registry, 펼치기 CPU다. hand-off보다 image 공유와 warm pool이 맞다.
3. **browser·desktop VM 환경.** 무겁지만 꾸준해서, 빠른 이동보다 배치의 문제다.
4. **GPU process에 묶인 것.** 추론 엔진 host 쪽, tokenizer, optimizer offload, checkpoint다. 옮길 수 없다. 엔진은 코어를 잃는 순간 느려진다(vLLM 문서, 2603.22774). 고정 분할이 낫다는 직접 실험 결과와 같다.

## 5. 한계

- **분류는 정규식이다.** compound 명령은 맨 앞에서 맞는 종류 하나로 센다.
- **Python RL의 CPU는 51개 trace의 단독 재생 값이다.** 8 vCPU라 폭은 제한 없는 경우에 가깝다.
- **채점 재실행은 깨끗한 image에 agent patch를 적용한 상태에서 했다.** 기록된 job처럼 agent가 쓰던 sandbox에서 이어 돌린 것이 아니다. 8 vCPU라 xdist·compile이 넓게 퍼졌다.
- **env 생성 CPU는 image 3개, 이 서버의 curl·tar 경로로만 쟀다.** containerd unpack, lazy loading, 원격 provider의 내부 비용은 다를 수 있다.
- **env 생성의 원인은 모양으로만 추정했다.** 기록에는 생성 시간 숫자 하나뿐이고 provider 내부 단계가 없다. "대기열"은 요청 순서·준비 곡선과 맞는 설명이지, 측정한 단계가 아니다.
- **image에서 읽은 양은 40개 trace의 agent 명령만 쟀다.** 채점, boot 뒤 page cache 상태, readahead 설정에 따라 달라진다. layer 공유는 선택한 47개 image(여러 step에서 고름)의 값이다.
- **iter2 job 두 개뿐이다.** Qwen3.5-35B-A3B GRPO, Python SWE에 대한 결과다. 동기 step에 wave가 두 개다.
- **조사 결과에는 미확인 값이 있다.** 2차 출처뿐인 값은 표에 표시했다. Chromium 수치, OSWorld 권장 사양, CPU:GPU 비율 전망은 신뢰할 출처를 찾지 못해 표에서 뺐다.

## 6. 재현

```bash
cd experiments/contention
python3 trace_kinds.py kinds --out .local/trace-kinds.json
python3 trace_kinds.py eval --out .local/trace-eval.json
python3 trace_kinds.py env --out .local/trace-env.json
python3 reward_eval.py prepare --out .local/reward-eval
python3 sandbox.py --candidates .local/reward-eval/candidates.json solo --cores 2-25 --vcpus 8 --node 0 --sibling-offset 64 --harness-cpus 0-1
python3 reward_eval.py report .local/runs/20261109T015041Z-solo-jabedc2t --prep .local/reward-eval --out .local/reward-eval/report.json
taskset -c 26-31 python3 env_cost.py aweaiteam/scaleswe:itamarst_eliot_pr421 aweaiteam/scaleswe:geopandas_geopandas_pr3504 docker.io/swerebenchv2/qiskit-qiskit-terra:9543-d4006dd --work .local/env-cost --docker <local images> --out .local/env-cost.json
python3 env_cost.py --layers <image-sizes.json의 47개> --reads .local/runs/20261106T*-pilot-* --out .local/env-share.json  # 읽은 양과 VM boot 시간
python3 critical_path.py
```

원값은 [kinds.json](kinds.json), [eval.json](eval.json), [env.json](env.json), [reward-eval.json](reward-eval.json), [env-cost.json](env-cost.json), [env-share.json](env-share.json)에 있다.
