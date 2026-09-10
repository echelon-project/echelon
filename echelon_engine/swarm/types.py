"""types — the swarm TYPE registry: plan, council, skeptic, board, and user-defined.

Like cartridges, swarm types are two-tier:
  - Built-in: defined here in the installed package
  - User: registered via `echelon swarm register` → ~/.echelon/swarms.json

A SwarmType spec defines:
  - name: the --type flag value (e.g. 'plan', 'council', 'board-plan')
  - kind: 'read' (analyze → report) | 'execute' (managed work via shared ledger)
  - mode: 'plan' (fan-out lenses), 'council' (multi-seat deliberation), 'solo' (single strong model)
  - lenses/seats: the parallel workers for plan/council modes (read-kind only)
  - cartridge: default cartridge to equip
  - description: one-line what it does

User-registered types may only be kind=read (execute types bind code, built-in only).

Usage:
  echelon swarm --type plan "goal"
  echelon swarm --type board-plan "goal"    # draws BOARD_DESIGN.json
  echelon swarm --type board --design BOARD_DESIGN.json --folder <repo>  # executes
  echelon swarm types                       # list available
  echelon swarm register <name> ...         # register user type
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# User swarm type registry
_USER_SWARMS_PATH = Path.home() / ".echelon" / "swarms.json"


@dataclass
class SwarmType:
    """One swarm type. A swarm type owns its kind (read|execute|author) and dispatch shape.

    kind:
      - 'read': analyze → produce a report. Mode determines worker shape:
          - 'plan': fan-out over lenses (parallel, different prompts per lens)
          - 'council': multi-seat deliberation (parallel, different roles per seat)
          - 'solo': single strong model with a specific frame
      - 'execute': do MANAGED WORK through the shared ledger (built-in only, no mode).
      - 'author': PRODUCE A LARGE ARTIFACT by driving the agent loop step by step.
          The key insight: the model output ceiling (~8k for gemini) is an AGENT
          problem, not a routing one — an agent doesn't emit one giant artifact, it
          takes many small bounded ACTIONS in a loop. So an author type decomposes the
          artifact into ordered SECTIONS and authors them one bounded step at a time
          (each step writes/edits one region, under the ceiling), then a gate_model
          reviews and a verify pass confirms the artifact actually changed.
          Built-in only (binds the author runner). See author.py.
    """
    name: str                       # the --type value (kebab-case)
    kind: str = "read"              # 'read' | 'execute' | 'author'
    mode: str = "plan"              # 'plan' | 'council' | 'solo' (read-kind only)
    description: str = ""           # one-line what it does
    lenses: list[dict] = field(default_factory=list)   # [{name, prompt}] for plan mode
    seats: list[dict] = field(default_factory=list)    # [{name, role, prompt}] for council
    frame: str = ""                 # solo mode: the system prompt / instruction frame
    cartridge: str = "architect"    # default cartridge to equip
    model: str = ""                 # model override ('' = provider default)
    source: str = "builtin"         # 'builtin' | 'user'
    # ── author-kind fields (only meaningful when kind='author') ──────────────
    tier: str = ""                  # author tier: 'gem' (gemini Pro) | 'deep' (deepseek) | '' = provider default
    author_model: str = ""          # explicit model for the author loop ('' = tier default)
    gate_model: str = ""            # model that reviews each authored section ('' = same as author)
    decompose: list[dict] = field(default_factory=list)  # [{name, instruction}] ordered sections to author
    artifact: str = ""              # default output artifact path (overridable via --output)


# ── BUILT-IN SWARM TYPES ──────────────────────────────────────────────────────

_BUILTIN: dict[str, SwarmType] = {}


def _register_builtin(st: SwarmType) -> SwarmType:
    st.source = "builtin"
    _BUILTIN[st.name] = st
    return st


# plan — fan-out goal analysis over 6 lenses
_register_builtin(SwarmType(
    name="plan",
    mode="plan",
    description="Fan-out goal analysis over multiple lenses (architecture, security, performance, UX, ops, cost)",
    cartridge="architect",
    lenses=[
        {"name": "architect", "prompt": "Analyze from an ARCHITECTURE perspective: system design, component decomposition, scaling strategy, integration points, architectural risks."},
        {"name": "security", "prompt": "Analyze from a SECURITY perspective: threat surface, auth, data sensitivity, injection risks, secrets management, compliance."},
        {"name": "performance", "prompt": "Analyze from a PERFORMANCE perspective: bottlenecks, caching, latency budgets, throughput limits, cold-start costs."},
        {"name": "ux", "prompt": "Analyze from a UX perspective: consumer needs, state machine (loading/empty/error/edge), information hierarchy, minimum viable surface."},
        {"name": "ops", "prompt": "Analyze from an OPS perspective: deployability, observability, failure modes, rollback, config management, runbook requirements."},
        {"name": "cost", "prompt": "Analyze from a COST perspective: token pricing, infrastructure, development time, maintenance burden, optimization opportunities."},
    ],
))

# council — multi-seat deliberation
_register_builtin(SwarmType(
    name="council",
    mode="council",
    description="Multi-seat deliberation: options, defect-finding, honesty-check, decision synthesis",
    cartridge="brainstorm",
    seats=[
        {"name": "options", "role": "options-generator",
         "prompt": "Generate 3-5 ALTERNATIVE approaches. For each: what's different, trade-offs, when it's better."},
        {"name": "defect", "role": "defect-finder",
         "prompt": "Find EVERY defect, bug-prone assumption, and logic gap. Be ruthless."},
        {"name": "honesty", "role": "honesty-checker",
         "prompt": "Check every CLAIM for honesty. Flag unverified claims, guessed numbers, overconfidence."},
        {"name": "decision", "role": "decision-synthesizer",
         "prompt": "Synthesize ALL findings into ONE prioritized action list. Rank by urgency."},
    ],
))

# skeptic — red-team review
_register_builtin(SwarmType(
    name="skeptic",
    mode="solo",
    description="Red-team review: find hidden assumptions, missing edge cases, failure cascades, over-engineering",
    cartridge="brainstorm",
    frame="""You are a RED-TEAM SKEPTIC. Break this plan. Find:
