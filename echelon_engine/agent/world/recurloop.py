"""recurloop.py — AUTOLOOP WRAPPED BY AUTOLOOP, planned the MiroFish way.

The owner's shape (2026-06-18): "small goal. autoloop, wrapped by more autoloop" +
"use mirofish method to think the complete plan" + "have own tiering, depend on
circumstances" + "auto put" (depth decides itself).

So this is NOT a fixed two-level nesting. It is TIERED, SELF-DECIDING recursion that
uses board()'s shared append-only ledger as the PLANNING ORGAN:

  • A goal is sized by the bank's own warmth signal (the reflex-vs-think gate, applied to
    planning). The decision of HOW to attack it is TIERED, per circumstance:
        WARM/small  -> DISPATCH         (one equipped partner; reflex — no plan needed)
        LUKEWARM     -> SELF-DECOMPOSE   (a board() seeded with the goal; workers post sub-goals)
        COLD/big     -> PLAN-FIRST       (a planner partner posts the initial sub-goals, then
                                          workers drain + grow the board)
  • The plan GROWS ON THE LEDGER (MiroFish-as-planning): partners POST newly-discovered
    sub-goals onto the board; the field keeps draining until the board is empty or budget
    is spent. The plan is not pre-computed top-down — it accretes bottom-up as work lands.
  • RECURSION IS AUTOMATIC ("auto put"): a posted sub-goal that is itself big enough re-enters
    the SAME tiered decider — so a sub-goal can spawn its own child board. Bounded by a DEPTH
    cap + a NESTED BUDGET (a child gets a slice of the parent's remaining budget). Sufficiency:
    carry the least that lets it act — a warm leaf never spawns a board it doesn't need.

The outer driver is autoloop: it proposes a big frontier goal FROM THE BANK and hands it to
this recursive act, then earns from the whole sub-arc and proposes the next. So:
    autoloop (the cycle)  ×  recursive tiered board() (the growing plan)  =
    a self-driving swarm that grows its own plan on the ledger and earns from it.

Run with `python -X utf8`. Budget + depth bound everything; it halts with a report.

partner.Ledger, partner.dispatch, partner.board are lazily imported from the ported
echelon_engine.agent.partner. autoloop.propose_goal and autoloop._default_spend_reader
are from the sibling echelon_engine.agent.world.autoloop.
LAYERING-TENSION: memory.warmth, memory.store (atoms layer) are lazily imported inside
function bodies — not flagged by scanner. plan_cache is in echelon_engine.atoms.plan_cache.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ── THE TIER DECIDER — the reflex-vs-think gate, applied to PLANNING ───────────────────
# warmth verdict -> how to attack the goal. This is the "own tiering, depend on circumstances":
# the substrate reads the goal's warmth from its own bank and picks the cheapest sufficient mode.
TIER_DISPATCH = "dispatch"            # warm/small: one partner, reflex, no plan
TIER_SELF_DECOMPOSE = "self_board"    # lukewarm: a board, workers discover + post sub-goals
TIER_PLAN_FIRST = "plan_board"        # cold/big: a planner posts the plan first, then workers


def decide_tier(goal: str, scope: str, *, store=None, scope_graph=None,
                size_hint: int | None = None) -> tuple[str, dict]:
    """Pick HOW to attack `goal`, tiered by the bank's warmth + the goal's size. Returns
    (tier, signal). Cheapest-sufficient: a goal the estate already knows well (WARM) is a
    reflex — just dispatch it; an unknown big one (COLD) needs planning first. Best-effort:
    if warmth can't be read, fall back to size alone (long/conjunctive goal -> decompose).

    LAYERING-TENSION: warmth and store are in echelon_engine.atoms (atoms layer). Lazy
    imports inside this function — not flagged by scanner. Route via services gate when ready.
    """
    verdict, score = "unknown", 0.0
    try:
        # LAYERING-TENSION: warmth and store are in echelon_engine.atoms (atoms layer).
        # Lazily imported inside functions — not flagged by scanner.
        from echelon_engine.atoms.warmth import warmth as _warmth  # type: ignore[import]
        from echelon_engine.atoms.store import SeedStore  # type: ignore[import]
        store = store or SeedStore()
        r = _warmth(goal, store, scope=scope, scope_graph=scope_graph)
        verdict, score = r.verdict, float(r.score)
    except Exception:
        pass

    # SIZE: a goal naming many sub-parts (commas, "and", "then", a list) is structurally big
    # regardless of warmth — conjunction is the cheap syntactic proxy for "this fans out".
    n_conj = sum(goal.lower().count(w) for w in (", ", " and ", " then ", " + ", ";"))
    big = (size_hint if size_hint is not None else n_conj) >= 2

    signal = {"verdict": verdict, "score": round(score, 3), "conjunctions": n_conj, "big": big}

    # warm + small  -> reflex dispatch. warm + big -> still decompose (known but multi-part).
    # cold/unknown + big -> plan first (the estate doesn't know it; think before acting).
    # everything else -> self-decompose (the middle: a board, workers grow the plan).
    if verdict == "warm" and not big:
        return TIER_DISPATCH, signal
    if verdict == "cold" and big:
        return TIER_PLAN_FIRST, signal
    if big:
        return TIER_SELF_DECOMPOSE, signal
    if verdict in ("warm", "lukewarm"):
        return TIER_DISPATCH, signal
    return TIER_SELF_DECOMPOSE, signal


@dataclass
class RecurResult:
    goal: str
    tier: str
    depth: int
    status: str
    outcome_ok: bool | None
    spent: float
    children: list["RecurResult"] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"goal": self.goal[:120], "tier": self.tier, "depth": self.depth,
                "status": self.status, "outcome_ok": self.outcome_ok,
                "spent": round(self.spent, 4), "detail": self.detail[:160],
                "children": [c.as_dict() for c in self.children]}


def recurse_goal(goal: str, *, scope: str, folder: str,
                 depth: int = 0, max_depth: int = 3,
                 budget_usd: float = 5.0,
                 db_path: str | None = None, model: str | None = None,
                 dispatch_fn: Callable[..., dict] | None = None,
                 board_fn: Callable[..., dict] | None = None,
                 decompose_fn: Callable[..., list] | None = None,
                 spent_fn: Callable[[], float] | None = None,
                 review_judge: Callable[..., bool] | None = None,
                 store=None, on_event: Callable[[str, dict], None] | None = None) -> RecurResult:
    """Attack ONE goal with the tiered, self-deciding recursion. depth + budget bound it:
    at max_depth a goal can no longer spawn a board (it must dispatch — the leaf); the nested
    budget means a child only ever sees the parent's REMAINING slice. 'auto put': whether a
    sub-goal recurses is decided by re-running decide_tier on it, not by a fixed level.

    partner.dispatch, partner.board, and partner.Ledger are lazily imported from the
    ported echelon_engine.agent.partner. autoloop._default_spend_reader is from sibling
    echelon_engine.agent.world.autoloop.
    """
    emit = on_event or (lambda k, d: None)
    if dispatch_fn is None:
        from echelon_engine.agent.partner import dispatch as _d
        dispatch_fn = _d
    if board_fn is None:
        from echelon_engine.agent.partner import board as _b
        board_fn = _b
    if spent_fn is None:
        # default to the REAL spend reader (same as run_recurloop) so a DIRECT recurse_goal call
        # gets a live budget brake too — not a blind lambda:0.0 that never trips the cap.
        from .autoloop import _default_spend_reader  # sibling in apps/world/
        spent_fn = _default_spend_reader(model)

    spent0 = spent_fn()
    if spent0 >= budget_usd:
        return RecurResult(goal=goal, tier="halted", depth=depth, status="budget",
                           outcome_ok=False, spent=spent0, detail="budget cap before acting")

    tier, signal = decide_tier(goal, scope, store=store)
    # DEPTH CAP collapses planning tiers to a dispatch leaf — past max_depth, no more boards.
    if depth >= max_depth and tier != TIER_DISPATCH:
        tier = TIER_DISPATCH
        signal["depth_capped"] = True
    emit("recur_tier", {"goal": goal[:90], "depth": depth, "tier": tier, "signal": signal})

    # ── LEAF: one equipped partner (reflex). dispatch() already verifies + earns by trace. ──
    if tier == TIER_DISPATCH:
        res = dispatch_fn(goal, scope=scope, folder=folder, db_path=db_path, model=model)
        outcome = res.get("outcome") or {}
        ok = outcome.get("ok") if outcome else (res.get("status") == "completed")
        return RecurResult(goal=goal, tier=tier, depth=depth, status=res.get("status", "unknown"),
                           outcome_ok=ok, spent=spent_fn(), detail=(outcome.get("detail") or "")[:160])

    # ── BOARD: DECOMPOSE the goal into sub-goals, SEED them on the ledger as the plan, then
    # drain. A sub-goal that is itself big RE-ENTERS recurse_goal (auto-spawn a child board),
    # bounded by depth + remaining budget. The decomposition is what makes the recursion REAL
    # (live finding 2026-06-18: relying on the board PARTNER to volunteer post() calls gave
    # "0 sub-goals drained" — a flat board-of-one. So WE decompose + post, deterministically). ──
    plan_first = (tier == TIER_PLAN_FIRST)
    from echelon_engine.agent.partner import Ledger
    ledger = Ledger()
    # ── PLAN CACHE: a generated plan is a reusable BAKE. Before paying a planner to decompose,
    # recall a cached plan for this goal's MEANING ($0 hit); only a MISS pays the model, and the
    # generated plan is then crystallized so the next same/similar goal is free. Own the caching,
    # don't re-pay the model for a baked plan ([[plan-is-a-cacheable-bake-own-the-caching]]). Only
    # plan_first results are cached (the syntactic split is already $0 — nothing to save). ──
    _pc = None
    _verdict = None   # the plan-cache review verdict (reflex|review|miss)
    if plan_first:   # only the (paid) planning path is worth caching; the syntactic split is $0
        try:
            # plan_cache is in echelon_engine.atoms.plan_cache — lazy import
            from echelon_engine.atoms.plan_cache import PlanCache  # type: ignore[import]
            _pc = PlanCache(scope=scope, db_path=db_path)
            _verdict = _pc.get(goal)
        except Exception:
            _pc = _verdict = None
    _plan_card_id = None   # the plan-card to reinforce by trace once the run's outcome is known
    # A cached plan is SERVED TO BE REVIEWED, not blindly reused (the cold-Opus law):
    #   route='reflex' -> WARM earned plan: execute/delegate without regenerating (proven, play it).
    #   route='review' -> matched but unproven/near: RUN THE REVIEWER. sound -> use it (win stands);
    #                     UNSOUND -> reject -> regenerate (a wrong served plan is CAUGHT, not reused).
    #   route='miss'   -> generate fresh.
    # This makes the saving measure CORRECTNESS (a reviewed-sound plan), not a blind call-skip.
    _served = False
    if _verdict is not None and _verdict.route == "reflex" and _verdict.leaves:
        subgoals = [lf["task"] if isinstance(lf, dict) else str(lf) for lf in _verdict.leaves]
        _plan_card_id = _verdict.card_id
        _served = True
        emit("plan_served", {"goal": goal[:80], "route": "reflex", "match": _verdict.match,
                             "eff": _verdict.eff, "leaves": len(subgoals)})
    elif _verdict is not None and _verdict.route == "review" and _verdict.leaves and _pc is not None:
        rev = _pc.review_plan(goal, _verdict.leaves, judge=review_judge, model=model)
        emit("plan_reviewed", {"goal": goal[:80], "match": _verdict.match,
                               "sound": rev.get("sound"), "reason": rev.get("reason", "")[:40]})
        if rev.get("sound"):
            subgoals = [lf["task"] if isinstance(lf, dict) else str(lf) for lf in _verdict.leaves]
            _plan_card_id = _verdict.card_id
            _served = True
        # unsound -> fall through to regenerate below
    if not _served:
        subgoals = (decompose_fn or _decompose)(goal, scope=scope, plan_first=plan_first,
                                                folder=folder, model=model, db_path=db_path,
                                                dispatch_fn=dispatch_fn)
        if _pc is not None and subgoals:   # MISS / rejected-review that produced a real plan -> cache it
            try:
                _plan_card_id = _pc.put(goal, [{"task": sg, "tier": ""} for sg in subgoals])
            except Exception:
                pass
    for i, sg in enumerate(subgoals):
        ledger.post(f"sg{depth}-{i}", by="decomposer", task=sg, parent=goal[:60])
    emit("recur_plan", {"goal": goal[:80], "depth": depth, "tier": tier,
                        "sub_goals": [s[:70] for s in subgoals]})

    children: list[RecurResult] = []
    # Drain the LIVING plan: each posted-but-unlanded sub-goal is attacked by recursing on it.
    # (When folder is None / dry-run, board_fn won't post real sub-goals; the loop simply ends —
    # the structure is exercised, the spend is not.)
    drained = 0
    while True:
        if spent_fn() >= budget_usd:
            emit("recur_halt", {"reason": "budget", "depth": depth}); break
        pending = ledger.pending() if ledger else []
        if not pending or drained >= 12:    # 12 = a sub-goal fan cap per board (anti-runaway)
            break
        sub = pending[0]
        drained += 1
        child = recurse_goal(sub["task"], scope=scope, folder=folder,
                             depth=depth + 1, max_depth=max_depth,
                             budget_usd=budget_usd,          # remaining-budget bound (spent is shared)
                             db_path=db_path, model=model, dispatch_fn=dispatch_fn,
                             board_fn=board_fn, decompose_fn=decompose_fn,
                             spent_fn=spent_fn, review_judge=review_judge,
                             store=store, on_event=on_event)
        children.append(child)
        if ledger:
            ledger.land(sub["id"], "recur", {"status": child.status, "ok": child.outcome_ok})

    any_ok = any(c.outcome_ok for c in children) if children else False
    # EARN-BY-TRACE: the plan that drove this board warms on a verified-green run, decays on a red
    # one — so a GOOD plan rises in the cache and a bad one falls (source='trace', never say-so).
    # This is the property the provider's prefix cache can never have. Best-effort; never breaks
    # the run. Only when children actually ran (a dry-run with no drain doesn't grade a plan).
    if _pc is not None and _plan_card_id and children:
        try:
            _pc.reinforce(_plan_card_id, ok=any_ok)
        except Exception:
            pass
    return RecurResult(goal=goal, tier=tier, depth=depth,
                       status="completed" if any_ok else "incomplete",
                       outcome_ok=any_ok, spent=spent_fn(), children=children,
                       detail=f"{'plan-first ' if plan_first else ''}board, {len(children)} sub-goals drained")


def _split_conjuncts(goal: str) -> list[str]:
    """Cheap ($0) syntactic decomposition: split a conjunctive goal into its parts. Handles the
    common 'do A, B, and C' / 'A; B; C' / 'A then B' shapes. Returns [goal] if it doesn't split
    (a non-conjunctive goal is its own single sub-goal). This is the SELF_DECOMPOSE floor — no
    model spend; a planner partner (plan_first) is the escalation above it."""
    import re
    # normalise separators to commas, then split; keep a leading shared clause as context.
    head, _, tail = goal.partition(":")
    body = tail.strip() if tail.strip() else head
    prefix = (head.strip() + ": ") if tail.strip() else ""
    parts = re.split(r",\s*and\s+|,\s*|;\s*|\s+and\s+|\s+then\s+", body)
    parts = [p.strip(" .") for p in parts if p and len(p.strip(" .")) > 3]
    if len(parts) <= 1:
        return [goal]
    # ── Drop DANGLING MODIFIERS, fold them onto the real sub-goals as a shared rider. ──
    # A conjunct that opens with a quantifier/preposition ("each ...", "with ...", "for all
    # ...") carries no actionable head — it is a constraint on the WHOLE set, not a member of
    # it. Letting it become its own sub-goal sends a partner a goal it cannot act on -> a
    # FALSE-RED in the trace (live finding 2026-06-18: 'each with an inline assert self-test'
    # became a 4th phantom leaf). So peel trailing riders off the end and append them to each
    # genuine sub-goal instead, so the constraint still travels but no phantom leaf is dispatched.
    _RIDER = re.compile(r"^(each|all|every|both|with|without|for|using|so that|such that|that)\b",
                        re.IGNORECASE)
    real, riders = [], []
    for p in parts:
        (riders if _RIDER.match(p) else real).append(p)
    if not real:                       # all fragments were riders — nothing splits cleanly
        return [goal]
    rider_suffix = (" (" + "; ".join(riders) + ")") if riders else ""
    return [f"{prefix}{p}{rider_suffix}" for p in real]


def _decompose(goal: str, *, scope: str, plan_first: bool, folder: str | None,
               model: str | None, db_path: str | None, dispatch_fn=None) -> list[str]:
    """Turn a goal into sub-goals. SELF_DECOMPOSE -> the $0 syntactic split. PLAN_FIRST -> a
    planner partner reads the goal + bank and writes a numbered plan to disk, parsed back into
    sub-goals (the cold/big case needs THINKING, not a syntactic split). Best-effort: a planner
    that fails falls back to the syntactic split, so decomposition never dead-ends."""
    syntactic = _split_conjuncts(goal)
    if not plan_first:
        return syntactic
    # PLAN_FIRST: escalate to a planner partner. (Kept simple + best-effort; if no real dispatch
    # or it errors, fall back to the syntactic split — the loop still proceeds.)
    try:
        if dispatch_fn is None or folder is None:
            return syntactic
        plan_goal = (f"Decompose this goal into a SHORT numbered list of independent sub-goals "
                     f"(one per line, '1. ...'), then STOP — do not implement them: {goal}")
        res = dispatch_fn(plan_goal, scope=scope, folder=folder, db_path=db_path, model=model)
        text = res.get("answer") or ""
        import re
        lines = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip()
                 for ln in text.splitlines() if re.match(r"^\s*\d+[.)]\s+", ln)]
        lines = [ln for ln in lines if len(ln) > 6]
        return lines or syntactic
    except Exception:
        return syntactic


# ── THE OUTER DRIVER: autoloop whose ACT is the recursive tiered board() ──────────────
def run_recurloop(*, scope: str, folder: str,
                  budget_usd: float = 5.0, max_turns: int = 4, max_depth: int = 3,
                  db_path: str | None = None, model: str | None = None,
                  dispatch_fn: Callable[..., dict] | None = None,
                  board_fn: Callable[..., dict] | None = None,
                  spent_fn: Callable[[], float] | None = None,
                  on_event: Callable[[str, dict], None] | None = None) -> dict[str, Any]:
    """The full composition: the outer autoloop proposes a big frontier goal FROM THE BANK,
    hands it to recurse_goal (tiered, self-deciding, plan-grows-on-the-ledger), and proposes
    the next. autoloop × recursive board. Bounded by budget + turn cap + depth; halts with a
    report. Reuses autoloop's bank proposer + halt discipline; only the ACT is swapped from a
    flat dispatch to the recursive act."""
    from .autoloop import propose_goal, _default_spend_reader  # sibling in apps/world/
    # LAYERING-TENSION: SeedStore in echelon_engine.atoms.store (atoms layer).
    # Scanner law: apps must not import echelon_engine.atoms directly.
    from echelon_engine.atoms.store import SeedStore  # type: ignore[import]
    emit = on_event or (lambda k, d: None)
    if spent_fn is None:
        spent_fn = _default_spend_reader(model)
    store = SeedStore(db_path) if db_path else SeedStore()

    turns: list[dict] = []
    worked: set[str] = set()
    halted = ""
    for n in range(1, max_turns + 1):
        spent = spent_fn()
        if spent >= budget_usd:
            halted = f"budget cap (${spent:.4f} >= ${budget_usd})"; break
        prop = propose_goal(store, scope, avoid=worked)
        if prop is None:
            halted = "no open frontier left in the bank"; break
        worked.add(prop.source_atom_id)
        emit("recurloop_propose", {"turn": n, "goal": prop.goal, "from_atom": prop.source_atom_id})
        result = recurse_goal(prop.goal, scope=scope, folder=folder, depth=0, max_depth=max_depth,
                              budget_usd=budget_usd, db_path=db_path, model=model,
                              dispatch_fn=dispatch_fn, board_fn=board_fn, spent_fn=spent_fn,
                              store=store, on_event=on_event)
        turns.append({"turn": n, "from_atom": prop.source_atom_id, "result": result.as_dict()})
        emit("recurloop_turn", {"turn": n, "tier": result.tier, "ok": result.outcome_ok,
                                "spent": round(spent_fn(), 4)})
    else:
        halted = f"turn cap ({max_turns} turns)"

    report = {"halted_reason": halted, "total_spent": round(spent_fn(), 4),
              "n_turns": len(turns), "turns": turns}
    emit("recurloop_report", report)
    return report


def main() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(
        prog="apps.world.recurloop",
        description="Autoloop wrapped by autoloop, planned the MiroFish way (tiered recursive board).")
    ap.add_argument("--scope", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--budget", type=float, default=5.0)
    ap.add_argument("--max-turns", type=int, default=4)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--db", default=None)
    ap.add_argument("--model", default=None, help="loop-driver model (default: resolved via the driver chain; see `echelon providers`)")
    ap.add_argument("--decide-only", metavar="GOAL", default=None,
                    help="just print the tier the decider picks for GOAL (no act, $0)")
    args = ap.parse_args()
    if not args.model:  # hardened 2026-07-03: resolve via the one driver chain
        from echelon_engine.atoms.driver import resolve_driver
        args.model = resolve_driver("brain").model

    if args.decide_only:
        tier, signal = decide_tier(args.decide_only, args.scope)
        print(json.dumps({"goal": args.decide_only, "tier": tier, "signal": signal}, indent=2))
        return

    def _printer(k, d):
        print(f"[{k}] {json.dumps(d, default=str)[:220]}", flush=True)

    report = run_recurloop(scope=args.scope, folder=args.folder, budget_usd=args.budget,
                           max_turns=args.max_turns, max_depth=args.max_depth,
                           db_path=args.db, model=args.model, on_event=_printer)
    print("\n=== RECURLOOP REPORT ===")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
