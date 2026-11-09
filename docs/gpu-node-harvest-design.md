# GPU 노드의 남는 CPU에 sandbox를 올리는 motivation 실험 설계 (2026-11-03)

목적: GPU 학습 노드의 남는 CPU에서 agentic RL sandbox를 돌리고, 학습이 CPU를 원할 때 hand-off로 CPU만 비워 주는 시나리오가 성립하는지 판단한다. 먼저 CXL 없이 A6000 × 2 서버에서 motivation(G1~G3)을 보고, 메커니즘(G4, G5)은 실제 비일관 CXL pool과 GPU가 있는 장치에서 잰다.

## 0. 요약

- **주장**: GPU 작업은 비싸고 critical path 위에 있다. sandbox는 싸고 critical path에서 얇다. 그래서 sandbox는 GPU 작업이 원할 때 즉시 양보해야 하고, 즉시 양보하려면 메모리를 옮기지 않아야 한다. 이 조건에서 CXL 상주 sandbox와 CPU만 비우는 hand-off는 고정 분할보다 GPU 노드에 sandbox를 더 많이 올린다.
- **지금 아는 것**: GPU 노드 CPU가 논다는 관찰은 accepted venue에 있다(Acme NSDI'24, Philly ATC'19). 실제로 GPU 학습 노드에 sandbox를 올리는 운영 사례도 있다(Aleph Alpha, 고정 분할). RL 학습 노드의 CPU 수요를 시간축으로 잰 연구는 찾지 못했다.
- **가장 큰 위험**: 고정 분할로 충분하다. 학습의 CPU 수요가 평평하거나, 고정 분할에 격리를 더한 것만으로 GPU 손해가 1% 이하이면 hand-off가 필요 없다.
- **첫 실험**: A6000 × 2 서버에서 sandbox 없이 RL 학습의 CPU, DRAM, DDR 대역폭을 phase별로 잰다(G1). 수요가 시간에 따라 크게 변할 때만 다음 단계로 간다.

## 1. 배경: 왜 이 방향인가

### 1.1 CPU 노드 사이 contention은 비용이 작았다

[실험 계획 v2](experiment-plan-tail-placement.md) 8장의 screen과 원본 job 분석 결과다.

| 측정 | 결과 |
|---|---|
| 원본 job critical path (8.5) | rollout 시간의 LLM 87.7%, env 생성 6.2%, tool+agent 6.1%. 모든 rollout의 tool ×1.2 → step +1.0%, LLM ×1.2 → step +13.9%, env 생성 ×2 → step +7.3% |
| sync screen (8.1~8.3) | 배치 사이 step 시간 차이 1.6% 이내. 쏠린 노드의 명령 +10~13% |
| async screen (8.4) | home 배치에서 노드 하나에 최대 84개, 대기/실행 42%. co-runner 60개 이상인 rollout의 명령 시간 합 중앙값 +44%, p90 +116%. 그래도 가장 느린 10% group +1.9%, 처리량 −1.2% |
| CXL tax ([DRAM/CXL 결과](results/numa-immediate-20261014/README.md)) | trace 단위 중앙값 +11.2%, 기하평균 +13.2% |

정리하면 다음과 같다.
- sandbox는 CPU overcommit을 잘 견딘다. 명령이 2배 느려져도 group은 2% 안팎만 늦다.
- 민감한 쪽은 GPU 엔진이다. LLM이 20% 느려지면 step이 14% 늦어진다.
- 그래서 CPU 노드 사이에서 sandbox를 옮기는 이득은 작다. 반대로 sandbox가 GPU 작업을 방해하는 비용은 크다.

### 1.2 버린 방향

