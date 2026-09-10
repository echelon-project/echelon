"""Focused unit tests for echelon_engine.atoms.providers.base.

Pins the pure surface of the provider contract: the LLMResponse/ToolCall dataclasses,
the reason-extraction + reflex-render helpers, and the shared token-count seam on
ProviderBase. No network — a tiny in-test provider subclass exercises the base.
"""
from __future__ import annotations

import pytest

from echelon_engine.atoms.providers.base import (
    ProviderBase,
    LLMResponse,
    ToolCall,
    _extract_reason,
    _render_reflex,
)


# ── dataclasses ──────────────────────────────────────────────────────────────
def test_toolcall_defaults():
    tc = ToolCall(name="read", args={"path": "x"})
    assert tc.name == "read"
    assert tc.args == {"path": "x"}
    assert tc.id == ""


def test_llmresponse_defaults_and_timestamp():
    r = LLMResponse(content="hi")
    assert r.content == "hi"
    assert r.tokens_in == 0 and r.tokens_out == 0 and r.tokens_cached == 0
    assert r.status == "success"
    assert r.tool_calls == []
    assert r.raw is None
    # timestamp is auto-populated, ISO-ish, and distinct from the default-factory pitfall
    assert isinstance(r.timestamp, str) and "T" in r.timestamp


def test_llmresponse_independent_mutable_defaults():
    a = LLMResponse(content="a")
    b = LLMResponse(content="b")
    a.tool_calls.append(ToolCall(name="x", args={}))
    assert b.tool_calls == []  # field(default_factory) — not a shared list


# ── _extract_reason ──────────────────────────────────────────────────────────
def test_extract_reason_clean_tags():
    out = _extract_reason("<reason>the move is e4</reason><act>...", "<reason>", "</reason>")
    assert out == "the move is e4"


def test_extract_reason_missing_open_tag():
    # weak model jumped straight in, no open tag
    out = _extract_reason("just thinking here</reason>", "<reason>", "</reason>")
    assert out == "just thinking here"


def test_extract_reason_cuts_at_leaked_action_marker():
    # no close tag, but an action marker leaked in — reasoning is everything before it
    out = _extract_reason("<reason>weighing options CHOICE: x", "<reason>", "</reason>")
    assert out == "weighing options"


def test_extract_reason_strips_stray_angle_brackets():
    out = _extract_reason("<weighing the whole thing wrapped>", "<reason>", "</reason>")
    assert out == "weighing the whole thing wrapped"


def test_extract_reason_empty():
    assert _extract_reason("", "<reason>", "</reason>") == ""


# ── _render_reflex ───────────────────────────────────────────────────────────
def test_render_reflex_empty_path():
    assert _render_reflex([]) == ""


def test_render_reflex_uses_gist_and_caps_to_five():
    path = [{"gist": f"lesson {i}"} for i in range(8)]
    out = _render_reflex(path)
    assert out.startswith("EARNED REFLEX")
    # only the first 5 hops are rendered
    assert "lesson 0" in out and "lesson 4" in out
    assert "lesson 5" not in out


def test_render_reflex_falls_back_to_coordinate_and_strings():
    out = _render_reflex([{"coordinate": "echelon:abc"}, "plain string hop"])
    assert "echelon:abc" in out
    assert "plain string hop" in out


# ── ProviderBase.count_tokens / count_messages (shared seam) ──────────────────
class _StubProvider(ProviderBase):
    @property
    def name(self) -> str:
        return "stub"

    def send(self, messages, model_id, tools=None, **kwargs):
        return LLMResponse(content="ok")


def test_provider_count_tokens_positive_int():
    p = _StubProvider()
    n = p.count_tokens("hello world, this is some text")
    assert isinstance(n, int) and n > 0


def test_provider_count_messages_positive_int():
    p = _StubProvider()
    n = p.count_messages([{"role": "user", "content": "hello"},
                          {"role": "assistant", "content": "hi back"}])
    assert isinstance(n, int) and n > 0


def test_send_stream_until_default_raises():
    p = _StubProvider()
    assert p.supports_stream is False
    with pytest.raises(NotImplementedError):
        p.send_stream_until([{"role": "user", "content": "x"}], "m", stop=["</reason>"])
