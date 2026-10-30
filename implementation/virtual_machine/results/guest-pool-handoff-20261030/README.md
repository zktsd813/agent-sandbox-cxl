# Guest metadata pooling: direct와 state-only handoff

2026-10-30. 현재 guest의 clean lower ext4 page cache를 등록해 promotion 때 중복 backing을 공유하고, private CoW도 CXL에 유지하는 구현이다. Demotion은 현재 논리 RAM을 anonymous private RAM으로 복사하고 grant를 반환한다. 두 handoff에서 full-RAM snapshot 파일을 사용하지 않는다. [구조·코드·신뢰 경계](../../../../docs/cxl-guest-pool-handoff.md)

## 실행 구성과 판정

각 모드는 새 A/B pair에서 실행했다. 독립 boot ID를 갖는 3개 sandbox, 각각 1 vCPU·512MiB RAM·160MiB의 실제 lower file cache를 사용한다. A는 local image를 열고 B host는 NFS를 통해 같은 image를 연다. Guest에는 virtio-blk가 유지된다. `Async` block I/O와 reporting-only balloon을 사용한다.

| 검증 | State-only | Direct |
|---|---|---|
| CXL에서 실행 위치 이전 | 세 VM 각각 A→B→A 두 차례, 총 12회 | A→B→A 두 차례, 총 4회 |
| Promotion / demotion | 각 9회 | 각 6회 |
| RAM 파일 dump/전송 | 사용하지 않음 | 사용하지 않음 |
| CPU/KVM 처리 | CPU·장치 state 복원, 목적지 KVM 새로 생성 | 동일 VM/vCPU/VMCS/EPT-root identity 유지 |
| 실제 3-VM 공통 file-cache PFN | 160MiB | 160MiB |
| 최종 private grant·credit 잔류 | 0 bytes | 0 bytes |

Direct에서는 하나의 retained KVM graph를 옮기며 두 peer VM이 함께 데이터를 공유한다. 세 VM 모두 동시에 direct handoff하는 시험은 아니다. EPT root identity는 유지하지만 실행·CoW·tier 전환 중 leaf mapping과 page count는 바뀔 수 있다.

## 무엇을 확인했는가

- **현재 guest authority:** Kernel이 clean resident folio를 고정하고 게시한 raw 208-byte status, inode/UUID/generation, page index, GPA와 image extent를 독립적으로 대조한다. 등록 중 추가 block read는 0이다. Promotion의 파일 데이터 전수 비교량과 RAM snapshot량은 0이다.
- **공유와 CoW:** 등록된 현재 page-cache GPA를 실제 host PTE와 연결한다. Canonical PFN은 read-only이고, native HVA write와 KVM write 모두 private CXL PFN으로 분리된다. KVM smoke는 기존 read SPTE 이후 write와 처음부터 absent PTE에 write하는 경우를 각각 실행한다. 바뀌지 않은 4,095 bytes와 canonical·peer 전체가 보존된다.
- **폐기 후 재사용:** Guest ACK trace와 host의 실제 mapping 변경을 GPA별로 연결하고, 이후 400MiB anonymous allocation의 실제 GPA/PTE와 교차한다. 두 모드 모두 144MiB가 ACK 뒤 실제 새 데이터에 사용된 것을 확인했다. State-only controller의 이른 polling 결과는 143.984375MiB이며, 뒤에 캡처한 완전한 로그로 allocation 전 완료된 4개 페이지를 추가 확인했다. 단순 ACK bytes나 grant 크기를 재사용량으로 세지 않는다.
- **NFS와 파일 변경:** B에서 cold refill 시 guest block read와 실제 NFS READ 증가를 확인한다. B에서 160MiB 파일의 실제 OverlayFS copy-up·upper 변경과 lower/peer 보존을 검사한다. 이 큰 upper copy는 검사 후 제거한다. Handoff를 넘어 유지하는 쓰기 증거는 별도의 4MiB tmpfs 파일과 4MiB MAP_PRIVATE 데이터다. 400MiB anonymous 재사용 검사는 이전 전에 해제하며, 이를 handoff 동안 살아 있는 working set으로 주장하지 않는다.
- **Demotion과 반환:** Mapper의 자체 보고 외에 `/proc/<pid>/maps` 원문으로 전체 512MiB HVA가 anonymous `rw-p`로 바뀌었는지 확인한다. Mapping 교체·KVM invalidation 완료와 VMA 증거를 확인한 뒤 grant를 반환하고, 재개 후 guest 내용을 다시 검사한다. State-only의 두 cross-VM 재할당은 각각 368MiB grant 중 실제 private mapping/복사 352MiB와 미사용 credit 16MiB를 구분한다. 이 증거는 물리 NUMA node의 DRAM 배치를 측정한 것은 아니다.
- **종료:** 전체 224MiB canonical image를 다시 검증하고 private allocator의 모든 grant·credit 반환, source/wrapper fencing, outer QEMU와 소유 memory backing 정리를 확인한다.

