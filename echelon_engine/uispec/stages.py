#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stages.py — the TASK prompts, rescued verbatim-in-spirit from the page-rework scratchpad scripts.

Each stage is the PROMPT + posture; the runner (`_runner.py`) owns the mechanics. Kept as constants
so the judgment the arc paid ~$1.5M gemini tokens to discover is versioned, not re-improvised each
time. Provenance (which throwaway script each came from) is noted per prompt.

Two families:
  GENERATORS  — produce an artifact (spec / audit-worker / blueprint / synth). temp≈0.2–0.3.
  GATES       — refute an artifact against the real code (a clearing from a context that
                did NOT build it). temp≈0.1, skeptic posture.
"""
from __future__ import annotations

# ── AUDIT (the page-rework HEAVY worker) — from page_rework_gemini.py ───────
AUDIT_TASK = """You are a SENIOR front-end architect + skeptical UX/UI reviewer auditing ONE page
of a Shopee seller-operations dashboard ("Seller OS"), Indonesian UI. You are given the
page's HTML shell and its page-<slug>.js controller (thin-host SPA: a router swaps the
view body; the page JS mounts components, consumes /api endpoints, renders tables/forms).

Produce a SINGLE markdown document with EXACTLY these three top-level sections:

## 1. FORENSIC AUDIT + VERDICT
- **What this page claims to be** (infer its job from name, structure, endpoints).
- **What it actually does** (enumerate: endpoints consumed, tables, tabs, forms/fields,
  key buttons/actions, major responsibilities).
- **Inventory counts** (endpoints, tables, forms, fields, buttons, distinct responsibilities).
- **VERDICT: does it do what it claims?** State gaps, dead ends, half-built flows, things
  that surface RAW DATA instead of answering the user's decision.
- **SKEPTIC UX/UI LENS** — position yourself as a demanding user. Is there hierarchy? Are
  proper components used? Is it structured or a flat dump? Is one page doing too many jobs?
  Call out amateur tells, crammed density, missing states (empty/loading/error), and any
  place the page makes the USER compute what the software should have answered.

## 2. BLUEPRINT CONTRACT
Redesign the page AS A CONTRACT (not code). Cover, in order:
- **Purpose** (one sentence — the decision this page exists to serve).
- **Capabilities** (the jobs it must support).
- **Data contract** (what it reads, from which endpoints; what it writes).
- **HIERARCHY** (primary -> secondary -> tertiary; what leads the eye, what is on-demand).
- **Component map** (which component per slot: table / master-detail / drawer / wizard / KPI).
- **Interaction & states** (empty, loading, error, partial, success for each flow).
- **What to SPLIT OUT** if the page is overloaded (name the sub-pages/menus).

## 3. SCALE SIMULATION
Simulate this page under LOAD. Be concrete and quantitative:
- **Millions of rows**: which tables/lists explode? Where does client-side rendering,
  Tabulator, or unpaginated fetch break? Name the exact failure points and the fix
  (server pagination, virtual scroll, indexed search, aggregation-at-source).
- **Thousands of concurrent users**: which endpoints get hammered? N+1 fetch patterns,
  missing caching, polling storms, SSE fan-out. Name them and the fix.
- **Verdict**: is the current architecture survivable at scale, or does it need a rewrite?
"""

# ── AUDIT GATE (skeptic refuter of the audit doc) — from page_rework_gate.py ─
AUDIT_GATE_TASK = """You are a SKEPTICAL senior reviewer GATING another architect's audit of a page.
You did NOT write the audit. Your job is to REFUTE it, not agree with it. You are given:
(A) the ORIGINAL page code (HTML + JS), and (B) the AUDIT+BLUEPRINT+SCALE doc produced about it.

Go claim by claim through the doc. For EACH significant claim (a named failure point, an
inventory count, a scale verdict, a proposed split), render one of:
- **CONFIRMED** — the code backs it; cite the function/line evidence.
- **OVERSTATED** — partly true but exaggerated; say what's actually true.
- **WRONG** — the code contradicts it; cite the contradiction.
Default to skepticism: if you cannot find code evidence, mark it UNVERIFIED, do not pass it.

Then two more sections:
## MISSED — what the audit FAILED to catch (real problems in the code it didn't mention).
## GATE VERDICT — is this rework doc SAFE to act on? Which of its recommendations are solid
vs which need rework before we build. Be decisive.

Structure your reply as markdown with headers. Be terse and evidence-first — cite code."""

# ── SYNTH (reconcile audit + gate into one build-ordered rework doc) — from run_synth_batch.py ─
SYNTH_TASK = """You are the SYNTHESIZER closing out a page-rework analysis. You are given:
(A) the AUDIT doc (forensic audit + blueprint contract + scale simulation), and
(B) the skeptic GATE verdict that refuted it claim-by-claim (CONFIRMED / OVERSTATED / WRONG / MISSED).

