#!/usr/bin/env python3
"""Rough wall-clock estimate for collecting the rollout traces (docs/trace-collection-plan.md).

Model (stated so it can be checked):
- A rollout alternates `turns` LLM turns and tool calls. Per turn: prefill of the new input tokens, decode of the output
  tokens, then one tool call of `tool_s` seconds on the sandbox host.
- `slots` rollouts run at once (limited by sandbox CPU: about one core per sandbox, no contention). The fraction of them
  waiting on the LLM sets the decode batch b; b is also capped by the KV-cache capacity at the mean context length.
- Decode step time = (weight bytes per GPU + b * context * KV bytes per token / TP) / (HBM bandwidth * efficiency)
  + a fixed per-step overhead. Prefill time = FLOPs / (peak * MFU), with FLOPs = 2 * active params * tokens
  + 4 * tokens * context * attention width * layers.
- With R replicas, rollouts are spread evenly. Wall time = max(latency bound, GPU-throughput bound).
All inputs are guesses or measured from the 264 SWE traces (turns 51.5, context mean 30.6K, new input 862/turn,
tool 1.35 s/command); output tokens per turn are swept because the reasoning length of the target models is unknown.
"""

import itertools
import math

GB = 1e9

HARDWARE = {  # per GPU: memory GB, HBM GB/s, dense BF16 TFLOPS, FP8 compute (False: weight-only on Ampere)
    "A6000": dict(mem=48, bw=768, bf16=155, fp8_compute=False, reserve=3.0, overhead_ms=4.0),
    "H100-SXM": dict(mem=80, bw=3350, bf16=989, fp8_compute=True, reserve=5.0, overhead_ms=2.5),
}
MODELS = {  # params (B), active params (B), layers, KV heads, head dim, attention heads
    "Qwen3-32B": dict(params=32.8, active=32.8, layers=64, kv_heads=8, head_dim=128, heads=64),
    "Qwen3-Coder-30B-A3B": dict(params=30.5, active=3.3, layers=48, kv_heads=4, head_dim=128, heads=32),
    "DeepCoder-14B": dict(params=14.8, active=14.8, layers=48, kv_heads=8, head_dim=128, heads=40),
}
BW_EFF, MFU = 0.65, 0.40  # achieved fraction of HBM bandwidth in decode, of peak FLOPs in prefill

# workload: model, rollouts, turns, new input tokens per turn, mean context, tool seconds per turn (plan section 6.2)
# DeepSWE-Preview is a Qwen3-32B fine-tune, so it uses the Qwen3-32B shape.
WORKLOADS = {
    "W1-a DeepSWE (512x8)": dict(model="Qwen3-32B", rollouts=4096, turns=51.5, new_in=862, ctx=28000, tool_s=1.35),
    "W1-b Qwen3-32B (128x8)": dict(model="Qwen3-32B", rollouts=1024, turns=51.5, new_in=862, ctx=28000, tool_s=1.35),
    "W2 mini-swe 30B-A3B (128x8)": dict(model="Qwen3-Coder-30B-A3B", rollouts=1024, turns=51.5, new_in=862, ctx=28000,
                                        tool_s=1.35),
    "W3 terminal 30B-A3B (89x8)": dict(model="Qwen3-Coder-30B-A3B", rollouts=712, turns=40, new_in=1000, ctx=24000,
                                       tool_s=4.0),
    "W4 DeepCoder (1024x8)": dict(model="DeepCoder-14B", rollouts=8192, turns=1, new_in=1000, ctx=6000, tool_s=10.0),
    "W5 SWE-rebench DeepSWE (512x8)": dict(model="Qwen3-32B", rollouts=4096, turns=51.5, new_in=862, ctx=28000,
                                           tool_s=1.35),
}


def kv_bytes(m, dtype_bytes):
    return 2 * m["layers"] * m["kv_heads"] * m["head_dim"] * dtype_bytes


def best_layout(hw, gpus, m, wbytes):
    """Smallest TP that fits the weights with room for KV; returns (tp, replicas, kv GB per replica)."""
    for tp in (1, 2, 4, 8):
        if tp > gpus:
            break
        free = tp * (hw["mem"] * 0.92 - hw["reserve"]) - m["params"] * wbytes
        if free >= 0.25 * tp * hw["mem"]:
            return tp, gpus // tp, free
    tp = gpus
    return tp, 1, tp * (hw["mem"] * 0.92 - hw["reserve"]) - m["params"] * wbytes


