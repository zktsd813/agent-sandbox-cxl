# CXL microVM의 읽기 데이터 페이지 pooling

2026-10-30. 이 문서는 **virtio-blk 읽기를 관찰하고, VM을 잠시 정지한 상태에서 같은 내용의 guest RAM 페이지를 하나의 CXL 데이터 페이지에 연결하는 첫 구현**을 설명한다. Guest의 inode, folio, XArray, LRU와 참조 횟수는 각 VM 안에 유지한다. 공유 대상은 읽기 전용 image에서 온 데이터 바이트다.

실행 결과와 최종 바이너리 식별은 [pooling 검증 결과](../implementation/virtual_machine/results/pool-20261030/README.md)에 기록한다. 아래의 sandbox 수와 파일 크기는 검증 구성·성공 조건이며, 실행 결과를 대신하는 수치가 아니다. 기존 이전 구조는 [microVM 개요](cxl-microvm-overview.md), 운영 명령은 [구현 README](../implementation/virtual_machine/README.md)를 함께 참고한다.

## 무엇을 구현했는가

| 구성 | 이번 구현 |
|---|---|
| 발견 | 성공한 virtio-blk 읽기의 image digest, image offset, guest physical address(GPA)를 VMM이 기록 |
| 대략적인 검색 | 모든 VM이 RAM tier와 관계없이 고정된 CXL Bloom slot 사용 |
| 정확한 후보 확인 | Controller가 각 owner VMM에서 받은 private 관측 index를 조회 |
| 최종 안전성 확인 | 목적 VM을 정지하고 장치 I/O를 drain한 뒤 현재 GPA의 4KiB 전체를 canonical 바이트와 비교 |
| 데이터 연결 | 같은 GPA의 host virtual address(HVA)를 canonical CXL frame의 `MAP_PRIVATE` 매핑으로 교체 |
| 쓰기 격리 | 수정한 devDAX 경로가 CPU 및 host HVA 쓰기를 private DRAM 페이지로 CoW |
| 이전 | 논리적 guest RAM 전체를 snapshot 파일로 구체화한 뒤 DRAM/CXL에 복원 |

Guest kernel에 pooling 기능이나 filesystem lookup hook을 추가하지 않는다. Guest probe는 파일 읽기·쓰기·cache residency 측정만 수행하며, pooling controller에 inode나 folio 포인터를 제공하지 않는다.

현재는 **정지 시점의 admission**이다. 매 virtio-blk 요청에서 Bloom을 조회하여 storage read를 건너뛰는 온라인 경로는 구현하지 않았다. 일반 읽기는 먼저 image에서 완료되고, 관측한 후보를 다음 admission에서 공유한다. 공유를 설정한 뒤의 guest cache hit은 해당 VM의 기존 page-cache 경로와 GPA를 그대로 사용한다.

## 파일과 NFS 경로

Guest는 계속 ext4와 virtio-blk를 사용한다. NFS를 mount하는 주체는 원격 노드의 host Linux다.

```text
A/DRAM 또는 A/CXL
  guest ext4 → virtio-blk → Firecracker → A의 local sandbox.raw

B/CXL
  guest ext4 → virtio-blk → Firecracker → host NFS → A의 같은 sandbox.raw

이미 공유가 설정된 guest cache hit
  guest의 private cache metadata → GPA → EPT → canonical CXL 데이터 페이지
```

RAM tier와 storage backend는 독립적이다. A에서 CXL RAM을 사용해도 image는 local로 읽는다. B에서 발생한 guest cache miss는 host NFS를 통해 A의 image를 읽는다. Guest cache를 유지한 이전 뒤의 hot read는 virtio-blk 요청 없이 완료되어야 한다.

이번 fixture는 read-only ext4 image와 VM별 tmpfs 파일·guest `MAP_PRIVATE` 쓰기를 사용한다. 기존 writable ext4 fixture 전체를 공유 대상으로 확장한 것은 아니다. Mutable image의 같은 offset은 내용이 바뀔 수 있으므로 현재 pooling key로 안전하게 공유할 수 없다. Image 갱신은 새 digest/version을 가진 별도 대상으로 취급해야 한다.

Image ID는 image 전체의 SHA-256이다. Host pathname, inode 또는 Firecracker의 기존 virtio block device ID는 local/NFS 경로 사이에서 동일성을 보장하지 않으므로 사용하지 않는다. 준비 단계에서 `debugfs`로 payload 파일의 block 범위를 구하고, 실험의 admission 대상은 그 범위로 제한한다. Canonical 영역에는 image 전체를 복사하지만 filesystem metadata block은 이 실험의 공유 대상에서 제외한다.

