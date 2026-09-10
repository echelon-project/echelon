"""proxy_gate — the INTENT GATE: give non-Claude models the clarify-before-build instinct.

The problem this solves (owner, 2026-06-26): a non-Claude model (DeepSeek/Grok/etc.) driven through
the ECHELON proxy will confidently BUILD the wrong thing from an ambiguous instruction, because it
lacks the reflex Claude was trained into — "fact-check first, or ask if unsure." Claude self-gates;
other models don't. Since EVERY harness (claude-code, open-router, copilot-chat, gemini-cli) and the
echelon agent all flow through proxy.py, the proxy is the ONE seam where we can give that instinct to
whatever model is behind it, harness-agnostic by construction.

The design mirrors Claude Code's AUTO-MODE instinct: don't nag on every turn. Look at the ACTION TYPE
(is the model about to write/edit files or run destructive bash?) and the STEP HISTORY (is this early,
with no prior recall/read of the target — i.e. building blind?) to decide whether to inject a
clarify-discipline directive. Known-safe / well-grounded path → stay silent (no cost, no nag).

Two knobs, both observable from the request payload the proxy already parses:
  - model       → fire only for non-Claude models (Claude already holds the instinct = the leverage).
  - risk score  → fire only when build-risk is HIGH (ambiguous + about-to-act + ungrounded).

This module is PURE (no I/O, no model calls) so it adds ~no latency and is unit-testable.
"""
from __future__ import annotations

import re

# ── which models LACK the native clarify instinct ────────────────────────────
# Claude/Anthropic models self-gate (trained). Everything else gets the directive.
_CLAUDE_PAT = re.compile(r"claude|anthropic|opus|sonnet|haiku|fable", re.I)


def is_claude_model(model: str) -> bool:
    """True if the model already holds the clarify instinct natively (Claude family)."""
    return bool(_CLAUDE_PAT.search(model or ""))


# ── signals that the model is ABOUT TO ACT (build/destroy), read from the payload ──
# These are the high-stakes verbs in the LATEST user/system instruction. Their presence
# means an irreversible-ish change is imminent → grounding matters more.
_ACTION_SIGNALS = (
    "write", "edit", "create", "build", "implement", "refactor", "rewrite",
    "delete", "remove", "drop", "migrate", "deploy", "rename", "replace",
    "overwrite", "git ", "rm ", "push", "apply", "fix",
)

# Destructive/irreversible verbs — these ALWAYS warrant a confirm, even mid-task.
_DESTRUCTIVE_SIGNALS = (
    "delete", "drop ", "rm -rf", "rm -r", "git push", "force", "overwrite",
    "truncate", "wipe", "purge", "reset --hard",
)

# Signals the instruction is GROUNDED (a concrete file/line/function is cited) — lowers risk,
# because the model isn't guessing the target.
_GROUNDED_PAT = re.compile(
    r"\.(py|js|ts|tsx|jsx|html|css|json|md|sql|sh|ps1|go|rs|java|c|cpp|h)\b"
    r"|:\d+\b|line \d+|function \w+|class \w+|def \w+|`[^`]+`",
    re.I,
)

# Signals prior GROUNDING WORK happened in the step history (the model already recalled/read the
# target) — a well-grounded path, lower risk.
_PRIOR_GROUNDING = (
    "recall --warm", "remember ", "read", "grep", "glob", "cat ", "searched",
    "i found", "the file", "looking at", "## codebase", "tool_result",
)


