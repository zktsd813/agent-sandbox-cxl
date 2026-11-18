# 비일관 CXL hand-off (2026-11-17)

0~4단계 결과를 기록한다. 0~2단계 본문은 18 실행 당시 기록이다.
3·4단계와 그 한계는 아래에 따로 덧붙였다.
Compute Express Link(CXL)의 실제 비일관 장치는 쓰지 않았다.
물리 host L0의 Non-Uniform Memory Access(NUMA) node 0만 썼다.
두 바깥 가상 머신(VM, Virtual Machine)을 L1 A/B라 부른다.
Guest VM은 L2다. 네트워크는 host에서 QEMU socket backend만 썼다.
혼합 입출력용 가상 Ethernet 장치는 L1 안에만 만들었다.
Kernel 영수증의 node=1/2는 논리 host A/B다. 물리 NUMA node 번호가 아니다.
RAM은 Random Access Memory다. DRAM은 Dynamic Random Access Memory다.
vCPU는 Virtual Central Processing Unit이다. SWE는 Software Engineering이다.

## 0단계

`a1b5848`의 [합친 tree 검증](../trajectory-faults-20261116/merge-smoke.md)을 통과했다.
Gin 1 vCPU와 2 vCPU의 독립 검증이 통과했다.
14와 달라진 fault 수는 mapper 수정만 바꾼 비교로 분리했다.

## 1단계

설계는 `eb1f724`의 [NONCOHERENT.md](../../../implementation/virtual_machine/NONCOHERENT.md)다.
구현은 `94142f1`, 검증기 보완은 `1403f67`, 반복 시험은 `a96afda`다.
혼합 I/O(Input/Output) fixture는 `ca1593c`와 후속 `fad1a55`, `ee67794`다.
기본 설정은 그대로 두고 PML(Page Modification Logging)과 두 dirty bitmap을 옵션으로 켰다.
L0의 pml/nested와 L1 양쪽 pml은 모두 Y였다.
쓰기 보호 dirty log로 대체하지 않았다.
KVM(Kernel-based Virtual Machine)의 두 배 크기 bitmap과 PML page는 DAX에 둔다.
DAX는 direct access다. detach는 모든 PML index가 초기값인지 검사한다.

| 시험 | vCPU | hand-off | 누락 page | 설명 없는 RAM 밖 변화 |
|---|---:|---:|---:|---:|
| Gin 전체 궤적, heavy | 1 | 3 | 0 | 0 |
| PHP-CS-Fixer 전체 궤적, heavy | 1 | 5 | 0 | 0 |
| MicroPython 전체 궤적, heavy | 1 | 6 | 0 | 0 |
| Gin 전체 궤적, heavy | 2 | 3 | 0 | 0 |
| 최소 checksum guest, 40회 왕복 | 1 | 80 | 0 | 0 |
| RAM/block/UDP/vsock 혼합, 30회 왕복 | 1 | 60 | 0 | 0 |

총 157회 이동과 163개 hash 경계에서 누락이 없었다.
SWE 궤적은 명령 원문, 반환값, test 요약, 최종 diff의 독립 검증도 통과했다.
혼합 guest는 매 회 16 MiB RAM checksum, block 직접 읽기 1 MiB,
UDP(User Datagram Protocol) 16 KiB와 vsock 64 KiB 수신 내용을 확인했다.
B는 NFS(Network File System) block backend를 읽었다.
혼합 시험은 연결을 닫은 뒤 새 VMM(Virtual Machine Monitor) wrapper를 붙인다.
기존 resident의 network/vsock 허용 범위를 넓혔다고 주장하지 않는다.
KVM VM/vCPU 객체 생성 수는 계속 각각 1개였다.

첫 목록은 promotion 전체 복사를 보수적으로 표시해 2 GiB였다.
그 뒤 SWE 목록은 630~154,273 page, 약 2.46~602.63 MiB였다.
각 이동의 목록 수, hash가 그대로인 목록 page 수, KVM 전용·Firecracker 전용·교집합 수는
[summary.json](summary.json)에 있다. 첫 기준 hash는 변화율 집계에서 제외한다.
정답지의 C 도구는 backing을 읽기 전용으로 열어 4 KiB별 XXH64를 구한다.
원시 RAM dump는 저장하지 않는다. 이전 hash 배열 하나만 다음 경계까지 유지한다.
실제 page 7을 바꾸고 목록에서 뺀 음성 시험도 했다.
정답지가 page 7 누락을 잡고 실패 ACK(Acknowledgement)를 냈다.

Raw pointer 감사에서는 queue 초기화 뒤 유지되는 used-ring pointer를 확인했다.
매 export에서 upstream의 mark_virtio_queue_memory_dirty로 범위를 다시 표시한다.
0 채우기와 promotion/demotion copy_nonoverlapping도 명시적으로 표시한다.
비동기 block 읽기의 표시는 완료 수집 때 이뤄진다. 따라서 device save/drain 뒤에 걷는다.

Gin heavy의 명령 시간은 각 3회다. Oracle을 끄고 quiet 배타 잠금에서 쟀다.
Off: 13.928923433, 13.820186153, 13.951188289초다.
On: 14.122438833, 13.955799422, 13.799224803초다.
중앙값은 각각 13.928923433초, 13.955799422초다.
차이는 +26.876 ms(+0.193%)다. 작은 표본이며 분포가 겹친다.
중첩 가상화의 PML 비용을 물리 host의 상수로 쓰지 않는다.
10-21 물리 host 부품 측정의 0.24~0.38 µs/page는 별도 비교값이다.

SWE는 L1 각각 4 GiB와 CXL backing 8 GiB를 썼다.
최소 guest는 CXL 2 GiB, 혼합 시험은 4 GiB였다.
모든 시작 전에 numactl --hardware와 예산 + 4 GiB를 검사했다.
Free가 18,850 MiB였던 혼합 시도는 시작 전에 거절됐다.
소유한 빌드 파일과 이미 정지한 진단용 disk 파일의 깨끗한 cache만 반납했다.
전역 drop_caches는 쓰지 않았다.
모든 실행 VM과 공유 backing을 정리했다.
Python 기존 시험 48개가 통과했고 증거용 8개는 skip이다.
Rust 기본 계약 19개가 통과했고 1개는 ignore다.

