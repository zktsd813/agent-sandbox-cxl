# CXL VM hand-off 구현 지도

2026-11-12 2차 정리. 현재 tree는 VM-private CXL RAM과 VM hand-off를 유지한다.
연구 방향은 [HANDOFF](HANDOFF.md), 운영은 [구현 README](../implementation/virtual_machine/README.md),
기존 측정은 [resident direct](cxl-resident-direct.md)를 따른다.
Page cache 공유·canonical·Bloom·donor·discard 구현과 중간 제어 비교는
[정리 전 184167c](https://github.com/zktsd813/agent-sandbox-cxl/tree/184167c/implementation/virtual_machine)에 있다.
과거 `results/`와 `docs/results/`의 수치는 당시 소스의 결과다.
2차 정리에서 제거한 harness·heap·mapper 비교 경로는
[243b1b0의 구현](https://github.com/zktsd813/agent-sandbox-cxl/tree/243b1b0/implementation/virtual_machine)에 남아 있다.

## 남는 실행 경로

1. `lab.py`가 독립된 L1 A/B와 공유 DAX backing을 구성한다. 기존 snapshot·reuse·reuse-prepared·A Firecracker/B native direct 기준선을 유지한다.
2. `swe_handoff_prepare.py`가 원래 trace assets에 kernel·mapper·상주 제어기를 포장한다. Guest 등록 모듈은 필요하지 않다.
3. `swe_handoff_run.py`가 fresh pair별 실행과 정리를 맡고 `handoff_bench.py`가 명령 재생·RAM tier 변경·hand-off를 수행한다.
4. Promotion은 RAM 전체를 VM 전용 CXL frame으로 복사한다. Demotion은 현재 RAM 전체를 private DRAM으로 복사하고 grant를 반환한다.
5. State-only는 작은 semantic CPU/device state와 mapper handle을 전달한다. Direct는 CXL KVM graph·VMCS·EPT를 유지하고 host 연결을 rebind한다.
6. Resident는 목적지 process·block FD·io_uring·제어 연결을 source 실행 중 준비한다. 첫 B 방문 이후 native Unix endpoint와 binary packet을 사용한다.
7. `verify_swe_handoff.py`는 controller를 import하지 않고 원본 명령·결과·최종 diff·process/FD·mapper lease·kernel trace를 원시 증거로 검증한다.

## 기준 소스와 ABI

| 구성 | 기준 | 적용 순서·상태 |
|---|---|---|
| Firecracker | `95f868c8e345b1cc8faccd1a3c910b4989dc3f58` | 새 원본 복사본에 `firecracker.patch` |
| Linux | 제공된 DCD 6.6.0-rc6 archive | `cxl-static-capacity.patch` → `kvm-dax-state.patch` |
| QEMU | v10.2.2 `f8ed81651e61d9c2166df6121ce2af0f44f06b3e` | `qemu-cxl-direct-ram.patch` |
| Mapper | `/dev/cxl_pool`, ABI 4 | create/query/status/seal/import/retire/audit; 단일 owner epoch |

KVM은 shared arena·일반 할당 경로·8개 ioctl·mode 2/3·profiling을 유지한다.
옛 pointer 로그와 비공유 allocator·mode 0을 제거했다. 변경 대상 25개 파일의 identity는
`kvm-dax-source.json`의 SHA-256에 기록한다. `kernel-source.json`의 zip SHA-256과
tarball 자체 hash는 서로 다른 archive 형식이므로 직접 비교하지 않는다.

Mapper는 SELECT·credits·외부 CONFIG를 제거했다. Private frame 범위·flags·중복 거절 검사는 유지한다.
Handle의 152B 크기·offset은 reserved 공간으로 보존하고 ABI·persistent magic을 4로 올렸다.
Config·query는 56B이며 Firecracker mapper manifest는 version 4,
이를 담는 resident packet은 version 2다. 이전 ABI handle을 재사용하지 않는다.
Tier 변경 action은 `MemoryTier`다. 정상 hand-off의 RAM 전수 검사·mapping JSON은 없다.
VMM DAX heap과 resident JSON 재방문·evidence cache는 제거했다. 첫 preboot JSON import와 binary 재방문은 유지한다.
명시적 audit와 RAM 복사 시점의 전체 작업은 별도로 계측한다.

UART eventfd 수명, virtio queue의 최신 상태, IRQFD/ioeventfd 재연결과 block I/O drain을 유지한다.
실패한 remap 이후 resume/snapshot을 막는 guard는 공유 모듈에서 `pool_memory`로 옮겼다.
Direct는 1 vCPU, 고정 HVA, 동일 kernel/CPU 모델과 read-only MMIO block 조건이다.
`dax_nested_root_sync`는 nested lab의 outer KVM shadow EPT 보완이며 물리 CXL coherence 처리가 아니다.
물리 multi-host CXL, writable direct, 다중 vCPU와 장애 복구는 검증하지 않았다.

## Patch 변경량

아래 수치는 각 upstream 대비 현재 patch의 `git apply --numstat`이다.
과거 공유 구현의 변경량은 [184167c의 구현 지도](https://github.com/zktsd813/agent-sandbox-cxl/blob/184167c/docs/cxl-implementation-map.md)를 따른다.

| Patch | 파일 수 | 추가 | 삭제 |
|---|---:|---:|---:|
| `firecracker.patch` | 28 | 4223 | 95 |
| `kvm-dax-state.patch` | 25 | 1694 | 175 |
| `cxl-static-capacity.patch` | 2 | 10 | 8 |
| `qemu-cxl-direct-ram.patch` | 2 | 89 | 2 |

### firecracker.patch

| Upstream 경로 | 추가 | 삭제 |
|---|---:|---:|
| `src/firecracker/src/api_server/mod.rs` | 18 | 0 |
| `src/firecracker/src/api_server/request/actions.rs` | 19 | 0 |
| `src/firecracker/src/api_server_adapter.rs` | 329 | 7 |
| `src/firecracker/src/main.rs` | 1 | 0 |
| `src/vmm/src/arch/x86_64/vcpu.rs` | 122 | 3 |
| `src/vmm/src/arch/x86_64/vm.rs` | 40 | 8 |
| `src/vmm/src/builder.rs` | 56 | 1 |
| `src/vmm/src/device_manager/acpi.rs` | 30 | 0 |
| `src/vmm/src/device_manager/mmio.rs` | 35 | 0 |
| `src/vmm/src/device_manager/mod.rs` | 5 | 0 |
| `src/vmm/src/device_manager/persist.rs` | 36 | 5 |
| `src/vmm/src/devices/acpi/vmclock.rs` | 4 | 0 |
| `src/vmm/src/devices/legacy/i8042.rs` | 10 | 0 |
| `src/vmm/src/devices/legacy/serial.rs` | 41 | 2 |
| `src/vmm/src/devices/virtio/block/virtio/device.rs` | 100 | 31 |
| `src/vmm/src/devices/virtio/block/virtio/persist.rs` | 20 | 0 |
| `src/vmm/src/devices/virtio/persist.rs` | 12 | 0 |
| `src/vmm/src/direct.rs` | 772 | 0 |
| `src/vmm/src/lib.rs` | 160 | 2 |
| `src/vmm/src/persist.rs` | 196 | 9 |
| `src/vmm/src/pool_memory.rs` | 779 | 0 |
| `src/vmm/src/resources.rs` | 25 | 0 |
| `src/vmm/src/reuse.rs` | 682 | 0 |
| `src/vmm/src/rpc_interface.rs` | 86 | 1 |
| `src/vmm/src/vstate/memory.rs` | 263 | 3 |
| `src/vmm/src/vstate/vcpu.rs` | 181 | 11 |
| `src/vmm/src/vstate/vm.rs` | 200 | 11 |
| `src/vmm/tests/devices.rs` | 1 | 1 |

### kvm-dax-state.patch

| Upstream 경로 | 추가 | 삭제 |
|---|---:|---:|
| `arch/x86/include/asm/kvm_host.h` | 5 | 0 |
| `arch/x86/kvm/Kconfig` | 14 | 0 |
| `arch/x86/kvm/cpuid.c` | 20 | 10 |
| `arch/x86/kvm/i8254.c` | 48 | 3 |
| `arch/x86/kvm/i8254.h` | 3 | 0 |
| `arch/x86/kvm/i8259.c` | 3 | 3 |
| `arch/x86/kvm/ioapic.c` | 3 | 3 |
| `arch/x86/kvm/lapic.c` | 36 | 9 |
| `arch/x86/kvm/lapic.h` | 3 | 0 |
| `arch/x86/kvm/mmu.h` | 5 | 0 |
| `arch/x86/kvm/mmu/mmu.c` | 116 | 13 |
| `arch/x86/kvm/mmu/mmu_internal.h` | 4 | 0 |
| `arch/x86/kvm/mmu/tdp_mmu.c` | 35 | 5 |
| `arch/x86/kvm/vmx/vmx.c` | 87 | 9 |
| `arch/x86/kvm/x86.c` | 177 | 38 |
| `include/linux/kvm_dax.h` | 178 | 0 |
| `include/linux/kvm_host.h` | 2 | 1 |
| `include/uapi/linux/kvm_dax.h` | 38 | 0 |
| `kernel/module/main.c` | 11 | 0 |
| `virt/kvm/Makefile.kvm` | 1 | 0 |
| `virt/kvm/coalesced_mmio.c` | 6 | 6 |
| `virt/kvm/dax_pool.c` | 185 | 0 |
| `virt/kvm/eventfd.c` | 73 | 15 |
| `virt/kvm/irqchip.c` | 5 | 5 |
| `virt/kvm/kvm_main.c` | 636 | 55 |

## 자체 소스 규모

`implementation/virtual_machine/` 바로 아래의 Python·C·header·shell을 센다.
Patch·JSON·문서·vendor·생성물·결과 자료는 제외한다. 빈 줄과 주석을 포함한다.

| 언어/확장자 | 파일 수 | 줄 수 |
|---|---:|---:|
| `.c` | 7 | 4078 |
| `.h` | 2 | 67 |
| `.py` | 24 | 7019 |
| `.sh` | 8 | 803 |

| 자체 소스 | 줄 수 |
|---|---:|
| [analyze_resident_direct.py](../implementation/virtual_machine/analyze_resident_direct.py) | 104 |
| [analyze_resident_preparation.py](../implementation/virtual_machine/analyze_resident_preparation.py) | 102 |
| [build.sh](../implementation/virtual_machine/build.sh) | 211 |
| [control_rpc.c](../implementation/virtual_machine/control_rpc.c) | 155 |
| [control_rpc.py](../implementation/virtual_machine/control_rpc.py) | 74 |
| [experiment_sources.py](../implementation/virtual_machine/experiment_sources.py) | 75 |
| [guest-cxl.sh](../implementation/virtual_machine/guest-cxl.sh) | 63 |
| [guest-init.sh](../implementation/virtual_machine/guest-init.sh) | 129 |
| [handoff_bench.py](../implementation/virtual_machine/handoff_bench.py) | 830 |
| [kernel_handoff_trace.py](../implementation/virtual_machine/kernel_handoff_trace.py) | 71 |
| [l2-init.sh](../implementation/virtual_machine/l2-init.sh) | 56 |
| [lab.py](../implementation/virtual_machine/lab.py) | 2788 |
| [mode_compatibility.py](../implementation/virtual_machine/mode_compatibility.py) | 291 |
| [nfs-l2-init.sh](../implementation/virtual_machine/nfs-l2-init.sh) | 12 |
| [nfs_bench.py](../implementation/virtual_machine/nfs_bench.py) | 232 |
| [nfs_dependencies.sh](../implementation/virtual_machine/nfs_dependencies.sh) | 170 |
| [nfs_probe.c](../implementation/virtual_machine/nfs_probe.c) | 358 |
| [pool_memory_module.c](../implementation/virtual_machine/pool_memory_module.c) | 440 |
| [pool_memory_smoke.c](../implementation/virtual_machine/pool_memory_smoke.c) | 87 |
| [pool_memory_uapi.h](../implementation/virtual_machine/pool_memory_uapi.h) | 43 |
| [resident_bootstrap.c](../implementation/virtual_machine/resident_bootstrap.c) | 144 |
| [resident_control.c](../implementation/virtual_machine/resident_control.c) | 368 |
| [resident_fast.py](../implementation/virtual_machine/resident_fast.py) | 122 |
| [resident_prepare.py](../implementation/virtual_machine/resident_prepare.py) | 91 |
| [resident_proxy.py](../implementation/virtual_machine/resident_proxy.py) | 46 |
| [resident_wire.h](../implementation/virtual_machine/resident_wire.h) | 24 |
| [shared_probe.c](../implementation/virtual_machine/shared_probe.c) | 2526 |
| [swe-handoff-guest-run.sh](../implementation/virtual_machine/swe-handoff-guest-run.sh) | 135 |
| [swe-handoff-l2-init.sh](../implementation/virtual_machine/swe-handoff-l2-init.sh) | 27 |
| [swe_handoff_prepare.py](../implementation/virtual_machine/swe_handoff_prepare.py) | 123 |
| [swe_handoff_run.py](../implementation/virtual_machine/swe_handoff_run.py) | 255 |
| [test_control_rpc.py](../implementation/virtual_machine/test_control_rpc.py) | 199 |
| [test_experiment_sources.py](../implementation/virtual_machine/test_experiment_sources.py) | 70 |
| [test_handoff_control.py](../implementation/virtual_machine/test_handoff_control.py) | 114 |
| [test_handoff_evidence.py](../implementation/virtual_machine/test_handoff_evidence.py) | 162 |
| [test_resident_evidence.py](../implementation/virtual_machine/test_resident_evidence.py) | 117 |
| [test_resident_owner.py](../implementation/virtual_machine/test_resident_owner.py) | 140 |
| [test_resident_proxy.py](../implementation/virtual_machine/test_resident_proxy.py) | 47 |
| [test_swe_inflight_evidence.py](../implementation/virtual_machine/test_swe_inflight_evidence.py) | 64 |
| [trace_prepare.py](../implementation/virtual_machine/trace_prepare.py) | 188 |
| [verify_swe_handoff.py](../implementation/virtual_machine/verify_swe_handoff.py) | 714 |

## 검증 범위

이번 정리의 원본/정리 후 빌드·시험·제약은
[2차 정리 보고서](../.codex-runs/07-prune.report.md)에 기록했다.
모든 빌드·시험·lab은 NUMA 0 CPU 0–31에서 수행한다. Socket backend와 48GiB 이하의 owned fixture를 사용한다.
기존 물리 host 설정을 변경하지 않으며 결과 자료도 삭제하지 않는다.

재확인은 repo root에서 수행한다.

```bash
git apply --numstat implementation/virtual_machine/firecracker.patch
git apply --numstat implementation/virtual_machine/kvm-dax-state.patch
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 -m unittest discover \
  -s implementation/virtual_machine -p 'test_*.py'
```
