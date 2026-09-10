"""Focused unit tests for echelon_engine.atoms.identity — the soul-seeding atom.

seed_soul / seed_insights mint the canonical CVs + grown principles + the canary + the
formative insights into a scope at tier='core'. Idempotent by [TAG] (boot calls it every
waking, so a re-seed must add nothing). Depends only on store (migrated sibling).

Isolated SeedStore (the store.py v2_db fix) so the real soul scope is never touched —
we seed into a tmp 'echelon-self' and assert against the tmp store.
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms import identity


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


def test_seed_soul_writes_into_an_empty_core(store):
    n = identity.seed_soul(store, scope="test-self")
    assert n > 0                                  # CVs + principles + canary + insights minted
    core = store.seeds(scope="test-self", tier="core")
    assert core                                   # they landed at tier=core
    contents = " ".join(s.content for s in core)
    assert "[CANARY]" in contents                 # the tripwire is present
    assert identity.CANARY in contents


def test_seed_soul_is_idempotent_by_tag(store):
    first = identity.seed_soul(store, scope="test-self")
    second = identity.seed_soul(store, scope="test-self")  # boot re-seeds every waking
    assert first > 0
    assert second == 0                            # nothing new on the second pass


def test_seeded_soul_surfaces_in_the_core_query(store):
    identity.seed_soul(store, scope="test-self")
    # everything identity mints is written at tier='core' (the floor the agent wakes with),
    # so the core-tier query surfaces it.
    core = store.seeds(scope="test-self", tier="core")
    assert len(core) > 0
    assert any("[CV-001]" in s.content for s in core)
    # NOTE (learned in migration): seeds() reads the v1∪v2 UNION, and a v2 atom (cards) carries
    # NO real tier — the union reconstructs it as tier='working' (the lossy-shadow of the v2
    # read-flip, see store.seeds). So the SAME soul content also appears in a tier='working'
    # query via its v2 twin. That is an expected limitation of the current v2 read, NOT a
    # mis-seed: identity writes core, the v2 twin just lacks a tier column to carry it.
    working = store.seeds(scope="test-self", tier="working")
    soul_contents = {s.content for s in core}
    assert any(w.content in soul_contents for w in working)   # the v2 twins, tier-flattened


def test_seed_insights_subset_of_seed_soul(store):
    # seed_soul calls seed_insights internally; calling insights alone on a fresh scope
    # mints only the texture half (fewer than the whole soul).
    only_insights = identity.seed_insights(store, scope="ins-only")
    assert only_insights > 0
    # a fresh scope seeded with the WHOLE soul writes strictly more
    full = identity.seed_soul(store, scope="full-soul")
    assert full > only_insights


def test_self_scope_constant():
    assert identity.SELF_SCOPE == "echelon-self"
