"""cells — the five candidate systems under test.

Five cells, THREE causal comparisons. The cells are chosen so that each predeclared
comparison changes exactly one thing:

    B - A   effort  low  -> high   (v4-flash held constant)
    C - B   effort  high -> max    (v4-flash held constant)
    D - B   model   flash -> pro   (effective effort held at high)
    E - D   shape   one strong run -> 3 cheap workers + strong arbiter

There is no `medium` or `xhigh` cell ON PURPOSE: DeepSeek collapses both to `high`, so such a
cell would re-run B under a different label and report the noise between them as an effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Cell:
    key: str
    model: str
    effort: str
    calls_per_item: int
    label: str
    purpose: str
    # swarm-only
    workers: int = 0
    worker_model: str = ""
    worker_effort: str = ""
    arbiter_model: str = ""
    arbiter_effort: str = ""

    @property
    def is_swarm(self) -> bool:
        return self.workers > 0


CELLS: dict[str, Cell] = {
    "A": Cell("A", "deepseek-v4-flash", "low", 1,
              "Flash-low", "cheapest reasoning baseline"),
    "B": Cell("B", "deepseek-v4-flash", "high", 1,
              "Flash-high", "normal Flash baseline (the reference cell)"),
    "C": Cell("C", "deepseek-v4-flash", "max", 1,
              "Flash-max", "isolates extra test-time reasoning"),
    "D": Cell("D", "deepseek-v4-pro", "high", 1,
              "Pro-high", "strong single-run comparator"),
    "E": Cell("E", "", "mixed", 4,
              "Swarm", "3 independent cheap workers -> one strong arbiter",
              workers=3,
              worker_model="deepseek-v4-flash", worker_effort="low",
              arbiter_model="deepseek-v4-pro", arbiter_effort="high"),
}

# Predeclared comparisons. Declaring them here — before any run — is what stops the report
# from fishing through all ten cell pairs and presenting only the flattering ones.
COMPARISONS = [
    ("B", "A", "effort: low -> high (Flash)"),
    ("C", "B", "effort: high -> max (Flash)"),
    ("D", "B", "model: Flash -> Pro at high"),
    ("E", "D", "shape: swarm -> Pro-high single"),
]

# Practical-equivalence margin on weighted quality Q. Declared up front: if two survivors
# differ by less than this and neither has a severity advantage, the CHEAPER one wins.
EQUIVALENCE_Q = 0.02


def arbiter_brief(task: str, candidates: list[str]) -> str:
    """The arbiter prompt for cell E.

    Deliberately blind: no model names, no effort labels, no cell ids, no ordering signal
    (the caller shuffles). And it is NOT a vote — asking for a majority would cap the swarm at
    its workers' best answer, when the thing being tested is whether a strong reasoner given
    cheap drafts can exceed all of them.
    """
    blocks = "\n\n".join(
        f"--- CANDIDATE {i + 1} ---\n{c}" for i, c in enumerate(candidates))
    return (
        "You are given a task and several independent draft answers to it.\n\n"
        "Produce the best possible FINAL answer to the task. Do not vote, do not pick a "
        "favourite, and do not describe the drafts. Use them only as evidence: adopt what is "
        "correct, correct what is wrong, and supply what they all missed. If every draft is "
        "wrong, answer the task yourself and ignore them.\n\n"
        "Output only the final answer.\n\n"
        f"=== TASK ===\n{task}\n\n=== DRAFTS ===\n{blocks}\n"
    )
