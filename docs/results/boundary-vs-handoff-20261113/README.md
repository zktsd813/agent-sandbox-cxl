# 명령 경계 배치 대 실행 중 hand-off, 같은 tail의 사용률 (2026-11-13)

simulator 분석이다. 다언어 264 trace의 10 ms CPU profile과 기록된 LLM 간격을 재생했다. 실측 VM·lab·Docker를 새로 실행하지 않았다. 전체 수치는 [summary.json](summary.json), 모든 작업량·코어·사용률·변형의 표는 [tables.md](tables.md)에 있다.

## 0. 결론

- **경계 배치가 pool 이득의 대부분을 가져간다.** cap30·목표 80%에서 oracle 경계 1 ms의 CPU 가중 slowdown은 48/96/192코어 순으로 1.0732 / 1.0260 / 1.0079다. pool 이득의 68.8~77.9%를 가져가고, reactive를 더하면 11.9~13.7%p를 추가로 가져간다.
- **작은 노드에서는 reactive가 최대 사용률을 더 올린다.** 48코어·CPU 가중 slowdown ≤ 1.10의 최대 실제 사용률은 none 62.5%, oracle 경계 84.4%, reactive 87.5%, 결합 88.1%다. none 대비 노드 절감은 경계 26.0%, reactive 28.6%, 결합 29.1%다. 경계만 쓸 때와 비교한 결합 정책의 추가 노드 절감은 4.2%다.
- **192코어의 같은 SLO에서는 경계 배치와 reactive의 최대 사용률 차이가 아직 식별되지 않는다.** none은 87.7%이고 두 정책은 모두 측정 상단 약 94%까지 통과했다. 이때 none 대비 노드 절감의 확인된 하한은 약 7%다. 측정 밖의 최대점은 외삽하지 않는다.
- **정지 시간이 길면 CPU 가중 평균과 tail의 판단이 갈린다.** 192코어·목표 90%의 경계 L=1 s는 CPU 가중 1.066이지만 heavy p99 3.09로, none의 1.61보다 나쁘다.
- **class 예측도 oracle에 가까운 회수율을 보인다.** 목표 80%에서 class 경계 1 ms는 pool의 CPU 가중 이득 67.5~76.5%를 가져간다. 명령 recall 84.7%, precision 77.6%, CPU 가중 recall 98.5%다.
- **Python RL의 최대 사용률은 짧은 구간만으로 확정하지 않았다.** 1 vCPU·48코어·목표 90%·gap 1의 none은 측정 900초에서 실제 U 62.9%·CPU 가중 1.007, 4,500초에서 U 86.7%·CPU 가중 1.281다. 긴 구간은 이상적 pool도 1.188으로 지연이 남았다. ramp를 3,600초로 넓힌 추가 검증은 none U 88.0%·CPU 가중 1.055로 초기조건에도 민감했다. 목표 90%의 밀도 약 41 sandbox/코어·9.1 GiB/코어는 사용자 기준 bud05의 4 GB/코어도 넘는다.

## 1. 질문

1. 명령 시작 때 무거운 CPU를 예측해 한 번 옮기면, 실행 중 reactive hand-off와 이상적 공유의 이득을 얼마나 대체하는가?
2. CPU 가중 slowdown ≤ 1.05/1.10, heavy 명령 p99 ≤ 1.5/2.0을 유지하는 최대 실제 사용률과 필요한 노드 수는 얼마인가?
3. 경계 배치 뒤 남는 손실은 시작 때의 자리 부족, 이후의 겹침, 가벼운 명령 중 어디에 있는가?

## 2. 방법

