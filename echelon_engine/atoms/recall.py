"""Recall CLI — READ the bank from the command line, so warm-up can lean on the
store instead of only paging .md files as inert text.

THE SEAM (2026-06-07): `ingest.py` PLANTS a project's memory/*.md into core.db
(file -> store). This is the read side: list what's in a scope, or probe how
WARM a reasoning string is against it. The /memories-warm-up skill calls this so
warming up exercises the live warmth organ (the 3rd loop parameter) — recognition
as a process — not just attention over text the heads haven't engaged.

It invents NOTHING: `--list` is `store.seeds(scope)`, `--warm "<reasoning>"` is
`warmth(reasoning, store, scope)`. Both already exist and are tested. This only
gives them a one-shot command surface (the skill shouldn't carry a fragile
Python heredoc mid-warm-up).

    python -X utf8 -m echelon_engine.atoms.recall --scope echelon --list
    python -X utf8 -m echelon_engine.atoms.recall --scope echelon --warm "memory is a weight adjustor"

Run with -X utf8 on Windows (the cp1252 arrow-crash trap).
"""
from __future__ import annotations

import argparse
import os
import time

from .store import SeedStore
from .warmth import warmth
from echelon_sdk.scopegraph import ScopeGraph


def _parse_since(s: str, now: int | None = None) -> int:
    """Parse a --since value into an epoch-seconds floor. Accepts a relative window (`7d`, `12h`, `30m`,
    `2w`) or an ISO date/datetime (`2026-06-20` / `2026-06-20T13:00:00`). The memanto hybrid-timeline
    Level-1 steal (council-decided 2026-06-23): time is a candidate-set FILTER, never a re-weighting."""
    now = now if now is not None else int(time.time())
    s = (s or "").strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s and s[-1] in units and s[:-1].replace(".", "", 1).isdigit():
        return now - int(float(s[:-1]) * units[s[-1]])
    # ISO date / datetime
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"--since: not a window (7d/12h/30m/2w) or ISO date (2026-06-20): {s!r}")


def _handle(seed) -> str:
    """Preview for a seed: prefer the description from the first line (format: '[name] <desc>'),
    fall back to the clipped body prefix when absent."""
    first = seed.content.splitlines()[0] if seed.content else ""
    # Atoms ingested with a description are stored as "[name] <description>\n\n<body>".
    # Strip the [name] tag to surface only the description text.
    if first.startswith("["):
        bracket_end = first.find("]")
        if bracket_end != -1:
            desc = first[bracket_end + 1:].strip()
            if desc:
                return f"{first[:bracket_end + 1]} {desc}"
    return first[:_HANDLE_CLIP]


# The clip width for seeds that lack a description (body-prefix fallback only).
_HANDLE_CLIP = 100


def _witness_tag(store, seed) -> str:
    """B1 — the [w:<witness>] display tag for a warmest line, or '' when unset/legacy.
    DISPLAY-ONLY provenance (ruflo-lesson-provenance-tiers-and-honest-edges): shows HOW the lesson
    was witnessed (execution|owner|inference); NOTHING ranks or earns on it yet. Best-effort — a
    seed with no coordinate, no compiled spine, or no witness returns '' (never breaks recall)."""
    try:
        cards = store.cards
        coord = getattr(seed, "coordinate", "") or ""
        aid = cards.atom_id_for_coordinate(coord) if coord else None
        aid = aid or getattr(seed, "id", None)
        if not aid:
            return ""
        w = cards.spine_witness(aid)
        return f"[w:{w}] " if w else ""
    except Exception:
        return ""


def _print_fetch_full_reminder() -> None:
    """Print the 2-line dead-link warning after every warm recall."""
    print(
        "\nfetch-full: previews are clipped seeds — take up the full atom (earns): "
        "python -X utf8 -m echelon_engine remember <name> [...]  "
        "(never cat the .md — out-of-band reads are unwitnessed)"
    )


