"""Focused unit tests for echelon_engine.atoms.providers.grok (GrokProvider).

No network: construct with an explicit api_key, drive the pure _parse, and check the
class-level shared meter wiring (total_spent reads the class _meter).
"""
from __future__ import annotations

from echelon_engine.atoms.providers.grok import GrokProvider
from echelon_engine.atoms.providers.base import LLMResponse, ToolCall


def _p() -> GrokProvider:
    return GrokProvider(api_key="xai-test")


def test_name():
    assert _p().name == "grok"


def test_parse_text_and_usage():
    p = _p()
    data = {"model": "grok-4.3", "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 7}}
    r = p._parse(data, "grok-4.3")
    assert isinstance(r, LLMResponse)
    assert r.status == "success" and r.content == "answer"
    assert r.tokens_in == 100 and r.tokens_out == 7
    assert r.model_id == "grok-4.3"


def test_parse_tool_call():
    p = _p()
    data = {"choices": [{"message": {"content": "",
            "tool_calls": [{"id": "t9", "function": {"name": "bash",
                            "arguments": '{"cmd": "ls"}'}}]}}]}
    r = p._parse(data, "grok-4.3")
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0] == ToolCall(name="bash", args={"cmd": "ls"}, id="t9")


def test_parse_malformed_tool_args_kept_raw():
    p = _p()
    # invalid JSON in arguments -> wrapped as {"_raw": ...}, still a success parse
    data = {"choices": [{"message": {"content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "x",
                            "arguments": "{not json"}}]}}]}
    r = p._parse(data, "grok-4.3")
    assert r.tool_calls[0].args == {"_raw": "{not json"}


def test_parse_unexpected_shape():
    p = _p()
    r = p._parse({}, "grok-4.3")
    assert r.status == "error" and "unexpected response" in r.content


def test_shared_class_meter_is_aggregate():
    # total_spent reads the CLASS meter, not a per-instance one (the live aggregation finding).
    a = GrokProvider(api_key="xai-a")
    b = GrokProvider(api_key="xai-b")
    assert a._meter is b._meter
    assert isinstance(GrokProvider.total_spent(), float)