Live 오류 주입은 잘못된 cookie·epoch·등록 digest·canonical offset·이미 닫힌 registration을 거절한다. State-only는 잘못된 allocation lease·placement epoch·state hash로 import를 거절한 뒤 올바른 상태를 복원한다. 잘못된 load 뒤 Firecracker가 종료될 수 있으므로 각 실패마다 새 wrapper를 사용한다. Direct malformed token/device 구성은 Rust unit test로 검증하며, 모든 종류를 live import에 주입한 것은 아니다.

Bloom all-zero는 40,960개의 정상 key에 대한 후보 발견을 막고, all-one은 image 밖의 257개 key까지 hit시킨다. 정확한 canonical 조회가 잘못된 후보를 제외한다. 이는 live Bloom 조회와 controller 선택 규칙 검증이다. All-zero 상태에서 전체 promotion latency를 측정한 결과는 아니다.

## 메모리 비용의 해석

세 VM의 초기 promotion에서 동일한 160MiB payload는 480MiB의 private 복제 대신 하나의 canonical backing을 참조한다. **Payload 중복 제거는 320MiB**다. 총 할당량은 다음과 같이 계산한다.

| 초기 할당 항목 | 크기 |
|---|---:|
| VM별 private CXL mapping, 352MiB × 3 | 1,056MiB |
| 미리 적재한 immutable image 전체 | 224MiB |
| 미사용 CoW credit, 16MiB × 3 | 48MiB |
| 데이터 pool 총 할당량 | 1,328MiB |
| 3 × 512MiB와의 차이 | 208MiB |

208MiB는 초기 데이터 backing 기준이다. 이후 credit growth와 CoW는 별도로 반영해야 한다. KVM control arena·Bloom·controller metadata와 host RSS를 합친 서비스 전체 절감량이나, 이번 버전과 stock Firecracker의 성능 비교가 아니다. 기존 [stock 비교](../stock-comparison-20261030/README.md)는 이전 read-hint 구현의 별도 결과다.

## 독립 검증과 보존 자료

`verify_pool_handoff.py`는 실행 controller와 guest parser를 import하지 않는다. Receipt hash뿐 아니라 guest 등록 digest, 실제 PTE, status, API pause/resume 순서, allocator 소유권, source fence, NFS counter와 direct token을 다시 계산한다. `test_pool_handoff_evidence.py`는 바꾼 원시 receipt의 hash까지 재계산해 semantic 오류를 거절하는지 검사한다. 원본 증거는 수정하지 않는다.

최종 strict audit은 direct의 원시 receipt 792개, state-only의 1,348개를 통과했다. Semantic mutation은 **각 mode 20개, 총 40회** 모두 의도한 이유로 거절됐다. Mode별로 겹치는 case가 있으므로 서로 다른 종류 40개를 검증했다는 뜻은 아니다. 보관 archive를 새 디렉터리에 풀어 같은 frozen verifier로 검사한 결과도 통과했다. 간단한 기계 판독 결과는 [summary.json](summary.json)에 있다.

