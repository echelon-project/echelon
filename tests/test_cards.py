"""Focused unit tests for echelon_engine.atoms.cards — the card/atom credit layer.

cards is the EARN-BY-TRACE store: atoms are born neutral and gain weight ONLY when a
card that LOADED them succeeds (credit flows back). It sits on top of uame (imports
compute_score/content_id) and under store. These focused tests pin the keystone:
born-neutral, credit-on-success, the partial-share to loaded atoms, the chain rule,
and the typed-edge immune scan.

Each test runs on a fresh tmp db. effective_score()'s v1-borrow is avoided by giving
atoms novel content + checking earned (use_count>0) atoms, which rank by their own score.
"""
import pytest

from echelon_engine.atoms.cards import (
    CardStore, Atom, Card, RELATION_TYPES, SCORE_K,
)
from echelon_engine.atoms.uame import SCORE_BENCHMARK


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    """These fixtures predate the empty-scope guard (2026-07-09) and plant atoms with no
    scope= on real-scope-shaped heads. The guard's law stays strict (the gate refused
    allowlisting fixture heads — memory:* IS the orphan mechanism); tests opt in HERE,
    per-test and auto-reverted, via the deliberate escape hatch."""
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


# ── born neutral ──────────────────────────────────────────────────────────
def test_atom_born_neutral(store):
    aid = store.add_atom("tooling:cli:flag", "use -X utf8")
    a = store.get_atom(aid)
    assert a is not None
    assert a.score == SCORE_BENCHMARK          # B=100, no weight asserted at birth
    assert a.use_count == 0


def test_add_atom_is_content_addressed(store):
    a1 = store.add_atom("tooling:x", "same content")
    a2 = store.add_atom("tooling:x", "same content")
    assert a1 == a2                            # same coordinate+content => same id, no dup


# ── the keystone: a card earns, and its loaded atoms earn a SHARE ─────────
def test_reinforce_card_moves_score_by_q_minus_50(store):
    store.add_atom("tooling:a", "atom a")
    cid = store.add_card("do-a", refs=["tooling:a"])
    before = store.card(cid).score
    res = store.reinforce_card(cid, q=100.0)   # full success
    assert res["ok"]
    after = store.card(cid).score
    # delta = (q-50)*K, applied through the decay-weighted score engine (so > before)
    assert after > before
    assert res["atoms_credited"] == 1


def test_atom_earns_only_through_a_card(store):
    aid = store.add_atom("tooling:a", "atom a")
    assert store.get_atom(aid).use_count == 0
    cid = store.add_card("do-a", refs=["tooling:a"])
    store.reinforce_card(cid, q=100.0)
    earned = store.get_atom(aid)
    assert earned.use_count > 0                 # the card's success credited the atom
    assert earned.score > SCORE_BENCHMARK       # it rose above neutral via trace


def test_failed_run_does_not_raise_the_atom(store):
    aid = store.add_atom("tooling:a", "atom a")
    cid = store.add_card("do-a", refs=["tooling:a"])
    store.reinforce_card(cid, q=0.0)            # q<50 => negative delta
    a = store.get_atom(aid)
    assert a.score < SCORE_BENCHMARK            # a failure pushes it BELOW neutral, not up


def test_credit_is_split_across_loaded_atoms(store):
    store.add_atom("tooling:a", "atom a")
    store.add_atom("tooling:b", "atom b")
    cid = store.add_card("do-ab", refs=["tooling:a", "tooling:b"])
    res = store.reinforce_card(cid, q=100.0)
    assert res["atoms_credited"] == 2          # each gets a 1/2 share, not the whole delta


# ── the chain rule: credit decays back along the prev edge ────────────────
def test_chain_credit_flows_back_decayed(store):
    store.add_atom("tooling:up", "upstream")
    store.add_atom("tooling:down", "downstream")
    up = store.add_card("up-card", refs=["tooling:up"])
    down = store.add_card("down-card", refs=["tooling:down"], prev=up)
    up_before = store.card(up).score
    res = store.reinforce_card(down, q=100.0)
    assert res["chain_credited"] == 1          # one hop back along prev
    assert store.card(up).score > up_before    # upstream earned a DECAYED share


def test_neutral_run_propagates_nothing(store):
    store.add_atom("tooling:up", "upstream")
    up = store.add_card("up-card", refs=["tooling:up"])
    down = store.add_card("down-card", refs=[], prev=up)
    up_before = store.card(up).score
    res = store.reinforce_card(down, q=50.0)   # q==50 => delta 0 => nothing moves
    assert res["chain_credited"] == 0
    assert store.card(up).score == up_before