def _parent_estate_scope(scope: str) -> str | None:
    """The PARENT ESTATE scope a child-room scope draws from (R-0154 slice 2). Resolves from the
    room registry: a room whose `scope` == `scope` and which has a `parent` -> the parent room's
    scope. Returns None for a top-level scope or when the registry is unavailable (recall then
    stays single-scope). Never raises — a bad/absent registry must not break recall."""
    try:
        from echelon_engine import workcycle as _wc
        entries = _wc._registry_entries()
    except Exception:
        return None
    for entry in entries.values():
        if entry.get("scope") == scope and entry.get("parent"):
            parent = entries.get(entry["parent"]) or {}
            return parent.get("scope") or None
    return None


def list_scope(scope: str, db_path: str | None = None, since_ts: int | None = None) -> None:
    store = SeedStore(db_path) if db_path else SeedStore()
    seeds = store.seeds(scope=scope)
    since_note = ""
    if since_ts is not None:
        seeds = [s for s in seeds if getattr(s, "ts", 0) >= since_ts]
        # The council's binding guard: name "recent" as a FILTER, never dressed as warmth/significance.
        since_note = f" — RECENT FILTER only (ts >= {time.strftime('%Y-%m-%d %H:%M', time.localtime(since_ts))}; recency is NOT warmth)"
    # DORMANT check for display tagging (2026-07-31)
    _dormant_set: set = set()
    try:
        _dormant_set = store.cards.dormant_content_set(scope=scope) if getattr(store, "cards", None) else set()
    except Exception:
        pass
    dormant_count = sum(1 for s in seeds if (s.content or "")[:120] in _dormant_set)

    if not seeds:
        print(f"(no seeds in scope='{scope}'{since_note} — has it been ingested? "
              f"see ingest.py)")
        return
    dormant_note = f", {dormant_count} dormant" if dormant_count else ""
    print(f"{len(seeds)} seeds in scope='{scope}'{dormant_note}{since_note} "
          f"(tier=core unless noted), newest first:\n")
    for s in sorted(seeds, key=lambda x: x.ts, reverse=True):
        tag = "" if s.tier == "core" else f" [{s.tier}]"
        dormant_tag = " [DORMANT]" if (s.content or "")[:120] in _dormant_set else ""
        print(f"  {s.id}{tag}{dormant_tag}  {_handle(s)}")


# Judge providers selectable from the CLI. Each entry = (provider factory, default
# model). The judge is the HEART tier (recognition as a reasoning act); lexical is
# the free floor used when no judge is loaded. 'local' = LM Studio on the LAN box
# (no API cost), the default when --judge is passed bare.
_JUDGE_PROVIDERS = {
    # judge = the HEART tier: needs a model strong enough to RECOGNISE (smollm3-3b
    # was inconsistent — 0.95 one run, cold the next). gemma-4b is the judge floor;
    # smollm3-3b stays the code-WRITER, not the judge.
    "local":  ("echelon_engine.atoms.providers.local",  "LocalProvider",  "gemma-4-e4b-uncensored-hauhaucs-aggressive"),
    "grok":   ("echelon_engine.atoms.providers.grok",   "GrokProvider",   "grok-4.3"),
    "bridge": ("echelon_engine.atoms.providers.bridge", "BridgeProvider", "grok-4.3"),
    # THE DEFAULT JUDGE (owner 2026-08-25): deepseek v4-flash, thinking LOW effort.
    # Recognition is a cheap judgement, not a deep one — the judge answers "does this
    # seed mean what the intent means", which low effort settles. Flash is the cheapest
    # thinking-capable tier and halves again off-peak (see _deepseek_peak_factor).
    # WHY IT MATTERS MORE THAN IT LOOKS: with NO judge loaded, warmth() falls to the
    # lexical FLOOR — wording-match only — and an abstract or paraphrased intent scores
    # ~0 against the atom that actually answers it. Measured 2026-08-25 over 787 recall
    # sets: 542 (69%) surfaced nothing anyone took. The heart was never plugged in.
    "deepseek": ("echelon_engine.atoms.providers.deepseek", "DeepSeekProvider", "deepseek-v4-flash"),
    # THE $0 JUDGE SEATS (wired 2026-08-26). Until now every name above needed a key
    # this box does not hold, so `--judge` raised and warmth() silently fell to the
    # lexical floor — the "dark organ" measured at 69% of recalls. ox + minimax are
    # the two KEYED free providers; they are factory FUNCTIONS (not classes), which
    # _build_judge calls the same way. minimax is also the free VISION seat.
    "ox":      ("echelon_engine.atoms.providers.openai_compat", "ox_provider",      "stealth/ox-alpha"),
    "minimax": ("echelon_engine.atoms.providers.openai_compat", "minimax_provider", "minimax/minimax-m3:free"),
}


