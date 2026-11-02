# 목적지 준비가 왜 약 1초였는가: 원인 분해와 제어 경로 수정

## 결론

이전의 `목적지 준비 763.966ms + TCP 서버 준비 309.647ms`를 NFS나 page-cache 구현 자체의 지연으로 설명한 것은 잘못이었다. 실제로는 실험 제어기의 serial 명령, 파일 업로드, 고정 주기 polling, 실행 파일 hash 검증이 포함된 값이었다. 기존 제어기는 준비와 매번의 요청 처리를 함수 하나에서 진행하여 이 경계를 더 모호하게 만들었다.

동일한 Firecracker·커널로 기존 경로를 재실행하여 원인을 분해했다. **목적지 준비 766.562ms 동안 NFS 데이터 READ 증가는 0회**였고, 디스크 backend 생성 자체는 2.450ms였다. 준비 단계에는 guest RAM mapping이나 page-cache 공유 admission도 없다.

이 문제를 확인한 뒤 준비 코드를 묶어 실행하고, 첫 promotion 직후에 목적지와 제어 서버를 준비하도록 수정했다. 요청이 들어오면 이미 실행 중인 제어 서버가 소유권 전환을 처리한다. 준비된 요청에 프로세스 생성이나 NFS 이미지 재열기를 넣지 않는다. 최초 준비, 요청 처리, 검증 도구 전체 시간을 별도로 남긴다.

## 최종 측정 결과

상세 VMM profiling을 끈 최종 실행은 **24회 handoff(22회 RPC, 2회 부정 시험용 serial 전환), promotion/demotion 각각 2회, trace 명령 27개**를 통과했다. Guest `Cached`는 426.49–441.92MiB였다. 58개 거부 API, 26개 parked RAM/KVM 실제 접근 probe, FD/TID 유지 검증을 통과했다. 독립 verifier 증거 변조 시험도 **15/15 거절**했다.

준비는 각 실행에서 최초 한 번 측정한 값이며 분포나 중앙값이 아니다.

| 준비 항목 | 기존 경로 재측정 | 수정 후 최종 |
|---|---:|---:|
| B 준비: 외부 serial·파일 전달·실행 hash 포함 | 766.562ms | **334.894ms** |
| A/B 제어 서버 준비: 외부 명령 전체 | 352.094ms | **176.557ms** |
| 위 두 항목 합계(서로 겹치지 않음) | 1118.655ms | **511.450ms** |
| FD/TID 감사까지 포함한 명시적 prewarm 함수 전체 | 별도 계측 없음 | **774.305ms** |
| B 내부 VMM 준비: fork/exec/API 연결/NFS 준비 포함 | 별도 계측 없음 | **8.305ms** |
| B의 fork→API 연결 가능 | 별도 계측 없음 | **4.437ms** |
| B의 DirectPrepare API | 3.723ms | **3.667ms** |
| A/B 내부 제어 서버 각각 준비 | 별도 계측 없음 | **0.728 / 0.593ms** |

334.894ms는 scratch 위치 조회 37.862ms, script 전달 80.416ms, native 준비·실행 파일 hash 검사 외부 명령 214.987ms, 나머지 1.628ms로 구성된다. Native 8.305ms는 이 안의 하위 구간이다. **외부 실험 자동화 전체가 8ms가 됐다는 뜻은 아니다.** 한 번의 준비를 handoff 요청마다 반복하지 않도록 바꾼 것이 별도의 중요한 변경이다.

다음은 22개 RPC 표본 전체다. 최초 방문과 느린 표본을 모두 포함한다.

| 요청 및 실행 구간 | 중앙값 | 최소–최대 |
|---|---:|---:|
| source client 요청→목적지 Resume 수락 응답 | **10.050ms** | 8.359–33.931ms |
| source client 요청→실제 guest serial ACK | **24.833ms** | 14.325–49.881ms |
| Pause 요청→guest ACK | 23.583ms | 13.192–48.540ms |
| guest serial ping 응답 구간 | 14.902ms | 4.394–16.697ms |
| peer TCP 연결 | 0.772ms | 0.504–0.886ms |
| 외부 serial의 client 실행·출력 회수 명령 전체 | 112.534ms | 72.693–143.432ms |

