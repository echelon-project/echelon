"""echelon_engine.services_memory — the MEMORY vertical's private chain entrypoints.

The `services.py` gate re-exports these; apps (apps/web, apps/cli) import them ONLY
through that gate (the scanner forbids apps -> echelon_engine.atoms directly). This is
the memory-read migration the gate file anticipated ("services_memory ... as verticals
migrate"). Reads are in-process (no subprocess, no model call) — the same fast path
proxy.py:_live_stats uses, lifted to the gate so the web app can reach it lawfully.

All functions return plain JSON-able dicts/lists — never raw Atom objects — so the
apps layer never needs to import atom types.
"""
from __future__ import annotations

import hmac
import os
from typing import Any

from echelon_engine.atoms.cards import CardStore, Atom


def _store(db_path: str | None = None) -> CardStore:
    return CardStore(db_path) if db_path else CardStore()


def _atom_dict(a: Atom) -> dict[str, Any]:
    """Serialize an Atom to a plain dict (the only place apps see atom shape)."""
    return {
        "id": a.id,
        "coordinate": a.coordinate,
        "content": a.content,
        "score": a.score,
        "use_count": a.use_count,
        "born_from": a.born_from,
        "ts": a.ts,
        "scope": a.scope,
        "kind": a.kind,
        "valence": a.valence,
        "arousal": a.arousal,
        "tier": a.tier,
    }


def bank_stats(scope: str = "echelon", db_path: str | None = None) -> dict[str, Any]:
    """Live bank counts (atoms, earned_weight, cartridges) — mirrors proxy.py:_live_stats."""
    cs = _store(db_path)
    n = cs.count_atoms_in_scope(scope)
    rows = cs.conn.execute(
        "SELECT SUM(e.score) FROM atoms a JOIN atom_earned e ON a.id=e.atom_id WHERE a.scope=?", (scope,)).fetchone()
    earned_weight = round(float(rows[0] or 0), 1)
    from echelon_engine.atoms.cartridge_registry import all_specs
    cart_count = len(list(all_specs()))
    return {
        "scope": scope,
        "atoms": n,
        "earned_weight": earned_weight,
        "cartridges": cart_count,
        "bank": str(cs.db_path),
    }


def list_atoms(scope: str | None = "echelon", limit: int = 50, offset: int = 0,
               db_path: str | None = None) -> dict[str, Any]:
    """Paginated atoms in scope. Honest empty when none."""
    cs = _store(db_path)
    atoms = cs.atoms_in_scope(scope)
    total = len(atoms)
    page = atoms[offset:offset + limit]
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_atom_dict(a) for a in page]}


def get_atom(coordinate: str, db_path: str | None = None) -> dict[str, Any] | None:
    """One atom's full body by coordinate/slug (the remember read). None if missing."""
    cs = _store(db_path)
    atom_id = cs.atom_id_for_coordinate(coordinate)
    if not atom_id:
        return None
    body = cs.remember_fetch(atom_id, depth="body")
    if body is not None:
        return body
    atom = cs.get_atom(atom_id)
    return _atom_dict(atom) if atom is not None else None


def _open_seed_store(db_path: str | None):
    """Open a SeedStore the way the CLI does — with BOTH databases.

    THE BUG THIS FIXES (2026-08-15): `SeedStore(db_path)` passes the v2 bank as the
    CORE db, so warmth read the wrong store and returned `cold` / 0 atoms for a bank
    that plainly contained the answer. The CLI opens `SeedStore(core.db,
    v2_db=echelon.db)` and returned `lukewarm` with the same query on the same bank —
    two paths disagreeing about one bank.

    Symptom for a new user: they write their first memory, ask about it, and are told
    "I don't have any record of that" — the product's core promise failing on the
    first try, with the atom sitting right there. Given a v2 path, derive its core.db
    sibling; a caller passing a core.db path already gets the historical behavior.
    """
    from echelon_engine.atoms.recall import SeedStore
    if not db_path:
        return SeedStore()
    from pathlib import Path as _P
    p = _P(db_path)
    core = p.parent / "core.db"
    if p.name != "core.db" and core.exists():
        return SeedStore(str(core), v2_db=str(p))
    return SeedStore(str(p))


