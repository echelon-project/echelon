"""Focused unit tests for view-through decay and impression log.

Uses temporary SQLite databases — no real bank touched.
"""
import json
import time

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.scoring import compute_score, lam_for, SCORE_BENCHMARK


@pytest.fixture
def store(tmp_path):
    """CardStore backed by a temp db."""
    db = tmp_path / "test_echelon.db"
    return CardStore(db)


def _add_atom(cs, coord, content="test", kind="project", score=100.0,
              score_history=None):
    """Helper: insert an atom directly with known kind and history."""
    now = int(time.time())
    if score_history is None:
        score_history = "[]"
    with cs._lock:
        cs.conn.execute(
            "INSERT INTO atoms(id, coordinate, content, score, score_history, "
            "use_count, born_from, ts, scope, kind, valence, arousal) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (coord, coord, content, score,
             json.dumps(score_history) if isinstance(score_history, list) else score_history,
             1, "", now, "test", kind, 0.0, 0.0))
        cs.conn.commit()
    # Also create an earned row so remember_fetch works
    with cs._lock:
        cs.conn.execute(
            "INSERT OR IGNORE INTO atom_earned(atom_id, score, use_count, score_history, last_fetch_ts) "
            "VALUES (?, 100.0, 1, '[]', ?)", (coord, now))
        cs.conn.commit()
    # Compile the spine so remember_fetch works
    try:
        cs.compile_atom_struct(coord, content)
    except Exception:
        pass


def _add_impressions(cs, rows):
    """Insert synthetic impression rows. Each row = (ts, scope, query_hash,
    atom_coord, opened, consumed)."""
    now = int(time.time())
    with cs._lock:
        cs.conn.executemany(
            "INSERT INTO impressions(ts, scope, query_hash, atom_coord, opened, consumed) "
            "VALUES (?,?,?,?,?,?)", rows)
        cs.conn.commit()


# ── Impression log tests ─────────────────────────────────────────────────────

def test_impressions_table_created(store):
    """Impressions table exists after store init."""
    with store._lock:
        r = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='impressions'"
        ).fetchone()
    assert r is not None


def test_log_impressions_inserts_rows(store):
    """log_impressions inserts one row per atom coordinate."""
    n = store.log_impressions("test", "what is scoring",
                              ["test:a", "test:b", "test:c"])
    assert n == 3
    with store._lock:
        count = store.conn.execute(
            "SELECT COUNT(*) FROM impressions").fetchone()[0]
    assert count == 3


def test_log_impressions_empty_list(store):
    """Empty coord list returns 0, no rows inserted."""
    n = store.log_impressions("test", "query", [])
    assert n == 0


def test_mark_impressions_opened(store):
    """mark_impressions_opened sets opened=1 for matching recent rows."""
    now = int(time.time())
    _add_impressions(store, [
        (now, "test", "hash1", "test:a", 0, 0),
        (now, "test", "hash1", "test:b", 0, 0),
        (now, "test", "hash2", "test:a", 0, 0),
    ])
    n = store.mark_impressions_opened("test", "test:a")
    assert n == 2  # Two rows for test:a
    with store._lock:
        rows = store.conn.execute(
            "SELECT opened FROM impressions WHERE atom_coord='test:a'").fetchall()
    assert all(r["opened"] == 1 for r in rows)
    # test:b still has opened=0
    with store._lock:
        r = store.conn.execute(
            "SELECT opened FROM impressions WHERE atom_coord='test:b'").fetchone()
    assert r["opened"] == 0


def test_mark_impressions_opened_respects_window(store):
    """Old impressions outside the window are NOT marked."""
    now = int(time.time())
    old = now - 3600  # 1 hour ago (outside 30 min window)
    _add_impressions(store, [
        (old, "test", "hash1", "test:a", 0, 0),
        (now, "test", "hash2", "test:a", 0, 0),
    ])
    n = store.mark_impressions_opened("test", "test:a")
    assert n == 1  # Only the recent one


def test_evict_impressions(store):
    """evict_impressions deletes rows older than N days."""
    now = int(time.time())
    old = now - 100 * 86400  # 100 days ago
    _add_impressions(store, [
        (old, "test", "hash1", "test:a", 0, 0),
        (now, "test", "hash2", "test:b", 0, 0),
    ])
    n = store.evict_impressions(days=90)
    assert n == 1  # Only the old one
    with store._lock:
        remaining = store.conn.execute(
            "SELECT atom_coord FROM impressions").fetchall()
    assert len(remaining) == 1
    assert remaining[0]["atom_coord"] == "test:b"