24개 handoff 전체의 Export+Import API 중앙값은 **5.322ms**다. 기존 resident 최종값 5.316ms와 비슷하다. Firecracker 내부 연산을 이번 변경으로 대폭 줄였다고 주장하지 않는다. 바꾼 것은 초기 준비의 외부 왕복, 요청 직전의 반복 준비/plan 전달과 요청 측정 경계다.

재-promotion 직후 generation 23의 요청→Resume는 33.931ms였다. Import API 27.613ms 중 kernel attach가 **25.004ms**를 차지했다. 전체 24회의 kernel attach 중앙값은 1.637ms, 최대는 이 25.004ms였다. 이 느린 표본은 실제 전환 비용으로 남겼다. NFS open 지연이 아니며, 이번 최종 계측만으로 attach 내부 전체를 EPT root 동기화 한 항목에 귀속하지 않는다. 다음 최적화 대상으로 따로 볼 수 있는 잔여 비용이다.

## 기존 766.562ms의 구성

재현 실행 `prep-audit`의 한 번뿐인 최초 B 준비 값이다. 아래 항목은 서로 겹치지 않는다.

| 작업 | 외부 제어 시간 |
|---|---:|
| 디렉터리 생성 명령 | 44.581ms |
| 업로드용 scratch BAR 위치 조회 2회 | 93.762ms |
| launch script 업로드 | 86.500ms |
| FIFO·프로세스 실행·API socket polling 명령 | 170.016ms |
| environment 증거 업로드 | 87.776ms |
| 실행 PID 및 `/proc/PID/exe` SHA-256 확인 | 151.550ms |
| kernel owner PID 선택 명령 | 61.150ms |
| DirectPrepare API를 호출하는 외부 명령 | 68.388ms |
| 나머지 Python 처리 | 2.839ms |
| **합계** | **766.562ms** |

170.016ms가 fork syscall 하나의 시간은 아니다. Serial로 shell 명령을 보내고 결과를 기다리는 시간과 20ms 간격의 socket polling 등이 포함된다. SHA-256 항목은 Firecracker **실행 바이너리** 검사이며 NFS 이미지 전체 hash가 아니다.

DirectPrepare client API 자체는 **3.723ms**였다. 그 안의 VMM 디스크 생성 profile은 다음과 같다. 이 값들은 위 68.388ms 안에 포함되므로 다시 더하면 안 된다.

| VMM 내부 단계 | 시간 |
|---|---:|
| NFS 이미지 `open` | 2.122ms |
| `fstat`로 크기 확인 | 0.008848ms |
| metadata에서 virtio disk ID 생성 | 0.017545ms |
| pool identity 분기 | 0.003874ms |
| Async io_uring 생성 | 0.264943ms |
| 디스크 생성 전체 | 2.450486ms |

NFSv3 client counter는 LOOKUP +1, ACCESS +2, READ +0, GETATTR +0이었다. Counter는 RPC **횟수**이고 읽은 byte 수가 아니다. 준비 전부터 존재하던 READ 6144회를 준비 비용으로 세면 안 된다. 최초 image 검증과 workload I/O는 별도 단계다.

## NFS와 page-cache 코드에서 확인한 사실

- `DiskProperties::new`는 최초 준비 때 이미지 FD를 열고 `fstat` 결과를 크기와 disk ID에 재사용한다. 파일 전체를 읽거나 hash하지 않는다. 최초 준비 이후 FD/io_uring은 해당 host에서 유지한다.
- Guest-authority/mapper 모드의 `pool_cache::disk_identity`는 곧바로 `None`을 반환한다. 과거 block-read hint 수집을 추가로 수행하지 않는다.
- DirectPrepare는 guest RAM에 접근하거나 KVM attach를 하지 않는다. Bloom 검색, inode 확인, page-cache admission은 promotion 단계의 별도 작업이다.
- Handoff의 mapper checkpoint는 고정 크기 handle을 query/seal한다. 전체 GPA table 조회는 명시적 `FC_POOL_FULL_AUDIT=1`의 audit 경로에 한정된다.
- Read-only block의 상태 저장은 진행 중인 read를 drain하지만 `fsync`는 하지 않는다. I/O 완료와 최신 virtqueue 저장은 유지해야 한다.
- Source RAM의 `PROT_NONE` 교체는 실제 PTE 철회 비용이다. RAM 내용 전수 비교는 아니지만 workload가 만든 mapping에 따라 비용이 늘 수 있다. 이를 준비·검증 비용이라고 빼지 않았다.