Produce ONE final, build-ordered rework document. Rules:
- KEEP every claim the gate CONFIRMED. DROP or soften every claim it marked WRONG/OVERSTATED —
  do not smuggle a refuted claim back in. FOLD IN everything under the gate's MISSED section.
- Emit these sections: **Purpose**, **Confirmed problems** (post-gate, evidence-cited),
  **Blueprint contract** (purpose -> hierarchy -> component map -> states, reconciled),
  **Scale fixes** (only the survivable-at-scale prescriptions the gate confirmed),
  **BUILD ORDER** (a numbered, dependency-aware sequence a builder can execute worst-first).
- If a COUNCIL decision was supplied, honor it exactly (e.g. a binding split/keep ruling).
Be decisive and terse. Markdown with headers. This doc IS the build contract."""

# ── SPEC (two-layer component spec — the container floor-plan grammar) — from spec_component.py ─
SPEC_TASK = """You are a STAFF UX ARCHITECT producing a TWO-LAYER COMPONENT SPEC for one page \
('{slug}') of a Shopee-seller OS, as the foundation for reworking it from a flat faithful copy into \
a DERIVED, responsive, space+resource-aware layout (the 'skyscraper': go vertical via \
tabs/drawer/accordion/scroll, use lazy per-slot loading as the 'elevator' so weak devices only \
render the visible floor).

CONTAINER GRAMMAR (the real coordinate system — spec footprints in THESE units):
- WIDTH = EVEN units on a 16-col grid that scales 16 desktop -> 8 tablet -> 4 mobile. Odd width \
breaks reflow. So min_w is an EVEN integer 2..16.
- HEIGHT = fixed ~40px ROW-UNITS that do NOT scale across viewports. min_h is any integer (row-units).
- Responsive: state each block's reflow behavior at 8-col (tablet) and 4-col (mobile).

TWO LAYERS:
- LAYER 1 (visual block): each on-screen unit the eye sees as one thing (e.g. info-note, \
followup-badge, search-row, filter-chips, returns-table, row-detail/actions). This layer drives \
LAYOUT DERIVATION.
- LAYER 2 (registry map): map each visual block to the LXComponent render fn it should use \
(one of: modal, table, toast, search, drawer, badge, empty) or mark NEW if none fits. This layer \
drives the BUILD.

For EACH visual block emit an object with EXACTLY these fields:
  id, label, layer2_component (registry name or 'NEW:<name>'), renders_via (the page fn/markup source),
  min_w (even 2..16), min_h (row-units int), reflow_8col, reflow_4col,
  collapsible (bool + when), simultaneity ('always-visible' | 'multiplexable' | 'on-demand'),
  dep_edges (list of other block ids it has a LIVE DATA dependency on — these CANNOT be split across
  tabs/floors from each other), lifecycle_states (which of idle/loading/empty/error/result it has),
  when_to_use ('the job-to-be-done that justifies this component TYPE — if the type is wrong for the \
job, say so'), footprint_note (any honest caveat about the size claim).

Then a top-level 'clusters' array: group the blocks by dependency+simultaneity into candidate FLOORS
(a cluster = blocks that must be co-visible). And 'notes' for anything the auditor must scrutinize.

Output ONE json object: {{page, blocks:[...], clusters:[...], notes:[...]}}. Raw JSON, no fence, no prose."""

# ── SPEC GATE (audit the spec's dependency edges against real code) — from audit_spec.py ─
SPEC_GATE_TASK = """You are a SKEPTICAL STAFF ENGINEER auditing a TWO-LAYER COMPONENT SPEC for one \
page against that page's REAL code. You did NOT write the spec. The spec's most load-bearing (and \
most error-prone) claim is the DEPENDENCY EDGES — which visual blocks share LIVE DATA and therefore \
CANNOT be split across tabs/floors. A wrong edge either fuses blocks that are independent (killing a \
valid split) or splits blocks that share state (breaking the page). Refute every edge.

