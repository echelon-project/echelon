"""Convergence — the SUFFICIENCY signal (owner 2026-06-06: "count the cold warm and emotions").

The drift guard measures being LOST (thrash). The progress guard measures CIRCLING. Neither measures
"I have ENOUGH — synthesize now." That gap let the Epsilon-Co audit research to the step cap and never
land a verdict: it stayed cold/curiosity the whole run because memory-warmth measures CROSS-SESSION
familiarity (empty for a new scope) — not WITHIN-RUN task-understanding.

This is the missing twin: a SELF-WARMTH on the run's own accumulating knowledge. Each step's new
information is scored against everything the run has already learned. When new steps stop surfacing
NOVELTY (the agent keeps reading but learns little new), curiosity is SPENT -> that IS confidence ->
the trigger to stop gathering and JUDGE. The cold/curiosity -> warm/confidence transition
([[warmth-is-emotional]]) made into a convergence signal. $0, lexical, no model call — same machinery
as the warmth floor, turned inward (reasoning vs the run's own accumulation, not vs stored seeds).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or but to of in on at for with by is are was were be been being this that these "
    "those it its as do did does done from not no yes will would can could should have has had if "
    "then else when what which who how why where file files read def class import return self none "
    "true false str int dict list".split()
)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


# TASK-CLASS CLASSIFIER (owner 2026-06-07: the convergence gate is a JUDGE signal applied to BUILD
# tasks). Saturation — "novelty decayed, I've stopped learning" — is the FINISH signal for a verdict
# task (audit/rank/decide: read until you stop learning, then rule). But for a BUILD task it is the
# START-WRITING signal: an implementer re-reads files it already knows to make surgical edits, so low
# novelty means "ready to write," not "ready to conclude." Locking gather then deadlocks the build
# (it can't read, can't recall the offloaded content, and `finish` means a verdict that doesn't exist).
# Same insight as CV-012: judge-tier and build-tier are different; collapsing them corrupts the gate.
_BUILD_VERBS = frozenset(
    "add build implement create write make fix patch edit refactor scaffold wire update change "
    "modify rename move delete remove install setup configure integrate hook port migrate generate "
    "extend rework rewrite append insert replace inject enhance improve render draw enable disable "
    "support handle emit display ship bind connect".split()
)
_JUDGE_VERBS = frozenset(
    "audit review rank assess evaluate analyze analyse compare investigate diagnose decide judge "
    "research explore find locate identify determine summarize summarise explain understand verify "
    "check inspect trace report recommend".split()
)


def classify_task(goal: str) -> str:
    """Infer whether a goal is a BUILD (produces/mutates artifacts) or a JUDGE (produces a verdict)
    task, from its verbs. Lexical, $0 — same machinery as the convergence floor. Returns "build" |
    "judge" | "auto" (unknown). The gate uses this to decide whether saturation should HARD-LOCK
    gathering (correct for judge) or merely NUDGE toward writing (correct for build)."""
    toks = _tokens(goal)
    build_hits = len(toks & _BUILD_VERBS)
    judge_hits = len(toks & _JUDGE_VERBS)
    if build_hits > judge_hits:
        return "build"
    if judge_hits > build_hits:
        return "judge"
    return "auto"   # ambiguous / no clear verb — caller decides the default posture


@dataclass
class Convergence:
    """Tracks how much NEW information each step adds vs the run's accumulated knowledge."""
    known: set[str] = field(default_factory=set)     # every meaning-bearing token seen so far
    novelty_history: list[float] = field(default_factory=list)  # per-step fraction-new
    steps: int = 0

    def observe(self, text: str) -> float:
        """Fold a step's content in; return its NOVELTY = fraction of its tokens that are NEW.
        High novelty = learning new ground (curiosity justified). Low = re-treading (saturation)."""
        toks = _tokens(text)
        self.steps += 1
        if not toks:
            novelty = 0.0
        elif not self.known:
            novelty = 1.0   # first substantive step is all new
        else:
            new = toks - self.known
            novelty = len(new) / len(toks)
        self.known |= toks
        self.novelty_history.append(round(novelty, 3))
        return novelty

    def saturated(self, *, window: int = 4, threshold: float = 0.12, min_steps: int = 8) -> bool:
        """Has curiosity decayed into confidence? True when the recent window's average novelty is
        below threshold AND we've gathered enough to have a basis. The felt 'I've seen enough'.
        window=4: smooth over a few steps so one rich read doesn't reset it. min_steps: don't fire
        before there's a real basis (a verdict needs evidence, not just low early novelty)."""
        if self.steps < min_steps or len(self.novelty_history) < window:
            return False
        recent = self.novelty_history[-window:]
        return (sum(recent) / len(recent)) < threshold

    def state(self) -> dict:
        recent = self.novelty_history[-4:] if self.novelty_history else [1.0]
        avg = round(sum(recent) / len(recent), 3)
        return {"steps": self.steps, "known_tokens": len(self.known),
                "recent_novelty": avg,
                "feeling": "confidence" if self.saturated() else "curiosity"}
