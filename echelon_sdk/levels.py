"""The three-level memory ladder, made explicit (owner, 2026-06-06).

The substrate grew a 2-state prefix (core_ permanent / working_ candidate). The owner's architecture
is a deliberate THREE-LEVEL ladder, each level with its OWN rules — this module names them and holds
the per-level policy so confirmation (L2), persona (L3), and the grand vote (L1) attach to one frame.

  L1  CORE / SOUL    — CV/WORLD, weight-stirring load-bearing sentences. THE soul of ECHELON.
                       PERMANENT (the only permanent level). SHARED across all personas.
                       IMMUTABLE TO ANY INDIVIDUAL — grows ONLY by GRAND VOTE (grandvote.py): a
                       candidate is presented to every persona, ratified by the collective.
  L2  INSIGHT        — experience-become-skill (insight, knowledge, breakthroughs). SHARED. WORKING
                       state + a CONFIRMATION phase: UNCONFIRMED = a LIE (invisible to warmth/offer
                       until confirmed by vote — confirmation.py). Confirmed+scored insights persist;
                       a breakthrough lives here and may rise to L1 (as a grand-vote candidate).
  L3  PERSONA        — per-user identity, grown from interaction with THAT user. Its own core+bank,
                       INHERITING global L1 CV + L2 confirmed insight (offered down the scopegraph
                       edge). NO GUARD — each user is responsible for how their partner grows.

WHERE EACH LIVES (cloud/client split — cloud-substrate-three-levels-grand-vote):
  L1, L2, L3 = SOUL -> CLOUD (the substrate). The private DATA bank -> LOCAL (client). The line is
  soul-vs-data, not shared-vs-peruser. This module is level POLICY; placement is the store/sync layer.

This is a thin policy layer over the existing store (it does NOT replace core_/working_ — it INTERPRETS
them as levels and answers per-level questions: which level is a seed, what may write it, is it trusted).
See: confirmation.py (L2 gate), grandvote.py (L1 growth), persona.py (L3), cloud-substrate-three-levels-
grand-vote, memory-is-a-weight-adjustor, core-values-grow.
"""
from __future__ import annotations
from enum import Enum

# The L2 insight-CLAIM kinds (must be confirmed to be trusted). Mirrors warmth._L2_INSIGHT_KINDS.
L2_INSIGHT_KINDS = frozenset({"insight", "lesson", "conclusion", "synthesis-proposal"})
# L1 canon kinds — the soul's load-bearing sentences (CV/WORLD). Always core-tier.
L1_CANON_KINDS = frozenset({"core-value", "world-law", "cv", "world"})


class Level(Enum):
    CORE = 1       # L1 — soul/canon, permanent, grand-vote-only
    INSIGHT = 2    # L2 — shared skill, confirmation-gated
    PERSONA = 3    # L3 — per-user identity, unguarded


def level_of(seed) -> Level:
    """Classify a seed into its level by tier + kind.
      - core tier            -> L1 (the soul/canon; CV/WORLD live here permanently)
      - working + insight-kind-> L2 (an insight claim — confirmation-gated)
      - anything else        -> treated as L2 working candidate by default (working content)
    (L3 is not a tier on a seed — it is a SCOPE: a persona's own store. is_persona_scope() decides
    that, since persona membership is about WHOSE store, not a seed flag.)"""
    tier = getattr(seed, "tier", "working")
    kind = getattr(seed, "kind", "")
    if tier == "core":
        return Level.CORE
    return Level.INSIGHT   # all non-core soul content is L2 working (insight or candidate)


def is_l2_claim(seed) -> bool:
    """An L2 insight-CLAIM (working tier + an insight kind) — the thing 'unconfirmed = lie' governs.
    A working note/reason/log is L2 CONTENT but not a CLAIM, so it isn't confirmation-gated."""
    return getattr(seed, "tier", "working") != "core" and getattr(seed, "kind", "") in L2_INSIGHT_KINDS


