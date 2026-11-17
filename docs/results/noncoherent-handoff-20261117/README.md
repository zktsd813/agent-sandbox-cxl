# 비일관 CXL hand-off (2026-11-17)

현재 0·1단계 결과다. 2단계는 진행 중이다.
Compute Express Link(CXL)의 실제 비일관 장치는 쓰지 않았다.
물리 host L0의 Non-Uniform Memory Access(NUMA) node 0만 썼다.
두 바깥 가상 머신(VM, Virtual Machine)을 L1 A/B라 부른다.
Guest VM은 L2다. 네트워크는 host에서 QEMU socket backend만 썼다.
혼합 I/O의 TAP은 L1 안에만 만들었다.

## 0단계

`a1b5848`의 [합친 tree 검증](../trajectory-faults-20261116/merge-smoke.md)을 통과했다.
Gin 1 vCPU와 2 vCPU의 독립 검증이 통과했다.
vCPU는 virtual central processing unit이다.
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
SWE(Software Engineering) 궤적은 명령 원문, 반환값, test 요약, 최종 diff의 독립 검증도 통과했다.
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
소유한 빌드 파일의 깨끗한 cache만 반납했다. 전역 drop_caches는 쓰지 않았다.
모든 실행 VM과 공유 backing을 정리했다.
Python 기존 시험 48개가 통과했고 증거용 8개는 skip이다.
Rust 기본 계약 19개가 통과했고 1개는 ignore다. 최종 변경 뒤 다시 검사한다.

## CXL 환산 기준과 한계

제공된 [wbcost.jsonl](dcd-flush-cost/wbcost.jsonl)을 읽기만 했다.
이 실행은 node 2나 실제 DCD 장치를 사용하지 않았다.
Dirty cached line의 내려쓰기는 약 16 ns/line이다.
Absent line flush와 수신 무효화는 약 6.25 ns/line이다.
Cached clean line 무효화는 약 2 ns/line이다.
2 GiB는 33,554,432 line이다. 전체 dirty 내려쓰기는 약 536.87 ms,
전체 absent 무효화는 약 209.72 ms로 환산된다.
Lab의 backing은 DRAM(Random Access Memory)이다. 환산은 실측 hand-off 시간이 아니다.
실제 stale 재현도 하지 않았다.
원시는 implementation/virtual_machine/.local/run18에 있다.
