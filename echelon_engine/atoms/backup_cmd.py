"""backup — snapshot the bank to a timestamped copy, locally or to the cloud gateway.

    python -X utf8 -m echelon_engine backup           # snapshot echelon.db
    python -X utf8 -m echelon_engine backup --list    # list existing backups
    python -X utf8 -m echelon_engine backup --restore <path>  # restore from backup
    python -X utf8 -m echelon_engine backup --push    # push a snapshot to the cloud gateway
    python -X utf8 -m echelon_engine backup --cloud-list  # list cloud snapshots
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import time
import urllib.request
from pathlib import Path
from .. import estate as _estate


_BACKUP_DIR = Path.home() / ".echelon" / "backups"
_BANK = Path.home() / ".echelon" / "echelon.db"
# No built-in gateway: an archive/sync endpoint is deployment-specific.
# Set ECHELON_GATEWAY to your own host to enable the remote verbs.
_GATEWAY = os.environ.get("ECHELON_GATEWAY", "").rstrip("/")


def _gateway_token(explicit: str | None) -> str:
    tok = explicit or os.environ.get("ECHELON_TOKEN") or os.environ.get("ECHELON_MCP_TOKEN") or ""
    if not tok:
        # last resort: the work-folder .env the rest of the substrate uses
        env = Path.home() / ".echelon" / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("ECHELON_TOKEN=") or line.startswith("ECHELON_MCP_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
    if not tok:
        raise SystemExit("no gateway token — pass --token, or set ECHELON_TOKEN in the env "
                         "or ~/.echelon/.env (your token comes with your invite)")
    return tok


def _consistent_snapshot(src: Path) -> bytes:
    """A WAL-safe snapshot image of the bank, as bytes.

    Never shutil.copy2 a live bank: in WAL mode the .db can be a 4KB stub with all data in
    the sibling -wal, so the copy has NO TABLES (see _safe_file_copy), and copy2 also
    preserves the source mtime (breaks freshness gates).

    PORTABILITY (2026-08-02): `Connection.serialize()` is Python 3.11+. Box4 runs 3.10, where
    it raises AttributeError — which would have broken `backup --push` there. `Connection.
    backup()` exists on every supported version and is equally WAL-safe (it runs the online
    backup API under a read transaction), so it is the primary path."""
    live = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        if hasattr(live, "serialize"):
            return bytes(live.serialize())
        # 3.10 and older: online-backup into memory, then read the image out via a temp file
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.db"
            dest = sqlite3.connect(str(tmp))
            try:
                live.backup(dest)
            finally:
                dest.close()
            return tmp.read_bytes()
    finally:
        live.close()


def _safe_file_copy(src: Path, dst: Path) -> None:
    """Copy a bank FILE the WAL-safe way.

    THE TRAP (measured 2026-08-02): the bank runs in WAL mode, so a `shutil.copy2` of just
    the .db file copies a 4KB stub while every byte of real data sits in the sibling -wal.
    The result is not merely stale — it has NO TABLES AT ALL ("no such table: atoms"). It
    only looks fine when the source happens to be checkpointed (a clean close), which is
    exactly NOT the case when another process holds the bank open — i.e. whenever you most
    need the backup.

    Uses sqlite's online backup API (or serialize() on 3.11+), which reads under a read
    transaction and therefore includes the WAL."""
    live = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(dst))
        try:
            live.backup(dest)      # available on every supported python; WAL-safe
        finally:
            dest.close()
    finally:
        live.close()


def _push(bank: Path, gateway: str, token: str) -> int:
    if not bank.exists():
        print(f"bank not found at {bank}")
        return 1
    raw = _consistent_snapshot(bank)
    gz = gzip.compress(raw, compresslevel=6)
    print(f"pushing {bank.name}: {len(raw)/1e6:.1f}MB -> {len(gz)/1e6:.1f}MB gzipped -> {gateway}/backup")
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}/backup", data=gz, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/gzip",
                 "X-Bank-Name": bank.name,
                 "User-Agent": "echelon-backup/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    print(f"cloud: stored {out.get('stored')} ({out.get('bytes', 0)/1e6:.1f}MB) — "
          f"{out.get('kept')} snapshot(s) kept for '{out.get('user')}'")
    return 0


def _cloud_list(gateway: str, token: str) -> int:
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}/backup/list",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "echelon-backup/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    snaps = out.get("snapshots", [])
    if not snaps:
        print(f"(no cloud snapshots yet for '{out.get('user')}' — run: echelon backup --push)")
        return 0
    print(f"Cloud snapshots for '{out.get('user')}' at {gateway}:")
    for s in snaps:
        print(f"  {s['name']:<40} {s['bytes']/1e6:.1f}MB  {s['mtime']}")
    return 0


def _list_backups():
    if not _BACKUP_DIR.exists():
        print("(no backups found)")
        return
    backups = sorted(_BACKUP_DIR.glob("echelon_*.db"), reverse=True)
    if not backups:
        print("(no backups found)")
        return
    for b in backups[:20]:
        size_mb = b.stat().st_size / (1024 * 1024)
        ts = b.stat().st_mtime
        print(f"  {b.name:<50} {size_mb:.1f}MB  {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}")
    if len(backups) > 20:
        print(f"  ... and {len(backups) - 20} more")


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="echelon backup",
                                 description="Snapshot the bank to a timestamped backup.")
    ap.add_argument("--list", action="store_true", help="list existing backups")
    ap.add_argument("--restore", metavar="PATH", help="restore bank from a backup file")
    ap.add_argument("--bank", default=None,
                    help=f"bank to back up (default: {_BANK}; --delta defaults to the ECHELON_HOME-aware bank)")
    ap.add_argument("--push", action="store_true",
                    help="push a WAL-safe snapshot to the cloud gateway (POST /backup)")
    ap.add_argument("--cloud-list", action="store_true", help="list cloud snapshots")
    ap.add_argument("--delta", action="store_true",
                    help="write a delta backup (spec S8 V7): journal rows since the cursor + room sweep + changed atoms")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="delta out root (default: the estate archive if it exists, else <home>/archive)")
    ap.add_argument("--force", action="store_true",
                    help="delta: write even when nothing changed since the last run")
    ap.add_argument("--gateway", default=_GATEWAY,
                    help=f"gateway base URL (default: {_GATEWAY}, env ECHELON_GATEWAY)")
    ap.add_argument("--token", default=None,
                    help="bearer token (default: env ECHELON_TOKEN / ECHELON_MCP_TOKEN or ~/.echelon/.env)")
    a = ap.parse_args(argv)
    if not a.delta and not a.bank:
        a.bank = str(_BANK)

    if a.delta:
        from .delta import default_bank, default_out_root, run_delta
        # a hook-spawned child under ECHELON_HOME must NEVER read the live bank: the old
        # default=str(_BANK) made `a.bank` always truthy and bypassed default_bank() (V7 r1 heal)
        bank = Path(a.bank) if a.bank else default_bank()
        out_root = Path(a.out) if a.out else default_out_root()
        res = run_delta(bank, out_root, force=a.force)
        if not res.get("wrote"):
            print("delta: no change since the last run — nothing written")
            return 0
        print(f"delta: wrote {res['delta_dir']} · {res['journal_rows']} journal rows "
              f"(seq {res['from_seq']} -> {res['to_seq']}) · {res['rooms']} room(s) · "
              f"{res['atoms']} atom(s) · {res['bytes']:,} bytes"
              + (" · pushed to bucket" if res.get("pushed") else ""))
        return 0

    if a.push:
        rc = _push(Path(a.bank), a.gateway, _gateway_token(a.token))
        # LIVENESS RIDES THE DAILY JOB (owner 2026-08-18). The atom-liveness sweep is estate
        # hygiene, not a wrap gate: no claim in a wrap receipt is false if it never ran, and
        # it was the slowest step in the ritual. /wrap now only ENQUEUES (one file write);
        # the sweep happens here, off the critical path. It only REPORTS — a dead path is
        # usually a MOVE, and auto-disputing on a failed grep would destroy earned weight.
        # Never allowed to fail the backup: hygiene must not break the recovery path.
        try:
            from .liveness import drain
            res = drain()
            n = sum(s.get("anchor_gone", 0) for s in (res.get("scopes") or {}).values())
            if res.get("scopes"):
                print(f"liveness: swept {len(res['scopes'])} scope(s), "
                      f"{n} atom(s) awaiting a verdict "
                      f"(python -X utf8 -m echelon_engine liveness --report)")
        except Exception as e:
            print(f"liveness: sweep skipped ({type(e).__name__}: {e}) — backup unaffected")
        # OPENROUTER ROSTER RIDES THE DAILY JOB TOO (owner law 2026-09-01: "you must refresh
        # the roster every-day — there would be a new free tier, or past free tier that
        # retired"). Report-only: routing.model_chain skips retired free seats while the
        # snapshot is fresh; the DECLARED table is never rewritten silently.
        try:
            from .roster import refresh
            snap = refresh()
            if snap.get("ok"):
                print(f"roster: {snap['count']} openrouter free seats "
                      f"(+{len(snap['arrived'])}/-{len(snap['retired'])}); "
                      f"{len(snap['retired_routing_picks'])} routing pick(s) retired")
            else:
                print(f"roster: refresh skipped ({snap.get('error')}) — backup unaffected")
        except Exception as e:
            print(f"roster: refresh skipped ({type(e).__name__}: {e}) — backup unaffected")
        return rc
    if a.cloud_list:
        return _cloud_list(a.gateway, _gateway_token(a.token))

    if a.list:
        print(f"Backups in {_BACKUP_DIR}:")
        _list_backups()
        return 0

    if a.restore:
        src = Path(a.restore)
        if not src.exists():
            print(f"backup file not found: {src}")
            return 1
        dst = Path(a.bank)
        # Safety: back up current before restore
        safety = dst.with_suffix(".db.pre_restore")
        print(f"saving current bank to {safety.name}...")
        _safe_file_copy(dst, safety)      # WAL-safe: copy2 here would save a 4KB stub
        print(f"restoring from {src.name}...")
        # The SOURCE is a backup file (already checkpointed/serialized), so a plain copy is
        # right here — and it must stay a verbatim reproduction (restore-is-not-a-write-path).
        shutil.copy2(src, dst)
        # A restore leaves the OLD bank's -wal/-shm beside the new file; SQLite would replay
        # them over the restored image and silently resurrect pre-restore state.
        for side in (dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm")):
            if side.exists():
                side.unlink()
        # A whole-file restore is TRIGGER-INVISIBLE — the sync journal never sees it, and the
        # restored file's seq counter now sits underneath every peer's cursor (they would skip
        # ops silently). Re-minting the generation is the only detector: peers compare it before
        # cursors and fall back to a full re-baseline on mismatch. See sync_journal.py.
        try:
            from . import sync_journal as _sync
            gen = _sync.remint_generation(dst)
            print(f"sync generation re-minted: {gen[:12]} (peers will re-baseline)")
        except Exception as e:  # noqa: BLE001 — never fail a restore over the sync stamp
            print(f"(sync generation not re-minted: {e})")
        print("restored.")
        return 0

    # Default: create backup
    src = Path(a.bank)
    if not src.exists():
        print(f"bank not found at {src}")
        return 1

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = _BACKUP_DIR / f"echelon_{stamp}.db"
    _safe_file_copy(src, dst)   # WAL-safe: copy2 of a live WAL bank writes a table-less stub
    size_mb = dst.stat().st_size / (1024 * 1024)
    print(f"backup: {dst.name}  ({size_mb:.1f}MB)")

    # Cleanup: keep last 10, remove older
    all_backups = sorted(_BACKUP_DIR.glob("echelon_*.db"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in all_backups[:-10]:
        old.unlink()

    return 0
