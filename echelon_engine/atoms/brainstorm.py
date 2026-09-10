"""brainstorm — the COUNCIL ENGINE as a reusable capability (the brainstorm cartridge).

THE DREAM (owner, 2026-06-20): "from how council works, create a brainstorm cartridge — it uses a
series of different judges of multiple models: for taking decisions, finding options, checking
defects, an honesty check, and n more." This session ran a 4-seat + chair council BY HAND in a
scratch script to settle the equip fork (unanimous on the Gemini floor). That hand-rolled council is
exactly what should be a DOOR, not a throwaway — so this is it.

A brainstorm is a DELIBERATION, not a vote (the grandvote organ ratifies insights; this generates +
pressure-tests them). Its seats run in a deliberate ORDER that mirrors real reasoning, and — the
load-bearing part — DIFFERENT SEATS RUN ON DIFFERENT MODELS, so it is a genuine multi-judge council,
not one model arguing with itself (a single model's blind spots are correlated across its own seats).

THE SEATS (the owner's list + the substrate's own honesty law as a seat), in flow order:
  1. OPTIONS  (divergence)  — find the candidate space; enumerate distinct, real options. [reason floor]
  2. DEFECT   (skeptic)     — for each option, the failure mode that SURVIVES contact. [build floor]
  3. HONESTY  (the moat)    — is each option EARNED or merely asserted? what claim is unproven, what
                              would it take to earn it? (ECHELON's significance-by-trace law as a seat.) [council floor]
  4. DECISION (convergence) — weigh it; pick one; justify by the lens that decides it. [council floor]
  5. CHAIR    (synthesis)   — read every seat; render the decision + the concrete build directive. [council floor]
Extra seats plug in via the SEATS spec — `for n` is real: a brainstorm is just an ordered list of
typed seats, each (role, model-or-floor, instruction).

Each seat is routed through `routing.pick(<floor-role>)` -> `provider_for(...)`, so the multi-model
spread is the roster's, not hardcoded — and the COUNCIL floor is Gemini (owner moved it 2026-06-20).
The brainstorm earns by trace like any cartridge: a brainstorm that LANDS a good decision should warm
its seat atoms (scope `brainstorm-council`). See:
  - atoms-are-scoped-but-a-capability-crosses-scopes  (the council that birthed this ran on Gemini)
  - ux-cartridge-built-earns-by-trace-not-by-skill    (a capability is a cartridge that EARNS)
"""
from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field

from .routing import pick, provider_for


@dataclass
class Seat:
    """One council seat: a named lens, the floor-role it routes to (-> a model), and its instruction.
    `floor` is a routing role (council/reason/build/ux/audit…) resolved to a model at run time — so a
    seat's model follows the roster, and different seats land on different models by design."""
    name: str
    floor: str
    instruction: str
    model: str = ""        # resolved at run (pick(floor)); set explicitly to PIN a model.

    def resolved_model(self) -> str:
        return self.model or pick(self.floor)


# THE DEFAULT BRAINSTORM — the owner's five named lenses, in deliberation order. Multi-model by floor:
# OPTIONS/DEFECT land on reason/build (grok), HONESTY/DECISION/CHAIR on the council floor (gemini).
DEFAULT_SEATS: list[Seat] = [
    Seat("OPTIONS", "reason",
         "You are the OPTIONS seat (divergence). Enumerate the DISTINCT, real options for the question "
         "— not strawmen. 3-5 of them, each one line: the option + the single thing that makes it "
         "different. Do not pick; widen the space honestly."),
    Seat("DEFECT", "build",
         "You are the DEFECT/skeptic seat. For each option on the table, name the failure mode that "
         "SURVIVES contact with reality — the one that actually harms the user/goal, not a nitpick. "
         "Then say which option is most ROBUST and the one thing it must guard against."),
    Seat("HONESTY", "council",
         "You are the HONESTY seat — ECHELON's significance-by-trace law made a judge: significance is "
         "EARNED, never asserted; nothing grades its own homework. AUDIT TWO THINGS, not one: (1) the "
         "OPTIONS + the other seats' claims — earned (evidence/proven prior/trace) or merely asserted? "
         "(2) THE BRIEF ITSELF — do not trust the premises handed to you. Flag any FACT in the context/"
         "question that is assumed but unproven (e.g. 'X is more mature than Y', 'this is from Z'), "
         "because a false premise in the brief launders straight into the decision. Name each unearned "
         "claim and what it would take to earn it. Distrust confidence, including the brief's."),
    Seat("DECISION", "council",
         "You are the DECISION seat (convergence). Weigh the options against the defects and the honesty "
         "audit. PICK ONE. Give the single reason that decides it and the strongest objection you are "
         "knowingly accepting. Be decisive — a non-decision is a failure of this seat."),
]

CHAIR = Seat("CHAIR", "council",
             "You are the council CHAIR. Read every seat's verdict. Render the council's DECISION (which "
             "option), the shared reasoning, and the concrete BUILD DIRECTIVE: what to implement + what "
             "to guard against. If the seats disagree, name the disagreement and break it. <=160 words.")

