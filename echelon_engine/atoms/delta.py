"""delta — the LOCAL delta backup (spec S8 V7, DELTA-SYNC-DESIGN §1/§8 LOCAL half).

`echelon backup --delta` appends what changed since the last run — the trigger-fed
sync_journal rows after the cursor, a room sweep, and changed atom files — to
ARCHIVE/delta/<date>/, instead of snapshotting the whole bank. The two-way cloud
half of the design stays parked until NEXUS.

THE CURSOR — <home>/delta_cursor.json `{v:1, seq, last_run}` (home =
workcycle._echelon_home(), so ECHELON_HOME relocates everything). ONE WRITER:
this module. `seq` is the last journaled sync_journal seq; `last_run` is the ISO
timestamp of the last delta. The cursor advances ONLY after the manifest is
written (ack-after-write): a crash mid-delta re-writes the same rows next run.

THE NO-CHANGE HOUR — no new journal rows AND no room change -> NOTHING is
written (spec: "no-change hour -> no file"). A room change is any registered
room's cursor.json or open/*.json mtime newer than last_run. The journal file is
DELIBERATELY excluded from the detector: the delta's own receipt appends to
today's journal, so watching it would make every delta self-trigger the next one
(measured churn trap, 2026-08-29). Journal-only events ride the next delta —
the sweep copies today's journal file on every run regardless.

RECEIPT-BEFORE-CURSOR — the receipt is journaled BEFORE the cursor is written,
for the same reason: a receipt written after the cursor would sit newer than
last_run and self-trigger the next run even with the journal excluded (the
sweep's own copy target would be newer).

THE TIMER IS OPPORTUNISTIC — Windows has no cron; a hook is the only clock.
SessionStart arms (<home>/delta.armed), Stop disarms AND schedules a final delta,
UserPromptSubmit ticks: armed + >=3600s since last_run -> spawn
`python -X utf8 -m echelon_engine backup --delta` DETACHED. A separate scheduling
timestamp throttles concurrent prompts; only a completed scan advances last_run.
Hook helpers never raise; failures report to stderr. Tick opens NO bank.

LIVE-BANK SAFETY — the journal is read through a read-only
`sqlite3.connect(f"file:{bank}?mode=ro", uri=True)`; the WAL-mode bank file is
never copy2'd (a live WAL bank copies as a table-less 4KB stub — the reflex
copy2-a-live-wal-bank). Only room files and atom .md files are copied.

THE >100MB MIRROR (charter R5) — when a delta dir's byte total exceeds 100MB it
is pushed via archive.cmd_push(delta_dir, "delta/<date>/<HHMMSS>", all_=True).
all_=True is required: cmd_push's own default min_mb=100 filters per FILE, and a
delta dir's files are small — the default would skip every file and the mirror
would be a silent no-op. A push failure is a stderr warning + push_error in the
return; the local delta is the ack and the push never raises out of it.
"""
from __future__ import annotations

import json
import contextlib
import errno
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from echelon_engine import archive, workcycle
from .. import estate as _estate

VERSION = 1
MIN_INTERVAL = 3600                    # the opportunistic tick fires at most hourly
PUSH_BYTES = 100 * 1000 * 1000         # R5: delta dirs over this mirror to the bucket
_CURSOR_NAME = "delta_cursor.json"
_ARMED_NAME = "delta.armed"
_LOG_NAME = "delta.log"
_SCHEDULE_NAME = "delta_schedule.json"
_THREAD_LOCK = threading.RLock()
_ARCHIVE_ROOT = _estate.sibling("echelon-archive", "ECHELON-ARCHIVE")


@contextlib.contextmanager
def _delta_lock(name: str, *, timeout: float = 10):
    """Serialize delta writers across threads and processes; never unlink locks."""
    home = workcycle._echelon_home()
    home.mkdir(parents=True, exist_ok=True)
    if not _THREAD_LOCK.acquire(timeout=timeout):
        raise TimeoutError("delta writer busy")
    try:
        with (home / name).open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("delta writer busy") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        _THREAD_LOCK.release()


def _warn(operation: str, exc: Exception) -> None:
    # Provider/tool exceptions may contain credentials; report the class only.
    print(f"delta: {operation} unavailable ({type(exc).__name__}); "
          "archive completion unconfirmed", file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_epoch(iso: str) -> float:
    """ISO-8601 (UTC) -> epoch seconds; 0.0 for anything unparseable (a missing
    last_run means 'everything changed' on the first run)."""
    try:
        d = datetime.fromisoformat(str(iso or ""))
    except (TypeError, ValueError):
        return 0.0
    return d.timestamp() if d.tzinfo else d.replace(tzinfo=timezone.utc).timestamp()


def _cursor_path() -> Path:
    return workcycle._echelon_home() / _CURSOR_NAME


def _read_cursor() -> dict:
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"v": VERSION, "seq": 0, "last_run": ""}
    if not isinstance(data, dict) or data.get("v") != VERSION:
        return {"v": VERSION, "seq": 0, "last_run": ""}
    result = {"v": VERSION, "seq": int(data.get("seq") or 0),
              "last_run": str(data.get("last_run") or "")}
    if "file_hashes" in data:
        if not isinstance(data["file_hashes"], dict):
            raise ValueError("invalid delta file inventory")
        result["file_hashes"] = data["file_hashes"]
    return result


