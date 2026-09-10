"""Tests for the CARD_KIND governed classifier (registry.py) + the card.kind wiring.

Covers:
  - classify() maps real-world born_from spellings to the right banded code
  - the packed-payload born_froms (plan-cache/cartridge/offer) NEVER leak into the session band
  - is_session / class_of band math
  - add_card stamps kind from born_from at write time
  - the unstamped_card_kind antibody FIRES on a kind=0 card whose born_from classifies real
  - idempotency of classify (pure function)
"""
from __future__ import annotations

import pytest

from echelon_engine import registry as R
from echelon_engine.atoms.cards import CardStore


# ── classify: the real spellings measured from the bank ───────────────────────
@pytest.mark.parametrize("born_from, expected", [
    ("wrap-session 2026-07-12", 100),
    ("session-wrap-2026-07-21", 100),      # the hyphen-order variant that broke relive
    ("session_wrap_2026_07_21", 100),      # the underscore variant
    ("relive of 4b19c0", 101),
    ("session-offer", 110),
    ("trace", 200),
    ("think", 210),
    ("plan-cache|goal=x|leaves=[]", 230),  # packed payload — must NOT be session
    ("cartridge:ux-cartridge|foo", 300),   # packed payload — must NOT be session
    ("self_seed", 310),
    ("judged:disclaimed", 410),
    ("", 0),
    ("owner: 'perfecting the gem'", 0),    # pure narrative -> honest unknown
])
def test_classify_maps_real_born_from(born_from, expected):
    assert R.classify(born_from) == expected


def test_packed_payloads_never_leak_into_session_band():
    """A born_from carrying 'wrap'/'session' words INSIDE a structured prefix must classify by
    the prefix, never as a session (the misclassification that would pollute relive --list)."""
    assert not R.is_session(R.classify("plan-cache|goal=session wrap now"))
    assert not R.is_session(R.classify("cartridge:wrap-session|x"))
    assert not R.is_session(R.classify("session-offer|wrap-session"))


def test_band_math():
    assert R.class_of(100) == 100 and R.class_of(109) == 100 and R.class_of(230) == 200
    assert R.is_session(100) and R.is_session(109)
    assert not R.is_session(110)   # offer is NOT a walkable session arc
    assert not R.is_session(200)


def test_classify_is_pure_idempotent():
    for bf in ("wrap-session x", "plan-cache|g", "", "trace"):
        assert R.classify(bf) == R.classify(bf)


# ── the wiring: add_card stamps kind, and the antibody catches an unstamped card ──
@pytest.fixture
def store(tmp_path):
    return CardStore(db_path=tmp_path / "t.db")


def test_add_card_stamps_kind_from_born_from(store):
    cid = store.add_card("a session", ["x:a", "x:b"], born_from="wrap-session 2026-07-22")
    row = store.conn.execute("SELECT kind FROM cards WHERE id=?", (cid,)).fetchone()
    assert row["kind"] == 100

    cid2 = store.add_card("a plan", ["leaf-0"], born_from="plan-cache|goal=g|leaves=[]")
    row2 = store.conn.execute("SELECT kind FROM cards WHERE id=?", (cid2,)).fetchone()
    assert row2["kind"] == 230


def test_unstamped_kind_antibody_fires(store):
    """Simulate the relive-rot bug: a session card whose kind was left 0. The antibody must WARN."""
    cid = store.add_card("s", ["x:a", "x:b"], born_from="wrap-session 2026-07-22")
    # force the bug: blank the kind back to 0 as if wrap never stamped it
    store.conn.execute("UPDATE cards SET kind=0 WHERE id=?", (cid,))
    store.conn.commit()
    res = store.scan()
    hits = [w for w in res["warn"] if w["rule"] == "unstamped_card_kind" and w["card_id"] == cid]
    assert len(hits) == 1, "antibody did not fire on an unstamped session card"


def test_antibody_silent_on_honest_unknown(store):
    """A card whose born_from is pure narrative (classifies to 0) is NOT flagged — kind=0 is
    correct for it, not a defect."""
    cid = store.add_card("n", ["x:a"], born_from="dome-run")
    res = store.scan()
    hits = [w for w in res["warn"] if w["rule"] == "unstamped_card_kind" and w["card_id"] == cid]
    assert hits == []


