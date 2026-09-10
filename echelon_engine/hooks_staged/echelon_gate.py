#!/usr/bin/env python
"""SessionStart hook — KEEP THE ECHELON GATE FRESH on every wake.

The gate (echelon_engine/atoms/gate.py) is the provider-agnostic banner at the head
of a project's MEMORY.md. This hook re-splices the canon banner on every session start,
so the banner can never drift from the engine.

Engine path is discovered at runtime (import, not hardcoded). Scope is resolved via
the engine's resolve_scope module. Set ECHELON_ENGINE to override the engine path.
"""
import json
import os
import sys


def _discover_agent_root():
    """Discover the engine install directory at runtime."""
    try:
        import echelon_engine
        from pathlib import Path
        return str(Path(echelon_engine.__file__).parent.parent)
    except ImportError:
        fallback = os.environ.get("ECHELON_ENGINE", "")
        if fallback:
            return fallback
        # Last resort: assume sibling to cwd
        return str(Path(os.getcwd()).parent / "ECHELON-AGENT")


def _safe_main():
    raw = sys.stdin.read()
    ev = json.loads(raw) if raw.strip() else {}
    cwd = ev.get("cwd") or os.getcwd() or ""

    agent_root = _discover_agent_root()
    if agent_root not in sys.path:
        sys.path.insert(0, agent_root)

    # ARM THE DELTA TIMER (spec S8 V7): SessionStart is the only reliable clock on
    # Windows — write the armed marker so the UserPromptSubmit tick can fire hourly.
    try:
        from echelon_engine.atoms.delta import arm
        arm()
    except Exception:
        pass

    # Resolve scope through the engine's canonical resolver
    try:
        from echelon_engine.atoms.resolve_scope import resolve_scope
        scope = resolve_scope(cwd)
    except Exception:
        import re
        leaf = os.path.basename(os.path.normpath(cwd or ""))
        scope = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", leaf.lower())).strip("-") or "echelon"

    # Locate MEMORY.md
    import re as _re
    slug = _re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
    candidates = [
        os.path.join(os.path.expanduser("~"), ".claude", "projects", slug, "memory", "MEMORY.md"),
        os.path.join(cwd or "", "memory", "MEMORY.md"),
        os.path.join(cwd or "", "MEMORY.md"),
    ]
    mem = None
    for c in candidates:
        if c and os.path.isfile(c):
            mem = c
            break
    if not mem:
        return  # no MEMORY.md — nothing to refresh

    try:
        from echelon_engine.atoms.gate import write_into
        from pathlib import Path
        rep = write_into(Path(mem), scope)
        note = (f"[echelon-gate] banner refreshed in {os.path.basename(mem)} "
                f"(scope={scope}, body preserved {rep.get('preserved_body_chars', 0)} chars).")
        sys.stdout.write(note.encode("ascii", "replace").decode("ascii") + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _safe_main()
    except Exception:
        pass
    sys.exit(0)
