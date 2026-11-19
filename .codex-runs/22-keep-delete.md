# 22 남김 / 지움 표

정리 전 HEAD: `04ef44aca7f6c7496ccefa59de7fd1fdb45968fa`.
보존 annotated tag: `vm-pre-ept-adopt-cleanup-20261119` (push하지 않음). 기존 `vm-pre-cleanup-20261111` 유지.

판단 기준: CXL에는 RAM·EPT 표만, 나머지는 로컬 기본 할당. 빈 EPT는 `ept-adopt --baseline` boolean이다. `.local/`과 `docs/results/`의 기존 증거는 삭제하지 않는다. 다른 세션 파일은 수정하지 않는다.

## 파일별 결정

Git 추적 파일 전부를 열거했다. `.local/`, `__pycache__`는 생성 자료다.

| 파일 (`implementation/virtual_machine/` 기준) | 결정 | 이유 |
|---|---|---|
| `.gitignore` | 남김 | 빌드·원시 자료 제외 |
| `EPT_ADOPT.md` | 줄임 | 남은 EPT 경로·빈 EPT 스위치만 설명 |
| `MULTI_VCPU.md` | 지움 | 옛 direct 다중 vCPU 문서; EPT 문서로 통합 |
| `NONCOHERENT.md` | 줄임 | RAM/EPT cache 순서·선정리만 설명 |
| `README.md` | 줄임 | 단일 경로·실행·보존 태그 설명 |
| `ack-ept-breakdown.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `analyze_multi_vcpu_followup.py` | 지움 | direct 후속 결과 분석 |
| `analyze_resident_direct.py` | 지움 | direct 결과 분석 |
| `analyze_resident_preparation.py` | 지움 | direct 준비 분석 |
| `build.sh` | 줄임 | prefault overlay·direct 시험 제거, 남은 빌드·시험 |
| `cache-maintenance-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `coarse-prepare-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `context-switch-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `control_rpc.c` | 줄임 | L0→L1 lab 명령/HTTP/packet 제어; SSH 없는 initramfs의 lab 전용, 모드 분기 제거 |
| `control_rpc.py` | 줄임 | 기존 L0→L1 lab 제어 client; 남은 작업만 |
| `cxl-static-capacity.patch` | 남김 | DCD 원본 커널의 lab CXL 구성 보완 |
| `dax-size-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `direct-handoff-optimized-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `dirty_log_full.patch` | 지움 | 20 dirty-log 꼬리 진단 selftest |
| `dirty_log_live.patch` | 지움 | 20 dirty-log 꼬리 진단 selftest |
| `dirty_oracle.py` | 줄임 | RAM 합집합 영수증·EPT hash 정확성만; 공유 VM 객체 회계 제거 |
| `ept_adopt.py` | 줄임 | 단일 모드와 baseline boolean; direct/reuse-prepared 이름 제거 |
| `experiment_sources.py` | 남김 | 측정 소스 commit·hash 결박 |
| `firecracker-prefault.patch` | 지움 | state-only prefault 실험 |
| `firecracker.patch` | 줄임 | EPT export/import·준비 worker·상태/장치 복원·RAM mapper/cache만 |
| `full-migration-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `guest-cxl.sh` | 줄임 | 에뮬레이트 CXL 구성·동일 물리 배치 lab 전용 |
| `guest-init.sh` | 줄임 | EPT lab 부팅·PML·control만 |
| `guest-swe.config` | 남김 | SWE guest 구성 |
| `handoff-state-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `handoff_bench.py` | 줄임 | EPT runner가 쓰는 DRAM boot·promotion·명령/파일 helper만 |
| `heartbeat-attribution-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `host-cxl-residency.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `host-reference-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `io_mix.c` | 지움 | direct 혼합 I/O fixture |
| `kernel-source.json` | 남김 | 커널 원본 출처 |
| `kernel.config` | 남김 | L1/L2 kernel 구성 |
| `kernel_handoff_trace.py` | 지움 | direct kernel 꼬리 계측 |
| `kvm-dax-source.json` | 줄임 | 남은 patch의 원본·hash·ABI만 |
| `kvm-dax-state.patch` | 줄임 | VM별 EPT 표 할당/release/adopt·PFN 검사·nested lab 보완만 |
| `kvm-prefault.patch` | 지움 | state-only prefault 실험 |
| `l2-init.sh` | 줄임 | 남은 boot smoke만 |
| `lab.py` | 줄임 | prepare/start/verify/stop/configure/ept-adopt-bench만; 옛 모드·네트워크 실험 제거 |
| `latency-refinement-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `mode_compatibility.py` | 지움 | 삭제 모드 비교 |
| `multi_vcpu.py` | 지움 | direct 전용 다중 vCPU 실험 driver |
| `network-fairness-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `nfs-l2-init.sh` | 지움 | 삭제 NFS 비교 실험 |
| `nfs_bench.py` | 줄임 | SWE/EPT가 호출하는 API·pid·stop helper만; NFS 실험 삭제 |
| `nfs_dependencies.sh` | 지움 | 삭제 NFS 비교 준비 |
| `nfs_probe.c` | 지움 | 삭제 NFS 비교 probe |
| `page_hash.c` | 남김 | L0 RAM page hash 정답지 |
| `physical_ept_cost.c` | 지움 | 삭제 모드 EPT 비용 측정 |
| `pointer-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `pool_memory_module.c` | 줄임 | EPT가 쓰는 guest RAM mapper·lease·promotion만; direct 연계/진단 제거 |
| `pool_memory_smoke.c` | 지움 | 삭제 모드 mapper 단독 실험; 실제 EPT 정확성/계약으로 검증 |
| `pool_memory_uapi.h` | 남김 | guest RAM mapper lease ABI |
| `prefault_probe.c` | 지움 | prefault 비교 전용 |
| `qemu-cxl-direct-ram.patch` | 남김 | L1 사이 같은 CXL 물리 주소 배치의 QEMU v10.2.2 보완 |
| `ram-backend-source.json` | 줄임 | 원본 버전·남은 RAM 경로 출처만 |
| `resident_bootstrap.c` | 지움 | direct import 전용 프로세스 준비 |
| `resident_control.c` | 줄임 | 기존 native packet 운반·ACK 시간 경계·선정리 bridge만. 추가 daemon 없음; host별 공유 구조·owner selector·retire·감사·꼬리 계측 제거 |
| `resident_fast.py` | 줄임 | 기존 prepared worker의 packet 운반 client만; direct·parking·retire·재사용·꼬리 진단 제거 |
| `resident_prepare.py` | 지움 | direct import 준비 전용 |
| `resident_proxy.py` | 줄임 | control_rpc의 기존 packet relay client만; 별도 proxy 프로세스 아님 |
| `resident_wire.h` | 줄임 | EPT native packet 운반에 필요한 wire ABI만 |
| `results/backend-ablation-20261103/allocator-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/backend-ablation-20261103/artifacts.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/backend-ablation-20261103/evidence-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/backend-ablation-20261103/summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/backend-ablation-20261103/verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/cache-pml-20261021/cache-maintenance-data.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/cache-pml-20261021/cache-maintenance.pdf` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/cache-pml-20261021/cache-maintenance.png` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/cache-pml-20261021/manifest.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/cache-pml-20261021/plot.py` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/cache-pml-20261021/summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/backend-rpc1-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/backend-rpc2-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/backend-rpc3-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/backend-rpc4-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/cxl-actiontiming-build.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/cxl-inflight-evidence-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/cxl-seen-head-kernel-build.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/kernel-trace-before-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/rpc1-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/rpc2-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/rpc3-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/rpc4-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/swe-inflight-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/swe-nokick-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/swe-relay-ab-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/swe-rpc1-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/swe-rpc2-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/control-transport-20261103/swe-rpc3-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/dax-handoff-comparison.pdf` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/dax-handoff-comparison.png` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/figure-source.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/migration-three-way-1s.pdf` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/migration-three-way-1s.png` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/plot-data.csv` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/plot.py` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/three-way-figure-source.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/dax-scale-20261020/three-way-plot-data.csv` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/README.md` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/analyzer-negative-checks.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/archive-reverification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/archives.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/baseline-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/baseline-build.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/baseline-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/build-inputs.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/cached-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/cached-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/codeopt-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/codeopt-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/final-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/historical-reference.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/optimized-build.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/patch-reproduction.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/previous-two-samples.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/python-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-api-20261102/virtio-block-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/README.md` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/archive-reproduction.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/archives.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/build-inputs.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/build.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/development-runs.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/diagnostic-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/evidence-negative-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/final-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/final-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/fixture-build.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/fresh-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/patch-reproduction.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/post-verification-cleanup.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/python-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/rust-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-20261102/summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/README.md` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/archives.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/baseline-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/diagnostic-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/diagnostic-evidence-negative-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/diagnostic-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/evidence-negative-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/final-attribution.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/final-suite-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/direct-resident-preparation-20261102/input-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/README.md` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/audit-overlap-incident.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/cleanup.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/handoffs.csv` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/mailbox-audit-fixtures.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/mailbox-unit-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/manifest.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/mode-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/mode-unit-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/native-mailbox-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/packaging-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/state-mode-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/trace-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/trace-negative-tests.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/mode-compatibility-20261027/trace-summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/network-fairness-20261020/figure-source.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/network-fairness-20261020/latency-data.csv` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/network-fairness-20261020/migration-breakdown-tap.pdf` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/network-fairness-20261020/migration-breakdown-tap.png` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/network-fairness-20261020/plot.py` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/allocator-unit.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/archive-replay.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/artifacts.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/backend-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/build.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/direct-unit.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/evidence-tests.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/mapper-unit.log` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/source-audit.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/summary.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/swe-profiled-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `results/runtime-overhead-20261103/swe-unprofiled-verification.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `semantic-snapshot-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `shared_probe.c` | 줄임 | EPT가 쓰는 KVM API·CXL boot smoke만; migration/direct/prefault/공유 객체 측정 제거 |
| `snapshot-state-inventory.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `swe-handoff-guest-run.sh` | 남김 | 원본 SWE 명령 실행·독립 결과 추출 |
| `swe-handoff-l2-init.sh` | 남김 | SWE guest 부팅 |
| `swe_handoff_prepare.py` | 줄임 | EPT SWE fixture 준비만; 삭제 도구 빌드 제거 |
| `swe_handoff_run.py` | 줄임 | 단일 EPT 경로와 빈 EPT 스위치로 runner 단순화 |
| `tail_probe.py` | 지움 | perf/schedstat 꼬리 진단 |
| `test_control_rpc.py` | 줄임 | 남은 lab command/HTTP/packet relay 계약 |
| `test_dirty_oracle.py` | 줄임 | 남은 정답지 계약 |
| `test_experiment_sources.py` | 남김 | 측정 소스 계약 시험 |
| `test_handoff_control.py` | 줄임 | RAM promotion 및 EPT launch 계약만 |
| `test_handoff_evidence.py` | 줄임 | 남은 SWE 독립 검증 계약만 |
| `test_multi_vcpu.py` | 지움 | direct 전용 증거 계약 |
| `test_resident_evidence.py` | 지움 | direct 공유 객체 증거 |
| `test_resident_owner.py` | 지움 | direct owner selector·pidfd 감사 |
| `test_resident_proxy.py` | 지움 | direct relay 비교 전용 시험; 남은 relay 계약은 control_rpc 시험에 보존 |
| `test_swe_inflight_evidence.py` | 지움 | direct 실행 중 이동 증거 |
| `trace_prepare.py` | 지움 | 옛 단독 trace driver; 필요한 fixture 준비는 swe_handoff_prepare로 이동 |
| `used-kvm-reuse-comparison.json` | 지움 | 옛 비교·감사·성능 증거. 보존 태그에서 열람; docs/results 기록은 보존 |
| `verify_swe_handoff.py` | 줄임 | EPT SWE 결과·상태/표 hash 검증만; direct/resident/state-only 감사 제거 |

