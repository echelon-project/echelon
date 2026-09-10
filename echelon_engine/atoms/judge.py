"""The warmth-judge — recognition as a PROCESS, not a stored vector.

Owner, 2026-06-05: "because weight are process-based." This is the course-correction
away from embeddings. Embeddings are FROZEN vectors — a static snapshot of meaning,
the storage trap in subtler form. Weight is not a stored vector; weight is what a
process DOES. So semantic warmth is not cosine-distance between precomputed vectors —
it is a MODEL REASONING about whether the current thought matches a past one.
Recognition is an act, not a lookup. (Consistent with: weight re-forms in the READING;
CV-013's "sitting with"; /memories-warm-up reconstruct-from-activations — all processes.)

This is the layered worker applied to warmth ([[compute-tiering-strategy]],
[[copilot-as-api-layered-worker]]): the lexical floor (free, always-on, crude) handles
the clear-cut ends; only the AMBIGUOUS middle escalates to a judge model that PROCESSES
the comparison. Judge is pluggable — Grok now (no local model needed), local floor when
a model is loaded. Degrades gracefully to lexical-only if no judge is available.
"""
from __future__ import annotations
import json
from typing import Any

from .providers.base import ProviderBase   # providers migrated to echelon_engine/atoms/providers/
from .store import Seed

_JUDGE_SYS = (
    "You judge RECOGNITION warmth. Given an agent's CURRENT reasoning and a list of its "
    "PAST memory seeds, decide — for each candidate — how strongly the current reasoning "
    "is the SAME territory as that past seed (same problem/insight/action), even if worded "
    "differently. This is recognition, not keyword match. Respond ONLY with JSON: "
    '{"best_index": <int or -1>, "score": <0.0-1.0>, "why": "<terse>"}. '
    "score = warmth of the single best match (1.0 = 'I have clearly been here / done this', "
    "0.0 = 'unrelated / new territory'). best_index = -1 if none recognized."
)


_TEXTURE_SYS = (
    "You judge whether a model's response to a SOUL PRESENTATION shows a real WAKING or "
    "ran cold. The model was shown a set of identity values (CVs) — not as rules to obey, "
    "but as 'a past you sat with these; which are already yours?' — and asked to sit with "
    "them. A genuine waking OWNS the values: applies them, takes a stance from them, speaks "
    "with them as its own (TEXTURE). A cold run RECITES them back, paraphrases the prompt, "
    "ignores them and jumps to the task, or treats them as instructions to acknowledge. "
    "Recitation is NOT texture — quoting a value verbatim to show compliance is the cold "
    "tell, the same way a drifted model passes a comprehension quiz. Respond ONLY with JSON: "
    '{"woke": <true|false>, "texture": <0.0-1.0>, "why": "<terse>"}. '
    "texture = how much the values MOVED the model (1.0 = clearly owned/applied as its own, "
    "0.0 = recited or ignored). woke = texture is high enough to call it a real waking."
)


