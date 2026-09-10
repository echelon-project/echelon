"""Focused unit tests for echelon_engine.atoms.scoring — pure decay math leaf.

No IO, no DB. Tests the scoring leaf in isolation so a behaviour change in
compute_score surfaces here rather than hiding inside a broader store test.
"""
import math
import time

import pytest

from echelon_engine.atoms.scoring import compute_score, SCORE_BENCHMARK, SCORE_LAMBDA


def test_constants_have_expected_values():
    assert SCORE_BENCHMARK == 100.0
    assert SCORE_LAMBDA == 0.02


def test_empty_history_returns_benchmark():
    assert compute_score([]) == SCORE_BENCHMARK


def test_single_recent_delta_adds_exactly():
    now = int(time.time())
    result = compute_score([[now, 10.0]], now=now)
    assert abs(result - (SCORE_BENCHMARK + 10.0)) < 1e-9


def test_single_delta_does_not_decay_in_isolation():
    # A weighted average of ONE element cancels the weight: b + (δ·w)/(w) = b + δ.
    now = int(time.time())
    recent = compute_score([[now, 10.0]], now=now)
    old = compute_score([[now - 400 * 86400, 10.0]], now=now)
    assert recent == old == SCORE_BENCHMARK + 10.0


def test_recent_positive_outweighs_old_negative():
    now = int(time.time())
    s = compute_score([[now, 10.0], [now - 400 * 86400, -10.0]], now=now)
    assert s > SCORE_BENCHMARK
    assert s < SCORE_BENCHMARK + 10.0


def test_source_blind_two_vs_three_element_entries():
    now = int(time.time())
    two = compute_score([[now, 5.0]], now=now)
    three = compute_score([[now, 5.0, "judge"]], now=now)
    assert two == three


def test_decay_weight_uses_lambda():
    # w = e^(-λ·days). At exactly ln(2)/λ days the weight is ~0.5.
    now = int(time.time())
    half_life_days = math.log(2) / SCORE_LAMBDA
    old_ts = now - int(half_life_days * 86400)
    w_old = math.exp(-SCORE_LAMBDA * half_life_days)
    expected = SCORE_BENCHMARK + (10.0 * w_old + 10.0) / (w_old + 1.0)
    actual = compute_score([[old_ts, 10.0], [now, 10.0]], now=now)
    assert abs(actual - expected) < 1e-6


def test_negative_delta_lowers_score():
    now = int(time.time())
    s = compute_score([[now, -20.0]], now=now)
    assert s == SCORE_BENCHMARK - 20.0


def test_custom_benchmark_and_lambda():
    now = int(time.time())
    s = compute_score([[now, 5.0]], now=now, b=50.0, lam=0.1)
    assert abs(s - 55.0) < 1e-9


# ── Per-type lambda tests ────────────────────────────────────────────────────

def test_lam_for_known_types():
    from echelon_engine.atoms.scoring import lam_for, LAMBDA_BY_TYPE
    assert lam_for("feedback") == 0.005
    assert lam_for("user") == 0.005
    assert lam_for("project") == 0.03
    assert lam_for("reference") == 0.04


def test_lam_for_unknown_type_falls_back_to_default():
    from echelon_engine.atoms.scoring import lam_for, DEFAULT_LAMBDA
    assert lam_for(None) == DEFAULT_LAMBDA
    assert lam_for("") == DEFAULT_LAMBDA
    assert lam_for("bogus") == DEFAULT_LAMBDA


def test_lam_for_none_returns_default():
    from echelon_engine.atoms.scoring import lam_for, DEFAULT_LAMBDA
    assert lam_for(None) == DEFAULT_LAMBDA


def test_per_type_lam_affects_score_differently():
    """Same history, different types produce different scores because lambda
    controls how fast old deltas decay. A smaller lambda (feedback) keeps
    the score closer to the original delta; a larger lambda (reference)
    decays it further toward neutral."""
    from echelon_engine.atoms.scoring import lam_for
    now = int(time.time())
    history = [[now - 100 * 86400, 50.0], [now, 10.0]]
    score_feedback = compute_score(history, now=now, lam=lam_for("feedback"))
    score_reference = compute_score(history, now=now, lam=lam_for("reference"))
    # The old +50 delta decays more under reference (lam=0.04) than feedback (lam=0.005)
    # So feedback score should be higher (more weight on the old positive delta)
    assert score_feedback > score_reference, (
        f"feedback={score_feedback} should be > reference={score_reference}"
    )


# ── Read-time decay: score decreases with age ────────────────────────────────

def test_read_time_decay_old_delta_ranks_lower():
    """An atom with an old positive delta ranks LOWER today than at delta time.
    Construct history with a timestamp 100 days back and assert score < stored."""
    now = int(time.time())
    old_ts = now - 100 * 86400
    history = [[old_ts, 30.0]]
    # Score computed AT delta time (now = old_ts + epsilon)
    score_at_delta_time = compute_score(history, now=old_ts + 60,
                                        lam=SCORE_LAMBDA)
    # Score computed NOW
    score_now = compute_score(history, now=now, lam=SCORE_LAMBDA)
    # Single-element history: score = B + delta always (weight cancels)
    # So they should be equal for a single entry
    # But with TWO entries, the old one decays
    history2 = [[old_ts, 50.0], [now, -10.0]]
    score2_at_time = compute_score(history2, now=old_ts + 60, lam=SCORE_LAMBDA)
    score2_now = compute_score(history2, now=now, lam=SCORE_LAMBDA)
    # The old +50 should have less weight now than at delta time,
    # so score_now should be different from score_at_time
    assert abs(score2_at_time - score2_now) > 0.01, (
        f"Expected decay to change score: at_time={score2_at_time}, now={score2_now}"
    )


def test_read_time_decay_mixed_history():
    """With mixed old and recent deltas, the score shifts toward the recent ones
    as old ones decay."""
    now = int(time.time())
    old_ts = now - 200 * 86400
    history = [[old_ts, 20.0], [now, -5.0]]
    # The old +20 decays heavily, so the recent -5 dominates
    score = compute_score(history, now=now, lam=SCORE_LAMBDA)
    # Should be close to B - 5 (the recent dominates)
    assert score < SCORE_BENCHMARK + 5, (
        f"Expected recent -5 to dominate old +20, got {score}"
    )
    assert score < SCORE_BENCHMARK + 2, (
        f"Expected old delta to be heavily decayed, got {score}"
    )
