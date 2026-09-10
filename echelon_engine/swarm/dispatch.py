"""dispatch — provider-agnostic LLM dispatch for swarm workers.

The swarm orchestrators (plan, council, skeptic) need to send prompts to LLMs.
This module provides a single `send()` function that routes to the right provider
based on the --provider flag, respecting the routing ladder in config.

Supports: deepseek, anthropic, gemini, auto (picks from config)
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

_AGENT_ROOT = str(Path(__file__).resolve().parent.parent.parent)


# ── TOKENOMICS ────────────────────────────────────────────────────────────────
# Every sender below returns a bare `str`, so tokens_in/tokens_out/cached from the
# LLMResponse were DROPPED ON THE FLOOR. `echelon run` prints a full tokenomics block;
# `echelon swarm` printed lenses + elapsed and nothing else — a verb that spends money and
# reports no spend cannot be budgeted, bakeoff-compared, or audited after the fact.
#
# This meter is a process-level accumulator, not a return-shape change: senders stay `-> str`
# so no caller breaks. Lenses run on threads, so it is lock-guarded.
class _Meter:
    """Process-wide swarm spend, aggregated across parallel lens workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.calls = 0
            self.failures = 0
            self.tokens_in = 0
            self.tokens_out = 0
            self.tokens_cached = 0
            self.reasoning_chars = 0
            self.by_model: dict[str, dict[str, int]] = {}

    def record(self, model: str, resp: object, *, failed: bool = False) -> None:
        t_in = int(getattr(resp, "tokens_in", 0) or 0)
        t_out = int(getattr(resp, "tokens_out", 0) or 0)
        t_cache = int(getattr(resp, "tokens_cached", 0) or 0)
        reasoning = len(getattr(resp, "reasoning_content", "") or "")
        with self._lock:
            self.calls += 1
            if failed:
                self.failures += 1
            self.tokens_in += t_in
            self.tokens_out += t_out
            self.tokens_cached += t_cache
            self.reasoning_chars += reasoning
            row = self.by_model.setdefault(
                model, {"calls": 0, "in": 0, "out": 0, "cached": 0})
            row["calls"] += 1
            row["in"] += t_in
            row["out"] += t_out
            row["cached"] += t_cache

    def usd(self) -> tuple[float | None, list[str]]:
        """(cost, unpriced_models) via the estate's own price table.

        cost.usd_of falls back to _DEFAULT_USD = (5.0, 15.0) for an unknown model — a
        CONSERVATIVE GUESS, not a measurement. Silently folding that into a total prints an
        estimate wearing the costume of a number, so the unpriced models come back with the
        figure and the caller marks it approximate."""
        try:
            from echelon_engine.atoms.providers.cost import usd_of, USD_PER_M
        except Exception:
            return None, []
        total = 0.0
        unpriced: list[str] = []
        for model, r in self.by_model.items():
            if model not in USD_PER_M:
                unpriced.append(model)
            try:
                total += float(usd_of(model, r["in"], r["out"], r["cached"]))
            except Exception:
                return None, unpriced
        return total, unpriced

    def report(self) -> str:
        with self._lock:
            if not self.calls:
                return "   tokenomics: no LLM calls recorded"
            lines = [
                f"   tokenomics: {self.calls} call(s)"
                + (f", {self.failures} failed" if self.failures else "")
                + f" · in {self.tokens_in:,} (cached {self.tokens_cached:,})"
                f" · out {self.tokens_out:,}"
            ]
            usd, unpriced = self.usd()
            if usd is None:
                lines[0] += " · $UNKNOWN (price table unavailable)"
            elif unpriced:
                lines[0] += f" · ~${usd:.4f} (ESTIMATE — unpriced: {', '.join(sorted(unpriced))})"
            else:
                lines[0] += f" · ${usd:.4f}"
            if self.reasoning_chars:
                lines.append(f"   thinking:   {self.reasoning_chars:,} chars of CoT returned")
            if len(self.by_model) > 1:
                for model, r in sorted(self.by_model.items()):
                    lines.append(f"     {model:<26} {r['calls']:>3} call(s) "
                                 f"in {r['in']:>8,}  out {r['out']:>7,}")
            return "\n".join(lines)


METER = _Meter()


def send(prompt: str, provider: str = "auto", model: str = "",
         timeout: int = 300, temperature: float = 0.3,
         *, effort: str = "", response_format: dict | None = None) -> str:
    """Send a prompt to an LLM provider. Returns the response text.

    Args:
        prompt: The full prompt to send
        provider: 'auto' (config default), 'deepseek', 'anthropic', 'gemini'
        model: Model override (empty = provider default)
        timeout: Seconds before giving up
        temperature: 0.0-1.0
        effort: Reasoning effort — '' (provider default), 'low', 'high', 'max',
            or 'none' to disable thinking. Keyword-only and defaulted, so every
            existing call site is unaffected. Honoured by DeepSeek only; other
            providers ignore it rather than fail (see _send_anthropic/_send_gemini).

    EFFORT IS NOT MONOTONIC IN COST — measured 2026-08-17 on a 28-item sealed set:
    flash at `high` cost $0.0053 while flash at `low` cost $0.0110, because low
    effort emitted 8,166 output tokens against high's 3,100 (output bills ~3x input).
    Under-thinking rambles. Do not assume 'low' is the cheap setting; measure.

    Returns:
        Response text, or empty string on failure
    """
    # ── Resolve provider ──────────────────────────────────────────────────
    provider_name = _resolve_provider(provider)
    model_id = model or _default_model(provider_name)

    # ── DeepSeek (OpenAI-compatible) ──────────────────────────────────────
    if provider_name == "deepseek":
        return _send_deepseek(prompt, model_id, timeout, temperature, effort,
                              response_format)

    # ── Anthropic (Messages API) ──────────────────────────────────────────
    if provider_name == "anthropic":
        return _send_anthropic(prompt, model_id, timeout, temperature)

    # ── Gemini (Vertex AI) ────────────────────────────────────────────────
    if provider_name == "gemini":
        return _send_gemini(prompt, model_id, timeout, temperature)

    # ── Fallback: try each available provider in order ────────────────────
    for fallback in ["deepseek", "anthropic"]:
        try:
            return send(prompt, fallback, model, timeout, temperature,
                        effort=effort, response_format=response_format)
        except Exception:
            continue

    return ""


