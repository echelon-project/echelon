"""recipe — the ROUTING LAW. One board that answers "how should this goal be run?"

THE SYNTHESIS (owner, 2026-08-18). Four organs existed separately and each encoded one
reason. This module is the single law that combines them:

  1. WHY WE DELEGATE      — intelligence spends itself only on intelligence-class problems
                            (CV-001). Labor goes to a cheap isolated context; the expensive
                            context keeps the judgment. => pick the RUNG.
  2. WHY WE GATE          — a generator validating its own output shares the blind spot that
                            made the bug. The clearing must come from a context that did not
                            build it (CV-002: a free gate is the safety organ). => pick the GATE.
  3. WHY TIERS EXIST      — the question is never "which model is best", it is "what is the
                            CHEAPEST configuration sufficient for THIS class of work"
                            (bakeoff/tiers.py, 2026-08-17). => pick the LADDER position.
  4. WHY THE GOVERNOR     — composed context can substitute for escalation at zero API
                            premium; and it has TWO knobs, budget AND grain, because a full
                            atom body is dilution, not signal. => pick the CONTEXT.

THE LAW, in one line:

    route(goal) = (rung, gate, context) chosen by CLASS and floored by IRREVERSIBILITY

TWO OWNER RULINGS ARE LOAD-BEARING HERE.

**(a) Irreversibility is a VETO, not a vote.** An outward or destructive act SETS the tier.
A write/commit/deploy/send path can never be screened cheap, no matter how sufficient the
cheap rung looks, and it always carries a gate. Cost is not a consideration on a path that
cannot be undone. See `_classify` + `Route.veto`.

**(b) The screen is a FOCUSED GATE CHECK, not a self-rating.** Asking a worker "are you
confident?" measures its disposition, not its output. Instead the screen runs N narrow
checks — each one focus + its own context — on flash at low effort, where the account
carries a 2,500-call concurrency ceiling. The checks are effectively free in parallel, and
each returns a verdict on ONE named thing rather than a vibe about everything. A screen
passes only when every focus clears; any FAIL climbs.

WHAT THIS MODULE IS NOT. It does not execute anything. It emits a `Route` — a plan another
organ carries out — so `board` (kind=execute) keeps its own dispatch path untouched and
there is zero conflict. `echelon swarm -h` renders this board; `--route` prints the decision
for a goal without spending a token.

HONEST LIMIT. The rung ladder is grounded in a 28-item sealed bakeoff on ONE estate's item
mix; the context-governor arm is n=1 (one rescue from three tested items). So the defaults
here are a MEASURED STARTING POINT, not a proven optimum, and every Route carries `why` so
a wrong assignment is auditable rather than mysterious. Re-derive with `echelon bakeoff`
before treating any assignment as settled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── THE LADDER ────────────────────────────────────────────────────────────────
# Cheapest-first. A router walks it and stops at the first sufficient rung.
# Cost figures are MEASURED (2026-08-17, 28 sealed items, cold/all-miss peak pricing).
# NOTE the inversion at A vs B: low effort cost MORE than high, because it emitted 8,166
# output tokens against high's 3,100 and output bills ~3x input. Effort is not monotonic
# in cost — this ladder is ordered by MEASUREMENT, not by intuition.
RUNGS: dict[str, dict[str, Any]] = {
    "A": {"model": "deepseek-v4-flash", "effort": "low",
          "usd_28": 0.0110, "label": "flash-low",
          "note": "screen rung; cheapest to START, not cheapest to finish"},
    "B": {"model": "deepseek-v4-flash", "effort": "high",
          "usd_28": 0.0053, "label": "flash-high",
          "note": "CHEAPEST measured — thinking pays for itself in shorter output"},
    "C": {"model": "deepseek-v4-flash", "effort": "max",
          "usd_28": 0.0248, "label": "flash-max",
          "note": "diminishing; only where max reasoning is the whole job"},
    "D": {"model": "deepseek-v4-pro", "effort": "high",
          "usd_28": 0.0464, "label": "pro-high",
          "note": "the strong single rung; the floor for irreversible acts"},
    "E": {"model": "deepseek-v4-pro", "effort": "high",
          "usd_28": 0.0926, "label": "swarm+arbiter",
          "note": "fan-out then arbitrate; justified only where it is the ONLY sufficient rung",
          "fanout": True},
}
LADDER = ["A", "B", "C", "D", "E"]

# The rung an irreversible act floors to. Below this, a wrong answer can reach production.
VETO_FLOOR = "D"

# ── ACT CLASSES ───────────────────────────────────────────────────────────────
# Severity is the WORST-CASE CONSEQUENCE IF WRONG, never difficulty (bakeoff T1).
S4, S3, S2, S1 = "S4", "S3", "S2", "S1"

# TRAILING \w* IS LOAD-BEARING, NOT SLOPPINESS. These lists are word STEMS, and a
# trailing \b after a stem never matches the inflected form: r"\b(summar)\b" is FALSE for
# "summarize", "summary" and "summarizing" alike, because there is no boundary between
# "summar" and "ize". Written with \b the entire stem list is dead code that silently
# routes every inflected goal to the unclassified default. Caught 2026-08-18 by running
# the classifier on "summarize this changelog" instead of reading it. Any stem added here
# must be tested against its inflections — see tests/test_swarm_recipe.py.

# Signals that an act reaches OUTSIDE this process — these set the VETO.
#
# ONLY VERBS BELONG HERE. A noun is not an act: "extract the invoice totals" READS invoice
# data and "rewrite the onboarding email" EDITS text — neither sends anything. Listing
# `invoice`/`email` as outward vetoed both to the strong rung with a gate, which is not
# merely expensive, it is WRONG about what the goal does. Caught 2026-08-18 by running the
# table, not reading it. Keep this list verbs-only; put domains in _CRITICAL instead, where
# they raise severity without claiming an outward act.
#
# Deliberately broad WITHIN that rule: a false positive costs money, a false negative
# costs an unreviewable irreversible act.
# DESTRUCTIVE VERBS VETO ON THE VERB ALONE — no target required. A destructive verb has no
# safe reading: there is no benign sense of "erase", "destroy" or "drop <thing>". Requiring a
# target here is how an escape happens, because natural language omits it constantly.
#
# THIS LIST WAS BUILT BY A SWARM AUDIT, NOT BY INTROSPECTION (2026-08-18). Hand-testing found
# only FALSE POSITIVES (nouns vetoing) because that is what I thought to look for; the audit
# swept for FALSE NEGATIVES and found 10 confirmed escapes — `reset the repo`, `remove the
# database`, `erase the data`, `clear the cache`, `destroy the stack`, `write the file`,
# `save the document`, `update the database` all routed to the CHEAP screen rung. A veto list
# must be swept for what it MISSES; the misses are the whole safety surface.
_OUTWARD = re.compile(
    r"\b(commit|push\w*|deploy\w*|publish\w*|releas(?:e\s+it|ing)|merg\w*|rebas\w*|revert\w*|"
    r"delet\w*|drop\w*|truncat\w*|rm\b|purg\w*|wip\w*|erase\w*|eras(?:e|ing)|"
    r"remov\w*|destroy\w*|clear\w*|reset\w*|kill\w*|revok\w*|"
    r"send\w*|notif\w*|announc\w*|charg\w*|refund\w*|"
    r"migrat\w*|alter table|overwrit\w*|renam\w*|chmod|chown|"
    r"provision\w*|terminat\w*|restart\w*|rotat\w*|force[- ]push\w*)", re.I)

# Verb + TARGET pairs: the verb alone is too generic to veto on ("write a summary" must not
# veto; "write the file" must). Unlike the destructive list above these verbs have a real
# benign sense, so they need an object that names durable state.
#
# THE PREPOSITION IS OPTIONAL — requiring `to|into|onto|in` was itself an escape: English
# says "write the file" far more often than "write to the file", and the audit caught
# `write the file` / `save the document` / `update the database` sailing through.
_OUTWARD_PHRASE = re.compile(
    r"\b(writ\w*|sav\w*|updat\w*|insert\w*|appl\w*|creat\w*|modif\w*|patch\w*|"
    r"upload\w*|sync\w*|flush\w*)"
    r"(?:\s+\w+){0,4}?\s+(?:to\s+|into\s+|onto\s+|in\s+)?(?:the\s+|a\s+|our\s+)?"
    r"(file|files|db|database|table|row|record|schema|prod\w*|document|"
    r"disk|repo\w*|branch|remote|bucket|index|config|migration|s3|bank|"
    r"ledger|entry|entries|data|content|state|cache|volume|secret\w*)\b", re.I)

# Domains whose worst case is money/security/legal — S4 regardless of phrasing.
_CRITICAL = re.compile(
    r"\b(auth\w*|password|secret|token|credential\w*|api key|"
    r"payment\w*|billing|invoic\w*|ledger|reconcil\w*|refund|"
    r"tax(?:es|able|ation)?\b(?!\s+the\s+system)|"   # the money sense, not "tax the system"
    r"secur\w*|vulnerab\w*|injection|xss|csrf|traversal|privilege\w*|"
    r"pii|gdpr|complian\w*|audit\w*)", re.I)

# Work whose failure is rework, not loss.
_REVIEWABLE = re.compile(
    r"\b(summar\w*|extract\w*|classif\w*|translat\w*|rewrit\w*|rephras\w*|draft\w*|"
    r"brainstorm\w*|explor\w*|explain\w*|describ\w*|list\b|label\w*|"
    r"format\w*|lint|style|docstring\w*)", re.I)


@dataclass
class Focus:
    """ONE narrow gate check: a single question plus the context needed to answer it.

    The owner's correction to a naive screen: do not ask a worker to rate itself. Ask N
    specific questions, each carrying its own context, and require every one to clear.
    A focus is cheap enough to be free in parallel (flash concurrency ceiling = 2,500),
    so the gate's cost is LATENCY, not spend."""
    name: str
    question: str
    context: str = ""

    def render(self, artifact: str) -> str:
        parts = [f"You are checking ONE thing: {self.name}.",
                 f"QUESTION: {self.question}"]
        if self.context:
            parts.append(f"CONTEXT FOR THIS CHECK:\n{self.context}")
        parts.append(f"ARTIFACT UNDER CHECK:\n{artifact[:6000]}")
        parts.append('Answer with exactly one line: "PASS" or "FAIL: <one-line reason>". '
                     "Judge ONLY the named thing. Do not review anything else.")
        return "\n\n".join(parts)


