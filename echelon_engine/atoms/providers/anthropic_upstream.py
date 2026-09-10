"""AnthropicUpstreamProvider — wrap any Anthropic-compatible endpoint (DeepSeek,
Anthropic, or any /v1/messages-compatible upstream) behind the same send_anthropic()
→ LLMResponse → anthropic_response()/anthropic_sse() interface as GeminiProvider.

The proxy calls ONE method (send_anthropic) and uses the static response builders
regardless of backend — Gemini Vertex, DeepSeek Anthropic endpoint, or raw Anthropic.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, AsyncGenerator

import httpx

from .base import LLMResponse, ToolCall


class AnthropicUpstreamProvider:
    """Wraps an Anthropic-compatible /v1/messages endpoint behind the same
    Anthropic-native interface as GeminiProvider.send_anthropic().

    The proxy creates ONE provider per mode and calls:
        resp = provider.send_anthropic(body)       # LLMResponse
        provider.anthropic_response(resp)           # dict (JSON)
        provider.anthropic_sse(resp)                # async gen (SSE)

    No format translation needed — the upstream already speaks Anthropic.
    """

    def __init__(self, upstream_url: str, api_key: str = ""):
        self._url = upstream_url.rstrip("/")
        self._key = api_key
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "anthropic_upstream"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            h: dict[str, str] = {}
            if self._key:
                h["x-api-key"] = self._key
            self._client = httpx.AsyncClient(headers=h, http2=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Core interface (same shape as GeminiProvider) ───────────────────

    async def send_anthropic(self, body: dict) -> LLMResponse:
        """Forward an Anthropic request body to the upstream, parse the response
        into an LLMResponse. Non-streaming only — streaming is handled separately."""
        client = await self._get_client()
        target = f"{self._url}/v1/messages"

        payload = json.dumps(body).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        resp = await client.post(target, content=payload, headers=headers,
                                 timeout=httpx.Timeout(300.0, connect=10.0))

        if resp.status_code != 200:
            return LLMResponse(
                content=f"upstream error {resp.status_code}: {resp.text[:500]}",
                status="error",
            )

        data = resp.json()
        content_blocks = data.get("content", [])
        text = ""
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    name=block.get("name", ""),
                    args=block.get("input", {}),
                    id=block.get("id", ""),
                ))

        usage = data.get("usage", {})
        return LLMResponse(
            content=text,
            tool_calls=tool_calls,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            model_id=data.get("model", ""),
            status="success",
        )

    async def send_anthropic_stream(self, body: dict) -> AsyncGenerator[str, None]:
        """Forward an Anthropic streaming request to the upstream, yield raw SSE
        bytes as they arrive. Passes through the upstream's SSE stream unchanged
        (the upstream already speaks Anthropic SSE)."""
        client = await self._get_client()
        target = f"{self._url}/v1/messages"

        body = {**body, "stream": True}
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        async with client.stream("POST", target, content=payload, headers=headers,
                                 timeout=httpx.Timeout(300.0, connect=10.0)) as resp:
            if resp.status_code != 200:
                err = await resp.aread()
                yield f"event: error\ndata: {json.dumps({'error': err.decode()[:500]})}\n\n"
                return
            async for chunk in resp.aiter_bytes():
                yield chunk.decode("utf-8", errors="replace")

    # ── Response builders (static, same interface as GeminiProvider) ─────

    @staticmethod
    def anthropic_response(resp: LLMResponse, model_id: str | None = None) -> dict:
        """LLMResponse → Anthropic non-streaming JSON response."""
        msg_id = f"msg_{uuid.uuid4().hex[:16]}"
        model = model_id or resp.model_id or ""

        content: list[dict[str, Any]] = []
        if resp.content:
            content.append({"type": "text", "text": resp.content})
        for tc in resp.tool_calls:
            content.append({
                "type": "tool_use",
                "id": tc.id or f"toolu_{uuid.uuid4().hex[:8]}",
                "name": tc.name,
                "input": tc.args,
            })

        if resp.status == "error":
            stop_reason = "error"
        elif resp.tool_calls:
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        return {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": resp.tokens_in, "output_tokens": resp.tokens_out},
        }

    @staticmethod
    async def anthropic_sse(resp: LLMResponse, model_id: str | None = None):
        """LLMResponse → Anthropic SSE event stream (synthetic — the upstream
        already streams, so this is only used when we have a complete response)."""
        import asyncio as _asyncio
        msg_id = f"msg_{uuid.uuid4().hex[:16]}"
        model = model_id or resp.model_id or ""

        def _evt(t: str, d: dict) -> str:
            return f"event: {t}\ndata: {json.dumps(d)}\n\n"

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
            tool_id = tc.id or f"toolu_{uuid.uuid4().hex[:8]}"
            yield _evt("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": tool_id, "name": tc.name, "input": {}},
            })
            await _asyncio.sleep(0.001)
            yield _evt("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(tc.args)},
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
