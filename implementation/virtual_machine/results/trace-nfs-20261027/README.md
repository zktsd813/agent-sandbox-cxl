# 실제 개발 trace의 host NFS·CXL microVM 이전 검증

2026-10-27 실험 기록. 기존의 단일 파일 순차·무작위 읽기를 넘어, 기록된 개발 명령을 실행하던 같은 guest를 **A→B→A**로 이전한다. 파일 읽기뿐 아니라 소스 수정·컴파일·테스트 결과와 열린 FD가 이어지는지 확인한다.

## 확인한 결과

**24 trials·840개 명령·48회 A↔B handoff를 완료했다.** 같은 guest가 A→B→A로 이동한 뒤 원본 trace의 기대 종료값과 최종 patch를 재현했다. Read-only 이미지의 미캐시 데이터는 B host NFS로 읽고, 파일 수정·빌드 결과는 guest tmpfs upper에 유지하는 구조가 작동했다.

- 원본 종료값이 있는 명령 **816개**와 최종 제출 명령 **24개**를 검사했다. 원본에서 실패했던 **48개 nonzero 종료**도 기대값과 일치했다.
- 원본과 비교 가능한 **72개 테스트 요약**, 최종 patch 24개, handoff 48회의 열린 FD·수정 파일·source fence를 독립 검증했다.
- 모든 trial의 B backing mount가 정확히 하나이고 기대한 read-only backend였다. Guest lower block 쓰기와 local 조건의 NFS READ는 모두 0이었다.
- VM·TAP/bridge·임시 shared-memory backing을 모두 정리했다([cleanup](cleanup.json)).

최종 근거 파일: [전체 report](report.json), [독립 audit](audit.json), [요약](summary.json), [trial별 CSV](trials.csv), [원시 자료](raw-evidence.tar.gz), [음성 테스트](negative-tests.json), [synthetic 회귀 audit](regression-audit.json).

## 실행하는 workload와 이전 지점

기존 `.traj.json`에 기록된 **3개 trace, 총 105개 명령**을 원래 순서·명령 byte·작업 디렉토리·환경 설정으로 실행한다. 원본의 명령 사이 대기 시간은 재생하지 않는다.

| Trace | 명령 수 | A→B 이전 후 시작 index | B→A 이전 후 시작 index |
|---|---:|---:|---:|
| `gin-gonic__gin-3741` | 27 | 12 | 25 |
| `php-cs-fixer__php-cs-fixer-8331` | 35 | 21 | 33 |
| `micropython__micropython-13039` | 43 | 26 | 41 |

Index는 0부터 센다. 이전은 해당 명령을 시작하기 직전에 수행하므로 진행 중인 compiler/test process를 중간에 멈추는 시험은 아니다. 각 trial은 A에서 새 guest로 시작하고, B에서 중간 작업을 실행한 뒤 A로 돌아와 마지막 두 명령을 실행한다.

원본에서 실패했던 명령은 **기록된 nonzero exit code가 기대값**이다. 모두 0이어야 성공이라고 판정하지 않는다. 각 명령의 기대 종료값과 안정적인 테스트 결과를 대조하고, 마지막 제출 명령은 성공 및 원본과 같은 최종 patch를 요구한다.

MicroPython 원본의 command 33은 **514 tests·16,063 testcases, 512 passed·9 skipped·기존 실패 2개**이고, command 34는 **1 test·4 testcases, 0 passed·`bytes_compare3` 실패**가 기대 결과다. 이를 재현하는 것은 프로젝트 테스트가 모두 통과했다는 뜻이 아니다.

MicroPython 제출물의 원본과 재생 출력은 아래의 **정확히 지정한 stderr 경고 한 줄**이 patch 뒤에서 앞으로 이동해 전체 출력 SHA256이 다르다. 기존 replay와 동일하게 이 줄만 제거한 patch SHA256과 경고 출현 횟수를 함께 대조한다. 두 출력 모두 경고는 1회이고, 제거 후 SHA256은 `2461a59d68407b8d3cd1702844550dbdd0978e90635b0b0c3c26467942c0ba02`로 같다. 실행 명령과 patch 데이터는 일치하며 다른 출력은 정규화하지 않는다. 전체 출력 hash도 audit에 보존한다.

```text
/root/.bashrc: line 1: /opt/miniconda3/etc/profile.d/conda.sh: No such file or directory
```

## 파일과 RAM이 유지되는 구조

```text
A 실행: guest OverlayFS → read-only ext4 lower → virtio-blk → A local image
                  └── 파일 수정·빌드 산출물 → guest tmpfs upper

A → B: source 정지·snapshot·fence → 같은 DAX guest RAM으로 B 복원

B 실행: guest cache hit → 기존 guest RAM
        lower miss → virtio-blk → B local replica 또는 B host NFS → A image
        upper read/write → 같은 guest tmpfs

B → A: 같은 절차로 돌아와 A local image와 기존 upper/FD로 작업 계속
```

