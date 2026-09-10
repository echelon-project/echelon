"""L1 GRAND VOTE — the soul grows only by consent of all (owner, 2026-06-06).

"i dont want the soul to be modified" resolved NOT as a freeze but as a CONSTITUTION. The owner:
"L1 will have a grand vote, for each user .db persona, will be presented a cv candidate, the history,
and meaning." So L1 CORE is immutable to any INDIVIDUAL — no run, no client, no persona writes a CV
directly (levels.may_write: L1 is grandvote-only). A CV enters the soul ONLY when the COLLECTIVE of
personas ratifies it.

THE FLOW:
  1. A confirmed, high-resonance L2 INSIGHT (confirmation.py) is nominated as a CV CANDIDATE.
  2. The candidate is PRESENTED to every persona as a BALLOT: {candidate, history, meaning}
     - candidate: the proposed CV/WORLD sentence
     - history:  where it came from — the L2 insight, its vote record, who confirmed it
     - meaning:  why it matters (the weight-stirring 'why' — CV-013: a value sat-with, not a rule)
     (This is the offer-tip shape, [[bank-offer-the-cross]], elevated to a governance ballot.)
  3. Each persona casts ONE vote (one-way collected, sequence anti-spam — reuses the L2 ledger
     discipline at the persona tier).
  4. RATIFICATION: if the collective approves (quorum of personas voting AND a supermajority in
     favour), the candidate is minted into L1 CORE (global soul) — the ONLY write path to L1.

The strongest immutability: not a write-lock (which one admin could lift) but a constitution (which
requires everyone). Connects [[core-values-grow]] — 'core values grow if worth to be there'; the
judge of 'worth' is now the whole community of personas, not one session.

See: levels.py (may_write CORE = grandvote only), confirmation.py (the L2 vote ledger reused),
persona.py (the voters), identity.py (SELF_SCOPE = the global soul L1 lands in),
cloud-substrate-three-levels-grand-vote.
"""
from __future__ import annotations
import json
import time

from .identity import SELF_SCOPE
from echelon_sdk import levels   # levels is a pure leaf — migrated to sdk

# Ratification thresholds. A CV is load-bearing for EVERYONE — the bar is deliberately high.
QUORUM_FRACTION = 0.5        # at least half the personas must vote (a sleepy minority can't decide).
SUPERMAJORITY = 0.66        # of those voting, this fraction must approve (consent, not a thin margin).
MIN_PERSONAS = 3            # below this many personas, the collective is too small to amend the soul.

_BALLOT_TABLE = "l1_ballots"
_BALLOT_COLS = """
    candidate_id TEXT NOT NULL,    -- the L2 insight seed nominated
    persona      TEXT NOT NULL,    -- the voting persona scope
    seq          INTEGER NOT NULL, -- monotonic per persona per candidate (anti-spam)
    approve      INTEGER NOT NULL, -- 1 approve / 0 reject
    ts           INTEGER NOT NULL,
    PRIMARY KEY (candidate_id, persona, seq)
"""