## 2단계 구현

Cache 유지보수 구현은 `fe7b71d`다. 증거 보존은 `ce65ca7`이다.
실행 소스는 `3acb0f8`이다. 기본값은 바꾸지 않았다.
`--noncoherent`가 장부와 cache 유지를 함께 켠다.

보내는 쪽은 정지와 I/O drain 뒤 두 장부를 합친다.
RAM mapping을 park하기 전에 해당 page의 모든 line에 CLWB(Cache Line Write Back)를 한다.
SFENCE(Store Fence) 뒤 K=8의 단일 작성자 CXL log를 발행한다.
Kernel detach 마지막에 공유 KVM bump arena 전체를 내려쓴다.
Mapper header와 packet의 CXL mailbox 사본까지 내려쓴 뒤 응답한다.
받는 쪽은 mailbox를 먼저 무효화해 packet과 비교한다.
Kernel attach는 객체를 읽기 전에 arena를 무효화한다.
그 뒤 log를 읽고 필요한 RAM page를 CLFLUSHOPT(Cache Line Flush Optimized)와
MFENCE(Memory Fence)로 무효화한다.
기존 INVEPT 요청을 유지하고 재개한다.
INVEPT는 Invalidate Translations Derived from Extended Page Tables다.

Log bitmap은 packet에 넣지 않는다. 마지막 epoch만 추가한다.
Direct JSON(JavaScript Object Notation)은 version 3, resident 내부 packet은 version 4다.
바깥 resident wire는 version 3, KVM token은 version 2다.
Consumed는 L1별 DRAM tmpfs의 cookie별 파일에 둔다.
새 wrapper도 이를 이어받는다. 기록 8개보다 뒤처지면 RAM 전체를 무효화한다.
Promotion 전체 복사는 내려쓰기와 전체 RAM record로 처리한다.
이후 장부를 다시 시작해 불필요한 두 번째 전체 목록을 줄였다.

시험용 heartbeat가 export 뒤 raw pointer로 epoch를 쓰던 경로도 고쳤다.
Cache 모드에서는 VMM이 정지 후 GuestMemory.write_obj로 쓰고 장부에 넣는다.
VMClock은 같은 추적 API를 쓴다. Balloon, pmem, vhost 등은 기존 direct 계약에서 거절한다.

L0 정답지는 log의 실제 record와 emitted bitmap을 읽는다.
저장한 changed GPA(Guest Physical Address) 집합과 여섯 bitmap을 독립 검증기로 다시 재생한다.
Kernel은 실제 발행한 line의 coverage bitmap을 host DRAM에 만든다.
전체 arena의 끝까지 덮었는지 검사하고 영수증을 반환한다.
이 영수증을 남기려고 이미 내려쓴 공유 객체를 다시 쓰지 않는다.
원시 page hash 배열과 RAM dump는 보존하지 않는다.
GPA와 file offset을 잇는 불변 mapper table만 증거로 저장한다.

첫 hash는 DRAM 부팅 뒤 promotion을 끝내고 첫 CXL 실행 전에 뜬다.
장부는 DRAM VM 생성 때부터 켜져 있다. 초기 CXL record는 전체 RAM이다.
매 export 후 import 전에는 전체 backing의 hash를 다시 뜬다.
Import 경계에서는 hash 대신 무효화 범위를 검사한다.
따라서 다음 export의 hash 비교에 수신 이후 장치 쓰기도 포함된다.

고의 누락도 실제 VM에서 시험했다.
Kernel line 100을 건너뛰면 detach가 EIO를 내고 공유 상태는 FAILED(4)가 됐다.
수신 page 5를 건너뛰면 VMM이 무효화 범위 오류로 abort했다.
수신 실패 때 kernel attach는 이미 끝나 owner=B였지만 guest 재개 ACK는 없었다.
두 시험 모두 완료된 hand-off는 0회다. VM과 backing을 정리했다.
이 경로는 자동 복구하지 않는다.
최종 Python은 52개 통과와 기존 선택 시험 8개 skip이다.
Rust 계약은 23개 통과와 1개 ignore다. 두 base patch의 reverse check도 통과했다.
Prefault overlay는 양쪽 모두 fuzz 0으로 적용 검사를 통과했다.

## 2단계 결과

SWE 17회, 최소 guest 80회, 혼합 I/O 60회, 쓰기량별 880회다.
총 1,037 hand-off를 검증했다. Hash 경계는 1,047개다.
수신까지 포함한 범위 검사 경계는 2,084개다.
누락 page와 설명 없는 RAM 밖 변화는 모두 0이다.
저장한 bitmap과 변경 GPA의 독립 replay도 전부 통과했다.
SWE 독립 궤적 검증과 guest checksum·I/O 검증도 통과했다.
혼합 시험의 epoch 13에서 consumed 기록을 잃게 했다.
K=8 초과를 감지하고 RAM 전체를 무효화해 통과했다.

초기 promotion record는 RAM 전체다. 이를 뺀 SWE 목록은
616~154,310 page, 2.41~602.77 MiB였다.
KVM·Firecracker 전용 page와 교집합, 넘친 비율은 이동별로 summary.json에 있다.
두 장부 모두 단독으로 잡는 page가 있어 한쪽을 생략할 수 없다.
객체 arena의 실제 사용 범위는 본시험 전체에서 482,720~2,103,072 byte였다.
초기 cold 수신의 RAM 전체 무효화는 warmup 밖 원시 표본에도 그대로 보존했다.

아래는 정답지를 켠 본시험의 중앙값이다. 단위는 ms다.
WB는 Write Back이다.
각 쓰기량에서 방향별 warmup 10회 뒤 100회씩, 합계 200회를 집계했다.
0 MiB에서도 heartbeat·clock·kernel 등이 page를 쓴다.