NFS client는 **L1 host B**에 있고 guest에는 NFS mount가 없다. Guest가 수정한 파일은 read-only image에 기록하지 않고 RAM의 OverlayFS upper에 보관한다. 열린 뒤 unlink한 파일의 FD도 이전 전후 검사한다. Guest RAM은 처음부터 shared DAX에 있으므로 최초 DRAM→CXL promotion은 시험하지 않는다.

이 구성은 writable disk를 여러 host가 공유하는 구현이 아니다. Tmpfs 변경 내용의 전원 장애 내구성, disk writeback·fsync·writer fencing은 별도 과제다. 여기서 source fencing은 이전 source Firecracker가 같은 guest RAM을 계속 실행하지 못하도록 하는 절차다.

## 비교 조건과 환경

정식 matrix는 **3 trace × 2 backend × (retained 3회 + evicted 1회) = 24 trials·48 handoffs**다. 각 cycle에서 local/NFS 실행 순서를 교대한다.

| 항목 | 설정 |
|---|---|
| 물리 구성 | 한 host의 nested KVM, QEMU가 공유하는 coherent CXL emulation |
| L1 A/B | 각각 2 vCPU·4 GiB RAM |
| CXL guest RAM backing | 4 GiB shared device-DAX 영역 |
| L2 microVM | 2 vCPU·2 GiB RAM, 일반 snapshot 복원 |
| Guest filesystem | Read-only ext4 lower + guest tmpfs OverlayFS upper |
| 외부 storage | L0의 동일한 13 GiB read-only raw disk를 두 L1에 연결, QEMU `cache=none` |
| A backing | 모든 조건에서 A local image |
| B local 조건 | 같은 내용의 image가 B에도 미리 준비된 local replica |
| B NFS 조건 | B host가 A의 read-only export를 NFSv3/TCP로 접근 |
| NFS read 크기 | 요청 rsize 1 MiB, 실제 mount 관측의 협상값 **512 KiB** |
| Network | 격리된 TAP/bridge/vhost 경로; 물리 NIC·지연/대역폭 shaping 없음 |

Local replica는 두 L1에 같은 read-only disk를 연결해 모사하며, 생성·복제 비용은 포함하지 않는다. 두 L1의 Linux image cache는 별개다. Trial 시작 때 A/B host image cache를 비우고 원시 mincore 결과를 확인한다. 이후에는 A의 작업으로 A server cache가 자연스럽게 채워지는 반면, B local replica의 cache는 B가 읽기 전까지 차갑게 남을 수 있다. 따라서 local/NFS 시간 비율은 **이 실행·cache 배치의 결과**이며 순수 network 비용으로 해석하지 않는다. SSD/controller cache도 통제하지 않는다.

`retained`는 이전 후 guest cache를 강제로 비우지 않는 자연스러운 실행이다. 특정 페이지 집합이 모두 남았거나 모든 읽기가 cache hit라는 뜻은 아니다. `evicted`는 각 이전 후 guest에서 `sync; drop_caches=3`을 실행해 reclaim 가능한 cache를 비운다. Mapped page와 tmpfs upper까지 모두 제거되는 조건은 아니다. 특정 guest page-cache 페이지의 보존 및 zero backing I/O는 synthetic 시험이 직접 검증한다.

여기서 회수하는 것은 Linux page cache와 dentry/inode cache다. CPU cache line의 flush/invalidate나 비일관 CXL의 coherence 검증과는 구분한다.

## 시간과 I/O를 읽는 법

명령 시간은 guest `/proc/uptime`의 시작·종료 차이이며 **10 ms 해상도**다. 명령 실행과 해당 wrapper의 작업 구간을 포함하고, 명령 사이 원본 think time·serial 제어·snapshot state 전달은 제외한다. `destination_duration_s`는 B에서 실행한 명령 구간 합계다. 짧은 명령의 0 또는 10 ms 차이를 세밀한 latency 차이로 해석하지 않는다.

`controller_handoff_ns`는 별도로 기록한다. 명령 시간과 합쳐 µs/ms급 이전 성능을 주장하지 않는다. Snapshot state는 serial 제어 경로로 옮기며, guest RAM은 같은 DAX 영역에 유지한다.

NFS I/O는 각 명령 전후와 **trial 전체 전후**를 각각 관측한다. 독립 검증기가 `/proc/net/rpc/nfs`, `/proc/self/mountstats`, interface byte 원문에서 차이를 다시 계산하고 boot·복원·제어 구간 등 명령 구간 밖의 traffic도 따로 대조한다. Global RPC 시작 counter와 mount 완료 counter는 순차 관측되므로 차이를 기록한다.

