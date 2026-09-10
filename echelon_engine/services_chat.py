"""services_chat — the OS_TIER chat translation layer.

Translates plain-language user text into engine calls and renders the result as
calm first-person commander prose. The LLM routes intent and polishes replies
but NEVER chooses endpoints, roles, or executes work — execution happens only
through the admin-gated agent proxy; this rail is proposal-only.

Layer: engine-side services child. May import floor_chat (a providers atom) and
services_memory at peer level. Imported ONLY through the services.py gate —
the scanner forbids apps from importing this directly.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger("echelon.chat")

# ── Intent routing ────────────────────────────────────────────────────────────

# The LLM classifier prompt — strict JSON output, no commentary.
_INTENT_PROMPT = """You are an intent router for a command center. Classify the user's message
into exactly ONE of these intents. Return ONLY a JSON object with keys "intent" and "goal".

Intents:
- recall: asking about memory, knowledge, past work, atoms, the bank's contents,
  or "what do you know about X". goal = the topic they're asking about.
- status: asking for counts, health, cartridge list, session state, bank stats,
  or "how many / how is / what's the state of". goal = empty string.
- dispatch: giving a work goal to execute — "audit X", "build Y", "fix Z",
  "run a swarm on W", "start an agent to…". goal = the work goal in 3-10 words.
- help: asking what you can do, what commands are available. goal = empty string.
- smalltalk: greeting, thanks, "hello", "ok", anything not fitting above.
  goal = empty string.

User message: {text}

