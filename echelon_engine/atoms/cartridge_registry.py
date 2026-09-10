"""cartridge_registry — the one place that knows EVERY cartridge (owner 2026-06-20: "make it a registry,
there will be MUCH more cartridges born in the future").

THE GAP THIS CLOSES: each cartridge was HARDCODED in cartridge.py — UX had its own REFS block + compose_ux,
brainstorm its own block + compose_brainstorm, and a new one meant copy-paste-a-block + a new compose fn +
a hand-edited mem_dir. That doesn't scale to "much more cartridges." This registry is the declarative
catalog: ONE entry per cartridge (name, scope, label, refs, home_dir, skill), and the generic
compose/list/equip read from it. Registering a cartridge is adding a CartridgeSpec, not editing logic.

TWO-TIER REGISTRY (2026-06-26): built-in cartridges live in this module (the installed package).
User cartridges live at ~/.echelon/cartridges.json — registered via `echelon cartridge register`,
survive pip upgrades, and don't require editing the installed package. all_specs() merges both.
"""
from __future__ import annotations

import json
import os
from .. import estate as _estate
from dataclasses import dataclass, field
from pathlib import Path

# Canonical estate root — the directory that holds cartridge home dirs.
# Set ECHELON_ESTATE to override the default.
_ESTATE_ROOT = str(_estate.estate_root_for("command_root"))

# STARTER ATOMS FALLBACK (go-live audit 2026-07-30): when the hardcoded estate root
# does not exist on disk (a pip-installed user on a different machine), fall back to
# the packaged starter_atoms directory for the scopes that ship. This prevents
# cartridge home_dirs from silently resolving to a non-existent path.
_STARTER_ATOMS_DIR = str(
    Path(__file__).resolve().parent.parent / "data" / "starter_atoms")


def _resolve_home_dir(home_dir: str, scope: str) -> str:
    """Resolve a cartridge's home_dir, falling back to the packaged starter atoms
    when the estate root doesn't exist on disk. Returns '' if unresolvable."""
    if home_dir:
        if os.path.isdir(home_dir):
            return home_dir
        # Estate root doesn't exist — try the packaged starter atoms for this scope.
        starter = os.path.join(_STARTER_ATOMS_DIR, scope)
        if os.path.isdir(starter):
            return starter
    return home_dir

# User cartridge registry — survives pip upgrades, user-editable
_USER_REGISTRY_PATH = Path.home() / ".echelon" / "cartridges.json"


@dataclass
class CartridgeSpec:
    """One cartridge, declared. `refs` = the composed card's atoms IN FIRING ORDER (coordinates, across
    whatever scopes they live in — a capability composes across scopes). `home_dir` = where the scope's
    .md atoms are authored (planted via ingest). `skill` = the /skill name that equips it, if any."""
    name: str                       # the registry key (kebab; e.g. 'ux', 'brainstorm', 'scribe')
    scope: str                      # the home bank scope
    label: str                      # the composed card's label
    refs: list[str]                 # the card's atom coordinates, in firing order (may cross scopes)
    home_dir: str = ""              # where the scope's .md atoms live (for ingest); '' = inline/none
    skill: str = ""                 # the /skill that equips it, if built
    summary: str = ""               # one-line what-it-is
    source: str = "builtin"         # "builtin" | "user" — where this spec was registered


# ── THE REGISTRY — built-in cartridges live here; user cartridges in ~/.echelon/cartridges.json ──
_REGISTRY: dict[str, CartridgeSpec] = {}


def register(spec: CartridgeSpec) -> None:
    """Add/replace a cartridge in the BUILT-IN registry (idempotent by name).
    For user cartridges, use register_user() which persists to ~/.echelon/."""
    spec.source = "builtin"
    # Resolve home_dir against the starter_atoms fallback (go-live audit 2026-07-30).
    spec.home_dir = _resolve_home_dir(spec.home_dir, spec.scope)
    _REGISTRY[spec.name] = spec


# Aliases for retired/merged cartridges — `cartridge equip <old-name>` transparently
# resolves to the target cartridge with a deprecation note (the alias target must
# exist in the built-in or user registry). Old references keep working while
# pointing to the canonical cartridge.
_ALIASES: dict[str, str] = {
    "interface-build": "frontend-build",  # merged 2026-07-31 (89 lines → Template-Sourcing Entry Path)
}


def get(name: str) -> CartridgeSpec | None:
    """Get a cartridge by name. Checks built-in first, then aliases (retired/merged
    cartridges that redirect to a built-in target with a deprecation note), then
    the user registry."""
    spec = _REGISTRY.get(name)
    if spec is not None:
        return spec
    # Resolve aliases for retired/merged cartridges
    alias_target = _ALIASES.get(name)
    if alias_target:
        base = _REGISTRY.get(alias_target) or _load_user_registry().get(alias_target)
        if base is not None:
            import copy
            spec = copy.copy(base)
            spec.summary = (f"[DEPRECATED — merged into '{alias_target}' (2026-07-31); "
                            f"equip '{alias_target}' directly] {base.summary}")
            return spec
    return _load_user_registry().get(name)


def all_specs() -> list[CartridgeSpec]:
    """All cartridges: built-in first, then user-registered. User cartridges
    with the same name as a built-in are shadowed (built-in wins)."""
    builtin = list(_REGISTRY.values())
    user = _load_user_registry()
    # Built-in names shadow user duplicates
    builtin_names = {s.name for s in builtin}
    user_specs = [s for name, s in user.items() if name not in builtin_names]
    return builtin + user_specs


# ── User registry (survives pip upgrades) ─────────────────────────────────────

def _load_user_registry() -> dict[str, CartridgeSpec]:
    """Load user-registered cartridges from ~/.echelon/cartridges.json.
    Returns {} if missing or corrupt. Never crashes."""
    if not _USER_REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(_USER_REGISTRY_PATH.read_text(encoding="utf-8"))
        out: dict[str, CartridgeSpec] = {}
        for item in data if isinstance(data, list) else []:
            spec = CartridgeSpec(
                name=item.get("name", ""),
                scope=item.get("scope", ""),
                label=item.get("label", ""),
                refs=item.get("refs", []),
                home_dir=item.get("home_dir", ""),
                skill=item.get("skill", ""),
                summary=item.get("summary", ""),
                source="user",
            )
            if spec.name:
                out[spec.name] = spec
        return out
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def _save_user_registry(specs: dict[str, CartridgeSpec]) -> None:
    """Persist user cartridges to ~/.echelon/cartridges.json."""
    _USER_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "name": s.name, "scope": s.scope, "label": s.label,
            "refs": s.refs, "home_dir": s.home_dir, "skill": s.skill,
            "summary": s.summary,
        }
        for s in specs.values()
    ]
    tmp = _USER_REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(_USER_REGISTRY_PATH)


