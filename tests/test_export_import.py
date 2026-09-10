"""Round-trip tests for export/import bank portability.

Each test runs on a fresh temp db pair. The export must be EXACT — atom counts,
scores, timestamps, supersession links all preserved byte-for-byte, not just row
counts. The import must be TOLERANT but not lossy.
"""
import json
import os
import time

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.export_import import (
    export_bank, import_bank, EXPORT_VERSION,
    _engine_version, _coerce_value, _would_create_cycle,
)
from echelon_engine.atoms.uame import SCORE_BENCHMARK


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    """Allow empty scope for atoms created in tests (the empty-scope guard
    was added 2026-07-09; test fixtures opt in here)."""
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


def _seed_bank(db_path, scope="test", n=5):
    """Create a bank with known data for round-trip testing."""
    store = CardStore(db_path)
    atom_ids = []
    for i in range(n):
        aid = store.add_atom(
            f"tooling:test:{i}",
            f"test content item {i}",
            scope=scope,
            kind="note" if i % 2 == 0 else "lesson",
            valence=float(i) * 0.1,
            arousal=float(i % 3) * 0.1,
        )
        atom_ids.append(aid)
        time.sleep(0.001)  # ensure distinct timestamps

    # Add a supersession chain: atom[3] supersedes atom[2]
    if n >= 4:
        store.link(atom_ids[3], atom_ids[2], "supersedes")

    # Add a refs edge
    if n >= 2:
        store.link(atom_ids[0], atom_ids[1], "refs")

    store.conn.close()
    return atom_ids


# ── unit: _coerce_value ─────────────────────────────────────────────────────

def test_coerce_int():
    assert _coerce_value(42, "score", "INTEGER") == 42
    assert _coerce_value("42", "score", "INT") == 42
    assert _coerce_value(None, "score", "INTEGER") is None
    assert _coerce_value("not_a_number", "score", "INTEGER") == "not_a_number"


def test_coerce_real():
    assert _coerce_value(3.14, "valence", "REAL") == 3.14
    assert _coerce_value("2.5", "valence", "FLOAT") == 2.5


def test_coerce_text():
    assert _coerce_value("hello", "content", "TEXT") == "hello"
    assert _coerce_value(42, "content", "TEXT") == "42"


def test_coerce_unknown_type():
    # Bytes -> None (we never store blobs). Non-basic types -> str().
    assert _coerce_value(b"binary", "blob_col", "BLOB") is None  # bytes -> None
    result = _coerce_value({"key": "val"}, "future_col", "FUTURE")
    assert isinstance(result, str), f"expected str, got {type(result)}"


# ── unit: _would_create_cycle ────────────────────────────────────────────────

def test_no_cycle():
    """A simple chain a->b, b->c — adding a link between existing unrelated nodes."""
    db = ":memory:"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE atom_links (from_id TEXT, to_id TEXT, relation TEXT, superseded_on INTEGER DEFAULT 0)")
    # existing: a->b (supersedes)
    conn.execute("INSERT INTO atom_links VALUES ('a','b','supersedes',0)")
    # adding b->c should be fine
    assert not _would_create_cycle(conn, "b", "c", "supersedes")
    assert not _would_create_cycle(conn, "a", "b", "refs")  # refs don't check cycles
    conn.close()