- `raw/direct.tar.xz`, `raw/state-only.tar.xz`: 최종 raw receipts와 실행 당시 source, report, 외부 pair 정리 증거.
- `verification/`: 독립 감사와 semantic mutation 결과. Direct kernel identity에는 별도의 backend 감사도 있다.
- `build/`: 적용된 patch, config, 정확한 compiler/link command, guest build, source와 binary hash 및 실패했던 개발 build 로그.
- `fixture-manifest.json`: 최종 immutable fixture와 runtime 입력 hash.
- `raw/preparatory.tar.xz`, `preparatory-index.json`: 실패를 포함한 preparatory 실행. 최종 통과 횟수에 합산하지 않는다.
- `artifact-manifest.json`: 배포 파일의 SHA-256과 크기.

최종 runtime Firecracker SHA-256은 `064b48d68e094a5eadeac598f30baadaa37ddd188075feeabfc37b1549f23c60`이다. Cumulative patch는 upstream `95f868c8e345b1cc8faccd1a3c910b4989dc3f58`에 clean apply한 31개 파일이 실제 빌드 source와 일치한다. 최종 Rust 검증은 pooling 15개와 direct 3개, Python protocol/allocator는 14개다. 과거 반복 실행의 같은 test를 중복 합산하지 않는다.

준비 중 발견한 실패도 보존한다. 최초 guest helper의 epoch 0 거절을 수정했고, direct 목적지의 긴 serial launch line은 짧은 script로 업로드하도록 바꿨다. State-only의 첫 오류 주입 시험은 종료된 wrapper에 다음 요청을 보내 실패했으며 새 wrapper 방식으로 재실행했다. 초기 pilot에는 raw status·demotion proof나 최신 KVM module provenance가 빠진 경우가 있어 최종 증거를 대신하지 않는다.

## 보존 archive 재검증

아래는 저장소 root 기준이다. Archive에 기록된 측정 원본을 새 디렉터리에 풀어서 검사한다.

```bash
mkdir -p /tmp/guest-pool-replay
tar -xJf implementation/virtual_machine/results/guest-pool-handoff-20261030/raw/direct.tar.xz -C /tmp/guest-pool-replay
tar -xJf implementation/virtual_machine/results/guest-pool-handoff-20261030/raw/state-only.tar.xz -C /tmp/guest-pool-replay
python3 implementation/virtual_machine/verify_pool_handoff.py /tmp/guest-pool-replay/direct/report.json
python3 implementation/virtual_machine/verify_pool_handoff.py /tmp/guest-pool-replay/state-only/report.json
python3 implementation/virtual_machine/test_pool_handoff_evidence.py /tmp/guest-pool-replay/direct/report.json
python3 implementation/virtual_machine/test_pool_handoff_evidence.py /tmp/guest-pool-replay/state-only/report.json
```

Direct final은 guest trace보다 앞서 캡처한 host log의 짧은 prefix를 보완하기 위해, outer 종료 전 원래 epoch-0의 전체 host log를 추가로 읽었다. 이를 별도 hash receipt로 보존하며, ACK-before-allocation 순서는 원래 guest trace에서 확인한다. State-only 최종 실행은 guest trace 뒤 host log도 캡처한다.

## 새 pair에서 실행

기본 CXL QEMU, NFS fixture와 host kernel 준비는 [기존 준비 절차](../../../../docs/cxl-data-pooling.md), reporting guest의 원본은 [이전 guest build](../pool-discard-20261030/build/guest/README.md)를 따른다. 기존 source/build를 덮어쓰지 않고 새 경로를 사용한다. Host kernel은 이번 cumulative `kvm-dax-state.patch`와 `build/host-kernel.config`를 사용하고, 동일 build의 KVM modules를 넣는다. Module을 물리 host에 설치하지 않고 disposable L1에 로드한다.