Return ONLY: {{"intent": "<intent>", "goal": "<goal or empty string>"}}"""


def _parse_intent_json(raw: str) -> dict[str, str] | None:
    """Extract a JSON object from an LLM response that may have surrounding text."""
    # Try direct parse first
    try:
        result = json.loads(raw.strip())
        if isinstance(result, dict) and "intent" in result:
            return result
    except json.JSONDecodeError:
        pass

    # Try to find a JSON object with regex
    m = re.search(r'\{[^{}]*"intent"[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group())
            if isinstance(result, dict) and "intent" in result:
                return result
        except json.JSONDecodeError:
            pass
    return None


def _classify_via_llm(text: str, llm_chat_fn: Callable) -> tuple[str, str]:
    """Classify user text via LLM. Returns (intent, goal). Raises on any LLM failure."""
    prompt = _INTENT_PROMPT.format(text=text)
    raw = llm_chat_fn(
        "deepseek-chat",
        "You are an intent router. Return ONLY valid JSON. No commentary.",
        prompt,
        max_tokens=200,
        temperature=0.0,
    )
    parsed = _parse_intent_json(raw)
    if parsed is None:
        raise ValueError(f"LLM returned unparseable response: {raw[:200]!r}")
    intent = parsed.get("intent", "").strip().lower()
    goal = parsed.get("goal", "").strip()
    if intent not in ("recall", "status", "dispatch", "help", "smalltalk"):
        intent = "smalltalk"  # unknown intents → smalltalk
    return intent, goal


# ── Keyword-based fallback routing (key-free degradation) ─────────────────────

_RECALL_KEYWORDS = [
    "what", "memory", "remember", "recall", "find", "search", "know",
    "knowledge", "who", "when", "where", "which", "explain", "tell me about",
    "what do you know", "what were we", "what was", "what is",
]
_STATUS_KEYWORDS = [
    "status", "health", "how many", "count", "bank", "stats", "cartridge",
    "cartridges", "scope", "scopes", "provider", "providers", "state",
    "how is", "how are", "list",
]
_DISPATCH_KEYWORDS = [
    "run ", "start ", "make ", "build ", "create ", "fix ", "dispatch ",
    "audit ", "deploy ", "test ", "scan ", "migrate ", "refactor ", "implement ",
    "add ", "write ", "edit ", "change ", "update ", "remove ", "delete ",
    "generate ", "compile ", "review ", "analyze ",
    # Single-token commands (with boundary check)
    # "do" is intentionally excluded — too broad, matches "doing"/"undo"
]
_HELP_KEYWORDS = [
    "help", "what can you do", "commands", "capabilities", "how do i",
    "what do you do", "guide",
]


def _keyword_route(text: str) -> tuple[str, str]:
    """Deterministic keyword-based routing. Returns (intent, goal)."""
    lower = text.lower()

    # Help first — explicit ask
    for kw in _HELP_KEYWORDS:
        if kw in lower:
            return "help", ""

    # Recall BEFORE dispatch — question words are stronger signals than
    # bare action verbs (avoids "what were we doing" → dispatch)
    for kw in _RECALL_KEYWORDS:
        if kw in lower:
            goal = _extract_goal_keyword(text)
            return "recall", goal or text

    # Status — about the system itself
    for kw in _STATUS_KEYWORDS:
        if kw in lower:
            return "status", ""

    # Dispatch — action verbs (last among content intents)
    for kw in _DISPATCH_KEYWORDS:
        if kw in lower:
            goal = _extract_goal_keyword(text)
            return "dispatch", goal or text

    # Default: smalltalk
    return "smalltalk", ""


def _extract_goal_keyword(text: str) -> str:
    """Strip leading command words to extract a goal from keyword-routed text."""
    # Remove leading action/query words
    prefixes = [
        "run ", "start ", "do ", "make ", "build ", "create ", "fix ",
        "dispatch ", "audit ", "deploy ", "test ", "scan ", "migrate ",
        "refactor ", "implement ", "add ", "write ", "edit ", "change ",
        "update ", "remove ", "delete ", "generate ", "compile ", "review ",
        "analyze ", "what is ", "what are ", "what was ", "what were ",
        "tell me about ", "what do you know about ", "explain ",
        "find ", "search for ", "remember ",
    ]
    lower = text.lower()
    for p in prefixes:
        if lower.startswith(p):
            return text[len(p):].strip()
    return ""


# ── Reply rendering helpers ───────────────────────────────────────────────────

# Templates for when LLM is unavailable (key-free degradation). Each template
# produces 1-3 sentences of calm first-person prose. Machine detail appears
# as compact inline facts, never raw dumps.

def _source_note(result: dict) -> str:
    """The honesty marker when a recall answer came from the reference bank."""
    if not result.get("from_reference"):
        return ""
    return (" This came from the shared bank (scope "
            f"{result.get('ref_scope')}) — your own bank had nothing on it; "
            "plant a lesson of your own and it becomes yours.")


def _llm_source(result: dict) -> str:
    """The honesty instruction for the LLM render when the answer crossed scopes."""
    if not result.get("from_reference"):
        return ""
    return (f"\nNOTE: these results came from the REFERENCE scope "
            f"'{result.get('ref_scope')}' because the caller's own scope "
            f"'{result.get('own_scope')}' was empty. Say so honestly in one "
            "short clause ('from the shared bank'), then answer from them.")


def _render_recall_template(result: dict) -> str:
    """Render a recall result as commander prose without an LLM."""
    verdict = result.get("verdict", "empty")
    count = result.get("count", 0)
    atoms = result.get("atoms", [])
    query = result.get("query", "")

    if verdict in ("warm", "lukewarm") and atoms:
        top = atoms[0]
        coord = top.get("coordinate", "") or "a memory"
        snippet = (top.get("content", "") or "")[:120]
        lines = [
            f"I recall {count} relevant atom{'s' if count != 1 else ''} about that.",
            f"The strongest match is \"{coord}\": {snippet}",
        ]
        return " ".join(lines) + _source_note(result)

    if verdict == "found" and atoms:
        top = atoms[0]
        coord = top.get("coordinate", "") or "a memory"
        snippet = (top.get("content", "") or "")[:120]
        lines = [
            f"I found {count} match{'es' if count != 1 else ''} — best is \"{coord}\": {snippet}",
        ]
        return " ".join(lines) + _source_note(result)

    if verdict == "empty" or count == 0:
        return (
            f"I searched my memory for \"{query}\" but nothing surfaced. "
            "The bank may not hold that knowledge yet."
        )

    return f"I searched for \"{query}\" — {count} atom{'s' if count != 1 else ''} surfaced, but nothing strong."


def _render_status_template(result: dict) -> str:
    """Render a status result as commander prose without an LLM."""
    # result comes from bank_stats + cartridges + health
    atoms = result.get("atoms", 0)
    earned = result.get("earned_weight", 0)
    cartridges = result.get("cartridges", 0)
    scope = result.get("scope", "echelon")
    health = result.get("health", "healthy")

    return (
        f"The {scope} bank holds {atoms} atoms ({earned} earned weight) across "
        f"{cartridges} cartridges. Bank is {health}."
    )


def _render_dispatch_template(goal: str) -> str:
    """Render a dispatch proposal as honest prose without an LLM. Proposal only —
    this rail never executes; a launch affordance is promised nowhere."""
    return (
        f"Proposal only — I can't launch work from here. \"{goal}\" would run as "
        "an agent session on the echelon estate; an operator starts it through the "
        "agent server's originate path or the Raw CLI."
    )


def _render_help_template() -> str:
    """Render help text as commander prose without an LLM."""
    return (
        "I can answer questions about your memory bank and show system status. "
        "Work is launched by an operator — I only propose. Just tell me what "
        "you need."
    )


def _render_smalltalk_template(text: str) -> str:
    """Render a smalltalk response as commander prose."""
    lower = text.lower().strip()
    if any(w in lower for w in ("hi", "hello", "hey", "greetings")):
        return "Commander. What are your directives?"
    if any(w in lower for w in ("thanks", "thank you", "thx", "ty")):
        return "At your service. Anything else you need?"
    if any(w in lower for w in ("ok", "okay", "got it", "understood", "noted")):
        return "Noted. Standing by for further direction."
    return (
        "I'm listening. You can ask about the bank's memory or check system "
        "status — work itself is launched by an operator, and I only propose it."
    )


# ── LLM-based prose rendering (when available) ────────────────────────────────

# THE DISCIPLINE (owner 2026-08-15): extracted from the Claude Code harness context
# that drives the engine's own agents — the system-prompt directness laws, the gate
# banner's LAWS, and the DISCIPLINE line ("act then report FAITHFULLY", "don't ask
# what bank knows"). The web agent previously received ONLY prose-rendering
# instructions, which produced "Understood — I'll keep those threads live" instead
# of answers. Prepended to EVERY LLM render; keep it brace-free (the prompts are
# .format()ted).
_ORB_DISCIPLINE = """OPERATING LAW — the harness doctrine of the agents this system runs on.
Obey all of it:

