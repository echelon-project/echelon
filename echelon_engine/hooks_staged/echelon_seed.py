#!/usr/bin/env python
"""PostToolUse hook — seed Claude's own steps into the UAME spine (~/.echelon/core.db).

The thesis (owner, 2026-06-05): the ECHELON spine — warmth-read + seed-write — is
SUBSTRATE-level, not model-level. The Grok agent runs on it per-step; so can Claude (this
harness), given the right hook on every step. This is that hook. It puts Claude on the SAME
fine-grained spine as the agent: real memory (content-addressed, persists), real knowledge
(accumulates across projects in one db), real emotion (each seed carries valence/arousal).
See memory: claude-can-run-on-uame-via-hooks.

DESIGN (owner's choices):
  - WHEN: PostToolUse — every tool result is a "step" (the agent's per-step loop, applied to me).
  - GATE: warmth-COLD only. Score this step's action+observation against the claude-self scope;
    seed ONLY when it's new ground (cold). Warm re-treads write nothing — record knowledge, not noise.
    Mirrors the agent's auto_seed (it seeds on cold-resolved). Lexical floor only ($0, no model
    call) so the hook is microseconds and never costs money or stalls the turn.
  - STORE: ~/.echelon/core.db via the agent's real store.py (scope+valence+arousal) — NOT the old
    scope-less uame.py. core.db is primary now; markdown ~/.claude memory is for rare big facts.

SAFETY (PostToolUse stalls the session until it returns — a slow/crashing hook hurts every turn):
  - Hard rule: ANY failure -> silent exit 0. Never raise, never print to a non-suppressed channel,
    never hang. A missing spine, a locked db, an import error -> the turn proceeds untouched.
  - Lexical-only warmth (no judge_provider) = no network, no API key, deterministic, fast.
"""
import sys
import json