def _write_cursor(cursor: dict) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cursor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with tmp.open("r+b") as handle:
        os.fsync(handle.fileno())
    tmp.replace(path)


def default_out_root() -> Path:
    """The estate archive root when it exists, else
    <home>/archive. When ECHELON_HOME is explicitly set, <home>/archive wins —
    an overridden home means an isolated substrate (tests, MCP server), and the
    estate archive must never receive another home's deltas."""
    if os.environ.get("ECHELON_HOME"):
        return workcycle._echelon_home() / "archive"
    return _ARCHIVE_ROOT if _ARCHIVE_ROOT.exists() else workcycle._echelon_home() / "archive"


def default_bank() -> Path:
    """The live bank under the ECHELON_HOME-aware home (unlike backup_cmd._BANK,
    a module constant — the hook-spawned child must never read the wrong bank)."""
    return workcycle._echelon_home() / "echelon.db"


def _room_changed(entries: dict, last_run: float) -> bool:
    """True when any registered room's cursor.json or open/*.json is newer than
    last_run. The journal file is deliberately NOT watched (see module docstring:
    the receipt self-trigger churn trap). Missing room dirs are skipped."""
    if not last_run:
        return True                        # no prior run — everything is "changed"
    for entry in entries.values():
        e = Path(str(entry.get("path") or "")).resolve()
        if not e.is_dir():
            continue
        cursor = e / "cursor.json"
        if cursor.is_file() and cursor.stat().st_mtime > last_run:
            return True
        odir = e / "open"
        if odir.is_dir():
            for p in odir.glob("*.json"):
                if p.stat().st_mtime > last_run:
                    return True
        mem = e.parent / "memory"
        if mem.is_dir() and any(p.stat().st_mtime > last_run for p in mem.rglob("*.md")):
            return True
        # today's journal is NOT watched here — the sweep copies it every run,
        # but watching it would make the delta's own receipt self-trigger the
        # next run (see module docstring).
    return False


def _watched_hashes(entries: dict) -> dict:
    """Content evidence avoids filesystem-clock granularity and backdated edits.

    Journal-only receipts remain excluded from the no-change detector. Their
    contents ride the next room/atom/journal-table change as before.
    """
    hashes = {}
    for entry in entries.values():
        room = Path(str(entry.get("path") or "")).resolve()
        if not room.is_dir():
            continue
        paths = [room / "cursor.json", *(room / "open").glob("*.json"),
                 *(room.parent / "memory").rglob("*.md")]
        for path in paths:
            if path.is_file():
                hashes[str(path.resolve())] = archive._sha256(path)
    return hashes


def _sweep_room(delta_dir: Path, entry: dict, captured_hashes=None) -> dict:
    """Copy cursor.json + open/*.json + today's journal/*.jsonl under
    rooms/<name>/. Returns {cursor: bool, open: int, journal: int}."""
    e = Path(str(entry.get("path") or "")).resolve()
    name = entry.get("estate") or e.parent.name
    dst = delta_dir / "rooms" / name
    got = {"cursor": False, "open": 0, "journal": 0}
    if not e.is_dir():
        return got
    cursor = e / "cursor.json"
    if cursor.is_file():
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cursor, dst / cursor.name)
        if captured_hashes is not None:
            captured_hashes[str(cursor.resolve())] = archive._sha256(dst / cursor.name)
        got["cursor"] = True
    odir = e / "open"
    if odir.is_dir():
        for p in sorted(odir.glob("*.json")):
            (dst / "open").mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / "open" / p.name)
            if captured_hashes is not None:
                captured_hashes[str(p.resolve())] = archive._sha256(dst / "open" / p.name)
            got["open"] += 1
    jdir = e / "journal"
    today = f"{date.today().isoformat()}.jsonl"
    if jdir.is_dir() and (jdir / today).is_file():
        (dst / "journal").mkdir(parents=True, exist_ok=True)
        shutil.copy2(jdir / today, dst / "journal" / today)
        got["journal"] = 1
    return got


