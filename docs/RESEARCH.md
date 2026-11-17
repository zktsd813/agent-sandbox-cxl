# CXL VM hand-off 연구 정리

이 문서 하나에 연구 목표, 설계, 구현 상태, 지금까지의 측정 결론, 버린 방향, 남은 일을 모았다. 2026-11-12에 흩어져 있던 문서 40여 개를 합쳤다. 합치기 전 문서는 태그 `docs-pre-merge`에 있고, 이 문서에서 `docs-pre-merge:<경로>`로 적은 것은 `git show docs-pre-merge:<경로>`로 연다.

- 실행 방법: [README](../README.md), VM 구현은 [implementation/virtual_machine/README.md](../implementation/virtual_machine/README.md)
- 측정 원자료: [docs/results/](results/)

## 0. 한눈에

**목표.** 여러 서버가 CXL 메모리 하나를 같이 쓸 때, 실행 중인 sandbox VM을 다른 서버로 몇 ms 만에 옮긴다(hand-off). 그리고 이것이 기존 VM migration보다 빠르고 쓸모 있다는 것을 실험으로 보인다.

**된 것**
- VM의 메모리, 주소표(EPT), vCPU 상태(VMCS)를 CXL에 두고, 목적지가 이것을 다시 만들지 않고 그대로 이어받는 구현이 있다(2.2).
- 서버 한 대 안에 가짜 서버 두 대를 띄운 lab에서 옮기는 데 0.2~0.6 ms, 실제 SWE 작업 중에는 1.3~2.7 ms가 걸렸다. 테스트가 도는 도중에 옮겨도 결과가 정확했다(3.3).
- 코드를 33,000줄에서 12,000줄로 정리했다.

**측정이 말하는 것**
- 노드 하나만 CPU가 넘치는 순간은 실제로 생기고, 빠른 이동만 그 대기를 되찾는다.
- 하지만 그 대기는 전체 느려짐의 작은 몫이다. 명령 시간은 6% 안팎만 준다. 무거운 CPU 사용은 명령 문자열만 보고 미리 알 수 있어서 "미리 옮기면 되지 않나"라는 반론이 강하다(4장).
- 11-13에 시나리오를 다시 찾았다([motivation-scenarios-20261113](results/motivation-scenarios-20261113/README.md)).
  - 무거운 명령이 시작할 때 코어가 남는 노드로 보내면 이상적 공유 이득의 70~78%를 되찾는다. 같은 SLO에 노드가 48/96/192코어면 26/16/7% 이상 덜 든다(simulator).
  - LLM 대기 중 미리 옮기기는 이보다 못하다. 다음 명령을 몰라 헛이동이 많고, 아는 경우에도 대기 시작 때 본 부하가 낡는다.
  - 실행 중 이동은 sandbox가 CXL에서 돌아야 해서 tax에 진다(손익분기 tax 1~2%).
  - 이전 상태만 CXL에 두고 무거운 명령을 DRAM에서 돌리면 tax가 +2%였다(bud05 실측, 15 명령).
  - 그래서 이야기를 "sandbox는 CXL에 주차하고, 무거운 명령마다 코어가 남는 노드의 DRAM에서 시작한다"로 바꾼다. CXL 없이 100 Gb/s로 복사해도 CPU 가중으로는 거의 같다는 점이 가장 큰 약점이다.

**안 된 것**
- 진짜 여러 서버 CXL에서 해 보지 않았다. 그래서 캐시를 내려쓰고 비우는 coherence 처리도 아직 없다(3.5).
- 가장 강한 비교 대상(M4+P: 다시 띄우고 주소표를 미리 채우기)이 없어서 "기존보다 빠르다"를 판정할 수 없다(6.1).
- 메모리 크기·vCPU 수·회복 시간 같은 평가 실험 대부분이 남았다(6.2).

**다음**: 6.4.

---

## 1. 문제와 처음 주장

agentic RL rollout에서 sandbox(microVM)는 LLM 응답을 기다리다가 tool call(명령)이 올 때마다 짧게 CPU를 쓴다. 이 burst는 짧고 예측하기 어렵다고 보았다. 노드 하나에 burst가 겹치면 그 노드의 sandbox가 느려지고, 동기 RL에서는 가장 늦은 rollout이 step 시간을 정한다.

2026-10-16 발표에서 세운 주장은 다음과 같다.

1. sandbox는 한 번에 한 곳에서만 실행된다(single writer). 서버 사이 일관성이 없는 CXL 공유 메모리에도 맞는다.
2. 대부분 sandbox는 MPKI가 낮아 CXL 메모리에서도 느려짐(CXL tax)이 작다. 일부만 로컬 DRAM이 필요하다.
3. burst는 예측할 수 없고 VM migration은 비싸다. migration이 끝날 즈음 burst는 다른 곳으로 옮겨 가 있다. 그래서 ms 단위 hand-off가 필요하다.

1은 설계 조건으로 유지한다. 2와 3은 측정으로 상당 부분 약해졌다(4장).

---

## 2. 설계

### 2.1 무엇을 CXL에 두나

VM을 A에서 B로 옮길 때 B가 갖춰야 하는 것은 네 가지다.

| 항목 | 크기 | 기존 방법(migration, snapshot) | 이 연구 |
|---|---|---|---|
| guest 메모리 | 수 GB | 네트워크나 파일로 복사 | CXL에 두고 복사하지 않음 |
| 주소표(EPT): guest 주소 → 실제 메모리 주소 | 메모리 크기에 비례 | B에서 빈 표로 시작해 다시 채움 | CXL에 두고 B가 그대로 이어받음 |
| vCPU·장치 상태(VMCS, 레지스터, virtio) | Firecracker 의미 상태로 약 14 KB | 직렬화해서 넘김 | CXL에 두고 B가 이어받음(장치 상태는 작은 packet) |
| host 쪽 연결(FD, thread, eventfd, irqfd, tap, virtio backend) | 작음 | 새로 만듦 | B에서 다시 연결(CXL에 둘 수 없음) |

주소표를 다시 채우는 비용이 핵심이다. 빈 표로 시작하면 VM이 메모리 한 칸(4KB)을 처음 건드릴 때마다 CPU가 멈추고(EPT violation), KVM이 그 칸을 채운 뒤 재개한다. 1GB를 쓰면 약 26만 번이다. 이 연구는 표를 CXL에 둬서 이 비용을 없앤다. CXL이어야 하는 이유는 B의 하드웨어 page walker가 같은 물리 주소에서 표와 guest page를 직접 읽어야 하기 때문이다. RDMA로는 안 된다.

용어: 노드 사이 이동은 **hand-off**, 노드 안 DRAM ↔ CXL 이동은 **promotion/demotion**, **migration**은 비교 대상인 기존 VM migration에만 쓴다. motivation deck에서는 hand-off를 cross-node context switch라고 불렀다.

### 2.2 설계안과 실제 구현

처음 계획(2026-10-19)은 두 단계였다.

- 기본안: 메모리와 EPT는 CXL에 두고, vCPU 상태(약 20KB)는 직렬화해 CXL mailbox로 넘긴다. B에 미리 띄운 VMM이 역직렬화하고 EPT를 입양한다. 목표 정지 시간 10ms 미만.
- 선택안(in-place): VMCS와 KVM 객체까지 CXL에 두고 detach/attach만 한다. 위험이 커서 미뤄 뒀다(VMCS 포맷이 구현에 의존, host-state 필드, VMCLEAR).

**실제 구현(direct 모드)은 선택안까지 갔다.** guest RAM, EPT, VMCS, KVM의 VM/vCPU 객체가 모두 CXL DAX에 있고, B는 이것을 그대로 쓴다. 직렬화하는 CPU register snapshot은 없다. B에서 하는 일은 다음뿐이다(`kvm-dax-state.patch`의 `kvm_mmu_dax_ept_rebind`).

1. EPT 표 페이지가 모두 CXL 안에 있는지 확인한다(밖을 가리키면 거절).
2. B 커널의 작은 장부(struct page backlink, slab cache anchor)만 그 표 페이지에 다시 연결한다. 처음 보는 표 페이지만 연결하고, 다시 방문할 때는 새로 생긴 페이지만 본다.
3. TLB를 비우고 재개한다.

표의 내용은 다시 채우지 않는다. 대가는 "같은 CPU 모델·microcode·커널·KVM 모듈" 조건이 필수라는 점이다.

### 2.3 지켜야 할 조건과 구현 상태

