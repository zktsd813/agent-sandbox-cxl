# 기존 이전 모드와 host NFS 파일 경로의 호환성

2026-10-27. 이번 검증의 대상은 **기존 CPU/RAM 이전 모드에 read-only virtio-blk image와 guest tmpfs upper를 연결할 수 있는가**다. NFS는 목적지 host의 backing 접근 경로이며 guest filesystem은 그대로 유지한다.

**판정: 일반 `snapshot` + DAX mailbox는 현재 NFS 파일 경로와 호환된다. Direct와 reuse/prepared의 파일 장치 지원은 아직 완성되지 않았다.** Direct의 빠른 diskless 전환과 파일 workload의 성공은 서로 다른 검증 결과다.

## 모드 구분

크게 두 계열이고, 상태 전달 계열에는 세 구현이 있다. Full RAM snapshot은 별도 비교 기준이다.

| 계열·모드 | CXL에 유지하는 것 | 이전할 때 하는 일 |
|---|---|---|
| 상태 전달: `snapshot` | Guest RAM | CPU·clock·interrupt·장치 상태만 직렬화해 전달. 목적지에서 KVM과 host 자원을 새로 생성 |
| 상태 전달: `reuse` | Guest RAM | 같은 상태를 목적지의 기존 KVM worker에 복원 |
| 상태 전달: `reuse-prepared` | Guest RAM | 목적지 worker의 정리 작업을 미리 수행한 뒤 복원 |
| `direct` | Guest RAM과 지정한 KVM/vCPU/VMCS/EPT 객체 | 같은 객체를 detach/attach해 실행 owner를 전환 |

Direct도 **모든 상태가 CXL에 있는 것은 아니다**. Host FD, eventfd, kernel thread·worker와 device backend의 연결은 각 Linux에 종속된다. 현재 direct Linux 시험은 source Firecracker가 A에 남고 B의 native executor가 KVM을 실행한다. B의 executor는 UART PIO와 HLT를 처리하며 virtio-blk backend를 생성하거나 일반 MMIO를 처리하는 경로가 없다.

## 실제 검증과 판정

아래의 `거부`는 호환 성공이라는 뜻이 아니다. 기존 제한이 실제로 작동하는지 확인한 결과다.

| 시험 | 결과 | 검증 범위 |
|---|---|---|
| 현재 kernel/helper의 diskless direct, 1 vCPU | 20회 A↔B 성공 | 같은 VM/vCPU/EPT, 한 번 부팅, snapshot 호출 0, heap·clock·TSC 오류 0 |
| 기존 native `snapshot` controller, 1 vCPU·diskless | 20회 + warmup 4회 성공 | 실제 DAX mailbox와 fresh KVM restore |
| 기존 native `reuse` controller, 1 vCPU·diskless | 20회 + warmup 4회 성공 | 목적지 process·KVM FD·vCPU thread 유지 |
| 기존 native `reuse-prepared` controller, 1 vCPU·diskless | 20회 + warmup 4회 성공 | 미리 준비한 worker를 사용하고 동일 identity 유지 |
| Direct, 2 vCPU·diskless | Pause/export HTTP 400, errno 95 | Shared owner/generation 변화 없음. Resume 후 출력 counter 재진행 |
| Direct, 1 vCPU + virtio-blk | Pause/export HTTP 400, errno 95 | Owner/generation 변화 없음. **Resume 204 뒤에도 후속 읽기 응답 없음** |
| PrepareReuse, 1 vCPU + virtio-blk | 명시적인 diskless guard로 HTTP 400 | Resume 후 같은 FD로 cold backing read 성공, 데이터 오류 0 |
| PrepareReuse, 2 vCPU·diskless | 명시적인 one-vCPU guard로 HTTP 400 | Resume 후 출력 counter 재진행 |
| `snapshot` + DAX mailbox + 실제 local/NFS trace | **24 trials·840명령·48 handoffs 성공** | 2 vCPU·2 GiB, 같은 guest의 A→B→A, cache 유지/회수 조건 |

정식 trace는 Gin·PHP-CS-Fixer·MicroPython × local/NFS × (cache 유지 3회 + 회수 1회)다.
원본 종료값 816개, 마지막 제출 24개, 비교 가능한 테스트 요약 72개, 최종 patch와 열린 FD가 일치했다.
원본의 nonzero 종료 48건도 기대값으로 재현한 것이며 프로젝트 테스트가 전부 성공했다는 뜻은 아니다.
State는 **25,018–25,106 bytes**, RAM 전달량은 0이며 48회 모두 source/destination state bytes가 같았다.
MicroPython의 알려진 conda stderr 경고 위치만 원본과 달라 이전과 동일하게 지정한 한 줄·출현 횟수만 정규화했다.

Direct의 기존 asset으로도 20회 회귀를 따로 수행했다. 위 표의 direct 성공은 **새 NFS/OverlayFS kernel과 새 helper**를 사용한 결과다. 서로 다른 baseline의 시간을 성능 비교로 사용하지 않는다.

