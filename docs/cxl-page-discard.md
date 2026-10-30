# Guest가 버린 공유 페이지의 복사 없는 재사용

2026-10-30. 기존 pooling은 shared canonical 페이지를 처음 쓸 때 host CoW로 원본을 복사했다. Guest가 이미 그 페이지의 내용을 버린 경우에는 이 복사가 불필요하다. 이번 변경은 Linux의 **virtio-balloon free-page reporting**을 사용하여, 버려도 되는 페이지를 명시적으로 알리고 그 VM의 공유 연결을 fresh anonymous zero backing으로 교체한다.

기존 비교의 **4.08초는 4KiB 한 페이지의 CoW 시간이 아니다.** 160MiB file cache를 eviction하고, 224MiB anonymous 메모리를 할당·전체 쓰기·검사·해제한 시간이다. 이전 공유 페이지 40,960개가 그 과정에서 분리됐다. 한 페이지와 대량 재사용의 시간을 따로 측정해야 한다. [기존 비교](../implementation/virtual_machine/results/stock-comparison-20261030/README.md)는 변경하지 않는다.

[이번 실측 결과와 재현](../implementation/virtual_machine/results/pool-discard-20261030/README.md)은 단일 4KiB 쓰기, 400MiB 대량 재사용, 실제 guest/host ACK, NFS·DRAM/CXL 왕복을 구분한다. 정식 18개 반복 표본과 추가 재사용 3개, 이전 3회가 통과했다. 최초 쓰기 시간의 감소와 별도로 보고 대기·refill 비용을 함께 기록한다.

## 실제 guest/host 순서

1. Guest Linux의 reporter가 allocator의 free list에서 페이지를 잠시 빼낸다. 이 페이지들은 보고하는 동안 재할당되지 않는다.
2. Guest가 해당 GPA 범위를 reporting virtqueue에 넣고 host의 used-ring 응답을 기다린다.
3. Host가 전체 descriptor chain을 먼저 검증한다. 그중 해당 VM에 실제 설치했던 pooling overlay만 선택한다.
4. Host가 그 GPA의 HVA를 fresh anonymous `MAP_PRIVATE` zero backing으로 교체한다. Canonical 데이터를 읽어 복사하지 않는다. 해당 GPA의 과거 read hint도 제거하고 snapshot dirty bitmap을 표시한다.
5. Host가 virtqueue used ring에 완료를 게시한다.
6. Guest가 응답을 확인한 뒤 페이지를 allocator에 돌려준다. 이후 새 용도로 재사용한다.

```text
변경 전: VM A의 GPA ─┐
                     ├─ immutable canonical CXL page
         VM B의 GPA ─┘

변경 후: VM A의 GPA ─── fresh private zero backing
         VM B의 GPA ─── immutable canonical CXL page
```

Guest의 inode·folio·LRU를 다른 VM이나 host가 직접 읽지 않는다. Free-page reporting은 guest가 전달하는 정확한 페이지 수명 정보다. Bloom은 폐기를 승인하지 않으며, stale read hint로 폐기 여부를 추정하지 않는다. `free()`로 application 객체 하나를 해제한 것과 guest kernel이 전체 물리 페이지를 allocator에 반환한 것은 다르다.

**ACK가 모든 물리 메모리의 사전 할당 완료를 의미하지는 않는다.** 새 anonymous mapping은 lazy allocation을 사용한다. 이후 첫 접근에서 zero page 매핑이나 private page 할당·zeroing이 필요할 수 있다. 제거하는 것은 canonical 원본의 복사 의무다. 다른 VM이 사용하는 canonical 페이지는 변경하거나 반환하지 않는다.

## 적용 범위와 경쟁 조건

`FC_POOL_DISCARD=1`은 실험 기능을 명시적으로 활성화한다. Balloon은 target 0, free-page reporting 활성, free-page hinting·stats·deflate-on-OOM 비활성인 좁은 구성만 허용한다. 일반적인 balloon inflation이나 memory hotplug를 pooling과 통합한 것은 아니다. Fresh boot, restore, runtime 설정에서 같은 제약을 확인한다.

Host는 pending virtio-blk I/O의 GPA 범위를 제출 전부터 완료까지 추적한다. 진행 중인 I/O와 겹친 overlay는 연결을 바꾸지 않고 기존 CoW 경로로 남긴다. Non-overlay 페이지도 이 최적화에서는 매핑을 바꾸지 않는다. 그러므로 **report ACK와 실제 detach 성공을 구분**해야 한다. 기록에는 요청 범위, 실제 detach 범위, busy skip과 no-op을 구분하며, 검증은 실제 detach된 범위만 집계한다.

대상 집합은 VMM이 이전에 설치한 overlay의 GPA 기록이다. Kernel의 자동 CoW가 발생할 때마다 이 집합을 갱신하는 것은 아니다. 따라서 실제 remap된 페이지 수를 그대로 “회피한 CoW 횟수”로 해석하면 안 된다. `copied_bytes=0`도 이 host discard callback이 원본을 복사하지 않았다는 뜻이며, 보고 전의 다른 쓰기까지 CoW가 없었다는 뜻은 아니다.

Descriptor의 정렬·범위·중복 등을 전체 preflight한 뒤 remap한다. 인접한 대상은 합쳐서 매핑 호출 수를 줄인다. 실행 중 `MAP_FIXED` 실패는 일부 주소 공간을 이미 변경했을 수 있으므로 VMM을 즉시 종료하고 ACK하지 않는다. 기존 overlay가 설치된 상태의 state-only snapshot 거절도 유지한다.

