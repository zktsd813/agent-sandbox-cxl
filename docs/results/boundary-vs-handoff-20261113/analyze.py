#!/usr/bin/env python3
"""Aggregate paired seeds, interpolate SLO on actual utilization, and export the complete comparison tables."""
import argparse
import csv
import json
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
RAW = HERE.parents[2] / "experiments/contention/.local/boundary"
METRICS = ["move_cpu_over_run", "paused_work_over_run", "cpu_weighted_slowdown", "heavy_command_slowdown_p99", "lost_over_run", "run_cores_per_node",
           "bnd_moves", "reactive_moves", "bnd_blocked", "command_time_ratio", "command_slowdown_p50",
           "command_slowdown_p90", "command_slowdown_p99", "heavy_command_slowdown_p50", "heavy_command_slowdown_p90",
           "trace_slowdown_p50", "trace_slowdown_p99", "step_slowdown_p50", "step_slowdown_p99",
           "commands_finished", "commands_timed_out", "command_cpu_s", "command_solo_steps", "command_elapsed_steps",
           "sandbox_per_core", "gib_per_core", "tool_time_ratio",
           "initial_unfit_loss_cpu_s", "later_arrival_loss_cpu_s"]
SLOS = [("cpu_weighted_slowdown", 1.05), ("cpu_weighted_slowdown", 1.10),
        ("heavy_command_slowdown_p99", 1.5), ("heavy_command_slowdown_p99", 2.0)]


