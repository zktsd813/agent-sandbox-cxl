# EPT adopt

작업 트리의 hand-off 경로는 `ept-adopt` 하나다. CXL에는 guest RAM과 EPT 표만 둔다.
VMCS, KVM·vCPU 객체, PML page, dirty bitmap, EPT 장부는 각 host의 기본 DRAM 할당을 쓴다.
`--baseline`은 같은 준비 worker와 상태·장치 복원 코드에서 빈 로컬 EPT를 다시 채우는 비교 스위치다.

이전 모드와 증거 파일은 annotated tag `vm-pre-ept-adopt-cleanup-20261119`에 있다.
기존 `vm-pre-cleanup-20261111`도 유지한다. 태그는 push하지 않았다.
`docs/results/`의 과거 결과와 `.local/` 원시는 보존한다.

| 구성 | 파일 |
|---|---|
| EPT 표 할당·release/adopt, RAM mapper 검증 | `kvm-dax-state.patch` |
| 준비 worker, 상태 직렬화·장치 복원, cache 정리 | `firecracker.patch` |
| guest RAM mapper·lease | `pool_memory_module.c`, `pool_memory_uapi.h` |
| 두 L1의 CXL·네트워크 구성 | `lab.py`, `guest-init.sh`, `guest-cxl.sh` |
| 부팅·promotion, EPT 시험 | `handoff_bench.py`, `ept_adopt.py` |
| 원본 SWE fixture·실행·독립 검증 | `trace_prepare.py`, `swe_handoff_prepare.py`, `swe_handoff_run.py`, `verify_swe_handoff.py` |
| L0 정답지 | `dirty_oracle.py`, `page_hash.c` |

`resident_control.c`는 기존 lab의 native packet 운반과 guest ACK 관찰만 한다.
`resident_fast.py`와 `resident_proxy.py`는 그 client다. Proxy 프로세스는 없다.
`control_rpc*`는 SSH 없는 L1 initramfs의 명령·파일·API 채널이다.
모두 lab용이다. 새 daemon이나 host별 공유 객체를 추가하지 않았다.
읽기 전용 block backend는 L1 A의 NFSv3 export로 B에도 공급한다.
`nfs_dependencies.sh`는 그 runtime을 준비한다. 개인 storage는 작은 qcow2 overlay다.

원본은 Firecracker `95f868c8`, Linux DCD 6.6.0-rc6, QEMU v10.2.2다.
소스 출처는 `kernel-source.json`, `ram-backend-source.json`, `kvm-dax-source.json`에 있다.
커널은 `cxl-static-capacity.patch` → `kvm-dax-state.patch` 순서다.
Firecracker 원본에 `firecracker.patch`, QEMU 원본에 `qemu-cxl-direct-ram.patch`를 적용한다.
`patch --dry-run --fuzz=0 -p1`로 먼저 확인한다.
`build.sh --help`의 소스·출력 경로를 지정해 빌드한다.
`CXL_LAB_FC_SOURCE`, `CXL_LAB_KERNEL_SOURCE`, `CXL_LAB_KERNEL_BUILD`,
`CXL_LAB_KERNEL_MODULES`로 독립 tree를 고른다.
`build.sh test`는 Python 단위 시험, C 빌드, 관련 Rust 계약을 실행한다.
SWE 이미지는 `swe_handoff_prepare.py --help`의 입력으로 새 디렉터리에 포장한다.

이 서버에서 정리 뒤 빌드한 자료로 실행하는 예다. `/dev/kvm`, `numactl`, `qemu-img`가 필요하다.

```sh
V=implementation/virtual_machine
numactl --cpunodebind=0 --membind=0 python3 "$V/swe_handoff_run.py" \
  --assets "$V/.local/run22/assets3" \
  --qemu "$V/.local/qemu-cxl/build/qemu-system-x86_64" \
  --session my-ept-check --workload dirty --vcpus 2 --iterations 200 \
  --dirty-oracle --page-hash "$V/.local/run22/page-hash" \
  --output "$V/.local/my-ept-check"
```

`--workload`를 빼면 기본 Gin fixture의 원본 무거운 명령 앞에서 이동한다.
`--preclean --clean-wait 1`은 미리 정리, `--no-cache-maintenance`는 RAM·EPT 정리 해제다.
`--baseline`과 `--count-faults --dirty-lines 64`로 이미 매핑한 payload의 EPT violation을 비교한다.
독립 SWE 검증은 `python3 verify_swe_handoff.py <trial/report.json>`이다.

Runner는 node 0 여유가 lab 예산 + 4 GiB 이상인지 확인하고 LAB → QUIET 순서로 잠근다.
`--measure`는 QUIET 독점 잠금과 부하 기록을 켠다. 빌드는 공유 QUIET, nice 19, 최대 16 jobs로 한다.
생성한 VM과 `/dev/shm` backing만 종료·정리한다. 결과 파일은 남긴다.
설계는 [EPT_ADOPT.md](EPT_ADOPT.md), cache 순서는 [NONCOHERENT.md](NONCOHERENT.md),
실측은 [결과 문서](../../docs/results/ept-adopt-20261119/README.md)에 있다.

2 MiB chunk의 데이터와 최하위 PT를 로컬 DRAM으로 올리는 시험은 새 kernel·mapper·Firecracker가 필요하다.
이 서버의 최종 검증 자료는 `.local/run23/assets7`이다. 위 runner에서 이 assets를 고르고
`--tier-chunks 16 --tier-start 64 --tier-refault lazy --dirty-lines 64`를 더한다.
`--tier-refault eager`는 512개 leaf를 먼저 채운다. `--tier-pre-demote`는 hand-off 전에 미리 내린다.
`--tier-chunks half` 또는 `written --tier-history <이전 report.json>`으로 명시적으로 고른다.
비용과 lab 한계는 [EPT 나누기 결과](../../docs/results/ept-tier-split-20261119/README.md)에 있다.