| guest 쓰기 MiB | 목록 page / MiB | 장부 | RAM WB | log 발행 | 객체 WB | log 읽기 | RAM 무효화 | 객체 무효화 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 74 / 0.29 | 0.887 | 0.054 | 0.039 | 0.031 | 0.149 | 0.129 | 0.032 |
| 16 | 4,177 / 16.32 | 0.954 | 0.531 | 0.041 | 0.037 | 0.151 | 3.973 | 0.037 |
| 64 | 16,490 / 64.41 | 1.109 | 1.842 | 0.039 | 0.038 | 0.156 | 15.284 | 0.037 |
| 256 | 65,744 / 256.81 | 1.742 | 7.217 | 0.040 | 0.050 | 0.157 | 59.847 | 0.053 |

제어 영역 WB/무효화, 95백분위수(p95), 방향별 값과 line 수는 JSON에 있다.
정답지 gate를 포함한 ACK 중앙값은 쓰기량 순서로 885.428/903.656/906.623/983.683 ms다.
이 값은 관측기가 붙은 시험 시간이다.

별도 oracle-off 대조군의 pause→guest ACK 중앙값은 다음과 같다. 단위는 ms다.
기준선은 dirty 장부와 cache 유지를 끈다. 활성 군은 둘 다 켠다.
PML module 옵션은 양쪽 모두 on이다.

| 쓰기 MiB | 기준선 | 활성 | 중앙값 차이 | 활성 p95 |
|---:|---:|---:|---:|---:|
| 0 | 2.216 | 3.555 | 1.339 | 3.861 |
| 16 | 2.409 | 7.701 | 5.292 | 8.006 |
| 64 | 2.755 | 20.660 | 17.905 | 21.339 |
| 256 | 4.380 | 73.349 | 68.969 | 74.272 |

이 보조 비교는 8개 lab, 1,760 hand-off다. Page 누락 검증 횟수에는 더하지 않았다.

## 재현

실행 전 node 0 free, lab/quiet 잠금, 소유권과 disk 예산 규칙을 적용해야 한다.
아래 runner는 시작 전 메모리 guard와 종료 정리를 수행한다.
실행 입력의 SHA-256(Secure Hash Algorithm, 256 bit)은
각 원시 report의 config.prepared.inputs와 artifacts에 있다.
교체 shared-probe binary는 artifacts의 해시와 일치했다.
기반 fixture의 옛 probe_source가 남은 메타데이터 문제는 `3238701`에서 고쳤다.
원시 manifest는 보존했다. 실행 때 기록한 소스는 `3acb0f8`과 일치한다.
정확한 binary·실행 소스·옛 필드의 대조는 summary.json의 probe_provenance에 있다.

```sh
V=implementation/virtual_machine
numactl --cpunodebind=0 --membind=0 python3 "$V/multi_vcpu.py" \
  --vcpus 1 --resident-bench dirty --dirty-mib 64 \
  --iterations 100 --warmup 10 --measure --pml on \
  --noncoherent --dirty-oracle --cxl-mib 4096 \
  --assets "$V/.local/run18/assets-nc" \
  --qemu "$V/.local/qemu-cxl/build/qemu-system-x86_64" \
  --page-hash "$V/.local/run18/page-hash" --output "$V/.local/new-nc-64"
numactl --cpunodebind=0 --membind=0 python3 \
  docs/results/noncoherent-handoff-20261117/analyze.py "$V/.local/run18"
```

## CXL 환산 기준과 한계

제공된 [wbcost.jsonl](dcd-flush-cost/wbcost.jsonl)을 읽기만 했다.
이 실행은 node 2나 실제 DCD 장치를 사용하지 않았다.
Dirty cached line의 내려쓰기는 약 16 ns/line이다.
Absent line flush와 수신 무효화는 약 6.25 ns/line이다.
Cached clean line 무효화는 약 2 ns/line이다.
2 GiB는 33,554,432 line이다. 전체 dirty 내려쓰기는 약 536.87 ms,
전체 absent 무효화는 약 209.72 ms로 환산된다.
아래는 정상 상태 RAM line만의 환산이다. 단위는 ms다.
객체·log·제어 line을 더한 환산도 JSON에 있다.

| 쓰기 MiB | WB / 무효화 line 수 중앙값 | dirty WB 16 ns | absent 무효화 6.25 ns | 합계 | 매번 전체 RAM 처리 |
|---:|---:|---:|---:|---:|---:|
| 0 | 4,736 | 0.076 | 0.030 | 0.105 | 746.586 |
| 16 | 267,328 | 4.277 | 1.671 | 5.948 | 746.586 |
| 64 | 1,055,360 | 16.886 | 6.596 | 23.482 | 746.586 |
| 256 | 4,207,616 | 67.322 | 26.298 | 93.619 | 746.586 |

Absent WB 6.25 ns와 clean cached 무효화 2 ns의 다른 cache 상태도 JSON에 계산했다.

Lab의 backing은 DRAM이다. 환산은 실측 hand-off 시간이 아니다.
실제 stale 재현도 하지 않았다.
원시는 implementation/virtual_machine/.local/run18에 있다.

수신 RAM 단계에는 park 뒤 사용자 page table을 다시 만드는 fault 비용이 들어간다.
중첩 가상화, bitmap 검사, timer 기록 비용도 남는다.
장부 수집에는 KVM dirty log 수집뿐 아니라 Firecracker bitmap의 전체 page 검사도 들어간다.
이 시간을 순수 PML의 page당 비용으로 해석하지 않는다.
정답지를 켠 본시험의 pause→guest ACK에는 전체 backing hash 대기도 들어간다.
명시적인 gate 시간을 빼도 hash가 만든 cache 변화는 남는다.
따라서 별도의 oracle-off 기준선/활성 비교도 같은 guest payload로 수행했다.
보조 대조군은 방향별 warmup 10회와 측정 100회다.
기준선 뒤 활성 모드를 실행한 고정 순서다. 무작위 반복 실험은 아니다.
보조 대조군의 checksum과 ACK는 검사하지만 멈춘 page hash는 뜨지 않는다.
정확성 판단은 정답지를 켠 본시험에 둔다.
따라서 line 단가 환산은 cache 명령 부분만 추정한다.
실측 단계 전체나 실제 서버 사이 hand-off 시간을 대신하지 않는다.
Dirty WB/absent invalidation을 상한 쪽 가정으로,
absent WB/clean cached invalidation을 다른 cache 상태의 비교로 함께 둔다.
이는 모든 CXL 접근의 엄밀한 상·하한이라는 뜻은 아니다.

