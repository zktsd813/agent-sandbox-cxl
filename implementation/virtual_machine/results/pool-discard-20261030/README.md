# Guest 폐기 알림과 공유 페이지 재사용 — 2026-10-30

**Guest가 free page를 격리·보고하면 host가 해당 VM의 backing을 private zero mapping으로 교체하고, guest는 ACK를 받은 뒤 재사용하는 경로를 구현했다.** 다른 VM의 canonical 페이지는 유지한다. 설계와 예외 처리는 [페이지 폐기 문서](../../../../docs/cxl-page-discard.md)에 설명한다.

정식 실험은 **독립 VM 3개 × 160MiB file cache, 두 비교군 각각 3회 반복**이다. Reporting 군에서는 매 표본 최소 **150.61MiB**를 `현재 canonical cache → host remap → guest ACK → 실제 anonymous 재사용`으로 연결했다. 전체 18개 정규 표본과 추가 재사용 3개, migration 3회, guest 명령 404개, 원본 receipt 3,300개를 독립 검증했다. [정식 감사](verification/formal-suite-audit.json), [요약 수치](verification/formal-summary.json)에 근거를 보존한다.

## 4초와 4KiB는 다른 측정이다

이전 **4.08초는 4KiB 한 페이지의 CoW가 아니라**, 160MiB cache eviction과 224MiB anonymous 할당·전체 쓰기·검사·해제 시간이다. 이전 공유 페이지 40,960개가 분리됐다. [이전 결과](../stock-comparison-20261030/README.md)는 변경하지 않는다.

이번 단일 페이지 도구는 한 4KiB 페이지의 한 byte를 쓰고 나머지 4,095byte, canonical 및 peer의 원본, 실제 PFN 분리를 검사한다. 정식 두 lab에서 각 항목 100회씩 측정했다. 단위는 **µs, 중앙값**이다.

| 한 페이지 작업 | Baseline lab | Reporting lab |
|---|---:|---:|
| Host 직접 store, canonical CoW | 5.87 | 6.69 |
| KVM store, canonical CoW | 168.11 | 207.96 |
| KVM store, 이미 준비된 private page | 37.69 | 46.82 |
| KVM store, fresh zero mapping | 169.49 | 204.76 |

이 도구는 Firecracker 밖에서 KVM을 직접 사용한다. 두 lab의 차이를 reporting flag의 효과라고 해석하지 않는다. KVM 값은 진입·한 byte store·HLT exit를 포함하고, 사전 read setup은 timer 밖이다. Zero backing도 첫 쓰기의 할당·fault 비용은 남는다. Setup의 `detach_ns`는 canonical guest EPT를 의도적으로 설치한 뒤 측정한 값이 아니므로 실제 실행 중 discard 비용을 대표하지 않는다. 고정된 한 페이지·고정된 mode 순서의 진단이며 일반 hardware latency 분포가 아니다.

별도 새 VM의 **224MiB 진단**에서는 canonical을 guest가 실제 읽은 뒤 재사용했다. 원래 cache의 40,960개 PFN이 모두 바뀌었고, fault·write-protect·MMU invalidate·TLB flush 호출도 각각 40,960개였다.

| 별도 계측 실행 | 관측 시간 |
|---|---:|
| Guest 전체 eviction·224MiB 재사용 작업 | 5.123s |
| `__handle_mm_fault` inclusive 합 | 0.967s |
| 그 안의 `do_wp_page` inclusive 합 | 0.923s |
| KVM MMU invalidate inclusive 합 | 0.0784s |
| KVM remote TLB flush inclusive 합 | 0.0311s |

**중첩된 시간을 더하면 안 된다.** Probe·preemption 영향이 있으며, 큰 outlier 하나는 약 191ms였다. Trace 출력 정밀도는 1µs이므로 TLB median 0은 비용이 없다는 뜻이 아니다. 전체 작업과 outer fault 합의 산술 차이 4.156s는 이 실험에서 원인을 세분하지 않았다. 4초 전체를 원본 복사, mapping, peer를 기다린 시간이라고 설명할 근거가 없다. [진단 원본과 범위](diagnostic/README.md), [327,682개 trace entry 독립 검사](verification/cow-diagnostic-independent-audit.json)를 참고한다.

## 같은 400MiB 작업으로 폐기 경로 비교

두 군은 같은 새 Firecracker·guest kernel·image·helper를 사용한다. **Baseline은 reporting을 끈 기존 pooled CoW이며 순정 Firecracker가 아니다.** Reporting 군은 target 0의 reporting-only balloon과 `FC_POOL_DISCARD=1`을 사용한다. 순정 VMM 비교는 [이전 문서](../../../../docs/firecracker-pooling-comparison.md)에 별도로 있다.

각 VM은 512MiB RAM·2 vCPU, read-only virtio-blk Async backend를 사용한다. Guest metadata는 VM별로 유지한다. L1은 각 4 vCPU·8GiB RAM, emulated CXL 4GiB이며 물리 host 하나의 nested KVM/QEMU 환경이다. CPU 고정·THP/KSM 정책과 실제 executable/kernel/helper hash를 확인했다. Reporting → baseline 순서로 각각 새 두-node lab을 사용했다. 9개 표본은 9개 독립 boot가 아니라 3개 VM의 반복 측정이다.

