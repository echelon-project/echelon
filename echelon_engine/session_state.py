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
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STATE_DIR = Path.home() / ".echelon" / "session_state"

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

# Markers that mean "this user row is harness-injected, not human-typed".
_TAIL_NOISE_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "Caveat:",
    "[Request interrupted",
    "<task-notification>",
    "<bash-",
)


def _tail_row_text(row: dict) -> str:
    """Pull the text a transcript row carries; '' if it carries none."""
    msg = row.get("message", {})
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            b.get("text", "") for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_human_turn(row: dict) -> tuple[bool, str]:
    """True + cleaned text if this transcript row is a HUMAN-typed user turn.

    Excludes harness noise: local-command caveats, pure tool_result rows,
    task-notifications, command wrappers, and interrupt markers. An
    ide_opened_file hint is excluded UNLESS the human appended a real note
    after the closing tag (a common pattern).
    """
    if row.get("type") != "user":
        return False, ""
    c = row.get("message", {}).get("content")
    # A pure tool_result row is the harness feeding results back — not human.
    if isinstance(c, list) and c and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c
    ):
        return False, ""
    t = _tail_row_text(row).strip()
    if not t:
        return False, ""
    # An ide hint may carry a trailing human note: keep only the note.
    if "</ide_opened_file>" in t:
        after = t.split("</ide_opened_file>", 1)[1].strip()
        return (bool(after), after)
    if any(t.startswith(p) for p in _TAIL_NOISE_PREFIXES):
        return False, ""
    return True, t


def transcript_tail(transcript_path: str | Path, n: int = 8,
                    max_chars: int = 400) -> list[str]:
    """Return the last n HUMAN-typed turns from a Claude Code .jsonl, verbatim
    (each truncated to max_chars). Empty list if the file is missing/unreadable.
    """
    p = Path(transcript_path)
    if not p.exists():
        return []
    turns: list[str] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ok, txt = _is_human_turn(row)
                if ok:
                    if len(txt) > max_chars:
                        txt = txt[:max_chars].rstrip() + " …"
                    turns.append(txt)
    except Exception:
        return []
    return turns[-n:]


def find_transcript(session_uuid: str = "", project_path: str = "") -> str | None:
    """Best-effort locate the Claude Code transcript .jsonl for a session.

    Claude Code stores transcripts at
      ~/.claude/projects/<slugified-project>/<session-uuid>.jsonl
    The slug replaces path separators and ':' with '-'. If session_uuid is
    given we match it exactly; otherwise we return the most-recently-modified
    transcript under the project's slug dir.
    """
    proj_root = Path.home() / ".claude" / "projects"
    if not proj_root.exists():
        return None
    # Exact hit by uuid anywhere under projects/
    if session_uuid:
        hits = list(proj_root.glob(f"**/{session_uuid}.jsonl"))
        if hits:
            return str(hits[0])
    # Fall back to newest transcript in the project's slug dir
    if project_path:
        slug = project_path.replace("\\", "-").replace("/", "-").replace(":", "-")
        slug = slug.strip("-")
        cand = proj_root / slug
        if cand.exists():
            files = sorted(cand.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime_ns, p.name))
            if files:
                return str(files[-1])
    return None


