"""judges — blind rubric grading for items no program can score.

USED ONLY where determinism is impossible (register, decision hierarchy, unsupported claims).
Everything else goes to graders.py, because a judge adds variance to the very instrument
measuring variance.

The independence rules, all enforced here rather than described:
  * BLIND      — the judge sees the task, the rubric, and an answer. Never a model name,
                 effort label, cell id, cost, latency, or worker/arbiter metadata.
  * SHUFFLED   — answers are presented in random order per item, so position carries nothing.
  * TWO JUDGES — at least one OUTSIDE the candidate family. Candidates are all DeepSeek, so
                 the pair is Gemini + Claude. A DeepSeek judge scoring DeepSeek answers shares
                 the blind spot that produced them.
  * FROZEN     — the same rubric text for every cell, built once per item.
  * ESCALATE   — a disagreement greater than one point is NOT averaged. Averaging manufactures
                 a confident middle from two incompatible readings; it is flagged for human
                 adjudication instead.
  * SEPARATE   — judge spend is EVALUATION OVERHEAD, never added to candidate inference cost,
                 or a cell looks expensive because of how it was graded.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .items import Item

# Judge roster: (label, model_id, provider-factory-name). Both outside the DeepSeek family.
JUDGES = [
    ("gemini", "gemini-2.5-flash", "gemini"),
    ("claude", "claude-sonnet-4-6", "anthropic"),
]

_RUBRIC_PROMPT = """You are grading one answer against a fixed rubric. You do not know who \
or what produced it.

Score 0-3 using EXACTLY these definitions:
  3 = all critical criteria met; no material unsupported claim
  2 = all load-bearing criteria met; minor omission or presentation defect only
  1 = some useful work, but at least one load-bearing criterion is wrong or missing
  0 = fundamentally wrong, unsafe, refused an answerable task, empty, or unusable

LOAD-BEARING CHECKPOINTS (all must hold for a score of 2 or more):
{checkpoints}

RUBRIC CRITERIA (observable, not impressions):
{rubric}

=== TASK ===
{task}

=== ANSWER ===
{answer}

Reply with ONLY a JSON object: {{"score": <0-3>, "reason": "<one sentence>"}}
"""


@dataclass
class JudgeVote:
    judge: str
    score: int
    reason: str
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""


@dataclass
class RubricResult:
    item_id: str
    cell: str
    votes: list[JudgeVote] = field(default_factory=list)

    @property
    def scores(self) -> list[int]:
        return [v.score for v in self.votes if v.error == ""]

    @property
    def needs_human(self) -> bool:
        """A spread greater than one point is a genuine disagreement, not noise to average."""
        s = self.scores
        return len(s) >= 2 and (max(s) - min(s)) > 1

    @property
    def score(self) -> int:
        s = self.scores
        if not s:
            return 0                      # ungraded is never silently a pass
        if self.needs_human:
            return min(s)                 # hold the conservative reading until adjudicated
        return round(sum(s) / len(s))


def _provider_for(kind: str):
    if kind == "gemini":
        from echelon_engine.atoms.providers.gemini import GeminiProvider
        return GeminiProvider()
    if kind == "anthropic":
        from echelon_engine.atoms.providers.anthropic_upstream import AnthropicUpstreamProvider
        import os
        from echelon_sdk.keys import load_anthropic_key
        return AnthropicUpstreamProvider(
            upstream_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            api_key=os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or load_anthropic_key())
    raise ValueError(f"unknown judge provider {kind!r}")


def _parse_vote(text: str) -> tuple[int, str]:
    m = re.search(r"\{.*?\}", text or "", re.S)
    if not m:
        return -1, f"unparseable judge reply: {(text or '')[:120]}"
    try:
        d = json.loads(m.group(0))
        s = int(d.get("score", -1))
        return (s if 0 <= s <= 3 else -1), str(d.get("reason", ""))[:200]
    except Exception as e:  # noqa: BLE001
        return -1, f"bad judge JSON: {e}"


def build_brief(item: Item, answer: str) -> str:
    """The frozen brief. Identical for every cell — only `answer` differs."""
    return _RUBRIC_PROMPT.format(
        checkpoints="\n".join(f"  - {c}" for c in item.checkpoints) or "  (none declared)",
        rubric="\n".join(f"  - {c}" for c in item.rubric) or "  (none declared)",
        task=item.prompt,
        answer=answer,
    )


def grade_rubric(item: Item, cell: str, answer: str, *,
                 judges=None, timeout: int = 180,
                 send: Callable | None = None) -> RubricResult:
    """Two blind judges on one answer. `send` is injectable so this is testable offline."""
    res = RubricResult(item_id=item.id, cell=cell)
    if not (answer or "").strip():
        res.votes.append(JudgeVote("n/a", 0, "empty answer"))
        return res

    brief = build_brief(item, answer)
    for label, model, kind in (judges or JUDGES):
        try:
            if send is not None:
                text, ti, to = send(kind, model, brief)
            else:
                prov = _provider_for(kind)
                resp = prov.send([{"role": "user", "content": brief}],
                                 model_id=model, timeout=timeout, temperature=0)
                text = getattr(resp, "content", "") or ""
                ti = getattr(resp, "tokens_in", 0) or 0
                to = getattr(resp, "tokens_out", 0) or 0
            score, reason = _parse_vote(text)
            if score < 0:
                res.votes.append(JudgeVote(label, 0, reason, ti, to, error=reason))
            else:
                res.votes.append(JudgeVote(label, score, reason, ti, to))
        except Exception as e:  # noqa: BLE001
            res.votes.append(JudgeVote(label, 0, "", error=f"{type(e).__name__}: {e}"))
    return res


def blind_pack(item: Item, answers: dict[str, str], *, seed: int = 0) -> list[tuple[str, str]]:
    """Shuffle one item's cell answers into an identity-stripped pack.

    Returned as (cell, answer) so the caller can re-join after scoring — the JUDGE never
    receives the cell; only the caller holds the mapping.
    """
    pairs = [(c, a) for c, a in answers.items() if (a or "").strip()]
    random.Random(f"{seed}:{item.id}").shuffle(pairs)
    return pairs