초기 224MiB allocation은 reporter가 바꾼 free-list 순서 때문에 과거 cache를 12MiB만 재사용했다. 이를 충분한 재사용 검증으로 인정하지 않고 보존했다. 메모리 여유를 사전 확인한 뒤 두 군을 동일한 **400MiB** 작업으로 고정했다. 매 표본 allocation 전 16MiB 이상의 available reserve를 요구했다. 이전 224MiB 수치와 이번 400MiB 수치로 직접 속도 비율을 계산하지 않는다.

단위는 **ms, 9개 정규 표본의 중앙값**이다. Guest의 단계별 timer에는 serial 명령 전달·원본 trace 수집·PFN 감사가 들어가지 않는다.

| 단계 | 기존 pooled CoW | Reporting/ACK 후 재사용 |
|---|---:|---:|
| 160MiB hot read와 전체 검사 | 137.50 | 131.50 |
| Cache eviction | 27.98 | 28.69 |
| 400MiB mmap, 아직 touch하지 않음 | 0.012 | 0.012 |
| 400MiB 최초 전체 쓰기 | **4,312.17** | **1,109.97** |
| 400MiB 전체 데이터 검사 | 140.61 | 175.70 |
| Anonymous unmap | 64.66 | 66.31 |
| 160MiB guest-cold refill와 전체 검사 | **230.27** | **434.65** |

첫 쓰기는 줄었지만, **보고 대기와 refill 회귀도 남았다.** Reporting에서는 실제 재사용한 canonical 페이지 중 128MiB 이상이 host에서 remap되고 guest가 ACK를 소비한 시점까지 eviction 후 중앙값 **2,093.45ms**였다. 같은 guest MONOTONIC clock의 값이며 scheduling·host service·guest completion을 포함한다. 순수 host remap latency가 아니다. Controller의 polling·trace 전송까지 포함한 관측 대기는 중앙값 **5,896.95ms**였다. 이를 application latency나 direct handoff 비용으로 바꾸어 부르지 않는다.

Reporting 전체 epoch의 155개 batch 중 실제 remap을 한 105개 batch의 `host_work_ns` 중앙값은 1.252ms, 평균 7.824ms였다. Batch 크기가 다르며 한 페이지 비용이 아니다. 이 계측은 `discard_reported()` 내부 작업으로, descriptor chain 수집·로그 직렬화·ACK 게시와 이후 lazy fault 비용을 모두 포함하는 값이 아니다. 50개 no-op batch도 ACK와 구분했다. 실제 busy skip은 0개였고 해당 분기는 단위 테스트로 검증했다.

첫 round의 최초 쓰기 중앙값은 baseline/reporting 각각 5,350.16/2,126.81ms, 두 번째는 4,151.91/1,099.39ms, 세 번째는 4,225.67/1,091.53ms였다. Backing과 allocator 이력이 영향을 주므로 전체 중앙값만으로 warmup을 숨기지 않는다. 이 단계별 실험은 실제 agent RL trace의 종단 성능 검증이 아니다.

## 실제 재사용, peer 격리, 이전

- 각 sample 전에 현재 guest file-cache offset→GPA→VMM HVA→canonical PFN을 확인했다. Reporting 군은 매 sample 최소 150.73MiB의 과거 cache GPA를 실제 held anonymous allocation에서 재사용했고, 그중 최소 150.61MiB는 host remap·guest ACK를 거친 범위였다. Baseline은 160MiB 전체가 재사용되고 private PFN으로 바뀌었다.
- 400MiB 전체 word oracle, 다른 두 VM의 payload·private 상태, canonical image 전체 byte 비교가 통과했다. 큰 guest allocation은 첫 쓰기 **후** 전수 검사이며 모든 페이지의 write 전 zero 전수 측정은 아니다. Zero semantics와 한 byte partial-write 보존은 host 단위 테스트 및 한 페이지 도구로 확인했다.
- No-explicit-wait sample도 통과했다. 명시적인 report wait를 생략한 경우이며, serial 제어 사이에도 reporter가 실행될 수 있으므로 미보고 페이지의 fallback을 강제로 유발한 실험이라고 주장하지 않는다.
- VM1은 `A/CXL → B/CXL → A/DRAM → A/CXL`로 이동했다. B의 추가 재사용은 151.89MiB, 마지막 A/CXL은 151.16MiB에서 정확한 detach·ACK·reuse를 확인했다. B의 cold refill은 **NFS READ RPC 1,285개**를 발생시켰다.
- DRAM 복원은 DAX backing/admission이 없음을 확인하고, 종료·fence한 source의 이전 mutable CXL RAM 512MiB를 poison한 뒤에도 guest 상태를 유지했다. 각 이전에서 full RAM/state transport와 process identity·private 상태·warm cache·peer 데이터를 검사했다.

