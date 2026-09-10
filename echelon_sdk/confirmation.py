"""L2 confirmation — insight earns trust by VOTES, or it stays a lie (owner, 2026-06-06).

THE LEVEL MODEL (owner's three-level memory architecture):
  L1 CORE/SOUL    — CV/WORLD, weight-stirring load-bearing sentences. PERMANENT. Shared. Guarded hardest.
  L2 INSIGHT      — experience-become-skill. WORKING state + a CONFIRMATION phase: an UNCONFIRMED
                    insight is a LIE (untrusted — invisible to warmth/offer until confirmed). A
                    breakthrough insight lives here, earns confirmation, then may promote to L1.
  L3 PERSONA      — per-user identity/bank, no guard (see bank + per-user partition). Not this file.

This file is L2's confirmation organ. Owner's design, verbatim shape:
  - A FEEDBACK system: a thumbs-up from an LLM = +1, a thumbs-down = -2 (asymmetric — a lie costs
    more than a truth gains). Scaled into the COS delta space here so votes feed the SAME score the
    rest of the substrate uses (no parallel ELO — confirmation IS COS resonance, driven by votes).
  - Every vote carries its SEQUENCE (reported, ordered) — anti-spam/anti-fake-vote: a vote whose seq
    is <= the voter's last seq is rejected (replay/duplicate). One monotonic seq per voter.
  - ONE-WAY: there is NO vote endpoint clients can push to. The ECHELON SERVER is the only collector
    (it observes/pulls votes); this module is the server-side sink. (The transport is the server's;
    here we enforce the sequence rule + the score effect, the parts that must be tamper-evident.)

"unconfirmed = lie" is load-bearing: warmth/offer must treat an unconfirmed L2 insight as NOT
trusted. is_confirmed() is the gate they consult.

See: cos-x-entry-coordinate-spine, knowledge-bank-cos-x-entry-rag, core-values-grow (promote on
proven resonance), memory-is-a-weight-adjustor.
"""
from __future__ import annotations
import json
import time

# Vote -> COS delta. Owner's 1 : -2 asymmetry, scaled into the delta space where ~+45 is strong
# resonance (store.py: q=95 -> +45). CRITICAL CALIBRATION: the COS score is a decay-weighted AVERAGE
# of deltas, not a sum — so the steady-state score from repeated up-votes is ~B + VOTE_UP_DELTA. For
# sustained up-votes to CLEAR the confirm bar (B+25=125), VOTE_UP_DELTA MUST exceed 25. At +30 the
# averaged score settles ~130 (> 125, confirms); a down-vote at -60 (the 1:-2 asymmetry) drags the
# average below B fast, so a lie sinks and must earn its way back. (Proven: 5 up -> ~130 confirms;
# a down-wave -> falls below the bar again. Trust is continuous, not a one-time stamp.)
VOTE_UP_DELTA = 30.0
VOTE_DOWN_DELTA = -60.0   # = -2 * up (the asymmetry)

# Confirmation gate: an L2 insight is CONFIRMED (trusted) when its decay-weighted COS score clears
# B+25 AND it has net-positive evidence over a minimum number of votes (not one fluke up-vote).
CONFIRM_SCORE = 125.0     # == store.PROMOTE_THRESHOLD (B+25, COS synthesis-eligibility)
CONFIRM_MIN_VOTES = 2     # at least this many votes counted (anti single-vote confirmation)

_VOTES_TABLE = "l2_votes"
_VOTES_COLS = """
    seed_id   TEXT NOT NULL,
    voter_id  TEXT NOT NULL,
    seq       INTEGER NOT NULL,     -- monotonic per voter; replay/duplicate (<= last) rejected
    vote      INTEGER NOT NULL,     -- +1 (up) or -1 (down)
    ts        INTEGER NOT NULL,
    PRIMARY KEY (seed_id, voter_id, seq)
"""


