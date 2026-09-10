"""Tests for echelon_engine.atoms.card_credit — pure credit-math constants and helpers."""
import pytest

from echelon_engine.atoms.card_credit import (
    SCORE_K, SYNTH_ELIGIBLE, BEACON_THRESHOLD,
    CHAIN_DECAY, CHAIN_MAX_HOPS, CHAIN_MIN_DELTA, SHOCK_BONUS,
    _REFLEX_STOP,
    card_delta, atom_share, chain_hop_delta,
)
from echelon_engine.atoms.uame import SCORE_BENCHMARK


# ── constants ─────────────────────────────────────────────────────────────

def test_synth_eligible_above_benchmark():
    assert SYNTH_ELIGIBLE > SCORE_BENCHMARK


def test_beacon_well_above_synth():
    assert BEACON_THRESHOLD > SYNTH_ELIGIBLE


def test_chain_decay_halving():
    assert CHAIN_DECAY == 0.5


def test_shock_bonus_positive():
    assert SHOCK_BONUS > 0


def test_reflex_stop_is_frozenset():
    assert isinstance(_REFLEX_STOP, frozenset)
    assert "the" in _REFLEX_STOP


# ── card_delta ────────────────────────────────────────────────────────────

def test_card_delta_neutral_at_50():
    assert card_delta(50.0) == 0.0


def test_card_delta_positive_for_success():
    assert card_delta(100.0) > 0


def test_card_delta_negative_for_failure():
    assert card_delta(0.0) < 0


def test_card_delta_symmetry():
    """q=75 and q=25 should be symmetric around 0."""
    assert abs(card_delta(75.0) + card_delta(25.0)) < 1e-9


def test_card_delta_scaled_by_score_k():
    assert card_delta(85.0) == (85.0 - 50.0) * SCORE_K


# ── atom_share ────────────────────────────────────────────────────────────

def test_atom_share_single_atom_gets_full_delta():
    assert atom_share(10.0, 1) == 10.0


def test_atom_share_two_atoms_split_equally():
    assert atom_share(10.0, 2) == 5.0


def test_atom_share_zero_atoms_safe():
    assert atom_share(10.0, 0) == 0.0


def test_atom_share_negative_delta():
    assert atom_share(-10.0, 2) == -5.0


# ── chain_hop_delta ───────────────────────────────────────────────────────

def test_chain_hop_delta_hop0_is_full():
    assert chain_hop_delta(10.0, 0) == 10.0


def test_chain_hop_delta_hop1_halved():
    assert chain_hop_delta(10.0, 1) == pytest.approx(5.0)


def test_chain_hop_delta_terminates_below_min():
    """After enough hops the decayed delta should fall below CHAIN_MIN_DELTA → 0.0."""
    result = chain_hop_delta(1.0, 5)   # 1.0 * 0.5^5 = 0.03125 < 0.5
    assert result == 0.0


def test_chain_hop_delta_negative_delta():
    result = chain_hop_delta(-10.0, 1)
    assert result == pytest.approx(-5.0)