| 조건 | 내용 | 상태 |
|---|---|---|
| HPA 동일성 | 모든 서버의 CXL window base, HDM decoder, DAX offset이 같아야 EPT를 이어받을 수 있다 | 고정 HVA·PFN 검사 있음. 서버 간 비교 스크립트 없음 |
| devdax 노출 | pool을 system RAM으로 online하지 않는다(각 커널이 서로 덮어씀) | lab은 VM 전용 CXL frame mapper(`/dev/cxl_pool`, ABI 4) |
| 단일 소유자 | VM 메모리와 EPT는 현재 소유 host만 쓴다. owner epoch로 직렬화 | seal → handle → import, epoch, lease 검사 있음. 서버 간 atomic 없음(한 host 안 lab) |
| coherence | 비일관 CXL에서는 넘기기 전 write-back(guest dirty line, EPT A/D bit, mailbox), 받은 뒤 invalidate와 INVEPT가 정확성 조건 | 옵션 구현. KVM·장치 장부, 단일 작성자 log와 cache 유지. [11-17 검증](results/noncoherent-handoff-20261117/README.md)에서 목록 누락·범위 누락 0. Coherent lab이며 실제 stale 재현은 하지 않았다 |
| EPT 불변성 | DAX라 host mm이 swap·migration·reclaim을 하지 않아 이어받은 EPT에 mmu notifier 이벤트가 없다 | DAX arena는 EPT page를 재사용하지 않는다 |
| 같은 구성 | CPU 모델, microcode, 커널, KVM 모듈 동일 | lab은 양쪽 같은 binary. Intel direct L1은 XFD/AMX를 숨겨야 vCPU 생성이 된다 |
| device 연속성 | tap, socket, conntrack, block fd는 CXL에 둘 수 없다 | 읽기 전용 block만. network·쓰기 가능 disk direct는 미구현 |

### 2.4 two-class와 promotion/demotion

- CXL tax가 작은 sandbox만 CXL에 두고(이동 가능), tax가 큰 sandbox는 로컬 DRAM에 고정한다.
- tool call 사이(LLM 응답 대기)에 DRAM ↔ CXL로 VM 메모리를 옮겨 부류를 바꾼다.
- 구현된 promotion/demotion은 **RAM 전체 복사**다. 2 GiB에 약 1~2초 걸린다(3.3).
- 가장 큰 위험: CPU를 많이 쓰는 VM이 tax도 크면 옮길 VM이 없다. 측정에서 이 위험이 일부 맞았다. CPU를 넓게 쓰는 sandbox의 tax 중앙값은 1.14, 좁게 쓰는 sandbox는 1.02였다(4장 11-05 cap sweep).

---

## 3. 구현 상태 (2026-11-12, branch `vm-cleanup`)

### 3.1 코드

| 위치 | 내용 | 규모 |
|---|---|---|
| `implementation/virtual_machine/kvm-dax-state.patch` | KVM 객체·VMCS·EPT를 DAX에 할당, detach/attach ioctl, EPT rebind, nested lab 보완, 계측 | 25 files +1694/−175 |
| `implementation/virtual_machine/firecracker.patch` | DAX RAM, state-only/reuse/direct/resident 경로, 장치 재연결, restore timing | 28 files +4223/−95 |
| `pool_memory_module.c` 외 | VM 전용 CXL frame mapper(owner epoch, seal/import) | ABI 4 |
| 나머지 Python·C·shell | lab(`lab.py`), SWE runner·독립 verifier, resident 제어, 측정 | 41 files 11,967줄 |

기준 소스는 Firecracker `95f868c8`, Linux DCD 6.6.0-rc6(`cxl-static-capacity.patch` 2 files +10/−8 위에 `kvm-dax-state.patch`), QEMU v10.2.2(`qemu-cxl-direct-ram.patch` 2 files +89/−2)다. direct는 KVM ioctl 8개와 `dax_handoff_mode` 2·3을 쓴다.

2026-11-11~12에 두 번 정리했다(117 files 33,000줄 → 41 files 11,967줄). 정리 전 코드는 태그 `vm-pre-cleanup-20261111`(commit `184167c`)에 있다.

### 3.2 hand-off 모드

| 모드 | 넘길 때 하는 일 | B에서 새로 만드는 것 | 비교 사다리(6.1, Q10)에서의 위치 |
|---|---|---|---|
| `snapshot` / state-only | CPU·장치 상태를 저장하고 source 종료, B에서 복원 | KVM 객체, EPT(빈 표) | M4에 가장 가까움 |
| `reuse` | 실행 이력이 있는 B worker에 상태 복원 | 없음(FD·thread 재사용) | M4 + KVM 객체 사전 생성 |
| `reuse-prepared` | 빈 worker의 연결 정리를 미리 해 두고 복원 | 없음 | 위와 같음, 준비를 앞당김 |
| `direct` | DirectExport → B가 CXL의 객체를 attach | host 쪽 연결만 | 제안(in-place) |
| resident direct | 양쪽 VMM과 제어기를 상주시켜 소유권과 최신 장치 상태만 전달 | 첫 방문 뒤로는 없음 | 제안, 실사용 경로 |

`snapshot`이 M4의 조건(B 프로세스 사전 생성, MAP_SHARED, 메모리 dump 없음)을 모두 맞추는지는 따로 확인하지 않았다. `migration-bench`는 pre-copy M1이 아니라 정지 후 RAM 전체 전송 대조군이다.

### 3.3 lab 측정 (bud05, nested)

환경: Xeon Gold 6548Y+ 한 대 안에 QEMU L1 A/B 두 대(file-backed emulated CXL Type-3)를 띄우고, 그 안에서 Firecracker L2 sandbox를 A ↔ B로 옮긴다. network는 socket backend. NUMA 0만 쓴다. 원자료는 [results/vm-handoff-lab-20261112](results/vm-handoff-lab-20261112/README.md).

단위 ms, source pause 요청 → remote guest ACK.

| 모드 | p50 | p95 |
|---|---:|---:|
| snapshot | 8.53 | 12.38 |
| reuse | 9.01 | 9.49 |
| reuse-prepared | 1.40 | 1.52 |
| direct, 최소 Linux guest | 0.57 | 0.71 |
| direct, CPU-only guest | 0.21 | 0.22 |

실제 SWE 작업(Gin, PHP-CS-Fixer, MicroPython)을 기록된 명령 그대로 돌리며 옮겼다. 15 trial 모두 독립 검증(명령 원문, 반환값, test 요약, 최종 diff, 열린 FD·프로세스 연속성)을 통과했다.

| Gin direct, 무계측 | ms |
|---|---:|
| 처음 B로 | 2.5~2.7 |
| warm p50 / p95 (38 hop) | 1.34~1.36 / 1.87~2.08 |
| promotion 직후 B로 | 7.5~8.0 (대부분 nested 전용 EPT root 보완, 3.4) |
| state-only (controller wall time) | 43~100 |
| `go test ./...`(3.69초) 실행 중 B→A→B | 같은 PID로 계속 실행, 결과 정상 |

RAM 전체 promotion/demotion(2 GiB): promotion 첫 회 약 2.1~2.2초, 두 번째 약 1.05~1.13초. demotion 약 1.1~1.5초(한 번 2.69초가 나왔고 재실행은 1.23초).

이전 측정(10-15~11-04)은 다른 서버(Xeon Gold 5317)의 nested lab에서 TAP network로 쟀다. 지표(정지→ACK, API 합, 요청→Resume)와 RAM 크기, 장치 유무가 행마다 달라 서로 바로 비교할 수 없다. 흐름만 보면 다음과 같다.