BRAINSTORM_SCOPE = "brainstorm-council"   # the cartridge scope its seat atoms live in

# THE HONESTY LAW DISCLAIMER — prepended to EVERY brief and printed on EVERY run (owner, 2026-06-20:
# "add disclaimer when using brainstorm to always follow the honesty law of echelon"). A brainstorm is
# only as honest as its brief; the council reasons faithfully even from a FALSE premise (caught live: a
# brief asserted 'jcode's decay math is more mature than ECHELON's' — untrue, ECHELON had the same
# exponential time-decay first — and the council laundered that false fact into a decision). So the law
# rides on the council itself: significance is EARNED not asserted, and the seats must distrust the brief.
HONESTY_DISCLAIMER = (
    "⚖ ECHELON HONESTY LAW (binding on this council): significance is EARNED BY TRACE, never asserted — "
    "nothing grades its own homework. Distrust confident-sounding claims, INCLUDING the premises in this "
    "brief: a fact stated here is not proven by being stated. The HONESTY seat audits the brief's facts, "
    "not just the options. A decision built on an unearned premise is itself unearned. Surface unproven "
    "claims rather than reasoning past them."
)


@dataclass
class BrainstormResult:
    question: str
    seats: list[dict] = field(default_factory=list)   # [{name, model, verdict}]
    chair: str = ""
    chair_model: str = ""
    trace_path: str = ""
    execution_status: str = "unknown"


# GEMINI SPACING (bug-trap fix, owner 2026-06-20): the gemini family errors under BURST — N seats firing
# back-to-back with no spacing trips the backend (a rate/concurrency limit, not content). So a gemini-tier
# call waits a beat first. Per-process clock so spacing holds ACROSS seats, not just within one.
import threading as _threading
_GEMINI_GAP_S = 2.0
_last_gemini_call = [0.0]
_gemini_lock = _threading.Lock()


def _space_gemini(model: str) -> None:
    if not model.startswith("gemini"):
        return
    with _gemini_lock:
        import time as _t
        wait = _GEMINI_GAP_S - (_t.monotonic() - _last_gemini_call[0])
        if wait > 0:
            _t.sleep(wait)
        _last_gemini_call[0] = _t.monotonic()


def _ask(seat: Seat, brief: str, transcript: str, *, max_tokens: int = 500,
         on_attempt=None) -> tuple[str, str]:
    """Run one seat, WALKING ITS FLOOR'S FALLBACK CHAIN until a model answers (bug-trap fix): a single
    floor outage (gemini down) no longer kills the seat — it falls through to deepseek, the cheap floor.
    A PINNED seat.model still wins (explicit override). Gemini-tier calls are SPACED to dodge the burst
    error. Returns (model_id_that_answered, verdict). Best-effort: only if EVERY model in the chain fails
    does the seat report [FAILED ...] — one dead floor must not kill the council."""
    from .routing import model_chain
    # the try-order: a pinned model is the whole chain; else the floor's full fallback chain.
    chain = [seat.model] if seat.model else model_chain(seat.floor)
    sys_msg = (seat.instruction +
               "\n\nReply in <=140 words. Be concrete and name specifics; this is a working council, "
               "not an essay.")
    user = brief if not transcript else f"{brief}\n\n--- THE COUNCIL SO FAR ---\n{transcript}"
    last_err = "no model in chain"
    for attempt_index, model in enumerate(chain):
        if on_attempt:
            on_attempt("provider_started", attempt_index=attempt_index, requested_model=model)
        outcome = {"attempt_index": attempt_index, "requested_model": model}
        answer = None
        try:
            _space_gemini(model)
            prov = provider_for(model)
            r = prov.send([{"role": "system", "content": sys_msg},
                           {"role": "user", "content": user}],
                          model_id=model, temperature=0.4, max_tokens=max_tokens)
            metadata = getattr(r, "raw", None)
            if isinstance(metadata, dict):
                outcome.update(model_identity_status=metadata.get("model_identity_status", "unverified"),
                               request_id=metadata.get("request_id"),
                               selected_model=metadata.get("selected_model"),
                               usage_basis=metadata.get("usage_basis", "unavailable"))
            if getattr(r, "status", "") == "success" and (r.content or "").strip():
                answer = r.content.strip()
                outcome["status"] = "response_received"
            else:
                last_err = "provider failed or returned empty content"
                outcome["status"] = "failed"
        except Exception as e:
            last_err = type(e).__name__
            outcome.update(status="failed", error_type=type(e).__name__)
        # Trace errors must escape before fallback, never be mistaken for a
        # provider error that authorizes another call.
        if on_attempt:
            on_attempt("provider_finished", **outcome)
        if answer is not None:
            return model, answer
        # this model failed — fall to the next floor in the chain
    return (chain[-1] if chain else "?"), f"[FAILED after {len(chain)} model(s): {last_err}]"


