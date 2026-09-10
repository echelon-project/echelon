"""bakeoff — paired, sealed, severity-gated model comparison.

WHAT THIS ANSWERS (three causal questions, not five labels that look different):
  1. EFFORT  — on v4-flash, does low -> high -> max buy quality worth its cost?
  2. MODEL   — at the same effective `high`, does v4-pro beat v4-flash?
  3. SWARM   — does 3x cheap workers + a strong arbiter beat one strong single run?

WHY THE CELLS ARE WHAT THEY ARE. DeepSeek maps requested effort low->low, medium->high,
high->high, xhigh->high, max->max. So a design with separate `medium` and `xhigh` cells is
running duplicate treatments and calling them distinct — the v1 mistake. Only low / high / max
are real, and providers/deepseek.py:_EFFORT_MAP enforces that mapping on the wire.

THE DESIGN IS PAIRED: every cell answers the SAME sealed items, so comparisons are
within-item and the noise of "which questions did it get" cancels out.

WHAT THIS MODULE DOES *NOT* DO: it does not write benchmark items. An item's expected value
is domain ground truth; a harness that authors both the question and the key is grading its
own homework, which is the failure this estate has an atom about. Items are authored and
SEALED by the owner; this module validates, runs, scores, and refuses to proceed on a set
that does not meet the schema.
"""
from __future__ import annotations

from .items import Item, load_items, validate_item, seal_hash
from .cells import CELLS, Cell

__all__ = ["Item", "load_items", "validate_item", "seal_hash", "CELLS", "Cell"]
