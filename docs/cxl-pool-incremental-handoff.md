# RAM 전수 순회를 제거한 pooling handoff

## 무엇을 바꾸는가

CPU만 다른 호스트로 넘기는 **CXL→CXL handoff**에서는 RAM을 복사하거나 모든 GPA의 PFN을 다시 찾을 이유가 없다. VM의 실제 backing이 그대로이고, 그 매핑을 관리하는 주체가 바뀌었다는 것을 증명하면 된다.

기존 구현은 scattered backing의 안전성을 확인하려고 전체 GPA→PFN을 다시 수집했다. 동시에 Firecracker와 Python controller가 전체 mapping/credit 목록을 JSON으로 저장·재검사했다. 2GiB VM에서는 524,288개 매핑을 매번 처리했다. 이 방식은 안전성 확인을 반복 비용으로 만들었다.

이번 구현은 **VM별 authoritative mapper table 자체를 예약된 CXL 영역에 유지**한다. 목적지는 작은 handle로 같은 table을 연결한다. 다른 VM의 guest inode, xarray, refcount를 읽거나 공유하는 설계가 아니다. 각 VM의 guest filesystem metadata는 계속 독립적이다.

## 전수 처리가 필요한 경우와 필요하지 않은 경우

| 작업 | 필요한 범위 | 정상 경로의 처리 |
|---|---|---|
| DRAM→CXL 최초 promotion | 논리 RAM 매핑, private RAM 데이터 | 최초 grant 중복/범위 검사, private 데이터 복사. 연속 private 영역은 묶어서 복사 |
| CXL→DRAM demotion | 실행에 필요한 전체 논리 RAM | private DRAM으로 복사하고 기존 mapper를 retire |
| CXL→CXL handoff | VM별 descriptor와 소유권 정보 | 고정 크기 handle 검사. GPA/PFN 전수 조회·RAM 복사 없음 |
| CoW | 쓰는 4KiB 페이지 | canonical에서 private credit으로 복사 후 해당 매핑 변경 |
| Guest discard/reuse | guest가 격리해서 보고한 범위 | 그 범위의 lineage만 조회하고 해당 VM의 연결만 해제·zero. ACK 후 guest 재사용 |
| Credit 추가 | 새로 받은 credit 목록 | membership bitmap으로 기존/신규 중복 검사. 전체 mapping 재정렬 없음 |
| Guest 공유 후보 등록 | 선택한 immutable lower file의 resident folio | 현재 guest authority 확인. 전체 guest RAM 탐색이나 payload 비교 없음 |
| 최초 retained EPT admission | 처음 인계할 EPT 구조 | 최초 신뢰 기반 수립 검사는 유지. 이미 검증된 EPT의 반복 순회는 하지 않음 |
| 명시적 실험 audit | 사용자가 선택한 전체 PTE/매핑 | `FC_POOL_FULL_AUDIT=1`일 때만 수행. handoff 요구 조건이 아님 |
| 과거 hint-only 실험 경로 | hint로 선택한 후보 페이지 | guest authority가 없어 내용 비교가 필요. ABI 2 정상 경로에서는 호출하지 않음 |
| 손상/알 수 없는 mapper | 유효한 proof 없음 | 정상 handoff를 거절. 몰래 전체 스캔하는 fallback 없음 |

Promotion/demotion의 데이터 이동은 중복 검증 스캔과 구분해야 한다. 현재 eager-copy 방식에서는 실제로 데이터를 옮겨야 한다. 이를 없애려면 lazy migration/post-copy 같은 별도 설계가 필요하다.

## handoff의 안전성

