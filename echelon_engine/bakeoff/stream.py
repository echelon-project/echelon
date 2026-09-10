"""stream — sequential, information-gated benchmarking. Spend a call only where it can
still change the answer.

WHY THIS EXISTS. Two full sweeps were run at 224 calls each. Measured afterwards:
**140/140 scored answers came from items where every cell agreed** — 23 all-pass ceilings and
5 grader-bugs. Exactly ONE item of 28 separated any cell, and that one was noise. The sweep
design pays the same 5 calls for an item that discriminates and an item that cannot, so its
cost is set by the SET SIZE and its information by the handful of hard items inside it. That
is a fine-tuning shape (dense, exhaustive, repeatable) being used for FACT-FINDING.

THE FIX — three gates, each killing a distinct kind of waste:

  1. SCREEN BEFORE YOU SPREAD. Run the cheapest cell (A) alone first. If A passes an item,
     no more expensive cell can *lose* to it in a way that changes a tier assignment — the
     ladder question is "what is the CHEAPEST sufficient rung", and A already answered it.
     Screening drops the 23 all-pass ceilings after ONE call instead of five.
     (Note the asymmetry this exploits: a ceiling item is uninformative about ordering, but
     an item A FAILS is exactly where the ladder has a rung. Screening keeps those.)

  2. LADDER-CLIMB, NOT FAN-OUT. For items A failed, walk A→B→C→D→E and STOP at the first
     rung that passes. The tier map only ever needed the cheapest sufficient cell; running
     D and E after B already passed buys nothing but cost.

  3. SEQUENTIAL STOPPING ON THE COMPARISON. For each predeclared comparison, maintain a
     running paired difference and stop that comparison once its CI excludes the equivalence
     margin (a decision is reached) or once the remaining items cannot flip it (futility).
     A comparison that is settled at item 9 should not consume items 10-28.

WHAT THIS DELIBERATELY DOES NOT DO. It does not adaptively pick items by difficulty — that
would let the harness choose the questions that flatter a conclusion. Item ORDER is fixed by
the seal and shuffled once by seed; only the DEPTH of spend per item adapts.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from .cells import CELLS, COMPARISONS, EQUIVALENCE_Q, Cell
from .items import Item
from .graders import grade

LADDER = ["A", "B", "C", "D", "E"]
PASS_AT = 2


@dataclass
class Ledger:
    """Running record. Every field here is what makes an early stop DEFENSIBLE — a stop
    without the futility/decision reason recorded is indistinguishable from giving up."""
    scores: dict[str, dict[str, int]] = field(default_factory=dict)   # cell -> item -> score
    calls: int = 0
    screened_out: list[str] = field(default_factory=list)
    climbed: dict[str, str] = field(default_factory=dict)             # item -> stopping rung
    unserved: list[str] = field(default_factory=list)
    stopped: dict[str, str] = field(default_factory=dict)             # comparison -> reason
    tokens_in: int = 0
    tokens_out: int = 0

    def put(self, cell: str, item_id: str, score: int) -> None:
        self.scores.setdefault(cell, {})[item_id] = score


def _paired_delta(a: dict[str, int], b: dict[str, int], w: dict[str, int]) -> float:
    """Weighted paired Q difference over the items BOTH cells have answered so far."""
    ids = [i for i in a if i in b]
    den = sum(w[i] for i in ids)
    if not den:
        return 0.0
    return sum(w[i] * (a[i] - b[i]) / 3.0 for i in ids) / den


def _bootstrap_ci(a: dict[str, int], b: dict[str, int], w: dict[str, int],
                  *, n: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    ids = [i for i in a if i in b]
    if len(ids) < 3:
        return 0.0, -1.0, 1.0        # too little evidence to bound anything
    rng = random.Random(seed)
    obs = _paired_delta(a, b, w)
    ds = []
    for _ in range(n):
        s = [rng.choice(ids) for _ in ids]
        den = sum(w[i] for i in s)
        ds.append(sum(w[i] * (a[i] - b[i]) / 3.0 for i in s) / den if den else 0.0)
    ds.sort()
    return obs, ds[int(0.025 * len(ds))], ds[int(0.975 * len(ds)) - 1]


def comparison_state(led: Ledger, hi: str, lo: str, w: dict[str, int],
                     remaining: int, *, seed: int = 0) -> tuple[str, str]:
    """(state, reason) for one predeclared comparison. state ∈ open|decided|equivalent|futile."""
    a = led.scores.get(hi, {})
    b = led.scores.get(lo, {})
    common = [i for i in a if i in b]
    if len(common) < 5:
        return "open", f"only {len(common)} paired item(s)"

    obs, lo95, hi95 = _bootstrap_ci(a, b, w, seed=seed)
    if lo95 > EQUIVALENCE_Q:
        return "decided", f"ΔQ {obs:+.3f}, CI [{lo95:+.3f},{hi95:+.3f}] > margin"
    if hi95 < -EQUIVALENCE_Q:
        return "decided", f"ΔQ {obs:+.3f}, CI [{lo95:+.3f},{hi95:+.3f}] < -margin"
    if -EQUIVALENCE_Q < lo95 and hi95 < EQUIVALENCE_Q:
        return "equivalent", f"CI [{lo95:+.3f},{hi95:+.3f}] inside ±{EQUIVALENCE_Q}"

    # FUTILITY: even if every remaining item swung maximally, could the verdict move?
    den = sum(w.values())
    max_swing = sum(sorted(w.values(), reverse=True)[:remaining]) / den if remaining else 0.0
    if abs(obs) + max_swing < EQUIVALENCE_Q:
        return "futile", (f"|ΔQ| {abs(obs):.3f} + max remaining swing {max_swing:.3f} "
                          f"< margin {EQUIVALENCE_Q}")
    return "open", f"ΔQ {obs:+.3f}, CI [{lo95:+.3f},{hi95:+.3f}]"


def run_stream(items: list[Item], *, provider=None, seed: int = 0, timeout: int = 300,
               screen_cell: str = "A", ladder: list[str] | None = None,
               on_event: Callable[[str, dict], None] | None = None,
               max_calls: int | None = None) -> Ledger:
    """Sequential run. Returns the ledger; the caller renders/scores from it."""
    from .run import _send

    if provider is None:
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider()

    rungs = ladder or LADDER
    order = list(items)
    random.Random(seed).shuffle(order)          # fixed by seed, not chosen by difficulty
    w = {i.id: i.weight for i in order}
    led = Ledger()

    def emit(kind, **d):
        if on_event:
            on_event(kind, d)

    for idx, item in enumerate(order):
        remaining = len(order) - idx - 1

        # ── GATE 1: screen with the cheapest rung ────────────────────────────
        cell = CELLS[screen_cell]
        rec = _send(provider, cell.model, cell.effort, item.prompt,
                    item.id, screen_cell, "candidate", timeout)
        led.calls += 1
        led.tokens_in += rec.tokens_in
        led.tokens_out += rec.tokens_out
        s, why = grade(item, rec.content)
        if s < 0:
            s = -1                              # rubric: unresolved without judges
        led.put(screen_cell, item.id, max(s, 0))

        if s >= PASS_AT:
            led.screened_out.append(item.id)
            led.climbed[item.id] = screen_cell
            emit("screen", item=item.id, sev=item.severity, cell=screen_cell,
                 score=s, verdict="SUFFICIENT — cheapest rung holds, no fan-out")
            continue

        emit("screen", item=item.id, sev=item.severity, cell=screen_cell,
             score=max(s, 0), verdict="climb")

        # ── GATE 2: climb the ladder, stop at the first sufficient rung ──────
        landed = None
        for k in rungs[1:]:
            c = CELLS[k]
            if c.is_swarm:
                from .run import run_cell
                r = run_cell(provider, c, item, rng=random.Random(f"{seed}:{item.id}"),
                             timeout=timeout)
                led.calls += len(r.calls)
                led.tokens_in += sum(x.tokens_in for x in r.calls)
                led.tokens_out += sum(x.tokens_out for x in r.calls)
                content = r.answer
            else:
                rec = _send(provider, c.model, c.effort, item.prompt,
                            item.id, k, "candidate", timeout)
                led.calls += 1
                led.tokens_in += rec.tokens_in
                led.tokens_out += rec.tokens_out
                content = rec.content
            s2, _ = grade(item, content)
            led.put(k, item.id, max(s2, 0))
            emit("climb", item=item.id, cell=k, score=max(s2, 0))
            if s2 >= PASS_AT:
                landed = k
                break

        if landed:
            led.climbed[item.id] = landed
        else:
            led.unserved.append(item.id)

        # ── GATE 3: retire settled comparisons ──────────────────────────────
        for hi, lo, label in COMPARISONS:
            key = f"{hi}-{lo}"
            if key in led.stopped:
                continue
            state, reason = comparison_state(led, hi, lo, w, remaining, seed=seed)
            if state in ("decided", "equivalent", "futile"):
                led.stopped[key] = f"{state}: {reason}"
                emit("stop", comparison=key, state=state, reason=reason, label=label)

        if max_calls and led.calls >= max_calls:
            emit("budget", calls=led.calls, cap=max_calls)
            break
        if len(led.stopped) == len(COMPARISONS) and not led.unserved:
            emit("halt", reason="every predeclared comparison is settled")
            break

    return led


def render(led: Ledger, items: list[Item], baseline_calls: int) -> str:
    by = {i.id: i for i in items}
    lines = ["STREAMING RESULT — spend follows information, not set size", ""]
    lines.append(f"  calls spent        : {led.calls}")
    lines.append(f"  full-sweep baseline: {baseline_calls}")
    saved = baseline_calls - led.calls
    if baseline_calls:
        lines.append(f"  saved              : {saved} ({saved / baseline_calls:.0%})")
    lines.append(f"  tokens             : in {led.tokens_in:,}  out {led.tokens_out:,}")
    lines.append("")
    lines.append(f"  screened out at the cheapest rung : {len(led.screened_out)} item(s)")
    lines.append(f"  required a climb                  : "
                 f"{len(led.climbed) - len(led.screened_out)} item(s)")
    lines.append(f"  UNSERVED (no rung sufficed)       : {len(led.unserved)}")
    if led.unserved:
        for i in led.unserved:
            lines.append(f"      {i} [{by[i].severity}] — a real ceiling OR a grader bug")
    lines.append("")
    lines.append("  TIER ASSIGNMENT (cheapest sufficient rung per item):")
    tally: dict[str, list[str]] = {}
    for iid, k in led.climbed.items():
        tally.setdefault(k, []).append(iid)
    for k in LADDER:
        got = tally.get(k)
        if got:
            sevs = {}
            for i in got:
                sevs[by[i].severity] = sevs.get(by[i].severity, 0) + 1
            lines.append(f"    {k} {CELLS[k].label:<12} {len(got):>3} item(s)  {sevs}")
    lines.append("")
    lines.append("  COMPARISONS RETIRED EARLY:")
    if not led.stopped:
        lines.append("    (none reached a stopping rule)")
    for k, why in led.stopped.items():
        lines.append(f"    {k}  {why}")
    return "\n".join(lines)
