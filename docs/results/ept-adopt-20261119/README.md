# ept-adopt 결과

Compute Express Link(CXL)에 가상기계(Virtual Machine, VM)의 주메모리(Random Access Memory, RAM)와 Extended Page Tables(EPT) 표 page만 둔다.
나머지 Kernel-based Virtual Machine(KVM) 객체는 로컬 동적 메모리(Dynamic Random Access Memory, DRAM)에 둔다.
기존 RAM 쓰기 기록과 mailbox는 유지한다.

설계의 자세한 순서는 [EPT_ADOPT.md](../../../implementation/virtual_machine/EPT_ADOPT.md)에 있다.
구현은 누적 [KVM patch](../../../implementation/virtual_machine/kvm-dax-state.patch)와 [Firecracker patch](../../../implementation/virtual_machine/firecracker.patch)에 있다.

## 시험 환경과 범위

서버는 bud05다. CPU와 메모리는 node 0만 썼다.
바깥 host(L0) 위의 두 실험 host(L1) 사이에서 guest(L2)를 넘겼다.
일관성이 있는 DRAM 위에 CXL을 에뮬레이트했다.
실제 비일관 장비, 다른 물리 서버와 node 2 DCD(Dynamic Capacity Device)는 시험하지 않았다.

Guest RAM은 2 GiB다. VM별 EPT 영역은 16 MiB다.
가상 처리기(virtual Central Processing Unit, vCPU) 1·2개를 시험한다.
Block 장치는 읽기 전용이다.
단일 RAM memslot과 같은 root role을 쓴다.
Page Modification Logging(PML)과 기본 dirty bitmap을 켰다.
실행 금지 큰 page 완화는 껐다.
Dirty ring, manual protect와 중첩 guest는 거절한다.

기존 fixture는 DRAM에서 boot한 뒤 CXL로 promotion한다.
영역 좌표는 VM 생성 때 받는다.
커널 등록은 첫 CXL 실행 전에 한다.
수신 memslot의 주소는 미리 고정한다.
CXL mapping은 기존 lease 전달 때 같은 주소로 붙인다.
Adopt 전에 두 host의 물리 배치와 현재 mapper를 검사한다.

시간의 전체 구간은 소유 host의 단조 시계로 정지 요청 직전부터 guest 응답(Acknowledgment, ACK) 관찰까지다.
Guest ACK 뒤 payload 쓰기·검증을 마친 다음 이동을 요청한다.
정답지 실행 시간은 성능 표본에 넣지 않는다.
단계 시간은 가상기계 모니터(Virtual Machine Monitor, VMM)의 단조 시계다.
Software Engineering(SWE) 무거운 명령은 원본 명령 그대로 실행한다.
명령 시간은 guest 실행 시간과 host 관찰 시간을 함께 남긴다.

## 정확성

한 page는 4 KiB다. 정답지는 L0에서 RAM page hash와 실제 cache 명령 영수증을 확인한다.
표 hash는 release 뒤와 adopt 직후의 전체 16 MiB를 비교한다.

| 시험 | 완료한 한 방향 이동 | 누락 page | 표 hash 불변 |
|---|---:|---:|---:|
| 256 MiB 전체 word 쓰기, 200왕복 | 400 | 0 | 400/400 |
| Native 미리 정리 | 6 | 0 | 6/6 |
| Hypertext Transfer Protocol(HTTP) 미리 정리 | 6 | 0 | 6/6 |
| keepalive 없는 2 vCPU 경합 | 60 | 0 | 60/60 |

미리 정리 두 경로는 수신 선무효화를 실제로 실행했다.
두 smoke의 import 전체 RAM 무효화 fallback은 0회다.
이전 host의 RAM munmap, VM·vCPU·mapper fd 종료와 VMM 종료 뒤에도 표 hash가 같았다.
이때 현재 소유 guest도 멈춰 두어 CPU의 accessed/dirty bit 변경을 배제했다.

## 재현 자료

원시는 `implementation/virtual_machine/.local/run21/`에 있다.
각 trial은 소스 commit, 입력과 binary hash, 명령, 원문 log와 report를 남긴다.
시간 trial은 시작 전 프로세스 목록, `/proc/loadavg`와 node 0 여유 메모리를 남긴다.
실행 순서는 A→B→A→B(ABAB)다. A는 `ept-adopt`다.
B는 각각 `direct`와 `reuse-prepared`다.
대조군 `reuse-prepared`는 같은 준비 worker·상태·장치 경로에서 EPT adopt만 끈다.
로컬 EPT를 버린 뒤 다시 채운다.

기존 lab을 시작한 뒤 `lab.py ept-adopt-bench --help`로 실행 인자를 볼 수 있다.
결과 재분석은 아래 명령으로 한다. 실행 중인 VM과 장치에는 접근하지 않는다.

```sh
numactl --cpunodebind=0 --membind=0 python3 \
  docs/results/ept-adopt-20261119/analyze.py
```

분석은 측정과 겹치지 않도록 기존 공유 quiet 잠금을 쓴다.
