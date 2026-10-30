# CXL microVM 실행·전환 실험

처음부터의 배경·구현 과정·최근 파일 공유 논의와 PML/MPTCP 후속 계획은
[전체 정리](../../docs/cxl-microvm-overview.md)에 있다. 이 README는 실행·계측의 운영 문서다.

독립된 두 Linux 사이에서 microVM의 RAM은 공유 DAX에 유지하고 실행 주체를 바꾸는 실험이다.
현재 검증 환경은 **한 물리 호스트의 QEMU VM A/B + 각 VM 내부의 nested KVM/Firecracker**다.
실제 다중 물리 노드 CXL 연결의 성능을 측정한 결과는 아니다.

```text
물리 호스트: QEMU A                         QEMU B
             Linux/KVM + Firecracker         Linux/KVM + Firecracker
                         \                   /
                          공유 CXL Type-3 backing
                          → 각 Linux의 /dev/dax0.0
                          → 동일 microVM의 guest RAM
```

QEMU가 같은 파일을 공유 CXL 메모리로 노출한다. A/B의 일반 Linux RAM은 별도이며,
DAX 공유 영역은 기본 2GiB다. 기본 microVM은 1 vCPU·128MiB이고 RAM 전체를 DAX에 매핑한다.
게스트의 page table도 guest RAM에 포함된다.

<a id="session-handoff"></a>

## 다음 세션 인계: 2026-10-28

목표는 **microVM의 상태를 CXL에 유지하면서 실행 노드를 바꾸고, 기존 application 연결과
파일 접근까지 이어가는 것**이다. CPU/RAM 전환과 실제 서비스 I/O 전환을 함께 완성하는 중이다.
이 README를 운영·인계 문서로 유지하며, 측정 근거는 연결된 JSON과 원시 로그를 읽는다.

| 영역 | 확인한 상태 | 남은 작업 |
|---|---|---|
| CPU/RAM | 공유 DAX snapshot(2 vCPU trace 포함), KVM worker reuse/prepared, 동일 객체 direct handoff 및 크기별 측정 | 실제 물리 노드·다중 vCPU 및 장치가 있는 direct/reuse |
| Network | MPTCP를 쓰는 guest **client**를 A→B snapshot 복원해 같은 연결로 전송 지속 | 빠른 제어 경로 연결, inbound/server 이동, direct/reuse의 net 장치 승계 |
| Cache | PML dirty-page 기반 CPU cache 관리 비용 측정; EMR/DSA 조사 | DSA 하드웨어에서 preflush·최종 flush·reader invalidate 구현과 검증 |
| Filesystem | 기존 Host NFS RO lower + guest tmpfs upper trace; 추가로 A 소유 writable ext4와 DRAM↔CXL lifecycle 시험 구현 | 장애 시 단일 writer 보장, 명령 중 이전·장기 왕복, 공통 이미지 DAX 공유 |

