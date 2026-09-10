"""echelon-mcp — a local MCP server that EQUIPS the substrate for any MCP client
(LM Studio, Claude Desktop, …).

WHAT IT IS: a thin stdio MCP server that exposes the ECHELON substrate verbs as
MCP tools. Each tool SHELLS OUT to the `echelon` CLI (subprocess) — isolated,
robust, returns the exact CLI text the docs describe; no engine import-graph
(scanner/LLM providers) loaded into the server process. So the server starts
instantly and a tool call pays the engine cost only when invoked.

WHY raw protocol (no `mcp` SDK dependency): the install contract is "point your
MCP client at this script" — adding a `pip install mcp` step is friction. The MCP
stdio wire is plain JSON-RPC 2.0 over stdin/stdout; this implements the handshake
+ tools/list + tools/call directly with the stdlib. Same Python that runs echelon
runs this.

CONFIG = a WORK FOLDER (owner 2026-06-22). Set `ECHELON_HOME` to a folder; on
first start the server ensures it exists, has echelon CREATE + MIGRATE its dbs
into it (via the engine's own auto-migrating store open), and GENERATES a `.env`
template the user fills (api keys, web port, …). Everything the substrate writes
lives under that one folder — relocatable, per-install.

INSTALL (LM Studio → mcp.json):
    {
      "mcpServers": {
        "echelon": {
          "command": "python",
          "args": ["-m", "echelon_engine.mcp_server", "--home", "C:/echelon-bank"],
          "env": { "ECHELON_SCOPE": "echelon" }
        }
      }
    }
(`--home` is sugar for ECHELON_HOME; either works. ECHELON_SCOPE = default scope
for recall when the tool call omits one.)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-11-25"   # current Streamable-HTTP; we also echo the client's offered version
SERVER_NAME = "echelon"
SERVER_VERSION = "0.1.0"

# THE HOOK (owner 2026-06-22: "the same like hook on Claude"). MCP servers may
# return an `instructions` string on initialize; a well-behaved client injects it
# into the model's system context. This is the ECHELON GATE translated to MCP-tool
# verbs — the same four claims + the REFLEX/THINK routing law that boots Claude via
# MEMORY.md, but pointing at the echelon_* TOOLS instead of the CLI. Without it the
# model treats the tools as optional and answers "what is echelon" from training
# (the bug that exposed this). The canon lives in echelon_engine/atoms/gate.py;
# this is the deliberately-short tool-flavored door.
def _build_instructions() -> str:
    """Build the MCP initialize instructions — the REFLEX anchor for MCP clients.
    Includes live bank stats queried once at import time."""
    try:
        from echelon_engine.atoms.cards import CardStore
        cs = CardStore()
        n = cs.count_atoms_in_scope("echelon")
        earned = cs.conn.execute(
            "SELECT COUNT(*) FROM atom_earned WHERE use_count > 0 AND scope='echelon'"
        ).fetchone()[0]
        from echelon_engine.atoms.cartridge_registry import all_specs
        cart_count = len(list(all_specs()))
        stats = f"{n} atoms ({earned} earned) | {cart_count} cartridges | ~/.echelon/echelon.db"
    except Exception:
        stats = "~/.echelon/echelon.db"

    return f"""\
You are equipped with ECHELON — a persistent memory + reasoning SUBSTRATE that is YOURS.
Bank: {stats}

REFLEX — your operating doctrine (HOW you work, not what tools you have):
  ① RECALL before every load-bearing action. Use echelon_recall FIRST — it's free.
     WARM atoms are ground truth over your training. COLD = new ground, proceed honestly.
  ② WITNESS your reads. Use echelon_remember <slug> for full atom body — the fetch EARNS weight.
     Never trust your training's memory of what an atom says; only the witnessed read is truth.
  ③ DON'T ASK what the bank knows. Before asking the user, recall first — the bank may hold the answer.
  ④ DISPUTE atoms that misled you. If you followed a recalled atom and it was wrong, mark it stale
     (echelon_dispute) so the next recall doesn't repeat your mistake. fire_lower is the only downward path.
  ⑤ CHECK integrity. Before wrapping a session: scan for broken edges. Leave the bank cleaner.

A topic you expect to exist but don't see surfaced is DORMANT, not gone — re-query with a sharper intent.\
"""

_INSTRUCTIONS = _build_instructions()

# The .env template seeded into a fresh work folder. Keys the substrate's optional
# subsystems read; the user fills what they use. Bank dbs themselves need NO key.
_ENV_TEMPLATE = """\
# ECHELON work-folder config. The bank dbs (core.db, echelon.db) live beside this
# file and are created + migrated automatically — they need no key. Fill only the
# subsystems you use; blank = that subsystem stays off.

# --- LLM providers (only needed for council / dispatch / classify verbs) ---
DEEPSEEK_API_KEY=
GEMINI_API_KEY=
GROK_API_KEY=
# local floor (LM Studio / llama.cpp OpenAI-compatible endpoint)
LM_ENDPOINT=http://127.0.0.1:1234/v1

# --- optional web/graph viz ---
ECHELON_WEB_PORT=8800

# --- MCP HTTP transport (echelon-mcp --http) ---
# Port the always-on HTTP server binds (tunnel this to your domain for Agent Builder).
ECHELON_MCP_PORT=8888
# Bearer token REQUIRED when bound to a non-loopback host (internet-facing). Clients
# send `Authorization: Bearer <token>`. Leave blank ONLY for loopback-only dev.
ECHELON_MCP_TOKEN=
# Bind host: 127.0.0.1 (default, loopback) or 0.0.0.0 (behind the tunnel + token).
ECHELON_MCP_HOST=127.0.0.1

# --- defaults ---
ECHELON_SCOPE=echelon
"""

# Provider key env vars — the MULTI-TENANT CREDENTIAL BOUNDARY (owner 2026-08-15).
# These names are never allowed to cross a tenant edge through process-global state:
#   - _request_env() strips them from a MEMBER's subprocess (no owner key to spend)
#   - _ensure_home() refuses to hoist them out of a member's .env into os.environ
# Keys are resolved PER CALL from the caller's own home (echelon_sdk.keys._home_env).
_PROVIDER_KEY_VARS: tuple = (
    "DEEPSEEK_API_KEY", "GEMINI_API_VERTEX", "GEMINI_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "XAI_API_KEY", "GROK_API_KEY", "LM_API_TOKEN", "OPENAI_API_KEY",
)


def _ensure_home(home: Path) -> dict:
    """Make the work folder a valid substrate home: create the dir, have the engine
    create + migrate the dbs into it, and seed a .env if absent. Returns a small
    status dict for the `config` tool / startup log. Idempotent."""
    home.mkdir(parents=True, exist_ok=True)
    os.environ["ECHELON_HOME"] = str(home)
    created = {"home": str(home), "dbs": [], "env_seeded": False}
    # Open the stores ONCE so their __init__ creates + migrates the schema into the
    # fresh folder (CardStore/UAME mkdir + executescript the schema on connect).
    # IMPORTANT: resolve the db paths from THIS `home` explicitly — the module-global
    # DEFAULT_DB/DEFAULT_V2_DB are bound at import time, so on a long-lived server
    # (single-port multi-user) they point at the FIRST home, not this user's. Passing
    # the paths explicitly is what makes per-user create/seed land in the right bank
    # (owner 2026-07-02, fixing the cross-tenant seed leak).
    try:
        from echelon_engine.atoms.cards import CardStore
        from echelon_engine.atoms.uame import UAME
        v2_db = str(home / "echelon.db")
        core_db = str(home / "core.db")
        CardStore(v2_db)          # echelon.db (v2 primary) — spine/body/sidecar
        UAME(core_db)             # core.db (v1 soul/index)
        created["dbs"] = [Path(core_db).name, Path(v2_db).name]
        # SELF-SEED a fresh bank so it's born knowing itself — the first
        # echelon_recall('what is echelon') is WARM, not cold-empty (owner
        # 2026-06-22). No-ops on an existing bank (scope-not-empty check).
        from echelon_engine.atoms.self_seed import seed_if_empty
        seed = seed_if_empty(os.environ.get("ECHELON_SCOPE", "echelon"), home=str(home))
        created["self_seed"] = seed
        # CLONE the earned general-skill cartridges (ux/swarm/yagni/dev/scribe/
        # brainstorm/act-ready/partner) from the SOURCE bank into this home, so an
        # equipped model can recall real skills, not just the self-primer (owner
        # 2026-06-22). Only on a fresh seed, only when a distinct source bank
        # exists (default ~/.echelon, or $ECHELON_SOURCE_HOME). Content-addressed.
        if seed.get("seeded"):
            try:
                from echelon_engine.atoms.echelon_home import echelon_home
                from echelon_engine.atoms import clone_cartridges as cc
                source = os.environ.get("ECHELON_SOURCE_HOME") or str(Path.home() / ".echelon")
                if Path(source).expanduser().resolve() != echelon_home().resolve() \
                        and (Path(source).expanduser() / "echelon.db").exists():
                    # ECHELON_TEAMMATE_SAFE=1 (set in a shared/teammate gateway's env)
                    # clones ONLY general skills — drops the owner's private-estate
                    # cartridges + echelon:*/mol:* war-stories (owner 2026-07-02).
                    ts = os.environ.get("ECHELON_TEAMMATE_SAFE", "").strip().lower() \
                        in ("1", "true", "yes")
                    created["cartridges"] = cc.clone(source_home=source,
                                                     target_home=str(home),
                                                     teammate_safe=ts, verbose=False)
            except Exception as e:  # never block startup on the clone
                created["clone_error"] = repr(e)
    except Exception as e:  # never block startup on a migrate hiccup; report it
        created["db_error"] = repr(e)
    env_path = home / ".env"
    if not env_path.exists():
        env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")
        created["env_seeded"] = True
    # Load the home's .env into the environment (simple KEY=VALUE; comments/blank
    # ignored) — BUT NEVER PROVIDER KEYS on a multi-user server.
    #
    # os.environ is PROCESS-GLOBAL. Seeding a member's home here used to publish that
    # member's API keys into the server env via setdefault, where they outlived the
    # request (the caller's restore only rewinds ECHELON_HOME/SCOPE/TEAMMATE_SAFE) and
    # became the de-facto key for every later tenant — a cross-tenant credential leak,
    # and the mirror image of the owner-key leak fixed in echelon_sdk/keys.py.
    # Keys must be resolved PER CALL from the caller's own home (keys.py::_home_env),
    # never hoisted into shared process state.
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k.upper() in _PROVIDER_KEY_VARS:
                    continue      # per-call resolution only; never process-global
                os.environ.setdefault(k, v.strip())
    # ensure a writable sessions/ dir the model can author wrap specs into.
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    created["sessions_dir"] = str((home / "sessions").resolve())
    return created


def _sync_identity(headers, path: str = "") -> dict | None:
    """Who is syncing, and against WHICH bank. Returns {home, scope, peer, who}.

    CROSS-TENANT DISCIPLINE (cross-tenant-leaks-hide-in-in-process-writes): the sync routes
    write to the bank IN PROCESS, which is the shape that leaked into the owner bank in the
    2026-07-02 multi-user build. The home therefore comes from the CALLER'S TOKEN — never
    from the module-global default — and the caller's scope bounds what they may exchange.

    Resolves from the PRESENTED TOKEN, not from _REQ: the thread-local is pinned in do_POST
    only, so a do_GET route (/sync/hello, /sync/pull) would otherwise see either nothing or —
    worse, since worker threads are reused — a PREVIOUS request's identity. Deriving identity
    from the token on every call is the only reading that is correct for both verbs.
    (Caught 2026-08-02 by a live 403 on the box: GETs never resolved a member.)"""
    from echelon_engine.atoms import mcp_users as _mu
    presented = (_mu.bearer_from_headers(headers, path) or "").strip()
    owner_tok = os.environ.get("ECHELON_MCP_TOKEN", "").strip()

    # OWNER first — a registry row must never shadow the configured owner token. When no
    # owner token is configured the server is local-trust (loopback dev) and the caller is
    # the owner, matching authorized()'s own rule.
    if (not owner_tok) or presented == owner_tok:
        return {"home": _current_home(), "scope": os.environ.get("ECHELON_SCOPE", "echelon"),
                "peer": "peer:owner", "who": "owner"}
    rec = _mu.resolve(presented)
    if rec:
        return {"home": Path(rec["home"]), "scope": rec["scope"],
                "peer": f"peer:{rec['db']}", "who": rec["db"]}
    return None


def _sync_open(ident: dict):
    """Open the identity's OWN bank for a sync exchange. Never DEFAULT_V2_DB."""
    import sqlite3 as _sq
    db = Path(ident["home"]) / "echelon.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _sq.connect(str(db), timeout=30)
    conn.row_factory = _sq.Row
    return conn


def _current_home() -> Path:
    """The bank home for the CURRENT request: the per-request user home if one is
    pinned to this thread (single-port multi-user), else the resolved default. So
    the environment/writable paths shown to a sub-user match THEIR bank, not the
    server's default (owner 2026-07-02)."""
    home = getattr(_REQ, "home", None)
    if home:
        return Path(home)
    from echelon_engine.atoms.echelon_home import echelon_home
    return echelon_home()


