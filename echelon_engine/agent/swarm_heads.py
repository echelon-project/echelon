"""swarm_heads — the SUBJECT HEADS that branch off the swarm trunk (swarm_subject.py).

"swarm first, then branching to subject" (owner 2026-06-21). The trunk is general; a HEAD is the subject
knowledge. This module registers the heads. First head: `ui` (the audit→fix UX pipeline that earned its
discipline on the Epsilon-Co run). code / scribe / qc heads append here later — each is ~40 lines of
frame+rules+output+verify, NOT a new pipeline.

A head ships in two MODES where a subject has both an inspect and an act pass:
  ui:audit  — read-only: a judge grades each unit, emits a per-finding report. Never collides.
  ui:fix    — writes: a code-writer fixes from the findings, COLLISION-PARTITIONED (one-domain-one-file),
              VERIFIED structurally. The act pass is where the trunk's partition+leaf-split+verify earn out.
"""
from __future__ import annotations

import re
from pathlib import Path

from .swarm_subject import SubjectHead, Unit, WorkerResult, register_head

# THE YAGNI LADDER (yagni-ladder atom, ponytail-adopted) — appended to every FIX head's rules so the
# low-floor writers default to the MINIMUM scoped change, not a rewrite. The counterweight to swarm bloat;
# verify-it-ran catches breakage after, the ladder prevents it before.
_YAGNI = """\
WRITE THE LEAST CODE THAT WORKS — climb the ladder, stop at the first rung that satisfies:
(1) does this need to exist? no → skip it (YAGNI)  (2) stdlib does it? → use it  (3) native feature? → use
it  (4) dependency already present? → use it  (5) one line? → one line  (6) only then: the minimum that
works. Prefer the smallest scoped patch over a rewrite. NEVER cut for minimalism: validation, error
handling, security, accessibility, data-loss safety — those stay."""

# ── shared UI consumer/standard text (a head can override per-project via Unit.payload['frame']) ──
_UI_FRAME_DEFAULT = """\
You are a SENIOR PRODUCT DESIGNER with NO stake in this design, doing a COLD CODE-ONLY audit (you cannot
run the app — read the HTML/template + CSS). Grade FOR the real consumer, not in the abstract."""

_UI_RULES = """\
GRADE AGAINST THIS EARNED STANDARD — do not invent fresh taste.
THE 7 RANKED CRITERIA: (1) scannability at scale, (2) visual hierarchy / right center-of-gravity,
(3) repetition / redundancy (per-row chrome), (4) alignment & rhythm (8-pt), (5) label clarity,
(6) information density, (7) affordance & state (interactive looks interactive; empty/loading/error).
PLUS THE STRUCTURAL STANDARD:
 - DEVICE-FRAME: is the frame right (fixed phone-frame clamp+center vs fluid)? The OUTERMOST container
   must be clamped, not just an inner panel; overlays clamp inside the frame, not the viewport.
 - COMPONENT FIT (minimum-condition rule): LIST for scan-down-one-stream; CARDS only when the visual
   carries the decision; TABLE only when column-comparison is the real job (stack to mini-cards on phone);
   BADGE=static status/count never clickable; CHIP=clickable/filter; LABEL=naming. DIALOG only to block a
   critical decision; SIDE-SHEET/DRAWER for supplementary; persistent sidebar only on expanded width."""

_UI_AUDIT_OUTPUT = """\
Report ONLY THINGS TO CHANGE — no praise. PER FINDING, ranked highest-impact first:
### N. <title> — <criterion(s)>
- **Where:** element/selector/block (quote the line).
- **Context:** what this screen is + who uses it (name the consumer) so the WHY is concrete.
- **The problem:** the criterion + the cost to that consumer.
- **Change:** the concrete layout/CSS/structure fix (do not rewrite the feature).
End with: **HIGHEST-LEVERAGE CHANGE: <one>.**
Output the markdown report only — no code fences around the whole thing."""

_UI_FIX_OUTPUT = """\
Fix the findings. Layout/structure/clarity ONLY — no new features, no feature rewrites, no framework swaps.
Keep Jinja/HTMX/Alpine intact; reuse existing tokens/classes; invent no undefined CSS vars or template
filters. Output the FULL new file content in ONE fenced block:
```{lang}
<full new file>
```"""


