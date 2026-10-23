# CXL remote NUMA 기반 microVM handoff 상태 분석과 초기 실험

2026년 10월 14일

이 문서는 **2026년 10월 14일의 초기 설계·NUMA 실험 기록**이다. 아래의 구현 계획과
미구현 표시는 당시 기준이며, 이후 구현의 현재 상태를 나타내지 않는다. 2026년 10월 23일
기준 공유 DAX RAM, state snapshot·worker 재사용, KVM 객체 direct handoff, 크기별 migration
비교와 PML cache 관리 계측은 [현재 구현·사용법](../implementation/virtual_machine/README.md)에
정리했다. MPTCP 연결을 유지한 guest-client snapshot 이전 결과와 다음 구현 순서는
[세션 인계](../implementation/virtual_machine/README.md#session-handoff)와
[MPTCP 실행 검증](../implementation/virtual_machine/README.md#mptcp-session)에 있다.
EMR·DSA 및 주기적 preflush 논의도 그 문서에서 구현·측정 결과와 구분한다.
이 문서의 `.local/` 도구·이미지·원시 로그는 Git 배포 대상이 아니며, 아래 명령은 당시 실행 기록이다.

현재의 목표는 CXL을 remote NUMA memory로 모델링하고, guest RAM을 그 메모리에 둔 microVM의 실행 위치를 바꿀 때 무엇을 유지하고 전달하며 재구성해야 하는지 밝히는 것이다. 첫 단계는 Firecracker 설치와 부팅, remote NUMA 배치, pause/resume, 별도 VMM에서의 상태 복원이다. 그다음 guest RAM 복사를 제거하는 handoff를 구현한다.

핵심 가설은 **guest RAM을 고정하면 handoff의 주요 작업을 CPU·장치 상태 전달, 목적지 실행 환경 재구성, 실행 권한 이전으로 줄일 수 있다**는 것이다. microVM이 guest 커널 상태를 캡슐화하더라도 host의 KVM 객체와 I/O backend까지 guest RAM에 들어가는 것은 아니다.

## 연구 배경과 이번 단계의 범위

기존 논의의 출발점은 sandbox 생성 시점의 routing만으로는 배치 이후의 부하 변화와 warm sandbox의 위치를 함께 해결하기 어렵다는 점이다. 오래 유지되는 sandbox의 메모리는 두고, 대기 중인 실행 문맥을 여유 있는 CPU에서 재개하는 것이 목표다. 초기에는 VM 전체를 한 실행 위치에서만 가동한다.

이번 단계는 상태 분류와 최소 실행 검증에 집중한다. DSec의 상세 수치, 선행 연구의 우선권, 학회 적합성은 이 단계의 성공 조건으로 사용하지 않는다. 후속 단계에서 실제 workload의 배치 후 불균형을 측정하고, routing·snapshot/restore·live migration과 비교한다.

## 실험 모델과 의미

| 단계 | 구성 | 확인할 수 있는 것 |
| --- | --- | --- |
| 한 호스트의 remote NUMA | CPU node 0, guest RAM node 1 | 메모리 배치, 부팅, 실행과 메모리 위치 분리 |
| 같은 호스트의 두 VMM | 서로 다른 CPU node의 VMM이 동일 backing을 순차 사용 | 상태 export/import, 프로세스 독립성, 공유 RAM 재사용 |
| 여러 호스트의 공유 CXL | 다른 커널의 KVM들이 동일 pool 영역을 순차 사용 | 실제 host 간 handoff, fencing, visibility |

첫 두 단계의 NUMA 시스템은 cache coherent하다. 이 결과로 비일관성 CXL의 flush/invalidate 프로토콜이 검증되지는 않는다. 또한 CXL pooling이 모든 호스트의 동일 페이지 동시 접근을 자동으로 보장하지 않는다. 최종 장비에서는 공유 접근 또는 재할당 방식과 host별 주소 매핑을 확인해야 한다.

초기 구성은 x86_64, Firecracker, 1 vCPU, 128MiB RAM, initramfs 기반 guest다. 디스크와 네트워크를 제외해 CPU·RAM·timer 상태부터 관찰한다. 이후 virtio-blk, virtio-net, vsock을 하나씩 추가한다. CPU 모델과 guest에 노출하는 기능, VMM·snapshot 형식은 실험 사이에 고정한다.

## Handoff 시 상태를 처리하는 방법

아래 표는 구현할 handoff의 분류다. 기존 Firecracker가 이 전체 동작을 제공한다는 뜻은 아니다.

| 상태 | 현재 위치와 예 | Handoff 처리 |
| --- | --- | --- |
| Guest RAM | guest kernel, process heap/stack, guest page table, guest page cache | 같은 pool 페이지 유지. local tier가 있다면 잔여 페이지를 별도로 회수 |
| vCPU 실행 상태 | KVM 안의 GPR, RIP/RFLAGS, control·segment·debug registers | vCPU별 export/import |
| 확장 CPU 상태 | MSR, XSAVE, XCR, CPU feature 설정 | 저장·복원 및 목적지 호환성 검증 |
| CPU 이벤트 | LAPIC, pending interrupt/exception, MP state | 정지 경계에서 함께 저장·복원 |
| VM interrupt와 시간 | PIC, IOAPIC, PIT, KVM clock, TSC 관련 상태 | export/import. 중단 시간과 timer 만료 정책 정의 |
| 메모리 영역 설명 | guest physical address, 크기, backing offset, page size, slot 구성 | 설명자 전달. 목적지 host virtual address와 KVM memory slot 재생성 |
| Virtio의 guest 측 데이터 | descriptor, available/used ring, I/O buffer | guest RAM에 유지 |
| Virtio의 VMM 측 상태 | queue 진행 위치, device feature/status/config, interrupt 상태 | drain 이후 저장·복원. ring 데이터만으로 충분하지 않음 |
| Host 실행 객체 | VM/vCPU fd, eventfd, irqfd, ioeventfd, epoll, worker thread | 목적지에서 재생성·재연결. fd 번호와 포인터는 전달 불가 |
| 디스크 내용 | rootfs, writable layer, 완료된 write | 동일 backing 접근 보장 또는 별도 데이터 이동. RAM snapshot에 자동 포함되지 않음 |
| 네트워크와 vsock | guest TCP 상태, TAP, host socket, NAT/conntrack, 연결 endpoint | guest 부분 유지와 host 경로 재구성을 분리. 연결 연속성 정책 필요 |
| 제어 정보 | VM ID, owner, epoch, backing ID, state format | 전달·검증하고 이전 실행 주체를 차단 |

설치 버전의 CPU 상태 출발점은 Firecracker의 `VcpuState`이며, VM 공통 상태는 `VmState`다. CPU 상태의 세부 필드와 VM의 interrupt/clock 필드는 upstream 구현에 명시되어 있다. [vCPU 상태 소스](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/arch/x86_64/vcpu.rs), [VM 상태 소스](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/arch/x86_64/vm.rs).

실제 계측에서는 `MicrovmState`를 출발점으로 `vm_info`, `kvm_state`, `vm_state`, `vcpu_states`, `device_states`의 직렬화 크기와 저장·복원 시간을 나눈다. guest RAM은 별도 항목으로 측정한다. [Snapshot 구현](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/persist.rs).

## 기존 논의에서 바로잡을 부분

**VMCS와 EPT를 통째로 전달하지 않는다.** 일반적인 KVM userspace VMM에서는 guest의 architectural state를 KVM API로 읽고 복원한다. 하드웨어 실행 구조인 VMCS와 host physical address에 의존하는 EPT는 목적지 KVM이 관리한다. guest page table은 guest RAM에 남지만, 목적지의 host 매핑과 second-stage translation은 새로 구성된다. `VMCLEAR → VMPTRLD`는 비유로만 남긴다. [KVM API](https://www.kernel.org/doc/html/latest/virt/kvm/api.html).

**LLM 대기와 안전한 정지는 다르다.** guest의 background process, timer, DMA 또는 VMM의 비동기 I/O는 대기 중에도 동작할 수 있다. HLT만으로 quiescence를 판정하지 말고 모든 vCPU의 정지와 backend 접근 중단을 확인한다.

**메모리 복사를 없애도 상태 표현은 필요하다.** 작은 CPU·장치 상태의 직렬화는 허용하고, 제거할 비용을 guest RAM의 bulk copy로 정의한다. 상태를 CXL에 기록하더라도 쓰기량과 게시 비용을 계측한다.

**공유 RAM에 상태가 남는 것과 장애 복구는 다르다.** 갑작스러운 host 장애에서는 최신 CPU 상태나 dirty cache가 손실될 수 있다. 이번에는 정상적인 정지 후 handoff를 먼저 구현하며, 무손실 장애 복구는 별도 checkpoint·일관성·fencing 설계가 필요하다.

## Firecracker에서 시작할 구현 지점

Firecracker는 첫 실험 VMM으로 사용한다. 기존 snapshot 경로로 상태 경계를 관찰하고, 이후 guest memory backend를 수정한다. 기본 File snapshot 복원은 `MAP_PRIVATE`를 사용하므로 쓰기 시 COW가 발생할 수 있다. 따라서 “snapshot을 NUMA 1에서 복원했다”는 결과는 “같은 writable CXL 페이지를 계속 쓴다”는 결과와 다르다. Vsock도 snapshot/restore에서 transport reset이 발생하므로 기존 연결 유지가 기본 보장은 아니다. [Snapshot 지원 문서](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/snapshot-support.md).

수정 후보는 다음 세 가지다.

1. Guest RAM을 명시적 backing ID와 offset으로 생성하고, 목적지에서 같은 영역을 여는 경로를 추가한다. 같은 호스트에서는 shared mapping으로 시작하되, host 사이에서는 실제 공유 CXL 접근 방식에 맞춘다.
2. RAM dump와 CPU·VM·장치 상태 export를 분리한다. 초기에는 기존 직렬화 구조를 최대한 재사용한다.
3. 목적지의 메모리·KVM·backend 재구성이 완료되고 source의 접근이 차단된 이후에만 실행을 허용하는 소유권 절차를 추가한다.

`numactl --membind=1`은 guest RAM만이 아니라 VMM의 다른 신규 할당에도 적용된다. 초기 기능 검증에는 사용할 수 있지만, 이후에는 guest RAM VMA와 VMM heap을 나눠 배치·측정해야 한다. 공유 file page의 기존 NUMA 위치도 정책만으로 바뀌지 않으므로 `/proc/PID/numa_maps`와 `smaps`를 실제로 확인한다.

## 최소 handoff 절차

1. 목적지에서 CPU/KVM/VMM 호환성, backing 접근, 장치 자원을 준비한다. vCPU는 실행하지 않는다.
2. Source에서 신규 요청을 차단하고 모든 vCPU를 정지한다. I/O를 drain하고 backend가 RAM을 더 쓰지 않는 경계에 도달한다.
3. CPU·VM·장치 상태를 저장한다. RAM의 local tier 잔여분이 있으면 회수한다. 비일관성 장비의 visibility 처리는 별도로 정의한다.
4. Source의 실행·메모리·I/O 접근 권한을 회수한 뒤 coordinator가 새 epoch의 소유권을 게시한다. 실패 시 어느 쪽이 재개할 수 있는지 함께 정의한다.
5. Destination은 epoch와 backing을 검증하고 host mapping, KVM memory slots, backend 객체와 CPU·장치 상태를 복원한다.
6. Destination만 실행하고 라우팅을 전환한다. 이전 epoch의 재개 요청은 거부한다.

초기 NUMA 실험에서는 한 coordinator로 순서를 강제한다. 여러 호스트의 비일관성 CXL에서는 이 절차만으로 충분하지 않다. 특히 정렬된 8바이트 store의 host 간 원자성, `clwb`·fence의 도달 범위, destination invalidate, UC/WC 매핑 속성은 장비 보장을 확인해야 한다. `clflush`를 무조건적인 invalidate로 사용하면 stale dirty line의 writeback 문제가 생길 수 있으므로 이전 owner의 clean·revoke와 destination의 stale 상태 처리 순서를 함께 설계한다.

## 실험 단계와 완료 조건

| 순서 | 작업 | 완료 조건 |
| --- | --- | --- |
| 1 | Firecracker 설치와 최소 guest 부팅 | KVM 접근 확인, serial 출력과 counter 증가 |
| 2 | CPU 0과 memory 1 배치 | vCPU affinity와 guest RAM VMA의 NUMA 위치 확인 |
| 3 | Pause와 resume | 정지 중 counter 출력 중단, 재개 뒤 연속 증가 |
| 4 | 기본 snapshot과 새 VMM 복원 | source 종료 후 같은 guest 상태 재개, state/RAM 파일 크기 분리 |
| 5 | 동일 RAM backing의 두 VMM handoff | private COW 없이 같은 backing 영역 사용, A→B→A 상태 연속성 |
| 6 | 장치별 상태 추가 | block write, pending timer, 네트워크·vsock 재연결의 명시적 검증 |
| 7 | 여러 호스트로 확장 | fencing·중복 실행 방지·부분 실패·실제 memory visibility 검증 |

4단계까지는 snapshot baseline이며 5단계가 연구용 handoff의 첫 구현이다. CPU affinity만 다른 node로 옮기는 실험은 같은 KVM 객체를 유지하므로 host 간 handoff 비용을 보여주지 않는다.

Handoff 비용은 `stop + drain + memory visibility + state export + ownership + destination rebuild + state import + resume`으로 나눠 기록한다. 전송 바이트도 guest RAM, CPU·VM·장치 상태, 제어 메시지, local tier 회수량을 구분한다. 복사가 없더라도 cache writeback·page fault·EPT 재구성·cache warming 비용이 남는다.

RAM checksum, counter, timer, 파일 write sequence를 이용해 연속성을 확인한다. Handoff 도중 실패와 반복 A→B→A도 추가한다. 최소 counter 실험 하나의 성공을 전체 VM 상태의 정확성으로 확대 해석하지 않는다.

## 초기 실험 결과

2026년 10월 14일 현재 작업 호스트에서 다음을 실행했다. Firecracker는 시스템 패키지를 변경하지 않고 작업 디렉토리의 `.local/microvm/`에 설치했다. 실험 VMM 프로세스는 종료했고 이미지와 로그는 보관했다.

| 항목 | 결과 |
| --- | --- |
| KVM 접근 | 호스트 계정으로 `/dev/kvm` 접근, KVM API version 12 |
| VMM | Firecracker v1.17.0, 공식 release의 SHA-256과 다운로드 파일 일치 |
| Guest | 공식 CI kernel 6.1.188, host의 static BusyBox로 만든 initramfs |
| 구성 | 1 vCPU, 128MiB, block/network/vsock 장치 없음 |
| Source 실행 | CPU node 0에 제한, 관측된 vCPU 실행 CPU 1 |
| Source RAM 배치 | 128MiB guest VMA의 resident 11,846개 4KiB 페이지 모두 node 1, 약 46.3MiB |
| Pause와 resume | counter 2에서 2초간 출력 중단, 재개 후 3 |
| Snapshot | state 13,652 bytes, memory 134,217,728 bytes |
| 새 VMM 복원 | source 종료 후 CPU node 1에서 복원, 관측된 vCPU 실행 CPU 22 |
| Guest 연속성 | 새 boot marker 없이 counter 3에서 4로 진행 |
| 복원 RAM | 128MiB private file mapping, 관측 resident 3,695개 페이지 모두 node 1, 이 중 anonymous COW 265개 |

128MiB 전체를 touch한 실험은 아니다. NUMA 배치 결과는 관측 시점의 resident 페이지에 대한 결과다. 또한 memory snapshot은 실제 파일에 기록했으며 동일 RAM backing handoff는 아직 구현하지 않았다. 디스크·네트워크와 다중 vCPU의 복원 정확성도 다음 단계에서 확인한다.

이번 실험은 1~4단계의 최소 기능을 확인했다. **바로 다음 작업은 Firecracker의 RAM dump를 분리하고, 같은 shared backing에 CPU·VM·장치 상태만 복원하는 경로를 만드는 것이다.** 첫 구현에서도 새 프로세스를 사용하고 A→B→A를 반복해 source VMM의 숨은 상태에 의존하지 않는지 확인한다.

### 당시 실행 명령과 보관 자료

당시 작업 디렉토리는 `/home/ijkim/agent-sandbox-cxl`이었다. `.local/microvm/smoke.py`는
일회성 검증 도구이며 기존 trace replay harness와 독립적이다. 아래는 로컬 도구와
binary·kernel·initramfs가 준비된 호스트에서 사용한 명령이다. 해당 파일은 Git에 포함되지
않으므로 새 clone에서 이 명령만으로 재실행할 수 없다. 현재 지원하는 실행 경로는
[구현 README](../implementation/virtual_machine/README.md)의 `lab.py`다.

```bash
cd /home/ijkim/agent-sandbox-cxl
python3 .local/microvm/smoke.py
```

실행마다 별도 결과 디렉토리를 생성한다. 이번 결과는 `.local/microvm/run-rksnfxiq/`의 `report.json`, source/destination serial 로그, `numa_maps`, `smaps`, thread CPU 기록에 남아 있다. 바이너리와 RAM snapshot은 Git에서 제외했다.

위 최초 실험은 공식 v1.17.0 x86_64 배포 바이너리로 수행했다. 이후 개발 환경은 아래의 공식 소스 빌드로 전환한다. Guest kernel은 공식 CI의 `firecracker-ci/20261008-40f3a4819490-0/x86_64/vmlinux-6.1.188`을 사용한다. kernel 다운로드 후 계산한 SHA-256은 `6e105082d617064296680262ddf53844f292da803165e74e1a4f051940de48ef`다. [Firecracker release](https://github.com/firecracker-microvm/firecracker/releases/tag/v1.17.0), [공식 kernel 이미지](https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261008-40f3a4819490-0/x86_64/vmlinux-6.1.188).

## 공식 소스 개발 환경

2026년 10월 14일 release 소스 빌드와 최소 실행 검증을 완료했다. 새 바이너리로 부팅, 2초 pause, resume, source 종료 후 새 VMM에서 snapshot 복원이 통과했으며 counter는 2→3→4로 이어졌다. 결과는 `.local/microvm/run-n5nmd9dn/report.json`, 빌드 로그는 `.local/microvm/source-build.log`에 있다. 기존 `.local/microvm/release-v1.17.0-x86_64/`와 `firecracker-v1.17.0-x86_64.tgz`는 삭제했다. Guest kernel, initramfs, 기존 실험 로그는 유지한다.

소스 저장소는 `/home/ijkim/agent-sandbox-cxl/firecracker`에 있다. Remote `origin`은 `https://github.com/firecracker-microvm/firecracker.git`이며, v1.17.0의 commit `95f868c8e345b1cc8faccd1a3c910b4989dc3f58`에서 수정용 브랜치 `codex/cxl-handoff`를 만들었다. 상위 연구 저장소에서는 `/firecracker/`를 ignore하고, Firecracker 코드 변경은 이 독립 Git 저장소에서 관리한다.

소스 수정 후 다음 명령으로 다시 빌드한다. Docker 접근 권한이 필요하며, 공식 개발 이미지 `public.ecr.aws/firecracker/fcuvm:v93`가 Rust 등 빌드 도구를 제공한다. Host에 Rust를 별도로 설치할 필요는 없다.

```bash
cd /home/ijkim/agent-sandbox-cxl/firecracker
./tools/devtool -y build --release
./build/cargo_target/x86_64-unknown-linux-musl/release/firecracker --version
```

실행 바이너리는 `/home/ijkim/agent-sandbox-cxl/firecracker/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker`다. 같은 디렉토리에 jailer 등 도구도 생성된다. 시스템 경로에 복사하지 않고 빌드 산출물을 직접 사용하므로 재빌드 결과가 다음 검증 실행에 반영된다. Debug 빌드가 필요하면 `./tools/devtool -y build`를 사용하며 산출물은 같은 target 아래 `debug/`에 생성된다.

소스 수정의 주요 진입점은 다음과 같다.

| 파일 | 역할 |
| --- | --- |
| `firecracker/src/vmm/src/vstate/memory.rs` | guest RAM 생성, memfd shared mapping, snapshot private mapping |
| `firecracker/src/vmm/src/persist.rs` | snapshot 생성과 복원, RAM과 실행 상태를 분리할 지점 |
| `firecracker/src/vmm/src/arch/x86_64/vcpu.rs` | vCPU 상태 저장과 복원 |
| `firecracker/src/vmm/src/arch/x86_64/vm.rs` | VM 공통 interrupt와 clock 상태 |
| `firecracker/src/vmm/src/device_manager/persist.rs` | 장치 상태 저장과 복원 |

## SWE trace의 microVM 실행 확인

2026년 10월 14일 `gin-gonic__gin-3741` trace를 Firecracker guest 안에서 끝까지 재생했다. 27개 명령을 모두 실행했고, 원본에 관측 종료 코드가 있는 26개는 모두 일치했다. `go test ./...` 3회도 성공했으며, 최종 제출 출력에서 marker 한 줄을 제거한 내용의 SHA-256은 원본과 정확히 일치했다. 이는 기록된 작업의 재생 확인이며 SWE 정답 채점 결과는 아니다.

### 입력과 guest 구성

이 호스트에는 기존 trace inventory와 해시만 있어, 고정 commit의 trace 하나를 다운로드하고 크기와 SHA-256을 검증했다. 입력은 `harness/trace-replay/data/native/gin-gonic__gin-3741.traj.json`이며 SHA-256은 `5837d6d1dc2e53031ed8c4e11c122fd1f822097c0735c741ece201285947f997`이다.

작업 이미지는 기존 baseline에 고정한 `swebench/sweb.eval.x86_64.gin-gonic_1776_gin-3741@sha256:c134cc213a09fd5060dd3d3a70adde0eb6607750c111f397b163a306d0ee5a1e`다. 정지된 컨테이너에서 `docker export`로 파일시스템을 추출하고, guest init과 재생용 static BusyBox를 `/microvm`에 추가한 뒤 4GiB ext4 이미지로 만들었다. Trace 명령은 Docker에서 실행하지 않고 이 디스크를 부팅한 guest에서 실행했다.

| 항목 | 구성 또는 결과 |
| --- | --- |
| VMM | 공식 v1.17.0 소스 빌드 |
| Guest kernel | Linux 6.1.188 |
| 자원 | 4 vCPU, 4GiB guest RAM |
| Host 배치 | CPU 2–5, memory node 1 |
| 관측 메모리 배치 | 4GiB guest RAM VMA의 resident 48,549개 4KiB 페이지 모두 node 1 |
| 저장소 | 작업마다 복제한 writable virtio-blk root disk |
| 네트워크 | loopback만 활성화, virtio-net과 외부 연결 없음 |
| 명령 환경 | 원본 `/testbed`, `bash -c`, 이미지 환경변수와 trace의 명시적 환경변수 |
| 완료 명령 | 27/27 |
| 원본 종료 코드 비교 | 26/26 일치 |
| Go 테스트 | 3/3 성공 |
| 최종 제출 SHA-256 | `0e5003e771851819a41c7b1281f10fea23b077b0724221f1cb9859d1d80ac313`, 원본과 일치 |
| Guest 재생 구간 | 9.70초, 기록된 대기 간격 제외 |
| 부팅과 종료 정리를 포함한 관측 시간 | 약 11.08초, 디스크 복제 시간 제외 |

`grep`의 종료 코드 1과 `python3 --version`의 종료 코드 127은 원본에도 있는 결과다. Python을 guest에 설치해 이 동작을 바꾸지 않았다. 원본 시간창은 약 69.29초지만 이번에는 명령 사이 대기를 생략했으므로 두 시간을 성능 향상 비율로 비교하면 안 된다. 관측 NUMA 배치는 실행 초기에 실제로 할당된 페이지에 대한 결과이며 전체 4GiB를 touch한 검증은 아니다.

### Docker 이미지에서 추가로 준비해야 했던 것

Docker가 실행 시 제공하는 `/proc`, `/sys`, `/dev`, `/dev/pts`, `/dev/shm`, hostname, loopback과 `/etc/hosts`를 guest init에서 구성했다. 첫 실행에서는 `/etc/hosts`가 비어 있어 Go 테스트가 `localhost` 조회에 실패했다. Hostname 설정을 보완한 뒤 깨끗한 디스크로 재실행하자 세 테스트 모두 통과했다. Trace 명령과 테스트 소스는 이 보완 과정에서 수정하지 않았다.

현재의 최소 init에서는 작업 완료 후 디스크를 sync하고 guest를 halt한다. Host runner는 완료 marker와 halt 메시지를 모두 확인한 다음 해당 VMM만 종료하고 ext4에서 결과를 읽는다. 따라서 결과의 VMM 종료 코드 `-15`는 이 정리 절차의 SIGTERM이며 테스트 실패가 아니다. 정상 종료를 자동 감지하는 처리는 runner에 반영했다.

### 당시 실행 명령과 결과 위치

아래는 KVM과 로컬 root disk·runner가 준비된 호스트에서 사용한 명령이다. `.local/`의
도구와 디스크는 Git에 포함되지 않으며, 이 SWE trace의 microVM runner를 새 clone용
실행 경로로 배포한 것은 아니다.

```bash
cd /home/ijkim/agent-sandbox-cxl
python3 .local/microvm/swe-gin/run.py
```

당시 실행 준비 도구는 `.local/microvm/swe-gin/prepare.py`, 실행·비교 도구는 `.local/microvm/swe-gin/run.py`였다. 원본 trace 파싱과 제출 fingerprint 함수는 기존 `harness/trace-replay/experiment.py`를 재사용했다. 이 두 도구는 로컬 실행 확인용이며, 기존 replay harness에 범용 microVM backend를 통합한 상태는 아니다.

성공한 결과 디렉토리는 `.local/microvm/swe-gin/run-a0t5saiq/`다. `summary.json`에 명령별 원본·실제 종료 코드와 시간, 제출 비교 결과가 있으며, `manifest.json`에는 입력·환경·VMM 바이너리 해시가 있다. `serial.log`, `host.numa_maps`, `host.smaps`, `host.threads`와 `guest/results/`에 guest 출력·patch·kernel 로그를 보관했다. 최초 localhost 실패의 기록은 `run-zw_gcbiz/`에 남겼다.

검증 범위는 한 개 trace의 순차 실행이다. 나머지 263개 trace, 동시 실행, 원래 timestamp 재현, 외부 네트워크, snapshot/handoff 중 실행 연속성은 아직 확인하지 않았다. 명령 timeout은 60초로 두었으나 이번 성공 실행에서는 발생하지 않았다. 현재 guest runner는 timeout 시 명령을 종료하므로, Docker CLI만 종료하고 guest 작업이 남을 수 있는 기존 harness의 timeout 의미와 다르다. 범용 backend를 만들 때 명령 실행 통로와 이 timeout 동작을 명시적으로 맞춰야 한다.