def register_user(name: str, scope: str, label: str = "", refs: list[str] | None = None,
                  home_dir: str = "", skill: str = "", summary: str = "") -> CartridgeSpec:
    """Register a user cartridge — persists to ~/.echelon/cartridges.json.
    Survives pip upgrades. Idempotent by name (overwrites existing)."""
    user = _load_user_registry()
    spec = CartridgeSpec(
        name=name, scope=scope, label=label or name,
        refs=refs or [], home_dir=home_dir, skill=skill, summary=summary,
        source="user",
    )
    user[name] = spec
    _save_user_registry(user)
    return spec


def unregister_user(name: str) -> bool:
    """Remove a user cartridge from ~/.echelon/cartridges.json.
    Returns True if it was removed, False if it didn't exist."""
    user = _load_user_registry()
    if name not in user:
        return False
    del user[name]
    _save_user_registry(user)
    return True


# ── THE BORN CARTRIDGES (accreting catalog) ─────────────────────────────────────────────────────────
# PURE UX (2026-07-01, owner): a cartridge is ONE capability's KNOWLEDGE, not the tooling
# of what-to-use. So `ux` now holds ONLY the design-reasoning atoms — the lenses, the
# component grammar, the judge-society seats, the consumer-not-agent doctrine, the cold-reader
# ship gate. The DELIVERY MACHINERY (Gemini roster, image/Pro loops, the code-writer hand, the
# render-to-see mechanism) moved OUT to the `ux-pipeline` cartridge — equip that separately when
# you want the machine. An equipped claude harness reasons these lenses DIRECTLY (no swarm, no
# gemini): the harness holds the cartridge as warm reasoning, that IS the design tier.
register(CartridgeSpec(
    name="ux",
    scope="ux-cartridge",
    label="UX CARTRIDGE — pure UX/UI design reasoning (lenses + component grammar + judge-society + consumer-not-agent + cold-reader gate)",
    summary="UX/UI design KNOWLEDGE: device→frame grammar -> when-to-use-X components -> architect/judge/consumer/supporter/critic seats -> ui-for-consumer-not-agent -> cold-reader ship gate. The design tier; equip `ux-pipeline` for the delivery machinery.",
    home_dir="",   # role atoms in ux-cartridge; doctrine atoms in echelon (both already planted)
    skill="cartridge",
    refs=[
        "ux_cartridge:ux_architect_role",
        # STRUCTURE before component: device decides the FRAME, then the minimum
        # condition decides each component (card/list/table/badge/chip/dialog/sheet/drawer).
        "ux_cartridge:device_frame_layout_grammar",
        "ux_cartridge:component_selection_thresholds",
        # the judge-society SEATS (pure design reasoning across the lifecycle states).
        "ux_cartridge:ux_judge_role",
        "ux_cartridge:ux_consumer_role",
        "ux_cartridge:ux_supporter_role",
        "ux_cartridge:ux_critic_role",
        "echelon:ux_society_multi_seat_multi_state",
        # the consumer-not-agent doctrine (the load-bearing frame).
        "echelon:ui_for_consumer_not_for_agent",
        "echelon:echelon_ui_is_not_an_agent_chat",
        "echelon:defer_non_obvious_fixes_note_them_on_deck",
        # the SHIP GATE: a LOW-FLOOR cold reader proves a no-context user reads the
        # screen right — where it explains wrong, the screen is wrong. Fires LAST.
        "ux_cartridge:cold_reader_gate_low_floor_honest_review",
        "echelon:ux_cartridge_built_earns_by_trace_not_by_skill",
    ],
))

# ux-pipeline: the DELIVERY MACHINERY split out of `ux` (2026-07-01, owner: "a cartridge should
# be pure capability atoms, not what-to-use; let the pipeline become its own cartridge/offer").
# This is the HOW you produce + SEE a real UI: the Gemini roster tiers, the Pro-authors-CODE
# stepwise loop (design in HTML/CSS, NOT mockup images — images garble text), the code-writer
# hand, and the render-DB-free-through-Flux SEE mechanism. Equip this ALONGSIDE `ux` when you
# want to actually build/render a surface; equip `ux` alone for pure design reasoning.
register(CartridgeSpec(
    name="ux-pipeline",
    scope="ux-cartridge",
    label="UX-PIPELINE CARTRIDGE — the delivery machinery: Pro-authors-CODE stepwise + code-writer hand + render-through-Flux to SEE (design in HTML, not mockup images)",
    summary="UX delivery HOW: Gemini roster tiers -> Pro authors real HTML/CSS stepwise (not one-shot, not images) -> ui-builder implements the brief exactly -> render DB-free through Flux to SEE + resize. The machine that turns a `ux` design into a verified surface.",
    home_dir="",
    skill="cartridge",
    refs=[
        "echelon:gemini_is_the_ux_author_tier",
        "echelon:gemini_3_roster_tiers_and_live_vertex_ids",
        "echelon:gemini_thinking_token_gate",
        "ux_cartridge:ui_builder_role",
        "echelon:image_first_ui_loop_and_the_text_garbling_ceiling",
        "echelon:pro_ui_loop_the_judge_needs_a_satisficing_bar",
        "ux_cartridge:flux_db_free_static_render_escapes_live_app_trap",
    ],
))

# architect: the PLANNING sibling of ux (ux = the surface; architect = the structure). Pure system-design
# planning before code — a comprehensive, scaling-aware plan, not an implementation. The chain follows the
# arc42 section order (a public CC BY-SA 4.0 method) fused with C4 + ADR + ISO 25010 quality attributes; the
# decomposition is a legal re-synthesis, not a copied prompt (see the provenance atom). Born 2026-06-22 for
# scope 3kongsa. The card chains the architect's thinking order: goals → constraints → context → quality
# scenarios → strategy → building blocks → runtime → scaling → cross-cutting → ADRs → risks, framed by the
# what-it-is + plan-before-code discipline atoms and closed by provenance.
register(CartridgeSpec(
    name="architect",
    scope="architect-cartridge",
    label="ARCHITECT CARTRIDGE — pure big-picture system-design planning (arc42→C4→ADR chain), a plan not code",
    summary="pure app-design/architecture planning: goals & quality drivers -> constraints -> context -> measurable quality scenarios -> solution strategy -> building blocks -> runtime -> scaling/deployment -> cross-cutting -> ADRs -> risks; stays at the planning tier",
    home_dir=f"{_ESTATE_ROOT}/architect-cartridge/memory",
    skill="",
    refs=[
        "architect_cartridge:architect_cartridge_is_pure_planning",        # what it is (frame)
        "architect_cartridge:architect_frame_goals_and_quality_drivers",   # §1
        "architect_cartridge:architect_surface_constraints_before_designing",  # §2
        "architect_cartridge:architect_context_and_scope_draw_the_boundary",   # §3 / C4 L1
        "architect_cartridge:architect_quality_scenarios_are_the_real_spec",   # §10 (NFRs drive structure)
        "architect_cartridge:architect_solution_strategy_the_load_bearing_few",  # §4
        "architect_cartridge:architect_decompose_into_building_blocks",    # §5 / C4 L2-3
        "architect_cartridge:architect_runtime_view_trace_the_critical_scenarios",  # §6
        "architect_cartridge:architect_scaling_and_deployment_growth_story",   # §7 (the scaling story)
        "architect_cartridge:architect_crosscutting_concepts_decide_once",  # §8
        "architect_cartridge:architect_record_decisions_as_adrs",          # §9 (ADRs)
        "architect_cartridge:architect_risks_and_tech_debt_premortem",     # §11 (pre-mortem)
        "architect_cartridge:architect_plan_before_code_keeps_the_tier_free",  # the discipline (CV-012)
        "architect_cartridge:architect_cartridge_provenance_and_license",  # legal provenance (fires last)
    ],
))

