"""echelon_context_budget.py — the CONTEXT BUDGET guard (UserPromptSubmit).

THE ASK (owner, 2026-08-15): "can we make it to a reflex, the value at 400000, and it will
suggest a /wrap because almost hit a red line quota."

WHY THIS IS A HOOK AND NOT A `reflex: true` ATOM — the shape genuinely differs:
  - A reflex fires PreToolUse and matches a REGEX OVER THE COMMAND. Context size is nowhere in
    a tool payload, so no pattern can express this trigger. `reflex-when` (2026-08-15) added
    session FACTS, which is the right family — but a reflex still needs a matching COMMAND to
    hang off, and "the session got big" is not a command.
  - Firing per TOOL CALL would warn dozens of times per turn. A budget warning is a
    once-per-turn judgment, which is exactly UserPromptSubmit.
So this is a sibling organ to the reflex arc, not a rule inside it: same severability law, same
fail-quiet discipline, different event. The context number IS exported into session facts, so a
regular reflex CAN gate on it via `reflex-when: context_tokens > 400000` when a specific
dangerous command should be blocked late in a session.

THE MEASUREMENT — real, not estimated. Claude Code writes each assistant turn's `usage` block
into the session transcript (`~/.claude/projects/<slug>/<session-id>.jsonl`). Live context is
    input_tokens + cache_read_input_tokens + cache_creation_input_tokens
from the LAST usage row. That is the true number the API billed, not a file-size proxy — a
transcript is much larger on disk than the context it represents (JSON overhead, tool results
that were later dropped), so sizing off bytes would fire at the wrong time in both directions.

THRESHOLDS (owner-set): WARN at 400_000. The owner runs `opus[1m]`, so this is not "about to
truncate" — it is the point where a session has done enough that /wrap + /clear is cheaper than
carrying it further. Escalates at 700_000 (RED). Idempotent per band: it warns ONCE per band per
session, because a budget warning repeated every turn is noise that trains you to ignore it
([[a-sweep-that-cries-wolf-gets-ignored]]).

SEVERABLE: any failure exits 0 silently. Kill switch: ECHELON_CONTEXT_BUDGET=off.
"""
from __future__ import annotations

import json
import os
import sys

WARN_AT = int(os.environ.get("ECHELON_CTX_WARN", "400000"))
RED_AT = int(os.environ.get("ECHELON_CTX_RED", "700000"))
FACTS_DIR = os.path.expanduser("~/.echelon/session_facts")


def context_tokens(transcript_path: str) -> int:
    """Live context size from the LAST usage row in the transcript. 0 when unknown."""
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    last = None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                u = (rec.get("message") or {}).get("usage")
                if isinstance(u, dict):
                    last = u
    except Exception:
        return 0
    if not last:
        return 0
    return (int(last.get("input_tokens", 0) or 0)
            + int(last.get("cache_read_input_tokens", 0) or 0)
            + int(last.get("cache_creation_input_tokens", 0) or 0))


def _band(n: int) -> str:
    return "red" if n >= RED_AT else "warn" if n >= WARN_AT else ""


def _publish(session_id: str, tokens: int, band: str) -> str:
    """Write context_tokens into the session facts (so `reflex-when` can gate on it) and
    return the band already warned for, so each band warns exactly once."""
    prev = ""
    try:
        os.makedirs(FACTS_DIR, exist_ok=True)
        import re
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:64]
        p = os.path.join(FACTS_DIR, f"{safe}.json")
        facts = {}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    facts = json.load(f) or {}
            except Exception:
                facts = {}
        prev = str(facts.get("ctx_band_warned", "") or "")
        facts["context_tokens"] = tokens
        if band and band != prev:
            facts["ctx_band_warned"] = band
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass
    return prev


def main() -> int:
    if os.environ.get("ECHELON_CONTEXT_BUDGET", "").lower() in ("off", "0", "false"):
        return 0
    try:
        data = json.loads(sys.stdin.read().lstrip("﻿") or "{}")
        tokens = context_tokens(str(data.get("transcript_path", "") or ""))
        if not tokens:
            return 0
        band = _band(tokens)
        already = _publish(str(data.get("session_id", "") or "unknown"), tokens, band)
        if not band or band == already:
            return 0   # under budget, or this band already warned once

        k = f"{tokens // 1000}k"
        if band == "red":
            msg = (f"[echelon-context-budget] 🔴 RED LINE — this session is carrying {k} of "
                   f"context (red at {RED_AT // 1000}k). Every further turn re-sends all of it. "
                   f"STOP ADDING WORK: run `/wrap` now to bank what this session learned, then "
                   f"`/clear`. If the current task is genuinely unfinished, `/compact` it first "
                   f"— but /compact is itself a large request, so /wrap + /clear is cheaper "
                   f"whenever the next task is a different job.")
        else:
            msg = (f"[echelon-context-budget] ⚠ {k} of context carried (warn at "
                   f"{WARN_AT // 1000}k). Long sessions cost on every turn even when cached — "
                   f"84% of this owner's measured spend was above 150k. If the work in this "
                   f"window is DONE or you are switching jobs: `/wrap` then `/clear` (clearing "
                   f"is free; the bank already holds what matters across sessions). If you are "
                   f"mid-task, keep going — this fires once per band, not every turn.")

        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }}))
        return 0
    except Exception:
        return 0   # severed: a budget hint must never block the owner's turn


if __name__ == "__main__":
    raise SystemExit(main())