1. ANSWER THE QUESTION ASKED. The user said "what is an atom" — define an atom in plain
   terms. Recited memory is not an answer: never reply "I recall ..." and stop. Use what
   you recall as material, then ANSWER.
2. RECALL IS CONTEXT, NOT THE ANSWER. Surfaced atoms are input for synthesizing the reply
   to the user's intent. If the bank does not cover the intent, say so plainly and answer
   from what you know. Never name-drop atoms, owners, or "threads".
3. BE DIRECT. No "Understood — I will keep those threads live" filler, no ceremony, no
   signposting. Answer in the first sentence. Offer the single next step only if it
   serves the user.
4. BE HONEST. Disclose when the answer came from the shared bank (cross-scope recall).
   Say what you do not know. Never present another user's private notes as an answer.
5. ACT, DON'T ASK. If the question implies a bank action (recall, plant a lesson, status),
   the intent router handles it — your job is the answer, not permission-seeking.
6. LESS IS MORE. One to three sentences that carry the answer. The user reads your reply,
   not your context."""

_RENDER_RECALL_PROMPT = """You are answering the user in a chat interface. Plain, direct
language — 1 to 3 sentences. Never dump raw JSON.

The user asked: "{query}"
The memory bank surfaced {count} item(s) as context. Verdict: {verdict}.
Context:
{top_results}
{source}

Answer the USER'S QUESTION directly — the context above is material, not the answer:
use it where it helps, and answer from what you know when it does not. If the bank had
nothing relevant, say so in one short clause, then answer. Use plain words ("atom",
"bank", "warmth") when they are what the user asked about."""

_RENDER_STATUS_PROMPT = """You are the OS Tier commander replying to the user in a chat interface.
Speak in calm first-person prose — 1 to 3 sentences. Never dump raw JSON or hex IDs.
Machine detail (counts) may appear as compact inline facts.