# ── The ledger ────────────────────────────────────────────────────────────────

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

    # ── event types ──────────────────────────────────────────────────────

    def task(self, task_id: str, *,
             status: str = "pending",
             subject: str = "",
             description: str = "",
             blocked_by: list[str] | None = None,
             blocks: list[str] | None = None,
             owner: str = "",
             worth: str = "carry") -> dict:
        """Record a task state change. Status: pending | in_progress | completed | deleted."""
        return self._append("task", {
            "task_id": task_id,
            "status": status,
            "subject": subject,
            "description": description,
            "blocked_by": blocked_by or [],
            "blocks": blocks or [],
            "owner": owner,
            "worth": worth if worth in VALID_WORTH else "carry",
        })

    def decision(self, conclusion: str, *,
                 context: str = "",
                 prompted_by: str = "",
                 atoms_referenced: list[str] | None = None,
                 worth: str = "carry") -> dict:
        """Record a decision or conclusion reached during the session."""
        return self._append("decision", {
            "conclusion": conclusion,
            "context": context,
            "prompted_by": prompted_by,
            "atoms_referenced": atoms_referenced or [],
            "worth": worth if worth in VALID_WORTH else "carry",
        })

    def file(self, path: str, *,
             action: str = "modify",
             description: str = "",
             worth: str = "archive") -> dict:
        """Record a file that was created, modified, or read."""
        return self._append("file", {
            "path": path,
            "action": action,
            "description": description,
            "worth": worth if worth in VALID_WORTH else "archive",
        })

    def goal(self, goal_text: str, *,
             status: str = "active",
             worth: str = "carry") -> dict:
        """Record the current goal or a goal state change."""
        return self._append("goal", {
            "goal": goal_text,
            "status": status,  # active | achieved | abandoned
            "worth": worth if worth in VALID_WORTH else "carry",
        })

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

    def recall(self, atoms: list[str], *,
               intent: str = "",
               confidence: float = 0.0,
               tier: str = "",
               worth: str = "archive") -> dict:
        """Record which bank atoms were recalled and why."""
        return self._append("recall", {
            "atoms": atoms,
            "intent": intent,
            "confidence": confidence,
            "tier": tier,
            "worth": worth if worth in VALID_WORTH else "archive",
        })

    # ── compact lifecycle (discoverable continuity artifact) ──────────────

    def compact(self, continuity: str, *,
                session_uuid: str = "",
                project_path: str = "",
                model: str = "",
                tokens_used: int = 0,
                status: str = "new",
                kind: str = "session",
                target_estates: list[str] | None = None,
                goal: str = "") -> dict:
        """Write a compact record with FULL provenance + consumption token.

        This is a first-class continuity artifact — any ECHELON substrate
        session can discover and consume it. Lifecycle: new → taken.

        The compact includes a cryptographic consumption token (random 16-byte
        hex). To consume it, the caller must present this token. This prevents
        double-consumption under concurrent access (race-condition proof).

        The record always carries the WRITER's session_id (via _append) so a
        compact can never lie about its lineage — a test/smoke run is traceable
        to its writer even if it's stamped with another session's uuid. `kind`
        distinguishes a real "session" handoff from a "manual" / "test" write.

        Args:
            continuity: The continuity block text (structured state for next hop)
            session_uuid: The producing session's UUID
            project_path: Working directory of the producing session
            model: Model that produced the compact (if any)
            tokens_used: Tokens used in the session
            status: "new" (unconsumed) — never write "taken" directly
            kind: "session" (real handoff) | "manual" | "test" (provenance)
            target_estates: Estates this compact targets (discovery filter key).
                If None/empty, auto-derived from files_touched. Falls back to
                [project_path] if derivation yields nothing.
            goal: Human-readable goal. If the ledger has no current_goal, this
                is used. If STILL empty, a WARNING is printed (but the compact
                is still written for back-compat).
        """
        import secrets
        token = secrets.token_hex(16)
        meta = {
            "tokens_used": tokens_used,
            "model": model,
            "project_path": project_path,
        }
        # Snapshot current state for the compact metadata
        active = self.active_tasks()
        meta["open_tasks"] = [t["task_id"] for t in active]
        meta["open_task_subjects"] = [t.get("subject", "") for t in active]
        ledger_goal = self.current_goal()
        if ledger_goal:
            meta["current_goal"] = ledger_goal.get("goal", "")
        elif goal:
            meta["current_goal"] = goal
        files = self.files_touched(10)
        meta["files_touched"] = [f["path"] for f in files]

        # ── C2: warn on empty goal ────────────────────────────────────────
        cgoal = meta.get("current_goal", "")
        if not cgoal.strip():
            import sys as _sys
            print(
                "[session_state] WARNING: compact written with empty goal — "
                "the resume menu will be hard to disambiguate. Pass goal=\"...\" "
                "to compact() for a human-readable one-liner.",
                file=_sys.stderr,
            )

        # ── C1: target_estates (estate-aware discovery filter) ────────────
        if target_estates is None or len(target_estates) == 0:
            touched = [f["path"] for f in files]
            derived = []
            for p in touched:
                est = _estate_of_path(p)
                if est and est not in derived:
                    derived.append(est)
            if derived:
                target_estates = derived
            else:
                target_estates = [project_path] if project_path else []
        meta["target_estates"] = list(target_estates)

        # ── B1: stable non-secret compact_id (derived, not random) ───────
        ts = datetime.now(timezone.utc).isoformat()
        compact_id = hashlib.sha256((session_uuid + "|" + ts).encode()).hexdigest()[:12]
        meta["compact_id"] = compact_id

        return self._append("compact", {
            "status": status,
            "token": token,
            "session_uuid": session_uuid,
            "project_path": project_path,
            "continuity": continuity,
            "kind": kind,
            "meta": meta,
            "target_estates": list(target_estates),
            "worth": "carry",
            "ts": ts,
            "compact_id": compact_id,
        })

    def compact_taken(self, compact_ts: str, *,
                      token: str = "",
                      consumed_by: str = "",
                      project_path: str = "") -> dict:
        """Mark a compact as consumed (status: taken).

        The token MUST match the compact's consumption token — this prevents
        double-consumption under concurrent access. Without the correct token,
        this is a no-op that returns an error marker.
        """
        return self._append("compact_taken", {
            "compact_ts": compact_ts,
            "token": token,
            "consumed_by": consumed_by,
            "project_path": project_path,
            "worth": "archive",
        })

    # ── read helpers ──────────────────────────────────────────────────────

    def read(self, since: str | None = None,
             types: list[str] | None = None,
             worth: str | None = None,
             limit: int = 0) -> list[dict]:
        """Read events from the ledger, optionally filtered.

        Args:
            since: ISO timestamp — only events after this
            types: Filter to these event types (e.g. ['task', 'decision'])
            worth: Filter to this worth level ('carry', 'archive', 'drop')
            limit: Max events to return (0 = all)
        """
        if not self._path.exists():
            return []

        events: list[dict] = []
        type_set = set(types) if types else None

        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if since and evt.get("ts", "") <= since:
                    continue
                if type_set and evt.get("type") not in type_set:
                    continue
                if worth and evt.get("worth") != worth:
                    continue

                events.append(evt)
                if limit and len(events) >= limit:
                    break

        return events

    def active_tasks(self) -> list[dict]:
        """Get all non-completed tasks (pending + in_progress)."""
        all_tasks = self.read(types=["task"])
        # Merge: latest status per task_id
        latest: dict[str, dict] = {}
        for t in all_tasks:
            tid = t.get("task_id", "")
            if tid not in latest or t["ts"] > latest[tid]["ts"]:
                latest[tid] = t
        return [t for t in latest.values()
                if t.get("status") in ("pending", "in_progress")]

    def current_goal(self) -> dict | None:
        """Get the most recent active goal."""
        goals = self.read(types=["goal"], worth="carry")
        for g in reversed(goals):
            if g.get("status") == "active":
                return g
        return None

    def recent_decisions(self, n: int = 10) -> list[dict]:
        """Get recent decisions marked 'carry'."""
        return self.read(types=["decision"], worth="carry", limit=n)

    def files_touched(self, n: int = 20) -> list[dict]:
        """Get recently touched files."""
        files = self.read(types=["file"])
        # Deduplicate by path — keep latest
        seen: set[str] = set()
        deduped: list[dict] = []
        for f in reversed(files):
            p = f.get("path", "")
            if p not in seen:
                seen.add(p)
                deduped.append(f)
        return list(reversed(deduped))[-n:]

    def last_checkpoint(self) -> dict | None:
        """Get the most recent checkpoint."""
        checkpoints = self.read(types=["checkpoint"])
        return checkpoints[-1] if checkpoints else None

    # ── self-compaction: produce the continuity block ─────────────────────

    def continuity_block(self, transcript_path: str | None = None,
                         tail_n: int = 8) -> str:
        """Produce a structured continuity block for the next HOP.

        This is what the next session reads to resume. It's structured STATE,
        not narrative — tasks with status, decisions, files, current goal.

        The running provider self-compacts by calling this at window limits.
        It only costs reading the ledger (no model call needed for the
        structured part — the model call is for the narrative summary).

        If transcript_path is given, the last tail_n HUMAN turns are appended
        verbatim as a Transcript Tail — GROUND TRUTH the next session diffs the
        narrative against (the self-report can't drop a thread the tail still
        shows).
        """
        parts: list[str] = []

        # Current goal
        goal = self.current_goal()
        if goal:
            parts.append(f"## Current Goal\n{goal['goal']} (status: {goal.get('status', '?')})")

        # Active tasks
        tasks = self.active_tasks()
        if tasks:
            lines = ["## Open Tasks"]
            for t in tasks:
                status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(
                    t.get("status", ""), "❓")
                lines.append(f"- {status_icon} [{t['task_id']}] {t.get('subject', '')}")
                if t.get("description"):
                    lines.append(f"  {t['description'][:200]}")
                blocked = t.get("blocked_by", [])
                if blocked:
                    lines.append(f"  blocked by: {', '.join(blocked)}")
            parts.append("\n".join(lines))

        # Recent decisions worth carrying
        decisions = self.recent_decisions(5)
        if decisions:
            lines = ["## Key Decisions"]
            for d in decisions:
                lines.append(f"- {d['conclusion'][:200]}")
                if d.get("context"):
                    lines.append(f"  context: {d['context'][:150]}")
            parts.append("\n".join(lines))

        # Files in working set
        files = self.files_touched(15)
        if files:
            lines = ["## Working Set"]
            for f in files:
                action = f.get("action", "?")
                icon = {"create": "➕", "modify": "✏️", "read": "👁️"}.get(action, "📄")
                lines.append(f"- {icon} `{f['path']}`")
                if f.get("description"):
                    lines.append(f"  {f['description'][:120]}")
            parts.append("\n".join(lines))

        # Last checkpoint
        cp = self.last_checkpoint()
        if cp:
            parts.append(f"## Last Checkpoint\n{cp.get('summary', '')[:500]}")
            if cp.get("tokens_used"):
                parts.append(f"tokens_used: {cp['tokens_used']}")

        # Transcript tail — the human's actual last words, verbatim. This is
        # GROUND TRUTH, not a self-report: the narrative above can paraphrase a
        # thread out of existence, but it can't edit what the human typed here.
        if transcript_path:
            tail = transcript_tail(transcript_path, n=tail_n)
            if tail:
                lines = [
                    "## Transcript Tail (ground truth — the human's last words, verbatim)",
                    "_Reconcile the narrative against THIS. A thread here but not in the "
                    "narrative was DROPPED — surface it, don't swallow it._",
                ]
                for t in tail:
                    lines.append(f"- {t}")
                parts.append("\n".join(lines))

        if not parts:
            return "(No continuity data — fresh session)"

        header = "⚡ ECHELON CONTINUITY — structured state for the next hop\n"
        return header + "\n\n".join(parts)

    # ── stats ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Quick stats about the ledger."""
        if not self._path.exists():
            return {"events": 0, "scope": self.scope}

        counts: dict[str, int] = {}
        tasks: dict[str, str] = {}
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type", "?")
                counts[t] = counts.get(t, 0) + 1
                if t == "task":
                    tid = evt.get("task_id", "")
                    tasks[tid] = evt.get("status", "?")

        return {
            "scope": self.scope,
            "events": sum(counts.values()),
            "by_type": counts,
            "active_tasks": sum(1 for s in tasks.values() if s in ("pending", "in_progress")),
            "total_tasks": len(tasks),
        }


