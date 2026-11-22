#!/usr/bin/env python3
"""Persistent code-interpreter kernel for the CI sandbox replay: one per sandbox, started by `ci.sh start`.

Cells run in one globals dict, so state (and its memory) persists across cells as in a Jupyter kernel.
Protocol in the run directory R (argv[1], default /tmp/ci); the kernel holds both FIFOs open read-write, so no
open blocks and nothing waits by polling:
  client writes the cell to R/NNN.py, then "NNN\\n" to R/req; the kernel runs it, renames R/NNN.done into place and
  writes "NNN STATUS\\n" to R/resp, which only wakes the client (the .done file is the answer).
  R/NNN.out  the cell's stdout and stderr (file descriptors 1 and 2, so child processes are included)
  R/NNN.done "STATUS wall_s cpu_s maxrss_kib"; STATUS is ok, error (exception; the kernel keeps running) or
             timeout (per-cell alarm, CI_CELL_TIMEOUT s, default 30)
A request whose .done already exists is skipped (stale request after a restart).
"""

import ast
import os
import resource
import signal
import sys
import time
import traceback

RUN = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ci"
CELL_TIMEOUT = float(os.environ.get("CI_CELL_TIMEOUT", "30"))
# A runaway print loop stops at this file size (default 64 MiB) instead of filling the disk.
MAX_FILE = int(os.environ.get("CI_MAX_FILE", 64 << 20))


class CellTimeout(BaseException):
    """Not an Exception, so `except Exception` inside a cell does not swallow it."""


def on_alarm(signum, frame):
    raise CellTimeout(f"cell exceeded {CELL_TIMEOUT:g} s")


def execute(source, namespace):
    tree = ast.parse(source, "<cell>")
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    exec(compile(tree, "<cell>", "exec"), namespace)
    if last is not None:  # as in a notebook: show the value of a trailing expression
        value = eval(compile(ast.Expression(last.value), "<cell>", "eval"), namespace)
        if value is not None:
            print(repr(value))


def run_cell(n, namespace):
    path = os.path.join(RUN, n)
    out = os.open(path + ".out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    saved = os.dup(1), os.dup(2)
    os.dup2(out, 1)
    os.dup2(out, 2)
    t0, c0 = time.monotonic(), time.process_time()
    status = "ok"
    try:
        with open(path + ".py", encoding="utf-8") as stream:
            source = stream.read()
        signal.setitimer(signal.ITIMER_REAL, CELL_TIMEOUT)
        try:
            execute(source, namespace)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except BaseException as error:  # SystemExit from a cell too
        status = "timeout" if isinstance(error, CellTimeout) else "error"
        try:
            traceback.print_exc()
        except OSError:
            pass
    wall, cpu = time.monotonic() - t0, time.process_time() - c0
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os.dup2(saved[0], 1)
    os.dup2(saved[1], 2)
    for fd in (out, *saved):
        os.close(fd)
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with open(path + ".done.tmp", "w") as stream:
        stream.write(f"{status} {wall:.6f} {cpu:.6f} {maxrss}\n")
    os.rename(path + ".done.tmp", path + ".done")
    return status


def main():
    os.makedirs(RUN, exist_ok=True)
    fds = {}
    for name in ("req", "resp"):
        path = os.path.join(RUN, name)
        if not os.path.exists(path):
            os.mkfifo(path)
        fds[name] = os.open(path, os.O_RDWR)
    signal.signal(signal.SIGALRM, on_alarm)
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # writes past MAX_FILE fail with EFBIG instead of killing us
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))
    namespace = {"__name__": "__main__"}
    with open(os.path.join(RUN, "kernel.pid"), "w") as stream:
        stream.write(f"{os.getpid()}\n")
    open(os.path.join(RUN, "ready"), "w").close()
    pending = b""
    while True:
        pending += os.read(fds["req"], 4096)
        *lines, pending = pending.split(b"\n")
        for line in lines:
            n = line.decode(errors="replace").strip()
            if not n.isdigit() or os.path.exists(os.path.join(RUN, n + ".done")):
                continue
            status = run_cell(n, namespace)
            os.write(fds["resp"], f"{n} {status}\n".encode())


if __name__ == "__main__":
    main()