def search_atoms(query: str, scope: str = "echelon", limit: int = 10,
                 db_path: str | None = None) -> dict[str, Any]:
    """The web RECALL — real foveated warmth through the witnessed door (NOT lexical
    term-counting). A param query only, never raw argv. Returns the warmth VERDICT
    (warm/cold), the score, the guidance, and the warmest atoms — the same reading
    `recall --warm` gives, surfaced for the console. Honest empty if nothing surfaces."""
    from echelon_engine.atoms.recall import SeedStore
    from echelon_engine.atoms.warmth import warmth
    store = _open_seed_store(db_path)
    try:
        r = warmth(query, store, scope=scope, top_k=max(limit, 3))
    except Exception:
        # graceful fall-back to a lexical pass if warmth can't run (keeps the screen alive)
        return _lexical_search(query, scope, limit, db_path)

    out = []
    for sw in (r.warmest or [])[:limit]:
        seed = getattr(sw, "seed", None)
        if seed is None:
            continue
        out.append({
            "id": getattr(seed, "id", ""),
            "coordinate": getattr(seed, "coordinate", "") or getattr(seed, "slug", ""),
            "content": getattr(seed, "content", ""),
            "score": round(getattr(sw, "score", 0.0), 3),
            "via_scope": getattr(sw, "via_scope", ""),
        })
    return {"query": query, "scope": scope,
            "verdict": r.verdict, "score": round(r.score, 3),
            "guidance": r.guidance, "count": len(out), "atoms": out}


def _lexical_search(query: str, scope: str, limit: int, db_path: str | None) -> dict[str, Any]:
    """Crude lexical fallback (only if real warmth errors)."""
    cs = _store(db_path)
    atoms = cs.atoms_in_scope(scope)
    terms = query.lower().split()
    scored = []
    for a in atoms:
        content = (a.content or "").lower()
        m = sum(1 for t in terms if t in content)
        if m > 0:
            scored.append((a, m))
    scored.sort(key=lambda x: (-x[1], -x[0].score))
    warmest = [_atom_dict(a) for a, _ in scored[:limit]]
    return {"query": query, "scope": scope,
            "verdict": "found" if warmest else "empty",
            "count": len(warmest), "atoms": warmest, "guidance": "(lexical fallback)"}


def list_arc_cards(limit: int = 8, db_path: str | None = None) -> list[dict[str, Any]]:
    """Recent session arc-cards (the relive list)."""
    from echelon_engine.atoms.relive import recent_arc_cards
    cs = _store(db_path)
    return recent_arc_cards(cs=cs, limit=limit)