def test_would_create_cycle():
    """Adding c->a when a->b->c chain exists creates a cycle."""
    db = ":memory:"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE atom_links (from_id TEXT, to_id TEXT, relation TEXT, superseded_on INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO atom_links VALUES ('a','b','supersedes',0)")
    conn.execute("INSERT INTO atom_links VALUES ('b','c','supersedes',0)")
    # c -> a would create cycle: a->b->c->a
    assert _would_create_cycle(conn, "c", "a", "supersedes")
    conn.close()


def test_superseded_edge_not_followed():
    """A tombstoned (superseded_on>0) edge is not traversed for cycle detection."""
    db = ":memory:"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE atom_links (from_id TEXT, to_id TEXT, relation TEXT, superseded_on INTEGER DEFAULT 0)")
    # a->b (LIVE), b->c (TOMBSTONED)
    conn.execute("INSERT INTO atom_links VALUES ('a','b','supersedes',0)")
    conn.execute("INSERT INTO atom_links VALUES ('b','c','supersedes',99999)")
    # c->a: would create cycle only if b->c were live. It's not -> no cycle.
    assert not _would_create_cycle(conn, "c", "a", "supersedes")
    conn.close()


# ── integration: export ──────────────────────────────────────────────────────

def test_export_has_manifest(tmp_path):
    db_path = str(tmp_path / "test.db")
    _seed_bank(db_path)
    out_path = str(tmp_path / "export.json")
    result = export_bank(db_path, out_path)
    assert result["total_rows"] > 0

    with open(out_path) as f:
        doc = json.load(f)
    m = doc["manifest"]
    assert m["format"] == "echelon-bank-export"
    assert m["export_version"] == EXPORT_VERSION
    assert "atoms" in m["tables"]
    assert "atom_links" in m["tables"]
    assert "scope_inventory" in m


def test_export_scope_filter(tmp_path):
    db_path = str(tmp_path / "test.db")
    _seed_bank(db_path, scope="alpha")
    _seed_bank(CardStore(db_path).db_path, scope="beta")

    out_path = str(tmp_path / "export.json")
    r = export_bank(db_path, out_path, scope="alpha")
    assert r["scope_filter"] == "alpha"

    with open(out_path) as f:
        doc = json.load(f)
    atoms = doc["data"]["atoms"]
    # All exported atoms should be scope=alpha
    for a in atoms:
        assert a["scope"] == "alpha"


# ── integration: import ──────────────────────────────────────────────────────

def test_import_dry_run_no_target(tmp_path):
    """Dry run against non-existent target reports only inserts, no conflicts."""
    db_path = str(tmp_path / "src.db")
    _seed_bank(db_path)
    out_path = str(tmp_path / "export.json")
    export_bank(db_path, out_path)

    target = str(tmp_path / "target.db")
    rep = import_bank(out_path, target, dry_run=True)
    assert rep["dry_run"] is True
    assert rep["total_inserted"] > 0
    assert rep["total_conflicts"] == 0
    # Target file should NOT have been created
    assert not os.path.exists(target)


def test_import_dry_run_existing_target(tmp_path):
    """Dry run against existing target detects conflicts."""
    db_path = str(tmp_path / "src.db")
    _seed_bank(db_path)
    out_path = str(tmp_path / "export.json")
    export_bank(db_path, out_path)

    # First real import creates the target
    target = str(tmp_path / "target.db")
    import_bank(out_path, target)  # real import

    # Dry run against populated target
    rep = import_bank(out_path, target, dry_run=True)
    assert rep["dry_run"] is True
    # All rows should conflict (already there)
    assert rep["total_conflicts"] > 0


def test_round_trip_equality(tmp_path):
    """Export a seeded bank, import into fresh bank — assert atom count, scores,
    timestamps, and supersession links are byte-for-byte identical."""
    src_db = str(tmp_path / "src.db")
    atom_ids = _seed_bank(src_db, n=5)

    # Get source data directly
    src_store = CardStore(src_db)
    src_atoms = {a.id: a for a in src_store.atoms_in_scope("test")}

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    dst_store = CardStore(dst_db)
    dst_atoms = {a.id: a for a in dst_store.atoms_in_scope("test")}

    # Same set of atom IDs
    assert set(src_atoms.keys()) == set(dst_atoms.keys()), \
        f"Atom ID mismatch: src={len(src_atoms)} dst={len(dst_atoms)}"

    for aid in src_atoms:
        s = src_atoms[aid]
        d = dst_atoms[aid]
        assert s.coordinate == d.coordinate, f"coordinate mismatch for {aid}"
        assert s.content == d.content, f"content mismatch for {aid}"
        assert s.scope == d.scope, f"scope mismatch for {aid}"
        assert s.kind == d.kind, f"kind mismatch for {aid}"
        assert s.ts == d.ts, f"timestamp mismatch for {aid}: {s.ts} vs {d.ts}"
        assert s.score == d.score, f"score mismatch for {aid}: {s.score} vs {d.score}"
        assert s.valence == d.valence, f"valence mismatch for {aid}"
        assert s.arousal == d.arousal, f"arousal mismatch for {aid}"
        assert s.born_from == d.born_from, f"born_from mismatch for {aid}"

    # Verify edges
    src_edges = set()
    with src_store.conn:
        for r in src_store.conn.execute(
            "SELECT from_id, to_id, relation FROM atom_links WHERE superseded_on=0").fetchall():
            src_edges.add((r[0], r[1], r[2]))

    dst_edges = set()
    with dst_store.conn:
        for r in dst_store.conn.execute(
            "SELECT from_id, to_id, relation FROM atom_links WHERE superseded_on=0").fetchall():
            dst_edges.add((r[0], r[1], r[2]))

    assert src_edges == dst_edges, \
        f"Edge mismatch: src={len(src_edges)} dst={len(dst_edges)}"

    src_store.conn.close()
    dst_store.conn.close()


def test_round_trip_preserves_timestamps(tmp_path):
    """Ensure timestamps are preserved EXACTLY — not re-stamped at import time."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=3)

    src_store = CardStore(src_db)
    src_ts = {}
    for a in src_store.atoms_in_scope("test"):
        src_ts[a.id] = a.ts
    src_store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    dst_store = CardStore(dst_db)
    for a in dst_store.atoms_in_scope("test"):
        assert a.ts == src_ts[a.id], \
            f"Timestamp re-stamped! {a.id}: src={src_ts[a.id]} dst={a.ts}"

    dst_store.conn.close()


def test_round_trip_preserves_scores(tmp_path):
    """Ensure scores are copied exactly, not re-derived at import time."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=3)

    # Modify score on one atom to be non-default
    src_store = CardStore(src_db)
    src_atoms = src_store.atoms_in_scope("test")
    # Use _mutate_score to change a score
    src_store._mutate_score("atoms", src_atoms[0].id, op="earn", delta=25.0)
    src_store.conn.commit()

    # Record scores
    src_scores = {}
    for a in src_store.atoms_in_scope("test"):
        src_scores[a.id] = a.score
    src_store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    dst_store = CardStore(dst_db)
    for a in dst_store.atoms_in_scope("test"):
        assert a.score == src_scores[a.id], \
            f"Score mismatch {a.id}: src={src_scores[a.id]} dst={a.score}"

    dst_store.conn.close()


def test_round_trip_preserves_supersession_links(tmp_path):
    """Supersession chain must survive the round-trip intact."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=5)

    src_store = CardStore(src_db)
    src_supersedes = set()
    with src_store.conn:
        for r in src_store.conn.execute(
            "SELECT from_id, to_id FROM atom_links WHERE relation='supersedes' AND superseded_on=0"
        ).fetchall():
            src_supersedes.add((r[0], r[1]))
    src_store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    dst_store = CardStore(dst_db)
    dst_supersedes = set()
    with dst_store.conn:
        for r in dst_store.conn.execute(
            "SELECT from_id, to_id FROM atom_links WHERE relation='supersedes' AND superseded_on=0"
        ).fetchall():
            dst_supersedes.add((r[0], r[1]))
    dst_store.conn.close()

    assert src_supersedes == dst_supersedes, \
        f"Supersession links lost: src={len(src_supersedes)} dst={len(dst_supersedes)}"


def test_merge_policy_skip(tmp_path):
    """merge-policy=skip: conflicting rows are ignored, no error."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=2)
    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    # First import
    import_bank(export_path, dst_db)
    atoms_before = CardStore(dst_db).count_atoms_in_scope("test")

    # Second import with skip
    rep = import_bank(export_path, dst_db, merge_policy="skip")
    atoms_after = CardStore(dst_db).count_atoms_in_scope("test")
    assert atoms_before == atoms_after, "skip policy should not duplicate atoms"
    assert rep["total_conflicts"] > 0, "should report conflicts"


def test_merge_policy_error(tmp_path):
    """merge-policy=error: conflicting rows are reported but import doesn't crash."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=2)
    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)  # first import
    # Second import with error policy — should report conflicts, not crash
    rep = import_bank(export_path, dst_db, merge_policy="error")
    assert rep["total_conflicts"] > 0


def test_cycle_observed_not_blocked_on_import(tmp_path):
    """Import restores the link graph VERBATIM. A tampered export containing a
    supersession cycle is imported intact (the cycle edge IS written), and a
    WARNING is emitted, but no edge is dropped. Cycle BLOCKING is for the live
    write path (creating a NEW link), not for restoring history."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=3)
    src_store = CardStore(src_db)
    # Create a chain: a -> b -> c (supersedes)
    src_atoms = src_store.atoms_in_scope("test")
    a_id = src_atoms[0].id
    b_id = src_atoms[1].id
    c_id = src_atoms[2].id
    src_store.link(a_id, b_id, "supersedes")
    src_store.link(b_id, c_id, "supersedes")
    src_store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    # Now tamper with the export to add c -> a cycle edge
    with open(export_path) as f:
        doc = json.load(f)
    doc["data"]["atom_links"].append({
        "from_id": c_id, "to_id": a_id, "relation": "supersedes",
        "ts": int(time.time()), "superseded_on": 0,
    })
    with open(export_path, "w") as f:
        json.dump(doc, f)

    dst_db = str(tmp_path / "dst.db")
    rep = import_bank(export_path, dst_db)
    # Cycle is OBSERVED (warning) but NOT blocked — edges imported verbatim
    assert rep["cycles_detected"] > 0, "should observe (warn about) the supersession cycle"
    assert "verbatim" in rep["cycles_skipped"][0], \
        "warning should mention 'imported verbatim'"

    # Verify the cycle edge WAS imported (not dropped)
    import sqlite3
    dst_conn = sqlite3.connect(dst_db)
    dst_conn.row_factory = sqlite3.Row
    edges = {(r["from_id"], r["to_id"], r["relation"])
             for r in dst_conn.execute("SELECT from_id, to_id, relation FROM atom_links").fetchall()}
    assert (c_id, a_id, "supersedes") in edges, \
        "cycle edge was dropped — import must restore verbatim"
    dst_conn.close()


def test_unknown_column_tolerance_existing_bank(tmp_path):
    """Import survives when the export has columns the target schema lacks
    AND the target is an EXISTING older bank (simulates importing a
    newer-format export into an older engine). Warn-skip is correct here."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=2)
    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    # Add a fake column to atoms in the export (simulates newer format)
    with open(export_path) as f:
        doc = json.load(f)
    doc["manifest"]["tables"]["atoms"]["columns"].append("future_column")
    for row in doc["data"]["atoms"]:
        row["future_column"] = "should_be_ignored"
    with open(export_path, "w") as f:
        json.dump(doc, f)

    # Create an EXISTING bank with only base DDL (simulates an older engine
    # that predates migration-added columns)
    dst_db = str(tmp_path / "dst.db")
    import sqlite3
    old_conn = sqlite3.connect(dst_db)
    from echelon_engine.atoms.cards import (
        _SCHEMA, _STRUCT_SCHEMA, _IMPRESSIONS_SCHEMA, _WRAP_REVIEW_SCHEMA,
    )
    old_conn.executescript(_SCHEMA)
    old_conn.executescript(_STRUCT_SCHEMA)
    old_conn.executescript(_IMPRESSIONS_SCHEMA)
    old_conn.executescript(_WRAP_REVIEW_SCHEMA)
    old_conn.commit()
    old_conn.close()

    # Import into existing old-schema bank — should warn, not error
    rep = import_bank(export_path, dst_db)
    assert rep["total_inserted"] >= len(doc["data"]["atoms"]), \
        "should import all atoms despite unknown column"
    assert rep["total_warnings"] > 0, "should warn about unknown columns"

    # Verify atoms are intact
    dst_store = CardStore(dst_db)
    for a in dst_store.atoms_in_scope("test"):
        assert a.content.startswith("test content"), f"corrupted content: {a.content}"


def test_unknown_column_hard_error_on_fresh_bank(tmp_path):
    """When importing into a FRESHLY CREATED bank (not pre-existing), an
    unknown source column is a HARD ERROR — the canonical init path should
    have created every column. This is the gate catch: a lossy round-trip
    trap where migration-added columns were silently dropped."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=2)
    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    # Add a fake column to simulate a structural mismatch
    with open(export_path) as f:
        doc = json.load(f)
    doc["manifest"]["tables"]["atoms"]["columns"].append("future_column")
    for row in doc["data"]["atoms"]:
        row["future_column"] = "should_cause_error"
    with open(export_path, "w") as f:
        json.dump(doc, f)

    dst_db = str(tmp_path / "dst.db")
    with pytest.raises(ValueError, match="fresh bank missing columns"):
        import_bank(export_path, dst_db)


def test_import_creates_idempotent_structures(tmp_path):
    """Importing into a fresh bank creates all needed structures; re-import
    into the same bank doesn't break things."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=2)
    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    # Import twice — second should be safe
    import_bank(export_path, dst_db)
    count1 = CardStore(dst_db).count_atoms_in_scope("test")

    import_bank(export_path, dst_db)
    count2 = CardStore(dst_db).count_atoms_in_scope("test")

    assert count1 == count2, f"re-import changed count: {count1} -> {count2}"


def test_import_with_earned_atoms(tmp_path):
    """Atoms with custom score_history should survive round-trip with scores intact."""
    src_db = str(tmp_path / "src.db")
    store = CardStore(src_db)
    aid = store.add_atom("earned:one", "earned content", scope="test")
    # Give it some earned weight
    store._mutate_score("atoms", aid, op="earn", delta=30.0)
    store._mutate_score("atoms", aid, op="earn", delta=15.0)
    store.conn.commit()

    # Record the score and score_history
    a = store.get_atom(aid)
    src_score = a.score
    src_history = a.score_history
    store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    dst_store = CardStore(dst_db)
    a2 = dst_store.get_atom(aid)
    assert a2 is not None
    assert a2.score == src_score, f"earned score lost: {src_score} -> {a2.score}"
    assert a2.score_history == src_history, f"score history changed"
    dst_store.conn.close()


def test_export_bank_not_found(tmp_path):
    """Export fails cleanly when bank doesn't exist."""
    with pytest.raises(FileNotFoundError):
        export_bank(str(tmp_path / "nonexistent.db"), str(tmp_path / "out.json"))


def test_import_bank_not_found(tmp_path):
    """Import fails cleanly when export file doesn't exist."""
    with pytest.raises(FileNotFoundError):
        import_bank(str(tmp_path / "nonexistent.json"), str(tmp_path / "target.db"))


def test_import_invalid_format(tmp_path):
    """Import rejects non-export files."""
    bad_path = str(tmp_path / "bad.json")
    with open(bad_path, "w") as f:
        json.dump({"not": "a bank export"}, f)
    with pytest.raises(ValueError, match="not an echelon bank export"):
        import_bank(bad_path, str(tmp_path / "target.db"))


# ── regression: migration-column round-trip (gate catch 2026-07-31) ───────────

def test_importer_creates_full_schema_with_migrations(tmp_path):
    """Regression (a): When import_bank creates a fresh destination, it must
    include ALL migration-added columns and their indexes — cards.kind,
    cards.prev, atoms.scope/kind/valence/arousal, atom_spine.scope/witness,
    atom_earned.scope, and all associated indexes."""
    src_db = str(tmp_path / "src.db")
    _seed_bank(src_db, n=3)
    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    import sqlite3
    conn = sqlite3.connect(dst_db)
    conn.row_factory = sqlite3.Row

    # cards: kind + prev migration columns
    cards_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)").fetchall()}
    assert "kind" in cards_cols, "cards.kind missing — migration not run by canonical init"
    assert "prev" in cards_cols, "cards.prev missing — migration not run by canonical init"

    # atoms: rich-field migration columns
    atoms_cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
    for col in ("scope", "kind", "valence", "arousal"):
        assert col in atoms_cols, \
            f"atoms.{col} missing — migration not run by canonical init"

    # atom_spine: scope + witness + claim_confidence migration columns
    spine_cols = {r["name"] for r in conn.execute("PRAGMA table_info(atom_spine)").fetchall()}
    for col in ("scope", "witness", "claim_confidence"):
        assert col in spine_cols, \
            f"atom_spine.{col} missing — migration not run by canonical init"

    # atom_earned: scope migration column
    earned_cols = {r["name"] for r in conn.execute("PRAGMA table_info(atom_earned)").fetchall()}
    assert "scope" in earned_cols, \
        "atom_earned.scope missing — migration not run by canonical init"

    # Indexes that are created post-migration
    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    for idx in ("idx_cards_kind", "idx_cards_prev", "idx_atoms_scope",
                "idx_spine_scope", "idx_earned_scope_score",
                "idx_atoms_scope_score", "idx_atoms_born_from"):
        assert idx in indexes, f"{idx} missing — post-migration index not created"

    conn.close()


