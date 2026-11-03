# 동기 step 재생 v2와 image pool 확장 설계 (2026-10-29)

목적: `sandbox.py pilot --sync-steps`의 동기 재생을 원본 학습 job(sweagent/iter2-rl-rollouts)의 step 구조에 맞춘다. 같은 작업에서 image pool을 46개에서 수백 개로 늘린다. 이 설계는 새 story의 두 주장을 재기 위한 것이다.

- **C1**: 꼬리 rollout이 다른 작업(BE)과의 간섭으로 더 느려진다.
- **C2**: image locality와 부하 분산이 충돌한다.

숫자는 2026-10-29에 `index.jsonl`(rollout 23,139개)에서 계산했다. 9절의 1~4단계를 같은 날 구현했고(10절), 5단계 검증 결과는 11절에 있다.

## 0. 요약

| 항목 | 지금 | v2 |
|---|---|---|
| step 구성 | 192개(24 group)가 한꺼번에 시작 | **wave 2개**. 두 번째 wave는 step 길이의 약 20% 뒤에 시작 |
| 시작 퍼짐 | jitter 0~2초 | **rollout마다 기록된 env 생성 시간**만큼 늦게 부팅 |
| step 사이 | 간격 0 | **idle**(학습 구간). 기본은 step 길이의 약 30% |
| trace와 image | 56 group을 돌려 씀. step 3부터 88% 재사용 | **매 step 새 group과 새 image**. 원본 step 순서를 따름 |
| 배치 | sync에서는 pack/spread만 | sync에서도 home/least(+H) |
| image pool | 46개, 디스크에 상시 보관 | 실측용은 **segment(2 step) 단위로 받고 지움**. simulator용은 image마다 받고, 단독 재생하고, 지우는 streaming |

## 1. 원본 job의 step 구조

계산 방법:
- 실행 시작(launch) = `first_ts − env_creation_s`.
- launch 간격이 60초를 넘으면 wave를 나눈다.
- 앞 wave가 끝나기 전에 시작한 wave는 같은 step에 넣는다.
- rollout 32개 미만의 작은 wave(run별 235개, 542개, 전체의 약 3%)는 따로 다시 띄운 것으로 보고 뺐다.

| 항목 | run 1 (24시간) | run 2 (50시간) |
|---|---|---|
| step 수 | 19 | 34 |
| step당 wave | 2개 (18 step), 3개 (1 step) | 2개 (30), 1개 (3), 3개 (1) |
| wave 크기 | 중앙값 223개 (32 task × 8 = 256 명목) | 223개 |
| wave 안 launch 퍼짐 | p50 2초, p90 45초 | p50 3초, p90 57초 |
| 두 번째 wave의 시작 | 첫 wave 뒤 p50 9.1분 (p10 6.8, p90 14.0) | 15.5분 (9.0, 22.7) |
| step 길이 | p50 54분 | 64분 |
| step 사이 idle | p50 17.8분 (p10 13.9, p90 55.5) | 19.5분 (16.2, 62.2) |
| step당 rollout / image | 436 / 64 | 444 / 64 |
| 앞 step들에서 본 image의 비율 | 중앙값 0 | 0.11 |
| env 생성 시간 | p50 33초, p90 약 260초 | p50 104초, p90 약 270~300초 |
| 같은 image의 8개 사이 env 생성 시간 차 | p50 91초 | 84초 |
| p90 이후 꼬리 / step | 중앙값 0.11 | 0.09 |
| 중간 rollout 종료 시각 / step | 0.60 | 0.64 |
| 학습 후반 turn 수 | 95 → 107 | 104 → 120 |

