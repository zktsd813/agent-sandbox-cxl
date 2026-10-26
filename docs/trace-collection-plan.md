# RL rollout trace 수집 설계 v3: A6000 시험 + H100 cloud 본 수집 (2026-10-25)

목적: RL policy로 rollout 행동 trace를 **한 번** 모은다. 그 trace를 motivation(Phase A, C)과 이후 evaluation(D, E, F)에 계속 쓴다. A6000 × 2 서버(CUDA와 Docker만 설치된 상태)에서 계측과 경로를 시험하고, 본 수집은 H100 cloud에서 한다. 조사 근거는 [rl-trace-survey.md](rl-trace-survey.md)에 있다. 실제로 돌리는 순서와 A6000 서버(deep8)에서 확인한 결과는 [trace-collection-runbook.md](trace-collection-runbook.md)에 있다.

**진행 (2026-10-26)**: S0, S1 완료(deep8). deep8은 A6000 3장 중 다른 사용자가 GPU0을 쓰는 공용 서버라 GPU1·2만 썼다. 공용 서버에서는 다른 사용자의 image와 데이터를 훼손하지 않는 것을 1원칙으로 둔다(8절 4).

v2(2026-10-23)에서 바뀐 것: 규모를 production job과 DSec 규모 fleet에 맞춰 키웠다(6절). W5(SWE-rebench)를 넣었다. image 단위로 batch를 묶는다(8절). LLM 지연은 수집한 요청을 다시 보내는 profiling으로 잰다(2절 M7, 10절). rollout 제한(step 수, token 한도, temperature)을 각 policy의 RL 학습 스크립트에 맞췄다(6.2절).

## 0. 요약

| 항목 | 결정 |
|---|---|
| 수집 장소 | **A6000 × 2**: 계측·smoke·pilot과 재생 확인. **H100 cloud**: 본 수집과 LLM 지연 profiling. 어느 쪽이든 vLLM과 sandbox(container)를 같은 서버에서 돌림 |
| 수집 방식 | 순차 또는 낮은 병렬도. 동시 rollout 수는 CPU 코어와 GPU KV 용량 중 작은 쪽. agent 행동은 동시성과 무관하므로 contention은 합성과 재생에서 만든다 |
| 가져오는 것 | 명령과 순서, image(digest), turn마다 LLM 요청 원문과 token 수, 지연, 보상. 참고용으로 경합 없는 CPU 시계열 |
| 나중에 재는 것 | CPU burst, 메모리, dirty page, contention. 우리 bare-metal 서버에서 Firecracker로 재생하며 잰다 |
| LLM 지연 | 수집한 요청을 H100에서 동시 요청 수 N을 바꿔 다시 보내 지연 모델을 만들고, 수집 중 실측으로 검증한다. ×0.25~×4 sweep은 민감도 확인용 |
| 학습 구간 | 재지 않음. 공개 보고값이나 sweep으로 대신 |
| 규모 | task 2,137개, rollout 19,144개(6절). 기준은 "동시에 정렬되는 범위 안의 rollout이 모두 서로 다름" |
| rollout 제한 | 각 policy의 RL **학습** 설정을 따른다. SWE agent는 50 step, prompt 4K + response 32K. DeepCoder는 response 32K, temperature 0.6 (6.2절) |
| disk 제약 | task마다 image가 하나다. image 단위로 받고, 그 image를 쓰는 rollout을 모두 돌리고, 지운다. digest를 기록해 나중에 같은 image를 다시 받는다 |

workload는 다음과 같다(6절 표 참고).
- **W1**: R2E-Gym SWE. DeepSWE-Preview로 512 task × 8, Qwen3-32B로 그중 128 task × 8
- **W2**: 같은 128 task에 mini-swe-agent + Qwen3-Coder-30B-A3B, × 8
- **W3**: Terminal-Bench 2.0 89 task 전부 × 8
- **W4**: DeepCoder 1,024 문제 × 8
- **W5**: SWE-rebench 512 task(repo가 모두 다름) × 8
- 조용한 이웃은 수집하지 않고 합성한다.

## 1. 왜 순차 수집 + 합성인가