def relive_chain(card_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    """One session chain (relive by id). None if not found."""
    from echelon_engine.atoms.relive import relive, render
    cs = _store(db_path)
    links, err = relive(card_id, cs=cs, take_up=False)
    if err:
        return None
    return {"card_id": card_id, "chain": links, "rendered": render(links, card_id)}


def cartridge_list() -> list[dict[str, Any]]:
    """The cartridge registry, as plain dicts."""
    from echelon_engine.atoms.cartridge_registry import all_specs
    out = []
    for s in all_specs():
        out.append({"name": s.name, "scope": getattr(s, "scope", ""),
                    "summary": s.summary, "label": getattr(s, "label", "")})
    return out


def provider_config() -> dict[str, Any]:
    """Routing/provider config with any secret-ish fields redacted."""
    from echelon_engine.atoms import routing
    roles: dict[str, Any] = {}
    src = getattr(routing, "_ROLES", {}) or {}
    for role_name, cfg in src.items():
        if isinstance(cfg, dict):
            safe = dict(cfg)
            for k in list(safe.keys()):
                if any(w in k.lower() for w in ("key", "token", "secret", "password")):
                    safe[k] = "***REDACTED***"
            roles[role_name] = safe
        else:
            roles[role_name] = cfg
    table_path = getattr(routing, "_TABLE_PATH", None)
    return {"table_path": str(table_path) if table_path else None, "roles": roles}


def integrity_scan(db_path: str | None = None) -> dict[str, Any]:
    """Cheap integrity read: duplicate-coordinate conflicts across the bank."""
    cs = _store(db_path)
    atoms = cs.atoms_in_scope(None)
    coords: dict[str, list[str]] = {}
    for a in atoms:
        coords.setdefault(a.coordinate, []).append(a.id)
    conflicts = {c: ids for c, ids in coords.items() if len(ids) > 1}
    return {"total_atoms": len(atoms), "conflicts": len(conflicts),
            "conflict_details": [{"coordinate": c, "ids": ids}
                                 for c, ids in list(conflicts.items())[:20]]}


def bank_health(db_path: str | None = None) -> dict[str, Any]:
    """Service+bank health for the admin dashboard."""
    cs = _store(db_path)
    n = cs.count_atoms_in_scope(None)
    return {"service": "running",
            "bank": "healthy" if n > 0 else "empty",
            "atom_count": n}


# ── Scopes (de-default echelon — the bank holds many) ────────────────────────

def list_scopes(db_path: str | None = None) -> list[dict[str, Any]]:
    """Every scope in the bank with its atom count + earned count, busiest first.
    Feeds the console's global scope selector (the bank is NOT echelon-only)."""
    import collections
    cs = _store(db_path)
    atoms = cs.atoms_in_scope(None)
    by_scope: dict[str, list] = collections.defaultdict(list)
    for a in atoms:
        s = a.scope or "(unscoped)"
        by_scope[s].append(a)
    out = []
    for s, lst in by_scope.items():
        out.append({"scope": s, "atoms": len(lst),
                    "earned_count": sum(1 for a in lst if getattr(a, "use_count", 0) > 0)})
    out.sort(key=lambda x: -x["atoms"])
    return out


# ── Atlas (reuse the proven force-directed renderer) ─────────────────────────

def atlas_html(scope: str = "echelon", db_path: str | None = None) -> str:
    """The live atlas as standalone HTML — reuses graph_viz.build + render_html (the
    SAME force-directed, tier-colored map `echelon graph` and the old console show).
    Returned as an HTML blob the SPA embeds in a panel (srcdoc/iframe)."""
    from echelon_engine.atoms.graph_viz import build, render_html
    data = build(scope=scope) if db_path is None else build(scope=scope, db_path=db_path)
    return render_html(data)


# ── Cartridge inspect (read the atoms + card a cartridge composes) ───────────

def cartridge_inspect(name: str) -> dict[str, Any] | None:
    """One cartridge's full spec + the equip command. Read-only, free (no model call,
    no compose). The web NEVER triggers equip/compose (those earn/cost) — it surfaces
    the command to copy into the admin Raw-CLI console."""
    from echelon_engine.atoms.cartridge_registry import all_specs
    for s in all_specs():
        if s.name == name:
            refs = list(getattr(s, "refs", []) or [])
            return {
                "name": s.name,
                "scope": getattr(s, "scope", ""),
                "label": getattr(s, "label", ""),
                "summary": getattr(s, "summary", ""),
                "skill": getattr(s, "skill", ""),
                "refs": refs,
                "atom_count": len(refs),
                "equip_command": f"cartridge equip {s.name} \"<your goal>\"",
            }
    return None


# ── Provider key PRESENCE (✓/✗ only — value NEVER returned) ───────────────────

def provider_key_status() -> list[dict[str, Any]]:
    """Which providers have a key configured. Returns presence ONLY — the key VALUE is
    never read into the response (the owner-secret boundary: the web cannot exfil or
    write .apikey). 'configured' = the named loader resolves a non-empty key."""
    from echelon_sdk import keys as K
    checks = [
        ("deepseek", "load_deepseek_key"),
        ("xai/grok", "load_xai_key"),
        ("gemini",   "load_gemini_key"),
        ("lmstudio", "load_lmstudio_key"),
    ]
    out = []
    for label, fn_name in checks:
        present = False
        fn = getattr(K, fn_name, None)
        if fn is not None:
            try:
                present = bool(fn())          # value used ONLY for the truthiness check
            except Exception:
                present = False
        out.append({"provider": label, "configured": present})
    return out


def _provider_of_model(model_id: str) -> str:
    """Map a journal model id to its provider label (the /api/keys row it belongs under)."""
    m = (model_id or "").lower()
    if "deepseek" in m:
        return "deepseek"
    if "gemini" in m or m.startswith("models/"):
        return "gemini"
    if "grok" in m:
        return "xai/grok"
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "lmstudio" in m or "local" in m or "phi" in m or "smollm" in m or "gemma" in m:
        return "lmstudio"
    return "other"


def provider_usage(alert_usd: float | None = None) -> dict[str, Any]:
    """HONEST per-provider spend from the persistent budget journals (Account-Pool MVP
    verdict, council 2026-07-30: single-key + real usage, no fake pool). Reads every
    journal in the budget dir; aggregates usd/calls/tokens per provider and in total.
    Numbers come ONLY from recorded charges — a provider with no journal lines shows 0,
    never an estimate. alert_usd (or ECHELON_BUDGET_ALERT_USD) arms the overage flag."""
    import json as _json
    import os
    from echelon_engine.atoms.providers.cost import _budget_journal_dir
    if alert_usd is None:
        try:
            alert_usd = float(os.environ.get("ECHELON_BUDGET_ALERT_USD", "") or 0) or None
        except ValueError:
            alert_usd = None
    per: dict[str, dict[str, Any]] = {}
    total = {"usd": 0.0, "calls": 0, "tokens_in": 0, "tokens_out": 0}
    jdir = _budget_journal_dir()
    journals = sorted(jdir.glob("*.jsonl")) if jdir.exists() else []
    for jp in journals:
        try:
            lines = jp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except ValueError:
                continue
            prov = _provider_of_model(str(rec.get("model", "")))
            b = per.setdefault(prov, {"usd": 0.0, "calls": 0, "tokens_in": 0, "tokens_out": 0})
            usd = float(rec.get("usd", 0) or 0)
            b["usd"] += usd
            b["calls"] += 1
            b["tokens_in"] += int(rec.get("in", 0) or 0)
            b["tokens_out"] += int(rec.get("out", 0) or 0)
            total["usd"] += usd
            total["calls"] += 1
            total["tokens_in"] += int(rec.get("in", 0) or 0)
            total["tokens_out"] += int(rec.get("out", 0) or 0)
    for b in per.values():
        b["usd"] = round(b["usd"], 4)
    total["usd"] = round(total["usd"], 4)
    out: dict[str, Any] = {"providers": per, "total": total, "journals": len(journals)}
    if alert_usd:
        out["alert_usd"] = alert_usd
        out["alert"] = total["usd"] >= alert_usd
    return out


# ── Banner: the machine gate banner + an APPENDED user banner ────────────────

def _user_banner_path():
    from pathlib import Path
    import os
    return Path(os.environ.get("ECHELON_WEB_USER_BANNER",
                               str(Path.home() / ".echelon" / "web_user_banner.txt")))


def gate_banner(scope: str = "echelon") -> dict[str, Any]:
    """The current machine-generated gate banner (read-only, the single source of truth
    from gate.py) PLUS any appended user-defined banner. The user banner is a separate
    persisted block rendered UNDER the echelon banner — gate --write only ever replaces
    the machine block (above the first ---), so the two never clobber each other."""
    from echelon_engine.atoms.gate import gate_text
    machine = gate_text(scope)
    p = _user_banner_path()
    user = p.read_text(encoding="utf-8") if p.exists() else ""
    return {"machine_banner": machine, "user_banner": user}


def set_user_banner(text: str) -> dict[str, Any]:
    """Persist the user-defined banner appended under the echelon banner. This does NOT
    touch the machine gate banner (gate.py stays the source of truth for that block)."""
    p = _user_banner_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text or "", encoding="utf-8")
    return {"ok": True, "saved_chars": len(text or "")}