# engagement: the CLIENT FRONT DOOR — elicit the real requirement, bound the SOW, set acceptance criteria,
# map stakeholders/RACI, control change. Comes BEFORE pm (engagement gets the requirement out of the client
# + draws the contract boundary; pm decides what's worth building). A legal re-synthesis of public BA/
# requirements/PM methods (BABOK, IEEE 29148, PMBOK scope & change, SOW, RACI, acceptance/UAT, 5-whys).
# Born 2026-06-23. Chain: elicit -> bound SOW -> acceptance criteria -> stakeholders/RACI -> change control,
# framed by what-it-is + provenance.
register(CartridgeSpec(
    name="engagement",
    scope="engagement-cartridge",
    label="ENGAGEMENT CARTRIDGE — the client front door: elicit -> SOW scope -> acceptance criteria -> stakeholders/RACI -> change control",
    summary="client engagement / business analysis: elicit the real requirement (5-whys, testable) -> bound the scope (SOW in/out) -> acceptance criteria (define done) -> map stakeholders & decision rights (RACI) -> control change (not scope-creep); the agreed requirement BEFORE product judgment",
    home_dir=f"{_ESTATE_ROOT}/engagement-cartridge/memory",
    skill="",
    refs=[
        "engagement_cartridge:engagement_cartridge_is_the_client_front_door",   # what it is (frame)
        "engagement_cartridge:engagement_elicit_the_real_requirement",          # §1 elicit
        "engagement_cartridge:engagement_bound_the_scope_sow",                  # §2 SOW
        "engagement_cartridge:engagement_acceptance_criteria_define_done",      # §3 acceptance
        "engagement_cartridge:engagement_map_stakeholders_and_decision_rights", # §4 RACI
        "engagement_cartridge:engagement_control_change_not_scope_creep",       # §5 change control
        "engagement_cartridge:engagement_cartridge_provenance_and_license",     # legal provenance (fires last)
    ],
))

# delivery: EXECUTION ORCHESTRATION — the connective tissue. Decompose the plan into phases/work-packages
# (WBS), assign each to the right team/cartridge, sequence by dependency (critical path), define the hand-off
# contract at every team boundary + the comms cadence/decision log, define DoD + track progress. Decides
# who-builds-what-in-what-order + how teams hand off; NOT what to build (pm) or how it's designed (architect).
# A legal re-synthesis of public delivery methods (PMBOK WBS/critical-path/change, agile DoD/ceremonies,
# RACI, hand-off mgmt). Born 2026-06-23. Chain: decompose -> assign -> sequence -> hand-offs/comms -> DoD/track.
register(CartridgeSpec(
    name="delivery",
    scope="delivery-cartridge",
    label="DELIVERY CARTRIDGE — execution orchestration: WBS -> assign teams -> sequence (critical path) -> hand-off contracts + comms -> DoD/track",
    summary="project/delivery management: decompose the plan into phases & work-packages (WBS) -> assign each to the right team/cartridge -> sequence by dependency (critical path) -> define hand-off contracts + comms cadence + decision log -> definition-of-done + progress tracking; orchestrates who builds what in what order and how teams hand off",
    home_dir=f"{_ESTATE_ROOT}/delivery-cartridge/memory",
    skill="",
    refs=[
        "delivery_cartridge:delivery_cartridge_is_execution_orchestration",     # what it is (frame)
        "delivery_cartridge:delivery_decompose_the_plan_into_work_packages",    # §1 WBS
        "delivery_cartridge:delivery_assign_packages_to_the_right_team",        # §2 assign
        "delivery_cartridge:delivery_sequence_by_dependency_critical_path",     # §3 sequence
        "delivery_cartridge:delivery_define_handoffs_and_communication",        # §4 hand-offs/comms
        "delivery_cartridge:delivery_define_done_and_track_progress",           # §5 DoD/track
        "delivery_cartridge:delivery_cartridge_provenance_and_license",         # legal provenance (fires last)
    ],
))

# pm: the FRONT bracket of the build (what/why/for-whom/in-what-order) — the sibling that decides WHETHER
# and WHAT to build, where architect decides HOW. A legal re-synthesis of public PM methods (JTBD, Lean
# Startup, continuous discovery, RICE, MoSCoW, HEART/AARRR, the build-trap) — see the provenance atom. Born
# 2026-06-23. The card chains the PM thinking order: frame the job → discover/validate → prioritize → scope
# the slice → define the metric → build-measure-learn → guard the anti-patterns, framed by what-it-is and
# closed by provenance.
register(CartridgeSpec(
    name="pm",
    scope="pm-cartridge",
    label="PM CARTRIDGE — product judgment: what to build, for whom, why, in what order (JTBD→discovery→RICE→scope→metric→learn)",
    summary="product management judgment: frame the problem (JTBD) -> discover & validate -> prioritize (RICE) -> scope the smallest viable slice (MoSCoW/MVP) -> define the success metric -> build-measure-learn -> guard the anti-patterns; stays at the decision tier",
    home_dir=f"{_ESTATE_ROOT}/pm-cartridge/memory",
    skill="",
    refs=[
        "pm_cartridge:pm_cartridge_is_product_judgment",        # what it is (frame)
        "pm_cartridge:pm_frame_the_problem_jobs_to_be_done",    # §1 JTBD
        "pm_cartridge:pm_discover_and_validate_before_building", # §2 discovery
        "pm_cartridge:pm_prioritize_ruthlessly_rice",           # §3 RICE
        "pm_cartridge:pm_scope_the_smallest_viable_slice",      # §4 MoSCoW/MVP
        "pm_cartridge:pm_define_the_success_metric",            # §5 north-star/HEART
        "pm_cartridge:pm_build_measure_learn_loop",             # §6 the loop
        "pm_cartridge:pm_guard_against_the_product_anti_patterns", # §7 pre-mortem
        "pm_cartridge:pm_cartridge_provenance_and_license",     # legal provenance (fires last)
    ],
))

