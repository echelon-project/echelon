"""Tests for echelon_engine.atoms.providers.floor_chat auto-failover.

When the LOCAL floor (the owner's hardware, sometimes offline) is unreachable on
TRANSPORT, _chat retries the SAME prompt against deepseek-chat so a run does not die.
A cloud target (deepseek*) or the burst GPU is NOT wrapped. We stub the raw POST so
the test is network-free and deterministic — the routing/failover decision is what's
under test, not the wire.
"""
from __future__ import annotations
import urllib.error
import pytest

from echelon_sdk.config import floor_host
from echelon_engine.atoms.providers import floor_chat
from echelon_engine.atoms.providers.floor_chat import _chat, T1_ARCHITECT

#: The floor is CONFIGURED (LM_ENDPOINT env -> config 'floor.host' -> loopback default),
#: so these tests assert against the resolved host rather than any literal address.
FLOOR = floor_host()


def _is_floor(endpoint: str) -> bool:
    return endpoint.startswith(FLOOR)


@pytest.fixture(autouse=True)
def _stub_lmstudio_key(monkeypatch):
    """_resolve_endpoint calls load_lmstudio_key() for any non-deepseek model BEFORE the
    stubbed _post_chat runs — on a machine without LM Studio that raises and the failover
    logic under test is never reached. The key value itself is irrelevant to these tests."""
    monkeypatch.setattr(floor_chat, "load_lmstudio_key", lambda *a, **k: "sk-lm-test")


def test_local_floor_up_uses_floor(monkeypatch):
    """Floor reachable -> answer comes from the floor, deepseek never touched."""
    seen = {}
    def _post(endpoint, key, body, timeout):
        seen["endpoint"] = endpoint
        seen["model"] = body["model"]
        return "floor answer"
    monkeypatch.setattr(floor_chat, "_post_chat", _post)
    out = _chat(T1_ARCHITECT, "sys", "user")
    assert out == "floor answer"
    assert _is_floor(seen["endpoint"])            # the local floor endpoint
    assert seen["model"] == T1_ARCHITECT          # not remapped


def test_local_floor_down_fails_over_to_deepseek(monkeypatch):
    """Floor down (URLError) -> retried against deepseek-chat, returns its answer."""
    calls = []
    def _post(endpoint, key, body, timeout):
        calls.append((endpoint, body["model"]))
        if _is_floor(endpoint):                   # the local floor attempt
            raise urllib.error.URLError("connection refused")
        return "deepseek answer"                   # the failover attempt
    monkeypatch.setattr(floor_chat, "_post_chat", _post)
    monkeypatch.setattr(floor_chat, "load_deepseek_key", lambda *a, **k: "sk-ds-test",
                        raising=False)
    # load_deepseek_key is imported lazily inside _chat; patch the source too.
    import echelon_sdk.keys as _keys
    monkeypatch.setattr(_keys, "load_deepseek_key", lambda *a, **k: "sk-ds-test")

    out = _chat(T1_ARCHITECT, "sys", "user")
    assert out == "deepseek answer"
    # first the local floor was tried, then deepseek-chat
    assert len(calls) == 2
    assert _is_floor(calls[0][0]) and calls[0][1] == T1_ARCHITECT
    assert "deepseek.com" in calls[1][0] and calls[1][1] == "deepseek-chat"


def test_deepseek_target_not_wrapped(monkeypatch):
    """A deepseek* model id has no local floor to fall off of — called directly, once."""
    calls = []
    def _post(endpoint, key, body, timeout):
        calls.append(endpoint)
        return "ds direct"
    monkeypatch.setattr(floor_chat, "_post_chat", _post)
    out = _chat("deepseek-chat", "sys", "user")
    assert out == "ds direct"
    assert len(calls) == 1
    assert "deepseek.com" in calls[0]


def test_both_down_raises_clear_error(monkeypatch):
    """Floor down AND deepseek down -> a single clear RuntimeError naming both failures."""
    def _post(endpoint, key, body, timeout):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(floor_chat, "_post_chat", _post)
    import echelon_sdk.keys as _keys
    monkeypatch.setattr(_keys, "load_deepseek_key", lambda *a, **k: "sk-ds-test")
    with pytest.raises(RuntimeError) as ei:
        _chat(T1_ARCHITECT, "sys", "user")
    msg = str(ei.value).lower()
    assert "local floor unreachable" in msg
    assert "deepseek failover failed" in msg
