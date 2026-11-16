# 복원 전 주소표 채우기

기본 비교 결과는 commit `1616fb0`에서 확정했다. 이후 구현은 두 overlay로 분리했다. 기존 `kvm-dax-state.patch`와 `firecracker.patch`는 수정하지 않았다.

`CXL_LAB_PREFAULT=1`인 격리 빌드는 기존 patch 다음에 `kvm-prefault.patch`, `firecracker-prefault.patch`를 각각 적용한다. 기본 빌드는 이전 동작을 유지한다. 호스트 커널에는 적용하지 않는다.
Base patch 버전을 바꿀 때는 깨끗한 원본에 새 base와 overlay를 순서대로 다시 적용한다.

새 ioctl(input/output control)은 실험용 x86 가상 중앙처리장치(vCPU, virtual central processing unit) 인터페이스다. upstream `KVM_PRE_FAULT_MEMORY`와 호환되는 응용 바이너리 인터페이스(ABI, application binary interface)라고 주장하지 않는다. 80 byte 요청과 16 byte GPA(guest physical address) 범위를 쓴다. 실행 주체는 정지한 단일 vCPU와 고정된 메모리 슬롯을 보장해야 한다. 커널은 정렬, 오버플로, 범위 중복, 주기억장치(RAM, random-access memory) 슬롯, 쓰기 권한, 개수 상한을 검사한다. MMIO(memory-mapped input/output)는 거부한다. 오류나 부분 완료 때 재개하지 않는다. 무효화 순서나 슬롯 세대가 바뀌면 전체 작업을 다시 해야 한다.

QUERY는 현재 직접 매핑 주소표의 존재하는 leaf를 순회한다. 연속 RAM 범위를 합친다. source VMM(virtual machine monitor)이 자기 vCPU0 파일 서술자를 복제해 조회한다. 여러 VM이나 vCPU를 한 프로세스에서 다루는 용도가 아니다. 범위 목록은 VM 식별자, 이동 세대, RAM 크기와 함께 전달한다. 목적지는 이 값과 총 page 수를 검사한다. 메모리 내용은 읽거나 복사하지 않는다.

FILL은 `kvm_mmu_reload` 뒤 범위의 4 KiB마다 `kvm_mmu_do_page_fault`를 호출한다. 기본은 `prefetch=true`다. 별도 ACTIVATE 옵션은 접근 추적 entry의 잔여 fault를 확인하기 위해 쓰기 접근 방식으로 채운다. 이 옵션은 guest 비동기 page fault가 켜져 있으면 거부한다. 일반 MMU(memory management unit) 잠금과 fault 경로를 사용한다. source 조회는 MMU 잠금과 RCU(read-copy-update) 아래에서 leaf를 읽는다. 이 실험은 고정된 메모리 슬롯만 지원한다.

Firecracker는 CPU·VM·장치 상태 복원 뒤, vCPU thread 시작 전에 FILL을 호출한다. `--prefault all`은 RAM 전체, `--prefault mapped`는 이동 직전 source의 매핑 범위를 채운다. `--prefault-activate`는 ACTIVATE를 선택한다. source 조회·목록 전달·목적지 채우기는 모두 이동 요청과 재개 사이에 있다. 목적지 완료 보고서는 궤적 시간 측정 뒤 수집한다. 조회 두 번과 채우기의 커널 시간, 전체 경과 시간, 실제 처리 page 수를 별도로 남긴다.

기능 검증과 추가 측정 결과는 아래에 기록한다.

## 기능 시험

Gin heavy 전체 궤적을 두 경로로 한 번씩 실행했다. 두 run 모두 `lab verify`와 독립 workload 검증을 통과했다. A/B 각각 별도 VM에서 잘못된 요청 12종의 거부, 빈 root, 4 page 채우기, 정확한 2개 범위 조회, ACTIVATE도 확인했다. 이 ABI 시험에서는 guest를 실행하지 않았다. 원시 요약은 `prefault-functional.json`이다.

