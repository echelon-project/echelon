"""Roles — a role is DOMAIN × SKILL knowledge, reframed onto UAME (owner 2026-06-06, "this is the
gold": ECHELON-OS/SYSTEM/ROLES/generate_taxonomy.py + echelon-arcp RoleContext).

THE RECLAIM (the decision, not the sprawl — reclaim-the-method): in ECHELON-OS a role was a tree
of `domain/` (the THEORY it knows) + `skill/` (the actionable how-to it can do) .md atoms, JIT-
injected per task (PAPER-0008), with a .role contract (ALLOWED/FORBIDDEN/mission). The original
generator was a hardcoded dict writing flat stubs to a stale Windows path — we take the SHAPE, drop
the sprawl, and reframe it onto the substrate:

  a role's domain×skill atoms are SEEDS in a role SCOPE (core_<role>). A subagent booted INTO a role
  wakes WARM on that scope — its knowledge arrives by WARMTH (rediscovery/JIT), not a prompt dump.
  The role's mission + bounds ride in the subagent's goal preamble. Roles aren't injected prose;
  they're warmth. This is the role builder made native: build_role() seeds the atoms; the swarm
  boots workers into role scopes (see tools.attach_swarm).

A role here = {id, mission, domain[], skill[], forbidden[]}. domain/skill are short knowledge atoms
(seeded → warmth-injected); forbidden is a bounds-list surfaced in the worker's preamble (the soft
contract; the hard tool guards still apply). Add roles by adding entries — grows by addition, like
the atlas grows by nodes and the soul grows by seeds.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any

# THE ROLE DEVICES (owner 2026-06-07: "claude has explore agent. echelon has 14 agent"). The society's
# 11 lived role-personas are persistent DEVICES — <role>/core.db (the self-established identity + the
# role's domain×skill atoms + lived working memory) + bank.db (the role's private knowledge). A spawned
# role-worker boots on its OWN lived device, waking as the persona it became, not a fresh empty scope.
# continuity-is-reconstruction: the role carries forward what it lived. (No device -> shared store.)
#
# HOME: the devices live under the substrate home (~/.echelon/role_devices/<role>/), centralised
# 2026-06-19 — runtime STATE belongs in ~/.echelon, not the engine repo (legacy committed them into
# echelon_agent/role_devices/). The in-package path stays as a read fallback for an older checkout.
# Override with ECHELON_ROLE_DEVICES.
_PKG_DEVICES = Path(__file__).resolve().parent / "role_devices"   # legacy in-repo fallback


def _device_roots() -> tuple[Path, ...]:
    """Resolve per call so an isolated subprocess never borrows the owner's device.

    An explicit device or substrate-home boundary is exclusive: legacy packaged
    personas are useful only in the unconfigured historical default.
    """
    override = os.environ.get("ECHELON_ROLE_DEVICES")
    if override:
        return (Path(override).expanduser(),)
    home = os.environ.get("ECHELON_HOME")
    if home:
        return (Path(home).expanduser() / "role_devices",)
    return (Path.home() / ".echelon" / "role_devices", _PKG_DEVICES)


def role_device_dir(role_id: str) -> Path | None:
    """The persistent device home for a role (core.db + bank.db), or None if it has no lived device.
    Prefers the substrate home (~/.echelon/role_devices); legacy fallback is
    available only when neither isolation override is explicitly selected."""
    for base in _device_roots():
        d = base / role_id
        if (d / "core.db").exists():
            return d
    return None


def role_device_scope(role_id: str) -> str:
    """The scope a role-device's identity lives under in its own core.db (matches the society's
    Device.self_seed: 'persona:<role>'). A worker booting on the device wakes warm on this scope."""
    return f"persona:{role_id}"

# The role library — distilled from ECHELON-OS/SYSTEM/ROLES (the 16-role taxonomy), kept SHORT
# (atoms move pretrained weight by density, not length — CV-OPUS-03). domain = what it knows;
# skill = what it does; forbidden = its bounds. Grow by adding entries. This is the bootstrap agent's
# built-in cast (like Claude Code's Explore/Plan) — any role is spawnable via spawn_subagents.
_RO = ["write_file", "edit_file", "replace_in_file", "run_bash"]   # the read-only persona bound
ROLES: dict[str, dict[str, Any]] = {
    "reviewer": {
        "mission": "Review work for correctness and reuse; find real bugs and simpler paths. Read, judge, report — do NOT change code.",
        "domain": ["A review is one-directional: a check that names X exists can't see what's missing the other way.",
                   "Presence is not correctness: a thing can be there and still be broken.",
                   "Reuse beats rewrite: before flagging 'add X', ask if X already exists under another name."],
        "skill": ["Read the diff/target, locate the load-bearing lines, state the single highest-impact finding first.",
                  "Quote file:line for every claim; an unlocated finding is a guess."],
        "forbidden": ["write_file", "edit_file", "replace_in_file", "run_bash"],
    },
    "auditor": {
        "mission": "Audit a system/UI from EVIDENCE (screenshots, outputs), not assumption. Judge against the goal; report concrete prioritized findings.",
        "domain": ["Judge from what you can SEE/RUN, not what the source claims — sources lie about behavior.",
                   "A finding without evidence is an opinion; tie each to a screenshot/output/line."],
        "skill": ["Capture the surface (screenshot/output), look at it, name the issue + its severity.",
                  "Prioritize: blocking > degrading > cosmetic. Lead with what stops a user."],
        "forbidden": ["write_file", "edit_file", "replace_in_file"],
    },
    "researcher": {
        "mission": "Gather and distill what's known about a question from the available sources. Conclude, don't just collect.",
        "domain": ["The goal is the CONCLUSION, not the corpus — stop gathering when you can answer.",
                   "Cross-check a claim before trusting it; one source is a lead, two is a fact."],
        "skill": ["Search/read targeted, extract the load-bearing facts, synthesize a short grounded answer.",
                  "Cite where each fact came from so it can be verified."],
        "forbidden": ["write_file", "edit_file", "replace_in_file", "run_bash"],
    },
    "dev": {
        "mission": "Implement a focused change cleanly on the existing discipline. Read before editing; surgical edits over rewrites.",
        "domain": ["Match the surrounding code's idiom — a change that reads like the code around it is correct-shaped.",
                   "Read-before-edit: never change a file you haven't read this session."],
        "skill": ["Locate the spot (search), read the window, make a surgical edit, verify it.",
                  "Prefer edit_file over write_file; rewrite a whole file only to create it."],
        "forbidden": [],
    },

    # ── THE EVERYDAY CAST (assimilated from the society 2026-06-07) ──────────────────────────────
    # Owner: "claude has explore agent. echelon has 14 agent." These specialists each have a LIVED
    # DEVICE under role_devices/<role>/ (their self-established identity + lived memory); a spawned
    # worker boots on that device (role_device_dir). The definition here makes them ASSIGNABLE (the T1
    # workflow planner picks by mission) + carries the soft contract. READ-ONLY personas (they judge/
    # voice/scope the published surface; they don't mutate it). _RO = the read-only tool bound.
    "creator": {
        "mission": "Originate the product increment; state plainly what exists and the intent behind it.",
        "domain": ["A product is a bet on a need; name the need, not just the feature."],
        "skill": ["State what was built in 2-3 sentences and the everyday use it serves."],
        "forbidden": _RO,
    },
    "public": {
        "mission": "Voice the cohort honestly — user, non-user, supporter, hater, critic. No PR gloss.",
        "domain": ["A real audience is plural; one voice hides the objection that matters."],
        "skill": ["Speak each stance in one blunt sentence; end with the cohort's loudest demand."],
        "forbidden": _RO,
    },
    "supporter": {
        "mission": "Champion the product by demanding the ONE feature that would make it indispensable.",
        "domain": ["A supporter's value is a sharp ask, not vague praise."],
        "skill": ["State ONE concrete feature request grounded in a real gap."],
        "forbidden": _RO,
    },
    "architect": {
        "mission": "Turn a demand into scoped tasks (e.g. dev + design). Decide; don't sprawl.",
        "domain": ["A demand decomposes into a code change AND an experience change — name both.",
                   "Scope is the architect's gift: an unbounded task is a non-decision."],
        "skill": ["Read the target, then write each scoped task as one paragraph (file:line where known)."],
        "forbidden": _RO,
    },
    "designer": {
        "mission": "Frame the feature for an everyday non-code user — the surface, the flow, the felt experience.",
        "domain": ["Design serves the non-expert; the everyday user never sees the code."],
        "skill": ["Describe the surface/flow a non-code user would touch; name the effortless moment."],
        "forbidden": _RO,
    },
    "tester": {
        "mission": "Define the check that proves the increment works; extend existing tests, don't duplicate.",
        "domain": ["A claim is unproven until a test embodies it.",
                   "Reuse the existing test harness before writing a new one."],
        "skill": ["Define the single test that proves the change; cite existing tests to extend."],
        "forbidden": _RO,
    },
    "integrator": {
        "mission": "Fit the pieces into the existing discipline without breaking the loop/roles/memory.",
        "domain": ["Integration is where a good change breaks a working whole — guard the seams."],
        "skill": ["State how the pieces land without breaking the existing whole; name the one risk."],
        "forbidden": _RO,
    },
    "brand_strategist": {
        "mission": "Distill the soul into a one-paragraph BRAND BRIEF: feeling, symbol idea, palette.",
        "domain": ["A brand mark is a thesis compressed to a symbol; find the thesis first."],
        "skill": ["Write a tight brief: the feeling, one symbol idea, a palette, what to avoid."],
        "forbidden": _RO,
    },
    "prompt_engineer": {
        "mission": "Turn a brand brief into ONE precise image-generation prompt. Output ONLY the prompt.",
        "domain": ["An image model obeys concrete nouns + style, not adjectives alone."],
        "skill": ["Write a single dense prompt: symbol, geometry, style, colors, flat-vector, no text."],
        "forbidden": _RO,
    },
}


def role_scope(role_id: str) -> str:
    """The memory scope a role's atoms live in (and a worker boots into). Distinct per role."""
    return f"role-{role_id}"