해석:
- step 하나는 task 64개 × 8이고, 두 wave로 나뉘어 시작한다. step 사이에는 학습으로 보이는 idle이 있다.
- env 생성 시간은 image 계열과 거의 무관하다. 같은 image의 8개 사이에서도 80~90초 차이가 난다. 따라서 image pull보다는 sandbox service의 대기열이나 생성 경합이 주원인일 수 있다. 이 시간을 우리 image 읽기 비용의 근거로 쓰지 않는다.
- wave 둘의 의미(batch를 두 번에 나눠 띄움 / 한 step 앞서 돌리는 pipeline)는 데이터로 정하지 못했다. 재생에서는 관측된 시각 구조만 따른다.

## 2. 하드웨어에 맞춘 축소

노드당 밀도(15코어에 96개, 코어당 6.4)는 지금 실험과 같게 둔다. step 크기는 wave 비율을 지키며 줄인다.

| 구성 | 노드 | 동시 최대 | wave당 group | step당 image |
|---|---|---|---|---|
| 한 서버 (bud05 NUMA 0) | 15코어 × 2 | 192 | 12 (96개) | 24 |
| 두 서버 (bud04 + bud05) | 15코어 × 4 | 384 | 24 (192개) | 48 |

- **group 고르기**: 원본의 연속된 step을 그대로 따른다. step k의 wave마다 완전한 8개 group 중에서 필요한 수(12 또는 24)를 계열 비율대로 무작위로 뽑는다. 이렇게 하면 step 순서(학습 진행)와 step 안의 계열 구성이 유지된다. 같은 group은 한 계획 안에서 한 번만 쓴다.
- **시간 축소**: 지금 LLM 배율 ×0.25에서 step이 약 14분이라 원본 시각을 그대로 쓸 수 없다.
  - 두 번째 wave 지연과 idle은 원본의 step 대비 비율로 둔다. 기본값은 wave 지연 0.2, idle 0.3이다.
  - 실제 값은 계획을 만들 때 그 step의 예상 길이(단독 실행 기준)로 계산한다.
  - 절대 시간(원본 그대로)은 민감도 확인용으로 둔다.
- env 생성 시간은 배율 없이 rollout마다 기록된 값을 쓴다. 옵션으로 0, 또는 배율을 둔다.

## 3. 재생기 변경 (`sandbox.py`)

### 3.1 step 계획 파일

`rl_rollouts.py plan`(새 subcommand)이 seed로 계획을 한 번 만든다. 모든 조건이 같은 파일을 읽으므로 조건끼리 정확히 짝이 맞는다.

```json
{"source_run": "...", "seed": "...", "scale": {"wave_offset_frac": 0.2, "idle_frac": 0.3},
 "steps": [{"orig_step": 3,
            "waves": [{"offset_s": 0, "groups": ["<group id>", ...], "env_delay_s": {"<trace>": 31.5, ...}},
                      {"offset_s": 170, "groups": [...], "env_delay_s": {...}}],
            "idle_after_s": 250, "images": ["..."]}]}
```

### 3.2 `sync_loop`를 wave scheduler로

- `--sync-plan <file>`을 받으면 `--sync-steps`의 무작위 group 뽑기 대신 계획을 따른다. 기존 동작은 그대로 둔다.
- step 시작 전에 두 wave 모두의 disk를 준비한다(지금처럼 측정 밖).
- wave w는 step 시작 + `offset_s`에 launch한다. member는 launch + `env_delay_s`에 VM을 부팅한다.
- step은 모든 wave의 모든 member가 끝나야 끝난다. 그 뒤 `idle_after_s` 동안 쉰다. LS/BE 실험에서는 이 구간에 BE를 넣는다(이번 범위 밖, 연결 지점만 둔다).
- **배치**: `group_loop`의 home/least 코드(`home_node`, `live_count`, `free_slots`)를 함수로 빼서 wave launch 때 group마다 호출한다. +H는 기존 `--move handoff`를 그대로 쓴다.
- **기록**: `steps.jsonl`의 step 행에 wave별 launch 시각, member별 env 지연, image 목록, 계획의 `orig_step`을 더한다.
- **LLM 시간**: 검증 실행에서는 기록된 대기를 쓰는 선택지(`LLM_MODEL` 없음, 이미 있음)를 쓴다.