| 날짜 | 경로와 조건 | p50 (ms) | 원자료 |
|---|---|---:|---|
| 10-15 | direct 첫 구현, 128MiB, 장치 없음, 정지→guest ACK | 18.2 → 최적화 후 0.76 | `implementation/virtual_machine/direct-handoff-optimized-summary.json` |
| 10-19 | state-only / reuse / reuse-prepared, 128MiB | 6.0 / 2.3 / 2.3 | 같은 디렉터리의 `semantic-snapshot-comparison.json`, `used-kvm-reuse-comparison.json`, `coarse-prepare-comparison.json` |
| 10-20 | 2GiB, reuse-prepared / direct, 정지→첫 ACK | 2.64 / 0.34 | [results/dax-scale-20261020](../implementation/virtual_machine/results/dax-scale-20261020/) |
| 10-20 | 정지 후 RAM 전체 전송(TAP), 1GiB, ACK / 완료 | 약 773 / 4,896 | [results/network-fairness-20261020](../implementation/virtual_machine/results/network-fairness-20261020/) |
| 11-02 | 새 VMM direct, 2GiB, 읽기 전용 block + NFS, API 합 | 11.9 → 9.0, 무계측 8.3 | [results/direct-api-20261102](../implementation/virtual_machine/results/direct-api-20261102/README.md) |
| 11-03 | resident, 영속 RPC 연결, 요청→Resume | 2.97 | [results/control-transport-20261103](../implementation/virtual_machine/results/control-transport-20261103/) |
| 11-04 | resident, 무계측 실제 SWE(Gin) 반복 38회 | 4.09 (p95 5.33), promotion 직후 27.5 | [results/direct-resident-20261102](../implementation/virtual_machine/results/direct-resident-20261102/README.md) |
| 11-12 | 같은 경로, bud05, socket | 1.34~1.36 (p95 1.87~2.08), promotion 직후 7.5~8.0 | [results/vm-handoff-lab-20261112](results/vm-handoff-lab-20261112/README.md) |

**FragVisor와 같은 경계(11-12, node 1).** source에서 L2 vCPU가 마지막으로 guest를 나온 시각부터 목적지에서 처음 guest로 들어가기 직전까지를 한 방향으로 쟀다. 두 L1의 TSC 차이는 공유 메모리 ping-pong으로 보정했다(오차 약 0.09 µs). 원자료는 [results/vm-handoff-fragvisor-boundary-20261112](results/vm-handoff-fragvisor-boundary-20261112/README.md), 해석은 8.1.

| direct, 목적지 busy-poll | one-way p50 / p95 (µs) | 평균 |
|---|---:|---:|
| 2 MiB CPU-only guest | 37 / 44 | 42 |
| 128 MiB Linux guest | 288~295 / 343~348 | 301~314 |

nested root 보완(`dax_nested_root_sync`)은 11-02에 hand-off마다 하던 RAM 전수 조회를 없앤 직후 들어갔다. 전수 조회를 없애자 nested 환경에서 데이터 불일치와 triple fault가 났기 때문이다.

### 3.4 lab 숫자를 읽을 때 주의

- **nested라서 생기는 비용이 섞여 있다.** 바깥 KVM이 L1의 EPT를 따로 shadow로 들고 있어서, B가 CXL의 EPT를 이어받아도 바깥 표는 다시 채워야 한다. `kvm.dax_nested_root_sync`가 이를 맞추는 lab 전용 보완이다. kernel trace로 잰 promotion 직후 B hand-off 6.2~7.6 ms 중 4.7~5.4 ms가 이 구간이다. 물리 서버에는 없는 비용이지만 빼고 보고하지 않는다.
- **nested는 coherence 문제를 재현하지 못한다.** lab의 두 L1은 같은 물리 메모리를 하드웨어 일관성 아래 공유한다. 그래서 2.3의 write-back/invalidate가 없어도 맞게 돈다.
- 계획(2026-10-19)은 이 두 이유로 hand-off 성능 주장(Phase D, E3, F)을 실제 비일관 CXL multi-host에서 하기로 했다. 지금 lab은 기능 검증용이다.
- warm hop 하나가 40.97 ms로 튄 적이 있다(대부분 socket 대기, 원인 미확정).

### 3.5 아직 없는 것

| 항목 | 상태 |
|---|---|
| 물리 multi-host 비일관 CXL | 안 해 봄. bud05 node 2가 4 host 공유 DCD로 기록돼 있으나 쓰지 않음(용량 할당에 승인 필요) |
| coherence write-back / invalidate / INVEPT | 미구현 |
| 서버 간 HPA 동일성 검사, 서버 간 atomic 소유권 | 미구현 |
| network, 쓰기 가능 disk direct | 미구현(읽기 전용 block만) |
| M1 pre-copy, M3, M4+P, M4-naive/fair | 미구현(6.1) |
| EPT 이어받기와 VMCS·객체 재사용의 효과 분리 | 안 잼 |
| 장애 복구 | 범위 밖 |

2026-11-16에 direct를 고정 1~8 vCPU로 넓혔다. 2/4/8개 각각 205회 왕복과
CPU별 데이터·IPI·timer 검증을 통과했다. Gin 4 vCPU도 실행 중 왕복 후 baseline과 결과가 같았다.
범위와 source pause → guest ACK 수치는 [다중 vCPU 결과](results/multi-vcpu-handoff-20261116/README.md)에 있다.
이 검증도 한 host의 coherent nested lab이며 물리 multi-host 성능 주장이 아니다.

---

## 4. 측정에서 알게 된 것

hand-off가 필요한지를 확인하려고 10-14부터 11-09까지 잰 결과다. 원자료와 자세한 조건은 각 결과 디렉터리 README에 있다. 데이터셋은 다음 두 가지다.

- **다언어 SWE trace**: mini-SWE-agent 2.4.6 trace 264개(13,600 명령, Gemini 3.5 Flash). 빌드가 무겁다.
- **iter2**: 공개 RL rollout. Qwen3.5-35B-A3B GRPO, Python task. 응답 간격 중앙값 7.7초, p99 80초.

### 4.1 CXL tax

| 날짜 | 측정 | 결과 | 원자료 |
|---|---|---|---|
| 10-14 | 다언어 264 trace를 Docker로, 대기 없이 DRAM(node 0)과 CXL(node 2)에서 1회씩 | 비교 가능한 211쌍에서 trace 시간 증가는 기하평균 13.2%, 중앙값 11.2%, p95 42.8%, 최대 79.9%다. 명령 단위(10,807개)는 p50 1.018, p95 1.215, 최대 3.54다. instructions 가중 LLC MPKI는 DRAM 1.54 / CXL 1.48로 낮다 | [numa-immediate-20261014](results/numa-immediate-20261014/README.md) |
| 10-21 | 같은 trace에서 명령마다 새로 쓴 4KB page 범위 | call당 p50 2.0 MB, p95 315 MB, p99 1,983 MB. flush할 cache line 수나 이동 시간으로 바로 바꿀 수 없다 | [write-pages-node1-20261021](results/write-pages-node1-20261021/write-report.md) |
| 11-13 | bud05 DCD(node 2), Docker, 무거운 명령 15개(7개 언어) × 전부 DRAM / 전부 CXL / 이전 상태만 CXL × 3회, 순서 무작위 | 명령 시간 tax(명령 CPU 가중): 전부 CXL +56%(CPU 시간 +84%), 이전 상태만 CXL +2.0%(CPU 시간 +0.2%). 이전 상태는 중앙값 59 MiB, 최대 422 MiB | [motivation-scenarios-20261113](results/motivation-scenarios-20261113/README.md) 3.12 |
| 11-05 | 고정 clock Docker, CPU를 넓게 쓰는 trace 24개 / 좁게 쓰는 trace 24개 | tax 중앙값은 넓은 쪽 1.14, 좁은 쪽 1.02다. 넓은 sandbox를 늘 CXL에 두고 hand-off해도 tool 시간은 약 1.14(p90 1.35)로, 지금의 1.45보다 낫다 | [cap-sweep-20261104](results/cap-sweep-20261104/README.md) 8.5 |

주의:
- 10-14 측정은 1회, DRAM → CXL 고정 순서였다. `schedutil` 저부하 clock에서 쟀으므로 4 GHz 고정이면 tax가 더 클 수 있다(cap sweep 8.1, 8.5).
- 반복하고 순서를 무작위로 바꾼 명령 단위 tax(A4)는 아직 없다.

### 4.2 CPU 경합과 이동

