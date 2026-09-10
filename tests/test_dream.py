"""Focused unit test for echelon_engine.atoms.dream (the consolidation organ).

dream() is PURE: it takes a list of duck-typed Seed objects and returns Proposals,
holding no store handle. So its witness/exit logic is tested DIRECTLY with lightweight
fake seeds (no db needed) — the honest unit of this module. consolidate() is the awake
bridge that touches the store; it is driven against an ISOLATED SeedStore on tmp dbs.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from echelon_engine.atoms.dream import dream, consolidate, Proposal
from echelon_engine.atoms.store import SeedStore


@dataclass
class FakeSeed:
    """Duck-typed stand-in for a Seed — dream() only reads these attrs."""
    id: str
    content: str = "x"
    score: float = 100.0
    recall_count: int = 0
    self_seed: bool = False
    coordinate: str = ""
    kind: str = "lesson"
    valence: float = 0.0
    arousal: float = 0.0


# ---- pure dream() logic ----

def test_eligible_seed_proposed_global_when_witnessed_and_met():
    # score>=125 and recalls>=3 (meets_requirement) + not self_seed (eligible) + 2 witnesses.
    s = FakeSeed(id="a", score=130, recall_count=3)
    out = dream([s, s], min_witnesses=2)  # two in-snapshot rows => witness count 2
    assert len(out) == 1
    assert out[0].exit == "global"
    assert out[0].witnesses == 2
    assert out[0].seed is s


def test_under_witnessed_not_proposed():
    # eligible+met but only ONE instance and no wish => neither exit => no proposal.
    s = FakeSeed(id="a", score=130, recall_count=3)
    out = dream([s], min_witnesses=2)
    assert out == []


def test_requirement_unmet_not_proposed_without_wish():
    s = FakeSeed(id="a", score=100, recall_count=0)  # below bar
    out = dream([s, s], min_witnesses=2)
    assert out == []


def test_wished_unmet_becomes_self_seed():
    s = FakeSeed(id="a", score=100, recall_count=0)
    out = dream([s], wishes={"a"}, min_witnesses=2)
    assert len(out) == 1
    assert out[0].exit == "self_seed"


def test_self_seed_never_eligible_for_global():
    # Even witnessed+met, a self_seed seed is ineligible for global; here also not wished => dropped.
    s = FakeSeed(id="a", score=130, recall_count=3, self_seed=True)
    out = dream([s, s], min_witnesses=2)
    assert out == []


def test_ledger_witness_count_overrides_snapshot_rows():
    # Single in-snapshot row, but the witness LEDGER says 3 independent arrivals -> witnessed.
    s = FakeSeed(id="a", score=130, recall_count=3)
    out = dream([s], min_witnesses=2, witness_counts={"a": 3})
    assert len(out) == 1
    assert out[0].exit == "global"
    assert out[0].witnesses == 3


def test_strongest_instance_kept_as_representative():
    weak = FakeSeed(id="a", score=130, recall_count=3)
    strong = FakeSeed(id="a", score=200, recall_count=5)
    out = dream([weak, strong], min_witnesses=2)
    assert len(out) == 1
    assert out[0].seed is strong  # higher score wins as representative


def test_seed_without_id_skipped():
    class NoId:
        score = 130
        recall_count = 3
        self_seed = False
        id = None
    out = dream([NoId(), NoId()], min_witnesses=1)
    assert out == []


# ---- consolidate() against an isolated store ----

def test_consolidate_empty_scope_returns_clean_report(tmp_path):
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    report = consolidate(store, "test-scope-empty")
    assert report["scope"] == "test-scope-empty"
    assert report["proposals"] == 0
    assert report["nominated"] == []
    assert report["self_seeded"] == []
    assert report["skipped"] == []
    assert report["disclaimed"] == {"cards": 0, "atoms": 0}


def test_consolidate_no_grandvote_does_not_nominate(tmp_path):
    # A plain working seed (below the promote bar) -> not proposed, nothing nominated/self-seeded.
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    store.remember("scopeX", "a working note", kind="note")
    report = consolidate(store, "scopeX")
    assert report["nominated"] == []
    assert report["self_seeded"] == []
