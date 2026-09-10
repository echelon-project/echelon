"""Helper functions for the agent loop — _summarise_branch, _reflect, _offload.

Extracted from loop.py to keep the loop module slim.
"""
from __future__ import annotations
import json
from typing import Any


_OFFLOAD_THRESHOLD = 4000  # chars. ONLY a genuinely context-fattening result offloads. Raised from
                           # 800 (2026-06-06): 800 tripped on small files — a 60-line paths.py (1849
                           # chars) offloaded, then the agent read the HANDLE, whose output ALSO
                           # offloaded, baking an infinite re-read loop (the consult-test circling).
                           # 4000 ~ 60-80 lines of code stays INLINE; only big dumps offload.
_PEEK_LINES = 15           # head lines kept in context as the agent's window onto the offloaded raw.
# Tools whose output is ALREADY a mechanical, distilled structure (the AIFACTOR doctrine: the
# substrate compressed it, don't make an LLM re-compress it). Their output bypasses the summariser
# branch and goes to the trunk whole up to _STRUCTURED_CAP. map_repo is the archetype — its outline
# IS the summary. (Add structured tools here as they're reclaimed, e.g. a future repo_graph query.)
_SYNTH_HARDGATE = 3   # after this many ignored synthesize nudges, LOCK the gather tools (consult/finish only)
_GATHER_TOOLS = {"read_file", "search_file", "list_files", "map_repo", "run_bash"}
_STRUCTURED_TOOLS = {"map_repo"}
_STRUCTURED_CAP = 2500     # chars kept inline for a structured tool before it offloads to a handle.
                           # SMALL on purpose (owner: "offload small but often is N.P — not stingy on
                           # step"). A big map resident in the trunk slows every driver call (v9: 92s/
                           # step). So offload aggressively; the agent re-reads the handle or drills a
                           # subtree as cheap free steps. NEVER summarised (already distilled).
_STRUCTURED_PEEK_LINES = 14  # head lines kept inline when a structured output offloads — the stats
                             # line + first few entries: enough to know what you got + where to drill.


def _summarise_branch(raw: str, goal: str, action: str, history: list, provider, model: str) -> str:
    """The SUMMARISER BRANCH (owner 2026-06-06): a CLONE of the step that exists ONLY to summarise
    a big tool output, so the main driver never has to. It forks the step's FULL context (which is
    slim — every PRIOR tool output in `history` is itself already a summary, so the trunk never
    accumulated raws) + the raw, and asks a cheap model: 'what does this output MEAN for the goal?'
    The trunk absorbs only that conclusion. This is store-the-conclusion-not-the-transcript computed
    by a FRAME-SHARING clone (grounded in the goal), not a blind head-peek the driver had to digest.
    Branch-and-rejoin (uame-makes-parallel-swarm-safe); summarising is a bounded transform delegated
    to the cheap judge tier (CV-001). Returns the grounded summary, or '' on failure (caller falls
    back to a head-peek so a summariser hiccup never loses the output)."""
    sys = ("You are a summariser branch of an agent step. You are given the agent's GOAL, the ACTION "
           "it just took, and the RAW OUTPUT that action produced. Produce a SHORT, GROUNDED summary "
           "of what this output MEANS FOR THE GOAL — the facts/values the agent needs to keep going, "
           "not a generic description. Include concrete specifics (paths, names, counts, errors, the "
           "key lines) but drop boilerplate. If the output is an error, say what failed and the likely "
           "cause. A few sentences. You are this step's understanding, distilled — write what the "
           "driver should KNOW, so it never has to read the raw.")
    # The clone's context = the step's history (already slim — prior outputs are summaries) + the raw.
    msgs = list(history) + [
        {"role": "user", "content":
            f"GOAL: {goal}\n\nACTION JUST TAKEN: {action}\n\nRAW OUTPUT:\n{raw[:14000]}\n\n"
            "Summarise what this means for the goal (grounded, specific, short)."}]
    resp = provider.send(msgs, model_id=model, tools=None)
    return resp.content.strip() if resp.status == "success" and resp.content else ""


