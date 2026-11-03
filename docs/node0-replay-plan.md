# NUMA node 0에서 mini trace 실험 준비

> 초기 조사/실험 기록이다. 현재 실행 설정과 완료 결과는 [최신 README](../README.md)를 따른다.

확인일: 2026-10-07. 여기서 node 0은 현재 서버의 NUMA node 0이다. 현재 실행 환경 hostname은 `ubuntu`다. 원본 trace 264개를 다운로드·검증하고 16개 trace의 실제 Docker 재생을 완료했다. 680개 명령 중 종료 코드 차이는 6건이며, 제출 내용은 동일한 시작 경고의 위치를 제외하면 16개 모두 일치했다. 전체 실험은 공유 메모리 제한 재검증까지 완료했지만 Docker Hub 익명 다운로드 한도로 아직 시작하지 못했다. 현재 설정은 작업당 16GiB·PID 4096, 합계 최대 48GiB(시작 시 node 0 free의 80% 이하)의 공유 그룹이며 최초 4GiB·PID 512 검증 조건과 구분한다. 아래의 초기 준비 계획보다 최신 보고서와 실행 config를 우선한다. 최신 상태는 [실험 보고서](replay-report.md)를 따른다.

## 확인한 서버 상태

| 항목 | 현재 상태 |
|---|---|
| NUMA node 0 CPU | 온라인 논리 CPU `0-31`; 조회한 topology에서 서로 다른 core |
| NUMA node 0 memory | 약 125.6GiB 총량, 조회 당시 free 약 63.6GiB. Free는 변하는 값이며 실험 예산 전체로 취급하지 않음 |
| 다른 NUMA node | node 1은 CPU `32-63`과 약 31.5GiB 메모리, node 2는 현재 온라인 메모리 0 |
| Docker | 29.3.1, cgroup v2, systemd driver, 데몬 읽기 조회 성공 |
| Python | 3.12.3 |
| 도구 | `numactl`, `lscpu`, `git`, `perf`, `pidstat`, `vmstat` 있음 |
| 디스크 여유 | 약 101.6GiB |
| mini Python 패키지 | 현재 interpreter에 미설치; 받아 둔 소스는 있음 |

node 번호만으로 다른 노드의 메모리가 DRAM/CXL 중 무엇인지 판정하지 않았다. 첫 실험에서는 node 0의 일반 메모리 할당 경로로 제한하고 장치 재구성을 수행하지 않는다.

## 설치 또는 다운로드할 것

현재 replay 프로그램은 **Python 표준 라이브러리 + Docker CLI + numactl + systemd**를 사용한다. 필요한 호스트 프로그램은 설치돼 있다. 공유 메모리 상한을 적용하려면 비대화형 sudo 권한과 로컬 systemd Docker daemon이 필요하며, 이 서버에서 생성·제한·정리를 검증했다. mini의 trace와 Docker 실행 의미를 사용하며 전체 mini 패키지를 import하지 않는 구성이다.

필수로 준비할 데이터는 선택한 원본 `.traj.json`과 그 task image다. 호스트에 Go/Java/Node 등의 모든 개발 도구를 설치하지 않고 task image 안의 환경을 사용한다. 각 image가 필요한 의존성을 실제로 제공하는지는 준비 단계에서 확인한다.

Python 가상환경은 향후 분석 패키지를 격리하기 위해 권장한다. 결과 그래프에 `numpy`/`matplotlib`, 테이블 분석에 `pandas` 등을 추가할 수 있지만 재생 자체의 필수 의존성은 아니다. `uv`도 필수가 아니다.

현재 mini의 `DockerEnvironment`를 직접 import해 재사용하기로 바꾸면 mini 패키지 설치가 필요하다. 현재 `pyproject.toml`은 `pydantic`, `litellm`, `datasets` 등도 기본 의존성으로 포함하므로, 이 최소 replay 경로에서는 이를 모두 설치하지 않는다. GPU, 모델 가중치, LLM API key, SWE-ReX는 이 구성의 필수 요소가 아니다.

## 첫 task image 준비 상태

첫 후보는 `gin-gonic__gin-3741`이다. 공개 registry에서 amd64 image metadata 조회에 성공했다.

```text
docker.io/swebench/sweb.eval.x86_64.gin-gonic_1776_gin-3741
amd64 manifest digest:
sha256:c134cc213a09fd5060dd3d3a70adde0eb6607750c111f397b163a306d0ee5a1e
working directory: /testbed
compressed layers total: 360,615,062 bytes (약 344MiB)
```

압축 layer 합계는 다운로드 크기의 참고값이며 unpacked 디스크 사용량이 아니다. 현재 registry digest를 고정하면 이후 tag 변경을 피할 수 있지만 이것이 원 수집 시점과 같은 image임을 입증하지는 않는다. 이 image를 받아 컨테이너 제한과 trace 명령의 실제 실행을 검증했다. 전체 264개용 image는 현재 79개가 준비됐으며 나머지는 Docker Hub 로그인 후 다운로드해야 한다.

