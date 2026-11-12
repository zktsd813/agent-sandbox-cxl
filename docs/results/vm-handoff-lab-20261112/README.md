# VM hand-off lab 재검증 (2026-11-11~12, bud05)

코드 정리(branch `vm-cleanup`) 뒤 nested lab을 bud05에 새로 세우고 hand-off를 다시 쟀다. 05는 1차 정리 뒤(commit `243b1b0`), 07은 2차 정리 뒤(commit `9629e46`) 결과다. 해석은 [RESEARCH.md](../../RESEARCH.md) 3장에 있다.

## 환경

- bud05, Xeon Gold 6548Y+, NUMA 0(CPU 0-31)만 사용.
- 물리 host 한 대 안에 QEMU L1 A/B(file-backed emulated CXL Type-3), 그 안에서 Firecracker L2 sandbox.
- Intel direct L1은 XFD/AMX를 숨긴 CPU 모델(`-cpu host,vmx=on,xfd=off,amx-tile=off,amx-bf16=off,amx-int8=off`).
- network는 socket backend. 이전 기록(다른 서버, TAP)과 바로 비교할 수 없다.
- 실물 CXL, 물리 multi-host, 비일관 coherence는 이 측정 범위 밖이다.

## 파일

| 파일 | 내용 |
|---|---|
| `lab-presets-07.json` | preset(snapshot, reuse, reuse-prepared, 128MiB guest, 10 warmup/100 iterations)과 direct(128MiB Linux guest, 2MiB CPU-only guest, 6 warmup/30 iterations)의 구간별 p50/p95 |
| `swe-summary-07.json`, `swe-summary-05.json` | SWE trial별 hand-off·promotion·demotion 원시 값, kernel trace, warm 통계, 실행 중 hand-off 관측 |
| `warm-latency-analysis-07.json` | Gin warm hop별 send/receive/relay/network 분해 |
| `code-size-07.json` | 2차 정리 뒤 자체 소스와 patch 규모 |

원시 run 디렉터리(`implementation/virtual_machine/.local/runs/`)는 Git 밖이다.

## 결과 요약

hand-off(ms, source pause 요청 → remote guest ACK):

| 모드 | p50 | p95 |
|---|---:|---:|
| snapshot | 8.53 | 12.38 |
| reuse | 9.01 | 9.49 |
| reuse-prepared | 1.40 | 1.52 |
| direct, 128MiB Linux (재측정) | 0.57 | 0.71 |
| direct, 2MiB CPU-only guest | 0.21 | 0.22 |

SWE(Gin, PHP-CS-Fixer, MicroPython × baseline/state-only/direct + trace·inflight·warm trial): 05 15 trial, 07 12 trial 모두 독립 검증 통과. 07 재실행 3 trial(warm, state, trace)은 `swe-summary-07.json` 밖이고 표의 "07 재실행" 열에만 있다.

| Gin direct, 무계측 | 05 | 07 | 07 재실행 |
|---|---:|---:|---:|
| 처음 B | 3.05 | 2.69 | 2.48 |
| warm p50 (38 hop) | 1.95 | 1.36 | 1.34 |
| warm p95 | 2.33 | 2.08 | 1.87 |
| promotion 직후 B | 6.02 | 7.51 | 8.02 |
| warm 최대 | 2.50 | 2.33 | 40.97 |

- 07 warm 평균이 0.435 ms 준 것의 약 65%는 source send API 감소다. 어느 삭제 때문인지는 분리하지 않았다.
- promotion 직후 B의 증가는 거의 nested 전용 EPT root 보완 구간이다(kernel trace: 05 3.47 → 07 4.68 → 재실행 5.42 ms). 물리 CXL 지연으로 환산하지 않는다.
- 40.97 ms hop은 그중 40.07 ms가 API 밖 network/control 대기다. 원인은 확정하지 못했다.
- state-only(controller wall time)는 43~100 ms이고, 첫 hop에 약 40 ms 대기 구간이 있다.
- Gin `go test ./...`(index 12, 3.69초) 실행 중 B→A→B로 옮겨도 같은 PID·start_ticks로 계속 돌았다.
- RAM 전체(2 GiB) promotion 첫 회 2.10~2.18초, 두 번째 1.04~1.13초. demotion 1.11~1.48초. Gin state-only 첫 demotion이 한 번 2.69초였고, 같은 입력으로 다시 돌리면 1.23초였다.
