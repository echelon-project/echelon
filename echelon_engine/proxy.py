#!/usr/bin/env python
"""ECHELON API proxy — intercept Claude Code → LLM requests, inject REFLEX anchor + context pins.

Sits between Claude Code and the upstream API (Anthropic / DeepSeek / Gemini Vertex).
On every /v1/messages request:
  - Injects active context pins (cartridge/card/atom anchors that survive context pressure)
  - Appends the ECHELON REFLEX anchor with live bank stats
On every response, checks for pin placeholder markers — missing → re-inject (self-healing).

TWO MODES:
  --provider upstream (default): forward to an Anthropic-compatible upstream.
  --provider gemini: translate Anthropic ↔ Gemini via Vertex AI (GeminiProvider).

Usage:
  python -X utf8 -m echelon_engine proxy [--port 18787] [--upstream URL] [--scope SCOPE]
  python -X utf8 -m echelon_engine proxy --provider gemini
  ECHELON_UPSTREAM=... python -X utf8 -m echelon_engine proxy

Env:
  ECHELON_UPSTREAM   upstream base URL for --provider upstream (default: https://api.anthropic.com)
  ECHELON_SCOPE       bank scope for live stats (default: echelon)
  ECHELON_PROXY_PORT  listen port (default: 18787)

Pin control endpoints (localhost-only):
  POST /_echelon/pin     — equip a context pin  {type, id, label?, content_full?, goal?}
  POST /_echelon/unpin   — remove a pin          {id}
  GET  /_echelon/pins    — list active pins
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

AGENT_ROOT = str(Path(__file__).parent.parent)


def _proxy_code_version() -> str:
    """A short content hash of the proxy's OWN behaviour-defining source — so a running
    proxy reports WHICH code it loaded. Lets us catch a stale proxy at a glance (the
    blind-spot that wastes cycles: 'is :18787 the patched build or an old one?').
    Hashes this file + the gemini provider (the two we keep patching). Falls back to a
    marker if a file can't be read — never crashes the proxy over a version stamp."""
    import hashlib
    here = Path(__file__).resolve()
    targets = [here, here.parent / "atoms" / "providers" / "gemini.py"]
    h = hashlib.sha256()
    for t in targets:
        try:
            h.update(t.read_bytes())
        except OSError:
            h.update(b"<unreadable:" + str(t).encode() + b">")
    return h.hexdigest()[:12]


PROXY_CODE_VERSION = _proxy_code_version()

from echelon_engine.pins import PinRegistry


def _live_stats(scope: str) -> dict:
    """Query the v2 bank for live REFLEX stats — fast, no model call."""
    try:
        sys.path.insert(0, AGENT_ROOT)
        from echelon_engine.atoms.cards import CardStore
        cs = CardStore()
        n = cs.count_atoms_in_scope(scope)
        # earned weight from atom_earned (the REAL metric, not use_count>0)
        rows = cs.conn.execute(
            "SELECT SUM(e.score) FROM atoms a JOIN atom_earned e ON a.id=e.atom_id WHERE a.scope=?", (scope,)).fetchone()
        earned_weight = round(float(rows[0] or 0), 1)
        from echelon_engine.atoms.cartridge_registry import all_specs
        cart_count = len(list(all_specs()))
        return {"scope": scope, "atoms": n, "earned_weight": earned_weight, "cartridges": cart_count,
                "bank": str(cs.db_path) if hasattr(cs, "db_path") else "~/.echelon/echelon.db"}
    except Exception:
        return {"scope": scope, "atoms": "?", "earned_weight": "?", "cartridges": "?",
                "bank": "~/.echelon/echelon.db"}


def _build_anchor(stats: dict) -> str:
    """The REFLEX anchor injected into the system message tail."""
    ew = stats.get("earned_weight", "?")
    # A/B lever: ECHELON_DISCIPLINE_OFF=1 strips the discipline block — the clean
    # control arm for measuring what the discipline itself contributes (2026-07-09).
    discipline = "" if os.environ.get("ECHELON_DISCIPLINE_OFF") == "1" else (
        f"\nDISCIPLINE: PROVE don't claim (re-run what failed, read outputs back) · "
        f"ISOLATE before blaming (one layer, one change, retest) · "
        f"stuck twice? read the WIRE not your theory · symptom≠cause, dig to the mechanism · "
        f"laws live in CODE not prompts · REUSE estate organs before building · "
        f"BOUND every loop/prompt/retry · act then report FAITHFULLY (failures plainly) · "
        f"FINISH the loop: atom → ingest → receipt."
    )
    return (
        f"\n\n⚡ ECHELON ACTIVE | scope={stats['scope']} | {stats['atoms']} atoms "
        f"(earned={ew}) | {stats['cartridges']} cartridges\n"
        f"Bank: {stats['bank']}\n"
        f"REFLEX: ① recall --warm \"<intent>\" before load-bearing actions "
        f"② remember <slug> [<slug2> ...] through witnessed door "
        f"③ don't ask what bank knows "
        f"④ dispute atoms that misled you "
        f"⑤ scan integrity before wrap"
        # THE DISCIPLINE (owner 2026-07-09: the difference between models driving
        # ECHELON well is discipline more than capability — so it rides every request,
        # every channel). Full text: ECHELON-AGENT/docs/DISCIPLINE.md.
        + discipline
    )


