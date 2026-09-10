"""sleep — the off-line maintenance STATE: what the substrate does while not awake.

THE BIOLOGY (the name carries the reasoning). A brain does its housekeeping OFF-LINE — during sleep it
consolidates the day, replays what mattered, prunes weak synapses, and resolves conflicts, all while not
attending to the world. ECHELON already had `dream` (one consolidation pass — you DREAM while you SLEEP).
`sleep` is the containing STATE that runs the whole nightly cycle: dream (consolidate/promote) + the immune
scan (report broken structure) + OBSERVE twin-density and decayed-low atoms (NOT merge/prune — the DECAY
LAW already handles redundancy; see below). It is the mechanism behind ECHELON's promise "the estate gets
BETTER every session by itself" — made an explicit, budgeted, SAFE state instead of a hope.

DECAY HANDLES REDUNDANCY — SLEEP DOES NOT MERGE OR DELETE (owner 2026-06-20). Near-twin / "fake" atoms are
FINE: a twin that never earns DECAYS into the periphery (foveation just won't surface it); a twin that DOES
earn proved it was a real REFINEMENT, not a dupe. So sleep imports NONE of jcode's "merge at sim>0.95"
pressure — a forced merge would assert "these are the same" and could collapse a refinement into its parent
(the v1 sin). Sleep only OBSERVES (counts twins, surfaces decayed-low atoms) as health signals; the decay
law is the handler, not a maintenance action.

WHY A STATE, NOT JUST A SCRIPT: sleep is bounded by two human laws —
  • IT YIELDS TO WAKING. Active work suppresses sleep (you don't deep-sleep mid-conversation). A sleep
    cycle checks for waking and stands down — the owner returning is the alarm clock.
  • IT IS REFLEX-SAFE, AND ASKS BEFORE ANYTHING OUTWARD. Sleep does only what is local + reversible
    without consent (consolidate, scan, dedup-propose, prune-propose) — the two-tier safety law
    ([[jcode-ambient-mode-is-dream-productized]] safety system, which is ECHELON's owner-boundary made
    structural): TIER-1 REFLEX-SAFE = local/reversible/no-trace-outside → just do it; TIER-2 NEEDS-CONSENT
    = anything that leaves a trace outside the sandbox or talks to a human (push, PR, email, delete,
    deploy, spend) → NEVER in sleep; it is PREPARED and left for the waking owner to press. Sleep never
    presses the owner's button. And nothing is ever DELETED — pruning here means PROPOSE-for-prune; a weak
    atom is reported, not removed (the substrate's never-delete law).

CONSERVATIVE COLD START: a fresh estate sleeps CONSOLIDATE-ONLY (dream + scan); the heavier moves (dedup,
prune-proposal) unlock only once the estate has lived enough cycles to be trusted — earn-before-act applied
to the substrate's own upkeep. Keep ours: significance stays EARN-BY-TRACE — sleep proposes and reports,
the trace still does the actual weight-moving. This is a SUBSTRATE-axis organ (refines dream/immune), not a
new memory model. See [[jcode-ambient-mode-is-dream-productized]], dream, immune.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# The two-tier safety law (ECHELON's owner-boundary, made a classifier). The CORE RULE: anything that
# leaves a trace outside the local sandbox or communicates with a human is NEEDS-CONSENT — no exceptions.
REFLEX_SAFE = {            # TIER 1 — local, reversible, no outside trace. Sleep may do these unattended.
    "consolidate", "scan", "dedup_propose", "prune_propose", "extract_missed",
    "embed", "recall", "relink_propose", "read",
}
NEEDS_CONSENT = {          # TIER 2 — prepared in sleep, pressed only by the waking owner. NEVER auto-done.
    "push", "pull_request", "commit", "email", "post", "deploy", "delete_outside_sandbox",
    "install", "spend", "rotate_key", "comment_public",
}


def classify(action: str) -> str:
    """REFLEX-SAFE (do it asleep) vs NEEDS-CONSENT (prepare, leave for waking). Unknown actions default to
    NEEDS-CONSENT — the safe default is to ASK, never to assume an unlisted action is harmless."""
    if action in REFLEX_SAFE:
        return "reflex-safe"
    return "needs-consent"


@dataclass
class SleepReport:
    scope: str
    cycle_ts: int
    phases: list[dict] = field(default_factory=list)   # [{phase, did, detail}]
    prepared: list[dict] = field(default_factory=list)  # NEEDS-CONSENT items left for the waking owner
    woke_early: bool = False                            # waking detected -> stood down


def _awake(check_awake) -> bool:
    """Is the owner awake/working? sleep yields to waking. `check_awake` is an optional callable the caller
    supplies (e.g. 'is there an active session?'); absent -> assume asleep (the CLI runs deliberately)."""
    try:
        return bool(check_awake()) if check_awake else False
    except Exception:
        return False


def sleep_cycle(store, scope: str = "echelon", *, depth: str = "auto",
                check_awake=None, prune_floor: float = 0.05, dedup_sim: float = 0.95) -> SleepReport:
    """Run ONE off-line maintenance cycle over `scope`. Composes ECHELON's existing organs as sleep phases,
    each REFLEX-SAFE; anything outward is PREPARED, never done. Yields immediately if waking is detected.

    depth: 'consolidate' = dream + scan only (the conservative cold-start floor); 'full' = + dedup-propose
    + prune-propose + retroactive extract; 'auto' = consolidate on a young estate, full once it's lived
    enough (earn-before-act for the substrate's own upkeep). Returns a SleepReport — what it did + what it
    PREPARED for the owner. Never deletes; prune/dedup are PROPOSALS, the owner (or a later trace) decides.
    """
    from .cards import CardStore
    cs = store.cards if getattr(store, "cards", None) else CardStore()
    rep = SleepReport(scope=scope, cycle_ts=int(time.time()))

    if _awake(check_awake):
        rep.woke_early = True
        return rep   # don't deep-sleep mid-conversation

    # ── Impression TTL cleanup (operational telemetry, not memory) ──
    try:
        n = cs.evict_impressions(days=90)
        if n:
            rep.phases.append({"phase": "impressions", "did": "evict", "detail": f"{n} rows older than 90 days"})
    except Exception:
        pass  # best-effort cleanup

    # decide depth (auto): a scope that has lived enough cycles is trusted with the heavier moves.
    if depth == "auto":
        n = cs.count_atoms_in_scope(scope)
        depth = "full" if n >= 50 else "consolidate"

    # PHASE 1 — DREAM (consolidate/promote). REFLEX-SAFE: re-derives scores, nominates rises; deletes nothing.
    try:
        from .dream import consolidate
        r = consolidate(store, scope)
        rep.phases.append({"phase": "dream", "did": "consolidate",
                           "detail": {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}})
    except Exception as e:
        rep.phases.append({"phase": "dream", "did": "skipped", "detail": str(e)})

    # PHASE 2 — IMMUNE SCAN (the structure antibody). REFLEX-SAFE: scan() is READ-ONLY; healing a FAIL is a
    # mutation, so sleep PREPARES it (reports the broken edges) rather than auto-cutting them.
    try:
        from .immune import scan
        s = scan(scope=scope, store=cs)
        rep.phases.append({"phase": "immune", "did": "scan", "detail": s.get("stats", s)})
        fails = (s.get("fail") or [])
        if fails:
            rep.prepared.append({"action": "heal", "tier": classify("scan"),
                                 "why": f"{len(fails)} broken edge(s) found — heal is a mutation, owner reviews",
                                 "items": fails[:10]})
    except Exception as e:
        rep.phases.append({"phase": "immune", "did": "skipped", "detail": str(e)})

    if depth != "full":
        return rep   # conservative floor: a young estate sleeps lightly

    # PHASE 3 — TWIN-DENSITY (OBSERVE, do NOT merge). Owner 2026-06-20: dedup/fake atoms are FINE — the
    # DECAY LAW takes care of them. A near-twin that never earns DECAYS into the periphery (foveation just
    # won't surface it); a twin that DOES earn proved it was a real REFINEMENT, not a dupe. So sleep does
    # NOT propose merges (jcode's sim>0.95 merge imports a worry ECHELON doesn't have — and a forced merge
    # would assert "these are the same" and could collapse a refinement into its parent = the v1 sin). It
    # only REPORTS twin density as a health signal; decay is the handler, not a maintenance action. See
    # the-decay-law-handles-redundancy-no-merge.
    try:
        twins = _twin_count(cs, scope, dedup_sim)
        rep.phases.append({"phase": "twins", "did": "observe",
                           "detail": {"near_twins": twins, "handler": "decay-law (no merge)"}})
    except Exception as e:
        rep.phases.append({"phase": "twins", "did": "skipped", "detail": str(e)})

    # PHASE 4 — WEAK-SYNAPSE OBSERVE. Atoms decay has ALREADY pushed well below neutral with no earned use.
    # Sleep does not delete or even propose deletion (the never-delete law + decay already did the work) —
    # it just surfaces them so the owner CAN disclaim one if it's truly counterfeit. Decay is the mechanism;
    # this is a window onto what decay has done, not a second pruning law.
    try:
        weak = _prune_candidates(cs, scope, prune_floor)
        rep.phases.append({"phase": "weak", "did": "observe", "detail": {"decayed_low": len(weak)}})
        if weak:
            rep.prepared.append({"action": "review-weak", "tier": "reflex-safe",
                                 "why": "atoms decay pushed below neutral, never earned — REVIEW only, decay handles them",
                                 "items": weak[:10]})
    except Exception as e:
        rep.phases.append({"phase": "weak", "did": "skipped", "detail": str(e)})

    return rep


def _twin_count(cs, scope: str, sim_floor: float) -> int:
    """COUNT near-twin atom pairs (real-embedder cosine >= sim_floor) as a redundancy HEALTH SIGNAL — does
    NOT build merge proposals. The decay law is the handler: a twin that never earns decays into the
    periphery, a twin that earns was a real refinement (the-decay-law-handles-redundancy-no-merge). O(n^2)
    over a scope, gated to depth='full'; pure observation."""
    from echelon_sdk.minilm_embed import make_embedder
    atoms = cs.atoms_in_scope(scope)
    emb = make_embedder("auto")
    vecs = [emb.embed(a.content or "") for a in atoms]
    n = 0
    for i in range(len(vecs)):
        if not vecs[i]:
            continue
        for j in range(i + 1, len(vecs)):
            if vecs[j] and emb.similarity(vecs[i], vecs[j]) >= sim_floor:
                n += 1
    return n


def _prune_candidates(cs, scope: str, floor: float) -> list:
    """Atoms whose effective_score is below `floor`-equivalent AND that never earned a use — the weak-synapse
    candidates. NEVER returns for deletion; a candidate is REPORTED for the owner to disclaim if truly dead.
    (Score floor is on the 0-100 scale; an atom far below neutral with use_count 0 is the analogue of
    jcode's confidence<0.05 & strength<=1.)"""
    out = []
    for a in cs.atoms_in_scope(scope):
        eff, _ = cs.effective_score(a)
        earned = cs.struct_earned(a.id) or {}
        used = a.use_count or earned.get("use_count", 0)
        if eff < (50.0 + floor) and not used:   # well below neutral and never paid off
            out.append({"id": a.id[:12], "eff": round(eff, 1), "head": (a.content or "")[:60]})
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────
def _render(rep: SleepReport) -> None:
    if rep.woke_early:
        print("☼ awake — sleep stood down (don't deep-sleep mid-conversation).")
        return
    print(f"☾ SLEEP cycle over scope='{rep.scope}' — the off-line maintenance state.\n")
    for ph in rep.phases:
        print(f"  · {ph['phase']:<8} {ph['did']:<11} {ph.get('detail', '')}")
    if rep.prepared:
        print("\n  PREPARED for the waking owner (sleep never presses these):")
        for p in rep.prepared:
            print(f"    ⮕ [{p['tier']}] {p['action']}: {p['why']} ({len(p.get('items', []))} item(s))")
    print("\n  (nothing merged or deleted — the decay law handles redundancy; sleep only OBSERVES.)")


def _main(argv=None):
    import argparse
    import sys
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon sleep",
        description="The off-line maintenance STATE: consolidate + scan + dedup/prune-propose, reflex-safe, "
                    "yields to waking. What the substrate does while not awake.")
    ap.add_argument("--scope", default="echelon")
    ap.add_argument("--depth", choices=["auto", "consolidate", "full"], default="auto",
                    help="consolidate = dream+scan (cold-start floor); full = +observe twins/decayed-low; auto by estate age")
    ap.add_argument("--dedup-sim", type=float, default=0.95, help="near-twin cosine floor for the twin-density health signal")
    a = ap.parse_args(argv)
    from .store import SeedStore
    rep = sleep_cycle(SeedStore(), a.scope, depth=a.depth, dedup_sim=a.dedup_sim)
    _render(rep)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