## 관측 정보와 Bloom filter

VMM은 완전히 성공한 읽기의 completion에서 다음 후보를 남긴다.

```text
(image SHA-256, 4KiB image offset) → (VM ID, GPA, 관측 sequence)
```

이는 **I/O buffer의 과거 내용에 관한 hint**다. Guest는 그 GPA를 해제하거나 다른 용도로 재사용할 수 있다. Sequence도 guest page의 실제 재사용 generation은 아니다. 따라서 index에 key가 있다는 이유만으로 현재 page-cache residency나 데이터의 유효성을 인정하지 않는다.

각 VM의 Bloom은 CXL의 고정된 64KiB slot에 둔다. VM이 DRAM에 있을 때도 이 slot을 사용한다. Slot 번호는 VM ID에 대응하고 promotion/demotion 때 바뀌지 않는다. Slot별 writer는 controller가 지정한 현재 owner 하나이며, filter에는 포인터나 공유 refcount를 넣지 않는다.

현재 경로는 다음과 같다.

1. Controller가 owner VMM의 관측 파일을 읽고 VM ID와 run epoch를 확인한다.
2. 다른 VM의 Bloom slot을 읽어 같은 image offset이 있을 가능성을 검사한다.
3. Bloom hit에 대해 controller의 private owner index를 조회한다. 다른 guest kernel의 metadata를 직접 읽지 않는다.
4. CXL tier의 목적 VM에는 admission plan을 전달한다. DRAM tier는 후보와 Bloom만 갱신하고 canonical CXL data mapping을 설치하지 않는다.
5. VMM이 정지·drain 상태에서 자기 GPA의 현재 바이트를 canonical 페이지와 전수 비교한 뒤 매핑한다.

현재 exact index는 controller가 보관한 owner 관측 사본이다. 분산 RPC로 donor guest의 live page를 pin하고 내보내는 protocol은 구현하지 않았다. Canonical 페이지는 시작 때 image로부터 미리 채우고 실험 동안 변경·재사용하지 않는다. 따라서 owner 사본이 오래되어도 최종 데이터 판단은 ready canonical 바이트와 목적 GPA의 비교에 달려 있다.

Controller는 owner 관측 index와 별도로, 이미 admission된 canonical key를 담는 private READY catalog를 유지한다. 다른 owner의 key index는 그 owner의 Bloom hit이 있을 때만 조회한다. READY catalog에 아직 없는 key는 최초 admission 후보가 되고, 이미 등록된 key인데 Bloom이 놓친 경우에는 private 페이지를 유지한다. 이 catalog도 공유 CXL metadata가 아니며, 다른 VM의 exact index를 먼저 조회하여 Bloom을 우회하지 않는다.

Bloom이 실제 존재하는 후보를 놓치면 false negative다. 표준 Bloom 알고리즘의 정상 삽입·조회에서는 발생하지 않지만, 오래된 bit summary나 비일관 메모리의 가시성 지연으로 시스템 수준에서 생길 수 있다. 이 경우 공유 기회를 놓치고 private 복사본을 유지한다. False positive는 private index에서 추가 조회한 뒤 miss가 된다. 어떤 경우에도 Bloom만으로 frame의 주소·소유권·수명을 승인하지 않는다.

`pool_bloom_tool.c`는 고정된 slot 크기와 hash 수를 사용하고 refresh 시 cache line을 flush한다. 이는 현재 coherent QEMU 실험의 구현이며, 물리적인 비일관 CXL에서 필요한 플랫폼별 publish/acquire 보장을 검증한 결과는 아니다.

## 데이터 매핑과 CoW

기존 devDAX는 private mapping을 거부한다. [devdax-pool-cow.patch](../implementation/virtual_machine/devdax-pool-cow.patch)는 4KiB private mapping을 허용하는 실험 기능을 추가한다. **기본값은 비활성화**이고 L1 boot parameter `device_dax.pool_cow=1`로 명시적으로 켠다. Huge private fault는 사용하지 않는다.

