"""The matcher — warmth, not retrieval. THE breakthrough (owner, 2026-06-05).

The old failure: agonize over storing the 'right' clean seed, then INJECT it back —
model goes cold-but-loaded (the cache lie / theater). The fix: the seed can be anything
(messy is fine); the organ's job is to score the model's CURRENT REASONING against the
whole store and return a WARMTH — not the memory.

  warm  -> "you've been here / done this before" -> re-tread, the weight should re-form
  cold  -> "wrong recall, or genuinely new territory" -> explore, make a NEW seed

This is the canary inverted into a compass: the canary detected drift by ABSENCE; warmth
detects recognition by OVERLAP. We do NOT inject context — we tell the model how warm its
own thought is against its past, and let it RE-CHOOSE (rediscovery). This is
/memories-warm-up's warmth-gate as a live runtime signal.

STONE ONE = the lexical floor: deterministic, $0, always-on. n-gram overlap of the
reasoning's words/phrases against each seed. Crude but free and verifiable. A semantic
(embedding) tier escalates the ambiguous mid-range LATER — tiered like the compute
strategy (cheap floor, pay for precision only when the cheap signal can't decide).
Building lexical alone first so warm/cold is provable before non-deterministic embeddings.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

from .store import SeedStore, Seed

_WORD = re.compile(r"[a-z0-9]+")
# Drop ultra-common words so warmth reflects MEANING-bearing overlap, not "the/and/to".
_STOP = frozenset(
    "the a an and or but to of in on at for with by is are was were be been being "
    "this that these those it its as do did does done from not no yes i you he she we "
    "they them his her their our your my me will would can could should have has had "
    "if then else when what which who how why where".split()
)


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2]


def _ngrams(tokens: list[str], n: int) -> set[tuple]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)} if len(tokens) >= n else set()


def _overlap(a: set, b: set) -> float:
    """Fraction of A's grams present in B (asymmetric: how much of the REASONING is recognized)."""
    return len(a & b) / len(a) if a else 0.0


@dataclass
class SeedWarmth:
    seed: Seed
    score: float
    via_scope: str = ""   # if cross-scope: the related scope this was drawn from (the soul travelling)
    via_rel: str = ""     # the atlas relationship the warmth crossed


@dataclass
class WarmthReading:
    score: float                    # 0..1 recognition warmth of the reasoning vs the scope
    verdict: str                    # "warm" | "lukewarm" | "cold"
    warmest: list[SeedWarmth]       # the seeds the reasoning most overlaps (NOT injected — surfaced)
    guidance: str                   # what the model should DO (the emotion's action-tendency)
    emotion: str = "neutral"        # the DERIVED named feeling (circumplex; see affect.py)
    valence: float = 0.0            # -1 unpleasant .. +1 pleasant (from the warmest seed / judge)
    arousal: float = 0.0            #  0 calm .. 1 intense
    tier: str = "lexical"           # which tier produced the score: lexical | judged
    judge_tokens: tuple = (0, 0)    # (in, out) if the judge process ran


# Tunable thresholds — the lexical floor's warm/cold bands.
WARM = 0.45
LUKEWARM = 0.18
# How much warmth a seed passes along a uame_links edge to a linked seed (the keystone:
# a concrete memory lights up the abstract value it instances). <1 so recognition fades with
# distance — the concrete hit stays warmest, the value it reaches is real but secondary.
EDGE_DECAY = 0.7
# MULTI-HOP: warmth travels a CHAIN, not just one edge. action --instances--> lesson --refines-->
# value: a concrete recognition should reach the value two hops away, fainter each hop (EDGE_DECAY^h).
# Bounded so the soul-graph doesn't flood: stop at MAX_HOPS or when reached warmth < LUKEWARM.
MAX_HOPS = 3
# A seed whose atom has a LIVE successor (`supersedes` edge pointing at it) keeps only this
# fraction of its lexical warmth — history stays reachable but can never outrank the successor.
SUPERSEDED_DAMP = 0.5
# Escalation: if lexical found plausible candidates but didn't land clearly WARM, the floor
# may be missing same-meaning-different-words recognition (proven on the gz reasoning task).
# A candidate at/above this overlap is "worth a second, process-based look" by the judge.
JUDGE_FLOOR = 0.05


