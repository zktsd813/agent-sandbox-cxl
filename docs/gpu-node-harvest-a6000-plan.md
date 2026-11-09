# A6000 × 2 서버의 G1~G3 실행 계획 (2026-11-03)

[GPU 노드 harvest 설계](gpu-node-harvest-design.md) 5~7장의 G1~G3를 이 서버에서 어떻게 돌리고 어떻게 보일지 정한다. 무엇을 왜 재는지는 설계 문서를 따르고, 여기서는 구성, 측정 도구, 실험 행렬, 그림과 관문을 정한다.

> **2026-11-09.** G1을 돌렸다. 결과는 [A6000 G1과 decode CPU 결과](results/gpu-harvest-a6000-20261109/README.md)에 있다. 일반적인 구성에서는 관문 1을 넘지 못했고(C2 CPU Adam만 통과), G2·G3는 돌리지 않았다. 3장의 도구 중 실제로 만든 것은 `sampler.py`, `hook/sitecustomize.py`(phase 기록), `analyze_g1.py`다.

## 0. 요약

| 관문 | 질문 | 이 서버에서 하는 일 | 대표 그림 | 통과 기준 |
|---|---|---|---|---|
| G1 | 학습의 CPU 수요가 시간에 따라 변하나 | sandbox 없이 RL 학습 4개 구성을 100 ms 단위로 기록 | 그림 1 phase timeline, 그림 2 빌릴 수 있는 코어 CDF | step 안 p99 수요 − 평균 수요 ≥ 노드 thread의 10% |
| G2 | 고정 분할과 격리로 GPU 손해를 막을 수 있나 | GPU workload 2개 × sandbox 밀도 5단계 × 격리 조건 1~6 | 그림 4 밀도별 slowdown, 그림 5 남는 간섭 분해 | 조건 3이나 4로 목표 밀도에서 GPU 손해 > 1% |
| G3 | 같은 GPU 성능에서 sandbox를 몇 개 더 올리나 | G2 결과에서 계산 | 그림 7 방식별 최대 밀도 | 비우기(조건 5)가 제자리 축소(조건 4)보다 눈에 띄게 많다 |

먼저 할 일은 측정 도구(3장)와 noise floor 확인(2.4)이다. 그 다음 G1의 C1, C2를 돌려 관문 1을 판단한다. Firecracker 준비(8장)는 G1과 병행한다.

## 1. 이 서버에서 이미 확인한 것

### 1.1 하드웨어 (2026-11-03 측정)

| 항목 | 값 |
|---|---|
| CPU | Xeon Gold 6326 × 2 소켓, 소켓당 16 core, SMT on (64 thread), L3 소켓당 24 MiB |
| NUMA | node0 = CPU 0-15,32-47, node1 = CPU 16-31,48-63. 노드당 DRAM 약 190 GB, 총 314 GB |
| GPU | RTX A6000 × 2 (48 GB), NVLink NV4. 두 장 모두 node0 (CPU affinity 0-15,32-47) |
| RDT | `cat_l3`, `mba`, `cqm_mbm_total` 지원. resctrl은 아직 mount 안 됨 |
| perf | `uncore_imc/cas_count_read|write/` 있음. `perf_event_paranoid=4`라 root로 실행 |
| 기타 | governor `schedutil`, disk 여유 약 508 GB, model은 서버 공유 위치 `/mnt/ssd1/<org>/<model>`(root 소유, 읽기 전용)에 둔다 |

### 1.2 이 서버의 기존 agentic RL 학습 (`results/train-400-007`)

저장소 밖 작업 디렉토리(`train.py`, `.venv-train`)에 R2E-Gym SWE task의 실제 RL 학습이 있다. rllm(618fa7d) + verl 0.6.1 + vLLM 0.11.0, Qwen3-32B LoRA(rank 8), FSDP2 `CPUOffloadPolicy`, vLLM TP=2, batch 4 × 2 sample, 동시 rollout 2, sandbox는 Docker다.

verl이 남긴 step 12의 phase 시간이다.

| phase | 시간 (s) | step 대비 |
|---|---|---|
| collect_trajectory (rollout) | 4,543 | 89.7% |
| old_log_prob | 82 | 1.6% |
| update_actor | 245 | 4.8% |
| save_checkpoint | 36 | 0.7% |
| step | 5,065 | 100% |