Reuse와 prepared의 실제 negative test는 `PrepareReuse` API를 사용했다. 일반 reuse restore에도 동일한 diskless 검사가 있고, 추가로 output-only UART와 vCPU handle 1개를 요구하는 것은 소스에서 확인했다. 일반 reuse의 block `snapshot/load`까지 실행해 검증했다고 주장하지 않는다.

## 발견한 direct 실패 처리 문제

1 vCPU + block 구성은 전환 요청 전에 cold read에 성공했고 목적지 host NFS image도 읽을 수 있었다. 따라서 NFS export 접근 실패를 측정한 것이 아니다.

실제 source에서는 다음을 관측했다.

```text
정상 파일 읽기
→ Pause/export: HTTP 400, EOPNOTSUPP
→ shared ownership: 변경 없음
→ API가 보고하는 상태: Running
→ Resume: HTTP 204
→ 새 guest 읽기 요청: 제한된 polling 대기 후에도 완료 응답 없음
```

소스에서는 direct pause가 IRQFD를 해제한 뒤 export를 호출한다. Kernel은 여러 vCPU 또는 남아 있는 IRQFD/ioeventfd를 거부한다. Export 실패 시 일부 정지 작업이 이미 수행됐고 정상 rollback이 보장되지 않는다. 이것은 관측 결과를 설명하는 코드상의 원인 후보다. 이번 증거만으로 **UART 입력 전달 실패와 block completion 실패를 구분하지는 못했다**.

이 실패 경로를 이번 변경에서 수정하거나 device guard를 제거하지 않았다. 현재 trace runner는 direct lab 자체를 거부하므로 이 경로를 호환된 모드처럼 선택하지 않는다. Direct 통합 전에 지원 여부를 정지 이전에 검사하거나 실패 시 IRQFD·vCPU 상태를 되돌리는 절차가 필요하다. Counter만 다시 증가한 diskless 사례도 모든 I/O가 복구됐다는 증거로 확대하지 않는다.

초기 harness 실행에서는 musl의 오류 문자열, body 없는 GET 요청, L1 부팅 완료 대기 처리를 보정했다. 그 시도는 결과에 포함하지 않고 archive의 `harness-pilots/`에 보존한다. 위 판정은 수정 후 각각 새 VM pair에서 완료한 시험만 사용한다.

## 상태만 전달하는 경로에 연결한 변경

기존 trace 실험도 `FC_DAX_SNAPSHOT=1`로 RAM 복사 없이 상태만 저장했다. 이번에는 상태 전달을 serial에서 **기존 `handoff_control` DAX mailbox**로 바꾸는 선택지를 추가했다.

- `/dev/dax0.0`의 첫 2 MiB 예약 영역 중 1 MiB 위치에 기존 mailbox를 사용한다. Payload 용량은 256 KiB이며 guest RAM은 2 MiB부터 시작한다.
- Source의 pause·snapshot·종료·incarnation fence 뒤에 상태를 publish한다. 목적지는 nonce·방향·round·schema·RAM 크기·길이·FNV-1a64를 확인한 뒤 자기 tmpfs의 state 파일에 저장한다.
- 목적지 API process는 미리 시작하지만 KVM 생성과 guest RAM 연결은 restore에서 한다. `reuse` worker를 미리 만드는 동작과 구분한다.
- 재개 뒤 source/destination state 파일을 반출해 **원시 byte와 SHA256**을 별도로 비교한다. 이 반출은 검증용이며 restore의 전송 경로가 아니다.
- 새 helper의 regular-file 검사까지 포함해 기존 native controller 세 모드가 회귀 시험을 통과했다.

이 helper는 외부에서 fencing한 단일 publisher와 coherent lab을 전제로 한다. 다중 publisher, 실패 후 mailbox 복구, 비일관 CXL의 flush/invalidate protocol은 구현하지 않았다.

## 시간의 해석

**이 검증은 모드·파일 호환성을 확인한다.** 기존 최적화된 direct의 ms 이하 측정과 trace controller 시간을 같은 지표로 비교하지 않는다.

새 trace의 `controller_handoff_ns`는 pause 요청부터 재개 후 identity 응답까지이며 serial shell 제어 대기가 남아 있다. 목적지 API prestart와 사후 state·log 반출은 제외한다. 이전 [serial trace 실험](../trace-nfs-20261027/README.md)의 9.31초는 로그 수집까지 포함하므로 두 값을 동일 조건의 speedup으로 계산하지 않는다.

Native pause/snapshot/load/resume API와 mailbox publish/receive 시간은 각각 그 작업을 실행한 L1에서 잰다. 두 host의 시각을 빼거나 단계별 중앙값을 합쳐 실제 downtime으로 제시하지 않는다. 최적화된 native controller에 이번 2-vCPU·block trace를 직접 통합한 end-to-end latency는 별도 과제다.

