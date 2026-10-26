"""Convert an FP32 safetensors checkpoint to BF16 in place, shard by shard.

DeepSWE-Preview is published in FP32 (131 GB). vLLM casts to BF16 (or FP8)
when loading, so storing BF16 gives the same served model at half the disk.
Each shard is written to <name>.bf16.tmp, read back and checked (same
tensor names and shapes, values equal to the FP32 tensor rounded to BF16),
then renamed over the original, so extra disk is one shard at a time and a
crash leaves every shard either FP32 or BF16. Re-running skips converted
shards. Non-float tensors are kept as they are. Updates the index's
total_size and config.json's torch_dtype at the end.

usage (inside an image with torch + safetensors): to_bf16.py <model dir>
"""

import json
import os
import sys
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import load_file, save_file


def convert_shard(path):
    with safe_open(path, "pt") as f:
        meta = f.metadata()
        names = list(f.keys())
        if all(f.get_slice(n).get_dtype() != "F32" for n in names):
            return False
        out = {}
        for n in names:
            t = f.get_tensor(n)
            out[n] = t.to(torch.bfloat16) if t.dtype == torch.float32 else t
    tmp = path.with_name(path.name + ".bf16.tmp")
    save_file(out, tmp, metadata=meta or {"format": "pt"})
    back = load_file(tmp)
    assert back.keys() == out.keys(), path
    for n, t in out.items():
        assert back[n].shape == t.shape and back[n].dtype == t.dtype and torch.equal(back[n], t), (path, n)
    os.replace(tmp, path)
    return True


def main():
    d = Path(sys.argv[1])
    torch.set_num_threads(4)
    shards = sorted(d.glob("*.safetensors"))
    for i, p in enumerate(shards):
        done = convert_shard(p)
        print(f"[{i + 1}/{len(shards)}] {p.name} {'converted' if done else 'already bf16'}", flush=True)
    idx = d / "model.safetensors.index.json"
    if idx.exists():
        j = json.loads(idx.read_text())
        size = {"F32": 4, "BF16": 2, "F16": 2, "I64": 8, "I32": 4, "U8": 1, "BOOL": 1}
        total = 0
        for p in shards:
            with safe_open(p, "pt") as f:
                for n in f.keys():
                    s = f.get_slice(n)
                    total += size[s.get_dtype()] * int(torch.tensor(s.get_shape()).prod())
        j["metadata"]["total_size"] = total
        idx.write_text(json.dumps(j, indent=2))
    cfg = d / "config.json"
    c = json.loads(cfg.read_text())
    c["torch_dtype"] = "bfloat16"
    cfg.write_text(json.dumps(c, indent=2))
    (d / "CONVERTED").write_text("fp32 -> bf16 by experiments/rollout-trace/to_bf16.py; original revision in REVISION\n")
    print("done", flush=True)


if __name__ == "__main__":
    main()
