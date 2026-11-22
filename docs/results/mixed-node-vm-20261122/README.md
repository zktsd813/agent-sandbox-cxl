# VM 수는 메모리가 막는다: 네 단계 시나리오와 실험 계획 (2026-11-22)

상태: 핵심 네 단계는 사용자가 정리했고(11-22), [RESEARCH.md](../../RESEARCH.md) 1.1의 "핵심"에 넣었다. container와 VM이 섞인 노드라는 설정은 후보다. 실험은 아직 하나도 하지 않았다.

출처 표시는 RESEARCH.md와 같다. [실측]은 우리가 잰 값, [sim]은 simulator, [계산]은 잰 원자료를 다시 센 값, [trace]는 기록 분석, [논문]·[공개 자료]는 남의 값이다.

## 1. 핵심: 네 단계

1. **VM 수는 메모리가 막는다.**
2. **CXL로 메모리를 늘린다.**
3. **CXL pool로 노드 사이 메모리 불균형을 푼다.**
4. **CPU 불균형은 hand-off로 푼다.**

각 단계는 앞 단계 위에 선다. 1이 서야 2가 쓸모 있다. 2로 용량을 풀어야 3의 불균형과 4의 CPU 쏠림이 드러난다.

| 단계 | 주장 | 지금 근거 | 빠진 것 | 실험 |
|---|---|---|---|---|
| 1. 메모리가 막는다 | VM은 대부분의 시간을 LLM 대기로 보내며 메모리만 잡는다. 그래서 CPU보다 메모리가 먼저 찬다 | microVM에서 RL·SWE·code interpreter 모두 메모리가 먼저 막혔다. 그때 CPU 9~40% [실측 11-19] | full OS VM의 실제 상주. 압축·중복 제거·balloon 뒤에도 막히는지(압축과 합치기를 다 하면 교차점이 코어당 1.5~5 GiB까지 내려간다 [계산 11-19, 상한]) | 1, 3 |
| 2. CXL로 늘린다 | 명령 사이 상태는 CXL에, 무거운 명령이 새로 쓰는 메모리는 DRAM에 둔다 | 이렇게 나누면 무거운 명령 +2.0% [실측 11-13]. 가벼운 명령은 CXL에서 +1~5% [실측 10-14] | GUI VM의 tax. DRAM 영역을 붙였다 떼는 두 영역 VM(구현 없음). snapshot·pause와의 직접 비교 | 4, 3 |
| 3. pool로 메모리 불균형 | 배치는 지금 보이는 값으로 하고 VM은 배치 뒤에 자란다. 그래서 노드 사이 메모리가 쏠리고, 그 여유를 공유 pool이 작게 받는다 | 크기가 제각각인 SWE 입력에서 노드가 DRAM을 넘는 시간이 25~36%다. 노드마다 따로 남길 여유 22~31%를 pool은 6~8%로 받는다 [sim 11-19] | 실측이 없다. 실제 VM 시계열, container와 섞일 때 | 7, 2 |
| 4. hand-off로 CPU 불균형 | 메모리를 풀면 CPU가 함께 찬다. 무거운 명령이 시작될 때 코어가 남는 노드로 옮긴다. 메모리와 EPT가 CXL에 있어 비용이 VM 크기와 무관하다 | CPU당 DRAM 4 GiB인 SWE에서 가장 나은 배치로도 CPU 일의 3.5%가 넘치고, 그 99%는 다른 노드에 빈 코어가 있었다 [sim 11-20]. 명령 시작 때 옮기면 명령 시간 1.10~1.12배 → 1.03배 [sim 11-18]. 60 ms 이동이 대기 차이의 61~96%를 되찾았다 [실측 11-05]. lab에서 옮긴 뒤 EPT fault 0번 [실측 11-19] | 노드 안 QoS 대비 남는 몫. GUI action을 시작 때 알 수 있는지. full OS VM의 device 상태. 진짜 여러 서버 CXL | 2, 5, 6 |

EPT는 guest 주소를 실제 메모리 주소로 바꾸는 표다. hand-off는 guest 메모리와 EPT를 모든 서버에서 같은 CXL 주소에 두고, vCPU와 device 상태만 넘긴다(11-19 설계, lab 구현 있음).

**이겨야 할 대안(단계별)**

| 단계 | 대안 | 대안이 이기는 경우 | 우리가 이길 조건 |
|---|---|---|---|
| 1, 2 | 압축·중복 제거(zswap, KSM), balloon free page reporting, DAMON 회수 | 줄인 뒤 상주가 작아 CPU가 먼저 막힐 때 | 줄인 뒤에도 메모리가 막힐 때. 양쪽에 똑같이 켠다 |
| 2 | snapshot·pause(대기 중 내리고 다음 명령에서 복원) | 대기가 길고 복원이 싸면 메모리를 거의 다 비운다 | 복원이 명령마다 붙고, 큰 VM일수록 복원과 다시 읽기가 길 때 |
| 3 | 노드마다 여유를 남기기 | 쏠림이 작을 때 | 쏠림이 클 때. 크기가 제각각이고 노드당 VM 수가 적을수록 그렇다 |
| 3 | sandbox 수 기준 배치 | 크기가 비슷하면 쏠림을 1/3로 줄인다 [sim 11-19] | 크기가 제각각이면 이점이 거의 사라진다 [sim 11-19] |
| 4 | 노드 안 QoS(같은 노드 container가 CPU를 양보) | container가 덜 급한 일이면 노드 안에서 받는다 | container도 급하거나(동기 RL), 노드 전체 수요가 용량을 넘을 때 |
| 4 | live migration, 100 GbE 복사, snapshot 복원으로 옮기기 | 옮길 메모리가 작을 때 | 큰 VM. 메모리에 비례하는 비용이 burst보다 늦을 때 |

## 2. 가정: 작은 서버와, 실제로 VM이 필요한 workload(11-22 사용자)

가정은 핵심이 아니다. 위 네 단계가 어디서 가장 강한지를 정하는 배경이다.