1. Source vCPU를 정지하고 진행 중인 block I/O를 drain한다. Completion ring의 변경까지 끝낸다.
2. Mapper를 SEALED로 전환한다. 새 CoW·discard·credit 변경과 source resume를 거절한다.
3. Direct 모드는 KVM이 등록된 kernel mapper callback으로 descriptor 물리주소, 인스턴스 cookie, allocation lease, mapping generation, owner epoch, RAM geometry를 확인한다. 단순한 사용자 hint를 믿지 않는다.
4. Source process를 fence한다. 이미 설치된 writable PTE가 있는 source를 실행하면서 목적지를 켜도 된다는 뜻이 아니다.
5. 목적지의 `CPM_IMPORT`가 SEALED 소유권을 한 번만 소비하고 epoch를 증가시킨다. 동일 descriptor와 table을 연결하므로 전체 table을 다시 만들지 않는다.
6. Direct attach는 앞서 보존한 proof와 목적지 mapper identity를 비교한다. State-only는 CPU/device state와 handle manifest의 digest 결합을 유지한다.
7. 목적지에서 정상 실행한다. 새로운 PTE는 실제 접근 때 설치된다. 보존된 direct EPT의 backing identity는 kernel proof로 확인한다.

이전 handle의 cookie·epoch·mapping generation·allocation lease가 다르면 import를 거절한다. 같은 metadata 슬롯을 demotion 후 재사용해도 새 cookie가 이전 fd와 분리한다. 오래된 fd를 닫는 동작이 새 mapper를 retire할 수 없다.

실패 후 자동으로 source를 재개하거나 전체 RAM을 스캔해 우회하지 않는다. 이미 소유권이 이동했거나 mapping이 불완전한 경우는 실행을 중단해야 한다.

## 추가로 줄인 연산

- 정상 export/restore의 전체 mapping·credit JSON을 없앴다. 임시 mapping 벡터는 내용뿐 아니라 할당 용량까지 반환한다. ABI 2 handle은 19개 `u64`, 152 bytes이며 JSON manifest에는 VM identity 등 소량의 정보만 추가된다.
- Restore 때 전체 공유 GPA 집합을 재구성하지 않는다. 공유 이력(lineage)은 mapper entry에 남기고 guest reporting 범위만 조회한다. CoW 후에도 이력은 유지하고 discard 완료 때 지운다.
- Pooling의 장치 quiesce에서 사용하지 않는 device state를 직렬화하지 않는다. 장치의 prepare/drain은 유지한다.
- 읽기 전용 block backend의 snapshot preparation은 I/O drain만 한다. 쓰기 가능한 디스크의 flush와 오류 검사는 유지한다.
- Guest authority 경로에서는 사용하지 않는 과거 block-read hint 기록을 생성하지 않는다. Pending I/O buffer 추적은 계속한다.
- Controller allocator는 free extent를 관리한다. 매 allocation마다 전체 free frame 정렬·전체 pool 재검사를 하지 않는다. `check()`는 명시적 진단으로 남긴다.
- SWE promotion 후보 선택에서 반복하던 집합 교집합 계산을 한 번으로 줄였다.
- 단일 파일 registration의 frozen extent 검색은 정렬된 file page 순서에 맞춰 전진한다. 매 페이지마다 모든 extent를 검색하지 않는다.

## CXL metadata와 coherence의 범위

현재 구현·검증은 **한 물리 호스트의 coherent QEMU shared backing + 두 독립 Linux + nested KVM**이다. SEALED→ACTIVE의 atomic claim은 이 조건에서 검증한다. 실제 비일관적 CXL 노드에서 메모리 barrier만으로 coherence를 제공한다고 주장하지 않는다. 그 환경에서는 descriptor/table 게시·invalidate와 단일 owner lease를 별도로 연결해야 한다.

Host mapper metadata는 VM마다 별도이며 실행 owner만 갱신한다. Synthetic 배치는 3072MiB 이후, SWE 배치는 8192MiB 이후에 VM당 64MiB extent를 예약한다. Private allocator, canonical image, Bloom 영역, KVM arena와 겹치지 않게 분리한다.

