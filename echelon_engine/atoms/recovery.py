"""recovery.py — email+code recovery primitives (the shared half of /recover).

Owner 2026-07-30: "pair our echelon with an email, so if i moved to a new machine,
i could re-establish echelon using email and code" + "the mcp server should be
accepting email and code — it will close vulnerability too."

The split that closes the vulnerability: the WEB tier only requests codes and
resets passphrases; TOKEN issuance happens at the MCP gateway's
POST /recover/exchange — so a bearer token never renders in a browser page.
Both tiers share THIS store (codes are hashed, expiring, attempt-capped);
apps/web reaches it through the services gate, the gateway uses it directly.

Pairing lives in the web accounts store (web_accounts.json) as `recovery_email`;
this module READS that file (plain JSON — no apps import, layering stays clean).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

CODE_TTL = int(os.environ.get("ECHELON_RECOVERY_TTL", "900"))       # 15 min
MAX_ATTEMPTS = 5
RESEND_FLOOR = 60  # seconds between code emails per account


def _home() -> Path:
    return Path(os.environ.get("ECHELON_HOME", str(Path.home() / ".echelon")))


def _pending_path() -> Path:
    return _home() / "recovery_pending.json"


def _accounts_path() -> Path:
    return Path(os.environ.get("ECHELON_WEB_ACCOUNTS",
                               str(_home() / "web_accounts.json")))


def _load_pending() -> dict:
    try:
        return json.loads(_pending_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pending(d: dict) -> None:
    p = _pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def find_paired_account(email: str) -> dict | None:
    """Resolve a recovery request against the web accounts store: the address must
    match an account's paired recovery_email (or its login email IF that account
    has a recovery email paired). No pairing → not email-recoverable (opt-in).
    Returns the account dict WITHOUT credential fields."""
    e = (email or "").strip().lower()
    if not e:
        return None
    try:
        accounts = json.loads(_accounts_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    for acct in accounts:
        rec = (acct.get("recovery_email") or "").strip().lower()
        if rec and e in (rec, acct.get("email", "").lower()):
            return {k: v for k, v in acct.items()
                    if k not in ("passphrase_hash", "salt")}
    return None


def issue_code(account_email: str) -> str | None:
    """Mint + record a pending 8-digit code for the account. None = resend floor
    (a code was issued less than RESEND_FLOOR seconds ago)."""
    pending = _load_pending()
    now = time.time()
    ent = pending.get(account_email)
    if ent and now - ent.get("ts", 0) < RESEND_FLOOR:
        return None
    code = f"{secrets.randbelow(10**8):08d}"
    pending[account_email] = {"h": _code_hash(code), "exp": now + CODE_TTL,
                              "attempts": 0, "ts": now}
    pending = {k: v for k, v in pending.items() if v.get("exp", 0) > now}
    _save_pending(pending)
    return code


def check_code(account_email: str, code: str) -> bool:
    """Constant-time check against the pending entry; burns an attempt on miss,
    burns the entry on success or exhaustion. A code works ONCE."""
    pending = _load_pending()
    ent = pending.get(account_email)
    if not ent or ent.get("exp", 0) < time.time():
        pending.pop(account_email, None)
        _save_pending(pending)
        return False
    if ent.get("attempts", 0) >= MAX_ATTEMPTS:
        pending.pop(account_email, None)
        _save_pending(pending)
        return False
    ok = secrets.compare_digest(ent.get("h", ""), _code_hash(code or ""))
    if ok:
        pending.pop(account_email, None)
    else:
        ent["attempts"] = ent.get("attempts", 0) + 1
        pending[account_email] = ent
    _save_pending(pending)
    return ok