Given (A) the spec JSON and (B) the real HTML + page JS, go block by block:
- For each dep_edge, find the code evidence (shared variable, shared fetch, shared render target).
  Mark CONFIRMED (cite it), OVERSTATED (they touch but don't truly co-depend), or WRONG (independent).
- Flag any MISSING edge: blocks the code proves co-depend but the spec left unlinked.
- Check the min_w/min_h footprints and simultaneity for plausibility against what the code renders.
- Check each layer2_component mapping: is that the right component TYPE for the job, per the code?

Emit: a per-block verdict table, a MISSED-EDGES section, and a GATE VERDICT (is this spec safe to
derive a floor-plan from, or which edges must be fixed first). Terse, evidence-first, cite code."""

# ── ENVELOPE MAP (deterministic — no model): extract /api endpoints + handlers ─
# (from slice.py --stage map; kept as a doc note — the map stage is pure regex, lives in the CLI.)

# ── ENVELOPE MIGRATE (rewrite handlers to respond() + JS to read data[0]) — from slice.py --stage migrate ─
ENVELOPE_MIGRATE_ROUTES_TASK = """You are migrating a Python web app's API handlers to a UNIFORM \
RESPONSE ENVELOPE. The envelope is: every handler returns respond(payload) where the client receives \
a single object {{ ok, error, id, data, updatetime, version, source, tag }} and `data` is ALWAYS a \
list. A single-object result is wrapped as data:[obj]; a list result stays data:[...]. Non-JSON \
responses (file downloads, CSV, PDF, xlsx, attachments, /raw) are EXEMPT — leave them untouched.

Given the current handlers, rewrite EACH JSON handler to return via the envelope's respond(...) helper.
Preserve behavior exactly; change only the return shaping. Do not touch exempt handlers. Output the \
rewritten handler block(s) only, as valid Python, no prose."""

ENVELOPE_MIGRATE_JS_TASK = """You are migrating a page's front-end JS to consume the new UNIFORM \
RESPONSE ENVELOPE. Every /api call now returns {{ ok, error, id, data, updatetime, version, source, \
tag }} where `data` is ALWAYS a list. Where the code previously read the raw object, it must now read \
`resp.data[0]`; where it read a list, it now reads `resp.data`. Check `resp.ok` before use. Preserve \
all rendering/behavior; change only the unwrap. Output the rewritten JS only, no prose."""

ENVELOPE_GATE_TASK = """You are a SKEPTIC gating an API envelope migration. Given the ORIGINAL and \
the MIGRATED handlers + JS, verify per endpoint: (1) the handler returns the envelope shape with \
`data` as a list; (2) a single-object result is wrapped as data:[obj], not data:obj; (3) exempt \
(file/CSV/PDF/xlsx/raw) endpoints were NOT touched; (4) the JS reads data[0] for object endpoints and \
data for list endpoints, and checks ok. Mark each endpoint SOUND / UNSAFE (say why). End with a GATE \
VERDICT: safe to land, or which endpoints must be fixed. Evidence-first, cite the code."""

# ── KIT (build ONE LXComponent v1.2 registry component) — from kit_build.py ─
KIT_BUILD_TASK = """You are building ONE component for the LXComponent v1.2 registry, matching the \
existing contract EXACTLY. The contract (learn it from the reference components provided):
- Wrap in an IIFE guard; register via LXComponent.register(name, (d, {{h, money, int, attr}}) => html).
- PURE RENDER: no fetch, no timers, no state, no logic — a deterministic data->HTML function.
- XSS-safe: escape all interpolated data via h(); use trusted-html only for known nav/shell markup.
- Theme via CSS vars (var(--surface), var(--line), var(--r-lg)) and the .card/.btn classes; lx- prefix.
Given the registry contract + reference components + this component's spec, output ONLY the component \
JS (the full registered file), no prose."""

KIT_GATE_TASK = """You are a SKEPTIC gating a newly-generated LXComponent against the v1.2 contract \
and the reference components. Check: IIFE guard present; register signature exact; PURE render (no \
fetch/timer/state/logic); every interpolated datum escaped via h() (flag any raw interpolation as an \
XSS hole); theming via the CSS vars + lx- prefix; no invented helpers outside {{h, money, int, attr}}. \
Per-item verdict, then overall PASS / PASS-WITH-NITS / FAIL. Evidence-first, cite the code."""


# The stage registry: name -> (task_prompt, role). role picks the default model + temperature.
# 'gen' = generator (GEN_MODEL, temp 0.3); 'gate' = skeptic (GATE_MODEL, temp 0.1).
STAGES: dict[str, tuple[str, str]] = {
    "audit":            (AUDIT_TASK, "gen"),
    "audit-gate":       (AUDIT_GATE_TASK, "gate"),
    "synth":            (SYNTH_TASK, "gen"),
    "spec":             (SPEC_TASK, "gen"),
    "spec-gate":        (SPEC_GATE_TASK, "gate"),
    "envelope-routes":  (ENVELOPE_MIGRATE_ROUTES_TASK, "gen"),
    "envelope-js":      (ENVELOPE_MIGRATE_JS_TASK, "gen"),
    "envelope-gate":    (ENVELOPE_GATE_TASK, "gate"),
    "kit":              (KIT_BUILD_TASK, "gen"),
    "kit-gate":         (KIT_GATE_TASK, "gate"),
}