| 날짜 | 측정 | 결과 | 원자료 |
|---|---|---|---|
| 10-20~21 | optane 서버, 4 node × 5코어, 짧은 trace 67개 microVM pilot과 simulator | 코어당 sandbox 2.8 / 4.2 / 5.4개에서 대기/CPU 2.9 / 18.1 / 54.7%. simulator에서 10 ms hand-off는 59~90%를, 2 GiB pre-copy migration은 7~23%를 되찾는다 | `docs-pre-merge:experiments/contention/RESULTS-20261021.md` |
| 10-21 | 192코어 node 16대 fleet simulator | 사용률 80%에서 잃은 CPU 1.2~3.7%이고, 그중 노드 사이 몫은 6~35%다. migration은 최선 44%, hand-off는 92%를 되찾는다. step 지연 1.0006~1.011 | `docs-pre-merge:experiments/contention/RESULTS-fleet.md` |
| 10-14 | Preact trace 55 명령을 microVM 1 vCPU와 2 vCPU로 | 계측 구간 86.72 / 66.52초, guest runqueue 대기 185.83 / 115.78초. 2 vCPU에서 reschedule IPI 27,500회. 1회씩 | `docs-pre-merge:docs/microvm-scheduling.md` |
| 10-26~28 | bud05, iter2 rollout 448개, Firecracker 1 vCPU, 노드 분리(15×2) 대 통합(30) | 현실 LLM 속도(×1)에서 sandbox 평균 0.022코어, 사용률 11~15%, runqueue 대기 1.3~1.8%. 사용률 79%에서 대기 15%. 잃은 CPU의 70%는 다른 노드에 빈 코어가 있을 때 생겼고, 합치면 40~60% 준다. 동기 step 차이 1% 이내. 192코어 외삽 시 사용률 75%까지 잃은 CPU 1% 이하 | [virtual-node-rl-20261026](results/virtual-node-rl-20261026/README.md) |
| 10-29~11-03 | iter2 step 구조를 맞춘 동기 재생과 배치 screen | 재생/원본 시간 비 0.97~0.98. image locality 배치로 한 노드에 sandbox가 몰리지만 hand-off가 이를 없앤다. group·step 지연 차이 2% 이내. iter2 시간 구성: LLM 87.7%, env 생성 6.2%, tool+agent 6.1% | `docs-pre-merge:docs/experiment-plan-tail-placement.md` 8장 |
| 11-04~05 | sandbox 크기 cap과 hand-off. simulator와 bud05 Docker 실측(16코어, 다언어 48 trace) | 섞인 순서에서 cap 없음의 CPU 가중 slowdown은 1.175다. 60 ms 반응은 늘어난 tool 시간의 90%를 되찾는다. 단 늘어난 몫이 8.8%뿐이다. Python RL은 cap 1에서도 rollout이 0.8%만 는다 | [cap-sweep-20261104](results/cap-sweep-20261104/README.md) |
| 11-05 | bud05 NUMA 0을 15코어 pool 2개로 나누고 container cpuset을 실제로 이동 | 넓은 sandbox의 40%는 수명 1/4이 지나서야 첫 burst를 낸다. 사용률 41~83%에서 대기의 49~71%가 한 pool만 넘쳤을 때 생겼다. 60 ms 이동은 공유 pool 대비 대기 차이의 61~96%, 5초 이동은 10~14%를 되찾는다. 그러나 K=120에서 CPU 가중 명령 시간은 −6%, rollout p99는 1.58 → 1.50이다 | [pool-mover-20261105](results/pool-mover-20261105/README.md) |
| 11-13 | fleet simulator, 다언어 cap30, 16노드 × 48~256코어. 명령 시작 때 옮기기(경계), 실행 중 hand-off(reactive), LLM 대기 중 미리 옮기기, 100/25 Gb/s 복사, CXL tax | 사용률 90%에서 경계 1~10 ms는 pool 이득의 69~78%, reactive를 더하면 76~82%, pre-copy는 5~12%를 되찾는다. 경계 이동은 100 ms까지, 실행 중 이동은 10 ms까지 견딘다. 대기 중 미리 옮기기는 oracle도 52~56%다. 100 Gb/s 복사는 CPU 가중으로 경계 이득의 97%다. CPU 가중 ≤ 1.10에서 노드 절감은 48코어 26%, 256코어 4.2% 이상(상한 9.5%) | [boundary-vs-handoff-20261113](results/boundary-vs-handoff-20261113/README.md), [motivation-scenarios-20261113](results/motivation-scenarios-20261113/README.md) |

- 11-05 실험의 부하는 실제에서 정하지 않고 경합이 생기도록 골랐다(빌드가 무거운 다언어 trace, 빠른 LLM, CPU 제한 없음). 이동은 cpuset만 바꾼 비용 없는 이동이다.
- 11-06에 workload를 다시 봤다. Python RL로 15코어의 70%를 채우려면 sandbox 261개(코어당 17개)가 필요하다. 메모리가 허락하는 밀도(bud05 코어당 8~10개, DSec 2.7~4.2개)를 넘는다. 노드 사이 1초 CPU 상관은 Docker 정상 상태 −0.09~0.10, 동기 step 0.66~0.77이다(`docs-pre-merge:docs/workload-diversity-plan.md` 2절).

### 4.3 CPU 밖 자원과 다른 기회

| 날짜 | 측정 | 결과 | 원자료 |
|---|---|---|---|
| 11-02~04 | 같은 socket의 메모리 간섭(SMT sibling, 대역폭 부하)과 노드 안 해결책 | 대역폭 부하에서 gcc(MPKI 0.07)는 cycle ×1.31~1.42였고, RDT를 걸면 ×1.03~1.05로 돌아왔다. CLOS 2개를 tool call 단위로 재배정하면 ×1.088이다. 노드 안 수단으로 대부분 풀린다 | `docs-pre-merge:docs/dsec-interference-design.md` 4절, bud05 `~/agent-sandbox-cxl/docs/memory-interference-plan.md` 7절(git 밖) |
| 11-06 | 32코어 kernel 하나에 동시 microVM 32~192개 | exit(1.45 → 1.55 µs), virtio 알림, boot 같은 이벤트당 비용은 거의 그대로다. 명령 ×1.10은 clock 하락과 CPU 대기로 설명된다. L3를 pool마다 나눠도 CPU 가중 3.02 / 2.99로 같다. CPU 밖에서 따로 생기는 기회는 없다 | [microvm-system-20261106](results/microvm-system-20261106/README.md) |
| 11-09 | 명령 종류별 CPU, reward 평가, env 생성, sandbox 밖 CPU | 탐색 명령은 명령 수의 36~45%지만 CPU는 1~4%다. test·build·install이 trace CPU의 60%(RL), 94%(다언어)다. 그래서 무거운 명령을 명령 문자열만 보고 미리 안다. 기록된 env 생성(중앙값 94초)은 provider의 생성 대기열이 정했고, 모두 1초로 줄여도 step은 약 3% 준다. 채점 CPU는 agent CPU의 0.12배다. 우리 microVM은 0.28초에 뜬다 | [trace-character-20261109](results/trace-character-20261109/README.md) |
| 11-09 | GPU 노드의 남는 CPU에 sandbox를 올리고 학습이 CPU를 원할 때 hand-off로 비우기 | 사용자가 다른 서버(A6000)에서 시험했다. GPU 작업이 CPU 하나를 계속 써서 hand-off할 틈이 없었고, 고정 분할이 나았다 | repo 밖(branch `gpu-harvest-a6000-results`, c831300) |

### 4.4 지금 말할 수 있는 것

- **확인된 것**:
  - 배치할 때 burst를 몰라 한 노드만 넘치는 순간이 생긴다.
  - 그 불균형 대기는 빠른 이동만 되찾는다. 60 ms는 대부분 되찾고, 5초는 거의 못 되찾는다.
  - CPU 밖 시스템 자원은 CPU보다 먼저 막히지 않는다.
  - (11-13) 이 대기는 명령 시작 때 한 번 옮기는 것만으로 대부분 되찾는다. LLM 대기 중 미리 옮기기는 이보다 못하다.
  - (11-13) 이전 상태를 CXL에 둔 채 무거운 명령만 DRAM에서 돌리면 tax는 +2%다.
- **필요성을 약하게 하는 것**:
  - 되찾는 대기가 전체 느려짐의 작은 몫이다. 명령 −6%, rollout p99 1.58 → 1.50, 동기 step 차이 1% 이내다.
  - 현실 LLM 속도의 Python RL sandbox는 평균 0.02~0.06코어라 경합 자체가 드물다.
  - 경합이 크게 나온 결과는 부하를 그쪽으로 골랐다.
  - 무거운 CPU는 명령 문자열로 미리 보인다. Q3(tool call 경계에서 미리 옮기기) 반론이 강하다. 11-13 결과로 답은 생겼다(7장 Q3). 대신 이득이 명령 시작 때 옮기기에 몰리고, 실행 중 이동의 몫은 작다.
  - 옮기고 싶은 넓은 sandbox가 tax도 크다. CXL에서 돌면 실행 중 이동의 이득을 tax가 넘는다(손익분기 1~2%).
  - (11-13) CXL 없이 100 Gb/s로 멈추고 복사해도 CPU 가중으로는 명령 시작 때 옮기기 이득의 97%를 얻는다. 명령마다 새 shell을 띄우는 agent는 명령 사이 상태가 작다(중앙값 약 130 MiB). CXL의 몫은 tail, 25 GbE, 출발 노드 CPU, 메모리 용량이다.
  - (11-13) 노드가 클수록 이득이 준다. 256코어에서는 어떤 정책도 노드를 9.5%보다 더 줄이지 못한다.