RAM 전체 경로는 이동마다 524,288개 4 KiB page를 채웠다. kernel 시간은 이동마다 약 0.616–0.620초였다. 첫 이동 전 RAM fault는 8,925회다. 첫 이동 뒤 모든 명령의 RAM fault는 0회였다. 마지막 실제 leaf도 4 KiB 524,288개다. 이 하드웨어와 설정에서는 기본 `prefetch=true`로 충분했다. 본 비교에 ACTIVATE를 쓰지 않는다.

매핑 범위 경로는 세 이동에서 각각 9,707 / 146,977 / 159,584개 page를 채웠다. 목록은 각각 902 / 1,858 / 1,851개 범위다. 두 번 조회한 kernel 시간 합은 1.15 / 5.69 / 6.09 ms였다. 채우기 kernel 시간 합은 11.73 / 175.03 / 188.27 ms였다. 궤적 RAM fault는 261,929회였다. 새로 쓰는 page는 여전히 fault를 일으킨다. 전체 RAM 경로와 구분해야 한다.

초기 구현은 첫 채우기 뒤 `EAGAIN`으로 중단했다. 진단에서 메모리 슬롯 세대는 고정됐지만, 이 lab의 직접 접근(DAX, direct access) 풀 mapper는 첫 쓰기 fault에서 `zap_vma_ptes`를 호출한다. 이때 MMU 무효화 순서가 page마다 변했다. 전체 작업의 보수적인 순서 검사가 이를 감지했다. Firecracker는 최대 네 번 전체 범위를 다시 시도한다. 위 시험은 매 이동 두 번으로 완료했다. 두 번째 시도 시간도 정지 시간에 포함했다. 이 구현의 비용은 최적화된 prefault의 하한이 아니다. 오류 상태에서는 재개하지 않는다.

기존 Python 시험은 42개 통과, 증거 의존 8개 skip이다. 기존 Rust 계약과 새 ABI·범위 시험은 합계 17개 통과, 기존 1개 ignore였다. 커널 모듈과 Firecracker를 각각 격리 빌드했다. 모든 기능 시험의 QEMU와 공유 메모리 backing은 정리했다.

다음 본 측정은 새 모듈·VMM의 동일한 binary로 무이동, snapshot, direct, RAM 전체 prefault, 매핑 범위 prefault를 함께 재실행한다. fixture와 방식 순서를 무작위로 섞는다. 시간 run seed는 20261119, 수를 세는 run seed는 20261118이다. 조건마다 3회다.

추가 본 측정에는 `--trajectory-policies heavy all --prefault-variants all mapped --repetitions 3`을 쓴다. 단일 기능 시험은 `--modes state-only --prefault all` 또는 `--prefault mapped`로 고를 수 있다. `--prefault-probe`는 정적 ABI 시험 binary 경로다. 일반 host 커널에서 이 binary를 실행하지 않는다.

추가 실험의 물리 비용 계산은 재개 이후 demand fault만 다룬다. 예비 채우기 자체의 물리 실행 비용을 측정한 값은 아니다. 전체 RAM 경로는 미래에 처음 쓸 page의 fault도 미리 처리하므로, 잔여 demand 비용이 무이동보다 작아질 수 있다. Nested 채우기 시간을 물리 서버 시간으로 사용하거나, 이 음수 차이를 전체 성능 향상이라고 해석하지 않는다.

초기 계수 suite 하나는 RAM fault가 반드시 양수여야 한다는 이전 검증 규칙에 걸렸다. PHP all-RAM/all 조건은 RAM fault 0회, 장치 주소 fault 49회였고 guest 실행은 성공했다. 첫 명령 전 RAM 전체를 채운 영수증과 실제 leaf를 검증하는 경우에만 0회를 허용하도록 오프라인 검증을 고쳤다. 수정 후 같은 원시 run 검증과 Python 시험을 통과했다. 해당 검증 변경 뒤 `prefcount2`를 시작했다. `prefcount1`은 진단 자료다. 실행 코드와 binary는 바꾸지 않았다.

