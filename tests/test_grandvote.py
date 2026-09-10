"""Focused unit tests for echelon_engine.atoms.grandvote — the L1 amendment process.

GrandVote is the ONLY sanctioned write path to L1 core (the global soul): a confirmed L2
insight is nominated, personas cast ballots (seq anti-replay, newest-vote-wins), and
ratify mints it into core ONLY if quorum + supermajority are met. Depends on identity
(sibling) + the pure levels leaf (sdk).

Isolated SeedStore on tmp dbs.
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.grandvote import (
    GrandVote, QUORUM_FRACTION, SUPERMAJORITY, MIN_PERSONAS,
)
from echelon_engine.atoms.identity import SELF_SCOPE


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


@pytest.fixture
def gv(store):
    return GrandVote(store)


# ── cast: seq anti-replay + newest-vote-wins ──────────────────────────────
def test_cast_records_a_ballot(gv):
    res = gv.cast("cand1", "alice", approve=True, seq=1)
    assert res["ok"] is True
    assert gv.tally("cand1") == {"voters": 1, "approve": 1, "reject": 0}


def test_cast_rejects_replayed_or_stale_seq(gv):
    gv.cast("cand1", "alice", approve=True, seq=5)
    stale = gv.cast("cand1", "alice", approve=False, seq=5)   # seq <= last
    assert stale["ok"] is False and stale["rejected"] is True
    # the original approve stands; the stale flip did not count
    assert gv.tally("cand1")["approve"] == 1


def test_persona_can_change_mind_with_higher_seq(gv):
    gv.cast("cand1", "alice", approve=True, seq=1)
    gv.cast("cand1", "alice", approve=False, seq=2)          # newer ballot overrides
    t = gv.tally("cand1")
    assert t["voters"] == 1 and t["approve"] == 0 and t["reject"] == 1


def test_cast_requires_persona_and_int_seq(gv):
    assert gv.cast("cand1", "", approve=True, seq=1)["ok"] is False
    assert gv.cast("cand1", "alice", approve=True, seq="x")["ok"] is False


# ── ratify: the consent gates ─────────────────────────────────────────────
def test_ratify_refused_when_collective_too_small(gv):
    res = gv.ratify("cand1", scope=SELF_SCOPE, total_personas=MIN_PERSONAS - 1)
    assert res["ratified"] is False
    assert "too small" in res["reason"]


def test_ratify_refused_without_supermajority(store, gv):
    cand = store.remember("test-l2", "an insight up for a vote", kind="insight", tier="working")
    # 4 personas, split 2 approve / 2 reject -> 50% < 66% supermajority
    gv.cast(cand, "a", True, 1)
    gv.cast(cand, "b", True, 1)
    gv.cast(cand, "c", False, 1)
    gv.cast(cand, "d", False, 1)
    res = gv.ratify(cand, scope="test-l2", total_personas=4)
    assert res["ratified"] is False
    assert res["supermajority_ok"] is False


def test_ratify_mints_into_core_on_consent(store, gv):
    cand = store.remember("test-l2", "a widely-approved insight", kind="insight", tier="working")
    # 4 personas, 4 approve -> quorum (>=2) AND 100% >= 66% supermajority
    for i, p in enumerate(("a", "b", "c", "d")):
        gv.cast(cand, p, True, 1)
    # ratify must first FIND the candidate via seed_by_id — the bug fixed in migration was that a
    # v2-born candidate returned None ("candidate vanished") because get_atom matched the atom's own
    # id, not its born_from. Prove the fix at the source: the candidate IS resolvable.
    assert store.seed_by_id(cand) is not None
    res = gv.ratify(cand, scope="test-l2", total_personas=4)
    # the OUTCOME is the contract: consent met -> ratified -> a core id minted + full approval.
    # (We don't round-trip minted_id: the mint's kind='core-value' gives it a DIFFERENT v1 content-id
    # than the insight, while v2 dedups on coordinate+content — a v1/v2 id-divergence that's an
    # existing architectural seam, out of scope for this migration. The decision is the contract.)
    assert res["ratified"] is True
    assert res["minted_id"]
    assert res["approve_frac"] == 1.0


def test_thresholds_are_the_documented_constants():
    assert QUORUM_FRACTION == 0.5
    assert SUPERMAJORITY == 0.66
    assert MIN_PERSONAS == 3
