# RL rollout trace 수집 runbook (2026-10-26)

[trace-collection-plan.md](trace-collection-plan.md)의 수집을 새 서버에서 처음부터 돌리는 순서다. 도구 설명은 [experiments/rollout-trace/README.md](../experiments/rollout-trace/README.md)에 있다. A6000 서버(deep8)에서 S0와 S1까지 이 순서로 확인했다(5절).

## 0. 원칙

1. **다른 사용자의 image와 데이터를 절대 훼손하지 않는다.** prune과 `rmi -f`는 쓰지 않는다. image와 container 삭제는 `docker_safety.py`와 `trace.run` label을 거쳐서만 한다. 전용 cloud node여도 같은 도구를 쓴다.
2. 네트워크, CPU, disk를 나눠 쓴다. 다운로드는 속도를 제한하고, 오래 걸리는 작업은 `nice`와 `ionice`로 돌린다. 공용 disk는 모델 2개 정도까지만 둔다.
3. 쓰기 전에 GPU와 port가 비었는지 확인한다(`nvidia-smi`, `ss -ltn`). `serve_vllm.sh`도 이를 확인한다.

## 1. 서버 확인 (계획서 3절)

```bash
nvidia-smi; nvidia-smi topo -m
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv   # 다른 사용자가 쓰는 GPU
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi -L
docker info --format '{{.DockerRootDir}} {{.CgroupDriver}} {{.CgroupVersion}}'   # cgroup2 + systemd
df -h; nproc; free -g; timedatectl
ss -ltn                                                                  # 18000, 19000이 비었는지
docker login                                                             # Docker Hub 익명 pull 제한이 낮음
```

HF token은 필요 없다. 모델 4개와 dataset 3개가 모두 공개이고 gated가 아니다.

## 2. 설치

```bash
git clone git@github.com:zktsd813/agent-sandbox-cxl.git && cd agent-sandbox-cxl && git checkout cloud
cd experiments/rollout-trace
export DATA_ROOT=/data/ijkim ENV_ROOT=$HOME/rollout-env   # 서버에 맞게
setup/setup_env.sh                                         # venv 3개, 약 10분
```

## 3. 모델과 dataset

```bash
nice -n 19 ionice -c3 setup/fetch_hf.sh dataset R2E-Gym/R2E-Gym-Subset 20M
nice -n 19 ionice -c3 setup/fetch_hf.sh model agentica-org/DeepSWE-Preview 20M
```

- 전용 회선이면 속도 제한(세 번째 인자)을 올려도 된다. 1Gbps 공용 회선에서 20MB/s면 모델 하나에 약 1~2시간 걸린다.
- **DeepSWE-Preview는 FP32(131GB)로 올라와 있다.** 받은 뒤 BF16으로 바꾸면 62GB가 되고, vLLM이 올리는 모델은 같다. 변환은 shard 단위로 써서 검증하고 바꿔치기하므로 추가 disk는 shard 하나만 쓴다(deep8에서 약 30분).

  ```bash
  docker run --rm --label trace.run=convert --user $(id -u):$(id -g) -e HOME=/tmp \
    --cpuset-cpus 4-7 --cpu-shares 128 --memory 32g \
    -v $DATA_ROOT/models/agentica-org--DeepSWE-Preview:/model \
    -v $PWD/setup/to_bf16.py:/to_bf16.py:ro \
    --entrypoint python3 vllm/vllm-openai:v0.28.0 /to_bf16.py /model
  ```
- Qwen3-Coder-30B-A3B-Instruct는 BF16(57GB)이라 변환하지 않는다. Qwen3-32B, DeepCoder-14B는 아직 받아 보지 않았다.

## 4. 시험과 수집

