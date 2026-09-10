"""contracts — the CONTRACTS drawer: laws as data at <repo>/.echelon/contracts.

The state-of-LAWS half of the workspace substrate (WORKSPACE-INTEGRATION-CONTRACT
step 2c). One drawer per room: contracts/<name>.json (one per family, UI first)
+ contracts/components/<name>.json (the forge registry's component laws).

§2 shapes (SPEC-2c): a contract file is {v, id, kind, title, scope_globs, source,
constraint_priority, laws}; `laws` is keyed by LAW-ID, each law carries {title,
statement, severity, params, witness, source}. The verification receipt lives at
<room>/verification/latest.json (+ history/) as {v, ts, gate, subject, ok,
findings[], checks} — the shape workcycle._verify_violations counts.

Laws this module signs (contract §0): VERSIONED JSON (every file carries "v": 1;
a reader meeting v > 1 prints ONE skip line and ignores the file) · NEVER RAISE
(load/applicable/laws/component/receipt/doctor/main degrade to a stderr line,
never a traceback) · ONE WRITER (this module only READS the drawer) · stdlib only,
zero echelon_engine imports (the scanner enforces it).

CLI:  python -X utf8 -m echelon_engine.contracts <verb>
      echelon contracts <verb>             (after __main__.py wiring)
"""
from __future__ import annotations

import fnmatch
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1

# api.json required/optional fields (registry §`contracts/api.json`, 2e deliverable A).
API_REQUIRED = ("version", "exact", "read_only", "scope_globs", "base_path",
                "collection_verbs", "member_verbs", "response_envelope",
                "pagination_style", "id_style", "error_required_paths", "auth_default")
API_OPTIONAL = ("pagination_query_keys", "pagination_response_paths", "auth_public_paths")


def looks_like_api(path: Path | None, raw: dict) -> bool:
    """M1 heal: a file "looks like" an api.json contract by FILENAME
    (path.name == "api.json") OR by carrying registry API-shape fields —
    NEVER by raw.get("kind") == "api", because a schema-faithful registry
    document (01-contract-schemas.md's own example) has NO "kind" key at
    all: {"version":1,"exact":...,"read_only":...,"scope_globs":[...],
    "base_path":...}. Keying the UNREADABLE/doctor probe on "kind" left that
    exact shape silently invisible to both the fence-absence detector and
    doctor -- a dead fence with an all-green doctor (gate probe5, the ac057ab
    shape transplanted one level up: not a wrong KEY VALUE this time, but a
    wrong KEY altogether). Registry fields are checked at top level (the raw
    registry document itself) OR nested under "api" (the 2e dual-keyed
    drawer shape) -- either carries enough of API_REQUIRED to count."""
    if not isinstance(raw, dict):
        return False
    if path is not None and path.name == "api.json":
        return True
    if raw.get("kind") == "api":
        return True
    top_hits = sum(1 for f in API_REQUIRED if f in raw)
    nested = raw.get("api")
    nested_hits = sum(1 for f in API_REQUIRED if isinstance(nested, dict) and f in nested)
    return top_hits >= 3 or nested_hits >= 3


