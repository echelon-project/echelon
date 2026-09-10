"""Entry dataclass — a pure leaf atom describing one UAME row.

Depends only on addressing (content_id, domain_of) and stdlib. No DB, no IO.
Re-exported from uame.py for backward compat.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from .addressing import content_id, domain_of


@dataclass
class Entry:
    content: str
    domain: str = "general"          # COS top level -> the table
    coordinate: str = ""             # COS sub-path within the domain
    kind: str = "atom"
    permanent: bool = False          # True -> core_<domain>; False -> working_<domain>
    scope: str = ""
    supersedes: str = ""
    id: str = ""
    ts: int = field(default_factory=lambda: int(time.time()))
    ttl: int = 0                     # 0 = durable; >0 = seconds-to-live. Only the bank family uses it.
    valence: float = 0.0
    arousal: float = 0.0
    score: float = 100.0
    recall_count: int = 0
    self_seed: bool = False          # "kept by WISH, requirement UNMET". Immutable origin flag, set
                                     # once at write. self_seed -> own persona core, undeletable, but
                                     # PERMANENTLY ineligible for the global soul (dream-and-the-respect-
                                     # handshake). Never cleared. levels.eligible_for_global reads it.
    family: str = ""                 # "" -> core/working by `permanent`; else an explicit prefix
                                     # family (e.g. "bank") for content entries — see COS × ENTRY.

    def __post_init__(self):
        if not self.domain or self.domain == "general":
            self.domain = domain_of(self.coordinate)
        if not self.id:
            self.id = content_id(self.content, self.domain, self.kind)