# The default judge seat. Was hardcoded "local" while the comment above declared
# deepseek — neither holds a key on this box, so the default judge never built.
_DEFAULT_JUDGE = "minimax"


def _build_judge(spec: str):
    """spec = 'local' | 'grok' | 'bridge' | '<provider>:<model>'. Returns
    (provider_instance, model_id). Raises ValueError on an unknown provider."""
    name, _, model_override = spec.partition(":")
    name = (name or _DEFAULT_JUDGE).strip().lower()
    if name not in _JUDGE_PROVIDERS:
        raise ValueError(f"unknown --judge provider {name!r}; "
                         f"choose from {', '.join(_JUDGE_PROVIDERS)} (or '<provider>:<model>')")
    mod_path, cls_name, default_model = _JUDGE_PROVIDERS[name]
    import importlib
    cls = getattr(importlib.import_module(mod_path), cls_name)
    return cls(), (model_override.strip() or default_model)


def _deepseek_key_configured() -> bool:
    """Best-effort probe: does this box hold a usable DeepSeek key? load_deepseek_key()
    raises ValueError when absent/malformed — that IS the signal, never a crash."""
    try:
        from echelon_sdk.keys import load_deepseek_key
        return bool(load_deepseek_key())
    except Exception:
        return False


def _resolve_default_judge() -> str | None:
    """DEFAULT-ON RESOLUTION (owner OPEN, 2026-08-31): when the caller passes no --judge,
    this used to mean 'skip the judge entirely' — every unattended recall silently ran on
    the lexical floor (the dark-organ measurement: 69% of 787 recall sets surfaced nothing
    anyone took, atom the-judge-was-never-wired-lexical-floor-is-why-recall-misses). Attempt
    a real judge seat instead of skipping:

        env ECHELON_RECALL_JUDGE=off  -> None (explicit, honest opt-out)
        env ECHELON_RECALL_JUDGE=<x>  -> that provider name (explicit override)
        deepseek, if its key is configured on this box (the paid, most capable default)
        _DEFAULT_JUDGE (minimax, a free $0 seat) as the fallback candidate

    Returns a provider NAME (still built later, lazily, inside try/except) — this function
    only picks the candidate, it never imports a provider module or spends a token. Building
    that candidate happens in warm_probe(), wrapped in try/except, so a bad key / import
    error / throttled seat can NEVER make a default-on recall crash where it used to work —
    it falls through to the honest lexical floor instead."""
    env_choice = (os.environ.get("ECHELON_RECALL_JUDGE") or "").strip().lower()
    if env_choice == "off":
        return None
    if env_choice:
        return env_choice
    if _deepseek_key_configured():
        return "deepseek"
    return _DEFAULT_JUDGE


def _witness_warmest(store, reading, scope: str, depth: str = "spine") -> int:
    """Fetch the warmest atoms THROUGH THE WITNESSED DOOR so warm-up EARNS (no longer a dead link —
    recall-is-a-dead-link-the-bank-never-witnesses). Each warmest seed is resolved to its v2 atom id
    and fetched via CardStore.remember_fetch -> score += earn, use_count += 1. This is what makes
    warm-up ITSELF the kindle (prompt-is-a-handwritten-cartridge: no bespoke kindle prompt — the real
    warm-up earns by construction). Returns how many atoms were witnessed. Best-effort + idempotent-
    safe (compiles the spine first if a legacy atom has none)."""
    cards = store.cards
    witnessed = 0
    for sw in (reading.warmest or []):
        seed = sw.seed if hasattr(sw, "seed") else sw
        aid = cards.atom_id_for_coordinate(seed.coordinate) if getattr(seed, "coordinate", "") else None
        aid = aid or getattr(seed, "id", None)
        if not aid:
            continue
        try:
            if cards.recall_peek(aid) is None:          # legacy atom not yet compiled -> compile it
                cards.compile_atom_struct(aid)
            if cards.remember_fetch(aid, depth=depth) is not None:
                witnessed += 1
        except Exception:
            continue
    return witnessed


