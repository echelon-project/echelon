"""Focused unit tests for echelon_engine.atoms.topology — the cloud/client store-split SEAM.

Topology is a thin RESOLVER (lazy store factories + a structural-privacy assertion). It has
no pure algorithm to exercise in isolation; its contract is WHICH file each concern resolves
to and the wall between them. So these are construct + resolution tests on scratch paths:
- soul_store() builds a SeedStore on the soul db (memoized, one per process).
- local_bank() resolves to its OWN file, never the soul db (privacy is structural).
- assert_separation() encodes that wall.
persona_bank()/the real KnowledgeBank embedder are NOT exercised here (heavy I/O, out of this
slice's seam); we pin the cheap, load-bearing resolver contracts only and say so.
"""
import pytest

from echelon_engine.atoms.topology import Topology
from echelon_engine.atoms.store import SeedStore, DEFAULT_DB


def test_default_local_bank_db_is_separate_from_soul(tmp_path):
    soul = tmp_path / "core.db"
    t = Topology(soul_db=soul)
    # the local private bank defaults to a SEPARATE file alongside the soul, never the soul db.
    assert t.local_bank_db != t.soul_db
    assert t.local_bank_db == soul.parent / "local_bank.db"


def test_explicit_local_bank_db_is_honored(tmp_path):
    t = Topology(soul_db=tmp_path / "core.db",
                 local_bank_db=tmp_path / "private.db")
    assert t.local_bank_db == tmp_path / "private.db"


def test_soul_store_is_a_seedstore_on_the_soul_db(tmp_path):
    soul = tmp_path / "core.db"
    t = Topology(soul_db=soul)
    s = t.soul_store()
    assert isinstance(s, SeedStore)
    assert s.db_path == soul


def test_soul_store_is_memoized(tmp_path):
    t = Topology(soul_db=tmp_path / "core.db")
    assert t.soul_store() is t.soul_store()   # one per process


def test_assert_separation_true_when_local_differs(tmp_path):
    t = Topology(soul_db=tmp_path / "core.db",
                 local_bank_db=tmp_path / "private.db")
    assert t.assert_separation() is True


def test_assert_separation_false_when_local_equals_soul(tmp_path):
    soul = tmp_path / "core.db"
    t = Topology(soul_db=soul, local_bank_db=soul)
    # collapsing the two onto one file breaks the structural privacy guarantee.
    assert t.assert_separation() is False


def test_default_soul_db_is_global_default():
    # backward-compat: omitting soul_db points at the real global default (checked, not constructed,
    # so the unit test never opens the real soul bank).
    t = Topology.__new__(Topology)
    import inspect
    default = inspect.signature(Topology.__init__).parameters["soul_db"].default
    assert default == DEFAULT_DB


def test_close_is_safe_with_no_stores_built(tmp_path):
    # close() must tolerate never-instantiated lazy stores (all None) without raising.
    t = Topology(soul_db=tmp_path / "core.db")
    t.close()   # no stores were built; must be a no-op, not a crash
