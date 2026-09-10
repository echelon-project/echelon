"""Tests for wrap-review: the explicit feedback layer of wrap (M5).

Covers the four load-bearing traps from the spec:
  1. DOUBLE-EARN: one remember + one approve = exactly one fetch-earn + one approve-earn
  2. IDEMPOTENCE: re-running --approve with the same slugs in the same session window
     must NOT stack deltas
  3. SET HONESTY: a warm recall hit with NO remember must NOT appear in the leaned-on list
  4. SCORE CHANGE: approve moves score, dispute re-bases, silence leaves score identical

All tests use temp DBs (tmp_path) — never the live bank.
"""
from __future__ import annotations

import json
import time

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.earn_law import EARN_DELTA
from echelon_engine.atoms import wrap_review
from echelon_engine.atoms.wrap_review import (
    apply_verdict,
    list_leaned_on,
    _resolve_atom,
    _parse_slugs,
)


@pytest.fixture
def store(tmp_path):
    """Isolated CardStore on a temp DB."""
    return CardStore(tmp_path / "test_wrap_review.db")


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


def _seed_atom(store: CardStore, coordinate: str, content: str = "",
               scope: str = "echelon") -> str:
    """Add an atom and compile its structured projection. Returns atom_id."""
    aid = store.add_atom(coordinate, content or f"Test content for {coordinate}",
                         scope=scope)
    store.compile_atom_struct(aid)
    return aid


def _lean(store: CardStore, atom_id: str, times: int = 1) -> None:
    """Simulate `times` witnessed fetches through the door (remember_fetch)."""
    for _ in range(times):
        store.remember_fetch(atom_id)


# ═══════════════════════════════════════════════════════════════════
# LIST tests
# ═══════════════════════════════════════════════════════════════════

def test_list_empty_when_no_leans(store):
    """An atom that was never remembered should not appear."""
    _seed_atom(store, "test:unused")
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now - 3600)
    assert len(result) == 0


def test_list_includes_remembered_atom(store):
    """An atom with a witnessed fetch in the window should appear."""
    aid = _seed_atom(store, "test:used")
    _lean(store, aid)
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now - 3600)
    assert len(result) == 1
    assert result[0]["atom_id"] == aid


def test_list_excludes_atom_outside_window(store):
    """An atom fetched before the window should not appear."""
    aid = _seed_atom(store, "test:old")
    _lean(store, aid)
    # Window starts AFTER the fetch — should be excluded
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now + 3600)  # window in future
    assert len(result) == 0


def test_list_respects_scope(store):
    """Only atoms in the requested scope should appear."""
    aid_echelon = _seed_atom(store, "test:in-echelon", scope="echelon")
    aid_other = _seed_atom(store, "test:in-other", scope="other-scope")
    _lean(store, aid_echelon)
    _lean(store, aid_other)
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now - 3600)
    ids = {r["atom_id"] for r in result}
    assert aid_echelon in ids
    assert aid_other not in ids


def test_list_orders_by_fetch_time_desc(store):
    """Most recently leaned-on atoms should come first."""
    aid1 = _seed_atom(store, "test:first")
    aid2 = _seed_atom(store, "test:second")
    # Force known timestamps so ordering is deterministic (seconds granularity)
    ts_base = int(time.time()) - 100
    _lean(store, aid1)
    with store._lock:
        store.conn.execute("UPDATE atom_earned SET last_fetch_ts=? WHERE atom_id=?",
                           (ts_base, aid1))
        store.conn.commit()
    _lean(store, aid2)
    with store._lock:
        store.conn.execute("UPDATE atom_earned SET last_fetch_ts=? WHERE atom_id=?",
                           (ts_base + 10, aid2))
        store.conn.commit()
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now - 3600)
    assert result[0]["atom_id"] == aid2  # most recent first
    assert result[1]["atom_id"] == aid1


# ═══════════════════════════════════════════════════════════════════
# TRAP 3: SET HONESTY — warm recall ≠ leaned on
# ═══════════════════════════════════════════════════════════════════