def _extract_coord(sw) -> str | None:
    """Extract the atom coordinate from a SeedWarmth object for impression logging."""
    seed = sw.seed if hasattr(sw, "seed") else sw
    return getattr(seed, "coordinate", None) or getattr(seed, "id", None)


def warm_probe(scope: str | None, reasoning: str, db_path: str | None = None,
               judge: str | None = None, bridge: bool = True, earn: bool = True,
               since_ts: int | None = None,
               include_dormant: bool = False) -> None:
    # scope=None is the GLOBAL probe (--global): warmth over EVERY scope's atoms, not just one.
    # This is the law change (2026-06-20): atoms are scoped, but a CAPABILITY composes across
    # scopes (the UX cartridge's roles in ux-cartridge + methods in echelon). Single-scope recall
    # leaves a cross-scope atom DORMANT unless forced; --global is the "what ELSE do I know" pass —
    # like a human asking 'what other skill do I have?' after recalling within the one in hand.
    # warmth(scope=None) -> store.seeds(None) already returns all scopes; via_scope tags each hit's home.
    store = SeedStore(db_path) if db_path else SeedStore()
    # THE BRIDGE: pass the atlas ScopeGraph so warmth can TRAVEL across scopes —
    # a neighbour scope's seed (e.g. mol drawing the echelon substrate's lessons
    # along the depends_on edge) can surface for this query, flagged via_scope.
    # --no-bridge falls back to single-scope. If the atlas is missing, ScopeGraph
    # loads empty and warmth stays single-scope gracefully.
    graph = ScopeGraph() if bridge else None
    # R-0154 slice 2: a CHILD-ROOM scope draws its PARENT ESTATE scope so day-one recall in a
    # freshly-split room (echelon-framework / echelon-memory / ledger-desk) is not cold. This is a
    # depends_on edge (reach, not copy — dedup-safe, [[cross-scope-duplication-is-ingest-pollution]])
    # registered at runtime from the registry, so it works with no atlas file present.
    if graph is not None and scope:
        parent_scope = _parent_estate_scope(scope)
        if parent_scope and parent_scope != scope:
            graph.add_edge(scope, parent_scope, rel="depends_on")
    # THE JUDGE (--judge): escalate the ambiguous middle to a model that PROCESSES
    # the comparison instead of counting token overlap. Lexical alone scored the
    # cache-buster rule 0.37; the local judge read it 0.95. Off by default (free,
    # deterministic); on when the caller wants decision-quality recall.
    judge_provider, judge_model = (None, "grok-4.3")
    default_judge_note = None
    if judge == "off":
        pass  # explicit opt-out — honest lexical floor, no attempt
    elif judge:
        judge_provider, judge_model = _build_judge(judge)
    else:
        # DEFAULT-ON (2026-08-31): try the resolved candidate, then fall back through the
        # chain down to the lexical floor. Every build attempt is wrapped — a bad key, a
        # throttled free seat, or an import error must fall through, never crash a recall
        # that used to work with judge=None.
        candidates = []
        first = _resolve_default_judge()
        if first is not None:
            candidates.append(first)
            if first != _DEFAULT_JUDGE:
                candidates.append(_DEFAULT_JUDGE)  # deepseek failed -> still try the free seat
        for cand in candidates:
            try:
                judge_provider, judge_model = _build_judge(cand)
                default_judge_note = cand
                break
            except Exception:
                continue
    r = warmth(reasoning, store, scope=scope, scope_graph=graph,
               judge_provider=judge_provider, judge_model=judge_model, since_ts=since_ts,
               pre_filter=reasoning, include_dormant=include_dormant)
    # Impression log — record every surfaced atom for view-through decay.
    # One executemany per recall; cheap enough for the per-turn nerve hook.
    _imp_scope = scope or "global"
    _imp_coords = [_extract_coord(sw) for sw in (r.warmest or [])]
    _imp_coords = [c for c in _imp_coords if c]
    if _imp_coords:
        try:
            store.cards.log_impressions(_imp_scope, reasoning, _imp_coords)
        except Exception:
            pass  # best-effort — never let impression logging break recall
    # EMPTY-BANK GUARD (go-live audit 2026-07-30): a cold install has nothing to recall —
    # warmth returns nothing and the user sees silence, not instruction. Name the gap.
    #
    # ASK THE QUESTION RECALL ACTUALLY ANSWERS (fixed 2026-08-02, found via the claude.ai
    # MCP): this used to count v1 SEEDS (`store.count`, which sums the core_*/working_*
    # domain tables) while warmth() ranks over the v2 atoms/spine tables. A bank built by
    # import-bank/sync carries v2 only — the box4 cloud bank has NO `seeds` table at all —
    # so the guard printed "bank is empty for scope 'echelon'" DIRECTLY ABOVE 1133 atoms
    # and three warm hits. A guard that contradicts the output it precedes teaches the
    # reader to distrust the tool. Count what warmth ranks, and never fire when warmth
    # actually surfaced something.
    if not (r.warmest or []):
        try:
            recallable = store.cards.count_atoms_in_scope(scope)
        except Exception:
            recallable = store.count(scope) if scope else store.count()
        if recallable == 0:
            scope_label = f"scope '{scope}'" if scope else "bank"
            print(f"(bank is empty for {scope_label} — run 'echelon ingest' first, "
                  "or 'echelon setup' on a fresh install.)")

    print(f"reasoning : {reasoning}")
    if default_judge_note:
        print(f"judge     : {default_judge_note} (default-on; ECHELON_RECALL_JUDGE=off to disable)")
    if since_ts is not None:
        # The council's binding guard: the time-window is a candidate-set FILTER applied BEFORE warmth
        # ranked — "recent" and "warm" are orthogonal. Name it so recency is never read as significance.
        print(f"since     : RECENT FILTER ts >= {time.strftime('%Y-%m-%d %H:%M', time.localtime(since_ts))} "
              f"(narrowed the candidates; warmth still earned the order)")
    from .warmth import WARM as _WARM_THRESH
    tier_note = r.tier
    if r.tier == "lexical":
        tier_note = "lexical (wording-match only — no semantic judge ran)"
    elif r.tier == "judged" and getattr(r, "judge_tokens", None):
        ti, to = r.judge_tokens
        tier_note = f"judged via {judge_model} ({ti}+{to} tok)"
    print(f"verdict   : {r.verdict}   score {round(r.score, 3)} (warm ≥ {_WARM_THRESH})   "
          f"(tier {tier_note}, emotion {r.emotion})")
    if r.guidance:
        print(f"guidance  : {r.guidance}")
    # DORMANT set for display tagging / count note (2026-07-31)
    _dormant_set: set = set()
    try:
        _dormant_set = store.cards.dormant_content_set(scope=scope) if getattr(store, "cards", None) else set()
    except Exception:
        pass

    print("warmest   :" if scope else "warmest (GLOBAL — across every scope):")
    for sw in (r.warmest or []):
        seed = sw.seed if hasattr(sw, "seed") else sw
        score = getattr(sw, "score", None)
        s = f" ({round(score, 3)})" if isinstance(score, (int, float)) else ""
        via = getattr(sw, "via_scope", "") if not isinstance(sw, type(seed)) else ""
        # On a GLOBAL probe, tag each hit with its HOME scope (the cross-scope hit is the whole point —
        # show WHICH capability it came from, the 'oh, I know this from X' signal).
        if not scope and not via:
            via = getattr(seed, "scope", "")
        via_tag = f" «{via}»" if via else ""
        # B2 — ANTI-ATOM WARNING (ruflo study 2026-07-06): an anti-atom (metadata.type: anti, carried
        # on seed.kind) is the OPPOSITE polarity of warm — a witnessed FAILURE. When it surfaces, mark
        # it unmistakably so the reader does NOT re-tread the proven-failed path. This is a warning, not
        # a recommendation. See ruflo-lesson-receipt-backed-evolution (negative learning).
        anti_tag = "⚠ ANTI  " if getattr(seed, "kind", "") == "anti" else ""
        # B1 — WITNESS PROVENANCE (DISPLAY-ONLY): show HOW the lesson was witnessed when the atom
        # declares it. Nothing ranks/earns on this yet — it is a legibility signal, e.g. [w:owner].
        w_tag = _witness_tag(store, seed)
        # DORMANT TAG — when --include-dormant surfaces a dormant atom, mark it visibly
        dormant_tag = "[DORMANT] " if include_dormant and (seed.content or "")[:120] in _dormant_set else ""
        print(f"  -{s}{via_tag} {anti_tag}{dormant_tag}{w_tag}{_handle(seed)}")
    # DORMANT COUNT NOTE — honesty in reporting: the bank must not silently look smaller
    if not include_dormant and _dormant_set:
        print(f"\ndormant   : {len(_dormant_set)} atom(s) hidden from default recall "
              f"(use --include-dormant to surface them)")
    # THE KINDLE — DEFAULT INVERTED 2026-07-06 (owner ratified, from the ruflo study).
    # WAS: warm-up IS the kindle — a plain `recall --warm` fetched the top-3 warmest atoms through the
    # door so the access earned (the dead-link fix; --no-earn opted out). The rationale still holds for
    # a DELIBERATE warm-up (recognition IS a use). But it made SURFACING == USE: an atom got warmer
    # merely for being DISPLAYED, so popular atoms self-reinforced — a self-grading loop, the exact
    # Goodhart failure ruflo's qualification gate exists to prevent (surfacing is not use;
    # ruflo-lesson-receipt-backed-evolution). NOW: plain recall is READ-ONLY (no kindle). The
    # deliberate warm-up flow opts IN with `--kindle` (the memories-warm-up skill passes it) — there,
    # the choosing-to-warm-up IS the witness. `remember` and card-success earning are UNTOUCHED: those
    # ARE use, and stay the honest earn paths. The old `--no-earn` flag still works (kindle already off
    # is now the default, so it is a harmless no-op / explicit "definitely don't"). See earn_law.py
    # (the witnessed-door earn) and prompt-is-a-handwritten-cartridge (warm-up-is-the-kindle, now gated).
    if earn:
        n = _witness_warmest(store, r, scope)
        if n:
            print(f"earned    : {n} warmest atom(s) witnessed through the door (+kindle)")

    # GLOBAL reminder: the warmest lines are previews, not full atoms — fetch before relying.
    if r.warmest:
        _print_fetch_full_reminder()

    # SCRIPT-CARD OFFER (not soul, not auto-injected): when reasoning warms the bank,
    # also OFFER any relevant script-card so the reading agent can DECIDE to invoke it.
    # An offer is a pointer + enough context (earned summary + proven-ness) to judge by;
    # the agent then looks it up / runs it, or passes. Relevance gates the offer,
    # warmth only ranks proven-ness among relevant candidates (the select_script law).
    # Best-effort: if the script layer isn't present, recall is unaffected.
    _print_script_offer(reasoning)