# ── Install / integrate guide (the consumer-onboarding door) ─────────────────

def _wheel_version() -> str:
    """The engine version the wheel is named with (pip validates the URL's wheel
    filename before downloading — the install door's URL carries the real version)."""
    try:
        from importlib import metadata
        return metadata.version("echelon")
    except Exception:
        return "0.0.0"


def install_guide(base_url: str = "") -> dict[str, Any]:
    """How to install / integrate the ECHELON substrate — the consumer onboarding doc,
    served as structured steps the Install pane renders. Honest: the substrate ADAPTS,
    it does not force ([[echelon-adapts-to-you-it-does-not-force]]). base_url lets the
    page show the live gateway it's served from."""
    host = base_url or "http://<this-host>"
    return {
        "intro": ("ECHELON is a memory + reasoning substrate you plug in. Install the CLI, "
                  "point it at this gateway, and recall/earn against your own bank. It adapts "
                  "to your workflow — it does not force a way of working."),
        "sections": [
            {"title": "1 · The REST door (no install)",
             "body": "Every substrate verb is reachable over HTTP. Run a command:",
             "code": f"curl -s -X POST {host}/cli -H 'content-type: application/json' "
                     f"-d '{{\"args\":[\"recall\",\"--scope\",\"echelon\",\"--warm\",\"<intent>\"]}}'"},
            {"title": "2 · Stream slow verbs (SSE)",
             "body": "Long verbs (sleep/dream/wrap) stream over /tail:",
             "code": f"curl -N '{host}/tail?a=recall&a=--warm&a=<intent>'"},
            {"title": "3 · The OpenAPI schema",
             "body": "Point any OpenAPI client / agent at the gateway's schema:",
             "code": f"{host}/openapi.json"},
            {"title": "4 · MCP (for Claude Code / agents)",
             "body": "The same substrate is an MCP server — add it to your agent's MCP config:",
             "code": f"{host}/mcp"},
            {"title": "5 · Install the CLI locally (full substrate)",
             "body": "For the witnessed door (recall earns, relive, wrap), install the engine as a "
                     "PACKAGE from this gateway with your MCP token — no source checkout:",
             "code": f"pip install \"{host}/api/install/"
                     f"echelon-{_wheel_version()}-py3-none-any.whl?token=<your-mcp-token>\"\n"
                     "echelon recall --scope <you> --warm \"<intent>\""},
            {"title": "6 · Cold install on a fresh box (steered)",
             "body": "The /steer door installs echelon onto a new box for you, one command per turn "
                     "(the proven cold-install loop). Ask the owner for an invite to onboard a box.",
             "code": f"{host}/steer/register"},
        ],
    }


