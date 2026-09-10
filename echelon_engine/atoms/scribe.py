"""scribe — dispatch a model to READ code and WRITE accurate docs, as a REUSABLE DOOR (not a throwaway script).

THE HABIT FIX (owner 2026-06-20): "no cartridge = new .py with prompt → why not create a cartridge skill,
and expand it every time so we don't lose a good skill." Every doc/scan dispatch I was hand-rolling as a
fresh scratch/*.py with a bespoke prompt — the exact use-the-front-door-not-handrolled-drivers trap. This
is that capability made a DOOR: a parameterized `scribe` verb that dispatches a model to scan a folder and
write one doc, with the ANTI-FABRICATION discipline BAKED IN, learned the hard way:
  - a CHEAP model (gemini-2.5-flash) FABRICATES signatures it didn't read → use a PRECISE model
    (gemini-3.5-flash) for delicate API work (owner: "this is delicate work").
  - the HARD RULE in the rules: quote the real `def`/`class` line VERBATIM; OMIT what you didn't open;
    never invent a method/param/default.
  - VERIFY THE OUTCOME, not the model's "I'm done" (the file exists + spot-check signatures vs source).
Each improvement EXPANDS this door (a better rule, a better model) instead of being lost in a one-off
script. See use-the-cli-front-door-not-handrolled-drivers, wrap-is-a-composable-front-door-not-scripts.
"""
from __future__ import annotations

# The baked-in anti-fabrication discipline — the lesson every doc dispatch must carry. EXPAND this as the
# habit sharpens; do not re-derive it per task.
ACCURACY_RULES = (
    "DELICATE PRECISION WORK — accuracy over completeness, ALWAYS. Read-only exploration except the single "
    "output file (overwrite it). THE HARD RULE: for EVERY function/method/class/CLI-flag you document you "
    "MUST have opened the file and READ the actual `def`/`class`/argparse line — quote it VERBATIM (real "
    "parameter names, defaults, types). If you did NOT open the defining file, DO NOT document it — omit it "
    "rather than guess. NEVER invent a name, parameter, or default. A short ACCURATE doc beats a long one "
    "with fabricated signatures. When unsure of any signature, OPEN THE FILE and read the line first. "
    "Markdown with a table of contents."
)

# The precise tier for signature-level work. 2.5-flash is fast but fabricates; 3.5-flash reads carefully.
DEFAULT_SCRIBE_MODEL = "gemini-3.5-flash"


def scribe(goal: str, *, folder: str, out: str, scope: str = "echelon",
           model: str = DEFAULT_SCRIBE_MODEL, extra_rules: str = "", max_steps: int = 60,
           on_event=None, dispatch_fn=None) -> dict:
    """Dispatch a model to scan `folder` and write `out`, carrying the accuracy discipline. `goal` says
    WHAT to document; the anti-fabrication rules are added automatically. Returns the dispatch result +
    an OUTCOME verification (file exists + size). Trust the outcome, not the model's claim.

    `dispatch_fn` lets the caller (the agent layer) inject the dispatcher, keeping the atoms→agent edge
    OUT of this leaf module's hard dependencies. Default falls back to the agent dispatcher via a single
    sanctioned lazy import (the one atoms→agent edge, declared in _scanner.py:_SCAN_SKIP with its reason)."""
    import os
    dispatch = dispatch_fn
    if dispatch is None:
        from echelon_engine.agent.partner import dispatch  # noqa: PLC0415 — sanctioned (see _SCAN_SKIP)
    from echelon_engine.atoms.providers.gemini import GeminiProvider

    rules = ACCURACY_RULES + (f"\nOutput file: {out} (overwrite). " if out else "")
    if extra_rules:
        rules += "\n" + extra_rules
    full_goal = goal.rstrip() + f" Write the result to {out}. Ground EVERY claim in the actual source — " \
        "read the files, quote real signatures, do not invent."

    provider = GeminiProvider() if str(model).startswith("gemini") else None
    res = dispatch(full_goal, scope=scope, folder=folder, rules=rules, role="dev",
                   provider=provider, model=model, max_steps=max_steps, on_event=on_event)

    # OUTCOME VERIFICATION (act-ready: verify the outcome, not "I'm done").
    out_path = out if os.path.isabs(out) else os.path.join(folder, out)
    wrote = os.path.exists(out_path)
    size = os.path.getsize(out_path) if wrote else 0
    res["scribe_outcome"] = {"file": out_path, "written": wrote, "bytes": size}
    return res


def _main(argv=None):
    import argparse
    import sys
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon scribe",
        description="Dispatch a model to READ code and WRITE accurate docs (anti-fabrication baked in). "
                    "The reusable door that replaces hand-rolled scratch/*.py doc dispatches.")
    ap.add_argument("goal", help="what to document, one sentence (e.g. 'a comprehensive SDK reference')")
    ap.add_argument("--folder", required=True, help="the repo to scan + write into (the partner's hands)")
    ap.add_argument("--out", required=True, help="the single output doc path (relative to folder or absolute)")
    ap.add_argument("--scope", default="echelon", help="the bank scope (the cartridge the partner wakes with)")
    ap.add_argument("--model", default=DEFAULT_SCRIBE_MODEL, help=f"driver model (default {DEFAULT_SCRIBE_MODEL})")
    ap.add_argument("--max-steps", type=int, default=60)
    a = ap.parse_args(argv)

    def on_event(kind, data):
        if kind == "step":
            print(f"  [step {data.get('n')}/{data.get('max')}]", flush=True)
        elif kind in ("blocked", "call_killed", "answer"):
            print(f"  · {kind}: {str(data)[:160]}", flush=True)

    print(f"✒ scribe → {a.out}  (model {a.model})\n", flush=True)
    res = scribe(a.goal, folder=a.folder, out=a.out, scope=a.scope, model=a.model,
                 max_steps=a.max_steps, on_event=on_event)
    oc = res.get("scribe_outcome", {})
    print(f"\nstatus: {res.get('status')}")
    print(f"outcome: written={oc.get('written')} bytes={oc.get('bytes')} -> {oc.get('file')}")
    if not oc.get("written"):
        print("  ⚠ the model claimed done but NO file on disk — outcome FAILED, do not trust the claim.")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