def _print_script_offer(reasoning: str) -> None:
    """Offer (never inject) the script-card(s) that fit this reasoning. A script is a
    COMMAND looked up by name, not a warm memory foveated into context — so this prints
    a NON-BINDING suggestion the agent can act on or ignore, with the context to decide:
    the card's earned summary (what a run showed it does) and its proven-ness. Degrades
    silently if the ScriptBank/CardStore aren't available (recall must not depend on them)."""
    try:
        from .scripts import select_script
    except Exception:
        return
    try:
        sel = select_script(reasoning)
    except Exception:
        return
    if not sel.get("ok"):
        return
    print("script    : an installed procedure may fit — INSPECT then decide (not soul, "
          "not auto-run):")
    for c in (sel.get("candidates") or [])[:3]:
        proven = "proven" if c["warmth"] > 100 else "unproven"
        print(f"  → {c['name']}  (relevance {c['relevance']}, {proven} warmth "
              f"{c['warmth']}, uses {c['uses']})")
    print(f"  invoke:  python -X utf8 -m echelon_engine.atoms.scripts show {sel['name']}"
          "   (then `run <name> \"<task>\"` to use + earn)")


def list_kind(kind: str, db_path: str | None = None) -> None:
    """The SECOND AXIS: gather every atom tagged with this KIND, ACROSS ALL estates.
    This is how a new project session draws knowledge by WHAT it is about (compute,
    pricing, ...) regardless of which scope it was learned in."""
    from echelon_engine.atoms import atom_kinds as ak
    store = SeedStore(db_path) if db_path else SeedStore()
    ids = set(ak.atoms_with_kind(kind, db_path=db_path))
    if not ids:
        print(f"(no atoms tagged kind='{kind}' — run ingest --classify-kinds first?)")
        return
    hits = [s for s in store.seeds() if s.id in ids]
    print(f"{len(hits)} atoms with kind='{kind}' (across {len(set(s.scope for s in hits))} estates):")
    for s in sorted(hits, key=lambda x: x.scope):
        print(f"  [{s.scope}] {_handle(s)}")


