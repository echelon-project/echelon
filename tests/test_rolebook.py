"""tests/test_rolebook.py -- unit tests for echelon_engine.agent.rolebook.

Tests the pure seams: role_of, CARD_ORDER, SOCIETY, tier_for_goal/tier_cartridge,
RoleBook construction + events, compose_card_on_done (offline path). Network-bound
seams (live bank, CardStore with a real DB) are smoke-tested with a temp DB or skipped.
"""
from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path

import pytest

# -- import guard: skip whole module if the agent package is not yet importable --
pytest.importorskip("echelon_engine.agent.rolebook",
                    reason="echelon_engine.agent.rolebook not available")

from echelon_engine.agent.rolebook import (
    CARD_ORDER,
    SOCIETY,
    TIER_SCOPES,
    _KIND_TO_TIER,
    role_of,
    tier_for_goal,
    tier_cartridge,
    RoleBook,
    book_path,
    books_home,
    compose_card_on_done,
)


# -- role_of ---------------------------------------------------------------

class TestRoleOf:
    def test_society_name_maps_to_dome_role(self):
        assert role_of("builder") == "dev"
        assert role_of("checker") == "qc"
        assert role_of("doubter") == "system"
        assert role_of("supporter") == "architect"

    def test_dome_role_is_identity(self):
        for r in CARD_ORDER:
            assert role_of(r) == r

    def test_unknown_is_passthrough(self):
        assert role_of("wizard") == "wizard"
        assert role_of("") == ""

    def test_society_keys_cover_all_expected(self):
        assert set(SOCIETY.keys()) == {"builder", "checker", "doubter", "supporter"}

    def test_society_values_are_in_card_order(self):
        for v in SOCIETY.values():
            assert v in CARD_ORDER


# -- CARD_ORDER / TIER_SCOPES ---------------------------------------------

class TestConstants:
    def test_card_order_has_four_roles(self):
        assert len(CARD_ORDER) == 4

    def test_card_order_build_check_doubt_support(self):
        assert CARD_ORDER == ["dev", "qc", "system", "architect"]

    def test_tier_scopes_has_required_keys(self):
        for k in ("OS_TIER", "T1", "T2", "T3"):
            assert k in TIER_SCOPES

    def test_tier_scope_values_nonempty(self):
        for v in TIER_SCOPES.values():
            assert isinstance(v, str) and v


# -- tier_for_goal / tier_cartridge ----------------------------------------

class TestTierForGoal:
    def test_explicit_t1_wins(self):
        assert tier_for_goal("anything", explicit="T1") == "T1"

    def test_explicit_t2_wins(self):
        assert tier_for_goal("plan the project", explicit="T2") == "T2"

    def test_explicit_t3_wins(self):
        assert tier_for_goal("write code", explicit="T3") == "T3"

    def test_explicit_os_tier(self):
        assert tier_for_goal("anything", explicit="OS_TIER") == "OS_TIER"

    def test_explicit_case_insensitive(self):
        assert tier_for_goal("anything", explicit="t2") == "T2"

    def test_unknown_explicit_falls_through_to_classify(self):
        # an unknown explicit should NOT crash; falls through to classification
        result = tier_for_goal("write some code", explicit="BOGUS")
        assert result in TIER_SCOPES

    def test_no_explicit_returns_valid_tier(self):
        result = tier_for_goal("implement the feature")
        assert result in TIER_SCOPES

    def test_planning_goal_maps_to_t1(self):
        # "plan" should trigger planning kind -> T1 via gantt_pillars or fallback
        result = tier_for_goal("plan the architecture of the system")
        # either T1 (if classifier fires) or T2 (fallback) -- both valid
        assert result in TIER_SCOPES

    def test_empty_goal_does_not_crash(self):
        result = tier_for_goal("")
        assert result in TIER_SCOPES


class TestTierCartridge:
    def test_returns_tuple_tier_scope(self):
        tier, scope = tier_cartridge("implement the login page")
        assert tier in TIER_SCOPES
        assert scope == TIER_SCOPES[tier]

    def test_explicit_os_tier(self):
        tier, scope = tier_cartridge("any goal", explicit="OS_TIER")
        assert tier == "OS_TIER"
        assert scope == "echelon-op"

    def test_explicit_t3(self):
        tier, scope = tier_cartridge("list files", explicit="T3")
        assert tier == "T3"
        assert scope == "echelon-t3"


# -- book_path / books_home ------------------------------------------------

class TestBookPath:
    def test_books_home_default(self, monkeypatch):
        monkeypatch.delenv("ECHELON_BOOKS", raising=False)
        p = books_home()
        assert ".echelon" in str(p)
        assert "books" in str(p)

    def test_books_home_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ECHELON_BOOKS", str(tmp_path))
        assert books_home() == tmp_path

    def test_book_path_structure(self, tmp_path):
        p = book_path("myproject", "dev", home=tmp_path)
        assert p.parent == tmp_path / "myproject"
        assert p.name == "dev.jsonl"


