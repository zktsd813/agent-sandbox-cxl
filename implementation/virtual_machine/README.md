# CXL microVM 실행·전환 실험

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

## 구현한 세 경로

| 경로 | 전환할 때 하는 일 | 목적지에 유지되는 것 |
|---|---|---|
| `snapshot` | source 정지 → 의미적 상태 직렬화 → DAX mailbox 게시 → 목적지에서 KVM 객체 생성·복원 → 실행 | 공유 guest RAM; KVM VM/vCPU와 EPT는 새로 생성 |
| `reuse` | 같은 상태를 이미 실행 이력이 있는 목적지 worker에 복원 | 목적지의 KVM VM/vCPU FD, VMCS, thread, RAM mapping; 기존 IRQFD 정리는 복원 중 수행 |
| `reuse-prepared` | 비어 있는 worker의 연결 정리를 먼저 끝내고, 상태 도착 후 복원 | `reuse`와 같으며 목적지 정리를 전환 경로 밖에서 수행 |

별도로 **direct handoff** 구현도 보존했다. 수정 KVM이 `struct kvm`, vCPU, VMCS,
EPT 등의 지정 객체를 DAX에 할당하고 A에서 detach한 객체를 B에 attach한다.
일부 host 연결은 B의 것으로 다시 연결한다. snapshot이나 KVM VM 재생성이 없는,
주소·CPU 모델·물리 메모리 배치 제약이 강한 Intel 실험 경로다. 아래 기본 사용법은 의미적 snapshot 경로다.

의미적 snapshot은 raw `struct kvm`이나 VMCS 바이트를 직렬화하지 않는다.
Firecracker의 `MicrovmState`에 CPU 레지스터·MSR·interrupt controller·clock·장치 상태를 담는다.
현재 측정 구성의 상태 파일은 약 **13.7KB**다. 버전·길이·hash를 확인한 뒤 목적지가 읽는다.
RAM은 복사하거나 파일로 dump하지 않는다. FD, eventfd, host thread, `mm` 등 host 연결은 각 Linux 소유다.

동일한 HVA 매핑은 `0x400000000000`으로 고정할 수 있다. guest VA/GPA를 바꾸지 않아
게스트 page table을 고칠 필요가 없지만, **서로 다른 KVM의 EPT를 그대로 공유하는 기능은 아니다.**
`reuse`는 목적지 worker의 기존 mapping/EPT를 유지하고 RAM이나 EPT의 이동을 하지 않는다.
복원에 필요한 translation flush는 수행한다.

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

**DSA와 background preflush는 아직 구현하거나 측정하지 않았다.** EMR용 후보는 DSA Gen1의
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
전체 실행 비용 감소를 구분하여 CPU/DSA 각각 전환 시 처리와 주기적 처리를 비교할 계획이다.
측정 지표는 전환 p50/p99, guest 실행 성능 저하, background CPU·queue 비용과 재쓰기량이다.

## 코드 위치와 범위

- `lab.py`: 이미지 준비, 두 VM 관리, 환경 설정, 검증·측정. 운영 진입점은 이 파일 하나다.
- `build.sh`: kernel/QEMU/Firecracker 빌드 및 관련 테스트의 단일 진입점.
- `shared_probe.c`: 기존 `shared-probe` 안에 공유 메모리 protocol, 전환 controller, 계측을 유지한다.
- `cache_probe.h`: `shared_probe.c`에 포함되는 cache/PML 계측 코드이며 실행 파일은 추가하지 않는다.
- `guest-init.sh`, `guest-cxl.sh`, `l2-init.sh`: initramfs 안에서 실행되는 부팅·DAX 설정 코드다.
- `firecracker/src/vmm/src/persist.rs`, `vstate/memory.rs`: DAX RAM 유지 snapshot과 restore.
- `firecracker/src/vmm/src/reuse.rs`: 이미 사용한 worker의 호환성 검사·준비·의미적 복원.
- `firecracker/src/vmm/src/lib.rs`, `vstate/vcpu.rs`: API/VMM/vCPU 연동 및 direct 실행 제어.
- `implementation/linux/virt/kvm/`, `arch/x86/kvm/`: direct 경로의 DAX allocator·detach/attach·EPT 처리.

기본 검증은 diskless 1 vCPU, 동일 구성·동일 DAX allocation이다. block/net/vsock/PCI 장치를
포함한 worker 재사용은 구현하지 않았다. 다중 vCPU, 다른 물리 호스트의 TSC/캐시 일관성,
임의 이미지 간 pool 재할당도 추가 검증이 필요하다. 이 실험을 일반 CPU context switch와
동일한 보장이나 비용을 가진 구현으로 보지 않는다.

정리 전 README·분리된 빌드/추적 스크립트·중간 패치는
`.local/cleanup-latest.json`이 가리키는 `before-cleanup.tar.gz`에 보존했다.
기존 `.local` 실행 결과와 원시 로그도 유지한다. 현재 운영 설명은 이 README 하나로 관리한다.
