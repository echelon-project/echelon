"""context_arm — the third axis: does COMPOSED CONTEXT substitute for a more expensive rung?

THE QUESTION THIS ANSWERS, and why it is the same experiment rather than a new one.

The tier ladder assumes quality is bought with MODEL and EFFORT. The flow-of-context thesis
says a third axis dominates both: a cheap model fed the right 15 atoms may beat an expensive
one fed nothing. If true, the router's first move should be COMPOSE, not ESCALATE — escalation
is priced per token, composition is priced at zero.

So for every item where the ladder had to CLIMB (cheapest rung failed, a dearer rung passed),
re-run the CHEAPEST rung with composed context. Three outcomes, all informative:

  SUBSTITUTES  cheap+context passes -> context bought what escalation was going to buy, for
               free. This is the governor's whole case, measured per item.
  INSUFFICIENT cheap+context still fails -> the gap was capability, not context. Escalation
               is genuinely required here; the router should climb.
  HARMS        cheap+context fails where cheap-alone had partially succeeded -> SALIENCE
               DILUTION, the failure mode the thesis itself names ("a step drowned in 200
               atoms gates worse than one fed the right 15"). This is the arm that keeps the
               experiment honest: a governor study that cannot detect its own harm is
               advocacy, not research.

DESIGN NOTES THAT MATTER:

* Only CLIMB items are re-run. An item the cheap rung already passed cannot demonstrate
  substitution — it has nothing to substitute for. This is what keeps the arm cheap: it rides
  the streaming run's own selection instead of paying for a second full sweep.

* Context is composed by the SAME recall the engine uses, keyed on the item prompt, so what
  is measured is the real substrate rather than a hand-picked ideal context. A study that
  hand-tunes context per item measures the tuner.

* BUDGET IS A TREATMENT, not a constant. The thesis predicts an interior optimum (too few
  atoms starves, too many dilutes), so budget is swept. A single budget would find a point,
  not a curve, and the governor needs the curve to pick a default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .cells import CELLS
from .items import Item
from .graders import grade

# Atom budgets to sweep. The thesis predicts an interior optimum — if quality rises then falls,
# the peak IS the governor's default budget. A flat curve refutes the composition case.
BUDGETS = (0, 5, 15, 40)

# GRAIN sweep, in chars per atom. Measured on the live bank: atom bodies run 2k-13k chars, so
# grain is NOT a formatting detail — at full grain, 5 atoms is ~49k chars and the composed
# context dwarfs the task. The governor has TWO knobs (how many, how much of each) and the
# experiment must sweep both or it will attribute a grain effect to a budget effect.
GRAINS = (220, 1200)

PASS_AT = 2


@dataclass
class ContextTrial:
    item_id: str
    budget: int
    score: int
    atoms_used: int
    tokens_in: int
    tokens_out: int
    grain: int = 0
    verdict: str = ""


@dataclass
class ContextResult:
    trials: list[ContextTrial] = field(default_factory=list)
    substitutes: list[str] = field(default_factory=list)
    insufficient: list[str] = field(default_factory=list)
    harms: list[str] = field(default_factory=list)
    calls: int = 0

    def best_budget(self) -> dict[int, int]:
        """budget -> how many climb-items it rescued at the cheapest rung."""
        out: dict[int, int] = {}
        for t in self.trials:
            if t.score >= PASS_AT:
                out[t.budget] = out.get(t.budget, 0) + 1
        return out


def compose_context(prompt: str, scope: str, budget: int,
                    grain: int = 220) -> tuple[str, int]:
    """Compose per-step context the way the engine would. Returns (text, n_atoms).

    Uses the live recall path, NOT a curated set: the thing under test is whether the real
    substrate can carry a cheap model, and a hand-built context would measure the builder.
    """
    if budget <= 0:
        return "", 0
    try:
        from echelon_engine.atoms.store import SeedStore
        from echelon_engine.atoms.warmth import warmth
        store = SeedStore()
        # SIGNATURE MATTERS: warmth(reasoning, store, scope, top_k=...). `top_k` IS the
        # governor's budget knob — asking for 15 atoms is literally top_k=15, so the sweep
        # exercises the real retrieval depth rather than post-truncating a fixed pull.
        reading = warmth(prompt, store, scope, top_k=budget)
        seeds = getattr(reading, "warmest", None) or []
    except Exception:
        return "", 0

    picked = []
    for s in seeds[:budget]:
        seed = getattr(s, "seed", None)
        text = getattr(seed, "content", None) if seed is not None else None
        if text is None:
            text = s[1] if isinstance(s, (list, tuple)) and len(s) > 1 else str(s)
        t = " ".join(str(text).split())
        # GRAIN IS A SECOND TREATMENT, discovered by measuring: a full atom body runs 2k-13k
        # chars, so 5 atoms at full grain is ~49k chars — more dilution than the item itself
        # has signal. The estate's own boot banner pages ~110-char PREVIEWS, not bodies. So
        # "budget" alone is the wrong knob: the governor must choose HOW MANY and HOW MUCH.
        picked.append(t[:grain] + ("…" if len(t) > grain else ""))
    if not picked:
        return "", 0
    body = "\n".join(f"- {p}" for p in picked)
    return (f"RELEVANT PRIOR KNOWLEDGE (from your own memory; use what applies, "
            f"ignore what does not):\n{body}\n\n"), len(picked)


def run_context_arm(climb_items: list[Item], baseline: dict[str, int], *,
                    provider=None, scope: str = "echelon", cheapest: str = "A",
                    budgets=BUDGETS, grains=GRAINS, timeout: int = 300,
                    on_event: Callable[[str, dict], None] | None = None) -> ContextResult:
    """Re-run the CHEAPEST rung on climb-items, with composed context, across budgets.

    `baseline` is the cheap rung's score per item from the streaming run — needed to detect
    HARM, which is only visible against what the same rung scored without context.
    """
    from .run import _send

    if provider is None:
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider()

    cell = CELLS[cheapest]
    res = ContextResult()

    for item in climb_items:
        base = baseline.get(item.id, 0)
        rescued_at: int | None = None
        worst = base

        done = False
        for b in budgets:
            if b == 0 or done:
                continue                      # b=0 IS the baseline; do not pay for it twice
            for g in grains:
                ctx, n = compose_context(item.prompt, scope, b, grain=g)
                rec = _send(provider, cell.model, cell.effort, ctx + item.prompt,
                            item.id, cheapest, "candidate", timeout)
                res.calls += 1
                s, _why = grade(item, rec.content)
                s = max(s, 0)
                res.trials.append(ContextTrial(item.id, b, s, n,
                                               rec.tokens_in, rec.tokens_out, grain=g))
                worst = min(worst, s)
                if on_event:
                    on_event("ctx", {"item": item.id, "budget": b, "grain": g,
                                     "atoms": n, "score": s, "base": base,
                                     "ctx_chars": len(ctx)})
                if s >= PASS_AT and rescued_at is None:
                    rescued_at = b
                    done = True               # cheapest sufficient (budget, grain)
                    break

        if rescued_at is not None:
            res.substitutes.append(item.id)
        elif worst < base:
            res.harms.append(item.id)
        else:
            res.insufficient.append(item.id)

    return res


def render(res: ContextResult, n_climb: int) -> str:
    lines = ["CONTEXT ARM — does composition substitute for escalation?", ""]
    lines.append(f"  climb items tested : {n_climb}")
    lines.append(f"  extra calls        : {res.calls}")
    lines.append("")
    lines.append(f"  SUBSTITUTES  {len(res.substitutes):>3}  context replaced a dearer rung, "
                 f"at zero API premium")
    lines.append(f"  INSUFFICIENT {len(res.insufficient):>3}  gap was CAPABILITY — escalation "
                 f"genuinely required")
    lines.append(f"  HARMS        {len(res.harms):>3}  salience dilution — context made the "
                 f"cheap rung WORSE")
    if res.harms:
        lines.append(f"       {res.harms}")
    lines.append("")
    bb = res.best_budget()
    if bb:
        lines.append("  RESCUES BY BUDGET (the governor's default lives at the peak):")
        for b in sorted(bb):
            lines.append(f"    {b:>3} atoms -> {bb[b]} rescue(s)")
    else:
        lines.append("  no budget rescued any item — composition did not substitute here")
    lines.append("")
    lines.append("  GOVERNOR READING: SUBSTITUTES says compose-before-escalate; INSUFFICIENT "
                 "says\n  the ladder is real; HARMS is the dilution ceiling the policy must "
                 "respect.")
    return "\n".join(lines)
