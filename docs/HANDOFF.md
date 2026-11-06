# HANDOFF: CXL 기반 Agentic Sandbox 연구 (claude.ai 논의 정리)

작성일: 2026-10-19. claude.ai에서 진행한 설계·실험 계획 논의를 Claude Code 작업용으로 정리한 문서다. 프로젝트 `CLAUDE.md`가 이 파일을 import하므로 매 세션 자동으로 읽힌다.

> **2026-11-03 현재 상태.** 문서 색인은 [docs/README.md](README.md)다. CPU 노드 사이 contention은 비용이 작았다. sandbox CPU가 critical path에서 얇기 때문이다([실험 계획 v2](experiment-plan-tail-placement.md) 8장). 다음 방향은 GPU 노드의 남는 CPU에 sandbox를 올리고, 학습이 CPU를 원하면 hand-off로 CPU만 비우는 시나리오다([GPU 노드 harvest 설계](gpu-node-harvest-design.md)). 아래 0~10장은 2026-10-19 기준 계획이다.
>
> **2026-11-05.** Docker로 15코어 pool 2개 사이 실제 이동을 쟀다([pool 이동 실측](results/pool-mover-20261105/README.md)). 배치 때 burst를 몰라 한 노드만 넘치는 때가 많다. 사용률 40~83%의 섞인 부하에서 60 ms 이동은 공유 pool 대비 대기 차이의 61~96%를 되찾고, 5초 이동은 10~14%만 되찾는다. 다만 명령·rollout 수준 이득은 작다(K=120 CPU 가중 −6%, rollout p99 1.58 → 1.50). 사용률 95%에서 처음 이동 규칙이 안 들은 것은 문턱 탓이었고, 고쳐도 명령 시간은 3~5%만 준다.
>
> **2026-11-06.** 11-05 실험은 부하를 실제에서 정하지 않고 경합이 생기도록 골랐다(빌드가 무거운 다언어 trace, Gemini API 대기, CPU 제한 없음). 같은 기준으로 공개 GRPO rollout(Python)은 sandbox당 0.04코어라, 그 사용률에 닿으려면 메모리 한도를 넘는 밀도가 필요하다. GPU 없이 공개 trajectory와 iter2 RL serving 기록으로 workload를 넓히고, LLC 경합도 함께 보는 12시간 계획을 세웠다([workload-diversity-plan.md](workload-diversity-plan.md)).

같은 날 repo 상태와 대조해 사실이 다른 부분을 고쳤다. 고친 곳은 **[repo 대조]**로 표시했다. 실행 방법과 설정은 [README](../README.md), 완료한 측정은 [DRAM/CXL 결과](results/numa-immediate-20261014/README.md)가 기준이다.

---

## 0. 요약

- **문제**: agentic RL rollout의 sandbox(microVM)는 tool call마다 CPU burst가 짧고 예측하기 어렵게 생긴다. 노드 안 contention이 rollout을 늦추고, synchronous RL에서는 straggler가 step time을 정한다.
- **핵심 주장**: 이 ms 단위 contention은 VM migration으로 회수할 수 없다. CXL 공유 메모리 위에서 VM을 싸게 넘기는 VM hand-off와, tool call 단위의 동적 tier 분류로 회수할 수 있다.
- **최종 설계**: CXL tax가 작은 sandbox만 CXL에 상주시키고(메모리 + EPT), 노드 간에는 약 20KB 상태만 직렬화해 넘긴다(목표 10ms 미만). tax가 큰 sandbox는 로컬 DRAM에 고정하고, tool call 사이(LLM wait)에 DRAM ↔ CXL promotion/demotion으로 부류를 바꾼다.
- **지금 할 일**: 단일 bare-metal 서버에서 motivation 실험(Phase A, B 대부분, C simulator)을 먼저 끝낸다. 비일관 CXL multi-host 하드웨어는 Phase D, E3, F에만 필요하다. 구체 작업은 8장.
- **[repo 대조] 현재 repo 상태**: 264개 mini-SWE-agent trace를 Docker 컨테이너로 재생하는 harness(`harness/trace-replay/experiment.py`, `analyze.py`)가 있다. DRAM(node 0)과 CXL(node 2)에서 한 번씩 재생해 trace 단위 latency, MPKI, CPU, 메모리를 측정했다(1.2). microVM, 1ms 계측, 반복·순서 무작위 실행은 아직 없다.

---

## 1. 배경

### 1.1 원래 슬라이드(20261016.pptx, 4~12쪽)의 주장

- Motivation 1: sandbox는 동시에 여러 곳에서 실행되지 않는다(single writer). 비일관 CXL 공유 메모리에 적합하다.
- Motivation 2: 대부분 sandbox는 MPKI가 낮아 CXL 메모리에서 overhead가 작다. 일부는 로컬 메모리가 필요하다.
- Motivation 3: CPU burst가 예측 불가능하고 VM migration은 비싸다. migration이 끝날 즈음 burst가 다른 곳으로 옮겨가 있다.
- Abstraction: 여러 물리 노드를 CXL 공유 메모리로 묶은 virtual node.
- Design 1: cache coherence 때문에 한 VM의 vCPU는 한 물리 노드에서만 실행한다.
- Design 2: switch 대상 VM의 메모리는 전부 CXL에 둔다(mapping consistency).
- Design 3: VM을 Free / DRAM-bound로 나누고, 필요하면 tool call 사이 LLM wait을 이용해 옮긴다.
- Design 4: rollout 간 유사성을 이용한 dedup(KSM, Intel DSA).

### 1.2 기존 CXL tax 측정의 문제 (슬라이드 5쪽)

**[repo 대조]** 수치는 [결과 README](results/numa-immediate-20261014/README.md)와 `summary.json`, `paired-traces.csv`로 다시 확인했다.

- 대상: 264개 trace를 DRAM, CXL에서 한 번씩(총 528회). 53쌍 제외(timeout 50쌍: 양쪽 39, CXL에서만 11. 종료 코드 불일치 3). 비교 가능한 쌍은 211개.
- trace 단위 CXL/DRAM latency 비율: 중앙값 +11.2%, 기하평균 +13.2%, 시간 합계 +17.4%, p95 +42.8%, p99 +53.7%, 최대 +79.8%. 211개 중 66개(31%)가 20% 이상 느림. 최소 비율 0.63(−37%).
- 명령 단위(eligible 10,807개) 비율: 중앙값 +1.8%, p95 +21.5%, 최대 3.54배. 단 latency가 Docker exec 시작~종료라 Docker 제어 경로가 포함된다.
- **MPKI는 측정했다.** workload cgroup 전체의 user+kernel instructions, cycles, LLC reference/miss, retired load L3 miss를 perf로 쟀고 전부 유효했다. instructions 가중 LLC MPKI는 DRAM 1.54 / CXL 1.48이다. trace별 DRAM LLC MPKI는 중앙값 1.08, p95 2.65, 최대 4.32다(`paired-traces.csv`).
- 남는 문제:
  - 슬라이드 그래프는 elapsed time slowdown CDF라 MPKI를 직접 보여주지 않는다.
  - MPKI는 trace 단위 값이다(perf 기본 간격 1초). tool call 단위 MPKI와 tax의 관계는 아직 없다.
  - 조건마다 1회, DRAM → CXL 고정 순서, 전역 cache를 비우지 않고 다른 호스트 작업을 유지했다. −37%까지 나온 왼쪽 꼬리는 noise로 보인다.
  - MPOL_BIND로 payload 익명 메모리만 고정했다. 공유 파일 cache는 DRAM에 남을 수 있다.
- 결론: "little overhead"를 지지하기에는 부족하다. MPKI가 낮다는 점은 trace 단위로 확인됐지만 tax 분포가 넓다. 반복 측정, 순서 랜덤화, tool call 단위 재측정이 필요하다(Phase A4).

