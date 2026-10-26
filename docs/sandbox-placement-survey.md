# production sandbox 배치 조사: 조건, 코드, 이유 (2026-10-26)

질문: 실제 RL 학습과 sandbox 플랫폼은 sandbox를 어떤 조건으로 node에 놓고, 왜 그렇게 하는가. 사용자 가설인 "개수가 같아도 node마다 실제 CPU 사용률이 달라지고, 한 번 배치하면 그 차이가 오래간다"를 판단하는 근거로 쓴다.

조사 범위는 세 갈래다.
1. RL 학습 시스템(논문, 블로그, 공개 코드)
2. sandbox 플랫폼 코드
3. cluster scheduler 문헌

코드는 표에 적은 commit에서 읽었다. 본문의 주요 인용은 원문에서 다시 확인했다. 확인한 것은 E2B, sunabako, SkyRL-v0, R2E-Gym, ROLL, Nebius, Prime, DSec, Resource Central, Coach다. 나머지는 조사 agent가 원문에서 옮긴 것이다. **[추론]** 표시는 원문에 없는 해석이다.

## 0. 요약

1. **배치는 생성 때 한 번만 한다.** 실행 중인 sandbox를 CPU 부하 때문에 다른 node로 옮기는 시스템은 하나도 없었다.
   - 살아 있는 sandbox를 옮기는 유일한 예는 VenusRL이다. 계기는 메모리 OOM이다.
   - 나머지 재배치는 정지했던 sandbox를 다시 켤 때의 resume/restore뿐이다. 이때도 원래 node가 먼저다(DSec, E2B, AgentENV, CubeSandbox, ROCK). Daytona만 원래 runner가 붐비면 backup에서 다른 runner로 복원한다.
2. **배치 신호는 거칠다.**
   - sandbox 개수: DSec watcher, AgentENV 필터, PipelineRL, ROLL, k8s agent-sandbox warm claim
   - 정적 request: Kubernetes 기본 scheduler, Ray
   - 예약 메모리: sunabako, VenusRL
   - 수 초~수 분 늦은 측정 CPU: E2B 10~20초, Daytona 5분 평균과 15분 load average, Koordinator 60/300초, Trimaran 5~15분 창
   - ms~s 단위 tool call burst를 보는 신호는 없다.
3. **CPU는 일부러 overcommit한다.** 이유는 모두 "LLM을 기다리는 동안 논다"이다.
   - DSec: sandbox의 약 90%가 요청 CPU의 5% 이하를 쓴다
   - VenusRL: 약 14배
   - Prime: node당 256개, Burstable
   - Nebius: request = 사용량 중앙값
   - E2B: 할당 vCPU 4배를 점수의 분모로 쓴다. hard cap은 2026-07에 제거했다
4. **node와 workload가 상관을 갖게 만드는 배치 조건이 실제로 있다.**
   - image·template·snapshot locality: SkyRL-v0, CubeSandbox, Daytona, Cloudflare, agent-sandbox fleet KEP
   - group CoW 공유: VenusRL
   - 사용자 격리: DSec
   - backend·hardware별 후보 걸러내기: DSec
   - workload별 별도 cluster: RollArt
5. **보고된 문제는 대부분 생성 경로에 있다.**
   - control plane 과부하: dockerd crash, K8s API·etcd 포화
   - image pull 폭주와 disk 압박
   - 생성 지연의 꼬리, node 전체 OOM
   - DSec의 LS/BE 간섭(SMT, turbo, LLC)
   - **개수가 같은 node들 사이에서 실제 CPU 사용률 불균형을 잰 공개 수치는 찾지 못했다.**

## 1. RL 학습 시스템