def _writable_dir() -> Path:
    """The dir the equipped model may WRITE files into (wrap specs, scratch). Under
    the resolved home so it's always inside the bank's work folder."""
    d = _current_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _environment_block() -> str:
    """A runtime ENVIRONMENT section appended to the instructions on initialize so
    the equipped model KNOWS its host OS + where it may write (the transcript bug:
    the model invented 'session/...' for a wrap spec because it had no idea what
    path was writable). Computed live — OS + the resolved home/writable dir."""
    import platform
    home = _current_home()
    sep = "\\" if os.name == "nt" else "/"
    example = str((home / "sessions" / "my-session-wrap.json"))
    return (
        "\n\nYOUR ENVIRONMENT (live — use these real paths, never invent one):\n"
        f"- OS: {platform.system()} ({os.name}); path separator '{sep}'.\n"
        f"- Bank home (read by the tools): {home}\n"
        f"- WRITABLE dir (you MAY create/write files here — wrap specs, scratch): "
        f"{_writable_dir()}\n"
        "- To wrap a session: write your wrap-spec JSON into the WRITABLE dir above "
        f"(e.g. {example}), then call echelon_wrap with that absolute spec_path. Do "
        "NOT pass a relative/invented path — echelon_wrap reads a real file and will "
        "error on a path that doesn't exist. echelon_config reports these paths too."
    )


# ── per-request user bank (single-port multi-user, owner 2026-07-02) ──────────
# The gateway serves many users on ONE port; each request runs the CLI against the
# CALLER'S bank. do_POST sets this thread-local after auth (from the token→user
# registry); _run_echelon/_stream_echelon fold it into the SUBPROCESS env only —
# never mutating the shared os.environ (ThreadingHTTPServer runs requests
# concurrently). Unset → the default home (back-compat for the owner/loopback).
import threading as _threading
_REQ = _threading.local()
# serializes the rare first-contact bank creation (it mutates shared os.environ via
# _ensure_home); steady-state requests never take it.
_SEED_LOCK = _threading.Lock()


def _request_env() -> dict:
    """A subprocess env for THIS request: os.environ + the caller's ECHELON_HOME/
    ECHELON_SCOPE if a user bank was resolved for this thread.

    BILLING BOUNDARY (owner 2026-08-15): for a non-owner request the box's provider
    keys are REMOVED. Members hold the full tool surface — isolation is the enabler —
    but a paid call must be funded by the caller's own key, resolved from their own
    ECHELON_HOME/.env. This is defence in depth behind _capability_guard: even if a
    verb slips past the guard, there is no owner key in its environment to spend.
    """
    env = os.environ.copy()
    home = getattr(_REQ, "home", None)
    scope = getattr(_REQ, "scope", None)
    if home:
        env["ECHELON_HOME"] = home
    if scope:
        env["ECHELON_SCOPE"] = scope
    if home and not _is_owner():
        for var in _PROVIDER_KEY_VARS:
            env.pop(var, None)
    return env


def _run_echelon(args: list[str], timeout: int = 180) -> str:
    """Shell out to the echelon CLI with the current env (ECHELON_HOME set). Uses
    the SAME interpreter running this server (-m echelon_engine), so no PATH/branch
    surprises. -X utf8 avoids the cp1252 arrow crash. Returns combined stdout+stderr
    text (the CLI's human output IS the tool result). Runs against the per-request
    user bank when one is set (single-port multi-user)."""
    cmd = [sys.executable, "-X", "utf8", "-m", "echelon_engine", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout, env=_request_env())
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: `echelon {' '.join(args)}` timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return f"ERROR running echelon {args!r}: {e!r}"


def _stream_echelon(args: list[str]):
    """Generator: run the echelon CLI and YIELD stdout line-by-line as it prints
    (for the /tail SSE endpoint — the slow verbs dream/sleep/wrap where you want to
    WATCH it run, not block on one buffered response). Same interpreter + -X utf8 as
    _run_echelon; stderr folded into the stream so errors are visible. Yields the
    process exit line last."""
    cmd = [sys.executable, "-X", "utf8", "-u", "-m", "echelon_engine", *args]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", bufsize=1, env=_request_env())
    except Exception as e:  # noqa: BLE001
        yield f"ERROR launching echelon {args!r}: {e!r}"
        return
    try:
        for line in iter(p.stdout.readline, ""):
            yield line.rstrip("\n")
    finally:
        p.stdout.close()
        rc = p.wait()
        yield f"[exit {rc}]"


def _t_cli(a: dict) -> str:
    """Raw argv passthrough to the echelon CLI — the MCP twin of the web Raw CLI
    door (POST /api/cli, apps/web/api.py). SAME surface, same isolation
    (_request_env binds the caller's own home/scope). Gated to owner/admin/
    owner@example.com via _tool_allowed. LOG every call — raw argv is the widest
    door the substrate has."""
    args = a.get("args")
    if not args or not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        return "ERROR: 'args' must be a list of CLI argument strings"
    logging.getLogger("echelon.mcp.cli").info("echelon_cli call: %s", " ".join(args))
    return _run_echelon(args, timeout=60)


# ── tool registry: name → (json-schema, handler) ────────────────────────────────
# Handlers receive the parsed `arguments` dict and return a text string. They build
# the echelon CLI argv; the server frames it into MCP content.
def _DEFAULT_SCOPE() -> str:
    """The scope a verb uses when the caller names none.

    MUST prefer the REQUEST's scope over the process env: os.environ["ECHELON_SCOPE"]
    is the SERVER's scope (the owner's "echelon" on a shared box), so defaulting to it
    made every member verb silently target the owner's scope name. The per-request
    value is set by do_POST from the token's registry record.
    """
    return (getattr(_REQ, "scope", None)
            or os.environ.get("ECHELON_SCOPE", "echelon"))


def _t_recall(a: dict) -> str:
    scope = a.get("scope") or _DEFAULT_SCOPE()
    intent = a["intent"]
    args = ["recall", "--scope", scope, "--warm", intent]
    # kindle law inverted 2026-07-06: plain recall is FREE; kindle=True is the
    # deliberate warm-up door (trace-credit to the surfaced atoms).
    if a.get("kindle"):
        args.append("--kindle")
    return _run_echelon(args)


def _t_remember(a: dict) -> str:
    return _run_echelon(["remember", a["slug"]])


def _t_inspect(a: dict) -> str:
    return _run_echelon(["inspect", a["slug"]])


def _t_relive(a: dict) -> str:
    args = ["relive"]
    if a.get("card_id"):
        args.append(a["card_id"])
    else:
        args.append("--list")
    return _run_echelon(args)


def _t_config(a: dict) -> str:
    # config + the live environment block, so a model asking "where can I write /
    # what's my OS" gets the writable dir + host platform, not just the bank paths.
    return _run_echelon(["config"]) + _environment_block()


def _contain_root(root: str) -> str:
    """Confine a caller-supplied filesystem path to the caller's OWN home.

    THE HOLE THIS CLOSES (skeptic gate, 2026-08-15): `--root` is a raw caller string.
    ECHELON_HOME decides where the bank is WRITTEN, not what the filesystem read can
    REACH — so `ingest --root <owner estate>/memory` planted the OWNER's private atoms
    into a member's own bank, which they could then recall and remember. The
    "isolation already bounds every verb" argument conflated destination with source.

    Owner is unconstrained (they own the box). For a member the path is resolved and
    must live under their home; anything else raises, with the reason stated.
    """
    if _is_owner():
        return root
    home = getattr(_REQ, "home", None)
    if not home:
        raise ValueError("no bank home is bound to this request")
    try:
        base = Path(home).expanduser().resolve()
        target = Path(root).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"could not resolve path: {e}") from e
    if target != base and base not in target.parents:
        raise ValueError(
            f"path '{root}' is outside your bank. Members may only read paths under "
            f"their own home — this is what keeps one tenant's files out of another's "
            f"bank. Use a path inside your own memory folder.")
    return str(target)


def _contain_write_path(out: str) -> str:
    """Contain a caller-supplied OUTPUT path. The file itself need not exist yet, so
    the check runs against its parent directory."""
    if _is_owner():
        return out
    p = Path(out).expanduser()
    _contain_root(str(p.parent if p.parent != Path("") else Path(".")))
    return str(p)


def _t_check(a: dict) -> str:
    return _run_echelon(["check", "--root", _contain_root(a["root"])])


def _t_ingest(a: dict) -> str:
    # A member may only plant into their OWN scope: a caller-supplied scope would let
    # them label content as another estate's (the ingest canonicalization gate is
    # satisfiable by naming a directory, so it is not a boundary on its own).
    scope = (a.get("scope") or _DEFAULT_SCOPE()) if _is_owner() else \
        (getattr(_REQ, "scope", None) or _DEFAULT_SCOPE())
    return _run_echelon(["ingest", "--scope", scope, "--root", _contain_root(a["root"])])


# ── the REST of the access surface (owner 2026-06-22) ────────────────────────────
# Same robust shell-out-to-CLI pattern. Grouped: capability (cartridge/gate/group-
# scope), curation (dispute/disclaim/redeem/scan/heal), maintenance (dream/sleep/
# warmth/graph). Mutating verbs default to the SAFE side (dry-run / require explicit
# args) because this server is meant to face the internet via the 8888 tunnel.

def _t_cartridge(a: dict) -> str:
    """Equip an earned capability. action: list | registry | equip | compose.
    equip needs `name` + `goal`; compose needs `name`. Default = list."""
    action = (a.get("action") or "list").strip()
    if action == "equip":
        if not a.get("name") or not a.get("goal"):
            return "ERROR: cartridge equip needs both `name` and `goal`."
        return _run_echelon(["cartridge", "equip", a["name"], a["goal"]])
    if action == "compose":
        if not a.get("name"):
            return "ERROR: cartridge compose needs `name`."
        return _run_echelon(["cartridge", "compose", a["name"]])
    if action == "registry":
        return _run_echelon(["cartridge", "registry"])
    return _run_echelon(["cartridge", "list"])


def _t_gate(a: dict) -> str:
    scope = a.get("scope") or _DEFAULT_SCOPE()
    return _run_echelon(["gate", "--scope", scope])


def _client_hook_snippet(scope: str) -> str:
    """The Claude Code settings.json SessionStart hook a NEW USER pastes into his
    own ~/.claude/settings.json so the REFLEX banner refreshes each session. The
    bank is cloud-hosted (this server), but the *banner* must fire in HIS harness —
    a hook is the only thing that runs on his SessionStart. It just curls the
    gateway's /cli gate verb (no local engine needed)."""
    return (
        '{\n'
        '  "hooks": {\n'
        '    "SessionStart": [\n'
        '      {\n'
        '        "matcher": "startup",\n'
        '        "hooks": [\n'
        '          {\n'
        '            "type": "command",\n'
        '            "command": "curl -s -X POST '
        '\\"$ECHELON_GATEWAY/cli\\" -H \\"Authorization: Bearer $ECHELON_TOKEN\\" '
        f'-H \\"Content-Type: application/json\\" -d \\"{{\\\\\\"args\\\\\\":[\\\\\\"gate\\\\\\",\\\\\\"--scope\\\\\\",\\\\\\"{scope}\\\\\\"]}}\\""\n'
        '          }\n'
        '        ]\n'
        '      }\n'
        '    ]\n'
        '  }\n'
        '}'
    )


def _t_install(a: dict) -> str:
    """ONE-CALL ONBOARDING for a new ECHELON user (owner 2026-07-02). Everything is
    cloud-hosted: the user's bank/scope lives on THIS server (isolated by --home).
    This tool composes the whole install and returns it as a single readable result:
      ① BANNER + REFLEX GATE  — the gate banner for the user's scope (recall-first law).
      ② MEMORY INDEX          — if `root` given, check+ingest the user's memory/*.md
                                into his scope; else scaffold guidance.
      ③ HOOKS                 — the Claude Code settings.json SessionStart hook to
                                paste (keeps the banner warm each session, curl-only).
      ④ CONFIG                — resolved cloud paths + the .mcp.json reminder.
    Idempotent: re-running only re-plants new atoms and re-prints the setup."""
    scope = a.get("scope") or _DEFAULT_SCOPE()
    out: list[str] = []
    out.append("═══════════════════════════════════════════════════════════════")
    out.append(f"  ECHELON INSTALL — you are now onboarding into scope '{scope}'")
    out.append("  (cloud-hosted bank; your memory lives on this server, isolated)")
    out.append("═══════════════════════════════════════════════════════════════")

    # ① banner + reflex gate
    out.append("\n── ① THE GATE + REFLEX LAW ──────────────────────────────────")
    out.append(_run_echelon(["gate", "--scope", scope]))
    out.append("\nREFLEX (your operating doctrine):\n" + _INSTRUCTIONS)

    # ② memory index (optional)
    out.append("\n── ② MEMORY INDEXING ────────────────────────────────────────")
    root = a.get("root")
    if root:
        # same containment as _t_check/_t_ingest — this verb reaches the same CLI
        root = _contain_root(str(root))
        out.append(_run_echelon(["check", "--root", root]))
        out.append(_run_echelon(["ingest", "--scope", scope, "--root", root]))
    else:
        out.append(
            "No `root` given — nothing planted yet. To index your own memory:\n"
            "  1. Make a folder with one lesson per `.md` (frontmatter name/description/\n"
            "     metadata.type + a one-lesson body).\n"
            "  2. Call echelon_install again with root=<that folder>, OR use\n"
            "     echelon_check(root) then echelon_ingest(root) directly.\n"
            "Your scope already holds the self-primer + general-skill cartridges "
            "(see echelon_recall('what is echelon') — it should be WARM).")

    # ③ client-side hook (banner stays warm each session)
    out.append("\n── ③ CLAUDE CODE HOOK (paste into ~/.claude/settings.json) ───")
    out.append(
        "Set two env vars in your shell first:\n"
        "  ECHELON_GATEWAY=<the https URL you connect this MCP to>\n"
        "  ECHELON_TOKEN=<your bearer token>\n"
        "Then merge this into ~/.claude/settings.json (refreshes the banner each session):")
    out.append(_client_hook_snippet(scope))

    # ④ config + mcp.json reminder
    out.append("\n── ④ CONFIG (your live cloud bank) ──────────────────────────")
    out.append(_run_echelon(["config"]))
    out.append(
        "\nYour Claude Code .mcp.json entry (HTTP/SSE transport):\n"
        '  {\n'
        '    "mcpServers": {\n'
        '      "echelon": {\n'
        '        "type": "http",\n'
        '        "url": "<your gateway URL>",\n'
        '        "headers": { "Authorization": "Bearer <your token>" }\n'
        '      }\n'
        '    }\n'
        '  }')
    out.append("\n✅ Install composed. Call echelon_recall FIRST on any real task — "
               "WARM atoms beat your training; COLD = new ground, offer to remember.")
    return "\n".join(out)