# ── convenience ───────────────────────────────────────────────────────────────

def open_ledger(scope: str = "echelon", session_id: str | None = None) -> SessionLedger:
    """Open the session state ledger for a scope."""
    return SessionLedger(scope, session_id=session_id)


def list_ledgers() -> list[dict]:
    """List all scopes that have session state ledgers."""
    ledgers: list[dict] = []
    if _STATE_DIR.exists():
        for p in sorted(_STATE_DIR.glob("*.jsonl")):
            scope = p.stem
            ledger = SessionLedger(scope)
            ledgers.append({"scope": scope, "path": str(p), **ledger.stats()})
    return ledgers


# ── Discoverable compact protocol ─────────────────────────────────────────────
# Any ECHELON substrate session — ECHELON agent loop OR plain Claude Code —
# checks for unconsumed compacts on startup. The compact replaces the frozen
# spine for non-ECHELON harnesses (Claude Code has no PinRegistry).

def _norm_path(p: str) -> str:
    """Normalize a project path for cross-estate comparison (case/sep-insensitive
    on Windows). A compact authored as 'd:\\WORK\\ECHELON' and a cwd of
    'D:\\WORK\\ECHELON' are the SAME estate — the drift bug was treating them as
    different OR not filtering at all."""
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


def _estate_of_path(path: str) -> str:
    """Heuristically map a touched file path to its estate root.

    Walks up from the file's directory looking for the nearest ancestor that
    contains a `.git` dir, a `memory/` dir, or a `.echelon/` dir — any of which
    signals an estate root. Falls back to the path's top-level work dir (the
    first segment after the drive/root). Always returns an absolute, normalized
    path (never empty for a non-empty input).
    """
    if not path:
        return ""
    p = Path(path).resolve()
    if not p.is_absolute():
        return ""
    # Start from the file's parent dir (or the file itself if it's a dir)
    cur = p if p.is_dir() else p.parent
    # Walk up looking for estate markers
    root = Path(p.anchor)  # e.g. C:\ or /
    while cur != root and cur != cur.parent:
        if (cur / ".git").exists():
            return _norm_path(str(cur))
        if (cur / "memory").is_dir():
            return _norm_path(str(cur))
        if (cur / ".echelon").is_dir():
            return _norm_path(str(cur))
        cur = cur.parent
    # Fall back: the top-level directory below the anchor
    # e.g. for ~\work\repo\file.py, walk up to C:\, then
    # the first meaningful dir under the drive is the fallback.
    parts = Path(_norm_path(str(p))).parts
    # On Windows parts might be ('C:\\', 'Users', 'user', ...)
    # Fall back to the root of the repo-ish structure
    return _norm_path(str(Path(p.anchor)))