1. HIDDEN ASSUMPTIONS — what isn't stated? What if false?
2. MISSING EDGE CASES — scale, bad data, partial failure
3. RESOURCE GAPS — skills, time, money, API limits
4. FAILURE CASCADES — blast radius if one part fails
5. OVER-ENGINEERING — what could be simpler/cheaper/deleted?
6. WRONG ABSTRACTIONS — wrong boundaries, wrong ownership
Be specific — give concrete counter-examples, not vague warnings.""",
))

# audit — codebase audit over multiple lenses
_register_builtin(SwarmType(
    name="audit",
    mode="plan",
    description="Codebase audit: bugs, security, style, architecture, duplication, dead code — all in parallel",
    cartridge="architect",
    lenses=[
        {"name": "bugs", "prompt": "Find BUGS: logic errors, off-by-one, null handling, race conditions, incorrect assumptions. Be specific — file:line."},
        {"name": "security", "prompt": "Find SECURITY issues: injection, broken auth, exposed secrets, unsafe deserialization, missing validation."},
        {"name": "style", "prompt": "Find STYLE/IDIOM issues: naming, consistency, complexity, God functions, missing error handling, Python anti-patterns."},
        {"name": "architecture", "prompt": "Find ARCHITECTURE issues: wrong abstractions, circular deps, missing layers, leaky boundaries, SRP violations."},
        {"name": "duplication", "prompt": "Find DUPLICATION: copied code, near-duplicates, missing shared utilities, DRY violations. Quote the duplicated blocks."},
        {"name": "dead-code", "prompt": "Find DEAD CODE: unreachable paths, unused functions, obsolete branches, imports never used, stale comments."},
    ],
))

# ux — the UX/UI design swarm (the ultraplan equivalent for INTERFACES, not code). Where `plan`/`audit`
# reason about systems, `ux` reasons about the human surface, following the REAL design order: you cannot
# judge cosmetics before the flow, nor the flow before the job-to-be-done. So the lenses are LAYERED
# top-of-funnel → polish (JTBD → IA → content → flow → states → visual → a11y → consumer verdict), each a
# focused seat. This encodes the bank's hard-won UX lessons: design for the CONSUMER not the agent
# (ui-for-consumer-not-for-agent), and judge across LIFECYCLE STATES not one idle frame
# (ux-society-multi-seat-multi-state — the false-pass killer). Cartridge: ux (the gemini UX-author tier).
_register_builtin(SwarmType(
    name="ux",
    mode="plan",
    description="UX/UI design swarm: JTBD → information architecture → content → flow → state machine → visual craft → accessibility → consumer verdict (the design order, layered)",
    cartridge="ux",
    lenses=[
        {"name": "jtbd", "prompt": "JOB-TO-BE-DONE. Who is this screen/app FOR (the real human, not an operator), and what are they trying to ACCOMPLISH, in what CONTEXT (device, urgency, expertise)? State the primary job in one sentence and the 2-3 secondary jobs. Everything downstream serves THIS — design for the consumer, NOT for an agent/operator. Name who would be confused and why."},
        {"name": "ia", "prompt": "INFORMATION ARCHITECTURE. What PAGES/SCREENS must exist to do the job (the page inventory)? How are they organized and named? What is the NAVIGATION model (how you move between them, what's always reachable, the back/home/escape paths)? Draw the screen map. Flag any page that's missing or any that shouldn't exist."},
        {"name": "content", "prompt": "CONTENT & HIERARCHY per screen. What is ON each page — every element, in priority order (primary action, supporting info, secondary, chrome)? What is the READING ORDER and the single most important thing the eye should hit first? Critique the COPY: is it plain human language or jargon/operator-speak? What can be CUT?"},
        {"name": "flow", "prompt": "FLOW & INTERACTION. Trace the user JOURNEY step by step across screens to complete the primary job. At each step: what action, what affordance signals it, what happens on click, where it leads. Count the steps — where is it too many? What are the decision points, the dead ends, the places a user gets stuck or has to guess?"},
        {"name": "states", "prompt": "STATE MACHINE per screen — the false-pass killer. For EVERY screen enumerate ALL states: initial/loading, EMPTY (no data yet), PARTIAL, ERROR (and which errors — network, validation, permission, not-found), SUCCESS, and EDGE (huge data, tiny data, slow, offline). For each state: what does the user SEE and what can they DO? Most designs only specify the happy idle frame — name every state that's undefined."},
        {"name": "visual", "prompt": "VISUAL CRAFT (cosmetics — judged LAST, because polish on a wrong flow is wasted). Assess hierarchy (does size/weight/color match importance?), spacing & rhythm, typography (scale, legibility), color (intent, contrast, restraint), and CONSISTENCY across screens (do the same things look the same?). Flag where the cosmetics fight the hierarchy or feel unfinished/amateur."},
        {"name": "a11y", "prompt": "ACCESSIBILITY & RESPONSIVE — the floor a real product must clear. Keyboard reachability and focus order, color contrast (WCAG AA), screen-reader labels/roles, hit-target sizes, motion/animation safety, and RESPONSIVE behavior (what breaks at mobile/tablet/wide). Name concrete failures, not 'should be accessible'."},
        {"name": "consumer", "prompt": "CONSUMER VERDICT — read the WHOLE design as the actual non-expert human user (the judge-chair seat). Walk in cold: would I understand what this is, find what I need, complete the job, and recover from a mistake — WITHOUT being told? Where would a real person hesitate, misread, or rage-quit? Give the blunt verdict: does this serve a consumer, or is it a UI for an agent?"},
    ],
))

# polish — the COSMETIC CRAFT swarm (born 2026-07-10 from the owner's complaint on the AlphaApp
# estate: "non styled input, tight form and too many field, flatten of many component in a single
# window — embarrassing me in front of client"). Where `ux` designs the whole surface from the
# job down, `polish` assumes the flow is SETTLED and attacks only the craft layer — the tier that
# makes a working product look finished. Every lens must end in a copy-ready PRESCRIPTION
# (tokens/markup/CSS patterns), not just findings: the redesign IS the deliverable.
#
# BUILD/RENDER doctrine earned on the first run (2026-07-10, AlphaApp OS aurora restructure):
#  - claude-deep hardcodes proxy port 18788: N cold-starting workers RACE and kill each other
#    ("ConnectionRefused"). Once a proxy is live, later workers "reuse" it and run concurrently.
#    So fan out from cold by SERIALIZING into per-page git worktrees (one → commit → next); the
#    parallelism is across worktree COMMITS gathered by ONE render-gate sweep, not concurrent cold starts.
#  - Trigger-spawned UI (drawers/sheets/modals/popovers) is INVISIBLE to a page-level pass — it only
#    exists after a click with real data. Polish it as COMPONENTS: extract markup + render fn, inject a
#    prod-shaped mock dataset (reuse existing fixtures, shape the gaps), render headless to PNG, skin,
#    re-render. Pure render needs NO proxy — those fan out freely, each with its own mock data.
#  - Gate by EVIDENCE not the worker's verdict labels; render before ship (0 pageerrors + selectors in DOM).
#  Sibling: the ~/.claude/skills/polish-and-gate skill carries the full operator loop.
_register_builtin(SwarmType(
    name="polish",
    mode="plan",
    description="Cosmetic craft swarm: forms & inputs → density & disclosure → token/component discipline → component finish → client-demo verdict. For 'it works but looks embarrassing' — each lens prescribes the corrected pattern, copy-ready.",
    cartridge="frontend-design",
    lenses=[
        {"name": "forms", "prompt": "FORMS & INPUTS. Inventory EVERY input/select/textarea: styled by the design system or a raw browser box? Labels present, aligned, associated? Width rhythm (fields sized to their content)? Then the FIELD COUNT per form: which fields can be CUT (derivable), DEFAULTED (pre-filled), DEFERRED (progressive disclosure), or STAGED (wizard step)? Tight forms: prescribe the padding/gap/row-height scale. Deliver the ONE form pattern (markup + CSS) every form must adopt."},
        {"name": "density", "prompt": "DENSITY & DISCLOSURE — many components flattened into a single window. Per screen: list every component in the main viewport, mark each KEEP-VISIBLE (earns the first screen — serves the primary job) / DEMOTE (collapse, tab, accordion, drawer, secondary page) / KILL (serves nobody). Prescribe the disclosure mechanic per demotion and the resulting first-screen composition. The test: one primary surface + one action rail per screen, everything else behind one click."},
        {"name": "system", "prompt": "TOKEN & COMPONENT DISCIPLINE. One type scale (prescribe the steps), one spacing scale (8px grid), one radius/shadow set, one badge/pill/button/drawer/empty-state system. Inventory every violation: ad-hoc font sizes, off-scale inline paddings, per-page duplicate component implementations, divergent chrome, hardcoded color fallbacks. Deliver the corrected :root token block + the migration table (violation → token)."},
        {"name": "chrome", "prompt": "COMPONENT FINISH — the pixel-level amateur tells. Tables (header weight, row height, numeric alignment, truncation), cards (padding symmetry, title/body rhythm), buttons (size variants, icon alignment), badges/status pills (contrast, casing), empty/loading states (styled or bare text?), scrollbars, focus rings, dark-mode parity. Name each unfinished detail with its location and the exact fix."},
        {"name": "client-eye", "prompt": "THE CLIENT DEMO VERDICT. Walk every screen as a skeptical CLIENT watching the owner demo this product. Rank screens most-embarrassing first, name the 5 single worst moments (the screen a client would screenshot and mock), and for each what a polished competitor shows instead. Blunt verdict per screen: demo-ready / needs-work / hide-from-clients. End with the shortest path to demo-ready."},
    ],
))

# board — execute-kind: loads a BOARD_DESIGN.json task graph and runs the REAL board() coordinator
_register_builtin(SwarmType(
    name="board",
    kind="execute",
    mode="",
    description="Execute a task graph through the real board() coordinator. Requires --design BOARD_DESIGN.json. Draw the design first with board-plan.",
    cartridge="pm",
))

# board-plan — read-kind: draws a reviewable task graph (BOARD_DESIGN.json) via council deliberation
_register_builtin(SwarmType(
    name="board-plan",
    kind="read",
    mode="council",
    description="Draw a board task graph: decompose → sequence → risk-assess → decide, then emit BOARD_DESIGN.json + report",
    cartridge="pm",
    seats=[
        {"name": "decompose", "role": "task-decomposer",
         "prompt": "Decompose the goal into concrete tasks. Each task: what, who (which cartridge/role), estimated effort, acceptance criteria."},
        {"name": "sequence", "role": "dependency-sequencer",
         "prompt": "Sequence tasks by dependency. Find the critical path. Which tasks can run in parallel?"},
        {"name": "risk", "role": "risk-assessor",
         "prompt": "For each task: what could go wrong? Likelihood, impact, mitigation. Which tasks are highest-risk?"},
        {"name": "decide", "role": "decision-synthesizer",
         "prompt": "Synthesize into a board-ready plan: prioritized task list with owners, estimates, and hand-off contracts."},
    ],
))

# features — feature discovery and gap analysis
_register_builtin(SwarmType(
    name="features",
    mode="plan",
    description="Feature discovery: find gaps, opportunities, missing capabilities, competitive differentiators",
    cartridge="pm",
    lenses=[
        {"name": "gaps", "prompt": "Find GAPS: what's missing that users need? What do competitors have that we don't? What's on the backlog too long?"},
        {"name": "opportunities", "prompt": "Find OPPORTUNITIES: what adjacent problems could we solve? What would 10x the product? What's the smallest high-impact feature?"},
        {"name": "ux-debt", "prompt": "Find UX DEBT: what frustrates users? What takes too many clicks? What's confusing? What state is missing (loading/empty/error)?"},
        {"name": "tech-debt", "prompt": "Find TECH DEBT blocking features: what's hard to change? What's untested? What's poorly abstracted?"},
    ],
))

# marriage — integration compatibility analysis
_register_builtin(SwarmType(
    name="marriage",
    mode="plan",
    description="Integration analysis: compatibility, contract mismatches, data shape gaps, auth boundaries between systems",
    cartridge="ops",
    lenses=[
        {"name": "contracts", "prompt": "Map the API/data CONTRACTS between the systems. What fields, types, formats? Where do they mismatch?"},
        {"name": "auth", "prompt": "Map the AUTH boundary. How does identity flow between systems? Tokens, sessions, scopes, expiry?"},
        {"name": "failure", "prompt": "What happens when ONE system is down? Timeouts, retries, circuit breakers, degraded mode?"},
        {"name": "data", "prompt": "DATA SHAPE analysis: schemas, migration, consistency, duplicates, conflict resolution. What's the source of truth?"},
    ],
))

# deprecate — deprecation impact analysis
_register_builtin(SwarmType(
    name="deprecate",
    mode="plan",
    description="Deprecation impact: what depends on it, migration path, communication plan, rollback strategy",
    cartridge="architect",
    lenses=[
        {"name": "dependents", "prompt": "Map EVERYTHING that depends on this. Importers, callers, configs, docs, tests, CI, dashboards."},
        {"name": "migration", "prompt": "Design the MIGRATION PATH. Step-by-step. What's the replacement? How do callers transition? Feature flags?"},
        {"name": "risk", "prompt": "What BREAKS if this is removed today? What breaks during migration? What's the rollback plan?"},
        {"name": "communication", "prompt": "COMMUNICATION plan: who needs to know? Timeline, deprecation warnings, docs updates, changelog."},
    ],
))

# refactor — refactoring plan
_register_builtin(SwarmType(
    name="refactor",
    mode="plan",
    description="Refactoring plan: what to change, in what order, how to verify no regressions, risk assessment",
    cartridge="architect",
    lenses=[
        {"name": "target", "prompt": "Define the TARGET architecture. What does 'done' look like? What's the new structure?"},
        {"name": "steps", "prompt": "Decompose into SEQUENTIAL STEPS. Each step must leave the system working. What's the critical path?"},
        {"name": "verify", "prompt": "How do we VERIFY each step? Tests to run, metrics to check, behavior to preserve. Regression detection."},
        {"name": "risk", "prompt": "RISK assessment per step. What's most likely to break? Rollback per step? Which step is highest-risk?"},
    ],
))


# ── AUTHOR-KIND SWARM TYPES ───────────────────────────────────────────────────
# Author types PRODUCE a large artifact by driving the agent loop step by step (one
# bounded section per step, under the model output ceiling), then a gate_model reviews
# and a verify pass confirms the file changed. The decompose list is the SECTION RECIPE.

# ux-build — author a real UI artifact stepwise (the proven Pro-authors-code-stepwise
# pipeline, made a first-class swarm type). Distinct from the read-kind `ux` DESIGN swarm:
# `ux` reasons about the surface (produces a report); `ux-build` BUILDS the surface (produces HTML).
_register_builtin(SwarmType(
    name="ux-build",
    kind="author",
    description="Author a UI artifact (HTML/CSS) section by section — gemini Pro drives the loop, building one region per bounded step under the output ceiling",
    cartridge="ux",
    tier="gem",
    gate_model="",
    artifact="UI_BUILD.html",
    decompose=[
        {"name": "skeleton", "instruction": "Author the HTML SKELETON ONLY: doctype, head (title, meta, a single <style> block with CSS custom properties / design tokens for color, spacing, type scale), and an empty <body> with the top-level layout containers (header, main, footer / sidebar) as empty divs with semantic classes. NO content yet, NO component CSS yet — just the structural frame and the token palette. Keep it small."},
        {"name": "layout-css", "instruction": "Add the LAYOUT CSS into the existing <style> block: the grid/flex rules that position the top-level containers from the skeleton (header/sidebar/main/footer). Use the design tokens already defined. Do NOT add component-level styling yet. Edit only the <style> block."},
        {"name": "header", "instruction": "Author the HEADER content + its scoped CSS: brand/logo, primary nav, any global actions. Fill the header container from the skeleton and add the header's CSS rules to the <style> block. One region, bounded."},
        {"name": "main", "instruction": "Author the MAIN content region + its CSS: the primary screen content (cards/panels/lists/forms as the design calls for), filling the main container. Add the component CSS to the <style> block. This is the heart — keep it to this one region, do not touch header/footer."},
        {"name": "states", "instruction": "Add the STATE styling and any state markup the design needs: loading/empty/error/disabled/hover/focus visual states for the components you authored. Edit the <style> block and add minimal state-demonstrating markup only where the design requires it. The false-pass killer — do not ship a happy-idle-only frame."},
        {"name": "polish", "instruction": "Final POLISH pass: tighten spacing rhythm, typographic scale, color contrast (WCAG AA), consistency across components, and responsive behavior (one mobile breakpoint). Edit the <style> block. Do NOT restructure — only refine. Then state the artifact is complete."},
    ],
))

# scribe — author accurate documentation by reading code then writing stepwise (deep tier).
# Folds the old atoms/scribe.py intent into the universal author door: read → outline → write
# section by section, each section verified against the real code (no fabrication).
_register_builtin(SwarmType(
    name="scribe",
    kind="author",
    description="Author accurate docs from code: read the target, outline, then write section by section — each grounded in real definitions, never fabricated (deep tier)",
    cartridge="scribe",
    tier="deep",
    artifact="DOCS.md",
    decompose=[
        {"name": "survey", "instruction": "READ the target code/folder. Identify the public surface: modules, key classes/functions, entry points, data shapes. Do NOT write the doc yet — produce a precise OUTLINE of the sections the doc needs (overview, install/setup, the core concepts, the API surface, examples). Ground every section in a file you actually read."},
        {"name": "overview", "instruction": "Author the OVERVIEW section of the doc artifact: what this is, what problem it solves, the mental model. Quote real entry points (file:symbol). Write only this section into the artifact."},
        {"name": "concepts", "instruction": "Author the CORE CONCEPTS section: the load-bearing abstractions, how they relate, the data flow. Every claim must trace to a real definition you read — cite file:line for non-obvious ones. Append to the artifact."},
        {"name": "api", "instruction": "Author the API / USAGE section: the public functions/commands with their real signatures (read them, do not guess), parameters, return shapes, and a correct example for each. Append to the artifact. Never invent a parameter that isn't in the code."},
        {"name": "verify", "instruction": "VERIFY pass: re-read the artifact against the code. Fix any claim that doesn't match a real definition (the dead-claim trap). Confirm every cited file:symbol exists. State the doc is accurate and complete."},
    ],
))

# code — author a code change as a small-edit loop (deep tier). The general "build a feature
# / make a change" author type: plan the minimal edit set, then make small bounded edits one
# region at a time, then verify it runs.
_register_builtin(SwarmType(
    name="code",
    kind="author",
    description="Author a code change as a small-edit loop: plan minimal edits → make one bounded edit per step → verify it runs (deep tier)",
    cartridge="yagni",
    tier="deep",
    artifact="",
    decompose=[
        {"name": "plan", "instruction": "Read the relevant code. Climb the YAGNI ladder first (is the change even needed; can existing code do it). Then produce the MINIMAL edit plan: the smallest set of bounded edits, each one file-region, that achieves the goal. Do NOT write code yet — list the edits in order."},
        {"name": "edit", "instruction": "Make the FIRST bounded edit from your plan — one region, smallest change that works. Match the surrounding code's idiom. Then make the next, one at a time. Keep each edit small and self-contained; do not rewrite whole files. Stop when the plan's edits are applied."},
        {"name": "verify", "instruction": "VERIFY the change actually runs: import/parse the changed files, run the narrowest test or smoke check that exercises the change. If it fails, fix the cause (not the symptom) with another small edit. State clearly whether it is verified-green or still red, with the evidence."},
    ],
))

# Stubs for later author tiers — registered so `swarm types` advertises the roadmap, but
# they share the generic single-pass decompose until each grows its real section recipe.
for _stub_name, _stub_desc in (
    ("novel", "Author long-form prose chapter by chapter (stub — generic single-pass until its recipe is grown)"),
    ("art", "Author visual art via image generation (stub — generic single-pass until its recipe is grown)"),
    ("music", "Author music/audio composition (stub — generic single-pass until its recipe is grown)"),
):
    _register_builtin(SwarmType(
        name=_stub_name, kind="author", description=_stub_desc,
        cartridge="architect", tier="deep", artifact=f"{_stub_name.upper()}.txt",
        decompose=[{"name": "draft", "instruction": "Produce the artifact in bounded steps toward the goal; this type's section recipe is not yet specialized."}],
    ))


# ── Registry access ───────────────────────────────────────────────────────────

def get(name: str) -> SwarmType | None:
    """Get a swarm type by name. Built-in first, then user."""
    st = _BUILTIN.get(name)
    if st is not None:
        return st
    return _load_user().get(name)


def all_types() -> list[SwarmType]:
    """All swarm types: built-in first, then user-registered."""
    builtin = list(_BUILTIN.values())
    builtin_names = {s.name for s in builtin}
    user = [s for name, s in _load_user().items() if name not in builtin_names]
    return builtin + user


def builtin_names() -> set[str]:
    return set(_BUILTIN.keys())


# ── User registry (survives pip upgrades) ─────────────────────────────────────

def _load_user() -> dict[str, SwarmType]:
    """Load user-registered swarm types from ~/.echelon/swarms.json."""
    if not _USER_SWARMS_PATH.exists():
        return {}
    try:
        data = json.loads(_USER_SWARMS_PATH.read_text(encoding="utf-8"))
        out: dict[str, SwarmType] = {}
        for item in data if isinstance(data, list) else []:
            st = SwarmType(
                name=item.get("name", ""),
                kind=item.get("kind", "read"),
                mode=item.get("mode", "plan"),
                description=item.get("description", ""),
                lenses=item.get("lenses", []),
                seats=item.get("seats", []),
                frame=item.get("frame", ""),
                cartridge=item.get("cartridge", "architect"),
                model=item.get("model", ""),
                tier=item.get("tier", ""),
                author_model=item.get("author_model", ""),
                gate_model=item.get("gate_model", ""),
                decompose=item.get("decompose", []),
                artifact=item.get("artifact", ""),
                source="user",
            )
            if st.name:
                out[st.name] = st
        return out
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def _save_user(specs: dict[str, SwarmType]) -> None:
    _USER_SWARMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = [{
        "name": s.name, "kind": s.kind, "mode": s.mode, "description": s.description,
        "lenses": s.lenses, "seats": s.seats, "frame": s.frame,
        "cartridge": s.cartridge, "model": s.model,
        "tier": s.tier, "author_model": s.author_model, "gate_model": s.gate_model,
        "decompose": s.decompose, "artifact": s.artifact,
    } for s in specs.values()]
    tmp = _USER_SWARMS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(_USER_SWARMS_PATH)


def register_user(name: str, mode: str = "plan", description: str = "",
                  lenses: list[dict] | None = None, seats: list[dict] | None = None,
                  frame: str = "", cartridge: str = "architect",
                  model: str = "", kind: str = "read") -> SwarmType:
    """Register a user swarm type → ~/.echelon/swarms.json. Idempotent by name.

    User-registered types may only be kind='read'. Types that BIND CODE — 'execute'
    (the board coordinator) and 'author' (the agent-loop driver) — are built-in only,
    because a user JSON entry can't carry the code those kinds dispatch into.
    """
    if kind != "read":
        raise ValueError(
            f"user-registered types may only be kind='read'; '{kind}' types bind code "
            f"and are built-in only")
    user = _load_user()
    st = SwarmType(
        name=name, kind=kind, mode=mode, description=description,
        lenses=lenses or [], seats=seats or [], frame=frame,
        cartridge=cartridge, model=model, source="user",
    )
    user[name] = st
    _save_user(user)
    return st


def unregister_user(name: str) -> bool:
    """Remove a user swarm type. Returns True if removed."""
    user = _load_user()
    if name not in user:
        return False
    del user[name]
    _save_user(user)
    return True
