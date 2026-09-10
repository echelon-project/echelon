"""contract — the swarm's response shape, declared ONCE and used at both ends.

THE DEFECT THIS REPLACES (found 2026-08-18 by auditing the swarm with the swarm). The
worker's output shape lived in two places that could not be kept in sync:

  1. a PROSE description embedded in the prompt ("return a JSON block wrapped in ```json…"),
  2. a REGEX that tried to scrape that shape back out of the answer.

The regex could not express nesting. Its bare-JSON fallback was
`\\{[^{}]*"ok"\\s*:\\s*(true|false)[^{}]*\\}` — and `[^{}]` cannot cross a nested brace, so any
response carrying `findings: [{...}]` (which is every real report) failed to match unless it
happened to be fenced with ```json. Three modes lost work outright: bare JSON, prose-then-
JSON, and a plain ``` fence without the language tag. A worker that did its job perfectly was
recorded as `[✗] 0 findings` — and a failed lens reads to a human as "nothing wrong there".

THE FIX IS NOT A BETTER REGEX. It is to stop pattern-matching model prose at all:

    declare  →  contracts/*.json is the single source of the shape
    request  →  the prompt spec is RENDERED from the contract; the provider is asked for
                response_format=json_object so the answer is a JSON document by construction
    ingest   →  json.loads, then read fields BY KEY with contract defaults and coercion

Nothing here matches text. A field that is absent takes its declared default; a field of the
wrong type is coerced or defaulted, never dropped in silence. Parse failure is reported as a
FAILURE with its reason, never as an empty-but-successful result — that conflation is what
made a dead lens look like a clean bill of health.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent / "contracts"
_CACHE: dict[str, dict] = {}

# THE VERDICT LAW, declared ONCE (2026-08-18). The contract's `verdict` field carries the
# short form for the prompt; this is the full statement the gate leans on. Kept here rather
# than duplicated in prose so the rule cannot drift between the copy the WORKER is told and
# the copy the READER is judged against — the same one-shape-two-places defect that made the
# regex above lose 6 of 6 real reports.
VERDICT_LAW = """CONFIRMED = HARD TRUTH: the worker OPENED the file and read the lines, or RAN
the command and saw the output, and `evidence` quotes it so a reader can check WITHOUT
trusting the worker. PLAUSIBLE = HYPOTHESIS: inferred, pattern-matched, recalled, or read from
a name/doc; `evidence` then names what was NOT checked and the one command that would settle
it. Only these two — no percentages, no "likely". ANY doubt downgrades. An unlabelled finding
is PLAUSIBLE. A fabricated evidence line (output the worker did not actually see) is the single
disqualifying failure: it discredits every CONFIRMED from that lens."""


def load(name: str = "lens_report") -> dict:
    """Load a contract by name. Cached — contracts are static files."""
    if name not in _CACHE:
        _CACHE[name] = json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return _CACHE[name]


# ── prompt side: render the spec FROM the contract ────────────────────────────

def prompt_spec(name: str = "lens_report") -> str:
    """The output-format block for the prompt, generated from the contract.

    Generated, never hand-written: a hand-written spec is a second copy of the shape and
    will drift from the one the ingest actually reads."""
    c = load(name)
    fields = c["fields"]
    lines = [
        "# OUTPUT FORMAT — return ONE JSON object and nothing else",
        "",
        "No markdown fences, no prose before or after. The response is parsed as a JSON",
        "document; any text outside the object is unaddressable and will be lost.",
        "",
        "Keys (a missing key takes its default; never invent keys):",
    ]
    for key, spec in fields.items():
        if key == "findings":
            continue
        doc = spec.get("doc", "")
        lines.append(f'  "{key}": {spec["type"]}'
                     + (f"  — {doc}" if doc else ""))
    item = fields["findings"]["item"]["fields"]
    lines.append('  "findings": list of objects, each with:')
    for key, spec in item.items():
        bits = spec["type"]
        if spec.get("values"):
            bits = "|".join(spec["values"])
        doc = spec.get("doc", "")
        lines.append(f'      "{key}": {bits}' + (f"  — {doc}" if doc else ""))
    lines += [
        "",
        "EXAMPLE (shape only — do not copy its content):",
        json.dumps(c["example"], indent=2),
        "",
        "# THE VERDICT LAW — the field that decides how your report is read",
        VERDICT_LAW,
        "",
        "A short report of witnessed fact BEATS a long one padded with inference. Do NOT",
        "manufacture findings to look productive, and do NOT speculate into CONFIRMED: an",
        'all-CONFIRMED report from a lens that did little reading is a tell. "No findings on',
        'this lens" is an honest, acceptable result.',
        "",
        'If this lens found nothing, return "findings": [] with "ok": true.',
        '"ok": false means the lens could NOT run — never use it to mean "found nothing",',
        "and never fabricate a finding to avoid an empty list.",
    ]
    return "\n".join(lines)


# ── ingest side: read BY KEY, with declared defaults ──────────────────────────

def _coerce(value: Any, spec: dict) -> Any:
    """Coerce one value to its declared type. Never raises — a bad value takes the default."""
    t = spec["type"]
    default = spec.get("default")
    if value is None:
        return default
    try:
        if t == "bool":
            return bool(value) if not isinstance(value, str) else value.strip().lower() in ("true", "1", "yes")
        if t == "str":
            return value if isinstance(value, str) else json.dumps(value)
        if t == "float":
            v = float(value)
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v
        if t == "enum":
            allowed = spec.get("values", [])
            s = str(value).strip().upper()
            for a in allowed:
                if a.upper() == s:
                    return a
            return default
        if t == "list[str]":
            if isinstance(value, str):
                return [value]
            return [x if isinstance(x, str) else json.dumps(x) for x in value]
        if t == "list[object]":
            return list(value) if isinstance(value, list) else []
    except (TypeError, ValueError):
        return default
    return value


def ingest(payload: Any, name: str = "lens_report") -> dict:
    """Read a worker response into the contract's shape, BY KEY.

    `payload` is the decoded JSON object (or a raw string, decoded here). Returns a dict
    with every declared field present. On a parse failure the result carries ok=False and a
    `parse_error` naming the cause — a caller can then distinguish "the lens ran and found
    nothing" from "the lens produced nothing readable", which the old path could not."""
    c = load(name)
    fields = c["fields"]

    if isinstance(payload, (str, bytes)):
        text = payload.decode() if isinstance(payload, bytes) else payload
        payload = _decode(text)
        if payload is None:
            return _empty(c, parse_error="response contained no JSON object",
                          raw_len=len(text))
    if not isinstance(payload, dict):
        return _empty(c, parse_error=f"response was {type(payload).__name__}, expected object")

    out: dict[str, Any] = {}
    for key, spec in fields.items():
        if key == "findings":
            continue
        out[key] = _coerce(payload.get(key), spec)

    item_spec = fields["findings"]["item"]
    raw_items = _coerce(payload.get("findings"), fields["findings"])
    items: list[dict] = []
    dropped = 0
    for row in raw_items:
        if not isinstance(row, dict):
            dropped += 1
            continue
        f = {k: _coerce(row.get(k), s) for k, s in item_spec["fields"].items()}
        # A finding with no title carries no claim — count it rather than discarding it
        # silently, so "the model emitted junk" never looks like "the model found less".
        if not f.get("title"):
            dropped += 1
            continue
        items.append(f)
    out["findings"] = items
    out["parse_error"] = ""
    if dropped:
        out["dropped_findings"] = dropped
    return out


