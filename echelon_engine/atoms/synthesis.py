"""Synthesis — the COS atom-birth mechanism (COSys_DESIGN Layer 6), ported as PROPOSALS.

COS sec 9: two high-rated atoms that combine well -> a new atom is born. Core principles,
held faithfully:
  - BOTH parents must be PROVEN: score > B+25 (synthesis only promotes already-resonant content).
  - It is DATA, not conflict resolution: "Both originals remain. The synthesis produces a third."
    Append-only — parents are never touched (the substrate's law).
  - AI PROPOSES, user approves (COS sec, "Lean toward: AI proposes, user approves"). So this module
    DETECTS candidate pairs and records a PROPOSAL; it does NOT write merged content (that needs a
    model call and a human gate — a separate approved step). The hard part is the detection.

What COS gets from coordinates (knowing two atoms are "in the same context"), our scope-less seeds
get from a SEED-VS-SEED warmth matcher: two seeds are merge-candidates when they are MUTUALLY WARM
(high symmetric lexical overlap with EACH OTHER) AND both proven. This is the new piece — warmth.py
scores reasoning-vs-seed (asymmetric); synthesis needs seed-vs-seed (symmetric, neither privileged).

A proposal is itself stored as a seed (kind='synthesis-proposal') so it's content-addressed,
inspectable, and decays/proves like anything else. Approving it (writing the merged child with
born_from) is the deferred step. See core-values-grow, the COS port (commits 6e2f6a6/00dc4e4/66d7022).
"""
from __future__ import annotations
import json

from .store import SeedStore, Seed, PROMOTE_THRESHOLD
from .warmth import _tokens, _ngrams

# How mutually-warm two seeds must be to be merge-candidates. Symmetric overlap (Jaccard on the
# blended uni+bi gram sets). High bar: synthesis should fire on genuine near-duplicates / two
# facets of one idea, not loose topical neighbours.
SYNTH_AFFINITY = 0.40


def _gramset(text: str) -> set:
    """Blended unigram + bigram set for a seed's content (the shape the matcher compares)."""
    toks = _tokens(text)
    return set(toks) | _ngrams(toks, 2)


def affinity(a: Seed, b: Seed) -> float:
    """Symmetric warmth between two seeds: Jaccard overlap of their gram sets. Neither is
    privileged (unlike reasoning-vs-seed). 1.0 = identical grams, 0.0 = disjoint."""
    ga, gb = _gramset(a.content), _gramset(b.content)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    union = len(ga | gb)
    return inter / union if union else 0.0


def find_pairs(store: SeedStore, scope: str | None = None,
               affinity_floor: float = SYNTH_AFFINITY,
               proven_floor: float = PROMOTE_THRESHOLD) -> list[dict]:
    """Find synthesis-CANDIDATE pairs: both seeds PROVEN (score > B+25) AND mutually warm
    (affinity >= floor). Returns the pairs ranked by affinity — the proposals to consider.
    Does NOT write anything. Pure detection (the hard part of COS synthesis)."""
    seeds = [s for s in store.seeds(scope=scope) if s.score > proven_floor]
    pairs = []
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            a, b = seeds[i], seeds[j]
            if a.id == b.id:
                continue
            aff = affinity(a, b)
            if aff >= affinity_floor:
                pairs.append({
                    "a": a.id, "b": b.id, "a_scope": a.scope, "b_scope": b.scope,
                    "affinity": round(aff, 3),
                    "parent_score_avg": round((a.score + b.score) / 2.0, 1),
                    "a_content": a.content[:120], "b_content": b.content[:120],
                })
    pairs.sort(key=lambda p: p["affinity"], reverse=True)
    return pairs


def propose(store: SeedStore, scope: str | None = None,
            target_scope: str = "synthesis-proposals") -> list[dict]:
    """Detect candidate pairs and RECORD each as a proposal seed (kind='synthesis-proposal',
    born_from in the content). Append-only, content-addressed (re-running is idempotent —
    the same pair yields the same proposal id). Returns the proposals recorded. The merged-content
    write (approval) is the deferred, model-gated step — NOT done here."""
    pairs = find_pairs(store, scope=scope)
    recorded = []
    for p in pairs:
        body = json.dumps({
            "born_from": [p["a"], p["b"]],
            "affinity": p["affinity"],
            "parent_score_avg": p["parent_score_avg"],
            "a": p["a_content"], "b": p["b_content"],
        }, ensure_ascii=False)
        pid = store.remember(target_scope, body, kind="synthesis-proposal",
                             valence=0.2, arousal=0.3)   # mild curiosity — a candidate, unproven
        recorded.append({"proposal_id": pid, **p})
    return recorded
