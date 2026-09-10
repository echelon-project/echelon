"""Affect — derive a NAMED emotion from the valence/arousal canvas (the circumplex model).

Owner, 2026-06-05: "good and bad are not only the label it should have. try to use emotions
on human as the canvas." Warmth is not a scalar and not a good/bad flag — it's an EMOTIONAL
reading. We store two continuous axes (valence: unpleasant<->pleasant, arousal: calm<->intense)
and DERIVE the named feeling from where a reading lands. Emotions emerge from the canvas, not a
hardcoded list — extensible, no in-between blur. Each named emotion carries an ACTION-TENDENCY,
which is the whole point: the feeling tells the model what to DO. See warmth-is-emotional.

Recognition is folded in too: how strongly the current thought matches a past seed (the
warmth `score`) decides whether we're in KNOWN territory (emotions of recognition) or NEW
territory (emotions of novelty). So affect takes (score, valence, arousal).
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Affect:
    emotion: str        # the derived named feeling
    tendency: str       # what the feeling inclines the model to DO (the action-tendency)
    valence: float
    arousal: float


# Bands. recognition (warmth score) splits known vs new; valence splits good vs bad;
# arousal splits intense vs mild. The named emotion sits where they cross.
_KNOWN = 0.45     # at/above: this is recognized territory
_FAINT = 0.15     # below: essentially new/unrecognized


def derive(score: float, valence: float, arousal: float) -> Affect:
    """Map (recognition, valence, arousal) -> a named emotion + its action-tendency."""
    recognized = score >= _KNOWN
    faint = score < _FAINT
    intense = arousal >= 0.5
    pleasant = valence > 0.15
    unpleasant = valence < -0.15

    if recognized:
        if unpleasant:
            # been here AND it hurt — the gz warning, the Haiku drift. The key case.
            if intense:
                return Affect("dread", "AVOID — you recognize this and it burned before; do otherwise, do NOT repeat the path", valence, arousal)
            return Affect("regret", "RECONSIDER — you've been here and it went poorly; choose differently this time", valence, arousal)
        if pleasant:
            if intense:
                return Affect("conviction", "ACT — strong positive recognition; commit to the known path", valence, arousal)
            return Affect("confidence", "RE-TREAD — you solved this before; reuse it, don't re-derive", valence, arousal)
        # recognized but emotionally neutral
        return Affect("familiarity", "RE-TREAD — known territory; lean on what you did before", valence, arousal)

    if faint:
        if unpleasant:
            return Affect("unease", "SLOW DOWN — something feels off / wrong recall; re-anchor before proceeding", valence, arousal)
        return Affect("curiosity", "EXPLORE — new ground, no prior history; lean in and leave a seed when resolved", valence, arousal)

    # lukewarm middle — partial, unresolved recognition
    if unpleasant:
        return Affect("wariness", "PROCEED CAREFULLY — partial match to something that went badly; verify before committing", valence, arousal)
    return Affect("intrigue", "CHECK — partial recognition; look at the warmest seed, you may be near a known path", valence, arousal)
