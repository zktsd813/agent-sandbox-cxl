# Resident direct: 목적지 준비, 실행 환경 재사용, 왕복 검증

구현 설명은 [목적지 FD·장치 실행 환경 재사용](../../../../docs/cxl-resident-direct.md)을 따른다. 이 디렉터리는 2026-11-02의 실험 원자료와 독립 검증 결과다.

## 최종 실행

Gin SWE trace의 원래 27개 명령, 1vCPU·2GiB sandbox, A의 read-only image와 B의 host NFS를 사용했다. Guest의 NFS mount가 아니다. Guest는 같은 virtio-blk 장치를 계속 사용한다.

전환은 trace 명령 경계에서 수행했다. 지속적인 I/O 제출 중 임의 시점에 중단하는 별도 stress와 구분한다.

**24회 handoff, promotion 2회, demotion 2회**를 완료했다. 22회는 TCP 제어 경로이고, 2회는 A/B에서 잘못된 import를 시험하는 serial 진단 경로다. 두 번째 CXL 할당에서도 A→B→A를 수행했다. Handoff 직전 guest `Cached`는 **426.64~441.91MiB**였다. 이는 guest 전체 cache 크기이며 canonical로 공유한 lower data 크기와 다르다.

| 시간 범위 | 표본 | 중앙값 | 평균 | 최소~최대 |
|---|---:|---:|---:|---:|
| 최초 B 준비 API | 1 | 3.533ms | 3.533ms | — |
| 재방문 준비 API: 접근 차단 probe | 23 | 0.857ms | 0.852ms | 0.513~1.118ms |
| TCP 연결 준비, pause 이전 | 22 | 0.726ms | 0.936ms | 0.479~3.124ms |
| Export+Import API | 24 | **5.316ms** | 6.723ms | 4.811~26.898ms |
| TCP pause→guest 응답 | 22 | **24.153ms** | 24.841ms | 13.080~46.266ms |
| 위 구간의 guest serial ping 응답 부분 | 22 | 14.959ms | 14.501ms | 4.329~17.131ms |
| 위 구간에서 ping 시간을 뺀 나머지 제어·전송 | 22 | 9.035ms | 10.340ms | 7.991~30.827ms |

최초 B 준비 API 내부의 실제 VMM 작업은 **2.372ms**였다. 프로세스 실행과 serial 시험 제어까지 포함한 준비 controller 시간은 **763.966ms**, A/B TCP 서버 시작은 별도로 **309.647ms**였다. 이 준비 동안 source는 실행 중이다. 이 숫자를 숨기고 준비 비용을 0으로 취급하지 않는다.

최초 handoff는 API **9.644ms**, pause→guest 응답 **31.149ms**였다. 첫 방문과 느린 표본을 모두 포함했다. 최대 지연 표본의 kernel detach+attach는 **22.505ms**로, 이 outlier도 남겼다. 작은 표본과 단일 nested 환경의 결과이므로 안정적인 p95나 보편적인 개선율을 주장하지 않는다.

Ping 시간을 뺀 값은 계측된 전체에서 해당 구간을 뺀 값이다. Guest의 첫 명령 실행 시각을 직접 관측한 시간이 아니다. 각 구간의 중앙값을 더하면 전체 중앙값과 일치하지 않는다. `serial-diagnostic` 경로의 거부 시험·영수증 수집에 걸린 수초도 별도로 남아 있으며 TCP 지연에 섞지 않았다.

## 기존 방식과 비교

같은 최종 Firecracker 바이너리·kernel·image·27개 SWE 명령에서 resident 옵션을 끈 기존 경로도 별도로 실행했다. **10회 handoff와 두 번씩 promotion/demotion**, 독립 suite 검증을 통과했고 명령 return code·테스트·제출 결과가 resident 실행과 같았다.

| Export+Import API | 표본 | 중앙값 | 평균 | 최소~최대 |
|---|---:|---:|---:|---:|
| 매번 새 목적지 VMM | 10 | 9.190ms | 9.445ms | 8.228~11.978ms |
| 준비한 resident VMM 재사용 | 24 | 5.316ms | 6.723ms | 4.811~26.898ms |

관측한 API 중앙값은 약 **42.1% 감소**했다. 순차 단일 trial이고 표본 수와 제어 client도 다르므로, 감소분 전부를 disk FD 재사용 하나의 효과로 분해한 수치는 아니다. 기존 경로의 serial 진단 wall time과 새 TCP pause→ACK를 직접 성능비로 나누지 않는다. 실제 동일 FD·io_uring·vCPU thread 유지 여부는 별도의 `/proc` 증거로 확인했다.

## 무엇을 검증했나

- 원래 SWE 명령 27개의 출력, 테스트 결과와 제출 diff, boot ID/PID, 열린 뒤 unlink한 파일의 inode·offset 연속성을 확인했다.
- 실제 parked RAM 읽기와 `KVM_GET_REGS` 접근 probe **26개**에서 각각 `EFAULT`와 `EIO`를 확인했다.
- Parked 작업 거부 **52건**, stale generation·다른 graph·잘못된 manifest digest **각 2건**을 확인했다.
- A의 활성 FD는 **45개**, B는 첫 활성 **45개 → 이후 46개로 고정**됐다. Disk FD와 io_uring FD, vCPU TID는 각 host에서 유지됐다. B의 추가 1개 FD는 초기 guest-memory wrapper가 보유하는 CPM FD이며 왕복마다 증가하지 않는다.
- Retained VM/vCPU/VMCS/EPT identity, allocation generation, owner epoch와 mapper handle을 확인했다. Handoff 중 `full_scan_pages` 증가는 없고 RAM·CPU snapshot payload도 0이다.
- 공유 원본 image/canonical 데이터 검증과 최종 VMM/TCP controller/outer VM 정리를 확인했다. 전체 이미지 비교는 시험 종료 검증이며 timed handoff에 포함하지 않는다.
- 증거 파일을 바꾸고 SHA-256도 다시 계산한 **8개 의미 검증 부정 시험**을 모두 거절했다. RAM 접근 허용, KVM 접근 허용, probe 누락, FD/thread 재생성, 잘못된 negative case, 준비 위치 변경, 측정 시간 축소를 포함한다.
- Release build, direct Rust unit test **5개**, handoff Python test **9개**, C의 `-Wall -Wextra -Werror -static` 빌드와 shell 문법 검사를 통과했다. 배포 patch를 upstream에 다시 적용한 **34개 파일**이 실제 build source와 일치한다.

