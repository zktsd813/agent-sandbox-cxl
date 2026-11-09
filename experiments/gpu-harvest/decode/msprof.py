"""GPU kernel time breakdown of one msdecode decode step (eager) at a given batch."""
import sys, torch
sys.path.insert(0, sys.argv[2])
import msdecode as m
B = int(sys.argv[1])
e = m.Engine(B, 700); ids = m.prompts(B, 96)
tok = m.sample(e.forward(ids), False)
for _ in range(300): tok = m.sample(e.forward(tok[:, None]), False)  # context ~400 like mid-run
torch.cuda.synchronize()
from torch.profiler import profile, ProfilerActivity
with profile(activities=[ProfilerActivity.CUDA]) as p:
    for _ in range(10): tok = m.sample(e.forward(tok[:, None]), False)
    torch.cuda.synchronize()
print(p.key_averages().table(sort_by="cuda_time_total", row_limit=14, max_name_column_width=60))