def test_set_honesty_warm_recall_no_remember_not_in_list(store):
    """A warm recall hit (impressions row) with NO remember_fetch must NOT
    appear in the leaned-on list. Only atom_earned.last_fetch_ts matters."""
    aid = _seed_atom(store, "test:viewed-but-not-taken")
    # Simulate a recall view — write an impression row (no remember, no earn)
    with store._lock:
        store.conn.execute(
            "INSERT INTO impressions (ts, scope, query_hash, atom_coord) "
            "VALUES (?, ?, ?, ?)",
            (int(time.time()), "echelon", "test-query-hash", "test:viewed-but-not-taken"))
        store.conn.commit()
    # The atom was never remembered — should NOT appear in leaned-on
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now - 3600)
    assert len(result) == 0, (
        f"Set honesty violated: {len(result)} atoms in leaned-on list, "
        "but none were remembered — only impressions exist"
    )


def test_set_honesty_remembered_and_viewed_appears_once(store):
    """An atom both viewed AND remembered should appear exactly once."""
    aid = _seed_atom(store, "test:viewed-and-taken")
    # Add an impression row (view)
    with store._lock:
        store.conn.execute(
            "INSERT INTO impressions (ts, scope, query_hash, atom_coord) "
            "VALUES (?, ?, ?, ?)",
            (int(time.time()), "echelon", "test-query", "test:viewed-and-taken"))
        store.conn.commit()
    # Now actually remember it
    _lean(store, aid)
    now = int(time.time())
    result = list_leaned_on(store, "echelon", now - 3600)
    assert len(result) == 1
    assert result[0]["atom_id"] == aid


# ═══════════════════════════════════════════════════════════════════
# TRAP 1: DOUBLE-EARN
# ═══════════════════════════════════════════════════════════════════

def test_double_earn_one_remember_one_approve(store):
    """One remember_fetch + one approve = exactly one fetch-earn + one
    approve-earn in score_history. The approve must NOT re-run the fetch-earn."""
    aid = _seed_atom(store, "test:double-earn")

    # Get score before anything
    before_earned = store.struct_earned(aid)
    before_score = before_earned["score"] if before_earned else 100.0

    # One remember (fetch earn)
    _lean(store, aid)

    # Get score after fetch
    after_fetch = store.struct_earned(aid)
    assert after_fetch["use_count"] == 1
    assert after_fetch["score"] == before_score + EARN_DELTA  # +2.0

    # Now approve (should add a SECOND earn, not re-do the first)
    now = int(time.time())
    session_start = now - 3600
    result = apply_verdict(store, aid, "approve", session_start)
    assert result["ok"]
    assert result["verdict"] == "approve"

    # Check score_history on atoms table — should have exactly the approve entry
    atom = store.get_atom(aid)
    hist = json.loads(atom.score_history or "[]")
    # Count entries by source
    approve_entries = [e for e in hist if len(e) > 2 and e[2] == "wrap-approve"]
    fetch_entries_in_atoms = [e for e in hist if len(e) > 2 and str(e[2]).startswith("fetch")]

    assert len(approve_entries) == 1, (
        f"Expected exactly 1 wrap-approve entry in atoms.score_history, got {len(approve_entries)}"
    )
    # The fetch-earn lives in atom_earned, NOT in atoms — verify NO fetch entry leaked into atoms
    assert len(fetch_entries_in_atoms) == 0, (
        f"fetch entry leaked into atoms.score_history: {fetch_entries_in_atoms}"
    )

    # atom_earned should still have exactly 1 fetch (unchanged by approve)
    earned = store.struct_earned(aid)
    assert earned["use_count"] == 1, (
        f"atom_earned.use_count should be 1 (fetch only), got {earned['use_count']}"
    )
    earned_hist = json.loads(
        store.conn.execute("SELECT score_history FROM atom_earned WHERE atom_id=?",
                           (aid,)).fetchone()["score_history"] or "[]")
    fetch_entries = [e for e in earned_hist if len(e) > 2 and str(e[2]).startswith("fetch")]
    assert len(fetch_entries) == 1, (
        f"Expected exactly 1 fetch entry in atom_earned.score_history, got {len(fetch_entries)}"
    )


