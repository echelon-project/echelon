"""earn_law — pure leaf: how a structured atom's weight moves under the witnessed door.

A TINY LEAF (true-atom-is-a-tiny-leaf): pure math, stdlib only, no DB, no upward imports. The
CardStore witnessed-fetch/fire_lower methods delegate the NUMBER here so the earn law lives in one
place (the antibody to scattered re-implementations that drift — see remember-is-the-witnessed-door).

THE SETTLED LAW (remember-is-the-witnessed-door-earn-law, council-ratified, asymmetric):
  - fetched through the door -> EARNS BY DEFAULT (the choice IS the witness; no self-report, no
    outcome gate). up is MECHANICAL only.
  - read it and it MISLED you -> fire_lower() (a reader DUTY) decays it; never deleted.
  - there is NO fire_higher. weight rises only by witnessed use, falls only by the bound duty.

The kindle uses the SIMPLE mechanical earn (a first witnessed access = a kick-start, not a forced
slope — t1-cartridge-is-the-kindle-kick-start). The full time-anchored Elo law
(time-anchored-elo-weight-law: rank-relative earn, tenure-buffered decay, soft-reset) is the next
upgrade and slots in HERE behind the same two functions, no call-site change.
"""
from __future__ import annotations

# scratchboard-proven constants (prove-the-mechanism-in-scratch-before-core); tune in the live eval.
EARN_DELTA = 2.0       # per witnessed fetch — modest, repeatable; a kick-start, not a slope.
DECAY_DELTA = 10.0     # per fire_lower — down hurts more than up earns (the asymmetry).
SCORE_FLOOR = 1.0      # decay asymptotes here; NEVER deletes (append-only law).


def earn(score: float, delta: float = EARN_DELTA) -> float:
    """The witnessed-fetch earn: mechanical, unconditional. score += delta."""
    return score + delta


def decay(score: float, delta: float = DECAY_DELTA) -> float:
    """The fire_lower decay: down only, floored, never deleted. score -= delta, clamped to FLOOR."""
    return max(SCORE_FLOOR, score - delta)
