"""OpenAIProvider — one generic brain for ANY OpenAI-compatible /chat/completions endpoint.

echelon had three near-duplicate OpenAI-shaped providers (grok, deepseek, local) because
there was no generic one. This is that generic one: parameterize base_url + default_model
+ key_env and it drives any OpenAI-compatible tier — NVIDIA NIM, Together, Fireworks,
OpenRouter, a local vLLM, LM Studio, etc. The existing named providers stay (they carry
tier-specific quirks: DeepSeek cache tokens, bridge socket, Grok reasoning); this covers
the long tail without a new file per endpoint.

Wire (identical to DeepSeekProvider): POST an OpenAI /chat/completions with Bearer auth,
`tools` schemas on the request, `choices[0].message.tool_calls` parsed into echelon's
normalized ToolCall. Native tool-calling REQUIRED for the loop driver role — a config
whose model can't tool-call is a text-transformer only.

Passthrough: any extra kwargs land in the request body (so provider-specific extras like
Nemotron's chat_template_kwargs / reasoning_budget work without a subclass). Key is
resolved env-first via echelon_sdk.keys._read_named_key(key_env).

Example (NVIDIA Nemotron):
    OpenAIProvider(
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="nvidia/nemotron-3-ultra-550b-a55b",
        key_env="NVIDIA_API_KEY",
        name="nemotron",
    )

Stdlib urllib (echelon's no-third-party-dep discipline).
"""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error
from typing import Any

from .base import ProviderBase, LLMResponse, ToolCall

# body keys we manage explicitly; everything else in `extra`/kwargs is passed through
_MANAGED = {"model", "messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens"}


class OpenAIProvider(ProviderBase):
    """Any OpenAI-compatible endpoint as a ProviderBase.

    base_url        e.g. "https://integrate.api.nvidia.com/v1" (no trailing /chat/completions)
    default_model   used when send() gets no model_id
    key_env         env var / key-file name holding the Bearer token (env wins)
    name            provider label for logs + cost meter
    extra           dict merged into every request body (provider-specific defaults)
    """

    def __init__(
        self,
        base_url: str,
        default_model: str,
        key_env: str = "OPENAI_API_KEY",
        name: str = "openai-compat",
        api_key: str | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._endpoint = self._base + "/chat/completions"
        self._default_model = default_model
        self._name = name
        self._extra = dict(extra or {})
        if api_key:
            self._key = api_key
        else:
            # env-first resolver, shared with the named loaders
            try:
                from echelon_sdk.keys import _read_named_key
                self._key = _read_named_key(key_env) or os.environ.get(key_env, "")
            except Exception:
                self._key = os.environ.get(key_env, "")
        if not self._key:
            raise RuntimeError(f"{name}: key not found (set {key_env} in env or ~/.echelon/.env)")

    @property
    def name(self) -> str:
        return self._name

    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # provider defaults first, but strip underscore-prefixed CONTROL keys (e.g. _timeout)
        # that are for us, not the API body
        body: dict[str, Any] = {k: v for k, v in self._extra.items() if not k.startswith("_")}
        body.update({
            "model": model_id or self._default_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0),
        })
        if "top_p" in kwargs:
            body["top_p"] = kwargs["top_p"]
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = kwargs.get("tool_choice", "auto")
        # pass through any other non-managed kwargs (e.g. chat_template_kwargs, reasoning_budget)
        for k, v in kwargs.items():
            if k not in _MANAGED and k not in ("timeout", "tool_choice"):
                body[k] = v

        req = urllib.request.Request(
            self._endpoint,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        # default from extra (so a config can set a longer floor for slow thinking models),
        # overridable per call
        timeout = kwargs.get("timeout", self._extra.get("_timeout", 120))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            return LLMResponse(content=f"HTTP {e.code}: {detail}", status="error",
                               model_id=model_id or self._default_model)
        except Exception as e:  # noqa: BLE001 — the loop must always get a response object
            return LLMResponse(content=f"{type(e).__name__}: {e}", status="error",
                               model_id=model_id or self._default_model)
        return self._parse(data, model_id or self._default_model)

    def _parse(self, data: dict[str, Any], model_id: str) -> LLMResponse:
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
                    args = {"_raw": args}
            tool_calls.append(ToolCall(name=fn.get("name", ""), args=args,
                                       id=tc.get("id", "")))
        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            tokens_cached=usage.get("prompt_cache_hit_tokens", 0) or 0,
            model_id=data.get("model", model_id),
            status="success",
            tool_calls=tool_calls,
            raw=data,
        )