| 시스템 | 배치 신호와 정책 | 단위·시점 | GPU와 같은 node | 밝힌 이유 | 보고된 문제 | 출처 |
|---|---|---|---|---|---|---|
| **DSec** (DeepSeek) | 후보 걸러내기(health, backend, hardware) → power-of-k 중 "least loaded". load는 정의되지 않음. watcher가 모으는 것은 "number of running sandboxes ... per edge, per user, and per task". 최근 배치를 local view에 더함. edge가 최종 거절권. 80% 넘으면 cloud bursting(image가 30TB 집합에 다 들어 있는 task만) | sandbox마다, 생성 때. pause/resume은 같은 edge. migration 없음 | 아니오. 별도 CPU fleet(약 160 node, 30K 코어) | "avoiding herding and reducing interference among bursty RL environments during setup and tool calls". 1초 안에 수천 개 생성. GPU 선점, 신뢰 경계 | LS/BE 간섭 +45.2% → +17.3%. 생성 timeout, image 분배 압박 | arXiv 2609.22978 §3.2–3.4, §4, §5.2, §7, §8.5 |
| **VenusRL** | node의 측정 메모리가 임계값 T 아래일 때만 받음. T는 예측 증가량으로 동적으로 정함. 넘으면 redirect. CPU는 신호가 아님 | sandbox마다. OOM이면 RSS가 가장 큰 sandbox를 RDMA로 옮김(2.7초) | 분리형. 물리 위치는 안 밝힘 | "Memory, not CPU or network, is the deployment bottleneck". 메모리 사용은 평균 약 8% | 메모리 burst(pip 설치, 0.5% 미만). 생성 수가 막히면 처리량 0.51배 | arXiv 2610.03286 §2.2, §5.1–5.2, App. A, D |
| **Kimi K3 / AgentENV** | 개수, 시작 중 개수, 측정 CPU%, 할당 %(정지 포함) 기준선으로 거른 뒤 round-robin 또는 random. 요청에 담긴 CPU·메모리·image hint는 무시 | microVM마다, 생성 때. resume은 원래 node | 별도 runtime node | 수명의 최대 98%가 LLM 대기. container runtime에서 kernel panic | 메모리 overcommit 6.5배(report) vs 9.6배(README) | K3 report §5.3.2, kvcache-ai/AgentENV `services/scheduler/internal/` @bc51e699 |
| **MiniMax / Tencent CubeSandbox** | 거르는 조건: CPU quota, **측정 CPU 사용률 상한**, 메모리, **template이 그 node에 있어야 함(hard filter)**, 진행 중 생성 수. 그다음 가중 점수(microVM 수, 진행 중 생성, CPU, 메모리; 기본 가중치는 확인 못 함)로 상위 몇 개 중 random | sandbox마다, 생성 때. restore는 원래 node가 먼저이고, 다른 node는 cpuid hash와 kernel이 같을 때만 | 별도 sandbox cloud | K8s로는 동시성이 안 나옴 | K8s master 과부하, 10만 개 시작에 수 분 | 36kr 인터뷰, TencentCloud/CubeSandbox `CubeMaster/pkg/selector` @1a3d38ba |
| **GLM / slime / sunabako** | 남은 예약 메모리(× `memory_overcommit`)에 비례한 가중 random. node별 admission lock | sandbox마다, 생성 때 | 전용 sandbox node(Ray 밖) | 코드 주석: "A slightly stale least-loaded snapshot otherwise sends an entire concurrent burst to one node" | 불안정한 sandbox(sample 제외), 꼬리 | zhuzilin/sunabako `client.py` L365–369 |
| **Qwen / MegaFlow** | FIFO + 분산 semaphore. 8코어·16GB ECS에 task 하나 | task마다 | 아니오 | 큰 host(208코어, task 50개)에서 image pull 혼잡, 초기화 경합. 동시 1,000개에서 시작이 1분 → 13분 | | arXiv 2601.07526 |
| **RollArt** (Alibaba) | 전용 K8s CPU cluster(SWE용과 나머지용 분리). node 신호는 안 밝힘. trajectory는 같은 instance로 고정 | 환경마다 | 아니오 | CPU를 쓰는 환경이 GPU node에서 경합. 같은 disk I/O quota를 나눠 쓰다 연쇄 실패 | env.reset 꼬리가 수백 초. container 생성이 host CPU·disk를 소모 | arXiv 2512.22560 §1, §3.1, §4.3, §7.1 |
| **ROLL / ROCK** | ROLL: `rank % num_nodes`. ROCK: Ray SPREAD + 요청 CPU, 재시작 시 원래 host로 고정. burst 한도 ≤ 2 × 요청. image는 모든 node에 미리 받음 | worker·sandbox마다 | ROLL은 같은 Ray node, ROCK은 별도 [추론] | ROLL 코드 주석: CPU worker가 한곳에 몰려 생기는 OOM을 피하려고 | | alibaba/ROLL `resource_manager.py` L136, alibaba/ROCK `operator/ray.py` |
| **DeepSWE / R2E-Gym** | 기본 kube-scheduler. request 1 CPU / 1Gi, limit 없음(Burstable). nodeSelector `karpenter.sh/nodepool: bigcpu-standby`. SWE image는 모든 node에 미리 받음 | trajectory pod마다 | 별도 CPU pool | 512개를 docker로 동시에 띄우다 dockerd가 죽음 | | together.ai/blog/deepswe, R2E-Gym `runtime/docker.py` L258 |
| **SkyRL-v0** | **`sha256(image) % replicas`로 node를 정함.** image가 없으면 random. 부하 신호 없음 | image마다. 따라서 같은 task의 GRPO group 전체 [추론] | 분리형(EKS i4i) | "Each replica ... doesn't have even NVMe storage for all images ... we route by Image ID" | NVMe에 image가 다 안 들어감. 16 CPU node에 80~100개 | SkyRL-v0 blog, SkyRL-OpenHands `k8s_deploy/api-gateway/main.go` L61, L89 |
| **Nebius** (SWE-rebench) | 기본 kube-scheduler. request = 사용량 중앙값(0.5 CPU / 2GiB), limit 약 4 CPU / 16GB | pod마다 | **GPU node에 같이 놓음** | "deploy agents next to training and inference runs". GPU 서버의 CPU·메모리가 남아서 | 꼬리, Pending pod, zombie, rate limit | arXiv 2508.03501 App. D, Nebius blog |
| **NVIDIA NeMo-Gym / Nemotron 3 Super** | Ray SPREAD, `num_cpus=0.1`. GPU node마다 약 32 task | rollout마다 | **GPU node(Ray cluster 하나)** | root가 없어 Apptainer를 씀. CPU가 남아서 | node 전체 OOM(memory watchdog 추가). pkill이 vLLM을 죽임 | arXiv 2604.12374 §3.2.5, NeMo-Gym `swe_agents/app.py` |
| **Prime INTELLECT-3** | 자체 bin-packer와 autoscaler. node당 256개, Burstable. 점수는 비공개 | sandbox마다 | 분리형 [추론] | "sandboxes alternate between brief execution spikes and idle waiting periods" | API server·etcd 포화(명령당 2.5초), image 전송이 NIC를 포화 | arXiv 2512.16144 §2.3 |
| **ARL-Tangram** (Xiaomi) | 첫 action 때 자원이 충분한 node 중 CPU·메모리 부하 균형으로 고르고, trajectory를 그 node에 고정. 코어는 action마다 cgroup으로 다시 줌 | node는 trajectory마다, CPU는 action마다 | 별도 CPU cluster(256코어 × 15) | 환경을 수명의 47%만 씀 | K8s 기준선은 batch 1536에서 과부하 | arXiv 2603.13019 §5.2, §6 |
| PipelineRL | node별 CPU-heavy job 수가 가장 적은 곳(고정된 개수) | 환경 server마다, 시작 때 | 같은 node | 안 밝힘 | | ServiceNow/PipelineRL `world.py` |
| Orchard (Microsoft) | 기본 scheduler, `workload=sandbox` node pool, request = limit | pod마다 | 전용 spot pool | | 생성 timeout, eviction | microsoft/Orchard `k8s_client.py` |
| AReaL / AEnvironment | 기본 scheduler, request = limit(Guaranteed) | pod마다 | 별도 [추론] | | | inclusionAI/AEnvironment `aenv_pod_handler.go` |
| Kimi k1.5/K2, GLM, Seed, KAT-Coder | 안 밝힘 | | 대부분 분리형 | | KAT-Coder V2.5: sandbox 원인 실패 16%, image가 disk를 거의 채움, timeout 6~7% | 각 tech report |

