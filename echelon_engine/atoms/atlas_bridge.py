"""Atlas bridge — turn a component-atlas's cards into CHARGED UAME seeds.

Owner, 2026-06-05: "if our global uame keep accepting the seed, that connection to the
atlas-component would be an instant guide for emotion warmth." The component-atlas already
encodes, per card, a deliberate human JUDGMENT — status (live/empty_state/partial), a
gap_reason, the role/what_it_is. That judgment IS pre-loaded emotional charge. We don't
re-judge it (the atlas already did); we TRANSLATE recorded status -> felt charge, so an
agent entering the scope feels the dashboard's whole emotional terrain on arrival — no
cold-start, no trial-and-error discovery.

Same-infrastructure soul-port: the component-atlas spine is ECHELON's spine verbatim, so
its cards drop straight into UAME as seeds of the scope. The map IS the felt memory.
Status-driven charge table (deterministic, $0): the atlas fields drive the emotion directly.
"""
from __future__ import annotations
import json
from pathlib import Path

from .store import SeedStore

# status -> (valence, arousal, emotion-label). The recorded judgment becomes felt charge.
#   live      = a solved, working surface  -> confidence (reuse, it works)
#   empty_state = can't be built yet (gap)  -> wariness  (don't promise this; the why is gap_reason)
#   partial   = half-there                  -> caution   (verify before committing)
#   planned   = intended, not built         -> curiosity-leaning (open ground)
_STATUS_CHARGE = {
    "live":        (0.5, 0.2),
    "confirmed":   (0.5, 0.2),
    "empty_state": (-0.4, 0.5),
    "partial":     (-0.2, 0.4),
    "planned":     (0.1, 0.3),
    "degraded":    (-0.5, 0.6),
}
_DEFAULT_CHARGE = (0.0, 0.0)


def seed_scope_from_atlas(define_dir: Path | str, scope: str, store: SeedStore) -> int:
    """Mint one charged seed per component card under define_dir into `scope`.

    Returns the count written. Idempotent: content-addressed dedup means re-running on an
    unchanged atlas writes nothing new. Re-run after the atlas changes to refresh the terrain."""
    define_dir = Path(define_dir)
    written = 0
    for card_path in sorted(define_dir.glob("*.json")):
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        cid = card_path.stem
        status = (card.get("status") or "").lower()
        valence, arousal = _STATUS_CHARGE.get(status, _DEFAULT_CHARGE)

        # The seed content = what the agent should RECOGNIZE about this surface. For a gap,
        # the gap_reason IS the "why it hurts" — the heart of the wariness seed.
        what = card.get("what_it_is") or card.get("role") or card.get("title") or cid
        line = f"[{cid}] ({card.get('type','?')}, {status or 'unknown'}) {what}"
        if status in ("empty_state", "partial") and card.get("gap_reason"):
            line += f" — GAP: {card['gap_reason']}"
        if card.get("data_source"):
            line += f" [data: {card['data_source']}]"

        store.remember(scope, line, kind="component", valence=valence, arousal=arousal)
        written += 1
    return written