Read fault는 canonical CXL PFN을 매핑한다. 첫 접근부터 write인 경우에는 kernel이 준비한 `cow_page`에 원본을 복사한다. 이미 읽은 private PTE에 대한 쓰기는 일반 kernel CoW 경로를 거친다. KVM도 host PTE의 쓰기 가능 여부를 사용하므로 guest CPU 쓰기에 동일한 격리가 적용된다. Host가 `pread` 등의 destination으로 guest HVA에 쓰는 경우도 이 경로를 따른다. 별도의 4KiB KVM memory slot을 대량 생성하지 않는다.

CoW로 새로 할당되는 페이지는 **host DRAM**이다. 따라서 CXL tier도 쓰기 후에는 CXL/DRAM 혼합 상태가 될 수 있다. 모든 private 쓰기를 CXL allocator로 돌리는 기능은 남아 있다.

Firecracker의 `PoolCache` action은 다음을 확인한다.

- VM이 paused 상태이고 모든 block I/O가 drain되었는지 확인한다. VMM event loop가 action을 처리하는 동안 host 장치 event가 동시에 RAM을 변경하지 않는다.
- VM ID/run epoch, image ID, GPA 정렬·범위·중복을 검증한다.
- Canonical allocation의 범위, devDAX 장치 번호, 4KiB alignment를 확인하고 자기 mutable RAM reservation과의 중첩을 거부한다.
- 현재 hint의 key와 4KiB 전체 바이트를 확인한다. 재사용된 GPA는 mismatch로 건너뛴다.
- `MAP_FIXED` 실패로 주소 공간이 손상되었을 가능성이 있으면 그 프로세스의 resume·snapshot·추가 admission을 차단한다.

Balloon, virtio-mem, pmem 장치는 이 prototype에서 거부한다. 해당 장치의 discard·hotplug 동작을 새 backing 구조와 통합하지 않았기 때문이다. Canonical frame의 불변성과 전체 수명은 신뢰하는 실험 controller의 책임이다. Driver 자체가 모든 privileged host writer를 막는 보안 경계는 아니다.

이 실험의 Firecracker는 전용 L1 안에서 `--no-seccomp`로 실행한다. Runtime admission이 여는 plan/report/sysfs와 새 mapping 작업을 기존 production seccomp 정책에 통합한 것은 아니다. 실험에서의 데이터 격리 검증과 production sandbox 보안 설정의 검증을 구분한다.

낮은 계층의 검증은 [CoW 결과](../implementation/virtual_machine/results/pool-cow-20261030/summary.json)에 보관한다. 여기서는 실제 PFN 공유, host CPU·최초 write fault·`pread`·fork 격리, KVM의 read 후 PFN 유지와 write 후 PFN 분리, 같은 HVA의 반복 remap에 따른 EPT 무효화를 확인한다. 이 작은 probe만으로 큰 sandbox의 cache 공유를 입증하지는 않는다.

## Promotion, handoff, demotion

현재 실험의 CXL 주소 배치는 다음과 같다. 각 구간의 겹침은 controller와 VMM이 검증한다.

| CXL offset | 용도 |
|---|---|
| 처음 2MiB | 기존 실험 control/mailbox |
| 2MiB부터, 2GiB 미만 | VM별 mutable RAM reservation |
| 2GiB부터 | 변경하지 않는 canonical image |
| 3GiB부터 | VM별 64KiB Bloom slot |

여러 VM을 한 번에 같은 tier로 옮겨야 하는 제약은 없다. 검증은 일부 VM이 A/CXL에 남아 있는 동안 다른 VM을 B/CXL로 옮기고, 한 VM을 DRAM으로 내리는 동안 나머지 VM의 데이터를 다시 읽는다.

각 이전은 source pause → 장치 drain → **논리적 RAM 전체 snapshot** → source 종료와 PID incarnation fence → destination restore → 후보 재검증과 재매핑 → resume 순서다. Hint 파일은 VM과 함께 전달하지만 기존 관측을 정답으로 신뢰하지 않는다. Destination은 새로운 run epoch를 사용한다.

State와 RAM snapshot은 먼저 source host의 local tmpfs에 완성한다. Source가 정지한 상태에서 별도 `cp`로 local/NFS transfer 경로에 옮기고, state·RAM·hint 파일 각각을 `cmp`로 비교한 뒤 source를 종료한다. 이 전송 단계에는 600초 timeout을 적용한다. Destination의 owner hint도 Firecracker 시작 전에 local 파일로 복사·비교하고, VMM의 JSON reader/writer는 buffering을 사용한다. 작은 제어 API의 timeout과 큰 NFS 파일 전송을 분리하기 위한 구성이다.

