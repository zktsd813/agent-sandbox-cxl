# image 모으기, CXL, hand-off를 함께 넣은 용량 simulator (2026-11-18)

simulator 분석이다. 새 실측은 없다. 질문은 "같은 image를 한 노드에 모으면 sandbox를 더 넣을 수 있나, CXL·hand-off와 같이 쓰면 어떤가"다. 앞선 계산([image-placement-20261118](../image-placement-20261118/README.md))은 page cache 메모리만 셌다. 여기서는 CPU와 실행 시간까지 넣는다.

## 0. 결론

**노드에 넣을 수 있는 sandbox 수는 메모리 천장과 CPU 천장 중 낮은 쪽이 정한다.** 어느 쪽이 먼저 막히는지는 LLM 대기의 길이가 정한다.

코어당 sandbox 수(같은 SLO, 96코어 × 16노드, 형제 시작 간격 최대 60초). 괄호는 DRAM 2 GiB/코어에 image를 퍼뜨린 경우 대비 배수다.

| LLM 대기 비율 | DRAM 2, 퍼뜨림 | DRAM 2, 가장 좋은 image 묶음 | + CXL 1 GiB/코어 | + CXL 2 GiB/코어 | + CXL 2 + hand-off |
|---|---|---|---|---|---|
| 74% (기록 그대로) | 1.01 CPU | 1.01 (묶지 않음) | 1.01 | 1.01 | 1.17 (×1.16) |
| 85% (기록 ×2) | 1.57 메모리 | 1.57 (묶지 않음) | 1.70 (×1.09) | 1.70 (×1.09) | 1.94 (×1.24) |
| 92% (기록 ×4) | 1.63 메모리 | 1.80 (×1.11, 4개씩) | 2.54 (×1.56) | 2.74 (×1.68) | 3.25 (×1.99) |

1. **LLM 대기가 짧으면 CPU가 먼저 막힌다.** 기록 그대로(대기가 sandbox 시간의 74%, sandbox당 평균 0.77코어)는 DRAM 2 GiB/코어에서도 메모리가 남는다. image 모으기도 CXL도 소용이 없고, hand-off만 +14~16%를 준다.
2. **대기가 길면 메모리가 먼저 막힌다.** 92%에서는 CXL 2 GiB/코어가 ×1.68, hand-off까지 더하면 ×1.99다. 85%에서는 메모리가 겨우 막혀 CXL이 +9%, hand-off까지 +24%다.
3. **image 모으기는 메모리가 막힐 때만 득이고, 그 크기는 CXL보다 작다.** 가장 좋을 때 +11%다(92%, 4개씩). sandbox당 메모리 1.2~1.6 GiB 중 image page cache 몫이 0.13 GiB(약 10%)뿐이기 때문이다.
4. **CPU가 막힐 때 image 모으기는 크게 손해다.** 같은 image 8개를 한 노드에 두면 sandbox 수가 −52%(74%), −43%(85%)다. 형제가 같은 명령을 비슷한 때 돈다.
5. **hand-off를 쓰면 image 모으기의 CPU 손해가 거의 사라진다.** 92%·CXL 2에서 묶음 크기와 관계없이 3.19~3.25다. 그래서 메모리가 막힐 때 둘을 같이 쓸 수 있다(CXL 1 + hand-off에서 8개씩 묶으면 +8%).
6. **page cache를 처음 읽는 시간은 작다.** sandbox당 0.02~0.09초다. hand-off로 옮긴 노드에서 다시 읽으면 0.2~0.5초다. sandbox 하나의 명령 시간 합은 평균 67초다. 실행 시간 차이는 대부분 형제 몰림에서 온다.
7. **형제가 함께 시작하면(간격 0초) image 모으기는 훨씬 나쁘다.** 8개씩 묶으면 가장 낮은 밀도에서도 SLO를 넘는다. 같은 trace를 복제한 재생이라 상한이다.

### 0.1 어느 천장이 먼저 막히나

sandbox 하나가 평균으로 쓰는 메모리와 CPU의 비를 보면 된다(퍼뜨림, 형제 간격 최대 60초, SLO를 지키는 밀도 근처).

| LLM 대기 비율 | 명령 실행이 sandbox 시간에서 차지하는 비율 | 실행 중 sandbox당 CPU | sandbox당 메모리 | 메모리 ÷ CPU |
|---|---|---|---|---|
| 74% (기록 그대로) | 26% | 0.78코어 | 1.50 GiB | 1.9 GiB/코어 |
| 85% (기록 ×2) | 15% | 0.44 | 1.27 | 2.9 |
| 92% (기록 ×4) | 8% | 0.25 | 1.22 | 4.9 |

