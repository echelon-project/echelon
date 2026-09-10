"""S8b V8b — the foreign-scope edge guard at seed-and-link (tests/test_store_links.py).

The defect: a [[slug]] ref resolved by scanning EVERY scope's atoms (newest match
wins), so a slug that exists in two estates silently linked across estates — the
232 edge_to_disclaimed on `echelon scan --scope echelon` (echelon atoms whose refs
edge points at a DISCLAIMED atom in mol/gamma-support/...). The fix is the guard at the
door: a ref resolves FIRST within the authoring atom's scope; a bare slug with no
same-scope match is LEFT UNLINKED and recorded in store.last_foreign_skips as
(name, scope, best_foreign_id); an explicit [[scope:…]] coordinate still resolves
cross-scope (the author meant it). NO heal/tombstone of the historical edges —
scan still reports the 232 honestly. Owner ruling 2026-08-29.
"""
import time

import pytest

from echelon_engine.atoms.store import SeedStore


@pytest.fixture
def link_store(tmp_path):
    # both stores redirected to scratch (the test_store.py isolation pattern)
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


def refs_to(store, atom_id):
    """The v2 atom ids this atom's LIVE out `refs` edges point at."""
    return [e["to_id"] for e in store.cards.edges_of(atom_id)
            if e["dir"] == "out" and e["relation"] == "refs"]


# ── the exact bug: same slug in two estates, foreign one newer ─────────────
def test_bare_slug_links_within_own_scope_not_foreign_newer(link_store):
    # THE EXACT BUG: the same slug exists in BOTH estates, and the foreign (beta)
    # atom is NEWER. A's atom must still link to A's own same-slug atom — never
    # across estates (newest-match-wins over every scope was the defect).
    link_store.remember("alpha", "alpha's shared lesson", coordinate="alpha:shared")
    link_store.remember("beta", "beta's shared lesson", coordinate="beta:shared")
    a_v2 = link_store.cards.atom_id_for_coordinate("alpha:shared")
    b_v2 = link_store.cards.atom_id_for_coordinate("beta:shared")
    # make B provably newer than A (ts has second granularity — bump it past now)
    with link_store.cards._lock:
        link_store.cards.conn.execute("UPDATE atoms SET ts=? WHERE id=?",
                                      (int(time.time()) + 3600, b_v2))
        link_store.cards.conn.commit()
    # the un-guarded cross-scope resolver WOULD pick B (the bug precondition, proven)
    assert link_store.cards.atom_id_for_coordinate("shared") == b_v2
    link_store.remember("alpha", "alpha consumer [[shared]]", coordinate="alpha:consumer-x")
    x_v2 = link_store.cards.atom_id_for_coordinate("alpha:consumer-x")
    assert refs_to(link_store, x_v2) == [a_v2]          # A's atom, not B's


# ── bare slug that exists ONLY in a foreign scope ──────────────────────────
def test_bare_slug_only_foreign_leaves_no_edge_and_records_skip(link_store):
    link_store.remember("beta", "beta's only-b lesson", coordinate="beta:only-b")
    b_v2 = link_store.cards.atom_id_for_coordinate("beta:only-b")
    link_store.remember("alpha", "alpha consumer [[only-b]]", coordinate="alpha:ref-x")
    x_v2 = link_store.cards.atom_id_for_coordinate("alpha:ref-x")
    assert refs_to(link_store, x_v2) == []             # no edge — never a cross-estate link
    assert link_store.last_foreign_skips == [("only-b", "alpha", b_v2)]   # one recorded skip


# ── an explicit [[scope:…]] coordinate still links cross-scope ─────────────
def test_explicit_scope_coordinate_still_links_cross_scope(link_store):
    link_store.remember("beta", "beta's explicit lesson", coordinate="beta:explicit")
    b_v2 = link_store.cards.atom_id_for_coordinate("beta:explicit")
    link_store.remember("alpha", "alpha consumer [[beta:explicit]]", coordinate="alpha:ref-y")
    x_v2 = link_store.cards.atom_id_for_coordinate("alpha:ref-y")
    assert refs_to(link_store, x_v2) == [b_v2]         # [[scope:…]] means it — by intent


