"""swarm effort control — the routing knob the 2026-08-17 bakeoff measured but never wired.

WHY THIS TEST EXISTS. `echelon bakeoff` proved effort is a real, non-monotonic cost lever
(flash at `high` cost $0.0053 over a 28-item sealed set while flash at `low` cost $0.0110 —
low effort emitted 8,166 output tokens against high's 3,100, and output bills ~3x input).
None of it was reachable from `echelon swarm`: no --effort flag, zero effort references in
swarm/dispatch.py. This suite guards the wiring that closed that gap.

THE DISCIPLINE (see atom `a-verifier-that-re-derives-the-rule-tests-a-fiction`): a test that
re-implements the effort->request mapping would pass while the real call path drops the field.
So these assert on the ACTUAL HTTP body, captured by patching urllib at the transport seam —
the same body DeepSeek would receive.
"""
import io
import json
import urllib.request

import pytest

import echelon_sdk.keys as K
from echelon_engine.swarm.dispatch import send


class _FakeHTTP(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def wire(monkeypatch):
    """Capture the request body that would go over the wire; return the capture list."""
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _FakeHTTP(json.dumps({
            "choices": [{"message": {"content": "ok", "reasoning_content": ""},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            "model": "deepseek-v4-flash",
        }).encode())

    monkeypatch.setattr(K, "load_deepseek_key", lambda *a, **k: "sk-test")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


@pytest.mark.parametrize("effort,thinking,reasoning", [
    ("low", "enabled", "low"),
    ("high", "enabled", "high"),
    ("max", "enabled", "max"),
    ("none", "disabled", None),      # `none` DISABLES thinking, never sends an effort
])
def test_effort_reaches_the_wire(wire, effort, thinking, reasoning):
    """Each effort level lands in the real request body, not just in a mapping table."""
    send("ping", provider="deepseek", model="deepseek-v4-flash", effort=effort)
    body = wire[0]
    assert body["thinking"]["type"] == thinking
    assert body.get("reasoning_effort") == reasoning


def test_empty_effort_preserves_provider_default(wire):
    """effort='' must be OMITTED, not forwarded — passing it explicitly is a DIFFERENT request
    than not passing it. Preserve the owner-ruled adapter default (thinking on, low)."""
    send("ping", provider="deepseek", model="deepseek-v4-flash", effort="")
    body = wire[0]
    assert body["thinking"]["type"] == "enabled"
    assert body["reasoning_effort"] == "low"


def test_effort_is_keyword_only_and_defaulted():
    """Every pre-existing call site (swarm/plan, swarm/council, swarm/author, xray) calls
    send() without an effort. The parameter must be keyword-only AND defaulted so none of
    them break — the atlas found xray.py as a consumer that a swarm/*.py grep missed."""
    import inspect
    sig = inspect.signature(send)
    p = sig.parameters["effort"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default == ""


def test_non_deepseek_providers_ignore_effort_rather_than_crash(monkeypatch):
    """A caller may pass --effort with an anthropic/gemini brain configured. That must be a
    no-op, not a TypeError: the flag is DeepSeek-specific but the swarm is provider-agnostic."""
    seen = {}

    def fake_anthropic(prompt, model, timeout, temperature):
        seen["called"] = True
        return "ok"

    monkeypatch.setattr("echelon_engine.swarm.dispatch._send_anthropic", fake_anthropic)
    out = send("ping", provider="anthropic", model="claude-sonnet-4-6", effort="max")
    assert out == "ok" and seen.get("called")


def test_medium_and_xhigh_fold_to_high(wire):
    """DeepSeek maps medium/xhigh onto high server-side. The bakeoff omitted a `medium` cell
    for exactly this reason — such a cell would silently re-run `high` under another label.
    Guard the fold so a caller asking for medium sees the tier it ACTUALLY lands on."""
    for asked in ("medium", "xhigh"):
        wire.clear()
        send("ping", provider="deepseek", model="deepseek-v4-flash", effort=asked)
        assert wire[0]["reasoning_effort"] == "high", f"{asked} must fold to high"
