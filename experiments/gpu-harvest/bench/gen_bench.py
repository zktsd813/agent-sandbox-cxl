"""Standalone vLLM decode timing for the C1s model, to reproduce the gen slowdown outside verl.
usage: gen_bench.py LABEL. Run with experiments/gpu-harvest/hook on PYTHONPATH; HARVEST_CUDA_BLOCKING=1 selects blocking sync.
Batches of 320 (one GPU's share of a C1s step) and 16 (the long-tail end of a step), 256 tokens each with ignore_eos.
"""
import os, sys, time
HARVEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # experiments/gpu-harvest
os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")
import pandas as pd
from vllm import LLM, SamplingParams
label = sys.argv[1]
rows = pd.read_parquet(HARVEST + "/data/gsm8k/train.parquet")
prompts = [r[0]["content"] for r in rows["prompt"][:320]]
llm = LLM("/mnt/ssd1/Qwen/Qwen2.5-1.5B-Instruct", gpu_memory_utilization=0.4, max_model_len=1536, seed=1)
sp = SamplingParams(temperature=1.0, max_tokens=256, ignore_eos=True, seed=1)
llm.generate(prompts[:32], sp, use_tqdm=False)
for n in (320, 16):
    times = []
    for _ in range(3):
        t0 = time.perf_counter(); llm.generate(prompts[:n], sp, use_tqdm=False); times.append(time.perf_counter() - t0)
    best = min(times)
    print(f"RESULT {label} batch={n} wall_s={best:.3f} ms_per_decode_step={best / 256 * 1e3:.2f} all={[round(t, 3) for t in times]}", flush=True)
cpu = os.times()
print(f"RESULT {label} process_cpu_s user={cpu.user:.1f} sys={cpu.system:.1f}", flush=True)
