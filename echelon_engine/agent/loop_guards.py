"""Drift/progress/convergence guard helpers — _ask_why_repeat and _judge_repeat_legit.

Extracted from loop.py to keep the loop module slim.
"""
from __future__ import annotations
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .loop_models import MemoryContext


def _ask_why_repeat(provider, messages, model_id, tc) -> tuple[str, int, int]:
    """A repeated action triggered the drift check. ASK the agent its reason (don't infer it),
    so warmth can judge whether the repetition is reasoning or hope. The reason is the agent's
    own words — a real reason (the world is non-stationary: a deploy rolling, a transient 5xx,
    eventual consistency) can be voiced; re-rolling on hope cannot say anything that runs warm.
    Returns (reason_text, tokens_in, tokens_out). See drift-guard-is-reasoning-not-steps."""
    ask = (
        f"You are about to repeat the same action — {tc.name}({json.dumps(tc.args)}) — that you "
        "already tried. Before you do: WHY would the result differ this time? State your reason "
        "in one or two sentences. If the world will change between attempts (a process is "
        "completing, a transient condition is clearing, you are pacing a retry), say so plainly. "
        "If nothing about the world will differ, say that instead."
    )
    probe = messages + [{"role": "user", "content": ask}]
    resp = provider.send(probe, model_id=model_id, tools=None)
    if resp.status == "success" and resp.content:
        return resp.content.strip(), resp.tokens_in, resp.tokens_out
    return "", (resp.tokens_in if resp.status == "success" else 0), \
              (resp.tokens_out if resp.status == "success" else 0)


def _judge_repeat_legit(failure_context: str, reason: str, memory: "MemoryContext",
                        model_id: str) -> tuple[bool, float, str, int, int]:
    """Is a repeated action LEGIT (the world is non-stationary, a different result is coherent)
    or HOPE (thrash)? Uses the FOCUSED non-stationarity judge — it TESTS whether THIS specific
    world will change, not whether the reason merely looks like the retry principle (the over-
    eager failure mode the general recognition-judge had: it allowed a hopeless zcat re-roll
    because the text 'looked like' WORLD-01). The principle-seed anchors the concept; the judge
    tests the instance. Returns (legit, score, why, tok_in, tok_out)."""
    jp = memory.judge_provider
    if jp is None:
        # No judge (no model loaded): conservative floor — without the heart we cannot test the
        # world, so an identical repeat is treated as hope. The compute-tiering floor case.
        return False, 0.0, "no judge to test non-stationarity (conservative: treat as hope)", 0, 0
    from echelon_engine.atoms.judge import judge_nonstationary
    payload = (f"FAILURE/STATE:\n{failure_context[:500]}\n\nAGENT'S STATED REASON TO REPEAT:\n{reason[:400]}")
    j = judge_nonstationary(payload, jp, model_id=model_id)
    if not j:
        return False, 0.0, "judge unavailable", 0, 0
    legit = bool(j.get("legit", False))
    score = float(j.get("score", 0.0))
    why = j.get("why", "")
    tok = j.get("_tokens", (0, 0))
    return legit, score, why, tok[0], tok[1]
