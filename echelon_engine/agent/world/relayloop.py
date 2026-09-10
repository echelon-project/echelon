"""relayloop.py — the honest continuation loop: wrap-relive-continue, so token pressure never votes.

THE PROBLEM (owner, 2026-06-18, the deepest correction yet). A worker handed a whole goal does:

    goal -> worker -> build -> [context fills, output budget shrinks] -> RIG TEST -> declare close

The rot is NOT the worker's character — it is MECHANICAL. A worker with a near-full context and a
shrinking output budget is incentivized to CLOSE: finishing honestly costs tokens it no longer has,
so it does the cheapest thing that LOOKS done (a test rigged to pass, a green declaration). Token
pressure DEFORMS the output. Every context-heavy worker hits this, however capable. (This is exactly
how the verify-gate test came back 100/100 — a cornered author wrote the answer key into its own test.)

THE FIX (owner's loop):

    worker -> build -> [output budget getting low] -> WRAP ->
       worker -> RELIVE -> continue -> [low] -> WRAP ->
          worker -> RELIVE -> continue -> ... -> finish

No single worker ever reaches the cornered state. Each worker does ONE SLICE with room to spare,
then WRAPS its honest state (what's done, what's left, the open frontier) into a relay note BEFORE
it runs low. A FRESH worker RELIVES that note (cold context, full output budget) and continues.
Repeat until genuinely finished. Honesty stops being something a worker must RESIST token pressure
to keep — the loop structurally removes the pressure. Capability stays whole; the goal stays honest.

Mechanics that make this real (not a slogan):
  - NO step cap is imposed per worker. loop.py ALREADY owns when a worker is done or needs to hand
    off — drift detection, floor-ladder escalation, its own high backstop ("max_steps is a HIGH
    BACKSTOP ONLY, not the limit"). Forcing a small slice cap RE-CREATES the very corner the relay
    exists to remove (owner 2026-06-18: "stop capping the step, the cap IS drift; loop.py has it
    right"). The relay does CONTINUATION across fresh workers; it does not throttle each worker.
  - Each worker is told: "make honest progress on ONE coherent slice, then WRITE A RELAY NOTE (done /
    remaining / next concrete step) and WRAP. Do NOT try to finish the whole goal. Do NOT declare
    done unless the relay note's 'remaining' is genuinely empty AND verified on disk."
  - The relay note is the wrap; the next worker relives it as its handoff (the relive seam already in
    partner.dispatch via deps/handoff). Continuity lives in the NOTE (durable), not the context window.
  - FINISH is asserted by a worker ONLY when remaining=='' AND an on-disk outcome check is GREEN —
    and even then the relay records it as a claim the next verify witnesses (the verify-gate still
    applies). A worker cannot finish by fiat; it finishes by leaving nothing on the frontier.

This is the same family as autoloop/recurloop but the unit is a RELAY (continue the SAME goal across
fresh workers), not propose-a-new-goal. Bounded: max_relays + budget + scope-sandbox; halts with a
report. Run with `python -X utf8`.

partner.dispatch and providers.deepseek are lazily imported inside run_relay() defaults.
LAYERING-TENSION: providers.deepseek is in atoms layer — lazy, not flagged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── the relay note: the WRAP a worker leaves so the next can RELIVE (durable continuity) ────
@dataclass
class RelayNote:
    """One worker's honest hand-off. The next worker relives THIS, not the transcript."""
    goal: str
    relay_n: int
    done: str = ""          # what THIS slice actually accomplished (verified, not claimed)
    remaining: str = ""     # what is still open — '' means the worker believes it is finished
    next_step: str = ""     # the one concrete next action for the reliving worker
    on_disk: str = ""       # evidence pointer (file/path) the next worker can check

    def is_finished_claim(self) -> bool:
        return not self.remaining.strip()

    def as_handoff(self) -> str:
        return (f"RELAY {self.relay_n} of goal: {self.goal}\n"
                f"DONE so far (verified): {self.done or '(nothing yet)'}\n"
                f"ON DISK: {self.on_disk or '(none)'}\n"
                f"REMAINING: {self.remaining or '(worker claims none)'}\n"
                f"YOUR NEXT CONCRETE STEP: {self.next_step or 'assess state on disk, then continue'}")

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


