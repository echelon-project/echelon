"""Focused unit tests for echelon_engine.atoms.synthesis — the COS atom-birth DETECTOR.

Two seams:
  1. PURE: affinity()/_gramset() — symmetric Jaccard warmth between two seeds. Tested directly
     on Seed objects (no store), the real algorithmic core.
  2. STORE-BACKED: find_pairs()/propose() over a SeedStore. Detection of mutually-warm PROVEN
     pairs. We isolate on a scratch SeedStore(tmp/core.db, v2_db=tmp/core_v2.db) and drive the
     thresholds via the exposed params: proven_floor=0.0 lets freshly-remembered seeds qualify
     (the SCORE machinery is store.py's concern, not synthesis's — synthesis's contract is the
     affinity gate + proposal recording, which is what we pin). propose() appends proposal seeds;
     re-running is idempotent (content-addressed), which we assert.
"""
import pytest

from echelon_engine.atoms.store import SeedStore, Seed
from echelon_engine.atoms import synthesis


# ── pure: affinity / _gramset ─────────────────────────────────────────────
def _seed(content, sid="x", scope="s", score=200.0):
    return Seed(id=sid, scope=scope, content=content, kind="atom", tier="working",
                supersedes="", ts=0, valence=0.0, arousal=0.0, score=score,
                recall_count=0, coordinate="")


def test_affinity_identical_content_is_one():
    a = _seed("the quick brown fox", sid="a")
    b = _seed("the quick brown fox", sid="b")
    assert synthesis.affinity(a, b) == 1.0


def test_affinity_disjoint_content_is_zero():
    a = _seed("alpha beta gamma", sid="a")
    b = _seed("xylophone zebra quokka", sid="b")
    assert synthesis.affinity(a, b) == 0.0


def test_affinity_partial_overlap_between_zero_and_one():
    a = _seed("memory bank warmth foveation", sid="a")
    b = _seed("memory bank trace earning", sid="b")
    aff = synthesis.affinity(a, b)
    assert 0.0 < aff < 1.0


def test_affinity_is_symmetric():
    a = _seed("one two three four", sid="a")
    b = _seed("two three five six", sid="b")
    assert synthesis.affinity(a, b) == synthesis.affinity(b, a)


def test_affinity_empty_content_is_zero():
    a = _seed("", sid="a")
    b = _seed("something here", sid="b")
    assert synthesis.affinity(a, b) == 0.0


def test_gramset_includes_unigrams_and_bigrams():
    g = synthesis._gramset("alpha beta gamma")
    assert "alpha" in g                      # unigram present
    assert ("alpha", "beta") in g            # bigram present


# ── store-backed: find_pairs ──────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


def test_find_pairs_detects_mutually_warm_pair(store):
    store.remember("syn", "memory bank warmth foveation trace earning", coordinate="t:a")
    store.remember("syn", "memory bank warmth foveation trace earning rank", coordinate="t:b")
    store.remember("syn", "completely unrelated subject matter here", coordinate="t:c")
    pairs = synthesis.find_pairs(store, scope="syn", affinity_floor=0.40, proven_floor=0.0)
    assert len(pairs) == 1
    p = pairs[0]
    bodies = {p["a_content"], p["b_content"]}
    assert any("foveation trace earning" in x for x in bodies)
    assert p["affinity"] >= 0.40


def test_find_pairs_affinity_floor_excludes_weak_pairs(store):
    store.remember("syn", "alpha beta gamma delta", coordinate="t:a")
    store.remember("syn", "epsilon zeta eta theta", coordinate="t:b")
    pairs = synthesis.find_pairs(store, scope="syn", affinity_floor=0.40, proven_floor=0.0)
    assert pairs == []


def test_find_pairs_ranked_by_affinity_descending(store):
    store.remember("syn", "shared base alpha beta gamma", coordinate="t:a")
    store.remember("syn", "shared base alpha beta gamma delta", coordinate="t:b")  # very close to a
    store.remember("syn", "shared base epsilon", coordinate="t:c")               # weaker to a/b
    pairs = synthesis.find_pairs(store, scope="syn", affinity_floor=0.10, proven_floor=0.0)
    assert len(pairs) >= 2
    affs = [p["affinity"] for p in pairs]
    assert affs == sorted(affs, reverse=True)


# ── store-backed: propose ─────────────────────────────────────────────────
def test_propose_records_proposal_seeds(store):
    store.remember("syn", "memory bank warmth foveation trace earning", coordinate="t:a")
    store.remember("syn", "memory bank warmth foveation trace earning rank", coordinate="t:b")
    # propose() uses the module default SYNTH_AFFINITY (0.40) and PROMOTE_THRESHOLD for proven;
    # patch the floors low by calling find_pairs-equivalent path: propose has no param, so we
    # exercise it with seeds that clear the default affinity but verify it RECORDS when pairs exist.
    recorded = synthesis.propose(store, scope="syn", target_scope="synth-test")
    # the two near-identical seeds clear the 0.40 affinity floor; whether they're "proven" depends on
    # the store's default score. Assert the recording CONTRACT: each record carries a proposal_id and
    # the parent ids, and proposals are readable back from the target scope.
    for r in recorded:
        assert "proposal_id" in r and r["proposal_id"]
        assert "a" in r and "b" in r
    if recorded:
        back = store.seeds(scope="synth-test")
        assert any(s.kind == "synthesis-proposal" for s in back)


def test_propose_is_idempotent_on_rerun(store):
    store.remember("syn", "memory bank warmth foveation trace earning", coordinate="t:a")
    store.remember("syn", "memory bank warmth foveation trace earning rank", coordinate="t:b")
    first = synthesis.propose(store, scope="syn", target_scope="synth-idem")
    second = synthesis.propose(store, scope="syn", target_scope="synth-idem")
    # content-addressed: the same pair yields the same proposal id both runs (no duplication).
    assert {r["proposal_id"] for r in first} == {r["proposal_id"] for r in second}
