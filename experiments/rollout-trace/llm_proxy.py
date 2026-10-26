"""Recording reverse proxy in front of vLLM (M2).

Each rollout gets its own base URL, http://127.0.0.1:19000/r/<rollout_id>/v1, and
the proxy forwards /r/<rollout_id>/<rest> to UPSTREAM/<rest>. For every
request it appends one line to <root>/<rollout dir>/llm.jsonl and stores the
request body gzipped under llm_req/. Rollout ids use '~' where the trace
directory has '/', e.g. W1~deepswe~<task>~g0.

Times are wall clock (time.time()) so they line up with Docker event times.

usage: llm_proxy.py --root traces --upstream http://127.0.0.1:18000 [--port 19000]
"""

import argparse
import asyncio
import gzip
import json
import time
from pathlib import Path

import aiohttp
from aiohttp import web

HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}


def rollout_dir(root, rid):
    parts = [p for p in rid.split("~") if p not in ("", ".", "..")]
    return root.joinpath(*parts)


def parse_usage(body, streamed):
    """Return (usage, finish_reason) from a chat/completions response."""
    usage, finish = None, None
    chunks = []
    if streamed:
        for line in body.splitlines():
            if line.startswith(b"data:") and line[5:].strip() not in (b"", b"[DONE]"):
                try:
                    chunks.append(json.loads(line[5:]))
                except json.JSONDecodeError:
                    pass
    else:
        try:
            chunks = [json.loads(body)]
        except json.JSONDecodeError:
            pass
    for c in chunks:
        if c.get("usage"):
            usage = c["usage"]
        for ch in c.get("choices") or []:
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    return usage, finish


class Proxy:
    def __init__(self, root, upstream):
        self.root = Path(root)
        self.upstream = upstream.rstrip("/")
        self.seq = {}
        self.session = None

    async def start(self, app):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))

    async def stop(self, app):
        await self.session.close()

    async def handle(self, req):
        t_req = time.time()
        rid = req.match_info["rid"]
        tail = req.match_info["tail"]
        body = await req.read()
        n = self.seq.get(rid, 0)
        self.seq[rid] = n + 1
        d = rollout_dir(self.root, rid)
        is_completion = req.method == "POST" and tail.endswith("completions")
        if is_completion:
            (d / "llm_req").mkdir(parents=True, exist_ok=True)
            (d / "llm_req" / f"{n:04d}.json.gz").write_bytes(gzip.compress(body))
        streamed = False
        try:
            streamed = bool(json.loads(body).get("stream")) if body else False
        except (json.JSONDecodeError, AttributeError):
            pass

        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP_HEADERS}
        rec = {"rollout": rid, "seq": n, "path": tail, "t_req": t_req,
               "req_bytes": len(body), "stream": streamed}
        t_first = None
        buf = bytearray()
        try:
            async with self.session.request(req.method, f"{self.upstream}/{tail}",
                                            params=req.query, data=body, headers=headers) as up:
                resp = web.StreamResponse(status=up.status, headers={
                    k: v for k, v in up.headers.items() if k.lower() not in HOP_HEADERS})
                await resp.prepare(req)
                async for chunk in up.content.iter_any():
                    if t_first is None:
                        t_first = time.time()
                    buf.extend(chunk)
                    await resp.write(chunk)
                await resp.write_eof()
                rec["status"] = up.status
        except Exception as e:  # upstream down or client went away
            rec["error"] = repr(e)
            resp = web.Response(status=502, text=repr(e))
        rec["t_first"] = t_first
        rec["t_done"] = time.time()
        rec["resp_bytes"] = len(buf)
        if is_completion:
            usage, finish = parse_usage(bytes(buf), streamed)
            rec["finish_reason"] = finish
            if usage:
                rec["prompt_tokens"] = usage.get("prompt_tokens")
                rec["completion_tokens"] = usage.get("completion_tokens")
                rec["cached_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            d.mkdir(parents=True, exist_ok=True)
            with (d / "llm.jsonl").open("a") as f:
                f.write(json.dumps(rec) + "\n")
        return resp


def make_app(root, upstream):
    p = Proxy(root, upstream)
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.on_startup.append(p.start)
    app.on_cleanup.append(p.stop)
    app.router.add_route("*", "/r/{rid}/{tail:.*}", p.handle)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--upstream", default="http://127.0.0.1:18000")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19000)
    a = ap.parse_args()
    web.run_app(make_app(a.root, a.upstream), host=a.host, port=a.port, access_log=None)


if __name__ == "__main__":
    main()
