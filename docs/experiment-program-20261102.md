# 다음 실험 계획: 무엇을 보이려는가 (2026-11-02)

> 이 문서는 [실험 계획 v2](experiment-plan-tail-placement.md)로 대체되었다. E1~E5와 P 계열은 v2의 단계 A~C 안에 들어간다.

전제: bud05 `/Serverless`에 약 200 GB가 추가로 확보된다(지금 여유 59 GB → 약 260 GB). Docker sandbox 이미지 정리(약 180 GB)는 하지 않은 상태로 계획한다.

## 1. 지금까지 알게 된 것

- 현실 LLM 속도(×1.0)와 코어당 6.4개에서 CPU runqueue contention은 작고, step 시간을 거의 바꾸지 않는다([virtual-node-rl-20261026](results/virtual-node-rl-20261026/README.md), [sync-replay-design.md](sync-replay-design.md) 12절).
- 원인은 네 가지다([load-balancing-survey.md](load-balancing-survey.md)):
  - sandbox가 노드에 비해 작다(최대 1 vCPU = 노드의 1/15).
  - 평균 부하가 낮다.
  - group끼리 상관이 거의 없다.
  - step 시간이 LLM 대기에 묶여 있다.
- 운영 host도 CPU 사용률이 낮다. 우리는 메모리가 먼저 밀도를 정하기 때문이라고 본다.
- 다른 작업과 섞여 돌 때(DSec식 co-location)는 LLC와 메모리 대역폭 간섭이 크다. 이 간섭은 노드 안 QoS로 다 없어지지 않는다(microbenchmark에서 1.4~2.1배가 남음, [dsec-interference-design.md](dsec-interference-design.md)).

## 2. 보이려는 주장 (검증할 가설)

| 가설 | 내용 | 맞으면 CXL / hand-off에 주는 의미 |
|---|---|---|
| **H1 메모리가 먼저 막힌다** | 현실 LLM 속도에서 밀도를 올리면 CPU보다 메모리가 먼저 한계에 닿는다. CPU는 남는다(stranding). | CXL 메모리 pool의 동기. 메모리를 풀면 같은 host에 sandbox를 더 넣을 수 있다. |
| **H2 CPU는 메모리가 풀린 뒤에 막힌다** | 메모리 제약이 없다면 코어당 약 24개 근처에서 CPU p99가 용량에 닿는다. 그때 contention이 step 시간을 늘린다. | 메모리를 CXL로 풀어 밀도를 올린 host에서는 CPU 불균형(그리고 hand-off)이 2차 문제로 생긴다. |
| **H3 큰 노드는 burst를 흡수한다** | 같은 코어당 밀도라도 노드당 sandbox가 많으면 합계가 평탄해져 대기가 작다(√N). | CPU 불균형은 작은 노드나 노드를 나눈 구성에서 주로 생긴다. hand-off의 동기를 노드 크기와 함께 말해야 한다. |
| **H4 co-location 간섭은 노드 안에서 다 못 고친다** | SCHED_IDLE, core scheduling, RDT를 다 써도 LLC·대역폭 간섭으로 tool call이 느려진다. 같은 socket 안에서 옮겨도 줄지 않는다. | 다른 host로 옮길 동기가 된다(hand-off). 이득의 상한은 조용한 host와의 차이다. |

## 3. 실험

