# Direct API 비용 분해와 최적화 — 2026-11-02

**네 조건 모두 실행·독립 검증을 통과했다.** 동일 Gin trace의 2GiB/1vCPU sandbox에서 조건별 10회 direct handoff, promotion/demotion 각각 2회, 원래 27개 명령을 수행했다. 총 40회 handoff, 8회 promotion, 8회 demotion, 108개 명령이다. 매회 source fence, guest identity/cache 유지, 동일 KVM/VMCS/EPT, mapper 소유권/세대, snapshot/RAM 복사 없음, 최종 제출 결과를 검사했다. 종료 시 canonical/원본 image 무결성과 모든 private grant 반환 및 실험 자원 회수도 검사했다.

## 결과

API는 **DirectExport + InstanceStart**다. Kernel은 그 안의 detach + attach다. Pause, resume, source fence, process exec, metadata 전송, guest ACK는 API에 포함하지 않는다. Promotion/demotion도 합치지 않는다.

| 조건 | 상세 계측 | API p50 | API 평균 | Kernel p50 | 표본 |
|---|---|---:|---:|---:|---:|
| baseline: 이전 코드 + actimeo=0 | 켬 | 11.882ms | 11.471ms | 1.415ms | 10 |
| codeopt: metadata 조회 통합 + IRQFD batch | 켬 | 10.861ms | 10.836ms | 1.599ms | 10 |
| cached: 코드 변경 + immutable NFS 캐시 | 켬 | 8.986ms | 9.308ms | 1.565ms | 10 |
| final: 최종 설정 | 끔 | **8.339ms** | 8.655ms | 1.472ms | 10 |

계측을 켠 조건끼리의 API 중앙값 감소는 **24.38%**다. 상세 계측을 끈 최종 수치는 별도 실행이다. 네 조건은 순차적인 단일 trial이며 randomized paired 실험이 아니다. 모든 최초 방문과 마지막 workload 후 복귀를 포함했다. 10표본의 p95는 안정적인 tail 추정치로 주장하지 않는다. 실험은 한 물리 호스트의 coherent QEMU CXL + nested KVM이며 실제 multi-host CXL 수치가 아니다.

직전 11.663ms/2.479ms 수치는 2표본이었다. 이번 baseline으로 API 수준은 재현되지만 kernel 중앙값은 변동한다. Kernel은 이번에 수정하지 않았다. 전체 수치·방향별 결과·원시 표본은 [summary.json](summary.json), [baseline attribution](baseline-attribution.json), [codeopt attribution](codeopt-attribution.json), [cached attribution](cached-attribution.json)에 있다.

## 나머지 시간은 어디에 쓰였는가

Baseline의 평균 11.471ms = kernel 1.698ms + 장치 복원 3.385ms + 목적지 metadata/mapper 1.661ms + API dispatch/response와 마지막 계측 로그 2.141ms + 나머지 source 정리/wrapper/스레드/기록 2.586ms다. 평균의 합이며 중앙값을 합산한 표가 아니다.

B의 NFS image open/크기/ID 조회 평균은 2.763/0.591/0.566ms였다. Regular image의 크기와 ID 조회를 하나로 합쳤다. IRQFD는 SRCU 동기화 한 번과 기존 pending injection drain을 유지한다. Immutable NFS 옵션은 source image ext4를 read-only로 remount하고 client에 `actimeo=60,nocto`를 적용한다. Guest metadata를 공유하지 않는다. 실제 RO mount·NFS 옵션도 독립 검증했다. 각 조건의 실제 remote NFS data read는 20/18/20/29회로, data path 자체를 우회하지 않았다.

최적화 후에도 새로운 host-local 장치/wrapper/스레드와 mapper import가 필요하다. 과거 2GiB의 **0.336ms**는 준비된 executor/FD 경로이며 block/NFS 장치의 host-local 복원이 없었다. 현재 1ms 미만을 복원했다고 주장하지 않는다. [설계·계측 상세](../../../../docs/cxl-direct-api-latency.md)와 [과거 증거](historical-reference.json)를 참고한다.

## 검증과 변경 범위

- 모든 handoff의 mapper `full_scan_pages` **증가량 0**. 최초 promotion admission의 524,288페이지 카운터는 유지한다.
- 원래 SWE 명령, return code, 테스트 결과, 최종 제출 diff 일치. Warm-cache 확인 시 block read 0, remote 명령에서는 실제 NFS read 확인.
- virtio-block unit test **41개**, Python authority/allocator protocol test **19개** 통과. 최적화 전과 같은 disk ID를 유지하는 검사 포함.
- Cumulative Firecracker patch를 upstream `95f868c8…`에 새로 적용하여 실제 빌드 소스 **33개 파일**과 byte-for-byte 일치: [patch-reproduction.json](patch-reproduction.json).
- Kernel/guest/mapper 및 image 입력은 동일. VMM 바이너리와 실행 소스 해시는 [build-inputs.json](build-inputs.json), 각 suite report, raw `/proc/PID/exe` 영수증에 보존한다.
- 추가 왕복은 첫 promotion 직후 동일 command 경계에서 수행한다. 독립 verifier는 전체 epoch itinerary와 각 명령 경계까지 검사한다.

## 원시 증거 재검증

`baseline.tar.xz`, `codeopt.tar.xz`, `cached.tar.xz`, `final.tar.xz`에는 raw API/guest/kernel 영수증, VMM/serial 로그, workload report, 원본 trace, command archive, frozen 실행/검증 소스, source/fixture hash가 들어 있다. 큰 guest image와 VM RAM backing은 포함하지 않는다. 압축 파일 해시는 `archives.json`에 있다.

```sh
mkdir -p /tmp/direct-api-recheck
tar -xf final.tar.xz -C /tmp/direct-api-recheck
python3 /tmp/direct-api-recheck/cxl-direct-profile-final/measured-sources/verify_swe_pool.py \
  --suite /tmp/direct-api-recheck/cxl-direct-profile-final/report.json \
  --output /tmp/direct-api-recheck/final-verification.json
```

나머지 세 압축 파일도 같은 방법으로 frozen verifier를 실행한다. `*-suite-verification.json`은 실행 당시 결과이며 `archive-reverification.json`은 압축 후 새 디렉터리에서 다시 확인한 결과다. `analyzer-negative-checks.json`에는 API 수치, kernel 수치, timing raw record, 영수증 해시를 변조했을 때 분석기가 거절하는 확인을 기록한다.

새 실험은 [설계 문서의 재현 명령](../../../../docs/cxl-direct-api-latency.md#재현과-증거)을 사용한다. `--restore-timing`은 opt-in이며, `--immutable-nfs-cache`는 변경되지 않는 read-only lower image에만 적용한다.