def _reflect(goal: str, answer: str, transcript: list, provider, model: str) -> dict:
    """REFLECTION → SOUL-GROWTH (the Reflexion idea, reframed onto ECHELON's path, owner 2026-06-06).
    At task end, a branch reads the whole TRAJECTORY and distills the DURABLE LESSON — not 'what I
    did' (auto-seed already stores GOAL->RESOLVED) but 'what the NEXT run should KNOW': the gotcha
    that cost steps, the approach that worked, the trap to avoid. This is memory-as-weight-adjustor
    made ACTIVE — the soul grows by EXPERIENCE distilled, not transcript stored (CV-013: data->identity
    in the reading). Mine-not-adopt: we take Reflexion's self-critique IDEA, on our seed-bank discipline.
    Returns {lesson, valence, arousal} or {} on failure (caller falls back to the flat auto-seed)."""
    sys = (
        "You are the reflection branch of an agent that just finished a task. A rich run usually "
        "teaches MORE THAN ONE thing — distill 1 to 3 DISTINCT durable lessons, each the kind of "
        "transferable insight that would help a FUTURE, DIFFERENT task. Examples of distinct lessons "
        "from one run: the METHOD that solved the core problem; a separate ENVIRONMENT gotcha that "
        "cost steps; a TOOL trick. Keep them SEPARATE — do not merge a cipher method with a shell "
        "gotcha.\n\n"
        "CRITICAL — each lesson must GENERALIZE, never bake in THIS task's specific input or output. "
        "The next encounter will have DIFFERENT content. WRONG: 'when ROT13 gives \"seed two bravo\", "
        "trust it'. RIGHT: 'a short all-letters ciphertext is likely ROT13 or a Caesar shift — try "
        "those first'. State the SIGNATURE/approach, not the answer.\n\n"
        "For each lesson give a COS COORDINATE — a colon path naming where this knowledge lives "
        "(domain:topic:specific), e.g. crypto:rot13:recognition, env:windows:python, tooling:bash:hex. "
        "Also rate how the run FELT: valence -1..1 (costly struggle negative, clean win positive), "
        "arousal 0..1 (how charged).\n"
        'Respond ONLY with JSON: {"lessons": [{"lesson": "<transferable insight>", '
        '"coordinate": "<domain:topic:specific>"}, ...], "valence": <-1..1>, "arousal": <0..1>}.')
    # Compact the trajectory to action->result lines (the trunk is already slim; this is the record).
    traj = "\n".join(
        f"- {t.get('tool', t.get('role'))}({json.dumps(t.get('args', {}))[:80]}) -> {str(t.get('result',''))[:120]}"
        for t in transcript if t.get("role") == "tool")[:6000]
    msgs = [{"role": "system", "content": sys},
            {"role": "user", "content": f"GOAL: {goal}\n\nFINAL ANSWER: {answer}\n\n"
             f"TRAJECTORY (action -> result):\n{traj}\n\nWhat are the 1-3 durable, generalizing lessons?"}]
    resp = provider.send(msgs, model_id=model, tools=None)
    if resp.status != "success" or not resp.content:
        return {}
    txt = resp.content.strip()
    s, e = txt.find("{"), txt.rfind("}")
    if s == -1 or e == -1:
        return {}
    try:
        d = json.loads(txt[s:e + 1])
    except json.JSONDecodeError:
        return {}
    # normalize: accept the new multi-lesson shape, and stay back-compat with a single {lesson}.
    lessons = d.get("lessons")
    if not lessons and d.get("lesson"):
        lessons = [{"lesson": d["lesson"], "coordinate": d.get("coordinate", "")}]
    clean = [l for l in (lessons or []) if isinstance(l, dict) and l.get("lesson")]
    if not clean:
        return {}
    return {"lessons": clean, "valence": d.get("valence", 0.4), "arousal": d.get("arousal", 0.5)}


