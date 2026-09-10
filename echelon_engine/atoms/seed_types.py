"""Seed dataclass — the caller-facing memory unit, a pure leaf atom.

Depends only on scope_map (scope_to_domain) and addressing (content_id), both pure leaves.
No DB, no IO. Re-exported from store.py for backward compat.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .scope_map import scope_to_domain
from .addressing import content_id as _content_id


@dataclass(slots=True)
class Seed:
    scope: str
    content: str
    kind: str = "note"
    tier: str = "working"
    supersedes: str = ""
    id: str = ""
    ts: int = field(default_factory=lambda: int(time.time()))
    valence: float = 0.0   # -1 unpleasant .. +1 pleasant (felt at write-time)
    arousal: float = 0.0   #  0 calm .. 1 intense
    score: float = 100.0   # COS rating (B=100 neutral); resonance grows it, promotion reads it
    recall_count: int = 0  # how many times this seed proved warm (resonance evidence)
    coordinate: str = ""   # COS taxonomy path (domain:topic:...); empty = raw/uncoordinated
    self_seed: bool = False  # "kept by WISH, requirement UNMET" — immutable origin flag. self_seed
                             # seeds live in a persona's OWN core, undeletable, but are PERMANENTLY
                             # ineligible for the global soul (levels.eligible_for_global reads this).

    def __post_init__(self) -> None:
        if not self.id:
            # Match UAME's content_id (content, domain, kind) so a Seed's id == the stored Entry's
            # id for the same content — one id scheme across the adapter and the real store.
            self.id = _content_id(self.content, scope_to_domain(self.scope, self.coordinate),
                                  self.kind)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
