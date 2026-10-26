# DSec 규모 contention 검증 계획 (2026-10-21, 실행 전에 고정)

목표: motivation 세 질문에 데이터로 답한다.

- **Q1.** DSec 규모 node(192코어, sandbox 수백 개)에서도 node별 CPU contention이 생기는가? 생긴다면 어떤 조건에서인가?
- **Q2.** 그 contention을 1초대 full VM migration은 못 따라가고 10ms hand-off는 따라가는가?
- **Q3.** RL rollout의 step 시간에 얼마나 차이가 나는가?

판정 기준은 이 문서에 먼저 적고, 결과를 본 뒤 바꾸지 않는다.

## 도구

| 도구 | 역할 | 검증 상태 |
|---|---|---|
| `sandbox.py solo` | trace별 단독 실행 burst(10ms vCPU 실행 시간). 1 vCPU와 4 vCPU | 1 vCPU 67개 완료 |
| `sandbox.py pilot` | 실제 node(cpuset) 실행. 분리·공유, GRPO 그룹, 이동 흉내(`--move`) | 4×5 실측 62회 |
| `sim.py` | 기준 simulator (Python) | 4×5 실측과 일치 |
| `fleet.py` | numba simulator. power-of-k 배치, 여러 vCPU, 메모리 비례 migration, NIC 직렬화, RL job 모드 | 손계산 검사 6개, `sim.py` 회귀(±0.2%p) |

다른 agent가 socket 0(CPU 0~5)을 쓰므로 실제 실행은 socket 1에서만 한다.
- node 2개 × 5코어 (13-17, 18-22)
- harness CPU: 12, 23, 36, 47
- memory node 1

## 입력

- trace 264개 (`candidates-all.json`). guest 메모리는 Docker peak × 1.5(최소 2GiB, 최대 24GiB).
- 단독 실행: 1 vCPU (코어 13~23, 11개 병렬), 4 vCPU (코어 13~20, 2개 병렬). 네트워크 켬.
- simulation에 쓰는 trace: 단독 실행이 끝까지 돌았고, 종료 코드 불일치가 명령의 10% 이하인 것.

## 검증 사다리

| 단계 | 실측 조건 | 비교 | 합격 기준 |
|---|---|---|---|
| V1 (완료) | 4×5, 짧은 67개, 1 vCPU, 분리·공유, 코어당 2.0~5.4 | `sim.py`, `fleet.py` | 대기/실행이 5% 이상이면 상대 ±20%, 미만이면 ±1%p |
| V2 | 2×5, **264개**, 1 vCPU, node당 14·21개, 분리·공유(1×10), seed 1·2 | `fleet.py` 같은 구성 | V1과 같음. trace 지연 p50·p90 ±0.03 |
| V2b | 2×5, 264개, **4 vCPU**, node당 6·10개, 분리·공유, seed 1 | `fleet.py` (4 vCPU 단독 실행 입력) | V1과 같음 |
| V4 | 2×5, 264개, 1 vCPU, node당 14·21개, `--move` instant / hand-off 10ms / stop-and-copy / pre-copy, seed 1·2 | `fleet.py` 같은 메커니즘 | 메커니즘 순위가 같고, 이동 없음 대비 감소율 차이 ±10%p 이내 |
| V5 | 2×5, 264개, GRPO G=8 (pilot `--group-size 8`), node당 16개, 분리, seed 1 | `fleet.py` RL 모드(B=1, G=8, 학습 0초) | step 지연 p50 ±0.05 |

합격하지 못한 단계가 있으면 원인을 고치고 다시 검증한다. 그 전에는 S 단계 결과를 결론에 쓰지 않는다.

**실행 전 수정 (2026-10-21 10:00 UTC, V 실행 전)**: 위 표의 밀도는 짧은 trace(sandbox 평균 0.17코어) 기준이었다. 264개 단독 실행 결과 평균 수요가 1 vCPU 0.25코어, 4 vCPU 0.425코어라 node당 21개면 과포화다. 그래서 사용률 기준으로 다시 정한다. 기준과 합격선은 그대로다.
- 검증 trace: 두 단독 실행 모두에서 쓸 수 있는 242개 (`candidates-v.json`). 실제 실행과 simulation이 같은 목록을 쓴다.
- V2: 1 vCPU, node당 12·16개(사용률 약 60·80%) × 분리·공유 × seed 1·2, node당 20개(약 100%) × 분리·공유 × seed 1
- V2b: 4 vCPU, node당 6·9개(약 51·77%) × 분리·공유 × seed 1
- V4: 1 vCPU, 이동 4종 × node당 16개 × seed 1·2, node당 12개 × seed 1. 이동 없음 기준은 V2 실행
- V5: GRPO G=8, node당 16개, 분리·공유, seed 1

## 큰 규모 실험 (`fleet.py`, V 단계 통과 후)

공통: node 16개, power-of-2 배치, 측정 600초(ramp 120초), seed 3개, 264개 trace.

| 실험 | 축 | 질문 |
|---|---|---|
| S1 node 크기 | 코어 5/10/20/48/96/192, 코어당 2.7·4.2·5.5, 1·4 vCPU | Q1: node가 커지면 contention이 어떻게 줄어드는가 |
| S2 사용률 | 192코어, 코어당 밀도를 사용률 50~95%가 되게 sweep | Q1: DSec 운영 상한(사용률 80%)에서 contention이 있는가 |
| S3 burst 상관 | RL job 모드: B=8·32, G=8, 학습 30초, job 수로 밀도 조절. 독립 rollout과 비교 | Q1·Q3: 같은 task rollout의 동기화된 burst |
| S4 메커니즘 | S1~S3에서 잃은 CPU가 1% 이상인 조건마다: 이동 없음, 이상적 공유, 즉시, hand-off 1ms·10ms, migration stop-and-copy·pre-copy(trace별 메모리, NIC 25·37·100Gbps). 정책 4개 중 최선 | Q2·Q3 |
| S5 민감도 | LLM 대기 ×0.5·×2, 배치 k=1·2·4 | 결론의 견고성 |

## 판정

- **Q1 부정**: S1~S3 모두에서 192코어 node·사용률 80% 이하의 잃은 CPU가 1% 미만이면, "node별 contention" motivation은 성립하지 않는다. 그 경우 사용률을 높여 node 수를 줄이는 쪽("같은 지연 목표에서 필요한 node 수")으로 재구성할지 검토한다.
- **Q2 부정**: S4에서 2GiB 이상 migration(가장 유리한 정책)이 hand-off 감소분의 절반 이상을 되찾으면, "migration으로 충분하다"는 반론에 진다.
- **Q3**: RL step 지연 차이가 5% 미만이면 end-to-end 동기로는 약하다.

## 산출

- `RESULTS-fleet.md`: V 단계 표(합격 여부), S 단계 표와 그림, 판정.
- 그림: node 크기별 잃은 CPU, 사용률별 잃은 CPU, 메커니즘별 회수율, RL step 지연.