**서버.**
- container와 같이 사는 노드를 따로 모형으로 만들지 않는다. VM이 쓸 수 있는 몫을 "코어와 메모리가 조금 적은 서버"로 둔다.
- simulator는 32·48·96코어를 비교한다. 실측은 bud05 NUMA 0(VM용 28코어, 메모리 약 100 GiB, 코어당 약 3.6 GiB)이다. 이 크기가 바로 그런 서버다.

**workload를 고르는 기준.** VM이 실제로 필요해야 한다.
1. 자기 OS가 필요하다: Windows, Android, display server와 여러 앱이 도는 데스크톱 Linux, 시스템 설정을 바꾸는 일.
2. kernel 기능이 필요하다: Docker 안의 Docker, systemd, mount, kernel module.
3. 공개 자료가 실제로 VM에서 돌린다고 말한다.

**우리 SWE는 기준에 들지 않는다.** 264 trace의 명령 13,600개 중 Docker 0, systemd 0, kernel module 0, GUI 0, mount 2개였다 [계산 11-22]. Python RL과 code interpreter도 container 일이다(DSec도 SWE와 tool-use를 container로 돌린다).

| workload | 기준 | 공개 근거 | 이번에 |
|---|---|---|---|
| 데스크톱 Linux agent(OSWorld) | 1 | OSWorld는 VM에서 돈다(Docker provider 기본 4 GB·4코어). ComputerRL은 가상 데스크톱 1,000개 이상으로, CUA-GYM은 OSWorld VM 2,000개 rollout pool로 RL을 했다 [논문] | **실측한다**(실험 1). image와 공개 trajectory가 있다 |
| Android agent(AndroidWorld) | 1 | Android emulator는 QEMU 기반 VM이다. 최신 emulator는 VM 메모리를 최소 4 GB로 잡는다 | 다음. AndroidWorld로 모은 trajectory 2,640개가 공개되어 있다(Uni-GUI-OpenMobile) |
| Windows agent(Windows Agent Arena) | 1 | Linux host에서 Windows는 VM으로만 돈다. image 약 30 GB | 뒤로. image 준비가 크다 |
| Docker를 쓰는 terminal task(Terminal-Bench의 compose task) | 2 | Vercel은 trial마다 Firecracker microVM을 띄우고 그 안에서 Docker를 돌린다 | 뒤로. 어느 task가 Docker를 쓰는지 먼저 센다 |

### 2.1 근거: DSec

DSec(DeepSeek의 sandbox 시스템, arXiv 2609.22978) [논문]:
- backend는 넷이다.
  - 함수 호출: 짧고 상태 없는 일.
  - container: SWE와 일반 tool-use의 주 backend. host kernel을 같이 써서 보안에 민감한 일에는 맞지 않는다.
  - Firecracker microVM: 보안에 민감한 일. container보다 메모리를 더 쓰고 늦게 뜬다.
  - QEMU full VM: Android, GUI, OS 고유 API가 필요한 일.
- 나누는 기준은 크기가 아니라 격리와 OS 기능이다.
- 한 production 노드에서 container 최대 1,048개, microVM 최대 524개를 봤다. 노드당 microVM 800개 또는 container 3,200개까지 돌렸다.
- 정적 분할은 없다. 입장 제어를 하고, 덜 급한 sandbox는 SCHED_IDLE로 CPU를 양보한다.
- 메모리는 따로 다룬다. microVM은 virtio-pmem DAX, DAMON, balloon free page reporting으로 줄인다. container는 pause와 cgroup 회수로 줄인다.
- container와 microVM sandbox의 약 90%가 요청한 CPU의 평균 5% 이하만 쓴다.
- production에서는 container와 microVM이 개수와 자원 모두에서 대부분이다. full VM은 소수다.

### 2.2 가정이 네 단계를 어떻게 강하게 하나

- **1(메모리가 막는다).**
  - VM 메모리는 guest kernel과 guest page cache가 잡고 있어, balloon처럼 guest가 도와야 줄어든다. container 메모리는 host가 바로 회수한다.
  - container와 같이 사는 노드에서는 VM 몫이 노드보다 작다.
  - 그래서 메모리는 VM 몫에서 먼저 막힌다.
- **2(CXL로 늘린다).** full OS VM은 크다. 설정 4~8 GB, vCPU 2~8개다 [공개 자료: OSWorld Docker 안내 8 GB·8코어, QEMU 예 4 GB·vCPU 2개, dockur/windows 기본 4 GB·2코어, Android emulator 최소 4 GB]. 실제 상주는 재야 한다(실험 1).
- **3(pool로 불균형).**
  - 노드 메모리 합의 흔들림은 대략 1/√(노드당 VM 수)로 준다. VM이 크고 VM 몫이 작으면 노드당 VM 수가 적어 쏠림이 커진다.
  - 크기를 264-trace 분포로 넓히자 pool이 할 일이 약 4배가 되었다 [sim 11-19].
- **4(hand-off로 CPU 불균형).**
  - full OS VM은 vCPU가 여럿이라 한 VM의 burst가 넓다.
  - live migration과 snapshot 복원은 비용이 VM 크기에 비례한다. 11-18에 Firecracker 2 GiB VM을 디스크에서 복원했더니 다음 무거운 명령이 0.2~1.5초 늘었다 [실측].
  - hand-off는 메모리를 옮기지 않는다.
- **작은 일을 빼도 자원은 남는다.** 우리 SWE 264개 trace(10-14 Docker 재생) [계산]:

  | 최대 메모리 기준 | trace 비율 | 메모리 합 비율 | CPU 시간 비율 | 평균 코어 수 중앙값 |
  |---|---|---|---|---|
  | 1 GiB 이상 | 45% | 92% | 88% | 4.4 |
  | 2 GiB 이상 | 28% | 76% | 68% | 4.5 |
  | 4 GiB 이상 | 14% | 54% | 40% | 5.1 |

  "작은 VM이 많으면 필요 없다"는 개수 이야기이고, 자원은 큰 쪽이 정한다. 다만 SWE 자료이고, DSec에서 SWE는 container 쪽이다.

### 2.3 가정에서 바뀌는 것

