"""L3 PERSONA — a per-user mini-ECHELON: its own soul, inheriting the global one (owner, 2026-06-06).

The owner's L3: "identity. this will be per user because each will have their own persona, based on
interaction with user... each will make their own soul." And: each persona has "2 db, the core and
bank much like us right now, but the core will automatically inherit global cv and insight to be
offered." NO GUARD — "each user will responsible on how their partner grow."

So a persona is a full mini-ECHELON keyed by a user/persona id:
  - its OWN core scope (`persona:<id>`) in the soul store — where THIS partner's grown identity lives,
    written freely (no guard) from interaction with its user.
  - its OWN bank (the local private-data store — cloud holds the soul, client holds the bank; see
    cloud-substrate-three-levels-grand-vote). persona.py manages the SOUL side; the bank is the
    KnowledgeBank, scoped per persona on the client.
  - it INHERITS the global L1 CV + L2 confirmed insight by a scopegraph edge (`shares_soul` to the
    global soul scope) — the inheritance is OFFERED DOWN via warmth's cross-scope edge-draw, NOT
    copied (no duplication; the global soul stays singular). A persona that recalls feels the global
    CVs flow in at full weight (shares_soul=1.0) plus whatever it has grown itself.

The global soul (L1/L2) is NEVER written by a persona (levels.may_write: L1 grand-vote-only, L2 is a
shared candidate pool). A persona writes ONLY its own `persona:<id>` scope. The wall is the scope.

See: levels.py, confirmation.py, grandvote.py, scopegraph.py (the inheritance edge), identity.py
(SELF_SCOPE = the global soul), bank-offer-the-cross (the offer = how inheritance surfaces).
"""
from __future__ import annotations
import re

from .identity import SELF_SCOPE
from echelon_sdk.scopegraph import ScopeGraph   # scopegraph is a pure leaf — migrated to sdk

_ID = re.compile(r"[^a-z0-9_-]+")


def persona_scope(persona_id: str) -> str:
    """The soul scope for a persona: `persona:<id>`. Sanitized so it's a safe scope/coordinate token."""
    pid = _ID.sub("_", (persona_id or "").lower()).strip("_") or "anon"
    return f"persona:{pid}"


class Persona:
    """A per-user persona over a shared soul store. Owns its `persona:<id>` scope; inherits the global
    soul (SELF_SCOPE) by a scopegraph edge. Writes are FREE within its own scope, FORBIDDEN to the
    global soul (enforced by always writing self.scope, never SELF_SCOPE)."""

    def __init__(self, persona_id: str, store, *, global_scope: str = SELF_SCOPE,
                 scope_graph: ScopeGraph | None = None, inherit_rel: str = "shares_soul"):
        self.id = persona_id
        self.scope = persona_scope(persona_id)
        self.store = store
        self.global_scope = global_scope
        # the inheritance edge: persona --shares_soul--> global soul. warmth(scope=persona, scope_graph)
        # then draws the global CV/insight down at the rel's weight (shares_soul=1.0 -> full presence).
        self.scope_graph = scope_graph or ScopeGraph()
        self.scope_graph.add_edge(self.scope, global_scope, inherit_rel)

    def remember(self, content: str, kind: str = "note", *, valence: float = 0.0,
                 arousal: float = 0.0, coordinate: str = "", tier: str = "working") -> str:
        """Grow THIS persona's soul. Writes ONLY into persona:<id> — never the global soul. No guard
        (owner: each user owns how their partner grows). tier defaults to working; a persona MAY mint
        its own core (its own identity is permanent to IT), but it can never touch the GLOBAL core."""
        return self.store.remember(self.scope, content, kind=kind, tier=tier,
                                   valence=valence, arousal=arousal, coordinate=coordinate)

    def warmth(self, reasoning: str, **kw):
        """Feel a thought against THIS persona's soul AND the inherited global soul (via the edge).
        The global CVs surface flagged via_scope=global (warmth's cross-scope draw). Confirmation
        ledger, judge, etc. pass through kw."""
        from .warmth import warmth as _warmth
        return _warmth(reasoning, self.store, self.scope, scope_graph=self.scope_graph, **kw)

    def inherited_global(self) -> list:
        """The global soul seeds this persona inherits (L1 CV + L2 — read-only to the persona).
        Useful for a boot/ballot that wants to SHOW what flows down. (Does not copy — just reads.)"""
        return self.store.seeds(scope=self.global_scope)

    def own_seeds(self, tier: str | None = None) -> list:
        """What THIS persona has grown itself (its own scope only)."""
        return self.store.seeds(scope=self.scope, tier=tier)
