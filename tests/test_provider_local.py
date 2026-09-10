"""Focused unit tests for echelon_engine.atoms.providers.local (LocalProvider).

No network: construct with an explicit api_key (bypasses the keyfile probe) and drive
the pure response PARSER (_parse) directly — OpenAI-shape payload in, LLMResponse out,
including the LM-Studio bare-{"error":...} case and tool-call normalization.
"""
from __future__ import annotations

from echelon_engine.atoms.providers.local import LocalProvider
from echelon_engine.atoms.providers.base import LLMResponse, ToolCall


def _p() -> LocalProvider:
    return LocalProvider(api_key="sk-lm-test")


def test_name_and_stream_flag():
    p = _p()
    assert p.name == "local"
    assert p.supports_stream is True


def test_parse_plain_text():
    p = _p()
    data = {"model": "smollm3-3b", "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
    r = p._parse(data, "smollm3-3b")
    assert isinstance(r, LLMResponse)
    assert r.status == "success"
    assert r.content == "hello"
    assert r.tokens_in == 12 and r.tokens_out == 3
    assert r.tool_calls == []


def test_parse_tool_call_json_args():
    p = _p()
    data = {"choices": [{"message": {"content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read",
                            "arguments": '{"path": "x.py"}'}}]}}]}
    r = p._parse(data, "m")
    assert r.status == "success"
    assert len(r.tool_calls) == 1
    tc = r.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "read" and tc.args == {"path": "x.py"} and tc.id == "c1"


def test_parse_bare_error_envelope():
    # a 200 carrying {"error": "..."} (no choices) is surfaced as an error status
    p = _p()
    r = p._parse({"error": "Model is unloaded."}, "m")
    assert r.status == "error"
    assert "unloaded" in r.content.lower()


def test_parse_unexpected_shape():
    p = _p()
    r = p._parse({"weird": True}, "m")
    assert r.status == "error"
    assert "unexpected response" in r.content


def test_send_absorbs_lm_studio_peg_format_error_with_bounded_redecode(monkeypatch):
    """OPEN-0071: gpt-oss-20b mid-loop gets 'HTTP 400: Engine protocol predict stream returned an
    error: ... does not match the expected peg-native format' from LM STUDIO's own harmony parser
    (a bad decode, not our parsing). send() re-decodes up to peg_retries times (last one with
    temperature nudged) before surfacing the error, so the dispatch loop never pays an attempt."""
    from echelon_engine.atoms.providers import local as L
    p = L.LocalProvider(api_key="k", endpoint="http://x/v1/chat/completions", peg_retries=2)
    peg = L.LLMResponse(content="HTTP 400: Engine protocol predict stream returned an error: "
                        '{"code":500,"message":"The model produced output that does not match the '
                        'expected peg-native format","type":"server_error"}', status="error", model_id="m")
    ok = L.LLMResponse(content="def f(): return 1", status="success", model_id="m")
    seen = []
    answers = [peg, peg, ok]
    monkeypatch.setattr(p, "_send_once", lambda body, mid, t: (seen.append(dict(body)), answers.pop(0))[1])
    r = p.send([{"role": "user", "content": "go"}], "openai/gpt-oss-20b")
    assert r.status == "success" and r.content == "def f(): return 1"
    assert len(seen) == 3 and p._peg_hits == 2
    assert seen[0]["temperature"] == 0 and seen[1]["temperature"] == 0 and seen[2]["temperature"] == 0.3
    # exhausted: the error still surfaces (never masked forever) and counts every hit
    p2 = L.LocalProvider(api_key="k", endpoint="http://x/v1/chat/completions", peg_retries=2)
    monkeypatch.setattr(p2, "_send_once", lambda body, mid, t: peg)
    r2 = p2.send([{"role": "user", "content": "go"}], "openai/gpt-oss-20b")
    assert r2.status == "error" and "peg-native" in r2.content and p2._peg_hits == 2
    # an unrelated error is NOT retried
    other = L.LLMResponse(content="HTTP 500: boom", status="error", model_id="m")
    p3 = L.LocalProvider(api_key="k", endpoint="http://x/v1/chat/completions")
    calls = []
    monkeypatch.setattr(p3, "_send_once", lambda body, mid, t: (calls.append(1), other)[1])
    assert p3.send([{"role": "user", "content": "go"}], "m").status == "error" and len(calls) == 1
