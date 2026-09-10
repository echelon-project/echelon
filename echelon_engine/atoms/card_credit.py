"""card_credit — pure credit-math constants and delta helpers (leaf, no DB).

Extracted verbatim from cards.py.  All values are pure arithmetic over
scalars — no sqlite, no threading, no CardStore dependency.

Public surface:
    SCORE_K             — scale factor mapping (q-50) to a delta.
    SYNTH_ELIGIBLE      — score threshold for L2 synthesis-eligible tier.
    BEACON_THRESHOLD    — score threshold for L1 beacon / core candidate.
    CHAIN_DECAY         — credit-halving factor per hop along the prev-edge.
    CHAIN_MAX_HOPS      — depth cap so backprop terminates.
    CHAIN_MIN_DELTA     — negligible-delta floor; backprop stops here.
    SHOCK_BONUS         — one-time redemption bonus when a disclaimed card re-earns by trace.
    _REFLEX_STOP        — stopword set for v2_reflex_match word-overlap scoring.
    card_delta()        — compute the raw delta from q (convenience helper).
    atom_share()        — split a card delta across N atoms (§7-P2).
    chain_hop_delta()   — apply one hop's CHAIN_DECAY to a delta.
"""
from __future__ import annotations
from .uame import SCORE_BENCHMARK

SCORE_K = 1.0
SYNTH_ELIGIBLE = SCORE_BENCHMARK + 25.0   # >B+25 -> L2 synthesis-eligible (§7/§9)
BEACON_THRESHOLD = SCORE_BENCHMARK + 80.0  # pinned global beacon -> L1 candidate (§20.3, well above avg)

# Chain-credit (the card→card chain rule): an upstream card that ENABLED a success earns a DECAYED
# share of the downstream delta — less per hop, never equal/more. Honest, bounded, terminating.
CHAIN_DECAY = 0.5        # credit halves each hop back along the prev-edge
CHAIN_MAX_HOPS = 8       # depth cap — a chain never credits an unbounded ancestry
CHAIN_MIN_DELTA = 0.5    # stop once the decayed delta is negligible (long chains terminate honestly)

# SHOCK = RESET-TO-NEUTRAL + BONUS (owner, 2026-06-11): a card disclaimed as a LIE that later re-earns
# through REAL trace work doesn't inch up from the floor — its slate is WIPED (the doubt resolved, dead
# judged history discharged) and it restarts at neutral + this bonus, because a conviction that survived
# being-called-false then proved true is stronger than one never questioned. Reset (not a multiplier)
# sidesteps the dead-history denominator drag so the bonus lands clean. Applied once, on redemption, to
# the card's own positive tip outcome only (never the atom share / chain propagation).
SHOCK_BONUS = 30.0   # on top of the real earned delta — the reward for surviving the doubt

# Stopwords for v2_reflex_match's word-overlap — drop the filler so the MEANING-bearing tactic words
# (king, transfer, layer, above, row, col...) drive the match, not "the/and/is".
_REFLEX_STOP = frozenset("the and that this with for are was its has have you your will can but not "
                         "reason reflex when then move idea winning win wins because situation create "
                         "where which one two get got onto into from out off can".split())


# ── pure arithmetic helpers ────────────────────────────────────────────────

def card_delta(q: float) -> float:
    """Compute the raw score delta from an outcome q in [0, 100].

    delta = (q - 50) * SCORE_K.  Positive for q > 50 (success), negative for
    q < 50 (failure), zero for a neutral run.  This is the §7-P2 earn formula.
    """
    return (q - 50.0) * SCORE_K


def atom_share(card_delta_value: float, num_atoms: int) -> float:
    """The §7-P2 partial credit each atom receives when its card is reinforced.

    Each atom loaded by the card earns an equal 1/num_atoms share of the card's
    delta.  Returns 0.0 when num_atoms is 0 (safe no-op).
    """
    if num_atoms == 0:
        return 0.0
    return card_delta_value / num_atoms


def chain_hop_delta(delta: float, hop: int) -> float:
    """Decay a delta by CHAIN_DECAY^hop — the chain-rule per-hop attenuation.

    hop=0 → full delta (the tip card itself); hop=1 → CHAIN_DECAY * delta; etc.
    Returns 0.0 once the magnitude is below CHAIN_MIN_DELTA (matches the
    CardStore termination condition so callers can pre-check without a store).
    """
    decayed = delta * (CHAIN_DECAY ** hop)
    if abs(decayed) < CHAIN_MIN_DELTA:
        return 0.0
    return decayed
