# DRAM / CXL 재생 결과 (2026-10-14)

같은 264개 trace를 node 0 CPU + node 0 DRAM에서 실행한 뒤 node 0 CPU + node 2 CXL에서 실행했다. 두 조건 모두 CPU 2-31, 작업당 메모리 32GiB, 부모 상한 33GiB, PID 16384, swap 0이다. timestamp/gap 대기만 제거하고 명령 내부 sleep·I/O·timeout은 유지했다.

총 528회, 27,200개 명령을 완료했고 MPKI 계측은 전부 유효했다. OOM·PID 제한·시작/종료 NUMA 배치 오류는 없었다. 원본 Submitted 260개는 두 조건 모두 정규화한 제출 내용이 일치했다. 원본 LimitsExceeded 3개와 InternalServerError 1개는 제출 비교 대상이 아니다.

| 지표 | 결과 |
|---|---:|
| 비교 가능한 trace 쌍 | 211 / 264 |
| CXL latency 증가: 비율 기하평균 / 시간 합계 | 13.15% / 17.38% |
| 중앙값 / p95 / p99 / 최대 증가율 | 11.16% / 42.79% / 53.75% / 79.85% |
| 전체 instructions로 가중한 LLC MPKI: DRAM / CXL | 1.5357 / 1.4836 |
| 최대 메모리 GiB: DRAM / CXL | 18.08 / 20.27 |

![Trace slowdown CDF](slowdown-cdf.png)

timeout이 있는 50개 쌍과 종료 코드가 다른 3개 쌍은 대표 latency 비교에서 제외했다. 39개 trace는 두 조건 모두, 11개는 CXL에서만 timeout이 있었다. 나머지 3개는 Preact focus 이벤트 테스트 두 건과 Caddy ACME 테스트 한 건의 성공/실패 차이다. 제외된 결과도 CSV에 남아 있다.

latency는 Docker exec 시작부터 종료까지의 호스트 monotonic 시간이다. timeout 뒤 컨테이너 내부 작업이 살아 있을 수 있어 후속 명령과 겹칠 수 있다. 각 조건 1회·고정 DRAM→CXL 순서이며 공유 cache를 비우거나 다른 호스트 작업을 중지하지 않았다. 따라서 순서·cache·경합을 분리한 인과 추정은 아니다. MPOL_BIND로 payload 익명 메모리를 지정했지만 모든 공유 파일 cache 접근을 CXL로 강제한 것은 아니다.

## 보관 자료

- `paired-traces.csv`, `paired-commands.csv`: 전체 비교값과 제외 사유. 대표 비교는 `eligible=True`만 사용한다.
- `node0/traces.csv`, `node2/traces.csv`: CPU·메모리·MPKI·IPC·NUMA·I/O 요약.
- `summary.json`, `monitor-final-validation.json`: 집계와 전체 실행 검증.
- `run.json`: 당시 설정·커널·PMU 정의·측정 코드 hash·실제 trace 순서.
- `image-lock.json`: 실제 사용한 image ID와 export 시 로컬에서 확인한 registry digest. 원본 trace 수집 당시 digest는 알려져 있지 않다.
- `slowdown-cdf.{png,pdf,csv,json}`, `comparison.png`, `report.md`: 그림·그림 데이터·상세 비교표.

원시 events/metrics/perf/task 로그, 다운로드한 trace와 Docker 이미지는 제외했다. 원시 결과는 측정 서버의 `harness/trace-replay/results/numa-immediate-20261014/`에 남아 있다. 이 export로 CDF는 다시 그릴 수 있지만 원시 계측을 다시 요약할 수는 없다.

## 다른 머신에서 이어가기

저장소 최상위에서 실행한다. CDF 재생성에는 Python과 matplotlib만 필요하다.

```bash
python -m pip install matplotlib
python harness/trace-replay/analyze.py --cdf docs/results/numa-immediate-20261014
```

새 실험은 [최상위 README](../../../README.md)의 호스트 준비·trace 다운로드·비교 실행 절차를 따른다. 기록한 순서가 필요하면 `run.json`의 `trace_order`를 사용한다. 각 파일 경로는 `harness/trace-replay/data/native/<name>.traj.json`이고 SHA-256도 함께 기록돼 있다. 이미지는 `image-lock.json`의 registry digest로 pull하고, image ID 일치를 확인한 뒤 원래 reference로 tag한다. CXL node와 CPU 번호, perf binary 경로는 새 호스트에서 확인한다.

`run.json`의 코드 hash는 측정 당시 버전을 가리킨다. 현재 분석 코드에는 측정 후 CDF 재생성 기능이 추가됐다.
