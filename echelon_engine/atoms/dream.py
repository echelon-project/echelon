"""dream — the consolidation organ. NOT sleep: a DREAM (owner, 2026-06-07).

At field-cold (end of a waking run), the dream replays the run's WORKING seeds and surfaces what
deserves to be remembered. The owner's hard constraints, proven first in sim_fork_field_v3.py:

  - 100% IN RAM, ISOLATED. dream() receives a SNAPSHOT (a plain list of Seed objects copied out of
    the store) and holds NO store handle. It CANNOT write the soul. It returns PROPOSALS only; the
    caller (awake) routes them. Whatever the dream builds in its locals evaporates on return.

  - WITNESSED resonance, not repetition. A conclusion earns a proposal when INDEPENDENT forks
    converge on it (same content-addressed id reached from independent re-derivation views) — NOT
    when one fork repeats itself (the cache-lie; the COS score already averages that away).

  - TWO EXITS (the respect handshake, dream-and-the-respect-handshake):
      A) eligible: NOT self_seed AND meets the requirement (score>=125 AND recalls>=3, witnessed-
         convergent over time) -> a global-soul candidate (grand vote / confirmation).
      B) self_seed: "the model WISHED it though the requirement was unmet" -> its own persona core,
         undeletable, permanently ineligible for the global soul.
    The exits are mutually exclusive; eligibility is decided by levels.eligible_for_global +
    levels.meets_requirement, the SAME predicates grandvote/confirmation consult.

  - NEVER DELETE. A seed that earns neither exit simply isn't proposed — it stays in working and
    falls out of recall by decay. The dream proposes; it never removes.

Dream forks here = deterministic re-derivation ($0, no LLM) — the same mechanism the sim proved.
A real LLM-driven dream (recombine/hypothesise) is a later same-interface swap: it still returns
Proposals and still holds no store handle.

See: dream-and-the-respect-handshake, compiler-era-fork-field, levels.py, store.self_seed.
"""
from __future__ import annotations

from dataclasses import dataclass

from echelon_sdk import levels
from .store import scope_to_domain as _scope_to_domain


@dataclass(frozen=True)
class Proposal:
    """A survivor of the dream. The caller routes it through its honest exit.
      exit == "global"    -> eligible candidate (grandvote.nominate / confirmation)
      exit == "self_seed" -> the model's wish (store.self_seed, own core)
    The dream never acts; it only proposes."""
    seed: object          # a Seed (kept duck-typed so dream.py doesn't import store.py)
    exit: str             # "global" | "self_seed"
    witnesses: int        # how many independent forks converged (0 for a pure wish)
    reason: str