기존의 연속된 DAX 영역만 복사하면 canonical overlay와 DRAM CoW 페이지를 빠뜨린다. 이번 구현은 VMM이 현재 보고 있는 HVA의 실제 바이트를 일반 snapshot 파일에 모두 저장한다. CXL 복원에서는 이를 VM별 DAX reservation으로 import하고 공유를 다시 설정한다. DRAM 복원에서는 private file mapping을 사용하며 shared data overlay를 설치하지 않는다. Bloom mapping은 별도의 host helper가 계속 CXL에서 사용한다.

Demotion 후에는 이전 VM의 mutable CXL reservation을 poison하고 guest 및 다른 VM을 다시 검사한다. Canonical 영역을 poison하지 않는다. 이미 다른 VM들이 사용하는 canonical frame은 최초 donor의 tier 변경과 무관하게 유지되어야 한다.

이 선택 때문에 **이번 pooling 실험의 CXL→CXL handoff도 전체 RAM을 복사한다.** 기존 shared-DAX state-only handoff의 비용이나 direct handoff 비용으로 해석하면 안 된다. Overlay가 설치된 VM의 state-only DAX snapshot은 명시적으로 거절한다. Scattered backing manifest와 private CoW 소유권을 전달하여 RAM 복사 없는 handoff를 복원하는 작업은 후속 단계다. 기존 direct/reuse의 diskless 제약도 그대로다.

## 검증 구성과 증거

기본 구성은 3개 sandbox, 각 512MiB guest RAM, 160MiB의 immutable 파일이다. 성공 기준은 각 sandbox의 guest file cache와 실제 공유 PFN이 각각 최소 128MiB이며, 세 VM이 동시에 같은 canonical offset 집합을 최소 128MiB 공유하는 것이다. 파일 크기만으로 cache 크기를 추정하지 않는다.

| 검사 | 확인하는 것 |
|---|---|
| Guest의 fresh `MAP_SHARED` + `mincore` | Private CoW 페이지와 구분한 실제 파일 cache residency |
| Guest의 전체 word oracle | 원본 파일의 모든 데이터, VM별 private mapping과 tmpfs 파일의 내용 |
| `/proc/meminfo`, virtio block counter | Cache 규모의 보조 근거와 hot read의 block I/O 0 |
| Guest `cachemap`의 file offset→GPA 기록 | 현재 resident file-cache 페이지와 과거 I/O buffer hint를 구분 |
| 정지 중 guest GPA→VMM HVA→host PFN 대조 | 현재 파일 페이지가 실제 canonical CXL frame을 가리키는지 확인 |
| 세 VM의 공유 offset 교집합 | 각 VM이 서로 다른 영역만 공유한다고 잘못 집계하지 않도록 확인 |
| Guest eviction → anonymous churn | 과거 hint의 GPA 재사용, host CoW와 stale content 거절 |
| B에서 cold refill | Virtio-blk 요청과 실제 host NFS READ 증가 |
| 반복 promotion/demotion와 peer 재검사 | 한 VM의 이전·poison이 다른 VM을 손상시키는지 확인 |
| 잘못된 owner/epoch/image/범위, Bloom bit 조작 | 잘못된 후보의 거절과 approximate discovery의 한계 |

`mincore`가 160MiB이고 과거 hint GPA에서 160MiB의 공유 PFN을 찾았다는 두 관측만으로는 **그 공유 PFN이 현재 guest file cache인지** 확정할 수 없다. 해제·재사용된 과거 I/O buffer가 PFN 집계에 포함될 수 있기 때문이다. 초기 측정 방식은 데이터 frame 공유와 guest cache residency를 각각 확인한 것으로 구분한다.

보강한 검증은 guest의 `cachemap` 명령으로 payload를 새 read-only `MAP_SHARED`로 매핑하고 `mlock`한다. Guest pagemap에서 present PFN을 읽어 `file offset → GPA`를 기록하며, 연속된 GPA는 손실 없는 range 형식으로 저장한다. 이 기록을 얻는 동안 새 block read가 없었는지도 확인한다. VM을 pause한 상태에서 준비된 file extent, VMM admission의 GPA/HVA, canonical PFN을 연결하고 **실제로 대응한 현재 파일 페이지만** 공유량에 포함한다. 검사 뒤 resume하고 `cacheunmap`으로 pin과 매핑을 해제한다.

