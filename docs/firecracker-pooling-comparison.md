# 일반 Firecracker와 현재 CXL pooling 구현의 차이

비교 기준은 **수정하지 않은 Firecracker v1.17.0, commit `95f868c8e345b1cc8faccd1a3c910b4989dc3f58`**다. 현재 수정본의 upstream baseline과 같으며, 같은 toolchain·release·musl target으로 별도 빌드한다. 서로 다른 버전의 성능을 비교하지 않는다.

여기서 “순수 Firecracker”는 VMM source와 binary가 upstream 그대로라는 뜻이다. Host/L1 kernel·QEMU·guest fixture는 비교군 모두 같은 실험 구성을 사용한다. Host kernel 전체까지 별도의 stock 배포판으로 바꾼 비교가 아니다. 두 VMM 모두 같은 `--no-seccomp` 설정을 사용하므로 production 보안 설정의 성능·격리 비교로 확대하지 않는다.

현재까지의 pooling 검증은 [2026-10-30 lifecycle 결과](../implementation/virtual_machine/results/pool-20261030/README.md)에 있다. 여기서 입증한 것은 여러 VM의 실제 데이터 frame 공유, 쓰기 격리, 전체 RAM snapshot을 통한 이전 정합성이다. 사용 가능한 CXL 용량 증가, 기존 direct handoff와의 통합, 실제 비일관 다중 host 동작까지 완료했다는 의미는 아니다.

수정하지 않은 VMM과의 [실측 비교 결과](../implementation/virtual_machine/results/stock-comparison-20261030/README.md)도 별도로 검증했다. 독립 부팅한 3개 VM의 160MiB payload는 stock에서 총 480MiB의 서로 다른 frame을 사용했고, pooling에서는 같은 160MiB frame을 사용했다. **Stock의 동일 snapshot clone도 160MiB를 공유했다.** Pooling의 회수·224MiB 재사용 workload는 중앙값 4.08초로, hint만 사용하는 CXL 조건의 0.27초보다 크게 느렸다. 현재 결과를 용량 절감이나 성능 개선이 완성된 상태로 해석하면 안 된다.

## 일반 Firecracker가 이미 제공하는 기능

Firecracker는 guest RAM과 장치·CPU 상태를 snapshot으로 저장하고 다른 VMM process에서 복원한다. Guest cache도 RAM의 일부이므로 이 경로로 보존될 수 있다. Disk image는 운영자가 별도로 유지해야 한다. Host에 mount한 NFS 경로의 image를 여는 것도 이러한 일반 file backend 위에서 가능하다. NFS 지원 자체를 pooling의 새 기능으로 세지 않는다.