# qa: the BACK bracket of the build (does-it-work / where-does-it-break / prove-it) — the verifier of what
# the architect designed and the build produced. A legal re-synthesis of public testing methods (ISTQB,
# risk-based testing, equivalence/boundary analysis, the test pyramid, the oracle problem, exploratory
# testing, ISO 25010, CI gates) — see the provenance atom. Born 2026-06-23. The card chains the QA order:
# risk strategy → case design → pyramid → oracle → exploratory → CI gate, framed by what-it-is + provenance.
register(CartridgeSpec(
    name="qa",
    scope="qa-cartridge",
    label="QA CARTRIDGE — proving it works: risk strategy → case design → pyramid → oracle → exploratory → CI gate",
    summary="quality assurance: risk-based test strategy -> equivalence/boundary case design -> test pyramid -> define the oracle -> exploratory testing -> automated CI quality gate; finds where it breaks and PROVES it works, doesn't fix",
    home_dir=f"{_ESTATE_ROOT}/qa-cartridge/memory",
    skill="",
    refs=[
        "qa_cartridge:qa_cartridge_is_proving_it_works",        # what it is (frame)
        "qa_cartridge:qa_risk_based_test_strategy",             # §1 risk
        "qa_cartridge:qa_design_cases_equivalence_and_boundary", # §2 case design
        "qa_cartridge:qa_shape_the_test_pyramid",               # §3 pyramid
        "qa_cartridge:qa_define_the_oracle",                    # §3.5 oracle
        "qa_cartridge:qa_exploratory_test_the_unknowns",        # §4 exploratory
        "qa_cartridge:qa_automate_into_a_ci_quality_gate",      # §5 CI gate
        "qa_cartridge:qa_cartridge_provenance_and_license",     # legal provenance (fires last)
    ],
))

# ops: the RUN-IT bracket — integrate with 3rd-party systems, deploy, observe, recover. The lens that asks
# "will it run/integrate/survive in production, cheaply and reversibly" (vs architect=how, pm=worth-it,
# qa=provable). A legal re-synthesis of public ops/integration methods (12-factor, anti-corruption layer,
# at-least-once/idempotency, SRE observability/SLOs/runbooks, graceful degradation/circuit-breakers/flags)
# — see the provenance atom. Born 2026-06-23. Chain: vendor contract → thin adapter → idempotent delivery →
# secrets/config per env → observability+runbook → failure/rollback, framed by what-it-is + provenance.
register(CartridgeSpec(
    name="ops",
    scope="ops-cartridge",
    label="OPS CARTRIDGE — running it in the real world: 3rd-party integration + deploy + observe + recover",
    summary="operations & integration: pick the vendor's stable contract -> thin anti-corruption adapter -> idempotent at-least-once delivery -> secrets/config per environment -> observable + runbook -> failure & rollback; decides the operational tech and the seams",
    home_dir=f"{_ESTATE_ROOT}/ops-cartridge/memory",
    skill="",
    refs=[
        "ops_cartridge:ops_cartridge_is_running_it_in_the_real_world",   # what it is (frame)
        "ops_cartridge:ops_integrate_at_the_vendors_stable_contract",    # §1 the contract
        "ops_cartridge:ops_thin_anti_corruption_adapter",               # §2 the adapter
        "ops_cartridge:ops_guarantee_delivery_idempotency_and_retries", # §3 delivery
        "ops_cartridge:ops_secrets_and_config_per_environment",         # §4 secrets/config
        "ops_cartridge:ops_observable_with_a_runbook",                  # §5 observability
        "ops_cartridge:ops_plan_failure_and_rollback",                  # §6 failure/rollback
        "ops_cartridge:ops_cartridge_provenance_and_license",           # legal provenance (fires last)
    ],
))

# interface-build (retired 2026-07-31): merged into frontend-build as the optional
# "Template-Sourcing Entry Path" (3 atoms: template-audit, seam-mapping, ui-state-machine).
# The alias in _ALIASES redirects `cartridge equip interface-build` → frontend-build.

register(CartridgeSpec(
    name="brainstorm",
    scope="brainstorm-council",
    label="BRAINSTORM CARTRIDGE — a multi-model council: options/defect/honesty/decision/chair",
    summary="a multi-model deliberation council (options/defect/honesty/decision/chair), the decision organ",
    home_dir=f"{_ESTATE_ROOT}/research/_brainstorm_cartridge",
    skill="",
    refs=[
        "brainstorm_council:brainstorm_options_seat_role",
        "brainstorm_council:brainstorm_defect_seat_role",
        "brainstorm_council:brainstorm_honesty_seat_role",
        "brainstorm_council:brainstorm_decision_seat_role",
        "brainstorm_council:brainstorm_chair_seat_role",
        "echelon:brainstorm_is_a_multi_model_council_cartridge",
    ],
))

register(CartridgeSpec(
    name="scribe",
    scope="scribe",
    label="SCRIBE CARTRIDGE — dispatch a precise model to read code and write accurate docs",
    summary="read code -> write accurate docs (precise model, quote real def lines, verify the file on disk)",
    home_dir=f"{_ESTATE_ROOT}/research/_scribe_cartridge",
    skill="",
    refs=[
        "scribe:scribe_discipline",
    ],
))

# act-ready: the OPERATING half of boot (warm-up primes RECALL; this primes OPERATING). Its atoms live in
# scope `echelon` (the doctrine is the substrate's own, not a separate scope) — a cartridge composes across
# scopes, so the home scope is just where its card is filed; the refs name the doctrine atoms wherever they sit.
register(CartridgeSpec(
    name="act-ready",
    scope="echelon",
    label="ACT-READY CARTRIDGE — the operating doctrine: act like ECHELON, not just know the estate",
    summary="the operating half of boot — convene-council / harvest-where-you-lack / classify-before-outward / act-don't-ask / witness-through-door",
    home_dir="",   # doctrine atoms are in scope echelon, already planted
    skill="act-ready",
    refs=[
        "echelon:act_ready_is_the_operating_half_of_boot",
        "echelon:warmup_primes_knowing_not_operating",
        # the YAGNI ladder rides the boot-conduct chain: write the least code, BY DEFAULT, from the
        # first edit of every session (ponytail-adopted; safety carve-outs never cut).
        "swarm_pipeline:yagni_ladder",
    ],
))

# yagni: the LAZIEST-SENIOR-DEV card on its own — equip it to hold the ladder for a code-heavy session, and
# it also rides the act-ready (boot) chain by default. Adopted from ponytail; counterweight to swarm bloat.
register(CartridgeSpec(
    name="yagni",
    scope="swarm-pipeline",
    label="YAGNI LADDER — write the least code that works (need it? stdlib? native? dep? one line? then minimum); safety carve-outs never cut",
    summary="the laziest-senior-dev discipline (ponytail-adopted): climb the ladder before writing code; chained into boot via act-ready so every session writes minimal scoped changes",
    home_dir=f"{_ESTATE_ROOT}/swarm-pipeline",
    skill="",
    refs=["swarm_pipeline:yagni_ladder"],
))

