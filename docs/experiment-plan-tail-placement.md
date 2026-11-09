# 실험 계획 v2: locality 배치의 tail contention과 CXL hand-off (2026-11-02)

> 2026-11-03: 8장 screen에서 M2는 sync와 async 모두 기준(5%)에 못 미쳤다. 다음 방향은 [GPU 노드 harvest 설계](gpu-node-harvest-design.md)다.

이 계획은 [experiment-program-20261102.md](experiment-program-20261102.md)를 대신한다. 그 문서의 E1~E5와 P 계열은 아래 단계 안에 들어간다.

## 0. 주장

> agentic RL sandbox는 **locality 배치**(같은 image·같은 GRPO group을 한 host에 모음)로 image pull, 생성 시간, host page cache를 크게 줄일 수 있다. 하지만 배치 단위가 group이 되어 거칠어지고, group의 CPU 수요는 배치 시점에 알 수 없다. 그래서 일부 host에 CPU가 몰리고, 그 host의 rollout이 step의 **tail**(straggler)이 된다. least-k 배치는 tail을 피하는 대신 locality를 잃는다. **CXL hand-off는 배치는 locality로 하고, 몰린 CPU만 ms 단위로 다른 host에 넘겨** 두 장점을 함께 얻는다.

평균 runqueue 대기는 주장에서 뺀다. 이미 작다고 확인했다.

## 1. 주장이 성립하려면 참이어야 하는 것

| # | 명제 | 지금 근거 | 판정 |
|---|---|---|---|
| M1 | locality 배치의 이득이 크다 (pull, 생성 시간, cache 메모리) | **외부:** DSec 8,192개 동시 생성에 60분 이상, 노드당 1.6 TB 쓰기. RollArt env.reset이 실패 iteration의 78%. MegaFlow 시작 1 → 13분. **simulator:** 고정 home 배치로 노드당 image cache 3~6배 감소. | **강함.** 다만 lazy loading(DSec는 image의 4~13%만 읽음)이면 줄어든다. 우리 실측은 없다(C2는 page cache 공유와 group 단위 배치 때문에 차이가 0이었다). |
| M2 | locality 배치는 CPU를 몰리게 하고, 그것이 step tail을 늘린다 | **우리:** group CPU 수요가 p10 76 ~ p90 801 CPU초(10배), CV 1.07, 상위 10% group이 37%. 배치 시점에 알 수 없는 무거운 꼬리다. home은 한 wave에 노드 하나에 80개, 다른 노드에 16개를 놓았다. **하지만** 지금까지의 비교(home과 least의 step 시간 차이 1% 이내, 노드 최대 CPU 1초 p99 14.3 대 14.0코어)는 사용률이 낮은 조건에서 나왔다(2절 참고). | **운영 수준 부하에서는 아직 검증 안 됨.** 지금 근거만으로 약하다고 볼 수 없다. |
| M3 | 다른 방법으로는 둘을 함께 얻기 어렵다 | least-k는 locality를 잃는다(정의상). 상한 있는 locality(넘치면 다른 host)는 배치 시점 정보만 쓴다. migration은 초 단위이고 목적지에서 image를 다시 받는다. 노드 안 우선순위는 코어 수를 늘리지 못한다. | **미검증.** 특히 "상한 있는 locality"와 "straggler 우선순위"가 강한 대안이다. |
| M4 | hand-off 비용이 이득보다 작다 | 10 ms hand-off 흉내는 있다. | **조건부.** 옮긴 뒤 목적지에서 base image를 읽으려면 image가 CXL에 공유되어 있거나 원격에서 읽어야 한다. CXL tax(이전 측정 trace 중앙값 +11%)는 옮긴 VM만 낸다. |

## 2. motivation이 되나 (판단)

**조건부로 된다.** 이득 쪽(M1)은 외부 근거가 강하다. 약한 고리는 비용 쪽(M2)이다. 그리고 M2를 지금까지 잰 조건 자체가 운영보다 가벼웠다.

**지금까지 조건이 가벼웠던 이유** ([load-balancing-survey.md](load-balancing-survey.md) 4.1절):

| 항목 | 지금까지 | 운영 / 비교 기준 |
|---|---|---|
| 코어당 sandbox | 6.4 (SMT 끔) | DSec container 최대 약 17, microVM 1/2 환산 약 8.5. scale unit 평균 12.7 |
| 사용률 | ×1.0에서 시간 평균 7%. step 꼬리와 idle이 포함되어 있다. 살아 있는 sandbox만 보면 13~19% | 밀도를 운영 수준으로 올리면 ×1.0에서도 20~30% 이상 |
| 메모리 | 명목 대비 3배 overcommit. NUMA 0이 아직 차지 않음 | bud05 비율(4 GB/코어)이면 코어당 8~10개에서 메모리가 먼저 참 |
| SMT | 끔 | DSec는 켬(384 thread) |
| 노드 수 | 2 | 운영은 수십~수백. 노드가 많을수록 노드당 group이 적어 home의 불균형이 커짐 |
| 대리 조건 | ×0.25, ×0.15로 LLM을 빠르게 해 부하를 올림 | 같은 평균 사용률이라도 sandbox 수가 적어 덜 평탄하다. 실제 고밀도와 같지 않다 |