세 VM의 공통 페이지를 측정할 때는 세 VM 모두의 페이지를 pin하고 모두 pause한 뒤, 같은 정지 구간에서 PFN을 검사한다. 검사가 끝날 때까지 어느 VM도 resume하거나 unpin하지 않는다. 이렇게 얻은 실제 파일 offset 집합의 교집합을 공통 cache 공유량으로 계산한다.

이 guest helper는 독립적인 **측정 oracle**이다. 기록한 GPA는 pooling hint index나 admission plan 생성에 입력하지 않는다. 구현은 계속 VMM 관찰 방식이고, guest의 측정 코드를 pooling의 실행 조건으로 요구하지 않는다. Fixture manifest의 `guest_cache_gpa_audit=true`가 이 보강된 증거 형식을 표시한다. 결과 표에서 초기 데이터 frame 검사와 현재 file-cache 대응 검사를 구분하며, 이전 실행에 새 증거가 소급하여 생긴 것으로 해석하지 않는다.

실험은 `Sync` virtio-blk engine을 기본으로 사용하고 `--io-engine Async`도 선택할 수 있다. Engine·vCPU 수·측정 방법·완료 여부는 각 실행 결과에 따로 기록한다. Async engine의 drain 경로를 코드에 반영한 것과 해당 workload의 실제 검증 완료는 구분한다. 저장된 receipt는 SHA-256과 함께 남기고 `verify_pool_results.py`로 다시 검사한다.

## 재현

아래는 기존 home-storage/NFS assets와 준비된 CXL kernel·QEMU·Firecracker source를 사용하는 실험 절차다. Source bootstrap은 기존 구현 README를 따른다. 기존 작업 source와 kernel build를 직접 덮어쓰지 말고 별도 복사본을 사용한다.

먼저 prepared Linux source의 복사본에 `devdax-pool-cow.patch`를 적용한다. 기존 CXL static-capacity/KVM 실험 patch가 포함된 같은 kernel 계열을 사용한다. Kernel config에는 client뿐 아니라 **NFS server도** 필요하다.

```bash
# 아래 변수는 준비한 복사본과 기존 NFS assets의 절대 경로로 설정한다.
pool_lab="$PWD/implementation/virtual_machine"
pool_kernel_prepared=/home/ijkim/agent-sandbox-cxl/implementation/linux
pool_kernel_source=/tmp/pool-linux-repro
pool_kernel_build=/tmp/pool-kernel-build-repro
pool_base_assets="$pool_lab/.local/assets-home-storage-api-v4-20261028"
pool_fc=/tmp/firecracker-home-storage-20261028/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker
pool_assets="$pool_lab/.local/assets-pool-repro"
pool_tools=/tmp/pool-tools-repro

# 기존 prepared source와 분리한 새 복사본을 만든다.
test ! -e "$pool_kernel_source"
test ! -e "$pool_kernel_build"
cp -a --reflink=auto "$pool_kernel_prepared" "$pool_kernel_source"
mkdir -p "$pool_kernel_build"
cp "$pool_lab/kernel.config" "$pool_kernel_build/.config"
patch --dry-run -p1 -d "$pool_kernel_source" -i "$pool_lab/devdax-pool-cow.patch"
patch -p1 -d "$pool_kernel_source" -i "$pool_lab/devdax-pool-cow.patch"
"$pool_kernel_source/scripts/config" --file "$pool_kernel_build/.config" \
  --enable NFSD --disable NFSD_V2 --disable NFSD_V3_ACL --disable NFSD_V4 \
  --enable NFS_FS --enable NFS_V3 --enable VIRTIO_BLK --enable EXT4_FS
make -C "$pool_kernel_source" O="$pool_kernel_build" LOCALVERSION= olddefconfig
make -C "$pool_kernel_source" O="$pool_kernel_build" LOCALVERSION= -j24 bzImage modules
rg '^CONFIG_(NFSD|NFS_FS|NFS_V3)=y$' "$pool_kernel_build/.config"
```

Firecracker에는 현재 저장소의 `firecracker.patch`를 동일한 upstream baseline에 적용하여 빌드한다. 이 patch는 기존 DAX/migration 변경과 pooling 변경을 포함한다. 이미 patch된 작업 source에 중복 적용하지 않는다. 검증에는 build output 자체의 SHA-256을 사용한다.

