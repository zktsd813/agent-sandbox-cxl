# EPT adopt의 비일관 cache 정리

Guest RAM 쓰기는 KVM dirty bitmap과 Firecracker 장치 bitmap의 합집합으로 기록한다.
VM 생성 때부터 dirty tracking을 켠다. KVM의 새 mapping·guest write helper는 bitmap,
이미 매핑한 page의 CPU 쓰기는 PML, 장치의 쓰기 전 표시는 vm-memory bitmap이 맡는다.
장치 저장과 비동기 I/O drain 뒤 마지막 합집합을 수집한다.
Promotion의 직접 복사는 전체 RAM을 내려쓰고 새 dirty 경계를 만든다.

보내는 host는 합집합의 page에 `clwb` → `sfence`를 실행하고 쓰기 기록과 epoch를 발행한다.
받는 host는 아직 소비하지 않은 기록의 합집합에 `clflushopt` → `mfence`를 실행한다.
기록은 VM별 기존 RAM mapper의 metadata 영역에 있다. 8개를 넘겨 지연되면 RAM 전체를 무효화한다.
Packet·mailbox·mapper lease의 identity와 발행 순서도 검사한다.
공유 kernel 객체 영역이나 host별 공유 장부는 없다.

EPT의 A/D bit는 CPU도 쓰므로 page 목록을 따로 만들지 않는다.
Release 뒤 `[EPT base, base+bump)` 전체를 `clwb`·`sfence`로 내려쓴다.
받는 쪽은 같은 구간을 `clflushopt`·`mfence`로 무효화한 뒤 adopt한다.
실제 명령 발행 지점에서 bytes·line 수를 센다.

미리 정리는 기존 VMM event loop에서 소유자의 수집·내려쓰기·발행을 끝낸 뒤,
준비된 수신자에게 RAM 선무효화를 요청한다. 보조 thread는 없다.
이후 guest·장치 쓰기는 다음 기록에 다시 모인다. 최종 export에서도 마지막 기록을 발행한다.
`--preclean`으로 켜고, `--cache-maintenance` / `--no-cache-maintenance`로 RAM·EPT 정리를 고른다.
빈 EPT 비교도 같은 RAM 정리 경로를 쓴다.

L0 정답지는 중지 경계의 4 KiB page hash와 실제 명령 영수증을 독립 비교한다.
Adopt 전후와 비소유 host 종료 전후의 전체 16 MiB 표 hash도 비교한다.
오류 주입은 영역 밖 root, 잘못된 bump, EPT·RAM 무효화 생략 네 가지다.
Line hash 회계, perf·schedstat 꼬리 진단과 수동 보호 최적화는 없다.
정답지 gate는 lab용 관찰 장치이며 hand-off protocol에는 들어가지 않는다.
DRAM으로 에뮬레이트한 일관성 있는 lab의 검증이다. 실제 비일관 장비의 전기적 동작을 뜻하지 않는다.

## EPT 나누기

Chunk의 DRAM 사본은 mapper fd에만 있고 공유 GPA 표는 계속 CXL 집 주소다.
매 기록 수집의 KVM·장치 합집합을 로컬 chunk 누적 bitmap에 OR한다.
미리 정리로 현재 기록을 비워도 이 누적 집합은 내릴 때까지 남는다.
내리기에서 복사한 page를 로컬 pending bitmap에 넣는다. 다음 record 발행 후에만 지운다.
이미 앞 record에 있던 page도 다시 넣는다. 앞 record를 소비한 host도 새 집 값을 보아야 한다.
Eager fault는 guest 정지 중 RAM 값을 바꾸지 않는다. 이때 생긴 보수적 dirty bit는
최종 수집 뒤 새 chunk의 시작 경계에서 지운다. 그 뒤 guest·장치 쓰기는 모두 누적한다.

Lab은 L1 RAM도 파일로 backing하여 L0가 mapper의 실제 DRAM PFN을 읽는다.
올린 사본 = 집, 올린 뒤 변경 ⊆ 누적 bitmap, 내린 집 = 직전 DRAM을 각각 hash로 비교한다.
내리기 page가 바로 다음 record에 있는지도 별도로 검사한다.
Q35 RAM의 2~4 GiB hole을 보정한다. L1 mmap·KVM leaf와 독립인 읽기 전용 경로다.
