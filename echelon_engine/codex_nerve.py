"""ECHELON's Codex-harness nerve: a small, severable substrate adapter.

The native Codex hooks and the Claude-to-Codex bridge both call this module.
It only reads the local bank and emits compact context; a failed bank or hook
never interrupts the model turn.  Tool denial stays in the harness hook layer.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MIN_PROMPT = 20
_SKIP = re.compile(r"^\s*(/|hi\b|hello\b|hey\b|ok\b|yes\b|no\b|thanks|next\b|go\b)", re.I)
_TASK = re.compile(r"\b(build|fix|implement|refactor|migrate|audit|review|deploy|create|design|write|"
                   r"generate|scan|port|rewrite|add|remove|update|integrate|debug|research|test|verify)\b", re.I)

_DISCIPLINE = (
    "ECHELON DISCIPLINE: prove, do not claim; isolate one layer at a time; reuse existing "
    "estate organs; bound loops; report failures plainly; finish the loop with verification."
)


def _scope(cwd: str) -> str:
    """The nerve is SEVERABLE — a scope it cannot resolve must degrade, never raise into the
    turn. resolve_scope fails closed on an unknown dir (OPEN-0036); here that means "no banked
    scope owns this cwd", so the nerve reads against the leaf kebab and stays quiet."""
    from echelon_engine.atoms.resolve_scope import resolve_scope, UnknownScopeError
    try:
        return resolve_scope(cwd or os.getcwd())
    except UnknownScopeError as e:
        return e.candidate


def _room_brief(cwd: str, timeout: float = 3.0) -> str:
    """The workroom resume brief (W3 doors): `workcycle.resume_brief(room_path(cwd))`, VERBATIM —
    emitted before the bank warmth block so the room leads the turn. Time-boxed in a worker
    thread (same never-interrupt law as the bank probe): a pathological room must never delay
    the turn. '' on absent module / no room / any failure — the nerve stays severable."""
    try:
        from echelon_engine import workcycle  # guarded: sibling-owned module
    except Exception:
        return ""
    try:
        room = workcycle.room_path(cwd or None)
        if not room:
            return ""
        import threading
        out: list[str] = []
        def _run() -> None:
            try:
                out.append(workcycle.resume_brief(room) or "")
            except Exception:
                pass  # severable: a broken room never leaks into the turn
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        return out[0].rstrip("\n") if out else ""
    except Exception:
        return ""


def primer(prompt: str, cwd: str = "") -> tuple[str, dict[str, Any]]:
    """Return a compact per-turn warmth primer and safe route telemetry."""
    if os.environ.get("ECHELON_NERVE", "").lower() in {"off", "0", "false"}:
        return "", {"enabled": False}
    prompt = (prompt or "").strip()
    if len(prompt) < _MIN_PROMPT or _SKIP.match(prompt):
        return "", {"enabled": True, "skipped": True}
    try:
        from echelon_engine.atoms.store import SeedStore
        from echelon_engine.atoms.warmth import warmth
        from echelon_sdk.scopegraph import ScopeGraph

        scope = _scope(cwd)
        room_brief = _room_brief(cwd)
        reading = warmth(prompt[:400], SeedStore(), scope=scope, scope_graph=ScopeGraph(),
                          pre_filter=prompt[:400])
        lines = [room_brief] if room_brief else []
        lines.append(f"[ECHELON NERVE] scope={scope}; warmth={reading.verdict.upper()} "
                     f"score={reading.score}. {reading.guidance}")
        for hit in reading.warmest[:3]:
            seed = hit.seed
            coord = (getattr(seed, "coordinate", "") or getattr(seed, "id", "")).rsplit(":", 1)[-1]
            lines.append(f"- {coord}: {(seed.content or '')[:180]}")
        lines.append("WARM: use the surfaced move before searching fresh. LUKEWARM: inspect the "
                     "warmest move. COLD: think fresh, then crystallize what pays off.")
        if _TASK.search(prompt):
            lines.append("For load-bearing work: recall before acting; equip a relevant cartridge; "
                         "delegate labor and gate from an independent context.")
        lines.append(_DISCIPLINE)
        return "\n".join(lines), {"enabled": True, "scope": scope, "verdict": reading.verdict,
                                    "score": reading.score, "matches": len(reading.warmest)}
    except Exception as exc:  # severability: an unavailable bank never blocks Codex
        return "", {"enabled": True, "severed": type(exc).__name__}


def context_for(prompt: str, cwd: str = "") -> tuple[str, dict[str, Any]]:
    """Compose static discipline with the optional dynamic warmth primer."""
    dynamic, telemetry = primer(prompt, cwd)
    return dynamic or _DISCIPLINE, telemetry


def _event_payload() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _log_hook(telemetry: dict[str, Any]) -> None:
    """Write non-sensitive hook telemetry; never write the prompt or bank text."""
    try:
        path = Path.home() / ".echelon" / "hooks" / "codex_nerve.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": "userPromptSubmit", **telemetry}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass


def hook_main() -> int:
    """Codex ``UserPromptSubmit`` command-hook entry point.

    Codex command hooks render stdout as turn feedback/context. JSON is used
    only for its conventional envelope; failures intentionally remain silent.
    """
    try:
        event = _event_payload()
        prompt = event.get("prompt") or event.get("user_prompt") or event.get("text") or ""
        context, telemetry = context_for(str(prompt), str(event.get("cwd") or os.getcwd()))
        _log_hook(telemetry)
        if context:
            # Codex >= 0.152 rejects a bare additionalContext ("invalid user prompt submit JSON output",
            # owner 2026-09-03): the envelope is hookSpecificOutput/hookEventName, same as Claude Code.
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": context}}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