격리된 git 원본에 base와 overlay를 순서대로 적용했다. 문맥 오차를 허용하지 않는 `--fuzz=0` 검사다. 모든 대상 파일이 실제 빌드 source와 같았다. 기존 두 base patch도 초기 `675a9d5`와 같다. binary·모듈·설정·시험 로그 해시는 [prefault-build-validation.json](prefault-build-validation.json)에 둔다.

## 최종 계수의 재개 구성

`prefcount2`는 49회 통과 뒤, PHP snapshot/all/r0의 파생 장치 카운터가 한 명령에서 21→20으로 감소해 거절됐다. guest 실행은 성공했다. Live histogram의 total과 RAM bucket을 동시에 읽지 못한 1회 차이다. 값을 0으로 보정하거나 검증을 완화하지 않았다. 원시 자료를 남기고 같은 조건을 다시 쟀다.

재개는 동일 seed의 전체 90개 계획에서 유효한 앞 49개만 건너뛴다. 최종 계수는 `prefcount2`의 통과 49회와 `prefcount4`의 나머지 41회다. 새 조건의 실행 입력·CPU 해시도 앞부분과 같다. 중간 `prefcount3` 시도는 node 0 여유가 예산에 못 미쳐 VM을 만들기 전에 거절됐다. 닫힌 자체 원시 파일 캐시만 해제한 뒤 다시 시작했다.

`--skip-trials 49`는 새 session과 output에서 무작위 계획의 뒷부분을 실행한다. `verify_swe_handoff.py --successful-suites PART1/report.json PART2/report.json`은 통과 run만 합쳐 중복·입력·반환값·최종 diff를 검증하고 제외 내역도 기록한다. 분석 JSON에도 거절 run과 입력 suite 상태를 남긴다. 실행 코드와 VMM·커널 binary는 재개 중에 바꾸지 않았다.

<!-- FINAL_TRAJECTORY_TABLES -->
## 다섯 방식의 본 측정 결과

새 커널 모듈·VMM binary 하나로 기존 세 대조군도 다시 실행했다.
유효 궤적은 시간 90회와 계수 90회다. 30개 조건마다 각각 3회다.
실제 명령은 합계 6,300개다. 원래 1–4단계의 108회와 합쳐 평균내지 않는다.

시간 run 2개에 부하 표시가 있다.
Gin direct/all/r2에는 다른 계수 controller `python3`의 수명 평균 CPU 29.4%가 보였다.
MicroPython 전체 RAM/all/r2에는 root `apt-get` 120%가 보였다.
표의 중앙값과 분포에는 이 표본도 포함한다. 원시 `ps`와 loadavg를 보존했다.
계수 run에는 해당 부하 표시가 없다. Histogram 읽기 불일치 1건은 위 기준으로 거절·재측정했다.

### 실제 궤적 wall time

단위는 초다. 무이동은 절대 시간 중앙값이다. 나머지는 무이동보다 늘어난 시간이다.
다음 수신 프로세스 준비와 매 명령 뒤 보고서 저장도 포함한다.

| 대상·정책 | 무이동 | snapshot 증가 | direct 증가 | 전체 RAM 증가 | 기존 범위 증가 |
|---|---:|---:|---:|---:|---:|
| Gin · heavy | 13.176 | 2.900 | 1.174 | 2.041 | 2.052 |
| Gin · all | 13.149 | 4.242 | 1.779 | 18.248 | 9.298 |
| PHP-CS-Fixer · heavy | 3.959 | 0.957 | 0.867 | 3.602 | 1.135 |
| PHP-CS-Fixer · all | 3.994 | 2.823 | 1.235 | 23.267 | 5.699 |
| MicroPython · heavy | 8.779 | 1.453 | 0.715 | 4.267 | 1.771 |
| MicroPython · all | 8.778 | 3.396 | 1.342 | 28.540 | 10.104 |

### 명령·이동 구간의 합 (active)

