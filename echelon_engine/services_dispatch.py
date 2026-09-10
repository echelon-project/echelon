"""services_dispatch — the DISPATCH flow as a ChainResult (the first vertical slice).

This is the proof that chainboard expresses echelon's orchestration: partner.dispatch's
real shape — warm -> hands-check -> run -> verify -> earn -> gate — written as a straight-line
chain of named steps instead of 150 lines of tangled control flow. Each step is recorded
({name, ok, skipped, value, error}), so the live bridge / status card / trace-cards get
per-step evidence for free, and a failure short-circuits the rest (fail-fast).

PRIVATE service child: importable only from echelon_engine.services (the gate) — the scanner
forbids apps/ importing this directly. The heavy/real work (the actual model loop, the bank
warm, the trace write) is injected as callables so the chain SHAPE is testable at $0 and the
production wiring plugs the real atoms in. This keeps the chain pure-orchestration (no provider
import here) — the AlphaApp discipline: services orchestrate, atoms do the I/O.

The payload threaded through the chain is one dict (ctx), each step enriches it:
    ctx = {goal, scope, rules, needs_write, can_write, warm, guidance, run, outcome, earned}
"""
from __future__ import annotations

from typing import Any, Callable

from echelon_sdk.chain import ChainResult


def _needs_write(goal: str, rules: str | None) -> bool:
    blob = (goal + " " + (rules or "")).lower()
    return any(w in blob for w in (
        "write", "edit", "migrate", "rework", "redesign", "implement",
        "create", "add ", "fix ", "refactor", "change", "build"))


def dispatch_chain(
    goal: str,
    *,
    scope: str = "echelon",
    rules: str | None = None,
    can_write: bool = True,
    warm_fn: Callable[[str, str], dict] | None = None,
    run_fn: Callable[[dict], dict] | None = None,
    verify_fn: Callable[[dict], tuple[bool, str]] | None = None,
    earn_fn: Callable[[str, bool], dict] | None = None,
) -> ChainResult:
    """Build + run the dispatch chain for one goal. All I/O is injected (warm/run/verify/earn);
    with no injections it runs a $0 dry shape that still records every step. The chain is the
    same straight-line for the dry and live paths — only the injected leaves differ."""

    needs_write = _needs_write(goal, rules)

    def warm(_ctx: dict) -> dict:
        w = warm_fn(scope, goal) if warm_fn else {"verdict": "cold", "warmest": []}
        return {**_ctx, "warm": w}

    def hands_check(ctx: dict) -> dict:
        # a build goal handed to a no-write role wrote-nothing-and-claimed-done is the trap;
        # refuse loudly inside the chain (a failed step short-circuits run/verify/earn).
        if ctx["needs_write"] and not can_write:
            raise PermissionError(
                "goal needs to write to disk but the role has no write hands — "
                "dispatch with a writing role; a no-hands partner cannot build")
        return ctx

    def build_guidance(ctx: dict) -> dict:
        moves = []
        for c in (ctx.get("warm", {}).get("cartridges") or []):
            moves += (c.get("warmest") or [])
        g = (f"ECHELON-equipped partner in-scope ({scope}). RULES: {rules or '(bank only)'}."
             + ((" Earned moves: " + " | ".join(moves)) if moves else "")
             + " Act directly; prove the smallest slice first if this is new ground.")
        return {**ctx, "guidance": g}

    def run(ctx: dict) -> dict:
        res = run_fn(ctx) if run_fn else {"status": "dry-run", "answer": "", "steps": 0}
        return {**ctx, "run": res}

    def verify(ctx: dict) -> dict:
        if verify_fn:
            ok, detail = verify_fn(ctx)
        else:
            # default outcome check: a run that claims done must report a non-empty result
            r = ctx.get("run") or {}
            ok = r.get("status") in ("completed", "done", "success", "dry-run")
            detail = f"status={r.get('status')}"
        return {**ctx, "outcome": {"ok": bool(ok), "detail": detail}}

    def earn(ctx: dict) -> dict:
        ok = bool((ctx.get("outcome") or {}).get("ok"))
        earned = earn_fn(goal, ok) if earn_fn else {"credited": ok, "q": 85.0 if ok else 35.0}
        return {**ctx, "earned": earned}

    ctx0 = {"goal": goal, "scope": scope, "rules": rules,
            "needs_write": needs_write, "can_write": can_write}

    return (
        ChainResult.of(ctx0, name="dispatch")
        .pipe(warm)
        .pipe(hands_check)
        .pipe(build_guidance)
        .pipe(run)
        .where_ok()            # if the run step errored, skip verify+earn (fail-fast)
        .pipe(verify)
        .pipe(earn)
        .collect()
    )
