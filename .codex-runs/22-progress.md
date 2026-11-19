# 22 진행 기록

- 2026-11-19 11:24 UTC 시작. bud05, vm-cleanup, HEAD 04ef44a. 예산 종료 16:24 UTC.
- 지정 handoff/서버 규칙/21 결과를 읽음. 다른 세션 변경 보존, node0·lab/quiet 잠금·nice19·jobs≤16 적용. minimal-readable-experiments skill 적용.
- annotated 보존 태그 vm-pre-ept-adopt-cleanup-20261119 생성. push 없음. 기존 보존 태그 유지.
- 삭제 전 모든 추적 파일·두 patch 기능 표 작성. 원시 .local 142 GiB, / 여유 37 GiB, node0 free 44,603 MiB. 원시는 삭제하지 않음.
- 11:32 UTC KVM 공유 객체 allocator·direct ABI·audit/refcount/retire·SPTE 진단 삭제. EPT와 mapper admission만 10파일/567추가줄. 새 bzImage·kvm/kvm-intel 모듈 빌드, C probe/native control 빌드, Python 57통과8skip, 기존 Rust 계약 통과. 원본 fuzz0 dry-run 통과.
- 11:46 UTC Firecracker의 direct graph·heap allocator·옛 snapshot/reuse API/env 삭제, 상태 복원은 worker.rs로 이동. KVM/FC/QEMU 원본 fuzz0 dry-run 모두 통과. 원본 Linux zip 경로는 현재 없어 검증된 원본 DCD import commit 28ec385의 파일을 사용(별도 source tree). Python 35/35 통과, 새 mapper·native control·page hash·guest writer 빌드 통과. 최신 FC 재빌드·Rust 계약 진행, 새 assets2 포장 완료.
- 11:50 UTC smoke3 성공: 256 MiB 전체 쓰기 4hop, 누락0, 전체16MiB 표 hash4/4, release 뒤 이전 host 종료 불변. 첫 두 smoke의 CLI PCI bus/dirty tracking 누락은 수정하고 실패 자료를 그대로 보존. node0 약40GiB, 기존 .local 삭제 없음. 개인 storage 복사본이 lab당2.9GiB라 이후에는 QEMU qcow2 COW overlay로 만들어 기존 원시를 보존하면서 공간 사용을 줄임.
- 11:55 UTC 마지막 진단 잔재 제거: paused-clean phase6·옛 오류 환경변수·공유객체 cache fd 인자·mapper query/audit/retire/zero·비소유 counter 없음. 남은 오류 주입은 EPT 정확성 4종만. FC2964추가줄, Python35/35·Rust15회(고유14) 통과. 새 assets3와 모든 patch fuzz0/offset0 확인. 전체256MiB·2vCPU·200왕복, SWE heavy, 기준선·cache off, ABAB 검증 시작.
- 12:02 UTC offline verifier의 옛 mode 비교 helper를 삭제하고 promotion lease/alias·FD 연속성 검증을 실제 EPT trace에 연결. Python35/35, Rust 관련 계약 전체 재통과. build.sh test가 Python 계약도 실행하며 QEMU는 외부 옛 경로 대신 격리 source/toolchain을 사용. QEMU 증분 빌드 통과. 전체 쓰기 시험은 350회 이상 이동 중 누락0.
- 12:06 UTC 정확성 전체 통과: 256MiB fullword·2vCPU·200왕복=400이동/801경계/누락0/hash400불변, race+preclean, Gin 원본27명령·heavy3이동·27선정리 독립검증. 재충전 faults는 adopt6회 모두0, 빈EPT6회 모두65536. Cache off/HTTP/4오류주입 통과.
- 12:08 UTC ABAB 4블록×50측정(+블록별10warmup) 통과. 모드당100표본, adopt p50/p95 31.09/37.59ms, 빈EPT30.83/35.61ms. 외부 FC/QEMU0, 1분load0.84~0.90, node0free29.46~29.53GiB. 모든 소유lab·shm 종료 확인. 성공 oracle 영수증을 offline 재검증했고 template hash 불변. .local154.2GiB 보존. 결과 정리 장·최종보고 작성 중.
- 12:11 UTC 완료. 결과 README 정리 장과 cleanup.json, 22.report.md/22.last.md 작성. 마지막 Python35/35·diff whitespace 통과, 다른 FC/QEMU0. 추적 파일205→45, 총 newline276572→16296, KVM추가2789→567, FC5515→2964, EPT전용핵심945→935. RESEARCH 수정 필요 줄 목록만 보고서에 기록. 태그/commit push 없음.
