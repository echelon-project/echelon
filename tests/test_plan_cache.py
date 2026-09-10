"""Tests for echelon_engine.atoms.plan_cache — pure seam: put/get/reinforce logic with an
in-memory CardStore (no network, no model calls, $0). The injectable `judge` param lets us
test the review gate without a live provider."""
from __future__ import annotations
import pytest

from echelon_engine.atoms.plan_cache import PlanCache, PlanVerdict


# ── empty cache always misses ────────────────────────────────────────────────

def test_empty_cache_is_miss():
    cache = PlanCache(scope="test", db_path=":memory:")
    verdict = cache.get("build a search feature")
    assert verdict.route == "miss"
    assert verdict.leaves == []


# ── put() then get() — should hit on identical goal ──────────────────────────

def test_put_then_get_identical_goal():
    cache = PlanCache(scope="test", db_path=":memory:", floor=0.5)
    leaves = [{"task": "step 1"}, {"task": "step 2"}]
    card_id = cache.put("build a search feature", leaves)
    assert card_id  # got an id

    verdict = cache.get("build a search feature")
    # identical goal should match at similarity ~1.0 — route='review' or 'reflex'
    assert verdict.route in ("reflex", "review")
    assert verdict.leaves == leaves
    assert verdict.match > 0.8


# ── put() with empty leaves does not cache ──────────────────────────────────

def test_put_empty_leaves_returns_empty():
    cache = PlanCache(scope="test", db_path=":memory:")
    result = cache.put("some goal", [])
    assert result == ""


# ── reinforce() warms a card (smoke — doesn't require live DB warmth) ────────

def test_reinforce_green_does_not_crash():
    cache = PlanCache(scope="test", db_path=":memory:")
    card_id = cache.put("some goal", [{"task": "do x"}])
    cache.reinforce(card_id, ok=True)   # must not raise

def test_reinforce_red_does_not_crash():
    cache = PlanCache(scope="test", db_path=":memory:")
    card_id = cache.put("some goal", [{"task": "do x"}])
    cache.reinforce(card_id, ok=False)   # must not raise


# ── review_plan() with injected judge ─────────────────────────────────────────

def test_review_plan_injected_sound_judge():
    cache = PlanCache(scope="test", db_path=":memory:")
    result = cache.review_plan("build X", [{"task": "do A"}], judge=lambda g, l: True)
    assert result["sound"] is True
    assert result["via"] == "judge"

def test_review_plan_injected_unsound_judge():
    cache = PlanCache(scope="test", db_path=":memory:")
    result = cache.review_plan("build X", [{"task": "do A"}], judge=lambda g, l: False)
    assert result["sound"] is False

def test_review_plan_empty_leaves_guard():
    cache = PlanCache(scope="test", db_path=":memory:")
    result = cache.review_plan("goal", [])
    assert result["sound"] is False
    assert result["via"] == "guard"

def test_review_plan_judge_error_fails_closed():
    cache = PlanCache(scope="test", db_path=":memory:")
    def broken_judge(g, l): raise RuntimeError("explode")
    result = cache.review_plan("goal", [{"task": "x"}], judge=broken_judge)
    assert result["sound"] is False
    assert result.get("fail_closed") is True


# ── content-addressed ids prevent collision ───────────────────────────────────

def test_put_distinct_goals_get_distinct_ids():
    cache = PlanCache(scope="test", db_path=":memory:")
    id1 = cache.put("goal A", [{"task": "x"}])
    id2 = cache.put("goal B", [{"task": "y"}])
    assert id1 != id2

def test_put_same_goal_same_leaves_idempotent():
    cache = PlanCache(scope="test", db_path=":memory:")
    id1 = cache.put("same goal", [{"task": "x"}])
    id2 = cache.put("same goal", [{"task": "x"}])
    assert id1 == id2   # content-addressed = same hash = same id