## 2. sandbox 플랫폼 코드

| 플랫폼 | 배치 | locality | overcommit | 배치 후 재배치 | 출처 |
|---|---|---|---|---|---|
| **E2B** | Best-of-K(K=3, 무작위 표본). 점수 = (요청 + 할당 vCPU + 생성 중 + 0.5 × busy cores) / (4 × cores)와 hugepage 점수 중 큰 값 | 새 생성은 보지 않음. resume은 snapshot을 만든 node에 고정. CPU 모델 호환 필수 | 4배는 점수 분모일 뿐. hard cap은 2026-07 제거. node당 200개, 동시 시작 3개 | 없음 | e2b-dev/infra `placement_best_of_K.go` @8d800b4 |
| **Daytona** v0.190.0 | TOPSIS 점수. 측정 CPU(5분 평균) 0.25, 메모리·disk 0.4, 할당 각 0.03. 상위 10개 중 random | snapshot이 준비된 runner만 후보(runner의 1/3에 전파) | 할당 500%가 위험선 | 정지했던 sandbox를 켤 때만 다른 runner로 | daytona `runner.service.ts` @01c502b |
| k8s agent-sandbox / GKE | 기본 kube-scheduler. warm pool은 남은 warm 개수가 가장 많은 node부터(NodeSpread) | fleet KEP: image hash로 cluster 고정 | burstable 허용 | 없음 | kubernetes-sigs/agent-sandbox @f83f2d8 |
| AWS Lambda | sticky routing + placement service가 자원을 고르게 하고 상관된 배치를 줄임 | sticky, 3단 cache | 최대 10배 | 없음(slot은 12시간 뒤 재활용) | Firecracker NSDI'20 §4 |
| Fly.io | region 안 best-fit 순위 | region | | drain용 kill → clone뿐 | fly.io blog |
| Cloudflare Containers | image가 미리 받아진 가장 가까운 위치 | image 필수 | | | CF docs |