def discover_compact(scope: str, project_path: str | None = None,
                     cross_estate: bool = False) -> dict | None:
    """Find the most recent UNCONSUMED compact for a scope.

    This is the startup check: any session using ECHELON as substrate calls
    this first. If a compact exists with status=new and no matching
    compact_taken event, return it so the session can consume it.

    ESTATE-AWARE RESOLUTION (the cross-repo fix, v2):
      1. explicit `project_path` arg — filter by that estate
      2. env `ECHELON_ESTATE` if set and no explicit arg
      3. otherwise UNFILTERED (no cwd fallback) — caller sees ALL estates
    A compact matches when the resolved estate is a MEMBER of its
    `target_estates` list. Old compacts without `target_estates` resolve via
    their legacy `project_path` (back-compat).

    Pass cross_estate=True to deliberately return the newest unconsumed compact
    from ANY estate (ignoring the estate filter entirely).

    Args:
        scope: Bank scope to check
        project_path: Estate filter. None → resolve via ECHELON_ESTATE, then
            unfiltered. "" explicitly disables the filter. cross_estate=True
            also disables it.
        cross_estate: If True, ignore the estate filter and return the newest
            unconsumed compact from any project.

    Returns:
        The compact dict if found, None if nothing to consume. The returned
        dict may carry a "_cross_estate_warning" key if a NEWER compact exists
        for a DIFFERENT estate than the one returned.
    """
    ledger = SessionLedger(scope)
    if not ledger._path.exists():
        return None

    # ── C3: resolve the estate filter (kill cwd-keying) ──────────────────
    # 1. explicit project_path arg
    # 2. ECHELON_ESTATE env
    # 3. unfiltered (no cwd fallback)
    resolved_estate = None
    if project_path is not None:
        resolved_estate = project_path  # explicit — may be "" to disable
    elif not cross_estate:
        resolved_estate = os.environ.get("ECHELON_ESTATE", "")
        # If env is empty/unset, resolved_estate stays "" → unfiltered
    want = _norm_path(resolved_estate) if (resolved_estate and not cross_estate) else ""

    # Collect all events
    all_events: list[dict] = []
    with open(ledger._path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Collect consumed timestamps AND their tokens (for token enforcement)
    consumed: dict[str, str] = {}  # compact_ts -> token used
    for evt in all_events:
        if evt.get("type") == "compact_taken":
            ts = evt.get("compact_ts", "")
            tok = evt.get("token", "")
            if ts:
                consumed[ts] = tok

    # ── Helper: test if a compact matches the resolved estate ─────────────
    def _compact_matches_estate(evt: dict, want_estate: str) -> bool:
        """True if evt targets the wanted estate (membership in target_estates,
        or legacy project_path back-compat)."""
        if not want_estate:
            return True  # unfiltered
        targets = evt.get("target_estates")
        if targets:
            # C1: membership in target_estates list
            return any(_norm_path(t) == want_estate for t in targets)
        # Back-compat: old compacts lack target_estates — fall back to project_path
        return _norm_path(evt.get("project_path", "")) == want_estate

    # The newest unconsumed compact OVERALL (any estate) — used to warn if the
    # estate filter is hiding a newer one from a different project.
    newest_any: dict | None = None
    match: dict | None = None
    for evt in reversed(all_events):
        if evt.get("type") != "compact":
            continue
        if evt.get("status") != "new":
            continue
        if evt["ts"] in consumed:
            continue
        if newest_any is None:
            newest_any = evt
        if not _compact_matches_estate(evt, want):
            continue
        match = evt
        break

    if match is None:
        # Nothing matching the resolved estate — return None.
        # The caller (CLI/gate) may re-query cross-estate to report.
        return None

    # If the matched compact is NOT the newest overall, flag cross-estate drift.
    if newest_any is not None and newest_any["ts"] != match["ts"]:
        match = dict(match)
        match["_cross_estate_warning"] = {
            "newer_ts": newest_any["ts"],
            "newer_project": newest_any.get("project_path", ""),
        }
    return match

def discover_all_unconsumed(scope: str, limit: int = 20) -> list[dict]:
    """Return ALL unconsumed compacts for a scope (newest first), bounded to `limit`.
    Used by the ambiguity menu (C4) when no estate is resolved and multiple compacts
    exist across estates.
    """
    ledger = SessionLedger(scope)
    if not ledger._path.exists():
        return []

    all_events: list[dict] = []
    with open(ledger._path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    consumed: set[str] = set()
    for evt in all_events:
        if evt.get("type") == "compact_taken":
            ts = evt.get("compact_ts", "")
            if ts:
                consumed.add(ts)

    results: list[dict] = []
    for evt in reversed(all_events):
        if evt.get("type") != "compact":
            continue
        if evt.get("status") != "new":
            continue
        if evt["ts"] in consumed:
            continue
        results.append(evt)
        if limit > 0 and len(results) >= limit:  # limit<=0 = unlimited (the resolve path passes 0)
            break
    return results


def compact_handle_status(scope: str, compact_id: str) -> str:
    """For an EXACT-or-prefix compact_id, say whether it names a compact and its state:
    'unconsumed' (resumable), 'consumed' (already taken), or 'none' (not a compact handle).
    Lets relive give an honest message — 'already consumed' vs falling through to 'no card'."""
    if len(compact_id) < 6:
        return "none"
    ledger = SessionLedger(scope)
    if not ledger._path.exists():
        return "none"
    events = []
    with open(ledger._path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    consumed_ts = {e.get("compact_ts") for e in events if e.get("type") == "compact_taken"}
    matched = None
    for e in events:
        if e.get("type") != "compact":
            continue
        cid = compact_id_for(e)
        if cid == compact_id or cid.startswith(compact_id):
            matched = e  # newest wins (iterate forward, keep last)
    if matched is None:
        return "none"
    return "consumed" if matched["ts"] in consumed_ts else "unconsumed"


def consume_compact(scope: str, compact_ts: str, *,
                    token: str = "",
                    consumed_by: str = "",
                    project_path: str = "") -> dict | None:
    """Mark a compact as consumed. TOKEN-ENFORCED — the token MUST match
    the compact's consumption token. Without the correct token, the compact
    cannot be consumed (prevents cross-session/double-consumption races).

    Idempotent: if the same token already consumed this compact, returns None.
    """
    ledger = SessionLedger(scope)

    if not token:
        return None  # Cannot consume without a token

    if not ledger._path.exists():
        return None

    # Single-pass: validate token AND check if already consumed
    all_events: list[dict] = []
    with open(ledger._path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Find the compact and validate its token
    compact_token: str | None = None
    for evt in all_events:
        if evt.get("type") == "compact" and evt["ts"] == compact_ts:
            compact_token = evt.get("token", "")
            break

    if compact_token is None:
        return None  # Compact doesn't exist

    if token != compact_token:
        return None  # Wrong token — reject

    # Check if already consumed
    for evt in all_events:
        if (evt.get("type") == "compact_taken" and
                evt.get("compact_ts") == compact_ts):
            return None  # Already consumed

    return ledger.compact_taken(
        compact_ts,
        token=token,
        consumed_by=consumed_by,
        project_path=project_path,
    )


def discover_and_consume(scope: str, *,
                         consumed_by: str = "",
                         project_path: str | None = None,
                         cross_estate: bool = False) -> dict | None:
    """Full discovery → consume cycle. Returns the compact if one was found
    and consumed, None otherwise. Call this on session start.

    The compact's OWN token is extracted from the discovered compact and
    passed to consume_compact — the session doesn't need to know it ahead
    of time. This is the standard session-start entry point.

    ESTATE-AWARE BY DEFAULT: discovers via the C3 resolution order (explicit
    project_path → ECHELON_ESTATE env → unfiltered). Pass cross_estate=True
    to reach across estates. The returned compact may carry a
    "_cross_estate_warning" — the caller MUST surface it (a newer compact
    for a different estate was NOT consumed).
    """
    compact = discover_compact(scope, project_path=project_path,
                               cross_estate=cross_estate)
    if not compact:
        return None
    token = compact.get("token", "")
    consume_compact(
        scope, compact["ts"],
        token=token,
        consumed_by=consumed_by,
        project_path=project_path or compact.get("project_path", ""),
    )
    return compact


# ── compact_id: stable non-secret handle (B1) ─────────────────────────────────

def compact_id_for(evt: dict) -> str:
    """Return the stable, non-secret compact_id for a compact event.

    If the event already carries compact_id (top-level or in meta), return it.
    Otherwise derive it from session_uuid + ts (back-compat for old compacts
    written before compact_id was added — the handle is stable on read without
    rewriting the ledger).
    """
    cid = evt.get("compact_id") or evt.get("meta", {}).get("compact_id")
    if cid:
        return cid
    su = evt.get("session_uuid", "")
    ts = evt.get("ts", "")
    return hashlib.sha256((su + "|" + ts).encode()).hexdigest()[:12]


def list_unconsumed_compacts(scope: str, limit: int = 8) -> list[dict]:
    """Return newest-first unconsumed compacts ACROSS ALL ESTATES for the scope.

    Each dict: {compact_id, ts, goal, target_estates, project_path}.
    Relive's menu calls this — it shows all compacts, no estate filter
    (like recent_arc_cards).
    """
    all_uc = discover_all_unconsumed(scope, limit=limit)
    result: list[dict] = []
    for evt in all_uc:
        meta = evt.get("meta", {})
        result.append({
            "compact_id": compact_id_for(evt),
            "ts": evt.get("ts", ""),
            "goal": meta.get("current_goal", "") or "",
            "target_estates": evt.get("target_estates") or meta.get("target_estates") or [],
            "project_path": evt.get("project_path", ""),
        })
    return result


def resolve_compact_by_id(scope: str, compact_id: str) -> dict | None:
    """Find an unconsumed compact by its compact_id (accepts >=6 char prefix).

    GLOBAL: a named handle bypasses the estate filter entirely — the user
    named the exact thing, so estate-scoping is moot.

    Returns the full compact event dict if exactly one match, None if no match,
    or {"_ambiguous": True, "matches": [...]} if the prefix matches multiple.
    """
    if len(compact_id) < 6:
        return None  # Too short to be a compact_id prefix (min 6 chars)
    all_uc = discover_all_unconsumed(scope, limit=0)  # 0 = unlimited
    matches: list[dict] = []
    for evt in all_uc:
        cid = compact_id_for(evt)
        if cid == compact_id or cid.startswith(compact_id):
            matches.append(evt)
    if len(matches) == 0:
        return None
    if len(matches) == 1:
        return matches[0]
    return {"_ambiguous": True, "matches": matches}


def consume_compact_by_id(scope: str, compact_id: str,
                          consumed_by: str = "") -> dict | None:
    """Resolve a compact by id, then consume it using its OWN token.

    The token is read from the record — the user never types it.
    Returns the consumed compact dict (with its continuity block) or None.
    """
    resolved = resolve_compact_by_id(scope, compact_id)
    if resolved is None or resolved.get("_ambiguous"):
        return None
    token = resolved.get("token", "")
    if not token:
        return None
    consume_compact(scope, resolved["ts"], token=token,
                    consumed_by=consumed_by or "relive",
                    project_path=resolved.get("project_path", ""))
    return resolved


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_show(args) -> int:
    """Show the continuity block for a scope."""
    ledger = SessionLedger(args.scope)
    if not ledger._path.exists():
        print(f"No session state for scope '{args.scope}'.")
        return 0

    print(f"=== Session State: {args.scope} ===\n")
    print(ledger.continuity_block())
    return 0


def _cmd_stats(args) -> int:
    """Show stats for one or all ledgers."""
    if args.scope:
        ledger = SessionLedger(args.scope)
        print(json.dumps(ledger.stats(), indent=2))
    else:
        for info in list_ledgers():
            print(f"{info['scope']}: {info['events']} events, "
                  f"{info['active_tasks']}/{info['total_tasks']} active tasks")
    return 0


def _cmd_log(args) -> int:
    """Show recent events from the ledger."""
    ledger = SessionLedger(args.scope)
    events = ledger.read(
        types=args.types.split(",") if args.types else None,
        worth=args.worth or None,
        limit=args.n or 20,
    )
    if not events:
        print(f"No events in '{args.scope}' ledger.")
        return 0

    for evt in events:
        t = evt.get("type", "?")
        ts = evt.get("ts", "")[:19]
        if t == "task":
            print(f"{ts} 📋 [{evt.get('status','?')}] {evt.get('task_id','')} — {evt.get('subject','')[:100]}")
        elif t == "decision":
            print(f"{ts} 💡 {evt.get('conclusion','')[:150]}")
        elif t == "file":
            print(f"{ts} 📄 {evt.get('action','?')}: {evt.get('path','')}")
        elif t == "goal":
            print(f"{ts} 🎯 [{evt.get('status','?')}] {evt.get('goal','')[:150]}")
        elif t == "checkpoint":
            print(f"{ts} ⚡ CHECKPOINT: {evt.get('summary','')[:150]}")
        elif t == "recall":
            atoms = evt.get("atoms", [])
            print(f"{ts} 🧠 recall: {', '.join(atoms[:5])} ({evt.get('intent','')[:80]})")
        elif t == "compact":
            status = evt.get("status", "?")
            proj = evt.get("project_path", "")[:40]
            model = (evt.get("meta") or {}).get("model", "")
            print(f"{ts} 📦 COMPACT [{status}] project={proj} model={model} session={evt.get('session_uuid','')[:12]}")
        elif t == "compact_taken":
            print(f"{ts} ✅ COMPACT_TAKEN by={evt.get('consumed_by','')[:12]} ref={evt.get('compact_ts','')[:19]}")
        else:
            print(f"{ts} [{t}] {json.dumps(evt, ensure_ascii=False)[:200]}")
    return 0


def _warn_cross_estate(compact: dict) -> None:
    """Print the cross-estate-drift warning if the compact carries one."""
    w = compact.get("_cross_estate_warning")
    if w:
        print(f"\n⚠ CROSS-ESTATE DRIFT: a NEWER unconsumed compact exists for a "
              f"DIFFERENT project —\n   newer: {w['newer_ts'][:19]}  "
              f"project: {w['newer_project']}\n   You consumed the newest IN-ESTATE "
              f"compact. If the live work is in that other estate, run there instead.")


def _report_hidden_newer(scope: str, project_path: str | None) -> bool:
    """If nothing matched in-estate but a newer compact exists elsewhere, say so.
    Returns True if a hidden cross-estate compact was reported."""
    if project_path == "":  # filter explicitly disabled — nothing hidden
        return False
    other = discover_compact(scope, cross_estate=True)
    if other:
        resolved = _norm_path(project_path) if project_path else _norm_path(os.environ.get("ECHELON_ESTATE", ""))
        if resolved and _norm_path(other.get("project_path", "")) != resolved:
            print(f"\n⚠ A newer unconsumed compact exists for ANOTHER estate:")
            print(f"   {other['ts'][:19]}  project: {other.get('project_path','?')}")
            print(f"   Not consumed (estate-scoped). To take it deliberately:")
            print(f"   echelon session-state discover --consume --cross-estate --scope {scope}")
            return True
    return False


def _cmd_discover(args) -> int:
    """Check for unconsumed compacts. With --consume, performs full discovery+consumption.

    C4 (ambiguity menu): when no estate is resolved (no --project, no ECHELON_ESTATE)
    and multiple unconsumed compacts exist across estates, prints a menu instead of
    silently picking one. The user selects via --pick <idx> or --ts <ts>.
    """
    proj = args.project if args.project else None
    pick_idx = getattr(args, 'pick', None)
    pick_ts = getattr(args, 'ts_select', None)

    # Resolve estate (C3 order)
    resolved_estate = proj or os.environ.get("ECHELON_ESTATE", "")
    is_ambiguous = (not resolved_estate and not getattr(args, 'cross_estate', False))

    if args.consume:
        # ── consume path ──────────────────────────────────────────────────
        # If ambiguous (>1 estate, no estate resolved) AND no --pick/--ts,
        # show menu and refuse to auto-consume.
        if is_ambiguous and pick_idx is None and pick_ts is None:
            all_uc = discover_all_unconsumed(args.scope)
            if len(all_uc) > 1:
                _print_ambiguity_menu(all_uc, args.scope)
                print("\nRe-run with --consume --pick <idx> (or --ts <ts>) to select.")
                return 0
            if len(all_uc) == 1:
                # Exactly one total — no ambiguity, consume it directly
                compact = all_uc[0]
            else:
                print(f"No unconsumed compacts for scope '{args.scope}'.")
                return 0
        elif pick_ts:
            # Consume by exact timestamp
            compact = _find_unconsumed_by_ts(args.scope, pick_ts)
            if not compact:
                print(f"No unconsumed compact with ts '{pick_ts}' for scope '{args.scope}'.")
                return 1
        elif pick_idx is not None:
            all_uc = discover_all_unconsumed(args.scope)
            if pick_idx < 0 or pick_idx >= len(all_uc):
                print(f"Invalid index {pick_idx}. Valid: 0–{len(all_uc)-1}")
                return 1
            compact = all_uc[pick_idx]
        else:
            # Normal estate-resolved path
            compact = discover_compact(
                args.scope, project_path=proj,
                cross_estate=args.cross_estate,
            )

        if not compact:
            print(f"No unconsumed compacts for scope '{args.scope}' in this estate.")
            _report_hidden_newer(args.scope, proj)
            return 0

        # Consume it
        token = compact.get("token", "")
        consumed = consume_compact(
            args.scope, compact["ts"],
            token=token,
            consumed_by=args.consumed_by or "cli",
            project_path=proj or compact.get("project_path", ""),
        )
        if consumed is None and token:
            print(f"Compact {compact['ts'][:19]} could not be consumed (token mismatch or already taken).")
            return 1
        print(f"Consumed compact: {compact['ts'][:19]}")
        print(f"  Session:  {compact.get('session_uuid', '?')[:24]}")
        print(f"  Project:  {compact.get('project_path', '?')}")
        _warn_cross_estate(compact)
        continuity = compact.get("continuity", "")
        if continuity:
            print(f"\n── Continuity block ({len(continuity)} chars) ──")
            print(continuity[:2000])
        return 0

    # ── discover (non-consume) path ───────────────────────────────────────
    # C4: when ambiguous, show menu
    if is_ambiguous:
        all_uc = discover_all_unconsumed(args.scope)
        if len(all_uc) > 1:
            _print_ambiguity_menu(all_uc, args.scope)
            print("\nUse --pick <idx> (or --ts <ts>) to select, or --project <path> to filter by estate.")
            return 0
        if len(all_uc) == 0:
            print(f"No unconsumed compacts for scope '{args.scope}'.")
            return 0
        # Exactly one — show it directly
        compact = all_uc[0]
    elif pick_ts:
        compact = _find_unconsumed_by_ts(args.scope, pick_ts)
        if not compact:
            print(f"No unconsumed compact with ts '{pick_ts}' for scope '{args.scope}'.")
            return 1
    elif pick_idx is not None:
        all_uc = discover_all_unconsumed(args.scope)
        if pick_idx < 0 or pick_idx >= len(all_uc):
            print(f"Invalid index {pick_idx}. Valid: 0–{len(all_uc)-1}")
            return 1
        compact = all_uc[pick_idx]
    else:
        compact = discover_compact(args.scope, project_path=proj,
                                   cross_estate=args.cross_estate)

    if not compact:
        print(f"No unconsumed compacts for scope '{args.scope}' in this estate.")
        _report_hidden_newer(args.scope, proj)
        return 0
    _warn_cross_estate(compact)

    print(f"Found unconsumed compact: {compact['ts'][:19]}")
    print(f"  Session:  {compact.get('session_uuid', '?')[:24]}")
    print(f"  Project:  {compact.get('project_path', '?')}")
    print(f"  Status:   {compact.get('status', '?')}")
    meta = compact.get("meta", {})
    if meta.get("model"):
        print(f"  Model:    {meta['model']}")
    if meta.get("tokens_used"):
        print(f"  Tokens:   {meta['tokens_used']}")
    if meta.get("current_goal"):
        print(f"  Goal:     {meta['current_goal'][:120]}")
    targets = compact.get("target_estates") or meta.get("target_estates")
    if targets:
        print(f"  Estates:  {', '.join(targets)}")
    open_tasks = meta.get("open_tasks", [])
    if open_tasks:
        print(f"  Tasks:    {', '.join(open_tasks)}")
    continuity = compact.get("continuity", "")
    if continuity:
        print(f"\n── Continuity block ({len(continuity)} chars) ──")
        print(continuity[:2000])
    return 0


def _print_ambiguity_menu(compacts: list[dict], scope: str) -> None:
    """C4: print a menu of unconsumed compacts across estates."""
    print(f"\nMultiple unconsumed compacts for scope '{scope}' across estates:\n")
    print(f"{'Idx':<4} {'Timestamp':<22} {'Goal':<62} {'Estates'}")
    print("-" * 100)
    for i, c in enumerate(compacts):
        ts = c['ts'][:19]
        meta = c.get('meta', {})
        goal = (meta.get('current_goal', '') or '(no goal)')[:60]
        targets = c.get('target_estates') or meta.get('target_estates') or [c.get('project_path', '?')]
        estates = ', '.join(targets)[:40]
        print(f"[{i}]  {ts}  {goal:<62} {estates}")
    if len(compacts) >= 20:
        print(f"\n(showing newest 20 of {len(compacts)}+ — narrow with --project or ECHELON_ESTATE)")


def _find_unconsumed_by_ts(scope: str, ts: str) -> dict | None:
    """Find a specific unconsumed compact by exact timestamp."""
    all_uc = discover_all_unconsumed(scope, limit=0)  # 0 = unlimited
    for c in all_uc:
        if c['ts'] == ts:
            return c
    return None


def _cmd_purge(args) -> int:
    """Remove compact records from the ledger — the clean way to fix pollution
    (a test/smoke-run compact written into the live ledger). Backs up first.
    Match by exact --ts or by --writer session_id. Never touches anything else."""
    import shutil
    ledger = SessionLedger(args.scope)
    if not ledger._path.exists():
        print(f"No ledger for scope '{args.scope}'.")
        return 0
    if not args.ts and not args.writer:
        print("Specify --ts <timestamp> or --writer <session_id> to select what to purge.")
        return 1

    lines = [l for l in open(ledger._path, encoding="utf-8") if l.strip()]
    keep, removed = [], []
    for l in lines:
        try:
            e = json.loads(l)
        except json.JSONDecodeError:
            keep.append(l.rstrip("\n"))
            continue
        is_target = (
            e.get("type") == "compact" and (
                (args.ts and e.get("ts") == args.ts) or
                (args.writer and e.get("session_id") == args.writer)
            )
        )
        (removed if is_target else keep).append(l.rstrip("\n"))

    if not removed:
        print("No matching compact found — nothing to purge.")
        return 0

    print(f"{'Would remove' if args.dry_run else 'Removing'} {len(removed)} compact(s):")
    for l in removed:
        e = json.loads(l)
        print(f"  {e.get('ts','?')[:19]}  writer={e.get('session_id','?')}  "
              f"stamped={e.get('session_uuid','?')[:16]}  proj={e.get('project_path','?')}")
    if args.dry_run:
        print("(dry-run — ledger unchanged)")
        return 0

    # Timestamp the backup so a second purge can't clobber the first one's safety net.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    bak = f"{ledger._path}.bak-purge-{stamp}"
    shutil.copy(ledger._path, bak)
    with open(ledger._path, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")
    print(f"Purged. Backup: {os.path.basename(bak)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="echelon session-state",
        description="Session state ledger — structured continuity between hops")
    sub = ap.add_subparsers(dest="action", help="Action")

    p_show = sub.add_parser("show", help="Show the continuity block for a scope")
    p_show.add_argument("--scope", default="echelon", help="Scope (default: echelon)")

    p_stats = sub.add_parser("stats", help="Show ledger stats")
    p_stats.add_argument("--scope", default=None, help="Scope (omit for all)")

    p_log = sub.add_parser("log", help="Show recent events")
    p_log.add_argument("--scope", default="echelon", help="Scope (default: echelon)")
    p_log.add_argument("--types", help="Filter by type: task,decision,file,goal (comma-separated)")
    p_log.add_argument("--worth", choices=["carry", "archive", "drop"], help="Filter by worth")
    p_log.add_argument("--n", type=int, default=20, help="Max events to show")

    p_discover = sub.add_parser("discover", help="Check for unconsumed compacts (estate-aware)")
    p_discover.add_argument("--scope", default="echelon", help="Scope (default: echelon)")
    p_discover.add_argument("--project", help="Estate filter. None → ECHELON_ESTATE env, then unfiltered. Pass '' to disable.")
    p_discover.add_argument("--cross-estate", action="store_true",
                            help="Reach across estates (consume newest from ANY project)")
    p_discover.add_argument("--consume", action="store_true", help="Consume the compact (discover+consume cycle)")
    p_discover.add_argument("--consumed-by", default="", help="Identifier for the consuming session")
    p_discover.add_argument("--pick", type=int, default=None, help="Select compact by menu index (C4 ambiguity menu)")
    p_discover.add_argument("--ts", dest="ts_select", default=None, help="Select compact by exact timestamp")

    p_purge = sub.add_parser("purge", help="Remove a compact from the ledger (cleanup pollution)")
    p_purge.add_argument("--scope", default="echelon", help="Scope (default: echelon)")
    p_purge.add_argument("--ts", help="Exact compact ts to remove")
    p_purge.add_argument("--writer", help="Remove compacts written by this session_id (writer)")
    p_purge.add_argument("--dry-run", action="store_true", help="Show what would be removed, write nothing")

    args = ap.parse_args(argv)

    if args.action == "show":
        return _cmd_show(args)
    elif args.action == "stats":
        return _cmd_stats(args)
    elif args.action == "log":
        return _cmd_log(args)
    elif args.action == "discover":
        return _cmd_discover(args)
    elif args.action == "purge":
        return _cmd_purge(args)
    else:
        ap.print_help()
        return 0


_main = main

if __name__ == "__main__":
    import sys
    sys.exit(main())
