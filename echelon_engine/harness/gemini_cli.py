"""gemini_cli — Gemini CLI harness adapter.

Google's Gemini CLI speaks a Gemini-native API format that differs from
both Anthropic Messages and OpenAI Chat Completions. This adapter handles
the translation in both directions.

When the proxy is running with --provider gemini, this adapter is mostly
passthrough since the backend already speaks Gemini.
"""
from __future__ import annotations

from .base import register, HarnessAdapter


class GeminiCLIAdapter:
    name = "gemini-cli"
    label = "Gemini CLI"
    description = "Google Gemini CLI (Gemini-native API format)"

    def parse_request(self, body: dict) -> dict:
        """Parse Gemini API request into standardized form."""
        messages = []
        for content_item in body.get("contents", []):
            role = content_item.get("role", "user")
            if role == "model":
                role = "assistant"
            parts = content_item.get("parts", [])
            text = "\n".join(
                p.get("text", "") for p in parts if "text" in p
            )
            messages.append({"role": role, "content": text})

        # Extract system instruction
        system = None
        si = body.get("systemInstruction", {})
        if si:
            parts = si.get("parts", [])
            system = "\n".join(p.get("text", "") for p in parts if "text" in p)

        # Extract tools
        tools = None
        td = body.get("tools", [])
        if td:
            tools = []
            for t in td:
                for decl in t.get("functionDeclarations", []):
                    tools.append({
                        "name": decl.get("name", ""),
                        "description": decl.get("description", ""),
                        "input_schema": decl.get("parameters", {}),
                    })

        return {
            "model": body.get("model", ""),
            "messages": messages,
            "system": system,
            "tools": tools,
            "stream": body.get("stream", False),
            "max_tokens": body.get("generationConfig", {}).get("maxOutputTokens", 4096),
            "temperature": body.get("generationConfig", {}).get("temperature"),
            "top_p": body.get("generationConfig", {}).get("topP"),
            "_gemini_body": body,
        }

    def build_request(self, parsed: dict, echelon_context: str) -> dict:
        """Build Anthropic Messages API request (backend translates to Gemini if needed)."""
        system = parsed.get("system") or ""
        if system:
            system = system + "\n\n" + echelon_context
        else:
            system = echelon_context

        return {
            "model": _map_gemini_model(parsed["model"]),
            "system": system,
            "messages": parsed["messages"],
            "tools": parsed.get("tools"),
            "max_tokens": parsed["max_tokens"],
            "stream": parsed["stream"],
            "temperature": parsed.get("temperature"),
        }

    def parse_response(self, backend_response: dict, stream: bool) -> dict:
        """Translate Anthropic response to Gemini response format."""
        candidates = []
        content_text = ""
        for block in backend_response.get("content", []):
            if block.get("type") == "text":
                content_text += block.get("text", "")
            elif block.get("type") == "tool_use":
                import json
                candidates.append({
                    "content": {
                        "role": "model",
                        "parts": [{
                            "functionCall": {
                                "name": block.get("name", ""),
                                "args": block.get("input", {}),
                            }
                        }],
                    },
                    "finishReason": "STOP",
                })

        if content_text:
            candidates.append({
                "content": {
                    "role": "model",
                    "parts": [{"text": content_text}],
                },
                "finishReason": _map_gemini_stop_reason(
                    backend_response.get("stop_reason", "STOP")
                ),
            })

        return {
            "candidates": candidates or [{"content": {"role": "model", "parts": [{"text": ""}]}}],
            "usageMetadata": {
                "promptTokenCount": backend_response.get("usage", {}).get("input_tokens", 0),
                "candidatesTokenCount": backend_response.get("usage", {}).get("output_tokens", 0),
            },
        }

    def stream_chunk(self, backend_chunk: dict) -> dict:
        """Translate streaming chunk to Gemini format."""
        delta = backend_chunk.get("delta", {})
        text = ""
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
        return {
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": text}]},
            }],
        }

    def health_info(self) -> dict:
        return {"harness": "gemini-cli", "api": "gemini-native"}


register(GeminiCLIAdapter())


def _map_gemini_model(model: str) -> str:
    """Map Gemini model names to backend equivalents."""
    mapping = {
        "gemini-2.5-pro": "claude-opus-4-8",
        "gemini-2.5-flash": "claude-sonnet-4-6",
        "gemini-2.0-flash": "claude-haiku-4-5",
    }
    return mapping.get(model, model)


def _map_gemini_stop_reason(reason: str) -> str:
    mapping = {
        "end_turn": "STOP",
        "max_tokens": "MAX_TOKENS",
        "tool_use": "STOP",
        "stop_sequence": "STOP",
    }
    return mapping.get(reason, "STOP")
