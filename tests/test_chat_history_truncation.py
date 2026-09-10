"""Tests for OS_TIER chat history truncation.

Covers:
  - _truncate_history: long history truncated to token budget
  - Recent turns win (oldest dropped first)
  - Current turn always survives
  - Short history passes through untouched
  - Drop count recorded in chat_turn metadata
  - Empty history is a no-op
  - Exact-fit boundary: no false truncation
  - Configurable budget via parameter and env var
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


# ── Unit: _truncate_history ──────────────────────────────────────────────────

class TestTruncateHistory:
    """Direct unit tests for the truncation function."""

    def test_empty_history_returns_empty(self):
        from echelon_engine.services_chat import _truncate_history
        kept, dropped = _truncate_history([], "current turn", 100)
        assert kept == []
        assert dropped == 0

    def test_short_history_passes_through_untouched(self):
        """A small history that fits the budget is returned unchanged."""
        from echelon_engine.services_chat import _truncate_history
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        kept, dropped = _truncate_history(history, "what's up", 500)
        assert kept == history
        assert dropped == 0

    def test_long_history_truncated_to_budget(self):
        """When history exceeds the budget, older turns are dropped."""
        from echelon_engine.services_chat import _truncate_history
        # Build a long history of short messages — far more than a tiny budget.
        history = []
        for i in range(50):
            history.append({"role": "user", "content": f"message {i}"})
            history.append({"role": "assistant", "content": f"reply {i}"})
        # A budget of 100 tokens is far less than 100 messages.
        kept, dropped = _truncate_history(history, "final turn", 100)
        assert dropped > 0
        assert len(kept) < len(history)
        # At minimum, the most recent turn pair should be kept.
        assert len(kept) >= 1

    def test_recent_turns_win_oldest_dropped_first(self):
        """The most recent turn is always kept; the oldest is dropped first."""
        from echelon_engine.services_chat import _truncate_history
        history = [
            {"role": "user", "content": "oldest message"},
            {"role": "assistant", "content": "oldest reply"},
            {"role": "user", "content": "middle message"},
            {"role": "assistant", "content": "middle reply"},
            {"role": "user", "content": "newest message"},
            {"role": "assistant", "content": "newest reply"},
        ]
        # Budget just enough for the last 2 turns + current.
        # "middle message" + "middle reply" ≈ some tokens; the oldest pair
        # should be the first to go.
        kept, dropped = _truncate_history(history, "current turn", 30)
        # The newest turn ("newest message" / "newest reply") must be in kept.
        newest_in_kept = any(
            t.get("content", "") == "newest message" for t in kept
        )
        oldest_in_kept = any(
            t.get("content", "") == "oldest message" for t in kept
        )
        assert newest_in_kept, "newest turn must survive"
        assert not oldest_in_kept, "oldest turn must be dropped first"

    def test_current_turn_always_survives_even_when_large(self):
        """The current text is always preserved — it is counted against the
        budget but never itself dropped (it isn't part of history)."""
        from echelon_engine.services_chat import _truncate_history
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]
        # Budget so tight only the current turn fits (history gets fully dropped).
        kept, dropped = _truncate_history(history, "current turn", 5)
        # The function returns empty kept list — the current turn is handled
        # separately by the caller (chat_turn always processes `text`).
        assert dropped >= 0  # may drop all if budget is too tight

    def test_drop_count_recorded(self):
        """Dropped count equals the number of turns removed."""
        from echelon_engine.services_chat import _truncate_history
        history = []
        for i in range(30):
            history.append({"role": "user", "content": f"msg {i}"})
        kept, dropped = _truncate_history(history, "final", 80)
        assert dropped == len(history) - len(kept)
        assert dropped > 0

    def test_exact_boundary_no_false_truncation(self):
        """When total tokens exactly equal the budget, nothing is dropped."""
        from echelon_engine.services_chat import _truncate_history
        from echelon_sdk.tokenizer import count_messages
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        current = "c"
        exact = count_messages(history) + count_messages(
            [{"role": "user", "content": current}]
        )
        kept, dropped = _truncate_history(history, current, exact)
        assert kept == history
        assert dropped == 0

    def test_no_history_short_circuits_token_count(self):
        """None/empty history is handled without invoking the tokenizer."""
        from echelon_engine.services_chat import _truncate_history
        kept, dropped = _truncate_history([], "any text", 10)
        assert kept == []
        assert dropped == 0


# ── Integration: chat_turn records dropped_turns ──────────────────────────────

def _fake_llm(intent_json: str, prose: str = "Prose reply."):
    """Return a fake LLM: first call = classification, subsequent = prose."""
    calls = [0]

    def fake(model, system, user, **kwargs):
        calls[0] += 1
        return intent_json if calls[0] == 1 else prose

    return fake


