# 호출 시간 trace와 DSec burstiness 검증

> 초기 조사/실험 기록이다. 현재 실행 설정과 완료 결과는 [최신 README](../README.md)를 따른다.

조사일: 2026-10-07. 공개 문서, 수집 코드와 일부 실제 데이터 행을 읽었다. 기록된 명령 실행, Docker image 다운로드, 모델 호출은 수행하지 않았다.

NVIDIA Open-SWE-Traces의 공개 스키마에는 구조화된 호출·결과 timestamp가 없다. 따라서 원래 실행에서 명령 사이의 대기가 길었는지, 호출이 시간상 몰렸는지 직접 측정할 수 없다. 임의의 지연을 넣은 재실행은 그 지연을 가정한 실험이다. 앞서 확인한 스키마는 [확인 기록](evidence/open-swe-trace-schema.json)에 있다.

## DSec의 서로 다른 관측

[DSec 논문](https://arxiv.org/html/2609.22978v1)의 §4.1은 두 현상을 설명한다.

| 관측 | 논문 위치 | 검증에 필요한 기록 |
|---|---|---|
| 학습·평가 배치가 짧은 시간에 많은 sandbox 생성 요청을 보냄 | Figure 2 및 본문 | 생성 요청 시각, 배치/작업 ID, 동시성, 준비 완료 시각 |
| 실행 중 짧은 CPU 활동과 모델 응답 대기가 번갈아 나타남; 상태와 메모리는 유지됨 | Figure 3 및 본문 | 모델·도구 경계와 CPU/메모리 시계열 |

Figure 2 자체는 작업별 생성 sandbox 수의 분포다. 단일 에이전트의 tool-call 간격 분포가 아니다. Figure 5/§4.3의 수치는 약 90%의 sandbox가 **요청 CPU 용량 대비 평균 사용률 5% 이하**라는 뜻이다. 시간의 90%가 idle이라는 수치가 아니다.

이번 조사에서는 DSec 원본 운영 trace의 공개 다운로드를 찾지 못했다. 다른 공개 trace로 비슷한 현상이 나타나는지 조사할 수 있지만, 그것만으로 DSec 운영 환경의 분포·수치를 재검증했다고 할 수는 없다.

## 실제 샘플을 확인한 대안

| 자료 | 시간 정보 | 명령과 환경 | 실험 적합성 |
|---|---|---|---|
| SWE-bench 공식 mini-SWE-agent 원본 `.traj.json` | assistant/tool 메시지 `extra.timestamp` | 실제 bash 명령, 결과, 설정, Docker image 이름 | SWE 명령과 관측된 응답 간격을 함께 재실행할 출발점 |
| TraceLab v0.0.2 | `emitted_at`, `result_at`, `tool_wall_latency_ms`, 이벤트 시각 | 원본 명령 인수·출력 제거; 일부 명령 골격만 제공 | 실제 Claude Code/Codex 세션의 시간 패턴 분석 |
| pagarsky/agent-trace | 도구 `start_ns`, `end_ns`, `duration_ms`; CPU 시간·RSS 등의 telemetry | MBPP/Python, NL2Bash/Bash 입력과 출력; Bash fixture 생성 코드 | 작은 worker 실험 후보; 큰 SWE 작업과는 다름 |

### SWE-bench 공식 mini 원본

- [공식 공개 저장소](https://github.com/SWE-bench/20260902_mini-v2.4.6_gemini-3-5-flash)
- [검사한 실제 파일](https://raw.githubusercontent.com/SWE-bench/20260902_mini-v2.4.6_gemini-3-5-flash/8dcca98587e7f698753903e3b1747eb5dc013cb1/trajs/gin-gonic__gin-3741.traj.json)
- [mini 원본 출력 형식 설명](https://mini-swe-agent.com/latest/usage/output_files/)

검사한 스냅샷에는 `.traj.json` 파일 264개가 있다. 그중 Go 저장소 작업 `gin-gonic__gin-3741` 하나를 내려받아 확인했다. mini 버전은 2.4.6, 모델 API 호출은 27회이며, assistant 27개/tool 26개 모두 timestamp가 있었다. 종료 상태는 `Submitted`이며 이는 패치 제출이지 정답 판정 결과가 아니다.

샘플 환경 설정에는 다음이 들어 있다.

```text
image: docker.io/swebench/sweb.eval.x86_64.gin-gonic_1776_gin-3741:latest
cwd: /testbed
interpreter: bash -c
```

실제 첫 구간에서 계산한 값은 다음과 같다. 명령 구간은 timestamp 차이이며 CPU 사용 시간은 아니다.

| 명령 | assistant → tool 결과 | 이전 tool 결과 → 이번 assistant 응답 |
|---|---:|---:|
| `ls -la` | 0.064590초 | 첫 응답이라 계산하지 않음 |
| `grep -n -C 20 "StatusCode" logger.go` | 0.060819초 | 1.150898초 |
| `grep -n -C 10 "StatusCodeColor" logger_test.go` | 0.054898초 | 4.248392초 |
| `go test ./...` | 1.978364초 | 3.240150초 |

timestamp 의미는 현재 로컬 mini 2.4.6 코드로도 확인했다.

- `models/litellm_model.py`는 모델 응답을 받고 명령을 파싱한 뒤 assistant 시각을 기록한다. 따라서 이전 tool 결과와의 차이는 모델 요청·생성·네트워크·harness 처리를 포함한 **관측된 응답 간격**이다. 순수 모델 추론 시간이라고 해석하지 않는다.
- `agents/default.py`는 한 assistant가 낸 명령들을 모두 실행한 뒤 결과 메시지들을 만든다. `models/utils/actions_toolcall.py`에서 그때 tool timestamp를 기록한다.
- 한 응답에 명령 여러 개가 있으면 결과 시각은 각 명령의 개별 완료 시각이 아니다. 이 경우 전체 묶음의 실행 구간과 다음 응답까지의 간격을 사용한다. 검사한 한 파일은 모든 응답이 명령 한 개였으며, 다른 파일도 그렇다고 일반화하지 않는다.
- CPU/RSS 시계열과 sandbox 생성 시각은 이 샘플에 없다. image 이름은 있지만 `latest` tag이며 원래 digest는 확인하지 못했다. image 존재·재실행 성공도 아직 확인하지 않았다.

NVIDIA가 배포한 변환된 대화 데이터와 mini가 저장한 원본 trace는 보존하는 필드가 다르다. NVIDIA 쪽에 시간이 없다는 사실은 mini 원본에도 시간이 없다는 뜻이 아니다.

### TraceLab

[공식 코드와 스키마](https://github.com/uw-syfi/TraceLab), [v0.0.2 데이터 배포](https://github.com/uw-syfi/TraceLab/releases/tag/v0.0.2).

배포 JSONL에서 처음 30개 행만 확인했다. 그 안의 도구 호출 37개 모두 호출·결과 시각이 있었다. 예를 들어 `Read` 한 호출은 `23:55:38.897Z`부터 `23:55:38.957Z`까지 60ms다. 전체 모집단의 품질을 검증한 것은 아니다.

원본 입력·결과 텍스트는 개인정보 정리를 거쳐 제거된다. `command_skeleton`은 실행 파일 이름과 구조를 보여줄 수 있지만 실제 경로·인수를 복구하지 않는다. 따라서 시간 분석에는 유용하지만 같은 파일 작업·빌드·테스트를 그대로 재실행할 수는 없다. tool wall latency도 CPU 사용량은 아니다.

### AgentTrace

[공식 코드](https://github.com/pagarsky/agent-trace), [데이터 카드](https://huggingface.co/datasets/pagarsky/agent-trace).

Qwen3-1.7B의 NL2Bash와 MBPP 파일에서 각각 처음 30개 trace를 확인했다. 도구 시작/종료와 실제 입력·출력을 기록한다. 다만 확인한 250개 `llm_steps`의 `duration_ms`는 모두 null이다. 도구 간격을 계산할 수 있다는 사실과 LLM 실행 시간이 직접 기록되어 있다는 주장을 구분해야 한다.

수집 코드의 시각은 `perf_counter_ns()` 기준이다. 한 trace 내 차이는 계산할 수 있지만 서로 다른 수집 호스트의 절대 시각으로 합치면 안 된다. `timestamp_utc`는 trace 구성 시점에 기록하므로 시작 시각으로 취급하지 않는다. `max_rss_bytes`는 누적 peak에 기반한 telemetry이며 시간별 메모리 사용량을 뜻하지 않는다. 원 수집 환경은 Apple M3 Pro/macOS이므로 bud 서버의 자원 사용량과 직접 같다고 볼 수 없다.

## bud04/bud05 실험으로 연결하는 방법

우선 공식 mini 원본의 명령과 관측된 시간을 사용하는 쪽이 SWE 작업 재실행에 적합하다. 현재 [실험 코드](node0-replay-plan.md)는 여러 trace의 timestamp를 공통 시간축에 놓는 방식을 기본으로 한다. 실제 명령 완료 후 원본 응답 간격을 기다리는 방식은 `--schedule gaps` 비교 옵션으로 제공한다. 세션별 컨테이너 상태를 유지하며 CPU 작업에는 GPU나 모델 API가 필요하지 않다. 응답 대기는 한 assistant 묶음의 경계에 적용하며 각 명령 사이마다 넣지 않는다.

이는 기록된 행동을 고정해 실행하는 실험이다. 재실행 결과가 달라져도 다음 명령은 원본대로 진행하므로 상태 차이와 실패를 기록해야 한다. 원래 명령 실행 시간을 강제로 맞추지 않고 현재 서버에서 걸린 시간을 측정하면 전체 호출 시각은 원본과 달라질 수 있다. 원본 응답 간격 보존과 원본 절대 호출 스케줄 보존은 서로 다른 실험 조건이다.

함께 기록할 항목은 세션/명령 ID, 실제 시작·종료 시각, cgroup CPU 사용량, 요청 vCPU, `memory.current`, anonymous/file 메모리, CXL/NUMA별 배치 정보다. CPU 샘플 간격이 짧은 명령보다 길면 burst가 평균화되므로 짧은 주기 또는 이벤트 기반 측정을 고려한다. tool-call이 없을 때도 백그라운드 프로세스가 CPU를 쓸 수 있으므로 호출 간격만으로 idle을 판정하지 않는다.

이 자료로 검증할 수 있는 질문은 “공개 SWE 작업에서도 모델 응답 대기 중 CPU 사용이 줄고 메모리 상태가 유지되는가”다. RL 배치의 sandbox 생성 burst는 별도의 도착 기록을 구하거나 배치 크기·도착 패턴을 명시한 부하 실험으로 다뤄야 한다. TraceLab의 사람이 쓰는 세션 시작 분포를 DSec의 RL 배치 도착 분포로 대체하지 않는다.

샘플 개수·소스 커밋·계산값은 [확인 기록](evidence/timing-traces.json)에 보관했다.

## Trace 개수와 시간 패턴 합성에 대한 추가 판단

추가 조사일: 2026-10-07. 아래의 충분성 판단과 실험 설계는 공개 자료 및 표본 검사에 근거한 제안이다. 시간 모델 학습, NVIDIA trace 합성, 실제 명령 재실행은 아직 수행하지 않았다.

### 공개 규모와 충분성

| 자료 | 확인한 규모 | 해석 |
|---|---|---|
| 앞서 조사한 공식 mini 스냅샷 | 파일 264개, 파일명 정규화 기준 저장소 37개 | 초기 worker/CXL 비교 실험을 시작할 다양성은 있음. 단일 제출 모델의 표본이며 실제 재생 가능 수는 아직 미확인 |
| NVIDIA v1.2 | trace 107,267개, 고유 PR/`instance_id` 41,405개 | 같은 작업의 여러 실행을 포함. 전부 독립적인 작업으로 세면 안 됨 |
| TraceLab v0.0.2 | 세션 8,058개, agent step 665,453개, 도구 호출 743,819개, 사용자 52명 | 시간 모델 후보를 만들 규모는 있음. 모든 행이 시간 분석 조건을 충족하는 것은 아님 |

NVIDIA 수치는 [버전별 공개 분포](https://huggingface.co/datasets/nvidia/Open-SWE-Traces/blob/main/data_distribution.md)의 v1.2에서 확인했다. TraceLab 수치는 [v0.0.2 release](https://github.com/uw-syfi/TraceLab/releases/tag/v0.0.2) 기준이다. TraceLab README의 이전 v0.0.1 수치와 혼합하지 않는다.

mini는 기존 한 파일에서 검사를 확대했다. 10개 저장소에서 파일 크기 중간값에 가까운 파일을 고르고, 가장 작은 3개와 가장 큰 3개를 더한 뒤 중복을 제거했다. 총 16개 파일(약 32MB)을 읽었다. 그 안의 assistant 메시지 680개/tool 메시지 664개 모두 timestamp와 각 파일의 image 이름이 있었다. 계산한 응답 간격에 음수는 없었다. 16개 모두 `Submitted`였고 이 표본의 응답은 모두 단일 명령이었다. 표본은 형식·크기 범위를 확인하기 위한 선택이며, 모집단 지연 분포를 추정할 무작위 표본이 아니다. 전체 264개 실행 성공이나 timestamp 완전성을 보증하지 않는다.

갯수는 검증 목표와 함께 판단한다. 264개는 평균 메모리 사용, CPU 활동 구간, 정책별 실행 시간 등 초기 비교에 사용할 출발점이다. 그러나 작업 단위 p99는 상위 2~3개에 크게 좌우되므로 안정적인 tail 추정이나 모든 에이전트·모델에 대한 일반화에 충분하다고 단정하지 않는다. 동일 trace의 반복이나 복제는 측정 변동·동시성을 조사할 수 있지만 작업 다양성을 늘리지는 않는다.

작업/저장소 단위로 표본을 늘리며 효과 크기와 신뢰구간이 안정되는지 확인한다. 같은 세션의 수백 호출을 모두 독립 표본으로 취급하지 않는다. 시간 모델은 사용자·세션 단위로 학습/검증을 나누고, NVIDIA는 `instance_id` 단위로 분리한다. 초기 실험 규모의 예로 mini 100~264개, 확대 실험의 예로 NVIDIA 고유 작업 200~500개를 제안할 수 있지만 고정된 통계적 충분성 기준은 아니다. 언어, 저장소, 작업 길이, 성공/실패를 고려해 선정하고 재생 실패도 보고한다.

### NVIDIA의 명령에 TraceLab 시간을 붙일 수 있는가

구현은 가능하며 명시적인 합성 부하로 시스템을 비교하는 용도에 적합하다. NVIDIA는 저장소 작업의 명령·결과·추론 텍스트를 제공하고, TraceLab은 별도의 실사용 시간 관측을 제공한다. 두 자료를 결합하면 GPU 없이 실제 명령을 실행하며 응답 대기를 흉내 낼 수 있다.

그러나 NVIDIA의 데이터 품질과 다른 자료의 시간 패턴이 NVIDIA에 맞는지는 별개의 문제다. NVIDIA는 모델 학습/증류용 행동 데이터이며 성공·실패·결과 미상 실행을 포함한다. 품질 필터와 `resolved` 표시는 원래 실행 시간, 환경의 완전한 복원, TraceLab 지연 분포의 전이 가능성을 보증하지 않는다. 특히 TraceLab은 Claude/Codex와 사람의 상호작용이고 NVIDIA v1.2는 Qwen3.8-27B/mini이므로 모델·서빙·harness가 다르다.

[TraceLab 시간 분석](https://tracelab.cs.washington.edu/exp/session/session_timing_distribution/)은 사람이 다음 요청을 입력하는 대기와 모델 응답·도구 실행 구간을 구분한다. 게시된 전체 세션 시간에서 사람 대기가 차지하는 비중은 91.6%다. 이것을 자동화된 rollout의 모델 대기로 옮기면 idle을 크게 과장할 수 있다. [generation 시간 정의](https://tracelab.cs.washington.edu/exp/llm_generation/generation_time_cdf/) 역시 서빙 엔진에서 직접 잰 시간이 아니라 이벤트에서 계산한 관측 구간이다.

권장 방식은 다음과 같다.

1. TraceLab에서 timestamp가 있고 정상적으로 연결되는 도구 결과→다음 모델 출력 구간을 추출한다. 인간의 다음 입력 대기는 분리한다. streaming, 병렬 호출, background 작업의 겹침은 감지하고 초기에는 해석 가능한 직렬 구간을 사용한다. 공식 generation span과 재생에 사용할 batch 경계 지연의 정의 차이도 기록한다.
2. 처음에는 GPU 학습 없이 조건별 경험 분포를 사용한다. 결과 길이, 문맥 크기, 모델 출력 길이, 작업 단계, 직전 명령 종류/묶음 크기처럼 두 데이터에서 관측하거나 일관되게 근사할 수 있는 특성을 고려한다. 토큰 수를 근사할 경우 tokenizer와 숨겨진 reasoning 누락을 명시한다. NVIDIA teacher model과 같은 TraceLab 모델이 없으면 자동으로 같은 지연을 가정하지 않는다.
3. 각 호출의 지연을 독립적으로 뽑는 방식은 비교용으로 둔다. 짧은/긴 대기의 연속성, 세션별 속도 차이를 유지하려면 상태별 분포와 연속 구간 재표집 등을 사용한다. 단순 지연 히스토그램만 맞추면 burst 구조가 사라질 수 있다. 초기부터 복잡한 신경망 모델이 필요한 것은 아니다.
4. NVIDIA v1.2의 assistant 응답 묶음 경계에 대기를 붙인다. 원래 mini가 묶어서 실행한 명령 사이에 매번 모델 대기를 삽입하지 않는다. 명령 자체는 컨테이너에서 실행해 실제 소요 시간과 CPU/메모리를 측정한다. TraceLab 도구 소요 시간을 추가 sleep으로 붙이면 이미 실행한 명령 시간과 이중 계산할 수 있다.

개념적인 실행 순서는 `합성 응답 대기 → 기록된 명령 묶음 실제 실행 → 다음 합성 응답 대기`다. 작업 간 생성 도착 시각은 별도의 실험 입력으로 취급한다. 세션 내부 지연 합성이 sandbox 생성 burst를 복원하지는 않는다. NVIDIA는 task image/commit 연결이 별도로 필요하므로 데이터 크기와 재생 가능한 작업 수가 같다고 가정하지 않는다.

### 합성의 검증과 주장 범위

먼저 시간이 있는 mini 파일의 timestamp를 숨겨 놓고 같은 방법으로 지연을 생성한 뒤 실제 관측값과 비교한다. mini의 모델과 TraceLab 모델이 다르므로 이 비교는 전이 가정을 시험하는 것이며 일치를 보장하지 않는다. 작은 표본의 p99 수치를 과신하지 말고 p50/p90, tail 모양, 연속 지연의 상관, 여러 시간 창의 호출 수 변동을 확인한다. TraceLab 내부에서도 사용자·세션을 분리한 검증이 필요하다.

실제 시스템 비교에는 원본 mini 시간, 일정한 대기, 독립 분포 추출, 연속성과 조건을 보존한 합성을 각각 사용해 결론이 시간 모델에 얼마나 의존하는지 확인한다. 합성 대기를 0.5/1/2배로 바꾸는 민감도 실험도 유용하다. 동일 작업·합성 seed를 DRAM/CXL 조건에서 짝지어 실행하고 setup/cache 조건을 맞춰 비교한다. CPU 시계열은 별도로 수집해야 한다.

합성으로 burst를 넣고 재생에서 burst를 관측하는 것은 원래 NVIDIA 실행의 burstiness나 DSec의 주장을 독립적으로 입증하지 않는다. 이 경우 가능한 주장은 **TraceLab 관측에 근거해 가정한 시간 패턴에서 worker/CXL 방식의 효과를 평가했다**는 것이다. DSec 수치에 맞춰 시간을 조정한 뒤 그 수치가 재현됐다고 검증을 주장하는 방식은 피한다.

권장 순서는 원본 mini의 시간 기록으로 기본 실험을 시작하고, NVIDIA의 더 다양한 명령에 TraceLab 기반 시간을 합성해 확대하는 것이다. 이때 실제 시간 기록을 쓴 결과와 합성 시간 결과를 구분해 보고한다. [추가 확인 기록](evidence/timing-transfer-assessment.json)에 표본 검사와 수치의 출처를 저장했다.
