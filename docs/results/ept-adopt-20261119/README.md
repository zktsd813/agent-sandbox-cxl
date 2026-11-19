# ept-adopt 결과

Compute Express Link(CXL)에 가상기계(Virtual Machine, VM)의 주메모리(Random Access Memory, RAM)와 Extended Page Tables(EPT) 표 page만 둔다.
나머지 Kernel-based Virtual Machine(KVM) 객체는 로컬 동적 메모리(Dynamic Random Access Memory, DRAM)에 둔다.
기존 RAM 쓰기 기록과 mailbox는 유지한다.

설계의 자세한 순서는 [EPT_ADOPT.md](../../../implementation/virtual_machine/EPT_ADOPT.md)에 있다.
구현은 누적 [KVM patch](../../../implementation/virtual_machine/kvm-dax-state.patch)와 [Firecracker patch](../../../implementation/virtual_machine/firecracker.patch)에 있다.

## 시험 환경과 범위

서버는 bud05다. 중앙 처리 장치(Central Processing Unit, CPU)와 메모리는 node 0만 썼다.
바깥 host(L0) 위의 두 실험 host(L1) 사이에서 guest(L2)를 넘겼다.
일관성이 있는 DRAM 위에 CXL을 에뮬레이트했다.
실제 비일관 장비, 다른 물리 서버와 node 2 DCD(Dynamic Capacity Device)는 시험하지 않았다.

Guest RAM은 2 GiB다. VM별 EPT 영역은 16 MiB다.
가상 처리기(virtual Central Processing Unit, vCPU) 1·2개를 시험한다.
Block 장치는 읽기 전용이다.
단일 RAM memslot과 같은 root role을 쓴다.
Page Modification Logging(PML)과 기본 dirty bitmap을 켰다.
Direct의 전역 CXL 객체 allocator는 껐다. 켜져 있으면 ept-adopt 등록을 거절한다.
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
| 256 MiB 전체 word 쓰기, 1 vCPU 200왕복 | 400 | 0 | 400/400 |
| 256 MiB 모든 cache line 갱신, 2 vCPU 200왕복 | 400 | 0 | 400/400 |
| Native 미리 정리 | 6 | 0 | 6/6 |
| Hypertext Transfer Protocol(HTTP) 미리 정리 | 6 | 0 | 6/6 |
| keepalive 없는 2 vCPU 경합 | 60 | 0 | 60/60 |

미리 정리 두 경로는 수신 선무효화를 실제로 실행했다.
두 smoke의 import 전체 RAM 무효화 fallback은 0회다.
이전 host의 RAM munmap, VM·vCPU·mapper 파일 기술자(file descriptor, fd) 종료와 VMM 종료 뒤에도 표 hash가 같았다.
이때 현재 소유 guest도 멈춰 두어 CPU의 accessed/dirty bit 변경을 배제했다.

SWE 세 fixture의 무거운 명령 정책에서 W=0·1초를 모두 통과했다.
W는 미리 정리 뒤 이동까지 기다린 시간이다.
Gin 2 vCPU 조건도 통과했다.
Gin은 조건당 3회, PHP-CS-Fixer는 5회, MicroPython은 6회 이동했다.

| 오류 주입 | 검출 결과 |
|---|---|
| 영역 밖 root | 커널이 재개 전에 `EINVAL`로 거절 |
| 잘못된 bump | 커널이 재개 전에 `EINVAL`로 거절 |
| EPT 영역 무효화 생략 | 실제 명령 영수증의 영역 누락을 정답지가 검출 |
| RAM 무효화 생략 | 실제 수신 범위가 쓰기 log 합집합을 덮지 못함을 정답지가 검출 |

네 조건 모두 완료된 hand-off는 0회다.
`EINVAL`은 잘못된 인자 오류다.

기존 Python 시험은 65개 중 57개 통과, 입력 fixture가 필요한 8개는 skip했다.
Rust 계약 시험은 24개 통과, 별도 resident packet fixture가 필요한 1개는 ignored다.
18~20의 저장된 정답지 79조건도 현재 cache 영수증 검증기로 재생했다.
2,707개 hash 경계에서 누락 page는 0이었다.
기존 bitmap과 변경 page 자료를 다시 읽어 포함 관계도 독립 확인했다.
최종 direct SWE 자료의 기존 resident 증거 변조 8종도 모두 거절했다.
원본 자료는 수정하지 않았다.

## EPT를 다시 채우는가

이미 매핑된 256 MiB payload를 재개 뒤 다시 썼다.
각 모드는 warm-up 뒤 6회 이동을 비교했다.

| 모드 | 이동당 L1 EPT violation |
|---|---:|
| ept-adopt | 0 |
| direct | 0 |
| reuse-prepared | 65,536 |

새로 접근한 guest page의 fault까지 0이라는 뜻은 아니다.
비교 대상은 이전 host가 이미 매핑한 payload다.

## 정지부터 guest ACK까지

각 비교는 독립된 ABAB다. 블록마다 warm-up 20회 뒤 400회를 측정했다.
각 모드의 합계는 800회다. 단위는 ms다.
시간용 payload는 256 MiB의 모든 cache line에서 첫 8 byte를 갱신한다.

| 비교 | 모드 | 중앙값 | 95백분위 |
|---|---|---:|---:|
| direct 비교 | ept-adopt | 33.58 | 39.01 |
| direct 비교 | direct | 21.09 | 21.99 |
| reuse 비교 | ept-adopt | 34.31 | 38.83 |
| reuse 비교 | reuse-prepared | 31.40 | 35.22 |

외부 측정 VM은 없었다. 시작 전 다른 Firecracker·QEMU는 8블록 모두 0개였다.
L0의 1분 load average는 0.85~1.93이었다.
Node 0 여유 메모리는 49.2~52.5 GiB였다.
전체 프로세스와 1·5·15분 부하는 각 원시에 있다.

