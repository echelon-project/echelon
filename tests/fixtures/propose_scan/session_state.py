"""Session state ledger — structured continuity that survives the context window.

ECHELON's answer to Claude Code's harness task list, but scope-native.
An append-only JSONL file at ~/.echelon/session_state/<scope>.jsonl that
tracks tasks, decisions, files, and goals as they happen during a session.

The HOP reads this ledger on re-entry — it gets structured STATE, not a
narrative summary to parse. Each item carries a `worth` flag: "carry" (bring
to next hop), "archive" (keep for reference), or "drop" (transient).

COMPACT LIFECYCLE (discoverable continuity):
  A compact is a first-class artifact with full provenance (session UUID,
  project path, scope, status). Lifecycle: new (unconsumed) → taken (consumed
  by a session). Any ECHELON substrate session — including plain Claude Code —
  can discover and consume an unconsumed compact on startup. This replaces the
  frozen spine for non-ECHELON harnesses.

  # Write a compact (before session end / window limit):
  ledger.compact(continuity_block, session_uuid="...", project_path="...")

  # On next session start — discover and consume:
  compact = discover_compact("echelon")
  if compact:
      consume_compact("echelon", compact["ts"])
      # inject compact["continuity"] as system context

Usage:
  from echelon_engine.session_state import SessionLedger

  ledger = SessionLedger("echelon")
  ledger.task("build-session-ledger", status="in_progress",
              subject="Build session state ledger module")
  ledger.decision("Session state should be append-only JSONL",
                  context="Avoids DB deps, safe concurrent writes",
                  worth="carry")
  ledger.file("echelon_engine/session_state.py", action="create")
  ledger.goal("Build structured continuity for the HOP")

  # At window limit — produce the continuity block for the next hop:
  block = ledger.continuity_block()
  # The HOP reads this block and knows exactly what's open.
"""

# ── Worth classification ──────────────────────────────────────────────────────
# "carry"   → bring to the next hop (active task, open decision, current goal)
# "archive" → keep for session history but don't load into next context
# "drop"    → transient (a file peek, a completed micro-task) — don't keep

VALID_WORTH = {"carry", "archive", "drop"}


# ── Transcript tail: GROUND TRUTH that travels with the compact ────────────────
# The compact's narrative is a SELF-REPORT — same blind spot as the session it
# summarizes, so threads get silently dropped (the achilles-heel lesson). The
# fix is to carry the raw last-N HUMAN turns verbatim, un-paraphrased, so the
# next session can diff the narrative against what was actually said.

def transcript_tail(transcript_path: str | Path, n: int = 8,
                    max_chars: int = 400) -> list[str]:
    """Return the last n HUMAN-typed turns from a Claude Code .jsonl, verbatim
    (each truncated to max_chars). Empty list if the file is missing/unreadable.
    """
    p = Path(transcript_path)
    if not p.exists():
        return []
    turns: list[str] = []
class SessionLedger:
    """Append-only structured session state for a single scope.

    Thread-safe (append with lock). One file per scope at:
      ~/.echelon/session_state/<scope>.jsonl
    """

    def __init__(self, scope: str = "echelon", session_id: str | None = None):
        self.scope = scope
        self.session_id = session_id or uuid.uuid4().hex[:12]
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._path = _STATE_DIR / f"{scope}.jsonl"
        self._lock = __import__('threading').Lock()

    # ── write helpers ────────────────────────────────────────────────────

    def _append(self, event_type: str, data: dict) -> dict:
        """Append one event to the ledger. Returns the event dict."""
        record = {
            "session_id": self.session_id,
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Best-effort — never break the session for a log write
        return record
    def checkpoint(self, summary: str = "", *,
                   tokens_used: int = 0,
                   open_tasks: list[str] | None = None,
                   worth: str = "carry") -> dict:
        """Record a session checkpoint — used at window limits for the HOP."""
        return self._append("checkpoint", {
            "summary": summary,
            "tokens_used": tokens_used,
            "open_tasks": open_tasks or [],
            "worth": worth if worth in VALID_WORTH else "carry",
        })
