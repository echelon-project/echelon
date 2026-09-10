"""DeepSeekProvider — the cheap-strong agentic brain (DeepSeek, OpenAI-shaped).

Owner, 2026-06-05: use DeepSeek as the brain for the agentic loop. DeepSeek's API is
OpenAI-compatible, so this is a near drop-in of GrokProvider — same wire (POST to an
OpenAI-style /chat/completions with Bearer auth, `tools` schemas on the request,
`choices[0].message.tool_calls` parsed back into our normalized ToolCall list), different
endpoint + key + default model.

Models (2026):
  - deepseek-chat      — V3 line, fast, native function-calling. The LOOP DRIVER default.
  - deepseek-reasoner  — R1 line, thinking. Splits CoT into `reasoning_content`, so
                         `message.content` stays clean (no </think> leak to strip) — same
                         shape as grok-4.3. NOTE: deepseek-reasoner historically did NOT
                         support tool_calls; if a run needs tools, drive with deepseek-chat.

Pricing is cheap (a big reason to use it as the driver) — see cost.py USD_PER_M. This eases
the finite xAI floor: the dominant loop-driver spend moves to the cheapest capable tier
(compute-tiering-strategy: reach the cheapest tier that can actually do the work).

Stdlib only (urllib) — matches the estate's no-third-party-dep discipline.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from typing import Any

from .base import ProviderBase, LLMResponse, ToolCall
from echelon_sdk.keys import load_deepseek_key
from echelon_sdk.paths import log_file

_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"

# Vendor base URLs. The Anthropic-format door is what claude-deep's proxy fronts.
BASE_URL_OPENAI = "https://api.deepseek.com"
BASE_URL_ANTHROPIC = "https://api.deepseek.com/anthropic"

# ── MODEL CATALOG (vendor Model Details, read 2026-08-17) ─────────────────────────────────
# This provider is the LIBRARY for DeepSeek facts — limits, features, concurrency — so callers
# ask the provider instead of hardcoding a guess. Pricing lives in providers/cost.py (one rate
# table, peak/off-peak aware); everything else that is model-shaped lives here.
MODELS: dict[str, dict[str, Any]] = {
    "deepseek-v4-flash": {
        "version": "DeepSeek-V4-Flash-0731",
        "context": 1_000_000,
        "max_output": 384_000,
        "thinking": True,          # both modes; thinking is the DEFAULT
        "json_output": True,
        "tool_calls": True,
        "responses_api": True,
        "anthropic_api": True,
        "prefix_completion": True,  # beta
        "fim_completion": "non-thinking-only",
        "concurrency": 2500,
    },
    "deepseek-v4-pro": {
        "version": "DeepSeek-V4-Pro-0813",
        "context": 1_000_000,
        "max_output": 384_000,
        "thinking": True,
        "json_output": True,
        "tool_calls": True,
        "responses_api": True,
        "anthropic_api": True,
        "prefix_completion": True,
        "fim_completion": "non-thinking-only",
        "concurrency": 500,
    },
}

# The API resolves these retired ids to a live model server-side — a `deepseek-chat` request
# comes back stamped `deepseek-v4-flash`. Resolve them HERE so limit/pricing lookups land on
# what actually ran, not on a retired row.
ALIASES = {"deepseek-chat": "deepseek-v4-flash", "deepseek-reasoner": "deepseek-v4-flash"}


def resolve_model(model_id: str) -> str:
    """Canonical model id — follows the vendor's server-side aliasing of retired ids."""
    return ALIASES.get((model_id or "").strip().lower(), model_id)


def model_info(model_id: str) -> dict[str, Any]:
    """Limits + feature flags for a model. `{}` for an unknown id (never a fabricated row)."""
    return MODELS.get(resolve_model(model_id), {})


def supports(model_id: str, feature: str) -> bool:
    """Does this model support `feature` (thinking / tool_calls / json_output / …)?"""
    return bool(model_info(model_id).get(feature))


def max_output(model_id: str) -> int:
    """Vendor MAX OUTPUT for the model, or 0 when unknown — callers must not guess a ceiling."""
    return int(model_info(model_id).get("max_output", 0))


