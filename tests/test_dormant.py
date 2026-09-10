"""Dormant tier tests (2026-07-31).

DORMANT = COMPUTED STATE, read-time, from effective_score vs threshold.
Atoms below DORMANT_THRESHOLD (80.0) are demoted out of default recall pool,
NEVER deleted. Tests run on temp DBs only — the live bank is untouched.
"""
import json
import math
import time
import pytest

from echelon_engine.atoms.cards import (
    CardStore, Atom, SCORE_K,
)
from echelon_engine.atoms.uame import SCORE_BENCHMARK, compute_score
from echelon_engine.atoms.scoring import DORMANT_THRESHOLD, DORMANT_EXEMPT, lam_for


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    """Opt into legacy scope-omit for tests that predate the guard."""
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


# ── TRAP 3: View-decay alone cannot dormant an atom ──────────────────────
def test_view_decay_floor_not_dormant(store):
    """Atoms at the view-decay floor (85) are NOT dormant with threshold=80."""
    aid = store.add_atom("test:view_floor", "view-decayed to floor atom")
    # Manually set score to 85 (view-decay floor) via score_history with a
    # negative delta that brings it to exactly 85
    store.conn.execute(
        "UPDATE atoms SET score=85.0, score_history=? WHERE id=?",
        (json.dumps([[int(time.time()), -15.0, "view-decay"]]), aid))
    store.conn.commit()
    a = store.get_atom(aid)
    eff, _ = store.effective_score(a)
    assert eff >= 85.0  # at or near the floor
    assert eff >= DORMANT_THRESHOLD  # NOT dormant — trap 3 satisfied

    # Verify dormant_content_set doesn't catch it
    dormant = store.dormant_content_set()
    content_key = (a.content or "")[:120]
    assert content_key not in dormant


# ── Disclaimed atoms ARE dormant ──────────────────────────────────────
def test_disclaimed_atom_is_dormant(store):
    """Atoms at JUDGED_FLOOR (75) are dormant — they were explicitly down-corrected."""
    aid = store.add_atom("test:disclaimed", "disclaimed atom for dormancy test")
    # Set up as disclaimed: stamp born_from with judged mark
    store.conn.execute(
        "UPDATE atoms SET score=75.0, born_from=? WHERE id=?",
        (store._JUDGED_MARK + " ts:" + str(int(time.time())), aid))
    store.conn.commit()
    a = store.get_atom(aid)
    eff, _ = store.effective_score(a)
    assert eff == 75.0  # effective_score returns stored score for disclaimed
    assert eff < DORMANT_THRESHOLD  # below 80 → dormant

    dormant = store.dormant_content_set()
    content_key = (a.content or "")[:120]
    assert content_key in dormant


# ── Exempt types never dormant ──────────────────────────────────────
def test_feedback_atom_never_dormant(store):
    """Feedback-type atoms are exempt — they can NEVER be dormant."""
    aid = store.add_atom("test:fb", "user feedback atom", kind="feedback")
    # Push its score below threshold
    store.conn.execute(
        "UPDATE atoms SET score=50.0, score_history=? WHERE id=?",
        (json.dumps([[int(time.time()), -50.0, "fire_lower"]]), aid))
    store.conn.commit()
    a = store.get_atom(aid)
    eff, _ = store.effective_score(a)
    assert eff < DORMANT_THRESHOLD  # score IS below threshold but...
    # ...exempt type means it's never dormant
    dormant = store.dormant_content_set()
    content_key = (a.content or "")[:120]
    assert content_key not in dormant


def test_user_atom_never_dormant(store):
    """User-type atoms are exempt — they can NEVER be dormant."""
    aid = store.add_atom("test:user_pref", "user preference atom", kind="user")
    store.conn.execute(
        "UPDATE atoms SET score=50.0, score_history=? WHERE id=?",
        (json.dumps([[int(time.time()), -50.0, "fire_lower"]]), aid))
    store.conn.commit()
    a = store.get_atom(aid)
    dormant = store.dormant_content_set()
    content_key = (a.content or "")[:120]
    assert content_key not in dormant