- 지금까지 잰 SWE microVM(vCPU 1개, 상주 0.44 GiB)은 DSec 기준이면 container 쪽 일이다. 그래서 "이 비율에서 hand-off가 할 일이 거의 없다"(11-19)는 작은 sandbox에 대한 결론이 된다.
- full OS VM은 대부분 QEMU가 필요하다. 그래픽, 입력, USB 장치가 있고, Windows와 Android는 Firecracker에서 돌지 않는다. 그런데 우리 hand-off 구현은 Firecracker에 있다. 데스크톱 Linux는 guest 안의 가상 화면(Xvfb)으로 Firecracker에서 돌릴 수 있을 것으로 본다(실험 6).

## 3. 무너지는 조건(단계별)

1. **메모리가 막지 않는다.** 같은 image에서 뜬 데스크톱 VM을 KSM이 대부분 합치거나, balloon으로 대기 중 대부분을 돌려주면 줄인 뒤 상주가 작아 CPU가 먼저 막힌다(실험 1, 3).
2. **CXL에 두는 비용이 크다.** 화면 그리기, screenshot 인코딩, 앱 시작이 메모리 대역폭을 많이 쓰면 GUI VM의 tax가 크다(실험 4).
3. **쏠림이 작다.** 실제 VM이 배치 뒤에 크게 자라지 않거나 크기가 비슷하면, 노드마다 조금씩 남기는 것으로 충분하다(실험 1, 7).
4. **CPU 쏠림이 노드 안에서 풀린다.** container가 덜 급한 일이면 VM burst를 노드 안에서 받는다. 그러면 이야기는 container도 급한 동기 RL에서만 선다(실험 2). GUI action을 시작 때 알 수 없으면 실행 중에 옮겨야 하고, 그때는 tax가 직접 비용이 된다(실험 5).
5. **설정의 대상이 작다.** DSec에서도 full OS VM은 소수다. VM 몫의 대부분이 작은 보안용 microVM이면 다시 "작은 VM 여럿"이 된다. computer-use와 mobile agent RL의 규모는 공개 자료로 확인한다.
6. **device 상태가 크다.** full OS VM의 화면, 입력, network 상태를 넘기는 비용이 수십 ms를 넘으면 snapshot 복원과의 차이가 준다(실험 6).

## 4. 실험

원칙:
- 실측을 먼저 하고, simulator는 첫 걸러 보기와 서버 한 대를 넘는 규모에만 쓴다.
- bud05 NUMA 0만 쓴다(NUMA 1은 다른 사용자).
- 잴 때는 `.codex-runs/lab.lock`과 `quiet.lock`을 배타로 잡는다. 11-19에 다른 세션의 QEMU 실행과 겹쳐 RL swap 실행을 버렸다.
- 비교는 sandbox 쪽 느려짐만 본다.
- 배치는 지금 보이는 값만 쓴다.

| 실험 | 단계 | 질문 | 방식 | 어디서 | 승인 | 기간(어림) |
|---|---|---|---|---|---|---|
| 0 준비 | | image, trajectory | | bud05 NVMe | 없음 | 0.5일 |
| 1 데스크톱 VM 크기 | 1, 3 | 메모리가 먼저 막히나, 얼마나 자라나 | 실측 | bud05 NUMA 0 | 없음 | 2일 |
| 2 혼재 노드 | 3, 4 | container가 양보하면 얼마나 풀리나 | simulator | bud04 | 없음 | 0.5일(대리 입력), 실험 1 뒤 다시 |
| 3 VM 메모리 줄이기 | 1, 2 | 줄인 뒤에도 막히나 | 실측 | bud05 NUMA 0 | KSM·zswap(host 전체) | 1일 |
| 4 CXL tax | 2 | GUI VM을 CXL에 둘 수 있나 | 실측 | bud05 node 2 | DCD 용량 추가 | 1일 |
| 5 무거운 action 예측 | 4 | action 시작 때 알 수 있나 | trace + 실험 1 기록 | 어디서나 | 없음 | 0.5일 |
| 6 큰 VM 옮기기 | 4 | hand-off가 크기와 무관하게 이기나 | 실측 | bud05 lab | 없음 | 3일 |
| 7 가상 노드 pool | 2, 3 | 공유 pool이 노드별 CXL보다 VM을 더 받나 | 실측 | bud05 NUMA 0 + node 2 | DCD 용량 추가 | 2일 |

### 4.0 준비

- **디스크.** bud05 `/`는 11-22에 hand-off lab 실행 원자료를 지워 157 GB가 남았다(63%). 그래도 image와 VM 디스크는 NVMe `/Serverless/agent-sandbox-cxl/`(184 GB 남음)에 둔다.
- **image.** Hugging Face `xlangai/ubuntu_osworld`의 `Ubuntu.qcow2.zip`(11.4 GiB)을 받는다. VM마다 qcow2 덧씌움 파일을 만들어 base 하나를 같이 읽는다.
- **trajectory.** `xlangai/ubuntu_osworld_verified_trajs`에 여러 모델의 OSWorld-Verified 실행 기록(zip, 1~8 GiB)이 있다. screenshot은 빼고 action 기록만 푼다.
  - action은 pyautogui 코드다.
  - step마다 시각(초 단위)이 있을 것으로 보지만 확인해야 한다.
  - 15 step 실행 2개(UI-TARS, Claude 3.7 Sonnet)부터 쓴다.

### 실험 1: 데스크톱 VM 크기와 먼저 막히는 자원 (단계 1, 3)

- **질문.**
  - full OS agent VM은 실제로 얼마나 상주하고, 배치 뒤 얼마나 자라나?
  - CPU는 몇 vCPU를 동시에 쓰나?
  - 메모리 ÷ CPU가 SWE microVM(코어당 약 8 GiB)과 Docker SWE(1.9)에 비해 어디인가?
  - 대기 중 얼마나 식나?
- **구성.**
  - OSWorld Ubuntu VM을 QEMU/KVM으로 띄우고, NUMA 0의 CPU와 메모리에만 묶는다.
  - 크기는 4 GiB·vCPU 2개와 8 GiB·vCPU 4개 두 가지다.
  - 메모리 줄이기는 모두 끈다(balloon 없음, KSM·zswap 끔). 이것이 줄이기 전 기준이다.
