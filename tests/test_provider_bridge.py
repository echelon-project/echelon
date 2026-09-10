"""Focused unit tests for echelon_engine.atoms.providers.bridge (BridgeProvider).

The send() path needs the live Copilot bridge socket (ECHELON-MCP), so it is NOT
exercised here. We pin the pure, network-free seam: the message _flatten formatter,
the char-based token estimate, and construction/name.
"""
from __future__ import annotations

from echelon_engine.atoms.providers.bridge import (
    BridgeProvider,
    _flatten,
    _est_tokens,
)


def test_name_and_construction():
    p = BridgeProvider(timeout=30.0)
    assert p.name == "copilot-bridge"
    assert p.timeout == 30.0
    assert p._client is None  # lazy — not loaded until first use


def test_flatten_basic_roles():
    out = _flatten([{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"}])
    assert out == "USER: hi\n\nASSISTANT: hello"


def test_flatten_tool_result_role_renamed():
    out = _flatten([{"role": "tool", "content": "42"}])
    assert out.startswith("TOOL_RESULT: 42")


def test_flatten_renders_tool_calls_as_text():
    msgs = [{"role": "assistant", "content": "thinking",
             "tool_calls": [{"function": {"name": "read", "arguments": '{"p":"x"}'}}]}]
    out = _flatten(msgs)
    assert "[tool call: read(" in out
    assert "thinking" in out


def test_flatten_handles_missing_content():
    # a turn with no content key must not crash; empty content renders cleanly
    out = _flatten([{"role": "user"}])
    assert out == "USER: "


def test_est_tokens_char_based():
    assert _est_tokens("") == 1            # floor of 1
    assert _est_tokens("a" * 40) == 10     # ~4 chars/token