단위는 초다. 앞 표와 같은 방식으로 무이동 대비 증가를 표시한다.
미리 준비한 수신기로 이동하는 구간과 명령 송신→결과 수신 구간만 합친 값이다.
준비·보고서 기록 구간은 빠진다. 이 값도 guest CPU 실행 시간만을 뜻하지 않는다.
기존 범위 목록은 보고서 부피를 키운다. 특히 all 정책에서 wall과 active의 차이가 커졌다.
이 차이를 전부 주소표 복원 비용이라고 해석하지 않는다.

| 대상·정책 | 무이동 | snapshot 증가 | direct 증가 | 전체 RAM 증가 | 기존 범위 증가 |
|---|---:|---:|---:|---:|---:|
| Gin · heavy | 13.110 | 2.847 | 1.163 | 1.991 | 1.764 |
| Gin · all | 13.086 | 3.587 | 1.764 | 17.586 | 6.450 |
| PHP-CS-Fixer · heavy | 3.872 | 0.852 | 0.859 | 3.495 | 0.613 |
| PHP-CS-Fixer · all | 3.908 | 1.965 | 1.213 | 22.399 | 2.148 |
| MicroPython · heavy | 8.668 | 1.326 | 0.708 | 4.158 | 1.237 |
| MicroPython · all | 8.664 | 2.329 | 1.313 | 27.438 | 4.206 |

### 이동 정지 시간의 합

단위는 ms다. 같은 L0 시계의 요청→Resume 응답을 합한 중앙값이다.
조회·범위 전달·채우기·재시도·제어 비용이 모두 들어간다.
Direct의 native pause→Resume 구간은 이 L0 응답 구간보다 작다. JSON에는 둘 다 남긴다.

| 대상·정책 | snapshot | direct | 전체 RAM | 기존 범위 |
|---|---:|---:|---:|---:|
| Gin · heavy | 97.6 | 58.4 | 1,928.5 | 504.7 |
| Gin · all | 421.9 | 531.7 | 17,389.1 | 5,121.2 |
| PHP-CS-Fixer · heavy | 83.4 | 96.5 | 3,243.6 | 266.1 |
| PHP-CS-Fixer · all | 473.3 | 659.4 | 22,162.5 | 1,709.9 |
| MicroPython · heavy | 129.5 | 102.1 | 3,847.5 | 562.3 |
| MicroPython · all | 551.6 | 791.8 | 27,423.6 | 3,775.5 |

### 명령 구간 EPT 위반 합

3회 평균을 정수로 반올림했다. 아래는 RAM과 장치 주소를 모두 포함한 명령 구간 합이다.
전체 계수 구간 합은 JSON·CSV에 별도로 있다. 이동당 평균과 명령별 원시 계수도 있다.
전체 RAM/all 조건에도 소수의 장치 주소 위반은 남았다.

| 대상·정책 | 무이동 | snapshot | direct | 전체 RAM | 기존 범위 |
|---|---:|---:|---:|---:|---:|
| Gin · heavy | 261,754 | 515,667 | 262,413 | 8,948 | 261,729 |
| Gin · all | 261,355 | 584,421 | 263,096 | 16 | 262,755 |
| PHP-CS-Fixer · heavy | 34,079 | 112,180 | 38,656 | 14,853 | 34,102 |
| PHP-CS-Fixer · all | 34,088 | 208,288 | 38,825 | 15 | 34,119 |
| MicroPython · heavy | 86,981 | 180,505 | 88,148 | 6,929 | 86,977 |
| MicroPython · all | 86,963 | 284,529 | 88,315 | 10 | 86,996 |

### 예비 채우기의 비용과 의미

전체 RAM 경로는 이동마다 524,288개 4 KiB page를 채웠다.
최초 이동 뒤 모든 명령의 RAM fault는 18개 계수 run 전부 0회다.
이 하드웨어에서는 기본 `prefetch=true`로 충분했다.
전체 RAM/all의 명령 RAM fault도 모든 fixture에서 0회였다.