---

## 2. 최종 설계

### 2.1 구성 요소

| 요소 | 내용 |
| --- | --- |
| CXL 상주 VM | tax가 작은 sandbox. guest 메모리와 EPT를 CXL에 둔다. 노드 간 이동 때 둘 다 재사용한다. |
| VM hand-off (기본) | vCPU 레지스터, MSR, XSAVE, LAPIC, device 상태 등 약 20KB를 직렬화해 CXL mailbox로 넘긴다. 목적지에 미리 띄운 VMM shell이 역직렬화하고 기존 EPT를 입양한 뒤 재개한다. 목표 downtime 10ms 미만. |
| in-place 변형 (선택) | VMCS와 host 측 구조까지 CXL에 두고 detach/attach한다. 이전 구현에서 약 500us 확인(로컬 context switch는 1~4us). Phase C가 필요하다고 판정할 때만 진행(D7). |
| 로컬 DRAM VM | tax가 큰 sandbox. 노드에 고정되고 tax를 내지 않는다. |
| promotion/demotion | tool call 사이(LLM wait)에 노드 안에서 DRAM ↔ CXL로 VM 메모리를 옮겨 부류를 바꾼다. |

용어 규칙: 노드 간 이동은 "hand-off", 노드 안 tier 이동은 "promotion/demotion". "migration"은 비교 대상인 기존 VM migration에만 쓴다.

### 2.2 hand-off 순서

1. source vCPU 정지
2. 상태 직렬화(KVM_GET 계열) → CXL mailbox에 쓰고 write-back
3. source의 guest dirty line과 EPT(A/D bit 포함) write-back
4. 노드 간 알림
5. 목적지에서 invalidate
6. 역직렬화(KVM_SET 계열)
7. EPT 입양(EPT root 설정, INVEPT)
8. device 상태 로드
9. 재개

### 2.3 반드시 지켜야 할 불변식

- **HPA 동일성**: 모든 서버의 CXL window base(CEDT CFMWS), HDM decoder 설정, devdax offset이 같아야 한다. 다르면 EPT 입양이 성립하지 않는다.
- **devdax 노출**: pool을 system RAM으로 online하지 않는다. 각 서버 커널이 독립적으로 할당해 서로 덮어쓰기 때문이다.
- **single writer / 소유권**: VM의 메모리와 EPT는 현재 소유 host만 쓴다. VM별 ownership word(소유 host, 세대 번호)를 둔다. 장치가 서버 간 atomic을 보장하지 않으면 네트워크 위 중앙 관리자가 소유권 변경을 직렬화한다.
- **coherence**: detach 때 write-back(guest dirty line, EPT A/D bit, mailbox), attach 때 invalidate와 INVEPT. 비일관 CXL에서는 correctness 조건이다(성능 최적화가 아님).
- **EPT 불변성**: devdax라 host mm이 swap, page migration, reclaim을 하지 않으므로 입양한 EPT에 mmu notifier 이벤트가 생기지 않는다. 이것이 정적 EPT 재사용의 근거다.
- **동일 구성**: 모든 서버가 같은 CPU 모델, microcode, 커널, KVM 모듈이어야 한다.
- **device 연속성**: tap, socket fd, conntrack, block fd 같은 host 쪽 절반은 CXL에 둘 수 없다. 제어 채널(vsock 또는 CXL ring), network(drop vs 버퍼링), writable layer(CXL 또는 공유 스토리지) 처리를 설계해야 한다.

**[repo 대조]** 현재 단일 서버의 CXL은 system RAM NUMA node 2(`ZONE_MOVABLE`)로 쓴다. 단일 서버 A4에는 맞지만, 위의 devdax 노출 조건과는 다른 구성이다. 단일 서버 M3·M4에 CXL DAX 파일이 필요하면 별도 구성이 있어야 한다.

### 2.4 two-class 분류 기준 (주의)

- 기본 기준은 tool call 단위 CXL tax지만, 실제로는 **"tax 대 이동성의 가치"**로 봐야 한다. CXL 배치는 "옮길 수 있는 선택권"을 사는 것이다.
- contention을 풀려면 노드마다 이동 가능한 VM이 몇 개만 있으면 된다(이동성 예비분).
- 최대 위험: CPU를 많이 쓰는 tool call이 tax도 크다면 옮길 VM이 없다. Phase A4의 (CPU 수요, tax) 결합 분포로 가장 먼저 확인한다.

---

## 3. 설계 결정 이력

| 결정 | 버린 대안 | 이유 |
| --- | --- | --- |
| 기본 메커니즘 = 직렬화 hand-off | in-place(VMCS·host 구조 공유)를 기본으로 | VMCS 포맷이 구현 의존, host-state 필드, VMCLEAR semantic 등 위험이 큼. 직렬화는 표준 KVM API를 쓰고, 같은 host에서 Orthus와 ByteDance KVM live upgrade가 검증한 경로다. in-place는 D7로 남김. |
| 이름 = VM hand-off | "cross-node context switch" | ms 단위에서 context switch(1~4us)와 비교되면 불리하다. |
| two-class | 모든 VM을 CXL에 | 모든 VM이 CXL tax를 내게 된다. |
| promotion/demotion | 노드 안 이동도 "migration" | 논문 motivation이 "VM migration으로는 안 된다"라 용어가 섞이면 혼란. |
| 메커니즘 비교 = 같은 CXL 위 M3/M4/M4+P | full VM migration(M1)만 비교 | M1과 비교한 이득 대부분은 CXL 공유 메모리 자체에서 나온다. 리뷰어는 `x-ignore-shared`나 CXL 위 restore를 들고 온다. M1은 하드웨어 수준 비교(F0/F1)용. |
| device 연속성 = 비용 항목 | "끊김 없음"을 기능적 차별점으로 | 끊김 semantic을 알면 재접속으로 해결 가능(DSec도 snapshot 기반 pause/resume을 운영). 단 외부 TCP는 남는다: tail loss는 RTO(최소 200ms)로 증폭되고, host conntrack이 사라지면 RST. CPU contention의 주원인이 패키지 설치라 옮기고 싶은 VM이 network 활성인 경우가 많다. |
| 하드웨어 = 비일관 CXL multi-host (D, E3, F) | 단일 서버 nested emulation(NUMA 2개, 노드마다 6코어·32GB L1 VM 2개, 안에서 microVM) | nested에서는 L0가 shadow EPT02를 다시 채워서 EPT 입양 효과가 가려진다. VMX 명령이 trap되고, guest의 WBINVD는 사실상 no-op, 소켓 간 메모리는 coherent라 stale line(correctness)을 재현 못 한다. 같은 소켓 L1끼리는 LLC를 공유해 warm-up이 낙관적이다. |
| motivation = 단일 bare-metal 서버 | motivation도 nested L1 VM으로 | hand-off가 필요 없는 실험은 multi-host가 필요 없다. multi-host 동작은 simulator가 담당한다. nested는 I/O 많은 tool call의 burst를 부풀리고 가짜 contention을 만든다. |

---

## 4. 예상 리뷰어 질문과 답

**Q1. 그냥 VM migration 하면 되지 않나?**
메커니즘 계열은 migration이 맞다고 인정한다. 쟁점은 시간 스케일이다. burst와 contention 구간이 migration 지연보다 짧으면 migration이 끝날 때 burst도 끝나 있다. 근거는 Phase A3의 잃은 CPU 시간 가중 contention 구간 CDF와 잔여 시간, Phase C의 회수율 곡선(Fig 4)이다. 또 burst 중인 VM은 dirty rate가 높아 pre-copy가 수렴하지 않거나 auto-converge/dirty-limit throttle로 오히려 느려지고, migration 작업이 이미 과부하인 출발 host의 CPU를 쓰며, 로컬 DRAM 기반 migration은 목적지 메모리 여유가 필요하다(production에서는 메모리가 density 병목인 경우가 많다).