- CPU 천장은 SLO(CPU 가중 명령 시간 ≤ 1.10, 무거운 명령 p99 ≤ 2.0) 때문에 코어 사용률 약 0.72에서 온다.
- 그래서 메모리 ÷ CPU가 DRAM/코어 ÷ 0.72보다 크면 메모리가 먼저 막힌다. DRAM 2 GiB/코어면 기준은 약 2.8 GiB/코어다.
- CXL의 이득은 대략 min(메모리 ÷ CPU ÷ 2.8, (DRAM + CXL) ÷ DRAM)이다. 92%에서 4.9 ÷ 2.8 = 1.75이고, tax를 넣은 결과는 ×1.68이다.
- 두 단계(CXL과 hand-off)가 모두 필요한 것은 이 비가 2.8~5.6 GiB/코어(DRAM 2 + CXL 2 GiB/코어)일 때다.
- iter2 RL 기록(Python)은 명령 실행(tool+agent)이 시간의 6.1%이고 sandbox당 0.02~0.06코어라 92%보다도 메모리 쪽이다. 이 경우 CXL은 쓸모가 크지만 hand-off가 필요할 만큼 CPU가 차지 않는다.

## 1. 모델

| 항목 | 값 |
|---|---|
| simulator | [`capsim.py`](../../../experiments/capsim/capsim.py)에 GRPO group, image 묶음, page cache 첫 읽기를 더했다. 기본값은 11-16·11-18 결과와 같은 값을 낸다(확인함) |
| cluster | 16노드 × 96코어, 10 ms step, 준비 600초 + 측정 2,400초, seed 2개 |
| 부하 | 다언어 264 trace(10-14 Docker DRAM 재생의 10 ms CPU, 명령별 메모리). LLM 대기는 기록(Gemini 3.5 Flash)의 1/2/4배 |
| GRPO group | 같은 trace 8개가 함께 시작한다(형제마다 0~60초 무작위 지연, 또는 0초). 8개가 모두 끝나야 group이 다음 task를 받는다 |
| image 묶음 b | 형제 8개를 b개씩 나눠, 묶음의 첫 sandbox가 놓인 노드에 나머지를 둔다. 묶음 사이 배치는 기존 방식(2개 뽑아 CPU가 낮은 쪽). b = 1은 image를 보지 않는 퍼뜨림 |
| page cache 첫 읽기 | 그 task의 page가 없는 노드에서 명령이 시작하면 task 전용 working set을 1 GB/s로 읽는 동안 기다린다. 먼저 읽기 시작한 형제가 있으면 남은 시간만 기다린다. 그 task의 sandbox가 노드에 하나라도 있는 동안 page가 남는다. hand-off 목적지에도 같다 |
| CPU 천장 | 메모리를 제한하지 않은 실행에서 CPU 가중 명령 시간 ≤ 1.10, 무거운 명령 p99 ≤ 2.0을 지키는 최대 밀도 |
| 메모리 천장 | 밀도마다 sandbox당 메모리 m(b) = R − u + u/b_eff + S/n ≤ 노드 메모리. R은 그 실행의 평균 상주(hwm 모델, Docker, 자기 image page를 포함), u = 0.1385 GiB(task 전용 working set 평균), b_eff는 한 노드에서 한 벌을 같이 쓰는 형제 수, S = 2.86 GiB는 여러 task가 같이 쓰는 layer 한 벌(노드당, 상한), n은 노드당 sandbox 수 |
| CXL tax | DRAM에 다 들어가지 못한 몫만 CXL에 둔다. 그 비율 f = max(0, 1 − DRAM/필요량)만큼, tax 없는 실행과 상태를 모두 CXL에 둔 실행(무거운 명령 +2%, 가벼운 명령 +3%, 11-13 실측) 사이를 선형으로 잇는다 |
| hand-off | 무거운 명령(1 CPU초 이상)이 시작될 때 노드에 폭 + 1코어가 없으면, 빈 코어가 가장 많은 노드로 10 ms에 옮긴다. 메모리는 공유 pool이라고 본다 |

## 2. 결과 (`capacity.txt`, `capacity.json`)

코어당 sandbox 수. 칸은 "값 / 막힌 쪽"이다.

**형제 시작 간격 최대 60초**

