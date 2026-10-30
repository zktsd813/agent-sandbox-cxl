# 수정하지 않은 Firecracker와 CXL data pooling 비교 — 2026-10-30

**독립 부팅한 VM 3개의 실제 160MiB file-cache 데이터는 stock에서 고유 frame 480MiB, pooling에서 160MiB를 사용했다. 일반 Firecracker도 같은 warm snapshot을 복원하면 160MiB를 공유했다.** 따라서 추가한 기능은 독립 VM의 관측된 image 페이지를 사후에 canonical CXL frame으로 연결하는 것이다. 현재는 원래 DAX allocation을 반환하지 않으므로 순 가용 용량이 320MiB 늘었다고 주장하지 않는다.

**현재 pooling에는 큰 재사용 비용이 있다.** File-cache eviction과 224MiB anonymous 재사용·검사 workload는 중앙값 4,079ms로, 같은 CXL/hint 조건에서 공유하지 않았을 때의 271ms보다 약 15.1배 길었다. 15회 모두 이전 cache의 40,960개 PFN이 private으로 바뀌었다. 측정한 것은 전체 workload이며 한 번의 CoW fault 지연이나 그 비용만을 분리한 값이 아니다.

기능별 설명은 [비교 문서](../../../../docs/firecracker-pooling-comparison.md), 이전 promotion/demotion 검증은 [lifecycle 결과](../pool-20261030/README.md)에 있다.

## 기준과 조건

- Stock: upstream v1.17.0 commit `95f868c8e345b1cc8faccd1a3c910b4989dc3f58`, 수정 없이 별도 release/musl 빌드. 빌드 전후 source clean, 실제 실행한 `/proc/PID/exe` hash까지 검증했다. [빌드 기록](stock-build.json), [원본 로그](stock-build.log).
- Patched: lifecycle 검증과 동일한 binary `866f5b44b96535c0c525f6cee0a9ee09ff64900fbd83458c51c45b4de3505d08`. 이 비교를 위해 production VMM·kernel·기존 workload는 변경하지 않았다.
- 모든 군: 동일한 immutable image, guest kernel·timed helper, 512MiB RAM·2 vCPU·Async/Writeback read-only virtio-blk, guest ext4와 VM별 tmpfs/private mapping. 두 VMM 모두 `--no-seccomp`.
- 군마다 새 QEMU/KVM L1 두 노드와 새 outer disk를 생성했다. 물리 host 하나, L1당 4 vCPU·8GiB RAM, emulated CXL 4GiB. 실제 QEMU는 10.2.2, L1 kernel은 `6.6.0-rc6-dcd-v3`. Fixture의 과거 `prepared.qemu_version` 6.2 기록은 실제 실행 버전이 아니다.
- 동일한 실험용 L1 kernel을 사용한다. “Stock”은 VMM이 upstream 그대로라는 의미이며 host kernel 전체까지 stock인 독립 환경과 비교한 것은 아니다. 비일관 물리 다중 host CXL의 coherence·latency 검증도 아니다.
- L1 안에서 THP를 `never`, KSM을 0/없음으로 확인했다. 물리 host 정책은 바꾸지 않았다. QEMU A는 host CPU 0–3, B는 4–7; VMM main은 L1 CPU 2–3, vCPU는 각각 L1 CPU 0·1; guest helper는 guest CPU 0에 고정했다.

네 군의 순서는 stock DRAM → patched DRAM/hint 비활성 → CXL/hint 활성·공유 비활성 → CXL/pooling이다. 각 군은 **독립 boot 3개에서 5회씩, operation별 15회 관측**했다. 15개 독립 boot 실험으로 취급하지 않는다. VM 측정 순서는 round마다 회전한다. 전체 군의 순서는 고정되어 있으며 별도 stock 재실행으로 실행 시점 변동도 확인한다.

## Guest 작업 시간

단위는 ms, 중앙값이다. Serial 명령 전달·readiness 대기·JSON 출력·사후 검사는 guest timer 밖이다. 읽기는 전체 160MiB word oracle을 포함한다. 순 bandwidth, guest cache lookup latency, migration downtime이 아니다.

