"""GeminiProvider — Google Gemini via VERTEX EXPRESS (the proven path), on the google-genai SDK.

THE ARC (owner, 2026-06-19): a $300 GCP trial on no-org project free-ai-499902. The detour that
cost us: an `AQ.Ab8...`-format key is a VERTEX EXPRESS key, NOT an AI Studio key — pointed at the
AI Studio host (generativelanguage.googleapis.com) it 429s "prepayment credits depleted". The
owner handed the working shape: `genai.Client(vertexai=True, api_key=...)`. With vertexai=True the
SDK routes to aiplatform.googleapis.com automatically and a PLAIN key drives Vertex — no gcloud, no
service-account, no ADC OAuth (EROS's gcloud path needs a CLI this machine doesn't have). VERIFIED
LIVE: returned a real completion billed to free-ai-499902 ('ECHELON online', usage metered).

So this provider WRAPS the google-genai SDK (already installed, 1.66.0) rather than hand-rolling
urllib — the SDK owns the Vertex routing + auth that the raw HTTP path got wrong. It converts the
estate's OpenAI-shape `messages` into genai `contents`, and genai usage_metadata into LLMResponse.

BILLING: vertexai=True bills the key's project (free-ai-499902) → the $300 trial credit. Priced
$0 in cost.py while on trial credit; move to published Vertex rates when the credit runs out.

Models (Vertex, 2026): gemini-2.5-flash (default, fast), gemini-2.5-pro, gemini-2.5-flash-lite.

NOTE: text generation is the proven path here. Tool-calling via genai uses a different schema than
OpenAI `tools`; not wired yet (the brain-in-routing goal doesn't need it). A `tools=` arg is
accepted and ignored with a one-line log, so the contract holds; wiring genai FunctionDeclarations
is a clean follow-up.

Layer: echelon_engine.atoms.providers (leaf I/O). Imports echelon_sdk + the google-genai dep only.
"""
from __future__ import annotations
import os
from typing import Any

from .base import ProviderBase, LLMResponse, ToolCall
from echelon_sdk.keys import load_gemini_key
from echelon_sdk.paths import log_file


