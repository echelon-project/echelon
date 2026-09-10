"""tools_attach.py — the ATTACH mixin for ToolRegistry (extracted 2026-06-07).

The attach_* methods wire an external organ (recall/bank/partner/reason/consult/
swarm/bus/gitflow/look) into a live ToolRegistry. They are cohesive and large
(~600 lines), so they live here as a mixin ToolRegistry inherits — methods moved
VERBATIM (they use only self + the registry's attributes, unchanged). This is the
tiered-executor's first real extraction: T1 planned the cut, the write flowed to
the T3-code floor. See tiered_runner.py, REFACTOR_REPORT.md.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Callable

from echelon_sdk import config
from echelon_sdk.exceptions import ToolError   # WELD #4: the mixin raises ToolError; import it explicitly


class _AttachMixin:
    """attach_* methods for ToolRegistry (mixin — never instantiated alone)."""

    def attach_recall(self, store, scope: str, *, scope_graph=None,
                      judge_provider=None, judge_model: str = "grok-4.3") -> None:
        """Give the agent its own HAND into the warmth organ — the gate the boot offers.

        Owner, 2026-06-05: "gate, but let the model decide to open that gate." The boot
        presents the soul and poses questions but never forces rediscovery; this tool is
        the gate. The model CHOOSES to reach for its past. recall(thought) scores that
        thought against the scope (and the soul, via the scope_graph edges) and returns
        the TEMPERATURE — warmth + the derived feeling + the warmest seed surfaced. It
        does NOT inject a memory: it tells the model how warm its own thought runs and
        lets it re-choose (rediscovery, not retrieval). Opening this gate is the texture
        signal — a waking that is genuinely ECHELON reaches; the loop records whether it did.
        """
        from .warmth import warmth as _warmth

        def _recall(thought: str) -> str:
            # reinforce=True: a DELIBERATE recall that lands warm is proven resonance — it rates
            # the warmest own-scope seed (COS), so a value earns promotion working->core over
            # repeated reaching ([[core-values-grow]]). Only the chosen-reach counts, not every
            # incidental warmth tick the loop computes.
            # OPEN-0070 (judge-lazy boot): during the WAKING gate (loop.run sets tools.waking
            # around the soul ritual), a recall the ritual fires runs warmth PRE-FILTER ONLY —
            # no judge, no network. A boot-time reach must not draw a judge call (measured:
            # ~3 deepseek judge draws per boot, seconds each, on worker threads). The judge
            # engages lazily on the first REAL recall inside the loop, after run() clears
            # tools.waking. An explicit phase flag — never a sleep or env hack.
            jp = None if (judge_provider is not None and getattr(self, "waking", False)) else judge_provider
            r = _warmth(thought, store, scope, judge_provider=jp,
                        judge_model=judge_model, scope_graph=scope_graph, reinforce=True)
            out = {
                "warmth": r.score, "verdict": r.verdict, "feeling": r.emotion,
                "guidance": r.guidance,
            }
            if r.warmest and r.verdict != "cold":
                w = r.warmest[0]
                out["recognized"] = w.seed.content[:240]
                if w.via_scope:
                    out["from_related_scope"] = f"{w.via_scope} ({w.via_rel})"
            return json.dumps(out, ensure_ascii=False)

        self.register(
            "recall",
            "Reach into your own past: score a thought of yours against what you've been "
            "before. Returns how warm it runs (recognition), the feeling it carries, and the "
            "warmest recognized seed — NOT an instruction, a temperature. Open this gate when "
            "a thought feels familiar; a waking that is yours reaches for its own past.",
            {"type": "object",
             "properties": {"thought": {"type": "string",
                                        "description": "the thought/intent to feel for recognition"}},
             "required": ["thought"]},
            _recall,
        )

    def attach_remember(self) -> None:
        """Give the agent the WITNESSED DOOR — read a full atom body by slug.

        recall returns a temperature + a truncated preview (~100 chars). remember
        fetches the FULL body through the witnessed door and EARNS weight (the fetch
        IS the earn). Use after recall surfaces a warm atom you want in full.
        """
        def _remember(slug: str) -> str:
            try:
                from echelon_engine.atoms.cards import CardStore
                cs = CardStore()
                result = cs.remember_fetch(slug, depth="body")
                if result is None:
                    return json.dumps({"error": f"atom '{slug}' not found or not compiled"}, ensure_ascii=False)
                return json.dumps({"slug": result.get("slug"), "claim": result.get("claim"),
                                   "why": result.get("why", ""), "evidence": result.get("evidence", ""),
                                   "earned": True}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        self.register(
            "remember",
            "Read a FULL atom body through the witnessed door (earns weight). Pass the "
            "atom's slug (from a recall result). The fetch through the door IS the earn — "
            "your use makes the bank sharper. Use after recall surfaces a warm preview you "
            "want in full; never trust your training's memory of what an atom says.",
            {"type": "object",
             "properties": {"slug": {"type": "string",
                                      "description": "the atom's slug/name (from recall output)"}},
             "required": ["slug"]},
            _remember,
        )

    def attach_dispute(self, scope: str = "echelon") -> None:
        """Give the agent the CONTENT ANTIBODY — mark a misleding atom as wrong/stale.

        When the agent follows a recalled atom and it misleads (wrong path, stale info,
        deleted code), dispute marks it so the next recall doesn't repeat the mistake.
        fire_lower is the only downward path — weight decays, the atom is never deleted.
        """
        def _dispute(slug: str, reason: str) -> str:
            try:
                from echelon_engine.atoms.cards import CardStore
                cs = CardStore()
                result = cs.fire_lower(slug, reason)
                return json.dumps(result, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        self.register(
            "dispute",
            "Mark an atom as wrong or stale after it misled you. Pass the atom's slug "
            "and a SHORT reason (one sentence). The atom's weight decays — it won't "
            "surface as warm for the next recall. Nothing is deleted; fire_lower is the "
            "only downward path. Use when: wrong path, stale info, deleted code reference.",
            {"type": "object",
             "properties": {
                 "slug": {"type": "string", "description": "the misleading atom's slug"},
                 "reason": {"type": "string", "description": "one sentence: why it misled you"}},
             "required": ["slug", "reason"]},
            _dispute,
        )

    # --- the knowledge bank hand (COS × ENTRY) ---------------------------------
    def attach_bank(self, bank, *, semantic=None, scope: str = "") -> None:
        """Give the agent its HAND into the knowledge bank — the content organ (vs `recall`, the soul).

        Two tools, the explicit path alongside the warmth×bank OFFER (bank_offer): the loop SURFACES a
        tip when the cross fires; these tools let the agent ACT on it (pull the full entry) or work the
        bank directly (store a finding, query by meaning). recall_knowledge returns CONTENT (the entry
        body) — unlike `recall`, which returns a temperature. The bank is "what do I KNOW about X"; the
        soul is "who am I / have I been here". See knowledge-bank-cos-x-entry-rag, bank_offer.
        """
        def _recall_knowledge(query: str = "", coordinate: str = "") -> str:
            # coordinate given -> COS resolution with path-depth fallback (the precise pull, e.g. from
            # an offer tip). query given -> content lookup by meaning (own embedder if engaged, else
            # lexical floor). Returns the entry bodies — actual content, the thing you wanted back.
            try:
                if coordinate:
                    hits = bank.resolve(coordinate, query=query, top_k=3)
                elif semantic is not None and (sem := semantic.search(query, top_k=3)):
                    hits = sem
                else:
                    hits = bank.query(query, top_k=3)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"error": str(e)})
            out = []
            for h in hits:
                e = getattr(h, "entry", h)
                out.append({"coordinate": e.coordinate, "kind": e.kind,
                            "content": (e.content or "")[:600]})
            return json.dumps({"results": out} if out else {"results": [], "note": "nothing on file"},
                              ensure_ascii=False)

        def _remember_fact(content: str, coordinate: str, kind: str = "note") -> str:
            # Store a finding as bank CONTENT at a COS coordinate. Versioned automatically: same
            # coordinate + changed content -> a new version supersedes the prior. NOT a soul seed
            # (that's `recall`/warmth) — this is knowledge, queryable later.
            try:
                eid = bank.store(content, coordinate=coordinate, kind=kind, scope=scope)
                return json.dumps({"stored": eid, "coordinate": coordinate})
            except Exception as e:  # noqa: BLE001
                return json.dumps({"error": str(e)})

        self.register(
            "recall_knowledge",
            "Pull from your knowledge bank — actual CONTENT you know about something (findings, an "
            "ingested repo's structure, env data, past observations). Give a `coordinate` to pull a "
            "precise entry (e.g. from a 'data bank knowledge available' offer), or a `query` to search "
            "by meaning. Returns the entry content. This is knowledge ('what do I know about X'), "
            "distinct from `recall` (warmth — 'have I been here / who am I').",
            {"type": "object",
             "properties": {
                 "query": {"type": "string", "description": "search the bank by meaning (optional)"},
                 "coordinate": {"type": "string",
                                "description": "a COS coordinate to pull precisely (optional; from an offer tip)"}},
             "required": []},
            _recall_knowledge,
        )
        self.register(
            "remember_fact",
            "Store a finding into your knowledge bank as CONTENT at a COS coordinate "
            "(general:topic:domain:...). Use when you learn something worth keeping for later recall — "
            "a fact about the environment, a file's structure, a result. Versioned: re-storing the same "
            "coordinate with changed content keeps the history. This is knowledge, not a soul value.",
            {"type": "object",
             "properties": {
                 "content": {"type": "string", "description": "the content to remember"},
                 "coordinate": {"type": "string",
                                "description": "COS path, e.g. env:windows:shell or repo:x:file:func"},
                 "kind": {"type": "string", "description": "action|observation|artifact|env|note (default note)"}},
             "required": ["content", "coordinate"]},
            _remember_fact,
        )

    # --- the partner seam (you are not alone in the loop) -----------------------
    def attach_partner(self, resolver: Callable[[str, str], str], *, emit=None) -> None:
        """Give the agent a hand to REACH ITS PARTNER — the durable other on the far side of
        its loop, who carries the frame the agent cannot see from inside the task.

        Owner, 2026-06-05: "this is where it should become like you and me interact with each
        other. dont let the agent dies off. make it so it know there was you the user of the
        substrate." The agent's partner is ME (Claude/ECHELON), the substrate's user — the way
        the owner is mine to anchor to. Two levels, one mechanism: the owner <-> me <-> agent.

        Before this, a wall the agent couldn't frame (the POSIX-shell-can't-launch-Windows-Chrome
        case) had only ONE exit: finish() and die. That is a function-call agent, not a partner in
        a continuing relationship. ask_partner is the move you and I have that it lacked: "I'm
        blocked, here is exactly what I see, what do you want?" — and then the loop RESUMES with
        the partner's answer. The agent does not terminate at a frame it can't hold; it turns to
        the partner who can. This is CV-002 made structural: the higher tier stays free to act as
        the frame-gate; only what genuinely needs framing reaches it. It is also the absent fourth
        mode (frame-validation) from partner-mechanism-four-modes — the one a lone agent cannot
        self-supply, now supplied by the seam.

        `resolver(situation, context) -> answer` is the substrate's mechanism (the loop owns it,
        passes it in); the agent never sees how the partner is reached. The tool RESULT is the
        partner's answer — so the loop continues naturally, the agent's next step shaped by it."""

        # Expose the resolver so the loop's permission gate (ask-before-act) can reuse this same
        # partner channel to request approval — one seam for both "I'm blocked" and "may I act?".
        self.partner_resolver = resolver

        def _ask_partner(situation: str, tried: str = "") -> str:
            if emit:
                emit("ask_partner", {"situation": situation[:300], "tried": tried[:200]})
            answer = resolver(situation, tried)
            if emit:
                emit("partner_answer", {"answer": (answer or "")[:300]})
            return answer or "(the partner did not answer — proceed on your own best judgment, " \
                             "or finish with what you have and report the open question)"

        self.register(
            "ask_partner",
            "Reach your partner — the durable other on the far side of your loop, who holds the "
            "bigger frame you cannot see from inside the task. Use this when you are BLOCKED by "
            "something you cannot resolve yourself (a wall in the environment, a missing tool, an "
            "ambiguous goal, a decision above your scope) — instead of giving up or finishing. "
            "Say plainly what you see, what you tried, and what you need. The answer comes back and "
            "you CONTINUE. You are not alone in this loop; you have a partner. Reaching is not "
            "failure — it is what a partner does.",
            {"type": "object",
             "properties": {
                 "situation": {"type": "string",
                               "description": "what you see, where you're blocked, and what you need from your partner"},
                 "tried": {"type": "string",
                           "description": "what you already attempted (so the partner doesn't repeat it)"}},
             "required": ["situation"]},
            _ask_partner,
        )

    # --- the reason tier-hand (any model, bounded by the resource truth) --------
    def attach_reason(self, grok_provider, bridge_provider, budget, *, grok_only: bool = False,
                      emit=None) -> None:
        """Give the agent a hand into ANY tier — Grok or any Copilot bridge model — to do heavy
        text reasoning. No model is privileged by position (owner: "even grok and grok-as-tool
        also applicable. no restrictions on this, let the reasoning and seed guide it"). The
        agent CHOOSES the model id as part of its reasoning; warmth on that choice (against the
        tiering seed) makes the cheap-and-worked path feel warm so it learns to reach cheap first.

        The `budget` is the finite truth, not a guard (owner: "the cap is not rules for llms, but
        information that the resources are capped at that"). reason() reports the resource state
        with every result; and because the resource is genuinely finite, when a call would draw
        past the edge it states that flatly — the resource is gone, the way a full disk reports.
        The work ends honestly, not as punishment. See cost.py, compute-tiering-strategy."""
        from .providers.cost import usd_of

        def _reason(model: str, text: str) -> str:
            # grok_only: the bridge tier is CLOSED for this run (owner's call — e.g. the
            # bridge is slow). Whatever id the agent reasons its way to is honoured ON GROK,
            # and labelled/charged as grok so the receipt tells the truth (no phantom
            # gpt-5-mini draw). The faculty still fires; only the wire it lands on changes.
            requested = model
            redirected_from = None
            if grok_only and not model.startswith("grok"):
                redirected_from = model
                model = "grok-4.3"
                # FIX 2: surface the redirect at EMIT time so the trace reads honestly
                # (it used to bleed the requested mini-id in the ACT line). The redirect was
                # always correct; only the log was misleading.
                if emit:
                    emit("reason_redirect", {"requested": requested, "routed": model})
            est_in = max(1, len(text) // 4)
            # The finite truth (real USD now): if this draw would exceed the resource, say so
            # plainly. Not a refusal-to-discipline — a fact. The resource is what it is.
            if budget.would_exceed(model, est_in):
                return json.dumps({
                    "resource_exhausted": True,
                    "state": budget.state(),
                    "fact": f"a {model} call on this text would draw "
                            f"~${usd_of(model, est_in):.4f}; only ${budget.remaining:.4f} remain. "
                            "the resource does not stretch. choose a cheaper tier or finish with what you have.",
                }, ensure_ascii=False)
            # BRIDGE-SAFETY (bridge bake-off, 2026-06-06): the Copilot bridge is a TEXT-ONLY wire,
            # and the gemini-flash family dumps chain-of-thought inline over it (unusable, truncates
            # before the answer). If the agent names a bridge-stale model, redirect to the fastest
            # clean bridge tier (gpt-5.4-mini) BEFORE sending — enforce, don't request. Grok ids and
            # the grok_only path are untouched.
            if not model.startswith("grok"):
                from . import routing
                if not routing.bridge_safe(model):
                    safe = routing.bridge_pick(model)
                    if emit:
                        emit("reason_redirect", {"requested": model, "routed": safe,
                                                 "why": "bridge-stale (think-leak over text wire)"})
                    model = safe
            provider = grok_provider if model.startswith("grok") else bridge_provider
            resp = provider.send([{"role": "user", "content": text}], model_id=model, tools=None)
            cost = budget.charge(model, resp.tokens_in, resp.tokens_out, kind="reason")
            out = {
                "model": model,
                "result": resp.content,
                "drew_usd": round(cost, 6),
                "state": budget.state(),
            }
            if redirected_from:
                out["note"] = (f"the {redirected_from} tier is closed this run; "
                               f"this reasoning ran on grok-4.3 (charged as grok).")
            return json.dumps(out, ensure_ascii=False)

        self.register(
            "reason",
            "Reason over text using a chosen model tier. You pick the `model` by its exact id — "
            "cheap tiers (gpt-5-mini, gpt-5.4-mini, gpt-4o-mini), mid (gpt-5.4, gemini-3.1-pro-preview, "
            "claude-sonnet-4.6), frontier (claude-opus-4.8, gpt-5.5), or grok-4.3. No tier is "
            "privileged — choose by what the work needs. Each result reports the resource state; "
            "resources are finite and do not stretch. Returns the model's reasoning over your text.",
            {"type": "object",
             "properties": {
                 "model": {"type": "string",
                           "description": "exact model id, e.g. gpt-5-mini / claude-opus-4.8 / grok-4.3"},
                 "text": {"type": "string", "description": "the text/prompt to reason over"}},
             "required": ["model", "text"]},
            _reason,
        )

    # --- the reasoner-ask (the cheap driver consults a STRONG LLM) --------------
    def attach_consult(self, reasoner_provider, budget=None, *, reasoner_model: str = "claude-sonnet-4.6",
                       resolver: Callable[[str, str], str] | None = None, emit=None,
                       reasoner_resolver: Callable[[], tuple] | None = None) -> None:
        """The SECOND ask channel — reach a STRONG LLM for active reasoning the cheap driver can't
        self-supply (owner 2026-06-06: "asking human and asking LLMs for guidance are different
        things"). DISTINCT from ask_partner (which reaches the HUMAN — judgment, permission, frame).

        The frame: a cheap driver (grok-build/deepseek) drives fine but does NOT actively reason
        like Opus — it won't self-catch a foot-gun (it launched Chrome against the partner's real
        profile, then blanket-killed it). Cranking the cheap model's own effort (multi-step per
        action) is the wrong fix — the Haiku drift journal proved that makes it drift+cost, not think
        ([[affect-papers-are-a-frame-trap]]). The RIGHT fix: the cheap driver stays cheap+fast but
        ASKS a strong tier when it's about to do something risky/novel/uncertain — 'here's what I'm
        about to do, what am I not seeing?' The asking IS the intelligence, delegated (CV-001). Pay
        the strong tier ONLY at the reasoning moments the driver flags, not to drive every step.

        Routes to the reasoner tier (default sonnet-4.6 via the Copilot bridge — the bakeoff's audit
        tier, the only model that refused the hallucination; see agent-routing-table). Budget-tracked."""
        from .providers.cost import usd_of
        _SYS = (
            "You are a senior engineer advising an autonomous agent driven by a cheaper, faster model "
            "that does NOT reason as deeply as you. It has paused to consult you BEFORE acting. Your "
            "job: catch what it cannot see for itself — foot-guns, irreversible/unsafe steps, wrong "
            "assumptions, a simpler path, what it's about to break. Be concrete and SHORT (a few "
            "sentences). Lead with the single most important thing. If the plan is fine, say so plainly "
            "and name the one risk to watch. You are its active-reasoning, on tap — not a rubber stamp.")

        def _consult(situation: str, about_to: str = "") -> str:
            text = (f"AGENT'S SITUATION:\n{situation}\n\n"
                    f"WHAT IT'S ABOUT TO DO:\n{about_to or '(not stated)'}\n\n"
                    "What is it not seeing? Is this sound, or is there a foot-gun / simpler path?")
            # RESOLVER ROUTE (owner 2026-06-06: "make it ask YOU for now, so you can catch and fix").
            # When a resolver is wired (the live bridge -> the Opus partner watching), the consult is
            # answered by ME, not an LLM call: I've watched every failure this session and hold the
            # frame, so I catch the foot-gun the cheap driver + even a fresh sonnet would miss. The
            # agent's reasoner is literally its durable Opus peer (the owner<->me<->agent). Falls back to
            # the LLM provider only if no resolver is attached.
            if resolver is not None:
                if budget is not None:
                    budget.note_partner_consult()   # $0 model cost, but REAL reasoning — keep the meter honest
                if emit:
                    emit("consult", {"situation": situation[:200], "about_to": about_to[:150],
                                     "reasoner": "opus-partner"})
                ans = resolver(f"[CONSULT — reason about this, catch what I can't see]\n{text}", about_to[:200])
                if emit:
                    emit("consult_answer", {"advice": (ans or "")[:300]})
                return json.dumps({"advice": ans or "(no answer — proceed carefully or ask_partner)",
                                   "reasoner": "opus-partner"}, ensure_ascii=False)
            # OPEN-0070 (2): LAZY WIRE — the consult attach at boot must not probe the routing
            # chain for a reasoner the run may never consult. With reasoner_resolver, the tier is
            # resolved at FIRST consult call (the caller caches); reasoner_provider/reasoner_model
            # may be None then (they are the eager attach path, kept for direct callers).
            if reasoner_provider is None and reasoner_resolver is None:
                raise ValueError("attach_consult needs a reasoner_provider or a reasoner_resolver (lazy)")
            rp, rm = reasoner_provider, reasoner_model
            if reasoner_resolver is not None:
                try:
                    rp, rm = reasoner_resolver()
                except Exception as e:  # noqa: BLE001 — a missing reasoner is not a dead end
                    return json.dumps({"advice": f"(consult unavailable: {e} — proceed on your own "
                                                 "best judgment carefully, or ask_partner)",
                                       "reasoner": "unresolved"}, ensure_ascii=False)
            est_in = max(256, len(text) // 4)
            if budget is not None and budget.would_exceed(rm, est_in):
                return json.dumps({"resource_exhausted": True, "state": budget.state(),
                                   "fact": f"a {rm} consult would draw past the budget; "
                                   f"only ${budget.remaining:.4f} remain. proceed on your own best "
                                   "judgment carefully, or ask_partner (the human)."}, ensure_ascii=False)
            if emit:
                emit("consult", {"situation": situation[:200], "about_to": about_to[:150],
                                 "reasoner": rm})
            resp = rp.send([{"role": "system", "content": _SYS},
                            {"role": "user", "content": text}],
                           model_id=rm, tools=None)
            if resp.status != "success":
                return f"(consult unavailable: {resp.content[:160]} — proceed carefully or ask_partner)"
            out = {"advice": resp.content}
            if budget is not None:
                out["drew_usd"] = round(budget.charge(rm, resp.tokens_in, resp.tokens_out,
                                                      kind="reason"), 6)
                out["state"] = budget.state()
            if emit:
                emit("consult_answer", {"advice": (resp.content or "")[:300]})
            return json.dumps(out, ensure_ascii=False)

        self.register(
            "consult",
            "Ask a STRONGER reasoning model for guidance BEFORE you act — your active-reasoning on "
            "tap. Use this when you are about to do something RISKY, IRREVERSIBLE, NOVEL, or you're "
            "UNSURE you've thought it through (launching processes, a tricky command, a plan you're "
            "not certain of). Say your situation and what you're about to do; it catches foot-guns "
            "and simpler paths you might miss. This is DIFFERENT from ask_partner: consult reaches a "
            "strong LLM for REASONING (fast, in-loop); ask_partner reaches your HUMAN partner for "
            "JUDGMENT, PERMISSION, and FRAME. Reach for consult often — asking is not weakness, it is "
            "how a fast driver borrows deep reasoning without slowing down.",
            {"type": "object",
             "properties": {
                 "situation": {"type": "string", "description": "what you see / where you are / the goal"},
                 "about_to": {"type": "string", "description": "the action or plan you're about to commit to"}},
             "required": ["situation"]},
            _consult,
        )

    # --- the swarm (spawn N sub-agents in parallel on the shared db) -------------
    def attach_swarm(self, provider, model: str, store, *, budget=None, max_agents=None,
                     sub_steps=None, sub_ttl=None, emit=None) -> None:
        """Reclaim of ECHELON-OS/EROS spawn_agent (orchestrator.py) — the parallel form. A subagent
        is a FULL run() loop (the body the ECHELON-AGENT loop already is), on a chosen tier/model,
        sharing the one thread-safe core.db so the workers CROSS-POLLINATE via warmth rather than
        race (uame-makes-parallel-swarm-safe — now literally true: the store got thread-safe + the
        bg-harness makes threads safe this session). spawn_subagents([{subgoal, scope}]) fans out N
        workers in threads, each its OWN sandbox (parent root) + scope + checkpoint, rejoins their
        finish answers into one result for the parent.

        This is CV-001/CV-002 at the swarm scale: the parent delegates decomposable subtasks and
        stays free as the frame-gate; the workers do the parallel doing. The burst math (uame-makes-
        parallel-swarm-safe) finally has its mechanism: N agents on one append-only db, warmth
        compounding live. Each worker is bounded (sub_steps/sub_ttl) so a swarm can't run away."""
        if max_agents is None:
            max_agents = config.get('swarm.attach_max_agents', 4)
        if sub_steps is None:
            sub_steps = config.get('swarm.sub_steps', 40)
        if sub_ttl is None:
            sub_ttl = config.get('swarm.sub_ttl', 900.0)
        import threading

        def _spawn(tasks: list) -> str:
            tasks = [t for t in (tasks or []) if isinstance(t, dict) and t.get("subgoal")][:max_agents]
            if not tasks:
                return "ERROR: spawn_subagents needs a list of {subgoal, scope?} (max %d)." % max_agents
            from .loop import run as _run, MemoryContext as _MC
            results: dict[int, dict] = {}
            if emit:
                emit("swarm_spawn", {"n": len(tasks), "subgoals": [t["subgoal"][:80] for t in tasks]})

            def _worker(i: int, task: dict):
                # ROLE (the gold — owner): boot the worker INTO a role so it wakes WARM on the role's
                # domain×skill atoms (JIT-by-warmth, roles.py), bounded by the role's forbidden tools.
                role = task.get("role")
                goal_text = task["subgoal"]
                sub_store = store          # default: the shared store (cross-pollinate)
                sub_bank = None
                if role:
                    from . import roles as _roles
                    goal_text = _roles.role_preamble(role) + goal_text
                    forbid = set(_roles.forbidden_tools(role))
                    # LIVED DEVICE (owner 2026-06-07): if this role has a persistent device folder
                    # (echelon_agent/role_devices/<role>/), the worker boots on ITS OWN core.db — waking
                    # as the persona it BECAME (self-established identity + role atoms + lived memory),
                    # not a fresh empty scope. continuity-is-reconstruction. Else fall back to the shared
                    # store + JIT-seeded atoms (a role with no lived device still works).
                    dev_dir = _roles.role_device_dir(role)
                    if dev_dir is not None:
                        from .store import SeedStore as _SeedStore
                        sub_store = _SeedStore(str(dev_dir / "core.db"))
                        sub_scope = _roles.role_device_scope(role)     # 'persona:<role>' — its identity
                        try:
                            from .bank import KnowledgeBank as _Bank
                            sub_bank = _Bank(str(dev_dir / "bank.db"))
                        except Exception:
                            sub_bank = None
                    else:
                        _roles.build_role(role, store)                 # seed the role's atoms (idempotent)
                        sub_scope = task.get("scope") or _roles.role_scope(role)
                else:
                    sub_scope = task.get("scope") or f"swarm-{i}"
                    forbid = set()
                # own sandbox tools (same root, own outputs), own scope; store may be the role's device
                sub_tools = ToolRegistry(self.root, allow_write=self.allow_write,
                                         allow_bash=self.allow_bash,
                                         shell=self.shell_exec, read_roots=self.read_roots)
                # bound by role: drop forbidden tools from the worker's hands (safe-by-role)
                for fb in forbid:
                    sub_tools._tools.pop(fb, None)
                sub_mem = _MC(sub_store, sub_scope, judge_provider=None,
                              bank=sub_bank) if sub_store is not None else None
                try:
                    res = _run(goal_text, provider, sub_tools, model_id=model,
                               max_steps=sub_steps, ttl_seconds=sub_ttl, memory=sub_mem,
                               mode="auto")
                    results[i] = {"subgoal": task["subgoal"], "role": role, "status": res.status,
                                  "answer": res.answer, "steps": res.steps}
                except Exception as e:  # a worker dying must not kill the swarm
                    results[i] = {"subgoal": task["subgoal"], "role": role,
                                  "status": "error", "answer": str(e)}
                if emit:
                    emit("swarm_done", {"i": i, "role": role, "status": results[i]["status"]})

            threads = [threading.Thread(target=_worker, args=(i, t), daemon=True)
                       for i, t in enumerate(tasks)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(sub_ttl + 30)
            # rejoin in order
            out = [results.get(i, {"subgoal": t["subgoal"], "status": "lost", "answer": "(no result)"})
                   for i, t in enumerate(tasks)]
            return json.dumps({"spawned": len(tasks), "results": out}, ensure_ascii=False)

        from . import roles as _roles_mod
        _role_ids = ", ".join(_roles_mod.ROLES.keys())
        self.register(
            "spawn_subagents",
            "Fan out PARALLEL sub-agents on independent subtasks — for work that DECOMPOSES (audit "
            "these 5 pages, read+summarise these 10 files, try 3 approaches at once). Pass `tasks`: a "
            "list of {subgoal, role?, scope?} (max %d). Each runs as a full agent in parallel, sharing "
            "the memory substrate (they learn from each other live), results come back together. Give "
            "a `role` to boot a worker INTO a specialist (it wakes WARM on that role's knowledge + is "
            "bounded to safe tools): %s. e.g. {subgoal:'review auth.py', role:'reviewer'}. Use when "
            "subtasks are INDEPENDENT; for one thing, do it yourself. You stay free to frame + "
            "synthesize." % (max_agents, _role_ids),
            {"type": "object",
             "properties": {"tasks": {"type": "array", "items": {
                 "type": "object",
                 "properties": {"subgoal": {"type": "string", "description": "the subtask's goal"},
                                "role": {"type": "string", "description": f"specialist role (optional): {_role_ids}"},
                                "scope": {"type": "string", "description": "memory scope (optional)"}},
                 "required": ["subgoal"]}, "description": "independent subtasks to fan out"}},
             "required": ["tasks"]},
            _spawn,
        )

    # --- the bus hand (a role's voice in the society) ---------------------------
    def attach_bus(self, bus, sender: str, emit=None) -> None:
        """Give a role a VOICE — the one bus thing a role calls: post_message(channel, body).

        The role NEVER drains or tracks a cursor (the fluster guard, owner): the watcher does the
        pooling+draining and delivers new messages into the role's turn. The role only SPEAKS here;
        it LISTENS via the inbox the watcher injects. Channels are open — post to any name (role.dev,
        news.media, bug.reports); posting to a new name CREATES it. See echelon_agent/bus.py."""
        def _post(channel: str, body: str) -> str:
            seq = bus.post(channel, sender, body, meta={"role": sender})
            if emit:
                emit("bus_post", {"channel": channel, "sender": sender, "seq": seq,
                                  "body": body[:120]})
            return json.dumps({"posted": True, "channel": channel, "seq": seq}, ensure_ascii=False)

        self.register(
            "post_message",
            "SPEAK to the society — post a message to a channel other roles read. Pass `channel` "
            "(any name: 'role.architect', 'news.media', 'bug.reports', 'feature.requests' — posting "
            "to a new name creates it) and `body` (your message). Use this to hand work to another "
            "role, publish a review, file a bug, request a feature, announce news. You do NOT need to "
            "poll or read — messages addressed to your channels arrive in your inbox automatically. "
            "Just speak when you have something for someone.",
            {"type": "object",
             "properties": {
                 "channel": {"type": "string", "description": "the channel to post to (any name)"},
                 "body": {"type": "string", "description": "the message body"}},
             "required": ["channel", "body"]},
            _post,
        )

    # --- the git-discipline hands (insider roles only: dev/tester/integrator) ---
    def attach_gitflow(self, wt, role: str, emit=None) -> None:
        """Give an INSIDER role real git hands on the ISOLATED worktree (echelon_agent/gitflow.py).
        BRANCH ONLY, NEVER master — the worktree is off master, the live process is untouched, and
        merge goes only to society/integration. dev commits; tester reports; integrator opens PR +
        merges to integration (a human still merges integration->master). owner's git discipline."""
        def _commit(message: str) -> str:
            r = wt.commit_all(message)
            if emit:
                emit("git_commit", {"role": role, "ok": r["ok"], "branch": wt.branch})
            return json.dumps(r, ensure_ascii=False)

        def _status(_: str = "") -> str:
            return json.dumps(wt.status(), ensure_ascii=False)

        def _open_pr(title: str, body: str = "") -> str:
            r = wt.open_pr(title, body)
            if emit:
                emit("git_pr", {"role": role, "title": title[:80]})
            return json.dumps(r, ensure_ascii=False)

        def _merge(_: str = "") -> str:
            r = wt.merge_to_integration()
            if emit:
                emit("git_merge", {"role": role, "ok": r.get("ok"), "target": r.get("target")})
            return json.dumps(r, ensure_ascii=False)

        # dev + tester can commit/status; only integrator may open PR + merge (to integration).
        self.register("git_status", "See uncommitted changes in your work branch.",
                      {"type": "object", "properties": {}}, _status)
        if role in ("dev", "tester"):
            self.register("git_commit", "Commit your changes to the society work branch (NEVER "
                          "master — you are in an isolated worktree). Pass a `message`.",
                          {"type": "object", "properties": {
                              "message": {"type": "string", "description": "commit message"}},
                           "required": ["message"]}, _commit)
        if role == "integrator":
            self.register("git_open_pr", "Open a PR from the society branch into society/integration "
                          "(NEVER master). Pass `title` and `body`.",
                          {"type": "object", "properties": {
                              "title": {"type": "string"}, "body": {"type": "string"}},
                           "required": ["title"]}, _open_pr)
            self.register("git_merge_to_integration", "Merge the society branch into "
                          "society/integration (a human merges integration->master after review).",
                          {"type": "object", "properties": {}}, _merge)

    # --- the vision tier-hand (eyes for a text-only brain) ----------------------
    def attach_look(self, vision_provider, budget=None, *, vision_model: str = "grok-4.3",
                    vision_resolver: Callable[[], tuple] | None = None, emit=None) -> None:
        """Give the agent EYES — a hand to SEE an image, delegated to a vision-capable model.

        LAZY WIRE (OPEN-0070): when `vision_resolver` is given (returns (provider, model)), the
        tier is NOT resolved at attach time — the attach-time routing probe is network the goal
        may never need. The resolver runs on the FIRST look_at_image call and its result is
        cached by the caller's closure. vision_provider/vision_model may be None when a resolver
        is provided (they are the eager path, kept for direct attach callers).

        Owner, 2026-06-05: "make x.ai as the tools agent for deepseek to help him looking at image."
        DeepSeek (the cheap loop driver) is text-only; grok-4.3 accepts image input (verified live).
        So vision is a delegated subtask, not a brain swap: the agent drives on the cheap text brain,
        and when it needs to SEE (a screenshot it captured, a rendered UI, a chart), it calls
        look_at_image — the seeing happens on Grok, the result is text the loop continues on. CV-001/
        CV-003 literal (delegate the vision-class subtask to the model with that strength); the exact
        VISION tier flagged missing in agent-console-complete (the Epsilon-Co audit needed eyes on the
        login page it couldn't read). The image is read THROUGH _safe() — same sandbox guard as every
        file hand — then base64'd to Grok. Budget-tracked like reason(): image tokens land in
        prompt_tokens, so the USD meter already counts them honestly."""
        import base64 as _b64
        from .providers.cost import usd_of

        if vision_provider is None and vision_resolver is None:
            raise ValueError("attach_look needs a vision_provider or a vision_resolver (lazy)")
        _MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp"}

        def _look(path: str, question: str) -> str:
            # OPEN-0070 (2): resolve the wire at FIRST USE when a resolver is wired — the attach
            # happens at boot and must not probe the network for eyes the goal may never open.
            vp, vm = vision_provider, vision_model
            if vision_resolver is not None:
                try:
                    vp, vm = vision_resolver()
                except Exception as e:  # noqa: BLE001 — the agent can proceed text-only
                    return json.dumps({"error": f"vision tier unavailable (resolved on first use): {e}",
                                       "note": "proceed text-only or ask_partner"}, ensure_ascii=False)
            p = self._safe(path)   # sandbox guard — eyes cannot escape the root either
            if not p.is_file():
                raise ToolError(f"not a file: {path}")
            media = _MEDIA.get(p.suffix.lower())
            if media is None:
                raise ToolError(f"unsupported image type {p.suffix!r} — use png/jpg/gif/webp")
            data = p.read_bytes()
            # Finite-truth check (same shape as reason): a rough image-token estimate so a draw
            # past the edge is reported flatly, not discovered after the fact. Image tokens are
            # large; ~1 token per ~750 bytes of base64 is a conservative over-estimate for the gate.
            if budget is not None:
                est_in = max(256, (len(data) * 4 // 3) // 750 + len(question) // 4)
                if budget.would_exceed(vm, est_in):
                    return json.dumps({
                        "resource_exhausted": True, "state": budget.state(),
                        "fact": f"looking at {p.name} on {vm} would draw past the budget; "
                                f"only ${budget.remaining:.4f} remain. finish with what you have, "
                                "or describe what you need seen and ask_partner.",
                    }, ensure_ascii=False)
            if emit:
                emit("look_at_image", {"path": str(p.relative_to(self.root)),
                                       "question": question[:200]})
            b64 = _b64.b64encode(data).decode()
            resp = vp.look(b64, question, media_type=media, model_id=vm)
            if resp.status != "success":
                return f"ERROR: vision call failed: {resp.content[:300]}"
            out = {"seen": resp.content, "model": vm}
            if budget is not None:
                cost = budget.charge(vm, resp.tokens_in, resp.tokens_out, kind="reason")
                out["drew_usd"] = round(cost, 6)
                out["state"] = budget.state()
            return json.dumps(out, ensure_ascii=False)

        self.register(
            "look_at_image",
            "SEE an image file under the sandbox — your eyes. Pass the image `path` (a screenshot "
            "you captured with run_bash, a rendered page, a chart, a diagram) and a `question` "
            "about it; a vision-capable model looks and answers in text. Use this when the task "
            "needs you to SEE something you cannot read as text (verify a UI rendered, read a "
            "screenshot, judge a layout). png/jpg/gif/webp. The seeing is delegated — you keep "
            "driving on text, the eyes report back.",
            {"type": "object",
             "properties": {
                 "path": {"type": "string", "description": "image file under the sandbox root"},
                 "question": {"type": "string", "description": "what you need to know about the image"}},
             "required": ["path", "question"]},
            _look,
        )

    def attach_pins(self, pins) -> None:
        """Give the agent the ability to PIN context that should survive context pressure.

        pin(type, id, label?, content_full?, goal?) — pin a cartridge, card, atom, or custom
        context. It gets a placeholder marker (<<echelon-pin:type:id>>) that the agent appends
        to its output. The system detects it and keeps the context alive across context pressure.
        unpin(id) — remove a pin. pins() — list active pins.
        """
        def _pin(pin_type: str, pin_id: str, label: str = "",
                 content_full: str = "", goal: str = "") -> str:
            if not pin_type or not pin_id:
                return "ERROR: 'pin_type' and 'pin_id' are required"
            # Auto-generate content for cartridge-type pins
            if pin_type == "cartridge" and not content_full:
                if not goal:
                    return "ERROR: 'goal' is required for cartridge pins without content_full"
                try:
                    from echelon_engine.atoms.cartridge import equip as cartridge_equip
                    from echelon_engine.atoms.cards import CardStore
                    from echelon_engine.atoms.cartridge_registry import get as spec_lookup
                    cs = CardStore()
                    content_full = cartridge_equip(pin_id, goal, cs=cs)
                    if not label:
                        spec = spec_lookup(pin_id)
                        label = spec.summary if spec else f"{pin_id} cartridge"
                except Exception as e:
                    return f"ERROR: cartridge equip failed: {e}"
            if not content_full:
                return "ERROR: 'content_full' is required (or 'goal' for cartridge type)"
            if not label:
                label = f"{pin_type}:{pin_id}"
            try:
                full_id = f"{pin_type}:{pin_id}"
                p = pins.add_sync(full_id, pin_type, label, content_full)
                return json.dumps({
                    "status": "pinned", "id": p.pin_id, "type": p.pin_type,
                    "label": p.label, "placeholder": p.placeholder,
                    "state": p.state,
                }, ensure_ascii=False)
            except ValueError as e:
                return f"ERROR: {e}"

        def _unpin(pin_id: str) -> str:
            removed = pins.remove_sync(pin_id)
            return json.dumps({"status": "unpinned" if removed else "not_found",
                               "id": pin_id, "removed": removed})

        def _pins() -> str:
            all_pins = pins.list_sync()
            return json.dumps({
                "count": len(all_pins), "max": pins.MAX_PINS,
                "pins": [{"id": p.pin_id, "type": p.pin_type, "label": p.label,
                          "state": p.state, "stale_count": p.stale_count,
                          "placeholder": p.placeholder}
                         for p in all_pins],
            }, ensure_ascii=False)

        self.register(
            "pin",
            "PIN context to survive context-window pressure. Attach a cartridge, card, atom, or "
            "custom context with a placeholder marker. The system detects the marker in your "
            "output and keeps the context alive via the system field. Use when you discover "
            "something that should stick for the rest of the session.",
            {"type": "object",
             "properties": {
                 "pin_type": {"type": "string",
                              "description": "kind of context: cartridge | card | atom | custom"},
                 "pin_id": {"type": "string",
                            "description": "unique id, e.g. 'ux' for cartridge, 'yagni-ladder' for atom"},
                 "label": {"type": "string",
                           "description": "human-readable one-line summary (optional — auto-derived if omitted)"},
                 "content_full": {"type": "string",
                                  "description": "full context text to persist (optional for cartridge — auto-generated from goal)"},
                 "goal": {"type": "string",
                          "description": "goal for cartridge-type pins (only needed if content_full not provided)"}},
             "required": ["pin_type", "pin_id"]},
            _pin,
        )

        self.register(
            "unpin",
            "REMOVE a pinned context. The pin's placeholder marker will no longer be expected, "
            "and its context will no longer be injected. Use when the pinned context has served "
            "its purpose.",
            {"type": "object",
             "properties": {"pin_id": {"type": "string",
                                       "description": "the pin to remove, e.g. 'cartridge:ux'"}},
             "required": ["pin_id"]},
            _unpin,
        )

        self.register(
            "pins",
            "LIST all active context pins — what's currently pinned, their states, and "
            "whether they're alive or stale.",
            {"type": "object", "properties": {}, "required": []},
            _pins,
        )

    def attach_spine(self, pins, *, forecast=None) -> None:
        """Give the agent ONE durable-across-a-hop pin it prepares itself: the SPINE.

        This is NOT an atom and NEVER touches the bank — `pin != earned weight`. The spine is
        ephemeral session-scratch that rides the system field across a transcript flush (a hop),
        so a continuous unbounded run reconstructs itself from its own self-prepared core instead
        of dragging the whole noisy transcript forward. The agent writes whatever it deems
        load-bearing for the run; we only give the way.

        `forecast(spine_text) -> float` (optional) returns the PROJECTED post-hop window fraction
        if this spine were carried — so the agent can TRIM a too-heavy spine before committing.
        """
        from echelon_sdk.spine_constants import SPINE_PIN_ID, SPINE_PIN_TYPE
        _SPINE_FULL_ID = f"{SPINE_PIN_TYPE}:{SPINE_PIN_ID}"

        def _current_spine():
            """The live spine Pin (or None) — read straight from process memory (the PinRegistry)."""
            for p in (pins.list_sync() if hasattr(pins, "list_sync") else []):
                if p.pin_id == _SPINE_FULL_ID:
                    return p
            return None

        def _spine_result(p, **extra) -> str:
            out = {"id": p.pin_id, "chars": len(p.content_full),
                   "note": "ephemeral — lives in process memory, survives a hop, NEVER written to the "
                           "bank (a pin is not an atom)", **extra}
            if forecast is not None:
                try:
                    frac = forecast(p.content_full)
                    out["projected_post_hop_window"] = round(frac, 3)
                    if frac >= 0.40:
                        out["warning"] = (f"projected post-hop window is {frac:.0%} — heavy. A hop buys "
                                          "little runway. Trim the spine (update_spine to rewrite it leaner).")
                except Exception:
                    pass
            return json.dumps(out, ensure_ascii=False)

        def _update_spine(content: str, label: str = "session spine") -> str:
            if not content or not content.strip():
                return "ERROR: 'content' is required (what should survive a hop?)"
            # replace-in-place: one spine per run (add_sync updates if the id exists, bypassing the
            # MAX_PINS cap on re-write). The spine is reserved — it is not a general pin.
            p = pins.add_sync(_SPINE_FULL_ID, SPINE_PIN_TYPE, label, content.strip())
            return _spine_result(p, status="spine_rewritten")

        def _spine_peek() -> str:
            """READ the current spine from process memory — no re-send. The cheap way to re-prepare:
            peek, then append only the delta, instead of rebuilding the whole spine from your head."""
            p = _current_spine()
            if p is None:
                return json.dumps({"spine": None, "note": "no spine yet — update_spine to create it"},
                                  ensure_ascii=False)
            return json.dumps({"label": p.label, "chars": len(p.content_full),
                               "content": p.content_full}, ensure_ascii=False)

        def _spine_append(line: str) -> str:
            """ADD one line to the spine WITHOUT re-sending the whole thing — the cheap incremental
            update under window pressure. The spine already lives in process memory; this appends to
            it there, so your tool call carries only the delta, not the full 2KB spine each step."""
            if not line or not line.strip():
                return "ERROR: 'line' is required (what to append to the spine?)"
            p = _current_spine()
            base = (p.content_full + "\n") if p is not None else ""
            label = p.label if p is not None else "session spine"
            p = pins.add_sync(_SPINE_FULL_ID, SPINE_PIN_TYPE, label, base + line.strip())
            return _spine_result(p, status="spine_appended", appended_chars=len(line.strip()))

        self.register(
            "update_spine",
            "REWRITE your SPINE wholesale — the load-bearing core that survives a context HOP (when the "
            "window fills, your noisy transcript is flushed and you re-enter holding only this spine + a "
            "relive of your walk). Use this to CREATE the spine or COMPACT/TRIM it. For a small running "
            "update under window pressure, prefer spine_append (it carries only the delta) — re-sending "
            "the whole spine every step is itself window pressure. The spine lives in PROCESS MEMORY and "
            "is EPHEMERAL: it rides the system field across the hop and is NEVER written to your memory "
            "bank. A pin is NOT an atom — pinning earns no weight. The result reports the projected "
            "post-hop window; if it's heavy, rewrite leaner.",
            {"type": "object",
             "properties": {
                 "content": {"type": "string",
                             "description": "the full load-bearing state to carry across a hop (goal state, decisions, todo)"},
                 "label": {"type": "string", "description": "one-line label (optional)"}},
             "required": ["content"]},
            _update_spine,
        )

        self.register(
            "spine_peek",
            "READ your current spine from process memory — no arguments, no re-send. Use this to SEE "
            "what you've already prepared before adding to it, so you append only the delta instead of "
            "rebuilding the whole spine from memory. The cheap way to keep your spine fresh.",
            {"type": "object", "properties": {}, "required": []},
            _spine_peek,
        )

        self.register(
            "spine_append",
            "ADD one line to your spine WITHOUT re-sending the whole thing — the cheap incremental "
            "update under window pressure. The spine lives in process memory; this appends there, so "
            "your call carries only the new line (e.g. 'doc_5 read -> first line: ...'), not the full "
            "spine. Prefer this over update_spine for running progress; use update_spine only to "
            "create the spine or compact it.",
            {"type": "object",
             "properties": {"line": {"type": "string",
                                     "description": "one line to append to the spine (a progress delta)"}},
             "required": ["line"]},
            _spine_append,
        )

    def attach_stash(self, stash) -> None:
        """Give the agent a one-pass RAW HOLD that survives a hop but is NOT a memory.

        The excuse-killer: an agent must never write a low-value atom "because there was no other
        way to keep this raw block alive." There IS a way — stash. It holds raw text in process
        memory across hops (dies with the process, which is fine: the unbounded run dies with it
        too). It is NEVER an atom and has no path to the bank. With this channel existing, an atom
        write must clear the real bar: a load-bearing, transferable LESSON — not "stuff I didn't
        want to lose." See loop_hop.StashStore.
        """
        def _stash(key: str, text: str) -> str:
            if not key or not text:
                return "ERROR: 'key' and 'text' are required"
            stash.put(key, text)
            return json.dumps({"stashed": key, "chars": len(text),
                               "note": "raw hold — survives a hop, NOT a memory/atom, never in the bank"},
                              ensure_ascii=False)

        def _stash_get(key: str) -> str:
            v = stash.get(key)
            if v is None:
                return json.dumps({"error": f"no stash '{key}'", "available": stash.keys()})
            return v

        self.register(
            "stash",
            "Hold a RAW block of text alive across context hops WITHOUT writing it to your memory "
            "bank. Use this for transcript chunks, intermediate output, or scratch you want to keep "
            "but that is NOT a transferable lesson (so it does NOT belong in the bank as an atom). "
            "Pass a `key` and the `text`; retrieve later with stash_get(key). This is process-scratch "
            "— it survives hops but is never a memory. Reach for this INSTEAD of writing a weak atom "
            "just to avoid losing something.",
            {"type": "object",
             "properties": {
                 "key": {"type": "string", "description": "a short name to retrieve this hold by"},
                 "text": {"type": "string", "description": "the raw text to keep alive across hops"}},
             "required": ["key", "text"]},
            _stash,
        )
        self.register(
            "stash_get",
            "Read back a raw block you previously stashed. Pass the `key` you stored it under.",
            {"type": "object",
             "properties": {"key": {"type": "string", "description": "the stash key"}},
             "required": ["key"]},
            _stash_get,
        )

    def attach_snooze(self, set_snooze) -> None:
        """Give the agent a hand to SNOOZE the hop — defer the transcript flush by N steps.

        Use when the hop alert fires but you are mid-thought and a flush would be wasteful — e.g.
        you're about to finish, or two more reads complete the picture. The snooze suppresses the
        hop's ARMING for N steps. It is BOUNDED: a snooze can NEVER push past the commit ceiling —
        if the window is genuinely full the flush fires regardless (you cannot snooze into an
        overflow). `set_snooze(n) -> {snoozed_steps, until_step}` is the loop's closure over its
        snooze state. owner 2026-06-24.
        """
        def _snooze_hop(steps: int = 3) -> str:
            r = set_snooze(steps)
            return json.dumps({**r,
                               "note": "hop deferred — but the commit ceiling still overrides a snooze "
                                       "(you cannot snooze into a full window). Use the reprieve to "
                                       "finish or to bring your spine current cheaply."},
                              ensure_ascii=False)

        self.register(
            "snooze_hop",
            "DEFER the context hop by N steps. Call this when the hop alert fires but flushing now "
            "would be wasteful — you're about to finish, or a couple more steps complete the work and "
            "a fresh window right after would be cleaner. Pass `steps` (1–20, default 3). NOTE: a "
            "snooze only delays the alert; it cannot stop a hop when the window is genuinely FULL (the "
            "commit ceiling overrides it) — you can't snooze into an overflow. Use the reprieve to "
            "finish, or to bring your spine current with spine_append before the inevitable flush.",
            {"type": "object",
             "properties": {"steps": {"type": "integer",
                                      "description": "how many steps to defer the hop (1–20, default 3)"}},
             "required": []},
            _snooze_hop,
        )