| 조건 | 160MiB hot read+검사 | Guest-cold refill+검사 | Private overwrite | Eviction+224MiB 재사용 |
|---|---:|---:|---:|---:|
| Stock DRAM | 116.76 | 264.68 | 9.62 | 255.62 |
| Patched DRAM, hint 비활성 | 112.89 | 187.08 | 12.56 | 259.87 |
| Patched CXL, hint만 활성 | 120.25 | 198.65 | 10.70 | 270.79 |
| Patched CXL, pooling | 138.57 | 223.23 | 12.52 | 4,079.49 |
| Stock DRAM, 마지막 별도 반복 | 122.91 | 218.93 | 9.82 | 297.51 |

마지막 stock 반복도 새 VM 3개 ×5회이며 같은 frozen source·fixture·설정을 사용했다. [tail-stock-audit.json](tail-stock-audit.json)에서 모두 통과했다. Cold 중앙값은 첫 stock의 264.68ms에서 218.93ms로 약 17% 달라졌다. 이 실행 시점 변동 때문에 작은 차이를 특정 patch의 효과로 단정하지 않는다.

전체 최소·최대, round별·VM별 분포와 원본 nanosecond 값은 audit 및 evidence에 보존한다. 예를 들어 pooling 재사용은 4,010.38–4,309.76ms, 공유하지 않은 CXL 재사용은 225.79–570.32ms였다. 첫 round의 allocation 비용도 제외하지 않았다.

Stock과 patched DRAM의 차이에는 이전 DAX·storage·migration 변경도 포함된다. Hint를 비활성화한 경우의 cold refill이 빨랐다는 관측만으로 pooling 효과라고 설명할 수 없다. 현재 설계의 반복 실행 비용과 frame 공유를 구분하고, 이 nested 환경의 차이를 실제 CXL hardware 성능으로 일반화하지 않는다.

Hot sample마다 현재 file cache를 pin/pause하여 실제 PFN을 확인했다. Pooling의 모든 sample에서 initial three-VM 공통 frame과 대응한 현재 cache가 160MiB였다. 각 recycle 뒤에는 **동일한 과거 GPA/HVA**의 PFN 변화를 비교했으며, 이때 그 주소들이 현재 file cache라는 주장은 하지 않는다. Cold refill 뒤 다시 admission한 시간은 다음 hot 작업의 timer 밖이다.

Cold는 guest cache residency가 0인 상태다. 각 sample 전 host image 전체를 읽고 `mincore`로 warm 상태를 확인했다. 실제 guest block read도 160MiB 이상 발생해야 통과한다. Storage-cold나 NFS-cold timing은 아니다. 네 군은 모두 image home인 A에서 local backend를 사용한다. CXL에 RAM이 있다고 항상 NFS인 것은 아니며, remote node B에서 실행할 때 host NFS를 쓴다.

Private overwrite는 이미 초기화된 guest-private 4MiB mapping과 4MiB tmpfs를 덮어쓴다. Canonical cache frame의 첫 host CoW를 독립 측정한 항목이 아니다. 매 sample 후 다른 두 VM의 전체 payload와 private 상태를 검사했다. Guest cache hit에서는 virtio-blk read와 hint callback을 실행하지 않는다. [소스 검토](source-review.json)는 가능한 추가 동작을 설명하지만 timing 차이의 원인을 확정하는 자료는 아니다.

## 실제 데이터 frame 공유

Guest의 file offset→GPA, 실제 VMM의 RAM mapping→HVA, `/proc/PID/pagemap`의 PFN을 연결했다. Original RAM mapping과 process start time을 확인하고, 같은 L1 host에서 세 VM을 모두 정지한 겹치는 구간에 수집한다. RSS/PSS나 과거 read hint만으로 공유를 추정하지 않는다.

| 조건 | 세 VM의 현재 cache 합계 | 고유 PFN의 byte 수 | 세 VM이 같은 file offset에서 보는 공통 frame |
|---|---:|---:|---:|
| Stock 독립 boot | 480MiB | 480MiB | 0 |
| Patched DRAM, hint 비활성 | 480MiB | 480MiB | 0 |
| Patched CXL, hint만 활성 | 480MiB | 480MiB | 0 |
| Patched CXL, pooling | 480MiB | 160MiB | 160MiB |
| Stock 동일 warm snapshot clone | 480MiB | 160MiB | 160MiB |

네 주요 군은 initial/final 모두 같은 수치를 확인했다. PFN은 **L1 host의 frame 번호**다. 물리 host 전체의 memory accounting과 다르며, DAX reservation·canonical image·host image cache를 합한 순 사용량도 아니다. Metadata는 VM별로 유지한다.