def _lex_score(r_uni: set, r_bi: set, seed: Seed) -> float:
    s_tokens = _tokens(seed.content)
    uni = _overlap(r_uni, set(s_tokens))
    bi = _overlap(r_bi, _ngrams(s_tokens, 2))
    return 0.6 * uni + 0.4 * bi


# THE V2-PRIMARY READ-FLIP (2026-06-16). The law (v2-is-primary-v1-is-cold-borrow, Mol L1582): v2 is
# the store recall ranks; v1 is the cold-borrow source only. warmth() ranks v1 SEEDS (they carry the
# uame_links soul the edge-walk needs), but the RANK must be governed by the v2 effective_score so the
# v2-only honesty mechanisms — dispute (down-correct) and trace-earn (up) — decide what surfaces. We do
# NOT replace lexical recognition with weight (that would surface a proven-but-irrelevant atom over an
# on-topic one); we MODULATE: lexical = "does the reasoning match", v2 weight = "has it proven out / been
# disputed", orthogonal signals combined. The multiplier is centered at 1.0 on a NEUTRAL atom (score=B),
# so an un-earned/un-matched seed is unchanged (pure back-compat); a disputed atom (JUDGED_FLOOR=75) is
# dampened below 1; an earned atom (>B) lifts above 1. Bounded so weight tilts ties, never lets a high-
# weight off-topic atom leap a strong lexical hit. effective_score already honors the cold-borrow + the
# anti-laundering guard — this is purely the RANK application of it. See v2-primary-code-gap-lossy-shadow.
_V2_B = 100.0            # the COS neutral benchmark (uame.SCORE_BENCHMARK) — the multiplier's pivot
_V2_TILT_LO = 0.5       # floor: a maximally-disclaimed atom keeps half its lexical warmth (never zeroed —
                        # the dispute GATE already hard-drops a disclaimed atom; this only orders the rest)
_V2_TILT_HI = 1.5       # ceiling: a strongly-earned atom gets at most +50% — tilts ties, can't leap topic


def _v2_multiplier(effective: float) -> float:
    """Map a v2 effective_score (COS spine, neutral=100) to a rank multiplier in [_V2_TILT_LO, _V2_TILT_HI],
    1.0 at neutral. Linear around B with a gentle slope, then clamped. None/neutral -> 1.0 (no-op)."""
    if effective is None:
        return 1.0
    m = 1.0 + (effective - _V2_B) / _V2_B      # 75 -> 0.75, 100 -> 1.0, 150 -> 1.5
    return max(_V2_TILT_LO, min(_V2_TILT_HI, m))


# L2 insight kinds: an insight-CLAIM that must be CONFIRMED to be trusted ("unconfirmed = lie").
# A working-tier seed of one of these kinds is an L2 candidate; until confirmed it is INVISIBLE to
# warmth (a lie can't be recalled as truth). Core-tier (L1) is always trusted; non-insight kinds
# (lessons-as-logs, notes, reasons) aren't claims subject to the gate. See confirmation.py.
_L2_INSIGHT_KINDS = frozenset({"insight", "lesson", "conclusion", "synthesis-proposal"})


