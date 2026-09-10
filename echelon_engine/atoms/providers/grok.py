"""GrokProvider — the cheap-frontier brain (xAI Grok, OpenAI-shaped).

Wire is the proven shape from LOCAL_LLM/scripts/grind.py::call_local: POST to an
OpenAI-style /chat/completions with Bearer auth. Grok adds two things the loop needs:
  - OpenAI `tools` (function schemas) on the request,
  - `choices[0].message.tool_calls` parsed back into our normalized ToolCall list.

grok-4.3 splits thinking into `reasoning_content`, so `message.content` is clean —
no </think> leak to strip. Pricing (verified 2026-06-05): grok-4.3 = $1.25/1M in,
$2.50/1M out; DOUBLES above the 200K-token long_context_threshold. Keep ctx < 200K.

Stdlib only (urllib) — no third-party dep, matches grind.py's discipline.
"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.error
from typing import Any

from .base import ProviderBase, LLMResponse, ToolCall
from echelon_sdk.keys import load_xai_key
from echelon_sdk.paths import log_file

_ENDPOINT = "https://api.x.ai/v1/chat/completions"
_LONG_CTX_THRESHOLD = 200_000  # tokens; price doubles above this


class GrokProvider(ProviderBase):
    # ── shared USD meter (class-level so EVERY GrokProvider instance charges the SAME ledger).
    # The live driver makes many providers across a recurse/board run; spend must aggregate, not
    # reset per instance — else a fresh GrokProvider() (e.g. in _default_spend_reader) reads $0
    # while a different instance did the spending (live finding 2026-06-18: spent=0.0 on a run
    # that genuinely dispatched Grok). One class meter = the budget brake sees the real total. ──
    from .cost import Budget as _Budget
    _meter = _Budget(total=float("inf"))   # the cap lives in recurloop/autoloop; meter just counts

    def __init__(self, api_key: str | None = None, endpoint: str = _ENDPOINT):
        self._key = api_key or load_xai_key()
        self._endpoint = endpoint

    @classmethod
    def total_spent(cls) -> float:
        """Cumulative USD this process has drawn through any GrokProvider. _default_spend_reader
        reads this to enforce the budget cap — so the brake is real, not turn/depth-only."""
        return float(cls._meter.spent)

    @property
    def name(self) -> str:
        return "grok"

    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str = "grok-4.3",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0),
        }
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        if tools:
            # OpenAI tool schema: [{"type":"function","function":{name,description,parameters}}]
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
        timeout = kwargs.get("timeout", 90)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            return LLMResponse(content=f"HTTP {e.code}: {detail}", status="error", model_id=model_id)
        except Exception as e:  # noqa: BLE001 — surface any transport failure as a status
            return LLMResponse(content=f"{type(e).__name__}: {e}", status="error", model_id=model_id)

        resp = self._parse(data, model_id)
        # CHARGE the shared meter on a real, successful draw (driver tier — this IS the loop).
        # cache-aware: xAI returns the cache-hit portion under usage.prompt_tokens_details.
        if resp.status == "success":
            cached = (data.get("usage", {}).get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0)
            try:
                self._meter.charge_driver(resp.model_id, resp.tokens_in, resp.tokens_out,
                                          cached_tokens=cached or 0)
            except Exception:   # metering must never break a real call
                pass
        return resp

    def look(
        self,
        image_b64: str,
        question: str,
        *,
        media_type: str = "image/png",
        model_id: str = "grok-4.3",
        **kwargs: Any,
    ) -> LLMResponse:
        """Eyes for a text-only brain. grok-4.3 accepts ['text','image'] input (verified live
        2026-06-05 against /v1/language-models) — so the SAME brain the loop already uses for
        reason()/judge can SEE. Builds the OpenAI multimodal `content` array (text + a base64
        data-URL image) and sends it down the normal /chat/completions wire. The image tokens
        come back inside usage.prompt_tokens, so the existing USD meter already counts them —
        no separate vision pricing. This is CV-001/CV-003 literal: the seeing is delegated to the
        model that has the strength, the cheap text driver keeps driving. See attach_look in tools.py."""
        content = [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
        ]
        return self.send([{"role": "user", "content": content}], model_id=model_id, tools=None, **kwargs)

    def generate_image(
        self,
        prompt: str,
        *,
        out_path: str,
        model_id: str = "grok-imagine-image-quality",
        aspect_ratio: str = "1:1",
        resolution: str = "1k",
        n: int = 1,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Image GENERATION (text -> PNG) — the tier the agent lacked (it could SEE via look(),
        not DRAW). xAI /v1/images/generations, verified contract (owner 2026-06-06): model
        grok-imagine-image-quality (NOT -pro, deprecated 2026-05-15), response_format=b64_json so
        the bytes come inline (URLs are temporary) and the SAME b64 feeds straight back into look()
        for the vision-judge. Writes the PNG to out_path (caller keeps it under the sandbox).

        `n` > 1 = SAME-PROMPT VARIATIONS in ONE request (the doc's sample_batch — most efficient for
        variations of one prompt). Writes n files (out_path stem + _vN); returns the list in `images`.

        Returns {ok, path, b64, images[], model, moderation, prompt, error?}. Stdlib urllib only —
        matches send()'s discipline. NOTE on cost: image gen is priced PER-IMAGE — the caller must
        charge Budget.charge_image(model, n); this method does not meter."""
        import base64
        import os
        endpoint = self._endpoint.replace("/chat/completions", "/images/generations")
        body: dict[str, Any] = {
            "model": model_id, "prompt": prompt, "response_format": "b64_json",
            "aspect_ratio": aspect_ratio, "resolution": resolution,
        }
        if n > 1:
            body["n"] = n
        req = urllib.request.Request(
            endpoint, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"}
        except Exception as e:  # noqa: BLE001 — surface any transport failure flatly
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        items = data.get("data") or []
        if not items:
            return {"ok": False, "error": f"unexpected response: {json.dumps(data)[:400]}"}

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        stem, ext = os.path.splitext(out_path)
        images = []
        for i, item in enumerate(items):
            b64 = item.get("b64_json")
            if not b64:
                continue
            path = out_path if (n == 1 or i == 0 and len(items) == 1) else f"{stem}_v{i+1}{ext}"
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            images.append({"path": path, "b64": b64, "moderation": item.get("respect_moderation")})
        if not images:
            return {"ok": False, "error": "no b64 in any returned image"}
        first = images[0]
        return {"ok": True, "path": first["path"], "b64": first["b64"], "images": images,
                "prompt": prompt, "model": data.get("model", model_id),
                "moderation": first["moderation"]}

    def generate_images_concurrent(
        self, prompts_and_paths: list[tuple], *, max_workers: int = 4, **kw
    ) -> list[dict]:
        """Fire DIFFERENT prompts CONCURRENTLY (the doc's AsyncClient+gather, done with threads to
        keep the stdlib-only discipline — same speedup, no xai_sdk dep). Each item is (prompt,
        out_path). Returns results in input order. Use for salvaging/replaying N distinct prompts."""
        from concurrent.futures import ThreadPoolExecutor
        results: list[dict | None] = [None] * len(prompts_and_paths)

        def _one(idx, prompt, path):
            results[idx] = self.generate_image(prompt, out_path=path, **kw)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_one, i, p, path) for i, (p, path) in enumerate(prompts_and_paths)]
            for f in futs:
                f.result()
        return results  # type: ignore[return-value]

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
                    with open(log_file(), "a") as lf:
                        lf.write(f"grok {fn.get('name', '?')} raw: {args}\n")
                    args = {"_raw": args}
            tool_calls.append(ToolCall(name=fn.get("name", ""), args=args, id=tc.get("id", "")))

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            model_id=data.get("model", model_id),
            status="success",
            tool_calls=tool_calls,
            raw=data,
        )
