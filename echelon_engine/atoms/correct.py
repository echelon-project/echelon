"""correct — the CONTENT antibody + introspection as a front door (dispute / disclaim / redeem / inspect).

THE GAP THIS CLOSES (owner, 2026-06-19): the substrate could SAY "this memory is wrong/stale" only from
raw Python. `dispute` is the hand the dream lacked — re-base a stale-but-honestly-authored atom to the
JUDGED_FLOOR so a cold reader stops trusting it — but it had no door. Same for `disclaim` (the counterfeit
self-rated class), `redeem` (a disclaimed row a real TRACE proves true again — trace-only, never say-so),
and `inspect` (an atom's earned trajectory — the thing I kept hand-rolling a `_probe.py` to read).

THE HONESTY LAWS these doors preserve (they are thin callers; the laws live in the engine ops):
  - dispute  : source='reasoner' → can drive a claim DOWN, can NEVER redeem one. Re-bases to JUDGED_FLOOR,
               never deletes (the stale claim stays legible), stamps reason + what superseded it.
  - disclaim : the counterfeit (self-rated/judged-origin) class → JUDGED_FLOOR, no use bump, idempotent.
  - redeem   : TRACE-ONLY (the Hole-4 gate). A say-so source is REFUSED. Needs a witness receipt.
  - inspect  : READ-ONLY. The soul score + the structured earned trajectory (atom_earned) + live edges.

See cards.py (dispute/disclaim_judged/redeem_by_trace), warmth-rewarded-reading-not-truth,
recall-is-a-dead-link-the-bank-never-witnesses, the §Hole-4 redemption gate.
"""
from __future__ import annotations

import argparse
import json
import sys

from .cards import CardStore, DEFAULT_V2_DB


def _resolve(cs: CardStore, ref: str, table: str) -> str | None:
    """A ref may be a raw row id or (for atoms) a slug/coordinate. Resolve to the row id."""
    if table == "atoms":
        return cs.atom_id_for_coordinate(ref) or (ref if _row_exists(cs, table, ref) else None)
    return ref if _row_exists(cs, table, ref) else None


def _row_exists(cs: CardStore, table: str, row_id: str) -> bool:
    with cs._lock:
        return cs.conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (row_id,)).fetchone() is not None


def dispute(ref: str, reason: str, superseded_by: str = "", table: str = "atoms",
            store: CardStore | None = None) -> dict:
    """Mark an atom/card WRONG or STALE (reasoner say-so drives it DOWN to JUDGED_FLOOR; never deletes,
    never redeems). `superseded_by` = the coordinate/id of the atom that now holds the truth (stamped as
    the receipt). Resolves a slug/coordinate to its row id first."""
    cs = store or CardStore()
    rid = _resolve(cs, ref, table)
    if not rid:
        return {"ok": False, "reason": f"no {table} row for {ref!r}"}
    res = cs.dispute(table, rid, reason, superseded_by=superseded_by)
    # CLOSE THE CONTRACT (2026-07-06): the immune's ANTIBODY 4 demands a supersedes
    # edge from the disclaimed atom to its replacement, but this verb only stamped
    # superseded_by into the disclaim note — so every dispute left an orphan_disclaim
    # WARN behind. Write the edge the scan checks for (disclaimed --supersedes--> truth).
    # Runs even on an idempotent re-dispute, so an already-orphaned disclaim is repairable.
    if superseded_by and table == "atoms":
        new_id = _resolve(cs, superseded_by, table)
        if new_id and new_id != rid:
            try:
                cs.link(rid, new_id, "supersedes")
            except Exception:
                pass  # edge is best-effort; the disclaim itself already stands
    return res


def disclaim(ref: str, reason: str = "", table: str = "atoms", store: CardStore | None = None,
             superseded_by: str = "", no_successor: bool = False) -> dict:
    """Re-base a judged-origin (self-rated, counterfeit) row to the JUDGED_FLOOR. Idempotent.

    WRITE-TIME INVARIANT (2026-07-31): a disclaim MUST declare either --superseded-by <ref>
    (the replacement atom) or --no-successor (explicit acknowledgment of the orphan). A bare
    disclaim that would create an orphan_disclaim WARN is REFUSED — the correction must be
    traceable back to the truth that replaces the lie. (See docs/write-time-invariants-audit.md)"""
    cs = store or CardStore()
    rid = _resolve(cs, ref, table)
    if not rid:
        return {"ok": False, "reason": f"no {table} row for {ref!r}"}
    if table == "atoms" and not superseded_by and not no_successor:
        return {"ok": False,
                "reason": "disclaim would create an orphan: pass --superseded-by <ref> "
                          "(the atom that replaces this one) or --no-successor to acknowledge"}
    if no_successor and "[no-successor]" not in reason:
        # Stamp the acknowledgment into the recorded reason (lands in born_from) so the
        # immune scan's ANTIBODY 4 treats this orphan as SETTLED, not suspect. Works on an
        # already-disclaimed atom too (the idempotent branch records the marker).
        reason = (reason + " " if reason else "") + "[no-successor]"
    res = cs.disclaim_judged(table, rid, reason=reason)
    # Write the supersedes edge if a successor is declared (mirrors dispute's contract).
    if superseded_by and table == "atoms" and res.get("ok"):
        new_id = _resolve(cs, superseded_by, table)
        if new_id and new_id != rid:
            try:
                cs.link(rid, new_id, "supersedes")
            except Exception:
                pass
    return res


