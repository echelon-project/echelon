#!/usr/bin/env python
"""SessionStop hook — checkpoint the live workroom, replacing the STUB.

The stub this replaces (hooks_cmd.STOP_HOOK_SOURCE) only logged session end. This hook
reads the hook payload from stdin (JSON, empty tolerated), resolves the workroom via the
sibling `echelon_engine.workcycle` module (`room_path(cwd)`), and writes a checkpoint
(`checkpoint(room, auto=True, scope resolved by checkpoint)` — auto never raises).

SAFETY: a session stop must never hang the harness. The whole body runs in a worker
thread time-boxed to 10s wall; failures report to stderr; exit is ALWAYS 0.

WHY 10s, NOT 2s (live smoke 2026-08-27): `import echelon_engine` runs the architecture
scanner (validate_chains) as a boot gate — ~3s cold. A 2s box abandoned the daemon
thread before the checkpoint ever wrote, so the live Stop hook silently never
checkpointed while the unit tests (fake engine, no scanner) stayed green.
"""
import json
import os
import sys
import threading

def _fallback_agent() -> str:
    """R-0171: the fallback root, from the estate config, never a literal."""
    from pathlib import Path
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from _estate_boot import engine_root
    return engine_root()


_FALLBACK_AGENT = None      # resolved on first use by _discover_agent_root


def _discover_agent_root() -> str:
    try:
        import echelon_engine
        from pathlib import Path
        return str(Path(echelon_engine.__file__).parent.parent)
    except ImportError:
        global _FALLBACK_AGENT
        if _FALLBACK_AGENT is None:
            _FALLBACK_AGENT = _fallback_agent()
        return _FALLBACK_AGENT


def _safe_main() -> None:
    raw = sys.stdin.read().lstrip("\ufeff")   # PowerShell pipes prepend a BOM; shrug it off
    ev = json.loads(raw) if raw.strip() else {}
    cwd = ev.get("cwd") or os.getcwd() or ""

    agent_root = _discover_agent_root()
    if agent_root not in sys.path:
        sys.path.insert(0, agent_root)

    from echelon_engine import workcycle  # guarded: sibling module may not exist yet

    room = workcycle.room_path(cwd or None)
    if not room:
        return  # no workroom — nothing to checkpoint
    # checkpoint resolves the declared room scope before consulting the bank.
    result = workcycle.checkpoint(room, auto=True)
    if isinstance(result, dict) and result.get("ok") is False:
        print("echelon stop: checkpoint unavailable; room state may be stale", file=sys.stderr)

    # DISARM THE DELTA TIMER (spec S8 V7): remove the armed marker AND flush one
    # final delta. disarm reports scheduling errors to stderr; scheduling alone
    # does not prove the final archive was written.
    try:
        from echelon_engine.atoms.delta import disarm
        disarm()
    except Exception as exc:
        print(f"echelon stop: final delta unavailable ({type(exc).__name__})", file=sys.stderr)


if __name__ == "__main__":
    try:
        def _run() -> None:
            try:
                _safe_main()
            except Exception as exc:
                print(f"echelon stop: checkpoint interrupted ({type(exc).__name__})", file=sys.stderr)
        _t = threading.Thread(target=_run, daemon=True)
        _t.start()
        _t.join(10.0)  # ≤10s wall (engine import ~3s); a pathological room must never hang session stop
        if _t.is_alive():
            print("echelon stop: checkpoint timed out; completion unconfirmed", file=sys.stderr)
    except Exception as exc:
        print(f"echelon stop: hook unavailable ({type(exc).__name__})", file=sys.stderr)
    sys.exit(0)
