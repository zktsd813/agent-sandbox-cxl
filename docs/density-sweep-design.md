# 밀도 sweep: 코어당 sandbox를 늘리면 무엇이 먼저 막히나 (2026-11-02)

목적: 사용률이 낮은 것이 overcommit이 부족해서인지 확인한다. 현실 LLM 속도(×1.0)에서 코어당 sandbox 수를 DSec 수준 이상으로 올리고, CPU, 메모리, 디스크, LLC 중 어느 것이 먼저 한계에 닿는지 잰다. 배경은 [load-balancing-survey.md](load-balancing-survey.md) 4.1절이다.

## 1. 방법: sandbox 수가 아니라 코어 수를 줄인다

sandbox 수를 늘리면 image가 더 필요하다. 지금 디스크로는 1.3배도 어렵다(우리 몫 141 GB 중 여유 59 GB, root 디스크는 system disk). 그래서 **같은 192개(step당)를 더 적은 코어에 올려** 코어당 밀도를 올린다. image와 trace가 같으므로 새 build가 없다.

| 수준 | 노드 (코어 + SMT sibling) | 물리 코어 | 코어당 | L3 way (15 × 코어/32) |
|---|---|---|---|---|
| c30 | 2-16,66-80 / 17-31,81-95 | 30 | 6.4 | 14 |
| c16 | 2-9,66-73 / 17-24,81-88 | 16 | 12 | 8 |
| c12 | 2-7,66-71 / 17-22,81-86 | 12 | 16 | 6 |
| c8 | 2-5,66-69 / 17-20,81-84 | 8 | 24 | 4 |
| c6 | 2-4,66-68 / 17-19,81-83 | 6 | 32 | 3 |

- DSec 비교: microVM 최대 4.2, container 최대 약 17(microVM 1/2 환산이면 8.5), scale unit 평균 12.7.
- **LLC를 코어 수에 맞춘다.** 코어를 줄여도 socket의 LLC는 그대로라, 그냥 두면 코어당 LLC가 실제 고밀도 host보다 넉넉해진다. 그래서 모든 sandbox를 resctrl group `ctls`에 넣고 L3 way를 코어 비율로 준다(`--ls-resctrl`).
- **메모리 대역폭은 맞추지 못한다.** MBA는 코어별 요청 속도를 늦추는 장치라 "몫"을 나눌 수 없다. 한계로 적는다.
- **메모리와 디스크는 상한을 걸지 않고 잰다.** 192개가 쓰는 메모리와 디스크는 코어 수와 거의 무관하다. 그래서 실행마다 최대치를 재고, 코어당 비율로 바꿔 서버 구성과 비교한다.
  - 메모리 비율: bud05 NUMA 0은 128 GB / 32코어 = 4 GB/코어. DSec microVM host는 1.5 TB / 192코어 = 7.8 GB/코어.
  - 디스크 비율: DSec는 local storage 3.4 TB / 192코어 = 17.7 GB/코어.
  - 상한을 걸면 Firecracker guest 메모리가 익명이라(swap 없이) 회수할 수 없어 OOM kill로 끝난다. 그 결과는 계산으로 같은 판단이 나온다.

## 2. workload와 조건

- **workload**: 새 계획의 segment 1(orig step 7, 9)이다. image가 이미 있다. ×1.0이므로 원본 wave 간격을 쓰는 `plan-c2-1srv-v2-orig-x1.json`을 쓴다(step 사이 idle은 run 2 중앙값 1,170초).
- **LLM**: token 모델 ×1.0.
- **배치**: 노드당 96개, least, 이동 없음, BE 없음.
- **SMT**: 켬. governor는 `schedutil`. turbo는 BIOS에서 꺼져 있다.
- **기준 실행(solo)**: SMT를 켠 상태에서 한 번 돈다(VMM thread는 sibling에 둔다). DSec matrix와 같이 쓴다(solo는 명령 간격이 0이라 LLM 모델과 무관하다).
- **cgroup**: 수준마다 `systemd-run --user --scope`로 실행해 cgroup 메모리를 따로 잰다.
- **시간**: 수준당 약 2.1시간(step 2개 × 약 60분 + idle). 5개 수준과 solo를 합해 약 12시간이다.

## 3. 측정

