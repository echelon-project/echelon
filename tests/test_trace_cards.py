"""Focused unit tests for echelon_engine.atoms.trace_cards — the trace→card bridge.

This turns a finished run's per-step records into action-cards and rates them by the
TRACE (never a model self-rating): compute_q is pure/deterministic, record_run creates
the step-card chain and backprops credit to the TIP (the chain rule does the rest).
Depends only on cards (migrated sibling).

Isolated: CardStore on a tmp db; BASELINE_FILE monkeypatched to tmp so the rolling
cost-baseline sidecar never touches the real ~/.echelon dir.
"""
from dataclasses import dataclass, field

import pytest

from echelon_engine.atoms import trace_cards as tc
from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.uame import SCORE_BENCHMARK


@dataclass
class FakeResult:
    """Mimics the loop.RunResult shape compute_q/record_run read off."""
    status: str = "completed"
    steps: int = 1
    tokens_in: int = 0
    tokens_out: int = 0
    transcript: list = field(default_factory=list)
    # verify_outcome is intentionally ABSENT by default (a normal agent run); set it to
    # engage the verify gate.


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


@pytest.fixture(autouse=True)
def isolate_baselines(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "BASELINE_FILE", tmp_path / "baselines.json")


# ── compute_q: pure, deterministic, centred at 50 ─────────────────────────
def test_q_is_50_for_non_completed_run():
    assert tc.compute_q(FakeResult(status="failed"), bad_steps=0, baseline_cost=None) == 50.0


def test_q_clean_run_above_neutral():
    # a completed run with no bad steps earns the clean-run bonus (> 50)
    q = tc.compute_q(FakeResult(status="completed", steps=4), bad_steps=0, baseline_cost=None)
    assert q > 50.0


def test_q_bad_steps_erode_the_bonus():
    clean = tc.compute_q(FakeResult(steps=4), bad_steps=0, baseline_cost=None)
    messy = tc.compute_q(FakeResult(steps=4), bad_steps=4, baseline_cost=None)
    assert messy < clean
    assert messy == pytest.approx(50.0)        # all-bad => no good bonus => neutral


def test_q_cheaper_than_baseline_earns_cost_bonus():
    cheap = tc.compute_q(FakeResult(steps=1, tokens_in=100, tokens_out=0),
                         bad_steps=0, baseline_cost=1000.0)
    dear = tc.compute_q(FakeResult(steps=1, tokens_in=900, tokens_out=0),
                        bad_steps=0, baseline_cost=1000.0)
    assert cheap > dear                        # getting cheaper than baseline pays


# ── the VERIFY GATE: a mechanical outcome, not transport success ──────────
def test_verify_false_is_verified_failure():
    r = FakeResult(status="completed")
    r.verify_outcome = False
    assert tc.compute_q(r, bad_steps=0, baseline_cost=None) == tc.VERIFIED_FAILURE_Q


def test_verify_none_caps_at_neutral():
    r = FakeResult(status="completed", steps=1, tokens_in=10, tokens_out=10)
    r.verify_outcome = None
    # unverifiable: transport success earns NEUTRAL, never the old 'it returned text' bonus
    assert tc.compute_q(r, bad_steps=0, baseline_cost=None) == 50.0


def test_verify_true_allows_the_bonus():
    r = FakeResult(status="completed", steps=2)
    r.verify_outcome = True
    assert tc.compute_q(r, bad_steps=0, baseline_cost=None) > 50.0


# ── count_bad_steps reads the real transcript shape ───────────────────────
def test_count_bad_steps_flags_tool_errors_and_gate_blocks():
    transcript = [
        {"role": "tool", "result": "ok, file written"},
        {"role": "tool", "result": "Error: command failed"},
        {"role": "gate", "decision": "BLOCK: write disabled"},
        {"role": "assistant", "result": "thinking"},
    ]
    assert tc.count_bad_steps(transcript) == 2
    assert tc.count_bad_steps(None) == 0


# ── record_run: builds the step-card chain + backprops to the TIP ─────────
def test_record_run_creates_chained_cards_and_reinforces_tip(store):
    result = FakeResult(status="completed", steps=2)
    summary = tc.record_run(store, "migrate the store atom", result,
                            loaded_coords_per_step=[["tooling:a"], ["tooling:b"]])
    assert len(summary["cards"]) == 2          # one card per non-empty step
    assert summary["reinforced"] is True
    assert summary["q"] > 50.0
    # the TIP card earned (backprop enters at the output); it rose above neutral
    tip = store.card(summary["cards"][-1])
    assert tip.score > SCORE_BENCHMARK
    # and the chain is wired: the tip points back at the first card
    assert tip.prev == summary["cards"][0]


def test_record_run_neutral_records_use_without_moving_score(store):
    # a completed-but-neutral run (Q==50) must register USE, not weight
    result = FakeResult(status="completed", steps=1)
    result.verify_outcome = None               # forces Q=50 (unverifiable)
    summary = tc.record_run(store, "neutral goal", result,
                            loaded_coords_per_step=[["tooling:x"]])
    assert summary["q"] == 50.0
    assert summary["reinforced"] is False
    tip = store.card(summary["cards"][-1])
    assert tip.score == SCORE_BENCHMARK         # no weight moved
    assert tip.use_count >= 1                   # but the use was recorded


def test_record_run_non_completed_creates_cards_but_does_not_reinforce(store):
    result = FakeResult(status="failed", steps=1)
    summary = tc.record_run(store, "failed goal", result,
                            loaded_coords_per_step=[["tooling:x"]])
    assert summary["cards"]                      # the episode happened — cards exist
    assert summary["reinforced"] is False        # but a non-completed run earns nothing
