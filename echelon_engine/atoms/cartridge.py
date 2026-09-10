"""cartridge — the CARTRIDGE door: compose, list, and EQUIP an earned capability.

THE ROOT DREAM, made a verb (owner 2026-06-20). A cartridge is the substrate's distributable unit:
atom=parameter, card=transformer, warmth=attention, trace=training — a SCOPE of earned atoms you PLUG
IN to BE a capability without re-deriving it. The substrate already had the parts scattered:
  - a SCOPE holds the atoms of one capability (e.g. `ux-cartridge` = the UX role atoms);
  - `cartridge_boot()` (boot.py) foveates a scope on a goal and wakes an agent EQUIPPED with it;
  - `--cartridge craft` on the agent CLI plugs an extra scope alongside the project scope.
What was MISSING was the front door: no way to (a) compose the loose method-atoms of an arc into ONE
named cartridge card, (b) LIST what cartridges exist, (c) EQUIP one by name like a skill. This module is
that door. It adds NO new storage — a cartridge IS a scope + the cards that compose its atoms; this just
surveys and equips them. See:
  - ux-cartridge-built-earns-by-trace-not-by-skill  (a capability is a cartridge, not a skill — it must EARN)
  - the-core-is-a-cartridge                          (the root dream: plug in to BE a capability)
  - tier-cartridge-equipped-by-default-on-the-boot   (a partner wakes holding its tier's cartridge)

THE VERBS:
  list                 — survey cartridges: each scope that holds earned capability atoms, its atom count,
                         earned weight, and the composed cartridge-cards living in it.
  registry             — list every registered CartridgeSpec (the catalog source of truth).
  compose <name>       — (idempotent) compose ANY registered cartridge from cartridge_registry.py into one
                         card chaining its atoms in firing order. The SINGLE generic path — there are no
                         per-cartridge compose functions (compose-ux/-brainstorm are thin back-compat
                         aliases for `compose ux` / `compose brainstorm`).
  equip <scope> [goal] — wake EQUIPPED with a cartridge: foveate the scope on the goal (cartridge_boot) and
                         print the warm moves as the operator's own earned ground. The skill front-end.

CARDS ARE A REGISTRY, NOT HARDCODED: a cartridge's card = its CartridgeSpec.refs composed; register a spec
to add a card. The old hardcoded UX/BRAINSTORM compose blocks were removed (they drifted from the registry).
"""
from __future__ import annotations

import json
import time

from .cards import CardStore


# A composed cartridge-card is marked in born_from so list()/equip() can find it without a new column.
CARTRIDGE_MARK = "cartridge:"

# NOTE (owner 2026-06-25: "cards should be a registry itself, not hardcoded"): the per-cartridge compose
# specs (UX_CARTRIDGE_REFS + compose_ux_cartridge, BRAINSTORM_CARTRIDGE_REFS + compose_brainstorm_cartridge)
# USED to live here as hardcoded blocks. They were the LAST hardcoded cards — a hand-maintained second copy
# that DRIFTED (the old UX_CARTRIDGE_REFS had 16 refs while the registry `ux` had grown to 20). They are
# now GONE: every card composes from cartridge_registry.py via the generic compose() below, which is the
# single source of truth. To add a card, register a CartridgeSpec — never write a compose_<x> function.


def cartridge_cards(cs: CardStore) -> list[dict]:
    """Every composed cartridge-card (born_from carries the cartridge mark), newest first. Each:
    {id, label, scope, refs, score, use_count}. The scope is parsed from the born_from mark."""
    rows = cs.conn.execute(
        "SELECT id,label,refs,score,use_count,born_from FROM cards WHERE born_from LIKE ? ORDER BY ts DESC",
        (f"%{CARTRIDGE_MARK}%",)).fetchall()
    out = []
    for r in rows:
        bf = r["born_from"] or ""
        scope = ""
        if CARTRIDGE_MARK in bf:
            scope = bf.split(CARTRIDGE_MARK, 1)[1].split("|", 1)[0].strip()
        out.append({"id": r["id"], "label": r["label"], "scope": scope,
                    "refs": json.loads(r["refs"] or "[]"), "score": r["score"],
                    "use_count": r["use_count"]})
    return out


