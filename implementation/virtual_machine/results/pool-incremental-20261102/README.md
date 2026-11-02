# Pooling handoff의 반복 RAM 순회 제거 — 2026-11-02

[설계와 필요한 순회 범위](../../../../docs/cxl-pool-incremental-handoff.md)를 함께 읽는다. CPU만 넘기는 CXL→CXL handoff와 DRAM↔CXL 데이터 이동을 별도로 집계한다.

## 검증 환경과 범위

- 한 물리 호스트의 Linux 5.15.0-198-generic, 두 독립 L1 Linux 6.6.0-rc6, nested VMX/KVM, coherent QEMU shared backing. 실제 비일관 CXL 다중 물리 노드 시험은 아니다.
- 두 모드마다 새로운 A/B pair와 독립 sandbox 3개를 만들었다. 각 sandbox는 1 vCPU·512MiB RAM·160MiB의 실제 guest file page cache를 사용했다.
- Synthetic 최종 결과는 direct의 promotion 6·demotion 6·handoff 4회, state-only의 promotion 9·demotion 9·handoff 12회다. 총 46회 전환이며 allocator 반환과 QEMU/memory backing cleanup까지 확인했다.
- 두 모드에서 stale handle/lease/epoch/generation, 중복 import, 슬롯 재사용, credit rollback을 검사했다. NFS cold refill, 실제 OverlayFS copy-up, canonical 불변성, discard ACK 이후 144MiB 이상의 실제 재사용도 원시 증거로 확인했다.
- Synthetic은 `--full-audit`를 명시하여 별도 PTE/PFN·읽기 전용 관찰과 guest 전체 payload oracle을 수행했다. Handoff 전후 `full_scan_pages`의 증가는 모든 인계에서 0이다. 이 stress audit와 guest oracle은 정상 handoff에 필요한 작업이 아니다.
- SWE는 원본 Gin trace의 27개 명령을 사용한다. 2GiB·1 vCPU이며 `--full-audit`를 전달하지 않는다. 이미지·명령·최종 submission 검증과 자연 발생한 lower cache 등록은 유지한다. 이번 SWE 결과는 한 trace의 두 모드이며, 과거 3 trace × 3 mode 전체를 다시 측정했다는 뜻이 아니다.

## 시간의 의미

`summary.json`의 handoff 시간은 state-only의 create+load API 또는 direct의 export+attach API 합이다. Source fence, 목적지 프로세스 launch, serial 전송, guest 응답 관측은 포함하지 않는다. Direct kernel detach+attach는 해당 API 시간의 일부이며 다시 더하면 안 된다. Promotion/demotion API는 별도 필드다.

512MiB stress의 API 중앙값은 direct **18.26ms**(4 samples), state-only **22.66ms**(12 samples)다. Direct kernel 구간 중앙값은 **11.09ms**다. 처음부터 장치 없이 준비한 과거 sub-10ms handoff와 동일한 조건은 아니다.

2GiB SWE의 같은 API 범위와 과거 Gin 결과 비교는 아래 최종 측정 표를 따른다. 과거 결과와의 비교는 무작위 교차 실험이나 pristine Firecracker 대비 성능 주장이 아니다.

| 2GiB Gin, 각 모드 2회 인계 | 과거 API 중앙값 | 이번 API 중앙값 | 이번 manifest |
|---|---:|---:|---:|
| state-only | 798.88ms | 22.07ms | 758–762 bytes |
| direct | 1170.84ms | 11.66ms | 695–699 bytes |

이번 direct의 kernel detach+attach 중앙값은 **2.48ms**다. 원본 명령 54개와 SWE 전환 12회가 통과했다. Synthetic을 합하면 최종 **58회 전환(20회 CPU handoff)**다. SWE에서 관찰한 모든 handle의 `full_scan_pages`는 최초 admission의 524,288에서 증가하지 않았다. Promotion/demotion은 실제 RAM 복사를 수행하며 API 시간이 약 1.7–3.3초인 구간으로 별도 기록한다.

## 원시 자료와 재검증

