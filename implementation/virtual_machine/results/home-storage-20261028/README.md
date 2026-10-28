# 고향 디스크와 DRAM↔CXL 왕복 검증

2026-10-28. 노드 A의 파일 image로 시작한 sandbox를 CXL에 올린 뒤 B의 CPU로 실행하고,
A로 돌아와 DRAM에서 계속 실행하는 경로를 구현했다. **Guest의 ext4와 virtio-blk는 유지하고,
Firecracker가 image를 여는 host 경로만 local/NFS/local로 바꾼다.**

이번 결과는 일반 snapshot 경로의 상태 연속성과 파일 보존 검증이다. Direct의 block 장치 이전은
아직 지원하지 않으며, 지원하지 않는 요청을 거절한 뒤 VM이 계속 작동하도록 실패 처리를 수정했다.

## 구현한 구조

| 단계 | 실행 CPU | Guest RAM | Host의 image 접근 |
|---|---|---|---|
| 시작 | A | A DRAM | A local disk |
| Promotion | A | CXL | A local disk |
| Handoff | B | 같은 CXL | NFS로 A의 같은 image |
| 고향 복귀 | A | 같은 CXL | A local disk |
| Demotion | A | A DRAM | A local disk |

RAM 위치와 storage 접근 위치는 독립적이다. A/CXL 단계에서도 NFS는 필요 없다.
Guest의 inode, page cache, dirty page, 열린 FD와 offset은 guest RAM에 포함된다.
Cache miss와 writeback은 계속 virtio-blk를 거쳐 현재 Firecracker의 image FD에 도달한다.
Guest ext4를 NFS mount로 교체하지 않으므로 기존 FD를 다른 filesystem에 재연결하는 과정도 없다.

실험은 A에만 private writable outer disk를 연결한다. 그 ext4 안의 `sandbox.raw`를
A에서는 local bind mount, B에서는 A의 NFSv3 export로 연다. 다른 노드에 writable 복제본을
만드는 방식은 시험하지 않았다. 고향이 아닌 B의 DRAM으로 내려가 B의 별도 local disk를 쓰는
기능은 없다. 그러려면 disk 복제·소유권 이전을 추가하거나 B에서도 NFS를 계속 사용해야 한다.

Source pause → block drain/fsync 성공 확인 → snapshot → source 종료·PID incarnation fence →
RAM 복사 또는 같은 CXL 연결 → destination restore 순서로 처리한다. Snapshot 준비의 fsync
오류를 기존처럼 로그만 남기고 넘기지 않고 호출자에게 돌려주며, state/RAM snapshot 파일을 쓰기 전에 실패한다.

Promotion과 demotion에서는 **RAM 전체를 stop-and-copy**한다. 2MiB control 영역은 보존하며,
모든 복사 byte를 비교한다. CXL의 A↔B 전환만 RAM 복사 0이다. Demotion 후에는 source들이
종료됐음을 확인하고 이전 CXL 영역을 덮어쓴 뒤 DRAM guest를 재개한다. 이 단계는 일회용
검증용이며 local RAM이 이전 CXL 영역에 계속 의존하는 오류를 탐지한다.

## 실행 결과

| 시험 | 최종 결과 | 독립 확인 |
|---|---|---|
| Writable ext4, 3회 전체 왕복 | **12 transitions · 74 workload commands** | Guest 이벤트 98개, 전체 FC 로그 14개, state 파일 24개 byte 비교 |
| 종료된 실제 disk | **10,494,464 words 전수 비교** | Outer/inner ext4 fsck, UUID·내용 hash 일치, 실제 image 손상 6종 탐지 |
| 실제 trace, local/NFS × cache 유지 3회·회수 1회 | **24 trials · 840 commands · 48 transitions** | 원본 종료값 816개·제출 24개·테스트 요약 72개, patch·열린 FD 일치 |
| Direct unsupported block/2-vCPU | **두 구성 모두 안전하게 거절** | Resume 없이 block cold read / guest counter 진행, owner·generation 불변 |
| 기존 1-vCPU diskless direct | **20 handoffs** | 한 번 부팅, snapshot 0, VM/vCPU/EPT identity 유지 |

