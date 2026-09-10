"""open_router — OpenRouter / OpenAI Chat Completions API harness adapter.

OpenRouter and many tools (Continue.dev, Cursor, Aider, Cody) speak
OpenAI-compatible Chat Completions API. This adapter translates between
OpenAI format and the backend provider's format.
"""
from __future__ import annotations

from .base import register, HarnessAdapter


class OpenRouterAdapter:
    name = "open-router"
    label = "OpenRouter / OpenAI API"
    description = "OpenRouter, Continue.dev, Cursor, Aider, Cody — OpenAI Chat Completions API"

    def parse_request(self, body: dict) -> dict:
        """Parse OpenAI Chat Completions format into standardized form."""
        messages = []
        for m in body.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                # Multi-part content (text + images)
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = "\n".join(texts)
            messages.append({"role": role, "content": content})

        return {
            "model": body.get("model", ""),
            "messages": messages,
            "system": None,  # OpenAI embeds system as a message with role=system
            "tools": _convert_openai_tools(body.get("tools")),
            "stream": body.get("stream", False),
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 0.7),
            "top_p": body.get("top_p"),
            "_openai_body": body,  # Preserve for response translation
        }

    def build_request(self, parsed: dict, echelon_context: str) -> dict:
        """Convert to Anthropic Messages API format with ECHELON context."""
        messages = list(parsed["messages"])

        # Extract system messages from OpenAI format
        system_parts = []
        user_assistant = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                user_assistant.append(m)

        # Add ECHELON context to system
        system_parts.append(echelon_context)
        system = "\n\n".join(system_parts)

        return {
            "model": _map_model(parsed["model"]),
            "system": system,
            "messages": user_assistant,
            "tools": parsed.get("tools"),
            "max_tokens": parsed["max_tokens"],
            "stream": parsed["stream"],
            "temperature": parsed.get("temperature"),
        }

    def parse_response(self, backend_response: dict, stream: bool) -> dict:
        """Translate Anthropic response back to OpenAI Chat Completions format."""
        content = ""
        tool_calls = []
        stop_reason = backend_response.get("stop_reason", "end_turn")

        for block in backend_response.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": _dump_json(block.get("input", {})),
                    },
                })

        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": content or None},
            "finish_reason": _map_stop_reason(stop_reason),
        }
        if tool_calls:
            choice["message"]["tool_calls"] = tool_calls
            choice["message"]["content"] = None

        # Map backend model to original model name (from the OpenAI request)
        model = backend_response.get("model", "")
        openai_body = parsed.get("_openai_body", {}) if isinstance(parsed, dict) else {}
        original_model = openai_body.get("model", model)

        return {
            "id": backend_response.get("id", ""),
            "object": "chat.completion",
            "created": backend_response.get("created", 0),
            "model": original_model,
            "choices": [choice],
            "usage": _convert_usage(backend_response.get("usage", {})),
        }

    def stream_chunk(self, backend_chunk: dict) -> dict:
        """Translate Anthropic SSE chunk to OpenAI streaming chunk."""
        delta = {"role": "assistant"}
        event_type = backend_chunk.get("type", "")

        if event_type == "content_block_delta":
            delta_block = backend_chunk.get("delta", {})
            if delta_block.get("type") == "text_delta":
                delta["content"] = delta_block.get("text", "")
            elif delta_block.get("type") == "input_json_delta":
                delta["tool_calls"] = [{
                    "index": backend_chunk.get("index", 0),
                    "function": {"arguments": delta_block.get("partial_json", "")},
                }]

        return {
            "id": backend_chunk.get("id", ""),
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta}],
        }

    def health_info(self) -> dict:
        return {"harness": "open-router", "api": "openai-chat-completions"}


register(OpenRouterAdapter())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _convert_openai_tools(tools: list | None) -> list | None:
    """Convert OpenAI tool format to Anthropic tool format."""
    if not tools:
        return None
    result = []
    for t in tools:
        fn = t.get("function", {})
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}),
        })
    return result


def _map_model(model: str) -> str:
    """Map OpenAI model names to backend model names."""
    mapping = {
        "gpt-4o": "claude-sonnet-4-6",
        "gpt-4o-mini": "claude-haiku-4-5",
        "gpt-4": "claude-opus-4-8",
        "o1": "claude-opus-4-8",
        "o3-mini": "claude-sonnet-4-6",
    }
    return mapping.get(model, model)


def _map_stop_reason(reason: str) -> str:
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "stop_sequence": "stop",
    }
    return mapping.get(reason, "stop")


def _convert_usage(usage: dict) -> dict:
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }


def _dump_json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
