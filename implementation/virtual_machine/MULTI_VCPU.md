# 다중 vCPU direct hand-off 설계

2026-11-16. 기준 commit `675a9d5`. source와 destination의 가상 CPU(vCPU)
수와 ID는 같다. 지원 상한은 8개다. 1 → 2 → 4 → 8 순서로 확인한다.
CPU hotplug, 물리 host 사이 비일관 cache 유지보수, 장애 복구는 이 변경의 범위 밖이다.

## 상태와 수명

| 범위 | 공유 CXL에 남는 상태 | 각 Linux host에서 다시 연결할 상태 |
|---|---|---|
| VM 공통 | KVM VM 객체, memslot, RAM identity, owner/epoch, interrupt controller, APIC map, EPT page registry | mm, MMU notifier, SRCU, VM 목록, debugfs, PM notifier, PIT worker, clock work |
| vCPU별 | vCPU ID/index, KVM vCPU 객체, run page, VMCS, register/FPU, MMU root, LAPIC IRR/ISR와 pending timer | FD/thread, mutex 소유, pid/kick 대상, CPU 번호, preempt notifier, VPID, VMCS host state, pvclock PFN cache, LAPIC hrtimer |
| 장치 | 읽기 전용 block의 작은 의미 상태와 virtio queue | eventfd, IRQFD, ioeventfd, backend, event-loop 구독 |
| EPT | page/header 연결, root, page 수 | struct page backlink와 allocator anchor; VM별 host-local 마지막 방문 head |

APIC는 Advanced Programmable Interrupt Controller, EPT는 Extended Page Tables,
VMCS는 Virtual Machine Control Structure다. FD는 file descriptor,
SRCU는 Sleepable Read-Copy Update다. 기존 단일 arena/단일 VM 계약은 유지한다.

## ABI

공유 header magic과 ABI(Application Binary Interface) 버전을 2로 올린다.
token에 `abi_version`, `vcpu_count`, 최대 8개의 `{id, reserved, vcpu_offset,
vmcs_offset, run_offset, ept_root_offset}`을 추가한다. 기존 scalar identity 필드는
vCPU 0의 mirror로 남겨 진단과 기존 1 vCPU 코드의 의미를 유지한다.
모든 entry와 generation, RAM identity를 attach 전에 검증한다. 수가 다르거나,
ID가 중복되거나, 이전 ABI이면 소유권을 바꾸기 전에 거절한다.
공유 header에는 각 vCPU의 pointer, VMCS offset, guest TSC(Time Stamp Counter),
LAPIC timer active/remaining을 둔다. header와 diagnostic 영역은 겹치지 않게 한다.
Direct JSON schema와 resident binary packet 버전도 올린다.
`KVM_DAX_VCPU`의 scalar 인수는 vCPU index다. 인수 0은 기존 의미다.

## 넘기는 순서

1. VMM이 모든 vCPU에 Pause를 보내고 응답을 전부 받는다. 각 vCPU의 미완료
   port I/O·MMIO(Memory-Mapped I/O)는 기존 drain 경로로 끝낸다.
2. 커널은 index 순서로 **모든** vCPU mutex를 trylock한다. 하나라도 실행 중이거나
   미완료 I/O가 있으면 `EBUSY`로 거절하고 이미 잡은 lock을 모두 푼다.
   IRQFD 정리와 local RAM 전환도 같은 전체 정지 검사를 사용한다.
   KVM_RUN은 mutex를 잡은 뒤에도 owner/mm을 다시 검사한다. 대기 중이던 source
   thread가 소유권 이전 후 실행할 수 없어야 한다.
3. owner를 BUSY로 바꾸고 VM 공통 clock/PIT work와 모든 LAPIC timer를 동기 취소한다.
   pending interrupt와 IPI(Inter-Processor Interrupt)는 공유 LAPIC에 보존한다.
   EPT를 검사하고 모든 VMCS를 VMCLEAR한다. host별 pvclock cache를 전부 비활성화한다.
4. VM 공통 observer를 drain하고 vCPU별 pid·CPU·slot cache를 끊는다.
   모든 mutex를 푼 뒤 release store로 DETACHED와 새 generation을 공개한다.
   어느 thread도 source graph를 더 이상 수정하지 않는다.
5. destination은 token 전체를 먼저 검증하고 BUSY를 획득한다. VM 공통 연결을
   만들고 vCPU array **전체**를 복원한다. 이어 모든 preempt notifier, MMU anchor,
   VMCS host state, VPID, pvclock cache와 LAPIC timer를 연결한다.