- **아직 열린 것**:
  - 다른 host로 옮기면 clock·cache 간섭이 줄어드는지(지금까지는 모두 한 socket 안이다).
  - 4 GHz에서 반복·무작위 순서로 잰 명령 단위 tax.
  - 고밀도에서 VM 생성·종료 비용.
  - 평가를 한꺼번에 돌리는 동기 RL의 reward 단계.
  - 여러 vCPU sandbox.
  - 실제 hand-off의 정지 시간과 warm-up 비용.

11-09 motivation deck(bud05 `~/agent-sandbox-cxl/docs/results/motivation-summary-20261109/`, git 밖)의 핵심 문장: "빠른 hand-off는 노드 불균형으로 생긴 대기를 대부분 없앤다. 하지만 그 대기는 느려짐의 작은 몫이라 명령 시간은 6% 안팎만 준다."

---

## 5. 버린 방향

| 방향 | 기간 | 버린 이유 | 기록 |
|---|---|---|---|
| GPU 노드 CPU harvest | 11-03~09 | GPU 작업이 CPU 하나를 계속 써서 비울 틈이 없다. 고정 분할이 낫다 | `docs-pre-merge:docs/gpu-node-harvest-design.md` |
| 메모리 간섭(LLC, 대역폭, SMT sibling)을 hand-off로 피하기 | 11-02~06 | RDT·CLOS로 노드 안에서 대부분 풀린다. L3 분리도 차이 없음 | `docs-pre-merge:docs/dsec-interference-design.md`, 4.3 |
| CPU 밖 시스템 자원(exit, virtio, journal, lock) 경합 | 11-06 | 이벤트당 비용이 192 VM까지 평평하다 | [microvm-system-20261106](results/microvm-system-20261106/README.md) |
| env 생성을 이동으로 줄이기 | 11-09 | 생성 시간은 provider 대기열이 정한다. 해법은 image 저장 방식(lazy, EROFS)과 warm pool이다 | [trace-character-20261109](results/trace-character-20261109/README.md) |
| 여러 서버의 page cache 공유(CXL 한 벌 + Bloom filter 위치 조회, guest pool, page discard, CoW 공유) | 10-27~11-10 | hand-off와 다른 주제라 별도 repo `~/cxl-page-cache`로 옮겼다 | `docs-pre-merge:docs/cxl-data-pooling.md`, 태그 `vm-pre-cleanup-20261111` |
| 코어 수 sweep, 실제 밀도 증가, DSec식 co-location 실험 계획(E1~E5, I1~I3) | 11-02 | 4.2·4.3 결과로 실행하지 않았다 | `docs-pre-merge:docs/experiment-program-20261102.md`, `density-sweep-design.md` |
| H100에서 RL trace를 직접 수집 | 10-23~25 | 공개 iter2 rollout으로 대신했다 | `docs-pre-merge:docs/trace-collection-plan.md` |
| Docker 기반 CXL checkpoint worker | 10-07~ | microVM hand-off로 방향을 바꿨다 | `docs-pre-merge:docs/worker-design.md` |
| Open-SWE, Terminal-Bench, BrowserGym, SWE-agent run-replay 재생 | 10-07 | 공식 mini-SWE-agent 264 trace를 썼다 | `docs-pre-merge:docs/replay-readiness.md`, `server-run-guide.md` |
| 끝난 기능 시험·특성 측정 코드: 쓰기 가능 home storage, MPTCP 연결 유지, cache/PML 비용, network fairness, DAX 크기 sweep, CPU context switch, KVM 객체 pointer 분류, NFS synthetic, VMM heap, resident JSON 재방문 | 10-20~11-04 | 결과가 기록됐고 남은 경로가 쓰지 않는다. 11-11~12 정리에서 코드를 지웠다 | 태그 `vm-pre-cleanup-20261111` |
| nested lab으로 hand-off 성능 주장 | 10-19 결정 | 바깥 KVM의 shadow EPT가 이어받기 효과를 가리고, coherence 문제를 재현하지 못한다(3.4). lab은 기능 검증에만 쓴다 | 이 문서 3.4 |

---

## 6. 남은 일

### 6.1 비교 대상(baseline)

"기존 방법보다 빠르다"를 보이려면 같은 CXL 위에서 기존 방법을 재야 한다.

| ID | 방법 | B에서 하는 일 | 상태 |
|---|---|---|---|
| M1 | 네트워크 live migration(QEMU pre-copy). 돌리면서 메모리 전체 복사 | 전부 새로, EPT 빈 표 | 없음. 정지 후 전체 전송 대조군만 있다(1GiB, TAP: ACK 약 773 ms, 완료 4.9초) |
| M2 | Firecracker snapshot. 멈추고 메모리 전체를 파일로, B가 읽음 | 전부 새로, EPT 빈 표 | 없음 |
| M3 | QEMU `x-ignore-shared`. 메모리는 CXL에 두고 복사 안 함 | VM 객체 새로, EPT 빈 표 | 없음 |
| M4 | Firecracker restore를 CXL에 맞게 고친 것(MAP_SHARED, dump 없음, 프로세스 사전 생성) | KVM 객체 새로, EPT 빈 표 → 재개 후 칸마다 멈춤 | `snapshot` 모드가 가장 가까움 |
| M4+P | M4 + 재개 전에 `KVM_PRE_FAULT_MEMORY`로 EPT를 미리 채움 | 재개 후 멈춤 없음, 대신 채우는 시간만큼 정지가 길어짐 | **없음** |
| M4-naive / fair | flush 없음 / flush 포함 | 비일관 CXL에서 stale 데이터 시연 / 공정 비교 | 없음(실 하드웨어 필요) |

M4+P가 가장 강한 상대다. 관문 G2가 "hand-off 정지 시간 ≤ M4+P"다. 단, 1GB huge page를 쓰면 표 줄 수가 적어 다시 채워도 싸므로 이득이 작다. 4KB가 강제되는 조건(KSM dedup, dirty tracking)을 함께 보여야 한다.

### 6.2 평가 실험

| ID | 내용 | 상태 |
|---|---|---|
| D1 | hand-off 구간 분해(정지, 상태 전달, EPT 이어받기, 장치, 재개) | 부분(kernel trace로 attach·EPT·nested root 구간) |
| D2 | 메모리 1~16GB, vCPU 1·2·4, dirty set, flush 방식별 시간. 메모리 크기에 평평해야 함 | 일부: [11-17](results/noncoherent-handoff-20261117/README.md)에 RAM 2GiB·1 vCPU·쓰기 0/16/64/256MiB를 쟀다. 메모리 크기 전체 sweep은 남았다. 참고: 10-21에 CLWB/CLFLUSHOPT write-back + invalidate를 쟀다. 1GiB에서 0.1/1/10/100%를 썼을 때 0.13/0.91/10.5/98.0 ms였다. 1%만 써도 RAM 전체를 훑으면 99.8 ms다. dirty 범위를 PML 등으로 좁혀야 한다([results/cache-pml-20261021](../implementation/virtual_machine/results/cache-pml-20261021/)) |
| D3 | 옮긴 뒤 95% 속도 회복 시간, M3·M4·M4+P와 같은 기준 | 안 함 |
| D4 | EPT 이어받기 vs 재구축 vs prefault × page size(4K/2M/1G) × working set | 안 함 |
| D5 | 수천 번 왕복 stress, write-back/invalidate를 하나씩 빼고 오류 재현 | 부분(거절·변조 시험, 왕복 수십 번) |
| D6 | 제어 채널, network, 쓰기 가능 disk 연속성 | direct는 읽기 전용 block만. 지운 시도가 둘 있다. snapshot 경로의 MPTCP 연결 유지(10-22, 4,000 record 오류 0, peer 최대 공백 5.77초)와 쓰기 가능 home disk의 DRAM↔CXL 왕복(10-28, 12 전환·74 명령). 둘 다 태그 `vm-pre-cleanup-20261111`에 있다 |
| D7 | in-place(VMCS 공유) | 이미 이 방식으로 구현됨 |
| E1 | DRAM ↔ CXL 복사 시간, 동시 전환 | 2 GiB 단일 숫자만 |
| E2 | LLM 응답 대기 분포 위에 복사 시간을 얹어 대기 안에 끝나는 비율 | 안 함 |
| E3~E6 | 동시 전환의 tax 상승, DRAM 비율별 실패율, 결정 입력, page 단위 tiering 한 점 | 안 함 |
| C | trace 재생 simulator. 정책 P0(이동 없음, 노드 안 QoS), P1(경합이 D ms 지속되면 이동), P2/P3(tool call 경계에서 예측/oracle로 이동), P4(two-class). 이동 지연 L을 100 µs~10 s로 sweep해 회수율 곡선(Fig 4)을 그린다 | fleet simulator(`experiments/contention/fleet.py`)가 일부를 한다. 정책 비교 곡선은 없음 |
| F | end-to-end. 한 step = task 64·128개 × G 4·8 rollout, 4대. F0(전부 로컬, DSec식 QoS), F2(전부 CXL, 이동 없음), F3(CXL + M3·M4·M4+P), F4(CXL + hand-off), F5(정적 two-class), F6(동적 two-class + promotion/demotion). 총 메모리를 고정한다 | 안 함(실 하드웨어 필요) |

