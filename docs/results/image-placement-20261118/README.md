# image에 따른 배치와 page cache 메모리 (2026-11-18)

계산 분석이다. 새 실측은 없다. 입력은 264 trace를 Docker로 cold 재생해 잰 image working set이다(옛 checkout `harness/trace-replay/results/pagecache-discover-20261103`, Git 밖). 11-09에 같은 계산을 했지만 스크립트가 남지 않아 다시 짰다. 11-09 값(퍼뜨림 271 GiB, task별로 모으기 56 GiB, repo별로 몰기 44 GiB)과 맞는다.

질문: storage를 DSec처럼 EROFS로 두고 page cache 메모리만 볼 때, 같은 image나 같은 lower image를 한 노드로 모으면 메모리를 얼마나 아끼나? CPU는 이 계산에 없다.

## 0. 결론

1. **page cache 대부분은 task 하나만 쓰는 layer다.** 264 task의 working set을 layer 기준 한 벌로 세면 39.4 GiB다.
   - task 하나만 쓰는 layer: 36.6 GiB
   - 한 repo의 여러 task가 쓰는 layer: 1.0 GiB
   - 여러 repo가 쓰는 layer(base, toolchain): 1.9 GiB. sandbox마다 읽은 양을 더하면 23.1 GiB지만 한 벌로는 작다.
2. **image를 보지 않고 퍼뜨려도 공통 layer는 이미 노드 안에서 공유된다.** 16노드에 task 264개 × rollout 8개를 퍼뜨리면 page cache가 274 GiB이고, 그중 233 GiB가 task 전용 layer다. 한 task의 rollout 8개가 평균 6.4개 노드에 흩어져 그만큼 복사본이 생긴다. 공유가 전혀 없으면(sandbox마다 따로) 512 GiB다.
3. **같은 image를 모으면 page cache가 크게 준다.** 같은 image를 한 노드에 b개씩 두면 b = 2/4/8에서 39/64/80% 준다. b = 8이면 274 → 55 GiB, 노드당 17.1 → 3.4 GiB, sandbox당 133 → 27 MiB다. 줄어드는 몫은 대략 1 − 1/b를 따른다.
4. **같은 lower image까지 몰아도 더 아끼는 것은 작다.** lower image로 몰면 42 GiB로, 같은 image 8개 모으기보다 23%(노드당 0.8 GiB) 더 준다. 대신 한 노드 sandbox의 81%가 같은 lower image(같은 repo 계열, 같은 언어와 toolchain)가 된다. repo로 몰아도 비슷하다(44.8 GiB, 54%).
5. **용량 이득은 sandbox의 다른 메모리에 달렸다.** 메모리로 막힐 때 노드당 sandbox 수는 (다른 메모리 + page cache 몫)에 반비례한다. 다른 메모리가 0.1 / 0.5 / 1.5 GiB면 같은 image 8개 모으기가 ×1.83 / ×1.20 / ×1.07이다. container에서 LLM 대기 중 남는 메모리는 50~76 MB이고 대부분 page cache였다([11-12](../agent-workload-20261112/README.md)). microVM은 guest 메모리가 남으므로 다른 메모리가 더 크다.
6. **실행 시간에서 얻는 것은 작게 추정된다(2.1절).** 처음 읽기를 형제와 나누는 이득은 명령 시간 합의 0.2~1.5%다(대역폭 0.25~2 GB/s).
7. **CPU 쪽 비용은 이 계산 밖이다.** 같은 image는 같은 task이고, RL에서는 같은 GRPO group이다. [11-05 실측](../pool-mover-20261105/README.md) 5.1절에서 group 8개가 한 노드에 몰리면 노드 평균 사용률 38%에서도 그 group의 tool 시간이 2.87배, group 완료 시간이 1.39배였다. 미리 나누면 2.00배, 1.24배였다. 같은 trace를 복제했으므로 상한이다.

