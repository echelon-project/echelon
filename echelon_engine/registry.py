"""registry — the SINGLE controlled vocabulary for card provenance-class (CARD_KIND).

WHY THIS EXISTS (root-cause fix, 2026-07-22): relive.recent_arc_cards filtered cards by
`born_from LIKE 'wrap-session%'`. But born_from is FREE NARRATIVE — several features pack
structured data into it (plan_cache: 'plan-cache|goal=...', cartridge: 'cartridge:<scope>|',
the disclaim antibody: 'judged:disclaimed'). Since 2026-07-12 wrap wrote narrative born_from
values ('session-wrap-...', 'owner: ...', raw hex ids), so ~20 sessions became invisible to
relive with ZERO errors. The industry-standard fix (Percona/RDF/schema.org controlled-vocab):
the MACHINE classifier is a stable CODE in its OWN column, the human label lives BESIDE it,
never IN it. This is the exact discipline `atom_links.RELATION_TYPES` already gives EDGES —
this file gives the card `born_from` classifier the same governance its twin already has.

BANDED CODES (the mature-system lesson: HTTP status class = code//100). A taxonomy of
kinds-of-thing saturates in the LOW HUNDREDS even at scale — 1M USERS create 1M CARDS, not
1M KINDS (that is the whole point of a controlled vocabulary). Capacity was never the
constraint; RENUMBER-SAFETY is. So codes are grouped by provenance CLASS in 100-bands, and a
new source in year 6 lands in the right band as a fresh code — you NEVER renumber, never
collide, and a filter can match a whole family (`is_session` = 100 <= code < 200).

NAMESPACE NOTE: this is CARD-kind (provenance class of an arc/action card), a governed INT.
It is DISTINCT from atoms.kind (a free TEXT content-type: 'note'/'lesson'/'component'). Do
not conflate them — different column, different table, different vocabulary.

Both echelon_engine AND echelon_sdk import THIS module — one source of truth, no re-declare.
"""
from __future__ import annotations

# ── CARD_KIND: code -> canonical name. Banded by provenance class (code // 100). ──────────
CARD_KIND: dict[int, str] = {
    0:   "unknown",          # unclassified / legacy — never a demotion, just "not yet coded"

    # 100s — SESSION LIFECYCLE (the arc-cards relive walks)
    100: "wrap",             # a /wrap-session arc-card (the canonical session unit)
    101: "relive",           # a /relive-resumed session's arc-card (a branch off a root)
    102: "resume",           # a resumed/continued session
    109: "session_inferred", # NO session marker in born_from, but session STRUCTURE (prev-chained
                             # + multi-ref chain). Recoverable by relive (is_session true) but the
                             # provenance is honestly "inferred", not a verified wrap marker.

    # 110s — SESSION OFFERS (candidate lessons awaiting a promote/decline decision)
    110: "offer",            # session_offer card (born_from='session-offer')

    # 200s — REASONING ARTIFACTS (cards born from thought, not a session)
    200: "trace",            # earned from an execution trace (born_from='trace')
    210: "think",            # crystallize() of a think() chain that paid off
    220: "synthesis",        # a synthesized higher-order card
    230: "plan_cache",       # plan_cache put() (born_from='plan-cache|goal=...')

    # 300s — CAPABILITY / EQUIPMENT
    300: "cartridge",        # a composed cartridge card (born_from='cartridge:<scope>|...')
    310: "self_seed",        # self-knowledge bootstrap card (born_from='self_seed')
    320: "skill",            # a skill-definition card

    # 400s — CORRECTION / IMMUNE (the honesty layer)
    400: "judged",           # a model-judged card ('judged:...' marks)
    410: "disclaimed",       # a card disclaimed as a lie
    420: "dispute",          # a dispute/correction card

    # 500s+ — reserved for future provenance classes (integrations, external imports, ...).
    # A new source lands here in a fresh band; nothing above ever renumbers.
}

# reverse: every alias (and the canonical name) -> code. The classifier's lexicon. A born_from
# marker or a caller-supplied string is matched against these. Multiple aliases may map to one
# code (the whole reason born_from string-matching was fragile — many spellings, one meaning).
_ALIASES: dict[str, int] = {}


