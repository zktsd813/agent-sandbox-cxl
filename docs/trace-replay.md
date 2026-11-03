# GPU 없이 agent tool trace를 재실행하는 공개 코드

> 초기 조사/실험 기록이다. 현재 실행 설정과 완료 결과는 [최신 README](../README.md)를 따른다.

확인일: 2026-10-07.

기록된 model action을 순서대로 공급하고 실제 tool 명령을 CPU sandbox에서 실행하면 GPU나 LLM API 없이 worker의 실행 시간·메모리·세션 관리 등을 실험할 수 있다. 모델 자체의 지연이나 품질은 이 실험으로 측정하지 않는다. 대상 trace에 GPU 작업이 포함되어 있다면 해당 작업은 별도 요구사항을 가진다.

## 받아둔 코드

| 경로 | 공식 저장소 | 확인한 커밋 | 라이선스 |
|---|---|---|---|
| `../harness/mini-swe-agent/` | [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | `04d809ceab9df28f9adaed044884180159172930` | MIT |
| `../harness/SWE-agent/` | [SWE-agent/SWE-agent](https://github.com/SWE-agent/SWE-agent) | `3ea751c087f32b16e039a2233dd6eefecef325d5` | MIT |

둘 다 기본 branch의 depth 1 clone이다. 의존 패키지 설치, 이미지 다운로드, sandbox 실행, 모델 호출은 수행하지 않았다. 다운로드 기록은 `evidence/swe-checkouts.json`에 있다.

## SWE-agent: 공식 replay 기능이 이미 있음

[공식 CLI 문서](https://swe-agent.com/latest/usage/cli/)는 `sweagent run-replay`가 trajectory의 action을 환경에서 다시 실행한다고 설명한다. 받은 코드에서도 다음 경로를 확인했다.

- `../harness/SWE-agent/sweagent/run/run_replay.py`: trajectory의 `replay_config`로 환경·도구 구성을 읽고, 기록된 assistant message와 tool call을 추출한다. model을 `ReplayModel`로 바꿔 실행한다.
- `../harness/SWE-agent/sweagent/agent/models.py`: `ReplayModel.query()`가 파일에 저장된 action을 순서대로 반환한다. 실제 LLM inference나 API 호출을 수행하지 않는다.
- `../harness/SWE-agent/tests/test_run_replay.py`: Docker 환경을 사용한 upstream replay 테스트가 있다. 이번 조사에서 이 테스트를 실행하지는 않았다.

의존 패키지와 runtime 준비 후 사용 형식은 다음과 같다.

```bash
sweagent run-replay --traj_path /absolute/path/to/run.traj
```

현재 runner는 `replay_config`가 있어야 한다. `.traj`라는 확장자만 같다고 모두 호환되는 것은 아니다. tools, parser와 환경 준비가 기록 당시 구성에 맞아야 한다. mini의 `.traj.json` 또는 학습용 messages 데이터도 이 명령에 바로 넣을 수 있다고 가정하지 않는다.

받은 저장소에서 `replay_config`가 있는 5단계 테스트 trace를 확인했다.

```text
harness/SWE-agent/tests/test_data/trajectories/
  gpt4__swe-agent-test-repo__default_from_url__t-0.00__p-0.95__c-3.00__install-1/
    6e44b9__sweagenttestrepo-1c2844.traj
```

이는 형식과 연결 확인에 쓸 작은 테스트 trace이며 대표적인 메모리 workload는 아니다. 같은 저장소의 오래된 일부 `.traj`에는 `replay_config`가 없어 현재 runner에서 그대로 읽을 수 없다. 사용하려면 해당 trace의 환경 구성을 복원하거나 다른 입력 형식으로 변환해야 한다.

## mini-swe-agent: 새 worker adapter와 연결하기 쉬움

`../harness/mini-swe-agent/src/minisweagent/models/test_models.py`의 `DeterministicModel`은 지정한 output message를 순서대로 반환한다. 현재 버전의 model message에는 `extra.actions`가 있고 agent loop는 이를 environment로 보내 실행한다.

따라서 기록된 assistant message를 준비하여 이 모델에 공급하면 모델 호출 없이 tool 실행 경로를 이용할 수 있다. 이는 upstream 테스트용 구성 요소를 활용하는 방법이다. 완성된 범용 trace import CLI가 있다는 뜻은 아니다. 기존 trace의 action 형식 변환, 실행 환경 초기화, 마지막 제출 처리와 대기 시간 재현은 연결 코드에서 처리해야 한다. 테스트용 비용 값도 실제 API 비용으로 해석하지 않는다.

새 CXL worker의 첫 연결에는 mini의 좁은 Environment 인터페이스가 유리하고, 기존 replay 동작을 즉시 읽어볼 대상으로는 SWE-agent의 runner가 유리하다.

## 공개 trace 후보

| 데이터 | 확인한 내용 | 실험에 사용할 때 필요한 작업 |
|---|---|---|
| [SWE-bench/SWE-smith-trajectories](https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories) | 공식 card는 SWE-agent와 Claude 3.7 Sonnet으로 생성한 trajectory라고 설명한다. MIT. messages, instance ID, patch 등의 필드가 있다. | 대응하는 SWE-smith task·repository·실행 환경을 준비하고 messages를 runner 입력으로 변환한다. 데이터셋 card에 현재 SWE-agent의 `replay_config` 필드는 나열되어 있지 않다. |
| [NVIDIA Open-SWE-Traces](https://huggingface.co/datasets/nvidia/Open-SWE-Traces) | SWE-agent, OpenHands, mini-swe-agent 등으로 수집한 trajectory. messages, tools, instance ID, repo, metadata 등을 제공한다. card의 라이선스는 CC-BY-4.0. | harness와 버전을 선택하고 대응 task 환경을 복원한다. messages와 tool schema를 worker 입력으로 변환한다. 공개 card만으로 개별 trace의 정확한 inference 대기 시간 보존 여부를 확정할 수 없다. |
| [UW TraceLab](https://github.com/uw-syfi/TraceLab) | 수집·분석 코드와 공개 시간·호출 통계가 있다. 공개 trace는 sanitization 과정에서 tool inputs와 local path를 제거한다. | tool 빈도·시간 분포와 부하 스케줄 생성에 활용한다. 공개 데이터만으로 원래 shell 명령을 재실행할 수는 없다. |

이 데이터셋 전체를 다운로드하지는 않았다. 위 라이선스는 각 프로젝트/card의 표기이며 task repository와 이미지·의존성에는 별도 조건이 적용된다.

## CXL worker 실험에서 추가할 부분

[AgentZip 논문 §5.3](https://arxiv.org/html/2609.11294v1)은 tool action과 직전 LLM inference 시간을 기록하고, 재생 시 `think_time_ms_before`만큼 기다린다고 설명한다. 이 원칙을 CPU worker 실험에 적용할 수 있다. 해당 논문의 정확한 공개 replayer 구현 위치는 이번 조사에서 확정하지 못했다.

SWE-agent의 기본 ReplayModel과 mini의 DeterministicModel은 trace에 있는 action을 반환할 뿐, action별로 원래 inference 시간을 기다리는 실험 기능은 제공하지 않는다. worker 실험용 연결 코드에서 다음을 명시한다.

1. Task의 시작 이미지, repository commit, workspace 초기 상태.
2. 세션별 action 순서와 cwd/env/tool 정의.
3. 기록된 inference 대기 시간 또는 명시한 합성 대기 시간.
4. 동시 세션 수와 도착 스케줄.
5. tool 출력·종료 코드·실행 시간·메모리, timeout과 프로세스 정리 결과.

기록된 출력만 반환하는 mock replay는 실제 메모리·CPU 부하를 만들지 않는다. 자원 실험에서는 명령을 실제 실행하고, 다른 시스템 설정에서도 같은 action·대기 시간·초기 상태를 사용한다. 새 명령 선택을 하지 않는 고정 trace 실험이므로 재실행 중 환경 결과가 달라졌을 때의 모델 적응은 평가하지 않는다.
