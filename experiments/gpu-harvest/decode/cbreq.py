"""Shared request stream for the continuous-batching comparison: request i = (96-token gsm8k prompt, output length).
Output lengths are uniform in [64, 512] (seed 0) and generation ignores EOS, so both engines produce the same tokens."""
import os
HARVEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # experiments/gpu-harvest
import numpy as np

MODEL = "/mnt/ssd1/Qwen/Qwen2.5-1.5B-Instruct"
PLEN, NREQ = 96, 100000


def requests():
    import pandas as pd
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODEL)
    rows = pd.read_parquet(HARVEST + "/data/gsm8k/train.parquet")
    ids = [tok.apply_chat_template(r, add_generation_prompt=True) for r in rows["prompt"][:4000]]
    prompts = [x[:PLEN] for x in ids if len(x) >= PLEN]
    lens = np.random.default_rng(0).integers(64, 513, size=NREQ)
    return tok, prompts, lens
