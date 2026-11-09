"""Continuous-batching K-step decode engine on top of msdecode.Engine.
usage: msengine.py BATCH K MODE(sync|overlap) SECONDS
Each chunk is one CUDA graph of K decode steps. At every chunk boundary the host copies the tokens, detokenizes them
incrementally (tokenizers DecodeStream, as vLLM does), checks stop conditions, and replaces finished requests with new
ones. Replacement is a CUDA-graph prefill into their KV slots (one graph per power-of-two row count, n admissions are
split by binary decomposition), fed from pinned staging buffers so the host never blocks on the copy.
  sync:    replay chunk n -> wait -> boundary work -> replay chunk n+1 (GPU idles during boundary work)
  overlap: replay chunk n+1 is enqueued before the boundary work of chunk n; a finished slot then decodes one wasted
           chunk before its replacement prefill runs (stream order).
All waits are blocking-sync events (the host thread sleeps). Reports useful tokens/s, process CPU, boundary CPU per
chunk, GPU idle share (CUDA event gaps) and wasted slot-tokens.
"""
import json, os, sys, time
import numpy as np
import torch
from tokenizers.decoders import DecodeStream
from msdecode import Engine, sample, dev
from cbreq import requests, PLEN

B, K, MODE, SECONDS = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
SIZES, MAXADM = (32, 16, 8, 4, 2, 1), 256  # prefill graph row counts; max admissions per boundary
tok, prompts, lens = requests()
fast = tok._tokenizer
STOP_IDS = {tok.eos_token_id, tok.convert_tokens_to_ids("<|im_end|>")}
e = Engine(B, PLEN + 512 + 3 * K + 8)
cur = torch.zeros(B, dtype=torch.long, device=dev)
out = torch.zeros(B, K, dtype=torch.long, device=dev)
host = [torch.zeros(B, K, dtype=torch.long, pin_memory=True) for _ in range(2)]
done = [torch.cuda.Event(blocking=True) for _ in range(2)]
pf_ids = {n: torch.zeros(n, PLEN, dtype=torch.long, device=dev) for n in SIZES}
pf_slots = {n: torch.zeros(n, dtype=torch.int32, device=dev) for n in SIZES}
stage_ids = [torch.zeros(MAXADM, PLEN, dtype=torch.long, pin_memory=True) for _ in range(2)]
stage_slots = [torch.zeros(MAXADM, dtype=torch.int32, pin_memory=True) for _ in range(2)]


def k_steps():
    for j in range(K):
        nxt = sample(e.forward(cur[:, None]), False)
        out[:, j] = nxt; cur.copy_(nxt)


def prefill(n):
    cur[pf_slots[n].long()] = sample(e.forward(pf_ids[n], pf_slots[n]), False)


next_req = 0
admits = 0
req = [None] * B      # per slot: [target_len, generated, DecodeStream, text_len, stopped]
start = [0] * B       # first chunk index whose output belongs to the slot's current request
launched = 0          # chunks enqueued so far
stats = dict(useful=0, wasted=0, finished=0, loop_cpu=0.0, admit_cpu=0.0)
pevs = []             # (start, end) events around each boundary's prefill replays


def admit(slots, pg):
    """Assign new requests to slots and enqueue their prefill graph replays."""
    global next_req, admits
    buf = admits % 2; admits += 1
    ids, sl = stage_ids[buf], stage_slots[buf]
    for j, s in enumerate(slots):
        i = next_req; next_req += 1
        ids[j] = torch.from_numpy(prompts_np[i % len(prompts_np)])
        sl[j] = s
        req[s] = [int(lens[i]), 1, DecodeStream(skip_special_tokens=True), 0, False]  # prefill samples token 1
        start[s] = launched
    a = torch.cuda.Event(enable_timing=True); b = torch.cuda.Event(enable_timing=True)
    a.record()
    off, left = 0, len(slots)
    for n in SIZES:
        while left >= n:
            pf_ids[n].copy_(ids[off:off + n], non_blocking=True); pf_slots[n].copy_(sl[off:off + n], non_blocking=True)
            pg[n].replay(); off += n; left -= n
    b.record(); pevs.append((a, b))