# ── View-through decay tests ─────────────────────────────────────────────────

def test_view_decay_empty_no_impressions(store):
    """No impressions -> no deltas."""
    rep = store.view_decay_pass("test")
    assert rep["atom_count"] == 0
    assert rep["deltas_applied"] == 0


def test_view_decay_nerve_noise_immune(store):
    """Impressions where NO sibling opened produce zero deltas.
    This is the 'nerve-noise immunity' guarantee."""
    now = int(time.time())
    _add_impressions(store, [
        (now, "test", "hash_nerve", "test:noisy", 0, 0),  # 10 losing, but no sibling opened
        (now, "test", "hash_nerve2", "test:noisy2", 0, 0),  # same situation
    ])
    _add_atom(store, "test:noisy", kind="project")
    _add_atom(store, "test:noisy2", kind="project")
    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] == 0, (
        f"Expected 0 deltas (nerve noise), got {rep}"
    )


def test_view_decay_losing_impressions_produce_deltas(store):
    """10 losing impressions against an opened sibling -> one -1.0 delta."""
    now = int(time.time())
    query_hash = "hash_competed"
    # 10 losing impressions for test:loser
    for _ in range(10):
        _add_impressions(store, [(now, "test", query_hash, "test:loser", 0, 0)])
    # One opened sibling in the same query set (makes it a 'competed' set)
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])

    _add_atom(store, "test:loser", kind="project", score=100.0)
    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] == 1, (
        f"Expected 1 delta applied, got {rep}"
    )
    assert rep["atom_count"] == 1
    assert abs(rep["total_delta"] - (-1.0)) < 0.1