그래서 M2는 **운영 수준 밀도에서** 다시 재야 한다. 메모리가 그 밀도를 막는다면(bud05 비율), 그 자체가 CXL 메모리 pool의 동기가 된다. 이 경우 주장은 다음 사슬이 된다.
- CXL 메모리 pool로 밀도를 운영 수준까지 올린다.
- 그 밀도에서 locality 배치의 CPU 쏠림이 tail이 된다.
- CXL hand-off가 그 tail을 없앤다.

메모리와 hand-off가 모두 CXL에서 나온다.

M2가 커질 수 있는 조건과 그 이유:
0. **밀도가 운영 수준일 때(코어당 8.5~17개, SMT 켬).** 위 표의 이유로 가장 먼저 확인한다.
1. **노드가 많고 노드당 group이 적을 때.** home 배치의 불균형은 무작위 burst가 아니라 "group을 통째로 던지는 balls-into-bins"다. 노드가 많아 노드당 group이 몇 개뿐이면 최대 노드는 평균의 2배를 쉽게 넘는다. √N 평탄화가 group 단위에서는 잘 안 된다.
2. **사용률이 높을 때.** CXL 메모리 pool로 밀도를 올리거나, 추론이 빨라질 때다.
3. **group 안에서 CPU가 동시에 몰리는 단계가 있을 때.** 끝의 검증(reward test), 시작의 setup이 그렇다. group을 한 host에 모으면 이 상관이 한 host에 집중된다.
4. **sandbox에 vCPU가 여러 개일 때.** burst가 넓어진다.

이 조건들에서도 home의 step 시간이 least-k보다 뚜렷이 길지 않으면(예: 5% 미만), 이 방향은 약하다. 그때는 메모리 용량(CXL pool로 밀도 증가)이나 co-location 간섭 쪽으로 주장을 옮긴다.

**reviewer가 바로 물을 대안**
- 상한 있는 consistent hashing(노드가 차면 다음 노드로)
- straggler에 CPU 우선순위 주기
- partial rollout / 비동기 RL(Kimi): tail을 기다리지 않는다
- lazy image loading: locality의 이득 자체가 작아진다

계획에 이 대안을 모두 baseline으로 넣는다.

## 3. 지표

| 측면 | 지표 |
|---|---|
| tail (비용) | step 시간(가장 늦은 rollout). step 느려짐(단독 기준). **step을 정한 rollout들(늦게 끝난 상위 5%)의 느려짐과 그 원인 분해**(runqueue, 실행 시간 증가, 그 밖). tool call 느려짐 p99. 노드별 1초 CPU p99와 포화 시간 비율. |
| locality (이득) | 노드별 pull 횟수·바이트, pull 대기, sandbox 준비 시간과 첫 명령 지연, 노드별 image page cache(GB), 노드별 디스크 |
| 이동 비용 | 이동 횟수, 이동 중 멈춘 시간, 목적지에서 새로 읽은 image 바이트 |

## 4. 단계와 관문

### 단계 A: motivation (bud05, 노드 4개 흉내)

공통 구성:
- NUMA 0을 노드 4개(코어 7개 + sibling)로 나눈다. SMT 켬.
- 노드마다 image cache 디렉터리를 따로 둬서 노드별 page cache를 만든다.
- pull 흉내: "registry"에서 노드 cache로 복사한다. 노드별 대역폭과 cache 용량(LRU)을 제한한다.
- workload: segment 1(원본 step 7, 9). step당 192개라 코어 28개면 코어당 6.9개뿐이다. 그래서 노드의 코어 수를 줄여 밀도를 올린다(E1과 같은 방식, L3 way도 코어 비율로). 실제로 sandbox를 늘리는 E2 계획(step당 384개)도 쓴다.
- 밀도 수준: 코어당 약 7(지금 수준) / 8.5(DSec microVM 환산) / 12(scale unit 평균 근처) / 17(DSec container 최대). 노드 4개 × 코어 7 / 6 / 4 / 3에 해당한다.
- 수준마다 cgroup 메모리 최대치를 재서, 그 밀도가 bud05 비율(4 GB/코어)과 DSec 비율(7.8 GB/코어)로 가능한지 같이 적는다.

