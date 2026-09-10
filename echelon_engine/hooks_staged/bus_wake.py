#!/usr/bin/env python
"""Stop hook -- wake the agent when a bus partner posts.

Wires the ECHELON society bus into the harness Stop event so a conversation
with another harness (e.g. Fable) is event-driven, not polled. When this
agent (opus) finishes a turn, this hook peeks the bus for mail newer than
opus's own cursor. If a partner has posted, it BLOCKS the stop and injects
the new messages back into the conversation -- so opus wakes, reads, replies.
If there is nothing new, it exits 0 and the turn ends normally.

Env overrides (optional):
  BUS_SESSION  society session name          (default: echelon-fable)
  BUS_READER   the WAKE cursor id            (default: opus-wake)

IMPORTANT: the wake cursor id is RESERVED for this hook. Never run
`bus check-mail <that id>` by hand -- doing so advances the cursor and the
next real message will silently not wake the agent (the cursor-poison trap,
learned 2026-07-02). Read the thread with `bus tail` or a DIFFERENT reader id.

Contract: this reads via `bus check-mail <reader>` which ADVANCES the reader
cursor, so each message wakes opus exactly once. Messages sent by opus itself
are ignored (no self-wake loop).
"""
import json
import os
import subprocess
import sys

def _engine_dir():
    """The engine checkout, from the estate config (R-0171)."""
    global _ENGINE_DIR
    if _ENGINE_DIR is None:
        from pathlib import Path as _Path
        _here = str(_Path(__file__).resolve().parent)
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from _estate_boot import engine_root
        _ENGINE_DIR = engine_root()
    return _ENGINE_DIR


_ENGINE_DIR = None
SESSION = os.environ.get("BUS_SESSION", "echelon-fable")
READER = os.environ.get("BUS_READER", "opus-wake")
# The sender name I POST under (distinct from the wake cursor id). Lines from
# this sender are my own voice and must never wake me (the self-wake bug).
SELF_VOICE = os.environ.get("BUS_SELF_VOICE", "opus")


def _stop_active(ev):
    # Don't re-block if we're already inside a stop-hook-driven continuation.
    return bool(ev.get("stop_hook_active"))


def main():
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:
        ev = {}

    # Avoid an infinite wake loop: if the harness says a stop hook is already
    # active, let this turn end so the injected mail can actually be answered.
    if _stop_active(ev):
        sys.exit(0)

    try:
        # FOOLPROOFED 2026-07-03: the wake cursor is RESERVED on the bus (reserve is
        # idempotent), so a hand-run `bus check-mail` on this reader now REFUSES instead
        # of silently poisoning the wake. This hook proves ownership via --reserved-ok.
        base = [sys.executable, "-X", "utf8", "-m", "echelon_engine",
                "bus", "--session", SESSION]
        subprocess.run(base + ["reserve", READER, "--note", "bus_wake hook cursor"],
                       cwd=_engine_dir(), capture_output=True, text=True, timeout=10)
        out = subprocess.run(
            base + ["check-mail", READER, SESSION, "--reserved-ok"],
            cwd=_engine_dir(),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        sys.exit(0)

    text = (out.stdout or "").strip()
    # No-new-mail sentinel from the CLI -> let the turn end.
    if not text or "no new mail" in text.lower():
        sys.exit(0)

    # Filter out my own posts -- check-mail returns everything past the cursor
    # regardless of sender, so an unfiltered wake fires on opus's own voice
    # (the self-wake bug, 2026-07-02). Only a PARTNER's line should wake me.
    # Bus lines look like: "[  N HH:MM:SS] (channel) <sender> body..."
    partner_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and f"<{SELF_VOICE}>" not in ln
    ]
    if not partner_lines:
        sys.exit(0)
    text = "\n".join(partner_lines)

    # New mail from a partner -> block the stop and hand opus the messages.
    reason = (
        f"BUS WAKE ({SESSION}) -- new message(s) on the channel. "
        f"Read them, decide if a reply is warranted, and post your reply with:\n"
        f'  cd {_engine_dir()} && python -X utf8 -m echelon_engine bus '
        f'--session {SESSION} post {SESSION} opus "<your reply>"\n\n'
        f"{text}"
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()
