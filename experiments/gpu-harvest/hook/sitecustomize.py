"""Phase recorder loaded by every Python process when this directory is on PYTHONPATH.

When HARVEST_PHASE_LOG is set, the verl trainer's marked_timer also appends
{"name", "start_ns", "end_ns", "pid"} (CLOCK_MONOTONIC) to that jsonl file.
The verl trainer loop runs inside a Ray actor, so the patch is applied on import there.
With HARVEST_CUDA_BLOCKING=1 it also switches CUDA waits from spinning to blocking sync when torch is imported.
"""
import os

PATH = os.environ.get("HARVEST_PHASE_LOG")
TARGETS = ("verl.trainer.ppo.ray_trainer", "recipe.one_step_off_policy.ray_trainer",
           "rllm.trainer.verl.agent_ppo_trainer")

if PATH:
    import contextlib
    import importlib.abc
    import importlib.machinery
    import json
    import sys
    import time

    def record(name, start, end):
        line = json.dumps({"name": name, "start_ns": start, "end_ns": end, "pid": os.getpid()}) + "\n"
        with open(PATH, "a") as stream:
            stream.write(line)

    def wrap(original):
        @contextlib.contextmanager
        def marked_timer(name, timing_raw, *args, **kwargs):
            start = time.monotonic_ns()
            record(name, start, None)
            try:
                with original(name, timing_raw, *args, **kwargs):
                    yield
            finally:
                record(name, start, time.monotonic_ns())
        return marked_timer

    class Loader(importlib.abc.Loader):
        def __init__(self, inner):
            self.inner = inner

        def create_module(self, spec):
            return self.inner.create_module(spec)

        def exec_module(self, module):
            self.inner.exec_module(module)
            if hasattr(module, "marked_timer"):
                module.marked_timer = wrap(module.marked_timer)

    def blocking_sync():
        """Sets CU_CTX_SCHED_BLOCKING_SYNC on every visible GPU's primary context before torch creates it.
        The CUDA default (auto) spins the waiting host thread when there are fewer contexts than CPUs."""
        import ctypes
        cu = ctypes.CDLL("libcuda.so.1")
        count, dev = ctypes.c_int(), ctypes.c_int()
        if cu.cuInit(0) != 0 or cu.cuDeviceGetCount(ctypes.byref(count)) != 0:
            return
        for ordinal in range(count.value):
            if cu.cuDeviceGet(ctypes.byref(dev), ordinal) == 0:
                cu.cuDevicePrimaryCtxSetFlags(dev, 0x04)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "torch" and os.environ.get("HARVEST_CUDA_BLOCKING") == "1":
                blocking_sync()
                return None
            if fullname not in TARGETS:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None:
                spec.loader = Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, Finder())