# swarm: the PIPELINE cartridge — a CHAIN card (owner 2026-06-21: "swarm first, then branching to subject").
# The trunk (fan-out/partition/leaf-split/verify) is general; the head (ui/code/scribe/qc) is the subject.
# Engine code: swarm_subject.py (trunk) + swarm_heads.py (heads). This card teaches when+how to reach for it.
register(CartridgeSpec(
    name="swarm",
    scope="swarm-pipeline",
    label="SWARM CARTRIDGE — fan-out trunk → branch to a subject head (ui/code/scribe/qc); collision-safe, leaf-split, verified",
    summary="the swarm pipeline: a general trunk (no-cap fan-out / collision-partition / leaf-split / verify-it-ran) that branches to a subject head — ui first, earned on a real UX audit→fix run",
    home_dir=f"{_ESTATE_ROOT}/swarm-pipeline",
    skill="",
    refs=[
        "swarm_pipeline:swarm_trunk_then_branch",       # the chain shape
        "swarm_pipeline:swarm_fanout_no_cap",           # trunk 1
        "swarm_pipeline:swarm_collision_partition",     # trunk 2
        "swarm_pipeline:swarm_leaf_split_on_overflow",  # trunk 3
        "swarm_pipeline:swarm_verify_it_ran_not_taste", # trunk 4
        "swarm_pipeline:swarm_ui_head",                 # the first subject branch
        "swarm_pipeline:swarm_code_head",               # the second subject branch (code review)
        "swarm_pipeline:swarm_writer_tier_flash_lite_banned",  # the writer-tier LAW + the test-gate-or-abandon law
    ],
))

# software-house: the PIPELINE ORCHESTRATOR — not a phase, the LAW that binds the phase cartridges into one
# app-dev arc (engagement→pm→ux→architect→delivery→build→qa→ops). Equip it to BE the software house on ANY
# project without re-instructing the method: the arc self-surfaces on build-shaped intent, the gates fire
# (pm front / qa back / never-skip-a-phase), and the growth-rule keeps the roster born-able. Its refs cross
# every phase scope (like act-ready/ux already do) — it composes the whole house, the phases stay their own
# cartridges. Born 2026-06-23 (owner: make the software-house method a portable cartridge, not a re-instruction).
# Chain: the 3 LAW atoms (orchestrator frame / the gates / the growth-rule) anchoring each phase's frame atom
# in firing order.
register(CartridgeSpec(
    name="software-house",
    scope="software-house-cartridge",
    label="SOFTWARE-HOUSE CARTRIDGE — the pipeline orchestrator: engagement→pm→ux→architect→delivery→build→qa→ops, with the gates + the growth-rule",
    summary="the full app-dev arc as ONE portable capability: bind the phase cartridges into the pipeline (never skip a phase; pm gates the front, qa gates the back), and grow the roster by the born-when-a-real-gap-shows rule; equip to BE the software house on any project",
    home_dir=f"{_ESTATE_ROOT}/software-house-cartridge/memory",
    skill="",
    refs=[
        # the LAW (the orchestrator's own atoms)
        "software_house_cartridge:software_house_cartridge_is_the_pipeline_orchestrator",     # the frame: the arc + which lenses cross-cut
        # the front of the arc, in firing order (each phase's frame atom anchors the transition)
        "engagement_cartridge:engagement_cartridge_is_the_client_front_door",                 # 1 engagement
        "pm_cartridge:pm_cartridge_is_product_judgment",                                      # 2 pm  (FRONT gate)
        "ux_cartridge:ux_architect_role",                                                     # 3 ux  (surface)
        "architect_cartridge:architect_cartridge_is_pure_planning",                           # 4 architect (structure)
        "delivery_cartridge:delivery_cartridge_is_execution_orchestration",                   # 5 delivery (sequence)
        "swarm_pipeline:yagni_ladder",                                                        # 6 build — what: least code that works
        "software_house_cartridge:software_house_build_delegation_law",                       # 6 build — HOW: orchestrator dispatches, never hand-writes
        "software_house_cartridge:software_house_floor_model_is_the_worker",                  # 6 build — WHO: cheap floor model types, expensive model gates
        "qa_cartridge:qa_cartridge_is_proving_it_works",                                      # 7 qa  (BACK gate)
        "ops_cartridge:ops_cartridge_is_running_it_in_the_real_world",                        # 8 ops (run it)
        # the gates + the growth-rule (fire after the arc is laid, so the law lands last)
        "software_house_cartridge:software_house_never_skip_a_phase_pm_gates_front_qa_gates_back",
        "software_house_cartridge:software_house_a_new_cartridge_is_born_when_a_real_gap_shows",
    ],
))

# partner: the CULMINATION capability — hand an equipped peer ONE sentence, it ACTS, you verify the OUTCOME.
# The refs chain the culmination atom + the hard-won dispatch DISCIPLINE the arc earned (watch it, async from
# the entry call, swarm the leaf not the estate, keep equipping simpler not harder).
register(CartridgeSpec(
    name="partner",
    scope="echelon",
    label="PARTNER CARTRIDGE — dispatch an equipped peer on one sentence; verify the outcome, not the diffs",
    summary="the culmination skill — one sentence -> equipped peer acts -> verify the outcome; with the dispatch discipline (watch it / async from entry / swarm the leaf / equip simpler)",
    home_dir="",   # all refs in scope echelon
    skill="partner",
    refs=[
        "echelon:partner_is_the_culmination_skill",
        "echelon:watch_the_dispatch_dont_fire_and_forget",
        "echelon:async_inherited_from_the_entry_call",
        "echelon:swarm_the_independent_unit_not_the_estate",
        "echelon:equipping_simpler_not_harder",
    ],
))

# ui-audit: audit OS pages for broken DOM references, orphan functions, dead CSS,
# and duplicated code after page splits or refactors. Born from the supplier.html
# split (2026-06-24) where a sed range accidentally deleted #tab-orders and
# openLinkModal landed in the wrong page.
register(CartridgeSpec(
    name="ui-audit",
    scope="ui-audit-cartridge",
    label="UI-AUDIT CARTRIDGE — cross-reference DOM, functions, CSS, and dedupe after page splits",
    summary="audit OS pages: 1.DOM refs 2.function chain 3.CSS 4.dedupe 5.intent-action 6.state/empty/error 7.nav reachability 8.pattern consistency 9.accessibility floor",
    home_dir=f"{_ESTATE_ROOT}/research/_ui_audit_cartridge",
    skill="",
    refs=[
        "ui_audit_cartridge:dom_reference_check",
        "ui_audit_cartridge:function_call_chain",
        "ui_audit_cartridge:css_selector_audit",
        "ui_audit_cartridge:dedupe_scan",
        "ui_audit_cartridge:intent_action_mapping",
        "ui_audit_cartridge:state_flow_integrity",
        "ui_audit_cartridge:nav_reachability",
        "ui_audit_cartridge:pattern_consistency",
        "ui_audit_cartridge:accessibility_floor",
    ],
))

