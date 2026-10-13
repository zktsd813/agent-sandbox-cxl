"""Summarize one replay run without loading its large metrics CSV into memory."""

import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict
from contextlib import nullcontext
from pathlib import Path
from experiment import submission_fingerprint


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
            for field, column in (("max_sampled_anon_other_bytes", "anon_other_bytes"),
                                  ("max_sampled_file_other_bytes", "file_other_bytes"),
                                  ("max_sampled_swap_bytes", "swap_bytes"),
                                  ("max_sampled_oom_kill", "oom_kill")):
                session[field] = max(session[field], int(row[column]))
    flush_cycle()
    for name, session in summary["sessions"].items():
        own = [e for e in commands if e["session"] == name]
        session.update({"commands": len(own), "returncode_mismatches": sum(e["session"] == name for e in mismatches),
                        "submission_matches_source": submissions.get(name, {}).get("matches_source"),
                        "submission_marker_first_line": submissions.get(name, {}).get("marker_first_line"),
                        "submission_equivalent_except_warning_order": submissions.get(name, {}).get("equivalent_except_warning_order"),
                        "below_005_core_fraction": session["below_005_core_seconds"] / max(session["sampled_seconds"], 1e-9)})
        finish = ends.get(name) or failed.get(name)
        if name in starts and finish:
            session["cpu_time_s"] = (finish["final_resources"]["cpu_usec"] - starts[name]["initial_resources"]["cpu_usec"]) / 1e6
            session["duration_s"] = finish["elapsed_s"] - starts[name]["elapsed_s"]
            session["mean_cpu_cores"] = session["cpu_time_s"] / max(session["duration_s"], 1e-9)
            session["recorded_peak_memory_bytes"] = finish["final_resources"]["peak_bytes"]
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
        and all(not s["max_sampled_anon_other_bytes"] and not s["max_sampled_swap_bytes"]
                and not s["max_sampled_oom_kill"] and not s["max_sampled_pids_limit_events"] for s in summary["sessions"].values()))
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if bins:
        os.environ.setdefault("MPLCONFIGDIR", str(root.resolve() / ".matplotlib"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
        times = sorted(bins)
        axes[0].plot(times, [bins[t]["cpu"] / bins[t]["n"] for t in times])
        axes[1].plot(times, [bins[t]["memory"] / bins[t]["n"] for t in times])
        axes[2].step(actual_times, actual_counts, where="post", label="Docker exec calls")
        axes[2].plot(times, [bins[t]["residents"] / bins[t]["n"] for t in times], label="Measured resident containers")
        source_origin = manifest["source_origin_unix"]
        windows = [(t["first"] - source_origin if manifest["starts"] == "recorded" else 0,
                    (t["first"] - source_origin if manifest["starts"] == "recorded" else 0) + t["last"] - t["first"])
                   for t in manifest["traces"]]
        scheduled_times, scheduled_counts = overlap([(a * manifest["scale"], b * manifest["scale"]) for a, b in windows])
        axes[2].step(scheduled_times, scheduled_counts, where="post", label="Source interaction windows", alpha=.6)
        axes[0].set_ylabel("CPU cores used\n(1 s sample mean)")
        axes[1].set_ylabel("Total memory GiB\n(1 s sample mean)")
        axes[2].set_ylabel("Concurrency")
        axes[2].set_xlabel("Seconds since replay start")
        axes[2].legend(fontsize="small")
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
             "공유 파일 cache의 다른 node 페이지는 별도로 기록한다. 익명 메모리 제한 통과만으로 모든 공유 페이지까지 node 0임을 주장할 수 없다. 이 서버의 재생 결과만으로 DSec의 원본 운영 환경에서 발생한 burst를 검증했다고 볼 수 없다.", "",
             "## trace별 결과", "", "| trace | 명령 | 코드 불일치 | CPU초 | 최대 메모리 GiB | 0.05 core 미만 관측 시간 비율 | 다른 node 파일 cache 최대 MiB | 제출 일치 |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for s in sorted(sessions.values(), key=lambda s: s["trace"]):
        matched = s["submission_matches_source"]
        lines.append(f"| {s['trace']} | {s['commands']} | {s['returncode_mismatches']} | {s.get('cpu_time_s',0):.2f} | {s['sampled_peak_memory_bytes']/2**30:.3f} | {s['below_005_core_fraction']:.1%} | {s['max_sampled_file_other_bytes']/2**20:.3f} | {'미비교' if matched is None else ('일치' if matched else '불일치')} |")
    if "budget_kernel_peak_memory_gib" in summary:
        lines += ["", f"공유 메모리 상한은 {manifest['config']['aggregate_memory_limit_bytes']/2**30:.3f}GiB이고 kernel이 기록한 그룹 최대 사용량은 {summary['budget_kernel_peak_memory_gib']:.3f}GiB다. 작업별 상한을 모두 더한 크기만큼 메모리를 예약하지 않으며, 그룹 합계를 kernel이 제한한다. 그룹 최대값에는 준비 과정과 작은 budget holder 컨테이너가 포함된다."]
    lines += ["", "불일치 명령의 batch/action 번호, 원본·실제 종료 코드와 오류는 `summary.json`, 명령 출력은 세션별 `.log`, 실제 측정값은 `metrics.csv`에 있다.", ""]
    (root / "report.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    root = parser.parse_args().results
    summary = summarize(root)
    print(json.dumps({key: summary[key] for key in ("infrastructure_passed", "commands", "timeouts", "resident_peak",
                                                   "command_overlap_peak", "aggregate_sampled_peak_memory_gib")}, indent=2))
    print(f"Report: {root / 'report.md'}")


if __name__ == "__main__":
    main()