def _audit_parse(raw: str, unit: Unit) -> str | None:
    """Audit emits prose markdown; truncation shows as a missing HIGHEST-LEVERAGE line or empty body."""
    t = (raw or "").strip()
    if not t or "###" not in t and "HIGHEST-LEVERAGE" not in t and len(t) < 40:
        return None
    return t


def _fix_parse(raw: str, unit: Unit) -> str | None:
    """Fix emits one fenced code block (the full new file). No block = truncated → leaf-split."""
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", raw or "", re.S)
    return (m.group(1).rstrip() + "\n") if (m and m.group(1).strip()) else None


# ── the verify gate (the regressions THIS session caught, generalized) ──
def _ui_fix_verify(results: list[WorkerResult]) -> list[str]:
    """Structural checks over produced files — surface, don't swallow. The trunk is the orchestrator that
    PROVES the run ran, not the taste-maker. (Caught this session: a dropped <script>, an invented filter.)
    Note: full parse/script-diff checks need the ORIGINAL on disk; here we flag the cheap, in-artifact tells
    and leave deep diff checks to the caller that has both versions."""
    problems: list[str] = []
    for r in results:
        if not r.ok:
            problems.append(f"{r.key}: worker failed ({r.detail})")
            continue
        a = r.artifact or ""
        # an x-data="foo()" with no matching definition in-file and no obvious shared js is a smell
        for fn in set(re.findall(r'x-data="([a-zA-Z_]\w*)\(\)', a)):
            if f"function {fn}" not in a and f"{fn} =" not in a and f"{fn}:" not in a:
                problems.append(f"{r.key}: x-data='{fn}()' has no in-file definition (verify shared js)")
        # unbalanced template blocks
        if a.count("{% block") != a.count("{% endblock"):
            problems.append(f"{r.key}: unbalanced {{% block %}}/{{% endblock %}}")
    return problems


def make_ui_head(mode: str = "audit", project_frame: str | None = None,
                 lang: str = "html") -> SubjectHead:
    """Build the ui head in `audit` (judge, read-only) or `fix` (writer, partitioned) mode. `project_frame`
    injects the project's consumer context (VISION-derived); without it the generic designer frame is used.
    The PARTITION for fix mode is supplied by the caller (it knows the file map) — see the card/runner."""
    frame_txt = (project_frame or _UI_FRAME_DEFAULT)
    if mode == "audit":
        return SubjectHead(
            name="ui:audit",
            frame=lambda u: u.payload.get("frame", frame_txt),
            rules=lambda u: _UI_RULES,
            output=lambda u: _UI_AUDIT_OUTPUT,
            parse=_audit_parse,
            verify=lambda rs: [f"{r.key}: empty audit" for r in rs if r.ok and not (r.artifact or "").strip()],
            model="gemini-3.5-flash",   # the JUDGE tier
            max_tokens=4000,
        )
    if mode == "fix":
        return SubjectHead(
            name="ui:fix",
            frame=lambda u: u.payload.get("frame", frame_txt),
            rules=lambda u: _UI_RULES + "\n\n" + _YAGNI,
            output=lambda u: _UI_FIX_OUTPUT.replace("{lang}", lang),
            parse=_fix_parse,
            verify=_ui_fix_verify,
            model="gemini-3-flash-preview",   # writer tier (flash-LITE is BANNED — owner standing constraint)
            max_tokens=6000,
        )
    raise ValueError(f"ui head mode must be 'audit' or 'fix', got {mode!r}")


# register the default-frame ui heads so `run_subject('ui:audit', ...)` works out of the box; a project
# rebuilds them with its own frame via make_ui_head(project_frame=...).
register_head(make_ui_head("audit"))
register_head(make_ui_head("fix"))


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE CODE HEAD — the second subject branch (swarm-code-head). Same trunk, the code-review standard.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
_CODE_FRAME_DEFAULT = """\
You are a SENIOR ENGINEER doing a careful code review. You read ONE file at a time with full attention
(defect detection peaks under ~400 LOC/pass — the swarm gives you that focus). Grade FOR this project's
real architecture and conventions, not against generic taste."""