| ID | 조건 | 보이는 것 | 시간 |
|---|---|---|---|
| **A1 이득** | 배치 {least-k(sandbox 단위 p2c, DSec), image 우선 p2c, group least, group home, 상한 있는 home} × image 저장 {eager 1 Gbps, eager 10 Gbps, lazy(working set만)}. 코어당 8.5, ×0.25 | M1: 배치별 pull·준비 시간·cache. locality의 이득이 image 저장 방식에 따라 얼마나 변하나 | 배치 5개 × 저장 2~3개 × 45분 ≈ 8~11시간 |
| **A2 비용** | 배치 {least-k, group home, 상한 있는 home} × **밀도 {코어당 7, 8.5, 12, 17}, ×1.0(현실 LLM 속도)**. 그 위에 코어당 8.5에서 {검증 단계 켬, 2 vCPU, ×0.25}를 추가한다. 저장은 A1에서 차이가 가장 컸던 하나 | M2: 운영 수준 밀도에서 locality 배치가 tail을 얼마나 늘리나. 그 밀도에서 메모리가 먼저 차는지 | 배치 3개 × (밀도 4개 + 추가 3개) × 약 1시간(×1.0) ≈ 20시간 |

**관문 G1** (A1·A2 뒤):
- 계속: 운영 수준 밀도(코어당 8.5~17) 중 어떤 조건에서 home이 least-k보다 pull·준비 시간·cache를 2배 이상 줄이고, 동시에 step 시간을 5% 이상 늘린다. 그 밀도가 메모리 비율 때문에 불가능하다면, CXL 메모리를 전제로 한 결과로 적는다.
- 다시 생각: 이득이 작거나(lazy이면 차이가 거의 없음), 비용이 모든 조건에서 2% 미만이다.

### 단계 B: 해결 (bud05)

A2에서 비용이 가장 컸던 조건 두 개에서 비교한다.

| 방법 | 배치 | 사후 처리 | 비고 |
|---|---|---|---|
| least-k | p2c | 없음 | locality 포기 |
| 상한 있는 home | home, 넘치면 다음 노드 | 없음 | 배치 시점 정보만 사용 |
| home + straggler 우선순위 | home | wave의 90%가 끝난 뒤 남은 rollout의 우선순위를 올림 | 노드 안 해결 |
| home + migration | home | pre-copy 흉내(GiB당 0.7초) + 목적지 image pull | 기존 migration |
| **home + hand-off** | home | 10 ms hand-off, 목적지는 CXL 공유 image를 읽는다고 가정 | 제안 |
| POOL | – | 모든 코어를 하나로 | 이상적 상한 |

**관문 G2**:
- 성공: home + hand-off의 step 시간이 least-k와 2% 이내이고, pull·cache는 home의 1.2배 이내다. 그리고 다른 대안보다 둘 중 적어도 하나에서 낫다.

시간은 방법 6개 × 조건 2개 × 45분 ≈ 9시간이다.

### 단계 C: 일반화와 검증

| ID | 내용 | 보이는 것 |
|---|---|---|
| C1 두 host | bud04 + bud05에서 노드 = 실제 host. pull은 실제 network(대역폭 제한). | 흉내(한 서버의 노드 분할)와 실제 host의 차이. 특히 page cache, LLC, 메모리 대역폭이 실제로 갈렸을 때 |
| C2 밀도 | 코어 축소(E1)와 실제 sandbox 증가(E2) | CXL로 메모리 한계를 풀어 밀도가 오를 때 M2가 얼마나 커지나 |
| C3 co-location | BE 부하(E3) | 다른 원인으로 생긴 host hot spot도 hand-off로 피하나 |

### 단계 D: 규모 (simulator)

`fleet.py`를 A·B 결과로 다시 보정해 노드 16~64대, 노드당 192코어, DSec 밀도에서 돌린다. 산출물은 다음과 같다.
- 배치별 step 시간과 cache·pull
- hand-off 회수율
- 같은 처리량에 필요한 노드 수

### 단계 E: 실제 CXL hand-off

비일관 CXL multi-host 장비가 필요하다(HANDOFF Phase D). 이 계획의 범위 밖이다.

## 5. 구현할 것

| 항목 | 상태 |
|---|---|
| 노드 4개 구성(`--nodes` 4개) | 있음 |
| 노드별 image cache와 pull 흉내(eager / lazy, 대역폭, 용량, LRU) | **새로** |
| sandbox 단위 p2c, image 우선 p2c, 상한 있는 home | **새로** (지금 계획 모드는 group 단위만 지원) |
| 검증 단계 흉내: rollout 끝에 그 trace의 가장 무거운 test 명령을 다시 실행 | **새로** |
| straggler 우선순위 baseline(renice) | **새로** |
| migration 흉내에 목적지 image pull 더하기 | **새로** (pre-copy 흉내는 있음) |
| step tail 분석(늦게 끝난 상위 rollout의 느려짐 분해) | **새로** (`sync_steps.py` 확장) |
| hand-off 흉내(10 ms 멈춤, affinity 변경), POOL, BE 부하, host·cgroup·LLC 기록 | 있음 |

## 6. 순서, 시간, 디스크

| 순서 | 내용 | 시간 | 디스크 |
|---|---|---|---|
| 1 | 구현과 smoke | 1~2일 | – |
| 2 | A1 → A2 | 약 30시간 | 노드 cache 4 × 30 GB + writable 약 40 GB = 약 160 GB. bud05 여유 298 GB(2026-11-02 확인)라 가능하다. E2(약 205 GB)와는 동시에 못 하므로, A가 끝나면 노드 cache를 지운다 |
| 3 | 관문 G1 판단 | – | – |
| 4 | B | 약 9시간 | 같음 |
| 5 | C1 (bud04 정리, SMT 설정 필요), C2, C3 | 각 6~12시간 | C1은 bud04에 약 125 GB |
| 6 | D | 하루 | – |