Standard Linux reporter는 background 작업이다. 재사용 전에 보고되지 못한 페이지에는 기존 CoW가 발생할 수 있다. Guest를 idle 상태로 기다리게 한 뒤 재사용하는 검증은 protocol의 작동을 보여주지만, 보고 대기 시간을 제외한 first-touch 시간만으로 application 전체가 빨라졌다고 판단하면 안 된다.

## 측정과 증거를 읽는 법

- **4KiB 단일 페이지:** host store와 실제 KVM guest store를 따로 측정한다. 한 byte만 쓰고 나머지 4,095byte 및 peer의 원본을 검사한다. KVM 값에는 `KVM_RUN` 진입과 HLT exit가 들어가며 순수 fault latency가 아니다. 측정 전 read setup은 timer 밖이고 fresh-zero mapping은 이 단계에서 backing이 준비될 수도 있다.
- **대량 재사용:** cache eviction, 보고·ACK 대기, anonymous mmap, 최초 전체 쓰기, 전체 검사, unmap을 분리한다. 보고 대기까지 포함한 관측 시간과 guest 작업 시간을 함께 기록한다.
- **Protocol:** guest trace의 submit·정확한 SG 범위·used-ring 응답을 host의 요청·실제 detach·ACK 기록과 연결한다. 서로 다른 guest/host 시계의 절대 timestamp를 직접 비교하지 않는다. Trace ring loss나 빠진 필수 record를 허용하지 않는다.
- **실제 데이터:** 공유 전 현재 file-cache GPA와 host PFN을 확인한다. 이후 실제 detach 범위와 zero backing, 새 데이터를 쓴 private PFN을 확인하고 다른 두 VM의 payload를 전수 검사한다. Fresh anonymous zero backing의 아직 fault하지 않은 페이지에 고유 PFN이 있다고 가정하지 않는다.
- **Host fault 진단:** 정규 성능 표본과 별도 실행에서 `__handle_mm_fault`, `do_wp_page`, KVM MMU notifier, remote TLB flush의 진입·복귀를 추적한다. 이 kernel에서는 `do_cow_fault`가 inline되어 있다. 아직 host PTE가 없는 첫 쓰기와 이미 읽은 페이지의 write-protect fault를 구분하며, canonical로 remap한 뒤 guest의 실제 read를 먼저 수행한다. 함수 시간은 하위 호출·preemption·probe overhead를 포함하므로 중첩된 합계를 더하거나 복사만의 시간이라고 해석하지 않는다.

단일 페이지 도구의 `detach_ns`는 setup mapping 교체 시간이다. KVM variant도 canonical mapping에 guest EPT가 설정된 상태를 만든 다음 detach하는 구성은 아니므로, 실행 중인 VM의 detach 비용을 대표하지 않는다. 실제 reporting callback의 `host_work_ns`와 guest의 완료 대기를 함께 확인해야 한다.

또한 “224MiB를 새로 할당했다”는 사실만으로 과거 cache의 160MiB를 재사용했다고 볼 수 없다. Reporter가 free list에서 페이지를 꺼냈다 돌려놓으면 allocator의 다음 선택이 달라질 수 있다. 검증은 새 allocation의 실제 GPA와 과거 cache GPA의 교집합을 확인한다. 작업 크기를 늘린 측정은 같은 크기의 별도 비교군과 비교하며, 이전 224MiB 결과와 직접 속도 비율을 계산하지 않는다.

## 이전과 남은 범위

Full RAM snapshot은 현재 HVA가 보는 canonical·private·zero 내용을 저장한다. Destination은 복원된 RAM에서 새 owner/epoch로 동작하며, reporting device 상태와 guest의 격리 중인 페이지 상태도 snapshot의 일부다. 이후 admission은 새로 관측·검증한 페이지에만 적용한다. Local/NFS image backend와 RAM tier는 독립적이다.

이번 기능도 VM별 원래 DAX reservation을 allocator에 반환하지 않는다. 원본을 복사하지 않는 detach, guest 재사용, 실제 사용 가능한 CXL 용량 증가를 구분한다. Canonical frame의 전역 수명 관리, private allocation의 CXL 배치, pooling 상태의 direct handoff, 비일관 물리 CXL 검증은 별도 과제다.

## 재현 도구

`pool_discard_prepare.py`는 이전 fixture를 변경하지 않고 새 VMM, reporting을 활성화한 guest kernel과 probe로 별도 fixture를 만든다. `pool-discard-guest-trace.patch`는 기존 reporter의 격리·submit·ACK 순서를 기록하며 allocator 동작은 바꾸지 않는다. Guest config에는 `CONFIG_VIRTIO_BALLOON=y`, `CONFIG_PAGE_REPORTING=y`, `CONFIG_EVENT_TRACING=y`가 필요하다.

`pool_discard_run.py`는 같은 새 binary와 guest kernel로 reporting 비활성/활성 군을 새 lab에서 실행한다. `pool_discard_bench.py`가 guest 단계별 측정과 현재 cache GPA·host PFN·guest ACK를 수집한다. `verify_pool_discard.py`와 `verify_pool_discard_protocol.py`는 원본 receipt로 이를 다시 검사한다. `pool_cow_latency.c`는 별도의 한 페이지 진단이고, `pool_cow_trace.py`는 정규 timing sample 이후 별도 kernel trace를 수집한다.
