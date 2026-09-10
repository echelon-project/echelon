"""tiered_runner.py — make a decomposed plan FLOW TO T3, fast and cheap.

THE OWNER'S INSIGHT (2026-06-07), from CV-001 + CV-005: the swarm method is not
OS_Tier's privilege; every tier delegates DOWNWARD, and everything that CAN land
on the cheapest tier MUST. Once T1 has compiled the plan (decided WHAT to do —
the intelligence-class work), the remaining steps are mechanical. They should
land on T3: PURE PYTHON first ($0, deterministic, inherently logged), DeepSeek
(cheap-strong) only as the fallback. "if everything lands to T3: tokenomics at
its height + tracked action and reasoning."

THE GAP THIS CLOSES (found live, refactor runs 2026-06-07): workflow.py already
has the parallelism (compute_waves = Gantt; run_workflow = parallel waves) and a
runner (make_agent_runner). But make_agent_runner runs a FULL LLM AGENT LOOP for
EVERY step — even a step that is pure mechanical execution (write this content to
this file). So a T3 atom pays for a T1 driver, and a single driver that must
"decide to stop reading and write" gets stuck over-reading (the offload->write
trap that stalled the hand-run refactor). The fix is structural: the tier ROUTES
THE EXECUTOR, with a deterministic T3 floor — so the driver never has to choose
to stop reading; mechanical atoms just run as code.

This is sim-proven (sim_tiered_swarm.py in ECHELON), with its two laws baked in:
  - STRICT-DOWN termination: a tier delegates only to a LOWER tier (the T3 floor
    always terminates) — here, a step is executed, never re-decomposed in place.
  - COMPOUND-PRESERVES-TIER: splitting is the planner's job (compute_waves);
    this runner executes atoms, it does not re-split them.

It reuses, does not fork: compute_waves + run_workflow (parallel waves) and
gantt_pillars.classify_tier (the work-kind classifier). It only adds the missing
T3 floor + the tier->executor routing that make_agent_runner lacked.

make_agent_runner (from echelon_engine.agent.workflow) and classify_tier /
TIER_FAST (from echelon_sdk.gantt_pillars) are lazily imported. Both modules
are ported; a missing import raises ImportError at call time.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

# ── THE T3 PURE-PYTHON FLOOR: deterministic handlers ($0, inherently logged) ──────
# A step whose intent matches one of these runs as CODE — no model, no tokens, and
# the "action + reasoning" is the code path itself (the most traceable layer). Each
# handler takes (step, deps, folder) and returns a result dict, or raises to fall
# through to the next tier. They cover the mechanical ops a compiled plan produces:
# write a file from given content, move/copy, list, count, read — the refactor's
# "write the extracted module" is exactly write_file_atom.

def _resolve(folder: str, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (Path(folder) / p)


def _write_file_atom(step: dict, deps: dict, folder: str) -> dict:
    """A step that supplies the literal content to write (the planner already produced it,
    or a dependency did). Pure file write — the canonical T3-code atom (the refactor's
    'write the extracted module verbatim'). Content comes from step['content'] or, if a
    dependency produced it, deps[<id>]['content']/['answer']."""
    path = step.get("path") or step.get("file")
    content = step.get("content")
    if content is None:
        # pull from a dependency's output (a prior atom that produced the text)
        for dr in (deps or {}).values():
            content = dr.get("content") or dr.get("answer")
            if content:
                break
    if not path or content is None:
        raise ValueError("write_file_atom needs path + content (no model needed)")
    target = _resolve(folder, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"status": "completed", "answer": f"wrote {len(content)} chars to {path}",
            "executor": "T3-code", "cost": 0.0}


def _list_atom(step: dict, deps: dict, folder: str) -> dict:
    pattern = step.get("pattern", "*")
    base = _resolve(folder, step.get("path", "."))
    hits = sorted(str(p.relative_to(folder)) for p in base.glob(pattern) if p.is_file())
    return {"status": "completed", "answer": json.dumps(hits), "executor": "T3-code", "cost": 0.0}


def _read_atom(step: dict, deps: dict, folder: str) -> dict:
    target = _resolve(folder, step.get("path", ""))
    if not target.is_file():
        raise ValueError("read_atom: not a file")
    text = target.read_text(encoding="utf-8")
    return {"status": "completed", "answer": text, "content": text,
            "executor": "T3-code", "cost": 0.0}


# intent keyword -> handler. The planner tags a step with kind/op, OR we infer from the
# task verb. Kept small + explicit: a handler runs ONLY when the step is unambiguously
# that mechanical op (it raises otherwise, falling through to the model tiers).
_T3_HANDLERS: dict[str, Callable[[dict, dict, str], dict]] = {
    "write_file": _write_file_atom,
    "write": _write_file_atom,
    "list": _list_atom,
    "read": _read_atom,
}


def t3_code_handler(step: dict) -> Callable | None:
    """Return a pure-Python handler if this step is an unambiguous mechanical atom, else None.
    Prefer an explicit step['op']; fall back to the leading verb of the task."""
    op = (step.get("op") or "").strip().lower()
    if op in _T3_HANDLERS:
        return _T3_HANDLERS[op]
    # explicit content+path with no reasoning needed = a write atom even without an op tag
    if (step.get("content") is not None) and (step.get("path") or step.get("file")):
        return _write_file_atom
    return None


# ── THE TIERED run_step: tier ROUTES the executor (the thing that was missing) ─────
def make_tiered_runner(provider, model: str, folder: str, *, budget=None,
                       max_steps: int = 60, deepseek=None, deepseek_model: str = "deepseek-chat",
                       local=None, local_model: str = "smollm3-3b-gabliterated-i1",
                       on_route: Callable[[dict], None] | None = None,
                       guidance: str | None = None,
                       files: list[str] | None = None,
                       image: list[str] | None = None) -> Callable[[dict, dict], dict]:
    """Build a run_step that routes a step to the CHEAPEST executor that can do it:
      1. T3-CODE  — a pure-Python handler matches  -> run it, $0 (the floor).
      2. T3-DEEPSEEK — a 'fast'/atomic step that needs a little judgment -> one cheap call.
      3. FULL AGENT LOOP — a real reasoning step (planning/code_generation/validation) ->
         make_agent_runner's full loop (booted into the role, warmth, guards).
    Drop-in for run_workflow's run_step, so the EXISTING parallel-wave engine fans these
    out — independent atoms in a wave run concurrently, each on its cheapest tier. This is
    'flow to T3': the expensive tiers are spent only where reasoning is actually required.

    OS-tier steering context: guidance (string prepended as system-reminder), files (paths to
    read and attach as grounding context), image (paths for visual context) are forwarded to
    make_agent_runner for full-agent-loop steps.

    make_agent_runner is lazily imported from the ported echelon_engine.agent.workflow.
    classify_tier / TIER_FAST are from echelon_sdk.gantt_pillars (already in sdk).
    """
    from echelon_engine.agent.workflow import make_agent_runner

    # classify_tier + TIER_FAST are in echelon_sdk (already there as gantt_pillars)
    from echelon_sdk.gantt_pillars import classify_tier, TIER_FAST

    _full = make_agent_runner(provider, model, folder, budget=budget, max_steps=max_steps,
                              guidance=guidance, files=files, image=image)

    def run_step(step: dict, deps: dict) -> dict:
        # 1) T3 pure-python floor — mechanical atom, no tokens.
        fn = t3_code_handler(step)
        if fn is not None:
            try:
                r = fn(step, deps, folder)
                if on_route:
                    on_route({"id": step.get("id"), "tier": "T3", "executor": "T3-code"})
                return r
            except Exception:
                pass  # not actually a clean code atom -> fall through to a model tier

        kind = step.get("tier") or classify_tier(step.get("task", ""))
        # 2) T3 floor — a 'fast' atomic step that needs a touch of judgment, but not a full
        #    reasoning loop. LOCAL-FIRST ($0 LM Studio), DeepSeek as the paid fallback: try the
        #    free local model; if it's missing or errors (cold-load failed, transport down), fall
        #    back to the cheap-paid deepseek so the step NEVER dies for want of the local floor.
        if kind == TIER_FAST and (local is not None or deepseek is not None):
            task = step.get("task", "")
            handoff = "".join(f"\n[from {d}]: {(dr.get('answer') or '')[:400]}"
                              for d, dr in (deps or {}).items())
            msgs = [{"role": "user", "content": task + handoff}]
            if local is not None:
                resp = local.send(msgs, model_id=local_model)
                if resp.status != "error":
                    if budget is not None:
                        budget.charge(local_model, resp.tokens_in, resp.tokens_out,
                                      kind="driver", tier="T3")   # $0 by cost.py, tag tier for by_tier
                    if on_route:
                        on_route({"id": step.get("id"), "tier": "T3", "executor": "T3-local"})
                    return {"status": "completed", "answer": resp.content or "",
                            "executor": "T3-local", "cost": 0.0}
                # local failed (after its own JIT retry) -> fall through to the paid floor below.
            if deepseek is not None:
                resp = deepseek.send(msgs, model_id=deepseek_model)
                if budget is not None:
                    budget.charge(deepseek_model, resp.tokens_in, resp.tokens_out,
                                  kind="driver", tier="T3")   # tag the tier so by_tier is real
                if on_route:
                    on_route({"id": step.get("id"), "tier": "T3", "executor": "T3-deepseek"})
                return {"status": "completed", "answer": resp.content or "",
                        "executor": "T3-deepseek", "cost": None}

        # 3) the real reasoning tier — a full agent loop (planning/code/validation).
        # Thread the tier tag into the budget charge by passing tier=kind to _full,
        # which flows through make_agent_runner -> _run -> charge_driver -> charge(tier=...).
        # This tags every driver-step spend in the full loop with the step's tier so
        # by_tier accounting is real, not reconstructed.
        if on_route:
            on_route({"id": step.get("id"), "tier": kind, "executor": "agent-loop"})
        r = _full(step, deps, tier=kind)
        r.setdefault("executor", "agent-loop")
        return r

    return run_step
