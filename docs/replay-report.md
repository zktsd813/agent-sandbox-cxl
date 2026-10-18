# NUMA node 0에서 mini trace 재생 실험

> 초기 조사/실험 기록이다. 현재 실행 설정과 완료 결과는 [최신 README](../README.md)를 따른다.

작성 시각: 2026-10-07T08:31:17.592104+00:00

이 문서는 기준 서버에서 완료한 실험의 기록이다. 다른 머신의 준비 절차는 [저장소 README](../README.md)를 따른다. 원시 계측, 세부 보고서와 그래프는 기준 서버의 `harness/trace-replay/results/`에 보관하며 소스 배포에는 포함하지 않는다.

16개 trace의 실제 재생을 완료했다. 전체 264개 실험은 당시 Docker Hub 익명 pull 한도로 막혀 시작하지 못했다. 기준 서버에는 task 이미지 79/264개가 캐시에 있었으며 나머지 185개 다운로드를 위해 Docker Hub 인증이 필요했다. 사용자 계정과 관리자 계정 모두 저장된 Docker 로그인 설정이 없었다. 이 캐시 상태는 다른 머신에 적용되지 않는다.

| 항목 | 16개 검증 결과 |
|---|---:|
| 완료 trace / 실행 명령 | 16 / 680 |
| 원본 종료 코드 비교 / 불일치 | 664 / 6 |
| 제출 원문 해시 일치 | 10 / 16 |
| 동일한 시작 경고의 위치만 제외한 제출 내용 일치 | 16 / 16 |
| NUMA 익명 메모리 이탈 / swap / OOM / runner 오류 | 0 / 0 / 0 / 0 |

검증은 4개씩 네 묶음으로 실행했다. 각 묶음 안에서는 첫 명령을 함께 시작하고 이후 명령은 각 trace의 상대 timestamp를 따랐다. 전체 작업 사이의 원래 시작 시각을 재현한 실험은 아니다. CPU 2-5를 공유하고 컨테이너당 memory node 0의 4GiB 및 PID 512 상한을 적용했다. Controller와 계측기는 CPU 0-1, memory node 0으로 묶었다.

| 묶음 | 명령 | 코드 불일치 | 최대 동시 Docker exec | 최대 합계 메모리 GiB | 상세 보고서 |
|---|---:|---:|---:|---:|---|
| 1 | 168 | 0 | 4 | 2.956 | `pilot16/batch1-retry/report.md` |
| 2 | 137 | 0 | 4 | 1.641 | `pilot16/batch2/report.md` |
| 3 | 197 | 3 | 4 | 0.371 | `pilot16/batch3/report.md` |
| 4 | 178 | 3 | 4 | 5.774 | `pilot16/batch4/report.md` |

종료 코드 차이 6건은 Three.js 명령 3건의 원본 timeout이 이번에는 정상 종료된 경우, Preact 빌드의 프로세스 생성 EAGAIN과 browser focus 테스트 실패 각 1건, Tokio cargo test의 60초 timeout 1건이다. Browser 버전은 원본과 재생 모두 Chrome Headless 151.0.0.0이므로 버전 차이로 설명하지 않았다.

추가 재검증에서 PID 상한을 4096으로 높이자 Preact는 약 3,100개 PID를 사용했고 4GiB 상한에서 OOM이 발생했다. 이후 작업별 상한 16GiB와 전체 컨테이너의 공유 메모리 상한을 적용한 재검증은 55개 명령을 끝까지 실행했다. 시작 시 여유 메모리에 따라 실제 공유 상한은 43.306GiB였다. 작업 최대 사용량은 4.813GiB, PID 최대는 3316개였고 OOM·PID 상한 도달은 없었다. 원본과 다른 종료 코드 2건은 `npm run test:karma`와 `npm run test:karma:minify`의 browser focus 테스트 실패이며 제출 원문은 일치했다. 기준 서버의 `results/preact-shared-budget/report.md`에 별도로 기록했다.

원문 해시는 stdout/stderr 순서가 달라지면 달라진다. 경고 위치를 제외한 비교에서도 경고 횟수와 나머지 내용이 모두 같아야 일치로 판정했다. 제출 내용 일치는 SWE 정답 채점 결과가 아니다.

## 현재 관측된 자원 패턴

최초 16개 표본에서 trace별로 0.05 core 미만을 사용한 관측 시간 비율의 중앙값은 84.6%였다. 네 묶음의 평균 CPU 사용량은 각각 0.846, 1.663, 0.331, 1.830 core였다. 이는 CPU 4개를 공유한 초기 제한 조건의 결과이며, 프로세스 상한으로 빌드가 실패한 작업도 포함하므로 전체 workload의 사용률로 일반화하지 않는다.