정식 trace 실행 중 **08:15:42.710–08:15:59.182 UTC**에 별도 CPU에서 smoke artifact의 SHA/CPIO 검증이 잘못 겹쳤다. 읽기만 수행했고 VM 상태를 바꾸지는 않았지만, 공유 storage·memory bandwidth·cache 경합 가능성이 있다. 따라서 이 실행은 **호환성·상태 연속성의 근거**로 사용하고, 통제된 성능 비교나 serial 대비 speedup을 주장하지 않는다. 원시 timing과 [겹친 검증 기록](audit-overlap-incident.json)을 보존한다.

## 검증 자료

- **기존 controller:** 3 modes·72 handoffs(본 측정 60 + warmup 12), raw event와 report 일치. Source/destination FNV·길이 및 archive SHA256 대조. 변조 15개 거부. 이 baseline은 매 round state bytes 자체를 보관하지 않아 payload hash를 사후 재계산하지는 못한다.
- **Direct/guard 시험:** legacy/current direct baseline과 4개 제한 사례를 raw API·pool·probe·counter와 대조. Report 변조 32개 거부, parser 검사 12개 통과.
- **Mailbox helper:** 메모리 fixture 25개, 잘못된 CLI 입력 14개 검사. 실제 DAX에서 양방향 전달과 15개 오류 주입을 확인했다. 거부 시 목적지 파일 내용이 변하지 않는지도 검사한다.
- **Trace auditor:** 기존 trace·FD·mount·block/NFS 검증에 두 state의 byte 일치, 2개 mailbox receipt와 4개 API receipt, helper build/source 연결을 추가했다. 실제 증거 변조 **21개**와 독립 fixture **13개**를 모두 거부했다. Direct/reuse로 모드를 잘못 표기하는 경우도 거부하며, 기존 serial archive도 계속 읽을 수 있다.

[정식 trace report](trace-report.json) · [전체 trace audit](trace-audit.json) · [실제 증거 변조 검사](trace-negative-tests.json) · [Native mailbox 오류 주입](native-mailbox-tests.json) · [Direct/guard audit](mode-audit.json) · [기존 상태 전달 controller audit](state-mode-audit.json) · [Handoff별 원시 시간 표](handoffs.csv) · [원시 자료](raw-evidence.tar.gz) · [자원 정리](cleanup.json)

Trace audit는 mailbox phase 96개와 API phase 192개, 실행 wrapper 105개, build/artifact 항목 13개, measured patch 3개와 helper source 3개도 확인했다. 결과 archive를 새 디렉토리에 풀어 VM 권한 없이 다시 검증할 수 있다.

```bash
mkdir -p /tmp/mode-evidence
tar -xzf implementation/virtual_machine/results/mode-compatibility-20261027/raw-evidence.tar.gz \
  -C /tmp/mode-evidence
python3 implementation/virtual_machine/verify_trace_results.py \
  /tmp/mode-evidence/trace/report.json --formal
python3 /tmp/mode-evidence/audit-tools/audit_mode_compatibility.py \
  /tmp/mode-evidence/compatibility/*/ --out /tmp/mode-audit.json
python3 /tmp/mode-evidence/audit-tools/audit_state_mode_baselines.py \
  /tmp/mode-evidence/native-state/index.json --out /tmp/state-mode-audit.json \
  --file-list /tmp/state-mode-files.txt
```

배포 archive를 새 디렉토리에 풀어 **7,844개 파일의 SHA256**을 확인하고 세 종류의 offline audit를 다시 통과했다([packaging audit](packaging-audit.json)).

큰 원본 prepared image가 있으면 trace verifier의 `--artifacts --assets-dir PATH`를 추가한다.
Trace 준비·실행은 [운영 README](../../README.md#host-nfs와-실제-개발-trace-재생)의 `--state-transport dax-mailbox`를 사용한다.
Direct/guard 실행기는 `mode_compatibility.py --help`에 case와 asset 인자를 명시한다. `direct-baseline`은 현재 trace asset을 쓸 수 있지만 나머지 guard case는 `sandbox.raw`와 `nfs-l2-initramfs.cpio`가 있는 **NFS synthetic asset**을 사용한다. 각 case는 새 VM pair를 소유하고 종료한다.

## 남는 통합 작업

먼저 direct의 거부·복구 경계를 안전하게 만들고, virtio-blk queue 상태와 host image FD, ioeventfd/IRQFD, MMIO 처리 주체를 정해야 한다. 이후 1 vCPU·read-only disk부터 direct A→B→A를 연결하고 다중 vCPU barrier로 확장한다. 상태 전달 계열에서는 파일을 가진 worker의 reuse 계약과 native controller 연결이 별도다.

모든 시험은 한 물리 host의 nested KVM/coherent CXL emulation이다. 실제 다중 host, 최초 DRAM→CXL promotion, persistent writable disk 내구성, PML을 포함한 비일관 cache 처리와 MPTCP의 통합은 아직 이 결과에 포함되지 않는다.