최종 실행 자체는 `passed`였다. 최초 suite의 사후 검증은 TCP controller 종료 영수증의 CRLF를 정규식에서 허용하지 않아 `failed`로 기록됐다. 원자료의 이 값을 수정하지 않았다. 검증기의 줄 끝 처리만 수정한 뒤 [최종 독립 suite 검증](final-suite-verification.json)이 통과했고, 부정 시험도 통과했다.

## 보관 자료

| 파일 | 내용 |
|---|---|
| [summary.json](summary.json) | 최종 범위별 시간, FD 유지, 검증 건수 |
| [final-attribution.json](final-attribution.json) | 최종 24회 개별 표본과 TCP/serial 별 집계 |
| [final.tar.xz](final.tar.xz) | 최종 report·영수증·전체 VMM/guest 로그·당시 source 사본 |
| [final-suite-verification.json](final-suite-verification.json) | 원자료의 독립 재검증 |
| [evidence-negative-tests.json](evidence-negative-tests.json) | SHA를 다시 계산한 8개 증거 변조 거부 |
| [fresh.tar.xz](fresh.tar.xz) | 같은 바이너리의 기존 새-VMM 경로 10회 비교·회귀 실행 |
| [fresh-suite-verification.json](fresh-suite-verification.json) | 기존 경로의 독립 검증 |
| [diagnostic.tar.xz](diagnostic.tar.xz) | 앞선 상세 계측 실행, 12회 handoff·2회씩 promotion/demotion |
| [diagnostic-attribution.json](diagnostic-attribution.json) | 상세 계측의 배타적 단계 시간 |
| [build-inputs.json](build-inputs.json) | 실제 바이너리·kernel·guest helper·image hash |
| [patch-reproduction.json](patch-reproduction.json) | upstream에서 patch 재현 및 source 일치 |
| [development-runs.json](development-runs.json) | 개발 중 실패·deferred run 기록 |
| [post-verification-cleanup.json](post-verification-cleanup.json) | 독립 검증 이후 보관했던 최종 trial의 private disk 정리 |

상세 계측 실행의 재방문 장치 복원 평균은 약 **0.572ms**였다. Source `PROT_NONE` 교체는 중앙값 약 **0.130ms**, workload 후 최대 약 **6.399ms**였다. 이는 기존 host PTE를 철회하는 비용이며 guest 데이터의 전수 비교가 아니다. 이 진단 실행은 최종 attach-return identity 검사 보강 이전 바이너리이므로 최종 no-profile 성능과 같은 표본으로 합치지 않는다.

개발 중에는 timestamp가 붙은 로그 파싱, ACK의 CRLF, 제어 서버 listener의 30초 idle timeout을 수정했다. 마지막 timeout 수정 후 최종 실행에서 전체 workload·demotion을 기다렸다가 다시 promotion한 뒤에도 제어 서버가 살아 있는 것을 확인했다. 실패 실행의 표본을 최종 성능에 섞지 않았다.

## 재현

기존 SWE fixture 준비는 [SWE pooling 문서](../../../../docs/cxl-swe-pool-traces.md)를 따른다. Kernel/CPM 모듈은 이번에 변경하지 않았다. 이번 Firecracker patch를 적용한 release executable과 기본 fixture로 새 fixture를 만든다. 기존 fixture는 덮어쓰지 않는다.

```sh
python3 implementation/virtual_machine/resident_prepare.py \
  --base-assets /absolute/path/to/base-swe-fixture \
  --firecracker /absolute/path/to/patched-firecracker \
  --output /absolute/path/to/new-resident-fixture

python3 -u implementation/virtual_machine/swe_pool_run.py \
  --assets /absolute/path/to/new-resident-fixture \
  --qemu /absolute/path/to/qemu-system-x86_64 \
  --qemu-root /absolute/path/to/qemu-root \
  --session unique-resident-session \
  --names gin-gonic__gin-3741 --modes direct \
  --resident-direct --resident-fast-control --immutable-nfs-cache \
  --direct-profile-roundtrips 10 --output /tmp/unique-resident-run

python3 implementation/virtual_machine/verify_swe_pool.py \
  --suite /tmp/unique-resident-run/report.json --output /tmp/resident-verification.json

python3 implementation/virtual_machine/analyze_resident_direct.py \
  /tmp/unique-resident-run/gin-gonic__gin-3741-direct/report.json \
  --output /tmp/resident-attribution.json

python3 implementation/virtual_machine/test_resident_evidence.py \
  /tmp/unique-resident-run/gin-gonic__gin-3741-direct/report.json \
  --output /tmp/resident-negative-tests.json
```

상세 단계 계측은 `--restore-timing`을 추가한다. 준비·handoff·promotion/demotion의 범위를 유지하고 최초 방문을 포함한다. 이 결과는 coherent 단일 물리 호스트의 nested KVM 결과이며 실제 비일관 CXL 다중 host 성능을 의미하지 않는다.
