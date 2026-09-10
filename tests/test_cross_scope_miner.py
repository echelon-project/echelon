"""Tests for the cross-scope near-dup miner (smart-recall slice 1). Pure/network-free — builds a tiny
in-memory SQLite bank with the atoms + atom_spine + atom_sidecar shape and asserts the mechanical
behavior: vectorized cosine, cross-scope filtering, union-find clustering, mechanical pre-sort, and the
READ-ONLY-except-sidecar discipline. numpy is a hard dependency of the miner (spec)."""
import json
import sqlite3

import pytest

np = pytest.importorskip("numpy")   # spec: vectorized cosine requires numpy

from echelon_engine.atoms import cross_scope_miner as csm


def _bank():
    """A minimal in-memory bank with the three tables the miner reads/writes."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE atoms (id TEXT PRIMARY KEY, coordinate TEXT, content TEXT, score REAL,
            score_history TEXT, use_count INT, born_from TEXT, ts INT, scope TEXT, kind TEXT,
            valence REAL, arousal REAL);
        CREATE TABLE atom_spine (atom_id TEXT PRIMARY KEY, slug TEXT, claim TEXT, directive TEXT,
            compiler_version INT, compiled_ts INT, claim_confidence REAL, scope TEXT, witness TEXT);
        CREATE TABLE atom_sidecar (atom_id TEXT PRIMARY KEY, embedding TEXT NOT NULL DEFAULT '',
            trigger_signals TEXT NOT NULL DEFAULT '[]');
    """)
    return c


def _add(c, aid, scope, slug, claim, kind="lesson", born_from="", directive=""):
    c.execute("INSERT INTO atoms (id,coordinate,scope,kind,born_from,ts,score) VALUES (?,?,?,?,?,?,?)",
              (aid, f"c:{aid}", scope, kind, born_from, 0, 100.0))
    c.execute("INSERT INTO atom_spine (atom_id,slug,claim,directive,scope) VALUES (?,?,?,?,?)",
              (aid, slug, claim, directive, scope))
    c.commit()


# ── the hashing embedder is deterministic + uniform-dim + normalized ───────────────────────────────
def test_hash_embed_deterministic_and_normalized():
    v1 = csm.hash_embed("the silent floor swallowed the auth error")
    v2 = csm.hash_embed("the silent floor swallowed the auth error")
    assert np.allclose(v1, v2)                       # deterministic — resumable/testable
    assert v1.shape == (csm.EMBED_DIM,)              # uniform dim
    assert abs(np.linalg.norm(v1) - 1.0) < 1e-9      # L2-normalized


def test_hash_embed_empty_is_zero():
    v = csm.hash_embed("")
    assert np.linalg.norm(v) == 0.0


def test_identical_text_cosine_is_one():
    a, b = csm.hash_embed("floor key revoked silent degrade"), csm.hash_embed("floor key revoked silent degrade")
    assert float(a @ b) == pytest.approx(1.0)


# ── the vectorized near-dup pass finds ONLY cross-scope pairs ───────────────────────────────────────
def test_near_dup_is_cross_scope_only():
    c = _bank()
    # two near-identical atoms in DIFFERENT scopes (the regression) + a same-scope dup (must be ignored)
    _add(c, "a1", "echelon", "silent-floor", "the floor swallowed the auth error silently degrade lexical")
    _add(c, "a2", "flux", "quiet-floor", "the floor swallowed the auth error silently degrade lexical")
    _add(c, "a3", "flux", "quiet-floor-2", "the floor swallowed the auth error silently degrade lexical")
    _add(c, "z1", "echelon", "unrelated", "cats sleep on warm keyboards in the afternoon sun")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c   # reuse the built bank
    atoms = m.load_atoms()
    mat = m.embed_matrix(atoms)
    pairs = m.near_dup_pairs(atoms, mat, threshold=0.9)
    idx = {a.atom_id: i for i, a in enumerate(atoms)}
    got = {tuple(sorted((atoms[i].atom_id, atoms[j].atom_id))) for i, j, _ in pairs}
    assert ("a1", "a2") in got                       # cross-scope near-dup surfaced
    assert ("a1", "a3") in got                       # cross-scope (echelon vs flux) surfaced
    assert ("a2", "a3") not in got                   # SAME-scope (flux/flux) filtered out
    assert not any("z1" in p for p in got)           # unrelated atom never pairs


def test_clustering_unions_multi_scope_component():
    c = _bank()
    _add(c, "a1", "echelon", "s", "alpha beta gamma delta epsilon zeta trap repeated pattern here")
    _add(c, "a2", "flux",    "s", "alpha beta gamma delta epsilon zeta trap repeated pattern here")
    _add(c, "a3", "mol",     "s", "alpha beta gamma delta epsilon zeta trap repeated pattern here")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    mat = m.embed_matrix(atoms)
    clusters = m.cluster(atoms, m.near_dup_pairs(atoms, mat, threshold=0.9))
    assert len(clusters) == 1
    assert set(clusters[0].scopes) == {"echelon", "flux", "mol"}
    assert clusters[0].presort["scope_count"] == 3