def _thinking_budget_from_env() -> int | None:
    """Resolve the Gemini thinking budget from GEMINI_THINKING_BUDGET (owner 2026-07-10) — the
    proxy/launcher sets this once so a whole claude-gem session reasons with a real budget instead
    of the thinking-starved default that made recent agentic runs narrate-not-act.
    Returns: 0 (OFF) when unset/empty/unparseable — the honest default that keeps max_tokens meaning
    answer length; a positive int to CAP thinking spend; None for the model's DYNAMIC default
    ("-1"/"dynamic"/"auto"/"on"). Kept env-only so no call site changes."""
    raw = os.environ.get("GEMINI_THINKING_BUDGET", "").strip().lower()
    if not raw:
        return 0
    if raw in ("-1", "dynamic", "auto", "on"):
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _messages_to_contents(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Flatten OpenAI-shape messages into (system_instruction, prompt_text) for genai. System
    messages become the system_instruction; user/assistant text is joined in order. (Tool turns
    aren't modeled yet — see the module note.)"""
    system_parts: list[str] = []
    convo_parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            convo_parts.append(f"Assistant: {content}")
        else:
            convo_parts.append(content if role == "user" else f"{role}: {content}")
    system = "\n\n".join(system_parts) if system_parts else None
    return system, "\n\n".join(convo_parts)


# genai's FunctionDeclaration accepts only a SUBSET of OpenAPI/JSON-Schema and rejects anything
# else with `extra_forbidden`. Claude Code's tool input_schema carries many JSON-Schema validation
# keywords genai forbids: "$schema", "additionalProperties", "exclusiveMinimum/Maximum",
# "minLength", "title", etc. A denylist is whack-a-mole (each new tool surfaces another key and the
# whole /messages request 500s → "API error" on every tool-bearing turn). So we ALLOWLIST the keys
# genai's schema is known to accept, dropping everything else. This is the robust fix.
_GENAI_SCHEMA_KEEP = {
    "type", "format", "description", "nullable", "enum", "items", "properties",
    "required", "example", "anyOf", "minItems", "maxItems", "minimum", "maximum",
    "minLength", "maxLength", "pattern", "default", "propertyOrdering",
}


def _sanitize_genai_schema(node: Any) -> Any:
    """Recursively keep ONLY the JSON-Schema keys genai's OpenAPI schema accepts (allowlist), so
    no forbidden validation keyword (e.g. exclusiveMinimum, $schema, additionalProperties) reaches
    the FunctionDeclaration and 500s the request. Pure; copies. 'minimum'/'maximum' are kept (genai
    accepts them) so an exclusiveMinimum:0 degrades to no bound rather than crashing."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k not in _GENAI_SCHEMA_KEEP:
                continue
            out[k] = _sanitize_genai_schema(v)
        # a 'properties' object with no 'type' confuses genai → default it to object
        if "properties" in out and "type" not in out:
            out["type"] = "object"
        # 'required' must only name keys that SURVIVED in 'properties' — else genai 400s
        # ("required fields [...] are not defined in the schema properties"). Prune to the intersection.
        if isinstance(out.get("required"), list) and isinstance(out.get("properties"), dict):
            out["required"] = [k for k in out["required"] if k in out["properties"]]
            if not out["required"]:
                out.pop("required")
        return out
    if isinstance(node, list):
        return [_sanitize_genai_schema(x) for x in node]
    return node


def _to_genai_tools(tools: list[dict[str, Any]]):
    """Tool schema -> a genai Tool holding FunctionDeclarations. Accepts both the OpenAI shape
    [{"type":"function","function":{name,description,parameters}}] and the Anthropic shape
    [{name,description,input_schema}] (what Claude Code sends). The parameter schema is SANITIZED
    of JSON-Schema meta keys genai rejects (see _sanitize_genai_schema)."""
    from google.genai import types
    decls = []
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
        params = _sanitize_genai_schema(params)
        decls.append(types.FunctionDeclaration(
            name=name,
            description=fn.get("description", ""),
            parameters=params,
        ))
    return [types.Tool(function_declarations=decls)] if decls else None


def _to_genai_contents(messages: list[dict[str, Any]], image_parts: list):
    """OpenAI-shape messages -> genai Content list for a TOOL LOOP. Maps:
      user            -> Content(role='user', parts=[text])
      assistant text  -> Content(role='model', parts=[text])
      assistant w/ tool_calls -> Content(role='model', parts=[function_call ...])
      tool result     -> Content(role='user',  parts=[function_response]) (genai carries results as user)
    System messages are handled separately (system_instruction), so they're skipped here. The latest
    user image_parts are appended to the last user turn so vision still rides a tool loop."""
    from google.genai import types
    import json as _json
    contents = []
    # index tool_call ids -> their function name, so a later tool-result can name its function_response.
    id_to_name: dict[str, str] = {}
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            continue
        if role == "tool":
            tid = m.get("tool_call_id", "")
            fr_part = types.Part.from_function_response(
                name=id_to_name.get(tid, "tool"),
                response={"result": m.get("content", "")})
            # PARALLEL TOOL CALLS (2026-07-09, the 400 INVALID_ARGUMENT wound): when the
            # model emits N function_calls in ONE turn, gemini requires the N
            # function_responses to come back in ONE user Content — "number of function
            # response parts must equal the function call parts of the function call
            # turn". Claude Code sends them as N tool_result blocks → N tool messages
            # here; MERGE consecutive ones into the same user Content.
            last = contents[-1] if contents else None
            if (last is not None and last.role == "user" and last.parts
                    and all(getattr(p, "function_response", None) is not None
                            for p in last.parts)):
                last.parts.append(fr_part)
            else:
                contents.append(types.Content(role="user", parts=[fr_part]))
            continue
        if role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(types.Part.from_text(text=str(m["content"])))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                tid = tc.get("id", "")
                if tid:
                    id_to_name[tid] = name
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = _json.loads(args)
                    except Exception:
                        args = {}
                fc_part = types.Part.from_function_call(name=name, args=args or {})
                # RE-ATTACH the gemini thought_signature onto the function_call part (required by
                # gemini-3.x or it 400s on a multi-tool turn). Claude Code STRIPS the field on echo
                # (proven by the wire tap), so the carried value is usually empty — fall back to the
                # SERVER-SIDE cache keyed by tool_call id (which DOES survive the round-trip).
                sig_b: bytes | None = None
                carried = tc.get("thought_signature", "")
                if carried:
                    try:
                        import base64 as _b64
                        sig_b = _b64.b64decode(carried)
                    except Exception:
                        sig_b = None
                if sig_b is None and tid:
                    sig_b = GeminiProvider._SIG_CACHE.get(tid)
                if sig_b:
                    try:
                        fc_part.thought_signature = sig_b
                    except Exception:
                        pass
                parts.append(fc_part)
            contents.append(types.Content(role="model", parts=parts or [types.Part.from_text(text="")]))
            continue
        # user (default)
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=str(m.get("content", "")))]))
    if image_parts and contents and contents[-1].role == "user":
        contents[-1].parts.extend(image_parts)
    return contents


class GeminiProvider(ProviderBase):
    # Shared USD meter (class-level, mirroring DeepSeek/Grok): spend AGGREGATES across instances so
    # the budget brake sees the real process total. On trial credit a draw charges $0 (cost.py).
    from .cost import Budget as _Budget
    _meter = _Budget(total=float("inf"))

    # THOUGHT-SIGNATURE CACHE (2026-06-28 — the multi-tool 400 fix). gemini-3.x requires the opaque
    # thought_signature returned on a function_call to be ECHOED BACK on the next turn's function_call
    # part. We tried to round-trip it via a custom field on the Anthropic tool_use block — but the
    # WIRE TAP (proxy_log) PROVED Claude Code STRIPS non-standard fields on echo (req#2: has_sig=false),
    # so the harness can't carry it. The fix: cache it SERVER-SIDE keyed by the tool_call id (which DOES
    # survive the round-trip). On capture, store {id: sig}; on rebuild, if the echoed block lost its sig,
    # look it up by id and re-attach. Class-level so it survives across the per-request provider calls.
    _SIG_CACHE: dict[str, bytes] = {}

    # PROVIDER-WIDE BURST GUARD (2026-06-20). Gemini Vertex burst-limits: a fast tool loop (dispatch) or
    # back-to-back council seats trip 429 RESOURCE_EXHAUSTED. The fix belongs HERE, in the provider, not in
    # one caller (brainstorm had a local spacer; dispatch had none and 429'd writing the SDK docs). A
    # class-level clock spaces EVERY gemini call, and a 429 gets a short exponential backoff-retry. So any
    # path through GeminiProvider is protected. See council-floor-needs-spacing-and-a-chain.
    import threading as _threading
    _MIN_GAP_S = 2.0
    _last_call = [0.0]
    _call_lock = _threading.Lock()
    # RATE-LIMIT RETRY WRAPPER (owner 2026-06-28): the Express free tier's RPM/TPM quota on the pro
    # model trips 429 RESOURCE_EXHAUSTED on a sustained agentic task. Google's guidance: exponential
    # backoff WITH JITTER, and be patient (most 429s are transient window-exhaustion that clears in
    # seconds-to-a-minute). These tune the wrapper; raise _RL_MAX_RETRIES / _RL_BACKOFF_CAP_S for a
    # tighter quota. Honors a server Retry-After hint when the SDK surfaces one.
    _RL_MAX_RETRIES = 8          # was 4 — ride out a depleted window instead of giving up at ~30s
    _RL_BACKOFF_BASE_S = 2.0     # first backoff ~2s, doubling
    _RL_BACKOFF_CAP_S = 60.0     # cap a single sleep (a per-minute window resets within this)

    # ── PRO ROTATION POOL (owner 2026-07-10) ──────────────────────────────────────────────────
    # THE FRAGILITY: a single pinned pro model is a single point of quota failure. When 3.1-pro's
    # Express window is depleted, backoff-on-the-same-model burns 8 sleeps then FAILS the turn —
    # exactly the 429-storm that forced the static opus→3.5-flash pin. The fix is ROTATION: on a
    # 429 that a short backoff won't clear (quota window, not a burst blip), switch to the NEXT pro
    # model instead of exhausting retries on one. Ordered best→fallback (owner: "pro rotation
    # between 3.1 pro, 2.5 pro, 3.5 flash"). A model that 404s on this Express key/region (2.5-pro
    # currently does — re-verified 2026-07-10) is auto-skipped and REMEMBERED so the pool self-heals
    # if the key later gains it, with no code change. Set ECHELON_GEM_PRO_POOL to override the order.
    _PRO_POOL: list[str] = ["gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-3.5-flash"]
    _DEAD_MODELS: set[str] = set()   # models that 404'd here — skip on the next rotation (class-wide)
    # how many transient/burst retries to spend on ONE model before rotating to the next pro model.
    # A quota 429 rotates FAST (don't waste the window); a plain 5xx blip still gets a couple tries.
    _ROTATE_AFTER_RETRIES = 2

    @classmethod
    def _pro_pool(cls) -> list[str]:
        raw = os.environ.get("ECHELON_GEM_PRO_POOL", "").strip()
        pool = [m.strip() for m in raw.split(",") if m.strip()] if raw else list(cls._PRO_POOL)
        return [m for m in pool if m not in cls._DEAD_MODELS]

    def __init__(self, api_key: str | None = None):
        self._key = api_key or load_gemini_key()
        self._client = None  # lazy: build the genai client on first send (avoids import cost at routing)

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(vertexai=True, api_key=self._key)
        return self._client

    @classmethod
    def total_spent(cls) -> float:
        """Cumulative USD this process has drawn through any GeminiProvider (the loop's budget
        brake reads this). $0 while on the $300 trial credit."""
        return float(cls._meter.spent)

    def _charge(self, resp: "LLMResponse") -> None:
        if resp.status == "success":
            try:
                self._meter.charge_driver(resp.model_id, resp.tokens_in, resp.tokens_out)
            except Exception:
                pass

    @property
    def name(self) -> str:
        return "gemini"

    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str = "gemini-2.5-flash",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # TOOL-CALLING (wired 2026-06-20, owner: "dispatch, can use gemini right? yes please"). Until now
        # tools= was accepted-and-ignored, so a gemini partner reasoned but never called a tool (the
        # described-it-but-wrote-nothing failure). Now OpenAI tool schemas become genai FunctionDeclarations
        # and the response's function calls map back to our ToolCall — so gemini can DRIVE a dispatch loop.
        # When tools are present we build genai `contents` from the FULL message history (assistant
        # tool_calls + tool results included), not the flattened text path, so a multi-turn loop holds.
        genai_tools = _to_genai_tools(tools) if tools else None
        system, prompt = _messages_to_contents(messages)
        # VISION: images=[path|bytes, ...] are sent as genai image Parts alongside the text prompt.
        # This is what makes Gemini the roster's `vision` tier (its famous strength) — give it eyes on
        # a screenshot, a diagram, or the Flux surface it's polishing. Absent -> plain text, no cost.
        image_parts = self._image_parts(kwargs.get("images"))
        cfg: dict[str, Any] = {}
        if genai_tools is not None:
            cfg["tools"] = genai_tools
            # AUTO function-calling OFF: ECHELON's loop runs tools itself and feeds results back, so the
            # provider must RETURN the function call, not auto-execute it (the genai SDK would otherwise try).
            from google.genai import types as _t
            cfg["automatic_function_calling"] = _t.AutomaticFunctionCallingConfig(disable=True)
        if system:
            cfg["system_instruction"] = system
        if "temperature" in kwargs:
            cfg["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            cfg["max_output_tokens"] = kwargs["max_tokens"]
        # CONTEXT CACHING (owner, 2026-06-19: "Google caching is really cheap — keep the agent loop +
        # TTL as long as possible"): pass cached_content=<cache.name> to reuse a pre-cached prefix
        # (e.g. the plan held alive across a judge loop). 3.5-flash cached input is $0.15 vs $1.50/1M.
        # When a cache carries the system_instruction, DON'T also pass it inline (the SDK rejects both).
        if kwargs.get("cached_content"):
            cfg["cached_content"] = kwargs["cached_content"]
            cfg.pop("system_instruction", None)
        # IMAGE OUTPUT: an image model (gemini-3.1-flash-image) needs response_modalities incl. IMAGE.
        if kwargs.get("response_modalities"):
            cfg["response_modalities"] = kwargs["response_modalities"]
        # IMAGE SIZE/QUALITY: a design MOCKUP doesn't need full-res — a smaller image_size + lower
        # output_compression_quality cuts the output bytes (and the judge's input tokens) a lot. Pass
        # image_size='1K'|'2K'|... and/or compression 1-100 (owner: "prompt the output resolution,
        # the judge doesn't need high-res").
        if kwargs.get("image_size") or kwargs.get("aspect_ratio") or kwargs.get("compression"):
            from google.genai import types as _t
            ic: dict[str, Any] = {}
            if kwargs.get("image_size"):   ic["image_size"] = kwargs["image_size"]
            if kwargs.get("aspect_ratio"): ic["aspect_ratio"] = kwargs["aspect_ratio"]
            if kwargs.get("compression") is not None:
                ic["output_compression_quality"] = kwargs["compression"]
            cfg["image_config"] = _t.ImageConfig(**ic)
        # MEDIA RESOLUTION: how the model READS an input image. A layout/hierarchy judge needs only
        # 'low'/'medium' — far fewer input tokens than the default high-res tiling.
        if kwargs.get("media_resolution"):
            from google.genai import types as _t
            mr = kwargs["media_resolution"]
            cfg["media_resolution"] = getattr(_t.MediaResolution, mr, mr) if isinstance(mr, str) else mr

        # ── THE GEMINI THINKING GATE (owner, 2026-06-19: "thinking tokens are a big trap on Gemini,
        # make the gate a special kind for Gemini") ──────────────────────────────────────────────
        # Gemini 2.5 spends THINKING tokens from the SAME output budget BEFORE any visible text. So a
        # max_tokens that every other provider reads as "answer length" gets EATEN by thinking here —
        # proven: max_tokens=30 returned 'ECHEL' (truncated) until thinking was disabled, then the
        # FULL text fit. This is invisible to a caller and silently corrupts a swarm worker's output.
        # POLICY: thinking is OFF by default (thinking_budget=0) so max_tokens means what every ECHELON
        # caller assumes. Opt back in for genuine reasoning with think=True or an explicit thinking_budget.
        #
        # ENV OVERRIDE (owner 2026-07-10, to mitigate the recent claude-gem crash — a thinking-starved
        # agentic run narrates-not-acts / breaks tool calls): if GEMINI_THINKING_BUDGET is defined in the
        # environment, thread it into EVERY request without touching call sites. The launcher/proxy sets
        # it once so a whole claude-gem session reasons with a real budget.
        # Precedence: explicit thinking_budget kwarg > think=True > env var > the OFF default.
        # Env values: positive int = cap the thinking spend; 0 = OFF; "-1"/"dynamic"/"auto"/"on" = the
        # model's dynamic default (unbounded, like think=True). Unparseable = ignored (OFF default holds).
        thinking_budget = kwargs.get("thinking_budget")          # explicit kwarg wins outright
        if thinking_budget is None:
            if kwargs.get("think"):
                thinking_budget = None                            # think=True -> SDK dynamic default
            else:
                thinking_budget = _thinking_budget_from_env()     # env var, or 0 (OFF) if unset/unparseable
        cfg["_thinking_budget"] = thinking_budget  # popped into ThinkingConfig in _build_config

        # CONTENTS: a tool loop needs the FULL multi-turn history as genai Contents (function_call +
        # function_response parts paired), so the model sees what it called and what came back. Plain
        # text/vision turns keep the flattened single-prompt path (cheaper, unchanged).
        if genai_tools is not None:
            contents = _to_genai_contents(messages, image_parts)
        else:
            contents = [prompt, *image_parts] if image_parts else prompt
        # TOOL-CALL TRUNCATION RETRY (owner 2026-06-28): a tool turn whose function_call args
        # exceed max_output_tokens comes back TRUNCATED — genai sets finish_reason=MAX_TOKENS and
        # the candidate carries no usable function_call → status='error', no tool_calls → the harness
        # sees [Tool use interrupted] and no-ops (the claude-gem 'narrates instead of acts' bug). The
        # output size of a tool call (a big Edit's old/new_string) is unknowable in advance, so we can't
        # pre-size the budget — instead, DETECT the truncation and RETRY with a bigger one. Only when
        # tools are in play (a plain text turn that hits MAX_TOKENS is a genuinely long answer, not a
        # broken tool call — don't loop on those).
        _base_max = cfg.get("max_output_tokens", 0) or 0
        _budgets = [_base_max] if _base_max else [8192]
        if genai_tools is not None:
            # escalate from the requested ceiling; cap so a pathological loop terminates.
            for nxt in (16384, 32768, 65536):
                if nxt > _budgets[-1]:
                    _budgets.append(nxt)
        last = None
        for attempt, mx in enumerate(_budgets):
            cfg["max_output_tokens"] = mx
            try:
                client = self._get_client()
                built_cfg = self._build_config(dict(cfg))  # copy — _build_config pops _thinking_budget
                resp = self._call_with_rotation(client, model_id, contents, built_cfg)
            except Exception as e:  # noqa: BLE001 — surface any SDK/transport failure as a status
                return LLMResponse(content=f"{type(e).__name__}: {e}", status="error", model_id=model_id)
            last = self._to_response(resp, model_id)
            # Retry ONLY a truncated TOOL turn: tools were requested, the response is truncated
            # (MAX_TOKENS finish or empty/error), and we have a bigger budget left to try.
            if (genai_tools is not None
                    and self._is_truncated_tool_turn(resp, last)
                    and attempt < len(_budgets) - 1):
                from echelon_sdk.paths import log_file
                try:
                    with open(log_file(), "a") as lf:
                        lf.write(f"gemini: tool-call truncated at max_output_tokens={mx} "
                                 f"(finish=MAX_TOKENS, no tool_call) — retrying at {_budgets[attempt+1]}\n")
                except Exception:
                    pass
                continue
            break
        return last

    @staticmethod
    def _image_parts(images: Any) -> list:
        """Build genai image Parts from images=[path|bytes, ...]. A str/Path is read off disk; raw
        bytes are sent as-is. mime is sniffed from the extension (png default). Returns [] if none."""
        if not images:
            return []
        from pathlib import Path
        from google.genai import types
        parts = []
        for img in images:
            if isinstance(img, (bytes, bytearray)):
                data, mime = bytes(img), "image/png"
            else:
                p = Path(img)
                data = p.read_bytes()
                ext = p.suffix.lower().lstrip(".")
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                        "gif": "image/gif"}.get(ext, "image/png")
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        return parts

    def look(
        self,
        image_b64: str,
        question: str,
        *,
        media_type: str = "image/png",
        model_id: str = "gemini-2.5-flash",
        **kwargs: Any,
    ) -> LLMResponse:
        """Eyes for a text-only brain — the SAME contract Grok's look() satisfies, so GeminiProvider
        is a drop-in vision tier for attach_look (owner 2026-06-24: switch vision to gemini). The
        b64 image is decoded to bytes and ridden through send(images=[bytes]); the genai path appends
        it to the user turn as an image Part. media_type is informational here (the bytes carry their
        own mime via _image_parts; gemini sniffs png by default for raw bytes). Image tokens land in
        the response usage so the shared _meter counts them. gemini-2.5-flash is the cheap vision tier."""
        import base64
        try:
            data = base64.b64decode(image_b64)
        except Exception as e:  # noqa: BLE001 — a bad b64 must come back as an LLMResponse, not raise
            return LLMResponse(content=f"vision error: bad base64 ({e})", status="error", model_id=model_id)
        return self.send([{"role": "user", "content": question}],
                         model_id=model_id, tools=None, images=[data], **kwargs)

    def _build_config(self, cfg: dict[str, Any]):
        """Turn the kwargs dict into a genai GenerateContentConfig, applying the thinking gate.
        _thinking_budget: 0 disables thinking (default — max_tokens stays honest); None lets the
        model think dynamically (opt-in via think=True); a positive int caps the thinking spend."""
        from google.genai import types
        tb = cfg.pop("_thinking_budget", 0)
        kw: dict[str, Any] = {k: v for k, v in cfg.items() if not k.startswith("_")}
        if tb is not None:
            # Explicit budget (0 = off, N = capped). None = omit -> the model's dynamic default.
            kw["thinking_config"] = types.ThinkingConfig(thinking_budget=tb)
        return types.GenerateContentConfig(**kw) if kw else None

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        """If a 429 carries a server-suggested wait (Retry-After header or a 'retry in Ns' hint in
        the error body), return it in seconds — so we wait the amount the SERVER asked for instead of
        guessing. Best-effort: returns None when nothing parseable is present."""
        import re
        msg = str(exc)
        # google-genai surfaces RetryInfo as 'retryDelay': '37s' inside the error detail
        m = re.search(r"retry[\s_-]*delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s?", msg, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        m = re.search(r"retry[\s-]?after['\"]?\s*[:=]\s*(\d+(?:\.\d+)?)", msg, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    # models we rotate across — a pro model failing over should try the NEXT pro model, not just
    # backoff-on-itself. A flash/other model call rotates within its own single-item pool (no-op).
    @classmethod
    def _rotation_pool_for(cls, model_id: str) -> list[str]:
        """The ordered model pool to rotate through for this call. A pro model → the pro pool
        (with model_id moved to the FRONT so the caller's choice is tried first); anything else →
        just itself (no rotation)."""
        pool = cls._pro_pool()
        if model_id in pool:
            return [model_id] + [m for m in pool if m != model_id]
        # a call that explicitly asked for a pro-family model still gets the pool as fallback
        if "pro" in model_id and pool:
            return [model_id] + [m for m in pool if m != model_id]
        return [model_id]

    def _call_with_rotation(self, client, model_id: str, contents, config):
        """generate_content across the PRO ROTATION POOL. Each model gets a bounded retry budget
        (_ROTATE_AFTER_RETRIES) via _call_with_guard; if it's still failing with a transient/quota
        error, ROTATE to the next pro model instead of burning the full _RL_MAX_RETRIES on one. A
        404 marks the model dead (skipped forever this process). The LAST model in the pool gets the
        FULL retry budget (nowhere left to rotate — ride out its window). Non-transient errors from
        any model raise immediately."""
        pool = self._rotation_pool_for(model_id)
        last_exc: Exception | None = None
        for i, m in enumerate(pool):
            is_last = (i == len(pool) - 1)
            budget = None if is_last else self._ROTATE_AFTER_RETRIES  # None -> full _RL_MAX_RETRIES
            try:
                return self._call_with_guard(client, m, contents, config, max_retries=budget)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                msg = str(e)
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code == 404 or "404" in msg or "NOT_FOUND" in msg:
                    self._DEAD_MODELS.add(m)   # never try this model again this process
                    self._log_rotate(m, "404 NOT_FOUND — dead, skipping", i, pool)
                    continue
                is_quota = code == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg
                transient = is_quota or code in (500, 502, 503, 504) or any(
                    s in msg for s in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL",
                                       "Server disconnected", "Connection reset"))
                if transient and not is_last:
                    self._log_rotate(m, f"{'quota 429' if is_quota else 'transient'} — rotating", i, pool)
                    continue
                raise   # non-transient, or the last model exhausted → surface it
        if last_exc:
            raise last_exc

    def _log_rotate(self, model: str, why: str, idx: int, pool: list[str]):
        nxt = pool[idx + 1] if idx + 1 < len(pool) else "(none left)"
        try:
            with open(log_file(), "a") as lf:
                lf.write(f"gemini: pro-rotation {model} {why} -> next={nxt} "
                         f"(pool={'>'.join(pool)}, dead={sorted(self._DEAD_MODELS)})\n")
        except Exception:
            pass

    def _call_with_guard(self, client, model_id: str, contents, config, *, max_retries: int | None = None):
        """generate_content behind TWO guards:
          1. BURST SPACER — a class-level clock spaces EVERY call _MIN_GAP_S apart (holds across
             instances/seats/steps) so a fast tool loop doesn't spike.
          2. RATE-LIMIT RETRY WRAPPER — a 429 RESOURCE_EXHAUSTED is retried with EXPONENTIAL BACKOFF
             + JITTER (Google's guidance), patient enough (_RL_MAX_RETRIES) to ride out a depleted
             Express-tier window, honoring a server Retry-After hint when present. Non-429 re-raises
             immediately; a 429 re-raises only after the last retry is spent."""
        import time as _t, random as _rand
        retries = self._RL_MAX_RETRIES if max_retries is None else max_retries
        for attempt in range(retries + 1):
            with self._call_lock:
                gap = self._MIN_GAP_S - (_t.monotonic() - self._last_call[0])
                if gap > 0:
                    _t.sleep(gap)
                self._last_call[0] = _t.monotonic()
            try:
                return client.models.generate_content(model=model_id, contents=contents, config=config)
            except Exception as e:
                msg = str(e)
                # TRANSIENT-ERROR NET (2026-07-09, long-loop survival): a multi-hour claude-gem
                # loop makes thousands of Vertex calls — a single transient 500/503/504 or a
                # DEADLINE_EXCEEDED used to raise straight through, error the turn, and derail
                # the loop. Retry those on the same backoff as a 429. The status CODE attribute
                # is checked first (precise); string markers are the fallback for SDK errors
                # that don't carry one. Genuine 4xx (bad request, auth) still raises immediately.
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                is_429 = code == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg
                transient = is_429 or code in (500, 502, 503, 504) or any(
                    s in msg for s in ("UNAVAILABLE", "DEADLINE_EXCEEDED",
                                       "INTERNAL", "Server disconnected", "Connection reset"))
                if not transient or attempt == retries:
                    raise
                # Prefer the server's suggested wait; else exponential backoff capped, WITH jitter.
                suggested = self._retry_after_seconds(e)
                if suggested is not None:
                    back = min(suggested, self._RL_BACKOFF_CAP_S) + _rand.uniform(0, 0.5)
                    why = f"server retry-after {suggested:.0f}s"
                else:
                    base = self._RL_BACKOFF_BASE_S * (2 ** attempt)
                    back = min(base, self._RL_BACKOFF_CAP_S) * (0.5 + _rand.random())  # full jitter
                    why = "exp backoff+jitter"
                from echelon_sdk.paths import log_file
                try:
                    with open(log_file(), "a") as lf:
                        lf.write(f"gemini: transient {code or '429'} ({msg[:80]}), {why} {back:.1f}s "
                                 f"(attempt {attempt+1}/{retries})\n")
                except Exception:
                    pass
                _t.sleep(back)

    @staticmethod
    def _is_truncated_tool_turn(resp: Any, parsed: "LLMResponse") -> bool:
        """True if a TOOL turn came back truncated — the signature of the claude-gem no-op:
        the model ran out of output budget mid-function_call, so genai sets finish_reason=MAX_TOKENS
        and emits no usable tool_call. Detect both shapes: (a) explicit MAX_TOKENS finish with no
        tool_calls parsed, or (b) a fully empty/error response (no text, no tool_calls, no image)."""
        # (b) empty/error — already classified by _to_response
        if parsed.status == "error" and not parsed.tool_calls and not (parsed.content or "").strip():
            return True
        # (a) MAX_TOKENS finish reason on the candidate, with no tool_call recovered
        try:
            cand0 = (getattr(resp, "candidates", None) or [None])[0]
            fr = getattr(cand0, "finish_reason", None)
            fr_name = getattr(fr, "name", None) or str(fr or "")
            if "MAX_TOKENS" in fr_name and not parsed.tool_calls:
                return True
        except Exception:
            pass
        return False

    def _to_response(self, resp: Any, model_id: str) -> LLMResponse:
        # text can raise/warn when the candidate is a pure function call (no text part) — guard it.
        try:
            text = getattr(resp, "text", None) or ""
        except Exception:
            text = ""
        # TOOL CALLS: genai exposes resp.function_calls (each .name + .args dict) on a tool turn. Map them
        # to our normalized ToolCall so the loop executes them exactly like a grok/openai tool turn.
        # THOUGHT SIGNATURE (gemini-3.x): the opaque sig lives on the response PART that carries the
        # function_call (candidate.content.parts[].thought_signature), NOT on resp.function_calls. We
        # pair each function_call with its part's signature so we can ECHO it back next turn (else
        # genai 400s "missing thought_signature" on the SECOND tool call — breaks every multi-tool loop,
        # which is every swarm worker). Walk the candidate parts in order.
        _sig_by_idx: list[str] = []
        try:
            cand0 = (getattr(resp, "candidates", None) or [None])[0]
            for p in (getattr(getattr(cand0, "content", None), "parts", None) or []):
                if getattr(p, "function_call", None) is not None:
                    sig = getattr(p, "thought_signature", None)
                    if isinstance(sig, (bytes, bytearray)):
                        import base64 as _b64
                        sig = _b64.b64encode(sig).decode("ascii")
                    _sig_by_idx.append(sig or "")
        except Exception:
            _sig_by_idx = []
        tool_calls: list[ToolCall] = []
        for i, fc in enumerate(getattr(resp, "function_calls", None) or []):
            args = dict(getattr(fc, "args", None) or {})
            sig = _sig_by_idx[i] if i < len(_sig_by_idx) else ""
            # UNIQUE id, not f"gem_{i}" (THE claude-gem loop-killer, found 2026-07-09):
            # genai function_calls usually carry NO id, so per-turn indexing made EVERY
            # turn's first tool call "gem_0" — the harness conversation then holds duplicate
            # tool_use ids, and Claude Code aborts the colliding tool use mid-stream
            # ("[Tool use interrupted]" on the 2nd tool turn of a session; also silently
            # cross-wired the id-keyed _SIG_CACHE). uuid makes the id session-unique.
            import uuid as _uuid
            tcid = getattr(fc, "id", "") or f"toolu_gem_{_uuid.uuid4().hex[:12]}"
            # CACHE the raw signature bytes by tool_call id — Claude Code strips the field on echo,
            # so we re-attach it server-side by id next turn (see _SIG_CACHE).
            if sig:
                try:
                    import base64 as _b64
                    GeminiProvider._SIG_CACHE[tcid] = _b64.b64decode(sig)
                    # cap the cache — a days-long proxy makes tens of thousands of tool calls;
                    # only recent ids are ever echoed back. Insertion-ordered dict: evict oldest.
                    while len(GeminiProvider._SIG_CACHE) > 2048:
                        GeminiProvider._SIG_CACHE.pop(next(iter(GeminiProvider._SIG_CACHE)))
                except Exception:
                    pass
            tool_calls.append(ToolCall(name=getattr(fc, "name", "") or "", args=args,
                                       id=tcid, thought_signature=sig))
        um = getattr(resp, "usage_metadata", None)
        tokens_in = getattr(um, "prompt_token_count", 0) or 0 if um else 0
        tokens_out = getattr(um, "candidates_token_count", 0) or 0 if um else 0
        # CACHE HIT: how many prompt tokens were served from a cached prefix (billed cheaper).
        tokens_cached = (getattr(um, "cached_content_token_count", 0) or 0) if um else 0
        # IMAGE OUTPUT: an image model returns inline_data blobs in the candidate parts, not .text.
        # Pull the first image's bytes into raw['image_bytes'] so the caller can save the mockup.
        image_bytes = self._extract_image(resp)
        out = LLMResponse(
            content=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=tokens_cached,
            model_id=model_id,
            # success if there's text, a tool call, OR an image — only a truly empty response is an error.
            status="success" if (text or tool_calls or image_bytes) else "error",
            tool_calls=tool_calls,
            raw={"image_bytes": image_bytes} if image_bytes else None,
        )
        self._charge(out)
        return out

    def create_cache(self, model_id: str, contents: str, *, system_instruction: str = "",
                     ttl_seconds: int = 3600, display_name: str = "echelon-cache") -> str:
        """Explicit context cache: park a prefix (e.g. the plan) so a long loop re-reads it CHEAP
        (3.5-flash cached input $0.15 vs $1.50/1M). Returns the cache name to pass as cached_content=.
        TTL default 1h, extendable via update_cache (owner: keep TTL as long as possible). NOTE the
        per-model minimum (3.5-flash = 4096 tokens) — too small a prefix is rejected by the API."""
        from google.genai import types
        client = self._get_client()
        cache = client.caches.create(
            model=model_id,
            config=types.CreateCachedContentConfig(
                display_name=display_name,
                system_instruction=system_instruction or None,
                contents=[contents],
                ttl=f"{ttl_seconds}s",
            ),
        )
        return cache.name

    def update_cache(self, cache_name: str, ttl_seconds: int) -> None:
        """Extend a cache's TTL so it outlives a long judge loop (keep the plan alive cheaply)."""
        from google.genai import types
        self._get_client().caches.update(
            name=cache_name,
            config=types.UpdateCachedContentConfig(ttl=f"{ttl_seconds}s"))

    @staticmethod
    def _extract_image(resp: Any) -> bytes | None:
        """Return the first inline image's raw bytes from a genai response, or None. Image models
        (gemini-3.1-flash-image) put the picture in candidate.content.parts[].inline_data.data."""
        try:
            for cand in getattr(resp, "candidates", None) or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", None) or []:
                    inline = getattr(part, "inline_data", None)
                    data = getattr(inline, "data", None) if inline else None
                    if data:
                        return bytes(data)
        except Exception:  # noqa: BLE001 — extraction is best-effort; absence is normal
            pass
        return None

    # ── Anthropic wire format translation ─────────────────────────────────────
    # Claude Code speaks Anthropic /v1/messages. These methods make GeminiProvider
    # accept Anthropic-format requests and return Anthropic-format responses, so
    # a proxy can route /v1/messages directly to Gemini Vertex AI without a
    # separate translation layer. The same Anthropic API key can then be used
    # where an OpenAI-format key would be needed — the provider owns the bridge.

    # Region note (Express key, asia-southeast1): gemini-2.5-pro and gemini-2.5-flash-lite
    # 404 NOT_FOUND here (re-verified live 2026-07-10) — but a 404 is now auto-handled by the
    # rotation pool (marks the model dead + rotates), so a stale map entry no longer retry-loops
    # the caller. opus → gemini-3.1-pro-preview (owner 2026-07-10): the pro tier maps to the REAL
    # pro head of the rotation pool. Its Express quota IS prone to depletion, but _call_with_rotation
    # now fails a quota-429 OVER to the next pool model (→ gemini-3.5-flash) instead of burning 8
    # backoffs and failing the turn — the fragility that forced the old static flash pin. So we can
    # prefer the strong model again and let rotation absorb the quota storms.
    _ANTHROPIC_MODEL_MAP: dict[str, str] = {
        "claude-sonnet-4-6": "gemini-2.5-flash",
        "claude-sonnet-4-5": "gemini-2.5-flash",
        "claude-opus-4-7":   "gemini-3.1-pro-preview",
        "claude-opus-4-8":   "gemini-3.1-pro-preview",
        "claude-haiku-4-5":  "gemini-2.5-flash",
    }

    _CLAUDE_PREFIXES: list[tuple[str, str]] = [
        ("claude-sonnet", "gemini-2.5-flash"),
        ("claude-opus",   "gemini-3.1-pro-preview"),
        ("claude-haiku",  "gemini-2.5-flash"),
    ]

    @classmethod
    def map_anthropic_model(cls, model: str) -> str:
        """Claude model name → Gemini equivalent. Gemini names pass through."""
        if model.startswith("gemini"):
            return model
        if model in cls._ANTHROPIC_MODEL_MAP:
            return cls._ANTHROPIC_MODEL_MAP[model]
        for prefix, gemini_model in cls._CLAUDE_PREFIXES:
            if model.startswith(prefix):
                return gemini_model
        return "gemini-2.5-flash"

    @staticmethod
    def _flatten_content(content: Any) -> str:
        """Anthropic content (string | [{type, text}, ...]) → flat string. Image blocks
        become a marker, never a str() dump (a base64 dump is a token bomb)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
                    elif block.get("type") == "image":
                        parts.append("[image]")
                    elif block.get("type") == "tool_result":
                        parts.append(GeminiProvider._flatten_content(block.get("content", "")))
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _block_image_bytes(block: dict) -> bytes | None:
        """Anthropic image block → raw bytes (base64 source; url sources aren't fetched)."""
        src = block.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            import base64
            try:
                return base64.b64decode(src["data"])
            except Exception:
                return None
        return None

    @classmethod
    def _content_parts(cls, content: Any) -> tuple[str, list[bytes]]:
        """Anthropic content (str | block list) → (flat_text, image_bytes). This is the
        vision door for claude-gem: an image block (a pasted screenshot, or Claude Code's
        Read of a PNG arriving inside a tool_result) comes back as BYTES so the caller can
        ride it to Gemini as a real image Part — before this, image blocks were either
        DROPPED (user turns) or str()-dumped as base64 text (tool_results — the token bomb
        that blew up long claude-gem loops on any Read of an image). Nested tool_result
        content (str or block list) is walked recursively."""
        if isinstance(content, str):
            return content, []
        if not isinstance(content, list):
            return str(content), []
        texts: list[str] = []
        images: list[bytes] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text", "")))
            elif btype == "image":
                data = cls._block_image_bytes(block)
                if data:
                    images.append(data)
                    texts.append("[image attached]")
                else:
                    texts.append("[image: unreadable source]")
            elif btype == "tool_result":
                t, i = cls._content_parts(block.get("content", ""))
                texts.append(t)
                images.extend(i)
        return "\n".join(texts), images

    @staticmethod
    def _flatten_system(system: Any) -> str | None:
        """Anthropic system (string | [{type, text}] | None) → string or None."""
        if system is None:
            return None
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            parts = [str(b["text"]) for b in system
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "\n\n".join(parts) if parts else None
        return str(system)

    def send_anthropic(self, body: dict) -> LLMResponse:
        """Accept an Anthropic /v1/messages request body, translate to OpenAI
        format, call self.send(), and return the LLMResponse.

        This is the single entry point for an Anthropic-format caller — the
        provider owns the translation, so any Anthropic API client (Claude Code,
        Anthropic SDK) can use a Gemini Vertex key through this door."""
        import json as _json

        model_id = self.map_anthropic_model(body.get("model", ""))
        system = self._flatten_system(body.get("system"))

        # Messages: Anthropic content blocks → string-based OpenAI shape.
        # IMAGES (the claude-gem vision door): image blocks anywhere in the history are
        # extracted as bytes. Only the ones from the TAIL of the conversation (last
        # _IMG_TAIL_MSGS appended messages) ride to Gemini as live image Parts — the model
        # is looking at them NOW. Older ones stay as '[image attached]' markers in text
        # (re-sending every historical screenshot each turn would bloat a long loop).
        _IMG_TAIL_MSGS = 2
        messages: list[dict[str, Any]] = []
        imgs_at: list[tuple[int, bytes]] = []  # (index-into-messages, bytes)
        for m in body.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content")

            if role == "system":
                flat = self._flatten_content(content)
                if flat:
                    system = (system + "\n\n" + flat) if system else flat
                continue

            if role == "assistant" and isinstance(content, list):
                tool_calls: list[dict[str, Any]] = []
                text_parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            # echo the gemini thought_signature back (required on the next turn's
                            # function_call part — else genai 400s on the 2nd tool call).
                            "thought_signature": block.get("_gem_thought_signature", ""),
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": _json.dumps(block.get("input", {})),
                            },
                        })
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                msg: dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    msg["content"] = "\n".join(text_parts)
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                messages.append(msg)
                continue

            if role == "user" and isinstance(content, list):
                has_tool = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                if has_tool:
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            # _content_parts, not str(): a Read of a PNG arrives here as an
                            # image block — str() would dump its base64 as text (token bomb).
                            text, imgs = self._content_parts(block.get("content", ""))
                            messages.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": text,
                            })
                            for b in imgs:
                                imgs_at.append((len(messages) - 1, b))
                    continue
                text, imgs = self._content_parts(content)
                messages.append({"role": role, "content": text})
                for b in imgs:
                    imgs_at.append((len(messages) - 1, b))
                continue

            messages.append({"role": role, "content": self._flatten_content(content)})

        # Tools: Anthropic input_schema → OpenAI parameters
        tools = None
        raw_tools = body.get("tools")
        if raw_tools:
            tools = []
            for t in raw_tools:
                if isinstance(t, dict):
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                        },
                    })

        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "tools": tools,
        }
        # Forward only the TAIL images as live Parts (see _IMG_TAIL_MSGS note above).
        if imgs_at:
            cutoff = len(messages) - _IMG_TAIL_MSGS
            tail_imgs = [b for idx, b in imgs_at if idx >= cutoff]
            if tail_imgs:
                kwargs["images"] = tail_imgs
        if system:
            kwargs["system"] = system
        if body.get("max_tokens"):
            kwargs["max_tokens"] = body["max_tokens"]
        if body.get("temperature") is not None:
            kwargs["temperature"] = body["temperature"]
        if body.get("stop_sequences"):
            kwargs["stop"] = body["stop_sequences"]

        # System as leading system message (GeminiProvider.send reads the first
        # system-role message as system_instruction via _messages_to_contents)
        if system:
            messages = [{"role": "system", "content": system}] + messages

        return self.send(messages, **kwargs)

    async def send_anthropic_stream(self, body: dict):
        """Streaming entry point — calls send_anthropic() then yields synthetic
        Anthropic SSE events. Same interface as AnthropicUpstreamProvider's
        send_anthropic_stream(), so the proxy uses identical routing for both.

        send_anthropic() is a BLOCKING sync Vertex call; this runs on the proxy's
        async event loop (Claude Code is a full-async harness). Calling it directly
        FREEZES the loop for the whole Vertex round-trip — the SSE events can't flush
        and the harness sees a stalled stream → 'API error · Retrying'. Offload the
        blocking call to a worker thread so the loop stays live to stream the events
        out. (async-inherited-from-the-entry-call: never block the async entry path.)

        KEEP-ALIVE (owner 2026-06-28, the [Tool use interrupted] fix): to_thread keeps
        the LOOP unfrozen but emits NOTHING during the wait — and gemini-3.1-pro is slow
        (model latency + the 2s burst spacer + any 429 backoff). The harness opens a
        streaming request, hears SILENCE for 25s+, and ABORTS the tool use mid-flight
        ([Tool use interrupted]). The fix: run the blocking call as a task and emit SSE
        `event: ping` keep-alives every few seconds WHILE it runs, so the byte stream
        stays live and the harness never times out. This is 'fake SSE that behaves like
        a real stream' — synthesized events, but kept alive on the wire."""
        import asyncio, json as _json
        task = asyncio.create_task(asyncio.to_thread(GeminiProvider.send_anthropic, self, body))
        # Drip pings until the Vertex call returns. The harness treats `event: ping` as
        # liveness, not content — it resets its stall timer without altering the message.
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.5)
            except asyncio.TimeoutError:
                yield f"event: ping\ndata: {_json.dumps({'type': 'ping'})}\n\n"
            except Exception:
                break  # the call raised — let the await below surface it
        resp = await task  # re-raises any exception from the thread (handled inside send_anthropic)
        async for event in self.anthropic_sse(resp, resp.model_id):
            yield event

    @staticmethod
    def anthropic_response(resp: LLMResponse, model_id: str | None = None) -> dict:
        """Convert an LLMResponse into an Anthropic-format non-streaming JSON response."""
        import uuid as _uuid
        msg_id = f"msg_{_uuid.uuid4().hex[:16]}"
        model = model_id or resp.model_id or "gemini-2.5-flash"

        content: list[dict[str, Any]] = []
        if resp.content:
            content.append({"type": "text", "text": resp.content})
        for tc in resp.tool_calls:
            block = {
                "type": "tool_use",
                "id": tc.id or f"toolu_{_uuid.uuid4().hex[:8]}",
                "name": tc.name,
                "input": tc.args,
            }
            # carry the gemini thought_signature so it survives the round-trip and can be echoed
            # back on the next turn's function_call (genai requires it — see _to_genai_contents).
            if getattr(tc, "thought_signature", ""):
                block["_gem_thought_signature"] = tc.thought_signature
            content.append(block)

        if resp.status == "error":
            stop_reason = "error"
        elif resp.tool_calls:
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": resp.tokens_in, "output_tokens": resp.tokens_out},
        }

    @staticmethod
    async def anthropic_sse(resp: LLMResponse, model_id: str | None = None):
        """Yield Anthropic-format SSE events from a complete LLMResponse.

        Synthetic streaming: constructs the full SSE event sequence (message_start,
        content_block_start/delta/stop, message_delta, message_stop) with small
        inter-event delays so Claude Code sees a valid event stream."""
        import asyncio as _asyncio
        import json as _json
        import uuid as _uuid

        msg_id = f"msg_{_uuid.uuid4().hex[:16]}"
        model = model_id or resp.model_id or "gemini-2.5-flash"

        def _evt(t: str, d: dict) -> str:
            return f"event: {t}\ndata: {_json.dumps(d)}\n\n"

        yield _evt("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "content": [], "model": model,
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": resp.tokens_in, "output_tokens": 0},
            },
        })
        await _asyncio.sleep(0.001)

        idx = 0
        if resp.content:
            yield _evt("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            await _asyncio.sleep(0.001)
            yield _evt("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": resp.content},
            })
            await _asyncio.sleep(0.001)
            yield _evt("content_block_stop", {
                "type": "content_block_stop", "index": idx,
            })
            await _asyncio.sleep(0.001)
            idx += 1

        for tc in resp.tool_calls:
            tool_id = tc.id or f"toolu_{_uuid.uuid4().hex[:8]}"
            _tu_block = {"type": "tool_use", "id": tool_id, "name": tc.name, "input": {}}
            # carry the gemini thought_signature on the streamed tool_use block too (Claude Code
            # uses the STREAMING path) — it must round-trip back so the next turn can echo it.
            if getattr(tc, "thought_signature", ""):
                _tu_block["_gem_thought_signature"] = tc.thought_signature
            yield _evt("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": _tu_block,
            })
            await _asyncio.sleep(0.001)
            yield _evt("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": _json.dumps(tc.args)},
            })
            await _asyncio.sleep(0.001)
            yield _evt("content_block_stop", {
                "type": "content_block_stop", "index": idx,
            })
            await _asyncio.sleep(0.001)
            idx += 1

        if resp.status == "error":
            stop_reason = "error"
        elif resp.tool_calls:
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        yield _evt("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": resp.tokens_out},
        })
        await _asyncio.sleep(0.001)
        yield _evt("message_stop", {"type": "message_stop"})
