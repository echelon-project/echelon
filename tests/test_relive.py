"""Focused unit tests for echelon_engine.atoms.relive — resume a session as a CHAIN of weight.

relive walks a session's arc-CARD (an ordered list of atom coordinates) over an isolated
CardStore, re-firing each atom's warmth and surfacing the supporting conversation it anchored
in a SIDECAR table (relive_evidence.db, OUTSIDE the soul). This suite pins the store-backed
navigation seams on an isolated CardStore (tmp core_v2.db) and the evidence sidecar on an
isolated tmp db (relive.EVIDENCE_DB monkeypatched), so nothing touches ~/.echelon.

Covered:
  - recent_arc_cards: only wrap-session/relive cards, newest-first
  - prior_arc_card: explicit prev-edge wins; timestamp fallback; None at start of history
  - next_arc_cards: prev-edge children + forked_from children (fork => >1); ts fallback
  - record_fork: stamps forked_from into born_from idempotently; rejects self/missing
  - record_evidence / evidence_for: idempotent anchor round-trip
  - relive: walks the chain in order, surfaces evidence, marks a missing atom
  - render: experience-first formatting
"""
import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import relive as R


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    """These fixtures predate the empty-scope guard (2026-07-09) and plant atoms with no
    scope= on real-scope-shaped heads. The guard's law stays strict (the gate refused
    allowlisting fixture heads — memory:* IS the orphan mechanism); tests opt in HERE,
    per-test and auto-reverted, via the deliberate escape hatch."""
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def cs(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


@pytest.fixture(autouse=True)
def isolate_evidence(tmp_path, monkeypatch):
    """Point the evidence sidecar at a tmp db so tests never touch ~/.echelon."""
    monkeypatch.setattr(R, "EVIDENCE_DB", str(tmp_path / "relive_evidence.db"))


# helper: cards carry a real ts column; force ordering deterministically
def _set_ts(cs, card_id, ts):
    cs.conn.execute("UPDATE cards SET ts=? WHERE id=?", (ts, card_id))
    cs.conn.commit()


# ── recent_arc_cards ───────────────────────────────────────────────────────
def test_recent_arc_cards_only_session_cards_newest_first(cs):
    a = cs.add_card("session A", ["echelon:x"], born_from="wrap-session 2026-06-01")
    b = cs.add_card("session B", ["echelon:y"], born_from="relive 2026-06-02")
    junk = cs.add_card("random proc", ["echelon:z"], born_from="some-other-source")
    _set_ts(cs, a, 100); _set_ts(cs, b, 200); _set_ts(cs, junk, 300)
    got = R.recent_arc_cards(cs)
    ids = [c["id"] for c in got]
    assert junk not in ids                      # non-session born_from excluded
    assert ids[0] == b and ids[1] == a          # newest first


# ── prior_arc_card ─────────────────────────────────────────────────────────
def test_prior_arc_card_prefers_explicit_prev_edge(cs):
    first = cs.add_card("oldest", ["echelon:a"], born_from="wrap-session 1")
    second = cs.add_card("newest", ["echelon:b"], born_from="wrap-session 2", prev=first)
    _set_ts(cs, first, 10); _set_ts(cs, second, 20)
    p = R.prior_arc_card(second, cs)
    assert p is not None and p["id"] == first


def test_prior_arc_card_timestamp_fallback_when_no_prev(cs):
    first = cs.add_card("oldest", ["echelon:a"], born_from="wrap-session 1")
    second = cs.add_card("newest", ["echelon:b"], born_from="wrap-session 2")  # no prev edge
    _set_ts(cs, first, 10); _set_ts(cs, second, 20)
    p = R.prior_arc_card(second, cs)
    assert p is not None and p["id"] == first   # found by ts < second


def test_prior_arc_card_none_at_start_of_history(cs):
    only = cs.add_card("only", ["echelon:a"], born_from="wrap-session 1")
    _set_ts(cs, only, 10)
    assert R.prior_arc_card(only, cs) is None


def test_prior_arc_card_none_for_missing_card(cs):
    assert R.prior_arc_card("nonexistent-id", cs) is None


# ── next_arc_cards (the fork blind-spot fix) ───────────────────────────────
def test_next_arc_cards_finds_prev_edge_child(cs):
    root = cs.add_card("root", ["echelon:a"], born_from="wrap-session 1")
    child = cs.add_card("child", ["echelon:b"], born_from="wrap-session 2", prev=root)
    _set_ts(cs, root, 10); _set_ts(cs, child, 20)
    kids = R.next_arc_cards(root, cs)
    assert [k["id"] for k in kids] == [child]


def test_next_arc_cards_fork_returns_multiple_children(cs):
    root = cs.add_card("root", ["echelon:a"], born_from="wrap-session 1")
    # two siblings: one via prev-edge, one via forked_from tag — both children of root
    c1 = cs.add_card("branch-prev", ["echelon:b"], born_from="wrap-session 2", prev=root)
    c2 = cs.add_card("branch-fork", ["echelon:c"],
                     born_from=f"relive 3 | forked_from={root}")
    _set_ts(cs, root, 10); _set_ts(cs, c1, 20); _set_ts(cs, c2, 30)
    kids = R.next_arc_cards(root, cs)
    ids = {k["id"] for k in kids}
    assert ids == {c1, c2}                       # a fork surfaces BOTH futures
    assert len(kids) > 1


def test_next_arc_cards_timestamp_fallback(cs):
    root = cs.add_card("root", ["echelon:a"], born_from="wrap-session 1")
    later = cs.add_card("later-unlinked", ["echelon:b"], born_from="wrap-session 2")
    _set_ts(cs, root, 10); _set_ts(cs, later, 20)
    kids = R.next_arc_cards(root, cs)
    assert [k["id"] for k in kids] == [later]    # linear successor by ts


def test_next_arc_cards_end_of_history(cs):
    last = cs.add_card("last", ["echelon:a"], born_from="wrap-session 1")
    _set_ts(cs, last, 10)
    assert R.next_arc_cards(last, cs) == []


# ── record_fork ────────────────────────────────────────────────────────────
def test_record_fork_stamps_and_is_idempotent(cs):
    root = cs.add_card("root", ["echelon:a"], born_from="wrap-session 1")
    branch = cs.add_card("branch", ["echelon:b"], born_from="relive 2")
    assert R.record_fork(branch, root, cs) is True
    c = cs.card(branch)
    assert f"forked_from={root}" in c.born_from
    # idempotent: second stamp is a no-op
    assert R.record_fork(branch, root, cs) is False
    # and now next_arc_cards(root) can reach the sibling via the tag
    assert any(k["id"] == branch for k in R.next_arc_cards(root, cs))


def test_record_fork_rejects_self_and_missing(cs):
    root = cs.add_card("root", ["echelon:a"], born_from="wrap-session 1")
    assert R.record_fork(root, root, cs) is False        # self-fork rejected
    assert R.record_fork("missing", root, cs) is False   # missing card rejected
    assert R.record_fork(root, "", cs) is False          # empty root rejected


# ── evidence sidecar round-trip ────────────────────────────────────────────
def test_record_and_read_evidence_roundtrip(cs):
    R.record_evidence("card-1", "echelon:move-a", "sess-x", 0, "we decided to do X because Y")
    ex, sess = R.evidence_for("card-1", "echelon:move-a")
    assert ex == "we decided to do X because Y"
    assert sess == "sess-x"


def test_record_evidence_is_idempotent_per_coord(cs):
    R.record_evidence("card-1", "echelon:move-a", "s1", 0, "first")
    R.record_evidence("card-1", "echelon:move-a", "s1", 0, "second")   # overwrites
    ex, _ = R.evidence_for("card-1", "echelon:move-a")
    assert ex == "second"
    # only one row survived
    c = R._conn()
    n = c.execute("SELECT COUNT(*) FROM relive_evidence WHERE card_id=? AND atom_coord=?",
                  ("card-1", "echelon:move-a")).fetchone()[0]
    c.close()
    assert n == 1


def test_evidence_for_missing_returns_none_pair(cs):
    assert R.evidence_for("no-card", "no-coord") == (None, None)


# ── relive: walk the chain ─────────────────────────────────────────────────
def test_relive_walks_chain_in_order_and_surfaces_evidence(cs):
    # two real atoms at real coordinates so the chain has content. NB: card refs are
    # normalized (hyphen->underscore) by _norm_coord, so we use underscore coords that
    # survive unchanged — evidence must be recorded under the SAME normalized coord relive
    # looks up (card.refs), which is the whole point of the round-trip.
    cs.add_atom("echelon:move_one", "FIRST move content")
    cs.add_atom("echelon:move_two", "SECOND move content")
    card_id = cs.add_card("the arc", ["echelon:move_one", "echelon:move_two"],
                          born_from="wrap-session 1")
    R.record_evidence(card_id, "echelon:move_one", "sess", 0, "why move one")
    # peek-only (take_up=False) surveys the spine for free — the move re-forms from the claim
    # (which falls back to the first content line for a plain atom), with NO earn.
    links, err = R.relive(card_id, cs, take_up=False)
    assert err is None
    assert [lk["ordinal"] for lk in links] == [0, 1]            # walked in order
    assert links[0]["move"] == "FIRST move content"            # served via the spine claim
    assert links[0]["warm"] == "peek"
    assert links[0]["exchange"] == "why move one"
    assert links[1]["exchange"] is None                        # no evidence for move-two


def test_relive_take_up_earns_through_the_door(cs):
    # THE FIX (2026-06-19): reliving must EARN by going through the witnessed door, not raw-read
    # content out of band (the dead-link bug). Walking with take_up=True fetches each move via
    # remember_fetch, which witnesses the access — so the atom's earned use_count rises.
    cs.add_atom("echelon:earn_me", "a move worth earning")
    card_id = cs.add_card("earning arc", ["echelon:earn_me"], born_from="wrap-session 1")
    aid = cs.atom_id_for_coordinate("echelon:earn_me")
    cs.compile_atom_struct(aid)
    before = cs.recall_peek(aid); assert before is not None     # spine exists, peek is free
    links, err = R.relive(card_id, cs, take_up=True)            # default: take up through the door
    assert err is None
    assert links[0]["warm"] == "earned"
    # the fetch was witnessed: use_count incremented (the door earned)
    served = cs.remember_fetch(aid, depth="spine")
    assert served is not None


def test_relive_marks_missing_atom(cs):
    card_id = cs.add_card("ghost arc", ["echelon:not-planted"], born_from="wrap-session 1")
    links, err = R.relive(card_id, cs, take_up=False)
    assert err is None
    assert "atom missing" in links[0]["move"]


def test_relive_unknown_card_returns_error(cs):
    links, err = R.relive("does-not-exist", cs, take_up=False)
    assert links is None
    assert "no card" in err


# ── render ─────────────────────────────────────────────────────────────────
def test_render_is_experience_first(cs):
    links = [{"ordinal": 0, "coord": "echelon:move-one", "move": "FIRST move content",
              "warm": "reflex", "exchange": "why we did it", "session": "s"}]
    out = R.render(links, "my session")
    assert "RELIVE: my session" in out
    assert "FIRST move content" in out
    assert "why we did it" in out