def ox_provider(api_key: str | None = None) -> OpenAIProvider:
    """OX-ALPHA via OpenRouter — THE FREE GATE (owner ruling 2026-08-25).

    A stealth reasoning model built for coding + long-horizon agentic work: 1,048,576-token
    context, 131,072 completion tokens, tools/tool_choice + response_format, text+image+video
    in. Priced at ZERO on OpenRouter during the preview, so it is charged neither for prompt
    nor completion tokens.

    WHY THIS IS THE GATE and not a builder: CV-002 — a free gate is the safety organ. Every
    lane in this estate pays a subagent to gate what another subagent built, and a gate is the
    one job where a DIFFERENT model beats a cheaper copy of the same one (an independent
    context with different priors catches what a same-family reviewer rationalises). At zero
    cost we can afford to gate EVERYTHING, including the small lanes that ship ungated today.
    Proven on first contact 2026-08-25: handed the crm_snapshot upsert loop cold, with no hint
    of what to look for, it named the exact defect a paid gate had needed a live sqlite probe
    to prove — "uniform per-row stamping + MAX() watermark makes any partial write look
    complete".

    ⚠️ FREE TIER IS RATE-LIMITED — a second call moments after the first answered HTTP 429.
    Callers must back off and retry (measured: one ~20s wait cleared it). Treat 429 as normal
    operating weather, never as a failure, and never let a gate report PASS because its request
    was throttled — a throttled gate is an ABSTENTION, not a clearing.

    Reasoning chains across turns: pass `reasoning: {"enabled": true}` and hand the assistant
    message's `reasoning_details` back UNMODIFIED on the next call, and the model continues
    from where it stopped instead of restarting cold — the right shape for a multi-pass gate
    that re-derives a finding.
    """
    return OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        default_model="stealth/ox-alpha",
        key_env="OPENROUTER_API_KEY",
        name="ox",
        api_key=api_key,
        extra={"_timeout": 300},
    )


def minimax_provider(api_key: str | None = None) -> OpenAIProvider:
    """MINIMAX-M3 via OpenRouter — THE FREE *PIXEL* GATE (owner, 2026-08-25).

    The sibling of [ox_provider]. Ox is the free REASONING gate (code, logic, contracts);
    this is the free VISION gate. Priced at zero on the `:free` tier.

    WHY IT MATTERS HERE: the estate's UI gate law is that the FINAL gate for any UI work is
    PIXEL/VISUAL — you must actually look at the rendered screen, because a DOM/wire check is
    only a quick pre-check. That law has been expensive to honour (a paid vision worker per
    render) and so it has been skipped, which is exactly how a render gate once passed 43/43
    while its most sensitive popout was structurally broken. A free vision model makes the
    pixel gate affordable on EVERY UI lane instead of the ones we chose to pay for.

    Proven on first contact 2026-08-25, blind: handed a rendered mockup with ONE planted defect
    (a button running off the right canvas edge) and no hint of what to look for, it reported
    "Red rectangle: clipped — its right edge runs off the canvas" and correctly cleared the
    properly-padded one.

    Wire format is standard OpenAI multimodal: content is a LIST of parts,
    `{"type":"text",...}` plus `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`.
    A data: URL works, so a screenshot never has to be hosted.

    ⚠️ Same free-tier weather as ox: expect HTTP 429 and back off. A THROTTLED GATE IS AN
    ABSTENTION, NEVER A PASS — a UI lane must not go green because its pixel gate was
    rate-limited.
    """
    return OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        default_model="minimax/minimax-m3:free",
        key_env="OPENROUTER_API_KEY",
        name="minimax",
        api_key=api_key,
        extra={"_timeout": 300},
    )


def nemotron_provider(api_key: str | None = None) -> OpenAIProvider:
    """The NVIDIA Nemotron-3-ultra-550b config of OpenAIProvider (thinking on, capped
    reasoning budget so a step can't degenerate into an <unk> token loop)."""
    return OpenAIProvider(
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="nvidia/nemotron-3-ultra-550b-a55b",
        key_env="NVIDIA_API_KEY",
        name="nemotron",
        api_key=api_key,
        extra={"top_p": 0.95,
               # per-agent-step reasoning: enough to DECIDE a tool call, not write an essay.
               # 8192 blew past the 120s read timeout (the gater TimeoutError); 3072 keeps each
               # step responsive. A longer floor for the read timeout covers a heavy step.
               "chat_template_kwargs": {"enable_thinking": True},
               "reasoning_budget": 3072,
               "_timeout": 240},
    )


def openrouter_provider(api_key: str | None = None) -> OpenAIProvider:
    """Generic OpenRouter wire (R-0135) — any '<org>/<model>' id rides here when no dedicated
    factory matches (ox/minimax keep theirs). The free roster is WEATHER (atoms/roster.py
    refreshes daily; routing.model_chain drops retired ':free' seats while the snapshot is
    fresh). Same key, same ABSTAIN law: 429 = abstention, never a PASS."""
    return OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        default_model="openrouter/free",
        key_env="OPENROUTER_API_KEY",
        name="openrouter",
        api_key=api_key,
        extra={"_timeout": 300},
    )


def nim_provider(api_key: str | None = None) -> OpenAIProvider:
    """NVIDIA NIM wire, generic (owner provisioned NVIDIA_API_KEY 2026-09-01) — 83 free
    endpoints incl. deepseek-v4-flash/pro, kimi-k3, muse-glimmer, the nemotron family.
    Rate-limits are the only cost. NO nemotron-specific extras here: chat_template_kwargs /
    reasoning_budget 400 on non-nemotron models — nemotron_provider keeps its tuned config
    for nvidia/* ids; this factory serves everyone else on the wire."""
    return OpenAIProvider(
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="deepseek-ai/deepseek-v4-flash-0731",
        key_env="NVIDIA_API_KEY",
        name="nim",
        api_key=api_key,
        extra={"_timeout": 300},
    )
