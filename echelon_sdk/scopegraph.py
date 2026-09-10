"""The scope graph — cross-scope warmth, read from THE ATLAS (not a new table).

Owner, 2026-06-05: "atlas is your answer." The relationship-index that lets memory
travel between scopes (the soul) ALREADY EXISTS, proven + scanner-enforced: the ECHELON
atlas. A scope == an atlas node id; a relationship == a typed edge in
architecture/substrate.json ({from,to,rel,label}). We do NOT build a scope_edges table —
we CONSUME the atlas graph. The atlas enforces the graph's integrity; the memory organ
reads it to know which scopes a given scope draws warmth from.

Frame-guard ([[affect-papers-are-a-frame-trap]]): an edge modulates RECOGNITION REACH
(which autobiographical seeds are eligible to be re-felt across a defined relationship),
NOT activation. Pure rediscovery across a relationship. Stays on ECHELON's basis.

The soul travels here: a scope linked to a 'self'/soul scope draws that soul's seeds at
full strength on EVERY task — the soul is "always loaded" by an explicit, editable edge,
not a hardcoded global tier.
"""
from __future__ import annotations
import json
from pathlib import Path
from .estate_paths import command_root as _command_root

# The atlas substrate wiring. Default location; overridable for tests.
_ATLAS_ROOT = _command_root()
DEFAULT_ATLAS = ((_ATLAS_ROOT / "architecture" / "substrate.json")
                 if _ATLAS_ROOT else Path("architecture/substrate.json"))

# Relationship -> how strongly warmth flows ACROSS that edge (0..1). A scope always draws
# its OWN seeds at 1.0; these scale the neighbour's seeds. Tuned by what the rel MEANS:
#   strong draw (you literally build on it) vs faint cross-pollination (siblings).
_REL_WEIGHT = {
    "shares_soul":        1.0,   # the soul scope — always fully present
    "claims":             1.0,   # ADOPTION at atom grain — a claimed atom is your own (owner 2026-07-24)
    "depends_on":         0.8,   # you build on it; its lessons are nearly your own
    "evolved_from":       0.7,   # lineage — you inherit the parent's hard-won memory
    "merged_into":        0.7,
    "subsumes":           0.7,
    "provides_to":        0.6,
    "calls":              0.5,
    "specifies":          0.5,
    "decision_echoed_in": 0.5,   # same decision re-instantiated elsewhere
    "sibling":            0.3,   # weak cross-pollination
}
_DEFAULT_WEIGHT = 0.4


class ScopeGraph:
    def __init__(self, atlas_path: Path | str = DEFAULT_ATLAS):
        self.atlas_path = Path(atlas_path)
        self._edges: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.atlas_path.exists():
            return  # no atlas -> graph is empty -> warmth stays single-scope (graceful)
        try:
            data = json.loads(self.atlas_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        rel = data.get("relationship", {})
        for group in rel.values():          # lineage / wiring / convergence / ...
            if isinstance(group, list):
                self._edges.extend(e for e in group if "from" in e and "to" in e)

    def neighbours(self, scope: str) -> list[tuple[str, float, str]]:
        """Scopes `scope` draws warmth from: [(other_scope, weight, rel), ...].

        Directed: an edge from `scope`->X means scope BUILDS ON X, so scope draws X's
        memory (the dependency's lessons are nearly your own). We follow OUT-edges.
        Edges carrying an `atoms` filter are ATOM-GRAIN (see claims()) and are skipped
        here — a whole-scope draw on a per-atom claim would adopt the member's entire
        memory, the exact overreach the filter exists to prevent."""
        out: list[tuple[str, float, str]] = []
        for e in self._edges:
            if e["from"] == scope and not e.get("atoms"):
                rel = e.get("rel", "")
                out.append((e["to"], _REL_WEIGHT.get(rel, _DEFAULT_WEIGHT), rel))
        # de-dup keeping the strongest weight per neighbour
        best: dict[str, tuple[str, float, str]] = {}
        for s, w, r in out:
            if s not in best or w > best[s][1]:
                best[s] = (s, w, r)
        return list(best.values())

    def claims(self, scope: str) -> list[tuple[str, float, list[str]]]:
        """Atom-grain adoptions: [(member_scope, weight, [slug, ...]), ...] from `claims`
        edges carrying an `atoms` filter (owner 2026-07-24: 'echelon could claim that atom' —
        the per-atom sibling of the group-scope whole-scope reach). The claimed atom stays in
        its home scope with its id/weight/history untouched; recall simply draws EXACTLY those
        coordinates at the claim weight."""
        out: list[tuple[str, float, list[str]]] = []
        for e in self._edges:
            if e["from"] == scope and e.get("rel") == "claims" and e.get("atoms"):
                out.append((e["to"], _REL_WEIGHT["claims"], list(e["atoms"])))
        return out

    def claimants_of(self, home_scope: str, slug: str) -> list[str]:
        """THE REVERSE INDEX (council directive 2026-07-24, open + home-anchored responsibility):
        which scopes have adopted this atom via a `claims` edge. Only the HOME scope edits/retires
        an atom; this makes the blast radius of a home edit queryable BEFORE the edit. Normalizes
        dash/underscore because stored coordinates are snake_case while edges keep kebab-case."""
        norm = lambda t: (t or "").lower().replace("-", "_")
        h, s = norm(home_scope), norm(slug)
        return sorted({e["from"] for e in self._edges
                       if e.get("rel") == "claims" and e.get("atoms") and norm(e.get("to", "")) == h
                       and any(norm(a) == s for a in e["atoms"])})

    def add_edge(self, frm: str, to: str, rel: str = "shares_soul") -> None:
        """Register a programmatic edge (not from the atlas file). Used for PERSONA inheritance: a
        persona scope gets a `shares_soul`/`evolved_from` edge to the global soul at birth, so it
        DRAWS the global L1/L2 warmth down the edge (persona.py). Idempotent on (from,to,rel)."""
        if not any(e["from"] == frm and e["to"] == to and e.get("rel") == rel for e in self._edges):
            self._edges.append({"from": frm, "to": to, "rel": rel})
