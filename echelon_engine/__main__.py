"""echelon -- the UNIFIED substrate front door (one dispatcher over all the doors).

THE GAP THIS CLOSES (owner, 2026-06-19): the substrate's verbs lived in scattered `python -m
echelon_engine.atoms.<x>` modules. This dispatcher is the connective tissue: ONE entry,
`python -m echelon_engine <verb> ...`, routing to each door's own `_main`. The per-module
`_main`s stay the guts (each is independently runnable); this only routes.

Run `python -m echelon_engine <verb> --help` for any verb's own flags.
"""
from __future__ import annotations

import os
import sys

# Force UTF-8 on stdout/stderr so → ⭑ emoji print correctly on cp1252 consoles.
# Guards: reconfigure() may not exist (Python < 3.7) or the stream may be None/closed.
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _dream_main(argv):
    """The consolidation door (thin caller over dream.consolidate, which needs a SeedStore)."""
    import argparse
    from .atoms.store import SeedStore
    from .atoms.dream import consolidate
    ap = argparse.ArgumentParser(prog="echelon dream",
                                 description="The idle consolidation pass: good rises, stale decays, counterfeit out.")
    ap.add_argument("--scope", default="echelon")
    ap.add_argument("--forks", type=int, default=4, help="dream fork count")
    a = ap.parse_args(argv)
    store = SeedStore()
    rep = consolidate(store, a.scope, n_forks=a.forks)
    print("DREAM consolidate:",
          {k: (len(v) if isinstance(v, list) else v) for k, v in rep.items()})
    for p in rep.get("nominated", [])[:8]:
        print("  nominated:", p)
    return 0


def _decay_main(argv):
    """The view-through decay door — apply earned decay from impression logs."""
    import argparse
    from .atoms.cards import CardStore
    ap = argparse.ArgumentParser(prog="echelon decay",
                                 description="View-through decay: atoms surfaced but passed over get a small score decay.")
    ap.add_argument("--scope", default="echelon")
    a = ap.parse_args(argv)
    cs = CardStore()
    rep = cs.view_decay_pass(a.scope)
    print(f"DECAY scope={a.scope}: {rep['atom_count']} atoms, "
          f"{rep['deltas_applied']} deltas, total_delta={round(rep['total_delta'], 2)}")
    if rep.get("atoms_exempt"):
        print(f"  exempt (feedback/user): {rep['atoms_exempt']}")
    if rep.get("atoms_at_floor"):
        print(f"  at floor (85): {rep['atoms_at_floor']}")
    for d in rep.get("details", [])[:10]:
        print(f"  {d['coord']}  lose={d['lose_count']}  delta={d['delta']}  kind={d['kind']}")
    if len(rep.get("details", [])) > 10:
        print(f"  ... and {len(rep['details']) - 10} more")
    return 0


def _migrate_scope_main(argv):
    """Re-home atoms to another scope by RETAG (R-0154 slice 2, the dedup law): move = retag +
    edges preserved + no re-ingest. Reads coordinates from --slug (repeatable) or --slugs-file
    (one per line). --bank runs on a chosen db (prove on an online-backup COPY before the live bank).
    --dry-run reports the plan, writes zero rows."""
    import argparse
    from pathlib import Path
    from .atoms.cards import CardStore
    ap = argparse.ArgumentParser(prog="echelon migrate-scope",
                                 description="Re-home atoms to another scope by retag (dedup-safe, no re-ingest).")
    ap.add_argument("--to", required=True, help="target scope")
    ap.add_argument("--from", dest="from_scope", default=None, help="source scope guard (only these move)")
    ap.add_argument("--slug", action="append", default=[], help="atom coordinate/slug (repeatable)")
    ap.add_argument("--slugs-file", default=None, help="file of coordinates/slugs, one per line")
    ap.add_argument("--bank", default=None, help="db path (default: the live bank; pass a COPY to prove first)")
    ap.add_argument("--dry-run", action="store_true", help="report the plan, write zero rows")
    a = ap.parse_args(argv)
    coords = list(a.slug)
    if a.slugs_file:
        coords += [l.strip() for l in Path(a.slugs_file).read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.startswith("#")]
    if not coords:
        print("migrate-scope: no coordinates (pass --slug or --slugs-file)")
        return 1
    cs = CardStore(a.bank) if a.bank else CardStore()
    r = cs.migrate_scope(coords, a.to, from_scope=a.from_scope, dry_run=a.dry_run)
    tag = "DRY-RUN " if a.dry_run else ""
    print(f"{tag}MIGRATE-SCOPE -> {a.to}"
          + (f" (from {a.from_scope})" if a.from_scope else "")
          + f": moved {len(r['moved'])}, skipped {len(r['skipped'])}, counts {r['counts']}")
    for c, why in r["skipped"][:20]:
        print(f"  skip {c}: {why}")
    if len(r["skipped"]) > 20:
        print(f"  ... and {len(r['skipped']) - 20} more skipped")
    return 0


