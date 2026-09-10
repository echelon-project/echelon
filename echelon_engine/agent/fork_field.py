"""fork_field.py — the warmth-clocked watcher fork-field, on the LIVE core.db.

THE FRONTIER (memory: compiler-era-fork-field, dream-and-the-respect-handshake).
run_workflow (workflow.py) is the STATIC wave scheduler: it computes every wave
up front (compute_waves) and runs each wave to completion before the next, in
strict topological order. That whole-wave-at-once loop is the last surviving
CURSOR — the for-loop the compiler era deletes.

This is its replacement: ONE rolling frontier, warmth-clocked.
  - A WATCHER flips a step BLOCKED -> READY the moment all its deps are DONE
    (no fixed waves; the frontier re-resolves every tick).
  - THE PAGER schedules by LIVE warmth read from core.db — warmth(step.task, store,
    scope).score orders the READY frontier, hottest first. This is the whole point
    of building on the live bank, not a hand-set STATE: the field is scheduled by
    what the soul actually recognizes as hot RIGHT NOW.
  - DECAY is the clock of the clockless machine, and it cuts ONE way only: a
    READY-but-unchosen step cools (idle-hot = the runaway-narration shape); a
    BLOCKED step is WAITING ON A WATCHER for its inputs (a legitimate Gantt wait)
    and is IMMUNE to decay until its deps land. "Waiting is not drifting." This is
    a DESIGN LAW, not a knob — the sim died on first run without it (critical-path
    tasks cooled below the floor before their turn).
  - COHERENCE without a lock: a step is CLAIMED by a content-addressed token before
    it runs; a second fork reaching the same step YIELDS (no redundant work, no mutex).
  - COLD-TERMINATION, damped: the field stops when every live (non-DONE) step is
    below the fire floor, OR all steps are DONE. A re-warming edge may re-heat a
    neighbour, but decay outruns propagation, so the field always cools (the
    anti-newline-flood invariant).

PROVEN FIRST in sim_fork_field_v2.py (ECHELON, 9 invariants green) — this is that
exact physics with the deterministic stubs swapped for the live organs:
  stub warmth  -> warmth(task, store, scope) read against core.db
  stub model   -> the injected run_step executor (the tiered runner / agent runner)

It takes the SAME workflow dict + run_step contract as run_workflow, so it is a
drop-in alternative scheduler: the tiered runner plugs in unchanged. Threads the
existing seams (compute_waves' readiness logic, the run_step executor) — it does
NOT fork the executor or the policy. See workflow.py, tiered_runner.py.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ── physics constants (the clock of the clockless machine; from sim v2) ─────────
# Registry-driven (config.get): tunable via ~/.echelon/config.json without a code change; the literal
# here is the FALLBACK if config/json is absent (same degrade-safe contract as the registry itself).
import echelon_sdk.config as _cfg
DECAY = _cfg.get("fork_field.decay", 0.55)        # a READY-but-unchosen step's warmth *= this per idle tick
PROPAGATE = _cfg.get("fork_field.propagate", 0.40)  # a finished step adds this * its warmth to a re-warm edge
COLD = _cfg.get("fork_field.cold", 0.10)          # below this, a step is too cold to fire (the halt floor)
REWARM_CAP = _cfg.get("fork_field.rewarm_cap", 8)   # safety: a healthy field cools within this many re-warms

BLOCKED, READY, RUNNING, DONE = "BLOCKED", "READY", "RUNNING", "DONE"


@dataclass
class FieldStep:
    """A step on the rolling frontier. `task` is the action text the PAGER scores for warmth."""
    id: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    rewarms: list[str] = field(default_factory=list)   # edges this step re-heats on finish
    raw: dict = field(default_factory=dict)            # the original workflow step (passed to run_step)
    state: str = BLOCKED
    warmth: float = 1.0           # live; seeded by the bank read, decays while idle-hot
    claimed_by: Optional[str] = None
    rewarm_count: int = 0


# ── the pager: read warmth LIVE from core.db ───────────────────────────────────
def make_bank_pager(store, scope: str, *, judge_provider=None, judge_model: str = "grok-4.3",
                    scope_graph=None) -> Callable[[str], float]:
    """Return warmth_of(task_text) -> 0..1, read live from the bank. The hotter the soul runs on
    a step's action, the sooner the pager schedules it. judge_provider=None -> lexical floor ($0).
    This is the seam the whole 'build on the live core.db' decision turns on: the field is paced by
    what the bank ACTUALLY recognizes, not a hand-set number."""
    from echelon_engine.atoms.warmth import warmth as _warmth

    def warmth_of(task_text: str) -> float:
        try:
            r = _warmth(task_text, store, scope, judge_provider=judge_provider,
                        judge_model=judge_model, scope_graph=scope_graph)
            return float(getattr(r, "score", 0.0) or 0.0)   # WarmthReading.score = 0..1 recognition
        except Exception:
            return 0.0   # a bank hiccup must never wedge the field; fall back to cold-neutral

    return warmth_of


def _claim_token(step_id: str) -> str:
    """Content-addressed claim token (coherence without a lock) — sim v2's mechanism, verbatim."""
    return hashlib.sha256(step_id.encode()).hexdigest()[:8]