`CPUOffloadPolicy`는 optimizer step을 CPU에서 돌린다. 그래서 update_actor 구간이 2장의 "optimizer CPU offload" 구성의 실제 예다. 기존 trace에는 container별 cgroup CPU(200 ms)와 model/tool/command 사건만 있다. 노드 전체의 core별 CPU, DDR 대역폭, verl phase 시각은 없다. 3장의 도구가 이 빈칸을 채운다.

## 2. 공통 구성

### 2.1 노드 흉내

| 역할 | CPU | 메모리 | 쓰임 |
|---|---|---|---|
| GPU 노드 | node0 (기본 32 thread = GPU당 16 thread) | node0 DDR | 학습·추론 엔진과 harvest한 sandbox |
| CPU 노드 | node1 중 30 thread (16-30,48-62) | node1 DDR | 비울 때 sandbox가 가는 곳, sandbox 단독 baseline |
| 관리 | 31,63 | node1 | sampler, `sandbox.py` harness, 요청 재생기 |
| CXL 대용 | – | node1 DDR | 조건 6에서 sandbox 메모리를 처음부터 여기 둔다 (Pond 방식) |

- GPU당 thread는 Acme(16 thread)에 맞춘 16을 기본으로 한다. 8 thread/GPU(node0의 core 0-7과 그 sibling 32-39)를 변형으로 둔다. 32 thread/GPU는 node0만으로 만들 수 없어 하지 않는다. DGX H100(GPU당 28 thread)보다 빌릴 코어가 적은 쪽이라 결과가 보수적이다.
- 학습 쪽 process 전체를 cgroup v2 scope에 넣어 cpuset과 mems를 고정한다(`sudo systemd-run --uid=ijkim --scope -p AllowedCPUs=... -p AllowedMemoryNodes=0`). Ray worker와 vLLM은 자식이라 물려받는다. Docker container는 docker의 cgroup에 따로 생기므로 `--cpuset-cpus`와 `--cpuset-mems`를 직접 준다.

### 2.2 실행 환경 고정

실행마다 `env.json`에 남긴다: SMT(`/sys/devices/system/cpu/smt/active`), governor, turbo, resctrl mount와 schemata, GPU clock, 저장소·rllm·verl commit, model digest, 그 시각 다른 사용자 process 목록.

- governor는 `performance`, GPU clock은 `nvidia-smi -lgc`로 고정한다.
- **공유 서버 주의**: governor, SMT, resctrl, GPU clock은 서버 전체 설정이다. docker group에 다른 사용자 5명이 있다. 바꾸기 전에 사용 시간을 맞추고, 끝나면 되돌린다. 실행 전 `nvidia-smi`와 load를 확인해 다른 작업이 있으면 그 실행은 버린다.

### 2.3 지표

| 쪽 | 지표 | 출처 |
|---|---|---|
| GPU (추론) | throughput(token/s), TTFT p50·p99, TPOT p50·p99 | 요청 재생기 |
| GPU (학습) | step time, phase별 시간 | verl `timing_s/*` + phase 기록 |
| sandbox | rollout group 지연, 명령 시간 | `sandbox.py` 결과 (CPU 노드 단독 대비) |
| 자원 | core별·thread별 CPU, host 메모리, DDR·PCIe 대역폭, LLC·MBM | sampler (3장) |

### 2.4 noise floor

G2의 관문이 "GPU 손해 1%"라서 측정 잡음이 그보다 충분히 작아야 한다. 도구가 준비되면 sandbox 없이 각 GPU workload를 5번 반복한다. 반복 간 변동계수(CV)가 0.5% 미만인지 확인하고, 넘으면 실행 길이를 늘리거나 clock 고정을 다시 본다. 이 값은 모든 그림에 회색 띠로 같이 그린다.

## 3. 새로 만들 도구 (`experiments/gpu-harvest/`)

