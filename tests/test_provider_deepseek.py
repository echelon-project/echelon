"""Focused unit tests for echelon_engine.atoms.providers.deepseek (DeepSeekProvider).

No network: explicit api_key, drive the pure _parse — including DeepSeek's cache-hit
token accounting (prompt_cache_hit_tokens -> LLMResponse.tokens_cached) — plus the
shared class-meter wiring.
"""
from __future__ import annotations

from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
from echelon_engine.atoms.providers.base import LLMResponse, ToolCall


def _p() -> DeepSeekProvider:
    return DeepSeekProvider(api_key="sk-test")


def test_name():
    assert _p().name == "deepseek"


def test_parse_carries_cache_hit_tokens():
    p = _p()
    data = {"model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                      "prompt_cache_hit_tokens": 800}}
    r = p._parse(data, "deepseek-v4-pro")
    assert isinstance(r, LLMResponse)
    assert r.tokens_in == 1000 and r.tokens_out == 50
    assert r.tokens_cached == 800   # the cache-hit portion is carried for cheap billing


def test_parse_no_cache_info_defaults_zero():
    p = _p()
    data = {"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    r = p._parse(data, "deepseek-chat")
    assert r.tokens_cached == 0


def test_parse_tool_call():
    p = _p()
    data = {"choices": [{"message": {"content": "",
            "tool_calls": [{"id": "d1", "function": {"name": "grep",
                            "arguments": '{"q": "foo"}'}}]}}]}
    r = p._parse(data, "deepseek-chat")
    assert r.tool_calls[0] == ToolCall(name="grep", args={"q": "foo"}, id="d1")


def test_parse_unexpected_shape():
    p = _p()
    r = p._parse({"nope": 1}, "deepseek-chat")
    assert r.status == "error" and "unexpected response" in r.content


def test_charge_skips_error_response():
    # _charge must not meter an error response (only successful draws bill).
    p = _p()
    before = DeepSeekProvider.total_spent()
    p._charge(LLMResponse(content="HTTP 500", status="error"))
    assert DeepSeekProvider.total_spent() == before


def test_shared_class_meter():
    a = DeepSeekProvider(api_key="sk-a")
    b = DeepSeekProvider(api_key="sk-b")
    assert a._meter is b._meter
    assert isinstance(DeepSeekProvider.total_spent(), float)