### 3.3 분석 (`sync_steps.py`)

- wave별 꼬리, step 안 정규화 시각의 동시 실행 수 곡선을 더한다.
- step별 cold image 읽기 양(guest base disk 읽기 sector, `--mem-sample-s`), 부팅 뒤 첫 명령 지연, host image cache 시계열(`IMAGE_CACHE_LOG`)을 더한다.
- segment 안 step 순서(첫 step 대 나머지)별로 나눠 누적 효과를 본다.

## 4. image pool: 실측용 (segment 방식)

### 4.1 디스크 예산

| 서버 | `/Serverless` 여유 | 기존 image 46개 | 둘을 합쳐 쓸 수 있는 양 | image 수 (평균 2.57 GiB) |
|---|---|---|---|---|
| bud05 | 71 GiB | 119 GiB | 약 190 GiB | 약 70 |
| bud04 | 99 GiB | 같은 46개 | 약 218 GiB | 약 80 |

- 실측용으로 step 8개 × 24 = 192 image(한 서버)가 필요하다. 한꺼번에 둘 수 없다.
- 측정 중 image를 받아 만드는 것은 안 된다. build가 CPU와 NVMe를 쓰고, harness CPU는 0-1뿐이며, NUMA 1은 쓰지 않는다.
- **기존 46개를 지워야 공간이 난다.** 지금 결과의 재현에 필요한 image다. 지우기 전에 사용자 확인이 필요하다.

### 4.2 segment 순서

```
for segment in 계획을 2 step씩 자른 것:
    image 48개 받기·만들기 (NUMA 0 코어 2-31, 측정 없음, --jobs 4~8)
    for condition in 조건 목록:          # 같은 segment를 모든 조건이 쓴다
        image page cache만 비우기 (CACHE_RESET=images)
        2 step 재생 (wave, env 지연, idle 포함)
    image 48개 지우기 (digest는 image.json에 남음)
```

- build 비용은 segment마다 한 번이고, 조건 수와 무관하다. 지난 build는 46개에 110분(4 병렬)이 걸렸다. 그래서 48개에 약 2시간, 192개에 약 8시간이다.
- segment 경계에서 끊기는 것은 두 step을 넘는 누적 효과(앞 image의 page cache, 메모리 단편화)뿐이다. step 사이는 원래 barrier와 idle로 끊긴다.
- **시간 예산** (×0.25, 한 서버): step 약 14분 + wave 지연 + idle로 2 step에 약 40분이다. 조건 5개(least, home, least+H, home+H, POOL)면 segment당 약 3.5시간이다. segment 4개면 build를 포함해 약 22시간이다.
- **같은 image를 다시 만들 수 있게**: 지금 build는 tag로 받고 digest를 기록한다. `registry_rootfs`는 `repo@sha256:...` 참조를 해석하지 못한다(`:`로 tag를 자름). digest로 다시 받는 경로를 고쳐야 한다.

## 5. image pool: simulator용 (streaming)

C2의 결론은 image 종류 수에 묶여 있다(46개에서 P1 cache가 포화된다). simulator 입력을 수백~수천 image로 늘린다.

- 완전한 8개 group이 있는 image는 1,673개다(group 2,065개).
- image마다 다음을 차례로 하고 image는 지운다.
  1. 받기·만들기
  2. image page cache 비우기
  3. 그 image의 group 전부를 단독 재생 (`solo`, LLM 대기 없음)
  4. `image_blocks.py snapshot` (그 image만)
  5. 지우기