def _decode(text: str) -> dict | None:
    """Get the JSON object out of a response body. Returns None if there is none.

    With `response_format=json_object` the whole body IS the object and the first branch
    takes it. The remaining branches are TOLERANCE, not shape-detection: a provider that
    ignores json_output, a fallback provider that has no such mode, or a model that wraps
    its answer in a fence anyway. They only strip WRAPPING — the object's own shape is never
    inspected here; that is the contract's job in `ingest`.

    NOTE the balanced scan: the old code used `\\{[^{}]*"ok"...[^{}]*\\}`, and `[^{}]` cannot
    cross a nested brace, so it silently failed on every response carrying findings:[{...}].
    Nesting is not a regular language; count braces instead of pretending otherwise."""
    t = text.strip()
    # 1. The body is the object (json_object mode).
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 2. Strip a code fence, with or without a language tag.
    fence = re.search(r"```(?:json|JSON)?\s*\n(.*?)```", t, re.S)
    if fence:
        try:
            obj = json.loads(fence.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # 3. The first balanced {...} that parses as an object.
    for start in (i for i, ch in enumerate(t) if ch == "{"):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if esc:
                esc = False
            elif ch == "\\" and in_str:
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch == "{":
                depth += 1
            elif not in_str and ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
    return None


def _empty(c: dict, *, parse_error: str, raw_len: int = 0) -> dict:
    out = {k: s.get("default") for k, s in c["fields"].items()}
    out["ok"] = False
    out["findings"] = []
    out["parse_error"] = parse_error + (f" ({raw_len} chars)" if raw_len else "")
    return out


def response_format(name: str = "lens_report") -> dict | None:
    """The provider `response_format` this contract asks for, or None."""
    rf = load(name).get("response_format")
    return {"type": rf} if rf else None