| LLM 대기 | 구성 | b = 1 | b = 2 | b = 4 | b = 8 |
|---|---|---|---|---|---|
| ×1 (74%) | DRAM 2 | 1.009 CPU | 0.917 CPU | 0.779 CPU | 0.489 CPU |
| | DRAM 2 + CXL 2 | 1.009 CPU | 0.917 CPU | 0.779 CPU | 0.489 CPU |
| | + hand-off | 1.150 CPU | 1.171 CPU | 1.146 CPU | 1.070 CPU |
| ×2 (85%) | DRAM 2 | 1.565 메모리 | 1.507 CPU | 1.258 CPU | 0.891 CPU |
| | DRAM 4 | 1.720 CPU | 1.507 CPU | 1.258 CPU | 0.891 CPU |
| | DRAM 2 + CXL 1 또는 2 | 1.699 CPU | 1.508 CPU | 1.258 CPU | 0.891 CPU |
| | + hand-off | 1.906 CPU | 1.932 CPU | 1.892 CPU | 1.941 CPU |
| ×4 (92%) | DRAM 2 | 1.629 메모리 | 1.697 메모리 | 1.801 메모리 | 1.547 CPU |
| | DRAM 4 | 2.865 CPU | 2.579 CPU | 2.249 CPU | 1.547 CPU |
| | DRAM 2 + CXL 1 | 2.472 메모리 | 2.539 CPU | 2.210 CPU | 1.546 CPU |
| | DRAM 2 + CXL 2 | 2.742 CPU | 2.539 CPU | 2.210 CPU | 1.546 CPU |
| | DRAM 2 + CXL 1 + hand-off | 2.526 메모리 | 2.618 메모리 | 2.721 메모리 | 2.731 메모리 |
| | DRAM 2 + CXL 2 + hand-off | 3.189 CPU | 3.214 CPU | 3.208 CPU | 3.248 CPU |

**같은 밀도에서 b에 따른 느려짐**(CPU 가중 명령 시간 / 무거운 명령 p99 / sandbox당 첫 읽기 초, 간격 최대 60초)

| LLM 대기, 밀도 | 구성 | b = 1 | b = 2 | b = 4 | b = 8 |
|---|---|---|---|---|---|
| ×1, 1.2 | DRAM | 1.089 / 1.67 / 0.09 | 1.132 / 1.94 / 0.06 | 1.249 / 2.57 / 0.03 | 1.409 / 3.23 / 0.02 |
| | hand-off | 1.039 / 1.33 / 0.21 | 1.039 / 1.34 / 0.20 | 1.044 / 1.37 / 0.19 | 1.041 / 1.35 / 0.18 |
| ×2, 2.2 | DRAM | 1.252 / 2.40 / 0.07 | 1.362 / 2.85 / 0.05 | 1.602 / 3.78 / 0.03 | 1.918 / 5.35 / 0.02 |
| | hand-off | 1.121 / 1.65 / 0.24 | 1.104 / 1.62 / 0.23 | 1.137 / 1.75 / 0.25 | 1.099 / 1.59 / 0.23 |

sandbox당 메모리(밀도별 평균): 대기 ×1 1.55 GiB, ×2 1.35 GiB, ×4 1.22~1.25 GiB. 8개씩 묶으면 0.07~0.14 GiB 준다.

형제가 함께 시작하는 경우(간격 0초)는 `capacity.txt`에 있다. 8개씩 묶으면 모든 대기 배율에서 가장 낮은 밀도부터 SLO를 넘는다.

## 3. 한계

- **형제는 같은 trace의 복제다.** 실제 rollout은 LLM 출력이 달라 명령과 시점이 어긋난다. 형제 몰림의 손해는 상한이다. 60초 간격은 그 사이 어디쯤을 보려는 가정이다.
- **sandbox당 메모리는 Docker 기록의 high-water mark다.** guest가 한 번 쓴 메모리를 돌려주지 않는 microVM을 흉내 낸 값이다. 메모리를 돌려주면(balloon, free page reporting) sandbox당 메모리가 작아지고, image page cache 몫과 image 모으기의 이득이 커진다.
- **자리 받기는 평균 상주 메모리로 본다.** 여유 없이 꽉 채운다고 보아 메모리 천장이 낙관적이다. 메모리가 넘칠 때의 비용(swap, 다시 읽기)은 넣지 않았다.
- page cache 공유는 DSec식 EROFS와 microVM DAX를 가정한다. hand-off 목적지에 생기는 page cache 한 벌은 메모리에 넣지 않았다(읽는 시간만 넣음).
- 96코어만 돌렸다. hand-off 목적지는 메모리 여유를 보지 않는다(공유 pool이라고 본다).
- CXL tax는 상태 전부를 CXL에 둔 실행과의 선형 보간이다.

## 4. 재현

```bash
cd experiments/capsim
# .local/inputs.npz(prep.py), .local/ws-per-trace.json(image_place.py --per-trace)
python imgcap.py .local/imgcap.jsonl 28          # bud04 NUMA 0, 3,520회, 약 25분
python imgcap_analyze.py .local/imgcap.jsonl --json .local/capacity.json
```

## 파일

| 파일 | 내용 |
|---|---|
| `imgcap.jsonl.gz` | 실행 3,520개(대기 3종 × b 4종 × 간격 2종 × 구성 4종 × 밀도 17~19 × seed 2) |
| `capacity.txt`, `capacity.json` | 메모리 천장과 SLO를 적용한 코어당 sandbox 수 |
