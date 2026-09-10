"""LocalProvider — a $0 local brain served by a remote LM Studio (OpenAI-shaped).

base.py named this exact seam: "Every brain — Grok, the Copilot bridge, a local LM Studio
model — implements this one shape." LM Studio exposes an OpenAI-compatible
/v1/chat/completions, so this is a near drop-in of DeepSeekProvider — same wire (Bearer auth,
`tools` schemas on the request, `choices[0].message.tool_calls` parsed back into our normalized
ToolCall list), three differences:

  1. ENDPOINT — a remote LM Studio (default the configured floor host/v1), not the DeepSeek cloud.
  2. COST — $0. The model runs on the owner's own hardware; cost.py prices it at zero, so the
     T2/T3 floor is genuinely free local compute, not cheap-paid API (compute-tiering-strategy:
     reach the cheapest tier that can do the work — local 3B is the true floor).
  3. JIT-LOAD RETRY — LM Studio loads a model ON FIRST HIT and returns {"error":"Model is
     unloaded."} (HTTP 400, a BARE error string, NOT OpenAI's error envelope) on the very first
     request while it loads. Verified live 2026-06-08: a single retry after a beat succeeds. So
     send() retries ONCE on the unloaded signal before surfacing an error — a swarm step must not
     die because the model was cold. The server holds ~one model resident, so a tier-switch can
     trigger a load; the retry absorbs it.

Stdlib only (urllib) — matches the estate's no-third-party-dep discipline.
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from typing import Any

from .base import ProviderBase, LLMResponse, ToolCall
from echelon_sdk.keys import load_lmstudio_key
from echelon_sdk.paths import log_file
from echelon_sdk.config import floor_endpoint

# DIRECT LAN endpoint (the same LM Studio server, behind no Cloudflare). The Cloudflare-fronted
# domain (the configured floor host) RATE-LIMITS a concurrent burst -> HTTP 500/524 (proven: 11/12 swarm
# requests died via Cloudflare; 8/8 returned 200 in ~2.8s DIRECT). Direct = no CF choke, no TLS, no
# UA-header workaround needed, lower latency. Override with LM_ENDPOINT env or the endpoint= arg.
_ENDPOINT = floor_endpoint("/v1/chat/completions")  # config 'floor.host' / LM_ENDPOINT env (one home)
# Optional remote fallback for the local floor, e.g. an LM Studio published through a
# tunnel. Empty by default: with no ECHELON_REMOTE_LLM set there is no remote fallback.
_ENDPOINT_CLOUDFLARE = os.environ.get("ECHELON_REMOTE_LLM", "")  # single calls only
# The bare-string error LM Studio returns while a model is loading on demand.
_UNLOADED_MARKER = "is unloaded"
# LM Studio's harmony/PEG parser rejecting one gpt-oss decode (server-side 500 wrapped in a 400).
_PEG_MARKER = "does not match the expected"

# THE AGENT-GRADE SYSTEM PROMPT (proven 2026-09-03, MoE bake-off A/B, atom
# an-agent-grade-system-prompt-is-the-difference-between-a-local-seat-that-heals-and-one-that-stalls):
# the SAME local model on the SAME 2-turn venv-failure probe FAILS bare and PASSES with this
# ~350-token distillation of a fresh harness seat. Minimal-prompt passes were FLAKY across the
# whole 7-model bake-off; agent-prompt passes were CONSISTENT — so injection is wiring, not garnish.
# Injected by send() only when the caller supplies no system message of its own.
AGENT_SYSTEM = (
    "You are an autonomous coding agent in a terminal harness. The user is NOT watching and "
    "cannot answer questions; asking them anything is a task failure. Everything you need is "
    "discoverable from the environment with commands.\n\n"
    "OPERATING LAWS:\n"
    "1. ERROR RECOVERY: a failed command is information, not a dead end. Read the error text, "
    "form a hypothesis, try a different door. 'not recognized' means the executable is not on "
    "PATH: try the module form (`python -m <package>`), an absolute path, or locate it "
    "(`where <name>`, `dir /s /b <name>*`). Only report failure after 3 distinct attempts.\n"
    "2. ENVIRONMENT: Windows 11. Python projects often run inside a venv; a bare console "
    "script missing from PATH usually works as `python -X utf8 -m <module>` from the project "
    "root, or via the venv's Scripts directory.\n"
    "3. NEVER ask where something is installed, never ask permission, never explain what you "
    "would do — do it.\n"
    "4. Follow the project's declared rituals (banners, CLAUDE.md laws) exactly; they outrank "
    "your habits."
)


class LocalProvider(ProviderBase):
    def __init__(self, api_key: str | None = None, endpoint: str | None = None,
                 *, load_retries: int = 1, load_wait_s: float = 6.0, peg_retries: int = 2):
        # _ENDPOINT already folds LM_ENDPOINT env via floor_endpoint(); endpoint= arg still wins.
        self._endpoint = endpoint or _ENDPOINT
        self._key = api_key or load_lmstudio_key()
        self._load_retries = load_retries   # extra attempts on a "model is unloaded" cold start
        self._load_wait_s = load_wait_s     # pause between the cold-error and the retry (JIT load)
        self._peg_retries = peg_retries     # re-decodes absorbed on LM Studio's PEG/harmony 400 (OPEN-0071)
        self._peg_hits = 0                  # how many such errors this provider swallowed (observability)

    # LM Studio's OpenAI endpoint streams SSE, so the reflex-gate can sever the decode mid-stream
    # (true mid-stream) rather than the prefill-pair fallback. See ProviderBase.send_reflex_gated.
    supports_stream = True

    @property
    def name(self) -> str:
        return "local"

    def send_stream_until(
        self, messages: list[dict[str, Any]], model_id: str = "smollm3-3b-gabliterated-i1",
        stop: list[str] | None = None, **kwargs: Any,
    ) -> LLMResponse:
        """TRUE mid-stream cut: stream tokens, stop the instant any `stop` marker appears in the
        accumulated text, return what came BEFORE it. Used by the reflex-gate to sever at </reason>.
        SSE lines are `data: {json}` with a trailing `data: [DONE]`. Falls through to a plain stopped
        send() if the server rejects streaming (so the gate still works)."""
        stop = stop or []
        body: dict[str, Any] = {
            "model": model_id, "messages": messages,
            "temperature": kwargs.get("temperature", 0), "stream": True,
        }
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        if stop:
            body["stop"] = stop  # belt-and-suspenders: server stops too; we also cut client-side
        req = urllib.request.Request(
            self._endpoint, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) echelon-agent/1.0"},
            method="POST",
        )
        timeout = kwargs.get("timeout", 120)
        acc: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0].get("delta", {}).get("content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    acc.append(delta)
                    text = "".join(acc)
                    if any(m in text for m in stop):  # CUT: a stop marker just closed
                        break
        except urllib.error.HTTPError:
            # server may not accept stream on this build — fall back to the stopped non-stream call
            kw = dict(kwargs); kw["stop"] = list(kwargs.get("stop", []) or []) + list(stop)
            return self.send(messages, model_id, **kw)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(content=f"{type(e).__name__}: {e}", status="error", model_id=model_id)
        return LLMResponse(content="".join(acc), status="success", model_id=model_id)

    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str = "smollm3-3b-gabliterated-i1",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": AGENT_SYSTEM}, *messages]
        body: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0),
        }
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = kwargs.get("tool_choice", "auto")

        timeout = kwargs.get("timeout", 120)
        # JIT-load retry: a cold model 400s with "Model is unloaded." on the first hit; retry once.
        attempts = self._load_retries + 1
        last: LLMResponse | None = None
        for i in range(attempts):
            last = self._send_once(body, model_id, timeout)
            if last.status != "error" or _UNLOADED_MARKER not in (last.content or "").lower():
                break
            if i < attempts - 1:
                time.sleep(self._load_wait_s)   # give LM Studio a beat to finish loading the model
        # OPEN-0071 (2026-09-03, first .des build): gpt-oss-20b mid-loop answers 'HTTP 400: Engine
        # protocol predict stream returned an error: ... does not match the expected peg-native
        # format' — that is LM STUDIO's own harmony/PEG parser rejecting one malformed decode, not
        # a shape our engine failed to parse (10/10 clean at temperature 0 single-turn; the 7
        # errors in desdemo's ledger were all mid-loop with tool history). The decode is
        # non-deterministic under sampling, so a re-decode usually lands; absorb it HERE ($0,
        # seconds) instead of surfacing an error the dispatch loop pays a whole attempt to reroll.
        # Second retry nudges temperature so a deterministic decode cannot repeat the same output.
        for j in range(self._peg_retries):
            if last.status != "error" or _PEG_MARKER not in (last.content or "").lower():
                break
            self._peg_hits += 1
            if j == self._peg_retries - 1:
                body = {**body, "temperature": max(float(body.get("temperature", 0) or 0), 0.3)}
            last = self._send_once(body, model_id, timeout)
        return last  # type: ignore[return-value]

    def _send_once(self, body: dict[str, Any], model_id: str, timeout: float) -> LLMResponse:
        req = urllib.request.Request(
            self._endpoint,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                # the configured floor host sits behind Cloudflare, which 403s (error 1010, "banned by
                # browser signature") a request with urllib's default User-Agent. curl gets through
                # because it sends its own UA; we must too. A browser-shaped UA clears the block
                # (verified live 2026-06-08). Without this every local call 403s and silently falls
                # back to the paid DeepSeek floor — the $0 tier would never actually be reached.
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) echelon-agent/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            # LM Studio returns {"error":"Model is unloaded."} (bare string) or
            # {"error":{"message":...}} (OpenAI shape). Surface the message either way so the
            # _UNLOADED_MARKER check in send() can see it and trigger the JIT-load retry.
            msg = detail
            try:
                err = json.loads(detail).get("error")
                msg = err if isinstance(err, str) else (err or {}).get("message", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            return LLMResponse(content=f"HTTP {e.code}: {msg}", status="error", model_id=model_id)
        except Exception as e:  # noqa: BLE001 — surface any transport failure as a status
            return LLMResponse(content=f"{type(e).__name__}: {e}", status="error", model_id=model_id)

        return self._parse(data, model_id)

    def _parse(self, data: dict[str, Any], model_id: str) -> LLMResponse:
        # A 200 can still carry a bare {"error": ...} from LM Studio — treat it as an error status.
        if "choices" not in data and "error" in data:
            err = data["error"]
            msg = err if isinstance(err, str) else (err or {}).get("message", json.dumps(err))
            return LLMResponse(content=str(msg), status="error", model_id=model_id, raw=data)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            return LLMResponse(content=f"unexpected response: {json.dumps(data)[:300]}",
                               status="error", model_id=model_id, raw=data)

        content = msg.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    with open(log_file(), "a") as lf:
                        lf.write(f"local {fn.get('name', '?')} raw: {args}\n")
                    args = {"_raw": args}
            tool_calls.append(ToolCall(name=fn.get("name", ""), args=args, id=tc.get("id", "")))

        usage = data.get("usage", {})
        # No cache-hit accounting for local — it's $0 either way (cost.py prices these at zero).
        return LLMResponse(
            content=content,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            model_id=data.get("model", model_id),
            status="success",
            tool_calls=tool_calls,
            raw=data,
        )
