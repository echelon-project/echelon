"""session_offer — experience from a DIRECT session, OFFERED to the author (the respect handshake).

THE GAP THIS CLOSES (owner, 2026-06-11): the trace-loop (trace_cards.record_run) earns v2 weight only
when work runs THROUGH the agent loop (loop.py) and produces a real EXECUTION TRACE. A direct session —
the OS reasoning + editing by hand, research, a design conversation — does real learning but produces NO
loop trace, so its experience earned NOTHING into v2 and was lost when the session ended ("lost seed from
experience, not memory").

THE HONEST FIX (owner: "the auto-created card will be OFFERED to the author, to promote or not — follow
our core EXACTLY"): we do NOT let a direct session grade its own homework (the cardinal v1 sin that v2
exists to forbid — significance-by-fiat). Instead:

  1. RECORD the episode's cards at Q=50 NEUTRAL — the episode HAPPENED, so the cards exist (same rule as
     trace_cards for a cold/no-baseline run), but they move NO weight. No assertion, no self-rating.
  2. OFFER them as PENDING — surfaced to the author. The author is the CONSENT GATE
     (dream-and-the-respect-handshake: proposals only, consent decides, NEVER self-mint, NEVER delete).
  3. On PROMOTE: a single capped CONSENT bump (a fixed Q, not an author-chosen magnitude) flows through
     the NORMAL reinforce_card path. This is weaker than a trace-earned Q on purpose — the author CONSENTS
     the episode is worth keeping warm; that is not the same claim as "the execution PROVED it." The card
     is flagged consent_kept so the borrow is honest (relive-dont-migrate): kept-by-consent, not earned.
  4. On DECLINE: nothing. The neutral card stays and DECAYS naturally (nothing is ever deleted — the
     substrate law). An un-promoted episode simply never earned rank — the honest default.

So a direct session can finally feed v2 — but only THROUGH a consent gate, never by self-grade. This is
the card-tier analogue of grandvote.py (which gates the L1 soul); here the author gates their OWN core.
See: trace_cards.py, cards.py (§7-P2 the keystone), dream-and-the-respect-handshake, relive-dont-migrate,
v2-is-primary-v1-is-cold-borrow, the-card-layer-must-be-chained.
"""
from __future__ import annotations

import json
import time

from .cards import CardStore, DEFAULT_V2_DB, SCORE_K

# The CONSENT Q. Deliberately MODEST: an author consenting "keep this warm" is a weaker signal than a
# real execution trace, so it must not mint as much weight as a proven run can. Centred-delta is
# (CONSENT_Q - 50) * SCORE_K = +12 — enough to lift a neutral card off the floor, far below the +25
# SYNTH_ELIGIBLE bar a card must EARN through actual reuse. Consent opens the door; use walks through it.
CONSENT_Q = 62.0