기존 범위 경로는 새로 쓰는 page의 fault를 남긴다.
예를 들어 PHP all의 명령 RAM fault 평균은 무이동 34,088회, snapshot 207,338회, 기존 범위 34,104회였다.
기존 범위 경로는 첫 명령 전 채운 목록과 나중에 새로 쓰는 page를 구분한다.
Direct의 작은 잔여 증가를 모두 새 page라고 단정한 것은 아니다. 주소별 이전 SPTE 대조는 하지 않았다.

다음은 kernel 경과 시간 합의 중앙값을 이동 수로 나눈 값이다.
단위는 ms/이동이다. 전체 RAM은 약 617 ms였다. source 조회는 두 번 순회한 시간의 합이다.
Nested 시간이며 물리 서버의 예비 채우기 단가가 아니다. 보수적인 전체 재시도도 포함한다.

| 대상·정책 | 전체 RAM 채움 | 기존 범위 채움 | 기존 범위 source 조회 |
|---|---:|---:|---:|
| Gin · heavy | 616.62 | 124.28 | 4.29 |
| Gin · all | 616.57 | 150.23 | 5.00 |
| PHP-CS-Fixer · heavy | 617.08 | 23.18 | 1.37 |
| PHP-CS-Fixer · all | 616.42 | 20.99 | 1.24 |
| MicroPython · heavy | 617.03 | 59.92 | 2.42 |
| MicroPython · all | 616.39 | 57.78 | 2.34 |

기존 범위 경로는 heavy 정책의 active 증가를 snapshot보다 줄였다.
all 정책에서는 매번 기존 매핑 전체를 다시 채우는 비용이 커졌다.
전체 RAM 경로는 미래에 처음 쓸 page까지 앞에서 채우므로 정지 시간이 더 커졌다.
이 결과는 최적화된 예비 채우기의 하한이 아니다. Direct는 기존 표를 재사용해 이 재구축 작업을 피한다.
이동하는 네 방식의 wall time 중앙값은 여섯 조건 모두 direct가 가장 작았다.
Active에서는 PHP heavy의 기존 범위가 direct보다 작았다. 작은 시간 순위를 일반화하지 않는다.

각 snapshot 방식은 계수 실행에서 357회 이동마다 새 VM/vCPU를 만들었다.
세 snapshot 방식 합계는 각각 1,071개다. Direct는 357회 이동 중 새 생성이 0회였다.
Snapshot 상태는 13,938–13,950 byte였다. RAM dump와 RAM 전송은 0 byte다.
계수 실행의 실제 leaf는 모두 4 KiB였다. 반대 L1의 leaf는 항상 0개였다.

검증 원장은 [prefault/validation.json](prefault/validation.json)이다.
[조건 요약](prefault/conditions.csv), [개별 궤적](prefault/trials.csv), [명령별 시간·fault](prefault/commands.csv)를 보존했다.
[summary.json](prefault/summary.json)에는 실제 SPTE 수, 명령 시간 증가의 중앙값·p95·최대,
이동 뒤 명령 순번별 fault, 전체 계수 구간 합, native direct 정지 시간도 있다.
`command_extra_s_*`는 명령 구간의 증가다. `step_extra_s_*`는 앞선 이동도 포함한다.

모든 유효 run의 guest CPU flag와 실행 입력 해시는 각각 하나다.
유효 궤적 180회의 QEMU 식별자 360개가 실제로 종료됐고 backing도 없어졌다.
`r14` 실행 포인터 395개에 기록된 QEMU 식별자 790개도 종료 상태를 재확인했다.
시작 전 node 0 여유의 유효 run 최솟값은 25,053 MiB였다. 요구값은 24,640 MiB다.

분석 재실행은 아래와 같다. 실패한 source suite에서는 통과한 앞부분만 선택하고 제외 내역을 기록한다.

```sh
python3 docs/results/trajectory-faults-20261116/analyze.py \
  implementation/virtual_machine/.local/run14/prefcount2/report.json \
  implementation/virtual_machine/.local/run14/prefcount4/report.json \
  implementation/virtual_machine/.local/run14/preftime1/report.json \
  --physical implementation/virtual_machine/.local/run14/physical3/physical.json \
  --output OUTPUT
```
