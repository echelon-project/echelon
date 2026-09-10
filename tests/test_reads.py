"""Focused unit tests for echelon_engine.atoms.reads — ranked / coordinate-scoped READ
helpers over a bare UAME.

reads.top() and reads.at_coordinate() are pure read endpoints: they take a UAME, walk its
soul tables (core_*/working_*), and return Entry lists ranked by (score, ts). We seed a
scratch UAME via u.append(Entry(...)) — full isolation on a tmp db — then assert the ranking
and filtering contracts. No pure helper to mock; the seam is the UAME read, so these are real
end-to-end reads on a throwaway store.
"""
import pytest

from echelon_engine.atoms.uame import UAME, Entry
from echelon_engine.atoms import reads


@pytest.fixture
def u(tmp_path):
    return UAME(tmp_path / "core.db")


def _seed(u, content, *, domain="tooling", coordinate="tooling:x", score=100.0,
          kind="atom", permanent=False):
    e = Entry(content=content, domain=domain, coordinate=coordinate, kind=kind,
              permanent=permanent, score=score)
    u.append(e)
    return e


# ── top(): highest-scoring first, soul-wall honored ───────────────────────
def test_top_orders_by_score_descending(u):
    _seed(u, "low", coordinate="tooling:a", score=10.0)
    _seed(u, "high", coordinate="tooling:b", score=90.0)
    _seed(u, "mid", coordinate="tooling:c", score=50.0)
    got = reads.top(u)
    assert [e.content for e in got] == ["high", "mid", "low"]


def test_top_min_score_filters(u):
    _seed(u, "below", coordinate="tooling:a", score=10.0)
    _seed(u, "above", coordinate="tooling:b", score=80.0)
    got = reads.top(u, min_score=50.0)
    assert [e.content for e in got] == ["above"]


def test_top_kind_filter(u):
    _seed(u, "an-atom", coordinate="tooling:a", kind="atom", score=70.0)
    _seed(u, "a-note", coordinate="tooling:b", kind="note", score=80.0)
    got = reads.top(u, kind="note")
    assert [e.content for e in got] == ["a-note"]


def test_top_domain_scopes_to_one_domain(u):
    _seed(u, "tool-hit", domain="tooling", coordinate="tooling:a", score=60.0)
    _seed(u, "memory-hit", domain="memory", coordinate="memory:a", score=90.0)
    got = reads.top(u, domain="tooling")
    assert [e.content for e in got] == ["tool-hit"]


def test_top_limit_caps_results(u):
    for i in range(5):
        _seed(u, f"s{i}", coordinate=f"tooling:{i}", score=float(i))
    got = reads.top(u, limit=2)
    assert len(got) == 2
    assert got[0].content == "s4"  # highest score first


def test_top_marks_domain_and_permanence(u):
    _seed(u, "core-one", domain="tooling", coordinate="tooling:a",
          score=50.0, permanent=True)
    got = reads.top(u, domain="tooling")
    e = next(x for x in got if x.content == "core-one")
    assert e.domain == "tooling"
    assert e.permanent is True


# ── at_coordinate(): segment-aware prefix match ───────────────────────────
def test_at_coordinate_matches_exact_and_under(u):
    _seed(u, "exact", coordinate="tooling:bash")
    _seed(u, "under", coordinate="tooling:bash:gzip")
    _seed(u, "sibling", coordinate="tooling:python")
    got = reads.at_coordinate(u, "tooling:bash")
    contents = {e.content for e in got}
    assert contents == {"exact", "under"}


def test_at_coordinate_is_segment_aware_not_substring(u):
    # "tooling:bash" must NOT match "tooling:bashful" (segment boundary, not prefix string).
    _seed(u, "real", coordinate="tooling:bash")
    _seed(u, "decoy", coordinate="tooling:bashful")
    got = reads.at_coordinate(u, "tooling:bash")
    assert [e.content for e in got] == ["real"]


def test_at_coordinate_empty_prefix_returns_all(u):
    _seed(u, "a", coordinate="tooling:a", score=10.0)
    _seed(u, "b", coordinate="memory:b", domain="memory", score=20.0)
    got = reads.at_coordinate(u, "")
    assert {e.content for e in got} == {"a", "b"}
    # still ranked score-descending
    assert got[0].content == "b"
