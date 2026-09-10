"""ECHELON compaction engine — crystallize a conversation into atoms + spine.

Anthropic's compaction produces a flat text summary that gets injected and lost.
ECHELON's compaction produces STRUCTURED output that ACCRETES into the bank:

  1. SUMMARY — dense, structured (what happened / decisions / builds)
  2. ATOMS — candidate facts discovered (one lesson each, bank-ingestible)
  3. SPINE — continuation block for the HOP (the next session's re-entry point)

Combined with the HOP: window fills → ECHELON compact → atoms planted →
spine re-entry → continue. Each hop enriches the bank.

Usage:
  echelon compact run [--transcript FILE] [--provider deepseek|gemini]
  echelon compact extract <log>   # forensic: extract Anthropic summaries
  echelon compact detect <log>    # forensic: list Anthropic compactions
  echelon compact show <log>      # forensic: print one Anthropic summary
"""
from __future__ import annotations

from pathlib import Path

# Canonical paths — the single source of truth for compact artifacts.
# Gate banner and other modules import these instead of hardcoding.
ECHELON_DIR = Path.home() / ".echelon"
HANDOFF_PATH = ECHELON_DIR / "compact_handoff.md"
HANDOFF_PATH_DISPLAY = "~/.echelon/compact_handoff.md"
BANK_PATH_DISPLAY = "~/.echelon/echelon.db"

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Compaction result ────────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    """The structured output of an ECHELON compaction."""
    summary: str                           # Dense conversation summary
    atoms: list[dict] = field(default_factory=list)   # Candidate atoms [{slug, description, body}]
    spine: str = ""                        # Continuation block for the next HOP
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    provider: str = ""
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw: dict | None = None                # Full provider response


# ── Compaction prompt ─────────────────────────────────────────────────────────

COMPACT_SYSTEM = """You are the ECHELON compaction engine. Your job is to crystallize a conversation
into STRUCTURED memory that outlives the context window.

THE THREE OUTPUTS YOU PRODUCE:

1. SUMMARY — A dense, structured account of the conversation. Include:
   - What was the primary goal and was it achieved?
   - What was built / modified / decided?
   - What errors were hit and how were they fixed?
   - What is the current state (files open, work in flight)?

2. ATOMS — Candidate facts discovered during this conversation. Each atom is ONE
   indivisible lesson. Rules:
   - ONE fact per atom. Never merge distinct lessons.
   - Only include what was ACTUALLY DISCOVERED OR CONFIRMED in this conversation.
     Do not invent general advice or restate known facts.
   - Each atom must be SELF-CONTAINED — someone reading it cold must understand it.
   - Use kebab-case slugs that describe the specific finding.
   - If nothing novel was discovered, return an empty list (honesty > volume).

3. SPINE — A continuation block that lets the NEXT session resume exactly where
   this one left off. Include:
   - What work is IN PROGRESS right now (not completed)
   - Open decisions that need resolution
   - Files currently being modified
   - The exact next action to take
   - Any constraints or caveats the next session must know
   Write the spine as a direct address to the next ECHELON session ("You are
   resuming..."). Be specific — the next session has NO context except this spine.

OUTPUT FORMAT — return valid JSON only, no markdown fence, no commentary:
{"summary": "...", "atoms": [{"slug": "...", "description": "...", "body": "..."}], "spine": "..."}

QUALITY: Be precise. Be honest (empty atoms > fake atoms). Be complete (the spine
must be sufficient to resume work without reading the original conversation)."""


def _build_compact_messages(transcript: list[dict]) -> list[dict]:
    """Build the messages array for compaction from a conversation transcript."""
    messages = [{"role": "system", "content": COMPACT_SYSTEM}]

    # Add a condensed version of the conversation
    parts: list[str] = []
    for m in transcript:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text", "")
                    # Truncate very long text blocks
                    if len(t) > 8000:
                        t = t[:8000] + f"\n... [truncated {len(t)-8000} chars]"
                    texts.append(t)
            content = "\n".join(texts)

        if not content:
            continue

        # Truncate very long messages
        if len(content) > 12000:
            content = content[:12000] + f"\n... [truncated {len(content)-12000} chars]"

        parts.append(f"[{role}]: {content}")

    full = "\n\n---\n\n".join(parts)
    messages.append({"role": "user", "content": f"Crystallize this conversation:\n\n{full}"})
    return messages