def _copy_atoms(delta_dir: Path, entry: dict, last_run: float, previous_hashes=None,
                captured_hashes=None) -> int:
    """Copy every *.md under the room repo's memory/ with mtime > last_run into
    atoms/<relpath>. Returns how many were copied. The repo root is the registry
    entry's parent (entries point at <repo>/.echelon)."""
    e = Path(str(entry.get("path") or "")).resolve()
    mem = e.parent / "memory"
    if not mem.is_dir():
        return 0
    n = 0
    for p in mem.rglob("*.md"):
        changed = (archive._sha256(p) != previous_hashes.get(str(p.resolve()))
                   if previous_hashes is not None else p.stat().st_mtime > last_run)
        if changed:
            rel = p.relative_to(mem)
            dst = delta_dir / "atoms" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            if captured_hashes is not None:
                captured_hashes[str(p.resolve())] = archive._sha256(dst)
            n += 1
    return n


def _dir_bytes(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


def run_delta(bank, out_root, *, force: bool = False, client=None) -> dict:
    """Serialize the complete scan, archive and cursor acknowledgement."""
    with _delta_lock("delta.writer.lock"):
        return _run_delta(bank, out_root, force=force, client=client)


def _run_delta(bank, out_root, *, force: bool = False, client=None) -> dict:
    """One delta: journal rows after the cursor + room sweep + changed atoms +
    MANIFEST.json; cursor advances only after the manifest (ack-after-write).

    Returns {"wrote": False} on a no-change hour (nothing written, cursor
    untouched), else {"wrote": True, delta_dir, journal_rows, from_seq, to_seq,
    rooms, atoms, bytes, pushed, push_error}."""
    started_at = _now_iso()
    cursor = _read_cursor()
    bank = Path(bank)
    # ONE resolved out_root everywhere (gate r1 V7 M-2): a relative / 8.3 / symlinked
    # --out or ECHELON_HOME made relative_to() raise AFTER the manifest and BEFORE
    # the cursor -> unbounded duplicate deltas, swallowed by the Stop hook.
    out_root = Path(out_root).resolve()
    # LIVE-BANK SAFETY: read-only URI connection — the WAL-mode bank is never
    # copy2'd (a live copy is a table-less stub; the reflex).
    conn = None
    try:
        conn = sqlite3.connect(f"file:{bank}?mode=ro", uri=True)
        rows = [dict(zip(("seq", "tbl", "pk", "op", "payload", "scope", "ts", "origin"), r))
                for r in conn.execute(
                    "SELECT seq, tbl, pk, op, payload, scope, ts, origin "
                    "FROM sync_journal WHERE seq > ? ORDER BY seq",
                    (cursor["seq"],))]
    except sqlite3.OperationalError:
        rows = []                          # missing bank / no sync_journal table — nothing to read
    finally:
        if conn is not None:
            conn.close()
    last_run = _to_epoch(cursor["last_run"])
    entries = workcycle._registry_entries()
    scanned_hashes = _watched_hashes(entries)
    previous_hashes = cursor.get("file_hashes")
    changed = (scanned_hashes != previous_hashes if previous_hashes is not None
               else _room_changed(entries, last_run))
    if not rows and not changed and not force:
        return {"wrote": False}

    stamp = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:12]
    today = date.today().isoformat()
    from_seq = cursor["seq"]
    to_seq = rows[-1]["seq"] if rows else from_seq
    delta_dir = out_root / "delta" / today / f"{stamp}-seq{from_seq}-{to_seq}"
    delta_dir.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []

    if rows:
        jfile = delta_dir / f"{stamp}-seq{from_seq}-{to_seq}.jsonl"
        with open(jfile, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        written.append(jfile)

    n_rooms = 0
    captured_hashes = {path: (previous_hashes or scanned_hashes).get(path)
                       for path in scanned_hashes}
    for entry in entries.values():
        got = _sweep_room(delta_dir, entry, captured_hashes)
        if got["cursor"] or got["open"] or got["journal"]:
            n_rooms += 1
    n_atoms = 0
    for entry in entries.values():
        n_atoms += _copy_atoms(delta_dir, entry, last_run, previous_hashes, captured_hashes)

    # MANIFEST last: every other file is hashed into it (itself excluded).
    files = {str(p.relative_to(delta_dir)).replace("\\", "/"): archive._sha256(p)
             for p in delta_dir.rglob("*") if p.is_file()}
    bytes_total = _dir_bytes(delta_dir)
    manifest = {"v": VERSION, "written_at": _now_iso(), "scan_started_at": started_at,
                "from_seq": from_seq, "to_seq": to_seq,
                "journal_rows": len(rows), "rooms": n_rooms, "atoms": n_atoms,
                "bytes": bytes_total, "files": files}
    (delta_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    # Flush copied payloads and the manifest before acknowledging the scan.
    for path in [*(delta_dir / rel for rel in files), delta_dir / "MANIFEST.json"]:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())

    # Receipt BEFORE the cursor: a receipt written after last_run would self-trigger
    # the next run (the receipt appends to the very journal the sweep copies).
    room = workcycle.room_path()
    if room is None:
        print("delta: no room in this cwd — receipt not journaled", file=sys.stderr)
    else:
        workcycle.journal(room, "receipt", {"delta": "run",
                                            "delta_dir": str(delta_dir.relative_to(out_root)),
                                            "journal_rows": len(rows),
                                            "from_seq": from_seq, "to_seq": to_seq,
                                            "bytes": bytes_total})

    # ACK-AFTER-WRITE: the cursor moves only once the manifest is on disk.
    # Changes after the scan started must remain eligible for the next delta,
    # including files modified after they were copied by this sweep.
    _write_cursor({"v": VERSION, "seq": to_seq, "last_run": started_at,
                   "file_hashes": captured_hashes})

    pushed, push_error = False, None
    if bytes_total > PUSH_BYTES:
        prefix = f"delta/{today}/{stamp}"
        try:
            archive.cmd_push(delta_dir, prefix, all_=True, client=client)
            pushed = True
        except Exception as exc:           # the local delta is the ack — never fail it over the mirror
            push_error = f"{type(exc).__name__}: {exc}"
            print(f"delta: >100MB mirror push failed ({push_error}) — local delta intact",
                  file=sys.stderr)
    return {"wrote": True, "delta_dir": str(delta_dir), "journal_rows": len(rows),
            "from_seq": from_seq, "to_seq": to_seq, "rooms": n_rooms, "atoms": n_atoms,
            "bytes": bytes_total, "pushed": pushed, "push_error": push_error}


# ── the opportunistic timer (hooks are the only clock on Windows) ────────────

def arm() -> None:
    """SessionStart: write the armed marker. Never raises, <50ms."""
    try:
        home = workcycle._echelon_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / _ARMED_NAME).write_text(_now_iso() + "\n", encoding="utf-8")
    except Exception:
        pass