def test_round_trip_preserves_cards_kind(tmp_path):
    """Regression (b): cards.kind values must survive round-trip exactly.
    This was the concrete lossy column — 4,729 values silently dropped on a
    live-copy import before the fix."""
    src_db = str(tmp_path / "src.db")
    store = CardStore(src_db)
    # Add atoms first
    for i in range(3):
        store.add_atom(f"tooling:test:{i}", f"content {i}", scope="test")
    # Add cards with known kind values via born_from classification
    # (add_card auto-classifies kind from born_from via registry.classify)
    store.add_card("wrap card", [], born_from="wrap-session")     # kind=100
    store.add_card("cartridge card", [], born_from="cartridge:x") # kind=300
    store.add_card("unknown card", [], born_from="no-marker")     # kind=0
    store.conn.commit()
    store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    # Verify cards.kind values match exactly
    import sqlite3
    src_conn = sqlite3.connect(src_db)
    src_conn.row_factory = sqlite3.Row
    dst_conn = sqlite3.connect(dst_db)
    dst_conn.row_factory = sqlite3.Row
    src_cards = {
        r[0]: r["kind"]
        for r in src_conn.execute("SELECT id, kind FROM cards ORDER BY id").fetchall()
    }
    dst_cards = {
        r[0]: r["kind"]
        for r in dst_conn.execute("SELECT id, kind FROM cards ORDER BY id").fetchall()
    }
    assert src_cards == dst_cards, \
        f"cards.kind mismatch:\n  src={src_cards}\n  dst={dst_cards}"
    src_conn.close()
    dst_conn.close()


