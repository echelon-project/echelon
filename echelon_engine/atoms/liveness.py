"""liveness — sweep the oldest-unvalidated atoms and check their ANCHORS still exist (wrap 7c).

WHY (Henry canon §V GOTCHAS law, adopted 2026-08-12; mechanised 2026-08-15): atoms rot. The
file:line an atom cites gets renamed, the verb it describes changes flags, the OPEN item it
tracks closes. Step 7c of /wrap asks for a sweep of the ~10 oldest-unvalidated atoms each
wrap — but done by hand it is grep-per-atom, so it is the step most likely to be skipped
under context pressure, which is exactly when the estate is rotting fastest.

WHAT THIS VERB DOES AND DOES NOT DO — the line matters:
  DOES (mechanical, free, no judgment): pick the oldest-unvalidated anchor-bearing atoms;
    extract their ANCHORS (file paths, `echelon <verb>` references, --flags); check cheaply
    whether each anchor still exists on disk / in the CLI surface; report per-atom.
  DOES NOT: decide the verdict. The skill's three verdicts (re-stamp / supersede / dispute)
    are judgments about MEANING, and a dead file path does not by itself mean the lesson is
    wrong — it may have moved. So this prints EVIDENCE and the exact command for each verdict,
    and changes nothing. Auto-disputing on a failed grep would silently destroy earned weight
    on a rename, which is worse than the rot it fixes.

Also reports the two alarms step 7c names: STRAY SIBLINGS (one coordinate carrying duplicate
rows — the 5x open_next_session_swarm_hardening disease) and HOARDING (>5 live OPEN-* atoms).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Anchors worth checking: a path-shaped token, an `echelon <verb>` reference, a python module.
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][\w.\-]+){2,}\.\w{1,5}|[\w\-]+/[\w.\-/]+\.\w{1,5}")
_VERB_RE = re.compile(r"echelon\s+([a-z][a-z\-]{2,})\b")
_MOD_RE = re.compile(r"echelon_engine[\w.]*\.(\w+)")

# FALSE-POSITIVE GUARDS (found by running this sweep on the real bank, 2026-08-15).
# A liveness sweep that cries wolf gets ignored, and an ignored sweep is worse than none —
# it converts "the anchors were checked" into a claim nobody reads. Two artifacts were real:
#   - prose after the word 'echelon' parsed as a verb ("echelon are the ...") -> _VERB_STOP
#   - an extension ALTERNATION inside prose ("pdf/.bak/.db") parsed as a path -> _looks_like_path
_VERB_STOP = {"are", "is", "was", "has", "had", "and", "the", "but", "for", "not", "can",
              "did", "does", "you", "its", "it's", "this", "that", "them", "they", "with",
              "from", "into", "onto", "over", "when", "then", "than", "also", "only", "own",
              "bank", "atom", "atoms", "scope", "session", "memory", "estate", "agent",
              "way", "ways", "must", "will", "would", "should", "could", "may", "might",
              "itself", "one", "two", "each", "every", "any", "all", "some", "such",
              "keeps", "makes", "needs", "uses", "runs", "gets", "goes", "lives", "works",
              "how", "why", "what", "who", "where", "which", "now", "here", "there"}


def _looks_like_path(tok: str) -> bool:
    """Filter prose that merely LOOKS path-shaped. A real anchor has a segment before the
    final extension that is not itself a bare extension list ('pdf/.bak/.db' is prose)."""
    t = tok.replace("\\", "/")
    segs = [s for s in t.split("/") if s]
    if len(segs) < 2:
        return False
    # every segment after the first starting with '.' => an extension alternation, not a path
    if all(s.startswith(".") for s in segs[1:]):
        return False
    return bool(re.search(r"[A-Za-z0-9_\-]\.[A-Za-z0-9]{1,5}$", t))


def _atom_rows(scope: str, limit: int, db_path: str | None = None) -> list[dict]:
    from .cards import CardStore, DEFAULT_V2_DB
    cs = CardStore(db_path or DEFAULT_V2_DB)
    with cs._lock:
        rows = cs.conn.execute(
            "SELECT a.id, a.coordinate, a.ts, a.content, e.last_fetch_ts, e.use_count "
            "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id "
            "WHERE a.scope = ? "
            "ORDER BY COALESCE(e.last_fetch_ts, 0) ASC, a.ts ASC", (scope,)).fetchall()
    return [dict(r) for r in rows]


def _anchors(content: str) -> dict[str, list[str]]:
    """Anchor-bearing tokens in an atom body: paths, engine verbs, engine modules."""
    paths = {p for p in _PATH_RE.findall(content or "") if _looks_like_path(p)}
    verbs = {v for v in _VERB_RE.findall(content or "") if v not in _VERB_STOP}
    mods = {m for m in _MOD_RE.findall(content or "")}
    return {"paths": sorted(paths)[:6], "verbs": sorted(verbs)[:6], "modules": sorted(mods)[:6]}


def _known_verbs() -> set[str]:
    """The engine's live CLI surface — so a cited verb can be checked without spawning it."""
    try:
        from ..__main__ import _route  # type: ignore
        return set(_route().keys())
    except Exception:
        return set()


