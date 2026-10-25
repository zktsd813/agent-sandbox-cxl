# RL 학습 sandbox trace 수집을 위한 조사 (2026-10-23)

목적: H100을 빌려 실제 agentic RL(GRPO) 학습을 돌리고, 그때 나오는 sandbox trace(CPU, tool call 시각, LLM 지연, sandbox 생성·배치)를 기록한다. 이 문서는 문제 set과 image, 모델, framework, SWE 외 sandbox 환경을 조사한 결과다. 숫자는 모두 출처에서 확인한 값이고, 확인하지 못한 것은 "(미확인)"으로 적었다. image 크기는 Docker Hub의 압축 크기다.

## 1. image가 있는 SWE 문제 set

| 문제 set | Hugging Face | 규모 | image | 보상용 테스트 | 학습에 쓴 예 |
|---|---|---|---|---|---|
| R2E-Gym-Subset | [R2E-Gym/R2E-Gym-Subset](https://huggingface.co/datasets/R2E-Gym/R2E-Gym-Subset) | 4,578개 (전체 8.1K, Python 13개 repo) | `docker_image` 열, `namanjain12/<project>_final:<commit>`. 1.5~1.9GB | 테스트별 PASSED/FAILED | DeepSWE-Preview (Qwen3-32B, 64 H100 × 6일, step당 64×8=512 container) [[blog]](https://www.together.ai/blog/deepswe), SA-SWE-32B (16 H100, 64×8) [[arXiv]](https://arxiv.org/html/2511.16108v1) |
| SWE-Gym | [SWE-Gym/SWE-Gym](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) | 2,438개 (Python 11개 repo), Lite 234 | `xingyaoww/sweb.eval.x86_64.<owner>_s_<repo>-<num>`. 약 1.1GB | FAIL_TO_PASS, PASS_TO_PASS | SkyRL mini-swe-agent 예제, SkyRL-v0 (293개 부분집합) |
| SWE-rebench | [nebius/SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) | 21.3K (+6.5K 정제), Python 3,400개 repo | `swerebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>`, 약 7,500개. 약 1.3GB | FAIL_TO_PASS, PASS_TO_PASS | Nebius: Qwen2.5-72B DAPO, 16×8 H200, task당 10 rollout. **sandbox마다 Kubernetes pod, 0.5 CPU / 2GiB** [[arXiv]](https://arxiv.org/html/2508.03501v2) |
| SWE-rebench-V2 | [nebius/SWE-rebench-V2](https://huggingface.co/datasets/nebius/SWE-rebench-V2) | 32,079개, 20개 언어 | `swerebenchv2/<owner>-<repo>:<PR>-<sha>` (project별 repo). 약 1.4GB | FAIL_TO_PASS, PASS_TO_PASS | RL 학습 보고 없음 |
| SWE-smith | [SWE-bench/SWE-smith](https://huggingface.co/datasets/SWE-bench/SWE-smith) | 약 50K, 128개 repo | **repo당 image 하나** (약 294개), `jyangballin/swesmith.x86_64.*`. 약 1.3GB | FAIL_TO_PASS, PASS_TO_PASS | SWE-agent-LM (SFT). SkyRL GRPO에 쓰였다고 repo에 적힘 |
| Multi-SWE-RL | [ByteDance-Seed/Multi-SWE-RL](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-RL) | 4,723개, Java/TS/JS/Go/Rust/C/C++ | `mswebench/<org>_m_<repo>:pr-<N>`, 약 0.6~0.9GB. 없으면 직접 build | f2p/p2p 등 | 학습 모델 (미확인) |
| Nemotron RL SWE Pivot | [nvidia/Nemotron-RL-Agentic-SWE-Pivot-v1](https://huggingface.co/datasets/nvidia/Nemotron-RL-Agentic-SWE-Pivot-v1) | 6,436개 (SWE-Gym + R2E-Gym 재구성) | 원본 image를 Apptainer `.sif`로 변환 | 원본과 같음 | Nemotron 3 Super: 512 GPU, agent 동시 768개, 16×32, 200 turn [[docs]](https://docs.nvidia.com/nemotron/latest/nemotron/super3/rl/swe.html) |
| SWE-bench Verified | [princeton-nlp/SWE-bench_Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) | 500개 | `swebench/sweb.eval.*`, Epoch 축소판 500개 합계 30GiB [[Epoch]](https://epoch.ai/latest/swebench-docker) | 있음 | **평가 전용. 학습에 쓰지 않음** |

- image 하나가 압축 1~2GB라, 수천 개면 TB 단위 disk가 필요하다. DeepSWE는 CPU node(약 200코어, NVMe 6TB)에 image를 미리 받아 두었다.
- SWE-smith(repo당 image 하나)와 SWE-rebench-V2(project별 repo)는 받아야 할 image 수가 훨씬 적다.
- 그 밖에 OpenSWE(Dockerfile만 공개, 직접 build), SWE-Factory, SWE-Dev, SWE-Flow는 image 공개 여부를 확인하지 못했다.

## 2. 모델

| 용도 | 모델 | 비고 |
|---|---|---|
| RL로 학습된 SWE checkpoint | [agentica-org/DeepSWE-Preview](https://huggingface.co/agentica-org/DeepSWE-Preview) (Qwen3-32B) | Verified 42.2%. R2E-Gym scaffold |
| | NovaSky-AI/SA-SWE-32B (Qwen3-32B) | 39.4%. model card 비어 있음 |
| | [moonshotai/Kimi-Dev-72B](https://huggingface.co/moonshotai/Kimi-Dev-72B) (Qwen2.5-72B) | 60.4% |
| 작은 RL checkpoint | NovaSky-AI/SkyRL-Agent-7b-v0 / 8b-v0 / 14B-v0 | 성능 낮음(9~22%). model card 비어 있음 |
| 1노드(8×H100)에서 학습 시작점 | Qwen3-8B | SkyRL mini-swe-agent 예제. SWE-Gym은 8B에 어려워 보상 0이 많다고 README가 경고 |
| 2노드에서 학습 시작점 | Qwen3-Coder-30B-A3B | SkyRL 예제 (2×8 H100) |

## 3. RL framework

| framework | SWE recipe와 공개 하드웨어 | sandbox 방식 | 동기/비동기 | 계측 위치 |
|---|---|---|---|---|
| **SkyRL (mini-swe-agent 예제)** [[repo]](https://github.com/NovaSky-AI/SkyRL/tree/main/examples/train/mini_swe_agent) | Qwen3-8B, 8×H100 1대, 기본 batch 16 × group 4, 20 step 제한. SWE-Gym | trajectory마다 Ray task 하나가 그 Ray node에서 docker/podman을 실행. step마다 전부 한꺼번에 시작 | 동기 batch | `mini_swe_generator.py`의 실행·생성 지점. container라 cgroup으로 CPU를 직접 잴 수 있음 |
| SkyRL-Agent (SA-SWE) | 2×8 H100, 64×8, 50 turn | OpenHands 원격 runtime 필요 | 비동기 pipeline (준비·실행·보상을 queue로 겹침) | Harbor 연동 시 단계별 시간 기록 |
| rLLM (DeepSWE) [[repo]](https://github.com/rllm-org/rllm) | DeepSWE는 64 H100, Kubernetes (dockerd가 수백 개에서 죽어서) | Docker/Daytona/Modal 공통 interface. sandbox를 미리 만들어 두는 warm queue | 비동기, partial rollout | sandbox protocol wrapper |
| verl [[docs]](https://verl.readthedocs.io/en/latest/advance/rollout_trace.html) | SWE recipe가 main에 없음 | 사용자가 agent loop 작성. Sandbox Fusion, Daytona, Modal | 비동기 agent loop | `rollout_trace_op`로 tool call 입출력·시각 기록 |
| NeMo-RL + NeMo Gym [[docs]](https://docs.nvidia.com/nemo/gym/v0.5.0/infrastructure/engineering-notes/swe-rl-case-study.md) | Qwen3-30B-A3B, 24 node. 512 rollout에 step당 약 20분 | Slurm + Apptainer, 작업당 약 1코어 | 비동기 | 문서화된 것 없음 |
| ROLL / RollArt [[repo]](https://github.com/alibaba/ROLL) | Qwen3.5-2B를 8 GPU에서 (시험 규모). RollArt는 3,000 GPU 이상 | ROCK sandbox service, 환경을 CPU cluster로 분리 | 환경 단위 비동기 | (미확인) |
| AReaL, slime, prime-rl, Agent Lightning | SWE 예제 있음, 하드웨어 대부분 미확인 | 원격 sandbox service 위주 | 비동기 | 일부 profiling 문서 |

## 4. SWE 외 sandbox 환경

| 환경 | sandbox | CPU 성격 | RL 사용 예 |
|---|---|---|---|
| Terminal (Terminal-Bench 2.0, Endless Terminals, Harbor) [[harbor]](https://github.com/harbor-framework/harbor) | task별 Docker image | 매우 다양 (파일 편집부터 compile, 모델 학습까지) | OpenThoughts-Agent: sandbox당 1 vCPU / 2GB, 동시 280개, group 8 [[arXiv]](https://arxiv.org/html/2606.24855) |
| 경쟁 프로그래밍 + 테스트 보상 (DeepCoder, SandboxFusion) | 실행 service | rollout이 끝날 때 테스트(15개, 6~12초 timeout)가 group 전체에서 한꺼번에 몰림 | DeepCoder: iteration당 문제 1,024개, 32 H100 [[blog]](https://www.together.ai/blog/deepcoder) |
| code interpreter 추론 (ReTool, SimpleTIR, rStar2) | stateless Python 실행 service | 매우 짧고 잦음 (호출당 약 0.3초) | rStar2: 512 prompt × 32 → 16 [[arXiv]](https://arxiv.org/html/2508.20722v1) |
| ML engineering (MLE-bench-lite, SandMLE) | Docker | 길고 무거움 (실행당 14~196초, 메모리 큼) | SandMLE: group 4, batch 16 [[arXiv]](https://arxiv.org/html/2604.04872) |
| Computer use (OSWorld) | QEMU VM (KVM) | desktop이 계속 CPU를 씀. 인스턴스당 10코어 / 40GB 예 | ComputerRL: 동시 1,024, group 16 [[arXiv]](https://arxiv.org/html/2508.14040) |
| Tool/API (AppWorld, τ²-bench) | Python process | 대부분 놂. AppWorld는 turn이 늘수록 매번 기록을 재실행해 CPU 증가 | LOOP: 40 task × 6 [[arXiv]](https://arxiv.org/html/2502.01600) |
| 검색·deep research, 텍스트 게임 | 검색 server, Python | CPU 거의 없음 | — |

DSec는 backend를 네 가지로 나눈다. 함수 호출(judge 코드), container(SWE, tool, terminal), Firecracker microVM(security), QEMU VM(Android, GUI) [[arXiv]](https://arxiv.org/html/2609.22978v1).

## 5. trace 수집 계획에 주는 시사점

1. **framework는 SkyRL mini-swe-agent 예제가 가장 맞다.**
   - 지금 264개 trace와 같은 mini-swe-agent harness라 기존 데이터와 바로 비교된다.
   - 8×H100 1대(Qwen3-8B)로 시작할 수 있다.
   - sandbox가 Ray task가 실행하는 일반 container라, CPU server를 Ray worker로 붙이고 cgroup으로 sandbox별 CPU를 잴 수 있다. 다만 sandbox를 CPU node에 고정하려면 Ray custom resource를 쓰는 작은 코드 수정이 필요하다.
   - group 크기는 8로 올린다(`n_samples_per_prompt`).
2. **문제 set은 SWE-Gym 또는 R2E-Gym-Subset.** 둘 다 image가 공개되어 있고 SkyRL·DeepSWE recipe가 있다. image 수를 줄이려면 SWE-smith가 낫다.
3. **sandbox 종류.** 이 연구는 microVM(Firecracker)이 대상이다. 두 가지 방법이 있다.
   - container로 그대로 기록한다.
   - SkyRL의 실행부를 우리 Firecracker runner로 바꾼다. Docker image를 rootfs로 바꾸는 pipeline(`experiments/contention/sandbox.py build`)은 이미 있지만, image 수천 개를 변환하는 비용이 든다.
4. **CPU 쪽 규모.** NeMo는 동시 작업 하나에 약 1코어가 필요하다고 측정했다.
   - batch 16 × group 8 = 128개면 128코어 이상이 필요하다.
   - 이 서버는 socket 1에 24 thread뿐이라, CPU node를 함께 빌려야 한다.
   - node 사이 불균형을 보려면 CPU node가 여러 대여야 하고, 배치 정책(흩기 / image locality)도 바꿔 볼 수 있어야 한다.
5. **기록할 것.**
   - sandbox별 10ms CPU (cgroup `cpu.stat`, `cpu.pressure`)
   - tool call 시작·끝 시각
   - LLM 요청·응답 시각
   - sandbox 생성·삭제 시각과 image pull 시간
   - step 경계
   - sandbox가 놓인 node
6. **주의.**
   - dockerd 과부하: DeepSWE가 수백 개에서 겪었다. 여러 host로 나누거나 podman을 쓴다.
   - image pull과 Docker Hub rate limit: 미리 받거나 local registry를 둔다.
   - disk: TB 단위가 필요하다.
   - 경합 왜곡: burst 모양은 경합 없는 밀도(sandbox당 1코어 이상)에서 재고, 경합은 실제 밀도에서 따로 잰다.
7. **SWE 외 환경도 함께 돌리면 좋은 것.**
   - terminal (Harbor): 가장 비슷한 RL recipe가 있고, CPU 사용이 다양하다.
   - 경쟁 프로그래밍 테스트 보상: group 전체가 끝날 때 함께 burst를 낸다.
   - code interpreter: 아주 짧은 burst가 잦다.
   - AppWorld나 τ²: 조용한 이웃으로, 섞인 부하를 볼 때 쓴다.
