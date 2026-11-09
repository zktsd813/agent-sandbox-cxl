"""Decode wall per step with vLLM async scheduling (mp executor) vs sync, and CPU of the whole process tree.
usage: async_bench.py BATCH ASYNC(0|1) [SECONDS]"""
import os, sys, time
HARVEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # experiments/gpu-harvest

def tree_cpu(pid):
    """utime+stime (s) of pid and its descendants from /proc."""
    kids = {}
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
                kids.setdefault(int(f[1]), []).append((int(p), (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")))
            except OSError:
                pass
    total, todo = 0.0, [pid]
    own = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    total += (int(own[11]) + int(own[12])) / os.sysconf("SC_CLK_TCK")
    while todo:
        for c, t in kids.get(todo.pop(), []):
            total += t; todo.append(c)
    return total

if __name__ == "__main__":
    import pandas as pd
    from vllm import LLM, SamplingParams
    batch, asy = int(sys.argv[1]), sys.argv[2] == "1"
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 30
    rows = pd.read_parquet(HARVEST + "/data/gsm8k/train.parquet")
    prompts = [r[0]["content"] for r in rows["prompt"][:batch]]
    llm = LLM("/mnt/ssd1/Qwen/Qwen2.5-1.5B-Instruct", gpu_memory_utilization=0.4, max_model_len=1536, seed=1,
              async_scheduling=asy)
    sp = SamplingParams(temperature=1.0, max_tokens=512, ignore_eos=True, seed=1)
    llm.generate(prompts, sp, use_tqdm=False)
    c0, t0, n = tree_cpu(os.getpid()), time.perf_counter(), 0
    while time.perf_counter() - t0 < secs:
        llm.generate(prompts, sp, use_tqdm=False); n += 1
    wall, cpu = time.perf_counter() - t0, tree_cpu(os.getpid()) - c0
    steps = n * 512
    print(f"RESULT batch={batch} async={int(asy)} wall_ms_per_token_step={wall / steps * 1e3:.2f} "
          f"tree_cpu_ms_per_step={cpu / steps * 1e3:.2f} tree_cpu_cores={cpu / wall:.2f}", flush=True)
