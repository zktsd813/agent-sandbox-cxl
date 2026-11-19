# 23 진행 기록

- 2026-11-19 12:14 UTC 시작. vm-cleanup HEAD 616d5fd. 예산 8시간. 지정 인계·서버 규칙·설계·결과 읽음. 다른 세션 변경 보존. node0 free 29611MiB, / 여유 23GiB. 새 원시는 .local/run23. 기존 실험 원시는 수정하지 않음.
- 기존 MemoryTier는 전체 RAM promotion, mapper 공유 GPA 표는 집 주소. 로컬 overlay를 mapper fd에 두어 기존 HVA·memslot을 유지. KVM 로컬 chunk bitmap과 PT zap/선택을 추가할 계획. 공유 mapper 표는 변경하지 않음.
- 12:21 UTC KVM/mapper/Firecracker 구현과 첫 빌드 통과. Python35/35. 절반 chunk×100왕복이 append-only16MiB를 넘으므로 TLB flush+RCU 뒤 unreachable 표만 로컬 bitmap에서 재사용하도록 설계 변경. L0 정답지는 QEMU의 L1 RAM file에서 실제 DRAM PFN을 읽어 promotion복사·누적dirty·demotion집·다음record를 검증. 첫 smoke는 실행 소스 미commit gate에서 중단, 소유 VM 종료. 새 RAM backing 정리 목록 수정.