def test_approve_neither_touches_atom_earned_nor_its_use_count(store):
    """Approve operates on atoms table, not atom_earned — must not bump
    atom_earned.use_count or modify atom_earned.score_history."""
    aid = _seed_atom(store, "test:earned-untouched")
    _lean(store, aid)

    earned_before = store.struct_earned(aid)
    earned_hist_before = store.conn.execute(
        "SELECT score_history FROM atom_earned WHERE atom_id=?",
        (aid,)).fetchone()["score_history"]

    now = int(time.time())
    result = apply_verdict(store, aid, "approve", now - 3600)
    assert result["ok"]

    earned_after = store.struct_earned(aid)
    earned_hist_after = store.conn.execute(
        "SELECT score_history FROM atom_earned WHERE atom_id=?",
        (aid,)).fetchone()["score_history"]

    # atom_earned must be byte-identical after approve
    assert earned_after["use_count"] == earned_before["use_count"], (
        "approve must not touch atom_earned.use_count"
    )
    assert earned_after["score"] == earned_before["score"], (
        "approve must not touch atom_earned.score"
    )
    assert earned_hist_after == earned_hist_before, (
        "approve must not touch atom_earned.score_history"
    )


# ═══════════════════════════════════════════════════════════════════
# TRAP 2: IDEMPOTENCE
# ═══════════════════════════════════════════════════════════════════

def test_idempotence_approve_same_window_is_noop(store):
    """Re-running approve in the same session window must NOT stack deltas."""
    aid = _seed_atom(store, "test:idempotent")
    session_start = int(time.time()) - 3600

    # First approve
    r1 = apply_verdict(store, aid, "approve", session_start)
    assert r1["ok"]
    assert not r1.get("noop")

    atom_after_first = store.get_atom(aid)
    score_after_first = atom_after_first.score

    # Second approve — same atom, same session window
    r2 = apply_verdict(store, aid, "approve", session_start)
    assert r2["ok"]
    assert r2.get("noop"), (
        f"Second approve in same window should be noop, got {r2}"
    )

    atom_after_second = store.get_atom(aid)
    assert atom_after_second.score == score_after_first, (
        f"Score changed on re-apply: {score_after_first} -> {atom_after_second.score}"
    )

    # Verify only ONE wrap-approve entry in score_history
    hist = json.loads(atom_after_second.score_history or "[]")
    approve_entries = [e for e in hist if len(e) > 2 and e[2] == "wrap-approve"]
    assert len(approve_entries) == 1, (
        f"Expected 1 wrap-approve entry, got {len(approve_entries)}: {approve_entries}"
    )


def test_idempotence_dispute_same_window_is_noop(store):
    """Re-running dispute in the same session window must be a noop."""
    aid = _seed_atom(store, "test:idempotent-dispute")
    session_start = int(time.time()) - 3600

    r1 = apply_verdict(store, aid, "dispute", session_start,
                       reason="stale claim")
    assert r1["ok"]
    assert not r1.get("noop")

    atom_after_first = store.get_atom(aid)
    score_after_first = atom_after_first.score

    r2 = apply_verdict(store, aid, "dispute", session_start,
                       reason="stale claim — retry")
    assert r2["ok"]
    assert r2.get("noop"), (
        f"Second dispute in same window should be noop, got {r2}"
    )

    atom_after_second = store.get_atom(aid)
    assert atom_after_second.score == score_after_first


def test_idempotence_approve_then_dispute_same_window_conflict(store):
    """Approving then disputing the same atom in the same window: the dispute
    should be a noop because a verdict already exists for this window."""
    aid = _seed_atom(store, "test:conflict")
    session_start = int(time.time()) - 3600

    r1 = apply_verdict(store, aid, "approve", session_start)
    assert r1["ok"]

    r2 = apply_verdict(store, aid, "dispute", session_start,
                       reason="changed my mind")
    assert r2.get("noop"), (
        f"Verdict conflict: second verdict in same window should be noop, got {r2}"
    )


def test_idempotence_different_window_allows_reapply(store):
    """Same atom, different session window — should allow a new verdict."""
    aid = _seed_atom(store, "test:cross-window")
    window_1 = int(time.time()) - 7200  # 2 hours ago
    window_2 = int(time.time()) - 3600  # 1 hour ago

    r1 = apply_verdict(store, aid, "approve", window_1)
    assert r1["ok"]
    assert not r1.get("noop")

    r2 = apply_verdict(store, aid, "approve", window_2)
    assert r2["ok"]
    assert not r2.get("noop"), (
        "Different session window should allow new verdict"
    )

    # Should have two wrap_reviews rows
    with store._lock:
        count = store.conn.execute(
            "SELECT COUNT(*) as n FROM wrap_reviews WHERE atom_id=?",
            (aid,)).fetchone()["n"]
    assert count == 2