_OFFER_TABLE = "session_offers"
_OFFER_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_OFFER_TABLE} (
    card_id   TEXT PRIMARY KEY,   -- the neutral card recorded for this episode (v2 cards.id)
    session   TEXT NOT NULL,      -- the session/goal key the episode belongs to
    summary   TEXT NOT NULL,      -- a human-readable one-line of what the episode was (for the offer UI)
    status    TEXT NOT NULL,      -- 'pending' | 'promoted' | 'declined'
    ts        INTEGER NOT NULL,   -- when offered
    decided_ts INTEGER            -- when promoted/declined (NULL while pending)
);
"""


class SessionOffer:
    """Records a direct session's episodes as NEUTRAL cards + a PENDING offer, and applies the author's
    promote/decline consent. Shares the CardStore (and its db/lock) — the offers live beside the cards."""

    def __init__(self, store: CardStore | None = None, db_path: str = DEFAULT_V2_DB):
        self.cards = store if store is not None else CardStore(db_path)
        with self.cards._lock:
            self.cards.conn.executescript(_OFFER_SCHEMA)
            self.cards.conn.commit()

    def record_episode(self, session: str, summary: str, coords: list[str],
                       prev_card_id: str = "") -> str:
        """Record ONE episode of a direct session as a neutral card (Q unmoved) + a pending offer.
        `coords` = the atom coordinates this episode loaded/touched (the card's refs — same grain as a
        trace step). Returns the card id. Idempotent: the card is content-addressed (re-recording the
        same episode no-ops the card) and the offer is keyed on card_id (INSERT OR IGNORE)."""
        if not coords:
            return ""   # an episode that loaded nothing has no refs to compose — nothing to offer.
        label = f"{(session or 'session')[:40].strip()}:offer"
        card_id = self.cards.add_card(label, coords, born_from="session-offer", prev=prev_card_id)
        with self.cards._lock:
            self.cards.conn.execute(
                f"INSERT OR IGNORE INTO {_OFFER_TABLE} (card_id,session,summary,status,ts) "
                "VALUES (?,?,?, 'pending', ?)",
                (card_id, session, summary[:300], int(time.time())))
            self.cards.conn.commit()
        return card_id

    def pending(self, session: str | None = None) -> list[dict]:
        """The offers awaiting the author's decision — the consent queue. Optionally scoped to one
        session. Each row carries enough to PRESENT the offer (summary + the card's refs + its current
        neutral score), so the author decides on substance, not blind."""
        q = f"SELECT card_id,session,summary,ts FROM {_OFFER_TABLE} WHERE status='pending'"
        args: tuple = ()
        if session:
            q += " AND session=?"
            args = (session,)
        q += " ORDER BY ts"
        with self.cards._lock:
            rows = self.cards.conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            c = self.cards.card(r["card_id"])
            out.append({
                "card_id": r["card_id"], "session": r["session"], "summary": r["summary"],
                "ts": r["ts"], "refs": (c.refs if c else []),
                "score": round(c.score, 1) if c else None,
            })
        return out

    def promote(self, card_id: str) -> dict:
        """The author CONSENTS to keep this episode warm. Applies ONE capped consent bump through the
        normal reinforce_card path (so atoms earn their §7-P2 partial share honestly) and flags the
        offer 'promoted'. NOT a self-grade: the magnitude is fixed (CONSENT_Q), the author only chooses
        whether, not how much. Refuses a non-pending offer (idempotent — no double-credit)."""
        with self.cards._lock:
            r = self.cards.conn.execute(
                f"SELECT status FROM {_OFFER_TABLE} WHERE card_id=?", (card_id,)).fetchone()
        if r is None:
            return {"ok": False, "reason": "no such offer"}
        if r["status"] != "pending":
            return {"ok": False, "reason": f"already {r['status']} — consent is applied once"}
        # source='consent' (audit Hole 4, 2026-06-12): author say-so, NOT a trace. This is the LIVE LEAK
        # fix — without it, a consent bump (CONSENT_Q>50, a positive tip) would REDEEM a disclaimed lie by
        # say-so + grant the shock bonus. Only source=='trace' may redeem; consent earns honestly but the
        # gate refuses it as a redeemer.
        res = self.cards.reinforce_card(card_id, CONSENT_Q, source="consent")
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason", "reinforce failed")}
        with self.cards._lock:
            self.cards.conn.execute(
                f"UPDATE {_OFFER_TABLE} SET status='promoted', decided_ts=? WHERE card_id=?",
                (int(time.time()), card_id))
            self.cards.conn.commit()
        return {"ok": True, "card_id": card_id, "consent_q": CONSENT_Q,
                "new_score": round(self.cards.card(card_id).score, 1),
                "note": "kept by author CONSENT (not trace-earned) — a modest warmth, earns its rank by reuse"}

    def decline(self, card_id: str) -> dict:
        """The author declines. Nothing is minted; the neutral card STAYS and decays naturally (nothing
        is ever deleted — the substrate law). Records the decision so it leaves the pending queue."""
        with self.cards._lock:
            r = self.cards.conn.execute(
                f"SELECT status FROM {_OFFER_TABLE} WHERE card_id=?", (card_id,)).fetchone()
            if r is None:
                return {"ok": False, "reason": "no such offer"}
            if r["status"] != "pending":
                return {"ok": False, "reason": f"already {r['status']}"}
            self.cards.conn.execute(
                f"UPDATE {_OFFER_TABLE} SET status='declined', decided_ts=? WHERE card_id=?",
                (int(time.time()), card_id))
            self.cards.conn.commit()
        return {"ok": True, "card_id": card_id,
                "note": "declined — card left neutral to decay (never deleted)"}
