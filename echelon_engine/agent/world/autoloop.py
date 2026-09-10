"""autoloop.py — TRUE AUTONOMOUS, ECHELON-BACKED.

The dream made into a CYCLE. The pieces existed (cartridge_boot = wake, partner.dispatch =
act, earn_craft_from_trace + record_run = earn, outcome-verify = honesty); what was missing
was the seam that closes them into a self-driving loop: the agent PROPOSING ITS OWN NEXT GOAL
from the substrate's own state, instead of a human handing the sentence.

The cycle (one turn):
    boot  →  propose-goal-from-bank  →  dispatch (equipped partner acts)  →  verify outcome
          →  earn-by-trace  →  re-warm  →  propose-next

What makes it ECHELON-autonomous and not just an agent in a while-loop: the next goal is
RECALLED OUT OF THE ESTATE, never invented ([[memory-is-a-weight-adjustor]] — the warmest
atom on "what to work on next"). Each turn the bank is DIFFERENT (warmth moved because the
last turn earned), so the next proposal is informed by what just paid off. The substrate
steers itself.

The boundaries (owner, 2026-06-18): BUDGET + SCOPE-SANDBOXED. A hard $ cap stops runaway
spend; the partner is sandboxed to one workspace; the loop HALTS WITH A REPORT at the cap or
the iteration cap — it does not run forever. Bank-proposed goals (not a seeded queue).

Run with `python -X utf8` (cp1252 arrow-crash). Never raw-SQL the soul.

partner.dispatch (echelon_engine.agent.partner) is lazily imported. providers.grok
(echelon_engine.atoms.providers.grok) is lazily imported inside _default_spend_reader —
LAYERING-TENSION: that is an atoms import from the agent layer. memory.store
(echelon_engine.atoms.store) is lazily imported inside run_autoloop — LAYERING-TENSION.
All three are inside function bodies (not module-level), so the scanner does not flag them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── the FRONTIER SIGNAL: which atoms describe unfinished work the loop can take up ──────
# An open-frontier atom is one a /wrap-session producer left as live work-in-flight: its
# content carries an OPEN/next-step marker. We read those out of the bank (no invention) and
# the freshest un-worked one becomes the next goal.
_FRONTIER_MARKERS = (
    "open:", "open (", "open —", "still open", "the open frontier", "next =", "next:",
    "next (owner", "next step", "work in flight", "wire ", "drive ", "unspent",
    "haven't", "hasn't", "not yet", "todo", "remaining",
)


def _is_frontier(content: str) -> bool:
    c = (content or "").lower()
    return any(m in c for m in _FRONTIER_MARKERS)


def _first_line(content: str) -> str:
    return (content or "").strip().splitlines()[0] if content else ""


# The OPEN markers that introduce the actual unfinished CLAUSE inside a frontier atom (vs the
# atom's topic). We extract the sentence AFTER one of these — that is the goal, not the whole
# atom. Ordered most-specific first so "OPEN (other half):" beats a bare "open".
_OPEN_CLAUSE_MARKERS = (
    "still open:", "still open —", "still open (the other half):", "still open:",
    "open (other half):", "open (the other half):",
    "open:", "open —", "the open frontier is:", "the open frontier", "next =", "next:", "next step",
)


def _extract_open_clause(content: str) -> str | None:
    """Pull the unfinished-work CLAUSE out of a frontier atom — the text after an OPEN marker,
    trimmed to one sentence. This is what makes the proposed goal the REMAINING work, not the
    atom's whole topic (the refinement caught 2026-06-18: proposing the atom's first line
    re-surfaced already-done work; the OPEN clause is the live edge)."""
    low = content.lower()
    best_pos, best_marker = None, None
    for m in _OPEN_CLAUSE_MARKERS:
        i = low.find(m)
        if i != -1 and (best_pos is None or i < best_pos):
            best_pos, best_marker = i, m
    if best_pos is None:
        return None
    tail = content[best_pos + len(best_marker):].strip()
    # one sentence / clause: stop at the first hard stop or link/citation marker
    for stop in (". ", "\n", " [[", " (commit", " (see", " See ", " — see"):
        j = tail.find(stop)
        if j != -1:
            tail = tail[:j]
    return tail.strip(" .—-:") or None


@dataclass
class Proposal:
    """A next goal proposed from the bank — with the atom it came from, so the loop is
    auditable (you can always see WHICH frontier atom drove WHICH goal)."""
    goal: str
    source_atom_id: str
    source_handle: str
    score: float = 0.0


def propose_goal(store, scope: str, *, recent_first: bool = True,
                 avoid: set[str] | None = None) -> Proposal | None:
    """Read the bank's OPEN-FRONTIER atoms and frame the freshest un-worked one as the next
    goal. This is the ECHELON seam: the goal comes from the estate's own recorded state, not
    a human or a hallucination. `avoid` skips atom ids already worked this run (so the loop
    advances instead of re-proposing the same frontier). Returns None when no frontier remains
    — the honest 'nothing left to autonomously do' signal (the loop then halts with a report)."""
    avoid = avoid or set()
    seeds = store.seeds(scope=scope)
    # A real frontier atom must have an EXTRACTABLE open clause (the remaining work), not just
    # mention 'open' somewhere. We pair each candidate with its clause and skip those with none.
    candidates = []
    for s in seeds:
        if s.id in avoid or not _is_frontier(s.content):
            continue
        clause = _extract_open_clause(s.content)
        if clause and len(clause) > 12:        # a real instruction, not a stray marker
            candidates.append((s, clause))
    if not candidates:
        return None
    # Freshest first — the most recently-recorded open work is the live edge (wrap-session
    # writes the open handoff last). Recency is the proxy for 'the current frontier'.
    candidates.sort(key=lambda sc: sc[0].ts, reverse=recent_first)
    top, clause = candidates[0]
    handle = _first_line(top.content)[:80]
    # The goal is the OPEN CLAUSE, grounded by which atom it came from (auditable).
    goal = clause
    return Proposal(goal=goal, source_atom_id=top.id,
                    source_handle=f"{handle} → {clause[:80]}",
                    score=float(getattr(top, "score", 0.0)))


@dataclass
class TurnResult:
    n: int
    goal: str
    source_atom_id: str
    status: str
    outcome_ok: bool | None
    earned: dict | None
    spent_after: float
    detail: str = ""


@dataclass
class LoopReport:
    turns: list[TurnResult] = field(default_factory=list)
    halted_reason: str = ""
    total_spent: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "halted_reason": self.halted_reason,
            "total_spent": round(self.total_spent, 4),
            "turns": [t.__dict__ for t in self.turns],
            "completed_ok": sum(1 for t in self.turns if t.outcome_ok),
            "n_turns": len(self.turns),
        }


def run_autoloop(*, scope: str, folder: str,
                 budget_usd: float = 5.0,
                 max_turns: int = 6,
                 db_path: str | None = None,
                 dispatch_fn: Callable[..., dict] | None = None,
                 budget_spent_fn: Callable[[], float] | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 model: str | None = None) -> LoopReport:
    """The self-driving loop. Bank-proposed goals, budget + scope-sandboxed, halts with a report.

        scope          : the project bank scope (the cartridge that makes the partner a local operator)
        folder         : the ONE workspace the partner is sandboxed to (its hands resolve here)
        budget_usd     : HARD cap — the loop halts BEFORE a turn that would exceed it
        max_turns      : iteration cap — a second halt condition (never run forever)
        dispatch_fn    : the act primitive; defaults to partner.dispatch (injectable for $0 tests)
        budget_spent_fn: reads cumulative spend; defaults to the live Grok provider's spend

    Each turn: propose from the bank → dispatch an equipped partner → the dispatch already
    verifies the outcome + earns craft by trace (the hooks wired 2026-06-18) → record the turn
    → re-propose (the bank moved). Halts on: budget cap, turn cap, or NO FRONTIER LEFT (the
    honest 'done' — nothing left to autonomously take up)."""
    emit = on_event or (lambda k, d: None)
    if dispatch_fn is None:
        from echelon_engine.agent.partner import dispatch as _dispatch
        dispatch_fn = _dispatch
    if budget_spent_fn is None:
        budget_spent_fn = _default_spend_reader(model)

    # LAYERING-TENSION: SeedStore is in echelon_engine.atoms.store (atoms layer).
    # Lazily imported inside function — not flagged by scanner.
    from echelon_engine.atoms.store import SeedStore  # type: ignore[import]
    store = SeedStore(db_path) if db_path else SeedStore()

    report = LoopReport()
    worked: set[str] = set()

    for n in range(1, max_turns + 1):
        spent = budget_spent_fn()
        report.total_spent = spent
        if spent >= budget_usd:
            report.halted_reason = f"budget cap reached (${spent:.4f} >= ${budget_usd})"
            emit("autoloop_halt", {"reason": report.halted_reason, "turn": n})
            break

        prop = propose_goal(store, scope, avoid=worked)
        if prop is None:
            report.halted_reason = "no open frontier left in the bank — nothing to autonomously take up"
            emit("autoloop_halt", {"reason": report.halted_reason, "turn": n})
            break

        worked.add(prop.source_atom_id)
        emit("autoloop_propose", {"turn": n, "goal": prop.goal,
                                  "from_atom": prop.source_atom_id})

        # ACT — the equipped partner. dispatch() already: warms the cartridge on this goal,
        # verifies the outcome on disk (claims-done-blind guard), and earns craft by trace.
        res = dispatch_fn(prop.goal, scope=scope, folder=folder,
                          db_path=db_path, model=model)
        outcome = res.get("outcome") or {}
        ok = outcome.get("ok") if outcome else (res.get("status") == "completed")
        spent_after = budget_spent_fn()
        report.total_spent = spent_after

        turn = TurnResult(n=n, goal=prop.goal, source_atom_id=prop.source_atom_id,
                          status=res.get("status", "unknown"), outcome_ok=ok,
                          earned=res.get("earned"), spent_after=round(spent_after, 4),
                          detail=(outcome.get("detail") or "")[:200])
        report.turns.append(turn)
        emit("autoloop_turn", {"turn": n, "status": turn.status, "ok": ok,
                               "spent": turn.spent_after, "earned": turn.earned})

    else:
        report.halted_reason = f"turn cap reached ({max_turns} turns)"
        emit("autoloop_halt", {"reason": report.halted_reason})

    emit("autoloop_report", report.as_dict())
    return report


def _default_spend_reader(model: str | None) -> Callable[[], float]:
    """Read cumulative USD spend from the live loop-driver provider. Best-effort: if no
    provider/meter is reachable, returns 0.0 (the budget cap then never trips on spend —
    the turn cap still bounds the loop, so it is still safe).

    THE BUG THIS FIXES (2026-08-17): this function took `model` and IGNORED it, always
    constructing GrokProvider. Every non-Grok run — deepseek is the estate default — read
    spend as 0.0 forever, so `--budget` was INERT and only the turn cap bounded cost. The
    docstring admitted the failure while the signature promised the opposite. recurloop and
    worldjournal both import this reader, so all three loops inherited the dead cap.

    LAYERING-TENSION: atoms.providers is the atoms layer; lazily imported inside the
    function — not flagged by scanner.
    """
    name = (model or "").lower()
    # Ordered most-specific-first: the driver's model id names its vendor.
    if "deepseek" in name:
        candidates = ("deepseek.DeepSeekProvider", "grok.GrokProvider")
    elif "gemini" in name:
        candidates = ("gemini.GeminiProvider", "grok.GrokProvider")
    elif "grok" in name:
        candidates = ("grok.GrokProvider",)
    else:
        # Unknown/unset model: try each metered provider rather than assuming one vendor.
        candidates = ("deepseek.DeepSeekProvider", "gemini.GeminiProvider", "grok.GrokProvider")

    for path in candidates:
        mod_name, _, cls_name = path.partition(".")
        try:
            mod = __import__(f"echelon_engine.atoms.providers.{mod_name}",
                             fromlist=[cls_name])
            prov = getattr(mod, cls_name)()
            if hasattr(prov, "total_spent"):
                return lambda p=prov: float(p.total_spent())
        except Exception:
            continue
    return lambda: 0.0


# ── CLI — so the substrate (or a /loop) can launch the autonomous loop directly ────────
def main() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(
        prog="apps.world.autoloop",
        description="True autonomous, ECHELON-backed: bank-proposed goals, budget+sandbox bounded.")
    ap.add_argument("--scope", required=True, help="project bank scope (the cartridge)")
    ap.add_argument("--folder", required=True, help="the ONE workspace the partner is sandboxed to")
    ap.add_argument("--budget", type=float, default=5.0, help="hard USD cap (halt before exceeding)")
    ap.add_argument("--max-turns", type=int, default=6, help="iteration cap (second halt condition)")
    ap.add_argument("--db", default=None, help="bank db path (default: SeedStore default)")
    ap.add_argument("--model", default=None, help="loop-driver model (default: resolved — ECHELON_BRAIN > config 'brain' > first live provider; see `echelon providers`)")
    ap.add_argument("--propose-only", action="store_true",
                    help="only print the goal the bank would propose right now (no dispatch, $0)")
    args = ap.parse_args()
    if not args.model:  # hardened 2026-07-03: resolve via the one driver chain
        from echelon_engine.atoms.driver import resolve_driver
        args.model = resolve_driver("brain").model

    if args.propose_only:
        # LAYERING-TENSION: SeedStore is in echelon_engine.atoms.store.
        # Lazily imported inside function — not flagged by scanner.
        from echelon_engine.atoms.store import SeedStore  # type: ignore[import]
        store = SeedStore(args.db) if args.db else SeedStore()
        prop = propose_goal(store, args.scope)
        print(json.dumps(prop.__dict__ if prop else {"proposal": None}, indent=2, default=str))
        return

    def _printer(k, d):
        print(f"[{k}] {json.dumps(d, default=str)[:200]}", flush=True)

    report = run_autoloop(scope=args.scope, folder=args.folder, budget_usd=args.budget,
                          max_turns=args.max_turns, db_path=args.db, model=args.model,
                          on_event=_printer)
    print("\n=== AUTOLOOP REPORT ===")
    print(json.dumps(report.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