이전은 **full RAM snapshot**이다. 성공한 snapshot 경계의 discard sequence를 기록해 retired source와 새 epoch의 ACK를 구분했다. 실제로 잘린 post-snapshot tail은 관측되지 않았다. 의도적으로 pending report 순간에 snapshot/kill을 주입한 stress 검증은 아니다. Overlay가 있는 state-only/direct handoff는 지원 완료로 취급하지 않는다.

## 검증과 재현

[Host 빌드·선별 테스트](build/README.md)는 pool 9, balloon 31, snapshot/device/I/O 9회 통과를 기록한다. 일부 테스트가 중복된다. 깨끗한 upstream에 patch를 적용한 28개 파일과 실제 release source가 일치했다. [Protocol·snapshot cutoff 테스트](verification/pool-discard-independent-unit-tests.log) 20개와 [trace parser 테스트](verification/trace-parser-unit.log) 12개도 통과했다.

성공한 실제 증거를 변조하는 검사에서는 reporting 10개와 baseline 6개를 모두 거절했다. 실패를 success로 감추기, 표본 누락, 시간 조작, 이전 canonical PFN 유지, runtime hash·원본 receipt 변조, 거짓 detach 수/범위, 누락·역순 ACK를 포함한다. [Reporting 거절 기록](verification/formal-reporting-negative-audit.json), [baseline 거절 기록](verification/formal-baseline-negative-audit.json)에 원인을 보존했다. 이는 측정 증거의 일관성 검사이며 측정 프로그램 자체가 악의적인 경우에 대한 보안 증명은 아니다.

준비 중 실패한 세 pilot은 [실패 이력](failures/README.md)에 원본 상태 그대로 보존했다. Controller가 성공으로 표시한 pilot3 reporting도 독립 검증에서 로그 interleaving을 발견했으므로 정식 결과에서 제외했다. 최종 실행은 `--log-path`로 VMM 로그와 guest serial 출력을 분리했다. 메모리 sizing smoke는 [준비 기록](preparatory/README.md), 진단은 [별도 기록](diagnostic/README.md)에 있다.

실행 소스·raw evidence와 추출 후 재검증 명령은 [evidence 재현 안내](evidence/REPLAY.md)에 있다. Base fixture·전용 host kernel·CXL QEMU는 [기존 준비 절차](../../../../docs/cxl-data-pooling.md)를 사용한다. Guest kernel에는 reporting config와 계측 patch를 적용한다. 정확한 config·원본 source·compiler command·재빌드 검사와 host smoke는 [guest 빌드 기록](build/guest/README.md)에 있다.

실제 배포 archive를 새 경로에 추출한 뒤 [정식 suite](verification/archive-replay-suite-audit.json)와 [별도 CoW 진단](verification/archive-replay-diagnostic-audit.json)을 다시 검증했다. [원래 positive audit과 모든 필드가 동일](verification/archive-replay-comparison.json)했다. 패키징은 모든 archive member의 이름·type·길이·SHA-256을 원본과 대조하며 외부 disk image는 포함하지 않는다.

정식 두 lab의 QEMU 4개는 종료됐고 network cleanup·socket·임시 RAM backing 정리도 기록했다. [Reporting 정리 증거](evidence/reporting-packaging-proof.json), [baseline 정리 증거](evidence/baseline-packaging-proof.json), [배포 파일 manifest](manifest.json)를 함께 보존한다.

다음은 새 실행의 골격이다. 모든 output/assets 경로는 새 디렉터리를 사용한다.

```sh
cd implementation/virtual_machine
gcc -std=c11 -O2 -static -Wall -Wextra -Werror pool_discard_probe.c -o /tmp/pool-discard-probe
gcc -std=c11 -O2 -static -Wall -Wextra -Werror pool_cow_latency.c -o /tmp/pool-cow-latency
python3 pool_discard_prepare.py --base-assets "$POOL_BASE_ASSETS" \
  --assets /tmp/discard-assets --firecracker "$DISCARD_FC" \
  --guest-kernel "$DISCARD_GUEST_KERNEL" --guest-source "$DISCARD_GUEST_SOURCE" \
  --guest-config "$DISCARD_GUEST_CONFIG" --extra-tool /tmp/pool-cow-latency
python3 -u pool_discard_run.py --assets /tmp/discard-assets \
  --qemu "$POOL_QEMU" --qemu-root "$POOL_QEMU_ROOT" --session discard-new \
  --modes reporting baseline --rounds 3 --anon-mib 400 --lifecycle \
  --output /tmp/discard-run
python3 verify_pool_discard.py /tmp/discard-run/report.json --out /tmp/discard-audit.json
```

원래 VM별 DAX reservation 반환, private page의 CXL 배치, online donor export, 비일관 물리 CXL, scattered backing을 유지하는 direct handoff는 남아 있다. `copied_bytes=0`은 해당 callback이 canonical을 복사하지 않았다는 뜻이다. VMM의 overlay GPA 기록은 자동 kernel CoW 때마다 갱신하지 않으므로 remap 횟수를 곧바로 회피한 CoW 횟수로 해석하지 않는다. 실제 frame 공유, 복사 없는 폐기, 순 가용 CXL 용량 증가는 서로 다른 결과다.