def _last_user_text(messages: list[dict]) -> str:
    """The text of the most recent user instruction — what the model is about to act on."""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(
                    b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _history_text(messages: list[dict], skip_last_user: bool = True) -> str:
    """Flattened prior turns — used to detect whether grounding work already happened."""
    parts: list[str] = []
    seen_last_user = False
    for m in reversed(messages):
        if m.get("role") == "user" and not seen_last_user and skip_last_user:
            seen_last_user = True
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    parts.append(str(b.get("text", "") or b.get("content", "")))
    return "\n".join(parts)


def classify(payload: dict) -> dict:
    """Score the build-risk of THIS request. Pure function over the parsed payload.

    Returns {risk: 'low'|'high', score: float, reasons: [...], action: bool, destructive: bool,
             grounded: bool, prior_grounding: bool}.

    Heuristic (the 'action type + history of steps' instinct):
      + the latest instruction asks to ACT (build/edit/destroy)        → risk up
      + the act is DESTRUCTIVE/irreversible                            → risk up (a lot)
      - the instruction is GROUNDED (cites a file/line/symbol)          → risk down
      - prior turns show GROUNDING WORK (recall/read/grep happened)     → risk down
      + the instruction is SHORT and imperative (terse ask, no detail)  → risk up
    HIGH risk = about to act AND (not grounded AND no prior grounding), or destructive-while-blind.
    """
    messages = payload.get("messages", []) or []
    last = _last_user_text(messages).lower()
    hist = _history_text(messages).lower()

    action = any(sig in last for sig in _ACTION_SIGNALS)
    destructive = any(sig in last for sig in _DESTRUCTIVE_SIGNALS)
    grounded = bool(_GROUNDED_PAT.search(last))
    prior_grounding = any(sig in hist for sig in _PRIOR_GROUNDING)
    terse = action and len(last.strip()) < 220 and not grounded

    score = 0.0
    reasons: list[str] = []
    if action:
        score += 0.4; reasons.append("instruction asks to act (build/edit)")
    if destructive:
        score += 0.5; reasons.append("destructive/irreversible verb present")
    if terse:
        score += 0.2; reasons.append("terse imperative, no detail")
    if grounded:
        score -= 0.35; reasons.append("instruction is grounded (cites file/line/symbol)")
    if prior_grounding:
        score -= 0.3; reasons.append("prior turns show grounding work")
    score = max(0.0, min(1.0, score))

    # HIGH when about to act while blind, or destructive without solid grounding.
    blind = action and not grounded and not prior_grounding
    high = blind or (destructive and not (grounded and prior_grounding)) or score >= 0.5
    return {
        "risk": "high" if high else "low",
        "score": round(score, 3),
        "reasons": reasons,
        "action": action,
        "destructive": destructive,
        "grounded": grounded,
        "prior_grounding": prior_grounding,
    }


# ── the directive injected into the system tail when the gate fires ──────────
_CLARIFY_DIRECTIVE = (
    "\n\n⛔ ECHELON INTENT GATE — clarify before you build.\n"
    "You are about to act on an instruction that is ambiguous, ungrounded, or irreversible. "
    "Before producing build output:\n"
    "  1. STATE YOUR INTERPRETATION of the request in ONE sentence.\n"
    "  2. LIST the assumptions you will VERIFY against the real code (file/line/contract) — "
    "and verify them (read the file, don't guess) before editing.\n"
    "  3. If the intent is genuinely AMBIGUOUS, or the change is DESTRUCTIVE/IRREVERSIBLE, "
    "say so and ASK rather than guess.\n"
    "Fact-check first; a wrong build from a confident guess is the failure this gate prevents. "
    "If you are already grounded and the path is clear, proceed — do not stall on a clear task."
)

_DESTRUCTIVE_RIDER = (
    "\n  ⚠ This instruction names a DESTRUCTIVE/IRREVERSIBLE action — confirm the target and "
    "blast radius explicitly before executing it."
)


def gate_supplement(payload: dict, *, enabled: bool = True) -> tuple[str, dict]:
    """Decide whether to inject the clarify directive, and return (text, verdict).

    Fires ONLY when: gate enabled AND the model is non-Claude AND build-risk is high.
    `text` is "" when the gate does not fire (no injection, no cost, no nag).
    `verdict` is the classify() dict plus {model, is_claude, fired}.
    """
    model = payload.get("model", "") or ""
    claude = is_claude_model(model)
    verdict = classify(payload)
    fired = bool(enabled and not claude and verdict["risk"] == "high")
    verdict.update({"model": model, "is_claude": claude, "fired": fired})
    if not fired:
        return "", verdict
    text = _CLARIFY_DIRECTIVE
    if verdict["destructive"]:
        text += _DESTRUCTIVE_RIDER
    return text, verdict