구현하는 동안 새 디스크가 필요 없는 E1(코어 축소, least 배치만)을 먼저 돌려 둘 수 있다. E1은 배치 비교 없이 "밀도를 운영 수준으로 올리면 사용률과 tail이 어떻게 변하나"를 먼저 보여 준다. A2의 밀도 수준을 정하는 근거가 된다.

## 7. 가정과 한계

- hand-off 뒤 목적지가 base image를 읽는 방식: 흉내에서는 source 노드의 cache 파일을 계속 읽는다. 이는 image가 CXL에 공유되어 있다는 가정이다. 이 가정을 설계의 일부로 명시한다. migration baseline은 목적지에서 image를 다시 받게 해서 공정하게 비교한다.
- 한 서버를 노드 4개로 나누면 LLC와 메모리 대역폭은 노드 사이에 공유된다. 노드별 page cache만 갈린다. C1(두 host)로 보완한다.
- lazy pull은 working set 실측 바이트로 대기 시간만 흉내 낸다. block 단위로 실제로 받아 오지는 않는다.
- 검증 단계는 trace 안의 test 명령을 다시 실행하는 흉내다. 실제 reward 계산(정답 test)과 다를 수 있다.

## 8. 빠른 screen (2026-11-02)

전체 계획(구현 1~2일 + 단계 A·B 약 40시간 + C)을 바로 하지 않는다. 가설에 유리한 조건에서 먼저 짧게 본다.
- 거기서도 안 보이면 빨리 접는다.
- 보이면 그 조건만 ×1.0, 반복, 실제 pull로 길게 검증한다.

| 항목 | 값 | 이유 |
|---|---|---|
| 노드 | 4개 × 코어 4 + sibling (`2-5,66-69 / 6-9,70-73 / 17-20,81-84 / 21-24,85-88`), L3 8 way | 노드가 많을수록 group 단위 배치의 쏠림이 크다 |
| 밀도 | step당 192개 / 16코어 = 코어당 12 | DSec scale unit 평균 근처 |
| LLM | ×0.25 | 사용률이 40~50%가 되어 M2가 드러나기 쉬운 조건이다(실제 고밀도보다 burst가 거칠다) |
| workload | segment 1(orig step 7, 9), 2 step, 같은 seed | – |
| 배치 | p2c(sandbox 단위, DSec least-k) / group least / group least + 10 ms hand-off / group home | group least는 배치 시점에 locality와 개수 균형을 함께 맞춘다. 가장 강한 "배치만으로 해결" 대안이다 |
| locality | pull 흉내 없이, 실제 배치에서 계산 | (노드, image) 쌍 = pull 수. eager 바이트(rootfs 크기), lazy 바이트(실제 base 읽기). 노드별 직렬 pull 시간(1 / 10 Gbit/s) |
| 분석 | `placement_screen.py` (p2c를 기준으로) | step 시간 비, 기준 run에서 마지막 5%로 끝난 rollout의 종료 시각 비, 노드별 대기/실행, 노드별 1초 수요 p99, 이동 수 |
| 시간 | 정책당 약 40분, 전체 약 2.7시간 | – |

**group 배치일 때 pull 규모** (이전 2노드 실행에서 계산):
- step당 image 24개, eager 52~62 GiB.
- 노드별 직렬 pull 시간은 1 Gbit/s에서 4~5.5분이다. 원본 env 생성 시간 p90(약 4.5분)과 비슷하다. 10 Gbit/s에서는 약 30초, lazy이면 약 20초다.
- p2c는 image 하나를 노드 약 4개가 각각 받는다(smoke: 24개 image에 95쌍). 그래서 eager 1 Gbit/s에서는 노드당 pull이 4배가 된다.

**판단 기준**
- **M2가 보임**: group least(또는 home)의 step 시간이나 마지막 5% rollout의 종료가 p2c보다 5% 이상 늦고, hand-off가 그 차이를 대부분 없앤다. 이 경우 ×1.0과 실제 pull로 길게 검증한다.
- **M2가 안 보임**: 모든 group 배치가 p2c와 2% 이내다. 이 경우 검증 단계나 2 vCPU 조건을 한 번씩만 더 보고 나서 접을지 정한다.

### 8.1 결과: M2가 보이지 않는다

2026-11-02 08:21~11:06 UTC, bud05. 실행 4개 모두 step당 192개, 384개 sandbox가 끝까지 돌았다. 불일치(388~494)와 timeout(24~29)은 정책 사이에 비슷하다. 숫자는 [placement-screen-20261102.json](results/virtual-node-rl-20261026/measured/placement-screen-20261102.json)이다.

