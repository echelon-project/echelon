"""Compaction summary extractor — harvest Anthropic-generated conversation summaries
from ECHELON's raw proxy log.

Compaction is a 3-step flow (verified 2026-06-26):
  1. count_tokens → proxy → provider (measure conversation size)
  2. Summarization → Anthropic private infra at Anthropic's summarization infrastructure (TLS-encrypted)
  3. Post-compact summary → injected as system message in next /v1/messages → proxy

Step 3 means the summary transits our proxy and is FULLY extractable from the raw log.
This module detects compaction events and extracts the Anthropic-generated summaries.

Detection signature:
  - A count_tokens request appears
  - The next /v1/messages request is ≥40% smaller than the preceding one
  - That smaller request contains the compaction summary as a system message

Usage:
  python -X utf8 -m echelon_engine compact detect <raw_log>       # list compaction events
  python -X utf8 -m echelon_engine compact extract <raw_log>      # extract & save summaries
  python -X utf8 -m echelon_engine compact show <raw_log> [--n 0] # print summary to stdout
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Core extraction logic ────────────────────────────────────────────────────

def parse_raw_log(path: str | Path) -> list[dict]:
    """Parse a raw proxy log file into a list of request/response records."""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _decode_hex_body(hex_str: str) -> bytes:
    """Decode a hex-encoded body from the raw log format."""
    # Format: "[BINARY N bytes] <hex>" or "[BINARY N bytes, truncated] <hex>"
    idx = hex_str.find("] ")
    if idx == -1:
        return b""
    hex_part = hex_str[idx + 2:].replace(" ", "").replace("\n", "").replace("\r", "")
    try:
        return bytes.fromhex(hex_part)
    except ValueError:
        return b""


def _get_body_size(record: dict) -> int:
    """Extract body size in bytes from a raw log record."""
    body = record.get("body", "")
    if not body:
        return 0
    # Try to parse [BINARY N bytes] format
    import re
    m = re.search(r'\[BINARY (\d+) bytes\]', body)
    if m:
        return int(m.group(1))
    # Otherwise, it's a text body
    return len(body.encode("utf-8"))


def detect_compactions(records: list[dict]) -> list[dict]:
    """Find all compaction events in a sequence of raw log records.

    Returns a list of compaction event dicts:
      {index, ts, pre_size, post_size, reduction_pct, pre_req_index, post_req_index}
    """
    events = []
    # Track message request sizes with their indices
    msg_requests = []  # (record_index, size, record)

    for i, rec in enumerate(records):
        if rec.get("dir") != "REQ":
            continue
        path = rec.get("path", "")
        if not path.endswith("/messages") or "count_tokens" in path:
            continue
        size = _get_body_size(rec)
        if size > 0:
            msg_requests.append((i, size, rec))

    # Find drops ≥40% between consecutive message requests.
    # Filter out tiny post-compact messages (< 50KB) — those are regular user
    # messages, not compaction summaries (which carry the full context summary).
    for j in range(1, len(msg_requests)):
        prev_idx, prev_size, prev_rec = msg_requests[j - 1]
        curr_idx, curr_size, curr_rec = msg_requests[j]

        if prev_size > 50000 and curr_size > 50000:  # Real compactions carry large summaries
            reduction = (prev_size - curr_size) / prev_size
            if reduction >= 0.40:
                # Check for count_tokens within a 60-second window before this event
                ct_found = False
                try:
                    curr_ts = curr_rec.get("ts", "")
                    if curr_ts:
                        from datetime import datetime as _dt, timezone as _tz
                        curr_dt = _dt.fromisoformat(curr_ts)
                        for k in range(max(0, curr_idx - 50), curr_idx):
                            rec = records[k]
                            if (rec.get("dir") == "REQ" and
                                    "count_tokens" in rec.get("path", "")):
                                try:
                                    ct_dt = _dt.fromisoformat(rec.get("ts", ""))
                                    if (curr_dt - ct_dt).total_seconds() < 60:
                                        ct_found = True
                                        break
                                except Exception:
                                    pass
                except Exception:
                    pass

                events.append({
                    "pre_index": prev_idx,
                    "post_index": curr_idx,
                    "ts": curr_rec.get("ts", ""),
                    "pre_size": prev_size,
                    "post_size": curr_size,
                    "reduction_pct": round(reduction * 100, 1),
                    "count_tokens_seen": ct_found,
                    "confidence": "high" if ct_found else "medium",
                })

    # Deduplicate: consecutive compactions within 120s are the same event.
    # After a real compaction, every subsequent message is naturally smaller —
    # we only want the FIRST drop in each compaction cycle.
    deduped: list[dict] = []
    for evt in events:
        if not deduped:
            deduped.append(evt)
            continue
        try:
            last_ts = deduped[-1]["ts"]
            curr_ts = evt["ts"]
            if last_ts and curr_ts:
                from datetime import datetime as _dt, timezone as _tz
                last_dt = _dt.fromisoformat(last_ts)
                curr_dt = _dt.fromisoformat(curr_ts)
                if (curr_dt - last_dt).total_seconds() < 120:
                    continue  # Same compaction cycle, skip
        except Exception:
            pass
        deduped.append(evt)

    return deduped


def extract_postcompact_body(record: dict) -> str | None:
    """Extract the full text body from a post-compaction request record.

    Returns the decoded JSON body as a string if extractable, None otherwise.
    """
    body = record.get("body", "")
    if not body:
        return None

    # Try hex-encoded binary first
    if "[BINARY" in body:
        raw = _decode_hex_body(body)
        if raw:
            return raw.decode("utf-8", errors="replace")

    # Text body (already a string)
    return body


def extract_summary_text(body_str: str) -> str | None:
    """Extract the compaction summary from a post-compaction request body.

    The summary is typically in a user message starting with
    'This session is being continued from a previous conversation...'
    It can also appear as a system message injection (Anthropic's format).

    Searches ALL messages (user and system) for the continuation marker.
    """
    if not body_str:
        return None

    try:
        payload = json.loads(body_str)
    except json.JSONDecodeError:
        return None

    messages = payload.get("messages", [])

    # Markers that identify the compaction summary
    _SUMMARY_MARKERS = [
        "This session is being continued",
        "The conversation so far",
        "Previous conversation summary",
        "Summary of the conversation",
        "Here is a summary",
    ]

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle content blocks (Claude API format)
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            content = "\n".join(texts)

        if not content or not isinstance(content, str):
            continue

        # Search FULL content — the summary marker may be deep in a message
        # that starts with CLAUDE.md / system-reminder preamble (can be 50KB+)
        if any(marker in content for marker in _SUMMARY_MARKERS):
            return content

    return None


def extract_summaries(records: list[dict], output_dir: str | Path | None = None) -> list[dict]:
    """Extract all compaction summaries from a raw log, optionally saving to files.

    Returns list of {ts, reduction_pct, pre_size, post_size, summary, saved_path?}
    """
    events = detect_compactions(records)
    results = []

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    for evt in events:
        post_rec = records[evt["post_index"]]
        body = extract_postcompact_body(post_rec)
        summary = extract_summary_text(body) if body else None

        result = {
            "ts": evt["ts"],
            "reduction_pct": evt["reduction_pct"],
            "pre_size": evt["pre_size"],
            "post_size": evt["post_size"],
            "confidence": evt["confidence"],
            "summary": summary,
        }

        if summary and output_dir:
            # Save to compact_summaries/<timestamp>.json
            try:
                ts = datetime.fromisoformat(evt["ts"]).strftime("%Y%m%d_%H%M%S")
            except Exception:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

            save_path = output_dir / f"compact_{ts}.json"
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump({
                    "ts": evt["ts"],
                    "reduction_pct": evt["reduction_pct"],
                    "pre_size": evt["pre_size"],
                    "post_size": evt["post_size"],
                    "confidence": evt["confidence"],
                    "summary": summary,
                }, f, ensure_ascii=False, indent=2)
            result["saved_path"] = str(save_path)

        results.append(result)

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_detect(records: list[dict], _args) -> int:
    """List compaction events found in the log."""
    events = detect_compactions(records)
    if not events:
        print("No compaction events detected.")
        print("  (Look for ≥40% message-size drop near a count_tokens request)")
        return 0

    print(f"Found {len(events)} compaction event(s):\n")
    for i, evt in enumerate(events):
        tag = "🔴 HIGH" if evt["confidence"] == "high" else "🟡 MEDIUM"
        print(f"  [{i}] {evt['ts']}  {tag}")
        print(f"      pre:  {evt['pre_size']:>10,} bytes")
        print(f"      post: {evt['post_size']:>10,} bytes")
        print(f"      drop: {evt['reduction_pct']:.1f}%")
        print(f"      count_tokens seen: {evt['count_tokens_seen']}")
        print()
    return 0


def _cmd_extract(records: list[dict], args) -> int:
    """Extract and save compaction summaries."""
    output_dir = args.output or str(Path.home() / ".echelon" / "compact_summaries")
    results = extract_summaries(records, output_dir=output_dir)

    if not results:
        print("No compaction events detected.")
        return 0

    saved = 0
    for i, r in enumerate(results):
        if r.get("saved_path"):
            print(f"  [{i}] saved: {r['saved_path']}")
            print(f"      {r['ts']}  -{r['reduction_pct']}%  ({r['pre_size']:,} -> {r['post_size']:,} bytes)")
            saved += 1
        else:
            print(f"  [{i}] skipped: no summary extractable (confidence={r['confidence']})")

    print(f"\n{saved} summary(s) saved to {output_dir}")
    return 0


def _cmd_show(records: list[dict], args) -> int:
    """Print a compaction summary to stdout."""
    events = detect_compactions(records)
    if not events:
        print("No compaction events detected.")
        return 0

    n = args.n if hasattr(args, 'n') and args.n is not None else 0
    if n >= len(events):
        print(f"Compaction index {n} out of range (found {len(events)}).")
        return 1

    evt = events[n]
    post_rec = records[evt["post_index"]]
    body = extract_postcompact_body(post_rec)
    summary = extract_summary_text(body) if body else None

    if not summary:
        print(f"No summary extractable from compaction [{n}].")
        print(f"Post-compact raw body (first 2000 chars):")
        print(body[:2000] if body else "(no body)")
        return 1

    print(f"=== Compaction [{n}]  {evt['ts']}  -{evt['reduction_pct']}%  "
          f"({evt['pre_size']:,} -> {evt['post_size']:,} bytes) ===\n")
    print(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="echelon compact",
        description="Extract Anthropic-generated compaction summaries from ECHELON proxy logs.")
    ap.add_argument("action", nargs="?", default="detect",
                    choices=["detect", "extract", "show"],
                    help="detect: list compaction events | extract: save summaries | show: print one")
    ap.add_argument("log_file", nargs="?", help="Path to raw proxy log file")
    ap.add_argument("--output", "-o", help="Output directory for extracted summaries "
                    "(default: ~/.echelon/compact_summaries)")
    ap.add_argument("--n", type=int, default=0, help="Which compaction to show (for 'show' action)")
    args = ap.parse_args(argv)

    if not args.log_file:
        # Try to find the most recent raw log
        log_dir = Path.home() / ".echelon" / "_proxy"
        if log_dir.exists():
            raw_logs = sorted(log_dir.glob("raw_*.log"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
            if raw_logs:
                args.log_file = str(raw_logs[0])
                print(f"Auto-detected log: {args.log_file}\n", file=sys.stderr)
            else:
                print("No raw log files found in ~/.echelon/_proxy/", file=sys.stderr)
                print("Usage: echelon compact detect <path-to-raw-log>", file=sys.stderr)
                return 1
        else:
            print("No ~/.echelon/_proxy/ directory found.", file=sys.stderr)
            print("Usage: echelon compact detect <path-to-raw-log>", file=sys.stderr)
            return 1

    if not os.path.exists(args.log_file):
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1

    print(f"Parsing {args.log_file}...", file=sys.stderr)
    records = parse_raw_log(args.log_file)
    print(f"  {len(records)} entries", file=sys.stderr)

    if args.action == "detect":
        return _cmd_detect(records, args)
    elif args.action == "extract":
        return _cmd_extract(records, args)
    elif args.action == "show":
        return _cmd_show(records, args)
    return 0


# Entry point for the unified CLI dispatcher
_main = main

if __name__ == "__main__":
    sys.exit(main())