def cartridge_list(cs: CardStore | None = None) -> list[dict]:
    """Survey the cartridges on the substrate. A CARTRIDGE = a scope holding earned capability atoms;
    this rolls up, per scope, the atom count + total earned weight + the composed cartridge-cards that
    live in it. Returns a list of dicts (scopes with the most earned weight first):
        {scope, atoms, earned_weight, cards: [<cartridge-card dicts in this scope>]}
    A scope with no composed card still appears (atoms are parameters waiting to be composed); a card
    whose scope has no atoms is surfaced under scope '(unscoped)'. READ-ONLY — never mutates the bank."""
    cs = cs or CardStore()
    # roll up atoms by scope
    by_scope: dict[str, dict] = {}
    for r in cs.conn.execute(
            "SELECT scope, COUNT(*) n FROM atoms WHERE scope!='' GROUP BY scope"):
        by_scope[r["scope"]] = {"scope": r["scope"], "atoms": r["n"], "earned_weight": 0.0, "cards": []}
    # earned weight per scope (the witnessed-door earned table, joined back to atoms.scope)
    for r in cs.conn.execute(
            "SELECT a.scope scope, SUM(e.score) w FROM atoms a JOIN atom_earned e ON a.id=e.atom_id "
            "WHERE a.scope!='' GROUP BY a.scope"):
        if r["scope"] in by_scope and r["w"] is not None:
            by_scope[r["scope"]]["earned_weight"] = round(float(r["w"]), 1)
    # attach the composed cartridge-cards to their home scope
    for card in cartridge_cards(cs):
        sc = card["scope"] or "(unscoped)"
        by_scope.setdefault(sc, {"scope": sc, "atoms": 0, "earned_weight": 0.0, "cards": []})
        by_scope[sc]["cards"].append(card)
    # cartridge scopes first = those that carry a composed card or look capability-shaped, by weight desc
    return sorted(by_scope.values(),
                  key=lambda s: (len(s["cards"]) > 0, s["earned_weight"], s["atoms"]), reverse=True)


def compose(name: str, cs: CardStore | None = None) -> dict:
    """GENERIC compose from the REGISTRY (owner: 'make it a registry, much more cartridges coming'). Compose
    any registered cartridge by name — no per-cartridge function. Idempotent (content-addressed). Returns
    {id,label,refs,present,missing}. This is what `cartridge compose <name>` calls; compose-ux/-brainstorm
    stay as thin aliases for back-compat."""
    from . import cartridge_registry as reg
    spec = reg.get(name)
    if spec is None:
        return {"error": f"no cartridge '{name}' in registry", "known": [s.name for s in reg.all_specs()]}
    cs = cs or CardStore()
    present, missing = [], []
    for coord in spec.refs:
        (present if cs.atom_id_for_coordinate(coord) else missing).append(coord)
    import time as _t
    born = f"{CARTRIDGE_MARK}{spec.scope} | composed {_t.strftime('%Y-%m-%d')} | {spec.summary or name}"
    cid = cs.add_card(spec.label, spec.refs, born_from=born)
    return {"id": cid, "label": spec.label, "refs": spec.refs, "present": present, "missing": missing}


def cartridge_card_for_scope(scope: str, cs: CardStore) -> dict | None:
    """The composed cartridge-card whose home scope is `scope` (the most-recent if several). None if the
    scope has no composed card — then equip falls back to plain home-scope foveation."""
    for c in cartridge_cards(cs):
        if c["scope"] == scope:
            return c
    return None


def cartridge_card_for_label(label: str, cs: CardStore) -> dict | None:
    """The composed cartridge-card by its exact LABEL (the unique key once several cartridges share a home
    scope — e.g. act-ready and partner both file under `echelon`, so scope alone collides). Newest first."""
    for c in cartridge_cards(cs):
        if c["label"] == label:
            return c
    return None