## 1. 가정과 방법

| 항목 | 값 |
|---|---|
| storage | DSec처럼 image layer를 읽기 전용 EROFS로 둔다. 한 노드에서 같은 layer를 쓰는 sandbox는 page cache를 같이 쓴다. task 전용 layer(workspace)도 공유 EROFS layer라고 본다. microVM이면 guest가 host page cache를 DAX로 매핑해야 이 공유가 생긴다 |
| page 열쇠 | (layer diff_id, 파일 경로, page 번호). 같은 layer의 같은 page는 노드마다 한 벌 |
| working set | trace마다 image lower 파일의 page cache를 비우고 Docker로 cold 재생(대기 없이)한 뒤 남은 page. 중앙값 139 MiB, p90 566 MiB, 평균 248 MiB. 실행 전체의 합이고, 시간에 따른 변화와 회수는 보지 않는다 |
| layer 분류 | 여러 repo가 쓰는 layer / 한 repo의 여러 task가 쓰는 layer / task 하나만 쓰는 layer |
| lower image | base부터 쌓은 layer 사슬 중 다른 image도 가진 가장 긴 것. 264 image에서 17개다(repo는 38개) |
| cluster | 16노드, task 264개 × rollout 8개 = 2,112 sandbox, 노드마다 132개 |
| 배치 | 퍼뜨림: image를 보지 않고 무작위 순서로 돌려 넣는다(power-of-k가 image에 대해 하는 일). image b개씩: task의 rollout 8개를 b개 묶음으로 나눠 가장 덜 찬 노드에 둔다. lower image로 몰기·repo로 몰기: 그 기준으로 정렬해 노드를 차례로 채운다 |
| seed | 3개 평균 |

## 2. 결과 (`image-place.json`)

| 배치 | page cache 합 | 노드당 (최소~최대) | sandbox당 | 퍼뜨림 대비 | 한 노드에 모인 같은 task (노드 평균) | 노드에서 가장 큰 lower image 몫 |
|---|---|---|---|---|---|---|
| 퍼뜨림 | 274.3 GiB | 17.1 (14.6~19.7) GiB | 133.0 MiB | – | 3.1 | 0.18 |
| 같은 image 2개씩 | 168.1 | 10.5 (8.5~13.0) | 81.5 | −39% | 4.5 | 0.21 |
| 같은 image 4개씩 | 97.8 | 6.1 (4.2~8.2) | 47.4 | −64% | 6.7 | 0.22 |
| 같은 image 8개씩 | 54.6 | 3.4 (1.9~5.5) | 26.5 | −80% | 8.0 | 0.25 |
| lower image로 몰기 | 42.0 | 2.6 (0.5~6.9) | 20.4 | −85% | 8.0 | 0.81 |
| repo로 몰기 | 44.8 | 2.8 (0.6~6.8) | 21.7 | −84% | 8.0 | 0.54 |

layer 종류별 page cache 합(GiB, 여러 repo / repo 안 / task 전용):

| 배치 | 여러 repo | repo 안 | task 전용 |
|---|---|---|---|
| 퍼뜨림 | 27.7 | 13.7 | 232.9 |
| 같은 image 8개씩 | 14.1 | 3.9 | 36.6 |
| lower image로 몰기 | 3.5 | 1.3 | 37.3 |

같은 image 8개 모으기의 이득은 거의 모두 task 전용 layer에서 나온다. lower image로 몰기가 더 아끼는 것은 여러 repo·repo 안 layer의 노드별 복사본이다.

메모리로 막힐 때 노드당 sandbox 수(퍼뜨림 = 1). sandbox당 page cache 몫은 노드당 132개일 때 값이다.

| sandbox의 다른 메모리 | 같은 image 2개 | 4개 | 8개 | lower image로 몰기 |
|---|---|---|---|---|
| 0.1 GiB | ×1.28 | ×1.57 | ×1.83 | ×1.92 |
| 0.5 GiB | ×1.09 | ×1.15 | ×1.20 | ×1.21 |
| 1.5 GiB | ×1.03 | ×1.05 | ×1.07 | ×1.07 |