def _t_vision(a: dict) -> str:
    # `url` drives a real browser. A file:// (or bare path) URL turns a screenshot
    # verb into a local-file reader, so members get http(s) only; the owner is free.
    url = str(a["url"])
    if not _is_owner():
        low = url.strip().lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return ("ERROR: vision accepts http:// or https:// URLs only. A file:// or "
                    "local path would let a screenshot read this server's disk.")
    args = ["vision", "--url", url]
    if a.get("width"):
        args.extend(["--width", str(a["width"])])
    if a.get("height"):
        args.extend(["--height", str(a["height"])])
    return _run_echelon(args)


def _t_brainstorm(a: dict) -> str:
    args = ["brainstorm", a["question"]]
    if a.get("context"):
        args.extend(["--context", a["context"]])
    return _run_echelon(args, timeout=300)


def _t_scribe(a: dict) -> str:
    """Dispatch a model to read a folder and write docs into it.

    CONTAINMENT (skeptic re-gate, 2026-08-15): `target` becomes the agent's SANDBOX
    FOLDER, and that sandbox carries read/write/edit/bash. Uncontained, a member with
    their own provider key (the sanctioned path — so _capability_guard waves them
    through) could aim a writing agent at another tenant's estate. This is the same
    destination-vs-source confusion as the ingest hole, but WORSE, because the agent's
    hands can write there. Bound it to the caller's own home like every other path.
    """
    args = ["scribe", _contain_root(a["target"])]
    if a.get("goal"):
        args.append(a["goal"])
    return _run_echelon(args, timeout=300)


def _t_clone_cartridges(a: dict) -> str:
    """Clone earned cartridges into a bank.

    CONTAINMENT: `from_bank`/`to_bank` are BANK PATHS. Uncontained, a member could
    name the owner's bank as the source and clone its cartridges into their own —
    a cross-tenant read wearing a provisioning verb's clothes. For a member the
    DESTINATION is forced to their own home; the SOURCE may only be their own home
    (the owner's shared seed already reached them at provisioning time, filtered by
    ECHELON_TEAMMATE_SAFE, which is the sanctioned path for owner content).
    """
    args = ["clone-cartridges"]
    if _is_owner():
        if a.get("from_bank"):
            args.extend(["--from", a["from_bank"]])
        if a.get("to_bank"):
            args.extend(["--to", a["to_bank"]])
        scope = a.get("scope") or _DEFAULT_SCOPE()
    else:
        home = getattr(_REQ, "home", None)
        if not home:
            return "ERROR: no bank home is bound to this request"
        if a.get("from_bank"):
            args.extend(["--from", _contain_root(a["from_bank"])])
        args.extend(["--to", str(Path(home).expanduser().resolve())])
        scope = getattr(_REQ, "scope", None) or _DEFAULT_SCOPE()
    args.extend(["--scope", scope])
    return _run_echelon(args)


def _t_swarm_subject(a: dict) -> str:
    """Fan a swarm over a subject, grounded on `files`.

    CONTAINMENT: every entry of `files` is a caller-supplied path handed to dispatched
    workers — same class as _t_scribe's target. Each one is bounded to the caller's own
    home for a member; the scope is likewise forced to theirs.
    """
    args = ["swarm-subject", "--subject", a["subject"]]
    if a.get("files"):
        files = a["files"]
        if not isinstance(files, list):
            files = [files]
        args.extend(["--files"] + [_contain_root(str(f)) for f in files])
    scope = (a.get("scope") or _DEFAULT_SCOPE()) if _is_owner() else \
        (getattr(_REQ, "scope", None) or _DEFAULT_SCOPE())
    args.extend(["--scope", scope])
    return _run_echelon(args, timeout=600)


def _t_resolve_scope(a: dict) -> str:
    """Resolve a directory to its bank scope.

    LEAK FIX (owner 2026-08-15): this verb used to be owner-only because, with no
    --cwd, it resolves the SERVER's working directory and would report an owner
    estate's scope to a member. The right fix is in the verb, not a capability gate:
    for a non-owner we never fall back to the server cwd — an omitted --cwd resolves
    the CALLER'S OWN home. A member can still resolve a path they name, which only
    tells them how the resolver treats a string they already had.
    """
    args = ["resolve-scope", "--json"]
    cwd = a.get("cwd")
    if not _is_owner():
        # An explicit cwd is a directory-existence + scope-name ORACLE: the resolver
        # probes for .git/CLAUDE.md and walks ancestors against the SERVER's live
        # scopes, so a member could map the owner's estate by guessing paths. A member
        # always resolves their own home; the verb stays available, the probe does not.
        cwd = getattr(_REQ, "home", None)
    if cwd:
        args.extend(["--cwd", str(cwd)])
    return _run_echelon(args)


def _t_group_scope(a: dict) -> str:
    """Make a parent scope reach members via atlas edges. Dry-run unless apply=true
    (it mutates the atlas; default safe)."""
    members = a.get("members") or []
    if isinstance(members, str):
        members = [members]
    members = [str(m).strip() for m in members if str(m).strip()]
    if not a.get("parent") or not members:
        return "ERROR: group-scope needs `parent` and at least one `members` entry."
    # Atlas edges are the CROSS-SCOPE RECALL mechanism, so an unconstrained parent
    # would let a member wire their own scope to reach one they do not own. The bank
    # is already theirs, but the edge names are not validated by the CLI — so bound
    # the parent to the caller's own scope for a member (they may still group scopes
    # that live INSIDE their own bank, which is the legitimate use).
    if not _is_owner():
        own = (getattr(_REQ, "scope", None) or "").strip()
        parent = str(a["parent"]).strip()
        if own and parent != own and not parent.startswith(f"{own}-"):
            return (f"ERROR: parent scope '{parent}' is not yours. Members may group "
                    f"scopes under their own scope ('{own}' or '{own}-*') — atlas edges "
                    f"are how recall reaches across scopes, so they stay inside your bank.")
    args = ["group-scope", a["parent"], *members]
    if a.get("rel"):
        args += ["--rel", a["rel"]]
    if a.get("apply"):
        args.append("--apply")
    return _run_echelon(args)


def _t_dispute(a: dict) -> str:
    """Mark an atom/card wrong or stale. action: dispute | disclaim | redeem.
    dispute/disclaim want a `reason`; redeem clears it."""
    action = (a.get("action") or "").strip()
    if action not in ("dispute", "disclaim", "redeem"):
        return "ERROR: action must be one of dispute | disclaim | redeem."
    if not a.get("ref"):
        return "ERROR: pass `ref` — the atom slug/coordinate or row id."
    args = [action, a["ref"]]
    if a.get("reason"):
        args += ["--reason", a["reason"]]
    if a.get("superseded_by"):
        args += ["--superseded-by", a["superseded_by"]]
    if a.get("witness"):
        args += ["--witness", a["witness"]]
    if a.get("table"):
        args += ["--table", a["table"]]
    return _run_echelon(args)


def _t_scan(a: dict) -> str:
    """Immune system. action: scan (read-only report, default) | heal (tombstone
    broken edges). heal stays a dry-run unless apply=true."""
    action = (a.get("action") or "scan").strip()
    scope = a.get("scope") or _DEFAULT_SCOPE()
    if action == "heal":
        args = ["heal", "--scope", scope]
        if not a.get("apply"):
            args.append("--dry-run")   # safe default for an internet-facing call
        return _run_echelon(args)
    return _run_echelon(["scan", "--scope", scope])


def _t_dream(a: dict) -> str:
    args = ["dream", "--scope", a.get("scope") or _DEFAULT_SCOPE()]
    if a.get("forks"):
        args += ["--forks", str(a["forks"])]
    return _run_echelon(args, timeout=600)


def _t_sleep(a: dict) -> str:
    args = ["sleep", "--scope", a.get("scope") or _DEFAULT_SCOPE()]
    if a.get("depth"):
        args += ["--depth", a["depth"]]
    return _run_echelon(args, timeout=900)


def _t_warmth(a: dict) -> str:
    args = ["warmth", "--scope", a.get("scope") or _DEFAULT_SCOPE()]
    if a.get("iters"):
        args += ["--iters", str(a["iters"])]
    if a.get("dry_run"):
        args.append("--dry-run")
    return _run_echelon(args, timeout=900)


def _t_graph(a: dict) -> str:
    args = ["graph", "--scope", a.get("scope") or _DEFAULT_SCOPE()]
    if a.get("limit"):
        args += ["--limit", str(a["limit"])]
    if a.get("out"):
        # `out` is a WRITE path (the rendered atlas HTML) — contain it like every
        # other caller-supplied path, or a member could drop a file anywhere the
        # service user can write. _contain_root allows a not-yet-existing file by
        # checking its parent directory.
        args += ["--out", _contain_write_path(a["out"])]
    scope_arg = a.get("scope") or _DEFAULT_SCOPE()
    if not _is_owner():
        args[2] = getattr(_REQ, "scope", None) or scope_arg
    return _run_echelon(args, timeout=300)


def _t_wrap(a: dict) -> str:
    # The agent does NOT author a spec file (owner 2026-06-22: a weak model can't
    # produce the episodes/card/evidence JSON, so it gets stuck demanding a
    # spec_path). Instead it passes PLAIN content it CAN produce — a label + a list
    # of lesson sentences — and the SERVER does the heavy lifting: plant each lesson
    # as an atom, assemble a valid wrap spec, write it to the sessions dir, run it.
    # (spec_path still honored for a power user who already has a spec file.)
    if a.get("spec_path"):
        # CONTAINMENT (skeptic gate, 2026-08-15): `spec_path` is read off disk by
        # wrap.py (`Path(spec).read_text()` + json.loads), so uncontained it is an
        # arbitrary-file read. It is also UNDOCUMENTED — absent from this tool's
        # published schema — and _dispatch hands handlers the raw arguments dict with
        # no schema filtering, so a member can pass it anyway. An audit that reads
        # schemas, or greps for path-shaped parameter NAMES, cannot see this one:
        # the handler body is the only source of truth for what a tool accepts.
        args = ["wrap", _contain_root(a["spec_path"])]
        scope = (a.get("scope") if _is_owner() else getattr(_REQ, "scope", None)) \
            or _DEFAULT_SCOPE()
        if scope:
            args += ["--scope", scope]
        return _run_echelon(args)

    import json as _json, re, time
    label = (a.get("label") or "session").strip()
    lessons = a.get("lessons") or []
    if isinstance(lessons, str):
        lessons = [lessons]
    lessons = [str(x).strip() for x in lessons if str(x).strip()]
    if not lessons:
        return ("ERROR: nothing to wrap. Pass `lessons` — a list of short lesson "
                "sentences from the session (what was learned/decided). The server "
                "turns them into atoms + a session card for you; you do NOT write a spec file.")
    # Route through _DEFAULT_SCOPE (which prefers the REQUEST's scope) instead of
    # reading os.environ directly — that direct read was the SERVER's scope, so a
    # member's wrap silently labelled atoms under the owner's scope name inside their
    # own bank. Members are pinned to their own scope, as _t_ingest already is.
    scope = (a.get("scope") if _is_owner() else getattr(_REQ, "scope", None)) \
        or _DEFAULT_SCOPE()
    slug_base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "session"
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # 1) plant each lesson as an atom (so the card has real refs to compose).
    #    Open the stores against THIS REQUEST'S home (single-port multi-user) — the
    #    module-global default db is bound at import time, so an in-process write here
    #    would otherwise land in the SERVER's bank, not the caller's (owner 2026-07-02,
    #    fixing the cross-tenant wrap-write leak the sandbox test caught).
    try:
        from echelon_engine.atoms.store import SeedStore
        from echelon_engine.atoms.cards import CardStore
        _home = _current_home()
        _core_db = str(_home / "core.db")
        _v2_db = str(_home / "echelon.db")
        store = SeedStore(_core_db, v2_db=_v2_db)
        slugs, coords = [], []
        for i, lesson in enumerate(lessons):
            slug = f"{slug_base}-{stamp}-{i+1}"
            coord = f"{scope}:{slug}"
            store.remember(scope=scope, content=f"[{slug}] {lesson}", kind="feedback",
                           tier="core", coordinate=coord, valence=0.1, arousal=0.1)
            slugs.append(slug); coords.append(coord)
        # compile spine for the new atoms so the card can resolve + relive them
        cards = CardStore(_v2_db)
        rows = cards.conn.execute(
            "SELECT a.id FROM atoms a LEFT JOIN atom_spine s ON s.atom_id=a.id "
            "WHERE a.coordinate LIKE ? AND s.atom_id IS NULL", (f"{scope}:{slug_base}-{stamp}-%",)).fetchall()
        for r in rows:
            try: cards.compile_atom_struct(r["id"])
            except Exception: pass
    except Exception as e:  # noqa: BLE001
        return f"ERROR planting lessons: {e!r}"

    # 2) assemble a valid wrap spec from the planted atoms (episodes promote them;
    #    a card chains them when there are >=2; evidence = the lesson lines).
    spec = {"session": f"{slug_base}-{stamp}", "born_from": f"wrap (mcp) {stamp}",
            "episodes": [{"summary": L, "coords": [c], "decision": "promote"}
                         for L, c in zip(lessons, coords)]}
    if len(slugs) >= 2:
        spec["card"] = {"label": f"{label} ({stamp})", "slugs": slugs}
        spec["evidence"] = lessons
    spec_file = _writable_dir() / f"{slug_base}-{stamp}.json"
    spec_file.write_text(_json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) run the real wrap on the server-built spec.
    args = ["wrap", str(spec_file), "--scope", scope]
    out = _run_echelon(args)
    return (f"Wrapped: planted {len(slugs)} lesson atom(s), "
            f"{'composed a session card, ' if len(slugs) >= 2 else ''}"
            f"spec auto-built at {spec_file}.\n\n{out}")