### 6.3 관문

- **G1**: 짧은 contention이 잃은 시간의 상당 부분이고, tool call 경계에서 미리 옮기는 oracle 정책으로 못 메운다. 4장 측정이 이 관문을 약하게 만든다.
- **G2**: 정지 시간 ≤ M4+P, EPT 재구축과 device 리셋이 없다.
- **G3**: end-to-end에서 two-class 동적 정책(F6)이 공유 메모리 migration(F3)보다 step time이나 필요 노드 수에서 낫다.

### 6.4 다음 순서

1. **연구 방향 판단**(사용자 결정): 4.4가 노드 사이 CPU 경합만으로는 동기가 약하다고 말한다. hand-off가 실제로 이길 조건을 정해야 한다. 후보는 다음과 같다.
   - 작은 노드와 높은 사용률
   - 동기 RL에서 평가를 한꺼번에 돌리는 reward 단계
   - host를 바꿔야만 피할 수 있는 간섭
   - 메모리가 밀도를 정하는 배치에서 hand-off로 만드는 여유

   11-12에 사용자가 마지막 후보를 구체적으로 제안했다.
   - 코어당 메모리는 메모리 채널이 제한하고, CXL이 용량을 늘린다.
   - CXL에 민감한 sandbox는 DRAM에 두어야 한다. 그런 sandbox가 한 노드에 몰리면 hand-off로 푼다.

   10-14 자료를 명령 단위로 다시 보니 민감도는 sandbox가 아니라 무거운 명령의 성질이었다. 1회, Docker, 익명 메모리만 CXL에 둔 측정이다. 분석 스크립트는 그 세션 scratchpad에만 있고 repo에 없어서 다시 만들어야 한다.
   - test 명령이 CPU의 73%, build가 21%이고 둘 다 +23% 느렸다. 탐색·git·편집 명령은 +1~5%였다.
   - tax 5% 미만 명령은 CPU의 12%뿐이었다.

   그래서 "민감한 sandbox는 DRAM에 고정하고 둔감한 것을 옮긴다"는 틀로는 옮길 CPU가 거의 없다. 대안은 명령 단위 틀이다.
   - 모든 sandbox를 CXL에 둔다.
   - 도착한 명령 문자열로 무거운 명령을 알아보고 로컬 DRAM에서 돌린다.
   - 무거운 명령이 겹치면 hand-off로 DRAM과 CPU가 남는 노드를 고른다.

   **11-13 결과**([motivation-scenarios-20261113](results/motivation-scenarios-20261113/README.md)): 명령 단위 틀이 가장 강한 시나리오로 남았다.
   - 명령 시작 때 코어가 남는 노드로 보내는 것이 이득의 대부분이다. 실행 중 이동과 대기 중 미리 옮기기는 약하다.
   - 이전 상태만 CXL에 두고 무거운 명령을 DRAM에서 돌리면 tax +2%다.
   - 남은 질문은 두 가지다.
     - 쉬던 VM을 다른 노드에 10 ms 안에 붙이는 데 hand-off가 M4·M4+P보다 얼마나 나은가(아래 2).
     - guest가 "CXL 층 + 붙은 노드의 DRAM 층"을 쓰게 하는 설계와 그 비용. 명령마다 DRAM 층을 붙이고, 명령 뒤 남은 page를 CXL로 내린다. 구현도 측정도 없다.
2. **M4+P와 효과 분리**: lab에서 바로 할 수 있다.
   - `KVM_PRE_FAULT_MEMORY` prefault를 `snapshot` 경로에 붙인다.
   - EPT만 이어받는 단계와 VMCS·객체까지 이어받는 단계를 나눠 잰다. 이렇게 하면 G2를 lab 수준에서 처음 판정한다.
   - 11-13 결과로 비교할 상황이 정해졌다. LLM 대기 동안 쉬던 VM을 무거운 명령 시작 때 다른 노드에 붙이고, 첫 명령이 끝날 때까지를 잰다. 명령 시작 때 옮기기는 10 ms 안이면 1 ms와 결과가 같으므로, M4가 EPT를 다시 채우는 비용까지 10 ms 안에 들면 메커니즘으로서 hand-off의 몫이 사라진다.
3. **명령 단위 CXL tax 재측정**(A4): 4 GHz 고정, 5회, 순서 무작위로 잰다. 명령 단위 CPU와 함께 기록해 (CPU 수요, tax) 분포를 만들고 repo에 남긴다. 11-13에 무거운 명령 15개를 bud05 DCD에서 3회, 순서 무작위로 쟀다(4.1). 264 trace 전체는 아직이다.
4. **실제 비일관 CXL**:
   - bud05 node 2 DCD를 쓸 수 있는지 확인한다(용량 할당은 승인 필요).
   - 서버 간 stale 시험 → write-back/invalidate/INVEPT 구현 → M4-naive/fair 순서로 간다.
5. **promotion/demotion 구간 분해와 E2**: 2 GiB 1~2초가 LLM 응답 대기 안에 끝나는 비율을 본다. 11-13 설계에서는 VM 전체가 아니라 무거운 명령이 DRAM에 남긴 page만 내린다(중앙값 16 MiB, 최대 1.6 GB). DCD 쓰기 대역폭은 아직 재지 않았다.

---

## 7. 예상 리뷰어 질문

| 질문 | 답 | 근거 실험 |
|---|---|---|
| Q1 그냥 VM migration 하면? | burst와 contention이 migration 지연보다 짧으면 끝날 때 burst도 끝나 있다. burst 중 VM은 dirty rate가 높아 pre-copy가 수렴하지 않고, 과부하 출발 host의 CPU를 쓴다 | A3 구간 길이, C 회수율 곡선 |
| Q2 CXL에 메모리를 두면 migration이 싸지는 건 알려진 것 아닌가? | 맞다(LSFMM 2023). 그래서 같은 CXL 위 M3·M4·M4+P와 비교한다. 차이는 EPT 이어받기, KVM 객체 재생성 회피, 비일관 CXL 소유권 프로토콜 | B, D3, D4 |
| Q3 tool call 경계(LLM 대기)에서 미리 옮기면? | 대기 시작 때는 다음 명령을 모른다(명령은 LLM 생성이 끝나야 나온다). 이력으로 예측하면 이동의 74%가 헛이동이고, 다음 명령을 아는 oracle도 pool 이득의 52~56%만 되찾는다. 대기 시작 때 본 목적지 부하가 명령 시작 때는 낡아서다. 명령 시작 때 명령 문자열을 보고 옮기면 68~78%다. 대기 중 미리 옮기기를 더하면 오히려 나빠지고, 대기를 4배로 늘려도 나아지지 않는다. 남는 반론은 "명령 시작 때 100 Gb/s로 복사"다. CPU 가중으로는 같고 tail에서 진다 | [motivation-scenarios-20261113](results/motivation-scenarios-20261113/README.md) 3.5, 3.11 |
| Q4 노드 안 QoS(overcommit + SCHED_IDLE + core scheduling)로 충분? | QoS는 코어를 만들지 못한다. 노드 총수요가 용량을 넘는 correlated burst를 보여야 한다 | F0, P0 |
| Q5 1GB huge page면 EPT 재구축은 거의 공짜 아닌가? | 맞다. KSM dedup과 dirty tracking이 4KB를 강제하는 조건을 함께 보인다 | D4 |
| Q6 `KVM_PRE_FAULT_MEMORY`로 미리 채우면? | M4+P baseline. 이어받기는 상수 비용, prefault는 매핑 크기에 비례 | M4+P, D4 |
| Q7 page 단위 tiering(TPP, Memtis)은? | 일부 page가 로컬에 있으면 HPA 동일성과 EPT 재사용이 깨지고 hand-off 전 write-back이 working set에 비례한다 | E6 |
| Q8 생성 시점에 한 번 분류(Pond)하면? | 명령 단위 CXL/DRAM 비율이 p50 1.02, p95 1.22, 최대 3.54로 넓다(10-14, 1회). sandbox 안 분산과 sandbox 사이 분산을 아직 나눠 보지 않았다 | A4 |
| Q9 작은 host라 생긴 contention 아닌가? | host 코어 수를 sweep한다 | A3 |
| Q10 M4와의 차이는? | M4에서 하나씩 바꿔 KVM 객체 사전 생성 → EPT 이어받기 → device 연속성으로 3분해. `snapshot` → `reuse-prepared` → `direct`가 이 사다리의 일부다 | D3 |
| Q11 FragVisor는 vCPU 이동이 86 µs인데 왜 더 느린가? | 86 µs는 상태(약 6.9 KB)만 넘기고 미리 만든 vCPU에 복원하는 구간이다. 메모리는 옮긴 뒤 DSM으로 page마다 가져오고 EPT도 다시 채운다. 같은 경계로 잰 우리 nested 값은 CPU-only guest 37 µs, Linux guest 약 295 µs다(p50). Linux 쪽은 executor wake와 Firecracker detach가 대부분이다. 목적지 L1의 EPT 재구축은 0회다. 같은 경계의 bare-metal 숫자는 아직 없다 | D1, D3, 8.1 |