# ── no regression: own-scope slug links; intra-batch forward refs resolve ──
def test_slug_in_own_scope_only_links_and_intrabatch_resolves(link_store):
    link_store.remember("alpha", "alpha's local lesson", coordinate="alpha:local")
    l_v2 = link_store.cards.atom_id_for_coordinate("alpha:local")
    link_store.remember("alpha", "alpha consumer [[local]]", coordinate="alpha:ref-z")
    x_v2 = link_store.cards.atom_id_for_coordinate("alpha:ref-z")
    assert refs_to(link_store, x_v2) == [l_v2]         # no regression
    # intra-batch: two atoms in ONE remember_many referencing each other still resolve
    link_store.remember_many([
        {"scope": "alpha", "content": "alpha batch one [[two]]", "coordinate": "alpha:one"},
        {"scope": "alpha", "content": "alpha batch two [[one]]", "coordinate": "alpha:two"},
    ])
    one_v2 = link_store.cards.atom_id_for_coordinate("alpha:one")
    two_v2 = link_store.cards.atom_id_for_coordinate("alpha:two")
    assert set(refs_to(link_store, one_v2)) == {two_v2}
    assert set(refs_to(link_store, two_v2)) == {one_v2}
    assert link_store.last_foreign_skips == []


# ── the ingest summary line (end-to-end through ingest_folder) ─────────────
def test_ingest_summary_prints_foreign_skip_line(tmp_path, monkeypatch, capsys):
    from echelon_engine.atoms import ingest as ingest_mod
    from echelon_engine.atoms import store as store_mod
    monkeypatch.setattr(store_mod, "DEFAULT_V2_DB", tmp_path / "core_v2.db")
    monkeypatch.setattr(store_mod, "DEFAULT_DB", tmp_path / "core.db")
    monkeypatch.setenv("ECHELON_INGEST_SCOPE", "1")   # the canonicalization-gate override
    # a foreign-scope atom already holds the slug
    bank = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    bank.remember("beta", "beta holds the slug", coordinate="beta:shared-slug")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "consumer.md").write_text(
        "---\n"
        "name: consumer\n"
        "description: a consumer atom\n"
        "metadata:\n"
        "  type: note\n"
        "---\n"
        "This references [[shared-slug]] which lives only in beta.\n",
        encoding="utf-8")
    ingest_mod.ingest_folder(tmp_path, "alpha")
    out = capsys.readouterr().out
    assert "1 foreign-scope ref occurrence(s) / 1 distinct slug(s) left unlinked" in out
    assert "[[shared-slug]]" in out
    side = mem / "_foreign-skips.txt"                      # gate r1 V8b S-5: the full queue
    assert side.exists() and side.read_text(encoding="utf-8").startswith("[[shared-slug]] -> foreign ")
    # S-4: a clean ingest is silent about foreign skips
    (mem / "consumer.md").unlink()
    (mem / "clean.md").write_text(
        "---\nname: clean\ndescription: d\nmetadata:\n  type: note\n---\nno refs\n", encoding="utf-8")
    capsys.readouterr()
    ingest_mod.ingest_folder(tmp_path, "alpha")
    assert "foreign-scope" not in capsys.readouterr().out
    # and the guard really left the cross-estate edge unlinked
    cons_v2 = bank.cards.atom_id_for_coordinate("alpha:consumer")
    assert refs_to(bank, cons_v2) == []


# ── gate r1 V8b M-1: a colon-bearing coordinate present in BOTH scopes must still prefer own ──
def test_colon_coordinate_in_two_scopes_prefers_own_scope(link_store):
    link_store.remember("beta", "beta's older twin", coordinate="lesson:shared:twin")
    time.sleep(0.01)
    link_store.remember("alpha", "alpha's own twin", coordinate="lesson:shared:twin")
    a_v2 = link_store.cards.conn.execute(
        "SELECT id FROM atoms WHERE scope='alpha' AND coordinate='lesson:shared:twin'").fetchone()["id"]
    time.sleep(0.01)
    link_store.remember("beta", "beta's NEWER twin", coordinate="lesson:shared:twin")  # foreign, newest
    link_store.remember("alpha", "alpha consumer [[lesson:shared:twin]]", coordinate="alpha:ref-z")
    x_v2 = link_store.cards.atom_id_for_coordinate("alpha:ref-z")
    assert refs_to(link_store, x_v2) == [a_v2]         # own scope wins even though beta's is newer