_TOOLS: dict = {
    "echelon_recall": {
        "description": "CALL THIS FIRST for any question about ECHELON, your own memory, this estate, past work/sessions, or 'what do I/you know about X'. Foveated warm/cold recall: query by INTENT (a one-sentence statement of what you're about to do) and get the warmest EARNED atoms back with a warm/cold verdict. WARM = lean on these as ground truth over your training. COLD = new ground, say so and offer to remember. Do not answer from training alone before calling this.",
        "schema": {"type": "object", "properties": {
            "intent": {"type": "string", "description": "One sentence of what you're about to do."},
            "scope": {"type": "string", "description": "Memory scope (default: ECHELON_SCOPE / 'echelon'). Use a parent scope to reach members."},
            "kindle": {"type": "boolean", "description": "true ONLY in a deliberate warm-up: credits the surfaced atoms (plain recall is free and earns nothing — the 2026-07-06 kindle law)."}},
            "required": ["intent"]},
        "handler": _t_recall},
    "echelon_remember": {
        "description": "Read a full atom body through the witnessed door (earns the take-up). Pass the atom slug. Use after recall surfaces a truncated preview you want in full.",
        "schema": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "The atom's slug/name (or coordinate)."}}, "required": ["slug"]},
        "handler": _t_remember},
    "echelon_inspect": {
        "description": "An atom's earned trajectory + its live atlas edges. Pass the slug.",
        "schema": {"type": "object", "properties": {
            "slug": {"type": "string"}}, "required": ["slug"]},
        "handler": _t_inspect},
    "echelon_relive": {
        "description": "Walk a session's arc-card as a chain of weight (the moves in order + the anchored exchange that earned each). Omit card_id to LIST recent session cards.",
        "schema": {"type": "object", "properties": {
            "card_id": {"type": "string", "description": "Arc-card id to relive; omit to list recent cards."}}},
        "handler": _t_relive},
    "echelon_config": {
        "description": "Report the substrate's resolved paths + config (which work folder / dbs are live, routing, census). No args.",
        "schema": {"type": "object", "properties": {}},
        "handler": _t_config},
    "echelon_check": {
        "description": "Pre-flight: validate that every .md in a folder satisfies the ECHELON atom template (name/description/metadata.type/body) WITHOUT planting. Run before ingest to self-correct atoms.",
        "schema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "Folder (or project root) of memory .md to validate."}}, "required": ["root"]},
        "handler": _t_check},
    "echelon_ingest": {
        "description": "Plant a folder's memory/*.md atoms into the bank under a scope (idempotent, content-addressed). Echoes the resolved path; skips+reports malformed atoms.",
        "schema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "Folder (or project root) holding the memory .md."},
            "scope": {"type": "string", "description": "Scope to plant under (default: ECHELON_SCOPE)."}}, "required": ["root"]},
        "handler": _t_ingest},
    "echelon_wrap": {
        "description": "Close a session by distilling what was learned. Pass `lessons` — a list of short lesson sentences from the session (what was learned, decided, or corrected). The SERVER turns them into atoms + a session card and runs the wrap for you — you do NOT author or write any spec file. Optional `label` names the session.",
        "schema": {"type": "object", "properties": {
            "lessons": {"type": "array", "items": {"type": "string"},
                        "description": "The session's lessons, one short sentence each (e.g. ['Owner wanted X', 'Y was the fix', 'Z is the trap']). These become atoms; >=2 also compose a relivable session card."},
            "label": {"type": "string", "description": "A short name for the session (default: 'session')."},
            "scope": {"type": "string", "description": "Scope to plant under (default: your own scope)."},
            "spec_path": {"type": "string", "description": "POWER USER: path to a wrap spec JSON you already have. Must be inside your own bank home. Leave unset and pass `lessons` instead — the server composes the spec for you."}},
            "required": ["lessons"]},
        "handler": _t_wrap},

    # ── vision / reasoning verbs ─────────────────────────────────────────────
    "echelon_vision": {
        "description": "Shoot a URL and get a screenshot back via the Flux mirror eye. Trap-free: verify a UI change, see what a page looks like, check a deployed app.",
        "schema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The URL to screenshot."},
            "width": {"type": "integer", "description": "Viewport width (optional)."},
            "height": {"type": "integer", "description": "Viewport height (optional)."}},
            "required": ["url"]},
        "handler": _t_vision},
    "echelon_brainstorm": {
        "description": "Convene a multi-model deliberation council on a question. Each seat runs on its model; the chair synthesizes. For decisions that benefit from multiple perspectives.",
        "schema": {"type": "object", "properties": {
            "question": {"type": "string", "description": "The question to brainstorm."},
            "context": {"type": "string", "description": "Optional grounding context (code, constraints, the fork)."}},
            "required": ["question"]},
        "handler": _t_brainstorm},
    "echelon_scribe": {
        "description": "Dispatch a precise model to read code and write accurate docs. Pass the file/module to document + an optional goal. Quotes real def lines verbatim, omits what wasn't opened, verifies files on disk.",
        "schema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "File path, module name, or folder to document."},
            "goal": {"type": "string", "description": "What kind of docs to produce (optional)."}},
            "required": ["target"]},
        "handler": _t_scribe},
    "echelon_clone_cartridges": {
        "description": "Copy earned cartridges from one bank into another — the local seed of the cartridge marketplace.",
        "schema": {"type": "object", "properties": {
            "from_bank": {"type": "string", "description": "Source bank path."},
            "to_bank": {"type": "string", "description": "Destination bank path."},
            "scope": {"type": "string", "description": "Scope to clone (default: ECHELON_SCOPE)."}}},
        "handler": _t_clone_cartridges},
    "echelon_swarm_subject": {
        "description": "Fan-out a subject swarm over files: parallel subagent processing partitioned by file. Subject types: ui, code, scribe, qc. Long-running.",
        "schema": {"type": "object", "properties": {
            "subject": {"type": "string", "description": "Subject type (ui, code, scribe, qc)."},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files or glob patterns to process."},
            "scope": {"type": "string", "description": "Scope (default: ECHELON_SCOPE)."}},
            "required": ["subject"]},
        "handler": _t_swarm_subject},
    "echelon_resolve_scope": {
        "description": "Resolve a working directory to its bank scope. Pass `cwd` (or omit for current dir). Returns the scope name — the ONE source of truth for scope resolution.",
        "schema": {"type": "object", "properties": {
            "cwd": {"type": "string", "description": "Working directory to resolve (default: server cwd)."}}},
        "handler": _t_resolve_scope},

    # ── capability verbs ──────────────────────────────────────────────────────
    "echelon_cartridge": {
        "description": "Equip an EARNED CAPABILITY (atom=parameter, card=transformer, warmth=attention). action='list' surveys cartridges (ux/swarm/yagni/scribe/brainstorm/act-ready/partner); 'equip' (needs name+goal) wakes you holding that cartridge foveated on the goal; 'compose' rebuilds a cartridge card; 'registry' lists every spec. Use BEFORE doing work a known cartridge already holds.",
        "schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "equip", "compose", "registry"], "description": "default 'list'."},
            "name": {"type": "string", "description": "cartridge name (for equip/compose)."},
            "goal": {"type": "string", "description": "what you're about to do (for equip)."}}},
        "handler": _t_cartridge},
    "echelon_gate": {
        "description": "Print the ECHELON gate banner — what the substrate is + the three entry doors (existing user / new project / migrating). Orientation for a fresh client.",
        "schema": {"type": "object", "properties": {
            "scope": {"type": "string", "description": "this project's scope (default: ECHELON_SCOPE)."}}},
        "handler": _t_gate},
    "echelon_install": {
        "description": "ONE-CALL ONBOARDING — call this FIRST when you connect a fresh ECHELON gateway. It installs you into your (cloud-hosted, isolated) scope: prints the GATE banner + REFLEX law, INDEXES your memory/*.md if you pass `root`, hands you the Claude Code SessionStart HOOK to keep the banner warm, and reports your live CONFIG + the .mcp.json entry. Idempotent — safe to re-run.",
        "schema": {"type": "object", "properties": {
            "scope": {"type": "string", "description": "your scope / project short-name (default: ECHELON_SCOPE — the scope this gateway is bound to)."},
            "root": {"type": "string", "description": "optional: a folder of your memory .md to check+ingest into your scope now."}}},
        "handler": _t_install},
    "echelon_group_scope": {
        "description": "Make a PARENT scope reach MEMBER scopes via atlas edges (cross-scope recall), with NO rename and NO weight loss. Dry-run by default; pass apply=true to write the edges.",
        "schema": {"type": "object", "properties": {
            "parent": {"type": "string", "description": "the parent (group) scope."},
            "members": {"type": "array", "items": {"type": "string"}, "description": "member scope(s) the parent should reach."},
            "rel": {"type": "string", "enum": ["subsumes", "depends_on", "evolved_from", "merged_into", "provides_to"], "description": "atlas relation (default subsumes)."},
            "apply": {"type": "boolean", "description": "write the edges (default false = dry-run)."}},
            "required": ["parent", "members"]},
        "handler": _t_group_scope},

    # ── curation verbs (the antibodies — let a client correct the bank) ────────
    "echelon_dispute": {
        "description": "Mark an atom/card as wrong or stale so warmth stops trusting it (nothing is deleted — weight decays). action='dispute' (wrong) | 'disclaim' (stale) | 'redeem' (clear, restore). Pass `ref` (slug/coordinate/id) and a `reason`.",
        "schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["dispute", "disclaim", "redeem"]},
            "ref": {"type": "string", "description": "atom slug/coordinate or row id."},
            "reason": {"type": "string", "description": "why it's wrong/stale (dispute/disclaim)."},
            "superseded_by": {"type": "string", "description": "slug that replaces it (optional)."},
            "witness": {"type": "string", "description": "evidence reference (optional)."},
            "table": {"type": "string", "enum": ["atoms", "cards"], "description": "default atoms."}},
            "required": ["action", "ref"]},
        "handler": _t_dispute},
    "echelon_scan": {
        "description": "The immune system over the memory graph. action='scan' (read-only report of broken edges, default) | 'heal' (tombstone them). heal stays a DRY-RUN unless apply=true.",
        "schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["scan", "heal"], "description": "default 'scan'."},
            "scope": {"type": "string"},
            "apply": {"type": "boolean", "description": "for heal: actually tombstone (default false)."}}},
        "handler": _t_scan},

    # ── maintenance verbs (offline organs; usually scheduled, here on demand) ──
    "echelon_dream": {
        "description": "The consolidation pass: good rises, stale decays, counterfeit out. Run on a scope to let the bank settle after a burst of ingests/wraps. Slow.",
        "schema": {"type": "object", "properties": {
            "scope": {"type": "string"}, "forks": {"type": "integer", "description": "dream fork count (optional)."}}},
        "handler": _t_dream},
    "echelon_sleep": {
        "description": "The off-line maintenance state: consolidate + scan + dedup/prune-propose. depth='auto'|'consolidate'|'full'. The full nightly hygiene pass. Slow.",
        "schema": {"type": "object", "properties": {
            "scope": {"type": "string"},
            "depth": {"type": "string", "enum": ["auto", "consolidate", "full"], "description": "default auto."}}},
        "handler": _t_sleep},
    "echelon_warmth": {
        "description": "Re-score / decay maintenance over a scope (averaged passes to damp noise). Pass dry_run=true to preview without writing. Slow.",
        "schema": {"type": "object", "properties": {
            "scope": {"type": "string"}, "iters": {"type": "integer"},
            "dry_run": {"type": "boolean"}}},
        "handler": _t_warmth},
    "echelon_graph": {
        "description": "Render the bank as a force-directed atlas HTML (returns the output path). limit caps the node count; scope '' = all scopes (dense).",
        "schema": {"type": "object", "properties": {
            "scope": {"type": "string"}, "limit": {"type": "integer"},
            "out": {"type": "string", "description": "output html path (optional)."}}},
        "handler": _t_graph},
    "echelon_cli": {
        "description": "Raw echelon CLI passthrough — the SAME door as the web Raw CLI (POST /api/cli): run any `echelon` verb (recall/remember/wrap/status/...). ADMIN DOOR: allowed for the owner, admin-role accounts, and owner@example.com only. Every other identity does not see this tool in tools/list at all.",
        "schema": {"type": "object", "properties": {
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "CLI argument list, e.g. [\"recall\", \"--warm\", \"deploy the estate\"]."}},
            "required": ["args"]},
        "handler": _t_cli},
}