## Stock이 이미 하는 것: snapshot clone과 NFS

한 stock donor를 warm 상태로 full snapshot하고 종료·fence했다. 서로 다른 stock VMM process 3개가 같은 RAM 파일 inode를 `MAP_PRIVATE`로 복원했다. Resume 이후에도 현재 file cache 160MiB 전체가 공통 PFN을 사용했다. 초기 guest boot ID·PID·cookie가 같은 것은 같은 snapshot을 복제했기 때문이다. 한 clone의 private write 후 두 peer의 private 상태와 전체 payload는 유지됐다.

이어 한 clone을 full snapshot하고 source를 종료·fence한 뒤 A/DRAM→B/DRAM으로 전송·복원했다. State와 RAM은 수신 후 원본과 byte 비교했다. Image는 B host의 NFS 경로로 열었다. 복원 직후 hot160MiB 검사에서 block read는 0, 강제 guest-cold refill에서는 160MiB를 읽고 host NFS READ RPC가 1,285 증가했다. [독립 검증](stock-snapshot-audit.json)은 152개 원본 receipt, 19개 guest 명령과 10개 변조 거절을 포함한다.

이는 stock의 기능 증명이다. Clone의 반복 성능을 네 군과 비교한 실험이나, 다른 host의 NFS RAM mapping이 같은 물리 frame을 공유한다는 증거가 아니다. 새로운 동일 데이터가 독립적으로 생성된 VM을 자동으로 연결하는 기능도 아니다.

## Admission 비용과 기존 handoff 구분

Pooling의 33개 control event에서 `PoolCache` API 실행 시간 합의 중앙값은 860.80ms, controller 전체 관측 시간의 중앙값은 5,273.35ms였다. 전자는 raw API duration을 합한 값, 후자는 실험의 plan·receipt 전달·제어를 포함한다. 두 값 모두 **이 paused admission 실험의 비용이며 기존 direct handoff latency가 아니다.** 모든 promotion/demotion·handoff가 이 수치라는 해석은 잘못이다.

현 구현은 guest가 명령 하나를 완료한 다음 pause하여 admission한다. Full image canonical copy를 미리 채우고 재사용하지 않으며, exact owner index는 controller의 private 관측 사본이다. Bloom은 approximate candidate hint이고, guest의 정상 cache-hit마다 다른 VM metadata를 조회하는 경로는 없다. Canonical frame으로 remap한 뒤에는 guest의 기존 lookup을 따른다.

## 기존 lifecycle 결과와 남은 범위

이전 [lifecycle 실험](../pool-20261030/README.md)은 3개 sandbox ×160MiB에서 Sync/Async 총 30회 promotion·cross-host 이전·demotion, 321개 guest 명령을 통과했다. Async 현재-cache PFN 검증, stale hint 거절, private write·CoW, NFS refill, demotion 후 이전 mutable CXL RAM poison을 포함한다. 이번 비교가 그 30회를 다시 실행한 것은 아니다.

아직 완료하지 않은 것은 원래 DAX frame 회수·재할당, private CoW page의 CXL 배치, online donor export/admission, shared writable image, 실제 비일관 CXL publish/invalidate와 frame 수명 관리다. 이번 pooling 이전은 full RAM snapshot이고 overlay가 있는 state-only DAX snapshot은 거절한다. Scattered backing을 보존하는 direct handoff와 PML/MPTCP 통합도 남았다. 임의의 guest 명령 실행 중 migration, 세 VM의 동시 지속 read/write stress, 실제 agent RL trace 성능을 검증한 결과는 아니다.

## 검증 자료와 재현

[primary-audit.json](primary-audit.json)은 frozen source·실제 binary/env·raw API/guest JSON·cache GPA/PFN·현재 residency·private 상태·타이밍을 교차 확인한다. [verify_pool_compare.py](../../verify_pool_compare.py), 독립 구현 [verify_stock_snapshot.py](../../verify_stock_snapshot.py)를 사용한다. 각 주요 군에서 false success, sample 누락, 시간·공유량·CoW count·binary·receipt 변조 등 8개를 거절하고 stock clone 검증기는 별도 10개를 거절한다.

