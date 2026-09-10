"""proof-line — compose the /wrap step-7 proof-ledger line from what the bank already recorded.

WHY THIS EXISTS (owner, 2026-08-15): step 7 of the wrap skill requires one line —
`proof: warmth-at-open=<n> spend≈<n> reflex-fires=<n> spurious=<n>` — feeding M3/M2 of
docs/long-horizon-proof.md. Every input for it was ALREADY on disk (reflex_fires.jsonl is
ts+scope tagged; impressions carries what was surfaced and consumed; the prior arc-card
carries the window start). Composing it by hand meant ~4 exploratory queries per wrap and
made the line the easiest part of the bar to silently skip. A line that is expensive to
produce gets dropped exactly when the session is long — which is when the proof needs it.

THE WINDOW: from the previous session arc-card's ts (the same `_prev_session_card` lineage
`wrap` chains to) up to now. Self-maintaining — no new state, no session id to thread
through, and it means "since the last wrap" by construction.

SPURIOUS JUDGMENT: the skill says judge spurious "by whether each fired guard's condition
was actually present". That is a JUDGMENT, not a count, so this verb does NOT fabricate it.
It prints the fires grouped by reflex with their payloads (--fires) so the judgment is cheap
to make, and emits `spurious=?` unless the caller passes --spurious N. Auto-filling that
number with a guess would be a fake measurement in a proof ledger — the one place a
convenient number is worse than a missing one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

FIRELOG = Path(os.path.expanduser("~/.echelon/reflex_fires.jsonl"))


def window_start(scope: str, db_path: str | None = None) -> tuple[int, str]:
    """Start of this wrap's window = the previous session arc-card's ts (and its id).

    Uses the SAME lineage predicate `wrap._prev_session_card` uses, so the proof window and
    the arc-card chain agree by construction. Falls back to 24h ago when the estate has no
    prior session card (a first wrap) — reported honestly by the caller."""
    from .cards import CardStore, DEFAULT_V2_DB
    from .coord_norm import _norm_coord
    from ..registry import SESSION_ARC_CODES

    cs = CardStore(db_path or DEFAULT_V2_DB)
    kind_sql = "kind IN (" + ",".join(str(c) for c in SESSION_ARC_CODES) + ")"
    norm_scope = _norm_coord(scope)
    scope_sql = "refs LIKE ?" if norm_scope else "1=1"
    params = (f'%"{norm_scope}:%',) if norm_scope else ()
    with cs._lock:
        r = cs.conn.execute(
            f"SELECT id, ts FROM cards WHERE (({kind_sql}) OR born_from LIKE 'wrap-session%' "
            "OR born_from LIKE 'relive%' OR born_from LIKE 'session-wrap%' "
            f"OR born_from LIKE 'session_wrap%') AND {scope_sql} "
            "ORDER BY ts DESC LIMIT 1", params).fetchone()
    if r:
        return int(r["ts"]), r["id"]
    return int(time.time()) - 86400, ""


def reflex_fires(scope: str, since: int, until: int | None = None) -> list[dict]:
    """Every reflex fire in the window. Scope-filtered on the `scope:name` the hook logs —
    an estate's proof line counts ITS guards, not the machine's."""
    until = until or int(time.time())
    out: list[dict] = []
    if not FIRELOG.exists():
        return out
    for line in FIRELOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue  # a corrupt line never breaks the ledger read
        ts = int(rec.get("ts", 0))
        if not (since <= ts <= until):
            continue
        if scope and not str(rec.get("reflex", "")).startswith(f"{scope}:"):
            continue
        out.append(rec)
    return out


def warmth_at_open(scope: str, since: int, until: int | None = None) -> float | None:
    """The nerve score on the session's FIRST turn in the window — read from the warmth
    ledger the UserPromptSubmit nerve appends (hooks/echelon_prime.py).

    Why a ledger and not a recomputation: the score is a function of the prompt text and the
    bank state AT THAT MOMENT. Recomputing it at wrap time would produce a different, honest-
    looking number for a different question — a fake measurement in a proof ledger. Returns
    None when the window predates the ledger (an honest gap, never a zero)."""
    path = Path(os.path.expanduser("~/.echelon/warmth_turns.jsonl"))
    if not path.exists():
        return None
    until = until or int(time.time())
    best: tuple[int, float] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts = int(rec.get("ts", 0))
        if rec.get("scope") != scope or not (since <= ts <= until):
            continue
        if best is None or ts < best[0]:
            best = (ts, float(rec.get("score", 0.0)))
    return best[1] if best else None


def compose(scope: str, since: int | None = None, spend: str = "", spurious: str = "",
            db_path: str | None = None) -> dict:
    start, prev_card = (since, "") if since else window_start(scope, db_path)
    if since:
        _, prev_card = window_start(scope, db_path)
    now = int(time.time())
    fires = reflex_fires(scope, start, now)
    by_reflex: dict[str, int] = {}
    for f in fires:
        by_reflex[f.get("reflex", "?")] = by_reflex.get(f.get("reflex", "?"), 0) + 1
    warm = warmth_at_open(scope, start, now)
    line = (f"proof: warmth-at-open={warm if warm is not None else 'n/a'} "
            f"spend≈{spend or 'n/a'} "
            f"reflex-fires={len(fires)} spurious={spurious or '?'}")
    return {"scope": scope, "window_start": start, "window_end": now, "prev_card": prev_card,
            "fires": fires, "by_reflex": by_reflex, "warmth": warm, "line": line}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon proof-line",
        description="compose the /wrap proof-ledger line from the bank's own records")
    ap.add_argument("--scope", required=True)
    ap.add_argument("--since", type=int, default=0,
                    help="unix ts window start (default: the previous session arc-card's ts)")
    ap.add_argument("--spend", default="", help="approx token spend if known (else n/a)")
    ap.add_argument("--spurious", default="",
                    help="count of spurious fires — YOUR judgment; omitted prints '?' rather than a guess")
    ap.add_argument("--fires", action="store_true",
                    help="list the in-window fires with payloads, so spurious can be judged cheaply")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    res = compose(a.scope, a.since or None, a.spend, a.spurious)
    if a.json:
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 0

    hrs = round((res["window_end"] - res["window_start"]) / 3600, 1)
    print(f"window: {hrs}h since "
          + (f"card {res['prev_card']}" if res["prev_card"] else "24h-ago (no prior session card)"))
    if res["by_reflex"]:
        print("fires by reflex:")
        for name, n in sorted(res["by_reflex"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:3}x  {name}")
    else:
        print("fires by reflex: none in window")
    if a.fires:
        print("\nfire detail (judge spurious: was the guard's condition actually present?):")
        for f in res["fires"]:
            when = time.strftime("%m-%d %H:%M", time.localtime(f.get("ts", 0)))
            print(f"  [{when}] {f.get('reflex')}  tool={f.get('tool')}")
            print(f"           {str(f.get('payload', ''))[:150]}")
    if not a.spurious:
        print("\nNOTE: spurious=? — judge it from the fire detail above and re-run with "
              "--spurious N (this verb will not guess a number into a proof ledger).")
    print("\n" + res["line"])
    return 0


main = _main

if __name__ == "__main__":
    sys.exit(_main())