# <estate>-boot-procedures: the UNIFORM per-estate boot cartridge (owner 2026-06-25: "make it uniform,
# <estate>-boot-procedures, that cover the boot and list of procedures"). ONE card per estate that chains,
# in firing order: (A) the ECHELON BOOT DOCTRINE (how to BE echelon — the recall-routing law, atoms/cards,
# the tools, scopes, the warm-up→act-ready→add-memory→wrap→relive→cartridge arc) sourced from scope
# `echelon` so every estate's boot card shares the SAME doctrine head; then (B) THIS ESTATE'S PROCEDURE LIST
# (the mol how-tos — change-workflow, the deploy map, prelim hot-patch, the atlas-sync law, the hot-patch
# tool, broker provision, the scanner rules) from scope `mol`. Equip it to wake holding both the doctrine
# AND the estate's playbooks foveated on the goal. A capability composes across scopes (refs name the home
# scope of each atom); the cartridge's own `scope` is just where its card is filed. This is the mol instance
# of the uniform pattern — clone it per estate by swapping the (B) procedure refs.
register(CartridgeSpec(
    name="mol-boot-procedures",
    scope="mol",
    label="MOL-BOOT-PROCEDURES — the boot doctrine (how to BE echelon) + the mol estate's procedure list, in firing order",
    summary="uniform per-estate boot cartridge: ECHELON boot doctrine (recall-routing/atoms-cards/tools/scopes/warm-up→act-ready→wrap→relive→cartridge) THEN the mol procedure list (change-workflow / deploy-map / prelim-hotpatch / atlas-sync law / hotpatch tool / broker provision / scanner rules)",
    home_dir="",   # refs cross scopes echelon + mol, all already planted
    skill="",
    refs=[
        # ── (A) THE ECHELON BOOT DOCTRINE (scope echelon) — how to BE echelon, in boot order
        "echelon:echelon_recall_is_the_door",              # the door: query an intent
        "echelon:echelon_reflex_vs_think",                 # the routing law: warm→reflex / cold→think
        "echelon:echelon_atoms_and_cards",                 # the two units
        "echelon:echelon_the_tools",                       # the 8 echelon_* tools
        "echelon:echelon_scopes_and_home",                 # scope partitioning
        "echelon:echelon_warm_up_before_work",             # warm up before load-bearing action
        "echelon:act_ready_is_the_operating_half_of_boot", # the operating half
        "echelon:warmup_primes_knowing_not_operating",     # why both halves
        "echelon:echelon_how_to_add_memory",               # write an atom
        "echelon:echelon_wrap_to_close_the_loop",          # wrap to close the loop
        "echelon:wrap_is_the_session_close_out",           # wrap = the close-out verb
        "echelon:echelon_relive_resumes_a_session",        # relive resumes a session
        "echelon:echelon_cartridge_is_the_root_dream",     # the cartridge unit (this thing)
        # ── (B) THE MOL PROCEDURE LIST (scope mol) — this estate's how-tos, in use order
        "mol:change_workflow_playbook",                    # the step-by-step for any 3-repo change
        "mol:estate_deploy_map_and_howto",                 # canonical LIVE port/path map for all 6 services
        "mol:preliminary_deploy_to_box",                   # prelim hot-patch workflow (incl. the _cfg.json law)
        "mol:atlas_sync_law_and_wrap_phase",               # the atlas-sync law + wrap Phase 1.5
        "mol:hotpatch_deployer_tool",                      # the reusable multi-repo hot-patch tool
        "mol:broker_provision_via_gh_action",              # provision a secret into the broker
        "mol:scanner_enforced_rules",                      # the boot-time scanner rules DE + Prelim enforce
    ],
))

# opt-cartridge: the OPTIMIZATION discipline — profile→bottleneck→cheapest-fix→measure→no-regressions.
# Born 2026-06-26 from the recall performance work. Prevents: guessing the bottleneck, caching
# the wrong thing, over-engineering the fix, claiming done without measuring.
register(CartridgeSpec(
    name="opt",
    scope="opt-cartridge",
    label="OPT CARTRIDGE — performance optimization discipline: profile → bottleneck → cheapest fix first → measure after → no regressions",
    summary="performance optimization: profile don't guess -> find the real bottleneck -> fix the cause not the symptom -> cheapest fix first (remove dead work/index/query shape/batch/denormalize/algorithm/cache LAST) -> measure after -> verify no regressions",
    home_dir=f"{_ESTATE_ROOT}/opt-cartridge/memory",
    skill="",
    refs=[
        "opt_cartridge:opt_cartridge_frame",             # the frame: what this cartridge IS
        "opt_cartridge:opt_profile_dont_guess",          # §1 profile first, find the REAL bottleneck
        "opt_cartridge:opt_fix_cause_not_symptom",       # §2 fix the cause, don't cache symptoms
        "opt_cartridge:opt_cheapest_fix_first",          # §3 climb the ladder, stop when fast enough
        "opt_cartridge:opt_measure_after",               # §4 re-measure, find the next bottleneck
        "opt_cartridge:opt_no_regressions",              # §5 verify existing tests pass
    ],
))

# intent-cartridge: the CODE HONESTY discipline — does the code honestly do what its
# names, docstrings, and claims say it does? Born 2026-06-26 from catching the score-cache
# mismatch and the FTS dead-claim. Prevents: dead claims (code that silently fails),
# over-loaded functions (does more than name claims), wrong ID schemes (v1 vs v2).
register(CartridgeSpec(
    name="intent",
    scope="intent-cartridge",
    label="INTENT CARTRIDGE — code honesty audit: does the code honestly do what its name/docstring/claim says?",
    summary="code honesty: read function names as contracts -> trace implementation against claims -> check ID bridges (v1/v2) -> test known inputs (dead claim detection) -> flag gaps (over-loaded/dead-claim/wrong-ID/silent-no-op)",
    home_dir=f"{_ESTATE_ROOT}/intent-cartridge/memory",
    skill="",
    refs=[
        "intent_cartridge:intent_cartridge_frame",       # the frame: what this cartridge IS
        "intent_cartridge:intent_honest_function_names",  # §1 names are contracts — code must match
        "intent_cartridge:intent_dead_code_claims",       # §2 dead claims: code that silently fails
        "intent_cartridge:intent_read_the_intent",        # §3 read the exact intent (user's words)
        "intent_cartridge:intent_trace_implementation",   # §4 trace every change against intent
    ],
))