## 18 종료 시점의 남은 위험과 다음 brief

이 lab은 coherent backing이다. 내려쓰기를 빼도 값이 우연히 맞을 수 있다.
이번 정확성 근거는 page 목록의 완전성과 실제 발행한 범위의 덮임이다.
실제 비일관 장치에서 stale 값을 재현한 증거는 아니다.
소유하지 않은 host kernel의 객체 접근 경로 전체 감사는 다음 범위다.
공유 bump arena는 이전 할당까지 포함하며 VM별로 줄이지 않았다.
Arena 재사용 규칙도 아직 없다.
Network/vsock은 연결을 닫은 혼합 direct fixture로 검증했다.
일반 resident의 활성 socket 이전을 지원한다고 주장하지 않는다.
실제 CXL 시간은 cache 상태와 mapping 상주 상태에 따라 달라진다.

다음 brief에는 VM별 객체 arena, 비소유 kernel 접근 감사, 안전한 영역 재사용이 필요하다.
이어 LLM 대기 중 미리 정리와 수신 측 미리 무효화를 붙일 수 있다.
실제 비일관 multi-host 장치 시험은 별도 승인 범위에서 해야 한다.

## 18의 정리와 저장 공간

이 실행과 연결된 lab config 43개에서 살아 있는 소유 VM이 없음을 확인했다.
소유한 공유 메모리 backing과 임시 hash 배열도 남지 않았다.
최종 .local은 79 GiB, run18은 6.4 GiB다. / 여유는 109 GiB였다.
시작 때 .local 54 GiB, / 여유 134 GiB였다.
실행은 약 2시간 30분이었다. 0~2단계의 요청된 본시험을 완료했다.
Local commit만 만들었고 push하지 않았다.

## 3단계: LLM 대기 중 미리 정리

LLM(Large Language Model, 대규모 언어 모델) 응답을 기다리는 구간에 live clean을 넣었다.
VM은 멈추지 않는다. 장치 emulation과 같은 VMM thread에서 실행한다.
KVM_GET_DIRTY_LOG → Firecracker bitmap 수집 → RAM CLWB → SFENCE → record 발행 순서다.
정지 clean 시험만 vCPU를 멈추고 입출력을 비운다.
경합별 논리는 [설계 메모](../../../implementation/virtual_machine/NONCOHERENT.md)에 있다.

받는 쪽은 새 record 알림을 받으면 미리 무효화하고 host DRAM의 consumed를 올린다.
첫 attach 전의 cold observer도 처리한다. Hand-off 때는 마지막 record만 남는다.
알림 처리가 끝나기 전에 다음 record를 발행하지 않는 단일 작성자 방식이다.
독립적인 주기 reader와 ring overwrite의 동시 실행을 추가한 것은 아니다.

RAM 유지보수는 mapper의 kernel direct map에서 실행한다.
HVA(Host Virtual Address, host 가상 주소) page table을 다시 만드는 fault가 없다.
수신만 바꾼 중간 구현에서는 같은 fault가 다음 송신 WB로 옮겨 갔다.
그래서 송신 WB(Writeback, 내려쓰기)와 수신 무효화를 함께 옮겼다.
Mapper는 불변 GPA(Guest Physical Address, guest 물리 주소) 표의 로컬 사본만 읽는다.
비소유 observer는 공유 lease·epoch·stat을 쓰지 않는다.

### 256 MiB 반복 대조

방향별 warmup 10회, 측정 100회다. 각 arm은 총 220 hand-off다.
두 arm 모두 kernel direct map을 쓴다. 아래 값은 측정 200회의 중앙값이다.

| 항목 | 정리 없음 | 미리 정리 |
|---|---:|---:|
| Hand-off 목록 | 65,742 page / 256.805 MiB | 59 page / 0.230 MiB |
| 장부 수집 | 1.734 ms | 0.867 ms |
| 송신 RAM WB | 7.565 ms | 0.071 ms |
| Record 발행 | 0.041 ms | 0.035 ms |
| 수신 log 읽기 | 0.156 ms | 0.154 ms |
| 수신 RAM 무효화 | 7.572 ms | 0.080 ms |
| 객체 WB / 무효화 | 0.050 / 0.053 ms | 0.053 / 0.053 ms |

수신 사전 무효화가 hand-off에서 덜어 낸 RAM 단계는 중앙값 차이 7.492 ms다.
18의 HVA 경로 256 MiB 무효화 약 59.8 ms와 비교하면,
정리 없이도 fault 없는 경로는 약 7.57 ms다. 서로 다른 실행의 비교다.
객체·log·제어 비용과 최종 목록 수집 비용은 남는다.

정답지 없는 별도 대조군도 각각 220회다. 측정 구간의 정지→guest ACK 중앙값은
20.812 ms → 5.078 ms다. p95는 21.459 ms → 5.456 ms다.
최초 2 GiB cold observer 무효화는 약 620 ms였다. 최초 준비 비용으로 따로 센다.
이는 한 coherent host의 중첩 lab 결과다. 실제 비일관 CXL hand-off 시간은 아니다.
고정 순서의 작은 arm 비교이며 무작위 반복 실험은 아니다.

정리 1회의 목록 중앙값은 256.801 MiB다.
장부 수집 1.745 ms + WB 7.240 ms + 발행 0.041 ms다.
세 비용 합의 중앙값은 9.031 ms다. 수신 사전 처리와 알림 왕복은 별도다.
이 220회 전체에는 warmup과 최초 정리가 들어 있다.
수집 비용 p95는 107.220 ms, 최대 1,733.801 ms다.
따라서 live clean의 지연 상한이나 항상 LLM 대기 안에 끝난다는 보장은 하지 않는다.
정답지 없는 대조군도 정리 합 중앙값 8.997 ms, p95 317.204 ms, 최대 1,471.179 ms였다.
대기 W는 tool 결과 수신부터 센다. 정리가 쓴 시간은 W에서 빼고 남은 시간만 잔다.
실제 대기, 예정 대기, 초과 시간도 저장했다.

