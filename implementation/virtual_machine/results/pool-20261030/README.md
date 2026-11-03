# CXL data-page pooling 검증 — 2026-10-30

**3개 sandbox가 각각 160MiB의 guest file cache를 사용한 상태에서, 총 30회 전환과 321개 guest 명령을 통과했다.** Async 실행에서는 현재 guest file-cache 페이지의 GPA를 직접 확인하여 세 VM이 같은 160MiB의 canonical CXL frame을 읽는 것까지 검증했다. Guest inode·folio·XArray·LRU는 VM별로 유지한다.

구현과 재현 절차는 [설계 문서](../../../../docs/cxl-data-pooling.md), 기계가 읽는 요약은 [summary.json](summary.json)에 있다. 이 결과는 합성 correctness workload의 실측이며 agent RL trace의 실행 성능이나 메모리 절감률을 주장하지 않는다.

## 완료한 실행

| 항목 | Sync | Async |
|---|---:|---:|
| Sandbox / sandbox당 vCPU | 3 / 1 | 3 / 2 |
| Sandbox당 RAM / 파일 cache | 512MiB / 160MiB | 512MiB / 160MiB |
| 전체 왕복 cycle | 2 | 1 |
| Promotion / cross-host handoff / demotion | 6 / 8 / 6 | 3 / 4 / 3 |
| Guest 명령 | 188 | 133 |
| 전체 payload word 검사 | 112회, 17.5GiB | 61회, 9.53125GiB |
| 현재 file-cache GPA→canonical PFN 대응 | 측정하지 않음 | **17회 모두 40,960페이지, 160MiB; 미대응 0** |
| 세 VM의 같은 정지 구간에서 공통 cache PFN | 측정하지 않음 | **3회 모두 160MiB** |
| 원본 receipt | 1,643개 | 1,065개 |
| 실행·사후 검증·cleanup | 통과 | 통과 |

Sync는 `mincore`로 160MiB residency를 확인하고, 과거 virtio-blk hint GPA에서 같은 크기의 공유 PFN을 각각 확인했다. 두 집합이 현재 같은 페이지라는 증거는 없다. **현재 guest cache 공유라는 결론은 보강된 Async 증거에 근거한다.** Async의 측정용 guest helper가 read-only `MAP_SHARED`와 `mlock`으로 페이지를 고정하고 file offset→GPA를 기록한다. 그 GPA를 admission의 HVA, 실제 host PFN과 연결한다. 이 oracle의 GPA는 pooling 후보나 plan 생성에 사용하지 않는다.

각 cycle에서 VM 1·3은 `A/DRAM → A/CXL → B/CXL → A/CXL → A/DRAM`, VM 2는 `A/DRAM → A/CXL → A/DRAM`을 수행한다. 순차 전환 동안 DRAM/CXL VM이 공존하며, 매 전환 후 다른 VM의 데이터도 검사한다. Async의 세 VM 동시 pin/pause 검사 위치는 A/A/A, B/A/B, A/A/A다. 최종적으로 모두 A/DRAM에 복귀했고 guest boot ID, workload PID·cookie와 VM별 private 데이터가 유지됐다.

## 실제로 확인한 실패 조건

- **페이지 재사용과 CoW:** 파일 cache를 eviction하고 224MiB anonymous churn을 수행했다. 세 번의 검사에서 각각 과거 공유 40,960 frame이 private으로 분리됐다. Stale content 후보는 Sync에서 40,960·57,751개, Async에서 40,960개 거절됐다. 두 번째 Sync의 수치는 누적된 hint GPA가 포함된 것이며 현재 cache 크기가 아니다.
- **NFS cold refill:** B에서 160MiB를 다시 읽을 때 host NFS READ가 Sync에서 각각 1,282회, Async에서 1,296회 증가했다. 이후 hot read와 이전 직후 검사에서는 guest block read I/O가 0이었다. Guest는 ext4→virtio-blk를 유지하며 NFS는 host에서 사용한다.
- **Demotion 후 독립성:** 총 9번, 이전 VM의 512MiB mutable CXL RAM 영역을 poison했다. 복귀한 DRAM VM과 peer VM의 전체 데이터 검사를 통과했다. 다른 VM이 사용하는 canonical 영역은 poison하지 않는다.
- **잘못된 admission:** VM ID·epoch·image·정렬·중복·범위 오류를 HTTP 400으로 거절하고, stale hint·content mismatch는 매핑하지 않았다. 실행 중 PoolCache와 overlay가 있는 state-only snapshot도 거절했다. 거절된 snapshot은 기존 sentinel 파일을 보존했다.
- **Bloom 오류:** slot을 0 또는 1로 채워 false negative/false positive 상황을 만들었다. Bloom은 후보 탐색에만 쓰이며 정확한 index와 현재 바이트 확인을 대신하지 않는다. 별도 5개 control unit test가 false negative에서 다른 owner index를 조회하지 않는 것 등을 확인한다.
- **증거 검증기의 실패 감지:** 각 실행에 대해 성공 표시·전환 누락·epoch·private 데이터·residency·공유량·PFN scan·NFS 사용·cleanup·receipt hash를 조작한 10개 입력을 모두 거절했다.