이번 변경에서 Firecracker 및 kernel patch는 수정하지 않았다. 이전의 read-only disk/mapper 안전 조건을 유지한 채 **실험 제어기 경로의 불필요한 왕복과 반복 준비를 수정**했다.

## 실제로 바꾼 흐름

```text
A에서 실행 → DRAM→CXL promotion
            → B의 VMM·NFS FD·io_uring 및 A/B 제어 서버 준비
            → 준비 완료 상태

handoff 요청 → source 상주 제어 서버 → peer 연결
             → Pause → DirectExport → 작은 metadata 전달
             → DirectImport/첫 InstanceStart → mapper checkpoint → Resume
             → Resume 수락 응답 → guest serial ping 응답
```

`resident_bootstrap.c`가 디렉터리·FIFO·PID 기록·fork/exec·API 연결 대기·DirectPrepare를 노드 안에서 처리한다. Socket이 존재하기만 하는 상태와 실제 연결 가능한 상태도 구분한다. `resident-control launch`는 제어 서버를 fork하고 pipe로 listen 완료를 확인한다. 이전 shell polling을 사용하지 않는다.

`--resident-ready-control`은 최초 promotion 이후 명시적 준비를 요구한다. 각 handoff 직전에 DirectPrepare를 재호출하지 않고, 작은 checkpoint plan도 요청 필드에 넣어 목적지에서 작성한다. 실제 import 때의 token generation, VM identity, manifest digest, allocation lease 검증은 그대로 수행한다. Readiness cache를 RAM 소유권 증거로 사용하지 않는다.

첫 B 방문의 VM/vCPU wrapper·vCPU thread·나머지 장치 객체 생성은 여전히 첫 import에 포함된다. 첫 방문을 warm sample로 바꾸거나 결과에서 버리지 않았다. Demotion 후 재-promotion에서는 새로운 mapper handle을 받아야 하며, 이전 allocation을 재사용하지 않는다.

## 측정 경계

| 지표 | 포함하는 것 |
|---|---|
| `cold_prepare_controller_ms` | script 전달, 외부 serial 제어, VMM bootstrap, 실행 파일 hash 검사 |
| `native_vmm_ms.native_ready_ns` | 노드 안의 디렉터리/FIFO 생성, fork/exec, API 대기, owner 설정, NFS 준비 |
| `native_server_ms` | 각 노드 제어 서버의 fork부터 listen 준비 완료까지 |
| `full_prewarm_controller_ms` | 외부 제어에서 준비 함수 전체; 위 준비와 FD/TID 감사까지 포함 |
| `request_to_resume_response_ms` | source 노드 C client의 RPC 연결 시작부터 목적지 Resume API 수락 응답까지; 로컬/peer 연결 포함 |
| `request_ms` | 위 작업 및 실제 guest serial ping ACK가 source client에 돌아오기까지 |
| `handoff_ms` | source Pause 요청 직전부터 guest ACK 반환까지; 기존 pause→ACK 지표 |
| `controller_command_wall_ns` | 외부 serial에서 RPC client를 실행하고 출력 회수하는 전체 명령 시간 |

Native 준비 시간은 외부 controller 시간에 포함되는 하위 구간이다. 둘을 더하거나, native 시간만으로 외부 자동화 전체가 완료됐다고 말하면 안 된다. 준비되지 않은 목적지에 대한 cold end-to-end 시간이 warm RPC 시간과 같다는 주장도 하지 않는다. **Resume 수락 응답은 guest의 첫 instruction timestamp가 아니다.** 실제 guest 응답까지의 값도 함께 제시한다.

RPC 시계는 source 노드의 C client 안에서 시작한다. 그 client를 실험용 serial shell로 실행하는 비용과 출력 회수는 `controller_command_wall_ns`에 남는다. 상주 서버 자체는 요청 전에 이미 실행 중이다. 따라서 RPC 수치를 outer Python 함수 전체의 end-to-end 시간이라고 부르지 않는다.

## 검증 범위