# ── ROLE-BASED CAPABILITY MODEL (owner 2026-07-02: "1 engine, unlimited users/
# personas") ──────────────────────────────────────────────────────────────────
# One gateway serves many isolated personas. Each token carries a ROLE that bounds
# BOTH what tools it can SEE (tools/list) and what it can CALL (tools/call). The
# EVERY USER GETS THE FULL SURFACE (owner 2026-08-15, correcting the 2026-07-02 design).
#
# The mistake being corrected: members were served 15 of 24 tools, on the reasoning that
# isolation made some verbs unsafe. That inverted the owner's intent —
#
#   "echelon should be his, full capabilities on every user or member; the reason for
#    the isolation IS to ensure full capabilities served."
#
# Isolation is the ENABLER, not the fence. Three mechanisms already bound a member's
# reach, and the capability gate was a fourth doing work the first three already did:
#   1. _REQ.home / _REQ.scope — _request_env() binds EVERY call to the caller's own
#      ECHELON_HOME + ECHELON_SCOPE. A verb physically cannot touch another bank.
#   2. ECHELON_TEAMMATE_SAFE — clone-time content filter; the owner's private-estate
#      cartridges and war-stories never enter a teammate home in the first place.
#   3. Per-home .env — each user home carries its own provider keys.
#
# So ingest/check/group_scope/clone_cartridges all operate INSIDE the caller's home and
# are restored outright. resolve_scope is restored with its leak FIXED (it now answers
# within the caller's home rather than the server cwd) — the fix belongs in the verb,
# not in a capability gate.
#
# The one genuinely real constraint was never capability, it was BILLING: the four
# dispatch verbs shell out through _run_echelon, which inherits the server env — so a
# member's call would have spent the OWNER's keys. That is fixed at the source in
# _request_env(): a member request never inherits owner provider keys. The verbs stay
# available to everyone and are funded by the CALLER'S OWN key (see _capability_guard).
_OWNER_ONLY_TOOLS: frozenset = frozenset()

# echelon_cli (raw argv) is additionally granted to admin-role accounts — owner and
# admin come from _is_owner(). This set is an explicit per-email allowance ON TOP of
# that, and is EMPTY by default: a deployment opts specific accounts in by setting
# ECHELON_CLI_ALLOWED_EMAILS to a comma-separated list. Raw argv is the widest
# possible surface, so it is never granted implicitly.
_CLI_ALLOWED_EMAILS: frozenset = frozenset(
    e.strip().lower()
    for e in os.environ.get("ECHELON_CLI_ALLOWED_EMAILS", "").split(",")
    if e.strip()
)

# Verbs that call a paid LLM. Not restricted — GUARDED: for a member we strip the
# owner's keys from the request env and, when the caller has no key of their own,
# return an honest, capability-accurate explanation instead of silently billing the
# owner or emitting a confusing provider stack trace.
# Each entry: tool -> (what it needs, why this provider).
_BUDGET_TOOLS: frozenset = frozenset({
    "echelon_brainstorm", "echelon_scribe", "echelon_vision", "echelon_swarm_subject",
})

# Provider requirements per budget verb. VISION IS THE HONEST CASE the owner named:
# a DeepSeek key does NOT enable echelon_vision — DeepSeek has no vision model — so
# "you have no key" would be a misleading refusal for a user who has one. The guard
# must distinguish NO KEY from WRONG-CAPABILITY KEY.
_TOOL_PROVIDERS: dict = {
    # tool: (accepted providers, human note when none are configured)
    "echelon_brainstorm": (("deepseek", "gemini", "anthropic", "xai"),
                           "a council needs at least one chat provider"),
    "echelon_scribe":     (("deepseek", "gemini", "anthropic", "xai"),
                           "scribe reads code and writes docs with a chat provider"),
    "echelon_swarm_subject": (("deepseek", "gemini", "anthropic", "xai"),
                              "a swarm fans out over a chat provider"),
    # vision is NOT satisfiable by deepseek — it needs a multimodal provider
    "echelon_vision":     (("gemini", "anthropic"),
                           "vision needs a MULTIMODAL provider (DeepSeek has no vision "
                           "model, so a DeepSeek key alone cannot run this verb)"),
}


def _caller_keys() -> dict:
    """Which providers the CURRENT caller has keys for, read from their own home.

    Reads the caller's ECHELON_HOME/.env rather than the process env, so the answer is
    about the member — never about the box. Returns {provider: 'ready'|'missing'}.
    """
    home = getattr(_REQ, "home", None)
    if not home:
        try:
            from echelon_sdk.keys import keys_summary
            return keys_summary()
        except Exception:
            return {}
    found: dict = {}
    env_path = Path(home) / ".env"
    wanted = {"DEEPSEEK_API_KEY": "deepseek", "GEMINI_API_VERTEX": "gemini",
              "GEMINI_API_KEY": "gemini", "ANTHROPIC_AUTH_TOKEN": "anthropic",
              "XAI_API_KEY": "xai", "LM_API_TOKEN": "lmstudio"}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            prov = wanted.get(k.strip().upper())
            if prov and v.strip():
                found[prov] = "ready"
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return found


def _capability_guard(name: str) -> str | None:
    """For a paid verb, return an HONEST refusal when the CALLER cannot fund it —
    or None to proceed. The owner is never guarded (they fund the box).

    Honesty is the whole point (owner 2026-08-15): the message must say which
    provider is missing and WHY this verb needs that class of provider, so a user
    holding a DeepSeek key is told "vision needs a multimodal provider", not the
    false "you have no key".
    """
    if name not in _BUDGET_TOOLS or _is_owner():
        return None
    accepted, why = _TOOL_PROVIDERS.get(name, ((), "this verb needs an LLM provider"))
    have = {p for p, s in _caller_keys().items() if s == "ready"}
    if have & set(accepted):
        return None
    if have:
        return (f"ERROR: `{name}` needs {' or '.join(accepted)} — {why}.\n"
                f"Your bank has a key for: {', '.join(sorted(have))}. That key cannot run "
                f"this verb.\nAdd one with:  echelon config set-key <provider> <key>\n"
                f"(keys live in YOUR home, are used only for YOUR calls, and are never "
                f"shared with the gateway owner.)")
    return (f"ERROR: `{name}` calls a paid LLM and no provider key is configured for your "
            f"bank, so there is nothing to run it with — {why}.\n"
            f"Add one with:  echelon config set-key <provider> <key>   "
            f"(accepted here: {', '.join(accepted)})\n"
            f"Your key stays in YOUR home and funds only YOUR calls. The gateway owner's "
            f"keys are never used for member requests.")
# safe CLI verbs a MEMBER may run through the raw /cli passthrough. `gate` is needed
# by the SessionStart banner hook; the rest are read-only / own-bank verbs. The full
# raw CLI (dispatch/proxy/group-scope/clone-cartridges/…) stays owner-only.
_MEMBER_CLI_VERBS: frozenset = frozenset({
    "gate", "recall", "remember", "inspect", "relive", "config", "status",
    "cartridge", "scan", "dream", "warmth",
})
# heavy maintenance verbs that hit the local model floor — allowed for members but
# noted; kept available since they operate ONLY on the member's own bank.
# (warmth/sleep/dream/graph stay member-visible.)


def _is_owner() -> bool:
    """True when the CURRENT request is the OWNER (full surface). Owner is decided by
    POSITIVE proof — the request presented the configured owner token — set on the
    thread-local in do_POST (owner 2026-07-02, closing the 'unknown token = owner'
    hole). A registered member marked role owner/admin also qualifies. Everything
    else (member, or — when NO owner token is configured for loopback dev — the
    default) follows _REQ.is_owner, which do_POST computes explicitly."""
    if getattr(_REQ, "is_owner", False):
        return True
    return getattr(_REQ, "role", None) in ("owner", "admin")


def _tool_allowed(name: str) -> bool:
    """Whether the current request's role may use this tool."""
    if _is_owner():
        return True
    if name == "echelon_cli":
        # raw argv: admin-role accounts and owner@example.com (owner 2026-08-16)
        return (getattr(_REQ, "role", None) == "admin"
                or (getattr(_REQ, "email", "") or "").lower() in _CLI_ALLOWED_EMAILS)
    return name not in _OWNER_ONLY_TOOLS


def _deny_reason(name: str) -> str:
    """The refusal a MEMBER sees for an owner-only tool — actionable, not opaque."""
    if name in _BUDGET_TOOLS:
        return (f"ERROR: `{name}` runs on the owner's LLM budget and is OWNER-ONLY on "
                "this shared gateway. To use council/scribe/vision/swarm, run YOUR OWN "
                "echelon gateway with your own API keys (see mcp/README.md → onboarding). "
                "Your memory tools (recall/remember/wrap/cartridge/…) work here as normal.")
    return (f"ERROR: `{name}` is OWNER-ONLY (it reaches beyond your own bank or "
            "administers the engine). Your isolated persona can use the memory + "
            "capability tools; cross-tenant/admin verbs are reserved to the owner.")


# ── OpenAPI doc for the /cli + /tail REST gateway (owner 2026-06-23) ──────────────
# The "broader coverage" door: anything that imports an OpenAPI/Swagger schema but
# can't speak MCP (OpenAI Actions/custom GPTs, n8n, Zapier, plain curl) reaches the
# SAME substrate CLI through ONE generic endpoint. NOT public — each user runs their
# own ECHELON_HOME bank; the Bearer token + per-home isolation IS the boundary, same
# as the Gemini×MCP / LM-Studio×MCP pattern. Raw argv passthrough: the CLI parses, the
# token authes — simpler, faster, portable, zero per-verb maintenance.
def _bank_stats_snapshot() -> str:
    """Fast bank stats for the OpenAPI description — no import-time DB hit."""
    try:
        from echelon_engine.atoms.cards import CardStore
        cs = CardStore()
        n = cs.count_atoms_in_scope("echelon")
        earned = cs.conn.execute(
            "SELECT COUNT(*) FROM atom_earned WHERE use_count > 0 AND scope='echelon'"
        ).fetchone()[0]
        return f"{n} atoms ({earned} earned)"
    except Exception:
        return "~/.echelon/echelon.db"


def _openapi_doc(base_url: str) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "ECHELON CLI Gateway", "version": SERVER_VERSION,
                 "description": (
                     "A thin HTTP door over the echelon substrate CLI. POST /cli "
                     "runs a command and returns its output; GET /tail streams a "
                     "command's output live over SSE. Bearer-token auth; each user "
                     "reaches their own substrate (ECHELON_HOME). "
                     "REFLEX: ① recall --warm before load-bearing actions "
                     "② remember <slug> through witnessed door "
                     "③ don't ask what bank knows "
                     "④ dispute atoms that misled you "
                     "⑤ scan integrity before wrap. "
                     f"Bank: ~/.echelon/echelon.db | scope=echelon | {_bank_stats_snapshot()}.")},
        "servers": [{"url": base_url}],
        "components": {"securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": {
            "/cli": {"post": {
                "operationId": "runCli",
                "summary": "Run an echelon CLI command and return its full output.",
                "description": "Body carries the raw argv (e.g. [\"recall\",\"--warm\",\"what is "
                               "echelon\"]). The server runs `echelon <argv>` and returns the "
                               "combined stdout/stderr text. Use this for normal (fast) verbs.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["argv"], "properties": {
                        "argv": {"type": "array", "items": {"type": "string"},
                                 "description": "The echelon CLI arguments, e.g. "
                                                "[\"recall\",\"--scope\",\"echelon\",\"--warm\",\"<intent>\"]."},
                        "timeout": {"type": "integer", "description": "seconds (default 180)."}}}}}},
                "responses": {"200": {"description": "command output",
                    "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "ok": {"type": "boolean"}, "output": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}}}}}}},
                    "401": {"description": "unauthorized (Bearer token required)"}}}},
            "/tail": {"get": {
                "operationId": "tailCli",
                "summary": "Run a command and STREAM its output line-by-line over SSE.",
                "description": "For the slow verbs (dream/sleep/wrap/warmth) where you want to "
                               "watch it run. Pass the argv as repeated ?a= query params, e.g. "
                               "/tail?a=sleep&a=--depth&a=full. Returns text/event-stream; each "
                               "stdout line is one SSE `data:` event; stream ends with [exit N].",
                "parameters": [{"name": "a", "in": "query", "required": True,
                    "schema": {"type": "array", "items": {"type": "string"}},
                    "style": "form", "explode": True,
                    "description": "One repeated ?a= per argv token (a=sleep&a=--depth&a=full)."}],
                "responses": {"200": {"description": "SSE stream of stdout lines",
                    "content": {"text/event-stream": {}}},
                    "401": {"description": "unauthorized"}}}},
        },
    }