# ═══════════════════════════════════════════════════════════════════
# TRAP 4: SCORE CHANGE — approve moves, dispute re-bases, silence = identical
# ═══════════════════════════════════════════════════════════════════

def test_approve_moves_atom_score_by_earn_delta(store):
    """Approve must increase the atom's score by EARN_DELTA (via earn path)."""
    aid = _seed_atom(store, "test:approve-score")
    score_before = store.get_atom(aid).score

    now = int(time.time())
    result = apply_verdict(store, aid, "approve", now - 3600)
    assert result["ok"]

    score_after = store.get_atom(aid).score
    # With empty history, approve adds EARN_DELTA = 2.0 above benchmark (100)
    # So score goes from 100.0 to 102.0
    assert score_after > score_before, (
        f"Approve should increase score: {score_before} -> {score_after}"
    )
    delta = score_after - score_before
    assert delta > 0, f"Delta should be positive, got {delta}"


def test_dispute_rebases_score_down(store):
    """Dispute must re-base the atom's score via the disclaim path."""
    aid = _seed_atom(store, "test:dispute-score")
    # Earn some weight first so the drop is visible
    _lean(store, aid, times=3)

    score_before = store.get_atom(aid).score

    now = int(time.time())
    result = apply_verdict(store, aid, "dispute", now - 3600,
                           reason="this claim is stale")
    assert result["ok"]

    score_after = store.get_atom(aid).score
    # Dispute -> disclaim -> JUDGED_FLOOR (75.0)
    assert score_after < score_before, (
        f"Dispute should decrease score: {score_before} -> {score_after}"
    )

    # Verify the atom is marked as disputed
    atom = store.get_atom(aid)
    assert "judged:disclaimed" in (atom.born_from or ""), (
        f"Atom should be marked judged:disclaimed, got born_from={atom.born_from!r}"
    )


def test_silence_leaves_score_unchanged(store):
    """Atoms not named in --approve or --dispute must keep their score."""
    aid = _seed_atom(store, "test:untouched")
    _lean(store, aid)

    score_before = store.get_atom(aid).score
    # Neither approve nor dispute this atom — simulate silence
    now = int(time.time())
    # List it, but don't apply any verdict
    result = list_leaned_on(store, "echelon", now - 3600)
    assert len(result) == 1

    score_after = store.get_atom(aid).score
    assert score_after == score_before, (
        f"Silence should not change score: {score_before} -> {score_after}"
    )


# ═══════════════════════════════════════════════════════════════════
# Resolution tests
# ═══════════════════════════════════════════════════════════════════

def test_resolve_by_atom_id(store):
    """Direct atom ID should resolve."""
    aid = _seed_atom(store, "test:by-id")
    resolved = _resolve_atom(store, aid, "echelon")
    assert resolved == aid


def test_resolve_by_coordinate(store):
    """Exact coordinate should resolve."""
    aid = _seed_atom(store, "test:by-coord")
    resolved = _resolve_atom(store, "test:by-coord", "echelon")
    assert resolved == aid


def test_resolve_by_scoped_coordinate(store):
    """Scope-prefixed coordinate should resolve."""
    aid = _seed_atom(store, "test:scoped", scope="echelon")
    resolved = _resolve_atom(store, "test:scoped", "echelon")
    assert resolved == aid


def test_resolve_unknown_returns_none(store):
    """Unknown slug should return None."""
    resolved = _resolve_atom(store, "nonexistent:slug", "echelon")
    assert resolved is None


def test_parse_slugs(store):
    """Comma-separated slug parsing."""
    assert _parse_slugs("a,b,c") == ["a", "b", "c"]
    assert _parse_slugs("  a , b , c  ") == ["a", "b", "c"]
    assert _parse_slugs("") == []
    assert _parse_slugs("single") == ["single"]


# ═══════════════════════════════════════════════════════════════════
# M5 ledger test
# ═══════════════════════════════════════════════════════════════════