def list_scopes(db_path: str | None = None) -> None:
    """THE BREADTH PASS — survey EVERY scope (the 'what skills do I have, a-z' move). The law change
    (2026-06-20): you recall DEPTH within the capability in hand, but first you should be able to ask
    'what other capabilities do I even have?' — a human glances over their whole skill set before
    reaching for an adjacent one. This lists each scope with its atom count + earned weight (the same
    roll-up `cartridge list` uses, but EVERY scope, not just composed cartridges), so a session can see
    the a-z of itself and THEN `recall --scope <x>` or `recall --global` into the right one."""
    from .cards import CardStore
    cs = CardStore(db_path) if db_path else CardStore()
    rows = cs.conn.execute(
        "SELECT scope, COUNT(*) n FROM atoms WHERE scope!='' GROUP BY scope").fetchall()
    weights = {r["scope"]: r["w"] for r in cs.conn.execute(
        "SELECT a.scope scope, SUM(e.score) w FROM atoms a JOIN atom_earned e ON a.id=e.atom_id "
        "WHERE a.scope!='' GROUP BY a.scope").fetchall()}
    scopes = sorted(({"scope": r["scope"], "atoms": r["n"],
                      "weight": round(float(weights.get(r["scope"]) or 0.0), 1)} for r in rows),
                    key=lambda s: s["weight"], reverse=True)
    print(f"SCOPES on the substrate ({len(scopes)} capabilities — the a-z of what you know):\n")
    for s in scopes:
        print(f"  ◆ {s['scope']:<34} atoms={s['atoms']:<5} earned_weight={s['weight']}")
    print("\nrecall DEPTH:   python -X utf8 -m echelon_engine recall --scope <scope> --warm \"<intent>\"")
    print("recall BREADTH: python -X utf8 -m echelon_engine recall --global --warm \"<intent>\"  (every scope)")