# ── typed edges + the immune scan ─────────────────────────────────────────
def test_link_rejects_out_of_vocabulary_relation(store):
    a = store.add_atom("tooling:a", "a")
    b = store.add_atom("tooling:b", "b")
    with pytest.raises(ValueError, match="closed vocabulary"):
        store.link(a, b, relation="totally-made-up")


def test_link_and_edges_of_roundtrip(store):
    a = store.add_atom("tooling:a", "a")
    b = store.add_atom("tooling:b", "b")
    rel = sorted(RELATION_TYPES)[0]
    assert store.link(a, b, relation=rel) is True
    assert store.link(a, b, relation=rel) is False   # idempotent (INSERT OR IGNORE)
    edges = store.edges_of(a)
    assert any(e["to_id"] == b and e["dir"] == "out" for e in edges)


# ── scope migration (R-0154 slice 2, the dedup law) ─────────────────────────
def test_migrate_scope_retags_across_the_three_scope_tables(store):
    a = store.add_atom("lesson:framework_forge", "the forge door", scope="echelon")
    # spine + earned sidecars carry the scope column; seed them so the retag is provable
    store.conn.execute("INSERT INTO atom_spine (atom_id,slug,claim,scope) VALUES (?,?,?,?)",
                       (a, "framework_forge", "forge door", "echelon"))
    store.conn.execute("INSERT INTO atom_earned (atom_id,scope) VALUES (?,?)", (a, "echelon"))
    store.conn.commit()
    r = store.migrate_scope(["lesson:framework_forge"], "echelon-framework", from_scope="echelon")
    assert r["counts"] == {"atoms": 1, "atom_spine": 1, "atom_earned": 1}
    assert store.conn.execute("SELECT scope FROM atoms WHERE id=?", (a,)).fetchone()[0] == "echelon-framework"
    assert store.conn.execute("SELECT scope FROM atom_spine WHERE atom_id=?", (a,)).fetchone()[0] == "echelon-framework"
    assert store.conn.execute("SELECT scope FROM atom_earned WHERE atom_id=?", (a,)).fetchone()[0] == "echelon-framework"


def test_migrate_scope_preserves_edges_and_never_duplicates(store):
    a = store.add_atom("lesson:recall_hook", "recall before asking", scope="echelon")
    b = store.add_atom("lesson:bank_law", "bank where it belongs", scope="echelon")
    store.link(a, b, relation=sorted(RELATION_TYPES)[0])
    before = store.conn.execute("SELECT COUNT(*) FROM atom_links").fetchone()[0]
    total_before = store.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    store.migrate_scope(["lesson:recall_hook", "lesson:bank_law"], "echelon-memory", from_scope="echelon")
    # edges reference atom ids, so a retag keeps every link — and mints NO new atom (the dedup law).
    assert store.conn.execute("SELECT COUNT(*) FROM atom_links").fetchone()[0] == before
    assert store.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == total_before
    # no coordinate lives in both the old and the new scope (no split-scope atom)
    split = store.conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE scope='echelon' AND coordinate IN "
        "(SELECT coordinate FROM atoms WHERE scope='echelon-memory')").fetchone()[0]
    assert split == 0


def test_migrate_scope_dry_run_writes_nothing(store):
    a = store.add_atom("lesson:x", "x", scope="echelon")
    r = store.migrate_scope(["lesson:x"], "echelon-memory", from_scope="echelon", dry_run=True)
    assert r["dry_run"] and r["moved"] == ["lesson:x"] and r["counts"]["atoms"] == 0
    assert store.conn.execute("SELECT scope FROM atoms WHERE id=?", (a,)).fetchone()[0] == "echelon"


def test_migrate_scope_from_scope_guard_skips_wrong_scope(store):
    store.add_atom("lesson:y", "y", scope="other")
    r = store.migrate_scope(["lesson:y"], "echelon-memory", from_scope="echelon")
    assert r["moved"] == [] and r["skipped"] and r["counts"]["atoms"] == 0


def test_scan_clean_graph_has_no_failures(store):
    a = store.add_atom("tooling:a", "a")
    b = store.add_atom("tooling:b", "b")
    store.link(a, b, relation=sorted(RELATION_TYPES)[0])
    report = store.scan()
    assert report["fail"] == []                # a clean graph: no dangling/foreign edges


def test_scan_catches_dangling_edge(store):
    a = store.add_atom("tooling:a", "a")
    # edge to an atom id that was never planted (allowed at write — resolved at read)
    store.link(a, "deadbeefdeadbeef", relation=sorted(RELATION_TYPES)[0])
    report = store.scan()
    rules = {f["rule"] for f in report["fail"]}
    assert "dangling_edge_to" in rules
