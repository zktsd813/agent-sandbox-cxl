# FragVisor 대응 경계 재측정 — 2026-11-12

bud05(Xeon Gold 6548Y+), host kernel 6.6.0-rc6-niagara-lreclaim-20260923,
QEMU 10.2.2 L1 A/B, L1/L2 Linux 6.6.0-rc6-dcd-v3, Firecracker 1.17.0.
모든 실행·빌드·분석은 host CPU **32–63 / NUMA memory node 1**에 제한했다.
모드마다 fresh pair를 사용했다. L1은 각각 2 vCPU·2 GiB이며,
공유 backing 포함 최대 예산은 **8,256 MiB**다. socket backend만 허용했다.

경계는 source의 마지막 VM-exit 후 `__vmx_vcpu_run()` 반환 직후 →
destination의 첫 `__vmx_vcpu_run()` 호출 직전이다
([kernel patch:853·857](../../../implementation/virtual_machine/kvm-dax-state.patch#L853)).
실제 VM-entry 명령까지의 assembly prologue/epilogue는 남는다.
controller 요청→exit와 entry→ACK는 경계 밖이다. CPU guest는 요청 전에 HLT로 이미 나와 있다.
기존 `handoff`는 source의 제어 시작→remote ACK 관측이다.

L1 raw TSC는 2.5 GHz다. 공유 DAX Cristian ping-pong을 시작·끝에 각각 200회 수행하고,
최소 RTT 표본으로 Δ=B−A를 구해 두 추정치의 평균으로 보정했다.
Δ 시작→끝(µs)은 CPU50 **0.0864→0.0848**, CPU0 **12.6316→12.6296**,
Linux50 **0.2800→0.2752**, Linux0 **−0.2632→−0.2644**,
새 Linux0 session **0.6024→0.5988**이다.
RTT/2는 0.0812–0.0912 µs, drift까지 더한 보수적 오차는 0.0854–0.0936 µs다.
무보정 A→B/B→A 합÷2의 평균은 보정 표본 평균과 10⁻⁸ µs 이내에서 일치한다.
이는 상수 Δ의 대수적 상쇄 확인이며 독립적인 보정 정확도 검증은 아니다.

단위는 µs, 각 셀은 **p50 / p95**다. 조건마다 warmup 10 + measured 100이며 방향별 n=50이다.
p95는 nearest rank다. direct는 spin=0·native timer slack·HTTP 제어를 유지했다.

| 조건 | one-way 전체 | mean | A→B | B→A | 기존 handoff |
|---|---:|---:|---:|---:|---:|
| 2 MiB CPU, poll 50 | 92.99 / 134.97 | 89.15 | 109.57 / 134.97 | 58.78 / 137.28 | 210.86 / 211.48 |
| 2 MiB CPU, poll 0 | 37.22 / 44.29 | 42.38 | 37.23 / 44.56 | 37.22 / 40.53 | 54.87 / 63.96 |
| 128 MiB Linux, poll 50 | 401.50 / 558.34 | 444.13 | 396.67 / 558.66 | 404.83 / 552.64 | 594.85 / 743.15 |
| 128 MiB Linux, poll 0 | 294.76 / 343.44 | 300.76 | 309.37 / 357.52 | 270.87 / 337.18 | 393.21 / 2323.31 |
| 위 조건 새 session | 288.21 / 347.86 | 314.34 | 304.05 / 377.77 | 265.76 / 313.89 | 377.79 / 1147.46 |

Linux poll 0의 가산 구간 p50는 exit→detach 반환 **95.63**,
반환→publish **0.25**, 알림 관측 **0.15**, 관측→entry **195.88** µs다.
분위수끼리의 합은 전체 분위수가 아니다. 개별 표본의 구간 합은 경계 시간과 일치한다.
request→exit p50는 **27.28** µs, entry→controller ACK 관측은 **34.72** µs다.
기존 profile의 kernel detach/attach는 **22.31/37.64** µs,
B executor wake는 **103.73** µs다. poll 50에서도 wake는 **109.50** µs다.
executor는 futex로 깨며, poll 설정으로 크게 줄어든 것은 공유 알림 대기다.

reuse-prepared는 07 preset(poll=50, spin=500, controller CPU0/executor CPU1은 **L1 번호**)을 사용했다.
아래는 µs, warmup 10 + measured 100이다.

| reuse-prepared 구간 | p50 | p95 |
|---|---:|---:|
| pause API | 254.99 | 315.59 |
| save API | 442.00 | 477.07 |
| transport: publish + receive | 44.51 | 50.72 |
| restore API | 585.71 | 623.18 |
| resume API | 60.82 | 77.84 |
| save + transport + restore + resume 합 | 1125.26 | 1189.81 |
| pause→remote ACK | 1532.86 | 1670.83 |

**reuse의 1125.26/1189.81 µs는 동일한 exit→entry 실측이 아닌 가장 가까운 구간 합이다.**
pause 완료 이후부터 계산하고, stage 사이 알림/wake 공백을 제외하며,
종료도 첫 entry 대신 resume API 응답이다. 방향별 합은 A→B **1129.17/1181.33**,
B→A **1119.79/1216.12** µs다. request→ACK는 **1533.31/1671.30** µs다.
native KVM 객체에는 direct용 DAX resume profile이 연결되지 않아 정확한 경계는 수집하지 못했다.

state payload는 **14,264 B**, MSR은 **190개**다(FragVisor 약 6.9 KB·14개와 범위가 다름).
save 내부의 vCPU 요청/수집 p50 **202.07 µs**, bitcode 인코딩·파일 쓰기 **155.57 µs**,
restore 역직렬화 **168.39 µs**, vCPU 요청/복원 **252.18 µs**
(그 안의 arch state 적용 **103.97 µs**)를 확인했다.
save/restore API와 내부 함수 p50 차이는 각각 약 65 µs다.
코드상 vCPU 기본 save/restore는 각각 약 **11/12 ioctl**이며 VM·IRQFD ioctl은 별도다.
이 수집 구간에는 thread 통신도 포함된다. ioctl별 시간 및 FragVisor의 38 µs와 같은 함수 경계는 분리하지 못했다.

512 MiB guest에서 실제 working set은 약 **430.2–430.4 MiB(84%)**이며,
4 KiB마다 64 B를 검사한다. 모드마다 warmup 4 + measured 20이다.
아래 시간은 반복 방문의 p50(ms), EPT 수는 **별도 tracing run**의 첫 B 방문/반복 B 방문이다.

| 모드 | first scan | warm scan | first−warm | pause→W ready | L1 scan EPT violation |
|---|---:|---:|---:|---:|---:|
| direct, poll 0 | 3.715 | 3.628 | 0.088 | 4.033 | 0 / 0 |
| reuse-prepared | 3.763 | 3.602 | 0.174 | 5.341 | 29 / 0 |
| snapshot | 799.007 | 3.763 | 795.161 | 810.860 | 110388 / 110388 |

첫 B 방문(n=1, warmup에 포함)의 first/warm/ready는 direct **79.345/4.398/85.709 ms**,
reuse **12.389/5.139/18.136 ms**, snapshot **834.905/4.321/847.643 ms**다.
첫 A 복귀의 first−warm은 각각 **0.194/0.622/813.157 ms**다.
direct의 첫 B 방문도 L1 EPT fault는 0이었다. 첫 방문 시간에서 direct가 reuse보다 작지는 않았다.
reuse는 EFER.NX 토글로 MMU context를 초기화하지만 기존 EPT 할당을 재사용하며,
이 실험의 반복 방문에서는 scan fault가 0이었다. snapshot은 매번 새 VM/EPT다.

tracefs의 `kvm:kvm_page_fault`를 사용했다. scan의 load RIP **0x406373**으로 필터하고,
snapshot에서 매번 data **110,173 page** + page-table walk **215 page**를 확인했다.
모든 trace의 overrun/drop은 0이며 같은 scan window에서 중복 GPA page는 없었다.
시간 측정에 trace 결과를 섞지 않았다. 원본 gzip trace는 각 `.local/runs/` 아래에 보존했다.

direct의 register/VM state 직렬화 payload·RAM/EPT 복사량은 **0 B**다.
고정 mailbox는 token **136 B** + counter **8 B**이며, 알림 포함 CPU 경로 **160 B**,
Linux 경로는 epoch/ACK 포함 **184 B**의 protocol store다.
profile 영역 footprint **256 B**, VM-exit마다 추가되는 TSC store **8 B**,
시계 보정·JSON telemetry·계속 실행되는 guest 쓰기는 별도다.
KVM/VMCS/EPT 객체와 clock/timer/owner metadata의 **제자리 갱신은 존재**한다.
총 공유 메모리 write traffic 자체를 0 B로 주장하지 않는다.

채택한 12개 lab 모두 QEMU 전 thread의 host affinity가 32–63이고,
실행 전후 `numa_maps` 전체 page 합(공유 backing·실행 파일·라이브러리 포함)이 **N1만** 있었다.
Firecracker thread는 L1 CPU0/1에서 해당 QEMU thread를 통해 실행된다.
node 1 free는 시작 전 15,050–15,148 MiB였다.
다른 사용자의 기존 codex/node/sshd와 system service는 관측했지만 새 고부하 작업은 확인하지 못했다.
1초 간격 ps는 짧은 간섭을 배제하지 못한다. 모든 소유 VM과 `/dev/shm` backing을 정리했다.

기반 commit은 `2a4bb97`, 변경은 `dcf120e`(raw TSC), `3cb0e29`(시계 보정/probe),
`55c6577`(snapshot profile scope 누락), `6d17d96`(assembly 호출 경계)다.
Python unittest 48개(8 skip), static C 빌드, kernel 재빌드와 채택 lab verify를 통과했다.
예비 wrapper 경계 run 5개와 scope 검증 실패 run은 주 결과에서 제외했다.

**한계:** nested L1→L2 entry는 L0를 거친다. direct의 `dax_nested_root_sync`를 포함한
L0 shadow EPT02 재구성이 시간에 섞이므로 물리 host 값으로 해석할 수 없다.
공유 메모리는 coherent하며 flush 비용이 없다. TSC 오차와 첫 방문 n=1의 불확실성이 남는다.
FragVisor의 원 계측 경계는 추정이고 DSM/RDMA page fetch는 여기서 구현·측정하지 않았다.

전체 통계(min/max/mean/n·방향별·세부 profile·배치·원시 경로)와 계측 파일:줄은
[summary.json](summary.json)에 있다. [experiment.py](experiment.py)는 조건별 실행·배치·trace 수집,
[analyze.py](analyze.py)는 원시 report로부터 통계를 재계산한다.
두 script 모두 `numactl --cpunodebind=1 --membind=1 --physcpubind=32-63` 아래에서 실행한다.
실행기는 준비된 `.local/assets09-final/`과 `.local/run09-qemu/`를 사용한다.