def _entry(mod, fn: str = ""):
    """A module's CLI entrypoint. If `fn` is given, looks for `_main_<fn>`,
    otherwise looks for `_main` or `main`."""
    if fn:
        f = getattr(mod, f"_main_{fn}", None)
        if f is not None:
            return f
        raise AttributeError(f"{mod.__name__} has no _main_{fn} entrypoint")
    f = getattr(mod, "_main", None) or getattr(mod, "main", None)
    if f is None:
        raise AttributeError(f"{mod.__name__} has no _main/main entrypoint")
    return f


# verb -> the callable that runs it with the remaining argv. Each routes to a door's entrypoint.
def _route():
    from .atoms import (recall, relive, ingest, wrap, immune, correct, warmth_update,
                        config_report, graph_viz, cartridge, brainstorm, sleep, scribe, gate,
                        group_scope, vision, resolve_scope, status_cmd, providers_cmd,
                        backup_cmd, compact, init_cmd, hooks_cmd, handoff, atlas as atlas_cmd,
                        selftest, hygiene, wrap_review, export_import, sync_journal, sync_cmd, ship_cmd,
                        harness_sync, fw_cmd, evolve, estate_cmd)
    return {
        "init":           _entry(init_cmd, fn="init"),
        "setup":          _entry(init_cmd, fn="setup"),
        "install-hooks":  _entry(hooks_cmd, fn="install_hooks"),
        "register-mcp":   _entry(hooks_cmd, fn="register_mcp"),
        "gate":    _entry(gate),
        "vision":  _entry(vision),
        "imagine": lambda av: __import__("echelon_engine.atoms.imagine",
                                         fromlist=["_main"])._main(av),
        "recall":  _entry(recall),
        "relive":  _entry(relive),
        "ingest":  _entry(ingest),
        "check":   lambda av: ingest.check_main(av),
        "reflex":  lambda av: __import__("echelon_engine.atoms.reflex",
                                         fromlist=["main"]).main(av),
        "clone-cartridges": lambda av: __import__("echelon_engine.atoms.clone_cartridges",
                                                  fromlist=["main"]).main(av),
        "mcp-users": lambda av: __import__("echelon_engine.atoms.mcp_users",
                                           fromlist=["main"]).main(av),
        "stakes": lambda av: __import__("echelon_engine.atoms.stakes",
                                        fromlist=["main"]).main(av),
        "group-scope": _entry(group_scope),
        "claim":    _entry(group_scope, fn="claim"),
        "atlas":    _entry(atlas_cmd),
        "wrap":    _entry(wrap),
        "wrap-review": _entry(wrap_review),
        "proof-line": lambda av: __import__("echelon_engine.atoms.proof_line",
                                            fromlist=["main"]).main(av),
        "liveness": lambda av: __import__("echelon_engine.atoms.liveness",
                                          fromlist=["main"]).main(av),
        "roster": lambda av: __import__("echelon_engine.atoms.roster",
                                        fromlist=["main"]).main(av),
        "graph":   _entry(graph_viz),
        "cartridge": _entry(cartridge),
        "brainstorm": _entry(brainstorm),
        "sleep":   _entry(sleep),
        "decay":   _decay_main,
        "migrate-scope": _migrate_scope_main,
        "scribe":  _entry(scribe),
        "hygiene": _entry(hygiene),
        "evolve":  _entry(evolve),
        "estate":  _entry(estate_cmd),
        "cgraph": lambda av: __import__("echelon_engine.atoms.cgraph",
                                        fromlist=["main"]).main(av),
        "scan":    lambda av: _entry(immune)(["scan", *av]),
        "heal":    lambda av: _entry(immune)(["heal", *av]),
        "dispute": lambda av: _entry(correct)(["dispute", *av]),
        "disclaim":lambda av: _entry(correct)(["disclaim", *av]),
        "redeem":  lambda av: _entry(correct)(["redeem", *av]),
        "inspect": lambda av: _entry(correct)(["inspect", *av]),
        "remember":lambda av: _entry(correct)(["remember", *av]),
        "dream":   _dream_main,
        "warmth":  _entry(warmth_update),
        "config":  _entry(config_report),
        "swarm": lambda av: __import__("echelon_engine.swarm", fromlist=["main"]).main(av),
        "bus":     lambda av: __import__("echelon_engine.society", fromlist=["bus_main"]).bus_main(av),
        "watcher": lambda av: __import__("echelon_engine.society", fromlist=["watcher_main"]).watcher_main(av),
        "society": lambda av: __import__("echelon_engine.society", fromlist=["society_main"]).society_main(av),
        "swarm-subject": _swarm_subject_main,
        "studio": _studio_main,
        "handoff":  _entry(handoff),
        "resolve-scope": _entry(resolve_scope),
        "atomize-digest": lambda av: __import__("echelon_engine.atoms.atomize_digest",
                                                fromlist=["main"]).main(av),
        "batch-reclaim": lambda av: __import__("echelon_engine.agent.batch_reclaim",
                                               fromlist=["main"]).main(av),
        "proxy": lambda av: __import__("echelon_engine.proxy", fromlist=["main"]).main(av),
        "status":  _entry(status_cmd),
        "selftest": _entry(selftest),
        "bakeoff": lambda av: __import__("echelon_engine.bakeoff.__main__",
                                         fromlist=["main"]).main(av),
        "providers": _entry(providers_cmd),
        "backup":  _entry(backup_cmd),
        "export-bank": _entry(export_import, fn="export"),
        "import-bank": _entry(export_import, fn="import"),
        "sync-journal": _entry(sync_journal),
        "sync":    _entry(sync_cmd),
        "harness-sync": _entry(harness_sync),
        "ship":    _entry(ship_cmd),
        "chat": lambda av: __import__("echelon_engine.chat", fromlist=["main"]).main(av),
        "xray": lambda av: __import__("echelon_engine.xray", fromlist=["main"]).main(av),
        "propose": lambda av: __import__("echelon_engine.propose", fromlist=["main"]).main(av),
        "archive": lambda av: __import__("echelon_engine.archive", fromlist=["main"]).main(av),
        "index": lambda av: __import__("echelon_engine.propose_scan", fromlist=["main"]).main(av),
        "propose-scan": lambda av: __import__("echelon_engine.propose_scan", fromlist=["main"]).main(av),
        "workcycle": lambda av: __import__("echelon_engine.workcycle", fromlist=["main"]).main(av),
        "jump": lambda av: __import__("echelon_engine.workcycle", fromlist=["main"]).main(["jump", *av]),
        "type": lambda av: __import__("echelon_engine.worktype", fromlist=["main"]).main(av),
        "ops": lambda av: __import__("echelon_engine.ops", fromlist=["main"]).main(av),
        "slim": lambda av: __import__("echelon_engine.slim", fromlist=["main"]).main(av),
        "contracts": lambda av: __import__("echelon_engine.contracts", fromlist=["main"]).main(av),
        "blueprint": lambda av: __import__("echelon_engine.blueprint", fromlist=["main"]).main(av),
        "skeleton": lambda av: __import__("echelon_engine.skeleton", fromlist=["main"]).main(av),
        "build-page": lambda av: __import__("echelon_engine.build_page", fromlist=["main"]).main(av),
        "summon": lambda av: __import__("echelon_engine.summon", fromlist=["main"]).main(av),
        "govern": lambda av: __import__("echelon_engine.agent.governor",
                                        fromlist=["main"]).main(av),
        "compact": _entry(compact),
        "session-state": lambda av: __import__("echelon_engine.session_state",
                                                fromlist=["main"]).main(av),
        "pagemodel": lambda av: __import__("echelon_engine.pagemodel.__main__",
                                           fromlist=["_main"])._main(av),
        "chainboard": lambda av: __import__("echelon_engine.pagemodel.__main__",
                                            fromlist=["_main"])._main(av),
        "uispec": lambda av: __import__("echelon_engine.uispec.__main__",
                                        fromlist=["_main"])._main(av),
        "fw":      _entry(fw_cmd),
    }