- `synthetic.tar.xz`: 최종 direct/state-only 원시 receipts, 보고서, 실행 시점 소스, verifier 결과, fixture·binary hash와 cleanup 기록.
- `swe-gin.tar.xz`: 최종 2GiB Gin 두 모드의 원본 trace·명령 결과·guest archive·보고서·소스·독립 verifier 결과.
- `diagnostic-direct.tar.xz`: 최종 측정 전의 direct 시험. 임시 paused EPT 대조를 포함하며 최종 timing 표에는 합치지 않는다.
- `diagnosis/`: 실패 원인별 기록, root 보완 이전 triple fault/데이터 불일치 로그, 과거 API 시간 원시 추출. 실패한 시험을 통과한 표본에 넣지 않았다.
- `build/`: incremental kernel/VMM/mapper/guest build와 test 로그, L1/guest 설정, 최종 binary/patch hash. Python 63개, Rust device preparation 4개·direct contract 3개·pool memory 15개가 통과했다. 최종 VMM 재빌드 뒤 pool memory 15개를 다시 실행했다.
- `SHA256SUMS`: 압축 원시 자료의 digest. 데이터가 큰 mapping admission 계획을 포함하므로 압축해서 보관한다.

저장소 root에서 새 임시 디렉터리에 풀고 같은 verifier로 다시 읽을 수 있다.

```bash
evidence=$(mktemp -d /tmp/pool-incremental-verify.XXXXXX)
lab=implementation/virtual_machine
results=$lab/results/pool-incremental-20261102
tar -xJf "$results/synthetic.tar.xz" -C "$evidence"
tar -xJf "$results/swe-gin.tar.xz" -C "$evidence"
python3 "$lab/verify_pool_handles.py" "$evidence/synthetic/direct/report.json"
python3 "$lab/verify_pool_handles.py" "$evidence/synthetic/state-only/report.json"
python3 "$lab/verify_swe_pool.py" --suite "$evidence/swe-gin/report.json"
python3 "$results/summarize.py" --synthetic "$evidence/synthetic" \
  --swe "$evidence/swe-gin" --output "$evidence/summary.json"
```

실행 재현에는 기존 fixture 빌드 절차에 ABI 2 mapper와 최신 KVM/Firecracker patch를 적용한다. 새 smoke helper도 정적으로 빌드하고 `pool_handoff_prepare.py --extra-tool`로 L1 fixture에 포함한다.

```bash
gcc -std=c11 -O2 -static -Wall -Wextra -Werror \
  implementation/virtual_machine/pool_memory_handle_smoke.c \
  -o /tmp/pool-memory-handle-smoke
# pool_handoff_prepare.py의 기존 인자에 추가:
# --extra-tool /tmp/pool-memory-handle-smoke
```

각 suite의 `report.json.arguments`, `commands`/`start`/`bench`, fixture manifest에 실제 실행 인자와 입력 hash가 있다. 기존 output/session을 덮어쓰지 않고 새 이름으로 실행한다. 외부 QEMU/KVM 및 root가 필요한 disposable lab이다. Direct는 공유 KVM arena 하나당 선택된 VM/vCPU 한 개를 지원하며, 동시에 실행하는 나머지 sandbox는 일반 KVM 경로를 사용한다.

## 알려진 범위

전수 PFN prefault를 없애자 nested KVM에서 오래된 shadow EPT에 의한 것으로 판단되는 데이터 불일치/triple fault가 나타났다. Guest TLB flush만으로는 해결되지 않았다. 실험용 `dax_nested_root_sync=1`은 retained root의 한 byte에 같은 값을 써서 외부 KVM의 shadow root 무효화를 유도한다. 최종 시험에서는 임시 EPT 순회 진단 코드를 제거했으며 이 root 보완만 남겼다. 이는 현재 외부 KVM 동작에 의존하는 lab 보완이고 물리 CXL coherence 프로토콜이 아니다.

Mapper table은 VM 사이에 공유하는 guest metadata가 아니다. 각 VM의 실행 owner가 관리하는 별도 host metadata다. 2GiB VM/16GiB device 기준 실제 약 16.5MiB와 예약 extent 64MiB/VM의 비용이 추가된다. SWE fixture의 canonical 이미지는 전체 크기를 preload하고 setup/final 검증에서 전체 이미지를 읽는다. 이 작업을 handoff 시간이나 실제 절감한 데이터 용량으로 계산하지 않는다.