def disarm() -> dict:
    """Stop: disarm and request a final delta; completion remains unconfirmed.

    Return scheduling observations without raising into the hook. An exception
    after Popen may leave a child alive, so a failed acknowledgement is not retry-safe.
    """
    result = {"disarmed": False, "scheduled": False, "archive_complete": None}
    try:
        armed = workcycle._echelon_home() / _ARMED_NAME
        try:
            armed.unlink()
        except FileNotFoundError:
            pass
        result["disarmed"] = True
    except Exception as exc:
        result["disarm_error"] = type(exc).__name__
        _warn("timer disarm", exc)
    # The final flush is SPAWNED DETACHED, never run inline (gate r1 V7 M-3): the
    # Stop hook lives in a 10s box that a first-run delta (every atom, every
    # room, a sha256 per file) plausibly exceeds -- an inline run left orphaned
    # manifest-less dirs and a silently broken promise. Scheduling a detached
    # child is NOT proof that it completed; its manifest is separate evidence.
    try:
        result["pid"] = _spawn_delta(workcycle._echelon_home())
        result["scheduled"] = True
    except Exception as exc:
        result["schedule_error"] = type(exc).__name__
        result["retry_safe"] = False
        _warn("final delta scheduling", exc)
    return result


def _spawn_delta(home: Path) -> int | None:
    """Spawn `backup --delta` DETACHED with stdout/stderr -> <home>/delta.log
    (never inherited: a hook's stdout is the harness protocol channel)."""
    log = open(home / _LOG_NAME, "a", encoding="utf-8")
    try:
        argv = [sys.executable, "-X", "utf8", "-m", "echelon_engine", "backup", "--delta"]
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
        else:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        log.close()
    return getattr(child, "pid", None)


def tick() -> None:
    """UserPromptSubmit: if armed and >=3600s since the last delta, spawn
    `python -X utf8 -m echelon_engine backup --delta` DETACHED. Throttle attempts
    separately from the successful archive watermark. Opens no bank; never raises."""
    try:
        home = workcycle._echelon_home()
        if not (home / _ARMED_NAME).exists():
            return
        with _delta_lock("delta.schedule.lock", timeout=0.02):
            cursor = _read_cursor()
            schedule = home / _SCHEDULE_NAME
            try:
                last_attempt = json.loads(schedule.read_text(encoding="utf-8"))["attempted_at"]
            except FileNotFoundError:
                last_attempt = ""
            # Throttle attempts without pretending those files were archived.
            if time.time() - max(_to_epoch(cursor["last_run"]), _to_epoch(last_attempt)) < MIN_INTERVAL:
                return
            temporary = schedule.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"v": 1, "attempted_at": _now_iso()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(schedule)
            _spawn_delta(home)
    except Exception as exc:
        _warn("scheduled delta", exc)
