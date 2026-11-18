# 20 진행 기록

- 시작: 2026-11-18 06:04 UTC, bud05, vm-cleanup, b53c9f5.
- 기존 변경: .gitignore, docs/RESEARCH.md, agent-workload/capacity 결과, experiments/capsim. 읽기만 하며 stage하지 않는다.
- 18 서버 규칙과 19 기록·설계·결과 확인. node0 free 21 GiB, / free 99 GiB. lab/quiet 잠금, NUMA0, nice19, jobs<=16 유지.
- minimal-readable-experiments 적용. 기존 runner를 확장한다. 원시 자료 implementation/virtual_machine/.local/run20.
- 우선 수집 단계와 L1 KVM 구간별 시간, scheduling 상태를 계측하고 256 MiB 220회 재현부터 수행한다.