**일반 Firecracker도 메모리 페이지를 공유할 수 있다.** Fresh boot는 private anonymous RAM을 사용하지만, snapshot restore는 RAM 파일을 `MAP_PRIVATE`로 매핑한다. 같은 host에서 여러 VM이 같은 RAM snapshot inode를 사용하면 수정하지 않은 페이지를 공유하고 쓰기 시 분리할 수 있다. 공식 [snapshot 문서](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/snapshot-support.md#overview)와 [memory 구현](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/vstate/memory.rs)에 명시된 동작이다.

따라서 비교할 대상은 다음 두 경우를 구분해야 한다.

- **독립적으로 부팅한 VM:** 같은 image를 읽어도 각 guest의 cache RAM은 별개로 만들어진다. Host의 image page cache 공유와 guest RAM 공유는 다르다.
- **같은 warm snapshot의 clone:** 같은 RAM 파일을 출발점으로 사용하므로 일반 Firecracker도 clean guest 페이지를 공유할 수 있다. Clone의 초기 guest boot ID·PID·cookie가 같은 것은 이 출발 상태를 복제한 결과다.

현재 pooling은 첫 번째 경우의 관측된 image 데이터 페이지를 나중에 canonical CXL frame으로 연결한다. 같은 image digest와 block offset을 사용하므로 서로 다른 guest GPA에 있는 데이터도 후보가 된다. 이것은 서로 다른 image의 임의의 동일 content를 찾는 일반 deduplication은 아니다.

## 기능과 검증 범위

| 항목 | 일반 Firecracker | 현재 수정본 |
|---|---|---|
| Guest ext4·virtio-blk와 host file/NFS image | 기존 file backend 사용 | 같은 구조 사용 |
| Guest cache를 포함한 full RAM snapshot/restore | 지원 | Canonical alias와 private CoW 내용도 구체화하여 사용 |
| 같은 warm RAM snapshot clone의 clean page 공유 | `MAP_PRIVATE` restore로 가능 | 이 기존 기능을 배제하지 않음 |
| 독립 VM이 읽은 같은 image block의 guest RAM 연결 | 이 관측·catalog·admission 경로는 없음 | 성공한 read hint, Bloom, private exact index, paused byte 비교와 remap |
| Guest inode·folio·XArray·LRU | 각 guest의 상태 | 각 guest의 상태 유지 |
| 명시적인 공유 devDAX pool과 고정 Bloom slot | 이 프로젝트의 API·배치·수명 관리 없음 | DRAM/CXL VM 모두 Bloom을 유지하며 CXL admission에서 데이터 연결 |
| 공유 후 guest/host 쓰기 격리 | Snapshot file에는 일반 CoW | 실험 devDAX private mapping에 CoW 추가; 새 페이지는 host DRAM |
| 독립 VM의 반복 DRAM↔CXL 및 노드 이전 | 이 프로젝트의 controller·devDAX 경로는 없음 | Full snapshot으로 30회 전환 검증 |
| 원래 DAX frame 회수와 순 용량 절감 | 이 prototype과 별개의 메모리 관리 | 아직 구현하지 않음 |
| Pooling 상태의 zero-copy/state-only handoff | 해당 pooling 상태 없음 | 아직 미지원; overlay가 있으면 state-only DAX snapshot 거절 |
| 비일관 물리 CXL에서 publish·invalidate·frame 재사용 | 이 비교의 검증 범위 밖 | 미검증 |

“일반 Firecracker는 CXL을 사용할 수 없다”는 의미도 아니다. CXL이 host의 일반 system RAM/NUMA memory로 노출되면 anonymous RAM의 배치는 host 정책의 영향을 받는다. 여기서 추가한 것은 특정 devDAX allocation의 공유 frame, canonical catalog, Bloom slot과 이전 중 backing 관리다. 실제 CXL NUMA 성능과 이 QEMU devDAX 실험을 혼동하지 않는다.

Upstream도 snapshot 준비 시 block I/O를 drain하고 image를 flush한다. 현재 patch는 해당 오류를 로그만 남기고 넘어가는 경로를 변경하여 checkpoint 생성을 실패시키는 검사를 추가했다. “일반 Firecracker에는 flush가 없다”는 비교는 하지 않는다.

## 비교 실험을 읽는 기준

같은 image·guest kernel·512MiB RAM·2 vCPU·Async block engine에서 세 VM을 사용한다. Guest cache는 파일 크기로 추정하지 않고 실제 residency와 file offset→GPA→host PFN으로 확인한다. 성능 측정은 guest 내부 시계를 사용하고 serial 명령 전달·응답 대기는 제외한다. 읽기 안에 전체 word 검사가 포함되므로 결과는 **읽기와 데이터 검사를 합친 workload 시간**이며 순 메모리 bandwidth나 XArray lookup latency가 아니다.

비교군은 upstream DRAM, 수정본 DRAM에서 hint 비활성, 수정본 CXL에서 hint만 활성, 수정본 CXL에서 pooling 활성이다. 앞의 두 경우로 수정본의 비활성 경로를 비교하고, 뒤의 두 경우로 같은 hint 관측을 유지한 상태의 공유 효과를 비교한다. 같은 warm snapshot을 사용한 upstream clone 공유는 별도의 기준으로 확인한다.

Cold refill은 guest file cache를 비운 뒤 수행하는 읽기다. Host image cache가 warm인 조건을 물리 storage cold read라고 부르지 않는다. Eviction과 anonymous churn의 시간에는 allocator·전체 데이터 쓰기와 검사도 포함되며, CoW 한 번의 비용으로 해석하지 않는다. 초기부터 변경한 guest `MAP_PRIVATE` 4MiB의 재쓰기도 canonical frame의 첫 host CoW 비용과 다르다.

네 성능 비교군은 모두 image의 home인 A에서 실행한다. CXL RAM을 사용해도 CPU가 A에 있으면 image는 local file이다. NFS는 B에서 실행할 때 host가 image를 여는 경로이며, 메모리가 CXL인지 DRAM인지 자체로 결정되지 않는다. 별도 stock snapshot 실험은 A/DRAM에서 B/DRAM으로 복원하면서 NFS를 사용했고, 기존 pooling lifecycle 실험은 B/CXL에서 NFS를 사용했다. 네 군의 cold timing을 NFS 성능으로 읽으면 안 된다.

RSS/PSS만으로 CXL 용량을 계산하지 않는다. DevDAX mapping은 일반 anonymous/file-backed RAM과 accounting이 다를 수 있다. 현재 file-cache의 고유 PFN 수, 예약한 DAX allocation, canonical image 크기, host의 file cache를 구분한다. 이 비교의 PFN은 Firecracker를 실행하는 L1 host에서 측정한다. QEMU를 실행하는 물리 host 전체의 순 메모리 사용량 계측과 같지 않다. 실제 data-frame 중복이 줄었어도 원래 DAX frame을 회수하지 않으면 그만큼의 용량을 다른 VM에 재할당했다고 주장할 수 없다.

## 완료된 것과 남은 것

이미 완료한 lifecycle 검증은 3개 sandbox 각각 160MiB cache, Sync·Async 총 30회 전환과 321개 guest 명령이다. 보강된 Async 측정 17회 모두 현재 guest file-cache 40,960페이지가 canonical PFN과 대응했고, 세 VM을 같은 구간에 정지한 세 번의 검사에서도 공통 160MiB를 확인했다. Eviction 이후 재사용·CoW, stale hint 거절, NFS refill, demotion 뒤 이전 mutable CXL RAM poison 검사도 통과했다.

Guest 명령을 완료한 뒤 다음 명령 사이에서 VM을 pause하여 admission과 이전을 수행했다. 세 VM의 상태와 backing이 함께 존재한 것은 검증했지만, 세 VM이 동시에 파일을 계속 읽고 쓰는 처리량 실험이나 명령 실행 도중의 임의 시점 이전 stress는 아니다. Async engine·2 vCPU를 사용했다는 사실만으로 이러한 동시성 범위를 검증했다고 확대하지 않는다. RO payload와 VM별 tmpfs/private mapping을 사용했으므로 shared writable filesystem의 coherence도 검증 대상이 아니다.

이는 immutable image·정지 admission·coherent QEMU/nested-KVM 환경의 결과다. Canonical image는 미리 채우고 재사용하지 않으며 owner index는 controller가 가진 private 관측 사본이다. 실제 donor cache를 실시간으로 pin하여 export하는 protocol은 아직 없다. Online admission, 원래 frame 회수, CoW의 CXL 배치, scattered backing을 보존하는 direct handoff와 PML/MPTCP 통합이 후속 단계다.
