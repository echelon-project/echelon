"""backfill_card_kind — one-time classify of every existing card's kind (Phase C step 5).

Runs registry.classify(born_from) over all cards; for cards that classify to 0 (unknown) but
carry SESSION STRUCTURE (prev-chained + >=2 refs), assigns 109=session_inferred so relive
recovers them honestly (recoverable, but tagged inferred not verified-wrap). Idempotent:
re-running produces the same codes. --dry-run previews without writing.

This is the ONLY mass write to echelon.db in the card.kind change — gated before it runs.
"""
from __future__ import annotations
import argparse
import json
import sys

from .atoms.cards import CardStore
from .registry import classify, name_of, is_session


def compute_backfill(cs: CardStore) -> tuple[list[tuple], dict]:
    """Return (updates, stats). updates = [(card_id, new_kind, reason), ...] for cards whose
    computed kind differs from their stored kind. Never downgrades a non-zero stored kind to 0."""
    prev_targets = {r[0] for r in cs.conn.execute(
        "SELECT DISTINCT prev FROM cards WHERE prev != ''")}
    updates, stats = [], {"total": 0, "by_marker": 0, "by_structure": 0,
                          "left_unknown": 0, "unchanged": 0}
    for r in cs.conn.execute("SELECT id, born_from, refs, prev, kind FROM cards"):
        stats["total"] += 1
        stored = (r["kind"] if "kind" in r.keys() else 0) or 0   # NULL kind -> 0 (both = unknown)
        code = classify(r["born_from"])
        reason = "marker"
        if code == 0:
            # structural fallback: an un-marked card that sits in the prev-rope with a real
            # chain is very likely a session (verified: recovers emit-chain + image-context,
            # correctly skips 1-ref fork stubs).
            refs = json.loads(r["refs"] or "[]")
            in_rope = (r["id"] in prev_targets) or bool(r["prev"])
            if in_rope and len(refs) >= 2:
                code, reason = 109, "structure"
        if code == 0:
            stats["left_unknown"] += 1
            reason = "unknown"
        elif reason == "marker":
            stats["by_marker"] += 1
        elif reason == "structure":
            stats["by_structure"] += 1
        # never overwrite a real stored code with 0 (append-only spirit: don't demote)
        if code != stored and not (code == 0 and stored != 0):
            updates.append((r["id"], code, reason))
        else:
            stats["unchanged"] += 1
    return updates, stats


def run(dry_run: bool = True) -> dict:
    cs = CardStore()
    updates, stats = compute_backfill(cs)
    session_after = sum(1 for _, k, _ in updates if is_session(k))
    if not dry_run:
        with cs._lock:
            cs.conn.executemany("UPDATE cards SET kind=? WHERE id=?",
                                [(k, cid) for cid, k, _ in updates])
            cs.conn.commit()
    return {"dry_run": dry_run, "updates": len(updates),
            "session_cards_set": session_after, "stats": stats,
            "sample": [(cid[:12], k, name_of(k), why) for cid, k, why in updates[:15]]}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill cards.kind from born_from (one-time).")
    ap.add_argument("--apply", action="store_true", help="WRITE the codes (default: dry-run)")
    a = ap.parse_args(argv)
    res = run(dry_run=not a.apply)
    print(f"=== card.kind backfill {'(APPLIED)' if a.apply else '(DRY RUN)'} ===")
    print(f"  cards updated: {res['updates']}")
    print(f"  of which session-band: {res['session_cards_set']}")
    print(f"  stats: {res['stats']}")
    print("  sample:")
    for cid, k, nm, why in res["sample"]:
        print(f"    {cid}  kind={k:4}({nm:16}) via {why}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
