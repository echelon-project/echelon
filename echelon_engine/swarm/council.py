"""council + skeptic — post-plan deliberation and red-team review.

COUNCIL (`swarm --council`):
  Runs a multi-model deliberation council (brainstorm cartridge) over a plan.
  The council has 4 seats: options (generates alternatives), defect (finds bugs),
  honesty (checks assumptions), decision (ranks/synthesizes), chair (moderates).
  Each seat may run on a different model tier.

SKEPTIC (`swarm --skeptic`):
  Red-team review — actively tries to break the plan. Finds hidden assumptions,
  missing edge cases, resource gaps, failure cascades, and over-engineering.
  Runs on a single strong model with a skeptic frame.

Both read the plan output from `swarm --plan` and produce a structured critique.

Usage:
  swarm council --on plan_report.md
  swarm skeptic --on plan_report.md
  swarm council --goal "..." --context "..."   (standalone, no prior plan)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .context import SwarmContext


# ── Council seats ─────────────────────────────────────────────────────────────

COUNCIL_SEATS = {
    "options": {
        "role": "options-generator",
        "prompt": "Generate 3-5 ALTERNATIVE approaches to this plan. "
                  "For each: what's different, what's the trade-off, and when "
                  "would it be better than the proposed plan? Be concrete.",
    },
    "defect": {
        "role": "defect-finder",
        "prompt": "Find EVERY defect, bug-prone assumption, and logic gap in "
                  "this plan. For each: where it breaks, what the consequence "
                  "is, and a concrete fix. Be ruthless — a missed defect ships.",
    },
    "honesty": {
        "role": "honesty-checker",
        "prompt": "Check every CLAIM in this plan for honesty. Which claims are "
                  "unverified? Which numbers are guessed? Which 'it should work' "
                  "statements lack evidence? Flag overconfidence and hand-waving.",
    },
    "decision": {
        "role": "decision-synthesizer",
        "prompt": "Synthesize ALL findings from the other seats into a SINGLE "
                  "prioritized action list. Rank by: (1) what MUST be fixed before "
                  "building, (2) what SHOULD be improved, (3) what's nice-to-have. "
                  "State your confidence in each ranking.",
    },
}


@dataclass
class SeatResult:
    seat: str
    ok: bool
    content: str
    error: str = ""


def run_council(
    plan_text: str = "",
    goal: str = "",
    context: str = "",
    seats: list[str] | None = None,
    seat_defs: dict | None = None,
    provider: str = "auto",
    model: str = "",
    timeout: int = 300,
    effort: str = "",
) -> dict:
    """Run the council swarm.

    Args:
        plan_text: The plan to critique (from swarm --plan output)
        goal: Goal (if no plan_text, standalone mode)
        context: Additional context
        seats: Which council seat NAMES to run (default: all built-in)
        seat_defs: Optional dict of {name: {role, prompt}} custom seat definitions.
                   Merged with built-in COUNCIL_SEATS (custom takes precedence).
        provider: LLM provider
        model: Model override
        timeout: Per-seat timeout

    Returns:
        Council report dict
    """
    # Merge custom seat definitions with built-in seats
    all_seats = dict(COUNCIL_SEATS)
    if seat_defs:
        all_seats.update(seat_defs)

    selected = [s for s in (seats or all_seats) if s in all_seats]
    if not selected:
        selected = list(all_seats)

    print(f"⚡ ECHELON SWARM — council")
    print(f"   seats: {', '.join(selected)}")

    ctx = SwarmContext("brainstorm")
    frozen = ctx.frozen_block()
    print(f"   frozen block: {len(frozen)} chars (hash={ctx.frozen_hash})")

    results: dict[str, SeatResult] = {}

    def _worker(seat_name: str) -> SeatResult:
        seat = all_seats[seat_name]
        # Build dynamic block with the plan + seat-specific prompt
        parts = [f"## ROLE\nYou are the **{seat['role']}** in a deliberation council."]
        parts.append(f"## INSTRUCTION\n{seat['prompt']}")
        if plan_text:
            parts.append(f"## PLAN TO REVIEW\n{plan_text[:8000]}")
        if goal:
            parts.append(f"## GOAL\n{goal}")
        if context:
            parts.append(f"## CONTEXT\n{context}")
        dynamic = "\n\n".join(parts)
        full_prompt = frozen + "\n\n" + dynamic

        try:
            raw = _send(full_prompt, provider, model, timeout, effort)
            return SeatResult(seat=seat_name, ok=True, content=raw)
        except Exception as e:
            return SeatResult(seat=seat_name, ok=False, content="", error=str(e))

    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(_worker, s): s for s in selected}
        for future in as_completed(futures):
            result = future.result()
            results[result.seat] = result
            status = "✓" if result.ok else "✗"
            print(f"   [{status}] {result.seat} ({len(result.content)} chars)")

    # Synthesize output
    report = {
        "goal": goal or "plan review",
        "seats": selected,
        "findings": {
            name: {"role": all_seats[name]["role"], "content": r.content[:3000],
                   "ok": r.ok, "error": r.error}
            for name, r in results.items()
        },
        "swarm_follow_up": [
            "swarm --skeptic --on <this council output>   (red-team the council findings)",
        ],
    }
    return report


# ── Skeptic ───────────────────────────────────────────────────────────────────

SKEPTIC_FRAME = """\
You are a RED-TEAM SKEPTIC. Your job is to BREAK this plan. Find everything wrong:

1. HIDDEN ASSUMPTIONS — what does this plan assume that isn't stated? What happens
   if that assumption is false?

2. MISSING EDGE CASES — what inputs, states, or conditions aren't handled? What
   breaks at scale? Under load? With bad data? During a partial failure?

3. RESOURCE GAPS — what does this plan need that isn't available? Skills, time,
   money, infrastructure, API access, rate limits?

4. FAILURE CASCADES — if one part fails, what else breaks? Trace the blast radius.

5. OVER-ENGINEERING — what's more complex than it needs to be? What could be
   simpler, cheaper, or deleted entirely?

6. WRONG ABSTRACTIONS — what's modeled wrong? Wrong boundaries? Wrong ownership?

For each finding, provide a CONCRETE counter-example or scenario where it breaks.
Be specific — 'this could fail' is useless. 'This fails when X happens because Y'
is useful. Score each finding: CRITICAL (would cause the plan to fail), HIGH
(significant rework needed), MEDIUM (should be addressed), LOW (nice to fix).\
"""


def run_skeptic(
    plan_text: str = "",
    goal: str = "",
    context: str = "",
    provider: str = "auto",
    model: str = "",
    timeout: int = 300,
    frame: str = "",
    effort: str = "",
) -> dict:
    """Run the skeptic/red-team review.

    Args:
        plan_text: The plan to critique
        goal: Goal (if no plan_text)
        context: Additional context
        provider: LLM provider
        model: Model override
        timeout: Timeout
        frame: Custom system frame (from SwarmType.frame). If empty, uses SKEPTIC_FRAME.

    Returns:
        Skeptic report dict
    """
    print(f"⚡ ECHELON SWARM — skeptic")

    ctx = SwarmContext("brainstorm")
    frozen = ctx.frozen_block()
    print(f"   frozen block: {len(frozen)} chars (hash={ctx.frozen_hash})")

    parts = [frame or SKEPTIC_FRAME]
    if plan_text:
        parts.append(f"## PLAN TO BREAK\n{plan_text[:12000]}")
    if goal:
        parts.append(f"## GOAL\n{goal}")
    if context:
        parts.append(f"## CONTEXT\n{context}")
    dynamic = "\n\n".join(parts)
    full_prompt = frozen + "\n\n" + dynamic

    try:
        raw = _send(full_prompt, provider, model, timeout, effort)
        print(f"   [✓] skeptic ({len(raw)} chars)")
        return {
            "goal": goal or "plan review",
            "ok": True,
            "content": raw,
            "swarm_follow_up": [
                "Review the skeptic findings. Fix CRITICAL issues before building.",
                "Re-run swarm --plan with the fixes applied.",
            ],
        }
    except Exception as e:
        print(f"   [✗] skeptic failed: {e}")
        return {"goal": goal or "plan review", "ok": False, "error": str(e)}


# ── Provider dispatch ─────────────────────────────────────────────────────────

def _send(prompt: str, provider: str, model: str, timeout: int,
          effort: str = "") -> str:
    """Send a prompt to an LLM provider. Routes through the shared swarm dispatcher."""
    from .dispatch import send
    return send(prompt, provider=provider, model=model, timeout=timeout,
                effort=effort)