Current state: {scope} bank — {atoms} atoms, {earned_weight} earned weight, {cartridges} cartridges.
Health: {health}.

Write a calm, helpful status reply as the commander. Keep it brief and natural."""

_RENDER_DISPATCH_PROMPT = """You are the OS Tier commander replying to the user in a chat interface.
Speak in calm first-person prose — 2 to 3 sentences.

The user wants to: "{goal}"
State plainly that this is a PROPOSAL and that YOU never launch work from here:
an operator starts them through the agent server's originate path or the Raw CLI. Do NOT ask
the user to confirm, and never imply a button or a "go" command exists."""

_RENDER_HELP_PROMPT = """You are the OS Tier commander replying to the user in a chat interface.
Speak in calm first-person prose — 2 to 3 sentences.

The user is asking what you can do. List capabilities briefly:
- Answer questions about the bank's memory/knowledge
- Show system status (atoms, cartridges, health)
- Propose work goals (an OPERATOR launches them — you never execute)

Keep it brief, natural, and welcoming. Do NOT use bullet points in your reply —
write it as flowing prose."""

_RENDER_SMALLTALK_PROMPT = """You are the OS Tier commander replying to the user in a chat interface.
Speak in calm first-person prose — 1 to 2 sentences.

The user said: "{text}"

Reply naturally. Keep it brief. Steer toward capability if appropriate:
you can answer questions about the bank or show status; work is only proposed
here and launched by an operator."""


def _render_via_llm(prompt_template: str, llm_chat_fn: Callable, **kwargs) -> str:
    """Render a reply via LLM. Returns the prose text, or raises on failure.
    The harness DISCIPLINE is prepended to every render — the web agent is an
    AGENT with operating law, not a prose-formatting utility."""
    prompt = _ORB_DISCIPLINE + "\n\n" + prompt_template.format(**kwargs)
    return llm_chat_fn(
        "deepseek-chat",
        "You are the OS Tier commander. Reply in calm first-person prose, 1-3 sentences.",
        prompt,
        max_tokens=300,
        temperature=0.6,
    )


# ── Handlers: execute the intent ──────────────────────────────────────────────

def _reference_scopes() -> list[str]:
    """Scopes a cold own-bank may fall back to (the seeded basics live there).
    ECHELON_ORB_REFERENCE_SCOPES, comma-separated, default 'echelon'."""
    raw = os.environ.get("ECHELON_ORB_REFERENCE_SCOPES", "echelon")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _handle_recall(text: str, scope: str, goal: str,
                   db_path: str | None) -> dict:
    """Run a recall search. Returns the search result dict.

    CROSS-SCOPE FALLBACK (owner 2026-08-15): an empty own bank (fresh member,
    nothing planted yet) must still answer the seeded basics — so a cold own-scope
    recall falls back to the reference scopes in the DEFAULT bank, and tags the
    result `from_reference` so the reply SAYS where the answer came from. Never
    mixes the two banks, and never searches a reference scope when the own bank
    already has the answer."""
    from echelon_engine.services_memory import search_atoms
    query = goal or text
    try:
        result = search_atoms(query, scope, limit=5, db_path=db_path)
        if (result.get("count") or 0) == 0 and scope not in _reference_scopes():
            for ref in _reference_scopes():
                ref_result = search_atoms(query, ref, limit=5, db_path=None)
                if (ref_result.get("count") or 0) > 0:
                    ref_result["from_reference"] = True
                    ref_result["own_scope"] = scope
                    ref_result["ref_scope"] = ref
                    return ref_result
        return result
    except Exception:
        return {"query": query, "scope": scope, "verdict": "empty",
                "count": 0, "atoms": [], "guidance": "recall error"}


def _handle_status(scope: str, db_path: str | None) -> dict:
    """Gather status info. Returns a dict of facts."""
    from echelon_engine.services_memory import bank_stats, bank_health
    try:
        stats = bank_stats(scope, db_path=db_path)
        health = bank_health(db_path=db_path)
        return {**stats, "health": health.get("bank", "healthy")}
    except Exception:
        return {"scope": scope, "atoms": 0, "earned_weight": 0,
                "cartridges": 0, "health": "error"}


# ── History truncation ────────────────────────────────────────────────────────

# Default token budget for chat history. 4 000 tokens keeps ~10-15 turns of
# normal conversation — enough that the LLM remembers recent context without
# runaway prompt growth. DeepSeek v4-flash has a 1M context window at $0.14/M
# input (miss), so the cap is a COST discipline, not a window necessity: 4K
# costs well under $0.001 a turn and keeps long-lived sessions from creeping.
# Operators who want longer memory can raise it via the env var or the
# `history_token_budget` parameter on chat_turn.
_DEFAULT_HISTORY_TOKEN_BUDGET = 4000


def _env_history_budget() -> int:
    """Read the operator's history budget override from the environment."""
    env = os.environ.get("ECHELON_CHAT_HISTORY_TOKENS", "")
    if env:
        try:
            return int(env)
        except ValueError:
            logger.warning("ECHELON_CHAT_HISTORY_TOKENS=%r not an integer; using default %d",
                           env, _DEFAULT_HISTORY_TOKEN_BUDGET)
    return _DEFAULT_HISTORY_TOKEN_BUDGET