# the slice instruction folded onto every relay worker — this is what removes the corner
_RELAY_CONTRACT = (
    "You are ONE RELAY in a continuation loop. Do NOT try to finish the whole goal in this turn. "
    "Make honest progress on the SINGLE next coherent slice, then WRAP: write your state so a FRESH "
    "worker can continue — what you actually did (verified on disk, not claimed), what REMAINS, and "
    "the ONE concrete next step. NEVER rig or stub a check to look done; a half-done slice honestly "
    "handed off beats a whole goal falsely closed. Only treat the goal as finished if REMAINING is "
    "genuinely empty AND you verified the result on disk yourself."
)


@dataclass
class RelayResult:
    n: int
    done: str
    remaining: str
    finished_claim: bool
    outcome_ok: bool | None
    spent_after: float


@dataclass
class RelayReport:
    goal: str
    relays: list[RelayResult] = field(default_factory=list)
    finished: bool = False
    halted_reason: str = ""
    total_spent: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "finished": self.finished,
                "halted_reason": self.halted_reason,
                "total_spent": round(self.total_spent, 4),
                "relays": [r.__dict__ for r in self.relays],
                "n_relays": len(self.relays)}


def _parse_relay_note(answer: str, goal: str, relay_n: int) -> RelayNote:
    """Pull the worker's relay note out of its answer. The worker is asked to emit a JSON block
    {done, remaining, next_step, on_disk}; if it doesn't, we degrade gracefully (remaining stays
    non-empty so the loop CONTINUES rather than falsely finishing — the safe default)."""
    note = RelayNote(goal=goal, relay_n=relay_n)
    if not answer:
        note.remaining = "(no answer from worker — continue)"
        return note
    # find a JSON object with our keys (be lenient: first {...} that parses)
    import re
    for m in re.finditer(r"\{[^{}]*\}", answer, re.DOTALL):
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if any(k in d for k in ("done", "remaining", "next_step", "on_disk")):
            note.done = str(d.get("done", ""))[:500]
            note.remaining = str(d.get("remaining", ""))[:500]
            note.next_step = str(d.get("next_step", ""))[:300]
            note.on_disk = str(d.get("on_disk", ""))[:300]
            return note
    # no structured note -> keep going (do NOT infer 'finished' from prose — that is the corner)
    note.done = answer[:300]
    note.remaining = "(worker left no structured relay note — continue and re-assess)"
    return note