def _offload(result: str, tool: str, args: dict, call_id: str, step: int, outputs_dir,
             *, goal: str = "", action: str = "", history: list | None = None,
             provider=None, model: str = "") -> str:
    """Write a large raw tool result WHOLE to the outputs folder; return a context-slim stand-in.
    A SUMMARISER BRANCH (a frame-sharing clone of the step, on the cheap judge model) distills what
    the raw MEANS for the goal — the trunk absorbs only that + a read_file handle to the raw. Falls
    back to a head-peek if no summariser provider is wired or the branch fails. Small results return
    unchanged; reading an already-offloaded handle returns the raw WHOLE (no re-offload loop)."""
    from pathlib import Path
    if len(result) <= _OFFLOAD_THRESHOLD:
        return result
    od = Path(outputs_dir)
    # NEVER re-offload a read of an already-offloaded file (the tail-eating loop, 2026-06-06): when
    # the agent follows a handle (read_file/cat on a path UNDER outputs/), that raw is exactly what
    # it asked for — return it WHOLE, never replace it with another handle pointing at itself.
    pathlike = str(args.get("path") or args.get("cmd") or "")
    if pathlike and od.name in pathlike:   # the arg references the outputs dir -> following a handle
        return result                       # return the raw it asked for; do NOT re-offload (no loop)
    # STRUCTURED-TOOL BYPASS (2026-06-06): some tools ALREADY emit a mechanical, distilled form —
    # the AIFACTOR doctrine made literal (map_repo's output IS the structural summary). Running it
    # through the deepseek SUMMARISER is the exact redundant LLM-compression the doctrine forbids
    # (substrate over the brain): the map is already the compressed truth. So a structured tool's
    # output goes to the trunk WHOLE up to a generous cap, never to the summariser. (This was THE
    # bottleneck of epsilon-co-audit-v8: map_repo fired, then deepseek re-summarised the map.)
    if tool in _STRUCTURED_TOOLS:
        # OFFLOAD SLIM, ALWAYS (owner 2026-06-06: "we are not stingy on step. offload small but
        # often is N.P."). The mentality fix: do NOT hoard a big map inline to "save a re-read" —
        # a fat trunk slows EVERY driver call (the v9 92s/step). Keep the trunk LIGHT; the agent
        # re-reads the handle or drills a subtree as CHEAP EXTRA STEPS (steps are free, being lost
        # is the only cost — drift-guard-is-reasoning-not-steps). Structured output still NEVER
        # goes to the summariser (it's already distilled); it just lands slim + a handle.
        if len(result) <= _STRUCTURED_CAP:
            return result
        od.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in tool)[:24]
        fp = od / f"step{step:03d}_{safe}_{call_id}.txt"
        try:
            fp.write_text(result, encoding="utf-8")
        except OSError:
            return result[:_STRUCTURED_CAP]
        # A SLIM head: the stats line + the first handful of entries (enough to know what you got
        # and decide where to drill) — not a fat char-dump. Trunk stays light, detail is one cheap
        # read away.
        head = "\n".join(result.splitlines()[:_STRUCTURED_PEEK_LINES])
        return (f"{head}\n... [{len(result)} chars total — trunk-slim on purpose. The FULL map is on "
                f"disk: read_file(\"{fp}\"). Better: map_repo a SUBTREE (e.g. directory='module') to "
                f"drill in — a scoped map is a cheap, focused step. Don't hoard the whole map inline.]")
    od.mkdir(parents=True, exist_ok=True)
    # Name by step+tool+call-id so the handle is traceable to exactly this action.
    safe_tool = "".join(c if c.isalnum() else "_" for c in tool)[:24]
    fpath = od / f"step{step:03d}_{safe_tool}_{call_id}.txt"
    try:
        fpath.write_text(result, encoding="utf-8")
    except OSError:
        return result   # if we can't offload, keep the raw inline (correctness over slimness)
    lines = result.splitlines()
    # THE BRANCH: a frame-sharing clone summarises the raw (grounded in the goal). The trunk keeps
    # only this summary + the handle — never the raw, never a blind peek the driver must digest.
    summary = ""
    if provider is not None and history is not None:
        try:
            summary = _summarise_branch(result, goal, action, history, provider, model)
        except Exception:
            summary = ""
    if summary:
        return (f"[tool output: {len(result)} chars, {len(lines)} lines — summarised by a branch "
                f"(full raw: read_file(\"{fpath}\")):]\n{summary}")
    # Fallback (no summariser / branch failed): a head-peek, so an output is never lost.
    peek = "\n".join(lines[:_PEEK_LINES])
    more = max(0, len(lines) - _PEEK_LINES)
    return (f"[tool result offloaded — {len(result)} chars, {len(lines)} lines. "
            f"Read the full raw with read_file(\"{fpath}\"). HEAD peek "
            f"({_PEEK_LINES} of {len(lines)} lines{f'; {more} more' if more else ''}):]\n{peek}")