def test_round_trip_full_schema_comparison(tmp_path):
    """Regression (c): After round-trip, compare PRAGMA table_info on BOTH
    sides for EVERY exported table — full column sets AND per-column values
    row-for-row. Any future migration-added column can never silently vanish
    again because this test introspects the FULL schema, not just the fields
    that the existing equality test checked."""
    src_db = str(tmp_path / "src.db")
    store = CardStore(src_db)
    for i in range(5):
        store.add_atom(
            f"tooling:test:{i}", f"content {i}",
            scope="test", kind="lesson" if i % 2 == 0 else "note",
            valence=float(i) * 0.2, arousal=float(i % 3) * 0.1,
        )
    store.conn.commit()
    store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    import sqlite3
    src_conn = sqlite3.connect(src_db)
    src_conn.row_factory = sqlite3.Row
    dst_conn = sqlite3.connect(dst_db)
    dst_conn.row_factory = sqlite3.Row

    # All user tables (exclude sqlite_ internal and FTS virtual tables)
    tables = [r[0] for r in src_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
        "ORDER BY name").fetchall()]

    for tbl in tables:
        # 1. PRAGMA table_info: identical column sets
        src_info = {r["name"]: dict(r) for r in
                    src_conn.execute(f"PRAGMA table_info(\"{tbl}\")").fetchall()}
        dst_info = {r["name"]: dict(r) for r in
                    dst_conn.execute(f"PRAGMA table_info(\"{tbl}\")").fetchall()}
        assert set(src_info.keys()) == set(dst_info.keys()), \
            f"{tbl}: column set mismatch\n  src={sorted(src_info.keys())}\n  dst={sorted(dst_info.keys())}"

        # 2. Row-for-row comparison (order-agnostic by primary key)
        src_rows = src_conn.execute(f"SELECT * FROM \"{tbl}\"").fetchall()
        dst_rows = dst_conn.execute(f"SELECT * FROM \"{tbl}\"").fetchall()
        assert len(src_rows) == len(dst_rows), \
            f"{tbl}: row count mismatch — src={len(src_rows)} dst={len(dst_rows)}"

        # Get PK columns from PRAGMA
        pk_cols = [r["name"] for r in
                   src_conn.execute(f"PRAGMA table_info(\"{tbl}\")").fetchall()
                   if r["pk"] > 0]

        if pk_cols:
            # Index by PK tuple for order-agnostic comparison
            src_by_pk = {}
            for r in src_rows:
                pk_vals = tuple(r[c] for c in pk_cols)
                src_by_pk[pk_vals] = dict(r)
            dst_by_pk = {}
            for r in dst_rows:
                pk_vals = tuple(r[c] for c in pk_cols)
                dst_by_pk[pk_vals] = dict(r)

            # Both sides should have the same PKs
            src_pks = set(src_by_pk.keys())
            dst_pks = set(dst_by_pk.keys())
            assert src_pks == dst_pks, \
                f"{tbl}: PK set mismatch — only in src={src_pks - dst_pks}, only in dst={dst_pks - src_pks}"

            # Every column value must match
            for pk in src_pks:
                for col in src_info:
                    sv = src_by_pk[pk][col]
                    dv = dst_by_pk[pk][col]
                    assert sv == dv, \
                        f"{tbl}[{pk}].{col}: {sv!r} != {dv!r}"
        else:
            # No PK — compare as ordered lists (both sides should have
            # the same rows in the same order, since import preserves order)
            for col in src_info:
                for i, (sr, dr) in enumerate(zip(src_rows, dst_rows)):
                    assert sr[col] == dr[col], \
                        f"{tbl}[{i}].{col}: {sr[col]!r} != {dr[col]!r}"

    src_conn.close()
    dst_conn.close()