@dataclass
class Route:
    """The routing decision. A PLAN, never an execution — another organ carries it out."""
    rung: str
    severity: str
    outward: bool
    veto: bool
    screen: bool                     # may we start cheap and climb?
    gate: bool                       # is a gate REQUIRED?
    foci: list[Focus] = field(default_factory=list)
    ctx_atoms: int = 0               # governor knob 1: how many atoms
    ctx_grain: int = 0               # governor knob 2: how much of each (chars)
    why: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        return RUNGS[self.rung]["model"]

    @property
    def effort(self) -> str:
        return RUNGS[self.rung]["effort"]

    @property
    def fanout(self) -> bool:
        return bool(RUNGS[self.rung].get("fanout"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rung": self.rung, "label": RUNGS[self.rung]["label"],
            "model": self.model, "effort": self.effort, "fanout": self.fanout,
            "severity": self.severity, "outward": self.outward, "veto": self.veto,
            "screen": self.screen, "gate": self.gate,
            "foci": [f.name for f in self.foci],
            "context": {"atoms": self.ctx_atoms, "grain": self.ctx_grain},
            "why": list(self.why),
        }


def _classify(goal: str, kind: str = "read") -> tuple[str, bool, list[str]]:
    """(severity, outward, reasons). Severity = worst case if WRONG, not difficulty."""
    why: list[str] = []
    g = goal or ""

    m_out = _OUTWARD.search(g) or _OUTWARD_PHRASE.search(g)
    outward = kind == "execute" or bool(m_out)
    if kind == "execute":
        why.append("kind=execute — the act reaches outside this process")
    elif outward:
        why.append(f"goal names an outward act ({m_out.group(0).strip()!r})")

    if _CRITICAL.search(g):
        sev = S4
        why.append(f"critical domain ({_CRITICAL.search(g).group(0)!r}) — "
                   "worst case is money/security/legal")
    elif outward:
        sev = S3
        why.append("outward but not a critical domain — worst case is rework in production")
    elif _REVIEWABLE.search(g):
        sev = S2
        why.append("human-reviewable output — worst case is a retry")
    else:
        sev = S3
        why.append("unclassified — defaulting to S3 (a guess that is WRONG "
                   "should cost rework, not silence)")
    return sev, outward, why


# Foci are the gate's teeth. Each is one narrow question + the context to answer it.
def _foci_for(severity: str, kind: str, goal: str) -> list[Focus]:
    """The focus list for a gate. Every focus must clear or the screen fails."""
    foci = [
        Focus("completeness", "Does the artifact fully answer the stated goal, "
                              "with nothing asked-for left out?", f"GOAL: {goal}"),
        Focus("grounding", "Is every factual claim supported by the provided context, "
                           "with no invented specifics (numbers, names, paths, APIs)?"),
    ]
    if severity in (S4, S3):
        foci.append(Focus(
            "correctness", "Are the load-bearing steps actually right — arithmetic, "
                           "logic, and control flow? Recompute; do not trust the "
                           "artifact's own summary of itself."))
    if severity == S4:
        foci.append(Focus(
            "blast-radius", "If this is WRONG and executed, what is the worst realistic "
                            "consequence, and is it reversible? FAIL if an irreversible "
                            "loss is possible and unguarded."))
    if kind == "execute":
        foci.append(Focus(
            "precondition", "Does the artifact verify its preconditions before acting "
                            "(target exists, backup present, idempotent on re-run)?"))
    if kind == "author":
        foci.append(Focus(
            "contract", "Does the artifact meet its structural contract — required "
                        "sections present, parseable, no truncation mid-structure?"))
    return foci


# Context governor. TWO knobs, because measuring found budget alone was the wrong axis:
# live atom bodies run 2k-13k chars, so 5 atoms at full grain is ~49k chars of dilution
# for a one-line task. The single observed rescue happened at the SMALL grain.
# n=1 — these are a starting point, not a policy. See module docstring.
_CTX = {
    S4: (8, 220),
    S3: (5, 220),
    S2: (5, 220),
    S1: (0, 0),      # a fail-safe task does not need the estate's memory
}


def route(goal: str, *, kind: str = "read", mode: str = "plan",
          force_rung: str = "", allow_screen: bool = True) -> Route:
    """Decide how to run this goal. Pure — spends nothing, touches nothing.

    force_rung wins over classification (an explicit --model/--effort is the operator
    overriding the law on purpose) but NEVER lifts the veto's gate requirement: the
    operator may choose to pay more, not to skip the safety organ."""
    severity, outward, why = _classify(goal, kind)
    veto = outward

    if force_rung:
        # AN OVERRIDE MAY PAY MORE, NEVER LESS, ON AN IRREVERSIBLE ACT. The docstring always
        # claimed force_rung "never lifts the veto's gate requirement" — but it silently let
        # an operator force an irreversible act DOWN onto the cheapest rung, which is the
        # same hole worn as a feature. Caught by the swarm audit, 2026-08-18. The floor is
        # now clamped by ladder position, so --model/--effort can escalate and never de-escalate.
        rung = force_rung
        if veto and LADDER.index(rung) < LADDER.index(VETO_FLOOR):
            why.append(f"override to {rung} REFUSED — an irreversible act cannot be routed "
                       f"below {VETO_FLOOR}; clamped up. An override may pay more, never less.")
            rung = VETO_FLOOR
        else:
            why.append(f"rung {rung} FORCED by operator "
                       "(classification would not have picked it)")
        screen = False
    elif veto:
        rung = VETO_FLOOR
        why.append(f"VETO: irreversible act floors the rung to {VETO_FLOOR} "
                   f"({RUNGS[VETO_FLOOR]['label']}) and disables cheap screening — "
                   "irreversibility is a veto, not a vote")
        screen = False
    else:
        # Reversible work: start at the screen rung and climb only on a failed focus.
        rung = "A" if allow_screen else "B"
        screen = allow_screen
        why.append("reversible: start at the screen rung and climb only on a FAILED focus "
                   "(measured: 25 of 28 items never needed to climb)")

    # The gate is required wherever being wrong is expensive, and always on an outward act.
    gate = veto or severity in (S4, S3)
    if gate and not veto:
        why.append(f"gate REQUIRED: {severity} — a generator validating itself "
                   "shares the blind spot that made the bug")
    elif not gate:
        why.append(f"gate optional: {severity} — worst case is a retry")

    atoms, grain = _CTX.get(severity, (5, 220))
    if atoms:
        why.append(f"context: {atoms} atoms at {grain}-char grain "
                   "(grain is a SECOND knob — full bodies dilute)")

    return Route(rung=rung, severity=severity, outward=outward, veto=veto,
                 screen=screen, gate=gate,
                 foci=_foci_for(severity, kind, goal),
                 ctx_atoms=atoms, ctx_grain=grain, why=why)


def climb(current: str) -> str | None:
    """The next rung up, or None at the top. Climb only on a FAILED focus."""
    i = LADDER.index(current)
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


# ── THE BOARD (rendered by `echelon swarm -h`) ────────────────────────────────

def board() -> str:
    """The recipe board: how to pick, what it costs, and why."""
    L = []
    L.append("ECHELON SWARM — ROUTING RECIPE BOARD")
    L.append("=" * 74)
    L.append("")
    L.append("  route(goal) = (rung, gate, context) chosen by CLASS, floored by IRREVERSIBILITY")
    L.append("")
    L.append("THE LADDER — cheapest sufficient rung wins (cost: 28 sealed items, cold peak)")
    L.append("-" * 74)
    for r in LADDER:
        c = RUNGS[r]
        L.append(f"  {r}  {c['label']:<14} {c['model']:<19} effort={c['effort']:<5} "
                 f"${c['usd_28']:.4f}")
        L.append(f"     {c['note']}")
    L.append("")
    L.append("  ! Effort is NOT monotonic in cost: B (high) measured CHEAPER than A (low),")
    L.append("    because low effort emitted 2.6x the output tokens. Measure, don't assume.")
    L.append("")
    L.append("THE FOUR REASONS, as one law")
    L.append("-" * 74)
    L.append("  DELEGATE  labor to the cheapest sufficient rung; judgment stays expensive.")
    L.append("  GATE      from a context that did NOT build it — a generator validating")
    L.append("            itself shares the blind spot that made the bug.")
    L.append("  TIER      never 'which model is best' — 'what is CHEAPEST SUFFICIENT here'.")
    L.append("  GOVERN    compose context before escalating; two knobs, atoms AND grain.")
    L.append("")
    L.append("CLASSIFICATION — severity is the worst case if WRONG, not difficulty")
    L.append("-" * 74)
    L.append("  S4  money / security / legal ......... gate REQUIRED · 8 atoms @220")
    L.append("  S3  outward or unclassified .......... gate REQUIRED · 5 atoms @220")
    L.append("  S2  human-reviewable ................. gate optional · 5 atoms @220")
    L.append("  S1  fail-safe (worst case: a retry) .. gate optional · no context")
    L.append("")
    L.append(f"  VETO: an outward/irreversible act floors to rung {VETO_FLOOR} "
             f"({RUNGS[VETO_FLOOR]['label']}),")
    L.append("        disables cheap screening, and ALWAYS carries a gate.")
    L.append("        Irreversibility is a veto, not a vote — cost stops being an input.")
    L.append("")
    L.append("THE SCREEN — a focused gate check, never a self-rating")
    L.append("-" * 74)
    L.append("  Asking a worker 'are you confident?' measures disposition, not output.")
    L.append("  Instead: N narrow checks, each ONE focus + its OWN context, on flash-low")
    L.append("  (concurrency ceiling 2,500 — free in parallel; the cost is latency).")
    L.append("  Every focus must PASS. Any FAIL climbs one rung and re-checks.")
    L.append("")
    L.append("  foci by class:  completeness · grounding")
    L.append("                  + correctness            (S3, S4)")
    L.append("                  + blast-radius           (S4)")
    L.append("                  + precondition           (kind=execute)")
    L.append("                  + contract               (kind=author)")
    L.append("")
    L.append("USAGE")
    L.append("-" * 74)
    L.append("  echelon swarm --goal \"...\"                  auto-route (this board decides)")
    L.append("  echelon swarm --goal \"...\" --route          print the decision, spend nothing")
    L.append("  echelon swarm --goal \"...\" --effort high    override the rung on purpose")
    L.append("  echelon swarm --goal \"...\" --no-screen      skip screening, start strong")
    L.append("")
    L.append("  An explicit override may choose to pay MORE. It may never skip a veto's gate.")
    L.append("")
    L.append("HONEST LIMIT")
    L.append("-" * 74)
    L.append("  Ladder = one 28-item sealed bakeoff on one estate's mix. Governor = n=1")
    L.append("  (one rescue, three items tested). Defaults are a MEASURED STARTING POINT,")
    L.append("  not a proven optimum. Re-derive with `echelon bakeoff` before trusting.")
    return "\n".join(L)


def explain(r: Route) -> str:
    """Render one routing decision, with its reasons."""
    c = RUNGS[r.rung]
    L = [f"ROUTE → rung {r.rung} ({c['label']}) · {c['model']} effort={c['effort']}"]
    L.append(f"  severity : {r.severity}"
             + ("  OUTWARD" if r.outward else "")
             + ("  [VETO]" if r.veto else ""))
    L.append(f"  screen   : {'yes — climb on a failed focus' if r.screen else 'NO'}")
    L.append(f"  gate     : {'REQUIRED' if r.gate else 'optional'}"
             + (f" · foci: {', '.join(f.name for f in r.foci)}" if r.gate else ""))
    L.append(f"  context  : {r.ctx_atoms} atoms @ {r.ctx_grain} chars"
             if r.ctx_atoms else "  context  : none")
    L.append("  why:")
    for w in r.why:
        L.append(f"    - {w}")
    return "\n".join(L)
