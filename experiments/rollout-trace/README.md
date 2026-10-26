# rollout-trace: RL rollout trace 수집 도구

[trace-collection-plan.md](../../docs/trace-collection-plan.md) 7절의 계측 도구다. A6000 서버(deep8)에서 만들고 S1(실제 모델로 1 task × 1회)까지 시험했다. 새 서버(H100 cloud 등)에서 처음부터 돌리는 순서는 [trace-collection-runbook.md](../../docs/trace-collection-runbook.md)에 있다.

## 1원칙: 공용 서버의 다른 사용자 데이터를 훼손하지 않는다

Docker image 저장소는 서버 전체가 공유한다. 그래서 이 도구들은 다음을 지킨다.

- `prune`(어떤 종류든), `docker rmi -f`를 쓰지 않는다.
- image 삭제는 `docker_safety.py`만 한다. 다음 조건을 모두 만족할 때만 force 없이 지운다.
  - 실행 시작 때 찍은 snapshot에 없던 image다.
  - 우리 `pull_log.csv`에 있는 image다.
  - 누구의 container도 그 image를 쓰지 않는다.
  - 우리가 받지 않은 tag가 붙어 있지 않다.
- container 삭제는 이번 실행의 `trace.run` label이 붙은 것만 한다.
- R2E-Gym 안의 위험한 코드는 쓰지 않는다.
  - `docker_bash_utils/remove_images.sh`: `docker rmi -f $(docker images -a -q)`, 서버의 모든 image를 지움
  - `repo_analysis/*`: `docker system prune -a`
- Harbor는 기본값 `delete=True`면 `compose down --rmi local --volumes`를 하므로 끄고 돌린다.
- R2E-Gym은 container를 다시 쓸 때 이름 일부만 맞아도 집는다. `r2e_rollout.py`에서 정확한 이름과 우리 label이 모두 맞을 때만 쓰도록 바꿨다.
- 받을 때마다 Docker root의 남은 공간을 확인한다(`--min-free-gb`, 기본 150GB).
- 네트워크와 CPU를 다른 사용자와 나눠 쓴다.
  - 모델 다운로드는 `setup/fetch_hf.sh`로 속도를 제한한다(기본 20MB/s). hf_xet 설정으로는 제한되지 않았다.
  - 오래 걸리는 작업은 `nice -n 19 ionice -c3`로 돌린다.
  - 다른 사용자의 GPU와 port는 쓰기 전에 비어 있는지 확인한다. deep8에서는 GPU0과 port 8000을 다른 사용자가 쓴다.

## 도구

| 파일 | 하는 일 |
|---|---|
| `docker_safety.py` | baseline snapshot, pull 기록(digest, 크기, 시간), 조건을 확인한 뒤 삭제 |
| `llm_proxy.py` | vLLM 앞의 기록용 proxy. `/r/<rollout>/v1/...`. 요청마다 `llm.jsonl`에 기록하고 요청 본문은 `llm_req/*.json.gz`에 저장 |
| `docker_events.py` | 이번 실행의 container·exec 이벤트를 ns 시각으로 기록. `--split`이면 rollout별 `exec.jsonl`, `container.jsonl`로 나눔 |
| `cgroup_sampler.py` | container별 cgroup. CPU는 10ms, PSI·메모리·IO는 100ms. 서버 전체 PSI와 load도 함께 기록(오염 판정용) |
| `vllm_metrics.py` | vLLM `/metrics`를 1초 간격으로 기록(실행 중·대기 요청 수, KV 사용률, prefix cache) |
| `r2e_rollout.py` | R2E-Gym rollout 하나. container 이름·label·cpuset을 넣고, `put_archive`로 넣은 파일 내용을 기록 |
| `driver.py` | 위 도구들을 띄우고 task(image) 단위로 받기 → G개 실행 → 정리를 반복 |
| `manifest.py` | 하드웨어, 버전, commit 기록 |
| `serve_vllm.sh` | 고정한 vLLM image로 서버를 띄움. GPU와 port가 비었는지 먼저 확인 |
| `select_tasks.py` | W1 task 512개(공용 128개 포함) 선택. seed 고정. 결과는 `tasks/W1.jsonl` |
| `selftest.py` | GPU 없이 proxy·events·sampler·safety 시험 |
| `fake_vllm.py` | 첫 턴에 `finish`를 내는 가짜 vLLM. GPU 없이 수집 경로 전체를 시험할 때 씀 |
| `setup/setup_env.sh` | scaffold별 venv 3개를 고정 버전으로 만듦 |
| `setup/fetch_hf.sh` | HF 모델·dataset을 속도 제한, 이어받기, 재시도, 크기 검증과 함께 받음 |
| `setup/to_bf16.py` | FP32 checkpoint를 shard 단위로 BF16 변환(DeepSWE-Preview는 FP32 131GB로 올라와 있음) |
| `estimate_time.py` | GPU 시간 대략 추정 |

## 환경 변수

| 변수 | 기본값(deep8) | 뜻 |
|---|---|---|
| `DATA_ROOT` | `/mnt/ssd1/ijkim` | 모델(`models/`), dataset(`datasets/`)을 두는 곳 |
| `ENV_ROOT` | `~/rollout-env` | venv와 R2E-Gym checkout, `VERSIONS` |
| `GPUS`, `TP`, `PORT`, `CPUSET` | `1,2`, `2`, `18000`, `16-23,48-55` | `serve_vllm.sh`의 vLLM 배치 |

도구는 `$ENV_ROOT/venv-r2e`로 실행한다. proxy 기본 port는 19000, vLLM은 18000이다.

## 출력

```
<root>/<W>/<model>/<task>/g<k>/
  meta.json  result.json  llm.jsonl  llm_req/  exec.jsonl  container.jsonl
  put_archive.jsonl  put_archive/  cgroup/<cid>.csv  scaffold/trajectory.json
<root>/_global/
  docker_events.jsonl  vllm_metrics.csv  host_pressure.csv  manifest.json
  baseline_images.json  pull_log.csv  removal_log.jsonl  driver.jsonl  jobs/  logs/
```

계획서 7절은 `traces/<workload>/<model>/<task_id>/g<k>/` 구조를 그렸는데 같은 구조다. task id는 `/`를 `_`, `:`를 `@`로 바꿔 쓴다.

## 아직 없는 것

- W2(mini-swe-agent), W3(Harbor), W4(DeepCoder 실행기), W5(SWE-rebench task 선택과 R2E-Gym 호환 확인). D3 smoke에서 작성한다.
- TTFT. R2E-Gym은 streaming 없이 요청하므로 proxy가 보는 첫 byte 시각이 완료 시각과 같다. 필요하면 proxy가 vLLM 쪽으로만 streaming으로 요청하도록 바꾼다.