```bash
PY=$ENV_ROOT/venv-r2e/bin/python
$PY selftest.py /tmp/selftest          # GPU 없이: proxy, events, sampler, safety (11개 항목)

# GPU 없이 수집 경로 전체: 가짜 vLLM이 첫 턴에 finish를 냄
$PY fake_vllm.py --port 18000 &
$PY driver.py --workload W1 --model-tag fake --llm hosted_vllm/fake \
  --tasks tasks/W1.jsonl --n-tasks 1 --g 2 --concurrency 2 --root /tmp/e2e --run e2e
kill %1

# 실제 모델 (deep8: GPU1,2 FP8. H100: GPUS/CPUSET을 바꾸고 BF16 그대로)
./serve_vllm.sh $DATA_ROOT/models/agentica-org--DeepSWE-Preview deepswe --quantization fp8 &
$PY driver.py --workload W1 --model-tag deepswe --llm hosted_vllm/deepswe \
  --tasks tasks/W1.jsonl --n-tasks 1 --g 1 --concurrency 1 \
  --root $DATA_ROOT/traces/s1 --run s1 \
  --sandbox-cpus 0-15,32-47 --tool-cpu 24
```

- 각 실행 후 `_global/removal_log.jsonl`과 `docker images`를 `_global/baseline_images.json`과 비교해서, 원래 있던 image가 그대로인지 확인한다.
- vLLM을 멈출 때는 `docker stop trace-vllm-<name>`을 쓴다. `--rm`으로 띄웠으므로 container는 따로 지울 필요가 없다.

## 5. deep8에서 확인한 것 (2026-10-25)

**서버**
- RTX A6000 3장이다. GPU0은 다른 사용자의 vLLM이 쓰고 있어서, NV4로 연결된 GPU1·2(NUMA1, CPU 16-31·48-63)만 썼다.
- Xeon Gold 6326 2 socket, 64 thread, 314GB 메모리다.
- 공용 서버(접속자 13명)라 port 8000이 이미 쓰이고 있었다.

**vLLM** (v0.28.0, digest `61fc8a…`, DeepSWE FP8 weight-only Marlin, TP2)
- weight 적재 80초, KV cache 25.3GiB = **207,424 token**이다.
- 36,864 token 요청 기준 동시 5.6개, 평균 context 16K 기준 약 13개다.
- 요청 하나만 있을 때 decode는 약 34 token/s다.

**S1** (W1 aiohttp task 1개 × 1회, temperature 1.0, 50 step, 36,864 token)
- 보상 1.0으로 문제를 풀었다. agent가 스스로 끝냈고, 35 step이라 잘리지 않았다.
- LLM 35턴. context 2.7K → 16.9K token, turn당 출력 평균 870 token, 마지막 턴 cached token 16,736개.
- exec 67개(0이 아닌 exit 6개), 실행 시간 합계 7.6초. `put_archive` 4건(R2E-Gym 도구 파일).
- cgroup 10ms 간격 95,497행(955초). CPU 4.8초 사용, 메모리 peak 74MB.
- 전체 약 16분 중 LLM 대기가 933초다. 순차 수집이라 sandbox는 거의 놀았다.
- image 받기 43초(약 1.2GB), 정리할 때 우리가 받은 image만 지웠고 원래 있던 image 27개는 그대로였다.

**알게 된 것**
- R2E-Gym 설치는 `uv pip install -e`가 의존성 해석에서 멈춘다. repo의 `uv.lock`으로 `uv sync --frozen` 하면 바로 된다.
- HF 다운로드 중 연결이 자주 끊긴다(DeepSWE 하나에 15번). `fetch_hf.sh`가 이어받고 크기를 검증한다.
- Harbor는 Python 3.12 이상이 필요하다.
- R2E-Gym은 container를 멈출 때 10초 timeout을 기다린다. rollout마다 약 10초가 더 든다.
- TTFT는 잴 수 없다(README "아직 없는 것" 참고).

## 6. 다음 단계

계획서 9절의 S2 smoke다. 모델별로 묶어 바꾸는 횟수를 줄인다.
1. DeepSWE: W1 2회, W5 2회. W5는 SWE-rebench dataset과 task 선택 script가 필요하고, R2E-Gym이 SWE-rebench image를 돌리는지 확인해야 한다.
2. Qwen3-32B: W1 2회.
3. Qwen3-Coder: W2(mini-swe-agent), W3(Harbor, `delete` 끄기).
4. DeepCoder: W4. 실행기를 새로 작성한다.