def _register(code: int, *names: str) -> None:
    for n in names:
        _ALIASES[n.lower()] = code


# canonical names auto-register
for _c, _n in CARD_KIND.items():
    _ALIASES[_n] = _c

# the messy REAL-WORLD spellings that must all resolve to one code (measured from the bank:
# 'session-wrap-2026-07-21', 'session_wrap_...', 'wrap-session 2026-07-12', etc.)
_register(100, "wrap", "wrap_session", "wrap-session", "session-wrap", "session_wrap", "session")
_register(101, "relive", "relive_branch", "forked_from")
_register(102, "resume")
_register(109, "session_inferred")
_register(110, "offer", "session-offer", "session_offer")
_register(200, "trace")
_register(210, "think", "crystallize", "crystallise")
_register(220, "synthesis", "synthesize", "synthesise")
_register(230, "plan_cache", "plan-cache")
_register(300, "cartridge")
_register(310, "self_seed", "self-seed")
_register(320, "skill")
_register(400, "judged")
_register(410, "disclaimed", "disclaim")
_register(420, "dispute", "disputed")


def code_for(alias: str) -> int:
    """Resolve a single alias/name to its CARD_KIND code, or 0 (unknown) if unrecognised."""
    return _ALIASES.get((alias or "").strip().lower(), 0)


def name_of(code: int) -> str:
    """The canonical name for a code (''-safe: unknown code -> 'unknown')."""
    return CARD_KIND.get(int(code), "unknown")


def class_of(code: int) -> int:
    """The provenance BAND (code // 100 * 100): 100=session, 200=reasoning, 300=capability,
    400=correction. Lets a filter match a whole family without listing each member."""
    return (int(code) // 100) * 100


def is_session(code: int) -> bool:
    """True for any session-lifecycle card (the family relive --list surfaces): 100 <= code < 110
    are the arc-card kinds (wrap/relive/resume/session_inferred). Offers (110) are NOT session
    arcs — they're candidates, excluded from relive just as the old born_from filter excluded them."""
    return 100 <= int(code) < 110


# codes relive treats as a walkable session arc-card (wrap/relive/resume/inferred — NOT offer)
SESSION_ARC_CODES: tuple[int, ...] = (100, 101, 102, 109)


def classify(born_from: str) -> int:
    """Classify a card's born_from narrative into a CARD_KIND code. THE core function: wrap
    stamps kind=classify(born_from) at write; the backfill runs it over legacy cards. Matching
    is longest-marker-first so 'plan-cache|goal=x' hits 230 before a bare token could mislead.

    Returns 0 (unknown) when nothing matches — an HONEST default, never a guess. A narrative
    that classifies to 0 but should have a code is exactly what the immune rule flags."""
    bf = (born_from or "").strip().lower()
    if not bf:
        return 0
    # 1) structured-prefix markers (the packed-payload born_froms) — check these FIRST, longest
    #    conceptual match wins, because 'plan-cache|...' also contains no bare alias.
    _PREFIX = [
        ("plan-cache", 230), ("cartridge:", 300), ("session-offer", 110),
        ("self_seed", 310),
        ("wrap-session", 100), ("session-wrap", 100), ("session_wrap", 100),
        ("relive", 101), ("resume", 102),
    ]
    for marker, code in _PREFIX:
        if bf.startswith(marker):
            return code
    for marker, code in _PREFIX:
        if marker in bf:
            return code
    # 2) disclaim/judge marks (substring anywhere — the antibody's own convention)
    if "judged:" in bf:
        return 410 if "disclaim" in bf else 400
    if "dispute" in bf or "superseded" in bf:
        return 420
    # 3) bare leading token (e.g. 'trace', 'wrap', 'skill')
    head = bf.replace("|", " ").split()[0] if bf.split() else ""
    c = code_for(head)
    if c:
        return c
    # 4) whole-string alias (single-word born_from)
    return code_for(bf)


__all__ = ["CARD_KIND", "code_for", "name_of", "class_of", "is_session",
           "classify", "SESSION_ARC_CODES"]