- **부하.**
  - 공개 trajectory의 action을 guest 안 OSWorld 서버로 기록된 순서대로 보낸다. 화면이 기록과 달라져도 다음 action을 보낸다(SWE와 같은 open-loop).
  - action 사이 대기는 기록된 step 간격을 쓰고, 0.5배와 2배도 돈다. 기록이 없으면 iter2 RL의 LLM 대기 분포(중앙값 7.7초)를 쓴다.
  - 메모리 한도로 VM 수를 늘려 노드 0을 채운다(11-19 `fill.py` 방식).
- **기록**(VM마다 1초):
  - QEMU 익명 메모리, guest MemAvailable과 page cache
  - vCPU thread 실행 시간
  - step마다 바뀐 page(soft-dirty)
  - 10초·30초 넘게 안 쓴 page(idle page tracking, 11-19 memscan)
  - VM 사이 같은 내용 page(page hash), lz4 압축률 표본
  - 부팅 시간, QEMU snapshot 시간과 크기
- **산출.** VM당 상주와 배치 뒤 자라는 폭, 메모리 ÷ CPU, 식은 몫, 합칠 수 있는 몫, burst 폭, 먼저 막힌 자원. 시계열은 실험 2와 7의 입력으로 남긴다.
- **판정.**
  - 상주가 VM당 1.5 GiB 이상이고 식은 몫이 크면 1단계가 선다.
  - 배치 뒤 자라는 폭이 크고 VM마다 다르면 3단계가 선다.
  - burst가 vCPU 여럿을 동시에 쓰고 코어당 메모리 4 GiB 근처에서 CPU도 차면 4단계가 할 일이 있다.
- **두 번째 종류.** 시간이 되면 Android emulator(AndroidWorld)를 같은 방식으로 잰다. Windows는 image(약 30 GB)와 라이선스 문제로 뒤로 미룬다.

### 실험 2: 혼재 노드 simulator (단계 3, 4)

- **질문.** container와 VM이 한 노드에 섞여 있을 때, container의 양보로 VM의 메모리 쏠림과 CPU burst가 얼마나 풀리나? 남는 몫이 pool과 hand-off의 몫이다.
- **입력.**
  - 실험 1 전에는 대리 입력을 쓴다. container는 SWE 1 GiB 미만과 RL이고, VM은 넓힌 SWE 1 GiB 이상이다(11-20 `imbalance_wide.py`, CPU도 크기에 맞춰 늘린 입력).
  - 실험 1 뒤에는 VM 입력을 실측 시계열로 바꾼다.
- **모형.**
  - 16노드 × 96코어, CPU당 DRAM 2·4 GiB.
  - VM 몫은 노드 메모리의 25·50·100%다. 100%는 VM 전용 노드다.
  - container는 급한 일 또는 덜 급한 일이다.
    - 덜 급하면 VM이 원할 때 CPU를 빼앗기고, 그만큼 느려진다.
    - 메모리는 container를 회수해 내준다. 비용은 11-19 swap 실측(1 MiB 다시 읽을 때 SWE 100~200 ms, RL 24~30 ms)으로 셈한다.
  - 배치는 지금 보이는 값(2개 뽑기: 메모리, CPU, 개수)과 무작위다.
- **지표.**
  - VM 몫 초과 노드-시간과, 그중 container 양보로 받은 몫(container 느려짐 포함)
  - 남은 몫을 공유 pool로 받을 크기(p99)
  - VM CPU 넘친 일과, 그중 다른 노드에 빈 코어가 있던 몫(hand-off 상한)
  - VM 몫 크기별 노드당 VM 수
- **판정.**
  - container가 덜 급할 때 양보가 VM 넘침의 대부분을 받으면, 4단계는 "container도 급한 동기 RL"로 좁힌다.
  - VM 몫이 작을수록 남는 몫이 커지는지 본다(사용자 가정).

### 실험 3: VM 메모리 줄이기 (단계 1, 2의 대안)

- **질문.** 남이 쓰는 줄이기를 다 켜도 메모리가 막히나?
- **구성.** 실험 1과 같은 부하에 하나씩 켠다.
  - virtio-balloon free page reporting: VM마다 켜므로 host 설정은 바꾸지 않는다.
  - DAMON 기반 회수: QEMU process 대상.
  - KSM과 zswap: host 전체 설정이라 사용자 승인 뒤에 한다.
- **기록.** 상주 메모리 변화, step 시간 변화(특히 대기 뒤 첫 action), 줄이기에 드는 CPU.
- **판정.** 줄인 뒤 상주로 실험 1의 판정을 다시 한다. 2~4단계의 이득은 줄인 뒤 상태 위에서 잰다.

### 실험 4: GUI VM의 CXL tax (단계 2)

- **질문.** 데스크톱 VM 메모리를 CXL에 두면 action이 얼마나 느려지나?
- **구성.**
  - 같은 trajectory를 VM 메모리 전체를 node 0(DRAM)에 두고 한 번, node 2(CXL DCD)에 두고 한 번 돈다. 3회, 순서 무작위다.
  - DCD 용량 추가는 사용자 승인 뒤에 한다. memory block은 손으로 online하지 않는다.
- **기록.** action마다 실행부터 화면이 안정될 때까지의 시간과 CPU 시간. screenshot 인코딩 시간은 따로 잰다.
- **판정.**
  - 대부분의 action이 +5% 안이면 CXL에 두는 쪽이 선다.
  - 무거운 action만 크면 SWE처럼 나눠 두기(기본 CXL, 새 메모리 DRAM)를 쓴다. 다만 QEMU에는 아직 구현이 없다.

### 실험 5: 무거운 action을 시작 때 알 수 있나 (단계 4)

