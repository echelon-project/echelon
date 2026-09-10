"""claude_code — Claude Code harness adapter (Anthropic Messages API).

The default harness. Claude Code already speaks Anthropic API natively, so
this adapter is mostly passthrough with ECHELON context injection.
"""
from __future__ import annotations

from .base import register, HarnessAdapter


class ClaudeCodeAdapter:
    name = "claude-code"
    label = "Claude Code"
    description = "Claude Code CLI and desktop app (Anthropic Messages API)"

    def parse_request(self, body: dict) -> dict:
        """Claude Code already speaks Anthropic API. Parse into standardized form."""
        return {
            "model": body.get("model", ""),
            "messages": list(body.get("messages", [])),
            "system": body.get("system"),
            "tools": body.get("tools"),
            "stream": body.get("stream", False),
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            # Preserve original for passthrough
            "_original": body,
        }

    def build_request(self, parsed: dict, echelon_context: str) -> dict:
        """Inject ECHELON context into the system message."""
        result = dict(parsed.get("_original", parsed))
        # Inject ECHELON REFLEX into system
        system = result.get("system")
        if isinstance(system, str):
            result["system"] = system + "\n\n" + echelon_context
        elif isinstance(system, list):
            result["system"] = list(system) + [
                {"type": "text", "text": echelon_context, "cache_control": None}
            ]
        else:
            result["system"] = echelon_context
        # Remove internal fields
        result.pop("_original", None)
        return result

    def parse_response(self, backend_response: dict, stream: bool) -> dict:
        """Passthrough — Claude Code expects Anthropic API responses."""
        return backend_response

    def stream_chunk(self, backend_chunk: dict) -> dict:
        """Passthrough streaming chunks."""
        return backend_chunk

    def health_info(self) -> dict:
        return {"harness": "claude-code", "api": "anthropic-messages"}


register(ClaudeCodeAdapter())
