"""Tests for echelon_engine.agent.loop_models — MemoryContext and AgentResult.

Pure data-class tests: construction, defaults, field types. No model call or
database needed — these are value objects.
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock

from echelon_engine.agent.loop_models import MemoryContext, AgentResult


# ── AgentResult ──────────────────────────────────────────────────────────────

class TestAgentResult:
    def test_required_fields(self):
        r = AgentResult("completed", "done", 3)
        assert r.status == "completed"
        assert r.answer == "done"
        assert r.steps == 3

    def test_token_defaults_zero(self):
        r = AgentResult("completed", "done", 1)
        assert r.tokens_in == 0
        assert r.tokens_out == 0

    def test_transcript_default_empty_list(self):
        r = AgentResult("completed", "done", 1)
        assert r.transcript == []

    def test_warmth_trace_default_empty_list(self):
        r = AgentResult("completed", "done", 1)
        assert r.warmth_trace == []

    def test_woke_default_none(self):
        r = AgentResult("completed", "done", 1)
        assert r.woke is None

    def test_transcript_is_independent_per_instance(self):
        """Default factory: two instances must NOT share the same list object."""
        r1 = AgentResult("completed", "done", 1)
        r2 = AgentResult("blocked", "stuck", 2)
        r1.transcript.append({"x": 1})
        assert r2.transcript == []

    def test_warmth_trace_is_independent_per_instance(self):
        r1 = AgentResult("completed", "done", 1)
        r2 = AgentResult("blocked", "stuck", 2)
        r1.warmth_trace.append({"step": 1, "score": 0.9})
        assert r2.warmth_trace == []

    def test_status_variants(self):
        for status in ("completed", "blocked", "timeout", "error", "stopped", "reboot", "done"):
            r = AgentResult(status, "", 0)
            assert r.status == status

    def test_full_construction(self):
        tr = [{"role": "tool", "result": "ok"}]
        wt = [{"step": 1, "score": 0.8, "verdict": "warm"}]
        r = AgentResult("completed", "answer", 5, 100, 50, tr, wt, woke=True)
        assert r.tokens_in == 100
        assert r.tokens_out == 50
        assert r.woke is True
        assert r.transcript is tr
        assert r.warmth_trace is wt


# ── MemoryContext ─────────────────────────────────────────────────────────────

class TestMemoryContext:
    def _make_store(self):
        """A minimal mock that satisfies MemoryContext's type annotation."""
        return MagicMock()

    def test_required_fields(self):
        store = self._make_store()
        mc = MemoryContext(store=store, scope="test")
        assert mc.store is store
        assert mc.scope == "test"

    def test_auto_seed_default_true(self):
        mc = MemoryContext(store=self._make_store(), scope="s")
        assert mc.auto_seed is True

    def test_auto_seed_can_be_disabled(self):
        mc = MemoryContext(store=self._make_store(), scope="s", auto_seed=False)
        assert mc.auto_seed is False

    def test_judge_provider_default_none(self):
        mc = MemoryContext(store=self._make_store(), scope="s")
        assert mc.judge_provider is None

    def test_judge_model_default(self):
        mc = MemoryContext(store=self._make_store(), scope="s")
        assert mc.judge_model == "grok-4.3"

    def test_bank_default_none(self):
        mc = MemoryContext(store=self._make_store(), scope="s")
        assert mc.bank is None

    def test_bank_semantic_default_none(self):
        mc = MemoryContext(store=self._make_store(), scope="s")
        assert mc.bank_semantic is None

    def test_confirmation_default_none(self):
        mc = MemoryContext(store=self._make_store(), scope="s")
        assert mc.confirmation is None

    def test_all_optional_fields(self):
        judge = MagicMock()
        bank = MagicMock()
        sem = MagicMock()
        conf = MagicMock()
        mc = MemoryContext(
            store=self._make_store(), scope="echelon",
            auto_seed=False,
            judge_provider=judge,
            judge_model="deepseek-chat",
            bank=bank,
            bank_semantic=sem,
            confirmation=conf,
        )
        assert mc.auto_seed is False
        assert mc.judge_provider is judge
        assert mc.judge_model == "deepseek-chat"
        assert mc.bank is bank
        assert mc.bank_semantic is sem
        assert mc.confirmation is conf