| 항목 | 설정 |
|---|---|
| simulator | [`fleet.py`](../../../experiments/contention/fleet.py)의 fluid thread water-filling, carry=True, kernel_wait 없음. 기본 경계 배치는 꺼져 있고 기존 지표를 유지한다 |
| 다언어 입력 | 10-14 Docker DRAM 264 trace / 13,600 명령. 현재 checkout에 원본이 없어 옛 checkout을 읽기 전용으로 사용. [`cap_sweep.py`](../../../experiments/contention/cap_sweep.py)의 같은 10 ms profile과 cap stretch |
| cluster | 16노드 × 48/96/192코어. 새 sandbox는 floating power-of-2, 정책 쌍에 같은 seed와 안정적인 placement RNG |
| 경계 | heavy 명령의 첫 step에 한 번. boot 제외. 직전 수요, margin=1코어, 모든 목적지 중 최선(JSQ). 목적지에 폭을 예약해 같은 step의 중복 예약 방지. 이동 중 reactive 선택 제외 |
| 예측 | oracle: profile CPU ≥ 1 s, 폭=CPU/solo 길이. class: 옛 `cmdkind.kind()`로 test/build/install, 폭은 같은 repo의 다른 trace의 class-heavy 평균 폭 중앙값. 없으면 다른 모든 trace의 중앙값. 자기 trace를 학습에서 제외 |
| 반응 | hand-off 실제 정지 10 ms, detect 50 ms, hold 1 s, max_batch=8. strict_handoff=True로 step 말 결정의 경계를 보정하고 source CPU 2 ms도 정확한 budget으로 계산. 기본 False는 기존 결과를 보존. pre-copy는 detect 500 ms / hold 5 s, 100 Gb/s NIC와 trace별 guest 메모리 |
| 경계 비용 | L=1/10/100/1000 ms. source CPU 입력은 1 ms(10 ms 변형은 2 ms). 100/1000 ms는 지연 효과를 보기 위해 CPU 비용을 1 ms로 고정. 경계 정지는 부분 step으로 반영하며 명령 종료 step의 남는 시간도 보간 |
| 시간·seed | cap30/cap16: ramp 600 + 측정 5,400 s, seed 2. 새 RL 비교: RL1 600+900 s, RL8 600+300 s. 정상적인 기존 RL1 none/pool/bnd100의 4,500 s 결과는 긴 구간 control로 보존. 동기 step: 1,800+7,200 s. 총 1448개 완료 설정. 미완료 seed 쌍 0개는 표에서 제외 |
| 지표 | 측정 구간 안에서 시작하고 끝난 명령. solo는 해당 정책의 segment 길이, CPU 가중 평균은 profile CPU-s로 가중. 분위수는 seed별 분위수 두 개의 산술평균. lost=(대기+정지 중 못 한 CPU)/실행 CPU |
| timeout | main은 기존 cap sweep처럼 timeout 비활성(1e6 s). 기본 simulator의 pause 후 timeout 확인 동작을 보존하고, strict_timeout=True인 60 s 절단을 192코어·80/90%에서 별도 비교. strict 모드는 정지 중에도 60 s에서 자른다. 잘린 명령의 전체 solo 분모는 유지하므로 slowdown이 낮게 편향될 수 있음 |
| SLO | x=run_cores_per_node/cores, 측정점 사이 선형 보간. 노드 비=N_X/N_none=U_none/U_X. 최고점 통과는 최대 사용률의 하한, 이에 따른 노드 비는 상한. 최저점도 실패하면 구간 내 최대점을 산출하지 않음 |

[`boundary.py`](../../../experiments/contention/boundary.py)는 grid 생성·재개·부하 검사를 담당한다. 입력과 코드 SHA256은 summary의 metadata에 남겼다. 수정의 영향을 받지 않아 보존한 기존 행의 config ID와 당시 metadata는 retained_input_provenance에 따로 보존했다. cap16의 단독 tool 시간 비는 **1.017698**이며 contention slowdown과 별도다. 이동/s는 16노드 전체의 측정 구간 합이다.

약 4시간의 grid 예산에서 지연 보정의 재계산과 seed 2를 유지하기 위해 RL 측정 구간을 줄였다. 긴 구간의 정상 control을 함께 보존했지만, 짧은 구간과 일치한다고 가정하지 않는다. 최초 단일 설정 wall은 다언어 81.44초, RL의 600초 측정 benchmark는 360.51초였다(입력 준비 제외, JIT가 발생하면 포함).

## 3. 결과

### 3.1 같은 목표 사용률 80%의 명령 지연과 손실

| 코어 | 정책 | 실제 U % | CPU 가중 | heavy p99 | 잃은 CPU % | 경계 이동/s | reactive 이동/s |
|---|---|---|---|---|---|---|---|
| 48 | none | 77.3 | 1.230 | 2.294 | 34.40 | 0.00 | 0.00 |
| 48 | pool | 80.4 | 1.002 | 1.038 | 0.35 | 0.00 | 0.00 |
| 48 | bnd-oracle-1ms | 79.4 | 1.073 | 1.330 | 13.10 | 6.94 | 0.00 |
| 48 | bnd-class-1ms | 79.3 | 1.076 | 1.407 | 13.57 | 7.32 | 0.00 |
| 48 | reactive | 79.7 | 1.048 | 1.223 | 7.99 | 0.00 | 43.03 |
| 48 | bnd-oracle-1ms+reactive | 79.7 | 1.046 | 1.195 | 7.65 | 6.07 | 41.15 |
| 96 | none | 78.4 | 1.098 | 1.669 | 14.70 | 0.00 | 0.00 |
| 96 | pool | 79.7 | 1.000 | 1.003 | 0.02 | 0.00 | 0.00 |
| 96 | bnd-oracle-1ms | 79.4 | 1.026 | 1.145 | 4.60 | 9.04 | 0.00 |
| 96 | bnd-class-1ms | 79.3 | 1.028 | 1.181 | 4.89 | 9.62 | 0.00 |
| 96 | reactive | 79.6 | 1.015 | 1.092 | 2.55 | 0.00 | 31.93 |
| 96 | bnd-oracle-1ms+reactive | 79.6 | 1.014 | 1.079 | 2.39 | 6.29 | 30.22 |
| 192 | none | 79.4 | 1.036 | 1.324 | 5.43 | 0.00 | 0.00 |
| 192 | pool | 79.8 | 1.000 | 1.000 | 0.00 | 0.00 | 0.00 |
| 192 | bnd-oracle-1ms | 79.8 | 1.008 | 1.056 | 1.39 | 9.85 | 0.00 |
| 192 | bnd-class-1ms | 79.8 | 1.008 | 1.068 | 1.47 | 10.71 | 0.00 |
| 192 | reactive | 79.8 | 1.003 | 1.030 | 0.61 | 0.00 | 18.30 |
| 192 | bnd-oracle-1ms+reactive | 79.8 | 1.003 | 1.025 | 0.55 | 4.44 | 17.39 |