# ── Neutral atoms are NOT dormant ──────────────────────────────────────
def test_neutral_atom_not_dormant(store):
    """Atoms at B=100 (neutral) are NOT dormant."""
    aid = store.add_atom("test:neutral", "neutral atom at 100")
    a = store.get_atom(aid)
    eff, _ = store.effective_score(a)
    assert eff >= SCORE_BENCHMARK
    assert eff >= DORMANT_THRESHOLD

    dormant = store.dormant_content_set()
    assert len(dormant) == 0


# ── count_dormant returns correct structure ──────────────────────────
def test_count_dormant_structure(store):
    """count_dormant returns {count, dormant} with correct info."""
    # Add one disclaimed atom (dormant) and one neutral (not)
    aid1 = store.add_atom("test:disc", "disclaimed atom")
    store.conn.execute(
        "UPDATE atoms SET score=75.0, born_from=? WHERE id=?",
        (store._JUDGED_MARK, aid1))
    store.add_atom("test:neut", "neutral atom")
    store.conn.commit()

    info = store.count_dormant()
    assert isinstance(info, dict)
    assert "count" in info
    assert "dormant" in info
    assert info["count"] >= 1
    assert len(info["dormant"]) == info["count"]


# ── Auto-wake: earn restores a dormant atom ──────────────────────────
def test_earn_wakes_dormant_atom(store):
    """An earn event that lifts effective_score above threshold makes the
    atom live again — no extra verb needed. Dormancy is computed state."""
    aid = store.add_atom("test:wake_me", "dormant atom to be woken")
    # Push just below threshold with a negative delta — one earn (+2) lifts it above
    initial_score = DORMANT_THRESHOLD - 1.0  # 79.0 with threshold 80
    hist = [[int(time.time()) - 86400, initial_score - 100.0, "fire_lower:test"]]
    store.conn.execute(
        "UPDATE atoms SET score=?, score_history=? WHERE id=?",
        (initial_score, json.dumps(hist), aid))
    store.conn.commit()

    a = store.get_atom(aid)
    eff_before, _ = store.effective_score(a)
    assert eff_before < DORMANT_THRESHOLD

    dormant_before = store.dormant_content_set()
    content_key = (a.content or "")[:120]
    assert content_key in dormant_before

    # Compile atom spine so remember_fetch can work
    store.conn.execute(
        "INSERT OR IGNORE INTO atom_earned (atom_id, score, use_count, score_history, last_fetch_ts) "
        "VALUES (?, ?, 0, ?, ?)",
        (aid, initial_score, json.dumps(hist), int(time.time())))
    store.conn.execute(
        "INSERT OR IGNORE INTO atom_spine (atom_id, slug, claim, directive) "
        "VALUES (?, 'wake_me', 'test claim', 'test directive')",
        (aid,))
    store.conn.commit()

    # Earn through the witnessed door
    result = store.remember_fetch(aid)
    assert result is not None, "remember_fetch should succeed"

    # After earn, the atom should be above threshold
    a2 = store.get_atom(aid)
    eff_after, _ = store.effective_score(a2)
    assert eff_after >= DORMANT_THRESHOLD, (
        f"earn should wake dormant atom: {eff_before} → {eff_after}")

    dormant_after = store.dormant_content_set()
    assert content_key not in dormant_after


# ── No dormancy when bank is all neutral ──────────────────────────────
def test_no_dormant_with_all_neutral(store):
    """When all atoms are at neutral (B=100), dormant_count is 0."""
    for i in range(5):
        store.add_atom(f"test:atom{i}", f"neutral atom {i}")
    info = store.count_dormant()
    assert info["count"] == 0
    dormant = store.dormant_content_set()
    assert len(dormant) == 0


# ── Threshold boundary test ──────────────────────────────────────────
def test_threshold_boundary(store):
    """Atoms at exactly the threshold are NOT dormant (< not <=)."""
    aid = store.add_atom("test:at_threshold", "atom at dormancy threshold")
    # Set score exactly at threshold
    store.conn.execute("UPDATE atoms SET score=? WHERE id=?", (DORMANT_THRESHOLD, aid))
    store.conn.commit()

    dormant = store.dormant_content_set()
    a = store.get_atom(aid)
    content_key = (a.content or "")[:120]
    assert content_key not in dormant  # strict less-than


