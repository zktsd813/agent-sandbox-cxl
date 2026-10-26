"""End-to-end check of the trace tools without a GPU.

Starts a fake OpenAI-compatible upstream, llm_proxy, docker_events and
cgroup_sampler; runs one labelled busybox container that executes a few
commands and makes two LLM calls (one streamed); then checks that llm,
exec and cgroup records all land in the same rollout directory. Pulls
busybox through DockerSafety and removes only what it pulled.

usage: selftest.py <scratch dir>
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from aiohttp import web

from docker_safety import LABEL_ROLLOUT, LABEL_RUN, DockerSafety

HERE = Path(__file__).resolve().parent
PY = sys.executable
RUN = f"selftest-{int(time.time())}"
RID = "SELF~fake~task1~g0"
IMAGE = "busybox:1.36.1"


async def fake_chat(req):
    body = await req.json()
    usage = {"prompt_tokens": 11, "completion_tokens": 3,
             "prompt_tokens_details": {"cached_tokens": 8}}
    if body.get("stream"):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        for tok in ["a", "b", "c"]:
            await resp.write(f'data: {json.dumps({"choices": [{"delta": {"content": tok}}]})}\n\n'.encode())
        await resp.write(f'data: {json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage})}\n\n'.encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp
    return web.json_response({"choices": [{"message": {"content": "abc"}, "finish_reason": "stop"}],
                              "usage": usage})


def serve_fake(port):
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake_chat)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


def post(url, payload):
    r = urllib.request.Request(url, data=json.dumps(payload).encode(),
                               headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=10).read()


def main():
    scratch = Path(sys.argv[1])
    root = scratch / "traces"
    procs = []

    def bg(*args):
        p = subprocess.Popen([PY, *args], cwd=HERE)
        procs.append(p)
        return p

    safety = DockerSafety(scratch / "state", RUN, min_free_gb=50)
    safety.snapshot()
    try:
        bg("-c", "import selftest; selftest.serve_fake(18001)")
        bg("llm_proxy.py", "--root", str(root), "--upstream", "http://127.0.0.1:18001", "--port", "19001")
        bg("docker_events.py", "--root", str(root), "--run", RUN)
        bg("cgroup_sampler.py", "--root", str(root), "--run", RUN)
        time.sleep(2)

        safety.pull(IMAGE)
        c = safety.client.containers.run(IMAGE, "sleep 60", detach=True,
                                         labels={LABEL_RUN: RUN, LABEL_ROLLOUT: RID})
        base = f"http://127.0.0.1:19001/r/{RID}/v1"
        post(f"{base}/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        c.exec_run(["sh", "-c", "i=0; while [ $i -lt 300000 ]; do i=$((i+1)); done"])
        post(f"{base}/chat/completions", {"model": "m", "stream": True,
                                          "messages": [{"role": "user", "content": "again"}]})
        c.exec_run(["sh", "-c", "exit 3"])
        time.sleep(1.5)
        removed = safety.remove_containers()
        time.sleep(1.5)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()
    subprocess.run([PY, "docker_events.py", "--root", str(root), "--split"], cwd=HERE, check=True)
    image_removed = safety.remove_image(IMAGE)

    d = root / "SELF" / "fake" / "task1" / "g0"
    llm = [json.loads(l) for l in (d / "llm.jsonl").read_text().splitlines()]
    execs = [json.loads(l) for l in (d / "exec.jsonl").read_text().splitlines()]
    cg = list((d / "cgroup").glob("*.csv"))
    rows = cg[0].read_text().splitlines() if cg else []
    checks = {
        "2 llm records": len(llm) == 2,
        "usage parsed (plain)": llm[0].get("prompt_tokens") == 11 and llm[0].get("cached_tokens") == 8,
        "usage parsed (stream)": llm[1].get("completion_tokens") == 3 and llm[1].get("finish_reason") == "stop",
        "first byte <= done": all(r["t_req"] <= r["t_first"] <= r["t_done"] for r in llm),
        "request bodies saved": len(list((d / "llm_req").glob("*.json.gz"))) == 2,
        "2 exec records": len(execs) == 2,
        "exec cmd + exit code": execs[-1].get("exit_code") == 3 and "exit 3" in execs[-1].get("cmd", ""),
        "exec times": all(x.get("t_start_ns") and x.get("t_end_ns") for x in execs),
        "cgroup csv": len(rows) > 50,
        "container removed": len(removed) == 1,
        "pulled image removed": image_removed,
    }
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    print(f"cgroup rows: {len(rows)}; execs: {[(x.get('cmd'), x.get('exit_code')) for x in execs]}")
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
