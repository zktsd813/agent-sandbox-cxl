# Sandbox load balancing: 왜 문제가 안 보이고, 언제 깨지나 (2026-11-02)

질문 세 가지에 답한다.
1. 일반 cloud에서 생기는 load balancing 문제가 우리 측정에서는 왜 안 보이나?
2. AI 학습·추론에서 sandbox가 fine-grained하지 않게 되는 경우가 있나?
3. 다음 실험에서 CPU 말고 무엇을 같이 재야 하나?

근거는 우리 실행 데이터(4절), cloud 문헌 조사, AI sandbox 사례 조사다. 사례 중 핵심 수치(DSec, RollArt, MegaFlow, Anthropic)는 원문에서 다시 확인했다. 나머지는 조사 보고를 옮긴 것이고 "(2차)"는 원문이 아닌 기사로 확인한 것이다.

## 1. 요약

- **cloud hot spot의 네 가지 재료:**
  - (a) host의 큰 몫을 차지하며 오래 사는 tenant
  - (b) 서로 상관된 수요(하루 주기, 같은 고객, 동기화된 job)
  - (c) 꼬리가 긴 이질성(Borg 2019에서 상위 1% job이 CPU의 99%)
  - (d) 시분할할 수 없는 메모리
- **우리 CPU 상황에는 (a)와 (c)가 없고 (d)는 CPU에 해당하지 않는다.** sandbox 하나의 최대치가 1 vCPU = 노드의 1/15(DSec 크기 노드면 1/192)이고, 평균은 vCPU의 2~6%다. 상위 10% sandbox가 CPU의 34~40%를 써서, 혼자 노드를 차지하는 sandbox가 없다.
- **"fine-grained"는 시간이 아니라 크기의 문제다.** tool call 자체는 짧지 않다. test는 명령의 8%인데 CPU의 47%를 쓰고, 최대 57 CPU초다. 다만 sandbox 하나가 노드에 비해 작아 여러 개가 섞이면서 평탄해진다.
- **남는 변수는 (b) 상관이다.** 같은 GRPO group 안의 상관은 0에 가깝다. 하지만 노드 합계의 분산이 독립 가정의 1.4~6배다. wave 시작 같은 공통 시점이 원인으로 보인다. ×0.25에서 p99 수요가 용량(15코어)에 닿은 것도 이 몫이다.
- **목표 함수가 둔감하다.** rollout 시간의 대부분이 LLM 대기라, tool call이 느려져도 step 시간은 1~3%밖에 바뀌지 않는다. contention이 있어도 "문제"로 보이지 않는 이유 중 하나다.
- **AI 학습·추론에서 이 성질이 깨지는 곳은 CPU의 정상 상태가 아니라 다음 다섯 군데다.** 대부분 디스크, network, registry, 메모리, kernel lock이 CPU보다 먼저 막힌다.
  - 생성(setup) 폭주
  - 동기화된 검증(reward)
  - 메모리 순간 최대치
  - 무거운 환경 종류(desktop VM, ML, build)
  - 긴 세션

## 2. cloud에서 hot spot이 생기는 이유 (문헌)

