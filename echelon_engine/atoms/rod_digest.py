#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
rod_digest — ZERO-model structural feature extractor (a true atom: pure, tiny, leaf).

The CORRECTED council insight (owner): rods DON'T judge, they DETECT/EXTRACT. The
cheapest possible rod is a pure string-op (zero model, zero VRAM): pull the structured
signals that disambiguate active-now — date, supersede/archive keywords, live-now
keywords, dated-ledger shape. These get APPENDED to the atom before the cone reads it,
so the cone judges a PRE-DIGESTED atom (sharper + faster) instead of raw text.
Rods enrich; the cone reads.

Single responsibility: given atom content text → return a short feature string.
No model calls. No imports beyond stdlib.
"""
import re

# ---- compiled regexes (module-level: compile once) ----
_RE_DATE      = re.compile(r"20\d\d-\d\d-\d\d|20\d\d[01]\d[0-3]\d")
_RE_SUPERSEDE = re.compile(r"supersed|revert|reverses|obsolete|deprecat", re.I)
_RE_ARCHIVE   = re.compile(r"archiv|destroyed|pre-cutover|disabled|stopped\b", re.I)
_RE_LIVE      = re.compile(r"still in force|still applied|in force|live now|currently|right now", re.I)
_RE_LEDGER    = re.compile(r"session-20|ledger|SESSION 20")


def rod_digest(content: str) -> str:
    """ZERO-model rod: extract cheap structured signals from an atom. Returns a short
    feature string to append to the atom for the cone to read."""
    feats = []
    d = _RE_DATE.search(content)
    if d:
        feats.append(f"date={d.group(0)}")
    if _RE_LEDGER.search(content[:90]):
        feats.append("dated-session-ledger(decays-by-age)")
    if _RE_SUPERSEDE.search(content):
        feats.append("mentions-supersede/revert")
    if _RE_ARCHIVE.search(content):
        feats.append("mentions-archive/destroy")
    if _RE_LIVE.search(content):
        feats.append("mentions-still-in-force")
    return "; ".join(feats) if feats else "no structural markers"
