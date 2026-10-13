# agent-sandbox-cxl

mini-swe-agent의 기록된 명령을 Docker에서 재실행하고 CPU·메모리·동시성을 측정하는 NUMA node 0 baseline이다. GPU, 모델 가중치, LLM API key는 사용하지 않는다. CXL 배치와 원격 worker, 노드 간 실행 상태 이동은 후속 단계다.

## 저장소 구성

| 파일 | 역할 |
|---|---|
| `harness/trace-replay/experiment.py` | trace 다운로드·검증, 시간 배치, 컨테이너 실행, NUMA·자원 제한, 계측·정리 |
| `harness/trace-replay/analyze.py` | 종료 코드·제출 내용 비교, CPU·메모리·동시성·지연 분석과 그래프 |
| `harness/trace-replay/configs/node0.json` | CPU pool, 작업별·전체 메모리 상한, PID 상한, 계측 주기 |
| `harness/trace-replay/data/inventory.json` | 원본 제출의 고정 commit, trace 경로·크기·SHA-256 |
| `harness/trace-replay/data/sample16.json` | 기존 pilot에 사용한 16개 파일 이름 |
| [기존 실험 보고서](docs/replay-report.md) | 완료한 pilot과 전체 실행 계획 |

Python 실행 파일은 두 개로 유지한다. 함수와 설정을 재사용하고, 새 framework나 여러 wrapper 파일을 추가하지 않는다. 원본 trace, Docker 이미지, 원시 계측 결과와 upstream 저장소 사본은 각 머신에서 준비한다.

## 다른 머신에서 준비

기존 검증 환경은 x86_64 Linux, Python 3.12, cgroup v2와 systemd driver를 사용하는 로컬 Docker daemon이다. 새 머신에도 Docker CLI, `numactl`, Python 3.11 이상과 `sudo -n`으로 필요한 systemd 자원 설정을 수행할 권한이 필요하다. rootless Docker와 원격 Docker daemon은 현재 실행 경로의 지원 대상이 아니다. task 이미지의 개발 도구는 컨테이너 안에 있으므로 호스트에 언어별 도구를 모두 설치할 필요는 없다.

```bash
git clone https://github.com/zktsd813/agent-sandbox-cxl.git
cd agent-sandbox-cxl
python3 -m venv .venv
. .venv/bin/activate
python -m pip install matplotlib
```

재생 프로그램은 Python 표준 라이브러리만 사용한다. `matplotlib`은 분석 그래프용이다. mini-swe-agent와 SWE-agent 패키지를 설치하거나 별도 checkout할 필요는 없다.

먼저 새 머신의 NUMA topology와 실행 권한을 확인한다.

```bash
numactl --hardware
lscpu -e=CPU,NODE
docker info --format '{{.Architecture}} {{.CgroupVersion}} {{.CgroupDriver}}'
sudo -n true
```

`configs/node0.json`의 `node_cpu_pool`, `controller_cpus`, `workload_cpus`를 새 머신의 NUMA node 0 CPU 번호에 맞춘다. controller와 workload CPU는 겹치지 않아야 한다. 기본값은 controller `0-1`, workload `2-5`, 최대 4개 작업이다. 아래 전체 실행 예제의 `2-31`도 새 머신에 맞게 바꾼다.

작업당 메모리 상한은 16GiB, PID 상한은 4096이다. 별도 부모 cgroup이 전체 합계를 최대 48GiB 및 실행 준비 시 node 0 free memory의 80% 이하로 제한한다. 작업별 상한 합계만큼 메모리를 예약하지 않는다. 전체 예산이 작업 하나의 상한보다 작으면 실행을 거부하므로 머신 용량에 맞게 두 상한을 함께 조정한다.

## trace 준비와 실행 계획 확인

원본은 `SWE-bench/20260902_mini-v2.4.6_gemini-3-5-flash`의 commit `8dcca98587e7f698753903e3b1747eb5dc013cb1`이다. 아래 명령은 264개 trace를 내려받고 크기와 SHA-256을 검증한다. 이미 있는 파일도 검증하며, 불일치 파일은 덮어쓰지 않고 오류를 낸다.

```bash
python harness/trace-replay/experiment.py --download-traces
python harness/trace-replay/experiment.py --plan \
  harness/trace-replay/data/native/*.traj.json > /tmp/mini-replay-plan.json
```

