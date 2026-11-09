"""Minimal multi-step decode engine for Qwen2.5-1.5B: K decode steps (forward + sampling + token feedback) are
captured in one CUDA graph, so the host touches the GPU once per K tokens instead of once per token.
usage:
  msdecode.py check                       greedy tokens vs HF transformers for 2 prompts
  msdecode.py bench BATCH K [SECONDS]     wall and host-thread CPU per decode step
Fixed batch, all prompts truncated to the same length, temperature 1 (Gumbel-max), no EOS: a decode-only microbenchmark.
"""
import json, os, sys, time
HARVEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # experiments/gpu-harvest
import torch
import torch.nn.functional as F
from safetensors.torch import load_file
from vllm import _custom_ops as ops
from vllm.vllm_flash_attn import flash_attn_with_kvcache

MODEL = "/mnt/ssd1/Qwen/Qwen2.5-1.5B-Instruct"
cfg = json.load(open(f"{MODEL}/config.json"))
H, NH, NKV, L = cfg["hidden_size"], cfg["num_attention_heads"], cfg["num_key_value_heads"], cfg["num_hidden_layers"]
D, EPS, I = H // NH, cfg["rms_norm_eps"], cfg["intermediate_size"]
dev = torch.device("cuda")


class Engine:
    def __init__(self, batch, max_len):
        w = {k: v.to(dev) for k, v in load_file(f"{MODEL}/model.safetensors").items()}
        p = "model.layers.{}."
        self.layers = []
        for i in range(L):
            g = lambda n: w[p.format(i) + n]
            self.layers.append(dict(
                ln1=g("input_layernorm.weight"), ln2=g("post_attention_layernorm.weight"),
                qkv_w=torch.cat([g(f"self_attn.{x}_proj.weight") for x in "qkv"]),
                qkv_b=torch.cat([g(f"self_attn.{x}_proj.bias") for x in "qkv"]),
                o=g("self_attn.o_proj.weight"),
                gate_up=torch.cat([g("mlp.gate_proj.weight"), g("mlp.up_proj.weight")]),
                down=g("mlp.down_proj.weight")))
        self.embed, self.norm = w["model.embed_tokens.weight"], w["model.norm.weight"]
        self.B, self.max_len = batch, max_len
        self.k = [torch.zeros(batch, max_len, NKV, D, dtype=torch.bfloat16, device=dev) for _ in range(L)]
        self.v = [torch.zeros_like(c) for c in self.k]
        inv = 1.0 / (cfg["rope_theta"] ** (torch.arange(0, D, 2, device=dev, dtype=torch.float32) / D))
        f = torch.outer(torch.arange(max_len, device=dev, dtype=torch.float32), inv)
        self.cos, self.sin = f.cos().to(torch.bfloat16), f.sin().to(torch.bfloat16)
        self.seqlens = torch.zeros(batch, dtype=torch.int32, device=dev)

    def forward(self, ids, slots=None):
        """ids [B, T] -> logits of the last position [B, V]; appends T positions to the KV cache.
        With slots (int32 [n]), ids [n, T] is a prefill that overwrites those KV-cache slots from position 0."""
        B, T = ids.shape
        seqlens = self.seqlens if slots is None else torch.zeros(B, dtype=torch.int32, device=dev)
        x = self.embed[ids].view(B * T, H)
        res = None
        for i, ly in enumerate(self.layers):
            if res is None:
                res = x; h = torch.empty_like(x); ops.rms_norm(h, x, ly["ln1"], EPS)
            else:
                h = x; ops.fused_add_rms_norm(h, res, ly["ln1"], EPS)
            qkv = F.linear(h, ly["qkv_w"]).add_(ly["qkv_b"])  # bias in the GEMM picks a slow cuBLAS kernel
            q, k, v = qkv.split([NH * D, NKV * D, NKV * D], dim=-1)
            o = flash_attn_with_kvcache(q.reshape(B, T, NH, D), self.k[i], self.v[i],
                                        k=k.reshape(B, T, NKV, D), v=v.reshape(B, T, NKV, D),
                                        rotary_cos=self.cos, rotary_sin=self.sin, rotary_interleaved=False,
                                        cache_seqlens=seqlens, cache_batch_idx=slots, causal=True)
            x = F.linear(o.reshape(B * T, NH * D), ly["o"])
            ops.fused_add_rms_norm(x, res, ly["ln2"], EPS)
            gu = F.linear(x, ly["gate_up"])
            act = torch.empty(B * T, I, dtype=x.dtype, device=dev)
            torch.ops._C.silu_and_mul(act, gu)
            x = F.linear(act, ly["down"])
        ops.fused_add_rms_norm(x, res, self.norm, EPS)
        if slots is None:
            self.seqlens += T
        else:
            self.seqlens.index_fill_(0, slots.long(), T)  # graph-capturable
        return F.linear(x.view(B, T, H)[:, -1], self.embed)


