"""Provider contract — the seam the agent loop calls.

Lifted clean from EROS/ENGINE/providers/provider_interface.py, kept OpenAI-native
(no Gemini parts/Waggle/Vertex baggage). Every brain — Grok, the Copilot bridge,
a local LM Studio model — implements this one shape. The loop never knows which.

A ToolCall is normalized: {"id", "name", "args"(dict)}. Providers translate their
wire format INTO this so the loop is provider-agnostic.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _extract_reason(text: str, open_t: str, close_t: str) -> str:
    """Pull the reasoning out of <reason>…</reason>. The reason-stop call returns text ending at (or
    just before) the close tag. ROBUST to a weak floor that mangles the tags: tolerate a missing open
    tag (jumped straight in), a missing/mangled close tag (a weak model wrote `<...reasoning...>` as one
    blob, or dropped the slash), and a leaked action marker (CHOICE/<act>) — the reasoning is everything
    BEFORE the action, with stray angle-brackets stripped. The reasoning's MEANING is the bank key, so
    exact tag fidelity doesn't matter; clean capture does."""
    import re as _re
    t = (text or "").strip()
    # drop a leading open tag if present (literal or mangled "<reason" / bare "<")
    t = _re.sub(rf"^\s*{_re.escape(open_t)}\s*", "", t)
    t = _re.sub(r"^\s*<\s*reason[^>]*>?\s*", "", t, flags=_re.I)
    # cut at the FIRST boundary: the close tag (literal/mangled), or an action marker that leaked in
    cut = len(t)
    for marker in (close_t, "</reason", "<act", "CHOICE", "ACTION:"):
        k = t.find(marker)
        if k != -1:
            cut = min(cut, k)
    body = t[:cut]
    # strip stray angle brackets a weak model wrapped the whole thing in (`<...text...>`)
    body = body.strip().strip("<>").strip()
    return body


def _render_reflex(path: list) -> str:
    """Render a think() PATH into the injected <reflex> block — the earned moves, highest first.
    Each path hop is a dict from think(): {gist, coordinate, earned, ...}. Keep it terse (prefilled
    context). `gist` is the atom's content; fall back to coordinate."""
    lines = []
    for hop in path[:5]:
        if isinstance(hop, dict):
            c = hop.get("gist") or hop.get("content") or hop.get("coordinate") or ""
        else:
            c = str(hop)
        c = " ".join(str(c).split())
        if c:
            lines.append(f"- {c[:200]}")
    return "EARNED REFLEX (what worked before for this reasoning):\n" + "\n".join(lines) if lines else ""


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""
    thought_signature: str = ""   # Gemini-3.x: opaque sig on a function_call that MUST be echoed
                                   # back on the next turn's function_call part, else genai 400s
                                   # ("missing thought_signature"). Base64 str. Empty for non-gemini.


@dataclass
class LLMResponse:
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0   # of tokens_in, how many were a CACHE HIT (billed cheaper). DeepSeek
                             # returns prompt_cache_hit_tokens; a growing transcript hits ~its whole
                             # prefix. 0 = no cache info (provider doesn't report it / no hit).
    model_id: str = ""
    status: str = "success"  # success | error | timeout
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    reasoning_content: str = ""  # THINKING-MODE CoT, returned at the same level as `content`
                                 # (DeepSeek V4, grok-4.3). Kept OUT of `content` so callers never
                                 # see a </think> leak. CONTRACT: when a turn made TOOL CALLS, this
                                 # MUST be echoed back in the assistant message on every subsequent
                                 # request or DeepSeek returns HTTP 400. With no tool call it is
                                 # ignored by the API and need not be replayed.


