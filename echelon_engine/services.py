"""echelon_engine.services — THE GATE (the one public surface of the engine).

External callers (apps/cli, apps/web, apps/world) import ONLY this module for
orchestration. Private subdivisions live in echelon_engine/services_<domain>.py
and stay internal — the scanner forbids importing them from outside the engine.

This gate file NEVER implements; it re-exports the chain entrypoints from the
private services_*.py children (memory, dispatch, world) once they are migrated.
Right now it is an empty gate — the skeleton stage. Flows land here as the
migration moves each vertical (dispatch first).
"""
from __future__ import annotations

# The gate dispatches to private chain entrypoints in services_*.py children.
# (More land here as verticals migrate: services_memory, services_world.)
from echelon_engine.services_dispatch import dispatch_chain
from echelon_engine.services_dispatch_atoms import (
    real_leaves, dry_leaves, make_warm_fn, make_earn_fn, make_agentic_run_fn, make_verify_fn,
)

# The MEMORY vertical — in-process bank reads for the apps layer (apps/web, apps/cli).
# Apps import these THROUGH this gate; the scanner forbids apps -> echelon_engine.atoms.
from echelon_engine.services_memory import (
    bank_stats, list_atoms, get_atom, search_atoms, list_arc_cards, relive_chain,
    cartridge_list, provider_config, integrity_scan, bank_health,
    list_scopes, atlas_html, cartridge_inspect, provider_key_status, provider_usage,
    gate_banner, set_user_banner, install_guide,
    provision_mcp_user, rotate_mcp_user, mcp_user_exists, mcp_user_by_email,
    mcp_user_by_token, mcp_bearer_token, mcp_token_for_email,
    recovery_issue_code, recovery_check_code, recovery_find_account,
    remember_lesson,
)

# The STUDIO vertical — UI-tree generation + the HTML round-trip for apps/studio (the visual UI
# gateway / rework tool). Apps call these THROUGH this gate; the provider import stays in-engine.
from echelon_engine.services_studio import (
    generate_ui_tree, studio_tiers, import_html, export_html,
)


def run_dispatch(goal: str, **kwargs):
    """The public dispatch entry: run the dispatch chain and REQUIRE it (a failed step raises
    FrameworkError, the honest fail-fast). Returns the chain's enriched ctx on success. Callers
    (apps, the DispatchBoard) use THIS, never services_dispatch directly (scanner-enforced)."""
    result = dispatch_chain(goal, **kwargs).require()
    return result.value


def dispatch_result(goal: str, **kwargs):
    """Same as run_dispatch but returns the full ChainResult (steps + outcome) WITHOUT raising —
    for callers that want the per-step record even on failure (the trace-card sink, the console)."""
    return dispatch_chain(goal, **kwargs)


def dispatch_with_atoms(goal: str, store, cards, **kwargs):
    """Run the dispatch chain with REAL warm + earn leaves (warmth/store recall + cards trace-credit),
    run + verify on the $0 DRY shape (no model call). The no-network composition proof: the chain runs
    on real recall + real credit without spending a token. Caller may override any leaf via kwargs."""
    leaves = dry_leaves(store, cards)
    leaves.update(kwargs)        # caller may still override any leaf (e.g. inject run/verify)
    return dispatch_chain(goal, **leaves)


def dispatch_live(goal: str, store, cards, *, model=None, artifact_path=None, **kwargs):
    """Run the dispatch chain END-TO-END on ALL real leaves: warm (recall) → run (a real floor_chat
    model call) → verify (on-disk artifact check, or transport-check if no artifact) → earn (trace
    credit). This SPENDS a model call. `artifact_path` makes verify grade a real on-disk outcome (the
    gates-by-mechanism law); omit it for a transport-check. Returns the full ChainResult."""
    kw = {} if model is None else {"model": model}
    leaves = real_leaves(store, cards, artifact_path=artifact_path, **kw)
    leaves.update(kwargs)
    return dispatch_chain(goal, **leaves)


def dispatch_agentic(goal: str, store, cards, *, registry, provider, model: str,
                     artifact_path=None, max_steps: int = 8, **kwargs):
    """Run the dispatch chain with a REAL AGENTIC run leaf: the model drives a tool-loop over the
    sandboxed `registry` (it reads/writes/edits real files on disk), then verify grades the on-disk
    outcome and earn credits the trace. This is the fullest composition — warm(recall) → AGENTIC
    run(model + real hands → artifacts on disk) → verify(on-disk) → earn(credit). The model+registry+
    provider are injected so the caller owns the sandbox + the spend. Returns the full ChainResult."""
    leaves = {
        "warm_fn": make_warm_fn(store),
        "run_fn": make_agentic_run_fn(registry, provider, model, max_steps=max_steps),
        "verify_fn": make_verify_fn(artifact_path),
        "earn_fn": make_earn_fn(cards),
    }
    leaves.update(kwargs)
    return dispatch_chain(goal, **leaves)


# ── Chat (OS_TIER) ────────────────────────────────────────────────────────────

from echelon_engine.services_chat import chat_turn


__all__ = [
    "run_dispatch", "dispatch_result", "dispatch_with_atoms", "dispatch_live",
    "dispatch_agentic", "chat_turn", "remember_lesson",
]
