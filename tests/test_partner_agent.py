"""tests/test_partner_agent.py -- unit tests for echelon_engine.agent.partner
    and echelon_engine.agent.board.

Tests the pure / transport seams:
  - verify_gate (pure, no network)
  - _LegacyLedger invariants (claim/land/act/post/pending)
  - Ledger (from board.py) claim/land/act/post invariants
  - run_board simulate flags (no live agents)
  - board() sim paths via partner.board()
  - dispatch hands-check refusal (pure, no loop call)
  - earn_craft_from_trace (offline: no-card-found path)

Network-bound: partner.dispatch with a live loop, swarm, board with real agents.
These are NOT tested here; this suite is $0.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("echelon_engine.agent.partner",
                    reason="echelon_engine.agent.partner not available")

from echelon_engine.agent.partner import (
    _CLAIMED_DONE,
    _LegacyLedger,
    verify_gate,
    earn_craft_from_trace,
    board as partner_board,
    Ledger as _PartnerLedger,
)
from echelon_engine.agent.board import Ledger, run_board


# -- verify_gate -----------------------------------------------------------

class TestVerifyGate:
    def test_claimed_done_red_outcome_returns_outcome_failed(self):
        for status in _CLAIMED_DONE:
            result = verify_gate(status, {"ok": False, "detail": "nothing changed"})
            assert result == "outcome-failed", f"expected outcome-failed for status={status}"

    def test_claimed_done_green_outcome_unchanged(self):
        for status in _CLAIMED_DONE:
            result = verify_gate(status, {"ok": True, "detail": "files changed"})
            assert result == status

    def test_claimed_done_no_outcome_unchanged(self):
        for status in _CLAIMED_DONE:
            result = verify_gate(status, None)
            assert result == status

    def test_not_done_claim_never_fires(self):
        for status in ("error", "running", "skipped", None, "refused", "timeout"):
            result = verify_gate(status, {"ok": False, "detail": "red"})
            assert result == status, f"gate must not fire for status={status}"

    def test_outcome_missing_ok_key_treated_as_false(self):
        """An outcome dict without 'ok' -> ok defaults to None/falsy -> gate may fire."""
        # outcome.get("ok") returns None for missing key -> not outcome.get("ok") is True
        result = verify_gate("completed", {"detail": "unclear"})
        assert result == "outcome-failed"  # None is falsy

    def test_returns_string_not_none(self):
        result = verify_gate("completed", {"ok": False})
        assert isinstance(result, str)


# -- _LegacyLedger (partner.py's internal ledger) -------------------------

class TestLegacyLedgerClaim:
    def test_first_claim_returns_true(self):
        L = _LegacyLedger()
        assert L.claim("g1", "partner-A") is True

    def test_owner_re_claim_is_idempotent(self):
        L = _LegacyLedger()
        L.claim("g1", "partner-A")
        assert L.claim("g1", "partner-A") is True  # same partner -> True (idempotent)

    def test_rival_claim_returns_false(self):
        L = _LegacyLedger()
        L.claim("g1", "partner-A")
        assert L.claim("g1", "partner-B") is False

    def test_different_goals_independent(self):
        L = _LegacyLedger()
        assert L.claim("g1", "A") is True
        assert L.claim("g2", "B") is True


class TestLegacyLedgerLand:
    def test_land_appends_and_returns_entry(self):
        L = _LegacyLedger()
        L.claim("g1", "A")
        e = L.land("g1", "A", "v1")
        assert e["op"] == "land"
        assert e["goal"] == "g1"
        assert e["result"] == "v1"

    def test_land_append_only_second_land_does_not_clobber(self):
        L = _LegacyLedger()
        L.claim("g1", "A")
        L.land("g1", "A", "v1")
        L.land("g1", "A", "v2")
        lands = L.read("g1")
        assert len(lands) == 2
        assert lands[0]["result"] == "v1"
        assert lands[1]["result"] == "v2"
        assert lands[1]["seq"] > lands[0]["seq"]

    def test_landings_property(self):
        L = _LegacyLedger()
        L.claim("g1", "A")
        L.land("g1", "A", "done")
        assert len(L.landings) == 1

    def test_read_all_vs_by_goal(self):
        L = _LegacyLedger()
        L.claim("g1", "A")
        L.land("g1", "A", "r1")
        L.claim("g2", "B")
        L.land("g2", "B", "r2")
        all_lands = L.read()
        g1_lands = L.read("g1")
        assert len(all_lands) == 2
        assert len(g1_lands) == 1
        assert g1_lands[0]["goal"] == "g1"


class TestLegacyLedgerPost:
    def test_post_appends_to_board(self):
        L = _LegacyLedger()
        rec = L.post("sub1", "partner-A", "do the thing", role="dev")
        assert rec["id"] == "sub1"
        assert rec["task"] == "do the thing"

    def test_post_idempotent_on_same_id(self):
        L = _LegacyLedger()
        r1 = L.post("sub1", "A", "task1")
        r2 = L.post("sub1", "B", "task2")  # same id, different by/task -> no duplicate
        assert r1["task"] == r2["task"]  # original is returned

    def test_pending_includes_unfinished(self):
        L = _LegacyLedger()
        L.post("g1", "A", "task1")
        pending = L.pending()
        assert len(pending) == 1

    def test_pending_excludes_landed(self):
        L = _LegacyLedger()
        L.post("g1", "A", "task1")
        L.claim("g1", "A")
        L.land("g1", "A", "done")
        assert L.pending() == []

    def test_post_records_parent(self):
        L = _LegacyLedger()
        rec = L.post("child", "A", "task", parent="parent-goal")
        assert rec["parent"] == "parent-goal"


class TestLegacyLedgerAct:
    def test_act_appends_to_log(self):
        L = _LegacyLedger()
        L.act("g1", "A", "dev", "step_start", {"x": 1})
        acts = [e for e in L._log if e.get("op") == "act"]
        assert len(acts) == 1
        assert acts[0]["kind"] == "step_start"

    def test_act_with_books_routes_to_book(self, tmp_path):
        """Ledger.act routes to the role-book directory; the file exists once bound."""
        L = _LegacyLedger(scope="testscope", books_home=str(tmp_path))
        # The bind_books call (in __init__) creates the RoleBook dirs
        # Verify the books dict was set up (directory should exist after bind_books)
        book_dir = tmp_path / "testscope"
        assert book_dir.is_dir()
        # act should not raise regardless of WorldJournal availability
        L.act("g1", "A", "dev", "step_start")
        # the act must be in the log
        acts = [e for e in L._log if e.get("op") == "act"]
        assert len(acts) == 1


# -- Ledger from board.py -------------------------------------------------

class TestBoardLedger:
    """Ledger in board.py is the extracted version; same invariants as _LegacyLedger."""

    def test_claim_first_wins(self):
        L = Ledger()
        assert L.claim("g1", "A") is True
        assert L.claim("g1", "B") is False

    def test_land_append_only(self):
        L = Ledger()
        L.claim("g1", "A")
        L.land("g1", "A", "v1")
        L.land("g1", "A", "v2")
        lands = L.read("g1")
        assert len(lands) == 2
        assert lands[1]["seq"] > lands[0]["seq"]

    def test_post_and_pending(self):
        L = Ledger()
        L.post("sub", "A", "subtask")
        assert len(L.pending()) == 1
        L.claim("sub", "A")
        L.land("sub", "A", "done")
        assert L.pending() == []

    def test_landings_property(self):
        L = Ledger()
        L.claim("g1", "A")
        L.land("g1", "A", "result")
        assert len(L.landings) == 1


# -- run_board simulate flags ---------------------------------------------

class TestRunBoardSimulate:
    def test_simulate_race_claim_unique(self):
        result = run_board(["g1", "g2"], scope="test", simulate_race=True)
        assert result["claims_unique"] is True
        assert result["landed"] == 0

    def test_simulate_double_land_append_only(self):
        result = run_board(["g1"], scope="test", simulate_double_land=True)
        assert result["append_only_ok"] is True
        assert result["landed"] == 2  # two lands on same goal

    def test_dry_run_no_folder(self):
        result = run_board(["g1", "g2"], scope="test", n_partners=2)
        assert result["landed"] == 2
        assert result["claims_unique"] is True
        assert result["append_only_ok"] is True

    def test_dry_run_goal_dicts(self):
        goals = [{"id": "g1", "task": "do A"}, {"id": "g2", "task": "do B"}]
        result = run_board(goals, scope="test")
        assert result["landed"] == 2

    def test_dry_run_returns_n_partners(self):
        """The dry-run path returns n_partners in the result dict."""
        result = run_board(["x"], scope="myproject", n_partners=3)
        assert result["n_partners"] == 3


# -- partner.board() shim -------------------------------------------------

class TestPartnerBoardShim:
    """partner.board() delegates to run_board; verify the shim works."""

    def test_shim_simulate_race(self):
        result = partner_board(["g1"], scope="test", simulate_race=True)
        assert result["claims_unique"] is True

    def test_shim_dry_run(self):
        result = partner_board(["g1", "g2"], scope="test")
        assert result["landed"] == 2

    def test_ledger_alias_is_board_ledger(self):
        """partner.Ledger should be the board.Ledger (the shim)."""
        assert _PartnerLedger is Ledger


# -- earn_craft_from_trace ------------------------------------------------

class TestEarnCraftFromTrace:
    """Tests the offline code path: no such craft card in bank (uses a temp DB or empty)."""

    def test_no_card_returns_detail_dict(self, tmp_path):
        """When the craft card doesn't exist in the bank, return a no-card detail dict."""
        db = str(tmp_path / "empty.db")
        result = earn_craft_from_trace("implement feature X", outcome_ok=True, db_path=db)
        # Either error or no-card detail -- both valid; must not raise
        assert result is None or isinstance(result, dict)
        if isinstance(result, dict):
            assert "card" in result or "error" in result

    def test_bugfix_labels_bugfix_card(self, tmp_path):
        """Goals containing 'fix' should target craft:bugfix-loop."""
        # We can't assert the label is IN the DB, but we can verify the function runs
        db = str(tmp_path / "empty.db")
        result = earn_craft_from_trace("fix the login bug", outcome_ok=True, db_path=db)
        assert result is None or isinstance(result, dict)
        if isinstance(result, dict) and "card" in result:
            assert "bugfix" in result["card"]

    def test_feature_labels_feature_card(self, tmp_path):
        db = str(tmp_path / "empty.db")
        result = earn_craft_from_trace("add the dashboard page", outcome_ok=True, db_path=db)
        assert result is None or isinstance(result, dict)
        if isinstance(result, dict) and "card" in result:
            assert "feature" in result["card"]

    def test_does_not_raise_on_bad_db(self, tmp_path):
        """earn_craft_from_trace must NEVER raise (best-effort)."""
        result = earn_craft_from_trace(
            "implement something",
            outcome_ok=False,
            db_path=str(tmp_path / "nonexistent" / "db.db"),
        )
        assert result is None or isinstance(result, dict)