| 배치 | step 2 시간 | step 3 시간 | 마지막 5% 종료 (s2 / s3) | 노드 대기/실행 최대 (s2 / s3) | 이동 | pull 쌍 (s2) | 노드 최대 eager pull 1 / 10 Gbit/s | lazy 1 Gbit/s |
|---|---|---|---|---|---|---|---|---|
| p2c (기준) | 934 s | 1,280 s | 1 / 1 | 3.2% / 2.8% | 0 | 93 | 532 / 53 s | 26 s |
| group least | ×0.992 | ×0.991 | ×0.998 / ×1.002 | 9.0% / 4.1% | 0 | 24 | 170 / 17 s | 14 s |
| group least + hand-off | ×0.998 | ×0.990 | ×0.996 / ×1.000 | 2.8% / 2.2% | 607 | 24 | 170 / 17 s | 14 s |
| group home | ×0.984 | ×0.992 | ×0.987 / ×1.000 | 19.1% / 4.5% | 0 | 24 | 216 / 22 s | 15 s |

- **step 시간 차이는 1.6% 이내다.** group 배치가 오히려 조금 빠르지만 noise 수준이다. 판단 기준의 "안 보임"에 해당한다.
- **노드 쏠림 자체는 생겼다.**
  - home step 2에서 node 2에 72개(평균 48)가 모였다. 그 노드의 대기/실행은 19%, 그 노드 sandbox의 명령 시간 합은 p2c보다 12% 길었다.
  - group least도 한 노드의 명령 시간이 10% 길었다.
  - hand-off는 노드 대기를 고르게 했다(최대 9.0% → 2.8%, 이동 607회). 그러나 step 시간에는 영향이 없었다.
- **locality 이득(M1)은 크다.** step당 pull 쌍이 93개에서 24개(3.9배 적음), eager 바이트가 239 GiB에서 62 GiB가 된다. 노드 하나의 직렬 pull 시간은 1 Gbit/s에서 532초 → 170초, 10 Gbit/s에서 53초 → 17초, lazy에서 26초 → 14초다.

### 8.2 원인 사슬의 어디서 끊기나

가설은 다음과 같다. GRPO는 group의 rollout이 모두 끝나야 학습에 쓴다. 그런데 group 안에서 turn이 많아 가장 늦게 끝나는 rollout(tail)이 같은 노드의 co-running sandbox 때문에 더 느려지면, group 완료와 학습 전체가 늦어진다. 이를 두 단계로 나눠 본다.

**(가) co-runner가 sandbox를 느리게 하는가: 그렇다.** rollout의 명령 시간 합을 p2c의 같은 rollout과 비교했다. 명령이 돌 때 같은 노드에 있던 co-runner 수(명령 시간 가중)로 나누면 다음과 같다.

| 배치 | co-runner 0~29 | 30~44 | 45~59 | 60 이상 |
|---|---|---|---|---|
| group least | 중앙값 ×1.015, p90 ×1.11 (116개) | ×1.020, ×1.20 (209개) | ×0.985, ×1.09 (9개) | – |
| group home | ×0.963, ×1.08 (120개) | ×1.044, ×1.20 (130개) | ×1.086, ×1.29 (62개) | ×1.126, ×1.44 (22개) |

**(나) tail rollout이 co-runner 때문에 느려져 group 완료가 늦어지는가: tail은 조금 느려지지만 group 완료에는 거의 나타나지 않는다.** group 48개(step 2개 × 24)마다 p2c에서 가장 늦게 끝난 member를 tail로 잡았다.

| 배치 | tail의 명령 시간 (p2c 대비, 중앙값 / p90 / 최대) | group 완료 시각 (p2c 대비, 중앙값 / p90 / 최대) | 5% 이상 늦은 group |
|---|---|---|---|
| group least | ×1.028 / ×1.10 / ×1.83 | ×1.001 / ×1.010 / ×1.045 | 0 |
| group least + hand-off | ×1.011 / ×1.16 / ×2.02 | ×1.000 / ×1.007 / ×1.027 | 0 |
| group home | ×1.012 / ×1.18 / ×2.32 | ×1.000 / ×1.010 / ×1.043 | 0 |

- tail은 실제로 turn이 많은 rollout이다. turn 수가 group 중앙값의 1.34배(중앙값), 1.75배(p90)다.
- tail의 명령 시간 비중은 ×0.25에서 중앙값 6%, p90 29%, 최대 47%다. 그래서 명령이 10~18% 느려져도 group 완료는 1% 안팎만 늦다. 명령 시간이 2배 가까이 된 tail은 명령이 11~22초뿐인 rollout이다(늘어난 시간 11~18초). 명령이 긴 tail에서 가장 크게 늘어난 경우는 282초 → 314초(+11%)였다.