# ── the field ──────────────────────────────────────────────────────────────────
def run_fork_field(
    wf: dict,
    *,
    run_step: Callable[[dict, dict], dict],
    warmth_of: Optional[Callable[[str], float]] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
    max_parallel: Optional[int] = None,   # None -> config fork_field.max_parallel (registry-driven)
    max_ticks: Optional[int] = None,      # None -> config fork_field.max_ticks
    journal: Optional[Any] = None,        # WorkflowRunJournal | None — durable persistence (tail/wfpersist)
) -> dict:
    """Run a validated workflow as a warmth-clocked fork-field instead of static waves.

    SAME contract as run_workflow: `run_step(step, deps) -> result dict`, where deps maps a
    dep-id to its result so a step sees its dependencies' output. Returns the same shape
    {steps, started, finished, elapsed, ok} plus `ticks` and `cold` (why it stopped).

    `warmth_of(task_text) -> 0..1` is the PAGER (use make_bank_pager for the live read). If
    None, every READY step is equally warm (1.0) — the field degenerates to topological order
    (still correct, just unpaged); pass the bank pager to schedule by the live soul.

    NEVER raises on a step failure — a failed step is recorded DONE-with-error so the field
    keeps moving (a real board keeps moving), exactly like run_workflow.

    journal=None (default): pure in-memory. Pass a WorkflowRunJournal to enable durable persistence.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # registry-driven defaults (caller may override; None -> the config value -> the fallback)
    if max_parallel is None:
        max_parallel = _cfg.get("fork_field.max_parallel", 18)
    if max_ticks is None:
        max_ticks = _cfg.get("fork_field.max_ticks", 5000)

    def emit(k: str, d: dict) -> None:
        if on_event:
            on_event(k, d)
        if journal is not None:
            journal.record(k, d)

    if warmth_of is None:
        warmth_of = lambda _t: 1.0   # noqa: E731 — unpaged fallback (topological order)

    steps: dict[str, FieldStep] = {}
    for s in wf["steps"]:
        steps[s["id"]] = FieldStep(
            id=s["id"], task=s.get("task", "") or s.get("agent", "") or s["id"],
            depends_on=list(s.get("depends_on", []) or []),
            rewarms=list(s.get("rewarms", []) or []),
            raw=s,
        )
    # seed each step's warmth from the live bank ONCE at compile (then it decays/propagates).
    # NO born-warm floor: the pager's verdict is authoritative. A step the soul runs cold on is
    # LEGITIMATELY cold — if it never warms (no re-warm edge lands on it) the field cools it out
    # (field-cold, the step undone), which is the honest answer, not a bug to paper over. (A floor
    # here would override the soul's recognition — the very thing building on the live bank is for.)
    for st in steps.values():
        st.warmth = warmth_of(st.task)

    results: dict[str, dict] = {}
    started = time.time()
    emit("field_start", {"steps": len(steps)})

    def _watch_and_frontier() -> list[FieldStep]:
        """The WATCHER + PAGER: flip BLOCKED->READY where deps landed, return READY frontier
        (warm enough to fire), hottest first."""
        frontier: list[FieldStep] = []
        for st in steps.values():
            if st.state == BLOCKED and all(steps[d].state == DONE for d in st.depends_on):
                st.state = READY
                emit("watch_ready", {"id": st.id, "warmth": round(st.warmth, 3)})
            if st.state == READY and st.warmth >= COLD:
                frontier.append(st)
        frontier.sort(key=lambda s: s.warmth, reverse=True)   # the pager: hottest first
        return frontier

    def _cool() -> None:
        """Decay — ONLY READY-but-unchosen steps cool. BLOCKED is decay-immune (waiting on a
        watcher = a legitimate Gantt wait, not drift). The owner's law, the sim's hard invariant."""
        for st in steps.values():
            if st.state == READY:
                st.warmth *= DECAY

    def _is_cold() -> bool:
        live = [s for s in steps.values() if s.state != DONE]
        if not live:
            return True
        return all(s.warmth < COLD for s in live)

    def _write_back(st: FieldStep, result: dict) -> None:
        results[st.id] = result
        st.state = DONE
        emit("write_back", {"id": st.id, "status": result.get("status")})
        # propagate: finishing re-warms its edges (damped by decay; capped as a tripwire)
        for nb in st.rewarms:
            n = steps.get(nb)
            if n is None or n.state == DONE:
                continue
            n.warmth += st.warmth * PROPAGATE
            n.rewarm_count += 1
            if n.rewarm_count > REWARM_CAP:
                # the anti-newline-flood tripwire: a field that won't cool is a bug, not a slow run.
                emit("rewarm_cap", {"id": nb, "count": n.rewarm_count})

    tick = 0
    cold_reason = ""   # decided at exit: all-done if every step DONE, else field-cold (cooled out)
    while not _is_cold():
        tick += 1
        if tick > max_ticks:
            cold_reason = "max-ticks"
            break
        # WATCH must run even when nothing fires this tick, so a step whose deps just landed gets
        # flipped BLOCKED->READY (and its warmth re-evaluated against the floor) before we judge cold.
        frontier = _watch_and_frontier()
        if not frontier:
            _cool()                       # nothing hot enough yet; let time pass
            continue

        # claim (coherence) — the chosen frontier this tick; cap to max_parallel hottest.
        chosen: list[FieldStep] = []
        for st in frontier[:max_parallel]:
            if st.claimed_by is None:
                st.claimed_by = _claim_token(st.id)
                st.state = RUNNING
                chosen.append(st)
        emit("tick", {"tick": tick, "fire": [s.id for s in chosen],
                      "frontier": [(s.id, round(s.warmth, 3)) for s in frontier]})

        # fork: run the chosen steps in parallel from their dep-snapshot (RAM is per-fork).
        def _do(st: FieldStep) -> tuple[str, dict]:
            deps = {d: results.get(d, {}) for d in st.depends_on}
            t0 = time.time()
            emit("step_start", {"id": st.id, "task": st.task[:80]})
            try:
                r = run_step(st.raw, deps)
            except Exception as e:  # a dying step must not kill the field
                r = {"status": "error", "answer": repr(e)}
            r = {**r, "id": st.id, "agent": st.raw.get("agent"),
                 "elapsed": round(time.time() - t0, 2), "warmth": round(st.warmth, 3)}
            emit("step_done", {"id": st.id, "status": r.get("status"), "elapsed": r["elapsed"]})
            return st.id, r

        with ThreadPoolExecutor(max_workers=min(max_parallel, len(chosen))) as ex:
            futs = [ex.submit(_do, st) for st in chosen]
            done_now = {sid: r for sid, r in (f.result() for f in as_completed(futs))}
        for st in chosen:
            _write_back(st, done_now[st.id])

        _cool()                           # everyone still idle-hot cools a tick

    finished = time.time()
    undone = [s.id for s in steps.values() if s.state != DONE]
    if not cold_reason:                    # not set by a max-ticks break
        cold_reason = "all-done" if not undone else "field-cold"
    ordered = [results[s["id"]] for s in wf["steps"] if s["id"] in results]
    ok = all(r.get("status") not in ("error",) for r in ordered)
    out = {
        "steps": ordered,
        "started": started, "finished": finished, "elapsed": round(finished - started, 2),
        "ok": ok, "ticks": tick, "cold": cold_reason,
        "undone": undone,
    }
    emit("field_done", {"ok": ok, "ticks": tick, "cold": cold_reason, "elapsed": out["elapsed"]})
    return out