# ── mechanical pre-sort (evidence, not classification) ─────────────────────────────────────────────
def test_presort_flags_born_from_failure():
    c = _bank()
    _add(c, "t1", "echelon", "trap-a", "swallowed the error silently a repeated trap",
         born_from="judged:disclaimed dream")
    _add(c, "t2", "flux", "trap-b", "swallowed the error silently a repeated trap",
         born_from="dispute superseded")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    clusters = m.cluster(atoms, m.near_dup_pairs(atoms, m.embed_matrix(atoms), threshold=0.9))
    assert len(clusters) == 1
    p = clusters[0].presort
    assert p["all_born_from_failure"] is True        # both born from failure -> trap candidate
    assert p["born_from_failure_count"] == 2


def test_presort_echo_hint_same_kind_no_failure():
    c = _bank()
    _add(c, "e1", "echelon", "doctrine-a", "delegation is intelligence boundary spend on class problems",
         kind="cv", born_from="")
    _add(c, "e2", "flux", "doctrine-b", "delegation is intelligence boundary spend on class problems",
         kind="cv", born_from="")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    clusters = m.cluster(atoms, m.near_dup_pairs(atoms, m.embed_matrix(atoms), threshold=0.9))
    p = clusters[0].presort
    assert p["same_kind"] is True and p["any_born_from_failure"] is False   # echo hint, not trap


# ── sidecar fill is additive + tagged + respects only_empty (READ-ONLY discipline) ─────────────────
def test_presort_flags_test_scope_pollution():
    c = _bank()
    _add(c, "s1", "synth-test", "x", "the vault mechanism uses ephemeral tokens and honeypot lures")
    _add(c, "s2", "wire-test", "y", "the vault mechanism uses ephemeral tokens and honeypot lures")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    clusters = m.cluster(atoms, m.near_dup_pairs(atoms, m.embed_matrix(atoms), threshold=0.9))
    p = clusters[0].presort
    assert p["has_test_scope"] is True
    assert p["all_test_scopes"] is True              # both members in throwaway scopes -> gate discounts
    assert set(p["test_scopes"]) == {"synth-test", "wire-test"}


def test_real_scope_not_flagged_as_test():
    c = _bank()
    _add(c, "r1", "echelon", "x", "delegation is the intelligence boundary keep the top agent free")
    _add(c, "r2", "flux", "y", "delegation is the intelligence boundary keep the top agent free")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    clusters = m.cluster(atoms, m.near_dup_pairs(atoms, m.embed_matrix(atoms), threshold=0.9))
    assert clusters[0].presort["has_test_scope"] is False


def test_fill_sidecar_only_empty_and_tagged():
    c = _bank()
    _add(c, "a1", "echelon", "s", "some claim text here for embedding")
    # pre-seed a1 with a legacy vector -> only_empty must NOT clobber it
    c.execute("INSERT INTO atom_sidecar (atom_id, embedding) VALUES (?,?)", ("a1", "LEGACY"))
    _add(c, "a2", "flux", "s2", "another distinct claim for embedding here")
    c.commit()
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    written = m.fill_sidecar(atoms, m.embed_matrix(atoms), only_empty=True)
    assert written == 1                              # only a2 filled; a1's legacy vector preserved
    a1 = c.execute("SELECT embedding FROM atom_sidecar WHERE atom_id='a1'").fetchone()["embedding"]
    a2 = c.execute("SELECT embedding FROM atom_sidecar WHERE atom_id='a2'").fetchone()["embedding"]
    assert a1 == "LEGACY"                             # untouched
    payload = json.loads(a2)
    assert payload["model"] == csm.EMBED_MODEL and len(payload["vec"]) == csm.EMBED_DIM


def test_atoms_table_is_read_only():
    """The miner must NEVER write atoms/atom_spine. Snapshot both before/after a full run."""
    c = _bank()
    _add(c, "a1", "echelon", "s", "claim one for the read only test here now")
    _add(c, "a2", "flux", "s2", "claim two for the read only test here now")
    before_atoms = c.execute("SELECT * FROM atoms").fetchall()
    before_spine = c.execute("SELECT * FROM atom_spine").fetchall()
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    atoms = m.load_atoms()
    m.fill_sidecar(atoms, m.embed_matrix(atoms))
    after_atoms = c.execute("SELECT * FROM atoms").fetchall()
    after_spine = c.execute("SELECT * FROM atom_spine").fetchall()
    assert [dict(r) for r in before_atoms] == [dict(r) for r in after_atoms]
    assert [dict(r) for r in before_spine] == [dict(r) for r in after_spine]


# ── end-to-end run() shape ─────────────────────────────────────────────────────────────────────────
def test_run_produces_report_shape():
    c = _bank()
    _add(c, "a1", "echelon", "s", "repeated cross scope trap alpha beta gamma silent swallow",
         born_from="disclaimed")
    _add(c, "a2", "flux", "s2", "repeated cross scope trap alpha beta gamma silent swallow",
         born_from="disclaimed")
    m = csm.CrossScopeMiner(":memory:"); m.conn = c
    rep = m.run(threshold=0.9, fill=True)
    assert rep["atom_count"] == 2
    assert rep["cross_scope_cluster_count"] == 1
    assert rep["embed_model"] == csm.EMBED_MODEL
    assert "total" in rep["runtime_seconds"]
    assert rep["clusters"][0]["presort"]["all_born_from_failure"] is True