E2B의 주목할 이력:
- 2026-10-23 commit `2e64e00833` 전까지 측정 CPU 항은 host 전체 %를 vCPU처럼 다뤘다. 그래서 최대 0.5 vCPU어치만 반영됐다.
- [추론] 그동안 사실상 할당 vCPU만 보고 배치했다.
- 같은 commit의 test 주석: "One more allocated vCPU must not outweigh a nearly saturated host."
- PR #2201은 오래된 metric 때문에 생긴 thundering herd를 다뤘다.

## 3. 왜 개수나 요청량으로 배치하는가

| 이유 | 근거 |
|---|---|
| **보장과 일관성** | Kubernetes 문서: request 합 ≤ 용량은 "protects against a resource shortage on a node when resource usage later increases". descheduler도 "consistency with the kube-scheduler"를 위해 request를 쓴다. Borg는 prod task의 feasibility를 limit으로 계산한다 |
| **지금 사용률은 미래 peak를 말해 주지 않는다** | Take it to the Limit(EuroSys'21): "Placing a new task based only on the available resources at the time of scheduling can lead to resource shortages". Trimaran: 평균 기반은 "does not consider the bursty variations". Resource Central: 여러 VM의 high percentile이 시간상 겹치면 고갈 |
| **metric이 늦고 오래됐다** | DSec는 local view에 최근 배치를 더하고 edge가 거절한다. sunabako 주석: 오래된 least-loaded가 burst 전체를 한 node로 보낸다. E2B PR #2201. Koordinator·Trimaran은 최근 pod를 request로 추정한다. Sparrow와 Mitzenmacher는 오래된 정보로 인한 herding을 다룬다 |
| **속도와 규모** | DSec: 1초 안에 수천 개, 초당 5,000개 이상. E2B: 배치 3.07ms → 38.8µs(PR #1044). Borg는 score caching과 random 탐색 없이는 3일이 걸렸다. Protean은 VM 하나에 20ms |
| **사용량 예측이 어렵다** | Trimaran: 사용자가 정확한 사용량을 추정하기 어렵다. Sparrow: 작업 시간 예측은 "notoriously difficult". Coach: VM은 플랫폼에서 보이지 않는다 |
| **옮기는 것은 비싸니 잘 놓고 두자** | Resource Central: "it is better to place VMs where they can stay". Coach: "migration is the last option as it is the most expensive". Sandpiper: migration 중 처리량이 약 20% 떨어진다. Google과 Azure의 live migration은 유지보수와 packing 용도이고, hot spot 해소 용도라는 공개 자료는 없다 |
| **node 안 QoS로 대신한다** | DSec: SCHED_IDLE + core scheduling. Borg: CPU는 throttle하면 되고, thread-per-request가 지속 불균형을 완화한다 |

재배치 주기의 예: descheduler 2~5분, VMware DRS 1분(demand 창 15분), Sandpiper 10초 주기로 감지하고 20~237초 안에 해소. 모두 초~분 단위다.

## 4. 사용자 가설에 주는 의미

가설: "개수가 같아도 workload가 달라 node마다 실제 사용률이 다르고, 배치 뒤 그 차이가 오래간다. 한 node 안의 결정으로는 최적을 정할 수 없으니 여러 node의 CPU를 하나로 본다."

**가설을 받쳐 주는 사실**
1. 배치 신호가 개수, 요청량, 예약량이거나, 측정값이라도 수 초~수 분 늦다. 생성 때 한 번 정하고 옮기지 않는다. 그러니 생긴 차이는 sandbox 수명만큼 남는다. DSec 중앙값 15~17분, p99 3시간 이상.
2. **node와 workload를 묶는 배치 조건이 실제로 있다.**
   - SkyRL-v0는 image hash로 같은 task의 group 전체를 한 node에 보낸다. 무거운 task(예: Java 빌드)의 group 8개가 한 node에 들어간다.
   - CubeSandbox는 template이 있는 node만 후보로 둔다. Daytona는 snapshot이 1/3 runner에만 있다.
   - VenusRL의 group CoW는 group이 한 node에 있어야 효과가 있다.
   - DSec는 사용자를 node 단위로 격리하므로 job 구성이 node마다 다를 수 있다.
3. 큰 sandbox node의 운영자들이 이미 상관을 문제로 인식한다.
   - Lambda: "multiple workloads from the same application ... behave as a single workload".
   - DSec: power-of-k의 목적에 "reducing interference among bursty RL environments"를 적었다.
   - MegaFlow와 RollArt는 큰 host에서의 경합을 이유로 구조를 바꿨다.

**가설에 불리한 사실**
1. 무작위로 퍼뜨리는 배치(DSec power-of-k, AgentENV round-robin, ROCK SPREAD)에서는 workload 종류가 섞여도 1/√n으로 평균화된다. 우리 trace로 계산하면 사용률 80%에서 ±2~4%p다(research-notes 2026-10-26 이후 논의).
2. 운영자들이 보고한 문제는 생성 경로, image, OOM, SMT 간섭이다. "개수가 같은데 오래 뜨거운 node"를 보고한 곳은 없다. 다만 재지 않았기 때문일 수 있다. 수치로 보인 공개 자료 자체가 없다. 가장 가까운 것은 Borg 2019 trace의 cell 사이 편차(중앙값에서 약 30%)다.
3. 불균형이 오래간다면, 리뷰어는 느린 재배치(분 단위 descheduler식, 또는 정지 중 이동)로 충분하다고 반론할 것이다.

**연구에 쓸 수 있는 빈 곳**
- "같은 개수 node 사이 실제 CPU 사용률 분포"를 잰 공개 수치가 없다. 새 RL trace로 합성하면 처음으로 보일 수 있다. 이때 locality(image hash, template), group CoW, 사용자 격리 같은 실제 배치 조건을 그대로 넣는다.
- 공개 시스템 어디에도 ms 단위 contention에 반응하는 메커니즘이 없다. 실행 중 재배치는 OOM(VenusRL) 말고는 없다. 이것은 사실이다.
- locality가 배치를 묶는 이유는 image·snapshot이 그 node에 있어야 하기 때문이다. CXL에 image를 두면 이 제약이 풀린다(앞의 image 논의와 연결).

## 5. 정정

- research-notes-20261026.md 2.8절에 적은 "Nebius는 cpuset으로 591.8초 중 29.1초를 줄였다"는 이번 조사에서 확인되지 않았다. Nebius 논문 App. D, 블로그, 별도 검색 어디에도 "cpuset"이 없었다. 출처를 찾기 전까지 철회한다.
- DSec의 "800 microVM 또는 3,200 container"는 논문 표현이 "at least ... stable operation"이다(§4.3). 관측 최대는 524 / 1,048이다.

## 6. 확인하지 못한 것

- DSec "least loaded"의 정확한 정의와 k 값
- Prime bin-packer의 점수
- MiniMax production runtime과 공개 CubeSandbox가 같은지, CubeSandbox 기본 가중치
- AgentENV 공개 scheduler가 production과 같은지
- Kimi k1.5/K2, GLM, Seed, KAT-Coder의 배치 정책(논문에 없음)
- ByteDance와 Amazon의 agentic sandbox 배치 논문

원자료(clone한 repo, 내려받은 논문 텍스트)는 이 세션의 scratchpad에만 있고 repo에는 넣지 않았다.