Home 시험은 RAM 512MiB를 promotion/demotion 때 총 **3GiB** 복사했고, CXL에 머문 6회 handoff에서는 0 byte다.
Trace 시험은 2GiB RAM을 48번 tier 전환해 총 **96GiB** 복사했다. 모든 native import/export는
전체 byte 비교를 수행했으며 demotion 뒤 CXL poison 검사도 통과했다.
Trace의 기대된 nonzero 종료 48건도 원본과 일치한다. 이 숫자는 실패한 migration의 개수가 아니다.
MicroPython 8회는 기존 conda startup warning의 출력 위치가 달라 raw submission hash가 다르다.
기존 검증 규칙대로 해당 한 줄만 제거하고 출현 횟수는 같음을 확인했으며, 정규화한 patch bytes는 일치했다.
원시 출력도 그대로 보관하므로 임의의 stderr나 test 실패를 숨기는 정규화는 아니다.

독립 verifier에는 home 증거 변조 **31개**, trace 증거 변조 **40개**, direct 증거 변조 **42개**를
넣어 모두 거부됨을 확인했다. 실제 disk bytes를 바꾼 6개 검사는 이 metadata/receipt 변조 검사와 별도다.
RAM helper의 75 unit 검사·19 CLI 오류 입력·ASan/UBSan 검사, 파일 probe의 22 오류 입력,
Firecracker의 fsync 실패·snapshot 미생성·API dispatch 등 선택 회귀 11개도 통과했다.
LeakSanitizer는 실행 환경의 ptrace 제약으로 지원되지 않아 비활성화했으며 해당 실패 기록은 보존한다.

Writable ext4 시험과 실제 개발 trace는 목적과 쓰기 배치가 다르다.

- **Writable ext4:** 수정 파일을 disk에 기록한다. `write`, `fsync`, dirty write, `MAP_SHARED`,
  rename, journal 순서, 열린 파일과 unlink 후 열린 FD를 검사한다. 정상 unmount 후 새 guest로
  부팅하고, 종료된 외부 disk를 `debugfs`로 다시 추출해 독립 Python oracle과 대조한다.
- **실제 trace:** Gin·PHP-CS-Fixer·MicroPython의 기록된 agent 명령을 재생한다.
  RO ext4 lower + guest tmpfs upper를 유지하며 **A/DRAM→B/CXL→A/DRAM**의 두 경계를 거친다.
  수정·빌드 결과와 최종 patch, 열린 FD, 원본 명령 결과가 이어지는지를 검사한다.
  원본 trace의 nonzero 종료도 기대값으로 비교하며, 프로젝트 테스트 전체 성공이나 새 RL 학습을 뜻하지 않는다.

Cache 사용에 따른 **명령 실행 구간의 NFS READ 횟수**도 원시 counter와 대조했다.

| Trace | Cache 유지 3회 범위 | Cache 회수 1회 |
|---|---:|---:|
| Gin | 18–22 | 3,267 |
| PHP-CS-Fixer | 154–158 | 1,925 |
| MicroPython | 379–384 | 3,480 |

Cache miss가 실제 원격 backing 읽기로 이어짐을 보여주는 횟수이며 physical network latency나 speedup을 뜻하지 않는다.

## Direct 거절 처리에서 발견하고 수정한 문제

이전 실험에서는 block 구성을 direct로 이전하려다 HTTP 400을 받은 뒤 새 읽기가 멈췄다.
정지 전 block/다중 vCPU preflight를 추가했지만 최초 실제 실행에서는 문제가 남았다.
API adapter가 실패한 Pause 요청 뒤에도 성공한 Pause와 같이 blocking API 수신 루프에 들어가,
장치 EventManager를 처리하지 않았기 때문이다.

최종 수정은 **성공한 Pause만 정지 루프에 들어가고, 성공한 Resume만 그 루프를 해제**한다.
실제 RuntimeApiController와 EventManager 회귀 테스트에 이어, 새 guest에서 HTTP 400 직후
별도 Resume 없이 cold read가 완료되는지 검사했다. Reuse의 기존 diskless 제한도 유지한다.
알려진 unsupported 구성의 안전한 거절을 검증했으며 임의 kernel export 실패의 완전한 rollback을
구현했다고 주장하지 않는다. Direct로 block workload를 이전하는 다음 단계에는 host FD,
virtqueue, MMIO 처리, ioeventfd/IRQFD와 device worker 소유권 이전이 필요하다.

