# Direct handoff의 API 비용과 과거 1ms 미만 결과

이 문서는 매번 목적지 VMM을 생성하던 경로의 분석이다. 후속 [resident direct 구현](cxl-resident-direct.md)은 목적지 FD·io_uring을 미리 준비하고 왕복 때 실행 환경을 재사용한다. 아래 수치는 당시 조건을 그대로 보존한다.

## 측정 범위부터 구분하기

직전 Gin 결과의 **11.66ms**는 `DirectExport`와 목적지 `InstanceStart`의 HTTP API 시간을 더한 값이다. **2.48ms**는 그 안에 포함된 KVM detach/attach 시간이다. 두 수치는 2회 handoff의 중앙값이었다.

API에는 source 장치 정리, mapper 봉인, 작은 장치 상태 파일 기록, 목적지 mapper 연결, KVM/장치의 host-local wrapper 생성, vCPU 스레드 시작이 포함된다. 현재 목적지는 매번 새 Firecracker 프로세스다. CPU·VMCS·EPT 객체는 유지하지만 host-local 실행 환경까지 재사용하지는 않는다.

이 API 수치에는 pause, source fence, 프로세스 실행, 호스트 사이 metadata 전송, resume, guest ACK가 **포함되지 않는다**. 따라서 서비스 중단 시간 전체로 읽으면 안 된다. Promotion/demotion도 별도로 측정한다.

직전 두 원시 표본은 다음과 같다. 여기서는 중앙값끼리 추측하지 않고 각 표본에서 직접 뺀다.

| 방향 | Export API | Import API | Kernel detach | Kernel attach | API에서 kernel을 뺀 시간 |
|---|---:|---:|---:|---:|---:|
| A→B | 2.777ms | 10.620ms | 0.819ms | 1.335ms | 11.243ms |
| B→A | 2.258ms | 7.671ms | 0.203ms | 2.602ms | 7.124ms |

이전 실행에는 세부 타이머가 없으므로 과거 9.18ms의 세부 항목을 사후에 정확하게 복원할 수는 없다. 같은 코드·이미지·VM 구성으로 계측을 추가하고 반복했다.

## 이번에 추가한 계측

`--restore-timing`을 켜면 `FC_RESTORE_TIMING=1`을 VMM에 전달한다. 평상시에는 비활성이다.

- Export: 사전 검사/파일 열기, I/O drain와 장치 상태 수집, mapper seal, manifest 읽기, ioeventfd 해제, IRQFD/export/detach ioctl, token 읽기, 직렬화, write, sync, 영수증 로그.
- Import: 장치 상태/manifest 읽기와 digest 검증, mapper import, KVM capability 확인, kernel attach와 VM wrapper, memory/vCPU wrapper, 장치 복원, vCPU 스레드 시작, 이벤트 등록.
- 디스크 복원 내부: open, 크기 조회, ID 구성, pool identity, Async io_uring 생성.
- API thread: 요청 파싱, VMM 응답 대기, 응답 구성. 남은 client socket/스케줄링/로그 구간은 잔여 시간으로 명시한다.
- Kernel: 기존 8개 detach/8개 attach 단계 타이머를 켜고, API가 끝난 뒤 예약된 진단 영역 128바이트만 읽는다. guest RAM을 읽는 기능이 아니다.

`analyze_direct_profile.py`는 원시 영수증 SHA-256, API 응답, VMM 로그를 확인하고 표본별 합을 검증한다. Kernel 시간은 부모 stage에서 빼서 이중 계산을 막는다. 세부 시간의 **평균**은 합산할 수 있지만, 각 항목의 중앙값을 더해서 전체 중앙값으로 취급하면 안 된다. 계측 로그 자체의 시간도 residual에 남는다.

## 기준 실행에서 확인된 병목

2GiB/1vCPU Gin sandbox, 원래 27개 명령, 4회 추가 왕복으로 총 10회 handoff를 수행했다. 최초 attach와 workload 실행 뒤 복귀도 제외하지 않았다. API 중앙값 **11.882ms**, 평균 **11.471ms**, kernel 중앙값 **1.415ms**였다.

