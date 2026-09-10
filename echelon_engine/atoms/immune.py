"""immune — the STRUCTURE antibody as a payable front door (scan + heal).

THE GAP THIS CLOSES (owner, 2026-06-19): the immune system (CardStore.scan) was reachable only inline —
every wrap folded it in, and acting on a finding meant hand-rolling a `_foo.py` to call unlink(). So the
estate accrued OPEN DEBT it could not pay through a door: "heal N edge_to_disclaimed FAILs", "M orphan_
disclaim WARNs owed". scan() REPORTS; healing was a deliberate Python act with no CLI. This module is the
door: `scan` (read-only report) and `heal` (mechanically fix the FAIL classes that have a mechanical fix).

THE LINE between heal-able and not (the honesty boundary):
  - A FAIL is PROVABLY broken structure → mechanically healable:
      dangling_edge_from / dangling_edge_to / edge_to_disclaimed / unknown_relation
      → `unlink(from,to,relation)` (tombstone the edge; never DELETE — atlas append-over-rewrite).
  - A WARN is DRIFT the scanner can't prove → needs a REASONER decision, NOT an auto-edit:
      orphan_disclaim   → which atom supersedes it is a judgement (draw the supersedes edge by hand).
      live_contradiction→ which side is wrong is a judgement (dispute one side — see correct.py).
    heal NEVER touches a WARN. It names them as owed. Auto-resolving drift would mint the very
    significance-by-fiat the substrate forbids.

This is a THIN caller: scan() and unlink() are the engine ops; this only routes findings to unlink and
reports. See cards.py (scan/unlink), graph-scanner-is-the-immune-system, graph-scanner-composes-with-dispute.
"""
from __future__ import annotations

import argparse
import sys

from .cards import CardStore, DEFAULT_V2_DB

# The FAIL rules whose fix is mechanical by TOMBSTONE (unlink the broken edge). (A WARN never here.)
_HEALABLE_EDGE_FAILS = {"dangling_edge_from", "dangling_edge_to", "edge_to_disclaimed", "unknown_relation"}
# The FAIL rule healed by RE-POINTING (not tombstoning): an edge into a SUPERSEDED atom is moved to
# the successor that now holds the truth, so the connection is kept, not severed (OPEN-0025/0033).
_REPOINT_EDGE_FAILS = {"edge_to_superseded"}


def scan(scope: str = "echelon", store: CardStore | None = None) -> dict:
    """Read-only immune report. Returns {fail, warn, stats}. Never mutates."""
    cs = store or CardStore()
    return cs.scan(scope=scope)


def heal(scope: str = "echelon", store: CardStore | None = None, dry_run: bool = False) -> dict:
    """Mechanically heal the FAIL classes that have a mechanical fix: tombstone (unlink) each broken edge.
    WARNs are NEVER auto-fixed — they need a reasoner's judgement; they are reported as owed. With
    dry_run=True, report what WOULD be healed without touching anything. Returns
    {healed:[...], left_warn:[...], unhandled_fail:[...], stats}."""
    cs = store or CardStore()
    rep = cs.scan(scope=scope)
    healed, unhandled = [], []
    for f in rep["fail"]:
        if f["rule"] in _REPOINT_EDGE_FAILS and f.get("from_id") and f.get("to_id") and f.get("successor_id"):
            # An edge into a SUPERSEDED atom: re-point it to the successor (keep the connection,
            # now aimed at the truth), don't tombstone. This is what clears the uncleaarable
            # edge_to_disclaimed FAILs that grew as new atoms linked to a corrected slug.
            rel = f.get("relation") or _relation_of(cs, f["from_id"], f["to_id"])
            entry = {"rule": f["rule"], "from_id": f["from_id"], "to_id": f["to_id"],
                     "relation": rel, "successor_id": f["successor_id"]}
            if not dry_run and rel:
                entry["repointed"] = cs.relink(f["from_id"], f["to_id"], f["successor_id"], rel)
            healed.append(entry)
        elif f["rule"] in _HEALABLE_EDGE_FAILS and f.get("from_id") and f.get("to_id"):
            # Prefer the relation the scanner found (the exact edge that failed), fall back to
            # _relation_of for old scan outputs that predate the relation-in-findings fix.
            rel = f.get("relation") or _relation_of(cs, f["from_id"], f["to_id"])
            entry = {"rule": f["rule"], "from_id": f["from_id"], "to_id": f["to_id"],
                     "relation": rel}
            if not dry_run and entry["relation"]:
                entry["unlinked"] = cs.unlink(entry["from_id"], entry["to_id"], entry["relation"])
            healed.append(entry)
        else:
            unhandled.append(f)   # a FAIL with no mechanical fix (shouldn't happen for current rules)
    return {"healed": healed, "left_warn": rep["warn"], "unhandled_fail": unhandled,
            "dry_run": dry_run, "stats": rep["stats"]}