`--plan`은 Docker를 호출하거나 workload를 실행하지 않는다. 검증된 입력은 264개 trace, 13,600개 명령이며 기록상 작업 시간창 최대 12개가 겹친다. 전체 시간 범위는 6,822.64초, 약 114분이다.

## 한 작업부터 재생

```bash
python harness/trace-replay/experiment.py --pull \
  --output harness/trace-replay/results/smoke-gin \
  harness/trace-replay/data/native/gin-gonic__gin-3741.traj.json
python harness/trace-replay/analyze.py harness/trace-replay/results/smoke-gin
```

`--pull`은 측정 시작 전에 task 이미지를 준비한다. Docker Hub 다운로드 한도에 걸리면 해당 머신의 터미널에서 `docker login` 후 다시 준비한다. 인증 정보는 저장소에 넣지 않는다. 출력 디렉토리는 덮어쓰지 않으므로 재실행에는 새로운 이름을 사용한다.

## 전체 264개 재생

```bash
python harness/trace-replay/experiment.py --pull \
  --setup on-arrival --max-active 12 --workload-cpus 2-31 --keep-going \
  --output harness/trace-replay/results/full264 \
  harness/trace-replay/data/native/*.traj.json
python harness/trace-replay/analyze.py harness/trace-replay/results/full264
```

이 명령은 trace 사이의 기록된 상대 시작 시각과 각 명령 묶음의 timestamp를 유지한다. 264개를 한순간에 시작하지 않는다. 첫 명령 목표 시각에 컨테이너를 생성하고, 준비·슬롯 대기·명령 지연을 기록한다. `--keep-going`은 자원 제한으로 실패한 작업을 정리하고 다른 작업을 계속한다. 일반 명령의 nonzero 종료 코드는 원본과 비교하며, 그것만으로 실험 전체를 중단하지 않는다.

결과에는 `manifest.json`, `events.jsonl`, `metrics.csv`, 작업별 출력 로그가 생성된다. 분석 후 `summary.json`, `report.md`, `timeline.png`가 추가된다. 원시 결과는 Git에 올리지 않는다.

## 이어서 할 일과 해석 범위

기준 서버에서는 16개 trace의 680개 명령을 재생했다. 비교 가능한 종료 코드 664건 중 6건이 달랐고, 동일한 시작 경고의 위치를 정규화한 제출 내용은 16개 모두 일치했다. Preact의 PID·메모리 제한을 추가 검증해 현재 상한을 정했다. 전체 264개 실행은 당시 이미지 다운로드 한도로 중단했다. 기준 서버의 이미지 캐시는 다른 머신으로 전달되지 않는다.

새 머신에서 한 작업의 계측·NUMA 제한을 확인하고, 전체 replay 보고서를 완성하는 것이 다음 단계다. CPU가 낮은 시간에도 남는 익명 메모리와 파일 cache를 구분한 뒤 CXL 배치·회수 또는 실행 노드 변경과 비교한다.

- timestamp는 assistant 메시지 시각이다. 원래 sandbox 생성 시각이나 정확한 LLM 추론 시간을 뜻하지 않는다. trace 사이의 wall clock 비교 가능성은 실험 가정이다.
- 각 trace는 같은 컨테이너를 유지하고 명령마다 새 Bash를 실행한다. 기록된 다음 행동을 사용하므로 실행 결과에 따라 LLM이 계획을 수정하지 않는다.
- mini와 같이 timeout은 Docker CLI를 종료하고 기록한다. 컨테이너 내부 명령은 계속 살아 있을 수 있어 대기 중에도 계측한다. 세션 종료 때 컨테이너 전체를 정리한다.
- 익명 메모리의 node 0 제한과 이미 다른 node에 존재하는 공유 파일 cache의 사용은 구분한다. Docker daemon과 기존 호스트 서비스는 workload 계측 합계에 포함하지 않는다.
- 제출 내용 일치는 SWE 정답 채점 결과가 아니다. 이 replay만으로 DSec의 운영 sandbox 생성 burst나 CXL 효과를 검증했다고 해석하지 않는다.

원본 harness 참고용 checkout은 mini-swe-agent `04d809ceab9df28f9adaed044884180159172930`, SWE-agent `3ea751c087f32b16e039a2233dd6eefecef325d5`였다. 구현 참고가 필요할 때 각 upstream 저장소에서 해당 commit을 확인한다.