def test_view_decay_does_not_bump_use_count(store):
    """Council ruling 2026-08-25: view-decay is a penalty for being passed over,
    not a use. A view-decay delta must append a 'view-decay'-sourced history
    entry and move the score, but must NOT increment use_count. A normal
    earn (op='earn', e.g. the fetch/reinforce path) still bumps it."""
    now = int(time.time())
    query_hash = "hash_use_count"
    for _ in range(10):
        _add_impressions(store, [(now, "test", query_hash, "test:loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    _add_atom(store, "test:loser", kind="project", score=100.0)

    before = store.get_atom("test:loser")
    assert before.use_count == 1  # seeded by _add_atom

    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] == 1, f"expected a decay delta, got {rep}"

    after = store.get_atom("test:loser")
    assert after.use_count == before.use_count, (
        f"view-decay must not bump use_count: before={before.use_count} "
        f"after={after.use_count}"
    )
    hist = json.loads(after.score_history)
    assert hist, "decay must append a history entry"
    last = hist[-1]
    assert len(last) == 3 and last[2] == "view-decay", (
        f"expected a 'view-decay'-sourced history entry, got {last}"
    )
    assert after.score < before.score, "decay must still move the score down"

    # Contrast: a normal earn (fetch/reinforce path) DOES bump use_count.
    _add_atom(store, "test:earner", kind="project", score=100.0)
    earner_before = store.get_atom("test:earner")
    store._apply_delta("atoms", "test:earner", 2.0, source="trace")
    earner_after = store.get_atom("test:earner")
    assert earner_after.use_count == earner_before.use_count + 1, (
        "a normal earn must still increment use_count"
    )


def test_view_decay_idempotent(store):
    """Re-running view_decay_pass doesn't double-charge (consumed=1)."""
    now = int(time.time())
    query_hash = "hash_idem"
    for _ in range(10):
        _add_impressions(store, [(now, "test", query_hash, "test:loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    _add_atom(store, "test:loser", kind="project", score=100.0)

    rep1 = store.view_decay_pass("test")
    assert rep1["deltas_applied"] == 1

    rep2 = store.view_decay_pass("test")
    assert rep2["deltas_applied"] == 0, (
        f"Re-run should apply 0 deltas (idempotent), got {rep2}"
    )


def test_view_decay_cap_max_three(store):
    """Max -3.0 delta per atom per pass, even with 100 losing impressions."""
    now = int(time.time())
    query_hash = "hash_cap"
    for _ in range(100):  # 100 losing -> would be -10, but capped at -3
        _add_impressions(store, [(now, "test", query_hash, "test:loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    _add_atom(store, "test:loser", kind="project", score=100.0)

    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] >= 1
    assert rep["total_delta"] >= -3.01, (
        f"Delta should be capped at -3.0, got {rep['total_delta']}"
    )


def test_view_decay_floor_respected(store):
    """Never decay below 85 floor."""
    now = int(time.time())
    query_hash = "hash_floor"
    for _ in range(50):
        _add_impressions(store, [(now, "test", query_hash, "test:loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    # Start at 86 — close to the floor
    _add_atom(store, "test:loser", kind="project", score=86.0)
    rep = store.view_decay_pass("test")
    # Should not push below 85
    from echelon_engine.atoms.cards import CardStore
    atom = store.get_atom("test:loser") if store.get_atom("test:loser") else None
    if rep["deltas_applied"] > 0:
        # After applying delta, check the effective score
        eff, _ = store.effective_score(atom) if atom else (0, False)
        assert eff >= 84.99, f"Score {eff} should be >= 85 (floor)"


def test_view_decay_feedback_exempt(store):
    """Feedback-type atoms are EXEMPT from view-decay."""
    now = int(time.time())
    query_hash = "hash_feedback"
    for _ in range(10):
        _add_impressions(store, [(now, "test", query_hash, "test:fb_loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    _add_atom(store, "test:fb_loser", kind="feedback", score=100.0)
    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] == 0, (
        f"Feedback atom should be exempt, got {rep}"
    )
    assert rep["atoms_exempt"] >= 1


def test_view_decay_user_exempt(store):
    """User-type atoms are EXEMPT from view-decay."""
    now = int(time.time())
    query_hash = "hash_user"
    for _ in range(10):
        _add_impressions(store, [(now, "test", query_hash, "test:usr_loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    _add_atom(store, "test:usr_loser", kind="user", score=100.0)
    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] == 0, (
        f"User atom should be exempt, got {rep}"
    )


def test_view_decay_less_than_k_no_delta(store):
    """Fewer than K=10 losing impressions produces no delta."""
    now = int(time.time())
    query_hash = "hash_few"
    for _ in range(5):  # Only 5
        _add_impressions(store, [(now, "test", query_hash, "test:loser", 0, 0)])
    _add_impressions(store, [(now, "test", query_hash, "test:winner", 1, 0)])
    _add_atom(store, "test:loser", kind="project", score=100.0)
    rep = store.view_decay_pass("test")
    assert rep["deltas_applied"] == 0, (
        f"Fewer than K=10 should produce no delta, got {rep}"
    )


def test_view_decay_subthreshold_rows_accumulate_across_passes(store):
    """Sub-threshold losing rows must survive a pass in which ANOTHER atom crosses
    the threshold, so impressions ACCUMULATE — a slow-bleed atom surfaced <K times
    per pass interval must still decay eventually (gate finding 2026-07-31: the
    original blanket consumed-wipe reset accumulation every wrap)."""
    now = int(time.time())
    qh = "hash_accum"
    # test:slow has only 5 losing rows; test:fast has 10 and crosses the threshold
    for _ in range(5):
        _add_impressions(store, [(now, "test", qh, "test:slow", 0, 0)])
    for _ in range(10):
        _add_impressions(store, [(now, "test", qh, "test:fast", 0, 0)])
    _add_impressions(store, [(now, "test", qh, "test:winner", 1, 0)])
    _add_atom(store, "test:slow", kind="project", score=100.0)
    _add_atom(store, "test:fast", kind="project", score=100.0)

    rep1 = store.view_decay_pass("test")
    assert rep1["deltas_applied"] == 1  # only test:fast

    # test:slow's rows must remain unconsumed after the pass
    with store._lock:
        n_unconsumed = store.conn.execute(
            "SELECT COUNT(*) FROM impressions "
            "WHERE atom_coord='test:slow' AND consumed=0").fetchone()[0]
    assert n_unconsumed == 5, (
        f"Sub-threshold rows were wiped (found {n_unconsumed}/5 unconsumed) — "
        "accumulation across passes is broken"
    )

    # 5 more losing rows arrive in a new competed set -> 10 total -> decays now
    qh2 = "hash_accum2"
    for _ in range(5):
        _add_impressions(store, [(now, "test", qh2, "test:slow", 0, 0)])
    _add_impressions(store, [(now, "test", qh2, "test:winner2", 1, 0)])
    rep2 = store.view_decay_pass("test")
    assert rep2["deltas_applied"] == 1, (
        f"Accumulated 10 losing rows should produce a delta, got {rep2}"
    )
    assert abs(rep2["total_delta"] - (-1.0)) < 0.1