전체 사용률 점 및 cap16/Python RL은 [metrics.csv](metrics.csv)에 있다. CPU 가중 평균, heavy p99, lost CPU는 서로 다른 가중 방식의 지표다.

### 3.2 pool 이득 중 되찾은 비율

각 cell은 **CPU 가중 / heavy p99 회수율(%)**이다. (none − X)/(none − pool)로 계산했다. 음수나 100% 초과를 자르지 않고, 분모가 거의 없으면 산출하지 않는다.

| 코어 | 목표 U % | bnd-oracle-1ms | bnd-class-1ms | reactive | bnd-oracle-1ms+reactive |
|---|---|---|---|---|---|
| 48 | 80 | 68.8 / 76.8 | 67.5 / 70.6 | 79.7 / 85.3 | 80.8 / 87.5 |
| 48 | 90 | 68.6 / 80.0 | 67.7 / 73.9 | 74.6 / 86.2 | 76.1 / 89.1 |
| 96 | 80 | 73.5 / 78.7 | 71.7 / 73.2 | 84.7 / 86.5 | 85.8 / 88.5 |
| 96 | 90 | 71.6 / 80.7 | 70.9 / 76.5 | 75.1 / 85.0 | 77.2 / 88.0 |
| 192 | 80 | 77.9 / 82.9 | 76.5 / 79.2 | 90.6 / 90.7 | 91.6 / 92.4 |
| 192 | 90 | 77.4 / 82.5 | 76.5 / 79.1 | 80.2 / 84.7 | 82.1 / 86.9 |


### 3.3 SLO를 지키는 최대 실제 사용률과 노드 수

cell은 **최대 실제 U / none 대비 필요한 노드 수 비**다. `≥U / ≤비`는 grid 상단까지 통과한 하한/상한이다. 둘 다 최대점을 찾지 못한 비교의 노드 비는 비워 뒀다. 네 SLO 전체와 cap16/RL은 [slo.csv](slo.csv)에 있다.

| SLO | 코어 | none | pool | bnd-oracle-1ms | bnd-class-1ms | reactive | bnd-oracle-1ms+reactive |
|---|---|---|---|---|---|---|---|
| CPU 가중 ≤ 1.05 | 48 | 51.2% / 1.000 | ≥93.5% / ≤0.548 | 72.2% / 0.710 | 71.0% / 0.722 | 80.0% / 0.641 | 80.5% / 0.636 |
| CPU 가중 ≤ 1.05 | 96 | 70.0% / 1.000 | ≥94.6% / ≤0.739 | 86.8% / 0.806 | 86.4% / 0.810 | 88.5% / 0.790 | 89.3% / 0.783 |
| CPU 가중 ≤ 1.05 | 192 | 82.0% / 1.000 | ≥95.1% / ≤0.862 | 94.2% / 0.870 | 94.0% / 0.872 | 93.5% / 0.876 | 94.3% / 0.869 |
| CPU 가중 ≤ 1.1 | 48 | 62.5% / 1.000 | ≥93.5% / ≤0.668 | 84.4% / 0.740 | 84.0% / 0.743 | 87.5% / 0.714 | 88.1% / 0.709 |
| CPU 가중 ≤ 1.1 | 96 | 78.6% / 1.000 | ≥94.6% / ≤0.831 | ≥93.3% / ≤0.842 | ≥93.3% / ≤0.843 | ≥93.4% / ≤0.842 | ≥93.4% / ≤0.841 |
| CPU 가중 ≤ 1.1 | 192 | 87.7% / 1.000 | ≥95.1% / ≤0.923 | ≥94.3% / ≤0.930 | ≥94.3% / ≤0.930 | ≥94.3% / ≤0.931 | ≥94.3% / ≤0.930 |
| heavy p99 ≤ 1.5 | 48 | 52.8% / 1.000 | ≥93.5% / ≤0.565 | 88.7% / 0.596 | 85.1% / 0.621 | 91.6% / 0.577 | ≥91.8% / ≤0.576 |
| heavy p99 ≤ 1.5 | 96 | 72.3% / 1.000 | ≥94.6% / ≤0.764 | ≥93.3% / ≤0.774 | ≥93.3% / ≤0.775 | ≥93.4% / ≤0.774 | ≥93.4% / ≤0.774 |
| heavy p99 ≤ 1.5 | 192 | 85.6% / 1.000 | ≥95.1% / ≤0.900 | ≥94.3% / ≤0.907 | ≥94.3% / ≤0.908 | ≥94.3% / ≤0.908 | ≥94.3% / ≤0.907 |
| heavy p99 ≤ 2 | 48 | 70.3% / 1.000 | ≥93.5% / ≤0.752 | ≥91.5% / ≤0.769 | ≥91.4% / ≤0.769 | ≥91.7% / ≤0.767 | ≥91.8% / ≤0.766 |
| heavy p99 ≤ 2 | 96 | 86.5% / 1.000 | ≥94.6% / ≤0.914 | ≥93.3% / ≤0.927 | ≥93.3% / ≤0.927 | ≥93.4% / ≤0.927 | ≥93.4% / ≤0.926 |
| heavy p99 ≤ 2 | 192 | ≥92.4% | ≥95.1% | ≥94.3% | ≥94.3% | ≥94.3% | ≥94.3% |