_CODE_RULES = """\
GRADE AGAINST THIS RANKED STANDARD (web-confirmed 2025; Google/Microsoft/AWS/OWASP-distilled). Report
LOAD-BEARING findings, not style noise. Rank by SEVERITY — a correctness bug outranks a nit.
 1. CORRECTNESS & BUGS (highest) — boundary conditions, null/empty inputs, off-by-one, race conditions,
    error paths, resource leaks (unclosed files/connections), wrong logic.
 2. SECURITY — OWASP Top 10 lens: injection, authz/authn at the boundary, secrets in code, unsafe
    deserialization, SSRF. Name the vuln class.
 3. PERFORMANCE (hot paths only) — N+1 queries, O(n^2) that scales, unbounded memory, needless recompute.
    Do NOT micro-optimize cold code.
 4. MAINTAINABILITY — readability, naming, dead code, duplication, leaky abstraction, missing types, a
    function doing too much. Match the file's OWN conventions; do not impose a foreign style.
 5. TESTS — are tests isolated, with meaningful asserts and edge cases? Are they as good as the code?"""

_CODE_AUDIT_OUTPUT = """\
Report ONLY findings worth acting on — no praise. PER FINDING, ranked by SEVERITY (highest first):
### N. <title> — <CORRECTNESS|SECURITY|PERFORMANCE|MAINTAINABILITY|TESTS>
- **Where:** file:line (quote the code).
- **The problem:** the failure class + the concrete cost (what breaks, when).
- **Fix:** the precise change (scoped — do not propose a rewrite).
End with: **HIGHEST-SEVERITY ISSUE: <one>.**
If the file is genuinely clean, say so in one line and stop. Output markdown only — no wrapping fence."""

_CODE_FIX_OUTPUT = """\
Patch the findings — SCOPED to exactly what they name (no drive-by rewrites, no scope drift). Preserve
public signatures unless a finding names them. Do not invent imports/symbols. Keep the file's conventions.
Output the FULL new file content in ONE fenced block:
```{lang}
<full new file>
```"""


def _code_fix_verify(results: list[WorkerResult]) -> list[str]:
    """Structural checks over patched files — surface, don't swallow. Python files must still AST-parse;
    flag obvious dropped-content / invented-symbol smells. Deep semantic checks (tests, signature diff vs
    original) belong to the caller that has both versions + the test suite."""
    import ast
    problems: list[str] = []
    for r in results:
        if not r.ok:
            problems.append(f"{r.key}: worker failed ({r.detail})")
            continue
        a = r.artifact or ""
        # python files must parse (the key encodes the path; __ was the path separator)
        if r.key.endswith("py") or "py" in r.key.rsplit("__", 1)[-1]:
            try:
                ast.parse(a)
            except SyntaxError as e:
                problems.append(f"{r.key}: patched file no longer parses — {e.msg} line {e.lineno}")
        # a suspiciously short patch of a non-trivial file = likely truncation/over-deletion
        if len(a.splitlines()) < 3 and a.strip():
            problems.append(f"{r.key}: patch is {len(a.splitlines())} lines — likely truncated/over-deleted")
    return problems


def make_code_head(mode: str = "audit", project_frame: str | None = None,
                   lang: str = "python") -> SubjectHead:
    """Build the code head in `audit` (reviewer, read-only) or `fix` (writer, one-file-per-worker, verified)
    mode. `project_frame` injects the project's architecture/convention laws so intentional patterns aren't
    flagged as bugs."""
    frame_txt = (project_frame or _CODE_FRAME_DEFAULT)
    if mode == "audit":
        return SubjectHead(
            name="code:audit",
            frame=lambda u: u.payload.get("frame", frame_txt),
            rules=lambda u: _CODE_RULES,
            output=lambda u: _CODE_AUDIT_OUTPUT,
            parse=_audit_parse,
            verify=lambda rs: [f"{r.key}: empty review" for r in rs if r.ok and not (r.artifact or "").strip()],
            model="gemini-3.5-flash",          # the reviewer/judge tier
            max_tokens=4000,
        )
    if mode == "fix":
        return SubjectHead(
            name="code:fix",
            frame=lambda u: u.payload.get("frame", frame_txt),
            rules=lambda u: _CODE_RULES + "\n\n" + _YAGNI,
            output=lambda u: _CODE_FIX_OUTPUT.replace("{lang}", lang),
            parse=_fix_parse,
            verify=_code_fix_verify,
            model="gemini-3-flash-preview",     # writer tier (flash-LITE is BANNED — owner standing constraint)
            max_tokens=6000,
        )
    raise ValueError(f"code head mode must be 'audit' or 'fix', got {mode!r}")


register_head(make_code_head("audit"))
register_head(make_code_head("fix"))
