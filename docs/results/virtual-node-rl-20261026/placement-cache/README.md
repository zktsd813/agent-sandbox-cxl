# 배치 정책 대 image page cache (simulator v2, 2026-10-28)

질문: 3FS 같은 원격 storage는 그대로 두고 배치만 바꾸면, 노드별 host page cache(image)와 CPU 대기를 둘 다 줄일 수 있나. hand-off가 locality 우선 배치의 CPU 쏠림을 지워 주나.

## 방법

- `experiments/contention/fleet.py`를 실측에 맞춘 보정값(`cpu_inflation` 1.4, kernel wait floor)으로 돌렸다.
  - 입력: 단독 실행 rollout 420개, 실제 GRPO group(8개).
  - 노드: 15코어. 노드당 sandbox 96개(코어당 6.4). 노드 8대에서는 64/128/160개로 밀도를 바꿨다.
  - LLM 대기: token 모델에 배율 ×1.0/0.25/0.15를 곱했다.
  - 비동기는 group마다 독립이고, 동기는 cluster 전체를 한 step으로 묶었다.
  - 측정 3시간, seed 2개.
- 정책
  - P1: 무작위 두 노드 중 덜 바쁜 쪽(DSec의 power-of-k)
  - P2: locality 우선. 그 image를 가진 노드 중 가장 덜 바쁜 곳, 없으면 P1
  - P3: P1 점수에서 image를 가진 노드에 sandbox 5개 또는 20개만큼 가산점
  - P4: P2 + hand-off(유동 locality)
  - P5: image별 고정 home(image 번호 mod N)
  - +H: 10 ms hand-off. 노드 초과 수요의 50 ms 평균이 0.1코어 이상이면 가장 바쁜 sandbox를 한가한 노드로 옮긴다.
  - POOL: 모든 코어를 하나로 합친 이상적 virtual node
- cache 모델
  - 노드에서 어떤 image의 sandbox가 처음 시작되면 working set 전체를 3FS에서 읽어 cache한다(fetch). 마지막 sandbox가 떠나고 600초 뒤 버린다.
  - 옮겨 간 sandbox는 목적지에서 working set의 ρ(0, 0.25, 1)만큼 새로 읽는다. ρ = 0.25는 bud04에서 잰 값이다(VM 수명 중 무작위 시각에 옮길 때 남은 base 읽기 비율 중앙값 0.25, 평균 0.28; [guest-memory](../guest-memory/README.md)).
  - working set은 image별로 bud05 host page cache에 상주한 block을 4 KiB 단위로 hash해 모은 것이다(`image_blocks.py`). 동기 step ×1.0 실행 동안 10분마다 찍은 snapshot 21개의 합집합이며, image 46개가 모두 실측이다. image 사이에 내용이 같은 block은 한 번만 센다.
  - "shared CXL cache"는 모든 노드가 cache 하나를 같이 쓰는 경우다. 배치와 무관하다.
- **모델 점검**: bud04 실측(한 host에서 VM 평균 147개, image 45개)에서 image의 host page cache는 평균 4.1 GiB, peak 5.4 GiB였다. 모델의 노드당 P1 cache(VM 96개)는 4.7~5.4 GiB, shared cache는 5.5 GiB다. 3시간 합집합을 working set으로 쓰므로 모델이 조금 크게 잡는다.

## 결과 (노드당 96개, ρ = 0.25, seed 평균)

칸의 값은 runqueue 대기 / 노드당 image cache(GiB)다.

| 노드 | LLM | 모드 | 사용률 | P1 | P1+H | P2 (locality만) | P5+H (고정 home + hand-off) |
|---|---|---|---|---|---|---|---|
| 8 | ×0.25 | 비동기 | 0.49 | 0.5% / 5.11 | 0.3% / 5.14 | 14.4% / 0.93 | **0.4% / 1.30** |
| 8 | ×0.15 | 비동기 | 0.74 | 5.9% / 5.34 | 2.3% / 5.39 | 64.0% / 0.95 | **2.4% / 1.89** |
| 8 | ×0.15 | 동기 | 0.48 | 6.2% / 4.80 | 3.5% / 4.94 | 62.4% / 0.88 | **4.3% / 1.79** |
| 16 | ×0.25 | 비동기 | 0.49 | 0.4% / 5.27 | 0.3% / 5.29 | 52.8% / 0.50 | **0.4% / 0.95** |
| 16 | ×0.15 | 비동기 | 0.75 | 5.4% / 5.42 | 1.9% / 5.47 | 107.5% / 0.51 | **2.0% / 1.61** |
| 16 | ×1.0 | 비동기 | 0.13 | 0.0% / 4.70 | 0.0% / 4.70 | 0.4% / 0.50 | 0.1% / 0.53 |