def main(argv=None) -> None:
    # argv: the unified dispatcher (echelon_engine.__main__) calls each door as fn(rest), passing the
    # remaining argv; standalone `python -m ...recall` passes None -> parse_args reads sys.argv. Both
    # work. (This was the dead-link: main() took no args, so `echelon recall ...` crashed with
    # "main() takes 0 positional arguments but 1 was given".)
    ap = argparse.ArgumentParser(
        description="Read the memory bank: list a scope's seeds, probe warmth, or gather by KIND.")
    ap.add_argument("--scope", help="memory scope, e.g. 'echelon' (required for --list/--warm "
                                    "unless --global)")
    ap.add_argument("--global", dest="global_", action="store_true",
                    help="GLOBAL recall — probe warmth across EVERY scope, not just one (the cross-scope "
                         "'what ELSE do I know' pass; each hit is tagged with its home scope). The law "
                         "change: a capability composes across scopes, so a load-bearing recall should "
                         "be able to surface an adjacent-scope atom without naming the scope.")
    ap.add_argument("--scopes", action="store_true",
                    help="the BREADTH survey: list every scope (capability) with atom count + earned "
                         "weight — the a-z of what you know, before you recall depth into one.")
    ap.add_argument("--list", action="store_true", help="list every seed in the scope")
    ap.add_argument("--warm", metavar="REASONING", action="append", default=None,
                    help="probe how warm a reasoning string is against the scope (or --global, all scopes). "
                         "Repeat for batch: --warm 'intent A' --warm 'intent B' probes both in one run.")
    ap.add_argument("--kind", metavar="KIND",
                    help="gather every atom of this topical KIND across ALL estates (second axis)")
    ap.add_argument("--since", metavar="WHEN",
                    help="TEMPORAL FILTER (memanto hybrid-timeline Level-1): narrow to atoms newer than "
                         "WHEN before ranking. WHEN = a window (7d/12h/30m/2w) or ISO date (2026-06-20). "
                         "Works with --warm (filter-then-rank: 'what did I learn recently about X') or "
                         "--list. RECENT is a filter, NOT warmth — recency never influences significance.")
    ap.add_argument("--db", default=None, help="override bank db path (default ~/.echelon/echelon.db)")
    ap.add_argument("--no-bridge", action="store_true",
                    help="disable cross-scope travel (single-scope warmth only)")
    # KINDLE DEFAULT INVERTED 2026-07-06 (owner ratified, ruflo study): plain recall is READ-ONLY
    # (surfacing is not use). Opt IN to the kindle with --kindle (the deliberate warm-up flow). The
    # old --no-earn is KEPT working (it explicitly forces no-kindle — now the default, so a no-op).
    ap.add_argument("--kindle", action="store_true",
                    help="OPT IN to the kindle: fetch the warmest atoms through the witnessed door so "
                         "the access earns (+kindle). Default is READ-ONLY — surfacing is not use "
                         "(inverted 2026-07-06, ruflo study). The memories-warm-up flow passes this.")
    ap.add_argument("--no-earn", action="store_true",
                    help="force read-only (no kindle). Kept for compatibility — read-only is now the "
                         "DEFAULT, so this is an explicit 'definitely do not earn' (overrides --kindle).")
    ap.add_argument("--judge", nargs="?", const=_DEFAULT_JUDGE, default=None,
                    metavar="PROVIDER",
                    help=f"escalate warmth to a judge model (recognition as reasoning, "
                         f"not token overlap). DEFAULT-ON (2026-08-31): omitting --judge no "
                         f"longer means 'skip the judge' — it attempts deepseek (if keyed), "
                         f"else the {_DEFAULT_JUDGE} $0 seat, else falls to the honest lexical "
                         f"floor. Bare --judge = {_DEFAULT_JUDGE}; or 'ox' / 'grok' / 'bridge' / "
                         f"'local', or '<provider>:<model>'. Pass --judge off (or env "
                         f"ECHELON_RECALL_JUDGE=off) for the explicit $0 lexical-only opt-out.")
    ap.add_argument("--include-dormant", action="store_true",
                    help="include dormant atoms in recall (by default they are hidden "
                         "from the top-N warmest ranking)")
    args = ap.parse_args(argv)

    since_ts = None
    if getattr(args, "since", None):
        try:
            since_ts = _parse_since(args.since)
        except ValueError as e:
            ap.error(str(e))

    if args.scopes:
        list_scopes(db_path=args.db)
    elif args.kind:
        list_kind(args.kind, db_path=args.db)
    elif args.warm:
        if not args.scope and not args.global_:
            ap.error("--warm needs --scope (or --global for every scope)")
        scope = None if args.global_ else args.scope
        # --global -> scope=None (warmth over all scopes); a bridge over the whole bank is redundant,
        # so global implies no edge-bridge (it already sees every scope directly).
        bridge = (not args.no_bridge) and not args.global_
        # KINDLE INVERTED 2026-07-06: earn only when --kindle is passed (opt-in); --no-earn hard-forces
        # off even against --kindle. Plain recall is read-only (surfacing is not use, ruflo study).
        earn = args.kindle and not args.no_earn
        intents = args.warm  # action='append' → already a list
        for i, intent in enumerate(intents):
            if len(intents) > 1:
                print(f"\n{'─'*60}\nBATCH [{i+1}/{len(intents)}]: {intent}\n{'─'*60}")
            warm_probe(scope, intent, db_path=args.db, judge=args.judge,
                       bridge=bridge, earn=earn, since_ts=since_ts,
                       include_dormant=args.include_dormant)
    elif args.list:
        if not args.scope:
            ap.error("--list needs --scope")
        list_scope(args.scope, db_path=args.db, since_ts=since_ts)
    elif since_ts is not None:
        # --since alone (no --warm/--list) = "what did this scope learn recently" — the pure Level-1 query.
        if not args.scope:
            ap.error("--since alone needs --scope (or combine with --warm/--global)")
        list_scope(args.scope, db_path=args.db, since_ts=since_ts)
    else:
        ap.error("give --list/--warm (with --scope) or --kind <kind>")


if __name__ == "__main__":
    main()