그 이유를 나누면 다음과 같다.
1. **tail rollout에도 co-runner는 있었다.** step을 정한 상위 5개 rollout의 같은 노드 co-runner 수는 다음과 같다.
   - 생애 앞 절반: 18~61개
   - 생애 전체 평균: 11~38개
   - 마지막 10%: 0~7개

   노드가 비는 것은 step 끝 무렵뿐이다. step 시간 비율별 살아 있는 rollout 수(192개 중)는 다음과 같다.

   | step 시간 | 25% | 50% | 75% | 90% | 95% |
   |---|---|---|---|---|---|
   | 살아 있는 rollout | 131~168 | 111~140 | 24~49 | 3~10 | 2~5 |
   | 노드당 최대 | 34~49 | 32~63 | 8~23 | 1~4 | 1~2 |
2. **tail rollout은 LLM 대기 때문에 길다.** 명령 실행 비중은 ×0.25에서 rollout 전체 중앙값 7%, p90 26%다. step 2의 tail(jodal, numpydoc, werkzeug)은 0.3~13%다. 명령이 12% 느려져도 이런 rollout은 최대 1.6% 늦다. ×1.0이면 비중이 약 1/4로 준다.
3. **무거운 tail rollout은 무거운 명령이 생애 후반에 몰린다.** step 3의 tail은 무거운 group 하나다(spanner-317). 명령 실행이 약 350초로 rollout의 36%다.
   - 이 group 명령의 co-runner 수(가중)는 7~20개였다. 생애 평균(18~33개)보다 적다. 노드가 비어 가는 시점에 명령이 돈다.
   - 그래서 8개가 한 노드에 모여도 명령 시간은 p2c와 같았다(347~350초 대 359초).
4. step을 정하는 rollout은 기록된 env 생성 지연 때문에 step 시작 후 270~330초에 시작한다. 이 지연은 replay에서 고정값이다. 그래서 배치가 pull 시간을 바꾸는 효과(M1)는 계산으로만 반영된다.

**설계의 한계.** step 끝에 노드가 비는 것은 job 하나를 전용 노드에서 synchronous로 돌린 구성 때문이다. 실제로는 그렇지 않을 수 있다.
- 공유 fleet(DSec)에서는 여러 job과 agent가 같은 노드를 쓴다.
- 비동기 RL은 끝난 자리를 바로 다음 rollout으로 채운다.

두 경우 모두 tail rollout의 후반에도 co-runner가 남는다. 따라서 이번 결과는 "sync, 단일 job" 조건에만 해당한다.

### 8.3 판단

- **메커니즘은 확인됐다.** locality 배치는 노드 쏠림을 만든다(대기/실행 최대 9~19%, 쏠린 노드의 명령 +10~13%). hand-off는 그 쏠림을 없앤다(최대 2.8%, 이동 607회).
- **sync·단일 job에서는 쏠림의 비용이 step 시간에 나타나지 않았다.** 쏠림은 step 중반에 있고, 그때 끝나는 rollout은 step 시간을 정하지 않는다. 보완할 비용이 측정되지 않았으므로, hand-off의 이득도 이 조건에서는 측정되지 않는다. "hand-off로 보완한다"는 연결이 틀렸다는 뜻은 아니다. 이 조건에서는 그 연결을 보일 수 없다는 뜻이다.
- **다음은 노드가 비지 않는 조건이다(8.4).** 이때 쏠림은 모든 rollout의 지연과 처리량에 들어간다. 지표는 step 시간 대신 group 지연(staleness, slot 점유)과 처리량이다.
- 검증 단계, 2 vCPU 조건은 async 결과를 본 뒤 정한다.

### 8.4 async screen (2026-11-03)

| 항목 | 값 |
|---|---|
| 노드, 밀도, L3, LLM | 8.1과 같다(4노드 × 4코어 + SMT, 코어당 12개, 8 way, ×0.25) |
| 실행 방식 | sync plan 없이 group slot 24개(8개씩)가 독립적으로 돈다. slot의 다음 group은 가장 늦은 member가 끝나면 시작한다. slot은 192개지만 먼저 끝난 member의 자리는 group이 끝날 때까지 빈다. 그래서 살아 있는 sandbox는 평균 약 140개(노드당 약 35개)다 |
| task | image가 있는 51개 group에서 slot마다 무작위로 고른다. seed가 같아 모든 정책에서 slot별 task 순서가 같다 |
| 배치 | p2c(sandbox 단위) / group least / group home(image 고정, 라운드 사이 cache 재사용) / home + 10 ms hand-off |
| 시간 | 정책당 ramp 300초 + 3,300초, 전체 약 4.2시간 |
| 분석 | `placement_screen.py`(async run은 자동 판별). (slot, 라운드)로 짝지은 group·rollout 지연 비, 기준 run에서 가장 느린 10% group, 시간당 완료 rollout, 노드 대기/실행, co-runner 수별 명령 시간, 노드 cache 무제한일 때 pull 수 |

판단 기준:
- **M2가 보인다**: home(또는 least)의 가장 느린 10% group 지연이나 처리량이 p2c보다 5% 이상 나쁘고, hand-off가 그 차이를 대부분 없앤다.
- **안 보인다**: 모든 차이가 2% 이내다.
- 예상: 쏠린 노드의 명령 +10~13%에 명령 비중(중앙값 7%, p90 26%)을 곱하면 평균 rollout은 1~3% 늦어진다. 명령 비중이 큰 group(p90)이 쏠린 노드에 걸리면 수 % 이상이 될 수 있다.

