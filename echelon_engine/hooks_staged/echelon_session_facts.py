"""echelon_session_facts.py — the SESSION FACTS ledger (PostToolUse). Cheap, additive, spinal.

THE PROBLEM (owner, 2026-08-15): reflexes fire on a REGEX OVER THE COMMAND STRING, which is
the only thing the PreToolUse hook can see. A regex cannot know whether the danger CONDITION
is actually present, so guards misfire on commands that merely look dangerous. Measured on one
session: 11 of 13 fires spurious. `claude-deep-checks-out-a-branch` (match: `git push`) fired
5 times without a single claude-deep worker ever having been dispatched — the guard's whole
premise was absent, and the regex had no way to know.

THE FIX (owner's idea): keep a small JSON of facts about the ACTIVE SESSION, so a reflex can
gate on "is the condition present?" as well as "does the string match?". This hook writes it;
echelon_reflex.py reads it and evaluates an atom's optional `reflex-when:` predicate.

WHAT IT RECORDS — only facts that are (a) cheap to derive from the tool event itself and
(b) actually gate a real guard. This is deliberately NOT a transcript:
  workers_dispatched  — claude-deep / claude-gem / summon / swarm invocations seen
  merges_done         — gh pr merge / git merge seen
  worktrees_added     — git worktree add seen
  branches_checked_out— git checkout -b / switch -c seen
  db_copies           — cp/copy of a .db seen
  commands_run        — total Bash calls (a cheap "has anything happened yet" signal)
  files_read          — Read/Grep/Glob calls
  cwd                 — the session's last-seen working directory
  scope               — the resolved estate (written lazily by the reflex hook, not here)

DESIGN LAWS (both learned the hard way in this estate):
  1. SEVERABLE — any failure exits 0 and writes nothing. A facts file is an OPTIMISATION;
     losing it must never block a tool call or suppress a guard.
  2. FAIL OPEN — a MISSING or unreadable facts file means the consumer fires the guard
     anyway. A spurious warning is recoverable; a guard that silently vanishes when a file
     is absent is the exact failure mode reflexes exist to prevent.

Kill switch: ECHELON_SESSION_FACTS=off.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

FACTS_DIR = os.path.expanduser("~/.echelon/session_facts")
MAX_AGE = 60 * 60 * 12   # a facts file older than this is a stale session; ignore it

# ── SESSION IDENTITY (2026-08-17) ────────────────────────────────────────────────────────
# `session_id` is ECHELON-MINTED and primary: harness-independent, so a Codex / bare-CLI
# session still has a spine. The harness's own id is carried ALONGSIDE as
# `<harness>_session_id` so a transcript can still be correlated. Minted once, on the
# session's first tool call, and stable thereafter (the facts file is the anchor).
ECHELON_SID_LEN = 16


def _mint_echelon_sid() -> str:
    import secrets
    return secrets.token_hex(ECHELON_SID_LEN // 2)


def _harness_name(data: dict) -> str:
    """Which harness produced this event. Defaults to 'claude' (this hook's host)."""
    return str(os.environ.get("ECHELON_HARNESS") or "claude").strip().lower() or "claude"


# ── OUTWARD ACTS (the stakes-meter override; see 0GAP/STAKES_METER_DECISION.md) ──────────
# Council verdict 2026-08-17: irreversibility is a VETO, not a vote — any outward act sets
# LOAD-BEARING regardless of every other signal. Council guard directive, honored here:
# start NARROW and high-confidence. Not every `ssh` is a deploy; a local install is not a
# publish. Over-triggering destroys the meter's credibility faster than under-triggering.
# Widen this only from observed false NEGATIVES, never from a hunch.
_OUTWARD = (
    ("deploys",   re.compile(r"\b(?:scp|rsync)\b[^|;&]*\s\S+:", re.I)),          # copy TO a remote host
    ("deploys",   re.compile(r"\bssh\b[^|;&]*\b(?:systemctl|service)\s+(?:restart|start|stop)\b", re.I)),
    ("restarts",  re.compile(r"\b(?:systemctl|service)\s+(?:restart|start|stop)\b", re.I)),
    ("publishes", re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|"
                             r"\bpip\s+install\b[^|;&]*\.whl\b|\bdocker\s+push\b|\bgh\s+release\s+create\b", re.I)),
    ("pushes",    re.compile(r"\bgit\s+push\b", re.I)),
)