def mean(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return statistics.mean(values) if values else None


def slo_limit(points, metric, limit):
    """Maximum feasible x on the piecewise-linear measured curve. Never extrapolate past endpoints."""
    points = sorted((r["actual_util"], r[metric]) for r in points if r.get(metric) is not None)
    if not points:
        return dict(util=None, status="missing")
    feasible = [x for x, y in points if y <= limit]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if (y0 - limit) * (y1 - limit) < 0:
            feasible.append(x0 + (limit - y0) * (x1 - x0) / (y1 - y0))
    if not feasible:
        return dict(util=None, status="below_grid", lowest_measured_util=points[0][0])
    return dict(util=max(feasible), status="at_least" if points[-1][1] <= limit else "interpolated",
                lowest_measured_util=points[0][0], highest_measured_util=points[-1][0])


def table(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as out:
        writer = csv.DictWriter(out, keys, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def number(value):
    return "—" if value is None else f"{value:.4g}" if isinstance(value, float) else str(value)


def markdown_table(rows, columns):
    head = "| " + " | ".join(columns) + " |\n|" + "|".join("---" for _ in columns) + "|\n"
    return head + "".join("| " + " | ".join(number(r.get(c)) for c in columns) + " |\n" for r in rows)


def rl_width_variability(metadata):
    """Read the same eligible solo programs, without executing guests or importing old checkout code."""
    import sys
    import numpy as np
    sys.path.insert(0, str(HERE.parents[2] / "experiments/contention"))
    import fleet
    sources = {w:source for m in metadata for w,source in m.get("sources",{}).items() if w in ("rl1","rl8")}
    result = {}
    for workload,source in sources.items():
        programs = fleet.load_programs(source["solo"], source["candidates"])
        peak_ratio,cv = [],[]
        for info in programs.values():
            for kind,value in info["program"][1:]:  # first segment is boot
                if kind == "cpu" and value.sum()*fleet.DT >= 1.0:
                    peak_ratio.append(float(np.percentile(value,90)/value.mean()))
                    cv.append(float(value.std()/value.mean()))
        result[workload] = {"traces":len(programs),"heavy_commands":len(cv),
            "p90_over_mean":dict(zip(("p10","p50","p90","p99"),np.percentile(peak_ratio,[10,50,90,99]).tolist())),
            "cv":dict(zip(("p10","p50","p90","p99"),np.percentile(cv,[10,50,90,99]).tolist()))}
    return result


def write_readme(summary):
    rows = summary["results"]
    primary_rl = [r for r in rows if r["priority"] == 3 and r["ramp_s"] == 600 and r["duration_s"] == (900.0 if r["workload"] == "rl1" else 300.0)]
    main = [r for r in rows if r["priority"] == 1 and r["timeout_s"] > 60]
    def get(source, **match):
        return next((r for r in source if all(r.get(k) == v for k, v in match.items())), {})
    def f(value, digits=3):
        return "—" if value is None else f"{value:.{digits}f}"
    def metric(cores, util, variant, key):
        return get(main, cores=cores, target_util=util, variant=variant).get(key)
    selected = ["none", "pool", "bnd-oracle-1ms", "bnd-class-1ms", "reactive", "bnd-oracle-1ms+reactive"]
    metrics = []
    for cores in (48, 96, 192):
        for variant in selected:
            r = get(main, cores=cores, target_util=80, variant=variant)
            metrics.append({"코어":cores, "정책":variant, "실제 U %":f(r.get("actual_util",0)*100,1),
                "CPU 가중":f(r.get("cpu_weighted_slowdown")), "heavy p99":f(r.get("heavy_command_slowdown_p99")),
                "잃은 CPU %":f(r.get("lost_pct"),2), "경계 이동/s":f(r.get("bnd_moves_per_s"),2),
                "reactive 이동/s":f(r.get("reactive_moves_per_s"),2)})
    recovery = []
    recovered = [r for r in summary["recovered"] if r["priority"] == 1 and r["timeout_s"] > 60]
    for cores in (48, 96, 192):
        for util in (80, 90):
            r = {"코어":cores, "목표 U %":util}
            for variant in ("bnd-oracle-1ms", "bnd-class-1ms", "reactive", "bnd-oracle-1ms+reactive"):
                p = get(recovered, cores=cores, target_util=util, variant=variant)
                a,b = p.get("cpu_weighted_slowdown_pool_recovered"),p.get("heavy_command_slowdown_p99_pool_recovered")
                r[variant] = f"{f(a*100 if a is not None else None,1)} / {f(b*100 if b is not None else None,1)}"
            recovery.append(r)
    slos = [r for r in summary["slos"] if r["priority"] == 1 and r["timeout_s"] > 60]
    slo_rows = []
    for key,limit in SLOS:
        for cores in (48,96,192):
            r = {"SLO":("CPU 가중" if key.startswith("cpu") else "heavy p99")+f" ≤ {limit:g}", "코어":cores}
            for variant in selected:
                x=get(slos,cores=cores,metric=key,slo=limit,variant=variant)
                u=x.get("util")
                if u is None:
                    cell="구간 내 없음"
                else:
                    prefix="≥" if x["status"]=="at_least" else ""
                    cell=prefix+f(u*100,1)+"%"
                    ratio=x.get("nodes_over_none")
                    if ratio is not None:
                        cell+=" / "+("≤" if x.get("node_ratio_status")=="upper_bound" else "")+f(ratio,3)
                r[variant]=cell
            slo_rows.append(r)
    s48 = {v:get(slos,cores=48,metric="cpu_weighted_slowdown",slo=1.10,variant=v) for v in selected}
    s192 = {v:get(slos,cores=192,metric="cpu_weighted_slowdown",slo=1.10,variant=v) for v in selected}
    incremental48 = (1 - s48["bnd-oracle-1ms+reactive"]["nodes_over_none"] / s48["bnd-oracle-1ms"]["nodes_over_none"]) * 100
    rec80 = [get(recovered,cores=c,target_util=80,variant="bnd-oracle-1ms")["cpu_weighted_slowdown_pool_recovered"] for c in (48,96,192)]
    class80 = [get(recovered,cores=c,target_util=80,variant="bnd-class-1ms")["cpu_weighted_slowdown_pool_recovered"] for c in (48,96,192)]
    added80 = [get(recovered,cores=c,target_util=80,variant="bnd-oracle-1ms+reactive")["cpu_weighted_slowdown_pool_recovered"]-x for c,x in zip((48,96,192),rec80)]
    costs=[]
    for cores in (48,96,192):
        for latency in ("1ms","10ms","100ms","1s"):
            r=get(main,cores=cores,target_util=90,variant="bnd-oracle-"+latency)
            costs.append({"코어":cores,"L":latency,"CPU 가중":f(r.get("cpu_weighted_slowdown")),"heavy p99":f(r.get("heavy_command_slowdown_p99"))})
    sensitivity=[]
    for util in (80,90):
        for variant in ("bnd-oracle-1ms","bnd-oracle-1ms-view1","bnd-oracle-1ms-peak","bnd-oracle-1ms-k2"):
            r=get(main,cores=192,target_util=util,variant=variant)
            sensitivity.append({"목표 U %":util,"정책":variant,"CPU 가중":f(r.get("cpu_weighted_slowdown")),"heavy p99":f(r.get("heavy_command_slowdown_p99"))})
    causes=[]
    for r in summary["causes"]:
        if r["priority"]==1 and r["timeout_s"]>60 and r["target_util"] in (80,90):
            causes.append({"코어":r["cores"],"목표 U %":r["target_util"],"(a) 이후 명령 시작 관측 %":f(r["a_with_later_arrival_pct"],2),
                "(b) 맞는 노드 없음 %":f(r["b_no_fitting_node_pct"],2),"(c) light %":f(r["c_light_pct"],2),
                "잔차 %":f(r["residual_unattributed_pct"],2),"시작 여유 전체 %":f(r["a_start_fit_pct"],2)})
    predictor=next(p for m in summary["metadata"] for p in m.get("predictors",[]) if p["policy"]=="cap30")
    variability=[]
    for label,key in (("예측 폭 / 실제 평균 폭","width_predicted_over_actual"),("예측 폭 − 실제 평균 폭 (코어)","width_error_cores"),
                      ("heavy 수요 p90 / 평균","heavy_p90_over_mean"),("heavy 수요 CV","heavy_cv")):
        variability.append({"항목":label,**{q:f(predictor[key][q]) for q in ("p10","p50","p90","p99")}})
    for workload,stats in summary.get("rl_width_variability",{}).items():
        for label,key in (("수요 p90 / 평균","p90_over_mean"),("수요 CV","cv")):
            variability.append({"항목":workload+" heavy "+label,**{q:f(stats[key][q]) for q in ("p10","p50","p90","p99")}})
    density=[]
    for r in summary["density"]:
        if r["cores"]==192 and r["variant"]=="none" and r["ramp_s"]==600 and r["duration_s"]==(900.0 if r["workload"]=="rl1" else 300.0):
            density.append({"입력":r["workload"],"LLM gap 배수":r["gap_scale"],"목표 U %":r["target_util"],
                "실제 U %":f(r["actual_util"]*100,1),"sandbox/코어":f(r["sandbox_per_core"],2),"GiB/코어":f(r["gib_per_core"],2)})
    rl=[]
    for workload in ("rl1","rl8"):
        for gap in (1.0,.25):
            for cores in (48,96,192):
                a=get(primary_rl,priority=3,workload=workload,gap_scale=gap,cores=cores,target_util=90,variant="none")
                b=get(primary_rl,priority=3,workload=workload,gap_scale=gap,cores=cores,target_util=90,variant="reactive")
                c=get(primary_rl,priority=3,workload=workload,gap_scale=gap,cores=cores,target_util=90,variant="bnd-oracle-1ms")
                d=get(primary_rl,priority=3,workload=workload,gap_scale=gap,cores=cores,target_util=90,variant="pool")
                rl.append({"입력":workload,"gap":gap,"코어":cores,"none 실제 U %":f(a["actual_util"]*100 if a.get("actual_util") is not None else None,1),
                    "none CPU 가중":f(a.get("cpu_weighted_slowdown")),"pool CPU 가중":f(d.get("cpu_weighted_slowdown")),
                    "bnd1ms CPU 가중":f(c.get("cpu_weighted_slowdown")),"reactive CPU 가중":f(b.get("cpu_weighted_slowdown"))})
    horizon=[]
    for cores in (48,192):
        for duration in (900.0,4500.0):
            for variant in ("none","pool","bnd-oracle-100ms"):
                r=get(rows,priority=3,workload="rl1",gap_scale=1.0,cores=cores,target_util=90,variant=variant,duration_s=duration,ramp_s=600)
                if r:
                    horizon.append({"코어":cores,"측정 s":duration,"정책":variant,"실제 U %":f(r["actual_util"]*100,1),
                        "CPU 가중":f(r["cpu_weighted_slowdown"]),"heavy p99":f(r["heavy_command_slowdown_p99"])})
    warm=[]
    for r in rows:
        if r["priority"]==3 and r["ramp_s"]==3600:
            warm.append({"정책":r["variant"],"실제 U %":f(r["actual_util"]*100,1),
                "CPU 가중":f(r["cpu_weighted_slowdown"]),"heavy p99":f(r["heavy_command_slowdown_p99"])})
    steps=[]
    for r in rows:
        if r["priority"]==4:
            steps.append({"정책":r["variant"],"실제 U %":f(r["actual_util"]*100,1),"step p50":f(r["step_slowdown_p50"]),"step p99":f(r["step_slowdown_p99"])})
    timeout=[]
    for r in rows:
        if r["timeout_s"]==60 and r["variant"] in ("none","bnd-oracle-1ms","reactive","bnd-oracle-1ms+reactive"):
            timeout.append({"목표 U %":r["target_util"],"정책":r["variant"],"실제 U %":f(r["actual_util"]*100,1),
                "timeout 명령 %":f(r["timeout_pct"],3),"CPU 가중":f(r["cpu_weighted_slowdown"]),"heavy p99":f(r["heavy_command_slowdown_p99"])})
    def md(rs):
        return markdown_table(rs,list(rs[0])) if rs else "미완료: 실행된 seed 쌍 없음.\n"
    warm_section = ("초기조건 추가 검증: 같은 48코어·목표 90%·gap 1, 측정 900초에서 ramp를 600→3600초로 바꿨다. "
        "ramp는 sandbox 최초 출발의 분산 범위와 측정 시작을 함께 정한다. 단일 측정점이므로 최대 사용률은 외삽하지 않는다.\n\n"+md(warm)) if warm else ""
    warm_repro = '''# 초기조건 민감도 8설정.
run0 "$boundary_python" - <<'PYWARM'
import json
from pathlib import Path
root = Path("experiments/contention/.local/boundary")
configs = [dict(c, ramp_s=3600.0) for c in json.loads((root / "rl-grid.json").read_text())
           if c["workload"] == "rl1" and c["gap_scale"] == 1 and c["cores"] == 48 and c["target_util"] == 90
           and c["variant"] in ("none", "pool", "reactive", "bnd-oracle-1ms")]
(root / "warm-grid.json").write_text(json.dumps(configs, indent=1))
PYWARM
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/warm-grid.json" --output "$boundary_out/warm.jsonl" --jobs 8
''' if warm else ""
    rl_bnd90=get(primary_rl,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="bnd-oracle-1ms")
    rl_react90=get(primary_rl,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="reactive")
    rl_pool90=get(primary_rl,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="pool")
    rl_none90=get(primary_rl,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="none")
    long_none=get(rows,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="none",ramp_s=600,duration_s=4500.0)
    long_pool=get(rows,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="pool",ramp_s=600,duration_s=4500.0)
    warm_none=get(rows,priority=3,workload="rl1",gap_scale=1.0,cores=48,target_util=90,variant="none",ramp_s=3600,duration_s=900.0)
    warm_note = (f"ramp를 3,600초로 넓힌 추가 검증은 none U {f(warm_none['actual_util']*100,1)}%·CPU 가중 {f(warm_none['cpu_weighted_slowdown'])}로 초기조건에도 민감했다. "
                 if warm_none else "")
    quarter = {v:get(primary_rl,priority=3,workload="rl1",gap_scale=.25,cores=48,target_util=90,variant=v)
               for v in ("none","bnd-oracle-1ms","reactive")}
    report = f"""# 명령 경계 배치 대 실행 중 hand-off, 같은 tail의 사용률 (2026-11-13)

simulator 분석이다. 다언어 264 trace의 10 ms CPU profile과 기록된 LLM 간격을 재생했다. 실측 VM·lab·Docker를 새로 실행하지 않았다. 전체 수치는 [summary.json](summary.json), 모든 작업량·코어·사용률·변형의 표는 [tables.md](tables.md)에 있다.

## 0. 결론

- **경계 배치가 pool 이득의 대부분을 가져간다.** cap30·목표 80%에서 oracle 경계 1 ms의 CPU 가중 slowdown은 48/96/192코어 순으로 {metric(48,80,'bnd-oracle-1ms','cpu_weighted_slowdown'):.4f} / {metric(96,80,'bnd-oracle-1ms','cpu_weighted_slowdown'):.4f} / {metric(192,80,'bnd-oracle-1ms','cpu_weighted_slowdown'):.4f}다. pool 이득의 {min(rec80)*100:.1f}~{max(rec80)*100:.1f}%를 가져가고, reactive를 더하면 {min(added80)*100:.1f}~{max(added80)*100:.1f}%p를 추가로 가져간다.
- **작은 노드에서는 reactive가 최대 사용률을 더 올린다.** 48코어·CPU 가중 slowdown ≤ 1.10의 최대 실제 사용률은 none {s48["none"]["util"]*100:.1f}%, oracle 경계 {s48["bnd-oracle-1ms"]["util"]*100:.1f}%, reactive {s48["reactive"]["util"]*100:.1f}%, 결합 {s48["bnd-oracle-1ms+reactive"]["util"]*100:.1f}%다. none 대비 노드 절감은 경계 {s48["bnd-oracle-1ms"]["node_saving_pct"]:.1f}%, reactive {s48["reactive"]["node_saving_pct"]:.1f}%, 결합 {s48["bnd-oracle-1ms+reactive"]["node_saving_pct"]:.1f}%다. 경계만 쓸 때와 비교한 결합 정책의 추가 노드 절감은 {incremental48:.1f}%다.
- **192코어의 같은 SLO에서는 경계 배치와 reactive의 최대 사용률 차이가 아직 식별되지 않는다.** none은 {s192["none"]["util"]*100:.1f}%이고 두 정책은 모두 측정 상단 약 94%까지 통과했다. 이때 none 대비 노드 절감의 확인된 하한은 약 7%다. 측정 밖의 최대점은 외삽하지 않는다.
- **정지 시간이 길면 CPU 가중 평균과 tail의 판단이 갈린다.** 192코어·목표 90%의 경계 L=1 s는 CPU 가중 {metric(192,90,'bnd-oracle-1s','cpu_weighted_slowdown'):.3f}이지만 heavy p99 {metric(192,90,'bnd-oracle-1s','heavy_command_slowdown_p99'):.2f}로, none의 {metric(192,90,'none','heavy_command_slowdown_p99'):.2f}보다 나쁘다.
- **class 예측도 oracle에 가까운 회수율을 보인다.** 목표 80%에서 class 경계 1 ms는 pool의 CPU 가중 이득 {min(class80)*100:.1f}~{max(class80)*100:.1f}%를 가져간다. 명령 recall {predictor['recall']*100:.1f}%, precision {predictor['precision']*100:.1f}%, CPU 가중 recall {predictor['cpu_recall']*100:.1f}%다.
- **Python RL의 최대 사용률은 짧은 구간만으로 확정하지 않았다.** 1 vCPU·48코어·목표 90%·gap 1의 none은 측정 900초에서 실제 U {f(rl_none90.get('actual_util',0)*100,1)}%·CPU 가중 {f(rl_none90.get('cpu_weighted_slowdown'))}, 4,500초에서 U {f(long_none.get('actual_util',0)*100,1)}%·CPU 가중 {f(long_none.get('cpu_weighted_slowdown'))}다. 긴 구간은 이상적 pool도 {f(long_pool.get('cpu_weighted_slowdown'))}으로 지연이 남았다. {warm_note}목표 90%의 밀도 약 41 sandbox/코어·9.1 GiB/코어는 사용자 기준 bud05의 4 GB/코어도 넘는다.

## 1. 질문

1. 명령 시작 때 무거운 CPU를 예측해 한 번 옮기면, 실행 중 reactive hand-off와 이상적 공유의 이득을 얼마나 대체하는가?
2. CPU 가중 slowdown ≤ 1.05/1.10, heavy 명령 p99 ≤ 1.5/2.0을 유지하는 최대 실제 사용률과 필요한 노드 수는 얼마인가?
3. 경계 배치 뒤 남는 손실은 시작 때의 자리 부족, 이후의 겹침, 가벼운 명령 중 어디에 있는가?

## 2. 방법

| 항목 | 설정 |
|---|---|
| simulator | [`fleet.py`](../../../experiments/contention/fleet.py)의 fluid thread water-filling, carry=True, kernel_wait 없음. 기본 경계 배치는 꺼져 있고 기존 지표를 유지한다 |
| 다언어 입력 | 10-14 Docker DRAM 264 trace / 13,600 명령. 현재 checkout에 원본이 없어 옛 checkout을 읽기 전용으로 사용. [`cap_sweep.py`](../../../experiments/contention/cap_sweep.py)의 같은 10 ms profile과 cap stretch |
| cluster | 16노드 × 48/96/192코어. 새 sandbox는 floating power-of-2, 정책 쌍에 같은 seed와 안정적인 placement RNG |
| 경계 | heavy 명령의 첫 step에 한 번. boot 제외. 직전 수요, margin=1코어, 모든 목적지 중 최선(JSQ). 목적지에 폭을 예약해 같은 step의 중복 예약 방지. 이동 중 reactive 선택 제외 |
| 예측 | oracle: profile CPU ≥ 1 s, 폭=CPU/solo 길이. class: 옛 `cmdkind.kind()`로 test/build/install, 폭은 같은 repo의 다른 trace의 class-heavy 평균 폭 중앙값. 없으면 다른 모든 trace의 중앙값. 자기 trace를 학습에서 제외 |
| 반응 | hand-off 실제 정지 10 ms, detect 50 ms, hold 1 s, max_batch=8. strict_handoff=True로 step 말 결정의 경계를 보정하고 source CPU 2 ms도 정확한 budget으로 계산. 기본 False는 기존 결과를 보존. pre-copy는 detect 500 ms / hold 5 s, 100 Gb/s NIC와 trace별 guest 메모리 |
| 경계 비용 | L=1/10/100/1000 ms. source CPU 입력은 1 ms(10 ms 변형은 2 ms). 100/1000 ms는 지연 효과를 보기 위해 CPU 비용을 1 ms로 고정. 경계 정지는 부분 step으로 반영하며 명령 종료 step의 남는 시간도 보간 |
| 시간·seed | cap30/cap16: ramp 600 + 측정 5,400 s, seed 2. 새 RL 비교: RL1 600+900 s, RL8 600+300 s. 정상적인 기존 RL1 none/pool/bnd100의 4,500 s 결과는 긴 구간 control로 보존. 동기 step: 1,800+7,200 s. 총 {summary['configs']}개 완료 설정. 미완료 seed 쌍 {len(summary['incomplete_seed_pairs'])}개는 표에서 제외 |
| 지표 | 측정 구간 안에서 시작하고 끝난 명령. solo는 해당 정책의 segment 길이, CPU 가중 평균은 profile CPU-s로 가중. 분위수는 seed별 분위수 두 개의 산술평균. lost=(대기+정지 중 못 한 CPU)/실행 CPU |
| timeout | main은 기존 cap sweep처럼 timeout 비활성(1e6 s). 기본 simulator의 pause 후 timeout 확인 동작을 보존하고, strict_timeout=True인 60 s 절단을 192코어·80/90%에서 별도 비교. strict 모드는 정지 중에도 60 s에서 자른다. 잘린 명령의 전체 solo 분모는 유지하므로 slowdown이 낮게 편향될 수 있음 |
| SLO | x=run_cores_per_node/cores, 측정점 사이 선형 보간. 노드 비=N_X/N_none=U_none/U_X. 최고점 통과는 최대 사용률의 하한, 이에 따른 노드 비는 상한. 최저점도 실패하면 구간 내 최대점을 산출하지 않음 |

[`boundary.py`](../../../experiments/contention/boundary.py)는 grid 생성·재개·부하 검사를 담당한다. 입력과 코드 SHA256은 summary의 metadata에 남겼다. 수정의 영향을 받지 않아 보존한 기존 행의 config ID와 당시 metadata는 retained_input_provenance에 따로 보존했다. cap16의 단독 tool 시간 비는 **1.017698**이며 contention slowdown과 별도다. 이동/s는 16노드 전체의 측정 구간 합이다.

약 4시간의 grid 예산에서 지연 보정의 재계산과 seed 2를 유지하기 위해 RL 측정 구간을 줄였다. 긴 구간의 정상 control을 함께 보존했지만, 짧은 구간과 일치한다고 가정하지 않는다. 최초 단일 설정 wall은 다언어 81.44초, RL의 600초 측정 benchmark는 360.51초였다(입력 준비 제외, JIT가 발생하면 포함).

## 3. 결과

### 3.1 같은 목표 사용률 80%의 명령 지연과 손실

{md(metrics)}
전체 사용률 점 및 cap16/Python RL은 [metrics.csv](metrics.csv)에 있다. CPU 가중 평균, heavy p99, lost CPU는 서로 다른 가중 방식의 지표다.

### 3.2 pool 이득 중 되찾은 비율

각 cell은 **CPU 가중 / heavy p99 회수율(%)**이다. (none − X)/(none − pool)로 계산했다. 음수나 100% 초과를 자르지 않고, 분모가 거의 없으면 산출하지 않는다.

{md(recovery)}

### 3.3 SLO를 지키는 최대 실제 사용률과 노드 수

cell은 **최대 실제 U / none 대비 필요한 노드 수 비**다. `≥U / ≤비`는 grid 상단까지 통과한 하한/상한이다. 둘 다 최대점을 찾지 못한 비교의 노드 비는 비워 뒀다. 네 SLO 전체와 cap16/RL은 [slo.csv](slo.csv)에 있다.

{md(slo_rows)}

### 3.4 이동 지연, view, 폭, power-of-2

목표 90%, oracle 경계만 비교했다.

{md(costs)}
192코어에서 기본 view=0/평균 폭/JSQ를 한 요소씩 바꾼 결과다.

{md(sensitivity)}

### 3.5 경계 배치 뒤 남는 잃은 CPU

다음 표는 cap30 oracle 1 ms에서 전체 lost CPU를 분모로 한 비율이다. (a)는 선택된 노드에 **실제 평균 폭**만큼 시작 여유가 있었고, 이후 그 노드에서 다른 명령 시작이 관측된 경우, (b)는 시작 때 그 폭이 들어갈 노드가 하나도 없던 경우, (c)는 CPU < 1 s 명령이다. boot, margin 때문에 맞는 노드가 있어도 못 들어간 경우, 후속 시작이 아직 관측되지 않은 시작 여유 명령은 잔차에 보존했다. (a)+(b)+(c)+잔차=100%다.

{md(causes)}
(a)는 관측에 따른 분류다. 후속 명령이 해당 손실 step까지 계속 실행 중인지를 추적한 counterfactual 분해가 아니므로 이후 도착의 인과 효과를 단독으로 증명하지 않는다. 같은 명령의 수요가 평균 폭보다 커지는 효과도 함께 있다. 마지막 열은 후속 시작 여부와 무관한 시작 여유 전체다. 모든 작업량의 분해는 [causes.csv](causes.csv)에 있다. heavy 명령의 폭 변화와 class 오차는 다음과 같다. 폭 오차는 class-heavy 예측 명령에 대해 계산했고, 비율 오차는 실제 평균 폭이 양수인 명령을 사용했다.

{md(variability)}
class의 leave-one-trace-out fallback은 {predictor['fallback_traces']} trace였다. 실제 heavy 명령은 {predictor['actual_heavy']:,}개로 CPU의 {predictor['heavy_cpu_share']*100:.1f}%다.

### 3.6 cap16

cap16은 solo tool 시간을 1.77% 늘린다. contention 지표와 SLO는 cap16 solo를 기준으로 한다. cap30과의 총 지연 비교에서는 별도로 보고한 cap 비용도 함께 고려해야 한다. 96/192코어 × 70/80/85/90/95% × 여섯 정책 × 두 seed는 [tables.md](tables.md)에 모두 있다.

### 3.7 Python RL과 메모리 밀도

지정된 `20261026T114153Z-solo-kx2i0s5k/run.json`은 **1 vCPU jq 단일 trace**다. 확인된 iter2 run `20261026T133107Z-solo-5i8p_9nm`(440 trace)에서 `candidates-rl.json`과 기존 usable 기준으로 **420개**를 읽었다. 8 vCPU rollout `20261104T092752Z-solo-pdkup4sy`는 같은 candidates와 **5개**만 겹친다. `20261109T015041Z`는 reward 평가용이라 사용하지 않았다. 1/8 vCPU는 같은 표본의 대조 실험이 아니다.

다음은 192코어의 밀도다. 48/96코어의 반올림 차이와 모든 정책의 실제 U는 [density.csv](density.csv)에 있다. GiB/코어 = sandbox/코어 × 228 MiB / 1024이며 [10-27 메모리 측정](../virtual-node-rl-20261026/guest-memory/README.md)의 VM 평균을 사용했으며 host cache 등은 더하지 않았다. bud05의 사용자 기준은 4 GB/코어다.

{md(density)}
목표 90%에서의 contention 결과:

{md(rl)}
1 vCPU·gap 1·48코어·목표 90%의 oracle 경계 1 ms / reactive는 CPU 가중 {f(rl_bnd90.get("cpu_weighted_slowdown"))} / {f(rl_react90.get("cpu_weighted_slowdown"))}, heavy p99 {f(rl_bnd90.get("heavy_command_slowdown_p99"),2)} / {f(rl_react90.get("heavy_command_slowdown_p99"),2)}다. 새 구간 pool의 CPU 가중은 {f(rl_pool90.get("cpu_weighted_slowdown"))}다. 기존 긴 구간 control과도 함께 비교해야 한다. 원인을 초기 상태나 특정 LLM phase 하나로 확정할 추가 시계열 계측은 하지 않았다. 실제 U와 목표의 차이도 그대로 보여 준다. 측정 상단까지 SLO를 만족하면 최대 사용률의 하한만 알 수 있으며, 그 경우 추가 노드 절감을 확정하지 않았다. 8 vCPU는 표본 5개와 짧은 측정 구간의 보조 결과다.

gap 0.25·48코어의 목표 90%에서는 CPU 가중 slowdown이 none {f(quarter['none'].get('cpu_weighted_slowdown'))}, 경계 1 ms {f(quarter['bnd-oracle-1ms'].get('cpu_weighted_slowdown'))}, reactive {f(quarter['reactive'].get('cpu_weighted_slowdown'))}다. 이 조건은 경계 배치가 reactive보다 좋다. 이동 지연이 다른 두 정책의 비교이며, reactive가 모든 작업량에서 우세하다는 결론은 내리지 않는다.

긴 구간에서 변하지 않는 정책(none/pool/bnd100)의 정상 결과도 보존했다. 서로 다른 측정 길이를 섞어서 SLO를 보간하지 않았다.

{md(horizon)}

{warm_section}

### 3.8 동기 RL step

cap sweep 4절의 설정: 192코어, async 밀도 목표 200%, batch16×group8, train 30 s, 같은 trace를 group 안에서 복제했다.

{md(steps)}

### 3.9 60초 timeout 민감도

192코어의 절단된 명령을 포함한 지표다. main SLO 산정에는 이 표를 사용하지 않았다.

{md(timeout)}

## 4. 한계

- CPU runqueue 경합을 나타내는 fluid simulator다. cache·주파수·메모리 대역폭·CXL tax·실제 restore 비용·고밀도 생성 비용은 들어 있지 않다. VM·Docker 실측 검증을 이번 작업에서 새로 하지 않았다.
- 기존 reactive 경로는 10 ms 입력이 추가 정지 0 step으로 계산되는 경계 양자화가 있었다. 이번 표의 반응 정책은 strict_handoff=True로 실제 1-step(10 ms) 정지를 적용했다. 기본 False는 이전 결과 보존용이다. 이전의 낙관적인 reactive 행은 최종 집계에서 제외했다.
- cap30은 관측할 수 있었던 최대 30코어를 뜻한다. 더 큰 host에서 명령의 병렬 폭이 더 커지는 효과는 외삽하지 않았다.
- 수요·배치 결정은 10 ms 격자다. 1 ms 경계 정지는 step CPU 예산에서 부분 정지로 반영하고 마지막 step을 보간하지만, 뒤 segment의 시작은 계속 격자에 맞춘다.
- 경계와 strict reactive의 source CPU 비용은 같은 부분 step budget으로 처리했다. 기존 기본값에서만 원래의 CPU task 양자화를 유지한다. 실제 overhead/run은 metrics.csv에 보존했다.
- L=100 ms/1 s는 정지 지연의 민감도이며 특정 restore 구현의 전체 CPU 비용을 재현한 값은 아니다. source CPU를 1 ms로 고정한 이 비교는 CPU 비용이 큰 restore에 유리하다.
- heavy의 평균 폭 oracle도 이후 다른 명령 도착과 자기 명령의 폭 변화를 모른다. (a)/(b)/(c)는 admission 시점 관측에 따른 분류이며 인과적 counterfactual 분해가 아니다.
- 측정 구간 양 끝에서 미완료 명령을 제외하는 선택 효과와 seed 2개의 표본 오차가 있다. seed별 최소/최대는 summary/metrics에 보존했다. 분위수를 모든 seed의 원시 명령으로 합친 값과 구분해야 한다.
- 같은 사용률을 얻는 sandbox 밀도는 CPU 모델에서 정했다. 메모리 수용 가능성과는 별도다. 228 MiB 추정은 1 vCPU 측정에서 왔고 8 vCPU의 실측 평균 메모리를 대신하지 않는다.
- 이전 checkout은 읽기 전용 입력이다. `/Serverless`, `~/cxl-page-cache`, node 1/2 실행을 사용하지 않았다. 모든 simulator 실행은 node 0 bind/nice 19, worker 최대 24였다.

## 5. 재현

Python 3.12, numpy 2.5.4, numba 0.68.1, llvmlite 0.50.0을 현재 checkout의 `.venv`에 설치했다. 기존 raw 입력은 옛 checkout에 있어야 한다. 생성 JSONL은 git 밖 `.local/boundary/`에 두고 요약만 commit했다.

```bash
export PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMBA_NUM_THREADS=1
boundary_python=.venv/bin/python
boundary_out=experiments/contention/.local/boundary
run0() {{ numactl --cpunodebind=0 --membind=0 nice -n 19 "$@"; }}
run0 "$boundary_python" experiments/contention/fleet.py test
run0 "$boundary_python" experiments/contention/cap_sweep.py test
run0 "$boundary_python" experiments/contention/boundary.py grid --priorities 1 2 --duration-s 5400 --timeout-s 1000000 --grid "$boundary_out/grid.json" --output "$boundary_out/results.jsonl"
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/grid.json" --output "$boundary_out/results.jsonl" --jobs 24
run0 "$boundary_python" experiments/contention/boundary.py grid --priorities 3 --duration-s 900 --timeout-s 1000000 --grid "$boundary_out/rl-grid.json" --output "$boundary_out/rl.jsonl"
run0 "$boundary_python" -c 'import json,pathlib;p=pathlib.Path("experiments/contention/.local/boundary/rl-grid.json");cs=json.loads(p.read_text());[c.update(duration_s=300.0) for c in cs if c["workload"]=="rl8"];p.write_text(json.dumps(cs,indent=1))'
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/rl-grid.json" --output "$boundary_out/rl.jsonl" --jobs 24
run0 "$boundary_python" experiments/contention/boundary.py grid --priorities 4 --ramp-s 1800 --duration-s 7200 --timeout-s 1000000 --grid "$boundary_out/step-grid.json" --output "$boundary_out/step.jsonl"
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/step-grid.json" --output "$boundary_out/step.jsonl" --jobs 24
run0 "$boundary_python" -c 'import json,pathlib;d=pathlib.Path("experiments/contention/.local/boundary");cs=json.loads((d/"grid.json").read_text());cs=[dict(c,timeout_s=60.0,strict_timeout=True) for c in cs if c["priority"]==1 and c["cores"]==192 and c["target_util"] in (80,90)];(d/"timeout-grid.json").write_text(json.dumps(cs,indent=1))'
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/timeout-grid.json" --output "$boundary_out/timeout.jsonl" --jobs 24
# 보존한 긴 RL control 162개도 같은 config ID로 재현한다.
run0 "$boundary_python" - <<'PYCONTROL'
import json
from pathlib import Path
import sys
sys.path.insert(0, "experiments/contention")
from boundary import config_id
root = Path("experiments/contention/.local/boundary")
ids = set(json.loads(Path("docs/results/boundary-vs-handoff-20261113/summary.json").read_text())["long_control_config_ids"])
configs = [dict(c, duration_s=4500.0) for c in json.loads((root / "rl-grid.json").read_text())]
configs = [c for c in configs if config_id(c) in ids]
assert len(configs) == len(ids)
(root / "long-grid.json").write_text(json.dumps(configs, indent=1))
PYCONTROL
run0 "$boundary_python" experiments/contention/boundary.py run --grid "$boundary_out/long-grid.json" --output "$boundary_out/long.jsonl" --jobs 24
{warm_repro}run0 "$boundary_python" docs/results/boundary-vs-handoff-20261113/analyze.py --readme --widths --final
```

run은 config SHA256으로 이미 완료한 설정을 건너뛰고 한 줄마다 flush한다. 큰 grid 전마다 uptime/상위 CPU process를 기록하고, 외부 node 0 사용이 4코어를 넘으면 10분 대기 후 재검사한다. 여전히 바쁘면 exit 75로 후속 grid도 멈춘다. 합성 입력에서 기본값을 보존한 6설정의 이전 버전 대조, self-test, SLO 보간 및 요약 percentile 일치를 검증했다. 실행 시간·서버 시작/종료 기록은 `.codex-runs/10.last.md`에 있다.
"""
    c=summary.get("completion",{})
    if any(c.get(k,0)<v for k,v in (("main",606),("rl",600),("timeout",60))):
        label="시간 예산 내 완료 결과" if summary.get("is_final") else "진행 중"
        report=f"> {label}: main {c.get('main',0)}/606, 새 RL {c.get('rl',0)}/600, timeout {c.get('timeout',0)}/60. 미완료 값은 추정하지 않는다.\n\n"+report
    (HERE / "README.md").write_text(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="*")
    parser.add_argument("--final", action="store_true", help="label any remaining omissions as budget-limited")
    parser.add_argument("--widths", action="store_true", help="also read RL solo profiles for within-command width distributions")
    parser.add_argument("--readme", action="store_true", help="also regenerate the Korean result report")
    args = parser.parse_args()
    inputs = args.inputs or [p for p in RAW.glob("*.jsonl") if p.name != "load.jsonl"]
    raw, metadata = {}, []
    for path in inputs:
        if not path.exists():
            continue
        for line in path.open():
            r = json.loads(line)
            if "config_id" in r:
                raw[r["config_id"]] = r
        meta = path.with_suffix(".inputs.json")
        if meta.exists():
            metadata.append(json.loads(meta.read_text()))
    groups = {}
    for r in raw.values():
        key = tuple(r[k] for k in ("priority", "workload", "gap_scale", "timeout_s", "cores", "target_util", "variant", "ramp_s", "duration_s"))
        groups.setdefault(key, []).append(r)
    rows = []
    incomplete = []
    for key, rs in sorted(groups.items()):
        if len(rs) != 2:
            incomplete.append(dict(condition=key, seeds=[r["seed"] for r in rs]))
            continue
        row = dict(zip(("priority", "workload", "gap_scale", "timeout_s", "cores", "target_util", "variant", "ramp_s", "duration_s"), key))
        row.update(seeds=len(rs), **{k: mean(rs, k) for k in METRICS})
        if row["priority"] != 3:
            row["gib_per_core"] = None
        row["actual_util"] = row["run_cores_per_node"] / row["cores"]
        row["lost_pct"] = row["lost_over_run"] * 100
        row["bnd_moves_per_s"] = row["bnd_moves"] / row["duration_s"]
        row["reactive_moves_per_s"] = row["reactive_moves"] / row["duration_s"]
        row["timeout_pct"] = row["commands_timed_out"] / row["commands_finished"] * 100 if row["commands_finished"] else None
        for metric in ("cpu_weighted_slowdown", "heavy_command_slowdown_p99"):
            row[metric + "_seed_min"] = min((r[metric] for r in rs if r[metric] is not None), default=None)
            row[metric + "_seed_max"] = max((r[metric] for r in rs if r[metric] is not None), default=None)
        row["loss_cpu_s_by_cause"] = [statistics.mean(r["loss_cpu_s_by_cause"][i] for r in rs) for i in range(4)]
        rows.append(row)
    indexed = {(r["priority"], r["workload"], r["gap_scale"], r["timeout_s"], r["cores"], r["target_util"], r["ramp_s"], r["duration_s"], r["variant"]): r for r in rows}
    recovered = []
    for key, row in indexed.items():
        base, pool = indexed.get(key[:-1] + ("none",)), indexed.get(key[:-1] + ("pool",))
        if base is None or pool is None:
            continue
        r = {k: row[k] for k in ("priority", "workload", "gap_scale", "timeout_s", "cores", "target_util", "variant", "ramp_s", "duration_s")}
        for metric in ("cpu_weighted_slowdown", "heavy_command_slowdown_p99"):
            diff = base[metric] - pool[metric] if base[metric] is not None and pool[metric] is not None else 0
            r[metric + "_pool_recovered"] = (base[metric] - row[metric]) / diff if diff > 1e-6 and row[metric] is not None else None
        recovered.append(r)
    curves = {}
    for r in rows:
        if r["priority"] == 4:
            continue
        key = tuple(r[k] for k in ("priority", "workload", "gap_scale", "timeout_s", "cores", "ramp_s", "duration_s", "variant"))
        curves.setdefault(key, []).append(r)
    slos = []
    for key, curve in sorted(curves.items()):
        for metric, limit in SLOS:
            r = dict(zip(("priority", "workload", "gap_scale", "timeout_s", "cores", "ramp_s", "duration_s", "variant"), key))
            r.update(metric=metric, slo=limit, **slo_limit(curve, metric, limit))
            slos.append(r)
    for r in slos:
        base = next((b for b in slos if all(b[k] == r[k] for k in ("priority", "workload", "gap_scale", "timeout_s", "cores", "ramp_s", "duration_s", "metric", "slo")) and b["variant"] == "none"), None)
        if base and base["util"] and r["util"] and base["status"] == "interpolated":
            r["nodes_over_none"] = base["util"] / r["util"]
            r["node_saving_pct"] = (1 - r["nodes_over_none"]) * 100
            r["node_ratio_status"] = "upper_bound" if r["status"] == "at_least" else "interpolated"
        else:
            r.update(nodes_over_none=None, node_saving_pct=None, node_ratio_status="unidentified")
    causes = []
    for row in rows:
        if row["variant"] != "bnd-oracle-1ms":
            continue
        a, b, c, residual = row["loss_cpu_s_by_cause"]
        total = a + b + c + residual
        r = {k: row[k] for k in ("priority", "workload", "gap_scale", "timeout_s", "cores", "target_util", "ramp_s", "duration_s")}
        r.update(a_start_fit_pct=(a-row["initial_unfit_loss_cpu_s"]) / total * 100 if total else None,
                 b_no_fitting_node_pct=b / total * 100 if total else None, c_light_pct=c / total * 100 if total else None,
                 residual_boot_or_unfit_pct=(residual + row["initial_unfit_loss_cpu_s"]) / total * 100 if total else None,
                 a_with_later_arrival_pct=row["later_arrival_loss_cpu_s"] / total * 100 if total else None,
                 residual_unattributed_pct=(residual+a-row["later_arrival_loss_cpu_s"]) / total * 100 if total else None,
                 total_lost_cpu_s=total)
        causes.append(r)
    density = [{k:r[k] for k in ("workload", "gap_scale", "cores", "target_util", "ramp_s", "duration_s", "actual_util", "variant", "sandbox_per_core", "gib_per_core")}
               for r in rows if r["priority"] == 3]
    summary = dict(configs=len(raw), incomplete_seed_pairs=incomplete, seed_aggregation="arithmetic mean of two per-seed summaries, including quantiles",
                   input_files=[str(p) for p in inputs], metadata=metadata, results=rows, recovered=recovered,
                   slos=slos, causes=causes, density=density,
                   slo_method="piecewise linear at actual run_cores_per_node/cores; no extrapolation; at_least denotes a grid endpoint")
    summary["long_control_config_ids"] = sorted(r["config_id"] for r in raw.values() if r["priority"]==3 and r["workload"]=="rl1" and r["ramp_s"]==600 and r["duration_s"]==4500.0)
    summary["retained_input_provenance"] = []
    for label in ("results", "rl"):
        original = RAW / (label + ".before-fractional-repair.jsonl.bak")
        meta = RAW / (label + ".before-fractional-repair.inputs.json.bak")
        if original.exists() and meta.exists():
            retained = []
            for line in original.open():
                r = json.loads(line)
                if (r["config_id"] in raw and not (r.get("bnd_on") and r.get("bnd_latency_s") == .001)
                        and r["mechanism"] != "hand-off 10ms" and (label != "rl" or r["workload"] == "rl1")):
                    retained.append(r["config_id"])
            summary["retained_input_provenance"].append(dict(source=str(original), metadata=json.loads(meta.read_text()),
                config_ids=sorted(retained), reason="unaffected by fractional boundary / strict reactive corrections"))
    summary["completion"] = {
        "main":sum(r["priority"] in (1,2) and r["timeout_s"]>60 for r in raw.values()),
        "rl":sum(r["priority"]==3 and r["ramp_s"]==600 and r["duration_s"]==(900.0 if r["workload"]=="rl1" else 300.0) for r in raw.values()),
        "timeout":sum(r["timeout_s"]==60 for r in raw.values()),
        "step":sum(r["priority"]==4 for r in raw.values()),
        "expected":{"main":606,"rl":600,"timeout":60,"step":12}}
    summary["warm_configs"] = sum(r["priority"]==3 and r["ramp_s"]==3600 for r in raw.values())
    summary["is_final"] = args.final
    if args.widths:
        summary["rl_width_variability"] = rl_width_variability(metadata)
    (HERE / "summary.json").write_text(json.dumps(summary, indent=1, allow_nan=False) + "\n")
    table(HERE / "metrics.csv", [{k:v for k,v in r.items() if k != "loss_cpu_s_by_cause"} for r in rows])
    table(HERE / "recovered.csv", recovered)
    table(HERE / "slo.csv", slos)
    table(HERE / "causes.csv", causes)
    table(HERE / "density.csv", density)
    columns = ["workload", "gap_scale", "cores", "target_util", "ramp_s", "duration_s", "variant", "actual_util", "cpu_weighted_slowdown",
               "heavy_command_slowdown_p99", "lost_pct", "bnd_moves_per_s", "reactive_moves_per_s"]
    text = "# 전체 결과\n\nseed별 분위수 두 개의 평균이다. 실제 사용률은 비율(0~1)이며, SLO 경계는 측정 구간 밖으로 외삽하지 않는다.\n\n"
    for priority in sorted({r["priority"] for r in rows}):
        selected = [r for r in rows if r["priority"] == priority and r["timeout_s"] > 60]
        text += f"## 우선순위 {priority}\n\n" + markdown_table(selected, columns) + "\n"
    text += "## SLO\n\n" + markdown_table([r for r in slos if r["timeout_s"] > 60],
        ["workload", "gap_scale", "cores", "ramp_s", "duration_s", "variant", "metric", "slo", "util", "status", "nodes_over_none", "node_saving_pct", "node_ratio_status"])
    text += "\n## pool 이득 중 회수\n\n" + markdown_table([r for r in recovered if r["timeout_s"] > 60],
        ["workload", "gap_scale", "cores", "target_util", "ramp_s", "duration_s", "variant", "cpu_weighted_slowdown_pool_recovered", "heavy_command_slowdown_p99_pool_recovered"])
    text += "\n## 잃은 CPU 분해\n\n" + markdown_table(causes,
        ["workload", "gap_scale", "cores", "target_util", "ramp_s", "duration_s", "a_with_later_arrival_pct", "b_no_fitting_node_pct", "c_light_pct", "residual_unattributed_pct", "a_start_fit_pct"])
    text += "\n## Python RL 밀도\n\n" + markdown_table([r for r in density if r["variant"] == "none"], list(density[0]) if density else [])
    (HERE / "tables.md").write_text(text)
    if args.readme:
        write_readme(summary)
    print(f"{len(raw)} configs, {len(rows)} seed pairs, {len(slos)} SLO rows -> {HERE}")


if __name__ == "__main__":
    main()