# ── gate-catch-2: verbatim atom_links restore (2026-07-31) ───────────────────

def test_round_trip_atom_links_exact(tmp_path):
    """Round-trip equality on atom_links must be EXACT — row count AND full
    multiset of (from_id, to_id, relation, ts, superseded_on). The previous
    cycle-detection Phase 2 in import_bank DELETED tombstoned supersession
    legs that happened to form mutual pairs, losing 18 rows on a live copy."""
    src_db = str(tmp_path / "src.db")
    store = CardStore(src_db)
    atom_ids = []
    for i in range(6):
        aid = store.add_atom(f"tooling:test:{i}", f"content {i}", scope="test")
        atom_ids.append(aid)

    # Create a variety of edges: refs, supersedes, supersedes tombstoned
    store.link(atom_ids[0], atom_ids[1], "refs")
    store.link(atom_ids[1], atom_ids[2], "supersedes")
    store.link(atom_ids[2], atom_ids[3], "supersedes")
    store.link(atom_ids[4], atom_ids[5], "supersedes")
    # Tombstone a supersedes edge (simulates merge receipt)
    store.unlink(atom_ids[1], atom_ids[2], "supersedes")
    store.conn.commit()
    store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    import_bank(export_path, dst_db)

    import sqlite3
    src_conn = sqlite3.connect(src_db)
    src_conn.row_factory = sqlite3.Row
    dst_conn = sqlite3.connect(dst_db)
    dst_conn.row_factory = sqlite3.Row

    # Row counts must match EXACTLY
    src_count = src_conn.execute("SELECT COUNT(*) FROM atom_links").fetchone()[0]
    dst_count = dst_conn.execute("SELECT COUNT(*) FROM atom_links").fetchone()[0]
    assert src_count == dst_count, \
        f"atom_links row count mismatch: src={src_count} dst={dst_count}"

    # Full multiset comparison: (from_id, to_id, relation, ts, superseded_on)
    src_rows = set()
    for r in src_conn.execute(
        "SELECT from_id, to_id, relation, ts, superseded_on FROM atom_links"
    ).fetchall():
        src_rows.add((r["from_id"], r["to_id"], r["relation"],
                      r["ts"], r["superseded_on"]))
    dst_rows = set()
    for r in dst_conn.execute(
        "SELECT from_id, to_id, relation, ts, superseded_on FROM atom_links"
    ).fetchall():
        dst_rows.add((r["from_id"], r["to_id"], r["relation"],
                      r["ts"], r["superseded_on"]))

    only_src = src_rows - dst_rows
    only_dst = dst_rows - src_rows
    assert not only_src, f"Rows missing in destination: {only_src}"
    assert not only_dst, f"Rows only in destination: {only_dst}"
    assert src_rows == dst_rows, \
        f"atom_links multiset mismatch: src={len(src_rows)} dst={len(dst_rows)}"

    src_conn.close()
    dst_conn.close()


