> **Inherited note (v0.1.0 public).** This document was carried over from the private engine and only partly re-verified for the public cut. The `apps/` layer it describes (web command center, Flux UI) is not in this release, and test counts it cites are stale; README.md is the accurate entry point.

# ECHELON Architecture

## What ECHELON is

ECHELON is a **memory + reasoning substrate**. Four claims, each settled by the owner and seen to work:

1. **A memory is a WEIGHT-ADJUSTOR, not a record.** It stores the SEED that re-creates an understanding when you read it; recall RE-SHAPES the model, it doesn't just inform it. One lesson per file (an ATOM).
2. **Recall is FOVEATED VISION.** The warmth BANK is an eye: atoms matching your current intent come into sharp focus, the rest stay dormant-but-active in the periphery. Query by intent; warm = known ground that paid off, cold = new ground.
3. **Significance is EARNED BY TRACE, never asserted.** An atom is born neutral; it gains weight only when a CARD (an ordered sequence of atoms that composes into a procedure) that USED it SUCCEEDS — credit flows back to its atoms. Nothing grades its own homework.
4. **The core is a CARTRIDGE.** Atom=parameter, card=transformer, warmth=attention, trace=training — a swappable earned core you PLUG IN to BE a capability, loading the bake without re-baking.

## The Three-Layer Design

```
apps/              ← Transport: CLI, web server, world loops
  ↓  may ONLY import echelon_engine.services (the GATE)
echelon_engine/    ← ENGINE: atoms, providers, cards, agent loop, MCP server
  ↓  may ONLY import echelon_sdk
echelon_sdk/       ← LIBRARY: pure primitives, stdlib only, zero upward imports
```

The dependency law is **enforced at import time** by `echelon_engine/_scanner.py`. On `import echelon_engine`, an AST-based static analysis runs and checks:
- No module imports upward (sdk → engine, atoms → boards, apps → atoms)
- Private `services_*.py` modules are only imported from within the engine
- `ChainResult` chains have valid shape (start with `.of()`, straight-line, unique step names, `.on()` before `.pipe()`)

A violation is an **ImportError**, not a lint warning. The law is a boot gate, not a suggestion.

### echelon_sdk — Library

Pure primitives with no import-time I/O, no dependencies beyond stdlib. The dependency root.

Key modules: `chain.py` (ChainResult pipeline primitive), `config.py` (registry-driven config), `keys.py` (API key loader), `paths.py` (canonical substrate paths), `roles.py` (role taxonomy), `levels.py` (three-level memory ladder), `compiler/` (intent pool, AST-safe writer).

### echelon_engine — Engine

The substrate machinery. Divided into sub-packages:

- **atoms/** — Leaf I/O: store, bank, warmth, cards, recall, ingest, wrap, dream, immune (scan/heal), correct (dispute/disclaim/redeem), cartridge, brainstorm, vision, status, providers, backup, providers (Grok/DeepSeek/Gemini/Local/Bridge)
- **agent/** — Orchestration spine: CLI loop, partner (dispatch/swarm/board), workflow (DAG scheduler), fork_field (warmth-clocked scheduler), web command center (FastAPI server, status cards, society view, atlas panel)
- **agent/world/** — Autonomous loops: autoloop (flat), recurloop (recursive tiered), relayloop (fresh-worker relay), worldjournal (observable journal+nudge), worldview (journal observer)
- **swarm/** — Pluggable swarm types (read: analyze→report; execute: managed work via shared ledger). Types own their schema — plan/council/skeptic/audit/board-plan (read) + board (execute, requires `--design`). User-registerable via `~/.echelon/swarms.json`.
- **Root files**: `services.py` (THE GATE — the one public surface), `_scanner.py` (architecture law), `__main__.py` (unified CLI dispatcher, 67+ verbs in 9 domain groups), `mcp_server.py` (stdio JSON-RPC MCP server), `proxy.py` + `proxy_gate.py` (provider-agnostic API proxy with intent-gate for non-Claude models)

### apps — Transport

Thin shells over the engine's CLI entry points. Currently stubs (`cli/`, `web/`).

## The Data Model

### Atom

The fundamental unit of memory. Content-addressed, scoped, scored by use.

- **Spine**: name (kebab slug), description (one dense line — the hook recall ranks on), type (user/feedback/project/reference), scope
- **Body**: the single lesson, self-contained (restates parent context — script/DB/date, dates absolute)
- **Sidecar**: earned trajectory (fetch history, score history), live edges (typed relationships to other atoms)
- **Earned**: effective score = atom's own earned weight (v2-native, no v1 borrow since 2026-06-26)

### Card

An ordered sequence of atoms that composes into a **procedure**. Cards chain atoms in firing order. When a card succeeds, credit flows back to its atoms (earn-by-trace). Cards are content-addressed — same sequence = same card.

### Edge vocabulary

| Edge type | Meaning |
|---|---|
| `refs` | General reference between atoms |
| `supersedes` | New atom replaces an older one |
| `refines` | Atom sharpens/extends another |
| `part_of` | Atom belongs to a larger whole |
| `depends_on` | Atom requires another to be valid |
| `contradicts` | Atom disagrees with another |

### Scope

A walled namespace for atoms. Each project/domain gets its own scope (`echelon`, `my-app`, `research`, etc.). Scopes can be grouped (`group-scope`), and atoms can cross-reference across scopes.

## Earn-by-Trace Philosophy

Atoms are born with a soul score of 100.0 (flat). They gain **earned weight** only through witnessed use:

1. **Fetch through the witnessed door** — `remember <slug>` records the fetch, adds earned score. A file-read (`cat memory/*.md`) is out-of-band and earns nothing.
2. **Card success** — when a card (procedure) succeeds, credit flows back to every atom it used.
3. **fire_lower** is the ONLY downward hand-path — weight decays but atoms are never deleted. Stale atoms are disputed/disclaimed, not removed.
4. **Effective score** (v2-native since 2026-06-26): the atom's own earned score. The v1 cold-borrow bridge (borrowing legacy `core.db` scores for un-earned atoms) was removed — v2 is now weight-richer on its own.

A low-scored atom is DORMANT, not gone. Query for it by intent; it will surface if relevant.

## The CLI Surface — 67 verbs in 9 domains

Run `python -m echelon_engine` (no args) for grouped help. Verbs organized by what you want to DO:

| Domain | Verbs | Purpose |
|---|---|---|
| **SETUP** | init, setup, install-hooks, register-mcp | First-time installation & integration |
| **BANK** | recall, remember, inspect, ingest, check, reflex, clone-cartridges, mcp-users, group-scope, claim | Read & write memory |
| **INTEGRITY** | hygiene, scan, heal, dispute, disclaim, redeem | Immune system & correction |
| **SESSION** | relive, wrap, sleep, dream, warmth | Work cycle |
| **OBSERVER** | status, selftest, config, graph, gate, handoff, resolve-scope, providers, backup | Introspection (all read-only, $0) |
| **AGENT** | xray, blueprint, skeleton, build-page, dispatch, run, workflow, summon | Equipped execution (budget-gated, costs money) |
| **AUTONOMOUS** | autoloop, recurloop, relayloop, worldjournal, worldview | Self-driving loops (budget & turn-cap guarded) |
| **SPECIALIZED** | cartridge, brainstorm, scribe, vision, imagine, swarm, swarm-subject, bus, watcher, society, studio, atomize-digest, batch-reclaim, compact, session-state, atlas, pagemodel, uispec | One-shot tools |
| **INFRASTRUCTURE** | proxy, chat | API proxy + interactive terminal |

See `docs/VERBS.md` for per-verb details. Regenerate via `python scripts/gen_verb_table.py`.

**Natural chains**: `check → ingest → recall → remember` (atom lifecycle), `scan → heal` (immune), `recall → [work] → wrap → sleep` (session), `cartridge list → cartridge equip` (capability).

## The Cartridge System

The root dream made operational. A cartridge is a **scope of earned atoms** you plug in to BE a capability.

**The formula**: atom = parameter, card = transformer, warmth = attention, trace = training.

**The registry**: `echelon_engine/atoms/cartridge_registry.py` — a declarative `CartridgeSpec` catalog. Adding a cartridge = adding a spec to the registry, not editing logic. The gate auto-generates the live registry into every MEMORY.md banner.

**The verbs**:
- `cartridge list` — survey all cartridges (scopes with composed cards float to top)
- `cartridge equip <name> "<goal>"` — wake holding that cartridge's warm atoms foveated on the goal
- `cartridge compose <name>` — (re)build a cartridge's card from the registry

**Born cartridges** (25 built-in, more user-registerable): `ux`, `ux-pipeline`, `architect`, `engagement`, `delivery`, `pm`, `qa`, `ops`, `brainstorm`, `scribe`, `act-ready`, `yagni`, `swarm`, `software-house`, `partner`, `ui-audit`, `mol-boot-procedures`, `opt`, `intent`, `security`, `review`, `refactor`, `slim`, `debug`, `migrate`. `interface-build` merged into `frontend-build` (2026-07-31) as a Template-Sourcing Entry Path; `cartridge equip interface-build` aliases transparently. Run `echelon cartridge list` for the live roster including user-registered cartridges.

Equipping follows the card's refs ACROSS scopes (a capability composes across scopes, not just one) — each atom tagged with its home scope, each witnessed through the door.

## The Gate

`echelon_engine/atoms/gate.py` emits the ECHELON gate — a provider-agnostic banner prepended to every project's MEMORY.md. It contains: the four claims, the three entry doors (existing user / new project / migrating project), the live cartridge registry, and the REFLEX/THINK routing law.

The gate is **machine-managed**: regenerate with `python -X utf8 -m echelon_engine gate --write <file>`. It replaces ONLY the banner above the first `---`; everything below is the project's own content, preserved verbatim.

## Memory Lifecycle

```
Author  →  Plant  →  Recall  →  Dispute  →  Dream  →  Wrap
(write    (ingest   (warmth-   (content    (consoli-  (session
 .md)     into      ranked     antibody)   dation)    close-out)
           bank)     fetch)
```

1. **Author** — Write `.md` atoms to `memory/`. One lesson per file. Frontmatter: name, description, type.
2. **Plant** — `ingest` compiles frontmatter into the bank, links edges, validates template.
3. **Recall** — `recall --warm "<intent>"` retrieves through the witnessed door. `--warm` repeatable for batch queries. Fetch earns weight. Warm = reflex (re-tread proven path), cold = think (generate new move, crystallize back).
4. **Immune (structure)** — `scan` reports broken edges; `heal` tombstones them.
5. **Correct (content)** — `dispute` (atom is wrong/stale), `disclaim` (reset score), `redeem` (restore with trace evidence).
6. **Dream** — Consolidation pass: good atoms rise in score, stale ones decay.
7. **Wrap** — Session close-out: offer (episode summary), decide (decision record), card (compose arc-card), evidence (link artifacts), scan (post-wrap immune check).

## Agent Substrate

The agent loop (`echelon_engine/agent/`) is a provider-agnostic act→observe→repeat spine:

- **dispatch** — Single partner on a goal (equip cartridge → run → verify → earn)
- **swarm** — N parallel partners on independent goals (fan-out over leaf units)
- **board** — N partners deliberating on shared goals (shared ledger)
- **workflow** — Multi-step DAG scheduler with wave-based execution
- **fork_field** — Warmth-clocked scheduler for autonomous continuous operation

### Autonomous loops

Five self-driving loop shapes in `echelon_engine/agent/world/`:

| Loop | Shape |
|---|---|
| `autoloop` | Flat autonomous — bank-proposed goals, one dispatch per turn |
| `recurloop` | Recursive tiered — big goals spawn sub-goals via shared ledger board |
| `relayloop` | Fresh-worker relay — one goal across N fresh workers (wrap-relive-continue) |
| `worldjournal` | Observable — autoloop + append-only journal + steerable nudge file |
| `worldview` | Observer — tail, card, html, serve, nudge a running journal |

The agent CLI (`echelon_engine/agent/cli.py`) wires providers, tools, memory, boot, judge, vision, consult, swarm, and partner seams into one entry point.

## MCP Server

`echelon_engine/mcp_server.py` implements the MCP protocol (stdio JSON-RPC) without any SDK dependency. Exposes 24 tools across 4 groups (memory, capability, curation, maintenance) — see `mcp/README.md` for the full tool table and setup instructions. Each tool call shells out to the `echelon` CLI as a subprocess, giving fast startup and per-call isolation. The recall-first hook injects substrate context into the model's system prompt.