def sample(logits, greedy):
    if greedy:
        return logits.argmax(-1)
    # Gumbel-max: argmax(logits - log E), E ~ Exp(1)
    e = torch.empty(logits.shape, dtype=torch.float32, device=logits.device).exponential_().log_()
    return torch.sub(logits, e).argmax(-1)


def prompts(batch, length):
    import pandas as pd
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODEL)
    rows = pd.read_parquet(HARVEST + "/data/gsm8k/train.parquet")
    ids = [tok.apply_chat_template(r, add_generation_prompt=True) for r in rows["prompt"]]
    ids = [x[:length] for x in ids if len(x) >= length][:batch]
    return torch.tensor(ids, device=dev)


def check():
    from transformers import AutoModelForCausalLM
    ids = prompts(2, 96)
    e = Engine(2, 160)
    tok = sample(e.forward(ids), True)
    mine = [tok]
    for _ in range(31):
        tok = sample(e.forward(tok[:, None]), True); mine.append(tok)
    mine = torch.stack(mine, 1).tolist()
    hf = AutoModelForCausalLM.from_pretrained(MODEL, torch_dtype=torch.bfloat16).to(dev)
    ref = hf.generate(ids, max_new_tokens=32, do_sample=False)[:, 96:].tolist()
    for a, b in zip(mine, ref):
        same = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), len(a))
        print(f"first {same}/32 greedy tokens match HF")


def bench(batch, K, seconds, plen=96, steps_total=512):
    e = Engine(batch, plen + steps_total + K)
    ids = prompts(batch, plen)
    tok = torch.zeros(batch, dtype=torch.long, device=dev)
    out = torch.zeros(batch, K, dtype=torch.long, device=dev)
    host = torch.zeros(batch, K, dtype=torch.long, pin_memory=True)
    done = torch.cuda.Event(blocking=True)

    def k_steps():
        for j in range(K):
            nxt = sample(e.forward(tok[:, None]), False)
            out[:, j] = nxt; tok.copy_(nxt)

    def reset():
        e.seqlens.zero_(); tok.copy_(sample(e.forward(ids), False))

    s = torch.cuda.Stream()
    s.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(s):
        reset(); k_steps()  # warm-up outside the graph
    torch.cuda.current_stream().wait_stream(s)
    reset()
    g = torch.cuda.CUDAGraph()
    with torch.cuda.graph(g):
        k_steps()
    tokens = []
    split = [0.0] * 4  # replay wall, replay cpu, wait wall, wait cpu
    n_steps, c0, t0 = 0, time.thread_time(), time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        reset()
        for _ in range(steps_total // K):
            a, ac = time.perf_counter(), time.thread_time()
            g.replay()
            b, bc = time.perf_counter(), time.thread_time()
            host.copy_(out, non_blocking=True); done.record(); done.synchronize()
            split[0] += b - a; split[1] += bc - ac; split[2] += time.perf_counter() - b; split[3] += time.thread_time() - bc
            tokens.append(host.tolist()); n_steps += K
    torch.cuda.synchronize()
    wall, cpu = time.perf_counter() - t0, time.thread_time() - c0
    r = {"batch": batch, "K": K, "steps": n_steps, "wall_ms_per_step": wall / n_steps * 1e3,
         "host_cpu_ms_per_step": cpu / n_steps * 1e3, "host_cpu_share": cpu / wall,
         "tok_per_s": batch * n_steps / wall,
         "replay_ms_per_step": [split[0] / n_steps * 1e3, split[1] / n_steps * 1e3],
         "wait_ms_per_step": [split[2] / n_steps * 1e3, split[3] / n_steps * 1e3]}
    print("RESULT " + json.dumps(r), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "check":
        check()
    else:
        bench(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]) if len(sys.argv) > 4 else 20)