- **질문.** SWE에서는 명령 문자열만 보고 무거운 명령 CPU의 98.5%를 잡았다 [trace 11-13]. GUI action도 그런가?
- **방법.** 실험 1에서 step마다 잰 CPU와 action 문자열을 잇는다. 문자열은 앱 실행, terminal에 입력한 명령, 단축키, 클릭으로 나눈다. 규칙 기반 분류의 재현율·정밀도와 잡힌 CPU 비율을 SWE와 같은 방식으로 낸다.
- **판정.**
  - 잡히면 action 시작 때 옮긴다.
  - 안 잡히면 실행 중에 옮겨야 한다. 그때는 VM이 CXL에서 돌아야 하므로 실험 4의 tax가 직접 비용이 된다.

### 실험 6: 큰 VM 옮기기 비용 (단계 4)

- **질문.** 4·8 GiB 데스크톱 VM에서 hand-off가 live migration과 snapshot 복원보다 얼마나 낫고, 그 차이가 크기에 따라 벌어지나?
- **구성.**
  - live migration: 같은 서버 안 QEMU 사이에서, 대역폭을 25·100 Gb/s로 제한한다. VM이 쉴 때와 무거운 action 중에 각각 한다.
  - snapshot 복원: NVMe 파일로 저장하고 복원한다.
  - hand-off: 데스크톱 guest를 Firecracker로 옮겨(Xvfb, guest 안 OSWorld 서버) 기존 EPT 이어받기 경로로 옮긴다. bud05 중첩 VM lab에서 한다.
- **기록.** 정지 시간, 전체 시간, 출발 노드 CPU, 옮긴 뒤 첫 action이 끝날 때까지의 시간, EPT fault 수.
- **판정.**
  - 메모리 크기를 늘려도 hand-off의 정지 시간과 첫 action 지연은 평평하고, 다른 둘은 크기에 비례하는지 본다.
  - Firecracker로 데스크톱이 안 돌면 QEMU에 EPT 이어받기를 넣는 비용을 따로 어림한다.

### 실험 7: 가상 노드로 pool 실측 (단계 2, 3)

- **질문.** 같은 DRAM과 CXL 총량에서, 노드들이 CXL을 같이 쓰면(공유 pool) 노드마다 나눠 가질 때보다 VM을 더 받고 덜 느려지나?
- **구성.**
  - bud05 NUMA 0을 가상 노드 4개로 나눈다. 노드마다 CPU 묶음(cpuset)과 DRAM 예산을 따로 둔다.
  - CXL은 node 2(DCD)를 쓴다.
  - VM은 지금 보이는 값으로 가상 노드에 배치하고, 처음에는 DRAM에 둔다.
  - 가상 노드의 DRAM 사용이 예산을 넘으면, 그 노드에서 가장 오래 쉰 VM의 메모리를 host가 node 2로 옮긴다(migratepages). guest의 도움은 필요 없다.
  - 두 조건을 비교한다.
    - 노드별 CXL: 노드마다 CXL의 1/4만 쓴다.
    - 공유 pool: 어느 노드든 CXL 전체에서 쓴다.
- **부하.** 실험 1의 데스크톱 VM이다. 그 전에는 11-19 RL·SWE microVM으로 도구를 맞춘다.
- **기록.** 받은 VM 수, CXL로 내린 양과 횟수, 다시 DRAM으로 올린 양, swap 사용, 명령·action 느려짐, 노드별 메모리 사용 시계열.
- **판정.** 공유 pool이 같은 느려짐에서 VM을 더 받거나, 같은 VM 수에서 덜 느려지면 3단계가 실측으로 선다. simulator의 "노드 여유 22~31% → pool 6~8%"와 맞춰 본다.
- **한계.** 한 서버 안의 가상 노드라 노드 사이 cache와 메모리 대역폭을 같이 쓴다. 진짜 여러 서버 CXL은 아직 없다.

### 4.1 순서와 관문

1. 준비. image와 trajectory를 받는 동안 실험 2(대리 입력)와 실험 5의 trace 쪽을 먼저 한다.
2. 실험 1.
3. **관문 A**(실험 1, 2 뒤): VM 몫에서 메모리가 먼저 막히고, VM이 배치 뒤에 제각각 자라고, container 양보로 다 풀리지 않는가? 아니면 설정을 다시 정한다.
4. 실험 3, 4, 7. 모두 승인이 필요하다(KSM·zswap, DCD 용량).
5. **관문 B**: 줄인 뒤에도 막히고, CXL tax가 받아들일 만하고, 공유 pool이 실측으로 이기는가?
6. 실험 6, 그리고 실험 2를 실측 VM 시계열로 다시 돈다.

### 4.2 사용자 승인이 필요한 것

- KSM, zswap 켜기(실험 3)
- CXL DCD 용량 추가(실험 4, 7)
- push

## 5. 결과(11-22)

### 5.1 작은 서버 simulator(실험 2를 서버 크기로 바꾼 것)

- **입력.** 실측 데스크톱 VM 전에는 대리 입력을 쓴다. 넓힌 SWE 중 최대 메모리 1 GiB 이상인 sandbox 231개이고, CPU도 크기에 맞춰 늘렸다(11-20). 시간 평균 메모리 2.1 GiB(CV 0.78), 평균 0.45코어, 코어당 4.7 GiB다.
- **모형.** 16노드, 서버 32·48·96코어, 코어당 메모리 2·4 GiB, 평균 메모리 85·95%다. 배치는 지금 보이는 값 2개 뽑기와 무작위다. `imbalance_wide.py vmonly`, `--set small`, [summary/small.jsonl](summary/small.jsonl), [보고서](summary/small-report.txt).

**메모리: 서버가 작을수록 쏠림이 크다**(평균 메모리 85%, 괄호는 지금 메모리 2개 뽑기)

| 노드 메모리(예) | 노드당 VM | DRAM 초과 시간 | 노드마다 남길 여유 p99 | 공유 pool p99 |
|---|---|---|---|---|
| 64 GiB(32코어 × 2) | 27 | 21% (25%) | 33% (40%) | 8.8% (9.7%) |
| 96 GiB(48코어 × 2) | 40 | 14% (20%) | 20% (32%) | 4.8% (7.0%) |
| 192 GiB(96코어 × 2, 48코어 × 4) | 80 | 6.5% (17%) | 8.6% (18%) | 2.2% (3.9%) |
| 384 GiB(96코어 × 4) | 160 | 1.8% (14%) | 1.5% (14%) | 0.5% (2.6%) |