| ID | 무엇을 하나 | 무엇을 보이나 | 시간 | 추가 디스크 | 준비 |
|---|---|---|---|---|---|
| **E1** 코어 축소 밀도 sweep | 같은 192개(step당)를 물리 코어 30 / 16 / 12 / 8 / 6개에 올린다(코어당 6.4 → 32). ×1.0, SMT 켬, L3 way를 코어 비율로 줄인다. 원본 wave 간격을 쓴다. | **H2, H1(계산)**: CPU 대기와 step 시간이 밀도에 따라 어떻게 늘고, 어디서 runqueue 대기가 아니라 SMT 실행 지연으로 나타나는지. 각 수준의 최대 메모리를 GB/코어로 바꿔 서버 비율(bud05 4, DSec 7.8)과 비교한다. | 약 12.5시간 | 없음(VM 디스크 최대 약 40 GB) | `queue-density.sh` 준비됨 |
| **E2** 실제 밀도 증가 | 30코어 전부에 192 / 288 / 384개(코어당 6.4 / 9.6 / 12.8)를 올린다. 원본 step 5에서 중첩된 group(192 ⊂ 288 ⊂ 384). ×1.0, SMT 켬. cgroup 115 GB 상한, swap 없음. | **H1**: 모든 자원이 함께 늘 때 무엇이 먼저 막히는지 실제로 본다(메모리 PSI·OOM, 디스크, 생성 경로, CPU). **H3**: E1의 c16(노드당 96개, 코어당 12)과 E2의 384(노드당 192개, 코어당 12.8)를 비교해 노드 크기의 효과를 본다. | 약 5.5시간(image build와 solo 포함) | image 48개 약 125 GB + VM 디스크 최대 약 80 GB | `queue-density2.sh` 준비됨(계획 3개 생성, trace 변환 완료) |
| **E3** DSec식 co-location | segment 1, ×0.25. BE 없음 / mix 30%·50% × QoS(없음, core scheduling, RDT) / BE를 노드 0에만. | **H4(노드 안)**: QoS를 다 써도 남는 느려짐이 rollout과 step 시간에 얼마나 드러나는지. 노드 0에만 BE를 둘 때 노드 1이 보호되는지(같은 socket 안 이동의 상한). | 약 6시간(E1의 solo 재사용) | 없음 | `queue-dsec.sh` 준비됨 |
| **E4** 조용한 host 상한 (bud04) | bud04에서 SMT를 켜고 같은 segment를 BE 없이 돌린다. | **H4(노드 사이)**: tool call마다 (bud05 간섭 시간 − bud04 조용한 시간)이 다른 host로 옮겨 되찾을 수 있는 상한이다. | 약 3시간 | bud04에 image 약 125 GB(bud04의 예전 image 118 GB와 중단된 실험 27 GB를 지워야 함) | bud04 정리와 SMT 설정 필요 |
| **E5** CXL 메모리 (나중) | CXL을 online한 뒤 E2의 384 수준을 VM 메모리 일부를 CXL에 두고 다시 돌린다. BE 메모리를 CXL에 둔 E3 조건도 다시 돌린다. | **H1→H2 연결**: CXL로 메모리 한계를 풀면 밀도가 올라가고, 그때 CPU가 막히기 시작하는지. CXL이 대역폭 간섭을 덜어 주는지. | 미정 | 미정 | CXL online 필요(지금 node 2가 0 MB) |

### E1과 E2를 둘 다 하는 이유

| | E1 코어 축소 | E2 실제 증가 |
|---|---|---|
| 바꾸는 것 | 코어 수(sandbox 수는 같음) | sandbox 수(코어 수는 같음) |
| 장점 | 새 image가 필요 없다. 코어당 32개까지 갈 수 있다. CPU만 떼어 볼 수 있다. | 메모리, 디스크, 생성 경로가 실제로 늘어난다. 실제로 무엇이 먼저 막히는지 보인다. |
| 약점 | 메모리·디스크는 계산으로만 판단한다. 메모리 대역폭을 코어 비율로 줄이지 못한다. 노드가 작아 burst가 덜 평탄하다. | 코어당 12.8개까지만 간다(메모리 때문). image가 125 GB 더 필요하다. |

E1의 c16(코어당 12, 노드당 96개, 노드당 코어 8개)과 E2의 384(코어당 12.8, 노드당 192개, 노드당 코어 15개)는 밀도가 거의 같고 노드 크기만 다르다. 두 결과의 차이가 H3이다.

## 4. 예상과 판정

E1 수준별 예상(×1.0 검증 실행에서 계산)은 다음과 같다.

| 수준 | 코어당 | CPU 평균 사용률 | 1초 p99 수요 ÷ 코어 | 필요 메모리 |
|---|---|---|---|---|
| c30 | 6.4 | 7% | 0.28 | 2.2 GB/코어 |
| c16 | 12 | 12% | 0.52 | 4.1 GB/코어 |
| c12 | 16 | 17% | 0.69 | 5.4 GB/코어 |
| c8 | 24 | 25% | 1.04 | 8.1 GB/코어 |
| c6 | 32 | 33% | 1.38 | 10.8 GB/코어 |

