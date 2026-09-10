"""card_types — Atom and Card dataclasses (pure leaf, no DB).

Extracted verbatim from cards.py lines 79-141.  No sqlite, no threading,
no business policy.  The only dependency is uame.content_id (content-address
hasher) and the coord_norm leaf.

Public surface:
    Atom        — seed-token at a coordinate; born neutral (B=100).
    Card        — command card: refs + rating + use_count + prev edge.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from .uame import SCORE_BENCHMARK, content_id
from .coord_norm import _norm_coord


@dataclass
class Atom:
    """A seed-token at a coordinate. Born neutral (B=100); earns score only via the cards that load it.

    THE RICH FIELDS (scope/kind/valence/arousal) — added 2026-06-16 for the v2-primary read-flip.
    Until then v2 carried only (coordinate, content) — a LOSSY shadow of v1's full Entry, so v2 could
    not be the store recall ranks (it had nowhere to PUT scope, which recall filters by). These four
    let an atom be born COMPLETE in v2; they default empty/0.0 so every existing call site (add_atom
    with no extras, a re-read of an old 8-column row) stays valid. See v2-primary-code-gap-lossy-shadow.
    """
    coordinate: str
    content: str
    id: str = ""
    score: float = SCORE_BENCHMARK
    score_history: str = "[]"
    use_count: int = 0
    born_from: str = ""              # synthesis provenance (parent atom ids, json) — §9
    ts: int = field(default_factory=lambda: int(time.time()))
    scope: str = ""                  # the v1 Entry.scope — recall FILTERS by this (the load-bearing one)
    kind: str = ""                   # note/lesson/insight/... — the Entry.kind
    valence: float = 0.0             # affective sign, carried from the Entry
    arousal: float = 0.0             # affective magnitude, carried from the Entry

    def __post_init__(self):
        self.coordinate = _norm_coord(self.coordinate)
        if not self.id:
            self.id = content_id(self.content, self.coordinate.split(":", 1)[0] or "general", "atom")

    @property
    def tier(self) -> str:
        """The earned tier (NOT asserted): unproven < synthesis-eligible(L2) < beacon(L1)."""
        from .card_credit import SYNTH_ELIGIBLE, BEACON_THRESHOLD
        if self.score >= BEACON_THRESHOLD:
            return "beacon"          # L1 core candidate
        if self.score >= SYNTH_ELIGIBLE:
            return "eligible"        # L2 insight
        return "unproven"            # L3 / candidate


@dataclass
class Card:
    """The command (§6): refs (atom coordinates it composes) + rating + use_count. Forecasts an action.

    A card is ONE LAYER of a reasoning chain — not a held result. `prev` is the upstream card whose
    output this card consumed (the card→card EDGE). Reasoning = the forward pass card→card→card; no
    single card holds the answer (the same way no single weight holds a thought). The edge is what
    makes credit flow back along the chain (the chain rule), and what makes `relive` a real forward
    pass rather than re-earning isolated steps. See the-card-layer-must-be-chained.
    """
    label: str
    refs: list                       # coordinates of the atoms this card loads, in intended order
    id: str = ""
    score: float = SCORE_BENCHMARK
    score_history: str = "[]"
    use_count: int = 0
    born_from: str = ""
    prev: str = ""                   # upstream card id (the card→card edge); "" = chain head
    ts: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self):
        self.refs = [_norm_coord(r) for r in self.refs if _norm_coord(r)]
        if not self.id:
            # id is content-addressed on label+atom-refs ONLY (NOT prev) — the SAME action recurring
            # in different chains is the same card; the edge is per-occurrence, set at wire time.
            self.id = content_id(self.label + "|" + "|".join(self.refs), "card", "card")