# Injected only on the gemini channel: claude-gem is vision-native and can draw.
_GEMINI_POWERS = (
    "\n🖼 GEMINI CHANNEL: you run on Gemini (vision-native), with two image powers the "
    "stock harness doesn't list:\n"
    "• READ images: use the Read tool on any .png/.jpg — real pixels reach the model "
    "through the proxy (screenshots, mockups, diagrams).\n"
    "• CREATE/EDIT images (no built-in tool — use Bash from D:\\WORK\\ECHELON-AGENT):\n"
    "    python -X utf8 -m echelon_engine imagine \"<prompt>\" -o out.png "
    "[--input ref.png] [--aspect 16:9] [--size 1K] [--quality 60]\n"
    "  Image generation takes 30–120s — run it with a generous Bash timeout (300000) or "
    "run_in_background, never the default. Then Read the output back to verify it before "
    "calling it done (imagine → Read → refine)."
)


# ── session payload log (append-only, one file per proxy session) ────────────
# OFF by default — enable with --log (session events) or --log-raw (full HTTP dump).
# The raw log grows fast (300MB+/day); both are opt-in.
import uuid as _uuid
import threading as _threading
from datetime import datetime as _datetime, timezone as _timezone

_LOG_ENABLED = False
_RAW_LOG_ENABLED = False

_SESSION_ID = _uuid.uuid4().hex[:8]
_LOG_DIR = Path.home() / ".echelon" / "_proxy"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_PATH = _LOG_DIR / f"session_{_SESSION_ID}_{_datetime.now(_timezone.utc).strftime('%Y%m%d')}.log"
_LOG_LOCK = _threading.Lock()

# Raw HTTP log — headers + body for EVERY request through the proxy, unfiltered
_RAW_LOG_PATH = _LOG_DIR / f"raw_{_SESSION_ID}_{_datetime.now(_timezone.utc).strftime('%Y%m%d')}.log"

# Last few requests in memory for the /_echelon/dump endpoint
_DUMP_RING: list[dict] = []
_DUMP_MAX = 5


def _log_event(direction: str, payload: dict) -> None:
    """Append a REQ or RESP event to the session log file. No-op unless --log was passed."""
    if not _LOG_ENABLED:
        return
    ts = _datetime.now(_timezone.utc).isoformat()
    record = {"ts": ts, "dir": direction, "payload": payload}
    try:
        with _LOG_LOCK:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # log is best-effort — never break the session


