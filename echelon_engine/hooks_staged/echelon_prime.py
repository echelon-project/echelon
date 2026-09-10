"""echelon_prime.py — the AFFERENT NERVE for the interactive harness (UserPromptSubmit).

THE GAP THIS CLOSES (owner, 2026-07-08): "echelon substrate are the last to be considered, or
sometimes agent really forgot to use it." The field's converged law (Letta core-memory blocks,
Mem0 middleware injection): memory the agent must REMEMBER to consult will be forgotten — the
loop must inject it involuntarily. The SDK channel already has this (claude-echelon nerve.prime
on every turn); this hook is the same afferent nerve for the owner's interactive channel.

WHAT IT DOES: on every real user prompt, run the FREE lexical-floor warmth probe (in-process —
no second python spawn, no judge, no kindle: surfacing is free, use earns) against the scope
resolved from the cwd, and inject a compact primer as additionalContext: verdict + top seeds +
the routing law. The model then CHOOSES to take up an atom (remember <slug>) — injection primes,
the witnessed read earns, so the earn-by-use law is untouched.

SEVERABILITY (inherited from the engine's safe_fire doctrine): ANY failure -> exit 0, no output,
the turn proceeds unprimed. A dead bank must never block the owner's channel. Kill switch:
set ECHELON_NERVE=off.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

_ENGINE_DIR = None


def _engine_dir():
    """The engine checkout, from the estate config (R-0171)."""
    global _ENGINE_DIR
    if _ENGINE_DIR is None:
        _here = str(Path(__file__).resolve().parent)
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from _estate_boot import engine_root
        _ENGINE_DIR = engine_root()
    return _ENGINE_DIR
MAX_REASONING = 400          # chars of the prompt fed to the probe (topic, not essay)
MIN_PROMPT = 20              # shorter than this = greeting/ack, not worth a probe
SKIP = re.compile(r"^\s*(/|hi\b|hello\b|hey\b|ok\b|oke\b|yes\b|no\b|thanks|thank you|boss\b|mantap|lanjut\b|next\b|go\b|y\b|n\b)", re.I)

# TASK-SHAPED prompt = a work-order, not a question. On these, inject the DELEGATION reflex
# (CV-001/002/011) as a CORE BLOCK — rule-based, always-present, never similarity-matched:
# a prompt about "fix the invoice PDF" will never lexically surface a delegation atom, so the
# doctrine must ride the rule, exactly as Letta core-memory rides the system prompt.
TASK = re.compile(r"\b(build|fix|implement|refactor|migrate|audit|review|deploy|create|design|"
                  r"write|generate|scan|port|rewrite|convert|add|remove|rename|update|upgrade|"
                  r"integrate|automate|optimi[sz]e|debug|investigate|research|analy[sz]e|"
                  r"benchmark|test|verify|clean|dedup|consolidate)\b", re.I)
QUESTION = re.compile(r"^\s*(what|why|how|when|where|which|who|is|are|do|does|did|can|could|"
                      r"would|any\b)", re.I)
# DECISION-shaped: a fork the owner is weighing. Councils exist for exactly this; a decision
# prompt fires the organ block even when phrased as a question ("should we X?").
DECIDE = re.compile(r"\b(should (we|i)|choose|decide|decision|trade-?off|vs\.?|versus|"
                    r"which (one|way|approach)|worth it|or should)\b", re.I)

ORGAN_BLOCK = (
    "ECHELON ORGANS — weigh these BEFORE working inline; zero-consideration is the failure mode "
    "(CV-001: intelligence spends itself only on intelligence-class problems; CV-002: a free gate "
    "is the safety organ; CV-011: hand the PROBLEM, not the script):\n"
    "  EQUIP a cartridge when one covers the domain (31 registered: ux, architect, qa, security, "
    "review, debug, refactor, migrate, opt, slim, port, intent, software-house, frontend-*, ...):\n"
    "    echelon cartridge equip <name> \"<goal>\"   (survey: echelon cartridge list)\n"
    "  DELEGATE the labor, keep the gate free:\n"
    "    claude-deep \"<goal>\"                      cheap isolated worker (heavy impl / audit / sweep)\n"
    "    claude-gem \"<goal>\"                       vision/UX worker (screens, design, front-end)\n"
    "    echelon summon [--once|--bus <ch>]        nerve-connected Claude peer (substrate-aware; bus = multi-slice gated)\n"
    "    echelon dispatch | run --goal | swarm     engine partner door / N equipped lenses\n"
    "  COUNCIL a hard fork — never settle a vision/architecture fork alone:\n"
    "    echelon brainstorm \"<question>\"           multi-model judge council (options/defect/honesty/decision/chair)\n"
    "  ANALYZE a page before reasoning about it — don't read a 100k-line page.js by hand:\n"
    "    echelon pagemodel --os-dir <d> --out c.json   deterministic page->GRAPH (modules/nodes/endpoint fan-in); alias chainboard\n"
    "    echelon uispec run --os-dir <d> --slugs a,b    the JUDGMENT half: gemini audit->skeptic gate->synth, worst-first (also: audit/spec/synth/kit + per-stage gates)\n"
    "  GATE every clearing from a context that did NOT build it (delegate-and-gate / swarm-and-gate "
    "skills; skeptic protocol on the bus).\n"
    "  PIPELINE a multi-phase build: equip software-house (pm gates the front, qa gates the back).\n"
    "Inline is right ONLY when it is one-file-one-command fast, or it IS the judgment itself."
)


def _nearest_room(cwd: str) -> Path | None:
    """Nearest built .echelon room; a nested child beats its estate ancestor."""
    try:
        cur = Path(cwd or os.getcwd()).resolve()
        if not cur.is_dir():
            cur = cur.parent
        for base in (cur, *cur.parents):
            room = base / ".echelon"
            if (room / "room.json").is_file():
                return room
    except Exception:
        pass
    return None


def _room_binding(cwd: str) -> tuple[Path | None, str | None, str | None]:
    """Return nearest room plus an explicitly inherited child scope."""
    room = _nearest_room(cwd)
    if room is None:
        return None, None, None
    try:
        data = json.loads((room / "room.json").read_text("utf-8"))
        parent = data.get("parent")
        if not parent:
            return room, data.get("scope"), None
        registry = json.loads(Path(os.path.expanduser("~/.echelon/rooms.json")).read_text("utf-8")).get("rooms", {})
        pmeta = registry.get(parent) or {}
        parent_data = json.loads((Path(pmeta.get("path", "")) / "room.json").read_text("utf-8"))
        parent_scope = parent_data.get("scope") or pmeta.get("scope")
        if not parent_scope or data.get("scope") != parent_scope:
            return room, None, parent
        return room, parent_scope, parent
    except Exception:
        return room, None, None


def _primer(prompt: str, cwd: str) -> str | None:
    sys.path.insert(0, _engine_dir())
    from echelon_engine.atoms.resolve_scope import resolve_scope
    from echelon_engine.atoms.store import SeedStore
    from echelon_engine.atoms.warmth import warmth
    from echelon_sdk.scopegraph import ScopeGraph

    room, inherited_scope, parent = _room_binding(cwd)
    if parent and not inherited_scope:
        raise ValueError("child room scope does not match its registered parent")
    scope = inherited_scope or resolve_scope(cwd or os.getcwd())
    reasoning = prompt[:MAX_REASONING]
    store = SeedStore()
    r = warmth(reasoning, store, scope=scope, scope_graph=ScopeGraph(),
               pre_filter=reasoning)   # lexical floor first: free, ~1s, no earn
    # R-0134 HYBRID (owner ruled 2026-09-01): escalate to the judge ONLY when the floor
    # says LUKEWARM — the ambiguous middle is exactly where wording-match is least
    # trustworthy (the cache-buster atom scored 0.37 lexical / 0.95 judged). COLD and
    # clearly-WARM keep the free floor reading: bounded cost (~1k deepseek tok), latency
    # only when it matters (inside the 12s hook budget). Judge chain = recall.py's
    # default-on resolution; ANY judge failure falls back to the honest floor.
    # Kill switch: ECHELON_NERVE_JUDGE=off.
    tier = "floor"
    if r.verdict == "lukewarm" and \
            os.environ.get("ECHELON_NERVE_JUDGE", "").lower() not in ("off", "0", "false"):
        try:
            from echelon_engine.atoms.recall import _build_judge, _resolve_default_judge
            _cand = _resolve_default_judge()
            if _cand:
                _jp, _jm = _build_judge(_cand)
                _rj = warmth(reasoning, store, scope=scope, scope_graph=ScopeGraph(),
                             judge_provider=_jp, judge_model=_jm, pre_filter=reasoning)
                r = _rj
                tier = f"judged:{_cand}"
        except Exception:
            pass   # throttled seat / bad key / import error -> floor reading stands
    scope_thin = len(store.seeds(scope=scope)) < 15   # fresh workspace = born walled (owner 2026-07-08)

    # WARMTH LEDGER (2026-08-15): the wrap's proof line needs warmth-at-open, and this score
    # was computed here and thrown away — so the line was hand-typed from banner scrollback, or
    # skipped. One append per turn makes `echelon proof-line` read a REAL recorded number
    # instead of recomputing a different one after the fact. Severable: never breaks the turn.
    # SEEDS (council ruling 2026-08-25, C-capped): record what the nerve actually SURFACED —
    # up to 3 warmest atom coords/slugs, never bodies or prompt text. Telemetry only; this
    # ledger must never feed the redeem gate and must never be imported elsewhere.
    try:
        _ledger_path = os.path.expanduser("~/.echelon/warmth_turns.jsonl")
        _MAX_LEDGER_BYTES = 5 * 1024 * 1024   # 5 MB
        try:
            if os.path.getsize(_ledger_path) > _MAX_LEDGER_BYTES:
                _rotated = os.path.expanduser("~/.echelon/warmth_turns.1.jsonl")
                os.replace(_ledger_path, _rotated)   # overwrite any previous rotation
        except FileNotFoundError:
            pass
        _seeds = []
        for sw in r.warmest[:3]:
            _coord = getattr(sw.seed, "coordinate", "") or sw.seed.id
            if _coord:
                _seeds.append(_coord)
        with open(_ledger_path, "a", encoding="utf-8") as _f:
            _f.write(json.dumps({"ts": int(time.time()), "scope": scope,
                                 "score": r.score, "verdict": r.verdict,
                                 "tier": tier, "seeds": _seeds}) + "\n")
    except Exception:
        pass

    # IMPRESSIONS (2026-09-01, the landing-rate re-measure found the gap): recall.py's
    # impression log says "cheap enough for the per-turn nerve hook" — but this hook calls
    # warmth() directly and never logged, so the estate's HIGHEST-VOLUME recall channel was
    # invisible to the landing-rate metric (impressions went silent whenever no explicit
    # `echelon recall` ran). Same shape as recall.py:290-299; best-effort, never breaks a turn.
    try:
        _imp_coords = []
        for sw in (r.warmest or []):
            _c = getattr(sw.seed, "coordinate", "") or sw.seed.id
            if _c:
                _imp_coords.append(_c)
        if _imp_coords:
            store.cards.log_impressions(scope or "global", reasoning, _imp_coords)
    except Exception:
        pass

    _tier_note = "" if tier == "floor" else f"  ({tier}, R-0134 hybrid)"
    lines = [f"[echelon-nerve] warmth on this turn (scope {scope}): "
             f"{r.verdict.upper()} {r.score}  — {r.guidance}{_tier_note}"]
    if room is not None and parent:
        try:
            from echelon_engine import workcycle
            lines.append(f"[child-room] {workcycle.resume_brief(room)}\n[parent-scope] {parent} -> {scope}")
        except Exception:
            lines.append(f"[child-room] {room}\n[parent-scope] {parent} -> {scope}")
    for sw in r.warmest[:3]:
        via = f" «via {sw.via_scope}»" if sw.via_scope else ""
        name = getattr(sw.seed, "coordinate", "") or sw.seed.id
        name = name.rsplit(":", 1)[-1]
        body = (sw.seed.content or "")[:140]
        tag = "" if body.startswith("[") else f"[{name}] "   # bodies often lead with their own slug
        lines.append(f"  - ({sw.score}) {tag}{via}{body}")
    if r.verdict == "warm":
        lines.append("WARM -> REFLEX: known ground; lean on these before searching fresh. "
                     "Take up a full atom (earns): python -X utf8 -m echelon_engine remember <name>")
    elif r.verdict == "lukewarm":
        lines.append("LUKEWARM -> CHECK the warmest seed before acting; "
                     "recall deeper if it looks load-bearing: echelon recall --scope "
                     f"{scope} --warm \"<intent>\"")
    else:
        lines.append("COLD -> THINK: likely new ground — generate fresh, then crystallize "
                     "what pays off as a new atom at wrap.")
    # FRESH-SCOPE FALLBACK (owner, 2026-07-08: "fresh workspace, fresh scope, didn't get the good
    # experience other scopes already banked"). A thin scope's warmth is blind by construction —
    # so the estate's top v2-EARNED atoms travel in «scope»-marked. Earned weight is the
    # constitution-in-waiting until the promote gate is re-based on v2 earning.
    if scope_thin:
        try:
            try:
                # canonical: atom_earned-aware, persona-local kinds excluded (cards.top_earned)
                from echelon_engine.atoms.cards import CardStore
                travel = [(t[0], t[1], t[2]) for t in
                          CardStore().top_earned(4, exclude_scopes=[scope, "echelon-self"])]
            except Exception:
                import sqlite3
                _con = sqlite3.connect(os.path.expanduser("~/.echelon/echelon.db"))
                _cur = _con.cursor()
                _cur.execute("SELECT scope, content, score FROM atoms "
                             "WHERE score > 110 AND scope != ? AND scope != 'echelon-self' "
                             "ORDER BY score DESC LIMIT 4", (scope,))
                travel = _cur.fetchall(); _con.close()
            if travel:
                lines.append(f"FRESH SCOPE ({scope}) — the estate's earned ground travels in:")
                for _sc, _content, _score in travel:
                    lines.append(f"  ★ ({_score:.0f}) «{_sc}» {(_content or '').strip()[:120]}")
        except Exception:
            pass
    # THE ORGAN REFLEX (owner, 2026-07-08: "every fresh session it always needs myself to instruct
    # claude-deep/summon/swarm... cartridge, pipeline, council, skeptic — 0 consideration if i
    # didn't remind it"). Decision-shaped always fires (councils exist for forks, even phrased as
    # questions); task-shaped fires unless it's a bare informational question.
    if DECIDE.search(prompt) or (TASK.search(prompt) and not QUESTION.match(prompt)):
        lines.append(ORGAN_BLOCK)
    return "\n".join(lines)


def _eos_banner_for(cwd: str) -> str | None:
    """Independent of _primer/scope resolution — a cwd with no live bank scope (a fresh
    echelon-fw project, or the framework repo itself) must still see the FRAMEWORK DOOR
    line. Severable: any failure returns None, never raises (P1-3 fix, OPEN-0060 phase A
    gate finding: _primer's UnknownScopeError used to escape to the outer except and
    swallow this banner along with everything else)."""
    try:
        from echelon_eos_awareness import eos_banner
        return eos_banner(cwd)
    except Exception:
        return None


def main() -> int:
    if os.environ.get("ECHELON_NERVE", "").lower() in ("off", "0", "false"):
        return 0
    try:
        raw = sys.stdin.read().lstrip("﻿")   # PowerShell pipes prepend a BOM; shrug it off
        data = json.loads(raw or "{}")
        prompt = (data.get("prompt") or "").strip()
        cwd = data.get("cwd") or ""
        # BANNER FIRST, independent of the prompt-length/SKIP gate and of _primer's scope
        # resolution: an unknown-scope cwd (fresh echelon-fw project, framework repo itself)
        # or a short/greeting prompt must still surface the FRAMEWORK DOOR line.
        banner = _eos_banner_for(cwd)
        if len(prompt) < MIN_PROMPT or SKIP.match(prompt) or "<local-command" in prompt:
            if not banner:
                return 0
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": banner,
            }}))
            return 0
        # TICK THE DELTA TIMER (spec S8 V7): armed + >=3600s -> spawn the delta
        # DETACHED. Never raises, opens no bank; the engine import is paid by
        # _primer below anyway.
        if _engine_dir() not in sys.path:  # guarded like the sibling hooks (gate r1 V7 M-1)
            sys.path.insert(0, _engine_dir())
        try:
            from echelon_engine.atoms.delta import tick
            tick()
        except Exception:
            pass
        try:
            ctx = _primer(prompt, cwd)
        except Exception:
            ctx = None   # e.g. UnknownScopeError from a cwd with no live bank scope
        if banner:
            ctx = f"{ctx}\n{banner}" if ctx else banner
        if not ctx:
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }}))
        return 0
    except Exception:
        return 0   # severed: the owner's turn must never be blocked by the nerve


if __name__ == "__main__":
    raise SystemExit(main())