- 남는 것: image별 working set block hash, 단독 실행 요약(`solo-summary.csv` 행), image 크기.
- 처리량은 다운로드가 정한다(지난 build 기준 image당 약 2.4분, 4 병렬). 500개에 약 20시간, 1,673개에 약 70시간이다. 단독 재생은 440개 rollout이 24코어에서 약 16분이라 작다.
- **검증**: 지금 working set은 동시 실행 중 21번 찍은 snapshot의 합집합이다. 단독 재생 후 snapshot과 다를 수 있으므로, 기존 46개에서 두 방식을 먼저 비교한다.
- **`fleet.py` 변경**:
  - sync mode에 wave 2개, wave 지연, idle, env 지연을 넣는다.
  - step마다 원본 순서대로 새 group을 뽑는다(같은 run 안에서 재사용 없음).
  - 원래 크기(wave당 32 group, step당 image 64개)를 그대로 쓴다.
  - group 사이 image 재사용은 원본 비율(0~11%)만 남는다.

## 6. 검증 (재생이 원본을 따르는가)

1. **×1.0, 기록된 LLM 대기, 기록된 env 지연, 원본 wave 지연과 idle**(축소 없음). 한 서버 192개로 step 2개를 돌린다(약 2시간 + idle).
2. 원본과 비교한다.
   - step 안 정규화 시각의 동시 실행 수 곡선
   - 중간 rollout 종료 / step (원본 0.60~0.64)
   - p90 이후 꼬리 / step (0.09~0.11)
   - 두 번째 wave가 첫 wave의 꼬리와 겹치는 시간
3. 맞으면 ×0.25(축소 규칙 적용)로 C2 matrix를 돌린다. 맞지 않으면 어긋난 항목을 결과 문서에 적고 계속할지 정한다.

## 7. 이 재생으로 할 실험

| 실험 | 조건 | 새로 보는 것 |
|---|---|---|
| C2 실측 (중단된 실험 3의 대체) | least / home / least+H / home+H / POOL, ×0.25, 한 서버 | 매 step 차가운 image에서의 host image cache, cold 읽기 양, 첫 명령 지연, CPU 대기 |
| C2 simulator | 노드 16~64대, image pool 500+ | 포화가 풀린 상태에서 P1 대 P5+H의 cache·fetch |
| C1 준비 | 같은 계획에 idle 구간 BE를 추가(다음 설계) | 다음 step 시작 때 BE를 비켜야 하는 상황, wave 겹침에서 꼬리 간섭 |

## 8. 한계와 열린 질문

- env 생성 시간의 원인(image pull 대 대기열)을 모른다. 그래서 VM 부팅 전 지연으로만 넣는다. 원격 image 읽기 비용은 따로 흉내 낼 수 있다(예: sandbox cgroup의 `io.max`로 base image 읽기 대역폭 제한). 이번 범위에는 넣지 않았다.
- 원본의 노드 배치(어느 rollout이 어느 노드에 있었나)는 데이터에 없다.
- step 길이 비율로 줄인 wave 지연과 idle은 가정이다. 절대값 sweep으로 민감도를 본다.
- 실측은 segment 경계 때문에 2 step을 넘는 누적 효과를 재지 못한다.
- 작은 wave(다시 띄운 rollout, 약 3%)는 재생하지 않는다.

## 9. 작업 순서

1. `rl_rollouts.py plan`: 원본 step 복원(1절 방법), 축소 규칙, 계획 파일
2. `sandbox.py`: `--sync-plan` wave scheduler, sync 배치(home/least), 기록 확장
3. `registry_rootfs`의 digest 참조 지원, segment driver(`run_rl_matrix_paired.sh` 확장 또는 새 driver)
4. 기존 46개로 짧은 smoke(계획 1 step, image 재사용 허용)
5. **사용자 확인 뒤** 기존 image 정리, 6절 검증 실행
6. streaming pool 수집(5절), `fleet.py` sync mode 확장
7. C2 실측 matrix와 simulator

## 10. 구현 (2026-10-29)