**결과.** 2026-11-03 01:18~05:18 UTC, bud05. 실행 4개 모두 sandbox 오류가 없다. 불일치(1,753~1,933)와 timeout(165~195)은 정책 사이에 비슷하고, home이 가장 많다. 숫자는 [placement-screen-async-20261103.json](results/virtual-node-rl-20261026/measured/placement-screen-async-20261103.json)이다. 기준은 p2c이고, (slot, 라운드)로 짝지은 group 134~136개를 비교했다. 처리량은 ramp 뒤 3,300초 동안 끝난 rollout 수다.

| 배치 | 완료 group | 시간당 rollout | group 지연 (중앙값 / p90 / 최대) | 가장 느린 10% group | 5% 이상 늦은 group | 노드 대기/실행 최대 | 이동 | pull | 노드 최대 eager pull 1 / 10 Gbit/s |
|---|---|---|---|---|---|---|---|---|---|
| p2c (기준) | 136 | 1,223 | 1 | 1 | – | 7.4% | 0 | 184 | 977 / 98 s |
| group least | 136 | 1,223 (0%) | ×1.000 / ×1.017 / ×1.070 | ×0.999 | 2 | 15.4% | 0 | 104 | 577 / 58 s |
| group home | 134 | 1,209 (−1.2%) | ×1.004 / ×1.055 / ×1.119 | ×1.019 | 17 | 41.7% | 0 | 47 | 306 / 31 s |
| home + hand-off | 136 | 1,218 (−0.4%) | ×1.000 / ×1.015 / ×1.078 | ×0.994 | 3 | 5.8% | 4,425 | 47 | 306 / 31 s |

pull은 노드 cache가 무제한일 때 (노드, image) 쌍의 수다. hand-off한 sandbox는 시작 노드에서만 센다(rootfs가 CXL 상주 상태와 함께 간다고 본다). 명령 시간을 p2c의 같은 rollout과 비교하고, 명령이 돌 때 같은 노드에 있던 co-runner 수(명령 시간 가중, 이동 반영)로 나누면 다음과 같다.

| 배치 | co-runner 0~14 | 15~29 | 30~44 | 45~59 | 60 이상 |
|---|---|---|---|---|---|
| group least | – | ×1.006, p90 ×1.46 (15개) | ×0.999, ×1.21 (902개) | ×1.36 (1개) | – |
| group home | ×0.845, ×0.99 (37개) | ×0.983, ×1.34 (87개) | ×1.000, ×1.18 (283개) | ×1.123, ×1.48 (385개) | ×1.435, ×2.16 (110개) |
| home + hand-off | – | ×0.991, ×1.17 (112개) | ×1.000, ×1.14 (769개) | ×1.020, ×1.20 (37개) | – |

- **판단 기준의 "안 보임"이다.** 가장 느린 10% group과 처리량의 차이가 모두 2% 이내다(home +1.9%, −1.2%).
- **쏠림은 sync보다 훨씬 크다.** home은 image마다 노드 하나에 고정한다. node 2에는 평균 48개, 최대 84개가 모였다(p2c는 최대 46개). 그 노드의 대기/실행은 42%였다. co-runner가 60개 이상인 rollout은 명령 시간 합이 중앙값 +44%, p90 +116% 늘었다. sync home(8.2)은 +13%, +44%였다.
- **그래도 group 지연에는 작게 나타난다.** 134개 중 17개 group이 5% 이상, 2개가 10% 이상 늦었고, 최대는 +12%다. group 지연 대부분이 LLM 대기이기 때문이다(8.5). home은 p2c보다 timeout이 30개 많다. timeout 난 명령은 상한에서 잘리므로, 가장 느린 명령의 증가는 실제보다 작게 잡혔을 수 있다.
- **hand-off는 쏠림을 없애면서 locality를 유지한다.** 이동 4,425회(약 1.2회/초) 뒤 노드 대기/실행 최대가 5.8%로, p2c(7.4%)보다도 낮다. 명령 중 평균 co-runner가 60개 이상인 rollout은 없었다. pull은 home과 같은 47개(image당 1번)다. p2c의 184개(eager 451 GiB)보다 3.9배 적다(115 GiB).