def _log_raw_http(direction: str, method: str, path: str, headers: dict,
                  body: bytes | str | None, status: int | None = None) -> None:
    """Log EVERY HTTP request/response through the proxy with full headers + body.
    Written to a separate raw log (can be large). No-op unless --log-raw was passed.
    Body is stored as base64 if binary, string otherwise. Truncated at 512KB per entry."""
    if not _RAW_LOG_ENABLED:
        return
    ts = _datetime.now(_timezone.utc).isoformat()
    body_repr: str | None = None
    if body is not None:
        if isinstance(body, bytes):
            if len(body) > 524288:  # 512KB cap
                body_repr = f"[BINARY {len(body)} bytes, truncated] {body[:200].hex()}"
            else:
                body_repr = f"[BINARY {len(body)} bytes] {body.hex()}"
        else:
            if len(body) > 524288:
                body_repr = body[:524288] + f"\n... [TRUNCATED {len(body)-524288} chars]"
            else:
                body_repr = body
    record = {
        "ts": ts,
        "dir": direction,
        "method": method,
        "path": path,
        "headers": {k: v for k, v in (headers or {}).items()},
        "body": body_repr,
    }
    if status is not None:
        record["status"] = status
    try:
        with _LOG_LOCK:
            with open(_RAW_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _dump_request(payload: dict) -> None:
    """Log the request + capture a preview for the /_echelon/dump ring buffer."""
    # Strip stream/content for logging (too large)
    log_payload = {k: v for k, v in payload.items() if k not in ("stream",)}
    _log_event("REQ", log_payload)
    # Ring buffer preview
    system = payload.get("system")
    if isinstance(system, str):
        preview = system[-800:] if len(system) > 800 else system
    elif isinstance(system, list):
        texts = [b.get("text", "") for b in system if isinstance(b, dict)]
        preview = "\n".join(texts)[-800:]
    else:
        msgs = payload.get("messages", [])
        sys_msgs = [m.get("content", "") for m in msgs if m.get("role") == "system"]
        preview = "\n".join(sys_msgs)[-800:] if sys_msgs else "(no system block)"
    _DUMP_RING.append({
        "ts": _datetime.now(_timezone.utc).timestamp(),
        "system_preview": preview,
        "model": payload.get("model", "?"),
    })
    if len(_DUMP_RING) > _DUMP_MAX:
        _DUMP_RING.pop(0)


# ── noise filter + cache optimizer + context enricher ─────────────────────
# Three moves that make the proxy a smart intermediary:
#   1. NOISE FILTER — deduplicate repetitive harness bloat (saves ~3K tokens/req)
#   2. CACHE OPTIMIZER — split messages into FROZEN (front, stable, cache hit)
#      and DYNAMIC (tail, wrapped as ##STATUS, changes each request). The frozen
#      prefix stays byte-identical across requests → reliable cache hit.
#   3. CONTEXT ENRICHER — when running WITHOUT Claude harness, inject git status,
#      date, bank overview.

_NOISE_PATTERNS = [
    "The task tools haven't been used recently.",
    "## Auto Mode Active",
    "## Exited Auto Mode",
    "## Exited Plan Mode",
    "The user sent a new message while you were working:",
]

# Harness signals that indicate Claude Code context (vs bare agent loop)
_HARNESS_SIGNALS = ["gitStatus", "claudeMd", "git status", "Current branch:"]

# System message content signals that indicate DYNAMIC (cache-busting) content.
# These change every request — file notices, opened-file hints, mode changes.
_DYNAMIC_SIGNALS = [
    "was modified, either by the user or by a linter",
    "The user opened the file",
    "Exited Plan Mode",
    "Exited Auto Mode",
    "Auto Mode Active",
    "The task tools haven't been used recently",
    "The user sent a new message while you were working",
]


def _is_dynamic(msg: dict) -> bool:
    """True if this system message changes between requests. Check the HEAD
    (first 300 chars) for dynamic signals — large warm-up blobs often have
    file notices concatenated deep inside, but the HEAD determines classification."""
    content = str(msg.get("content", ""))
    head = content[:300]
    if any(sig in head for sig in _DYNAMIC_SIGNALS):
        return True
    # Large blocks whose head is clean are FROZEN — warm-up, CLAUDE.md, gate
    if len(content) > 2000:
        return False
    # Small messages: if they match noise patterns, they're dynamic
    if any(p in head for p in _NOISE_PATTERNS):
        return True
    # Small messages with no clear signal: check full content for dynamic signals
    return any(sig in content for sig in _DYNAMIC_SIGNALS)


def _optimize_messages(messages: list[dict]) -> list[dict]:
    """Noise filter + split into FROZEN (front) / DYNAMIC (tail) for cache stability.
    FROZEN messages are sorted by content hash for stable ordering.
    DYNAMIC messages are wrapped in a ##STATUS block at the bottom."""
    frozen: list[dict] = []
    dynamic: list[dict] = []
    user_assistant: list[dict] = []
    seen_hashes: set[int] = set()
    dropped = 0

    for m in messages:
        if m.get("role") == "system":
            content = str(m.get("content", ""))
            is_noise = any(p in content for p in _NOISE_PATTERNS)
            if is_noise:
                h = hash(content)
                if h in seen_hashes:
                    dropped += 1
                    continue
                seen_hashes.add(h)
            if _is_dynamic(m):
                dynamic.append(m)
            else:
                frozen.append(m)
        else:
            user_assistant.append(m)

    if dropped:
        import sys as _sys
        print(f"[echelon proxy] noise filter: dropped {dropped} duplicate harness messages",
              file=_sys.stderr)

    # Sort frozen by content hash → byte-identical across requests → cache hit
    frozen.sort(key=lambda m: hash(str(m.get("content", ""))))

    # Wrap dynamic messages in a ##STATUS block at the tail
    result = list(frozen)
    if dynamic:
        status_lines = ["##STATUS"]
        for m in dynamic:
            status_lines.append(str(m.get("content", "")))
        result.append({"role": "system", "content": "\n\n".join(status_lines)})
    result.extend(user_assistant)
    return result


def _enrich_context(messages: list[dict], harness_name: str = "") -> list[dict]:
    """Inject context appropriate for the harness.
    Claude Code provides git status, CLAUDE.md, etc. — other harnesses may not.
    The harness adapter tells us what's missing."""
    all_text = "\n".join(str(m.get("content", "")) for m in messages)
    has_harness = any(sig in all_text for sig in _HARNESS_SIGNALS)

    # Only inject when running with a non-Claude-Code harness that lacks context
    if has_harness and harness_name == "claude-code":
        return messages  # Claude Code already provides this
    if harness_name == "claude-code" and has_harness:
        return messages

    # Non-Claude harness or no harness signals detected — inject context
    import time as _time
    import subprocess as _sp
    enrichments: list[str] = []

    # Date
    enrichments.append(f"currentDate: {_time.strftime('%Y-%m-%d')}")

    # Git status (best-effort, first 500 chars)
    try:
        r = _sp.run(["git", "status", "--short"], capture_output=True, text=True,
                    cwd=os.getcwd(), timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            enrichments.append(f"gitStatus: {r.stdout.strip()[:600]}")
        # Recent commits
        r2 = _sp.run(["git", "log", "--oneline", "-5"], capture_output=True, text=True,
                     cwd=os.getcwd(), timeout=5)
        if r2.returncode == 0 and r2.stdout.strip():
            enrichments.append(f"Recent commits: {r2.stdout.strip()}")
    except Exception:
        pass

    # Bank overview
    try:
        from echelon_engine.atoms.cards import CardStore
        cs = CardStore()
        n = cs.count_atoms_in_scope("echelon")
        enrichments.append(f"Bank: ~/.echelon/echelon.db ({n} atoms, scope=echelon)")
    except Exception:
        pass

    context = "\n".join(enrichments)
    messages.insert(0, {"role": "system", "content": f"<system-reminder>\n{context}\n</system-reminder>"})
    return messages


# ── HTTP proxy ──────────────────────────────────────────────────────────────

async def proxy_handler(request):
    """Handle every incoming HTTP request. Intercept /v1/messages, route through provider."""
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "transfer-encoding")}
    body = await request.body()

    # ── Log EVERY request (raw, unfiltered) ──────────────────────────────
    _log_raw_http("REQ", request.method, str(request.url.path),
                  {k: v for k, v in request.headers.items()}, body)

    # Intercept messages requests to inject REFLEX anchor + context pins
    if request.url.path.endswith("/messages") and request.method == "POST":
        try:
            raw_payload = json.loads(body)

            # ── Step 0: parse through harness adapter ──
            harness_adapter = getattr(request.app.state, "harness", None)
            if harness_adapter is not None:
                payload = harness_adapter.parse_request(raw_payload)
            else:
                payload = raw_payload

            # ── Step 1: optimize messages (noise filter + cache-stable ordering) ──
            msgs = payload.get("messages", [])
            if msgs:
                payload["messages"] = _optimize_messages(msgs)
            # ── Step 2: enrich context if running without Claude harness ──
            if msgs:
                hn = getattr(harness_adapter, "name", "") if harness_adapter is not None else ""
                payload["messages"] = _enrich_context(payload["messages"], hn)

            _dump_request(payload)

            # ── Step 3: inject REFLEX anchor + context pins ──
            pins = request.app.state.pins
            pins_text = pins.build_system_text() if pins is not None else ""
            anchor = _build_anchor(request.app.state.stats)
            supplement = (pins_text + "\n" + anchor) if pins_text else anchor
            # On the gemini channel the model has powers the stock Claude harness doesn't
            # advertise — tell it, or it never reaches for them (claude-gem 2026-07-09).
            if getattr(request.app.state, "provider_mode", "") == "gemini":
                supplement += _GEMINI_POWERS

            # ── Step 3b: INTENT GATE — give non-Claude models the clarify instinct ──
            # Harness-agnostic by construction: every harness flows through here, so the
            # gate covers them all. Fires only for non-Claude models on high build-risk
            # requests (ambiguous + about-to-act + ungrounded). Claude self-gates → exempt.
            if getattr(request.app.state, "gate_enabled", True):
                try:
                    from echelon_engine.proxy_gate import gate_supplement
                    gate_text, gate_verdict = gate_supplement(payload)
                    if gate_text:
                        supplement = supplement + gate_text
                        _log_event("GATE", gate_verdict)
                        import sys as _s
                        print(f"[echelon proxy] intent-gate FIRED "
                              f"(model={gate_verdict['model']} score={gate_verdict['score']} "
                              f"reasons={gate_verdict['reasons']})", file=_s.stderr)
                except Exception:
                    pass  # the gate must never break a request

            # Build the final request through the harness adapter
            if harness_adapter is not None:
                # Inject ECHELON context via the harness adapter
                system_block = payload.get("system")
                if system_block:
                    if isinstance(system_block, str):
                        payload["system"] = system_block + supplement
                    elif isinstance(system_block, list):
                        system_block.append({"type": "text", "text": supplement})
                else:
                    msgs = payload.get("messages", [])
                    if msgs and msgs[0].get("role") == "system":
                        msgs[0]["content"] = msgs[0]["content"] + supplement
                    else:
                        msgs.insert(0, {"role": "system", "content": supplement.lstrip()})
                # Let the harness build the final request
                payload = harness_adapter.build_request(payload, supplement)
            else:
                # Legacy path (no harness adapter loaded)
                system_block = payload.get("system")
                if system_block:
                    if isinstance(system_block, str):
                        payload["system"] = system_block + supplement
                    elif isinstance(system_block, list):
                        system_block.append({"type": "text", "text": supplement})
                else:
                    msgs = payload.get("messages", [])
                    if msgs and msgs[0].get("role") == "system":
                        msgs[0]["content"] = msgs[0]["content"] + supplement
                    else:
                        msgs.insert(0, {"role": "system", "content": supplement.lstrip()})

            body = json.dumps(payload).encode("utf-8")
            if "content-length" in {k.lower() for k in headers}:
                headers["content-length"] = str(len(body))
        except Exception:
            pass  # If we can't parse, forward unchanged — don't break the session

    # ── Provider routing (gemini or upstream) ──────────────────────────
    provider_mode = getattr(request.app.state, "provider_mode", "upstream")
    # ── count_tokens: Claude Code calls this BEFORE every turn. In gemini mode there is no
    # upstream to pass it through to (that only exists for upstream/DeepSeek), so an unhandled
    # /messages/count_tokens fell through to a None response → the client errored every turn
    # ("API error retrying"). Answer it locally with a char/4 estimate in Anthropic shape — the
    # count is advisory (Claude Code uses it for context budgeting, not correctness). ──────────
    if (provider_mode == "gemini" and request.method == "POST"
            and request.url.path.endswith("/count_tokens")):
        from starlette.responses import JSONResponse
        try:
            payload = json.loads(body)
        except Exception:
            return _error(400, "Invalid JSON body")
        approx = 0
        sysblk = payload.get("system")
        if isinstance(sysblk, str):
            approx += len(sysblk)
        elif isinstance(sysblk, list):
            approx += sum(len(str(b.get("text", ""))) for b in sysblk if isinstance(b, dict))
        for m in payload.get("messages", []):
            c = m.get("content")
            if isinstance(c, str):
                approx += len(c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "image":
                        approx += 6000  # ~1500 tok flat for an image, not its base64 length
                        continue
                    inner = b.get("text", b.get("content", ""))
                    if isinstance(inner, list):  # tool_result holding blocks (maybe images)
                        approx += sum(len(str(x.get("text", ""))) + (6000 if x.get("type") == "image" else 0)
                                      for x in inner if isinstance(x, dict))
                    else:
                        approx += len(str(inner))
        return JSONResponse({"input_tokens": max(1, approx // 4)}, status_code=200)

    if provider_mode and request.url.path.endswith("/messages") and request.method == "POST":
        try:
            payload = json.loads(body)
        except Exception:
            return _error(400, "Invalid JSON body")

        provider = await _get_provider(request)
        is_stream = payload.get("stream", False)

        try:
            if is_stream:
                from starlette.responses import StreamingResponse

                async def sse_stream():
                    async for event in provider.send_anthropic_stream(payload):
                        # Translate through harness adapter for non-Claude-Code clients
                        if harness_adapter is not None and harness_adapter.name != "claude-code":
                            try:
                                chunk_data = json.loads(event) if isinstance(event, str) else event
                                translated = harness_adapter.stream_chunk(chunk_data)
                                yield ("data: " + json.dumps(translated, ensure_ascii=False) + "\n\n").encode("utf-8")
                            except Exception:
                                yield event.encode("utf-8") if isinstance(event, str) else event
                        else:
                            yield event.encode("utf-8") if isinstance(event, str) else event

                return StreamingResponse(
                    sse_stream(), status_code=200,
                    headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                )
            else:
                from starlette.responses import JSONResponse
                resp = await provider.send_anthropic(payload)
                result = type(provider).anthropic_response(resp, resp.model_id)
                # Translate through harness adapter for non-Claude-Code clients
                if harness_adapter is not None and harness_adapter.name != "claude-code":
                    try:
                        result = harness_adapter.parse_response(result, stream=False)
                    except Exception:
                        pass  # If translation fails, return Anthropic format as fallback
                _log_event("RESP", {"model": resp.model_id, "stop_reason": getattr(resp, "stop_reason", None),
                                    "usage": getattr(resp, "usage", None) if hasattr(resp, "usage") else None,
                                    "content_len": len(str(result.get("content", ""))) if isinstance(result, dict) else 0})
                return JSONResponse(result, status_code=200)
        except Exception as e:
            import traceback as _tb
            _tb.print_exc(file=sys.stderr)
            _log_event("ERR", {"error": f"{type(e).__name__}: {e}", "traceback": _tb.format_exc()[-2000:]})
            return _error(500, f"Provider error: {type(e).__name__}: {e}")

    # ── Non-messages passthrough (upstream mode only) ──────────────────
    if provider_mode == "upstream":
        import httpx
        from starlette.responses import Response
        provider = await _get_provider(request)
        client = await provider._get_client()
        upstream = request.app.state.upstream
        target = f"{upstream}{request.url.path}"
        if request.url.query:
            q = request.url.query
            target += f"?{q.decode()}" if isinstance(q, bytes) else f"?{q}"
        try:
            upstream_resp = await client.request(
                method=request.method, url=target, headers=headers,
                content=body, timeout=httpx.Timeout(300.0, connect=10.0))
        except Exception as e:
            return _error(502, f"Upstream error: {e}")
        resp_headers = {k: v for k, v in upstream_resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "content-length", "content-encoding")}
        return Response(content=upstream_resp.content,
                        status_code=upstream_resp.status_code,
                        headers=resp_headers)

    # ── Catch-all: in gemini mode there's no upstream to pass through to, so any path that isn't
    # /messages or /count_tokens (a stray GET, a probe) would fall through and return None →
    # Starlette "'NoneType' object is not callable". Return a clean 404 instead. (2026-06-28) ──
    from starlette.responses import JSONResponse
    return JSONResponse({"error": f"path not served in {provider_mode} mode: {request.url.path}"},
                        status_code=404)


async def _get_provider(request):
    """Lazy-init and return the active provider (gemini or upstream).
    Both implement send_anthropic() / send_anthropic_stream() / anthropic_response()."""
    if request.app.state._provider is not None:
        return request.app.state._provider

    mode = request.app.state.provider_mode
    if mode == "gemini":
        from echelon_engine.atoms.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        # Wrap sync send_anthropic for the async interface
        provider.send_anthropic = _sync_to_async(provider.send_anthropic)
        request.app.state._provider = provider
        return provider
    else:
        from echelon_engine.atoms.providers.anthropic_upstream import AnthropicUpstreamProvider
        provider = AnthropicUpstreamProvider(
            upstream_url=request.app.state.upstream,
            api_key=request.app.state.api_key or "",
        )
        request.app.state._provider = provider
        return provider


def _sync_to_async(fn):
    """Wrap a sync function as async for the uniform provider interface.

    fn (GeminiProvider.send_anthropic) is a BLOCKING Vertex call. Calling it directly
    inside the async wrapper would run it ON the event loop and freeze uvicorn for the
    whole round-trip → the harness times out ('API error · Retrying'). Offload to a
    worker thread so the loop stays responsive. (Mirror of the stream-path fix in
    GeminiProvider.send_anthropic_stream; async-inherited-from-the-entry-call.)"""
    import asyncio as _asyncio
    async def wrapper(*a, **kw):
        return await _asyncio.to_thread(fn, *a, **kw)
    return wrapper


def _error(code: int, msg: str):
    from starlette.responses import JSONResponse
    return JSONResponse({"error": msg}, status_code=code)


# ── Pin control endpoints (thin wrappers over PinRegistry) ──────────────

async def pin_handler(request):
    """POST /_echelon/pin — equip a context pin.
    Body: {type, id, label?, content_full?, goal?}
    If type=cartridge and content_full is not provided, calls cartridge.equip()."""
    from starlette.responses import JSONResponse
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    pin_type = (data.get("type") or "").strip()
    pin_id = (data.get("id") or "").strip()
    if not pin_type or not pin_id:
        return JSONResponse({"error": "'type' and 'id' are required"}, status_code=400)

    label = (data.get("label") or "").strip()
    content_full = (data.get("content_full") or "").strip()
    goal = (data.get("goal") or "").strip()

    # Auto-generate content for cartridge-type pins
    if pin_type == "cartridge" and not content_full:
        if not goal:
            return JSONResponse(
                {"error": "'goal' is required for cartridge-type pins without content_full"},
                status_code=400)
        try:
            from echelon_engine.atoms.cartridge import equip as cartridge_equip
            from echelon_engine.atoms.cards import CardStore
            from echelon_engine.atoms.cartridge_registry import get as spec_lookup
            cs = CardStore()
            content_full = cartridge_equip(pin_id, goal, cs=cs)
            if not label:
                spec = spec_lookup(pin_id)
                label = spec.summary if spec else f"{pin_id} cartridge"
        except Exception as e:
            return JSONResponse({"error": f"cartridge equip failed: {e}"}, status_code=500)

    if not content_full:
        return JSONResponse({"error": "'content_full' is required (or 'goal' for cartridge type)"},
                            status_code=400)
    if not label:
        label = f"{pin_type}:{pin_id}"

    try:
        full_id = f"{pin_type}:{pin_id}"
        pin = await request.app.state.pins.add(full_id, pin_type, label, content_full)
        return JSONResponse({
            "status": "pinned",
            "id": pin.pin_id,
            "type": pin.pin_type,
            "label": pin.label,
            "placeholder": pin.placeholder,
            "state": pin.state,
            "content_size": len(pin.content_full),
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=429)


async def unpin_handler(request):
    """POST /_echelon/unpin — remove a context pin. Body: {id}"""
    from starlette.responses import JSONResponse
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    pin_id = (data.get("id") or "").strip()
    if not pin_id:
        return JSONResponse({"error": "'id' is required"}, status_code=400)

    removed = await request.app.state.pins.remove(pin_id)
    return JSONResponse({"status": "unpinned" if removed else "not_found",
                         "id": pin_id, "removed": removed})


async def pins_handler(request):
    """GET /_echelon/pins — list all active context pins."""
    from starlette.responses import JSONResponse
    pins = await request.app.state.pins.list()
    return JSONResponse({
        "count": len(pins),
        "max": request.app.state.pins.MAX_PINS,
        "pins": [{"id": p.pin_id, "type": p.pin_type, "label": p.label,
                  "state": p.state, "stale_count": p.stale_count,
                  "placeholder": p.placeholder}
                 for p in pins],
    })


async def dump_handler(request):
    """GET /_echelon/dump — last N intercepted system prompts (ring buffer) + session log path."""
    from starlette.responses import JSONResponse
    import time as _time
    now = _time.time()
    entries = []
    for d in _DUMP_RING:
        entries.append({
            "ts": d["ts"],
            "ago_sec": round(now - d["ts"], 1),
            "model": d["model"],
            "system_tail": d["system_preview"],
        })
    return JSONResponse({
        "session_id": _SESSION_ID,
        "log_file": str(_LOG_PATH),
        "raw_log_file": str(_RAW_LOG_PATH),
        "entries": entries,
        "anchor_injected": _build_anchor(request.app.state.stats),
    })


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="echelon proxy", description="ECHELON API proxy — inject REFLEX anchor into every request")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ECHELON_PROXY_PORT", "18787")))
    ap.add_argument("--upstream", default=os.environ.get("ECHELON_UPSTREAM", "https://api.anthropic.com"))
    ap.add_argument("--scope", default=os.environ.get("ECHELON_SCOPE", "echelon"))
    ap.add_argument("--api-key", default=os.environ.get("ECHELON_UPSTREAM_KEY", os.environ.get("ANTHROPIC_AUTH_TOKEN", "")))
    ap.add_argument("--provider", choices=["upstream", "gemini"], default=None,
                    help="upstream: forward to Anthropic-compatible API; gemini: translate to Gemini Vertex AI")
    ap.add_argument("--harness", default=os.environ.get("ECHELON_HARNESS", "claude-code"),
                    choices=["claude-code", "open-router", "copilot-chat", "gemini-cli"],
                    help="Harness adapter: claude-code (default), open-router, copilot-chat, gemini-cli")
    ap.add_argument("--no-gate", action="store_true",
                    help="disable the intent gate (the clarify-before-build directive for non-Claude models)")
    ap.add_argument("--log", action="store_true",
                    help="enable session event logging (REQ/RESP summaries to ~/.echelon/_proxy/)")
    ap.add_argument("--log-raw", action="store_true",
                    help="enable raw HTTP logging (full headers+bodies; grows fast, ~300MB+/day)")
    args = ap.parse_args(argv or [])

    # ── LLM_PROVIDER contract: resolve provider from config when --provider is None ──
    if args.provider is None:
        try:
            from echelon_sdk.config import resolve_llm_provider
            resolved = resolve_llm_provider()
            args.provider = resolved["mode"]
            if resolved.get("upstream"):
                args.upstream = resolved["upstream"]
            key = resolved.get("api_key", "")
            if key and not args.api_key:
                args.api_key = key
        except Exception as e:
            print(f"ECHELON proxy: LLM_PROVIDER resolution failed — {e}", file=sys.stderr)
            print("  Falling back to --provider upstream / https://api.anthropic.com", file=sys.stderr)
            args.provider = "upstream"

    if not args.api_key:
        print("ECHELON proxy: warning — no upstream API key set (ECHELON_UPSTREAM_KEY or ANTHROPIC_AUTH_TOKEN)", file=sys.stderr)

    # Resolve harness adapter
    try:
        from echelon_engine.harness import resolve as resolve_harness
        harness = resolve_harness(args.harness)
    except Exception as e:
        print(f"ECHELON proxy: harness '{args.harness}' unavailable — {e}", file=sys.stderr)
        print("  Falling back to claude-code", file=sys.stderr)
        from echelon_engine.harness.claude_code import ClaudeCodeAdapter
        harness = ClaudeCodeAdapter()

    # ── Logging: OFF by default; --log / --log-raw enable ──────────────────
    global _LOG_ENABLED, _RAW_LOG_ENABLED
    _LOG_ENABLED = args.log
    _RAW_LOG_ENABLED = args.log_raw

    stats = _live_stats(args.scope)
    anchor = _build_anchor(stats)

    print(f"\nECHELON proxy listening on http://localhost:{args.port}")
    print(f"  session:  {_SESSION_ID}")
    print(f"  code ver: {PROXY_CODE_VERSION}  (proxy.py + gemini.py hash — stale-proxy guard)")
    logging_parts = []
    if _LOG_ENABLED:
        logging_parts.append(f"events -> {_LOG_PATH}")
    if _RAW_LOG_ENABLED:
        logging_parts.append(f"raw -> {_RAW_LOG_PATH}")
    print(f"  logging:  {' + '.join(logging_parts) if logging_parts else 'OFF (--log / --log-raw to enable)'}")
    # ── Provider startup guard: fail loud before binding the port ──────────
    if args.provider == "gemini":
        try:
            from google import genai  # noqa: F401 — verify the dep exists BEFORE we listen
        except ImportError as e:
            print(f"ECHELON proxy: FATAL — --provider gemini requires google-genai SDK. "
                  f"Install it: pip install google-genai", file=sys.stderr)
            print(f"  Import error: {e}", file=sys.stderr)
            print(f"  Python: {sys.executable}", file=sys.stderr)
            return 1
        print(f"  google-genai: OK (vertexai-ready)")
        # Also verify the key loads NOW (not lazily on first request — fail at boot)
        try:
            from echelon_sdk.keys import load_gemini_key
            key = load_gemini_key()
            print(f"  gemini key:   loaded ({len(key)} chars, prefix={key[:12]}...)")
        except ValueError as e:
            print(f"ECHELON proxy: FATAL — Gemini key not found. "
                  f"Set GEMINI_API_VERTEX (or GEMINI_API_KEY) in ~/.echelon/.env", file=sys.stderr)
            print(f"  Error: {e}", file=sys.stderr)
            return 1

    print(f"  harness:  {args.harness} ({harness.label})")
    print(f"  provider: {args.provider}")
    if args.provider == "gemini":
        print(f"  mode:     Anthropic ↔ Gemini Vertex AI translation")
    else:
        print(f"  upstream: {args.upstream}")
    print(f"  scope:    {stats['scope']} ({stats['atoms']} atoms, earned={stats['earned_weight']}, {stats['cartridges']} cartridges)")
    print(f"  bank:     {stats['bank']}")
    print(f"\nAnchor injected into every system message:")
    print(f"{anchor}")
    print(f"\nSet ANTHROPIC_BASE_URL=http://localhost:{args.port} in .env.echelon")
    print("─" * 60)

    # Build the Starlette app
    from starlette.applications import Starlette
    from starlette.routing import Route
    from contextlib import asynccontextmanager

    stats_data = _live_stats(args.scope)
    anchor_text = _build_anchor(stats_data)

    @asynccontextmanager
    async def lifespan(_app):
        _app.state.provider_mode = args.provider
        _app.state.upstream = args.upstream.rstrip("/")
        _app.state.api_key = args.api_key
        _app.state.stats = stats_data
        _app.state.anchor = anchor_text
        _app.state.harness = harness
        _app.state.gate_enabled = not args.no_gate
        _app.state.pins = PinRegistry()
        _app.state._provider = None  # lazy-init on first request
        yield
        if _app.state._provider is not None:
            p = _app.state._provider
            if hasattr(p, "aclose"):
                await p.aclose()

    async def health(request):
        from starlette.responses import JSONResponse
        s = request.app.state.stats
        pins_snapshot = request.app.state.pins.snapshot()
        h = request.app.state.harness
        return JSONResponse({"status": "ok",
                             "harness": h.name, "harness_label": h.label,
                             "provider": request.app.state.provider_mode,
                             "code_version": PROXY_CODE_VERSION,
                             "stats": s, "anchor": request.app.state.anchor,
                             "pins": len(pins_snapshot),
                             "python_exe": sys.executable,
                             "engine_dir": AGENT_ROOT})

    routes = [
        Route("/v1/{path:path}", proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
        Route("/health", health, methods=["GET"]),
        # Pin control (must come before catch-all)
        Route("/_echelon/pin", pin_handler, methods=["POST"]),
        Route("/_echelon/unpin", unpin_handler, methods=["POST"]),
        Route("/_echelon/pins", pins_handler, methods=["GET"]),
        Route("/_echelon/dump", dump_handler, methods=["GET"]),
        # Catch-all for everything else (proxied upstream)
        Route("/{path:path}", proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
    ]

    # ── Raw response logging middleware ────────────────────────────────────
    from starlette.middleware.base import BaseHTTPMiddleware

    class RawResponseLogger(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            # Log what we can without breaking streaming responses
            resp_body = None
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                # Non-streaming: read body safely
                try:
                    if hasattr(response, "body"):
                        resp_body = response.body
                    elif hasattr(response, "raw_body"):
                        resp_body = response.raw_body
                except Exception:
                    pass
            _log_raw_http("RESP", request.method, str(request.url.path),
                          dict(response.headers), resp_body,
                          status=response.status_code)
            return response

    app = Starlette(routes=routes, lifespan=lifespan)
    if _RAW_LOG_ENABLED:
        app.add_middleware(RawResponseLogger)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
