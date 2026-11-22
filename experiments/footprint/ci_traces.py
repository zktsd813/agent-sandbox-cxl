#!/usr/bin/env python3
"""Code-interpreter (CI) sandboxes for the footprint measurement: math-RL trajectories (ReTool-SFT) replayed as
cells in one persistent Python kernel per microVM.

fetch:   download JoeYing/ReTool-SFT (2000 rows) as JSON pages from the Hugging Face datasets server.
convert: turn N trajectories with >= 3 code cells into mini-swe-agent .traj.json traces (what replay.load_trace and
         sandbox.make_job read) plus candidates-ci.json. Command 0 starts the kernel (ci/ci.sh start), command i
         writes cell i to the run directory and runs it in the kernel (ci/ci.sh run i). The recorded LLM wait before
         cell i is t0 + beta * output tokens of the assistant text that ends with the cell (reasoning plus code);
         tokens = characters / 4 (no tokenizer installed).
test:    replay traces on the host without a VM (same command protocol, kernel from ci/), plus protocol checks.
image:   build the root disk (Docker build -> export -> ext4 + image.json with sandbox.py's init stub).

The data is untrusted text; convert never executes it. test executes cells on the host: run it under `unshare -rn`.
"""

import argparse
import collections
import datetime
import glob
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOCAL = HERE / ".local"
DATA = LOCAL / "ci-data/retool"
TRACES = LOCAL / "ci-traces"
IMAGES = LOCAL / "images"
CANDIDATES = HERE / "candidates-ci.json"
CI = HERE / "ci"
DATASET = "JoeYing/ReTool-SFT"
IMAGE_KEY, TAG = "ci-python", "ci-python:1"
LLM_T0, LLM_BETA = 6.156, 0.02898  # sandbox.py --llm-model tokens fit (iter2 RL rollouts), s and s per output token
CHARS_PER_TOKEN = 4
CELL = re.compile(r"<code>\s*```python\n(.*?)```\s*</code>\s*<interpreter>(.*?)</interpreter>", re.S)
DELIM = "__CI_CELL__"
START = 'exec "${CI_HOME:-/opt/ci}/ci.sh" start'
IN_IMAGE = {"numpy", "sympy", "scipy", "mpmath"} | set(sys.stdlib_module_names)
RISKY = re.compile(r"\b(subprocess|os\.system|os\.popen|socket|requests|urllib|input\s*\(|open\s*\(|shutil|while\s+True)")

