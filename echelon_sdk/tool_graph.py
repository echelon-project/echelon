"""Tool graph — typed tools + typed relationships, with a self-contained validator.

THE RECLAIM (owner 2026-06-06): the atlas already solved "typed nodes with enforced relationships"
(keys.schema + validate.js: edges must resolve, be reciprocal, no dangling ids, exit 1 on violation).
We take that LOGIC and apply it to the agent's TOOLS — tools as typed nodes, their relationships as
typed edges, validated.

HARD CONSTRAINT (owner: "this is sensitive and must be standalone, cannot touch atlas, must claim it
100%"): this module imports NOTHING from the atlas and reads NONE of its files. The atlas is
load-bearing + shared + pushed to the org repo. We re-implement the validation LOGIC from scratch
here, owning every line — reclaim-the-method (re-implement clean on our own ground, never wire to
the source). Zero atlas coupling.

WHAT A TOOL CONTRACT IS — each tool declares typed edges:
  requires      : tools that must have produced state first (e.g. edit_file requires read_file —
                  the read-before-edit discipline, declared instead of only hand-coded).
  produces      : the kind of artifact it emits (handle / file-ptr / text / image-desc / none).
  consumes      : the artifact kind it takes in (file-ptr / image / text / none).
  forbidden_with: roles/contexts this tool must not run under (cross-ref to roles.forbidden).
  pairs_with    : tools it's meant to be used alongside (advisory edge).

THE VALIDATOR (validate(), the atlas logic re-owned): every edge target must RESOLVE to a known
tool (dangling = FAIL); reciprocal edges must point both ways (WARN); produces/consumes kinds must
be in the kind vocabulary (FAIL). exit 1 on any FAIL = enforce-don't-request, applied to the toolset
itself — a drift-guard for the agent's hands (catches a contract referencing a removed/renamed tool,
a broken requires-chain). This push: declare + validate ONLY. Runtime enforcement (turning a
`requires` edge into the live gate) is a deliberate later step — the live guards are load-bearing.
"""
from __future__ import annotations

# The artifact-kind vocabulary (produces/consumes must draw from this — the "schema").
KINDS = {"none", "text", "file-ptr", "handle", "image-desc", "lesson", "plan", "summary", "results"}

# THE CONTRACT — the typed tool graph. Each tool is a node; its edges are typed relationships.
# Keep it in lockstep with the registry's builtins (the validator catches drift if it isn't).
CONTRACT: dict[str, dict] = {
    "read_file":       {"produces": "text", "consumes": "file-ptr",
                        "pairs_with": ["search_file", "edit_file"]},
    "search_file":     {"produces": "text", "consumes": "none",
                        "pairs_with": ["read_file", "edit_file"]},
    "list_files":      {"produces": "text", "consumes": "none"},
    "edit_file":       {"requires": ["read_file"], "produces": "none", "consumes": "text",
                        "pairs_with": ["read_file", "search_file"]},
    "replace_in_file": {"requires": ["read_file"], "produces": "none", "consumes": "text"},
    "write_file":      {"produces": "file-ptr", "consumes": "text"},
    "run_bash":        {"produces": "handle", "consumes": "none",
                        "pairs_with": ["check_bg", "kill_bg", "look_at_image"]},
    "check_bg":        {"requires": ["run_bash"], "produces": "text", "consumes": "handle",
                        "pairs_with": ["run_bash"]},
    "kill_bg":         {"requires": ["run_bash"], "produces": "none", "consumes": "handle",
                        "pairs_with": ["run_bash"]},
    "look_at_image":   {"produces": "image-desc", "consumes": "file-ptr",
                        "pairs_with": ["run_bash"]},
    "recall":          {"produces": "text", "consumes": "none"},
    "ask_partner":     {"produces": "text", "consumes": "none", "pairs_with": ["consult"]},
    "consult":         {"produces": "text", "consumes": "none", "pairs_with": ["ask_partner"]},
    "reason":          {"produces": "text", "consumes": "text"},
    "plan":            {"produces": "plan", "consumes": "none", "pairs_with": ["finish"]},
    "spawn_subagents": {"produces": "results", "consumes": "none"},
    "finish":          {"produces": "none", "consumes": "none", "pairs_with": ["plan"]},
}

_RECIPROCAL = ("pairs_with",)   # edges that should point both ways (the atlas reciprocity rule)


def validate(contract: dict | None = None, known_tools: set | None = None) -> tuple[list, list]:
    """The atlas logic, re-owned for tools. Returns (fails, warns). Pure — no I/O, no atlas.
    - every requires/pairs_with target RESOLVES to a node in the contract (else FAIL).
    - produces/consumes kinds are in the KIND vocabulary (else FAIL).
    - pairs_with is reciprocal — if A pairs_with B, B should pairs_with A (else WARN).
    - if known_tools is given (the live registry), every contract node + edge target must exist
      in it, and every live tool should have a contract entry (drift between hands and contract).
    """
    c = contract if contract is not None else CONTRACT
    fails: list[str] = []
    warns: list[str] = []
    nodes = set(c)

    for tool, edges in c.items():
        for kind_key in ("produces", "consumes"):
            k = edges.get(kind_key)
            if k is not None and k not in KINDS:
                fails.append(f"tool '{tool}': {kind_key} '{k}' not in KIND vocabulary")
        for rel in ("requires", "pairs_with", "forbidden_with"):
            for target in edges.get(rel, []):
                if rel == "forbidden_with":
                    continue  # roles, not tools — resolved elsewhere (roles.py)
                if target not in nodes:
                    fails.append(f"tool '{tool}': {rel} '{target}' does not resolve to a known tool")
                    continue
                if rel in _RECIPROCAL:
                    back = c.get(target, {}).get(rel, [])
                    if tool not in back:
                        warns.append(f"tool '{tool}': {rel} [{target}] not reciprocated — "
                                     f"'{target}' should {rel} '{tool}' too (a pairing points both ways)")

    if known_tools is not None:
        for tool in c:
            if tool not in known_tools:
                warns.append(f"contract has '{tool}' but the live registry does not (removed/renamed?)")
        for tool in known_tools:
            if tool not in c:
                warns.append(f"live tool '{tool}' has no contract entry (declare its edges)")
    return fails, warns