수 2개 뽑기 기준이다. 메모리 쏠림은 노드 메모리 크기, 곧 노드당 VM 수가 정한다. 같은 메모리면 코어 수와 상관없이 같다. 공유 pool은 노드마다 남길 여유의 약 1/4이다. 평균 95%면 모든 크기에서 노드가 DRAM을 넘는 시간이 31~40%다.

**CPU: 서버가 작을수록 넘친다**(코어당 메모리 4 GiB, 평균 메모리 85%, CPU 평균 75%. 괄호는 지금 메모리 2개 뽑기)

| 서버 | 노드가 CPU를 넘는 시간 | 넘친 CPU 일 | 다른 노드에 빈 코어가 있던 몫 |
|---|---|---|---|
| 32코어 | 19% (23%) | 4.9% (7.5%) | 100% |
| 48코어 | 15% (19%) | 2.8% (4.8%) | 100% |
| 96코어 | 8% (16%) | 0.9% (3.0%) | 100% |

- 코어당 메모리 2 GiB(CPU 평균 38~42%)에서는 32코어 서버도 넘친 CPU 일이 0.8% 이하다. 메모리가 먼저 막힌다.
- 코어당 4 GiB, 곧 DRAM 2 GiB에 CXL 2 GiB를 더한 비율에서 CPU가 메모리와 함께 찬다. 이때 hand-off의 몫(넘친 일 × 옮길 곳 있는 몫)은 32코어 서버가 96코어의 약 5배다.
- 평균 메모리 95%(CPU 84%)면 32코어에서 넘친 일이 7.4~9.7%, 96코어에서 2.3~5.5%다.

**뜻.** 사용자 가정(작은 서버)이 맞다. 같은 코어당 메모리에서 서버가 작으면 노드당 VM이 적어 메모리 쏠림(3단계)과 CPU 쏠림(4단계)이 모두 커진다. 대리 입력의 결과이므로 실측 VM 시계열로 다시 돈다(5.3).

### 5.2 데스크톱 VM 실측(실험 1)

**구성.**
- OSWorld Ubuntu VM(QEMU 8.2/KVM, 4 GiB, vCPU 4, OSWorld Docker provider 기본값)을 bud05 NUMA 0에서 돌렸다.
  - VM은 CPU 4-31,68-95(28코어)를 쓰고, 메모리는 node 0에 묶었다.
  - controller와 memscan은 CPU 0-3,64-67에서 돌았다.
- 자리 24개를 40분 채웠다. 자리마다 VM을 띄우고, task 준비를 하고, 공개 trajectory를 기록된 간격대로 재생한 뒤 VM을 지운다.
  - trajectory: OSWorld-Verified, Claude 3.7 Sonnet 15-step, 10개 영역의 301개 task. step 간격 중앙값은 15초다.
- 메모리 줄이기는 모두 껐다. lab·quiet 잠금을 잡았다.
- 코드: `experiments/desktop-vm/desktop_vm.py`, `analyze.py`. 결과: [summary/fill24.json](summary/fill24.json).

**결과**(VM 168개, 모두 끝까지 돌았다. task 준비 오류 0, screenshot 실패 0)

| 항목 | 값 |
|---|---|
| 부팅(서버 응답까지) | 11.1초. 그동안 1.9코어 |
| QEMU 익명 메모리: 부팅 직후 / task 준비 뒤 / episode 끝 | 2.5 / 2.9 / 3.5 GiB(p90 3.9) |
| VM별 시간 평균 메모리 | 3.4 GiB, VM 사이 CV 0.07 |
| 배치 뒤 자라는 양(끝 − 부팅 직후) | 중앙값 0.96 GiB(p10 0.62, p90 1.40) |
| episode 중 CPU | VM당 평균 0.13코어(QEMU 전체 0.25, 부팅 포함). 1코어를 넘는 초 7.6%, 가장 넓은 초 4.0코어 |
| 노드(VM 24개 동시) | 메모리 80 GiB, CPU 4.7코어(28코어의 17%, p99 11.5코어). 쓰는 코어당 17 GiB |
| 30초 넘게 안 쓴 guest page(memscan) | 26% |
| VM 사이 같은 내용 page | VM 10~16개가 같이 있을 때 guest 메모리의 47~49%가 다른 page와 같다. VM 안 중복은 10%, zero page는 7.5% |

guest 안에서 본 한 VM(부팅 몇 분 뒤): 익명 0.95 GB, page cache 1.47 GB, slab 0.18 GB, 빈 메모리 1.24 GB였다. host 쪽 3.1 GiB가 guest가 쓰는 양보다 큰 것은, guest가 한 번 썼다 비운 page가 host로 돌아오지 않기 때문이다.

**balloon free page reporting을 켜도 거의 그대로다**(같은 seed와 task로 두 번째 실행, VM 168개, [summary/fill24-balloon.json](summary/fill24-balloon.json)).
- VM별 시간 평균 3.35 GiB(끈 경우 3.38), 노드 합 79.4 GiB(79.7)로 0.4% 줄었다. CPU도 같았다.
- free page reporting은 guest의 빈 메모리를 2 MiB 덩어리(order 9)로만 돌려준다. 켠 VM 하나를 보면 guest 빈 메모리 0.83 GB 중 약 0.4 GB가 host에 남았다.
- 더 큰 몫은 page cache다(1.66 GB). guest는 메모리 압박이 없어 cache를 그대로 둔다.
- 데스크톱 VM 메모리를 줄이는 것은 balloon이 아니라 두 가지다. 하나는 guest 안 page cache 회수(DSec의 DAMON)이고, 다른 하나는 VM 사이 중복 합치기다. 둘 다 아직 재지 않았다.