def _check_path(tok: str, roots: list[Path]) -> bool:
    """Does this cited path still exist? Absolute first, then relative to each root.
    A bare basename match counts — an atom citing `warmth.py` is still anchored if the file
    moved within the tree; that is a MOVE (re-stamp), not a death (dispute)."""
    t = tok.replace("\\", "/")
    if Path(t).exists():
        return True
    for r in roots:
        if (r / t).exists():
            return True
        base = t.rsplit("/", 1)[-1]
        try:
            if any(r.rglob(base)):
                return True
        except Exception:
            continue
    return False


def sweep(scope: str, limit: int = 10, roots: list[str] | None = None,
          db_path: str | None = None) -> dict:
    rows = _atom_rows(scope, limit, db_path)

    # ── STRAY-SIBLING ALARM: one coordinate carrying duplicate rows ──────────────────────
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["coordinate"]] = counts.get(r["coordinate"], 0) + 1
    strays = sorted([(c, n) for c, n in counts.items() if n > 1], key=lambda kv: -kv[1])

    # ── HOARDING ALARM: live OPEN-* atoms ────────────────────────────────────────────────
    opens = sorted({r["coordinate"] for r in rows
                    if re.search(r"(^|:)open[_\-]", r["coordinate"] or "", re.I)})

    root_paths = [Path(p) for p in (roots or []) if Path(p).is_dir()]
    verbs = _known_verbs()

    # Anchor-bearing atoms first (an atom naming a file/verb/flag), oldest-unvalidated order.
    seen: set[str] = set()
    checked: list[dict] = []
    for r in rows:
        if r["coordinate"] in seen:
            continue
        anc = _anchors(r["content"] or "")
        if not (anc["paths"] or anc["verbs"] or anc["modules"]):
            continue
        seen.add(r["coordinate"])
        dead_paths = [p for p in anc["paths"] if not _check_path(p, root_paths)] if root_paths else []
        # A stop-list only catches prose words someone thought of ('are', 'way', ...). The
        # STRUCTURAL rule: report a cited verb as dead only when it LOOKS like a verb —
        # hyphenated (`proof-line`) or a compiled-in name that has since been removed. Bare
        # prose after the word "echelon" is not a citation, so it is never rot. This keeps the
        # sweep credible, which is the only property that makes it get read.
        dead_verbs = [v for v in anc["verbs"]
                      if verbs and v not in verbs and "-" in v]
        checked.append({
            "slug": (r["coordinate"] or "").split(":", 1)[-1],
            "coordinate": r["coordinate"],
            "age_days": round((time.time() - int(r["ts"] or 0)) / 86400, 1),
            "last_fetch": int(r["last_fetch_ts"] or 0),
            "rechecked": "rechecked" in (r["content"] or ""),
            "anchors": anc,
            "dead_paths": dead_paths,
            "dead_verbs": dead_verbs,
            "status": ("ANCHOR-GONE" if (dead_paths or dead_verbs) else "anchors-live"),
        })
        if len(checked) >= limit:
            break

    return {"scope": scope, "checked": checked, "strays": strays, "opens": opens,
            "scope_drift": scope_drift(db_path),
            "roots": [str(p) for p in root_paths], "verbs_known": len(verbs)}