# motion-craft: the MOTION/ANIMATION knowledge corpus — a standalone cartridge ported from Emil Kowalski's
# design engineering philosophy (MIT). Its 8 laws govern WHEN to animate (frequency gate), HOW (durations +
# easing + physicality + interruptibility), what to DELETE (remedial hierarchy), and the performance traps
# that drop frames. A 9th general-doctrine atom (rejected-candidates law) lives here because the anti-slop
# device was born in find-animation-opportunities. Referenced by polish-and-gate, ux-taste, render-judge-gate,
# and blueprint — each pulls the specific laws it needs through the cartridge chain.
# Born 2026-07-31 (port from emilkowalski/skills).
register(CartridgeSpec(
    name="motion-craft",
    scope="motion-craft-cartridge",
    label="MOTION-CRAFT CARTRIDGE — the motion/animation knowledge corpus: frequency gate -> duration + easing law -> physicality -> interruptibility -> performance traps -> apple spring constants -> remedial hierarchy -> rejected-candidates law",
    summary="UI motion as a craft discipline: WHEN to animate (frequency gate + deletion posture) -> HOW (durations + 3 named cubic-beziers + physicality + interruptibility) -> PERFORMANCE TRAPS (only transform/opacity, FM-shorthand, recalc-storm, blur-cap) -> APPLE SPRING CONSTANTS (damping/response triples, momentum projection, materials, tracking) -> REMEDIAL HIERARCHY (delete > reduce > fix easing > fix origin > interruptible > GPU > asymmetric > polish > a11y) -> REJECTED-CANDIDATES LAW (any findings output MUST include what was considered and refused)",
    home_dir=f"{_ESTATE_ROOT}/motion-craft-cartridge/memory",
    skill="",
    refs=[
        "motion_craft_cartridge:motion_craft_frame",            # the frame: what this cartridge IS
        "motion_craft_cartridge:motion_frequency_gate",         # §1 the frequency gate (the crown jewel)
        "motion_craft_cartridge:motion_duration_easing_law",    # §2 duration table + 3 named cubic-beziers + ease-in banned
        "motion_craft_cartridge:motion_physicality_law",        # §3 never scale(0), origin from trigger, press at 0.95-0.98
        "motion_craft_cartridge:motion_interruptibility_law",   # §4 transitions retarget, keyframes restart, @starting-style, asymmetric
        "motion_craft_cartridge:motion_performance_traps",      # §5 transform/opacity only, FM-shorthand, recalc-storm, blur<20px
        "motion_craft_cartridge:motion_apple_spring_constants", # §6 damping/response triples, momentum projection, materials, tracking
        "motion_craft_cartridge:motion_remedial_hierarchy",     # §7 delete > reduce > fix easing > fix origin > interruptible > GPU > asymmetric > polish > a11y
        "motion_craft_cartridge:rejected_candidates_law",       # §8 general ECHELON doctrine — any output MUST name what it refused
    ],
))

# ── THE LIFECYCLE ROSTER — the 6 on-demand jobs a dev/agent reaches for (born 2026-07-01, owner: make
# cartridges a COMPLETE roster of equip-by-name capabilities for recurring jobs). security + review are
# DISTILLED from the harness /security-review + /code-review skills (their discipline, not a generic
# checklist); refactor/slim/debug/migrate from the craft, each cross-linking the existing atoms it extends.

# security: FIND VULNERABILITIES / harden — distilled from the /security-review skill. Its discipline:
# scope to the diff, research the codebase's own security model, trace user-input→sink, walk the fixed
# category taxonomy, filter false positives HARD (>80% confidence + the exclusion list), report
# exploitable-only with a concrete exploit scenario. The security sibling of `review` (same diff-scoped,
# confidence-gated review discipline, pointed at attackers instead of bugs).
register(CartridgeSpec(
    name="security",
    scope="security-cartridge",
    label="SECURITY CARTRIDGE — find vulnerabilities / harden: scope-to-diff → research the model → trace input→sink → assess by category → filter false-positives → report exploitable-only",
    summary="security review (distilled from /security-review): review only the diff's new attack surface -> research the codebase's own auth/sanitize patterns -> trace attacker-controllable input to dangerous sinks -> assess by taxonomy (injection/authz/crypto/code-exec/data-exposure) -> filter false positives hard (>80% + exclusion list) -> report exploitable-only with a concrete exploit scenario",
    home_dir=f"{_ESTATE_ROOT}/security-cartridge/memory",
    skill="",
    refs=[
        "security_cartridge:security_cartridge_frame",             # the frame: what this cartridge IS
        "security_cartridge:security_scope_to_the_diff",           # §1 review the new surface, not the repo
        "security_cartridge:security_research_the_model",          # §2 learn the codebase's security patterns
        "security_cartridge:security_trace_user_input_to_sink",    # §3 input → sink data-flow
        "security_cartridge:security_assess_by_category",          # §4 the fixed taxonomy
        "security_cartridge:security_filter_false_positives",      # §5 exclusion list + >80% bar
        "security_cartridge:security_report_exploitable_only",     # §6 file:line/severity/exploit/fix
    ],
))

# review: CORRECTNESS-GATE a DIFF/PR — distilled from the /code-review skill + pr-review-toolkit agents.
# Its discipline: scope the diff, load the project rules (CLAUDE.md + comments), a multi-lens pass
# (rules/bugs/history/prior-PRs/comments), a dedicated silent-failure hunt, confidence-score ≥80 (filter
# aggressively), report cited-and-actionable. The correctness sibling of `security`.
register(CartridgeSpec(
    name="review",
    scope="review-cartridge",
    label="REVIEW CARTRIDGE — correctness-gate a diff/PR: scope the diff → load project rules → multi-lens pass → hunt silent failures → score ≥80 → report cited & actionable",
    summary="code review (distilled from /code-review): review the diff not the repo -> load the CLAUDE.md + comment rules -> multi-lens pass (rules/bugs/git-history/prior-PRs/comments) -> hunt silent failures (empty catch/broad-catch/unjustified fallback) -> score each finding 0-100 keep only >=80 -> report cited & actionable, no nitpicks",
    home_dir=f"{_ESTATE_ROOT}/review-cartridge/memory",
    skill="",
    refs=[
        "review_cartridge:review_cartridge_frame",                 # the frame: what this cartridge IS
        "review_cartridge:review_scope_the_diff",                  # §1 the change, not the repo
        "review_cartridge:review_load_the_project_rules",          # §2 CLAUDE.md + comments = the contract
        "review_cartridge:review_multi_lens_pass",                 # §3 rules/bugs/history/PRs/comments
        "review_cartridge:review_hunt_silent_failures",            # §4 the error-handling specialist lens
        "review_cartridge:review_score_and_filter",                # §5 confidence >=80, filter hard
        "review_cartridge:review_report_cited_and_actionable",     # §6 cite the rule/bug, give the fix
    ],
))