`ept-adopt`는 이 lab에서 `direct`보다 느렸다.
로컬 장부를 RCU(Read-Copy Update)로 정리하는 release와 상태 복원 비용이 있다.
`reuse-prepared`의 payload EPT 재충전은 ACK 뒤 payload 쓰기 때 일어난다.
따라서 이 표와 이동 뒤 무거운 명령 시간을 함께 봐야 한다.

아래는 direct 비교의 ept-adopt 두 블록을 합친 단계 중앙값이다.
각 단계는 800개 표본이다. 단위는 ms다. 단계 중앙값의 합은 전체 중앙값과 다르다.

| 단계 | 중앙값 |
|---|---:|
| RAM 내려쓰기·기록 발행 | 8.264 |
| release | 5.849 |
| EPT 내려쓰기 | 0.277 |
| vCPU·VM 상태 읽기 | 0.242 |
| 순수 직렬화 | 0.144 |
| RAM·EPT 무효화와 mapper 붙이기 | 7.904 |
| adopt, lab 중첩 보완 포함 | 2.317 |
| 상태 넣기 | 0.429 |
| 장치 복원 | 0.119 |

상태 packet은 1 vCPU에서 약 14.1 kB다.
2 vCPU에서는 약 25.7 kB다. RAM 내용은 packet에 넣지 않는다.

## 메모리 크기와 adopt 비용

조건마다 warm-up 뒤 400회 이동을 측정했다. 시간 단위는 ms다.
표 page 수는 `bump / 4096`이다. 폐기한 표도 포함하는 할당량이다.
할당량은 warm-up을 포함한 420회 관찰의 중앙값이다.
Guest boot가 만진 메모리도 표에 포함된다.

| Payload | 할당 표 page | adopt 중앙값 | 95백분위 |
|---|---:|---:|---:|
| 16 MiB | 52 | 0.302 | 0.345 |
| 256 MiB | 172 | 2.012 | 2.134 |
| 1 GiB | 554 | 8.025 | 8.481 |

표가 많은 조건에서 adopt 시간이 늘었다.
이 수치는 lab 전용 중첩 shadow 보완을 포함한다.
실제 하드웨어의 adopt 시간으로 그대로 옮길 수는 없다.
세 조건 모두 외부 Firecracker·QEMU는 0개였다.
시작 전 1분 load average는 0.77~1.02였다.

## 이동 뒤 첫 무거운 명령

SWE 원본 명령을 바꾸지 않았다. 각 비교는 ABAB다.
블록마다 새 lab 22개에서 101회 이동했다. 각 모드 합계는 202회다.
아래는 fixture별로 같은 명령 구성을 합친 host 관찰 시간의 중앙값이다.
단위는 ms다. 요청 전송과 완료 관찰 비용도 포함한다.
Guest 실행 시간과 명령별 분포는 JSON에 따로 있다.
Guest 시간의 기록 해상도는 10 ms다.

| 비교 | Fixture | 표본/모드 | ept-adopt | 대조군 |
|---|---|---:|---:|---:|
| direct | gin-gonic | 48 | 2,885.20 | 2,902.12 |
| direct | php-cs-fixer | 70 | 495.95 | 488.05 |
| direct | micropython | 84 | 603.71 | 601.80 |

## 코드 크기

시작 commit `8de3e8e`의 소스와 비교한 추가 줄 수다.
빈 줄과 주석도 센다. 누적 patch 전체 크기는 세지 않는다.

| 구분 | ept-adopt 추가 | direct 전용 핵심 |
|---|---:|---:|
| KVM | 474 | 최소 1,260 |
| Firecracker | 416 | 975 |
| 시험 도구 | 443 | 최소 268 |

KVM과 Firecracker는 각각 수백 줄이다.
시험 도구에는 새 명령, 표 hash·오류 주입·비소유 host 종료 검증을 포함한다.
결과 분석 도구는 별도다. 정확한 파일별 수치는 `summary.json`에 있다.
Direct 수치는 공유 RAM·cache·준비 worker 코드와 흩어진 hook을 제외한 하한이다.
핵심 KVM direct 함수와 `direct.rs` 본문이 그대로인 것도 소스 비교로 확인한다.

## 없앤 것과 남은 제약

이 모드는 VMCS(Virtual Machine Control Structure), KVM·vCPU 객체, PML page와 dirty bitmap을 공유하지 않는다.
EPT 장부도 각 host의 로컬 slab에서 만든다.
Release 뒤에는 로컬 root 목록과 vCPU root 참조가 모두 비어 있다.
Adopt 전 guest 진입도 거절한다.
그래서 비소유 host의 notifier·memslot 변경·종료가 공유 표에 도달할 경로가 없다.
Direct의 15경로 진입 감사, 공유 refcount 지연, 공유 객체 parking과 재사용 규칙이 필요 없다.
Direct용 KVM keepalive도 띄우지 않는다.
기존 RAM mapper, 쓰기 log, mailbox와 준비된 worker는 유지한다.

EPT 영역은 append-only다. 버린 표도 같은 VM 생존 중 재사용하지 않는다.
16 MiB를 넘으면 닫힌 실패로 끝난다.
Adopt는 leaf 칸을 읽지 않지만, 도달 가능한 모든 표의 로컬 장부를 다시 만든다.
따라서 그 비용은 표 page 수에 비례한다.
현재 검증한 상태 packet은 1·2 vCPU 범위다.
실제 비일관 cache의 전기적 동작은 이 DRAM 실험으로 검증할 수 없다.
무효화 생략 시험은 정답지가 실제 발행 명령의 범위 누락을 검출하는 시험이다.

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