# ── Provider dispatch ─────────────────────────────────────────────────────────

def _resolve_upstream(name: str | None = None) -> tuple[str, str, str]:
    """Resolve provider → (upstream_url, api_key, model_id). Returns sync-friendly config."""
    key = ""
    upstream = ""

    if name is None:
        try:
            from echelon_sdk.config import resolve_llm_provider
            resolved = resolve_llm_provider()
            name = resolved.get("mode", "deepseek")
            upstream = resolved.get("upstream", "") or ""
            key = resolved.get("api_key", "") or ""
        except Exception:
            name = "deepseek"

    if name in ("gemini", "vertex"):
        # Gemini uses a different path — OpenAI-compatible endpoint
        upstream = "https://generativelanguage.googleapis.com/v1beta/openai"
        if not key:
            try:
                from echelon_sdk.keys import load_gemini_key
                key = load_gemini_key() or ""
            except Exception:
                pass
        model = "gemini-2.5-flash"
    else:
        if not upstream:
            upstream = os.environ.get("ECHELON_UPSTREAM", "https://api.deepseek.com/anthropic")
        if not key:
            key = os.environ.get("ECHELON_UPSTREAM_KEY", os.environ.get("ANTHROPIC_AUTH_TOKEN", ""))
        if not key:
            try:
                from echelon_sdk.keys import load_deepseek_key
                key = load_deepseek_key() or ""
            except Exception:
                pass
        model = "deepseek-chat"

    return upstream, key, model


def _call_upstream(
    messages: list[dict],
    upstream_url: str,
    api_key: str,
    model_id: str,
    max_tokens: int = 4000,
    timeout: int = 120,
) -> tuple[str, int, int]:
    """Make a sync HTTP call to the upstream API (Anthropic-compatible endpoint).
    Returns (response_text, tokens_in_est, tokens_out)."""
    import urllib.request
    import urllib.error

    body = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    payload = json.dumps(body).encode("utf-8")

    # DeepSeek Anthropic endpoint: /anthropic/v1/messages
    url = upstream_url.rstrip("/")
    if not url.endswith("/messages"):
        if "/anthropic" in url:
            url = url.rstrip("/") + "/v1/messages"
        else:
            url = url + "/v1/messages"

    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        headers["x-api-key"] = api_key

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        return f"Upstream error {e.code}: {err_body}", 0, 0

    # Extract text from Anthropic response format
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")

    usage = data.get("usage", {})
    tokens_in = usage.get("input_tokens", 0)
    tokens_out = usage.get("output_tokens", 0)

    return text, tokens_in, tokens_out


def _parse_compact_response(text: str) -> dict:
    """Parse the model's JSON response, tolerant of markdown fences and truncation."""
    import re
    # Strip thinking tags (DeepSeek R1)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)

    # Try to find a JSON object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # Fallback: treat the whole text as the summary
    return {"summary": text, "atoms": [], "spine": ""}


# ── Core compaction ───────────────────────────────────────────────────────────

def compact(
    messages: list[dict],
    provider_name: str | None = None,
    model_id: str | None = None,
) -> CompactionResult:
    """Run ECHELON compaction on a conversation transcript.

    Args:
        messages: The conversation in OpenAI format [{role, content}, ...]
        provider_name: 'deepseek', 'gemini', or None (auto-detect from config)
        model_id: Override the default model for the provider

    Returns:
        CompactionResult with summary, atoms, spine, and metadata
    """
    upstream, key, default_model = _resolve_upstream(provider_name)
    model = model_id or default_model

    compact_msgs = _build_compact_messages(messages)

    # Estimate input tokens (rough: 4 chars ≈ 1 token)
    full_text = "\n".join(str(m.get("content", "")) for m in compact_msgs)
    tokens_in_est = len(full_text) // 4

    try:
        text, tokens_in, tokens_out = _call_upstream(
            compact_msgs, upstream, key, model, max_tokens=4000, timeout=120)
    except Exception as e:
        return CompactionResult(
            summary=f"Compaction failed: {e}",
            tokens_in=tokens_in_est,
            provider=provider_name or "auto",
            model=model,
        )

    parsed = _parse_compact_response(text)

    return CompactionResult(
        summary=parsed.get("summary", text),
        atoms=parsed.get("atoms", []),
        spine=parsed.get("spine", ""),
        tokens_in=tokens_in or tokens_in_est,
        tokens_out=tokens_out,
        model=model,
        provider=provider_name or "auto",
        raw={"provider_response": text, "usage": {"input": tokens_in, "output": tokens_out}},
    )


