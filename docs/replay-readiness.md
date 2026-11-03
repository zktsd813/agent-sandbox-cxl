# 공개 trace의 재실행 가능성과 Open-SWE-Traces 실행 준비

> 초기 조사/실험 기록이다. 현재 실행 설정과 완료 결과는 [최신 README](../README.md)를 따른다.

확인일: 2026-10-07. 공개 문서, replay 구현, dataset schema와 소량의 실제 행을 확인했다. 컨테이너 생성·image pull·모델 호출·전체 replay는 수행하지 않았다. 따라서 아래의 재생 가능성은 실행 준비 정보와 인터페이스 확인 수준이며, 이 호스트에서의 성공을 의미하지 않는다.

## 후보와 검증 범위

| 후보 | 공개된 재료 | 실제 실행 재생에 필요한 작업 |
|---|---|---|
| [SWE-agent의 테스트 trajectory](https://github.com/SWE-agent/SWE-agent/tree/3ea751c087f32b16e039a2233dd6eefecef325d5/tests/test_data/trajectories) | 받은 저장소에 5단계 trace, replay_config, 공식 RunReplay/ReplayModel과 upstream 테스트가 있다. | 패키지·Docker/SWE-ReX 준비. 이 작은 trace는 replay 연결 확인용이다. |
| [NVIDIA Open-SWE-Traces](https://huggingface.co/datasets/nvidia/Open-SWE-Traces) | model/tool messages, 실제 command/arguments, observations, source task dataset과 instance ID. mini split의 첫 실제 행을 확인했다. | 원본 task의 초기 환경을 복원하고 tool/action 형식 변환. 아래 준비 항목 참고. |
| [Terminal-Bench 2.0 trajectories](https://huggingface.co/datasets/yoonholee/terminalbench-trajectories) | task 이름, agent/model, JSON steps의 tool 이름·명령·관측, 전체 실행 시간. card는 52,104행 중 34,462행에 steps가 있다고 설명한다. | 비어 있지 않은 trace를 고르고 같은 버전의 task assets와 실행 환경을 준비한다. 이 데이터셋은 공개 수집본이며 공식 benchmark의 원본 trace와 구분한다. |
| [BrowserGym 논문 traces](https://huggingface.co/datasets/agentlabtraces/agentlabtraces) | 공식 BrowserGym README가 공개 archive를 연결한다. 파일 목록과 다운로드 공개 여부를 확인했다. | archive 내부의 task·seed·action과 benchmark 서비스를 복원해야 한다. 전체 archive는 약 207GB로 이번에 받거나 내부 trace를 검증하지 않았다. 재실행 성공까지 확인된 후보로 분류하지 않는다. |
| [SWE-smith trajectories](https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories) | 공식 coding trajectory와 대응 [SWE-smith](https://github.com/SWE-bench/SWE-smith) task 생태계. | tool messages를 변환하고 대응 task 이미지를 준비한다. 원본 messages가 현재 RunReplay의 replay_config와 같은 형식인 것은 아니다. |

Terminal-Bench 공개 수집본의 row 10000을 실제로 조회했다. `task_name=crack-7z-hash`, `agent=terminus-2`, 23 steps이며 `bash_command`로 archive 조회·추출·결과 파일 작성 명령이 들어 있다. 총 실행 시간은 약 279.839초로 기록되어 있다. 처음 조회한 다른 행들에는 steps가 없어 row 수만으로 재생 가능한 수를 판단하면 안 된다.

이 수집본의 observations는 card에 따르면 5,000자로 잘린다. tools의 `fn/cmd`는 원래 harness별 action을 정규화한 형식이다. 정확한 terminal 상태, keystroke, 명령별 대기·취소를 재현하려면 원본 action 정보가 보존되었는지 추가로 대조한다. 전체 trial duration은 LLM 대기 시간과 동일하지 않다.

## 실제 공개 replay 구현도 있는가

SWE-agent의 `harness/SWE-agent/sweagent/run/run_replay.py`와 `ReplayModel`은 이미 받은 코드다.

추가로 [Crab](https://github.com/open-agent-infra/crab)의 `9607d61a41dc44358cf078c4b438bfd971c8ee9d` 커밋에서 다음 구현을 읽었다.

- [iFlow replay 예제](https://github.com/open-agent-infra/crab/blob/9607d61a41dc44358cf078c4b438bfd971c8ee9d/docs/sdk-iflow-replay.md): Terminal-Bench task assets와 iFlow의 원본 proxy trajectory를 입력받아 model API key 없이 실제 tool을 실행한다. trace 자체는 예제 저장소에 포함되지 않는다.
- [Terminal-Bench 원본 trace 다운로드 코드](https://github.com/open-agent-infra/crab/blob/9607d61a41dc44358cf078c4b438bfd971c8ee9d/scripts/tbench_trace_downloader.py): 공개 submission 페이지의 원본 trajectory와 results archive를 다운로드하는 경로가 있다. 개별 submission에 원본 다운로드 URL이 존재하는지 확인해야 한다.
- [Claude Code trace 다운로드 코드](https://github.com/open-agent-infra/crab/blob/9607d61a41dc44358cf078c4b438bfd971c8ee9d/benchmarks/download_claude_code_traces.py): 위 Terminal-Bench HF 수집본을 입력으로 사용한다.
- [mini trace replay service](https://github.com/open-agent-infra/crab/blob/9607d61a41dc44358cf078c4b438bfd971c8ee9d/integrations/llm_services/mini_swe_trace_replay/service.py): 기록된 assistant response를 반환하고 response 지연을 제어한다. 원본 mini 메시지의 extra.timestamp가 있으면 시간 차이를 계산한다.

Crab의 현재 backend는 runc·CRIU·ZFS·eBPF 등 별도 호스트 구성을 요구한다. 이 조사에서는 설치하거나 새 worker의 dependency로 추가하지 않았다. 코드의 존재는 확인했지만 Open-SWE-Traces를 변환 없이 이 서비스에 넣으면 호환된다는 뜻은 아니다. 받은 현재 mini harness의 action dictionary와 NVIDIA의 tool_calls 형식, 원래 model adapter와 preset을 맞춰야 한다.

## Open-SWE-Traces에서 실제로 확인한 것

`v1.2 / minisweagent` row 0:

- instance ID: `python-distro_distro_pr145`.
- repository: `python-distro/distro`.
- source task dataset: `AweAI-Team/Scale-SWE` — 실제 행의 `hf_dataset_name` 필드에서 확인했다.
- 총 41 messages와 24 tool calls. 8개 assistant message에 복수 tool calls가 있다.
- 모든 호출의 tool 이름은 bash이며 arguments에 command 문자열이 들어 있다.
- 기록된 명령은 `/testbed`를 사용한다.
- 마지막 호출은 mini의 제출 완료 marker를 출력하고 patch 파일을 읽는 명령이다.
- schema에는 command별 timestamp, inference duration, RSS, CPU 또는 hardware-counter 필드가 없다.

따라서 dataset card의 instance_id 설명만 보고 모든 행을 SWE-rebench-V2에 연결하면 안 된다. 가능한 경우 **hf_dataset_name + instance_id**로 원본 task를 찾고, repository와 시작 commit도 대조한다.

## 준비해야 할 요소

| 요소 | 어디서 구하는가 / 구현 범위 |
|---|---|
| Trace와 버전 | NVIDIA dataset의 config·split·revision·trajectory ID 고정. 먼저 mini split의 소량 sample로 시작. |
| 원본 task | Scale-SWE 또는 SWE-rebench-V2의 대응 instance. image·commit·test 구성을 task에서 얻는다. |
| 초기 실행 환경 | task 이미지에 들어가 올바른 base commit과 수정 전 workspace를 준비한다. golden patch나 기록된 최종 model patch를 초기 상태에 적용하지 않는다. |
| Runtime | 컨테이너를 쓸 경우 Docker/Podman과 이미지·디스크 공간. 새 worker를 쓸 경우 해당 이미지와 같은 의존성·filesystem 환경을 제공해야 한다. |
| Replay driver | assistant.tool_calls를 읽고 arguments JSON을 파싱하여 worker로 command를 전달한다. 같은 응답의 복수 action과 제출 완료를 처리한다. shell 문자열을 재해석하거나 command를 host에서 실행하지 않는다. |
| Tool 계약 | tool 이름·arguments·출력 형식, cwd/env, timeout, shell interpreter, background process 수명을 맞춘다. file/browser tool을 가진 다른 split은 전용 adapter가 필요하다. |
| 평가 | task별 test patch·test command·결과 parser. 필요하면 FAIL_TO_PASS와 PASS_TO_PASS 확인. 기록된 resolved 값은 재실행 결과를 대신하지 않는다. |
| 시스템 실험 | worker에서 새로 측정할 CPU/RSS/peak memory/latency, 동시 세션 수, 자원 한도와 LLM 대기 시간 정책. |

받은 mini-swe-agent의 Python 요구사항은 `>=3.10`이다. DockerEnvironment는 지정 이미지, cwd, env, Bash interpreter를 사용한다. 기본 command timeout은 30초이고 container 수명은 2시간이다. trace의 build/test가 이 한도를 넘을 수 있어 실험 설정에서 정해야 한다. 컨테이너 이미지의 언어 runtime 버전은 host Python 버전과 별개다.

### 원본 task의 환경 필드

공식 [Scale-SWE card](https://huggingface.co/datasets/AweAI-Team/Scale-SWE)는 다음을 제공한다.

- image_url, workdir, parent_commit, pr_commit, github_url.
- pre_commands: 이미지에 들어간 뒤 올바른 시작 commit을 준비하는 명령.
- f2p_patch, f2p_script, FAIL_TO_PASS, PASS_TO_PASS.

공식 [SWE-rebench-V2 card](https://huggingface.co/datasets/nebius/SWE-rebench-V2)는 다음을 제공한다.

- image_name, base_commit, repo, problem_statement.
- install_config의 install/test_cmd/docker_specs/log_parser.
- test_patch, FAIL_TO_PASS, PASS_TO_PASS.

[SWE-rebench builder](https://github.com/SWE-rebench/SWE-rebench-V2)에는 base Dockerfile, per-task image 생성, evaluator와 log parser 코드도 있다. 기존 이미지가 접근 가능하면 재빌드 없이 사용할 수 있지만, image 이름이 있다는 것만으로 현재 registry 접근과 호스트 아키텍처 호환성이 검증되지는 않는다.

Scale-SWE의 workdir 예시는 `/workspace/<repo>`이며 확인한 NVIDIA sample command는 `/testbed`를 사용한다. 원래 NVIDIA 실행 환경이 사용한 경로 구성이나 초기화 방법을 확인해 같은 위치를 제공해야 한다. absolute path를 무조건 문자열 치환하면 shell·코드 내용까지 바뀔 수 있으므로 피한다. env와 dependency 준비도 이미지 이름만으로 동일하다고 가정하지 않는다.

Scale-SWE 조회 서비스는 이번에 해당 샘플의 개별 row 검색·필터 요청에서 오류를 반환했다. source dataset과 환경 schema는 확인했지만 **python-distro sample의 image_url/parent_commit을 실제로 대조하지 못했다**. 그래서 이 샘플을 지금 바로 실행할 수 있다고 표시하지 않는다. registry manifest, image digest, architecture와 실제 시작 상태도 아직 확인하지 않았다.

## 권장 첫 실험

모델 없이 재생하는 경우 필요한 실행은 다음 경로다.

```text
trace의 tool_calls + 원본 task의 이미지·시작 상태
    → 순서대로 action 공급
    → 같은 worker 세션에서 실제 명령 실행
    → 새 출력·종료 코드·자원 사용량 기록
    → task 테스트와 결과 비교
```

첫 연결은 현재 저장소의 작은 SWE-agent replay trace로 확인할 수 있다. 실제 coding workload는 Open-SWE-Traces mini split으로 확장하고, 개발 이외의 terminal workload는 Terminal-Bench에서 별도로 선택한다. 브라우저 trace는 서비스 초기화와 state 재현이 더 필요한 다음 단계로 둔다.

GPU와 실제 LLM API는 action replay에 필요하지 않다. 단, task 자체의 tool workload가 GPU를 요구하면 그 task는 별도다. NVIDIA schema에 원래 LLM 대기 시간이 없으므로 첫 실험에서는 0초·고정값·명시한 분포 중 정책을 선택하고, 측정값과 합성값을 구분한다. timing 없이 모든 명령을 이어서 실행한 결과를 원래 agent 실행 시간이라고 해석하지 않는다.