---

## 8. 관련 연구

| 연구 | 핵심 | 이 연구와의 차이 |
|---|---|---|
| Orthus (ASPLOS'19) | 같은 host에서 VM grafting, 메모리 공유로 GPA→HPA 유지, 정지 약 30ms | 같은 host, 새 KVM 인스턴스 |
| KVM live upgrade (ByteDance, KVM Forum 2020) | shared memory로 상태 저장·로드, vCPU 정지 11~38ms, 메모리 크기와 거의 무관 | 같은 host |
| 특허 US 11875174 | warm upgrade 뒤 새 VM이 이전 EPT base를 넘겨받음 | 같은 host |
| QEMU live update (Oracle) | 같은 host에서 QEMU 교체, guest RAM 같은 HVA, 정지 100ms 미만 | 같은 host, VM 재생성 |
| CXLfork (ASPLOS'25) | 프로세스 page table을 CXL 물리 주소로 rebase해 host 간 fork | 프로세스, read-only clone |
| TrEnv / TrEnv-X (SOSP'24) | host별 사전 구축 page table. VM EPT 사전 구축은 future work | 이어받기가 아니라 재구축 |
| Aquifer (arXiv 2606.24079) | 비일관 CXL 2.0 MHD에서 microVM snapshot 공유, ownership 프로토콜 | read-only, 복사 방식 |
| Firecracker snapshot | MAP_PRIVATE lazy 로딩, 새 프로세스에서 VM 재생성, vsock 리셋 | device 리셋, EPT 재구축 |
| LSFMM 2023 CXL VM migration | CXL 공유 메모리로 VM migration | 같은 계열, M3로 비교 |
| Pond (ASPLOS'23) | VM 단위 로컬·pool 메모리 배분을 ML로 예측 | 정적, tool call 단위 아님 |
| DSec (DeepSeek, arXiv 2609.22978) | power-of-k 배치 + SCHED_IDLE + core scheduling, migration 없음. sandbox 약 90%가 평균 CPU 5% 이하 | 노드 간 재배치 없음 |
| FragVisor (EuroSys'23, "Aggregate VM") | 여러 서버에 걸친 분산 VM. 메모리는 DSM, vCPU를 서버 사이로 옮긴다. vCPU 이동 평균 86 µs(그중 register dump 38 µs) | 메모리를 page 단위로 끌어오고 EPT를 서버마다 따로 채운다. 86 µs에 이 비용이 없다(8.1) |

"실행 중인 VM / EPT·VMCS가 CXL에 있음 / host 간 재구축 없이 넘김 / 비일관 CXL 위 read-write 소유권 이전"을 함께 만족하는 선행 연구는 찾지 못했다. 2025~2026 학회와 arXiv, GiantVM 같은 분산 hypervisor는 더 확인해야 한다.

### 8.1 FragVisor의 86 µs (2026-11-12 코드 분석)

논문 7.3절: "inter-node migration overhead of a vCPU: it is 86 µs on average – including 38 µs to dump registers". 환경은 Xeon E5-2620 v4, 56 Gbps InfiniBand RDMA, Linux 4.4.137 + Popcorn, kvmtool이다. 공개 코드(`systems-nuts/FragVisor`, kernel `bb358fa`, kvmtool `840c22a`)를 읽어 확인했다.

**하는 일**
1. guest가 hypercall로 이동을 요청한다. KVM이 `KVM_RUN`에서 빠져나오며 커널 안에서 vCPU 상태를 저장한다(`pophype_save_vcpu_states`). 저장 대상은 regs, sregs, FPU, XSAVE, XCR, LAPIC, events, MP state, MSR 14개로 약 6.9 KB다.
2. 이 상태와 `struct kvm_vcpu` 원본을 RDMA SEND로 목적지에 보낸다.
3. 목적지는 **부팅 때 미리 만들어 둔** 같은 vCPU fd를 찾아 VMCS를 load하고 상태를 커널 함수로 복원한다. 서버마다 VM fd와 모든 vCPU fd, VMCS를 부팅 때 만든다.
4. 이어서 Popcorn이 kvmtool의 vCPU thread를 목적지로 옮긴다(host register만, RAM은 아님).
5. 목적지에서 `KVM_RUN`을 다시 부른다.

**하지 않는 일**
- guest 메모리를 옮기지 않는다. 목적지에서 EPT violation이 날 때마다 DSM이 4 KiB page를 원격에서 가져와 그 서버의 EPT에 채운다.
- source EPT root를 목적지에 넘기지 않는다. 그 대입 줄은 주석 처리돼 있다(`hype_kvm.c:3045`). EPT는 서버마다 따로 있고, 그 서버에 이미 있던 매핑만 남는다.

**86 µs에 대해 확인한 것**
- 계측 경계와 원자료가 공개 코드에 없다. 상태 전송 구간 타이머(`update_vcpu_ns`)는 쌓기만 하고 어디서도 출력하지 않는다. 86 µs 집계 스크립트, 표본 수, 분포도 없다(평균 하나).
- 옮긴 뒤 page를 가져오고 EPT를 채우는 비용, cache·TLB warm-up은 이 숫자 밖이다.
- 코드에 "1 vCPU per node" 가정이 있다.
- InfiniBand 없이 쓰는 TCP messaging layer는 코드에 있다. 다만 수정된 4.4 host kernel이 필요해서 우리 서버(Xeon Gold 6548Y+)에서 그대로 돌리기는 어렵다.

**우리 연구에 주는 의미**
- FragVisor의 이동은 HANDOFF 원래 기본안(상태 직렬화 + 목적지에 미리 만든 vCPU)과 같은 단계다. 우리 표의 `reuse-prepared`(KVM 객체 사전 생성)에 해당하고, EPT 이어받기는 없다.
- 그래서 "상태만 넘기는 부분은 bare metal + RDMA에서 100 µs 안쪽"이라는 참고값이 이미 있다. 리뷰어는 우리 숫자를 이것과 견줄 것이다. 우리 장점은 상태 전달 속도보다 **옮긴 뒤 메모리를 끌어오거나 EPT를 다시 채우지 않는 것**에서 보여야 한다(6.2 D3, D4).

**같은 경계로 잰 우리 숫자 (2026-11-12, bud05 NUMA node 1, nested lab)**

경계는 source L1에서 L2 vCPU의 마지막 VM-exit 직후(`__vmx_vcpu_run` 반환)부터 목적지 L1의 첫 `__vmx_vcpu_run` 호출 직전까지다. FragVisor의 경계는 공개되지 않아서 이 경계가 가장 가까운 해석이다. 두 L1의 TSC 차이는 공유 메모리 ping-pong(200회, 최소 RTT)으로 보정했고 오차는 약 0.09 µs다. A→B와 B→A를 평균해 보정과 무관하게 낸 값도 같았다. 조건마다 warmup 10 + 측정 100회다. 원자료는 [results/vm-handoff-fragvisor-boundary-20261112](results/vm-handoff-fragvisor-boundary-20261112/README.md).

| 경로 | p50 / p95 (µs) | 평균 | 주된 구간 (p50, µs) |
|---|---:|---:|---|
| FragVisor (논문, bare metal + RDMA) | ? | 86 | register dump 38 |
| direct, 2 MiB CPU-only guest | 37 / 44 | 42 | source detach 8, 알림 0.3, 목적지 attach~entry 29(그중 kernel attach 23) |
| direct, 128 MiB Linux guest | 288~295 / 343~348 | 301~314 | source detach 96(Firecracker 쪽 129, native 쪽 50), 목적지 알림 관측 → entry 196(B가 목적지일 때 executor futex wake 약 104, kernel attach 38, `KVM_RUN` → entry 26) |
| reuse-prepared (FragVisor와 같은 방식) | 구간 합 1,125 / 1,190 | | save API 442, restore API 586, 상태 14,264 B(MSR 190개) |

- 목적지가 공유 메모리 알림을 50 µs 간격으로 확인하면 CPU guest가 93 µs, Linux guest가 402 µs로 늘어난다. 위 표는 busy-poll 기준이다. RDMA 수신 polling과 비슷한 조건이다.
- direct가 hand-off 한 번에 넘기는 직렬화 상태와 RAM·EPT 복사는 0 B다. 공유 메모리에 쓰는 protocol 값은 160~184 B(token 136 B 포함)다.
- Linux guest에서 가장 큰 덩어리는 목적지 executor를 futex로 깨우는 약 104 µs다. busy-poll executor로 바꾸면 줄어들 것으로 **추정**하지만 아직 안 해 봤다. source 쪽도 Firecracker 경로(129 µs)가 native 경로(50 µs)보다 길다.
- reuse-prepared는 exit → entry를 정확히 재지 못했다. 표의 값은 pause가 끝난 뒤의 API 구간 합이다. FragVisor의 커널 안 저장(38 µs)보다 느린 이유는 Firecracker API·thread 통신·직렬화와 더 넓은 상태 범위다(ioctl별 기여는 나누지 않았다).

**옮긴 뒤 작업 데이터 다시 읽기 (512 MiB guest, working set 약 430 MiB)**

| 모드 | 반복 방문 first − warm scan (ms, p50) | 목적지 L1 EPT violation (첫 방문 / 반복) |
|---|---:|---:|
| direct | 0.09 | 0 / 0 |
| reuse-prepared | 0.17 | 29 / 0 |
| snapshot (매번 새 KVM VM) | 795 | 110,388 / 110,388 |

- 반복 방문에서는 direct와 reuse-prepared 모두 비용이 거의 없다. reuse 계열은 노드마다 이전 방문의 EPT가 남기 때문이다. FragVisor도 노드별 EPT가 남는다. 다만 다른 노드에서 쓴 page는 DSM으로 다시 가져와야 하고, 그 비용은 이 lab이 재현하지 못한다.
- 빈 EPT에서 시작하는 snapshot은 매번 약 11만 번의 L1 EPT violation과 0.8초를 쓴다(nested라서 한 번에 약 7 µs).
- **첫 방문은 direct가 오히려 느렸다.** first − warm이 direct 75 ms, reuse 7 ms다(각 n=1). direct의 L1 EPT violation은 0이었다. 그래서 75 ms는 바깥 L0가 B용 shadow EPT를 새로 채우는 nested 전용 비용으로 **추정**한다. L0 tracing은 sudo가 필요해 하지 않았다. reuse의 B는 측정 전에 같은 backing으로 seed guest를 한 번 띄워 두어 이미 데워져 있었다. 그래서 첫 방문 비교는 한 가지 요인만 바꾼 비교가 아니다.
- 하드웨어와 무관한 근거는 EPT violation 수다. direct는 목적지 L1이 EPT를 다시 채우지 않는다(0회). 시간 차이는 물리 서버에서 다시 재야 한다.

**한계**
- nested라 L1 → L2 VM-entry와 VMCS 명령이 L0를 거친다. kernel attach 23~38 µs에도 이 비용이 섞여 있다.
- 두 L1이 coherent 메모리를 공유하므로 write-back·invalidate 비용이 없다.
- 첫 방문은 n=1이다.

---

## 9. 서버와 실행 규칙

| 서버 | 구성 | 주의 |
|---|---|---|
| bud05 | Intel Xeon Gold 6548Y+ 2소켓. node 0 = CPU 0-31 + DRAM 약 128 GB, node 1 = CPU 32-63 + DRAM 32 GB, node 2 = CPU 없는 CXL | **NUMA 0만 쓴다.** 예외: 11-12 FragVisor 경계 측정은 node 0이 다른 실험 중이라 사용자 지시로 node 1만 썼다(CPU 32-63, lab 10 GiB 이하). governor `performance`(영구), turbo 켬(약 4 GHz). nosmt로 부팅하지만 runtime에 SMT를 켰고 NUMA 1 sibling은 offline(재부팅하면 꺼짐). dockerd는 cpuset CPU 64 이상을 거부한다 |
| bud05 node 2 | 4 host가 공유하는 CXL DCD. bud05는 host 2 | 부팅 뒤 `cxl_pci`를 다시 probe하고 `dcdctl add -H 2 -R 0`. **block을 손으로 online하지 않는다.** add/release는 사용자 승인 뒤에만. region 1은 다른 프로젝트가 쓴다 |
| bud04 | kernel 6.18.0-m3, dax0.0·dax2.0 각 256 GiB devdax | NUMA 0만 쓴다. root는 되지만 `/Serverless` 아래 다른 내용은 건드리지 않는다. AC 전원 문제로 두 번 꺼졌다(10-18, 10-27) |

- 10-14 DRAM/CXL 측정 서버는 문서에 "Niagara"로 적혀 있다. kernel(`6.6.0-rc6-niagara-…`)이 bud05와 같다.
- 예전 문서의 "turbo 꺼짐, 2.5 GHz"는 `schedutil`이 boost를 요청하지 않아 생긴 오해다.
- 실험은 CPU pinning과 관리용 코어(0-1) 분리를 쓴다. 조건마다 반복하고 순서를 무작위로 하며, 제외 기준을 미리 정한다.
- git: push는 사용자가 명시적으로 허락할 때만 한다. origin 이력은 11-04에 다시 썼다. local commit은 origin 위로 다시 얹고, 옛 이력을 merge하지 않는다.

---

## 10. 자료 위치

| 위치 | 내용 |
|---|---|
| `harness/trace-replay/` | Docker trace 재생 harness(`experiment.py`, `analyze.py`). 사용법은 [README](../README.md) |
| `experiments/contention/` | microVM·Docker 경합 실험, fleet simulator, pool 이동, trace 분석 스크립트. 색인은 [README](../experiments/contention/README.md) |
| `experiments/rollout-trace/` | RL trace 수집 시간 추정 |
| `implementation/virtual_machine/` | hand-off 구현(KVM·Firecracker patch, mapper, lab). 사용법은 [README](../implementation/virtual_machine/README.md) |
| `implementation/virtual_machine/results/` | hand-off 구현 단계별 검증 기록(10-20~11-03) |
| `docs/results/` | motivation 측정과 lab 재검증 원자료 |
| 태그 `docs-pre-merge` (`091cd67`) | 합치기 전 문서 전부(`docs/*.md`, `docs/evidence/`, `experiments/contention/`의 PLAN·RESULTS 문서, pooling 결과 디렉터리) |
| 태그 `vm-pre-cleanup-20261111` (`184167c`) | 코드 정리 전 VM 구현과 pooling 실험 |
| `~/cxl-page-cache` | page cache 공유 프로젝트(별도 repo) |

데이터셋:
- 다언어 trace: `SWE-bench/20260902_mini-v2.4.6_gemini-3-5-flash`, commit `8dcca98587e7f698753903e3b1747eb5dc013cb1`. trace 264개, 명령 13,600개. 목록과 SHA-256은 `harness/trace-replay/data/inventory.json`, image digest는 `docs/results/numa-immediate-20261014/image-lock.json`에 있다.
- **LLM 대기**: 이전 tool timestamp에서 다음 assistant timestamp까지다. mini-SWE-agent는 응답을 파싱한 뒤 시각을 적는다. 그래서 이 값은 순수 추론 시간이 아니라 관측된 응답 간격이다. token 단위 타이밍은 없다.
