# 이 서버에서 Open-SWE-Traces를 실행하는 방법

> 초기 조사/실험 기록이다. 현재 실행 설정과 완료 결과는 [최신 README](../README.md)를 따른다.

확인일: 2026-10-07. 서버와 받은 소스를 읽기 전용으로 점검했다. 패키지 설치, 컨테이너 생성, 이미지 다운로드와 실제 replay는 아직 수행하지 않았다.

한 서버에서 기록된 명령을 재실행하는 데는 새 워커가 필수는 아니다. SWE-agent의 기존 replay와 SWE-ReX/Docker 실행 기능을 사용할 수 있다. 다만 NVIDIA 데이터 형식과 원래 task 환경을 맞추는 준비는 필요하다. bud04·bud05의 CXL 사용 정책과 세션 상태 이동을 실험하려면 그 부분을 관리할 새 워커를 구현한다.

## 현재 준비 상태

| 항목 | 확인 결과 | 남은 작업 |
|---|---|---|
| 호스트 Python | 3.12.3. 받은 SWE-agent는 3.11 이상을 요구한다. | 작업용 가상환경에 패키지와 의존성 설치 |
| Docker | CLI와 데몬 29.3.1. 데몬 정보 조회 성공 | 개별 task 이미지 접근과 실제 컨테이너 실행 확인 |
| CPU / 메모리 | Docker 데몬이 CPU 64개와 약 157GiB 메모리를 보고한다. | task별 사용량 측정 후 동시 실행 수 결정 |
| 작업 디스크 | 약 109GB 여유 | 소량 trace와 task 이미지부터 준비 |
| Harness 소스 | SWE-agent와 mini-swe-agent를 받아 두었다. | 현재 Python에는 패키지가 없고 저장소의 .venv도 없다. |
| NVIDIA task 환경 | 원본 dataset의 환경 필드를 조사했다. | 선택한 trace의 이미지, 시작 commit, 경로를 실제 대조 |
| NVIDIA replay 입력 | messages와 tool_calls를 확인했다. | SWE-agent replay 형식 변환 또는 mini용 replay driver |

메모리는 Docker가 보고한 총량으로, 여유 메모리나 CXL pool 용량이 아니다. 기본 sandbox 안에서는 Docker socket 접근이 거부되었지만 sandbox 밖의 읽기 전용 조회는 성공했다. 설치 여부는 현재 Python과 두 저장소를 기준으로 확인했다. [관측 기록](evidence/server-replay-readiness.json).

## 무엇을 다시 실행하는가

Trace에서 기록된 assistant의 tool 호출을 꺼내 같은 순서로 실제 실행한다. 새 출력, 종료 코드, 실행 시간과 자원 사용량을 얻는다. LLM이 다음 행동을 새로 결정하지 않으므로 모델 가중치, GPU와 LLM API key가 필요하지 않다. CPU 작업인 task부터 선택한다.

기록된 출력만 돌려주는 방식은 명령이 실제로 수행되지 않아 CPU·메모리 workload 실험이 되지 않는다. 기록된 명령 재실행은 모델의 추론 계산량을 포함하지 않으며, 실행 결과가 달라져도 다음 행동은 기록된 것을 사용한다.

```text
NVIDIA trace + 원본 task 환경
  → 기록된 action 공급
  → SWE-agent / SWE-ReX
  → task Docker 컨테이너에서 실제 명령 실행
  → 새로운 로그·자원 측정·필요하면 task 평가
```

## 준비할 세 가지