def scope_drift(db_path: str | None = None) -> list[dict]:
    """Canonicals whose scope disagrees with the copies they subsume — FILING DRIFT.

    Owner, 2026-08-18, on `prod_db_connection`: "maybe it was a day when i do wk job at flux?
    and the cross repo still act that it was flux atom? so we should migrate the atom scope
    owner?" — exactly right, and it is the same class of rot as a dead anchor, so it belongs
    in the same daily sweep.

    HOW IT HAPPENS. Hygiene picks the canonical by earned WEIGHT, not by domain. If a lesson
    about estate A was filed while you were standing in estate B, and the B copy happens to
    be the heaviest, the merged atom lives in B forever. `prod_db_connection` is a WK fact
    (MySQL `epsilon-co` on the WK prod box) whose lineage runs epsilon-co -> echelon ->
    epsilon-co-sop -> ux-cartridge -> flux, and it ended up canonical in `flux` because
    that is where the heaviest copy was on 06-21.

    IT ONLY REPORTS. Migration is real surgery: `atoms` has no rename-scope verb, and the
    canonical here carries 4 merge receipts plus 20 inbound refs, so rewriting scope in place
    would leave every one of them pointing at a coordinate that no longer exists — the
    dangling-successor defect. The fix is to re-canonicalise through hygiene, and that is a
    decision, not a cron job."""
    from .cards import CardStore, DEFAULT_V2_DB
    try:
        cs = CardStore(db_path or DEFAULT_V2_DB)
        with cs._lock:
            rows = cs.conn.execute(
                "SELECT c.scope AS canon_scope, c.coordinate AS canon_coord, "
                "       d.scope AS dup_scope "
                "FROM atom_links l "
                "JOIN atoms c ON c.id = l.from_id "
                "JOIN atoms d ON d.id = l.to_id "
                "WHERE l.relation='subsumes' AND l.superseded_on=0").fetchall()
    except Exception:
        return []

    by_canon: dict[str, dict] = {}
    for r in rows:
        e = by_canon.setdefault(r["canon_coord"],
                                {"coordinate": r["canon_coord"],
                                 "scope": r["canon_scope"], "dup_scopes": {}})
        s = r["dup_scope"] or ""
        e["dup_scopes"][s] = e["dup_scopes"].get(s, 0) + 1

    out = []
    for e in by_canon.values():
        others = {s: n for s, n in e["dup_scopes"].items() if s and s != e["scope"]}
        if not others:
            continue
        # The MAJORITY scope among the subsumed copies is the domain's own vote. Flag only
        # when the canonical is the ODD ONE OUT — a canonical that already agrees with its
        # copies is correctly filed, however many scopes it merged.
        top, n = max(others.items(), key=lambda kv: kv[1])
        if n >= 2 and e["scope"] not in others:
            # COUNT IS A HINT, NOT A VERDICT. The majority scope is where the most filing
            # copies landed, which is not the same as the domain that OWNS the fact — a
            # lesson ported into the substrate scope can outnumber the estate it is about.
            # So the candidates travel with the count and the caller rules. Naming one
            # winner here would launder a tally into an answer.
            out.append({"coordinate": e["coordinate"], "canonical_scope": e["scope"],
                        "candidates": sorted(others.items(), key=lambda kv: -kv[1]),
                        "top_by_count": top, "dup_scopes": e["dup_scopes"]})
    return sorted(out, key=lambda d: d["coordinate"])