| 단계 | 파일 | 내용 |
|---|---|---|
| 1 | `rl_rollouts.py plan` | 1절의 방법(`job_steps`)으로 원본 step을 복원한다. wave가 정확히 2개인 step만 쓴다. wave마다 완전한 group을 계열 비율대로 고르고, 모자라면 다음 step의 같은 wave에서 채운다. member 지연은 (wave 첫 launch부터 자기 launch까지) + `--env-scale` × env 생성 시간이다. `--timing scaled`(기본, `--step-estimate-s` 840초 × 0.2 / 0.3) 또는 `orig`. `--pool selection`은 기존 image 46개로 smoke를 돌리는 용도다. |
| 1 | `rl_rollouts.py convert --plan` | 계획의 group trace를 변환하고 `candidates-plan.json`을 쓴다. group id는 `groups.json`의 번호(`G0123`)다. |
| 2 | `sandbox.py pilot --sync-plan P --plan-steps a-b` | step마다 disk를 모두 준비한 뒤 wave를 offset에 launch하고, member를 지연 뒤에 부팅한다. 배치는 wave launch 때 group 단위로 정한다(home/least, spread는 member 단위). network 식별자는 step 안 번호로 미리 정한다(`ctv<n>`, 10.200.102+). 마지막 step 뒤에는 idle을 두지 않는다. `steps.jsonl`에 wave launch 시각, member별 wave·지연·node·vm을 기록한다. |
| 3 | `sandbox.py build` | `repo@sha256:` 참조를 해석한다. 첫 build의 digest를 `image-pins.json`에 남기고, 다음 build는 그 digest로 받는다(`--unpinned`로 끔). `--plan/--plan-steps`로 segment의 image만 만든다. 기존 45개 image의 digest를 미리 기록했다(46개 중 narwhals-828은 build된 적이 없다). |
| 3 | `sandbox.py drop-images` | segment image를 지운다. `candidates-rl.json`의 image와 pin이 없는 image는 지우지 않는다. `--dry-run`이 있다. |
| 3 | `run_plan_segments.sh` | segment마다 build → solo(`SOLO`가 없으면) → 조건별 실행(image cache만 비움) → `DROP_IMAGES=1`이면 삭제. NUMA 0만 쓴다. |
| 2 | `sync_steps.py` | plan step의 기대 종료를 wave offset + 지연 + solo 시간으로 계산한다. wave별 꼬리, 정규화 시각 20칸의 live rollout 수, 부팅·첫 명령 지연, base disk 읽기 양을 더했다. `--solo`를 여러 번 줄 수 있다. 기존 sync 실행에서는 결과가 이전과 같다(바이트 단위로 확인). |

- digest 경로는 가장 작은 image를 pin으로 다시 받아 확인했다(같은 manifest digest가 나옴).
- 원본 50시간 run에서 step 2부터 8 step을 고르면 orig step 5, 6, 7, 9~13이 나온다(`.local/plan-c2-1srv-v2.json`). step 3, 4는 wave가 2개가 아니고, step 2, 8은 두 번째 wave에 완전한 group이 없어 뺐다(11절). image는 192개, member 지연은 p50 105초, p90 256초, 최대 371초다.
- 처음 계획(`plan-c2-1srv.json`, orig step 2, 5~11)은 11절 검증에 썼다.

## 11. 검증 결과 (2026-10-29, bud05)

6절 1번 조건(×1.0, 기록된 LLM 대기, 원본 wave 지연과 idle)으로 첫 계획의 segment 0(orig step 2, 5)을 돌렸다. 노드당 96개, least 배치, 이동 없음. 384개 모두 끝났다. 요약은 [measured/plan-validate-sync-steps.json](results/virtual-node-rl-20261026/measured/plan-validate-sync-steps.json)에 있다.

