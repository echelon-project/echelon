"""floor_chat — the raw OpenAI-shaped chat primitive (WELD #1, cut from add_steering).

In echelon-agent the `_chat` call + the T1/T2 model ids lived inside `add_steering.py`
(a 1436-line god-module that ALSO holds the IntentPool/compiler/review-gate orchestration).
`memory/warmth_update.py` imported `_chat` + `T1_ARCHITECT` from it at top level, which
dragged the WHOLE T2/T3 ADD stack into the memory layer — the weld that blocked migrating
warmth_update (and, downstream, atom_kinds).

THE CUT: `_chat` is a pure transport — one /chat/completions call routed by model id to the
local floor, a cloud tier (deepseek), or the burst-T1 GPU over the eos channel. It depends on
stdlib + echelon_sdk.keys ONLY. So it belongs in the providers atom-package as a primitive;
warmth_update (and atom_kinds) now import it from here, and the orchestration half (Intent/
compiler/Status/review-card composition) stays behind in the agent layer where it belongs.

Layer: an atoms/providers module — stdlib + echelon_sdk only (scanner-enforced).
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error

from echelon_sdk.keys import load_lmstudio_key
from echelon_sdk.config import floor_endpoint

# --- Endpoint + the proven local-floor model ids -----------------------------
ENDPOINT = floor_endpoint("/v1/chat/completions")  # config 'floor.host' / LM_ENDPOINT env, not a hardcoded LAN IP
T1_ARCHITECT = "gemma-4-e4b-uncensored-hauhaucs-aggressive"   # frames / reviews
T2_IMPLEMENTER = "smollm3-3b-gabliterated-i1"                  # generates intents
DS_IMPLEMENTER = "deepseek-chat"                               # cloud escalation tier
# The Cloudflare-1010 trap: a non-browser UA gets banned. Always send a browser UA.
_UA = "Mozilla/5.0"

# BURST-T1: an ephemeral GPU (Qwen3-32B) reached as a stateless /chat endpoint over the
# eos reverse-tunnel — enqueue a 'chat' command to the control server, poll the result.
BURST_T1 = "qwen3-32b-burst"
_EOS_HOST = "http://127.0.0.1:8888"

# Qwen3 is a reasoning model — strip its <think> block from the returned text.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _resolve_endpoint(model: str) -> tuple[str, str]:
    """Route a model id to its (endpoint, bearer-key). Local floor by default; a cloud tier
    (deepseek) escalated for what the local models mangle."""
    if model.startswith("deepseek"):
        from echelon_sdk.keys import load_deepseek_key
        return "https://api.deepseek.com/v1/chat/completions", load_deepseek_key()
    return ENDPOINT, load_lmstudio_key()


def _chat_via_eos(model: str, messages: list, max_tokens: int, temperature: float,
                  timeout: float) -> str:
    """Reach the burst-T1 GPU through the eos channel: POST a 'chat' command to the control
    server, poll /result. The VM agent curls its localhost vllm and returns the completion.
    Strips the <think> block (Qwen3 is a reasoning model)."""
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    enq = urllib.request.Request(f"{_EOS_HOST}/cmd",
        data=json.dumps({"kind": "chat", "body": body, "timeout": timeout}).encode(),
        headers={"Content-Type": "application/json"})
    cid = json.loads(urllib.request.urlopen(enq, timeout=15).read())["id"]
    deadline = time.time() + timeout + 30
    while time.time() < deadline:
        time.sleep(2)
        r = json.loads(urllib.request.urlopen(f"{_EOS_HOST}/result/{cid}", timeout=10).read())
        if not r.get("pending"):
            if not r.get("ok"):
                raise RuntimeError(f"burst chat failed: {r.get('stderr')}")
            return _THINK_RE.sub("", r.get("content", "")).strip()
    raise TimeoutError(f"burst chat {cid} timed out after {timeout}s")


# DeepSeek is the cloud failover when the local floor is UNREACHABLE (the floor is the owner's
# own hardware and is sometimes offline). The floor stays $0-preferred — we only escalate on a
# genuine TRANSPORT failure (connection refused / timeout / HTTP error / a persistent cold-load),
# never on a merely-weak local answer (a 200 with poor text is still the floor's answer). One
# mapping: every local model id falls back to deepseek-chat.
_DS_FAILOVER_MODEL = "deepseek-chat"


def _post_chat(endpoint: str, key: str, body_obj: dict, timeout: float) -> str:
    """One raw OpenAI-shaped POST. Raises (URLError/HTTPError/timeout) on a transport failure so
    the caller can decide to fail over."""
    req = urllib.request.Request(endpoint, data=json.dumps(body_obj).encode(), headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    })
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(raw)["choices"][0]["message"]["content"]


def _chat(model: str, system: str, user: str, *, max_tokens: int = 1600,
          temperature: float = 0.4, timeout: float = 120.0) -> str:
    """One OpenAI-shaped /chat/completions call. Routes by model id to the local floor
    (gemma/smollm), a cloud tier (deepseek), or the BURST-T1 GPU over the eos channel.
    Returns the assistant text (the text-action body).

    AUTO-FAILOVER: when a LOCAL-floor call fails on TRANSPORT (the floor is offline /
    unreachable / times out / HTTP errors), the SAME prompt is retried once against
    deepseek-chat so a run does not die because the owner's hardware is down. A model id
    already targeting a cloud tier (deepseek*) or the burst GPU is NOT wrapped — it has no
    local floor to fall off of."""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    if model == BURST_T1 or model.startswith("burst"):
        return _chat_via_eos(model, messages, max_tokens, temperature, max(timeout, 600))

    endpoint, key = _resolve_endpoint(model)
    body_obj = {"model": model, "temperature": temperature,
                "max_tokens": max_tokens, "messages": messages}

    # A non-local (deepseek) target has no floor to fall off of — call it directly.
    if model.startswith("deepseek"):
        return _post_chat(endpoint, key, body_obj, timeout)

    # LOCAL FLOOR with cloud failover on transport failure.
    try:
        return _post_chat(endpoint, key, body_obj, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ConnectionError) as e:
        from echelon_sdk.keys import load_deepseek_key
        ds_endpoint, ds_key = "https://api.deepseek.com/v1/chat/completions", load_deepseek_key()
        ds_body = dict(body_obj, model=_DS_FAILOVER_MODEL)
        try:
            return _post_chat(ds_endpoint, ds_key, ds_body, timeout)
        except Exception as ds_e:  # noqa: BLE001
            raise RuntimeError(
                f"local floor unreachable ({type(e).__name__}: {e}) AND deepseek failover failed "
                f"({type(ds_e).__name__}: {ds_e})"
            ) from ds_e