def brainstorm(question: str, *, context: str = "", seats: list[Seat] | None = None,
               chair: Seat | None = None, trace_path: str | Path | None = None) -> BrainstormResult:
    """Run a brainstorm council on `question`. `context` is optional grounding (the real material —
    code, the fork, the constraints). `seats` overrides the default five lenses; `chair` overrides the
    synthesis seat. Each seat runs on its floor's model, seeing the running
    transcript. A new, fsynced trace preserves each completed response before
    dispatching the next seat. Trace failures stop further provider calls."""
    seats = seats or DEFAULT_SEATS
    chair = chair or CHAIR
    brief = f"{HONESTY_DISCLAIMER}\n\nTHE QUESTION:\n{question}"
    if context:
        brief += f"\n\nCONTEXT:\n{context}"
    from .echelon_home import echelon_home
    path = Path(trace_path) if trace_path is not None else (
        echelon_home() / "traces" / "brainstorm" / (uuid.uuid4().hex + ".jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    res = BrainstormResult(question=question, trace_path=str(path.resolve()))
    started = time.monotonic()
    with path.open("x", encoding="utf-8") as trace:
        def emit(kind, **fields):
            trace.write(json.dumps({"v": 1, "kind": kind, "ts": time.time(),
                                   "elapsed_s": time.monotonic() - started,
                                   **fields}, ensure_ascii=False) + "\n")
            trace.flush()
            os.fsync(trace.fileno())

        emit("council_started", brief_sha256=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
             seats=[{"name": s.name, "requested_model": s.model or None, "floor": s.floor}
                    for s in [*seats, chair]])
        transcript = ""
        active_seat = None
        failures = 0
        try:
            for index, s in enumerate([*seats, chair]):
                active_seat = index
                emit("seat_started", seat_index=index, name=s.name,
                     requested_model=s.model or None, floor=s.floor)
                model, verdict = _ask(s, brief, transcript, max_tokens=600 if index == len(seats) else 500,
                                      on_attempt=lambda kind, **fields: emit(kind, seat_index=index, **fields))
                failed = verdict.startswith("[FAILED after ")
                failures += int(failed)
                emit("seat_response", seat_index=index, name=s.name, model=model,
                     status="failed" if failed else "response_received", verdict=verdict,
                     cost_status="unavailable")
                if index == len(seats):
                    res.chair, res.chair_model = verdict, model
                else:
                    res.seats.append({"name": s.name, "model": model, "verdict": verdict})
                    transcript += f"\n{s.name} (on {model}):\n{verdict}\n"
            res.execution_status = ("failed" if failures == len(seats) + 1 else
                                    "partial" if failures else "responses_recorded")
            emit("council_finished", status=res.execution_status, failed_seats=failures,
                 acceptance_status="not_assessed")
        except BaseException as exc:
            # If storage itself failed, preserve the original exception; a missing
            # terminal event means unknown, never a fabricated successful council.
            try:
                emit("council_interrupted", seat_index=active_seat,
                     error_type=type(exc).__name__, retry_safe=False)
            except BaseException:
                pass
            raise
    return res


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────
def _render(res: BrainstormResult) -> None:
    print(f"\n{HONESTY_DISCLAIMER}\n")
    print(f"=== BRAINSTORM COUNCIL: {res.question}\n")
    for s in res.seats:
        print(f"{'='*70}\n{s['name']}  (on {s['model']})\n{'='*70}\n{s['verdict']}\n")
    print(f"{'='*70}\nCHAIR  (on {res.chair_model})\n{'='*70}\n{res.chair}\n")


def _main(argv=None):
    import argparse
    import sys
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon brainstorm",
        description="The brainstorm council: N typed judges across multiple models — options / defect / "
                    "honesty / decision / chair. A deliberation, not a vote.")
    ap.add_argument("question", help="the question/fork to deliberate")
    ap.add_argument("--context", default="", help="grounding material (the real fork, constraints, code)")
    ap.add_argument("--context-file", default="", help="read context from a file instead")
    ap.add_argument("--seats", default="", help="comma-list to subset the default seats "
                    "(e.g. 'OPTIONS,DEFECT,DECISION'); omit for all five")
    ap.add_argument("--json", action="store_true", help="emit the raw result as JSON")
    ap.add_argument("--trace", default=None, help="new durable JSONL trace path; never overwrites an existing trace")
    a = ap.parse_args(argv)

    context = a.context
    if a.context_file:
        from pathlib import Path
        context = Path(a.context_file).read_text(encoding="utf-8")
    seats = None
    if a.seats:
        want = {n.strip().upper() for n in a.seats.split(",")}
        seats = [s for s in DEFAULT_SEATS if s.name in want] or None
    res = brainstorm(a.question, context=context, seats=seats, trace_path=a.trace)
    if a.json:
        print(json.dumps({"question": res.question, "seats": res.seats,
                          "chair": res.chair, "chair_model": res.chair_model,
                          "trace_path": res.trace_path,
                          "execution_status": res.execution_status}, indent=2))
    else:
        _render(res)
        print(f"\nDurable trace: {res.trace_path}")
        print(f"Execution status: {res.execution_status}")
    return 0 if res.execution_status == "responses_recorded" else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
