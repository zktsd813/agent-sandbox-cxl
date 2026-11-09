"""Host-CPU breakdown of one vLLM V1 decode step (in-process engine, fixed batch, ignore_eos).
usage: prof_vllm.py LABEL BATCH SECONDS [MAX_TOKENS]
Wraps the engine-step functions and records wall and thread CPU time (time.thread_time) per call.
With blocking CUDA sync (HARVEST_CUDA_BLOCKING=1), thread CPU is real host work; GPU waits show as wall only.
"forward" is execute_model minus its wrapped parts: graph dispatch/replay, compute_logits launch, forward context.
"""
import json, os, sys, time
HARVEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # experiments/gpu-harvest
from collections import defaultdict
os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")
import pandas as pd
from vllm import LLM, SamplingParams
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.engine.core import EngineCore
from vllm.v1.engine.output_processor import OutputProcessor
from vllm.v1.worker.gpu_model_runner import GPUModelRunner

label, batch, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
tokens = int(sys.argv[4]) if len(sys.argv) > 4 else 512
acc = defaultdict(lambda: [0, 0.0, 0.0])  # name -> calls, wall_s, cpu_s
on = False

def wrap(cls, attr, name):
    inner = getattr(cls, attr)
    def timed(*a, **k):
        if not on:
            return inner(*a, **k)
        w, c = time.perf_counter(), time.thread_time()
        try:
            return inner(*a, **k)
        finally:
            r = acc[name]; r[0] += 1; r[1] += time.perf_counter() - w; r[2] += time.thread_time() - c
    setattr(cls, attr, timed)

wrap(EngineCore, "step", "engine_step")
wrap(EngineCore, "step_with_batch_queue", "engine_step")
wrap(Scheduler, "schedule", "schedule")
wrap(Scheduler, "update_from_output", "update_from_output")
wrap(GPUModelRunner, "execute_model", "execute_model")
wrap(GPUModelRunner, "_update_states", "update_states")
wrap(GPUModelRunner, "_prepare_inputs", "prepare_inputs")
wrap(GPUModelRunner, "_preprocess", "preprocess")
wrap(GPUModelRunner, "_sample", "sample")
wrap(GPUModelRunner, "_bookkeeping_sync", "bookkeeping_sync")
wrap(GPUModelRunner, "_to_list", "to_list (D2H sync)")
wrap(OutputProcessor, "process_outputs", "process_outputs (detok)")

rows = pd.read_parquet(HARVEST + "/data/gsm8k/train.parquet")
prompts = [r[0]["content"] for r in rows["prompt"][:batch]]
llm = LLM("/mnt/ssd1/Qwen/Qwen2.5-1.5B-Instruct", gpu_memory_utilization=0.4, max_model_len=1536, seed=1,
          async_scheduling=os.environ.get("ASYNC") == "1")
sp = SamplingParams(temperature=1.0, max_tokens=tokens, ignore_eos=True, seed=1)
llm.generate(prompts, sp, use_tqdm=False)
on = True
c0, t0, n = os.times(), time.perf_counter(), 0
while time.perf_counter() - t0 < seconds:
    llm.generate(prompts, sp, use_tqdm=False); n += 1
on = False
wall, c1 = time.perf_counter() - t0, os.times()
cpu = (c1.user - c0.user) + (c1.system - c0.system)
steps = acc["engine_step"][0]
em = acc["execute_model"]
parts = ("update_states", "prepare_inputs", "preprocess", "sample", "bookkeeping_sync")
fwd = [em[0], em[1] - sum(acc[p][1] for p in parts), em[2] - sum(acc[p][2] for p in parts)]
acc["forward (dispatch+replay+logits)"] = fwd
out = {"label": label, "batch": batch, "calls": n, "engine_steps": steps, "wall_ms_per_step": wall / steps * 1e3,
       "process_cpu_ms_per_step": cpu / steps * 1e3, "process_cpu_share": cpu / wall,
       "per_step_ms": {k: {"wall": v[1] / steps * 1e3, "cpu": v[2] / steps * 1e3} for k, v in acc.items()}}
print("RESULT " + json.dumps(out), flush=True)
print(f"{label} batch={batch} steps={steps} wall/step={out['wall_ms_per_step']:.2f}ms "
      f"process cpu/step={out['process_cpu_ms_per_step']:.2f}ms share={cpu / wall:.2f}")
for k, v in sorted(out["per_step_ms"].items(), key=lambda x: -x[1]["cpu"]):
    print(f"  {k:34s} wall {v['wall']:7.3f} ms  cpu {v['cpu']:7.3f} ms")