def _relation_of(cs: CardStore, from_id: str, to_id: str) -> str | None:
    """Recover the edge's relation when the scan finding didn't carry it (the live edge between the pair)."""
    for e in cs.edges_of(from_id, live_only=True):
        if e["to_id"] == to_id and e["dir"] == "out":
            return e["relation"]
    return None


def _render_scan(rep: dict) -> str:
    out = [f"IMMUNE SCAN: {rep['stats']}"]
    for f in rep["fail"]:
        out.append(f"  FAIL {f['rule']} | {f['detail']}")
    for w in rep["warn"]:
        out.append(f"  warn {w['rule']} | {str(w.get('atom_id', w.get('to_id', '')))[:16]} | {w['detail']}")
    if not rep["fail"] and not rep["warn"]:
        out.append("  clean — no broken memories")
    return "\n".join(out)


def _render_heal(rep: dict) -> str:
    verb = "WOULD heal" if rep["dry_run"] else "healed"
    out = [f"IMMUNE HEAL ({verb}): {len(rep['healed'])} edge(s); {rep['stats']}"]
    for h in rep["healed"]:
        if h.get("successor_id"):   # a re-point (edge_to_superseded)
            done = h.get("repointed")
            mark = "(dry-run)" if rep["dry_run"] else ("ok" if done else "no-op")
            out.append(f"  {verb} {h['rule']} {str(h['from_id'])[:12]} -{h['relation']}-> "
                       f"{str(h['to_id'])[:12]} ⇒ {str(h['successor_id'])[:12]}  {mark}")
        else:
            mark = "(dry-run)" if rep["dry_run"] else ("ok" if h.get("unlinked") else "no-op")
            out.append(f"  {verb} {h['rule']} {str(h['from_id'])[:12]} -{h['relation']}-> {str(h['to_id'])[:12]}  {mark}")
    for w in rep["left_warn"]:
        out.append(f"  OWED (reasoner judgement, not auto-healed) {w['rule']} | {w['detail']}")
    for f in rep["unhandled_fail"]:
        out.append(f"  UNHANDLED FAIL {f['rule']} | {f['detail']}")
    return "\n".join(out)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="The structure antibody: scan the memory graph, heal broken edges.")
    ap.add_argument("action", choices=["scan", "heal"], help="scan = read-only report; heal = tombstone broken edges")
    ap.add_argument("--scope", default="echelon")
    ap.add_argument("--dry-run", action="store_true", help="(heal) report what would be healed without touching")
    a = ap.parse_args(argv)
    cs = CardStore()
    if a.action == "scan":
        rep = scan(a.scope, cs)
        print(_render_scan(rep))
        return 1 if rep["fail"] else 0
    rep = heal(a.scope, cs, dry_run=a.dry_run)
    print(_render_heal(rep))
    return 1 if rep["unhandled_fail"] else 0


if __name__ == "__main__":
    sys.exit(_main())