### 3.4 이동 지연, view, 폭, power-of-2

목표 90%, oracle 경계만 비교했다.

| 코어 | L | CPU 가중 | heavy p99 |
|---|---|---|---|
| 48 | 1ms | 1.124 | 1.471 |
| 48 | 10ms | 1.123 | 1.469 |
| 48 | 100ms | 1.129 | 1.575 |
| 48 | 1s | 1.189 | 3.564 |
| 96 | 1ms | 1.060 | 1.256 |
| 96 | 10ms | 1.061 | 1.256 |
| 96 | 100ms | 1.065 | 1.354 |
| 96 | 1s | 1.117 | 3.366 |
| 192 | 1ms | 1.025 | 1.123 |
| 192 | 10ms | 1.024 | 1.123 |
| 192 | 100ms | 1.028 | 1.222 |
| 192 | 1s | 1.066 | 3.090 |

192코어에서 기본 view=0/평균 폭/JSQ를 한 요소씩 바꾼 결과다.

| 목표 U % | 정책 | CPU 가중 | heavy p99 |
|---|---|---|---|
| 80 | bnd-oracle-1ms | 1.008 | 1.056 |
| 80 | bnd-oracle-1ms-view1 | 1.007 | 1.047 |
| 80 | bnd-oracle-1ms-peak | 1.007 | 1.055 |
| 80 | bnd-oracle-1ms-k2 | 1.009 | 1.061 |
| 90 | bnd-oracle-1ms | 1.025 | 1.123 |
| 90 | bnd-oracle-1ms-view1 | 1.021 | 1.105 |
| 90 | bnd-oracle-1ms-peak | 1.024 | 1.126 |
| 90 | bnd-oracle-1ms-k2 | 1.028 | 1.126 |


### 3.5 경계 배치 뒤 남는 잃은 CPU

다음 표는 cap30 oracle 1 ms에서 전체 lost CPU를 분모로 한 비율이다. (a)는 선택된 노드에 **실제 평균 폭**만큼 시작 여유가 있었고, 이후 그 노드에서 다른 명령 시작이 관측된 경우, (b)는 시작 때 그 폭이 들어갈 노드가 하나도 없던 경우, (c)는 CPU < 1 s 명령이다. boot, margin 때문에 맞는 노드가 있어도 못 들어간 경우, 후속 시작이 아직 관측되지 않은 시작 여유 명령은 잔차에 보존했다. (a)+(b)+(c)+잔차=100%다.

| 코어 | 목표 U % | (a) 이후 명령 시작 관측 % | (b) 맞는 노드 없음 % | (c) light % | 잔차 % | 시작 여유 전체 % |
|---|---|---|---|---|---|---|
| 48 | 80 | 97.62 | 1.60 | 0.72 | 0.06 | 97.68 |
| 48 | 90 | 90.80 | 8.32 | 0.81 | 0.07 | 90.84 |
| 96 | 80 | 99.28 | 0.03 | 0.67 | 0.02 | 99.30 |
| 96 | 90 | 96.88 | 2.32 | 0.78 | 0.01 | 96.89 |
| 192 | 80 | 99.36 | 0.00 | 0.63 | 0.01 | 99.37 |
| 192 | 90 | 98.84 | 0.41 | 0.75 | 0.01 | 98.85 |