# refactor: RESTRUCTURE WITHOUT behavior change — the craft discipline. Pin behavior with green tests
# (the safety net, leans on qa), make-the-change-easy (Kent Beck), smallest reversible step (extract/
# rename/inline/move), keep tests green after EACH step, commit at every green. Distinct from slim
# (removes weight) and opt (changes performance) — refactor must not change observable behavior.
register(CartridgeSpec(
    name="refactor",
    scope="refactor-cartridge",
    label="REFACTOR CARTRIDGE — restructure without behavior change: pin behavior (green tests) → make-the-change-easy → smallest reversible step → green after each → commit at every green",
    summary="restructure code WITHOUT changing behavior: pin the current behavior with a green test net -> make the change easy then make the easy change (Kent Beck) -> take the smallest reversible step (extract/rename/inline/move) -> keep tests green after every step -> commit at every green; behavior is provably identical before and after",
    home_dir=f"{_ESTATE_ROOT}/refactor-cartridge/memory",
    skill="",
    refs=[
        "refactor_cartridge:refactor_cartridge_frame",             # the frame: what this cartridge IS
        "refactor_cartridge:refactor_pin_behavior_with_tests",     # §1 the green safety net (leans on qa)
        "refactor_cartridge:refactor_make_the_change_easy",        # §2 Kent Beck's law
        "refactor_cartridge:refactor_smallest_reversible_step",    # §3 one named transformation at a time
        "refactor_cartridge:refactor_keep_tests_green",            # §4 run the suite after each step
        "refactor_cartridge:refactor_commit_at_every_green",       # §5 bank each green as a save-point
    ],
))

# slim: DE-BLOAT / remove dead weight — EXTENDS yagni_ladder (yagni stops bloat being written; slim
# removes bloat already there) + opt's "remove dead work first". Prove it dead → delete dead code →
# prune unused deps → collapse premature abstraction → dedupe divergent copies → verify green + smaller.
register(CartridgeSpec(
    name="slim",
    scope="slim-cartridge",
    label="SLIM CARTRIDGE — de-bloat / remove dead weight: prove-it-dead → delete dead code → prune unused deps → collapse premature abstraction → dedupe copies → verify green + smaller",
    summary="de-bloat (extends yagni_ladder + opt's remove-dead-work-first): prove code unreachable before cutting -> delete dead code (incl. shipped-flag corpses) -> prune unused dependencies -> collapse premature abstraction (single-impl interfaces) -> dedupe divergent copies into one source of truth -> verify the suite still green AND the count measurably dropped; behavior-preserving",
    home_dir=f"{_ESTATE_ROOT}/slim-cartridge/memory",
    skill="",
    refs=[
        "slim_cartridge:slim_cartridge_frame",                     # the frame: what this cartridge IS
        "slim_cartridge:slim_prove_it_dead_first",                 # §1 prove unreachable before cutting
        "slim_cartridge:slim_delete_dead_code",                    # §2 dead code + flag corpses (opt rung 1)
        "slim_cartridge:slim_prune_unused_dependencies",           # §3 deps as liability (yagni backwards)
        "slim_cartridge:slim_collapse_premature_abstraction",      # §4 inline the one-impl seam
        "slim_cartridge:slim_dedupe_divergent_copies",             # §5 one source of truth
        "slim_cartridge:slim_verify_green_and_smaller",            # §6 prove safe AND effective
    ],
))

# debug: ROOT-CAUSE a failure — the SCIENTIFIC METHOD applied to code, anti-guessing. Reproduce reliably
# → isolate by bisection → form ONE falsifiable hypothesis → test it → fix the CAUSE not the symptom →
# prove the fix flips behavior (+ regression test). Shares fix-cause-not-symptom with opt.
register(CartridgeSpec(
    name="debug",
    scope="debug-cartridge",
    label="DEBUG CARTRIDGE — root-cause a failure (scientific method): reproduce → bisect/isolate → falsifiable hypothesis → test it → fix the CAUSE → prove the fix flips behavior",
    summary="root-cause a failure (scientific method, anti-guessing): reproduce reliably (deterministic repro first) -> isolate by bisection (git bisect / binary-search input / disable half) -> form ONE falsifiable hypothesis -> test it (observe, don't change) -> fix the CAUSE not the symptom -> prove the fix flips behavior (fails without, passes with) + lock it with a regression test",
    home_dir=f"{_ESTATE_ROOT}/debug-cartridge/memory",
    skill="",
    refs=[
        "debug_cartridge:debug_cartridge_frame",                   # the frame: what this cartridge IS
        "debug_cartridge:debug_reproduce_reliably",                # §1 deterministic repro first
        "debug_cartridge:debug_isolate_by_bisection",              # §2 halve the search space
        "debug_cartridge:debug_form_falsifiable_hypothesis",       # §3 one testable claim
        "debug_cartridge:debug_test_the_hypothesis",               # §4 observe, let the result decide
        "debug_cartridge:debug_fix_cause_not_symptom",             # §5 patch the cause (like opt)
        "debug_cartridge:debug_prove_the_fix_flips_behavior",      # §6 fails-without/passes-with + regression
    ],
))

# migrate: deps/version/data/framework UPGRADE — the law is NEVER big-bang. Pin the current state →
# read the breaking changes → shim the seam (anti-corruption adapter) → migrate incrementally behind a
# flag → verify parity (old vs new) → remove the shim (slim the old path). Reuses ops' adapter + qa's oracle.
register(CartridgeSpec(
    name="migrate",
    scope="migrate-cartridge",
    label="MIGRATE CARTRIDGE — deps/version/data/framework upgrade (never big-bang): pin current → read breaking changes → shim the seam → incremental behind a flag → verify parity → remove the shim",
    summary="upgrade deps/version/data/framework — the law is NEVER big-bang: pin the current state (exact versions + behavior baseline) -> read the changelog/breaking-changes -> shim the seam (anti-corruption adapter) -> migrate incrementally behind a runtime flag (always shippable/revertible) -> verify parity (new matches old on real inputs) -> remove the shim/flag/old path when stable",
    home_dir=f"{_ESTATE_ROOT}/migrate-cartridge/memory",
    skill="",
    refs=[
        "migrate_cartridge:migrate_cartridge_frame",               # the frame: what this cartridge IS
        "migrate_cartridge:migrate_pin_the_current_state",         # §1 lock versions + behavior baseline
        "migrate_cartridge:migrate_read_breaking_changes",         # §2 the changelog is the map
        "migrate_cartridge:migrate_shim_the_seam",                 # §3 anti-corruption adapter (like ops)
        "migrate_cartridge:migrate_incrementally_behind_a_flag",   # §4 one slice at a time, never big-bang
        "migrate_cartridge:migrate_verify_parity",                 # §5 old vs new on real inputs (qa oracle)
        "migrate_cartridge:migrate_remove_the_shim",               # §6 delete old path/flag/shim (slim)
    ],
))