# ── card scope derived from refs' atoms (the shared-bank clash fix) ───────────
def test_card_scope_derived_from_refs(store):
    """A card carries no scope column; its scope is the plurality scope of the atoms it refs.
    This is what lets relive --list --scope X separate projects in the shared bank."""
    import os
    os.environ["ECHELON_LEGACY_SCOPE_OMIT"] = "1"  # allow the test to plant scoped atoms freely
    try:
        store.add_atom("proj_a:lesson1", "c1", scope="proj_a")
        store.add_atom("proj_a:lesson2", "c2", scope="proj_a")
        store.add_atom("proj_b:lesson1", "c3", scope="proj_b")
    finally:
        os.environ.pop("ECHELON_LEGACY_SCOPE_OMIT", None)
    assert store.card_scope(["proj_a:lesson1", "proj_a:lesson2"]) == "proj_a"
    assert store.card_scope(["proj_b:lesson1"]) == "proj_b"
    assert store.card_scope(["nonexistent:x"]) == ""   # nothing resolves -> ''


def test_recent_arc_cards_scope_filter(store):
    """recent_arc_cards(scope=X) returns only sessions whose derived scope is X."""
    from echelon_engine.atoms.relive import recent_arc_cards
    import os
    os.environ["ECHELON_LEGACY_SCOPE_OMIT"] = "1"
    try:
        store.add_atom("proj_a:a1", "aa", scope="proj_a")
        store.add_atom("proj_b:b1", "bb", scope="proj_b")
    finally:
        os.environ.pop("ECHELON_LEGACY_SCOPE_OMIT", None)
    store.add_card("A session", ["proj_a:a1"], born_from="wrap-session A")
    store.add_card("B session", ["proj_b:b1"], born_from="wrap-session B")
    a_only = recent_arc_cards(store, scope="proj_a")
    labels = [c["label"] for c in a_only]
    assert "A session" in labels and "B session" not in labels
    # no scope -> both listed
    allc = [c["label"] for c in recent_arc_cards(store, scope=None)]
    assert "A session" in allc and "B session" in allc


def test_recent_arc_cards_pagination_cursor(store):
    """--before <ts> pages back through older sessions with no overlap, even when a scope is
    sparse in the recent pool (the limit-3-returned-2 bug: batch until `limit` matches)."""
    from echelon_engine.atoms.relive import recent_arc_cards
    import os, time
    os.environ["ECHELON_LEGACY_SCOPE_OMIT"] = "1"
    try:
        # 6 echelon sessions interleaved with 6 other-scope ones (sparsity), distinct ts.
        for i in range(6):
            store.add_atom(f"echelon:e{i}", f"e{i}", scope="echelon")
            store.add_atom(f"other:o{i}", f"o{i}", scope="other")
        for i in range(6):
            store.conn.execute("UPDATE cards SET ts=? WHERE id=?",  # force ordering
                               (1000 + i, store.add_card(f"E{i}", [f"echelon:e{i}"], born_from="wrap-session E")))
            store.conn.execute("UPDATE cards SET ts=? WHERE id=?",
                               (1000 + i, store.add_card(f"O{i}", [f"other:o{i}"], born_from="wrap-session O")))
        store.conn.commit()
    finally:
        os.environ.pop("ECHELON_LEGACY_SCOPE_OMIT", None)
    page1 = recent_arc_cards(store, limit=3, scope="echelon")
    assert len(page1) == 3, f"sparse scope should still fill the page, got {len(page1)}"
    cursor = page1[-1]["ts"]
    page2 = recent_arc_cards(store, limit=3, scope="echelon", before=cursor)
    ids1 = {c["id"] for c in page1}
    ids2 = {c["id"] for c in page2}
    assert ids1.isdisjoint(ids2), "pages overlap — cursor is wrong"
    assert all(c["ts"] < cursor for c in page2), "page 2 has a card >= cursor"
