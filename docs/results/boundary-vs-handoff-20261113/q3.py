#!/usr/bin/env python3
"""Task 12: paired-seed idle migration, network boundary copying, and CXL tax analysis."""
import json
import hashlib
from collections import Counter, defaultdict

from analyze import HERE, RAW, METRICS, SLOS, mean, slo_limit, table, markdown_table, write_readme


def get(rows, **match):
    return next((r for r in rows if all(r.get(k) == v for k, v in match.items())), {})


def ratio(a, b):
    return a / b if a is not None and b else None


def f(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


def headline(data):
    hist=[get(data["results"],cores=c,target_util=90,gap_scale=1,variant="pre-hist-1s") for c in (48,96,192)]
    text="**12번 추가 결론([7절](#7-llm-대기-중-이동-경계-복사와-cxl-tax-12번)): 위 hand-off 수치는 CXL 실행 tax를 제외한 경합 비교다.** "
    if all(hist):
        text+="대기 중 pre-hist 1초는 목표90%에서 pool CPU 가중 이득의 "+"/".join(f(100*r["cpu_weighted_slowdown_pool_recovered"],1) for r in hist)+"%(48/96/192코어)를 회수했다. "
    tax=[get(data["results"],cores=c,target_util=90,gap_scale=1,variant="cxl-hybrid-1.23") for c in (48,96,192)]
    if all(tax):
        text+="heavy +23% tax를 낸 CXL 결합의 CPU 가중 slowdown은 "+"/".join(f(r["cpu_weighted_slowdown"]) for r in tax)+"로 DRAM 경계보다 나빴다. "
    text+="100G 경계 복사는 container 상태·전용 대역폭을 가정한 유리한 하한이며, 이 가정에서는 경계1ms에 가까운 이득을 얻는다."
    return text


def crossing(points):
    """First loss of an advantage as tax rises. No extrapolation or monotonic fitting."""
    points = sorted(points)
    if not points:
        return dict(tau=None, status="missing")
    if points[0][1] > 0:
        return dict(tau=None, status="below_grid", bound=points[0][0])
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if d0 <= 0 < d1:
            return dict(tau=t0 - d0 * (t1 - t0) / (d1 - d0), status="interpolated", bracket=[t0,t1])
    return dict(tau=None, status="above_grid", bound=points[-1][0])


def execution(root):
    guards=[json.loads(line) for line in (root/"load.jsonl").open()] if (root/"load.jsonl").exists() else []
    timings=[]
    for stage in ("a","slo","tax.failed","tax","tax-slo","match","hist30","gap"):
        path=root/(stage+".log")
        if not path.exists():continue
        content=path.read_text()
        checks=[r for r in guards if r["uptime"] in content]
        timings.append(dict(stage=stage,guard_count=len(checks),
                            preparation_excluded_wall_s=checks[-1]["time"]-checks[0]["time"] if len(checks)>1 else None))
    benchmark=None
    for line in (root/"bench.log").open() if (root/"bench.log").exists() else []:
        if line.startswith("{"):
            benchmark=json.loads(line).get("wall_s")
            break
    affinity=root/"affinity-tax.json"
    numa={}
    if affinity.exists():
        rs=json.loads(affinity.read_text())["processes"]
        numa=dict(processes_including_parent=len(rs),cpu_affinity=rs[0]["affinity"] if rs else [],
                  nice_values=sorted({r["nice"] for r in rs}),
                  anonymous_outside_node0_processes=sum(bool(r["anonymous_outside0"]) for r in rs),
                  shared_file_node1_pages_range=[min(r["shared_file_pages_outside0"].get("1",0) for r in rs),max(r["shared_file_pages_outside0"].get("1",0) for r in rs)] if rs else [])
    return dict(benchmark_wall_s=benchmark,timings=timings,load_checks=guards,numa_sample=numa,
                resolved_failure="First TAX attempt produced no rows: tau=1.1 interpolation left 29 positive idle-edge roundoff samples, total 2.185e-14 CPU-s. Threshold cleanup conserves segment work; boundary self-test and resumed full TAX passed.")


def analyze():
    summary = json.loads((HERE / "summary.json").read_text())
    root = RAW.parent / "boundary12"
    raw, groups, metadata = {}, defaultdict(list), []
    planned = {}
    for path in sorted(root.glob("*-grid.json")):
        if path.name in ("all-grid.json", "bench-grid.json"):
            continue
        for c in json.loads(path.read_text()):
            # Keep the same ID calculation as the runner, without importing trace input code.
            planned[hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()[:20]] = c
    for path in sorted(root.glob("*.jsonl")):
        if path.name == "load.jsonl" or path.stem == "bench":
            continue
        for line in path.open():
            r = json.loads(line)
            if str(r.get("study", "")).startswith("12-"):
                if r["config_id"] in raw:
                    raise ValueError(f"duplicate config {r['config_id']}")
                raw[r["config_id"]] = r
        meta = path.with_suffix(".inputs.json")
        if meta.exists():
            metadata.append(dict(path=str(meta), data=json.loads(meta.read_text())))
    keys = ("workload", "cores", "target_util", "variant", "gap_scale", "ramp_s", "duration_s", "per_node")
    for r in raw.values():
        groups[tuple(r[k] for k in keys)].append(r)
    extra = ["dram_cpu_weighted_slowdown", "dram_run_cores_per_node", "pre_moves", "pre_wrong_moves",
             "pre_wrong_move_fraction", "pre_heavy_starts", "pre_predicted_unfit", "pre_actual_unfit",
             "pre_unfit_fraction", "pre_initial_fit_starts", "pre_later_unfit", "pre_blocked", "pre_reserved_unfit",
             "pre_starts", "pre_late_starts", "pre_late_s", "pre_late_heavy_starts"]
    rows, incomplete = [], []
    for key, rs in sorted(groups.items()):
        row = dict(zip(keys, key))
        if len(rs) != 2 or {r["seed"] for r in rs} != {"1", "2"}:
            incomplete.append(row | dict(seeds=[r["seed"] for r in rs]))
            continue
        row.update({k:mean(rs,k) for k in METRICS + extra})
        row.update(priority=20, timeout_s=1e6, seeds=2, studies=sorted({r["study"] for r in rs}),
                   config_ids=sorted(r["config_id"] for r in rs), actual_util=row["run_cores_per_node"]/row["cores"],
                   useful_util=row["dram_run_cores_per_node"]/row["cores"], lost_pct=100*row["lost_over_run"],
                   tax_heavy=rs[0].get("tax_heavy"), pre_latency_s=rs[0].get("pre_latency_s"),
                   bnd_latency_s=rs[0].get("bnd_latency_s"), copy_gbps=rs[0].get("copy_gbps"))
        for name in ("pre", "bnd", "reactive"):
            row[name+"_moves_per_s"] = row[name+"_moves"] / row["duration_s"]
        row["pre_actual_unfit_fraction"] = ratio(row["pre_actual_unfit"], row["pre_heavy_starts"])
        row["pre_later_unfit_fraction"] = ratio(row["pre_later_unfit"], row["pre_initial_fit_starts"])
        row["pre_reserved_unfit_fraction"] = ratio(row["pre_reserved_unfit"], row["pre_heavy_starts"])
        row["pre_late_fraction"] = ratio(row["pre_late_starts"], row["pre_starts"])
        row["pre_mean_late_s"] = ratio(row["pre_late_s"], row["pre_starts"])
        row["seed_values"] = {r["seed"]:{k:r[k] for k in ("cpu_weighted_slowdown","heavy_command_slowdown_p99","lost_over_run")} for r in rs}
        rows.append(row)
    # Read the original IDs too: all original measurements are reused, never relabelled as new runs.
    old_raw = {}
    for filename in summary["input_files"]:
        for line in open(filename):
            r = json.loads(line)
            if r.get("priority") == 1 and r["timeout_s"] > 60:
                old_raw[r["config_id"]] = r
    references = []
    for r in summary["results"]:
        if r["priority"] != 1 or r["timeout_s"] <= 60 or r["cores"] not in (48,96,192):
            continue
        ids = sorted(x["config_id"] for x in old_raw.values() if all(x.get(k)==r.get(k) for k in
                     ("cores","target_util","variant","gap_scale","ramp_s","duration_s")))
        assert len(ids) == 2, (r["variant"], r["cores"], r["target_util"], ids)
        references.append(dict(r, config_ids=ids, useful_util=r["actual_util"], source="task10"))
    combined = references + rows
    for r in rows:
        same = dict(cores=r["cores"], target_util=r["target_util"], gap_scale=r["gap_scale"])
        base, pool = get(combined, **same, variant="none"), get(combined, **same, variant="pool")
        for metric in ("cpu_weighted_slowdown", "heavy_command_slowdown_p99"):
            delta = base.get(metric,0)-pool.get(metric,0)
            r[metric+"_pool_recovered"] = (base[metric]-r[metric])/delta if delta>1e-6 else None
    # DRAM policies extend all four original SLOs; CXL uses CPU<=1.10 and useful DRAM work.
    slos, tax_slos = [], []
    for cores in (48,96,192):
        variants = sorted({r["variant"] for r in rows if r["cores"]==cores and r["gap_scale"]==1 and
                           (r["tax_heavy"] is not None or r["variant"] in
                            ("pre-hist-1s","pre-hist-1s+bnd-class-1ms","pre-hist-1s+bnd-class-100ms","bnd-copy-25G","bnd-copy-100G"))})
        for variant in variants:
            curve = [r for r in rows if r["cores"]==cores and r["variant"]==variant and r["gap_scale"]==1]
            if len(curve)<(2 if variant.endswith("+bnd-class-100ms") else 3):
                continue
            tax = curve[0]["tax_heavy"]
            if tax is None and cores==192:
                continue
            for metric,limit in ([("cpu_weighted_slowdown",1.10)] if tax is not None else SLOS):
                base=get(summary["slos"],priority=1,cores=cores,metric=metric,slo=limit,variant="none",timeout_s=1e6)
                s = dict(priority=20,workload="cap30",gap_scale=1.0,timeout_s=1e6,cores=cores,ramp_s=600,
                         duration_s=5400.0,variant=variant,metric=metric,slo=limit,
                         **slo_limit([dict(r,actual_util=r["useful_util"]) for r in curve],metric,limit))
                if base.get("status")=="interpolated" and s["util"]:
                    n = base["util"]/s["util"]
                    s.update(nodes_over_none=n,node_saving_pct=100*(1-n),node_ratio_status="upper_bound" if s["status"]=="at_least" else "interpolated")
                else:
                    s.update(nodes_over_none=None,node_saving_pct=None,node_ratio_status="unidentified")
                    if tax is not None and base.get("status")=="interpolated" and s["status"]=="below_grid":
                        s["nodes_over_none_lower_bound_if_feasible"]=base["util"]/s["lowest_measured_util"]
                if tax is not None:
                    s.update(tax_heavy=tax,util_axis="DRAM-reference useful CPU / nominal capacity")
                (tax_slos if tax is not None else slos).append(s)
    for s in tax_slos:
        comparisons=[]
        for baseline in ("bnd-oracle-1ms","bnd-oracle-100ms","bnd-copy-100G"):
            base=get(slos,cores=s["cores"],variant=baseline,metric="cpu_weighted_slowdown",slo=1.10) if baseline.startswith("bnd-copy") else get(
                summary["slos"],priority=1,cores=s["cores"],variant=baseline,metric="cpu_weighted_slowdown",slo=1.10,timeout_s=1e6)
            if not base:continue
            status="unidentified"
            value=None
            if s["util"] and base["util"]:
                value=base["util"]/s["util"]
                status="interpolated" if s["status"]==base["status"]=="interpolated" else (
                    "upper_bound" if s["status"]=="at_least" and base["status"]=="interpolated" else (
                    "lower_bound" if s["status"]=="interpolated" and base["status"]=="at_least" else "both_censored"))
                if status=="both_censored":value=None
            comparisons.append(dict(baseline=baseline,nodes_over_baseline=value,status=status))
        s["DRAM_node_comparisons"]=comparisons
    breaks = []
    for cores in (48,96,192):
        for target in (80,90):
            for policy in ("reactive","hybrid"):
                curve = [r for r in rows if r["cores"]==cores and r["target_util"]==target and r["variant"].startswith("cxl-"+policy+"-")]
                for baseline in ("bnd-oracle-1ms","bnd-oracle-100ms","bnd-copy-100G"):
                    base = get(combined,cores=cores,target_util=target,gap_scale=1.,variant=baseline)
                    if not base:
                        continue
                    for metric in ("cpu_weighted_slowdown","heavy_command_slowdown_p99"):
                        points=[(r["tax_heavy"],r[metric]-base[metric]) for r in curve]
                        breaks.append(dict(cores=cores,target_util=target,policy=policy,baseline=baseline,metric=metric,**crossing(points)))
    completion = dict(Counter(r["study"] for r in raw.values()))
    expected = dict(Counter(c["study"] for c in planned.values()))
    predictors=metadata[0]["data"]["predictors"] if metadata else []
    heavy_share=next((p["heavy_cpu_share"] for p in predictors if p["policy"]=="cap30"),None)
    solo_floor=[dict(tau=t,cpu_weighted_solo_floor=(heavy_share*t*t+(1-heavy_share)*1.02**2)/(heavy_share*t+(1-heavy_share)*1.02))
                for t in (1.,1.02,1.05,1.1,1.23)] if heavy_share is not None else []
    node_breaks=[]
    for cores in (48,96,192):
        for policy in ("reactive","hybrid"):
            for baseline in ("bnd-oracle-1ms","bnd-oracle-100ms","bnd-copy-100G"):
                samples=[];bounds=[];gain=[];loss=[]
                for r in tax_slos:
                    if r["cores"]!=cores or not r["variant"].startswith("cxl-"+policy+"-"):continue
                    compare=get(r["DRAM_node_comparisons"],baseline=baseline)
                    bounds.append(dict(tau=r["tax_heavy"],status=compare.get("status","missing"),nodes_over_baseline=compare.get("nodes_over_baseline"),slo_status=r["status"]))
                    if compare.get("status")=="interpolated":samples.append((r["tax_heavy"],compare["nodes_over_baseline"]-1))
                    value=compare.get("nodes_over_baseline")
                    if value is not None:
                        if compare["status"] in ("interpolated","upper_bound") and value<1:gain.append(r["tax_heavy"])
                        if compare["status"] in ("interpolated","lower_bound") and value>1:loss.append(r["tax_heavy"])
                result=crossing(samples) if len(samples)>1 else dict(tau=None,status="censored_or_insufficient")
                if result["status"]!="interpolated" and loss:
                    upper=min(loss)
                    lower=max((t for t in gain if t<upper),default=None)
                    result=dict(tau=None,status="bounded",tau_lower=lower,tau_upper=upper)
                node_breaks.append(dict(cores=cores,policy=policy,baseline=baseline,**result,samples=bounds))
    q3 = dict(configs=len(raw),expected=expected,completion=completion,incomplete_seed_pairs=incomplete,
              missing_config_ids=sorted(planned.keys()-raw.keys()),results=rows,references=references,slos=slos,
              tax_slos=tax_slos,tax_break_even=breaks,tax_node_break_even=node_breaks,tax_input_solo_floor=solo_floor,metadata=metadata,execution=execution(root),
              inputs=metadata[0]["data"]["q3_inputs"] if metadata else {},
              aggregation="arithmetic mean of two per-seed summaries; quantiles are not pooled; cohort counter fractions are ratios of mean counts except pre_wrong/unfit (mean per-seed fraction)",
              tax_method="Physical CPU weights, DRAM solo lengths and heavy labels; useful served CPU is physical served CPU / segment tax. Same DRAM offered density at each tau. Light tax 1.02 even at heavy-tau=1 anchor.")
    summary["q3"] = q3
    summary["slos"] = [r for r in summary["slos"] if r.get("priority")!=20]+slos
    summary["total_configs"] = summary["configs"]+summary.get("followup",{}).get("configs",0)+len(raw)
    (HERE/"summary.json").write_text(json.dumps(summary,indent=1,allow_nan=False)+"\n")
    flat = lambda r:{k:v for k,v in r.items() if k not in ("seed_values","config_ids","studies")}
    table(HERE/"q3.csv",[flat(r) for r in rows])
    table(HERE/"q3-tax-break-even.csv",breaks)
    table(HERE/"q3-tax-slo.csv",[dict(r,DRAM_node_comparisons=json.dumps(r["DRAM_node_comparisons"])) for r in tax_slos])
    table(HERE/"slo.csv",summary["slos"])
    (HERE/"q3-tables.md").write_text("# 12번 전체 결과\n\n두 seed 요약의 평균. actual_util=물리 CPU, useful_util=DRAM 작업량 기준 CPU.\n\n"+
        markdown_table(rows,["cores","target_util","gap_scale","variant","actual_util","useful_util","cpu_weighted_slowdown","heavy_command_slowdown_p99","cpu_weighted_slowdown_pool_recovered","heavy_command_slowdown_p99_pool_recovered","pre_moves_per_s","bnd_moves_per_s","reactive_moves_per_s","pre_wrong_move_fraction","pre_unfit_fraction","pre_actual_unfit_fraction","pre_later_unfit_fraction","pre_late_fraction","pre_mean_late_s"]))
    figures(q3)
    write_readme(summary)
    print(f"Task12: {len(raw)} configs, {len(rows)} paired rows, {completion}; missing {len(q3['missing_config_ids'])}")


def figures(data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = data["results"] + data["references"]
    for kind in ("idle","tax"):
        fig,axes=plt.subplots(3,2,figsize=(11,11),layout="constrained")
        for ax,(cores,target) in zip(axes.flat,((c,t) for c in (48,96,192) for t in (80,90))):
            subset=[r for r in rows if r["cores"]==cores and r["target_util"]==target and r["gap_scale"]==1]
            if kind=="idle":
                for family in ("pre-oracle","pre-hist","bnd-oracle","bnd-class"):
                    points=[]
                    for latency in (.1,.3,1.,3.):
                        name=f"{family}-{latency:g}s"
                        if family.startswith("bnd") and latency==.1:name=family+"-100ms"
                        r=get(subset,variant=name)
                        if r:points.append((latency,r["cpu_weighted_slowdown"]))
                    ax.plot([p[0] for p in points],[p[1] for p in points],"o-",label=family)
                ax.set_xscale("log")
                ax.set_xlabel("Migration latency L (s)")
            else:
                for policy in ("reactive","hybrid"):
                    points=sorted((r["tax_heavy"],r["cpu_weighted_slowdown"]) for r in subset if r["variant"].startswith("cxl-"+policy+"-"))
                    ax.plot([p[0] for p in points],[p[1] for p in points],"o-",label="CXL "+policy)
                for name,style,color in (("bnd-oracle-1ms","--","#228833"),("bnd-oracle-100ms",":","#777777"),("bnd-copy-100G","-.","#AA3377")):
                    r=get(subset,variant=name)
                    if r:ax.axhline(r["cpu_weighted_slowdown"],linestyle=style,color=color,label="DRAM "+name)
                ax.set_xlabel("Heavy-command execution tax (tau)")
            ax.set_title(f"{cores} cores/node, target {target}%")
            ax.set_ylabel("CPU-weighted slowdown\n(DRAM solo = 1)")
            ax.grid(alpha=.2)
        handles,labels=axes.flat[0].get_legend_handles_labels()
        fig.legend(handles,labels,loc="outside lower center",ncol=3)
        for extension in ("png","pdf"):
            extra=dict(metadata={"CreationDate":None,"ModDate":None}) if extension=="pdf" else {}
            fig.savefig(HERE/f"q3-{kind}.{extension}",dpi=180,**extra)
        plt.close(fig)


def markdown(data):
    rows=data["references"]+data["results"]
    def row(cores,target,name,gap=1):
        return get(rows,cores=cores,target_util=target,variant=name,gap_scale=gap)
    main=[]
    for cores in (48,96,192):
        for target in (80,90):
            for name in ("none","pool","bnd-oracle-1ms","bnd-class-1ms","reactive","bnd-oracle-1ms+reactive",
                         "pre-oracle-1s","pre-hist-1s","pre-oracle-3s","pre-hist-3s","pre-hist-1s+bnd-class-1ms","pre-hist-1s+bnd-class-100ms","bnd-copy-25G","bnd-copy-100G"):
                r=row(cores,target,name)
                if not r:continue
                base,pool=row(cores,target,"none"),row(cores,target,"pool")
                recovery=(base["cpu_weighted_slowdown"]-r["cpu_weighted_slowdown"])/(base["cpu_weighted_slowdown"]-pool["cpu_weighted_slowdown"])
                tail_recovery=(base["heavy_command_slowdown_p99"]-r["heavy_command_slowdown_p99"])/(base["heavy_command_slowdown_p99"]-pool["heavy_command_slowdown_p99"])
                main.append({"코어":cores,"목표U%":target,"정책":name,"CPU 가중":f(r["cpu_weighted_slowdown"]),"heavy p99":f(r["heavy_command_slowdown_p99"]),
                             "pool 회수%":f(100*recovery,1),"tail 회수%":f(100*tail_recovery,1),"pre 이동/s":f(r.get("pre_moves_per_s"),2),"경계 이동/s":f(r["bnd_moves_per_s"],2),
                             "reactive 이동/s":f(r["reactive_moves_per_s"],2),"헛이동%":f(100*r["pre_wrong_move_fraction"],1) if r.get("pre_wrong_move_fraction") is not None else "—"})
    same_latency=[]
    for cores in (48,96,192):
        for target in (80,90):
            for latency in (.1,.3,1.,3.):
                r={"코어":cores,"목표U%":target,"L(s)":latency}
                for family in ("pre-oracle","pre-hist","bnd-oracle","bnd-class"):
                    name=f"{family}-{latency:g}s"
                    if family.startswith("bnd") and latency==.1:name=family+"-100ms"
                    x=row(cores,target,name)
                    r[family]=f(x.get("cpu_weighted_slowdown"))+" / "+f(x.get("heavy_command_slowdown_p99"))
                same_latency.append(r)
    stale=[]
    for cores in (48,96,192):
        for target in (80,90):
            for name in ("pre-oracle-0.1s","pre-oracle-1s","pre-oracle-3s","pre-hist-0.1s","pre-hist-1s","pre-hist-3s"):
                r=row(cores,target,name)
                if not r:continue
                stale.append({"코어":cores,"목표U%":target,"정책":name,"목적지 폭 부족%":f(100*r["pre_unfit_fraction"],1),
                              "실제 평균 폭 부족%":f(100*r["pre_actual_unfit_fraction"],1),
                              "타 예약도 반영한 부족%":f(100*r["pre_reserved_unfit_fraction"],1),
                              "초기 충분→부족%":f(100*r["pre_later_unfit_fraction"],1) if r["pre_later_unfit_fraction"] is not None else "—",
                              "명령까지 넘긴 이동%":f(100*r["pre_late_fraction"],1),"평균 초과(s)":f(r["pre_mean_late_s"])})
    slo_rows=[]
    for r in data["slos"]+data["tax_slos"]:
        if r["metric"]!="cpu_weighted_slowdown" or r["slo"]!=1.10:continue
        nodes=("≤" if r["node_ratio_status"]=="upper_bound" else "")+f(r["nodes_over_none"])
        if r.get("nodes_over_none_lower_bound_if_feasible") is not None:
            nodes="≥"+f(r["nodes_over_none_lower_bound_if_feasible"])+" (더 낮은 U에서 가능할 때)"
        slo_rows.append({"코어":r["cores"],"정책":r["variant"],"최대 유효U%":("≥" if r["status"]=="at_least" else "")+f(100*r["util"],1) if r["util"] is not None else "최저점에서도 실패",
                         "none 대비 노드":nodes,"상태":r["status"]})
    break_rows=[]
    for r in data["tax_break_even"]:
        if r["policy"]!="reactive":continue
        break_rows.append({"코어":r["cores"],"목표U%":r["target_util"],"DRAM 기준":r["baseline"],"지표":r["metric"],
                           "손익분기 τ":f(r["tau"]) if r["tau"] is not None else ("<" if r["status"]=="below_grid" else ">")+f(r.get("bound"))})
    sensitivity=[]
    for target in (80,90):
        for name in ("none","pool","pre-oracle-1s","pre-oracle-3s","pre-hist-1s","pre-hist-3s","bnd-class-1ms","reactive"):
            for gap in (1,4):
                r=row(96,target,name,gap)
                if r:
                    base,pool=row(96,target,"none",gap),row(96,target,"pool",gap)
                    recovery=(base["cpu_weighted_slowdown"]-r["cpu_weighted_slowdown"])/(base["cpu_weighted_slowdown"]-pool["cpu_weighted_slowdown"]) if base and pool else None
                    sensitivity.append({"목표U%":target,"정책":name,"gap 배수":gap,"실제U%":f(100*r["actual_util"],1),"sandbox/코어":f(r["sandbox_per_core"],2),"CPU 가중":f(r["cpu_weighted_slowdown"]),"heavy p99":f(r["heavy_command_slowdown_p99"]),
                        "CPU 회수%":f(100*recovery,1) if recovery is not None else "—","목적지 부족%":f(100*r["pre_unfit_fraction"],1) if r.get("pre_unfit_fraction") is not None else "—", "늦음%":f(100*r["pre_late_fraction"],1) if r.get("pre_late_fraction") is not None else "—"})
    inputs=data["inputs"]
    copy_state=[]
    for label,key in (("anon+file (MiB)","heavy_cpu_weighted_anon_file_bytes"),("anon+file+cum_write (MiB)","heavy_cpu_weighted_copy_bytes")):
        copy_state.append({"항목":label,**{q:f(inputs.get(key,{}).get(q,0)/2**20,1) for q in ("p10","p50","p90","p99")}})
    for gbps in (25,100):
        copy_state.append({"항목":f"{gbps}G 정지 (ms)",**{q:f(1000*(inputs.get("heavy_cpu_weighted_copy_bytes",{}).get(q,0)/(gbps*1e9/8)+.005),1) for q in ("p10","p50","p90","p99")}})
    prediction=[{"문턱":r["threshold"],**{k:f(100*r[k],1) for k in ("recall","precision","cpu_recall","predicted_fraction")}} for r in inputs.get("history",[])]
    predictor=data["metadata"][0]["data"]["predictors"][0] if data["metadata"] else {}
    if predictor:
        prediction.append({"문턱":"명령 문자열",**{k:f(100*predictor[k],1) for k in ("recall","precision","cpu_recall")},
                           "predicted_fraction":f(100*predictor["predicted_heavy"]/predictor["commands"],1)})
    hist30=[]
    for target in (80,90):
        for name in ("pre-hist-1s","pre-hist30-1s"):
            r=row(96,target,name)
            if r:hist30.append({"목표U%":target,"문턱":.3 if "hist30" in name else .2,"CPU 가중":f(r["cpu_weighted_slowdown"]),
                               "heavy p99":f(r["heavy_command_slowdown_p99"]),"이동/s":f(r["pre_moves_per_s"],2),"헛이동%":f(100*r["pre_wrong_move_fraction"],1)})
    conclusions=[]
    for cores in (48,96,192):
        base,pool=row(cores,90,"none"),row(cores,90,"pool")
        if not base or not pool:continue
        cells={}
        for name in ("pre-oracle-1s","pre-hist-1s","pre-hist-1s+bnd-class-1ms","bnd-copy-100G","bnd-oracle-1ms","reactive"):
            r=row(cores,90,name)
            if r:cells[name]=f(100*(base["cpu_weighted_slowdown"]-r["cpu_weighted_slowdown"])/(base["cpu_weighted_slowdown"]-pool["cpu_weighted_slowdown"]),1)
        conclusions.append({"코어":cores,**cells})
    prose=[]
    if len(conclusions)==3 and all("pre-hist-1s" in r and "pre-oracle-1s" in r for r in conclusions):
        prose.append("48/96/192코어·목표90%에서 pre-hist 1초는 pool 이득의 "+"/".join(r["pre-hist-1s"] for r in conclusions)+
                     "%, 미래를 아는 pre-oracle 1초도 "+"/".join(r["pre-oracle-1s"] for r in conclusions)+"%를 회수했다.")
        wrong=[row(c,90,"pre-hist-1s").get("pre_wrong_move_fraction") for c in (48,96,192)]
        prose.append("pre-hist 이동의 "+"/".join(f(100*x,1) for x in wrong)+"%는 헛이동이다.")
        gaps=[100*(row(c,90,"pre-oracle-1s")["cpu_weighted_slowdown_pool_recovered"]-row(c,90,"pre-hist-1s")["cpu_weighted_slowdown_pool_recovered"]) for c in (48,96,192)]
        stale_fraction=[row(c,90,"pre-oracle-1s")["pre_unfit_fraction"] for c in (48,96,192)]
        prose.append("이력을 oracle로 바꿨을 때 추가 회수는 "+"/".join(f(x,1) for x in gaps)+"%p다. Oracle도 미리 옮긴 heavy의 "+"/".join(f(100*x,1) for x in stale_fraction)+"%는 실행 시작 때 목적지에 평균 폭만큼 여유가 없었다.")
        combined_penalty=[row(c,90,"pre-hist-1s+bnd-class-1ms").get("cpu_weighted_slowdown",0)-row(c,90,"bnd-class-1ms")["cpu_weighted_slowdown"] for c in (48,96,192)]
        prose.append("pre-hist 뒤 시작 시점에 class 경계를 더해도, class1ms 단독보다 CPU 가중 slowdown이 각각 "+"/".join(f(x,4) for x in combined_penalty)+" 높았다.")
    tax_costs=[row(c,90,"cxl-hybrid-1.23") for c in (48,96,192)]
    if all(tax_costs):
        prose.append("τ=1.23의 CXL 결합은 CPU 가중 "+"/".join(f(r["cpu_weighted_slowdown"]) for r in tax_costs)+
                     ", DRAM oracle 경계1ms는 "+"/".join(f(row(c,90,"bnd-oracle-1ms")["cpu_weighted_slowdown"]) for c in (48,96,192))+"다.")
    thresholds=[get(data["tax_break_even"],cores=c,target_util=90,policy="hybrid",baseline="bnd-oracle-1ms",metric="cpu_weighted_slowdown") for c in (48,96,192)]
    if all(r.get("tau") is not None for r in thresholds):
        prose.append("CXL 결합이 DRAM 경계1ms와 같아지는 heavy tax는 측정 τ 사이 보간으로 약 +"+"/+".join(f(100*(r["tau"]-1),1) for r in thresholds)+"%였다. +23%를 감당할 여유는 보이지 않았다.")
    if all(row(c,90,"bnd-copy-100G") for c in (48,96,192)):
        fraction=[]
        for c in (48,96,192):
            none=row(c,90,"none")["cpu_weighted_slowdown"]
            fraction.append((none-row(c,90,"bnd-copy-100G")["cpu_weighted_slowdown"])/(none-row(c,90,"bnd-oracle-1ms")["cpu_weighted_slowdown"]))
        prose.append("100G 복사 하한은 경계1ms가 가져간 CPU 가중 이득의 "+"/".join(f(100*x,1) for x in fraction)+"%를 가져갔다.")
    tax_performance=[]
    for cores in (48,96,192):
        for policy in ("reactive","hybrid"):
            for tau in (1.,1.02,1.05,1.1,1.23):
                r=row(cores,90,f"cxl-{policy}-{tau:g}")
                if r:tax_performance.append({"코어":cores,"정책":policy,"τ":tau,"물리U%":f(100*r["actual_util"],1),
                    "유효U%":f(100*r["useful_util"],1),"CPU 가중":f(r["cpu_weighted_slowdown"]),"heavy p99":f(r["heavy_command_slowdown_p99"])})
    node_break_rows=[]
    for r in data["tax_node_break_even"]:
        value=f(r["tau"]) if r["tau"] is not None else (("<" if r["status"]=="below_grid" else ">")+f(r.get("bound")) if "grid" in r["status"] else "범위 끝 제약")
        if r["status"]=="bounded":value=(f(r["tau_lower"])+" < " if r["tau_lower"] is not None else "")+"τ ≤ "+f(r["tau_upper"])
        node_break_rows.append({"코어":r["cores"],"정책":r["policy"],"DRAM 기준":r["baseline"],"노드 수 손익분기 τ":value,"상태":r["status"]})
    runtime=data.get("execution",{})
    wall_rows=[{"stage":r["stage"],"입력 준비 제외 wall(s)":f(r["preparation_excluded_wall_s"],1)} for r in runtime.get("timings",[])]
    text=f"""
## 7. LLM 대기 중 이동, 경계 복사와 CXL tax (12번)

### 7.0 숫자로 보는 결론

목표90%에서 pool의 CPU 가중 이득 중 회수한 비율(%):

{markdown_table(conclusions,["코어","pre-oracle-1s","pre-hist-1s","pre-hist-1s+bnd-class-1ms","bnd-copy-100G","bnd-oracle-1ms","reactive"])}

{' '.join(prose)}

### 7.1 질문과 방법

LLM 대기 중 몇 초짜리 migration을 숨기면 명령 시작 때 빠른 이동이 필요한지 검사한다. pre-oracle은 다음 명령의 실제 heavy 여부/평균 폭을 알고, pre-hist는 **자기 trace 전체를 뺀** 다른 모든 trace의 이력 빈도를 쓴다. key는 직전 batch의 우선순위가 가장 높은 cmdkind, 직전 실제 heavy 여부, 앞의 heavy 존재다. 미관측 key는 다른 trace 전체 heavy 빈도로 대체한다. 폭은 10번 class와 같은 repo LOO 중앙값이다. 대기 동안 목적지 폭을 예약하고, 이동이 gap을 넘긴 시간만 명령 지연에 넣는다. 결합 정책은 실제 도착 후 명령 시작 때 다시 판단한다.

새 설정 {data['configs']}개, 완료 {data['completion']}, 미완료 seed 쌍 {len(data['incomplete_seed_pairs'])}개. 모두 cap30, 16노드, seed 1·2, ramp600초+측정5400초다. 10번 주 비교와 같은 uncensored timeout 10^6초를 쓴다. 기존 none/pool/경계/reactive 결과와 config ID는 재사용했다. 상세 표는 [q3.csv](q3.csv), [전체 표](q3-tables.md), [tax 손익분기](q3-tax-break-even.csv), [tax SLO](q3-tax-slo.csv)와 summary.q3에 있다. p99는 두 seed의 p99 평균이다.

이력 예측 정확도(%):

{markdown_table(prediction,["문턱","recall","precision","cpu_recall","predicted_fraction"])}

heavy 앞 gap p10/p50/p90={f(inputs.get('heavy_gap_s',{}).get('p10'),2)}/{f(inputs.get('heavy_gap_s',{}).get('p50'),2)}/{f(inputs.get('heavy_gap_s',{}).get('p90'),2)}초, 앞 gap≥3초인 heavy CPU는 {f(100*inputs.get('heavy_cpu_gap_at_least3_fraction',0),1)}%다. 이력은 실제 heavy 이력을 무료로 알며, 추가 학습/추론 비용은 없다.

### 7.2 같은 이동 지연에서 비교

각 셀은 **CPU 가중 / heavy p99**다. 0.3·3초 경계 및 class 1초도 같은 L 비교를 위해 추가했다. class100ms 등 기존 config는 다시 실행하지 않았다.

{markdown_table(same_latency,["코어","목표U%","L(s)","pre-oracle","pre-hist","bnd-oracle","bnd-class"])}

![대기 중 이동과 경계 이동](q3-idle.png)

주요 정책의 성능과 회수율. 음수 회수율도 그대로 표기한다.

{markdown_table(main,["코어","목표U%","정책","CPU 가중","heavy p99","pool 회수%","tail 회수%","pre 이동/s","경계 이동/s","reactive 이동/s","헛이동%"])}

### 7.3 오래된 view와 남은 정지

목적지 폭 부족은 **pre로 옮긴 실제 heavy 명령** 중 실제 명령 실행 시작 직전, 직전 step의 물리 수요를 뺀 여유가 예측 폭보다 작은 비율이다. 실제 평균 폭 부족도 따로 보인다. 다른 gap의 예약까지 뺀 값은 별도 pre_reserved_unfit_fraction이다. 초기 충분→부족은 선택 당시 그 폭을 수용했던 목적지만 분모로 한다. 따라서 선택 때부터 불충분했던 partial-fit을 오래된 view 탓으로 세지 않는다. L>gap이면 gap 끝 명목 시점 대신 이동 후 실행 가능 시점에서 검사한다. 명령까지 넘긴 이동은 모든 이동 명령 admission 중 L이 gap을 넘긴 비율, 평균 초과는 0초도 포함한다. 이동 수/헛이동은 측정 중 결정한 cohort, admission 지표는 측정 중 명령이 시작한 cohort라 경계에서 다를 수 있다.

{markdown_table(stale,["코어","목표U%","정책","목적지 폭 부족%","실제 평균 폭 부족%","타 예약도 반영한 부족%","초기 충분→부족%","명령까지 넘긴 이동%","평균 초과(s)"])}

### 7.4 CXL 없는 경계 복사의 유리한 하한

CSV의 batch를 정렬한 command_start 순서와 결합했다. 명령별 정지는 `(anon+file+cum_write)/(Gbps×10^9/8)+5ms`, 출발 CPU는 복사 초×1코어다. CSV에서 빠진 boot 264개는 복사 대상에서 제외했다. 그중 heavy는2개이며 전체 heavy CPU의 {f(100*inputs.get('missing_boot_heavy_cpu_fraction',0),4)}%다. 상태 크기는 추정하지 않았다. container 자료라 guest kernel/page cache를 빠뜨리고, 미리 띄운 VMM·전용 링크·동시 복사 대역폭 공유 없음·정지 중 추가 dirty 없음으로 **복사 방식에 유리한 하한**이다. CSV 해시는 summary.q3.inputs.state_sha256에 기록했다.

heavy CPU로 가중한 명령 시작 직전 상태와 정지 시간의 분위수:

{markdown_table(copy_state,["항목","p10","p50","p90","p99"])}

위 주요 정책 표의 bnd-copy 행이 성능 비교다. 새 DRAM 정책에는 3.3절의 네 SLO를 모두 계산했다. 결합100ms는 80/90% 두 점만 있어, 그 범위를 벗어나는 최대점은 하한으로만 표시한다. CPU 가중≤1.10에서 pre-hist/결합/복사 정책의 최대 실제 사용률을 3.3절 표에도 추가했다. 측정 범위 밖은 외삽하지 않는다.

### 7.5 CXL 실행 tax의 손익분기

heavy 수요 폭을 유지하고 시간과 CPU를 τ배, light는1.02배로 늘렸다. slowdown 분모와 heavy 판정은 **원래 DRAM 길이/CPU**다. CPU 가중의 가중치는 실제 tax가 포함된 CPU이며 DRAM CPU 가중도 q3.csv에 따로 기록한다. 10ms profile 끝부분은 올림하므로 시간축에 최대10ms의 추가 이산화 오차가 있다. heavy τ=1도 light1.02 tax를 내는 보간 기준점이다.

입력 명령 전체를 한 번씩 완료하고 시간축 올림을 무시한 CPU 가중 solo 하한은 아래와 같다. 실제 grid의 완료 명령 모집단과는 다르다. τ=1.23에서는 이 입력 혼합 자체가1.10을 넘으므로 부하 분산만으로 tax를 제거할 수 없다.

{markdown_table(data['tax_input_solo_floor'],['tau','cpu_weighted_solo_floor'])}

같은 목표 U에서는 DRAM 기준 sandbox 밀도를 고정했다. 물리 CPU 사용률은 tax 때문에 높아질 수 있다. 노드 수/SLO x축에는 각 segment의 served CPU를 tax로 나눈 **유효 DRAM 작업량 사용률**을 쓴다. 추가 CPU 소비를 처리량 증가로 세지 않는다. 노드 비는 U_none/U_X이며 ≥endpoint이면 노드 비는 상한이다. 제공 부하가 같아도 정책의 실제 사용률은 다르다.

![CXL tax와 DRAM 경계 배치](q3-tax.png)

목표90%에서 tax를 포함한 실제 slowdown과 두 사용률:

{markdown_table(tax_performance,["코어","정책","τ","물리U%","유효U%","CPU 가중","heavy p99"])}

아래는 reactive 단독 손익분기다. 결합의 동일 계산도 CSV/summary에 있다. 표는 sampled τ에서 처음 이득을 잃는 구간의 선형 보간이며, 범위 밖은 부등호로 적는다. 소수점 세 자리는 보간 위치이지 측정 정밀도가 아니다.

{markdown_table(break_rows,["코어","목표U%","DRAM 기준","지표","손익분기 τ"])}

CPU 가중≤1.10의 최대 유효 사용률과 none 대비 필요한 노드:

{markdown_table(slo_rows,["코어","정책","최대 유효U%","none 대비 노드","상태"])}

노드 수 기준 τ 손익분기는 양쪽 SLO 최대 사용률이 범위 안에서 식별된 점만 보간한다. 범위 끝(≥) 둘을 나눠 점 추정하지 않는다. 각 τ에서 DRAM 경계 대비 노드 비와 상·하한은 summary.q3.tax_slos.DRAM_node_comparisons에 있다.

{markdown_table(node_break_rows,["코어","정책","DRAM 기준","노드 수 손익분기 τ","상태"])}

### 7.6 긴 LLM gap 민감도와 한계

gap4는 목표 사용률을 유지하도록 밀도를 다시 정했다. 더 긴 idle에 정지를 숨기기 쉬운 효과와 오래된 view/예약 효과를 함께 포함한다.

{markdown_table(sensitivity,["목표U%","정책","gap 배수","실제U%","sandbox/코어","CPU 가중","heavy p99","CPU 회수%","목적지 부족%","늦음%"])}

문턱0.3 비교(96코어, L=1초):

{markdown_table(hist30,["목표U%","문턱","CPU 가중","heavy p99","이동/s","헛이동%"])}

이 분석은 관측한 264개 trace를 반복한 simulator 결과다. pre 이동 CPU는 1ms로, 기존 migration의 실제 CPU 비용보다 유리하게 설정했다. Oracle은 미래 명령을 아는 상한이다. 10ms 수요/profile 및 직전 step view의 한계, seed2의 불확실성, config별 완료 명령 모집단 차이도 남는다. τ는 CPU 폭을 바꾸지 않는 시간 팽창 가정이며 실제 CXL 메모리 계층을 모사하지 않는다. heavy에 대한 tax를 특정 하드웨어 수치로 그대로 일반화하지 않는다.

### 7.7 재현

단일 설정 benchmark는 {f(runtime.get('benchmark_wall_s'),2)}초였다. 실제 stage별 wall은 다음과 같고, 모든 seed/측정 길이를 유지했다. 첫 TAX 시도는 보간 잔차로0개 완료 후 중단됐으며 수정 후 재개했다. `boundary.py test`가 작업량 보존과 DRAM 분모/유효 CPU를 검사한다. 기본 13조건의 이전 버전 지표 완전 일치와 fleet/cap self-test도 확인했다.

{markdown_table(wall_rows,['stage','입력 준비 제외 wall(s)'])}

실행별 코드 해시와 NUMA/부하 기록은 summary.q3.metadata/execution, 자세한 서버 시작·종료와 한계는 `.codex-runs/12.last.md`에 기록했다. 모든 명령은 node0 bind/nice19 아래에서 실행했다. 익명 메모리는N0만 관측했지만 기존 공유 시스템 라이브러리 file page 일부는N1에 남아 있었으며, 이를 옮기거나 다른 프로세스에 개입하지 않았다.

```bash
export PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMBA_NUM_THREADS=1
run0() {{ numactl --cpunodebind=0 --membind=0 nice -n 19 "$@"; }}
r12=experiments/contention/.local/boundary12
for stage in A SLO TAX TAX-SLO MATCH HIST30 GAP; do
  stem=${{stage,,}}
  run0 .venv/bin/python3 experiments/contention/boundary.py grid --q3 "$stage" --grid "$r12/$stem-grid.json" --output "$r12/$stem.jsonl"
  run0 .venv/bin/python3 experiments/contention/boundary.py run --grid "$r12/$stem-grid.json" --output "$r12/$stem.jsonl" --jobs 24
done
run0 .venv/bin/python3 docs/results/boundary-vs-handoff-20261113/q3.py
run0 .venv/bin/python3 experiments/contention/fleet.py test
run0 .venv/bin/python3 experiments/contention/cap_sweep.py test
run0 .venv/bin/python3 experiments/contention/boundary.py test
```
"""
    return text


if __name__ == "__main__":
    analyze()