6. 모든 vCPU에 TLB(Translation Lookaside Buffer) flush를 요청한다. nested lab에서는
   사용 중인 각 EPT root의 outer shadow 동기화도 수행한다. 마지막으로 OWNED를
   공개하고 모든 thread를 재개한다. 일부 attach 실패는 FAILED로 닫고 실행하지 않는다.

## interrupt와 동시성

- APICv와 posted interrupt, nested guest는 계속 끈다.
  destination의 vCPU array가 완성되기 전에는 timer나 PIT worker를 깨우지 않는다.
- PML(Page Modification Logging)은 기본 off다. `--pml on`으로 선택할 수 있다.
  Dirty bitmap과 PML page는 DAX에 둔다. detach에서 모든 PML index가 511인지 검사한다.
  비일관 hand-off의 장부·cache 순서는 [NONCOHERENT.md](NONCOHERENT.md)를 따른다.
- APIC map은 DAX에 있는 LAPIC만 가리킨다. 모든 ID와 pointer를 확인하며 source의
  RCU reader와 callback은 소유권 공개 전에 drain한다. IPI는 보존된 IRR/ISR와
  request bit를 사용한다. kick 대상 pid와 physical CPU 번호는 destination에서 갱신한다.
- 각 vCPU의 wait queue/preempt notifier는 host의 thread와 연결한다. source thread는
  잠든 상태로 남을 수 있지만 소유권 이전 뒤 shared object에 진입할 수 없다.
- VM clock과 PIT는 한 번만 처리한다. 각 vCPU의 TSC·pvclock·LAPIC timer는 따로 처리한다.
  timer cancel 뒤 pending 상태를 보존하고, 목적지에서 남은 시간을 기준으로 재등록한다.
- EPT page registry의 count는 atomic 증가로 고친다. registry publish와 count는
  vCPU가 모두 멈춘 시점에 검사한다. `seen_head`는 VM identity가 붙은 host-local 값으로
  두고, 전체 graph rebind와 각 vCPU root/TLB 처리를 구분한다.
- 다중 vCPU에서 추가로 발생하는 KVM host pointer와 static-key 사용은 실제 SMP
  (Symmetric Multiprocessing) 부팅·IPI·timer 시험으로 확인한다.

## 검증과 자원

기존 Python/C/Rust 단위 시험과 1 vCPU lab verify를 먼저 확인한다. 각 2/4/8 단계에서
최소 Linux의 CPU별 쓰기·checksum과 pinned thread ping-pong을 돌려 수백 번 왕복한다.
각 CPU의 timer/IPI 증가, 데이터 무결성, source 실행 중 detach 거절을 기록한다.
source Pause 요청부터 remote guest ACK(Acknowledgement)까지 warmup 10, 표본 100을 잰다.
실패 표본을 성공 latency에 섞지 않는다. nested 수치는 물리 multi-host 성능이 아니다.

작업과 출력은 `multi-vcpu` worktree에만 둔다. 기준 `.local`은 읽기 전용 입력이다.
NUMA(Non-Uniform Memory Access) node 0만 사용한다. 빌드는 nice 19, 최대 12 jobs,
quiet shared lock 아래 수행한다. lab은 전역 lab exclusive lock과 node 0 free-memory
예산 검사 뒤 L1별 4~6 GiB로 시작한다. 측정은 quiet exclusive lock을 잡는다.
실행 종료는 이 실행의 `lab.py stop`과 backing 정리를 반드시 수행한다.

## 구현 후 확인

2/4/8개 각각 205회 왕복과 실행 중 마지막 vCPU의 detach 거절을 통과했다.
각 단계의 기존 1개 경로, EPT를 버리는 mode 2도 확인했다.
4개 Gin fixture는 실행 중 B→A→B를 포함해 baseline과 결과가 같았다.
RAM mapper의 쓰기 fault에서 PTE(Page Table Entry)를 지우면 다른 vCPU의
PFN(Page Frame Number) 조회가 `EFAULT`로 실패할 수 있었다.
기존 PTE를 유지하고 write-protect 해제는 Linux의 `finish_mkwrite_fault`에 맡겼다.
native 실행기도 모든 vCPU FD·run mapping을 만든 다음 thread를 깨우도록 고쳤다.
수치와 남은 한계는 [결과](../../docs/results/multi-vcpu-handoff-20261116/README.md)에 있다.
