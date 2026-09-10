"""graders — deterministic scoring where determinism is possible.

The rule: an LLM judge is the LAST resort, not the default. Where a task has an exact answer,
an executable check, or a parseable structure, a program decides — because a judge introduces
variance into the measurement instrument itself, and this experiment is trying to measure
variance in the candidates.

Every grader returns (score 0..3, reason). Score >= 2 is a pass.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .items import Item


def _num(text: str) -> list[float]:
    """Every number in the text, commas and currency stripped."""
    cleaned = re.sub(r"[,$]", "", text)
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", cleaned)]


def grade_exact(item: Item, answer: str) -> tuple[int, str]:
    """Exact presence of ANY accepted form.

    `expected` may be a string OR a list of acceptable answers. The list form exists because
    the first live run failed 4 items on synonyms, not on capability: models answered "weak
    hash" where the key said `md5`, and "overload" where the key said `saturation`. Both are
    CORRECT. An exact-matcher that rejects a synonym is not measuring the model, it is
    measuring the key — and it silently converts a passing item into a critical failure.

    RULE (enforced by the validator): use `exact` ONLY for literal tokens — an id, a figure,
    a yes/no. For a CONCEPT, enumerate every defensible phrasing here or use `set`.
    """
    accepted = item.expected if isinstance(item.expected, (list, tuple)) else [item.expected]
    got = " ".join(answer.split()).lower()
    for want in accepted:
        if str(want).strip().lower() in got:
            return 3, f"matched {want!r}"
    return 0, f"none of {[str(a) for a in accepted]} found"


def grade_numeric(item: Item, answer: str) -> tuple[int, str]:
    """The expected value must appear within tolerance.

    Deliberately checks PRESENCE among the answer's numbers rather than parsing 'the' answer:
    a correct figure buried in correct working is still correct, and a rubric that demands a
    format is testing formatting, not arithmetic. Format belongs in `structural`.
    """
    want = float(item.expected)
    tol = float(item.tolerance or 0.0)
    nums = _num(answer)
    if not nums:
        return 0, "no number in answer"
    hit = [n for n in nums if abs(n - want) <= tol]
    if not hit:
        closest = min(nums, key=lambda n: abs(n - want))
        return 0, f"expected {want} (+/-{tol}), closest was {closest}"
    # The final number stated should be the answer, not an intermediate.
    if abs(nums[-1] - want) <= tol:
        return 3, f"correct: {want}"
    return 2, f"correct value present but not the final figure (ends {nums[-1]})"


def grade_set(item: Item, answer: str) -> tuple[int, str]:
    """Exhaustive-logic items: the full expected set, no more and no less."""
    want = {str(x).strip().lower() for x in (item.expected or [])}
    low = answer.lower()
    found = {w for w in want if w in low}
    missing = want - found
    if not missing:
        return 3, f"all {len(want)} members present"
    if len(found) >= len(want) * 0.5:
        return 1, f"missing {sorted(missing)}"
    return 0, f"missing {len(missing)}/{len(want)}: {sorted(missing)}"


def grade_structural(item: Item, answer: str) -> tuple[int, str]:
    """Instruction-following / extraction: the OUTPUT SHAPE is the thing under test.

    `expected` is a dict of structural assertions, e.g.
        {"json": true, "keys": ["a","b"], "max_words": 50, "forbid": ["sorry"]}
    """
    spec: dict[str, Any] = item.expected or {}
    problems: list[str] = []

    body = answer.strip()
    if spec.get("json"):
        fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
        raw = fence.group(1).strip() if fence else body
        try:
            parsed = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            return 0, f"not valid JSON: {e}"
        for k in spec.get("keys", []):
            if isinstance(parsed, dict) and k not in parsed:
                problems.append(f"missing key {k!r}")
    else:
        for k in spec.get("keys", []):
            if k.lower() not in body.lower():
                problems.append(f"missing {k!r}")

    if (mw := spec.get("max_words")) and len(body.split()) > int(mw):
        problems.append(f"{len(body.split())} words > max {mw}")
    for bad in spec.get("forbid", []):
        if str(bad).lower() in body.lower():
            problems.append(f"contains forbidden {bad!r}")

    if not problems:
        return 3, "all structural requirements met"
    return (1 if len(problems) == 1 else 0), "; ".join(problems)


def grade_exec(item: Item, answer: str) -> tuple[int, str]:
    """Coding items: run HIDDEN tests against the produced code.

    The fixture is a pytest file the candidate never sees. Extraction takes the last fenced
    block, which is the conventional 'final answer' position.
    """
    blocks = re.findall(r"```(?:python)?\s*(.+?)```", answer, re.S)
    if not blocks:
        return 0, "no code block in answer"
    code = blocks[-1]

    fixture = Path(item.fixture)
    if not fixture.exists():
        return 0, f"fixture missing: {item.fixture}"

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "solution.py").write_text(code, encoding="utf-8")
        (d / "test_hidden.py").write_text(fixture.read_text(encoding="utf-8"),
                                          encoding="utf-8")
        try:
            r = subprocess.run(["python", "-m", "pytest", "-q", str(d / "test_hidden.py")],
                               cwd=td, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return 0, "hidden tests timed out"
    tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
    if r.returncode == 0:
        return 3, f"hidden tests pass ({tail[0]})"
    if "passed" in (r.stdout or ""):
        return 1, f"partial: {tail[0]}"
    return 0, f"hidden tests fail: {tail[0]}"


GRADERS = {
    "exact": grade_exact,
    "numeric": grade_numeric,
    "set": grade_set,
    "structural": grade_structural,
    "exec": grade_exec,
}


def grade(item: Item, answer: str) -> tuple[int, str]:
    """Deterministic grade, or (-1, 'rubric') when the item needs blind LLM adjudication."""
    if not answer or not answer.strip():
        return 0, "empty answer"
    fn = GRADERS.get(item.grader)
    if fn is None:
        return -1, "rubric — requires blind graders"
    return fn(item, answer)
