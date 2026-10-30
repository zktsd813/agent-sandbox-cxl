# 실제 SWE trace의 promotion/demotion 및 두 handoff 검증

264개 기록 중 Gin·PHP-CS-Fixer·MicroPython을 실제 microVM에서 재생했다. **3개 trace × DRAM 대조군/state-only/direct = 9개 조건 모두 통과**했다. 원본 명령 315개, promotion 12회, demotion 12회, handoff 12회를 검증했다.

성공은 원본 command byte·순서·종료 코드·test 결과·최종 제출 패치가 일치한다는 뜻이다. 원래 실패한 명령은 같은 nonzero 종료 코드를 기대한다. 기존 replay 검증기처럼 startup warning을 분리한 제출 fingerprint와 warning 수를 대조하며, SWE 정답 채점을 새로 수행한 결과는 아니다.

## 실행 조건

Guest는 1 vCPU·2GiB RAM이며, 같은 patched Firecracker와 guest kernel을 사용한다. 조건마다 새 A/B pair와 private disk, 16GiB CXL 영역을 만든다. Guest는 RO ext4 lower + tmpfs upper를 유지한다. A는 local image, B host는 같은 이미지의 NFS FD를 virtio-blk에 연결한다.

Lifecycle 조건은 명령 경계에서 **A/DRAM → A/CXL → B/CXL → A/CXL → A/DRAM**을 수행한다. 모든 명령을 마친 뒤 promotion/demotion을 한 번 더 실행한다. State-only는 CPU·장치 상태만 저장·전달하고, direct는 동일 KVM/vCPU 객체를 유지한다. 두 모드의 handoff 자체에서 RAM snapshot/transfer는 0바이트다.

Gin은 CPU 0–7, PHP는 8–15, MicroPython은 16–23을 사용했다. 각 trace의 세 mode는 같은 CPU 집합에서 순서대로 실행했다. Gin direct, PHP, MicroPython suite는 서로 다른 physical core 집합에서 겹쳐 실행됐다. 조건별 1회이며 아래 시간은 이 실행 조건의 관측치다.

## 실제 명령 결과

시간은 guest의 개별 명령 실행 시간 합계이며 전환·검증 시간은 제외한다.

| Trace | 명령 수/조건 | DRAM | State-only | Direct | 원본·대조군과 결과 비교 |
|---|---:|---:|---:|---:|---|
| Gin | 27 | 25.82s | 26.50s | 24.18s | 일치 |
| PHP-CS-Fixer | 35 | 10.04s | 9.73s | 10.90s | 일치 |
| MicroPython | 43 | 15.52s | 16.94s | 18.36s | 일치 |

Guest boot ID·controller PID, 수정된 상태 파일, unlink 후 열린 FD의 내용과 offset도 이어졌다. 최종 demotion에서는 전체 2GiB 논리 RAM을 anonymous DRAM으로 복사하고 CXL private frame grant를 모두 반환했다. 각 이미지와 canonical 데이터는 실행 후 전체 비교·SHA-256 검사를 통과했다.

대조군은 같은 patched VMM의 DRAM 실행이다. 이 표로 pristine Firecracker 대비 overhead나 안정적인 speedup을 추정하지 않는다.

## 자연 발생한 clean lower cache

데이터를 미리 읽어 cache를 채우지 않았다. Trace별 13·14·15개 lower 파일의 현재 resident clean folio를 guest kernel이 등록했다. 두 promotion에서 연결한 canonical 바이트와 B→A 복귀 전 현재 guest cache의 실제 shared PFN을 구분했다.

| Trace / mode | 첫 promotion 공유 | 두 번째 promotion 공유 | 복귀 전 present shared PFN | B 명령 구간 NFS READ |
|---|---:|---:|---:|---:|
| Gin / state-only | 49.24MiB | 49.43MiB | 26.92MiB | 31 |
| Gin / direct | 48.88MiB | 49.02MiB | 48.88MiB | 21 |
| PHP / state-only | 20.64MiB | 20.64MiB | 10.95MiB | 161 |
| PHP / direct | 20.64MiB | 20.64MiB | 20.64MiB | 161 |
| MicroPython / state-only | 30.77MiB | 31.26MiB | 17.04MiB | 372 |
| MicroPython / direct | 30.90MiB | 31.39MiB | 30.90MiB | 369 |

Present PTE는 실제 backing의 증거이며 cache-hit 횟수는 아니다. 마지막 재promotion 직후의 audit에서는 아직 접근하지 않은 lazy mapping 때문에 present shared PFN이 모두 0으로 기록됐다. 등록·mapping 크기를 present 값으로 바꿔 보고하지 않았다.

DRAM 대조군의 최종 `Cached`는 Gin 440.1MiB, PHP 75.1MiB, MicroPython 160.4MiB였다. 여기에는 tmpfs 등도 포함된다. 모든 trace가 100MiB 이상의 순수 lower file cache를 사용했다거나 전체 cache를 공유했다고 해석하지 않는다.

각 조건은 **독립 pool의 VM 하나**다. Canonical image 전체 3~4GiB를 미리 적재했으므로 위 공유 바이트만으로 메모리 절감량을 계산하지 않는다. 여러 VM의 동일 canonical frame 공유와 실제 회수·재사용 증거는 [앞선 전용 검증](../guest-pool-handoff-20261030/README.md)에 있다. 이번 자연 workload에서는 shared→private CoW/discard detach가 발생하지 않았고, 그 동작의 근거도 앞선 전용 시험과 구분한다.

