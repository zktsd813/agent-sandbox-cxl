# EPT 나누기

Compute Express Link(CXL) 메모리와 host 로컬 동적 메모리(Dynamic Random Access Memory, DRAM)를 한 가상 머신(Virtual Machine, VM)에서 함께 쓴다.
확장 페이지 표(Extended Page Tables, EPT)의 상위 3단계 표는 CXL에 둔다.
최하위 페이지 표(Page Table, PT)는 2 MiB chunk 데이터의 위치를 따른다.
4 KiB 매핑만 쓴다. 모든 데이터 page의 집 주소는 CXL에 남는다.

구현은 Linux 가상화 모듈(Kernel-based Virtual Machine, KVM), 기존 메모리 mapper, Firecracker에 들어간다.
Mapper fd의 로컬 overlay로 같은 VMM 주소와 memslot을 유지한다.
계층 bit, 누적 dirty bitmap, DRAM 사본·PT는 host 밖으로 보내지 않는다.

vCPU는 가상 프로세서(virtual Central Processing Unit)다.
전환은 vCPU 정지와 장치 입출력 drain 뒤에만 한다.
올리기는 2 MiB 복사 → mapper 전환 → PT 교체 → 주소 변환 캐시(Translation Lookaside Buffer, TLB) 비움 순서다.
Lazy는 다음 접근에서 PT를 채운다. Eager는 멈춘 vCPU 문맥에서 기존 fault 경로로 512 page를 채운다.
내리기는 누적 dirty page만 집에 복사하고 `clwb`로 내려쓴다. 복사 page는 바로 다음 record에 다시 넣는다.
Hand-off 전에는 전부 내린다. Release는 계층 bitmap과 실제 상위 표를 검사한다.

기존 16 MiB append-only 영역은 절반 chunk의 반복 왕복을 감당하지 못한다.
따라서 변환 비움과 읽기 경로 지연 회수(Read-Copy Update, RCU)가 끝난 표만 재사용한다.
이 free bitmap도 로컬이다. Adopt는 도달 가능한 표 밖의 이전 할당만 회수한다.
붙이는 순간에는 EPT byte를 바꾸지 않는다.

## 코드와 검증 경계

22 HEAD `616d5fd`와 실제 소스를 비교한 추가 줄이다. Patch의 문맥 줄과 생성물은 뺐다.

| 구분 | 추가 줄 |
|---|---:|
| KVM | 195 |
| 메모리 mapper | 94 |
| Firecracker | 164 |
| 유지 시험 도구 | 230 |
| 원시 실행·분석 스크립트 | 241 |

KVM과 mapper 합계는 289줄이다. 새 daemon이나 공유 KVM 객체는 없다.
설계는 [EPT_ADOPT.md](../../../implementation/virtual_machine/EPT_ADOPT.md), cache 순서는 [NONCOHERENT.md](../../../implementation/virtual_machine/NONCOHERENT.md)에 있다.
최종 기능 commit은 `f042730`이다. 원본 patch의 fuzz·offset 0 적용과 Python 36개·Rust 고유 계약 14개를 통과했다.

## 정확성

2 GiB VM이다. 아래 이동마다 L0 정답지 누락은 0, release→adopt 전체 16 MiB EPT hash는 일치했다.

| DRAM chunk 수·조건 | 왕복 | 이동·hash 일치 |
|---|---:|---:|
| 0, 2 vCPU·256 MiB 전체 word 쓰기 | 200 | 400/400 |
| 1, lazy·미리 정리 | 100 | 200/200 |
| 16, lazy·미리 정리 | 100 | 200/200 |
| 512, lazy·미리 정리 | 100 | 200/200 |
| 1, 최종 binary lazy | 100 | 200/200 |
| 1, 최종 binary eager | 100 + 준비 1 | 202/202 |

앞 네 조건은 assets6다. 이후 진단 PFN 복사의 잠금 범위와 실패 차단 시점을 보완한 assets7로 마지막 두 조건을 다시 돌렸다.
KVM과 데이터·PT 전환 알고리즘은 같다. Assets7의 절반 조건 추가 4이동, Gin, 오류 주입도 통과했다.
정답지를 켠 공유 EPT 정확성 이동 합계는 1,447회다. 누락 0, hash 1,447/1,447이다.
이전 host의 munmap·파일 서술자 정리·Firecracker 종료 뒤에도 표 hash가 유지됐다.

