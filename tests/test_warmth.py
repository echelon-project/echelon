"""Focused unit tests for echelon_engine.atoms.warmth — the foveation/recognition organ.

warmth() scores the model's CURRENT reasoning against the stored seeds in a scope and
returns a TEMPERATURE (warm/lukewarm/cold), not memory. It depends on store (seeds) and
the pure affect leaf (now in echelon_sdk). These tests exercise the LEXICAL floor only
(judge_provider=None) so nothing hits a network — the always-on free tier.

Isolated SeedStore (the store.py v2_db fix is what makes this possible): both v1+v2 dbs
on tmp paths, so seeds planted here never touch the real soul bank.
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.warmth import warmth, WARM, LUKEWARM, WarmthReading


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


def _seed(store, scope, content, coordinate):
    store.remember(scope, content, coordinate=coordinate)


# ── the lexical floor: overlap drives the temperature ─────────────────────
def test_strong_overlap_reads_warm(store):
    _seed(store, "test-w", "always pass python -X utf8 to avoid the cp1252 crash", "tooling:utf8")
    r = warmth("python -X utf8 avoids the cp1252 crash on windows", store, "test-w")
    assert isinstance(r, WarmthReading)
    assert r.score >= LUKEWARM                 # it recognized the topic
    assert r.warmest                           # surfaced the matching seed (not injected)


def test_no_overlap_reads_cold(store):
    _seed(store, "test-c", "the gzip flag controls compression level", "tooling:gzip")
    r = warmth("quarterly revenue projections for the marketing team", store, "test-c")
    assert r.score < WARM
    assert r.verdict in ("cold", "lukewarm")


def test_empty_scope_is_cold(store):
    r = warmth("anything at all", store, "empty-scope")
    assert r.warmest == []
    assert r.verdict == "cold"


def test_verdict_bands_follow_thresholds(store):
    # the verdict must be consistent with the numeric score against WARM/LUKEWARM
    _seed(store, "test-b", "content addressed append only store with wal retry", "tooling:store")
    r = warmth("content addressed append only store with wal retry", store, "test-b")
    if r.score >= WARM:
        assert r.verdict == "warm"
    elif r.score >= LUKEWARM:
        assert r.verdict == "lukewarm"
    else:
        assert r.verdict == "cold"


# ── scope isolation: a seed in scope A doesn't warm scope B ───────────────
def test_warmth_is_scoped(store):
    _seed(store, "scope-a", "the canonical chainboard composition framework", "tooling:chainboard")
    # same reasoning, a DIFFERENT scope with no such seed -> not warm
    r = warmth("the canonical chainboard composition framework", store, "scope-b")
    assert r.warmest == []                     # scope-b holds nothing
    assert r.verdict == "cold"


# ── the dispute gate: a disclaimed atom is not recalled as truth ──────────
def test_disclaimed_content_is_not_surfaced(store):
    _seed(store, "test-d", "a claim that will be disclaimed as false", "tooling:claim")
    # find the v2 atom and disclaim it (the dispute organ marks it in v2)
    atoms = store.cards.atoms_in_scope("test-d")
    assert atoms, "seed should have a v2 twin"
    store.cards.disclaim_judged("atoms", atoms[0].id, reason="proven false")
    r = warmth("a claim that will be disclaimed as false", store, "test-d")
    # the disclaimed content must NOT come back warm (read-side completion of dispute)
    assert all("disclaimed as false" not in (sw.seed.content or "") for sw in r.warmest)
