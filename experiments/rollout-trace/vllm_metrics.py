"""Poll vLLM /metrics once a second (M2 server load).

Writes running/waiting request counts, KV cache usage and prefix cache
counters to <root>/_global/vllm_metrics.csv. Metric names changed across
vLLM versions, so each column takes the first name that exists.

usage: vllm_metrics.py --root traces --url http://127.0.0.1:18000/metrics
"""

import argparse
import time
import urllib.request
from pathlib import Path

COLUMNS = {
    "running": ["vllm:num_requests_running"],
    "waiting": ["vllm:num_requests_waiting"],
    "kv_usage": ["vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"],
    "prefix_queries": ["vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total"],
    "prefix_hits": ["vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total"],
    "preemptions": ["vllm:num_preemptions_total"],
    "prompt_tokens": ["vllm:prompt_tokens_total"],
    "generation_tokens": ["vllm:generation_tokens_total"],
}


def parse(text):
    """Sum samples per metric name over all label sets."""
    out = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        name_labels, _, value = line.rpartition(" ")
        name = name_labels.split("{", 1)[0]
        try:
            out[name] = out.get(name, 0.0) + float(value)
        except ValueError:
            pass
    return out


def pick(m, names):
    for n in names:
        if n in m:
            return m[n]
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:18000/metrics")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    out = Path(a.root) / "_global"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "vllm_metrics.csv"
    new = not path.exists()
    with path.open("a") as f:
        if new:
            f.write("t," + ",".join(COLUMNS) + "\n")
        nxt = time.time()
        while True:
            t = time.time()
            try:
                m = parse(urllib.request.urlopen(a.url, timeout=2).read().decode())
                f.write(f"{t:.3f}," + ",".join(str(pick(m, n)) for n in COLUMNS.values()) + "\n")
            except OSError:
                f.write(f"{t:.3f}" + "," * len(COLUMNS) + "\n")
            f.flush()
            nxt += a.interval
            time.sleep(max(0.0, nxt - time.time()))


if __name__ == "__main__":
    main()