# ── tiny helpers ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict | None:
    """Own reader (never workcycle's private one): broken/v > 1 -> None + 1 line."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"contracts: {path}: bad json, skipped", file=sys.stderr)
        return None
    except OSError:
        print(f"contracts: {path}: unreadable, skipped", file=sys.stderr)
        return None
    if isinstance(data, dict) and isinstance(data.get("v"), int) and data["v"] > VERSION:
        print(f"contracts: {path}: v{data['v']} unsupported, skipped", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def _room_path(start: Path | None = None) -> Path | None:
    """Nearest ancestor whose .echelon/ holds room.json (never creates)."""
    cur = (Path(start).resolve() if start is not None else Path.cwd().resolve())
    while True:
        e = cur / ".echelon"
        if (e / "room.json").exists():
            return e
        if cur == cur.parent:
            return None
        cur = cur.parent


def scope_from_room(start: Path | None = None) -> tuple[str, str] | None:
    """OPEN-0070 (scope autodetect): when a CLI runs without --scope, resolve the scope from the
    WORKING ROOM — walk up from `start` (default cwd) for the nearest .echelon/room.json and adopt
    its `scope`; fall back to the nearest echelon-harness-contract.json {scope}. Returns
    (scope, source) with source in ("room", "contract") so the caller can print WHERE the scope
    came from; None when neither exists. Stdlib-only, never creates, never raises."""
    e = _room_path(start)
    if e is not None:
        try:
            rj = json.loads((e / "room.json").read_text(encoding="utf-8")) or {}
            sc = (rj or {}).get("scope")
            if sc:
                return sc, "room"
        except Exception:
            pass
    cur = (Path(start).resolve() if start is not None else Path.cwd().resolve())
    while True:
        c = cur / "echelon-harness-contract.json"
        if c.exists():
            try:
                d = json.loads(c.read_text(encoding="utf-8")) or {}
                sc = (d or {}).get("scope")
                return (sc, "contract") if sc else None
            except Exception:
                return None
        if cur == cur.parent:
            return None
        cur = cur.parent


# ── load ─────────────────────────────────────────────────────────────────────

def load(room: Path) -> dict[str, dict]:
    """All contract files keyed by id; unreadable / bad json / v > 1 / missing v
    or id / duplicate id (first wins) each print ONE stderr line, skipped."""
    result: dict[str, dict] = {}
    e = Path(room).resolve()
    cdir = e / "contracts"
    if not cdir.is_dir():
        return result
    files = sorted(cdir.glob("*.json")) + sorted((cdir / "components").glob("*.json"))
    for p in files:
        data = _read_json(p)
        if data is None:
            continue
        if not isinstance(data.get("v"), int):
            print(f"contracts: {p}: missing v, skipped", file=sys.stderr)
            continue
        cid = data.get("id")
        if not isinstance(cid, str) or not cid.strip():
            print(f"contracts: {p}: missing id, skipped", file=sys.stderr)
            continue
        if cid in result:
            print(f"contracts: {p}: duplicate id {cid!r} (name {data.get('name')!r}), "
                  f"first wins — component() cannot reach the shadowed file", file=sys.stderr)
            continue
        result[cid] = data
    return result


# ── queries ──────────────────────────────────────────────────────────────────

def applicable(room: Path, kind: str | None = None,
               touches: list[str] | None = None) -> list[str]:
    """Sorted ids touching the working set: an empty scope_globs applies to
    every touch, else any touch fnmatch'ing any glob (POSIX-normalized, so a
    Windows backslash path still matches). `kind` accepted and ignored (2d).
    fnmatch's `*` crosses `/`, so `os_client/*` and `os_client/**` match the
    same paths — there is no globstar; write globs knowing that (2d reads them).
    A malformed `scope_globs` (not a list) is skipped with one stderr line —
    never iterated as a string, whose `*` characters would fail OPEN to every
    path (gate-caught 2026-08-27)."""
    touches = [t.replace("\\", "/") for t in (touches or [])]
    ids: list[str] = []
    for cid, c in load(room).items():
        globs = c.get("scope_globs") or []
        if not isinstance(globs, list):
            print(f"contracts: {cid}: scope_globs must be a list, got "
                  f"{type(globs).__name__}; contract skipped", file=sys.stderr)
            continue
        if not globs or any(fnmatch.fnmatch(t, g) for t in touches for g in globs):
            ids.append(cid)
    return sorted(ids)


def laws(room: Path, contract_id: str) -> dict:
    """The contract's `laws` block keyed by LAW-ID, or {} on any miss."""
    c = load(room).get(contract_id)
    return c.get("laws") if isinstance(c, dict) and isinstance(c.get("laws"), dict) else {}


def component(room: Path, name: str) -> dict | None:
    """A component contract by `name` OR `id`; None on miss."""
    for c in load(room).values():
        if c.get("name") == name or c.get("id") == name:
            return c
    return None


# ── receipt ──────────────────────────────────────────────────────────────────

def receipt(room: Path, *, gate: str, subject: str, findings: list[dict],
            checks: dict | None = None) -> Path:
    """§2.3 receipt to latest.json (last gate wins) + history/ (append-only);
    returns the history path. NEVER raises — a failed write is a stderr line."""
    e = Path(room).resolve()
    ts = _now()
    doc = {"v": VERSION, "ts": ts, "gate": gate, "subject": subject,
           # ok = no ERROR-severity finding (gate r1 V8b M-2 / INC-0003): acked warnings ride
           # along; `not findings` stamped every warning-carrying bind as ok:false and made
           # propose's post-build mode (_has_bind_receipt needs ok True) unreachable.
           "ok": not any(f.get("severity") == "error" for f in findings),
           "findings": findings, "checks": checks or {}}
    hist = e / "verification" / "history" / (
        f"{ts.replace(':', '').split('+')[0]}-{gate}-{subject}.json")
    latest = e / "verification" / "latest.json"
    try:
        hist.parent.mkdir(parents=True, exist_ok=True)
        latest.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        latest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    except OSError as exc:
        print(f"contracts: receipt write failed: {exc}", file=sys.stderr)
    return hist


# ── doctor ───────────────────────────────────────────────────────────────────