def estimate(hw_name, gpus, wl, out_tok, slots, wbytes, kvb):
    hw, m = HARDWARE[hw_name], MODELS[wl["model"]]
    tp, replicas, kv_free = best_layout(hw, gpus, m, wbytes)
    if kv_free <= 0:
        return None
    kv_tok = kv_free * GB / kv_bytes(m, kvb)
    b_cap = max(1.0, 0.85 * kv_tok / wl["ctx"])
    flops_peak = hw["bf16"] * (2 if (hw["fp8_compute"] and wbytes == 1) else 1) * 1e12 * tp
    width = m["heads"] * m["head_dim"]

    def prefill_s(tokens, ctx):
        return (2 * m["active"] * 1e9 * tokens + 4 * tokens * ctx * width * m["layers"]) / (flops_peak * MFU)

    def step_s(b):
        bytes_per_gpu = m["active"] * 1e9 * wbytes / tp if m["active"] < m["params"] else m["params"] * 1e9 * wbytes / tp
        if m["active"] < m["params"]:  # MoE: experts touched per step grow with batch, up to all weights
            bytes_per_gpu = min(m["params"], m["active"] * (1 + 0.6 * b)) * 1e9 * wbytes / tp
        bytes_per_gpu += b * wl["ctx"] * kv_bytes(m, kvb) / tp
        return bytes_per_gpu / (hw["bw"] * GB * BW_EFF) + hw["overhead_ms"] / 1000

    per_replica = slots / replicas
    b = min(per_replica, b_cap)
    for _ in range(50):  # fixed point: share of rollouts in the LLM phase
        llm_s = prefill_s(wl["new_in"], wl["ctx"]) + out_tok * step_s(b)
        frac = llm_s / (llm_s + wl["tool_s"])
        b = max(1.0, min(per_replica * frac, b_cap))
    llm_s = prefill_s(wl["new_in"], wl["ctx"]) + out_tok * step_s(b)
    rollout_s = wl["turns"] * (llm_s + wl["tool_s"])
    latency_bound = wl["rollouts"] * rollout_s / min(slots, b_cap * replicas / max(frac, 1e-3))
    gpu_per_rollout = wl["turns"] * (prefill_s(wl["new_in"], wl["ctx"]) + out_tok * step_s(b_cap) / b_cap)
    throughput_bound = wl["rollouts"] * gpu_per_rollout / replicas
    return dict(tp=tp, replicas=replicas, b_cap=b_cap, batch=b, turn_s=llm_s, rollout_min=rollout_s / 60,
                hours=max(latency_bound, throughput_bound) / 3600)


PRECISION = {  # (label, weight bytes, KV bytes) per hardware and model family
    "A6000": {"dense": [("BF16", 2, 2), ("FP8w", 1, 2), ("FP8w+KV8", 1, 1)], "moe": [("BF16", 2, 2), ("FP8w+KV8", 1, 1)],
              "small": [("BF16", 2, 2)]},
    "H100-SXM": {"dense": [("BF16", 2, 2), ("FP8", 1, 1)], "moe": [("BF16", 2, 2), ("FP8", 1, 1)],
                 "small": [("BF16", 2, 2)]},
}


def family(model):
    return {"Qwen3-32B": "dense", "Qwen3-Coder-30B-A3B": "moe"}.get(model, "small")


def main():
    configs = [("A6000", 2), ("H100-SXM", 2), ("H100-SXM", 4), ("H100-SXM", 8)]
    for slots in (32, 64, 128):
        for out_tok in (300, 500, 900):
            print(f"\n## sandbox slots {slots}, output tokens per turn {out_tok}")
            for hw, g in configs:
                cells, best_total = [], 0.0
                for name, wl in WORKLOADS.items():
                    opts = []
                    for label, wb, kb in PRECISION[hw][family(wl["model"])]:
                        r = estimate(hw, g, wl, out_tok, slots, wb, kb)
                        if r:
                            opts.append((r["hours"], label, r))
                    h, label, r = min(opts)
                    bf = [o for o in opts if o[1] == "BF16"]
                    best_total += h
                    gpu_min = h * 60 * g / wl["rollouts"]
                    cells.append(f"{name.split()[0]} {h:.0f}h[{label}] (BF16 {bf[0][0]:.0f}h)" if bf and bf[0][1] != label
                                 else f"{name.split()[0]} {h:.0f}h[{label}]")
                    if name.startswith("W1-a"):
                        detail = (f"W1-a: TP{r['tp']}x{r['replicas']}, KV-limited batch {r['b_cap']:.1f}/replica, "
                                  f"turn {r['turn_s']:.1f}s, rollout {r['rollout_min']:.0f} min, {gpu_min:.2f} GPU-min/rollout")
                print(f"{hw} x{g}: total {best_total/24:.1f} d | " + ", ".join(cells))
                print(f"    {detail}")


if __name__ == "__main__":
    main()