(a)는 관측에 따른 분류다. 후속 명령이 해당 손실 step까지 계속 실행 중인지를 추적한 counterfactual 분해가 아니므로 이후 도착의 인과 효과를 단독으로 증명하지 않는다. 같은 명령의 수요가 평균 폭보다 커지는 효과도 함께 있다. 마지막 열은 후속 시작 여부와 무관한 시작 여유 전체다. 모든 작업량의 분해는 [causes.csv](causes.csv)에 있다. heavy 명령의 폭 변화와 class 오차는 다음과 같다. 폭 오차는 class-heavy 예측 명령에 대해 계산했고, 비율 오차는 실제 평균 폭이 양수인 명령을 사용했다.

| 항목 | p10 | p50 | p90 | p99 |
|---|---|---|---|---|
| 예측 폭 / 실제 평균 폭 | 0.487 | 1.002 | 1.960 | 11.017 |
| 예측 폭 − 실제 평균 폭 (코어) | -1.994 | 0.002 | 1.236 | 4.439 |
| heavy 수요 p90 / 평균 | 1.103 | 1.644 | 2.650 | 4.174 |
| heavy 수요 CV | 0.186 | 0.572 | 1.352 | 3.117 |
| rl1 heavy 수요 p90 / 평균 | 1.014 | 1.018 | 1.483 | 3.427 |
| rl1 heavy 수요 CV | 0.015 | 0.030 | 0.596 | 1.901 |
| rl8 heavy 수요 p90 / 평균 | 1.078 | 1.161 | 1.708 | 3.045 |
| rl8 heavy 수요 CV | 0.070 | 0.183 | 0.656 | 1.157 |

class의 leave-one-trace-out fallback은 4 trace였다. 실제 heavy 명령은 1,552개로 CPU의 98.4%다.

### 3.6 cap16

cap16은 solo tool 시간을 1.77% 늘린다. contention 지표와 SLO는 cap16 solo를 기준으로 한다. cap30과의 총 지연 비교에서는 별도로 보고한 cap 비용도 함께 고려해야 한다. 96/192코어 × 70/80/85/90/95% × 여섯 정책 × 두 seed는 [tables.md](tables.md)에 모두 있다.

### 3.7 Python RL과 메모리 밀도

지정된 `20261026T114153Z-solo-kx2i0s5k/run.json`은 **1 vCPU jq 단일 trace**다. 확인된 iter2 run `20261026T133107Z-solo-5i8p_9nm`(440 trace)에서 `candidates-rl.json`과 기존 usable 기준으로 **420개**를 읽었다. 8 vCPU rollout `20261104T092752Z-solo-pdkup4sy`는 같은 candidates와 **5개**만 겹친다. `20261109T015041Z`는 reward 평가용이라 사용하지 않았다. 1/8 vCPU는 같은 표본의 대조 실험이 아니다.

다음은 192코어의 밀도다. 48/96코어의 반올림 차이와 모든 정책의 실제 U는 [density.csv](density.csv)에 있다. GiB/코어 = sandbox/코어 × 228 MiB / 1024이며 [10-27 메모리 측정](../virtual-node-rl-20261026/guest-memory/README.md)의 VM 평균을 사용했으며 host cache 등은 더하지 않았다. bud05의 사용자 기준은 4 GB/코어다.

| 입력 | LLM gap 배수 | 목표 U % | 실제 U % | sandbox/코어 | GiB/코어 |
|---|---|---|---|---|---|
| rl1 | 0.25 | 50 | 49.4 | 6.15 | 1.37 |
| rl1 | 0.25 | 60 | 59.3 | 7.38 | 1.64 |
| rl1 | 0.25 | 70 | 69.4 | 8.61 | 1.92 |
| rl1 | 0.25 | 80 | 79.2 | 9.84 | 2.19 |
| rl1 | 0.25 | 90 | 89.4 | 11.07 | 2.46 |
| rl1 | 1 | 50 | 35.0 | 22.75 | 5.07 |
| rl1 | 1 | 60 | 42.1 | 27.30 | 6.08 |
| rl1 | 1 | 70 | 49.2 | 31.85 | 7.09 |
| rl1 | 1 | 80 | 56.3 | 36.40 | 8.10 |
| rl1 | 1 | 90 | 63.3 | 40.95 | 9.12 |
| rl8 | 0.25 | 50 | 48.2 | 7.24 | 1.61 |
| rl8 | 0.25 | 60 | 57.8 | 8.69 | 1.93 |
| rl8 | 0.25 | 70 | 67.3 | 10.14 | 2.26 |
| rl8 | 0.25 | 80 | 77.0 | 11.58 | 2.58 |
| rl8 | 0.25 | 90 | 86.6 | 13.03 | 2.90 |
| rl8 | 1 | 50 | 34.6 | 27.09 | 6.03 |
| rl8 | 1 | 60 | 41.4 | 32.51 | 7.24 |
| rl8 | 1 | 70 | 48.3 | 37.93 | 8.45 |
| rl8 | 1 | 80 | 55.1 | 43.35 | 9.65 |
| rl8 | 1 | 90 | 62.1 | 48.77 | 10.86 |