추가 Preact 재검증에서는 평균 CPU 사용량 0.894 core에 비해 1초 구간 샘플 평균의 최대가 28.548 core였고, 0.05 core 미만인 관측 시간 비율은 80.7%였다. 빌드·테스트가 CPU를 집중적으로 사용하고 그 사이 CPU가 낮아지는 패턴을 관측했다. CPU 사용이 낮아진 뒤에도 파일 cache 등을 포함한 메모리가 남아 있는 구간이 있다. 낮은 CPU 사용 시간을 모든 프로세스가 정지한 시간으로 해석하지 않는다.

추가 Preact 재검증의 그래프는 기준 서버의 `results/preact-shared-budget/timeline.png`에 있다.

이 결과는 trace의 명령과 기록된 대기 간격을 이 서버에서 재생하면 나타나는 패턴이다. 아직 전체 264개를 원본 시작 시각에 맞춰 측정하지 않았으므로 전체 도착 burst, 동시 부하의 메모리 합계, CXL 효과에 대한 결론은 내리지 않는다.

## 전체 실험 계획

[공식 mini 제출의 고정 commit](https://github.com/SWE-bench/20260902_mini-v2.4.6_gemini-3-5-flash/tree/8dcca98587e7f698753903e3b1747eb5dc013cb1)에서 원본 264개, 37개 저장소의 trace를 받았고 Git blob 해시를 검증했다. 264개 모두 파서 검증을 통과했으며 기록된 명령은 총 13,600개다. 원본 상태는 Submitted 260개, LimitsExceeded 3개, InternalServerError 1개이며 중단된 trace도 기록된 구간만 재생한다.

전체 timestamp 구간은 6,822.64초, 약 114분이고 관측된 작업 시간창은 최대 12개 겹친다. 전체 실험에서는 이 상대 시작 시각을 유지하고 첫 명령 시각에 컨테이너를 생성한다. CPU 2-31을 공유하며 memory node 0을 사용한다. 최대 resident 수는 12개로 정하고, 슬롯 대기·컨테이너 준비·명령 실행 때문에 생긴 지연을 기록한다. 작업 하나에는 최대 16GiB 및 PID 4096개를 허용하고, 전체 실험 메모리 합계는 kernel cgroup이 최대 48GiB 및 시작 시 node 0 free memory의 80% 이하로 제한한다. 모든 작업의 상한을 합친 크기만큼 메모리를 예약하지 않는다. 개별 작업이 자원 제한으로 실패하면 해당 컨테이너를 종료하고 다른 trace는 계속 실행해 실패 원인도 보고한다.

전체 264개를 원본 timestamp에 따라 같은 실험에서 재생하는 계획이다. 264개를 같은 순간에 시작하는 것은 원본 시간 패턴을 바꾸는 별도 부하 실험이며 현재 계획에 포함하지 않았다.

GPU와 LLM 호출은 사용하지 않는다. 명령 간 대기는 원본 timestamp에서 온다. 원본에 sandbox 생성 시각이나 수집 당시 이미지 digest가 없으므로 첫 명령 시각을 원래 sandbox 도착 시각으로 해석하지 않는다. 현재 이미지 ID는 각 실험의 manifest/events에 기록한다. 공유 파일 cache는 다른 NUMA node 페이지를 사용할 수 있어 별도 측정한다. Docker daemon과 기존 호스트 서비스의 CPU·메모리는 workload 계측 합계에 포함하지 않는다.

전체 실험용 코드와 공유 메모리 제한은 준비됐으며, 실제 공유 그룹 생성·제한 적용·정리와 프로세스 중단/timeout/실패 격리 검증을 완료했다. 필요한 호스트 도구는 이미 설치돼 있다. 공유 상한 설정에는 비대화형 sudo 권한과 로컬 systemd Docker daemon이 필요하며 이 서버에서 확인했다. Docker Hub 인증은 [Docker 공식 안내](https://docs.docker.com/docker-hub/usage/pulls/#authentication)에 따라 서버 터미널에서 `docker login`으로 진행한다. 로그인 완료 후 캐시를 재사용해 남은 이미지를 받고, 전체 replay와 CPU·메모리·동시성·지연·실패 분석을 이어서 진행할 수 있다. 이 서버의 재생 관측을 DSec 운영 trace 자체의 검증 결과로 해석하지 않는다.

전체 재생 명령은 이미지 준비 후 다음과 같다. 설정 및 입력/코드 해시, 실제 이미지 ID, 공유 그룹 자원, 명령 시작·종료와 실패를 결과에 기록한다.

```bash
python harness/trace-replay/experiment.py \
  --setup on-arrival --max-active 12 --workload-cpus 2-31 --keep-going \
  --output harness/trace-replay/results/full264 \
  harness/trace-replay/data/native/*.traj.json
python harness/trace-replay/analyze.py \
  harness/trace-replay/results/full264
```
