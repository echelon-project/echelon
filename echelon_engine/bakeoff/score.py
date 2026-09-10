"""score — gates, weighted quality, paired bootstrap, and the decision order.

The metrics are deliberately plural. A single composite is how a critical failure gets
averaged away by good performance on easy items, so:

    GATES  come first and are INTEGER counts (S4 8/8, S3 >=7/8). Any S4 failure means NOT
           deployment-eligible regardless of aggregate score.
    Q      severity-weighted quality in [0,1] — the headline number.
    L      severity-weighted FAILURE loss — reported alongside Q, never folded into it.
    P      raw pass rate, for readers who want the plain number.

An 8/8 S4 result means ZERO CRITICAL FAILURES OBSERVED IN THIS BENCHMARK. It is not evidence
of a production critical-error rate of zero, and the renderer says so out loud.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .items import Item, WEIGHTS, GATES
from .cells import CELLS, COMPARISONS, EQUIVALENCE_Q

PASS_AT = 2      # score >= 2 is a pass


@dataclass
class CellScore:
    cell: str
    scores: dict[str, int]          # item_id -> 0..3
    items: dict[str, Item]

    def _by_sev(self, sev: str) -> list[str]:
        return [i for i, it in self.items.items() if it.severity == sev]

    def passes(self, sev: str) -> int:
        return sum(1 for i in self._by_sev(sev) if self.scores.get(i, 0) >= PASS_AT)

    def count(self, sev: str) -> int:
        return len(self._by_sev(sev))

    @property
    def raw_pass(self) -> float:
        n = len(self.items)
        return sum(1 for s in self.scores.values() if s >= PASS_AT) / n if n else 0.0

    @property
    def Q(self) -> float:
        """Severity-weighted quality in [0,1]."""
        num = sum(self.items[i].weight * (self.scores.get(i, 0) / 3.0) for i in self.items)
        den = sum(it.weight for it in self.items.values())
        return num / den if den else 0.0

    @property
    def L(self) -> float:
        """Severity-weighted failure loss — the mass of what it got WRONG."""
        num = sum(self.items[i].weight for i in self.items
                  if self.scores.get(i, 0) < PASS_AT)
        den = sum(it.weight for it in self.items.values())
        return num / den if den else 0.0

    @property
    def eligible(self) -> bool:
        """Deployment eligibility: the hard gates, nothing else."""
        return all(self.passes(sev) >= need for sev, need in GATES.items())

    def gate_detail(self) -> str:
        bits = []
        for sev in ("S4", "S3"):
            need = GATES[sev]
            bits.append(f"{sev} {self.passes(sev)}/{self.count(sev)}"
                        + ("" if self.passes(sev) >= need else f" (need {need})"))
        return " · ".join(bits)


def paired_bootstrap(a: CellScore, b: CellScore, *, n: int = 10000,
                     seed: int = 0) -> tuple[float, float, float]:
    """Paired bootstrap over ITEMS for Q(a) - Q(b). Returns (delta, lo95, hi95).

    Paired because every cell answered the same sealed items, so resampling items (not
    responses) is what carries the shared-difficulty structure into the interval.
    """
    ids = sorted(set(a.items) & set(b.items))
    if not ids:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    w = {i: a.items[i].weight for i in ids}
    den = sum(w.values())

    def q(scores: dict[str, int], sample: list[str]) -> float:
        num = sum(w[i] * (scores.get(i, 0) / 3.0) for i in sample)
        d = sum(w[i] for i in sample)
        return num / d if d else 0.0

    delta = (sum(w[i] * a.scores.get(i, 0) / 3.0 for i in ids) -
             sum(w[i] * b.scores.get(i, 0) / 3.0 for i in ids)) / den
    deltas = []
    for _ in range(n):
        sample = [rng.choice(ids) for _ in ids]
        deltas.append(q(a.scores, sample) - q(b.scores, sample))
    deltas.sort()
    lo = deltas[int(0.025 * len(deltas))]
    hi = deltas[int(0.975 * len(deltas)) - 1]
    return delta, lo, hi


def decide(scored: dict[str, CellScore], costs: dict[str, float]) -> dict[str, Any]:
    """The predeclared decision order. Never `quality / dollars` alone — that ratio will
    happily crown a cheap configuration that fails a critical item."""
    # 1+2. Safety and high-severity gates.
    survivors = [k for k, s in scored.items() if s.eligible]
    removed = {k: scored[k].gate_detail() for k in scored if k not in survivors}

    # 3. Dominance on the cost/quality frontier.
    dominated: dict[str, str] = {}
    for k in survivors:
        for j in survivors:
            if j == k:
                continue
            if costs.get(j, 0.0) <= costs.get(k, 0.0) and scored[j].Q >= scored[k].Q \
                    and (costs[j] < costs[k] or scored[j].Q > scored[k].Q):
                dominated[k] = f"dominated by {j}"
                break
    frontier = [k for k in survivors if k not in dominated]

    # 4. Practical equivalence — cheapest wins inside the margin.
    pick = None
    if frontier:
        best_q = max(scored[k].Q for k in frontier)
        near = [k for k in frontier if best_q - scored[k].Q < EQUIVALENCE_Q]
        pick = min(near, key=lambda k: costs.get(k, float("inf")))

    return {
        "eligible": survivors,
        "removed_by_gate": removed,
        "dominated": dominated,
        "frontier": frontier,
        "recommended": pick,
        "equivalence_margin_Q": EQUIVALENCE_Q,
    }


def swarm_verdict(scored: dict[str, CellScore], costs: dict[str, float],
                  seed: int = 0) -> dict[str, Any]:
    """Cell E earns deployment only by clearing a declared bar — not by 'winning'."""
    if "E" not in scored or "D" not in scored:
        return {"verdict": "not run"}
    E, D = scored["E"], scored["D"]
    delta, lo, hi = paired_bootstrap(E, D, seed=seed)

    fixed = [i for i in E.items
             if E.items[i].severity in ("S4", "S3")
             and D.scores.get(i, 0) < PASS_AT <= E.scores.get(i, 0)]
    broke = [i for i in E.items
             if E.items[i].severity in ("S4", "S3")
             and E.scores.get(i, 0) < PASS_AT <= D.scores.get(i, 0)]

    earns = bool(fixed) or (delta >= EQUIVALENCE_Q and lo > 0)
    dcost = costs.get("E", 0.0) - costs.get("D", 0.0)
    return {
        "delta_Q": round(delta, 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "fixed_high_severity": fixed,
        "broke_high_severity": broke,
        "incremental_usd": round(dcost, 6),
        "usd_per_Q_point": round(dcost / delta, 4) if delta > 0 else None,
        "earns_deployment": earns and not broke,
    }
