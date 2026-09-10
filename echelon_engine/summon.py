"""echelon summon — the claude-echelon door as ONE verb (chat by default, bus optional).

THE GAP THIS CLOSES (owner, 2026-07-08): claude-echelon (the courtyard harness at
D:\\WORK\\CLAUDE-ECHELON — ECHELON runs Claude, Claude remembers through echelon, nerve
connected) worked, but summoning it needed tribal knowledge re-explained every session:
where it lives, PYTHONPATH, --scope, --interactive, utf8. This verb bakes all of it in:

  echelon summon                          # chat with a nerve-connected Claude peer
  echelon summon "audit the invoice flow" # chat, opening with a goal
  echelon summon --once "do X"            # one-shot worker (dies with its context)
  echelon summon --peer judge "gate v1"   # PERSISTENT named peer: scriptable like
  echelon summon --peer judge "re-gate"   #   --once, but the SAME session resumes —
                                          #   the peer SLEEPS between calls, never dies
                                          #   (owner 2026-07-23: --once from a directed
                                          #   agent always killed the peer mid-arc)
  echelon summon --bus reviews --session s7 --role auditor "discuss slice 2"
                                          # summon the peer INTO a society bus channel

The courtyard stays its own repo (claude_echelon is the guts; this only routes) — found
via the CLAUDE_ECHELON_DIR env, an installed package, or the default estate location.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from . import estate as _estate

DEFAULT_HOME = _estate.sibling("claude-echelon", "CLAUDE-ECHELON")


def _find_home(override: str | None) -> Path | None:
    """Locate the claude-echelon repo (None = the package is importable, no path needed)."""
    if override:
        return Path(override)
    try:
        import claude_echelon  # noqa: F401 — pip-installed, no PYTHONPATH needed
        return None
    except ImportError:
        pass
    env = os.environ.get("CLAUDE_ECHELON_DIR")
    if env:
        return Path(env)
    return DEFAULT_HOME


def _gate_loop_banner() -> None:
    """Surface the gate-loop cartridge whenever someone summons — the delegation-phase
    doctrine (summon the JUDGE as loop-driver, don't be the middleman relay). Read live
    from the registry so the summary stays in sync with the cartridge. Silent if absent."""
    try:
        import json
        reg = Path.home() / ".echelon" / "cartridges.json"
        if not reg.exists():
            return
        specs = json.loads(reg.read_text(encoding="utf-8"))
        gl = next((c for c in specs if c.get("name") == "gate-loop"), None)
        if not gl:
            return
        print("⚑ GATE-LOOP CARTRIDGE — for iterative quality gates, don't relay verdicts by hand:")
        print(f"   {gl.get('summary', '')}")
        skill = gl.get("skill")
        print(f"   equip: echelon cartridge equip gate-loop \"<goal>\"" +
              (f"   ·   /skill {skill}" if skill else ""))
        print("─" * 60)
    except Exception:
        pass


PEERS_REG = Path.home() / ".echelon" / "peers.json"


def _peers_load() -> dict:
    try:
        import json
        return json.loads(PEERS_REG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _peers_save(reg: dict) -> None:
    import json
    PEERS_REG.parent.mkdir(parents=True, exist_ok=True)
    PEERS_REG.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def _resolve_scope(explicit: str | None, cwd: str) -> str:
    """--scope wins; otherwise resolve the cwd to its bank scope (a summon lands where you stand)."""
    if explicit:
        return explicit
    try:
        from .atoms.resolve_scope import resolve_scope
        return resolve_scope(cwd)
    except Exception:
        return os.environ.get("ECHELON_SCOPE", "echelon")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon summon",
        description="summon a claude-echelon peer (nerve-connected Claude) — "
                    "chat by default, --once for a one-shot worker, --bus to join a society channel")
    ap.add_argument("goal", nargs="*", help="opening goal (optional in chat mode)")
    ap.add_argument("--scope", default=None,
                    help="bank scope (default: resolved from the cwd, like resolve-scope)")
    ap.add_argument("--model", default=None,
                    help="worker model tier (e.g. sonnet, opus; default: CLI default)")
    ap.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                    help="reasoning-effort tier for the worker (default: harness default, high)")
    ap.add_argument("--provider", default=None, required=True,
                    choices=["claude", "anthropic", "gemini", "deepseek"],
                    help="REQUIRED backend selector — no silent default to a cheap provider. "
                         "'claude'/'anthropic' = real Anthropic (no proxy); 'gemini'/'deepseek' "
                         "= route through that provider's NERVE-TRANSLATING proxy (warmth-read + "
                         "seed-write per request, richer than hook-events). Same persistent-peer "
                         "loophole works on every provider.")
    ap.add_argument("--cwd", default=None,
                    help="working directory for the peer (default: where you summon from)")
    ap.add_argument("--once", action="store_true",
                    help="one-shot: run the goal and close — the peer DIES with its context; "
                         "for anything iterative (a gate loop, a multi-call arc) use --peer")
    ap.add_argument("--peer", default=None, metavar="NAME",
                    help="PERSISTENT named peer: scriptable like --once (one message per "
                         "call, no stdin), but the SAME SDK session resumes across calls — "
                         "the peer sleeps between calls instead of dying. Registry: "
                         "~/.echelon/peers.json, keyed scope:name. `--peer-list` surveys; "
                         "`--peer NAME --end` retires one")
    ap.add_argument("--resume", default=None, metavar="SESSION_ID",
                    help="resume a prior SDK session by its traced id (printed on every "
                         "run's close) — restores that session's full context + cache. "
                         "Works with --once (one-shot resumed run) or --peer (adopt the id "
                         "under a peer name). The accurate counterpart to the session trace.")
    ap.add_argument("--peer-list", action="store_true",
                    help="list the registered persistent peers and exit")
    ap.add_argument("--end", action="store_true",
                    help="with --peer NAME: retire the peer (drop its registry entry)")
    ap.add_argument("--max-turns", type=int, default=None,
                    help="turn budget for --once runs (default 40; real implementation WPs need 80+)")
    ap.add_argument("--home", default=None,
                    help="claude-echelon repo dir (default: CLAUDE_ECHELON_DIR env, "
                         f"or {DEFAULT_HOME})")
    # ── bus mode: summon the peer INTO a society channel ──
    ap.add_argument("--bus", default=None, metavar="CHANNEL",
                    help="join a society bus CHANNEL: the peer gets the composed role brief "
                         "(protocol + absolute bus-post command) as its opening goal")
    ap.add_argument("--session", default="default",
                    help="society session for --bus (the shared 'default' is refused — convene one)")
    ap.add_argument("--bus-path", default=None, help="explicit bus db path (overrides --session)")
    ap.add_argument("--role", default="peer",
                    help="role name for --bus (registered roles auto-equip their cartridge; "
                         "an unregistered name gets the default protocol) [default: peer]")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the spawn command (and the bus brief) without summoning")
    a = ap.parse_args(argv)

    _gate_loop_banner()   # advertise the delegation-phase gate doctrine on every summon

    goal = " ".join(a.goal).strip()
    cwd = a.cwd or os.getcwd()
    scope = _resolve_scope(a.scope, cwd)

    # ── peer mode: persistent named session (sleeps between calls, never dies) ──
    if a.peer_list:
        reg = _peers_load()
        if not reg:
            print("no persistent peers registered (summon --peer <name> \"<goal>\" starts one)")
        for key, e in reg.items():
            print(f"  {key:30s} session={e.get('session_id', '?')[:12]}…  "
                  f"last={e.get('last_used', '?')}  cwd={e.get('cwd', '')}")
        return 0
    peer_key = f"{scope}:{a.peer}" if a.peer else None
    if a.end:
        if not peer_key:
            print("summon: --end needs --peer NAME", file=sys.stderr)
            return 2
        reg = _peers_load()
        if reg.pop(peer_key, None) is None:
            print(f"summon: no peer '{peer_key}' registered", file=sys.stderr)
            return 2
        _peers_save(reg)
        print(f"⚡ peer '{peer_key}' retired.")
        return 0
    if a.peer and a.bus:
        print("summon: --peer and --bus are different doors (a bus watcher is already "
              "persistent) — pick one", file=sys.stderr)
        return 2
    if a.peer and not goal:
        print("summon: --peer needs a goal (one message per call)", file=sys.stderr)
        return 2

    home = _find_home(a.home)
    if home is not None and not (home / "claude_echelon" / "__main__.py").exists():
        print(f"summon: claude-echelon not found at {home} — pip install it, set "
              f"CLAUDE_ECHELON_DIR, or pass --home", file=sys.stderr)
        return 2

    # ── bus mode: compose the role brief; it becomes the opening goal ──
    if a.bus:
        # same privacy guard as `echelon watcher`: the shared default bus is the mixup vector.
        if a.session == "default" and not a.bus_path:
            print("summon: refusing --session default for --bus (same-named roles steal each "
                  "other's mail on the shared bus).\n  fix: `echelon society convene` mints a "
                  "unique session, or pass --session <name> / --bus-path <path>.", file=sys.stderr)
            return 2
        from echelon_sdk import society as soc
        db = soc.session_bus_path(a.session, a.bus_path)
        busctl_cmd = " ".join(
            [sys.executable, "-X", "utf8", "-m", "echelon_engine", "bus"]
            + (["--bus", str(db)] if a.bus_path else ["--session", a.session])
            + ["post", a.bus, a.role])
        goal = soc.compose_brief(a.role, a.bus, busctl_cmd, goal=goal)
        print(f"⚡ summoning '{a.role}' onto channel '{a.bus}' (session {a.session})")
        print(f"   watch: echelon bus --session {a.session} tail {a.bus}")

    if not goal:
        goal = ("The owner summoned you for an interactive chat session. Greet in one line "
                "(state your scope and that the nerve is connected), then wait for their message.")

    cmd = [sys.executable, "-X", "utf8", "-m", "claude_echelon", goal, "--scope", scope]
    # EVERY summon gets a session-file so its SDK session id is captured on close —
    # a --once worker used to die leaving no resumable handle (the trap that lost the
    # audit workers 2026-08-28). Peers key theirs by name (persist in the registry);
    # once/chat runs get a timestamped trace file we read back to print the --resume cmd.
    session_file: Path | None = None
    if a.peer:
        # scriptable single-message run, but the SAME session wakes each call
        reg = _peers_load()
        entry = reg.get(peer_key, {})
        # explicit --resume wins: adopt a specific traced session under this peer name.
        resume_id = a.resume or entry.get("session_id")
        if resume_id:
            cmd += ["--resume", resume_id]
            verb = "adopting" if a.resume else "waking"
            print(f"⚡ {verb} peer '{peer_key}' (session {resume_id[:12]}…)")
        else:
            print(f"⚡ new peer '{peer_key}' — its session will persist across calls")
        session_file = PEERS_REG.parent / "peers" / f".last-{scope}-{a.peer}.json"
    else:
        import time as _time
        session_file = (PEERS_REG.parent / "peers" /
                        f".once-{scope}-{_time.strftime('%Y%m%d-%H%M%S')}.json")
        if a.resume:
            cmd += ["--resume", a.resume]
            print(f"⚡ resuming session {a.resume[:12]}… (context + cache restored)")
        if not a.once:
            cmd.append("--interactive")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    cmd += ["--session-file", str(session_file)]
    if a.model:
        cmd += ["--model", a.model]
    if a.effort:
        cmd += ["--effort", a.effort]
    cmd += ["--cwd", cwd]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]

    if a.dry_run:
        print(f"provider={a.provider}")
        print("would run:", " ".join(cmd if len(goal) < 120 else
                                     [c if c != goal else f"<goal {len(goal)} chars>" for c in cmd]))
        if a.bus:
            print("─" * 60 + "\n" + goal)
        return 0

    # The backend is chosen HERE, explicitly, by --provider. For gemini/deepseek we boot
    # (or reuse) that provider's nerve-translating proxy and hand the child the proxy'd
    # ANTHROPIC_BASE_URL + model env — the SAME substrate claude-deep/claude-gem stand on.
    # For claude/anthropic it's a passthrough (real Anthropic). The proxy is torn down
    # (only if we spawned it) when the peer call returns.
    from echelon_engine.claude_echelon import proxy_session
    try:
        with proxy_session(a.provider) as penv:
            env = dict(penv)                             # proxy base-url + model env baked in
            env["PYTHONUTF8"] = "1"                      # the cp1252 trap, closed at the door
            env.setdefault("ECHELON_SCOPE", scope)
            if home is not None:                         # not pip-installed: run from its repo
                env["PYTHONPATH"] = str(home) + os.pathsep + env.get("PYTHONPATH", "")
            rc = subprocess.run(cmd, env=env).returncode
    except KeyboardInterrupt:
        print("\n⚡ summon closed.")
        return 0
    except RuntimeError as e:
        print(f"summon: {e}", file=sys.stderr)
        return 1
    # Read back the captured session id (written by the child harness on close) and
    # surface a resumable handle for EVERY run — peer or once/chat.
    sid = None
    if session_file and session_file.exists():
        import json
        try:
            sid = json.loads(session_file.read_text(encoding="utf-8")).get("session_id")
        except Exception:
            sid = None
    if a.peer:
        if sid:
            import time
            reg = _peers_load()
            reg[peer_key] = {"session_id": sid, "cwd": cwd, "model": a.model,
                             "last_used": time.strftime("%Y-%m-%d %H:%M:%S")}
            _peers_save(reg)
            print(f"⚡ peer '{peer_key}' sleeping — wake it: echelon summon --peer {a.peer} \"<next>\"")
    elif sid:
        # once / chat: no registry entry, but the id is now traceable — print the exact
        # command to resume THIS session (context + cache intact) instead of losing it.
        model_flag = f" --model {a.model}" if a.model else ""
        print(f"⚡ session {sid} traced ({session_file}).")
        print(f"   resume: echelon summon --provider {a.provider}{model_flag} "
              f"--peer resumed-{sid[:8]} --resume {sid} \"<next>\"")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
