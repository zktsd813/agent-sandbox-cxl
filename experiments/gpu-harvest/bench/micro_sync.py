"""Separates the two costs of CUDA blocking sync: wake latency and slower CPU work after waking.
usage: micro_sync.py spin|blocking CORE GPU
Test 1: GPU sleep kernel of G ms then synchronize; overhead = wall - GPU time.
Test 2: decode-like loop: fixed Python work W, launch small kernels + GPU sleep, then .item().
        W's wall time shows the CPU speed right after the thread wakes.
"""
import ctypes, os, sys, time, statistics as st
mode, core, gpu = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.environ["CUDA_VISIBLE_DEVICES"] = gpu
os.sched_setaffinity(0, {core})
if mode == "blocking":
    cu = ctypes.CDLL("libcuda.so.1"); dev = ctypes.c_int()
    cu.cuInit(0); cu.cuDeviceGet(ctypes.byref(dev), 0); cu.cuDevicePrimaryCtxSetFlags(dev, 0x04)
import torch
x = torch.zeros(64, device="cuda"); torch.cuda.synchronize()
s, e = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
s.record(); torch.cuda._sleep(10_000_000); e.record(); torch.cuda.synchronize()
cyc_per_ms = 10_000_000 / s.elapsed_time(e)

def work(n=20000):
    t = 0
    for i in range(n):
        t += i * 3 % 7
    return t

print(f"mode={mode} core={core}")
for g in (0.05, 1, 10):
    over = []
    for _ in range(300):
        s.record(); torch.cuda._sleep(int(g * cyc_per_ms)); e.record()
        t0 = time.perf_counter(); e.synchronize(); wall = (time.perf_counter() - t0) * 1e3
        over.append(wall - s.elapsed_time(e))
    over = over[20:]
    print(f"T1 gpu={g}ms sync overhead us p50={st.median(over)*1e3:.1f} p90={st.quantiles(over, n=10)[8]*1e3:.1f}")

for g in (0, 2, 10):
    ws, its = [], []
    for _ in range(300):
        t0 = time.perf_counter(); work(); t1 = time.perf_counter()
        for _ in range(50):
            x.add_(1)
        if g:
            torch.cuda._sleep(int(g * cyc_per_ms))
        x[0].item(); t2 = time.perf_counter()
        ws.append((t1 - t0) * 1e3); its.append((t2 - t0) * 1e3)
    ws, its = ws[20:], its[20:]
    print(f"T2 gpu={g}ms python work ms p50={st.median(ws):.3f} p90={st.quantiles(ws, n=10)[8]:.3f} iter ms p50={st.median(its):.3f}")