| 겹치지 않는 구간 | 평균 |
|---|---:|
| 목적지 장치 복원 | 3.385ms |
| Kernel detach + attach | 1.698ms |
| 목적지 metadata 검증 + mapper import | 1.661ms |
| API 왕복 및 마지막 계측 로그 등 | 2.141ms |
| 나머지 source 정리, wrapper/스레드 준비, 기록 | 2.586ms |
| 합계 | 11.471ms |

장치 복원의 큰 차이는 host NFS 경로였다. B의 disk 준비에서는 평균 open **2.763ms**, 크기 조회 **0.591ms**, ID용 metadata 조회 **0.566ms**가 발생했다. 로컬 디스크인 A에서는 각각 **0.024ms**, **0.002ms**, **0.009ms**였다. Async engine 자체는 양쪽 약 0.2ms였다.

이 실행은 `actimeo=0`이다. NFS attribute cache를 꺼 둔 상태에서 경로 open, `SEEK_END`, `fstat`이 여러 번 metadata를 재검증했다. Guest page cache를 전수 조사하는 비용이 아니다.

후속 코드 최적화 실행에서 API 잔여 구간도 다시 나눴다. 두 API를 합친 평균은 요청 파싱 **0.208ms**, 응답 구성 **0.084ms**, VMM dispatch/응답 대기와 마지막 main-thread 계측 출력 **0.715ms**, client Unix socket 왕복과 API-thread 계측 출력 **1.226ms**였다. 마지막 두 구간의 스케줄링과 로그 시간을 따로 추정해 빼지는 않았다. Serial controller의 명령 전달 시간은 이 client-side API 타이머 밖이다.

## 적용한 변경과 안전 조건

1. **Regular image의 metadata 조회를 합친다.** `fstat` 결과 하나로 파일 크기와 virtio disk ID를 구성한다. Raw block device는 `st_size`가 용량이 아니므로 기존 `SEEK_END` 경로를 유지한다. Disk ID는 기존 update 경로와 같은지 테스트한다.
2. **Direct I/O의 IRQFD를 묶어서 해제한다.** 기존 bounded kernel ioctl을 사용해 SRCU 동기화를 한 번 수행하고 각 IRQFD의 pending injection 완료를 기다린다. 지원하지 않는 IRQFD 집합은 변경 전에 거절되므로 기존 개별 해제 경로로 돌아간다. 완료 대기나 source fencing은 생략하지 않는다.
3. **Immutable lower에 한해 NFS attribute cache를 허용한다.** `--immutable-nfs-cache`는 image 파일이 있는 A의 ext4를 먼저 read-only로 remount하고, B를 `actimeo=60,nocto`로 mount한다. 시작 전 image SHA-256과 종료 시 canonical/원본 검사는 유지한다. 읽기 전용 원본 mount와 실제 NFS 옵션을 독립 verifier가 검사한다. Writable 파일이나 실행 중 image를 교체하는 경로에 이 옵션을 적용하지 않는다.

세 번째 변경은 host NFS metadata 캐싱이다. 다른 microVM의 guest inode/xarray/refcount를 공유하는 설계가 아니다. Guest metadata는 여전히 VM별 private이다.

## 변경 후 비교

각 조건에서 최초 방문을 포함한 handoff 10회를 측정했다. 코드 변경만 적용하면 API 중앙값은 **11.882 → 10.861ms**, immutable NFS 캐시까지 적용하면 **8.986ms**였다. 이 세 값은 상세 계측을 켠 실행이다. 같은 최종 설정에서 상세 계측을 끈 별도 10회 실행은 **8.339ms 중앙값**, 평균 **8.655ms**였다. 두 실행의 차이를 전부 계측 비용으로 단정하지 않는다.

IRQFD/export ioctl 구간에서 kernel detach를 뺀 평균은 **0.418 → 0.136ms**였다. 목적지 장치 복원 평균은 **3.385 → 2.847 → 1.515ms**였다. NFS 캐시 조건의 B에서는 warm open이 **30~50µs**였고, 최초 접근까지 포함한 open 평균은 **0.315ms**였다. 최초 접근을 빼서 평균 개선을 과장하지 않았다.

