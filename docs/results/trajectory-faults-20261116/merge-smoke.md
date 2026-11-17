# 합친 tree 검증 (2026-11-17)

기준 HEAD는 `c999fe9`다. 기본 patch 두 개는 다중 vCPU 빌드와 SHA-256이 같다.
17의 산출물을 재사용했다. 해시는 [merge-smoke.json](merge-smoke.json)에 있다.

Gin 무거운 명령 정책의 1 vCPU resident direct가 통과했다.
Gin 전체 명령의 2 vCPU baseline과 resident direct도 통과했다.
실행 중 왕복을 포함한다. `lab.py verify`와 독립 검증을 모두 통과했다.
반환값, test 요약, 최종 diff, CPU 설정이 같다.
Python 시험은 48 통과, 기존 증거용 8 skip이다. Rust 계약 시험은 19 통과, 1 ignore다.

명령 구간 EPT(Extended Page Tables) 위반 합은 160,835회다.
17의 동일 입력 161,054회와 -0.14% 차이다.
14의 약 262,413회보다 줄어든 이유도 분리했다.
현재 입력에서 mapper만 이전 버전으로 바꾸면 266,692회다.
14 평균보다 +1.63%다. `eb15cff`가 첫 쓰기의 PTE(Page Table Entry) 제거를 없앤 효과다.
합치기 오류 때문에 검증이나 명령 실행이 생략된 것이 아니다.

첫 시도는 기본 커널의 histogram 기능 부재로 계수 시작에서 거절됐다.
17의 histogram 커널 fixture로 다시 실행했다.
Rust 도구의 오래된 헤더 경로도 수정해 재실행했다. 저장소 코드 수정은 없었다.

NUMA(Non-Uniform Memory Access) node 0만 사용했다.
각 L1 4 GiB, CXL(Compute Express Link) backing 8 GiB, 제어 64 MiB다.
시작 직전마다 예산 16,448 MiB + 4 GiB를 검사했다.
모든 실행은 socket backend, lab 배타 잠금과 quiet 잠금을 사용했다.
실행한 VM과 공유 backing은 모두 정리했다.
원시는 `implementation/virtual_machine/.local/run18/`에 있다.
