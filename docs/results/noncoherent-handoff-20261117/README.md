# 비일관 CXL hand-off (2026-11-17)

0·1·2단계 결과를 기록한다.
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

## 남은 위험과 다음 brief

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

## 정리와 저장 공간

이 실행과 연결된 lab config 43개에서 살아 있는 소유 VM이 없음을 확인했다.
소유한 공유 메모리 backing과 임시 hash 배열도 남지 않았다.
최종 .local은 79 GiB, run18은 6.4 GiB다. / 여유는 109 GiB였다.
시작 때 .local 54 GiB, / 여유 134 GiB였다.
실행은 약 2시간 30분이었다. 0~2단계의 요청된 본시험을 완료했다.
Local commit만 만들었고 push하지 않았다.
