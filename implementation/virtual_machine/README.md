# CXL microVM hand-off

공유 CXL에 guest RAM을 유지하고 실행 host를 A↔B로 바꾸는 실험이다.
현재 검증 환경은 한 물리 host의 QEMU L1 A/B와 nested KVM/Firecracker L2다.
물리 multi-host CXL은 검증하지 않았다. 연구 방향은 [HANDOFF](../../docs/HANDOFF.md),
구현 범위는 [구현 지도](../../docs/cxl-implementation-map.md), 기존 측정은
[resident direct](../../docs/cxl-resident-direct.md)에 있다.

Page cache 공유 실험은 별도 `cxl-page-cache` 저장소로 옮겼다.
이 checkout은 VM-private RAM mapper와 hand-off만 유지한다.
정리 전 운영 문서와 과거 pooling 실행은
[184167c의 README](https://github.com/zktsd813/agent-sandbox-cxl/blob/184167c/implementation/virtual_machine/README.md)를 따른다.
`results/`와 `docs/results/`는 당시 소스·설정의 기록이다.

## 모드와 메모리 배치

| 경로 | 이전할 때 하는 일 | 목적지에 유지되는 것 |
|---|---|---|
| `snapshot` / state-only | 작은 CPU·장치 상태 저장, source 종료, 목적지 복원 | CXL guest RAM; KVM 객체와 EPT는 새로 생성 |
| `reuse` | 의미적 상태를 이미 실행 이력이 있는 worker에 복원 | 목적지의 VM/vCPU FD·thread·RAM mapping |
| `reuse-prepared` | 비어 있는 worker의 연결 정리를 먼저 수행한 뒤 복원 | `reuse`와 같음 |
| direct, 새 프로세스 | KVM 객체를 detach한 뒤 새 VMM에서 attach | CXL의 지정 VM/vCPU·VMCS·EPT 객체 |
| resident direct | 양쪽 VMM·I/O backend·제어기를 준비하고 소유권과 최신 장치 상태 전달 | 위 KVM 객체와 host별 FD·thread; 첫 방문 이후 재사용 |

State-only는 raw KVM 구조체를 직렬화하지 않는다. Direct는 CPU register snapshot을 만들지 않는다.
Host thread·FD·eventfd·IRQFD·UART·ACPI·virtio 연결은 각 host에서 다시 연결한다.
Resident의 기본 제어는 native Unix endpoint와 binary packet, 준비된 TCP 연결과 control RPC relay다.
첫 B 방문에는 작은 JSON metadata로 VM wrapper를 구성하고 이후에는 binary packet을 쓴다.

Promotion은 DRAM의 **RAM 전체**를 VM 전용 CXL frame으로 복사한다.
Demotion은 현재 RAM 전체를 private anonymous DRAM으로 복사하고 CXL grant를 반환한다.
Guest 등록 목록이나 파일별 cache 공유는 필요하지 않다. 이 복사 비용을 CPU hand-off 시간과 분리한다.
정상 hand-off는 `seal` → 고정 크기 handle → `import`로 owner epoch를 진행하며 RAM을 순회하지 않는다.
Source 접근 차단, I/O drain, 최신 장치 상태와 lease/epoch 검사는 유지한다.

## 파일 역할

| 파일 | 역할 |
|---|---|
| `build.sh`, `kernel.config`, `*-source.json` | offline 빌드와 기반 소스 identity |
| `cxl-static-capacity.patch`, `qemu-cxl-direct-ram.patch` | nested CXL lab 구성 |
| `kvm-dax-state.patch`, `pointer-audit.json` | KVM DAX 객체·EPT rebind·nested root 보완·계측 |
| `firecracker.patch` | DAX RAM/heap, state-only/reuse/direct/resident, 장치 연결과 restore timing |
| `lab.py`, `guest-init.sh`, `guest-cxl.sh`, `l2-init.sh` | A/B 준비·시작·종료, baseline과 독립 기능 시험 |
| `shared_probe.c`, `cache_probe.h`, `mptcp_probe.h` | CPU/RAM·PML·network baseline과 관측 |
| `pool_memory_module.c`, `pool_memory_uapi.h`, `pool_memory_smoke.c` | `/dev/cxl_pool` VM-private mapper, ABI 3와 독립 smoke test |
| `handoff_bench.py` | SWE 실행·frame 할당·promotion/demotion·state-only/resident hand-off |
| `swe_handoff_prepare.py`, `swe_handoff_run.py`, `swe-handoff-*.sh` | 기존 trace assets의 재포장, fresh pair별 CLI, guest 명령 protocol |
| `verify_swe_handoff.py`, `test_handoff_*.py` | 명령 결과·최종 diff·메모리 lease·원시 증거의 독립 검증 |
| `resident_control.c`, `resident_bootstrap.c`, `resident_wire.h` | 상주 제어기와 목적지 process/I/O 사전 준비 |
| `resident_fast.py`, `resident_proxy.py`, `resident_prepare.py` | 준비된 제어 경로, binary relay, 기존 fixture의 실행 파일 갱신 |
| `control_rpc.c`, `control_rpc.py` | bounded 명령·파일 업로드·Unix relay |
| `kernel_handoff_trace.py`, `analyze_*.py` | opt-in kernel probe와 제어/복원 시간 분석 |
| `nfs_bench.py`, `nfs_probe.c`, `nfs_dependencies.sh`, `nfs-l2-init.sh` | 공통 host NFS 준비·baseline·소유 process 관리 |
| `home_storage_*`, `home-storage-l2-init.sh` | writable ext4와 RAM DRAM↔CXL lifecycle |
| `trace_bench.py`, `trace_prepare.py`, `trace-*.sh` | 실제 개발 trace의 준비·일반 snapshot hand-off |
| `ram_transfer.c`, `mode_compatibility.py` | RAM 전체 migration baseline과 모드 거절 조건 검증 |
| `experiment_sources.py`, `verify_*.py`, `test_*.py` | Git 소스 참조와 독립 회귀 검증 |

## 빌드

명령은 repo root 기준이다. NUMA 0 CPU 0–31만 사용한다.
원본·toolchain·crate cache는 미리 준비한다. 아래 명령은 다운로드하지 않는다.

```bash
V="$PWD/implementation/virtual_machine"
export CXL_LAB_FC_SOURCE="$V/.local/fc-work"
export CXL_LAB_KERNEL_SOURCE="$V/.local/linux-work"
export CXL_LAB_KERNEL_BUILD="$V/.local/kernel-build"
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 bash "$V/build.sh" kernel
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 bash "$V/build.sh" firecracker
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 bash "$V/build.sh" test
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 -m unittest discover -s "$V" -p 'test_*.py'
```

Firecracker 원본 commit은 `95f868c8e345b1cc8faccd1a3c910b4989dc3f58`이다.
새 복사본에 `firecracker.patch`를 적용한다. 빌드는 작업 tree에 patch를 재적용하지 않는다.
로컬 `.local/rust/bin/cargo`(standalone) 또는 `.local/cargo/bin/cargo`(rustup)가 있으면 Rust 1.97.0의 musl target을
`--offline --locked --release`로 빌드한다. 없으면 기존 Firecracker Docker 개발 환경을 사용한다.
로컬 binary는 `$CXL_LAB_FC_SOURCE/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker`다.
Musl target에는 C cross compiler도 필요하다. bud05에서는 `apt-get download musl musl-dev musl-tools`로 받은 패키지를 `.local/musl`에 `dpkg -x`로 추출한다. `build.sh`는 이 디렉터리(또는 `CXL_LAB_MUSL_ROOT`)의 specs를 재배치하고 로컬 wrapper·Linux UAPI 경로를 설정하여 static musl binary를 빌드한다.

Linux는 `kernel-source.json`과 `kvm-dax-source.json`의 원본 identity를 확인한다.
`cxl-static-capacity.patch` → `kvm-dax-state.patch` 순서다.
`CONFIG_KVM=m`, `CONFIG_KVM_INTEL=m`, `CONFIG_KVM_DAX_POOL_TEST=y`를 쓴다.
`build.sh kernel`은 별도 경로에 modules를 stage하며 host 커널을 설치하지 않는다.
Mapper는 해당 kernel build tree를 대상으로 빌드한다.

```bash
mkdir -p "$V/.local/mapper"
cp "$V/pool_memory_module.c" "$V/pool_memory_uapi.h" "$V/.local/mapper/"
printf 'obj-m += pool_memory_module.o\n' > "$V/.local/mapper/Makefile"
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 make -C "$CXL_LAB_KERNEL_BUILD" \
  M="$V/.local/mapper" -j24 modules
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 bash "$V/build.sh" qemu /path/to/qemu-upstream
```

QEMU 기반은 v10.2.2의 `f8ed81651e61d9c2166df6121ce2af0f44f06b3e`다.
기존 Docker 빌드와 소스 준비의 상세는 정리 전 README에 있다.
`CXL_LAB_KERNEL_SOURCE`는 shared probe의 kvm_dax/mptcp UAPI 경로에도 적용된다.
SWE guest kernel은 별도 `O=` 디렉터리에서 `kernel.config` 위에 `guest-swe.config`를 `"$CXL_LAB_KERNEL_SOURCE/scripts/kconfig/merge_config.sh" -m -O "$V/.local/guest-swe-build" "$V/kernel.config" "$V/guest-swe.config"`로 합친 뒤, 같은 `O=`로 `make olddefconfig vmlinux`를 실행한다(CPU·NUMA 제한 동일).
`pool_memory_smoke`는 별도 disposable lab의 mapper 시험이다. 컴파일과 실행을 구분한다.

## Lab 준비와 baseline 실행

다음은 VM images·static BusyBox·QEMU·guest rootfs가 준비된 lab용 명령이다.
이번 bud05 정리에서는 VM을 실행하지 않는다. Host의 module·커널·CXL 설정도 바꾸지 않는다.
각 trial은 새 session·assets를 사용한다. 종료·실패 때 기록된 process identity로만 정리한다.

```bash
export CXL_LAB_SESSION=handoff-test
export CXL_LAB_ASSETS="$V/.local/assets-handoff-test"
FC="$CXL_LAB_FC_SOURCE/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker"
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" prepare \
  --firecracker "$FC" --l1-kernel /path/to/bzImage \
  --module-dir /path/to/modules/RELEASE --l2-kernel /path/to/vmlinux
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" start \
  --vcpus 2 --memory-mib 2048 --cxl-mib 2048 --dax-guest \
  --qemu /path/to/qemu-system-x86_64
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" verify
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" handoff-bench --preset snapshot
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" handoff-bench --preset reuse
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" handoff-bench --preset reuse-prepared
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/lab.py" stop
```

새 프로세스 direct 기준선은 **별도 fresh pair**를 `lab.py start --dax-handoff --dax-guest --dax-4k`
조건으로 시작하고 `lab.py direct-handoff-bench --guest linux --executor firecracker --control http`로 실행한다.
`lab.py migration-bench`는 네트워크를 통한 RAM 전체 migration 기준선이다.
`mode_compatibility.py --require-safe-direct-rejection`은 지원하지 않는 구성을 pause 전에 거절하는지 확인한다.
기존 cache/PML·MPTCP·storage 시험은 각 CLI의 `--help`를 따른다.

## SWE trace와 hand-off

먼저 `nfs_bench.py prepare`와 `trace_prepare.py`로 원래 trace assets를 준비한다.
`trace_prepare.py --source-repo`는 기존 trajectory와 image를 읽기만 한다.
다음 단계는 kernel·mapper·상주 제어기를 새 fixture에 넣는다.

```bash
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/swe_handoff_prepare.py" \
  --trace-assets /path/to/trace-assets --assets "$V/.local/swe-handoff-assets" \
  --firecracker "$FC" --guest-kernel /path/to/vmlinux --guest-config /path/to/guest.config \
  --l1-kernel /path/to/bzImage --mapper-module "$V/.local/mapper/pool_memory_module.ko" \
  --kvm-module-dir /path/to/rebuilt-kvm-modules
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/swe_handoff_run.py" \
  --assets "$V/.local/swe-handoff-assets" --qemu /path/to/qemu-system-x86_64 \
  --qemu-root /path/to/qemu-root --session swe-handoff --cpus 0-7 \
  --names gin-gonic__gin-3741 --modes baseline state-only direct \
  --output "$V/.local/swe-handoff-run"
numactl --cpunodebind=0 --membind=0 --physcpubind=0-31 python3 "$V/verify_swe_handoff.py" \
  --suite "$V/.local/swe-handoff-run/report.json"
```

Runner는 1 vCPU·2GiB sandbox, L1별 4 vCPU·8GiB, 공유 CXL 16GiB layout을 쓴다.
실행 소스는 commit된 Git blob과 일치해야 한다. 미검토 변경을 commit하라는 자동 동작은 없다.
`handoff_bench.py`는 이미 시작한 fresh pair에서 한 trace를 실행하는 하위 CLI다.
명령 문자열·환경·순서와 최종 제출 diff는 원본 trace에서 독립 검증한다.
실패한 trial의 private storage와 증거를 보존한다.

| 주요 flag | 의미 |
|---|---|
| `--modes baseline state-only direct` | 같은 수정 VMM의 DRAM 기준선과 두 hand-off 방식 |
| `--restore-timing` | VMM 내부 상세 계측; 무계측 표본과 분리 |
| `--kernel-trace` | direct hand-off 구간에만 nested root·EPT·TLB probe |
| `--direct-profile-roundtrips N` | 실제 명령 사이에 추가 왕복, 0–20 |
| `--inflight-handoff-index I` | 지정된 실제 명령의 PID/starttime을 확인하면서 B→A→B |
| `--keep-storage` | 성공 trial의 외부 storage도 보존 |
| `--defer-verification` | 실행만 수집; suite를 검증 통과로 표시하지 않음 |

Resident의 ready/fast/native/memory control과 immutable NFS cache는 기본이다.
중간 비교 toggle은 제거했다. 비교 결과는 [resident direct](../../docs/cxl-resident-direct.md)와
[Direct API latency](../../docs/cxl-direct-api-latency.md)에 남아 있다.

## 한계

Direct는 현재 1 vCPU·동일 kernel/CPU 모델·고정 HVA·MMIO·read-only block 조건이다.
Balloon·hotplug·pmem·지원하지 않는 장치 구성은 사전에 거절한다.
Writable ext4 lifecycle은 별도 storage baseline이며 writable direct 지원을 뜻하지 않는다.
Mapper ABI 3은 이전 ABI handle을 받지 않는다. 같은 binary·kernel·mapper로 양쪽을 준비한다.

`dax_nested_root_sync=1`은 nested lab의 outer KVM shadow EPT 보완이다.
물리 host 성능에서 이 비용을 임의로 빼지 않는다. 일반 TLB 무효화·단일 owner·CXL 가시성은 별도 보장이다.
비일관 물리 multi-host CXL, 다중 vCPU direct, writable direct, 장애 복구와
최신 resident 경로의 MPTCP/PML 통합은 검증하지 않았다.