# command-shape -> fact key. Kept tiny and literal: this runs on EVERY tool call.
_SIGNALS = (
    ("workers_dispatched", re.compile(r"\b(claude-deep|claude-gem|echelon\s+summon|echelon\s+swarm|swarm_deep)\b")),
    ("merges_done",        re.compile(r"\b(gh pr merge|git merge)\b")),
    ("worktrees_added",    re.compile(r"\bgit worktree add\b")),
    ("branches_created",   re.compile(r"\bgit (checkout -b|switch -c)\b")),
    ("db_copies",          re.compile(r"\b(cp|copy)\b[^|;&]*\.db\b")),
    ("pushes",             re.compile(r"\bgit push\b")),
    ("proposals_checked",  re.compile(r"\bpropose\s+check\b")),  # 2d: a checked proposal exists -> the touches-fence reflex may speak
)


def _path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:64]
    return os.path.join(FACTS_DIR, f"{safe}.json")


def load_facts(session_id: str) -> dict | None:
    """Read a session's facts. Returns None when absent, stale, or unreadable — the caller
    MUST treat None as 'unknown' and fail open."""
    try:
        p = _path(session_id)
        if not os.path.exists(p):
            return None
        if time.time() - os.path.getmtime(p) > MAX_AGE:
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def main() -> int:
    if os.environ.get("ECHELON_SESSION_FACTS", "").lower() in ("off", "0", "false"):
        return 0
    try:
        raw = sys.stdin.read().lstrip("﻿")
        data = json.loads(raw or "{}")
        sid = str(data.get("session_id", "") or "unknown")
        tool = data.get("tool_name", "")
        tin = data.get("tool_input", {}) or {}
        payload = json.dumps(tin, ensure_ascii=False)
        cwd = str(data.get("cwd", "")).replace("\\", "/")

        os.makedirs(FACTS_DIR, exist_ok=True)
        facts = load_facts(sid) or {"session_id": sid, "started": int(time.time())}
        facts["cwd"] = cwd or facts.get("cwd", "")
        facts["updated"] = int(time.time())

        # Session identity: mint ECHELON's own id ONCE, keep the harness's alongside.
        # `session_id` above is the FILE KEY (the harness's, since that is what keys the
        # event); `echelon_session_id` is the durable id that goes in the commit trailer.
        if not facts.get("echelon_session_id"):
            facts["echelon_session_id"] = _mint_echelon_sid()
        hname = _harness_name(data)
        facts["harness"] = hname
        facts[f"{hname}_session_id"] = sid

        if tool in ("Bash", "PowerShell"):
            facts["commands_run"] = int(facts.get("commands_run", 0)) + 1
            cmd = str(tin.get("command", ""))
            for key, rx in _SIGNALS:
                if rx.search(cmd):
                    facts[key] = int(facts.get(key, 0)) + 1
            # outward acts — the LOAD-BEARING override. Counted per class so the meter can
            # report WHICH irreversible thing happened, not just that something did.
            for key, rx in _OUTWARD:
                if rx.search(cmd):
                    facts[key] = int(facts.get(key, 0)) + 1
                    facts["outward_acts"] = int(facts.get("outward_acts", 0)) + 1
        elif tool in ("Read", "Grep", "Glob"):
            facts["files_read"] = int(facts.get("files_read", 0)) + 1
        elif tool in ("Edit", "Write", "NotebookEdit"):
            facts["files_written"] = int(facts.get("files_written", 0)) + 1
        elif tool == "Agent":
            # a native subagent IS a dispatched worker for guard purposes
            facts["workers_dispatched"] = int(facts.get("workers_dispatched", 0)) + 1

        tmp = _path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False)
        os.replace(tmp, _path(sid))   # atomic: a reader never sees a half-written file
        return 0
    except Exception:
        return 0   # severed: facts are an optimisation, never a blocker


if __name__ == "__main__":
    raise SystemExit(main())