## 최종 성능 측정

Cache를 유지한 조건은 backend별 **3회**다. 시간은 전체 명령 구간 합계의 중앙값과 범위이며, B 구간은 별도 중앙값이다.

| Trace | Local 전체 초, 중앙값 [최소–최대] | NFS 전체 초, 중앙값 [최소–최대] | B 구간 local / NFS 초 |
|---|---:|---:|---:|
| Gin | 17.99 [17.84–18.12] | 17.93 [17.85–18.28] | 8.81 / 8.75 |
| PHP-CS-Fixer | 8.14 [7.74–8.17] | 8.03 [7.85–8.10] | 2.66 / 2.75 |
| MicroPython | 14.67 [14.66–15.06] | 15.05 [14.87–15.16] | 7.66 / 7.58 |

이 표만으로 NFS가 더 빠르거나 비용이 없다고 결론 내리지 않는다. 표본이 작고 범위가 겹치며 server/replica cache 배치가 다르다. 이미 실행한 작업의 cache와 RAM upper를 유지하면서 원격 image에 필요한 읽기만 발생한다는 점이 핵심이다.

아래는 **B NFS 조건의 trial 전체** READ 시작 횟수와 READ RPC 수신량이다. 수신량은 mountstats 값이며 순수 파일 payload와 구분한다. Retained는 3회 중앙값 [범위], evicted는 **1회뿐인 민감도 대조**다.

| Trace | Retained READ [범위] | Evicted READ | Retained 수신 MiB [범위] | Evicted 수신 MiB |
|---|---:|---:|---:|---:|
| Gin | 16 [15–16] | 3,377 | 0.55 [0.47–0.63] | 118.45 |
| PHP-CS-Fixer | 158 [157–159] | 2,071 | 16.56 [16.53–16.58] | 84.75 |
| MicroPython | 383 [377–387] | 3,444 | 11.64 [11.19–11.64] | 104.55 |

Evicted 조건의 local/NFS 전체 명령 시간은 각각 Gin **19.66/19.51초**, PHP **8.66/9.02초**, MicroPython **15.98/16.56초**였다. Cache 회수로 READ가 늘어나는 현상을 확인한 것이며 반복 통계나 전체 cache가 제거됐다는 증거는 아니다. 모든 trial에서 명령 구간 밖의 NFS READ delta는 0이었고, READ 시작·완료 counter도 일치했다. Metadata RPC는 별도이므로 모든 network traffic이 0이라는 뜻은 아니다.

48회 handoff의 state는 **25,018–25,106 bytes**, guest RAM 전달량은 **0 bytes**였다. Serial 제어·state 반출입을 포함한 controller handoff는 중앙값 **9.31초**, 범위 **7.60–9.95초**였다. 이 경로는 재현과 검증을 위한 제어 구현이며 빠른 handoff 실적으로 제시하지 않는다. 앞의 workload 시간 표에는 이 시간이 포함되지 않는다.

## 검증하는 증거

`verify_trace_results.py`는 실행기와 별도로 다음을 대조한다.

- 원본 trace hash, 모든 명령의 byte/hash·순서·기대 종료값, guest archive와 serial 기록의 동일성.
- 준비한 initramfs CPIO 안의 실제 executable wrapper, interpreter·환경·작업 디렉토리·timeout, 측정 당시 소스와 build artifact hash.
- 최종 제출 patch fingerprint와 Go package 결과, PHPUnit test/assertion 수, MicroPython test/testcase·성공·실패·skip 결과 등 원본과 비교 가능한 테스트 요약.
- Guest boot ID/PID, 수정한 파일의 hash, unlink한 FD의 inode·내용·연속 offset, A→B→A 명령 경계.
- Source PID incarnation과 실행 종료 상태를 담은 fence 원문, snapshot state 크기·SHA256·전달 중 불변성, DAX 경로의 기록.
- Guest block counter의 쓰기 0, host cache 초기 상태, 명령별·전체 trial NFS counter, 요약 수치의 재계산.

Timeout을 거부하고 command/watchdog process group을 정리한다. 이 trace들에는 daemon을 별도 session으로 남기는 명령이 없다. 임의 workload의 모든 detached child까지 이 방식으로 격리한다는 주장은 하지 않는다.

## 재검토에서 발견하고 고친 문제

NFS storage setup·CPU affinity·backend 선택을 공통 함수로 정리해 synthetic과 trace 실행에서 재사용한다. 독립 검증기의 parser는 실행기 계산을 그대로 신뢰하지 않기 위해 별도로 유지했다.

