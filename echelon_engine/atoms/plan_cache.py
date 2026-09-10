"""plan_cache.py — OWN the plan caching (don't rent the provider's prefix cache).

The insight ([[plan-is-a-cacheable-bake-own-the-caching]]): a generated PLAN for a goal is a reusable
BAKE — the intelligence-class work (decompose + tier-tag) done ONCE. Re-paying the model to regenerate
the same plan for the same/similar goal is exactly the waste the cartridge thesis deletes ("load the
bake without re-baking"). DeepSeek's automatic prefix cache does a shallow, rented, opaque, input-only
version; we own a real one: foveated (warmth recall on goal MEANING, so a near-synonym goal hits),
earned (a plan that led to a green run warms; a bad plan decays), and portable.

THE SHAPE (thin layer over CardStore + the estate's own $0 embedder):
  - A plan is stored as a CARD: label = "plan:<scope>:<short goal>", refs = the tier-tagged leaves
    (encoded), born_from carries the full goal text (the cache key for meaning match).
  - get(goal): warmth recall over stored plan goals via OwnEmbedder similarity (the SAME engine
    v2_reflex_match uses). HIT (>= floor) -> return the cached leaves, $0, NO model call. MISS -> None.
  - put(goal, leaves): crystallize a generated plan so the next identical/similar goal is free.
  - A HIT's card can be reinforced on a verified-green downstream run (earned-by-trace), so good plans
    warm and bad ones decay — the property the provider cache can never have.

This is the T1 cartridge's real win restated: not "the seed makes the model decompose" (a capable
model does that from a strict kindle prompt), but "decompose ONCE, then the bank serves the plan" —
sufficiency, $0 on repeat. See [[t1-cartridge-is-the-kindle-kick-start]].
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from echelon_engine.atoms.cards import CardStore


# A cached plan is NOT served as a finished answer to blindly reuse (the auditor's confound: a blind
# hit can serve the WRONG plan and count it as a saving). It is served as a CARD TO BE REVIEWED — the
# reflex/think router applied to plans ([[reflex-vs-think-two-systems]], v2_reflex_match's two tiers):
#   route='reflex'  — the matched plan is WARM (earned: it drove green runs before) -> EXECUTE/DELEGATE
#                     it to the next tier without re-generating. Proven, play it.
#   route='review'  — a plan MATCHED the goal's meaning but is COLD/unproven (or a weaker near-match)
#                     -> hand it to the reviewer to CONSIDER: sound? delegate it. not? think/regenerate.
#                     A served-but-unsound plan fails review and falls through — it can't be a silent
#                     wrong-plan "saving". This dissolves the brittle accept/reject floor.
#   route='miss'    — nothing matched the meaning -> generate fresh (then crystallize for next time).
@dataclass
class PlanVerdict:
    route: str                # 'reflex' | 'review' | 'miss'
    goal: str = ""            # the cached goal that matched (empty on miss)
    leaves: list = field(default_factory=list)   # the served plan's tier-tagged leaves (the candidate)
    match: float = 0.0        # goal-meaning similarity (1.0 = exact)
    eff: float = 0.0          # the matched plan-card's effective (earned) score — WHY it routed
    card_id: str = ""         # the plan-card id (so a verified run can reinforce it)


class PlanCache:
    """Own plan caching keyed on goal MEANING. $0 stdlib embedder, no provider, no download."""

    def __init__(self, scope: str = "echelon", db_path: str | None = None, floor: float = 0.82):
        self.scope = scope
        self.cards = CardStore(db_path) if db_path else CardStore()
        # similarity above which a goal HITS a cached plan. 0.82 is tuned CONSERVATIVE: it clears the
        # phrasing-variant band (same goal-class reworded scores ~0.89-0.97) but rejects a cross-class
        # collision (unrelated goals share filler words "a/test/and" and can score ~0.78, especially
        # when the cache corpus is THIN — a single stored plan makes the OwnEmbedder space degenerate).
        # A false HIT serves the wrong plan (worse than a miss), so the floor errs toward miss.
        self.floor = floor

    # ── the LOOKUP: serve the matched plan as a card to REVIEW, tiered by earned warmth ──────
    def get(self, goal: str, *, review_floor: float | None = None) -> PlanVerdict:
        """Recall the best meaning-match plan and ROUTE it (never a blind hit):
          - WARM match (>= floor AND earned > neutral) -> route='reflex' (execute/delegate, proven).
          - matched but COLD/near (>= review_floor) -> route='review' (a candidate to consider; the
            reviewer decides sound->delegate or not->think; a wrong plan fails review, no silent serve).
          - nothing matches -> route='miss' (generate fresh).
        review_floor defaults to floor*0.75 (a weaker match still worth REVIEWING, not auto-executing).
        $0, no model call. The reflex/think router applied to plans."""
        rf = review_floor if review_floor is not None else self.floor * 0.75
        plans = self._stored_plans()
        if not plans:
            return PlanVerdict(route="miss")
        from echelon_sdk.bank_embed_own import OwnEmbedder
        corpus = [_GoalAtom(p["goal"]) for p in plans]
        emb = OwnEmbedder().fit(corpus)
        qv = emb.embed(goal)
        if not qv:
            return PlanVerdict(route="miss")
        best, best_s = None, 0.0
        for p in plans:
            s = emb.similarity(qv, emb.embed(p["goal"]))
            if s > best_s:
                best_s, best = s, p
        if best is None or best_s < rf:
            return PlanVerdict(route="miss")
        eff = self._card_eff(best["card_id"])
        from echelon_engine.atoms.cards import SCORE_BENCHMARK
        warm = best_s >= self.floor and eff > SCORE_BENCHMARK
        return PlanVerdict(route="reflex" if warm else "review",
                           goal=best["goal"], leaves=best["leaves"], match=round(best_s, 3),
                           eff=round(eff, 1), card_id=best["card_id"])

    # ── the STORE: crystallize a generated plan so the next goal can reuse it ───────────────
    def put(self, goal: str, leaves: list) -> str:
        """Cache a freshly generated plan. The card is content-addressed on the PAYLOAD (goal +
        leaves hash), NOT a truncated label — the auditor caught that label=goal[:48]+leaf-COUNT refs
        let two goals sharing a 48-char prefix + leaf-count COLLIDE and silently drop a plan (born_from
        is excluded from the card id, cards.py:138). Here the label carries a content hash of (goal,
        leaves), so distinct plans get distinct ids. born_from still holds the verbatim goal+leaves
        (refs are normalized by add_card and can't hold JSON). Empty leaves are not cached."""
        if not leaves:
            return ""
        import hashlib
        payload = json.dumps({"goal": goal, "leaves": leaves}, sort_keys=True)
        h = hashlib.sha1(payload.encode()).hexdigest()[:12]
        label = f"plan:{self.scope}:{h}"               # distinct payload -> distinct id (no collision)
        refs = [f"leaf-{i}" for i in range(len(leaves))]
        born = f"plan-cache|goal={goal}|leaves={json.dumps(leaves, sort_keys=True)}"
        return self.cards.add_card(label, refs, born_from=born)

    def reinforce(self, card_id: str, ok: bool) -> None:
        """Earn-by-trace: a plan that led to a verified-green run warms; a red one earns the stall Q.
        So good plans rise and bad ones decay — the property the provider cache can never have."""
        self.cards.reinforce_card(card_id, q=85.0 if ok else 35.0, source="trace")

    # ── the REVIEWER: judge a served candidate plan SOUND for the goal (the route='review' gate) ──
    def review_plan(self, goal: str, leaves: list, *, judge=None,
                    model: str | None = None) -> dict:
        """A served plan is a CANDIDATE, not an answer. The reviewer judges whether the cached leaves
        are SOUND for THIS goal — sound -> delegate it (the cache win is real, no regenerate); unsound
        -> reject -> the caller regenerates. This is what makes the win measure CORRECTNESS, not a
        blind call-skip (the cold-Opus fix): a wrong served plan is CAUGHT here, not silently reused.

        `judge(goal, leaves) -> bool` is injectable ($0 tests / a custom policy). Default = ONE cheap
        model call (the cheapest tier — reviewing is cheaper than regenerating): does this short plan
        cover the goal's parts? Best-effort: a judge error is treated as UNSOUND (fail closed -> the
        caller regenerates), never as a silent sound. Returns {sound, reason, via}."""
        if not leaves:
            return {"sound": False, "reason": "empty plan", "via": "guard"}
        if judge is not None:
            try:
                return {"sound": bool(judge(goal, leaves)), "reason": "injected judge", "via": "judge"}
            except Exception as e:
                return {"sound": False, "reason": f"judge error: {e}", "via": "judge", "fail_closed": True}
        # default: one cheap model call. Reviewing (a yes/no over a short plan) is far cheaper than
        # regenerating the plan — so even on a 'review' route we usually still beat re-decomposing.
        try:
            from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
            prov = DeepSeekProvider()
            plan_txt = "\n".join(f"- {lf.get('task', lf) if isinstance(lf, dict) else lf}" for lf in leaves)
            msgs = [{"role": "system", "content": "You review whether a PLAN soundly covers a GOAL. "
                     "Answer with ONLY 'SOUND' if the plan's steps cover the goal's parts, or "
                     "'UNSOUND' if it misses a part or is for a different goal. One word."},
                    {"role": "user", "content": f"GOAL: {goal}\n\nPLAN:\n{plan_txt}\n\nVerdict:"}]
            resp = prov.send(msgs, model_id=model or "deepseek-chat")
            verdict = (resp.content or "").strip().upper()
            sound = verdict.startswith("SOUND")
            return {"sound": sound, "reason": verdict[:40], "via": "deepseek"}
        except Exception as e:
            return {"sound": False, "reason": f"review error: {e}", "via": "deepseek", "fail_closed": True}

    # ── internals ───────────────────────────────────────────────────────────────────────────
    def _card_eff(self, card_id: str) -> float:
        """The plan-card's earned score (reinforce_card moves it). > neutral = warm/proven. Best-effort."""
        try:
            c = self.cards.card(card_id)
            return float(c.score) if c is not None else 0.0
        except Exception:
            return 0.0

    def _stored_plans(self) -> list:
        """All plan-cards for this scope, decoded back to {goal, leaves, card_id}."""
        rows = self.cards.conn.execute(
            "SELECT id, label, refs, born_from FROM cards WHERE label LIKE ?",
            (f"plan:{self.scope}:%",)).fetchall()
        out = []
        for r in rows:
            bf = r["born_from"] or ""
            goal, leaves = "", []
            # born_from = "plan-cache|goal=<goal>|leaves=<json>" — split on the markers (goal may
            # contain no '|', and leaves JSON has none at top level).
            if "goal=" in bf:
                rest = bf.split("goal=", 1)[1]
                if "|leaves=" in rest:
                    goal, leaves_json = rest.split("|leaves=", 1)
                    try:
                        leaves = json.loads(leaves_json)
                    except json.JSONDecodeError:
                        leaves = []
                else:
                    goal = rest
            out.append({"goal": goal or r["label"], "leaves": leaves, "card_id": r["id"]})
        return out


class _GoalAtom:
    """A minimal atom-shaped object so OwnEmbedder.fit (which reads `.content`) can fit on goals."""
    __slots__ = ("content",)

    def __init__(self, content: str):
        self.content = content
