# 20 진행 기록

- 시작: 2026-11-18 06:04 UTC, bud05, vm-cleanup, b53c9f5.
- 기존 변경: .gitignore, docs/RESEARCH.md, agent-workload/capacity 결과, experiments/capsim. 읽기만 하며 stage하지 않는다.
- 18 서버 규칙과 19 기록·설계·결과 확인. node0 free 21 GiB, / free 99 GiB. lab/quiet 잠금, NUMA0, nice19, jobs<=16 유지.
- minimal-readable-experiments 적용. 기존 runner를 확장한다. 원시 자료 implementation/virtual_machine/.local/run20.
- 우선 수집 단계와 L1 KVM 구간별 시간, scheduling 상태를 계측하고 256 MiB 220회 재현부터 수행한다.
- 06:09 UTC: 계측 smoke 2 clean/2 hand-off, 누락 0. L1 DIRTY_TIME와 FC collect 구간·schedstat 수집 확인. perf wrapper 대신 설치된 /usr/lib/linux-tools/6.8.0-149-generic/perf 사용. QEMU vCPU TID는 QMP query-cpus-fast로 식별.
- 06:17 UTC: 첫 정답지 on/off 각 220 clean 통과. On 수집 median 1.822 ms/p95 154.098/max 825.388 ms, off 1.810/6.276/3221.383 ms. 50ms 초과 수집 on14/off8 전부 커널 D-bit 처리 구간. Slots/mmu lock 획득, TLB flush, copy는 대체로 µs. Clean 요청→dispatch median 약0.125ms, max0.216ms.
- 총 clean 시간에는 계측 로그·oracle live 경계 ACK가 포함된다. On 총 clean >50ms15건 중 한 건은 ioctl 50ms 이하와 나머지 비용의 합이다. 순수 단계 시간과 혼동하지 않는다. Oracle-on hand-off ACK 약918ms에는 정지 hash가 들어가므로 시간 기준으로 쓰지 않는다.
- L0 QEMU run delay는 초기 꼬리에서 수십µs 이하. L1 VMM 실행 시간은 D-bit 처리 지연과 비슷하다. L0 perf에서 EPT violation→page fault→queued_read_lock_slowpath stack을 확인. 아직 꼬리별 비교와 직접 KVM 시험 전이므로 원인 판정은 보류.
- 기존 /proc/schedstat의 timestamp(jiffies, host HZ=1000)를 L0 CLOCK_MONOTONIC에 보정했다. perf 구간 대응에 사용. 0.262ms read span+1ms tick. 각 clean 앞뒤의 mono_ns·vmstat/node0/CPU/load는 처음부터 저장됐다. 연속 관측만 첫 실행 중간부터 추가했다. 구간 대응은 원시 mono_ns가 1차이며 jiffies 보정은 대조용이다.
- page_hash의 선택 line hash 모드 추가, 8KiB fixture에서 변경 line 0→2→0 검사 통과. 큰 line state는 종료 시 삭제하는 oracle 연동을 준비 중.

- 06:25 UTC: on/off/on/off 각220 clean(총880) 완료. 총 clean >50ms는15/8/18/6, 각 최대847/3229/6402/976ms. 모두 가장 큰 구간은 D-bit clear. 커널 수집>50ms는14/8/18/6. 원시별 구간·스케줄 표 tail-analysis.json 생성.
- 같은 upstream dirty_log_perf_test: L0/L1/L0/L1 각220. GET_DIRTY_LOG median0.972/0.748/0.997/0.756ms, max2.300/0.869/2.431/0.879ms. >50ms 전부0. PML Y, manual protect 비활성(-g), 4KiB, 256MiB, vCPU1. L1은 guest physical memory 쓰기이며 DAX 공유 hand-off graph는 쓰지 않는다.
- 정지 clean 진단(같은 256MiB/220회, oracle off) max14.999ms, >50ms0. Live 방식에서만 나타난 중첩 EPT 동시 경로를 지목한다. VM을 멈추지 않는 live 정리의 의미를 임의로 바꾸지 않는다.
- L0 perf 첫 on arm: 꼬리 창 표본6686개 중 queued_read_lock_slowpath leaf2878(43.0%), 정상891개 중0. root sync stack3365/6686, 정상451/891. 꼬리에서는 root sync 자체 비율보다 lock 대기·EPT 읽기 작업량이 증가했다. 명령 함수 주소는 같은 시점 /proc/kallsyms로 복원했다.