네 군과 tail repeat의 총 75개 sample set, stock clone 기능 검사를 합쳐 guest 명령 709개와 원본 receipt 5,011개를 확인했다. 각 sample set은 네 timed operation을 포함한다. Tail의 8개 변조 검사까지 합쳐 총 50개를 거절했다. 이는 프로그램이 만들어낸 증거의 내부 일관성·정확성을 검사하는 것으로, 악의적인 측정기 자체에 대한 보안 증명은 아니다. [summary.json](summary.json)은 주요 수치와 한계를 기계가 읽는 형태로 모은다. [검증기 독립 소스 리뷰](primary-verifier-source-review.json)도 보존한다.

[PFN helper 검사](pfn-helper-unit.json), [주소 연결 검사](mapping-unit.json), [guest helper host smoke](guest-probe-smoke.json), [harness 검토](harness-review.json)도 보존한다. Host smoke는 microVM 성능 자료가 아니다. 준비 단계의 두 중단·실패와 원본 로그는 [failures/history.json](failures/history.json)에 있다. V1은 source provenance 고정을 위한 재시작, V2는 BusyBox `taskset -c` 미지원에 따른 sandbox boot 전 실패였으며 유효한 timing sample로 포함하지 않았다.

최종 [cleanup.json](cleanup.json)은 6개 formal session의 QEMU process incarnation 12개가 종료됐고 소유한 control socket·임시 shared backing이 제거됐음을 확인한다. [primary-run.log](primary-run.log), [tail-stock-run.log](tail-stock-run.log)에 wrapper 완료와 stop 기록을 보존한다.

원본 report·receipts·measured source는 [evidence/REPLAY.md](evidence/REPLAY.md)의 명령으로 추출하고 검증할 수 있다. Archive에는 4GiB outer disk image를 넣지 않는다. Raw evidence의 byte hash를 보존하며 source report를 요약본으로 대체하지 않는다. 실제 배포 archive를 새 경로에 추출한 후 [primary 재검증](archive-replay-primary-audit.json), [tail 재검증](archive-replay-tail-audit.json)도 통과했고, 원래 positive audit의 모든 필드와 같았다. [Packaging 기록](packaging.json)은 원본 5,101개 파일·2.29GB와 7개 archive의 byte/hash 검사를 포함한다. [manifest.json](manifest.json)은 배포 결과 파일을, [evidence/metadata.json](evidence/metadata.json)은 각 archive와 원본 member를 식별한다.

새 실험은 repository root에서 다음과 같이 준비한다. `POOL_BASE_ASSETS`, `STOCK_FC`, `POOL_QEMU`, `POOL_QEMU_ROOT`는 각각 [기존 fixture 절차](../../../../docs/cxl-data-pooling.md), [stock build 명령](stock-build.json), 이 lab의 CXL QEMU·firmware 경로로 설정한다. `/dev/kvm`과 lab network setup 권한 및 충분한 CPU·RAM이 필요하다. Output/assets 디렉터리는 새 경로를 사용한다.

```sh
cd implementation/virtual_machine
gcc -std=c11 -O2 -static -Wall -Wextra -Werror pool_compare_probe.c -o /tmp/pool-compare-probe
gcc -std=c11 -O2 -static -Wall -Wextra -Werror pool_compare_pfn.c -o /tmp/pool-compare-pfn
python3 pool_compare_prepare.py --base-assets "$POOL_BASE_ASSETS" \
  --assets /tmp/pool-comparison-assets --stock "$STOCK_FC"
python3 -u pool_compare_run.py --assets /tmp/pool-comparison-assets \
  --qemu "$POOL_QEMU" --qemu-root "$POOL_QEMU_ROOT" \
  --session poolcomparison --rounds 5 --output /tmp/pool-comparison-run
python3 verify_pool_compare.py /tmp/pool-comparison-run/report.json \
  --negative-tests --out /tmp/pool-comparison-audit.json
python3 verify_stock_snapshot.py /tmp/pool-comparison-run/stock-snapshot/report.json \
  --negative-tests --out /tmp/stock-snapshot-audit.json
```

Helper source·harness·fixture는 모든 군에서 고정했고, VMM binary는 각 군에 지정한 stock 또는 patched hash로 검증했다. Stock 반복은 같은 실행 명령에 `--arms stock-dram`과 별도 session/output을 준다. 단일 군 결과는 suite 전체 대신 그 안의 `stock-dram/report.json`을 검증기에 전달한다.