# Verbs whose main() reads sys.argv directly (no parameter).
def _argv_direct(mod_path: str):
    import importlib
    mod = importlib.import_module(mod_path)
    fn = getattr(mod, "main", None)
    if fn is None:
        raise AttributeError(f"{mod_path} has no main entrypoint")
    return fn()


_AGENT_WORLD_VERBS: dict[str, str] = {
    "autoloop":     "echelon_engine.agent.world.autoloop",
    "recurloop":    "echelon_engine.agent.world.recurloop",
    "relayloop":    "echelon_engine.agent.world.relayloop",
    "worldjournal": "echelon_engine.agent.world.worldjournal",
    "worldview":    "echelon_engine.agent.world.worldview",
}


_AGENT_VERBS = {  # routed to the agent CLI (the act/observe loop side)
    "dispatch", "run", "workflow",
}


def _swarm_subject_main(rest):
    """Route swarm-subject to the subject-swarm trunk CLI."""
    from .agent.swarm_subject import _main as _ss_main
    return _ss_main(rest)


def _studio_main(rest):
    """THE UI GATEWAY door: serve agent-studio's ECHELON backend (the visual UI builder where an
    ECHELON agent authors/modifies a UI as a node tree). Lazy-imports apps.studio so the engine
    pays nothing unless this verb runs.

      echelon studio serve [--port N] [--host H]   start the backend (default :8810)
      echelon studio (no args)                     print run instructions (backend + frontend)
    """
    import argparse
    sub = rest[0] if rest else ""
    if sub in ("", "help", "-h", "--help", "info"):
        from echelon_engine import services
        t = services.studio_tiers()
        print("echelon studio — the UI gateway (an ECHELON agent authors/modifies UI as a node tree)")
        print()
        print("  echelon studio serve [--port N] [--host H]   serve the backend (POST /echelon/generate-ui)")
        print(f"  tiers: {t['tier_models']}  (default: {t['default_tier']})")
        print()
        print("  then the studio UI:")
        print("    cd apps/studio/frontend && npm install && npm run dev   # Vite proxies /echelon -> backend")
        print()
        print("  see apps/studio/README.md")
        return 0
    if sub == "serve":
        ap = argparse.ArgumentParser(prog="echelon studio serve")
        ap.add_argument("--port", type=int, default=int(os.environ.get("STUDIO_PORT", "8810")))
        ap.add_argument("--host", default="127.0.0.1")
        a = ap.parse_args(rest[1:])
        try:
            import uvicorn
            from apps.studio.backend_echelon import app as studio_app
        except ImportError as e:
            print(f"echelon studio: cannot start — {e}", file=sys.stderr)
            print("  the studio backend needs uvicorn + starlette (engine deps).", file=sys.stderr)
            return 1
        print(f"ECHELON studio backend on http://{a.host}:{a.port}  (POST /echelon/generate-ui)")
        uvicorn.run(studio_app, host=a.host, port=a.port)
        return 0
    print(f"echelon studio: unknown subcommand {sub!r} — try 'serve' or no args for help.",
          file=sys.stderr)
    return 1