E2의 384 수준은 살아 있는 VM당 host 메모리 약 0.47 GiB로 잡으면 약 135 GB가 필요하다. 115 GB 상한에 닿아 OOM이나 메모리 압력이 나타날 것으로 예상한다.

판정:
- **H1 맞음**: E2에서 384 수준의 메모리가 상한에 닿는다(PSI memory 증가, OOM, image page cache 회수). 같은 수준의 CPU 대기와 step 시간은 192와 거의 같다. 그리고 E1에서 메모리 필요량이 4 GB/코어를 넘는 c16에서도 CPU 대기가 작다.
- **H2 맞음**: E1의 c8~c6에서 대기/실행과 step 시간이 뚜렷이 는다(예: step 시간 5% 이상).
- **H2 틀림**: c6(코어당 32)에서도 step 시간이 거의 같다. 그러면 현실 LLM 속도에서 CPU 불균형은 메모리를 풀어도 문제가 되지 않는다. hand-off 주장은 co-location(H4)이나 빠른 추론, 여러 vCPU sandbox 조건으로 옮겨야 한다.
- **H3**: 같은 밀도에서 E2(노드가 큼)의 대기가 E1(노드가 작음)보다 작으면 맞다.
- **H4**: E3에서 RDT까지 쓴 조건의 tool call 느려짐이 10% 이상 남고, 노드 0에만 BE를 둘 때 노드 1의 느려짐도 크면 맞다(같은 socket으로는 못 피함). 그러면 E4로 다른 host의 상한을 잰다.

## 5. 순서와 디스크

E1과 E3은 새 디스크가 필요 없다. 그래서 200 GB를 확보하는 동안 먼저 돌릴 수 있다.

| 순서 | 실험 | 누적 시간 | bud05 디스크 여유 (200 GB 확보 가정) |
|---|---|---|---|
| 1 | E1 (`queue-e1-e3.sh`의 앞부분) | 약 12.5시간 | 실행 중 최저 약 220 GB (확보 전이면 약 20 GB) |
| 2 | E3 (같은 script, E1의 solo 재사용) | 약 19시간 | 실행 중 최저 약 220 GB |
| 3 | E2 (`queue-density2.sh`, 200 GB 확보 뒤) | 약 24.5시간 | image 뒤 약 135 GB, 384 수준 최저 약 55 GB |
| 4 | E4 (bud04) | 별도 | bud04 정리 필요 |
| 5 | E5 | CXL online 뒤 | |

- E2를 돌린 뒤 segment 1 image(123 GB)를 지우면 여유가 다시 늘어난다. E1·E3을 다시 돌릴 일이 없을 때 지운다.
- Docker sandbox 이미지(약 180 GB)를 정리하면 여유가 더 생긴다. 계획에는 넣지 않았다.

## 6. 바뀐 점 (이번 정리에서 고친 것)

- E1의 계획을 `plan-c2-1srv-v2-orig-x1.json`으로 바꿨다. 원래 대기열은 ×0.25용으로 줄인 wave 간격(168초)을 ×1.0에서 쓰고 있었다. 이제 원본 간격(약 14분)을 쓴다.
  - step 7과 9 사이의 기록된 idle은 3,524초다. 건너뛴 step 8이 그 사이에 있기 때문이다. 그래서 run 2의 중앙값 1,170초로 바꿨다.
- `rl_rollouts.py plan --candidates-out`을 더했다. E2 계획이 segment 1의 `candidates-plan.json`을 덮어쓰지 않고 `candidates-dens.json`을 쓴다.
- E3 대기열은 `SOLO`가 주어지면 그 solo를 쓴다.

## 7. 제안: 배치 × filesystem 시나리오 (P 계열, 미확정)

E1~E3은 같은 rollout과 같은 배치에서 자원 조건만 바꾼다. 배치 자체를 바꾸는 실험이 빠져 있다.

**이전 배치 실험(C2)이 filesystem 효과를 못 본 이유**
- 두 "노드"가 한 서버의 page cache를 같이 쓴다.
- 배치를 GRPO group 단위로 정해, least도 image 하나를 노드 하나에만 올렸다(least와 home 모두 두 노드에 동시에 있는 image가 0개였다). 그래서 least와 home의 차이는 CPU 쏠림뿐이었다.
- DSec의 power-of-k는 sandbox 단위로 배치해, 같은 image의 sandbox가 여러 노드에 흩어진다. 이 경우를 재지 않았다.