Kernel 코드는 이번에 바꾸지 않았다. 기존 세부 타이머로 확인한 코드 최적화 실행의 kernel 합계 평균은 **1.749ms**, 그 중 attach의 architecture rebind 단계가 **1.425ms**였다. 이 단계에는 nested EPT root 동기화와 VMX/clock 등의 연결이 함께 들어간다. 1.425ms 전부가 root 동기화 단독 비용이라고 단정할 수는 없다. Kernel 시간은 첫 방문, 새 EPT 노드, 실제 실행 내용과 스케줄링에 따라 변하므로 직전 2회 중앙값 2.48ms와 곧바로 성능 개선율을 계산하지 않는다.

현재 남은 비용은 새 프로세스의 metadata/mapper 연결, 장치 이벤트와 IRQ 설정, wrapper/스레드 준비, architecture attach, API dispatch다. 필요한 소유권 검증과 I/O drain을 유지했다. 아직 1ms 미만 handoff를 복원한 것은 아니다. 각 조건이 한 번의 순차 trial이므로 작은 차이나 p95를 안정적인 회귀 판정으로 해석하지 않는다.

## 예전 1ms 미만은 무엇이 달랐는가

2026-10-20의 2GiB direct 실험에도 **0.336472ms 중앙값**이 있다. 4회 warmup 뒤 20회 측정했고, pause부터 첫 guest heartbeat ACK까지 포함했다. Kernel attach 중앙값은 **0.045194ms**였다. 128MiB만 작아서 얻은 결과가 아니다.

그 실험은 고정된 contiguous DAX RAM, 미리 준비된 executor/FD, 고정 CPU 배치를 사용했다. Firecracker의 control/device runtime은 A에 남았고 block/NFS 장치를 다른 호스트의 VMM에 복원하지 않았다. Dense working-set 검사 시간은 첫 ACK 뒤 별도 구간이었다.

현재 pooling 경로는 scattered/CoW mapper, 소유권 proof, RO virtio-blk와 reporting balloon, 목적지의 새 VMM/장치/스레드가 추가됐다. Nested 실험에서는 다른 L1에서 변경한 EPT를 outer KVM에 알리는 root 동기화도 수행한다. 이 동기화는 이전 반복 실험에서 실제 stale EPT 오류를 막기 위해 추가한 것으로, 확인 없이 제거할 수 없다.

과거 수치를 다시 목표로 삼으려면 목적지의 host-local 실행 환경을 미리 준비하고 왕복 때 재사용하는 경로가 필요하다. 비용을 API 바깥으로만 옮긴 결과와 실제 pause→guest ACK가 줄어든 결과를 구분해서 검증해야 한다. 새 소유권마다 바뀌는 queue 상태와 kernel binding은 재검사해야 한다.

## 재현과 증거

증거와 최종 수치는 [실험 결과](../implementation/virtual_machine/results/direct-api-20261102/README.md)에 기록한다. 코드 최적화만 적용한 실행과 immutable NFS 캐시까지 켠 실행을 분리한다. 한 coherent host의 nested KVM 실험이며, 실제 여러 호스트 CXL의 coherence/지연 결과는 아니다.

```sh
python3 implementation/virtual_machine/swe_pool_run.py \
  --assets /absolute/path/to/prepared-assets \
  --qemu /absolute/path/to/qemu-system-x86_64 \
  --qemu-root /absolute/path/to/qemu-root \
  --names gin-gonic__gin-3741 --modes direct \
  --restore-timing --direct-profile-roundtrips 4 \
  --immutable-nfs-cache --session unique-session \
  --output /tmp/unique-direct-profile

python3 implementation/virtual_machine/analyze_direct_profile.py \
  /tmp/unique-direct-profile/gin-gonic__gin-3741-direct/report.json \
  --output /tmp/unique-direct-profile/attribution.json
```

追加 왕복은 첫 promotion 직후 같은 명령 경계에서 수행한다. 매회 boot/PID, page-cache read I/O, retained VM/vCPU/VMCS/EPT identity, generation, mapper handle, source fence를 확인한다. 원래 명령의 결과와 제출 diff도 독립 재검증한다.
