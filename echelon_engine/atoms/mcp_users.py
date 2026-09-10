"""mcp_users — the token → user-bank registry for the SINGLE-PORT multi-user gateway.

THE MODEL (owner 2026-07-02, "single port, db name follows the user"): the one
`echelon-mcp --http` gateway on :8888 serves MANY users. A caller proves identity
with their BEARER TOKEN; that token maps to a USER RECORD {db, scope} and the
gateway opens `<ECHELON_HOME_ROOT>/<db>.db` for that request. So isolation is
per-user DB FILE, resolved per request — no second port, no second service.

Registry file: `$ECHELON_MCP_USERS` (default `<home-root>/mcp_users.json`), shape:
    {
      "<bearer-token>": {"db": "alice", "scope": "alice", "email": "a@x.com"},
      ...
    }
The DB lives at `<home-root>/<db>` — a per-user WORK FOLDER (so core.db + echelon.db
live under `<home-root>/<db>/`). `<home-root>` is the DIRECTORY that holds the default
bank (dirname of $ECHELON_HOME, default `~/.echelon`'s parent is `~`, so we treat the
resolved default home's PARENT as the root and each user gets a sibling folder).

BACK-COMPAT (load-bearing): a token that is the gateway's OWN `ECHELON_MCP_TOKEN`
(the owner/default), OR any token not in the registry on a loopback dev box, resolves
to the DEFAULT home (unchanged behavior). So adding the registry never breaks the
existing single-user :8888 — the owner keeps their bank, new users get their own.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _root_dir() -> Path:
    """The directory under which per-user work folders live. Each user's bank is a
    sibling folder of the default home: default home `~/.echelon` → users live at
    `~/.echelon/<db>/`. We nest under the default home so one $ECHELON_HOME_ROOT
    override relocates everything."""
    from .echelon_home import echelon_home
    return Path(os.environ.get("ECHELON_MCP_USERS_ROOT", str(echelon_home())))


def registry_path() -> Path:
    return Path(os.environ.get("ECHELON_MCP_USERS", str(_root_dir() / "mcp_users.json")))


def load_registry() -> dict:
    p = registry_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _valid_db_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name or ""))


def resolve(token: str | None) -> dict | None:
    """Return the user record for a bearer token, or None if unknown.
    Record = {"db": <safe-name>, "scope": <str>, "home": <abs work-folder>, ...}."""
    if not token:
        return None
    reg = load_registry()
    rec = reg.get(token.strip())
    if not isinstance(rec, dict):
        return None
    db = str(rec.get("db") or rec.get("scope") or "").strip()
    if not _valid_db_name(db):
        return None
    scope = str(rec.get("scope") or db).strip()
    home = str((_root_dir() / db).resolve())
    return {"db": db, "scope": scope, "home": home,
            "email": rec.get("email"), "role": rec.get("role")}


def add_user(*, db: str, scope: str | None = None, token: str | None = None,
             email: str | None = None, role: str = "member") -> dict:
    """Register a new user: a db/work-folder name + a bearer token. Returns the
    record (with the token). Generates a token if none given. Idempotent on db name
    (re-registering overwrites that user's record). Owner/admin op."""
    import secrets
    db = db.strip()
    if not _valid_db_name(db):
        raise ValueError(f"invalid db name {db!r} — use [a-z0-9_-], <=64 chars, no leading dash")
    token = (token or secrets.token_urlsafe(32)).strip()
    scope = (scope or db).strip()
    reg = load_registry()
    # idempotent on db name means the OLD token must die with the re-register —
    # the registry is keyed by token, so drop stale entries for this db first
    # (rotation exists so a lost machine's token stops working).
    reg = {t: r for t, r in reg.items() if (r or {}).get("db") != db}
    reg[token] = {"db": db, "scope": scope, "email": email, "role": role}
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"db": db, "scope": scope, "token": token,
            "home": str((_root_dir() / db).resolve()), "email": email, "role": role}