목표 90%에서의 contention 결과:

| 입력 | gap | 코어 | none 실제 U % | none CPU 가중 | pool CPU 가중 | bnd1ms CPU 가중 | reactive CPU 가중 |
|---|---|---|---|---|---|---|---|
| rl1 | 1 | 48 | 62.9 | 1.007 | 1.000 | 1.001 | 1.002 |
| rl1 | 1 | 96 | 63.0 | 1.001 | 1.000 | 1.000 | 1.000 |
| rl1 | 1 | 192 | 63.3 | 1.000 | 1.000 | 1.000 | 1.000 |
| rl1 | 0.25 | 48 | 88.6 | 1.050 | 1.000 | 1.008 | 1.013 |
| rl1 | 0.25 | 96 | 89.3 | 1.020 | 1.000 | 1.003 | 1.006 |
| rl1 | 0.25 | 192 | 89.4 | 1.008 | 1.000 | 1.001 | 1.002 |
| rl8 | 1 | 48 | 62.6 | 1.003 | 1.000 | 1.001 | 1.001 |
| rl8 | 1 | 96 | 62.4 | 1.000 | 1.000 | 1.000 | 1.000 |
| rl8 | 1 | 192 | 62.1 | 1.000 | 1.000 | 1.000 | 1.000 |
| rl8 | 0.25 | 48 | 86.8 | 1.028 | 1.000 | 1.010 | 1.015 |
| rl8 | 0.25 | 96 | 86.9 | 1.007 | 1.000 | 1.002 | 1.005 |
| rl8 | 0.25 | 192 | 86.6 | 1.001 | 1.000 | 1.000 | 1.001 |

1 vCPU·gap 1·48코어·목표 90%의 oracle 경계 1 ms / reactive는 CPU 가중 1.001 / 1.002, heavy p99 1.02 / 1.04다. 새 구간 pool의 CPU 가중은 1.000다. 기존 긴 구간 control과도 함께 비교해야 한다. 원인을 초기 상태나 특정 LLM phase 하나로 확정할 추가 시계열 계측은 하지 않았다. 실제 U와 목표의 차이도 그대로 보여 준다. 측정 상단까지 SLO를 만족하면 최대 사용률의 하한만 알 수 있으며, 그 경우 추가 노드 절감을 확정하지 않았다. 8 vCPU는 표본 5개와 짧은 측정 구간의 보조 결과다.

gap 0.25·48코어의 목표 90%에서는 CPU 가중 slowdown이 none 1.050, 경계 1 ms 1.008, reactive 1.013다. 이 조건은 경계 배치가 reactive보다 좋다. 이동 지연이 다른 두 정책의 비교이며, reactive가 모든 작업량에서 우세하다는 결론은 내리지 않는다.

긴 구간에서 변하지 않는 정책(none/pool/bnd100)의 정상 결과도 보존했다. 서로 다른 측정 길이를 섞어서 SLO를 보간하지 않았다.

| 코어 | 측정 s | 정책 | 실제 U % | CPU 가중 | heavy p99 |
|---|---|---|---|---|---|
| 48 | 900 | none | 62.9 | 1.007 | 1.154 |
| 48 | 900 | pool | 62.9 | 1.000 | 1.000 |
| 48 | 900 | bnd-oracle-100ms | 62.9 | 1.002 | 1.068 |
| 48 | 4500 | none | 86.7 | 1.281 | 3.259 |
| 48 | 4500 | pool | 86.5 | 1.188 | 2.524 |
| 48 | 4500 | bnd-oracle-100ms | 86.9 | 1.205 | 2.582 |
| 192 | 900 | none | 63.3 | 1.000 | 1.006 |
| 192 | 900 | pool | 63.3 | 1.000 | 1.000 |
| 192 | 900 | bnd-oracle-100ms | 63.3 | 1.000 | 1.001 |
| 192 | 4500 | none | 86.4 | 1.195 | 2.665 |
| 192 | 4500 | pool | 86.3 | 1.173 | 2.428 |
| 192 | 4500 | bnd-oracle-100ms | 86.4 | 1.177 | 2.478 |


초기조건 추가 검증: 같은 48코어·목표 90%·gap 1, 측정 900초에서 ramp를 600→3600초로 바꿨다. ramp는 sandbox 최초 출발의 분산 범위와 측정 시작을 함께 정한다. 단일 측정점이므로 최대 사용률은 외삽하지 않는다.

