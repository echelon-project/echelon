"""wrap-review — the explicit feedback layer of wrap: approve or dispute the atoms this
session actually LEANED ON. The engine SERVES the leaned-on set (atoms with a witnessed
take-up via remember_fetch in the session window), and the caller approves or disputes
each; silence = no-op.

APPROVE -> reinforce through the existing earn/trace path (one EARN_DELTA at source
"wrap-approve"). DISPUTE -> the existing disclaim path (cards.py dispute()).
IDEMPOTENCE: a wrap_reviews row with PK (atom_id, session_window_start) guards re-apply.
M5 FEED: each verdict appends to ~/.echelon/wrap_review_ledger.jsonl for the long-
horizon proof consumption.

LEANED-ON SET = atoms with atom_earned.last_fetch_ts >= session_window_start.
EXPLICITLY NOT: impressions rows (warm recall hits with no remember fetch).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .cards import CardStore, DEFAULT_V2_DB
from .earn_law import EARN_DELTA

# Default session window: 12 hours (43200 seconds) — the spec's fallback
# when no explicit SessionStart marker exists in the DB.
DEFAULT_SESSION_WINDOW_SECS = 43200


def default_session_window_start(now: int | None = None) -> int:
    """The default window start, BUCKETED to the window size so it is STABLE
    across invocations. The gate catch (2026-07-31): `now - WINDOW` recomputed
    per call gives every invocation a fresh session_window_start, which defeats
    the (atom_id, session_window_start) idempotence PK — two `--approve` runs
    seconds apart would stack the earn. A session straddling a bucket boundary
    gets two windows (documented, acceptable); a caller wanting exact control
    passes --since explicitly."""
    now = int(now if now is not None else time.time())
    return (now // DEFAULT_SESSION_WINDOW_SECS) * DEFAULT_SESSION_WINDOW_SECS

# M5 ledger path — best-effort JSONL append
_LEDGER_PATH = Path.home() / ".echelon" / "wrap_review_ledger.jsonl"


def _resolve_atom(store: CardStore, slug_or_id: str, scope: str) -> str | None:
    """Resolve a slug, coordinate, or atom ID to an atom_id.
    Tries: direct atom ID -> exact slug -> scope:slug -> echelon:slug."""
    # Direct atom ID
    if store.get_atom(slug_or_id):
        return slug_or_id
    # Coordinate / slug resolution
    return (store.atom_id_for_coordinate(slug_or_id)
            or store.atom_id_for_coordinate(f"{scope}:{slug_or_id}")
            or store.atom_id_for_coordinate(f"echelon:{slug_or_id}"))


def _parse_slugs(s: str) -> list[str]:
    """Parse comma-separated slug list, stripping whitespace."""
    return [x.strip() for x in s.split(",") if x.strip()]


def list_leaned_on(store: CardStore, scope: str, since_ts: int) -> list[dict]:
    """Return atoms with a witnessed take-up (remember_fetch) in the session window.

    Each row: atom_id, coordinate, content, score, use_count, last_fetch_ts, slug, scope.
    Ordered by last_fetch_ts DESC (most recently leaned-on first).

    EXPLICITLY NOT the impressions table — views are implicit, earns are explicit.
    """
    with store._lock:
        rows = store.conn.execute(
            """SELECT a.id AS atom_id, a.coordinate, a.content,
                      e.score, e.use_count, e.last_fetch_ts,
                      s.slug, a.scope
               FROM atom_earned e
               JOIN atoms a ON a.id = e.atom_id
               LEFT JOIN atom_spine s ON s.atom_id = e.atom_id
               WHERE e.last_fetch_ts >= ? AND a.scope = ?
               ORDER BY e.last_fetch_ts DESC""",
            (since_ts, scope)
        ).fetchall()
    return [dict(r) for r in rows]


def _append_ledger(atom_id: str, verdict: str, reason: str,
                   session_window_start: int, ts: int) -> None:
    """Append a verdict line to the M5 wrap-review ledger (JSONL, best-effort).

    The ledger feeds M5 in docs/long-horizon-proof.md: which atoms did a session
    actually lean on, confirmed by the human loop. Shape: one JSON object per line,
    with atom_id, verdict, reason, session_window_start, ts.
    """
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "atom_id": atom_id,
            "verdict": verdict,
            "reason": reason,
            "session_window_start": session_window_start,
            "ts": ts,
        }
        with open(_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # best-effort; ledger append must never break the review


def apply_verdict(store: CardStore, atom_id: str, verdict: str,
                  session_window_start: int, reason: str = "") -> dict:
    """Apply approve or dispute to one atom. Returns a receipt dict.

    APPROVE: calls _mutate_score(op="earn", delta=EARN_DELTA, source="wrap-approve")
    on the atoms table — the same earn path as the existing reinforce_card, with a
    distinct source tag so the fetch-earn and approve-earn are separate entries.

    DISPUTE: calls store.dispute() — the existing disclaim path (cards.py:1518).

    IDEMPOTENCE: insert into wrap_reviews with PK (atom_id, session_window_start);
    re-apply with the same slug in the same session window is a no-op.

    M5 FEED: each verdict appends to the wrap-review ledger for the long-horizon
    proof (M5 in docs/long-horizon-proof.md).
    """
    # --- idempotence guard ---
    with store._lock:
        existing = store.conn.execute(
            "SELECT verdict FROM wrap_reviews WHERE atom_id=? AND session_window_start=?",
            (atom_id, session_window_start)
        ).fetchone()

    if existing:
        return {"ok": True, "noop": True, "verdict": existing["verdict"],
                "reason": f"already reviewed in this session window"}

    # --- apply ---
    if verdict == "approve":
        result = store._mutate_score("atoms", atom_id, op="earn",
                                     delta=EARN_DELTA, source="wrap-approve")
    elif verdict == "dispute":
        result = store.dispute("atoms", atom_id, reason=reason)
    else:
        return {"ok": False, "reason": f"unknown verdict: {verdict!r}"}

    if not result.get("ok"):
        return result

    # --- record for idempotence ---
    now = int(time.time())
    with store._lock:
        store.conn.execute(
            "INSERT INTO wrap_reviews (atom_id, session_window_start, verdict, reason, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (atom_id, session_window_start, verdict, reason, now)
        )
        store.conn.commit()

    # --- M5 feed: append to ledger ---
    _append_ledger(atom_id, verdict, reason, session_window_start, now)

    return {"ok": True, "verdict": verdict,
            "before": result.get("before"), "after": result.get("after"),
            "delta": result.get("delta")}


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description="wrap-review: list, approve, or dispute atoms this session leaned on")
    ap.add_argument("--scope", default="echelon",
                    help="scope to review (default: echelon)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    ap.add_argument("--since", type=int, default=None,
                    help=f"session window start as unix timestamp "
                         f"(default: {DEFAULT_SESSION_WINDOW_SECS}s ago)")
    ap.add_argument("--approve", type=str, default="",
                    help="comma-separated slugs, coordinates, or atom IDs to approve")
    ap.add_argument("--dispute", type=str, default="",
                    help="comma-separated slugs, coordinates, or atom IDs to dispute")
    ap.add_argument("--reason", type=str, default="",
                    help="reason for dispute (shared across all disputed slugs)")

    a = ap.parse_args(argv)

    store = CardStore()
    since = a.since if a.since is not None else default_session_window_start()
    session_window_start = since

    if a.approve or a.dispute:
        # ── apply verdicts ──
        receipts = []
        for slug in _parse_slugs(a.approve):
            atom_id = _resolve_atom(store, slug, a.scope)
            if not atom_id:
                receipts.append({"slug": slug, "ok": False, "reason": "not found"})
                continue
            r = apply_verdict(store, atom_id, "approve", session_window_start)
            receipts.append({"slug": slug, "atom_id": atom_id, **r})

        for slug in _parse_slugs(a.dispute):
            atom_id = _resolve_atom(store, slug, a.scope)
            if not atom_id:
                receipts.append({"slug": slug, "ok": False, "reason": "not found"})
                continue
            r = apply_verdict(store, atom_id, "dispute", session_window_start,
                              reason=a.reason)
            receipts.append({"slug": slug, "atom_id": atom_id, **r})

        if a.json:
            print(json.dumps(receipts, indent=2))
        else:
            for r in receipts:
                status = "OK" if r.get("ok") else "FAIL"
                noop = " (noop)" if r.get("noop") else ""
                before = r.get("before")
                after = r.get("after")
                score_line = ""
                if before is not None and after is not None:
                    score_line = f" before={before:.1f} after={after:.1f}"
                print(f"  [{status}] {r['slug']}: {r.get('verdict', 'ERROR')}{noop}{score_line}")
    else:
        # ── list leaned-on set ──
        atoms = list_leaned_on(store, a.scope, since)
        if a.json:
            print(json.dumps(atoms, indent=2))
        else:
            since_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(since))
            print(f"Leaned-on atoms (scope={a.scope}, since={since} = {since_str}):")
            if not atoms:
                print("  (none)")
            else:
                print(f"  {'SLUG':<40} {'COORD':<35} {'SCORE':>8}  {'FETCHED'}")
                print(f"  {'-'*40} {'-'*35} {'-'*8}  {'-'*19}")
                for row in atoms:
                    slug = (row.get("slug") or "")[:38]
                    coord = (row.get("coordinate") or "")[:33]
                    score = f"{row.get('score', 0):.1f}"
                    fetched = time.strftime('%Y-%m-%d %H:%M',
                                            time.localtime(row["last_fetch_ts"]))
                    print(f"  {slug:<40} {coord:<35} {score:>8}  {fetched}")
                print(f"\n  {len(atoms)} atoms. Approve: --approve slug1,slug2  "
                      f"Dispute: --dispute slug3 --reason '...'")