def rotate_user(db: str | None = None, email: str | None = None) -> dict:
    """Rotate an existing user's token: same db/scope/email/role, a fresh bearer
    token, old token dead. Select the user by db name (the handle/username) OR by
    registered email — give exactly one. Raises ValueError if it isn't registered
    (rotate is not register — call `add_user` for a new user)."""
    reg = load_registry()
    rec = None
    if email:
        e = (email or "").strip().lower()
        rec = next((r for r in reg.values()
                    if (r or {}).get("email", "").strip().lower() == e), None)
        if rec is None:
            raise ValueError(f"no registered user with email {email!r} — nothing to rotate")
    elif db:
        d = db.strip()
        rec = next((r for r in reg.values() if (r or {}).get("db") == d), None)
        if rec is None:
            raise ValueError(f"no registered user with db {d!r} — nothing to rotate")
    else:
        raise ValueError("rotate needs --db <handle> or --email <address>")
    return add_user(db=str(rec.get("db")), scope=rec.get("scope"), email=rec.get("email"),
                    role=rec.get("role") or "member")


def bearer_from_headers(headers, path: str = "") -> str | None:
    """Extract the raw bearer token a client sent, the same three ways the gateway
    accepts it (Authorization: Bearer, X-Api-Key/token headers, ?token= query)."""
    auth = (headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    if auth:
        return auth
    for h in ("X-Api-Key", "token", "Token", "x-token", "Api-Key", "x-goog-api-key"):
        v = (headers.get(h) or "").strip()
        if v:
            return v
    if path:
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(path).query)
        for k in ("token", "Authorization", "api_key", "key"):
            vals = [v.rstrip(")") for v in (q.get(k) or [])]
            if vals:
                return vals[0]
    return None


def main(argv=None) -> int:
    """CLI: register / list users. `echelon mcp-users add --db alice [--token T]`."""
    import argparse
    ap = argparse.ArgumentParser(prog="echelon mcp-users",
                                 description="Manage the single-port gateway's token→user-bank registry.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="register a user (db name + token).")
    a.add_argument("--db", required=True, help="user's db/work-folder name ([a-z0-9_-]).")
    a.add_argument("--scope", default=None, help="memory scope (default = db name).")
    a.add_argument("--token", default=None, help="bearer token (default: generate one).")
    a.add_argument("--email", default=None)
    a.add_argument("--role", default="member")
    sub.add_parser("list", help="list registered users (tokens masked).")
    r = sub.add_parser("rotate", help="issue a fresh token for an existing user; old token dies.")
    r.add_argument("--db", default=None, help="user's db/work-folder name (must already be registered).")
    r.add_argument("--email", default=None, help="the user's registered email (either --db or --email, not both).")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        rec = add_user(db=args.db, scope=args.scope, token=args.token,
                       email=args.email, role=args.role)
        print("registered user:")
        print(f"  db     = {rec['db']}")
        print(f"  scope  = {rec['scope']}")
        print(f"  home   = {rec['home']}")
        print(f"  token  = {rec['token']}   <-- give this to the user (keep secret)")
        print(f"  registry = {registry_path()}")
        return 0
    if args.cmd == "rotate":
        try:
            rec = rotate_user(db=args.db, email=args.email)
        except ValueError as e:
            print(f"error: {e}")
            return 1
        print("rotated user:")
        print(f"  db     = {rec['db']}")
        print(f"  scope  = {rec['scope']}")
        print(f"  token  = {rec['token']}   <-- new token; old one is now dead")
        print(f"  registry = {registry_path()}")
        return 0
    if args.cmd == "list":
        reg = load_registry()
        if not reg:
            print(f"(no users registered) — {registry_path()}")
            return 0
        for tok, rec in reg.items():
            masked = tok[:6] + "…" + tok[-4:] if len(tok) > 12 else "…"
            print(f"  {rec.get('db','?'):20s} scope={rec.get('scope','?'):16s} "
                  f"token={masked}  {rec.get('email') or ''}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
