# CXL microVM 구현 지도와 기준 소스 대비 변경량

2026-11-11 갱신. 기존 handoff 구현의 기준은 `1c359875c`이며, 아래 원래 누적 변경량 표는 그 시점으로 고정한다. 후속 donor guest-cache 소스 `06f440013`의 추가 변경량은 별도 절에 기록한다. 최초 구상부터의 배경은 [전체 개요](cxl-microvm-overview.md), 실행 명령은 [구현 README](../implementation/virtual_machine/README.md), CPU handoff 제어 비용은 [resident direct](cxl-resident-direct.md)를 따른다.

2026-11-04 후속: [고정 CXL VM 사이의 donor guest-cache 읽기 공유](../implementation/virtual_machine/README.md#donor-guest-cache의-온라인-읽기-공유--고정-cxl--무한-수명-실험)를 별도 opt-in으로 추가했다. 이미지 prefill 없이 동작하며 아래의 기존 전환 경로 및 기준 커밋의 변경량과 구분한다.

2026-11-05 후속: [5개 cache 읽기 경로의 측정 기준과 결과](../implementation/virtual_machine/README.md#공유-범위와-읽기-지연의-측정-기준)를 추가했다. Guest hit/miss, Bloom positive 공유/실제 miss, Bloom negative를 구분하며 donor serial RPC 및 QEMU socket 실험망의 지연을 별도로 확인했다.

2026-11-05 채널 최적화: [전용 vsock 조회와 TCP 재사용](../implementation/virtual_machine/README.md#전용-guest-조회-채널과-tcp-재사용)을 구현했다. 동일 환경의 serial 대조군 대비 공유 읽기 중앙값은 13.5–14.2ms에서 2.83–2.87ms로 줄었으며, 5개 경로·연결 재사용·동일 donor frame 공유를 검증했다.

2026-11-05 TCP 복구: [cxld 실험을 철회하고 TCP/vsock 재사용 경로로 복구](../implementation/virtual_machine/README.md#cxld-실험-철회와-tcp-복구)했다. Busy polling이 donor vCPU 하나를 소비하므로 adapter·CXL transport 실행 옵션을 제거했다. 기존 실험 요약과 TCP의 남은 비용 분해, 공유 후 guest cache hit 검증은 유지한다.

## 현재 구현을 읽는 순서

1. **파일 경로:** guest ext4/OverlayFS → guest page cache → virtio-blk → host의 local 또는 NFS image FD.
2. **메모리 배치:** DRAM→CXL promotion은 공유 가능한 clean lower 데이터를 canonical backing에 연결하고 나머지 RAM을 복사한다. CXL→DRAM demotion은 현재 논리 RAM을 복사한다.
3. **CPU handoff:** CXL RAM을 유지한 채 state-only 또는 direct로 실행 owner를 옮긴다. Promotion/demotion과 다른 작업이다.
4. **Pooling:** VM별 inode/folio/페이지 테이블은 private이다. immutable 데이터 backing만 공유하며 쓰기는 private CXL CoW, 폐기는 ACK 뒤 재사용한다.
5. **Resident 제어:** 목적지 프로세스·disk FD·io_uring·제어 연결을 준비하고, 방문 이후 VM/vCPU FD·thread를 재사용한다. 정상 handoff의 전체 RAM 검사·mapping JSON·이미지 재해시는 없다.
6. **온라인 guest-cache 공유:** 위 canonical 기반 promotion과 별도의 `FC_PEER_CACHE_CONFIG` 경로다. 요청 VM의 cache miss 때 Bloom → donor guest 조회 → 기존 CXL frame 연결을 수행한다. 수명 고정·전환 제외 조건이며, [식별자·guest 변경·DSec과의 차이](cxl-data-pooling.md#online-peer-cache)를 먼저 확인한다.

<a id="peer-cache-source-delta"></a>

## 온라인 donor cache의 추가 소스 변경

구현 커밋은 `06f440013`이다. `5c9fcadb9`에 기록된 Firecracker patch와 현재 patch를
동일한 upstream에 적용한 결과끼리 비교하면 다음 5개 파일, **+759/-2줄**이 추가된다.
현재 upstream 대비 누적치는 **36개 파일, +7,013/-77줄**이다. 기존 표의 35개 파일 수치와
시점을 혼동하지 않는다. 원본 vendor checkout은 Git에 추가하지 않는다.

| Firecracker 경로 | 추가 | 삭제 | 역할 |
|---|---:|---:|---|
| `src/vmm/src/peer_cache.rs` | 733 | 0 | Bloom, donor TCP/vsock 조회, publish/attach, 선택적 계측, transport 테스트 |
| `src/vmm/src/builder.rs` | 3 | 0 | opt-in peer-cache 초기화 |
| `src/vmm/src/devices/virtio/block/virtio/device.rs` | 3 | 0 | RO·Sync 실험 경로의 구성 제한 |
| `src/vmm/src/devices/virtio/block/virtio/request.rs` | 19 | 2 | read 요청의 공유 분기와 기존 backend fallback |
| `src/vmm/src/lib.rs` | 1 | 0 | peer-cache 모듈 등록 |

저장소 자체 소스에는 `guest-pool.c`/UAPI의 resident-only `GUEST_POOL_LOOKUP`,
`pool_memory_module.c`/UAPI의 `CPM_PEER_PUBLISH`·`CPM_PEER_ATTACH`, guest vsock helper,
init script, assets 준비 및 기능/latency 실험 도구가 추가됐다. 실행 시 메모리 전수 조사나
이미지 canonical 사전 적재를 하지 않는다. donor pin 목록 검색은 최대 1,024개인 선형 탐색이고
파일 extent 검색도 선형이므로, 이것을 다중 donor용 상수 시간 index로 설명하면 안 된다.

게시 전 점검은 [구현 README](../implementation/virtual_machine/README.md#tcp-경로의-남은-비용)에
기록한다. 원격 main에 먼저 게시된 CPU contention·trace 분석 커밋은 그대로 보존한다.

## 기준 소스와 변경 규모

`+/-`는 `git apply --numstat`의 추가/삭제 줄 수다. 주석·테스트·선택적 초기 prototype도 포함한 **누적 소스 diff**이며 실행되는 명령 수나 이번 최적화만의 크기가 아니다. 아래 upstream patch 밖의 자체 모듈·제어기는 다음 절에 별도로 기록한다.

| 구성요소 | 기준 | 배포 patch | 파일 수 | 추가 | 삭제 |
|---|---|---|---:|---:|---:|
| Firecracker | **v1.17.0**, `95f868c8e345b1cc8faccd1a3c910b4989dc3f58` | `firecracker.patch` | 35 | 6,254 | 75 |
| Linux KVM | 제공 DCD 소스의 **6.6.0-rc6**, 아래 설명 참고 | `kvm-dax-state.patch` | 26 | 1,914 | 184 |
| Linux CXL | 같은 DCD 소스 | `cxl-static-capacity.patch` | 2 | 10 | 8 |
| 초기 devdax CoW prototype | 같은 커널의 devdax | `devdax-pool-cow.patch` | 1 | 34 | 3 |
| Guest discard 계측 | guest의 기존 virtio-balloon reporting 경로 | `pool-discard-guest-trace.patch` | 2 | 54 | 2 |
| QEMU | **v10.2.2**, `f8ed81651e61d9c2166df6121ce2af0f44f06b3e` | `qemu-cxl-direct-ram.patch` | 2 | 89 | 2 |

모든 patch는 [`implementation/virtual_machine/`](../implementation/virtual_machine/)에 있다. 합계는 68개 파일 항목, **+8,355/-274줄**이다. 새 `.rs`/`.c` 파일도 patch에 포함되며, 전체 vendor checkout이나 빌드 바이너리를 별도로 Git에 올리지 않는다.

**Linux의 기준은 정식 upstream 6.6.0-rc6 그대로가 아니다.** 제공받은 `dcd-v3-2023-10-30.zip`의 `linux-kernel-dcd-v3-2023-10-30`을 기준으로 우리 변경을 계산했다. 제공 archive 자체에 이미 포함된 DCD 변경은 위 숫자에 포함되지 않는다. Archive SHA-256은 `5b29096b3c7e4d9dceeb431d5ff35cd549159524c055301d1e5d4cb6b5819975`다. Archive는 별도로 준비하며, [kernel-source.json](../implementation/virtual_machine/kernel-source.json)과 [kvm-dax-source.json](../implementation/virtual_machine/kvm-dax-source.json)에 기반 파일 identity와 적용 순서가 있다. QEMU/Firecracker 기준은 [ram-backend-source.json](../implementation/virtual_machine/ram-backend-source.json)에 있다.

### Firecracker 파일별 변경

| 기준 소스 경로 | 추가 | 삭제 | 변경 역할 |
|---|---:|---:|---|
| `src/firecracker/src/api_server/mod.rs` | 18 | 0 | 추가 제어 action의 API 처리 |
| `src/firecracker/src/api_server/request/actions.rs` | 21 | 0 | DAX·pool·resident action 파싱 |
| `src/firecracker/src/api_server_adapter.rs` | 330 | 7 | VMM event loop의 native Unix endpoint, binary packet 송수신 |
| `src/firecracker/src/dax_heap.rs` | 395 | 0 | 선택적 공유 DAX VMM heap allocator |
| `src/firecracker/src/main.rs` | 5 | 0 | 공유 heap·실험 모드 초기화 |
| `src/vmm/src/arch/x86_64/vcpu.rs` | 122 | 3 | 상태 복원, TSC·MSR 처리와 DAX vCPU 연결 |
| `src/vmm/src/arch/x86_64/vm.rs` | 40 | 8 | 목적지의 로컬 KVM VM wrapper 복원 |
| `src/vmm/src/builder.rs` | 59 | 1 | DAX/direct 시작·복원 경로 선택 |
| `src/vmm/src/device_manager/acpi.rs` | 30 | 0 | ACPI 장치 상태 재연결 |
| `src/vmm/src/device_manager/mmio.rs` | 35 | 0 | MMIO transport의 local 연결 처리 |
| `src/vmm/src/device_manager/mod.rs` | 5 | 0 | 추가 device-manager 경로 노출 |
| `src/vmm/src/device_manager/persist.rs` | 43 | 5 | 준비한 backend를 이용한 장치 복원 |
| `src/vmm/src/devices/acpi/vmclock.rs` | 4 | 0 | 재사용 clock 상태 반영 |
| `src/vmm/src/devices/legacy/i8042.rs` | 10 | 0 | 재사용 장치 상태 초기화 |
| `src/vmm/src/devices/legacy/serial.rs` | 41 | 2 | UART 상태 복원, 동일 eventfd 수명 유지 |
| `src/vmm/src/devices/virtio/balloon/device.rs` | 201 | 0 | free-page reporting·공유 연결 discard·ACK |
| `src/vmm/src/devices/virtio/balloon/mod.rs` | 2 | 0 | reporting 기능 노출 |
| `src/vmm/src/devices/virtio/balloon/persist.rs` | 26 | 0 | reporting balloon 상태 복원 |
| `src/vmm/src/devices/virtio/block/virtio/device.rs` | 101 | 7 | FD·io_uring 준비/재사용, I/O drain·복구 |
| `src/vmm/src/devices/virtio/block/virtio/io/async_io.rs` | 5 | 0 | 완료 처리 및 준비 상태 확인 |
| `src/vmm/src/devices/virtio/block/virtio/persist.rs` | 20 | 0 | 기존 block backend로 상태 복원 |
| `src/vmm/src/devices/virtio/block/virtio/request.rs` | 42 | 1 | 초기 read-hint 수집; 현재 공유 권한의 근거는 아님 |
| `src/vmm/src/devices/virtio/persist.rs` | 12 | 0 | virtio 상태·subscriber·IRQFD 재연결 |
| `src/vmm/src/direct.rs` | 791 | 0 | direct preflight, export/import, resident binary packet, 실패 시 실행 차단 |
| `src/vmm/src/lib.rs` | 171 | 3 | VMM pause/resume·pool·direct/reuse 경로 통합 |
| `src/vmm/src/persist.rs` | 227 | 10 | RAM dump 없는 state-only DAX 저장/복원·선택적 단계 계측 |
| `src/vmm/src/pool_cache.rs` | 813 | 0 | canonical/Bloom·초기 read-hint pooling 실험 경로 |
| `src/vmm/src/pool_memory.rs` | 1,162 | 0 | guest 등록 기반 promotion/demotion, ABI 2 mapper, CoW/discard 연동 |
| `src/vmm/src/resources.rs` | 26 | 0 | 복원·준비 시 VMM resource 구성 |
| `src/vmm/src/reuse.rs` | 715 | 0 | state-only worker 재사용·미리 준비한 복원 경로 |
| `src/vmm/src/rpc_interface.rs` | 102 | 1 | 추가 action dispatch, legacy pause/export·import/resume 결합 |
| `src/vmm/src/vstate/memory.rs` | 243 | 3 | device-DAX RAM, 고정 HVA·공유 매핑 |
| `src/vmm/src/vstate/vcpu.rs` | 191 | 11 | vCPU FD/thread의 재사용·상태 이전 |
| `src/vmm/src/vstate/vm.rs` | 245 | 12 | DAX private ioctl, export/detach·attach, ioevent 연결 |
| `src/vmm/tests/devices.rs` | 1 | 1 | 변경된 테스트 초기화 인터페이스 반영 |

### Linux KVM 파일별 변경

| 기준 소스 경로 | 추가 | 삭제 | 변경 역할 |
|---|---:|---:|---|
| `arch/x86/include/asm/kvm_host.h` | 5 | 0 | KVM DAX allocator 연결 |
| `arch/x86/kvm/Kconfig` | 14 | 0 | 실험용 CONFIG_KVM_DAX_POOL_TEST |
| `arch/x86/kvm/cpuid.c` | 20 | 10 | 공유 CPUID 상태 할당/해제 |
| `arch/x86/kvm/i8254.c` | 50 | 5 | PIT 상태 인계와 host worker 재연결 |
| `arch/x86/kvm/i8254.h` | 3 | 0 | PIT DAX hook 선언 |
| `arch/x86/kvm/i8259.c` | 4 | 4 | PIC 상태 할당/해제 |
| `arch/x86/kvm/ioapic.c` | 3 | 3 | IOAPIC 상태 할당/해제 |
| `arch/x86/kvm/lapic.c` | 37 | 10 | LAPIC 상태와 로컬 timer 인계 |
| `arch/x86/kvm/lapic.h` | 3 | 0 | LAPIC DAX hook 선언 |
| `arch/x86/kvm/mmu.h` | 5 | 0 | 공유 EPT rebind·invalidate 인터페이스 |
| `arch/x86/kvm/mmu/mmu.c` | 118 | 13 | 최초 EPT admission, 증분 rebind, nested root 보완, TLB 요청 |
| `arch/x86/kvm/mmu/mmu_internal.h` | 4 | 0 | 공유 EPT page 연결 metadata |
| `arch/x86/kvm/mmu/tdp_mmu.c` | 35 | 5 | 공유 EPT page 할당, fault 시 backing 범위 확인 |
| `arch/x86/kvm/vmx/vmx.c` | 144 | 9 | VMCS 인계와 destination CPU의 VMX 상태 재설정 |
| `arch/x86/kvm/vmx/vmx.h` | 1 | 0 | VMX DAX hook 선언 |
| `arch/x86/kvm/x86.c` | 178 | 39 | 아키텍처 상태 detach/attach와 clock·MMU 처리 |
| `include/linux/kvm_dax.h` | 182 | 0 | 커널 내부 allocator·mapping identity·handoff 선언 |
| `include/linux/kvm_host.h` | 2 | 1 | 공유 KVM 구조의 추가 상태 |
| `include/uapi/linux/kvm_dax.h` | 38 | 0 | userspace용 token·private ioctl ABI |
| `kernel/module/main.c` | 11 | 0 | 실험용 동기 init 해제로 노드 간 module 주소 배치 재현 |
| `virt/kvm/Makefile.kvm` | 1 | 0 | DAX pool 빌드 연결 |
| `virt/kvm/coalesced_mmio.c` | 6 | 6 | 공유 상태 할당/해제 |
| `virt/kvm/dax_pool.c` | 328 | 0 | DAX allocator, 공유 arena 상태·계측 |
| `virt/kvm/eventfd.c` | 75 | 17 | IRQFD quiesce와 source 연결 해제 |
| `virt/kvm/irqchip.c` | 5 | 5 | IRQ routing 상태 할당/해제 |
| `virt/kvm/kvm_main.c` | 642 | 57 | private ioctl, 단일 owner, VM/vCPU FD 인계, mapper identity 연동 |

### 나머지 upstream patch

| Patch | 기준 소스 경로 | 추가 | 삭제 | 역할 |
|---|---|---:|---:|---|
| `cxl-static-capacity.patch` | `drivers/cxl/core/hdm.c` | 7 | 7 | 실험용 고정 CXL capacity 노출 |
| 위와 같음 | `drivers/cxl/core/mbox.c` | 3 | 1 | 고정 capacity mailbox 처리 |
| `devdax-pool-cow.patch` | `drivers/dax/device.c` | 34 | 3 | 초기 devdax 공유 mapping의 CoW 실험; 현재 ABI 2 mapper와 별개 |
| `pool-discard-guest-trace.patch` | `drivers/virtio/virtio_balloon.c` | 19 | 2 | reporting 제출·완료 시점 계측 |
| 위와 같음 | `include/trace/events/pool_discard.h` | 35 | 0 | discard tracepoint |
| `qemu-cxl-direct-ram.patch` | `hw/mem/cxl_type3.c` | 85 | 2 | 같은 host backing을 CXL 영역으로 노출하는 lab 구성 |
| 위와 같음 | `include/hw/cxl/cxl_device.h` | 4 | 0 | 추가 device 상태 선언 |

Guest page reporting 자체는 Linux의 기존 기능을 사용한다. 위 guest patch는 제출/완료 증거 수집용이고, 공유 연결을 해제하는 실질 구현은 Firecracker balloon과 `/dev/cxl_pool`에 있다.

## 별도로 추가한 핵심 소스

아래는 upstream 파일 수정량과 구분한 자체 구현이다. 줄 수는 `1c359875c` 파일 전체이며 공백·주석을 포함한다. 테스트·전체 harness를 포함한 총량으로 해석하지 않는다. 링크는 모두 저장소가 직접 관리하는 소스다.

| 자체 파일 | 줄 수 | 역할 |
|---|---:|---|
| [guest-pool.c](../implementation/virtual_machine/guest-pool.c) | 436 | 현재 guest inode/folio 등록, pin/lock, 영구 status |
| [guest-pool-uapi.h](../implementation/virtual_machine/guest-pool-uapi.h) | 153 | guest 등록 ABI |
| [guest-pool-cli.c](../implementation/virtual_machine/guest-pool-cli.c) | 199 | guest helper 제어 |
| [guest-pool-build.py](../implementation/virtual_machine/guest-pool-build.py) | 124 | guest built-in 통합 및 빌드 |
| [guest_pool_protocol.py](../implementation/virtual_machine/guest_pool_protocol.py) | 146 | 등록 목록·image·extent 해석 |
| [pool_memory_module.c](../implementation/virtual_machine/pool_memory_module.c) | 604 | host persistent mapper, write fault CoW, discard, owner epoch |
| [pool_memory_uapi.h](../implementation/virtual_machine/pool_memory_uapi.h) | 50 | host mapper ABI 2 |
| [pool_handoff_control.py](../implementation/virtual_machine/pool_handoff_control.py) | 184 | controller-private frame grant 및 세대 관리 |
| [pool_bloom_tool.c](../implementation/virtual_machine/pool_bloom_tool.c) | 87 | CXL Bloom slot 읽기/갱신 |
| [resident_bootstrap.c](../implementation/virtual_machine/resident_bootstrap.c) | 154 | 목적지 프로세스·API·backend 준비 |
| [resident_control.c](../implementation/virtual_machine/resident_control.c) | 396 | persistent TCP/Unix 제어, owner 확인, state 전달 |
| [resident_wire.h](../implementation/virtual_machine/resident_wire.h) | 24 | 제어 wire 형식 |
| [resident_fast.py](../implementation/virtual_machine/resident_fast.py) | 147 | 요청/응답 framing·계측 |
| [resident_prepare.py](../implementation/virtual_machine/resident_prepare.py) | 92 | 준비 자원 관리 |
| [resident_proxy.py](../implementation/virtual_machine/resident_proxy.py) | 46 | 실행 중인 제어기로 요청 relay |
| [kernel_handoff_trace.py](../implementation/virtual_machine/kernel_handoff_trace.py) | 71 | handoff 구간 한정 kernel 함수 계측 |

Guest helper는 `guest-pool-build.py`가 guest kernel의 `drivers/misc/`에 두 파일을 넣고 Kconfig/Makefile에 연결한다. `CONFIG_GUEST_POOL=y` built-in이며 영구 status page 수명을 보장한다. Host mapper는 `pool_memory_module.c`/`pool_memory_uapi.h`를 별도 모듈로 빌드해 `/dev/cxl_pool`을 노출한다. 이 두 구현은 Linux KVM patch의 줄 수에 포함되지 않는다.

## 실행 경로와 구현상 보장

### 이미지·파일·guest cache

A는 이미지의 고향이다. A에서 실행할 때 host의 local FD, B에서는 같은 immutable 이미지의 NFS FD를 virtio-blk backend로 쓴다. CXL에 있다는 이유만으로 NFS를 사용하는 것은 아니다. Guest는 두 경우 모두 ext4/virtio-blk를 보고, cache hit은 guest RAM에서 끝난다. Miss만 block I/O로 내려간다. 수정 파일은 guest OverlayFS의 tmpfs upper에 두며 guest RAM과 함께 유지한다. 별도 writable ext4 lifecycle 실험은 있지만 최신 resident direct는 read-only backend 범위다.

### 공유 자격·Bloom·CoW

`guest_pool_prepare()`/`guest_pool_scan_file()`는 현재 inode/mapping/index, clean·uptodate·writeback 상태를 확인하고 resident order-0 folio의 참조와 lock을 유지한다. Host는 정지 후 영구 status의 ACTIVE/cookie/epoch/등록 목록 digest와 frozen image extent를 확인한다. Dirty bit 0이나 과거 block-read hint만으로 공유하지 않는다. 등록 목록 digest는 promotion의 권한 검증이며, 정상 CPU handoff의 RAM/이미지 전수 hash와 구분한다.

각 VM의 Bloom slot은 DRAM/CXL tier와 관계없이 CXL에 둔다. Controller가 promotion 때 후보를 찾고 private exact canonical index의 READY 상태와 guest 등록으로 최종 결정한다. 다른 VM의 inode·XArray를 읽지 않는다. Stale Bloom의 false negative는 공유 기회를 놓치고, false positive는 정확한 조회에서 배제한다. 현재 일반 guest cache-hit 경로에는 Bloom 조회가 없다.

Canonical은 미리 적재한 immutable image이고 수명이 고정된다. `cpm_fault()`는 shared frame을 읽기 전용으로 연결하고, CPU/host 쓰기가 발생하면 해당 VM의 credit에서 private CXL frame을 할당해 복사한다. 원래 canonical은 다른 VM을 위해 유지된다. Guest가 free-page report로 기존 내용 폐기를 선언하면 `CPM_DISCARD`로 그 VM의 연결을 private zero backing으로 바꾸고 완료 ACK 이후 재사용한다. Dirty 데이터의 보존이 필요한 CoW와 명시적 discard를 구분한다.

현재 canonical 전체 적재량과 unused CoW credits도 실제 할당 비용에 포함한다. 독립 VM 데이터를 합치는 기능을 순정 Firecracker snapshot clone의 파일-backed 공유와 동일한 기능으로 비교하지 않는다. 자세한 조건은 [순정 비교](firecracker-pooling-comparison.md)에 있다.

### Promotion·demotion과 ABI 2 mapper

`pool_memory.rs`는 promotion 때 공유 대상으로 승인된 GPA는 canonical에 연결하고 나머지 내용을 private CXL에 복사한다. `/dev/cxl_pool`의 mapping table·credit·owner epoch는 **해당 VM 전용 CXL metadata**로 유지한다. 다른 microVM이 공유하거나 동시에 수정하는 filesystem metadata가 아니다.

정상 handoff는 `CPM_SEAL` → 고정 크기 handle 전달 → `CPM_IMPORT`로 owner epoch를 진행한다. 매번 전체 mapping table을 JSON으로 만들거나 모든 PFN을 다시 확인하지 않는다. 최초 mapping admission, 실제 promotion/demotion 복사, 명시적 `CPM_AUDIT`는 전체/범위 작업이 필요한 별도 경우다. Demotion은 CoW 이후의 현재 논리 RAM을 DRAM에 복사하고 CXL mapping을 없앤 뒤 grant를 반환한다. 다시 promotion하면 새 allocation generation을 사용한다.

### State-only와 direct

| 항목 | State-only | Direct / resident direct |
|---|---|---|
| CPU·장치 상태 | Firecracker semantic state 직렬화/복원 | CXL의 KVM graph를 인계하고 최신 장치 상태만 전달 |
| Guest RAM | CXL mapper 유지, RAM snapshot 없음 | CXL mapper 유지, RAM snapshot 없음 |
| KVM VM/vCPU·VMCS/EPT | 목적지 로컬 객체 생성/복원; reuse 계열은 목적지 객체 재사용 | 공유된 지정 KVM 객체·VMCS/EPT 유지, host 연결 rebind |
| 사용자 공간 | 목적지 복원 경로 | 각 노드 resident VMM; 첫 방문 이후 FD/thread 재사용 |
| 제약 | 의미적 상태 호환성 필요 | 동일 kernel/주소·CPU 모델 등 강한 실험 제약, 현재 1 vCPU |

`direct.rs::send_resident_memory()`는 pause 후 I/O drain, 최신 장치 상태 저장, mapper seal, event/IRQFD 해제, KVM detach, source RAM의 PROT_NONE 교체를 수행한다. `(version, token, DevicesState, mapper manifest)`를 작은 bitcode packet으로 만든다. 해당 manifest는 ABI 2 handle 중심이며 전체 GPA 목록이 아니다. 측정 warm packet은 약 566–582B였으며 CPU register snapshot이나 RAM payload가 아니다.

목적지 `receive_resident_memory()`는 packet·generation·identity를 확인하고 mapper를 같은 HVA에 연결한다. 기존 KVM FD로 attach한 뒤 queue index·UART·ACPI와 IRQFD/ioeventfd를 복구하고 resume한다. 과거 방문 때의 장치 상태를 재사용하지 않는다. Import가 소유권 변경 뒤 실패하면 `reuse_restore_failed`로 재개를 거절한다. Export가 detach 뒤 실패하면 중단하며 부분 인계 상태에서 계속 실행하지 않는다.

`api_server_adapter.rs`의 native Unix endpoint는 VMM event loop가 직접 처리한다. `resident_control.c`는 양쪽의 상주 TCP 연결과 local Unix 연결, pidfd/SO_PEERCRED 확인을 재사용한다. Warm binary 경로는 HTTP/API thread 왕복 및 JSON staging 파일을 생략한다. 처음 B에 방문할 때는 VM wrapper·thread 등을 만드는 기존 최초 import 경로를 거치며, 준비 비용과 최초 handoff를 warm 왕복과 구분한다.

UART는 `Arc<EventFdTrigger>`로 epoll에 등록된 **동일한 FD 번호와 수명**을 보존한다. 복구 때 dup된 FD로 교체해 옛 epoll 항목이 남는 busy loop를 고쳤다. Header/payload를 하나의 write로 전달해 추가 wakeup도 줄였다.

### EPT·TLB와 실제 물리 host의 차이

현재 topology는 물리 L0 KVM → A/B L1 guest host → Firecracker L2 sandbox다. `kvm_mmu_dax_ept_rebind()`는 처음 admission 이후 새로 추가된 EPT page metadata만 연결한다. 같은 shared arena의 페이지를 재사용하지 않는 실험 계약에 의존한다.

`kvm_dax_nested_root_sync()`는 retained root offset 1에 동일 byte를 써서 outer KVM의 오래된 shadow 변환 무효화를 유도하는 lab 보완이다. `dax_nested_root_sync`의 기본값은 false이고 nested harness에서만 켠다. 물리 host가 직접 sandbox를 실행하면 이 outer shadow 계층이 없어 해당 호출은 제외할 수 있다. 일반 `KVM_REQ_TLB_FLUSH_CURRENT`는 옵션 밖에서 여전히 요청하며 필요한 INVEPT/TLB 처리, CXL 데이터의 가시성과 단일 owner 보장은 남는다. [KVM MMU 설명](https://docs.kernel.org/virt/kvm/x86/mmu.html#translation)과 [upstream VMX flush 구현](https://github.com/torvalds/linux/blob/master/arch/x86/kvm/vmx/vmx.c)을 참고한다.

Source PROT_NONE 교체는 RAM 내용을 검사하지 않지만 커널의 기존 PTE 정리 비용을 갖는다. Nested root 함수의 경과 시간에는 outer KVM 처리와 스케줄링 영향이 포함될 수 있다. Nested 비용을 단순히 빼서 물리 host 성능으로 발표하지 않는다.

## 검증 결과와 남은 작업

실행 코드의 핵심 revision은 `bd174148f`(UART), `b59c79f2a`(native endpoint), `a52327cb9`(binary state), `0e4f78b58`(frame write 결합)이다. 최신 실제 workload/계측 harness는 `54725b6fc`, 요약은 `1c359875c`를 기준으로 한다.

- 같은 VMM binary의 세 경로 비교 144 handoff 통과. 각 첫 왕복 제외 44개 표본/경로에서 HTTP+files 8.113ms → native+binary 6.992ms 중앙값. 상세 계측을 켠 비교다.
- 상세 계측을 끈 실제 Gin 반복 44 handoff 통과. 명시적 반복의 첫 두 hop을 제외한 38개 중앙값 4.092ms, p95 5.333ms. 요청→Resume 응답이며 guest의 첫 instruction/서비스 응답 시간이 아니다.
- Gin/PHP trace × state-only/direct에서 총 124개 원래 명령 결과, 열린 삭제 파일, promotion/demotion 및 작업 상태 유지 확인. 지속 block read와 실제 `go test` 도중의 handoff도 별도 통과했다.
- 3 VM × 실제 160MiB cache의 CoW/discard/재사용 검증. State-only는 9 promotion·9 demotion·12 handoff, direct 시험은 이동 대상 1개+peer 2개에서 6·6·4회. 정상 handoff mapper full-scan 카운터 증가 0.
- 계측한 두 느린 SWE handoff는 13.030ms/31.057ms. Source mapping 철회 4.989ms와 nested root 26.003ms의 서로 다른 원인을 확인했다. 구간별 합계와 한계는 [상세 breakdown](cxl-resident-direct.md#같은-handoff의-제어별-비용)에 기록한다.

이 값들은 순정 Firecracker 대비 이번 최적화의 속도 향상 수치가 아니다. 순정 비교는 별도 문서의 동일 workload·동일 메모리 조건을 따른다. 초기 0.34ms executor는 VMM/장치를 A에 남긴 경로여서 현재 장치 실행 환경까지 인계하는 API와 측정 범위가 다르다.

남은 작업은 (1) pause/resume와 IPC 잔여 비용의 추가 분리, source PTE 철회 최적화, (2) 물리 다중 host에서 주소·TLB·CXL 가시성 검증, (3) PML과 host I/O를 포함한 writer 추적 및 DSA cache 처리, (4) MPTCP와 virtio-net의 소유권/queue 이전 통합, (5) 다중 vCPU·writable image·장애 복구다. PML/MPTCP는 각각의 기존 독립 실험이 있으며 최신 resident pooling 경로까지 통합된 것은 아니다.

## 재확인과 Git 보관 범위

아래는 repo root에서 patch 통계를 재확인하는 읽기 전용 명령이다. 소스를 이미 수정한 vendor checkout에 patch를 다시 적용하지 않는다.

```bash
git apply --numstat implementation/virtual_machine/firecracker.patch
git apply --numstat implementation/virtual_machine/kvm-dax-state.patch
git show 1c359875c:implementation/virtual_machine/firecracker.patch
python3 -m unittest discover -s implementation/virtual_machine -p 'test_resident_*.py'
```

[구현 README](../implementation/virtual_machine/README.md)의 빌드·실행 절차와 [resident 검증](cxl-resident-direct.md#안전성-검증)을 사용한다. 새 머신은 vendor 소스, DCD archive, toolchain/cache, VM image와 fixture를 별도로 준비해야 한다. 소스 참조만으로 기존 실험 binary가 Git에 포함되는 것은 아니다.

게시 전 재확인에서 resident 제어기 10개·Git 소스 참조 4개 시험을 통과했다. Unix socket bind가 필요한 두 시험은 sandbox 제한을 해제한 동일 명령으로 재실행했다. Firecracker checkout과 배포 patch의 완전 일치·역적용 가능 여부, KVM 26개 파일과 source metadata 일치, 여섯 patch의 통계, 문서의 단계별 시간 합계와 새 링크를 확인했다. 실행 코드는 기존 검증 revision 그대로이며 이번 문서 정리를 위해 VM workload를 다시 실행하지 않았다.

이번 Git 게시에는 patch·자체 소스·테스트·간결한 문서만 포함한다. 원시 report/trace, `.tar`/`.gz`, 이미지·바이너리·vendor 사본은 추가하지 않는다. `/tmp/cxl-revision-*` 경로는 해당 실행 머신의 로컬 근거이며 GitHub에서 내려받을 수 있는 경로가 아니다. 기존 역사적 문서의 archive 링크도 로컬 보관 자료일 수 있다.