def resolve_cartridge(token: str, cs: CardStore) -> tuple[str, dict | None]:
    """Resolve an equip target by REGISTRY NAME first (the cross-scope-safe key), else by scope (back-compat).
    Returns (home_scope, card-or-None). A registered name pins the exact card by its spec label, so two
    cartridges sharing a home scope no longer collide; a bare scope keeps the old most-recent-in-scope path."""
    from . import cartridge_registry as reg
    spec = reg.get(token)
    if spec is not None:
        return spec.scope, cartridge_card_for_label(spec.label, cs)
    return token, cartridge_card_for_scope(token, cs)


def equip(target: str, goal: str = "", *, cs: CardStore | None = None) -> str:
    """EQUIP a cartridge: wake holding the cartridge's atoms as warm ground, foveated on the goal. The
    user-facing front door (the /cartridge skill calls it). `target` is a registry NAME (preferred — pins
    the exact cartridge even when several share a home scope) or a bare scope (back-compat). Returns the
    assembled opening.

    OPTION A — EQUIP FOLLOWS THE CARD REFS ACROSS SCOPES (council-decided 2026-06-20, unanimous on the
    Gemini floor; see [[atoms-are-scoped-but-a-capability-crosses-scopes]]). The cartridge is the CARD,
    not the home scope: a capability composes across scopes (UX cartridge = roles in `ux-cartridge` +
    methods in `echelon`), so equipping the home scope alone left the method atoms DORMANT (a live
    'lukewarm' equip). So equip now resolves the composed card's refs WHEREVER they live, takes each up
    through the witnessed door (a take-up that earns), and presents them as the cartridge's chain. The
    council's guard: a stale/missing ref is FLAGGED, never silently dropped. If the target has no composed
    card, equip degrades to plain home-scope foveation (cartridge_boot) — back-compat, never worse.

    NAME-FIRST RESOLUTION: once several cartridges share a home scope (act-ready + partner both file under
    `echelon`), scope alone collides (most-recent wins). So equip resolves the target through the registry
    by name first — pinning the exact card by its spec label — and only falls back to scope-keying."""
    cs = cs or CardStore()
    from .store import SeedStore
    from .boot import cartridge_boot
    store = SeedStore()
    scope, card = resolve_cartridge(target, cs)
    # The home-scope foveation is always the BASE (the goal still steers which home-scope atoms surface).
    ctx = cartridge_boot(store, scope, goal or scope)
    base = ctx.opening_message

    if not card:
        return base   # no composed card — plain foveation, as before

    # OPTION A: take up the card's refs across whatever scopes they live in, through the witnessed door.
    chain_lines, missing = [], []
    for coord in card["refs"]:
        aid = cs.atom_id_for_coordinate(coord)
        if not aid:
            missing.append(coord)
            continue
        if cs.recall_peek(aid) is None:        # legacy atom with no compiled spine -> compile first
            cs.compile_atom_struct(aid)
        served = cs.remember_fetch(aid, depth="spine")   # witnessed take-up — earns the kindle
        home = coord.split(":", 1)[0] if ":" in coord else ""
        claim = (served or {}).get("claim", "") if served else ""
        tag = f"«{home}» " if home else ""
        chain_lines.append(f"  • {tag}{claim[:160]}")

    block = [
        "",
        f"◆ THE CARTRIDGE CHAIN (`{card['label']}`) — its {len(card['refs'])} atoms, taken up across "
        f"scopes (the capability composes ACROSS scopes, not just `{scope}`):",
    ]
    block += chain_lines
    if missing:
        # Check whether ALL refs are missing (never-ingested) or just some (stale).
        all_missing = len(missing) == len(card["refs"])
        if all_missing:
            block.append(f"  ⚠ {len(missing)} ref(s) not in bank — ingest the cartridge scope first "
                         f"(echelon setup --bootstrap): " + ", ".join(missing))
        else:
            block.append(f"  ⚠ {len(missing)} ref(s) no longer resolve (stale cartridge — recompile): "
                         + ", ".join(missing))
    return base + "\n" + "\n".join(block)


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────
def _print_named_catalog(cs: CardStore) -> None:
    """The FRONT DOOR: the named, equippable cartridges (the registry) + exactly how to use them. This
    is what 'what cartridges do I have / how do I use them' should answer — names, not 70 raw scopes."""
    from . import cartridge_registry as reg
    specs = reg.all_specs()
    # Count how many refs actually resolve in the bank per spec (go-live audit 2026-07-30:
    # a cold install has zero resolved refs — the column makes the gap visible).
    resolution: dict[str, tuple[int, int]] = {}
    for s in specs:
        resolved = sum(1 for coord in s.refs if cs.atom_id_for_coordinate(coord))
        resolution[s.name] = (resolved, len(s.refs))

    print("CARTRIDGES — an earned capability you PLUG IN by name (atom=parameter, card=transformer, warmth=attention).\n")
    print(f"  {'NAME':<12} {'SKILL':<10} {'RESOLVED':<10} WHAT IT IS")
    print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*40}")
    for s in specs:
        skill = f"/{s.skill}" if s.skill else "-"
        res, total = resolution.get(s.name, (0, len(s.refs)))
        res_str = f"{res}/{total}" if total > 0 else "-"
        print(f"  {s.name:<12} {skill:<10} {res_str:<10} {s.summary}")
    # Flag cartridges with zero resolved refs (go-live audit: ghost cartridges that
    # exist in the registry but have no atoms in the bank — the user can't equip them).
    ghosts = [s.name for s in specs if resolution.get(s.name, (0, 0))[0] == 0 and resolution.get(s.name, (0, 1))[1] > 0]
    if ghosts:
        print(f"\n  ⚠ {len(ghosts)} cartridge(s) have no atoms in the bank: {', '.join(ghosts)}")
        print("    Ingest the cartridge scope first: echelon setup --bootstrap")
    print("\nHOW TO USE:")
    print("  list      what cartridges exist (this view) →  python -m echelon_engine cartridge list")
    print("  registry  every registered spec + its refs  →  python -m echelon_engine cartridge registry")
    print("  equip     wake holding a cartridge, by NAME →  python -m echelon_engine cartridge equip <name> \"<goal>\"")
    print("  compose   (re)build a cartridge's card      →  python -m echelon_engine cartridge compose <name>")
    print("  register  add a user cartridge (~/.echelon) →  python -m echelon_engine cartridge register <name> --scope <s>")
    print("  unregister  remove a user cartridge          →  python -m echelon_engine cartridge unregister <name>")
    if specs:
        eg = specs[-1].name
        print(f"\n  e.g.  python -X utf8 -m echelon_engine cartridge equip {eg} \"what you're about to do\"")


