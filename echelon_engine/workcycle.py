"""workcycle — the ROOM verb: state of WORK at <repo>/.echelon/.

The state-of-WORK half of the workspace substrate (WORKSPACE-INTEGRATION-CONTRACT
step 2b). One room per repo: <root>/.echelon/ with room.json, cursor.json,
journal/, open/, closed/, incidents/, proposals/, index/, contracts/,
changes/active, changes/completed, verification/.

Laws this module signs (contract §0): ONE WRITER (only this module writes room
files) · NEVER BLOCK (resume_brief degrades to `ROOM unavailable (<reason>)`) ·
VERSIONED JSON (every file carries "v": 1; a reader meeting v > 1 prints one
skip line and continues) · STATE TO THE ROOM (checkpoint writes cursor.json AND
the SessionLedger in one call; the ledger is best-effort, never fatal).

CLI:  python -X utf8 -m echelon_engine.workcycle <verb>
      echelon workcycle <verb>            (after __main__.py wiring, see WIRING_NOTE.md)
"""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from echelon_engine import contracts as _contracts
from echelon_engine import session_state
from echelon_engine.atoms import resolve_scope as _resolve_scope_mod
from echelon_engine.atoms.echelon_home import echelon_home as _echelon_home
from echelon_engine.atoms.hygiene import DEFAULT_DB as _DEFAULT_DB

VERSION = 1


class RoomExists(Exception):
    """Raised by init() when a room (room.json) already exists at the root."""


class ClaimHeld(RuntimeError):
    """Raised by claim()/release() when the active row belongs to another session.
    Carries the holder's pid + liveness (owner 2026-09-01: a claim is a MARK, not a
    freeze — the refused claimant must be able to see whether it is really worked on)."""

    def __init__(self, holder: str, ts: str, pid: int | None = None, alive: bool | None = None):
        live = "" if alive is None else (" (pid alive)" if alive else " (pid DEAD — steal with --steal-dead)")
        super().__init__(f"held by {holder} since {ts}{live}")
        self.holder, self.ts, self.pid, self.alive = holder, ts, pid, alive


class ClaimBusy(RuntimeError):
    """The SQLite writer lock could not be acquired; no claim result is known.

    This is deliberately distinct from :class:`ClaimHeld`: before ``BEGIN
    IMMEDIATE`` succeeds there is no safe holder row to report.  ``code`` is the
    SQLite primary/extended error code from the original exception, which is
    retained as ``__cause__`` by the acquisition helper.
    """

    def __init__(self, operation: str, code: int | None):
        super().__init__(f"{operation} acquisition busy; no acknowledgement")
        self.operation, self.code = operation, code


def _begin_claim_transaction(conn: sqlite3.Connection, operation: str) -> None:
    """Begin the sole claim writer transaction, classifying only busy/locked."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        primary = code & 0xFF if isinstance(code, int) else None
        if primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise ClaimBusy(operation, code) from exc
        raise


# ── tiny helpers ─────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid) -> bool | None:
    """Is the process alive on THIS machine? None = unknowable (no/invalid pid).
    The claim's pid is only meaningful on the box that stamped it — same-host estates."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _norm_slash(p: str) -> str:
    # forward slashes in JSON paths (Windows-safe room files)
    return str(p).replace("\\", "/")


def _ancestors(p: Path):
    # yields p, its parent, ..., up to the filesystem root (inclusive)
    while True:
        yield p
        if p == p.parent:
            return
        p = p.parent


def _count_json(d: Path) -> int:
    return len(list(d.glob("*.json"))) if d.is_dir() else 0


def _journal_lines(jdir: Path) -> int:
    # count of non-blank lines across all journal files
    if not jdir.is_dir():
        return 0
    n = 0
    for p in jdir.glob("*.jsonl"):
        for line in _safe(lambda: p.read_text(encoding="utf-8").splitlines(), []):
            if line.strip():
                n += 1
    return n


def _latest_journal_file(jdir: Path) -> Path | None:
    files = sorted(jdir.glob("*.jsonl")) if jdir.is_dir() else []
    return files[-1] if files else None