# ── Multiple scopes: dormant is scope-aware ──────────────────────────
def test_dormant_scope_aware(store):
    """dormant_content_set(scope=...) only considers atoms in that scope."""
    # Add dormant atom in scope 'test' and neutral in scope 'other'
    aid1 = store.add_atom("test:a", "dormant in test", scope="test")
    store.conn.execute(
        "UPDATE atoms SET score=75.0, born_from=? WHERE id=?",
        (store._JUDGED_MARK, aid1))
    store.add_atom("other:b", "neutral in other", scope="other")
    store.conn.commit()

    # Scope 'test' should see the dormant atom
    test_dormant = store.dormant_content_set(scope="test")
    a1 = store.get_atom(aid1)
    assert (a1.content or "")[:120] in test_dormant

    # Scope 'other' should have none
    other_dormant = store.dormant_content_set(scope="other")
    assert len(other_dormant) == 0


# ── DORMANT_EXEMPT is a frozenset ──────────────────────────────────
def test_dormant_exempt_types():
    """The exemption frozenset contains the types that can never be dormant."""
    assert "feedback" in DORMANT_EXEMPT
    assert "user" in DORMANT_EXEMPT
    assert isinstance(DORMANT_EXEMPT, frozenset)


# ── GATE FIX: prefix collision must never hide a LIVE atom ──────────────────
def test_live_twin_vetoes_the_shared_prefix(store):
    """The prefix is a cross-store bridge and prefixes COLLIDE (cross-scope
    duplicate bodies, superseded near-twins: measured live 2026-07-31, 97
    dormant atoms' prefixes matched 210 atoms — 113 LIVE). A prefix enters the
    filter set only when EVERY atom bearing it is dormant; a live twin vetoes.
    Direction of error is under-filtering, never hiding live knowledge."""
    long_body = "shared first line of a duplicated lesson body " * 4  # > 120 chars
    a1 = store.add_atom("test:twin_dormant", long_body + " (old copy)")
    a2 = store.add_atom("test:twin_live", long_body + " (live copy)")
    # disclaim ONLY the first — both share content[:120]
    store.conn.execute(
        "UPDATE atoms SET score=75.0, born_from=? WHERE id=?",
        (store._JUDGED_MARK + " ts:" + str(int(time.time())), a1))
    store.conn.commit()
    atom1, atom2 = store.get_atom(a1), store.get_atom(a2)
    assert (atom1.content or "")[:120] == (atom2.content or "")[:120]  # collision is real
    eff1, _ = store.effective_score(atom1)
    eff2, _ = store.effective_score(atom2)
    assert eff1 < DORMANT_THRESHOLD <= eff2  # one dormant, one live

    dormant = store.dormant_content_set()
    assert (atom2.content or "")[:120] not in dormant  # live twin veto holds


# ═══════════════════════════════════════════════════════════════════════════
# Regression: shared-law fold-in (2026-07-31)
# Verifies that effective_score, dormant_content_set, and count_dormant all
# route through ONE _effective_from_row helper.  A future law change that
# edits only the helper should keep these passing; a re-forked copy would
# diverge and fail.
# ═══════════════════════════════════════════════════════════════════════════

def _decayed_score(history, now, kind=None):
    """Expected score via the public compute_score, for test assertions."""
    if not history:
        return SCORE_BENCHMARK
    return compute_score(history, now=now, lam=lam_for(kind))