class ProviderBase(ABC):
    """One brain. Reasons over a goal + tool schemas, returns either text or tool calls."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    # ── shared PRE-FLIGHT token counting (one impl, every provider inherits) ───────────────
    # The pure-stdlib DeepSeek-BPE estimator: lets the loop / Budget.would_exceed know a call's
    # size BEFORE sending it (the API's returned `usage` is the post-call billing truth). EXACT
    # for the DeepSeek driver, a strong estimate for other vendors' BPE. See echelon_agent.tokenizer.
    def count_tokens(self, text: str) -> int:
        """Estimate the tokens `text` will cost on THIS provider (pre-flight)."""
        from echelon_sdk.tokenizer import count_tokens
        return count_tokens(text)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the tokens an OpenAI-shape message list will cost (pre-flight)."""
        from echelon_sdk.tokenizer import count_messages
        return count_messages(messages)

    @abstractmethod
    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single turn. `messages` is OpenAI chat shape
        ([{role, content}] + assistant tool_calls / tool results).
        Returns an LLMResponse carrying text and/or normalized tool_calls."""
        ...

    # ── STREAMING capability (opt-in; default = not supported, prefill fallback used) ───────────
    # A provider that can sever its own decode mid-stream overrides supports_stream -> True and
    # implements send_stream_until (cut the generation when `stop` is hit, return what came before
    # it + a resume handle). LM Studio's OpenAI endpoint streams SSE, so LocalProvider opts in.
    supports_stream: bool = False

    def send_stream_until(
        self, messages: list[dict[str, Any]], model_id: str, stop: list[str], **kwargs: Any
    ) -> LLMResponse:
        """True mid-stream cut: stream the decode, stop the moment a `stop` marker appears, return the
        text emitted BEFORE it. Default raises — only providers with supports_stream=True implement it.
        The reflex-gate uses this when available; otherwise it falls back to a stop-sequence + prefill
        pair of plain send() calls (identical result, one extra short call)."""
        raise NotImplementedError("provider does not support true mid-stream; use the prefill path")

    # ── THE REFLEX GATE: reason -> reflex -> (think) -> act, enforced by the TRANSPORT ──────────
    # The structural fix for "agent reasons and acts in the same step". The model is prompted to
    # reason inside <reason>…</reason> FIRST. ECHELON severs generation the instant </reason> closes,
    # runs the captured REASONING (not the raw input) through the earned bank, and injects the bank's
    # response back before the model acts. So the move is conditioned on (its own reasoning) + (the
    # earned reflex for that reasoning). The hook is the REASONING -> it warmth-matches on MEANING, so
    # a reflex generalizes across surface states (the chess 0/10 null came from keying on the board).
    #
    #   reason  : send with stop=["</reason>"] (true mid-stream if available, else a plain stopped call)
    #   reflex  : store.think_about(reasoning, scope) -> {chain, confidence, ...}; confidence = the router
    #   think   : confidence < think_floor  => COLD/new ground (no earned move; the model generates fresh)
    #   act     : resume with <reason>…</reason>\n<reflex>…</reflex>\n prefilled -> model emits <act>
    #
    # Returns the final LLMResponse (the ACT), with .raw["reflex"] = {reasoning, confidence, chain,
    # routed: "reflex"|"think", injected}. WITNESS/CRYSTALLIZE is the caller's job (it alone knows if
    # the act worked) via crystallize_reflex() below — cold-then-witnessed is the think->reflex learning.
    def send_reflex_gated(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        store: Any = None,
        scope: str = "echelon",
        *,
        reason_tag: str = "reason",
        think_floor: float = 0.45,
        top_k: int = 7,
        reflex_fn: Any = None,
        percept: str = "",
        **kwargs: Any,
    ) -> LLMResponse:
        """`reflex_fn(text) -> (injected, confidence[, tier])` is the reflex lookup; pass it for a
        PURE-v2 cartridge (CardStore.v2_reflex_match). Default: store.think_about over the union pool.

        REFLEX IS PRE-DELIBERATIVE (owner: the ball-to-the-face — a warm reflex fires on RECOGNITION,
        before you think, even if thinking would call it unneeded). So if `percept` is given (a cheap
        situation descriptor the caller already has — e.g. the board geometry, NO model call), we
        RECOGNIZE first: match warm reflexes against the percept; a WARM hit FIRES immediately and the
        model acts on it WITHOUT the full reason pass (the speed; the flinch fires on the shape). Only
        when no warm reflex recognizes the percept do we fall to the slow THINK path (reason -> match
        cold candidates -> act -> witness -> crystallize). No percept => the reason-first path (estate
        default)."""
        open_t, close_t = f"<{reason_tag}>", f"</{reason_tag}>"

        # 0. RECOGNIZE (pre-deliberative reflex) — cheap percept match; a WARM reflex FIRES before think.
        if percept and reflex_fn is not None:
            try:
                out = reflex_fn(percept)
                inj, conf = out[0], out[1]
                tier = out[2] if len(out) > 2 else "warm"
            except Exception:
                inj, conf, tier = "", 0.0, "warm"
            if tier == "warm" and conf >= think_floor and inj:
                # FIRE: act on the recognized reflex, no reason pass (the flinch — fast, pre-deliberative).
                prefill = (f"{open_t}(reflex recognized this situation — acting on the proven move)"
                           f"{close_t}\n<reflex>REFLEX (earned — proven move, play it):\n{inj}</reflex>\n")
                r2 = self.send(list(messages) + [{"role": "assistant", "content": prefill}], model_id, **kwargs)
                r2.raw = {**(r2.raw or {}), "reflex": {"reasoning": percept, "confidence": conf,
                          "tier": "warm", "routed": "reflex", "injected": inj, "fired_on": "percept"},
                          "reason_prefill": prefill}
                r2.content = prefill + (r2.content or "")
                return r2
            # no warm reflex recognized the percept -> fall through to the slow THINK path below.

        # 1. REASON — stop the instant reasoning closes (transport enforces reason-before-act).
        if self.supports_stream:
            r1 = self.send_stream_until(messages, model_id, stop=[close_t], **kwargs)
        else:
            kw = dict(kwargs); kw["stop"] = list(kwargs.get("stop", []) or []) + [close_t]
            r1 = self.send(messages, model_id, **kw)
        if r1.status != "success":
            return r1
        reasoning = _extract_reason(r1.content, open_t, close_t)

        # 2. REFLEX — run the REASONING through the bank. confidence + TIER are the router.
        reflex = {"reasoning": reasoning, "confidence": 0.0, "chain": [], "routed": "think",
                  "injected": "", "tier": ""}
        if reasoning and (reflex_fn is not None or store is not None):
            conf, injected, path, tier = 0.0, "", [], "warm"
            try:
                if reflex_fn is not None:                 # PURE-v2 cartridge path
                    out = reflex_fn(reasoning)
                    # reflex_fn may return (injected, conf) or (injected, conf, tier). tier='cold' = a
                    # SEEDED-but-unproven candidate -> route to THINK (consider it), not REFLEX (do it).
                    injected, conf = out[0], out[1]
                    tier = out[2] if len(out) > 2 else "warm"
                else:                                     # estate path: think_about over the union pool
                    t = store.think_about(reasoning, scope=scope, top_k=top_k)
                    conf = float(t.get("confidence", 0.0) or 0.0)
                    path = t.get("path", []) or []
                    injected = _render_reflex(path)
            except Exception as e:  # a reflex failure must not kill the act — degrade to think
                conf, injected, path = 0.0, "", []
                reflex["_error"] = f"{type(e).__name__}: {e}"
            reflex.update(confidence=conf, chain=path, tier=tier)
            # 3. ROUTE on confidence + tier:
            #   warm + conf>=think_floor   -> REFLEX (earned, proven — play it)
            #   conf>=candidate_floor      -> THINK with a CANDIDATE to consider (a cold seed, or a
            #                                 meaningful-but-unproven match — owner: "fire on think too")
            #   below candidate_floor      -> THINK on true NEW GROUND (the match is a noise whisper)
            candidate_floor = think_floor * 0.5
            if tier == "warm" and conf >= think_floor and injected:
                reflex["routed"] = "reflex"
                reflex["injected"] = "REFLEX (earned — proven move, play it):\n" + injected
            elif injected and conf >= candidate_floor:
                reflex["routed"] = "think"      # cold seed OR weak warm match = a candidate to CONSIDER
                reflex["injected"] = ("THINK — an UNPROVEN candidate matched your reasoning (not yet "
                                      "earned here). CONSIDER it; if it works it becomes a reflex:\n" + injected)
            else:
                reflex["routed"] = "think"
                reflex["injected"] = ("(no candidate for this reasoning — NEW GROUND. Reason it out "
                                      "fresh; ECHELON will crystallize what works.)")

        # 4. ACT — resume with reasoning + the injected reflex prefilled; model emits the action.
        prefill = f"{open_t}{reasoning}{close_t}\n<reflex>{reflex['injected']}</reflex>\n"
        act_messages = list(messages) + [{"role": "assistant", "content": prefill}]
        r2 = self.send(act_messages, model_id, **kwargs)
        r2.raw = {**(r2.raw or {}), "reflex": reflex, "reason_prefill": prefill}
        # the full assistant turn is reason + reflex + act (so callers/crystallize see the whole move)
        r2.content = prefill + (r2.content or "")
        return r2

    def crystallize_reflex(
        self, cards: Any, reasoning: str, move: str, scope: str = "echelon", q: float = 90.0
    ) -> str | None:
        """WITNESS step (caller-driven): after an act is witnessed as good by TRACE, bank the
        reasoning->move as a reflex KEYED ON THE REASONING (its meaning), so the SAME tactic fires on
        unseen surface states. PURE v2: writes directly to the CardStore (`cards`) and EARNS the atom
        above neutral via a one-ref card, so v2_reflex_match will let it fire (no v1 cold-borrow, no
        SeedStore — a cartridge is a clean earned-v2 core). Returns the atom id, or None."""
        if not reasoning or not move or cards is None:
            return None
        try:
            import hashlib
            coord = f"{scope}:reflex_{hashlib.sha1(reasoning.strip().lower().encode()).hexdigest()[:12]}"
            content = (f"[reflex] WHEN (reasoning): {reasoning.strip()[:400]}\n"
                       f"THEN (move): {move.strip()[:200]}")
            aid = cards.add_atom(coord, content, scope=scope, kind="reflex", born_from="reflex-gate")
            # EARN it above neutral so it's a PROVEN reflex (v2_reflex_match gates on effective_score>B).
            cid = cards.add_card(f"reflex {coord}", [coord], born_from="reflex-gate")
            # explicit provenance: the caller-contract IS trace-witnessing (see docstring) —
            # an unlabeled q defaults to 'trace' silently, which hides drift if a caller slips.
            cards.reinforce_card(cid, q=q, source="trace")
            return aid
        except Exception:
            return None