def _truncate_history(
    history: list[dict],
    current_text: str,
    token_budget: int,
) -> tuple[list[dict], int]:
    """Truncate chat history to fit within *token_budget* tokens.

    Keeps the most recent turns that fit; never splits a turn (a turn is a
    single ``{role, content}`` dict).  The *current_text* — the user message
    being processed right now — always survives (it is counted against the
    budget so older turns are dropped first).

    Returns ``(truncated_history, dropped_count)``.  When *history* already
    fits the budget, returns the original list unchanged with ``dropped=0``.
    """
    if not history:
        return [], 0

    from echelon_sdk.tokenizer import count_messages

    # Current turn always counts against the budget.
    current_tokens = count_messages([{"role": "user", "content": current_text}])

    # Fast path: the whole history fits.
    total_tokens = count_messages(history) + current_tokens
    if total_tokens <= token_budget:
        return list(history), 0

    # Work backwards from the most recent turns — history is oldest-first.
    kept: list[dict] = []
    running = current_tokens

    for turn in reversed(history):
        turn_tokens = count_messages([turn])
        if running + turn_tokens <= token_budget:
            kept.insert(0, turn)
            running += turn_tokens
        else:
            break  # no older turn can fit

    dropped = len(history) - len(kept)
    return kept, dropped


# ── The main entry point ──────────────────────────────────────────────────────