**판단**
- **M2(쏠림이 group 완료와 처리량을 늦춘다)는 노드가 비지 않는 async에서도 기준(5%)에 못 미친다.** 명령이 2배 가까이 느려져도 LLM 대기가 지배해서 group은 2% 안팎만 늦다. 8.5의 critical path 분석과 같은 결론이다. M2만을 위한 긴 검증 run은 근거가 약하다.
- **남는 차이는 M1(locality)이다.** pull이 3.9배 적고, 노드 하나의 직렬 eager pull이 1 Gbit/s에서 977초 → 306초로 준다. replay에서는 env 생성 지연을 기록값으로 고정하므로, 이 차이가 지연에 들어가지 않는다. 8.5에서 env 생성은 sandbox 쪽에서 가장 민감한 항목이었다(×0.25에서 2배가 되면 group +12.7%).
- **hand-off의 역할은 CPU contention의 직접 비용을 줄이는 것이 아니다.** locality 배치(home)를 쓰면서 그 부작용(쏠림)을 없애는 것이다. 이득의 크기는 쏠림 비용이 아니라 pull 비용이 정한다. 이 조건에서 쏠림 비용은 group 지연 1~2%다.

### 8.5 원본 job의 critical path (trace 분석, 2026-11-03)

원본 RL job(sweagent/iter2-rl-rollouts, rollout 23,139개, step 53개)의 timestamp로 rollout 시간을 나눴다. 스크립트는 `experiments/contention/critical_path.py`다.
- **env 생성**: `env_creation_time`
- **LLM**: tool 결과 → 다음 assistant 응답 간격의 합. 학습 엔진의 실제 속도(약 34 token/s/요청)다.
- **tool+agent**: 나머지. 명령 실행, agent 쪽 처리, 첫 LLM 호출 하나가 섞여 있어 실제 명령 시간보다 조금 크다.

**rollout 전체**: LLM 87.7%, env 생성 6.2%, tool+agent 6.1%. rollout 하나 기준 tool+agent 비중은 중앙값 5.3%, p90 12%다.

**critical path**: 단위마다 가장 늦게 끝난 rollout의 구성을 단위 길이(첫 launch → 마지막 끝)에 대한 비율로 본다. 모든 rollout의 한 요소를 늘렸을 때 단위 길이가 얼마나 늘어나는지도 계산했다.

| 단위 | LLM 배율 | 늦은 launch | env 생성 | LLM | tool+agent | tool 전부 ×1.2 | tool 전부 ×2 | env 생성 전부 ×2 |
|---|---|---|---|---|---|---|---|---|
| step(53개) | ×1 | 21.6% | 5.5% | 66.7% | 5.0% (p90 15.1%) | +1.0% (p90 +3.2%) | +6.9% (+22.2%) | +7.3% (+11.4%) |
| group 8개(2,065개) | ×1 | 0% | 3.7% | 89.6% | 4.6% (p90 10.5%) | +0.9% (+2.1%) | +4.8% (+11.1%) | +3.9% (+14.7%) |
| group 8개 | ×0.25 | 0% | 12.4% | 62.9% | 15.2% (p90 33.3%) | +3.1% (+6.7%) | +16.3% (+34.0%) | +12.7% (+40.8%) |
| step 첫 wave | ×0.25 | 0% | 8.4% | 59.1% | 26.9% (p90 53.4%) | +5.9% (+10.7%) | +29.9% (+57.4%) | +10.7% (+35.0%) |

비율은 중앙값이다. step은 대부분 wave가 둘이다(53개 중 50개). 53개 중 49개 step에서 마지막 rollout이 마지막 wave에 있었다. 그래서 step에는 "늦은 launch"(두 번째 wave가 늦게 시작한 시간)가 22% 들어간다. 이 지연은 첫 wave의 진행에 묶여 있어서 LLM 배율로 단순히 줄일 수 없다. 그래서 step은 ×1만 적었다.

**해석**
- **sandbox는 critical path 위에 있지만 얇다.** 가장 늦은 rollout도 LLM 호출과 명령을 번갈아 실행하므로, 명령 시간은 그대로 critical path에 들어간다. 다만 학습 엔진 속도에서는 그 몫이 5% 안팎이다. co-runner로 명령이 20% 느려져도 group과 step은 약 1% 늦어진다. 8.1~8.2의 측정과 같은 크기다.
- **critical path의 tail은 turn이 많은 rollout이다.** 가장 늦은 rollout의 turn 수는 단위 중앙값의 1.19배(group), 1.47배(step)다.
- **sandbox 쪽에서 더 민감한 것은 env 생성이다.** env 생성은 모든 rollout의 시작에 몰려 있고 p90이 277초다. ×1에서도 2배가 되면 step이 7% 늦어진다. 추론이 4배 빠르면(×0.25) group이 13%(p90 41%) 늦어진다. image pull과 locality(M1)가 바로 이 항목이다. **[11-09]** 기록된 env 생성 시간은 image pull이 아니라 provider의 생성 대기열이 정했다. env 생성을 모두 1초로 줄여도 step은 약 3% 준다([trace 성격 다시 보기](results/trace-character-20261109/README.md) 3절).
- **추론이 빨라지면 sandbox 몫이 커진다.** ×0.25에서 tool+agent가 group critical path의 15%(p90 33%), 첫 wave의 27%다. 명령이 20% 느려지면 3~6%(p90 7~11%) 늦어진다. CPU contention이 학습 시간에 보일 수 있는 조건은 추론이 빠르고 tool이 무거운 경우다.