바깥 host를 L0, 안쪽 두 host를 L1이라고 부른다.
L0는 실제 L1 DRAM frame과 CXL 집의 4 KiB hash를 읽는다.
올린 사본 = 집, 올린 뒤 변경 ⊆ 누적 bitmap, 내린 집 = 직전 DRAM을 각각 검사한다.
그 뒤 바로 다음 record에 내린 page가 있는지 확인한다. 압축 원시 hash도 독립 재검증했다.
절반 조건은 변경 page 관찰 13,112,470개, 내린 page 관찰 13,634,279개였다. 보수적 dirty 표시가 조금 더 많다.
이 조건의 독립 tier hash 검사는 307,200회다. EPT 최고 사용량은 700,416 byte로 16 MiB 안이었다.

Gin 원본 27명령의 독립 검증을 통과했다. 이전 실행의 같은 명령 dirty 기록으로 315·175·175 chunk를 골랐다.
무거운 명령 전에 올리고 명령 뒤 대기 경계에서 내린 다음 hand-off했다.
이 구간의 DRAM chunk에서 장치 bitmap 표시 57,355 page 관찰을 확인했다. CPU·장치가 같은 page를 표시할 수 있다.
기존 race·cache 정리 해제·HTTP(Hypertext Transfer Protocol) 제어·빈 EPT 기준선도 통과했다. 기준선의 준비 후 6회 이동은 payload fault가 매번 65,536회였다.

오류 주입 결과다.

- DRAM을 남긴 hand-off: KVM release가 `EXDEV`로 거절했다.
- 내릴 page 하나 누락: L0의 집 대 DRAM hash가 검출했다.
- 다음 record에서 page 하나 누락: L0가 검출했다. 앞 record에 있던 page라도 다시 요구한다.
- 기존 영역 밖 root·잘못된 bump는 adopt 전에 거절했다. RAM·EPT 무효화 생략은 명령 영수증이 검출했다.
- 마지막 명시 TLB 비움 생략은 guest 오류 없이 통과했다. Mapper notifier가 앞서 변환을 비웠기 때문이다. 모든 비움 경로를 제거한 시험은 아니다.

Native와 HTTP export 실패는 source 종료로 차단된다. 직접 내리기 API(Application Programming Interface) 실패 뒤 Resume 요청도 HTTP 400으로 거절됐다.
L0 시험 모드만 기존 gate에 PFN·bitmap 영수증을 쓴다. 수신 worker는 이를 사용하지 않는다. 정상 packet과 공유 mapper 표에는 tier 상태가 없다.

## 비용

**모든 수치는 중첩·DRAM 기반 CXL 에뮬레이션의 동작 비용이다. 실제 CXL 지연과 L1 EPT page walk 이득은 포함하지 않는다.**
정답지 gate를 끈 별도 ABAB 40 lab, 측정 920이동이다.
각 lab 전에 외부 Firecracker·QEMU는 0개였다. 1분 load average는 0.56~1.14, node 0 free는 18,818~19,755 MiB였다.

한 chunk 올리기 중앙값이다. 256 MiB 쓰기 부하에서 각 방식 100표본이다.

| 항목 | Lazy | Eager |
|---|---:|---:|
| 올리기 본체: 할당·복사·mapper/PT 교체·비움 요청 | 0.372 ms | 0.717 ms |
| pause·drain·기록 발행·HTTP 제어를 포함한 API | 10.520 ms | 10.796 ms |
| 재개 뒤 첫 접근 EPT violation | 512회 | 0회 |

Eager는 올리기 안에서 512 page를 합성 fault 경로로 채운다. 작업을 앞당긴 비용이다.
VM 전체 `tlb_flush` 통계는 두 방식 모두 관찰 구간당 1 증가했다. Chunk별 TLB hit/miss나 walk 지연을 분리한 값은 아니다.

내리기 본체 중앙값이다. 단위는 µs다. 같은 2 MiB chunk의 접근 page 수를 고정했고, 각 칸 40표본이다.
실제 복사 page 수가 입력 0·1·8·64·512와 모두 같았다. 이 표도 같은 중첩·DRAM lab 수치다.

| 쓴 page | Lazy PT | Eager PT |
|---:|---:|---:|
| 0 | 21.23 | 38.77 |
| 1 | 35.19 | 40.97 |
| 8 | 38.51 | 46.28 |
| 64 | 77.90 | 79.97 |
| 512 | 351.54 | 370.89 |

Eager는 접근하지 않은 leaf도 미리 채우므로 0 page에서도 떼는 PT 작업이 더 많다.
복사·내려쓰기·host 가상 주소(Host Virtual Address, HVA) 무효화 단계만 보면 lazy의 0/512 page 중앙값은 1.39/313.12 µs였다.
본체 수치는 공통 dirty 수집·record 발행을 제외한다. API와 hand-off 수치는 이를 포함한다.