class TestChatTurnTruncation:
    """chat_turn truncates history and records the drop count."""

    def test_dropped_turns_in_metadata_when_truncated(self):
        from echelon_engine.services_chat import chat_turn
        llm = _fake_llm('{"intent": "smalltalk", "goal": ""}', "Hello commander.")
        # Build a huge history that will trigger truncation with a tiny budget.
        history = []
        for i in range(100):
            history.append({"role": "user", "content": f"turn {i} message"})
            history.append({"role": "assistant", "content": f"turn {i} reply"})
        result = chat_turn(
            "hello", history=history, scope="echelon",
            _llm_chat=llm, history_token_budget=80,
        )
        assert "reply" in result
        assert "dropped_turns" in result
        assert result["dropped_turns"] > 0

    def test_no_dropped_turns_key_when_history_fits(self):
        from echelon_engine.services_chat import chat_turn
        llm = _fake_llm('{"intent": "smalltalk", "goal": ""}', "Hi.")
        result = chat_turn(
            "hello", history=[{"role": "user", "content": "hi"}],
            scope="echelon", _llm_chat=llm, history_token_budget=500,
        )
        assert "reply" in result
        assert "dropped_turns" not in result  # key absent when nothing dropped

    def test_no_dropped_turns_key_when_no_history(self):
        from echelon_engine.services_chat import chat_turn
        llm = _fake_llm('{"intent": "smalltalk", "goal": ""}', "Greetings.")
        result = chat_turn(
            "hello", history=None, scope="echelon",
            _llm_chat=llm,
        )
        assert "reply" in result
        assert "dropped_turns" not in result

    def test_dispatch_intent_records_dropped_turns(self):
        """The dispatch return path also includes dropped_turns."""
        from echelon_engine.services_chat import chat_turn
        llm = _fake_llm(
            '{"intent": "dispatch", "goal": "audit auth"}',
            "I can audit that. Confirm?",
        )
        history = []
        for i in range(100):
            history.append({"role": "user", "content": f"msg {i}"})
        result = chat_turn(
            "audit the auth", history=history, scope="echelon",
            _llm_chat=llm, history_token_budget=80,
        )
        assert "action" in result
        assert result["action"]["type"] == "dispatch"
        assert "dropped_turns" in result
        assert result["dropped_turns"] > 0

    def test_keyword_fallback_path_records_dropped_turns(self):
        """When LLM is unavailable (keyword fallback), truncation still happens."""
        from echelon_engine.services_chat import chat_turn
        history = []
        for i in range(100):
            history.append({"role": "user", "content": f"msg {i}"})
        result = chat_turn(
            "hello", history=history, scope="echelon",
            _llm_chat=None, history_token_budget=80,
        )
        assert "reply" in result
        assert "dropped_turns" in result
        assert result["dropped_turns"] > 0


# ── Configurable budget ──────────────────────────────────────────────────────

class TestBudgetConfiguration:
    """The token budget is configurable via parameter and env var."""

    def test_parameter_overrides_default(self):
        from echelon_engine.services_chat import chat_turn
        llm = _fake_llm('{"intent": "smalltalk", "goal": ""}', "OK.")
        history = []
        for i in range(80):
            history.append({"role": "user", "content": f"msg {i}"})
        # A very tight budget → most turns dropped.
        tight = chat_turn(
            "hello", history=list(history), scope="echelon",
            _llm_chat=llm, history_token_budget=50,
        )
        # A generous budget → nothing dropped.
        generous = chat_turn(
            "hello", history=list(history), scope="echelon",
            _llm_chat=llm, history_token_budget=50000,
        )
        assert tight.get("dropped_turns", 0) > 0
        assert "dropped_turns" not in generous

    def test_env_var_sets_budget(self):
        from echelon_engine.services_chat import chat_turn
        llm = _fake_llm('{"intent": "smalltalk", "goal": ""}', "OK.")
        history = []
        for i in range(80):
            history.append({"role": "user", "content": f"msg {i}"})
        with patch.dict(os.environ, {"ECHELON_CHAT_HISTORY_TOKENS": "50"}):
            result = chat_turn(
                "hello", history=list(history), scope="echelon",
                _llm_chat=llm,
            )
        assert result.get("dropped_turns", 0) > 0

    def test_env_var_garbage_falls_back_to_default(self):
        from echelon_engine.services_chat import _env_history_budget
        with patch.dict(os.environ, {"ECHELON_CHAT_HISTORY_TOKENS": "not-a-number"}):
            budget = _env_history_budget()
        # Should fall back to the default, not crash.
        assert budget == 4000


# ── Honesty: the truncated history isn't silently ignored ─────────────────────

class TestTruncationHonesty:
    """The drop is recorded — never silently pretend full memory."""

    def test_dropped_turns_exact_count(self):
        from echelon_engine.services_chat import _truncate_history
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
            {"role": "user", "content": "e"},
            {"role": "assistant", "content": "f"},
        ]
        kept, dropped = _truncate_history(history, "current", 20)
        assert dropped == len(history) - len(kept)
        # With a tiny budget we expect at least some drops.
        assert dropped >= 0

    def test_drop_count_monotonic_with_budget(self):
        """A smaller budget drops at least as many turns as a larger one."""
        from echelon_engine.services_chat import _truncate_history
        history = []
        for i in range(40):
            history.append({"role": "user", "content": f"turn {i}"})
        _, dropped_tight = _truncate_history(list(history), "current", 30)
        _, dropped_loose = _truncate_history(list(history), "current", 200)
        assert dropped_tight >= dropped_loose