## KVM patch 기능별 결정

| 기능 덩어리 | 결정 | 이유 |
|---|---|---|
| VM별 EPT 표 bump·로컬 장부·release/adopt ioctl | 남김 | ept-adopt 핵심. 표 bytes 불변 |
| EPT span PFN admission·mapper provider | 줄임 | EPT가 쓰는 RAM 주소 검증만; direct 공유 arena 경로 삭제 |
| nested root sync·PFNMAP fault 보완 | 줄임 | 중첩 lab에서 EPT와 mapper가 쓰는 보완만; lab 전용 표시 |
| VMCS/KVM/vCPU/PML/bitmap/run/EPT 장부 allocator·free | 지움 | 모든 객체를 커널 기본 로컬 DRAM 할당으로 되돌림 |
| detach/attach/reattach·mode2/3·VM object arena·owner selector | 지움 | direct 전용 |
| 비소유 진입 counter/guard·공유 refcount·RCU 지연 | 지움 | release 뒤 공유 표 참조를 제거하는 EPT 경로에 불필요 |
| retire·allocation_id·backlink·parked object·reuse ACK | 지움 | 공유 객체 수명 관리 전용 |
| SPTE 원인 counter·dirty clear 진단·line hash 회계 | 지움 | 20 진단 전용 |
| EPT 사용 구간 cache 영수증 | 남김 | RAM/EPT 실제 발행 범위 정확성 검증 |