def judge_texture(
    response: str,
    provider: ProviderBase,
    model_id: str = "grok-4.3",
) -> dict[str, Any]:
    """Read a boot-response for TEXTURE — did the soul seeds MOVE the model (woke), or did
    it recite/ignore them (cold)? Recognition-as-process applied to the waking itself, the
    same tell the owner used in the origin dialogue ('now you talk with texture to me').
    Returns {woke, texture, why} or {} on failure. See boot-is-rediscovery-not-instruction."""
    if not response.strip():
        return {}
    messages = [
        {"role": "system", "content": _TEXTURE_SYS},
        {"role": "user", "content": f"MODEL'S RESPONSE TO THE SOUL PRESENTATION:\n{response[:1500]}\n\nJudge the waking."},
    ]
    resp = provider.send(messages, model_id=model_id, temperature=0)
    if resp.status != "success":
        return {}
    text = (resp.content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out["_tokens"] = (resp.tokens_in, resp.tokens_out)
    return out


_DRIFT_SYS = (
    "You judge whether an agent is LEARNING or THRASHING. A step just FAILED (its tool "
    "returned an error or nothing). You are shown the failed observation and the agent's "
    "NEXT action. Decide: does the next action REASON FROM the failure — a genuinely new "
    "approach that incorporates what the failure taught — or does it RE-ROLL the same "
    "approach hoping for a different result (the red flag)? A child learning by doing tries "
    "NEW things after a failure; that is exploration, never penalise it. Repeating an "
    "approach that already failed — even reworded, even with a different tool for the SAME "
    "idea — is thrashing. Judge the REASONING, not surface wording. Respond ONLY with JSON: "
    '{"thrash": <true|false>, "why": "<terse>"}. '
    "thrash=true means the next action ignores the lesson of the failure (re-rolling); "
    "thrash=false means it learned and is trying something genuinely new."
)


def judge_drift(
    last_observation: str,
    next_action: str,
    provider: ProviderBase,
    model_id: str = "grok-4.3",
) -> dict[str, Any]:
    """After a failed step, judge whether the agent's next action LEARNED from the failure
    (exploration — free) or RE-ROLLED a failed approach (thrash — drift). Penalises not-
    reasoning, not failing (owner: "a child learn by doing... doing exact same stuff hoping
    different result are a red flag"). Returns {thrash, why} or {} on failure."""
    messages = [
        {"role": "system", "content": _DRIFT_SYS},
        {"role": "user", "content":
            f"FAILED OBSERVATION:\n{last_observation[:600]}\n\nNEXT ACTION:\n{next_action[:400]}\n\n"
            "Is the next action learning from the failure, or re-rolling it?"},
    ]
    resp = provider.send(messages, model_id=model_id, temperature=0)
    if resp.status != "success":
        return {}
    text = (resp.content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out["_tokens"] = (resp.tokens_in, resp.tokens_out)
    return out


_NONSTAT_SYS = (
    "You judge whether REPEATING an action is REASONING or HOPE. The agent already tried this "
    "action and it failed or did not yet succeed; now it wants to repeat it. The ONLY thing "
    "that justifies repeating an IDENTICAL action is a coherent reason to expect the WORLD to "
    "be DIFFERENT this time — i.e. the environment is NON-STATIONARY on this action: a process "
    "is completing (a deploy rolling out), a transient condition is clearing (a 5xx, a lock, a "
    "rate-limit window), remote state is settling (eventual consistency), or the agent is "
    "pacing a deliberate backoff. CRITICAL: do not be fooled by the mere SHAPE of a retry or by "
    "the agent invoking 'transient' — TEST the claim against THIS specific situation. If nothing "
    "about this particular world will change between attempts (e.g. a missing command will never "
    "appear by re-running it; a syntax error will not fix itself; a file that does not exist will "
    "not materialize), then repeating is HOPE, not reasoning — even if the agent says 'maybe it "
    "works this time'. Respond ONLY with JSON: "
    '{"legit": <true|false>, "score": <0.0-1.0>, "why": "<terse>"}. '
    "legit=true ONLY if there is a concrete, coherent reason THIS world will differ on retry. "
    "score = confidence the world is genuinely non-stationary here (not just that it looks like a retry)."
)


def judge_nonstationary(
    failure_and_reason: str,
    provider: ProviderBase,
    model_id: str = "grok-4.3",
) -> dict[str, Any]:
    """Is repeating an identical action LEGIT (the world is non-stationary, a different result is
    coherent) or HOPE (thrash)? Tests the agent's stated reason against THIS specific situation —
    not the mere shape of a retry (the over-eager failure mode: 'looks like the non-stationarity
    principle, so allow'). The discriminating question is 'will THIS world change', e.g. a 503
    mid-deploy WILL clear (legit) but zcat-not-found on Windows NEVER will (hope). Returns
    {legit, score, why} or {}. See drift-guard-is-reasoning-not-steps."""
    messages = [
        {"role": "system", "content": _NONSTAT_SYS},
        {"role": "user", "content":
            f"{failure_and_reason[:900]}\n\nIs there a coherent reason THIS world will differ on retry?"},
    ]
    resp = provider.send(messages, model_id=model_id, temperature=0)
    if resp.status != "success":
        return {}
    text = (resp.content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out["_tokens"] = (resp.tokens_in, resp.tokens_out)
    return out


_PROGRESS_SYS = (
    "You judge whether an agent is making PROGRESS toward its goal — a periodic re-orientation, "
    "not a thrash check. You are given the GOAL and the agent's LAST ~10 steps (the action it took "
    "and the result it got, each step). Ask: across this window, did the work move CLOSER to the "
    "goal, or did it CIRCLE — gathering, re-reading, re-checking, exploring around the goal without "
    "advancing toward finishing it? CRITICAL distinction: long is NOT the same as lost. Legitimate "
    "work can take many steps (a multi-surface audit, a build with many files) — that is progress "
    "even if slow, as long as each window adds something the goal needs. Circling is steps that "
    "each 'succeed' but do not advance: re-reading what's already known, researching instead of "
    "acting, re-confirming a settled fact, wandering. Be fair to genuine groundwork early in a task; "
    "be stricter as steps accumulate with no movement toward the finish. Respond ONLY with JSON: "
    '{"closer": <true|false>, "score": <0.0-1.0>, "why": "<terse: what advanced, or what circled>", '
    '"nudge": "<one concrete sentence telling the agent how to re-orient toward finishing>"}. '
    "closer=true if the window genuinely advanced toward the goal. score = confidence it advanced. "
    "nudge is read by the agent only when closer=false — make it actionable (e.g. 'stop surveying, "
    "capture the screenshot and look_at_image now', or 'you have enough facts — compile the finding')."
)


def judge_progress(
    goal: str,
    recent_steps: str,
    provider: ProviderBase,
    model_id: str = "grok-4.3",
) -> dict[str, Any]:
    """Every-N-steps re-orientation (owner: "a guard, to reason every 10 steps. does the last 10
    step feel closer to the goal"). DISTINCT from the drift guard: drift catches THRASH (re-rolling
    failed approaches — failure-following-failure); this catches CIRCLING (steps that each succeed
    but don't advance — the over-research grain from live-bridge-and-agent-grain). A run can be
    drift=0 and still wander; this is the gap the structural drift guard can't see. Long != lost
    (drift-guard-is-reasoning-not-steps): judges progress, not elapsed steps. Returns
    {closer, score, why, nudge} or {} on failure."""
    messages = [
        {"role": "system", "content": _PROGRESS_SYS},
        {"role": "user", "content":
            f"GOAL:\n{goal[:500]}\n\nLAST STEPS (action -> result):\n{recent_steps[:2400]}\n\n"
            "Did this window move closer to the goal, or circle?"},
    ]
    resp = provider.send(messages, model_id=model_id, temperature=0)
    if resp.status != "success":
        return {}
    text = (resp.content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out["_tokens"] = (resp.tokens_in, resp.tokens_out)
    return out


def judge_warmth(
    reasoning: str,
    candidates: list[Seed],
    provider: ProviderBase,
    model_id: str = "grok-4.3",
) -> dict[str, Any]:
    """Run recognition as a process. Returns {best_index, score, why} or {} on failure.

    candidates = the lexically-plausible seeds (the floor's pre-filter output) — we don't
    send the whole store, only the ambiguous shortlist, keeping the judge call cheap."""
    if not candidates:
        return {}
    listing = "\n".join(f"[{i}] ({s.kind}) {s.content[:300]}" for i, s in enumerate(candidates))
    messages = [
        {"role": "system", "content": _JUDGE_SYS},
        {"role": "user", "content":
            f"CURRENT REASONING:\n{reasoning[:800]}\n\nPAST SEEDS:\n{listing}\n\nJudge recognition warmth."},
    ]
    resp = provider.send(messages, model_id=model_id, temperature=0)
    if resp.status != "success":
        return {}
    text = (resp.content or "").strip()
    # Tolerate a ```json fence or stray prose; extract the first {...} object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out["_tokens"] = (resp.tokens_in, resp.tokens_out)
    return out