def compact_and_plant(
    messages: list[dict],
    scope: str = "echelon",
    provider_name: str | None = None,
    output_dir: str | Path | None = None,
) -> CompactionResult:
    """Compact a conversation AND plant the resulting atoms into the bank.

    This is the full crystallize cycle: compact → plant atoms → save spine.
    """
    result = compact(messages, provider_name=provider_name)

    # Plant atoms into the bank
    if result.atoms:
        try:
            from echelon_engine.atoms.cards import CardStore
            cs = CardStore()
            planted = 0
            for atom in result.atoms:
                slug = atom.get("slug", "").strip()
                body = atom.get("body", "").strip()
                if not slug or not body:
                    continue
                coord = f"{scope}:{slug}"
                try:
                    cs.add_atom(coord, body, scope=scope, kind="compact")
                    planted += 1
                except Exception:
                    pass
            result.raw = result.raw or {}
            result.raw["atoms_planted"] = planted
        except Exception as e:
            result.raw = result.raw or {}
            result.raw["plant_error"] = str(e)

    # Save spine to output dir
    if output_dir and result.spine:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        spine_path = output_dir / f"spine_{ts}.md"
        with open(spine_path, "w", encoding="utf-8") as f:
            f.write(f"# ECHELON Spine — {ts}\n\n")
            f.write(result.spine)
            f.write(f"\n\n---\nCompacted: {result.ts}\nModel: {result.model}\n")
        result.raw = result.raw or {}
        result.raw["spine_path"] = str(spine_path)

    return result


# ── Self-compaction: the running provider compacts its OWN conversation ─────
# The key efficiency: the provider already has the conversation in its prompt
# cache. Instead of sending the full transcript to a separate model (paying
# input tokens twice), the running provider produces the continuity block as a
# byproduct of its normal response. The structured state comes from the ledger
# (free — disk read). The narrative summary comes from the provider (uses
# cached prefix, only pays output tokens).

def self_compact(
    scope: str = "echelon",
    narrative: str | None = None,
    provider_name: str | None = None,
    transcript_path: str | None = None,
) -> str:
    """Produce the HOP re-entry context by combining:
    1. Structured continuity block from the session state ledger (FREE — disk read)
    2. The transcript tail — the human's last words VERBATIM (ground truth)
    3. Optional narrative summary from the running provider (uses CACHED conversation)

    This is what the HOP reads on re-entry. The structured part requires NO model
    call — it reads the ledger that the agent maintained during the session.

    Args:
        scope: Bank scope
        narrative: Optional narrative summary (if the provider already produced one)
        provider_name: If provided and no narrative given, produce one via model call
        transcript_path: Path to the Claude Code .jsonl transcript — its tail is
            embedded as ground truth the next session diffs the narrative against

    Returns:
        Complete HOP re-entry context (structured state + transcript tail + narrative)
    """
    from echelon_engine.session_state import SessionLedger

    ledger = SessionLedger(scope)
    block = ledger.continuity_block(transcript_path=transcript_path)

    if narrative:
        return f"{block}\n\n## Narrative Summary\n{narrative}"

    # No narrative provided — the structured block alone may be sufficient
    # for the HOP. If the caller wants a narrative, they pass it in or
    # call compact() separately with the conversation.
    return block