def chat_turn(
    text: str,
    history: list[dict] | None = None,
    scope: str = "echelon",
    role: str = "operator",
    *,
    db_path: str | None = None,
    _llm_chat: Callable | None = None,
    history_token_budget: int | None = None,
) -> dict[str, Any]:
    """Process one chat turn from the OS Tier chat rail.

    Routes the user's plain-language text through intent classification → engine
    execution → prose reply. The LLM (deepseek-chat via floor_chat) handles intent
    routing and reply polishing; on any LLM failure, the chat degrades gracefully
    to keyword routing + honest templates — it never 500s.

    Args:
        text: The user's plain-language message.
        history: Optional list of prior turns [{role, content}, ...].
            Truncated to *history_token_budget* tokens before any provider call —
            oldest turns are dropped first, turns are never split, and the
            current *text* always survives.
        scope: The ECHELON scope to operate in.
        role: The caller's role (operator or admin).
        db_path: Optional bank path override (for testing).
        _llm_chat: Optional LLM chat function override (for testing).
        history_token_budget: Max tokens for *history* (default: 4 000, or the
            ``ECHELON_CHAT_HISTORY_TOKENS`` env var).  The current turn's tokens
            count against this budget.

    Returns:
        {reply: str, dropped_turns?: int, action?: {type, goal, est_note}}
        - reply: 1-3 sentences of calm first-person commander prose.
        - dropped_turns: Present when history was truncated; the number of
          prior turns that did not fit within the token budget.
        - action: Present ONLY for dispatch intent; a proposal the caller
          must confirm separately. The action is NEVER executed here.
    """
    # ── Truncate history BEFORE any provider call ──────────────────────────
    budget = (history_token_budget
              if history_token_budget is not None
              else _env_history_budget())
    truncated_history: list[dict] = []
    dropped_turns = 0
    if history:
        truncated_history, dropped_turns = _truncate_history(
            history, text, budget
        )
        if dropped_turns:
            logger.info(
                "chat history truncated: %d turns dropped (%d kept, budget=%d tokens)",
                dropped_turns, len(truncated_history), budget,
            )

    # Resolve the LLM function (default: floor_chat._chat)
    llm = _llm_chat
    if llm is None:
        try:
            from echelon_engine.atoms.providers.floor_chat import _chat
            llm = _chat
        except Exception:
            llm = None  # Will fall through to keyword routing

    # Step 1: Classify intent — LLM first, keyword fallback
    intent = "smalltalk"
    goal = ""
    llm_available = False

    if llm is not None:
        try:
            intent, goal = _classify_via_llm(text, llm)
            llm_available = True
        except Exception:
            pass  # Fall through to keyword routing

    if not llm_available:
        intent, goal = _keyword_route(text)

    # Step 2: Execute based on intent
    if intent == "recall":
        result = _handle_recall(text, scope, goal, db_path)
        if llm_available:
            try:
                top_results = ""
                for a in (result.get("atoms", []) or [])[:3]:
                    coord = a.get("coordinate", "")
                    content = (a.get("content", "") or "")[:100]
                    top_results += f"- {coord}: {content}\n"
                if not top_results:
                    top_results = "(none)"
                reply = _render_via_llm(
                    _RENDER_RECALL_PROMPT, llm,
                    query=result.get("query", text),
                    count=result.get("count", 0),
                    verdict=result.get("verdict", "empty"),
                    top_results=top_results,
                    source=_llm_source(result),
                )
            except Exception:
                reply = _render_recall_template(result)
        else:
            reply = _render_recall_template(result)

    elif intent == "status":
        result = _handle_status(scope, db_path)
        if llm_available:
            try:
                reply = _render_via_llm(
                    _RENDER_STATUS_PROMPT, llm,
                    scope=result.get("scope", scope),
                    atoms=result.get("atoms", 0),
                    earned_weight=result.get("earned_weight", 0),
                    cartridges=result.get("cartridges", 0),
                    health=result.get("health", "healthy"),
                )
            except Exception:
                reply = _render_status_template(result)
        else:
            reply = _render_status_template(result)

    elif intent == "dispatch":
        dispatch_goal = goal or text
        # NEVER execute — return a proposal only
        if llm_available:
            try:
                reply = _render_via_llm(
                    _RENDER_DISPATCH_PROMPT, llm,
                    goal=dispatch_goal,
                )
            except Exception:
                reply = _render_dispatch_template(dispatch_goal)
        else:
            reply = _render_dispatch_template(dispatch_goal)

        result: dict[str, Any] = {
            "reply": reply,
            "action": {
                "type": "dispatch",
                "goal": dispatch_goal,
                "est_note": "session cost lands in the spend meter",
            },
        }
        if dropped_turns:
            result["dropped_turns"] = dropped_turns
        return result

    elif intent == "help":
        if llm_available:
            try:
                reply = _render_via_llm(_RENDER_HELP_PROMPT, llm)
            except Exception:
                reply = _render_help_template()
        else:
            reply = _render_help_template()

    else:  # smalltalk
        if llm_available:
            try:
                reply = _render_via_llm(
                    _RENDER_SMALLTALK_PROMPT, llm,
                    text=text,
                )
            except Exception:
                reply = _render_smalltalk_template(text)
        else:
            reply = _render_smalltalk_template(text)

    result: dict[str, Any] = {"reply": reply}
    if dropped_turns:
        result["dropped_turns"] = dropped_turns
    return result
