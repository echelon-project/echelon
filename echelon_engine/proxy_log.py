#!/usr/bin/env python
"""proxy_log — a THIN, transform-free Gemini proxy that exists ONLY to DUMP the wire.

Why this exists (owner, 2026-06-28): the full proxy (proxy.py) transforms every request
(intent-gate, schema sanitize, cache split, thought_signature round-trip) — so when claude-gem
400s on "missing thought_signature in functionCall parts" on the 2nd tool call, we can't tell
WHERE the signature dies: in our SSE out, in Claude Code's echo back, or in our genai rebuild.

This proxy does the MINIMUM: forward the Anthropic request to ECHELON's WORKING GeminiProvider
(send_anthropic / send_anthropic_stream — proven to emit clean SSE), and DUMP, verbatim, both
sides of every /v1/messages exchange to one readable file:
  - REQUEST  IN : the full JSON body (so we see the tool_use blocks Claude Code echoes BACK,
                  incl. whether the thought_signature survived its round-trip)
  - SSE     OUT : every event frame we emit (so we see what Claude Code RECEIVED to echo)

No intent-gate, no schema mangling, no cache split, no anchor — nothing that could itself cause
or mask the bug. Read the dump, diff "what we sent" vs "what came back", and the signature's
death point is visible. Once found, the fix lands in gemini.py; this stays as the wire tap.

Usage:
  python -X utf8 -m echelon_engine.proxy_log [--port 18799] [--dump PATH]
  then point a client at http://127.0.0.1:18799
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


class WireDump:
    """Append-only, human-readable dump of both sides of each exchange."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self._w(f"\n{'='*70}\n=== proxy_log session start {datetime.now(timezone.utc).isoformat()} ===\n")

    def _w(self, s: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(s)

    def request_in(self, body: dict) -> int:
        self.n += 1
        n = self.n
        # surface the tool_use blocks specifically — that's where the signature rides
        tu = []
        for m in body.get("messages", []):
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result"):
                        tu.append({"type": b.get("type"), "name": b.get("name"),
                                   "id": b.get("id") or b.get("tool_use_id"),
                                   "has_sig": "_gem_thought_signature" in b,
                                   "sig": b.get("_gem_thought_signature", "")[:24]})
        self._w(f"\n{'─'*70}\n[{_ts()}] >>> REQUEST #{n} IN  (stream={body.get('stream')}, "
                f"msgs={len(body.get('messages', []))}, tools={len(body.get('tools', []) or [])})\n")
        if tu:
            self._w(f"    tool blocks echoed back: {json.dumps(tu, ensure_ascii=False)}\n")
        self._w("    FULL BODY:\n" + json.dumps(body, ensure_ascii=False, indent=1) + "\n")
        return n

    def sse_out(self, n: int, frame: str) -> None:
        self._w(f"[{_ts()}] <<< SSE #{n}: {frame.rstrip()}\n")

    def note(self, n: int, msg: str) -> None:
        self._w(f"[{_ts()}] ::: #{n}: {msg}\n")


async def handler(request):
    from starlette.responses import JSONResponse, StreamingResponse, Response
    app = request.app
    dump: WireDump = app.state.dump
    path = request.url.path

    # count_tokens — answer locally so the client's pre-flight doesn't error (no transform of /messages).
    if path.endswith("/count_tokens") and request.method == "POST":
        body = json.loads(await request.body() or b"{}")
        approx = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
        return JSONResponse({"input_tokens": max(1, approx // 4)})

    if not (path.endswith("/messages") and request.method == "POST"):
        return JSONResponse({"error": "proxy_log only serves /v1/messages"}, status_code=404)

    body = json.loads(await request.body())
    n = dump.request_in(body)
    provider = app.state.provider

    if body.get("stream"):
        async def gen():
            try:
                async for ev in provider.send_anthropic_stream(body):
                    s = ev if isinstance(ev, str) else ev.decode("utf-8", "replace")
                    dump.sse_out(n, s)
                    yield s.encode("utf-8") if isinstance(s, str) else s
            except Exception as e:
                import traceback
                dump.note(n, f"STREAM EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                raise
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})
    else:
        try:
            resp = provider.send_anthropic(body)
            result = type(provider).anthropic_response(resp, getattr(resp, "model_id", None))
            dump.note(n, "NONSTREAM RESULT:\n" + json.dumps(result, ensure_ascii=False, indent=1))
            return JSONResponse(result)
        except Exception as e:
            import traceback
            dump.note(n, f"NONSTREAM EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return JSONResponse({"error": str(e)}, status_code=500)


async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "proxy": "log-only", "provider": "gemini",
                         "dump": str(request.app.state.dump.path)})


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="echelon proxy-log",
                                 description="Thin transform-free Gemini proxy that dumps the wire.")
    ap.add_argument("--port", type=int, default=18799)
    ap.add_argument("--dump", default=str(Path.home() / ".echelon" / "_proxy" / "wire.log"))
    a = ap.parse_args(argv or [])

    from echelon_engine.atoms.providers.gemini import GeminiProvider
    from starlette.applications import Starlette
    from starlette.routing import Route
    from contextlib import asynccontextmanager
    import uvicorn

    dump = WireDump(Path(a.dump))
    provider = GeminiProvider()

    # send_anthropic is sync — wrap async for the streaming interface uniformity
    import asyncio
    _orig = provider.send_anthropic
    async def _async_send(b):
        return _orig(b)
    # (send_anthropic_stream is already async on the provider; send_anthropic stays sync, called directly)

    @asynccontextmanager
    async def lifespan(app):
        app.state.dump = dump
        app.state.provider = provider
        yield

    routes = [
        Route("/v1/{path:path}", handler, methods=["POST", "GET"]),
        Route("/health", health, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)

    print(f"\nproxy_log (transform-free wire tap) on http://127.0.0.1:{a.port}")
    print(f"  dump: {a.dump}")
    print(f"  point a client at  ANTHROPIC_BASE_URL=http://127.0.0.1:{a.port}")
    print("─" * 60)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