## NUMA 제한 적용

실험 컨테이너를 생성할 때 CPU와 memory node를 함께 제한한다. 최초 검증은 CPU `2-5`, 작업당 4GiB 및 PID 512를 사용했다. Preact 빌드에서 프로세스 상한이 부족했고 이를 높이자 4GiB에서 OOM이 발생했다. 현재는 작업당 16GiB·PID 4096과 공유 메모리 상한으로 재검증했다. 전체 실행은 CPU `2-31`, controller/관측기는 CPU `0-1`을 사용한다.

```text
--cpuset-cpus=2-31
--cpuset-mems=0
--memory=16g
--memory-swap=16g
--pids-limit=4096
```

Docker의 `--memory-swap`은 메모리와 swap의 합계이므로 `--memory`와 같게 설정해 swap을 막는다. CPU quota는 초기 비교에서 별도로 추가하지 않고 cpuset으로 실행 가능한 CPU를 제한한다. 추가 quota는 burst 중 throttling을 만들 수 있으므로 다른 실험 조건으로 취급한다. [Docker 자원 제한](https://docs.docker.com/engine/containers/resource_constraints/), [Docker run 옵션](https://docs.docker.com/reference/cli/docker/container/run/).

호스트 Python을 `numactl`로 묶는 것만으로 Docker 컨테이너가 같은 NUMA 정책을 상속한다고 가정하면 안 된다. 컨테이너는 Docker 데몬이 생성하므로 컨테이너 생성 옵션으로 제한해야 한다. 호스트 controller와 sampler에는 CPU `0-1`, memory node `0`을 별도로 적용한다.

컨테이너 시작 후 실제 cgroup의 `cpuset.cpus.effective`, `cpuset.mems.effective`, `memory.swap.max`와 실제 프로세스의 허용 범위를 확인한다. `memory.numa_stat` 및 권한이 허용되는 `/proc/<pid>/numa_maps`로 실제 페이지 배치를 관찰한다. 부모 cgroup 제약으로 effective 값이 요청과 다르면 실패로 처리한다. [cgroup v2 cpuset](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpuset-interface-files).

새 익명 메모리 할당의 node 0 제한과, 다른 프로세스가 이미 다른 node에 만든 공유 파일 cache 페이지의 사용은 구분한다. Docker daemon, 호스트 kernel, 다른 서비스의 모든 자원이 node 0에 제한되는 구성은 아니다. cgroup NUMA accounting만으로 모든 공유 매핑의 배치를 입증할 수도 없다. 다른 node의 메모리가 관측되면 유형과 범위를 기록하고 strict baseline 검증 상태를 표시한다. CPU cpuset은 다른 호스트 작업에 대한 독점 예약도 아니므로 경합을 기록한다. 전역 cache 제거와 다른 서비스 이동은 초기 준비에 포함하지 않는다.

실행 설정은 [node0.json](../harness/trace-replay/configs/node0.json)에 있다. 기본값은 CPU `2-5`와 최대 4개이고, 전체 실험은 실행 옵션으로 CPU `2-31`과 최대 12개를 선택한다. 작업별 메모리 상한 16GiB와 별도로, 전용 부모 cgroup이 모든 작업의 메모리 합계를 최대 48GiB 및 시작 시 node 0 free memory의 80% 이하로 제한한다. 작업별 상한의 합계를 예약하는 방식은 아니다. 실제 제한 적용과 그룹 정리를 검증했다.

## 코드 두 파일

사용자 요청에 따라 이전의 6개 모듈 계획을 두 파일로 통합했다. 기능별 함수는 같은 파일 안에 둔다. 개인 skill `minimal-readable-experiments`에도 코드량을 줄이되 읽기 쉬운 함수·이름·흐름을 유지하고, 이 실험은 두 Python 파일로 구성한다는 지침을 추가했다.

| 파일 | 책임 |
|---|---|
| [experiment.py](../harness/trace-replay/experiment.py) | trace 파싱/검증, 실행 계획, 컨테이너 생성, NUMA 제한, 동시 replay, 자원 측정, 로그와 정리 |
| [analyze.py](../harness/trace-replay/analyze.py) | 결과 요약, 종료 코드 차이/지연량/메모리 배치 확인, CPU·메모리·명령 시간 그래프 |

기존 JSON config와 생성된 결과 데이터는 코드 파일 수에 포함하지 않는다. 실행은 표준 라이브러리를 사용하고 그림 생성은 `matplotlib`을 사용한다. 현재 interpreter에서 matplotlib 사용과 모의 결과의 PNG 생성을 확인했다.

`load_trace`는 Docker trace를 읽고 필요한 image/cwd/env/interpreter를 사용한다. 원본 `run_args`와 host environment forwarding은 적용하지 않는다. timestamp 누락·음수 간격·불완전한 observation을 거부한다. 마지막 제출 명령에 tool observation이 없는 형식은 허용한다. `Submitted`를 정답 판정 성공으로 취급하지 않는다.

세션 동안 같은 컨테이너를 유지해 수정 파일과 background 프로세스를 보존하되 명령마다 새 Bash를 실행한다. trace의 interpreter를 따른다. 호출 간 shell `cd`/변수 상태를 임의로 유지하지 않는다. 명령 문자열은 host shell로 평가하지 않고 Docker 인수로 전달한다. host workspace, Docker socket, CXL DAX 장치를 task 안에 bind-mount하지 않는다.

명령 timeout은 mini와 같이 Docker CLI를 종료하고 -1로 기록한 뒤 다음 명령으로 진행한다. 컨테이너 안의 프로세스가 계속 실행될 수 있어 대기 중 사용량도 관측한다. 관측 실패/OOM/PID 상한 도달은 기본적으로 실험을 중단한다. `--keep-going`에서는 실패한 작업의 컨테이너를 제거하고 다른 trace를 계속 재생한다. 정리 실패는 항상 전체 중단을 요청한다. 일반 명령의 nonzero exit는 원본에도 존재할 수 있으므로 원본 return code와 함께 기록하고 무조건 중단하지 않는다. 재생 결과에 따라 다음 행동을 새로 결정하는 기능은 없다.

## 여러 trace를 timestamp에 맞춰 동시 실행

가능하다. 기본 옵션 `--schedule timestamps --starts recorded`는 첫 assistant timestamp의 상대 위치와 이후 assistant timestamp를 함께 사용한다.

```text
목표 시각 = 이번 실험 시작 시각
          + (해당 assistant timestamp - 선택한 trace들의 가장 이른 첫 assistant timestamp)
```

각 trace는 자기 컨테이너와 실행 thread를 가진다. 서로 다른 컨테이너는 동시에 실행할 수 있고, 같은 컨테이너에서는 명령 순서를 유지한다. 한 assistant가 여러 action을 내면 원래 mini 의미대로 순차 실행한다. 앞 명령이 목표 시각까지 끝나지 않으면 완료 후 다음 명령을 실행하고 `lateness_s`를 기록한다. 원본의 종료 시각이나 실행 시간을 강제로 맞추지는 않는다.

기존에 검사한 실제 16개 파일에서 첫 assistant부터 마지막 관측 timestamp까지의 시간창은 최대 3개 겹쳤다. 예를 들어 `fluent__fluentd-4030`은 2026-09-01 21:05:10.881 UTC부터 약 191.49초, `fluent__fluentd-4598`은 21:05:48.533 UTC부터 약 82.70초의 관측을 가진다. 두 번째 trace를 이번 실험 시작 후 약 37.65초에 시작하면 이 기록의 상대 위치를 사용할 수 있다. 표본의 겹침은 전체 제출의 최대 동시성을 뜻하지 않는다.

이 해석은 선택한 trace들의 wall clock을 비교할 수 있다는 가정에 의존한다. 같은 공식 제출의 파일을 사용하지만 metadata만으로 원 호스트 식별과 clock synchronization을 입증하지는 못했다. 서로 다른 제출/호스트의 epoch timestamp를 근거 없이 혼합하지 않는다. 이 가정은 manifest에 남긴다.

timestamp는 assistant 메시지 생성 시각이다. 원래 sandbox 생성·준비 완료·종료 시각과 최초 모델 요청 시각은 없다. 따라서 이 코드는 **관측된 명령 요청 시간 배치**를 재생하며, DSec의 sandbox 생성 도착 burst를 복원했다고 해석하지 않는다. 이미지는 측정 전에 준비한다. 기본 `precreate`는 모든 컨테이너를 측정 전에 생성하며, 전체 실험의 `--setup on-arrival`은 해당 trace의 첫 명령 목표 시각에 컨테이너를 생성한다. 준비 및 슬롯 대기 때문에 생긴 지연을 기록하고, resident container 수와 원본 상호작용 시간창의 겹침 수를 구분한다. 전체 264개를 이 시간표로 실행하면 원본의 관측 시간창은 최대 12개 겹친다. 264개를 같은 순간에 시작하는 것은 별도의 부하 실험이다.

비교 옵션은 다음과 같다.

| 옵션 | 의미 |
|---|---|
| `--starts recorded` | trace 사이의 첫 관측 timestamp 상대 위치 사용; 기본값 |
| `--starts together` | 첫 관측들을 동시에 시작하는 별도 부하 조건 |
| `--schedule timestamps` | 각 명령 묶음의 목표 시각 유지, 늦으면 지연 기록; 기본값 |
| `--schedule gaps` | 실제 묶음 완료 후 원본의 다음 응답 대기 간격 적용 |
| `--scale 1` | 원래 시간 간격 사용; 다른 배율은 명시적으로 시간을 변형한 실험 |
| `--setup on-arrival` | 첫 명령 목표 시각에 컨테이너 생성; image는 사전 준비 |
| `--max-active 12` | resident 작업 수 상한; 초과하면 슬롯 대기 기록 |
| `--workload-cpus 2-31` | 모든 작업이 공유할 CPU pool |
| `--keep-going` | 개별 작업의 자원 실패를 격리하고 다른 trace 재생 계속 |

## 사용과 검증

프로젝트 최상위에서 실행한다. 아래 A/B는 선택한 원본 파일 경로다. HTTPS 원본 URL도 입력으로 사용할 수 있다.

```bash
python harness/trace-replay/experiment.py --plan A.traj.json B.traj.json
python harness/trace-replay/experiment.py --pull --output harness/trace-replay/results/run1 A.traj.json B.traj.json
python harness/trace-replay/analyze.py harness/trace-replay/results/run1
```

`--plan`은 Docker를 호출하거나 명령을 실행하지 않는다. URL을 입력하면 trace 파일만 읽는다. 실제 실행에서는 image를 미리 준비하거나 `--pull`로 다운로드한다. 결과 디렉토리가 이미 있으면 덮어쓰지 않는다. 설정에 따라 controller는 numactl로 CPU `0-1`/memory node `0`에 묶인다.

264개 원본을 해시 및 parser/plan으로 검증했고, 실제 16개 trace의 680개 명령을 재생했다. 실제 NUMA 제한·timeout·CPU/메모리 계측·제출 내용 비교와 보고서 생성을 확인했다. 추가 Preact 재검증에서 공유 메모리 제한 아래 55개 명령을 완료했고 OOM 및 PID 상한 도달은 없었다. 실제 프로세스를 사용한 별도 검증에서는 자원 실패 격리와 timeout 동작을 확인했다. 원본 종료 코드 차이는 [실험 보고서](replay-report.md)에 남겼다.

## 필요한 측정

CPU는 처음 20ms 주기로 `cpu.stat`을 읽고 실제 샘플 간 시간차로 사용률을 계산한다. Memory는 100ms부터 시작해 `memory.current`, 지원되는 `memory.peak`, `memory.stat`의 anon/file, `memory.numa_stat`, `memory.swap.current`, `memory.events`를 기록한다. `nr_throttled` 등 CPU throttling도 수집한다. 설정한 주기가 정확히 달성된다고 가정하지 않고 실제 측정 시각·지연을 보존한다.

`docker stats`의 낮은 갱신 빈도만으로 짧은 CPU burst를 판정하지 않는다. 관측 범위는 Docker CLI 프로세스가 아니라 컨테이너 내부 workload cgroup이다. 대기 중 살아 있는 background 프로세스도 계속 측정한다. 진짜 per-command CPU time과 세션 cgroup의 구간 차이는 background 작업이 있으면 다르므로 후자를 명령 자체의 CPU time으로 표시하지 않는다.

결과물은 `manifest.json`(trace hash, 코드 hash, kernel, 시간/자원 설정), `events.jsonl`(실제 image ID, 명령 시각, 계획 대비 지연, 오류와 종료 자원), `metrics.csv`, 세션별 출력 log, `summary.json`, `timeline.png`, `report.md`다. image pull은 측정 clock 전에 수행하며 컨테이너 생성 시점은 `--setup`으로 선택한다. Cold/warm cache는 실험 조건으로 구분하고 전체 호스트 cache는 지우지 않는다.

## 최초 완료 기준과 확장 순서

1. 컨테이너 하나에서 제한이 적용되고 node 0 메모리 배치가 검증됨.
2. `gin-gonic__gin-3741`의 명령과 원본 응답 간격을 끝까지 재생하고 파일 상태/제출 결과/새 출력·실패를 수집함.
3. CPU·메모리 그래프에 실제 도구 실행과 응답 대기를 표시함. 재생 오류를 DSec 검증 결과로 취급하지 않음.
4. 서로 다른 5~10개 task로 환경·실행 의미를 검증한 뒤, 메모리 예산을 고려해 동시 세션 수를 늘림.

이 단계에서 필요한 것은 로컬 trace runner와 Docker session/계측 backend다. 향후 실시간 mini agent 연결에서는 같은 backend에 mini Environment 계약을 제공하는 adapter를 추가할 수 있다. 원격 worker 서비스, CXL checkpoint, 노드 간 migration은 이 baseline이 동작한 다음 단계다.

현재 관측과 image metadata는 [확인 기록](evidence/node0-replay-readiness.json)에 보관했다.
