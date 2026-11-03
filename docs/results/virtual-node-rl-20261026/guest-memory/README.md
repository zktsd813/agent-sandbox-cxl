# guest 메모리 구성과 virtio-pmem DAX (bud04, 2026-10-27)

질문: sandbox 하나가 host 메모리를 어디에 쓰나. 그중 읽기 전용 base image의 몫은 얼마인가. base를 DAX(virtio-pmem)로 붙여 guest page cache 사본을 없애면 얼마나 줄어드나. 옮겨 간 sandbox가 목적지에서 새로 읽을 base 비율(ρ)은 얼마인가.

## 방법

- bud04 NUMA 0, 15코어 노드 2개(ISO, pack), 노드당 96개(코어당 6.4), LLM ×0.25, ramp 600초 + 측정 1,800초. 두 실행은 seed(`mem1`)가 같아 같은 group 순서를 뽑는다.
- **blk**: base image를 읽기 전용 virtio-blk로 붙인다(지금 기본).
- **pmem**: base를 virtio-pmem으로 붙이고 guest에서 `ro,dax,noload`로 mount한다(`--base-pmem`). upper(쓰기 층)와 job 디스크는 그대로 virtio-blk다.
- guest RAM 2 GiB, 1 vCPU, balloon free page reporting을 켰다.
- 계측
  - guest: 5초마다 MemFree, Cached, Buffers, AnonPages, Shmem, Dirty와 디스크별 읽기 sector(`--mem-sample-s 5`)
  - host: VM별 RssAnon / RssFile, image 파일별 host page cache 상주량(mincore, 60초 간격)
- 각 실행 전에 image 파일의 host page cache만 비웠다(`image_blocks.py evict`). 다른 파일의 cache는 건드리지 않았다.
- 결과: [mem-report.json](mem-report.json) (`mem_report.py`)

## 결과

### VM 하나 (측정 구간, VM별 중앙값의 중앙값 / 평균, VM 726개)

| 항목 | blk | pmem |
|---|---|---|
| host RssAnon | 190 / 228 MiB | 235 / 269 |
| guest 사용량 (2 GiB − MemFree) | 174 / 208 | 214 / 245 |
| guest kernel 등 (사용량 − page cache − anon − shmem) | 107 / 106 | 191 / 194 |
| guest page cache (Cached + Buffers) | 61 / 96 | 13 / 46 |
| guest AnonPages | 0.3 / 1.6 | 0.3 / 1.2 |
| 해제됐는데 host가 들고 있는 몫 (RssAnon − 사용량) | −4 / 16 | −3 / 19 |

- **guest 프로세스 메모리는 거의 0이다.** 측정 시간 대부분이 LLM 대기다. tool call이 쓴 메모리는 끝나면 해제되고, free page reporting이 host로 돌려준다(해제됐는데 남은 몫이 중앙값 0 근처).
- **VM 하나가 host에서 쓰는 메모리의 절반 이상이 guest kernel 쪽이다**(약 107 MiB). 2 GiB guest RAM의 struct page 32 MiB, kernel image, slab 등이며, 세부는 재지 않았다. 나머지 대부분은 page cache다.
- **guest page cache 중 base image 몫은 약 48 MiB다**(blk와 pmem의 중앙값 차이). host RssAnon의 약 1/4이다. 나머지 13 MiB는 upper와 job 디스크다.

### host 전체 (평균 147개 VM 동시)

| 항목 | blk | pmem |
|---|---|---|
| RssAnon 합 | 32.5 GiB (peak 43.3) | 38.6 (49.6) |
| image의 host page cache | 4.1 GiB (peak 5.4) | 4.9 (6.5) |
| **sandbox 메모리** (위 둘의 합) | **36.4 GiB** (peak 46.1) | **43.2** (53.0) |
| RssFile 합 (DAX 매핑. image cache와 같은 page를 VM마다 셈) | 0.3 | 9.1 |

pmem이 sandbox 메모리를 19% **늘렸다**.

### 왜 늘었나: pmem 장치의 struct page