def doctor(room: Path) -> list[dict]:
    """Contract §8 row 9: drawer rows as {row: 9, check, ok, evidence}; an
    empty drawer degrades to one row ok False, evidence CONTRACTS-NOT-LOADED."""
    e = Path(room).resolve()
    cdir = e / "contracts"
    files = (sorted(cdir.glob("*.json")) + sorted((cdir / "components").glob("*.json"))
             if cdir.is_dir() else [])
    if not files:
        return [{"row": 9, "check": "drawer present", "ok": False,
                 "evidence": "CONTRACTS-NOT-LOADED"}]

    parsed: list[dict] = []
    bad: list[str] = []
    for p in files:
        d = _read_json(p)  # prints its own stderr line for bad json / v > 1
        if d is None:
            bad.append(f"{p.name} (unreadable/bad json/v>1)")
        elif not isinstance(d.get("v"), int):
            bad.append(f"{p.name} (missing v)")
        elif not isinstance(d.get("id"), str) or not d.get("id").strip():
            bad.append(f"{p.name} (missing id)")
        else:
            parsed.append(d)
    rows: list[dict] = [
        {"row": 9, "check": "every contract parses with v+id", "ok": not bad,
         "evidence": "; ".join(bad) if bad else f"{len(parsed)} files"},
    ]

    ids = [c["id"] for c in parsed]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    rows.append({"row": 9, "check": "ids unique across the drawer",
                 "ok": not dups,
                 "evidence": f"duplicates: {dups}" if dups else f"{len(ids)} ids"})

    missing: list[str] = []
    for c in parsed:
        for lid, law in (c.get("laws") or {}).items():
            if not isinstance(law, dict):
                missing.append(f"{c['id']}/{lid} (not an object)")
                continue
            if not law.get("witness"):
                missing.append(f"{c['id']}/{lid} (no witness)")
            if law.get("severity") not in ("error", "warning"):
                missing.append(f"{c['id']}/{lid} (severity {law.get('severity')!r})")
    rows.append({"row": 9, "check": "every law has witness + severity",
                 "ok": not missing,
                 "evidence": "; ".join(missing) if missing else "all laws witnessed"})

    if any(c.get("kind") == "ui" for c in parsed):
        have = {lid for c in parsed for lid in (c.get("laws") or {})}
        need = ("UI-TOKENS", "UI-MOUNT-SCOPED-IDS", "PAGE-RECEIPT-SHAPE",
                "PAGE-RED-BUDGET")
        absent = [n for n in need if n not in have]
        rows.append({"row": 9, "check": "PROPOSAL-GATE ids present (kind: ui)",
                     "ok": not absent,
                     "evidence": f"missing: {absent}" if absent else "all four present"})

    # M1 heal (gate probe5): a contract is API-SHAPED by looks_like_api()
    # (filename api.json OR carrying registry API fields) — NEVER by
    # raw.get("kind") == "api" alone, since the registry's own schema
    # example (01-contract-schemas.md) has no "kind" key at all. A loaded
    # contract that is api-shaped but lacks kind:"api" still gets the
    # required-fields row (the "no-kind-but-loadable" case gate probe5
    # showed silently invisible before this heal).
    for c in parsed:
        if not looks_like_api(None, c):
            continue
        api = c.get("api") if isinstance(c.get("api"), dict) else c
        missing = [f for f in API_REQUIRED if f not in api]
        rows.append({"row": 9, "check": f"{c['id']}: required api.json fields present",
                     "ok": not missing,
                     "evidence": f"missing: {missing}" if missing else "all required fields present"})

    # RULINGS A1: a file on disk that LOOKS like an api contract (parses,
    # but load() dropped it for missing v/id) must FAIL a row, not merely be
    # absent from the row list — the exact fail-open shape ac057ab caught.
    loaded_ids = {c["id"] for c in parsed}
    for p in files:
        raw = _read_json(p)
        if looks_like_api(p, raw) and (not isinstance(raw, dict) or raw.get("id") not in loaded_ids):
            rows.append({"row": 9, "check": f"{p.name}: api contract loads",
                         "ok": False,
                         "evidence": f"{p.name} looks like an api contract but was dropped (missing/invalid v or id) — API-READ-ONLY cannot fire on it"})
    return rows


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI: list | show <id> | applicable [--kind K] --touch P ... | doctor.
    JSON out for show/applicable/doctor; list prints `id kind title` lines."""
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon contracts",
        description="The CONTRACTS drawer — laws as data at <repo>/.echelon/contracts")
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="list contracts as `id kind title` lines")
    p_show = sub.add_parser("show", help="one contract as JSON: show <id>")
    p_show.add_argument("id")
    p_app = sub.add_parser("applicable", help="ids touching paths: applicable --touch P ...")
    p_app.add_argument("--kind", default=None, help="accepted and ignored (reserved for 2d)")
    p_app.add_argument("--touch", action="append", default=[],
                       help="repo-relative path (repeatable)")
    sub.add_parser("doctor", help="drawer health rows (row 9 of `room doctor`)")

    args = ap.parse_args(argv)

    room = _room_path()
    if room is None:
        print("ROOM unavailable (no room — run `workcycle init` at the repo root)")
        return 1

    if args.action == "list":
        for cid, c in sorted(load(room).items()):
            print(f"{cid}  {c.get('kind', '-')}  {c.get('title', '-')}")
    elif args.action == "show":
        c = load(room).get(args.id)
        if c is None:
            print(f"contracts: no contract {args.id!r} (CONTRACT-CLAIMED-NOT-FOUND)")
            return 1
        print(json.dumps(c, ensure_ascii=False, indent=2))
    elif args.action == "applicable":
        print(json.dumps(applicable(room, args.kind, args.touch),
                         ensure_ascii=False, indent=2))
    elif args.action == "doctor":
        rows = doctor(room)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0 if all(r["ok"] for r in rows) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
