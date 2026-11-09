"""vLLM continuous-batching baseline on the same request stream as msengine.py.
usage: vllm_cb.py BATCH SECONDS ; ASYNC=1 enables async scheduling (multiprocess engine, so CPU is the process tree).
Keeps BATCH requests in flight through LLMEngine.add_request/step and counts generated tokens (DELTA outputs).
"""
import json, os, sys, time
import psutil


def tree_cpu(p):
    t = 0.0
    for q in [p] + p.children(recursive=True):
        try:
            c = q.cpu_times(); t += c.user + c.system
        except psutil.NoSuchProcess:
            pass
    return t


def main():
    B, SECONDS = int(sys.argv[1]), float(sys.argv[2])
    asyn = os.environ.get("ASYNC") == "1"
    if not asyn:
        os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")
    from vllm import LLM, SamplingParams
    from vllm.inputs import TokensPrompt
    from vllm.sampling_params import RequestOutputKind
    from cbreq import requests, MODEL
    _, prompts, lens = requests()
    llm = LLM(MODEL, gpu_memory_utilization=0.5, max_model_len=1024, max_num_seqs=B, seed=1, async_scheduling=asyn)
    eng = llm.llm_engine
    nxt = 0

    def add():
        nonlocal nxt
        i = nxt; nxt += 1
        sp = SamplingParams(temperature=1.0, max_tokens=int(lens[i]), ignore_eos=True, seed=i,
                            output_kind=RequestOutputKind.DELTA)
        eng.add_request(str(i), TokensPrompt(prompt_token_ids=prompts[i % len(prompts)]), sp)

    def run(seconds):
        live, toks, steps = 0, 0, 0
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            while live < B:
                add(); live += 1
            for o in eng.step():
                toks += len(o.outputs[0].token_ids)
                if o.finished:
                    live -= 1
            steps += 1
        return toks, steps, live

    me = psutil.Process()
    run(15)                                   # warm-up
    if os.environ.get("MARK"):
        open(os.environ["MARK"], "w").write(str(time.time()))
    w0 = time.time()
    c0, t0 = tree_cpu(me), time.perf_counter()
    toks, steps, live = run(SECONDS)
    wall, cpu = time.perf_counter() - t0, tree_cpu(me) - c0
    r = {"engine": "vllm-async" if asyn else "vllm-sync", "batch": B, "steps": steps, "wall_s": wall, "t_start": w0, "t_end": w0 + wall,
         "useful_tok_s": toks / wall, "proc_cpu_cores": cpu / wall, "ms_per_step": wall / steps * 1e3}
    print("RESULT " + json.dumps(r), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