def self_compact_and_checkpoint(
    scope: str = "echelon",
    narrative: str = "",
    tokens_used: int = 0,
    session_uuid: str = "",
    project_path: str = "",
    model: str = "",
    kind: str = "session",
    dry_run: bool = False,
) -> str:
    """Self-compact AND write a discoverable compact record.

    Call this at window limits or session boundaries. Produces:
    1. A compact record in the session state ledger (status: new, with token)
    2. A handoff file at ~/.echelon/compact_handoff.md (for warmup discovery)

    Returns the HOP re-entry context with a post-compact reminder message.

    dry_run=True builds and RETURNS the block but writes NOTHING (no ledger
    record, no handoff file) — the safe way to smoke-test compaction without
    polluting the live continuity ledger. kind tags provenance ("session" vs
    "manual"/"test") so a non-handoff write is always traceable.
    """
    from echelon_engine.session_state import SessionLedger, find_transcript

    ledger = SessionLedger(scope)

    # Locate this session's transcript so its tail (ground truth) rides along.
    transcript_path = find_transcript(session_uuid=session_uuid, project_path=project_path)

    # Build the continuity block (with the transcript tail embedded)
    block = self_compact(
        scope=scope,
        narrative=narrative if narrative else None,
        transcript_path=transcript_path,
    )

    if dry_run:
        # Smoke-test path: produce the block, touch NOTHING durable.
        return block + "\n\n[dry-run — no compact written, no handoff saved]"

    # Write discoverable compact record
    compact_rec = ledger.compact(
        block,
        session_uuid=session_uuid,
        project_path=project_path,
        model=model,
        tokens_used=tokens_used,
        kind=kind,
    )

    # Write handoff file for warmup/gate discovery
    HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    handoff_text = (
        f"# ECHELON Continuity Handoff\n\n"
        f"**Scope:** `{scope}`\n"
        f"**Project:** `{project_path}`\n"
        f"**Session:** `{session_uuid}`\n"
        f"**Compacted:** {compact_rec['ts']}\n"
        f"**Tokens used:** {tokens_used}\n"
        f"**Token:** `{compact_rec.get('token', '')[:16]}...`\n\n"
        f"---\n\n"
        f"## Compact Protocol (read before acting)\n\n"
        f"A compact is STRUCTURED session state (decisions, working set, checkpoint, narrative) "
        f"PLUS a verbatim **Transcript Tail** (the human's actual last words). The structured "
        f"parts and the narrative are a SELF-REPORT — they can paraphrase a thread out of "
        f"existence. The transcript tail is GROUND TRUTH. It lives in the session state ledger "
        f"with full provenance.\n\n"
        f"**⚠ RECONCILE BEFORE TRUST (do this FIRST):**\n"
        f"1. Read the **Transcript Tail** section. Diff it against the narrative/working set. "
        f"Any thread present in the tail but ABSENT from the narrative was DROPPED by the "
        f"self-report — surface it to the user, do not swallow it.\n"
        f"2. Verify the **Project** path above is where work ACTUALLY last happened — a compact "
        f"can be authored in a different estate than the live work (cross-estate drift). If the "
        f"dirty working tree / newest transcript is elsewhere, trust the ground truth, not the path.\n"
        f"3. Check **provenance**: a real handoff has `kind=session` and its writer `session_id` "
        f"matches its stamped `session_uuid`. A `kind=test`/`manual` compact, or one whose writer "
        f"≠ stamp, is NOT a session handoff — do not treat it as continuity.\n"
        f"4. Only after reconciling: act on the structured state.\n\n"
        f"**Lifecycle:** `new` (unconsumed) → `taken` (consumed). "
        f"Consumption is TOKEN-ENFORCED: the compact carries a cryptographic token; "
        f"to consume it you must present the matching token. This prevents "
        f"double-consumption under concurrent access.\n\n"
        f"**Consuming (the witnessed path — ESTATE-SCOPED):**\n"
        f"```\n"
        f"echelon session-state discover --consume --scope {scope}\n"
        f"```\n"
        f"This runs `discover_and_consume()` — finds the newest unconsumed compact "
        f"**authored in THIS estate (cwd)**, validates the token, marks it `taken`, and returns "
        f"the continuity block. It will NOT silently consume a compact from another project; if a "
        f"newer one exists elsewhere it WARNS and tells you to add `--cross-estate` deliberately. "
        f"The bank WITNESSES the consumption.\n\n"
        f"**Reading directly (the unwitnessed path):**\n"
        f"Reading this file directly (or via the gate banner pointer) gives you the "
        f"content but the bank NEVER witnesses it — no `compact_taken` event is written, "
        f"the compact stays `new`, and the continuity trace is broken. It works "
        f"functionally (you see the state) but earns no warmth.\n\n"
        f"**Idempotency:** Re-consuming with the same token is a no-op (returns None). "
        f"Consuming with no/wrong token is rejected.\n\n"
        f"---\n\n"
        f"{block}"
    )
    with open(HANDOFF_PATH, "w", encoding="utf-8") as f:
        f.write(handoff_text)

    return block


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_run(args) -> int:
    """Run ECHELON compaction on a transcript file."""
    transcript_path = args.transcript
    if not transcript_path:
        print("Error: --transcript <file> is required for 'run'", file=sys.stderr)
        print("  Provide a JSONL transcript file (one JSON message per line)", file=sys.stderr)
        return 1

    if not os.path.exists(transcript_path):
        print(f"Transcript not found: {transcript_path}", file=sys.stderr)
        return 1

    # Load transcript
    messages = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                # Support both flat {role, content} and Claude transcript format
                if "message" in msg:
                    msg = msg["message"]
                messages.append(msg)
            except json.JSONDecodeError:
                pass

    if not messages:
        print("No messages found in transcript.", file=sys.stderr)
        return 1

    print(f"Loaded {len(messages)} messages from {transcript_path}")
    print(f"Provider: {args.provider or 'auto'}")
    print(f"Compacting...")

    output_dir = args.output or str(Path.home() / ".echelon" / "compact_runs")
    result = compact_and_plant(
        messages,
        scope=args.scope,
        provider_name=args.provider,
        output_dir=output_dir,
    )

    print(f"\n{'='*60}")
    print(f"COMPACTION COMPLETE")
    print(f"{'='*60}")
    print(f"Model:     {result.model}")
    print(f"Tokens:    {result.tokens_in} in / {result.tokens_out} out")
    print(f"Atoms:     {len(result.atoms)} candidate(s)")
    if result.raw and "atoms_planted" in result.raw:
        print(f"Planted:   {result.raw['atoms_planted']} atom(s) into bank")
    if result.raw and "spine_path" in result.raw:
        print(f"Spine:     {result.raw['spine_path']}")

    print(f"\n── SUMMARY ──")
    print(result.summary[:2000])

    if result.atoms:
        print(f"\n── CANDIDATE ATOMS ({len(result.atoms)}) ──")
        for atom in result.atoms:
            print(f"  [{atom.get('slug', '?')}] {atom.get('description', '')[:120]}")

    if result.spine:
        print(f"\n── SPINE ──")
        print(result.spine[:1500])

    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="echelon compact",
        description="ECHELON compaction — crystallize conversation into atoms + spine")
    sub = ap.add_subparsers(dest="action", help="Action")

    # run
    p_run = sub.add_parser("run", help="Run ECHELON compaction on a transcript")
    p_run.add_argument("--transcript", "-t", required=True,
                       help="Path to transcript file (JSONL, one message per line)")
    p_run.add_argument("--provider", "-p", choices=["deepseek", "gemini"], default=None,
                       help="Provider for compaction (default: auto-detect from config)")
    p_run.add_argument("--output", "-o", help="Output directory for spine + results")
    p_run.add_argument("--scope", default="echelon", help="Bank scope for atom planting")

    # forensic sub-actions (delegate to compact_extract)
    p_detect = sub.add_parser("detect", help="Detect Anthropic compactions in a proxy log")
    p_detect.add_argument("log_file", nargs="?", help="Path to raw proxy log")
    p_detect.add_argument("--output", "-o", help="Output directory")

    p_extract = sub.add_parser("extract", help="Extract Anthropic compaction summaries from proxy log")
    p_extract.add_argument("log_file", nargs="?", help="Path to raw proxy log")
    p_extract.add_argument("--output", "-o", help="Output directory")

    p_show = sub.add_parser("show", help="Print an Anthropic compaction summary")
    p_show.add_argument("log_file", nargs="?", help="Path to raw proxy log")
    p_show.add_argument("--n", type=int, default=0, help="Which compaction to show")

    # self-compact: produce HOP re-entry context from the ledger
    p_self = sub.add_parser("self", help="Self-compact: produce HOP re-entry context from ledger")
    p_self.add_argument("--scope", default="echelon", help="Scope (default: echelon)")
    p_self.add_argument("--narrative", "-n", help="Optional narrative summary text")
    p_self.add_argument("--tokens", type=int, default=0, help="Tokens used (for checkpoint)")
    p_self.add_argument("--session", default="", help="Session UUID")
    p_self.add_argument("--project", default="", help="Project path (default: cwd)")
    p_self.add_argument("--model", default="", help="Model that produced the compact")
    p_self.add_argument("--kind", default="session", choices=["session", "manual", "test"],
                        help="Provenance of this compact (default: session)")
    p_self.add_argument("--dry-run", action="store_true",
                        help="Build + print the block but write NOTHING (safe smoke test)")
    p_self.add_argument("--force", action="store_true",
                        help="Override the safety guards (stamp a session uuid / write a test kind)")

    args = ap.parse_args(argv)

    if args.action == "run":
        return _cmd_run(args)
    elif args.action == "self":
        import os as _os
        project = args.project or _os.getcwd()

        # GUARD: refuse to write a REAL ('session') compact stamped with an
        # explicit --session uuid unless --force. This is the test-pollution
        # fix: a smoke test that passes --session would otherwise write a
        # fake-lineage handoff the next session consumes as real. Smoke tests
        # must use --dry-run (writes nothing) or --kind test (tagged manual).
        if (args.session and args.kind == "session"
                and not args.dry_run and not args.force):
            print("⚠ REFUSED: writing a 'session' compact stamped with an explicit "
                  "--session uuid risks fake lineage (the pollution bug).", file=sys.stderr)
            print("  Smoke-test? use   --dry-run   (writes nothing).", file=sys.stderr)
            print("  Real manual write? use  --kind manual  (tagged provenance).", file=sys.stderr)
            print("  Sure it's a real session handoff? add  --force.", file=sys.stderr)
            return 2

        context = self_compact_and_checkpoint(
            scope=args.scope,
            narrative=args.narrative or "",
            tokens_used=args.tokens or 0,
            session_uuid=args.session or "",
            project_path=project,
            model=args.model or "",
            kind=args.kind,
            dry_run=args.dry_run,
        )
        print(context)
        print()
        print("─" * 60)
        if args.dry_run:
            print("🧪 DRY RUN — nothing written to ledger or handoff.")
            return 0
        print(f"💾 Compact saved for {args.scope}  (kind={args.kind})")
        print(f"   Continuity:  {HANDOFF_PATH_DISPLAY}")
        print(f"   Ledger:      ~/.echelon/session_state/{args.scope}.jsonl")
        print()
        print("   To resume with continuity:")
        print("   → Claude Code: type /clear    (new context window)")
        print("   → ECHELON HOP: handled automatically on re-entry")
        print("   → New session: echelon session-state discover --consume")
        return 0
    elif args.action in ("detect", "extract", "show"):
        # Delegate to forensic tool
        from echelon_engine.atoms.compact_extract import main as forensic_main
        forensic_argv = [args.action]
        if hasattr(args, 'log_file') and args.log_file:
            forensic_argv.append(args.log_file)
        if hasattr(args, 'output') and args.output:
            forensic_argv.extend(["--output", args.output])
        if hasattr(args, 'n') and args.n is not None:
            forensic_argv.extend(["--n", str(args.n)])
        return forensic_main(forensic_argv)
    else:
        ap.print_help()
        return 0


_main = main

if __name__ == "__main__":
    sys.exit(main())
