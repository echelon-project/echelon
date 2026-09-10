"""The TRANSIENT step wrapper — ECHELON's <system-reminder>.

Extracted from loop.py to keep the loop module slim.
"""
from __future__ import annotations


class _StepReminder:
    """The TRANSIENT step wrapper — ECHELON's <system-reminder> (owner 2026-06-07: "wrapper is
    good... could become easy template also for step categoriser").

    THE ANOMALY IT FIXES: the per-step ephemeral signals (TODO, warmth, guard nudges) were each
    appended as a PERMANENT new user turn every step and never pruned — after N steps the trunk
    held N stale copies the model re-reads every turn (fat trunk, narrowed vision; see
    not-stingy-on-steps-offload-slim). The mess was the STEP-BY-STEP output, not the final answer.

    THE FIX: these signals are not history — they are the CURRENT state of the step. So they live
    in ONE typed block, rebuilt fresh each step and rendered as a SINGLE transient message that is
    appended right before the send and popped right after (it never persists in `messages`). Only
    the current step's wrapper is ever in context — exactly the Claude Code system-reminder model.

    TYPED SLOTS = the step template / categoriser: todo (the live plan), warmth (this step's
    temperature), guard (the one active nudge, if any), category (what this step IS). The same
    block that slims context also declares the step's type — the categoriser's home.
    """
    __slots__ = ("todo", "warmth", "guards", "category")

    def __init__(self) -> None:
        self.todo: str = ""
        self.warmth: str = ""
        self.guards: list[str] = []   # active nudges THIS step (progress/convergence/deadlock/...)
        self.category: str = ""       # advancing | circling | thrash | synthesizing | ...

    def add_guard(self, text: str) -> None:
        if text:
            self.guards.append(text.strip())

    def empty(self) -> bool:
        return not (self.todo or self.warmth or self.guards or self.category)

    def render(self) -> str:
        """One transient <step-context> block. Labeled slots; only non-empty ones shown."""
        lines = ["<step-context> (transient — current state of THIS step, not history)"]
        if self.category:
            lines.append(f"category: {self.category}")
        if self.todo:
            lines.append("todo:\n" + self.todo)
        if self.warmth:
            lines.append("warmth: " + self.warmth)
        for g in self.guards:
            lines.append("guard: " + g)
        lines.append("</step-context>")
        return "\n".join(lines)