## Firecracker patch 기능별 결정

| 기능 덩어리 | 결정 | 이유 |
|---|---|---|
| EPT packet·release/adopt·빈 EPT boolean | 남김 | 동일 상태/장치 경로의 대조군 |
| 준비 worker·public KVM 상태 직렬화·TSC 복원 | 줄임 | reuse 모드 이름·snapshot API 경로 제거, EPT helper로 이동 |
| 읽기 전용 block·serial/ACPI 장치 복원 | 줄임 | EPT가 쓰는 준비된 장치 복원만 |
| RAM mapper lease·고정 HVA·DRAM→CXL promotion | 줄임 | EPT fixture가 실제 쓰는 가지; demotion·옛 snapshot 연동 제거 |
| KVM∪device dirty log·clwb·발행·invalidate·선정리 | 줄임 | 핵심 순서·on/off·영수증·오류 주입 유지 |
| DirectExport/Import·resident direct·attach_fd/token·owner 검사 | 지움 | 공유 KVM 객체 직접 이전 제거 |
| snapshot/state-only/reuse/reuse-prepared 모드/API/env | 지움 | EPT가 쓰는 상태 복원만 별도 helper로 보존 |
| 공유 VMM heap allocator·direct native dispatch | 지움 | EPT에는 로컬 heap와 로컬 VM만 |
| clean-helper thread·manual-protect 잔재·schedstat/perf/line hash | 지움 | 20 진단/최적화 전용 |
| native packet dispatch | 줄임 | 기존 worker export/import/clean만; 새 daemon 없음 |

## 실행·검증 경계

기능별 커밋 전에 해당 C/kernel/Firecracker 빌드와 Python·Rust 계약 시험을 통과시킨다. 원본에 fuzz 0 dry-run을 확인한다. node 0·lab/quiet 잠금을 지키며 새 binary로 256 MiB 전체 쓰기·2 vCPU·200왕복·SWE heavy·EPT violation·ABAB 각 50회를 검증한다.

기존 native transport bridge는 VM별 packet 운반과 guest ACK 시간 경계 때문에 최소한만 남긴다. `control_rpc`는 L0에서 SSH 없는 L1 initramfs에 명령을 전달하는 lab 채널이며 제품 daemon이 아니다. `resident_proxy.py`는 이 채널의 client 함수이며 별도 프로세스를 띄우지 않는다. 정확성 시험은 선택적 hash gate로 멈춘 경계에서만 표를 읽는다.