### CXL 단가 환산

18과 같은 64 byte line 단가를 유지했다.
Dirty WB 16 ns, absent WB/무효화 6.25 ns, clean cached 무효화 2 ns다.
마지막 수준 cache인 LLC(Last Level Cache)는 약 60 MB다.
그보다 큰 목록에는 [기존 wbcost.jsonl](dcd-flush-cost/wbcost.jsonl)의 256 MiB·1 GiB dirty 행도 적용했다.
이 행의 실제 명령은 CLFLUSHOPT다. 송신 WB의 대체 가정으로 약 6.2~6.7 ns/line을 썼다.
큰 목록의 CLWB를 이번에 직접 측정한 값은 아니다. 기존 파일만 읽었고 장치는 접근하지 않았다.
256 MiB 대조군의 RAM WB+무효화 환산은 다음과 같다.

| 가정 | 정리 없음 | 미리 정리 뒤 잔여 |
|---|---:|---:|
| Dirty WB 16 ns + absent 무효화 6.25 ns | 93.617 ms | 0.084 ms |
| 큰 목록 dirty 단가 대체 6.2~6.7 ns + absent 무효화 6.25 ns | 52.383~54.487 ms | 작은 잔여에는 위 행 사용 |

객체·log·제어 line까지 18의 첫 가정으로 합치면 94.160 ms → 0.628 ms다.
이는 cache 명령 부분만의 환산이다. 장부, 주소 변환, 정지·재개, 통신 시간을 포함하지 않는다.
대기 구간으로 옮긴 WB와 무효화 비용이 사라지는 것은 아니다.

### SWE와 정답지

세 fixture의 전체 궤적에 무거운 명령 정책을 적용했다.
W=0, 1초, 원시 궤적의 tool 결과→다음 assistant 응답 간격(최대 10초)을 비교했다.
같은 batch의 다음 명령은 recorded W=0이다. 명령이 끝날 때마다 정리한다.
최종 표와 단계별 분포는 아래 표와 summary.json의 stage3에 있다.

단위는 MiB다. 각 cell은 목록의 중앙값(최대)이다. 각 조건은 fixture당 1회다.

| Fixture | 조건별 hand-off | 정리 없음 | W=0 | W=1초 | 기록 간격 |
|---|---:|---:|---:|---:|---:|
| Gin | 3 | 198.047 (561.277) | 0.012 (0.180) | 0.266 (0.305) | 0.324 (0.363) |
| PHP-CS-Fixer | 5 | 33.180 (55.199) | 0.141 (0.371) | 0.266 (0.324) | 0.324 (0.430) |
| MicroPython | 6 | 28.695 (222.855) | 0.012 (0.156) | 0.275 (0.508) | 0.277 (0.512) |

다음은 정리 없음 → W=1초의 RAM 단계 중앙값이다. 모든 W의 단계별 분포는 JSON에 있다.

| Fixture | WB ms | 무효화 ms | 정리 1회 MiB 중앙값(최대) | 수집+WB+발행 ms 중앙값(최대) |
|---|---:|---:|---:|---:|
| Gin | 5.977 → 0.082 | 5.959 → 0.081 | 3.012 (563.043) | 1.194 (19.292) |
| PHP-CS-Fixer | 1.235 → 0.081 | 1.056 → 0.083 | 2.766 (45.812) | 1.137 (2.651) |
| MicroPython | 0.973 → 0.091 | 0.896 → 0.083 | 2.531 (222.391) | 1.116 (7.950) |

수신 사전 처리와 알림 왕복은 정리 합에서 제외했다. Bytes와 각 record의 시간은 JSON에 남겼다.
W=0의 실제 간격은 정리·알림 때문에 약 11~62 ms였다. W=1은 약 1초였다.
기록 간격은 약 1.068~10초였다. 예정값과 실제값, 초과 시간은 각 report에 있다.

| Fixture | 18 단가의 RAM 환산 ms, 정리 없음 → W=1 | 큰 목록 대체 가정 ms, 정리 없음 |
|---|---:|---:|
| Gin | 72.197 → 0.097 | 40.398~42.020 |
| PHP-CS-Fixer | 12.095 → 0.097 | 중앙 목록은 LLC보다 작음 |
| MicroPython | 10.461 → 0.100 | 중앙 목록은 LLC보다 작음 |

Gin 2 vCPU, W=1초도 3회 이동이 통과했다. 잔여 목록은 중앙값 0.445 MiB, 최대 0.547 MiB다.
첫 준비의 전체 RAM 처리와 대기로 옮긴 비용은 위 hand-off 수치에 포함하지 않는다.

정지 clean은 각 경계의 바뀐 page가 이번 목록 안에 있는지 hash로 검사한다.
Live clean은 멈추지 않고, 다음 hand-off에서 지난 경계 이후 목록의 합집합과 비교한다.
별도의 경합 시험은 guest가 고정 16 page를 계속 쓰는 동안 clean을 600회 실행했다.
마지막 clean 뒤에도 그 page들의 값이 전진함을 확인했다.
두 hand-off의 마지막 목록에 16 page가 모두 있었다.
원시 bitmap을 다시 읽는 독립 검증도 실행했다. 고의로 page를 빼는 단위 시험은 실패를 검출했다.

## 4단계: VM별 객체 영역과 재사용

VM 생성 때 전역 bump에서 전용 16 MiB를 한 번 예약한다.
이후 VM·vCPU·VMCS(Virtual Machine Control Structure)·EPT(Extended Page Tables)·dirty bitmap·PML·kvm_run과
APIC(Advanced Programmable Interrupt Controller) map은
그 VM의 bump에서 할당한다. Bump lock과 owner 판정은 host DRAM에 있다.
Hand-off의 객체 유지 범위는 descriptor를 포함한 [vm_base, vm_bump)다.
초과하면 NULL과 FAILED로 끝난다. DRAM fallback이나 재개를 하지 않는다.
실제 서버의 영역 배정자는 네트워크 중앙 관리자여야 한다.
Lab의 생성용 전역 cmpxchg를 비일관 host 사이 allocator로 쓰지 않는다.