| 도구 | 내용 | 쓰는 곳 |
|---|---|---|
| `sampler.py` | 100 ms마다 `/proc/stat` core별 CPU를 읽는다. 추적 cgroup의 thread별 CPU(`/proc/<pid>/task/*/stat`)를 owner로 묶는다: trainer, vLLM engine, ray, sandbox, 기타. `/proc/meminfo`와 cgroup `memory.current`, NVML(SM 사용률, PCIe rx/tx, clock, 전력)도 읽는다. 모든 시각은 CLOCK_MONOTONIC ns | G1, G2 |
| DDR·LLC 수집 | `sudo perf stat -a -I 100 --per-socket -e uncore_imc/cas_count_read/,uncore_imc/cas_count_write/`로 소켓별 DDR 대역폭을 잰다. resctrl mon group으로 학습 group과 sandbox group의 MBM 대역폭과 LLC 점유를 따로 잰다 | G1, G2 |
| `phases.py` | verl의 `marked_timer`를 감싸 phase 시작·끝을 jsonl에 남긴다. vLLM sleep/wake_up과 weight sync 구간도 남긴다. 같은 사건을 Unix socket으로 실시간 방송한다 | G1, G2 조건 4·5 |
| `replay_requests.py` | SWE trace의 turn별 prompt·completion token 수와 동시 요청 수로 vLLM OpenAI API에 open-loop 요청을 보낸다. 요청마다 TTFT, TPOT, 끝 시각을 남긴다 | G2 추론 |
| `phase_shrink.py` | phase 방송을 받아 sandbox cgroup의 `cpuset.cpus`를 줄였다 되돌린다 | G2 조건 4 |
| `phase_evict.py` | phase 방송을 받아 tool call 중인 sandbox의 vCPU thread를 CPU 노드 코어로 옮겼다 되돌린다. `sandbox.py --move hand-off` 경로를 쓴다 | G2 조건 5 |
| `plot_g*.py` | 그림 1~7 | – |

sampler 자체가 쓰는 CPU를 잰다(목표 < 1 core). sampler는 관리 코어에서만 돈다.

## 4. G1: 학습 노드는 언제 CPU를 원하나

### 4.1 구성

| ID | 구성 | model | 설정 | 보려는 CPU 구간 |
|---|---|---|---|---|
| C1 | colocated sync (verl 기본) | Qwen2.5-3B full FT, GSM8K GRPO | offload 없음, vLLM sleep mode | 생성 phase의 엔진 thread, 전환 때 weight·KV 이동 |
| C1o | C1 + verl param·optimizer offload | 같음 | `param_offload`, `optimizer_offload` on | phase 경계의 H2D/D2H, pinned buffer, DDR 대역폭 |
| C2 | optimizer CPU offload | Qwen3-8B full FT, GSM8K GRPO | FSDP2 `CPUOffloadPolicy` (CPU Adam) | update phase에 CPU Adam이 코어를 다 쓰는 구간 |
| C3 | 학습·생성 분리 async | Qwen2.5-3B | GPU0 = vLLM 생성, GPU1 = 학습. verl `recipe/one_step_off_policy` | 학습 GPU 쪽 optimizer step, weight 전송 구간 |
| C4 | 실제 agentic SWE (기존 `train.py`) | Qwen3-32B LoRA | 1.2절 설정 그대로, Docker sandbox 2개 동시 | 실제 workload. sandbox CPU는 cgroup으로 따로 센다 |

- C1~C3의 task는 math다. 설계 문서처럼 GPU 쪽 phase 구조만 실제와 같으면 된다. 각 20 step을 돌리고 앞 2 step은 warmup으로 버린다.
- C4는 step 하나가 약 85분이라 2 step만 돈다. 실제 RL과 phase 길이 비율을 맞춰 보는 확인용이다.
- C3에 필요한 verl recipe는 pip 패키지에 없다. verl 0.6.1 소스의 `recipe/`를 받아 rllm 패치와 같이 도는지 먼저 확인한다. 안 되면 C3는 빼고 그 사실을 적는다.

### 4.2 코어 sweep

C1과 C2를 학습 cpuset 32 / 16 / 8 thread로 돌려 step time을 잰다. 학습이 1% 이내 손해로 버티는 최소 코어 수가 나온다. 이 값이 G2 조건 2(고정 분할)에서 학습에 줄 코어 수다.

### 4.3 산출

- demand(t): 100 ms 구간마다 학습 쪽 owner의 thread CPU 합(코어 단위)
- borrowable(t) = budget − demand(t) − 1 (여유 1 core)
- phase별 표: 길이, 주기, demand 평균·p95·최대, DDR 대역폭 평균·최대, host 메모리 최대
- 예측 가능성: demand가 budget의 50%를 넘는 시간 중 특정 phase 안에 든 비율. phase 시작에서 그 구간까지의 lead time

