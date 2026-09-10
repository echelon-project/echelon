"""COS time-decay scoring — a pure leaf atom, no IO, no upward imports.

compute_score is the decay-weighted average that every score read re-derives from the
stored history. SCORE_BENCHMARK (B=100, neutral/unproven) and SCORE_LAMBDA (decay rate
~0.02/day) are the two constants that govern the shape of the curve.

cards.py imports these from .uame (re-exported there for backward compat); uame.py
imports them from HERE. No other module should import from this file directly — prefer
the re-exports at uame level so the import surface stays stable.
"""
from __future__ import annotations
import math
import time

SCORE_BENCHMARK: float = 100.0
SCORE_LAMBDA: float = 0.02

# Per-type decay rates: perishable atoms (project, reference) decay faster; durable atoms
# (feedback, user) decay slower — they are "truths about the user/process" that don't rot from
# being unread. DEFAULT: 0.02 (current behavior for untyped / unknown).
LAMBDA_BY_TYPE: dict[str, float] = {
    "feedback": 0.005,
    "user": 0.005,
    "project": 0.03,
    "reference": 0.04,
}
DEFAULT_LAMBDA: float = 0.02

# ── Dormant tier (2026-07-31) ────────────────────────────────────────────────
# Atoms whose effective_score falls below this threshold are DORMANT: demoted
# OUT of the default recall pool, NEVER deleted. A sharper/explicit query still
# reaches them (--include-dormant), and any earn lifts them back automatically.
# Rationale from live-bank histogram (7497 atoms, 2026-07-31):
#   - JUDGED_FLOOR = 75 (disclaimed atoms re-based here)
#   - View-decay floor = 85 (impressions alone never push below this)
#   - 80 sits BETWEEN them: disclaimed atoms (75) → dormant; view-decayed atoms
#     (≥85) → NOT dormant (trap 3: impressions alone cannot dormant an atom).
#   - Only deliberate negative signal (dispute/disclaim) or sustained time-decay
#     can push below 80.
DORMANT_THRESHOLD: float = 80.0

# Types that can NEVER be dormant (same as view-decay exempt: they are truths
# about the user/process that don't rot from being unread).
DORMANT_EXEMPT: frozenset[str] = frozenset({"feedback", "user"})


def lam_for(atom_type: str | None) -> float:
    """Return the per-type decay lambda, or DEFAULT_LAMBDA if unknown/None."""
    if atom_type and atom_type in LAMBDA_BY_TYPE:
        return LAMBDA_BY_TYPE[atom_type]
    return DEFAULT_LAMBDA


def compute_score(history: list, now: int | None = None,
                  lam: float = SCORE_LAMBDA, b: float = SCORE_BENCHMARK) -> float:
    """COS time-decay weighted average — score = B + Σ(δ·w)/Σ(w), w=e^(-λ·age_days)."""
    if not history:
        return b
    now = now if now is not None else int(time.time())
    num = den = 0.0
    # splat-unpack (audit, 2026-06-12): score_history entries are [ts, delta] (legacy) OR [ts, delta, src]
    # (provenance-tagged). compute_score is source-BLIND (every source weights identically — differential
    # weighting would silently move live scores = a relive-violation); src is read only by the gates. The
    # `*_` tolerates BOTH lengths forever (append-only: old 2-element rows are never rewritten).
    for ts, delta, *_ in history:
        w = math.exp(-lam * max(0.0, (now - ts) / 86400.0))
        num += delta * w
        den += w
    return b + (num / den if den else 0.0)
