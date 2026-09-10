"""status — bank overview at a glance (atom counts, scopes, earned weight, health).

    python -X utf8 -m echelon_engine status [--scope echelon] [--json]
"""
from __future__ import annotations

import argparse
import json
import time


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="echelon status",
                                 description="Bank overview: atom counts, scopes, earned weight, immune health.")
    ap.add_argument("--scope", default=None, help="scope to inspect (default: all)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    from .cards import CardStore
    cs = CardStore()

    # Scope survey
    rows = cs.conn.execute(
        "SELECT scope, COUNT(*) n FROM atoms WHERE scope!='' GROUP BY scope").fetchall()
    weights = {r["scope"]: r["w"] for r in cs.conn.execute(
        "SELECT a.scope scope, SUM(e.score) w FROM atoms a JOIN atom_earned e ON a.id=e.atom_id "
        "WHERE a.scope!='' GROUP BY a.scope").fetchall()}
    scopes = sorted(({"scope": r["scope"], "atoms": r["n"],
                       "weight": round(float(weights.get(r["scope"]) or 0.0), 1)} for r in rows),
                    key=lambda s: s["weight"], reverse=True)

    total_atoms = sum(s["atoms"] for s in scopes)
    total_weight = sum(s["weight"] for s in scopes)

    # UNSCOPED CENSUS (2026-08-18): the scope survey above filters `scope!=''`, so atoms with an
    # EMPTY scope were excluded from the headline total — the bank read 8462 while holding 8637.
    # Those 175 are the pre-scope era (namespace lives INSIDE the coordinate: `persona:dev`,
    # `memory:epsilon-co:...`); a 2026-07-08 mining pass disclaimed 12 as orphans and never finished
    # the other 163, which still hold real earned weight no scope-grouped door can serve.
    # A filtered total rendered as THE total is how that stayed invisible for six weeks, so the
    # count is surfaced here rather than silently dropped. Rescue-vs-retire is a separate decision;
    # this only stops the number lying.
    unscoped_row = cs.conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(e.score), 0) w FROM atoms a "
        "LEFT JOIN atom_earned e ON a.id=e.atom_id "
        "WHERE a.scope='' OR a.scope IS NULL").fetchone()
    unscoped_atoms = int(unscoped_row["n"] or 0)
    unscoped_weight = round(float(unscoped_row["w"] or 0.0), 1)

    # B2 — anti-atom census (negative learning): count witnessed-FAILURE atoms per scope so the
    # "rejects are knowledge" organ is visible in the overview. kind='anti' is set at ingest.
    anti_by_scope = {r["scope"]: r["n"] for r in cs.conn.execute(
        "SELECT scope, COUNT(*) n FROM atoms WHERE kind='anti' AND scope!='' GROUP BY scope").fetchall()}
    total_anti = sum(anti_by_scope.values())

    # DORMANT CENSUS (2026-07-31): atoms whose effective_score fell below threshold.
    # Dormant atoms are hidden from default recall but NEVER deleted.
    dormant_info = cs.count_dormant(scope=a.scope)
    total_dormant = dormant_info["count"]

    # Immune health
    fail_list: list = []
    warn_list: list = []   # bound BEFORE the try: the human-output path below reads warn_list
    try:                   # unconditionally, so a scan failure must degrade to "unknown", not NameError.
        from .immune import scan as immune_scan
        scan_result = immune_scan(scope=a.scope or "echelon")
        fail_list = scan_result.get("fail", [])
        warn_list = scan_result.get("warn", [])
        fails = len(fail_list) if isinstance(fail_list, list) else (fail_list or 0)
        warns = len(warn_list) if isinstance(warn_list, list) else (warn_list or 0)
    except Exception:
        fails, warns = -1, -1

    # Last activity
    last_earn = cs.conn.execute(
        "SELECT MAX(last_fetch_ts) FROM atom_earned").fetchone()[0] or 0
    last_ingest = cs.conn.execute(
        "SELECT MAX(ts) FROM atoms").fetchone()[0] or 0

    if a.json:
        out = {
            "total_atoms": total_atoms,
            "total_earned_weight": total_weight,
            "unscoped_atoms": unscoped_atoms,
            "unscoped_earned_weight": unscoped_weight,
            "bank_atoms": total_atoms + unscoped_atoms,
            "scopes": len(scopes),
            "scope_detail": {s["scope"]: {"atoms": s["atoms"], "earned_weight": s["weight"]}
                             for s in scopes},
            "immune": {"fails": fails, "warns": warns},
            "anti_atoms": total_anti,
            "anti_by_scope": anti_by_scope,
            "dormant_atoms": total_dormant,
            "last_earn_ts": last_earn,
            "last_ingest_ts": last_ingest,
        }
        print(json.dumps(out, indent=2))
        return 0

    # Human output
    health = "green" if fails == 0 and warns == 0 else ("amber" if fails == 0 else "red")
    if fails < 0:
        health = "unknown"

    print(f"ECHELON BANK  ({total_atoms} atoms, {round(total_weight, 1)} earned weight, "
          f"{len(scopes)} scopes)")
    if unscoped_atoms:
        print(f"  unscoped:   {unscoped_atoms} atom(s) held but NOT in any scope "
              f"({unscoped_weight} earned weight, unreachable by scope-grouped recall) "
              f"— bank holds {total_atoms + unscoped_atoms}")
    print(f"  immune: {health}  (fails={fails}, warns={warns})")
    if isinstance(warn_list, list) and warn_list:
        for w in warn_list:
            aid = w.get("atom_id", "?")
            rule = w.get("rule", "?")
            detail = w.get("detail", "")
            print(f"    WARN [{rule}] {aid}  {detail}")
    if fails != 0 or warns != 0:
        # the detail verb is `scan` (read-only) — `echelon immune` does not exist; the
        # cold-reader door audit 2026-07-06 followed this pointer and hit the wall.
        print(f"  — detail: echelon scan   (heal tombstones what scan finds)")
    if total_anti:
        print(f"  anti-atoms: {total_anti}  (witnessed failures — do-not-re-tread; ⚠ ANTI in recall)")
    if total_dormant:
        print(f"  dormant:    {total_dormant} atom(s) hidden from default recall "
              f"(use --include-dormant to surface)")
    if last_earn:
        print(f"  last earn:   {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_earn))}")
    if last_ingest:
        print(f"  last ingest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_ingest))}")
    print()

    if a.scope:
        scopes = [s for s in scopes if s["scope"] == a.scope]
        if not scopes:
            print(f"(scope '{a.scope}' not found)")
            return 0

    for s in scopes[:20]:
        bar = "█" * min(int(s["weight"] / 50), 40) if s["weight"] > 0 else ""
        print(f"  {s['scope']:<28} atoms={s['atoms']:<5} earned={s['weight']:<8} {bar}")
    if len(scopes) > 20:
        print(f"  ... and {len(scopes) - 20} more scopes")

    # B4 — one-line pointer to the earning-loop alarm (a metric that CAN silently zero needs an alarm,
    # not just a display; ruflo-lesson-recorded-everything-distilled-nothing).
    print("\n  selftest: run 'echelon selftest' to prove the earning loop")

    return 0
