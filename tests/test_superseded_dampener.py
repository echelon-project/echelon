"""Superseded dampener tests (2026-08-12).

The gap this closes: warmth() honored disclaimed (dropped) and dormant (filtered)
but not SUPERSEDED — an atom with a live successor ranked as if current, so a stale
June handoff outranked the August wrap on lexical overlap (the shaky-nerve symptom,
OPEN-context-economy-program item 2). Claim-level and structural: the dampener reads
the live `supersedes` EDGE, never the clock — the memanto council guard (recency is a
filter, not a re-weighting) stands untouched.

Tests run on temp DBs only — the live bank is untouched.
"""
import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.seed_types import Seed
from echelon_engine.atoms.warmth import warmth, SUPERSEDED_DAMP


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def cards(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


# ── superseded_content_set: the bridge ─────────────────────────────────────

def test_live_supersedes_target_is_in_set(cards):
    old = cards.add_atom("test:old_handoff", "the standing next-session handoff list of open items")
    new = cards.add_atom("test:new_wrap", "resume menu: the latest wrap carrying the live open items")
    assert cards.link(new, old, "supersedes")
    sup = cards.superseded_content_set()
    a_old = cards.get_atom(old)
    a_new = cards.get_atom(new)
    assert (a_old.content or "")[:120] in sup
    assert (a_new.content or "")[:120] not in sup  # the successor is never dampened


def test_tombstoned_edge_leaves_the_set(cards):
    old = cards.add_atom("test:old2", "an old lesson later superseded then the edge retired")
    new = cards.add_atom("test:new2", "the newer lesson that once superseded the old one")
    cards.link(new, old, "supersedes")
    assert (cards.get_atom(old).content or "")[:120] in cards.superseded_content_set()
    cards.unlink(new, old, "supersedes")   # tombstone, never DELETE
    assert (cards.get_atom(old).content or "")[:120] not in cards.superseded_content_set()


def test_live_bearer_vetoes_shared_prefix(cards):
    """Prefixes collide across duplicate bodies (content-prefix-bridges-collide-on-dup-bodies):
    a prefix enters the set only when EVERY bearer is superseded."""
    body = "shared first line of a duplicated handoff body " * 4  # > 120 chars
    old = cards.add_atom("test:dup_old", body + " (superseded copy)")
    live = cards.add_atom("test:dup_live", body + " (live copy)")
    new = cards.add_atom("test:dup_new", "a different successor body")
    cards.link(new, old, "supersedes")
    a_old, a_live = cards.get_atom(old), cards.get_atom(live)
    assert (a_old.content or "")[:120] == (a_live.content or "")[:120]  # collision is real
    assert (a_old.content or "")[:120] not in cards.superseded_content_set()  # veto holds


def test_self_loop_edge_is_ignored(cards):
    """Legacy data noise: an atom 'superseding itself' must not hide itself.
    link() rejects self-edges, so plant the noise row raw — as the live bank carries."""
    aid = cards.add_atom("test:self_loop", "an atom with a legacy self-supersede edge")
    cards.conn.execute(
        "INSERT INTO atom_links (from_id,to_id,relation,ts,superseded_on) VALUES (?,?,?,0,0)",
        (aid, aid, "supersedes"))
    cards.conn.commit()
    assert (cards.get_atom(aid).content or "")[:120] not in cards.superseded_content_set()


def test_scope_filter(cards):
    old = cards.add_atom("test:scoped_old", "scoped superseded atom body", scope="alpha")
    new = cards.add_atom("test:scoped_new", "scoped successor atom body", scope="alpha")
    cards.link(new, old, "supersedes")
    ck = (cards.get_atom(old).content or "")[:120]
    assert ck in cards.superseded_content_set(scope="alpha")
    assert ck not in cards.superseded_content_set(scope="beta")


# ── warmth(): the rank application ──────────────────────────────────────────

OLD_CONTENT = ("the standing next-session handoff read first after warmup open items "
               "swarm hardening six items each with why")
NEW_CONTENT = ("resume menu latest session wrap open items numbered next session reads "
               "this handoff first")
QUERY = "open items next session handoff read first"


class _FakeStore:
    """Minimal SeedStore shape warmth() touches: seeds(), links_of(), .cards."""
    def __init__(self, seeds, cards):
        self._seeds, self.cards = seeds, cards

    def seeds(self, scope=None, tier=None):
        return [s for s in self._seeds if scope is None or s.scope == scope]

    def links_of(self, seed_id):
        return []


def _mk_store(cards, scope="echelon"):
    seeds = [Seed(scope=scope, content=OLD_CONTENT, kind="note"),
             Seed(scope=scope, content=NEW_CONTENT, kind="note")]
    return _FakeStore(seeds, cards), seeds


def test_superseded_seed_cannot_outrank_its_successor(cards):
    old = cards.add_atom("echelon:old_handoff", OLD_CONTENT, scope="echelon")
    new = cards.add_atom("echelon:new_wrap", NEW_CONTENT, scope="echelon")
    cards.link(new, old, "supersedes")
    store, _ = _mk_store(cards)
    r = warmth(QUERY, store, "echelon", top_k=3)
    assert r.warmest, "fixture must produce recognition"
    assert r.warmest[0].seed.content == NEW_CONTENT, (
        "the live successor must rank above the superseded handoff")
    scores = {sw.seed.content: sw.score for sw in r.warmest}
    assert scores[OLD_CONTENT] < scores[NEW_CONTENT]


def test_control_without_live_edge_old_outranks(cards):
    """THE CONTROL (anti-no-op / mutation check): the same fixture with the edge
    tombstoned restores pure lexical order — proving the dampener, and only the
    dampener, flips the ranking. If this control ever fails, the fixture no longer
    exercises the dampener and the green above is meaningless."""
    old = cards.add_atom("echelon:old_handoff", OLD_CONTENT, scope="echelon")
    new = cards.add_atom("echelon:new_wrap", NEW_CONTENT, scope="echelon")
    cards.link(new, old, "supersedes")
    cards.unlink(new, old, "supersedes")
    store, _ = _mk_store(cards)
    r = warmth(QUERY, store, "echelon", top_k=3)
    assert r.warmest[0].seed.content == OLD_CONTENT, (
        "control: without a live edge the old handoff must win on lexical overlap "
        "— if it doesn't, the integration fixture proves nothing")


def test_dampened_seed_still_surfaces_when_alone(cards):
    """DAMPEN, not drop: history stays reachable when it is the only recognition."""
    old = cards.add_atom("echelon:lone_old", OLD_CONTENT, scope="echelon")
    new = cards.add_atom("echelon:lone_new", "completely unrelated successor body", scope="echelon")
    cards.link(new, old, "supersedes")
    store = _FakeStore([Seed(scope="echelon", content=OLD_CONTENT, kind="note")], cards)
    r = warmth(QUERY, store, "echelon", top_k=3)
    assert r.warmest and r.warmest[0].seed.content == OLD_CONTENT
    assert r.warmest[0].score > 0


def test_damp_factor_is_applied_exactly(cards):
    old = cards.add_atom("echelon:exact_old", OLD_CONTENT, scope="echelon")
    new = cards.add_atom("echelon:exact_new", NEW_CONTENT, scope="echelon")
    store, _ = _mk_store(cards)
    base = {sw.seed.content: sw.score for sw in warmth(QUERY, store, "echelon", top_k=3).warmest}
    cards.link(new, old, "supersedes")
    damped = {sw.seed.content: sw.score for sw in warmth(QUERY, store, "echelon", top_k=3).warmest}
    assert damped[OLD_CONTENT] == pytest.approx(base[OLD_CONTENT] * SUPERSEDED_DAMP, abs=0.002)
    assert damped[NEW_CONTENT] == pytest.approx(base[NEW_CONTENT], abs=0.002)