- guest kernel은 pmem 장치 전체에 대해 4 KiB마다 64 B의 struct page를 guest RAM에 만든다. 장치 크기의 1/64다.
- image 파일 평균이 5.49 GiB(최대 9 GiB)라 VM마다 평균 88 MiB가 든다. 측정한 guest kernel 쪽 증가는 84 MiB(107 → 191)로 이 값과 맞는다.
- 없앤 base page cache(약 48 MiB)보다 크다. DSec 논문이 적은 "128 GB 장치에 guest RAM 2 GB"와 같은 비용이다.
- 이 비용은 image 파일 크기에 비례하고, 실제로 읽는 양과는 관계가 없다. VM당 base 읽기는 중앙값 53 MiB, p90 125 MiB, 평균 70 MiB다.
- image를 실제 사용 크기로 줄이면(`resize2fs -M`) 비용도 줄어든다. 표본 3개의 ext4 사용량은 1.5~2.5 GiB였고, 그러면 struct page는 24~40 MiB다. 이때 순이득은 VM당 10~25 MiB(sandbox 메모리의 5~10%)로 추정한다. 실측은 하지 않았다.

**정리**: guest 안 base image 사본은 host 전체에서 약 6.9 GiB(147 × 48 MiB)로 sandbox 메모리의 약 19%다. DAX로 없앨 수 있는 상한이 이 정도다. struct page 비용을 빼면 실제로 얻을 수 있는 몫은 그보다 훨씬 작다. image data 공유(dedup)로 얻는 메모리 이득은 이 workload에서 작다.

### 지연

| | blk | pmem |
|---|---|---|
| 사용률 | 50.6% | 50.2% |
| runqueue 대기 / 실행 | 4.9% | 4.2% |
| 명령 느려짐 p50 / p90 / p99 | 1.28 / 1.74 / 4.01 | 1.22 / 1.60 / 3.18 |
| tool 결과 불일치 / timeout (명령 약 5,000개) | 694 / 48 | 762 / 44 |

pmem이 조금 빠르다. virtio-blk 요청 없이 load로 읽기 때문일 수 있다. 조건마다 1회라 차이의 크기는 믿지 않는다.

### ρ: 옮겨 간 sandbox가 새로 읽을 base 비율

blk 실행에서 VM별 base 디스크 누적 읽기를 VM 나이별로 보았다(완료한 VM 585개).

| VM 나이 | 30초 | 60초 | 120초 | 300초 | 600초 |
|---|---|---|---|---|---|
| 앞으로 남은 읽기 비율 (중앙값 / 평균) | 0.79 / 0.69 | 0.57 / 0.53 | 0.31 / 0.35 | 0.09 / 0.16 | 0 / 0.04 |

- **수명 중 무작위 시각에 옮기면 남은 비율은 중앙값 0.25, 평균 0.28이다.** [placement-cache](../placement-cache/README.md)의 simulator가 가정한 ρ = 0.25와 맞는다.
- 읽기는 앞에 몰린다. 처음 2분에 2/3를 읽는다. rollout 수명은 중앙값 437초, p90 693초다.
- 남은 읽기는 아직 읽지 않은 block이다. 그래서 guest 메모리가 함께 가는 hand-off에서도 목적지에서 새로 읽어야 한다(목적지 host page cache 또는 원격 storage). 이미 읽은 block은 guest page cache에 있어 함께 간다.

## 한계

- 조건마다 1회, 한 서버의 노드 2개다.
- guest kernel 쪽 약 107 MiB의 세부(slab, page table)는 재지 않았다.
- pmem은 원래 크기 image로만 돌렸다. 줄인 image의 효과는 추정이다.
- ρ는 blk 실행에서 완료한 VM만 쓴 값이다. guest가 page cache를 버렸다 다시 읽은 경우도 읽기로 센다. guest 여유 메모리가 1.8 GiB라 드물 것으로 본다.

## 재현

```bash
cd experiments/contention
# image cache 기록과 image만 비우기는 새 driver 옵션
SEED=mem1 RAMP_S=600 DURATION_S=1800 CACHE_RESET=images IMAGE_CACHE_LOG=1 PILOT_ARGS="--mem-sample-s 5" \
  ./run_rl_matrix_paired.sh <solo-run> mem-s025-k96-blk:96:none:pack:0.25:none
SEED=mem1 RAMP_S=600 DURATION_S=1800 CACHE_RESET=images IMAGE_CACHE_LOG=1 PILOT_ARGS="--base-pmem --mem-sample-s 5" \
  ./run_rl_matrix_paired.sh <solo-run> mem-s025-k96-pmem:96:none:pack:0.25:none
python3 mem_report.py .local/runs/<blk-run> .local/runs/<pmem-run> --out docs/results/virtual-node-rl-20261026/guest-memory/mem-report.json
```
