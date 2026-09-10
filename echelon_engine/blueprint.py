"""echelon blueprint — the PRE-CODE page-design discipline (design a page the way a senior does).

Where `xray` audits a whole REPO until you understand it, `blueprint` runs the same spine at
PAGE altitude and encodes the discipline an LLM skips: it prepares the whole page BEFORE a line
of code — purpose, capabilities, data, presentation, interaction surfaces, form CORRECTNESS,
states, responsiveness — and only THEN cosmetics. Each phase is judged against the page's
declared PURPOSE and (optionally) LOCKS its decision into the component-atlas so the skeleton is
durable and future pages REUSE it instead of re-inventing.

THE PIPELINE (mirrors xray: ground → comprehend → equipped lenses → gap → chair verdict):
  0. GROUND (deterministic, $0)   the page HTML + the /api/* it calls + its existing atlas cards.
  1. PURPOSE (1 cheap call)       what this page is FOR: the one job, personas, context/device.
  2. LENSES (parallel, equipped)  the 8 design lenses, each a focused question answered against
                                  the purpose: capabilities, data, presentation, interaction,
                                  FORM-CORRECTNESS, states, responsiveness, cosmetic.
  3. GAP LOOP (the loop)          a critic asks what stayed unexamined; re-runs starved lenses.
  4. VERDICT (1 strong call)      the chair emits the BUILD-SPEC: the page skeleton + the atlas
                                  cards to lock + a build directive claude-echelon can follow.
  5. LOCK (--lock, optional)      write the verdict's cards into the atlas (current in-repo atlas
                                  by default) so the decisions become durable contracts.

The correctness gap this closes: lenses data/interaction/form/states own exactly what `polish`
(cosmetic-only) and `ux` (repo-altitude) both miss — right input TYPE, VALIDATION, fields
SURFACED, error STATES — per page, against its purpose.

USAGE
  echelon blueprint api_app_dash/web/public/os/finance.html
  echelon blueprint <page> --lenses data,forms,states --loops 0
  echelon blueprint <page> --lock          # write cards into the atlas
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Reuse xray's proven organs — do NOT reinvent them.
from echelon_engine.xray import (
    _ask, _extract_json, _extract_json_tail, _as_dict, _safe_card_id, _CARD_KEYS,
    atlas_scan, atlas_digest,
)

_PAGE_CAP = 14_000          # chars of the page source fed to seats (bounded)
_SIB_CAP = 4_000            # chars of a shared asset (app.js) excerpt
_PROBES_PER_ROUND = 5


# ══ PHASE 0 — GROUND (page altitude — one page + its contracts, deterministic) ══

_API_RE = re.compile(r"""["'`](/api/[A-Za-z0-9_\-/{}.:?=&]+)["'`]""")
_FN_RE = re.compile(r"""\b(open[A-Z]\w*|show[A-Z]\w*|toggle[A-Z]\w*)\s*\(""")
_INPUT_RE = re.compile(r"<(input|select|textarea)\b[^>]*>", re.IGNORECASE)


def page_ground(page: Path, repo_root: Path) -> dict:
    """Deterministic census of ONE page — the evidence floor. No LLM.
    Reads the page source, the /api/* endpoints it references, the trigger fns it spawns
    (open*/show*/toggle* → drawers/modals/sheets), and its raw <input>/<select> inventory."""
    src = page.read_text(encoding="utf-8", errors="replace")
    endpoints = sorted(set(m.group(1).split("?")[0] for m in _API_RE.finditer(src)))
    triggers = sorted(set(m.group(1) for m in _FN_RE.finditer(src)))
    inputs = _INPUT_RE.findall(src)
    input_lines = [m.group(0)[:160] for m in _INPUT_RE.finditer(src)][:40]
    # stylesheet + script links (what the page stands on)
    links = re.findall(r"""<(?:link|script)[^>]*(?:href|src)=["']([^"']+)["']""", src)
    body_page = re.search(r'data-page=["\']([\w-]+)["\']', src)
    return {
        "page": str(page.relative_to(repo_root)) if _is_relative(page, repo_root) else str(page),
        "page_name": page.stem,
        "data_page": body_page.group(1) if body_page else page.stem,
        "size": len(src),
        "endpoints": endpoints,
        "triggers": triggers,          # spawned components (drawers/sheets/modals)
        "input_count": len(inputs),
        "input_sample": input_lines,
        "assets": sorted(set(l for l in links if "/api/" not in l))[:20],
        "src": src[:_PAGE_CAP],
    }


def _is_relative(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve()); return True
    except ValueError:
        return False


def page_digest(g: dict) -> str:
    """Bounded text form of the page ground that rides in every prompt."""
    parts = [
        f"PAGE: {g['page']}  (data-page={g['data_page']}, {g['size']} chars, "
        f"{g['input_count']} raw inputs)",
        f"ENDPOINTS it calls ({len(g['endpoints'])}): "
        + (", ".join(g['endpoints']) or "(none found)"),
        f"TRIGGER-SPAWNED components (open*/show*/toggle*): "
        + (", ".join(g['triggers']) or "(none)"),
        f"ASSETS: " + ", ".join(g['assets']),
    ]
    if g["input_sample"]:
        parts.append("RAW INPUT/SELECT/TEXTAREA sample (check each for correct type + validation):\n"
                     + "\n".join("  " + s for s in g["input_sample"]))
    parts.append("\n── PAGE SOURCE (truncated) ──\n" + g["src"])
    return "\n".join(parts)


# ══ PHASE 1 — PURPOSE (what is this page FOR) ══════════════════════════════════

_PURPOSE_PROMPT = """\
You are a senior product engineer about to (re)design ONE page. BEFORE any code, establish what
the page is FOR — reason ONLY from the page census below; cite what you inferred from. A ```json
fence:

```json
{{
  "purpose": "one sentence: the ONE job this page exists to do",
  "personas": ["who uses it — role, skill level"],
  "context": "device / urgency / how often they use it",
  "primary_action": "the single most important thing a user does here",
  "secondary_jobs": ["2-3 supporting jobs"],
  "evidence": ["what in the census supports each conclusion"]
}}
```

── PAGE CENSUS ──
{digest}
"""


# ══ PHASE 2 — LENSES (the senior's design order, each vs the purpose) ══════════

# lens → (cartridge to equip, the focused question). The ORDER is the discipline.
_LENSES: dict[str, tuple[str, str]] = {
    "capabilities": ("ux",
        "CAPABILITIES. Enumerate every VERB this page must support to serve its purpose — "
        "view, filter/search, sort, create, edit, delete, confirm, export, navigate. For each: "
        "is it PRESENT in the page now (cite), MISSING, or half-wired? Which verbs are core to "
        "the primary action vs secondary? Flag any capability the purpose implies but the page lacks."),
    "data": ("ux",
        "DATA & SURFACING. From the endpoints the page calls, list what DATA it surfaces and where "
        "from. For each dataset: is every field the user NEEDS surfaced, or is a key field buried/"
        "missing? Are fields shown that serve nobody (cut them)? Map each endpoint → what it feeds. "
        "Name the single most important datum the eye should hit first."),
    "presentation": ("frontend-design",
        "PRESENTATION. For each dataset, prescribe the RIGHT display: a table (many rows, scan/"
        "compare), cards (few rich items), a KPI/stat (single number), a chart (trend/breakdown), "
        "a list (simple sequence). Match the form to the data's nature AND the reading job. Flag "
        "every current mismatch (e.g. a table where a KPI belongs, a wall of cards that should be a "
        "table). Prescribe row height / column set / truncation for tables; the card anatomy for cards."),
    "interaction": ("frontend-design",
        "INTERACTION SURFACES. Enumerate the spawned UI the capabilities require: dropdown action "
        "menus, primary/secondary buttons, popovers, DRAWERS (detail/edit), MODALS, CONFIRMATION "
        "boxes (for destructive/irreversible actions), toasts. For each: what triggers it, what it "
        "contains, does it exist now (the census lists trigger fns). Flag any destructive action with "
        "NO confirmation, any action with no affordance, any drawer/modal that should exist but doesn't."),
    "forms": ("frontend-design",
        "FORM CORRECTNESS — the heart. For EVERY input/select/textarea (the census samples them): "
        "(1) correct TYPE? number→type=number+inputmode=numeric, money→decimal, date→type=date, "
        "email/tel/url→their types, not a raw text box; (2) VALIDATION — required marked? format/"
        "range enforced? what happens on bad input; (3) ERROR UX — is there inline error feedback or "
        "does it fail silently; (4) LABEL associated + visible; (5) field COUNT — which fields to CUT "
        "(derivable), DEFAULT (pre-fill), DEFER (progressive disclosure), STAGE (wizard). Deliver the "
        "corrected input spec per field. This is what a client notices AND what breaks in real use."),
    "states": ("ux",
        "STATE MACHINE — the false-pass killer. For the page and each surface (table, form, drawer) "
        "enumerate ALL states: initial/loading, EMPTY (no data), PARTIAL, ERROR (network, VALIDATION, "
        "permission, not-found), SUCCESS, EDGE (huge/tiny/slow/offline data). For each: what does the "
        "user SEE and what can they DO? Name every state currently UNDEFINED (most pages only build the "
        "happy idle frame). A form with no validation-error state and a table with no empty state are failures."),
    "responsive": ("frontend-design",
        "RESPONSIVENESS. How must this page reflow to serve NICELY at each viewport (wide desktop, "
        "laptop, tablet, phone)? What is the breakpoint behavior for the primary surface (table→cards? "
        "columns drop? drawer→full-screen?), the action rail, the nav? Name concrete breakpoints and what "
        "each does. Flag what breaks or overflows today."),
    "cosmetic": ("frontend-design",
        "COSMETIC CRAFT — judged LAST (polish on a wrong page is wasted). Hierarchy (size/weight/color "
        "match importance?), spacing rhythm (8px), typography scale, color restraint + contrast, and "
        "CONSISTENCY (same things look the same). Flag the pixel-level amateur tells: unstyled inputs, "
        "off-scale paddings, divergent chrome, bare empty states, missing focus rings. Copy-ready fixes only."),
}

_DEFAULT_LENSES = ["capabilities", "data", "presentation", "interaction", "forms", "states"]
_ALL_LENSES = list(_LENSES)   # + responsive, cosmetic in --deep


def _lens_prompt(lens: str, digest: str, purpose: dict, extra: str) -> str:
    from echelon_engine.swarm.context import SwarmContext
    cart, question = _LENSES[lens]
    ctx = SwarmContext(cart)
    goal = (f"PAGE DESIGN — the '{lens}' lens (judge everything against the page's PURPOSE below).\n"
            f"{question}\n\n"
            "Ground every finding in the page census; cite what you reasoned from. Put findings in "
            "the JSON `findings` array and your ordered, concrete PRESCRIPTION in `recommendations` "
            "(the corrected spec a builder can follow) + a prose summary.")
    context = (f"── THE PAGE'S PURPOSE (phase-1) ──\n{json.dumps(purpose, indent=1)[:1800]}\n\n"
               f"── PAGE CENSUS ──\n{digest}")
    if extra:
        context += f"\n\n── ATLAS: existing reusable cards (REUSE these, don't reinvent) ──\n{extra}"
    prompt, _ = ctx.build(goal, context)
    return prompt


# ══ PHASE 3 — GAP LOOP ═════════════════════════════════════════════════════════

_CRITIC_PROMPT = """\
You are the COMPLETENESS CRITIC of a page design. Below: the page census and every lens's output.
Your one job: what stayed UNANSWERED that would leave the page incorrect or unfinished — a form
with no validation state, a destructive action with no confirm, a dataset with no empty state, a
capability with no surface? Return a ```json fence:

```json
{{
  "unanswered": ["a specific correctness/completeness gap still open"],
  "starved_lenses": ["which lenses ({lenses}) would change their answer if they reconsidered"]
}}
```
If nothing material is missing, return empty lists — do not invent work.

── PAGE CENSUS ──
{digest}

── LENS OUTPUT SO FAR ──
{findings}
"""


# ══ PHASE 4 — VERDICT (the build-spec) ═════════════════════════════════════════

_CHAIR_PROMPT = """\
You are the CHAIR of a page design. Below: the page census, its purpose, and every lens's
prescription. Synthesize the BUILD-SPEC — the page skeleton a senior would hand a builder BEFORE
they write code. Resolve lens disagreements EXPLICITLY. No filler.

Write a markdown report with EXACTLY these sections:

# PAGE BLUEPRINT — {page_name}
## 1. PURPOSE            (the one job, personas, context, primary action)
## 2. CAPABILITIES       (the verbs, core vs secondary; gaps to add)
## 3. DATA & SURFACING   (each endpoint → what it feeds; the field surfacing decisions)
## 4. PRESENTATION       (per dataset: table/card/kpi/chart/list + the spec)
## 5. INTERACTION        (drawers/modals/dropdowns/confirms — trigger → content; missing ones)
## 6. FORMS              (per input: correct type, validation, error UX, label, cut/default/defer/stage)
## 7. STATES             (per surface: loading/empty/partial/error(+validation)/success/edge)
## 8. RESPONSIVE         (breakpoint behavior for the primary surface, rail, nav)
## 9. COSMETIC           (the craft layer — last)
## 10. HIERARCHY (IA)    (THE ANTI-CROWDING PASS — rank every component: what is THE ONE primary
                         surface the eye hits first; what is secondary; what gets GROUPED into a
                         labeled cluster; what is rare/expert and belongs behind a drawer/details.
                         A page that puts 8 equal tables on one plane is a FAILURE — say which
                         collapse into a group and which defer. This is where you fix over-crowding.)
## BUILD ORDER           (the ordered steps a builder follows: skeleton → data → forms → states → polish)

Then, AFTER the report, a ```json fence for the machine — the atlas cards this design LOCKS
(the page card + its component/form/table/etc cards). Use the atlas vocabulary:
```json
{{"cards": [
  {{"id":"{page_id}","type":"page","title":"...","what_it_is":"...","role":"...",
    "part_of":"[[{repo_id}]]","status":"partial","decision":"the key page-level design decisions",
    "endpoints":["..."],"reads_fields":["..."]}},
  {{"id":"{page_id}-<component>","type":"form|table|kpi|chart|list|control|panel","title":"...",
    "what_it_is":"...","role":"...","part_of":"[[{page_id}]]","status":"planned|partial|live",
    "decision":"the presentation/interaction/state decision for this component",
    "importance":1,"disclosure":"surface","group":null}}
]}}
```
ids kebab-case; only emit keys you have evidence for; absence = unknown.

IA CONTRACT — EVERY non-page card MUST carry these three keys (the skeleton generator is the
layout engine; it lays out BY these ranks — omitting them makes the page render FLAT, which is
the over-crowding bug this pass exists to kill):
- "importance": 1 = primary (the one surface the eye hits first — usually ONE per page),
  2 = secondary (supporting), 3 = rare/expert. Do NOT make everything a 1; rank honestly.
- "disclosure": "surface" (visible on load) | "group" (clustered into a labeled <section> with
  its siblings sharing the same `group`) | "drawer" (hidden, opened by a row/trigger — for
  per-item detail) | "defer" (in a collapsed <details>, expand-in-place for rare/expert content).
- "group": a short cluster id (e.g. "traffic-tabs", "ads-metrics") for cards that belong together
  under ONE labeled section; null if the card stands alone. Cards sharing a group + disclosure
  "group" collapse into a single section — THIS is how you turn 8 stacked tables into one
  tabbed/grouped cluster instead of 8 equal planes.
Judge importance/disclosure against the page's PURPOSE and primary_action: the primary action's
surface is importance:1/surface; bulk raw-data tables are usually group or defer, never 8-on-a-plane.

CONTRACT — every card with "type":"form" MUST additionally carry a machine "fields" array in
EXACTLY this shape (NOT prose in `decision` — the skeleton generator reads this deterministically
and REJECTS a form card without it). One object per input, in display order:
```json
{{"id":"page-...-form-x","type":"form", ...,
  "fields":[
    {{"id":"<REAL dom id from the page census — reuse the actual input id, do not invent>",
      "label":"<visible label, Indonesian>","control":"input|select|textarea|checkbox|radio",
      "type":"text|number|date|email|tel|url|search|checkbox|radio|hidden",
      "inputmode":"numeric|decimal|tel|email|url|text",   // omit if plain text
      "required":true,
      "validation":{{"min":0,"max":999999,"step":"0.01","maxlength":500,"pattern":"...",
                    "domain_rule":"a rule attrs can't express, e.g. must not exceed available stock"}},
      "error":"<inline error copy, Indonesian>",
      "options_source":"/api/... or null",
      "disposition":"surface|default|defer|stage|cut"}}
  ],
  "form_rules":{{"submit_disabled_until_valid":true,"server_error_surface":true,"confirm_on_submit":false}}
}}
```
Use the REAL input ids/types from the RAW INPUT sample in the census as the base; the forms lens
says what to CORRECT (right type, add inputmode, add min/max, add validation). Omit "cut" fields.
Absence of a validation key = that constraint does not apply (never a guessed default).

── PAGE PURPOSE ──
{purpose}

── PAGE CENSUS ──
{digest}

── LENS PRESCRIPTIONS ──
{findings}

── OPEN GAPS (critic) ──
{unanswered}
"""


def _findings_digest(lens_out: dict[str, dict]) -> str:
    parts = []
    for ln, d in lens_out.items():
        j = d.get("json")
        if j:
            parts.append(f"═ LENS {ln} ═\n{json.dumps(j, indent=1, ensure_ascii=False)[:3800]}")
        else:
            parts.append(f"═ LENS {ln} (unstructured) ═\n{d.get('raw','')[:2200]}")
    return "\n\n".join(parts)


# ══ ORCHESTRATION ══════════════════════════════════════════════════════════════

def run_blueprint(page: Path, repo_root: Path, lenses: list[str], loops: int,
                  provider: str, model: str, chair_model: str, out_dir: Path | None,
                  do_lock: bool, as_json: bool,
                  no_skeleton: bool = False, force_skeleton: bool = False,
                  allow_no_atlas: bool = False) -> int:
    def log(msg: str) -> None:
        if not as_json:
            print(msg, flush=True)

    log(f"\n══ ECHELON BLUEPRINT ══  {page.name}")
    log(f"   lenses: {', '.join(lenses)}   gap-loops: {loops}   provider: {provider}")

    # ── 0. GROUND ──
    log("\n[0/4] GROUND — page census ($0)")
    try:
        g = page_ground(page, repo_root)
    except OSError as e:
        log(f"✗ cannot read the page: {e}")
        if as_json:
            print(json.dumps({"error": "unreadable_page", "detail": str(e)}))
        return 1
    digest = page_digest(g)
    log(f"  {g['input_count']} inputs, {len(g['endpoints'])} endpoints, "
        f"{len(g['triggers'])} trigger-spawned components")

    # ── 0b. ATLAS (MANDATORY reuse anchor) — blueprint CANNOT act alone ──
    # Owner law 2026-07-10: "blueprint cannot act alone; for it to work, xray/atlas-component are
    # MANDATORY." The atlas is blueprint's reuse anchor — without it there is no component
    # vocabulary, no canonical_for to reuse, no 'don't reinvent what exists'. So blueprint REFUSES
    # when no atlas is present and points at xray to initialize one first (escape hatch: --allow-no-atlas).
    atlas = atlas_scan(repo_root)
    atlas_evidence = ""
    repo_id = repo_root.name.lower().replace(" ", "-")
    # the repo-relative page path for copy-paste guidance (page.name drops the dir, so
    # the printed re-run command would be wrong for a nested page like web/index.html)
    a_page_arg = g["page"] if not Path(g["page"]).is_absolute() else page.name
    if not atlas:
        if not allow_no_atlas:
            log("\n✗ NO COMPONENT ATLAS FOUND — blueprint cannot act alone.")
            log("  The atlas is blueprint's reuse anchor (canonical components, the shared shell,")
            log("  what already exists). Initialize it FIRST — `xray --atlas` lands the proposal")
            log("  at the canonical docs/c-atlas/ so this command auto-detects it on the next run:")
            log(f"    echelon xray {repo_root} --atlas        # propose + land the component atlas")
            log(f"    echelon blueprint {a_page_arg} --repo {repo_root}   # now it finds the atlas")
            log("  (override with --allow-no-atlas to blueprint against no atlas — NOT recommended.)")
            if as_json:
                print(json.dumps({"error": "no_atlas",
                                  "hint": f"run: echelon xray {repo_root} --atlas"}))
            return 4
        log("  ⚠ no atlas found — proceeding with --allow-no-atlas (no reuse anchor; components may be reinvented)")
    else:
        _kind = "PROPOSAL atlas (unverified — cards are inferred)" if atlas.get("proposal") else "atlas"
        log(f"  {_kind} FOUND: {atlas['dir']} ({len(atlas['cards'])} cards) — feeding reusable cards")
        atlas_evidence = atlas_digest(atlas)
        # a plausible repo root id from the atlas (root card)
        roots = [c["id"] for c in atlas["cards"] if c.get("type") in ("repo", "system")]
        if roots:
            repo_id = roots[0]
    page_id = f"page-{g['data_page']}"

    # ── 1. PURPOSE ──
    log("\n[1/4] PURPOSE — what is this page for?")
    purpose_raw = _ask(_PURPOSE_PROMPT.format(digest=digest), provider, model, "purpose", log)
    purpose = _as_dict(_extract_json(purpose_raw), {"purpose": purpose_raw[:400]})
    log(f"  → {purpose.get('purpose', '?')}")

    # ── 2. LENSES + 3. GAP LOOP ──
    lens_out: dict[str, dict] = {}
    unanswered: list[str] = []
    to_run = list(lenses)
    extra = atlas_evidence
    for round_no in range(loops + 1):
        if not to_run:
            break
        log(f"\n[2/4] LENSES — round {round_no + 1}: {', '.join(to_run)}")
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(to_run)))) as pool:
            futs = {pool.submit(_ask, _lens_prompt(ln, digest, purpose, extra),
                                provider, model, f"lens:{ln}", log): ln for ln in to_run}
            for fut in as_completed(futs):
                ln = futs[fut]
                try:
                    raw = fut.result() or ""
                except Exception as e:  # one dead lens must not kill the run
                    log(f"  ✗ lens:{ln} crashed ({type(e).__name__}: {e})")
                    raw = ""
                lens_out[ln] = {"json": _extract_json(raw), "raw": raw}
        if round_no >= loops:
            break
        log(f"\n[3/4] GAP LOOP — completeness critic (round {round_no + 1})")
        critic_raw = _ask(_CRITIC_PROMPT.format(
            digest=digest, findings=_findings_digest(lens_out),
            lenses=", ".join(lenses)), provider, model, "critic", log)
        critic = _as_dict(_extract_json(critic_raw), {})
        unanswered = critic.get("unanswered") or []
        starved = [l for l in (critic.get("starved_lenses") or []) if l in lenses]
        if not starved or not unanswered:
            log("  critic: nothing material missing — loop closes early")
            break
        # unlike xray, blueprint has no file probes — the critic's named gaps ARE the
        # new evidence; a rerun on a byte-identical prompt is a paid re-roll.
        extra = (atlas_evidence + "\n\n── GAPS THE CRITIC NAMED (answer these) ──\n"
                 + "\n".join(f"- {u}" for u in unanswered))
        to_run = starved

    # ── persist the paid lens work BEFORE the chair (a chair failure must not
    #    discard every completed seat's output) ──
    out = out_dir or (repo_root / ".echelon" / "blueprint" / g["page_name"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "blueprint.partial.json").write_text(json.dumps(
        {"purpose": purpose, "lenses": {k: v["json"] for k, v in lens_out.items()},
         "unanswered": unanswered}, indent=1, ensure_ascii=False), encoding="utf-8")

    # ── 4. VERDICT ──
    log("\n[4/4] VERDICT — the chair emits the build-spec")
    chair_raw = _ask(_CHAIR_PROMPT.format(
        page_name=g["page_name"], page_id=page_id, repo_id=repo_id,
        purpose=json.dumps(purpose, indent=1)[:2000], digest=digest,
        findings=_findings_digest(lens_out),
        unanswered="\n".join(f"- {u}" for u in unanswered) or "(none)"),
        provider, chair_model or model, "chair", log)
    # the machine fence rides AFTER the report; the report body echoes the prompt's own
    # example fences — first-fence extraction binds the contract to a fiction.
    cards_json = _extract_json_tail(chair_raw, prefer_key="cards") or {}
    cards = cards_json.get("cards", []) if isinstance(cards_json, dict) else (
        cards_json if isinstance(cards_json, list) else [])
    cards = [c for c in cards if isinstance(c, dict)]
    report_md = re.sub(r"```json\s*[\[{].*?[\]}]\s*```", "", chair_raw,
                       flags=re.DOTALL).strip()
    if not report_md:
        log("  ⚠ chair produced no report — publishing the raw lens findings instead")
        report_md = ("# PAGE BLUEPRINT (chair failed — raw lens findings)\n\n"
                     + _findings_digest(lens_out))

    # ── write outputs ──
    (out / "BLUEPRINT.md").write_text(report_md, encoding="utf-8")
    machine = {"ground": {k: v for k, v in g.items() if k != "src"},
               "purpose": purpose, "lenses": {k: v["json"] for k, v in lens_out.items()},
               "unanswered": unanswered, "cards": cards}
    (out / "blueprint.json").write_text(json.dumps(machine, indent=1, ensure_ascii=False),
                                        encoding="utf-8")

    # ── 4b. SKELETON — auto-generate the base scaffold if it doesn't exist yet ──
    # The middle organ (owner 2026-07-10): the blueprint decided WHAT the page is; the skeleton
    # bakes that into a real correct HTML scaffold. Generate it here so the pipeline flows without
    # a manual second step — idempotent (skip if present unless --skeleton forces a rebuild).
    if not no_skeleton and cards:
        skel_path = out / f"{g['page_name']}.skeleton.html"
        if skel_path.exists() and not force_skeleton:
            log(f"\n[4b] SKELETON — exists ({skel_path.name}), skipping (use --skeleton to rebuild)")
        else:
            try:
                from echelon_engine.skeleton import build_skeleton
                html = build_skeleton(machine)
                skel_path.write_text(html, encoding="utf-8")
                _readme = getattr(build_skeleton, "readme", "")
                if _readme:
                    (out / f"{g['page_name']}.skeleton.README").write_text(_readme, encoding="utf-8")
                viols = getattr(build_skeleton, "violations", [])
                n_in = html.count("<input") + html.count("<select") + html.count("<textarea")
                log(f"\n[4b] SKELETON — {skel_path.name}: {n_in} typed inputs, "
                    f"{html.count('FLASH:FILL')//2} fill slots"
                    + (f", ⚠ {len(viols)} contract violation(s)" if viols else ""))
                for v in viols:
                    log(f"      ⚠ {v}")
            except Exception as e:
                log(f"\n[4b] SKELETON — generation failed: {e}")

    # ── 5. LOCK (--lock) — write the cards into the atlas ──
    if do_lock and cards:
        if not atlas:
            log("\n[5] LOCK skipped — no atlas found to lock into")
        else:
            log(f"\n[5] LOCK — writing {len(cards)} card(s) into {atlas['dir']}")
            define = Path(atlas["dir"]) / "define"
            define.mkdir(parents=True, exist_ok=True)
            locked = []
            for c in cards:
                # the id is MODEL OUTPUT — sanitize before it becomes a filename
                # (a path-ish id like '../../x' or 'C:/x' would write outside the atlas),
                # and keep the lock scoped to THIS page's cards (an unscoped id could
                # silently overwrite the repo root card or another page's contract).
                cid = _safe_card_id(c.get("id"))
                safe_page = _safe_card_id(page_id)
                if not cid:
                    continue
                if cid != safe_page and not cid.startswith(safe_page + "-"):
                    log(f"    ⚠ skipped [[{cid}]] — outside this page's scope ({safe_page})")
                    continue
                card = {k: v for k, v in c.items()
                        if k in _CARD_KEYS and v not in ("", [], None)}
                card["id"] = cid
                # lineage is ENGINE-owned: never trust a model-claimed version/prev_hash
                card.pop("version", None)
                path = define / f"{cid}.json"
                if path.resolve().parent != define.resolve():
                    continue
                # append-friendly: bump version + prev_hash if the card already exists
                if path.is_file():
                    try:
                        old = json.loads(path.read_text(encoding="utf-8"))
                        import hashlib
                        old_v = old.get("version")
                        card["version"] = (old_v + 1) if isinstance(old_v, int) else 2
                        card["prev_hash"] = hashlib.md5(
                            json.dumps(old, sort_keys=True).encode()).hexdigest()[:12]
                    except Exception:
                        card["version"] = 2
                        card["prev_hash"] = "corrupt"
                else:
                    card["version"] = 1
                try:
                    path.write_text(json.dumps(card, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
                except OSError as e:
                    log(f"    ⚠ could not write [[{cid}]] ({e}) — skipped")
                    continue
                locked.append(cid)
                log(f"    locked [[{cid}]] v{card.get('version')} ({card.get('type')})")
            log(f"  {len(locked)} card(s) written — commit the atlas to seal the lock")

    if as_json:
        print(json.dumps(machine, ensure_ascii=False))
    else:
        log(f"\n══ BLUEPRINT COMPLETE ══")
        log(f"  report:  {out / 'BLUEPRINT.md'}")
        log(f"  machine: {out / 'blueprint.json'}")
        log(f"  cards proposed: {len(cards)}" + ("  (use --lock to write them)" if not do_lock else ""))
    return 0


# ══ CLI ══════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon blueprint",
        description="Design a page the way a senior does — BEFORE code. Purpose → capabilities → "
                    "data → presentation → interaction → form-correctness → states → responsive → "
                    "cosmetic, each judged against the page's purpose; emits a build-spec and "
                    "optionally locks the skeleton into the component-atlas.")
    ap.add_argument("page", help="path to the page file (e.g. web/public/os/finance.html)")
    ap.add_argument("--repo", default=".", help="repo root (for atlas + relative paths; default cwd)")
    ap.add_argument("--deep", action="store_true",
                    help=f"all {len(_ALL_LENSES)} lenses (adds responsive+cosmetic) + 2 gap loops")
    ap.add_argument("--lenses", default=None,
                    help=f"comma list from: {', '.join(_LENSES)} (default: {','.join(_DEFAULT_LENSES)})")
    ap.add_argument("--loops", type=int, default=None, help="gap-loop rounds (default 1; --deep 2; 0 none)")
    ap.add_argument("--lock", action="store_true", help="write the verdict's cards into the atlas")
    ap.add_argument("--provider", default="auto", help="auto | deepseek | gemini | anthropic")
    ap.add_argument("--model", default="", help="model override for scan/lens seats")
    ap.add_argument("--chair-model", default="", help="stronger model for the verdict")
    ap.add_argument("--out", default=None, help="output dir (default <repo>/.echelon/blueprint/<page>)")
    ap.add_argument("--skeleton", action="store_true", help="force-rebuild the base skeleton even if it exists")
    ap.add_argument("--no-skeleton", action="store_true", help="do NOT auto-generate the base skeleton")
    ap.add_argument("--allow-no-atlas", action="store_true",
                    help="blueprint against NO component atlas (NOT recommended — the atlas is the "
                         "mandatory reuse anchor; run `echelon xray --atlas` to initialize one first)")
    ap.add_argument("--json", action="store_true", help="machine output only")
    a = ap.parse_args(argv)

    repo_root = Path(a.repo).resolve()
    page = Path(a.page)
    if not page.is_absolute():
        page = (repo_root / a.page).resolve() if not page.exists() else page.resolve()
    if not page.is_file():
        print(f"blueprint: not a file: {page}", file=sys.stderr)
        return 1

    if a.lenses:
        lenses = [x.strip() for x in a.lenses.split(",") if x.strip()]
        bad = [x for x in lenses if x not in _LENSES]
        if bad:
            print(f"blueprint: unknown lens(es): {', '.join(bad)} — choose from {', '.join(_LENSES)}",
                  file=sys.stderr)
            return 2
        if not lenses:
            print(f"blueprint: --lenses named none — choose from {', '.join(_LENSES)}",
                  file=sys.stderr)
            return 2
    else:
        lenses = list(_ALL_LENSES if a.deep else _DEFAULT_LENSES)
    loops = max(0, a.loops) if a.loops is not None else (2 if a.deep else 1)

    return run_blueprint(page, repo_root, lenses, loops, a.provider, a.model, a.chair_model,
                         Path(a.out).resolve() if a.out else None, a.lock, a.json,
                         no_skeleton=a.no_skeleton, force_skeleton=a.skeleton,
                         allow_no_atlas=a.allow_no_atlas)


if __name__ == "__main__":
    sys.exit(main())
