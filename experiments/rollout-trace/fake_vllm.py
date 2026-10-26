"""Stand-in for vLLM when testing the collection path without a GPU.

Answers every chat completion with R2E-Gym's finish action, so a rollout
goes through env setup, one LLM turn, the reward tests and cleanup.
Serves an empty /metrics.

usage: fake_vllm.py [--port 18000]
"""

import argparse

from aiohttp import web

ANSWER = ("<think>test</think>\n<function=finish>\n<parameter=command>submit</parameter>\n"
          "<parameter=result>none</parameter>\n</function>")


async def chat(req):
    await req.json()
    return web.json_response({
        "id": "fake", "object": "chat.completion", "model": "fake",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })


async def metrics(req):
    return web.Response(text="vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18000)
    a = ap.parse_args()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/metrics", metrics)
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
