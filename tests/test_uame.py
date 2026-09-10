"""Focused unit tests for echelon_engine.atoms.uame — the leaf-most engine atom.

uame is the two-prefix × COS-domain content-addressed store (the schema spine the
whole memory core sits on). It depends on stdlib only, so it migrates first. These
are the FOCUSED-domain tests (the old cross-cutting test_substrate.py was discarded);
functional/integration tests come later once store+cards+bank exist.

Each test runs on a fresh tmp db — no shared state, no ~/.echelon touched.
"""
import math
import time

import pytest

from echelon_engine.atoms.uame import (
    UAME, Entry, content_id, domain_of, compute_score,
    SCORE_BENCHMARK,
)


@pytest.fixture
def db(tmp_path):
    return UAME(tmp_path / "core.db")


# ── pure helpers ──────────────────────────────────────────────────────────
def test_content_id_is_deterministic_and_16_hex():
    a = content_id("hello", "tooling", "atom")
    b = content_id("hello", "tooling", "atom")
    assert a == b
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)


def test_content_id_keys_on_content_domain_kind():
    base = content_id("x", "d", "atom")
    assert content_id("x", "d", "atom") == base
    assert content_id("y", "d", "atom") != base          # content
    assert content_id("x", "e", "atom") != base          # domain
    assert content_id("x", "d", "card") != base          # kind


def test_domain_of_takes_first_segment_sanitized():
    assert domain_of("tooling:cli:flag") == "tooling"
    assert domain_of("Work Flow:x") == "work_flow"        # lowered + space->_
    assert domain_of("") == "general"                     # fallback
    assert domain_of("", fallback="raw") == "raw"


def test_compute_score_empty_history_is_benchmark():
    assert compute_score([]) == SCORE_BENCHMARK


def test_compute_score_recent_delta_weighted_near_full():
    now = int(time.time())
    # a +10 delta right now -> score ~ benchmark + 10 (weight ~1.0)
    s = compute_score([[now, 10.0]], now=now)
    assert abs(s - (SCORE_BENCHMARK + 10.0)) < 1e-6


def test_compute_score_single_entry_does_not_decay():
    # a weighted AVERAGE of one element is that element: w cancels (δ·w / w = δ),
    # so a lone delta returns b+δ regardless of age. Decay only differentiates
    # entries of DIFFERENT ages against each other (next test).
    now = int(time.time())
    recent = compute_score([[now, 10.0]], now=now)
    old = compute_score([[now - 400 * 86400, 10.0]], now=now)
    assert recent == old == SCORE_BENCHMARK + 10.0


def test_compute_score_recent_outweighs_old_in_mixed_history():
    # a fresh +10 and an ancient -10: the recent one dominates the weighted avg,
    # so the score pulls UP (toward the recent delta), not to zero.
    now = int(time.time())
    s = compute_score([[now, 10.0], [now - 400 * 86400, -10.0]], now=now)
    assert s > SCORE_BENCHMARK                            # recent positive wins
    assert s < SCORE_BENCHMARK + 10.0                     # but old one still drags a little


def test_compute_score_is_source_blind():
    # [ts, delta] and [ts, delta, src] must weight identically (the *_ splat)
    now = int(time.time())
    two = compute_score([[now, 5.0]], now=now)
    three = compute_score([[now, 5.0, "judge"]], now=now)
    assert two == three


# ── Entry dataclass ───────────────────────────────────────────────────────
def test_entry_autofills_id_and_domain_from_coordinate():
    e = Entry(content="c", coordinate="tooling:cli")
    assert e.domain == "tooling"                          # derived from coordinate
    assert e.id == content_id("c", "tooling", "atom")     # auto content-id


def test_entry_explicit_domain_kept():
    e = Entry(content="c", domain="identity", coordinate="x:y")
    assert e.domain == "identity"


# ── the two-table wall (the load-bearing safety property) ─────────────────
def test_append_routes_core_vs_working_by_permanence(db):
    db.append(Entry(content="soul", coordinate="identity:self", permanent=True))
    db.append(Entry(content="cand", coordinate="identity:self", permanent=False))
    assert "core_identity" in db.tables()
    assert "working_identity" in db.tables()
    assert db.count(domain="identity", permanent=True) == 1
    assert db.count(domain="identity", permanent=False) == 1


def test_append_is_content_addressed_dedup(db):
    e = Entry(content="same", coordinate="tooling:x", permanent=True)
    db.append(e)
    db.append(Entry(content="same", coordinate="tooling:x", permanent=True))
    assert db.count(domain="tooling", permanent=True) == 1   # second write is a no-op


def test_append_many_matches_individual_appends(tmp_path):
    entries = [Entry(content=f"c{i}", coordinate="tooling:x", permanent=True) for i in range(5)]
    a = UAME(tmp_path / "a.db")
    b = UAME(tmp_path / "b.db")
    for e in entries:
        a.append(e)
    b.append_many([Entry(content=f"c{i}", coordinate="tooling:x", permanent=True) for i in range(5)])
    assert a.stats()["soul_total"] == b.stats()["soul_total"] == 5


def test_append_many_dedups_same_content(db):
    db.append_many([
        Entry(content="dup", coordinate="tooling:x", permanent=True),
        Entry(content="dup", coordinate="tooling:x", permanent=True),
    ])
    assert db.count(domain="tooling", permanent=True) == 1


# ── stats / census ────────────────────────────────────────────────────────
def test_stats_reports_soul_total_and_domains(db):
    db.append(Entry(content="a", coordinate="tooling:x", permanent=True))
    db.append(Entry(content="b", coordinate="workflow:y", permanent=False))
    st = db.stats()
    assert st["soul_total"] == 2
    assert set(st["domains"]) == {"tooling", "workflow"}


# ── the SOUL GUARD: scaffolding never enters the soul ─────────────────────
def test_soul_guard_refuses_underscore_template(db):
    with pytest.raises(ValueError, match="SOUL GUARD"):
        db.append(Entry(content="tpl", coordinate="tooling:_template", permanent=True))


def test_soul_guard_refuses_memory_index(db):
    with pytest.raises(ValueError, match="SOUL GUARD"):
        db.append(Entry(content="idx", coordinate="echelon:MEMORY", permanent=True))


def test_soul_guard_exempts_bank_family(db):
    # a bank content entry may carry a scaffolding-shaped coordinate (regenerable index)
    db.append(Entry(content="idx", coordinate="echelon:MEMORY", family="bank"))
    assert db.count(family="bank") == 1