def redeem(ref: str, witness: str, table: str = "atoms", store: CardStore | None = None) -> dict:
    """Redeem a DISCLAIMED row a real TRACE proves true again. TRACE-ONLY — `witness` is the trace receipt;
    a say-so source is refused by the engine gate."""
    cs = store or CardStore()
    rid = _resolve(cs, ref, table)
    if not rid:
        return {"ok": False, "reason": f"no {table} row for {ref!r}"}
    return cs.redeem_by_trace(table, rid, witness, source="trace")


def inspect(ref: str, store: CardStore | None = None) -> dict:
    """READ-ONLY: an atom's full state — the soul score (atoms table) + the structured EARNED trajectory
    (atom_earned: score/use_count/last_fetch_ts/score_history) + its live edges + disclaim status. This is
    the door that replaces the hand-rolled `_probe.py`."""
    cs = store or CardStore()
    aid = cs.atom_id_for_coordinate(ref) or (ref if _row_exists(cs, "atoms", ref) else None)
    if not aid:
        return {"ok": False, "reason": f"no atom for {ref!r}"}
    with cs._lock:
        soul = cs.conn.execute(
            "SELECT coordinate, score, use_count, born_from FROM atoms WHERE id=?", (aid,)).fetchone()
        earned = cs.conn.execute(
            "SELECT score, use_count, last_fetch_ts, score_history FROM atom_earned WHERE atom_id=?",
            (aid,)).fetchone()
        spine = cs.conn.execute("SELECT slug, claim FROM atom_spine WHERE atom_id=?", (aid,)).fetchone()
    out = {"ok": True, "id": aid,
           "coordinate": soul["coordinate"] if soul else None,
           "slug": spine["slug"] if spine else None,
           "claim": spine["claim"] if spine else None,
           "soul_score": round(soul["score"], 1) if soul else None,
           "soul_uses": soul["use_count"] if soul else None,
           "disclaimed": bool(soul and cs._JUDGED_MARK in (soul["born_from"] or "")
                              and not (soul["born_from"] or "").startswith(cs._REDEEMED_MARK)),
           "edges": cs.edges_of(aid, live_only=True)}
    if earned:
        out["earned_score"] = round(earned["score"], 1)
        out["earned_uses"] = earned["use_count"]
        out["last_fetch_ts"] = earned["last_fetch_ts"]
        try:
            out["earned_history"] = json.loads(earned["score_history"])[-5:]
        except Exception:
            out["earned_history"] = earned["score_history"]
    else:
        out["earned_score"] = None  # never fetched through the door yet
    # CLAIMED_BY (council 2026-07-24: open + home-anchored responsibility) — the reverse index of
    # scopes that adopted this atom via `claims` atlas edges. Only the home scope edits/retires;
    # a non-empty list here IS the blast radius of that edit. Best-effort: no atlas, no list.
    try:
        from echelon_sdk.scopegraph import ScopeGraph
        home, _, slug_c = (out.get("coordinate") or "").partition(":")
        out["claimed_by"] = ScopeGraph().claimants_of(home, slug_c) if home and slug_c else []
    except Exception:
        out["claimed_by"] = []
    return out


def remember(ref: str, store: CardStore | None = None) -> dict:
    """THE FULL-BODY READ, WITNESSED. Closes the last dead-link (owner, 2026-06-19): recall serves a
    ~100-char preview and inspect clips the claim to 100 chars, so to read the WHOLE atom prose I kept
    falling back to `cat memory/*.md` — querying files out-of-band, the bank never witnessing the read.
    `remember` serves the full structured body through the witnessed door (CardStore.remember_fetch,
    depth='body') so the take-up EARNS and the warmth re-forms. This is the read verb the substrate was
    missing: 'recall to find it, remember to take it up in full.' There is no file-read fallback anymore."""
    cs = store or CardStore()
    aid = cs.atom_id_for_coordinate(ref) or (ref if _row_exists(cs, "atoms", ref) else None)
    if not aid:
        return {"ok": False, "reason": f"no atom for {ref!r}"}
    served = cs.remember_fetch(aid, depth="body")  # full body + the witnessed earn
    if not served:
        return {"ok": False, "reason": f"{ref!r} has no compiled spine (ingest/compile it first)"}
    served["ok"] = True
    return served


