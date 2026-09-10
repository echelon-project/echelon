"""public_stats — the ONE public, read-only aggregate of the live bank.

Serves the landing page's "Live substrate" block (OPEN-0107, owner 2026-09-06).
The site's own rule is "claim only what was witnessed", so this reports the bank
on THIS host — never a number from anywhere else pretending to be the server's.

HARD PRIVACY BOUNDARY (never widen): this returns COUNTS ONLY — no atom bodies,
no atom names, no scope names, no per-scope breakdown. A public route may expose
that the bank holds N atoms across M scopes; it must never expose WHICH scopes or
WHAT they hold. Everything here is a `COUNT(*)` / `SUM(score)` — never a row scan.

The counts are cached for `_TTL_S` seconds keyed by the bank file's mtime, so a
burst of page loads costs one cheap query per minute, not one per request.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

# Cache 60 s (owner: "cached 60 s, never a scan").
_TTL_S = 60.0
# (payload, monotonic_expiry, bank_mtime_ns) — invalidated by TTL OR a bank write.
_CACHE: tuple[dict, float, int] | None = None

# Western Indonesia Time (WIB = UTC+7); the owner asked for the snapshot in WIB.
_WIB_OFFSET_S = 7 * 3600


def _bank_path() -> Path:
    """The live bank this host serves — ECHELON_HOME/echelon.db (same as the CLI)."""
    home = os.environ.get("ECHELON_HOME") or str(Path.home() / ".echelon")
    return Path(home) / "echelon.db"


def _iso_wib(epoch_s: float) -> str:
    """ISO-8601 timestamp in WIB (+07:00) from a UTC epoch."""
    lt = time.gmtime(epoch_s + _WIB_OFFSET_S)
    return time.strftime("%Y-%m-%dT%H:%M:%S+07:00", lt)


def _cartridge_count() -> int:
    """Cartridges in the roster — from the registry, not the bank. Best-effort."""
    try:
        from echelon_engine.atoms.cartridge_registry import all_specs
        return len(list(all_specs()))
    except Exception:
        return 0


def _compute(bank: Path) -> dict:
    """Cheap aggregate counts from the live bank. COUNT/SUM only — never a scan."""
    # read-only, immutable=1 so a concurrent WAL writer never blocks us and we
    # never touch the bank's journal (a public reader must not perturb the writer).
    uri = f"file:{bank.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        cur = conn.cursor()
        atoms = cur.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        scopes = cur.execute(
            "SELECT COUNT(DISTINCT scope) FROM atoms WHERE scope IS NOT NULL AND scope <> ''"
        ).fetchone()[0]
        earned_weight = cur.execute(
            "SELECT COALESCE(SUM(score), 0) FROM atom_earned"
        ).fetchone()[0]
        earned_atoms = cur.execute(
            "SELECT COUNT(DISTINCT atom_id) FROM atom_earned"
        ).fetchone()[0]
    finally:
        conn.close()

    mtime = bank.stat().st_mtime
    now = time.time()
    return {
        "ok": True,
        "atoms": int(atoms),
        "earned_weight": int(round(float(earned_weight))),
        "earned_atoms": int(earned_atoms),
        "scopes": int(scopes),
        "cartridges": _cartridge_count(),
        # WIB, both the moment we read AND the bank's own last-write time.
        "snapshot_at": _iso_wib(now),
        "bank_mtime": _iso_wib(mtime),
    }


def public_stats(*, force: bool = False) -> dict:
    """Aggregate bank counts for the public landing page. Cached `_TTL_S` seconds,
    invalidated early when the bank file is rewritten. On any error returns
    {"ok": False, "error": ...} so the page keeps its baked-in snapshot."""
    global _CACHE
    bank = _bank_path()
    try:
        mtime_ns = bank.stat().st_mtime_ns
    except OSError as e:
        return {"ok": False, "error": f"bank unavailable: {type(e).__name__}"}

    if not force and _CACHE is not None:
        payload, expiry, cached_mtime = _CACHE
        if time.monotonic() < expiry and cached_mtime == mtime_ns:
            return payload

    try:
        payload = _compute(bank)
    except Exception as e:  # noqa: BLE001 — a public page must degrade, never 500 loudly
        return {"ok": False, "error": f"{type(e).__name__}"}

    _CACHE = (payload, time.monotonic() + _TTL_S, mtime_ns)
    return payload