**구성**
- **노드**: NUMA 0을 노드 4개(코어 7개 + sibling)로 나눈다. 노드 2개보다 배치의 차이가 잘 드러난다.
- **노드별 image cache**:
  - 노드마다 image cache 디렉터리를 따로 둔다. 같은 image라도 노드마다 다른 파일이라 host page cache가 노드별로 갈린다.
  - 처음 쓰는 image는 "registry"(지금의 image 디렉터리)에서 노드 cache로 복사한다(pull). 복사 대역폭은 노드별로 제한한다(1 Gbps / 10 Gbps). 노드 cache 용량을 제한하고 LRU로 버린다.
  - pull을 기다리는 시간이 sandbox 준비 시간이 된다.
- **pull 방식**:
  - eager: image 전체를 받는다.
  - lazy: 실제로 읽는 block만 받는다. image별 working set 실측(`image_blocks.py`)으로 받을 양을 정한다. 이전 측정에서 sandbox가 실제로 읽는 base image는 p50 55 MB였다(image 크기 p50 2.2 GiB).
- **배치 정책**:

| 정책 | 단위 | 기준 | 비고 |
|---|---|---|---|
| S-p2c | sandbox | 무작위 두 노드 중 덜 바쁜 쪽 | DSec 방식 |
| S-p2c-img | sandbox | 두 노드 중 image를 가진 쪽 우선, 같으면 덜 바쁜 쪽 | locality 일부 반영 |
| G-least | group | 가장 덜 바쁜 노드 | 지금의 least |
| G-home | group | image별 고정 home | locality 최대 |
| G-home+H | group | G-home + 10 ms hand-off | 배치는 image로, CPU는 이동으로 |

**측정**
- 노드별 pull 횟수와 바이트, pull 대기
- sandbox 준비 시간과 첫 명령 지연
- 노드별 image page cache(cache 디렉터리별 상주량)
- 노드별 디스크(cache + writable)
- CPU 대기, rollout과 step 시간(pull 때문에 늦게 시작한 straggler 포함)

**보이려는 것**
1. **image locality와 부하 분산의 충돌을 실제 microVM으로 잰다.**
   - group·home 배치는 pull과 cache를 노드 수만큼 줄이지만 CPU를 쏠리게 한다.
   - sandbox 단위 p2c는 CPU를 고르게 하지만 image를 노드마다 받게 한다.
   - 대역폭이 좁으면 pull 대기가 step 시간을 늘린다(DSec의 8,192개 폭주, RollArt의 env.reset, MegaFlow의 1 → 13분과 같은 현상).
2. **hand-off가 둘을 분리하는지 본다.** image로 배치하고 CPU는 옮기는 방식(G-home+H)이 pull·cache와 CPU 대기를 함께 낮추는지 확인한다. simulator에서는 cache가 3~6배 작아지고 대기가 P1 수준이었다(results 12절). 이를 실측으로 확인하는 것이다.
3. **언제 locality가 중요한지 정한다.** 대역폭(1 / 10 Gbps)과 eager / lazy pull을 바꾼다. lazy이면 받는 양이 image의 몇 %라 locality의 값이 작아질 것으로 예상한다.

**추가로 붙일 수 있는 filesystem 축**
- base image를 virtio-blk로 둘 때와 virtio-pmem DAX로 둘 때. DAX는 guest 안의 사본 없이 host cache를 같이 쓰므로, home 배치와 함께 노드 메모리가 얼마나 줄어드는지 본다(이전 bud04 측정: DAX의 struct page 비용으로 오히려 19% 증가).
- writable layer의 위치(local / 공유). hand-off에서 writable layer를 어디 두느냐의 문제다. 나중에 한다.

**비용**
- 구현: 노드별 cache와 pull(대역폭·용량 제한), sandbox 단위 p2c와 image 우선 p2c, 노드별 cache 상주량 기록.
- 디스크: 노드 cache 4개 × 30 GB + writable 약 40 GB. 200 GB 확보 뒤 가능하다.
- 시간: 조건당 약 45분(×0.25, segment 1). 정책 5개 × 대역폭 2개 ≈ 8시간.