```bash
mkdir -p "$pool_tools"
for tool in pool_bloom_tool pool_pfn_audit pool_cow_probe; do
  gcc -std=c11 -O2 -static -Wall -Wextra -Werror \
    "$pool_lab/$tool.c" -o "$pool_tools/${tool//_/-}"
done
python3 "$pool_lab/pool_prepare.py" \
  --base-assets "$pool_base_assets" --assets "$pool_assets" \
  --firecracker "$pool_fc" \
  --l1-kernel "$pool_kernel_build/arch/x86/boot/bzImage" \
  --kernel-modules "$pool_kernel_build/arch/x86/kvm" \
  --extra-tool "$pool_tools/pool-bloom-tool" \
  --extra-tool "$pool_tools/pool-pfn-audit" \
  --extra-tool "$pool_tools/pool-cow-probe" \
  --payload-mib 160 --storage-mib 4096

# 기존 README에서 준비한 patched QEMU와 firmware root의 절대 경로.
pool_qemu_root=/home/ijkim/agent-sandbox-cxl/implementation/virtual_machine/.local/qemu/root
pool_qemu=/home/ijkim/agent-sandbox-cxl/implementation/virtual_machine/.local/qemu-cxl/build/qemu-system-x86_64
python3 "$pool_lab/pool_run.py" \
  --assets "$pool_assets" --qemu "$pool_qemu" --qemu-root "$pool_qemu_root" \
  --session pool-repro --cycles 2 --vcpus 1 --io-engine Sync --host-cpus 0-5
# 위 명령이 출력한 POOL_REPORT의 파일 경로를 넣는다.
python3 "$pool_lab/verify_pool_results.py" /absolute/path/to/report.json --negative-tests
```

`pool_run.py`는 각 L1을 4 vCPU·8GiB RAM으로 시작하고, 4GiB CXL·64MiB ivshmem scratch·`/dev/shm` backing·격리된 TAP network를 사용한다. Sandbox는 각각 512MiB다. Host CPU 0–5는 재현 예시이므로 실행 전 사용 가능한 범위를 확인한다. Wrapper는 bench 성공·실패 뒤 자신이 시작한 pair를 정리한다.

이 검증은 전용 L1 pair를 점유하고 storage claim을 남긴다. 같은 pair에서 bench를 다시 실행하는 대신 wrapper로 새 pair를 시작한다. 실패 로그와 보고서는 보존한다. Native `pool-cow-probe`는 지정한 CXL 구간 64KiB를 실제로 덮어쓰므로 실행 중인 VM/pool/Bloom 영역과 겹치지 않는 소유한 구간에서만 실행한다. 기본 benchmark 구간을 임의로 probe에 전달하지 않는다.

## 현재 한계와 다음 단계

이번 구현은 여러 VM이 같은 CXL frame을 실제로 읽고, 쓰기 시 격리되는지 검증하기 위한 것이다. VM별 원래 DAX reservation과 canonical allocation을 미리 확보하고, admission으로 불필요해진 원래 frame을 allocator에 반환하지 않는다. 따라서 **PFN 공유 확인과 실제 사용 가능 메모리 용량 증가는 같은 결과가 아니다.** 메모리 절감률을 주장하려면 기존 reservation의 회수·재할당까지 구현해야 한다.

공유 key는 같은 image digest와 offset이다. 서로 다른 image나 다른 offset에 우연히 같은 내용이 있는 페이지를 찾는 일반적인 content deduplication은 하지 않는다. Host의 local/NFS page cache도 제거하지 않았으므로, guest cache의 공유량을 전체 시스템의 순 메모리 절감량으로 계산하지 않는다.

다음 구현 과제는 admission의 온라인화, image 사전 적재 대신 관측한 donor 데이터의 보호·publish, frame lifetime과 회수, private CoW의 CXL 배치, scattered backing을 보존하는 state-only handoff다. 물리적인 비일관 CXL에서는 publish 이후 가시성, reader invalidate, owner fence와 frame 재사용을 별도로 검증해야 한다. 현재의 approximate Bloom만으로 이러한 정확성 요구를 없앨 수는 없다.

PML과 MPTCP는 이 prototype의 실행 조건에 포함하지 않는다. PML은 변경된 private RAM의 이전 처리를 줄이는 후속 통합 대상이고, MPTCP는 실행 노드가 바뀌는 guest 연결을 유지하는 별도 경로다. Host NFS가 동작한다고 guest application connection의 이전까지 해결되는 것은 아니다.