def test_mutual_supersedes_cycle_round_trip(tmp_path):
    """A source bank containing a mutual supersedes cycle (A->B tombstoned,
    B->A live) round-trips with BOTH legs and their superseded_on values
    intact, and a warning is emitted. The tombstoned leg is a merge receipt
    that must never be dropped during restore."""
    src_db = str(tmp_path / "src.db")
    store = CardStore(src_db)
    aid_a = store.add_atom("tooling:test:a", "atom A", scope="test")
    aid_b = store.add_atom("tooling:test:b", "atom B", scope="test")

    # Create mutual supersedes pair: A->B (live), B->A (live)
    store.link(aid_a, aid_b, "supersedes")
    store.link(aid_b, aid_a, "supersedes")

    # Tombstone one leg: A->B becomes a merge receipt
    store.unlink(aid_a, aid_b, "supersedes")
    store.conn.commit()

    # Record source state
    import sqlite3
    src_conn = sqlite3.connect(src_db)
    src_conn.row_factory = sqlite3.Row
    src_edges = {}
    for r in src_conn.execute(
        "SELECT from_id, to_id, superseded_on FROM atom_links WHERE relation='supersedes'"
    ).fetchall():
        key = (r["from_id"], r["to_id"])
        src_edges[key] = r["superseded_on"]
    src_conn.close()
    store.conn.close()

    export_path = str(tmp_path / "export.json")
    export_bank(src_db, export_path)

    dst_db = str(tmp_path / "dst.db")
    rep = import_bank(export_path, dst_db)

    # A cycle warning must have been emitted (observability)
    assert rep["cycles_detected"] > 0, \
        "should warn about observed supersedes cycle"
    assert any("verbatim" in w for w in rep["cycles_skipped"]), \
        "warning should indicate edges imported verbatim"

    # Both legs must be present with identical superseded_on values
    dst_conn = sqlite3.connect(dst_db)
    dst_conn.row_factory = sqlite3.Row
    dst_edges = {}
    for r in dst_conn.execute(
        "SELECT from_id, to_id, superseded_on FROM atom_links WHERE relation='supersedes'"
    ).fetchall():
        key = (r["from_id"], r["to_id"])
        dst_edges[key] = r["superseded_on"]
    dst_conn.close()

    assert set(src_edges.keys()) == set(dst_edges.keys()), \
        f"Edge key mismatch: src={set(src_edges.keys())} dst={set(dst_edges.keys())}"
    for key, src_so in src_edges.items():
        dst_so = dst_edges[key]
        assert src_so == dst_so, \
            f"superseded_on mismatch for {key}: src={src_so} dst={dst_so}"


def test_cycle_dfs_still_works_for_live_write_path():
    """The _would_create_cycle DFS remains available for the live write path
    (creating a NEW link at runtime). It correctly detects cycles in live
    supersedes chains. Import no longer applies it to document restore, but
    the capability itself must not be broken."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE atom_links (from_id TEXT, to_id TEXT, relation TEXT, superseded_on INTEGER DEFAULT 0)")
    # Linear chain: a -> b -> c (all live)
    conn.execute("INSERT INTO atom_links VALUES ('a','b','supersedes',0)")
    conn.execute("INSERT INTO atom_links VALUES ('b','c','supersedes',0)")

    # c -> a would close the cycle
    assert _would_create_cycle(conn, "c", "a", "supersedes"), \
        "DFS should detect the cycle on live edges"

    # a -> c is fine (no cycle)
    assert not _would_create_cycle(conn, "a", "c", "supersedes"), \
        "DFS should not flag a valid extension"

    # Tombstoned edges are not followed
    conn.execute("INSERT INTO atom_links VALUES ('c','d','supersedes',99999)")
    assert not _would_create_cycle(conn, "d", "a", "supersedes"), \
        "DFS should not follow tombstoned c->d edge"

    conn.close()
