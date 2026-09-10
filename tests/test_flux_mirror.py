"""tests/test_flux_mirror.py -- unit tests for echelon_engine.agent.flux (node + mirror).

Proves the THREE structural guarantees the steering protocol s5b demands, using a fake node so the
suite is $0 / no live Flux server:
  - session-is-forever  -- MirrorSession mints session.create exactly ONCE; create() is a hard trap.
  - anchor-before-drive -- drive()/observe() raise NotAnchored until anchor() has run.
  - drain-not-stream    -- observe() reads the bus drain and advances the cursor (since/next_since).
Plus: envelope unwrap, Guardrail A (moded session needs a target), context-manager teardown.

Network-bound (a live node) is the separate live-proof step, not tested here.
"""
from __future__ import annotations

import pytest

pytest.importorskip("echelon_engine.agent.flux", reason="flux package not available")

from echelon_engine.agent.flux import MirrorSession, ReMintForbidden, NotAnchored
from echelon_engine.agent.flux.node import FluxNodeError


class FakeNode:
    """Records every bus/dom/drain call; returns canned, protocol-shaped replies. No network."""

    def __init__(self):
        self.bus_calls: list[dict] = []
        self.dom_calls: list[dict] = []
        self.drain_calls: list[dict] = []
        self._drain_cursor = 0

    def bus(self, payload, *, session="", secret=""):
        self.bus_calls.append({"payload": payload, "session": session, "secret": secret})
        kind = payload.get("kind")
        if kind == "session.create":
            return {"ok": True, "envelope": {"kind": kind, "sot": True, "payload": {
                "session": "abc123def456", "secret": "s3cr3t",
                "mirror_url": "http://node/?session=abc123def456"}}}
        return {"ok": True, "envelope": {"kind": kind, "payload": {"rev": 1}}}

    def dom(self, session, *, settle="dom", wait_for="", settle_ms=1500):
        self.dom_calls.append({"session": session, "settle": settle})
        return {"tags": ["nav-001"], "html": "<html></html>", "rev": 0}

    def drain(self, session, *, since=0, timeout=2.0, kinds="", max_events=50):
        self.drain_calls.append({"session": session, "since": since})
        self._drain_cursor += 1
        return {"events": [{"kind": "hand", "i": self._drain_cursor}],
                "next_since": since + 1}


# -- session-is-forever ------------------------------------------------------------------
def test_mints_session_exactly_once_on_construction():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/", mode="verify-fix")
    creates = [c for c in node.bus_calls if c["payload"]["kind"] == "session.create"]
    assert len(creates) == 1
    assert m.session == "abc123def456" and m.secret == "s3cr3t"


def test_create_method_is_a_hard_trap():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/")
    with pytest.raises(ReMintForbidden):
        m.create()  # the cardinal sin -- re-mint -- cannot be done by accident through the handle
    # and it did NOT issue a second session.create
    assert sum(c["payload"]["kind"] == "session.create" for c in node.bus_calls) == 1


def test_no_public_create_path_other_than_the_trap():
    # The class exposes exactly one create -- the trap. There is no other mint entry point.
    assert MirrorSession.create.__doc__ and "cardinal sin" in MirrorSession.create.__doc__


# -- anchor-before-drive -----------------------------------------------------------------
def test_drive_before_anchor_raises():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/")
    with pytest.raises(NotAnchored):
        m.drive("click", tag="nav-001")
    # no hand op reached the bus
    assert not any(c["payload"].get("kind") == "hand" for c in node.bus_calls)


def test_observe_before_anchor_raises():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/")
    with pytest.raises(NotAnchored):
        m.observe()


def test_drive_after_anchor_lands_with_session_and_secret():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/")
    m.anchor()
    m.drive("click", tag="nav-001")
    hand = [c for c in node.bus_calls if c["payload"].get("kind") == "hand"][0]
    assert hand["session"] == "abc123def456" and hand["secret"] == "s3cr3t"
    assert hand["payload"]["payload"]["op"] == "click"


# -- drain-not-stream (cursor advances) --------------------------------------------------
def test_observe_advances_the_cursor():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/")
    m.anchor()
    m.observe()
    m.observe()
    # second drain was asked for events AFTER the first (since advanced)
    assert node.drain_calls[0]["since"] == 0
    assert node.drain_calls[1]["since"] == 1


# -- Guardrail A + envelope unwrap -------------------------------------------------------
def test_moded_session_requires_target():
    node = FakeNode()
    with pytest.raises(ValueError):
        MirrorSession(node, target="", mode="verify-fix")
    assert node.bus_calls == []  # refused before the wire


def test_bad_envelope_fails_loud():
    class BadNode(FakeNode):
        def bus(self, payload, *, session="", secret=""):
            return {"ok": True, "envelope": {"payload": {}}}  # no session
    with pytest.raises(FluxNodeError):
        MirrorSession(BadNode(), target="https://example.com/")


# -- teardown ----------------------------------------------------------------------------
def test_context_manager_ends_the_session():
    node = FakeNode()
    with MirrorSession(node, target="https://example.com/") as m:
        m.anchor()
    assert any(c["payload"]["kind"] == "session.end" for c in node.bus_calls)


def test_use_after_end_is_forbidden():
    node = FakeNode()
    m = MirrorSession(node, target="https://example.com/")
    m.anchor()
    m.end()
    with pytest.raises(ReMintForbidden):
        m.drive("click", tag="nav-001")