def dream(working_snapshot: list, *, n_forks: int = 4,
          wishes: set | None = None, min_witnesses: int = 2,
          witness_counts: dict | None = None, earned_by_id: dict | None = None) -> list[Proposal]:
    """Consolidate a run's working seeds. PURE: no store handle, no writes, RAM discarded on return.

    Args:
        working_snapshot: a COPY of the run's working Seed objects (the store stays untouched).
        n_forks: kept for interface stability (the real LLM dream recombines across this many forks);
                 in the deterministic dream, convergence is read directly from the snapshot.
        wishes: optional set of seed ids the model explicitly WISHED to keep (Exit B). A wished id
                that did not meet the requirement becomes a self_seed proposal.
        min_witnesses: how many INDEPENDENT instances must converge to count as witnessed.

    WITNESSED = INDEPENDENT INSTANCES, NOT VIEWS. The cache-lie is one fork repeating itself; the
    truth is N forks SEPARATELY arriving at the same conclusion. On an append-only content-addressed
    store, independent arrivals hash to the SAME content-id, so the 2nd insert is an INSERT-OR-IGNORE
    NO-OP — NOT a distinct row (this is the bug the audit found: counting distinct rows pinned the
    witness count at 1 forever). The honest count comes from the WITNESS LEDGER (uame_witness): the
    number of DISTINCT arrival timestamps for a content-id (a different run = a different ts). Pass it
    in via `witness_counts={id: n}` (consolidate reads store.u.witness_count per snapshot seed). When a
    seed has no ledger entry (legacy, written before the ledger), its count is absent and we FALL BACK
    to the in-snapshot row count so legacy seeds are never UNDER-counted to 0.
    (A single agent's repeated up-votes raise that one row's SCORE, never its arrival count — score is
    the COS average, the cache-lie wall; witnesses is the independent-arrival count. Two walls.)

    Returns: proposals only (Exit A 'global' for witnessed+eligible+met; Exit B 'self_seed' for
    wished-but-unmet). A seed that is neither witnessed-eligible nor wished is NOT proposed — it
    stays working and falls out of recall (never deleted)."""
    wishes = wishes or set()
    earned_by_id = earned_by_id or {}

    # count INDEPENDENT instances per content-id (distinct rows that converged to the same id),
    # and keep the strongest instance as the representative.
    witness: dict[str, int] = {}
    by_id: dict[str, object] = {}
    for s in working_snapshot:
        sid = getattr(s, "id", None)
        if sid is None:
            continue
        witness[sid] = witness.get(sid, 0) + 1
        prev = by_id.get(sid)
        if prev is None or getattr(s, "score", 0) > getattr(prev, "score", 0):
            by_id[sid] = s

    witness_counts = witness_counts or {}
    proposals: list[Proposal] = []
    for sid, s in by_id.items():
        # Prefer the LEDGER's independent-arrival count (the honest wall); fall back to the in-snapshot
        # row count only when the ledger has nothing for this id (a legacy seed) — never under-count to 0.
        n_w = witness_counts.get(sid) or witness.get(sid, 0)
        # The LIVE v2 earning signal for this seed (effective_score + witnessed-door use_count), injected
        # by consolidate() from CardStore.atom_earned. Absent -> meets_requirement falls to the dead v1
        # fields (the tombstone in levels.py), which now never promote — correct: the v1 signal is dead.
        earned = earned_by_id.get(sid)
        # WITNESS-CONVERGENCE, RE-BASED (architect ruling, gate-request 2). True INDEPENDENT witnesses are
        # structurally impossible on a single-persona estate (the same reason grandvote cannot ratify), so
        # on the v2-EARNED branch ONLY the strongest available proof is 'proven across TIME': >=min_witnesses
        # witnessed door-uses that SPAN >=2 distinct days. use_count encodes the count; earned['day_spread']
        # (from score_history per-event ts) encodes the spread. When per-event ts is absent (day_spread<0),
        # we accept plain use_count>=min_witnesses but FLAG it in the reason (one enthusiastic session must
        # not launder convergence). The v1 path (earned is None) is UNCHANGED — n_w must clear the gate.
        earn_conv = False
        conv_note = ""
        if earned is not None:
            uc0 = int(earned.get("use_count", 0))
            spread = int(earned.get("day_spread", -1))
            if uc0 >= min_witnesses and spread >= 2:
                earn_conv = True
                conv_note = f"earned-convergence: {uc0} door-uses across {spread} days"
            elif uc0 >= min_witnesses and spread < 0:
                earn_conv = True
                conv_note = (f"earned-convergence UNVERIFIED-SPREAD: {uc0} door-uses but no per-event ts "
                             "(single-session laundering NOT ruled out)")
        witnessed = (n_w >= min_witnesses) or earn_conv
        # Exit A: witnessed (independent instances OR earned-across-time), eligible, and earned the RE-BASED bar.
        if (witnessed and levels.eligible_for_global(s)
                and levels.meets_requirement(s, earned=earned)):
            eff = float((earned or {}).get("effective_score", getattr(s, "score", 0)))
            uc = int((earned or {}).get("use_count", getattr(s, "recall_count", 0)))
            base = (f"witnessed by {n_w} independent instances" if n_w >= min_witnesses
                    else conv_note)
            proposals.append(Proposal(
                seed=s, exit="global", witnesses=(n_w if n_w >= min_witnesses else int((earned or {}).get("use_count", 0))),
                reason=f"{base}; v2 earning met (effective_score {eff:.0f}, witnessed-uses {uc})"))
        # Exit B: the model wished it, though it did not earn eligibility.
        elif sid in wishes:
            proposals.append(Proposal(
                seed=s, exit="self_seed", witnesses=n_w,
                reason="wished by the model; requirement unmet (self_seed -> own core, ineligible)"))
        # else: neither — stays working, falls out of recall by decay. Never deleted, never proposed.
    return proposals