### 4.4 그림과 표

- **그림 1 (phase timeline, 대표 그림)**: C2의 2 step. 가로축은 시간이고 세로로 4단을 쌓는다. 공통으로 phase 구간을 배경 색 띠로 칠한다.
  - (a) busy core: owner별 누적 면적. budget을 점선으로 긋는다.
  - (b) node0 DDR read/write GB/s
  - (c) host 메모리 사용량
  - (d) GPU SM 사용률

  C1, C3, C4는 같은 형식의 작은 그림으로 옆에 둔다. 읽는 법은 다음과 같다. 수요가 update나 offload phase에서만 솟고 rollout 동안 낮으면 G1 주장이 선다. 띠와 무관하게 평평하면 실패다.
- **그림 2 (빌릴 수 있는 코어 CDF)**: 구성별 borrowable(t)의 CDF. 고정 분할이 줄 수 있는 코어(= budget − 4.2의 최소 코어)를 세로선으로 긋는다. 선 오른쪽 면적이 동적으로만 빌릴 수 있는 몫이다.
- **그림 3 (코어 sweep)**: 학습 cpuset 크기별 step time. 구성별로 선 하나다.
- **표 1**: 4.3의 phase별 표.

### 4.5 관문 1

- 통과: C1, C2, C3 중 하나 이상에서 step 안의 (p99 demand − 평균 demand) ≥ 3.2 thread(노드 32 thread의 10%)이고, 그 구간이 phase로 설명된다(4.3 비율 ≥ 80%).
- 실패: 모든 구성에서 평평하다. 고정 분할로 충분하므로 G1 characterization만 정리하고 멈춘다.

## 5. G2: sandbox가 GPU 작업을 얼마나 늦추나

### 5.1 GPU 쪽 workload

| ID | 내용 | 한 점의 길이 | 지표 |
|---|---|---|---|
| W-inf32 | vLLM, Qwen3-32B TP=2. 설계 6.4대로 올릴 수 있는 가장 큰 model | 10분, 3회 | throughput, TTFT p99, TPOT p50 |
| W-inf8 | vLLM, Qwen3-8B TP=1 × 2 instance. kernel이 짧아 CPU에 민감한 쪽 | 10분, 3회 | 같음 |
| W-train | G1에서 수요 변화가 가장 큰 구성(C2 예상) | 5 step | step time, phase별 시간 |

- 추론 부하는 GPU 단독 최대 throughput의 70%를 open-loop로 고정한다. 그 속도는 noise floor 확인 때 정한다.
- 요청 token 분포는 `harness/trace-replay`의 SWE trace에서 뽑는다. 이 서버의 C4 trace(`trajectory-cache`)와 분포를 비교해 적는다.

### 5.2 sandbox 쪽

- **기본: Firecracker SWE replay** (`experiments/contention/sandbox.py pilot`, `--llm-model tokens --gap-scale 0.25 --placement home`). async screen과 같은 부하라서 CPU 노드 결과(실험 계획 v2 8장)와 바로 비교된다. 이후 실제 CXL 장치의 G4로도 그대로 이어진다. 준비물은 8장에 있다.
- **대안: R2E-Gym Docker 재생**. 이 서버에 이미 있는 image와 C4 trace의 명령을 같은 token model 대기로 재생한다. Firecracker 준비가 늦으면 G2 조건 1~3을 이것으로 먼저 본다. microVM이 아니므로 결과에 그렇게 표시한다.

### 5.3 실험 행렬

밀도는 GPU 노드에 올린 동시 sandbox 수다: 0, 50, 100, 200, 400.

| # | 조건 | 이 서버에서 구현 |
|---|---|---|
| 1 | 격리 없음 | sandbox cpuset = GPU 노드 32 thread 전체 |
| 2 | 고정 분할 | 학습에 4.2의 최소 코어, sandbox에 나머지 |
| 3 | 2 + 격리 | sandbox를 `chrt --idle 0`으로 띄운다. resctrl에서 sandbox group에 L3 way 일부(CAT)와 MBA 상한을 준다. GPU·NVMe IRQ는 학습 코어로 고정한다. 정확한 way 수와 MBA 값은 resctrl mount 후 2~3개 점으로 고른다 |
| 4 | 제자리 동적 축소 | 평소 sandbox cpuset = 학습 base 코어를 뺀 전부. phase 신호에서 2 core로 줄이고 끝나면 되돌린다 (`phase_shrink.py`) |
| 5 | 비우기 (hand-off 상한) | phase 신호에서 sandbox vCPU를 CPU 노드 코어로 옮기고 끝나면 되돌린다 (`phase_evict.py`). 메모리는 그대로다 |
| 6 | 메모리 위치, SMT | 조건 2~5에서 sandbox 메모리를 node0(GPU 소켓 DDR)과 node1(CXL 대용)에 각각 둔다. SMT on/off는 조건 2와 5만 본다 |