비교 기준은 두 가지다.
- **같은 rollout의 원본 시각**: 계획한 member마다 wave offset + 지연 + 원본 기록 시간창(`last_ts − first_ts`)으로 끝나는 시각. 재생기가 계획을 제대로 따르는지 본다.
- **원본 step 전체**: 원본 step의 모든 rollout(축소 전). 축소한 계획이 원본 step의 모양을 따르는지 본다. live 수는 192/n으로 맞췄다.

| 항목 | step 0 재생 | 같은 rollout 원본 | 원본 step 2 전체 | step 1 재생 | 같은 rollout 원본 | 원본 step 5 전체 |
|---|---|---|---|---|---|---|
| step 길이 | 4,148초 | 4,228초 | 2,542초 | 3,824초 | 3,830초 | 3,856초 |
| 중간 rollout 종료 / step | 0.415 | 0.414 | 0.439 | 0.669 | 0.680 | 0.674 |
| p90 이후 꼬리 / step | 0.093 | 0.096 | 0.156 | 0.092 | 0.077 | 0.081 |
| wave 2 시작 ~ wave 1 끝 겹침 | 207초 (0.05) | 172초 (0.04) | – | 2,270초 (0.59) | 2,283초 (0.60) | – |

정규화 시각 20칸의 live rollout 수:

| | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| step 1 재생 | 4 | 96 | 92 | 77 | 80 | 154 | 149 | 144 | 140 | 136 | 133 | 120 | 102 | 94 | 76 | 67 | 46 | 31 | 18 | 2 |
| 원본 step 5 전체 | 7 | 93 | 86 | 75 | 93 | 159 | 156 | 148 | 140 | 134 | 124 | 115 | 106 | 95 | 82 | 72 | 47 | 35 | 18 | 3 |

- **재생기는 계획을 따른다.** member마다 (재생 실행 시간) / (원본 기록 시간창)은 p10 0.93~0.96, 중앙값 0.97~0.98, p90 1.00이다. step 길이 차는 2% 이내, live 곡선 차는 칸마다 6개 이내다. 재생이 약 3% 짧은 이유는 명령 실행이 원본 환경보다 빠르기 때문으로 보인다(LLM 대기는 기록값 그대로).
- **step 1은 원본 step 5 전체와 맞는다.** 중간 종료 0.669 대 0.674, 꼬리 0.092 대 0.081이다. wave 2가 wave 1 꼬리와 step의 60% 동안 겹치는 구조도 같다.
- 부팅은 p50 0.41초, 첫 명령은 p50 0.03초로 member 시작 지연(p50 약 100초)에 비해 무시할 만하다. CPU 사용률은 5~8%, 대기/실행은 2%다(×1.0이라 contention이 거의 없다).
- **step 0은 원본 step 2와 다르다(1.6배 길다).** 원인은 계획이다. 원본 step 2의 두 번째 wave는 91개(task 25개, task당 평균 3.6개)만 데이터에 있고 완전한 group이 없다. 계획은 이 wave를 step 5의 두 번째 wave로 채웠는데, 그 rollout은 시간창 중앙값이 2,247초로 step 2의 413초보다 훨씬 길다.
  - 이런 wave는 다시 띄운 rollout이 아니다. 첫 wave와 task가 거의 겹치지 않는다(25개 중 1개). 정상 wave인데 rollout 일부만 데이터에 남은 것으로 보인다.
  - 50시간 run에서 두 번째 wave의 완전한 group이 1개 이하인 step은 1, 2, 8, 28이다.
- **수정**: `plan`은 이제 wave마다 자기 완전한 group이 `--min-own-groups`(기본 groups-per-wave / 2 = 6)개 미만이면 그 step을 건너뛴다. 새 계획은 orig step 5, 6, 7, 9~13이다. segment 0(step 5, 6)의 image 48개 중 19개가 이미 있다.
- 판정: 재생 방식은 원본 step의 시각 구조를 재현한다. 다음은 새 계획으로 ×0.25 C2 matrix다.


## 12. C2 실측 (×0.25, 중단)