def consolidate(store, scope: str, *, wishes: set | None = None, n_forks: int = 4,
                grandvote=None) -> dict:
    """The AWAKE bridge — the ONLY place the dream's output touches the live store. Fires at
    field-cold. Snapshots the run's working seeds OUT of the store (a copy), runs the PURE dream
    on the copy (the store handle never enters dream()), then routes each proposal through its
    honest exit:
      - exit 'global'    -> grandvote.nominate (a candidate to rise; the eligibility guard there
                            is belt-and-suspenders — the dream already excluded self_seed).
      - exit 'self_seed' -> store.self_seed (the model's wish -> own core, undeletable, ineligible).
    Returns a report {proposals, nominated, self_seeded, skipped}. Does NOT delete anything; seeds
    that earned neither exit stay working and fall out of recall by decay (never deleted).

    grandvote: an optional GrandVote instance; if None, 'global' proposals are reported but not
    nominated (the caller may route them later). store.self_seed always runs for wishes."""
    # 1) snapshot working seeds for this scope OUT of the store (the dream gets a COPY)
    snapshot = list(store.seeds(scope=scope, tier="working"))

    # 1b) read the WITNESS LEDGER for each snapshot seed (audit #1): the count of INDEPENDENT arrivals
    # (distinct write timestamps) that converged on this content-id. This is the only honest witness
    # signal on a content-addressed store — the dream itself stays PURE (no store handle), it just
    # receives the counts. Best-effort: a store without the ledger yields {} and dream falls back to
    # the in-snapshot row count.
    witness_counts: dict = {}
    witness_ledger_broken = False   # surfaced as a standing-gap line so the owner sees the second dead gate
    u = getattr(store, "u", None)
    if u is not None and hasattr(u, "witness_count"):
        for s in snapshot:
            sid = getattr(s, "id", None)
            if sid is not None:
                try:
                    n = u.witness_count(sid)
                    if n:
                        witness_counts[sid] = n
                except Exception:
                    # the uame_witness (arrival_id) ledger is legacy/absent on this bank — witness_count
                    # raises. That is the SECOND dead gate (independent of the earning gate): the dream's
                    # convergence check has no ledger to read. We fall back to earned-across-time (below)
                    # and surface the breakage. Only flag once (any raise means the ledger is unusable).
                    witness_ledger_broken = True

    # 1c) read the LIVE v2 EARNING SIGNAL for each snapshot seed (the re-base, addendum 2026-07-08).
    # In v1-detached mode store.seeds() returns v2 atoms with id == the v2 atom id, so atom_earned keys
    # match directly. effective_score = the witnessed-door earned weight (max of atoms.score and
    # atom_earned.score when used); use_count = witnessed-USE events (door only — reads never earn). This
    # is what meets_requirement() now gates on; the dead v1 recall_count is bypassed. Best-effort: a
    # store without v2 yields {} and the dream falls back to the (dead) v1 fields → nothing promotes.
    earned_by_id: dict = {}
    cards = getattr(store, "cards", None)
    if cards is not None and hasattr(cards, "struct_earned"):
        for s in snapshot:
            sid = getattr(s, "id", None)
            if sid is None:
                continue
            try:
                er = cards.struct_earned(sid)          # {score, use_count} from atom_earned, or None
                if not er:
                    continue
                atom = cards.get_atom(sid)
                # effective_score honors the disclaim floor + the used-atom max, exactly like the query.
                eff = cards.effective_score(atom)[0] if atom is not None else float(er.get("score", 0.0))
                # day_spread: distinct calendar days the witnessed door-uses span (proven-across-time).
                # -1 signals 'no per-event ts available' so the dream can flag unverified spread.
                spread = cards.earn_day_spread(sid) if hasattr(cards, "earn_day_spread") else -1
                earned_by_id[sid] = {"effective_score": float(eff),
                                     "use_count": int(er.get("use_count", 0)),
                                     "day_spread": spread}
            except Exception:
                pass

    # 2) the PURE dream — no store handle crosses this call
    proposals = dream(snapshot, n_forks=n_forks, wishes=wishes, witness_counts=witness_counts,
                      earned_by_id=earned_by_id)

    # 3) route each proposal through its honest exit (this is the awake side, store writes allowed)
    nominated, self_seeded, skipped = [], [], []
    for p in proposals:
        if p.exit == "self_seed":
            new_id = store.self_seed(scope=scope, content=p.seed.content,
                                     kind=getattr(p.seed, "kind", "lesson"),
                                     coordinate=getattr(p.seed, "coordinate", ""),
                                     valence=getattr(p.seed, "valence", 0.0),
                                     arousal=getattr(p.seed, "arousal", 0.0))
            # The working candidate BECAME the wished core conviction. A content-addressed self_seed
            # has the SAME id as its working source, so both rows would now exist (the working copy,
            # self_seed=0, shadowing the core copy, self_seed=1 — the duplicate-source bug that lets
            # a self-seed read as eligible). Expire the working copy: the wish CONSUMED it (like a
            # promotion). expire() can only touch working_* — the soul wall keeps core safe; this is
            # append-only-honest (the working candidate was always ephemeral).
            try:
                dom = _scope_to_domain(scope, getattr(p.seed, "coordinate", ""))
                store.u.expire(new_id, dom)
            except Exception:
                pass
            self_seeded.append({"id": new_id, "content": p.seed.content[:60], "reason": p.reason})
        elif p.exit == "global":
            # PROMOTION in v2 = crossing the EARNED threshold, append-only (owner's ruling, addendum
            # 2026-07-08). The dream already proved eligibility (meets_requirement gated on the LIVE v2
            # effective_score+use_count). There is NO working->core row-move in v2 (the atoms table has no
            # tier column — tier is earned-not-asserted): the atom IS promoted by having crossed the bar.
            # We still try the v1 store.promote for a genuine v1 seed (back-compat, additive) — but a
            # v2-native atom whose id isn't in a v1 table returns False there, which is NOT a failure here.
            was_earned = p.seed.id in earned_by_id          # the atom carried the live v2 earning signal
            try:
                v1_promoted = store.promote(p.seed.id, scope)   # v1 row-move (False for a v2-native atom)
            except Exception:
                v1_promoted = False
            promoted = was_earned or v1_promoted
            if promoted:
                # grandvote.nominate is belt-and-suspenders (dream already excluded self_seed). On this
                # SINGLE-PERSONA estate ratify() can never fire (needs >=3 personas) — so we REPORT the
                # candidate and NEVER fake a quorum; L1 minting stays gated on a real grand vote.
                if grandvote is not None:
                    res = grandvote.nominate(p.seed.id, scope)
                    (nominated if res.get("ok") else skipped).append(
                        {"id": p.seed.id, "content": p.seed.content[:60],
                         "witnesses": p.witnesses, "ok": res.get("ok"),
                         "reason": res.get("reason", p.reason)})
                else:
                    nominated.append({"id": p.seed.id, "content": p.seed.content[:60],
                                      "witnesses": p.witnesses, "ok": None,
                                      "reason": p.reason + " — earned candidate (grand vote gated)"})
            else:
                skipped.append({"id": p.seed.id, "content": p.seed.content[:60],
                                "witnesses": p.witnesses, "ok": False,
                                "reason": "not promotable (no live v2 earning and v1 gate unmet)"})
    # SOUL-REPAIR BY MECHANISM (audit #7, owner: "soul repair is protocol, not maintenance"). The dream
    # is the soul's self-maintenance organ, so disclaiming a judged-origin counterfeit (a self-rated
    # score — "Q from a model's say-so", forbidden by the cards.py spine) belongs HERE, at field-cold,
    # caught structurally — NOT by the OS hand-editing the db. find_judged_origin names them by pattern;
    # disclaim_judged re-bases each BELOW neutral (a discovered lie ranks under a fresh card) and stamps
    # the receipt — never deletes (the lie stays legible, and the memory of being lied-to is itself
    # load-bearing). A disclaimed card can still re-earn through the real trace loop (and earns HARDER if
    # it does — the shock multiplier; see reinforce_card). Best-effort; never breaks the dream.
    disclaimed = {"cards": 0, "atoms": 0}
    cards_store = getattr(store, "cards", None)
    if cards_store is not None and hasattr(cards_store, "find_judged_origin"):
        try:
            found = cards_store.find_judged_origin()
            for cid in found.get("cards", []):
                if cards_store.disclaim_judged("cards", cid, reason="dream").get("ok"):
                    disclaimed["cards"] += 1
            for aid in found.get("atoms", []):
                if cards_store.disclaim_judged("atoms", aid, reason="dream").get("ok"):
                    disclaimed["atoms"] += 1
        except Exception:
            pass

    # THE STANDING GAP (architect's ruling): name it every dream run so it surfaces to the owner. An
    # earned candidate can be NOMINATED but never RATIFIED into L1 on this estate — grandvote.ratify
    # needs MIN_PERSONAS>=3 and a quorum, and this is a single-persona estate. Promotion to the earned
    # tier (L2/beacon) is real and append-only; the CONSTITUTION (L1) waits, correctly, for a real vote.
    l1_gap = None
    if nominated:
        l1_gap = ("%d earned candidate(s) nominated but L1 ratification is GATED: grandvote needs "
                  ">=3 personas + quorum; this is a single-persona estate, so no vote can ratify. The "
                  "earned tier is conferred (append-only); the constitution waits for a real grand vote."
                  ) % len(nominated)

    # SECOND standing gap (architect ruling gate-request 2): the witness ledger is dead, so the dream's
    # independent-convergence check has no data — the re-based path leans on earned-across-time instead.
    # Surface it every run so the owner sees BOTH gaps, not just the L1 one.
    witness_gap = None
    if witness_ledger_broken:
        witness_gap = ("witness ledger (uame_witness.arrival_id) is legacy/absent — witness_count() "
                       "raises, so independent-convergence cannot be read. The re-based promotion path "
                       "falls back to earned-across-time (>=3 witnessed door-uses spanning >=2 days). "
                       "Rebuilding the ledger is a separate v1-organ repair, out of this slice.")

    return {"scope": scope, "proposals": len(proposals),
            "nominated": nominated, "self_seeded": self_seeded, "skipped": skipped,
            "disclaimed": disclaimed, "l1_gap": l1_gap, "witness_gap": witness_gap}