def _print_list(rows: list[dict], cs: CardStore | None = None) -> None:
    if cs is not None:
        _print_named_catalog(cs)
        print("\n" + "─" * 78)
    print("\nFULL SUBSTRATE SURVEY (every scope of earned atoms, composed-cartridges first):\n")
    for s in rows:
        cards = s["cards"]
        head = f"  ◆ {s['scope']:<22} atoms={s['atoms']:<4} earned_weight={s['earned_weight']}"
        print(head)
        for c in cards:
            print(f"      └─ card {c['id'][:12]}  use={c['use_count']:<3} score={round(c['score'],1)}")
            print(f"         {c['label']}")
            print(f"         composes {len(c['refs'])} atoms")


def _main(argv=None):
    import argparse
    import sys
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon cartridge",
        description="The cartridge door: list / compose-ux / equip an earned capability.")
    sub = ap.add_subparsers(dest="verb")
    sub.add_parser("list", help="survey the cartridges on the substrate")
    cp = sub.add_parser("compose", help="compose ANY registered cartridge by name (the registry path)")
    cp.add_argument("name", help="cartridge name from the registry (e.g. ux, brainstorm, scribe)")
    sub.add_parser("compose-ux", help="alias: compose ux")
    sub.add_parser("compose-brainstorm", help="alias: compose brainstorm")
    sub.add_parser("registry", help="list every registered cartridge spec")
    reg = sub.add_parser("register", help="register a user cartridge in ~/.echelon/cartridges.json")
    reg.add_argument("name", help="cartridge name (kebab-case)")
    reg.add_argument("--scope", required=True, help="home bank scope")
    reg.add_argument("--label", default="", help="display label (default: name)")
    reg.add_argument("--home", dest="home_dir", default="", help="memory directory path")
    reg.add_argument("--skill", default="", help="/skill name that equips it")
    reg.add_argument("--summary", default="", help="one-line description")
    unreg = sub.add_parser("unregister", help="remove a user cartridge from ~/.echelon/cartridges.json")
    unreg.add_argument("name", help="cartridge name to remove")
    eq = sub.add_parser("equip", help="wake equipped with a cartridge, foveated on a goal")
    eq.add_argument("target", help="cartridge NAME (e.g. partner, act-ready) or a bare scope (e.g. ux-cartridge)")
    eq.add_argument("goal", nargs="?", default="", help="what you're about to do (foveation signal)")
    a = ap.parse_args(argv)

    cs = CardStore()

    def _emit_compose(rep: dict) -> int:
        if rep.get("error"):
            print(f"⚠ {rep['error']}; known: {rep.get('known')}")
            return 2
        print(f"composed: card {rep['id'][:12]}  ({len(rep['present'])} atoms, {len(rep['missing'])} missing)")
        print(f"  {rep['label']}")
        for m in rep["missing"]:
            print(f"    ⚠ MISSING atom: {m}")
        return 0

    if a.verb in (None, "list"):
        _print_list(cartridge_list(cs), cs)
        return 0
    if a.verb == "registry":
        from . import cartridge_registry as reg
        builtin = [s for s in reg.all_specs() if s.source == "builtin"]
        user = [s for s in reg.all_specs() if s.source == "user"]
        print(f"REGISTERED CARTRIDGES ({len(builtin)} built-in, {len(user)} user):\n")
        for s in builtin:
            print(f"  ◆ {s.name:<12} scope={s.scope:<18} skill={s.skill or '-':<10} ({len(s.refs)} atoms)")
            print(f"       {s.summary}")
        if user:
            print(f"\n  USER-REGISTERED (~/.echelon/cartridges.json):")
            for s in user:
                print(f"  ◇ {s.name:<12} scope={s.scope:<18} skill={s.skill or '-':<10} ({len(s.refs)} atoms)")
                print(f"       {s.summary}")
        return 0
    if a.verb == "register":
        from .cartridge_registry import register_user
        spec = register_user(
            name=a.name, scope=a.scope, label=a.label or a.name,
            home_dir=a.home_dir, skill=a.skill, summary=a.summary)
        print(f"registered: {spec.name} (scope={spec.scope}) → ~/.echelon/cartridges.json")
        print(f"  label: {spec.label}")
        if spec.summary:
            print(f"  summary: {spec.summary}")
        return 0
    if a.verb == "unregister":
        from .cartridge_registry import unregister_user
        ok = unregister_user(a.name)
        if ok:
            print(f"unregistered: {a.name} from ~/.echelon/cartridges.json")
        else:
            print(f"not found: {a.name} (not in user registry)")
            return 1
        return 0
    if a.verb == "compose":
        return _emit_compose(compose(a.name, cs))
    if a.verb == "compose-ux":          # back-compat alias
        return _emit_compose(compose("ux", cs))
    if a.verb == "compose-brainstorm":  # back-compat alias
        return _emit_compose(compose("brainstorm", cs))
    if a.verb == "equip":
        print(equip(a.target, a.goal, cs=cs))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main())