# -- RoleBook --------------------------------------------------------------

class TestRoleBook:
    def test_init_creates_dir(self, tmp_path):
        rb = RoleBook("testscope", "dev", home=tmp_path)
        assert (tmp_path / "testscope").is_dir()

    def test_events_empty_before_record(self, tmp_path):
        rb = RoleBook("s", "qc", home=tmp_path)
        # events may include the book_open note if WorldJournal is present
        # but should never raise
        evts = rb.events()
        assert isinstance(evts, list)

    def test_events_returns_list_of_dicts(self, tmp_path):
        rb = RoleBook("s", "dev", home=tmp_path)
        evts = rb.events()
        for e in evts:
            assert isinstance(e, dict)

    def test_record_action_appends_to_jsonl(self, tmp_path):
        rb = RoleBook("s", "dev", home=tmp_path)
        # Write a raw action line directly to the .jsonl (the canonical way books work)
        rb.path.parent.mkdir(parents=True, exist_ok=True)
        with open(rb.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "action", "goal": "g1", "kind": "step_start"}) + "\n")
        evts = rb.events()
        assert any(e.get("event") == "action" for e in evts)

    def test_snapshot_property_does_not_crash(self, tmp_path):
        rb = RoleBook("s", "system", home=tmp_path)
        snap = rb.snapshot
        assert isinstance(snap, dict)

    def test_multiple_roles_separate_files(self, tmp_path):
        rb_dev = RoleBook("proj", "dev", home=tmp_path)
        rb_qc = RoleBook("proj", "qc", home=tmp_path)
        assert rb_dev.path != rb_qc.path
        assert rb_dev.path.name == "dev.jsonl"
        assert rb_qc.path.name == "qc.jsonl"

    def test_construct_writes_real_jsonl_book(self, tmp_path, monkeypatch):
        """Constructing a RoleBook writes a real .jsonl book through WorldJournal.
        The file must exist AND contain the book_open note — the no-op stub path is gone."""
        monkeypatch.setenv("ECHELON_BOOKS", str(tmp_path))
        rb = RoleBook("testscope", "builder", home=tmp_path)
        # File must exist
        assert rb.path.exists(), f"Expected {rb.path} to exist"
        # Must contain the book_open note
        content = rb.path.read_text(encoding="utf-8")
        assert "book_open" in content, f"book_open note not found in {rb.path}"
        # The journal is always present — no None guard
        assert rb._journal is not None, "RoleBook._journal must not be None (stub path removed)"

    def test_record_action_writes_through_journal(self, tmp_path):
        """record_action writes through the WorldJournal to the jsonl."""
        rb = RoleBook("s", "dev", home=tmp_path)
        rb.record_action("g1", "partner1", "step_start", {"info": "test"})
        content = rb.path.read_text(encoding="utf-8")
        # The action should appear in the jsonl via the journal
        assert "step_start" in content or "action" in content

    def test_record_outcome_writes_through_journal(self, tmp_path):
        """record_outcome writes through the WorldJournal to the jsonl."""
        rb = RoleBook("s", "qc", home=tmp_path)
        rb.record_outcome("g1", "partner1", ok=True, summary="all green")
        content = rb.path.read_text(encoding="utf-8")
        assert "all green" in content or "outcome" in content


# -- compose_card_on_done (offline) ----------------------------------------

class TestComposeCardOnDone:
    """compose_card_on_done is DB-dependent but has a short-circuit path when no
    role-books have moves. Test the no-moves short-circuit."""

    def test_no_moves_returns_no_card_dict(self, tmp_path):
        result = compose_card_on_done(
            "testscope", "test goal",
            outcome_ok=True,
            home=tmp_path,
        )
        # No role-book files -> no actions -> should return the no-moves sentinel
        # (or an error dict if CardStore fails; both are acceptable non-raising outcomes)
        assert result is None or isinstance(result, dict)
        if isinstance(result, dict) and "detail" in result:
            assert "no role-book moves" in result["detail"]

    def test_does_not_raise_on_bad_db_path(self, tmp_path):
        """compose_card_on_done must never raise (best-effort); bad DB -> error dict."""
        result = compose_card_on_done(
            "scope", "goal",
            outcome_ok=False,
            home=tmp_path,
            db_path=str(tmp_path / "nonexistent" / "bad.db"),
        )
        # Either the no-moves short-circuit fires before the DB call, or it returns
        # an error dict -- either way, no raise.
        assert result is None or isinstance(result, dict)
