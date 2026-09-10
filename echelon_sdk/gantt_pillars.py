"""gantt_pillars — the SOM/TENG refinements, reclaimed onto workflow.py (2026-06-07).

workflow.py already IS the gantt-swarm: compute_waves (TENG's topological sort -> parallel waves),
run_workflow (NICE's wave execution), plan_with_model/_parse_plan (SOM's decompose + agent assign),
make_agent_runner (NICE's dispatch to lived role-devices). The four EROS/EOS pillars, already here.

A 100%-LOC read of the EROS pillars (som/teng/nice/orchestrator + waves) surfaced THREE proven
refinements workflow.py lacks. Per reclaim-the-method (mine the menu, reimplement clean, don't
copy-whole) this module ADDS only those three, as pure functions over the existing workflow dict —
no rewrite of workflow.py:

  1. RE-DECOMPOSE (from SOM.decompose's recurse-til-atomic): split a step whose task is clearly
     compound ("do X and Y and Z") into atomic sub-steps, rewiring depends_on. SOM recursed via an
     LLM; the deterministic floor here splits on explicit conjunctions so a fat step becomes a wave.

  2. GRADE-ORDER WITHIN A WAVE (from TENG._topological_sort: order by deps THEN grade-descending):
     workflow.compute_waves sorts a wave alphabetically (ready.sort()). TENG runs higher-priority
     bars first within a wave. grade_steps() assigns a 1-10 grade + a tier; order_wave() sorts a
     wave by grade desc so the critical bar starts first.

  3. VERIFY (from TENG.verify_result + reality_mode): reject fake success — a step "answer" that is
     a stub/placeholder/empty is not a real completion. verify_step() is the honesty gate the
     orchestrator can apply to each result (the ECHELON form of 'reject fake success').

Deterministic (no LLM) so it's a $0 floor and unit-testable; the LLM-driven SOM/TENG grading is a
later same-interface swap (grade_steps could call a model; verify_step could call TENG). See:
compiler-era-fork-field, dream-and-the-respect-handshake, reclaim-the-method.
"""
from __future__ import annotations

import re

# tier vocabulary (mirrors TENG): which kind of work -> which silicon class
TIER_FAST = "fast"
TIER_CODE = "code_generation"
TIER_VALIDATION = "validation"
TIER_PLANNING = "planning"

_CONJUNCTIONS = (" and ", " then ", " plus ", "; ", " & ")

# keyword -> tier (TENG's heuristic classifier, condensed)
_TIER_KW = [
    (TIER_VALIDATION, ("test", "verify", "validate", "check", "audit", "review", "debug")),
    (TIER_PLANNING, ("design", "architect", "strategy", "refactor", "plan ", "decompose")),
    (TIER_CODE, ("implement", "write", "code", "function", "module", "build", "create", "add")),
    (TIER_FAST, ("analyze", "summary", "extract", "classify", "list", "summarize", "identify", "find")),
]

_COMPLEX_KW = ("architect", "refactor", "redesign", "migrate", "integrate", "orchestrat",
               "pipeline", "framework", "security", "distributed", "end-to-end")
_SIMPLE_KW = ("rename", "typo", "comment", "bump version", "toggle", "remove unused", "move file")


def classify_tier(task: str) -> str:
    """TENG's keyword tier classification for a step's task text."""
    t = task.lower()
    for tier, kws in _TIER_KW:
        if any(kw in t for kw in kws):
            return tier
    return TIER_FAST


def grade_task(task: str, n_deps: int = 0) -> int:
    """Grade 1-10 (TENG): complexity signal + dependency weight. Deterministic floor."""
    t = task.lower()
    words = re.findall(r"[A-Za-z0-9_]+", t)
    score = 5
    if len(words) > 40:
        score += 2
    elif len(words) < 8:
        score -= 1
    score += min(n_deps, 3)
    if any(kw in t for kw in _COMPLEX_KW):
        score += 2
    if any(kw in t for kw in _SIMPLE_KW):
        score -= 2
    score += sum(1 for c in _CONJUNCTIONS if c in t)   # compound = harder
    return max(1, min(10, score))


def grade_steps(wf: dict) -> dict:
    """Annotate every step with a grade (1-10) and a tier, in place-safe copy. TENG's grading pass.
    Returns a new workflow dict; the input is not mutated."""
    steps = []
    for s in wf.get("steps", []):
        s2 = dict(s)
        deps = s.get("depends_on") or []
        s2.setdefault("grade", grade_task(s.get("task", ""), len(deps)))
        s2.setdefault("tier", classify_tier(s.get("task", "")))
        steps.append(s2)
    return {**wf, "steps": steps}