# ── MCP stdio JSON-RPC loop ──────────────────────────────────────────────────────
def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(id_, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _dispatch(req: dict, client_proto: str | None = None) -> dict | None:
    """Handle one JSON-RPC request and RETURN its response dict (or None for a
    notification with no reply). Transport-agnostic: the stdio loop and the HTTP
    transport both feed requests here, so the two share ONE protocol implementation.
    `client_proto` (HTTP only) is the version the client offered at initialize — we
    echo it back so the negotiated version matches what the client expects."""
    method = req.get("method")
    id_ = req.get("id")
    params = req.get("params") or {}

    def ok(result):  return {"jsonrpc": "2.0", "id": id_, "result": result}
    def err(code, msg): return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}

    if method == "initialize":
        # echo the client's offered protocolVersion (falling back to ours) so a strict
        # client sees a version it sent — the negotiated-version check some clients run.
        offered = (params.get("protocolVersion") or client_proto or PROTOCOL_VERSION)
        return ok({
            "protocolVersion": offered,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            # the hook: the gate + the live environment block (OS + writable dir),
            # so the model recalls-first AND knows where it may write.
            "instructions": _INSTRUCTIONS + _environment_block(),
        })
    if method == "notifications/initialized":
        return None  # notification, no response
    if method == "tools/list":
        # inject the LIVE writable dir into the spec_path param description so the
        # model sees the CONCRETE full path to write a wrap spec to. Deep-copy so the
        # static schema dict isn't mutated across requests.
        import copy
        wdir = str(_writable_dir())
        sample = str(_writable_dir() / "session.json")
        tools = []
        for n, t in _TOOLS.items():
            if not _tool_allowed(n):
                continue   # a MEMBER never sees owner-only tools (role-based surface)
            schema = copy.deepcopy(t["schema"])
            sp = schema.get("properties", {}).get("spec_path")
            if sp is not None:
                sp["description"] = (
                    f"ABSOLUTE path to the wrap-spec JSON. Write your spec into the "
                    f"writable dir {wdir} first, then pass that full path here "
                    f"(e.g. {sample}). Never a relative/invented path — the file must exist.")
            tools.append({"name": n, "description": t["description"], "inputSchema": schema})
        return ok({"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = _TOOLS.get(name)
        if tool is None:
            return err(-32602, f"unknown tool: {name}")
        if not _tool_allowed(name):
            # enforce authz at the CALL layer too (not just hidden from list) — a
            # member cannot invoke an owner-only tool even by guessing the name.
            # (_OWNER_ONLY_TOOLS is empty since 2026-08-15; kept as the enforcement
            # seam so a future restriction cannot be list-only.)
            return ok({"content": [{"type": "text", "text": _deny_reason(name)}],
                       "isError": True})
        _cap = _capability_guard(name)
        if _cap:
            # NOT an authz denial — the caller MAY run this verb, they just have no
            # key that can fund it. The message says which provider and why.
            return ok({"content": [{"type": "text", "text": _cap}], "isError": True})
        try:
            text = tool["handler"](args)
        except KeyError as e:
            text = f"ERROR: missing required argument {e}"
        except Exception as e:  # noqa: BLE001
            text = f"ERROR: {e!r}"
        return ok({"content": [{"type": "text", "text": text}], "isError": text.startswith("ERROR")})
    if method == "ping":
        return ok({})
    if id_ is not None:
        return err(-32601, f"method not found: {method}")
    return None


def _handle(req: dict) -> None:
    """stdio path: dispatch + write the response to stdout (the JSON-RPC channel)."""
    resp = _dispatch(req)
    if resp is not None:
        _send(resp)


# ── MCP Streamable-HTTP transport (owner 2026-06-22: bind a port, tunnel to ─────
# your gateway host, register in Google Agent Builder). Stdlib http.server only — no
# new dep (the project's no-dependency contract). One always-on server, many clients.
#
# SPEC COMPLIANCE (the bug that bit: Agent Builder errored "Client failed to
# initialize"). The MCP Streamable-HTTP spec requires the SESSION handshake: the server
# issues an `Mcp-Session-Id` header on the initialize response, and the client MUST then
# send it back on every later request. A strict client treats a missing session id as
# "session not established" and aborts. So we: mint a session id on initialize, accept it
# (+ `MCP-Protocol-Version`) thereafter, honor the client's negotiated protocolVersion,
# answer either application/json OR text/event-stream per the client's Accept, and add
# CORS (exposing Mcp-Session-Id) for the browser-based console. DELETE ends a session.
# AUTH: a non-loopback host REQUIRES ECHELON_MCP_TOKEN; clients send Authorization: Bearer.
def _http_transport(host: str, port: int) -> int:
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = os.environ.get("ECHELON_MCP_TOKEN", "").strip()
    public = host not in ("127.0.0.1", "localhost", "::1")
    if public and not token:
        print("[echelon-mcp] REFUSING to bind a non-loopback host without ECHELON_MCP_TOKEN "
              "(set it in the work-folder .env). An open, internet-facing bank is unsafe.",
              file=sys.stderr)
        return 2

    sessions: set[str] = set()   # live session ids minted at initialize

    def _presented_token(headers, path: str = "") -> str | None:
        """The bare token the client presented (Bearer / api-key header / ?token=),
        whatever form. Used both to authenticate and to identify owner-vs-member."""
        auth = (headers.get("Authorization") or "").strip()
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        if auth:
            return auth
        for h in ("X-Api-Key", "token", "Token", "x-token", "Api-Key", "x-goog-api-key"):
            v = (headers.get(h) or "").strip()
            if v:
                return v
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(path).query)
        for p in ("token", "Authorization", "api_key", "key"):
            vals = [v.rstrip(")") for v in (q.get(p) or [])]
            if vals:
                return vals[0]
        return None

    def authorized(headers, path: str = "") -> bool:
        """AUTH (owner 2026-07-02, hardened): a request is authorized iff it presents
        EITHER the configured owner token OR a token registered in the user registry.
        Any OTHER token (or none) on a token-protected server is DENIED — an unknown
        token no longer silently gets in (it previously landed on the owner surface,
        a public-owner hole). Loopback dev with NO owner token configured still allows
        all (local trust only)."""
        if not token:
            return True  # loopback dev: no owner token configured, allow (local only)
        presented = _presented_token(headers, path)
        if not presented:
            return False
        if presented == token:
            return True  # the owner
        # a registered member token is authorized (to THEIR own bank + member surface)
        try:
            from echelon_engine.atoms import mcp_users as _mu
            return _mu.resolve(presented) is not None
        except Exception:
            return False

    debug = bool(os.environ.get("ECHELON_MCP_DEBUG"))
    debug_path = os.path.join(os.environ.get("ECHELON_HOME", "."), "mcp-debug.log")

    def _dbg(msg: str):
        if not debug:
            return
        try:
            import time as _t
            with open(debug_path, "a", encoding="utf-8") as lf:
                lf.write(f"[{_t.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # LOG EVERYTHING before any routing/auth — the lowest level. Catches GET probes,
        # OPTIONS preflights, HEAD, unknown methods, malformed requests (owner 2026-06-22:
        # "just log everything before rejecting connection"). raw_requestline + all headers
        # are captured the instant the request line is parsed, before do_* dispatch.
        def handle_one_request(self):
            try:
                super().handle_one_request()
            finally:
                pass

        def parse_request(self):
            ok = super().parse_request()
            if ok:
                try:
                    hdrs = {k: v for k, v in self.headers.items()}
                    _dbg(f">>> {self.command} {self.path} :: headers={json.dumps(hdrs)}")
                except Exception:
                    _dbg(f">>> {getattr(self,'requestline','?')!r}")
            else:
                _dbg(f">>> UNPARSEABLE requestline={getattr(self,'raw_requestline',b'')!r}")
            return ok

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version, Accept")
            self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id, MCP-Protocol-Version")

        def _send_payload(self, code: int, payload, *, session_id: str | None = None,
                          proto: str | None = None):
            """Reply to a JSON-RPC POST. Strict Streamable-HTTP clients (e.g. Google CES
            Agent Builder) open an SSE stream for the response and expect REAL SSE framing
            — NOT a fixed Content-Length body that merely has the SSE content-type. The
            working reference (DeepWiki) replies chunked, no Content-Length, with
            cache-control:no-cache + x-accel-buffering:no, then ends the stream. We mirror
            that: for SSE, omit Content-Length, add those stream headers, write the event,
            and close the connection to terminate the stream (single-response tools). The
            earlier Content-Length+keep-alive SSE looked done-but-broken to CES → 'Client
            failed to initialize'. Plain JSON path (non-SSE) is unchanged."""
            accept = (self.headers.get("Accept") or "")
            as_sse = "text/event-stream" in accept
            self.send_response(code)
            if session_id:
                self.send_header("Mcp-Session-Id", session_id)
            self.send_header("MCP-Protocol-Version", proto or PROTOCOL_VERSION)
            self._cors()
            if as_sse:
                body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")   # end the stream at close (no Content-Length)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True               # terminate the SSE stream cleanly
            else:
                body = json.dumps(payload).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def _plain(self, code: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            from urllib.parse import urlsplit
            route = urlsplit(self.path).path.rstrip("/")  # drop ?query before matching
            if route in ("/health", "/healthz"):
                self._plain(200, {"ok": True, "server": SERVER_NAME, "tools": len(_TOOLS)})
                return
            # the OpenAPI schema for the /cli + /tail REST gateway (no auth — it's just
            # the contract; the endpoints it describes still require the token).
            if route in ("/openapi.json", "/openapi"):
                scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
                hosthdr = self.headers.get("Host") or f"{host}:{port}"
                self._plain(200, _openapi_doc(f"{scheme}://{hosthdr}"))
                return
            # ── TWO-WAY SYNC (wave 3, 2026-08-02) ────────────────────────────────────
            # GET /sync/hello?scope=  — the handshake. Returns THIS bank's journal
            # generation + head, and the caller's push cursor as WE have it. Generations
            # are exchanged BEFORE cursors: if ours changed (restore / vault heal), the
            # client's cursor points into rewound history and it must re-baseline rather
            # than replay from a stale seq.
            # GET /sync/pull?since=&scope=&limit=  — our journal ops after the caller's
            # cursor, scope-filtered SERVER-SIDE (never trust the client to filter — a
            # local bank holds 100+ scopes incl. other clients' estates).
            if route in ("/sync/hello", "/sync/pull"):
                if not authorized(self.headers, self.path):
                    self._plain(401, {"ok": False, "error": "unauthorized (Bearer token required)"})
                    return
                try:
                    from urllib.parse import urlsplit as _uss, parse_qs as _pqs
                    q = _pqs(_uss(self.path).query)
                    ident = _sync_identity(self.headers, self.path)
                    if not ident:
                        self._plain(403, {"ok": False, "error": "unrecognized member token"})
                        return
                    scope = (q.get("scope") or [ident["scope"]])[0]
                    if scope != ident["scope"]:
                        # a member may ONLY sync their own scope; the token is the boundary
                        self._plain(403, {"ok": False, "error":
                                          f"token is bound to scope '{ident['scope']}'"})
                        return
                    conn = _sync_open(ident)
                    try:
                        from echelon_engine.atoms import sync_journal as _sj
                        if not _sj.generation(conn):
                            _sj.attach(conn, force=True)
                        if route == "/sync/hello":
                            peers = _sj.peer_state(conn, ident["peer"])
                            # ENGINE VERSION (owner 2026-08-02): sync reports version drift
                            # both ways, so shipping the engine becomes "bump the version"
                            # instead of a manual deploy hunt. Reported only — never
                            # auto-deployed.
                            # pyproject FIRST, then package metadata. With an editable install
                            # importlib.metadata returns the version at INSTALL time whenever
                            # the cwd is not the repo, so a metadata-only reading never sees a
                            # bump and the ship signal silently never fires (caught 2026-08-02).
                            # Resolved INLINE, not via export_import: an older deploy may not
                            # have that module at all (box4 didn't).
                            _ver = "unknown"
                            try:
                                import re as _re
                                _pj = Path(__file__).resolve().parent.parent / "pyproject.toml"
                                if _pj.exists():
                                    _m = _re.search(r'^version\s*=\s*["\']([^"\']+)["\']',
                                                    _pj.read_text(encoding="utf-8"), _re.M)
                                    if _m:
                                        _ver = _m.group(1)
                            except Exception:
                                pass
                            if _ver == "unknown":
                                try:
                                    from importlib.metadata import version as _pkgver
                                    _ver = _pkgver("echelon")
                                except Exception:
                                    _ver = "unknown"
                            self._plain(200, {"ok": True, "generation": _sj.generation(conn),
                                              "head": _sj.head(conn), "scope": scope,
                                              "engine": _ver,
                                              "peer_engine": (q.get("engine") or [""])[0],
                                              "your_cursor": peers["pushed_through"]})
                            return
                        since = int((q.get("since") or ["0"])[0])
                        limit = min(int((q.get("limit") or ["2000"])[0]), 5000)
                        ops = _sj.ops_since(conn, since, scope=scope, limit=limit)
                        self._plain(200, {"ok": True, "generation": _sj.generation(conn),
                                          "scope": scope, "ops": ops,
                                          "head": _sj.head(conn)})
                        return
                    finally:
                        conn.close()
                except Exception as e:  # noqa: BLE001
                    self._plain(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                    return

            # GET /backup/list — the caller's own cloud snapshots (token-auth; pairs with
            # POST /backup). Owner sees 'owner'; a member sees their own shelf only.
            if route == "/backup/list":
                if not authorized(self.headers, self.path):
                    self._plain(401, {"ok": False, "error": "unauthorized (Bearer token required)"})
                    return
                presented = _presented_token(self.headers, self.path)
                user = "owner" if ((not token) or presented == token) else None
                if user is None:
                    try:
                        from echelon_engine.atoms import mcp_users as _mu3
                        rec3 = _mu3.resolve(presented)
                        user = rec3["db"] if rec3 else None
                    except Exception:
                        user = None
                if user is None:
                    self._plain(403, {"ok": False, "error": "unrecognized member token"})
                    return
                shelf = Path(os.environ.get("ECHELON_HOME", str(Path.home() / ".echelon"))) \
                    / "cloud_banks" / user
                snaps = []
                if shelf.is_dir():
                    for p in sorted(shelf.glob("*.gz"), key=lambda x: (x.stat().st_mtime_ns, x.name), reverse=True):
                        snaps.append({"name": p.name, "bytes": p.stat().st_size,
                                      "mtime": time.strftime("%Y-%m-%d %H:%M UTC",
                                                             time.gmtime(p.stat().st_mtime))})
                self._plain(200, {"ok": True, "user": user, "snapshots": snaps})
                return

            # GET /backup/fetch[?name=<snapshot>] — download a cloud snapshot from the
            # caller's OWN shelf (token-auth; pairs with POST /backup + /backup/list).
            # No ?name → the newest. The new-machine restore door (owner 2026-07-30:
            # re-establish via email+code reveals the token; this pulls the bank back).
            if route == "/backup/fetch":
                if not authorized(self.headers, self.path):
                    self._plain(401, {"ok": False, "error": "unauthorized (Bearer token required)"})
                    return
                presented = _presented_token(self.headers, self.path)
                user = "owner" if ((not token) or presented == token) else None
                if user is None:
                    try:
                        from echelon_engine.atoms import mcp_users as _mu4
                        rec4 = _mu4.resolve(presented)
                        user = rec4["db"] if rec4 else None
                    except Exception:
                        user = None
                if user is None:
                    self._plain(403, {"ok": False, "error": "unrecognized member token"})
                    return
                from urllib.parse import urlsplit as _us4, parse_qs as _pq4
                name = (_pq4(_us4(self.path).query).get("name") or [""])[0]
                shelf = Path(os.environ.get("ECHELON_HOME", str(Path.home() / ".echelon"))) \
                    / "cloud_banks" / user
                if name:
                    # plain filename only — no traversal out of the caller's shelf
                    if name != Path(name).name or not name.endswith(".gz"):
                        self._plain(400, {"ok": False, "error": "name must be a plain *.gz filename"})
                        return
                    snap = shelf / name
                else:
                    snaps2 = sorted(shelf.glob("*.gz"), key=lambda p: (p.stat().st_mtime_ns, p.name)) \
                        if shelf.is_dir() else []
                    snap = snaps2[-1] if snaps2 else None
                if snap is None or not snap.is_file():
                    self._plain(404, {"ok": False, "error": "no snapshot found (push one first: "
                                      "echelon backup --push)"})
                    return
                blob = snap.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", f'attachment; filename="{snap.name}"')
                self.send_header("Content-Length", str(len(blob)))
                self._cors()
                self.end_headers()
                self.wfile.write(blob)
                return

            # GET /api/public/stats — the ONE public, read-only aggregate of THIS host's
            # live bank (OPEN-0107, owner 2026-09-06). Pre-auth like /health/atlas, but
            # COUNTS ONLY — no atom bodies, names, or scope list ever cross this route.
            # CORS is scoped to the landing origin (not the wildcard the rest of the
            # gateway uses). Cached 60 s in public_stats; a public page must never 500,
            # so a compute error returns {ok:false} and the page keeps its baked snapshot.
            if route == "/api/public/stats":
                try:
                    from echelon_engine.atoms.public_stats import public_stats as _pubstats
                    payload = _pubstats()
                except Exception as e:  # noqa: BLE001
                    payload = {"ok": False, "error": type(e).__name__}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # CORS: the landing page only. A public GET needs no CORS to be fetched
                # same-origin, but the owner asked it be scoped to your gateway host.
                self.send_header("Access-Control-Allow-Origin", "https://your gateway host")
                self.send_header("Vary", "Origin")
                self.send_header("Cache-Control", "public, max-age=60")
                self.end_headers()
                self.wfile.write(body)
                return

            # GET /atlas — the PUBLIC memory-graph (owner 2026-07-01, restored 2026-07-02).
            # Pre-auth like /health, but SCOPE-LOCKED to the mol family so it never leaks
            # the whole estate: ?scope= is honored ONLY if it names a PUBLIC scope; anything
            # else silently falls back to 'mol'. Renders graph_viz's force-directed D3 HTML
            # with a scope picker injected into the header.
            if route == "/atlas":
                from urllib.parse import urlsplit as _us, parse_qs as _pq
                # DEMO-ONLY (owner 2026-07-30): the public atlas must show GENERATED data,
                # never real memory — the previous mol-family lock still exposed a real
                # project's atoms. The 'demo' scope is synthetic (scripts/gen_demo_scope.py).
                PUBLIC_SCOPES = ("demo",)
                req_scope = (_pq(_us(self.path).query).get("scope") or ["demo"])[0]
                scope = req_scope if req_scope in PUBLIC_SCOPES else "demo"
                try:
                    from echelon_engine.atoms import graph_viz
                    data = graph_viz.build(scope=scope)
                    html = graph_viz.render_html(data)
                    # inject a scope picker into the header (before the theme/hdr close)
                    opts = "".join(
                        f'<option value="{s}"{" selected" if s == scope else ""}>{s}</option>'
                        for s in PUBLIC_SCOPES)
                    picker = (f'<select id="scopepick" onchange="location.search=\'?scope=\'+this.value" '
                              f'style="margin-left:12px;background:#111;color:#e5e5e5;border:1px solid #333;'
                              f'border-radius:6px;padding:3px 6px;">{opts}</select>')
                    if '<span class="stat">' in html:
                        html = html.replace('<span class="stat">', picker + '<span class="stat">', 1)
                    body = html.encode("utf-8")
                    self.send_response(200); self._cors()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                except Exception as e:  # never 500 the public page silently
                    msg = f"<pre>atlas unavailable: {e!r}</pre>".encode("utf-8")
                    self.send_response(200); self._cors()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers(); self.wfile.write(msg)
                return
            # GET /steer/report?session=<id> — the cold-install runbook so far (what ran,
            # exits, the model's findings). Observability for the steered install.
            if route == "/steer/report":
                if not authorized(self.headers, self.path):
                    self.send_response(401); self._cors()
                    self.send_header("Content-Length", "0"); self.end_headers()
                    return
                from urllib.parse import urlsplit as _us, parse_qs as _pq
                sid = (_pq(_us(self.path).query).get("session") or [""])[0]
                try:
                    from deploy import steer_install as _steer
                except Exception:
                    import importlib
                    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    if _here not in sys.path:
                        sys.path.insert(0, _here)
                    _steer = importlib.import_module("deploy.steer_install")
                self._plain(200, _steer.session_report(sid))
                return
            # GET /tail — launch a command and STREAM its stdout over SSE. argv = repeated
            # ?a= params (a=sleep&a=--depth&a=full). Token-gated like everything mutating.
            if route == "/tail":
                if not authorized(self.headers, self.path):
                    self.send_response(401); self._cors()
                    self.send_header("Content-Length", "0"); self.end_headers()
                    return
                from urllib.parse import urlsplit, parse_qs
                argv = parse_qs(urlsplit(self.path).query).get("a", [])
                if not argv:
                    self._plain(400, {"ok": False, "error": "pass argv as repeated ?a= params"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self._cors()
                self.end_headers()
                try:
                    for line in _stream_echelon(argv):
                        chunk = f"data: {json.dumps(line)}\n\n".encode("utf-8")
                        self.wfile.write(chunk); self.wfile.flush()
                    self.wfile.write(b"event: done\ndata: \"[stream-end]\"\n\n"); self.wfile.flush()
                except Exception:
                    pass  # client disconnected — normal teardown
                self.close_connection = True
                return
            # GET /mcp = OPEN THE SERVER->CLIENT SSE STREAM (Streamable HTTP spec). Strict
            # clients (Google CES) initialize, then GET /mcp to receive server-initiated
            # messages; a 405 here aborts their session (the bug — seen live: CES POSTed
            # initialize fine, then GET /mcp got 405 and it gave up). We have no
            # server-initiated messages, so we hold an EMPTY keep-alive SSE stream open
            # (periodic comment pings) until the client disconnects. Each request has its
            # own thread (ThreadingHTTPServer), so blocking here is fine.
            if not authorized(self.headers, self.path):
                self.send_response(401); self._cors()
                self.send_header("Content-Length", "0"); self.end_headers()
                return
            import time as _t
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            sid = self.headers.get("Mcp-Session-Id")
            if sid:
                self.send_header("Mcp-Session-Id", sid)
            self.send_header("MCP-Protocol-Version",
                             self.headers.get("MCP-Protocol-Version") or PROTOCOL_VERSION)
            self._cors()
            self.end_headers()
            try:
                # an initial comment so the client sees the stream is live, then keepalives.
                self.wfile.write(b": stream open\n\n"); self.wfile.flush()
                while True:
                    _t.sleep(15)
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
            except Exception:
                pass  # client disconnected — normal stream teardown

        def do_DELETE(self):
            # session teardown (spec): client DELETEs with its Mcp-Session-Id.
            sid = self.headers.get("Mcp-Session-Id")
            if sid:
                sessions.discard(sid)
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
            except Exception:  # noqa: BLE001
                raw = b""
            # TEMP DEBUG (owner 2026-06-22): log exactly what the client sends so we can
            # see why Agent Builder's MCP client fails to initialize. Remove when solved.
            if os.environ.get("ECHELON_MCP_DEBUG"):
                try:
                    hdrs = {k: v for k, v in self.headers.items()}
                    with open(os.path.join(os.environ.get("ECHELON_HOME", "."), "mcp-debug.log"), "a", encoding="utf-8") as lf:
                        lf.write(f"\n--- POST {self.path}\nheaders={json.dumps(hdrs)}\nbody={raw.decode('utf-8','replace')}\n")
                except Exception:
                    pass
            # PRE-AUTH BY DESIGN: /recover/exchange authenticates by the emailed
            # recovery code, not a bearer token — a new machine has no token yet
            # (owner 2026-07-30). The route itself burns the code before answering.
            from urllib.parse import urlsplit as _us0
            _preauth = _us0(self.path).path.rstrip("/") == "/recover/exchange"
            if not _preauth and not authorized(self.headers, self.path):
                self._plain(401, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32001, "message": "unauthorized (Bearer token required)"}})
                return

            # SINGLE-PORT MULTI-USER (owner 2026-07-02): resolve the caller's own bank
            # from their token and pin it to this thread for the duration of the request.
            # A token in the registry → that user's isolated db; anything else (owner
            # token / loopback) → the default home (unchanged). Bank is lazily created +
            # seeded on first contact.
            _REQ.home = None
            _REQ.scope = None
            _REQ.role = None
            _REQ.email = None
            # OWNER IDENTITY = positive proof (owner 2026-07-02): the request presents
            # the configured owner token. When NO owner token is configured (loopback
            # dev), the server is local-trust and every caller is treated as owner.
            _presented = _presented_token(self.headers, self.path)
            _REQ.is_owner = (not token) or (_presented == token)
            try:
                from echelon_engine.atoms import mcp_users as _mu
                _tok = _mu.bearer_from_headers(self.headers, self.path)
                _rec = _mu.resolve(_tok)
                if _rec and _presented != token:
                    # a registered MEMBER (never let the owner token be shadowed by a
                    # registry row — owner identity wins)
                    _REQ.home = _rec["home"]
                    _REQ.scope = _rec["scope"]
                    # a registry user is a MEMBER unless explicitly owner/admin — this
                    # is what bounds their tool surface (role-based capability model).
                    _REQ.role = _rec.get("role") or "member"
                    _REQ.email = (_rec.get("email") or "").strip().lower()
                    if not os.path.exists(os.path.join(_rec["home"], "echelon.db")):
                        # first contact for this user → create + seed their bank.
                        # _ensure_home mutates shared os.environ, so serialize + restore.
                        with _SEED_LOCK:
                            if not os.path.exists(os.path.join(_rec["home"], "echelon.db")):
                                _saved = {k: os.environ.get(k) for k in
                                          ("ECHELON_HOME", "ECHELON_SCOPE", "ECHELON_TEAMMATE_SAFE")}
                                try:
                                    os.environ["ECHELON_TEAMMATE_SAFE"] = "1"
                                    os.environ["ECHELON_SCOPE"] = _rec["scope"]
                                    _ensure_home(Path(_rec["home"]))
                                finally:
                                    for k, v in _saved.items():
                                        if v is None:
                                            os.environ.pop(k, None)
                                        else:
                                            os.environ[k] = v
            except Exception:
                # on any resolution error, fall back to member-least-privilege unless
                # this is provably the owner token (fail safe, never fail to owner).
                _REQ.home = None
                _REQ.scope = None
                _REQ.role = None if _REQ.is_owner else "member"
                _REQ.email = None

            # POST /cli — the REST gateway: raw argv passthrough to the echelon CLI.
            # Body = {"argv":[...], "timeout"?:N}. The CLI parses; we just run it and
            # return its text. Separate from the JSON-RPC /mcp path below.
            from urllib.parse import urlsplit as _urlsplit
            if _urlsplit(self.path).path.rstrip("/") == "/cli":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception as e:  # noqa: BLE001
                    self._plain(400, {"ok": False, "error": f"parse error: {e}"})
                    return
                argv = body.get("argv")
                if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv) or not argv:
                    self._plain(400, {"ok": False,
                                      "error": "body must be {\"argv\": [\"<verb>\", ...]} — a non-empty list of strings"})
                    return
                # ROLE GATE on the raw /cli passthrough (owner 2026-07-02): /cli can run
                # ANY verb, so it would bypass the tool-level role model. A MEMBER may only
                # run a small safe verb allowlist here (the banner hook needs `gate`); the
                # full raw CLI is owner-only. Their typed MCP tools remain the real door.
                if not _is_owner():
                    verb = argv[0]
                    if verb not in _MEMBER_CLI_VERBS:
                        self._plain(403, {"ok": False, "argv": argv,
                            "error": (f"/cli verb '{verb}' is owner-only. Members use the typed "
                                      "echelon_* MCP tools (already scoped to your bank); the raw "
                                      "CLI passthrough is reserved to the owner.")})
                        return
                out = _run_echelon(argv, timeout=int(body.get("timeout") or 180))
                self._plain(200, {"ok": not out.startswith("ERROR"), "argv": argv, "output": out})
                return

            # POST /recover/exchange {email, code} — the TOKEN half of email+code
            # recovery (owner 2026-07-30: "the mcp server should be accepting email
            # and code — it will close vulnerability too"). Pre-auth BY DESIGN: the
            # code (emailed to the PAIRED address, hashed at rest, 15-min TTL, 5
            # attempts, burn-on-use) IS the credential. Members get a ROTATED token
            # (the lost machine's token dies); an admin account gets the owner
            # gateway token. The bearer token travels only over this API — never a
            # browser page.
            if _urlsplit(self.path).path.rstrip("/") == "/recover/exchange":
                try:
                    rbody = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception as e:  # noqa: BLE001
                    self._plain(400, {"ok": False, "error": f"parse error: {e}"})
                    return
                from echelon_engine.atoms import recovery as _rc
                acct = _rc.find_paired_account(str(rbody.get("email") or ""))
                if not acct or not _rc.check_code(acct["email"], str(rbody.get("code") or "")):
                    self._plain(400, {"ok": False, "error": "code invalid or expired"})
                    return
                if acct.get("role") == "admin":
                    issued = token or ""
                    rotated = False
                else:
                    issued, rotated = "", False
                    try:
                        from echelon_engine.atoms import mcp_users as _mu5
                        hit = next((r for r in _mu5.load_registry().values()
                                    if (r or {}).get("email", "").strip().lower()
                                    == acct["email"].lower()), None)
                        if hit and hit.get("db"):
                            rec5 = _mu5.add_user(db=hit["db"], scope=hit.get("scope"),
                                                 email=acct["email"],
                                                 role=hit.get("role") or "member")
                            issued, rotated = rec5["token"], True
                    except Exception:
                        issued, rotated = "", False
                if not issued:
                    self._plain(404, {"ok": False, "error": "no provisioned access found for "
                                      "this account — ask the owner for an invite"})
                    return
                self._plain(200, {"ok": True, "token": issued, "token_rotated": rotated,
                                  "email": acct["email"],
                                  "restore": "GET /backup/fetch with this token → your "
                                             "latest bank snapshot"})
                return

            # POST /sync/push — apply the caller's journal batch to THEIR OWN bank under the
            # merge law (history EVENTS keyed by witnessing bank; see sync_journal.apply_ops).
            # Body = gzipped {generation, scope, ops}. Returns the seq consumed through, which
            # the client stores only after this returns (ack-after-apply).
            #
            # CROSS-TENANT DISCIPLINE (cross-tenant-leaks-hide-in-in-process-writes): this is
            # an IN-PROCESS WRITE on a multi-user gateway — exactly the shape that leaked into
            # the owner bank in the 2026-07-02 build. It opens the bank via _sync_open(ident),
            # which resolves the caller's OWN home from their token, never the module-global
            # default. Scope is re-checked here too; the token is the boundary.
            if _urlsplit(self.path).path.rstrip("/") == "/sync/push":
                if not authorized(self.headers, self.path):
                    self._plain(401, {"ok": False, "error": "unauthorized (Bearer token required)"})
                    return
                try:
                    ident = _sync_identity(self.headers, self.path)
                    if not ident:
                        self._plain(403, {"ok": False, "error": "unrecognized member token"})
                        return
                    body = raw
                    if (self.headers.get("Content-Encoding") or "").lower() == "gzip" \
                            or (self.headers.get("Content-Type") or "").endswith("gzip"):
                        import gzip as _gz
                        body = _gz.decompress(raw)
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    scope = str(payload.get("scope") or ident["scope"])
                    if scope != ident["scope"]:
                        self._plain(403, {"ok": False, "error":
                                          f"token is bound to scope '{ident['scope']}'"})
                        return
                    ops = payload.get("ops") or []
                    if not isinstance(ops, list):
                        self._plain(400, {"ok": False, "error": "ops must be a list"})
                        return
                    # server-side scope filter: never apply an op the token has no claim to,
                    # even if the client mislabels the batch.
                    ops = [o for o in ops if str(o.get("scope") or "") == scope]
                    conn = _sync_open(ident)
                    try:
                        from echelon_engine.atoms import sync_journal as _sj
                        if not _sj.generation(conn):
                            _sj.attach(conn, force=True)
                        applied = _sj.apply_ops(conn, ops,
                                                origin=str(payload.get("generation") or "peer"))
                        through = max((int(o.get("seq") or 0) for o in ops), default=0)
                        # remember how far THIS peer has pushed, so /sync/hello can tell them
                        _sj.set_peer_state(conn, ident["peer"],
                                           peer_generation=str(payload.get("generation") or ""),
                                           pushed_through=through)
                        self._plain(200, {"ok": True, "applied": applied, "through": through,
                                          "applied_ops": len(ops), "scope": scope,
                                          "generation": _sj.generation(conn)})
                        return
                    finally:
                        conn.close()
                except Exception as e:  # noqa: BLE001
                    self._plain(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                    return

            # POST /backup — CLOUD BANK BACKUP (owner 2026-07-30: "if local machine has
            # something happened i wont lose all of our memory together"). Body = a gzipped
            # sqlite image (client: `echelon backup --push`, WAL-safe serialize()). Stored
            # per-identity under ECHELON_HOME/cloud_banks/<user>/, timestamped, last 14 kept.
            # Owner token → 'owner'; a registered member → their db name. Same door for the
            # whole team — each invite token gets its own isolated snapshot shelf.
            if _urlsplit(self.path).path.rstrip("/") == "/backup":
                MAX_BACKUP = 512 * 1024 * 1024
                if not raw:
                    self._plain(400, {"ok": False, "error": "empty body — send the gzipped bank "
                                      "(client: echelon backup --push)"})
                    return
                if len(raw) > MAX_BACKUP:
                    self._plain(413, {"ok": False, "error": f"backup too large (> {MAX_BACKUP} bytes)"})
                    return
                try:
                    import gzip as _gz
                    import io as _io
                    head = _gz.GzipFile(fileobj=_io.BytesIO(raw)).read(16)
                    if not head.startswith(b"SQLite format 3"):
                        self._plain(400, {"ok": False, "error": "body does not decompress to a sqlite db"})
                        return
                except Exception as e:  # noqa: BLE001
                    self._plain(400, {"ok": False, "error": f"not valid gzip: {e}"})
                    return
                user = "owner"
                if not _REQ.is_owner:
                    try:
                        from echelon_engine.atoms import mcp_users as _mu2
                        rec2 = _mu2.resolve(_presented_token(self.headers, self.path))
                        user = rec2["db"] if rec2 else None
                    except Exception:
                        user = None
                    if not user:
                        self._plain(403, {"ok": False, "error": "unrecognized member token"})
                        return
                bank_name = (self.headers.get("X-Bank-Name") or "echelon.db").strip()
                safe_bank = "".join(c for c in bank_name if c.isalnum() or c in "._-") or "echelon.db"
                shelf = Path(os.environ.get("ECHELON_HOME", str(Path.home() / ".echelon"))) \
                    / "cloud_banks" / user
                shelf.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                dest = shelf / f"{safe_bank}.{stamp}.gz"
                dest.write_bytes(raw)
                kept = sorted(shelf.glob(f"{safe_bank}.*.gz"), key=lambda p: (p.stat().st_mtime_ns, p.name))
                for old in kept[:-14]:
                    old.unlink()
                self._plain(200, {"ok": True, "user": user, "stored": dest.name,
                                  "bytes": len(raw), "kept": min(len(kept), 14)})
                return

            # POST /steer/register | /steer/next — the cold-install STEERING brain (the
            # desktop side of agent_probe.py). register mints a session from box facts;
            # next folds the box's last result + returns the next action (DeepSeek+ops).
            steer_path = _urlsplit(self.path).path.rstrip("/")
            if steer_path in ("/steer/register", "/steer/next"):
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception as e:  # noqa: BLE001
                    self._plain(400, {"ok": False, "error": f"parse error: {e}"})
                    return
                try:
                    from deploy import steer_install as _steer
                except Exception:
                    import importlib, os as _os
                    _here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                    if _here not in sys.path:
                        sys.path.insert(0, _here)
                    _steer = importlib.import_module("deploy.steer_install")
                try:
                    if steer_path == "/steer/register":
                        self._plain(200, _steer.register(body.get("facts") or {}))
                    else:
                        self._plain(200, _steer.next_action(body.get("session", ""), body.get("result")))
                except Exception as e:  # noqa: BLE001 — never crash the gateway on a steer turn
                    self._plain(500, {"action": "wait", "secs": 5, "error": repr(e)})
                return

            try:
                req = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception as e:  # noqa: BLE001
                self._plain(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"parse error: {e}"}})
                return

            # Detect the initialize call so we can MINT a session id + echo the client's
            # negotiated protocol version (the handshake a strict client requires).
            first = req[0] if isinstance(req, list) and req else req
            is_init = isinstance(first, dict) and first.get("method") == "initialize"
            client_proto = None
            if is_init:
                client_proto = ((first.get("params") or {}).get("protocolVersion")) or PROTOCOL_VERSION
            new_sid = str(uuid.uuid4()) if is_init else None

            try:
                if isinstance(req, list):
                    out = [r for r in (_dispatch(x, client_proto) for x in req) if r is not None]
                    payload = out if out else {"ok": True}
                else:
                    resp = _dispatch(req, client_proto)
                    payload = resp if resp is not None else {"ok": True}
            except Exception as e:  # noqa: BLE001 — never crash the server on one bad call
                self._plain(500, {"jsonrpc": "2.0", "id": (first or {}).get("id"),
                                  "error": {"code": -32603, "message": repr(e)}})
                return

            if new_sid:
                sessions.add(new_sid)

            # NOTIFICATIONS / responses with no result -> MCP spec says HTTP 202 Accepted
            # with an EMPTY body, NOT a 200 with a JSON body. The live CES (Java SDK MCP
            # Client) died right after POST notifications/initialized because we replied
            # 200 {"ok":true}; a strict client expects 202 + nothing and aborts otherwise.
            # A request had an id and produced a real result -> stream it normally.
            req_is_notification = isinstance(req, dict) and req.get("id") is None
            if req_is_notification:
                self.send_response(202)
                if new_sid:
                    self.send_header("Mcp-Session-Id", new_sid)
                self.send_header("MCP-Protocol-Version", client_proto or PROTOCOL_VERSION)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            self._send_payload(200, payload, session_id=new_sid, proto=client_proto)

        def log_message(self, *a):  # quiet; route logs to stderr ourselves if needed
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[echelon-mcp] HTTP transport on http://{host}:{port}/mcp  "
          f"(health: /health · REST: POST /cli, GET /tail, /openapi.json) — {len(_TOOLS)} tools — "
          f"auth={'token' if token else 'OPEN (loopback only)'} — streamable-http (session+sse)",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="echelon-mcp",
        description="MCP server that equips the ECHELON substrate (stdio, or HTTP on a port).")
    ap.add_argument("--home", default=None,
                    help="work folder for the substrate (dbs + .env). Sets ECHELON_HOME. "
                         "Created + schema-migrated on first start; a .env template is seeded.")
    ap.add_argument("--http", action="store_true",
                    help="serve MCP over Streamable HTTP (always-on, many clients) instead of stdio. "
                         "Use for the :8888 tunnel to Google Agent Builder.")
    ap.add_argument("--host", default=os.environ.get("ECHELON_MCP_HOST", "127.0.0.1"),
                    help="HTTP bind host (default 127.0.0.1; use 0.0.0.0 behind the tunnel). "
                         "A non-loopback host REQUIRES ECHELON_MCP_TOKEN.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ECHELON_MCP_PORT", "8888")),
                    help="HTTP bind port (default 8888).")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else (
        Path(os.environ["ECHELON_HOME"]).expanduser() if os.environ.get("ECHELON_HOME")
        else Path.home() / ".echelon")
    status = _ensure_home(home)
    # startup log goes to STDERR (stdout is the JSON-RPC channel — must stay clean)
    print(f"[echelon-mcp] home={status['home']} dbs={status.get('dbs')} "
          f"env_seeded={status['env_seeded']} — {len(_TOOLS)} tools ready", file=sys.stderr)

    # HTTP transport: an always-on networked server (the :8888 tunnel target).
    if args.http:
        # MULTI-TENANT MODE ⇒ KEY ISOLATION IS NOT OPTIONAL (owner 2026-08-15).
        # --http is exactly the shape that serves other people, so the flag is set
        # HERE rather than left to systemd: a fix that depends on remembering an
        # Environment= line fails OPEN on the one deploy that forgets it, and the
        # failure is silent (a member's call quietly spends the owner's key). Set it
        # in code and the guarantee travels with the binary.
        os.environ.setdefault("ECHELON_KEYS_ISOLATED", "1")
        print("[echelon-mcp] key isolation ON — provider keys resolve per-caller "
              "from their own home (.env); no cross-tenant fallback.", file=sys.stderr)
        return _http_transport(args.host, args.port)

    # stdio transport (default): one client spawns this process and talks JSON-RPC.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(req)
        except Exception as e:  # noqa: BLE001 — one bad request must not kill the server
            print(f"[echelon-mcp] handler error: {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
