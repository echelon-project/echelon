"""author — the AUTHOR-kind swarm runner: produce a large artifact by DRIVING THE AGENT
LOOP step by step.

THE KEY INSIGHT (owner, the marriage reframe): the model output ceiling (~8k tokens for
gemini) is NOT a routing problem — it's an AGENT problem. An agent doesn't emit one giant
artifact in a single completion; it takes many small bounded ACTIONS in a loop, each one
under the ceiling. So an author type decomposes its artifact into ordered SECTIONS and
authors them one bounded step at a time. The artifact lives ON DISK between steps; each
step reads the growing file and writes/edits ONE region. That is how a model with a small
output budget builds a large, coherent artifact — by acting like an agent.

This is the third swarm kind beside read (analyze→report) and execute (managed ledger work).

Pipeline per author run:
  1. resolve the tier → provider + model (gem = gemini Pro, deep = deepseek)
  2. for each section in st.decompose:
       drive ONE make_agent_runner step, sandboxed to the artifact's folder, whose task is
       "author/edit THIS section of <artifact>, bounded, under the ceiling"
  3. GATE: the gate_model reviews the assembled artifact for coherence/defects
  4. VERIFY: confirm the artifact actually exists and CHANGED on disk (the exit-0 lie's
     cousin — a step can claim success without writing; we check the bytes).

Built-in only — it binds the agent loop. User types stay kind=read.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


# tier → default author model. The author loop needs a provider that natively drives a
# tool loop (write_file/edit_file/run_bash). gem = gemini Pro (the UX-author tier, stepwise);
# deep = deepseek-chat (the cheap loop-driver default for code/docs).
_TIER_MODEL = {
    "gem":  "gemini-3.1-pro-preview",
    "deep": "deepseek-chat",
    "":     "deepseek-chat",
}

# cartridge → agent ROLE. make_agent_runner boots the step INTO a role (role_preamble +
# role_device_dir + forbidden_tools come from echelon_sdk.roles.ROLES). A cartridge name
# like 'ux'/'scribe'/'yagni' is NOT a valid role — passing it raw gives an empty preamble
# and no lived device (the author loses its persona). So map each author type's cartridge to
# the closest REAL role. Unknown cartridges fall back to 'dev' (a writeable, lived role).
_VALID_ROLES = {
    "architect", "auditor", "brand_strategist", "creator", "designer", "dev",
    "integrator", "prompt_engineer", "public", "researcher", "reviewer", "supporter", "tester",
}
_CARTRIDGE_ROLE = {
    "ux": "designer", "scribe": "researcher", "yagni": "dev",
    "architect": "architect", "brainstorm": "researcher", "ops": "integrator",
    "pm": "architect", "qa": "tester",
}


def _author_role(st) -> str:
    """The agent ROLE the author worker boots into. Map the type's cartridge to a real role;
    if the cartridge IS already a role, use it; else fall back to 'dev'."""
    cart = st.cartridge or "dev"
    if cart in _VALID_ROLES:
        return cart
    return _CARTRIDGE_ROLE.get(cart, "dev")


def _resolve_author_model(st) -> str:
    """The model that DRIVES the author loop, in precedence order:
    explicit author_model > st.model > tier default."""
    if st.author_model:
        return st.author_model
    if st.model:
        return st.model
    return _TIER_MODEL.get(st.tier, _TIER_MODEL[""])


def _section_task(st, section: dict, artifact: Path, goal: str, idx: int, total: int) -> str:
    """The bounded task handed to ONE agent step: author exactly THIS section of the
    artifact, in place, under the output ceiling. The agent reads the growing file itself."""
    rel = artifact.name
    exists = artifact.is_file()
    contract = (
        f"You are authoring ONE section of a larger artifact, step {idx}/{total}.\n"
        f"GOAL (the whole artifact): {goal}\n\n"
        f"ARTIFACT FILE: {rel}\n"
        + (f"It already exists with prior sections — READ it first, then EDIT it in place "
           f"(use edit_file to add/modify only this section's region; do NOT rewrite the whole file).\n"
           if exists else
           f"It does not exist yet — CREATE it (write_file) with this first section.\n")
        + f"\nTHIS SECTION ({section['name']}): {section['instruction']}\n\n"
        f"DISCIPLINE — act like an agent, not a one-shot generator:\n"
        f"  • Make a SMALL, BOUNDED change — only this section's region. Stay well under the output ceiling.\n"
        f"  • Do NOT regenerate or restate sections already in the file.\n"
        f"  • Leave the artifact VALID and coherent after your edit (it must still parse/render).\n"
        f"  • When this section is written to the file, state what you wrote and finish.\n"
    )
    return contract


def run_author_swarm(st, *, goal: str, context: str = "", folder: str = "",
                     output: str = "", provider: str = "auto", timeout: int = 300,
                     on_event=None) -> dict:
    """Drive an author-kind swarm: author the artifact section by section through the agent
    loop, then gate + verify. Returns a result dict {status, artifact, sections, gate, verified}.

    folder: the sandbox the agent writes in (default cwd). output: artifact path override.
    """
    from echelon_engine.atoms.routing import provider_for
    from echelon_engine.agent.workflow import make_agent_runner

    folder = folder or str(Path.cwd())
    folder_path = Path(folder).resolve()
    artifact_name = output or st.artifact or f"{st.name.upper()}_OUTPUT.txt"
    artifact = (folder_path / artifact_name) if not Path(artifact_name).is_absolute() else Path(artifact_name)

    author_model = _resolve_author_model(st)
    prov = provider_for(author_model)
    sections = st.decompose or [{"name": "draft", "instruction": "Produce the full artifact toward the goal."}]

    print(f"⚡ ECHELON SWARM — {st.name} (kind=author, tier={st.tier or 'default'})")
    print(f"   {st.description}")
    print(f"   author model: {author_model}   artifact: {artifact}")
    print(f"   sections: {len(sections)} ({', '.join(s['name'] for s in sections)})")

    # capture the pre-state for the verify pass (the bytes, not the model's word)
    before_exists = artifact.is_file()
    before_size = artifact.stat().st_size if before_exists else 0
    before_mtime = artifact.stat().st_mtime if before_exists else 0.0

    # ONE agent runner, reused per section (each call is a fresh bounded loop in the same sandbox).
    role = _author_role(st)  # map the type's cartridge to a REAL agent role (lived device + preamble)
    guidance = (
        "You are an AUTHOR worker in an ECHELON author-swarm. Build a large artifact by making "
        "many small bounded edits, one section per step — never one giant completion. "
        + (f"\nCONTEXT:\n{context}" if context else "")
    )
    run_step = make_agent_runner(prov, author_model, str(folder_path),
                                 guidance=guidance, on_event=on_event)

    t0 = time.time()
    section_results: list[dict] = []
    for i, section in enumerate(sections, 1):
        task = _section_task(st, section, artifact, goal, i, len(sections))
        print(f"   [{i}/{len(sections)}] authoring section '{section['name']}'...")
        try:
            res = run_step({"agent": role, "task": task}, {})
        except Exception as e:
            res = {"status": "error", "answer": str(e)[:300], "steps": 0}
            print(f"      section '{section['name']}' raised: {str(e)[:160]}", file=sys.stderr)
        section_results.append({
            "name": section["name"],
            "status": res.get("status", "?"),
            "steps": res.get("steps", 0),
            "answer": (res.get("answer") or "")[:300],
        })
        print(f"      → {res.get('status','?')} ({res.get('steps',0)} steps)")

    elapsed = round(time.time() - t0, 1)

    # ── VERIFY (the exit-0 lie's cousin): did the artifact actually change on disk? ──
    after_exists = artifact.is_file()
    after_size = artifact.stat().st_size if after_exists else 0
    after_mtime = artifact.stat().st_mtime if after_exists else 0.0
    verified = after_exists and (
        not before_exists or after_size != before_size or after_mtime > before_mtime
    )

    # ── GATE: a reviewer reads the assembled artifact for coherence/defects ──
    gate = None
    if after_exists:
        gate = _gate_artifact(st, artifact, goal, provider=provider, timeout=timeout)

    status = "completed" if verified else "failed-no-artifact-change"
    all_section_ok = all(r["status"] not in ("error", "failed") for r in section_results)
    if not all_section_ok and verified:
        status = "completed-with-section-errors"

    print(f"   ── author result: {status} ── ({elapsed}s, {len(sections)} sections)")
    print(f"   artifact {'CHANGED' if verified else 'UNCHANGED'} on disk: {artifact} "
          f"({after_size} bytes)")
    if gate:
        print(f"   gate verdict: {gate.get('verdict','?')}")

    return {
        "status": status,
        "artifact": str(artifact),
        "verified": verified,
        "sections": section_results,
        "gate": gate,
        "elapsed_secs": elapsed,
        "bytes": after_size,
    }


def _gate_artifact(st, artifact: Path, goal: str, *, provider: str = "auto",
                   timeout: int = 300) -> dict:
    """The gate: a reviewer model reads the FINISHED artifact and judges it against the goal.
    Returns {verdict, notes}. The clearing comes from a model READING the output, not the one
    that wrote it.

    PROVIDER ROUTING (the T4 fix): the gate must reach the gate_model's OWN provider. The swarm
    dispatch send() routes by a single configured BRAIN provider (deepseek by default), so a
    gemini gate_model sent through it lands on DeepSeek's API, fails, and returns "" — which the
    old code silently read as a REVISE verdict (a gate that claims a verdict without reviewing —
    the exit-0 lie's cousin). So unless the caller FORCES a provider (--provider X), we resolve the
    gate model's provider directly via provider_for(gate_model), exactly like the author loop does.
    And an EMPTY reply is an honest gate-error, never a fabricated REVISE.
    """
    try:
        content = artifact.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"verdict": "unreadable", "notes": str(e)[:160]}
    if len(content) > 60_000:
        content = content[:60_000] + "\n... [truncated for gate review]"

    gate_model = st.gate_model or _resolve_author_model(st)
    prompt = (
        f"You are the GATE for an author-swarm. An author worker built this artifact toward the goal.\n"
        f"GOAL: {goal}\n\n"
        f"Judge the artifact: is it COHERENT (sections fit together), COMPLETE (covers the goal), "
        f"and CORRECT (no broken/contradictory/placeholder content)? Be specific.\n"
        f"Start your reply with exactly one verdict word: PASS, REVISE, or FAIL.\n"
        f"Then 2-5 bullet notes (the defects to fix, or why it passes).\n\n"
        f"--- ARTIFACT ({artifact.name}) ---\n{content}\n--- END ---\n"
    )
    raw = ""
    try:
        if provider and provider != "auto":
            # caller forced a provider — honor it through the dispatch router
            from .dispatch import send
            raw = send(prompt, provider=provider, model=gate_model, timeout=timeout)
        else:
            # default: reach the gate_model's OWN provider (the T4 fix), not the single brain
            from echelon_engine.atoms.routing import provider_for
            prov = provider_for(gate_model)
            resp = prov.send([{"role": "user", "content": prompt}],
                             model_id=gate_model, temperature=0.3, timeout=timeout)
            raw = getattr(resp, "content", "") or ""
    except Exception as e:
        return {"verdict": "gate-error", "notes": str(e)[:200], "gate_model": gate_model}
    head = (raw or "").strip()
    if not head:
        # empty reply = the review did NOT happen; do NOT fabricate a verdict
        return {"verdict": "gate-error", "notes": "gate model returned no content "
                f"(model={gate_model}, provider={provider})", "gate_model": gate_model}
    verdict = "REVISE"
    for v in ("PASS", "REVISE", "FAIL"):
        if head.upper().startswith(v):
            verdict = v
            break
    return {"verdict": verdict, "notes": head[:1200], "gate_model": gate_model}