## 전환 시간 관측

아래는 2GiB guest의 L1 Unix-socket API 시간 중앙값이다. 각 cell은 6개 동작의 관측값이다.

| Mode | Promotion API | Demotion API | Handoff의 두 API 합계 |
|---|---:|---:|---:|
| State-only | 2,427ms | 2,201ms | 780ms (`create + load`) |
| Direct | 2,551ms | 1,838ms | 1,155ms (`export + attach`) |

전체 전환에는 guest 등록, metadata 반출, ownership 제어와 검증도 포함된다. 위 API 합계는 end-to-end blackout이 아니다. 모든 명령·왕복·재promotion·audit·결과 export를 포함한 controller replay는 state-only 187~309초, direct 181~280초였다. 큰 등록 목록의 serial 반출과 전수 mapping 검사를 포함하는 기능 검증 도구의 시간이므로 최적화된 handoff latency로 해석하지 않는다. 초기 이미지 준비와 마지막 canonical 검사는 controller replay 시간 밖에 있다.

정확한 값과 operation별 API 시간은 [observations.json](observations.json)에 있다.

## 독립 검증과 원시 증거

[verification.json](verification.json)은 압축 archive를 새 디렉터리에 풀고 다시 검증한 결과다. 전체 9개 Cartesian 조합, 공통 실행 source/kernel/VMM fingerprint, trace별 동일 입력, 원본 및 DRAM 대조군과의 결과 일치를 확인한다. Controller와 admission parser를 import하지 않고 raw serial, guest archive, live authority, 실제 PFN, grant와 process fence를 다시 읽는다.

별도의 [의미적 변조 검사](semantic-negatives.json)는 27건을 모두 거부했다. 잘못된 return code에 맞춰 serial·guest archive·checksum을 모두 고쳐도 원본 oracle과의 불일치를 검출했다. Inode/extent/GPA·dirty flag·live status·private alias·credit alias·RAM transfer 주장·epoch·다른 incarnation의 fence도 검사했다. Parser의 10개 unit test 결과는 [unit-tests.txt](unit-tests.txt)에 있다.

[resource-cleanup.json](resource-cleanup.json)은 성공한 9개 pair와 준비 단계 실패 4개의 PID incarnation fence, TAP/bridge, shared-memory backing 및 소유 private disk 정리를 기록한다. 원본 이미지와 trace asset은 유지한다.

```bash
cd implementation/virtual_machine
(cd results/swe-pool-traces-20261030 && sha256sum -c SHA256SUMS)
SWE_REPLAY_DIR=$(mktemp -d /tmp/swe-pool-replay.XXXXXX)
for name in aggregate gin-baseline gin-lifecycle php-matrix micro-matrix; do
  tar -xJf "results/swe-pool-traces-20261030/$name.tar.xz" -C "$SWE_REPLAY_DIR"
done
python3 verify_swe_pool.py --suite "$SWE_REPLAY_DIR/report.json" \
  --output "$SWE_REPLAY_DIR/reverified.json"
python3 verify_swe_pool_negatives.py \
  "$SWE_REPLAY_DIR/suites/gin-baseline/gin-gonic__gin-3741-baseline/report.json" \
  "$SWE_REPLAY_DIR/suites/gin-lifecycle/gin-gonic__gin-3741-state-only/report.json" \
  "$SWE_REPLAY_DIR/suites/gin-lifecycle/gin-gonic__gin-3741-direct/report.json" \
  --output "$SWE_REPLAY_DIR/negatives.json"
```

`aggregate.tar.xz`에는 통합 manifest와 최종 검증·재현용 source hash가 있고, 나머지 네 archive에는 각 실행의 원본 report·receipt·serial·guest 결과·실제 측정 source가 있다. 이미지와 kernel/VMM binary는 큰 로컬 asset으로 별도 관리하며, digest와 준비 입력은 fixture manifest에 남긴다. [실험 흐름·재현 방법](../../../../docs/cxl-swe-pool-traces.md)을 참고한다.

처음 네 준비 실행은 workload 부팅 전에 중단됐다. 작은 단위의 NFS SHA 읽기가 120초 제한에 걸렸고, 다음 실행은 같은 병목을 확인한 뒤 중단했다. 전체 이미지를 1MiB 단위로 읽어 SHA 검사를 유지하도록 고쳤다. 이어 symlink의 size 검사와 canonical 도구의 `O_NOFOLLOW` 제약을 해결하기 위해 같은 inode의 hardlink를 사용했다. [preparation-retries.tar.xz](preparation-retries.tar.xz)에 이 기록을 보관하며, 성공한 9개 workload 결과에 섞지 않는다.

측정 후 runner가 독립 검증기의 보조 source 두 개도 freeze하도록 보강했다. 측정 당시 runner는 각 suite에 그대로 보관했다. Guest 실행 controller, parser, fixture, kernel/VMM binary는 9개 조건에서 동일하다. 검증기만 Gin baseline 이후 공통 입력 비교를 보강했고, 마지막 archive 재검증에는 같은 최종 검증기를 사용했다.

환경은 한 physical host의 coherent shared memory를 쓰는 nested KVM이다. 실제 비일관 CXL, 명령 실행 도중의 handoff, 여러 real-trace VM의 공동 pool 용량 절감, pooling과 PML/MPTCP의 통합은 이번 결과에 포함하지 않는다.