## Agent RL에서 tmpfs에 수정 내용을 두는 사례

**실제 사례가 있다.** [NVIDIA NeMo-Gym SWE agent 코드](https://github.com/NVIDIA-NeMo/Gym/blob/main/responses_api_agents/swe_agents/app.py)는
Apptainer의 `--writable-tmpfs`를 사용하고, trajectory·결과용 디렉터리는 별도로 bind한다.
[Nemotron 3 Super 보고서 p.30](https://research.nvidia.com/labs/nemotron/files/NVIDIA-Nemotron-3-Super-Technical-Report.pdf#page=30)도
SWE-RL의 writable tmpfs overlay를 설명한다. 현재 main과 보고서 당시 소스가 같은 버전이라고 가정하지 않았다.

[Aleph Alpha의 Kolibri RL 설명](https://aleph-alpha.com/en/blog/why-rl-environments-have-always-been-an-infra-problem/)은
공유 read-only SIF/squashfuse image 위에 sandbox마다 크기 제한이 있는 tmpfs OverlayFS upper를 둔다.
이 글만으로 모든 artifact의 영속 저장 정책까지 알 수는 없다. 두 사례는 container 기반이며
CXL microVM 이전 사례는 아니다. [DSec §5](https://arxiv.org/html/2609.22978v1#S5.SS1)의 writable ext4/OverlayBD는 disk 경로로 구분한다.

우리에게는 **RO base + guest tmpfs upper를 실제 agent workload의 비교 구성으로 유지할 근거**가 된다.
수정량이 작으면 쓰기 상태도 RAM과 함께 옮기기 쉽다. 반면 빌드 산출물·의존성 설치가 커지면
VM RAM 사용량이 늘고, sandbox 종료 전에 patch·결과를 따로 내보내야 한다.
[Apptainer 문서](https://apptainer.org/docs/user/latest/persistent_overlays.html)에서 설명하듯 tmpfs 수정분 자체는 종료 후 영속 저장소가 아니다.
이번 writable ext4 경로는 종료 이후 보존도 필요한 sandbox의 별도 선택지다.

## 검증 범위와 제한

- 한 물리 host 위의 QEMU A/B와 nested KVM, coherent CXL 모사 환경이다. 실제 다중 host의
  비일관 cache 처리, 물리 network 장애와 CXL hardware는 검증하지 않았다.
- Writer는 controller가 이전 process를 종료해 하나로 제한한다. NFS는 `nolock`이며
  분산 lease, partition 이후 중복 실행 차단과 장애 복구를 구현하지 않았다.
- Writable 시험은 guest ext4 `commit=600`, dirty writeback 지연으로 dirty page를 유지한다.
  Global Dirty 수치는 개별 파일의 dirty page 목록이 아니다. 파일 내용 보존은 별도로 비교한다.
- Clean guest/host unmount와 새 boot의 저장 내용 확인이다. 전원 장애나 journal crash recovery,
  `fsync` 응답 뒤 서버 전원 차단의 내구성은 별도 시험이다.
- Source/destination에서 같은 disk bytes와 ext4/FD identity를 확인한다. Virtio block GET_ID는
  host inode/device에서 만들어지므로 local/NFS 사이에서 가상 disk serial까지 동일하다고 주장하지 않는다.
- Serial 제어, 전체 RAM byte 비교, image hash, cold-read 검사를 포함하는 **정확성 시험**이다.
  기록된 시간은 기존 direct의 µs/ms handoff나 이전 serial의 9.31초와 비교하는 성능 수치가 아니다.
- PML 증분 복사·비일관 cache protocol과 MPTCP 연결 이전은 이번 경로에 통합하지 않았다.

## 실패 기록과 재현

최초 direct preflight 수정만으로는 장치 이벤트 정지가 해결되지 않아 실패 실행을 보존했다.
최종 API loop 수정 후 새 VM pair로 다시 실행했다. 최종 build를 실험 이미지에 넣는 임시 포장
스크립트에서도 `/` 권한이 `775→700`으로 바뀐 이미지에서 NFS 초기화가 실패했다. 권한을 낮춘
rpcbind의 경로 접근 실패로 추정하며 syscall 단위 추적은 수행하지 않았다. 원본 권한을 보존하도록
고치고, 원본 대비 Firecracker byte 이외의 archive entry 차이가 없는지 확인한 뒤 새 pair에서 통과했다.
이 실패는 guest workload 시작 전이며 성공 표의 trial에 포함하지 않는다.
기타 unit fixture 설정 실패와 negative-test mutator 수정 내역도 원시 자료에 보존한다.

[전체 요약](summary.json) · [Home report](home-report.json) · [Home audit](home-audit.json) ·
[최종 disk audit](disk-audit.json) · [Trace report](trace-report.json) · [Trace audit](trace-audit.json) ·
[Trace 오류 주입](trace-negative-tests.json) · [Direct audit](direct-audit.json) · [빌드·회귀](build.json) ·
[선행 사례](research.json) · [자원 정리](cleanup.json) · [새 디렉터리에서의 재검증](packaging-audit.json) ·
[원시 자료](raw-evidence.tar.gz) · [최종 disk](final-home-storage.raw.gz) · [파일 hash 목록](manifest.json)

최종 disk archive는 약 80MiB이며 하나의 canonical outer image만 보관한다.
원시 자료에는 full RAM payload나 큰 prepared trace image를 중복 포함하지 않는다.
RAM 동일성은 native full-byte-comparison receipt로 확인하고, 작은 CPU/device state bytes는 archive에서 직접 다시 비교한다.
Runtime 당시 prepared artifact 15개도 확인했으며 큰 prepared image를 가진 경우 trace auditor에 `--artifacts`를 추가한다.

운영 절차는 [구현 README의 home storage](../../README.md#home-storage-lifecycle)와
[trace lifecycle](../../README.md#trace-ram-lifecycle)을 따른다. 원시 자료의 시각은 UTC이며,
원래 측정 경로는 provenance로 유지한다. 새 디렉터리에 푼 archive는 아래 절차로 VM 없이 검증한다.

```bash
mkdir -p /tmp/home-evidence
tar -xzf implementation/virtual_machine/results/home-storage-20261028/raw-evidence.tar.gz -C /tmp/home-evidence
python3 /tmp/home-evidence/audit-tools/verify_home_storage.py \
  /tmp/home-evidence/home/report.json --full-patterns --negative-tests --out /tmp/home-audit.json
python3 /tmp/home-evidence/audit-tools/verify_trace_results.py \
  /tmp/home-evidence/trace/report.json --formal --json-out /tmp/trace-audit.json
python3 /tmp/home-evidence/audit-tools/audit-safe-direct.py \
  /tmp/home-evidence/direct/direct-block /tmp/home-evidence/direct/direct-two-vcpu \
  /tmp/home-evidence/direct/direct-baseline --out /tmp/direct-audit.json
gzip -dc implementation/virtual_machine/results/home-storage-20261028/final-home-storage.raw.gz \
  > /tmp/home-evidence/home-storage.raw
python3 /tmp/home-evidence/audit-tools/audit-home-storage-disk.py \
  --report /tmp/home-evidence/home/report.json --disk /tmp/home-evidence/home-storage.raw \
  --probe /tmp/home-evidence/tools/home-storage-probe --offline-copy --out /tmp/disk-audit.json
```

`--offline-copy`는 현재 host에서 과거 VM의 종료를 다시 입증하지 않는다. 종료 당시의 관측은
`cleanup.json`과 disk audit의 `shutdown`에 남아 있다. Detached copy에서는 filesystem과 실제 저장 내용,
이미지 손상 탐지까지 다시 검증한다.

다음 단계는 명령 실행 중 이전·장기 반복·장애 시 단일 writer 계약, native 제어 경로에 block 연결,
PML과 모든 writer의 cache 처리, MPTCP 연결 이전의 통합이다. 각 단계는 기존 snapshot/disk 검증과
별도로 direct 지원 범위와 성능을 측정해야 한다.