- **동시에 많이 돌리면 trace가 오염된다.** CPU가 붐비면 timeout이 나고 replay 경로가 바뀐다. 이전 실측에서 사용률이 높을 때 vCPU 시간의 10~36%가 경로가 갈라진 뒤에 쓰였다. 그래서 sandbox별 수요는 경합 없이 깨끗하게 재고, contention은 실험의 결과로 나오게 한다.
- **agent의 행동은 속도와 무관하다.** 명령, 출력, token 수, 성공 여부는 policy와 sampling이 정한다. 느리게 모아도 내용은 같다. G group의 상관도 같은 task·image에서 비슷한 명령이 나오는 데서 생기므로 따로 돌려도 기록된다. "G개가 함께 시작한다"는 시간상의 상관은 합성에서 넣는다.
- **LLM 지연은 엔진의 성질이다.** 한 turn의 지연은 입력·출력 token 수와 그 순간 server의 동시 요청 수로 정해진다. token 수는 순차 수집으로 정확히 얻고, 동시 요청 수에 따른 변화는 수집한 요청을 다시 보내는 profiling(M7)으로 따로 잰다. sandbox가 필요 없다.
- **시나리오가 많다.** 동기/비동기, 배치, overcommit, job 섞기를 시나리오마다 다시 수집할 수 없다. 한 번 모은 trace를 다시 조합해야 하고, evaluation은 같은 workload를 반복해야 한다.

합성 방법은 10절에 있다.

## 2. 무엇을 측정하나

| 묶음 | 항목 | 쓰임 |
|---|---|---|
| **M1 행동** | turn마다 실행한 명령, 출력(해시와 크기, 원문은 압축 저장), return code, 명령 시작·끝 시각, 최종 patch, 보상 | 재생 입력, tool 종류 분류(A1), burst 예측(A5) |
| **M2 LLM** | turn마다 요청·첫 byte·완료 시각, 입력 token(context 길이), cache hit token, 출력 token, **요청 원문(압축)**, 1초 간격 server 부하(실행 중·대기 요청 수, KV 사용률) | 지연 모델의 검증 값, LLM 대기 분포(A2), M7 profiling 입력 |
| **M3 group** | task id, group 안 번호(0~7), seed, 모델, scaffold, rollout 시작 시각 | group 유사도, 동기·비동기 GRPO 합성 |
| **M4 sandbox 생애** | image pull 시간과 크기, digest, container 생성·시작·종료 시각, 보상 테스트 구간 | 생성 폭주 모델, 재생 환경 고정 |
| **M5 자원 (참고)** | container별 cgroup: CPU 사용(10ms), CPU 압력(PSI), 메모리, IO(100ms) | 경합이 없었는지 확인(오염 판정), 재생 결과와 교차 확인 |
| **M6 생성 폭주** (로컬 서버에서 따로) | N = 8~512개 동시 생성 시 CPU, disk, 시간 | 생성 폭주 모델, image를 CXL에 두는 설계의 근거 |
| **M7 지연 profiling** (H100에서 따로) | M2의 요청 원문을 동시 rollout 수 N = 1, 8, 32, 64, 128, 256으로 다시 보냄. turn마다 TTFT, token당 시간, 완료 시간 | 합성의 LLM 지연 모델 (10절) |

MPKI, 메모리 대역폭, dirty page, working set은 수집 단계에서 재지 않는다. 우리 서버에서 Firecracker로 재생할 때 잰다.

## 3. GPU 서버 사전 확인 (A6000과 H100 각각)