### 2.1 처음 읽기 비용 추정

모으기가 실행 시간을 줄이는 길은 형제가 읽어 둔 page cache를 쓰는 것이다. 그 크기를 cold 재생의 working set과 명령 시간으로 어림했다. cold와 warm을 직접 비교한 측정은 없다.

| 읽기 대역폭 | 처음 읽기 시간 (중앙값) | 명령 시간 합 대비 (중앙값 / p90 / 전체) |
|---|---|---|
| 0.25 GB/s | 0.54초 | 1.5% / 6.4% / 1.5% |
| 0.5 GB/s | 0.27초 | 0.8% / 3.2% / 0.7% |
| 2 GB/s | 0.07초 | 0.2% / 0.8% / 0.2% |

- 명령 시간은 cold 재생(대기 없음)의 명령 시간 합이다. 중앙값 43초, p10 12초, p90 122초다. LLM 대기를 넣은 rollout 시간 대비로는 더 작다.
- working set을 한 번 읽는다고 보았다. 4 KiB씩 따로 기다리면(readahead가 없으면) 더 길다. 메모리가 모자라 버렸다 다시 읽는 비용은 넣지 않았다.
- GRPO 형제는 함께 시작해 같은 page를 거의 동시에 처음 읽는다. 모아도 첫 읽기를 기다리는 시간은 비슷하고, 읽는 양이 준다.
- 같은 task가 다음 step에 같은 노드로 돌아오면 이 비용을 통째로 아낀다.
- DSec은 8,192개 burst에서 원격 lazy 읽기가 image를 모두 로컬에 둔 경우와 같은 시간(약 35분)에 끝났다고 보고했다([trace-character-20261109](../trace-character-20261109/README.md)). 처음 읽기 비용이 작다는 쪽과 맞다.

## 3. 한계

- **page cache는 회수된다.** 깨끗한 page라 메모리가 모자라면 버렸다가 다시 읽을 수 있다. 중복이 실제로 용량을 막는 몫은 다시 읽히는 page에 달려 있다. 11-09 예비 측정(13 trace, 원자료는 남기지 않았다)에서 여러 task가 같이 쓰는 page는 수명 후반에도 90%가 다시 읽혔고, task 전용 page는 bytes 기준 29%만 다시 읽혔다. 그러면 퍼뜨림의 실제 비용은 위 표보다 작다.
- **같은 task의 rollout이 같은 page를 읽는다고 본다.** replay라 같다. 실제 rollout은 서로 다른 파일을 읽어 형제 사이 공유가 이보다 작을 수 있다.
- **microVM에서는 조건이 붙는다.** DSec의 microVM처럼 task workspace를 VM마다 따로 쓰는 디스크로 두면 같은 노드에 모아도 공유가 없다. guest page cache도 VM마다 따로 생기므로 DAX가 필요하다.
- **노드당 sandbox 수를 132로 고정했다.** 더 많이 넣으면 노드 안 공유가 더 커진다.
- CPU와 노드 사이 page cache 공유(`~/cxl-page-cache` 프로젝트)는 보지 않았다.

## 4. 재현

```bash
# 입력: pagecache-discover-20261103의 trace별 working-set.npz, lowers.json과 discover.jsonl
# bud04 /Serverless/ijkim-vnode-sim/imgplace-20261118/data 에 복사해 두었다
python experiments/capsim/image_place.py <data> image-place.json   # bud04 NUMA 0에서 약 1분
```

## 파일

| 파일 | 내용 |
|---|---|
| `image-place.json` | 전체 결과. working set 분위수, layer 종류별 합과 한 벌, 공유 없음, 배치 6종의 page cache·노드별 값·같은 task 수·lower image 몫 |
