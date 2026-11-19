# 22 진행 기록

- 2026-11-19 11:24 UTC 시작. bud05, vm-cleanup, HEAD 04ef44a. 예산 종료 16:24 UTC.
- 지정 handoff/서버 규칙/21 결과를 읽음. 다른 세션 변경 보존, node0·lab/quiet 잠금·nice19·jobs≤16 적용. minimal-readable-experiments skill 적용.
- annotated 보존 태그 vm-pre-ept-adopt-cleanup-20261119 생성. push 없음. 기존 보존 태그 유지.
- 삭제 전 모든 추적 파일·두 patch 기능 표 작성. 원시 .local 142 GiB, / 여유 37 GiB, node0 free 44,603 MiB. 원시는 삭제하지 않음.
- 11:32 UTC KVM 공유 객체 allocator·direct ABI·audit/refcount/retire·SPTE 진단 삭제. EPT와 mapper admission만 10파일/567추가줄. 새 bzImage·kvm/kvm-intel 모듈 빌드, C probe/native control 빌드, Python 57통과8skip, 기존 Rust 계약 통과. 원본 fuzz0 dry-run 통과.