| 확인 | 명령 | 기준 |
|---|---|---|
| GPU, 연결 | `nvidia-smi`, `nvidia-smi topo -m` | A6000 두 장(둘 사이 NV#), H100은 장수와 NVLink 확인 |
| GPU를 쓰는 container | `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` | 실패하면 NVIDIA Container Toolkit 설치 |
| CPU, 메모리 | `nproc`, `lscpu`, `free -g` | 동시 rollout 수를 정함 (5절) |
| disk | `df -h`, `docker info --format '{{.DockerRootDir}}'` | image 저장 위치의 남은 공간이 batch 크기를 정함 (8절) |
| cgroup | `stat -fc %T /sys/fs/cgroup`, `docker info --format '{{.CgroupDriver}} {{.CgroupVersion}}'` | cgroup2, systemd driver면 container별 경로가 `/sys/fs/cgroup/system.slice/docker-<id>.scope` |
| network | Hugging Face와 Docker Hub 접근, `docker login` | image 약 1,100개를 받으므로 login 필수 |
| 시간 | `chronyc tracking` 또는 `timedatectl` | 모든 기록은 그 서버 하나의 시계를 씀 |

## 4. 설치

1. **NVIDIA Container Toolkit** (없으면): 배포판 package로 설치하고 `nvidia-ctk runtime configure --runtime=docker`를 실행한 뒤 Docker를 재시작한다.
2. **vLLM**: 공식 image `vllm/vllm-openai`를 쓴다. 버전을 고정하고 digest를 기록한다. A6000과 H100에서 같은 버전을 쓴다.
3. **Python 환경**: `uv`로 scaffold마다 가상환경을 따로 만든다(`experiments/rollout-trace/setup/setup_env.sh`). Harbor는 Python 3.12 이상이 필요하고 R2E-Gym은 자기 `uv.lock`을 쓰기 때문이다. 계측 도구는 R2E-Gym 환경에서 돌린다.
   - **R2E-Gym** (W1, W5): git clone + `pip install -e .`. commit을 고정한다.
   - **mini-swe-agent** (W2): pip. 버전을 고정한다.
   - **Harbor** (W3): pip 또는 공식 설치 방법. 버전을 고정한다.
   - 계측 도구(7절): 우리가 작성한다. `aiohttp`, `docker` Python SDK를 쓴다.
4. **모델 weight**: Hugging Face에서 받고 revision을 기록한다.
   - agentica-org/DeepSWE-Preview: **FP32로 올라와 있어 131GB**다. 받은 뒤 `setup/to_bf16.py`로 BF16(62GB)으로 바꾼다
   - Qwen/Qwen3-32B 약 66GB
   - Qwen/Qwen3-Coder-30B-A3B-Instruct 약 61GB
   - agentica-org/DeepCoder-14B-Preview 약 30GB
5. **실험 repo**: 계측 도구와 driver는 이 repo의 `experiments/rollout-trace/`에 두고, GPU 서버에서 clone해서 쓴다.

## 5. 동시성과 CPU 배분

- **vLLM**: replica마다 CPU 2~4코어를 cpuset으로 고정한다. 계측 도구는 1~2코어를 쓴다.
- **sandbox**: 나머지 코어를 쓴다.
  - container마다 CPU 4개짜리 cpuset을 돌아가며 준다. `nproc`이 4로 보이므로 4 vCPU microVM 재생과 같은 조건이 된다.
  - CPU quota(`--cpus`)는 걸지 않는다. 자연스러운 burst를 재기 위해서다.
- **동시 rollout 수** = min(GPU KV 용량, sandbox용 코어 수 ÷ 2).
  - sandbox 평균 사용은 0.25~0.45코어라 이 정도면 경합이 드물다.
  - container별 PSI(`cpu.pressure`)로 경합이 있었는지 확인하고, 기준을 넘은 rollout은 표시해 둔다.

모델별 GPU 설정(vLLM, prefix cache 켬):

| 서버 | 모델 | 정밀도 | 배치 | 예상 동시 rollout (평균 context 16K) |
|---|---|---|---|---|
| A6000 × 2 (시험) | DeepSWE-Preview, Qwen3-32B | **FP8 weight-only** (Ampere에서는 W8A16 Marlin) | TP2 replica 1개 | 약 13개, KV도 FP8이면 약 26개 |
| A6000 × 2 (시험) | Qwen3-Coder-30B-A3B | FP8 weight-only | TP2 | 수십 개 |
| A6000 × 2 (시험) | DeepCoder-14B | BF16 | TP2 | 충분 |
| **H100 (본 수집)** | DeepSWE-Preview, Qwen3-32B | **BF16** (학습된 그대로) | TP2 replica를 GPU 수 ÷ 2개 | replica당 약 14개 |
| H100 (본 수집) | Qwen3-Coder-30B-A3B, DeepCoder-14B | BF16 | TP1 또는 TP2 | 수십 개 |

- 본 수집은 BF16으로 한다. 양자화가 행동을 바꾸는지 따질 필요가 없어진다. A6000의 FP8 pilot 데이터는 본 데이터에 섞지 않고 경로 확인용으로만 쓴다.
- 예시 인자(버전별 확인 필요): `--tensor-parallel-size 2 --max-model-len 36864 --enable-prefix-caching --gpu-memory-utilization 0.92`. `--max-model-len`은 workload의 prompt + response 한도에 맞춘다(W1·W2·W5 36,864, W4 34,816). A6000에서는 `--quantization fp8`을 더한다.

## 6. 규모와 workload (2026-10-25 확정)

### 6.1 규모의 근거

규모는 통계량이 아니라 **합성할 때 rollout을 복제하지 않아도 되는지**로 정한다.

- **복제는 동시에 정렬될 때만 문제가 된다.** 지금 fleet simulation(`experiments/contention/RESULTS-fleet.md`)은 trace 약 250개로 192코어 node 16개(동시 sandbox 약 5,600~9,800개)를 채운다. trace 하나가 20~40개씩 복제되지만, 시작 시각이 무작위라 trace를 15~120개로 줄여도 contention이 같거나 커졌다. 반면 동기 RL step은 모든 rollout을 t=0에 시작한다. 같은 trace 두 개가 같은 step에 들어가면 burst가 ms 단위로 완전히 겹쳐서, 실제 G group보다 강한 가짜 상관이 생긴다.
- **기준 1: job 하나의 step 안에서는 rollout이 모두 달라야 한다.** production job 크기는 DeepSWE step당 512 rollout(64 × 8), Nemotron 512~1,024 rollout(16 × 32, 64 × 16), DeepCoder step당 rollout 1,024개(학습 스크립트 128 문제 × 8. 블로그는 "iteration당 1,024 문제"라고 적었는데 스크립트와 다르다), rStar2 512 prompt × 32다. 주력 workload(W1)는 task 512개 × 8로 B = 512 step 하나를 복제 없이 만든다. W4는 1,024 문제 × 8이라 B = 128 step 8개를 문제 중복 없이 만든다.
- **기준 2: fleet 한 시점의 동시 sandbox를 복제 없이 채운다.** sandbox를 쓰는 agent rollout(W1, W2, W3, W5)이 10,952개로, 1 vCPU fleet(약 9,800개)까지 채운다. 여러 job을 섞을 때 job 사이에는 시작 시각이 어긋나므로 이 기준은 느슨해도 된다.
- **기준 3: repo 다양성.** burst 모양은 repo의 build·test 구성이 좌우한다. R2E-Gym은 repo가 10개뿐이라 512개를 뽑으면 pandas에서만 약 160개가 나온다. W5는 SWE-rebench에서 repo가 모두 다른 512개를 고른다.
- step 사이에는 같은 task를 다시 써도 된다. 실제 학습도 epoch마다 task를 다시 본다. 다만 같은 rollout trace를 여러 step에서 반복하면 step 사이 변동이 작게 나오므로, 여러 step을 합성할 때는 rollout을 돌려 쓰고 그 사실을 적는다.

### 6.2 workload 표

| ID | 문제 set | image | 모델, scaffold | 규모 | 설정 |
|---|---|---|---|---|---|
| **W1** | **R2E-Gym-Subset** ([HF](https://huggingface.co/datasets/R2E-Gym/R2E-Gym-Subset)) 4,578개, 10개 repo | `docker_image` 열에 task마다 하나. 무작위 20개 모두 Docker Hub에 있음, 압축 0.3~1.4GB(평균 0.69GB), 풀면 1.5~1.9GB | **DeepSWE-Preview** (R2E-Gym으로 RL 학습) + **Qwen3-32B** (학습 초기), R2E-Gym scaffold (`r2egym.agenthub.run.edit`, backend docker) | repo별 비율로 512 task. DeepSWE 512 × 8 = 4,096. Qwen3-32B는 그중 128 task × 8 = 1,024 | **DeepSWE 학습 설정**: temperature 1.0, 최대 50 step, prompt 4K + response 32K(넘으면 잘라서 표시), 생성 timeout 20분, 보상 계산 제한 5분. Qwen3-32B도 같은 제한 |
| **W2** | W1의 Qwen3-32B와 **같은 128 task** | W1과 공유 | **Qwen3-Coder-30B-A3B**, mini-swe-agent (docker environment, image는 dataset의 `docker_image`) | 128 × 8 = 1,024 | W1과 같은 제한(50 step, 36K, timeout). mini-swe-agent 기본값은 RL 학습 설정이 아니므로 쓰지 않는다 |
| **W3** | **Terminal-Bench 2.0** ([GitHub](https://github.com/harbor-framework/terminal-bench-2)) **89개 전부** | task마다 `task.toml`의 `docker_image` (예: `alexgshaw/adaptive-rejection-sampler:20251031`), `cpus = 1`, `memory_mb = 2048` | Qwen3-Coder-30B-A3B, Harbor 기본 agent | 89 × 8 = 712 | task 설정 그대로 (agent 900초) |
| **W4** | **DeepCoder-Preview-Dataset** ([HF](https://huggingface.co/datasets/agentica-org/DeepCoder-Preview-Dataset)): primeintellect, taco, lcbv5 | 실행기 하나: `python:3.11-slim` 기반 작은 실행기(직접 작성). 문제마다 테스트를 subprocess로 돌리고 rusage로 CPU를 잼 | **DeepCoder-14B-Preview**, vLLM으로 생성(n=8) | 1,024 × 8 = 8,192 | **DeepCoder 학습 설정**(32K 단계): temperature 0.6, prompt 2K + response 32K. 평균 출력은 학습 중 8K → 17.5K token. 테스트 timeout은 데이터셋 기준 |
| **W5** | **SWE-rebench** ([HF](https://huggingface.co/datasets/nebius/SWE-rebench)) 21.3K개, Python repo 3,400개. 공개 image가 있는 약 7,500개 중에서 고름 | task마다 `swerebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>`, 약 1.3GB | **DeepSWE-Preview**, R2E-Gym scaffold. SWE-bench 형식 image를 못 돌리면 mini-swe-agent로 바꿈(S2에서 확인) | **repo가 모두 다른** 512 task × 8 = 4,096 | W1과 같은 제한. 보상은 FAIL_TO_PASS, PASS_TO_PASS |

| 합계 | task | rollout | 그중 agent rollout (W1, W2, W3, W5) | image |
|---|---|---|---|---|
| | 2,137 (W1 512 + W3 89 + W4 1,024 + W5 512) | 19,144 | 10,952 | 약 1,100개, 압축 기준 1TB 이상 (W1 약 350GB, W5 약 670GB, W3 미측정) |

- W5의 policy는 R2E-Gym으로 학습된 DeepSWE라 SWE-rebench에서는 분포 밖이다. 이 점을 논문에 적는다. SWE-rebench로 RL 학습된 공개 policy를 찾으면 바꾼다.
- G는 8로 모은다. G = 4는 부분집합으로 만든다. G = 16 이상을 보려면 일부 task에서 더 모은다.
- 제한 출처: DeepSWE는 rLLM v0.1 `examples/swe/train_deepswe_32b.sh`(`agent.max_steps=50`, `max_prompt_length=4096`, `max_response_length=32768`, `rollout.n=8`, temperature 1.0)와 [DeepSWE 블로그](https://www.together.ai/blog/deepswe)(생성 timeout 20분, 보상 5분). DeepSWE 블로그의 "64K, 100 step"은 평가 설정이다. DeepCoder는 rLLM v0.1 `examples/deepcoder/train_deepcoder_32k.sh`(`max_response_length=32768`, `rollout.n=8`, temperature 0.6)와 [DeepCoder 블로그](https://www.together.ai/blog/deepcoder)(16K → 32K, 평균 출력 8K → 17.5K).
- 50 step 제한을 우리 264 trace에 걸면 42%가 잘리고 평균 turn 수가 51.5에서 43.1이 된다. RL 학습에서도 잘린 rollout이 생기므로 잘린 rollout을 버리지 않고 표시만 한다.

**task 고르기** (seed를 고정하고 목록을 `experiments/rollout-trace/`에 저장한다)
- W1: repo 비율을 맞춘다(pandas 1,444, numpy 781, pillow 620, orange3 482, aiohttp 299, tornado 261, scrapy 215, pyramid 189, datalad 179, coveragepy 108). 512개를 비율대로 고르고, 그중 128개(W1 Qwen3-32B·W2 공용)도 비율대로 고른다.
- W5: 공개 image가 있는 task를 repo별로 묶고, repo 512개를 무작위로 골라 repo마다 task 하나를 고른다.
- W4: 세 출처의 비율을 맞춰 1,024개를 고른다.

## 7. 계측 구현 (`experiments/rollout-trace/`)

scaffold 코드는 최소한만 고치고, 바깥에서 잡는다.

| 도구 | 하는 일 | 연결 방법 |
|---|---|---|
| **`llm_proxy.py`** | vLLM 앞의 기록용 reverse proxy. 요청마다 rollout id, 요청·첫 byte·완료 시각, 입력·cache hit·출력 token(응답의 `usage`), 종료 이유, 오류를 `llm.jsonl`에 쓰고, 요청 본문을 압축해 따로 저장 | rollout마다 base URL을 `http://127.0.0.1:19000/r/<rollout_id>/v1`로 다르게 주고, proxy가 앞부분을 떼어 vLLM으로 넘김. R2E-Gym은 `LLM_BASE_URL` 환경변수와 `hosted_vllm/<이름>` 모델명을 씀 |
| **`docker_events.py`** | `docker events`에서 container 생성·시작·종료와 **exec 생성·시작·종료**(명령 문자열, exit code, ns 시각)를 `docker_events.jsonl`에 씀 | R2E-Gym은 명령을 `exec_run`으로, mini-swe-agent는 `docker exec`로 실행하므로 framework와 상관없이 tool call을 잡음 (Harbor도 exec인지 확인 필요) |
| **`cgroup_sampler.py`** | 실행 중인 container마다 `cpu.stat`(10ms), `cpu.pressure`, `memory.current`, `memory.peak`, `io.stat`(100ms)를 읽어 container별 파일에 저장 | Docker event로 container 시작·종료를 따라감. 전용 코어 하나에 고정 |
| **`vllm_metrics.py`** | vLLM `/metrics`를 1초마다 읽어 실행 중·대기 요청 수, KV 사용률을 기록 | — |
| **`driver.py`** | (workload, 모델, task, group 번호) 목록을 동시성 제한 안에서 실행. rollout마다 별도 process를 띄우고 rollout id, base URL, container label 또는 이름 규칙을 지정. 끝나면 결과와 scaffold의 trajectory 파일을 모음. image 단위 batch(8절)를 따름 | R2E-Gym은 task 하나 × 1회 실행 단위로 호출 |
| **`profile_llm.py`** | M7. 저장한 요청 본문으로 가상 rollout N개를 만들어 각자 turn 순서대로 보냄. turn 사이에는 기록된 tool 시간만큼 쉼. prefix cache가 실제처럼 쓰이도록 rollout 안의 순서를 지킴 | sandbox 없이 vLLM만 띄워서 실행 |
| **`manifest.json`** | 하드웨어, driver·CUDA·Docker·vLLM 버전, 모든 git commit, image digest, 모델 revision, 설정 | 수집 시작 시 자동 생성 |

**container와 rollout 연결**: container에 `trace.rollout=<id>` label을 붙인다. mini-swe-agent는 `run_args`로 붙일 수 있다. R2E-Gym은 `containers.run` 호출에 label 인자를 넘기는 한 줄을 추가한다. 안 되면 container 이름 규칙으로 연결한다.

**출력 구조**
```
traces/<workload>/<model>/<task_id>/g<k>/
  meta.json        task, image digest, 모델, scaffold, seed, 시작·끝 시각
  llm.jsonl        turn별 LLM 기록 (proxy)
  llm_req/         turn별 요청 본문 (압축)
  exec.jsonl       명령별 기록 (docker events에서 추림)
  cgroup/          10ms CPU, PSI, 메모리, IO
  result.json      보상, 테스트 결과, patch, 오염 판정
  scaffold/        scaffold 원본 trajectory
traces/_global/    docker_events.jsonl, vllm_metrics.csv, manifest.json, pull_log.csv
profile/<model>/<hw>/N<n>/   M7 결과
```

## 8. image 용량 관리: image 단위로 받고, 돌리고, 지우기

**원칙**
1. image는 **digest로 고정**한다(`repo@sha256:...`). pull할 때 digest와 압축·풀린 크기를 `pull_log.csv`에 남긴다. 나중에 재생할 때 같은 digest로 다시 받으면 되므로, 수집 후 지워도 된다.
2. **image를 한 번만 받는다.** image 하나를 받으면 그 task를 쓰는 rollout을 모두 끝낸 뒤 지운다. W1 공용 128 task는 image 하나에 DeepSWE 8개, Qwen3-32B 8개, Qwen3-Coder(W2) 8개, 합쳐 24개 rollout이 붙는다.
3. **같은 repo의 task를 같은 batch에** 넣는다. 같은 repo의 image들은 아래 layer를 공유해서 받는 양과 disk가 줄어든다.
4. batch가 끝나면 그 batch의 container를 지우고(이번 실행의 `trace.run` label이 붙은 것만), 우리가 받은 image를 지운다. 매 batch 전후로 남은 disk를 기록한다. **공용 서버에서는 다른 사용자의 image를 절대 훼손하지 않는다.** `prune`(어떤 종류든)과 `docker rmi -f`는 쓰지 않는다. image 삭제는 `experiments/rollout-trace/docker_safety.py`만 하며, 실행 전 snapshot에 없었고 우리 pull 기록에 있고 누구의 container도 쓰지 않는 image만 force 없이 지운다. Docker가 layer 참조 수를 세므로 다른 image와 공유하는 layer는 남는다.
5. **pull은 측정과 겹치지 않게** 한다. 다음 batch의 pull은 sandbox 코어와 겹치지 않는 코어에서 하거나 현재 batch가 끝난 뒤 한다. pull 자체가 CPU·disk를 크게 쓰기 때문이다.

**batch 크기** = (disk 여유 − model weight − 여유 20%) ÷ (image 하나의 풀린 크기 약 1.5~2GB + container 쓰기 공간 약 0.5GB × 동시 container 수 ÷ batch task 수).

**phase 순서 (H100 본 수집)**

공용 128 task는 세 모델이 같은 image를 쓴다. GPU가 충분하면(예: H100 8장에 DeepSWE TP2, Qwen3-32B TP2, Qwen3-Coder TP2) 세 모델을 동시에 띄우고 image 단위로 24개를 한 번에 돌린다. GPU가 모자라면 공용 batch를 받은 뒤 모델을 바꿔 가며 돌리고 나서 지운다.

| phase | 모델 (GPU에 올림) | task | rollout |
|---|---|---|---|
| P1 | DeepSWE + Qwen3-32B + Qwen3-Coder | W1 공용 128 task (image 단위로 24개씩) | 128 × 24 = 3,072 |
| P2 | DeepSWE | W1 나머지 384 task | 384 × 8 = 3,072 |
| P3 | DeepSWE | W5 512 task | 512 × 8 = 4,096 |
| P4 | Qwen3-Coder-30B-A3B | W3 89 task | 89 × 8 = 712 |
| P5 | DeepCoder-14B | W4 1,024 문제 (실행기 image 하나) | 1,024 × 8 = 8,192 |
| P6 | 모델마다 | M7 지연 profiling (sandbox 없음) | — |

## 9. 실행 순서 (단계별)

| 단계 | 서버 | 내용 | 확인 |
|---|---|---|---|
| S0 준비 ✅ | A6000 | 3절 확인, 4절 설치 | GPU container 동작, cgroup 경로 확인 |
| S1 계측 ✅ | A6000 | 7절 도구 작성. R2E-Gym 1 task × 1회로 끝까지 시험 | llm/exec/cgroup 기록과 요청 본문이 한 rollout에 모두 연결됨. 결과는 runbook 5절 |
| S2 smoke | A6000 | W1~W5 각 scaffold를 1 task × 2회씩. W5는 R2E-Gym scaffold가 SWE-rebench image를 돌리는지 확인 | 형식, 보상, 정리 동작 |
| S3 pilot | A6000 | W1 8 task × 8, image 단위 batch 포함 전체 경로 | batch 처리, 오염 표시, 처리량, turn 수와 turn당 출력 token(계산기 가정 대체) |
| S4 재생 확인 | 우리 서버 | S3 rollout을 같은 digest image에서 재생해 return code 일치율 확인, Firecracker rootfs 변환 | 일치율이 낮으면 원인(network, 시각 의존)을 고치고 나서 본 수집 |
| S5 본 수집 | H100 | 8절 P1~P5 | batch마다 disk, 오염 rollout 비율, 실패율 기록 |
| S6 지연 profiling | H100 | P6. 모델마다 N sweep | S5 수집 중 실측 지연(같은 부하)과 모델 예측이 맞는지 |
| S7 정리 | H100 → 우리 서버 | trace를 재생 형식으로 변환해 옮김 | rollout 수, 누락, 오염 표시 |
| S8 민감도 | 우리 서버 | 합성 + simulator에서 지연 모델 ×0.25~×4 | 결론이 지연 값에 얼마나 민감한지 |

재생이 안 되는 trace를 대량으로 모으는 것을 막기 위해 S4를 본 수집 앞에 둔다. GPU 시간은 `experiments/rollout-trace/estimate_time.py`로 대략 추정한다.

## 10. 합성: 어떻게 하나

**재료**
- rollout 기록 (M1~M5)
- LLM 지연 모델 (M7): (동시 요청 수, context, cache hit, 출력 token) → turn 지연. S5의 실측으로 검증한다
- 생성 비용 모델 (M6)
- 학습 구간 (공개값 또는 sweep)

**규칙**: 동시에 정렬되는 범위(job 하나의 step 하나, 비동기에서는 같은 weight 갱신 직후 함께 재개되는 묶음) 안에서는 같은 rollout을 두 번 쓰지 않는다(6.1).

**순서**
1. **rollout 고르기**: 시나리오에 맞게 task B개 × G개를 고른다.
2. **orchestration**
   - 동기: 모두 t=0에 생성부터 시작하고, 마지막이 끝나면 학습 구간만큼 쉰다.
   - 비동기: 동시 rollout 수(pool)를 일정하게 유지한다. group 하나가 끝나면 새 task의 group(G개)을 함께 시작한다. group 안 G개가 모두 끝나야 학습 batch에 들어간다.
   - 비동기에서 넣을 것: weight 갱신 때 생성이 멈추는 시간(framework마다 다르므로 sweep), 그 직후 함께 재개되는 LLM 응답, partial rollout(멈춘 rollout의 sandbox는 상태를 유지한 채 기다림), oversampling과 dynamic sampling으로 중간에 버리는 rollout.
3. **추론 server 흉내**: 각 LLM turn의 시간을 그 순간 진행 중인 요청 수와 token 수로 지연 모델에서 정한다. 512개가 함께 시작하면 첫 응답이 비슷한 때 몰려 오는 "첫 turn 물결"이 여기서 생긴다.
4. **배치**: 무작위, 가장 덜 바쁜 node, group을 한 node에, image locality 중에서 고른다.
5. **재생**
   - (a) 우리 서버에서 Firecracker로 실제 재생한다. mock LLM server가 그 순간 부하로 지연을 계산한다.
   - (b) `fleet.py`로 큰 규모를 돌린다.
6. **검증**: 낮은 병렬도로 모은 trace로 합성한 결과와, 작은 규모 실제 병렬 실행(가능한 동시 수만큼)을 비교한다.

**한계**
- agent가 contention에 반응하지 않는다고 가정한다. timeout은 재생에서 실제로 일어난다.
- 지연은 동시 요청 수와 token 수의 함수로 근사한다. 측정한 H100 구성 밖으로는 외삽이다.
- 학습에 따른 policy 변화는 checkpoint 두 개(Qwen3-32B, DeepSWE)로만 본다.

## 11. motivation과 evaluation에서의 쓰임

| HANDOFF | 이 trace로 하는 것 |
|---|---|
| A1 | tool call 종류별 CPU burst, 메모리 (재생에서 측정) |
| A2 | LLM 대기 분포와 부하에 따른 변화 |
| A3 | 합성 step을 node 여러 대에서 재생해 contention과 node 사이 불균형 측정 |
| A4 | tool call 단위 CXL tax (재생) |
| A5 | 명령 시작 시점 정보로 burst 예측. task 단위로 학습·평가를 나눔 |
| C | simulator 입력 (실제 group 8개, 지연 모델, 생성 폭주) |
| F | 같은 합성 workload로 F0~F6 비교 (B = 64, 128, 512) |

## 12. 확인할 점과 위험

- **명령으로 재생이 되는가.** 같은 digest image + 같은 명령이면 같은 결과라는 전제다. pip 같은 network 접근과 시각 의존 명령은 갈라질 수 있다. S4에서 일치율을 재고, 필요하면 local PyPI mirror를 둔다.
- **R2E-Gym 도구 호출.** file editor 같은 도구도 container 안에서 exec로 실행되는지 S1에서 확인한다. exec가 아닌 방식(예: `put_archive`로 파일 복사)이 있으면 그 내용도 기록해야 재생된다.
- **Harbor.** exec 방식인지, container label을 붙일 수 있는지 S2에서 확인한다.
- **W5 scaffold.** R2E-Gym scaffold가 SWE-rebench image(SWE-bench 형식)를 돌리는지 S2에서 확인한다. 안 되면 W5만 mini-swe-agent로 바꾸고, 그 경우 W1과 scaffold가 달라진다는 점을 적는다.
- **disk.** 실제 풀린 image 크기는 S3 pilot과 P1 첫 batch에서 재서 batch 크기를 다시 정한다. W3 image 크기는 아직 모른다.
- **Docker Hub pull 제한.** image 약 1,100개를 받는다. login한 계정의 제한을 확인하고, 필요하면 cloud 안에 registry mirror를 둔다.
- **A6000 FP8과 H100 BF16의 차이.** pilot은 FP8이라 본 데이터와 행동이 다를 수 있다. pilot은 경로 확인용으로만 쓴다.
- **Terminal-Bench 2.0은 benchmark다.** CPU 성격의 다양성을 얻는 용도로만 쓴다.
- **budget이 긴 학습은 다루지 않는다.** DeepSWE의 학습 설정(50 step, 약 36K)은 production 중 짧은 편이다. NVIDIA Nemotron 3 Super의 SWE RL은 최대 200 turn, sequence 131K~196K, agent timeout 3,600초다([문서](https://docs.nvidia.com/nemotron/latest/nemotron/super3/rl/swe.html)). 50 step에서 잘린 꼬리가 결론에 영향을 주는지는 따로 확인해야 하고, 필요하면 공용 128 task를 100 step·64K로 더 모은다.

출처: [vLLM FP8 문서](https://docs.vllm.ai/en/latest/features/quantization/fp8/), [R2E-Gym runtime](https://github.com/R2E-Gym/R2E-Gym/blob/main/src/r2egym/agenthub/runtime/docker.py), [R2E-Gym runner](https://github.com/R2E-Gym/R2E-Gym/blob/main/src/r2egym/agenthub/run/edit.py)