class ConfirmationLedger:
    """Server-side L2 vote sink: enforces per-voter sequence (anti-spam), feeds votes into the COS
    score, and answers is_confirmed(). Shares the store's UAME connection (one db, one lock)."""

    def __init__(self, store):
        self.store = store
        self.u = store.u
        with self.u._lock:
            self.u.conn.execute(f"CREATE TABLE IF NOT EXISTS {_VOTES_TABLE} ({_VOTES_COLS})")
            self.u.conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_l2votes_seed ON {_VOTES_TABLE}(seed_id)")
            self.u.conn.commit()

    def _last_seq(self, seed_id: str, voter_id: str) -> int:
        with self.u._lock:
            r = self.u.conn.execute(
                f"SELECT MAX(seq) m FROM {_VOTES_TABLE} WHERE seed_id=? AND voter_id=?",
                (seed_id, voter_id)).fetchone()
        return r["m"] if r and r["m"] is not None else -1

    def vote(self, seed_id: str, scope: str, voter_id: str, up: bool, seq: int) -> dict:
        """Record one vote (server-collected). REJECTS if seq <= the voter's last seq for this seed
        (replay/spam). On accept: append the vote (append-only ledger) AND feed the asymmetric delta
        into the seed's COS score history (so confirmation IS resonance, vote-driven). Returns the
        outcome incl. the new score and whether the insight is now confirmed."""
        if not voter_id or not isinstance(seq, int):
            return {"ok": False, "reason": "voter_id and integer seq required"}
        last = self._last_seq(seed_id, voter_id)
        if seq <= last:
            # the anti-spam wall: out-of-sequence or duplicate -> rejected, no score effect.
            return {"ok": False, "reason": f"seq {seq} <= last {last} (replay/spam rejected)",
                    "rejected": True}

        vote_val = 1 if up else -1
        delta = VOTE_UP_DELTA if up else VOTE_DOWN_DELTA
        now = int(time.time())
        with self.u._lock:
            self.u._retry(
                f"INSERT INTO {_VOTES_TABLE} (seed_id,voter_id,seq,vote,ts) VALUES (?,?,?,?,?)",
                (seed_id, voter_id, seq, vote_val, now))
            self.u.conn.commit()
        # feed the delta into the SAME COS score the substrate uses (no parallel ELO). reinforce()
        # appends a delta to score_history and recomputes the decay-weighted score; we map our vote
        # delta to its q-space: q = 50 + delta (so reinforce's (q-50)*k == our delta, k=1).
        # allow_promote=False is CONSTITUTIONAL: a confirmation vote may RAISE the score (confirm the
        # insight as L2 truth) but must NEVER auto-promote it into the L1 soul — promotion to L1 is
        # GRAND-VOTE-ONLY (grandvote.py). Otherwise a vote-driven score crossing the COS promote
        # threshold would silently elevate an insight to core, bypassing the collective.
        res = self.store.reinforce(seed_id, scope, q=50.0 + delta, allow_promote=False)
        confirmed = self.is_confirmed(seed_id, scope)
        return {"ok": True, "vote": vote_val, "score": res.get("score"),
                "confirmed": confirmed, "seq": seq}

    def vote_counts(self, seed_id: str) -> dict:
        with self.u._lock:
            rows = self.u.conn.execute(
                f"SELECT vote, COUNT(*) c FROM {_VOTES_TABLE} WHERE seed_id=? GROUP BY vote",
                (seed_id,)).fetchall()
        up = sum(r["c"] for r in rows if r["vote"] > 0)
        down = sum(r["c"] for r in rows if r["vote"] < 0)
        return {"up": up, "down": down, "total": up + down, "net": up - down}

    def is_confirmed(self, seed_id: str, scope: str) -> bool:
        """L2's trust gate: an insight is CONFIRMED when its COS score clears B+25 AND it has enough
        net-positive vote evidence. 'unconfirmed = lie' — warmth/offer consult this before trusting
        an L2 insight. A once-confirmed insight that starts collecting down-votes can fall BELOW the
        bar again (decay-weighted score sinks) — trust is continuous, not a one-time stamp."""
        seed = self.store.seed_by_id(seed_id) if hasattr(self.store, "seed_by_id") else None
        if seed is None:
            return False
        counts = self.vote_counts(seed_id)
        return (seed.score >= CONFIRM_SCORE
                and counts["total"] >= CONFIRM_MIN_VOTES
                and counts["net"] > 0)