전체 15GiB의 논리 RAM snapshot을 저장했다. State·RAM·hint의 전송 파일을 원본과 byte 비교했고, CXL import 10.5GiB와 demotion poison 4.5GiB의 기록을 남겼다. Read-only canonical image도 전체 byte 비교했다. 앞선 실패 실행 세 개와 수정 사항은 [failures/history.json](failures/history.json)에 별도로 보존한다.

## 검증 자료

| 자료 | 내용 |
|---|---|
| [sync-audit.json](sync-audit.json), [async-audit.json](async-audit.json) | 원본 receipt·source hash, lifecycle, 데이터와 PFN 대응, 변조 입력 거절 |
| [sync-independent-audit.json](sync-independent-audit.json), [async-independent-audit.json](async-independent-audit.json) | 프로젝트 verifier를 import하지 않는 별도 검증 구현의 결과 |
| [audit-tools](audit-tools/README.md) | 원본 evidence 압축 해제와 오프라인 재검증 명령 |
| [sync-package.json](sync-package.json), [async-package.json](async-package.json) | 전체 report·원본 receipt·측정 source archive의 식별과 검증 |
| [archive-replay-audit.json](archive-replay-audit.json) | 배포 archive를 새 경로에 추출하여 Async 증거를 다시 검증한 결과 |
| [firecracker-tests.json](firecracker-tests.json), [firecracker-tests.tar.gz](firecracker-tests.tar.gz) | 최종 source의 13개 targeted regression test와 원본 로그 |
| [control-tests.json](control-tests.json), [control-tests.log](control-tests.log) | 최종 controller의 5개 unit test |
| [workload-unit-report.json](workload-unit-report.json) | Guest oracle의 host 단독 검사; microVM 검증과 구분 |
| [CoW probe](../pool-cow-20261030/summary.json) | 실제 KVM read/write, host write, first-write, fork CoW 및 16회 같은 HVA remap/EPT 갱신 |
| [patch-audit.json](patch-audit.json) | 깨끗한 upstream에 patch 적용 후 빌드 source 24개와 byte 일치 |
| [kernel.config](kernel.config) | NFS client/server와 최종 CXL kernel build 설정 |
| [manifest.json](manifest.json) | 배포한 결과 파일들의 SHA-256 |

최종 Firecracker SHA-256은 `866f5b44b96535c0c525f6cee0a9ee09ff64900fbd83458c51c45b4de3505d08`, L1 kernel은 `1e72c8a58c484dea501f1c2e3fc5a0efe93b46fe1e873e99a25c16154bf19e86`이다. 두 실행은 같은 바이너리를 사용했다. Firecracker upstream baseline은 `95f868c8e345b1cc8faccd1a3c910b4989dc3f58`이며 패치는 기존 migration 변경도 포함한다. 각 실행의 측정 source 사본과 hash를 archive에 보존했으므로 Sync 이후 추가된 cache GPA 검증을 과거 실행에 소급하지 않는다.

## 결과의 범위

1. **물리 frame 공유는 확인했지만 사용 가능 CXL 용량을 늘리지는 않았다.** 원래 VM별 DAX reservation을 allocator에 반환하지 않는다. 세 VM의 160MiB가 같은 frame을 본다는 사실을 순 메모리 320MiB 절감으로 계산하면 안 된다.
2. **이번 이전은 전체 RAM snapshot이다.** Canonical alias와 DRAM CoW의 실제 내용을 보존하기 위한 경로다. Overlay가 있으면 기존 state-only DAX snapshot을 거절한다. Zero-copy/direct handoff와의 통합은 완료하지 않았다.
3. **한 coherent 물리 host의 두 QEMU/KVM 노드에서 검증했다.** 실제 비일관 CXL 다중 host의 publish/invalidate/fence를 검증한 것은 아니다. CoW 새 페이지는 host DRAM이므로 CXL 단계도 hybrid가 될 수 있다.
4. **정지 admission과 immutable image에 한정한다.** Canonical image를 미리 적재하고 실험 동안 재사용하지 않는다. Donor live-page export, 온라인 read 우회, writable image 공유와 frame 회수는 후속 작업이다. Production seccomp 통합도 남아 있으며 실험 Firecracker는 전용 L1에서 `--no-seccomp`를 사용했다.

다음 구현 단계는 원래 frame의 회수·재할당, canonical/private backing manifest를 보존하는 state-only handoff, 비일관 CXL에서의 데이터 publish와 수명 관리다. PML과 MPTCP 통합은 이 결과와 별도 과제다.