def order_wave(wave_ids: list[str], wf: dict) -> list[str]:
    """Order step-ids WITHIN one wave by grade descending (TENG: critical bar first), stable by id.
    workflow.compute_waves sorts alphabetically; this is the priority refinement applied per wave."""
    by_id = {s["id"]: s for s in wf.get("steps", [])}
    return sorted(wave_ids, key=lambda sid: (-int(by_id.get(sid, {}).get("grade", 5)), sid))


def is_compound(task: str, *, min_clause_words: int = 2) -> bool:
    """A task is compound (re-decompose candidate) when splitting on explicit conjunctions yields
    2+ clauses that each carry real work (>= min_clause_words words). Keying off CLAUSE COUNT, not
    raw length, is SOM's actual intent: "write the parser and add tests and document it" is three
    units of work; "fast and clean" is one phrase. The per-clause word floor rejects throwaway
    joins ("do it and stop")."""
    clauses = _split_compound(task)
    if len(clauses) < 2:
        return False
    real = [c for c in clauses if len(re.findall(r"[A-Za-z0-9_]+", c)) >= min_clause_words]
    return len(real) >= 2


def _split_compound(task: str) -> list[str]:
    """Split a compound task into atomic clauses on explicit conjunctions."""
    pattern = re.compile("|".join(re.escape(c) for c in _CONJUNCTIONS))
    parts = [p.strip() for p in pattern.split(task) if p.strip()]
    return parts if len(parts) > 1 else [task]


def redecompose(wf: dict) -> dict:
    """SOM's recurse-til-atomic, one deterministic pass: replace each COMPOUND step with atomic
    sub-steps (s -> s.1, s.2, ...) run in sequence (each sub depends on the previous), and rewire
    any step that depended on s to depend on the LAST sub-step (s preserves its place in the DAG).
    Returns a new workflow dict; input untouched. Acyclic-preserving."""
    steps = wf.get("steps", [])
    new_steps: list[dict] = []
    last_sub: dict[str, str] = {}   # original id -> id of its last sub-step (the new "completion")

    for s in steps:
        sid = s["id"]
        task = s.get("task", "")
        if is_compound(task):
            clauses = _split_compound(task)
            prev = None
            for i, clause in enumerate(clauses, 1):
                sub_id = f"{sid}.{i}"
                sub = dict(s)
                sub["id"] = sub_id
                sub["task"] = clause
                # first sub inherits the original's external deps; later subs chain on the prior sub
                sub["depends_on"] = list(s.get("depends_on") or []) if prev is None else [prev]
                new_steps.append(sub)
                prev = sub_id
            last_sub[sid] = prev   # whoever depended on sid now depends on the last sub
        else:
            new_steps.append(dict(s))

    if last_sub:   # rewire external dependents to the last sub-step of any split step
        for s in new_steps:
            s["depends_on"] = [last_sub.get(d, d) for d in (s.get("depends_on") or [])]
    return {**wf, "steps": new_steps}


# ── VERIFY (TENG.verify_result, the honesty gate) ─────────────────────────────
_FAKE_MARKERS = ("todo", "not implemented", "placeholder", "stub", "tbd", "fixme",
                 "lorem ipsum", "...", "<your", "xxx")


def verify_step(result: dict) -> dict:
    """Reject fake success (TENG reality verifier, ECHELON floor). A step result is VERIFIED only if
    it actually claims completion AND its answer is not an empty/stub/placeholder. Returns the result
    with 'verified' and (if failed) 'verify_reason' added — the orchestrator decides what to do with
    an unverified step (retry / escalate / mark failed). Does not mutate the input."""
    r = dict(result)
    status = (r.get("status") or "").lower()
    answer = (r.get("answer") or "").strip()
    reasons: list[str] = []

    if status == "error":
        reasons.append("status is error")
    if not answer:
        reasons.append("empty answer")
    low = answer.lower()
    hit = [m for m in _FAKE_MARKERS if m in low]
    if hit:
        reasons.append(f"placeholder/stub markers: {hit}")
    if answer and len(answer) < 8 and status != "ok":
        reasons.append("answer too thin to be a real completion")

    r["verified"] = not reasons
    if reasons:
        r["verify_reason"] = "; ".join(reasons)
    return r


def prepare_workflow(wf: dict, *, do_redecompose: bool = True, do_grade: bool = True) -> dict:
    """The SOM->TENG preprocessing the orchestrator runs before workflow.run_workflow:
    re-decompose compound steps (SOM), then grade + tier them (TENG). The result is a normal
    workflow dict workflow.run_workflow executes unchanged; pass order_wave to it via the engine
    if per-wave priority ordering is wanted. Pure; returns a new dict."""
    out = wf
    if do_redecompose:
        out = redecompose(out)
    if do_grade:
        out = grade_steps(out)
    return out
