# GPU 노드 harvest 실험 도구 (A6000 × 2)

[A6000 실행 계획](../../docs/gpu-node-harvest-a6000-plan.md)의 G1과 그 뒤 추가 측정에 쓴 스크립트다. 결과와 해석은 [docs/results/gpu-harvest-a6000-20261109](../../docs/results/gpu-harvest-a6000-20261109/README.md)에 있다. 원자료는 `results/<run>/`에 쌓이고 git에 넣지 않는다.

## 전제

- `.venv-train`(verl 0.6.1, vLLM 0.11, torch 2.8 cu128)과 서버의 기존 agentic RL 환경(`setup.sh`, `train.py`, `dependency_patches.py`)이 저장소 루트에 있어야 한다. 이 파일들은 git에 없다.
- model은 `/mnt/ssd1/Qwen/<model>`에서 읽는다.
- GSM8K는 `prepare_gsm8k.py`로 `data/gsm8k/`에 만든다.
- C3는 verl 0.6.1 소스의 `recipe/`를 `.local/verl-v0.6.1`에 둔다.
- DDR 카운터, perf, sched trace는 `sudo -n perf`를 쓴다.

## G1

| 파일 | 내용 |
|---|---|
| `run_g1.sh CONFIG OUT [STEPS] [CPUS]` | C1, C1o, C2, C3을 학습 cpuset 안에서 돌리고 sampler와 phase 기록을 붙인다. `NGPUS`, `OMP`, `SMALL`(model), `SAVE`, `HARVEST_CUDA_BLOCKING` 환경변수 |
| `run_c4.sh OUT HOURS [CPUS]` | C4(`train.py`, Qwen3-32B LoRA, Docker sandbox). container CPU는 Docker scope별로 sampling |
| `sampler.py` | 100 ms마다 core별·thread별 CPU, NVML, 메모리, 소켓별 DDR. 시각은 CLOCK_MONOTONIC ns |
| `hook/sitecustomize.py` | `PYTHONPATH`에 두면 verl `marked_timer` phase를 `HARVEST_PHASE_LOG`에 남긴다. `HARVEST_CUDA_BLOCKING=1`이면 CUDA 대기를 blocking sync로 바꾼다 |
| `analyze_g1.py RUN` | `analysis.json`과 `fig1-timeline.png`. `--cdf`로 빌릴 수 있는 core CDF |
| `prepare_gsm8k.py` | verl 규칙 보상 형식의 GSM8K parquet |

## `bench/`: spin-wait와 주파수

| 파일 | 내용 |
|---|---|
| `spin_test.py spin\|blocking` | GPU 작업 약 3초를 기다리는 동안 host thread의 CPU 시간 |
| `micro_sync.py MODE CORE GPU` | GPU 대기 직후 Python 작업 시간(깨어나는 지연과 주파수 하락 분리) |
| `gen_bench.py LABEL` | verl 밖에서 vLLM 생성 시간(batch 320, 16) |
| `pause.c` | 채움·busy loop용 pause loop. `cc -O2 -o pause pause.c` |
| `pstat_phase.py LABEL RUN` | `perf stat -t`로 잰 GPU worker의 phase별 on-CPU 비율, 실효 GHz, 명령어 수. `PSTAT_DIR`에 csv |
| `sched_watch.sh OUT` / `sched_gaps.py OUT RUN` | blocking run의 10초 sched trace와 thread별 일하는·쉬는 구간 |
| `c4_analyze.py RUN` | C4 phase별 CPU, sandbox CPU, borrowable core(`c4-analysis.json`) |

## `decode/`: 추론 엔진의 host CPU와 sandbox 동거

모두 GPU0, CPU 4-7번 기준이다.

| 파일 | 내용 |
|---|---|
| `prof_vllm.py LABEL BATCH SECONDS` / `run_prof.sh` | vLLM V1 decode step의 함수별 wall·thread CPU |
| `async_bench.py BATCH ASYNC` | vLLM async scheduling의 step 시간과 프로세스 트리 CPU |
| `msdecode.py` | K decode step을 CUDA graph 하나로 묶은 엔진(`bench BATCH K SECONDS`) |
| `msprof.py` | 위 엔진의 GPU kernel 시간 분해 |
| `run_hog.sh N BATCH vllm\|K` | core마다 busy loop N개를 붙이고 vLLM 또는 K-step 엔진 실행 |
| `cbreq.py` | 두 엔진이 같이 쓰는 요청 stream(GSM8K prompt 96 token, 출력 64~512) |
| `msengine.py BATCH K sync\|overlap SECONDS` | 경계 작업(detokenize, stop 검사, 요청 교체 prefill graph)을 넣은 continuous batching K-step 엔진 |
| `vllm_cb.py BATCH SECONDS` | 같은 요청 stream의 vLLM continuous batching. `ASYNC=1`이면 async scheduling |
| `sbreplay.py OUT STREAMS SECONDS CPUSET` | C4 trace의 sandbox 명령(setup, tool call, grading)을 새 container에서 연달아 재생 |
| `run_cb.sh` | 엔진 4종 × sandbox 0·2 stream + sandbox 단독, 2회, 순서 섞음. 출력은 `results/decode-cb/`(`OUT`으로 바꿈) |
| `analyze_cb.py DIR` | 엔진 처리량·CPU와 sandbox 명령 slowdown 요약 |