# ── USER PROVISIONING (owner 2026-07-02) — the lawful door for apps/web onboarding.
# apps/web may NOT import echelon_engine.atoms directly (scanner import-law), so the
# MCP user-registry provisioning goes THROUGH this gate. Creates/rotates a teammate's
# MCP registry entry (token → isolated db/scope); their bank is lazily seeded
# teammate-safe on first MCP contact.
def provision_mcp_user(*, handle: str, email: str | None = None,
                       token: str | None = None, scope: str | None = None,
                       role: str = "member") -> dict[str, Any]:
    """Register (or rotate) an MCP user in the single-port registry. Returns the
    record {db, scope, token, home, email, role}. Raises ValueError on a bad handle."""
    from echelon_engine.atoms import mcp_users
    return mcp_users.add_user(db=handle, scope=scope or handle, token=token,
                              email=email, role=role)


def rotate_mcp_user(handle: str | None = None, email: str | None = None) -> dict[str, Any]:
    """Issue a fresh MCP token for an already-registered user; the old token dies
    immediately. Select by handle (db name) OR registered email — give exactly
    one. Returns {db, scope, token, home, email, role}. Raises ValueError if not
    registered (rotate is not register)."""
    from echelon_engine.atoms import mcp_users
    return mcp_users.rotate_user(db=handle, email=email)


def mcp_user_exists(handle: str) -> bool:
    """True if a registry entry with this db/handle already exists."""
    from echelon_engine.atoms import mcp_users
    reg = mcp_users.load_registry()
    return any((v or {}).get("db") == handle for v in reg.values())


def recovery_issue_code(account_email: str) -> str | None:
    """Mint a pending recovery code for an account (hashed, expiring, capped).
    None = resend floor. Apps-tier door to atoms/recovery."""
    from echelon_engine.atoms import recovery
    return recovery.issue_code(account_email)


def recovery_check_code(account_email: str, code: str) -> bool:
    """Burn-on-use check of a pending recovery code. Apps-tier door to atoms/recovery."""
    from echelon_engine.atoms import recovery
    return recovery.check_code(account_email, code)


def recovery_find_account(email: str) -> dict[str, Any] | None:
    """Resolve an address to its recovery-paired account (credential fields
    stripped). Apps-tier door to atoms/recovery."""
    from echelon_engine.atoms import recovery
    return recovery.find_paired_account(email)


def mcp_user_by_email(email: str) -> dict[str, Any] | None:
    """Find a registered MCP user by email → {db, scope, email, role} or None
    (token NOT included — lookups don't leak credentials)."""
    from echelon_engine.atoms import mcp_users
    e = (email or "").strip().lower()
    for rec in mcp_users.load_registry().values():
        if (rec or {}).get("email", "").strip().lower() == e:
            out = {k: rec.get(k) for k in ("db", "scope", "email", "role")}
            # `home` is COMPUTED at resolve time (root_dir/<db>), never stored in the
            # registry JSON — so callers must not reconstruct it from a guessed
            # convention. Resolve it here, through the registry's own root.
            try:
                out["home"] = str((mcp_users._root_dir() / str(rec.get("db"))).resolve())
            except Exception:
                out["home"] = None
            return out
    return None