| SWE 궤적 | vCPU | 끝 사용량 byte | MiB |
|---|---:|---:|---:|
| Gin | 1 | 2,018,400 | 1.925 |
| PHP-CS-Fixer | 1 | 773,472 | 0.738 |
| MicroPython | 1 | 1,088,864 | 1.038 |
| Gin | 2 | 2,258,720 | 2.154 |

16 MiB는 기록한 최대 사용량의 두 배보다 크다.
현재 lab의 host당 active KVM graph 하나라는 계약은 유지한다.
동시 여러 VM의 전체 수명 관리까지 구현한 것은 아니다.

VM1의 마지막 detach 뒤 양쪽 Firecracker와 제어기를 종료했다.
PID 재사용과 종료 중 reaping도 검사한다.
양쪽 host가 전체 RAM과 예약 객체 영역 16 MiB를 무효화하고 ACK한다.
한쪽 ACK만 넣은 재사용 arm은 거절됐다.
새 RAM은 non-temporal store로 0을 채우고 SFENCE한다.
전체 524,288 page의 0 hash를 확인했다.
VM2는 같은 RAM frame과 같은 객체 영역에서 B부터 시작했다.
VM1은 vCPU 1개, VM2는 2개다. 새 cookie와 allocation_id도 확인했다.
VM1 4회, VM2 6회의 hand-off와 guest·정답지 검증이 통과했다.

서로 다른 vCPU 수의 첫 재사용 시험에서는 guest invalid opcode가 났다.
같은 주소의 native EPT backlink metadata에 allocation_id 구분을 추가했다.
또한 이전 EPT root의 L0 shadow를 영역 반환 전에 버리도록 수정했다.
새 root의 주소만 건드리는 기존 방법은 다른 배치의 이전 root를 놓칠 수 있었다.
이 수정은 dax_nested_root_sync가 켜진 중첩 lab의 회수 단계에만 실행한다.
실제 비일관 하드웨어의 비소유 실행 중 객체를 쓰는 경로로 쓰지 않는다.

### 비소유 kernel 감사와 닫힌 실패

경로별 guard와 counter는 설계 메모의 표에 있다.
MMU(Memory Management Unit) notifier, refcount, file release, stats/debugfs,
APIC timer, irqfd의 wakeup·비동기 work,
preempt notifier, 전원 관리 notifier, mapper lease/epoch 경로를 검사했다.
Guard는 공유 kvm/vCPU 필드를 읽기 전에 host DRAM의 owner 표를 본다.
Detach 때 notifier·timer·irqfd를 해제하고 drain한다.
Timer/work queue 내부는 callback guard 전에 공유 list를 만질 수 있으므로 동기 취소·drain이 필수다.
kvmclock의 두 delayed work도 동기 취소한다. PIT(Programmable Interval Timer)도 기존 quiesce 계약을 따른다.
DAX MMU/APIC map의 RCU(Read-Copy Update) callback은 공유 head를 enqueue하지 않는다.
비소유 refcount 반납과 file release는 pinned graph의 native 참조를 남긴다.
이 참조는 L1 종료 때까지 남는다. 생산용 kernel의 일반 파괴·회수 증명은 아니다.

Parked VMM에 VM/vCPU fd dup·stats ioctl·vcpu mmap과 익명 mmap/munmap 100회를 시켰다.
비소유 ioctl과 vCPU mmap counter가 증가했고 접근은 거절됐다.
누적 counter snapshot의 최댓값은 다음과 같다. 서로 다른 host·시점의 값을 합한 수가 아니다.

| 경로 | 최댓값 | 처리 |
|---|---:|---|
| VM/vCPU ioctl, 9 | 50 | EIO로 거절 |
| vCPU mmap/fault, 10 | 40 | 접근 거절 |
| VM/vCPU file release, 4 | 2 | 공유 객체를 읽지 않고 참조 반납 지연 |
| arena 관리, 12 | 1 | 정지·fd fence·전체 무효화 뒤 회수만 허용 |
| sysfs snapshot, 13 | 1 | host DRAM 사본만 읽음 |
| mapper release | 9 | 로컬 파일 자원만 반납. 공유 lease를 쓰지 않음 |
| mapper 비소유 ioctl | 0 | Observer는 별도 불변 표 사본 사용 |

비소유 비동기 callback·refcount 경로는 이 시험에서 0이었다.
0이라는 관측이 모든 외부 workload에서 진입하지 않는다는 증명은 아니다.
객체 direct map에 추측 실행으로 들어온 line은 attach의 객체 무효화가 덮는다.
RAM의 이전 line은 새 record 처리 또는 backlog의 전체 무효화가 덮는다.

오류 주입 6종은 모두 예상 오류로 abort했다. 완료 hand-off는 모두 0이다.
송신 KVM WB line 누락, 송신 RAM WB 오류, 수신 RAM 무효화 오류,
packet의 미래 epoch, 손상 record, 64 KiB 객체 영역 초과를 각각 넣었다.
수신 오류 3종에는 Resume가 없었다.
밀린 기록이 K=8을 넘으면 전체 RAM을 처리한다. 전체 처리도 실패하면 abort한다.
부분 ACK 거절은 영역 재사용 시험에서도 확인했다.

### 검증 범위와 원시 자료

최종 선택 집합은 601 hand-off, 1,172 clean, 634 hash 경계다.
누락 page와 설명 없는 RAM 밖 변화는 모두 0이다.
SWE 59회, 256 MiB 두 arm 440회, 정지 clean 10회, 경합 2회,
SMP 20회, 혼합 I/O 60회, 재사용 10회다.
별도 정답지 없는 시간 대조는 440회다. 최신 kernel의 이전 2 MiB probe 재시험 20회도 따로 통과했다.
Python 54개와 Rust 계약 23개가 통과했다. 선택 시험 8개 skip, Rust 1개 ignore다.
기본 patch 역적용과 prefault overlay dry-run은 fuzz=0으로 통과했다.