def _render(res: dict) -> str:
    out = [f"=== ATOM LIVENESS SWEEP — scope '{res['scope']}' ==="]
    if not res["roots"]:
        out.append("  (no --root given: path anchors NOT checked — pass --root to verify them)")
    n_gone = sum(1 for c in res["checked"] if c["status"] == "ANCHOR-GONE")
    for c in res["checked"]:
        mark = "!!" if c["status"] == "ANCHOR-GONE" else "ok"
        out.append(f"  [{mark}] {c['slug']}  ({c['age_days']}d old"
                   + (", re-stamped" if c["rechecked"] else ", never validated") + ")")
        if c["dead_paths"]:
            out.append(f"        dead path(s): {', '.join(c['dead_paths'][:3])}")
        if c["dead_verbs"]:
            out.append(f"        unknown verb(s): {', '.join(c['dead_verbs'][:3])}")
    out.append("")
    out.append(f"  atoms: {len(res['checked'])} checked · {n_gone} with a dead anchor")
    if res["strays"]:
        out.append(f"  ⚠ STRAY SIBLINGS ({len(res['strays'])} coordinate(s) carrying duplicate rows):")
        for coord, n in res["strays"][:5]:
            out.append(f"      {n}x  {coord}")
        out.append("      fold them: python -X utf8 -m echelon_engine hygiene dedup --apply")
    if len(res["opens"]) > 5:
        out.append(f"  ⚠ HOARDING: {len(res['opens'])} live OPEN-* atoms in this scope "
                   f"— the estate is hoarding open threads:")
        for o in res["opens"][:8]:
            out.append(f"      - {o}")
    if n_gone:
        out.append("")
        out.append("  VERDICT IS YOURS (this verb changes nothing — a dead path may be a MOVE):")
        out.append("    still true, moved  -> add '· rechecked <date>' to the body, re-ingest")
        out.append("    superseded         -> add the `supersedes` edge to its successor")
        out.append("    flat wrong         -> python -X utf8 -m echelon_engine dispute <slug> --reason \"...\"")
    return "\n".join(out)