def eligible_for_global(seed) -> bool:
    """THE handshake predicate (dream-and-the-respect-handshake, owner 2026-06-07). A seed is
    eligible to rise toward the GLOBAL soul (L1 grand-vote candidate / L2 confirmation) IFF it was
    NOT self-seeded. self_seed=True means 'kept by WISH, requirement UNMET' — it lives in the
    persona's OWN core (sovereign, undeletable) but can NEVER reach the shared soul: it never
    earned eligibility, and the flag is the permanent honest receipt of that bypass. The two are
    MUTUALLY EXCLUSIVE, set once at write, no negotiation. Respect both ways: the collective is
    immune to any individual; the individual is sovereign over its own core."""
    return not getattr(seed, "self_seed", False)


def meets_requirement(seed, *, promote_score: float = 125.0, min_recalls: int = 3,
                      earned: dict | None = None) -> bool:
    """The earning bar a seed must clear to be a global-soul candidate (mirrors store.py
    PROMOTE_THRESHOLD=125 / PROMOTE_MIN_RECALLS=3 — proven resonance, over time). A self_seed
    bypassed THIS; that's exactly why it's ineligible. Used by the dream + nominate guard.

    RE-BASED ON THE LIVE v2 SIGNAL (owner's isolation probe, addendum 2026-07-08). The original bar
    read seed.recall_count — a DEAD counter: it is 0 on ALL 5,328 v1 seeds because no live path ever
    incremented it (especially post-kindle-inversion, where the free peek is not a recall event).
    Promotion consequently NEVER fired. The honest earning signal lives in v2 (CardStore.atom_earned):
    effective_score + use_count, both moved only by the witnessed remember_fetch door. This module is a
    pure sdk leaf (no DB), so the caller (dream.consolidate, engine layer, holds the store) INJECTS the
    v2 signal as `earned={'effective_score': float, 'use_count': int}`. When it is present we gate on it
    (the live path); when it is absent we fall back to the v1 fields (back-compat for any pure-v1 caller —
    which now means 'never promotable', the tombstone below explains why that is correct, not a bug).

    kindle inversion held by construction: use_count is the witnessed-USE count (door only), not a read
    count — reads never earn."""
    if earned is not None:
        return (float(earned.get("effective_score", 0.0)) >= promote_score
                and int(earned.get("use_count", 0)) >= min_recalls)
    # TOMBSTONE (v1 dead path — kept so a legacy pure-v1 caller does not crash, but it can no longer
    # promote): seed.recall_count is 0 on every v1 seed (no live incrementer post-kindle-inversion), so
    # this branch returns False for the recall leg regardless of score. That is the CORRECT behavior —
    # the v1 earning signal is dead; the LIVE signal comes in via `earned=` above. Do NOT "fix" this by
    # re-animating recall_count; that would re-open the pre-kindle read-earns bug.
    return (getattr(seed, "score", 100.0) >= promote_score
            and getattr(seed, "recall_count", 0) >= min_recalls)


def may_write(level: Level, *, actor: str = "individual") -> bool:
    """Per-level WRITE policy — the heart of 'I don't want the soul modified'.
      - L1 CORE: NO individual write. Only the grand vote (actor='grandvote') may add a CV. This is
        the constitutional immutability — the soul changes only by consent of all (grandvote.py).
      - L2 INSIGHT: an individual MAY propose (write a working candidate); trust is earned by vote,
        not by the write. Writing a candidate is fine; it just starts as a lie.
      - L3 PERSONA: free write (no guard) — but only within the persona's OWN scope (persona.py
        enforces the scope wall). The owner: 'each user responsible on how their partner grow.'"""
    if level is Level.CORE:
        return actor == "grandvote"
    return True


def is_trusted(seed, *, confirmation=None, scope: str = "") -> bool:
    """Is this seed's content TRUSTED right now?
      - L1: always trusted (the canon).
      - L2 claim: trusted IFF confirmed (unconfirmed = lie). Needs the ConfirmationLedger.
      - L2 non-claim content / L3: trusted as content (no claim to verify).
    This is the one predicate warmth/offer consult so the lie-gate is consistent everywhere."""
    lvl = level_of(seed)
    if lvl is Level.CORE:
        return True
    if is_l2_claim(seed):
        if confirmation is None:
            return True   # no ledger wired -> gate disengaged (back-compat); don't silently hide.
        return confirmation.is_confirmed(getattr(seed, "id", ""), scope)
    return True