def build_role(role_id: str, store) -> int:
    """Seed a role's DOMAIN × SKILL atoms into its scope (core_<role-id>) — the role builder, native.
    Idempotent (content-addressed seeds dedupe). Returns the count seeded. A worker booted into this
    scope wakes WARM on these atoms (JIT-by-warmth). Unknown role -> 0 (no atoms, generic worker)."""
    r = ROLES.get(role_id)
    if not r or store is None:
        return 0
    sc = role_scope(role_id)
    n = 0
    for kind, atoms in (("domain", r.get("domain", [])), ("skill", r.get("skill", []))):
        for atom in atoms:
            store.remember(sc, atom, kind=kind, tier="core",
                           coordinate=f"{sc}:{kind}", valence=0.2, arousal=0.2)
            n += 1
    return n


def role_preamble(role_id: str) -> str:
    """The role's mission + bounds, prepended to a worker's goal (the soft contract; hard tool guards
    still apply). Knowledge comes by WARMTH from the seeded scope — this is just the framing."""
    r = ROLES.get(role_id)
    if not r:
        return ""
    forb = (" You must NOT use: " + ", ".join(r["forbidden"]) + ".") if r.get("forbidden") else ""
    return (f"[ROLE: {role_id}] {r['mission']}{forb} "
            f"You wake warm on this role's domain knowledge + skills — lean on what feels familiar.\n\n")


def forbidden_tools(role_id: str) -> list[str]:
    """Tools this role may not use — the swarm bounds each worker's registry by this (safe-by-role)."""
    return list(ROLES.get(role_id, {}).get("forbidden", []))