이 공간도 비용이다. 실제 table 크기는 `4KiB header + GPA당 16 bytes + 최대 credit array 8MiB + device frame membership bitmap`이다. 2GiB RAM / 16GiB device에서는 약 16.5MiB를 사용하고 64MiB 용량을 예약한다. Guest 데이터 공유 절감량에서 이 비용을 제외하지 않아야 한다.

## 전수 prefault를 제거하면서 드러난 nested KVM 문제

이 시험에서는 L0 KVM 위에 A/B Linux가 있고, 그 안의 L1 KVM이 sandbox를 실행한다. A가 CXL에 있는 EPT를 변경해도 B 쪽 L0의 nested shadow EPT는 그 쓰기를 관찰하지 못한다. 기존 전수 PFN 조회를 제거한 뒤 반복 왕복에서 guest 데이터 불일치와 triple fault가 관찰되었다. Guest TLB flush만 추가한 대조 시험도 실패했다.

실험용 `kvm.dax_nested_root_sync=1`은 attach 중 **retained EPT root의 offset 1에 현재 byte를 그대로 한 번 쓴다**. 이 호스트 KVM의 비정렬 EPT 쓰기 처리 경로를 통해 오래된 nested shadow root를 무효화한다. Sandbox EPT root·내용은 유지하며 RAM이나 EPT leaf를 순회하지 않는다. Guest TLB flush도 명시적으로 요청한다. 이 옵션은 기본값이 꺼져 있고 두 nested lab harness에서만 켠다.

이는 외부 KVM의 주소 변환 캐시를 갱신하기 위한 실험 환경 보완이다. 물리 CXL cache-line의 publish/invalidate 프로토콜을 대신하지 않는다. 첫 진단 시험에서는 paused EPT/mapper 대조를 추가했고, 최종 시험에서는 그 진단 순회 코드를 제거한 상태로 다시 실행해 통과했다. 기존 diskless 경로의 수천 회 결과는 과거 구현의 기록이며 이번 변경을 같은 횟수로 재검증했다는 뜻이 아니다.

## 검증과 남은 범위

최종 [결과·원시 로그·재현 명령](../implementation/virtual_machine/results/pool-incremental-20261102/README.md)에 실행 환경을 기록했다. 독립 3 VM × 160MiB cache의 두 모드 stress와 2GiB Gin 원본 trace에서 총 **58회 전환·20회 CPU handoff·54개 SWE 명령**이 통과했다. 모든 CPU handoff에서 RAM 전송과 전수 조회 증가는 0이었다.

| 2GiB Gin, 각 모드 2회 인계 | 과거 API 중앙값 | 이번 API 중앙값 | 이번 manifest |
|---|---:|---:|---:|
| state-only | 798.88ms | 22.07ms | 758–762 bytes |
| direct | 1170.84ms | 11.66ms | 695–699 bytes |

두 시점의 같은 Gin API 범위를 비교한 값이며, 각 모드 2 samples다. 전체 downtime이나 pristine Firecracker 대비 결과가 아니다. Direct kernel detach+attach만의 중앙값은 2.48ms였다.

새 독립 verifier `verify_pool_handles.py`는 handle 인계와 전수 조회 카운터가 유지되는지 검사한다. 과거 full-manifest 결과용 `verify_pool_handoff.py`는 별도로 보존한다.

전수 PTE 관찰과 canonical 전체 비교는 stress 검증에서 명시적으로 수행할 수 있다. 이를 정상 handoff의 필수 작업으로 넣지 않는다. 측정은 promotion, demotion, handoff API, direct kernel detach/attach, serial orchestration을 나눠 보고해야 한다.

Userspace wrapper 재생성, serial 기반 실험 제어, I/O 연결 재구성, 실제 비일관적 다중 물리 노드 프로토콜은 별도 남은 작업이다. 전체 RAM 순회를 없앴다는 사실만으로 모든 handoff가 10ms 미만이라고 결론 내릴 수 없다.
