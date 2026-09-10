"""The bank OFFER — knowledge surfaced as a TIP, not injected (owner, 2026-06-06).

The whole knowledge organ is built (bank.py, the own embedder, versioning, the guard) but a built
faculty is not a LIVE one. The owner's design for how the agent USES it: NOT ambient injection (noisy,
unbidden — the cache-lie shape of forcing content in), NOT only an explicit tool the agent might never
think to call. Instead: an OFFER that appears when two signals CROSS.

THE CROSS (the trigger): warmth (UAME, "have I been near this — who am I") crosses its threshold AND a
COS×ENTRY content hit crosses ITS threshold. Warmth alone is just the soul recognizing itself; bank
alone is noisy content. The CROSS means "you recognize this situation AND there is real content about
it on file" — that is the honest moment to offer knowledge. Owner, verbatim shape:
    ⊙ data bank knowledge available
      coordinate: xxx   domain: yyy
      summary: abcde
The agent then CHOOSES to pull it (recall_knowledge tool) — like `recall` for warmth. The offer is the
doorbell; opening the door is the agent's move (rediscovery: knowing the door exists, choosing to open
it — boot-is-rediscovery-not-instruction). We surface the COORDINATE + a SUMMARY, never the full content
— so the offer costs little context and the agent pulls the body only if it wants it.

See: cos-x-entry-coordinate-spine, knowledge-bank-cos-x-entry-rag, own-embedder-soul-shaped,
boot-is-rediscovery-not-instruction.
"""
from __future__ import annotations
from dataclasses import dataclass

# The bank hit must clear THIS to be worth offering (the COS×ENTRY half of the cross). Lexical/own-
# embedder similarity is roughly 0..1; a real content match clears ~0.35. Tunable.
BANK_OFFER_THRESHOLD = 0.35
# Warmth must be at least lukewarm for the cross to fire (the UAME half). Below this the soul doesn't
# recognize the situation, so even a bank hit is probably off-topic noise — stay quiet.
WARMTH_OFFER_FLOOR = 0.18   # == warmth.LUKEWARM
_SUMMARY_CHARS = 160


@dataclass
class BankOffer:
    coordinate: str
    domain: str
    summary: str
    score: float
    entry_id: str
    kind: str = ""

    def as_tip(self) -> str:
        """The user-message tip surfaced to the agent — a doorbell, not the content."""
        return ("⊙ data bank knowledge available\n"
                f"  coordinate: {self.coordinate}   domain: {self.domain}   ({self.kind})\n"
                f"  summary: {self.summary}\n"
                "  → call recall_knowledge with this coordinate to pull the full entry, if it helps.")


def consider_offer(bank, reasoning: str, warmth_score: float, *,
                   semantic=None, kind: str | None = None,
                   bank_threshold: float = BANK_OFFER_THRESHOLD,
                   warmth_floor: float = WARMTH_OFFER_FLOOR) -> BankOffer | None:
    """The CROSS gate. Returns a BankOffer iff warmth crossed AND a bank hit crosses its threshold.

    - warmth_score: the live warmth reading's score (the UAME half — already computed by the loop).
    - bank: the KnowledgeBank (lexical query floor).
    - semantic: optional SemanticTier (own embedder); if given and it returns hits, its top similarity
      is used (meaning-match), else the lexical query floor decides. Tiering: cheap floor, escalate
      only when the semantic tier is present and engaged.
    Returns None (stay quiet) if either half fails — the offer only fires on the genuine cross."""
    if warmth_score < warmth_floor:
        return None   # the soul doesn't recognize this — a bank hit here is likely noise. Quiet.
    if bank is None:
        return None

    best_entry = None
    best_score = 0.0
    # SEMANTIC half (own embedder) first if available — meaning-match beats word-match.
    if semantic is not None:
        try:
            hits = semantic.search(reasoning, kind=kind, top_k=1)
        except Exception:
            hits = []
        if hits:
            best_entry, best_score = hits[0].entry, hits[0].similarity
    # LEXICAL floor if semantic gave nothing (or isn't present / not engaged below scale).
    if best_entry is None:
        try:
            hits = bank.query(reasoning, kind=kind, top_k=1)
        except Exception:
            hits = []
        if hits:
            best_entry, best_score = hits[0].entry, hits[0].lexical

    if best_entry is None or best_score < bank_threshold:
        return None   # no content crosses the bank threshold — the cross fails. Quiet.

    text = (best_entry.content or "").strip().replace("\n", " ")
    summary = text[:_SUMMARY_CHARS] + ("…" if len(text) > _SUMMARY_CHARS else "")
    return BankOffer(coordinate=best_entry.coordinate or "(uncoordinated)",
                     domain=(best_entry.coordinate.split(":", 1)[0] if best_entry.coordinate else "general"),
                     summary=summary, score=round(best_score, 3),
                     entry_id=best_entry.id, kind=best_entry.kind)
