"""services_dispatch_atoms — the REAL leaves for the dispatch chain.

The dispatch chain (services_dispatch.py) is pure orchestration: it takes its I/O as
injected callables (warm_fn / run_fn / verify_fn / earn_fn) so the SHAPE is testable at
$0. This module supplies the PRODUCTION leaves built from the migrated engine atoms —
so the chain composes real recall + real trace-credit, not stubs. It mirrors the live
echelon_agent/partner.py semantics (_warm_one / _earn_a_craft_card).

Layer note: this is a PRIVATE service child (echelon_engine, layer `echelon_engine`), so
it MAY import echelon_engine.atoms.* (the allowed downward edge). The scanner forbids only
the reverse. It is reachable only through echelon_engine.services (the gate).

Why a FACTORY (not module-level functions): warm/earn need a store/card-store bound to a
specific db (a scratch db in a test, the real bank in production). The factory closes over
those, returning the (scope, goal)/(goal, ok) callables the chain expects.
"""
from __future__ import annotations

from typing import Callable

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.warmth import warmth
from echelon_engine.atoms.providers.floor_chat import T1_ARCHITECT


def make_warm_fn(store: SeedStore, *, n: int = 5) -> Callable[[str, str], dict]:
    """A warm leaf bound to a SeedStore. Returns warm_fn(scope, goal) -> the cartridge verdict
    (verdict/score + the warmest move bodies), mirroring partner._warm_one. The chain's
    build_guidance reads `warmest` to seed the partner's guidance."""
    def warm_fn(scope: str, goal: str) -> dict:
        r = warmth(goal, store, scope=scope)
        warmest = []
        for sw in (getattr(r, "warmest", None) or [])[:n]:
            seed = sw.seed if hasattr(sw, "seed") else sw
            warmest.append((getattr(seed, "content", "") or "")[:140])
        return {"scope": scope, "verdict": getattr(r, "verdict", "unknown"),
                "score": round(float(getattr(r, "score", 0.0)), 3),
                "warmest": warmest,
                # the chain's build_guidance walks ctx['warm']['cartridges'][*]['warmest'];
                # one primary cartridge here, shaped so guidance picks up the earned moves.
                "cartridges": [{"scope": scope, "warmest": warmest}]}
    return warm_fn


def _craft_label(goal: str) -> str:
    """Which craft card a goal earns — a bugfix loop or a feature loop (partner._earn_a_craft_card)."""
    blob = goal.lower()
    bug = any(w in blob for w in ("fix", "bug", "crash", "error", "broken"))
    return "craft:bugfix-loop" if bug else "craft:feature-loop"


def make_earn_fn(cards: CardStore) -> Callable[[str, bool], dict]:
    """An earn leaf bound to a CardStore. Returns earn_fn(goal, ok) -> the trace-credit result.
    A GREEN outcome earns q=85 on the goal's craft card; a RED outcome marks the q=35 stall.
    source='trace' always (only a real execution trace can move a card / redeem a lie). If the
    craft card doesn't exist yet, it is created (born neutral) so the first trace has something
    to credit — the honest seed: the card earns from this trace, it does not assert rank."""
    def earn_fn(goal: str, ok: bool) -> dict:
        label = _craft_label(goal)
        with cards._lock:
            row = cards.conn.execute("SELECT id FROM cards WHERE label=?", (label,)).fetchone()
            cid = row["id"] if row else None
        if not cid:
            cid = cards.add_card(label, refs=[])      # born neutral; this trace is its first earn
        q = 85.0 if ok else 35.0
        rep = cards.reinforce_card(cid, q, source="trace")
        return {"card": label, "q": q, "credited": bool(rep.get("ok", False)),
                "card_score": rep.get("card_score")}
    return earn_fn


def make_run_fn(model: str = T1_ARCHITECT) -> Callable[[dict], dict]:
    """A REAL run leaf: actually call a model (the floor_chat primitive) with the goal + the
    chain's built guidance, and report the run as a structured result. This is the SINGLE-SHOT
    real run — NOT the full agentic tool-loop (that is the orchestration spine, migrating later);
    it proves the chain composes a real model call + token accounting, off the $0 dry stub.
    A transport failure is reported as status='error' (the verify leaf then grades it), never raised
    — the chain's fail-fast is for a wrote-nothing build, not for the weather."""
    def run_fn(ctx: dict) -> dict:
        from echelon_engine.atoms.providers.floor_chat import _chat
        guidance = ctx.get("guidance", "")
        try:
            answer = _chat(model, guidance, ctx["goal"], max_tokens=1200, temperature=0.3)
            return {"status": "completed", "answer": answer, "steps": 1,
                    "tokens_in": 0, "tokens_out": 0}
        except Exception as e:
            return {"status": "error", "answer": "", "steps": 0, "detail": str(e)[:160]}
    return run_fn