class TestFoldinRegression:
    """Synthetic store covering every _effective_from_row branch."""

    NOW = int(time.time())

    @pytest.fixture
    def store(self, tmp_path):
        return CardStore(tmp_path / "foldin_test.db")

    # ── Branch 1: judged-mark atom ──────────────────────────────────────
    def test_judged_mark_branch(self, store):
        """Judged-mark atoms return stored score, bypassing recompute.
        All three methods (effective_score, dormant_content_set, count_dormant)
        agree on the score."""
        aid = store.add_atom("test:judged", "judged atom", kind="project")
        store.conn.execute(
            "UPDATE atoms SET score=50.0, born_from=? WHERE id=?",
            (store._JUDGED_MARK, aid))
        store.conn.commit()  # REMINDER: commit after raw SQL

        a = store.get_atom(aid)
        eff, borrowed = store.effective_score(a)
        assert eff == 50.0
        assert borrowed is False

        # dormant_content_set: content key should be dormant since 50 < 80
        dcs = store.dormant_content_set()
        ck = (a.content or "")[:120]
        assert ck in dcs

        # count_dormant: should list this atom with score ~50
        cd = store.count_dormant()
        ids = [d["id"] for d in cd["dormant"]]
        assert aid[:12] in ids

    # ── Branch 2: exempt kind never dormant ─────────────────────────────
    def test_exempt_kind_outside_helper(self, store):
        """Exempt types (feedback, user) are gated OUTSIDE the helper —
        dormant_content_set and count_dormant skip them entirely.
        effective_score still computes a real score (no exemption there)."""
        for kind in ("feedback", "user"):
            aid = store.add_atom(f"test:{kind}_exempt", f"{kind} atom", kind=kind)
            # Push score below threshold
            store.conn.execute(
                "UPDATE atoms SET score=30.0, score_history=? WHERE id=?",
                (json.dumps([[self.NOW - 86400, -70.0, "test"]]), aid))
            store.conn.commit()

            a = store.get_atom(aid)
            eff, _ = store.effective_score(a)
            assert eff < DORMANT_THRESHOLD, f"{kind}: effective_score should be <80"

            # But dormant_content_set MUST NOT contain it
            dcs = store.dormant_content_set()
            ck = (a.content or "")[:120]
            assert ck not in dcs, f"{kind}: exempt type must never be dormant"

            cd = store.count_dormant()
            ids = [d["id"] for d in cd["dormant"]]
            assert aid[:12] not in ids, f"{kind}: exempt type must not appear in count"

    # ── Branch 3: door-borrow with earned history ───────────────────────
    def test_door_borrow_branch(self, store):
        """When atom_earned has use_count>0 AND non-empty score_history,
        effective_score = max(own, door).  Dormant methods use the same
        helper (door_require_earned_use=False — slightly wider gate — but
        for this fixture both paths produce the same result)."""
        aid = store.add_atom("test:borrow", "door-borrow atom", kind="reference")
        # Own score: modest decay — compute expected
        own_hist = [[self.NOW - 10 * 86400, -20.0, "test"]]
        own_expected = _decayed_score(own_hist, self.NOW, kind="reference")
        store.conn.execute(
            "UPDATE atoms SET score=?, score_history=? WHERE id=?",
            (own_expected, json.dumps(own_hist), aid))
        store.conn.commit()

        # Door score: stronger (earned more recently) — should win via max()
        door_hist = [[self.NOW - 2 * 86400, 20.0, "fetch"],
                      [self.NOW - 1 * 86400, 10.0, "fetch"]]
        door_expected = _decayed_score(door_hist, self.NOW, kind="reference")
        assert door_expected > own_expected, "fixture: door score must be higher"

        # Create earned row with use_count > 0 and score_history
        store.conn.execute(
            "INSERT OR REPLACE INTO atom_earned (atom_id, score, use_count, score_history, last_fetch_ts) "
            "VALUES (?, ?, 5, ?, ?)",
            (aid, door_expected, json.dumps(door_hist), self.NOW))
        store.conn.commit()

        a = store.get_atom(aid)
        eff, _ = store.effective_score(a)
        # Should be max(own, door) — door wins here
        assert eff == pytest.approx(door_expected, rel=1e-5), (
            f"door-borrow: expected max(own={own_expected:.2f}, door={door_expected:.2f})")

        # Dormant methods must agree
        dcs = store.dormant_content_set()
        ck = (a.content or "")[:120]
        # door_expected is well above 80, so NOT dormant
        assert ck not in dcs, "door-borrow atom above threshold should not be dormant"

        cd = store.count_dormant()
        ids = [d["id"] for d in cd["dormant"]]
        assert aid[:12] not in ids

    # ── Branch 4: door-borrow where own > door ──────────────────────────
    def test_door_borrow_own_wins(self, store):
        """When own score > door score, max(own, door) = own.  Verify the
        max is NOT a simple door-override."""
        aid = store.add_atom("test:own_wins", "own beats door", kind="project")
        # Own score: strong, recent earn
        own_hist = [[self.NOW - 1 * 86400, 30.0, "fetch"]]
        own_expected = _decayed_score(own_hist, self.NOW, kind="project")
        store.conn.execute(
            "UPDATE atoms SET score=?, score_history=? WHERE id=?",
            (own_expected, json.dumps(own_hist), aid))
        store.conn.commit()

        # Door score: weaker (older, smaller delta)
        door_hist = [[self.NOW - 30 * 86400, 5.0, "fetch"]]
        door_expected = _decayed_score(door_hist, self.NOW, kind="project")
        assert own_expected > door_expected, "fixture: own score must be higher"

        store.conn.execute(
            "INSERT OR REPLACE INTO atom_earned (atom_id, score, use_count, score_history, last_fetch_ts) "
            "VALUES (?, ?, 3, ?, ?)",
            (aid, door_expected, json.dumps(door_hist), self.NOW))
        store.conn.commit()

        a = store.get_atom(aid)
        eff, _ = store.effective_score(a)
        assert eff == pytest.approx(own_expected, rel=1e-5), (
            f"own-wins: expected own={own_expected:.2f}, got {eff:.2f}")

        # Consistency check: dormant_content_set and count_dormant agree
        dcs = store.dormant_content_set()
        ck = (a.content or "")[:120]
        # own_expected is well above 80, so NOT dormant
        assert ck not in dcs

        cd = store.count_dormant()
        ids = [d["id"] for d in cd["dormant"]]
        assert aid[:12] not in ids

    # ── Branch 5: plain decayed atom (no earned row) ────────────────────
    def test_plain_decayed_atom(self, store):
        """Atom with score_history but NO earned row: effective_score is
        recomputed from own history only."""
        aid = store.add_atom("test:plain", "plain decayed atom", kind="project")
        hist = [[self.NOW - 5 * 86400, -30.0, "fire_lower:test"]]
        expected = _decayed_score(hist, self.NOW, kind="project")
        store.conn.execute(
            "UPDATE atoms SET score=?, score_history=? WHERE id=?",
            (expected, json.dumps(hist), aid))
        store.conn.commit()

        a = store.get_atom(aid)
        eff, _ = store.effective_score(a)
        assert eff == pytest.approx(expected, rel=1e-5)

        # All three methods must agree on the effective score
        dcs = store.dormant_content_set()
        ck = (a.content or "")[:120]
        if eff < DORMANT_THRESHOLD:
            assert ck in dcs
        else:
            assert ck not in dcs

        cd = store.count_dormant()
        ids = [d["id"] for d in cd["dormant"]]
        if eff < DORMANT_THRESHOLD:
            assert aid[:12] in ids
        else:
            assert aid[:12] not in ids

    # ── Branch 6: neutral atom (empty history → B=100) ──────────────────
    def test_neutral_atom_benchmark(self, store):
        """Atom with empty score_history: _recompute_score returns
        SCORE_BENCHMARK (100).  Not dormant."""
        aid = store.add_atom("test:neutral", "fresh neutral atom")
        a = store.get_atom(aid)
        eff, _ = store.effective_score(a)
        assert eff == pytest.approx(SCORE_BENCHMARK)
        assert eff >= DORMANT_THRESHOLD

        dcs = store.dormant_content_set()
        ck = (a.content or "")[:120]
        assert ck not in dcs

        cd = store.count_dormant()
        ids = [d["id"] for d in cd["dormant"]]
        assert aid[:12] not in ids

    # ── Consistency: all three methods agree on every atom ──────────────
    def test_three_methods_agree_on_mixed_store(self, store):
        """With a mixed store (judged, exempt, door-borrow, plain, neutral),
        every atom's effective_score agrees with its dormant status.
        This is the anti-fork test: if someone re-copies the law into one
        method but not the others, this fails."""
        # 1. Judged-mark (score=65 → dormant)
        a1 = store.add_atom("test:mix_judged", "judged mix atom", kind="project")
        store.conn.execute(
            "UPDATE atoms SET score=65.0, born_from=? WHERE id=?",
            (store._JUDGED_MARK, a1))
        store.conn.commit()

        # 2. Plain decayed (below threshold)
        a2 = store.add_atom("test:mix_decayed", "decayed mix atom", kind="project")
        hist2 = [[self.NOW - 5 * 86400, -35.0, "fire_lower"]]
        score2 = _decayed_score(hist2, self.NOW, kind="project")
        store.conn.execute(
            "UPDATE atoms SET score=?, score_history=? WHERE id=?",
            (score2, json.dumps(hist2), a2))
        store.conn.commit()

        # 3. Door-borrow (earned row with higher score)
        a3 = store.add_atom("test:mix_borrow", "borrow mix atom", kind="reference")
        hist3_own = [[self.NOW - 15 * 86400, -40.0, "fire_lower"]]
        score3_own = _decayed_score(hist3_own, self.NOW, kind="reference")
        store.conn.execute(
            "UPDATE atoms SET score=?, score_history=? WHERE id=?",
            (score3_own, json.dumps(hist3_own), a3))
        hist3_door = [[self.NOW - 1 * 86400, 30.0, "fetch"]]
        score3_door = _decayed_score(hist3_door, self.NOW, kind="reference")
        assert score3_door > score3_own, "fixture: door must beat own"
        store.conn.execute(
            "INSERT OR REPLACE INTO atom_earned (atom_id, score, use_count, score_history, last_fetch_ts) "
            "VALUES (?, ?, 2, ?, ?)",
            (a3, score3_door, json.dumps(hist3_door), self.NOW))
        store.conn.commit()

        # 4. Exempt (feedback, below threshold — should NOT be dormant)
        a4 = store.add_atom("test:mix_fb", "feedback mix atom", kind="feedback")
        hist4 = [[self.NOW - 5 * 86400, -40.0, "fire_lower"]]
        store.conn.execute(
            "UPDATE atoms SET score=?, score_history=? WHERE id=?",
            (_decayed_score(hist4, self.NOW, kind="feedback"), json.dumps(hist4), a4))
        store.conn.commit()

        # 5. Neutral
        a5 = store.add_atom("test:mix_neutral", "neutral mix atom")

        # Now verify consistency per atom
        dcs = store.dormant_content_set()
        cd = store.count_dormant()
        cd_ids = [d["id"] for d in cd["dormant"]]

        for aid, should_be_dormant in [
            (a1, True),   # judged, score=65 < 80
            (a2, score2 < DORMANT_THRESHOLD),
            (a3, False),  # door-borrow elevates above threshold
            (a4, False),  # exempt type: never dormant
            (a5, False),  # neutral B=100
        ]:
            a = store.get_atom(aid)
            eff, _ = store.effective_score(a)
            ck = (a.content or "")[:120]

            if should_be_dormant:
                assert eff < DORMANT_THRESHOLD, (
                    f"atom {aid[:12]}: expected dormant (eff={eff:.1f})")
                assert ck in dcs, (
                    f"atom {aid[:12]}: dormant_content_set missing")
                assert aid[:12] in cd_ids, (
                    f"atom {aid[:12]}: count_dormant missing")
            else:
                if a.kind not in ("feedback", "user"):
                    assert eff >= DORMANT_THRESHOLD, (
                        f"atom {aid[:12]}: expected live (eff={eff:.1f})")
                # Exempt atoms can have low eff but still NOT be in dormant sets
                assert ck not in dcs, (
                    f"atom {aid[:12]}: should NOT be in dormant_content_set")
                assert aid[:12] not in cd_ids, (
                    f"atom {aid[:12]}: should NOT be in count_dormant")
