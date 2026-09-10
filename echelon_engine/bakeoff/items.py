"""items — the sealed benchmark set: schema, validation, sealing.

Every rule here exists because v1 shipped without it:

  * IMMUTABLE PROMPT BYTES     — a prompt edited mid-run silently changes the experiment.
  * SEVERITY BEFORE ANY RUN    — assigning severity after seeing answers is score-fitting.
  * LOAD-BEARING CHECKPOINTS   — "score 2 = load-bearing checkpoints correct" is unusable
                                 unless the item NAMES its checkpoints.
  * NO RUBRIC KEYED "judgment" — v1's grading key for subjective items was the word
                                 "judgment", which is not a criterion, it is a shrug.
  * DECLARED SEVERITY COUNTS   — v1's table claimed 3/5/6/1 and actually held 3/6/5/1. A
                                 distribution nobody counted is a distribution nobody can gate.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Severity -> weight, used by the weighted-quality metric Q.
WEIGHTS = {"S4": 4, "S3": 3, "S2": 2, "S1": 1}

# The sealed-set shape the protocol declares. Enforced, not documented: a set that does not
# match this is REFUSED, because v1's stated distribution and actual contents disagreed.
REQUIRED_COUNTS = {"S4": 8, "S3": 8, "S2": 8, "S1": 4}

# Deployment gates are INTEGER counts, never percentages. With 8-item strata a "85%" gate
# silently means 8/8 while an "80%" gate on the same size means 7/8 — v1's percentages did not
# express its own intended severity ordering.
GATES = {"S4": 8, "S3": 7}   # required passes; S2/S1 report only, no gate

GRADERS = {"exact", "numeric", "set", "structural", "exec", "rubric"}

# A rubric criterion may not be one of these — they name a vibe, not an observable.
_VAGUE = {"judgment", "quality", "good", "better", "sensible", "reasonable", "appropriate"}


@dataclass
class Item:
    id: str
    severity: str                      # S4 | S3 | S2 | S1
    family: str                        # billing, reconciliation, code-review, ...
    prompt: str                        # IMMUTABLE bytes
    grader: str                        # one of GRADERS
    checkpoints: list[str]             # load-bearing; wrong/missing => score < 2
    failure_conditions: list[str] = field(default_factory=list)
    expected: Any = None               # deterministic value where one exists
    tolerance: float | None = None     # for numeric graders
    fixture: str = ""                  # path
    fixture_sha256: str = ""           # canonical version hash
    rubric: list[str] = field(default_factory=list)   # observable criteria (subjective items)
    notes: str = ""

    @property
    def weight(self) -> int:
        return WEIGHTS[self.severity]


def validate_item(d: dict) -> list[str]:
    """Return a list of problems. Empty list = the item may be sealed."""
    errs: list[str] = []
    iid = d.get("id") or "<no id>"

    if not d.get("id"):
        errs.append("missing id")
    if d.get("severity") not in WEIGHTS:
        errs.append(f"{iid}: severity must be one of {sorted(WEIGHTS)}")
    if not str(d.get("family", "")).strip():
        errs.append(f"{iid}: missing family")
    if not str(d.get("prompt", "")).strip():
        errs.append(f"{iid}: empty prompt")
    if d.get("grader") not in GRADERS:
        errs.append(f"{iid}: grader must be one of {sorted(GRADERS)}")

    cps = d.get("checkpoints") or []
    if not cps:
        errs.append(f"{iid}: no load-bearing checkpoints — score>=2 would be undefinable")

    grader = d.get("grader")
    if grader in {"exact", "numeric", "set", "structural"} and d.get("expected") is None:
        errs.append(f"{iid}: grader={grader} requires a deterministic `expected`")
    if grader == "numeric" and d.get("tolerance") is None:
        errs.append(f"{iid}: numeric grader requires an explicit `tolerance` (0 is fine)")

    # THE SYNONYM TRAP, caught live: 4 of 28 items failed every cell because the key demanded
    # one phrasing of a CONCEPT ("md5", "saturation") while models gave a correct synonym
    # ("weak hash", "overload"). That converted 4 passes into critical failures and made the
    # whole S4 gate unreachable. So an `exact` item whose answer is a concept must enumerate
    # the accepted forms; a single string is only allowed for a LITERAL token.
    if grader == "exact":
        exp = d.get("expected")
        if isinstance(exp, str):
            # A first cut tried to infer "literal vs concept" from TOKEN SHAPE — short,
            # no spaces, alphanumeric. That rule passed `md5` and `saturation`, the two keys
            # that actually caused the failures, because a concept can be one short word.
            # Shape cannot carry this distinction. So the allow-list is CLOSED and explicit:
            # only answers with genuinely one correct surface form may be a bare string.
            LITERAL = {"yes", "no", "true", "false", "valid", "invalid"}
            if not (exp.strip().lower() in LITERAL
                    or re.fullmatch(r"-?\d+(\.\d+)?", exp.strip())):
                errs.append(
                    f"{iid}: exact grader with a single expected {exp!r}. Any answer with a "
                    f"defensible synonym must enumerate accepted forms as a LIST (or use "
                    f"`set`) — a key demanding one phrasing of a concept turns a correct "
                    f"answer into a critical failure (hit live: 'md5' vs 'weak hash', "
                    f"'saturation' vs 'overload')")
        elif not isinstance(exp, (list, tuple)) or not exp:
            errs.append(f"{iid}: exact grader needs a literal string or a non-empty list "
                        f"of accepted answers")
    if grader == "exec" and not d.get("fixture"):
        errs.append(f"{iid}: exec grader requires a `fixture` (the hidden tests)")

    if grader == "rubric":
        rub = d.get("rubric") or []
        if not rub:
            errs.append(f"{iid}: rubric grader requires observable criteria")
        for c in rub:
            if str(c).strip().lower() in _VAGUE:
                errs.append(f"{iid}: rubric criterion {c!r} is a vibe, not an observable "
                            f"— name the register / required facts / prohibited behaviour")

    if d.get("fixture") and not d.get("fixture_sha256"):
        errs.append(f"{iid}: fixture without a version hash — the run is not reproducible")

    # A prompt naming the experiment leaks the treatment into the task.
    leak = re.search(r"\b(flash|v4-pro|effort|swarm|arbiter|bakeoff|cell [A-E])\b",
                     str(d.get("prompt", "")), re.I)
    if leak:
        errs.append(f"{iid}: prompt leaks benchmark/model wording ({leak.group(0)!r})")
    return errs


def load_items(path: str | Path) -> tuple[list[Item], list[str]]:
    """Load a sealed set. Returns (items, problems). Non-empty problems => DO NOT RUN."""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    rows = raw.get("items", raw) if isinstance(raw, dict) else raw

    problems: list[str] = []
    items: list[Item] = []
    seen: set[str] = set()

    for d in rows:
        errs = validate_item(d)
        problems.extend(errs)
        if errs:
            continue
        if d["id"] in seen:
            problems.append(f"duplicate id {d['id']!r}")
            continue
        seen.add(d["id"])
        items.append(Item(**{k: v for k, v in d.items() if k in Item.__annotations__}))

    counts = {s: sum(1 for i in items if i.severity == s) for s in WEIGHTS}
    if items and counts != REQUIRED_COUNTS:
        problems.append(
            f"severity distribution {counts} != required {REQUIRED_COUNTS} "
            f"(v1 claimed 3/5/6/1 and actually held 3/6/5/1 — count, do not declare)")
    return items, problems


def seal_hash(items: list[Item]) -> str:
    """One hash over the immutable content of the whole set. Recorded on every run; a changed
    hash means the benchmark moved and results are not comparable."""
    h = hashlib.sha256()
    for it in sorted(items, key=lambda x: x.id):
        h.update(it.id.encode())
        h.update(it.severity.encode())
        h.update(it.prompt.encode())
        h.update(json.dumps(it.expected, sort_keys=True, default=str).encode())
        h.update(it.fixture_sha256.encode())
    return h.hexdigest()[:16]


def total_weight(items: list[Item]) -> int:
    """Sum of severity weights — the denominator of Q. 8/8/8/4 => 76."""
    return sum(i.weight for i in items)