- **CXL로 메모리 용량만 늘리기**: 새로움이 작다.
- **dedup과 snapshot**: 선행 연구가 많다(guest page cache를 다루는 DPC, CXL snapshot 논문들).
- **GPU 작업이 든 sandbox를 GPU 노드로 옮기기**: kernel RL은 평가에 sandbox 상태가 필요 없다. Kevin(ICLR'26)은 학습 GPU에서 vLLM을 CPU 메모리로 내린 뒤 직접 평가한다. KernelGym(Dr. Kernel)은 kernel source만 GPU worker pool로 보낸다. 이유는 세 가지다.
  - 평가가 (reference, candidate) → reward인 순수 함수다.
  - speedup이 reward라 GPU 한 장에 task 하나를 단독으로 돌린다.
  - 생성된 kernel이 CUDA sticky error를 자주 내서, 평가마다 새 process를 쓴다. NVIDIA 문서에 따르면 illegal address 같은 오류 뒤에는 process를 다시 띄워야 한다.

  그래서 이 방향은 GPU 작업이 sandbox 상태(빌드 결과, 설치한 package, data)에 의존할 때만 성립한다. 그런 workload의 trace가 없어 보류한다.

## 2. 시나리오와 설계

- **빌릴 수 있을 때**: GPU 노드에서 학습이 쓰지 않는 코어에 sandbox를 올린다. sandbox 메모리는 처음부터 CXL에 둔다. 그래서 GPU 노드의 DDR 용량과 대역폭을 쓰지 않는다(offload, pinned buffer 보호).
- **빌릴 수 없을 때**: 학습이 CPU를 원하는 구간에는 hand-off로 vCPU 실행만 다른 노드로 넘긴다. 메모리와 EPT는 CXL에 남고, 넘기는 상태는 약 20KB다. [HANDOFF](HANDOFF.md) 2장의 hand-off와 같다.
- **비울 대상**: rollout 시간의 약 88%가 LLM 대기라서 대부분의 sandbox는 CPU를 쓰지 않는다. 대기 중인 sandbox는 그대로 두고, LLM 응답이 오면 빈 노드를 골라 실행한다(tool call 경계 배치). 구간이 시작될 때 tool call을 실행 중인 sandbox만 중간에 넘기면 된다.

학습이 CPU를 원하는 구간은 RL 구성마다 다르다. 측정 전 가정이다.

| 구성 | 예상되는 CPU·host 메모리 구간 |
|---|---|
| colocated sync(verl 기본) | 학습 phase에는 생성이 멈춰 sandbox도 대부분 대기한다. 충돌은 생성 phase의 엔진 CPU thread(scheduler, detokenize)와 sandbox 사이다. 학습↔생성 전환 때 weight와 KV를 host DRAM으로 내리고 올린다(vLLM sleep mode, FSDP offload) |
| 학습·생성 분리 async | 학습 노드는 step마다 optimizer step, checkpoint, weight 전송 구간이 온다. 생성 노드는 엔진 thread가 계속 돈다 |
| optimizer CPU offload | step마다 CPU Adam이 모든 코어를 쓰는 구간이 생길 수 있다(ZeRO-Offload, ATC'21). Kevin도 DeepSpeed ZeRO-3 optimizer offload를 썼다 |
| 여러 job이 cluster를 공유 | job마다 패턴이 달라, 어느 순간이든 빌릴 수 있는 노드가 있을 가능성이 크다 |

## 3. 근거와 선행 연구

| 출처 | 종류 | 내용 | 이 시나리오에서의 의미 |
|---|---|---|---|
| Acme (NSDI'24) | 학회 | LLM 개발 cluster, A100 4,704장, 노드당 128 thread·1~2 TB. "CPU는 대체로 놀고"(GPU당 CPU 16개), host 메모리 사용률 50% 미만, IB NIC는 60% 넘는 시간 동안 idle | 빌릴 자원이 있다. 다만 pretraining과 평가가 대상이고 RL은 아니다. CPU 사용률은 그림(CDF)에만 있다 |
| Philly (ATC'19) | 학회 | "many servers underutilize CPU cycles yet highly utilize memory"(Fig 7) | 같은 관찰. 2017년 workload |
| CODA (ICDCS'20), Synergy (OSDI'22) | 학회 | 이미지·음성 학습은 전처리 때문에 CPU 코어에 민감하다. GPU 서버에 CPU job을 같이 올리는 scheduler | workload에 따라 다르다. 같은 노드 CPU job 배치는 선행 연구가 있다 |
| Meta DSI (ISCA'22) | 학회 | 추천 모델 전처리가 학습 노드 CPU를 넘어 별도 서버로 분리 | LLM과 반대 사례. 원문 미확인 |
| Harvest VMs (OSDI'20), Memory-Harvesting VMs (ASPLOS'22) | 학회 | cloud에서 남는 CPU와 메모리를 빌리는 VM. MHVM은 회수할 때 VM 메모리를 줄인다 | harvesting 자체는 오래된 주제다. sandbox는 메모리를 줄일 수 없다는 점이 다르다. Harvest VMs는 원문 미확인 |
| CoTail (arXiv 2609.05425) | preprint | 7B vLLM과 nginx를 같이 돌리면 throughput −78.8%. engine thread를 SCHED_FIFO로 올리면 −6.8%, TPOT +2.3%로 회복되지만 TTFT는 +298%가 남는다. 다른 NUMA로 떼어 놓아야 throughput 손실 6% 미만 | 우선순위만으로는 다 못 막는다. co-tenant가 CPU를 계속 쓰는 작업이라 실제 sandbox보다 무겁다 |
| DSec (arXiv 2609.22978) | preprint | scale unit 하나가 CPU 노드 약 160대, 30K core, DRAM 약 250 TB. 동시 sandbox 최대 약 38만. on-prem 80%를 넘으면 cloud로 넘긴다. sandbox와 GPU 서버를 network로 분리하고, agent loop을 preemptible GPU pod 밖으로 뺐다. 90% sandbox가 요청 CPU의 5% 이하를 쓴다 | sandbox 수요가 크고 peak가 넘친다. 보안 원칙(network 분리)과 GPU preemption 처리가 반론이자 요구사항이다 |
| Aleph Alpha 블로그 | 업계 | B300 8장짜리 노드 32대 중 31대에서 노드마다 CPU 112 thread를 sandbox에 고정. sandbox 약 25,000개가 약 3,500 thread를 나눠 쓴다. unprivileged user로 격리. 학습 slowdown 측정은 없다 | 이미 하는 운영이고, baseline은 고정 분할이다 |
| RollArt (arXiv 2512.22560) | preprint | 환경은 stateful·CPU-bound·heavy-tailed라 CPU 서버로 분리. 격리 없이 같이 두면 disk I/O가 quota를 고갈시킨다 | 분리의 이유가 격리다 |

DGX H100은 노드당 112 core, 2 TB다. 30K core는 약 270대(GPU 약 2,100장)의 CPU와 같다. 수천 GPU 규모 학습 cluster의 CPU를 절반만 써도 sandbox cluster 하나 분량이다. 실제로 얼마나 비는지는 Acme 그림 수준의 추정이다.

## 4. 예상 reviewer 질문과 필요한 답

| 질문 | 필요한 답 |
|---|---|
| 고정 분할로 충분하지 않나 (Aleph Alpha) | G1: 수요가 시간에 따라 크게 변한다. G2: 고정 분할과 격리로는 GPU 손해가 남는다 |
| cpuset을 동적으로 줄이면 되지 않나 | G2의 4와 5의 차이. 학습이 거의 모든 코어를 쓰는 구간이 있거나, 코어를 나눠도 LLC·DDR·mesh·IRQ로 간섭이 남아야 한다 |
| CPU 서버는 싸다 | G3을 CPU 노드 수와 cloud bursting 양으로 바꿔 보인다. 비용 대신 "GPU step time을 지키면서 같은 수의 sandbox"를 지표로 쓴다 |
| migration이나 snapshot으로 비우면 | G4: 비울 때 복사가 GPU 노드 NIC와 DDR을 쓴다. 구간이 짧거나 비울 양이 많을수록 차이가 커진다 |
| GPU 노드가 CXL pool에 붙나 | 실제 비일관 CXL pool과 GPU 장치에서 잰다. G5에서 CXL 트래픽과 GPU DMA의 간섭을 잰다 |
| harvesting은 오래된 주제다 | 빌리는 쪽이 상태를 가진 VM이고, 돌려줄 때 ms 안에 복사 없이 돌려준다는 점. 메모리를 줄일 수 없는 tenant라 MHVM 방식이 안 된다 |
| untrusted code가 학습 노드에 올라간다 | microVM과 CXL 메모리로 container보다 강하게 격리한다. network 경로는 DSec처럼 제한한다 |

## 5. 실험

공통 지표:
- **GPU 쪽**: step time과 phase별 시간, 생성 throughput, TTFT, TPOT, 학습 tokens/s
- **sandbox 쪽**: group 지연과 처리량(CPU 전용 노드 대비)
- **자원**: core별 CPU, thread별 CPU, host DRAM 사용량(pinned 포함), DDR·PCIe·CXL 대역폭

### G1. 학습 노드는 언제 CPU를 원하나 (sandbox 없이)

- **방법**: RL 학습만 돌리며 100 ms 단위로 자원을 기록한다. framework의 phase(생성, logprob, update, weight sync, checkpoint) 시각을 함께 남긴다. 2장 표의 구성 세 가지(colocated sync, 분리 async, optimizer CPU offload)를 각각 돌린다.
- **산출**:
  - 빌릴 수 있는 코어 수의 시간 분포
  - 남는 코어가 적은 구간의 길이, 주기, phase와의 대응(미리 알 수 있는가)
  - host DRAM과 DDR 대역폭이 peak가 되는 구간
- **중단 기준**: 수요가 평평하다. 즉 step 안에서 peak와 평균 수요의 차이가 노드 코어의 10% 미만이면, 고정 분할로 충분하므로 멈춘다.

### G2. sandbox가 GPU 작업을 얼마나 늦추나, 무엇으로 막히나

- **변수**: 노드에 올린 sandbox 수(0, 50, 100, 200, 400…)와 격리 방식

| # | 격리 방식 |
|---|---|
| 1 | 격리 없음 |
| 2 | 고정 분할(cpuset, Aleph Alpha 방식) |
| 3 | 2 + SCHED_IDLE + CAT(LLC 분할) + MBA + IRQ affinity |
| 4 | 제자리 동적 축소: CPU를 많이 쓰는 phase에 sandbox cpuset을 줄인다 |
| 5 | 비우기: 그 구간 동안 노드에 sandbox가 없다(hand-off의 상한) |
| 6 | 2~5 각각에서 sandbox 메모리를 GPU 소켓 DDR에 둔 경우와 CXL(또는 대용)에 둔 경우, SMT를 켠 경우와 끈 경우 |

- **보는 것**:
  - 2와 4의 차이: 동적 조절의 가치
  - 4와 5의 차이: 제자리에서 못 막는 간섭, 즉 hand-off만의 가치
  - 6: CXL 상주가 DDR 간섭을 없애는지
- **중단 기준**: 3이나 4로 목표 밀도에서 GPU 손해가 1% 이하이면 hand-off가 필요 없으므로 멈춘다.

### G3. 같은 GPU 성능에서 sandbox를 몇 개 올릴 수 있나

- **정의**: GPU step 손해 1% 이하, sandbox group 지연 5% 이하에서 GPU 노드당 sandbox 수. G2 결과에서 계산한다.
- **비교**: 고정 분할 / 제자리 동적 축소 / migration으로 비우기 / hand-off로 비우기
- **환산**: DSec 규모(scale unit 하나에 30K core)에서 필요한 CPU 노드 수와 cloud bursting 양
- **근거로 쓸 기존 결과**: async screen에서 명령이 2배 느려져도 group은 +2%였다. sandbox 쪽 조건(5%)은 높은 overcommit에서도 맞출 수 있을 것으로 본다.

### G4. 비우는 비용 (실제 CXL 장치)

- **상황**: phase 신호가 오면 tool call 실행 중인 sandbox N개를 비운다.
- **방식**:
  - kill 후 재시작
  - M2: Firecracker full snapshot을 공유 storage로 보내고 restore
  - M3: QEMU `x-ignore-shared`, 메모리는 CXL DAX
  - M4: Firecracker restore. 메모리 파일을 CXL DAX에 두고 MAP_SHARED, 메모리 dump 생략, 목적지 process 미리 생성
  - M4+P: M4에 `KVM_PRE_FAULT_MEMORY`로 resume 전 EPT 채우기
  - hand-off: vCPU·device 상태 약 20KB만 넘기고 EPT 입양
- **측정**:
  - 전체를 비우는 데 걸리는 시간과 sandbox downtime
  - 비우는 동안 GPU 노드 NIC·DDR·PCIe 사용량과 GPU 쪽 slowdown
  - coherence flush 비용
- **변수**: N, sandbox 메모리 크기
- **중단 기준**: 구간이 길고 미리 알 수 있고 N이 작아 migration으로 충분하면, CXL이 필요 없다.

### G5. CXL이 GPU 경로와 간섭하나 (실제 CXL 장치)

- sandbox가 CXL을 많이 쓸 때 GPU host↔device DMA 대역폭(pinned H2D/D2H, NCCL)이 떨어지는지 잰다. CXL 장치와 GPU가 같은 root complex나 PCIe switch를 쓰는지 확인한다.
- GPU 노드에서 sandbox의 CXL tax를 잰다. CPU 노드 결과(trace 중앙값 +11.2%)와 비교한다.

## 6. A6000 × 2 서버에서 먼저 할 것 (CXL 없이)

이 서버는 [RL rollout 수집 계획](trace-collection-plan.md)의 시험 서버와 같다. 거기 기록된 상태는 A6000 두 장(NVLink bridge), CUDA와 Docker 설치다. 실행에 필요한 준비는 [부록](#부록-a6000에서-실행-준비)에 모았다. 이 실험을 위해 다른 문서를 읽을 필요는 없다. 이 서버에서 확인한 사양과 구체적인 실험 행렬, 그림은 [A6000 실행 계획](gpu-node-harvest-a6000-plan.md)에, G1 결과는 [A6000 G1과 decode CPU 결과](results/gpu-harvest-a6000-20261109/README.md)에 있다.

### 6.1 볼 수 있는 것

| 실험 | 가능 여부 | 방법 |
|---|---|---|
| G1 | 가능 | 작은 model RL 학습 |
| G2 조건 1~5 | 가능 | 5(비우기)는 vCPU를 다른 코어 집합으로 다시 pinning하는 기존 10 ms hand-off 흉내(`sandbox.py --move hand-off`)로 대신한다. 1소켓이면 freeze로 대신한다 |
| G2 조건 6 | 2소켓이면 근사 | 반대편 소켓 DRAM을 CXL 대용으로 쓴다(remote NUMA로 CXL을 흉내 낸 Pond 방식) |
| G3 | 가능 | G2에서 계산 |
| G4, G5 | 불가 | 실제 장치 필요 |

### 6.2 구성

- **노드 흉내**: bud05처럼 서버 하나를 나눈다.
  - "GPU 노드": GPU가 붙은 소켓의 코어 일부와 그 소켓의 DDR. GPU당 코어를 production 비율에 맞춰 제한한다(DGX H100은 GPU당 14코어, Acme는 GPU당 16 thread). 비율 자체도 변수로 GPU당 8/16/32코어를 둔다. 제한하지 않으면 워크스테이션의 남는 CPU가 과장된다.
  - "CPU 노드": 나머지 코어(가능하면 반대편 소켓). 비울 때 sandbox가 가는 곳이다.
- **GPU 쪽 workload**:
  1. 추론만(G2 중심): vLLM을 GPU 1~2장에 띄운다. SWE trace의 turn별 prompt·completion token 수와 동시 요청 수로 요청을 재생한다. CoTail과 같은 측정인데, co-tenant가 실제 sandbox 부하다.
  2. RL 학습(G1 중심): verl GRPO, model 1.5B~7B. 2장 표의 세 구성을 돌린다. 2 × 48 GB에서 7B를 full fine-tune하려면 optimizer나 parameter offload가 필요할 가능성이 크다. 그게 보고 싶은 CPU 구간을 만든다. task는 math든 code든 상관없다. GPU 쪽 phase 구조가 실제와 같으면 된다.
- **sandbox 쪽**: bud05의 Firecracker SWE replay(`experiments/contention/sandbox.py`, token model LLM 시간)를 그대로 옮긴다. sandbox는 그 서버의 엔진과 연결될 필요가 없다. 실제 부하 패턴을 내는 tenant 역할이다.
- **준비**: 설치, image 복사, 새로 만들 도구는 [부록](#부록-a6000에서-실행-준비)에 있다.

### 6.3 먼저 확인할 서버 정보

코어 분할, NUMA 배치, model 크기를 정하는 데 필요하다.

```bash
lscpu | grep -E "Model name|Socket|Core|Thread|NUMA"
numactl -H
nvidia-smi topo -m
nvidia-smi --query-gpu=name,memory.total --format=csv
free -g
```

### 6.4 이 서버의 한계

- **PCIe GPU**: production RL은 NVLink로 묶인 SXM GPU를 쓴다. A6000은 host 전송이 PCIe를 거친다. CPU 간섭이 실제보다 크게 보일 수 있다. CoTail도 PCIe GPU(RTX 4090)를 썼다.
- **작은 model**: GPU kernel이 짧아 엔진의 CPU 쪽 처리 비중이 커진다. 이것도 간섭을 과장하는 방향이다. 올릴 수 있는 가장 큰 model로 재고, 이 점을 명시한다.
- **쓰임**: 여기 숫자는 이야기가 서는지 가르는 screen이다. 논문 수치는 실제 장치에서 다시 잰다.

## 7. 순서와 관문

| 순서 | 실험 | 장소 | 관문 |
|---|---|---|---|
| 1 | G1 | A6000 | 수요가 시간에 따라 크게 변한다 |
| 2 | G2 조건 1~3 | A6000 | 고정 분할과 격리로 GPU 손해가 남는다 |
| 3 | G2 조건 4~6, G3 | A6000 | 비우기가 제자리 축소보다 sandbox를 눈에 띄게 더 올린다 |
| 4 | G4, G5 | 실제 CXL + GPU 장치 | hand-off가 migration보다 빠르게, GPU 노드 자원을 쓰지 않고 비운다 |

1~3이 모두 통과하면 characterization(RL 학습 노드 자원 시간축), 메커니즘, end-to-end를 갖춘 논문이 된다. 1이나 2에서 멈추면 characterization만 남는다.

## 8. 열린 질문

- RL 구성 중 어느 것이 실제로 많이 쓰이는가. 공개된 대형 RL 학습이 optimizer offload를 쓰는가.
- GPU 노드가 preempt되거나 회수될 때 sandbox를 어디로 보내는가. sync job은 모든 노드가 같은 phase에 들어가므로, 받아 줄 곳이 phase가 다른 노드(다른 job, 생성 노드)여야 절약이 생긴다.
- 보안 경로: GPU 노드 위 microVM의 network를 DSec처럼 apiserver 하나로 제한할 수 있는가.

## 부록. A6000에서 실행 준비

### 설치

1. **NVIDIA Container Toolkit**(없으면): 배포판 package로 설치하고 `nvidia-ctk runtime configure --runtime=docker` 뒤 Docker를 재시작한다. 확인은 `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
2. **vLLM**: 공식 image `vllm/vllm-openai`. 버전을 고정하고 digest를 기록한다.
3. **verl**: commit을 고정해 설치한다. model은 1.5B~7B(Qwen 계열)에서 시작한다.
4. **측정 도구**: `perf`(uncore IMC로 DDR 대역폭), `sysstat`(`pidstat -t`), `nvidia-smi dmon`, Intel CPU면 PCM.

### sandbox replay를 옮기기

bud05의 Firecracker replay(`experiments/contention/sandbox.py`)를 그대로 쓴다.
- **필요한 것**: Docker, `fakeroot`, e2fsprogs, `numactl`, static `/usr/bin/busybox`, `/dev/kvm` 권한, 저장소의 Firecracker 빌드, guest kernel `.local/microvm/vmlinux-6.1.188`.
- **image**: bud05의 `experiments/contention/.local/images/<key>/rootfs.ext4`를 복사한다(home 배치 기준 47개, 115 GiB). 새로 만들려면 `sandbox.py build`를 쓴다.
- **실행**: async screen과 같은 명령에서 노드만 바꾼다. `--nodes`는 노드마다 CPU 목록을 `/`로 구분한다. 처음 둘이 "GPU 노드" 코어, 나머지가 "CPU 노드" 코어가 되도록 정한다.

```bash
python3 experiments/contention/sandbox.py --candidates experiments/contention/candidates-plan.json pilot \
  --nodes <노드0 CPU>/<노드1 CPU>/... --sibling-offset 0 --harness-cpus <관리 코어> \
  --per-node 48 --pool none --group-size 8 --placement home --llm-model tokens --gap-scale 0.25 \
  --move none --ramp-s 300 --duration-s 3300 --seed async --mem-sample-s 5
```

- **이미 있는 기능**:
  - `--move hand-off`: 쏠림을 감지하면 vCPU를 다른 노드 코어로 다시 pinning한다(10 ms 정지 흉내). G2 조건 5의 출발점이다.
  - `--ls-resctrl GROUP`: Firecracker thread가 vCPU를 만들기 전에 resctrl group에 넣는다. schemata로 sandbox의 L3 way(CAT)와 MBA를 제한하면 G2 조건 3이 된다.
  - `host.csv`: PSI, 회수, 디스크, softirq를 1초마다 기록한다.
- **SCHED_IDLE**: `chrt --idle 0`으로 `sandbox.py`를 시작하면 Firecracker thread가 정책을 물려받는다.

### 새로 만들 것

| 항목 | 내용 | 쓰는 곳 |
|---|---|---|
| 자원 sampler | 100 ms 단위 core별 CPU(`/proc/stat`), thread별 CPU, host 메모리, DDR·PCIe 대역폭. sandbox 없이도 돈다 | G1, G2 |
| phase 기록 | verl의 phase 시작·끝 시각을 같은 시계(CLOCK_MONOTONIC)로 남긴다 | G1, G2 |
| 요청 재생기 | SWE trace의 turn별 prompt·completion token 수와 동시 요청 수로 vLLM에 요청을 보낸다 | G2(추론만) |
| phase 연동 축소 | phase 신호가 오면 sandbox cgroup의 `cpuset.cpus`를 줄이고, 끝나면 되돌린다 | G2 조건 4 |
| phase 연동 비우기 | phase 신호가 오면 tool call 중인 sandbox를 "CPU 노드" 코어로 옮기고, 끝나면 되돌린다. 기존 `--move hand-off` 경로를 재사용한다 | G2 조건 5 |

### 실행 환경 기록

bud05와 같은 항목을 실행마다 남긴다. SMT 상태(`/sys/devices/system/cpu/smt/active`), turbo와 governor, resctrl mount 여부, GPU clock이다. GPU clock은 가능하면 `nvidia-smi -lgc`로 고정한다.

## 9. 출처

- [Acme (NSDI'24)](https://www.usenix.org/conference/nsdi24/presentation/hu), [arXiv](https://arxiv.org/html/2403.07648v2)
- [Philly (ATC'19)](https://www.usenix.org/system/files/atc19-jeon.pdf)
- [CODA (ICDCS'20)](https://www.cs.sjtu.edu.cn/~leng-jw/pubs/zhao20icdcs-coda.html), [Synergy (OSDI'22)](https://arxiv.org/abs/2110.06073)
- [Memory-Harvesting VMs (ASPLOS'22)](https://www.microsoft.com/en-us/research/publication/memory-harvesting-vms-in-cloud-platforms/)
- [CoTail (arXiv 2609.05425)](https://arxiv.org/html/2609.05425)
- [DSec (arXiv 2609.22978)](https://arxiv.org/html/2609.22978v1)
- [Aleph Alpha: RL environments are an infra problem](https://aleph-alpha.com/en/blog/why-rl-environments-have-always-been-an-infra-problem/)
- [RollArt (arXiv 2512.22560)](https://arxiv.org/html/2512.22560v2)
- [Kevin (arXiv 2507.11948)](https://arxiv.org/html/2507.11948), [Dr. Kernel / KernelGym (arXiv 2602.05885)](https://arxiv.org/html/2602.05885v1)
- [CUDA Runtime API, cudaError](https://docs.nvidia.com/cuda/cuda-runtime-api/group__CUDART__TYPES.html)
- [DGX H100 사양 (Stanford Marlowe)](https://docs.marlowe.stanford.edu/specs.html)
- Harvest VMs(OSDI'20), Meta DSI(ISCA'22), ZeRO-Offload(ATC'21)는 원문을 열어 확인하지 않았다.