성능 arm은 45c28cb 계열의 kernel fixture다. 현재 코드와 kernel cache 경로가 같다.
최종 기능·재사용·SWE arm은 48ab93c의 kernel fixture다.
그 뒤 nonresident 반환 제어기의 필드 누락 수정은 혼합 입출력으로 따로 검사한다.
소스 revision, 모듈·실행 파일 hash와 fixture 계보는 각 report에 있다.
현재 kernel 소스 재빌드의 두 모듈 hash는 최종 fixture와 일치한다.

원시는 implementation/virtual_machine/.local/run19에 있다.
분석은 `analyze.py implementation/virtual_machine/.local/run19 --stage19`로 재생한다.
summary.json은 최종 선택 집합과 중간 진단 집합을 구분한다.
실행 중 소스가 달라진 SWE 묶음, 수정 전 재사용·probe 실패, 예산 거절은 최종 집합에서 제외했다.
18의 기존 0~2단계 원시 결과와 요약은 보존했다.
실제 비일관 CXL과 다른 서버의 stale 재현은 여전히 별도 승인 범위다.
Node 2, host DAX와 dcdctl은 사용하지 않았다.

### 19의 정리

이 실행의 lab config 68개에서 살아 있는 소유 VM이 없었다.
공유 backing, control socket directory, stopped storage copy와 임시 hash 배열도 남지 않았다.
최종 .local은 89 GiB, run19는 6.5 GiB다. / 여유는 약 98.2 GiB다.
약 1시간 55분 동안 수행했다. commit은 로컬에만 남겼다. push하지 않았다.

## 20: 정리 꼬리 원인, 실제로 쓴 양과 내려쓴 양, 최적화 (중간에 멈춤)

2026-11-18 06:04 UTC에 Codex 20으로 시작했다. 10:09 UTC에 사용자 요청으로 멈췄다.
멈춘 시점의 코드는 `75e8243`이다. 이 장의 숫자는 Claude가 `analyze20.py`로 최종 원시 자료를 다시 읽어 확인했다.
Codex가 쓰려던 보고서와 SMP 반복 몇 회는 하지 못했다. 진행 기록은 `.codex-runs/20-progress.md`다.

### 정리 꼬리의 원인

19에서 VM을 멈추지 않는 정리(live clean)가 가끔 50 ms를 넘었다. 256 MiB 시험에서 220회 중 13회였고 최대는 1.7초였다.

| 확인한 것 | 결과 |
|---|---|
| 재현(정답지 on/off/on/off, 각 220회) | 50 ms 초과 15/8/18/6회, 최대 0.85/3.23/6.40/0.98초 |
| 최종 코드(`fea665a`)로 220회 | 50 ms 초과 8회, 최대 1.06초. 최적화 뒤에도 남는다 |
| 꼬리 47회가 생긴 곳 | 모두 L1 KVM이 쓰기 흔적(D bit)을 지우는 단계 |
| 그 밖의 단계 | 잠금 획득, TLB 비우기, bitmap 복사가 모두 0.14 ms 이하 |
| scheduling·메모리 회수 | L1 VMM과 L0 vCPU 대기가 최대 0.74 ms. 회수, compaction, swap, steal은 모두 0 |
| VM을 멈춘 정리(같은 lab) | 220회 최대 15 ms, 50 ms 초과 0 |
| 일반 KVM 쓰기 기록 시험(L0·L1, 멈춤·실행 중·256 MiB 전부 쓰기) | 각 220회 최대 4.7 ms, 50 ms 초과 0 |
| L0 perf, 꼬리 구간 | 표본의 42~44%가 L0 KVM `mmu_lock` 읽기 대기(`queued_read_lock_slowpath`). 정상 구간은 0~3.5% |
| 대기와 짝 | 꼬리 47회 모두, 읽기 대기 표본이 같은 QEMU의 다른 vCPU에서 2 ms 안의 `mmu_sync_children` 표본과 짝을 이룬다 |

원인은 L0 쪽이다.
- L1이 D bit를 지우면 L0는 자기가 대신 들고 있는 L2용 page table(shadow EPT)을 맞춘다(`kvm_mmu_sync_roots` → `mmu_sync_children`). 이 일은 L0의 `mmu_lock`을 쓰기로 잡는다.
- 같은 때 다른 vCPU는 guest가 256 MiB를 쓰며 EPT fault를 낸다. 이 fault는 같은 lock을 읽기로 잡아야 해서 기다린다.
- 그래서 nested lab에서만 생기는 비용으로 본다. 실제 하드웨어에서는 L1이 없어 shadow EPT도 없다.

다만 남은 질문이 있다. 일반 KVM 시험은 L1에서 실행 중에 돌려도 꼬리가 없었다. 그래서 lab의 어떤 조건이 필요한지 아직 모른다. 후보는 두 L1이 같은 EPT page를 함께 매핑하는 hand-off 구성이다. hand-off 없이 한 host에서만 쓰기·정리를 반복하는 대조를 계획했지만 하지 못했다.

### 실제로 쓴 양 대 내려쓴 양

L0 정답지에 64 B line 단위 hash를 더했다. kernel의 내려쓰기 명령 수는 명령마다 실제로 센다. 모든 경계에서 RAM 내려쓰기 명령 수가 목록 page × 64와 같았다.

guest가 page마다 정해진 수의 line을 쓰는 시험이다(16 MiB·256 MiB, VM을 멈춘 정리, 측정 구간만).

| 쓰기 | 목록 page ÷ 바뀐 page | 내려쓴 line ÷ 바뀐 line |
|---|---:|---:|
| page마다 1 line | 1.00~1.02 | 63~64 |
| page마다 8 line | 1.00~1.02 | 8.0~8.3 |
| page마다 64 line | 1.00~1.02 | 1.0 |
| 읽기만 | 목록에 없음 | 목록에 없음 |
| 같은 값 다시 쓰기 | 모두 목록에 있음, 값 변화 0 | — |