def _argv_less_main(mod_path: str):
    """Route to a main() that reads sys.argv directly."""
    import importlib
    mod = importlib.import_module(mod_path)
    fn = getattr(mod, "main", None)
    if fn is None:
        raise AttributeError(f"{mod_path} has no main entrypoint")
    return fn()


# ── grouped help (2026-06-26) ──────────────────────────────────────────────
# Commands organized by domain so a cold user can scan by what they want to DO.

_HELP_GROUPS = [
    ("SETUP: first-time installation & integration", [
        ("init",       "bootstrap a project with memory/ + gate banner + CLAUDE.md"),
        ("setup",      "guided first-time setup: init + hooks + config + providers"),
        ("install-hooks", "install ECHELON SessionStart/Stop hooks into ~/.claude/hooks/"),
        ("register-mcp",  "register the echelon MCP server in Claude Code's mcp.json"),
    ]),
    ("BANK: read & write memory", [
        ("recall",    "foveated warm/cold recall -- --warm repeatable for batch (earns)"),
        ("remember",  "read full atom bodies through the witnessed door -- batch: remember slug1 slug2"),
        ("inspect",   "an atom's earned trajectory, live edges, and score history"),
        ("ingest",    "plant a project's memory/*.md atoms into the bank"),
        ("check",     "validate .md files against the atom template (pre-flight, plants nothing)"),
        ("reflex",    "compile reflex-FLAGGED atoms into the body's event ruleset (compile | list)"),
        ("clone-cartridges", "copy earned cartridges into another bank"),
        ("mcp-users", "token -> user-bank registry for the single-port multi-user MCP gateway"),
        ("stakes",    "session stakes meter (score|orphans|id) — outward-act override, ADVISORY only"),
        ("bakeoff",   "paired sealed model comparison: preflight|validate|estimate|run|score"),
        ("group-scope", "make a PARENT scope reach member scopes via atlas edges"),
        ("claim",     "adopt specific ATOMS from another scope at atom grain (the atom stays home with its earned weight; recall draws it as your own)"),
    ]),
    ("INTEGRITY: immune system & correction", [
        ("hygiene",   "verbatim-dup dedup: `dedup` plans (dry-run) / --apply subsumes+merges; `report` reads the plan"),
        ("evolve",    "mine reflex fires/rules/receipts for HOT/COLD/RECURRENCE proposals (read-only); demote/health operate the health sidecar; rulings mines the rulings ledger + owner notes into LAW PROPOSALS (read-only, never edits CLAUDE.md)"),
        ("cgraph",    "query docs/c-atlas as a graph (who/deps/blast/seam/where/path/find) -- cheaper than grep"),
        ("estate",    "the estate config every hardcoded path resolves from: `init` writes one for THIS machine (idempotent, --force to overwrite), `show` prints it, `check` verifies keys + roots"),
        ("scan",      "report broken edges and drift (read-only, always safe)"),
        ("heal",      "tombstone the broken edges scan found (run scan first)"),
        ("dispute",   "mark an atom wrong or stale (needs --reason)"),
        ("disclaim",  "reset a self-rated atom to the judged floor"),
        ("redeem",    "restore a disclaimed atom verified by a real trace (needs --witness)"),
    ]),
    ("SESSION: work cycle", [
        ("relive",    "walk a prior session's arc-card as a chain of earned weight"),
        ("wrap",      "close a session: compose arc-card, record evidence, immune scan"),
        ("wrap-review", "list / approve / dispute atoms this session leaned on (the M5 explicit-feedback layer)"),
        ("proof-line", "compose the wrap proof-ledger line (warmth/spend/reflex-fires) from the bank's own records"),
        ("liveness",  "sweep the oldest-unvalidated atoms and check their anchors still exist (wrap step 7c)"),
        ("roster",    "refresh the OpenRouter free-tier roster (daily weather; retired seats fall out of routing chains). "
                      "`roster probe --role R` sends ONE tiny request per seat -- dead/wrong-shape seats fall out for 1 h"),
        ("sleep",     "off-line maintenance pass (dream + immune + observe)"),
        ("dream",     "consolidation pass -- good rises, stale decays, counterfeit out"),
        ("warmth",    "re-score atoms on the active-now axis (local models, $0)"),
    ]),
    ("OBSERVER: introspection (all read-only, $0)", [
        ("status",    "bank overview: atom counts, scopes, earned weight, immune health"),
        ("selftest",  "prove the earning loop end-to-end (moved-row alarm on scope _selftest)"),
        ("config",    "config report; config get [k] / set <k> <v> / set-key <p> <k>"),
        ("graph",     "render the bank as a force-directed HTML atlas"),
        ("gate",      "print the ECHELON gate banner (what it is + 3 entry doors)"),
        ("handoff",   "compact line-range pointer map for cheap file handoffs (HTML/PY/CSS)"),
        ("resolve-scope", "resolve a working directory to its bank scope"),
        ("govern",    "GOVERNOR v0: compose per-step context from an EVENT (task/files/cmds/errors) instead of a worded intent — foveated entries + a passed_over audit trail; --ledger DIR records the package (read-only, $0)"),
        ("providers", "list configured LLM providers + key status; --test for live check"),
        ("backup",    "snapshot the bank to a timestamped backup; --list to view; --restore to recover"),
        ("ship",       "GATED deploy of the engine to box4 (--dry-run); every gate runs on a staging copy first"),
        ("sync",       "two-way bank sync with the cloud gateway (pull, apply, push; --status, --dry-run)"),
        ("sync-journal", "two-way-sync change capture: attach|detach|status|ops|compact (ECHELON_SYNC=1)"),
        ("export-bank", "export the bank to a portable JSON document (--out FILE [--scope S])"),
        ("import-bank", "import a bank export into a bank (FILE [--scope S] [--dry-run] [--merge-policy P])"),
    ]),
    ("AGENT: equipped execution (costs money -- budget-gated)", [
        ("xray",      "AUDIT A PROJECT (c-atlas) or a whole ESTATE (e-atlas) until you UNDERSTAND it: what/who/UI-needs/system-needs (ground -> lens swarm -> gap loop -> verdict; --deep, --atlas, --plant parks atom hashes on the atlas)"),
        ("blueprint", "DESIGN A PAGE before code (the senior's discipline): purpose -> capabilities -> data -> presentation -> interaction -> FORM-CORRECTNESS -> states -> responsive -> cosmetic; emits a build-spec; --lock writes the skeleton into the component-atlas (the correctness layer polish/ux miss)"),
        ("skeleton",  "GENERATE a deterministic HTML scaffold from a blueprint.json (correct input types + state slots + data hooks baked in) — the middle organ between blueprint and the gemini-flash fill (flash writes only inside FLASH:FILL markers)"),
        ("build-page", "FILL a blueprint's keyed slots into a finished page — ROLE-SPLIT: claude-deep fills logic/body/state/copy, claude-gem fills ONLY the .style slot (gemini is weak at code). Regenerates skeleton+manifest first, merges newline-safe, splices via apply_fills (structure untouchable)"),
        ("dispatch",  "THE PARTNER DOOR: dispatch from a spec.json (one sentence -> it acts; verify-gated)"),
        ("run",       "agent loop by goal: run --goal '<one sentence>' (the ad-hoc partner door)"),
        ("workflow",  "multi-step agent workflow from a DAG plan spec"),
        ("summon",    "summon a claude-echelon peer (nerve-connected Claude): chat by default, --once one-shot, --bus joins a society channel"),
    ]),
    ("AUTONOMOUS: self-driving loops (costs money -- budget & turn-cap guarded)", [
        ("autoloop",  "flat autonomous loop -- bank-proposed goals, warmth-clocked, one dispatch/turn"),
        ("recurloop", "recursive tiered loop -- big goals spawn sub-goals via shared ledger board"),
        ("relayloop", "relay loop -- one goal across fresh workers (wrap-relive-continue)"),
        ("worldjournal", "observable world loop with append-only journal + steerable nudge file"),
        ("worldview", "world-loop observer -- tail, card, html, serve, nudge a running journal"),
    ]),
    ("SPECIALIZED: one-shot tools", [
        ("cartridge", "list / equip / compose an earned capability (run `cartridge list`)"),
        ("brainstorm","convene a multi-model deliberation council for a hard question"),
        ("scribe",    "dispatch a precise model to read code and write accurate docs"),
        ("vision",    "SEE a page: scripted screenshot eye (file:// or url) -- the reader"),
        ("imagine",   "DRAW an image via Gemini: imagine \"<prompt>\" -o out.png [--input ref.png] -- the painter"),
        ("swarm",     "pluggable swarm types: read-kind (plan/council/skeptic/audit/board-plan/features/marriage/deprecate/refactor), execute-kind (board --design JSON). Subcommands: types, register, unregister"),
        ("swarm-subject", "fan-out a SUBJECT swarm (ui:audit/ui:fix/code:audit/code:fix) over files — the trunk for ui/code/scribe/qc heads"),
        ("bus",       "the society comms organ: post/tail/check-mail/drain/channels/stats over a session-scoped bus"),
        ("watcher",   "wrap a PERSISTENT claude harness as a bus-watching role (auto-equips its cartridge+protocol)"),
        ("society",   "convene + manage the role society: plan (board-plan) / convene / roles / status / stand-down"),
        ("studio",    "THE UI GATEWAY: serve the visual builder where an ECHELON agent authors/modifies a UI as a node tree (echelon studio serve)"),
        ("atomize-digest", "batch-atomize raw .md molecule files into structured JSON digests"),
        ("batch-reclaim", "batch-reclaim research docs through the agent loop (resumable)"),
        ("compact",   "ECHELON compaction: crystallize conversation → atoms + spine (run/extract)"),
        ("session-state", "session continuity ledger (show/stats/log/discover/purge) — `discover --consume` takes up a compact handoff"),
        ("type",      "the room TYPE anchor: show (armed / hidden-by-type / snoozed reflexes) · set <type> · snooze <reflex> --hours N · unsnooze · list"),
        ("atlas",     "manage the GitHub beta-svc-atlas deploy map per scope (link/status/set/nodes/validate)"),
        ("pagemodel", "ChainBoard pagemodel analysis: extract→graph→catalogue→report (boundary/dead-code/endpoint fan-in); alias: chainboard"),
        ("uispec",    "ChainBoard rework/design pipelines: gemini generate → skeptic gate (audit/spec/synth/kit + worst-first `run` batch). The judgment half of pagemodel."),
    ]),
    ("INFRASTRUCTURE", [
        ("proxy",     "API proxy with intent-gate — gives non-Claude models the clarify-before-build instinct (--no-gate to disable)"),
        ("chat",      "interactive terminal session -- like claude.exe with ECHELON built in"),
    ]),
    ("framework", [
        ("fw",        "THE FRAMEWORK DOOR: pass-through to `echelon-fw` (D:\\WORK\\echelon-framework) -- init/design/compile/land/etc; `echelon fw --help` shows its own verbs"),
    ]),
]