**Q2. CXL에 메모리를 두면 migration이 싸지는 건 이미 알려진 것 아닌가?**
맞다. 2023 LSFMM에서 CXL 공유 메모리로 VM을 migration하는 발표가 있었고, VM이 아예 CXL에 살면 되지 않느냐는 질문도 나왔다. 그래서 같은 CXL 위의 M3/M4/M4+P와 비교한다. 차이는 (1) EPT 입양, (2) VMM·KVM 객체 재생성 회피, (3) 비일관 CXL에서 안전하게 넘기는 coherence/소유권 프로토콜이다. 서사: "같은 host 안에서만 쓰이던 live upgrade 기법(shared memory로 상태 전달, EPT 상속)을 CXL로 host 간에 일반화하고, burst 대응 scheduling primitive로 쓴다."

**Q3. tool call 경계(LLM wait, VM idle)에서 미리 옮기면 되지 않나?**
가장 강한 반론이다. 본인 Design 3과 같은 메커니즘이라 막지 못하면 hand-off 단계가 필요 없어진다. 대응: (a) tool call 시작 시점 정보로 burst 예측 상한(A5), (b) contention은 여러 VM burst가 겹치는 공동 사건이고 겹침 시점은 다른 VM의 LLM 응답 시점에 좌우됨(A3), (c) oracle 경계 정책(P3)으로도 남는 손실(Phase C).

**Q4. 노드 안 QoS(overcommit + SCHED_IDLE + core scheduling, DSec 방식)로 충분하지 않나?**
QoS는 노드 안에서 CPU를 나눌 뿐 코어를 만들지 못한다. 노드 총수요가 용량을 넘는 correlated burst(같은 GRPO group이 동시에 test phase 진입 등)를 보여야 한다. F0와 P0가 이 baseline이다.

**Q5. 1GB huge page로 매핑하면 EPT 재구축은 거의 공짜 아닌가?**
맞다. 그래서 4KB가 강제되는 조건을 함께 보인다. dedup(KSM)은 4KB 단위로 병합하고, dirty tracking(PML, flush 범위 제한에 필요)은 huge page를 쪼갠다. D4 ablation에서 page size별로 보인다. 4KB에서 격차가 작으면 EPT 입양은 구현 세부로 내린다.

**Q6. attach할 때 KVM_PRE_FAULT_MEMORY로 EPT를 미리 채우면?**
M4+P baseline이다. 입양은 상수 비용, prefault는 매핑 크기에 비례(vCPU ioctl이라 병렬화 가능). resume 전에 하면 downtime, 뒤로 미루면 결국 fault.

**Q7. 왜 page 단위 tiering(TPP, Memtis 계열)이 아니라 VM 통째로인가?**
일부 page가 로컬에 있으면 HPA 동일성과 EPT 재사용이 깨지고, hand-off 전에 로컬 부분을 write-back해야 해서 비용이 로컬 working set에 비례한다. E6에서 중간 지점 하나를 보여 trade-off로 설명한다.

**Q8. 생성 시점에 한 번 분류(Pond 방식)하면 되지 않나?**
같은 sandbox 안에서 tool call마다 tax가 얼마나 달라지는지(within 분산)와 sandbox 간 분산(between)을 비교한다(A4). within이 작으면 정적 분류로 단순화한다.

**Q9. 작은 host라서 생긴 contention 아닌가?**
contention 빈도와 길이는 host 코어 수에 크게 좌우된다. A3를 서버가 허용하는 최대 코어 수로 돌리고 코어 수를 sweep한다. simulator에서도 host 코어 수를 축으로 둔다.

**Q10. M4와 제안 방식의 차이는?**

| 항목 | M4 (공정 버전) | 제안 (VM hand-off) |
| --- | --- | --- |
| guest 메모리 | CXL DAX 파일을 MAP_SHARED로 매핑, 복사 없음 | 같음 |
| coherence 처리 | 공정성을 위해 같은 write-back·invalidate를 넣음 | 같음 |
| 메모리 dump | snapshot 생성 시 메모리를 쓰지 않도록 Firecracker 수정 필요 | 해당 없음 |
| 상태 전달 | Firecracker state file | 약 20KB, CXL mailbox |
| 목적지 준비 | 미리 띄운 Firecracker 프로세스가 LoadSnapshot 때 KVM VM·vCPU·memslot·device 생성 | 미리 띄운 VMM shell(KVM 객체도 미리 생성 가능) |
| EPT | 빈 EPT에서 lazy 재구축(M4+P는 prefault) | 기존 EPT 입양 |
| 재개 직후 비용 | page마다 EPT violation, VMM 쪽 매핑 fault | cache·TLB cold miss만 |
| device | snapshot 생성 시 vsock 리셋, network 보장 없음 | 설계하면 유지 가능 |

stock Firecracker restore는 메모리 파일을 MAP_PRIVATE로 매핑하고, full snapshot은 guest 메모리 전체를 쓰며, LoadSnapshot은 VM 구성 전에만 가능하다. 따라서 M4는 "CXL 공유 메모리에 맞게 고친 restore 경로"로 명시한다. 격차는 M4에서 하나씩 바꿔(KVM 객체 사전 생성 → EPT 입양 → device 연속성) 3분해해 보고한다. 또 M4-naive(flush 없음)는 비일관 CXL에서 stale 데이터로 틀린 결과가 나는 것을 보여 coherence 프로토콜 자체를 contribution으로 만든다. M4-fair(flush 포함)는 EPT 입양과 VMM 경로의 순수 차이를 잰다.

---

## 5. Prior work 위치