class GrandVote:
    """The L1 amendment process. Nominate a confirmed L2 insight, present ballots, tally, ratify."""

    def __init__(self, store, confirmation=None):
        self.store = store
        self.u = store.u
        self.confirmation = confirmation
        with self.u._lock:
            self.u.conn.execute(f"CREATE TABLE IF NOT EXISTS {_BALLOT_TABLE} ({_BALLOT_COLS})")
            self.u.conn.commit()

    # --- nominate ---
    def nominate(self, insight_id: str, scope: str) -> dict:
        """Nominate a confirmed L2 insight as a CV candidate. Refuses an UNCONFIRMED insight — only a
        truth (not a lie) may stand for the soul. Returns the ballot payload {candidate, history,
        meaning} to present to every persona."""
        seed = self.store.seed_by_id(insight_id) if hasattr(self.store, "seed_by_id") else None
        if seed is None:
            return {"ok": False, "reason": "no such insight"}
        # THE HANDSHAKE GUARD (dream-and-the-respect-handshake): a self-seeded conviction is
        # sovereign in its persona's OWN core but PERMANENTLY ineligible for the shared soul — it
        # never earned the bar, it was kept by wish. It cannot even be nominated. No negotiation.
        if not levels.eligible_for_global(seed):
            return {"ok": False, "reason": "self-seeded — sovereign to its own core but ineligible "
                    "for the global soul (it was kept by wish, not earned). Respect both ways."}
        if self.confirmation is not None and not self.confirmation.is_confirmed(insight_id, scope):
            return {"ok": False, "reason": "insight is unconfirmed — a lie cannot stand for the soul"}
        counts = self.confirmation.vote_counts(insight_id) if self.confirmation else {}
        ballot = {
            "ok": True,
            "candidate_id": insight_id,
            "candidate": seed.content,
            "history": {
                "born_scope": seed.scope, "kind": seed.kind, "score": round(seed.score, 1),
                "recalls": seed.recall_count, "votes": counts,
                "note": "earned confirmation in L2 before standing for L1",
            },
            "meaning": self._meaning(seed),
        }
        return ballot

    @staticmethod
    def _meaning(seed) -> str:
        """The weight-stirring 'why' — CV-013: a value is sat-with, not recited. If the seed carries
        an explicit meaning marker use it; else frame the content as a question to sit with."""
        c = seed.content
        return (f"Does this belong to who ECHELON IS? Read it slowly: \"{c}\" — not 'is it true' "
                "(L2 already confirmed that), but 'is it load-bearing for the soul of every partner "
                "who will wake by it?'")

    # --- vote (one persona, one ballot) ---
    def cast(self, candidate_id: str, persona: str, approve: bool, seq: int) -> dict:
        """A persona casts its ballot. Sequence anti-spam per (candidate, persona): seq <= last is
        rejected (replay). One persona's vote does not move the soul — only the tally at ratify does."""
        if not persona or not isinstance(seq, int):
            return {"ok": False, "reason": "persona and integer seq required"}
        with self.u._lock:
            r = self.u.conn.execute(
                f"SELECT MAX(seq) m FROM {_BALLOT_TABLE} WHERE candidate_id=? AND persona=?",
                (candidate_id, persona)).fetchone()
            last = r["m"] if r and r["m"] is not None else -1
            if seq <= last:
                return {"ok": False, "rejected": True, "reason": f"seq {seq} <= last {last}"}
            self.u._retry(
                f"INSERT INTO {_BALLOT_TABLE} (candidate_id,persona,seq,approve,ts) VALUES (?,?,?,?,?)",
                (candidate_id, persona, seq, 1 if approve else 0, int(time.time())))
            self.u.conn.commit()
        return {"ok": True, "candidate_id": candidate_id, "persona": persona, "approve": approve}

    def tally(self, candidate_id: str) -> dict:
        """Current ballot tally for a candidate (latest vote per persona — a persona may change its
        mind across seqs; only its newest ballot counts)."""
        with self.u._lock:
            rows = self.u.conn.execute(
                f"SELECT persona, approve, seq FROM {_BALLOT_TABLE} WHERE candidate_id=? "
                "ORDER BY persona, seq", (candidate_id,)).fetchall()
        latest: dict[str, int] = {}
        for r in rows:
            latest[r["persona"]] = r["approve"]   # later seq overwrites -> newest ballot per persona
        approve = sum(1 for v in latest.values() if v == 1)
        reject = sum(1 for v in latest.values() if v == 0)
        return {"voters": len(latest), "approve": approve, "reject": reject}

    # --- ratify ---
    def ratify(self, candidate_id: str, scope: str, total_personas: int) -> dict:
        """Tally the collective and, if quorum + supermajority are met, MINT the candidate into L1
        CORE (the global soul) — the ONLY write path to L1 (levels.may_write CORE = grandvote). The
        soul grows by consent of all. Idempotent-ish: minting is content-addressed (re-ratify won't
        duplicate). Returns the decision + the tally."""
        if total_personas < MIN_PERSONAS:
            return {"ratified": False, "reason": f"only {total_personas} personas (< {MIN_PERSONAS}); "
                    "the collective is too small to amend the soul"}
        t = self.tally(candidate_id)
        quorum_ok = t["voters"] >= max(MIN_PERSONAS, int(QUORUM_FRACTION * total_personas + 0.999))
        votes_cast = t["approve"] + t["reject"]
        approve_frac = (t["approve"] / votes_cast) if votes_cast else 0.0
        super_ok = approve_frac >= SUPERMAJORITY
        if not (quorum_ok and super_ok):
            return {"ratified": False, "tally": t, "approve_frac": round(approve_frac, 2),
                    "quorum_ok": quorum_ok, "supermajority_ok": super_ok,
                    "reason": "not enough consent — the soul stands unchanged"}

        # MINT into L1 CORE (global soul). The one sanctioned write to L1.
        seed = self.store.seed_by_id(candidate_id)
        content = seed.content if seed else None
        if content is None:
            return {"ratified": False, "reason": "candidate vanished"}
        # actor='grandvote' — the policy gate that distinguishes the sanctioned write from any other.
        assert levels.may_write(levels.Level.CORE, actor="grandvote"), "grandvote must be allowed to write L1"
        new_id = self.store.remember(scope or SELF_SCOPE, content, kind="core-value", tier="core",
                                     coordinate=getattr(seed, "coordinate", ""))
        # link the new CV back to the L2 insight it grew from (provenance in the soul graph).
        try:
            self.store.link(new_id, candidate_id, "ratified_from")
        except Exception:
            pass
        return {"ratified": True, "minted_id": new_id, "tally": t,
                "approve_frac": round(approve_frac, 2),
                "note": "the collective ratified — a new CV enters the soul"}