전체 표는 [report-v2.txt](report-v2.txt)와 [summary.json](summary.json)에, 그림은 [placement-cache-tradeoff.png](placement-cache-tradeoff.png)에, 설정은 [grid-cache.json](grid-cache.json)에 있다. v1(image 23개만 실측)의 표는 [report-v1.txt](report-v1.txt)에 있다. v1은 cache 절대값을 약 4배 작게 잡았다. 정책 사이의 순서와 배율은 v2와 같다.

- **고정 home + hand-off(P5+H)는 CPU 대기를 P1+H 수준으로 유지하면서 노드당 image cache를 약 3~6배 줄였다.**
  - 노드 16대, ×0.25: 5.27 GiB가 0.95 GiB가 됐다.
  - 사용률이 높으면(×0.15) hand-off 덕분에 대기도 P1보다 적다(5.4% → 2.0%).
- **메모리로 보면**: 노드 하나의 sandbox 메모리는 VM RssAnon 약 21 GiB(96 × 228 MiB, bud04 실측)에 image cache 5 GiB를 더한 값이다. 그래서 image cache는 약 20%를 차지한다. P5+H는 노드당 3.0~4.3 GiB, 즉 노드 sandbox 메모리의 11~16%를 줄인다.
- **cluster 합으로 보면**(노드 16대, ×0.25): P1이 84 GiB, P5+H가 15 GiB, shared CXL cache가 5.5 GiB다.
- **locality만 쓰면(P2, P5) CPU가 무너진다.** 대기가 사용률 50% 정도에서도 12~62%, 75%에서는 48~108%였다. 점수를 섞으면(P3) 그 중간에 놓인다. 이 결과의 요지는 hand-off가 locality 배치를 안전하게 만든다는 것이다.
- **현실 LLM 속도(×1.0)에서는 contention이 없다.** 이동 없이 locality 배치만으로 cache 이득을 다 얻는다(노드 16대에서 4.70 GiB → 0.50 GiB). hand-off는 밀도나 사용률이 높을 때만 필요하다.
- **P4(유동 locality + hand-off)는 P5+H보다 cache가 크다**(노드 16대, ×0.25에서 2.59 대 0.95 GiB). 옮겨 간 sandbox가 목적지에도 image를 올려놓고, 이후 배치가 그쪽을 따라가면서 image가 퍼진다. cache 이득을 유지하려면 home을 고정해야 한다.
- **3FS fetch**(노드 16대, ×0.25, GiB/시간): P1 40, P5+H는 ρ = 0이면 0.1, 0.25면 19, 1이면 64다. 실측 ρ(0.25)에서는 P1의 절반 이하다. ×0.15에서는 P1 27, P5+H 16이다.
- **이동 횟수**: 노드 16대, ×0.15에서 시간당 약 8만 번(노드당 초당 약 1.4번)이고, ×0.25에서는 시간당 약 5,500번이다.

## 실측 (실험 3, 중단)

bud04에서 실제 microVM으로 least-loaded / home 배치를 hand-off 유무별로 4회 돌리려 했다. 첫 실행(least-loaded) 14분째인 2026-10-27 09:33 UTC에 bud04가 갑자기 재부팅돼 결과가 없다. 직전 기록에서 NUMA 0 여유 메모리는 51 GiB였고, kernel 로그에는 종료 메시지나 오류가 없었다.

## 한계

- working set은 3시간 동안 상주한 block의 합집합이다. 600초 안의 실제 cache보다 클 수 있다(모델 점검 참고).
- 3FS 읽기 지연과 처음 읽을 때 생기는 tool call 지연은 모델에 넣지 않았다(fetch 양만 센다). image가 cache에서 밀려나는 것은 600초 고정이고, 메모리 압박에 따른 eviction은 넣지 않았다.
- 노드는 15코어다. 192코어 노드에서는 노드 안에서 burst를 흡수해 CPU 쏠림이 작다(본문 8절). 그만큼 locality 배치만으로 충분할 가능성이 크다.
- 노드 메모리 비율 계산은 guest kernel 쪽 비용(VM당 약 107 MiB)이 지금과 같다고 가정했다.
