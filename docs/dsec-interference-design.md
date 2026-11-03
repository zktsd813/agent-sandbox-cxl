# DSec식 co-location 간섭과 hand-off 설계 (2026-11-02)

목적: [DSec](https://arxiv.org/html/2609.22978v1)가 말한 contention을 bud05에서 재현하고, VM hand-off가 그것을 줄일 수 있는지 판단할 근거를 만든다.

이전 실측([virtual-node-rl-20261026](results/virtual-node-rl-20261026/README.md), [sync-replay-design.md](sync-replay-design.md) 11~12절)은 한 RL job의 rollout끼리 생기는 **runqueue contention**만 쟀다(SMT 끔, 다른 작업 없음). DSec가 잰 것은 **다른 작업과 섞여 돌 때의 간섭**이다.

## 1. DSec가 말한 contention

| 항목 | DSec 내용 (절) |
|---|---|
| 5% 수치 | sandbox의 약 90%가 요청 CPU의 5% 이하를 평균으로 쓴다(§4.3). contention의 근거가 아니라 **overcommit해도 된다**는 근거다. |
| 간섭 실험 | 노드 용량의 10~50%만큼 best-effort(BE) 부하를 같이 돌리면, latency-sensitive(LS) agent의 step 시간이 50%에서 **+45.2%** 늘었다(§8.5). |
| QoS | SCHED_IDLE만으로는 최대 3.4% 개선. SMT 형제 thread 공유가 남는다. core scheduling을 더하면 **+17.3%**까지 줄었다. |
| 남은 원인 | 다중 코어 부하에서의 turbo 하락, 메모리 대역폭, LLC 경합. 메모리 대역폭 격리는 적용하지 않았다. |
| 하드웨어 | 2 socket × 96코어 × SMT 2 (384 thread). microVM 524~800개/노드, container 1,048~3,200개/노드. |

## 2. bud05 설정 (2026-11-02 적용)

| 항목 | 상태 |
|---|---|
| SMT | 커널이 `nosmt`로 부팅한다(강제 아님). `smt/control`을 `on`으로 바꾼 뒤 NUMA 1 쪽 sibling(96~127)은 바로 offline했다. NUMA 0만 64 thread(0~31, sibling 64~95)이고 NUMA 1은 이전과 같다(32~63). **재부팅하면 다시 꺼진다.** |
| core scheduling | `CONFIG_SCHED_CORE=y`. 일반 사용자 권한으로 cookie를 만들 수 있다. |
| RDT | resctrl을 `/sys/fs/resctrl`에 mount했다. L3 CAT 15 way(120 MiB / 2 socket), MBA 10% 단위, MBM(socket별 group 대역폭), CMT(LLC 점유). group은 실행 중에만 만들고 끝나면 지운다. |
| 주파수 | `acpi-cpufreq`, 최대 2.5 GHz. boost는 1로 켜져 있지만 실제 측정 주파수가 2.5 GHz를 넘지 않는다. **turbo는 BIOS에서 꺼진 것으로 보인다.** 그래서 DSec의 "turbo 하락" 원인은 여기서 생기지 않는다. |
| governor | `schedutil` 그대로 둔다(이전 실측과 비교하기 위해). schedutil에서는 30%만 바쁜 CPU가 약 800 MHz로 돈다. 주의: 이전 실측에서 "같은 명령에 CPU 시간이 더 든다"는 몫에 이 주파수 효과가 섞여 있을 수 있다. |

## 3. 구현

| 파일 | 내용 |
|---|---|
| `experiments/contention/be_load.c` | BE 부하 생성기. 지정한 CPU마다 thread 하나를 두고, 매 10 ms 중 LEVEL만큼 일한다(thread마다 시작 위상이 무작위). 종류는 `cpu`(정수 hash, compile과 비슷), `avx`(AVX-512 FMA), `mem`(thread당 64 MiB stream triad), `mix`(cpu와 mem을 번갈아). `--idle`은 SCHED_IDLE, `--core-cookie`는 모든 thread를 하나의 core-scheduling cookie에 넣는다. 끝나면 한 일의 양을 JSON으로 출력한다. |
| `sandbox.py pilot` | `--be KIND:LEVEL`, `--be-nodes`, `--be-qos none\|idle\|core\|rdt`, `--be-rdt WAYS:MB%`(기본 4:30), `--be-mem-mb`, `--be-warmup-s`. BE는 실행 내내 노드의 모든 thread에서 돈다. resctrl이 있으면 노드마다 `ctbe<n>` group에 넣는다. root group과 BE group의 LLC 점유와 메모리 바이트를 1초마다 `resctrl.csv`에, thread별 busy 주파수와 IPC를 `turbostat.tsv`에 남긴다. `run.json`에 `co_load`와 `smt`를 기록한다. |
| `sandbox.py` `host_sampler` | pilot에서 항상 켜진다. PSI, 회수, 디스크, sandbox network, softirq, conntrack, 여유 공간을 1초마다 `host.csv`에 남긴다([load-balancing-survey.md](load-balancing-survey.md) 3절). |
| `run_plan_segments.sh` | `NODES`(SMT면 `2-16,66-80/17-31,81-95`)와 `SOLO_SIBLING`(SMT면 64: solo에서 VMM thread를 sibling에 둔다)을 더했다. |
| `/Serverless/agent-sandbox-cxl/logs/queue-dsec.sh` | 4절 I1 matrix 대기열(아직 실행하지 않음). |

SMT 노드에서는 vCPU와 VMM thread가 노드마다 30개 thread(코어 15개 + sibling 15개) 안에서 자유롭게 돈다. smoke(노드당 16개, 2분, `mix:0.3`, `rdt`)에서 모든 경로가 동작했다. BE는 노드마다 30 thread에서 약 16 GB/s를 썼고, resctrl group은 실행 뒤 지워졌다.

## 4. 먼저 잰 것: host microbenchmark (2026-11-02)

LS 대리 작업 두 개를 CPU 4(sibling 68)에 고정하고 5회 중앙값을 쟀다.
- `gcc`: `be_load.c` compile, 0.29초.
- `py`: dict 150만 개를 만들고 정렬, 1.86초. 메모리 접근이 많다.

BE는 NUMA 0의 다른 thread에서 돌렸다. 값은 BE가 없을 때 대비 배율이다.

| BE 조건 | 위치 | gcc | py |
|---|---|---|---|
| cpu 100% | sibling(68)만 | 1.31 | 1.46 |
| avx 100% | sibling만 | 1.40 | 1.51 |
| mem 100% | sibling만 | 1.58 | 1.88 |
| mem 50% | **같은 socket의 다른 코어만**(sibling 아님) | 1.51 | **3.26** |
| 위 + RDT (BE 4 way, MB 30%) | 같은 socket의 다른 코어만 | 1.40 | 2.16 |
| mix 30%, QoS 없음 | 모든 thread | 1.51 | 1.99 |
| mix 30%, SCHED_IDLE | 모든 thread | 1.27 | 1.69 |
| mix 30%, + core scheduling | 모든 thread | 1.27 | 1.64 |
| mix 30%, + RDT | 모든 thread | 1.36 | 1.53 |
| mix 50%, QoS 없음 | 모든 thread | 2.28 | 3.34 |
| mix 50%, + core scheduling | 모든 thread | 1.42 | 2.11 |

(governor `performance`. `schedutil`도 비슷하고 RDT 조건만 조금 낮다: py 1.74, 1.38.)

- **SMT 형제 간섭은 1.3~1.9배다.** core scheduling으로 없앨 수 있는 몫이다.
- **socket 단위 간섭(LLC, 메모리 대역폭)이 더 크다.** BE가 다른 코어에서만 돌아도 메모리를 많이 쓰는 py가 3.3배 느려졌다. core scheduling으로는 줄지 않는다.
- **로컬 QoS를 다 써도 1.4~2.1배가 남는다.** SCHED_IDLE + core scheduling + RDT를 함께 써도 mix 30%에서 1.36 / 1.53, mix 50% core에서 1.42 / 2.11이다. DSec의 +17.3%보다 큰 것은 우리 BE가 메모리를 더 많이 쓰기 때문일 수 있다(DSec는 BE의 종류를 밝히지 않았다).
- 대리 작업은 실제 tool call이 아니다. 실제 rollout으로 재는 것이 5절 I1이다.

## 5. hand-off로 줄일 수 있나

간섭을 원인별로 나누고, 원인마다 로컬에서 고칠 수 있는지 본다.

| 원인 | 로컬 해결 | 다른 곳으로 옮겨야 하나 |
|---|---|---|
| runqueue(CPU 몫) | SCHED_IDLE | 아니다 |
| SMT 형제 공유 | core scheduling | 아니다(같은 노드의 빈 코어로 충분) |
| LLC, 메모리 대역폭 | RDT(CAT, MBA). 일부만 줄고, BE 처리량을 고정으로 깎는다 | **같은 socket 안에서는 못 피한다.** 다른 socket이나 host로 가야 한다 |
| turbo 하락 | 없다 | 다른 host(bud05에서는 turbo가 꺼져 재현 불가) |

이전 runqueue contention과의 차이:
- **시간 규모가 다르다.** runqueue contention 구간은 대부분 10 ms보다 짧아 hand-off도 늦었다. BE 간섭은 BE 작업이 도는 동안(초~분) 계속된다. 그래서 hand-off의 지연(10 ms)은 문제가 안 된다.
- **대신 hand-off의 빠름이 꼭 필요하지도 않다.** 간섭이 길게 이어지면 LLM 대기 중에 일반 migration으로 옮겨도 된다(HANDOFF Q3와 같은 반론).

hand-off(CXL)가 이길 수 있는 조건:
1. **로컬 QoS 뒤에 남는 몫이 크다.** microbenchmark에서는 1.4~2.1배다. I1에서 실제 rollout으로 확인한다.
2. **조용한 host가 있다.** BE 부하가 host 사이에 고르지 않아야 옮길 곳이 있다. DSec처럼 power-of-k로 고르게 퍼지면 옮길 곳이 없다. BE의 시간·공간 분포가 필요하다.
3. **옮길 VM이 많고 자주 옮긴다.** host당 수백 개의 LS VM을 BE 변화(초 단위)에 맞춰 다시 배치하려면, 메모리를 복사하는 migration은 network 대역폭이 모자란다. CXL hand-off는 메모리를 옮기지 않으므로 빈도의 제약이 작다. 이것이 CXL이 필요한 이유가 된다.
4. **경쟁 대안보다 낫다.** 경쟁 대안은 세 가지다.
   - (a) LS가 tool call 중일 때만 BE를 MBA로 조이는 동적 throttling. 로컬이고 싸다. 대신 BE 처리량을 잃는다.
   - (b) BE를 옮기기. BE는 지연에 둔감하다.
   - (c) 정적 RDT 분할.

   hand-off는 BE 처리량을 지키면서 LS를 보호할 때만 의미가 있다. 그래서 LS 느려짐과 BE 처리량을 함께 보고해야 한다(`be-node*.json`).

CXL만의 추가 관점: VM 메모리가 CXL에 있으면 DRAM 채널이 아니라 CXL link를 쓴다.
- LS를 CXL에 두면 BE의 DRAM 대역폭 경합을 피할 수 있다. CXL tax와 맞바꾸는 것이다.
- 반대로 BE 메모리를 CXL에 두면 BE가 LS의 DRAM 대역폭을 덜 먹는다.
- 두 경우 모두 LLC는 여전히 공유한다. 지금 CXL node가 0 MB라 재려면 CXL을 online해야 한다.

## 6. 실험 계획

**I1 (bud05 한 대, 준비 완료).** 새 계획의 segment 1(orig step 7, 9)은 image가 이미 있다. ×0.25, 노드당 96개, SMT 노드. 조건당 약 45분, 전체 약 6~7시간(SMT solo 1시간 포함).

| 조건 | BE | QoS | 보는 것 |
|---|---|---|---|
| nobe | 없음 | – | SMT 켠 기준(이전 SMT 끈 실측과 비교) |
| mix30-none | mix 30%, 두 노드 | 없음 | DSec 무방비 기준 |
| mix30-core | mix 30% | SCHED_IDLE + core | DSec 방식 |
| mix30-rdt | mix 30% | + RDT 4 way / MB 30% | 로컬 QoS 최대 |
| mix50-core | mix 50% | SCHED_IDLE + core | DSec 50% |
| mix50-rdt | mix 50% | + RDT | 로컬 최대 |
| mix50n0-core | mix 50%, **노드 0만** | SCHED_IDLE + core | 노드 1(같은 socket, 다른 코어)이 얼마나 보호되나 = socket 안 이동의 상한 |

지표:
- 명령 느려짐을 runqueue 대기, CPU 시간 증가, 그 밖으로 분해(`slowdown_breakdown.py`)
- rollout과 step 시간
- BE 처리량(`be-node*.json`)
- 주파수(`turbostat.tsv`)
- LLC 점유와 대역폭(`resctrl.csv`)

**I2 (두 host).** bud04에서도 SMT를 켜고(같은 방식) BE 없이 같은 segment를 돌려 "조용한 host"의 명령 시간을 잰다. tool call마다 (bud05 간섭 시간 − bud04 조용한 시간)이 hand-off로 되찾을 수 있는 상한이다. 여기에 hand-off 비용과 CXL tax를 빼고, BE 부하 분포를 가정한 simulator로 회수율을 낸다.

**I3 (CXL, online 뒤).** BE 메모리를 CXL(`numactl --membind=2`)에 둘 때와 DRAM에 둘 때 LS 느려짐을 비교한다. LS VM 메모리를 CXL에 둔 경우와도 비교한다.

판정:
- I1에서 로컬 QoS(rdt) 뒤 남는 rollout 느려짐이 작다(예: 10% 미만) → 로컬 QoS로 충분하고 hand-off의 동기가 약하다.
- 남는 몫이 크고 대부분 LLC·대역폭이다 → I2로 다른 host의 상한을 잰다. 그 상한이 hand-off 비용과 CXL tax보다 크면 hand-off의 동기가 선다.