spec = importlib.util.spec_from_file_location("sandbox", REPO / "experiments/contention/sandbox.py")
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def fetch(args):
    DATA.mkdir(parents=True, exist_ok=True)
    info = json.loads(urllib.request.urlopen(f"https://huggingface.co/api/datasets/{DATASET}", timeout=30).read())
    base = f"https://datasets-server.huggingface.co/rows?dataset={DATASET}&config=default&split=train"
    total, offset = None, 0
    while total is None or offset < total:
        path = DATA / f"rows-{offset:04d}.json"
        if not path.exists():
            raw = urllib.request.urlopen(f"{base}&offset={offset}&length=100", timeout=60).read()
            path.write_bytes(raw)
        total = json.loads(path.read_text())["num_rows_total"]
        offset += 100
    sandbox.save_json(DATA / "manifest.json", {"dataset": DATASET, "revision": info["sha"], "rows": total,
                                               "fetched_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    print(f"{total} rows of {DATASET}@{info['sha'][:8]} in {DATA}")


def load_rows():
    return [r for path in sorted(glob.glob(str(DATA / "rows-*.json"))) for r in json.load(open(path))["rows"]]


def parse(text):
    """[(text generated before and including the cell, cell source, interpreter output)], final text; None when the
    markup is broken (a <code> or <interpreter> that is not part of one complete cell)."""
    cells, end = [], 0
    for match in CELL.finditer(text):
        code_end = text.index("</code>", match.end(1)) + len("</code>")
        cells.append((text[end:code_end], match.group(1), match.group(2)))
        end = match.end()
    if text.count("<code>") != len(cells) or text.count("<interpreter>") != len(cells):
        return None
    return cells, text[end:]


def cell_command(index, source):
    if DELIM in source.splitlines():
        raise ValueError(f"cell {index} contains the heredoc delimiter")
    source = source if source.endswith("\n") else source + "\n"
    return (f"cat > \"${{CI_RUN:-/tmp/ci}}/{index:03d}.py\" <<'{DELIM}'\n{source}{DELIM}\n"
            f'exec "${{CI_HOME:-/opt/ci}}/ci.sh" run {index:03d}\n')


def tokens(text):
    return round(len(text) / CHARS_PER_TOKEN)


def convert_one(row_idx, messages, revision):
    question, answer = messages[0]["content"], messages[1]["content"]
    cells, final = parse(answer)
    out = [{"role": "user", "content": question},
           {"role": "assistant", "content": "", "extra": {"actions": [{"command": START}], "timestamp": 0.0,
                                                          "tokens": {"context": 0, "output": 0}}},
           {"role": "tool", "content": "", "extra": {"returncode": 0, "timestamp": 0.0}}]
    t, context, waits = 0.0, len(question), []
    for i, (generated, source, output) in enumerate(cells, 1):
        wait = LLM_T0 + LLM_BETA * tokens(generated)
        waits.append(wait)
        t += wait
        out.append({"role": "assistant", "content": generated,
                    "extra": {"actions": [{"command": cell_command(i, source)}], "timestamp": round(t, 6),
                              "tokens": {"context": round(context / CHARS_PER_TOKEN), "output": tokens(generated)}}})
        out.append({"role": "tool", "content": output, "extra": {"returncode": 0, "timestamp": round(t, 6)}})
        context += len(generated) + len(output)
    final_wait = LLM_T0 + LLM_BETA * tokens(final)
    out.append({"role": "assistant", "content": final,  # no action: not replayed
                "extra": {"timestamp": round(t + final_wait, 6),
                          "tokens": {"context": round(context / CHARS_PER_TOKEN), "output": tokens(final)}}})
    environment = {"image": TAG, "cwd": "/work", "env": {}, "forward_env": [], "timeout": 90,
                   "interpreter": ["bash", "-c"]}
    trace = {"instance_id": f"retool-{row_idx:04d}", "trajectory_format": "retool-sft->mini-ci",
             "info": {"config": {"environment_type": "minisweagent.environments.docker.DockerEnvironment",
                                 "environment": environment},
                      "exit_status": "Submitted", "submission": "",
                      "source": {"dataset": DATASET, "revision": revision, "row": row_idx,
                                 "tokens": f"characters / {CHARS_PER_TOKEN}",
                                 "llm_wait": f"{LLM_T0} + {LLM_BETA} * output tokens"}},
             "messages": out}
    return trace, cells, waits, final_wait


def convert(args):
    revision = json.loads((DATA / "manifest.json").read_text())["revision"]
    usable = []
    for r in load_rows():
        parsed = parse(r["row"]["messages"][1]["content"])
        if parsed and len(parsed[0]) >= args.min_cells:
            usable.append(r)
    chosen = sorted(random.Random(args.seed).sample(usable, min(args.n, len(usable))), key=lambda r: r["row_idx"])
    TRACES.mkdir(parents=True, exist_ok=True)
    entries, all_waits, imports, risky, sizes = [], [], collections.Counter(), collections.Counter(), []
    for r in chosen:
        trace, cells, waits, final_wait = convert_one(r["row_idx"], r["row"]["messages"], revision)
        path = TRACES / f"{trace['instance_id']}.traj.json"
        path.write_text(json.dumps(trace))
        all_waits += waits
        for _, source, _ in cells:
            sizes.append(len(source))
            for m in re.findall(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", source, re.M):
                for name in (m[0] or m[1]).split(","):
                    imports[name.strip().split(".")[0].split(" ")[0]] += 1
            risky.update(set(x.split("(")[0].strip() for x in RISKY.findall(source)))
        entries.append({"name": trace["instance_id"], "trace": str(path.relative_to(REPO)), "image": TAG,
                        "image_key": IMAGE_KEY, "cwd": "/work", "setup": "", "family": "ci", "group": None,
                        "run": f"{DATASET}@{revision[:8]}", "commands": len(cells) + 1, "cells": len(cells),
                        "exit_status": "Submitted", "resolved": None,
                        "recorded_window_s": round(sum(waits), 3), "recorded_gap_s": round(sum(waits), 3),
                        "final_answer_wait_s": round(final_wait, 3),
                        "output_tokens": sum(tokens(g) for g, _, _ in cells), "memory_mib": args.memory_mib})
    sandbox.save_json(CANDIDATES, {"criteria": {"source": DATASET, "revision": revision, "rows": len(load_rows()),
                                                "usable": len(usable), "min_cells": args.min_cells, "n": args.n,
                                                "seed": args.seed, "group": "one sample per problem: null",
                                                "llm_wait_s": f"{LLM_T0} + {LLM_BETA} * output tokens (characters / "
                                                              f"{CHARS_PER_TOKEN}) of the text ending with the cell",
                                                "command_0": "start the kernel, no wait"},
                                   "traces": entries})
    windows = [e["recorded_window_s"] for e in entries]
    cells_per = collections.Counter(e["cells"] for e in entries)
    print(f"{len(entries)} of {len(usable)} usable trajectories (>= {args.min_cells} cells) -> {TRACES}, {CANDIDATES}")
    print(f"cells per trajectory {sorted(cells_per.items())}, total {sum(e['cells'] for e in entries)}")
    print(f"LLM wait per cell p10/p50/p90/max {percentile(all_waits, .1):.1f}/{percentile(all_waits, .5):.1f}/"
          f"{percentile(all_waits, .9):.1f}/{max(all_waits):.1f} s")
    print(f"trajectory length (sum of waits) p10/p50/p90/max {percentile(windows, .1):.0f}/{percentile(windows, .5):.0f}/"
          f"{percentile(windows, .9):.0f}/{max(windows):.0f} s")
    print(f"cell characters p50/p90/max {percentile(sizes, .5)}/{percentile(sizes, .9)}/{max(sizes)}")
    print(f"imports {imports.most_common()}")
    print(f"imports not in the image {sorted(set(imports) - IN_IMAGE)}; cells with risky patterns {dict(risky)}")


def cpu_busy(cpus, seconds=1.0):
    def snap():
        return {line.split()[0]: [int(x) for x in line.split()[1:]] for line in open("/proc/stat")
                if line.startswith("cpu") and line[3].isdigit()}
    a = snap()
    time.sleep(seconds)
    b = snap()
    busy = {}
    for cpu in cpus:
        d = [y - x for x, y in zip(a[f"cpu{cpu}"], b[f"cpu{cpu}"])]
        busy[cpu] = 1 - (d[3] + d[4]) / max(1, sum(d))
    return busy


def replay_local(commands, directory, timeout, extra_env=None):
    """guest-init.sh's command loop on the host (no waits): busybox timeout and env -i, kernel and client from ci/."""
    work = directory / "work"
    work.mkdir(parents=True)
    run_dir = directory / "ci"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(directory), "LANG": "C.UTF-8",
           "CI_HOME": str(CI), "CI_RUN": str(run_dir), **(extra_env or {})}
    rows = []
    try:
        for i, command in enumerate(commands):
            t0 = time.monotonic()
            p = subprocess.run([sandbox.BUSYBOX, "timeout", "-s", "KILL", str(timeout), sandbox.BUSYBOX, "env", "-i",
                                *[f"{k}={v}" for k, v in env.items()], "bash", "-c", command],
                               cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
            done = run_dir / f"{i:03d}.done"
            rows.append({"index": i, "rc": p.returncode, "command_s": time.monotonic() - t0,
                         "done": done.read_text().split() if done.exists() else None, "output": p.stdout})
    finally:
        pid_file = run_dir / "kernel.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass
    return rows


PROTOCOL = [  # (cell, expected rc, expected output fragment)
    ("x = 41", 0, ""),
    ("x += 1\nprint(x)", 0, "42"),
    ("x * 2", 0, "84"),
    ("1 / 0", 1, "ZeroDivisionError"),
    ("print(x)", 0, "42"),
    ("try:\n    while True:\n        pass\nexcept Exception:\n    print('swallowed')", 124, "CellTimeout"),
    ("print(x)", 0, "42"),
    ("import sys\nsys.exit(3)", 1, "SystemExit"),
    ("print('x' in globals())", 0, "True"),
    ("i = 0\nwhile True:\n    print(i)\n    i += 1", 1, ""),  # stops at CI_MAX_FILE; no room left for the traceback
    ("sum(range(10**13))", 125, "kernel restarted"),  # C loop: the alarm cannot interrupt it
    ("print('x' in globals())", 0, "False"),
    ("import os\nos._exit(3)", 125, "kernel restarted"),
    ("print('alive')", 0, "alive"),
]


def test(args):
    cpus = [int(c) for c in args.cpus.split(",")]
    busy = cpu_busy(cpus)
    if max(busy.values()) > 0.1:
        raise SystemExit(f"CPUs not idle: {busy}")
    os.sched_setaffinity(0, cpus)
    root = Path(tempfile.mkdtemp(prefix="ci-test-", dir=args.dir))
    print(f"CPUs {cpus} (busy {max(busy.values()):.1%} before), run directories under {root}")
    if args.protocol:
        commands = [START] + [cell_command(i, c) for i, (c, _, _) in enumerate(PROTOCOL, 1)]
        rows = replay_local(commands, root / "protocol", 60,
                            {"CI_CELL_TIMEOUT": "2", "CI_WAIT": "5", "CI_MAX_FILE": str(1 << 20)})
        failed = 0
        for row, (cell, rc, fragment) in zip(rows[1:], PROTOCOL):
            ok = row["rc"] == rc and fragment in row["output"]
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} rc {row['rc']:3d} (want {rc:3d}) {row['command_s']:6.2f} s  "
                  f"{cell.splitlines()[-1][:40]!r:44} -> {row['output'].strip().splitlines()[-1][:60] if row['output'].strip() else ''!r}")
        print(f"protocol: {len(PROTOCOL) - failed}/{len(PROTOCOL)} as expected; start rc {rows[0]['rc']}")
    entries = json.loads(CANDIDATES.read_text())["traces"]
    names = args.names or [e["name"] for e in entries[:args.traces or None]]
    cells, peaks, statuses, same = [], [], collections.Counter(), 0
    for entry in (e for e in entries if e["name"] in names):
        trace = sandbox.replay.load_trace(str(REPO / entry["trace"]))
        commands = [a["command"] for b in trace["batches"] for a in b["actions"]]
        recorded = [o["content"] for o in json.loads(Path(trace["source"]).read_text())["messages"] if o["role"] == "tool"]
        rows = replay_local(commands, root / entry["name"], 90)
        start = rows[0]
        if not args.quiet:
            print(f"{entry['name']}: {len(rows) - 1} cells, start rc {start['rc']} {start['command_s'] * 1e3:.0f} ms, "
                  f"gaps {[round(b['gap'], 1) for b in trace['batches']]}")
        peak = 0
        for row, want in zip(rows[1:], recorded[1:]):
            status, wall, cpu, maxrss = ((row["done"] or ["missing"]) + [0, 0, 0])[:4]  # "lost": no timing
            statuses[status] += 1
            same += row["output"].strip() == want.strip()
            cells.append((float(wall), float(cpu), row["command_s"]))
            peak = max(peak, int(maxrss))
            if not args.quiet or status != "ok":
                print(f"  {entry['name']} cell {row['index']}: rc {row['rc']} {status} cell {float(wall) * 1e3:7.1f} ms, "
                      f"cpu {float(cpu) * 1e3:7.1f} ms, command {row['command_s'] * 1e3:7.1f} ms, kernel maxrss "
                      f"{int(maxrss) / 1024:.0f} MiB, output {'same' if row['output'].strip() == want.strip() else 'differs'}: "
                      f"{row['output'].strip()[:50]!r}")
        peaks.append(peak / 1024)
    if cells:
        print(f"{len(peaks)} trajectories, {len(cells)} cells, status {dict(statuses)}, output same as recorded "
              f"{same}/{len(cells)}")
        for label, k in (("cell wall", 0), ("cell cpu", 1), ("command", 2)):
            v = [c[k] * 1e3 for c in cells]
            print(f"{label} ms p50/p90/p99/max {percentile(v, .5):.1f}/{percentile(v, .9):.1f}/{percentile(v, .99):.1f}/"
                  f"{max(v):.1f}, sum {sum(v) / 1e3:.1f} s")
        print(f"kernel peak RSS per trajectory MiB p50/p90/max {percentile(peaks, .5):.0f}/{percentile(peaks, .9):.0f}/"
              f"{max(peaks):.0f}")
    if not args.keep:
        shutil.rmtree(root)


def image(args):
    """Root disk like sandbox.build_image, from ci/Dockerfile instead of a registry image (needs Docker and pip
    network access during `docker build`)."""
    out = IMAGES / IMAGE_KEY
    if (out / "image.json").exists():
        raise SystemExit(f"{out} exists; remove it to rebuild")
    work = IMAGES / (IMAGE_KEY + ".partial")
    if work.exists():
        sandbox.run(["chmod", "-R", "u+w", work])
        shutil.rmtree(work)
    work.mkdir(parents=True)
    started = time.monotonic()
    sandbox.run(["docker", "build", "-q", "-t", TAG, str(CI)], stdout=subprocess.DEVNULL)
    inspect = json.loads(sandbox.output(["docker", "image", "inspect", TAG]))[0]
    container = sandbox.output(["docker", "create", TAG]).strip()
    try:
        sandbox.run(["docker", "export", "-o", str(work / "rootfs.tar"), container])
    finally:
        sandbox.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL)
    if not args.keep_image:
        sandbox.run(["docker", "image", "rm", TAG], stdout=subprocess.DEVNULL)
    tar_bytes = (work / "rootfs.tar").stat().st_size
    disk_bytes = -(-int(tar_bytes * 1.25 + 2 * sandbox.GIB) // sandbox.GIB) * sandbox.GIB
    (work / "init").write_text(sandbox.INIT_STUB)
    script = "\n".join([  # as in sandbox.build_image
        "set -e", "mkdir root", "tar --numeric-owner -xpf rootfs.tar -C root",
        "mkdir -p root/microvm", f"cp {shlex.quote(str(sandbox.BUSYBOX))} root/microvm/busybox",
        "mv init root/microvm/init",
        "chown 0:0 root/microvm root/microvm/busybox root/microvm/init",
        "chmod 755 root/microvm/busybox root/microvm/init",
        f"truncate -s {disk_bytes} rootfs.ext4", "mkfs.ext4 -q -F -L swe-root -d root rootfs.ext4"])
    sandbox.run(["fakeroot", "sh", "-c", script], cwd=work)
    sandbox.run(["e2fsck", "-fn", str(work / "rootfs.ext4")], stdout=subprocess.DEVNULL)
    sandbox.run(["chmod", "-R", "u+w", work / "root"])
    shutil.rmtree(work / "root")
    (work / "rootfs.tar").unlink()
    config = inspect["Config"]
    sandbox.save_json(work / "image.json", {
        "name": IMAGE_KEY, "repo_digest": TAG, "image_id": inspect["Id"], "repo_digests": [],
        "dockerfile_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(CI.iterdir())},
        "setup": "", "env": config.get("Env") or [], "working_dir": config.get("WorkingDir"),
        "export_tar_bytes": tar_bytes, "disk_bytes": disk_bytes,
        "disk_allocated_bytes": (work / "rootfs.ext4").stat().st_blocks * 512, "init_stub": sandbox.INIT_STUB,
        "busybox": str(sandbox.BUSYBOX), "build_s": round(time.monotonic() - started, 1),
        "built_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    work.rename(out)
    print(f"{out}: {tar_bytes / sandbox.GIB:.2f} GiB export, {disk_bytes // sandbox.GIB} GiB disk, "
          f"{time.monotonic() - started:.0f} s")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch").set_defaults(func=fetch)
    p = sub.add_parser("convert")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--min-cells", type=int, default=3)
    p.add_argument("--seed", default="ci-select-1")
    p.add_argument("--memory-mib", type=int, default=1024)
    p.set_defaults(func=convert)
    p = sub.add_parser("test")
    p.add_argument("names", nargs="*")
    p.add_argument("--traces", type=int, default=3, help="Without names: the first N candidates (0: all)")
    p.add_argument("--quiet", action="store_true", help="Print only cells that did not finish ok, and the summary")
    p.add_argument("--protocol", action="store_true", help="Also check state, errors, timeouts and kernel restarts")
    p.add_argument("--cpus", default="28,29")
    p.add_argument("--dir", default=None, help="Parent of the temporary run directories")
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=test)
    p = sub.add_parser("image")
    p.add_argument("--keep-image", action="store_true", help="Keep the Docker image after export")
    p.set_defaults(func=image)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
