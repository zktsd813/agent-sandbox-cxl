"""Summarize one replay run without loading its large metrics CSV into memory."""

import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from contextlib import nullcontext
from pathlib import Path
from experiment import read_perf, submission_fingerprint


def quantiles(values):
    values = sorted(values)
    if not values:
        return {key: 0 for key in ("p50", "p95", "p99", "max")}
    result = {"max": values[-1]}
    for key, fraction in (("p50", .5), ("p95", .95), ("p99", .99)):
        index = (len(values) - 1) * fraction
        lower, upper = math.floor(index), math.ceil(index)
        result[key] = values[lower] + (values[upper] - values[lower]) * (index - lower)
    return result


def overlap(intervals):
    changes = sorted([(start, 1) for start, end in intervals if end > start]
                     + [(end, -1) for start, end in intervals if end > start])
    times, counts, active = [0], [0], 0
    for at, change in changes:
        active += change
        times.append(at)
        counts.append(active)
    return times, counts


def summarize(root):
    manifest = json.loads((root / "manifest.json").read_text())
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    identities = {s["session"]: s for e in events if e["event"] == "replay_clock_start" for s in e["sessions"]}
    if not identities:
        identities = {t["name"]: {"trace": t["name"]} for t in manifest["traces"]}
    starts = {e["session"]: e for e in events if e["event"] == "session_start"}
    ends = {e["session"]: e for e in events if e["event"] == "session_end"}
    failed = {e["session"]: e for e in events if e["event"] == "session_failed"}
    submissions = {}
    sources = {t["name"]: t for t in manifest["traces"]}
    commands = [e for e in events if e["event"] == "command_end"]
    with (root / "commands.csv").open("w", newline="") as stream:
        fields = ("session", "batch", "action", "duration_s", "returncode", "expected_returncode", "timed_out")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(commands)
    for name in ends:
        source = sources.get(identities[name]["trace"], {})
        log = root / f"{name}.log"
        if source.get("source_submission_sha256") and log.exists():
            command = source.get("last_action_command")
            if command is None and Path(source["source"]).is_file():
                original = json.loads(Path(source["source"]).read_text())
                command = [a["command"] for m in original["messages"] for a in m.get("extra", {}).get("actions", [])][-1]
            last = next(e for e in reversed(commands) if e["session"] == name)
            block = log.read_text(errors="replace").rsplit(f"\n--- batch {last['batch']}, action {last['action']} ---\n", 1)[-1]
            if command is None or not block.startswith(command + "\n"):
                continue
            lines = block[len(command) + 1:].lstrip().splitlines(keepends=True)
            markers = [i for i, line in enumerate(lines) if line.strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
            if markers and last["returncode"] == 0:
                marker = markers[-1]
                fingerprint = submission_fingerprint("".join(lines[:marker] + lines[marker + 1:]))
                expected = source.get("source_submission_fingerprint")
                if expected is None and Path(source["source"]).is_file():
                    expected = submission_fingerprint(json.loads(Path(source["source"]).read_text())["info"]["submission"])
                equivalent = expected is not None and all(fingerprint[k] == expected[k]
                    for k in ("without_startup_warning_sha256", "startup_warning_count"))
                submissions[name] = {"matches_source": fingerprint["sha256"] == source["source_submission_sha256"],
                                     "equivalent_except_warning_order": equivalent,
                                     "marker_first_line": marker == 0, "extracted_from_log": True}
    mismatches = [e for e in commands if e["expected_returncode"] is not None
                  and e["returncode"] != e["expected_returncode"]]
    summary = {"schedule": manifest["schedule"], "starts": manifest["starts"],
               "source_exit_status_counts": dict(Counter(t.get("source_exit_status", "unknown") for t in manifest["traces"])),
               "setup": manifest["container_setup"], "config": manifest["config"],
               "errors": [e for e in events if e["event"] == "error"], "sessions": {},
               "completed_sessions": sorted(ends), "incomplete_sessions": sorted(set(identities) - set(ends)),
               "attempted_sessions": sorted({e["session"] for e in events if e["event"] in ("container_start", "session_start")}),
               "commands": len(commands), "returncode_checks": sum(e["expected_returncode"] is not None for e in commands),
               "command_starts_recorded": sum(e["event"] == "command_start" for e in events),
               "expected_commands": sum(t.get("command_count", 0) for t in manifest["traces"]),
               "returncode_mismatches": mismatches, "timeouts": sum(e.get("timed_out", False) for e in commands),
               "command_latency_s": quantiles([e["duration_s"] for e in commands]),
               "actual_span_s": max((e["elapsed_s"] for e in events), default=0),
               "observed_schedule_span_s": manifest["observed_schedule_span_s"],
               "lateness_s": quantiles([e["lateness_s"] for e in events if e["event"] == "batch_start"]),
               "admission_delay_s": quantiles([e["admission_delay_s"] for e in events if e["event"] == "container_start"])}
    previous, bins = {}, defaultdict(lambda: {"n": 0, "cpu": 0, "memory": 0, "residents": 0})
    cycle = None
    cycle_names = set()
    cycle_cpu = cycle_memory = cycle_residents = 0
    aggregate_peak_memory = resident_peak = 0

    def flush_cycle():
        nonlocal aggregate_peak_memory, resident_peak
        if cycle is None:
            return
        bucket = bins[int(cycle)]
        bucket["n"] += 1
        bucket["cpu"] += cycle_cpu
        bucket["memory"] += cycle_memory / 2**30
        bucket["residents"] += cycle_residents
        aggregate_peak_memory = max(aggregate_peak_memory, cycle_memory)
        resident_peak = max(resident_peak, cycle_residents)

    metrics = root / "metrics.csv"
    with (metrics.open() if metrics.exists() else nullcontext([])) as stream:
        for row in csv.DictReader(stream):
            name, at, cpu = row["session"], float(row["elapsed_s"]), int(row["cpu_usec"])
            new_cycle = float(row["sample_cycle_s"]) if "sample_cycle_s" in row else (cycle if cycle is not None and name not in cycle_names else at)
            if new_cycle != cycle:
                flush_cycle()
                cycle, cycle_cpu, cycle_memory, cycle_residents = new_cycle, 0, 0, 0
                cycle_names.clear()
            cycle_names.add(name)
            if name not in summary["sessions"]:
                summary["sessions"][name] = {"trace": identities.get(name, {}).get("trace", name),
                    "sampled_peak_memory_bytes": 0, "max_sampled_anon_other_bytes": 0,
                    "sampled_peak_anon_bytes": 0, "sampled_peak_file_bytes": 0,
                    "file_byte_seconds": 0, "file_target_byte_seconds": 0,
                    "max_sampled_file_other_bytes": 0, "max_sampled_swap_bytes": 0,
                    "max_sampled_oom_kill": 0, "sampled_seconds": 0,
                    "max_sampled_pids_limit_events": None, "max_sampled_pids_current": None,
                    "below_005_core_seconds": 0, "waiting_cpu_time_s": 0}
            session = summary["sessions"][name]
            if name in previous:
                last_at, last_cpu = previous[name]
                duration = at - last_at
                delta_cpu = max(0, cpu - last_cpu) / 1e6
                cores = delta_cpu / duration if duration > 0 else 0
                cycle_cpu += cores
                session["sampled_seconds"] += duration
                session["file_byte_seconds"] += int(row["file_bytes"]) * duration
                session["file_target_byte_seconds"] += int(row.get("file_target_bytes", int(row["file_bytes"]) - int(row["file_other_bytes"]))) * duration
                if cores < .05:
                    session["below_005_core_seconds"] += duration
                if row["phase"] in ("ready", "waiting"):
                    session["waiting_cpu_time_s"] += delta_cpu
            previous[name] = at, cpu
            if "pids_limit_events" in row:
                session["max_sampled_pids_limit_events"] = max(session["max_sampled_pids_limit_events"] or 0, int(row["pids_limit_events"]))
                session["max_sampled_pids_current"] = max(session["max_sampled_pids_current"] or 0, int(row["pids_current"]))
            memory = int(row["memory_bytes"])
            cycle_memory += memory
            cycle_residents += 1
            session["sampled_peak_memory_bytes"] = max(session["sampled_peak_memory_bytes"], memory)
            for kind in ("anon", "file"):
                session[f"sampled_peak_{kind}_bytes"] = max(session[f"sampled_peak_{kind}_bytes"], int(row[f"{kind}_bytes"]))
            for field, column in (("max_sampled_anon_other_bytes", "anon_other_bytes"),
                                  ("max_sampled_file_other_bytes", "file_other_bytes"),
                                  ("max_sampled_swap_bytes", "swap_bytes"),
                                  ("max_sampled_oom_kill", "oom_kill")):
                session[field] = max(session[field], int(row[column]))
    flush_cycle()
    for name, session in summary["sessions"].items():
        session["image_id"] = identities.get(name, {}).get("image_id")
        own = [e for e in commands if e["session"] == name]
        session.update({"commands": len(own), "returncode_mismatches": sum(e["session"] == name for e in mismatches),
                        "submission_matches_source": submissions.get(name, {}).get("matches_source"),
                        "submission_marker_first_line": submissions.get(name, {}).get("marker_first_line"),
                        "submission_equivalent_except_warning_order": submissions.get(name, {}).get("equivalent_except_warning_order"),
                        "below_005_core_fraction": session["below_005_core_seconds"] / max(session["sampled_seconds"], 1e-9)})
        session["mean_charged_file_target_fraction"] = session["file_target_byte_seconds"] / session["file_byte_seconds"] if session["file_byte_seconds"] else None
        finish = ends.get(name) or failed.get(name)
        if name in starts and finish:
            session["initial_anon_other_bytes"] = starts[name]["initial_resources"]["anon_other_bytes"]
            session["final_anon_other_bytes"] = finish["final_resources"]["anon_other_bytes"]
            session["cpu_time_s"] = (finish["final_resources"]["cpu_usec"] - starts[name]["initial_resources"]["cpu_usec"]) / 1e6
            session["duration_s"] = finish["elapsed_s"] - starts[name]["elapsed_s"]
            session["mean_cpu_cores"] = session["cpu_time_s"] / max(session["duration_s"], 1e-9)
            session["recorded_peak_memory_bytes"] = finish["final_resources"]["peak_bytes"]
            session["command_wall_time_s"] = sum(e["duration_s"] for e in own)
            session["command_wall_fraction"] = session["command_wall_time_s"] / max(session["duration_s"], 1e-9)
            for field in ("io_read_bytes", "io_write_bytes", "minor_and_major_faults", "major_faults",
                          "cpu_pressure_some_usec", "memory_pressure_some_usec", "io_pressure_some_usec"):
                if field in finish["final_resources"]:
                    session[field] = finish["final_resources"][field] - starts[name]["initial_resources"][field]
        perf_path = root / f"{name}.perf.jsonl"
        if perf_path.exists():
            session["perf"], rows = read_perf(perf_path)
            intervals = defaultdict(dict)
            for row in rows:
                if "interval" in row:
                    intervals[row["interval"]][row["event"]] = row
            origin = next((e["origin_s"] for e in events if e["event"] == "perf_start" and e["session"] == name), 0)
            with (root / f"{name}.perf.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("elapsed_s", "interval_s", "instructions", "cycles",
                    "llc_references", "llc_misses", "retired_load_l3_misses", "llc_mpki", "retired_load_l3_mpki", "min_running_percent"))
                writer.writeheader()
                previous_at = 0
                for at, group in sorted(intervals.items()):
                    counts = {event: float(row["counter-value"]) if not row["counter-value"].startswith("<") else 0
                              for event, row in group.items()}
                    instructions = counts.get("instructions", 0)
                    coverage = min(float(row["pcnt-running"]) for row in group.values())
                    valid = session["perf"]["valid"] and instructions > 0 and coverage >= 99
                    writer.writerow({"elapsed_s": origin + at, "interval_s": at - previous_at,
                        "instructions": instructions, "cycles": counts.get("cycles", 0),
                        "llc_references": counts.get("cache-references", 0), "llc_misses": counts.get("cache-misses", 0),
                        "retired_load_l3_misses": counts.get("retired_l3_misses", 0),
                        "llc_mpki": 1000 * counts.get("cache-misses", 0) / instructions if valid else "",
                        "retired_load_l3_mpki": 1000 * counts.get("retired_l3_misses", 0) / instructions if valid else "",
                        "min_running_percent": coverage})
                    previous_at = at
    actual_times, actual_counts = overlap([(e["started_s"], e["started_s"] + e["duration_s"]) for e in commands])
    summary.update({"resident_peak": resident_peak, "command_overlap_peak": max(actual_counts),
                    "aggregate_sampled_peak_memory_gib": aggregate_peak_memory / 2**30,
                    "total_cpu_time_s": sum(s.get("cpu_time_s", 0) for s in summary["sessions"].values())})
    summary["mean_cpu_cores_over_run"] = summary["total_cpu_time_s"] / max(summary["actual_span_s"], 1e-9)
    summary["peak_1s_mean_cpu_cores"] = max((b["cpu"] / b["n"] for b in bins.values()), default=0)
    summary["session_mean_cpu_cores"] = quantiles([s["mean_cpu_cores"] for s in summary["sessions"].values() if "mean_cpu_cores" in s])
    summary["session_below_005_core_fraction"] = quantiles([s["below_005_core_fraction"] for s in summary["sessions"].values()])
    budget_start = next((e for e in events if e["event"] == "budget_start"), None)
    budget_end = next((e for e in events if e["event"] == "budget_end"), None)
    if budget_start and budget_end:
        summary["budget_cpu_time_s"] = (budget_end["resources"]["cpu_usec"] - budget_start["resources"]["cpu_usec"]) / 1e6
        summary["budget_kernel_peak_memory_gib"] = budget_end["resources"]["peak_bytes"] / 2**30
        summary["budget_final_resources"] = budget_end["resources"]
    summary["infrastructure_passed"] = (bool(ends) and not summary["incomplete_sessions"] and not summary["errors"]
        and all(not (s.get("initial_anon_other_bytes", 0) or s.get("final_anon_other_bytes", 0)
                     if manifest["config"].get("numa_bind_helper") else s["max_sampled_anon_other_bytes"]) and not s["max_sampled_swap_bytes"]
                and not s["max_sampled_oom_kill"] and not s["max_sampled_pids_limit_events"] for s in summary["sessions"].values())
        and (not manifest.get("perf") or all(s.get("perf", {}).get("valid") for s in summary["sessions"].values())))
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if bins:
        os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axes = plt.subplots(4 if manifest.get("perf") else 3, 1, figsize=(12, 10), sharex=True)
        times = sorted(bins)
        axes[0].plot(times, [bins[t]["cpu"] / bins[t]["n"] for t in times])
        axes[1].plot(times, [bins[t]["memory"] / bins[t]["n"] for t in times])
        axes[2].step(actual_times, actual_counts, where="post", label="Docker exec calls")
        axes[2].plot(times, [bins[t]["residents"] / bins[t]["n"] for t in times], label="Measured resident containers")
        source_origin = manifest["source_origin_unix"]
        windows = [(t["first"] - source_origin if manifest["starts"] == "recorded" else 0,
                    (t["first"] - source_origin if manifest["starts"] == "recorded" else 0) + t["last"] - t["first"])
                   for t in manifest["traces"]]
        if manifest["schedule"] != "immediate":
            scheduled_times, scheduled_counts = overlap([(a * manifest["scale"], b * manifest["scale"]) for a, b in windows])
            axes[2].step(scheduled_times, scheduled_counts, where="post", label="Source interaction windows", alpha=.6)
        axes[0].set_ylabel("CPU cores used\n(1 s sample mean)")
        axes[1].set_ylabel("Total memory GiB\n(1 s sample mean)")
        axes[2].set_ylabel("Concurrency")
        axes[2].set_xlabel("Seconds since replay start")
        axes[2].legend(fontsize="small")
        if manifest.get("perf"):
            for path in root.glob("*.perf.csv"):
                with path.open() as stream:
                    measured = list(csv.DictReader(stream))
                axes[3].plot([float(row["elapsed_s"]) for row in measured],
                             [float(row["llc_mpki"]) if row["llc_mpki"] else float("nan") for row in measured], linewidth=.8)
            axes[3].set_ylabel("LLC MPKI\n(1 s intervals)")
            axes[3].set_xlabel("Seconds since replay start")
        figure.tight_layout()
        figure.savefig(root / "timeline.png", dpi=150)
        plt.close(figure)
    write_report(root, manifest, summary)
    return summary


