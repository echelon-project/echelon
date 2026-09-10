"""struct_split — pure leaf: parse an atom's prose blob into structured fields.

A TINY LEAF (true-atom-is-a-tiny-leaf): single responsibility (split content -> spine/body fields),
stdlib only, no upward imports, no policy, leaf-local. The structured-atom compiler (CardStore) and
the migration both delegate HERE so the parse lives in one place.

The shape it parses is the AUTHORED one (ingest format): "[slug] <claim>\n\n⮕ <date>. <body...>".
Per the council's unanimous one-risk (structured-atom-additive-sidecar-schema), spine fields are
EXTRACTED FROM AUTHORED STRUCTURE, never freely inferred: slug+claim come from the author's
[tag]+description; directive ONLY from an explicit imperative marker; why/evidence from the body.
"""
from __future__ import annotations

import re

# "[slug] <rest>" — the author's tag + description. Slugs are kebab/colon coordinates.
_TAG_RE = re.compile(r"^\s*\[([a-z0-9][a-z0-9 _:-]*?)\]\s*(.*)", re.I | re.S)
# An explicit author-written imperative. ONLY these become a directive (nothing inferred).
_DIRECTIVE_RE = re.compile(
    r"(?m)^\s*(?:DIRECTIVE:|Do:|Don'?t:|⛔|✅|RULE:|THE FIX[^\n]*:)\s*(.+)$")
# Lines that read like dated/verified receipts -> evidence.
_EVIDENCE_RE = re.compile(r"verif|proven|measured|confirmed|regression|green|test|shipped|live", re.I)


def split_content(content: str) -> dict:
    """Parse one atom's prose blob into {slug, claim, directive, why, evidence, tagged}.

    Nothing is freely invented. slug+claim = the author's [tag] + description line; directive =
    the first explicit imperative marker if present, else ''; why = the dated body narrative;
    evidence = the body lines that look like receipts. `tagged` reports whether the [slug] shape
    was found (a caller can flag untagged atoms for review)."""
    text = (content or "").strip()
    m = _TAG_RE.match(text)
    if m:
        slug, rest, tagged = m.group(1).strip(), m.group(2).strip(), True
    else:
        slug, rest, tagged = "", text, False
    # claim = the description up to the first blank line or the ⮕ body marker.
    head = rest.split("\n⮕", 1)[0]
    claim_part, *body_parts = head.split("\n\n", 1)
    claim = claim_part.strip().replace("\n", " ")
    # body = everything after the claim (either via ⮕ or via double-newline)
    after_arrow = rest[len(head):].strip()
    body = (body_parts[0].strip() + ("\n\n" + after_arrow if after_arrow else "")).strip() if body_parts else after_arrow
    dirs = _DIRECTIVE_RE.findall(body) or _DIRECTIVE_RE.findall(head)
    directive = dirs[0].strip() if dirs else ""
    why = body
    evidence = " ".join(l.strip() for l in body.splitlines() if _EVIDENCE_RE.search(l))
    return {"slug": slug, "claim": claim, "directive": directive,
            "why": why, "evidence": evidence, "tagged": tagged}