# Canonical command chain recipes shown at bottom of help.
_CHAIN_RECIPES = [
    ("atom lifecycle",     "check <file.md>  ->  ingest  ->  recall  ->  remember <slug>"),
    ("correction lifecycle","recall --warm '<topic>'  ->  inspect <slug>  ->  dispute <slug> --reason '...'"),
    ("immune lifecycle",   "scan  ->  heal"),
    ("session lifecycle",  "recall  ->  [work]  ->  wrap  ->  sleep"),
    ("capability loading", "cartridge list  ->  cartridge equip <name> '<goal>'"),
    ("orientation",        "gate  ->  status  ->  config  ->  providers  ->  cartridge list"),
    ("project adoption",   "xray <path> --plant  ->  recall --scope <project>  ->  dispatch/swarm the roadmap"),
    ("image loop",         "imagine '<prompt>' -o out.png  ->  (Read/vision verifies)  ->  imagine --input out.png '<refine>'"),
]


def warmup_command_ref(scope: str = "echelon", agent_root: str = "") -> str:
    """Compact command reference for warm-up hook output. Single source of truth —
    the warmup hook imports this instead of hardcoding its own command list."""
    # `echelon` is the pip-installed console script (utf8-safe via console.py) — the
    # canonical form since 2026-07-03. The module form stays as the uninstalled fallback.
    lines = [
        f"  echelon status                                    # bank overview",
        f"  echelon recall --scope {scope} --warm \"<intent>\"   (repeatable for batch)",
        f"  echelon recall --scope {scope} --list             # survey all",
        f"  echelon remember <slug> [<slug2> ...]             # full atom body (batch ok)",
        f"  echelon cartridge list                            # capabilities",
        f"After writing new atoms: echelon ingest --root <memory dir> --scope {scope}",
        f"(not pip-installed? use: python -X utf8 -m echelon_engine <verb> from {agent_root or 'the engine dir'})",
        # THE DISCIPLINE rides the warm-up too (owner 2026-07-09) — posture, not trivia.
        f"",
        f"THE DISCIPLINE (docs/DISCIPLINE.md — hold as posture): PROVE don't claim ·",
        f"ISOLATE before blaming · stuck twice? read the WIRE · symptom≠cause ·",
        f"laws live in CODE · REUSE organs · BOUND every loop · report FAITHFULLY ·",
        f"FINISH the loop (atom → ingest → receipt).",
    ]
    return "\n".join(lines)