def concurrency_limit(model_id: str) -> int:
    """Account-level concurrent-request cap. Exceeding it returns HTTP 429, and the limit is
    per-ACCOUNT across every API key — so a fan-out sizer must read this, not the key count."""
    return int(model_info(model_id).get("concurrency", 0))

# ── THINKING MODE (DeepSeek V4) ───────────────────────────────────────────────────────────
# The V4 line reasons before answering. Two knobs, both OpenAI-format on this wire:
#   {"thinking": {"type": "enabled"|"disabled"}}   — toggle (DEFAULT: enabled, effort high)
#   {"reasoning_effort": "low"|"high"|"max"}       — effort
# The vendor maps requested -> actual as below (identical for v4-flash and v4-pro). We ship the
# map rather than pass the raw string so a caller asking for "medium"/"xhigh" gets the tier it
# actually lands on instead of silently rounding.
_EFFORT_MAP = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "none": None,       # `none` DISABLES thinking (Responses-API semantics)
    "disabled": None,
    "off": None,
}

# Thinking mode IGNORES these — the vendor accepts them for compatibility and silently drops
# them. We omit them entirely so a run's body reflects what the model will actually honor.
_THINKING_IGNORED = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


_USER_ID_OK = __import__("re").compile(r"^[a-zA-Z0-9\-_]+$")


def _user_id_field(kwargs: dict[str, Any]) -> dict[str, Any]:
    """`user_id` — the vendor's per-user isolation handle (content-safety, KVCache, and
    scheduling isolation, plus per-user concurrency accounting).

    Vendor contract: `[a-zA-Z0-9\\-_]+`, max 512 chars, and MUST NOT carry user privacy data.
    An invalid id is DROPPED rather than sent, because a rejected request would cost the whole
    call — and silently mangling it (the mcp-users `rstrip(')')` trap) is worse than omitting.
    """
    uid = kwargs.get("user_id")
    if not uid:
        return {}
    uid = str(uid)
    if len(uid) > 512 or not _USER_ID_OK.match(uid):
        return {}
    return {"user_id": uid}


def _decode_keepalive(raw: bytes) -> dict[str, Any]:
    """Parse a response body that may carry KEEP-ALIVE padding.

    Vendor: while a request waits for inference the server holds the connection open and emits
    blank lines (non-streaming) or `: keep-alive` SSE comments (streaming). They are not part
    of the JSON, but a naive `json.loads` on the raw body can choke on them — so strip the
    padding before parsing. (The server closes the connection if inference has not started
    within 10 minutes, which is why a >600s timeout buys nothing.)
    """
    text = raw.decode(errors="replace")
    stripped = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith(": keep-alive")
    ).strip()
    return json.loads(stripped or text)


def _replay_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce the THINKING-MODE REPLAY CONTRACT on the outgoing message list.

    DeepSeek's rule, and it is a HARD 400 not a warning:
      * an assistant turn that made TOOL CALLS must carry its `reasoning_content` back on
        EVERY subsequent request — drop it and the API rejects the whole call;
      * an assistant turn with NO tool call may keep or drop it; the API ignores it.

    So the only unsafe move is stripping reasoning from a tool-calling turn. This pass leaves
    messages untouched except to DROP `reasoning_content` where it is inert (no tool_calls),
    which keeps the replayed context smaller without ever violating the contract.

    Non-dict entries (SDK message objects) pass through untouched — they already carry the
    field and know how to serialize themselves.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") != "assistant" or "reasoning_content" not in m:
            out.append(m)
            continue
        if m.get("tool_calls"):
            out.append(m)                      # MUST replay — leave exactly as-is
        else:
            out.append({k: v for k, v in m.items() if k != "reasoning_content"})
    return out


def _clamp_output(model_id: str, want: int) -> int:
    """Hold max_tokens under the model's real ceiling. V4 allows 384K output — far above the
    8192 most callers assume — so this mostly RAISES what is possible while stopping an
    over-ask from being rejected outright."""
    cap = max_output(model_id)
    return min(int(want), cap) if cap else int(want)