| 출처 | 수치 | 원인 |
|---|---|---|
| Borg: the Next Generation (EuroSys'20) | 상위 1% job이 CPU 99.2%, 메모리 99.1%. job 크기의 C²가 23,000을 넘는다 | 꼬리가 긴 이질성. 큐 지연은 C²에 비례한다(Pollaczek–Khinchine) |
| Resource Central (SOSP'17, Azure) | VM 60%는 평균 CPU 20% 미만이지만 다수가 P95 80% 이상. 하루 넘게 사는 VM이 core-hour의 95% 이상 | 오래 살고, burst가 있고, 하루 주기가 있는 VM |
| Coach (ASPLOS'25, Azure) | VM의 약 70%가 하루 중 메모리에 최고·최저가 있다. 메모리는 "대체 불가"라 20초 간격으로 감시한다 | 시간에 따라 변하는 사용. 시분할할 수 없는 메모리 |
| Pond (ASPLOS'23) | 코어 75% 할당 시 메모리 6% stranding, P95 25% | 코어가 먼저 동나 메모리가 남음 |
| Heracles (ISCA'15) | 격리하지 않으면 LLC·DRAM 방해로 latency가 SLO의 300%를 넘는다 | 공유 LLC, 대역폭, SMT, 전력 |
| Firecracker (NSDI'20) | 상관없는 workload N개면 효율이 대략 √N으로 좋아진다. 같은 application, 같은 고객의 workload는 하나처럼 움직인다 | 통계적 다중화와 상관 |
| VMware DRS, Sandpiper (NSDI'07) | 과부하 해소에 수십 초~수 분(DRS 1~5분 주기) | 오래 사는 VM의 지속 과부하만 migration으로 푼다 |
| Google live migration (VEE'18) | 월 100만 회 이상. 정지 시간 중앙값 약 50 ms | 주로 유지보수용 |

- **통계적 다중화의 식:** tenant N개가 독립이면 합계의 변동계수는 CV/√N이다. 쌍 상관이 ρ면 CV·√((1+(N−1)ρ)/N)이고, N이 커지면 CV·√ρ로 수렴한다. 상관이 있으면 tenant를 아무리 늘려도 줄지 않는 바닥이 생긴다.
- **Firecracker 논문의 지적:** "같은 application은 하나의 workload처럼 움직인다." GRPO group이나 같은 step에 함께 풀린 rollout이 바로 그 경우다.
- **해결 방식의 구분:** LLC, 대역폭, SMT, 전력, network, 디스크 같은 공유 자원은 운영에서 로컬 격리로 푼다(µs~초 단위). migration과 재배치는 메모리, stranding, 전력 예산, 유지보수 같은 용량 문제에 쓴다(분~일 단위). Resource Central은 "migration은 붐비는 자원을 그대로 쓰고 traffic burst를 만든다. 오래 머물 곳에 놓는 편이 낫다"고 정리한다.

## 3. CPU runqueue 말고 불균형을 만드는 자원

| 자원 | 노드 사이 문제가 되는 조건 (아니면 로컬 격리 문제) | 재는 방법 | 이번 pilot 기록 |
|---|---|---|---|
| 메모리 용량, 회수 | 합계가 노드를 넘음. 시분할할 수 없어 OOM이나 이동이 필요 | PSI memory, pgscan, refault | `host.csv`, guest anon·cache(`sandboxes.jsonl`) |
| page cache, dirty writeback | 공유 image 파일, writeback throttling | memory.stat, PSI io | image cache 상주(`image-cache.csv`), PSI io |
| 메모리 대역폭, LLC | 노드 전체 포화가 이어짐. MBA는 일부만 줄임 | resctrl MBM, CMT | `resctrl.csv` |
| SMT | 로컬(core scheduling) | IPC | `turbostat.tsv` IPC |
| 주파수, 전력 | 노드 안: 바쁜 코어가 많으면 clock 하락. rack 전력 상한은 노드 사이 문제 | turbostat, RAPL | `turbostat.tsv` (bud05는 turbo가 꺼져 있음) |
| 디스크 IOPS, 대역폭, 용량 | 장치 포화. writable layer 증가로 용량 고갈 | diskstats, PSI io, df | `host.csv` (디스크, 여유 공간), guest 읽기·쓰기 |
| network, softirq, conntrack | NIC나 softirq 코어 포화. conntrack은 노드 전역 표 | softnet_stat, softirq 시간, conntrack 수 | `host.csv` (TAP 바이트, drop, softirq, conntrack) |
| kernel lock (netns, mount, mmap_lock, rtnl, KVM) | CPU 사용률이 아니라 sandbox 수와 생성·삭제 빈도에 따라 커짐 | perf lock, offcputime | 없음. 생성 폭주 실험 때 `perf lock contention`을 추가할 후보 |
| IRQ, host thread (VMM, vhost, kworker) | 회계되지 않는 CPU가 tenant를 밀어냄 | /proc/softirqs, runqueue 지연 | `host.csv` irq·softirq, `nodes.csv`(노드 전체 runqueue) |
| image pull, 생성 폭주 | step 경계에서 동시에 cold start | network rx, registry 지연, 준비 시간 | 준비 시간(`prepare_s`, boot). pull은 재현하지 않음 |
| PID, fd 한도 | 느려지는 게 아니라 실패 | pids.events, file-nr | 없음 |

`host.csv`는 1초마다 다음 항목을 누적값으로 남긴다(차분해서 쓴다). `sandbox.py`의 `host_sampler`가 pilot에서 항상 켜진다.
- PSI cpu, memory, io
- fault와 회수(pgscan, allocstall, compact_stall, refault)
- 실행 디스크(nvme0n1)의 섹터·시간
- sandbox TAP·bridge 바이트와 drop
- 노드 CPU의 irq·softirq jiffies
- conntrack 항목 수
- 실행 파일 시스템의 여유 공간

## 4. 우리 데이터: 얼마나 fine-grained한가

C2 segment 0(least, ×0.25)과 검증 실행(×1.0)의 sandbox 384개, 10 ms vCPU 기록이다.

| 항목 | ×1.0 | ×0.25 |
|---|---|---|
| sandbox 평균 CPU p50 / p90 | 0.019 / 0.068코어 | 0.058 / 0.184코어 |
| 1초 최대가 1코어에 닿은 sandbox | 63% | 66% |
| 노드 평균 수요 (15코어 중) | 1.0코어 | 3.7~4.1코어 |
| 노드 합계 1초 CV / p99 / 최대 | 0.9~1.0 / 4.1~4.2 / 6.6~7.1코어 | 0.74~1.0 / 11~14 / 14.5~14.9코어 |
| 독립 가정의 p99 (이항 근사) | 약 3.3코어 | 약 8.3코어 |
| 실제 분산 ÷ 독립 가정 분산 (1초) | 1.4~1.7 | 3.5~6.0 |
| 같은 GRPO group 안 1초 상관 | −0.02 (다른 group끼리 −0.02) | 0.01 (다른 group끼리 −0.06) |
| burst 길이 (CPU 가중) 중앙값 / p90 | 1.0 / 2.0초 | 1.25 / 3.3초 |
| 10초 넘는 burst의 CPU 몫 / 최장 | 3% / 27초 | 3% / 21초 |
| 상위 10% / 1% sandbox의 CPU 몫 | 34% / 5% | 40% / 6% |
| guest anon 최대 p50 / p90 / 최대 | 54 / 152 / 711 MiB | 44 / 223 / 1,883 MiB |
| writable layer 쓰기 p50 / p90 / 최대 | 11 / 255 / 1,094 MiB | 10 / 324 / 11,582 MiB |
| host 여유 메모리 최저 | 111 GiB | 96 GiB |

명령 종류별 CPU (×0.25, 명령 40,078개):

| 종류 | 명령 수 | CPU 몫 | 명령당 CPU초 p50 / p99 / 최대 |
|---|---|---|---|
| test (pytest 등) | 3,210 | 46.8% | 0.68 / 18.4 / 57.2 |
| python 실행 | 13,922 | 33.0% | 0.14 / 2.8 / 11.6 |
| pip 등 install | 627 | 13.6% | 2.02 / 19.2 / 31.3 |
| 검색·읽기 | 16,178 | 3.5% | 0.02 / 0.2 / 2.3 |
| 그 밖 | 6,141 | 3.1% | – |

- CPU 상위 1% 명령이 전체 CPU의 33%를 쓴다. 10 CPU초를 넘는 명령은 210개다. 가장 긴 명령은 60초 명령 timeout에 잘렸다.
- 1 vCPU라 pytest가 코어 하나만 쓴다. vCPU가 여러 개면 같은 test가 더 넓은 burst가 된다.
- **해석:**
  - sandbox 하나는 노드에 비해 작고(최대 1/15), 서로 독립에 가깝다. 그래서 ×1.0에서는 수요의 p99가 용량의 30% 아래다.
  - 독립 가정보다 큰 분산(공통 시점)이 ×0.25에서 p99를 용량까지 끌어올린다.
  - CPU보다 디스크(writable layer 최대 11.6 GB, 실제로 디스크 여유가 12 GiB까지 떨어짐)와 메모리 꼬리(1.9 GB)가 더 "coarse"하다.

## 4.1 사용률 7%는 overcommit이 부족해서인가

- **7%는 동기 계획의 시간 평균이다.** env 지연, step 꼬리(대부분 끝나고 몇 개만 남은 시간), step 사이 idle이 들어 있다. 살아 있는 sandbox 하나의 평균은 ×1.0에서 0.021~0.029코어다. 192개가 모두 살아 있다면 30코어의 13~19%다(비동기 실측: 코어당 5.33개에서 11%).
- **우리 메모리 설정 (이미 overcommit):**
  - guest RAM은 명목 2 GiB다(계획 trace는 모두 2048 MiB).
  - Firecracker guest 메모리는 익명 private mapping이라 guest가 실제로 건드린 page만 host가 잡는다. 미리 예약하지 않는다.
  - balloon의 free page reporting이 guest가 해제한 page(끝난 test process)를 host에 돌려준다. guest page cache는 남는다.
  - 예약량 기준 admission이 없다. pilot은 노드당 개수를 고정해 띄우고, 메모리 합계를 검사하지 않는다.
  - VM은 `numactl --membind=0`이다. NUMA 0이 차면 host page cache(image) 회수 → swap(16 GiB, swappiness 60) → OOM 순으로 간다. 지금까지 swap은 0이었다. KSM은 켜져 있지만 Firecracker 메모리가 병합 대상이 아니라 공유 page가 0이다.
  - 192개 × 2 GiB = 명목 384 GiB를 NUMA 0 128 GiB에 올리므로 명목 대비 3배 overcommit이다. 실측 최대 합계는 host anon 53 GiB(113개 동시)였고, NUMA 0 free는 최저 24.5 GiB였다(회수 가능 page cache 67 GiB 별도).
- **밀도 비교 (코어당 sandbox 수):**
  - 우리: 6.4 (SMT 끔).
  - DSec microVM: 최대 800 / 192코어 = 4.2 (관측 최고 524 = 2.7). SDK 예시가 sandbox당 4 GiB라, 800개면 명목 3.2 TB를 1.5 TB에 올리는 셈이다(약 2배 overcommit). DSec도 balloon free page reporting과 virtio-pmem DAX로 메모리를 overcommit한다(§8.4). 800은 "보여 준 운영 지점이지 한계가 아니다".
  - DSec container: 최대 3,200 / 약 188코어 = 약 17. 관측 최고 1,048 = 약 5.6.
  - DSec scale unit 최고 동시 수: 380K / 30K코어 = 12.7 (container와 microVM 혼합).
  - MegaFlow 운영 host의 CPU 사용률: 5~10%.
- **container와 microVM의 환산:**
  - DSec 수치로는 관측 최고에서 2 : 1(1,048 대 524), 보여 준 최대에서 4 : 1(3,200 대 800)이다.
  - 메모리로 보면 우리 microVM 하나의 host anon은 p50 약 180 MiB다. 그 안의 guest process 메모리는 44~54 MiB이고, 나머지는 guest kernel과 guest page cache다. container라면 process 메모리만 들고 page cache는 host에서 공유되므로, 메모리로는 microVM 1 ≈ container 3~3.5다.
  - CPU로는 둘이 거의 같다(가상화 overhead가 작다).
  - 그래서 "container 1 = microVM 1/2"는 DSec 관측 최고와 맞고, CPU 밀도를 비교하는 데 무난하다. 메모리가 밀도를 정하는 상황이라면 1/3~1/4가 더 맞다.
  - 1/2로 환산하면 DSec container 최대는 microVM 기준 코어당 약 8.5개다. 우리 6.4는 그 약 75%이고, DSec microVM 최대(4.2)의 1.5배다.
- **해석:** 운영에서 CPU 사용률이 낮은 것은 CPU를 덜 overcommit해서라기보다, 밀도를 메모리·디스크·생성 경로 같은 다른 자원과 운영 여유가 정하기 때문으로 보인다. 다만 DSec가 어느 자원 때문에 그 밀도에서 멈췄는지는 논문에 없다.
- **bud05 NUMA 0에 대입하면:**
  - 살아 있는 VM당 host anon 약 0.47 GiB(실측 최대 합계 기준)로 잡으면 약 250~300개, 코어당 8~10개에서 메모리가 먼저 찬다. 이때 ×1.0 CPU 사용률은 20~30%다. page cache 회수와 swap까지 쓰면 더 올라갈 수 있다.
  - DSec container 최대를 1/2로 환산한 코어당 8.5개(255개)는 메모리로는 들어간다.
  - writable layer 디스크는 밀도에 비례해 늘어, 지금 여유(약 57 GiB)로는 빠듯하다.
- **CXL과의 연결:** 메모리 pool로 메모리 한계를 풀면 밀도가 올라가고, 그때 CPU가 binding 자원이 된다. CPU 불균형 문제는 "메모리가 풀린 뒤"에 나타나는 2차 문제로 보는 것이 맞다.
- **×0.25 실험의 의미:** 코어당 6.4개에서 LLM을 4배 빠르게 하면 평균 사용률은 ×1.0의 코어당 약 19개와 비슷하다. 하지만 tenant 수가 1/3이라 합계가 덜 평탄하다(√N). 그래서 실제 고밀도보다 contention을 **크게** 잡는 쪽의 대리 실험이다.
- **SMT:**
  - thread가 2배가 된다. 실제 처리량은 microbenchmark에서 코어당 1.27배(메모리 위주)~1.53배(정수 연산)다.
  - 같은 밀도면 runqueue 대기는 줄지만, sibling이 바쁠 때 실행이 느려진다. 그래서 대기가 아니라 CPU 시간 증가로 나타난다.
  - CPU 포화 밀도를 1.3~1.5배 올릴 뿐, 메모리가 밀도를 정한다는 구도는 바꾸지 않는다.

## 5. AI 학습·추론에서 fine-grained가 깨지는 경우 (사례)

| 깨지는 방식 | 사례 (원문 확인 ✓) | 막히는 자원 |
|---|---|---|
| **생성·setup 폭주** | DSec ✓: job 하나가 sandbox 32K개를 요청하고, 초당 5,000개 이상 생성한다. 8,192개를 노드 10대에 한꺼번에 띄우면 eager pull로 60분이 넘고, 노드당 디스크 쓰기가 1,600 GB를 넘는다(on-demand는 약 700 GB). | 디스크, network, registry |
| | RollArt (OSDI'26) ✓: env 실패가 약 10 iteration에 한 번이다. 그때 env.reset이 rollout 시간의 78%이고, iteration이 366 → 513초가 된다. 원인은 수백 worker의 동시 image pull, container 시작의 CPU·디스크 경합, 디스크 I/O quota 고갈이다. | network, 디스크, CPU |
| | MegaFlow (Alibaba) ✓: 208코어 / 3 TB / 1 Gbps host에서 시작 시간이 task 1개일 때 1분, 동시 1,000개일 때 13분이다. 해결은 task당 8코어 / 16 GB 전용 instance다. 이때 host CPU 사용률은 5~10%다. | network, 초기화 경합 |
| | Modal: rollout 시작에 sandbox 수십만 개. kernel `rtnl` lock 경합으로 container 시작이 수십 초 걸렸다. | kernel lock |
| | RL360 (MBZUAI, 2차): Daytona sandbox 8,192개를 동시에 띄우면 image pull 경합과 pod 연쇄 실패가 생겨, 시간차를 둔 wave로 나눴다. Daytona 문서: 동시 생성 8개에서 2.8초, 256개에서 9.9초. | registry, network |
| **동기화된 검증(reward)** | rStar2-Agent: step당 tool call이 최대 45K개라 local 실행으로는 CPU가 넘친다. 그래서 전용 실행 service를 둔다. | CPU (상관된 burst) |
| | RollPacker: test timeout 30초에서 prompt의 약 5%가 timeout이 나고, reward 단계가 학습 시간의 약 22%까지 늘어난다. timeout을 늘린 이유 중 하나가 "실행 중 CPU 경합"이다. | CPU |
| **무거운 환경 종류** | ComputerRL·UI-TARS-2: OSWorld desktop VM은 CPU를 많이 쓰고, 동시 실행이 많으면 불안정해진다(수천 개). MLE-bench: 실행당 36 vCPU / 440 GB / GPU 1개, 24시간. | CPU 지속 사용, 메모리 |
| **여러 vCPU** | MegaFlow: SWE task당 8코어. Claude Code on the web: 약 4 vCPU / 16 GB. Vercel Sandbox: 기본 2 vCPU. SWE-bench 평가 harness는 worker 수를 min(0.75 × 코어, 24) 미만으로 두라고 하며 "더 많으면 경합으로 느려진다"고 경고한다. | burst 폭 (우리 이전 측정: 4 vCPU에서 contention 40배) |
| **빠른 추론** | Cognition SWE-1.5 (950 tok/s): "무시할 만하던 시스템 지연이 주된 병목이 됐다." OpenAI Codex-Spark (>1,000 tok/s): 왕복 overhead를 80% 줄였다. 반대 사례로 Amazon 연구는 env 대기가 GPU 유휴의 0.8~4.9%뿐이라고 보고했다. | duty cycle 상승 |
| **긴 세션 (제품)** | DSec ✓: sandbox 수명 중앙값 15~17분, p99 3시간 이상. E2B/Manus는 몇 시간짜리 세션, Lovable은 sandbox마다 dev server를 띄운다. | 지속 부하. 일반 cloud VM과 비슷해짐 |
| **메모리 순간 최대치** | Anthropic infra-noise ✓: Terminal-Bench 2.0에서 infra 오류가 1× 한도에서 5.8%, 3×에서 2.1%, 상한 없음에서 0.5%다. 원인은 순간 메모리 증가로 인한 OOM kill이다. 점수는 상한 없음이 1×보다 6 pp 높다(1×와 3× 사이는 잡음 수준). SWE-bench는 5× RAM에서 +1.54 pp. | 메모리 (시분할 불가) |
| **디스크·image 용량** | SWE-bench image cache 약 2,000 GB(layer 중복 제거로 67 GiB). SWE-bench + SWE-Gym은 25 TB 이상. DSec는 일주일에 workspace와 snapshot이 130 TB를 넘는다. | 디스크 |
| **straggler** | MiniMax Forge: rollout 시간이 초에서 시간까지 퍼져, 엄격한 FIFO는 straggler에 묶인다. Kimi K2: 무거운 환경은 전용 service로 빼고, 긴 꼬리 task는 partial rollout으로 다음 iteration에 넘긴다. | 동기 step의 꼬리 |

정리:
- 정상 상태의 CPU는 어디서나 fine-grained다(MegaFlow host 5~10%, Daytona duty cycle 약 11%).
- 문제는 시간 축에서 **모두가 같은 일을 하는 순간**(생성, 검증)에 생긴다.
- 자원 축에서는 **시분할할 수 없는 자원**(메모리, 디스크, image)과 **노드 전역 자원**(network, registry, kernel lock)에서 생긴다.

## 6. 실험과 hand-off 주장에 주는 의미

**다음 실험에서 같이 볼 것**

DSec matrix에 그대로 얹을 수 있는 것:
- `host.csv`, `resctrl.csv`, `turbostat.tsv`로 자원별 압력을 같이 기록한다(이미 구현).
- 분석에 두 가지를 더한다. (1) 노드 합계의 분산 ÷ 독립 가정 분산을 wave 시작 후 시간대별로 나눈다(공통 시점의 몫). (2) 명령 종류별 CPU와 느려짐을 BE 조건마다 나눈다(test, install이 간섭에 더 민감한가).

fine-grained를 일부러 깨는 조건(구현이 필요한 것):

| 조건 | 방법 | 구현 상태 |
|---|---|---|
| 생성 폭주 | 지금은 env 생성 시간을 대기로만 넣는다. 부팅 때 image를 실제로 읽고 쓰는 일(pull·unpack 흉내)이나 기록된 install 명령을 먼저 실행한다. | 새로 구현 |
| 동기 검증 | 제출 뒤 task의 test 명령을 실행하고, group이나 step 단위로 동시에 몰린다. | 새로 구현 (SWE-rebench image의 test 명령 확인 필요) |
| 여러 vCPU | `--vcpus 2`, `4`. pytest가 여러 코어를 쓰게 할지(`-n`)는 따로 정한다. | 옵션 있음 |
| 빠른 추론 | `--gap-scale 0.1`. | 옵션 있음 |
| 긴 명령 | 60초 명령 timeout을 늘린다(지금은 무거운 test가 잘린다). | 설정 변경 |

**hand-off(CXL)에 주는 의미**
- **CPU runqueue 불균형은 hand-off의 동기로 약하다.** 크기가 작고 상관이 약해 노드 안에서 평탄해진다. 공통 시점의 몫은 시작 시각을 엇갈리게 해서 줄일 수 있다.
- **노드 사이 불균형이 실제로 생기는 자원은 메모리, 디스크, network, 생성 경로다.** 이 중 CXL과 직접 닿는 것은 메모리다.
  - 순간 최대치가 OOM과 reward 잡음을 만든다(Anthropic).
  - 메모리는 시분할이 안 돼 최대치에 맞춰 잡아야 한다(Coach, Pond).
  - CXL pool로 순간 최대치를 흡수하면 노드마다 최대치만큼 여유를 둘 필요가 줄어든다. 이전 pooling 분석(평균 밀도 +2~9%)은 평균을 봤다. **OOM과 infra 오류율 같은 꼬리 지표**로 다시 볼 가치가 있다.
- **co-location 간섭(LLC, 대역폭)은 같은 socket 안에서 못 피한다.** 노드 사이로 옮길 동기가 될 수 있다([dsec-interference-design.md](dsec-interference-design.md)).
- **생성 폭주는 image를 CXL에 두고 여러 host가 공유하면 줄일 수 있다.** pull과 unpack 대신 매핑한다(이전 CXL image 공유 논의와 연결된다). 이는 hand-off가 아니라 CXL image 공유의 동기다.

## 7. 출처

- cloud: [Borg 2019](https://research.google/pubs/borg-the-next-generation/), [Resource Central](https://www.microsoft.com/en-us/research/publication/resource-central-understanding-predicting-workloads-improved-resource-management-large-cloud-platforms/), [Coach](https://arxiv.org/abs/2501.11179), [Pond](https://arxiv.org/abs/2203.00241), [Protean](https://www.usenix.org/conference/osdi20/presentation/hadary), [Heracles](http://csl.stanford.edu/~christos/publications/2015.heracles.isca.pdf), [Caladan](https://www.usenix.org/conference/osdi20/presentation/fried), [CPI²](https://research.google/pubs/cpi2-cpu-performance-isolation-for-shared-compute-clusters/), [Firecracker](https://www.usenix.org/conference/nsdi20/presentation/agache), [Sandpiper](https://www.usenix.org/legacy/event/nsdi07/tech/full_papers/wood/wood.pdf), [Decade of Wasted Cores](https://people.ece.ubc.ca/sasha/papers/eurosys16-final29.pdf), [SOCK](https://www.usenix.org/conference/atc18/presentation/oakes), [Slacker](https://www.usenix.org/conference/fast16/technical-sessions/presentation/harter), [TMO](https://par.nsf.gov/servlets/purl/10332477)
- AI sandbox: [DSec](https://arxiv.org/html/2609.22978v1), [RollArt](https://arxiv.org/html/2512.22560v1), [MegaFlow](https://arxiv.org/html/2601.07526v2), [Modal 1M sandboxes](https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds), [Daytona RL cookbook](https://www.daytona.io/docs/guides/reinforcement-learning/hud-rl-cookbook), [rStar2-Agent](https://arxiv.org/html/2508.20722), [RollPacker](https://arxiv.org/html/2509.21009), [ComputerRL](https://arxiv.org/html/2508.14040v2), [UI-TARS-2](https://arxiv.org/pdf/2509.02544), [MLE-bench](https://github.com/openai/mle-bench/), [Anthropic infrastructure noise](https://www.anthropic.com/engineering/infrastructure-noise), [Large Language Monkeys](https://arxiv.org/html/2407.21787), [Cognition SWE-1.5](https://cognition.com/blog/swe-1-5), [Codex-Spark](https://openai.com/index/introducing-gpt-5-3-codex-spark/), [Amazon environment bubble](https://www.amazon.science/publications/characterizing-and-provisioning-the-environment-bubble-in-agentic-rl-llm-rollout), [MiniMax Forge](https://www.minimax.io/blog/forge-scalable-agent-rl-en-1779896141), [Kimi K2](https://arxiv.org/html/2507.20534), [Epoch SWE-bench images](https://epoch.ai/blog/swebench-docker), [SWE-bench docker guide](https://www.swebench.com/SWE-bench/guides/docker_setup/)