def make_agentic_run_fn(registry, provider, model: str, *, max_steps: int = 8) -> Callable[[dict], dict]:
    """A REAL AGENTIC run leaf: the model drives a tool-loop over a sandboxed ToolRegistry — it
    proposes tool calls (read/write/edit/list/search), the registry EXECUTES them against disk, the
    results feed back, and it loops until the model returns text with no more tool calls (done) or the
    step cap is hit. This is what makes `run` actually PRODUCE artifacts on disk — so the verify gate
    grades a real outcome, not just 'the model answered'. It is the minimal agentic spine (model +
    real hands), NOT the full loop.py (convergence gates / reflexes / offload) — that larger spine
    migrates as its own weld; this proves the COMPOSITION end-to-end with real writes.

    Reports {status, answer, steps, tool_calls}. A transport error is reported (status='error'), not
    raised — the chain's fail-fast is for a wrote-nothing BUILD, not for the weather."""
    def run_fn(ctx: dict) -> dict:
        messages = [{"role": "system", "content": ctx.get("guidance", "")},
                    {"role": "user", "content": ctx["goal"]}]
        specs = registry.schemas()
        tool_calls_made = 0
        try:
            for step in range(max_steps):
                resp = provider.send(messages, model_id=model, tools=specs, temperature=0.3)
                if getattr(resp, "status", "success") != "success":
                    return {"status": "error", "answer": "", "steps": step,
                            "detail": f"provider status={resp.status}", "tool_calls": tool_calls_made}
                calls = getattr(resp, "tool_calls", None) or []
                if not calls:
                    # no more tool calls -> the model is done; its text is the final answer
                    return {"status": "completed", "answer": resp.content or "",
                            "steps": step + 1, "tool_calls": tool_calls_made}
                # record the assistant turn, then EXECUTE each tool call against the sandbox
                messages.append({"role": "assistant", "content": resp.content or "",
                                 "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in calls]})
                for c in calls:
                    result = registry.execute(c.name, c.args)
                    tool_calls_made += 1
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": str(result)[:4000]})
            return {"status": "completed", "answer": "(step cap reached)",
                    "steps": max_steps, "tool_calls": tool_calls_made}
        except Exception as e:
            return {"status": "error", "answer": "", "steps": 0,
                    "detail": str(e)[:160], "tool_calls": tool_calls_made}
    return run_fn


def make_verify_fn(artifact_path: str | None = None) -> Callable[[dict], tuple[bool, str]]:
    """A REAL verify leaf: grade the OUTCOME by a mechanical on-disk check, not transport success
    (the gates-by-mechanism law — 'Q = the model answered something' is the counterfeit). If an
    artifact_path is given, the run is verified iff that file exists AND is non-empty on disk. With
    no artifact_path, fall back to a transport check (completed + non-empty answer) — honest but
    weaker, flagged in the detail. Returns (ok, detail)."""
    def verify_fn(ctx: dict) -> tuple[bool, str]:
        r = ctx.get("run") or {}
        if artifact_path is not None:
            import os
            ok = os.path.isfile(artifact_path) and os.path.getsize(artifact_path) > 0
            return ok, f"artifact={artifact_path} exists_nonempty={ok}"
        ok = r.get("status") == "completed" and bool((r.get("answer") or "").strip())
        return ok, f"transport-check status={r.get('status')} (no on-disk artifact declared)"
    return verify_fn


def real_leaves(store: SeedStore, cards: CardStore, *, model: str = T1_ARCHITECT,
                artifact_path: str | None = None) -> dict:
    """Bundle the PRODUCTION leaves for a dispatch_chain(**real_leaves(...)) call — all four now REAL:
    warm = warmth/store recall, run = a real floor_chat model call, verify = an on-disk outcome check
    (or transport-check if no artifact declared), earn = cards trace-credit. The chain runs end-to-end
    on real atoms. (run is single-shot, not the full agentic tool-loop — that spine migrates later; but
    the COMPOSITION — real recall → real model call → real on-disk verify → real credit — is proven.)"""
    return {
        "warm_fn": make_warm_fn(store),
        "run_fn": make_run_fn(model),
        "verify_fn": make_verify_fn(artifact_path),
        "earn_fn": make_earn_fn(cards),
    }


def dry_leaves(store: SeedStore, cards: CardStore) -> dict:
    """warm + earn REAL, run + verify the $0 dry stub — the no-network composition proof (used by the
    existing test_dispatch_real_leaves suite, which asserts the chain shape without spending a model call)."""
    return {"warm_fn": make_warm_fn(store), "earn_fn": make_earn_fn(cards)}
