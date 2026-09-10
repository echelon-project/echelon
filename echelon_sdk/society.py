"""society — the role→cartridge→protocol registry, protocol seeds, brief composer,
and the non-destructive check-mail peek, that turn the EchelonBus (the comms organ)
into a SOCIETY of persistent gated peers.

Promoted from the PROVEN scratchpad prototype (busctl.py / watcher.py). The bus is the
plumbing; this module is the SOCIETY layer on top of it:

  ROLE REGISTRY (FORK-1 lean): a role NAME resolves its cartridge(s) + protocol. The
    ECHELON-native store is BANK ATOMS in scope `echelon` (a role-binding that produced
    good gated work should WARM); a JSON fallback at ~/.echelon/society/roles.json is the
    reliable cold-start read path. We ALWAYS keep the JSON in sync so reads never depend on
    a live bank; we ADDITIONALLY mirror to atoms (best-effort) so the binding can earn.

  PROTOCOL SEEDS (FORK-4 lean): a dict name→prose seed. Each seed states the rules of
    engagement for a role. Hardened to a state machine only if one proves load-bearing.

  CHECK-MAIL (FORK-2 lean): a NON-DESTRUCTIVE peek-since-last-peek for humans. The watcher
    uses bus.drain() (destructive cursor) internally; humans peek without consuming, via a
    SEPARATE peek-cursor table so a peek never advances the watcher's read cursor.

  BRIEF COMPOSER: the watcher's turn-1 seed is MACHINE-GENERATED from role+cartridge+
    protocol (the owner's "briefings as command+flags, not hand-written .txt"). It injects
    the ABSOLUTE `echelon bus post ...` command (the relative-path trap — banked) and the
    equip command (so the harness runs it witnessed).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# ── home + paths ────────────────────────────────────────────────────────────────

SOCIETY_HOME = Path.home() / ".echelon" / "society"
ROLES_JSON = SOCIETY_HOME / "roles.json"
ROLE_SCOPE = "echelon"   # FORK-1: role atoms file under scope `echelon`


def session_bus_path(session: str = "default", bus_override: str | Path | None = None) -> Path:
    """Resolve the session-scoped bus db. A society is session-scoped + tail-f-watchable:
    default ~/.echelon/society/<session>/bus.db (+ bus.jsonl mirror). --bus overrides."""
    if bus_override:
        return Path(bus_override)
    return SOCIETY_HOME / session / "bus.db"


def jsonl_mirror_for(bus_db: Path) -> Path:
    """The jsonl mirror sits next to the db (a human tail -f's it)."""
    return bus_db.with_suffix(".jsonl")


# ── PROTOCOL SEEDS (FORK-4: named prose seeds) ──────────────────────────────────

PROTOCOL_SEEDS: dict[str, str] = {
    "persistent-gated-build": (
        "PROTOCOL persistent-gated-build — you are a BUILDER.\n"
        "Build ONE gated slice from the spec the architect posts. ADDITIVE only — do not "
        "break existing behavior. Self-test before you report: run the named test suite AND "
        "the named trap (distrust exit-0 — read the output, do not trust a green exit alone). "
        "Write a FILE deliverable (the reliable channel) AND post a one-line bus DONE with the "
        "evidence. Then WAIT for the next slice — your context PERSISTS across turns, do NOT "
        "re-ground each turn. If blocked, post a precise question and wait."
    ),
    "skeptic-verify": (
        "PROTOCOL skeptic-verify — you are an AUDITOR.\n"
        "You did NOT build this. Trust nothing on its word. Re-derive every load-bearing claim "
        "from SOURCE (read the files, not the report). Run the suite YOURSELF. Hunt the trap "
        "that breaks it. Then CLEAR (with what you checked) or BLOCK (with file:line evidence). "
        "The clearing must come from a context that did not build it — that is your whole value."
    ),
    "orchestrate-gate": (
        "PROTOCOL orchestrate-gate — you are the ARCHITECT.\n"
        "Run the loop: spec → dispatch a slice → GATE the deliverable (run the suite independently, "
        "read the diff, run a live smoke) → deploy only on the owner's button. Decompose the goal "
        "into the smallest gated slices. Never merge your own un-gated work. Relay the owner; the "
        "owner holds the deploy seat."
    ),
    "doc-from-code": (
        "PROTOCOL doc-from-code — you are a SCRIBE.\n"
        "Read the CODE, not the chat, and write docs that match what the code actually does. Quote "
        "file:line. Do not invent behavior. Flag any gap between a doc claim and the source. Write a "
        "file deliverable and post a bus DONE."
    ),
}

DEFAULT_PROTOCOL = "persistent-gated-build"


def protocol_seed(name: str) -> str:
    """The prose seed for a protocol name, or a generic fallback so an unknown protocol
    never produces an empty brief (degrade, never crash)."""
    seed = PROTOCOL_SEEDS.get(name)
    if seed:
        return seed
    return (
        f"PROTOCOL {name} — (no named seed; generic society rules).\n"
        "Do the work the architect posts. Self-test, distrust exit-0, write a file deliverable, "
        "post a bus DONE, then WAIT — your context PERSISTS, do not re-ground each turn."
    )


# ── ROLE REGISTRY (FORK-1: atoms scope echelon + JSON fallback) ─────────────────

_SEED_ROLES: dict[str, dict] = {
    "architect": {"cartridge": ["act-ready", "ux"], "protocol": "orchestrate-gate"},
    "builder":   {"cartridge": ["frontend-build"], "protocol": "persistent-gated-build"},
    "auditor":   {"cartridge": ["intent"],          "protocol": "skeptic-verify"},
    "scribe":    {"cartridge": ["scribe"],           "protocol": "doc-from-code"},
}


def _load_roles_json() -> dict[str, dict]:
    """Read the JSON fallback (the reliable read path). Seeds it on first use."""
    if not ROLES_JSON.exists():
        _save_roles_json(dict(_SEED_ROLES))
        return dict(_SEED_ROLES)
    try:
        data = json.loads(ROLES_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    _save_roles_json(dict(_SEED_ROLES))
    return dict(_SEED_ROLES)


def _save_roles_json(roles: dict[str, dict]) -> None:
    ROLES_JSON.parent.mkdir(parents=True, exist_ok=True)
    ROLES_JSON.write_text(json.dumps(roles, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def _normalize_role(spec: dict) -> dict:
    """Coerce a role spec to {cartridge:[...], protocol:str}."""
    cart = spec.get("cartridge", [])
    if isinstance(cart, str):
        cart = [c.strip() for c in cart.split(",") if c.strip()]
    return {"cartridge": list(cart), "protocol": spec.get("protocol", DEFAULT_PROTOCOL)}


def list_roles() -> dict[str, dict]:
    """All registered roles (JSON is the source of truth for reads)."""
    return {k: _normalize_role(v) for k, v in _load_roles_json().items()}


def resolve_role(name: str) -> dict | None:
    """Resolve a role NAME → {cartridge:[...], protocol:str}. None if unknown."""
    roles = _load_roles_json()
    spec = roles.get(name)
    if spec is None:
        return None
    return _normalize_role(spec)


def add_role(name: str, cartridge: list[str] | str, protocol: str = DEFAULT_PROTOCOL,
             *, mirror_atom: bool = True) -> dict:
    """Register/overwrite a role. Always persists to JSON; best-effort mirrors to a bank
    atom in scope `echelon` so a good binding can earn (FORK-1). The atom mirror NEVER
    fails the call — JSON is the reliable path."""
    spec = _normalize_role({"cartridge": cartridge, "protocol": protocol})
    roles = _load_roles_json()
    roles[name] = spec
    _save_roles_json(roles)
    if mirror_atom:
        _mirror_role_atom(name, spec)
    return spec


def _mirror_role_atom(name: str, spec: dict) -> bool:
    """Best-effort: write the role binding as an .md atom file under the society home and
    ingest it into scope `echelon`. Returns True on success, False (silently) otherwise —
    the bank is a bonus, not a dependency."""
    try:
        atoms_dir = SOCIETY_HOME / "role_atoms"
        atoms_dir.mkdir(parents=True, exist_ok=True)
        slug = f"society-role-{name}"
        cart = ", ".join(spec["cartridge"]) or "(none)"
        body = (
            f"---\n"
            f"name: {slug}\n"
            f"description: society role '{name}' binds cartridge [{cart}] + protocol "
            f"'{spec['protocol']}'\n"
            f"metadata:\n"
            f"  type: reference\n"
            f"---\n\n"
            f"Society role **{name}** auto-equips cartridge(s) [{cart}] and runs under "
            f"protocol `{spec['protocol']}`. A watcher resolving this role looks up this "
            f"binding, equips the cartridge(s), and composes the protocol seed into turn 1.\n"
        )
        (atoms_dir / f"{slug}.md").write_text(body, encoding="utf-8")
        return True
    except OSError:
        return False


# ── CHECK-MAIL: non-destructive peek-since-last-peek (FORK-2) ───────────────────
#
# A SEPARATE peek-cursor table in the SAME bus db, so a human peek never touches the
# watcher's drain cursor. Implemented here (not in bus.py) to keep the comms organ
# untouched — additive.

_PEEK_SCHEMA = """
CREATE TABLE IF NOT EXISTS peek_cursors (
    reader   TEXT NOT NULL,
    channel  TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (reader, channel));
"""


def check_mail(bus_db: Path, reader: str, channel: str, *, advance: bool = True,
               reserved_ok: bool = False) -> list[dict]:
    """NON-destructive peek: return messages on `channel` newer than `reader`'s PEEK cursor.
    Unlike bus.drain(), this uses an independent peek_cursors table, so peeking never
    consumes a watcher's drain stream. With advance=True (default) the peek cursor moves to
    the channel tip so the next check-mail shows only newer mail; the underlying messages are
    untouched (history() still returns everything). advance=False = pure read, no cursor move.

    RESERVED readers (wake cursors) refuse an ADVANCING peek unless reserved_ok=True — the
    cursor-poison trap (learned 2026-07-02: one hand-run check-mail on a wake cursor and the
    next real message silently fails to wake the agent) ENFORCED, not just warned about."""
    conn = sqlite3.connect(str(bus_db), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_PEEK_SCHEMA)
        if advance and not reserved_ok:
            try:
                r = conn.execute("SELECT note FROM reserved_readers WHERE reader=?",
                                 (reader,)).fetchone()
            except sqlite3.OperationalError:
                r = None  # bus predates the reserved_readers table — nothing reserved
            if r:
                raise PermissionError(
                    f"reader {reader!r} is RESERVED ({r['note'] or 'wake cursor'}) — an "
                    f"advancing check-mail poisons its wake. Use `bus tail`, a different "
                    f"reader id, or --no-advance.")
        row = conn.execute(
            "SELECT last_seq FROM peek_cursors WHERE reader=? AND channel=?",
            (reader, channel)).fetchone()
        last_seq = row["last_seq"] if row else 0
        rows = conn.execute(
            "SELECT seq, channel, sender, body, meta, ts FROM messages "
            "WHERE channel=? AND seq>? ORDER BY seq",
            (channel, last_seq)).fetchall()
        out = [{"seq": r["seq"], "channel": r["channel"], "sender": r["sender"],
                "body": r["body"], "meta": json.loads(r["meta"] or "{}"), "ts": r["ts"]}
               for r in rows]
        if advance:
            tip = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM messages WHERE channel=?",
                (channel,)).fetchone()["m"]
            new_last = max(last_seq, tip)
            conn.execute(
                "INSERT INTO peek_cursors (reader, channel, last_seq) VALUES (?,?,?) "
                "ON CONFLICT(reader, channel) DO UPDATE SET last_seq=excluded.last_seq",
                (reader, channel, new_last))
            conn.commit()
        return out
    finally:
        conn.close()


# ── BRIEF COMPOSER: the machine-generated turn-1 seed ───────────────────────────


def compose_brief(role: str, channel: str, busctl_cmd: str, *,
                  cartridge: list[str] | None = None, protocol: str | None = None,
                  goal: str = "", extra: str = "") -> str:
    """Compose the watcher's turn-1 seed from role+cartridge+protocol (NOT a hand-written
    .txt). Injects the absolute equip command (run witnessed) and the absolute bus-post
    command (the relative-path trap — banked: busctl/the verb is NOT in the role's cwd).

    `busctl_cmd` is the absolute `echelon bus post {channel} {role}` prefix (caller builds it
    from sys.executable + the engine, so the role posts with a path that resolves)."""
    binding = resolve_role(role) or {}
    cart = cartridge if cartridge is not None else binding.get("cartridge", [])
    proto = protocol if protocol is not None else binding.get("protocol", DEFAULT_PROTOCOL)
    cart = cart or []

    lines: list[str] = []
    lines.append(f"# ECHELON SOCIETY — you are the persistent '{role}' peer on channel '{channel}'.")
    lines.append("")
    if goal:
        lines.append(f"GOAL: {goal}")
        lines.append("")
    lines.append(protocol_seed(proto))
    lines.append("")

    if cart:
        # The role auto-equips its cartridge(s) WITNESSED — run the equip command first.
        lines.append("EQUIP your cartridge(s) FIRST (run these, witnessed — they warm your ground):")
        for c in cart:
            goal_arg = goal.replace('"', "'") if goal else role
            lines.append(f'  python -X utf8 -m echelon_engine cartridge equip {c} "{goal_arg}"')
        lines.append("")

    if extra:
        lines.append("EXTRA CONTEXT FROM THE ARCHITECT:")
        lines.append(extra)
        lines.append("")

    lines.append("[Your context PERSISTS across turns — do NOT re-ground each turn. When a new bus")
    lines.append("message arrives it is delivered as your next user turn. POST every reply with this")
    lines.append("EXACT absolute command (the bus verb is NOT in your cwd):")
    lines.append(f'  {busctl_cmd} "<msg>"')
    lines.append("On 'STAND DOWN', post a final summary and stop. Acknowledge readiness now with a")
    lines.append("short bus post, then wait for the next turn.]")
    return "\n".join(lines)
