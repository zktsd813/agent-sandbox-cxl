# CPU time of the host thread while it waits for ~3 s of GPU work, under the CUDA default vs blocking sync.
import ctypes, os, sys, time
mode = sys.argv[1]
if mode == "blocking":
    cu = ctypes.CDLL("libcuda.so.1")
    assert cu.cuInit(0) == 0
    dev = ctypes.c_int()
    assert cu.cuDeviceGet(ctypes.byref(dev), 0) == 0
    assert cu.cuDevicePrimaryCtxSetFlags(dev, 0x04) == 0  # CU_CTX_SCHED_BLOCKING_SYNC
import torch
a = torch.randn(4096, 4096, device="cuda")
torch.cuda.synchronize()
for wait in ("synchronize", "item"):
    t0, c0 = time.perf_counter(), time.thread_time()
    for _ in range(400):
        b = a @ a
    if wait == "synchronize":
        torch.cuda.synchronize()
    else:
        b.sum().item()
    wall, cpu = time.perf_counter() - t0, time.thread_time() - c0
    print(f"{mode:9s} {wait:11s} wall {wall:5.2f} s  host-thread CPU {cpu:5.2f} s  ({cpu / wall:4.0%})")