- 추론 workload에는 phase가 없다. 그래서 조건 4·5는 W-train에서만 의미가 있다. W-inf는 조건 1~3과 6을 본다.
- baseline:
  - GPU 단독(밀도 0)을 처음, 중간, 끝에 반복해 drift를 본다.
  - 같은 밀도의 sandbox를 CPU 노드에서 단독으로 돌려 sandbox 쪽 기준을 잡는다.
- 점의 실행 순서는 섞는다. 같은 조건을 연달아 돌리지 않는다.
- 대략의 시간이다. W-inf 2개 × 밀도 5 × 조건 1~3 × 3회 × 약 12분 ≈ 18시간이다. W-train은 밀도 {0, 100, 400} × 조건 1~5 × 5 step이다. step 길이는 G1 결과로 다시 계산한다. 조건 6은 W-inf32와 W-train에서 밀도 200만 본다.

### 5.4 그림

- **그림 4 (밀도별 GPU slowdown)**: workload마다 panel 하나(W-inf32 TTFT p99, W-inf32 TPOT p50, W-inf8 throughput, W-train step time). 가로축은 sandbox 밀도, 세로축은 GPU 단독 대비 slowdown(%)이다. 선은 조건별로 하나씩 긋고, 오차 막대는 반복 간 범위다. 1% 선과 noise floor 띠를 같이 그린다. 조건 2·3 선이 목표 밀도 전에 1%를 넘으면 G2 주장이 선다.
- **그림 5 (남는 간섭 분해)**: W-train, 밀도 200. 막대 하나가 조건 1→2→3→4→5에서 줄어드는 slowdown을 단계별로 보인다. 각 단계 차이에 대응하는 자원을 옆에 둔다.
  - 학습 group의 LLC 점유와 MBM 대역폭
  - vLLM engine thread의 run queue 대기(`/proc/<tid>/schedstat`)
  - 학습 코어의 IRQ 수

  4→5 차이가 "제자리에서 못 막는 간섭"이고, hand-off만의 가치다.
- **그림 6 (CXL 대용)**: 조건 2~5에서 sandbox 메모리가 node0일 때와 node1일 때를 나란히 둔다. node0 DDR 대역폭(MBM)과 GPU slowdown을 같이 보인다.

### 5.5 관문 2

- 통과: 조건 3이나 4로도 밀도 200 이상에서 W-train 또는 W-inf32의 slowdown이 1%를 넘는다. 그 차이는 noise floor의 2배보다 커야 한다.
- 실패: 조건 3이나 4로 1% 이하가 된다. hand-off가 필요 없으므로 멈춘다. 이때 "어떤 격리 조합이면 충분한가"를 결과로 남긴다.

## 6. G3: 같은 GPU 성능에서 몇 개를 올리나

- **정의**: GPU slowdown ≤ 1%이고 sandbox group 지연 ≤ 5%(CPU 노드 단독 대비)인 최대 밀도. 밀도 5단계 사이는 선형 보간하고, 보간한 값이라고 표시한다.
- **방식**:

  | 방식 | 근거 |
  |---|---|
  | 고정 분할 | 조건 2 |
  | 제자리 동적 축소 | 조건 4 |
  | 비우기(hand-off) | 조건 5 |
  | 비우기(migration) | 조건 5의 결과에 이동 비용을 더해 추정한다. 비용은 실제 장치의 full migration 측정(`implementation/virtual_machine/full-migration-comparison.json`)을 쓴다. 이 서버에서 잰 값이 아니므로 추정으로 표시하고, G4에서 다시 잰다 |

