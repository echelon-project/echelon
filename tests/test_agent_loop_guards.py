"""Tests for echelon_engine.agent.loop_guards — _ask_why_repeat and _judge_repeat_legit.

Pure-logic tests using scripted fake providers and mock MemoryContext.
No live model call needed.
"""
from __future__ import annotations
import json
import pytest
from unittest.mock import MagicMock, patch

from echelon_engine.agent.loop_guards import _ask_why_repeat, _judge_repeat_legit


def _make_tc(name="run_bash", args=None):
    tc = MagicMock()
    tc.name = name
    tc.args = args or {"cmd": "ls -la"}
    return tc


def _make_resp(content="", status="success", tokens_in=10, tokens_out=5):
    r = MagicMock()
    r.content = content
    r.status = status
    r.tokens_in = tokens_in
    r.tokens_out = tokens_out
    return r


# ── _ask_why_repeat ───────────────────────────────────────────────────────────

class TestAskWhyRepeat:
    def test_returns_reason_on_success(self):
        provider = MagicMock()
        provider.send.return_value = _make_resp("The endpoint is flaky, retrying.", tokens_in=20, tokens_out=8)
        reason, ti, to = _ask_why_repeat(provider, [], "model", _make_tc())
        assert reason == "The endpoint is flaky, retrying."
        assert ti == 20
        assert to == 8

    def test_includes_tool_name_in_ask(self):
        provider = MagicMock()
        provider.send.return_value = _make_resp("reason")
        _ask_why_repeat(provider, [], "model", _make_tc(name="write_file", args={"path": "/x"}))
        sent = provider.send.call_args[0][0]
        last_user = next(m for m in reversed(sent) if m["role"] == "user")
        assert "write_file" in last_user["content"]

    def test_returns_empty_on_provider_error(self):
        provider = MagicMock()
        provider.send.return_value = _make_resp("", status="error", tokens_in=0, tokens_out=0)
        reason, ti, to = _ask_why_repeat(provider, [], "model", _make_tc())
        assert reason == ""
        assert ti == 0
        assert to == 0

    def test_empty_content_returns_empty_reason(self):
        provider = MagicMock()
        provider.send.return_value = _make_resp("", status="success", tokens_in=5, tokens_out=0)
        reason, ti, to = _ask_why_repeat(provider, [], "model", _make_tc())
        assert reason == ""

    def test_existing_messages_prepended(self):
        """The provider must receive the existing messages plus the ask."""
        provider = MagicMock()
        provider.send.return_value = _make_resp("reason")
        existing = [{"role": "user", "content": "prior msg"}]
        _ask_why_repeat(provider, existing, "model", _make_tc())
        sent = provider.send.call_args[0][0]
        assert sent[0] == existing[0]
        assert len(sent) == 2   # original + the ask


# ── _judge_repeat_legit ───────────────────────────────────────────────────────

class TestJudgeRepeatLegit:
    def _make_memory(self, judge_provider=None, judge_model="grok-4.3"):
        mc = MagicMock()
        mc.judge_provider = judge_provider
        mc.judge_model = judge_model
        return mc

    def test_no_judge_conservative_floor(self):
        memory = self._make_memory(judge_provider=None)
        legit, score, why, ti, to = _judge_repeat_legit("fail", "reason", memory, "model")
        assert legit is False
        assert score == 0.0
        assert ti == 0
        assert to == 0
        assert "no judge" in why.lower() or "conservative" in why.lower()

    def test_legit_true_when_judge_says_so(self):
        """judge_nonstationary is lazily imported inside the function from echelon_engine.atoms.judge,
        so we patch it at its source module location."""
        judge = MagicMock()
        memory = self._make_memory(judge_provider=judge)
        fake_result = {"legit": True, "score": 0.85, "why": "process is rolling", "_tokens": (12, 6)}
        with patch("echelon_engine.atoms.judge.judge_nonstationary", return_value=fake_result):
            legit, score, why, ti, to = _judge_repeat_legit("state", "reason", memory, "model")
        assert legit is True
        assert score == pytest.approx(0.85)
        assert "rolling" in why
        assert ti == 12
        assert to == 6

    def test_not_legit_when_judge_says_hope(self):
        judge = MagicMock()
        memory = self._make_memory(judge_provider=judge)
        fake_result = {"legit": False, "score": 0.1, "why": "nothing will change", "_tokens": (8, 4)}
        with patch("echelon_engine.atoms.judge.judge_nonstationary", return_value=fake_result):
            legit, score, why, ti, to = _judge_repeat_legit("fail", "just try again", memory, "model")
        assert legit is False
        assert score == pytest.approx(0.1)

    def test_judge_unavailable_returns_false(self):
        judge = MagicMock()
        memory = self._make_memory(judge_provider=judge)
        with patch("echelon_engine.atoms.judge.judge_nonstationary", return_value=None):
            legit, score, why, ti, to = _judge_repeat_legit("fail", "reason", memory, "model")
        assert legit is False
        assert "unavailable" in why.lower()

    def test_tokens_default_zero_when_missing(self):
        judge = MagicMock()
        memory = self._make_memory(judge_provider=judge)
        fake_result = {"legit": True, "score": 0.7, "why": "ok"}   # no _tokens key
        with patch("echelon_engine.atoms.judge.judge_nonstationary", return_value=fake_result):
            legit, score, why, ti, to = _judge_repeat_legit("fail", "reason", memory, "model")
        assert ti == 0
        assert to == 0