def boundary(c, toks, pg):
    """Host work for chunk c: detokenize, stop checks, replace finished requests."""
    fin = []
    t0 = time.thread_time()
    for s in range(B):
        if start[s] > c:
            stats["wasted"] += K; continue
        r = req[s]
        take = min(K, r[0] - r[1])
        for t in toks[s, :take].tolist():
            piece = r[2].step(fast, t)
            if piece:
                r[3] += len(piece)
            if t in STOP_IDS:
                r[4] = True  # recorded only: lengths are fixed (ignore_eos) to match the vLLM run
        r[1] += take
        stats["useful"] += take; stats["wasted"] += K - take
        if r[1] >= r[0]:
            fin.append(s)
    t1 = time.thread_time()
    if fin:
        stats["finished"] += len(fin)
        admit(fin, pg)
    stats["loop_cpu"] += t1 - t0; stats["admit_cpu"] += time.thread_time() - t1


def replay(g, evs):
    global launched
    a = torch.cuda.Event(enable_timing=True); b = torch.cuda.Event(enable_timing=True)
    a.record(); g.replay(); b.record()
    slot = launched % 2
    host[slot].copy_(out, non_blocking=True); done[slot].record()
    evs.append((a, b)); launched += 1
    return slot


prompts_np = [np.array(p, dtype=np.int64) for p in prompts]
# warm-up and capture both graphs
s0 = torch.cuda.Stream(); s0.wait_stream(torch.cuda.current_stream())
with torch.cuda.stream(s0):
    for n in SIZES:
        prefill(n)
    k_steps()
torch.cuda.current_stream().wait_stream(s0)
pg, g = {n: torch.cuda.CUDAGraph() for n in SIZES}, torch.cuda.CUDAGraph()
for n in SIZES:
    with torch.cuda.graph(pg[n]):
        prefill(n)
with torch.cuda.graph(g):
    k_steps()
admit(list(range(B)), pg)
torch.cuda.synchronize(); pevs.clear()

evs, bcpu = [], []
if os.environ.get("MARK"):
    open(os.environ["MARK"], "w").write(str(time.time()))
w0 = time.time()
c0, t0 = os.times(), time.perf_counter()
pending = replay(g, evs)
c = 0
while time.perf_counter() - t0 < SECONDS:
    if MODE == "overlap":
        nxt = replay(g, evs)                 # GPU runs chunk c+1 while the host handles chunk c
        done[pending].synchronize()
        b0 = time.thread_time(); boundary(c, host[pending].numpy(), pg); bcpu.append(time.thread_time() - b0)
        pending = nxt
    else:
        done[pending].synchronize()
        b0 = time.thread_time(); boundary(c, host[pending].numpy(), pg); bcpu.append(time.thread_time() - b0)
        pending = replay(g, evs)
    c += 1
torch.cuda.synchronize()
wall, c1 = time.perf_counter() - t0, os.times()
cpu = (c1.user - c0.user) + (c1.system - c0.system)
busy = sum(a.elapsed_time(b) for a, b in evs) / 1e3
pbusy = sum(a.elapsed_time(b) for a, b in pevs) / 1e3
span = evs[0][0].elapsed_time(evs[-1][1]) / 1e3
r = {"engine": f"K{K}-{MODE}", "batch": B, "chunks": c, "wall_s": wall, "t_start": w0, "t_end": w0 + wall,
     "useful_tok_s": stats["useful"] / wall, "wasted_frac": stats["wasted"] / (stats["wasted"] + stats["useful"]),
     "finished": stats["finished"], "proc_cpu_cores": cpu / wall,
     "boundary_cpu_ms_per_chunk": 1e3 * float(np.mean(bcpu)), "boundary_cpu_ms_p99": 1e3 * float(np.percentile(bcpu, 99)),
     "loop_cpu_ms_per_chunk": 1e3 * stats["loop_cpu"] / c, "admit_cpu_ms_per_chunk": 1e3 * stats["admit_cpu"] / c,
     "graph_gpu_ms_per_chunk": 1e3 * busy / len(evs), "prefill_gpu_ms_per_chunk": 1e3 * pbusy / len(evs),
     "gpu_idle_frac": 1 - (busy + pbusy) / span}
print("RESULT " + json.dumps(r), flush=True)