- **그림 7**: 방식별 GPU 노드당 최대 sandbox 수(막대, 오차는 반복 범위). 오른쪽 축에 "GPU 1,000장당 아끼는 CPU 노드 수"를 둔다.
- **표 2**: DSec scale unit(30K core) 기준 환산. CPU 노드 수와 cloud bursting 양이다.
- **관문 3**: hand-off 비우기가 제자리 축소보다 1.5배 이상 많이 올리면 다음 단계(G4, G5, 실제 장치)로 간다. 더 적으면 characterization과 격리 결과로 정리한다.

## 7. 순서

| 순서 | 일 | 끝나는 조건 |
|---|---|---|
| 1 | 도구(3장), 실행 환경 고정, noise floor | sampler 오버헤드 < 1 core, CV < 0.5% |
| 2 | G1 C1, C2, 코어 sweep | 그림 1~3 초안, 관문 1 판단 |
| 2' | (병행) Firecracker 준비(8장), small model 다운로드, C3 recipe 확인 | `sandbox.py pilot` 짧은 실행 성공 |
| 3 | G1 C3, C4 | 표 1 완성 |
| 4 | G2 W-inf 조건 1~3, 6 | 그림 4 추론 panel |
| 5 | G2 W-train 조건 1~5, 6 | 그림 4·5·6, 관문 2 판단 |
| 6 | G3 계산 | 그림 7, 표 2, 관문 3 판단 |

## 8. 준비물

| 항목 | 상태 | 할 일 |
|---|---|---|
| `kvm`, `docker` group | 2026-11-03 `ijkim` 추가함 | 새로 login해야 적용된다 |
| Qwen3-32B, Qwen2.5-3B-Instruct, Qwen3-8B | `/mnt/ssd1/Qwen/`에 공유 (2026-11-03) | 옛 경로 `/mnt/ssd1/hf-models/Qwen/<model>/current`는 링크로 남겼다 |
| verl `recipe/one_step_off_policy` | 없음 | verl 0.6.1 소스에서 가져와 확인한다 (C3) |
| Firecracker binary | 없음 | `implementation/virtual_machine/build.sh`로 빌드해 `.local/bin/firecracker`에 둔다 |
| guest kernel | 없음 | `.local/microvm/vmlinux-6.1.188`을 bud05에서 복사하거나 빌드한다 |
| rootfs image | 없음 | bud05의 `experiments/contention/.local/images/` 47개, 115 GiB를 복사한다. disk 여유 508 GB |
| native trace | 없음 (gitignore) | bud05의 `harness/trace-replay/data/native/`를 복사한다 |
| busybox, fakeroot, e2fsprogs | 확인 필요 | 없으면 설치한다 |
| resctrl, governor, GPU clock, SMT | 서버 전체 설정 | 다른 사용자와 시간을 맞추고 root로 바꾼다 (2.2) |

## 9. 결과를 남기는 곳

- 결과 문서는 `docs/results/gpu-harvest-g1-YYYYMMDD/README.md`처럼 관문별로 둔다. G1은 추가 측정과 함께 `docs/results/gpu-harvest-a6000-20261109/`에 두었다. 기존 `docs/results/` 형식을 따라 그림, 표, 해석 범위를 적는다.
- 원자료(sampler csv, phase jsonl, 요청 log)는 `experiments/gpu-harvest/results/<run>/`에 두고 gitignore한다. README에는 run 이름과 `env.json` 요약만 남긴다.

## 10. 해석할 때 주의할 점

- **PCIe GPU와 작은 model**: 둘 다 CPU 간섭을 실제보다 크게 보이게 한다(설계 6.4). 그림마다 W-inf32와 W-inf8을 같이 두어 크기에 따른 차이를 보인다.
- **CPU 노드가 같은 서버의 다른 소켓이다**: LLC와 DDR은 나뉘지만 UPI, 전력과 온도는 공유한다. 조건 5가 실제 hand-off보다 조금 불리할 수 있다.
- **node1 DDR은 CXL이 아니다**: 지연은 CXL보다 낮다. 조건 6은 "GPU 소켓 DDR을 안 쓰는 효과"만 보이고, CXL tax는 G5에서 잰다.
- **Docker 대안을 쓴 결과**는 microVM과 vCPU exit, EPT 동작이 다르다. 그림에 표시한다.
- **2 GPU, 1 노드**: 노드 간 NCCL과 IB NIC 경로가 없다. 다른 job과 섞이는 cluster 효과(설계 2장 표의 마지막 행)는 여기서 보지 않는다.