def _thinking_fields(model_id: str, kwargs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Build the thinking/effort request fields. Returns (fields, thinking_on).

    Only the V4 line supports this; older `deepseek-chat`/`deepseek-reasoner` ids are left
    untouched so this cannot break an existing pinned run.
    """
    # OWNER DECISION 2026-08-17: gate on the RAW id, not the resolved alias. `deepseek-chat`
    # IS v4-flash server-side and could take thinking, but flipping it here would turn thinking
    # on for every legacy caller (swarm, judge tier, summariser) in one commit — changing cost
    # and latency estate-wide with no measured baseline. Opt in per call-site by naming an
    # explicit `deepseek-v4-*` id. Aliases stay exactly as they run today.
    if "v4" not in (model_id or "").lower():
        return {}, False

    raw = kwargs.get("reasoning_effort", kwargs.get("effort"))
    if raw is None:
        # OWNER RULING 2026-08-25: an unspecified effort defaults to LOW, not the vendor's
        # high. The caller that does not name an effort is, by definition, not asking for a
        # deep think — and the estate's highest-volume thinking caller is the recall JUDGE,
        # which answers "does this seed mean what this intent means". That is a cheap
        # recognition judgement; high effort buys nothing and costs on every recall.
        # Callers wanting depth pass reasoning_effort explicitly (low/high/max pass through;
        # medium/xhigh map to high per the vendor table in _EFFORT_MAP).
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, True

    key = str(raw).strip().lower()
    mapped = _EFFORT_MAP.get(key, "high")
    if mapped is None:
        return {"thinking": {"type": "disabled"}}, False
    return {"thinking": {"type": "enabled"}, "reasoning_effort": mapped}, True


class DeepSeekProvider(ProviderBase):
    # ── shared USD meter (class-level, mirroring GrokProvider). A recurse/board/autoloop run
    # makes many providers across a process; spend must AGGREGATE, not reset per instance — else
    # a fresh DeepSeekProvider() in _default_spend_reader reads $0 while a different instance did
    # the spending (the recurloop live finding 2026-06-18, applied to DeepSeek now that it is the
    # standalone-loop driver). One class meter = the budget brake sees the real total. ──
    from .cost import Budget as _Budget
    _meter = _Budget(total=float("inf"))   # cap lives in the loop; meter just counts

    def __init__(self, api_key: str | None = None, endpoint: str = _ENDPOINT):
        self._key = api_key or load_deepseek_key()
        self._endpoint = endpoint

    @classmethod
    def total_spent(cls) -> float:
        """Cumulative USD this process has drawn through any DeepSeekProvider. The loop's
        _default_spend_reader reads this to enforce the budget cap — so the brake is real."""
        return float(cls._meter.spent)

    def _charge(self, resp: "LLMResponse") -> None:
        """Charge the shared driver meter on a real, successful draw (cache-aware)."""
        if resp.status == "success":
            try:
                self._meter.charge_driver(resp.model_id, resp.tokens_in, resp.tokens_out,
                                          cached_tokens=getattr(resp, "tokens_cached", 0) or 0)
            except Exception:
                pass

    @property
    def name(self) -> str:
        return "deepseek"

    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str = "deepseek-chat",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        think, thinking_on = _thinking_fields(model_id, kwargs)
        body: dict[str, Any] = {
            "model": model_id,
            "messages": _replay_reasoning(messages),
        }
        body.update(think)
        body.update(_user_id_field(kwargs))
        # Thinking mode silently ignores temperature/top_p/penalties — omit rather than send a
        # field the model will drop, so the body is an honest record of the request.
        if not thinking_on:
            body["temperature"] = kwargs.get("temperature", 0)
        if "max_tokens" in kwargs:
            body["max_tokens"] = _clamp_output(model_id, kwargs["max_tokens"])
        # JSON MODE — ask for a JSON document instead of scraping one out of prose. Only
        # sent when the model advertises json_output, so an unsupporting model is never
        # handed a field it would reject. Applied in BOTH send() and asend(): a body field
        # present in one path and absent in the other is how the two silently diverge.
        rf = kwargs.get("response_format")
        if rf and supports(model_id, "json_output"):
            body["response_format"] = rf if isinstance(rf, dict) else {"type": str(rf)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = kwargs.get("tool_choice", "auto")

        req = urllib.request.Request(
            self._endpoint,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = kwargs.get("timeout", 90)  # hard-ish cap so a stalled call can't freeze a worker
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = _decode_keepalive(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 429:
                # Account-level CONCURRENCY cap, not a token/rate quota: v4-pro allows 500
                # in-flight requests, v4-flash 2500, counted per ACCOUNT across all keys.
                # Say so, because "429" alone sends people hunting for a spend limit.
                cap = concurrency_limit(model_id)
                detail = (f"concurrency cap hit (limit {cap} in-flight for "
                          f"{resolve_model(model_id)}, account-wide): {detail}")
            return LLMResponse(content=f"HTTP {e.code}: {detail}", status="error", model_id=model_id)
        except Exception as e:  # noqa: BLE001 — surface any transport failure as a status
            return LLMResponse(content=f"{type(e).__name__}: {e}", status="error", model_id=model_id)

        resp = self._parse(data, model_id)
        self._charge(resp)
        return resp

    async def send_async(
        self,
        messages: list[dict[str, Any]],
        model_id: str = "deepseek-chat",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """ASYNC twin of send() — a non-blocking POST so N calls are TRULY concurrent under one
        event loop (not GIL-serialized thread wrappers over blocking urllib). Async is inherited
        from the entry call: an async caller awaits this; many awaits run at once. Same body/parse
        contract as send()."""
        import httpx
        think, thinking_on = _thinking_fields(model_id, kwargs)
        body: dict[str, Any] = {
            "model": model_id,
            "messages": _replay_reasoning(messages),
        }
        body.update(think)
        body.update(_user_id_field(kwargs))
        if not thinking_on:
            body["temperature"] = kwargs.get("temperature", 0)
        if "max_tokens" in kwargs:
            body["max_tokens"] = _clamp_output(model_id, kwargs["max_tokens"])
        # JSON MODE — ask for a JSON document instead of scraping one out of prose. Only
        # sent when the model advertises json_output, so an unsupporting model is never
        # handed a field it would reject. Applied in BOTH send() and asend(): a body field
        # present in one path and absent in the other is how the two silently diverge.
        rf = kwargs.get("response_format")
        if rf and supports(model_id, "json_output"):
            body["response_format"] = rf if isinstance(rf, dict) else {"type": str(rf)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = kwargs.get("tool_choice", "auto")
        timeout = kwargs.get("timeout", 120)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(self._endpoint, json=body, headers={
                    "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
            if r.status_code >= 400:
                return LLMResponse(content=f"HTTP {r.status_code}: {r.text[:300]}",
                                   status="error", model_id=model_id)
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return LLMResponse(content=f"{type(e).__name__}: {e}", status="error", model_id=model_id)
        resp = self._parse(data, model_id)
        self._charge(resp)
        return resp

    def _parse(self, data: dict[str, Any], model_id: str) -> LLMResponse:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            return LLMResponse(content=f"unexpected response: {json.dumps(data)[:300]}",
                               status="error", model_id=model_id, raw=data)

        content = msg.get("content") or ""
        # THINKING MODE: the CoT arrives beside `content`, never inside it — so there is no
        # </think> tag to strip and `content` is already the clean final answer.
        reasoning = msg.get("reasoning_content") or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    with open(log_file(), "a") as lf:
                        lf.write(f"deepseek {fn.get('name', '?')} raw: {args}\n")
                    args = {"_raw": args}
            tool_calls.append(ToolCall(name=fn.get("name", ""), args=args, id=tc.get("id", "")))

        usage = data.get("usage", {})
        # DeepSeek splits prompt_tokens into a CACHE HIT count (billed ~10x cheaper) + a miss count.
        # The cache is automatic; a growing transcript with a stable prefix hits ~all of it. We carry
        # the hit count so the meter bills hits at the cheap rate instead of all-input-at-miss (the
        # ~5x over-report that halted runs early). See providers/cost.py usd_of cache-aware path.
        return LLMResponse(
            content=content,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            tokens_cached=usage.get("prompt_cache_hit_tokens", 0),
            model_id=data.get("model", model_id),
            status="success",
            tool_calls=tool_calls,
            raw=data,
            reasoning_content=reasoning,
        )