Coherent 물리 호스트 하나 안의 두 nested KVM 노드에서 1vCPU·2GiB sandbox를 사용했다. A는 local ext4 이미지, B는 immutable NFS 이미지 FD를 유지한다. Gin SWE trace 27개 명령을 원본 그대로 실행하고, promotion/demotion 및 CXL CPU handoff를 분리했다.

Same boot ID/PID, 열린 삭제 파일, private 파일, 명령 결과, 테스트 결과, 제출 diff, guest cache 유지, mapper generation, source RAM/KVM 접근 차단을 독립 verifier가 확인한다. Source fence/probe는 매번 수행하며, warm handoff에서 중복 DirectPrepare만 생략했다. 손상시킨 증거의 SHA-256까지 다시 계산하는 부정 검증으로 지표와 소유권 의미 검사를 확인한다.

이 결과는 일반 RAM migration과 동일 조건으로 새로 비교한 실험은 아니다. 과거 sub-ms 경로는 이미 준비된 KVM 실행자와 공유 메모리 heartbeat를 측정했고, 현재처럼 NFS block 장치 상태와 host RAM 접근 철회 전 과정을 포함하지 않았다. 현재 남은 비용은 동일한 경계에서 비교해야 한다. 실제 비일관 다중 호스트 CXL, writable disk, MPTCP는 이번 범위 밖이다.

## 원시 증거와 재현

- [comparison.json](comparison.json): 독립 verifier를 통과한 baseline/final의 준비 항목, NFS counter 차이, RPC 표본 전체.
- [final-attribution.json](final-attribution.json): 24회 API/kernel 시간. Profiling을 끈 실행이므로 내부 세부 stage 시간을 추정하지 않는다.
- [diagnostic-attribution.json](diagnostic-attribution.json): 초기 수정 경로의 상세 profiling 실행 8회. 최종 제어 서버 launcher를 추가하기 전 결과이며 최종 성능 표본과 합치지 않는다.
- [evidence-negative-tests.json](evidence-negative-tests.json): 최종 증거 변조 시험 15개.
- [input-comparison.json](input-comparison.json): runtime 입력 hash, 추출한 guest initramfs 내용·mode·symlink 335개와 workload 결과 일치 확인. 재포장한 cpio 파일 자체의 hash는 다르다.
- [archives.json](archives.json): archive SHA-256와 모든 regular file의 원본 byte 일치 확인.
- `baseline.tar.xz`, `diagnostic.tar.xz`, `final.tar.xz`: 각각 원시 suite, source freeze, guest/VMM 로그, API receipt와 독립 검증 결과. 내부 root는 `cxl-prep-audit`, `cxl-ready-audit`, `cxl-ready-final`이다.

Firecracker SHA-256는 세 실행 모두 `7a371e503d46a95836cae270594c43c03f64e577d58ea53c1742a14408337d1c`다. Kernel/mapper도 같은 fixture를 사용했다. C helper는 `gcc -O2 -Wall -Wextra -Werror -static`으로 빌드했고, Python handoff/protocol 단위 검증 19개가 통과했다.

최종 fixture는 `resident_prepare.py`로 기존 `swe-directopt1` fixture에 동일 Firecracker와 새 helper를 넣어 생성했다. 실제 실행 명령과 모든 입력 hash는 final archive의 suite `report.json`과 fixture manifest에 있다. 주요 실행 옵션은 다음과 같다.

```bash
python3 -u implementation/virtual_machine/swe_pool_run.py \
  --assets implementation/virtual_machine/.local/swe-ready2 \
  --qemu /home/ijkim/agent-sandbox-cxl/implementation/virtual_machine/.local/qemu-cxl/build/qemu-system-x86_64 \
  --qemu-root /home/ijkim/agent-sandbox-cxl/implementation/virtual_machine/.local/qemu/root \
  --session ready-final --names gin-gonic__gin-3741 --modes direct \
  --resident-direct --resident-fast-control --resident-ready-control \
  --immutable-nfs-cache --direct-profile-roundtrips 10 \
  --output /tmp/cxl-ready-final
```

이미 존재하는 session/output은 재사용하지 않는다. Archive를 새 경로에 푼 뒤에는 `verify_swe_pool.py --suite <root>/report.json`으로 원시 증거를 다시 검증할 수 있다. `analyze_resident_preparation.py <baseline의 trial/report.json> <final의 trial/report.json> --output comparison.json`이 위 비교표의 기초 자료를 재계산한다.
