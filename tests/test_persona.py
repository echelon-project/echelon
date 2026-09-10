"""Focused unit tests for echelon_engine.atoms.persona — per-persona soul over one store.

A Persona wraps a SeedStore with its own scope (persona:<id>) and an inheritance edge
(persona --shares_soul--> global soul), so warmth(persona) also draws the global CVs down.
It writes ONLY into its own scope — never the global soul. Depends on identity (sibling)
+ the pure scopegraph leaf (now in sdk).

Isolated SeedStore on tmp dbs (the store.py v2_db fix).
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.persona import Persona, persona_scope
from echelon_engine.atoms.identity import SELF_SCOPE


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


def test_persona_scope_is_namespaced_and_sanitized():
    assert persona_scope("alice") == "persona:alice"
    assert persona_scope("Bob Smith!") == "persona:bob_smith"   # lowered + sanitized


def test_persona_remember_writes_only_its_own_scope(store):
    p = Persona("alice", store)
    p.remember("alice's private note", coordinate="tooling:x")
    own = p.own_seeds()
    assert any("alice's private note" in s.content for s in own)
    # the GLOBAL soul scope must NOT have received it
    glob = store.seeds(scope=SELF_SCOPE)
    assert not any("alice's private note" in s.content for s in glob)


def test_two_personas_have_separate_souls(store):
    a = Persona("alice", store)
    b = Persona("bob", store)
    a.remember("alpha secret", coordinate="x:a")
    b.remember("beta secret", coordinate="x:b")
    a_contents = {s.content for s in a.own_seeds()}
    b_contents = {s.content for s in b.own_seeds()}
    assert "alpha secret" in a_contents and "beta secret" not in a_contents
    assert "beta secret" in b_contents and "alpha secret" not in b_contents


def test_inherited_global_reads_the_global_soul(store):
    store.remember(SELF_SCOPE, "[CV-001] a global core value", kind="cv", tier="core")
    p = Persona("alice", store)
    inherited = p.inherited_global()
    assert any("[CV-001]" in s.content for s in inherited)


def test_persona_inheritance_edge_is_wired(store):
    p = Persona("alice", store)
    # the persona's scope graph holds the shares_soul edge to the global scope
    assert p.scope == "persona:alice"
    assert p.global_scope == SELF_SCOPE
    # warmth() draws the global down via this edge (smoke: it runs without error, scoped to persona)
    r = p.warmth("a thought")
    assert hasattr(r, "verdict")