| 정책 | 실제 U % | CPU 가중 | heavy p99 |
|---|---|---|---|
| bnd-oracle-1ms | 88.1 | 1.007 | 1.046 |
| none | 88.0 | 1.055 | 1.520 |
| pool | 87.7 | 1.000 | 1.001 |
| reactive | 88.1 | 1.013 | 1.097 |


### 3.8 동기 RL step

cap sweep 4절의 설정: 192코어, async 밀도 목표 200%, batch16×group8, train 30 s, 같은 trace를 group 안에서 복제했다.

| 정책 | 실제 U % | step p50 | step p99 |
|---|---|---|---|
| bnd-oracle-100ms | 75.1 | 1.004 | 1.023 |
| bnd-oracle-1ms | 75.1 | 1.004 | 1.023 |
| bnd-oracle-1ms+reactive | 75.2 | 1.002 | 1.018 |
| none | 74.4 | 1.012 | 1.082 |
| pool | 75.3 | 1.001 | 1.013 |
| reactive | 75.2 | 1.003 | 1.020 |


### 3.9 60초 timeout 민감도

192코어의 절단된 명령을 포함한 지표다. main SLO 산정에는 이 표를 사용하지 않았다.

| 목표 U % | 정책 | 실제 U % | timeout 명령 % | CPU 가중 | heavy p99 |
|---|---|---|---|---|---|
| 80 | bnd-oracle-1ms | 79.7 | 0.548 | 1.007 | 1.057 |
| 80 | bnd-oracle-1ms+reactive | 79.8 | 0.544 | 1.003 | 1.024 |
| 80 | none | 79.2 | 0.562 | 1.031 | 1.326 |
| 80 | reactive | 79.8 | 0.544 | 1.003 | 1.030 |
| 90 | bnd-oracle-1ms | 89.4 | 0.558 | 1.021 | 1.121 |
| 90 | bnd-oracle-1ms+reactive | 89.5 | 0.554 | 1.017 | 1.095 |
| 90 | none | 87.7 | 0.593 | 1.079 | 1.556 |
| 90 | reactive | 89.4 | 0.556 | 1.019 | 1.109 |


## 4. 한계

- CPU runqueue 경합을 나타내는 fluid simulator다. cache·주파수·메모리 대역폭·CXL tax·실제 restore 비용·고밀도 생성 비용은 들어 있지 않다. VM·Docker 실측 검증을 이번 작업에서 새로 하지 않았다.
- 기존 reactive 경로는 10 ms 입력이 추가 정지 0 step으로 계산되는 경계 양자화가 있었다. 이번 표의 반응 정책은 strict_handoff=True로 실제 1-step(10 ms) 정지를 적용했다. 기본 False는 이전 결과 보존용이다. 이전의 낙관적인 reactive 행은 최종 집계에서 제외했다.
- cap30은 관측할 수 있었던 최대 30코어를 뜻한다. 더 큰 host에서 명령의 병렬 폭이 더 커지는 효과는 외삽하지 않았다.
- 수요·배치 결정은 10 ms 격자다. 1 ms 경계 정지는 step CPU 예산에서 부분 정지로 반영하고 마지막 step을 보간하지만, 뒤 segment의 시작은 계속 격자에 맞춘다.
- 경계와 strict reactive의 source CPU 비용은 같은 부분 step budget으로 처리했다. 기존 기본값에서만 원래의 CPU task 양자화를 유지한다. 실제 overhead/run은 metrics.csv에 보존했다.
- L=100 ms/1 s는 정지 지연의 민감도이며 특정 restore 구현의 전체 CPU 비용을 재현한 값은 아니다. source CPU를 1 ms로 고정한 이 비교는 CPU 비용이 큰 restore에 유리하다.
- heavy의 평균 폭 oracle도 이후 다른 명령 도착과 자기 명령의 폭 변화를 모른다. (a)/(b)/(c)는 admission 시점 관측에 따른 분류이며 인과적 counterfactual 분해가 아니다.
- 측정 구간 양 끝에서 미완료 명령을 제외하는 선택 효과와 seed 2개의 표본 오차가 있다. seed별 최소/최대는 summary/metrics에 보존했다. 분위수를 모든 seed의 원시 명령으로 합친 값과 구분해야 한다.
- 같은 사용률을 얻는 sandbox 밀도는 CPU 모델에서 정했다. 메모리 수용 가능성과는 별도다. 228 MiB 추정은 1 vCPU 측정에서 왔고 8 vCPU의 실측 평균 메모리를 대신하지 않는다.
- 이전 checkout은 읽기 전용 입력이다. `/Serverless`, `~/cxl-page-cache`, node 1/2 실행을 사용하지 않았다. 모든 simulator 실행은 node 0 bind/nice 19, worker 최대 24였다.