- 목록은 page 단위라, page 안에서 일부 line만 쓰면 나머지 line도 내려쓴다.
- 같은 값을 다시 쓰면 CPU의 쓰기 표시가 켜져서 목록에 들어간다. 값 hash로는 바뀐 것이 없다. 하드웨어가 구별하지 못하는 몫이다.
- 읽기만 한 page는 목록에 없다. 단, hand-off 직후 받는 쪽 KVM이 새로 만드는 쓰기 가능 mapping은 읽기만 한 page도 한 번 목록에 넣는다. 16 MiB 읽기 시험에서 첫 정리에 4,096 page가 들어갔고 그 뒤에는 0이었다(`spte-read-cause`).
- 목록에 있지만 값이 안 바뀐 page는 거의 모두 KVM 쪽 표시다. 장치 쪽(Firecracker) 표시는 매 수집마다 virtio queue 영역 3 page가 표시되는 것뿐이다.

실제 부하에서 hand-off 사이 구간마다 잰 값이다(측정 구간만).

| 부하 | 목록 page ÷ 바뀐 page | 목록 line ÷ 바뀐 line | 미리 정리의 반복 포함 내려쓴 line ÷ 바뀐 line |
|---|---:|---:|---:|
| 256 MiB 전부 쓰기 | 1.00 | 1.00 | 1.00 |
| 혼합 입출력 | 1.09 | 1.16 | 1.16 |
| SWE Gin | 1.04 | 1.15 | 1.22 |
| SWE MicroPython | 1.08 | 1.35 | 1.66 |
| SWE PHP-CS-Fixer | 1.11 | 1.33 | 1.88 |

- page 단위 값은 19 원시에서 Claude가 따로 계산한 값(1.04/1.08/1.11)과 같다.
- SWE에서 목록 page 안의 line은 74~87%가 실제로 바뀌었다.
- 미리 정리하면 같은 page를 다시 써서 여러 번 내려쓴다. 이 반복분은 LLM 대기 중에 처리된다.
- hand-off 때 VM 객체 영역은 아직 쓴 양보다 훨씬 많이 처리한다. 내려쓴 line ÷ 바뀐 line은 Gin 3.1, MicroPython 9.8, PHP 13이다. 영역 좁히기는 아래에서 보류했다.

### 최적화

256 MiB 시험이다. 미리 정리를 켰고, 각 arm 220회 중 측정 200회다. 시간은 lab(DRAM) 값이다.

| 바꾼 것 | 전 | 후 | 판정 |
|---|---|---|---|
| Firecracker 장부를 page마다 보지 않고 64-bit word째 원자적으로 떼어 읽기 | hand-off 수집 0.873 ms, 비일관 처리 전체 1.29 ms, ACK 5.25~5.49 ms | 0.134 ms, 0.54 ms, 4.20~4.91 ms | 채택 |
| 정리의 내려쓰기를 장치 thread 밖의 보조 thread로 옮기기 | 정상 수집 구간의 guest disk 최악 지연 중앙값 9.0 ms | 5.8 ms | 선택 옵션(기본 꺼짐). 꼬리 구간은 그대로 |
| KVM manual protect(읽기와 다시 보호하기를 나누기) | 쓰기 기록 읽기 1.0 ms | 0.115 ms. 다시 보호하기가 약 1초 | 버림. 코드 제거 |
| 객체 영역 좁히기 | — | — | 보류. 받는 쪽이 미리 끌어온 mapping과 소프트웨어 쓰기를 빠짐없이 잡는다는 근거가 부족 |
| 처음 보는 host의 전체 무효화 줄이기 | — | — | 보류. 그대로 둔다 |

- 비일관 처리 전체(보내는 쪽 수집 0.13, 내려쓰기 0.07, 발행 0.03, 객체 0.05 ms + 받는 쪽 log 읽기 0.12, 무효화 0.06, 객체 0.05 ms)는 0.52 ms다.
- 비일관 처리를 끈 같은 시험의 ACK는 4.82/4.89 ms였고 켠 쪽은 4.31/4.28 ms였다. 켠 쪽이 짧은 이유는 아직 가리지 못했다. 그래서 차이로 비용을 말하지 않고 위 구간 합을 쓴다.
- 보조 thread는 꼬리를 줄이지 못했다. 꼬리는 L0에서 vCPU가 lock을 기다리는 것이라 VMM thread와 무관하다.

### 정확성과 시험

- 누락 page는 0이다. 15개 회계 시험에서 hand-off 102회, 정리 173회, hash 경계 290회였다. 그 밖의 정답지 시험 17개도 모두 0이다.
- Python 65개(8 skip)가 `75e8243`에서 통과한다(Claude가 다시 돌림). Rust 계약 시험은 마지막 Firecracker 변경 뒤인 09:04에 통과했다.
- 측정 전에 거절된 실행이 16개 있다. node 0 여유 메모리 부족과 실행 중 소스 변경이 원인이고, 모두 최종 집합에서 뺐다.
- SMP legacy 경로가 한 번 실패했다("CPU 0 ping did not continue"). 누락 page는 0이었다. 다시 돌린 4회는 통과했다. 원인은 모른다.

### 원시 자료와 남은 일

- 원시는 `implementation/virtual_machine/.local/run20`(8.2 GiB, Git 제외)에 있다.
- 분석은 `analyze20.py implementation/virtual_machine/.local/run20`으로 재생한다. 결과는 같은 디렉터리의 `stage20-analysis.json`(9.8 MB)이다. 크기 때문에 `summary.json`에는 아직 넣지 않았다.
- 남은 일:
  - hand-off 없이 한 host에서만 쓰기·정리를 반복해 꼬리 조건 가리기
  - 비일관 처리를 켠 쪽 ACK가 더 짧은 이유
  - 객체 영역 좁히기의 근거
  - SMP legacy 단발 실패
- 실제 비일관 CXL, node 2, host DAX와 `dcdctl`은 쓰지 않았다. commit은 로컬에만 있다.