**네 단계에 주는 뜻**
- **1단계(메모리가 막는다)는 강하게 선다.**
  - 메모리가 찼을 때 CPU는 17%였다. 쓰는 코어당 17 GiB로, 11-19 SWE microVM(약 8 GiB)보다 메모리 쪽으로 더 멀다.
  - 코어당 메모리가 12 GiB를 넘어야 CPU가 먼저 막힌다.
- **줄이기의 몫이 크다(2단계의 대안).**
  - 같은 image에서 뜬 VM이라 VM 사이 같은 내용이 절반 가까이다. KSM이 노드 안에서 이만큼 합칠 수 있다.
  - pool에 한 벌만 두면 노드 사이로도 합친다(pool의 이점). 다만 KSM과 비교해 재야 한다(승인 필요).
- **3단계는 크기가 고를 때 약하다.** VM 크기가 거의 같고(CV 0.07), 배치 뒤 자라는 양도 비슷하다(CV 0.27). 그래서 지금 메모리나 개수로 배치하면 쏠림이 작다(5.3).
- **4단계는 이 속도의 데스크톱 VM에서는 할 일이 없다.** CPU가 낮다. 더 빠른 agent(간격 ×0.3)의 결과는 아래 5.5에 있다.

### 5.3 데스크톱 VM 시계열로 돌린 작은 서버 simulator

입력은 5.2의 VM 168개 시계열이다(시간 평균 3.4 GiB, CV 0.07, QEMU 전체 CPU 0.25코어, 수명 중앙값 5.2분). 설정은 5.1과 같다. [summary/small-desktop.jsonl](summary/small-desktop.jsonl).

| 노드 메모리(노드당 VM) | 평균 메모리 | DRAM 초과 시간: 수 / 지금 메모리 / 무작위 | 노드 여유 p99(수) | 공유 pool p99(수) | CPU 평균 |
|---|---|---|---|---|---|
| 64 GiB(16) | 85% | 3.4% / 4.5% / 24% | 3.0% | 1.4% | 10% |
| 64 GiB(18) | 95% | 29% / 27% / 39% | 14% | 6.4% | 11% |
| 128 GiB(36) | 95% | 15% / 21% / 36% | 6.2% | 2.6% | 23% |
| 192 GiB(49) | 85% | 0% / 0% / 9.6% | 없음 | 0 | 10~20% |
| 192 GiB(55) | 95% | 12% / 16% / 33% | 4.7% | 2.6% | 11~23% |

- 크기가 고른 VM이라, 지금 메모리나 개수로 고르면 평균 85%에서는 거의 넘치지 않는다. 쏠림은 평균 95%와 작은 서버에서만 남는다. 64 GiB 노드에서 노드 여유 14%를 pool이 6.4%로 받는다.
- 무작위 배치는 크게 쏠린다. 배치가 메모리를 볼 수 있으면 대부분 막는다는 뜻이다.
- CPU는 넘치지 않았다(평균 10~23%).

### 5.4 무거운 action을 시작 때 알 수 있나(실험 5, 첫 실행 기록)

- episode CPU의 85%는 action 직후 3초 밖에 있다. 데스크톱과 열린 앱이 계속 쓰는 바탕 부하다. action 직후 3초는 15%뿐이다.
- 그 안에서는 상위 10% step이 64%를 쓴다. 2코어초를 넘는 무거운 step은 9%다.
- "task 준비 뒤 첫 step"이라는 규칙 하나가 무거운 step의 61%를 잡는다(정밀도 0.68). 준비 단계에서 연 앱이 뜨는 CPU다.
- 나머지는 앱 아이콘 클릭, 단축키로 terminal 열기 같은 것이다. 클릭 좌표만으로는 무엇이 열릴지 모른다.
- SWE(명령 문자열로 무거운 CPU의 98.5%)보다 훨씬 약하다. GUI에서 hand-off를 쓰려면 action 시작 때가 아니라 CPU를 보고 옮겨야 한다. 그러면 실행 중 CXL tax가 직접 비용이 된다(실험 4).

### 5.5 빠른 agent: step 간격 ×0.3

- **왜.** 공개 trajectory는 API 모델의 평가 실행이다. step 간격 중앙값이 Claude 13~15초, UI-TARS 9초다. RL에서 정책 모델을 직접 띄우면 더 빠를 수 있다.
- **구성.** 5.2와 같다. 다만 Claude 3.7 Sonnet 100-step trajectory(300개 task)를 기록 간격의 0.3배(약 4초)로 재생했다. 30분, VM 291개가 모두 끝까지 돌았다. [summary/fill24-fast.json](summary/fill24-fast.json).

| 항목 | 기록 간격 | 간격 ×0.3 |
|---|---|---|
| VM별 시간 평균 메모리 | 3.4 GiB | 3.5 GiB |
| QEMU 전체 CPU(부팅 뺀 시계열) | 0.18코어 | 0.53코어 |
| 1코어를 넘는 초(vCPU) | 7.6% | 21% |
| 노드(VM 24개): 메모리 / CPU / CPU p99 | 80 GiB / 17% / 11.5코어 | 78 GiB / 45% / 22.7코어 |
| 쓰는 코어당 메모리 | 17 GiB | 6.2 GiB |

action당 CPU는 비슷하고 action이 3배 잦아져, VM당 CPU가 약 3배가 되었다. 메모리는 그대로다.

**작은 서버 simulator**(부팅 뺀 시계열, 코어당 메모리 2·4·8 GiB, [summary/smalldesk-fast.jsonl](summary/smalldesk-fast.jsonl), 기록 간격은 [summary/smalldesk-slow.jsonl](summary/smalldesk-slow.jsonl))

| 서버 | 코어당 메모리 | 평균 메모리 | CPU 평균 | 노드가 CPU를 넘는 시간 | 넘친 CPU 일 | 공정 분배 늘어짐 |
|---|---|---|---|---|---|---|
| 32코어 | 8 GiB | 85% | 77% | 7.6~8.7% | 0.8~1.0% | 1.01 |
| 32코어 | 8 GiB | 95% | 86% | 20~21% | 2.4~2.6% | 1.03 |
| 48코어 | 8 GiB | 85% | 77% | 4.4~4.6% | 0.35~0.38% | 1.00 |
| 48코어 | 8 GiB | 95% | 86% | 16~17% | 1.4~1.6% | 1.02 |
| 96코어 | 8 GiB | 85% | 77% | 0.8~1.2% | 0.03~0.06% | 1.00 |
| 96코어 | 8 GiB | 95% | 86% | 7.6~9.1% | 0.40~0.51% | 1.00~1.01 |