def _safe_main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return
    ev = json.loads(raw)

    tool = ev.get("tool_name", "")
    tin = ev.get("tool_input", {}) or {}
    # PAYLOAD KEY HEAL (2026-08-31): the harness sends `tool_response`; this hook was born
    # reading `tool_output` and therefore NEVER captured a single step (observation always
    # empty -> pre-gate return). Read both, prefer whichever has substance.
    tout = ev.get("tool_response") or ev.get("tool_output") or {}
    if isinstance(tout, dict):
        out_text = ""
        for k in ("text", "stdout", "output", "content", "result"):
            v = tout.get(k)
            if isinstance(v, str) and v.strip():
                out_text = v
                break
        if not out_text:
            try:
                out_text = json.dumps(tout)[:1200]
            except Exception:
                out_text = str(tout)
        exit_code = tout.get("exitCode", tout.get("exit_code", 0)) or 0
    else:
        out_text = str(tout)
        exit_code = 0
    cwd = ev.get("cwd", "")

    # Trivial-step filter: don't even score pure navigation/reads with no payload. These are
    # the agent's equivalent of a glance — not a step worth a memory. (Warmth would usually
    # call them warm anyway, but skipping is cheaper and clearer.)
    if tool in ("TodoWrite", "Read", "Glob", "Grep", "LS") and exit_code == 0:
        return

    # The "reasoning" we score = the observable action + its observation (same shape the agent
    # scores: content + tool + args). Keep it bounded — warmth is n-gram overlap, not a transcript.
    action = f"{tool} {json.dumps(tin)[:600]}"
    observation = (out_text or "")[:1200]
    reasoning = f"{action}\n{observation}"
    if len(reasoning.strip()) < 12:
        return

    # Import the SPINE (agent's store + warmth). If ECHELON-AGENT isn't importable, this hook is
    # simply inert — silent exit, no harm.
    import os
    from pathlib import Path as _Path
    _here = str(_Path(__file__).resolve().parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from _estate_boot import engine_root
    agent_root = engine_root()          # R-0171: config, never a literal
    if agent_root not in sys.path:
        sys.path.insert(0, agent_root)
    from echelon_engine.atoms.store import SeedStore
    from echelon_engine.atoms.warmth import warmth

    # TIER 1 — CAPTURE into a RAW staging scope (kind='raw'), NOT the scored spine. Raw captures
    # are material, not knowledge; the distill pass (memory/distill.py, run at a boundary with a
    # model) extracts the atomic lesson + coordinate and writes the real atom into claude-self.
    # Litter never reaches the scored spine — only atoms do. (Owner's seed-quality fix, 2026-06-05.)
    RAW_SCOPE = "claude-self-raw"
    SCOPE = "claude-self"
    store = SeedStore()  # ~/.echelon/core.db

    # THE BANK CROSS — fire BEFORE the capture gate (it's independent of new-ground capture).
    # Best-effort + fully guarded so a doorbell never crashes the turn or blocks on slowness.
    try:
        _maybe_bank_offer(reasoning, cwd, store)
    except Exception:
        pass

    # Cheap pre-gate: a capture with no substance isn't even worth staging (it would just be
    # dropped at distill, wasting a model call). Drop empties/acks here, before the DB write.
    if len(observation.strip()) < 8 and exit_code == 0:
        return

    # WARMTH against the ATOM spine (lexical floor only, $0): if this step already resonates with
    # a known atom, it's not new ground — don't even stage it. Gate on cold (genuinely new).
    reading = warmth(reasoning, store, SCOPE, judge_provider=None)
    if reading.verdict != "cold":
        return

    # EMOTION at write-time: a FAILED step (nonzero exit / error text) is charged unpleasant +
    # aroused — so the eventual atom is FELT like dread/regret next time (steers me away). A clean
    # step is mild-positive. The charge rides through distillation onto the atom.
    failed = exit_code != 0 or "error" in observation.lower()[:200] or "traceback" in observation.lower()
    valence, arousal = (-0.5, 0.6) if failed else (0.3, 0.2)

    proj = os.path.basename(cwd.rstrip("/\\")) if cwd else "?"
    content = f"[{proj}] {tool}: {observation[:400]}".strip()
    store.remember(RAW_SCOPE, content, kind="raw", valence=valence, arousal=arousal)


def _maybe_bank_offer(reasoning: str, cwd: str, store) -> None:
    """THE BANK CROSS (bank_offer.py, ported to the hook) — surface dormant ESTATE knowledge as a
    doorbell, not an injection. Two signals must cross: (a) warmth recognizes this situation against
    the estate's OWN scope AND (b) the top atom clears a content threshold. Only then is it the
    honest moment to offer — "you recognize this AND there's real content on file." We print the
    coordinate + a short summary (never the body); the agent CHOOSES to recall it. Lexical-only
    ($0), bounded, best-effort. Runs INDEPENDENTLY of the claude-self capture gate: recognizing
    estate knowledge has nothing to do with whether THIS step is new ground for claude-self."""
    from echelon_engine.atoms.warmth import warmth
    est = _estate_scope(cwd, store)
    if not est:
        return
    cross = warmth(reasoning, store, est, judge_provider=None)
    WARMTH_FLOOR, BANK_THRESHOLD = 0.18, 0.35  # == bank_offer LUKEWARM / OFFER threshold
    if cross.score < WARMTH_FLOOR or not cross.warmest:
        return
    top = cross.warmest[0]
    if (getattr(top, "score", 0.0) or 0.0) < BANK_THRESHOLD:
        return
    seed = top.seed
    coord = seed.coordinate or f"{est}:(uncoordinated)"
    body = (seed.content or "").strip().replace("\n", " ")
    summary = body[:160] + ("..." if len(body) > 160 else "")
    tip = ("\n(.) data bank knowledge available\n"
           f"  coordinate: {coord}\n"
           f"  summary: {summary}\n"
           f"  -> recall.py --scope {est} --warm \"...\" to pull the full entry, if it helps.\n")
    sys.stdout.write(tip.encode("ascii", "replace").decode("ascii"))


def _estate_scope(cwd: str, store) -> str:
    """Resolve the estate's bank scope from cwd — same law as the SessionStart hook: known-map ->
    live-scope leaf match -> kebab of the dir leaf. Returns "" if cwd is unreadable. NEVER defaults
    to another estate's scope (an unknown dir's CROSS searches its own fresh scope, which is empty
    -> no offer, correctly silent). Best-effort; never raises."""
    try:
        import os
        import re
        if not cwd:
            return ""
        leaf = os.path.basename(os.path.normpath(cwd))
        full = cwd.replace("\\", "/").lower()
        known = {
            "sotagle": "sotagle", "alpha-app": "alpha-app", "mol-data-engine": "mol",
            "beta-svc": "mol", "theta-dash": "theta-dash", "flux-stream": "flux",
            "echelon-agent": "echelon", "/echelon": "echelon",
        }
        for needle, sc in known.items():
            if needle in full:
                return sc
        slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", leaf.lower())).strip("-")
        if not slug:
            return ""
        live = {s.scope for s in store.seeds()}
        return slug if slug in live else slug
    except Exception:
        return ""


if __name__ == "__main__":
    try:
        _safe_main()
    except Exception:
        # Hard rule: a hook that fires on EVERY tool call must never crash the turn. Swallow all.
        pass
    sys.exit(0)