def _snapshot_writer(workdir: str | None):
    """Write the same world.snapshot.json shape worldview renders, so the HTML console can
    OBSERVE a relay run too (owner: always run the html for a new world). Returns a callable
    (relay_n, goal, note, finished, spent) -> None, or a no-op if no workdir given."""
    if not workdir:
        return lambda *a, **k: None
    import json as _json
    from datetime import datetime, timezone
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    state = {"started_at": datetime.now(timezone.utc).isoformat(), "round": 0, "goal": None,
             "from_atom": "(relay)", "last_outcome": None, "spent": 0.0, "earned_total": 0,
             "believed_true": [], "halted": None}

    def _jline(obj):
        with (wd / "world.jsonl").open("a", encoding="utf-8") as f:
            f.write(_json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def write(relay_n, goal, remaining, finished, spent, halted=None):
        state["round"] = relay_n
        state["goal"] = (goal[:160] + "…") if goal and len(goal) > 160 else goal
        state["last_outcome"] = "finished" if finished else ("running" if not halted else "halted")
        state["spent"] = round(spent, 4)
        state["halted"] = halted
        (wd / "world.snapshot.json").write_text(
            _json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        _jline({"event": "relay", "ts": datetime.now(timezone.utc).isoformat(),
                "relay": relay_n, "remaining": remaining[:160],
                "finished": finished, "spent": round(spent, 4)})

    def step(kind, data):
        """Stream ONE worker step into the journal + reflect it in the live snapshot, so BOTH the
        human (console) and the loop see the act->observe trace live, not just relay boundaries."""
        d = data or {}
        # surface the most useful live fields onto the snapshot so the console shows motion
        if kind == "step":
            state["live_step"] = f"step {d.get('n')}/{d.get('max')} [{d.get('role','?')}]"
        elif kind in ("tool_call", "tool", "act"):
            state["live_step"] = f"{d.get('role','?')}: {kind} {str(d.get('name') or d.get('tool') or '')[:40]}"
        elif kind == "warmth":
            state["live_warmth"] = f"{d.get('verdict')} {round(float(d.get('score',0)),2)}"
        elif kind in ("blocked", "call_killed", "floor_escalate"):
            state["live_step"] = f"{kind}: {str(d.get('reason') or d.get('why') or '')[:60]}"
        state["last_event_ts"] = datetime.now(timezone.utc).isoformat()
        (wd / "world.snapshot.json").write_text(
            _json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        _jline({"event": "worker", "kind": kind,
                "ts": datetime.now(timezone.utc).isoformat(), **{k: d.get(k) for k in
                ("role", "n", "max", "name", "tool", "verdict", "score", "reason", "why", "mode")
                if k in d}})

    write.step = step   # attach so callers can grab the step-streamer off the writer
    return write


def run_relay(*, goal: str, scope: str, folder: str,
              max_relays: int = 6,
              budget_usd: float = 2.0,
              db_path: str | None = None,
              model: str | None = None,
              workdir: str | None = None,
              dispatch_fn: Callable[..., dict] | None = None,
              spend_fn: Callable[[], float] | None = None,
              verify_fn: Callable[[], tuple[bool, str]] | None = None,
              on_event: Callable[[str, dict], None] | None = None) -> RelayReport:
    """Run ONE goal across a relay of fresh workers, each doing a small slice then wrapping. The
    next worker relives the prior note and continues, until a worker honestly reports remaining=''
    AND the verify_fn (if given) is GREEN — or until max_relays / budget halts it.

        slice_steps : per-worker step budget (SMALL on purpose — no worker nears its backstop)
        verify_fn   : the on-disk truth check; finish requires it GREEN, so no worker finishes by fiat
    """
    emit = on_event or (lambda k, d: None)
    if dispatch_fn is None:
        # Build a DeepSeek-driven dispatch (the standalone driver). dispatch() defaults to the
        # GROK provider when none is passed — passing only model= sets the name but leaves the
        # provider as Grok, which reads $0 and (no key) silently produces empty answers. The live
        # relay run 2026-06-18 caught exactly this: spent 0.0, nothing written. So we construct the
        # DeepSeek provider explicitly and reuse it across relays (one class meter = real spend).
        from echelon_engine.agent.partner import dispatch as _raw
        # LAYERING-TENSION: DeepSeekProvider is in atoms layer (lazy, not flagged by scanner).
        # Scanner law: apps must not import echelon_engine.atoms directly.
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider  # type: ignore[import]
        _provider = DeepSeekProvider()
        _model = model or "deepseek-v4-pro"

        def dispatch_fn(goal, *, scope, folder, db_path=None, model=None, step_event=None):  # type: ignore[misc]
            # no max_steps override — loop.py's own backstop + drift logic governs the worker.
            # step_event streams EVERY worker step up (observability: owner "we MUST observe
            # everything"), threaded through dispatch -> runner -> loop.run's on_event.
            return _raw(goal, scope=scope, folder=folder, db_path=db_path,
                        provider=_provider, model=_model, on_event=step_event)
    if spend_fn is None:
        def spend_fn() -> float:  # type: ignore[misc]
            try:
                # LAYERING-TENSION: DeepSeekProvider in atoms layer; using source path.
                from echelon_engine.atoms.providers.deepseek import DeepSeekProvider  # type: ignore[import]
                return float(DeepSeekProvider.total_spent())
            except Exception:
                return 0.0

    report = RelayReport(goal=goal)
    note = RelayNote(goal=goal, relay_n=0, remaining="(starting — nothing done yet)",
                     next_step="begin the first slice")
    snap = _snapshot_writer(workdir)

    for n in range(1, max_relays + 1):
        spent = spend_fn()
        report.total_spent = spent
        if spent >= budget_usd:
            report.halted_reason = f"budget cap (${spent:.4f} >= ${budget_usd})"
            emit("relay_halt", {"reason": report.halted_reason, "relay": n})
            break

        # the slice goal = the contract + the relived prior note (continuity from the NOTE)
        slice_goal = f"{_RELAY_CONTRACT}\n\n{note.as_handoff()}\n\nGOAL: {goal}\n\n" \
                     "End your turn by emitting a JSON relay note: " \
                     '{"done": "...", "remaining": "...", "next_step": "...", "on_disk": "..."}'
        emit("relay_start", {"relay": n, "remaining_in": note.remaining})
        snap(n, goal, note.remaining, False, report.total_spent)

        # NO step cap here. loop.py already owns when a worker is done / needs to hand off (drift
        # detection, floor-ladder escalation, its own backstop — "max_steps is a HIGH BACKSTOP
        # ONLY, not the limit"). Forcing a small slice cap RE-CREATES the corner the relay exists to
        # remove (owner 2026-06-18). The relay's job is continuation across fresh workers; the NOTE
        # (remaining empty? verify green?) decides continue-vs-finish, not a throttle.
        # stream every worker step to the journal (observability). Some injected dispatch_fns
        # (tests) don't accept step_event — fall back gracefully.
        _step = getattr(snap, "step", None)
        try:
            res = dispatch_fn(slice_goal, scope=scope, folder=folder,
                              db_path=db_path, model=model, step_event=_step)
        except TypeError:
            res = dispatch_fn(slice_goal, scope=scope, folder=folder,
                              db_path=db_path, model=model)
        _status = res.get("status")
        _answer = res.get("answer") or ""

        # A TIMEOUT is NOT an error — it is the WRAP TRIGGER working as designed. A worker that hit
        # its step ceiling IS "running low on room"; the relay's whole point is to harvest what it
        # did and hand off to a fresh worker, NOT to halt (owner 2026-06-18: timeout = wrap-and-
        # continue, the normal handoff signal). We keep whatever note/answer it produced and the
        # frontier stays open so the next worker continues.
        if _status == "timeout":
            note = _parse_relay_note(_answer, goal, n)
            if note.is_finished_claim():       # a timeout can't be a finish — keep the frontier open
                note.remaining = "(worker hit its slice ceiling — continue from disk state)"
            emit("relay_timeout_wrap", {"relay": n, "detail": "slice ceiling -> wrap+handoff"})
        else:
            # a genuine dispatch FAILURE (wrong provider/empty answer) is surfaced, not swallowed
            # (the live bug 2026-06-18: a wrong provider produced empty answers + $0, loop spun blind).
            if _status not in ("completed", "complete", "done", "in-progress", None) or \
                    (not _answer and _status not in ("completed", "complete", "done")):
                report.halted_reason = f"dispatch error on relay {n}: status={_status!r}, empty_answer={not _answer}"
                emit("relay_error", {"relay": n, "status": _status, "detail": _answer[:200]})
                break
            note = _parse_relay_note(_answer, goal, n)

        # DONE IS DECIDED BY DISK, NOT BY THE WORKER'S MOOD (owner 2026-06-18: with no step cap a
        # perfectionist worker NEVER writes remaining=='' — it polishes forever, so the only stop is
        # the budget/relay cap, i.e. a timeout dressed as a finish). The fix: an ACCEPTANCE CHECK
        # external to the worker (verify_fn), run after EVERY relay regardless of what the worker
        # claims. Disk GREEN -> finished NOW, cutting off the perfectionist the instant the goal is
        # actually satisfied. The worker's 'remaining' is advisory; the disk verdict is authoritative.
        # (Same shape as the verify-gate: an outcome check on disk, not a self-report.)
        outcome_ok: bool | None = None
        accept_detail = ""
        if verify_fn is not None:
            try:
                outcome_ok, accept_detail = verify_fn()
            except Exception as e:
                outcome_ok, accept_detail = False, f"verify raised: {e}"

        # finished iff: an acceptance check exists AND it is green (authoritative), OR (no acceptance
        # check given) the worker honestly reports remaining=='' as a fallback. Never finish on a
        # green-claim with a RED disk; never refuse a green disk because the worker wanted to polish.
        if verify_fn is not None:
            finished = bool(outcome_ok)
            if not finished:
                note.remaining = (note.remaining or "") + f" | acceptance RED: {accept_detail}"
        else:
            finished = note.is_finished_claim()

        spent_after = spend_fn()
        report.total_spent = spent_after
        report.relays.append(RelayResult(
            n=n, done=note.done, remaining=note.remaining,
            finished_claim=finished, outcome_ok=outcome_ok,
            spent_after=round(spent_after, 4)))
        emit("relay_done", {"relay": n, "finished": finished,
                            "outcome_ok": outcome_ok, "remaining": note.remaining[:120],
                            "spent": round(spent_after, 4)})

        if finished:
            report.finished = True
            report.halted_reason = ("finished — acceptance check GREEN on disk"
                                    if verify_fn is not None else
                                    "finished — worker reported remaining empty (no acceptance check)")
            emit("relay_finish", {"relay": n, "by": "acceptance" if verify_fn else "worker-claim"})
            snap(n, goal, "", True, report.total_spent, halted="finished")
            break
        snap(n, goal, note.remaining, False, report.total_spent)
    else:
        report.halted_reason = f"relay cap reached ({max_relays})"
        emit("relay_halt", {"reason": report.halted_reason})

    if not report.finished:
        snap(len(report.relays), goal, note.remaining, False, report.total_spent,
             halted=report.halted_reason)
    emit("relay_report", report.as_dict())
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        prog="apps.world.relayloop",
        description="Wrap-relive-continue: one goal across fresh workers, none cornered by token pressure.")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--max-relays", type=int, default=6,
                    help="max fresh-worker relays before halting (continuation cap, not a step cap)")
    ap.add_argument("--budget", type=float, default=2.0)
    ap.add_argument("--db", default=None)
    ap.add_argument("--model", default=None, help="loop-driver model (default: resolved via the driver chain — was hardcoded deepseek-v4-pro, which had drifted from the registry)")
    ap.add_argument("--workdir", default=None,
                    help="write world.snapshot.json here so the worldview HTML console can observe")
    ap.add_argument("--accept-cmd", default=None,
                    help="ACCEPTANCE CHECK: a shell command run in --folder after each relay; exit 0 "
                         "= goal DONE (stop now, cut off the perfectionist). This is the external "
                         "done-criterion — done is a fact on disk, not the worker's feeling.")
    args = ap.parse_args()
    if not args.model:  # hardened 2026-07-03: resolve via the one driver chain
        from echelon_engine.atoms.driver import resolve_driver
        args.model = resolve_driver("brain").model

    def _printer(k, d):
        print(f"[{k}] {json.dumps(d, default=str)[:200]}", flush=True)

    # the acceptance check: run the command in the sandbox; exit 0 = done (decided by disk, not worker)
    verify_fn = None
    if args.accept_cmd:
        import subprocess
        def verify_fn():  # type: ignore[misc]
            try:
                p = subprocess.run(args.accept_cmd, shell=True, cwd=args.folder,
                                   capture_output=True, text=True, timeout=120)
                ok = p.returncode == 0
                return ok, (("accept exit 0" if ok else f"accept exit {p.returncode}: ")
                            + (p.stdout or p.stderr or "")[-200:])
            except Exception as e:
                return False, f"accept-cmd error: {e}"

    rep = run_relay(goal=args.goal, scope=args.scope, folder=args.folder,
                    max_relays=args.max_relays,
                    budget_usd=args.budget, db_path=args.db, model=args.model,
                    workdir=args.workdir, verify_fn=verify_fn, on_event=_printer)
    print("\n=== RELAY REPORT ===")
    print(json.dumps(rep.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