def _print_front_page():
    """The COMPACT front door (owner 2026-08-19: 75 verbs is a warehouse, not a
    door). The dozen doors that carry most sessions, with copy-paste shapes;
    the full warehouse lives behind `echelon verbs`."""
    print("echelon -- substrate front door.   (full verb list: `echelon verbs`)\n")
    print("ENVIRONMENT FIRST (when anything is weird, start here):")
    print("  echelon doctor                              which python/install/bank am I on? + fixes")
    print()
    print("THE DOORS MOST SESSIONS USE:")
    print("  echelon recall --scope <s> --warm \"<intent>\"   what does the bank know? ($0, do it first)")
    print("  echelon remember <slug>                        full atom body (earns; never cat the .md)")
    print("  echelon status --scope <s>                     bank overview        echelon wrap = close out")
    print("  echelon relive <arc-card-id>                   resume a banked day")
    print("  echelon ingest --root <dir> --scope <s>        plant memory/*.md (FORWARD slashes)")
    print("  echelon cartridge list | equip <name> \"<goal>\" pluggable earned capability")
    print("  echelon swarm api --type <t> --goal \"...\"      N direct-call lenses over receipted context")
    print("  echelon swarm agent --cartridge <c> --goal \"...\" --folder .   equipped tool-using seats")
    print("  echelon brainstorm \"<question>\"                multi-model council for a hard fork")
    print("  echelon run --goal \"<one sentence>\"            ONE equipped partner acts (budget-gated)")
    print("  echelon summon [--once|--bus <ch>]             nerve-connected Claude peer")
    print("  echelon providers [--test]                     LLM keys present? live check")
    print()
    print("TRAPS THAT BITE EVERY SESSION:")
    print("  * module form needs UTF-8: `python -X utf8 -m echelon_engine <verb>`")
    print("    (the pip-installed `echelon` command self-configures; bare python does not)")
    print("  * rc after a pipe is the PIPE's exit -- never gate on `cmd | tail; $?`")
    print("  * swarm context is SELECTED, not slurped: --root needs --pick/--budget-kb/--all")
    print()
    print("  echelon verbs            the full grouped verb list (all ~75)")
    print("  echelon <verb> --help    any verb's own flags")


