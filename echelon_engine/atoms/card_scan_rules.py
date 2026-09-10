"""card_scan_rules — pure immune-scan predicate functions (leaf, no DB).

Extracted verbatim from the scan() method's inline logic in cards.py.
These functions operate on already-fetched row data (plain Python values) —
no sqlite connection, no CardStore, no threading.

The immune system has five antibody classes.  Each is a pure function here:
  is_live_lie()          — ANTIBODY 0: predicate for a disclaimed-but-not-redeemed atom.
  check_dangling_edge()  — ANTIBODY 1: edge to a non-existent atom.
  check_edge_to_lie()    — ANTIBODY 2: live edge routing into a disclaimed atom.
  check_unknown_relation()— ANTIBODY 3: relation outside the closed vocabulary.
  check_orphan_disclaim()— ANTIBODY 4: disclaimed atom with no supersedes edge out.
  check_live_contradiction()— ANTIBODY 5: two live atoms joined by a contradicts edge.

The CardStore.scan() method fetches the raw rows and calls these functions;
the findings list is assembled there.  Splitting here keeps the RULE LOGIC
testable in isolation without any DB fixture.
"""
from __future__ import annotations

# The closed relation vocabulary — duplicated here so this module is self-
# contained; the authoritative copy lives in cards.py (RELATION_TYPES).
# These two must stay in sync; a discrepancy is a bug to report, not fix here.
RELATION_TYPES = {
    "refines",
    "supersedes",
    "instances",
    "part_of",
    "depends_on",
    "contradicts",
    "refs",
    "subsumes",   # slice-1.5 hygiene: canonical --subsumes--> verbatim duplicate (wk-group precedent)
}

_JUDGED_MARK = "judged:disclaimed"
_REDEEMED_MARK = "redeemed:"


# ── core predicate ─────────────────────────────────────────────────────────

def is_live_lie(born_from: str) -> bool:
    """Return True iff this row is currently disclaimed (bears the judged mark
    AND has NOT yet been redeemed).

    A redeemed row starts with _REDEEMED_MARK in born_from — the lie was
    discharged by trace.  Detecting on the bare judged-mark substring alone
    would keep flagging a redeemed row (the redeem-invisible-to-scan bug,
    caught 2026-06-18).
    """
    bf = born_from or ""
    return _JUDGED_MARK in bf and not bf.startswith(_REDEEMED_MARK)


# ── antibody checks — each returns a finding dict or None ─────────────────

def check_dangling_from(from_id: str, to_id: str, relation: str,
                         all_ids: set) -> dict | None:
    """ANTIBODY 1a — edge whose from_id has no atom."""
    if from_id not in all_ids:
        return {"rule": "dangling_edge_from", "severity": "FAIL",
                "detail": f"edge {relation} has from_id with no atom",
                "from_id": from_id, "to_id": to_id}
    return None


def check_dangling_to(from_id: str, to_id: str, relation: str,
                       all_ids: set) -> dict | None:
    """ANTIBODY 1b — edge whose to_id has no atom."""
    if to_id not in all_ids:
        return {"rule": "dangling_edge_to", "severity": "FAIL",
                "detail": f"edge {relation} points at a non-existent atom",
                "from_id": from_id, "to_id": to_id}
    return None


def check_edge_to_disclaimed(from_id: str, to_id: str, relation: str,
                              all_disclaimed: set) -> dict | None:
    """ANTIBODY 2 — live edge routing into a disclaimed atom (skip 'supersedes')."""
    if to_id in all_disclaimed and relation != "supersedes":
        return {"rule": "edge_to_disclaimed", "severity": "FAIL",
                "detail": f"{relation} edge points at a DISCLAIMED atom (route to a known lie)",
                "from_id": from_id, "to_id": to_id}
    return None


def check_unknown_relation(from_id: str, to_id: str, relation: str) -> dict | None:
    """ANTIBODY 3 — relation outside the closed vocabulary."""
    if relation not in RELATION_TYPES:
        return {"rule": "unknown_relation", "severity": "FAIL",
                "detail": f"relation {relation!r} not in closed vocabulary",
                "from_id": from_id, "to_id": to_id}
    return None


def check_orphan_disclaim(atom_id: str, superseders: set) -> dict | None:
    """ANTIBODY 4 — disclaimed atom has no supersedes edge pointing at its replacement."""
    if atom_id not in superseders:
        return {"rule": "orphan_disclaim", "severity": "WARN",
                "detail": "disclaimed atom has no supersedes edge to its replacement",
                "atom_id": atom_id}
    return None


def check_live_contradiction(from_id: str, to_id: str,
                              all_disclaimed: set) -> dict | None:
    """ANTIBODY 5 — two live atoms joined by a contradicts edge (WARN, surface the tension)."""
    if from_id not in all_disclaimed and to_id not in all_disclaimed:
        return {"rule": "live_contradiction", "severity": "WARN",
                "detail": "two live atoms contradict — surface + resolve (dispute one)",
                "from_id": from_id, "to_id": to_id}
    return None