수 2개 뽑기와 지금 메모리 2개 뽑기의 범위다. 넘친 일은 모두 같은 초에 다른 노드에 빈 코어가 있었다.

- 코어당 2·4 GiB에서는 빠른 agent도 CPU 19~43%로 넘치지 않는다. 메모리가 먼저 막힌다.
- 코어당 8 GiB(예: DRAM 4 + CXL 4)에서 CPU가 77~86%로 차고 넘치기 시작한다. 4단계가 생기는 자리다. 작은 서버(32코어)에서 넘친 일은 96코어의 5~25배다.
- 기록 간격(0.18코어)은 코어당 8 GiB에서도 CPU 28~32%로 넘치지 않는다.
- 같은 비율의 SWE 대리 입력(32코어, 4 GiB, 85%에서 넘친 일 4.9%)보다 작다. 데스크톱 VM의 CPU는 바탕 부하가 커서 덜 튄다.

### 5.6 네 단계 판정(11-22)

| 단계 | 판정 | 근거 |
|---|---|---|
| 1. VM 수는 메모리가 막는다 | **선다** | 데스크톱 VM 24개가 메모리 80 GiB를 채울 때 CPU 17%(빠른 agent 45%). 쓰는 코어당 6~17 GiB |
| 2. CXL로 늘린다 | **필요하다. 줄이기는 직교로 양쪽에 켠다(11-22 사용자)** | 줄이기(중복 합치기, 압축, guest page cache 회수)는 VM당 필요한 바이트를 줄이고, CXL은 용량을 더한다. 둘은 겹쳐 쓸 수 있다. 다만 두 가지는 직교가 아니다. (1) 줄인 뒤의 메모리 ÷ CPU가 1단계의 문턱을 정한다. VM 사이 같은 내용 47~49%와 zero page 7.5%를 다 합치면 VM당 약 1.5 GiB가 된다[계산, 상한]. 그러면 쓰는 코어당 기록 간격 8.2 GiB, 빠른 agent 2.9 GiB다. 빠른 agent는 코어당 DRAM 4 GiB 서버에서 CPU가 먼저 막혀 CXL이 필요 없어진다. (2) 우리 설계는 guest RAM을 CXL frame mapper(devdax와 같은 공유 매핑)에 둔다. 그래서 host의 KSM(익명 private page만 합침), zswap, balloon으로 돌려받기가 그대로는 듣지 않는다. pool 안에 빈 chunk 반납과 노드 사이 읽기 전용 중복 공유를 따로 만들어야 한다. 만들면 노드마다 도는 KSM보다 나은 pool의 이점이 된다. CXL tax는 재지 않았다(승인 필요) |
| 3. pool로 메모리 불균형 | **크기가 고르면 약하다** | 데스크톱 VM은 크기가 고르다(CV 0.07). 메모리나 개수로 배치하면 평균 85%에서 거의 넘치지 않는다. 쏠림은 평균 95%와 작은 서버(64 GiB 노드: 노드 여유 14%, pool 6.4%)에서만 남는다. 크기가 제각각인 대리 입력에서는 클 수 있다(5.1). VM 종류가 섞인 fleet을 재야 한다 |
| 4. hand-off로 CPU 불균형 | **조건부로 선다** | 빠른 agent이고, CXL로 코어당 메모리가 8 GiB쯤이고, 서버가 작을 때만이다. 32코어에서 넘친 CPU 일은 0.8~2.6%이고, 모두 옮길 곳이 있었다. 느린 agent나 96코어 서버에서는 거의 없다. 무거운 action을 시작 때 알기 어려워, 옮기는 시점은 CPU를 보고 정해야 한다 |

### 5.7 한계

- 데스크톱 VM 한 종류(OSWorld Ubuntu, 4 GiB)만 쟀다. Android, Windows, 크기가 다른 VM이 섞인 fleet은 아직이다.
- trajectory는 평가 실행이고, open-loop 재생이라 화면이 기록과 달라질 수 있다. 다만 task 준비 오류와 screenshot 실패는 0이었다.
- VM을 episode마다 새로 띄웠다(부팅 11초, 21.5코어초). RL은 snapshot이나 warm pool로 되돌리는 경우가 많아, simulator 입력에서는 부팅을 뺐다.
- bud05 NUMA 0 한 곳(28코어, SMT 켬)에서 쟀다. 서버 사이 동작은 simulator다.
- KSM, zswap, guest page cache 회수, CXL tax는 재지 않았다. host 설정 변경과 DCD 용량 추가는 승인이 필요하다.

## 6. 근거

- DSec: https://arxiv.org/html/2609.22978v1 (2.2절 backend, 3.3절 입장 제어, 4.3절 사용률과 밀도, 5.2절 LS/BE와 microVM 메모리, 6.3절 container pause)
- OSWorld Docker 안내(8 GB·8코어 예): https://timothyxxx.github.io/OSWorld/installation/install_provider/docker.html
- OSWorld image: https://huggingface.co/datasets/xlangai/ubuntu_osworld
- OSWorld-Verified trajectory: https://huggingface.co/datasets/xlangai/ubuntu_osworld_verified_trajs
- dockur/windows 기본값(2코어, 4 GB): https://raw.githubusercontent.com/dockur/windows/master/readme.md
- Android emulator 최소 4 GB(API 37): https://developer.android.com/studio/releases/emulator
- 우리 자료: [placement-imbalance-20261119](../placement-imbalance-20261119/README.md)(크기 넓히기, CPU 늘리기), [sandbox-footprint-20261118](../sandbox-footprint-20261118/README.md)(microVM 실측, swap), 10-14 Docker 재생 `paired-traces.csv`(옛 checkout)