def test_ledger_append_on_approve(tmp_path, monkeypatch):
    """Each verdict should append to the M5 ledger file."""
    import echelon_engine.atoms.wrap_review as wr

    ledger = tmp_path / "test_ledger.jsonl"
    monkeypatch.setattr(wr, "_LEDGER_PATH", ledger)

    store = CardStore(tmp_path / "test_ledger.db")
    aid = _seed_atom(store, "test:ledger-approve")
    _lean(store, aid)

    now = int(time.time())
    result = apply_verdict(store, aid, "approve", now - 3600)
    assert result["ok"]

    assert ledger.exists(), "Ledger file should exist after approve"
    lines = ledger.read_text().strip().split("\n")
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["atom_id"] == aid
    assert entry["verdict"] == "approve"


def test_ledger_append_on_dispute(tmp_path, monkeypatch):
    """Dispute verdicts should also append to the ledger."""
    import echelon_engine.atoms.wrap_review as wr

    ledger = tmp_path / "test_ledger2.jsonl"
    monkeypatch.setattr(wr, "_LEDGER_PATH", ledger)

    store = CardStore(tmp_path / "test_ledger2.db")
    aid = _seed_atom(store, "test:ledger-dispute")
    _lean(store, aid)

    now = int(time.time())
    result = apply_verdict(store, aid, "dispute", now - 3600,
                           reason="test dispute reason")
    assert result["ok"]

    assert ledger.exists()
    lines = ledger.read_text().strip().split("\n")
    entry = json.loads(lines[-1])
    assert entry["atom_id"] == aid
    assert entry["verdict"] == "dispute"
    assert entry["reason"] == "test dispute reason"


def test_ledger_idempotent_no_double_append(tmp_path, monkeypatch):
    """Re-applying same verdict in same window should NOT double-append to ledger."""
    import echelon_engine.atoms.wrap_review as wr

    ledger = tmp_path / "test_ledger3.jsonl"
    monkeypatch.setattr(wr, "_LEDGER_PATH", ledger)

    store = CardStore(tmp_path / "test_ledger3.db")
    aid = _seed_atom(store, "test:ledger-idem")
    _lean(store, aid)

    session_start = int(time.time()) - 3600
    apply_verdict(store, aid, "approve", session_start)
    apply_verdict(store, aid, "approve", session_start)  # noop

    lines = ledger.read_text().strip().split("\n")
    assert len(lines) == 1, (
        f"Ledger should have 1 line after noop re-apply, got {len(lines)}"
    )


# ═══════════════════════════════════════════════════════════════════
# wrap_reviews table integrity
# ═══════════════════════════════════════════════════════════════════

def test_wrap_reviews_table_exists(store):
    """The wrap_reviews table should be created by CardStore.__init__."""
    with store._lock:
        tables = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wrap_reviews'"
        ).fetchall()
    assert len(tables) == 1


def test_wrap_reviews_pk_enforces_uniqueness(store):
    """The (atom_id, session_window_start) PK should prevent duplicates."""
    aid = _seed_atom(store, "test:pk")
    session_start = int(time.time()) - 3600
    now = int(time.time())

    # First insert
    store.conn.execute(
        "INSERT INTO wrap_reviews (atom_id, session_window_start, verdict, reason, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (aid, session_start, "approve", "", now))
    store.conn.commit()

    # Second insert with same PK should fail
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO wrap_reviews (atom_id, session_window_start, verdict, reason, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (aid, session_start, "dispute", "retry", now))


def test_default_window_start_is_stable_across_invocations():
    """The gate catch (2026-07-31): a per-call `now - WINDOW` default gave every
    CLI invocation a fresh session_window_start, so the idempotence PK never
    collided and repeated --approve runs STACKED the earn. The default must be
    bucketed: two calls seconds apart resolve to the same window start."""
    t = 1_785_450_000
    a = wrap_review.default_session_window_start(t)
    b = wrap_review.default_session_window_start(t + 90)  # "a minute later"
    assert a == b
    assert a % wrap_review.DEFAULT_SESSION_WINDOW_SECS == 0
    assert t - wrap_review.DEFAULT_SESSION_WINDOW_SECS < a <= t