def _render(action: str, res: dict) -> str:
    if action == "remember":
        if not res.get("ok"):
            return f"remember: {res['reason']}"
        parts = [f"REMEMBER {res.get('slug') or res['id']}  (full body, witnessed — the take-up earned)"]
        claim = res.get("claim") or ""
        if claim:
            parts.append(f"\n{claim}")
        for field in ("directive", "why", "evidence"):
            v = (res.get(field) or "").strip()
            if v:
                parts.append(f"\n--- {field} ---\n{v}")
        return "\n".join(parts)
    if not res.get("ok", True) and "reason" in res and action != "inspect":
        return f"{action}: FAILED — {res['reason']}"
    if action == "inspect":
        if not res.get("ok"):
            return f"inspect: {res['reason']}"
        e = res
        lines = [f"INSPECT {e['slug'] or e['id']}  ({e['coordinate']})",
                 f"  claim       : {(e['claim'] or '')}",
                 f"  soul score  : {e['soul_score']}  (uses {e['soul_uses']})",
                 f"  earned score: {e['earned_score']}  (uses {e.get('earned_uses')}, "
                 f"last_fetch {e.get('last_fetch_ts')})" if e['earned_score'] is not None
                 else "  earned score: — (never fetched through the door)",
                 f"  disclaimed  : {e['disclaimed']}",
                 f"  earned hist : {e.get('earned_history')}",
                 f"  live edges  : {len(e['edges'])}"]
        if e.get("claimed_by"):
            lines.insert(2, f"  claimed by  : {', '.join(e['claimed_by'])}  "
                            f"(home-anchored: only the home scope edits/retires — this list is the blast radius)")
        for ed in e["edges"][:8]:
            lines.append(f"    {ed['dir']:3} -{ed['relation']}- {str(ed['to_id'] if ed['dir']=='out' else ed['from_id'])[:14]}")
        return "\n".join(lines)
    # dispute/disclaim/redeem share the {ok, before, after} shape
    b, a = res.get("before"), res.get("after")
    return f"{action}: ok={res.get('ok')}  {b} -> {a}" + (f"  ({res['reason']})" if res.get("reason") else "")


def _main(argv=None):
    ap = argparse.ArgumentParser(description="The content antibody + introspection: dispute / disclaim / redeem / inspect / remember.")
    ap.add_argument("action", choices=["dispute", "disclaim", "redeem", "inspect", "remember"])
    ap.add_argument("ref", nargs="*", help="atom slug/coordinate or row id (remember: multiple ok; "
                    "dispute/disclaim/redeem/inspect: exactly one)")
    ap.add_argument("--reason", default="", help="(dispute/disclaim) why it's wrong/stale")
    ap.add_argument("--superseded-by", default="", help="(dispute/disclaim) coordinate/id of the atom that now holds the truth")
    ap.add_argument("--no-successor", action="store_true", help="(disclaim) explicit acknowledgment that this atom has no known replacement")
    ap.add_argument("--witness", default="", help="(redeem) the trace receipt proving it true again — REQUIRED")
    ap.add_argument("--table", default="atoms", choices=["atoms", "cards"])
    a = ap.parse_args(argv)
    cs = CardStore()
    refs = a.ref
    if a.action == "remember":
        if not refs:
            ap.error("remember needs at least one atom slug/coordinate")
        for i, ref in enumerate(refs):
            if len(refs) > 1:
                print(f"\n{'─'*60}\nREMEMBER [{i+1}/{len(refs)}]: {ref}\n{'─'*60}")
            res = remember(ref, store=cs)
            print(_render("remember", res))
        return 0
    # dispute / disclaim / redeem / inspect: exactly one ref
    if len(refs) != 1:
        ap.error(f"{a.action} takes exactly one ref (got {len(refs)})")
    ref = refs[0]
    if a.action == "dispute":
        if not a.reason:
            ap.error("dispute needs --reason")
        res = dispute(ref, a.reason, superseded_by=a.superseded_by, table=a.table, store=cs)
    elif a.action == "disclaim":
        res = disclaim(ref, reason=a.reason, table=a.table, store=cs,
                       superseded_by=a.superseded_by, no_successor=a.no_successor)
    elif a.action == "redeem":
        if not a.witness:
            ap.error("redeem is TRACE-ONLY — pass --witness <trace receipt>")
        res = redeem(ref, a.witness, table=a.table, store=cs)
    else:
        res = inspect(ref, store=cs)
    print(_render(a.action, res))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(_main())