def write_report(root, manifest, summary):
    sessions = summary["sessions"]
    checked_submissions = [s["submission_matches_source"] for s in sessions.values() if s["submission_matches_source"] is not None]
    equivalent_submissions = [s["submission_equivalent_except_warning_order"] for s in sessions.values()
                             if s["submission_equivalent_except_warning_order"] is not None]
    lines = ["# mini trace 재생 실험 보고서", "", "LLM 호출 없이 저장된 명령을 Docker에서 실제 실행하고 컨테이너 cgroup을 측정했다.", "",
             "| 항목 | 결과 |", "|---|---:|",
             f"| 선택 / 완료 trace | {len(manifest['traces'])} / {len(summary['completed_sessions'])} |",
             f"| 실험 기반 기능 검증 | {'통과' if summary['infrastructure_passed'] else '실패 또는 미완료'} |",
             f"| 명령 실행 / 원본 종료 코드 비교 / 불일치 | {summary['commands']} / {summary['returncode_checks']} / {len(summary['returncode_mismatches'])} |",
             f"| 명령 timeout | {summary['timeouts']} |",
             f"| 제출 내용 해시 비교 / 일치 | {len(checked_submissions)} / {sum(checked_submissions)} |",
             f"| 동일한 시작 경고의 출력 위치를 제외한 제출 내용 일치 | {sum(equivalent_submissions)} / {len(equivalent_submissions)} |",
             f"| 재생 시간 | {summary['actual_span_s']:.2f}초 |",
             f"| resident container / Docker exec 최대 동시 수 | {summary['resident_peak']} / {summary['command_overlap_peak']} |",
             f"| 컨테이너 CPU 시간 합계 / 실험 전체 평균 사용 core | {summary['total_cpu_time_s']:.2f}초 / {summary['mean_cpu_cores_over_run']:.3f} |",
             f"| 측정된 컨테이너 메모리 합계 최대 | {summary['aggregate_sampled_peak_memory_gib']:.3f}GiB |",
             f"| 명령 목표 시각 지연 p50 / p95 / p99 / 최대 | {summary['lateness_s']['p50']:.3f} / {summary['lateness_s']['p95']:.3f} / {summary['lateness_s']['p99']:.3f} / {summary['lateness_s']['max']:.3f}초 |", "",
             f"시간 배치: `{summary['schedule']}`, 시작: `{summary['starts']}`, 컨테이너 준비: `{summary['setup']}`, 시간 배율: {manifest['scale']}.",
             f"원본 trace 상태: {summary['source_exit_status_counts']}. Submitted는 정답 채점 결과가 아니다. 중단된 원본 trace도 기록된 명령 구간만 재생했다.",
             f"CPU pool `{manifest['config']['workload_cpus']}`를 모든 컨테이너가 공유하고, 메모리 node `{manifest['config']['memory_nodes']}`, 컨테이너당 {manifest['config']['memory_limit_bytes']/2**30:g}GiB를 적용했다.", "",
             "![CPU·메모리·동시성](timeline.png)", "", "## 관측", "",
             f"전체 구간의 평균 CPU 사용량은 {summary['mean_cpu_cores_over_run']:.3f} core이고, 1초 구간 샘플 평균의 최대는 {summary['peak_1s_mean_cpu_cores']:.3f} core였다. trace별 평균 CPU 사용량의 중앙값은 {summary['session_mean_cpu_cores']['p50']:.3f} core였다.",
             f"trace별로 0.05 core 미만을 사용한 관측 시간 비율의 중앙값은 {summary['session_below_005_core_fraction']['p50']:.1%}였다. 이는 샘플에서 계산한 낮은 CPU 사용 시간 비율이며, 명령이 없거나 모든 프로세스가 정지했다는 뜻은 아니다.",
             f"실제로 겹친 Docker exec 호출은 최대 {summary['command_overlap_peak']}개이고 resident container는 최대 {summary['resident_peak']}개였다. 명령 목표 시각 지연의 p95는 {summary['lateness_s']['p95']:.3f}초이며, 실행 환경이나 자원 경합으로 원본보다 느려진 명령은 다음 호출을 늦춘다.",
             "", "## 해석 범위", "",
             "기반 기능 통과는 선택한 세션이 종료되고 NUMA 익명 메모리 이탈·swap·OOM·runner 오류가 없었다는 뜻이다. 종료 코드 및 제출 내용의 원본 일치 여부는 별도로 확인해야 한다. SWE 정답 채점은 수행하지 않았다.",
             "제출 내용은 최종 명령의 출력에서 marker 한 줄만 제거하고 stderr를 포함한 나머지를 원본 submission 해시와 비교했다. marker 앞에 출력이 있는지도 별도로 기록한다. 재생 완료를 mini의 Submitted 판정과 같다고 취급하지 않는다.",
             "stdout/stderr 순서가 달라질 수 있어 `/root/.bashrc`의 누락된 conda.sh 경고 위치만 제외한 비교도 별도로 제공한다. 이 비교에서도 경고 횟수와 나머지 출력 내용은 모두 같아야 한다.",
             "원본 timestamp는 assistant 메시지 시각이며 sandbox 생성 시각이 아니다. 서로 다른 trace의 wall clock이 비교 가능하다고 가정했다. on-arrival 모드는 첫 명령 시각에 새 컨테이너를 생성하므로 준비 시간만큼 첫 명령이 늦을 수 있다. 슬롯이 부족한 경우 대기와 지연을 기록한다.",
             "CPU·메모리는 각각 설정된 주기로 관측하고 실제 샘플 간격을 사용한다. 그래프는 1초 구간의 샘플 평균이며 표의 최대 메모리는 샘플 주기에서 계산한 합계다. 명령 구간은 Docker CLI 대기 시간으로, CPU busy 시간과 같지 않다.",
             "mini와 같이 명령 timeout은 Docker CLI를 종료하고 -1로 기록한 뒤 다음 명령으로 진행한다. 컨테이너 안의 작업이 계속 실행될 수 있으므로 대기 구간의 CPU 사용도 측정했다. 세션 종료 시 컨테이너 전체를 제거한다.",
             "익명 메모리는 지정 node 배치를 검증한다. 공유 파일 cache의 다른 node 페이지는 별도로 기록하며, cgroup에 과금되지 않은 기존 공유 페이지의 접근 위치까지 측정하지는 않는다. 이 서버의 재생 결과만으로 DSec의 원본 운영 환경에서 발생한 burst를 검증했다고 볼 수 없다.", "",
             "## trace별 결과", "", "| trace | 명령 | 코드 불일치 | CPU초 | 최대 메모리 GiB | 0.05 core 미만 관측 시간 비율 | 다른 node 파일 cache 최대 MiB | 제출 일치 |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for s in sorted(sessions.values(), key=lambda s: s["trace"]):
        matched = s["submission_matches_source"]
        lines.append(f"| {s['trace']} | {s['commands']} | {s['returncode_mismatches']} | {s.get('cpu_time_s',0):.2f} | {s['sampled_peak_memory_bytes']/2**30:.3f} | {s['below_005_core_fraction']:.1%} | {s['max_sampled_file_other_bytes']/2**20:.3f} | {'미비교' if matched is None else ('일치' if matched else '불일치')} |")
    if "budget_kernel_peak_memory_gib" in summary:
        lines += ["", f"공유 메모리 상한은 {manifest['config']['aggregate_memory_limit_bytes']/2**30:.3f}GiB이고 kernel이 기록한 그룹 최대 사용량은 {summary['budget_kernel_peak_memory_gib']:.3f}GiB다. 작업별 상한을 모두 더한 크기만큼 메모리를 예약하지 않으며, 그룹 합계를 kernel이 제한한다. 그룹 최대값에는 준비 과정과 작은 budget holder 컨테이너가 포함된다."]
    if manifest.get("perf"):
        lines += ["", "## MPKI", "", "LLC MPKI = 1,000 × cache-misses / retired instructions. 별도로 retired load의 L3 miss MPKI도 제공한다.",
                  "`cache-misses`는 LONGEST_LAT_CACHE.MISS(0x2e/0x41)이며 data/code read, RFO, speculative access와 L1/L2 hardware prefetch를 포함한다. retired load L3 miss(0xd1/0x20)와 의미가 다르다.",
                  "컨테이너 cgroup에 속한 모든 프로세스의 user+kernel 실행을 workload CPU에서 집계한다. 컨테이너 준비·제거와 호스트 Docker daemon은 제외한다. 명령 사이에 계속 실행되는 컨테이너 작업은 포함한다.",
                  "한 event group에서 함께 측정하며 실행 비율 99% 미만이거나 카운터가 없으면 유효한 MPKI로 보고하지 않는다. trace 전체 MPKI는 카운터 합계의 비율이며 구간별 MPKI의 단순 평균이 아니다. instructions가 없는 구간의 MPKI는 비어 있다.",
                  f"이벤트 정의: [Intel 공식 문서]({manifest['perf']['event_reference']}). 구간별 값과 원시 카운터는 `.perf.csv` 및 `.perf.jsonl`에 저장했다.", "",
                  "| trace | LLC MPKI | retired load L3 MPKI | IPC | 카운터 유효 |", "|---|---:|---:|---:|---|"]
        for session in sessions.values():
            pmu = session.get("perf", {})
            lines.append(f"| {session['trace']} | {pmu.get('llc_mpki')} | {pmu.get('retired_load_l3_mpki')} | {pmu.get('ipc')} | {pmu.get('valid', False)} |")
    lines += ["", "불일치 명령의 batch/action 번호, 원본·실제 종료 코드와 오류는 `summary.json`, 명령 출력은 세션별 `.log`, 실제 측정값은 `metrics.csv`에 있다.", ""]
    (root / "report.md").write_text("\n".join(lines))


def summarize_suite(root):
    """Refresh aggregate results from finished children without re-reading their samples."""
    suite = json.loads((root / "suite.json").read_text())
    rows, valid_counts = [], Counter()
    for attempt in suite["attempts"]:
        path = root / attempt["directory"] / "summary.json"
        if not path.exists() or attempt["state"] == "running":
            continue
        summary = json.loads(path.read_text())
        for session in summary["sessions"].values():
            pmu = session.get("perf", {})
            row = {"index": attempt["index"], "trace": session["trace"], "repo": session["trace"].rsplit("-", 1)[0],
                   "image_id": session["image_id"],
                   "infrastructure_passed": summary["infrastructure_passed"], "duration_s": session.get("duration_s"),
                   "commands": session["commands"], "expected_commands": summary["expected_commands"],
                   "returncode_mismatches": session["returncode_mismatches"], "timeouts": summary["timeouts"],
                   "cpu_time_s": session.get("cpu_time_s"), "mean_cpu_cores": session.get("mean_cpu_cores"),
                   "peak_1s_cpu_cores": summary["peak_1s_mean_cpu_cores"],
                   "peak_memory_gib": (session.get("recorded_peak_memory_bytes") or session["sampled_peak_memory_bytes"]) / 2**30,
                   "peak_anon_gib": session["sampled_peak_anon_bytes"] / 2**30,
                   "peak_file_gib": session["sampled_peak_file_bytes"] / 2**30,
                   "low_cpu_time_fraction": session["below_005_core_fraction"],
                   "command_wall_fraction": session.get("command_wall_fraction"),
                   "waiting_cpu_time_s": session["waiting_cpu_time_s"],
                   "peak_pids": session["max_sampled_pids_current"],
                   "file_cache_other_node_mib": session["max_sampled_file_other_bytes"] / 2**20,
                   "mean_charged_file_target_fraction": session["mean_charged_file_target_fraction"],
                   "anon_other_node_bytes": session.get("final_anon_other_bytes", session["max_sampled_anon_other_bytes"]),
                   "peak_other_anon_including_runtime_bytes": session["max_sampled_anon_other_bytes"],
                   "memory_node": int(suite["config"]["memory_nodes"]),
                   "command_latency_p50_s": summary["command_latency_s"]["p50"],
                   "command_latency_p95_s": summary["command_latency_s"]["p95"],
                   "submission_equivalent": session["submission_equivalent_except_warning_order"],
                   "mpki_valid": pmu.get("valid", False), "llc_mpki": pmu.get("llc_mpki"),
                   "retired_load_l3_mpki": pmu.get("retired_load_l3_mpki"), "ipc": pmu.get("ipc")}
            row.update({key: session.get(key) for key in ("io_read_bytes", "io_write_bytes", "major_faults",
                "minor_and_major_faults", "cpu_pressure_some_usec", "memory_pressure_some_usec", "io_pressure_some_usec")})
            row.update({key.replace("-", "_"): value for key, value in pmu.get("counts", {}).items()})
            rows.append(row)
            if row["infrastructure_passed"] and row["mpki_valid"]:
                valid_counts.update(pmu["counts"])
    if rows:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with (root / "traces.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    valid = [row for row in rows if row["infrastructure_passed"]]
    summary = {"state": suite["state"], "selected": len(suite["traces"]), "analyzed": len(rows),
               "passed": len(valid), "failed": sum(a["state"] == "failed" for a in suite["attempts"]),
               "running_trace": next((a["trace"] for a in suite["attempts"] if a["state"] == "running"), None),
               "command_count": sum(row["commands"] for row in rows),
               "returncode_mismatches": sum(row["returncode_mismatches"] for row in rows),
               "distributions": {key: quantiles([row[key] for row in valid if row.get(key) is not None])
                    for key in ("duration_s", "cpu_time_s", "mean_cpu_cores", "peak_1s_cpu_cores", "peak_memory_gib",
                                "peak_anon_gib", "peak_file_gib", "low_cpu_time_fraction", "llc_mpki", "retired_load_l3_mpki", "ipc")},
               "mpki_valid_traces": sum(row["mpki_valid"] for row in valid),
               "instruction_weighted_llc_mpki": 1000 * valid_counts["cache-misses"] / valid_counts["instructions"]
                    if valid_counts["instructions"] else None}
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = ["# SWE trace 순차 실행 특성 분석", "",
             f"상태: **{suite['state']}**. 선택 {summary['selected']}개 / 분석 {summary['analyzed']}개 / 기반 검증 통과 {summary['passed']}개 / 실행 오류 {summary['failed']}개.",
             f"분석한 명령 {summary['command_count']}개, 원본 종료 코드와 불일치 {summary['returncode_mismatches']}개. 실행 중 trace: {summary['running_trace']}.", "",
             "각 trace를 한 번씩 독립 컨테이너에서 실행한다. 컨테이너 제거와 계측 종료가 끝난 뒤 다음 trace를 시작한다. 집계는 종료된 실행만 포함하고, 아래 분포는 기반 검증을 통과한 trace를 대상으로 한다.",
             f"CPU node {suite['config']['numa_node']}, memory node {suite['config']['memory_nodes']}, workload CPU {suite['config']['workload_cpus']}, controller CPU {suite['config']['controller_cpus']}, 컨테이너 메모리 상한 {suite['config']['memory_limit_bytes']/2**30:g}GiB, swap 0. 각 trace의 시계를 초기화하고 `{suite['schedule']}` 시간 정책과 {suite['scale']}배율을 적용한다.",
             "CPU 20ms, 메모리 100ms, PMU 1초 주기로 기록한다. CPU 최대는 1초 구간 샘플 평균, 메모리 최대는 kernel memory.peak이다. anon/file 최대는 별도 샘플 최대이므로 두 값을 더해 전체 최대와 비교하지 않는다.", "",
             "## 분포", "", "| 지표 | p50 | p95 | p99 | 최대 |", "|---|---:|---:|---:|---:|"]
    for key, stats in summary["distributions"].items():
        lines.append(f"| {key} | {stats['p50']:.4f} | {stats['p95']:.4f} | {stats['p99']:.4f} | {stats['max']:.4f} |")
    lines += ["", "## MPKI 해석", "",
              f"유효한 MPKI {summary['mpki_valid_traces']}개. 명령어 수로 가중한 전체 LLC MPKI: {summary['instruction_weighted_llc_mpki']}.",
              "LLC MPKI = 1,000 × LONGEST_LAT_CACHE.MISS / retired instructions. cache-misses(0x2e/0x41)는 data/code read, RFO, speculative access, L1/L2 prefetch를 포함한다. 별도 retired_load_l3_mpki는 MEM_LOAD_RETIRED.L3_MISS(0xd1/0x20) 기준이다. 두 값의 의미가 다르다.",
              "[Intel 이벤트 정의](https://perfmon-events.intel.com/platforms/emeraldrapids/core-events/core/). 같은 event group으로 컨테이너 cgroup의 user+kernel 실행만 측정한다. 카운터 실행 비율 99% 미만이거나 카운터가 누락되면 유효한 값으로 취급하지 않는다. instructions가 0인 구간은 MPKI를 계산하지 않는다.",
              "이 결과는 이 서버에서 재실행한 명령의 특성이다. 원본 LLM 추론의 CPU·메모리는 포함하지 않는다. 한 번씩 측정하므로 반복 간 변동이나 신뢰구간은 추정하지 않는다.",
              "다른 서버 작업과 CPU/cache를 독점하지 않는다. cgroup 필터는 다른 작업의 카운터 합산을 막지만 다른 작업이 유발하는 cache 경합까지 없애지는 않는다. 전역 cache를 비우지 않았으므로 이미지·파일 cache의 초기 상태는 실행 순서에 영향을 받을 수 있다.",
             "지정 NUMA node의 사용자 메모리 배치를 검증했다. write-page DRAM 실행은 컨테이너 cpuset을 선택한 단일 node로 제한한다. 기존 CXL 비교 실행은 cpuset 0,2와 payload MPOL_BIND를 사용해 비이동 커널 메모리를 node 0에 할당한다. 공유 파일 cache 통계는 해당 cgroup에 과금된 페이지 기준이다.", "",
              "## trace별 결과", "", "| trace | CPU초 | 평균 core | 최대 메모리 GiB | LLC MPKI | retired load L3 MPKI | 기반 검증 |", "|---|---:|---:|---:|---:|---:|---|"]
    for row in rows:
        lines.append(f"| {row['trace']} | {row['cpu_time_s']} | {row['mean_cpu_cores']} | {row['peak_memory_gib']:.3f} | {row['llc_mpki']} | {row['retired_load_l3_mpki']} | {row['infrastructure_passed']} |")
    lines += ["", "`traces.csv`에 trace별 CPU·메모리·MPKI·IPC·I/O·page fault·자원 대기·PID 최대·결과 일치 여부를 저장한다. 개별 디렉토리에는 원시 계측과 시계열 그래프·보고서를 저장한다.", ""]
    if valid:
        os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        figure, axes = plt.subplots(2, 2, figsize=(11, 8))
        for axis, key in zip(axes.flat, ("llc_mpki", "peak_memory_gib", "mean_cpu_cores", "low_cpu_time_fraction")):
            values = sorted(row[key] for row in valid if row.get(key) is not None)
            if values:
                axis.step(values, [(index + 1) / len(values) for index in range(len(values))], where="post")
            axis.set_xlabel(key)
            axis.set_ylabel("Fraction of completed traces")
            axis.grid(alpha=.3)
        figure.tight_layout()
        figure.savefig(root / "distributions.png", dpi=150)
        plt.close(figure)
        lines += ["![trace별 특성 분포](distributions.png)", ""]
    (root / "report.md").write_text("\n".join(lines))
    if suite["config"].get("write_tracking"):
        summarize_writes(root)
    return summary


def slowdown_cdf(root):
    """Plot a saved comparison CSV without requiring the original replay logs."""
    with (root / "paired-traces.csv").open() as stream:
        paired = list(csv.DictReader(stream))
    valid = [row for row in paired if row["eligible"] == "True"]
    if not valid:
        raise ValueError("No comparable trace pairs for a slowdown CDF")
    ratios = sorted(float(row["trace_latency_ratio_cxl_over_dram"]) for row in valid)
    if any(not math.isfinite(value) or value <= 0 for value in ratios):
        raise ValueError("Latency ratios must be finite and positive")
    changes = [100 * (value - 1) for value in ratios]
    stats = quantiles(changes)
    geomean = 100 * (math.exp(sum(math.log(value) for value in ratios) / len(ratios)) - 1)
    total = 100 * (sum(float(row["duration_s_node2"]) for row in valid)
                   / sum(float(row["duration_s_node0"]) for row in valid) - 1)
    excluded = Counter(row["excluded_reason"] for row in paired if row["eligible"] != "True")
    summary = {"source": "paired-traces.csv", "included_pairs": len(valid),
               "excluded_pairs": len(paired) - len(valid), "excluded_by_reason": dict(excluded),
               "definition": "100 * (CXL trace elapsed time / DRAM trace elapsed time - 1)",
               "geometric_mean_increase_percent": geomean, "total_elapsed_time_increase_percent": total,
               "percentiles_increase_percent": stats,
               "above_threshold_counts": {str(limit): sum(value > limit for value in changes)
                                          for limit in (0, 10, 20, 30, 50)}}
    (root / "slowdown-cdf.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (root / "slowdown-cdf.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("slowdown_percent", "cumulative_fraction"))
        writer.writerows((value, (index + 1) / len(changes)) for index, value in enumerate(changes))

    os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    figure, axis = plt.subplots(figsize=(7, 4.8))
    left, right = min(-5, math.floor(changes[0] / 10) * 10), math.ceil(changes[-1] / 10) * 10 + 5
    axis.step([left] + changes + [right], [0] + [100 * (i + 1) / len(changes)
              for i in range(len(changes))] + [100], where="post", linewidth=2.3,
              color="#2563eb", label="CXL vs DRAM")
    median = stats["p50"]
    axis.hlines(50, left, median, colors="#64748b", linestyles="--", linewidth=1)
    axis.vlines(median, 0, 50, colors="#64748b", linestyles="--", linewidth=1)
    axis.scatter([median], [50], color="#dc2626", s=32, zorder=4)
    axis.annotate(f"50%: {median:+.2f}%", (median, 50), xytext=(12, -22),
                  textcoords="offset points", color="#b91c1c")
    axis.set(xlim=(left, right), ylim=(0, 104), xlabel="Latency increase (%)",
             ylabel="CDF")
    axis.xaxis.set_major_formatter(PercentFormatter(100, decimals=0))
    axis.yaxis.set_major_formatter(PercentFormatter(100, decimals=0))
    axis.grid(alpha=.22)
    axis.legend(loc="lower right", fontsize=9)
    figure.tight_layout()
    for extension in ("png", "pdf"):
        figure.savefig(root / f"slowdown-cdf.{extension}", dpi=200, facecolor="white")
    plt.close(figure)
    return summary


def memory_cdf(root):
    """Plot observed per-container peaks, including traces with command timeouts."""
    os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    figure, axis = plt.subplots(figsize=(7, 4.8))
    summary = {"definition": "Per-container cgroup memory.peak / 1e6 (MB; charged memory, including file cache)",
               "scope": "DRAM containers only; command timeouts included; not simultaneous memory demand",
               "conditions": {}}
    points = []
    node, label, color = 0, "DRAM", "#2563eb"
    with (root / f"node{node}" / "traces.csv").open() as stream:
        rows = sorted(csv.DictReader(stream), key=lambda row: float(row["peak_memory_gib"]))
    values = [float(row["peak_memory_gib"]) * 2**30 / 1e6 for row in rows]
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError(f"node{node}: memory peaks must be finite and positive")
    summary["conditions"][f"node{node}"] = {
        "containers": len(rows), "min_mb": values[0], "mean_mb": sum(values) / len(values),
        "percentiles_mb": quantiles(values),
        "containers_with_timeouts": sum(int(row["timeouts"]) > 0 for row in rows),
        "infrastructure_failed": sum(row["infrastructure_passed"] != "True" for row in rows)}
    fractions = [(index + 1) / len(values) for index in range(len(values))]
    points.extend((node, row["trace"], value, fraction, row["timeouts"])
                  for row, value, fraction in zip(rows, values, fractions))
    axis.step([0] + values, [0] + fractions, where="post", linewidth=2,
              color=color, label=label)

    median = quantiles(values)["p50"]
    axis.hlines(.5, 0, median, colors="#64748b", linestyles="--", linewidth=1)
    axis.vlines(median, 0, .5, colors="#64748b", linestyles="--", linewidth=1)
    axis.scatter([median], [.5], color="#dc2626", s=32, zorder=4)
    axis.annotate(f"50%: {median:,.1f} MB", (median, .5), xytext=(14, -22),
                  textcoords="offset points", color="#b91c1c")
    axis.set(xlabel="Peak container memory (MB)", ylabel="CDF", xlim=(0, None), ylim=(0, 1.02))
    axis.yaxis.set_major_formatter(PercentFormatter(1))
    axis.ticklabel_format(axis="x", style="plain", useOffset=False)
    axis.grid(alpha=.25)
    axis.legend(loc="lower right", fontsize=9)
    figure.tight_layout()
    for extension in ("png", "pdf"):
        figure.savefig(root / f"memory-cdf.{extension}", dpi=200, facecolor="white")
    plt.close(figure)
    with (root / "memory-cdf.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("memory_node", "trace", "peak_memory_mb", "cumulative_fraction", "timeouts"))
        writer.writerows(points)
    (root / "memory-cdf.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def compare_runs(root):
    """Pair the same trace and batch/action across the two NUMA memory suites."""
    campaign = json.loads((root / "comparison.json").read_text())
    runs, suites = {}, {}
    for node in (0, 2):
        directory = root / f"node{node}"
        if (directory / "suite.json").exists():
            suites[node] = json.loads((directory / "suite.json").read_text())
        path = directory / "traces.csv"
        with (path.open() if path.exists() else nullcontext([])) as stream:
            runs[node] = {row["trace"]: row for row in csv.DictReader(stream)}
    paired, command_pairs = [], []
    for trace in runs[0].keys() & runs[2].keys():
        a, b = runs[0][trace], runs[2][trace]
        reasons = []
        if any(row["infrastructure_passed"] != "True" or row["mpki_valid"] != "True" for row in (a, b)):
            reasons.append("incomplete/resource/NUMA/PMU failure")
        if a["image_id"] != b["image_id"]:
            reasons.append("different image")
        children = [root / f"node{node}" / next(item["directory"] for item in suites[node]["attempts"] if item["trace"] == trace)
                    for node in (0, 2)]
        manifests = [json.loads((directory / "manifest.json").read_text()) for directory in children]
        if manifests[0]["traces"][0]["sha256"] != manifests[1]["traces"][0]["sha256"]:
            reasons.append("different source trace")
        if any(manifest["schedule"] != "immediate" for manifest in manifests):
            reasons.append("not immediate")
        for key in ("workload_cpus", "memory_limit_bytes", "pids_limit", "sampling", "numa_bind_sha256"):
            if manifests[0]["config"][key] != manifests[1]["config"][key]:
                reasons.append(f"different {key}")
        commands = []
        for directory in children:
            with (directory / "commands.csv").open() as stream:
                commands.append({(int(row["batch"]), int(row["action"])): row for row in csv.DictReader(stream)})
        if commands[0].keys() != commands[1].keys() or any(int(row["commands"]) != int(row["expected_commands"]) for row in (a, b)):
            reasons.append("incomplete command sequence")
        if any(commands[0][key]["returncode"] != commands[1][key]["returncode"] for key in commands[0].keys() & commands[1].keys()):
            reasons.append("different return codes")
        if any(int(row["timeouts"]) for row in (a, b)):
            reasons.append("timeout-censored latency")
        if manifests[0]["traces"][0].get("source_exit_status") == "Submitted" and any(row["submission_equivalent"] != "True" for row in (a, b)):
            reasons.append("submission diverged from source")
        eligible = not reasons
        row = {"index": int(a["index"]), "trace": trace, "eligible": eligible, "excluded_reason": "; ".join(reasons),
               "trace_latency_ratio_cxl_over_dram": float(b["duration_s"]) / float(a["duration_s"])
                    if a["duration_s"] and b["duration_s"] else None}
        for key in ("duration_s", "cpu_time_s", "mean_cpu_cores", "peak_memory_gib", "llc_mpki", "retired_load_l3_mpki", "ipc",
                    "mean_charged_file_target_fraction", "file_cache_other_node_mib", "anon_other_node_bytes"):
            row[key + "_node0"], row[key + "_node2"] = a.get(key), b.get(key)
        paired.append(row)
        for batch, action in sorted(commands[0].keys() & commands[1].keys()):
            left, right = commands[0][batch, action], commands[1][batch, action]
            dram_s, cxl_s = float(left["duration_s"]), float(right["duration_s"])
            command_pairs.append({"trace": trace, "batch": batch, "action": action, "eligible": eligible,
                                  "node0_s": dram_s, "node2_s": cxl_s, "ratio_cxl_over_dram": cxl_s / dram_s})
    paired.sort(key=lambda row: row["index"])
    for filename, rows in (("paired-traces.csv", paired), ("paired-commands.csv", command_pairs)):
        if rows:
            with (root / filename).open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0])
                writer.writeheader()
                writer.writerows(rows)
    valid = [row for row in paired if row["eligible"]]
    ratios = [row["trace_latency_ratio_cxl_over_dram"] for row in valid]
    valid_commands = [row for row in command_pairs if row["eligible"]]
    summary = {"state": campaign["state"], "selected_per_run": campaign["traces"],
               "finished_node0": len(runs[0]), "finished_node2": len(runs[2]), "paired": len(paired), "eligible_pairs": len(valid),
               "excluded_pairs": len(paired) - len(valid), "trace_latency_ratio_cxl_over_dram": quantiles(ratios) if ratios else None,
               "geomean_trace_latency_ratio": math.exp(sum(math.log(value) for value in ratios) / len(ratios)) if ratios else None,
               "total_paired_trace_latency_ratio": sum(float(row["duration_s_node2"]) for row in valid) / sum(float(row["duration_s_node0"]) for row in valid) if valid else None,
               "eligible_commands": len(valid_commands),
               "command_latency_ratio_cxl_over_dram": quantiles([row["ratio_cxl_over_dram"] for row in valid_commands]) if valid_commands else None}
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = ["# 대기 없는 SWE 실행: DRAM / CXL 메모리 비교", "",
             f"상태 **{summary['state']}**. node 0 memory 종료 {len(runs[0])}/{campaign['traces']}, node 2 memory 종료 {len(runs[2])}/{campaign['traces']}.",
             f"동일 trace 비교 {len(paired)}개, 비교 조건 통과 {len(valid)}개, 제외 {len(paired)-len(valid)}개.", "",
             "전체 trace를 node 0 CPU + node 0 memory에서 한 번씩 실행한 뒤, 같은 순서로 node 0 CPU + node 2(CXL) memory에서 한 번씩 실행한다. trace와 명령은 순차 실행하며 timestamp/gap 대기를 추가하지 않는다. 명령 내부 sleep과 timeout은 유지한다.",
             "latency는 호스트 monotonic clock으로 측정한 Docker exec 시작→종료 시간이다. Docker 제어 경로와 I/O 대기도 포함하며 CPU 실행 시간과 다르다. 종료 감지는 pidfd를 사용한다.",
             "비율은 CXL / DRAM으로, 1보다 크면 CXL run이 더 느리다. 같은 image·trace·CPU·자원 상한, 완료된 명령 수, 두 run의 종료 코드와 원본 제출 내용 일치를 확인한다. 자원/NUMA/PMU 실패와 timeout을 포함한 trace는 대표 latency 비교에서 제외하고 원시 결과를 보존한다.",
             f"trace latency 비율 분포: {summary['trace_latency_ratio_cxl_over_dram']}",
             f"trace별 비율 기하평균: {summary['geomean_trace_latency_ratio']}; 비교 trace 시간 합계 비율: {summary['total_paired_trace_latency_ratio']}",
             f"비교 가능한 명령 {len(valid_commands)}개; 명령 latency 비율 분포: {summary['command_latency_ratio_cxl_over_dram']}", "",
             "## 해석 범위", "",
             "각 payload를 정적 numa-bind helper의 set_mempolicy/get_mempolicy로 MPOL_BIND한 뒤 exec한다. node 2가 ZONE_MOVABLE만 제공하므로 cpuset은 0,2를 허용해 파이프 등 비이동 커널 메모리가 node 0을 사용할 수 있게 한다. 두 조건 모두 같은 helper·SYS_NICE capability·기본 seccomp를 사용한다. runc 초기화 중 일시적 node 0 익명 메모리는 최대값으로 기록하고 payload 시작/종료의 배치를 별도로 검증한다.",
             "기존 공유 image/file cache가 다른 node에 남을 수 있고 memory.numa_stat은 해당 cgroup에 과금된 페이지만 센다. 따라서 모든 파일 접근이 CXL을 거친다는 의미는 아니다. 파일 cache의 배치와 메모리 사용량을 함께 기록한다.",
             "node 0 run 후 node 2 run을 각각 한 번 수행한다. 전역 cache를 비우지 않고 다른 호스트 작업을 유지하므로 실행 순서·cache·시간에 따른 경합 영향을 분리한 인과 추정이나 반복 신뢰구간은 제공하지 않는다.", "",
             "## trace별 비교", "", "| trace | DRAM 초 | CXL 초 | CXL/DRAM | DRAM MPKI | CXL MPKI | 비교 유효 | 제외 이유 |", "|---|---:|---:|---:|---:|---:|---|---|"]
    for row in paired:
        lines.append(f"| {row['trace']} | {row['duration_s_node0']} | {row['duration_s_node2']} | {row['trace_latency_ratio_cxl_over_dram']} | {row['llc_mpki_node0']} | {row['llc_mpki_node2']} | {row['eligible']} | {row['excluded_reason']} |")
    lines += ["", "`paired-traces.csv`와 `paired-commands.csv`에 latency 비율을 저장한다. 각 run의 `traces.csv`와 개별 trace 디렉토리에는 CPU·메모리·MPKI·IPC·NUMA·page fault·I/O 원시 계측과 보고서를 저장한다.", ""]
    if valid:
        os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        figure, axes = plt.subplots(1, 2, figsize=(11, 4))
        values = sorted(ratios)
        axes[0].step(values, [(i+1)/len(values) for i in range(len(values))], where="post")
        axes[0].axvline(1, color="grey", linestyle="--")
        axes[0].set(xlabel="Trace latency: CXL / DRAM", ylabel="Fraction of eligible traces")
        axes[1].scatter([float(row["llc_mpki_node0"]) for row in valid], [float(row["llc_mpki_node2"]) for row in valid], s=20)
        axes[1].set(xlabel="DRAM LLC MPKI", ylabel="CXL LLC MPKI")
        figure.tight_layout()
        figure.savefig(root / "comparison.png", dpi=150)
        plt.close(figure)
        lines += ["![Latency / MPKI 비교](comparison.png)", ""]
        if campaign["state"] == "complete":
            slowdown_cdf(root)
            lines += ["![Trace slowdown CDF](slowdown-cdf.png)", ""]
    (root / "report.md").write_text("\n".join(lines))
    return summary


def summarize_writes(root):
    """Export observed physical write pages per tool call, without summing them as a trace-wide unique set."""
    config = json.loads((root / "config.json").read_text()) if (root / "config.json").exists() else {}
    rows = []
    for path in sorted(root.glob("*/events.jsonl")):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        trace = next(event["sessions"][0]["trace"] for event in events if event["event"] == "replay_clock_start")
        commands = {(event["batch"], event["action"]): event for event in events if event["event"] == "command_end"}
        for event in events:
            if event["event"] != "write_pages":
                continue
            command = commands.get((event["batch"], event["action"]))
            if command is None:
                continue
            rows.append({"trace": trace, "batch": event["batch"], "action": event["action"],
                "observed_pages": event["unique_observed_pages"], "observed_mb": event["unique_observed_bytes"] / 1e6,
                "write_fault_pages": event["write_fault_pages"], "buffered_write_pages": event["buffered_write_pages"],
                "valid": event["valid"], "timed_out": command["timed_out"], "duration_s": command["duration_s"],
                "collection_s": event["collection_s"],
                **{key: event.get(key, 0) for key in ("map_update_failures", "unresolved_faults", "unsupported_large_pages", "missed_return_probes")}})
    valid = [row for row in rows if row["valid"]]
    by_trace = defaultdict(list)
    for row in rows:
        by_trace[row["trace"]].append(row)
    traces = []
    for trace, calls in by_trace.items():
        measured = [call for call in calls if call["valid"]]
        stats = quantiles([call["observed_mb"] for call in measured])
        traces.append({"trace": trace, "measured_calls": len(calls), "valid_calls": len(measured),
                       "timeouts": sum(call["timed_out"] for call in calls),
                       **{f"{key}_tool_call_mb": value for key, value in stats.items()}})
    summary = {"traces": len(traces), "measured_calls": len(rows), "valid_calls": len(valid),
               "placement": {key: config[key] for key in ("numa_node", "controller_cpus", "workload_cpus", "memory_nodes", "memory_limit_bytes") if key in config},
               "invalid_calls": len(rows) - len(valid), "calls_with_timeouts": sum(row["timed_out"] for row in rows),
               "valid_tool_call_mb": quantiles([row["observed_mb"] for row in valid]),
               "valid_without_timeout_mb": quantiles([row["observed_mb"] for row in valid if not row["timed_out"]]),
               "all_observed_tool_call_mb": quantiles([row["observed_mb"] for row in rows]),
               "valid_tool_call_pages": quantiles([row["observed_pages"] for row in valid]),
               "definition": "Unique observed physical page addresses per tool-call window; 4096 bytes/page; repeated writes deduplicated",
               "scope": "Write faults after resetting existing mappings plus supported buffered write_end copied pages. Not all kernel writes, DMA, coherence traffic or dirty cache lines.",
               "limitations": "Write permission faults are write-triggered observations; coverage is incomplete. Freed/reused physical addresses count once within a call. THP disabled. No LLM wait replay. Timeout windows may include background tasks.",
               "errors": {key: sum(row[key] for row in rows) for key in (
                   "map_update_failures", "unresolved_faults", "unsupported_large_pages", "missed_return_probes")}}
    for filename, data in (("write-pages.csv", rows), ("write-traces.csv", traces)):
        if data:
            with (root / filename).open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(data[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(data)
    (root / "write-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    stats = summary["valid_tool_call_mb"]
    lines = ["# DRAM tool call write-page observations", "",
             f"측정 trace {len(traces)}개, tool call {len(rows)}개, 계측 검증 통과 {len(valid)}개, timeout {summary['calls_with_timeouts']}개.", "",
             f"배치: CPU node {config.get('numa_node')}, workload CPU {config.get('workload_cpus')}, memory node {config.get('memory_nodes')}. 유효하지 않은 call {summary['invalid_calls']}개는 아래 분포에서 제외하며 CSV에는 관측값과 오류를 보존한다.", "",
             "4 KiB 물리 페이지 주소를 tool call마다 중복 제거한다. 새 read-only 매핑은 write fault로 세지 않는다. 종료된 프로세스의 관측 페이지도 포함한다.", "",
             "| Tool call당 관측 write 범위 | MB |", "|---|---:|",
             *[f"| {key} | {value:,.3f} |" for key, value in stats.items()], "",
             f"계측 오류 합계: `{json.dumps(summary['errors'])}`. 전체 관측값 분포(불완전 call 포함)와 페이지 수 분포는 `write-summary.json`에 별도로 저장한다.", "",
             "이 값은 지원 경로에서 관측한 write 페이지 수다. 커널 내부의 모든 store, DMA, 실제 cache coherence 트래픽이나 경계 시점의 dirty cache line 수를 의미하지 않는다.",
             "write fault는 쓰기 권한을 얻으려는 접근이며, fault 이후 실제 명령의 retire까지 확인하는 방식은 아니다. raw 물리 주소는 결과에 내보내지 않는다.",
             "THP를 이 실행의 컨테이너 프로세스에서 비활성화했고 첫 write fault를 유도하므로 기존 latency 실험과 직접 비교하지 않는다. model wait 길이는 재현하지 않고 call 경계에서 수집한다.",
             "`write-pages.csv`는 call별 값, `write-traces.csv`는 trace별 call 분포다. 서로 다른 call의 페이지 수를 더한 값은 trace 전체의 고유 페이지 수가 아니다.", ""]
    (root / "write-report.md").write_text("\n".join(lines))
    return summary


def write_memory_ratios(root):
    """Normalize interval write observations by sampled container memory, not RSS."""
    from bisect import bisect_left, bisect_right

    rows = []
    for path in sorted(root.glob("*/events.jsonl")):
        directory = path.parent
        manifest = json.loads((directory / "manifest.json").read_text())
        period = manifest["config"]["sampling"]["memory_interval_ms"] / 1000
        events = [json.loads(line) for line in path.read_text().splitlines()]
        trace = next(e["sessions"][0]["trace"] for e in events if e["event"] == "replay_clock_start")
        commands = {(e["batch"], e["action"]): e for e in events if e["event"] == "command_end"}
        peak = next(iter(json.loads((directory / "summary.json").read_text())["sessions"].values()))["recorded_peak_memory_bytes"]
        times, memory = [], []
        last_read, previous = -math.inf, None
        with (directory / "metrics.csv").open() as stream:
            for sample in csv.DictReader(stream):
                cycle = float(sample["sample_cycle_s"])
                signature = tuple(int(sample[k]) for k in ("memory_bytes", "peak_bytes", "anon_bytes", "file_bytes"))
                # CPU-only rows reuse the preceding 100 ms memory snapshot.
                if cycle - last_read >= period - 1e-8:
                    times.append(float(sample["elapsed_s"]))
                    memory.append(signature[0])
                    last_read = cycle
                elif signature != previous:
                    raise ValueError(f"Could not reconstruct memory sampling clock: {directory}")
                previous = signature
        for event in events:
            if event["event"] != "write_pages":
                continue
            command = commands[(event["batch"], event["action"])]
            start, end = command["started_s"], command["started_s"] + command["duration_s"]
            left, right = bisect_left(times, start), bisect_right(times, end)
            maximum = max(memory[left:right], default=0)
            observed = event["unique_observed_bytes"]
            rows.append({"trace": trace, "batch": event["batch"], "action": event["action"],
                "valid": event["valid"], "timed_out": command["timed_out"], "started_s": start, "end_s": end,
                "observed_pages": event["unique_observed_pages"], "observed_bytes": observed,
                "memory_samples": right - left, "sampled_peak_memory_bytes": maximum,
                "first_memory_sample_s": times[left] if right > left else None,
                "last_memory_sample_s": times[right - 1] if right > left else None,
                "write_percent": 100 * observed / maximum if maximum else None,
                "trace_peak_memory_bytes": peak, "write_over_trace_peak_percent": 100 * observed / peak})
    valid = [row for row in rows if row["valid"]]
    available = [row for row in valid if row["write_percent"] is not None]
    if not available:
        raise ValueError("No valid write windows with positive memory samples")
    summary = {
        "analysis_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "definition": "100 * observed write pages * 4096 / maximum fresh memory.current sample inside the command window",
        "window": "command_end.started_s through started_s + duration_s; host monotonic seconds",
        "memory_sampling": "Fresh reads reconstructed from sample_cycle_s and configured memory interval; cached CPU-only rows excluded",
        "scope": "Per-tool-call distribution, valid write observations only; timeouts included unless stated otherwise",
        "limitations": "Not RSS or an instantaneous dirty-page fraction. Denominator includes charged file cache and kernel memory. Sampling can miss transient peaks; numerator includes freed pages, so ratios can exceed 100%.",
        "calls": len(rows), "valid_write_calls": len(valid), "invalid_write_calls": len(rows) - len(valid),
        "calls_with_ratio": len(available), "valid_calls_without_positive_memory_sample": len(valid) - len(available),
        "above_100_percent_calls": sum(row["write_percent"] > 100 for row in available),
        "write_percent": quantiles([row["write_percent"] for row in available]),
        "without_timeout_percent": quantiles([row["write_percent"] for row in available if not row["timed_out"]]),
        "over_trace_lifetime_peak_percent": quantiles([row["write_over_trace_peak_percent"] for row in valid]),
        "over_trace_lifetime_peak_same_calls_percent": quantiles([row["write_over_trace_peak_percent"] for row in available])}
    with (root / "write-memory-ratio.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (root / "write-memory-ratio.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_cdfs(root, include_pages=False)
    return summary


def write_cdfs(root, include_pages=True):
    """Replot portable call-level CSVs without raw logs or a workload replay."""
    os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    specs = [("write-memory-ratio.csv", "write_percent", "write-memory-ratio",
              "Observed write / sampled call memory peak (%)", "%")]
    if include_pages:
        specs.insert(0, ("write-pages.csv", "observed_mb", "write-pages-cdf",
                         "Observed write footprint per tool call (MB)", " MB"))
    summary = {"analysis_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "scope": "Valid tool calls; timeouts included; each call has equal weight",
               "display": "Linear axes, full observed range, interpolated median marked; not RSS or a resident dirty fraction",
               "plots": {}}
    for filename, column, output, label, unit in specs:
        source = root / filename
        with source.open() as stream:
            rows = list(csv.DictReader(stream))
        keys = {(row["trace"], row["batch"], row["action"]) for row in rows}
        if len(keys) != len(rows):
            raise ValueError(f"Duplicate tool calls in {filename}")
        valid = [row for row in rows if row["valid"] == "True"]
        values = sorted(float(row[column]) for row in valid if row[column] != "")
        if not values or any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError(f"Expected finite, nonnegative observations in {filename}")
        stats = quantiles(values)
        summary["plots"][output] = {
            "source": filename, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "column": column, "calls": len(values), "invalid_calls": len(rows) - len(valid),
            "valid_calls_without_value": len(valid) - len(values), "quantiles": stats}
        fractions = [(i + 1) / len(values) for i in range(len(values))]
        figure, axis = plt.subplots(figsize=(7, 4.8))
        axis.step([0] + values, [0] + fractions, where="post", color="#2563eb",
                  linewidth=2, label="DRAM")
        median = stats["p50"]
        axis.hlines(.5, 0, median, colors="#64748b", linestyles="--", linewidth=1)
        axis.vlines(median, 0, .5, colors="#64748b", linestyles="--", linewidth=1)
        axis.scatter([median], [.5], color="#dc2626", s=32, zorder=4)
        axis.annotate(f"50%: {median:,.2f}{unit}", (median, .5), xytext=(14, -22),
                      textcoords="offset points", color="#b91c1c")
        axis.set(xlabel=label, ylabel="CDF", xlim=(0, None), ylim=(0, 1.02))
        if unit == "%":
            axis.xaxis.set_major_formatter(PercentFormatter(100))
        else:
            axis.ticklabel_format(axis="x", style="plain", useOffset=False)
        axis.yaxis.set_major_formatter(PercentFormatter(1))
        axis.grid(alpha=.25)
        axis.legend(loc="lower right", fontsize=9)
        figure.tight_layout()
        for extension in ("png", "pdf"):
            figure.savefig(root / f"{output}.{extension}", dpi=200, facecolor="white")
        plt.close(figure)
    (root / "write-cdf.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def mpki_audit(root):
    """Check whether idle intervals or a few traces hide memory-intensive phases."""
    from bisect import bisect_right

    with (root / "paired-traces.csv").open() as stream:
        eligible = {row["trace"] for row in csv.DictReader(stream) if row["eligible"] == "True"}
    groups, checks = defaultdict(Counter), {}
    for node in (0, 2):
        directory = root / f"node{node}"
        with (directory / "traces.csv").open() as stream:
            traces = list(csv.DictReader(stream))
        expected = {key: sum(int(row[key]) for row in traces)
                    for key in ("instructions", "cycles", "cache_misses", "retired_l3_misses")}
        observed, lags = Counter(), []
        for path in sorted(directory.glob("*/*.perf.csv")):
            child = path.parent
            events = [json.loads(line) for line in (child / "events.jsonl").read_text().splitlines()]
            trace = next(event["sessions"][0]["trace"] for event in events if event["event"] == "replay_clock_start")
            start = next(event for event in events if event["event"] == "perf_start")
            lag = start["elapsed_s"] - start["origin_s"]
            lags.append(lag)
            with (child / "metrics.csv").open() as stream:
                samples = [(float(row["elapsed_s"]), int(row["cpu_usec"]) / 1e6)
                           for row in csv.DictReader(stream)]
            times = [sample[0] for sample in samples]

            def cpu_at(at):
                if at < times[0] or at > times[-1]:
                    return None
                index = min(bisect_right(times, at) - 1, len(samples) - 2)
                left, right = samples[index:index + 2]
                return left[1] + (right[1] - left[1]) * (at - left[0]) / (right[0] - left[0])

            with path.open() as stream:
                for row in csv.DictReader(stream):
                    counts = {key: float(row[column]) for key, column in (
                        ("instructions", "instructions"), ("cycles", "cycles"),
                        ("cache_misses", "llc_misses"), ("retired_l3_misses", "retired_load_l3_misses"))}
                    observed.update(counts)
                    if float(row["min_running_percent"]) < 99:
                        raise ValueError(f"Invalid PMU coverage: {path}")
                    end, duration = float(row["elapsed_s"]), float(row["interval_s"])
                    assert duration > 0
                    cpu_start, cpu_end = cpu_at(end - duration), cpu_at(end)
                    cores = (cpu_end - cpu_start) / duration if cpu_start is not None and cpu_end is not None else None
                    names = ["all_intervals"]
                    if counts["instructions"] / duration >= 1e8:
                        names.append("instructions_per_second_ge_100M")
                    if cores is not None:
                        names += [f"cpu_cores_ge_{cutoff:g}" for cutoff in (.05, .5, 1, 4) if cores >= cutoff]
                    # Perf's exact clock origin is uncertain within its startup/enable handshake.
                    shifted_start, shifted_end = cpu_at(end - duration + lag), cpu_at(end + lag)
                    if shifted_start is not None and shifted_end is not None and (shifted_end - shifted_start) / duration >= .5:
                        names.append("cpu_cores_ge_0.5_shifted")
                    for cohort in (["all", "latency_eligible"] if trace in eligible else ["all"]):
                        for name in names:
                            group = groups[f"node{node}/{cohort}/{name}"]
                            group.update(counts)
                            group.update(intervals=1, seconds=duration,
                                         cpu_unaligned_intervals=int(cores is None))
                            if counts["instructions"] > 0:
                                mpki = 1000 * counts["cache_misses"] / counts["instructions"]
                                for cutoff in (5, 10):
                                    if mpki >= cutoff:
                                        group.update({f"mpki_ge_{cutoff}_seconds": duration,
                                                      f"mpki_ge_{cutoff}_instructions": counts["instructions"],
                                                      f"mpki_ge_{cutoff}_intervals": 1})
        assert all(math.isclose(observed[key], expected[key], rel_tol=1e-10) for key in expected)
        checks[f"node{node}"] = {"traces": len(traces), "interval_counts_match_trace_totals": True,
            "perf_clock_uncertainty_s": quantiles(lags),
            "command_wall_fraction": sum(float(row["duration_s"]) * float(row["command_wall_fraction"]) for row in traces)
                / sum(float(row["duration_s"]) for row in traces),
            "waiting_cpu_time_s": sum(float(row["waiting_cpu_time_s"]) for row in traces)}
    rows = []
    for name, counts in sorted(groups.items()):
        instructions = counts["instructions"]
        rows.append({"group": name, **counts,
                     "llc_mpki": 1000 * counts["cache_misses"] / instructions if instructions else None,
                     "retired_load_l3_mpki": 1000 * counts["retired_l3_misses"] / instructions if instructions else None,
                     "ipc": instructions / counts["cycles"] if counts["cycles"] else None})
        baseline = groups[name.rsplit("/", 1)[0] + "/all_intervals"]
        rows[-1].update(instruction_fraction=instructions / baseline["instructions"],
                        elapsed_fraction=counts["seconds"] / baseline["seconds"])
        for cutoff in (5, 10):
            rows[-1][f"mpki_ge_{cutoff}_elapsed_fraction"] = counts[f"mpki_ge_{cutoff}_seconds"] / counts["seconds"]
            rows[-1][f"mpki_ge_{cutoff}_instruction_fraction"] = counts[f"mpki_ge_{cutoff}_instructions"] / instructions if instructions else None
    summary = {"definition": "MPKI is a ratio of summed counters, never a time average with idle zeros",
        "cpu_alignment": "20 ms cumulative CPU samples interpolated onto ~1 s PMU windows; shifted column tests startup clock uncertainty",
        "scope": "all includes command timeouts; latency_eligible uses the same 211 paired traces as latency analysis",
        "checks": checks, "groups": rows}
    (root / "mpki-audit.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (root / "mpki-audit.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(key for row in rows for key in row)), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    plots = parser.add_mutually_exclusive_group()
    plots.add_argument("--cdf", action="store_true", help="Replot paired-traces.csv without raw replay logs")
    plots.add_argument("--memory-cdf", action="store_true", help="Plot per-container peaks from node*/traces.csv")
    plots.add_argument("--mpki-audit", action="store_true", help="Audit active vs idle PMU intervals using raw samples")
    plots.add_argument("--write-summary", action="store_true", help="Summarize per-tool-call write-page events")
    plots.add_argument("--write-memory-ratio", action="store_true", help="Normalize write pages by sampled per-call container memory")
    plots.add_argument("--write-cdf", action="store_true", help="Plot write footprint and memory ratios from portable CSVs")
    arguments = parser.parse_args()
    root = arguments.results
    if arguments.write_cdf:
        summary = write_cdfs(root)
    elif arguments.write_memory_ratio:
        summary = write_memory_ratios(root)
    elif arguments.write_summary:
        summary = summarize_writes(root)
    elif arguments.mpki_audit:
        summary = mpki_audit(root)
    elif arguments.memory_cdf:
        summary = memory_cdf(root)
    elif arguments.cdf:
        summary = slowdown_cdf(root)
    elif (root / "comparison.json").exists():
        summary = compare_runs(root)
    else:
        summary = summarize_suite(root) if (root / "suite.json").exists() else summarize(root)
    print(json.dumps({key: value for key, value in summary.items() if isinstance(value, (bool, int, float, str))}, indent=2))
    output = "write-cdf.json" if arguments.write_cdf else "write-memory-ratio.png" if arguments.write_memory_ratio else "write-report.md" if arguments.write_summary else "mpki-audit.json" if arguments.mpki_audit else "memory-cdf.png" if arguments.memory_cdf else "slowdown-cdf.png" if arguments.cdf else "report.md"
    print(f"Output: {root / output}")


if __name__ == "__main__":
    main()
