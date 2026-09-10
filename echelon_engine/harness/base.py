"""base — the HarnessAdapter protocol and registry.

Each harness adapter is registered by name in the `registry` dict. The proxy
resolves `--harness <name>` to an adapter and delegates format-specific logic.
"""
from __future__ import annotations

from typing import Any, Protocol


class HarnessAdapter(Protocol):
    """Protocol for harness adapters. Each concrete adapter implements this."""

    name: str
    label: str
    description: str

    def parse_request(self, body: dict) -> dict:
        """Parse the harness's incoming request into a standardized dict:
          {model, messages: [{role, content}], tools, stream, max_tokens, temperature, ...}
        """
        ...

    def build_request(self, parsed: dict, echelon_context: str) -> dict:
        """Inject ECHELON context and return the request to send to the backend provider.
        The returned dict MUST be in the backend's expected format (Anthropic Messages API
        for upstream, or Gemini format for gemini provider).
        """
        ...

    def parse_response(self, backend_response: dict, stream: bool) -> dict:
        """Translate the backend response back to the harness's expected format."""
        ...

    def stream_chunk(self, backend_chunk: dict) -> dict:
        """Translate one streaming SSE chunk from backend format to harness format."""
        ...

    def health_info(self) -> dict:
        """Extra fields for the /health endpoint (harness name, version, etc.)."""
        ...


# ── Registry ──────────────────────────────────────────────────────────────────

registry: dict[str, HarnessAdapter] = {}


def register(adapter: HarnessAdapter) -> HarnessAdapter:
    """Register a harness adapter. Use as a decorator or call directly."""
    registry[adapter.name] = adapter
    return adapter