# ── the QUEUE — liveness is estate hygiene, not a wrap gate ───────────────────
# Owner, 2026-08-18: "liveness check won't affect a wrap right. then just wrote to a global
# ./echelon liveness queue, it will run daily with db backup."
#
# The step was in the wrap pipeline because atoms rot, not because a wrap DEPENDS on it: no
# claim in the receipt is false if the sweep never ran. It was the slowest step in the ritual
# and it gated nothing — so it moves off the critical path. `--enqueue` costs one file write;
# the daily `backup --push` task drains the queue and leaves a report the next session reads.
_QUEUE = Path.home() / ".echelon" / "liveness-queue.json"
_REPORT = Path.home() / ".echelon" / "liveness-report.json"


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def enqueue(scope: str, roots: list[str], sweep_n: int = 10) -> dict:
    """Record that this scope wants a sweep. Idempotent per scope — the queue is a SET of
    scopes to check, not a log of requests, so wrapping five times a day queues one sweep."""
    q = _load(_QUEUE, {})
    q[scope] = {"scope": scope, "roots": sorted(set(roots)), "sweep": sweep_n,
                "queued_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _QUEUE.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE.write_text(json.dumps(q, indent=1, ensure_ascii=False), encoding="utf-8")
    return q[scope]


def drain(limit_per_scope: int = 0) -> dict:
    """Run every queued sweep and write the report. Called by the daily backup task.

    NEVER MUTATES AN ATOM. The skill's own warning holds: a dead path is usually a MOVE, and
    auto-disputing on a failed grep destroys earned weight on a rename. So the daemon does the
    mechanical half and the report carries the judgment half to the next session."""
    q = _load(_QUEUE, {})
    if not q:
        return {"scopes": 0, "checked": 0, "anchor_gone": 0}
    out = {"ran_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "scopes": {}, "needs_verdict": [], "scope_drift": []}
    for scope, spec in sorted(q.items()):
        n = limit_per_scope or spec.get("sweep", 10)
        try:
            res = sweep(scope, n, spec.get("roots", []))
        except Exception as e:                      # a bad scope must not kill the backup job
            out["scopes"][scope] = {"error": f"{type(e).__name__}: {e}"}
            continue
        # sweep() returns "checked" (not "atoms") and "dead_paths"/"dead_verbs" (not
        # "dead_anchors"). Reading the wrong key returned 0-checked on a bank of 1,400
        # atoms — a clean-looking report that had examined nothing, which is the exact
        # silent-zero shape this session already found twice (the lens parser, the veto
        # sweep). Assert the key exists rather than .get()-ing a comfortable default.
        checked = res.get("checked")
        if checked is None:
            out["scopes"][scope] = {"error": f"sweep() returned no 'checked' key "
                                             f"(got {sorted(res)}) — schema drift"}
            continue
        gone = [a for a in checked if a.get("status") == "ANCHOR-GONE"]
        drift = res.get("scope_drift") or []
        out["scopes"][scope] = {"checked": len(checked), "anchor_gone": len(gone),
                                "scope_drift": len(drift)}
        for a in gone:
            dead = list(a.get("dead_paths") or []) + list(a.get("dead_verbs") or [])
            out["needs_verdict"].append({"scope": scope, "slug": a.get("slug"), "dead": dead})
        # Scope drift is bank-wide, not per-scope — dedupe so N queued scopes report it once.
        for d in drift:
            if not any(x.get("coordinate") == d["coordinate"] for x in out["scope_drift"]):
                out["scope_drift"].append(d)
    _REPORT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    _QUEUE.write_text("{}", encoding="utf-8")       # drained
    return out


def pending_report() -> dict:
    """What the last daily sweep found and nobody has ruled on yet."""
    return _load(_REPORT, {})


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon liveness",
        description="sweep the oldest-unvalidated atoms; check their anchors still exist. "
                    "Queued by /wrap, run daily by the backup task — it reports, never mutates.")
    ap.add_argument("--scope", default="")
    ap.add_argument("--sweep", type=int, default=10, help="how many atoms to check (default 10)")
    ap.add_argument("--root", action="append", default=[],
                    help="repo root to resolve path anchors against (repeatable)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--enqueue", action="store_true",
                    help="queue this scope for the daily sweep and exit (what /wrap calls)")
    ap.add_argument("--drain", action="store_true",
                    help="run every queued sweep and write the report (the daily task)")
    ap.add_argument("--report", action="store_true",
                    help="show the last daily sweep's findings awaiting a verdict")
    a = ap.parse_args(argv)

    if a.drain:
        res = drain()
        print(json.dumps(res, indent=1, ensure_ascii=False) if a.json else _render_drain(res))
        return 0
    if a.report:
        rep = pending_report()
        print(json.dumps(rep, indent=1, ensure_ascii=False) if a.json else _render_drain(rep))
        return 0
    if not a.scope:
        ap.error("--scope is required (or use --drain / --report)")
    if a.enqueue:
        spec = enqueue(a.scope, a.root, a.sweep)
        print(f"liveness: queued scope={a.scope} sweep={spec['sweep']} "
              f"roots={len(spec['roots'])} → runs with the next daily backup")
        return 0

    res = sweep(a.scope, a.sweep, a.root)
    if a.json:
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 0
    print(_render(res))
    return 0


def _render_drain(res: dict) -> str:
    if not res:
        return "liveness: no daily sweep has run yet."
    out = [f"LIVENESS DAILY SWEEP — {res.get('ran_utc', '?')}"]
    for scope, r in (res.get("scopes") or {}).items():
        if r.get("error"):
            out.append(f"  {scope}: ERROR {r['error']}")
        else:
            out.append(f"  {scope}: {r['checked']} checked · {r['anchor_gone']} anchor-gone")
    need = res.get("needs_verdict") or []
    if need:
        out.append("")
        out.append(f"  {len(need)} atom(s) AWAITING YOUR VERDICT (the daemon never mutates —")
        out.append("  a dead path is usually a MOVE, and auto-disputing destroys earned weight):")
        for n in need[:20]:
            out.append(f"    [{n['scope']}] {n['slug']} → {', '.join(map(str, n['dead']))[:90]}")
    else:
        out.append("  nothing awaiting a verdict.")
    drift = res.get("scope_drift") or []
    if drift:
        out.append("")
        out.append(f"  {len(drift)} canonical(s) with SCOPE DRIFT — filed where you were")
        out.append("  STANDING, not where the fact BELONGS (hygiene canonicalises by weight):")
        for d in drift[:15]:
            cands = ", ".join(f"{s}×{n}" for s, n in d.get("candidates", []))
            out.append(f"    {d['coordinate']}")
            out.append(f"       canonical={d['canonical_scope']}  subsumed copies: {cands}")
        out.append("  NOT auto-migrated: `atoms` has no rename-scope verb, and a canonical")
        out.append("  carries merge receipts + inbound refs that would dangle. Re-canonicalise")
        out.append("  through hygiene when you decide the owner — the count is a hint, not a verdict.")
    return "\n".join(out)


main = _main

if __name__ == "__main__":
    sys.exit(_main())