| 연구 | 핵심 | 제안과의 차이 | 대응 실험 |
| --- | --- | --- | --- |
| 특허 US 11875174 | warm upgrade 후 새 VM이 이전 VM의 EPT base를 넘겨받아 재구축 회피 | 같은 host | D4 |
| Orthus (ASPLOS'19, Alibaba) | VM grafting. 메모리 공유로 GPA→HPA 유지, 상태 save/load, downtime 약 30ms(libvirt 12ms, VGA 4ms, 16 vCPU 4ms, virtio-net 3ms, balloon 3ms). VT-x는 대부분 stateless, VM 상태는 VMCS에 있다고 서술 | 같은 host, 새 KVM 인스턴스 | D1 breakdown 대조 |
| KVM live upgrade (ByteDance, KVM Forum 2020) | VM 상태 저장·로드를 shared memory로 동시 진행, vCPU downtime 11~38ms(8vCPU/16GB~88vCPU/350GB, 메모리 크기와 거의 무관) | 같은 host. 직렬화 경로와 가장 가까움 | D1 |
| QEMU live update (Oracle) | 같은 host에서 QEMU 교체, guest RAM 같은 HVA 유지, 정지 100ms 미만, VM은 재생성 | 같은 host | B 참고치 |
| CXLfork (ASPLOS'25) | 프로세스 page table을 CXL 물리 주소로 rebase해 host 간 fork | 프로세스, read-only clone | 관련 연구 |
| TrEnv (SOSP'24, 확장판 TrEnv-X) | host별 사전 구축 page table(mm-template). VM은 lazy. EPT 사전 구축은 future work | 입양이 아니라 재구축 | D4 |
| Aquifer (arXiv 2606.24079) | 비일관 CXL 2.0 MHD에서 microVM snapshot 공유, ownership 프로토콜, hot page 사전 복사, emulation 평가 | read-only, 복사 방식 | D5, 실제 HW |
| KVM_PRE_FAULT_MEMORY | vCPU 실행 전 stage-2 EPT를 채우는 ioctl | eager 재구축 | M4+P, D4 |
| Firecracker snapshot | MAP_PRIVATE lazy 로딩, 새 프로세스에서 VM 재생성, vsock 리셋 | device 리셋, EPT 재구축 | M2, M4, D6 |
| LSFMM 2023 CXL VM migration 논의 | CXL 공유 메모리로 VM migration | 같은 메커니즘 계열 | M3, Phase C |
| Pond (ASPLOS'23) | VM 단위 로컬·pool 메모리 배분을 ML로 예측, remote NUMA로 CXL emulation | 정적, tool call 단위 아님 | F5, A4 |
| DSec (DeepSeek, arXiv 2609.22978) | power-of-k 배치 + SCHED_IDLE + core scheduling, migration 없음 | 노드 간 재배치 없음 | F0, P0 |

결론: "실행 중인 VM / EPT·VMCS가 CXL에 있음 / host 간 재구축·prefault 없이 넘김 / 비일관 CXL 위 read-write 소유권 이전" 네 조건을 동시에 만족하는 선행 연구는 찾지 못했다(검색이 완전하지 않으므로 2025~2026 ASPLOS/OSDI/SOSP/EuroSys/ATC/VEE, 최근 arXiv, GiantVM 같은 분산 hypervisor를 추가 확인할 것). CXL이 필수인 이유: 목적지 하드웨어 page walker가 같은 HPA에서 EPT page와 guest page를 직접 load할 수 있어야 하는데 RDMA로는 불가능하다.

DSec 관측(생성 burst, 짧은 CPU 활동과 응답 대기의 교대, 약 90% sandbox의 평균 사용률 5% 이하)의 해석 범위는 [timing-traces.md](timing-traces.md)에 정리했다.

---

## 6. 실험 환경 매트릭스

| 실험 | 필요한 것 | 환경 |
| --- | --- | --- |
| A1 tool call 프로파일 | host 1대 | bare-metal 서버 1대, microVM 직접 실행 |
| A2 LLM wait | trace만 | 오프라인 |
| A3 contention | host 1대(크기가 중요) | bare-metal 1대, 코어 수 sweep |
| A4 CXL tax | CXL 메모리가 붙은 host 1대 | 슬라이드 5를 잰 서버(아래), microVM 메모리를 CXL 노드에 바인딩 |
| A5 예측 | A1, A4 데이터 | 오프라인 |
| B baseline | host 2개처럼 보이는 것 | 같은 서버 안 프로세스 간 이동: QEMU localhost migration + `max-bandwidth`로 NIC 흉내(10/25/100Gbps sweep), Firecracker snapshot→다른 프로세스 restore, `x-ignore-shared`. flush 비용은 실측 기준값을 더함 |
| C simulator | A, B 결과 | 오프라인 |
| E1, E2, E4, E5 | host 1대 + CXL | bare-metal 1대 |
| D, E3, F, M4-naive 시연 | 비일관 CXL multi-host | 실제 하드웨어 |

**[repo 대조] 슬라이드 5를 잰 서버(Niagara)**: Intel Xeon Gold 6548Y+, 2소켓, SMT off(thread per core 1). NUMA node 0(CPU 0-31, DRAM 약 128GB), node 1(CPU 32-63), node 2(CPU 없는 CXL 메모리). 기존 측정은 controller CPU 0-1, workload CPU 2-31을 썼다. node 2는 `ZONE_MOVABLE`만 제공해서 cpuset mems를 `0,2`로 두고 payload를 MPOL_BIND helper로 고정했다. 2026-10-19 확인 시 `numactl --hardware`의 node 2 size가 0MB였다. A4 재측정 전에 CXL online 상태를 다시 확인해야 한다. CPU governor는 `schedutil`이라 주파수가 고정되지 않았다.

**[repo 대조]** 기존 측정은 microVM이 아니라 Docker 컨테이너(trace마다 새 컨테이너, 명령마다 새 bash)에서 했다. vCPU thread가 없으므로 A3의 runqueue 지표는 컨테이너 프로세스 단위로 대체하거나 microVM으로 옮겨야 한다.

비일관 CXL 테스트베드 가정: 서버 N대(목표 4대)가 multi-headed CXL 메모리 장치 하나를 공유, 서버 간 하드웨어 coherence 없음. 서버가 2소켓이면 sandbox는 CXL이 붙은 소켓 하나에서만 돌린다.

실행 환경 고정(모든 환경 공통): vCPU·VMM thread pinning, 서버마다 관리용 코어 1~2개 분리, SMT off(또는 기록), turbo off, 주파수 고정, 조건마다 5회 이상·순서 무작위, 제외 기준 사전 고정.

---

## 7. 실험 계획 (Phase A~F)

전체 원본은 claude.ai의 Claude Doc "CXL 기반 Agentic Sandbox 실험 계획 (SWE 274 trace)"에 있다(Markdown으로 내보내기 가능). 아래는 구현에 필요한 내용을 빠짐없이 옮긴 것이다.

**[repo 대조]** 원본 제목의 274는 잘못된 숫자다. 실제 데이터셋은 264개 trace다(7.1).

### 7.0 정의

| 용어 | 정의 |
| --- | --- |
| tool call 구간 | 명령 시작부터 결과 반환까지 |
| LLM wait 구간 | 결과 반환부터 다음 명령 도착까지 |
| contention 구간 | 노드의 runnable vCPU 수가 물리 코어 수를 넘는 연속 구간(1ms 해상도) |
| 잃은 CPU 시간 | contention 구간 동안 vCPU thread가 runqueue에서 기다린 시간의 합 |
| CXL tax | tool call 단위 (T_CXL − T_DRAM) / T_DRAM |
| L\* (break-even 길이) | D + C / (1 − f). C = downtime + attach 후 warm-up 손실, f = 과부하 노드에서 받는 CPU 비율, D = 탐지 지연 |
| 회수율 | 정책으로 줄어든 잃은 CPU 시간 ÷ 정책이 없을 때의 잃은 CPU 시간 |

L\* 예시(f = 0.5, D = 1ms, warm-up 1ms 가정): 500us 설계 ≈ 4ms, 5ms 설계 ≈ 13ms, 10ms 설계 ≈ 23ms.

### 7.1 데이터셋 (**[repo 대조]** 확인 완료)

- 원본: `SWE-bench/20260902_mini-v2.4.6_gemini-3-5-flash`, commit `8dcca98587e7f698753903e3b1747eb5dc013cb1`. mini-SWE-agent 2.4.6 `.traj.json` **264개**, 명령 13,600개. 파일 경로·크기·SHA-256은 `harness/trace-replay/data/inventory.json`.
- 각 trace에 assistant 메시지(LLM 응답 본문, 명령)와 tool 메시지(출력, `returncode`)가 있고, 둘 다 `extra.timestamp`(초 단위 wall clock)가 있다. 264개 전체에서 assistant 응답 13,600개가 모두 명령 1개였다. 따라서 tool timestamp는 해당 명령의 완료 시각으로 볼 수 있다.
- **LLM wait**: 이전 tool timestamp → 다음 assistant timestamp. 모델 요청, 생성, 네트워크, harness 처리가 다 들어간 **관측된 응답 간격**이다. 순수 inference 시간이 아니다(mini 2.4.6 코드에서 기록 지점 확인, [timing-traces.md](timing-traces.md)).
- 응답마다 token 수(prompt, completion, reasoning)가 있다. 토큰 단위 타이밍(streaming)은 없다. E5의 "streaming 출력의 tool 이름 시점" 입력은 이 데이터로 만들 수 없다.
- 기록상 작업 시간창은 최대 12개가 겹친다. 전체 시간 범위는 6,822.64초(약 114분), trace별 시간창 합계는 약 19.6시간이다.
- CPU·메모리 시계열과 sandbox 생성 시각은 없다. 그래서 sandbox 안에서 tool call을 재실행(replay)해 리소스 trace를 만든다.
- image는 trace에 `latest` tag로만 있다. 실제 사용한 image ID와 registry digest는 `docs/results/numa-immediate-20261014/image-lock.json`에 고정했다. 원본 수집 당시 digest는 알 수 없다.
- 이전 측정(264 pair, 53개 제외)과 같은 데이터셋이다. 제외 기준은 [결과 README](results/numa-immediate-20261014/README.md)를 따른다.

### 7.2 공통 인프라

- **replay harness**: 기록된 LLM 응답을 기록된 응답 시간 뒤에 내보내는 mock inference server(open-loop). tool 결과가 기록과 달라도 다음 명령은 그대로 보내고 diverged로 표시. LLM wait 스케일 1배(기본), 0.5배, 2배. 동시 실행은 무작위 시작 시각. GRPO 모사 모드는 같은 trajectory G개(4, 8)를 ±jitter로 동시 시작(같은 trajectory 복제라는 한계 명시).
  - **[repo 대조]** Docker 버전이 이미 있다(`experiment.py`). 기록된 명령을 순서대로 실행하는 open-loop이고, schedule은 `immediate`(대기 없음), `timestamps`(`--scale`로 응답 간격 배율), `--setup on-arrival --max-active N`(기록된 상대 시작 시각 유지). 종료 코드와 정규화한 제출 내용을 원본과 비교한다. 무작위 시작 오프셋, seed, GRPO 모드, 반복·순서 무작위화는 없다. 명령 timeout은 60초다.
- **sandbox**: Firecracker(snapshot baseline), QEMU 또는 Cloud Hypervisor(live migration baseline), 제안 VMM(서버마다 VMM shell pool). microVM 크기는 A1의 peak 메모리 p95 + 여유. vCPU 1 또는 2. 서버당 수십 개 이상. rootfs는 SWE-bench instance별 docker image 변환. hand-off 대상의 writable layer는 CXL 또는 공유 스토리지. 로컬 PyPI·git mirror, network 실험만 tc 주입.
  - **[repo 대조]** 기존 측정의 최대 메모리는 DRAM 18.08GiB / CXL 20.27GiB였다(trace 단위). 작업당 상한 32GiB, PID 상한 16384. microVM 크기를 정할 때 참고한다.
- **측정(1ms)**: bpftrace sched_switch·sched_wakeup → vCPU thread runqueue 대기, cgroup cpu.pressure, guest steal time. tool call 단위 카운터: LLC miss, MPKI, 로컬·CXL 대역폭(PCM), dTLB miss, page walk cycle. KVM: EPT violation(kvm_exit reason, kvm_page_fault), dirty page(PML 또는 dirty log). 이벤트 타임스탬프: tool call·LLM wait·hand-off·migration 각 구간 시작·종료. 서버 간은 PTP 기준.
  - **[repo 대조]** 현재 계측: cgroup CPU 20ms, 메모리 100ms 샘플링, perf event group(instructions, cycles, LLC ref/miss, retired load L3 miss) 기본 1초 간격(하한 100ms), cgroup PSI(cpu/memory/io), I/O 바이트, page fault, NUMA 배치. raw perf event는 Sapphire/Emerald Rapids 전용이고 `/usr/lib/linux-tools-6.8.0-146/perf`를 쓴다. 1ms runqueue, PCM 대역폭, dTLB, KVM tracepoint는 없다.
- **CXL pool 영역(비일관 테스트베드)**: VM slab(guest 메모리, 소유 host가 씀), EPT 영역(소유 host), mailbox(VM당 약 20KB 슬롯), ownership word(소유 host, 세대 번호), allocator 메타데이터(중앙 관리자).
- **플랫폼 검증(비일관 테스트베드, 실험 전)**: 서버 A가 쓰고 write-back 없이 B가 읽으면 stale이 보이는지. MLC로 서버별·동시 접속 수별 latency/bandwidth. clwb·clflushopt·범위 write-back/invalidate·wbinvd 비용. HPA 동일성 비교 스크립트. 서버 간 cmpxchg 원자성 경합 테스트.

### 7.3 Phase A: 워크로드 특성 (제안 시스템 불필요)

- **A1** 264개를 로컬 DRAM에서 단독 실행 5회. tool call별 지속 시간, CPU 시간, vCPU 수요 시계열, MPKI, 메모리 대역폭, dirty page 수, 네트워크 바이트. tool 종류 분류: pytest, 패키지 설치, python 스크립트, 빌드, git, 탐색(grep/cat/ls/find), 편집. 출력: 종류별 호출 비율·지속 시간·CPU 시간 분포, burst가 tool call 안 어디서 생기는지.
- **A2** turn별 LLM wait 길이, trajectory당 turn 수 → LLM wait CDF. 7.1의 timestamp로 오프라인 계산 가능.
- **A3** 서버 하나에 trajectory K개 동시 실행. overcommit 2, 4, 8, 16. GRPO 모드 G = 4, 8. host 코어 수 sweep. 지표: contention 구간 길이 CDF(개수 기준, 잃은 CPU 시간 가중), 잔여 시간 E[남은 길이 | 이미 d 지속](d = 1, 5, 10, 50ms), 공동 사건 비율(VM 2개 이상 burst가 겹친 구간의 잃은 CPU 시간 비율), 구간이 tool call 시작 후 100ms 안에 시작된 비율.
- **A4** 각 trajectory를 VM 메모리 전체 DRAM / 전체 CXL로 5회씩, 순서 무작위. tool call별 tax, MPKI·대역폭. 분석: **(최우선) tool call 단위 (CPU 수요, tax) 결합 분포**와, A3 contention 구간마다 "CPU를 쓰고 tax가 5% 또는 10% 미만인 VM"이 있는 비율. within/between 분산(ICC). 명령어 + 직전 tax로 tax 등급 분류기 AUC.
  - **[repo 대조]** 1회·고정 순서 버전은 끝났다(1.2). 명령 단위 latency 비율은 `paired-commands.csv`에 있다(Docker exec 경로 포함). 명령 단위 CPU 시간과 MPKI가 없어서 (CPU 수요, tax) 결합 분포는 아직 만들 수 없다.
- **A5** tool call 시작 시점 정보(명령어, 직전 이력, task)로 burst 길이·CPU 시간 예측. AUC, R².
- **판정**: 잃은 CPU 시간 대부분이 수 초 이상 구간 → migration으로 충분, 방향 재검토. contention 구간에 옮길 VM이 거의 없음 → 분류 기준을 "tax 대 이동성 가치"로 재설계. within 분산 작음 → 정적 분류로 단순화.

### 7.4 Phase B: baseline 메커니즘

| ID | 방식 | 구성 |
| --- | --- | --- |
| M1 | network pre-copy | QEMU 또는 Cloud Hypervisor, 25·100GbE |
| M2 | snapshot/restore | Firecracker full snapshot, 공유 스토리지 경유(Firecracker는 live migration 없음) |
| M3 | 공유 메모리 migration | QEMU `x-ignore-shared`, 메모리는 CXL DAX |
| M4 | 최강 restore | Firecracker, 메모리 파일을 CXL DAX에 두고 MAP_SHARED로 수정, 메모리 dump 생략, 프로세스 사전 생성 |
| M4+P | M4 + prefault | KVM_PRE_FAULT_MEMORY로 resume 전 EPT 채움 |
| M4-naive / M4-fair | flush 없음 / flush 포함 | 비일관 CXL에서 correctness 시연 / 공정 비교 |

- 메커니즘 비교 기준은 M3, M4, M4+P. M1은 하드웨어 수준 비교와 B3·B4 대조군.
- 비일관 CXL에서는 M3·M4·M4+P에도 제안과 같은 flush·invalidate를 넣고 그 비용을 C에 포함.
- **B1** 구간 분해(VM idle): 시작 → 정지 → 재개 → steady-state 회복(tool call 처리 속도 95%). EPT violation, host page fault, 출발·도착 CPU 시간. Firecracker 계열은 프로세스 생성 / LoadSnapshot / resume / 첫 tool call 응답 / working set 재적재 5구간. 메모리 1, 2, 4, 8GB × vCPU 1, 2, 4.
- **B2** idle(LLM wait 중) vs tool call 중(pytest, 패키지 설치, 빌드) 이동. M1은 pre-copy 라운드, 수렴, auto-converge·dirty-limit throttle과 그 동안 guest 속도. 모든 baseline: 출발 host CPU 소비, 같은 노드 다른 VM slowdown.
- **B3** 패키지 설치·git clone 중 이동, RTT 0.1ms·1ms. tool call 완료 시간 증가, TCP 재전송·RTO(nstat, ss -ti), 실패율.
- **B4** A3 고밀도 배치에서 VM 크기만큼 빈 메모리가 있는 목적지 비율(M1, M2만 해당).
- 문헌 참고치: Orthus grafting 29~37ms(같은 VM의 live migration은 downtime 268ms, 총 27.3초), QEMU live update 100ms 미만, ByteDance 11~38ms.

### 7.5 Phase C: trace replay simulator

- 입력: A3 VM별 vCPU 수요 시계열(1ms)·배치·경계, A4 tax, A5 예측기, B의 C·출발 host CPU 패널티·throttle·목적지 여유 제약.
- 모델: 노드 N = 4, 8, 16, host 코어 수 sweep. 과부하 노드에서 fair-share로 f 결정. 이동 가능 집합: 전부 / tax 임계값 이하 / CXL 비율 25·50·75%. 이동 중 VM 진행 0, warm-up 동안 속도 감소, 출발 host CPU 패널티.

| 정책 | 결정 시점 | 정보 | 메커니즘 |
| --- | --- | --- | --- |
| P0 무이동 | 생성 시 배치 | 노드 부하 | 없음(overcommit + QoS) |
| P1 reactive | contention이 D ms 지속 | 현재 상태 | 지연 L sweep |
| P2 경계·현실 | tool call 경계 | A5 예측기 | migration(B의 C) |
| P3 경계·oracle | tool call 경계 | 미래 완전 정보 | migration(B의 C) |
| P4 two-class | P1 + 경계 promotion/demotion | 현재 상태 + 예측 tax | hand-off + promotion |

- P1: 과부하 노드에서 CPU를 가장 많이 쓰는 이동 가능 VM을 가장 한가한 노드로. 같은 VM을 H ms 안에 되돌리지 않는 hysteresis.
- sweep: L 100us~10s(log, 10단계 이상), D 1·5·10ms, overcommit 2·4·8·16, G 1·4·8.
- 출력: **C1(핵심)** x = L(log), y = 회수율, M1·M3·M4·M4+P·hand-off·in-place(약 500us) 위치 점, P3 수평선. C2 = C1 + B2 부수 비용 패널티. C3 = CXL 비율·이동 가능 집합별 회수율. C4 = overcommit별 C1.
- 검증: Phase F 실측과 step time 오차 10% 이내 목표.
- 판정: P3가 P1 회수율의 80% 이상 → 노드 간 재배치 필요성 약함. C1이 M3·M4 지연 근처에서 포화 → 공유 메모리 migration으로 충분. hand-off 지점과 500us 지점 차이가 크면 D7 진행.

### 7.6 Phase D: VM hand-off (비일관 CXL 실서버)

목표: downtime이 M4+P 이하, attach 후 EPT 재구축 손실과 device 리셋 없음, downtime 10ms 미만.

- **D1** 2.2의 9구간 breakdown, vCPU 수별 직렬화 바이트. Orthus 30ms 분해와 나란히.
- **D2** 메모리 1~16GB, vCPU 1·2·4, dirty set 0~LLC 2배, flush 방식(wbinvd, clflush 범위, PML·dirty bit 제한), VMM shell 사전 생성 여부. 메모리 크기별 시간이 평평해야 한다. wbinvd가 다른 VM에 주는 slowdown.
- **D3** hand-off 직후 95% 회복 시간, EPT violation, cache·TLB miss. M3·M4·M4+P와 같은 C 정의로 비교. 격차 3분해(KVM 객체 사전 생성 / EPT 입양 / device 연속성).
- **D4 (핵심)** EPT 처리(입양 / lazy 재구축 / prefault vCPU 1·2·4 병렬) × 매핑(4KB/2MB/1GB) × working set(100MB/500MB/1GB/4GB). attach 직후 EPT violation, prefault 시간(downtime 포함), 회복 시간. dedup·PML을 켠 상태의 실제 매핑 크기 분포. EPT in CXL vs 로컬의 page walk cycle과 tool call 시간. trade-off 곡선(x = 초당 hand-off 수, y = 총 손실).
- **D5** ping-pong stress(A→B→A, 1~100ms 간격 수천 번, guest 안 쓰기-검증 checksum). negative test: dest invalidate, INVEPT, EPT A/D write-back, mailbox write-back을 하나씩 빼고 오류 재현. ownership word로 single-writer 위반 감지. devdax라 mmu notifier 이벤트가 없음을 확인. 부팅 시 HPA 동일성 검증. M4-naive의 stale 데이터 시연.
- **D6** 제어 채널(vsock 또는 CXL ring) 스트림 유지 또는 재접속 비용. network drop vs 버퍼링(B3 실험으로 TCP 재전송·RTO). writable layer 위치별 hand-off 가능 여부와 in-flight I/O drain.
- **D7 (조건부)** in-place 변형: breakdown과 correctness(VMCLEAR, host-state 필드 재작성, 동일 CPU 모델). hand-off 대비 추가 회수율과 구현 비용.
- 판정: downtime이 M4+P보다 길면 직렬화·shell 경로 최적화 또는 이점을 EPT·device로만 주장. 1GB에서 차이가 미미하면 4KB 강제 조건을 전면에. 4KB에서 prefault가 hand-off보다 크면 EPT 입양 유지. coherence 비용으로 10ms 초과면 L\* 재계산.

### 7.7 Phase E: promotion/demotion

- **E1** 1, 2, 4GB를 CXL↔DRAM 복사, 서버 하나 및 모든 서버 동시. 크기별 시간과 실효 대역폭.
- **E2** A2 LLM wait CDF 위에 E1 복사 시간. 복사보다 긴 wait 비율, 짧은 wait에서 다음 tool call 지연 분포.
- **E3** 모든 서버 동시 전환 시 CXL 상주 VM의 tax 상승. 병목이 장치 전체 대역폭인지 head별인지. GRPO 모드. rate limit 유무.
- **E4** 로컬 DRAM 비율 25·50·75%별 promotion 실패율, 강제 demotion 빈도.
- **E5** 결정 입력 비교: 직전 tax, 명령어 예측, streaming 출력의 tool 이름 시점(토큰 타이밍이 있을 때). 오예측 시 tax 분포. **[repo 대조]** 현재 데이터셋에는 토큰 타이밍이 없다(7.1).
- **E6** page 단위 tiering 한 지점: tax 감소 vs hand-off 직전 로컬 부분 write-back 비용.
- 판정: 복사보다 짧은 LLM wait이 다수면 비동기·부분 복사로. 동시 전환이 tax를 크게 올리면 rate limit과 전환 스케줄링 추가.

### 7.8 Phase F: end-to-end (같은 총 메모리)

- 워크로드: 한 step = 264개에서 뽑은 task B개(64, 128) rollout 동시 실행, G = 4, 8. step time = 가장 늦은 rollout. 10 step 이상. CXL 공유 서버 전부(목표 4대), 서버당 sandbox 수십 개 이상.

| ID | 메모리 | 이동 메커니즘 | 정책 | 질문 |
| --- | --- | --- | --- | --- |
| F0 | 전부 로컬 | 없음 | power-of-k + overcommit + SCHED_IDLE·core scheduling | 현재 production 수준(DSec) |
| F1 | 전부 로컬 | M1 | 경계 이동 | 로컬 전용에서 full migration을 쓰면? |
| F2 | 전부 CXL | 없음 | F0과 같음 | pooling만의 효과와 tax |
| F3 | 전부 CXL | M3·M4·M4+P | 경계 + reactive | 가장 강한 migration baseline |
| F4 | 전부 CXL | hand-off | reactive | hand-off 효과(tax 전부 냄) |
| F5 | two-class 정적 | hand-off | 생성 시 분류(Pond) | 정적으로 충분한가 |
| F6 | two-class 동적 | hand-off + promotion/demotion | 제안 정책 | 전체 시스템 |
| F6+ | two-class 동적 | in-place + promotion/demotion | 제안 정책 | D7 진행 시에만 |

- iso-capacity: 총 메모리 M 고정. 물리 DRAM은 못 바꾸므로 sandbox용 로컬 DRAM은 hugepage 예약·cgroup 한도로, pool은 slab 수로 제한. F0·F1은 서버마다 M/N 로컬. F2~F6은 CXL 비율 r(25·50·75%)만큼 pool slab, 나머지 균등. host OS와 VMM은 로컬.
- 지표: step time(중앙값, P99), rollout P50·P99, tool call 지연, 시간당 rollout, 같은 처리량에 필요한 노드 수, 노드당 동시 sandbox 수, 이동으로 인한 tool call 실패율, diverged 비율, CPU 사용률, 이동 횟수와 총 CPU 시간.
- 분해: F0→F2(pooling − tax), F2→F3→F4(공유 메모리 migration vs hand-off), F4→F5→F6(tax 큰 VM 제외, 동적 분류).
- 판정: F6이 F3보다 의미 있게 낫지 않으면 contribution을 D와 A 중심으로 재구성. F2가 F0보다 나쁘고 F6도 F0을 못 넘으면 CXL 도입 정당성 부족.

### 7.9 진행 순서와 관문

- A와 B를 먼저(단일 서버), 그 결과로 C 회수율 곡선.
- **G1** (C 이후): 짧은 contention이 잃은 시간의 상당 비중이고, oracle 경계 정책으로 못 메운다.
- D와 E는 구현과 병행. **G2** (D 이후): downtime ≤ M4+P, EPT 재구축과 device 리셋 없음.
- **G3** (F 이후): F6이 F3보다 step time 또는 필요 노드 수에서 우수.

### 7.10 산출 그림

| 그림 | 내용 | Phase |
| --- | --- | --- |
| Fig 1 | contention 구간 길이 CDF(잃은 CPU 시간 가중)와 잔여 시간 | A3 |
| Fig 2 | tool call 단위 (CPU 수요, tax) 산점도 | A4 |
| Fig 3 | 메커니즘별 C 분해, idle vs burst 중 이동 | B1, B2 |
| Fig 4 (핵심) | 메커니즘 지연 vs 회수율, hand-off·in-place 두 지점, oracle 수평선 | C |
| Fig 5 | hand-off 구간 breakdown과 메모리 크기별 scaling | D1, D2 |
| Fig 6 | EPT 입양 vs 재구축 vs prefault, page size별 | D4 |
| Fig 7 | LLM wait CDF 위 복사 시간 | A2, E2 |
| Fig 8 | F0~F6 step time과 필요 노드 수 | F |

### 7.11 이야기가 무너지는 조건

- 잃은 시간 대부분이 가장 강한 migration의 L\*보다 긴 구간 → migration으로 충분.
- oracle 경계 배치가 대부분 회수 → hand-off 단계 불필요.
- CPU를 많이 쓰는 VM이 곧 tax가 큰 VM → 옮길 VM이 없음.
- coherence 비용 포함 시 hand-off가 10ms를 크게 넘음 → L\* 재계산.
- 1GB page를 쓸 수 있음(dedup·dirty tracking 없음) → EPT 입양 이득 미미.

---

## 8. Claude Code 작업 목록 (우선순위 순)

T1~T6과 T8은 단일 bare-metal 서버에서 바로 시작할 수 있다. T7은 같은 서버의 프로세스 간 이동으로 시작한다. hand-off 구현(D)은 이 목록 밖이다.

### 제안 repo 구조

```
repo/
  CLAUDE.md                # @docs/HANDOFF.md import
  docs/HANDOFF.md
  configs/                 # 실험 조건 YAML (환경, overcommit, 반복 수, seed)
  traces/                  # 264 SWE trace 원본 (읽기 전용)
  images/                  # rootfs 빌드 스크립트와 산출물
  harness/                 # mock inference server, in-guest agent, launcher
  collect/                 # bpftrace, PSI sampler, perf/PCM wrapper, KVM tracepoint
  baselines/               # M1~M4+P 실행 스크립트
  sim/                     # Phase C simulator
  analysis/                # 지표 계산, 그림
  results/<run_id>/        # manifest.json + parquet
```

**[repo 대조] 실제 구조**: `harness/trace-replay/{experiment.py, analyze.py, configs/node0.json, data/}`, 결과 export는 `docs/results/<run>/`(CSV·JSON), trace 원본은 `harness/trace-replay/data/native/`(Git 제외), 원시 결과는 `harness/trace-replay/results/`(Git 제외). README는 "Python 실행 파일은 두 개로 유지하고 새 framework나 wrapper 파일을 추가하지 않는다"고 정했다. 위 제안 구조로 넓힐지는 microVM·simulator를 시작할 때 정한다. `harness/mini-swe-agent/`, `harness/SWE-agent/`는 upstream 참고용 checkout이다.

### T1. trace 형식 파악 — **[repo 대조]** 대부분 완료

- 완료: 스키마 확인(7.1, [timing-traces.md](timing-traces.md), `docs/evidence/`), LLM 응답 시간 의미 확인(관측된 응답 간격), 이전 측정과 같은 264개임을 확인, inventory·SHA-256 고정, `--plan`으로 명령 수·겹침·시간 범위 계산.
- 남은 일: 명령어를 7.3 A1의 tool 종류로 분류하는 규칙 초안과 분류 결과 분포. 현재 코드에는 분류 로직이 없다.

### T2. sandbox 이미지

- SWE-bench instance별 docker image를 Firecracker용 ext4 rootfs로 변환하는 스크립트. guest kernel, in-guest agent 포함.
- 로컬 PyPI·git mirror 구성.
- 이미지별 크기와 빌드 성공 여부 리포트.
- **[repo 대조]** Docker image 준비(`--pull`)와 image ID·digest 고정은 되어 있다(`image-lock.json`, `harness/trace-replay/data/images-*.json`). rootfs 변환, guest kernel, mirror는 없다.

### T3. replay harness

- mock inference server: trace의 LLM 응답을 기록된 지연 뒤에 반환(스케일 1, 0.5, 2배).
- in-guest agent: 명령 실행, stdout/stderr/exit code 반환, tool call 시작·종료 시각 기록. 제어 채널은 vsock.
- 결과가 기록과 다르면 diverged 표시, 다음 명령은 기록대로 진행(open-loop).
- launcher: 서버 하나에 K개 동시 실행, 무작위 시작 오프셋, GRPO 모드(G개 동시 + jitter), seed 고정.
- **[repo 대조]** Docker 기반 open-loop 재생, 응답 간격 배율(`--scale`), 동시 실행 상한(`--max-active`), 종료 코드·제출 비교는 있다(7.2). vsock agent, microVM, 무작위 오프셋, seed, GRPO 모드는 없다.

### T4. 측정 수집기 (1ms)

- bpftrace: vCPU thread별 runqueue 대기(sched_wakeup → sched_switch), 1ms bin으로 집계.
- cgroup cpu.pressure와 노드 runnable 수 샘플러.
- tool call 구간별 perf/PCM 카운터(LLC miss, MPKI, 로컬·CXL 대역폭, dTLB miss, page walk cycle).
- KVM tracepoint(kvm_exit reason EPT violation, kvm_page_fault) 카운터.
- 모든 타임스탬프는 CLOCK_MONOTONIC ns(서버 간은 PTP). 실행마다 manifest(git commit, 커널, CPU, BIOS 설정, 조건 YAML) 저장.
- **[repo 대조]** cgroup CPU 20ms·메모리 100ms 샘플러, perf event group(1초 기본), PSI, monotonic 시계, 실행별 `manifest.json`·`run.json`(설정, 커널, PMU 정의, 코드 hash, trace 순서)은 있다. 1ms bpftrace, 명령 구간별 perf, PCM, KVM tracepoint는 없다.

### T5. Phase A 실행 스크립트

- A1(단독 5회), A3(K, overcommit 2/4/8/16, G, 코어 수 sweep), A4(DRAM vs CXL 바인딩, 5회, 순서 무작위).
- 실행 환경 고정 스크립트: pinning, 관리용 코어 분리, SMT, turbo off, governor 고정. 위반 시 실행 거부.
- **[repo 대조]** A4의 1회·고정 순서 버전(`--compare-memory`)과 단독 순차 실행(`--sequential`)은 있다. CPU pinning과 관리용 코어 분리(controller 0-1)는 되어 있고 SMT는 꺼져 있다. governor는 `schedutil`이고 turbo·주파수 고정과 위반 시 실행 거부는 없다.

### T6. Phase A 분석

- contention 구간 추출(runnable > 코어 수, 1ms), 잃은 CPU 시간 가중 CDF, 잔여 시간 함수, 공동 사건 비율, tool call 시작 후 100ms 내 비율.
- tool call 단위 tax, (CPU 수요, tax) 결합 분포, contention 구간마다 "CPU 사용 중 + tax < 임계값" VM 존재 비율.
- within/between 분산(ICC), tax 등급 분류기(AUC), burst 예측기(AUC, R²).
- Fig 1, Fig 2, Fig 7(앞부분) 생성.
- **[repo 대조]** `analyze.py`에 trace·명령 단위 latency 비율, MPKI, slowdown CDF가 있다. 위 지표는 아직 없다.

### T7. Phase B baseline (단일 서버 버전부터)

- M1: QEMU localhost migration, `max-bandwidth`로 10/25/100Gbps 흉내. idle vs tool call 중. pre-copy 라운드, 수렴, throttle, downtime, 총 시간, 출발 측 CPU.
- M2/M4: Firecracker snapshot → 다른 프로세스 restore, 5구간 타이밍. M4는 MAP_SHARED, 메모리 dump 생략, 프로세스 사전 생성 수정 버전.
- M3: QEMU `x-ignore-shared`.
- M4+P: KVM_PRE_FAULT_MEMORY 적용.
- 공통 출력: 메커니즘별 C(downtime + warm-up), 회복 곡선, EPT violation 수. Fig 3.

### T8. Phase C simulator

- 입력: T6 산출(VM별 1ms 수요 시계열, 경계, tax, 예측기), T7 산출(C, 패널티).
- 정책 P0~P4, sweep(L, D, overcommit, G, 노드 수, 코어 수, CXL 비율).
- 출력 C1~C4, Fig 4. 결정적 재현(seed), 단위 테스트(작은 합성 trace에서 손계산과 일치).

### 데이터 스키마 초안

| 테이블 | 주요 컬럼 |
| --- | --- |
| tool_calls | run_id, traj_id, call_idx, tool_type, cmd_hash, start_ns, end_ns, cpu_time_ns, mpki, llc_miss, bw_local, bw_cxl, dirty_pages, net_bytes, diverged, mem_tier |
| llm_waits | run_id, traj_id, turn_idx, start_ns, end_ns, source(recorded/substituted) |
| vcpu_samples | run_id, node, vm_id, t_ms, runnable, runq_wait_us, steal_us |
| episodes | run_id, node, start_ms, end_ms, lost_cpu_ms, n_contributors, contributors |
| moves | run_id, mechanism, vm_mem_gb, vcpus, state(idle/busy), total_ms, downtime_ms, recovery_ms, ept_violations, src_cpu_ms |
| sim_results | config_hash, policy, L_us, D_ms, overcommit, G, nodes, cores, cxl_ratio, recovered_frac |

---

## 9. 미해결 질문

- ~~264개 trace의 실제 형식(LLM 응답 시간, 토큰 타이밍 유무).~~ **[repo 대조]** 해결: 응답 간격은 있고 토큰 타이밍은 없다(7.1).
- 비일관 CXL multi-host 하드웨어 확보 시점, 장치의 서버 간 atomic 지원 여부, head 수와 대역폭.
- 슬라이드 5를 측정한 서버의 CXL 장치 사양(A4에 사용). **[repo 대조]** 서버 구성은 6장에 정리했다. 장치 모델·용량·대역폭은 아직 모른다. 2026-10-19에는 node 2가 0MB로 보였다.
- microVM 메모리 크기(A1 peak 메모리 결과로 결정). 컨테이너 기준 최대 약 20GiB(7.2).
- 제안 VMM의 기반(Firecracker 수정 vs Cloud Hypervisor 등).
- 제어 채널 설계(vsock 재접속 vs CXL ring).
- Design 4(dedup, KSM)와 single-writer 가정의 충돌: 병합 page의 CoW·refcount를 여러 host kernel이 건드리게 된다. TrEnv-X가 agent instance 간 중복 감소를 이미 다루므로 차별화 필요.
- **[repo 대조]** Phase A를 Docker 컨테이너로 계속할지, microVM으로 옮긴 뒤 할지. A3 runqueue 지표와 A4 tax는 sandbox 형태에 영향을 받는다.

---

## 10. 참고

- Claude Doc 실험 계획 원본(claude.ai 로그인 필요): https://claude.ai/code/artifact/b61b24f9-3e0c-4a50-9a66-35e25e096cea
- Orthus (ASPLOS'19): https://cs.fsu.edu/~zwang/files/asplos19.pdf
- QEMU Live Update (Oracle): https://blogs.oracle.com/linux/post/qemu-live-update
- KVM live upgrade (ByteDance, KVM Forum 2020): https://kvm-forum.qemu.org/2020/Kvm-live-upgrade-with-properly-handling%20of%20passthrough%20devices.pdf
- Firecracker snapshot 지원 문서: https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md
- TrEnv / TrEnv-X: https://arxiv.org/html/2509.09525v1
- DSec (DeepSeek Elastic Compute): https://arxiv.org/html/2609.22978v1
- Aquifer: arXiv 2606.24079
- CXLfork (ASPLOS'25), Pond (ASPLOS'23), 특허 US 11875174, LSFMM 2023 CXL VM migration 세션
- Claude Code CLAUDE.md import 문서: https://code.claude.com/docs/en/memory