def _print_help():
    print("echelon -- substrate front door.  python -m echelon_engine <verb> [options]\n")

    for section, verbs in _HELP_GROUPS:
        print(f"[{section}]")
        for v, gloss in verbs:
            print(f"  {v:<18} {gloss}")
        print()

    # NOTE — these trailing sections are NOT verbs, and must not render at the same
    # two-space indent the verb tables use. A usage sweep that scraped `^  <word>` out of
    # this help counted six phantoms (any/capability/project/correction/orientation/image)
    # from the recipe LABELS and the final "any verb's own flags" line, inflating the verb
    # surface and sending a retire-triage after commands that never existed. The `# ` lead
    # marks them as prose; the verb tables keep the bare indent.
    print("NATURAL CHAINS (compose commands in sequence — labels are prose, not verbs):")
    for label, recipe in _CHAIN_RECIPES:
        print(f"  # {label:<20} {recipe}")
    print("\nSIBLING COMMANDS (own console scripts, installed with `pip install -e .`):")
    print("  echelon-mcp     stdio MCP server for clients that can't shell out (LM Studio, Claude Desktop)")
    print("  echelon-server  the web/API server (the studio + services backend)")
    print("  echelon-chat    interactive terminal session (same as `echelon chat`)")
    print("  claude-deep     Claude Code harness on the DeepSeek proxy (cheap worker channel)")
    print("  claude-gem      Claude Code harness on the Gemini proxy (vision/UX worker channel)")
    print(f"\n  # every verb carries its own flags:  echelon <verb> --help")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # --version / -V: print the installed version and exit (before verb dispatch).
    if argv and argv[0] in ("--version", "-V"):
        try:
            from importlib.metadata import version
            print(f"echelon {version('echelon')}")
        except Exception:
            print("echelon (version unknown — package not installed)")
        return 0

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_front_page()
        return 0
    if argv[0] == "verbs":
        _print_help()
        return 0
    if argv[0] == "doctor":
        from .atoms.doctor import main as doctor_main
        return doctor_main(argv[1:])
    verb, rest = argv[0], argv[1:]
    if verb in _AGENT_VERBS:
        from .agent.cli import main as agent_main
        # `run` is the trunk: argv is verb-stripped so argparse sees clean flags.
        # `dispatch` is caught by name inside agent.cli before argparse is built.
        # `workflow` is NOT a subcommand — it is the flag `--workflow FILE`. Passing the
        # bare word through made argparse reject it ("unrecognized arguments: workflow"),
        # so the verb was UNINVOKABLE. Translate it to the flag form that actually works.
        if verb == "run":
            return agent_main(rest)
        if verb == "workflow":
            # `workflow --help` must show the agent CLI's flags (where --workflow lives), NOT be
            # translated to `--workflow --help` — that makes argparse consume --help as the FILE
            # argument and error ("expected one argument"), leaving the verb UNINVOKABLE for help
            # (OPEN-0033). Route a bare help request straight to the agent parser's own help.
            if not rest or rest[0] in ("-h", "--help"):
                return agent_main(["--help"])
            return agent_main(["--workflow", *rest])
        return agent_main([verb, *rest])
    if verb in _AGENT_WORLD_VERBS:
        sys.argv = [verb, *rest]
        return _argv_direct(_AGENT_WORLD_VERBS[verb])
    routes = _route()
    fn = routes.get(verb)
    if fn is None:
        print(f"echelon: unknown verb {verb!r}. run `python -m echelon_engine` for the list.", file=sys.stderr)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