| 문제 | 변경 |
|---|---|
| 기존 synthetic audit가 host 수치를 report에서만 확인 | 새 실행은 Counter/cache 원문에서 delta를 재계산한다. 기존 192개 측정도 [L1 serial 소급 audit](../nfs-20261027/host-serial-audit.json)로 NFS counter와 B cache를 재확인했다. A cache의 개별 대응 한계는 남겼다. |
| Guest controller 준비 전에 serial 명령을 보내 입력이 사라질 수 있음 | `trace_ready`를 기다린 뒤 첫 명령을 보낸다. |
| 종료한 source의 zombie 상태 또는 `/proc` 조회 중 reap을 오류로 오판 | PID 시작 시각과 process state를 함께 확인한다. 먼저 읽고, 읽기 실패 시 entry가 실제로 사라졌는지 확인한다. 동일 incarnation의 실행 가능한 process와 잘못된 숫자 정보는 거부한다. |
| Static BusyBox에 `base64` applet이 없어 결과 반출 실패 | 지원되는 `uuencode -m` 경로를 사용하고 준비 단계에서 필요한 applet을 검사한다. |
| L1에 없는 `mountpoint`가 조건문에서 실패해 `/disk` bind mount가 누적 | 공통 `select_backend()`가 `/proc/mounts`를 읽어 기존 mount를 해제하고, 선택한 read-only mount가 정확히 하나인지 즉시 확인한다. |
| 중단 후 같은 L1 pair 재사용 시 기존 mount/service가 섞일 수 있음 | Pair를 한 번만 사용하도록 marker로 거부한다. 다시 실행하려면 `lab.py stop/start`로 새 pair를 만든다. |

위 수정 후 raw counter·cache 값, 출력, FD/fence 등 증거를 변조하거나 누락시키는 음성 테스트를 수행했다. **12개 변조를 모두 거부**했다([negative-tests.json](negative-tests.json)). Report와 요약을 동시에 고친 counter, hash까지 다시 계산한 출력·patch·테스트 요약, handoff 뒤 시계 역행, 명령 누락·중복, FD offset, fence, 다른 trial 증거 재사용, 중복 backend mount를 포함한다. Synthetic host 관측 parser의 10개 회귀·변조 검사도 통과했다([host 관측 검사](host-observation-tests.json)).

Mount 누적은 첫 24-trial 실행을 끝낸 뒤 독립 audit가 발견했다. 최상단 경로와 NFS counter는 기대한 backend에 맞았지만, 깨끗한 비교 조건을 충족하지 않아 그 실행은 최종 성능 표에서 제외했다. [실패한 audit](rejected-run-audit.json)와 당시 mount 원문·소스는 archive의 `rejected-formal-run/`에 보존한다. 수정 후 전체 matrix를 다시 측정했으며, 이 문서의 성능 표는 최종 통과한 실행만 사용한다. 중간 재실행에서 드러난 source 종료 조회 race도 수정한 뒤 새 pair에서 다시 시작했다.

수정 후 **synthetic 회귀 20 samples·2 handoffs**를 통과했다([regression-audit.json](regression-audit.json)). Local/NFS 교대와 3겹 mount를 1개로 복구하는 실제 L1 검사도 5건 통과했다([mount 회귀](backend-mount-regression.json)). 이 자료는 기능·검증기 회귀 근거이며 성능 표에는 별도로 반복한 trace 측정만 사용한다.

Source fencing은 정확한 실행 스크립트를 BusyBox로 시험했다. 살아 있는 process, 종료·PID 재사용, 조회 중 reap, 읽기 오류, 잘못된 PID/starttime을 포함한 **21개 fixture가 통과**했다([fence 검증](source-fence-tests.json)). 이 시험은 합성 `/proc` 파일을 사용하며 실제 host process에 신호를 보내지 않는다.

## 재현과 자료

의존성·image 준비, 새 L1 pair 실행, 정식 matrix와 독립 audit 명령은 [운영 README](../../README.md#host-nfs와-실제-개발-trace-재생)에 있다. 결과 archive를 별도 디렉토리에 풀면 VM 실행 권한 없이 다시 검증할 수 있다.

```bash
mkdir -p /tmp/trace-nfs-evidence
tar -xzf implementation/virtual_machine/results/trace-nfs-20261027/raw-evidence.tar.gz \
  -C /tmp/trace-nfs-evidence
python3 implementation/virtual_machine/verify_trace_results.py \
  implementation/virtual_machine/results/trace-nfs-20261027/report.json \
  --evidence-dir /tmp/trace-nfs-evidence --formal
```

`--artifacts`는 원래의 큰 prepared image·initramfs와 source image가 있을 때 추가한다. Raw archive에는 독립 audit에 필요한 원본 trace, measured source, 명령 결과·serial log·counter·state 자료를 함께 보관한다. 배포 archive를 새 디렉토리에 풀어 **7,290개 파일의 SHA256**을 확인하고 trace·synthetic 검증기를 다시 통과했다([packaging audit](packaging-audit.json)).
