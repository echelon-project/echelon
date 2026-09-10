"""Focused unit tests for echelon_engine.atoms.store — the scope adapter over uame+cards.

SeedStore keeps the remember()/seeds()/count()/promote() API while the storage is the
two-prefix UAME (v1, now frozen) + the v2 CardStore (PRIMARY). Since v1 is frozen, a
new atom is born in v2 only and read back via the v1∪v2 union in seeds().

MIGRATION BUG FIXED HERE: the original hardwired the v2 CardStore to the GLOBAL
DEFAULT_V2_DB regardless of db_path, so an isolated SeedStore was impossible (every
remember() hit the real soul bank). The fix adds a `v2_db` param (defaults to global).
test_isolated_store_does_not_touch_global_v2 pins it.
"""
import pytest

from echelon_engine.atoms.store import SeedStore, scope_to_domain, DEFAULT_V2_DB


@pytest.fixture
def store(tmp_path):
    # both stores redirected to scratch — the bug fix makes this real isolation
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


# ── scope_to_domain (the routing helper) ──────────────────────────────────
def test_coordinate_wins_over_scope():
    assert scope_to_domain("anyscope", "tooling:bash:gzip") == "tooling"


def test_testish_scopes_route_to_test_domain():
    assert scope_to_domain("test-foo") == "test"
    assert scope_to_domain("grow-x") == "test"


def test_plain_scope_sanitized_to_domain():
    assert scope_to_domain("my scope") == "my_scope"
    assert scope_to_domain("") == "general"


# ── remember / read-back (through the v1∪v2 union) ────────────────────────
def test_remember_returns_content_addressed_id(store):
    id1 = store.remember("test-x", "a fact", coordinate="tooling:x")
    id2 = store.remember("test-x", "a fact", coordinate="tooling:x")
    assert id1 == id2                          # same content => same id (dedup)
    assert len(id1) == 16


def test_remembered_seed_reads_back_in_scope(store):
    store.remember("test-scope", "remember me", coordinate="tooling:y")
    found = store.seeds(scope="test-scope")
    assert any(s.content == "remember me" for s in found)


def test_seeds_scope_filter_excludes_other_scopes(store):
    store.remember("scope-a", "alpha", coordinate="tooling:a")
    store.remember("scope-b", "beta", coordinate="tooling:b")
    a = [s.content for s in store.seeds(scope="scope-a")]
    assert "alpha" in a and "beta" not in a


# ── the migration bug fix: real isolation ─────────────────────────────────
def test_isolated_store_does_not_touch_global_v2(tmp_path):
    # the WHOLE point of the v2_db fix: a scratch store writes the scratch v2 db,
    # never the global soul bank. Assert the store's CardStore points at the tmp path.
    scratch_v2 = tmp_path / "scratch_v2.db"
    s = SeedStore(tmp_path / "core.db", v2_db=scratch_v2)
    assert s.cards.db_path == scratch_v2
    assert s.cards.db_path != DEFAULT_V2_DB
    # and a write lands in the scratch v2, provable by reading it back from THIS store
    s.remember("iso", "isolated fact", coordinate="tooling:iso")
    assert any(x.content == "isolated fact" for x in s.seeds(scope="iso"))


def test_default_v2_db_preserved_when_not_overridden():
    # backward-compat: omit v2_db and the global default is used (existing call sites unchanged).
    # Checked via the signature default, NOT by constructing — constructing would open + migrate
    # the REAL global soul bank, which a unit test must never touch.
    import inspect
    from echelon_engine.atoms.store import SeedStore as S
    default = inspect.signature(S.__init__).parameters["v2_db"].default
    assert default is None                     # None => __init__ falls back to DEFAULT_V2_DB