1. **실행 프로그램:** 가상환경에 받은 SWE-agent와 의존성인 SWE-ReX를 설치한다. SWE-agent에는 [run-replay](https://swe-agent.com/latest/usage/cli/)가 있으며, 받은 [구현](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/run/run_replay.py)은 ReplayModel로 기록된 행동을 공급한다.
2. **문제의 시작 환경:** trace의 hf_dataset_name과 instance_id로 원본 task를 찾는다. [Scale-SWE](https://huggingface.co/datasets/AweAI-Team/Scale-SWE)는 image_url, parent_commit, workdir와 pre_commands를 제공한다. [SWE-rebench-V2](https://huggingface.co/datasets/nebius/SWE-rebench-V2)는 image_name, base_commit와 설치·테스트 설정을 제공한다. 올바른 이미지와 수정 전 코드를 준비하고 경로·의존성을 맞춘다. 정답 patch를 미리 적용하지 않는다.
3. **Trace 변환:** NVIDIA의 messages/tool_calls를 받은 SWE-agent가 기대하는 history/replay_config로 변환하고, 원래 tool 정의·argument·종료 조건을 맞춘다. 단순 필드 이름 변경만으로 충분하다고 가정하지 않는다. 현재 SWE-agent의 function-calling parser는 한 응답에 한 호출을 기대한다. 복수 호출을 가진 trace는 원래 실행 순서를 보존해 처리해야 한다.

확인한 mini sample의 명령은 /testbed를 사용하지만 Scale-SWE 예시는 /workspace/<repo>를 사용한다. 같은 경로 구성을 제공해야 한다. 이 sample의 정확한 이미지와 시작 commit은 아직 매칭하지 못했으므로 재생 성공이 확인된 sample은 아니다.

NVIDIA [v1.2는 mini-swe-agent trajectory](https://huggingface.co/datasets/nvidia/Open-SWE-Traces)를 제공한다. 원본 SWE-agent를 유지하려면 v1.0/v1.1의 sweagent split에서 시작하는 것이 형식·tool 의미를 맞추기 쉽다. 그래도 NVIDIA 포맷 변환과 환경 복원은 필요하다. v1.2를 사용하려면 mini의 실행 의미를 유지하는 replay driver가 더 자연스럽다. 받은 mini는 같은 컨테이너 안에서 호출마다 새 Bash를 실행하고, 받은 SWE-agent는 지속되는 Bash session을 사용한다.

## 새 워커가 필요한 경우와 연결 위치

원격 서버에서 명령을 실행하는 기능 자체는 [SWE-ReX](https://swe-rex.com/latest/architecture/)에 이미 있다. 새 워커는 실험에 필요한 배치, 자원 제어, 관측과 CXL 상태 관리를 추가하는 서비스다.

```text
SWE-agent 또는 trace replayer
  → 실행 연결 adapter
  → 선택한 노드의 새 worker
  → 해당 task의 sandbox
```

SWE-agent의 agent loop를 유지하려면 worker를 SWE-ReX의 Runtime/Deployment 계약에 연결한다. Runtime은 명령·session·파일 기능, Deployment는 실행 환경의 시작·종료를 담당한다. 기존 SWE-ReX 실행 기능을 worker 안에서 이용할 수도 있다. 독자적인 실행 backend를 만들면 adapter가 이 계약을 제공해야 한다. 현재 RunReplay는 Python에서 Deployment 객체를 주입받을 수 있다. 자체 설정을 CLI로 선택하려면 설정 등록 작업도 필요할 수 있다.

| 최소 기능 | 쉬운 설명 |
|---|---|
| 세션 생성·종료 | trace 하나가 사용할 sandbox와 작업 파일을 준비하고 종료 후 정리한다. |
| 명령 실행·결과 | session ID, call ID, 명령, timeout을 받아 출력·종료 코드·실패 사유를 반환한다. |
| 상태 유지 | 다음 명령이 이전에 수정한 파일을 볼 수 있게 한다. SWE-agent 연결에서는 같은 셸의 cd/export 상태와 필요한 background process도 유지한다. |
| 파일 읽기·쓰기·업로드 | harness가 사용하는 파일과 worker가 실행하는 파일이 같은 sandbox 안에 있도록 한다. |
| 중단·timeout | 실행을 중단하고 종료를 확인한다. 단순히 통신 timeout이 났다고 작업이 끝난 것으로 처리하지 않는다. |
| 자원 제한·측정 | CPU·메모리 한도와 실행 시간, CPU 시간, peak memory, 실패·OOM을 기록한다. |

일반 명령 하나를 실행하는 API만으로 원본 SWE-agent 전체를 연결할 수 있는 것은 아니다. 받은 [SWEEnv](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/environment/swe_env.py)는 Bash session 생성, 세션 내 실행·중단, 독립 명령 실행, 파일 읽기·쓰기와 환경 종료를 사용한다. tool 준비에 필요한 업로드도 같은 환경으로 연결한다.

## CXL 기능의 추가 순서

첫째, 기존 Docker 경로에서 모델 없이 trace 하나를 재생해 초기 환경과 action 형식이 맞는지 확인한다. 둘째, 새 worker를 연결하고 같은 trace의 파일 결과·테스트 결과와 비교한다. 셋째, worker에 DRAM/CXL 배치·회수 정책과 필요한 상태 이동을 추가한다.

CXL이 연결되어 있다는 사실만으로 일반 Docker 프로세스가 자동으로 pool을 사용하는 것은 아니다. 프로세스 메모리를 어떤 경로로 CXL에 두고 회수할지 정해야 한다. bud04에서 실행한 세션을 bud05에서 계속하려면 수정 파일과 필요한 살아 있는 프로세스 상태, 실행 노드의 소유권도 관리해야 한다. 이는 일반 replay와 별도의 기능이다. 상세 범위는 [worker 설계안](worker-design.md)에 있다.

확인한 NVIDIA schema에는 원래 LLM 대기 시간과 명령별 메모리 측정치가 없다. 따라서 대기는 실험에서 정한 값으로 넣고 메모리는 worker에서 새로 측정한다. 결과에서 실제 측정과 합성한 대기를 구분한다.

대기 경계는 원래 harness의 모델 응답 단위를 따른다. 받은 mini의 DefaultAgent는 한 번의 model.query 응답에 포함된 actions를 순서대로 모두 실행한 뒤 다음 model.query를 호출한다. 같은 응답의 복수 tool call 사이에는 추가 모델 추론이 없다. 하나의 전체 trajectory에는 이런 모델 응답과 도구 실행이 여러 번 포함된다. 따라서 합성한 LLM 대기를 모든 개별 tool call 사이에 넣지 않고, 다음 assistant 응답을 공급하는 경계에 넣는다. 원본 trace 생성 때의 모델 호출과, 저장된 응답을 읽는 replay 동작은 구분한다.