def _last_journal_line(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _iter_jsonl(p: Path):
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _find_eos(d: Path) -> dict | None:
    # first *.eos file in d (sorted), parsed as JSON; None if none/parse-fail
    for p in sorted(d.glob("*.eos")):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _write_json(path: Path, data: dict) -> None:
    # atomic-ish: tmp file in the same dir, then replace
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    # v-gate: v > 1 prints one skip line and returns default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if isinstance(data, dict) and isinstance(data.get("v"), int) and data["v"] > VERSION:
        # stderr, never stdout: the reflex hook reads room files and its stdout IS the
        # harness protocol channel — one stray line there drops every guard (gate M-1).
        print(f"workcycle: {path} v{data['v']} unsupported, skipped", file=sys.stderr)
        return default
    return data


def _next_id(prefix: str, *dirs: Path) -> str:
    # next sequential id (OPEN-0007 / INC-0001) across the dirs
    hi = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob(f"{prefix}-*.json"):
            m = re.fullmatch(re.escape(prefix) + r"-(\d+)", p.stem)
            if m:
                hi = max(hi, int(m.group(1)))
    return f"{prefix}-{hi + 1:04d}"


def _safe(fn, default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _resolve_scope(root: Path) -> str:
    """Bank scope for a root, READ side. Degrades to the leaf name when the bank is down —
    and, since resolve_scope now fails closed on an unknown dir (OPEN-0036), also when no
    live scope owns `root`. Callers that CREATE a room want `_resolve_scope_new` instead."""
    return _safe(lambda: _resolve_scope_mod.resolve_scope(str(root)), root.name or "echelon")


def _resolve_scope_new(root: Path) -> str:
    """Bank scope for a root, CREATE side — `init()` registering a brand-new project's room
    legitimately mints a scope the bank has never seen, so it opens the EXPLICIT create door
    (`allow_new=True`) rather than silently taking the read side's degraded default."""
    return _safe(lambda: _resolve_scope_mod.resolve_scope(str(root), allow_new=True),
                 root.name or "echelon")


def _room_scope(room: Path) -> str:
    """The room's scope binding (room.json.scope, law 1) — falls back to resolve."""
    e = Path(room).resolve()
    rj = _read_json(e / "room.json", {}) or {}
    return rj.get("scope") or _resolve_scope(room)


# ── registry (~/.echelon/rooms.json — ONE WRITER: this module) ───────────────

def _registry_path() -> Path:
    """The room registry file under the substrate home ($ECHELON_HOME override)."""
    return _echelon_home() / "rooms.json"


def _registry_entries() -> dict:
    """{name: {path, estate, scope, type, registered}} for every registered room."""
    data = _read_json(_registry_path(), {}) or {}
    rooms = data.get("rooms") if isinstance(data, dict) else None
    return rooms if isinstance(rooms, dict) else {}


def _registry_estates() -> dict:
    """The top-level `estates {name: {rooms:[...], tag}}` map (R-0154 slice 2, LAW 2) — a named
    GROUP of rooms, never a directory with its own state. {} when absent (pre-slice-2 registries)."""
    data = _read_json(_registry_path(), {}) or {}
    est = data.get("estates") if isinstance(data, dict) else None
    return est if isinstance(est, dict) else {}


def _save_registry(rooms: dict, estates: dict | None = None) -> None:
    """Rewrite the registry. Preserves the top-level `estates` map (LAW 2): pass it to replace,
    omit to carry the existing map through — never silently drops it on a rooms-only write."""
    est = estates if estates is not None else _registry_estates()
    out = {"v": VERSION, "rooms": rooms}
    if est:
        out["estates"] = est
    _write_json(_registry_path(), out)


def register_room(root: Path, *, name: str | None = None, scope: str | None = None) -> dict:
    """Register the room at <root>/.echelon into ~/.echelon/rooms.json (idempotent).

    ONE WRITER law (contract §0 row 2): workcycle is the only rooms.json writer.
    `name` defaults to the room's estate; `scope` defaults to the room's declared
    scope — pass `scope` to bind an estate scope that differs from the bank scope
    (e.g. ECHELON-AGENT registers with scope `echelon-agent`)."""
    e = Path(root).resolve() / ".echelon"
    rj = _read_json(e / "room.json", None)
    if not rj:
        raise FileNotFoundError(f"no room at {root} (run `workcycle init` first)")
    key = name or rj.get("estate") or e.parent.name
    rooms = _registry_entries()
    entry = {
        "path": _norm_slash(str(e)),
        "estate": rj.get("estate") or key,
        "scope": scope or rj.get("scope") or _resolve_scope(root),
        "type": rj.get("type") or "engine",
        # first-registered is provenance; re-registering refreshes the rest (gate r1 S8a S-1)
        "registered": (rooms.get(key) or {}).get("registered") or _now(),
    }
    if isinstance((rooms.get(key) or {}).get("projects"), dict):
        entry["projects"] = rooms[key]["projects"]
    rooms[key] = entry
    _save_registry(rooms)
    return entry


def set_room_projects(name: str, projects: dict) -> dict:
    """Replace one room's project map through the sole rooms.json writer."""
    rooms = _registry_entries()
    if name not in rooms:
        raise KeyError(f"unknown registered room {name!r}")
    if not isinstance(projects, dict):
        raise ValueError("projects must be an object")
    clean = {}
    for slug, meta in projects.items():
        slug = str(slug or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug) or not isinstance(meta, dict):
            raise ValueError(f"invalid project {slug!r}")
        root = str(meta.get("root") or "").strip()
        if not root:
            raise ValueError(f"project {slug!r} needs a relative root")
        clean[slug] = {"name": str(meta.get("name") or slug), "root": _norm_slash(root),
                       "deploy": bool(meta.get("deploy", False)), "repo": bool(meta.get("repo", False))}
    rooms[name] = {**rooms[name], "projects": clean}
    _save_registry(rooms)
    return rooms[name]


def registered_rooms() -> list[str]:
    """Registered room names, sorted ([] when the registry is absent/empty)."""
    return sorted(_registry_entries())


def registry_entry(name: str) -> dict:
    """One registered room's entry ({} when unknown) — a READ door for tools
    that must never write rooms.json (harness-sync child contracts, hooks)."""
    return _registry_entries().get(name) or {}


# ── estates: a named GROUP of rooms (R-0154 slice 2, LAW 2) ──────────────────

def estates() -> dict:
    """The estates map, each with its declared `rooms` list, board `tag`, and the LIVE membership
    resolved from the registry (a room whose entry `estate` names this estate, PLUS every declared
    room that exists). An estate is a pointer-and-tag GROUP — it owns no directory and no state.

    Shape: {name: {"tag": str, "rooms": [declared...], "members": [live registry names...],
                   "missing": [declared-but-unregistered...]}}. Child rooms belong to their
    PARENT's estate (a promoted child is still in the same estate as the room it split from)."""
    est = _registry_estates()
    rooms = _registry_entries()
    # reverse index: which estate a registry entry belongs to (an explicit estate-membership map,
    # then the room's own `estate` field when it names a real estate, then its parent's estate).
    declared = {}
    for ename, meta in est.items():
        for rn in (meta.get("rooms") or []):
            declared[rn] = ename
    out = {}
    for ename, meta in est.items():
        members, missing = [], []
        for rn in (meta.get("rooms") or []):
            (members if rn in rooms else missing).append(rn)
        # also fold in any registered room whose parent is a member (children ride their parent)
        for rn, entry in rooms.items():
            par = entry.get("parent")
            if par and declared.get(par) == ename and rn not in members:
                members.append(rn)
        out[ename] = {"tag": meta.get("tag") or ename,
                      "rooms": list(meta.get("rooms") or []),
                      "members": sorted(set(members)), "missing": missing}
    return out


def estate_of(room: str) -> str | None:
    """The estate a registered room belongs to (its parent's estate if it is a child), or None."""
    rooms = _registry_entries()
    entry = rooms.get(room) or {}
    probe = entry.get("parent") or room     # a child rides its parent's estate membership
    for ename, meta in _registry_estates().items():
        if probe in (meta.get("rooms") or []):
            return ename
    return None


def set_estate(name: str, room: str, *, tag: str | None = None) -> dict:
    """Assign a registered ROOM to estate `name` (creating the estate if new). The ONE WRITER
    law holds — this is the only door that edits the `estates` map. Idempotent. A child room is
    NOT added directly (it rides its parent); assign the parent instead, refused with a hint."""
    rooms = _registry_entries()
    if room not in rooms:
        raise KeyError(f"unknown registered room {room!r} (register it first)")
    if rooms[room].get("parent"):
        raise ValueError(f"{room!r} is a child room; it rides its parent "
                         f"{rooms[room]['parent']!r}'s estate — assign the parent instead")
    est = _registry_estates()
    entry = dict(est.get(name) or {})
    entry.setdefault("tag", tag or name)
    if tag:
        entry["tag"] = tag
    members = list(entry.get("rooms") or [])
    # a room lives in exactly one estate — drop it from any other before adding (LAW 2)
    for ename, meta in est.items():
        if ename != name and room in (meta.get("rooms") or []):
            meta["rooms"] = [r for r in meta["rooms"] if r != room]
    if room not in members:
        members.append(room)
    entry["rooms"] = sorted(set(members))
    est[name] = entry
    _save_registry(rooms, estates=est)
    return {"ok": True, "estate": name, "tag": entry["tag"], "rooms": entry["rooms"]}


# ── child rooms: earned promotion / rollback (SPEC-S9 step 2) ────────────────

def _entry_hash(entry: dict) -> str:
    """Stable short hash of one canonical registry entry — the before/after
    hashes the registry journal records for every promote/demote."""
    return hashlib.sha256(
        json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry_journal(line: dict) -> None:
    """Append one line to <echelon-home>/registry-journal.jsonl — written only
    by workcycle promote/demote, the same ONE WRITER that owns rooms.json."""
    with open(_registry_path().with_name("registry-journal.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _tagged_in_dir(drawer: Path, slug: str) -> list[Path]:
    """Drawer files whose explicit project tag equals `slug` (sorted). Only
    tagged records move — criterion 4: null/ambiguous records stay with the
    parent no matter how casually they mention the project."""
    if not drawer.is_dir():
        return []
    out = []
    for p in sorted(drawer.glob("*.json")):
        item = _read_json(p, None) or {}
        if _project_slug(item.get("project")) == slug:
            out.append(p)
    return out


def _child_manifest(parent_dir: Path, slug: str) -> Path:
    return parent_dir / "manifests" / f"{slug}.json"


def promote(room: str, project: str, *, dry_run: bool = False,
            estate: bool = False, scope_override: str | None = None) -> dict:
    """Earned child room (SPEC-S9 step 2): promote <room>'s <project>.

    All five criteria are verified before ANY write; a broken one raises
    ValueError naming it. Dry-run returns the full plan (criteria, exact
    source/destination files, rollback) and writes ZERO bytes. Apply creates
    the MINIMAL child `<project-root>/.echelon/` (room.json, cursor.json,
    open/ closed/ incidents/, receipts.jsonl — no inbox, no journal, no
    claims/orchestrator seat), copies the tagged records verbatim (parent
    copies stay — the rollback baseline), writes the parent-side manifest of
    per-file hashes, and only then publishes the registry entry under the
    qualified name `room/project` with `parent: room`. Re-apply on a
    registered child is an idempotent no-op.

    ESTATE MODE (R-0154, owner ruling B, board #4145): a child becomes a room
    the day it has its OWN REPO. `estate=True` gates on repo-exists ONLY — the
    project root must be a real git repo (a `.git` dir OR file) — and WAIVES
    crit 2 (deploy boundary) and crit 3 (>=3 tagged items). crit 1 (repo=true)
    and demote rollback are unchanged. The obj-state case: promote NEVER rm's
    the child `.echelon/` (mkdir exist_ok, writes only its own files), so a
    project whose `.echelon/` already holds framework obj state (cache.json,
    ledger.jsonl, txn/) keeps it — the room files coexist."""
    slug = _project_slug(project)
    if not slug:
        raise ValueError(f"invalid project {project!r}")
    entries = _registry_entries()
    parent_entry = entries.get(room)
    if not parent_entry or not parent_entry.get("path"):
        raise KeyError(f"unknown registered room {room!r} (promote needs the registry door)")
    parent_dir = Path(parent_entry["path"]).resolve()
    pmeta = (parent_entry.get("projects") or {}).get(slug)
    child_name = f"{room}/{slug}"
    existing = entries.get(child_name)
    project_root = (parent_dir.parent / str(pmeta.get("root") or "")).resolve() \
        if pmeta and pmeta.get("root") else parent_dir.parent / slug
    child_dir = project_root / ".echelon"
    if existing and existing.get("parent") == room and (child_dir / "room.json").exists():
        return {"ok": True, "already": True, "child": child_name, "path": str(child_dir)}
    if existing:
        raise ValueError(
            f"criterion 5: qualified name {child_name!r} is already registered "
            f"under parent {existing.get('parent')!r} but {child_dir} holds no room — "
            f"roll back first: `workcycle demote {child_name}`")
    if (child_dir / "room.json").exists():
        raise RoomExists(str(project_root))

    refusals = []
    if not pmeta:
        refusals.append(f"{room!r} has no registered project {slug!r} (registry projects map)")
    else:
        if not pmeta.get("repo"):
            refusals.append(f"criterion 1: {slug!r} repo=false — no independent repository")
        if not estate and not pmeta.get("deploy"):
            refusals.append(f"criterion 2: {slug!r} deploy=false — no deploy boundary")
    tagged = _tagged_in_dir(parent_dir / "open", slug) if pmeta else []
    if not estate and len(tagged) < 3:
        refusals.append(f"criterion 3: {slug!r} has {len(tagged)} open tagged item(s); 3 required")
    if estate and pmeta and not (project_root / ".git").exists():
        # estate mode: the ONE gate is a real repo at the project root (a `.git`
        # dir OR the `.git` file of a worktree). "A child becomes a room the day
        # it has its own repo."
        refusals.append(f"estate criterion: {project_root} is not a git repo (no .git)")
    if refusals:
        raise ValueError("promotion refused:\n  " + "\n  ".join(refusals))
    if not project_root.is_dir():
        raise FileNotFoundError(f"criterion 1 evidence: project root {project_root} does not exist")

    drawers = {d: _tagged_in_dir(parent_dir / d, slug) for d in ("open", "closed", "incidents")}
    counts = {d: len(v) for d, v in drawers.items()}
    # scope: default INHERITS the parent (beta-svc/mrp -> mol). An explicit
    # `scope=` overrides — R-0154 owner ruling B pending: the estate case may bind
    # scope=own-name (`--scope <slug>` / `--scope-own`) so the new repo owns its bank.
    scope = scope_override or parent_entry.get("scope") or _resolve_scope(parent_dir)
    files = {d: {p.name: _sha256_file(p) for p in v} for d, v in drawers.items()}
    digest = hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    rollback = (f"`workcycle demote {child_name}` restores the parent exactly: it verifies the "
                f"manifest, back-copies child-only records under NEW parent ids, deletes only "
                f"{child_dir} — project source is never touched")
    if dry_run:
        return {"ok": True, "dry_run": True, "child": child_name, "path": str(child_dir),
                "estate": estate,
                "criteria": {"repo": bool(pmeta.get("repo")), "deploy": bool(pmeta.get("deploy")),
                             "open_items": len(tagged), "project_root": str(project_root)},
                "counts": counts, "files": {d: sorted(v) for d, v in files.items()},
                "rollback": rollback, "manifest_digest": digest, "scope": scope}

    child_dir.mkdir(parents=True, exist_ok=True)
    try:
        for sub in ("open", "closed", "incidents"):
            (child_dir / sub).mkdir(exist_ok=True)
        for d, srcs in drawers.items():
            for src in srcs:
                dst = child_dir / d / src.name
                dst.write_bytes(src.read_bytes())
        # validation before ANY registry publication: every copy must match its source
        for d, srcs in drawers.items():
            for src in srcs:
                dst = child_dir / d / src.name
                if not dst.exists() or _sha256_file(dst) != _sha256_file(src):
                    raise OSError(f"copy validation failed for {d}/{src.name}")
        _write_json(child_dir / "room.json", {
            "v": VERSION, "estate": child_name, "type": "engine",
            "parent": room, "project": slug, "scope": scope,
            "goal_ref": None, "services": [],
        })
        _write_json(child_dir / "cursor.json", {
            "v": VERSION, "goal": None, "task": None, "stage": "promoted",
            "checkpoint": (f"promoted from {room} with {counts['open']} open, "
                           f"{counts['closed']} closed, {counts['incidents']} incident(s) tagged {slug}"),
            "next_actions": [], "blockers": [], "working_set": [],
            "recent_decisions": [], "updated": _now(),
        })
        _ensure_gitignore(project_root)  # the project repo ignores its new .echelon/
        mdir = parent_dir / "manifests"
        mdir.mkdir(exist_ok=True)
        manifest = {"v": VERSION, "child": child_name, "parent": room, "project": slug,
                    "promoted": _now(), "origin": _session_id(None), "files": files}
        _write_json(_child_manifest(parent_dir, slug), manifest)
    except Exception:
        shutil.rmtree(child_dir, ignore_errors=True)  # never leave a partial child behind
        raise

    # registry publication — the atomic commit point
    child_entry = {"path": _norm_slash(str(child_dir)), "estate": child_name,
                   "parent": room, "project": slug, "scope": scope,
                   "type": "engine", "registered": _now()}
    entries[child_name] = child_entry
    _save_registry(entries)
    _registry_journal({"ts": _now(), "kind": "child_promoted", "child": child_name,
                       "parent": room, "project": slug, "scope": scope,
                       "before": {}, "after": _entry_hash(child_entry)})
    parent_receipt = receipt(parent_dir, "child_promoted",
                             f"promoted {slug}: {counts['open']} open, {counts['closed']} closed, "
                             f"{counts['incidents']} incident(s) -> {child_dir} (manifest {digest})",
                             origin=_session_id(None), ref=child_name, project=slug)
    # fold_now=False: promotion SEEDS the child cursor by hand; folding would grow a
    # journal/ seat inside the minimal child (gate 1). Child acts fold it later.
    child_receipt = receipt(child_dir, "promoted_from_parent",
                            f"registered under {room} ({counts['open']} open, {counts['closed']} "
                            f"closed, {counts['incidents']} incident(s); manifest {digest})",
                            origin=_session_id(None), ref=room, project=slug, fold_now=False)
    return {"ok": True, "child": child_name, "path": str(child_dir), "counts": counts,
            "manifest_digest": digest, "scope": scope,
            "parent_receipt": parent_receipt.get("ok"),
            "child_receipt": child_receipt.get("ok")}


def demote(child_name: str, *, dry_run: bool = False) -> dict:
    """Roll a child room back into its parent (SPEC-S9 step 2 rollback).

    Order: verify the manifest against the PARENT copies first (the rollback
    premise — promotion copied, never moved, so a verified manifest means
    deleting the child loses nothing), unregister the child, back-copy every
    child record that evolved or is child-only to the parent under a NEW
    collision-safe id (with a mapping receipt), then delete exactly the child
    `.echelon/` promotion created. Project source is never touched."""
    room, _, slug = child_name.rpartition("/")
    if not room or not slug:
        raise ValueError(f"demote needs the qualified child name <room>/<project>, got {child_name!r}")
    entries = _registry_entries()
    child_entry = entries.get(child_name)
    if not child_entry:
        raise KeyError(f"no registered child {child_name!r}")
    if child_entry.get("parent") != room:
        raise ValueError(f"{child_name!r} is registered under {child_entry.get('parent')!r}, "
                         f"not {room!r} — refusing")
    parent_entry = entries.get(room)
    if not parent_entry or not parent_entry.get("path"):
        raise KeyError(f"unknown parent room {room!r}")
    parent_dir = Path(parent_entry["path"]).resolve()
    child_dir = Path(child_entry["path"]).resolve()
    if child_dir == parent_dir or child_dir.name != ".echelon":
        raise ValueError(f"refusing: child path {child_dir} is not a distinct .echelon state dir")
    manifest_path = _child_manifest(parent_dir, slug)
    manifest = _read_json(manifest_path, None)
    if not manifest or manifest.get("child") != child_name:
        raise RuntimeError(f"no promotion manifest for {child_name} at {manifest_path} — "
                           f"refusing to delete {child_dir} blind")

    # 1. rollback premise: every parent copy the manifest recorded still exists, identical
    for d, recorded in (manifest.get("files") or {}).items():
        for name, sha in recorded.items():
            src = parent_dir / d / name
            if not src.exists() or _sha256_file(src) != sha:
                raise RuntimeError(f"parent record {d}/{name} no longer matches the promotion "
                                   f"manifest — refusing rollback (parent must stay complete)")

    # 2. classify the child's current records: untouched copies vs evolved/child-only
    untouched, back_copies = [], []
    for d in ("open", "closed", "incidents"):
        for p in sorted((child_dir / d).glob("*.json")) if (child_dir / d).is_dir() else []:
            recorded_sha = (manifest.get("files") or {}).get(d, {}).get(p.name)
            if recorded_sha and _sha256_file(p) == recorded_sha:
                untouched.append(f"{d}/{p.name}")  # the parent already holds this record
                continue
            item = _read_json(p, None) or {}
            old_id = str(item.get("id") or p.stem)
            m = re.match(r"([A-Za-z]+)-\d+", old_id)
            prefix = m.group(1) if m else "ITEM"
            new_id = _next_id(prefix, parent_dir / "open", parent_dir / "closed")
            back_copies.append({"drawer": d, "file": p.name, "old_id": old_id, "new_id": new_id,
                                "child_dir": str(p)})
    if dry_run:
        return {"ok": True, "dry_run": True, "child": child_name, "path": str(child_dir),
                "untouched": untouched,
                "back_copies": [f"{b['drawer']}/{b['file']} -> parent {b['new_id']}"
                                for b in back_copies],
                "deletes": [f"{child_dir}"]}

    # 3. unregister FIRST (spec rollback order), restore registration if a later step fails
    del entries[child_name]
    _save_registry(entries)
    try:
        for b in back_copies:
            item = _read_json(Path(b["child_dir"]), None) or {}
            item["id"] = b["new_id"]
            _write_json(parent_dir / b["drawer"] / f"{b['new_id']}.json", item)
    except Exception:
        entries[child_name] = child_entry
        _save_registry(entries)
        raise

    # 4. delete exactly the promotion-created child state dir
    shutil.rmtree(child_dir)
    manifest_path.unlink(missing_ok=True)
    mapping = ", ".join(f"{b['old_id']}->{b['new_id']}" for b in back_copies) or "none"
    _registry_journal({"ts": _now(), "kind": "child_demoted", "child": child_name,
                       "parent": room, "project": slug,
                       "before": _entry_hash(child_entry), "after": {},
                       "back_copied": len(back_copies), "untouched": len(untouched)})
    demote_receipt = receipt(
        parent_dir, "child_demoted",
        f"rolled back {child_name}: {len(untouched)} untouched copies stayed with the parent, "
        f"{len(back_copies)} child record(s) back-copied under new ids ({mapping}); "
        f"deleted {child_dir}",
        origin=_session_id(None), ref=child_name, project=slug)
    return {"ok": True, "child": child_name, "deleted": str(child_dir),
            "untouched": untouched, "back_copies": back_copies,
            "receipt_ok": demote_receipt.get("ok")}


def registry_room_by_scope(scope: str) -> Path | None:
    """The first registered room whose entry scope equals `scope` (insertion order)."""
    for entry in _registry_entries().values():
        if entry.get("scope") == scope and entry.get("path"):
            return Path(entry["path"]).resolve()
    return None


# ── address ──────────────────────────────────────────────────────────────────

def _worktree_main(repo: Path) -> Path | None:
    """<main checkout> for a git worktree dir, else None (a `.git` DIRECTORY is a main checkout)."""
    try:
        g = repo / ".git"
        if not g.is_file():
            return None
        line = g.read_text(encoding="utf-8", errors="replace").strip()
        if not line.startswith("gitdir:"):
            return None
        gd = line[len("gitdir:"):].strip().replace("\\", "/")
        if "/.git/worktrees/" not in gd:
            return None
        return Path(gd.split("/.git/worktrees/")[0]).resolve()
    except Exception:
        return None


def room_path(start: str | Path | None = None, *, name: str | None = None,
              scope: str | None = None) -> Path | None:
    """Resolve the room dir (the state dir) — ladder (spec S8 V1, ruling R8):
    explicit registry `name` → the ancestor walk from `start` (default cwd) →
    the registry room whose `scope == resolve_scope(start)` → None.

    The walk: nearest ancestor whose `.echelon/` holds room.json → else a
    `*.eos` file's `layout.state` (relative to the .eos dir) → else the git
    root's `.echelon/`. Returns None if no room EXISTS (init creates it).
    Never creates anything — a bare `.echelon/` dir without room.json is not a
    room (the bank's own `~/.echelon/` must not false-positive). An explicit
    `name` that is not registered returns None — it never falls through to cwd.
    """
    if name:
        entry = _registry_entries().get(name)
        return Path(entry["path"]).resolve() if entry and entry.get("path") else None
    start = Path(start).resolve() if start is not None else Path.cwd().resolve()
    cur = start if start.is_dir() else start.parent
    # 1. nearest ancestor whose room is built
    for d in _ancestors(cur):
        e = d / ".echelon"
        if (e / "room.json").exists():
            return e
    # 2. the .eos door — its layout.state names the room dir
    for d in _ancestors(cur):
        eos = _find_eos(d)
        if eos is not None:
            state = ((eos.get("layout") or {}).get("state")) or ".echelon/"
            room = (d / state).resolve()
            if (room / "room.json").exists():
                return room
            break  # a miss falls through to the registry (gate r1 S8a M-2)
    # 3. git root — an existing room there is still a room
    for d in _ancestors(cur):
        if (d / ".git").exists():
            e = d / ".echelon"
            if (e / "room.json").exists():
                return e
            # 3b. a git WORKTREE (`.git` is a FILE: "gitdir: <main>/.git/worktrees/<name>") has no
            # .echelon/ of its own (gitignored) — its room is the main checkout's room. Owner #932
            # (2026-09-02): <estate-root>/delta-shop-wt workers checkpointed nowhere.
            main = _worktree_main(d)
            if main is not None and (main / ".echelon" / "room.json").exists():
                return main / ".echelon"
            break  # a roomless repo is the COMMON case — never shadow leg 4
    # 4. registered-project binding (R-0154 slice 2, LAW 1): a roomless repo resolves to a room
    # ONLY when it is that room's own path or a REGISTERED PROJECT root of it — never a bare
    # scope match, which lands every scope-echelon repo in the echelon HOME room
    # ([[an-unbounded-upward-walk-lands-in-the-home-room]]). "no room" is a reachable answer.
    proj = _registry_room_by_project(cur)
    if proj is not None:
        return proj
    # 4b. LEGACY scope binding — kept ONLY when an explicit scope is passed (a caller that means
    # "the room owning this scope"), never for a bare roomless cwd (that is the home-room trap).
    if scope:
        return registry_room_by_scope(scope)
    return None


def _registry_room_by_project(cur: Path) -> Path | None:
    """The registered room a roomless cwd legitimately belongs to (LAW 1): the room whose OWN path
    is an ancestor of `cur`, or a room one of whose registered project roots is an ancestor of
    `cur`. Returns the room dir, or None (a roomless repo that is nobody's project has NO room)."""
    cur = Path(cur).resolve()
    for name, entry in _registry_entries().items():
        rdir = entry.get("path")
        if not rdir:
            continue
        repo_root = Path(rdir).resolve().parent   # <repo>/.echelon -> <repo>
        # this room's own repo (an ancestor of cur, but cur had no room.json in legs 1-3 because
        # e.g. the room dir is gitignored in a worktree already handled — belt-and-braces).
        if repo_root == cur or repo_root in cur.parents:
            return Path(rdir).resolve()
        # a registered project of this room whose root contains cur
        for pmeta in (entry.get("projects") or {}).values():
            proot = (repo_root / str(pmeta.get("root") or "")).resolve()
            if proot == cur or proot in cur.parents:
                return Path(rdir).resolve()
    return None


# ── init ─────────────────────────────────────────────────────────────────────

def init(root: Path, *, estate: str, type_: str = "engine",
         scope: str | None = None) -> Path:
    """Create <root>/.echelon/ with all drawers; refuses (RoomExists) if
    room.json exists; appends `.echelon/` to <root>/.gitignore."""
    root = Path(root).resolve()
    e = root / ".echelon"
    if (e / "room.json").exists():
        raise RoomExists(str(root))
    for sub in ("journal", "open", "closed", "incidents", "proposals", "index",
                "contracts", "changes/active", "changes/completed", "verification"):
        (e / sub).mkdir(parents=True, exist_ok=True)
    _write_json(e / "room.json", {
        "v": VERSION, "estate": estate, "type": type_,
        "scope": scope or _resolve_scope_new(root), "goal_ref": None, "services": [],
    })
    _write_json(e / "cursor.json", {
        "v": VERSION, "goal": None, "task": None, "stage": "init", "checkpoint": "",
        "next_actions": [], "blockers": [], "working_set": [],
        "recent_decisions": [], "updated": _now(),
    })
    _ensure_gitignore(root)
    # NOTE (spec S8 V1 deviation, 2026-08-29): init does NOT auto-register —
    # `workcycle register` is the explicit door into ~/.echelon/rooms.json.
    # Any code path that calls init (tests, tools, the self-gate mini-tree)
    # would otherwise write test/tmp rooms into the LIVE registry; an explicit
    # verb is the only pollution-proof registration.
    return e


def _ensure_gitignore(root: Path) -> None:
    """Append `.echelon/` to <root>/.gitignore if missing (create if absent)."""
    gi = root / ".gitignore"
    lines = []
    if gi.exists():
        try:
            lines = gi.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    if any(l.strip() == ".echelon/" for l in lines):
        return
    lines.append(".echelon/")
    gi.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── status ───────────────────────────────────────────────────────────────────

def _verify_violations(e: Path) -> int:
    # violation count from verification/latest.json (0 when absent/broken).
    # OK-STAMPING DEFECT (OPEN-0048): a bind seals ok:true when it has no ERROR-severity finding
    # (acked warnings ride along, INC-0003). But this counter used len(findings) — so an ok:true
    # bind carrying an acked/advisory warning reported violations>0, contradicting its own ok
    # stamp. Count only what actually BLOCKS: error-severity findings, plus warnings that were not
    # acked. Advisories and acked warnings do not count (they never made the receipt not-ok).
    data = _read_json(e / "verification" / "latest.json", {}) or {}
    findings = data.get("findings")
    if isinstance(findings, list):
        return sum(1 for f in findings if isinstance(f, dict) and (
            f.get("severity") == "error"
            or (f.get("severity") == "warning" and not f.get("acked"))))
    return _safe(lambda: int(data.get("violations", 0) or 0), 0)


def status(room: Path) -> dict:
    """One-line room status: cursor essentials + drawer counts + claims (file only)."""
    room = Path(room).resolve()
    e = Path(room).resolve()
    cursor = _read_json(e / "cursor.json", {}) or {}
    c = claims(e)
    return {
        "goal": cursor.get("goal"),
        "task": cursor.get("task"),
        "stage": cursor.get("stage"),
        "open": _count_json(e / "open"),
        "incidents": _count_json(e / "incidents"),
        "incidents_unresolved": len(unresolved_incidents(e)),
        "last_journal": _last_journal_line(_latest_journal_file(e / "journal")),
        "verify_violations": _verify_violations(e),
        "claims": {k: v.get("session") for k, v in c.items() if isinstance(v, dict)},
    }


# ── resume ───────────────────────────────────────────────────────────────────

def resume_brief(room: Path) -> str:
    """7-line operational snapshot (contract §4); never raises — a broken or
    missing room yields `ROOM unavailable (<reason>)`."""
    try:
        return _resume_brief(room)
    except Exception as exc:  # never block the turn
        return f"ROOM unavailable ({exc})"


def _resume_brief(room: Path) -> str:
    room = Path(room).resolve()
    e = Path(room).resolve()
    rj = _read_json(e / "room.json", {}) or {}
    if not rj:
        return "ROOM unavailable (no room.json)"
    cursor = _read_json(e / "cursor.json", {}) or {}

    goal = cursor.get("goal") or "-"
    task = cursor.get("task") or "-"
    stage = cursor.get("stage") or "-"
    checkpoint = cursor.get("checkpoint") or "-"
    next_actions = cursor.get("next_actions") or []
    if isinstance(next_actions, str):   # a string cursor (jump.py --next/--file) must NOT be iterated per character
        next_actions = [next_actions]
    ws = cursor.get("working_set") or []

    # CHANGE — active change records with their pending must_remove counts
    chg_parts: list[str] = []
    active = e / "changes" / "active"
    if active.is_dir():
        for p in sorted(active.glob("CHG-*.json")):
            d = _read_json(p, {}) or {}
            must = d.get("must_remove") or []
            intent = (d.get("intent") or p.stem)[:80]
            chg_parts.append(f"{p.stem} {intent} · must_remove pending: {len(must)}")
    change = " ; ".join(chg_parts) if chg_parts else "-"

    # DO NOT CREATE — canonical symbols from the index (working-domain filter is
    # a no-op until cursor carries a domain; canonical rows are the whole set)
    canonical: list[str] = []
    idx = e / "index"
    if idx.is_dir():
        for p in sorted(idx.glob("*.jsonl")):
            for row in _iter_jsonl(p):
                if row.get("canonical") and row.get("name"):
                    canonical.append(row["name"])
    dnc = ", ".join(canonical[:10]) if canonical else "-"

    # CONTRACTS — ids applicable to the working set (contracts.applicable)
    ids = _contracts.applicable(e, None, ws)
    contracts = ", ".join(ids[:8]) if ids else "-"

    n_open = _count_json(e / "open")
    n_inc = len(unresolved_incidents(e))
    n_verify = _verify_violations(e)
    n_claims = len(claims(e))

    open_line = (f"OPEN {n_open} items · INCIDENTS {n_inc} unresolved"
                 f" · VERIFY {n_verify} violations")
    if n_claims:
        open_line += f" · CLAIMED {n_claims}"
    return "\n".join([
        f"ROOM {rj.get('estate', '-')} · type {rj.get('type', '-')} · scope {rj.get('scope', '-')}",
        f"WHERE {goal} · {task}/{stage} · {checkpoint}",
        f"NEXT {' '.join(f'{i + 1}. {a}' for i, a in enumerate(next_actions)) if next_actions else '-'}",
        f"CHANGE {change}",
        f"DO NOT CREATE {dnc}",
        f"CONTRACTS {contracts}",
        open_line,
    ])


def resume_full(room: Path) -> str:
    """brief + working-set file list + last journal entry (verbatim JSON)."""
    brief = resume_brief(room)
    if brief.startswith("ROOM unavailable"):
        return brief
    room = Path(room).resolve()
    e = Path(room).resolve()
    cursor = _read_json(e / "cursor.json", {}) or {}
    ws = cursor.get("working_set") or []
    last = _last_journal_line(_latest_journal_file(e / "journal"))
    parts = [brief, "", "WORKING SET:"]
    parts += [f"  {w}" for w in ws] if ws else ["  -"]
    parts += ["JOURNAL:", json.dumps(last, ensure_ascii=False) if last else "-"]
    return "\n".join(parts)


# ── journal ──────────────────────────────────────────────────────────────────

def journal(room: Path, kind: str, data: dict) -> dict:
    """Append one line to journal/YYYY-MM-DD.jsonl; returns the record."""
    room = Path(room).resolve()
    jdir = Path(room).resolve() / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    record = {"v": VERSION, "ts": _now(), "kind": kind, **data}
    with open(jdir / f"{date.today().isoformat()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def record_verdict(room: Path, campaign: str, text: str) -> dict:
    """The first-class `verdict` kind (spec S8 V5, charter Part 7): a pilot /
    campaign step's ruling lives in the room's journal, not in a wrap atom —
    the wrap atom carries lessons only. Refuses an empty campaign or text."""
    if not (campaign or "").strip() or not (text or "").strip():
        raise ValueError("verdict needs a campaign and a text")
    return journal(room, "verdict", {"campaign": campaign.strip(), "text": text.strip()})


# ── checkpoint ───────────────────────────────────────────────────────────────

def _project_slug(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) else None


def _state_item(value: Any, project: str | None = None) -> dict:
    if isinstance(value, dict):
        return {"text": str(value.get("text") or ""), "project": _project_slug(value.get("project"))}
    return {"text": str(value or ""), "project": _project_slug(project)}


def checkpoint(room: Path, *, summary: str = "", completed: list[str] = (),
               remaining: list[str] = (), next_actions: list[Any] = (),
               blockers: list[Any] = (), files: list[str] = (),
               project: str | None = None, auto: bool = False, scope: str | None = None) -> dict:
    """cursor.json + journal line + SessionLedger checkpoint — one call, so the
    room and the bank never disagree. The ledger is best-effort, never fatal.

    auto=True: summary may be empty, missing `files` default to the ledger's
    files_touched() names, and the call NEVER raises — failures return
    {"ok": False, "err": ...}. Explicit calls raise on real failures.
    """
    try:
        next_values, blocker_values = list(next_actions), list(blockers)
        if project:
            next_values = [_state_item(v, project) for v in next_values]
            blocker_values = [_state_item(v, project) for v in blocker_values]
        return _checkpoint(room, summary=summary, completed=list(completed),
                           remaining=list(remaining), next_actions=next_values,
                           blockers=blocker_values, files=list(files),
                           auto=auto, scope=scope)
    except Exception as exc:
        if auto:
            return {"ok": False, "err": str(exc)}
        raise


def _checkpoint(room: Path, *, summary: str, completed: list[str],
                remaining: list[str], next_actions: list[str],
                blockers: list[str], files: list[str], auto: bool,
                scope: str | None, last_act: dict | None = None,
                updated: str | None = None, kind: str = "checkpoint") -> dict:
    if not summary and not auto:
        raise ValueError("checkpoint requires a summary (or auto=True)")
    room = Path(room).resolve()
    e = Path(room).resolve()
    if not (e / "room.json").exists():
        raise FileNotFoundError(f"no room at {room} (run `workcycle init`)")
    cursor = _read_json(e / "cursor.json", {}) or {}
    scope = scope or _room_scope(room)

    # Ledger — best-effort on every seam; the room write never waits on it.
    ledger = _safe(lambda: session_state.open_ledger(scope), None)
    ledger_files: list[str] = []
    decisions: list[str] = []
    if ledger is not None:
        if not files:
            ledger_files = _safe(
                lambda: [f.get("path", "") for f in ledger.files_touched(20)
                         if f.get("path")], [])
        _safe(lambda: ledger.checkpoint(summary,
                                        open_tasks=remaining if remaining else None), None)
        decisions = _safe(
            lambda: [d.get("conclusion", "") for d in ledger.recent_decisions(5)
                     if d.get("conclusion")], [])

    files = files if files else ledger_files
    working_set = [_norm_slash(f) for f in files]
    # UPSERT law (orchestrator heal 2026-08-27): an empty field never erases what a prior
    # checkpoint wrote — the Stop hook's auto checkpoint must not blank a manual one.
    # R-0138 (owner, board #991, 2026-09-02) "RECEIPTS MOVE THE ROOM, HANDS SET NEXT": a room
    # that has receipts owns its checkpoint text through fold() ALONE. A hand (the Stop hook,
    # jump.py, a manual `workcycle checkpoint`) sets next_actions + blockers and nothing else —
    # that is how the text stayed days stale while `updated` kept moving.
    hand_summary = summary
    summary = summary or str(cursor.get("checkpoint") or "")
    summary_ignored = ""
    if kind != "fold" and (e / "receipts.jsonl").exists():
        summary = str(cursor.get("checkpoint") or "")
        if hand_summary and hand_summary != summary:
            # Say so (board #2481/#2486, 2026-09-04): a worker whose --summary vanished
            # silently filed it as a tool bug. It is the law, not a cache.
            summary_ignored = ("R-0138: this room has receipts, so its checkpoint text is "
                               "owned by receipts (commit/board/route/items.py note); a hand "
                               "sets only next_actions/blockers. --summary was not applied.")
    if isinstance(next_actions, str):   # a bare string is ONE action; list(str) explodes it per character
        next_actions = [next_actions]
    next_actions = list(next_actions) or list(cursor.get("next_actions") or [])
    blockers = list(blockers) or list(cursor.get("blockers") or [])
    working_set = working_set or list(cursor.get("working_set") or [])
    # fold() (the receipt door, owner #932) passes last_act/updated so a folded cursor
    # still lands through THIS function — cursor.json keeps exactly one writer (doctor row 2).
    new_cursor = {
        "v": VERSION,
        "goal": cursor.get("goal"),
        "task": cursor.get("task"),
        "stage": cursor.get("stage"),
        "checkpoint": summary,
        "next_actions": next_actions,
        "blockers": blockers,
        "working_set": working_set,
        "recent_decisions": decisions or list(cursor.get("recent_decisions") or []),
        "updated": updated or _now(),
    }
    last_act = last_act if last_act is not None else cursor.get("last_act")
    if last_act:
        new_cursor["last_act"] = last_act
    _write_json(e / "cursor.json", new_cursor)
    # An auto checkpoint that carries nothing new (same summary, same files, no
    # completed/remaining) is not an event — it must not bury the journal under
    # repeats of the last manual line (129 identical lines measured 2026-08-29).
    cursor_moved = next_actions != list(cursor.get("next_actions") or []) \
        or blockers != list(cursor.get("blockers") or [])
    if auto and kind == "checkpoint" and not completed and not remaining and not cursor_moved:
        last = _last_journal_line(_latest_journal_file(e / "journal")) or {}
        if last.get("kind") == "checkpoint" and last.get("summary") == summary \
                and list(last.get("files") or []) == working_set:
            res = {"ok": True, "kind": "checkpoint", "auto": True,
                   "summary": summary, "deduped": True}
            if summary_ignored:
                res["summary_ignored"] = summary_ignored
            return res
    rec = journal(room, kind, {
        "auto": auto, "summary": summary, "files": working_set,
        "completed": completed, "remaining": remaining,
    })
    rec["ok"] = True
    if summary_ignored:
        rec["summary_ignored"] = summary_ignored
    return rec


# ── receipts: THE CURSOR IS A FOLD OF RECEIPTS (owner #932, 2026-09-02) ──────
# "room cursors are sometimes stale; when I direct another room's work from a
# different room it is not properly recorded." The Stop hook writes the cursor of
# the room it happens to STAND in — a cross-room act (a board row tagged [room], a
# run reaching a terminal state, an item opened/closed from another session, a
# route) moved nothing. So every such act now appends ONE receipt line to the
# TARGET room and folds its cursor. _checkpoint stays the only writer of
# cursor.json (doctor row 2); fold() goes through it like everyone else.

RECEIPT_KINDS = ("board_receipt", "run_state", "item_open", "item_close",
                 "ruling_acted", "route", "commit", "checkpoint", "project_backfill",
                 "child_promoted", "promoted_from_parent", "child_demoted",
                 # INCIDENT RESOLVE (OPEN-0109, 2026-09-06): closing an incident is an act with a
                 # receipt, not a hand-edit; ref = the INC id so a re-run dedupes.
                 "incident_resolved",
                 # STAFF role (OPEN-0074): a cadenced watcher's beat/refusal receipts — folded by
                 # the room pulse like any other receipt; the console staff strip counts them.
                 "staff_beat", "staff_skip",
                 # STALE SCAN (OPEN-0022, 2026-09-04): a check is an event — fresh+unchanged still
                 # stamps a receipt so freshness reads "checked N min ago" not the old stamp age;
                 # section_unmaintained records a stale section that a re-roll could not fix.
                 "check", "section_unmaintained")
FOLD_N = 5   # receipts summarised into the cursor's checkpoint line
# Bookkeeping kinds MOVE freshness (last_act/updated) but never NARRATE the checkpoint: a check
# says "nothing changed", a beat says "I clocked in". Folding them into the digest turned the
# gamma-support checkpoint into "verified checkpoint at 12:59 WIB, no change | verified
# next_acti..." (moderator 3ec72e59, 2026-09-04 13:3x) — the raw-record shape the owner named.
QUIET_KINDS = frozenset({"check", "section_unmaintained", "staff_beat", "staff_skip"})


def room_dir(room: str | Path) -> Path:
    """A registry NAME or a path -> the room dir (the .echelon state dir).

    The receipt doors are called from the console (board.py, runs.py,
    moderator.py) which knows rooms by NAME, and from the engine which knows
    them by PATH. One helper so neither side re-implements rooms.json."""
    if isinstance(room, Path):
        return room.resolve()
    s = str(room)
    if not s:
        raise ValueError("room is required")
    entry = _registry_entries().get(s)
    if entry and entry.get("path"):
        return Path(entry["path"]).resolve()
    p = Path(s)
    if (p / "room.json").exists():
        return p.resolve()
    if (p / ".echelon" / "room.json").exists():
        return (p / ".echelon").resolve()
    raise FileNotFoundError(f"no registered room {room!r}")


def receipts(room: str | Path, limit: int = 0) -> list[dict]:
    """The room's receipts, oldest→newest (last `limit` when limit > 0)."""
    e = room_dir(room)
    out = list(_iter_jsonl(e / "receipts.jsonl"))
    return out[-limit:] if limit else out


_RECEIPT_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def _receipt_lock(room):
    """Serialize receipt dedup, durable append and fold across participating writers."""
    with _RECEIPT_THREAD_LOCK:
        with (room / '.receipts.lock').open('a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            deadline = time.monotonic() + 10
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('receipt lock unavailable') from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def receipt(room: str | Path, kind: str, text: str, *, origin: str,
            ref: Any = None, project: str | None = None,
            fold_now: bool = True, at: str | None = None,
            strict_identity: bool = False) -> dict:
    """Append ONE receipt to <room>/receipts.jsonl and fold the room's cursor.

    A receipt is the atom of room state: WHAT happened (kind+text), WHO caused it
    (origin — the acting room/session/tool), and WHICH artefact it points at
    (ref — a board row n, a run id, an item id). Idempotent per (kind, ref) when
    ref is given: the board re-reads rows, and re-reading a row is not a new act.

    Never raises for a caller that is mid-act: an unwritable room costs a
    {"ok": False} not a traceback (the board must post even if a room dir is
    read-only — the same law route_to_room signs).

    `at` is the act's OWN timestamp, for a REPLAY of facts that already happened
    (the #932 backfill of 48h of board rows). Without it a replay would stamp
    every historical act with the backfill's wall clock — which would make the
    cursor lie about WHEN in the act of making it honest about WHAT. A fold
    orders receipts by file order and reads the newest one's ts, so a replay
    must be fed oldest-first."""
    try:
        if kind not in RECEIPT_KINDS:
            raise ValueError(f"kind must be one of {RECEIPT_KINDS}")
        e = room_dir(room)
        if not (e / "room.json").exists():
            raise FileNotFoundError(f"no room at {e}")
        rec = {"v": VERSION, "ts": str(at) if at else _now(),
               "kind": kind, "text": str(text or "")[:600],
               "origin": str(origin or "")[:120], "ref": ref,
               "project": _project_slug(project)}
        if strict_identity:
            rec["identity_sha256"] = hashlib.sha256(json.dumps(
                {"text": str(text or ""), "origin": str(origin or ""), "project": rec["project"]},
                sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        with _receipt_lock(e):
            path = e / 'receipts.jsonl'
            previous = []
            if path.exists():
                with path.open(encoding='utf-8') as stream:
                    for line_number, line in enumerate(stream, 1):
                        if not line.endswith('\n'):
                            return {"ok": False, "error_type": "ReceiptIncompleteRecord",
                                    "line": line_number, "retry_safe": False}
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise ValueError('receipt must be an object')
                            previous.append(row)
                        except (ValueError, TypeError):
                            return {"ok": False, "error_type": "ReceiptCorruption",
                                    "line": line_number, "retry_safe": False}
            if ref is not None:
                for prev in previous:
                    if prev.get("kind") == kind and prev.get("ref") == ref:
                        if strict_identity and any(prev.get(key) != rec.get(key)
                                                   for key in ("text", "origin", "project", "identity_sha256")):
                            return {"ok": False, "error_type": "ReceiptIdentityConflict",
                                    "retry_safe": False}
                        # A prior append may have lost its flush acknowledgement.
                        # Durable retry must flush again before claiming delivery.
                        with path.open("a", encoding="utf-8") as durable:
                            durable.flush()
                            os.fsync(durable.fileno())
                        out = {"ok": True, "deduped": True, "receipt": prev}
                        if fold_now:
                            out["fold"] = fold(e)
                        return out
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            out = {"ok": True, "receipt": rec}
            if fold_now:
                out["fold"] = fold(e)
            return out
    except Exception as exc:
        return {"ok": False, "err": str(exc)}


def _wib_stamp(ts: Any) -> str:
    """`MM-DDTHH:MM` in WIB (UTC+7) - the owner's ONE CLOCK law (console standards
    2026-09-02, board #1224: a 15:08 receipt read 08:08 because the digest sliced
    the raw UTC ISO string)."""
    d = _parse_ts(ts)
    if d is None:
        return str(ts or "")[5:16]
    return d.astimezone(timezone(timedelta(hours=7))).strftime("%m-%dT%H:%M")


def _receipt_line(r: dict) -> str:
    ref = r.get("ref")
    head = f"{_wib_stamp(r.get('ts'))} {r.get('kind')}"
    if ref not in (None, ""):
        head += f" {ref}"
    origin = r.get("origin") or ""
    if origin:
        head += f" <{origin}>"
    return f"{head}: {r.get('text') or ''}".strip()


def fold(room: str | Path) -> dict:
    """Recompute the cursor FROM the receipts — the whole point of A2.

    cursor.json keeps its shape (v/goal/task/stage/checkpoint/next_actions/
    blockers/working_set/recent_decisions/updated) and gains `last_act`
    {ts, kind, origin, ref, digest} — the newest receipt plus the last FOLD_N
    summarised, so a room whose work was directed from ANOTHER room still reads
    truthfully and `updated` is the timestamp of the act, not of a hook that
    happened to fire in the right cwd.

    R-0138 (owner, board #991, 2026-09-02) — "RECEIPTS MOVE THE ROOM, HANDS SET
    NEXT": the checkpoint text is DERIVED ONLY from receipts, each line stamped
    with its source, and hands (the Stop auto-checkpoint, jump.py) no longer
    rewrite it — they set next_actions and blockers only. So a fold DOES own
    `checkpoint`: it is the last FOLD_N receipts, and nothing else may claim it.
    Everything lands through _checkpoint (one writer, doctor row 2)."""
    e = room_dir(room)
    rs = receipts(e)
    if not rs:
        return {"ok": True, "folded": 0}
    newest = rs[-1]
    loud = [r for r in rs if r.get("kind") not in QUIET_KINDS] or rs
    digest = " | ".join(_receipt_line(r) for r in loud[-FOLD_N:])[:600]
    last_act = {"ts": newest.get("ts"), "kind": newest.get("kind"),
                "origin": newest.get("origin"), "ref": newest.get("ref"),
                "digest": digest, "project": _project_slug(newest.get("project"))}
    res = _checkpoint(e, summary=digest, completed=[], remaining=[],
                      next_actions=[], blockers=[], files=[], auto=True, scope=None,
                      last_act=last_act, updated=newest.get("ts") or _now(),
                      kind="fold")
    return {"ok": True, "folded": len(rs), "digest": digest,
            "last_act": last_act, "journal": bool(res)}


# ── type / snooze (charter Part 4) ───────────────────────────────────────────

def room_type(room: Path | None) -> str:
    """The room's declared TYPE (room.json.type); "" when there is no room."""
    if room is None:
        return ""
    rj = _read_json(Path(room).resolve() / "room.json", {}) or {}
    return str(rj.get("type") or "")


def set_type(room: Path, type_: str) -> dict:
    """Change the room's type; journaled (a type change is a decision)."""
    e = Path(room).resolve()
    rj = _read_json(e / "room.json", None)
    if not rj:
        raise FileNotFoundError(f"no room at {room}")
    old = rj.get("type")
    rj["type"] = type_
    _write_json(e / "room.json", rj)
    return journal(room, "type", {"event": "set", "old": old, "new": type_})


def _parse_ts(s: Any) -> datetime | None:
    """ISO-8601 -> aware datetime; None for anything else (naive = UTC)."""
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def snoozes(room: Path | None, now: str | None = None) -> list[dict]:
    """Live (unexpired) snooze entries. Expired ones are filtered on read, never
    rewritten here — reads must stay side-effect free for the hook. A missing or
    unparseable `until` counts as EXPIRED: a malformed entry must restore a guard,
    never remove one (gate M-2)."""
    if room is None:
        return []
    data = _read_json(Path(room).resolve() / "snooze.json", {}) or {}
    t_now = _parse_ts(now) or datetime.now(timezone.utc)
    live = []
    for s in (data.get("entries") or []):
        t = _parse_ts(s.get("until")) if isinstance(s, dict) else None
        if t is not None and t > t_now:
            live.append(s)
    return live


def snooze(room: Path, name: str, until: str, why: str = "") -> dict:
    """Mute a reflex (or type rule) until an ISO-8601 timestamp — never permanent.
    Replaces an existing entry of the same name; journaled."""
    if _parse_ts(until) is None:
        raise ValueError(f"snooze needs an ISO-8601 `until` timestamp, got {until!r} "
                         "(a mute without a real end is a hole)")
    e = Path(room).resolve()
    live = [s for s in snoozes(room) if s.get("name") != name]
    live.append({"name": name, "until": until, "why": why, "ts": _now()})
    _write_json(e / "snooze.json", {"v": VERSION, "entries": live})
    return journal(room, "snooze", {"event": "snooze", "name": name, "until": until, "why": why})


def unsnooze(room: Path, name: str | None = None) -> list[str]:
    """Lift a snooze by name, or every snooze when name is None; returns the
    names lifted. Journaled once per call."""
    e = Path(room).resolve()
    live = snoozes(room)
    keep = [s for s in live if name is not None and s.get("name") != name]
    lifted = [s["name"] for s in live if s not in keep]
    _write_json(e / "snooze.json", {"v": VERSION, "entries": keep})
    if lifted:
        journal(room, "snooze", {"event": "unsnooze", "names": lifted})
    return lifted


# ── open / close / incident ──────────────────────────────────────────────────

def open_item(room: Path, text: str, project: str | None = None) -> str:
    """Open an item; returns its id (OPEN-0001, incrementing across both
    open/ and closed/)."""
    room = Path(room).resolve()
    e = Path(room).resolve()
    o = e / "open"
    o.mkdir(parents=True, exist_ok=True)
    n = _next_id("OPEN", o, e / "closed")
    _write_json(o / f"{n}.json", {
        "v": VERSION, "id": n, "text": text, "opened": _now(),
        "closed": None, "note": None, "project": _project_slug(project),
    })
    # receipt door (owner #932): open_item takes an EXPLICIT room, so it is a
    # cross-room act by construction — the target room's cursor moves now.
    receipt(e, "item_open", f"{n} {text}", origin=_session_id(None), ref=n, project=project)
    return n


class UnverifiedClose(ValueError):
    """A claimed item closed by its own claimant with no independent verifier."""


def close_item(room: Path, item_id: str, note: str = "", *,
               bank: Path | None = None, verified_by: str = "",
               session: str = "", solo: bool = False) -> dict:
    """Close an open item: git-free move open/ -> closed/ with closed ts + note.

    Drops the item's claims.json key (the FILE is truth); with `bank`, also
    releases the row force=True best-effort — a closed item may leave a stale
    bank row, and that is the documented price of the split (files-are-truth).

    VERIFY-BY-OTHER (owner ruling 2026-09-01, mined from agent-room; the
    delegate-and-gate law as ledger protocol): a CLAIMED item self-closed by
    its claimant needs an independent `verified_by` (a different session, a
    gate, or the owner) — or an explicit `solo=True`, which records the close
    as declared-unverified rather than silently trusted. Unclaimed items close
    as before; `verified_by`/`closed_by` are recorded whenever given."""
    if not re.fullmatch(r"OPEN-\d+", item_id or ""):
        raise ValueError(f"bad item id {item_id!r}")
    room = Path(room).resolve()
    e = Path(room).resolve()
    src = e / "open" / f"{item_id}.json"
    if not src.exists():
        raise FileNotFoundError(f"no open item {item_id}")
    claimant = str((_read_json(e / "claims.json", {}) or {}).get(item_id, {}).get("session", ""))
    if claimant and not verified_by and not solo and (not session or session == claimant):
        raise UnverifiedClose(
            f"{item_id} is claimed by {claimant} — a self-close needs --verified-by "
            "<other session|gate|owner>, or --solo to record it declared-unverified")
    item = _read_json(src, None) or {}
    item["v"] = VERSION
    item["closed"] = _now()
    item["note"] = note
    if session:
        item["closed_by"] = session
    if verified_by:
        item["verified_by"] = verified_by
    elif claimant and session and session != claimant:
        item["verified_by"] = session          # a different session closing IS the other seat
    elif claimant:
        item["verified_by"] = "SOLO-DECLARED"  # explicit --solo: unverified, and says so
    else:
        item["verified_by"] = ""
    _write_json(e / "closed" / f"{item_id}.json", item)
    src.unlink()
    data = _read_json(e / "claims.json", {}) or {}
    if item_id in data:
        del data[item_id]
        _write_json(e / "claims.json", data)
    if bank is not None:
        try:
            release(room, item_id, session="", bank=bank, force=True)
        except Exception:
            pass  # best-effort: the file drop already happened; the row may linger
    receipt(e, "item_close", f"{item_id} closed{': ' + note if note else ''}",
            origin=session or _session_id(None), ref=item_id)
    return item


# ── claims: bank row + claims.json backpointer, ONE transaction (R-0008) ─────

def _bank_path(explicit: str | Path | None) -> Path:
    """The one bank home: explicit --bank, else $ECHELON_BANK, else the live bank."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("ECHELON_BANK")
    return Path(env) if env else _DEFAULT_DB


def _session_id(explicit: str | None) -> str:
    """Claimant identity: --session, else $ECHELON_SESSION, else user@host:ppid."""
    if explicit:
        return explicit
    env = os.environ.get("ECHELON_SESSION")
    return env if env else f"{getpass.getuser()}@{socket.gethostname()}:{os.getppid()}"


def _claims_table(conn: sqlite3.Connection) -> None:
    """room_claims schema — the partial unique index IS the cross-process lock."""
    conn.execute("CREATE TABLE IF NOT EXISTS room_claims(room TEXT, open_id TEXT,"
                 " session TEXT, ts TEXT, released TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS room_claims_active"
                 " ON room_claims(room, open_id) WHERE released IS NULL")
    # pid column (owner 2026-09-01, soft-claim liveness) — guarded add for existing banks
    if "pid" not in {r[1] for r in conn.execute("PRAGMA table_info(room_claims)")}:
        conn.execute("ALTER TABLE room_claims ADD COLUMN pid INTEGER")


def claim(room: Path, item_id: str, *, session: str, bank: Path,
          pid: int | None = None, steal_dead: bool = False) -> dict:
    """Claim an open item for a session: bank row + claims.json backpointer in
    ONE BEGIN IMMEDIATE transaction (the partial unique index is the real lock).
    Held by another session -> ClaimHeld (rollback); acquisition busy/locked ->
    ClaimBusy with no holder or acknowledgement; same session -> idempotent;
    a claims.json write failure rolls the row back — never a row without a pointer.

    Soft-claim liveness (owner 2026-09-01): a claim is a MARK, not a freeze. `pid`
    is the WORKER's process id stamped on the claim; a refused claimant gets the
    holder's pid + liveness in ClaimHeld, and `steal_dead=True` takes over a claim
    whose pid is verifiably DEAD (the stolen row is released with a note, in the
    same transaction — the file stays truth)."""
    if not re.fullmatch(r"OPEN-\d+", item_id or ""):
        raise ValueError(f"bad item id {item_id!r}")
    e = Path(room).resolve()
    if not (e / "open" / f"{item_id}.json").exists():
        raise FileNotFoundError(f"no open item {item_id}")
    estate = (_read_json(e / "room.json", {}) or {}).get("estate") or e.parent.name
    conn = sqlite3.connect(str(bank), timeout=5)
    conn.isolation_level = None
    stolen = None
    try:
        _begin_claim_transaction(conn, "claim")
        _claims_table(conn)
        row = conn.execute("SELECT session, ts, pid FROM room_claims"
                           " WHERE room=? AND open_id=? AND released IS NULL",
                           (estate, item_id)).fetchone()
        if row and row[0] != session:
            alive = _pid_alive(row[2])
            if not (steal_dead and alive is False):
                raise ClaimHeld(row[0], row[1], pid=row[2], alive=alive)
            conn.execute("UPDATE room_claims SET released=? WHERE room=? AND open_id=?"
                         " AND released IS NULL", (f"{_now()} stolen-dead by {session}", estate, item_id))
            stolen = {"session": row[0], "ts": row[1], "pid": row[2]}
            row = None
        if row:
            ts = row[1]
        else:
            ts = _now()
            conn.execute("INSERT INTO room_claims(room, open_id, session, ts, released, pid)"
                         " VALUES(?,?,?,?,NULL,?)", (estate, item_id, session, ts, pid))
        data = _read_json(e / "claims.json", {}) or {}
        prev = data.get(item_id) if isinstance(data.get(item_id), dict) else {}
        # idempotent re-claim without --pid keeps the stamped pid; a new pid refreshes it
        data[item_id] = {**prev, "session": session, "ts": ts, **({"pid": int(pid)} if pid else {})}
        _write_json(e / "claims.json", data)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    out = {"id": item_id, "session": session, "ts": ts, "room": estate, "held": True}
    if pid:
        out["pid"] = int(pid)
    if stolen:
        out["stole_dead_claim"] = stolen
    return out


def release(room: Path, item_id: str, *, session: str, bank: Path,
            force: bool = False) -> dict:
    """Release a claim: released=now + the claims.json key drop in ONE transaction.
    Held by another session -> ClaimHeld unless force; acquisition busy/locked ->
    ClaimBusy with no holder or acknowledgement. No active row -> the no-op
    {"held": False} — the FILE stays truth, never silently dropped."""
    if not re.fullmatch(r"OPEN-\d+", item_id or ""):
        raise ValueError(f"bad item id {item_id!r}")
    e = Path(room).resolve()
    estate = (_read_json(e / "room.json", {}) or {}).get("estate") or e.parent.name
    conn = sqlite3.connect(str(bank), timeout=5)
    conn.isolation_level = None
    try:
        _begin_claim_transaction(conn, "release")
        _claims_table(conn)
        row = conn.execute("SELECT session, ts FROM room_claims"
                           " WHERE room=? AND open_id=? AND released IS NULL",
                           (estate, item_id)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return {"held": False}
        if row[0] != session and not force:
            raise ClaimHeld(row[0], row[1])
        conn.execute("UPDATE room_claims SET released=? WHERE room=? AND open_id=?"
                     " AND released IS NULL", (_now(), estate, item_id))
        data = _read_json(e / "claims.json", {}) or {}
        if item_id in data:
            del data[item_id]
            _write_json(e / "claims.json", data)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"id": item_id, "session": row[0], "ts": row[1], "room": estate, "held": True}


def claims(room: Path) -> dict:
    """claims.json — FILE read only (boot/resume never open the db), annotated with
    pid liveness where a pid was stamped (alive: true/false; absent = no pid)."""
    data = _read_json(Path(room).resolve() / "claims.json", {}) or {}
    for entry in data.values():
        if isinstance(entry, dict) and entry.get("pid"):
            alive = _pid_alive(entry["pid"])
            if alive is not None:
                entry["alive"] = alive
    return data


def incident(room: Path, what: str, *, evidence: str = "", fix: str = "",
             receipt: str = "", project: str | None = None) -> str:
    """File an incident; returns its id (INC-0001, incrementing)."""
    room = Path(room).resolve()
    e = Path(room).resolve()
    inc = e / "incidents"
    inc.mkdir(parents=True, exist_ok=True)
    n = _next_id("INC", inc)
    _write_json(inc / f"{n}.json", {
        "v": VERSION, "id": n, "what": what, "opened": _now(),
        "evidence": evidence, "fix": fix, "receipt": receipt, "closed": None,
        "project": _project_slug(project),
    })
    return n


class IncidentRefused(Exception):
    """A resolve that must not happen (no such incident / already resolved)."""


def _incident_is_resolved(d: dict) -> bool:
    return bool((d or {}).get("closed") or (d or {}).get("resolved"))


def unresolved_incidents(room: str | Path) -> list[dict]:
    """Incident records with no `closed` stamp and no `resolved` block, oldest id first."""
    inc = Path(room).resolve() / "incidents"
    out = []
    for p in sorted(inc.glob("INC-*.json")) if inc.is_dir() else []:
        d = _read_json(p, {}) or {}
        if d.get("id") and not _incident_is_resolved(d):
            out.append(d)
    return out


def incident_resolve(room: str | Path, inc_id: str, *, by: str, note: str = "",
                     origin: str = "", board: bool = True) -> dict:
    """Resolve an incident (OPEN-0109): stamp the record, write ONE `incident_resolved`
    receipt (ref = the INC id, so a re-run is a no-op), post a board row.

    `by` names the item / run / commit that closed it (an incident resolved by nothing
    is a claim, not a resolution). Refuses a missing or already-resolved incident so the
    room never carries two resolutions of one incident."""
    e = Path(room).resolve()
    inc_id = str(inc_id).strip().upper()
    if not re.fullmatch(r"INC-\d{4}", inc_id):
        raise IncidentRefused(f"not an incident id: {inc_id}")
    if not str(by or "").strip():
        raise IncidentRefused("--by is required: name the item/run/commit that resolved it")
    path = e / "incidents" / f"{inc_id}.json"
    d = _read_json(path, None) if path.exists() else None
    if not d:
        raise IncidentRefused(f"no such incident {inc_id} in {e.name}")
    if _incident_is_resolved(d):
        prev = d.get("resolved") or {}
        raise IncidentRefused(f"{inc_id} already resolved"
                              f"{' by ' + str(prev.get('by')) if prev.get('by') else ''}"
                              f" at {d.get('closed') or prev.get('at')}")
    ts = _now()
    d["closed"] = ts
    d["resolved"] = {"by": str(by).strip()[:120], "note": str(note or "")[:600],
                     "at": ts, "origin": str(origin or "")[:120]}
    _write_json(path, d)
    text = f"{inc_id} resolved by {d['resolved']['by']}" + (f": {d['resolved']['note']}" if note else "")
    rcpt = receipt(e, "incident_resolved", text, origin=origin or "workcycle",
                   ref=inc_id, project=d.get("project"))
    room_name = (_read_json(e / "room.json", {}) or {}).get("name") or e.parent.name
    board_line = f"[{room_name}] INCIDENT {inc_id} RESOLVED by {d['resolved']['by']}"
    if note:
        board_line += f" — {d['resolved']['note'][:300]}"
    board_line += f" · {len(unresolved_incidents(e))} unresolved left"
    if board:
        _board_post(board_line)
    return {"ok": True, "id": inc_id, "resolved": d["resolved"], "receipt": rcpt,
            "board_line": board_line, "unresolved": len(unresolved_incidents(e))}


def assign_project(room: str | Path, kind: str, ref: str | int, project: str | None) -> dict:
    """Backfill a room object through the room/cursor writer."""
    e = room_dir(room)
    project = _project_slug(project)
    estate = (_read_json(e / "room.json", {}) or {}).get("estate") or e.parent.name
    projects = (_registry_entries().get(estate) or {}).get("projects") or {}
    if project is not None and project not in projects:
        raise ValueError(f"unknown project {estate}/{project}")
    if kind == "blocker":
        cursor = _read_json(e / "cursor.json", {}) or {}
        values = list(cursor.get("blockers") or [])
        index = int(ref)
        if index < 0 or index >= len(values):
            raise IndexError(index)
        updated = _state_item(values[index])
        if updated.get("project") == project:
            return {"ok": True, "changed": False}
        updated["project"] = project
        values[index] = updated
        _checkpoint(e, summary="", completed=[], remaining=[], next_actions=[], blockers=values,
                    files=[], auto=True, scope=None)
    else:
        sub = {"open": "open", "closed": "closed", "inbox": "inbox", "incident": "incidents"}.get(kind)
        if not sub:
            raise ValueError("kind must be open|closed|inbox|incident|blocker")
        path = e / sub / f"{ref}.json"
        item = _read_json(path, None)
        if item is None:
            raise FileNotFoundError(path)
        if _project_slug(item.get("project")) == project:
            return {"ok": True, "changed": False}
        item["project"] = project
        _write_json(path, item)
    receipt(e, "project_backfill", f"{kind}:{ref} -> {project or 'no-project'}",
            origin="project_backfill", ref=f"{kind}:{ref}:{project or '-'}", project=project)
    return {"ok": True, "changed": True, "project": project}


# ── move: re-home an item/ruling/note between rooms (R-0154 slice 1) ──────────
#
# Laws this verb signs:
#  - ITEMS re-number in the TARGET room's sequence; the source file becomes an
#    ALIAS TOMBSTONE {v, id, moved_to:"OPEN-yyyy@<room>", ts, by} — never
#    deleted, so every literal-path reader can resolve it forward.
#  - RULINGS never re-number: an R-id is estate-wide. A move only RETAGS the
#    ruling's room field through the board API (never a file edit) + a `say`.
#  - NOTES (inbox NOTE-nnnn.json) ride to the target inbox, re-numbered, with the
#    same tombstone at the source.
#  - IDEMPOTENT: moving something whose source is already a tombstone is a no-op
#    that reports where it went.
#  - A receipt lands in BOTH rooms (kind `route`). The ONE board row is posted by
#    the caller (the CLI / tools), not by this pure function — so a test never
#    hits the network.

_ALIAS_RE = re.compile(r"^(?P<id>[A-Z]+-\d+)@(?P<room>[A-Za-z0-9][A-Za-z0-9._/-]*)$")


def _item_drawers(kind_prefix: str) -> tuple[str, ...]:
    """The drawers an id of this prefix can live in (a move searches all)."""
    return {"OPEN": ("open", "closed"), "INC": ("incidents",),
            "NOTE": ("inbox",)}.get(kind_prefix, ("open",))


def _id_prefix(item_id: str) -> str:
    m = re.match(r"^([A-Z]+)-\d+$", item_id or "")
    return m.group(1) if m else ""


def _locate_record(room: Path, item_id: str) -> tuple[Path, dict] | None:
    """First (path, record) for `item_id` across its drawers in `room`, else None."""
    e = Path(room).resolve()
    for sub in _item_drawers(_id_prefix(item_id)):
        p = e / sub / f"{item_id}.json"
        if p.exists():
            return p, (_read_json(p, {}) or {})
    return None


def is_tombstone(record: dict) -> bool:
    return isinstance(record, dict) and bool(record.get("moved_to"))


def resolve_alias(room: str | Path, item_id: str, *, _seen: set | None = None) -> dict:
    """Follow a `moved_to` chain to the live record — the one door every reader
    uses so a moved id never dangles.

    Returns {"id", "room", "path", "record", "hops"}: the FINAL live id, the room
    it now lives in (a name when it crossed rooms, else the given room), the file,
    the record, and how many tombstones were followed. A record that is not a
    tombstone resolves to itself (hops 0). A broken/cyclic chain returns the last
    tombstone reached with `"dangling": True` rather than raising — a reader must
    degrade, never crash."""
    _seen = _seen or set()
    start = room_dir(room)
    found = _locate_record(start, item_id)
    if found is None:
        return {"id": item_id, "room": str(room), "path": None, "record": None,
                "hops": len(_seen), "dangling": True}
    path, rec = found
    if not is_tombstone(rec):
        return {"id": item_id, "room": str(room), "path": path, "record": rec,
                "hops": len(_seen)}
    key = f"{item_id}@{start}"
    if key in _seen:
        return {"id": item_id, "room": str(room), "path": path, "record": rec,
                "hops": len(_seen), "dangling": True}
    _seen.add(key)
    m = _ALIAS_RE.match(str(rec.get("moved_to") or ""))
    if not m:
        return {"id": item_id, "room": str(room), "path": path, "record": rec,
                "hops": len(_seen), "dangling": True}
    nxt_id, nxt_room = m.group("id"), m.group("room")
    try:
        nxt_dir = room_dir(nxt_room)
    except (FileNotFoundError, ValueError):
        return {"id": item_id, "room": str(room), "path": path, "record": rec,
                "hops": len(_seen), "dangling": True}
    return resolve_alias(nxt_dir, nxt_id, _seen=_seen)


class MoveRefused(RuntimeError):
    """A move that cannot proceed (unknown id, ruling-move with no server, …)."""


def move_item(source: str | Path, item_id: str, target: str | Path, *,
              by: str = "", board_row: bool = False) -> dict:
    """Re-home an OPEN/INC/NOTE record from `source` room to `target` room.

    Allocates a NEW id in the target's sequence, writes the record there (with
    `moved_from` provenance), turns the source file into an alias tombstone,
    drops any claim on the source id, and receipts BOTH rooms. Idempotent: if the
    source is already a tombstone, returns {"already": True, "moved_to": ...}.
    `board_row` is a hint the caller reads to post the ONE board row itself."""
    src_dir = room_dir(source)
    tgt_dir = room_dir(target)
    prefix = _id_prefix(item_id)
    if prefix not in ("OPEN", "INC", "NOTE"):
        raise MoveRefused(f"move_item handles OPEN/INC/NOTE ids, got {item_id!r}")
    if src_dir == tgt_dir:
        raise MoveRefused(f"source and target are the same room ({src_dir})")
    found = _locate_record(src_dir, item_id)
    if found is None:
        raise MoveRefused(f"no {item_id} in {src_dir}")
    src_path, rec = found
    if is_tombstone(rec):
        return {"ok": True, "already": True, "id": item_id,
                "moved_to": rec.get("moved_to"), "source": str(src_dir)}

    drawer = src_path.parent.name              # open|closed|incidents|inbox
    tgt_drawer = tgt_dir / drawer
    tgt_drawer.mkdir(parents=True, exist_ok=True)
    # NEW id: OPEN spans open+closed; INC over incidents; NOTE over inbox.
    if prefix == "OPEN":
        new_id = _next_id("OPEN", tgt_dir / "open", tgt_dir / "closed")
    else:
        new_id = _next_id(prefix, tgt_drawer)

    tgt_room_name = _room_name_for(tgt_dir)
    src_room_name = _room_name_for(src_dir)
    moved = dict(rec)
    moved["id"] = new_id
    moved["moved_from"] = f"{item_id}@{src_room_name}"
    _write_json(tgt_drawer / f"{new_id}.json", moved)

    ts = _now()
    tombstone = {"v": VERSION, "id": item_id,
                 "moved_to": f"{new_id}@{tgt_room_name}",
                 "ts": ts, "by": by or _session_id(None)}
    _write_json(src_path, tombstone)

    # drop a live claim on the source id (the file is truth; a moved item is not
    # held in the old room any more).
    claims_path = src_dir / "claims.json"
    cdata = _read_json(claims_path, {}) or {}
    if item_id in cdata:
        del cdata[item_id]
        _write_json(claims_path, cdata)

    text = f"{item_id} -> {tgt_room_name} {new_id} (moved, R-0154)"
    src_rcpt = receipt(src_dir, "route", text, origin=by or _session_id(None), ref=item_id)
    tgt_rcpt = receipt(tgt_dir, "route", f"{new_id} <- {src_room_name} {item_id} (moved, R-0154)",
                       origin=by or _session_id(None), ref=new_id)
    return {"ok": True, "id": item_id, "new_id": new_id,
            "source": src_room_name, "target": tgt_room_name, "drawer": drawer,
            "moved_to": f"{new_id}@{tgt_room_name}", "ts": ts,
            "board_line": f"[echelon] {item_id} -> {tgt_room_name} {new_id} (moved, R-0154)",
            "src_receipt": src_rcpt.get("ok"), "tgt_receipt": tgt_rcpt.get("ok")}


def _room_name_for(room_dir_path: Path) -> str:
    """The registry NAME whose path is this room dir, else the repo dir name."""
    rp = str(Path(room_dir_path).resolve()).replace("\\", "/")
    for name, entry in _registry_entries().items():
        if str(Path(entry.get("path", "")).resolve()).replace("\\", "/") == rp:
            return name
    return Path(room_dir_path).parent.name


def move_ruling(rid: str, target: str, *, by: str = "", retag_fn=None, say_fn=None) -> dict:
    """Re-home a ruling to `target` room. A ruling NEVER re-numbers (an R-id is
    estate-wide) — the move only RETAGS the ruling's room through the board API
    and records a `say`. Both API calls are INJECTED (`retag_fn(rid, target)`,
    `say_fn(rid, text, frm)`) so a test drives them without a live server; the
    CLI/tools wire the real board client. Returns the retag result + say ok."""
    if not re.match(r"^R-\d+$", rid or ""):
        raise MoveRefused(f"move_ruling handles R-ids, got {rid!r}")
    if retag_fn is None:
        raise MoveRefused("move_ruling needs a retag_fn bound to the board API "
                          "(a ruling's room is retagged through /api/rulings, never a file edit)")
    result = retag_fn(rid, target)
    said = None
    if say_fn is not None:
        said = say_fn(rid, f"moved to room {target} (R-0154)", by or "claude")
    return {"ok": True, "id": rid, "target": target, "retag": result,
            "said": bool(said) if say_fn is not None else None,
            "board_line": f"[echelon] {rid} retagged -> {target} (moved, R-0154)"}


# ── doctor ───────────────────────────────────────────────────────────────────

STATE_KINDS = {"session-wrap-": "wrap", "index-rollup": "rollup", "OPEN-": "open-item"}
DISPUTABLE = ("wrap", "rollup")   # pure state by construction; OPEN-* carry lessons -> owner judgment


def import_atoms(room: Path, root: Path, *, apply: bool = False, dispute_fn=None) -> dict:
    """The charter Part 2 migration sweep: the STATE half of wrap/rollup/OPEN atoms moves
    into the room (journal/imported/<name>.md, verbatim), a manifest lists them, and the
    pure-state kinds are disputed in the bank ONLY with apply=True. Never deletes a source
    file; re-runs are idempotent (same bytes overwrite)."""
    room, root = Path(room).resolve(), Path(root).resolve()
    dest = room / "journal" / "imported"
    dest.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for p in sorted(root.glob("*.md")):
        kind = next((k for pre, k in STATE_KINDS.items() if p.name.startswith(pre)), None)
        if not kind:
            continue
        (dest / p.name).write_bytes(p.read_bytes())
        row = {"name": p.stem, "kind": kind, "bytes": p.stat().st_size,
               "dispute": kind in DISPUTABLE, "disputed": False}
        if apply and row["dispute"]:
            try:
                (dispute_fn or _dispute_atom)(p.stem, "charter Part 2 migration: state moved to the room journal/imported/")
                row["disputed"] = True
            except Exception as exc:  # a bank refusal is a row, not a crash
                row["error"] = str(exc)[:160]
        rows.append(row)
    manifest = {"v": VERSION, "ts": _now(), "root": str(root), "apply": apply, "rows": rows}
    _write_json(dest / "_manifest.json", manifest)
    journal(room, "import-atoms", {"root": str(root), "apply": apply, "imported": len(rows),
                                   "disputed": sum(1 for r in rows if r["disputed"]),
                                   "dispute_pending": sum(1 for r in rows if r["dispute"] and not r["disputed"])})
    return manifest


def _dispute_atom(name: str, reason: str) -> None:
    from echelon_engine.atoms.correct import dispute
    dispute(name, reason)


def doctor(room: Path) -> list[dict]:
    """Contract §8 rows 1, 2, 5, 12 + row 9 (contracts drawer) as {row, ok,
    evidence} — evidence, never prose. Rows 5/12 mutate the room as they
    verify, then restore it."""
    room = Path(room).resolve()
    return [_row1(room), _row2(room), _row5(room), _row12(room)] + \
        _contracts.doctor(room)


def _row1(room: Path) -> dict:
    """1. room address agrees: room_path == the .eos layout.state dir == room."""
    room = Path(room).resolve()
    rp = room_path(str(room))
    ok = rp is not None and rp == room
    evidence = f"room_path={rp}"
    # the estate root's .eos (nearest one above the room) must name this dir
    for d in _ancestors(room.parent):
        eos = _find_eos(d)
        if eos is not None:
            state = ((eos.get("layout") or {}).get("state")) or ".echelon/"
            target = (d / state).resolve()
            evidence += f"; .eos layout.state={state!r} -> {target}"
            ok = ok and target == room
            break
    return {"row": 1, "ok": bool(ok), "evidence": evidence}


def _row2(room: Path) -> dict:
    """2. one writer per file: grep this package for cursor.json writers —
    exactly this module, and inside it exactly _checkpoint().

    The second leg is owner #932's guard: fold() recomputes the cursor from
    receipts, and the cheap way to write it would be a second _write_json here.
    That would give cursor.json three writers inside one file — which the
    file-level grep cannot see. So the row also counts the `_write_json(...
    "cursor.json")` call sites in THIS module: exactly TWO — init() creates it,
    _checkpoint() updates it, and nothing else touches it."""
    pkg = Path(__file__).resolve().parent
    writers: list[str] = []
    for p in sorted(pkg.glob("*.py")):
        src = _safe(lambda: p.read_text(encoding="utf-8"), "")
        if "cursor.json" in src and re.search(r"open\s*\(|write_text\s*\(", src):
            writers.append(p.name)
    mine = _safe(lambda: Path(__file__).resolve().read_text(encoding="utf-8"), "")
    sites = len(re.findall(r'_write_json\(\s*e\s*/\s*"cursor\.json"', mine))
    ok = writers == ["workcycle.py"] and sites == 2
    return {"row": 2, "ok": ok,
            "evidence": f"cursor.json writers: {writers}; in-module write sites: {sites} "
                        f"(want 2: init creates, _checkpoint updates)"}


def _row5(room: Path) -> dict:
    """5. Stop checkpoint: a simulated Stop payload advances cursor.json.updated
    and appends one journal line, within 2s."""
    e = Path(room).resolve()
    before = _read_json(e / "cursor.json", {}) or {}
    n_before = _journal_lines(e / "journal")
    t0 = time.monotonic()
    res = checkpoint(room, auto=True, summary="")
    elapsed = (time.monotonic() - t0) * 1000
    after = _read_json(e / "cursor.json", {}) or {}
    n_after = _journal_lines(e / "journal")
    ok = bool(res.get("ok")) and after.get("updated") != before.get("updated") \
        and n_after == n_before + 1 and elapsed <= 2000
    evidence = (f"updated {before.get('updated')} -> {after.get('updated')} · "
                f"journal +{n_after - n_before} · {elapsed:.0f}ms")
    return {"row": 5, "ok": ok, "evidence": evidence}


def _row12(room: Path) -> dict:
    """12. version skew: cursor.json with v:99 makes every reader print the one
    skip line (on STDERR — stdout belongs to the hook protocol) and continue.
    Restores the original cursor afterwards."""
    e = Path(room).resolve()
    cp = e / "cursor.json"
    original = cp.read_text(encoding="utf-8") if cp.exists() else None
    out = io.StringIO()
    try:
        _write_json(cp, {"v": 99, "goal": None})
        with contextlib.redirect_stderr(out):
            brief = resume_brief(room)
        printed = out.getvalue().strip()
        ok = "unsupported, skipped" in printed and brief.startswith("ROOM ")
    except Exception as exc:
        return {"row": 12, "ok": False, "evidence": f"raised {exc}"}
    finally:
        if original is not None:
            cp.write_text(original, encoding="utf-8")
        else:
            cp.unlink()
    line = printed.splitlines()[0] if printed else "(no skip line)"
    return {"row": 12, "ok": bool(ok), "evidence": f"v99 -> {line}"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _board_post(text: str) -> None:
    """Post ONE board row through the estate's board.py door (best-effort — a
    move already committed to disk; a down board must not undo it)."""
    try:
        import importlib.util
        estate = registry_entry("echelon").get("path")
        if not estate:
            return
        board_py = Path(estate).parent / "tools" / "board.py"
        if not board_py.exists():
            return
        spec = importlib.util.spec_from_file_location("_echelon_board", board_py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.post(text, frm="claude")
    except Exception as exc:
        print(f"workcycle move: board row not posted ({exc}); the move is on disk", file=sys.stderr)


def _board_module():
    """Load the estate board.py as a module (retag/say for a ruling move)."""
    import importlib.util
    estate = registry_entry("echelon").get("path")
    if not estate:
        raise MoveRefused("no echelon room registered — cannot reach the board API for a ruling move")
    board_py = Path(estate).parent / "tools" / "board.py"
    if not board_py.exists():
        raise MoveRefused(f"board.py not found at {board_py}")
    spec = importlib.util.spec_from_file_location("_echelon_board", board_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cli_move(args) -> int:
    prefix = _id_prefix(args.id)
    by = args.by or _session_id(None)
    if prefix == "R":
        if args.dry_run:
            print(f"move {args.id} -> room {args.to} (RETAG via /api/rulings, no renumber); "
                  f"say noted. ZERO bytes written (dry run)")
            return 0
        try:
            mod = _board_module()
            res = move_ruling(args.id, args.to, by=by,
                              retag_fn=lambda rid, room: mod.ruling_move(rid, room),
                              say_fn=lambda rid, text, frm: mod.ruling_event(rid, "say", text, frm=frm))
        except Exception as exc:
            print(f"workcycle: move refused: {exc}")
            return 1
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0

    # OPEN / INC / NOTE — a room move.
    src = room_path(name=args.from_room) if args.from_room else room_path()
    if src is None:
        print("workcycle move: no source room (run from a room, or pass --from <room>)")
        return 1
    try:
        tgt = room_dir(args.to)
    except FileNotFoundError as exc:
        print(f"workcycle: move refused: {exc}")
        return 1
    if args.dry_run:
        found = _locate_record(src, args.id)
        if found is None:
            print(f"workcycle: move refused: no {args.id} in {src}")
            return 1
        _, rec = found
        if is_tombstone(rec):
            print(f"{args.id} is already a tombstone -> {rec.get('moved_to')} (no-op)")
            return 0
        drawer = _locate_record(src, args.id)[0].parent.name
        print(f"move {args.id} ({drawer}) {_room_name_for(src)} -> {_room_name_for(tgt)}: "
              f"new id via target sequence, source becomes a tombstone, receipt in both rooms, "
              f"one board row. ZERO bytes written (dry run)")
        return 0
    try:
        res = move_item(src, args.id, tgt, by=by)
    except MoveRefused as exc:
        print(f"workcycle: move refused: {exc}")
        return 1
    if res.get("already"):
        print(f"{args.id} already moved -> {res['moved_to']} (idempotent no-op)")
        return 0
    if not args.no_board:
        _board_post(res["board_line"])
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: init|status|resume [--brief]|checkpoint [--auto] [--summary S]
    [--next N]* [--file F]*|journal <kind> <json>|open <text>|close <id>
    [--note N]|incident <what>|doctor"""
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon workcycle",
        description="The ROOM verb — state of WORK at <repo>/.echelon/")
    sub = ap.add_subparsers(dest="action", required=True)

    p_init = sub.add_parser("init", help="create the room")
    p_init.add_argument("--root", default=None, help="repo root (default: cwd)")
    p_init.add_argument("--estate", required=True, help="estate name")
    p_init.add_argument("--type", default="engine", dest="type_",
                        help="room type (default: engine)")
    p_init.add_argument("--scope", default=None, help="bank scope (default: resolve_scope)")

    p_register = sub.add_parser(
        "register", help="register the room at --root into ~/.echelon/rooms.json (idempotent)")
    p_register.add_argument("--root", default=None, help="repo root (default: cwd)")
    p_register.add_argument("--name", default=None, help="registry name (default: the room's estate)")
    p_register.add_argument("--scope", default=None,
                            help="registry scope (default: the room's declared scope)")
    p_register.add_argument("--estate", default=None,
                            help="assign this room to a named estate GROUP (R-0154 slice 2, LAW 2)")
    p_register.add_argument("--estate-tag", default=None, dest="estate_tag",
                            help="the estate's board tag (default: the estate name)")

    p_estates = sub.add_parser("estates", help="list the estate groups and their live members (LAW 2)")

    p_projects = sub.add_parser("projects", help="set one registered room's project map")
    p_projects.add_argument("--room", required=True)
    p_projects.add_argument("--file", required=True)

    p_promote = sub.add_parser(
        "promote", help="promote <room> <project> to an earned child room (SPEC-S9 step 2)")
    p_promote.add_argument("room")
    p_promote.add_argument("project")
    p_promote.add_argument("--dry-run", action="store_true", help="print the plan, write zero bytes")
    p_promote.add_argument("--estate", action="store_true",
                           help="estate mode (R-0154 B): gate on repo-exists only, waive deploy + 3-items")
    p_promote.add_argument("--scope", default=None,
                           help="override the child room's bank scope (default: inherit parent)")
    p_promote.add_argument("--scope-own", action="store_true",
                           help="bind the child room's scope to its own project slug (R-0154 brief)")

    p_demote = sub.add_parser(
        "demote", help="roll <room>/<project> back into its parent (the rollback)")
    p_demote.add_argument("child", help="qualified child name, e.g. beta-svc/mrp")
    p_demote.add_argument("--dry-run", action="store_true", help="print the plan, write zero bytes")

    p_jump = sub.add_parser("jump", help="print the room's repo path (shell wrappers cd to it)")
    p_jump.add_argument("name")

    p_imp = sub.add_parser("import-atoms", help="charter Part 2 sweep: state atoms -> journal/imported/ (+ --apply disputes wrap/rollup kinds)")
    p_imp.add_argument("--root", required=True, help="the memory/ dir holding the atoms")
    p_imp.add_argument("--apply", action="store_true", help="dispute the pure-state kinds in the bank")

    p_status = sub.add_parser("status", help="room status as JSON")
    p_status.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    p_resume = sub.add_parser("resume", help="resume snapshot (§4 format)")
    p_resume.add_argument("--brief", action="store_true", help="7-line brief only")
    p_resume.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    p_cp = sub.add_parser("checkpoint", help="cursor + journal + ledger in one call")
    p_cp.add_argument("--auto", action="store_true", help="never raises; files default to ledger files_touched")
    p_cp.add_argument("--summary", default="", help="checkpoint one-liner")
    p_cp.add_argument("--next", action="append", default=[], dest="next_actions",
                      help="next action (repeatable)")
    p_cp.add_argument("--blocker", action="append", default=[], dest="blockers")
    p_cp.add_argument("--project", default=None)
    p_cp.add_argument("--file", action="append", default=[], dest="files",
                      help="working-set file (repeatable)")

    p_j = sub.add_parser("journal", help="append a journal line: journal <kind> <json>")
    p_j.add_argument("kind")
    p_j.add_argument("json")

    p_verdict = sub.add_parser("verdict", help="record a campaign/pilot verdict: verdict <campaign> <text> (spec S8 V5)")
    p_verdict.add_argument("campaign")
    p_verdict.add_argument("text")
    p_verdict.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    p_open = sub.add_parser("open", help="open an item")
    p_open.add_argument("text")
    p_open.add_argument("--project", default=None)
    p_close = sub.add_parser("close", help="close an item")
    p_close.add_argument("id")
    p_close.add_argument("--note", default="", help="closing note")
    p_close.add_argument("--verified-by", default="", dest="verified_by",
                         help="independent verifier of the close (other session, gate, or owner)")
    p_close.add_argument("--solo", action="store_true",
                         help="close a claimed item WITHOUT independent verification (recorded as SOLO-DECLARED)")
    p_close.add_argument("--session", default=None, help="closer identity (default: user@host:ppid)")

    p_claim = sub.add_parser("claim", help="claim an open item (bank row + claims.json, one tx)")
    p_claim.add_argument("id")
    p_claim.add_argument("--session", default=None, help="claimant (default: user@host:ppid)")
    p_claim.add_argument("--pid", type=int, default=None,
                         help="WORKER process id stamped on the claim so a refused claimant can check liveness"
                              " (default: this command's parent process — the worker that invoked it)")
    p_claim.add_argument("--no-pid", action="store_true", help="stamp no pid (claim not tied to a process)")
    p_claim.add_argument("--steal-dead", action="store_true",
                         help="take over a claim whose stamped pid is verifiably dead")
    p_claim.add_argument("--bank", default=None,
                         help="bank path (default: $ECHELON_BANK or ~/.echelon/echelon.db)")
    p_claim.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    p_release = sub.add_parser("release", help="release a claim")
    p_release.add_argument("id")
    p_release.add_argument("--force", action="store_true",
                           help="release even when another session holds the item")
    p_release.add_argument("--session", default=None, help="claimant (default: user@host:ppid)")
    p_release.add_argument("--bank", default=None,
                           help="bank path (default: $ECHELON_BANK or ~/.echelon/echelon.db)")
    p_release.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    p_claims = sub.add_parser("claims", help="print claims.json as JSON")
    p_claims.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    p_inc = sub.add_parser(
        "incident", help="file an incident, or `incident resolve <INC-id> --by <item> [--note ...]`")
    p_inc.add_argument("what", help="incident text, or the word `resolve`")
    p_inc.add_argument("target", nargs="?", default=None, help="INC-xxxx (with `resolve`)")
    p_inc.add_argument("--project", default=None)
    p_inc.add_argument("--by", default=None, help="item/run/commit that resolved it (resolve)")
    p_inc.add_argument("--note", default="", help="one line on how (resolve)")
    p_inc.add_argument("--origin", default="", help="who acted (resolve)")
    p_inc.add_argument("--no-board", action="store_true", help="skip the board row (resolve)")
    p_inc.add_argument("--room", default=None, help="registry name to resolve from (default: cwd)")

    # receipt / fold — the cross-room doors (owner #932). Both take an EXPLICIT
    # --room name so a session standing anywhere can move the right room's cursor.
    p_rcpt = sub.add_parser("receipt", help="append a receipt to a room and fold its cursor")
    p_rcpt.add_argument("kind", choices=list(RECEIPT_KINDS))
    p_rcpt.add_argument("text")
    p_rcpt.add_argument("--room", default=None, help="registry name (default: cwd's room)")
    p_rcpt.add_argument("--origin", default="", help="who caused this act (room/session/tool)")
    p_rcpt.add_argument("--ref", default=None, help="artefact this points at (board n, run id, item id)")

    p_fold = sub.add_parser("fold", help="recompute the cursor from the room's receipts")
    p_fold.add_argument("--room", default=None, help="registry name (default: cwd's room)")

    p_move = sub.add_parser(
        "move", help="re-home an item/ruling/note to another room (R-0154 slice 1)")
    p_move.add_argument("id", help="OPEN-xxxx | INC-xxxx | NOTE-xxxx | R-xxxx")
    p_move.add_argument("--to", required=True, dest="to", help="target room (registry name)")
    p_move.add_argument("--from", default=None, dest="from_room",
                        help="source room (default: cwd's room; irrelevant for R-ids)")
    p_move.add_argument("--dry-run", action="store_true", help="print the plan, write zero bytes")
    p_move.add_argument("--by", default="", help="who moved it (default: session id)")
    p_move.add_argument("--no-board", action="store_true", help="do not post the board row")

    sub.add_parser("doctor", help="door-to-door verification (rows 1,2,5,12)")

    args = ap.parse_args(argv)

    if args.action == "init":
        root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
        try:
            room = init(root, estate=args.estate, type_=args.type_, scope=args.scope)
        except RoomExists as exc:
            print(f"workcycle: room already exists at {exc}")
            return 1
        print(f"room initialized at {room}")
        return 0

    # register / jump resolve their OWN target — no cwd room gate (jump must
    # work from a room-less cwd; register addresses the room by --root).
    if args.action == "register":
        root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
        try:
            entry = register_room(root, name=args.name, scope=args.scope)
        except FileNotFoundError as exc:
            print(f"workcycle: {exc}")
            return 1
        print(f"registered {entry['estate']} -> {entry['path']} (scope {entry['scope']})")
        if getattr(args, "estate", None):
            try:
                name = args.name or entry["estate"]
                er = set_estate(args.estate, name, tag=args.estate_tag)
                print(f"  estate {er['estate']} (tag {er['tag']}) -> {', '.join(er['rooms'])}")
            except (KeyError, ValueError) as exc:
                print(f"workcycle: estate assign failed: {exc}")
                return 1
        return 0

    if args.action == "estates":
        est = estates()
        if not est:
            print("no estates declared (workcycle register --estate <name> assigns a room)")
            return 0
        for name in sorted(est):
            e = est[name]
            print(f"{name}  (tag {e['tag']}, {len(e['members'])} room(s))")
            for rn in e["members"]:
                print(f"  - {rn}")
            for rn in e["missing"]:
                print(f"  ! {rn}  (declared but not registered)")
        return 0
    if args.action == "jump":
        room = room_path(name=args.name)
        if room is None:
            reg = registered_rooms()
            hint = f" (registered: {', '.join(reg)})" if reg else ""
            print(f"workcycle: no room named {args.name!r}{hint}")
            return 1
        print(room.parent)  # the repo root — shell wrappers cd to it
        return 0

    if args.action == "projects":
        try:
            project_map = json.loads(Path(args.file).read_text(encoding="utf-8"))
            entry = set_room_projects(args.room, project_map)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"workcycle: projects failed: {exc}")
            return 1
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0

    if args.action in ("promote", "demote"):
        try:
            _scope_ov = (_project_slug(args.project) if getattr(args, "scope_own", False)
                         else getattr(args, "scope", None))
            out = promote(args.room, args.project, dry_run=args.dry_run,
                          estate=getattr(args, "estate", False), scope_override=_scope_ov) \
                if args.action == "promote" else demote(args.child, dry_run=args.dry_run)
        except Exception as exc:
            print(f"workcycle: {args.action} refused: {exc}")
            return 1
        if out.get("already"):
            print(f"{out['child']} already promoted -> {out['path']} (idempotent no-op)")
            return 0
        if args.dry_run:
            if args.action == "promote":
                print(f"promote {out['child']}: all criteria true "
                      f"(repo={out['criteria']['repo']} deploy={out['criteria']['deploy']} "
                      f"open={out['criteria']['open_items']})")
                for d, names in out["files"].items():
                    for n in names:
                        print(f"  copy {d}/{n} -> {out['path']}/{d}/{n}")
                print(f"  rollback: {out['rollback']}")
            else:
                print(f"demote {out['child']}: manifest verified; "
                      f"{len(out['untouched'])} untouched copy(ies) stay with the parent")
                for b in out["back_copies"]:
                    print(f"  back-copy {b}")
                print(f"  deletes {out['path']} (project source untouched)")
            print("  ZERO bytes written (dry run)")
        elif args.action == "promote":
            print(f"promoted {out['child']} -> {out['path']} "
                  f"(open={out['counts']['open']} closed={out['counts']['closed']} "
                  f"incidents={out['counts']['incidents']}, manifest {out['manifest_digest']})")
        else:
            print(f"demoted {out['child']}: deleted {out['deleted']}; "
                  f"{len(out['back_copies'])} record(s) back-copied, "
                  f"{len(out['untouched'])} untouched copy(ies) stayed with the parent")
        return 0

    if args.action == "move":
        return _cli_move(args)

    room = room_path(name=args.room) if getattr(args, "room", None) else room_path()
    if room is None:
        reg = registered_rooms()
        hint = f" — registered: {', '.join(reg)}" if reg else ""
        print(f"ROOM unavailable (no room — run `workcycle init` at the repo root{hint})")
        return 1

    if args.action == "import-atoms":
        m = import_atoms(room, Path(args.root), apply=args.apply)
        pend = sum(1 for r in m["rows"] if r["dispute"] and not r["disputed"])
        print(f"imported {len(m['rows'])} state atom(s) -> {room / 'journal' / 'imported'} · disputed {sum(1 for r in m['rows'] if r['disputed'])} · dispute pending {pend}"
              + (" (dry-run: add --apply to dispute wrap/rollup kinds)" if not args.apply and pend else ""))
        return 0
    if args.action == "receipt":
        res = receipt(room, args.kind, args.text,
                      origin=args.origin or _session_id(None), ref=args.ref)
        if not res.get("ok"):
            print(f"workcycle: receipt failed: {res.get('err')}")
            return 1
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0
    if args.action == "fold":
        print(json.dumps(fold(room), ensure_ascii=False, default=str))
        return 0
    if args.action == "status":
        print(json.dumps(status(room), ensure_ascii=False, indent=2, default=str))
    elif args.action == "resume":
        print(resume_brief(room) if args.brief else resume_full(room))
    elif args.action == "checkpoint":
        try:
            res = checkpoint(room, summary=args.summary,
                             next_actions=args.next_actions, blockers=args.blockers,
                             files=args.files, project=args.project,
                             auto=args.auto)
        except Exception as exc:
            print(f"workcycle: checkpoint failed: {exc}")
            return 1
        if res.get("ok") is False:
            print(f"workcycle: checkpoint failed: {res.get('err')}")
            return 1
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif args.action == "journal":
        try:
            data = json.loads(args.json)
        except json.JSONDecodeError as exc:
            print(f"workcycle: bad json: {exc}")
            return 1
        if not isinstance(data, dict):
            print("workcycle: journal json must be an object")
            return 1
        print(json.dumps(journal(room, args.kind, data), ensure_ascii=False))
    elif args.action == "verdict":
        try:
            print(json.dumps(record_verdict(room, args.campaign, args.text), ensure_ascii=False))
        except ValueError as exc:
            print(f"workcycle: {exc}")
            return 1
    elif args.action == "open":
        print(open_item(room, args.text, project=args.project))
    elif args.action == "close":
        try:
            print(json.dumps(close_item(room, args.id, note=args.note,
                                        verified_by=args.verified_by, solo=args.solo,
                                        session=_session_id(args.session)),
                             ensure_ascii=False, indent=2))
        except (FileNotFoundError, ValueError) as exc:
            print(f"workcycle: {exc}")
            return 1
    elif args.action == "claim":
        try:
            res = claim(room, args.id, session=_session_id(args.session),
                        bank=_bank_path(args.bank),
                        pid=None if args.no_pid else (args.pid or os.getppid()),
                        steal_dead=args.steal_dead)
        except ClaimHeld as exc:
            live = ("" if exc.alive is None else
                    (" — pid ALIVE, really being worked on" if exc.alive
                     else " — pid DEAD, re-run with --steal-dead to take it over"))
            print(f"{args.id} held by {exc.holder} since {exc.ts}"
                  f"{f' (pid {exc.pid}{live})' if exc.pid else ''}")
            return 1
        except ClaimBusy as exc:
            code = "" if exc.code is None else f" [sqlite code {exc.code}]"
            print(f"workcycle: {exc.operation} acquisition busy{code}; no claim acknowledgement. "
                  "Inspect bank state, then make an explicit new attempt.")
            return 1
        except (FileNotFoundError, ValueError) as exc:
            print(f"workcycle: {exc}")
            return 1
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.action == "release":
        try:
            res = release(room, args.id, session=_session_id(args.session),
                          bank=_bank_path(args.bank), force=args.force)
        except ClaimHeld as exc:
            print(f"{args.id} held by {exc.holder} since {exc.ts}")
            return 1
        except ClaimBusy as exc:
            code = "" if exc.code is None else f" [sqlite code {exc.code}]"
            print(f"workcycle: {exc.operation} acquisition busy{code}; no release acknowledgement. "
                  "Inspect bank state, then make an explicit new attempt.")
            return 1
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.action == "claims":
        print(json.dumps(claims(room), ensure_ascii=False, indent=2))
    elif args.action == "incident":
        if args.what == "resolve":
            if not args.target:
                print("usage: workcycle incident resolve <INC-xxxx> --by <item> [--note ...]")
                return 2
            try:
                res = incident_resolve(room, args.target, by=args.by or "", note=args.note,
                                       origin=args.origin, board=not args.no_board)
            except IncidentRefused as exc:
                print(f"REFUSED: {exc}")
                return 1
            print(f"{res['id']} resolved by {res['resolved']['by']} · "
                  f"{res['unresolved']} unresolved left · receipt "
                  f"{'written' if not (res['receipt'] or {}).get('deduped') else 'deduped'}")
        else:
            print(incident(room, args.what, project=args.project))
    elif args.action == "doctor":
        rows = doctor(room)
        for r in rows:
            print(f"row {r['row']}: {'OK  ' if r['ok'] else 'FAIL'}  {r['evidence']}")
        return 0 if all(r["ok"] for r in rows) else 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