# ── Provider resolvers ────────────────────────────────────────────────────────

def _resolve_provider(provider: str) -> str:
    """Resolve 'auto' to the configured default provider."""
    if provider and provider != "auto":
        return provider
    try:
        from echelon_sdk.config import get
        return get("brain") or "deepseek"
    except Exception:
        return "deepseek"


def _default_model(provider: str) -> str:
    """Default model per provider."""
    defaults = {
        "deepseek": "deepseek-chat",
        "anthropic": "claude-sonnet-4-6",
        "gemini": "gemini-2.5-flash",
    }
    return defaults.get(provider, "deepseek-chat")


# ── Provider-specific senders ─────────────────────────────────────────────────

def _send_deepseek(prompt: str, model: str, timeout: int,
                   temperature: float, effort: str = "",
                   response_format: dict | None = None) -> str:
    """Send via DeepSeek's OpenAI-compatible API.

    `effort` is passed through to the provider, which maps it to the vendor's
    {"thinking": …, "reasoning_effort": …} fields (medium/xhigh fold to high).
    An empty string is OMITTED entirely so the provider default still applies —
    passing effort="" explicitly would be a different request than not passing it.
    """
    try:
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
        prov = DeepSeekProvider()
        msgs = [{"role": "user", "content": prompt}]
        extra = {"effort": effort} if effort else {}
        if response_format:
            extra["response_format"] = response_format
        resp = prov.send(msgs, model_id=model, temperature=temperature,
                         timeout=timeout, **extra)
        METER.record(model, resp, failed=getattr(resp, "status", "") != "success")
        return getattr(resp, "content", "") or ""
    except Exception as e:
        print(f"   [deepseek] {e}", file=sys.stderr)
        METER.record(model, None, failed=True)
        return ""


def _send_anthropic(prompt: str, model: str, timeout: int,
                    temperature: float) -> str:
    """Send via Anthropic Messages API."""
    try:
        from echelon_engine.atoms.providers.anthropic_upstream import \
            AnthropicUpstreamProvider
        import os
        upstream = os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not api_key:
            from echelon_sdk.keys import load_anthropic_key
            api_key = load_anthropic_key()

        prov = AnthropicUpstreamProvider(upstream_url=upstream, api_key=api_key)
        payload = {
            "model": model,
            "max_tokens": 4096,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = prov.send_anthropic(payload)
        result = AnthropicUpstreamProvider.anthropic_response(resp, model)
        # Extract text from content blocks
        text = ""
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        # Anthropic returns a raw dict, not an LLMResponse — its usage block is shaped
        # {input_tokens, output_tokens, cache_read_input_tokens}. Adapt to the meter's
        # duck-typed shape rather than teach the meter a second schema.
        _u = result.get("usage") or {}

        class _Usage:
            tokens_in = int(_u.get("input_tokens", 0) or 0)
            tokens_out = int(_u.get("output_tokens", 0) or 0)
            tokens_cached = int(_u.get("cache_read_input_tokens", 0) or 0)
            reasoning_content = ""
        METER.record(model, _Usage(), failed=not text)
        return text or str(result)
    except Exception as e:
        print(f"   [anthropic] {e}", file=sys.stderr)
        METER.record(model, None, failed=True)
        return ""


def _send_gemini(prompt: str, model: str, timeout: int,
                 temperature: float) -> str:
    """Send via Gemini Vertex AI."""
    try:
        import os
        from echelon_engine.atoms.providers.gemini import GeminiProvider
        prov = GeminiProvider()
        msgs = [{"role": "user", "content": prompt}]
        # HEADROOM (owner 2026-07-10): with GEMINI_THINKING_BUDGET on (dynamic/N), thinking spends
        # from the SAME output budget BEFORE visible text — the default 8192 got EATEN and truncated
        # long swarm/blueprint answers (a chair report + a big card-array JSON fence) mid-stream, so
        # _extract_json saw no closing fence → 0 cards. Give thinking-on gem calls real room. When
        # thinking is OFF (budget 0/unset), 8192 stays honest as before.
        _thinking_on = os.environ.get("GEMINI_THINKING_BUDGET", "").strip().lower() not in ("", "0")
        max_tokens = 32768 if _thinking_on else 8192
        resp = prov.send(msgs, model_id=model, temperature=temperature,
                         timeout=timeout, max_tokens=max_tokens)
        METER.record(model, resp, failed=getattr(resp, "status", "") != "success")
        return getattr(resp, "content", "") or ""
    except Exception as e:
        print(f"   [gemini] {e}", file=sys.stderr)
        METER.record(model, None, failed=True)
        return ""
