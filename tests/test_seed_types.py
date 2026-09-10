"""Focused unit tests for echelon_engine.atoms.seed_types — Seed dataclass leaf.

No IO, no DB. Tests the dataclass and its __post_init__ id derivation in isolation.
"""
import pytest

from echelon_engine.atoms.seed_types import Seed
from echelon_engine.atoms.addressing import content_id
from echelon_engine.atoms.scope_map import scope_to_domain


def test_seed_auto_fills_id_matching_uame_scheme():
    s = Seed(scope="test-x", content="a fact", coordinate="tooling:x")
    expected_domain = scope_to_domain("test-x", "tooling:x")
    expected_id = content_id("a fact", expected_domain, "note")
    assert s.id == expected_id
    assert len(s.id) == 16


def test_seed_id_is_deterministic():
    s1 = Seed(scope="test-x", content="a fact", coordinate="tooling:x")
    s2 = Seed(scope="test-x", content="a fact", coordinate="tooling:x")
    assert s1.id == s2.id


def test_seed_defaults():
    s = Seed(scope="test", content="x")
    assert s.kind == "note"
    assert s.tier == "working"
    assert s.supersedes == ""
    assert s.valence == 0.0
    assert s.arousal == 0.0
    assert s.score == 100.0
    assert s.recall_count == 0
    assert s.coordinate == ""
    assert s.self_seed is False


def test_seed_to_dict_includes_all_fields():
    s = Seed(scope="test", content="x")
    d = s.to_dict()
    assert "scope" in d
    assert "content" in d
    assert "id" in d
    assert d["content"] == "x"


def test_seed_explicit_id_not_overwritten():
    s = Seed(scope="test", content="x", id="myid")
    assert s.id == "myid"


def test_seed_different_content_gives_different_id():
    s1 = Seed(scope="test", content="alpha")
    s2 = Seed(scope="test", content="beta")
    assert s1.id != s2.id
