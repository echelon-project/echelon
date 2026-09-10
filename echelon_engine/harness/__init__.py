"""harness — adapters for AI harnesses that connect to the ECHELON proxy.

Each adapter knows how to:
  1. Parse the harness's incoming request format
  2. Inject ECHELON context (REFLEX anchor, bank stats, pins)
  3. Translate the standardized request to the backend provider
  4. Translate the backend response back to the harness's expected format

Usage:
  echelon proxy --harness claude-code    (default, Anthropic Messages API)
  echelon proxy --harness open-router    (OpenAI Chat Completions API)
  echelon proxy --harness copilot-chat   (GitHub Copilot Chat)
  echelon proxy --harness gemini-cli     (Gemini CLI)
"""
from __future__ import annotations

from .base import HarnessAdapter, registry


def resolve(name: str) -> HarnessAdapter:
    """Resolve a harness name to its adapter. Falls back to claude-code."""
    return registry.get(name) or registry["claude-code"]