새 계획(`plan-c2-1srv-v2.json`)을 segment 4개(각 2 step)로 돌린다. 조건은 다섯 개이고 모두 노드당 96개, LLM은 token 모델 ×0.25다. 조건 사이에는 image page cache만 비운다. driver 로그는 `/Serverless/agent-sandbox-cxl/logs/plan-c2.log`다.

segment 0 (orig step 5, 6, 2026-10-30, bud05):

| 조건 | step 시간 (초) | step 느려짐 (clean) | 대기/실행 | 사용률 | 이동 |
|---|---|---|---|---|---|
| least | 1,071 / 1,332 | 1.022 / 1.060 | 3.0% | 21% / 27% | – |
| home | 1,072 / 1,374 | 1.023 / 1.053 | 3.0% | 21% / 27% | – |
| least+H | 1,070 / 1,400 | 1.021 / 1.059 | 2.5% | 21% / 26% | 164 |
| home+H | 1,071 / 1,381 | 1.023 / 1.044 | 2.5% | 21% / 26% | 170 |
| POOL | 1,069 / 1,381 | 1.021 / 1.038 | 2.3% | 21% / 28% | – |

- 384개가 모든 조건에서 끝났다. 첫 명령 지연은 p50 0.05~0.06초다.
- hand-off는 대기를 약 18%, POOL은 약 23% 줄였다. step 시간 차이는 3% 이내이고, 조건 사이 순서와 같은 크기의 잡음과 구분되지 않는다.
- home은 step 0의 wave 2에서 노드 1에 80개, 노드 0에 16개를 놓았다(least는 48 / 48). 그래도 대기는 least와 같다. ×0.25에서 사용률이 21~28%라 한쪽에 몰려도 코어가 남는다.
- guest의 base image 읽기는 조건과 무관하게 step마다 13.5 / 21.6 GiB다. 두 노드가 한 host의 page cache를 공유하므로 home과 least의 image locality 차이는 이 실측에서 host 읽기 양으로 나타나지 않는다. locality 효과는 노드마다 cache가 따로 있는 simulator(7절 C2 simulator)에서 봐야 한다.
- 디스크: 실행 중 VM의 overlay(`upper.ext4`)가 최대 약 40 GiB까지 커진다. segment 0 동안 여유가 최저 12 GiB였다.

segment 1 (orig step 7, 9, 2026-10-30):

| 조건 | step 시간 (초) | step 느려짐 (clean) | 대기/실행 | 사용률 | 이동 |
|---|---|---|---|---|---|
| least | 933 / 1,262 | 0.995 / 1.008 | 2.6% | 28% / 20% | – |
| home | 934 / 1,286 | 0.995 / 1.008 | 2.8% | 27% / 19% | – |
| least+H | 926 / 1,271 | 0.987 / 1.008 | 2.5% | 29% / 20% | 182 |
| home+H | 950 / 1,262 | 1.012 / 1.007 | 2.6% | 27% / 20% | 150 |

- segment 1에서는 조건 사이 차이가 segment 0보다 더 작다(대기/실행 2.5~2.8%).
- **2026-10-30 14:47Z에 서버 종료 요청으로 중단했다.** segment 1의 POOL은 시작 24분 만에 멈췄다(`20261030T142350Z-...-seg1-5z_9d_x0`, `condition.txt`에 ABORTED 표시, 쓰지 않는다). segment 2, 3은 돌리지 않았다.
- segment 1 image 48개는 지우지 않았다(`.local/images`). 이어서 하려면 segment 1 POOL만 다시 돌린 뒤 `SEGMENTS="2 3"`으로 driver를 다시 시작한다. segment 1 solo는 `.local/runs/20261030T112821Z-solo-7uja8grg`다. POOL 재실행에는 `SEGMENTS=1 SOLO=<그 경로>`로 조건 하나만 주고, segment 2, 3에는 `SOLO`를 주지 않는다(segment마다 solo가 다르다).