```bash
cd implementation/virtual_machine
python3 guest-pool-build.py --base-source "$REPORTING_GUEST_SOURCE" \
  --base-build "$REPORTING_GUEST_BUILD" --source "$NEW_GUEST_SOURCE" \
  --build "$NEW_GUEST_BUILD" --output "$NEW_GUEST_RECORD"

mkdir "$POOL_MODULE_BUILD"
cp pool_memory_module.c pool_memory_uapi.h "$POOL_MODULE_BUILD/"
printf 'obj-m += pool_memory_module.o\n' > "$POOL_MODULE_BUILD/Makefile"
make -C "$NEW_HOST_SOURCE" O="$NEW_HOST_BUILD" M="$POOL_MODULE_BUILD" LOCALVERSION= modules

gcc -O2 -static -Wall -Wextra -Werror pool_handoff_probe.c -o /tmp/pool-handoff-probe
gcc -O2 -static -Wall -Wextra -Werror pool_memory_smoke.c -o /tmp/pool-memory-smoke-20261030
gcc -O2 -static -Wall -Wextra -Werror pool_memory_kvm_smoke.c -o /tmp/pool-memory-kvm-smoke-20261030

python3 pool_handoff_prepare.py --base-assets "$REPORTING_ASSETS" --assets "$NEW_ASSETS" \
  --firecracker "$NEW_FIRECRACKER" --guest-kernel "$NEW_GUEST_BUILD/vmlinux" \
  --guest-config "$NEW_GUEST_BUILD/.config" --guest-tool "$NEW_GUEST_RECORD/guest-pool" \
  --probe /tmp/pool-handoff-probe --l1-kernel "$NEW_HOST_BUILD/arch/x86/boot/bzImage" \
  --kvm-module "$NEW_HOST_BUILD/arch/x86/kvm/kvm.ko" \
  --kvm-intel-module "$NEW_HOST_BUILD/arch/x86/kvm/kvm-intel.ko" \
  --extra-tool "$POOL_MODULE_BUILD/pool_memory_module.ko" \
  --extra-tool /tmp/pool-memory-smoke-20261030 --extra-tool /tmp/pool-memory-kvm-smoke-20261030

python3 pool_handoff_run.py --assets "$NEW_ASSETS" --qemu "$CXL_QEMU" \
  --qemu-root "$QEMU_ROOT" --session pool-handoff-new --cycles 2 \
  --kvm-memory-smoke /tmp/pool-memory-kvm-smoke-20261030 --output "$NEW_RESULTS"
```

`pool_handoff_run.py`는 두 mode별 새 pair를 순서대로 만들고 stop·복사·독립 검증까지 수행한다. 이 wrapper는 코드 검토와 CLI 검사를 했으며, 보존된 최종 실행은 동일한 개별 `lab.py start` / `pool_handoff_bench.py` / `lab.py stop` 명령으로 실행했다. 정확한 최종 CLI/environment는 archive의 `outer-completion.json`을 따른다. 최종 측정은 run 디렉터리의 file-backed shared memory를 썼고 wrapper는 `/dev/shm`을 선택한다. 이 차이를 포함해 성능 동등성을 주장하지 않는다.

## 검증 범위와 다음 작업

현재 결과는 **한 물리 host의 coherent nested KVM**이다. 실제 비일관 multi-host CXL에서의 publication·flush/invalidate 검증은 남아 있다. Canonical full image는 고정하고 controller가 private allocation을 단독 관리한다. General filesystem, writable image 공유, controller 장애 복구, production seccomp, 다중 vCPU와 direct virtio-net은 아직 지원 범위 밖이다.

이번 controller에는 serial 명령·JSON manifest 전달·검증 scan이 포함된다. 전체 wall time을 기존 최적화 direct handoff의 latency나 한 페이지 CoW 비용으로 쓰지 않는다. 다음은 실제 workload에서 전환 시점의 캐시 공유량과 비용을 측정하고, 별도로 검증된 PML의 모든 writer 추적·publish 경계와 MPTCP/net device 소유권을 이 경로에 연결하는 단계다.
