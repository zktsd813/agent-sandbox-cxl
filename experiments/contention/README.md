# Contention 실험 (실험 2) sandbox

노드 간 contention 실험에 쓸 짧은 SWE trace sandbox를 만든다. 지금 범위는 1단계다:
trace 선택, task 이미지 → Firecracker root disk 변환, 1 vCPU microVM 단독 재생.

## 구성

| 파일 | 역할 |
|---|---|
| `sandbox.py` | `select`, `build`, `solo` 세 단계의 단일 진입점. trace 파싱과 제출 비교는 `harness/trace-replay/experiment.py`를 재사용 |
| `guest-init.sh` | guest PID 1. job disk(`/dev/vdb`)의 명령을 기록된 간격(`gap × scale`)에 맞춰 실행하고 결과를 같은 disk에 쓴다 |
| `candidates.json` | 선택한 trace와 고정 image digest. `select`가 다시 만든다 |
| `.local/` | image, 실행 결과 (git 제외) |

## 실행

```bash
python3 experiments/contention/sandbox.py select          # 67개: 비교 가능, peak < 1.6GiB, 길이 ≤ 180초, CPU > 5초
python3 experiments/contention/sandbox.py build --jobs 6  # docker pull(digest 고정) → export → fakeroot mkfs.ext4
python3 experiments/contention/sandbox.py solo            # 코어 13-23에 microVM 하나씩, 명령 간격 없이 재생
```

필요한 것: Docker, `fakeroot`, e2fsprogs(`mkfs.ext4 -d`, `debugfs`), `numactl`, static `/usr/bin/busybox`,
`/dev/kvm`, 저장소의 Firecracker 빌드와 `.local/microvm/vmlinux-6.1.188` guest kernel.

## sandbox 구성

- Firecracker 1 vCPU, 2GiB, 네트워크 없음(loopback만).
- root disk는 image별 ext4를 실행마다 sparse copy한다. job disk에는 명령(`cmd/NNN.sh`), 직전 LLM 간격(`cmd/NNN.gap`),
  환경 변수를 고정한 실행 wrapper(`run`)가 들어간다.
- 명령마다 새 `bash -c`, trace의 timeout(기본 60초)에 `SIGKILL`. Docker CLI만 끊던 mini와 timeout 의미가 다르다.
- vCPU thread를 물리 코어 하나에, VMM thread를 그 SMT sibling에 고정하고 guest 메모리는 `--node`에 묶는다.

## 측정

`solo` 결과는 `.local/runs/<시각>-solo-*/`에 남는다.

| 파일 | 내용 |
|---|---|
| `<trace>/commands.csv` | 명령별 종료 코드와 원본 비교, guest 시간(10ms), guest CPU(`/proc/stat`), host에서 본 vCPU 실행 시간 |
| `<trace>/threads.csv` | Firecracker thread별 `schedstat` 누적 실행·대기 시간, 10ms 간격 |
| `<trace>/serial.log` | host monotonic ns가 붙은 serial 출력. `SWE_CMD_START/END` marker로 host 시간과 맞춘다 |
| `solo-summary.csv` | trace별 불일치·timeout·제출 일치·CPU 시간, 기록된 간격을 포함한 평균 수요(코어) |

`mean_demand_cores_with_recorded_gaps = vCPU 실행 시간 / (명령 시간 + 기록된 LLM 간격 합)`이다.
노드 overcommit에서 평균 부하 ρ를 정할 때 쓴다.

## 동시 실행 (실험 2 pilot)

| 명령 | 역할 |
|---|---|
| `sandbox.py pilot --per-node K [--pool none\|socket\|all] [--group-size G] [--vcpus N]` | cpuset node 4개(기본 2-6/7-11/13-17/18-22)에 sandbox K개씩 상시 유지. base image 읽기 전용 공유 + overlay disk, host NAT |
| `sandbox.py episodes <run> --solo <solo-run>` | 1ms per-node `schedstat` 합계와 단독 대비 지연 → `contention-1ms.json`. 구간 길이·이동 상한 필드는 run_delay 기록 지연으로 왜곡되어 쓰지 않는다 |
| `sandbox.py network-down` | `ctbr0`, TAP, `contention-sandbox` 태그 iptables 규칙 제거 |
| `report.py` | 실행들을 seed 평균 표로 집계 |
| `sim.py` | 단독 실행 burst + LLM 대기로 node를 모델링하는 simulator. 분리·공유·이동 메커니즘별 잃은 CPU |
| `plot_story.py` | 결과 그림 (`figures/`) |

결과 정리: [RESULTS-20261021.md](RESULTS-20261021.md).

## DSec 규모 simulation

| 파일 | 역할 |
|---|---|
| `fleet.py` | numba simulator. power-of-k 배치, 여러 vCPU, trace별 메모리에 비례하는 migration, NIC 직렬화, RL job 모드. `--test`는 손계산 검사 |
| `validate.py` | pilot 실측을 같은 구성의 `fleet.py`와 비교([PLAN-fleet.md](PLAN-fleet.md) 검증 사다리) |
| `pilot_audit.py` | pilot 실행을 단독 실행과 명령 단위로 비교: 경로 이탈(timeout), 같은 경로의 CPU, 사용률을 맞추는 수요 배율 |
| `kernel_wait_fit.py` | 유체 모델이 놓치는 scheduler 대기 a(P)·exp(b·사용률)를 5/10/20코어 실측에 맞춤 (`fleet.KERNEL_WAIT`) |
| `trace_count.py` | trace 수를 15~120개로 줄여도 같은 사용률의 contention이 같은지 확인 |
| `plot_fleet.py` | `figures/fleet-*.png` |
| `grids/` | 큰 규모 실험의 조건 목록 (`fleet.py --grid`) |
| `candidates-all.json`, `candidates-v.json` | trace 264개 전체, 두 단독 실행에서 모두 쓸 수 있는 242개(검증용) |

계획: [PLAN-fleet.md](PLAN-fleet.md). 결과와 판정: [RESULTS-fleet.md](RESULTS-fleet.md).