| 자원 | 기록 | 판단 |
|---|---|---|
| CPU | `cpu1ms.csv`(노드 runqueue), `vcpu.csv`(VM별 10 ms), `turbostat.tsv`(thread별 busy, 주파수, IPC) | 대기/실행, 명령 느려짐 분해(`slowdown_breakdown.py`), rollout과 step 시간(`sync_steps.py`) |
| 메모리 | `host.csv`의 `cg_mem_gib`, `cg_anon_gib`, `cg_file_gib`, `cg_oom_kill`, PSI memory, 회수·swap 횟수. `sandboxes.jsonl`의 guest anon·cache | 최대 cgroup 메모리 ÷ 물리 코어 = 필요 GB/코어 |
| 디스크 | `host.csv`의 `vm_disk_gib`(job + writable 할당 합), 디스크 섹터·시간, PSI io, 여유 공간 | 최대 `vm_disk_gib` ÷ 코어 |
| LLC | `resctrl.csv` 대신 turbostat IPC. 필요하면 `ctls` group의 `mon_data` | 코어당 LLC를 고정했을 때의 CPU 시간 증가 |
| 생성 경로 | `prepare_s`, 부팅, 첫 명령 지연 | 밀도에 따라 늘어나는가 |
| network, softirq | `host.csv` | 밀도에 따라 늘어나는가 |

## 4. 예상 (기존 측정으로 계산)

×1.0 검증 실행에서 두 노드 합계 CPU 수요는 평균 약 2.0코어, 1초 p99 약 8.3코어였다. 최대 host anon 합계는 38 GB였고, 여기에 image page cache 20~30 GB를 더해 메모리 최대치를 60~70 GB로 잡았다.

| 수준 | 코어당 | CPU 평균 사용률 | p99 / 코어 수 | 필요 메모리 GB/코어 | bud05 비율(4)로 | DSec 비율(7.8)로 |
|---|---|---|---|---|---|---|
| c30 | 6.4 | 7% | 0.28 | 2.2 | 여유 | 여유 |
| c16 | 12 | 12% | 0.52 | 4.1 | **경계** | 여유 |
| c12 | 16 | 17% | 0.69 | 5.4 | 초과 | 여유 |
| c8 | 24 | 25% | **1.04** | 8.1 | 초과 | **경계** |
| c6 | 32 | 33% | 1.38 | 10.8 | 초과 | 초과 |

- bud05의 메모리 비율이면 **코어당 약 12개에서 메모리가 먼저** 찬다.
- DSec의 비율이면 **코어당 약 24개에서 CPU p99와 메모리가 함께** 한계에 닿는다.
- SMT를 켜면 CPU 처리량이 1.3~1.5배라 CPU 한계는 조금 더 뒤로 간다.
- 이 표가 맞는지, 그리고 CPU가 한계에 가까워질 때 대기가 runqueue로 나타나는지 SMT 때문에 실행 시간 증가로 나타나는지 보는 것이 이 실험이다.

## 5. 판정

- 사용률이 낮은 이유가 "메모리가 먼저 차서"라는 것은 다음으로 확인한다. c12~c16 수준에서도 CPU 대기와 step 시간이 c30과 비슷하다. 그러면서 필요 메모리가 서버의 GB/코어 비율을 넘는다.
- CPU가 먼저 막힌다면 c8 이하에서 대기/실행과 step 시간이 크게 늘고, 그때 필요 메모리가 7.8 GB/코어 이하다. 이 경우 CPU 불균형(그리고 hand-off)의 동기가 DSec 비율의 host에서 성립한다.
- 메모리 pool(CXL)의 동기는 "메모리 비율 때문에 CPU를 놀리는 수준"이 얼마나 되는지로 정한다. 예를 들어 bud05 비율에서는 c16~c8 사이가 CPU에는 여유가 있지만 메모리로는 갈 수 없는 구간이다.

## 6. 준비 상태

| 항목 | 상태 |
|---|---|
| `host.csv`의 cgroup 메모리, VM 디스크 열 | 구현, smoke 확인 |
| `--ls-resctrl GROUP` (firecracker가 vCPU thread를 만들기 전에 group에 들어간다) | 구현, smoke에서 thread 전부 들어간 것을 확인 |
| 대기열 `/Serverless/agent-sandbox-cxl/logs/queue-density.sh` | 작성, 실행 전 |
| 디스크 | 수준마다 VM 디스크 최대치가 약 40 GB(192개)이고 여유는 59 GB다. 수준 사이에 VM 디스크는 지워진다 |

## 7. 한계

- 메모리 대역폭은 코어 비율로 줄이지 못했다. 고밀도 수준에서 대역폭 경합을 실제보다 작게 본다.
- 메모리·디스크 한계는 계산으로 판단한다. 실제로 넘었을 때의 동작(OOM, 회수 지연)은 재지 않는다.
- step 2개, 수준마다 1회다.
- 노드 수는 2개로 고정이다. 노드가 작을수록(c6은 노드당 3코어) √N 효과가 작아져 대기가 커진다. 큰 노드를 같은 밀도로 쓰는 경우와 다르다.
