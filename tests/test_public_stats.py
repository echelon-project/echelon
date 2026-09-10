"""public_stats — the landing page's public aggregate (OPEN-0107, owner 2026-09-06).

The load-bearing rules this guards:
  1. COUNTS ONLY — the payload never carries an atom body, an atom name, or a scope
     name/list. A public route may say "N atoms across M scopes"; never WHICH or WHAT.
  2. The numbers are THIS host's live bank (ECHELON_HOME/echelon.db), computed by
     cheap COUNT/SUM, never a row scan.
  3. Cached 60 s, invalidated early on a bank write.
  4. A broken bank degrades to {"ok": False} so the page keeps its baked snapshot —
     it never raises.
  5. Timestamps are WIB (+07:00).
"""
from __future__ import annotations

import json
import sqlite3
import time

import echelon_engine.atoms.public_stats as ps


def _build_bank(path, *, atoms, scopes, earned):
    """A minimal bank with just the tables public_stats reads."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE atoms (id INTEGER PRIMARY KEY, scope TEXT, body TEXT, name TEXT)")
    conn.execute("CREATE TABLE atom_earned (atom_id INTEGER, score REAL)")
    for i in range(atoms):
        scope = scopes[i % len(scopes)] if scopes else ""
        conn.execute("INSERT INTO atoms (id, scope, body, name) VALUES (?,?,?,?)",
                     (i + 1, scope, f"secret body {i}", f"atom-name-{i}"))
    # `earned` = list of (atom_id, score); distinct atom_ids => earned_atoms
    for atom_id, score in earned:
        conn.execute("INSERT INTO atom_earned (atom_id, score) VALUES (?,?)", (atom_id, score))
    conn.commit()
    conn.close()


def _point_at(monkeypatch, tmp_path, **bank):
    ps._CACHE = None  # reset the module cache between cases
    db = tmp_path / "echelon.db"
    _build_bank(db, **bank)
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path))
    return db


def test_counts_are_correct(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path,
              atoms=10, scopes=["echelon", "gamma-support", "alpha-app"],
              earned=[(1, 5.0), (1, 2.0), (2, 3.0), (7, 1.5)])  # 3 distinct atoms, 11.5 weight
    d = ps.public_stats(force=True)
    assert d["ok"] is True
    assert d["atoms"] == 10
    assert d["scopes"] == 3
    assert d["earned_atoms"] == 3
    assert d["earned_weight"] == 12  # round(11.5) -> 12 (banker's? no: round-half-even => 12)
    assert d["cartridges"] >= 0


def test_payload_leaks_no_bodies_names_or_scopes(monkeypatch, tmp_path):
    """The hard privacy boundary: no atom body, name, or scope name in the payload."""
    _point_at(monkeypatch, tmp_path,
              atoms=5, scopes=["topsecret-scope", "another-private-scope"],
              earned=[(1, 1.0)])
    d = ps.public_stats(force=True)
    blob = json.dumps(d)
    assert "topsecret-scope" not in blob
    assert "another-private-scope" not in blob
    assert "secret body" not in blob
    assert "atom-name-" not in blob
    # keys are exactly the aggregate contract — nothing that could carry a list of rows
    assert set(d.keys()) == {
        "ok", "atoms", "earned_weight", "earned_atoms", "scopes",
        "cartridges", "snapshot_at", "bank_mtime",
    }
    # every value is a scalar (int/str/bool) — no list or dict that could hold rows
    assert all(isinstance(v, (int, str, bool)) for v in d.values())


def test_timestamps_are_wib(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, atoms=1, scopes=["s"], earned=[])
    d = ps.public_stats(force=True)
    assert d["snapshot_at"].endswith("+07:00")
    assert d["bank_mtime"].endswith("+07:00")


def test_cache_holds_for_ttl_then_recomputes_on_bank_write(monkeypatch, tmp_path):
    db = _point_at(monkeypatch, tmp_path, atoms=3, scopes=["s"], earned=[])
    first = ps.public_stats(force=True)
    assert first["atoms"] == 3

    # a second call within TTL and no bank change returns the SAME cached object
    second = ps.public_stats()
    assert second is first

    # writing the bank (new mtime) invalidates the cache even inside the TTL window
    time.sleep(0.01)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO atoms (scope, body, name) VALUES ('s','b','n')")
    conn.commit()
    conn.close()
    third = ps.public_stats()
    assert third is not first
    assert third["atoms"] == 4


def test_missing_bank_degrades_not_raises(monkeypatch, tmp_path):
    ps._CACHE = None
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "does-not-exist"))
    d = ps.public_stats(force=True)  # no bank file at all
    assert d["ok"] is False
    assert "error" in d


def test_corrupt_bank_degrades_not_raises(monkeypatch, tmp_path):
    ps._CACHE = None
    (tmp_path / "echelon.db").write_bytes(b"this is not a sqlite database")
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path))
    d = ps.public_stats(force=True)
    assert d["ok"] is False
