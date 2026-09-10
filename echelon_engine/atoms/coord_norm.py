"""coord_norm — coordinate normalisation leaf.

Pure function: no DB, no imports beyond stdlib re. A coordinate is the
substrate's identity key — normalised to lowercase, non-alnum runs
collapsed to '_', segments joined by ':'. Extracted verbatim from cards.py.
"""
from __future__ import annotations
import re

_COORD_RE = re.compile(r"[^a-z0-9:]+")


def norm_coord(c: str) -> str:
    """Normalise a raw coordinate string to the canonical form used in DB storage.

    Each ':'-separated segment is lowercased, non-alnum characters replaced
    with '_', and leading/trailing underscores stripped.  Empty segments are
    dropped.  The same logic lives in Card.__post_init__ and Atom.__post_init__
    via the internal alias _norm_coord; this module exposes the canonical name.
    """
    segs = [_COORD_RE.sub("_", s.strip().lower()).strip("_") for s in (c or "").split(":") if s.strip()]
    return ":".join(s for s in segs if s)


# Internal alias kept for backward-compatibility with cards.py's own usage.
_norm_coord = norm_coord