2026-10-28 추가 경로의 재현 절차는 [home storage lifecycle](#home-storage-lifecycle)과
[trace RAM lifecycle](#trace-ram-lifecycle)에 있다. Persistent ext4 시험과 기존 tmpfs upper trace를
구분한다. 최종 실행은 writable ext4 **12회 전환·74개 명령**, 실제 trace **24 trials·840개 명령·48회 전환**을 통과했다.
[결과·원시 증거·offline 재검증](results/home-storage-20261028/README.md)에 소스와 build hash, 오류 주입, 보존된 disk를 함께 정리했다.

2026-10-26 설계 업데이트: 파일 접근은 일반 snapshot의 작은 disk부터 검증하고, 공통 read-only
이미지의 DAX 매핑과 VM별 writable layer를 별도 실험한다. MPTCP 제어 경로 단축은 병렬로
진행할 수 있다. 이후 10월 27일 host NFS를 첫 read-only storage 실험으로 구현·검증했다.
[초기 synthetic 기록](results/nfs-20261027/README.md)은 guest cache hit의 NFS READ 0을 확인했다.
후속 리뷰에서 backend mount 누적을 고쳐 초기 성능 수치는 역사적 기록으로 구분했다.
수정 후 실제 개발 trace **24 trials·840명령·48 handoffs**와 synthetic 회귀 20 samples를 통과했다.
이 10월 27일 기록은 RO lower + tmpfs upper의 수정·빌드·FD 상태를 검증한 결과다.
이후 추가한 persistent disk snapshot 시험은 아래에 별도로 설명하며, 장치가 있는 direct/reuse 통합은 남아 있다.
[실행 절차](#host-nfs와-실제-개발-trace-재생)와 [최종 trace 실험 기록](results/trace-nfs-20261027/README.md)에
재현 명령, 독립 검증, 변조 시험과 원시 자료를 보관한다.
PML/cache·CPU/RAM·MPTCP는 각각 검증한 상태이며 통합 서비스 전환의 실측값은 아직 없다.

최근 MPTCP 검증은 [아래 실행 절차](#mptcp-session)와
[결과 JSON](results/mptcp-20261022/summary.json), [원시 로그](results/mptcp-20261022/raw-logs.json.gz)에 있다.
기존 CPU/RAM 측정은 아래의 snapshot/reuse/direct 비교를 그대로 보존했다.
**MPTCP 기능 시험의 5.77초 응답 간격을 기존 µs/ms handoff 결과와 직접 비교하지 않는다.**
Serial 제어 전달이 포함되고, KVM/EPT를 새로 만드는 별도 시험이다.

다음 작업의 순서는 다음과 같다. 아직 구현·측정하지 않은 항목이다.

1. 검증된 guest-client MPTCP 시험을 기준으로, 작은 state의 serial 전달을 기존 DAX mailbox
   경로에 연결하고 guest 재개·MP_JOIN·첫 application 응답을 peer의 시계로 나누어 측정한다.
2. Direct/reuse에 virtio-net의 논리 상태와 TAP·eventfd·IRQFD 연결 승계를 추가한다.
   vCPU pause만으로 host RX가 멈추지 않으므로 source ingress/queue writer를 차단하고 단일 owner를 보장한다.
3. Guest가 server인 이동과 새 client의 진입 경로를 검증한다. 새 주소 알림을 전달할 기존 경로,
   remote path-manager, listener 및 NAT/LB 처리를 정한다.
4. 같은 토폴로지의 TCP/MPTCP 정상 실행 throughput·CPU와 전환 p50/p99를 비교한다.
   CXL host packet queue는 필요성이 확인되면 추가한다. DSA preflush는 별도 후속 과제이며
   Host NFS의 DRAM↔CXL 및 persistent writable image 시험을 바탕으로, 다음에는 명령 실행 중 이전·장기 왕복과
   장애 복구를 다룬다. 파일·PML을 포함한 통합 순서는 [전체 계획](../../docs/cxl-microvm-overview.md#후속-구현-계획)을 따른다.

CPU/RAM 제어는 기존 `lab.py`·`build.sh`·`shared-probe`를 사용한다. 파일 실험은
`nfs_bench.py`의 공통 NFS 설정·소유 프로세스 관리를 재사용하며, 실제 trace 준비·재생·독립 검증을 별도 script로 둔다.
CPU 방식의 주기적 preflush를 다시 추진하지 않으며, 후속 preflush는 DSA 하드웨어를 전제로 한다.
`experiments/`, `CLAUDE.md`, `docs/HANDOFF.md`는 다른 작업 영역이므로 이 microVM 인계에 사용하지 않는다.
Git 배포 대상은 microVM 코드·패치·측정 결과와 관련 문서다. `main` 기준의 별도 작업 트리에서
반영하며, 공유 workspace의 `contention-fleet-sim` 작업과 로컬 VM·이미지·빌드 cache는 보존한다.
배포 검증은 [publication.json](results/publication.json)에 기록한다. 2026-10-21 최초 배포 기록과
2026-10-23 MPTCP 추가분의 검증을 구분하고, 기존 측정 결과의 날짜·소스 hash는 유지한다.

## 두 계열과 네 가지 이전 모드

| 경로 | 전환할 때 하는 일 | 목적지에 유지되는 것 |
|---|---|---|
| `snapshot` | source 정지 → 의미적 상태 직렬화 → DAX mailbox 게시 → 목적지에서 KVM 객체 생성·복원 → 실행 | 공유 guest RAM; KVM VM/vCPU와 EPT는 새로 생성 |
| `reuse` | 같은 상태를 이미 실행 이력이 있는 목적지 worker에 복원 | 목적지의 KVM VM/vCPU FD, VMCS, thread, RAM mapping; 기존 IRQFD 정리는 복원 중 수행 |
| `reuse-prepared` | 비어 있는 worker의 연결 정리를 먼저 끝내고, 상태 도착 후 복원 | `reuse`와 같으며 목적지 정리를 전환 경로 밖에서 수행 |

위 세 모드는 상태 전달 계열이다. 별도로 **direct handoff** 구현도 보존했다. 수정 KVM이 `struct kvm`, vCPU, VMCS,
EPT 등의 지정 객체를 DAX에 할당하고 A에서 detach한 객체를 B에 attach한다.
일부 host 연결은 B의 것으로 다시 연결한다. snapshot이나 KVM VM 재생성이 없는,
주소·CPU 모델·물리 메모리 배치 제약이 강한 Intel 실험 경로다. 아래 기본 사용법은 의미적 snapshot 경로다.

의미적 snapshot은 raw `struct kvm`이나 VMCS 바이트를 직렬화하지 않는다.
Firecracker의 `MicrovmState`에 CPU 레지스터·MSR·interrupt controller·clock·장치 상태를 담는다.
Diskless 1-vCPU 구성의 상태 파일은 약 **13.7KB**, 2-vCPU·block trace에서는 약 **25KB**다. 버전·길이·hash를 확인한 뒤 목적지가 읽는다.
기존 shared-CXL 경로는 RAM을 복사하거나 파일로 dump하지 않는다. 아래의 DRAM↔CXL 전환은
전체 RAM을 복사하는 별도 경로다. FD, eventfd, host thread, `mm` 등 host 연결은 각 Linux 소유다.

동일한 HVA 매핑은 `0x400000000000`으로 고정할 수 있다. guest VA/GPA를 바꾸지 않아
게스트 page table을 고칠 필요가 없지만, **서로 다른 KVM의 EPT를 그대로 공유하는 기능은 아니다.**
`reuse`는 목적지 worker의 기존 mapping/EPT를 유지하고 RAM이나 EPT의 이동을 하지 않는다.
복원에 필요한 translation flush는 수행한다.

2026-10-27 [모드 호환성 검증](results/mode-compatibility-20261027/README.md)에서 기존 네 모드의
지원 구성 회귀와 파일 구성을 구분했다. Host NFS·virtio-blk는 일반 `snapshot`에서 검증하며,
`reuse/prepared`와 direct의 기존 device/vCPU 제한을 제거하지 않았다. 특히 direct block의
전환 거부 뒤 Resume API가 성공해도 guest 요청이 복구되지 않는 실패 경로를 확인했다.
현재 패치는 지원하지 않는 direct block·다중 vCPU 구성을 pause 전에 거절하도록 한다.
`mode_compatibility.py --require-safe-direct-rejection`은 거절 직후, Resume 호출 전에도 source가
진행하는지 검사한다. 이는 안전한 거절 조건이며, direct에서 disk 이전을 지원한다는 뜻은 아니다.

## 비어 있는 worker의 의미

```text
FREE_READY → IN_USE → RETIRING → FREE_READY
```

`FREE_READY`는 KVM 객체가 존재하면서 이전 실행의 IRQFD 등 연결 정리가 완료된 상태다.
모든 바이트를 0으로 만든 상태는 아니다. 반환 시 정리를 끝내면 다음 할당의 임계 경로에
그 정리를 넣을 필요가 없다. `reuse-prepared`는 `PrepareReuse` API 한 번으로 이를 검증한다.
정리 시간은 별도로 기록하며 사라진 비용으로 계산하지 않는다.

현재는 **노드당 worker 한 개**를 번갈아 사용한다. 고정 N개 pool의 일반 scheduler/관리자는 아직 없다.
같은 CPU 모델·vCPU 수·RAM 크기·장치 구성·DAX allocation을 요구하며 불일치는 거부한다.
다른 RAM 크기나 장치 구성을 쓰려면 구성별 pool 또는 추가 재설정 구현이 필요하다.

## 사용법

저장소 루트에서 실행한다. host `/dev/kvm`, nested VMX, static BusyBox, C compiler,
QEMU CXL 빌드 및 로컬 Docker Firecracker 개발 이미지/의존성 cache가 필요하다.
커널은 제공받은 DCD 소스 기반 `6.6.0-rc6-dcd-v3`다. host에 커널을 설치하지 않는다.

새 clone에는 vendor 원본과 빌드 cache가 포함되지 않는다. 외부에서 받은
`dcd-v3-2023-10-30.zip`의 SHA-256을 `kernel-source.json`과 대조한 뒤, archive root
`linux-kernel-dcd-v3-2023-10-30`을 `implementation/linux/`로 풀어 둔다.
아래 source 준비는 **해당 디렉토리가 없는 새 checkout에서만** 수행한다.

```bash
git clone https://github.com/firecracker-microvm/firecracker.git firecracker
git -C firecracker checkout 95f868c8e345b1cc8faccd1a3c910b4989dc3f58
git -C firecracker apply ../implementation/virtual_machine/firecracker.patch
git clone --branch v10.2.2 --recurse-submodules https://gitlab.com/qemu-project/qemu.git \
  implementation/virtual_machine/.local/qemu-upstream
git -C implementation/virtual_machine/.local/qemu-upstream rev-parse HEAD
# QEMU base: f8ed81651e61d9c2166df6121ce2af0f44f06b3e
```

Firecracker 빌드는 Docker 이미지 `public.ecr.aws/firecracker/fcuvm:v93`와 Cargo cache가
준비된 경우 `--network none --offline`으로 실행한다. 새 환경에서는 먼저 네트워크가 가능한
Firecracker `tools/devtool` 개발 환경으로 이미지·의존성을 준비해야 한다. 기존 cache를 쓰는
offline 재빌드와 새 환경의 의존성 다운로드는 구분한다.

```bash
# 처음 만들거나 해당 소스를 바꿨을 때 실행
bash implementation/virtual_machine/build.sh kernel
bash implementation/virtual_machine/build.sh qemu "$PWD/implementation/virtual_machine/.local/qemu-upstream"
bash implementation/virtual_machine/build.sh firecracker
bash implementation/virtual_machine/build.sh test
```

빌드는 현재 소스를 사용하며 기존 Firecracker 작업 트리에 패치를 다시 적용하지 않는다.
커널은 `cxl-static-capacity.patch`와 `kvm-dax-state.patch`, QEMU는
`qemu-cxl-direct-ram.patch`를 build script가 적용 여부를 확인하여 적용한다.
QEMU 원본 경로는 위처럼 명시한다. 인자를 생략한 기본 경로는 기존 실험 호스트 전용이다.

```bash
# session과 assets를 분리하면 기존 실험 VM/이미지를 덮어쓰지 않는다.
export CXL_LAB_SESSION=my-test
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-my-test"
python3 implementation/virtual_machine/lab.py prepare
python3 implementation/virtual_machine/lab.py start \
  --vcpus 2 --memory-mib 2048 --cxl-mib 2048 --dax-guest \
  --qemu "$PWD/implementation/virtual_machine/.local/qemu-cxl/build/qemu-system-x86_64"
python3 implementation/virtual_machine/lab.py verify
python3 implementation/virtual_machine/lab.py dax-test
python3 implementation/virtual_machine/lab.py ram-test

# 인자 없으면 조회만 한다. 변경은 선택한 두 실험 VM 내부에만 적용된다.
python3 implementation/virtual_machine/lab.py configure
python3 implementation/virtual_machine/lab.py configure --nx-huge-pages never --rcu normal

# 동일한 실행 조건의 세 경로 비교
python3 implementation/virtual_machine/lab.py handoff-bench --preset snapshot --warmup 10 --iterations 100
python3 implementation/virtual_machine/lab.py handoff-bench --preset reuse --warmup 10 --iterations 100
python3 implementation/virtual_machine/lab.py handoff-bench --preset reuse-prepared --warmup 10 --iterations 100

python3 implementation/virtual_machine/lab.py status
python3 implementation/virtual_machine/lab.py console A  # 종료: Ctrl-]
python3 implementation/virtual_machine/lab.py stop
```

`--preset`은 warm 실행, controller/vCPU thread CPU 분리(0/1), 고정 RAM HVA,
제한된 CPU 모델, file serial, 50µs polling+최대 500µs spin을 함께 선택한다.
측정을 위한 `--no-seccomp`도 포함한다. `--iterations`, `--spin-us` 등 명시 옵션은 preset보다 우선한다.
다른 CPU/보안 프로필은 `handoff-bench --help`의 개별 옵션으로 구성한다.
`configure --nx-huge-pages never`는 실험용 NX huge-page 완화를 끄며, 선택한 VM의 기존 microVM을
종료하고 KVM 모듈을 다시 읽는다. `--rcu expedited`는 해당 두 VM의 RCU 정책만 바꾼다.
물리 host 설정은 바꾸지 않는다. 기존 비교와 맞추려면 이 설정도 같게 유지해야 한다.

결과는 `.local/runs/<run>/`의 JSON·serial log에 저장된다. `current-<session>.json`은 선택한
run을 가리키며 PID와 시작 시각을 함께 확인하여 관리한다. 같은 session에서 살아 있는 VM이
있으면 `prepare`/`start`는 거부된다. `stop`은 선택 session의 VM만 종료한다.

다른 검증도 같은 `lab.py`로 실행한다.

| 목적 | 시작/검증 명령 |
|---|---|
| DAX RAM 및 양방향 접근 | 위 `start --dax-guest`, `dax-test`, `ram-test` |
| KVM/Firecracker 객체의 DAX 배치·포인터 분류 | 별도 session에서 `start --cxl-mib 2048 --dax-guest --dax-kvm-state --qemu <CXL-QEMU>`, `verify`, `kvm-state-test` |
| 같은 KVM 객체의 직접 detach/attach | 별도 session에서 `start --cxl-mib 2048 --dax-guest --dax-handoff --qemu <CXL-QEMU>`, `verify`, `direct-handoff-bench --help` |
| CPU context switch 비교 | `context-bench --scope host` 또는 `--scope guest --role B` |

## Host NFS와 실제 개발 trace 재생

`trace_prepare.py`는 기록된 Gin·PHP-CS-Fixer·MicroPython 명령과 해당 rootfs image를 새 asset에 복사한다.
`trace_bench.py`는 각 trace를 새 guest에서 시작하고 정해진 명령 경계에서 **A→B→A**로 이전한다.
A의 Firecracker는 local image를, B는 비교 조건에 따라 local image 또는 A가 export한 host NFS image를 연다.
Guest의 ext4 lower image는 read-only이고, 파일 수정·빌드 산출물은 OverlayFS의 **guest tmpfs upper**에 남아 RAM과 함께 유지된다.
이는 shared writable disk의 writeback·durability 검증이 아니다.

아래는 저장소 루트에서 실행한다. `--source-repo`에는 원본 `.traj.json`이 있는
`harness/trace-replay/data/native/`와 준비된 `experiments/contention/.local/images/<trace>/rootfs.ext4`,
`image.json`이 필요하다. Trace hash와 image hash를 기록하고, 원본 image에 쓰지 않는다.
기존 kernel/QEMU/Firecracker 준비 요건은 [사용법](#사용법)을 따른다.

```bash
export CXL_LAB_SESSION=trace-nfs-new
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-trace-nfs-new"
export CXL_NFS_OUT="$PWD/implementation/virtual_machine/.local/nfs-deps-trace-new"
export CXL_NFS_KERNEL_SOURCE=/absolute/path/to/patched-linux-source
export CXL_NFS_CPUS=0-5
export JOBS=6
# 선택: 호환되는 O= build를 복사해 재사용한다.
export CXL_NFS_KERNEL_CACHE=/absolute/path/to/compatible-kernel-build
bash implementation/virtual_machine/nfs_dependencies.sh

# 새 NFS 기반 assets를 먼저 만든다. 두 asset 경로 모두 새 경로를 쓴다.
CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-trace-base-new" \
  python3 implementation/virtual_machine/nfs_bench.py prepare \
  --dependencies "$CXL_NFS_OUT" --firecracker /absolute/path/to/patched-firecracker
python3 implementation/virtual_machine/trace_prepare.py \
  --source-repo /absolute/path/to/repository-with-traces-and-images \
  --base-assets "$PWD/implementation/virtual_machine/.local/assets-trace-base-new" \
  --assets "$CXL_LAB_ASSETS" --dependency-manifest "$CXL_NFS_OUT/manifest.json" \
  --shared-probe "$PWD/implementation/virtual_machine/.local/assets-trace-base-new/shared-probe"
```

`nfs_dependencies.sh`는 NFSD/NFSv3뿐 아니라 `OVERLAY_FS`, `TMPFS`, `TMPFS_XATTR`를 켠 별도 kernel을 만든다.
이 설정은 trace guest의 writable RAM layer에 필요하다. 기존 base asset을 재사용할 때는
`trace_prepare.py`의 `--l1-kernel`, `--l2-kernel`, `--module-dir`로 같은 새 kernel/build의 파일을 함께 지정한다.
Host package 설치나 host NFS service 변경은 없다.

```bash
taskset -c 0-5 python3 implementation/virtual_machine/lab.py start \
  --vcpus 2 --memory-mib 4096 --cxl-mib 4096 --dax-guest --dax-4k \
  --migration-network --network-backend tap --memory-backing-dir /dev/shm \
  --storage-image "$CXL_LAB_ASSETS/storage.raw" \
  --qemu /absolute/path/to/patched-qemu-system-x86_64
taskset -c 0-5 python3 implementation/virtual_machine/trace_bench.py \
  --state-transport dax-mailbox --cycles 3 --eviction-cycles 1 --memory-mib 2048 --vcpus 2 \
  --host-cpus-a 0-2 --host-cpus-b 3-5
# 위 명령이 출력한 TRACE_REPORT의 실제 경로를 넣는다.
python3 implementation/virtual_machine/verify_trace_results.py \
  /absolute/path/to/trace-output/report.json --formal --artifacts \
  --assets-dir "$CXL_LAB_ASSETS" --json-out /absolute/path/to/trace-output/audit.json
python3 implementation/virtual_machine/lab.py stop
```

`--state-transport dax-mailbox`는 native helper로 상태 bytes만 기존 DAX mailbox에 게시한다.
위 준비 명령의 `--shared-probe`로 새 helper를 L1 initramfs에 넣어야 한다. 목적지의 빈 API process를
미리 시작하지만 KVM worker reuse는 아니다. Publish 전에 source를 종료·fence하고, receive에서
session/round/length/hash를 검증한 뒤 restore한다. 두 노드의 state bytes를 사후 대조한다.
`--state-transport serial`은 이전 전달 경로를 선택하며 CLI 기본값으로 유지한다.
두 경로 모두 shell 제어는 serial이고, trace controller 시간은 기존 최적화된 native controller와 구분한다.

NFS/trace 실험의 L1 pair는 **한 번만 사용**한다. 중단·실패·smoke 후에도 `lab.py stop`과 `start`로 새 pair를 만든다.
재실행은 marker로 거부해 mount 중첩과 이전 NFS/Firecracker 상태 재사용을 막는다. Dependency와 asset은 새 pair에서도 재사용할 수 있다.
다른 checkout의 QEMU library/BIOS를 쓸 때는 `CXL_LAB_QEMU_ROOT`도 해당 extracted root로 지정한다.

정식 matrix는 3 trace × 2 backend × (retained 3회 + evicted 1회)로 **24 trials·48 handoffs**다.
이는 실행할 시험 수이며 완료 결과는 [trace 실험 기록](results/trace-nfs-20261027/README.md)에서 확인한다.
Smoke는 새 pair에서 `--names gin-gonic__gin-3741 --cycles 1 --eviction-cycles 0`으로 실행하고 검증기의 `--formal`을 생략한다.
Retained는 guest cache를 강제로 비우지 않는 조건이고, evicted는 각 이전 후 guest의 reclaim 가능한 cache를 `drop_caches=3`으로 비운다.
Mapped page와 tmpfs upper가 모두 없어지는 조건은 아니다. Page-cache 보존의 직접적인 mincore/zero-I/O 증명은 별도 synthetic 시험으로 확인한다.

원본 trace에서 실패한 명령은 **기록된 nonzero exit code가 기대값**이다. 검증기는 명령 byte/hash·순서·종료값,
최종 제출물 fingerprint, 열린 뒤 unlink한 FD의 inode/offset, 수정된 파일 내용, source fencing, block/NFS 원시 counter를 대조한다.
Timeout과 최종 제출 명령의 실패는 거부한다. 시간은 guest 명령 실행 구간 합계이며 기록된 think time과 serial 제어 대기를 제외한다.
별도의 controller handoff 시간과 혼합해 application latency로 해석하지 않는다.

기존 `nfs_bench.py` synthetic 시험도 새 실행부터 sample별 `*-host.json`에 host counter/cache 관측을 남긴다.
`verify_nfs_results.py`는 raw 관측에서 delta를 재계산하며 성공 시 `host_observations=raw_validated`를 기록한다.
이 파일이 없는 과거 archive는 `report_only_legacy`로 명시한다. 과거 guest raw event·checksum 검증과,
host 수치를 report에서만 확인한 범위를 구분하기 위한 표시다.

<a id="home-storage-lifecycle"></a>

## Home storage와 DRAM↔CXL lifecycle

`home_storage_bench.py`는 **A/DRAM → A/CXL → B/CXL → A/CXL → A/DRAM** 순서로
일반 snapshot 경로를 실행한다. Guest는 모든 단계에서 같은 virtio-blk 장치와 writable ext4를 본다.
바뀌는 것은 Firecracker가 `/disk/sandbox.raw`를 여는 host 경로다. A에서는 A의 local ext4,
B에서는 A가 export한 NFSv3를 이용한다. RAM이 CXL에 있다는 이유만으로 NFS를 쓰지는 않는다.
A/CXL 단계에서도 storage는 local이며, 귀환 대상은 이미지를 소유한 A다.

`--home-storage-image`는 template `storage.raw`를 run 디렉터리의 `home-storage.raw`로 복사하고
**A에만 RW disk를 연결**한다. Outer ext4 안의 `sandbox.raw`가 실제 guest ext4 image이며,
B에는 그 disk의 복사본을 연결하지 않는다. Controller는 source Firecracker를 정지·종료하고
PID와 시작 시각으로 fence한 후 다음 writer를 시작한다. Guest의 파일 수정은 tmpfs upper가 아니라
이 ext4에 기록된다. 단일 호스트의 coherent CXL 모사 환경에서 controller가 소유권을 관리하며,
분산 lease나 장애 시 fencing, 비일관 CXL의 cache coherence를 구현한 것은 아니다.

DRAM→CXL promotion과 CXL→DRAM demotion은 **정지 후 전체 RAM 복사**다.
Native `ram-transfer`가 실제 복사량과 전체 byte 비교 결과를 기록한다. CXL→CXL 이동에서는
RAM 복사 없이 CPU/device state만 DAX mailbox로 전달한다. A로 demotion한 뒤에는 실제 RAM VMA가
local file인지 확인하고, 이전 CXL RAM을 덮어쓴 후 guest를 재개해 CXL 의존성이 사라졌는지 검사한다.
이 덮어쓰기 단계는 disposable 실험 전용이며 controller가 모든 이전 owner를 fence한 뒤 수행한다.
현재 helper는 앞 2MiB의 control/mailbox 영역을 제외하고 최대 2GiB의 단일 RAM region을 다룬다.

아래 명령은 저장소 루트에서 실행한다. 먼저 앞 절의 `nfs_dependencies.sh`와 `nfs_bench.py prepare`로
NFS 지원 kernel·userspace가 있는 base assets를 준비한다. `CXL_HOME_BASE`에는 그 새 경로를 지정한다.
Firecracker에는 현재 `firecracker.patch`를 반영하고, 이미 준비된 asset의 binary를 사후 교체하지 않는다.

```bash
export CXL_HOME_BASE="$PWD/implementation/virtual_machine/.local/assets-trace-base-new"
export CXL_HOME_BUILD="$PWD/implementation/virtual_machine/.local/home-tools-new"
export CXL_LAB_SESSION=home-storage-new
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-home-storage-new"
mkdir -p "$CXL_HOME_BUILD"
gcc -std=c11 -O2 -static -Wall -Wextra -Werror \
  implementation/virtual_machine/ram_transfer.c -o "$CXL_HOME_BUILD/ram-transfer"
gcc -O2 -static -pthread -Wall -Wextra -Werror \
  implementation/virtual_machine/shared_probe.c -o "$CXL_HOME_BUILD/shared-probe"
gcc -std=c11 -O2 -static -Wall -Wextra -Werror -DRAM_TRANSFER_UNIT_TEST \
  implementation/virtual_machine/ram_transfer.c -o "$CXL_HOME_BUILD/ram-transfer-unit"
"$CXL_HOME_BUILD/ram-transfer-unit" > "$CXL_HOME_BUILD/ram-transfer-unit.jsonl"
python3 implementation/virtual_machine/home_storage_prepare.py \
  --base-assets "$CXL_HOME_BASE" --assets "$CXL_LAB_ASSETS" \
  --firecracker /absolute/path/to/current-patched-firecracker \
  --shared-probe "$CXL_HOME_BUILD/shared-probe" \
  --ram-transfer "$CXL_HOME_BUILD/ram-transfer" --payload-mib 64 --mutable-mib 16
```

Prepare는 `home_storage_probe.c`를 `gcc -std=c11 -O2 -static -Wall -Wextra -Werror`로 빌드한다.
`--busybox /absolute/path/to/static-busybox`로 BusyBox를 명시할 수도 있다. `cpio`, `mke2fs`,
`blkid`, `readelf`가 필요하며 host에 image를 mount하지 않는다. Manifest에는 source·binary·image와
초기 파일의 SHA-256, ext4 UUID, compiler와 flags를 남긴다. 재준비할 때는 새 asset 경로를 사용한다.

```bash
taskset -c 0-5 python3 implementation/virtual_machine/lab.py start \
  --vcpus 2 --memory-mib 4096 --cxl-mib 4096 --dax-guest --dax-4k \
  --migration-network --network-backend tap --memory-backing-dir /dev/shm \
  --home-storage-image "$CXL_LAB_ASSETS/storage.raw" \
  --qemu /absolute/path/to/patched-qemu-system-x86_64
taskset -c 0-5 python3 implementation/virtual_machine/home_storage_bench.py \
  --cycles 3 --memory-mib 512 --vcpus 2
# HOME_STORAGE_REPORT 출력의 경로를 기록한다. 실패한 경우에도 선택한 pair를 종료한다.
python3 implementation/virtual_machine/lab.py stop
export CXL_HOME_REPORT=/absolute/path/to/home-storage-output/report.json
taskset -c 0-5 python3 implementation/virtual_machine/verify_home_storage.py \
  "$CXL_HOME_REPORT" --full-patterns --negative-tests \
  --out /absolute/path/to/home-storage-output/audit.json
```

`--cycles 1`은 짧은 기능 확인에 쓸 수 있다. 실행 pair는 재사용하지 않고 매번 새로 시작한다.
Controller는 read/write/fsync, rename과 순서가 있는 journal, `MAP_SHARED` 파일, 계속 열린 FD와
unlink 후 남은 FD의 내용·offset을 검사한다. Cold read의 guest block counter와 B의 NFS counter를
함께 확인해 B에서만 NFS READ가 생기는지, remote fsync write가 NFS에 도달하는지 판별한다.
마지막에는 원래 guest를 종료·unmount한 뒤 새 guest를 `home.audit=<generation>`으로 부팅하고,
같은 disk를 `ro,noload,noatime`으로 읽어 저장된 내용을 검사한다. 이는 정상 종료 후 persistence
검증이며 전원 장애나 journal recovery 검증은 아니다. 독립 verifier는 원시 receipt·VMA·state bytes·
source fence와 파일 pattern을 다시 확인하며 `--negative-tests`는 증거의 메모리 사본만 변조한다.

모든 QEMU가 종료된 뒤 실제 변경된 image도 보존해 검사할 수 있다. 아래 run 경로는 report의
`config.home_storage_image.path`가 가리키는 `home-storage.raw`의 부모 디렉터리다.

```bash
export CXL_HOME_RUN=/absolute/path/to/run-containing-home-storage.raw
e2fsck -fn "$CXL_HOME_RUN/home-storage.raw"
debugfs -R "dump /sandbox.raw $CXL_HOME_RUN/sandbox-final.raw" "$CXL_HOME_RUN/home-storage.raw"
e2fsck -fn "$CXL_HOME_RUN/sandbox-final.raw"
sha256sum "$CXL_HOME_RUN/home-storage.raw" "$CXL_HOME_RUN/sandbox-final.raw"
```

`lab.py stop`은 해당 session의 VM과 임시 CXL backing을 정리하고 run의 로그·변경된 disk는 보존한다.
실패 후에도 offline 검사 전에 `lab.py status`로 종료 여부를 확인한다. Heavy hash·pattern 검증은
VM 실행이 끝난 후 수행한다. Dirty-page 연속성을 관측하기 위해 이 fixture는 guest ext4 `commit=600`,
`dirty_writeback_centisecs=0`을 사용한다. Serial 제어와 전체 검증도 포함하므로 **정확성 시험이며
latency benchmark가 아니다**. PML 증분 복사, pre-copy, MPTCP 연결 이동은 이 경로에 아직 통합하지 않았다.

<a id="trace-ram-lifecycle"></a>

## 실제 trace에서 DRAM→CXL→DRAM 실행

기존 `trace_bench.py`의 기본값은 `--ram-lifecycle shared-cxl`이며 이전 실행 방법과 같다.
`--ram-lifecycle home-roundtrip`을 선택하면 기록된 두 명령 경계에서 **A/DRAM → B/CXL → A/DRAM**으로
옮긴다. 첫 경계는 source RAM snapshot을 CXL에 import하고, 두 번째 경계는 CXL을 A의 local tmpfs
RAM file로 export한다. Source 종료·fence, 전체 byte 비교, 실제 VMA 확인과 demotion 후 CXL 덮어쓰기
검증을 포함한다. 각 tier 전환의 `ram_transfer_bytes`에는 guest RAM 전체 크기가 기록된다.

이 trace의 filesystem은 기존 **RO ext4 lower + guest tmpfs upper**다. Local/NFS backend 대조도 유지한다.
따라서 persistent ext4 쓰기의 근거는 앞의 home storage 시험이며, trace에서는 실제 수정·빌드·FD 상태가
RAM tier 변경을 거쳐 이어지는지를 검증한다. `home-roundtrip`은 `--state-transport dax-mailbox`가 필수이며
최대 guest RAM은 2GiB다. L1 RAM은 guest RAM 두 배에 최소 1GiB 여유가 필요해 아래에서는 8GiB를 사용한다.

```bash
export CXL_LAB_SESSION=trace-home-new
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-trace-home-new"
python3 implementation/virtual_machine/trace_prepare.py \
  --source-repo /absolute/path/to/repository-with-traces-and-images \
  --base-assets "$CXL_HOME_BASE" --assets "$CXL_LAB_ASSETS" \
  --dependency-manifest "$CXL_NFS_OUT/manifest.json" \
  --shared-probe "$CXL_HOME_BUILD/shared-probe" --ram-transfer "$CXL_HOME_BUILD/ram-transfer"
taskset -c 0-5 python3 implementation/virtual_machine/lab.py start \
  --vcpus 2 --memory-mib 8192 --cxl-mib 4096 --dax-guest --dax-4k \
  --migration-network --network-backend tap --memory-backing-dir /dev/shm \
  --storage-image "$CXL_LAB_ASSETS/storage.raw" \
  --qemu /absolute/path/to/patched-qemu-system-x86_64
taskset -c 0-5 python3 implementation/virtual_machine/trace_bench.py \
  --state-transport dax-mailbox --ram-lifecycle home-roundtrip \
  --cycles 3 --eviction-cycles 1 --memory-mib 2048 --vcpus 2 \
  --host-cpus-a 0-2 --host-cpus-b 3-5
# TRACE_REPORT 출력의 경로를 기록하고, 실패한 경우에도 이 session을 종료한다.
python3 implementation/virtual_machine/lab.py stop
python3 implementation/virtual_machine/verify_trace_results.py \
  /absolute/path/to/trace-output/report.json --formal --artifacts \
  --assets-dir "$CXL_LAB_ASSETS" --json-out /absolute/path/to/trace-output/audit.json
```

Trace prepare는 base asset에 들어 있는 Firecracker를 사용하므로 해당 base도 현재 패치로 준비해야 한다.
`--ram-transfer`는 native helper를 L1의 `/lab/ram-transfer`에 넣고 binary/source hash를 manifest에 기록한다.
Smoke는 앞 절처럼 Gin만 한 번 실행하고 verifier의 `--formal`을 생략한다. Verifier는 기존 serial·
shared-CXL archive도 지원하며, 새 lifecycle에서는 RAM 복사량·source PID·VMA·poison receipt까지 검사한다.
이 경로 역시 한 물리 host의 coherent 실험이다. Tier 변경의 stop-and-copy 비용과 serial controller
시간을 기존 direct handoff의 µs/ms 수치와 합쳐 해석하지 않는다.

## 기존 측정값 읽는 법

아래는 정리 이전, 동일 빌드·20ms dwell·100회 대조 실험의 p50이다.
`기존 reuse`와 `준비 완료 reuse`를 비교하며 plain `snapshot`의 결과로 읽으면 안 된다.

| RCU 정책 | 경로 | 저장+복원 | 별도 worker 준비 | 관측 handoff 전체 |
|---|---|---:|---:|---:|
| 기본 | 기존 reuse | 9.146ms | — | 9.862ms |
| 기본 | 준비 완료 reuse | 1.461ms | 7.829ms | 2.264ms |
| expedited | 기존 reuse | 1.537ms | — | 2.305ms |
| expedited | 준비 완료 reuse | 1.441ms | 0.140ms | 2.173ms |

9ms는 기본 RCU 대조군이다. 기존 2.x ms 결과는 expedited RCU의 **handoff 전체**다.
1.44ms는 **저장+복원만**이다. 서로 다른 지표/설정을 한 개선 수치로 합치지 않는다.
준비 완료 경로는 peer 실행 중 반환 정리를 수행한다. 여유 없이 요청 시 준비하면 그 대기가 다시 드러난다.
단계별 p50의 합은 전체 p50과 일치하지 않을 수 있다. 지속적인 저장+복원 1ms 미만은 달성하지 못했다.

handoff 전체에는 API 처리·전달·thread wakeup·guest 실행 및 ACK 관측 대기도 포함된다.
`--profile-restore`는 API 내부 단계, `--profile-ack`는 별도 tracefs 수집을 위한 marker를 켠다.
계측은 실행 비용을 바꾸므로 profile 결과와 비계측 성능을 구분한다.

| 근거 | 내용 |
|---|---|
| [semantic-snapshot-comparison.json](semantic-snapshot-comparison.json) | RAM 복사 없는 의미적 snapshot 및 fresh restore |
| [used-kvm-reuse-comparison.json](used-kvm-reuse-comparison.json) | 사용 이력 있는 KVM worker 재사용 |
| [coarse-prepare-comparison.json](coarse-prepare-comparison.json) | 준비 단계 분리·RCU 대조군·negative test |
| [direct-handoff-optimized-summary.json](direct-handoff-optimized-summary.json), [latency-refinement-summary.json](latency-refinement-summary.json) | direct 초기 최적화·후속 단계별 비용·context switch 대조 |
| [heartbeat-attribution-summary.json](heartbeat-attribution-summary.json) | guest 재개와 heartbeat 관측 지연의 분리 |
| [context-switch-comparison.json](context-switch-comparison.json) | 로컬 측정과 문헌 snapshot 값; 문헌값은 직접 측정 결과와 구분 |
| [ack-ept-breakdown.json](ack-ept-breakdown.json) | ACK 관측 대기와 EPT fault 분석 |
| [snapshot-state-inventory.json](snapshot-state-inventory.json) | 직렬화 항목·파일 크기 |
| [host-reference-audit.json](host-reference-audit.json), [host-cxl-residency.json](host-cxl-residency.json) | host 포인터의 역할 및 direct 경로의 배치 |

## 공유 DAX 크기별 비교

[두 경로 그래프](results/dax-scale-20261020/dax-handoff-comparison.png) ·
[Full migration 포함, 1초 상한](results/dax-scale-20261020/migration-three-way-1s.png)

`--dense-working-set`은 기존 두 경로에 같은 workload를 적용한다. W는 guest의
`MemAvailable - 16MiB`이고 4KiB page마다 64B를 초기화·검증한다. 첫 ACK 뒤 첫 scan과
warm scan을 따로 기록하며, 다음 전환은 두 scan과 이전 source의 결과 소비가 끝난 뒤 시작한다.
기존 인자 없는 128MiB·16MiB heap 실험은 유지한다. Kernel·Firecracker 변경은 없다.

두 방식 모두 4GiB shared DAX, 실제 DAX alignment 4096, L2 1 vCPU, THP off를 사용했다.
크기마다 별도 L1 pair를 시작하고 warmup 4회 뒤 20회, 방향별 10회를 측정했다.
아래는 **정지→첫 W scan 완료 관측 평균 ms (첫 ACK까지의 평균 ms)**다.

| M (MiB) | Reuse-prepared | Direct handoff |
|---:|---:|---:|
| 512 | 10.24 (3.12) | 6.80 (0.71) |
| 768 | 13.78 (2.56) | 9.29 (0.31) |
| 1024 | 18.31 (2.92) | 13.91 (0.73) |
| 1536 | 27.82 (3.01) | 19.56 (0.34) |
| 2048 | 35.67 (2.81) | 26.13 (0.34) |

Reuse-prepared는 상태 13,831B를 직렬화·DAX 게시·복원하며 노드별 KVM 객체를 재사용한다.
Worker 준비 평균 0.26~0.41ms는 위 정지 구간 밖에 기록한다. Direct는 Firecracker 최초 부팅 뒤
양쪽 native executor가 동일 KVM/vCPU/VMCS/EPT를 이어받는다. 반복 구간에는 Firecracker API나
snapshot을 쓰지 않는다. 두 방식 모두 guest RAM 전송량은 0이며 direct의 제어 token·소유권 갱신은 존재한다.

이 표는 준비된 pool의 반복 전환이다. 최초 node 방문은 별도 `cold_first_visits`에 보존한다.
예를 들어 1GiB direct의 첫 A→B는 첫 ACK 4.70ms, W 완료 679.07ms였다.
Semantic B는 disposable guest로 같은 DAX backing을 미리 사용하지만 direct B는 첫 방문이므로,
초기 scan의 메모리·translation 준비 상태까지 같은 대조군은 아니다. Scan을 순수 EPT 비용으로 해석하지 않는다.

```bash
# 두 경로 모두 start에 --vcpus 2 --memory-mib 12288 --cxl-mib 4096
# --dax-guest --dax-4k --qemu <CXL-QEMU> 사용. Session/assets는 기존 결과와 분리한다.
python3 implementation/virtual_machine/lab.py handoff-bench --preset reuse-prepared \
  --memory-mib 1024 --dense-working-set --warmup 4 --iterations 20 --profile-restore --timer-slack-ns 1

# 별도 새 pair의 start에 --dax-handoff 추가. 크기별 새 pair를 사용한다.
python3 implementation/virtual_machine/lab.py direct-handoff-bench --guest linux \
  --memory-mib 1024 --dense-working-set --warmup 4 --iterations 20 --executor native \
  --kernel-mode 3 --control local --reuse-fds --profile --guest-cpu 0 --executor-cpu 1 \
  --poll-us 50 --spin-us 500 --timer-slack-ns 1 --irqfd-batch --resume-nokick --channel-spin-us 50
```

기능 실행 예시와 측정용 host CPU·KVM 모듈 설정은 구분한다. 실제 설정·명령과 원본은
`.local/dax-scale-latest.json`이 가리키는 evidence에 보존했다. Direct의 큰 RAM 실험은
KVM pool 2304MiB, VMM heap 2560MiB, telemetry 2816/2880MiB로 이동하여 RAM과 겹치지 않는다.
[dax-size-comparison.json](dax-size-comparison.json)에 단계별 평균·p50·p95, 방향별 결과,
첫 방문, 객체 identity·무결성 검증과 이전 TCP full snapshot 참고군을 저장했다. 본 측정 200회 모두 통과했다.

## RAM 전체 snapshot migration

`migration-bench`는 일반 RAM을 쓰는 L2 microVM을 L1 A/B 사이에서 번갈아 옮긴다.
Source 정지 → CPU·장치 상태+M 전체 RAM snapshot → TCP 전송 → 별도 tmpfs 파일 수신 →
목적지의 새 KVM VM/vCPU 복원 → 첫 응답 → working set 전체 재접근·검증 순서다.
QEMU의 바깥 L1 VM은 유지한다. 압축·zero/sparse 생략 없이 M 바이트 모두 전송한다.
목적지는 비어 있는 Firecracker API process만 미리 준비하며 KVM 객체는 restore 안에서 새로 만든다.

```bash
export CXL_LAB_SESSION=full-migration
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-full-migration-20261020"
python3 implementation/virtual_machine/lab.py prepare
python3 implementation/virtual_machine/lab.py start --vcpus 2 --memory-mib 12288 --migration-network --network-backend tap
python3 implementation/virtual_machine/lab.py verify
python3 implementation/virtual_machine/lab.py configure --nx-huge-pages never --rcu expedited
python3 implementation/virtual_machine/lab.py migration-bench --memory-mib 1024 --warmup 3 --iterations 20 --profile-restore
python3 implementation/virtual_machine/lab.py stop
```

위 migration 명령은 기능 실행 예시이며 측정용 CPU·IRQ 배치를 자동 적용하지 않는다.
아래 TAP 재측정은 NUMA node 0, A의 L1 vCPU0/1→host CPU0/1, B→2/3,
QEMU main·vhost→A CPU4/B CPU5, 양쪽 NIC IRQ→L1 CPU1로 고정했다.
적용 기록은 해당 run의 `cpu-placement.json`에 보존했다.

M은 128..2048MiB다. Guest는 `MemAvailable - 16MiB`를 확보하고 매 4KiB page에 nonzero
cache line을 써서 실제 상주시킨다. W는 이 allocation 크기이며 kernel 등의 고정 사용량은 별도다.
Migration guest만 `transparent_hugepage=never`로 부팅하여 THP용 free-memory reserve를 줄인다.
커널·page metadata·16MiB 여유 공간 때문에 W=M은 아니며 실제 W/M을 결과에 남긴다.
작은 128MiB에서는 W/M이 낮아 본 실험은 512/768/1024/1536/2048MiB로 한다.
재개 후 모든 W page를 읽어 검증하고 같은 읽기를 한 번 더 수행한다. 전체 W를 쓰는 CoW 비용은 제외된다.

결과는 `migration-<M>m-*/report.json`에 저장된다. 다음 측정은 의미를 구분한다.

- Source local clock: pause, snapshot, 송신, 원격 수신 완료 관측, 첫 ACK·전체 W 완료 관측.
- Destination local clock: 수신, restore API, resume, 첫 ACK, 첫/두 번째 W scan.
- `--profile-restore`: CPU state 수집·직렬화, RAM dump, decode, mmap, KVM VM/vCPU 생성,
  memslot, CPU/IRQ/clock/device 복원, thread 시작을 세분화한다. 중첩 scope끼리는 더하지 않는다.
- 기존 VM 종료·빈 API process 준비는 재개 후에 수행하며 별도 비용으로 기록한다.

`--trace-ept --warmup 0 --iterations 1 --profile-restore`는 별도의 EPT 진단 실행이다.
MMU root/fault 경로 시간, 새 page-table page 수, SPTE leaf 크기·설치 수와 trace 누락을 확인한다.
Root 준비와 W 페이지별 demand population을 구분한다. 크기에 비례하는 부분은 주로 후자다.
완료한 trace만 압축하여 ivshmem으로 반출하고 SHA-256을 검증한다. Migration RAM 전송은 TCP다.
Trace는 비용을 바꾸므로 일반 반복 측정과 분리한다. 첫 scan 전체를 순수 EPT 비용으로 부르지 않는다.
파일 mapping fault·cold cache·L0 nested 처리도 포함되며, L1 inclusive 시간과 L0 시간을 더하지 않는다.
Fault helper 밖의 VM exit/entry·guest 실행·scheduling도 있으므로 helper 시간과 첫 scan 시간은 다르다.

Guest instance·RAM pattern·clock/TSC 연속성과 full M-byte payload를 매번 검증한다.
Profile 실행은 CR3와 저장 MSR 개수·순서 hash도 비교한다. `FC_EXACT_TSC=1`로 공개 KVM
TSC offset 복원과 수신 MSR 목록 유지를 사용하고, `FC_VCPU_CPU=1`로 vCPU를 별도 L1 CPU에 둔다.
권장 network는 virtio-net → TAP/vhost-net → 전용 host bridge다. MTU는 1500이며
외부 uplink·host IP·route·방화벽을 바꾸지 않는다. `stop`이 자신이 만든 TAP/bridge를 정리한다.
기존 QEMU socket 대조군은 `--network-backend socket`으로 선택하며 CLI 기본값도 socket이다.
`--socket-listener B`로 listen/connect 역할을 교환할 수 있다. 물리 NIC 성능 측정은 아니다.

아래 표는 **초기 QEMU socket network 대조군**이다. 크기마다 warmup 3회 뒤 20회,
양방향 10회씩이며 시간은 중앙값이다. 권장 TAP network의 재측정은 아래 별도 결과를 참조한다.
`전체 W 완료`는 source pause 요청부터 목적지의 첫 W scan 완료 관측까지다.

| M (MiB) | W (MiB) | W/M | 정지→첫 응답 (s) | 전체 W 완료 (s) |
|---:|---:|---:|---:|---:|
| 512 | 430.2 | 84.0% | 1.526 | 3.456 |
| 768 | 681.3 | 88.7% | 2.183 | 5.160 |
| 1024 | 928.1 | 90.6% | 2.819 | 6.827 |
| 1536 | 1423.5 | 92.7% | 4.129 | 10.030 |
| 2048 | 1923.0 | 93.9% | 5.270 | 13.361 |

[full-migration-comparison.json](full-migration-comparison.json)에 크기별 p50/p95/평균,
합산 가능한 source 구간, destination restore 세부 단계, 방향별 결과, 별도 EPT trace와 원본 경로를 저장한다.
본 측정 100회 모두 RAM pattern·guest instance·clock/TSC·CR3·MSR 목록 검사를 통과했다.
Restore API 중앙값은 1.85~2.03ms다. 재개 뒤 demand EPT population은 이 시간 밖에 발생한다.
이 값은 동일 물리 host의 nested KVM·가상 network 결과이며 물리 두 노드의 절대 지연을 뜻하지 않는다.

### Network 방향 차이 재검증

[TAP 기반 A→B migration breakdown](results/network-fairness-20261020/migration-breakdown-tap.png)

QEMU socket의 바깥 TCP를 직접 조사하니 listener A는 `TCP_NODELAY=0`, connector B는
`1`이었다. L1 application의 TCP는 양쪽 모두 `1`이다. 바깥 설정만 같게 만들면 방향 차이가
사라지고, listener를 B로 바꾸면 느린 방향이 뒤집혔다. 물리 CPU 배치를 교환해도 유지됐다.
QEMU userspace의 Ethernet-over-TCP 경로에서 서로 다른 batching이 발생한 것이 주원인이다.

아래는 파일 I/O를 제외한 단일 TCP 연결, 1GiB payload, MTU 1500, warmup 2회 뒤
양방향 각 10회 결과다. Goodput은 `payload bit / 방향별 평균 수신확인 시간`이다.

| Network / 바깥 NODELAY A·B | A→B (Gb/s) | B→A (Gb/s) |
|---|---:|---:|
| 기존 socket / 0·1 | 7.56 | 2.58 |
| socket / 1·1 | 2.87 | 2.84 |
| socket / 0·0 | 7.54 | 7.56 |
| listener B / 1·0 | 2.55 | 7.81 |
| TAP/vhost, migration용 IRQ CPU1 | 37.08 | 37.13 |
| TAP/vhost, 같은 IRQ 설정 + host CPU 배치 교환 | 37.03 | 37.63 |

TAP은 vhost와 checksum/TSO offload를 사용하며 바깥 TCP tunnel을 제거한다. 따라서 개선 전부를
vhost 하나의 효과로 해석하지 않는다. 기본 IRQ 배치의 TAP 대조군은 49.80/49.61Gb/s였으나,
migration 재측정은 양쪽 IRQ를 L1 CPU1로 통일한 37Gb/s 구성을 사용했다.
동일 host의 두 network namespace/veth 대조군은 70.86/71.28Gb/s였다. 두 물리 host 결과는 아니다.

[network-fairness-comparison.json](network-fairness-comparison.json)에 대조군·원시 자료 hash와
TAP migration 크기별·방향별 결과를 저장한다. 이전 결과와 코드는 보존했다.
TAP의 1GiB migration에서 수신 확인을 포함한 전송 평균은 A→B 423.3ms, B→A 427.8ms다.
파일 I/O까지 포함하므로 위의 순수 TCP goodput과 구분한다. 같은 CPU 배치의 socket 대조군은
각각 1261.5ms, 3575.0ms였다. 전체 평균은 6.913초에서 4.896초가 되었고, 첫 page scan 평균은
4.146초/4.123초로 비슷했다. 두 migration 구성은 network 외에 NIC IRQ 배치도 다르므로
전체 차이를 network backend만의 독립 효과로 해석하지 않는다.

Live pre-copy는 후속 실험이다. Upstream snapshot API는 paused 상태용이므로 실행 중
RAM-only copy와 dirty harvest 경로가 필요하다. 기존 구현 후보는
[Loophole Labs fork](https://github.com/loopholelabs/firecracker)이며 현재 커널·버전 호환성을 먼저 검토한다.
[Drafter](https://github.com/loopholelabs/drafter)는 이를 이용한 pre/post-copy 사례이고 archived 상태다.

## PML과 cache 관리 비용

Cache 관리 비용은 기존 `shared-probe`의 `cache-bench`로 별도 측정한다. 각 Linux에
KVM VM/vCPU 하나를 유지하고 작은 L2 guest가 공유 DAX의 data slot에 쓴다. Firecracker의
state/direct 전환과는 독립된 계측이며, guest RAM의 cache 관리 비용을 분리하는 목적이다.

```bash
export CXL_LAB_SESSION=cache-pml
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-cache-pml"
python3 implementation/virtual_machine/lab.py prepare
python3 implementation/virtual_machine/lab.py start --vcpus 2 --memory-mib 12288 \
  --cxl-mib 4096 --dax-guest --dax-4k --migration-network --network-backend tap --memory-backing-dir /dev/shm \
  --qemu "$PWD/implementation/virtual_machine/.local/qemu-cxl/build/qemu-system-x86_64"
python3 implementation/virtual_machine/lab.py verify
python3 implementation/virtual_machine/lab.py configure --pml on
python3 implementation/virtual_machine/lab.py cache-bench --memory-mib 1024 \
  --dirty-permyriad 100 --mode dirty --iterations 20 --warmup 4
# 같은 workload로 전체 RAM 처리 및 cache 명령을 생략한 대조군.
python3 implementation/virtual_machine/lab.py cache-bench --mode full --dirty-permyriad 100
python3 implementation/virtual_machine/lab.py cache-bench --mode none --dirty-permyriad 100
python3 implementation/virtual_machine/lab.py stop
```

`100`은 페이지의 1%이며 `--write-bytes 64|4096`으로 페이지당 실제 쓰기량을 정한다.
Writer는 KVM dirty bitmap 수집·재설정 후 `CLWB/SFENCE`, reader는 TCP로 받은 불변
epoch bitmap을 확인하고 `CLFLUSHOPT/MFENCE`를 수행한다. ACK 뒤 모든 페이지의 설정한
쓰기 구간을 검증하고, 64B 모드에서는 미변경 마지막 word도 확인한다. 검증을 마쳐야
다음 writer를 허용한다. Bitmap은 실제 KVM 로그와 workload의 예상 집합이
정확히 일치해야 한다. `--dirty-log off`는 `full/none`에서만 사용할 수 있다.
이 로그는 L2 guest의 data slot 쓰기만 추적한다. VMM·kernel·DMA가 수정하는 데이터까지
실제 handoff에 포함하려면 별도 dirty 추적과 모든 writer의 정지가 필요하다.

`--scope host`는 native KVM/PML 대조군으로, 공유 memfd RAM과 loopback TCP를 사용한다.
물리 host의 KVM 설정은 변경하지 않는다. Guest 경로의 PML은 nested 에뮬레이션이다.
위의 tmpfs backing은 물리 host의 파일 writeback이 EPT를 변경하는 영향을 줄인다.
두 경로 모두 coherent 물리 host이므로 실제 비일관 CXL의 stale-cache 동작 검증은 아니다.
Flush byte 수는 명령이 처리한 주소 범위이며 실제 writeback traffic과 구분한다.
Pause→ACK는 source의 HLT 완료부터 reader의 cache 처리 완료 ACK까지이며, guest 실행·
정지 요청·Firecracker 상태 복원 비용은 포함하지 않는다. 수집·재설정·cache 명령·검증은
각각 기록한다. 결과 요약은 `cache-maintenance-comparison.json`에 둔다.

페이지 dirty 추적과 cache-line 상태는 다르다. PML은 guest write로 EPT dirty bit가
설정된 페이지를 기록한다. CPU cache에서 아직 writeback되지 않은 line의 목록을 주지는 않는다.
현재 구현은 **4KiB dirty 페이지마다 64B cache line 64개를 모두 처리**한다. 페이지에
64B만 쓴 경우도 같다. `CLWB`는 dirty line만 실제 writeback하므로 명령이 적용된 바이트 수를
실제 CXL 전송량으로 계산하면 안 된다. Reader는 자신의 PML이 아니라 writer의 bitmap으로
같은 DAX offset에 대응하는 로컬 주소를 invalidate한다. 양쪽 HVA가 같을 필요는 없다.

```text
Writer: guest HLT → GET_DIRTY_LOG → CLEAR_DIRTY_LOG → CLWB × 대상 line → SFENCE
                                      ↓ epoch + bitmap (TCP)
Reader: bitmap 검증 → CLFLUSHOPT × 대상 line → MFENCE → ACK → 설정한 데이터 구간 검증
```

`CLFLUSHOPT`는 dirty line이면 writeback도 수행한다. 따라서 reader에 이전 쓰기의 미반영
dirty line이 없고, 모든 writer가 소유권 순서를 지켜야 한다. 현재 검증은 초기 writeback과
순차적인 writer 교대로 이를 유지한다. Cache flush는 EPT/TLB invalidate와 별개다.

2026-10-21 측정은 native KVM/memfd 및 nested KVM/공유 DAX 각각 30개 조건,
총 **60개 조건·1,200회 본 측정**이며 모두 bitmap·데이터 검증을 통과했다. 각 조건은
warmup 4회 뒤 20회, 방향별 10회다. 물리 CPU는 Ice Lake Xeon Gold 5317이며 EMR 측정이 아니다.
아래는 **nested 공유 DAX의 writer flush + reader invalidate 합계 p50**이다.
각 페이지의 쓰기량은 64B이고, 백분율은 변경 페이지 비율이다.

| 변경 페이지 | 1GiB | 2GiB |
|---:|---:|---:|
| 0.1% | 0.131ms | 0.214ms |
| 1% | 0.911ms | 1.784ms |
| 10% | 10.477ms | 20.465ms |
| 100% | 98.039ms | 172.637ms |

1% 쓰기에서 전체 범위를 처리한 대조군은 각각 99.781ms, 172.442ms다. 1GiB·1%의
bitmap 수집·재설정·선별·hash와 cache 처리를 합친 p50은 1.171ms, HLT 이후 TCP ACK까지는
1.562ms였다. 단계 합계는 회차별로 합산한 뒤 p50을 구하며 단계별 p50을 더하지 않는다.
Nested PML은 에뮬레이션이고 native 대조군과 커널도 다르므로 tracking 비용을 EMR의
hardware PML 비용으로 해석하지 않는다. Dirty logging on/off 차이는 순수 PML 명령 비용이 아니다.
이 결과를 기존 handoff 수치에 더해 전체 전환 시간을 측정했다고 해석해서도 안 된다.

Git에 포함한 재현용 요약과 그림은
[summary.json](results/cache-pml-20261021/summary.json),
[cache-maintenance.png](results/cache-pml-20261021/cache-maintenance.png)다.
원시 로그와 진단 trace는 `.local/cache-pml-20261021/` 및 개별 run에 유지한다.
각 회차의 계측 JSON은 같은 공개 결과 디렉토리의 `raw-epochs.jsonl`에도 보존한다.

결과 JSON 안의 절대 `.local` 경로는 측정 당시의 provenance이며 다른 clone에 존재하는
파일 목록을 뜻하지 않는다. VM 이미지·원시 trace·vendor 소스와 빌드 cache는 Git에서 제외한다.
공개 그림은 저장소에 포함된 비교 JSON으로 다시 만들 수 있다(`matplotlib`, `numpy` 필요).

```bash
python3 implementation/virtual_machine/results/cache-pml-20261021/plot.py
python3 implementation/virtual_machine/results/dax-scale-20261020/plot.py
python3 implementation/virtual_machine/results/dax-scale-20261020/plot.py --with-full-migration
python3 implementation/virtual_machine/results/network-fairness-20261020/plot.py
```

### EMR에서 가능한 관측과 cache-line 선별

Emerald Rapids(5세대 Xeon Scalable)는 64B line, 코어별 48KiB L1D·2MiB L2와
SKU별 최대 320MB의 non-inclusive LLC를 사용한다. LLC에 없더라도 다른 코어의 L1/L2에
dirty line이 남을 수 있다. CHA는 coherence와 디렉터리를 관리하지만, 공개 소프트웨어
인터페이스에서 특정 VM의 모든 resident/dirty line 주소를 열거하는 기능은 확인하지 못했다.
[Intel 캐시 사양](https://www.intel.com/content/www/us/en/support/articles/000027820/processors/intel-xeon-processors.html),
[EMR CHA 명세 §2.2](https://cdrdv2-public.intel.com/817509/817509-EMR_XCC_UPG_Guide-Rev_001.pdf)

PEBS `MEM_TRANS_RETIRED.STORE_SAMPLE`은 write 주소의 표본, CHA PMU는 lookup·eviction
카운터, RDT CMT/MBM은 cache 점유량·traffic 집계를 제공한다. 성능 원인 분석에는 사용할 수
있지만 관측되지 않은 line의 flush를 안전하게 생략할 근거는 아니다.
[EMR PMU](https://perfmon-events.intel.com/platforms/emeraldrapids/core-events/core/),
[Linux resctrl](https://docs.kernel.org/filesystems/resctrl.html)

정확한 line 선별의 후보는 누락 없는 write 계측으로 페이지마다 64bit line mask를 유지하는
것이다. 전체 1GiB에 미리 할당하면 2MiB metadata가 필요하다. 한 line만 쓴 페이지는
64회 대신 1회 처리할 수 있지만 guest 실행 중 bitmap 갱신 비용을 지불한다. PML은 어느
페이지가 변경됐는지만 알려주므로, sampling에서 누락된 페이지 내부 line을 보완하지 못한다.
현재 cache-line tracking은 구현하지 않았다.

### DSA Cache Flush와 주기적 preflush: 조사·후속 설계

**주기적 preflush는 DSA 기반으로 설계하며 아직 구현하거나 측정하지 않았다.**
CPU cache 명령은 기존 전환 시 처리 baseline으로 유지한다. EMR용 후보는 DSA Gen1의
Cache Flush(opcode `0x20`)다. 주소·길이 범위를 제출하고 completion을 확인하는 작업이며,
dirty/resident line의 주소 목록을 반환하는 기능은 아니다. 실제 서버의 DSA 활성화,
`OPCAP[32]`, work queue·DAX 주소 변환·완료 순서를 먼저 확인해야 한다.
`GENCAP[3]`이 1일 때만 Cache Control로 cache 유지 writeback을 요청할 수 있다.
지원되지 않으면 해당 flag는 reserved이며 0으로 두어야 한다. 기본 invalidate 동작을
주기적으로 사용하면 guest의 hot cache를 비우는 비용이 생길 수 있다. EMR 지원 여부를
최신 DSA Gen2 명세만으로 단정하지 않는다.
[Intel DSA Rev.1.2 §8.3.17·GENCAP·OPCAP(보존본)](https://kib.kiev.ua/x86docs/Intel/DSA/341204-003.pdf)

DSA는 CPU의 line별 명령 발행을 줄이는 후보지만, 범위 병합·descriptor 준비·제출·queue
대기·실제 처리·completion 비용을 포함해야 한다. 작은 sparse 범위에서는 CPU가 더 빠를 수 있다.
공개 조사에서 EMR의 DSA 대 CPU cache flush를 직접 비교한 수치는 확보하지 못했다.
Intel/UIUC의 DSA 성능 논문도 Cache Flush를 분석에서 제외하므로 memcpy 대역폭을 flush
속도로 환산하지 않는다.
[DSA 성능 분석 논문](https://arxiv.org/html/2305.02480v2)

주기적 preflush는 실행 중 처리한 양만큼 전환 시 writer의 잔여 일을 줄이는 설계다.
재쓰기를 놓치지 않도록 **GET → 누적 변경 집합에 합치기 → CLEAR → flush → 완료 확인**
순서로 한다. Flush 뒤에 CLEAR하면 그 사이의 재쓰기를 지울 수 있다. 구현 시 kernel의
dirty-log 동기화와 모든 writer를 함께 검증해야 한다.
[KVM manual dirty logging](https://www.kernel.org/doc/html/latest/virt/kvm/api.html#kvm-cap-manual-dirty-log-protect2)

```text
실행 중: 반복 harvest → reader용 누적 집합 U 갱신 → rearm → 비동기 flush
전환 시: 모든 writer 정지 → 최종 로그 회수 → 진행 중 flush 완료 확인
         → 마지막 재쓰기 집합 flush 완료 → U 게시 → reader invalidate → 재개
```

Writer에서 이미 writeback된 line도 reader에는 stale 복사본이 남을 수 있다. 따라서 reader에는
최종 잔여 집합뿐 아니라 마지막 동기화 이후의 **전체 변경 집합 U**가 필요하다. 전환 지연 감소와
전체 실행 비용 감소를 구분하여 CPU 전환 시 처리, DSA 전환 시 처리, DSA 주기적 preflush를 비교할 계획이다.
측정 지표는 전환 p50/p99, guest 실행 성능 저하, background CPU·queue 비용과 재쓰기량이다.

## 실제 microVM handoff의 남은 문제: 2026-10-21 코드 재조사

이번 항목은 소스·공식 문서를 조사한 설계 검토이며 새 handoff 구현이나 성능 측정 결과가 아니다.
현재 direct Linux 경로는 A의 Firecracker로 부팅한 뒤 B의 `shared-probe`가 동일 KVM 객체를
실행한다. Source VMM은 A에 남는다(`lab.py:direct_handoff_bench`).
`reuse.rs:diskless()`는 block/net/vsock 등을 거부하고 `vmx.c:vmx_dax_handoff()`는
**PML 활성화를 거부**한다. Export도 dirty bitmap/ring과 남은 IRQFD/IOEVENTFD를 거부한다.
따라서 별도 PML cache benchmark, CPU/RAM handoff, 서비스 I/O 승계는 각각의 검증 범위다.

DSA flush 최적화에서 먼저 해결해야 할 사항은 다음과 같다.

- **주소 접근:** CPU가 DAX를 mmap했다고 DSA도 접근할 수 있는 것은 아니다. Node별 WQ와
  PASID/SVA·IOMMU 변환 및 device-DAX 접근을 확인한다. KVM DAX pool의 kernel alias도
  Firecracker guest RAM HVA와 별도의 주소·권한 경로가 필요하다. Queue/portal/PASID와
  completion buffer는 각 host에서 준비하고, raw runtime을 다른 host에 넘기지 않는다.
  [Linux SVA/ENQCMD](https://docs.kernel.org/arch/x86/sva.html)
- **범위와 batching:** 연속 dirty page를 run으로 합쳐 descriptor를 줄이고 여러 run을 batch로
  제출한다. Clean gap 병합은 추가 처리량과 descriptor 비용의 교환이며 VM allocation 경계를
  넘지 않는다. 서로 다른 owner의 metadata는 cache line을 공유하지 않도록 배치해야 한다.
- **추적 범위:** Guest PML 외에 VMM RX/block-read completion/virtqueue 쓰기, KVM 객체,
  VMCS backing, EPT table의 software/hardware 갱신과 control metadata를 포함해야 한다.
  Guest page table의 A/D 변경은 guest RAM에 속하지만 EPT table 자체의 A/D 변경은 별도다.
  기존 Firecracker software bitmap도 raw queue pointer를 얻을 때 표시하는 경로가 있으므로
  매 epoch bitmap을 지운 뒤 모든 후속 쓰기가 다시 표시되는지 검증해야 한다.
- **완료 세대:** `GET → U 합치기 → CLEAR → DSA 제출 → 해당 세대 성공 완료` 순서를 유지한다.
  이전 completion이 이후 재쓰기 기록을 지우면 안 된다. Timeout은 완료 또는 abort/drain이
  확인되기 전까지 ownership 게시를 막는다. Enqueue 수락과 작업 완료를 구분하고 batch의
  child 오류·부분 완료도 확인한다. CPU fence는 DSA 완료를 대신하지 않는다.
- **Reader:** 마지막 동기화 이후 전체 변경 집합 U를 invalidate한다. Writer의 잔여 집합만
  보내면 안 된다. CLFLUSHOPT와 DSA invalidate는 dirty line을 writeback할 수 있으므로
  reader에는 stale clean copy만 남을 수 있도록 이전 owner의 쓰기를 먼저 drain해야 한다.
- **정책:** 반복 쓰기와 queue backlog를 보고 background budget을 제한할 수 있다. Hot page의
  preflush를 늦추더라도 최종 flush에서는 제외하지 않는다. Reader invalidate와 bitmap/rearm,
  descriptor·enqueue·completion 비용이 각각 남으므로 DSA 속도 향상을 미리 수치로 단정하지 않는다.

PML을 실제 direct 경로에 결합하려면 PML buffer·index·dirty bitmap의 수명, 마지막 drain과
destination 재연결부터 설계해야 한다. Guest CPU dirty만으로 공유 KVM graph 전체의 cache
일관성을 검증할 수는 없다. Gen1 cache 동작·capability는 위 DSA 명세를 기준으로 확인한다.
현재 `kvm_dax_vm_ioctl()`의 detach는 바로 `DETACHED`를 게시하므로, DSA 완료 전 attach를
막는 ready generation이나 게시 지연도 필요하다. 기존 detach 뒤에 flush만 덧붙여서는 안 된다.

Guest 메모리에 포함되는 상태와 host의 I/O 연결은 다음과 같이 나뉜다.

| 대상 | Guest RAM에 보존되는 것 | 각 host에서 준비·차단·승계할 것 |
|---|---|---|
| TCP 서비스 | Guest socket/seq/ack/retransmit, guest에서 종단한 TLS·application 상태 | TAP, routing/FDB/ARP, NAT/conntrack, proxy/NIC queue와 ingress 경로 |
| Virtio 장치 | Descriptor/avail/used ring bytes | Queue cursor·파싱한 HVA pointer·pending request, eventfd/epoll/IRQFD/IOEVENTFD |
| Vsock | Guest socket/driver/ring | Host UDS·connection map·credit/buffer 상태; 현재 snapshot은 reset/reconnect 의미 |
| Guest 파일 | Guest FD/offset, inode/dentry/mount와 page cache, tmpfs 데이터 | Disk image FD·host page cache·io_uring·storage lease/visibility |

일반 TCP를 유지하려면 외부 peer가 보는 endpoint와 패킷 경로를 유지해야 한다. 같은 guest IP/MAC과
새 TAP만으로 host NAT/conntrack이 이전되지는 않는다. 초기 설계 후보는 고정 gateway에서
한 owner의 TAP으로만 전달하는 방식이다. Ingress 전환 중 buffer/drop/forward 정책과 TCP
재전송에 따른 서비스 공백을 측정해야 한다. 기존 L1 migration TCP 대역폭 측정은 L2 서비스
연결 승계의 검증이 아니다. Firecracker 공식 snapshot도 packet loss와 연결 유지를 보장하지
않으며 vsock 연결은 reset한다.
[Firecracker snapshot 제한](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md#limitations),
[Vsock reset](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md#vsock-device-reset)
아래 MPTCP 시험에서는 기존 연결에 다른 주소의 subflow를 추가하므로 node별 주소를 사용할 수 있다.
각 node까지의 도달 가능성과 virtio/backend 전환은 별도로 준비한다.

Filesystem은 세 경우를 구분한다. Initramfs/tmpfs 데이터는 guest RAM에 있다. 하나의 guest
filesystem을 유지하면서 shared virtual disk의 backend owner만 바꾸는 경우에는 동일 disk
blocks와 단일 writer를 보장해야 한다. 반면 host 둘이 image를 저장하는 filesystem 자체를
공유하려면 host cache/locking/lease 일관성이 필요하다. 일반 ext4/XFS의 raw volume을 두
kernel에서 동시에 mount하는 방식으로 해결하면 안 된다. DAX는 이런 조정을 제공하지 않는다.
[Linux DAX](https://docs.kernel.org/filesystems/dax.html),
[GFS2의 inter-node locking](https://www.kernel.org/doc/html/latest/filesystems/gfs2/index.html)

현재 block snapshot은 image bytes를 저장하지 않고 path/config와 장치 상태를 저장한다.
Async engine의 정상 경로는 outstanding I/O를 drain하고 host `sync_all()`을 수행한 뒤 completion을 guest에
반영한다(`block/virtio/device.rs:prepare_save`, `io/async_io.rs:drain_and_flush`).
이 과정이 interrupt를 만들 수 있어 `Vmm::save_state()`는 장치 상태를 KVM 상태보다 먼저 저장한다.
직접 handoff에도 같은 논리적 일관성 경계가 필요하다. Drain 실패와 storage flush 실패는 구분해
소유권 이전·오류 보고 정책을 정해야 한다. 현재 block wrapper는 실패를 log로 남기고 진행하므로
drain 성공을 별도로 확인하고, quiesce를 입증하지 못하면 ownership commit을 중단해야 한다.
Cache writeback 완료, 다른 host에서의 visibility,
전원 장애 후 storage durability도 각각의 보장이다.

제안하는 정상 전환의 순서는 다음과 같다. Destination의 mapping·backend·DSA queue는
미리 준비할 수 있지만 새 owner가 되기 전에는 guest ring을 처리하거나 disk를 쓰면 안 된다.

```text
새 I/O 접수 차단 → 모든 vCPU 정지 → device/kernel/DMA writer drain
→ completion·queue·interrupt 상태 확정 → CPU 상태 저장 또는 detach/VMCLEAR
→ 최종 dirty 수집 + 공유 metadata flush + DSA 성공 완료·visibility 확인
→ source 실행/쓰기 권한 차단 확인 + ownership commit
→ destination invalidate(guest U + 공유 metadata) → graph 검사·attach/host 연결 재설정
→ I/O 경로 전환 → vCPU 실행
→ 외부 client의 첫 정상 응답
```

비일관 host 사이에서는 현재 WB DAX control word의 acquire/release와 cmpxchg만으로 전역
가시성·원자성을 보장할 수 없다. 플랫폼이 지원하는 control/atomic 방식이나 외부 authority와
fencing이 필요하다. B 실행 뒤 ACK만 유실됐을 때 A를 다시 실행하면 중복 write가 생길 수 있다.
Commit 전 취소와 commit 후 복구를 구분하고 request ID·중복 제거 등 application 계약을 정한다.
Source cache/CPU에만 있던 상태는 source crash 시 읽을 수 없으므로 정상 handoff와 crash 복구의
보장도 구분한다. [QEMU 장치 migration 설계](https://www.qemu.org/docs/master/devel/migration/main.html#general-advice-for-device-developers)

물리 노드에서 EPT를 그대로 쓰려면 모든 leaf의 GPA→HPA뿐 아니라 root/non-leaf table의
HPA도 같은 backing을 의미해야 한다. 같은 DPA/HVA로는 충분하지 않다. VMCS revision·VMX
controls·CPU feature/XSAVE·clock 호환성과 destination의 host fields/VPID/TLB 처리도 필요하다.
[Linux CXL 주소 domain 설명](https://www.kernel.org/doc/html/latest/driver-api/cxl/conventions/cxl-atl.html)
현재 고정 주소 검사와 첫/끝 PFN 검사는 전체 pointer graph와 모든 중간 PFN의 불변성을 보장하는
완전한 검사로 보지 않는다. 다중 vCPU barrier, DAX arena 회수·재할당도 아직 미검증이다.

설계 후보는 CPU/KVM graph의 direct 승계를 유지하면서 장치의 작은 논리 상태만 전달하고,
TAP·file·eventfd 같은 backend는 목적지의 것으로 재연결하는 방식이다. 전체 guest RAM을
snapshot/copy해야 한다는 뜻은 아니다. 고정 I/O gateway를 쓰면 연결·storage 소유권 전환을
줄일 여지가 있지만 평상시 원격 I/O 경로와 gateway 장애 처리가 새 비용이 된다.

### Capybara·공유 packet buffer·NFS·DSec 비교

[Capybara(SIGCOMM '26) §4.1](https://www.comp.nus.edu.sg/~lijl/papers/capybara_sigcomm26.pdf)는
목적지 buffer 준비 → switch forwarding 변경 → TCP RX/TX 대기 데이터와 상위 session 상태
전달 → buffered packet 처리를 사용한다. Kernel-bypass TCP와 programmable switch의 공동
설계다. 논문의 TCP 전환 시간은 전체 microVM handoff 시간이 아니다.

우리 normal virtio-net 구성에서는 guest TCP 상태·재전송 buffer·application 상태가 이미 CXL
guest RAM에 있다. 현재 Firecracker RX도 `read_tap()`에서 guest RX descriptor에 직접 readv한다.
추가 공유의 주 대상은 전환 중 NIC/TAP 쪽에 남아 아직 guest에 전달되지 않은 packet이다.
Guest TCP export/import보다 virtio/backend ownership과 packet cutover에 집중할 수 있다.
아래는 논문의 CXL 구현 결과가 아니라 우리 환경에 적용하는 설계 후보다.

| 후보 | 전환 방식 | 남는 조건 |
|---|---|---|
| 고정 network backend + CXL RX/TX ring | Backend가 계속 수신, A/B의 consumer ownership만 변경 | Backend 분리·병목·장애 의존성, guest 전달 copy 비용 |
| Capybara식 forwarding 전환 + CXL buffer | B 준비 후 경로 변경, A에 남은 packet까지 B가 처리 | Old NIC/TAP queue drain·재정렬·late DMA·queue cursor/IRQ 처리 |

CXL에는 payload와 `offset/length/epoch/packet_id` descriptor를 두고 host pointer를 공유하지
않는 방향을 검토한다. NIC/TAP FD·DMA/IOMMU mapping은 node별로 준비한다. Linux `sk_buff`
전체 graph를 그대로 넘기는 것과는 다르다. 공유 staging ring은 추가 copy/traffic을 만들 수 있어
현재 direct-to-guest 경로와 비교해야 한다. TCP 연결 유지와 응답 지연 0도 구분한다.
[Linux sk_buff](https://docs.kernel.org/networking/skbuff.html),
[DMA 주소 체계](https://docs.kernel.org/core-api/dma-api-howto.html)

NFS는 mount 위치에 따라 다르다. 다음은 NFS over TCP로 같은 server와 export를 유지하는 가정이다.

| 구성 | CXL handoff와의 관계 |
|---|---|
| Guest가 직접 NFS mount | Guest의 FD/offset/page cache/NFS client·lock 상태가 함께 유지된다. Network와 lease/timer를 유지하면 같은 client의 실행 위치만 바꾸는 설계가 가능하다. |
| Host A/B가 각각 NFS mount 후 같은 disk image 제공 | 별도 NFS client/cache/FD이므로 backend drain, 쓰기 visibility, B cache 검증과 단일 writer fencing이 필요하다. |

첫 구성에서는 정상 handoff마다 모든 파일 close/reopen이나 guest cache 전체 fsync를 강제할
필요가 있다고 가정하지 않는다. 같은 guest의 dirty cache와 진행 중 RPC 상태를 유지하기
때문이다. 이는 설계 추론이며 network device 승계까지 검증한 결과는 아니다. NFSv4의 client
identity·lease를 유지하고, 장시간 중단이나 server recovery는 별도 처리한다.
[NFS client identity](https://www.kernel.org/doc/html/latest/admin-guide/nfs/nfs-client.html#the-nfs4-unique-id-parameter),
[NFSv4.1 session/retry](https://www.rfc-editor.org/rfc/rfc8881.html#section-2.10.6)
현재 `kernel.config`는 NFSv4.1이 꺼져 있으므로 4.1 session 실험에는 해당 설정이 필요하다.

[DSec 공개 논문 §3.3·5.3·6.3](https://arxiv.org/pdf/2609.22978)은 microVM의 memory/execution
snapshot 후 Firecracker를 종료하고 새 process로 복원한다. SDK의 다음 요청 전에 자동 resume하는
의미이며 기존 외부 TCP의 cross-node 무중단 승계를 보고한 것은 아니다. Image는 3FS에서
공유·on-demand load하지만 runtime writes는 node-local이다. VM 제어 경로에는 vsock proxy를 쓴다.
이는 우리 NFS writable-data 공유 가정과 구분한다. 다음 비교의 우리 항목은 목표 설계다.

| 항목 | Capybara | DSec | 우리 목표 |
|---|---|---|---|
| 전환 단위 | TCP connection + 상위 session | Sandbox lifecycle의 snapshot/resume | Guest 실행 주체와 device/backend ownership |
| 메모리 | Connection state 전달 | Memory/execution snapshot | 동일 CXL guest RAM 유지 |
| 외부 연결 | Planned migration의 TCP 연속성 | SDK 요청 관점의 재개 | Guest application 연결을 유지하고 host storage 경로를 재연결 |
| Storage | 서비스 backend 이용 | 3FS image + local writable layer | Host NFS의 RO lower + guest tmpfs upper를 먼저 검증; persistent 쓰기 후속 |

초기 설계에서는 initramfs/tmpfs root와 guest NFS workspace를 후보로 검토했다. 현재는
host NFS로 image를 연결하는 경로를 먼저 구현했다. Guest NFS 후보를 추후 시험한다면
HTTP뿐 아니라 NFS 등 guest flow의 수신 경로도 함께 전환해야 한다.
이 구성은 block backend 승계 범위를 줄이지만 virtio-net 상태·IRQ·packet 순서·단일 owner 검증은
남는다. Application 요청 ID·TCP reset/retransmit·NFS read/write/fsync/lock 결과를 함께 확인한다.

E2E 검증은 실제 Linux `TCP/MPTCP + tmpfs`와 2 vCPU의 `RO disk + tmpfs upper`를
각각 통과했다. 이후 두 경로의 통합, 단일 writer persistent disk의 `write→fsync→response`,
async I/O, direct/reuse의 다중 vCPU로 확장한다.
전환 단계별 장애·ACK 유실·late I/O·DSA timeout을 주입하고, request ID·파일 내용·lost IRQ·
동시 owner를 검사한다. 최종 지표는 heartbeat뿐 아니라 **외부 client의 application blackout**,
정상 실행 throughput, 요청 중복/누락, drain·DSA·invalidate·host rebind·routing·첫 응답의
단계별 시간이다. Direct/reuse의 디바이스 승계와 이 전체 범위의 성능 검증은 남아 있다.

<a id="mptcp-session"></a>

## MPTCP 설계와 실행 검증

MPTCP는 **guest Linux의 transport 구현**이다. Application은 하나의 stream socket을 사용하고,
guest kernel이 subflow·sequence·재전송을 관리한다. Firecracker는 virtio-net/TAP을 통해 packet을
전달한다. Guest의 MPTCP socket·key/token·송수신 buffer는 이미 CXL guest RAM에 포함되며,
host TAP/NIC queue와 eventfd 같은 연결은 각 host의 자원이다.

커널의 `CONFIG_MPTCP=y`, `net.mptcp.enabled=1`만으로 기존 TCP socket이 자동 변환되지는 않는다.
양 끝에서 MPTCP socket을 선택해야 한다. 이번에는 `mptcp_probe.h`의 `socket()`에서
`IPPROTO_TCP` 대신 `IPPROTO_MPTCP`를 지정했으며 일반 `connect/accept/read/write`를 사용했다.

| 적용 방법 | Application 소스 수정 | 이번 검증 |
|---|---|---|
| `socket(..., IPPROTO_MPTCP)` 또는 runtime의 MPTCP 옵션 | 직접 지원 또는 설정 필요 | Probe에서 직접 선택 |
| `mptcpize`의 `LD_PRELOAD` wrapper | 지원되는 동적 링크 프로그램은 불필요 | 미검증; 현재 static probe에는 적용하지 않음 |
| eBPF로 생성할 socket의 protocol 선택 | 불필요; kernel 기능·정책 필요 | 현재 DCD 6.6-rc6 빌드에서 미검증 |

[Linux socket API](https://docs.kernel.org/networking/mptcp.html#sockets-api),
[MPTCP 적용 방법](https://www.mptcp.dev/setup.html).
일반 application마다 migration 코드를 넣는 대신 guest 관리 hook이 새 주소와 경로를 설정할 수 있다.
이번에는 `l2-init.sh`가 복원 gate를 기다린 뒤 주소를 추가하고 generic netlink로 path manager를
호출한다. Host가 바뀌어도 guest vNIC가 그대로이므로 이 hook이 자동으로 생기는 것은 아니다.

기존 MPTCP 연결은 A 주소의 경로에서 B 주소의 경로로 전환할 수 있다. B 주소가 일반 IP routing으로
도달 가능하면 전환 후 A를 경유할 필요가 없고, 이동마다 programmable switch를 조작할 필요도 없다.
다만 새 경로의 route·firewall/NAT·listener와 양 끝의 MPTCP 협상이 필요하다. 일반 TCP fallback,
UDP traffic 및 신규 client의 service discovery/LB/DNS 진입 경로는 이 연결 승계에 포함되지 않는다.
MP_JOIN·재전송·application timeout도 남으므로 연결 유지가 무지연을 의미하지 않는다.
[경로 관리](https://www.mptcp.dev/#path-manager), [NAT/listener 조건](https://www.mptcp.dev/faq.html#my-server-app-rejects-the-creation-of-new-subflows)

MPTCP와 TUN을 켠 별도 커널·이미지로 실행한다. 기본 커널 설정과 기존 이미지는 보존한다.
새 실행 파일 대신 기존 `shared-probe`에 socket/경로 검증을 추가했다. 아래는 **현재 서버에 남아
있는 검증된 image를 재사용**하는 명령이다. 2026-10-23에 경로 존재와 init/probe source hash의
manifest 일치를 확인했다. 기존 `mptcp-20261022` 세션의 두 VM·전용 host network는 10월 22일
시험 종료 때 모두 정리했고, selector와 결과는 보존했다. 새 session 이름으로 새 run을 만든다.

```bash
cd /home/ijkim/agent-sandbox-cxl
export CXL_LAB_SESSION=mptcp-next
export CXL_LAB_ASSETS="$PWD/implementation/virtual_machine/.local/assets-mptcp-20261022"
export CXL_LAB_KERNEL_BUILD="$PWD/implementation/virtual_machine/.local/kernel-build-mptcp-20261022"
export CXL_LAB_KERNEL_MODULES="$PWD/implementation/virtual_machine/.local/kernel-modules-mptcp-20261022"
export CXL_LAB_KERNEL_CONFIG="$PWD/implementation/virtual_machine/.local/mptcp-20261022/kernel.config"
taskset -c 0-5 python3 implementation/virtual_machine/lab.py start \
  --vcpus 2 --memory-mib 2048 --cxl-mib 2048 --dax-guest --dax-4k \
  --migration-network --network-backend tap \
  --qemu "$PWD/implementation/virtual_machine/.local/qemu-cxl/build/qemu-system-x86_64"
python3 implementation/virtual_machine/lab.py mptcp-test --protocol tcp --rounds 1000
python3 implementation/virtual_machine/lab.py mptcp-test --protocol mptcp --switch-path --rounds 4000
python3 implementation/virtual_machine/lab.py mptcp-test --protocol mptcp --switch-path --snapshot-handoff --rounds 4000
python3 implementation/virtual_machine/lab.py mptcp-test --protocol mptcp --pause-ms 100 --rounds 1000
python3 implementation/virtual_machine/lab.py stop
```

CPU 0–5는 직전 시험의 범위이므로 실행 전 현재 자원 사용을 확인한다. 실제 CXL QEMU는 10.2.2다.
Manifest의 준비 도구 QEMU 6.2와 혼동하지 말고 `start --qemu`를 명시한다.
Init/probe 수정 시 같은 환경변수에서 `lab.py prepare`를 다시 실행한다. 해당 assets를 사용하는 VM은
먼저 종료한다. Kernel 수정 시 `MPTCP_TEST=1 bash implementation/virtual_machine/build.sh kernel`
다음 `prepare` 순서다. 새 cache를 만들 때는 위 build/modules/config/assets를 별도 경로로 지정하고
기준 `kernel.config`를 새 config 경로에 복사한 뒤 빌드한다. 이 경로들의 기본값은 기존 baseline이다.

128MiB DAX-backed L2 guest와 L1 B의 peer 사이에서 1KiB record를 양방향 검증한다.
`--switch-path`는 guest 주소 `10.88.0.2`로 연결한 뒤 `10.89.0.2` subflow를 추가하고
기존 subflow를 닫는다. Token·socket cookie·sequence·payload와 subflow별 TCP 상태·전송량을
확인한다. 닫힌 subflow가 조회 목록에 남을 수 있으므로 주소 개수만으로 성공을 판단하지 않는다.
`--pause-ms 100`으로 실행 중 일시 정지도 시험할 수 있다.

`--snapshot-handoff`는 source ingress를 차단하고 pause·state-only snapshot·source 종료 후
B에서 네트워크 장치까지 복원한다. B 복원 후 guest hook이 새 subflow를 만든다. 기존 주소는
blackhole로 차단하며 peer의 socket은 계속 유지한다. RAM은 복사하지 않지만 KVM/EPT는 새로
만든다. 작은 state 파일은 이 시험에서 serial controller로 전달하므로 전체 소요 시간을 기존
DAX mailbox나 최적화한 handoff의 성능으로 해석하지 않는다. 50ms ingress quiet는 통제된
실험 조건이며 일반 NIC/backend drain 보장이 아니다.

실제 적용에는 양 끝의 MPTCP 지원, MPTCP socket 사용, 목적지의 새 주소·route/TAP 준비와
guest path-manager hook이 필요하다. Direct/reuse의 네트워크 장치 승계와 CXL packet staging
queue는 구현하지 않았다. 기존 guest TCP/MPTCP 상태와 송수신 buffer는 guest RAM에 포함된다.
현재 검증은 **이전하는 guest가 client이고 peer의 listen socket이 유지되는 연결**이다.
Guest가 server인 inbound 연결에서 새 주소를 동적으로 알리는 설계는 `ADD_ADDR`와 remote client의
path-manager 참여가 필요하다. 미리 알린 주소 등 다른 도달 방법도 가능하다. 기존 경로로 알림을
전달해야 하는 구성에서는 그 전에 경로를 차단하면 안 되므로, 현재의 차단 순서를 그대로 적용할 수 없다.
Client/server 양방향의 자동 이전은 추가 구현 대상이다.
이 시험은 한 물리 호스트의 coherent QEMU 환경이며 비일관 CXL의 flush·DMA correctness나
평상시 bulk throughput overhead를 검증하지 않는다. 결과는 각 run의 `mptcp-*/report.json`에 남긴다.
Peer는 고정된 L1 B이고 이동 후 guest도 B에 있으므로 이전 전후 RTT의 경로 길이가 다르다.

2026-10-22 실행에서 TCP 대조군 1,000회, MPTCP 경로 교체 4,000회, 실제 A→B snapshot
이전 4,000회, 100ms pause 요청 대조군 1,000회가 모두 오류 없이 완료됐다.
이전 시 상태 파일은 13,904바이트였다. Peer가 관측한 최대 응답 간격은 5.770초였으며
serial state 전달 4.548초가 포함된다. 기능 확인용 제어 경로의 수치다.
Pause 중 guest clock은 정지 시간을 제외할 수 있으므로 blackout은 고정 peer의 시계로 평가한다.
전체 event·artifact hash·단계별 시간은 [저장 결과](results/mptcp-20261022/summary.json)에 보존했다.

재현할 때 주의할 구현 사항은 다음과 같다.

- Peer의 listen socket을 첫 `accept` 뒤에도 유지한다. 새 MP_JOIN SYN 처리에 필요하다.
- `MPTCP_FULL_INFO`의 TCP 상태와 경로별 bytes로 검증한다. 닫히는 subflow가 목록에 남을 수 있다.
  양쪽 token/cookie 불변, client connect 1회·server connect 0회, payload/sequence 오류 0도 확인한다.
- Source vCPU pause만으로 Firecracker RX의 guest RAM 쓰기는 멈추지 않는다. 현재 ingress 차단과
  50ms quiet는 통제된 실험 조건이며, 실제 handoff에는 backend/DMA writer의 종료 확인이 필요하다.
- 현재 BusyBox에는 `base64` applet이 없고 긴 shell 입력 줄도 잘린다. State 전달은 기존 `uudecode`와
  512자 chunk를 쓰고 destination SHA-256을 확인한다. 빠른 전환 측정에는 이 serial 전달을 교체한다.
- 실패 시에도 source/destination FC·peer 종료와 route/TAP/변경한 sysctl 정리를 독립 실행한다.
  `cleanup_errors`가 있으면 실패로 보고 diagnostic log를 보존한다. 다른 session의 VM을 일괄 종료하지 않는다.
- 현재 peer B의 MPTCP path-manager 한도는 `subflows=2`, `accepted ADD_ADDR=2`로 설정하며
  시험 종료 시 원래 값으로 복원하지 않는다. 전용 lab VM에서 실행하고 세션 종료 후 `lab.py stop`으로
  정리한다. 같은 B에서 다른 MPTCP 실험을 이어갈 때는 이 설정을 고려한다. 원래 한도 조회·복원은
  후속 구현 대상이며, `cleanup_errors=[]`가 모든 network namespace 설정의 원복을 뜻하지는 않는다.

## 코드 위치와 범위

- `lab.py`: 기본 이미지 준비, 두 VM 관리, CPU/RAM·MPTCP 검증·측정.
- `nfs_bench.py`, `trace_prepare.py`, `trace_bench.py`: 공통 host NFS 설정, synthetic 검사와 실제 trace 준비·이전.
- `verify_nfs_results.py`, `verify_trace_results.py`: 원시 로그·counter·state·출력의 독립 검증.
- `build.sh`: kernel/QEMU/Firecracker 빌드 및 관련 테스트의 단일 진입점.
- `shared_probe.c`: 기존 `shared-probe` 안에 공유 메모리 protocol, 전환 controller, 계측을 유지한다.
- `cache_probe.h`: `shared_probe.c`에 포함되는 cache/PML 계측 코드이며 실행 파일은 추가하지 않는다.
- `mptcp_probe.h`: 같은 실행 파일의 TCP/MPTCP stream·generic-netlink path-manager 검증 코드다.
- `guest-init.sh`, `guest-cxl.sh`, `l2-init.sh`: initramfs 안에서 실행되는 부팅·DAX 설정 코드다.
- `firecracker/src/vmm/src/persist.rs`, `vstate/memory.rs`: DAX RAM 유지 snapshot과 restore.
- `firecracker/src/vmm/src/reuse.rs`: 이미 사용한 worker의 호환성 검사·준비·의미적 복원.
- `firecracker/src/vmm/src/lib.rs`, `vstate/vcpu.rs`: API/VMM/vCPU 연동 및 direct 실행 제어.
- `implementation/linux/virt/kvm/`, `arch/x86/kvm/`: direct 경로의 DAX allocator·detach/attach·EPT 처리.

Reuse/direct의 기본 검증은 diskless 1 vCPU, 동일 구성·동일 DAX allocation이다. block/net/vsock/PCI 장치를
포함한 worker 재사용은 구현하지 않았다. 이 경로의 다중 vCPU, 다른 물리 호스트의 TSC/캐시 일관성,
임의 이미지 간 pool 재할당도 추가 검증이 필요하다. 이 실험을 일반 CPU context switch와
동일한 보장이나 비용을 가진 구현으로 보지 않는다.

정리 전 README·분리된 빌드/추적 스크립트·중간 패치는
`.local/cleanup-latest.json`이 가리키는 `before-cleanup.tar.gz`에 보존했다.
기존 `.local` 실행 결과와 원시 로그도 유지한다. 현재 운영 설명은 이 README 하나로 관리한다.

## Immutable data-page pooling prototype (2026-10-30)

Virtio-blk의 성공한 읽기에서 `(image digest, image offset, GPA)` hint를 수집하고,
paused VM의 현재 바이트를 검증한 뒤 동일한 canonical CXL frame에 연결하는 경로를 추가했다.
Guest inode/folio/LRU는 VM별로 유지한다. 모든 VM의 Bloom slot은 DRAM/CXL tier와 관계없이
CXL에 남으며, 정확한 후보 index는 controller/VMM의 private 상태다.

설계·주소 배치·재현 명령은 [data pooling 문서](../../docs/cxl-data-pooling.md),
실행 증거는 [pooling 결과](results/pool-20261030/README.md)에 정리한다.
`pool_prepare.py`, `pool_bench.py`, `pool_control.py`, `verify_pool_results.py`가 fixture,
다중 sandbox lifecycle, admission, 독립 검증을 담당한다.

Kernel에는 `devdax-pool-cow.patch`가 필요하며 기본값은 비활성화다. `lab.py start`의
`--pool-cow --dax-guest --dax-4k`가 전용 L1 kernel의 `device_dax.pool_cow=1`을 켠다.
NFS server용 `CONFIG_NFSD=y`와 같은 build의 KVM modules도 준비해야 한다.
공유 페이지에 대한 guest CPU 및 host HVA 쓰기는 private DRAM으로 CoW되므로, 쓰기 후
CXL tier는 혼합 배치가 될 수 있다. 실제 PFN/CoW의 낮은 계층 검증은
[CoW probe 결과](results/pool-cow-20261030/summary.json)에 보관한다.

이번 pooling 검증은 매 이전 때 논리적 RAM 전체를 snapshot으로 저장한다. Canonical overlay와
private CoW 페이지를 빠뜨리지 않기 위한 선택이며, **기존 state-only/direct handoff의 비용으로
해석하지 않는다.** Overlay가 있는 VM의 state-only DAX snapshot은 거부한다. NFS는 계속 host의
image FD 경로이고, A/CXL에서는 local image, B/CXL에서는 A의 image에 대한 NFS를 사용한다.

Canonical image를 미리 채우고 실험 동안 재사용하지 않으며, admission에서 원래 DAX reservation을
allocator에 반환하지 않는다. 실제 PFN 공유와 사용 가능 CXL 용량 증가를 구분한다. 현재는 paused
admission, immutable data, coherent QEMU/nested-KVM 검증 범위다. 온라인 read 우회,
donor export, frame 회수, 비일관 물리 CXL, pooling과 PML/MPTCP의 통합은 후속 작업이다.

### 수정하지 않은 Firecracker와의 비교

같은 upstream commit의 pristine VMM을 별도로 빌드하고, 3개 독립 sandbox × 5회 반복으로
stock DRAM, 수정본 DRAM/hint 비활성, CXL/hint 활성·공유 비활성, CXL/pooling을 비교했다.
현재 cache의 file offset→GPA→HVA→PFN을 측정해 stock의 payload 고유 frame 480MiB와
pooling의 160MiB를 확인했다. Stock의 동일 warm snapshot clone도 같은 160MiB를 공유하므로,
이를 독립 VM의 사후 pooling과 구분한다. 현재 CoW 재사용 비용은 크게 나타났고 원래 DAX
reservation을 반환하지 않으므로 순 용량 절감·성능 개선을 주장하지 않는다.

[기능·검증 범위](../../docs/firecracker-pooling-comparison.md),
[측정 결과와 재현·오프라인 감사](results/stock-comparison-20261030/README.md)를 참고한다.
Runtime admission을 사용하는 Firecracker는 전용 L1 안에서 `--no-seccomp`로 실행한다.
새 plan/report/sysfs/mapping 작업을 production seccomp 정책에 통합하는 작업도 남아 있다.
현재 file-cache 공유를 증명하는 검증에서는 guest의 측정용 `cachemap`이 read-only `MAP_SHARED`
페이지를 `mlock`하고 file offset→GPA를 기록한다. Pause 중 이 GPA를 VMM HVA와 canonical PFN에
연결하고, 검사 뒤 unpin한다. 이 기록은 admission plan 생성에 사용하지 않는다. 초기의
`mincore`와 과거 hint GPA PFN 각각의 검사와 보강된 현재 cache-page 대응 검사는 결과에서 구분한다.

### Guest의 명시적 폐기 알림과 private backing 교체

[페이지 폐기 설계](../../docs/cxl-page-discard.md)는 표준 Linux virtio-balloon free-page
reporting의 guest 격리 → host 처리 → used-ring ACK → allocator 반환 순서를 사용한다.
기존 pooling 환경의 VM ID/epoch 설정에 `FC_POOL_DISCARD=1`을 추가하며, balloon은
target 0·reporting 활성·hinting/stats/deflate-on-OOM 비활성 구성만 허용한다.
`FC_POOL_DISCARD_TRACE=1`은 검증용 손실 없는 batch 및 snapshot 경계 기록을 켠다.

Host는 그 VM에 설치했던 공유 overlay를 anonymous zero backing으로 교체한다.
Canonical과 다른 VM의 alias는 유지한다. 진행 중인 block I/O와 겹친 페이지는 건너뛰며,
report ACK와 실제 detach 성공은 별도로 집계한다. 보고 전 재사용은 기존 CoW로 처리한다.
Fresh backing의 실제 page allocation은 lazy이므로 첫 쓰기 비용 전체를 없애는 기능은 아니다.

`pool_discard_prepare.py`/`pool_discard_run.py`가 별도 fixture와 비교 실험을 만들고,
`verify_pool_discard.py`가 실제 guest ACK, GPA 재사용, host PFN, peer 데이터와 이전 경계를
검사한다. Guest trace patch는 계측용이며 filesystem lookup이나 allocator 정책을 바꾸지 않는다.
[실측 결과와 원본 검증](results/pool-discard-20261030/README.md)은 한 페이지 진단과
3개 VM의 반복·NFS·DRAM/CXL 이전, 보고 대기와 refill 비용을 구분한다.
원래 DAX reservation 반환, private backing의 CXL 배치, overlay를 포함한 direct handoff는
여전히 후속 작업이다.