## 5. 재현

Python 3.12, numpy 2.5.4, numba 0.68.1, llvmlite 0.50.0을 현재 checkout의 `.venv`에 설치했다. 기존 raw 입력은 옛 checkout에 있어야 한다. 생성 JSONL은 git 밖 `.local/boundary/`에 두고 요약만 commit했다.

```bash
export PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMBA_NUM_THREADS=1
boundary_python=.venv/bin/python
boundary_out=experiments/contention/.local/boundary
run0() { numactl --cpunodebind=0 --membind=0 nice -n 19 "$@"; }
run0 "$boundary_python" experiments/contention/fleet.py test
run0 "$boundary_python" experiments/contention/cap_sweep.py test
run0 "$boundary_python" experiments/contention/boundary.py grid --priorities 1 2 --duration-s 5400 --timeout-s 1000000 --grid "$boundary_out/grid.json" --output "$boundary_out/results.jsonl"
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/grid.json" --output "$boundary_out/results.jsonl" --jobs 24
run0 "$boundary_python" experiments/contention/boundary.py grid --priorities 3 --duration-s 900 --timeout-s 1000000 --grid "$boundary_out/rl-grid.json" --output "$boundary_out/rl.jsonl"
run0 "$boundary_python" -c 'import json,pathlib;p=pathlib.Path("experiments/contention/.local/boundary/rl-grid.json");cs=json.loads(p.read_text());[c.update(duration_s=300.0) for c in cs if c["workload"]=="rl8"];p.write_text(json.dumps(cs,indent=1))'
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/rl-grid.json" --output "$boundary_out/rl.jsonl" --jobs 24
run0 "$boundary_python" experiments/contention/boundary.py grid --priorities 4 --ramp-s 1800 --duration-s 7200 --timeout-s 1000000 --grid "$boundary_out/step-grid.json" --output "$boundary_out/step.jsonl"
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/step-grid.json" --output "$boundary_out/step.jsonl" --jobs 24
run0 "$boundary_python" -c 'import json,pathlib;d=pathlib.Path("experiments/contention/.local/boundary");cs=json.loads((d/"grid.json").read_text());cs=[dict(c,timeout_s=60.0,strict_timeout=True) for c in cs if c["priority"]==1 and c["cores"]==192 and c["target_util"] in (80,90)];(d/"timeout-grid.json").write_text(json.dumps(cs,indent=1))'
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/timeout-grid.json" --output "$boundary_out/timeout.jsonl" --jobs 24
# 보존한 긴 RL control 162개도 같은 config ID로 재현한다.
run0 "$boundary_python" - <<'PYCONTROL'
import json
from pathlib import Path
import sys
sys.path.insert(0, "experiments/contention")
from boundary import config_id
root = Path("experiments/contention/.local/boundary")
ids = set(json.loads(Path("docs/results/boundary-vs-handoff-20261113/summary.json").read_text())["long_control_config_ids"])
configs = [dict(c, duration_s=4500.0) for c in json.loads((root / "rl-grid.json").read_text())]
configs = [c for c in configs if config_id(c) in ids]
assert len(configs) == len(ids)
(root / "long-grid.json").write_text(json.dumps(configs, indent=1))
PYCONTROL
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/long-grid.json" --output "$boundary_out/long.jsonl" --jobs 24
# 초기조건 민감도 8설정.
run0 "$boundary_python" - <<'PYWARM'
import json
from pathlib import Path
root = Path("experiments/contention/.local/boundary")
configs = [dict(c, ramp_s=3600.0) for c in json.loads((root / "rl-grid.json").read_text())
           if c["workload"] == "rl1" and c["gap_scale"] == 1 and c["cores"] == 48 and c["target_util"] == 90
           and c["variant"] in ("none", "pool", "reactive", "bnd-oracle-1ms")]
(root / "warm-grid.json").write_text(json.dumps(configs, indent=1))
PYWARM
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/warm-grid.json" --output "$boundary_out/warm.jsonl" --jobs 8
run0 "$boundary_python" docs/results/boundary-vs-handoff-20261113/analyze.py --readme --widths --final
```

run은 config SHA256으로 이미 완료한 설정을 건너뛰고 한 줄마다 flush한다. 큰 grid 전마다 uptime/상위 CPU process를 기록하고, 외부 node 0 사용이 4코어를 넘으면 10분 대기 후 재검사한다. 여전히 바쁘면 exit 75로 후속 grid도 멈춘다. 합성 입력에서 기본값을 보존한 6설정의 이전 버전 대조, self-test, SLO 보간 및 요약 percentile 일치를 검증했다. 실행 시간·서버 시작/종료 기록은 `.codex-runs/10.last.md`에 있다.
