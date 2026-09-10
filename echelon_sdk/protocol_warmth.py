"""protocol_warmth — recognising a Protocol card by the WARMTH of its atoms (not a stored type flag).

The autonomy organ's RECOGNITION half ([[card-type-is-emergent-warmth-not-a-stamp]],
[[the-protocol-tool-is-the-autonomy-organ]]). A card is a container of ordered atom refs; whether it
BEHAVES as a Protocol (a runnable directed path) vs an Insight (a static symmetric cluster) is computed
LIVE from the DIRECTED warmth between its refs — never asserted as `type=protocol` (that would be the
cache-lie the soul refuses).

Co-authored: gemma designed the derivation + the directed-path criterion, smollm implemented, OS gated
(this docstring + the bug-fixes are the gate). The directed-warmth derivation (gemma): since uame_links
edges carry NO weight column, the directed strength A->B is DERIVED from (edge exists) × (relation type:
depends>supersedes>born_from>relates) × (target warmth) × (target recall bonus). A non-existent edge = 0.

`warm` is injected as a callable warm(from_ref, to_ref) -> float so this module stays WALL-CLEAN: it does
not reach across the UAME(core.db, edges) / CardStore(core_v2.db, atom scores) boundary itself — the
caller supplies the directed-warmth accessor wired to whichever store(s) hold the truth. Pure stdlib.
"""
from __future__ import annotations
from typing import Callable

# relation -> sequential-strength multiplier (gemma's derivation; a 'depends' edge is a stronger
# order signal than a loose 'relates'). Unknown relations fall back to the weakest (0.9).
REL_FACTOR = {"depends": 1.2, "supersedes": 1.05, "born_from": 1.0, "relates": 0.9}
_DEFAULT_REL = 0.9


def derive_directed_warmth(relation: str | None, target_score: float, target_recall: int,
                           *, norm: float = 200.0, recall_k: float = 0.01) -> float:
    """Derive the directed strength of one edge A->B from what the substrate HAS (no edge-weight column).
    relation=None (no A->B edge) -> 0.0. Else: RelFactor(relation) * (target_score/norm) *
    (1 + recall_k*target_recall). Gemma's formula; OS-gated so a missing edge is 0, not 1."""
    if relation is None:
        return 0.0
    rel = REL_FACTOR.get(relation, _DEFAULT_REL)
    return rel * (target_score / norm) * (1.0 + recall_k * max(0, target_recall))


def protocol_score(refs: list[str], warm: Callable[[str, str], float]) -> dict:
    """Measure how PROTOCOL-like an ORDERED list of atom refs is (vs a static cluster).

    warm(a, b) -> directed warmth a->b (e.g. derive_directed_warmth wired to the stores).
    A PROTOCOL = high SEQUENTIAL directed warmth (forward >> pairwise, asymmetric forward>backward).
    An INSIGHT = high but SYMMETRIC warmth (asymmetry ~ 0). Computed live; the ratio IS the type.

    Returns forward/backward/avg_pairwise/asymmetry/protocol_score/is_protocol.
    (smollm's impl, OS-verified: the len<2 guard + the ratio are correct as written.)
    """
    n = len(refs)
    if n < 2:
        return {"forward": 0.0, "backward": 0.0, "avg_pairwise": 0.0,
                "asymmetry": 0.0, "protocol_score": 0.0, "is_protocol": False}
    forward = sum(warm(refs[i], refs[i + 1]) for i in range(n - 1)) / (n - 1)
    backward = sum(warm(refs[i + 1], refs[i]) for i in range(n - 1)) / (n - 1)
    avg_pairwise = sum(warm(a, b) for a in refs for b in refs if a != b) / (n * (n - 1))
    asymmetry = forward - backward
    pscore = forward / avg_pairwise if avg_pairwise > 0 else 0.0
    return {"forward": forward, "backward": backward, "avg_pairwise": avg_pairwise,
            "asymmetry": asymmetry, "protocol_score": pscore,
            # a card BEHAVES as a protocol when its forward path dominates the cluster AND is directional
            "is_protocol": pscore > 1.3 and asymmetry > 0.05}
