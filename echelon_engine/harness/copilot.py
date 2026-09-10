"""copilot — GitHub Copilot Chat harness adapter.

Copilot Chat uses an OpenAI-compatible API with some extensions.
Mostly delegates to the OpenRouter adapter with Copilot-specific tweaks.

Key differences from standard OpenAI:
  - Model names are Copilot-specific (gpt-4o-copilot, etc.)
  - May include workspace context in a custom field
  - Token usage format may differ
"""
from __future__ import annotations

from .base import register, HarnessAdapter
from .open_router import OpenRouterAdapter

_openrouter = OpenRouterAdapter()


class CopilotChatAdapter:
    name = "copilot-chat"
    label = "GitHub Copilot Chat"
    description = "GitHub Copilot Chat (OpenAI-compatible with Copilot extensions)"

    def parse_request(self, body: dict) -> dict:
        """Parse Copilot Chat request. Mostly OpenAI-compatible."""
        parsed = _openrouter.parse_request(body)

        # Copilot may include workspace/repo context — extract and add to messages
        context = body.get("context") or body.get("workspace") or ""
        if context:
            parsed["messages"].insert(0, {
                "role": "system",
                "content": f"[workspace context]\n{context}",
            })

        return parsed

    def build_request(self, parsed: dict, echelon_context: str) -> dict:
        """Build backend request with ECHELON context + Copilot workspace awareness."""
        # Add a Copilot-specific prefix to ECHELON context
        full_context = (
            "[ECHELON substrate — active]\n" + echelon_context
        )
        return _openrouter.build_request(parsed, full_context)

    def parse_response(self, backend_response: dict, stream: bool) -> dict:
        """Translate to Copilot-compatible response format."""
        return _openrouter.parse_response(backend_response, stream)

    def stream_chunk(self, backend_chunk: dict) -> dict:
        """Passthrough streaming chunks."""
        return _openrouter.stream_chunk(backend_chunk)

    def health_info(self) -> dict:
        return {"harness": "copilot-chat", "api": "copilot-openai"}


register(CopilotChatAdapter())