정지→guest 확인 응답(Acknowledgement, ACK) 중앙값이다. 단위는 ms다. 각 칸 40표본이다. 이 표도 같은 중첩·DRAM lab 수치다.

| DRAM chunk | 최종 export에서 내림 | 미리 내림 |
|---:|---:|---:|
| 0 | 34.50 | 32.21 |
| 1 | 38.84 | 32.45 |
| 16 | 46.24 | 26.14 |
| 512 | 121.58 | 27.50 |

0 chunk의 두 arm은 같은 동작이며 시간 차이는 대조군 잡음이다.
미리 내림은 현재 CXL dirty 기록도 함께 정리한다. 차이를 DRAM 복사만의 효과로 분리하지 않았다.
ACK 뒤 payload 검증 시간은 이 경계에 들어가지 않는다. 검증 완료는 모든 이동에서 별도로 기다렸다.
받는 쪽 payload fault는 0·1·16·512 chunk에서 각각 0·512·8,192·65,536회였다.
내리며 없앤 PT만 다시 채운다. 기존 CXL 부분은 그대로 붙는다.

1·16 chunk는 guest 물리 주소(Guest Physical Address, GPA) 128 MiB부터 골랐다. 절반은 0~512에서 ACK용 chunk 32를 제외한 512개다.
중앙값 외 분포와 raw 경로는 [summary.json](summary.json)에 있다. 원시는 `implementation/virtual_machine/.local/run23/`다.
초기 root 참조·정답지 RAM hole 오류를 고친 smoke, 메모리·이름 사전 검사 중단, HTTP 종료 응답 기대 수정 자료도 보존했다.
메모리 부족 때 자기 원시의 page cache만 반환했다. 파일과 다른 세션 자료는 지우지 않았다.

## 남은 위험

실제 비일관 장비와 다른 물리 서버는 아직 검증하지 않았다.
소유권 fencing은 기존 controller와 mapper epoch 조건에 의존한다.
단일 guest 메모리 영역, 같은 root role, 읽기 전용 block, 실행 금지(No Execute, NX) 큰 page 완화 해제 제약을 유지한다.
표 재사용은 이전 owner의 RCU 종료를 전제로 한다. Release가 이를 기다린다.
실행 중 전환, 2 MiB 큰 leaf, 자동 계층 정책은 지원하지 않는다.

## Lab에서 알 수 없는 것과 실제 장비 시험

중첩 lab의 하드웨어는 L1 EPT 대신 L0 shadow 표를 걷는다.
따라서 DRAM PT의 page walk 지연 이득은 이 수치에서 알 수 없다.
CXL 파일도 L0 DRAM이다. 실제 CXL 지연을 반영하지 않는다.
여기서는 정확성과 복사·fault·hand-off 동작 비용만 평가한다.

실제 장비 시험은 사용자 승인 후 별도로 한다. 이번에는 실행하지 않았다.

1. 중첩 가상화를 쓰지 않는 두 host와 실제 CXL을 준비한다. CXL의 물리 배치를 같게 하고 VM 영역을 전용 예약한다. 수정한 KVM을 설치할 권한과 유지보수 시간을 먼저 승인받는다.
2. 같은 guest 상태, 4 KiB 매핑, dirty log, CPU 고정, vCPU 1·2개를 쓴다. 0·1·16·절반 chunk와 dirty 0·1·8·64·512 page를 ABAB로 반복한다. 미리 내림과 최종 export 내림을 비교한다. 원격 transport와 장치 drain 시간도 나눈다.
3. Guest에서 TLB 범위를 넘는 무작위 pointer chase를 실행한다. 재사용 거리와 접근 수를 고정한다. Guest cycle, 첫 접근 fault, 명령 지연, 지원되는 EPT·주소 변환 성능 counter를 함께 기록한다. 지원하지 않는 counter를 추정하지 않는다.
4. PT 위치만의 효과를 가리려면 데이터는 같은 로컬 DRAM에 두고 PT만 CXL 또는 DRAM에 두는 시험 전용 대조군이 추가로 필요하다. 이 대조군도 승인 뒤 만든다. 단순 CXL/DRAM 비교로 데이터 지연과 표 지연을 분리했다고 주장하지 않는다.
5. 두 host 왕복에서 dirty 목록, 내려쓰기·무효화 명령 수, release/adopt 표 hash, guest 검증을 함께 확인한다. 부하와 CPU·메모리 배치, kernel·binary hash, 성능 event 이름과 raw 설정을 보존한다.