def mcp_user_by_token(token: str | None, *, owner_token: str | None = None) -> dict[str, Any] | None:
    """Resolve a presented token the way the gateway authorizes it (mcp_server.authorized
    law): the OWNER token (if configured) OR a registered member token → {db, scope,
    home, role} or None. The token itself is never echoed back in the record.

    This is the apps-tier door for the /onboarding/install endpoint (token-gated
    engine download). Apps may not import atoms directly — this stays the gate."""
    if not token:
        return None
    presented = token.strip()
    owner = owner_token if owner_token is not None else os.environ.get("ECHELON_MCP_TOKEN")
    if owner and hmac.compare_digest(presented, owner):
        from echelon_engine.atoms.echelon_home import echelon_home
        return {"db": "echelon", "scope": "echelon",
                "home": str(echelon_home()), "role": "owner"}
    from echelon_engine.atoms import mcp_users
    return mcp_users.resolve(presented)


def mcp_bearer_token(headers: dict, path: str = "") -> str | None:
    """Extract the raw bearer token a client sent — the same three ways the gateway
    accepts it (Authorization: Bearer, token headers, ?token= query)."""
    from echelon_engine.atoms import mcp_users
    return mcp_users.bearer_from_headers(headers, path)


def mcp_token_for_email(email: str) -> str | None:
    """The raw registry TOKEN bound to a registered user's email — the ONE place a
    token is read OUT (the /api/install/resend-token door, emailing the account its
    own token). Every other lookup deliberately strips tokens (mcp_user_by_email)."""
    from echelon_engine.atoms import mcp_users
    e = (email or "").strip().lower()
    for tok, rec in mcp_users.load_registry().items():
        if (rec or {}).get("email", "").strip().lower() == e:
            return tok
    return None


def remember_lesson(lesson: str, *, scope: str, home: str | None = None,
                    label: str = "note") -> dict[str, Any]:
    """Plant ONE lesson sentence as an atom in `scope`. The apps-tier write door.

    WHY THIS EXISTS: a new user's bank is empty, and an empty bank demos an amnesiac —
    the assistant can only answer "my memory banks came up empty", which is the worst
    possible first impression for a MEMORY product. The onboarding's step 1 is therefore
    a WRITE: the user types one thing they know, and step 2 (ask) then has something
    real to find. The delta between empty and remembered is the whole value proposition,
    and only the user can create it — seeding demo atoms into a PERSONAL bank would be
    someone else's furniture.

    Writes to the caller's OWN home when given (single-port multi-user): the module
    default db is bound at import time, so an unqualified write on a long-lived server
    lands in the SERVER's bank, not the caller's — the cross-tenant wrap-write leak.

    Returns {ok, slug, coordinate} or {ok: False, error}.
    """
    import re as _re
    import time as _time

    text = (lesson or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to remember (empty lesson)"}
    if len(text) > 2000:
        return {"ok": False, "error": "lesson too long (2000 char max)"}
    scope = (scope or "").strip()
    if not scope:
        return {"ok": False, "error": "no scope resolved for this caller"}

    from pathlib import Path as _Path
    from echelon_engine.atoms.store import SeedStore
    from echelon_engine.atoms.cards import CardStore
    from echelon_engine.atoms.echelon_home import echelon_home

    base = _Path(home).expanduser() if home else echelon_home()
    slug_base = _re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "note"
    slug = f"{slug_base}-{_time.strftime('%Y%m%d-%H%M%S')}"
    coord = f"{scope}:{slug}"
    try:
        store = SeedStore(str(base / "core.db"), v2_db=str(base / "echelon.db"))
        store.remember(scope=scope, content=f"[{slug}] {text}", kind="feedback",
                       tier="core", coordinate=coord, valence=0.1, arousal=0.1)
        # compile the spine so recall/relive can resolve it immediately
        cards = CardStore(str(base / "echelon.db"))
        row = cards.conn.execute(
            "SELECT a.id FROM atoms a LEFT JOIN atom_spine s ON s.atom_id=a.id "
            "WHERE a.coordinate = ? AND s.atom_id IS NULL", (coord,)).fetchone()
        if row:
            try:
                cards.compile_atom_struct(row["id"])
            except Exception:
                pass          # spine compile is best-effort; the atom is planted
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{e!r}"}
    return {"ok": True, "slug": slug, "coordinate": coord}