def warmth(reasoning: str, store: SeedStore, scope: str, top_k: int = 3,
           judge_provider=None, judge_model: str = "grok-4.3", scope_graph=None,
           reinforce: bool = False, confirmation=None, since_ts: int | None = None,
           pre_filter: str = "", include_dormant: bool = False) -> WarmthReading:
    """Score the model's CURRENT reasoning against the store. Return a temperature, not memory.

    SINCE PRE-FILTER (since_ts, 2026-06-23, council-decided — memanto hybrid-timeline Level-1 steal):
    when given, the candidate SET is narrowed to seeds with ts >= since_ts BEFORE warmth ranks. Time is a
    FILTER on visibility, NOT a re-weighting — "recent" and "warm" (earned-by-trace) are orthogonal axes and
    are never conflated (the council's binding guard). The order among the survivors is still earned by
    warmth, unchanged. since_ts=None -> no filter (back-compat). See [[memanto-steal-hybrid-timeline-recall-since]].

    Tiered ([[compute-tiering-strategy]]): lexical floor (free, always-on) decides the clear
    ends; the AMBIGUOUS middle escalates to a judge PROCESS (recognition is an act, not a
    vector lookup — owner: "weight are process-based"). judge_provider=None -> lexical only.

    CROSS-SCOPE (the soul travelling): if scope_graph is given, also match the current scope's
    ATLAS NEIGHBOURS' seeds, scaled by the edge weight — drawn from the relationship-index that
    IS the soul (owner: "atlas is your answer"). A neighbour match is FLAGGED (via_scope/via_rel)
    so the agent knows the recognition came from a related project's experience.

    L2 CONFIRMATION GATE (owner: "unconfirmed = lie"): if a ConfirmationLedger is given, an
    UNCONFIRMED working-tier insight-claim is filtered OUT — a lie cannot be recalled as truth.
    L1 (core) is always trusted; only L2 insight-CLAIMS are gated. confirmation=None -> no gate
    (back-compat; the gate engages only where the ledger is wired)."""
    r_tokens = _tokens(reasoning)
    # Blend unigram + bigram overlap: unigrams catch topic, bigrams catch phrasing/recognition.
    r_uni, r_bi = set(r_tokens), _ngrams(r_tokens, 2)

    all_seeds = store.seeds(scope=scope)
    # FTS PRE-FILTER (2026-06-26): when pre_filter is given, use the FTS5 index
    # on atom_spine to narrow candidates before O(N) n-gram scoring. Reduces the
    # candidate set from 800+ atoms to ~20-50. Matches by slug (atom_spine.slug)
    # against seed coordinate suffixes — v1 and v2 use different ID schemes but
    # share coordinates (e.g. 'echelon:memory-is-a-weight-adjustor').
    if pre_filter:
        try:
            from .cards import CardStore
            cs = CardStore()
            fts_candidates = cs.search_spine_candidates(pre_filter, scope=scope, limit=50)
            if fts_candidates:
                slugs = {c["slug"] for c in fts_candidates if c.get("slug")}
                if slugs:
                    filtered = []
                    for s in all_seeds:
                        coord = s.coordinate or ""
                        # Extract slug: everything after the last ':' in the coordinate
                        coord_slug = coord.rsplit(":", 1)[-1] if ":" in coord else coord
                        # Also check by substring for coordinates that embed the slug differently
                        if coord_slug in slugs or any(slug in coord for slug in slugs if len(slug) > 10):
                            filtered.append(s)
                    if filtered:
                        all_seeds = filtered
        except Exception:
            pass  # FTS unavailable — fall through to full scan (never crash recall)
    if since_ts is not None:
        # Pre-filter the candidate set by recency BEFORE ranking (council directive). A pure visibility
        # narrow — warmth still earns the order among survivors; recency does not touch the score.
        all_seeds = [s for s in all_seeds if getattr(s, "ts", 0) >= since_ts]
    if confirmation is not None:
        # Drop unconfirmed L2 insight-claims (working tier + an insight kind). A lie is invisible
        # until it earns confirmation by vote. L1/core and non-claim kinds pass untouched.
        kept = []
        for s in all_seeds:
            is_l2_claim = (s.tier != "core") and (s.kind in _L2_INSIGHT_KINDS)
            if is_l2_claim and not confirmation.is_confirmed(s.id, scope):
                continue   # unconfirmed insight = a lie; not recalled as truth
            kept.append(s)
        all_seeds = kept

    # THE DISPUTE GATE (2026-06-16): honor a disclaim at READ time. warmth ranks by LEXICAL overlap
    # and never consults bank-score, so dispute()/disclaim — which lowers a v2 atom's score — would be
    # INVISIBLE here: a stale atom marked wrong would surface as warm as the truth. So we drop any seed
    # whose content matches a DISCLAIMED v2 atom (judged-mark on born_from), keyed by the same content-
    # prefix the cold-borrow uses. This is the read-side completion of the dispute organ: dispute() marks
    # it in v2, warmth() refuses to surface it as truth — exactly as the confirmation gate above drops an
    # unconfirmed insight. A disclaimed atom is NEVER deleted (its row + receipt stay legible for audit/
    # redemption); it just stops being RECALLED as truth. Best-effort: if v2 is unreadable the set is
    # empty and recall degrades to un-gated (never crashes). Redemption (a trace earn that wipes the mark)
    # removes the content from the set on the next read, so a redeemed atom surfaces again.
    try:
        _disclaimed = store.cards.disclaimed_contents() if getattr(store, "cards", None) else set()
    except Exception:
        _disclaimed = set()
    if _disclaimed:
        all_seeds = [s for s in all_seeds if (s.content or "")[:120] not in _disclaimed]

    # THE READ-FLIP (2026-06-16): the v2 effective_score map governs RANK. Built once per call (one
    # query, content-prefix keyed), best-effort {} so recall degrades to pure-lexical if v2 is
    # unreadable. _v2w(seed) -> the multiplier for that seed's earned/corrected v2 weight (1.0 if no
    # v2 twin or neutral). See v2-primary-code-gap-lossy-shadow.
    try:
        _v2map = store.cards.effective_scores_by_content(scope=scope) if getattr(store, "cards", None) else {}
    except Exception:
        _v2map = {}

    def _v2w(seed: Seed) -> float:
        return _v2_multiplier(_v2map.get((seed.content or "")[:120]))

    scored: list[SeedWarmth] = []
    for seed in all_seeds:
        score = _lex_score(r_uni, r_bi, seed) * _v2w(seed)   # own seeds, rank tilted by v2 weight
        if score > 0:
            scored.append(SeedWarmth(seed=seed, score=round(score, 3)))

    # CROSS-SCOPE: draw neighbour scopes' seeds along atlas edges, scaled by edge weight.
    if scope_graph is not None:
        for nb_scope, weight, rel in scope_graph.neighbours(scope):
            # neighbour scope -> its own v2 map (so a neighbour's earned weight tilts too).
            try:
                _nbmap = store.cards.effective_scores_by_content(scope=nb_scope) if getattr(store, "cards", None) else {}
            except Exception:
                _nbmap = {}
            for seed in store.seeds(scope=nb_scope):
                base = _lex_score(r_uni, r_bi, seed) * _v2_multiplier(_nbmap.get((seed.content or "")[:120]))
                if base > 0:
                    scored.append(SeedWarmth(seed=seed, score=round(base * weight, 3),
                                             via_scope=nb_scope, via_rel=rel))

    # ATOM-GRAIN CLAIMS (owner 2026-07-24: "echelon could claim that atom" — the per-atom sibling
    # of the group-scope reach). A `claims` atlas edge carries an `atoms` slug filter: the claimed
    # atom STAYS in its home scope (id, coordinate, earned weight untouched — no re-scope, no dup)
    # and recall draws EXACTLY those coordinates at the claim weight (1.0 — adoption, not neighbour
    # reach). If a seed also arrived via a whole-scope edge, the STRONGEST reading wins (no double-
    # count). This is the filing-mix-up fix that needs no mop-up: misfiled ≠ unreachable.
    if scope_graph is not None and hasattr(scope_graph, "claims"):
        by_seed_id = {sw.seed.id: sw for sw in scored}
        # the store normalizes coordinates to snake_case (gamma-support:reflex-x ->
        # gamma-support:reflex_x) — normalize BOTH sides or the filter silently never matches
        _norm = lambda t: (t or "").lower().replace("-", "_")
        for nb_scope, weight, slugs in scope_graph.claims(scope):
            coords = {f"{_norm(nb_scope)}:{_norm(s)}" for s in slugs}
            try:
                _nbmap = store.cards.effective_scores_by_content(scope=nb_scope) if getattr(store, "cards", None) else {}
            except Exception:
                _nbmap = {}
            for seed in store.seeds(scope=nb_scope):
                if _norm(seed.coordinate) not in coords:
                    continue
                base = _lex_score(r_uni, r_bi, seed) * _v2_multiplier(_nbmap.get((seed.content or "")[:120]))
                if base <= 0:
                    continue
                sw = SeedWarmth(seed=seed, score=round(base * weight, 3),
                                via_scope=nb_scope, via_rel="claims")
                prior = by_seed_id.get(seed.id)
                if prior is None:
                    scored.append(sw)
                    by_seed_id[seed.id] = sw
                elif sw.score > prior.score:
                    scored[scored.index(prior)] = sw
                    by_seed_id[seed.id] = sw

    # ── SUPERSEDED DAMPENER (2026-08-12) — honor live `supersedes` edges at rank time ──
    # The gap: warmth honored disclaimed (dropped) and dormant (filtered) but not SUPERSEDED —
    # an atom with a live successor ranked as if current, so a stale June handoff outranked the
    # August wrap on lexical overlap. Claim-level and structural (reads the edge, not the clock),
    # so it respects the council guard that recency never re-weights. DAMPEN, don't drop: the
    # atom is history, not a lie — it still surfaces when it is the only recognition, it just
    # cannot outrank its successor. Same content-prefix bridge + live-bearer veto as dormant.
    # MUST run BEFORE the edge-walk below: the first live gate showed the un-damped handoff
    # spreading its full 1.35 warmth to 3 neighbors (1.35*0.7=0.945 top hits) and then getting
    # damped itself — the stale spine kept flooding the top-k through its edges. Damp first,
    # THEN let the faded recognition travel.
    try:
        _sup_set = store.cards.superseded_content_set(scope=scope) if getattr(store, "cards", None) else set()
    except Exception:
        _sup_set = set()
    if _sup_set:
        for sw in scored:
            if (sw.seed.content or "")[:120] in _sup_set:
                sw.score = round(sw.score * SUPERSEDED_DAMP, 3)

    # WARMTH TRAVELS THE EDGE (the keystone, owner 2026-06-06) — now MULTI-HOP. A concrete memory
    # that ran warm lights up the ABSTRACT seed it links to (uame_links), even when that abstract seed
    # scored ~0 lexically — AND, transitively, the value THAT seed links to, fainter each hop. This is
    # the knowledge bank (concrete actions, with lexical surface) recognizing a situation, then the
    # edge carrying recognition along the chain action --instances--> lesson --refines--> value. Without
    # it abstract values stay buried (the full-life sim's perpetual newborn). A bounded BFS: each hop
    # multiplies by EDGE_DECAY (so reach = base * EDGE_DECAY^hops), stops at MAX_HOPS or when the reached
    # warmth falls below LUKEWARM. A seed only RISES by edge if the chain makes it warmer than it already
    # is; seeds off the graph get no boost (no flooding — proven). See memory-is-a-weight-adjustor
    # (warmth-is-the-link), seed-and-link-while-warm (edges=the soul), knowledge-bank-cos-x-entry-rag.
    if scored:
        by_id = {sw.seed.id: sw for sw in scored}
        # BFS frontier: (seed_id, warmth_reaching_it, hops_so_far). Seed from every lexical/own hit.
        frontier = [(sw.seed.id, sw.score, 0) for sw in scored if sw.score >= LUKEWARM]
        # best warmth a node has been reached with, to avoid re-walking a node on a weaker path.
        best_reach = {sid: sc for sid, sc, _ in frontier}
        while frontier:
            sid, sc, hops = frontier.pop()
            if hops >= MAX_HOPS:
                continue
            try:
                edges = store.links_of(sid)   # v1 ∪ v2 (the drain: new edges live in v2 atom_links)
            except Exception:
                continue
            for e in edges:
                other_id = e["to_id"] if e["from_id"] == sid else e["from_id"]
                reached = round(sc * EDGE_DECAY, 3)
                if reached < LUKEWARM:
                    continue
                # only proceed if this path reaches `other_id` warmer than any prior path did.
                if other_id in best_reach and reached <= best_reach[other_id]:
                    continue
                best_reach[other_id] = reached
                if other_id in by_id:
                    if reached > by_id[other_id].score:   # the chain makes it warmer than it was
                        by_id[other_id].score = reached
                        by_id[other_id].via_rel = e["relation"]
                else:                                     # a linked seed lexical didn't surface at all
                    linked = store.seed_by_id(other_id) if hasattr(store, "seed_by_id") else None
                    if linked is not None:
                        nsw = SeedWarmth(seed=linked, score=reached, via_rel=e["relation"])
                        by_id[other_id] = nsw
                        scored.append(nsw)
                frontier.append((other_id, reached, hops + 1))   # keep walking the chain

    # ── DORMANT FILTER (2026-07-31) — exclude dormant atoms from default recall ──
    # Dormancy is a COMPUTED STATE, not a column: effective_score < threshold at
    # read-time. Filter BEFORE sort+top_k so dormant atoms don't consume top-N slots.
    # Only the content-prefix cross-store bridge is used (same as disclaimed_contents).
    _dormant_set: set = set()
    if not include_dormant:
        try:
            _dormant_set = store.cards.dormant_content_set(scope=scope) if getattr(store, "cards", None) else set()
        except Exception:
            _dormant_set = set()
    if _dormant_set:
        scored = [sw for sw in scored if (sw.seed.content or "")[:120] not in _dormant_set]

    scored.sort(key=lambda x: x.score, reverse=True)
    top = scored[:top_k]
    overall = top[0].score if top else 0.0
    tier = "lexical"
    judge_tokens = (0, 0)

    # --- SIDECAR (not escalation): lexical is the BRAIN, the judge is the HEART. When a judge
    # is present it is THE lens (recognition is a reasoning act — "weight are process-based",
    # memory-is-a-weight-adjustor); lexical is the free $0 FLOOR only when no judge is loaded
    # (compute-tiering). The fatal old bug was letting lexical GATE the judge: an abstract seed
    # (e.g. the WORLD-01 non-stationarity principle) scores ~0 lexically and never reached the
    # judge's shortlist, so paraphrase/abstraction recognition was lost. The fix: the judge's
    # shortlist is NOT filtered by lexical's blindness — it always sees the top lexical hits
    # UNION the most recent seeds (so principles & paraphrases lexical can't see still get read).
    # The brain only decides when the heart is absent.
    if judge_provider is not None and all_seeds:
        from .judge import judge_warmth
        # Build the shortlist the judge reads: lexical's best (where it DID see overlap) plus
        # recent seeds (so abstract/paraphrase seeds lexical scored ~0 are never hidden).
        shortlist: list[Seed] = [sw.seed for sw in top]
        seen_ids = {s.id for s in shortlist}
        for s in sorted(all_seeds, key=lambda s: s.ts, reverse=True):
            if s.id not in seen_ids:
                shortlist.append(s); seen_ids.add(s.id)
            if len(shortlist) >= max(top_k * 2, 6):
                break
        j = judge_warmth(reasoning, shortlist, judge_provider, judge_model)
        if j and isinstance(j.get("score"), (int, float)):
            tier = "judged"
            judge_tokens = tuple(j.get("_tokens", (0, 0)))
            overall = float(j["score"])   # the heart is the lens: its reading IS the warmth
            bi = int(j.get("best_index", -1))
            if 0 <= bi < len(shortlist):
                chosen = SeedWarmth(seed=shortlist[bi], score=round(overall, 3))
                top = [chosen] + [sw for sw in top if sw.seed.id != chosen.seed.id]
            elif bi < 0:
                top = []   # judge recognized nothing — cold, regardless of stray lexical overlap

    verdict = "warm" if overall >= WARM else "lukewarm" if overall >= LUKEWARM else "cold"

    # COS RESONANCE -> SOUL GROWTH (opt-in; default off so a pure read never inflates scores).
    # When a DELIBERATE recall lands WARM, that warm hit is proven resonance — rate the warmest
    # seed (COSys_DESIGN rating-from-use), which lets a value EARN its way up over repeated
    # resonance ([[core-values-grow]]). Only on warm, only its-own-scope (don't touch a neighbour's
    # seed), only when reinforce=True (the recall gate sets it). NOTE (audit #1, 2026-06-11): this
    # RAISES the score but no longer AUTO-PROMOTES working->core — reinforce()'s default is now
    # allow_promote=False (promotion is grand-vote-only, levels.may_write CORE=grandvote). The value
    # still rises: once its earned score+recalls cross the bar AND the dream witnesses it, the dream
    # nominates it through the FRONT door. Resonance earns the weight; the collective grants the rank.
    if reinforce and verdict == "warm" and top and not top[0].via_scope:
        # map the warmth score (0..1) to COS quality Q (0..100); a clearly-warm hit rates high.
        q = 50.0 + min(overall, 1.0) * 50.0
        store.reinforce(top[0].seed.id, scope, q)

    # EMOTIONAL reading: take the warmest seed's stored charge (felt at write-time) as the
    # emotional colour of this recognition, then derive the named feeling from the canvas.
    # No match -> neutral charge -> derive() yields curiosity (new) / unease only if unpleasant.
    from echelon_sdk.affect import derive   # affect is a pure leaf — migrated to the sdk layer
    if top:
        val, aro = top[0].seed.valence, top[0].seed.arousal
    else:
        val, aro = 0.0, 0.0
    aff = derive(overall, val, aro)

    return WarmthReading(score=round(overall, 3), verdict=verdict, warmest=top,
                         guidance=aff.tendency, emotion=aff.emotion,
                         valence=round(val, 2), arousal=round(aro, 2),
                         tier=tier, judge_tokens=judge_tokens)
